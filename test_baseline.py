"""
test_baseline.py — 階段二快速驗證腳本

讀取 Vibration_Data/ 的真實資料，跑完整管線後執行基準期偵測，
印出每台設備的候選基準期，並輸出 CSV 到 output/baseline_candidates/。

執行方式：
    python test_baseline.py
    python test_baseline.py --device ZP1_2_M1    # 只跑指定設備
"""

import os
import sys
import argparse
import logging
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import load_vibration, load_current, load_mapping, align_current
from src.filters import apply_all_filters
from src.baseline_detector import BaselineDetector

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('test_baseline')

CANDIDATE_DIR = 'output/baseline_candidates'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default=None, help='只跑指定 device_id，如 ZP1_2_M1')
    args = parser.parse_args()

    os.makedirs(CANDIDATE_DIR, exist_ok=True)

    # ── 載入資料 ──────────────────────────────────────────────
    mapping = load_mapping('device_mapping.csv') if os.path.exists('device_mapping.csv') else pd.DataFrame()
    cur_data = load_current('Current_Data') if os.path.exists('Current_Data') else pd.DataFrame()
    vib_data = load_vibration('Vibration_Data')

    if not vib_data:
        logger.error("沒有振動資料，請先把 CSV 放進 Vibration_Data/")
        sys.exit(1)

    name_to_tag = {}
    if not mapping.empty:
        name_to_tag = dict(zip(mapping['devicename'], mapping['tagname']))

    detector = BaselineDetector()
    summary = []

    for device_id, df_raw in sorted(vib_data.items()):
        if args.device and device_id != args.device:
            continue

        device_base = df_raw['devicename'].iloc[0]
        tagname = name_to_tag.get(device_base)

        # 電流對齊
        if tagname and not cur_data.empty:
            df = align_current(df_raw, cur_data, tagname)
        else:
            df = df_raw.copy()
            df['current_A'] = None

        # 清洗管線
        df_clean = apply_all_filters(df)

        if df_clean.empty:
            logger.warning(f"{device_id}: 清洗後無有效資料，跳過")
            continue

        logger.info(f"\n{'='*55}")
        logger.info(f"  {device_id}  ({len(df_clean)} valid rows)")
        logger.info(f"{'='*55}")

        # 基準期偵測
        candidates = detector.detect(
            df_clean,
            time_col='datetime',
            device_id=device_id,
            output_dir=CANDIDATE_DIR,
        )

        if candidates.empty:
            logger.warning(f"{device_id}: 無法偵測到基準期")
            continue

        # 印出候選表
        print(f"\n  設備 {device_id} — 候選基準期：")
        cols = ['rank', 'start_date', 'end_date', 'accOA_median',
                'vRMS_median', 'accOA_cv', 'stability_score', 'n_rows']
        print(candidates[[c for c in cols if c in candidates.columns]].to_string(index=False))
        print()

        r1 = candidates.iloc[0]
        summary.append({
            'device_id': device_id,
            'rank1_start': r1['start_date'].date(),
            'rank1_end':   r1['end_date'].date(),
            'score':       round(r1['stability_score'], 4),
            'n_rows':      int(r1['n_rows']),
        })

    # ── 最終摘要 ──────────────────────────────────────────────
    if summary:
        print("=" * 55)
        print("  摘要：各設備 Rank 1 基準期")
        print("=" * 55)
        for s in summary:
            print(f"  {s['device_id']:20s} {s['rank1_start']} → {s['rank1_end']}  "
                  f"score={s['score']:.4f}  n={s['n_rows']}")
        print()
        print(f"  候選 CSV 已存至: {CANDIDATE_DIR}/")
        print()
        print("  ✅ 下一步：請確認 Rank 1 日期是否與保養記錄相符")
        print("     若正確 → 直接進行階段三")
        print("     若不符 → 在 device_mapping.csv 填入 train_start/train_end 後重跑")


if __name__ == '__main__':
    main()
