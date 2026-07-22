"""
scada_loader.py — Other_Data 與 tag_mapping 共用讀取邏輯

供 analyze_correlation.py 與 export_vibcurrent.py 共同使用。
"""

import os
import logging
import pandas as pd

from src.data_loader import safe_read_csv
from config import settings

logger = logging.getLogger(__name__)

MERGE_TOL = pd.Timedelta(minutes=settings.MERGE_TOLERANCE_MIN)


# ── 基礎工具 ────────────────────────────────────────────────

def parse_dt(series: pd.Series) -> pd.Series:
    """時間欄位正規化（/ → -）後解析為 datetime。"""
    return pd.to_datetime(
        series.astype(str)
        .str.replace('/', '-', regex=False)
        .str.replace(r'\s+', ' ', regex=True)
        .str.strip(),
        errors='coerce',
    )


def detect_col(df: pd.DataFrame, candidates: list) -> str | None:
    """從 candidates 清單找到第一個存在於 df 的欄位（不分大小寫）。"""
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


# ── 資料載入 ────────────────────────────────────────────────

def load_other_data(folder: str) -> pd.DataFrame:
    """
    讀取 Other_Data/ 的所有 CSV，合併為 long format。
    回傳欄位：datetime, tagname, value
    """
    if not os.path.exists(folder):
        logger.warning(f"資料夾 '{folder}' 不存在")
        return pd.DataFrame(columns=['datetime', 'tagname', 'value'])

    dfs = []
    for fname in sorted(os.listdir(folder)):
        if not fname.lower().endswith('.csv'):
            continue
        path = os.path.join(folder, fname)
        try:
            df = safe_read_csv(path)
        except Exception as e:
            logger.warning(f"{fname}: 讀取失敗 — {e}")
            continue

        time_col = detect_col(df, ['datetime', 'date', 'time', 'timestamp'])
        if time_col is None:
            time_col = next((c for c in df.columns
                             if 'date' in c.lower() or 'time' in c.lower()), None)
        if time_col is None:
            logger.warning(f"{fname}: 找不到時間欄位，跳過")
            continue

        tag_col = detect_col(df, ['tagname', 'tag_name', 'tag', 'sensor',
                                   'sensorname', 'TagName'])
        if tag_col is None:
            logger.warning(f"{fname}: 找不到 tagname 欄位，跳過。現有欄位：{list(df.columns)}")
            continue

        val_col = detect_col(df, ['value', 'val', 'measurement',
                                   'reading', 'current', 'Value'])
        if val_col is None:
            logger.warning(f"{fname}: 找不到 value 欄位，跳過。現有欄位：{list(df.columns)}")
            continue

        chunk = df[[time_col, tag_col, val_col]].copy()
        chunk.columns = ['datetime', 'tagname', 'value']
        chunk['datetime'] = parse_dt(chunk['datetime'])
        chunk = chunk.dropna(subset=['datetime'])
        chunk['value'] = pd.to_numeric(chunk['value'], errors='coerce')
        dfs.append(chunk)
        logger.info(f"  {fname}: {len(chunk)} 筆")

    if not dfs:
        logger.warning("Other_Data/ 沒有可讀取的資料")
        return pd.DataFrame(columns=['datetime', 'tagname', 'value'])

    result = pd.concat(dfs, ignore_index=True).sort_values('datetime').reset_index(drop=True)
    logger.info(f"Other_Data 合計：{len(result)} 筆，{result['tagname'].nunique()} 個 tagname")
    return result


def load_tag_mapping(path: str) -> pd.DataFrame:
    """
    讀取 tag_mapping.csv。
    必要欄位：tagname / variable_type / device_id
    選填欄位：unit
    """
    if not os.path.exists(path):
        logger.warning(f"'{path}' 不存在")
        return pd.DataFrame(columns=['tagname', 'variable_type', 'device_id', 'unit'])
    try:
        df = safe_read_csv(path)
    except Exception as e:
        logger.error(f"tag_mapping 讀取失敗：{e}")
        return pd.DataFrame(columns=['tagname', 'variable_type', 'device_id', 'unit'])

    df.columns = [c.strip() for c in df.columns]

    def _norm(s: str) -> str:
        return s.lower().replace(' ', '_').replace('-', '_')

    col_map = {_norm(c): c for c in df.columns}
    rename = {}
    for target in ('tagname', 'variable_type', 'device_id', 'unit'):
        if target in col_map and col_map[target] != target:
            rename[col_map[target]] = target
    if rename:
        df = df.rename(columns=rename)

    required = ['tagname', 'variable_type', 'device_id']
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.error(f"tag_mapping 欄位對應失敗：找不到 {missing}。"
                     f"實際欄位：{list(df.columns)}")
        return pd.DataFrame(columns=['tagname', 'variable_type', 'device_id', 'unit'])

    if 'unit' not in df.columns:
        df['unit'] = ''

    for col in ('tagname', 'variable_type', 'device_id'):
        df[col] = df[col].astype(str).str.strip()

    logger.info(f"tag_mapping：{len(df)} 筆，涵蓋 {df['device_id'].nunique()} 台設備")
    return df[['tagname', 'variable_type', 'device_id', 'unit']]


