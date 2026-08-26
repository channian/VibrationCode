"""
trend.py — 趨勢分析（線性回歸求劣化速率）

**必須以實際時間（天）為 x 軸，不是列索引。** 每小時聚合資料含缺口列
（`no_data`/`partial`/`not_running`），計算趨勢時這些列會先被濾掉，只留
`data_status == 'ok'` 的列參與回歸。若這時用「濾掉之後剩幾筆」的列索引
當 x 軸，缺口會被悄悄壓縮成 0——兩筆相隔 30 天的 ok 資料，索引上卻只差
1，回歸出來的斜率會被放大到失真（見計畫書 §三）。改用實際經過的天數，
缺口再長也只是 x 軸上的一段空白，不影響前後兩段資料真實的時間間距。

同樣的理由，本模組只使用 `data_status == 'ok'` 的列：`partial` 的數字
不可信、`not_running` 是正常停機、`no_data` 是斷線，三者都不代表設備真實
運轉時的振動水準，混入回歸只會製造假趨勢或蓋掉真趨勢。

`TrendResult.confidence` 是這個模組存在的核心理由之一：斜率算得出來不
代表能拿去下結論。樣本太少、觀察期太短、或資料本身線性度不足（R² 低）
時，斜率可能只是雜訊的產物，必須讓下游（規則層、agent）看得出信心度，
而不是只給一個數字。
"""

import logging

import numpy as np
import pandas as pd

from vibcore.config import DataStatus, DEFAULT_TREND, TrendConfig
from vibcore.types import BaselineStats, TrendResult

logger = logging.getLogger(__name__)

#: |slope_pct_per_month| 低於此值視為持平（flat）。
#: 基準期統計本身就有自然波動，斜率若換算成每月變化不到基準中位數的 2%，
#: 判成「上升」或「下降」容易只是在追噪音；設一個穩定帶，避免趨勢方向
#: 在每次重算時因極小的數值抖動而翻來覆去。
FLAT_BAND_PCT_PER_MONTH = 2.0

#: 一個月的天數換算基準，與 `TrendResult.slope_per_month` 的定義一致
_DAYS_PER_MONTH = 30.0


def _empty_trend(metric: str, n_points: int, note: str) -> TrendResult:
    """樣本不足或找不到欄位時的佔位結果；數值一律用 NaN，不用 0 冒充「沒有變化」。"""
    return TrendResult(
        metric=metric,
        slope_per_day=float('nan'),
        slope_per_month=float('nan'),
        slope_pct_per_month=float('nan'),
        intercept=float('nan'),
        r2=float('nan'),
        n_points=n_points,
        span_days=0.0,
        direction='unknown',
        confidence='low',
        note=note,
    )


