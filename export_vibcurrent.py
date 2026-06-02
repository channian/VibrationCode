"""
export_vibcurrent.py — 振動 + 電流 資料匯出（CSV + 靜態 HTML）

讀取振動資料（Vibration_Data/）與 SCADA 電流資料（Other_Data/），
對齊時間軸後過濾停機狀態，每台設備輸出：
  output/export/{device_id}.csv   — 對齊後的完整數值表
  output/export/{device_id}.html  — 靜態報告（時序圖 + 散佈圖 + 統計表）

執行方式：
    python export_vibcurrent.py                      # 全部設備
    python export_vibcurrent.py --device ZP1_2_M1    # 指定設備
    python export_vibcurrent.py --threshold 5.0      # 自訂電流下限（A）
    python export_vibcurrent.py --current-type 電流  # tag_mapping variable_type 名稱
"""

import os
import sys
import base64
import argparse
import logging
import warnings
import io
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import load_vibration
from src.filters import compute_derived, filter_spikes
from src.scada_loader import (load_other_data, load_tag_mapping,
                               pivot_scada, merge_vib_scada, MERGE_TOL)
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('export_vibcurrent')

warnings.filterwarnings('ignore', message='Glyph.*missing', category=UserWarning)

OTHER_DATA_DIR   = 'Other_Data'
TAG_MAPPING_PATH = 'tag_mapping.csv'
OUTPUT_DIR       = 'output/export'
DEFAULT_CURRENT_TYPE = '電流'

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


# ── 圖表工具 ─────────────────────────────────────────────────

