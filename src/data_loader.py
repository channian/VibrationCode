"""
data_loader.py — 振動與電流資料的讀取、清洗與對齊

主要函式：
  safe_read_csv     多編碼 fallback 讀取
  load_vibration    掃描 Vibration_Data/，解析設備 ID，回傳 dict
  load_current      讀取 Current_Data/，合併為虛擬大表
  load_mapping      讀取 device_mapping.csv
  align_current     以 merge_asof 對齊電流到振動時間軸
"""

import os
import glob
import logging
import pandas as pd

from src.device_parser import parse_filename
from config import settings

logger = logging.getLogger(__name__)

# 時間欄位年份合法範圍
_YEAR_MIN = 2020
_YEAR_MAX = 2030


# ──────────────────────────────────────────────────────────
# 基礎工具
# ──────────────────────────────────────────────────────────

def safe_read_csv(path: str) -> pd.DataFrame:
    """
    以多種編碼依序嘗試讀取 CSV，避免中文路徑或舊系統匯出的編碼問題。
    嘗試順序：utf-8-sig → cp950 → latin1
    """
    last_error = None
    for encoding in ('utf-8-sig', 'cp950', 'latin1'):
        try:
            df = pd.read_csv(path, encoding=encoding)
            logger.debug(f"Read {os.path.basename(path)} with encoding={encoding}")
            return df
        except UnicodeDecodeError as e:
            last_error = e
        except Exception as e:
            raise RuntimeError(f"Cannot parse CSV {path}: {e}") from e
    raise RuntimeError(f"Cannot decode {path} with any encoding: {last_error}")


def _detect_time_col(df: pd.DataFrame, path: str = '') -> str:
    """自動偵測含 'Date' 或 'Time'（不分大小寫）字眼的欄位名稱。"""
    for col in df.columns:
        if 'date' in col.lower() or 'time' in col.lower():
            return col
    raise ValueError(
        f"No time column found in {'`' + path + '`' if path else 'file'}. "
        f"Available columns: {list(df.columns)}"
    )


def _parse_and_validate_datetime(series: pd.Series, source: str = '') -> pd.Series:
    """
    解析時間欄位，並驗證年份在 2020–2030 合法範圍內。
    超出範圍的值不丟棄，但印出警告。
    """
     # 1. 確保是字串，並把所有斜線 / 換成橫線 -
    # 同時把多個空格縮減為一個空格，並去掉前後空白
    clean_series = (
        series.astype(str)
        .str.replace('/', '-', regex=False)
        .str.replace(r'\s+', ' ', regex=True)
        .str.strip()
    )

    # 2. 解析時間 (因為已經統一成橫線，解析會變得非常穩定)
    # 不指定 format，讓 Pandas 處理 yyyy-mm-dd 格式
    dt = pd.to_datetime(clean_series, errors='coerce')

    # 3. 偵測哪些行失敗並印出「真正」的原始內容
    nat_mask = dt.isna() & series.notna()
    if nat_mask.any():
        bad_val = series[nat_mask].unique()[:3]
        logger.warning(f"{source}: 解析失敗！原始內容範例: {list(bad_val)}")

    # ... 剩下的年份驗證邏輯 ...


    nat_count = dt.isna().sum()
    if nat_count > 0:
        logger.warning(f"{source}: {nat_count} rows failed datetime parse → set NaT")

    valid_year = (dt.dt.year >= _YEAR_MIN) & (dt.dt.year <= _YEAR_MAX)
    out_of_range = dt.notna() & ~valid_year
    if out_of_range.any():
        bad_years = dt[out_of_range].dt.year.unique().tolist()
        logger.warning(
            f"{source}: {out_of_range.sum()} rows have year outside "
            f"{_YEAR_MIN}–{_YEAR_MAX}: {bad_years}"
        )

    return dt


# ──────────────────────────────────────────────────────────
# 主要讀取函式
# ──────────────────────────────────────────────────────────

def load_vibration(folder: str) -> dict[str, pd.DataFrame]:
    """
    掃描 Vibration_Data/ 資料夾，依檔名解析 devicename 與 position（M1/M2）。

    Returns:
        dict[device_id, DataFrame]
        DataFrame 必有欄位: datetime, devicename, position, device_id
        多個相同 device_id 的檔案會合併（時間排序）
    """
    result: dict[str, pd.DataFrame] = {}
    csv_files = sorted(glob.glob(os.path.join(folder, '*.csv')))

    if not csv_files:
        logger.warning(f"No CSV files found in {folder}")
        return result

    for filepath in csv_files:
        device_base, position, device_id = parse_filename(filepath)
        if device_id is None:
            logger.warning(f"Cannot parse device info from filename: {os.path.basename(filepath)} — skipping")
            continue

        try:
            df = safe_read_csv(filepath)
            time_col = _detect_time_col(df, filepath)
            df[time_col] = _parse_and_validate_datetime(df[time_col], filepath)
            df = df.rename(columns={time_col: 'datetime'})
            df = df.dropna(subset=['datetime']).reset_index(drop=True)
            df = df.sort_values('datetime').reset_index(drop=True)

            if df.empty:
                logger.warning(
                    f"[vib] {device_id}: all rows dropped after datetime parse in "
                    f"{os.path.basename(filepath)} — check time column format"
                )
                continue

            # 掛載設備識別欄位
            df['devicename'] = device_base
            df['position'] = position
            df['device_id'] = device_id

            if device_id in result:
                result[device_id] = (
                    pd.concat([result[device_id], df], ignore_index=True)
                    .sort_values('datetime')
                    .reset_index(drop=True)
                )
            else:
                result[device_id] = df

            logger.info(f"[vib] {device_id}: loaded {len(df)} rows from {os.path.basename(filepath)}")

        except Exception as e:
            logger.error(f"Failed to load vibration file {filepath}: {e}")

    logger.info(f"load_vibration: total {len(result)} device(s) loaded from {folder}")
    return result


