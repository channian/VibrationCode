"""
aggregate.py — 每秒 Analytic 資料 → 每小時聚合（Tier 1）

核心職責有三：

1. **明確區分四種資料狀態**
   斷線（no_data）與未運轉（not_running）是完全不同的事：前者是設備異常
   需要告警，後者是正常狀態不該判異常。混為一談會同時毀掉趨勢圖與規則判定。
   資料不全（partial）則是第三種——有數字但不可信，不應進入趨勢回歸。

2. **依欄位語意選擇聚合方式**
   RMS/OA 類取平均代表水準；PEAK/CREST/KURT 類取最大值——衝擊事件若被
   平均掉，這些指標存在的意義就沒了。

3. **只用運轉中的樣本計算指標**
   停機時的振動是噪音。備機一天可能只跑兩小時，把停機時段平均進來會讓
   數字完全失真。
"""

import logging
from dataclasses import replace
import numpy as np
import pandas as pd

from vibcore.config import (
    AGG_SPEC, AGG_MEAN, AGG_MAX, AGG_AT_MAX, AT_MAX_REFERENCE,
    AXIS_ENERGY_COLS, AggregateConfig, DEFAULT_AGG, DataStatus,
)

logger = logging.getLogger(__name__)

_CURRENT_KEYWORDS = ('電流', 'current', '安培', 'amp')


def _find_current_col(df: pd.DataFrame) -> str | None:
    """找出電流欄位；排除振動特徵欄位，避免 accCREST 之類被誤判。"""
    vib_prefixes = ('acc', 'vel', 'disp')
    for c in df.columns:
        cl = c.lower()
        if any(cl.startswith(p) for p in vib_prefixes):
            continue
        if any(kw.lower() in cl for kw in _CURRENT_KEYWORDS):
            if df[c].notna().sum() > 0:
                return c
    return None


def mark_running(df: pd.DataFrame, cfg: AggregateConfig = DEFAULT_AGG) -> pd.Series:
    """
    判定每一筆是否為運轉狀態。

    有電流資料時以電流為準（最直接）；否則以 velRMS 門檻判斷。
    """
    current_col = _find_current_col(df)
    if current_col is not None:
        mask = df[current_col] > cfg.current_running_threshold
        logger.debug(f"運轉判定：電流欄 {current_col!r} > {cfg.current_running_threshold}")
        return mask.fillna(False)

    if 'velRMS' in df.columns:
        return (df['velRMS'] > cfg.vel_running_threshold).fillna(False)

    logger.warning("無電流亦無 velRMS 欄位，無法判定運轉狀態，全部視為運轉中")
    return pd.Series(True, index=df.index)


def _axis_energy_sorted(sub: pd.DataFrame) -> dict | None:
    """
    三軸能量佔比，**排序後**輸出（major / mid / minor）。

    刻意不保留 x/y/z 標籤：感測器可能貼錯方向，排序後的分佈與座標無關，
    直接迴避這個問題（見計畫書 §五）。
    """
    cols = [c for c in AXIS_ENERGY_COLS if c in sub.columns]
    if len(cols) < 3:
        return None
    means = sub[cols].mean()
    if means.isna().any():
        return None

    energy = np.square(means.values.astype(float))
    total = energy.sum()
    if total <= 0:
        return None

    ratios = np.sort(energy / total)[::-1]
    return {
        'major': round(float(ratios[0]), 4),
        'mid':   round(float(ratios[1]), 4),
        'minor': round(float(ratios[2]), 4),
    }


def _aggregate_running(sub_run: pd.DataFrame) -> dict:
    """對一小時內的運轉樣本，依欄位語意聚合。"""
    out: dict = {}
    ref = sub_run[AT_MAX_REFERENCE] if AT_MAX_REFERENCE in sub_run.columns else None
    idx_at_max = ref.idxmax() if ref is not None and ref.notna().any() else None

    for target, (source, how) in AGG_SPEC.items():
        if source not in sub_run.columns:
            out[target] = None
            continue
        col = pd.to_numeric(sub_run[source], errors='coerce').dropna()
        if col.empty:
            out[target] = None
            continue

        if how == AGG_MEAN:
            out[target] = float(col.mean())
        elif how == AGG_MAX:
            out[target] = float(col.max())
        elif how == AGG_AT_MAX:
            if idx_at_max is not None and idx_at_max in sub_run.index:
                val = pd.to_numeric(pd.Series([sub_run.at[idx_at_max, source]]),
                                    errors='coerce').iloc[0]
                out[target] = float(val) if pd.notna(val) else None
            else:
                out[target] = float(col.median())
        else:
            raise ValueError(f"未知的聚合方式：{how}")

    out['axis_energy_sorted'] = _axis_energy_sorted(sub_run)
    return out


