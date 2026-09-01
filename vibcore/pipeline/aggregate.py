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
import math
from dataclasses import replace
import numpy as np
import pandas as pd

from vibcore.config import (
    AGG_SPEC, AGG_MEAN, AGG_MAX, AGG_MIN, AGG_MEDIAN, AGG_AT_MAX, AT_MAX_REFERENCE,
    AXIS_ENERGY_COLS, AXIS_IMPACT_COLS, AXIS_IMPACT_MEDIAN_COLS, NOMINAL_INTERVALS_SEC,
    AggregateConfig, DEFAULT_AGG, DataStatus,
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
        # 一併帶出三軸合成的振動量值（與 accRMS 同單位）。佔比是歸一化後的
        # 結果，總能量很低時三個分量都接近雜訊，佔比會被雜訊主導而劇烈跳動
        # ——看起來就像感測器被重貼。單看佔比無從分辨，必須有這個量值才能
        # 讓下游規則判斷「這組佔比值不值得採信」。
        'energy': round(float(np.sqrt(total)), 6),
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
        elif how == AGG_MIN:
            out[target] = float(col.min())
        elif how == AGG_MEDIAN:
            out[target] = float(col.median())
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
    out.update(_axis_impact_max(sub_run))
    return out


def _axis_impact_max(sub_run: pd.DataFrame) -> dict:
    """
    逐軸衝擊型指標：先逐列取「三軸中最大」，再對該小時取 max 與 median。

    合成欄（accCREST / accKURT）是對合成訊號另外算的，不是三軸的極值——
    單一方向的衝擊在合成訊號裡會被其他兩軸稀釋。實測 ZP 3-5：三軸
    crest 為 4.65/5.01/4.30，合成欄卻是 4.08，低於任一軸。合成值本身
    沒錯（見 docs/DATA_CONTRACT.md §二），但要抓「某一方向開始出現衝擊」
    就需要逐軸的極值。

    **兩層取值的意義不同，不要混淆**：逐列的「三軸取最大」問的是「這一筆
    當下哪個方向最尖」，那是每筆資料本身的事實；小時層的 max/median 問的
    才是「這一小時要用哪個數字代表」。所以逐列一律取最大，小時層才分兩種
    ——median 供規則判定（對窗口長度不敏感），max 保留供證據呈現與回溯，
    理由見 config.py 對 `acc_kurt_median` 的說明。

    刻意只取極值、不保留是哪一軸——感測器可能貼錯方向，軸標籤不可信。
    """
    out: dict = {}
    for target, cols in AXIS_IMPACT_COLS.items():
        present = [c for c in cols if c in sub_run.columns]
        if not present:
            out[target] = None
            continue
        vals = sub_run[present].apply(pd.to_numeric, errors='coerce')
        row_max = vals.max(axis=1).dropna()
        out[target] = float(row_max.max()) if not row_max.empty else None

    for target, cols in AXIS_IMPACT_MEDIAN_COLS.items():
        present = [c for c in cols if c in sub_run.columns]
        if not present:
            out[target] = None
            continue
        vals = sub_run[present].apply(pd.to_numeric, errors='coerce')
        row_max = vals.max(axis=1).dropna()
        out[target] = float(row_max.median()) if not row_max.empty else None
    return out


def _empty_metrics() -> dict:
    """無可用指標時的空值列；一律用 NaN 以免下游出現 None/NaN 混用。"""
    out = {t: np.nan for t in AGG_SPEC}
    out.update({t: np.nan for t in AXIS_IMPACT_COLS})
    out.update({t: np.nan for t in AXIS_IMPACT_MEDIAN_COLS})
    out['axis_energy_sorted'] = None
    return out


def _snap_interval(seconds: float) -> int | None:
    """把推估出來的取樣間隔吸附到最接近的標稱值（見 NOMINAL_INTERVALS_SEC）。"""
    if seconds is None or not np.isfinite(seconds) or seconds <= 0:
        return None
    best = min(NOMINAL_INTERVALS_SEC, key=lambda c: abs(math.log(seconds / c)))
    if abs(math.log(seconds / best)) > math.log(1.5):
        return None       # 與任何標稱間隔都差太遠，不敢猜
    return best


def detect_cadence_segments(df: pd.DataFrame) -> pd.DataFrame:
    """
    以「日」為單位推估前端輸出間隔，並把間隔相同的連續日子合併成「取樣段」。

    為什麼需要分段，而不是整批推一個密度：**同一個量測點的匯出檔可能混雜
    兩種前端版本**。即時量測是每秒一筆、長期量測是每 10 分鐘一筆；設備陸續
    換版，於是一份匯出裡前段是每秒、後段是每 10 分鐘。

    舊版用「資料最密集的那一小時筆數」推密度，遇到這種混合資料會被前段的
    每秒資料帶走（max ≈ 3600，判定與預設相符而不覆寫），後段整段 10 分鐘
    資料因此套上為每秒資料訂的門檻，**全部變成 partial、所有指標型規則靜默
    跳過**。實測這會讓有資料的時段可分析比例從 100% 掉到 9%，而且報告照常
    產出、不報任何錯。

    用「日中位數間隔」而非筆數，是因為中位數不受缺漏樣本影響：一天只要
    多數相鄰樣本維持標稱間隔，中位數就是對的，即使當天缺了一半資料。

    跨版本當天（前半每秒、後半每 10 分鐘）的中位數會偏向筆數較多的那一種，
    該日的判定因此可能不準；一整段觀測期只會有一天如此，可接受。

    Returns:
        DataFrame[start_day, end_day, interval_sec, samples_per_hour, n_rows]，
        依時間排序。無法判斷時回傳空 DataFrame。
    """
    empty = pd.DataFrame(columns=['start_day', 'end_day', 'interval_sec',
                                  'samples_per_hour', 'n_rows'])
    if df is None or df.empty or 'datetime' not in df.columns:
        return empty

    ts = pd.to_datetime(df['datetime']).sort_values()
    day = ts.dt.floor('D')

    per_day = []
    for d, sub in ts.groupby(day, sort=True):
        if len(sub) < 2:
            per_day.append((d, None, len(sub)))
            continue
        deltas = sub.diff().dt.total_seconds()
        deltas = deltas[deltas > 0]
        interval = _snap_interval(float(deltas.median())) if len(deltas) else None
        per_day.append((d, interval, len(sub)))

    known = [i for _, i, _ in per_day if i is not None]
    if not known:
        return empty

    # 樣本太少而推不出間隔的日子，沿用前一個已知值（開頭則沿用後一個）。
    # 這種日子多半是斷線後的零星回報，不該自成一段。
    filled, last = [], None
    for d, i, n in per_day:
        if i is not None:
            last = i
        filled.append((d, last, n))
    first_known = next(i for _, i, _ in filled if i is not None)
    filled = [(d, i if i is not None else first_known, n) for d, i, n in filled]

    segments = []
    for d, i, n in filled:
        if segments and segments[-1]['interval_sec'] == i:
            segments[-1]['end_day'] = d
            segments[-1]['n_rows'] += n
        else:
            segments.append({'start_day': d, 'end_day': d,
                             'interval_sec': i, 'n_rows': n})

    out = pd.DataFrame(segments)
    out['samples_per_hour'] = (3600 / out['interval_sec']).round().astype(int).clip(lower=1)
    return out[['start_day', 'end_day', 'interval_sec', 'samples_per_hour', 'n_rows']]


def detect_samples_per_hour(df: pd.DataFrame,
                            cfg: AggregateConfig = DEFAULT_AGG) -> int | None:
    """
    推測整批資料的「每小時應有幾筆」（單一密度版）。

    密度猜錯的後果不是報錯，而是**每一小時都被判為 partial、所有指標型
    規則靜默跳過、當天完全不產生告警**。系統看起來還在跑、報告也照常
    產出，但實際上什麼都沒判定。這種錯誤比直接崩潰危險得多。

    實作委由 `detect_cadence_segments`，取涵蓋樣本數最多的那一段的密度。
    資料含多種取樣密度時單一數字必然是錯的，此時 `aggregate_hourly` 會
    改走逐日密度；本函式僅供只需要一個代表值的呼叫端使用。

    Returns:
        偵測到的每小時筆數；無法判斷或已接近設定值時回傳 None
    """
    segments = detect_cadence_segments(df)
    if segments.empty:
        return None

    dominant = segments.loc[segments['n_rows'].idxmax()]
    detected = int(dominant['samples_per_hour'])
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

    # 逐日密度：同一量測點的匯出檔可能混雜兩種前端版本（每秒／每 10 分鐘），
    # 此時任何單一密度值都會讓其中一段全部誤判為 partial。改為每個小時各自
    # 依所屬取樣段的密度判定，兩段就都能正常分析。
    day_sph: dict[pd.Timestamp, int] = {}
    if auto_detect_density:
        segments = detect_cadence_segments(df)
        if len(segments) > 1:
            logger.warning(
                f"  偵測到 {len(segments)} 種取樣密度，資料可能混雜不同前端版本："
                + "；".join(
                    f"{s.start_day:%Y-%m-%d}~{s.end_day:%Y-%m-%d} 每 {s.interval_sec} 秒"
                    f"（{s.samples_per_hour} 筆/小時）"
                    for s in segments.itertuples()
                )
                + "。各段將各自套用對應門檻；但**跨段的基準期不可比**，"
                  "尤其 PEAK/CREST/KURT 這類取最大值的指標會隨密度改變而系統性偏移。"
            )
        for s in segments.itertuples():
            for d in pd.date_range(s.start_day, s.end_day, freq='D'):
                day_sph[d] = int(s.samples_per_hour)
        detected = detect_samples_per_hour(df, cfg)
        if detected is not None:
            cfg = replace(cfg, expected_samples_per_hour=detected)
            logger.info(f"  資料主要密度：每小時 {detected} 筆")

    work = df.copy()
    work['_hour'] = work['datetime'].dt.floor('h')
    work['_running'] = mark_running(work, cfg)

    rows = []
    for hour, sub in work.groupby('_hour', sort=True):
        n_total = len(sub)
        sub_run = sub[sub['_running']]
        n_run = len(sub_run)

        expected = day_sph.get(hour.floor('D'), cfg.expected_samples_per_hour)
        # 運轉樣本門檻必須跟著密度走。寫死絕對筆數的話，低密度資料（每 10
        # 分鐘一筆＝每小時 6 筆）永遠達不到為每秒一筆訂的 60 筆，每個小時
        # 都會變成 partial，指標型規則全部跳過——而且不會報錯，只會安靜地
        # 什麼都判不出來。
        min_running = cfg.effective_min_running(expected)
        completeness = min(n_total / expected, 1.0)

        if n_run == 0:
            # 有資料但設備未運轉——正常狀態，不是異常
            status = DataStatus.NOT_RUNNING
            metrics = _empty_metrics()
        elif completeness < cfg.partial_threshold or n_run < min_running:
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
            # 下游（基準期、趨勢、規則）需要知道這一小時屬於哪種取樣密度，
            # 才能避免跨密度比較。
            'expected_samples': expected,
            **metrics,
        })

    result = pd.DataFrame(rows)

    if fill_gaps and not result.empty:
        result = _fill_gap_hours(result, cfg, day_sph)

    return result.sort_values('ts_hour').reset_index(drop=True)


