"""
test_model.py — 階段三快速驗證腳本

讀取 Vibration_Data/ 的真實資料，跑完整管線後：
  1. 自動偵測基準期
  2. 訓練 VFDEdgeHealthModel
  3. 全量推論
  4. 輸出 output/scores/{device_id}_scores.csv
  5. 印出摘要（基準期平均分數、最新分數）

執行方式：
    python test_model.py
    python test_model.py --device ZP1_2_M1
"""

import os
import sys
import argparse
import logging
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import load_vibration, load_current, load_mapping, align_current_with_diag
from src.filters import apply_all_filters
from src.baseline_detector import resolve_baseline
from src.health_model import VFDEdgeHealthModel

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('test_model')

SCORE_DIR     = 'output/scores'
MODEL_DIR     = 'output/models'
CANDIDATE_DIR = 'output/baseline_candidates'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default=None, help='只跑指定 device_id，如 ZP1_2_M1')
    args = parser.parse_args()

    os.makedirs(SCORE_DIR, exist_ok=True)
    os.makedirs(MODEL_DIR, exist_ok=True)
    os.makedirs(CANDIDATE_DIR, exist_ok=True)

    # ── 載入資料 ──────────────────────────────────────────────
    mapping  = load_mapping('device_mapping.csv') if os.path.exists('device_mapping.csv') else pd.DataFrame()
    cur_data = load_current('Current_Data') if os.path.exists('Current_Data') else pd.DataFrame()
    vib_data = load_vibration('Vibration_Data')

    if not vib_data:
        logger.error("沒有振動資料，請先把 CSV 放進 Vibration_Data/")
        sys.exit(1)

    name_to_tag = {}
    name_to_row = {}
    if not mapping.empty:
        name_to_tag = dict(zip(mapping['devicename'], mapping['tagname']))
        for _, row in mapping.iterrows():
            name_to_row[row['devicename']] = row

    summary = []

    for device_id, df_raw in sorted(vib_data.items()):
        if args.device and device_id != args.device:
            continue

        logger.info(f"\n{'='*60}")
        logger.info(f"  {device_id}")
        logger.info(f"{'='*60}")

        device_base = df_raw['devicename'].iloc[0]
        mapping_row = name_to_row.get(device_base)

        # 電流對齊（含診斷）
        df = align_current_with_diag(df_raw, cur_data, device_id, device_base, name_to_tag)

        # 清洗管線
        df_clean = apply_all_filters(df)
        if df_clean.empty:
            logger.warning(f"{device_id}: 清洗後無有效資料，跳過")
            continue

        # 基準期（自動偵測 Rank 1）
        df_baseline, is_manual = resolve_baseline(
            df_clean,
            mapping_row,
            device_id=device_id,
            output_dir=CANDIDATE_DIR,
        )
        if df_baseline.empty:
            logger.error(f"{device_id}: 無法取得基準期，跳過")
            continue

        # 訓練
        try:
            model = VFDEdgeHealthModel()
            model.train(df_baseline)
        except Exception as e:
            logger.error(f"{device_id}: 訓練失敗 — {e}")
            continue

        # 全量推論
        try:
            df_scored = model.predict(df_clean)
        except Exception as e:
            logger.error(f"{device_id}: 推論失敗 — {e}")
            continue

        # 儲存模型
        model_path = os.path.join(MODEL_DIR, f"{device_id}.pkl")
        model.save(model_path)

        # 輸出分數 CSV
        position = df_raw['position'].iloc[0]
        out_cols = ['datetime', 'device_id', 'position', 'load_bin',
                    'Health_Score', 'alert_level',
                    'Total_vRMS', 'accOA', 'Crest_Factor', 'current_A']
        df_scored['device_id'] = device_id
        df_scored['position']  = position
        out_df = df_scored[[c for c in out_cols if c in df_scored.columns]]
        out_df = out_df.rename(columns={'Health_Score': 'health_score'})
        score_path = os.path.join(SCORE_DIR, f"{device_id}_scores.csv")
        out_df.to_csv(score_path, index=False)

        # 統計摘要
        baseline_avg = df_scored.loc[
            df_scored['datetime'].isin(df_baseline['datetime']), 'Health_Score'
        ].mean()
        latest_score = df_scored['Health_Score'].dropna().iloc[-1] if df_scored['Health_Score'].notna().any() else float('nan')
        alert        = df_scored['alert_level'].dropna().iloc[-1]   if df_scored['alert_level'].notna().any() else 'Unknown'
        baseline_ok  = '✅' if baseline_avg >= 85 else '⚠️ '

        logger.info(f"  基準期平均分數 : {baseline_avg:.1f}  {baseline_ok}")
        logger.info(f"  最新健康分數   : {latest_score:.1f}  [{alert}]")
        logger.info(f"  分數 CSV 已存  : {score_path}")
        logger.info(model.summary().to_string(index=False))

        summary.append({
            'device_id':      device_id,
            'baseline_avg':   round(baseline_avg, 1),
            'baseline_ok':    baseline_ok,
            'latest_score':   round(latest_score, 1),
            'alert':          alert,
            'is_manual_base': is_manual,
        })

    # ── 最終摘要 ──────────────────────────────────────────────
    if summary:
        print(f"\n{'='*60}")
        print("  摘要：各設備健康分數")
        print(f"{'='*60}")
        for s in summary:
            print(
                f"  {s['device_id']:20s} | "
                f"基準期均分={s['baseline_avg']:5.1f} {s['baseline_ok']} | "
                f"最新={s['latest_score']:5.1f} [{s['alert']:8s}] | "
                f"基準={'人工' if s['is_manual_base'] else '自動'}"
            )
        print()
        failed = [s for s in summary if s['baseline_avg'] < 85]
        if failed:
            print(f"  ⚠️  {len(failed)} 台基準期均分 < 85，建議確認基準期是否正確")
        else:
            print("  ✅ 所有設備基準期均分 >= 85，模型校準正常")
        print(f"\n  下一步：確認後進行階段四（PNG 報告產出）")


if __name__ == '__main__':
    main()
