"""
deviation.py — 多變量偏離偵測（取代 0–100 健康分數）

為什麼不延用舊版 `src/health_model.py` 的 `Health_Score = 100 × exp(-λd)`
對外呈現（見計畫書 §四，這裡不重複列已在計畫書講清楚的三個問題，只講
本模組的取捨）：

- 0–100 分數需要一個「校準錨點」（舊版把基準期 95th percentile 錨定在
  80 分），這個錨點沒有物理意義，換一批基準期資料錨點就跟著飄，數字
  「看起來」精確但實際上任意。
- 分數是跨特徵、跨基準期壓縮後的單一純量，反而丟失了「哪個特徵在動」
  這個對 agent 和工程師都更有用的資訊。

本模組保留 Mahalanobis 距離機制本身（多變量同時小幅偏移，單一門檻規則
看不出來，仍有獨特價值），但**只輸出**：
  - `is_deviated`：是否偏離（布林）
  - `per_feature_sigma`：各特徵相對基準的標準化偏離量（沿用
    `BaselineStats.stats[f].sigma_of()`，以中位數/標準差為準，比平均數
    更抗離群值）
  - `top_contributors`：偏離量顯著的特徵，依 |σ| 由大到小排序

`per_feature_sigma` 用的是各特徵「各自」相對基準的標準化偏離（單變量
角度），`distance`（Mahalanobis 距離）則是「聯合」考慮特徵間相關性後的
整體偏離程度；兩者互補——distance 決定是否觸發，per_feature_sigma 決定
觸發時要指向哪個特徵，對應計畫書 §四的示意輸出。

協方差矩陣以 `np.linalg.pinv` 取偽逆，沿用舊版 `src/health_model.py`
`_fit_bin` 的做法：特徵數接近樣本數或特徵高度共線時，`cov` 可能奇異或
病態，直接 `inv` 會丟例外或炸出不合理的巨大距離，偽逆能穩定地退化處理
（奇異方向的距離貢獻視為 0）。

只使用 `data_status == 'ok'` 的列：`partial`/`no_data` 的數字不可信；
`not_running` 的振動是停機噪音，不該混進來平白拉高或拉低距離。

**範圍說明**：本模組只實作「給定基準期 → 擬合 → 對單一時間點評估」。
舊版 `VFDEdgeHealthModel` 另外做了「依電流/頻率自動分層（load binning）」
再各層獨立訓練；計畫書 §四提到工況分層邏輯保留，但 Phase 1 的規則契約
（`fit_deviation_model(agg, features, baseline)`）並未傳入分層欄位，
分層留給呼叫端在餵資料前自行依工況切片、對每個工況分別呼叫本模組。
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

from vibcore.types import BaselineStats, DeviationResult

logger = logging.getLogger(__name__)

#: 判定為「顯著貢獻者」的最低 |σ|；低於此值即使排名前幾也不列入
#: top_contributors，避免把雜訊當成重點呈現給 agent。
_CONTRIBUTOR_MIN_SIGMA = 1.0
_MAX_CONTRIBUTORS = 5


def _rows_in_baseline(agg: pd.DataFrame, baseline: BaselineStats | None) -> pd.DataFrame:
    """篩出 `data_status == 'ok'` 且落在基準期日期範圍內的列。"""
    ok = agg
    if 'data_status' in ok.columns:
        ok = ok[ok['data_status'] == 'ok']
    if baseline is not None and 'ts_hour' in ok.columns and not ok.empty:
        d = pd.to_datetime(ok['ts_hour']).dt.date
        ok = ok[(d >= baseline.start_date) & (d <= baseline.end_date)]
    return ok


def fit_deviation_model(agg: pd.DataFrame,
                         features: list[str],
                         baseline: BaselineStats) -> dict[str, Any]:
    """
    以基準期資料擬合多變量偏離模型（mean vector + covariance 偽逆）。

    只用基準期內、`data_status == 'ok'` 的列；特徵有 NaN 的列整列跳過
    （而非用 0 或平均值填補去湊樣本數——湊出來的協方差會低估真實變異，
    讓之後的距離系統性偏小，等於把偵測門檻悄悄調鬆）。

    Returns:
        可序列化（純 list/float/int/str，無 numpy 型別）的 dict：
        ``{'features', 'mean', 'cov_inv', 'n_samples', 'n_excluded_nan',
        'baseline_start', 'baseline_end'}``。
        後續 `evaluate_deviation` 只需要這個 dict，不需要重新讀基準期資料，
        方便存進 DB / cache。

    Raises:
        ValueError: 缺少必要欄位，或基準期內無 NaN 的可用樣本數 < 2
                    （少於 2 筆無法估計協方差）。
    """
    if agg is None or agg.empty:
        raise ValueError("fit_deviation_model: agg 為空，無法擬合")
    if not features:
        raise ValueError("fit_deviation_model: features 不可為空")
    missing_cols = [f for f in features if f not in agg.columns]
    if missing_cols:
        raise ValueError(f"fit_deviation_model: agg 缺少特徵欄位：{missing_cols}")

    ok = _rows_in_baseline(agg, baseline)
    if ok.empty:
        raise ValueError("fit_deviation_model: 基準期內找不到 data_status == 'ok' 的列")

    raw = ok[features].apply(pd.to_numeric, errors='coerce').to_numpy(dtype=float)
    valid_mask = ~np.isnan(raw).any(axis=1)
    X = raw[valid_mask]
    n_excluded = int((~valid_mask).sum())

    if len(X) < 2:
        raise ValueError(
            f"fit_deviation_model: 基準期無缺值樣本僅 {len(X)} 筆"
            f"（另有 {n_excluded} 筆因特徵含 NaN 被排除），至少需要 2 筆才能估計協方差"
        )

    mean = np.nanmean(X, axis=0)
    cov = np.cov(X, rowvar=False)
    if cov.ndim == 0:
        # 只有一個特徵時 np.cov 回傳純量，統一包成 1x1 矩陣以利後續矩陣運算
        cov = np.array([[float(cov)]])
    cov_inv = np.linalg.pinv(cov)  # 偽逆防奇異／病態協方差（沿用舊版 health_model.py 做法）

    model: dict[str, Any] = {
        'features': list(features),
        'mean': mean.tolist(),
        'cov_inv': cov_inv.tolist(),
        'n_samples': int(len(X)),
        'n_excluded_nan': n_excluded,
    }
    if baseline is not None:
        model['baseline_start'] = baseline.start_date.isoformat()
        model['baseline_end'] = baseline.end_date.isoformat()

    logger.info(
        f"fit_deviation_model: features={features} n_samples={len(X)} "
        f"n_excluded_nan={n_excluded}"
    )
    return model


def _latest_valid_row(agg: pd.DataFrame, features: list[str]) -> pd.Series | None:
    """
    從聚合資料中取「最新一筆、特徵皆無缺值、data_status == 'ok'」的列。

    由新到舊尋找，找到第一筆合格的即回傳；全部都不合格則回傳 None，
    由呼叫端決定如何處理（不崩潰，回報「資料不足」）。
    """
    d = agg
    if 'data_status' in d.columns:
        d = d[d['data_status'] == 'ok']
    if d.empty:
        return None
    if 'ts_hour' in d.columns:
        d = d.sort_values('ts_hour')

    for _, row in d.iloc[::-1].iterrows():
        vals = pd.to_numeric(row[features], errors='coerce')
        if not vals.isna().any():
            return row
    return None


def _extract_feature_values(row_or_agg: pd.Series | pd.DataFrame | dict,
                             features: list[str]) -> dict[str, float | None] | None:
    """
    統一把 `row_or_agg`（單列 Series/dict，或整份聚合 DataFrame）轉成
    `{feature: value}`。

    傳入整份 DataFrame 時，取最新一筆特徵齊全的列；若找不到（例如最近
    都是缺值或都非 ok），回傳 None，交由呼叫端輸出「資料不足」而非崩潰。
    傳入單列時，直接取值——即使含 NaN 也一併帶出，讓上層決定如何呈現。
    """
    if isinstance(row_or_agg, pd.DataFrame):
        row = _latest_valid_row(row_or_agg, features)
        if row is None:
            return None
        return {f: float(row[f]) for f in features}

    if isinstance(row_or_agg, pd.Series):
        return {f: row_or_agg.get(f) for f in features}

    if isinstance(row_or_agg, dict):
        return {f: row_or_agg.get(f) for f in features}

    raise TypeError(
        f"evaluate_deviation: row_or_agg 型別不支援：{type(row_or_agg)!r}"
        "（需為 pd.DataFrame / pd.Series / dict）"
    )


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _compute_per_feature_sigma(values: dict[str, float | None],
                                baseline: BaselineStats | None) -> dict[str, float]:
    """
    各特徵相對基準的標準化偏離量。刻意不用 Mahalanobis 模型內的
    mean/cov 反推，而是直接用 `BaselineStats.stats[f].sigma_of()`——
    後者以中位數/標準差為準，對離群值更穩健，且與系統其他地方
    （趨勢、規則）引用的基準統計是同一份，數字對得起來，agent 引用時
    不會出現「同一個特徵、不同地方算出不同 σ」的矛盾。
    """
    out: dict[str, float] = {}
    if baseline is None:
        return out
    for f, v in values.items():
        if _is_missing(v):
            continue
        stat = baseline.stats.get(f)
        if stat is None:
            continue
        out[f] = round(stat.sigma_of(float(v)), 3)
    return out


def _top_contributors(per_feature_sigma: dict[str, float]) -> list[str]:
    ranked = sorted(per_feature_sigma.items(), key=lambda kv: -abs(kv[1]))
    return [f for f, s in ranked if abs(s) >= _CONTRIBUTOR_MIN_SIGMA][:_MAX_CONTRIBUTORS]


def evaluate_deviation(row_or_agg: pd.Series | pd.DataFrame | dict,
                        model: dict[str, Any],
                        baseline: BaselineStats | None,
                        threshold_sigma: float = 3.0) -> DeviationResult:
    """
    以 `fit_deviation_model` 產出的模型，評估單一時間點是否偏離基準。

    Args:
        row_or_agg: 單列（Series/dict，需含 `model['features']` 各欄位），
                    或整份每小時聚合 DataFrame（自動取最新一筆合格列）。
        model: `fit_deviation_model` 的回傳值。
        baseline: 基準期統計，用於算 `per_feature_sigma`；為 None 時該欄
                  回傳空字典（仍會算 distance，只是無法拆解到各特徵）。
        threshold_sigma: Mahalanobis 距離門檻。命名沿用
                  `rule_config` 中 `STEP_CHANGE` 規則既有的參數鍵
                  `mahalanobis_sigma`——**這是直接對距離值設的門檻，
                  不是嚴謹統計意義上的常態分布 σ 分位數**（k 維距離平方
                  服從卡方分布，並非單變量常態），採此命名只為與既有
                  規則設定檔的語意一致，避免同一個概念出現兩種說法。

    Returns:
        DeviationResult。特徵缺值（單列含 NaN，或整份 DataFrame 找不到
        任何一列特徵齊全）時**不會崩潰**——回傳 `is_deviated=False`，
        `per_feature_sigma` 只含實際可算的特徵，並在 `note` 說明原因。
    """
    features: list[str] = model['features']

    values = _extract_feature_values(row_or_agg, features)
    if values is None:
        return DeviationResult(
            is_deviated=False,
            distance=0.0,
            threshold=threshold_sigma,
            per_feature_sigma={},
            top_contributors=[],
            note='找不到特徵齊全且 data_status == ok 的資料列，略過偏離判定',
            computable=False,   # 未評估，不是「貼合基準」
        )

    missing_features = [f for f, v in values.items() if _is_missing(v)]
    per_feature_sigma = _compute_per_feature_sigma(values, baseline)

    if missing_features:
        return DeviationResult(
            is_deviated=False,
            distance=0.0,
            threshold=threshold_sigma,
            per_feature_sigma=per_feature_sigma,
            top_contributors=_top_contributors(per_feature_sigma),
            note=f"特徵缺值（{', '.join(missing_features)}），"
                 "無法計算 Mahalanobis 距離，僅提供可用特徵的 σ",
            computable=False,   # 距離未算出，僅有部分特徵 σ
        )

    x = np.array([float(values[f]) for f in features], dtype=float)
    mean = np.array(model['mean'], dtype=float)
    cov_inv = np.array(model['cov_inv'], dtype=float)

    try:
        diff = x - mean
        d2 = float(diff @ cov_inv @ diff)
        distance = float(np.sqrt(d2)) if d2 > 0 else 0.0
        if not np.isfinite(distance):
            raise ValueError(f"distance 非有限值：{distance}")
    except Exception as e:
        logger.warning(f"evaluate_deviation: 距離計算失敗（{e}），視為未偏離")
        return DeviationResult(
            is_deviated=False,
            distance=0.0,
            threshold=threshold_sigma,
            per_feature_sigma=per_feature_sigma,
            top_contributors=_top_contributors(per_feature_sigma),
            note='Mahalanobis 距離計算失敗，略過偏離判定',
            computable=False,
        )

    is_deviated = distance > threshold_sigma
    top_contributors = _top_contributors(per_feature_sigma)

    note = ''
    if is_deviated and not top_contributors:
        # 有整體偏離但缺 baseline 或算不出各特徵 σ，仍要讓 agent 知道發生了什麼
        note = 'Mahalanobis 距離超過門檻，但缺乏基準統計，無法拆解至各特徵'

    return DeviationResult(
        is_deviated=is_deviated,
        distance=round(distance, 4),
        threshold=threshold_sigma,
        per_feature_sigma=per_feature_sigma,
        top_contributors=top_contributors,
        note=note,
    )