def _fig_to_b64(fig) -> str:
    """將 matplotlib Figure 轉為 base64 PNG 字串（用於內嵌 HTML）。"""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        fig.savefig(buf, format='png', dpi=130, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8')


def _x_fmt(ax, dates: pd.Series) -> None:
    span = (dates.max() - dates.min()).days
    if span <= 7:
        fmt, loc = mdates.DateFormatter('%m/%d %H:%M'), mdates.HourLocator(interval=6)
    elif span <= 90:
        fmt, loc = mdates.DateFormatter('%Y/%m/%d'), mdates.WeekdayLocator(interval=1)
    else:
        fmt, loc = mdates.DateFormatter('%Y/%m'), mdates.MonthLocator()
    ax.xaxis.set_major_formatter(fmt)
    ax.xaxis.set_major_locator(loc)
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right')


# ── 圖表產生 ─────────────────────────────────────────────────

def _plot_timeseries(df: pd.DataFrame, device_id: str,
                     current_col: str | None) -> str:
    """
    時序圖（2 列）：
      上：Total_vRMS  [+ accOA 次軸]
      下：電流（若有）[+ 頻率 次軸（若有）]
    """
    _setup_font()
    has_current = current_col and current_col in df.columns and df[current_col].notna().any()
    nrows = 2 if has_current else 1
    fig, axes = plt.subplots(nrows, 1, figsize=(13, 4 * nrows), sharex=False)
    if nrows == 1:
        axes = [axes]

    # ── 振動軸 ──
    ax1 = axes[0]
    if 'Total_vRMS' in df.columns:
        ax1.plot(df['datetime'], df['Total_vRMS'],
                 color='steelblue', lw=1, label='Total vRMS (mm/s)')
    ax1.set_ylabel('Total vRMS (mm/s)', color='steelblue')
    ax1.tick_params(axis='y', labelcolor='steelblue')
    ax1.grid(True, alpha=0.3)

    if 'accOA' in df.columns:
        ax1r = ax1.twinx()
        ax1r.plot(df['datetime'], df['accOA'],
                  color='darkorange', lw=0.8, alpha=0.7, label='accOA (g)')
        ax1r.set_ylabel('accOA (g)', color='darkorange')
        ax1r.tick_params(axis='y', labelcolor='darkorange')

    ax1.set_title(f"{device_id}  —  振動時序", fontsize=11)
    _x_fmt(ax1, df['datetime'])

    # ── 電流軸 ──
    if has_current:
        ax2 = axes[1]
        ax2.plot(df['datetime'], df[current_col],
                 color='crimson', lw=1, label=f'{current_col} (A)')
        ax2.set_ylabel(f'{current_col} (A)', color='crimson')
        ax2.tick_params(axis='y', labelcolor='crimson')
        ax2.grid(True, alpha=0.3)

        freq_col = next((c for c in df.columns if '頻率' in c or 'freq' in c.lower()), None)
        if freq_col:
            ax2r = ax2.twinx()
            ax2r.plot(df['datetime'], df[freq_col],
                      color='teal', lw=0.8, alpha=0.7, label=f'{freq_col} (Hz)')
            ax2r.set_ylabel(f'{freq_col} (Hz)', color='teal')
            ax2r.tick_params(axis='y', labelcolor='teal')

        ax2.set_title(f"{device_id}  —  電流時序", fontsize=11)
        _x_fmt(ax2, df['datetime'])

    plt.tight_layout()
    return _fig_to_b64(fig)


def _plot_scatter(df: pd.DataFrame, device_id: str,
                  current_col: str | None) -> str | None:
    """
    散佈圖（2 格）：
      左：電流 vs Total_vRMS
      右：電流 vs accOA
    時間著色（早=藍、近=紅）＋趨勢線。
    若無電流欄位則回傳 None。
    """
    _setup_font()
    if not current_col or current_col not in df.columns:
        return None
    if df[current_col].notna().sum() < 10:
        return None

    vib_targets = [(c, 'steelblue') for c in ['Total_vRMS', 'accOA']
                   if c in df.columns and df[c].notna().any()]
    if not vib_targets:
        return None

    ncols = len(vib_targets)
    fig, axes = plt.subplots(1, ncols, figsize=(6 * ncols, 5))
    if ncols == 1:
        axes = [axes]

    t_min = df['datetime'].min()
    t_max = df['datetime'].max()
    t_span = (t_max - t_min).total_seconds() or 1
    time_norm = ((df['datetime'] - t_min).dt.total_seconds() / t_span).values

    for ax, (ycol, _) in zip(axes, vib_targets):
        valid = df[[current_col, ycol]].dropna()
        if len(valid) < 5:
            ax.set_visible(False)
            continue
        idx = valid.index
        x = valid[current_col].values
        y = valid[ycol].values
        tn = time_norm[idx]

        sc = ax.scatter(x, y, c=tn, cmap='coolwarm_r', s=12, alpha=0.5)
        plt.colorbar(sc, ax=ax, label='早 → 近', fraction=0.04, pad=0.02)

        z = np.polyfit(x, y, 1)
        xr = np.linspace(x.min(), x.max(), 200)
        ax.plot(xr, np.poly1d(z)(xr), 'k--', lw=1.5)

        r = np.corrcoef(x, y)[0, 1]
        ax.set_xlabel(f'{current_col} (A)', fontsize=9)
        ax.set_ylabel(ycol, fontsize=9)
        ax.set_title(f'{current_col} vs {ycol}\nPearson r = {r:.3f}  (n={len(valid)})',
                     fontsize=10)
        ax.grid(True, alpha=0.3, linestyle='--')

    fig.suptitle(f"{device_id}  —  電流 vs 振動散佈圖（藍=早期  紅=近期）",
                 fontsize=12, y=1.01)
    plt.tight_layout()
    return _fig_to_b64(fig)


# ── HTML 組裝 ────────────────────────────────────────────────

def _stats_table_html(df: pd.DataFrame, cols: list[str]) -> str:
    num_cols = [c for c in cols if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
    if not num_cols:
        return ''
    stat = df[num_cols].describe().T[['count', 'mean', 'std', 'min', '50%', 'max']]
    stat.columns = ['筆數', '平均', '標準差', '最小', '中位數', '最大']
    stat = stat.round(4)

    rows_html = ''
    for col, row in stat.iterrows():
        rows_html += '<tr>' + f'<td><b>{col}</b></td>'
        rows_html += ''.join(f'<td>{v}</td>' for v in row.values) + '</tr>\n'

    return f"""
<table class="stats">
  <thead>
    <tr><th>欄位</th><th>筆數</th><th>平均</th><th>標準差</th>
        <th>最小</th><th>中位數</th><th>最大</th></tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>"""


def _build_html(device_id: str, df: pd.DataFrame,
                img_ts: str, img_sc: str | None,
                current_col: str | None,
                threshold: float) -> str:
    date_range = (f"{df['datetime'].min().strftime('%Y-%m-%d')} ~ "
                  f"{df['datetime'].max().strftime('%Y-%m-%d')}")

    display_cols = [c for c in
                    ['Total_vRMS', 'accOA', 'Crest_Factor', 'health_score', current_col]
                    if c and c in df.columns]

    stats_html = _stats_table_html(df, display_cols)
    scatter_section = ''
    if img_sc:
        scatter_section = f"""
<h2>電流 × 振動散佈圖</h2>
<img src="data:image/png;base64,{img_sc}" style="max-width:100%">"""

    filter_note = (f'電流 > {threshold} A（有電流資料）'
                   if current_col else f'Total vRMS > {settings.VTRMS_ON_THRESHOLD} mm/s')

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{device_id} — 振動電流報告</title>
<style>
  body {{font-family:Arial,sans-serif;margin:24px 32px;color:#222;background:#f9f9f9}}
  h1   {{color:#1a3c6b;border-bottom:2px solid #1a3c6b;padding-bottom:6px}}
  h2   {{color:#2c5f9e;margin-top:32px}}
  .meta{{background:#fff;border:1px solid #ddd;border-radius:6px;
         padding:14px 20px;margin-bottom:20px;display:inline-block}}
  .meta td{{padding:3px 16px 3px 0;font-size:0.93em}}
  .meta th{{color:#555;font-weight:normal;text-align:left}}
  table.stats{{border-collapse:collapse;width:100%;margin-top:8px;
               background:#fff;border-radius:4px;overflow:hidden;
               box-shadow:0 1px 3px rgba(0,0,0,.12)}}
  table.stats th,table.stats td{{border:1px solid #e0e0e0;padding:6px 10px;
                                 font-size:0.88em;text-align:right}}
  table.stats th{{background:#e8eef6;color:#333;text-align:center}}
  table.stats td:first-child{{text-align:left}}
  img{{border:1px solid #ddd;border-radius:4px;box-shadow:0 2px 6px rgba(0,0,0,.1)}}
  footer{{margin-top:40px;font-size:0.8em;color:#888}}
</style>
</head>
<body>
<h1>{device_id}  —  振動電流探索報告</h1>

<div class="meta">
<table>
  <tr><th>設備 ID</th><td>{device_id}</td>
      <th>資料期間</th><td>{date_range}</td></tr>
  <tr><th>有效筆數</th><td>{len(df):,}</td>
      <th>開機篩選條件</th><td>{filter_note}</td></tr>
  <tr><th>電流欄位</th><td>{current_col or '（無）'}</td>
      <th>時間對齊容差</th><td>{MERGE_TOL}</td></tr>
</table>
</div>

<h2>時序圖</h2>
<img src="data:image/png;base64,{img_ts}" style="max-width:100%">

{scatter_section}

<h2>統計摘要</h2>
{stats_html}

<footer>產生時間：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp;
VFDEdgeHealthModel export_vibcurrent.py</footer>
</body>
</html>"""


# ── 電流欄位辨識 ─────────────────────────────────────────────

def _resolve_current_col(scada_cols: list[str], current_type: str) -> str | None:
    """
    從 SCADA 欄位（= variable_type 值）中找出電流欄位。
    依序嘗試：1) 完全一致  2) 包含 current_type  3) 包含「電流/current/安培」。
    回傳找到的欄位名稱，找不到回傳 None。
    """
    if not scada_cols:
        return None
    # 1) 完全一致
    if current_type in scada_cols:
        return current_type
    # 2) 包含使用者指定的關鍵字（容許貼整段 description）
    ct = current_type.lower()
    for c in scada_cols:
        if ct in c.lower():
            return c
    # 3) 常見電流關鍵字
    for kw in ('電流', 'current', '安培', 'amp'):
        for c in scada_cols:
            if kw.lower() in c.lower():
                return c
    return None


# ── 單設備匯出 ───────────────────────────────────────────────

def export_device(device_id: str,
                  df_vib_raw: pd.DataFrame,
                  df_other: pd.DataFrame,
                  tag_map: pd.DataFrame,
                  output_dir: str,
                  current_type: str,
                  threshold: float) -> bool:
    logger.info(f"\n{'='*55}")
    logger.info(f"  {device_id}")
    logger.info(f"{'='*55}")

    # ── 振動：計算衍生欄位 + 過濾突波 ──（不過濾停機，由電流門檻決定）
    df_vib = compute_derived(df_vib_raw)
    df_vib = filter_spikes(df_vib)
    if df_vib.empty:
        logger.warning(f"{device_id}: 振動清洗後無有效資料，跳過")
        return False
    logger.info(f"  振動（突波過濾後）：{len(df_vib)} 筆")

    # ── 找設備電流 tag ──
    device_base = df_vib_raw['devicename'].iloc[0]
    dev_tags    = tag_map[tag_map['device_id'] == device_base]

    current_col = None
    if not dev_tags.empty:
        tagnames  = dev_tags['tagname'].tolist()
        df_dev    = df_other[df_other['tagname'].isin(tagnames)]

        if df_dev.empty:
            logger.warning(f"{device_id}: Other_Data 中找不到此設備的 tagname。"
                           f"tag_mapping 列出：{tagnames}")
        else:
            df_wide = pivot_scada(df_dev, dev_tags)
            scada_cols = [c for c in df_wide.columns if c != 'datetime']
            logger.info(f"  SCADA 欄位（variable_type）：{scada_cols}")

            # 時間重疊確認
            vib_range   = (df_vib['datetime'].min(), df_vib['datetime'].max())
            scada_range = (df_wide['datetime'].min(), df_wide['datetime'].max())
            if vib_range[0] <= scada_range[1] and scada_range[0] <= vib_range[1]:
                df_vib = merge_vib_scada(df_vib, df_wide)
                current_col = _resolve_current_col(scada_cols, current_type)
                if current_col is None:
                    logger.warning(f"  找不到電流欄位（指定 current_type='{current_type}'）；"
                                   f"現有 variable_type：{scada_cols}。"
                                   f"請用 --current-type 指定正確名稱")
                elif current_col != current_type:
                    logger.info(f"  電流欄位以部分比對找到：'{current_col}'")
            else:
                logger.warning(f"{device_id}: 振動 {vib_range[0].date()}~{vib_range[1].date()} "
                               f"與 SCADA {scada_range[0].date()}~{scada_range[1].date()} "
                               f"無時間重疊，跳過 SCADA 對齊")
    else:
        logger.warning(f"{device_id}: tag_mapping 中找不到 device_id='{device_base}'。"
                       f"請確認 tag_mapping 的 device_id 值與振動 devicename 一致")

    # ── 開機篩選 ──
    if current_col and current_col in df_vib.columns and df_vib[current_col].notna().any():
        mask = df_vib[current_col] > threshold
        logger.info(f"  開機篩選（{current_col} > {threshold} A）："
                    f"{mask.sum()}/{len(df_vib)} 筆通過")
        df_vib = df_vib[mask].reset_index(drop=True)
    else:
        mask = df_vib['Total_vRMS'] > settings.VTRMS_ON_THRESHOLD
        logger.info(f"  開機篩選（Total_vRMS > {settings.VTRMS_ON_THRESHOLD}）："
                    f"{mask.sum()}/{len(df_vib)} 筆通過")
        df_vib = df_vib[mask].reset_index(drop=True)

    if df_vib.empty:
        logger.warning(f"{device_id}: 開機篩選後無資料，跳過")
        return False

    # ── CSV 匯出 ──
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"{device_id}.csv")
    export_cols = ['datetime'] + [c for c in df_vib.columns if c != 'datetime']
    df_vib[export_cols].to_csv(csv_path, index=False)
    logger.info(f"  CSV → {csv_path}  ({len(df_vib)} 筆)")

    # ── 圖表 ──
    img_ts = _plot_timeseries(df_vib, device_id, current_col)
    img_sc = _plot_scatter(df_vib, device_id, current_col)

    # ── HTML 匯出 ──
    html = _build_html(device_id, df_vib, img_ts, img_sc, current_col, threshold)
    html_path = os.path.join(output_dir, f"{device_id}.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    logger.info(f"  HTML → {html_path}")

    return True


# ── 入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device',       default=None,
                        help='只匯出指定 device_id，如 ZP1_2_M1')
    parser.add_argument('--threshold',    type=float,
                        default=settings.CURRENT_ON_THRESHOLD,
                        help=f'電流開機下限（A），預設 {settings.CURRENT_ON_THRESHOLD}')
    parser.add_argument('--current-type', default=DEFAULT_CURRENT_TYPE,
                        help=f'tag_mapping 中電流的 variable_type，預設「{DEFAULT_CURRENT_TYPE}」')
    args = parser.parse_args()

    vib_data = load_vibration('Vibration_Data')
    if not vib_data:
        logger.error("找不到振動資料，請確認 Vibration_Data/ 資料夾")
        sys.exit(1)

    df_other = load_other_data(OTHER_DATA_DIR)
    tag_map  = load_tag_mapping(TAG_MAPPING_PATH)

    if df_other.empty:
        logger.warning("Other_Data/ 無資料，將只匯出振動欄位（無電流）")
    if tag_map.empty:
        logger.warning("tag_mapping.csv 讀取失敗，將只匯出振動欄位（無電流）")

    success = 0
    for device_id, df_raw in sorted(vib_data.items()):
        if args.device and device_id != args.device:
            continue
        ok = export_device(
            device_id, df_raw, df_other, tag_map,
            OUTPUT_DIR, args.current_type, args.threshold,
        )
        if ok:
            success += 1

    print(f"\n  完成：{success} 台設備  →  {OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