# ── 資料對齊 ────────────────────────────────────────────────

def pivot_scada(df_long: pd.DataFrame, tag_map_dev: pd.DataFrame) -> pd.DataFrame:
    """Long-format SCADA → 寬表（每個 variable_type 一欄）。"""
    tag_to_type = dict(zip(tag_map_dev['tagname'], tag_map_dev['variable_type']))
    df = df_long.copy()
    df['variable_type'] = df['tagname'].map(tag_to_type)
    df = df.dropna(subset=['variable_type'])
    wide = (df
            .pivot_table(index='datetime', columns='variable_type',
                         values='value', aggfunc='mean')
            .reset_index())
    wide.columns.name = None
    return wide


def merge_vib_scada(df_vib: pd.DataFrame,
                    df_wide: pd.DataFrame,
                    tolerance: pd.Timedelta = MERGE_TOL) -> pd.DataFrame:
    """merge_asof 對齊振動與 SCADA 寬表（以振動時間軸為基準）。"""
    vib  = df_vib.sort_values('datetime').reset_index(drop=True)
    wide = df_wide.sort_values('datetime').reset_index(drop=True)
    merged = pd.merge_asof(vib, wide, on='datetime',
                           tolerance=tolerance, direction='nearest')
    scada_cols = [c for c in wide.columns if c != 'datetime']
    matched = merged[scada_cols].notna().any(axis=1).sum() if scada_cols else 0
    logger.info(f"  merge_asof: vib {len(vib)} 筆，SCADA 對齊 {matched} 筆")
    return merged


# ── 累積值處理（用電量／流量／運轉時數等 counter 型欄位）──────
#
# 重要：不同 tag 在真實 SCADA/歷史資料庫中，通常是各自獨立的時間軸
# （各自的 scan rate、各自的時間戳，彼此不會剛好對齊）。若先 pivot 成
# 寬表再逐列 diff，同一列裡其他 tag 的值多半是 NaN，會導致差分與加總
# 大量失真（甚至全部變 0）。因此差分一律在單一 tag 的原始（long format）
# 時間軸上進行，彼此獨立，最後才依日期加總合併。

def diff_by_tag(df_long: pd.DataFrame, tagname: str) -> pd.DataFrame:
    """
    對單一 tagname 的原始時序（未 pivot）逐筆差分，取得區間增量。

    負值 diff（計數器歸零/重置）視為異常，該筆增量捨棄（設為 NaN）。

    Returns:
        DataFrame[datetime, delta]（該 tag 自己的時間戳 + 與上一筆的差值）
    """
    sub = (df_long[df_long['tagname'] == tagname]
           .sort_values('datetime').reset_index(drop=True))
    if sub.empty:
        return pd.DataFrame(columns=['datetime', 'delta'])
    delta = sub['value'].diff()
    n_reset = int((delta < 0).sum())
    if n_reset:
        logger.warning(f"  tagname={tagname!r}: 偵測到 {n_reset} 次計數器歸零/重置，該筆增量已捨棄")
    return pd.DataFrame({'datetime': sub['datetime'], 'delta': delta.where(delta >= 0)})


def daily_sum_by_tag(df_long: pd.DataFrame, tagname: str) -> pd.Series:
    """
    單一 tagname 差分後依日期加總。

    Returns:
        Series[date -> 當日總增量]；min_count=1 確保「該日完全無資料」回傳 NaN
        而非誤導性的 0（pandas .sum() 對全 NaN 切片預設回傳 0）。
    """
    d = diff_by_tag(df_long, tagname)
    if d.empty:
        return pd.Series(dtype=float)
    date = d['datetime'].dt.date
    return d.groupby(date)['delta'].sum(min_count=1)


def detect_data_gaps(df_wide: pd.DataFrame,
                     expected_interval_min: float = 2.0,
                     gap_factor: float = 3.0) -> pd.DataFrame:
    """
    掃描 datetime 欄位，找出明顯大於預期取樣間隔的斷點（資料缺漏時段）。

    Args:
        expected_interval_min: 預期取樣間隔（分鐘）
        gap_factor: 間隔超過 expected_interval_min * gap_factor 才算缺漏

    Returns:
        DataFrame[gap_start, gap_end, gap_hours]，依缺漏長度由大到小排序
    """
    dt = df_wide['datetime'].dropna().sort_values().reset_index(drop=True)
    if len(dt) < 2:
        return pd.DataFrame(columns=['gap_start', 'gap_end', 'gap_hours'])

    diff_min = dt.diff().dt.total_seconds() / 60.0
    threshold = expected_interval_min * gap_factor
    gap_idx = diff_min[diff_min > threshold].index

    rows = [{
        'gap_start': dt.iloc[i - 1],
        'gap_end':   dt.iloc[i],
        'gap_hours': round(diff_min.iloc[i] / 60.0, 2),
    } for i in gap_idx]

    result = pd.DataFrame(rows, columns=['gap_start', 'gap_end', 'gap_hours'])
    if not result.empty:
        result = result.sort_values('gap_hours', ascending=False).reset_index(drop=True)
    return result