def load_current(folder: str) -> pd.DataFrame:
    """
    讀取 Current_Data/ 下所有 CSV，合併成虛擬大表。
    期望欄位：datetime（或含 Date/Time 字眼的欄位）、tagname、value

    Returns:
        合併後的 DataFrame，欄位: datetime, tagname, value（及原有其他欄位）
        若無資料則回傳空 DataFrame
    """
    csv_files = sorted(glob.glob(os.path.join(folder, '*.csv')))

    if not csv_files:
        logger.warning(f"No CSV files found in {folder}")
        return pd.DataFrame(columns=['datetime', 'tagname', 'value'])

    dfs = []
    for filepath in csv_files:
        try:
            df = safe_read_csv(filepath)
            time_col = _detect_time_col(df, filepath)
            df[time_col] = _parse_and_validate_datetime(df[time_col], filepath)
            df = df.rename(columns={time_col: 'datetime'})
            df = df.dropna(subset=['datetime'])

            for required in ('tagname', 'value'):
                if required not in df.columns:
                    raise ValueError(f"Missing required column '{required}'")

            dfs.append(df)
            logger.info(f"[cur] loaded {len(df)} rows from {os.path.basename(filepath)}")

        except Exception as e:
            logger.error(f"Failed to load current file {filepath}: {e}")

    if not dfs:
        return pd.DataFrame(columns=['datetime', 'tagname', 'value'])

    combined = (
        pd.concat(dfs, ignore_index=True)
        .sort_values('datetime')
        .reset_index(drop=True)
    )
    logger.info(f"load_current: {len(combined)} rows, {combined['tagname'].nunique()} tag(s)")
    return combined


def load_mapping(path: str) -> pd.DataFrame:
    """
    讀取 device_mapping.csv。

    必要欄位: tagname, devicename, machine_id, model_group
    選填欄位: train_start, train_end（人工確認後填入的基準期）

    Returns:
        DataFrame，選填欄位若不存在則補空欄
    """
    required_cols = {'tagname', 'devicename', 'machine_id', 'model_group'}

    df = safe_read_csv(path)
    # 清理欄位名稱中的前後空白（防止 BOM 或手動編輯殘留）
    df.columns = [c.strip() for c in df.columns]

    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"device_mapping.csv missing required columns: {missing}")

    # 補充選填欄位
    for col in ('train_start', 'train_end'):
        if col not in df.columns:
            df[col] = None

    # 解析日期欄位（若已填入）
    for col in ('train_start', 'train_end'):
        mask = df[col].notna() & (df[col].astype(str).str.strip() != '')
        if mask.any():
            df.loc[mask, col] = pd.to_datetime(
                df.loc[mask, col], format='mixed', dayfirst=False, errors='coerce'
            )

    logger.info(f"load_mapping: {len(df)} device(s) loaded from {path}")
    return df


# ──────────────────────────────────────────────────────────
# 電流對齊
# ──────────────────────────────────────────────────────────

def align_current(
    df_vib: pd.DataFrame,
    df_cur: pd.DataFrame,
    tagname: str,
) -> pd.DataFrame:
    """
    以 merge_asof 將電流資料對齊到振動時間軸（容忍 5 分鐘誤差）。

    Args:
        df_vib:   振動 DataFrame，必須有 'datetime' 欄位
        df_cur:   電流虛擬大表，必須有 'datetime', 'tagname', 'value' 欄位
        tagname:  要篩選的電流 tag 識別碼

    Returns:
        df_vib 加上 'current_A' 欄位（未對齊的列為 NaN）
    """
    if df_cur.empty:
        df_vib = df_vib.copy()
        df_vib['current_A'] = None
        logger.warning(f"align_current: empty current table, skipping for tagname={tagname}")
        return df_vib

    df_tag = (
        df_cur[df_cur['tagname'] == tagname][['datetime', 'value']]
        .copy()
        .sort_values('datetime')
        .reset_index(drop=True)
    )

    if df_tag.empty:
        logger.warning(f"align_current: no current data for tagname='{tagname}'")
        df_vib = df_vib.copy()
        df_vib['current_A'] = None
        return df_vib

    df_vib_sorted = df_vib.sort_values('datetime').reset_index(drop=True)

    merged = pd.merge_asof(
        df_vib_sorted,
        df_tag.rename(columns={'value': 'current_A'}),
        on='datetime',
        direction='nearest',
        tolerance=pd.Timedelta(minutes=settings.MERGE_TOLERANCE_MIN),
    )

    aligned = merged['current_A'].notna().sum()
    total = len(merged)
    logger.info(
        f"align_current [{tagname}]: {aligned}/{total} rows aligned "
        f"({aligned / total * 100:.1f}%)"
    )

    return merged
