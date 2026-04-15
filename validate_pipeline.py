"""
validate_pipeline.py — 階段一驗證腳本

功能：讀取所有振動與電流資料、執行清洗管線，驗證資料可正常對齊。
不進行模型訓練，只做資料驗證。

執行方式：
    python validate_pipeline.py

輸出：
    output/pipeline_validation.csv
"""

import os
import sys
import logging
import pandas as pd

# 確保可以從專案根目錄 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import load_vibration, load_current, load_mapping, align_current
from src.filters import apply_all_filters

# ── 日誌設定 ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('validate_pipeline')

# ── 路徑設定 ──────────────────────────────────────────────
VIB_FOLDER     = 'Vibration_Data'
CUR_FOLDER     = 'Current_Data'
MAPPING_PATH   = 'device_mapping.csv'
OUTPUT_DIR     = 'output'
OUTPUT_CSV     = os.path.join(OUTPUT_DIR, 'pipeline_validation.csv')
MIN_VALID_ROWS = 10


def _load_mapping_safe() -> pd.DataFrame:
    """讀取 device_mapping.csv，若不存在則回傳空表並警告。"""
    if not os.path.exists(MAPPING_PATH):
        logger.warning(f"device_mapping.csv not found at '{MAPPING_PATH}' — current alignment will be skipped")
        return pd.DataFrame(columns=['tagname', 'devicename', 'machine_id', 'model_group', 'train_start', 'train_end'])
    return load_mapping(MAPPING_PATH)


def _load_current_safe() -> pd.DataFrame:
    """讀取電流資料，若資料夾不存在則回傳空表並警告。"""
    if not os.path.exists(CUR_FOLDER):
        logger.warning(f"Current_Data folder not found at '{CUR_FOLDER}' — skipping current alignment")
        return pd.DataFrame(columns=['datetime', 'tagname', 'value'])
    return load_current(CUR_FOLDER)


