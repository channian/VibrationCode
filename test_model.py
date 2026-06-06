"""
test_model.py — 階段三 / 四快速驗證腳本

讀取 Vibration_Data/ 的真實資料，跑完整管線後：
  1. 自動偵測基準期
  2. 訓練 VFDEdgeHealthModel
  3. 全量推論
  4. 輸出 output/scores/{device_id}_scores.csv
  5. 產出 output/reports/{device_id}_{position}_report.png
  6. 產出 output/device_summary.csv
  7. 印出摘要（基準期平均分數、最新分數）

執行方式：
    python test_model.py
    python test_model.py --device ZP1_2_M1
    python test_model.py --diagnose              # 加印模型有效性診斷報告
    python test_model.py --device ZP1_2_M1 --diagnose
    python test_model.py --no-report             # 跳過 PNG 產出（純數字驗證）
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
from src.reporter import generate_report, generate_device_summary
from src.scada_loader import load_other_data, load_tag_mapping, pivot_scada, merge_vib_scada

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('test_model')

SCORE_DIR     = 'output/scores'
MODEL_DIR     = 'output/models'
REPORT_DIR    = 'output/reports'
CANDIDATE_DIR = 'output/baseline_candidates'


def _print_diagnose(device_id: str, df_scored: pd.DataFrame,
                    df_baseline: pd.DataFrame, model) -> None:
    """
    印出模型有效性診斷報告，協助判斷分數飄浮原因。
    """
    import numpy as np
    SEP = '-' * 55

    print(f"\n{SEP}")
    print(f"  診斷報告：{device_id}")
    print(SEP)

    # 1. Bin 邊界（工況分層）
    bin_col  = getattr(model, '_bin_col', None) or 'current_A'
    bin_unit = 'Hz' if any(k in bin_col.lower() for k in ('freq', 'hz', '頻率')) else 'A'
    print(f"\n【1】Load Bin 邊界（{bin_col}  單位:{bin_unit}）")
    if model._bin_edges is not None:
        edges = model._bin_edges
        for i in range(len(edges) - 1):
            print(f"  Bin {i}: {edges[i]:.2f} ~ {edges[i+1]:.2f} {bin_unit}")
        if bin_col in df_scored.columns:
            col_min = df_scored[bin_col].min()
            col_max = df_scored[bin_col].max()
            out_low  = col_min < edges[0]
            out_high = col_max > edges[-1]
            if out_low or out_high:
                print(f"  ⚠️  評分期 {bin_col} 範圍 {col_min:.1f}~{col_max:.1f} {bin_unit} "
                      f"{'部分低於' if out_low else ''}{'部分高於' if out_high else ''}"
                      f"訓練範圍 {edges[0]:.1f}~{edges[-1]:.1f} {bin_unit}\n"
                      f"     → 超出範圍的資料強制分到邊界 Bin，負載補償可能失效")
            else:
                print(f"  ✅ 評分期 {bin_col} {col_min:.1f}~{col_max:.1f} {bin_unit} 在訓練範圍內")
    else:
        print("  無工況欄位，使用單一 Bin（不分層）")

    # 2. 各 Bin 訓練參數
    print("\n【2】各 Bin 訓練參數")
    print(model.summary().to_string(index=False))

    # 3. 基準期特徵 CV（穩定性）
    print("\n【3】基準期特徵變異係數 CV（< 設定門檻才算穩定）")
    from config import settings
    for feat in settings.FEATURES:
        if feat in df_baseline.columns:
            col = df_baseline[feat].dropna()
            cv = col.std() / col.mean() if col.mean() != 0 else float('nan')
            thresh = settings.CV_THRESHOLDS.get(feat, 0.25)
            flag = '✅' if cv < thresh else '⚠️ '
            print(f"  {flag} {feat:20s} CV={cv:.3f}  (門檻 {thresh})")

    # 4. 各 Bin：基準期 vs 近期 accOA 中位數比較（核心負載補償驗證）
    print("\n【4】各 Bin 的 accOA 比較：基準期 vs 近期")
    print("     （同 Bin 內近期中位數 > 基準期中位數 → 確認是真實劣化，非負載影響）")
    if 'accOA' in df_scored.columns and not df_baseline.empty:
        bl_ts = set(df_baseline['datetime'])
        df_scored['_is_baseline'] = df_scored['datetime'].isin(bl_ts)
        for bid in sorted(df_scored['load_bin'].unique()):
            grp = df_scored[df_scored['load_bin'] == bid]
            bl_med   = grp.loc[grp['_is_baseline'],  'accOA'].median()
            rec_med  = grp.loc[~grp['_is_baseline'], 'accOA'].median()
            bl_cur   = grp.loc[grp['_is_baseline'],  'current_A'].median() if 'current_A' in grp.columns else float('nan')
            rec_cur  = grp.loc[~grp['_is_baseline'], 'current_A'].median() if 'current_A' in grp.columns else float('nan')
            if not (isinstance(bl_med, float) and isinstance(rec_med, float)):
                continue
            import math
            if math.isnan(bl_med) or math.isnan(rec_med):
                print(f"  Bin {bid}: 資料不足，無法比較")
                continue
            ratio = rec_med / bl_med if bl_med > 0 else float('nan')
            flag  = '✅ 劣化確認' if ratio > 1.15 else ('— 相近' if ratio >= 0.9 else '⬇️  改善')
            print(f"  Bin {bid}: 基準 accOA={bl_med:.3f}(cur≈{bl_cur:.1f}A)  "
                  f"近期 accOA={rec_med:.3f}(cur≈{rec_cur:.1f}A)  "
                  f"比值={ratio:.2f}  {flag}")
        df_scored.drop(columns=['_is_baseline'], inplace=True, errors='ignore')

    # 5. 分數與特徵相關性（全量）
    print("\n【5】Health_Score 與特徵相關性（負值代表特徵升 → 分數降，符合預期）")
    for feat in settings.FEATURES:
        if feat in df_scored.columns:
            valid = df_scored[['Health_Score', feat]].dropna()
            if len(valid) > 10:
                corr = valid['Health_Score'].corr(valid[feat])
                direction = '✅ 負相關' if corr < -0.3 else ('⚠️  弱' if corr < 0 else '❌ 正相關（異常）')
                print(f"  {direction}  {feat:20s} r={corr:.3f}")

    # 6. 各 Bin 分數穩定性（std）
    print("\n【6】各 Bin 分數標準差（越小越穩定，> 15 代表可能有 Bin 跳動）")
    for bid in sorted(df_scored['load_bin'].unique()):
        subset = df_scored[df_scored['load_bin'] == bid]['Health_Score'].dropna()
        if len(subset) > 0:
            flag = '✅' if subset.std() <= 15 else '⚠️ '
            print(f"  {flag} Bin {bid}: n={len(subset):>5}  std={subset.std():.1f}  "
                  f"mean={subset.mean():.1f}  min={subset.min():.1f}  max={subset.max():.1f}")

    # 7. 平滑效果比較
    if 'health_score_smooth' in df_scored.columns:
        raw_std    = df_scored['Health_Score'].std()
        smooth_std = df_scored['health_score_smooth'].std()
        print(f"\n【7】平滑效果（SCORE_SMOOTH_WINDOW={settings.SCORE_SMOOTH_WINDOW}）")
        print(f"  原始分數 std={raw_std:.1f}  →  平滑後 std={smooth_std:.1f}"
              f"  （降低 {(1 - smooth_std/raw_std)*100:.0f}%）" if raw_std > 0 else "")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device',    default=None,  help='只跑指定 device_id，如 ZP1_2_M1')
    parser.add_argument('--diagnose',  action='store_true', help='印出模型有效性診斷報告')
    parser.add_argument('--no-report', action='store_true', help='跳過 PNG 報告產出')
    args = parser.parse_args()

    os.makedirs(SCORE_DIR,     exist_ok=True)
    os.makedirs(MODEL_DIR,     exist_ok=True)
    os.makedirs(REPORT_DIR,    exist_ok=True)
    os.makedirs(CANDIDATE_DIR, exist_ok=True)

    # ── 載入資料 ──────────────────────────────────────────────
    mapping  = load_mapping('device_mapping.csv') if os.path.exists('device_mapping.csv') else pd.DataFrame()
    cur_data = load_current('Current_Data') if os.path.exists('Current_Data') else pd.DataFrame()
    vib_data = load_vibration('Vibration_Data')

    if not vib_data:
        logger.error("沒有振動資料，請先把 CSV 放進 Vibration_Data/")
        sys.exit(1)

    # ── SCADA 頻率資料（Other_Data + tag_mapping）─────────────
    df_other = pd.DataFrame()
    tag_map  = pd.DataFrame()
    if os.path.exists('Other_Data') and os.path.exists('tag_mapping.csv'):
        df_other = load_other_data('Other_Data')
        tag_map  = load_tag_mapping('tag_mapping.csv')
        if not df_other.empty:
            logger.info(f"SCADA 已載入：{len(df_other):,} 筆，"
                        f"{df_other['tagname'].nunique()} 個 tagname")

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

        # ── SCADA 頻率合併（清洗後、基準期偵測前）──────────────
        df_merged = df_clean
        if not df_other.empty and not tag_map.empty:
            dev_tags = tag_map[tag_map['device_id'] == device_base]
            if not dev_tags.empty:
                df_dev_scada = df_other[df_other['tagname'].isin(dev_tags['tagname'].tolist())]
                if not df_dev_scada.empty:
                    try:
                        df_wide   = pivot_scada(df_dev_scada, dev_tags)
                        df_merged = merge_vib_scada(df_clean, df_wide)
                        scada_cols = [c for c in df_wide.columns if c != 'datetime']
                        logger.info(f"  SCADA 欄位已合併：{scada_cols}")
                    except Exception as e:
                        logger.warning(f"  SCADA 合併失敗，繼續不含 SCADA：{e}")

        # 基準期（自動偵測 Rank 1）
        df_baseline, is_manual = resolve_baseline(
            df_merged,
            mapping_row,
            device_id=device_id,
            output_dir=CANDIDATE_DIR,
        )
        if df_baseline.empty:
            logger.error(f"{device_id}: 無法取得基準期，跳過")
            continue

        # 訓練（自動偵測頻率欄位 → 電流 → 單一 bin）
        try:
            model = VFDEdgeHealthModel()
            model.train(df_baseline)
        except Exception as e:
            logger.error(f"{device_id}: 訓練失敗 — {e}")
            continue

        # 全量推論
        try:
            df_scored = model.predict(df_merged)
        except Exception as e:
            logger.error(f"{device_id}: 推論失敗 — {e}")
            continue

        # 儲存模型
        model_path = os.path.join(MODEL_DIR, f"{device_id}.pkl")
        model.save(model_path)

        position = df_raw['position'].iloc[0]

        # 輸出分數 CSV（動態加入 bin_col，如頻率欄位）
        out_cols = ['datetime', 'device_id', 'position', 'load_bin',
                    'Health_Score', 'health_score_smooth', 'alert_level',
                    'Total_vRMS', 'accOA', 'Crest_Factor', 'current_A']
        bin_col = getattr(model, '_bin_col', None)
        if bin_col and bin_col not in out_cols:
            out_cols.append(bin_col)
        df_scored['device_id'] = device_id
        df_scored['position']  = position
        out_df = df_scored[[c for c in out_cols if c in df_scored.columns]]
        out_df = out_df.rename(columns={'Health_Score': 'health_score'})
        score_path = os.path.join(SCORE_DIR, f"{device_id}_scores.csv")
        out_df.to_csv(score_path, index=False)

        # 統計摘要（df_baseline 的 datetime 比對 df_scored）
        baseline_avg = df_scored.loc[
            df_scored['datetime'].isin(df_baseline['datetime']), 'Health_Score'
        ].mean()
        latest_score = df_scored['Health_Score'].dropna().iloc[-1] if df_scored['Health_Score'].notna().any() else float('nan')
        alert        = df_scored['alert_level'].dropna().iloc[-1]   if df_scored['alert_level'].notna().any() else 'Unknown'
        baseline_ok  = '✅' if baseline_avg >= 85 else '⚠️ '

        logger.info(f"  基準期平均分數 : {baseline_avg:.1f}  {baseline_ok}")
        logger.info(f"  最新健康分數   : {latest_score:.1f}  [{alert}]")
        logger.info(f"  分數 CSV 已存  : {score_path}")

        # PNG 報告
        if not args.no_report:
            try:
                report_path = generate_report(
                    device_id=device_id,
                    position=position,
                    df_scored=df_scored,
                    df_baseline=df_baseline,
                    output_dir=REPORT_DIR,
                )
                logger.info(f"  PNG 報告已存   : {report_path}")
            except Exception as e:
                logger.error(f"{device_id}: 報告產出失敗 — {e}")

        if args.diagnose:
            _print_diagnose(device_id, df_scored, df_baseline, model)

        # 取 machine_id / model_group（from mapping，否則退回預設值）
        def _mval(key, default=''):
            if mapping_row is None:
                return default
            try:
                v = mapping_row[key]
                return str(v) if pd.notna(v) else default
            except (KeyError, TypeError):
                return default

        summary.append({
            'device_id':      device_id,
            'machine_id':     _mval('machine_id', device_base),
            'model_group':    _mval('model_group'),
            'position':       position,
            'baseline_avg':   round(baseline_avg, 1),
            'baseline_ok':    baseline_ok,
            'latest_score':   round(latest_score, 1),
            'alert':          alert,
            'is_manual_base': is_manual,
            'baseline_start': df_baseline['datetime'].min().strftime('%Y-%m-%d') if not df_baseline.empty else '',
            'baseline_end':   df_baseline['datetime'].max().strftime('%Y-%m-%d') if not df_baseline.empty else '',
            'data_count':     len(df_clean),
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

        # 產出設備總覽 CSV
        summary_path = 'output/device_summary.csv'
        try:
            generate_device_summary(summary, output_path=summary_path)
            print(f"\n  設備總覽已存   : {summary_path}")
        except Exception as e:
            logger.error(f"device_summary 產出失敗 — {e}")

        print(f"\n  PNG 報告位置   : output/reports/")
        print(f"  分數 CSV 位置  : output/scores/")


if __name__ == '__main__':
    main()
