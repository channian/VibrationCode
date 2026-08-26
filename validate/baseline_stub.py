"""
baseline_stub.py — 基準期計算的可替換 stub

**現況**：`vibcore.metrics.baseline` 已提供 `detect_baseline()`（滾動窗口
掃描 + 穩定度評分 + 窗口完整度把關），本檔優先接上這個真實模組。找不到時
（例如在還沒有這個模組的環境／分支跑本框架）才退回下面的簡化版
`_stub_compute_baseline`——直接取資料最前段、`ok` 樣本足夠的一段時間窗口
算中位數/平均/標準差，**不做穩定度篩選**，若資料一開始就在異常狀態，
基準會被污染，僅供跑通框架、不能拿來定門檻。

三軸能量佔比基準（`AXIS_SHIFT` / `ORIENTATION_CHANGE` 兩條規則要用）目前
沒有對應的 vibcore 模組（`detect_baseline` 只處理 `AGG_SPEC` 裡的純量指標，
`axis_energy_sorted` 是另一個獨立欄位），因此一律使用本檔的
`_stub_axis_energy_baseline`；若之後 `measure_point.axis_energy_baseline`
的離線等價模組出現，在 `_import_real_baseline_fn()` 比照下面的方式接上即可。
"""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd

from vibcore.config import AGG_SPEC, DEFAULT_TREND, DataStatus
from vibcore.types import BaselineStats, MetricStats

logger = logging.getLogger(__name__)

BaselineFunc = Callable[[pd.DataFrame, "int | str"], "BaselineStats | None"]
AxisBaselineFunc = Callable[[pd.DataFrame, int], "dict | None"]

#: 三軸能量佔比欄位在 aggregate_hourly 輸出中的欄名（見 aggregate.py `_axis_energy_sorted`）
_AXIS_KEYS = ('major', 'mid', 'minor')


def _stub_compute_baseline(agg: pd.DataFrame, point_id: int | str = '') -> BaselineStats | None:
    """
    Stub：取資料最前面 `DEFAULT_TREND.min_days` 天內的 `ok` 列，逐指標算
    中位數/平均/標準差。只在 `vibcore.metrics.baseline` 無法匯入時使用。

    資料不足 `min_points` 筆 `ok` 樣本時回傳 None——沒有可靠基準比亂給一個
    基準更安全，呼叫端（規則）必須自行處理 `baseline is None` 的狀況。
    """
    min_days, min_points = DEFAULT_TREND.min_days, DEFAULT_TREND.min_points
    if agg is None or agg.empty or 'data_status' not in agg.columns:
        return None

    ok = agg[agg['data_status'] == DataStatus.OK].sort_values('ts_hour')
    if ok.empty:
        return None

    window_end = ok['ts_hour'].iloc[0] + pd.Timedelta(days=min_days)
    window = ok[ok['ts_hour'] < window_end]
    if len(window) < min_points:
        # 前段資料不夠，退而求其次：用能拿到的前 min_points 筆 ok 樣本
        window = ok.iloc[:min_points]
    if len(window) < min_points:
        logger.debug(f"可用 ok 樣本僅 {len(window)} 筆（需要 {min_points}），無法建立基準期")
        return None

    stats: dict[str, MetricStats] = {}
    for metric in AGG_SPEC:
        if metric not in window.columns:
            continue
        vals = pd.to_numeric(window[metric], errors='coerce').dropna()
        if vals.empty:
            continue
        stats[metric] = MetricStats(
            median=float(vals.median()),
            mean=float(vals.mean()),
            std=float(vals.std(ddof=1)) if len(vals) > 1 else 0.0,
            n=int(len(vals)),
        )

    if not stats:
        return None

    return BaselineStats(
        point_id=point_id,
        start_date=window['ts_hour'].iloc[0].date(),
        end_date=window['ts_hour'].iloc[-1].date(),
        source='auto',
        stats=stats,
        n_hours=int(len(window)),
        note='validate/baseline_stub.py 產生的簡化基準期，僅供回測，非正式基準',
    )


def _stub_axis_energy_baseline(agg: pd.DataFrame,
                                min_days: int = DEFAULT_TREND.min_days) -> dict | None:
    """
    Stub：三軸能量佔比基準（`measure_point.axis_energy_baseline` 的離線版）。

    取同一段基準窗口內 `axis_energy_sorted` 的中位數，供 `AXIS_SHIFT` /
    `ORIENTATION_CHANGE` 兩條規則比對；`aggregate_hourly` 已把三軸能量算成
    排序後的 major/mid/minor（與座標方向無關，見 aggregate.py）。
    """
    if agg is None or agg.empty or 'data_status' not in agg.columns:
        return None
    ok = agg[agg['data_status'] == DataStatus.OK].sort_values('ts_hour')
    if ok.empty or 'axis_energy_sorted' not in ok.columns:
        return None

    window_end = ok['ts_hour'].iloc[0] + pd.Timedelta(days=min_days)
    window = ok[ok['ts_hour'] < window_end]
    rows = [r for r in window['axis_energy_sorted'] if isinstance(r, dict)]
    if len(rows) < 3:
        return None

    return {k: float(np.median([r[k] for r in rows if k in r])) for k in _AXIS_KEYS}


def _import_real_baseline_fn() -> tuple[BaselineFunc, AxisBaselineFunc, bool]:
    """
    接上真實的基準期模組 `vibcore.metrics.baseline.detect_baseline`；
    找不到時（例如尚未合併到目前分支）退回本檔 stub。

    `detect_baseline` 的簽章是 `(agg, cfg, point_id)`，本檔對外統一暴露
    `(agg, point_id)` 兩個參數的介面（`cfg` 用預設值即可，回測不需要調整
    滾動窗口掃描的細節參數），讓 `validate/backtest.py` 不必關心底下究竟
    是真實模組還是 stub、也不必因為兩者參數個數不同而寫兩套呼叫邏輯。
    """
    try:
        from vibcore.metrics.baseline import DEFAULT_BASELINE_CFG, detect_baseline

        def _adapted(agg: pd.DataFrame, point_id: int | str = '') -> BaselineStats | None:
            return detect_baseline(agg, DEFAULT_BASELINE_CFG, point_id=point_id)

        logger.info("已接上 vibcore.metrics.baseline.detect_baseline（真實基準期模組）")
        return _adapted, _stub_axis_energy_baseline, True
    except ImportError:
        logger.warning("vibcore.metrics.baseline 無法匯入，基準期使用內建簡化版（僅供跑通）")
        return _stub_compute_baseline, _stub_axis_energy_baseline, False


compute_baseline, compute_axis_energy_baseline, USING_REAL_BASELINE = _import_real_baseline_fn()
