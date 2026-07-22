"""
analyze_health_score.py — 全設備 HealthScore 平均分數與趨勢分析

讀取 Vibration_Data/ 各設備振動 CSV 內建的 HealthScore 欄位，計算每台
設備的平均分數（0 分與缺值視為無效資料，不列入分母），並分析分數
隨時間的維持趨勢（上升 / 持平 / 下降）。

執行方式：
    python analyze_health_score.py
    python analyze_health_score.py --device ZP1_2_M1
    python analyze_health_score.py --recent-days 30   # 近期窗口天數，預設 30

輸出：
    output/health_score/all_devices_summary.csv
    output/health_score/trend_grid.png     — 全設備小圖矩陣（時序 + 趨勢線）
    output/health_score/report.html
"""

import os
import sys
import argparse
import logging
import warnings
import io
import base64
import math
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import load_vibration
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('analyze_health_score')

warnings.filterwarnings('ignore', message='Glyph.*missing', category=UserWarning)

OUTPUT_DIR = 'output/health_score'
HS_KEYWORDS = ('healthscore', 'health_score', 'health score')

# 趨勢分類的穩定帶（分/月）：|斜率×30| 小於此值視為持平
TREND_STABLE_BAND = 1.0

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


def _alert_color(score: float) -> str:
    if np.isnan(score):
        return '#888'
    if score >= settings.ALERT_NORMAL:
        return '#157f3b'
    if score >= settings.ALERT_WARNING:
        return '#c68400'
    return '#c0392b'


# ── 欄位辨識 ─────────────────────────────────────────────────

def _resolve_hs_col(df: pd.DataFrame) -> str | None:
    """找出 HealthScore 欄位（優先完全比對 'HealthScore'，其次關鍵字）。"""
    if 'HealthScore' in df.columns:
        return 'HealthScore'
    for c in df.columns:
        if c.lower() in HS_KEYWORDS:
            return c
    return None


# ── 單設備分析 ───────────────────────────────────────────────

def analyze_device(device_id: str, df: pd.DataFrame, recent_days: int) -> dict | None:
    hs_col = _resolve_hs_col(df)
    if hs_col is None:
        logger.warning(f"{device_id}: 找不到 HealthScore 欄位，跳過")
        return None

    n_total = len(df)
    valid = df[df[hs_col].notna() & (df[hs_col] != 0)][['datetime', hs_col]].copy()
    valid = valid.sort_values('datetime').reset_index(drop=True)
    n_valid = len(valid)
    n_excluded = n_total - n_valid

    if n_valid == 0:
        logger.warning(f"{device_id}: {n_total} 筆資料全部無效（0 分或缺值），跳過")
        return None

    scores = valid[hs_col].astype(float)
    mean_score = float(scores.mean())

    result = {
        'device_id':      device_id,
        'n_total':        n_total,
        'n_valid':        n_valid,
        'n_excluded':     n_excluded,
        'excluded_pct':   round(n_excluded / n_total * 100, 1) if n_total else float('nan'),
        'mean':           round(mean_score, 2),
        'median':         round(float(scores.median()), 2),
        'std':            round(float(scores.std()), 2) if n_valid > 1 else float('nan'),
        'min':            round(float(scores.min()), 2),
        'max':            round(float(scores.max()), 2),
        'date_start':     valid['datetime'].min(),
        'date_end':       valid['datetime'].max(),
    }

    # ── 趨勢：線性回歸斜率（分/天 → 分/月）──
    days = (valid['datetime'] - valid['datetime'].min()).dt.total_seconds() / 86400.0
    if n_valid >= 5 and days.max() > 0:
        slope, intercept = np.polyfit(days, scores, 1)
        slope_per_month = slope * 30
    else:
        slope, intercept, slope_per_month = float('nan'), float('nan'), float('nan')

    if np.isnan(slope_per_month):
        trend_label = '資料不足'
    elif slope_per_month > TREND_STABLE_BAND:
        trend_label = '上升'
    elif slope_per_month < -TREND_STABLE_BAND:
        trend_label = '下降'
    else:
        trend_label = '持平'

    result['slope_per_month'] = round(slope_per_month, 2) if not np.isnan(slope_per_month) else float('nan')
    result['trend_label'] = trend_label

    # ── 前半期 vs 後半期（依時間中點切分，非依筆數）──
    t_mid = valid['datetime'].min() + (valid['datetime'].max() - valid['datetime'].min()) / 2
    first_half  = scores[valid['datetime'] < t_mid]
    second_half = scores[valid['datetime'] >= t_mid]
    fh_mean = float(first_half.mean()) if len(first_half) else float('nan')
    sh_mean = float(second_half.mean()) if len(second_half) else float('nan')
    result['first_half_mean']  = round(fh_mean, 2) if not np.isnan(fh_mean) else float('nan')
    result['second_half_mean'] = round(sh_mean, 2) if not np.isnan(sh_mean) else float('nan')
    result['half_change_pct'] = (
        round((sh_mean - fh_mean) / fh_mean * 100, 1)
        if not np.isnan(fh_mean) and fh_mean != 0 and not np.isnan(sh_mean) else float('nan')
    )

    # ── 近期窗口（最後 N 天）vs 整體 ──
    recent_cutoff = valid['datetime'].max() - pd.Timedelta(days=recent_days)
    recent = scores[valid['datetime'] >= recent_cutoff]
    result['recent_mean'] = round(float(recent.mean()), 2) if len(recent) else float('nan')
    result['recent_n'] = len(recent)

    result['_valid_series'] = valid[['datetime', hs_col]].rename(columns={hs_col: 'score'})
    result['_slope'] = slope
    result['_intercept'] = intercept
    return result


