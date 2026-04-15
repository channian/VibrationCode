"""
baseline_detector.py — 基準期自動偵測（三層篩選）

BaselineDetector.detect()
    在無人工指定基準期時，自動掃描設備資料中最穩定的 14 天時段，
    作為 VFDEdgeHealthModel 的健康訓練基準。

resolve_baseline()
    整合人工指定與自動偵測邏輯：
    - device_mapping 已有 train_start/train_end → 直接使用
    - 否則呼叫 BaselineDetector，使用 Rank 1 候選

輸出：
    output/baseline_candidates/{device_id}_candidates.csv
    欄位: rank, start_date, end_date, accOA_median, vRMS_median,
          accOA_cv, vRMS_cv, cf_cv, stability_score, n_rows
"""

import os
import logging
import numpy as np
import pandas as pd

from config import settings

logger = logging.getLogger(__name__)


def _cv(series: pd.Series) -> float:
    """
    計算變異係數 CV = std / mean。
    mean 為 0 或 NaN 時回傳 NaN。
    """
    m = series.mean()
    if np.isnan(m) or m == 0:
        return np.nan
    return series.std() / m


class BaselineDetector:
    """
    三層基準期自動偵測器。

    第一層：14 天滾動窗口掃描
        stability_score = accOA_median × accOA_CV
        取分數最低的前 BASELINE_PREFILTER_N 個窗口

    第二層：多特徵 CV 門檻驗證
        accOA_CV      < CV_THRESHOLDS['accOA']
        Total_vRMS_CV < CV_THRESHOLDS['Total_vRMS']
        Crest_Factor_CV < CV_THRESHOLDS['Crest_Factor']
        (缺少對應欄位時略過該條件)

    第三層：輸出候選 CSV 供人工確認
    """

    def detect(
        self,
        df: pd.DataFrame,
        time_col: str = 'datetime',
        top_n: int = settings.BASELINE_TOP_N,
        device_id: str = '',
        output_dir: str = '',
    ) -> pd.DataFrame:
        """
        掃描 df 找出最佳基準期候選。

        Args:
            df:         已清洗的振動 DataFrame（需含 accOA）
            time_col:   時間欄位名稱
            top_n:      回傳前 N 個候選
            device_id:  用於 log 與輸出檔名
            output_dir: 候選 CSV 輸出目錄（空字串則不輸出）

        Returns:
            DataFrame，欄位：rank, start_date, end_date, accOA_median, vRMS_median,
                              accOA_cv, vRMS_cv, cf_cv, stability_score, n_rows
            資料不足時回傳空 DataFrame。
        """
        tag = f"[baseline] {device_id}:" if device_id else "[baseline]"

        if df.empty:
            logger.warning(f"{tag} empty DataFrame — cannot detect baseline")
            return pd.DataFrame()

        if 'accOA' not in df.columns:
            logger.warning(f"{tag} missing 'accOA' column — cannot detect baseline")
            return pd.DataFrame()

        df = df.sort_values(time_col).reset_index(drop=True)
        t_min = df[time_col].min()
        t_max = df[time_col].max()
        span_days = (t_max - t_min).days

        window = pd.Timedelta(days=settings.BASELINE_WINDOW_DAYS)
        step   = pd.Timedelta(days=settings.BASELINE_STEP_DAYS)

        # ── 第一層：滾動窗口掃描 ──────────────────────────────
        candidates = []

        if span_days < settings.BASELINE_WINDOW_DAYS:
            logger.warning(
                f"{tag} data span {span_days} days < "
                f"window {settings.BASELINE_WINDOW_DAYS} days — using full range as single candidate"
            )
            row = self._score_window(df, t_min, t_max)
            if row:
                candidates.append(row)
        else:
            current = t_min
            while current + window <= t_max + step:
                w_end = min(current + window, t_max)
                mask = (df[time_col] >= current) & (df[time_col] < current + window)
                row = self._score_window(df[mask], current, current + window)
                if row:
                    candidates.append(row)
                current += step

        if not candidates:
            logger.warning(
                f"{tag} no valid windows found "
                f"(all windows < {settings.MIN_SAMPLES_PER_BIN} rows)"
            )
            return pd.DataFrame()

        df_cands = (
            pd.DataFrame(candidates)
            .sort_values('stability_score')
            .reset_index(drop=True)
        )

        logger.info(
            f"{tag} Layer 1: {len(df_cands)} valid windows scanned, "
            f"keeping top {settings.BASELINE_PREFILTER_N}"
        )
        df_layer1 = df_cands.head(settings.BASELINE_PREFILTER_N)

        # ── 第二層：CV 門檻過濾 ──────────────────────────────
        df_layer2 = self._apply_cv_filters(df_layer1, tag)

        # ── 取前 top_n，補 rank 欄 ───────────────────────────
        df_result = df_layer2.head(top_n).reset_index(drop=True).copy()
        df_result.insert(0, 'rank', range(1, len(df_result) + 1))

        # 欄位順序整理
        col_order = [
            'rank', 'start_date', 'end_date',
            'accOA_median', 'vRMS_median',
            'accOA_cv', 'vRMS_cv', 'cf_cv',
            'stability_score', 'n_rows',
        ]
        df_result = df_result[[c for c in col_order if c in df_result.columns]]

        # ── 第三層：輸出 CSV ──────────────────────────────────
        if output_dir and device_id:
            os.makedirs(output_dir, exist_ok=True)
            out_path = os.path.join(output_dir, f"{device_id}_candidates.csv")
            df_result.to_csv(out_path, index=False)
            logger.info(f"{tag} candidates saved → {out_path}")

        # ── Rank 1 log ────────────────────────────────────────
        if not df_result.empty:
            r1 = df_result.iloc[0]
            logger.info(
                f"{tag} Rank1 {r1['start_date'].date()} → {r1['end_date'].date()} | "
                f"score={r1['stability_score']:.4f} | n={int(r1['n_rows'])} "
                f"【自動選定，建議人工確認】"
            )
        else:
            logger.warning(f"{tag} no baseline candidates returned")

        return df_result

    # ── 內部輔助方法 ─────────────────────────────────────────

    @staticmethod
    def _score_window(
        window_df: pd.DataFrame,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> dict | None:
        """
        計算單一窗口的穩定性分數。
        樣本數不足 MIN_SAMPLES_PER_BIN 或 accOA 全為 NaN 時回傳 None。
        """
        if len(window_df) < settings.MIN_SAMPLES_PER_BIN:
            return None

        accOA_col = window_df['accOA'].dropna()
        if len(accOA_col) < settings.MIN_SAMPLES_PER_BIN:
            return None

        accOA_median = float(accOA_col.median())
        accOA_cv     = _cv(accOA_col)
        if np.isnan(accOA_cv):
            return None

        stability_score = accOA_median * accOA_cv

        # Total_vRMS（選用）
        vRMS_median = np.nan
        vRMS_cv     = np.nan
        if 'Total_vRMS' in window_df.columns:
            col = window_df['Total_vRMS'].dropna()
            if len(col) >= 2:
                vRMS_median = float(col.median())
                vRMS_cv     = _cv(col)

        # Crest_Factor（選用）
        cf_cv = np.nan
        if 'Crest_Factor' in window_df.columns:
            col = window_df['Crest_Factor'].dropna()
            if len(col) >= 2:
                cf_cv = _cv(col)

        return {
            'start_date':      start,
            'end_date':        end,
            'accOA_median':    round(accOA_median, 6),
            'vRMS_median':     round(vRMS_median, 6) if not np.isnan(vRMS_median) else np.nan,
            'accOA_cv':        round(accOA_cv, 6),
            'vRMS_cv':         round(vRMS_cv, 6) if not np.isnan(vRMS_cv) else np.nan,
            'cf_cv':           round(cf_cv, 6) if not np.isnan(cf_cv) else np.nan,
            'stability_score': round(stability_score, 6),
            'n_rows':          len(window_df),
        }

    @staticmethod
    def _apply_cv_filters(df: pd.DataFrame, tag: str = '') -> pd.DataFrame:
        """
        第二層 CV 門檻過濾。
        若對應欄位全為 NaN（缺少振動特徵），則略過該條件。
        若過濾後全空，退回第一層結果並印出警告。
        """
        cv_t = settings.CV_THRESHOLDS
        mask = pd.Series([True] * len(df), index=df.index)

        if df['accOA_cv'].notna().any():
            mask &= df['accOA_cv'] < cv_t['accOA']

        if 'vRMS_cv' in df.columns and df['vRMS_cv'].notna().any():
            mask &= df['vRMS_cv'] < cv_t['Total_vRMS']

        if 'cf_cv' in df.columns and df['cf_cv'].notna().any():
            mask &= df['cf_cv'] < cv_t['Crest_Factor']

        filtered = df[mask]
        passed = int(mask.sum())
        logger.info(f"{tag} Layer 2: {passed}/{len(df)} candidates passed CV filters")

        if filtered.empty:
            logger.warning(
                f"{tag} Layer 2: no candidates passed CV filters — "
                f"relaxing to Layer 1 results (設備狀態可能持續偏差)"
            )
            return df

        return filtered


# ──────────────────────────────────────────────────────────────────────
# 公開輔助函式
# ──────────────────────────────────────────────────────────────────────

def resolve_baseline(
    df: pd.DataFrame,
    mapping_row: 'pd.Series | None',
    device_id: str,
    time_col: str = 'datetime',
    output_dir: str = '',
) -> tuple[pd.DataFrame, bool]:
    """
    決定實際使用的訓練基準期資料。

    優先順序：
      1. device_mapping.csv 中已有 train_start/train_end → 直接切片（人工指定）
      2. 否則呼叫 BaselineDetector.detect()，使用 Rank 1 候選（自動選定）

    Args:
        df:          已清洗、套用 apply_all_filters 後的振動 DataFrame
        mapping_row: device_mapping.csv 對應列（pd.Series），無則傳 None
        device_id:   設備識別碼（用於 log 與輸出）
        time_col:    時間欄位名稱
        output_dir:  基準期候選 CSV 輸出目錄

    Returns:
        (df_baseline, is_manual)
        df_baseline : 基準期內的 DataFrame 子集（空 DataFrame 表示失敗）
        is_manual   : True = 人工指定，False = 自動選定
    """
    tag = f"[baseline] {device_id}:"

    # ── 嘗試人工指定基準期 ────────────────────────────────────
    if mapping_row is not None:
        ts = mapping_row.get('train_start') if hasattr(mapping_row, 'get') else None
        te = mapping_row.get('train_end')   if hasattr(mapping_row, 'get') else None

        # 若 Series 透過 [] 存取
        if ts is None:
            try:
                ts = mapping_row['train_start']
                te = mapping_row['train_end']
            except (KeyError, TypeError):
                ts = te = None

        if pd.notna(ts) and pd.notna(te):
            ts, te = pd.Timestamp(ts), pd.Timestamp(te)
            mask = (df[time_col] >= ts) & (df[time_col] <= te)
            df_baseline = df[mask].reset_index(drop=True)
            if not df_baseline.empty:
                logger.info(
                    f"{tag} using manual baseline {ts.date()} → {te.date()} "
                    f"({len(df_baseline)} rows)"
                )
                return df_baseline, True
            logger.warning(
                f"{tag} manual baseline {ts.date()} → {te.date()} yields no data "
                f"— falling back to auto detection"
            )

    # ── 自動偵測 ──────────────────────────────────────────────
    detector = BaselineDetector()
    candidates = detector.detect(
        df,
        time_col=time_col,
        device_id=device_id,
        output_dir=output_dir,
    )

    if candidates.empty:
        logger.error(f"{tag} could not determine any baseline period")
        return pd.DataFrame(), False

    r1 = candidates.iloc[0]
    mask = (df[time_col] >= r1['start_date']) & (df[time_col] < r1['end_date'])
    df_baseline = df[mask].reset_index(drop=True)

    if df_baseline.empty:
        logger.error(f"{tag} Rank1 candidate yielded empty baseline slice")
        return pd.DataFrame(), False

    logger.info(
        f"{tag} auto baseline applied: {r1['start_date'].date()} → "
        f"{r1['end_date'].date()} ({len(df_baseline)} rows)"
    )
    return df_baseline, False
