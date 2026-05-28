"""
analyze_correlation.py — SCADA 變數與振動特徵關聯性分析

讀取 Other_Data/ 的所有 SCADA 感測器資料，與 Vibration_Data/ 對齊，
計算各 SCADA 變數與振動特徵的 Pearson / Spearman 相關係數。

前置條件：
  - Other_Data/        ← SCADA 資料（3 欄：datetime / tagname / value）
  - tag_mapping.csv    ← tagname 對應定義（tagname / variable_type / device_id / unit）
  - Vibration_Data/    ← 振動資料（同原格式，用檔名識別設備）

執行方式：
    python analyze_correlation.py                    # 全部設備
    python analyze_correlation.py --device ZP1_2_M1  # 指定設備

輸出：
    output/correlation/{device_id}_heatmap.png
    output/correlation/{device_id}_scatter_top.png
    output/correlation/{device_id}_correlation_table.csv
    output/correlation/all_devices_correlation.csv   （多台設備時）
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
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('analyze_correlation')

warnings.filterwarnings('ignore', message='Glyph.*missing', category=UserWarning)

OTHER_DATA_DIR   = 'Other_Data'
TAG_MAPPING_PATH = 'tag_mapping.csv'
OUTPUT_DIR       = 'output/correlation'
VIB_FEATURES     = settings.FEATURES          # ['Total_vRMS', 'accOA', 'Crest_Factor']
MERGE_TOL        = pd.Timedelta(minutes=settings.MERGE_TOLERANCE_MIN)

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


# ── 資料載入 ────────────────────────────────────────────────

def _parse_dt(series: pd.Series) -> pd.Series:
    """同 data_loader 的時間解析邏輯（/ → - 正規化後轉換）。"""
    return pd.to_datetime(
        series.astype(str)
        .str.replace('/', '-', regex=False)
        .str.replace(r'\s+', ' ', regex=True)
        .str.strip(),
        errors='coerce',
    )


def _detect_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """從 candidates 清單依序找到第一個存在於 df 的欄位（不分大小寫）。"""
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


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

        time_col = _detect_col(df, ['datetime', 'date', 'time', 'timestamp'])
        if time_col is None:
            time_col = next((c for c in df.columns
                             if 'date' in c.lower() or 'time' in c.lower()), None)
        if time_col is None:
            logger.warning(f"{fname}: 找不到時間欄位，跳過")
            continue

        tag_col = _detect_col(df, ['tagname', 'tag_name', 'tag', 'sensor', 'sensorname', 'TagName'])
        if tag_col is None:
            logger.warning(f"{fname}: 找不到 tagname 欄位，跳過。現有欄位：{list(df.columns)}")
            continue

        val_col = _detect_col(df, ['value', 'val', 'measurement', 'reading', 'current', 'Value'])
        if val_col is None:
            logger.warning(f"{fname}: 找不到 value 欄位，跳過。現有欄位：{list(df.columns)}")
            continue

        chunk = df[[time_col, tag_col, val_col]].copy()
        chunk.columns = ['datetime', 'tagname', 'value']
        chunk['datetime'] = _parse_dt(chunk['datetime'])
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
    # case-insensitive + 空格/底線互換 的欄位對應
    def _normalize(s: str) -> str:
        return s.lower().replace(' ', '_').replace('-', '_')

    col_map = {_normalize(c): c for c in df.columns}
    rename = {}
    for target in ('tagname', 'variable_type', 'device_id', 'unit'):
        if target in col_map and col_map[target] != target:
            rename[col_map[target]] = target
    if rename:
        df = df.rename(columns=rename)

    # 若仍找不到，印出實際欄位名稱幫助診斷
    still_missing = [t for t in ('tagname', 'variable_type', 'device_id')
                     if t not in df.columns]
    if still_missing:
        logger.error(f"tag_mapping 欄位對應失敗：找不到 {still_missing}。"
                     f"實際欄位：{list(df.columns)}")

    required = ['tagname', 'variable_type', 'device_id']
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.error(f"tag_mapping 缺少欄位：{missing}")
        return pd.DataFrame(columns=['tagname', 'variable_type', 'device_id', 'unit'])

    if 'unit' not in df.columns:
        df['unit'] = ''

    # strip 空白
    for col in ('tagname', 'variable_type', 'device_id'):
        df[col] = df[col].astype(str).str.strip()

    logger.info(f"tag_mapping：{len(df)} 筆，涵蓋 {df['device_id'].nunique()} 台設備")
    return df[['tagname', 'variable_type', 'device_id', 'unit']]


# ── 資料對齊 ────────────────────────────────────────────────

def _pivot_scada(df_long: pd.DataFrame, tag_map_dev: pd.DataFrame) -> pd.DataFrame:
    """
    將設備的 long-format SCADA 轉為寬表（每個 variable_type 一欄）。
    同一時間點有多筆 → 取平均。
    """
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


def _merge_vib_scada(df_vib: pd.DataFrame, df_wide: pd.DataFrame) -> pd.DataFrame:
    """merge_asof 對齊振動與 SCADA 寬表（以振動時間軸為基準）。"""
    vib  = df_vib.sort_values('datetime').reset_index(drop=True)
    wide = df_wide.sort_values('datetime').reset_index(drop=True)
    merged = pd.merge_asof(vib, wide, on='datetime',
                           tolerance=MERGE_TOL, direction='nearest')
    scada_cols = [c for c in wide.columns if c != 'datetime']
    matched = merged[scada_cols].notna().any(axis=1).sum()
    logger.info(f"  振動 {len(vib)} 筆 × SCADA 對齊 {matched} 筆（tolerance={MERGE_TOL}）")
    return merged


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
            # 常數欄位無法計算相關係數，跳過
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
    ax.set_title(f"{device_id}  —  SCADA 變數與振動特徵相關性（Pearson r）",
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

    # 時間著色：0（早）→ 1（近）
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
    對單台設備執行完整關聯性分析。
    回傳：相關係數 DataFrame（空 = 資料不足或設定缺漏）
    """
    logger.info(f"\n{'='*55}")
    logger.info(f"  {device_id}")
    logger.info(f"{'='*55}")

    # ── 振動清洗（過濾停機、突波、平滑）──
    df_vib = apply_all_filters(df_vib_raw)
    if df_vib.empty:
        logger.warning(f"{device_id}: 振動清洗後無有效資料，跳過")
        return pd.DataFrame()
    logger.info(f"  振動有效筆數：{len(df_vib)}")

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
    df_wide = _pivot_scada(df_dev, dev_tags)
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
    df_merged = _merge_vib_scada(df_vib, df_wide)

    # ── 計算相關性 ──
    vib_cols  = [f for f in VIB_FEATURES if f in df_merged.columns]
    corr_df   = compute_correlations(df_merged, scada_cols, vib_cols)
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default=None, help='只分析指定 device_id，如 ZP1_2_M1')
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    vib_data = load_vibration('Vibration_Data')
    if not vib_data:
        logger.error("找不到振動資料，請確認 Vibration_Data/ 資料夾")
        sys.exit(1)

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

    # 多台設備彙整
    if len(all_results) > 1:
        combined_path = os.path.join(OUTPUT_DIR, 'all_devices_correlation.csv')
        pd.concat(all_results, ignore_index=True).to_csv(combined_path, index=False)
        logger.info(f"全設備相關係數彙整 → {combined_path}")

    print(f"\n  輸出目錄：{OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
