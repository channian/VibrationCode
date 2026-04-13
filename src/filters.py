"""
filters.py — 振動資料的衍生欄位計算、開機判斷、異常值過濾與平滑

函式：
  compute_derived    計算 Total_vRMS 與 Crest_Factor
  filter_spikes      過濾 Crest_Factor 突波
  is_running         依電流或 vRMS 判斷開機狀態
  smooth             滾動中位數平滑
  apply_all_filters  完整過濾管線（一次呼叫）
"""

import numpy as np
import pandas as pd
import logging

from config import settings

logger = logging.getLogger(__name__)


def compute_derived(df: pd.DataFrame) -> pd.DataFrame:
    """
    計算衍生欄位：
      Total_vRMS  = sqrt(velRMS_x² + velRMS_y² + velRMS_z²)
      Crest_Factor = velPEAK / (velRMS + 1e-6)

    若來源欄位不存在，對應衍生欄位設為 NaN 並印出警告。
    不修改原始 DataFrame。
    """
    df = df.copy()

    vib_xyz = ['velRMS_x', 'velRMS_y', 'velRMS_z']
    if 'Total_vRMS' not in df.columns:
        if all(c in df.columns for c in vib_xyz):
            df['Total_vRMS'] = np.sqrt(
                df['velRMS_x'] ** 2 + df['velRMS_y'] ** 2 + df['velRMS_z'] ** 2
            )
        else:
            missing = [c for c in vib_xyz if c not in df.columns]
            logger.warning(f"compute_derived: missing columns {missing}, Total_vRMS set to NaN")
            df['Total_vRMS'] = np.nan

    if 'Crest_Factor' not in df.columns:
        if 'velPEAK' in df.columns and 'velRMS' in df.columns:
            df['Crest_Factor'] = df['velPEAK'] / (df['velRMS'] + 1e-6)
        else:
            missing = [c for c in ('velPEAK', 'velRMS') if c not in df.columns]
            logger.warning(f"compute_derived: missing columns {missing}, Crest_Factor set to NaN")
            df['Crest_Factor'] = np.nan

    return df


def filter_spikes(df: pd.DataFrame) -> pd.DataFrame:
    """
    丟棄 Crest_Factor > CREST_FACTOR_SPIKE_THRESHOLD 的列（感測器突波）。
    若 Crest_Factor 欄位不存在則原樣回傳。
    """
    if 'Crest_Factor' not in df.columns:
        return df

    threshold = settings.CREST_FACTOR_SPIKE_THRESHOLD
    mask = df['Crest_Factor'] <= threshold
    removed = (~mask).sum()
    if removed > 0:
        logger.info(f"filter_spikes: removed {removed} rows (CF > {threshold})")
    return df[mask].reset_index(drop=True)


def is_running(df: pd.DataFrame) -> pd.DataFrame:
    """
    保留開機中的列：
      有電流欄位（current_A 非空）→ current_A > CURRENT_ON_THRESHOLD
      無電流欄位              → Total_vRMS > VTRMS_ON_THRESHOLD

    不修改原始 DataFrame。
    """
    has_current = (
        'current_A' in df.columns
        and df['current_A'].notna().any()
    )

    if has_current:
        mask = df['current_A'] > settings.CURRENT_ON_THRESHOLD
        logger.info(
            f"is_running [current]: {mask.sum()}/{len(df)} rows pass "
            f"(current_A > {settings.CURRENT_ON_THRESHOLD} A)"
        )
    else:
        if 'Total_vRMS' not in df.columns:
            logger.warning("is_running: no current and no Total_vRMS — cannot filter, returning all rows")
            return df
        mask = df['Total_vRMS'] > settings.VTRMS_ON_THRESHOLD
        logger.info(
            f"is_running [vRMS]: {mask.sum()}/{len(df)} rows pass "
            f"(Total_vRMS > {settings.VTRMS_ON_THRESHOLD} mm/s)"
        )

    return df[mask].reset_index(drop=True)


def smooth(df: pd.DataFrame, cols: list[str], window: int = 3) -> pd.DataFrame:
    """
    對指定欄位套用滾動中位數平滑（window=3, center=True）。
    邊界缺值以 ffill + bfill 填補。
    只對 DataFrame 中實際存在的欄位作用。
    """
    df = df.copy()
    for col in cols:
        if col not in df.columns:
            logger.debug(f"smooth: column '{col}' not found, skipping")
            continue
        df[col] = (
            df[col]
            .rolling(window=window, center=True, min_periods=1)
            .median()
            .ffill()
            .bfill()
        )
    return df


def apply_all_filters(df: pd.DataFrame) -> pd.DataFrame:
    """
    完整過濾管線，依序執行：
      1. compute_derived  — 計算 Total_vRMS / Crest_Factor
      2. filter_spikes    — 丟棄突波
      3. is_running       — 保留開機列
      4. smooth           — 滾動中位數平滑

    Returns:
        清洗後的 DataFrame（不含原始索引）
    """
    df = compute_derived(df)
    df = filter_spikes(df)
    df = is_running(df)
    smooth_cols = [c for c in settings.FEATURES if c in df.columns]
    df = smooth(df, smooth_cols)
    return df
