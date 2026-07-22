"""
compare_maintenance_vibration.py — 保養前後振動快篩比較（定頻設備）

讀取保養前／保養後各一份振動量測 CSV（原始感測器匯出欄位格式，通常僅 10 分鐘），
比較 velRMS / velOA / accRMS / accOA 四個核心欄位的統計量與變化率。

適用情境：定頻設備（無需電流工況分層），只需要保養前後的簡單彙整比較，
不進入 tag_mapping / health_model 那套完整 pipeline。

執行方式：
    python compare_maintenance_vibration.py --before before.csv --after after.csv
    python compare_maintenance_vibration.py --before before.csv --after after.csv --label ZP1_2

輸出：
    output/maintenance_compare/{label}_summary.csv
    output/maintenance_compare/{label}_compare.png
"""

import os
import sys
import argparse
import logging
import warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import safe_read_csv
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('compare_maintenance_vibration')

warnings.filterwarnings('ignore', message='Glyph.*missing', category=UserWarning)

OUTPUT_DIR = 'output/maintenance_compare'
METRICS = ['velRMS', 'velOA', 'accRMS', 'accOA']

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


# ── 讀取 + 統計 ───────────────────────────────────────────────

def load_metrics(path: str) -> pd.DataFrame:
    """讀取單一 CSV，取出 4 個核心欄位並轉數值型。"""
    df = safe_read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    missing = [m for m in METRICS if m not in df.columns]
    if missing:
        raise ValueError(
            f"{path}: 找不到欄位 {missing}；現有欄位（前 10 個）：{list(df.columns)[:10]}"
        )
    out = df[METRICS].apply(pd.to_numeric, errors='coerce')
    n_dropped = out.isna().any(axis=1).sum()
    if n_dropped:
        logger.warning(f"{path}: {n_dropped} 列有非數值/缺值")
    return out


def compare(before: pd.DataFrame, after: pd.DataFrame) -> pd.DataFrame:
    """依欄位計算保養前後統計量、差異與變化率（%）。差異 = 後-前，變化率 = 差異/前 × 100。"""
    rows = []
    for m in METRICS:
        b = before[m].dropna()
        a = after[m].dropna()
        b_mean, a_mean = float(b.mean()), float(a.mean())
        diff = a_mean - b_mean
        pct = diff / b_mean * 100 if b_mean else float('nan')
        rows.append({
            '欄位':        m,
            '保養前_筆數':  len(b),
            '保養前_平均':  round(b_mean, 5),
            '保養前_中位數': round(float(b.median()), 5),
            '保養前_標準差': round(float(b.std()), 5),
            '保養後_筆數':  len(a),
            '保養後_平均':  round(a_mean, 5),
            '保養後_中位數': round(float(a.median()), 5),
            '保養後_標準差': round(float(a.std()), 5),
            '差異':        round(diff, 5),
            '變化率(%)':    round(pct, 1),
        })
    return pd.DataFrame(rows)


def build_diff_table(cmp_df: pd.DataFrame) -> pd.DataFrame:
    """從完整彙整表擷取精簡版差異表（欄位/保養前/保養後/差異/變化率），方便直接貼進報告或投影片。"""
    diff_df = cmp_df[['欄位', '保養前_平均', '保養後_平均', '差異', '變化率(%)']].copy()
    diff_df = diff_df.rename(columns={'保養前_平均': '保養前', '保養後_平均': '保養後'})
    return diff_df


# ── 圖表 ────────────────────────────────────────────────────

def plot_compare(cmp_df: pd.DataFrame, label: str, path: str) -> None:
    """4 欄位的保養前後平均值分組長條圖，柱上標變化率（振動下降=綠色，上升=紅色）。"""
    _setup_font()
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = np.arange(len(cmp_df))
    w = 0.35
    ax.bar(x - w / 2, cmp_df['保養前_平均'], width=w, label='保養前', color='steelblue')
    ax.bar(x + w / 2, cmp_df['保養後_平均'], width=w, label='保養後', color='darkorange')

    for i, row in cmp_df.iterrows():
        pct = row['變化率(%)']
        color = '#157f3b' if pct < 0 else '#c0392b'
        y = max(row['保養前_平均'], row['保養後_平均'])
        ax.text(i, y * 1.03, f"{pct:+.1f}%", ha='center', fontsize=9, color=color)

    ax.set_xticks(x)
    ax.set_xticklabels(cmp_df['欄位'])
    ax.set_title(f"{label}  —  保養前後振動比較（綠=下降/改善，紅=上升）")
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    plt.tight_layout()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    logger.info(f"圖表 → {path}")


# ── 入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before', required=True, help='保養前 CSV 路徑')
    parser.add_argument('--after',  required=True, help='保養後 CSV 路徑')
    parser.add_argument('--label',  default='device', help='輸出檔名前綴（建議用設備名稱）')
    args = parser.parse_args()

    before = load_metrics(args.before)
    after  = load_metrics(args.after)
    logger.info(f"保養前：{len(before)} 筆 / 保養後：{len(after)} 筆")

    cmp_df = compare(before, after)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUTPUT_DIR, f"{args.label}_summary.csv")
    cmp_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    logger.info(f"彙整表 → {csv_path}")

    diff_df = build_diff_table(cmp_df)
    diff_path = os.path.join(OUTPUT_DIR, f"{args.label}_diff.csv")
    diff_df.to_csv(diff_path, index=False, encoding='utf-8-sig')
    logger.info(f"差異表 → {diff_path}")

    print('\n' + cmp_df.to_string(index=False))
    print('\n' + diff_df.to_string(index=False))

    png_path = os.path.join(OUTPUT_DIR, f"{args.label}_compare.png")
    plot_compare(cmp_df, args.label, png_path)


if __name__ == '__main__':
    main()
