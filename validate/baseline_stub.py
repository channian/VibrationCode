"""
baseline_stub.py — 基準期計算的可替換 stub

**現況（撰寫本檔當下）**：`vibcore.pipeline`／`vibcore.metrics` 尚未提供基準期
計算模組（`vibcore/metrics/iso.py`、`vibcore/metrics/deviation.py` 都已假設
呼叫端「已經有一份 `BaselineStats`」，但沒有任何模組負責「怎麼算出這份
`BaselineStats`」）。舊版 `src/baseline_detector.py` 有一套三層篩選的自動
偵測邏輯（14 天滾動窗口找最穩定區段），但屬於 §九「汰換」範圍，不能直接
沿用其實作細節，也不應該讓回測框架去依賴 legacy `src/`。

**這裡的作法**：先用最簡單、可解釋的規則頂上——取資料最前面、`ok` 樣本足夠
的一段時間窗口，直接算中位數/平均/標準差。這足以讓回測框架跑通、看出
「規則在這份資料上大概會觸發多少次」的量級，但**不是**正式選基準期的
方法（沒有做穩定度篩選，若資料一開始就在異常狀態，基準會被污染）。

**真實模組完成後如何接上**：在下面 `_import_real_baseline_fn()` 補上正確
的模組路徑即可，函式簽章需相容於
`Callable[[pd.DataFrame, int, int], BaselineStats | None]`
（引數依序為：單一量測點的每小時聚合、最短基準天數、最少可用點數）。
找不到真實模組時會自動退回本檔的 stub，不需要改呼叫端任何程式碼。
"""

from __future__ import annotations

import logging
from typing import Callable

import numpy as np
import pandas as pd

from vibcore.config import AGG_SPEC, DEFAULT_TREND, DataStatus
from vibcore.types import BaselineStats, MetricStats

logger = logging.getLogger(__name__)

BaselineFunc = Callable[[pd.DataFrame, int, int], "BaselineStats | None"]
AxisBaselineFunc = Callable[[pd.DataFrame, int], "dict | None"]

#: 三軸能量佔比欄位在 aggregate_hourly 輸出中的欄名（見 aggregate.py `_axis_energy_sorted`）
_AXIS_KEYS = ('major', 'mid', 'minor')


def _stub_compute_baseline(agg: pd.DataFrame,
                            min_days: int = DEFAULT_TREND.min_days,
                            min_points: int = DEFAULT_TREND.min_points) -> BaselineStats | None:
    """
    Stub：取資料最前面 `min_days` 天內的 `ok` 列，逐指標算中位數/平均/標準差。

    資料不足 `min_points` 筆 `ok` 樣本時回傳 None——沒有可靠基準比亂給一個
    基準更安全，呼叫端（規則）必須自行處理 `baseline is None` 的狀況。
    """
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
        point_id=-1,  # 由呼叫端（validate/points.py）填回實際 point_id
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
    嘗試接上真實的基準期模組；失敗則退回本檔 stub。

    回傳的第三個值 `is_real` 供 `validate/report.py` 在報告中註記
    「本次回測的基準期是 stub 算出來的，門檻建議需保守看待」，避免使用者
    誤以為回測用的已經是正式基準期演算法。
    """
    try:
        # 依 vibcore/pipeline/aggregate.py 的命名慣例推測；等真正模組落地
        # 後如路徑不同，改這裡即可，呼叫端完全不用動。
        from vibcore.pipeline.baseline import compute_baseline as real_fn  # type: ignore
        logger.info("已接上 vibcore.pipeline.baseline.compute_baseline（真實基準期模組）")
        try:
            from vibcore.pipeline.baseline import compute_axis_energy_baseline as real_axis_fn  # type: ignore
        except ImportError:
            real_axis_fn = _stub_axis_energy_baseline
        return real_fn, real_axis_fn, True
    except ImportError:
        return _stub_compute_baseline, _stub_axis_energy_baseline, False


compute_baseline, compute_axis_energy_baseline, USING_REAL_BASELINE = _import_real_baseline_fn()