def _linear_regression(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """
    最小平方回歸，回傳 (slope, intercept, r2)。

    y 完全不變（ss_tot == 0）時 R² 定義上是 0/0：若殘差也剛好是 0（資料
    真的一條值到底），視為完美擬合給 1.0；否則保守給 0.0，避免除以零
    炸掉或給出誤導性的高信心。
    """
    slope, intercept = np.polyfit(x, y, 1)
    y_hat = slope * x + intercept
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0:
        r2 = 1.0 if ss_res == 0 else 0.0
    else:
        r2 = 1.0 - ss_res / ss_tot
    return float(slope), float(intercept), r2


def compute_trend(agg: pd.DataFrame,
                   metric: str,
                   baseline: BaselineStats | None,
                   cfg: TrendConfig = DEFAULT_TREND) -> TrendResult:
    """
    對單一指標做線性回歸，估計劣化（或改善）速率。

    Args:
        agg: 每小時聚合結果（含 `ts_hour` / `data_status` 與 `metric` 欄）。
        metric: 指標名稱（`vibcore.config.AGG_SPEC` 的鍵名，例如 `'acc_kurt'`）。
        baseline: 基準期統計，用於把斜率換算成「相對基準中位數的百分比
                  變化/月」。為 `None`，或基準期沒有這個指標的統計量，或
                  中位數為 0（無法作分母）時，`slope_pct_per_month` 設為
                  NaN 並在 `note` 中說明——此時斜率本身（`slope_per_day`
                  等絕對值）仍照常計算，只是無法換算成相對百分比。
        cfg: 趨勢分析參數（最短觀察天數、最少樣本數、R² 門檻）。

    Returns:
        TrendResult。找不到欄位、資料為空、或可用樣本少於 2 筆時，
        斜率/截距/R² 一律為 NaN、`direction='unknown'`、`confidence='low'`，
        並在 `note` 中說明原因。
    """
    if agg is None or agg.empty or 'ts_hour' not in agg.columns or metric not in agg.columns:
        note = f'找不到指標 {metric} 的欄位或聚合資料為空，無法計算趨勢'
        logger.warning(f"compute_trend({metric})：{note}")
        return _empty_trend(metric, 0, note)

    ok = agg[agg['data_status'] == DataStatus.OK] if 'data_status' in agg.columns else agg.iloc[0:0]
    sub = ok[['ts_hour', metric]].copy()
    sub[metric] = pd.to_numeric(sub[metric], errors='coerce')
    sub = sub.dropna(subset=[metric]).sort_values('ts_hour')
    n_points = len(sub)

    if n_points < 2:
        note = f'可用樣本僅 {n_points} 筆（data_status == ok 且 {metric} 非空），不足以建立回歸線'
        logger.warning(f"compute_trend({metric})：{note}")
        return _empty_trend(metric, n_points, note)

    # ── 以實際經過天數為 x 軸，見模組說明 ──────────────────────
    t0 = sub['ts_hour'].iloc[0]
    x = ((sub['ts_hour'] - t0) / pd.Timedelta(days=1)).to_numpy(dtype=float)
    y = sub[metric].to_numpy(dtype=float)
    span_days = float(x.max() - x.min())

    slope_per_day, intercept, r2 = _linear_regression(x, y)
    slope_per_month = slope_per_day * _DAYS_PER_MONTH

    baseline_metric = baseline.stats.get(metric) if baseline is not None else None
    baseline_missing_reason = ''
    if baseline is None:
        baseline_missing_reason = '無基準期資料，無法換算相對變化百分比'
    elif baseline_metric is None:
        baseline_missing_reason = f'基準期缺少 {metric} 的統計量，無法換算相對變化百分比'
    elif baseline_metric.median == 0 or pd.isna(baseline_metric.median):
        baseline_missing_reason = '基準期中位數為 0，無法作為換算百分比的分母'

    if baseline_missing_reason:
        slope_pct_per_month = float('nan')
    else:
        slope_pct_per_month = slope_per_month / baseline_metric.median * 100.0

    # ── 信心度判定 ──────────────────────────────────────────────
    # 題面明訂的兩條規則（樣本數、R²）之外，額外把「觀察期是否達
    # cfg.min_days」也算進低信心的觸發條件：min_points 是用小時數換算的
    # 樣本數門檻，資料若剛好密集（例如連續一整天都是 ok），可能不到
    # min_days 就先湊滿 min_points 筆，此時 R² 再高也只是短窗內的偶然
    # 線性，不足以支撐「劣化速率」這種需要跨週/月驗證的結論——這正是
    # 「觀察期太短」被列為低信心原因之一的理由，而不只是樣本數的重複敘述。
    insufficient_points = n_points < cfg.min_points
    low_r2 = r2 < cfg.min_r2
    short_span = span_days < cfg.min_days

    # 期間涵蓋率：n_points 與 span_days 各自達標仍可能是「稀疏點橫跨長期」。
    # 例：90 天內只剩 24 個 ok 小時（涵蓋率 1.1%），點數與期間都過關、
    # 這些點又剛好共線 → R²≈1 被判 high。感測器長期斷線時正是這個形狀，
    # 而斷線是本專案已知的常見狀況，因此必須擋。
    expected_points = max(span_days * 24.0, 1.0)      # 每小時一筆
    coverage = n_points / expected_points
    low_coverage = coverage < cfg.min_completeness

    reasons = []
    if insufficient_points:
        reasons.append(f'樣本數不足：僅 {n_points} 筆，低於門檻 {cfg.min_points} 筆')
    if short_span:
        reasons.append(f'觀察期太短：僅 {span_days:.1f} 天，低於建議下限 {cfg.min_days} 天')
    if low_coverage:
        reasons.append(f'期間涵蓋率不足：{coverage:.1%}（{n_points} 筆 / 預期 '
                       f'{expected_points:.0f} 筆），低於門檻 {cfg.min_completeness:.0%}，'
                       f'資料過於稀疏（可能為感測器斷線）')
    if low_r2:
        reasons.append(f'R² 偏低：{r2:.2f}，低於門檻 {cfg.min_r2}，趨勢線性度不足')

    if insufficient_points or low_r2 or short_span or low_coverage:
        confidence = 'low'
    elif n_points >= cfg.min_points and r2 >= 0.7:
        confidence = 'high'
    else:
        confidence = 'medium'

    note_parts = []
    if confidence == 'low':
        note_parts.append('信心度低（' + '；'.join(reasons) + '）')
    if baseline_missing_reason:
        note_parts.append(baseline_missing_reason)
    note = '；'.join(note_parts)

    # ── 方向判定：依 slope_pct_per_month 與穩定帶 ─────────────────
    if pd.isna(slope_pct_per_month):
        direction = 'unknown'
    elif abs(slope_pct_per_month) < FLAT_BAND_PCT_PER_MONTH:
        direction = 'flat'
    elif slope_pct_per_month > 0:
        direction = 'up'
    else:
        direction = 'down'

    return TrendResult(
        metric=metric,
        slope_per_day=slope_per_day,
        slope_per_month=slope_per_month,
        slope_pct_per_month=slope_pct_per_month,
        intercept=intercept,
        r2=r2,
        n_points=n_points,
        span_days=span_days,
        direction=direction,
        confidence=confidence,
        note=note,
    )


def compute_trends(agg: pd.DataFrame,
                    metrics: list[str],
                    baseline: BaselineStats | None,
                    cfg: TrendConfig = DEFAULT_TREND) -> dict[str, TrendResult]:
    """對多個指標分別計算趨勢；單一指標失敗不影響其他指標。"""
    return {metric: compute_trend(agg, metric, baseline, cfg) for metric in metrics}
