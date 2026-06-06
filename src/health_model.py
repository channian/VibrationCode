"""
health_model.py — VFDEdgeHealthModel v2

依電流工況分層（Load Binning）+ Mahalanobis 距離評分的設備健康模型。

核心流程：
  train(df_baseline)  → 分層 → 每層訓練 Mahalanobis 模型 → 自動校準 λ
  predict(df)         → 分配 bin → 計算距離 → 輸出 Health_Score / alert_level
  save / load         → pickle 序列化

特別說明：
  - 無電流資料時自動降為單一 bin（不分層）
  - 協方差矩陣以 np.linalg.pinv 取偽逆，防奇異
  - NaN 特徵列不中斷流程，Health_Score 設 NaN
"""

import os
import math
import pickle
import logging
import numpy as np
import pandas as pd
from scipy.spatial.distance import mahalanobis

from config import settings

logger = logging.getLogger(__name__)

FEATURES = settings.FEATURES  # ['Total_vRMS', 'accOA', 'Crest_Factor']

# 頻率欄位關鍵字（優先於電流做工況分層）
_FREQ_KEYWORDS = ('頻率', 'freq', 'hz', 'frequency')


def _detect_bin_col(df: pd.DataFrame) -> str | None:
    """自動偵測分層欄位：頻率相關欄位優先，其次 current_A。"""
    for col in df.columns:
        if any(kw.lower() in col.lower() for kw in _FREQ_KEYWORDS):
            if df[col].notna().sum() >= 10:
                return col
    if 'current_A' in df.columns and df['current_A'].notna().sum() >= 10:
        return 'current_A'
    return None


# ──────────────────────────────────────────────────────────
# 內部工具
# ──────────────────────────────────────────────────────────

def _mahal_dist(x: np.ndarray, mean: np.ndarray, cov_inv: np.ndarray) -> float:
    """計算單筆樣本的 Mahalanobis 距離，含異常防護。"""
    try:
        d = mahalanobis(x, mean, cov_inv)
        return float(d) if np.isfinite(d) else np.nan
    except Exception:
        return np.nan


