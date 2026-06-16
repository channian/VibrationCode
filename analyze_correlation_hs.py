"""
analyze_correlation_hs.py — SCADA 變數與振動特徵 + 外部 HealthScore 關聯性分析

與 analyze_correlation.py 相同邏輯，差異在於：
  - 振動特徵欄位額外包含 Vibration_Data CSV 中的 HealthScore 欄位
  - 輸出至獨立目錄 output/correlation_hs/，不覆蓋原始結果

前置條件：
  - Other_Data/        ← SCADA 資料（3 欄：datetime / tagname / value）
  - tag_mapping.csv    ← tagname 對應定義（tagname / variable_type / device_id / unit）
  - Vibration_Data/    ← 振動資料（需含 HealthScore 欄位）

執行方式：
    python analyze_correlation_hs.py                    # 全部設備
    python analyze_correlation_hs.py --device ZP1_2_M1  # 指定設備

輸出：
    output/correlation_hs/{device_id}_heatmap.png
    output/correlation_hs/{device_id}_scatter_top.png
    output/correlation_hs/{device_id}_correlation_table.csv
    output/correlation_hs/all_devices_correlation.csv   （多台設備時）
"""

import os
import sys
import argparse
import logging
import warnings
import numpy as np
import pandas as pd
from scipy import stats
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib import font_manager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import load_vibration, safe_read_csv
from src.filters import apply_all_filters
from src.scada_loader import (load_other_data, load_tag_mapping,
                               pivot_scada, merge_vib_scada, MERGE_TOL)
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('analyze_correlation_hs')

warnings.filterwarnings('ignore', message='Glyph.*missing', category=UserWarning)

OTHER_DATA_DIR   = 'Other_Data'
TAG_MAPPING_PATH = 'tag_mapping.csv'
OUTPUT_DIR       = 'output/correlation_hs'

# 振動特徵 + Vibration_Data CSV 中的外部 HealthScore
VIB_FEATURES = settings.FEATURES + ['HealthScore']   # ['Total_vRMS', 'accOA', 'Crest_Factor', 'HealthScore']

_FONT_READY = False


# ── 字型 ────────────────────────────────────────────────────

def _setup_font() -> None:
    global _FONT_READY
    if _FONT_READY:
        return
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in settings.FONTS:
        if name in available:
            matplotlib.rcParams['font.family'] = name
            matplotlib.rcParams['axes.unicode_minus'] = False
            _FONT_READY = True
            return
    matplotlib.rcParams['axes.unicode_minus'] = False
    _FONT_READY = True


# ── 相關性計算 ───────────────────────────────────────────────

def compute_correlations(df: pd.DataFrame,
                         scada_cols: list[str],
                         vib_cols: list[str]) -> pd.DataFrame:
    """
    計算 SCADA 欄位 × 振動特徵的 Pearson / Spearman r。
    回傳：variable_type, vibration_feature, pearson_r, pearson_p,
           spearman_r, spearman_p, n_samples
    """
    rows = []
    for sc in scada_cols:
        for vc in vib_cols:
            valid = df[[sc, vc]].dropna()
            n = len(valid)
            if n < 10:
                continue
            x, y = valid[sc].values, valid[vc].values
            if np.std(x) < 1e-10 or np.std(y) < 1e-10:
                logger.debug(f"  skip constant: {sc} vs {vc}")
                continue
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                pr, pp = stats.pearsonr(x, y)
                sr, sp = stats.spearmanr(x, y)
            if np.isnan(pr) or np.isnan(sr):
                continue
            rows.append({
                'variable_type':     sc,
                'vibration_feature': vc,
                'pearson_r':         round(float(pr), 4),
                'pearson_p':         f"{pp:.2e}",
                'spearman_r':        round(float(sr), 4),
                'spearman_p':        f"{sp:.2e}",
                'n_samples':         n,
            })
    df_corr = pd.DataFrame(rows)
    if not df_corr.empty:
        df_corr = (df_corr
                   .assign(_abs=df_corr['pearson_r'].abs())
                   .sort_values('_abs', ascending=False)
                   .drop(columns='_abs')
                   .reset_index(drop=True))
    return df_corr


# ── 圖表輸出 ────────────────────────────────────────────────

