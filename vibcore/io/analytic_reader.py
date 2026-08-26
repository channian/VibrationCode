"""
analytic_reader.py — Tier 2 檔案（前端輸出的每秒 Analytic CSV）讀取

負責把檔案讀成標準化的 DataFrame，並抽出設備 metadata。
不做聚合與判定，那是 pipeline 的職責。

沿用舊版 `src/data_loader.py` 踩坑後的編碼與時間處理邏輯：
  · 多編碼 fallback（utf-8-sig → cp950 → latin1），處理中文路徑與舊系統匯出
  · 時間欄位正規化（/ → -、多空白縮減）後再解析
  · 年份合理範圍驗證
"""

import os
import glob
import logging
import pandas as pd

from vibcore.config import META_COLS

logger = logging.getLogger(__name__)

_ENCODINGS = ('utf-8-sig', 'cp950', 'latin1')
_YEAR_MIN, _YEAR_MAX = 2020, 2035


def safe_read_csv(path: str, **kwargs) -> pd.DataFrame:
    """
    以多種編碼依序嘗試讀取 CSV。

    分隔符號自動判斷（前端輸出為 TAB 分隔，但不假設永遠如此）。
    """
    last_error = None
    for enc in _ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=enc, sep=None, engine='python', **kwargs)
            logger.debug(f"讀取 {os.path.basename(path)}（encoding={enc}）")
            return df
        except UnicodeDecodeError as e:
            last_error = e
        except Exception as e:
            raise RuntimeError(f"無法解析 CSV {path}：{e}") from e
    raise RuntimeError(f"{path} 無法以任何編碼解讀：{last_error}")


def parse_datetime(series: pd.Series, source: str = '') -> pd.Series:
    """時間欄位正規化後解析；超出合理年份範圍者僅警告不丟棄。"""
    cleaned = (
        series.astype(str)
        .str.replace('/', '-', regex=False)
        .str.replace(r'\s+', ' ', regex=True)
        .str.strip()
    )
    dt = pd.to_datetime(cleaned, errors='coerce')

    n_fail = int(dt.isna().sum())
    if n_fail:
        sample = series[dt.isna()].unique()[:3]
        logger.warning(f"{source}：{n_fail} 筆時間解析失敗，範例 {list(sample)}")

    valid = dt.notna()
    bad_year = valid & ~dt.dt.year.between(_YEAR_MIN, _YEAR_MAX)
    if bad_year.any():
        years = sorted(dt[bad_year].dt.year.unique().tolist())
        logger.warning(f"{source}：{int(bad_year.sum())} 筆年份超出 "
                       f"{_YEAR_MIN}–{_YEAR_MAX}，實際為 {years}")
    return dt


def _detect_time_col(df: pd.DataFrame, path: str) -> str:
    for col in df.columns:
        if col.strip().lower() in ('time', 'datetime', 'timestamp', 'date'):
            return col
    for col in df.columns:
        if 'time' in col.lower() or 'date' in col.lower():
            return col
    raise ValueError(f"{path} 找不到時間欄位；現有欄位（前 10）：{list(df.columns)[:10]}")


def extract_metadata(df: pd.DataFrame) -> dict:
    """
    從 Analytic CSV 抽出設備 metadata。

    前端已把台帳資訊寫進每一列（Building/Floor/System/RPM/FMF 等），
    可直接用來建立設備台帳，不需另外維護對照表。
    """
    if df.empty:
        return {}

    first = df.iloc[0]
    meta = {c: first[c] for c in META_COLS if c in df.columns}

    # 一個檔案應只含一台設備；若不然需提醒，否則 metadata 會取錯
    if 'Name' in df.columns and df['Name'].nunique() > 1:
        logger.warning(f"單一檔案含多台設備：{sorted(df['Name'].dropna().unique())}，"
                       f"metadata 僅取第一台")
    return meta


def load_analytic_file(path: str) -> tuple[pd.DataFrame, dict]:
    """
    讀取單一 Analytic CSV。

    Returns:
        (df, meta)
        df   — 已解析 datetime 並依時間排序，欄位維持原名
        meta — 設備 metadata（Name/Building/Floor/System/RPM/FMF/Channel_* 等）
    """
    df = safe_read_csv(path)
    df.columns = [c.strip() for c in df.columns]

    time_col = _detect_time_col(df, path)
    df[time_col] = parse_datetime(df[time_col], os.path.basename(path))
    df = df.rename(columns={time_col: 'datetime'})
    df = df.dropna(subset=['datetime']).sort_values('datetime').reset_index(drop=True)

    meta = extract_metadata(df)
    logger.info(f"  {os.path.basename(path)}：{len(df)} 筆，"
                f"設備 {meta.get('Name', '?')}，"
                f"{df['datetime'].min()} ~ {df['datetime'].max()}")
    return df, meta


def load_analytic_dir(folder: str, pattern: str = '*.csv') -> dict[str, pd.DataFrame]:
    """
    讀取資料夾內所有 Analytic CSV，依設備名稱（Name 欄）分組合併。

    Returns:
        {device_id: DataFrame}，各自依時間排序
    """
    paths = sorted(glob.glob(os.path.join(folder, pattern)))
    if not paths:
        logger.warning(f"{folder} 內找不到符合 {pattern} 的檔案")
        return {}

    grouped: dict[str, list[pd.DataFrame]] = {}
    for path in paths:
        try:
            df, meta = load_analytic_file(path)
        except Exception as e:
            logger.error(f"{os.path.basename(path)} 讀取失敗，已跳過：{e}")
            continue
        if df.empty:
            continue
        device_id = str(meta.get('Name', '')).strip() or os.path.splitext(os.path.basename(path))[0]
        grouped.setdefault(device_id, []).append(df)

    result = {}
    for device_id, frames in grouped.items():
        merged = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
        result[device_id] = merged.sort_values('datetime').reset_index(drop=True)

    logger.info(f"共載入 {len(result)} 台設備")
    return result