def _fit_bin(X: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """
    對一個 bin 的特徵矩陣計算 mean vector 與 covariance pseudo-inverse。
    樣本數不足或全為 NaN 時回傳 None。
    """
    if len(X) < 2:
        return None
    mean = np.nanmean(X, axis=0)
    # 以有效樣本估算協方差（rowvar=False: 每欄是一個特徵）
    valid = X[~np.isnan(X).any(axis=1)]
    if len(valid) < 2:
        return None
    cov = np.cov(valid, rowvar=False)
    if cov.ndim == 0:               # 只有一個特徵的退化情況
        cov = np.array([[float(cov)]])
    cov_inv = np.linalg.pinv(cov)   # pseudo-inverse，防奇異
    return mean, cov_inv


def _calibrate_lambda(dists: np.ndarray, target_score: float = settings.SCORE_AT_95TH) -> float:
    """
    自動校準 λ：讓基準期 95th percentile 距離對應 target_score 分。
    λ = -ln(target_score / 100) / dist_95th
    """
    valid = dists[np.isfinite(dists)]
    if len(valid) == 0:
        return 1.0
    dist_95 = float(np.percentile(valid, 95))
    if dist_95 <= 0:
        return 1.0
    lam = -math.log(target_score / 100.0) / dist_95
    return lam


def _score(dist: float, lam: float) -> float:
    """health_score = clip(100 × exp(−λ × dist), 0, 100)"""
    if np.isnan(dist):
        return np.nan
    return float(np.clip(100.0 * math.exp(-lam * dist), 0.0, 100.0))


def _alert_level(score: float) -> str:
    if np.isnan(score):
        return 'Unknown'
    if score >= settings.ALERT_NORMAL:
        return 'Normal'
    if score >= settings.ALERT_WARNING:
        return 'Warning'
    return 'Critical'


# ──────────────────────────────────────────────────────────
# 主模型類別
# ──────────────────────────────────────────────────────────

class VFDEdgeHealthModel:
    """
    VFD 設備振動健康評估模型 v2。

    Attributes:
        load_bins       : 目標分層數（有分層依據時使用）
        min_bin_samples : 每層最低樣本數，不足則合併
        _bins           : list[dict] 每層的模型參數
        _bin_col        : 分層依據欄位名稱（頻率 > 電流，None = 不分層）
        _bin_edges      : 工況分層邊界（pd.qcut 產出）
        _has_current    : backward compat 旗標（_bin_col is not None 時為 True）
        _features       : 實際使用的特徵欄位（訓練時確認存在的子集）
    """

    def __init__(self, load_bins: int = settings.DEFAULT_N_BINS,
                 min_bin_samples: int = settings.MIN_SAMPLES_PER_BIN):
        self.load_bins       = load_bins
        self.min_bin_samples = min_bin_samples
        self._bins: list[dict] = []
        self._bin_col: str | None = None
        self._bin_edges      = None
        self._has_current    = False   # backward compat
        self._features: list[str] = []

    def __setstate__(self, state: dict) -> None:
        """舊版 pickle 補齊 _bin_col（backward compat）。"""
        self.__dict__.update(state)
        if not hasattr(self, '_bin_col'):
            self._bin_col = 'current_A' if getattr(self, '_has_current', False) else None

    # ── 訓練 ────────────────────────────────────────────────

    def train(self, df_baseline: pd.DataFrame,
              bin_col: str | None = None) -> None:
        """
        以基準期資料訓練模型。

        Args:
            df_baseline: 已清洗的基準期 DataFrame
                         必有 FEATURES 欄位（允許部分 NaN）；
                         選填工況欄位（頻率或電流，無則不分層）
            bin_col    : 明確指定分層欄位；若為 None 則自動偵測
                         （頻率關鍵字欄位優先，其次 current_A）
        """
        if df_baseline.empty:
            raise ValueError("train(): df_baseline is empty")

        # 確認實際存在的特徵欄位
        self._features = [f for f in FEATURES if f in df_baseline.columns]
        if not self._features:
            raise ValueError(f"train(): none of {FEATURES} found in df_baseline")

        # 決定分層欄位
        if bin_col and bin_col in df_baseline.columns and df_baseline[bin_col].notna().any():
            self._bin_col = bin_col
        else:
            self._bin_col = _detect_bin_col(df_baseline)
        self._has_current = self._bin_col is not None   # backward compat

        # ── 工況分層 ────────────────────────────────────────
        if self._bin_col:
            df_labeled, self._bin_edges = self._bin_by_col(df_baseline, self._bin_col)
            logger.info(f"train: 分層依據欄位 = {self._bin_col!r}")
        else:
            df_labeled = df_baseline.copy()
            df_labeled['_bin'] = 0
            self._bin_edges = None
            logger.info("train: 無工況欄位 — 使用單一 bin")

        # ── 每層訓練 ────────────────────────────────────────
        self._bins = []
        bin_ids = sorted(df_labeled['_bin'].unique())

        for bid in bin_ids:
            subset = df_labeled[df_labeled['_bin'] == bid]
            X = subset[self._features].values.astype(float)
            result = _fit_bin(X)
            if result is None:
                logger.warning(f"train: bin {bid} has insufficient data — skipped")
                continue
            mean, cov_inv = result

            # 計算基準期內所有距離 → 校準 λ
            dists = np.array([_mahal_dist(row, mean, cov_inv) for row in X])
            lam = _calibrate_lambda(dists)
            baseline_scores = np.array([_score(d, lam) for d in dists])
            avg_score = float(np.nanmean(baseline_scores))

            self._bins.append({
                'bin_id':    bid,
                'mean':      mean,
                'cov_inv':   cov_inv,
                'lambda':    lam,
                'n_train':   len(subset),
                'avg_score': avg_score,
            })
            logger.info(
                f"train: bin {bid} | n={len(subset)} | "
                f"λ={lam:.4f} | avg_baseline_score={avg_score:.1f}"
            )

        if not self._bins:
            raise RuntimeError("train(): all bins failed — cannot build model")

        avg_all = float(np.mean([b['avg_score'] for b in self._bins]))
        logger.info(f"train: complete | {len(self._bins)} bin(s) | overall avg_score={avg_all:.1f}")
        if avg_all < 85:
            logger.warning(
                f"train: baseline avg_score={avg_all:.1f} < 85 — "
                f"check baseline period quality"
            )

    # ── 推論 ────────────────────────────────────────────────

    def predict(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        對全量資料推論健康分數。

        新增欄位：
            load_bin      : 電流 bin 編號（整數）
            Health_Score  : 0–100，NaN 表示特徵不完整
            alert_level   : Normal / Warning / Critical / Unknown

        Returns:
            含新欄位的 DataFrame（原始順序保留）
        """
        if not self._bins:
            raise RuntimeError("predict(): model not trained")

        df = df.copy()

        # 分配 bin
        if self._bin_col and self._bin_edges is not None:
            df['load_bin'] = self._assign_bin(df)
        else:
            df['load_bin'] = 0

        # 逐列計算分數
        scores = np.full(len(df), np.nan)
        for i, row in enumerate(df[self._features].values.astype(float)):
            if np.isnan(row).any():
                continue
            bid = int(df['load_bin'].iloc[i])
            bin_params = self._get_bin_params(bid)
            if bin_params is None:
                continue
            d = _mahal_dist(row, bin_params['mean'], bin_params['cov_inv'])
            scores[i] = _score(d, bin_params['lambda'])

        df['Health_Score'] = scores
        df['alert_level']  = df['Health_Score'].apply(_alert_level)

        # ── 平滑分數（用於趨勢圖與警報，不取代原始分）──────
        w = settings.SCORE_SMOOTH_WINDOW
        if w and w > 1:
            df['health_score_smooth'] = (
                df['Health_Score']
                .rolling(window=w, center=True, min_periods=1)
                .median()
                .ffill()
                .bfill()
            )
        else:
            df['health_score_smooth'] = df['Health_Score']

        return df

    # ── 序列化 ──────────────────────────────────────────────

    def save(self, path: str) -> None:
        """以 pickle 儲存模型至指定路徑。"""
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)
        with open(path, 'wb') as f:
            pickle.dump(self, f)
        logger.info(f"Model saved → {path}")

    @classmethod
    def load(cls, path: str) -> 'VFDEdgeHealthModel':
        """從 pickle 檔案載入模型。"""
        with open(path, 'rb') as f:
            model = pickle.load(f)
        logger.info(f"Model loaded ← {path}")
        return model

    # ── 資訊查詢 ────────────────────────────────────────────

    def summary(self) -> pd.DataFrame:
        """回傳各 bin 的訓練摘要 DataFrame。"""
        if not self._bins:
            return pd.DataFrame()
        return pd.DataFrame([{
            'bin_id':    b['bin_id'],
            'n_train':   b['n_train'],
            'lambda':    round(b['lambda'], 4),
            'avg_baseline_score': round(b['avg_score'], 1),
        } for b in self._bins])

    # ── 內部輔助 ────────────────────────────────────────────

    def _bin_by_col(self, df: pd.DataFrame, col: str) -> tuple[pd.DataFrame, object]:
        """
        以 pd.qcut 等樣本數對指定欄位分層。
        若樣本不足則自動降低分層數，直到每層 >= min_bin_samples。
        """
        n_bins = self.load_bins

        while n_bins >= 1:
            try:
                labels, bin_edges = pd.qcut(
                    df[col],
                    q=n_bins,
                    labels=False,
                    retbins=True,
                    duplicates='drop',
                )
                counts = labels.value_counts()
                if counts.min() >= self.min_bin_samples:
                    df = df.copy()
                    df['_bin'] = labels.astype('Int64').fillna(0).astype(int)
                    logger.info(
                        f"train: {col!r} binning → {n_bins} bin(s) "
                        f"(counts: {dict(counts.sort_index())})"
                    )
                    return df, bin_edges
            except Exception as e:
                logger.debug(f"qcut n_bins={n_bins} failed: {e}")
            n_bins -= 1

        logger.warning(
            f"train: 所有分層嘗試失敗，退回單一 bin（總樣本：{len(df)}）"
        )
        df = df.copy()
        df['_bin'] = 0
        return df, None

    def _assign_bin(self, df: pd.DataFrame) -> pd.Series:
        """
        將推論資料的 _bin_col 分配到訓練時的 bin。
        無資料 / 超出範圍 → bin 0。
        """
        if self._bin_edges is None or not self._bin_col or self._bin_col not in df.columns:
            return pd.Series(0, index=df.index)

        bins = pd.cut(
            df[self._bin_col],
            bins=self._bin_edges,
            labels=False,
            include_lowest=True,
        )
        max_bin = len(self._bin_edges) - 2
        result = bins.astype('Int64').fillna(0).astype(int).clip(0, max_bin)
        return result

    def _get_bin_params(self, bid: int) -> dict | None:
        """取得指定 bin 的模型參數；若不存在則回退到最近的 bin。"""
        for b in self._bins:
            if b['bin_id'] == bid:
                return b
        # bin 不存在（可能推論資料電流超出訓練範圍）→ 用最後一個 bin
        return self._bins[-1] if self._bins else None