def main() -> int:
    """
    執行完整資料驗證管線。
    回傳 0 表示成功（所有設備均滿足最低資料量），非 0 表示有警告。
    """
    logger.info("=" * 60)
    logger.info("  VFDEdgeHealthModel v2 — Pipeline Validation (Phase 1)")
    logger.info("=" * 60)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── 載入資料 ──────────────────────────────────────────
    mapping = _load_mapping_safe()
    cur_data = _load_current_safe()

    if not os.path.exists(VIB_FOLDER):
        logger.warning(f"Vibration_Data folder not found at '{VIB_FOLDER}'")
        vib_data = {}
    else:
        vib_data = load_vibration(VIB_FOLDER)

    if not vib_data:
        logger.warning("No vibration data loaded. Please place CSV files in Vibration_Data/")
        logger.info("Validation complete — no data to process.")
        # 仍輸出空 CSV，不報錯
        pd.DataFrame(columns=[
            'device_id', 'position', 'row_count',
            'time_start', 'time_end', 'has_current', 'has_m1', 'has_m2',
        ]).to_csv(OUTPUT_CSV, index=False)
        return 0

    # ── 建立 devicename → tagname 的映射表 ──────────────
    name_to_tag: dict[str, str] = {}
    if not mapping.empty:
        name_to_tag = dict(zip(mapping['devicename'], mapping['tagname']))

    # ── 逐台設備處理 ──────────────────────────────────────
    records = []
    logger.info(f"\nProcessing {len(vib_data)} device position(s)...\n")

    for device_id, df_raw in sorted(vib_data.items()):
        if df_raw.empty:
            logger.warning(f"{device_id}: DataFrame is empty, skipping")
            records.append({
                'device_id': device_id, 'position': 'unknown', 'row_count': 0,
                'time_start': None, 'time_end': None, 'has_current': False,
            })
            continue

        device_base = df_raw['devicename'].iloc[0]
        position    = df_raw['position'].iloc[0]
        tagname     = name_to_tag.get(device_base)

        # ── 電流對齊前診斷 ────────────────────────────────
        if cur_data.empty:
            logger.warning(f"  {device_id}: 電流大表為空，跳過對齊")
        elif tagname is None:
            logger.warning(
                f"  {device_id}: device_mapping 中找不到 devicename='{device_base}' 的 tagname\n"
                f"    device_mapping 現有 devicename: {name_to_tag and list(name_to_tag.keys())}"
            )
        else:
            cur_tags = cur_data['tagname'].unique().tolist()
            if tagname not in cur_tags:
                logger.warning(
                    f"  {device_id}: tagname='{tagname}' 不在電流資料中！\n"
                    f"    電流資料實際 tagname: {cur_tags[:10]}"
                )
            else:
                vib_t0 = df_raw['datetime'].min()
                vib_t1 = df_raw['datetime'].max()
                cur_df_tag = cur_data[cur_data['tagname'] == tagname]
                cur_t0 = cur_df_tag['datetime'].min()
                cur_t1 = cur_df_tag['datetime'].max()
                overlap = not (vib_t1 < cur_t0 or cur_t1 < vib_t0)
                logger.info(
                    f"  {device_id}: tagname='{tagname}' ✅ | "
                    f"振動 {str(vib_t0)[:10]}~{str(vib_t1)[:10]} | "
                    f"電流 {str(cur_t0)[:10]}~{str(cur_t1)[:10]} | "
                    f"時間重疊={'是' if overlap else '❌ 無重疊'}"
                )

        # 電流對齊
        if tagname and not cur_data.empty:
            df_aligned = align_current(df_raw, cur_data, tagname)
        else:
            df_aligned = df_raw.copy()
            df_aligned['current_A'] = None

        has_current = (
            'current_A' in df_aligned.columns
            and df_aligned['current_A'].notna().any()
        )

        # 套用完整過濾管線
        try:
            df_filtered = apply_all_filters(df_aligned)
        except Exception as e:
            logger.error(f"{device_id}: filter pipeline failed — {e}")
            df_filtered = pd.DataFrame()

        row_count = len(df_filtered)
        time_start = df_filtered['datetime'].min() if not df_filtered.empty else None
        time_end   = df_filtered['datetime'].max() if not df_filtered.empty else None

        status = '✅' if row_count >= MIN_VALID_ROWS else '⚠️ '
        logger.info(
            f"  {status} {device_id:20s} | {row_count:>6} valid rows | "
            f"{str(time_start)[:19]} → {str(time_end)[:19]} | "
            f"current={'Yes' if has_current else 'No ':3s}"
        )
        if row_count < MIN_VALID_ROWS:
            logger.warning(
                f"     {device_id}: only {row_count} valid rows "
                f"(minimum {MIN_VALID_ROWS} required)"
            )

        records.append({
            'device_id':  device_id,
            'position':   position,
            'row_count':  row_count,
            'time_start': time_start,
            'time_end':   time_end,
            'has_current': has_current,
        })

    # ── 補充 has_m1 / has_m2（以 device_base 為單位）──────
    m1_bases = {r['device_id'].rsplit('_M1', 1)[0] for r in records if r['device_id'].endswith('_M1')}
    m2_bases = {r['device_id'].rsplit('_M2', 1)[0] for r in records if r['device_id'].endswith('_M2')}

    for r in records:
        base = r['device_id'].rsplit('_', 1)[0]   # 去掉 _M1 / _M2
        r['has_m1'] = base in m1_bases
        r['has_m2'] = base in m2_bases

    # ── 輸出 CSV ──────────────────────────────────────────
    df_out = pd.DataFrame(records, columns=[
        'device_id', 'position', 'row_count',
        'time_start', 'time_end', 'has_current', 'has_m1', 'has_m2',
    ])
    df_out.to_csv(OUTPUT_CSV, index=False)

    # ── 最終摘要 ──────────────────────────────────────────
    total = len(records)
    passed = sum(1 for r in records if r['row_count'] >= MIN_VALID_ROWS)
    failed = total - passed

    logger.info("\n" + "=" * 60)
    logger.info(f"  Total device positions : {total}")
    logger.info(f"  Passed (>= {MIN_VALID_ROWS} valid rows) : {passed}")
    logger.info(f"  Failed (< {MIN_VALID_ROWS} valid rows) : {failed}")
    logger.info(f"  Validation report saved: {OUTPUT_CSV}")
    logger.info("=" * 60)

    if failed > 0:
        logger.warning(f"{failed} device(s) below minimum data threshold — check source data")
        return 1
    logger.info("All devices passed validation ✅")
    return 0


if __name__ == '__main__':
    sys.exit(main())