def _fill_gap_hours(agg: pd.DataFrame, cfg: AggregateConfig,
                    day_sph: dict | None = None) -> pd.DataFrame:
    """
    為觀測範圍內完全無資料的小時補上 `no_data` 列。

    只補「範圍內」的缺口——首尾之外的缺失無從得知，交由 SENSOR_OFFLINE
    規則以「最後一筆距今多久」判定。
    """
    full_range = pd.date_range(agg['ts_hour'].min(), agg['ts_hour'].max(), freq='h')
    missing = full_range.difference(pd.DatetimeIndex(agg['ts_hour']))
    if len(missing) == 0:
        return agg

    day_sph = day_sph or {}
    gap_rows = [{
        'ts_hour': ts,
        'data_status': DataStatus.NO_DATA,
        'completeness': 0.0,
        'n_samples_total': 0,
        'n_samples_running': 0,
        'expected_samples': day_sph.get(ts.floor('D'), cfg.expected_samples_per_hour),
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


def rollup_daily(agg: pd.DataFrame, cfg: AggregateConfig = DEFAULT_AGG) -> pd.DataFrame:
    """
    把每小時聚合再彙整為每日一筆，供週報與長期趨勢使用。

    小時層是判定用的（規則引擎逐日評估最近幾小時），日層是**呈現用的**
    ——週報的趨勢圖、設備間比較、A/B 期間對照都跑在日層。兩者的取捨不同：
    小時層要保留缺口列讓趨勢圖能正確斷線，日層則要一個代表值。

    聚合語意沿用 `AGG_SPEC`：mean 類取各小時代表值的平均，max 類取各小時
    最大值的最大——**衝擊事件不可被平均掉**，這與小時層的理由相同。

    `running_hours` 是備機判定的依據（`STANDBY_NO_RUNTIME`），算的是
    「該小時有運轉樣本」的小時數，因此 `partial` 也計入：資料不全不代表
    設備沒轉，把它排除會讓備機判定失準。但**指標只取 `ok` 小時**，
    partial 的數字不具代表性。

    Returns:
        DataFrame[date, running_hours, <指標欄>, axis_energy_sorted]；
        輸入為空時回傳空 DataFrame。
    """
    if agg is None or agg.empty or 'ts_hour' not in agg.columns:
        return pd.DataFrame()

    d = agg.copy()
    d['_date'] = pd.to_datetime(d['ts_hour']).dt.date

    rows = []
    for day, sub in d.groupby('_date', sort=True):
        ok = sub[sub['data_status'] == DataStatus.OK]
        row: dict = {
            'date': day,
            'running_hours': int((pd.to_numeric(sub.get('n_samples_running'),
                                                errors='coerce').fillna(0) > 0).sum()),
        }

        for target, (_source, how) in AGG_SPEC.items():
            if target not in ok.columns or ok.empty:
                row[target] = None
                continue
            col = pd.to_numeric(ok[target], errors='coerce').dropna()
            if col.empty:
                row[target] = None
            elif how == AGG_MAX:
                row[target] = float(col.max())
            elif how == AGG_MIN:
                row[target] = float(col.min())
            else:                      # mean / at_max 都取平均作為當日代表值
                row[target] = float(col.mean())

        for target in AXIS_IMPACT_COLS:
            col = (pd.to_numeric(ok[target], errors='coerce').dropna()
                   if target in ok.columns and not ok.empty else pd.Series(dtype=float))
            row[target] = float(col.max()) if not col.empty else None

        # 軸能量分佈取各分量的中位數。用中位數而非平均，是因為單一小時的
        # 異常佔比（例如接近停機時的雜訊）不該把當日代表值帶走。
        dicts = [v for v in ok.get('axis_energy_sorted', pd.Series(dtype=object))
                 if isinstance(v, dict)]
        row['axis_energy_sorted'] = (
            {k: float(np.median([x[k] for x in dicts if k in x]))
             for k in ('major', 'mid', 'minor', 'energy')
             if any(k in x for x in dicts)}
            if dicts else None
        )
        rows.append(row)

    return pd.DataFrame(rows)