def _save_fig(fig, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    logger.info(f"  圖表已存：{path}")


def plot_heatmap(corr_df: pd.DataFrame, device_id: str, output_dir: str) -> None:
    """Pearson r 熱圖：SCADA 變數（列）× 振動特徵（欄）。"""
    _setup_font()
    if corr_df.empty:
        return

    pivot = corr_df.pivot(index='variable_type',
                          columns='vibration_feature',
                          values='pearson_r')

    fig_h = max(4, len(pivot) * 0.65 + 2)
    fig, ax = plt.subplots(figsize=(9, fig_h))

    sns.heatmap(
        pivot, ax=ax,
        annot=True, fmt='.3f',
        cmap='RdBu_r', center=0, vmin=-1, vmax=1,
        linewidths=0.5, linecolor='white',
        annot_kws={'size': 10},
        cbar_kws={'label': 'Pearson r'},
    )
    ax.set_title(f"{device_id}  —  SCADA 變數與振動特徵 + HealthScore 相關性（Pearson r）",
                 fontsize=12, pad=12)
    ax.set_xlabel('振動特徵', fontsize=10)
    ax.set_ylabel('')
    plt.tight_layout()
    _save_fig(fig, os.path.join(output_dir, f"{device_id}_heatmap.png"))


def plot_scatter_top(df: pd.DataFrame, corr_df: pd.DataFrame,
                     device_id: str, output_dir: str, top_n: int = 6) -> None:
    """前 top_n 強相關對的散佈圖（以時間早晚著色）。"""
    _setup_font()
    if corr_df.empty or len(df) < 6:
        return

    top = corr_df.head(top_n)
    n   = len(top)
    ncols = min(3, n)
    nrows = (n + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(5 * ncols, 4 * nrows))
    axes = np.array(axes).flatten()

    t_min = df['datetime'].min()
    t_max = df['datetime'].max()
    t_span = (t_max - t_min).total_seconds() or 1
    time_norm = ((df['datetime'] - t_min).dt.total_seconds() / t_span).values

    for i, (_, row) in enumerate(top.iterrows()):
        ax   = axes[i]
        xcol = row['variable_type']
        ycol = row['vibration_feature']
        valid_idx = df[[xcol, ycol]].dropna().index
        x = df.loc[valid_idx, xcol].values
        y = df.loc[valid_idx, ycol].values
        tn = time_norm[valid_idx]

        sc = ax.scatter(x, y, c=tn, cmap='coolwarm_r',
                        s=10, alpha=0.5, zorder=2)
        plt.colorbar(sc, ax=ax, label='早 → 近', fraction=0.04, pad=0.02)

        if len(x) >= 2:
            z  = np.polyfit(x, y, 1)
            xr = np.linspace(x.min(), x.max(), 100)
            ax.plot(xr, np.poly1d(z)(xr), color='black',
                    linewidth=1.5, linestyle='--', zorder=3)

        ax.set_xlabel(xcol, fontsize=9)
        ax.set_ylabel(ycol, fontsize=9)
        ax.set_title(f"r = {row['pearson_r']:.3f}  (n={row['n_samples']})", fontsize=10)
        ax.grid(True, alpha=0.3, linestyle='--')

    for j in range(i + 1, len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(f"{device_id}  —  相關性最強前 {n} 對  （藍=早期  紅=近期）",
                 fontsize=12, y=1.01)
    plt.tight_layout()
    _save_fig(fig, os.path.join(output_dir, f"{device_id}_scatter_top.png"))


# ── 主分析邏輯 ──────────────────────────────────────────────

def analyze_device(device_id: str,
                   df_vib_raw: pd.DataFrame,
                   df_other: pd.DataFrame,
                   tag_map: pd.DataFrame,
                   output_dir: str) -> pd.DataFrame:
    """
    對單台設備執行完整關聯性分析（含 HealthScore）。
    回傳：相關係數 DataFrame（空 = 資料不足或設定缺漏）
    """
    logger.info(f"\n{'='*55}")
    logger.info(f"  {device_id}")
    logger.info(f"{'='*55}")

    # ── 振動清洗（過濾停機、突波）── HealthScore 欄位隨資料列保留
    df_vib = apply_all_filters(df_vib_raw)
    if df_vib.empty:
        logger.warning(f"{device_id}: 振動清洗後無有效資料，跳過")
        return pd.DataFrame()
    logger.info(f"  振動有效筆數：{len(df_vib)}")

    # HealthScore 欄位存在性確認
    has_hs = 'HealthScore' in df_vib.columns and df_vib['HealthScore'].notna().any()
    if not has_hs:
        logger.warning(f"{device_id}: Vibration_Data 中找不到有效的 HealthScore 欄位，"
                       f"將僅分析 {settings.FEATURES}")

    # ── 從 tag_mapping 找此設備的 tags ──
    device_base = df_vib_raw['devicename'].iloc[0]
    dev_tags = tag_map[tag_map['device_id'] == device_base]
    if dev_tags.empty:
        logger.warning(f"{device_id}: tag_mapping 中找不到 device_id='{device_base}'。"
                       f"現有 device_id：{sorted(tag_map['device_id'].unique())}")
        return pd.DataFrame()

    tagnames = dev_tags['tagname'].tolist()
    df_dev = df_other[df_other['tagname'].isin(tagnames)]
    if df_dev.empty:
        logger.warning(f"{device_id}: Other_Data 中找不到 tagname {tagnames}")
        return pd.DataFrame()

    # ── Pivot 成寬表 ──
    df_wide = pivot_scada(df_dev, dev_tags)
    scada_cols = [c for c in df_wide.columns if c != 'datetime']
    logger.info(f"  SCADA 變數：{scada_cols}")

    # ── 時間重疊確認 ──
    vib_range   = (df_vib['datetime'].min(), df_vib['datetime'].max())
    scada_range = (df_wide['datetime'].min(), df_wide['datetime'].max())
    overlap = vib_range[0] <= scada_range[1] and scada_range[0] <= vib_range[1]
    if not overlap:
        logger.warning(f"{device_id}: 振動 {vib_range[0].date()}~{vib_range[1].date()} "
                       f"與 SCADA {scada_range[0].date()}~{scada_range[1].date()} 無時間重疊")
        return pd.DataFrame()

    # ── 對齊合併 ──
    df_merged = merge_vib_scada(df_vib, df_wide)

    # ── 計算相關性（VIB_FEATURES 含 HealthScore，缺欄位自動跳過）──
    vib_cols = [f for f in VIB_FEATURES if f in df_merged.columns]
    logger.info(f"  分析特徵欄位：{vib_cols}")
    corr_df  = compute_correlations(df_merged, scada_cols, vib_cols)
    if corr_df.empty:
        logger.warning(f"{device_id}: 資料不足，無法計算相關係數")
        return pd.DataFrame()

    # ── 輸出 CSV ──
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"{device_id}_correlation_table.csv")
    corr_df.to_csv(csv_path, index=False)

    # ── Console 摘要 ──
    print(f"\n  ┌─ {device_id}  相關係數排名（|Pearson r| 由大到小）")
    print(f"  │  {'SCADA 變數':<16} {'振動特徵':<18} {'Pearson r':>10} {'Spearman r':>11}")
    print(f"  │  {'─'*58}")
    for _, r in corr_df.iterrows():
        abs_r = abs(r['pearson_r'])
        flag  = '★' if abs_r >= 0.6 else ('△' if abs_r >= 0.3 else '·')
        print(f"  │{flag} {r['variable_type']:<16} {r['vibration_feature']:<18} "
              f"{r['pearson_r']:>10.3f} {r['spearman_r']:>11.3f}")
    print(f"  └─ ★ 強相關(|r|≥0.6)  △ 中相關(|r|≥0.3)  · 弱相關")
    print(f"     相關係數表 → {csv_path}")

    # ── 圖表 ──
    plot_heatmap(corr_df, device_id, output_dir)
    plot_scatter_top(df_merged, corr_df, device_id, output_dir)

    return corr_df


# ── 入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='SCADA 變數與振動特徵 + HealthScore 關聯性分析'
    )
    parser.add_argument('--device', default=None, help='只分析指定 device_id，如 ZP1_2_M1')
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    vib_data = load_vibration('Vibration_Data')
    if not vib_data:
        logger.error("找不到振動資料，請確認 Vibration_Data/ 資料夾")
        sys.exit(1)

    # HealthScore 欄位存在性統計
    hs_count = sum(
        1 for df in vib_data.values()
        if 'HealthScore' in df.columns and df['HealthScore'].notna().any()
    )
    logger.info(f"含 HealthScore 欄位的設備：{hs_count} / {len(vib_data)} 台")
    if hs_count == 0:
        logger.warning("所有設備的 Vibration_Data 均無 HealthScore 欄位，"
                       "將退回只分析振動特徵")

    df_other = load_other_data(OTHER_DATA_DIR)
    tag_map  = load_tag_mapping(TAG_MAPPING_PATH)

    if df_other.empty:
        logger.error("Other_Data/ 無資料，請先放入 SCADA CSV")
        sys.exit(1)
    if tag_map.empty:
        logger.error("tag_mapping.csv 讀取失敗，請確認檔案存在且格式正確")
        sys.exit(1)

    all_results = []

    for device_id, df_raw in sorted(vib_data.items()):
        if args.device and device_id != args.device:
            continue
        corr = analyze_device(device_id, df_raw, df_other, tag_map, OUTPUT_DIR)
        if not corr.empty:
            corr.insert(0, 'device_id', device_id)
            all_results.append(corr)

    if not all_results:
        logger.warning("沒有任何設備產出相關係數，請確認 tag_mapping 的 device_id 與振動檔名一致")
        return

    if len(all_results) > 1:
        combined_path = os.path.join(OUTPUT_DIR, 'all_devices_correlation.csv')
        pd.concat(all_results, ignore_index=True).to_csv(combined_path, index=False)
        logger.info(f"全設備相關係數彙整 → {combined_path}")

    print(f"\n  輸出目錄：{OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