# ── 圖表 ────────────────────────────────────────────────────

def plot_trend_grid(results: list[dict], path: str) -> None:
    """全設備小圖矩陣：每台設備一張子圖，散佈 + 回歸趨勢線。"""
    _setup_font()
    n = len(results)
    ncols = min(3, n)
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(5.2 * ncols, 3.6 * nrows), squeeze=False)

    for i, r in enumerate(results):
        ax = axes[i // ncols][i % ncols]
        series = r['_valid_series']
        color = _alert_color(r['mean'])
        ax.scatter(series['datetime'], series['score'], s=6, alpha=0.4, color='steelblue')

        if not np.isnan(r['_slope']):
            days = (series['datetime'] - series['datetime'].min()).dt.total_seconds() / 86400.0
            trend_y = r['_intercept'] + r['_slope'] * days
            ax.plot(series['datetime'], trend_y, color='crimson', lw=1.5, ls='--')

        ax.axhline(settings.ALERT_NORMAL, color='gray', ls=':', lw=0.7, alpha=0.6)
        ax.set_ylim(0, 105)
        ax.set_title(f"{r['device_id']}  (平均{r['mean']:.0f}, {r['trend_label']})",
                    fontsize=9.5, color=color)
        ax.tick_params(axis='x', labelrotation=30, labelsize=7)
        ax.tick_params(axis='y', labelsize=7)
        ax.grid(True, alpha=0.3)

    for j in range(n, nrows * ncols):
        axes[j // ncols][j % ncols].set_visible(False)

    fig.suptitle('全設備 HealthScore 趨勢（灰線=Normal門檻，紅虛線=線性趨勢）', fontsize=12, y=1.01)
    plt.tight_layout()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        fig.savefig(path, dpi=140, bbox_inches='tight')
    plt.close(fig)
    logger.info(f"趨勢矩陣圖 → {path}")


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        fig.savefig(buf, format='png', dpi=140, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8')


def plot_mean_bar(results: list[dict]) -> str:
    """全設備平均分數橫條圖，依 alert 門檻上色，由低到高排序（分數最差在最上面）。"""
    _setup_font()
    ordered = sorted(results, key=lambda r: r['mean'])
    fig, ax = plt.subplots(figsize=(8, 0.5 * len(ordered) + 1.5))
    colors = [_alert_color(r['mean']) for r in ordered]
    ax.barh([r['device_id'] for r in ordered], [r['mean'] for r in ordered], color=colors)
    ax.axvline(settings.ALERT_NORMAL, color='gray', ls=':', lw=1, alpha=0.7, label=f"Normal({settings.ALERT_NORMAL})")
    ax.axvline(settings.ALERT_WARNING, color='darkorange', ls=':', lw=1, alpha=0.7, label=f"Warning({settings.ALERT_WARNING})")
    ax.set_xlim(0, 105)
    ax.set_xlabel('平均 HealthScore（排除 0 分/缺值）')
    ax.legend(fontsize=8, loc='lower right')
    ax.grid(True, alpha=0.3, axis='x')
    plt.tight_layout()
    return _fig_to_b64(fig)


# ── HTML ────────────────────────────────────────────────────

def _summary_table_html(results: list[dict]) -> str:
    ordered = sorted(results, key=lambda r: r['mean'])
    rows = ''
    for r in ordered:
        color = _alert_color(r['mean'])
        trend_color = {'上升': '#157f3b', '下降': '#c0392b'}.get(r['trend_label'], '#888')
        std_s   = f"{r['std']:.1f}" if not np.isnan(r['std']) else '—'
        slope_s = f"{r['slope_per_month']:.2f}" if not np.isnan(r['slope_per_month']) else '—'
        pct_s   = f"{r['half_change_pct']:+.1f}%" if not np.isnan(r['half_change_pct']) else '—'
        rows += f"""<tr>
  <td>{r['device_id']}</td>
  <td>{r['n_valid']}/{r['n_total']}（排除{r['excluded_pct']}%）</td>
  <td style="color:{color}"><b>{r['mean']:.1f}</b></td>
  <td>{r['median']:.1f}</td>
  <td>{std_s}</td>
  <td>{r['min']:.1f} ~ {r['max']:.1f}</td>
  <td style="color:{trend_color}"><b>{r['trend_label']}</b></td>
  <td>{slope_s} 分/月</td>
  <td>{r['first_half_mean']} → {r['second_half_mean']}（{pct_s}）</td>
  <td>{r['recent_mean']}（近{r['recent_n']}筆）</td>
  <td>{r['date_start'].strftime('%Y-%m-%d')} ~ {r['date_end'].strftime('%Y-%m-%d')}</td>
</tr>"""
    return f"""
<table class="stats">
  <thead><tr>
    <th>設備</th><th>有效筆數</th><th>平均分</th><th>中位數</th><th>標準差</th>
    <th>範圍</th><th>趨勢</th><th>斜率</th><th>前半→後半</th><th>近期均分</th><th>資料期間</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>"""


def build_report(results: list[dict], img_bar: str, img_grid_path: str, recent_days: int) -> str:
    summary_html = _summary_table_html(results)
    grid_b64 = base64.b64encode(open(img_grid_path, 'rb').read()).decode('utf-8')

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>全設備 HealthScore 分析報告</title>
<style>
  body {{font-family:Arial,sans-serif;margin:24px 32px;color:#222;background:#f9f9f9}}
  h1   {{color:#1a3c6b;border-bottom:2px solid #1a3c6b;padding-bottom:6px}}
  h2   {{color:#2c5f9e;margin-top:32px}}
  table.stats{{border-collapse:collapse;width:100%;margin-top:8px;
               background:#fff;border-radius:4px;overflow:hidden;
               box-shadow:0 1px 3px rgba(0,0,0,.12)}}
  table.stats th,table.stats td{{border:1px solid #e0e0e0;padding:6px 10px;
                                 font-size:0.85em;text-align:right}}
  table.stats th{{background:#e8eef6;color:#333;text-align:center}}
  table.stats td:first-child{{text-align:left;font-weight:bold}}
  img{{border:1px solid #ddd;border-radius:4px;box-shadow:0 2px 6px rgba(0,0,0,.1);max-width:100%}}
  footer{{margin-top:40px;font-size:0.8em;color:#888}}
</style>
</head>
<body>
<h1>全設備 HealthScore 分析報告</h1>
<p>資料來源：Vibration_Data/ 各檔案內建的 <code>HealthScore</code> 欄位。
0 分與缺值視為無效資料（感測器未連線/模型未產出分數），已排除、不計入平均分母。
趨勢以線性回歸斜率換算為「分/月」，|斜率| &lt; {TREND_STABLE_BAND} 分/月視為持平。</p>

<h2>平均分數排行（由低到高）</h2>
<img src="data:image/png;base64,{img_bar}">

<h2>設備彙整表</h2>
{summary_html}

<h2>各設備趨勢圖</h2>
<img src="data:image/png;base64,{grid_b64}">

<footer>產生時間：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp;
VFDEdgeHealthModel analyze_health_score.py</footer>
</body>
</html>"""


# ── 入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default=None, help='只分析指定 device_id')
    parser.add_argument('--recent-days', type=int, default=30, help='近期窗口天數，預設 30')
    args = parser.parse_args()

    vib_data = load_vibration('Vibration_Data')
    if not vib_data:
        logger.error("找不到振動資料，請確認 Vibration_Data/ 資料夾")
        sys.exit(1)

    results = []
    for device_id, df in sorted(vib_data.items()):
        if args.device and device_id != args.device:
            continue
        r = analyze_device(device_id, df, args.recent_days)
        if r:
            results.append(r)
            logger.info(
                f"{device_id}: 平均={r['mean']:.1f}（{r['n_valid']}/{r['n_total']} 筆有效，"
                f"排除{r['excluded_pct']}%）| 趨勢={r['trend_label']}"
                f"（{r['slope_per_month']} 分/月）| 近{args.recent_days}天均分={r['recent_mean']}"
            )

    if not results:
        logger.warning("沒有任何設備有有效 HealthScore 資料")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    csv_path = os.path.join(OUTPUT_DIR, 'all_devices_summary.csv')
    summary_df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith('_')} for r in results])
    summary_df = summary_df.sort_values('mean').reset_index(drop=True)
    summary_df.to_csv(csv_path, index=False, encoding='utf-8-sig')
    logger.info(f"彙整表 → {csv_path}")

    detail_frames = []
    for r in results:
        s = r['_valid_series'].copy()
        s.insert(0, 'device_id', r['device_id'])
        detail_frames.append(s)
    detail_df = pd.concat(detail_frames, ignore_index=True)
    detail_path = os.path.join(OUTPUT_DIR, 'all_devices_valid_scores.csv')
    detail_df.to_csv(detail_path, index=False, encoding='utf-8-sig')
    logger.info(f"原始有效分數時序 → {detail_path}（{len(detail_df)} 筆，已排除 0 分/缺值）")

    grid_path = os.path.join(OUTPUT_DIR, 'trend_grid.png')
    plot_trend_grid(results, grid_path)

    img_bar = plot_mean_bar(results)
    html = build_report(results, img_bar, grid_path, args.recent_days)
    html_path = os.path.join(OUTPUT_DIR, 'report.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    logger.info(f"報告 → {html_path}")

    print(f"\n  完成：{len(results)} 台設備  →  {OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