def _empty_metrics() -> dict:
    """無可用指標時的空值列；一律用 NaN 以免下游出現 None/NaN 混用。"""
    out = {t: np.nan for t in AGG_SPEC}
    out['axis_energy_sorted'] = None
    return out


def detect_samples_per_hour(df: pd.DataFrame,
                            cfg: AggregateConfig = DEFAULT_AGG) -> int | None:
    """
    從資料本身推測「每小時應有幾筆」。

    預設值 3600（每秒一筆）對應正式環境的前端輸出，但實際資料未必是這個
    密度——歷史匯出可能降採樣過，不同設備的回報頻率也可能不同。

    密度猜錯的後果不是報錯，而是**每一小時都被判為 partial、所有指標型
    規則靜默跳過、當天完全不產生告警**。系統看起來還在跑、報告也照常
    產出，但實際上什麼都沒判定。這種錯誤比直接崩潰危險得多。

    取「資料最密集的那一小時」的筆數，避免被頭尾不完整的小時拉低。
    已接近預設值時回傳 None，表示沿用預設即可。

    Returns:
        偵測到的每小時筆數；無法判斷或已接近預設值時回傳 None
    """
    if df is None or df.empty or 'datetime' not in df.columns:
        return None

    per_hour = df.groupby(df['datetime'].dt.floor('h')).size()
    if per_hour.empty:
        return None

    detected = int(per_hour.max())
    expected = cfg.expected_samples_per_hour
    if detected >= expected * 0.8:
        return None

    logger.warning(
        f"資料密度約每小時 {detected} 筆，與預設的 {expected} 筆不符。"
        f"若沿用預設，每一小時都會被判為 partial 而使所有指標型規則靜默跳過"
    )
    return detected


def aggregate_hourly(df: pd.DataFrame,
                     cfg: AggregateConfig = DEFAULT_AGG,
                     fill_gaps: bool = True,
                     auto_detect_density: bool = True) -> pd.DataFrame:
    """
    將每秒的 Analytic 資料聚合為每小時一筆。

    Args:
        df: 含 `datetime` 欄的原始資料（每秒一筆）
        cfg: 聚合參數
        fill_gaps: 是否為觀測範圍內完全無資料的小時補上 `no_data` 列。
                   補上後趨勢圖才能正確斷線而非跨越缺口連線。
        auto_detect_density: 資料密度與 `cfg.expected_samples_per_hour`
                   明顯不符時自動改用偵測值。關掉的話密度猜錯會讓每一小時
                   都變成 partial，所有指標型規則靜默跳過（見
                   `detect_samples_per_hour` 的說明）。

    Returns:
        DataFrame，每列一小時，含 `data_status` / `completeness` /
        `n_samples_total` / `n_samples_running` 與各聚合指標。
    """
    if df.empty:
        return pd.DataFrame()

    if auto_detect_density:
        detected = detect_samples_per_hour(df, cfg)
        if detected is not None:
            cfg = replace(cfg, expected_samples_per_hour=detected)
            logger.info(f"  已改用偵測到的資料密度：每小時 {detected} 筆")

    work = df.copy()
    work['_hour'] = work['datetime'].dt.floor('h')
    work['_running'] = mark_running(work, cfg)

    rows = []
    for hour, sub in work.groupby('_hour', sort=True):
        n_total = len(sub)
        sub_run = sub[sub['_running']]
        n_run = len(sub_run)
        completeness = min(n_total / cfg.expected_samples_per_hour, 1.0)

        if n_run == 0:
            # 有資料但設備未運轉——正常狀態，不是異常
            status = DataStatus.NOT_RUNNING
            metrics = _empty_metrics()
        elif completeness < cfg.partial_threshold or n_run < cfg.min_running_samples:
            # 有數字但樣本不足，指標不具代表性
            status = DataStatus.PARTIAL
            metrics = _aggregate_running(sub_run)
        else:
            status = DataStatus.OK
            metrics = _aggregate_running(sub_run)

        rows.append({
            'ts_hour': hour,
            'data_status': status,
            'completeness': round(completeness, 4),
            'n_samples_total': n_total,
            'n_samples_running': n_run,
            **metrics,
        })

    result = pd.DataFrame(rows)

    if fill_gaps and not result.empty:
        result = _fill_gap_hours(result, cfg)

    return result.sort_values('ts_hour').reset_index(drop=True)


def _fill_gap_hours(agg: pd.DataFrame, cfg: AggregateConfig) -> pd.DataFrame:
    """
    為觀測範圍內完全無資料的小時補上 `no_data` 列。

    只補「範圍內」的缺口——首尾之外的缺失無從得知，交由 SENSOR_OFFLINE
    規則以「最後一筆距今多久」判定。
    """
    full_range = pd.date_range(agg['ts_hour'].min(), agg['ts_hour'].max(), freq='h')
    missing = full_range.difference(pd.DatetimeIndex(agg['ts_hour']))
    if len(missing) == 0:
        return agg

    gap_rows = [{
        'ts_hour': ts,
        'data_status': DataStatus.NO_DATA,
        'completeness': 0.0,
        'n_samples_total': 0,
        'n_samples_running': 0,
        **_empty_metrics(),
    } for ts in missing]

    logger.warning(f"  偵測到 {len(missing)} 個無資料小時（感測器斷線），已補為 no_data")
    return pd.concat([agg, pd.DataFrame(gap_rows)], ignore_index=True)


def summarize_gaps(agg: pd.DataFrame) -> pd.DataFrame:
    """
    把連續的缺口小時合併成區段，供報告與視覺化標註使用。

    `no_data`（斷線）與 `partial`（資料不全）**分開成段**，不合併——
    兩者的成因與處置不同，混在同一段會讓報告失準。

    Returns:
        DataFrame[gap_start, gap_end, hours, status]，依時長由長到短
    """
    empty = pd.DataFrame(columns=['gap_start', 'gap_end', 'hours', 'status'])
    if agg.empty:
        return empty

    d = agg.sort_values('ts_hour').reset_index(drop=True)
    is_gap = d['data_status'].isin(DataStatus.GAP)
    if not is_gap.any():
        return empty

    # 狀態改變或非缺口即切段：確保 no_data 與 partial 不會被併在一起
    status = d['data_status'].where(is_gap, other='__ok__')
    block = (status != status.shift()).cumsum()

    segments = []
    for _, seg in d[is_gap].groupby(block[is_gap], sort=True):
        segments.append({
            'gap_start': seg['ts_hour'].min(),
            'gap_end':   seg['ts_hour'].max() + pd.Timedelta(hours=1),
            'hours':     len(seg),
            'status':    seg['data_status'].iloc[0],
        })

    return (pd.DataFrame(segments)
            .sort_values('hours', ascending=False)
            .reset_index(drop=True))


def coverage_report(agg: pd.DataFrame) -> dict:
    """
    資料涵蓋率摘要。週報必須引用此資訊——涵蓋率不足時的結論不可信，
    agent 需據此標明信心度。
    """
    if agg.empty:
        return {'total_hours': 0}

    counts = agg['data_status'].value_counts()
    total = len(agg)
    analyzable = int(counts.get(DataStatus.OK, 0))
    return {
        'total_hours':      total,
        'ok_hours':         analyzable,
        'partial_hours':    int(counts.get(DataStatus.PARTIAL, 0)),
        'no_data_hours':    int(counts.get(DataStatus.NO_DATA, 0)),
        'not_running_hours': int(counts.get(DataStatus.NOT_RUNNING, 0)),
        'analyzable_ratio': round(analyzable / total, 4) if total else 0.0,
        'period_start':     agg['ts_hour'].min(),
        'period_end':       agg['ts_hour'].max(),
    }
