"""
analyze_specific_power.py — 空壓機比功率（節電效益）分析

讀取 Other_Data/ 的電流、用電量（累積）、流量（累積）、運轉時數（累積）資料，
篩出「運轉狀態」區間，逐日加總計算比功率（流量 ÷ 用電量），
比對 maintenance_log.csv 的保養日期，輸出保養前後比較報告。

前置條件：
  - Other_Data/        ← SCADA 資料（3 欄：datetime / tagname / value），可分散在多個 CSV
  - tag_mapping.csv    ← tagname 對應定義，需含 variable_type：電流／用電量／流量／運轉時數
  - maintenance_log.csv（選用）← 保養日期

執行方式：
    python analyze_specific_power.py                          # 全部設備
    python analyze_specific_power.py --device K21_B2F_空壓     # 指定設備
    python analyze_specific_power.py --price 3.5               # 電價（元/度），估算節費金額

輸出：
    output/specific_power/{device_id}_daily.csv    — 每日比功率明細
    output/specific_power/{device_id}_gaps.csv      — 資料缺漏時段
    output/specific_power/{device_id}_report.html   — 報告（時序圖 + 保養前後比較表）
"""

import os
import sys
import argparse
import logging
import warnings
import io
import base64
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data_loader import safe_read_csv
from src.scada_loader import (load_other_data, load_tag_mapping, pivot_scada,
                               diff_cumulative, detect_data_gaps)
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger('analyze_specific_power')

warnings.filterwarnings('ignore', message='Glyph.*missing', category=UserWarning)

OTHER_DATA_DIR    = 'Other_Data'
TAG_MAPPING_PATH  = 'tag_mapping.csv'
MAINTENANCE_PATH  = 'maintenance_log.csv'
OUTPUT_DIR        = 'output/specific_power'

# variable_type 關鍵字（不分大小寫比對 tag_mapping 的 variable_type / pivot 後欄名）
CURRENT_KEYWORDS = ('電流', 'current', '安培', 'amp')
KWH_KEYWORDS     = ('用電量', '度數', 'kwh', 'energy', '電能')
FLOW_KEYWORDS    = ('流量', 'flow', 'nm3', 'nm³', 'cmm')
RUNHR_KEYWORDS   = ('運轉時數', '運轉小時', 'runhour', 'runtime', 'run_hour', 'runhr')

RUN_DELTA_EPS = 1e-6   # 判斷 d_運轉時數 > 0 的浮點誤差容忍值

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


# ── 保養紀錄載入（獨立複製，避免依賴 export_vibcurrent.py）────

def load_maintenance_log(path: str) -> dict:
    """讀取 maintenance_log.csv → {device_id: [(Timestamp, event_name), ...]}（依日期排序）。"""
    if not os.path.exists(path):
        logger.info(f"無保養紀錄檔 '{path}'，跳過保養標記")
        return {}
    try:
        df = safe_read_csv(path)
    except Exception as e:
        logger.warning(f"保養紀錄讀取失敗：{e}")
        return {}

    df.columns = [c.strip().lower() for c in df.columns]
    dev_col  = next((c for c in df.columns if c in ('device_id', 'device', 'deviceid')), None)
    date_col = next((c for c in df.columns if 'date' in c or c in ('日期', '保養日期')), None)
    name_col = next((c for c in df.columns if 'event' in c or 'name' in c or '項目' in c), None)
    if dev_col is None or date_col is None:
        logger.warning(f"保養紀錄缺少 device_id 或 date 欄；現有欄位：{list(df.columns)}")
        return {}

    log: dict = {}
    for _, row in df.iterrows():
        dev = str(row[dev_col]).strip()
        dt  = pd.to_datetime(str(row[date_col]).replace('/', '-'), errors='coerce')
        if pd.isna(dt):
            continue
        name = str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else '保養'
        log.setdefault(dev, []).append((dt, name))

    for dev in log:
        log[dev].sort(key=lambda t: t[0])
    return log


# ── 欄位辨識 ─────────────────────────────────────────────────

def _resolve_col(cols: list, keywords: tuple) -> str | None:
    """依關鍵字清單找第一個符合的欄位（不分大小寫）。"""
    for c in cols:
        if any(kw.lower() in c.lower() for kw in keywords):
            return c
    return None


# ── 每日比功率計算 ───────────────────────────────────────────

def compute_daily_specific_power(df_wide: pd.DataFrame,
                                 current_col: str | None,
                                 kwh_col: str,
                                 flow_col: str,
                                 runhr_col: str | None) -> tuple[pd.DataFrame, dict]:
    """
    篩出運轉狀態區間，逐日加總 ΔFlow / ΔkWh，計算每日比功率。

    運轉狀態判斷優先序：
      1) 有運轉時數欄位 → d_運轉時數 > 0
      2) 無運轉時數欄位 → 電流 > settings.CURRENT_ON_THRESHOLD

    Returns:
        (daily_df, diag) — daily_df 欄位：date, flow_sum, kwh_sum,
                            specific_power, running_hours
                            diag：診斷資訊字典（運轉判斷方式、電流交叉驗證等）
    """
    diag: dict = {}

    cum_cols = [kwh_col, flow_col] + ([runhr_col] if runhr_col else [])
    df = diff_cumulative(df_wide, cum_cols)

    if runhr_col:
        mask_run = df[f'd_{runhr_col}'] > RUN_DELTA_EPS
        diag['run_detect_method'] = f'運轉時數（d_{runhr_col} > 0）'
        if current_col and current_col in df.columns:
            mask_curr = df[current_col] > settings.CURRENT_ON_THRESHOLD
            both_valid = mask_run.notna() & mask_curr.notna()
            mismatch = (mask_run.fillna(False) != mask_curr.fillna(False)) & both_valid
            diag['run_current_mismatch_pct'] = (
                round(100 * mismatch.sum() / both_valid.sum(), 1) if both_valid.sum() else None
            )
    elif current_col and current_col in df.columns:
        mask_run = df[current_col] > settings.CURRENT_ON_THRESHOLD
        diag['run_detect_method'] = f'電流（{current_col} > {settings.CURRENT_ON_THRESHOLD} A，無運轉時數欄位可交叉驗證）'
    else:
        raise ValueError("compute_daily_specific_power: 無運轉時數也無電流欄位，無法判斷運轉狀態")

    df['_running'] = mask_run.fillna(False)
    diag['running_rows'] = int(df['_running'].sum())
    diag['total_rows'] = len(df)

    df_run = df[df['_running']].copy()
    if df_run.empty:
        raise ValueError("compute_daily_specific_power: 運轉狀態篩選後無資料")

    df_run['date'] = df_run['datetime'].dt.date
    agg = {f'd_{flow_col}': 'sum', f'd_{kwh_col}': 'sum'}
    if runhr_col:
        agg[f'd_{runhr_col}'] = 'sum'
    daily = df_run.groupby('date').agg(agg).reset_index()

    rename = {f'd_{flow_col}': 'flow_sum', f'd_{kwh_col}': 'kwh_sum'}
    if runhr_col:
        rename[f'd_{runhr_col}'] = 'running_hours'
    daily = daily.rename(columns=rename)
    if 'running_hours' not in daily.columns:
        # 無運轉時數欄位時，用運轉筆數 × 取樣間隔粗估運轉時數（僅供參考）
        counts = df_run.groupby('date').size()
        daily['running_hours'] = daily['date'].map(counts) * np.nan  # 無法可靠估算，留空

    daily['specific_power'] = daily['flow_sum'] / daily['kwh_sum'].replace(0, np.nan)
    daily['datetime'] = pd.to_datetime(daily['date'])
    return daily.sort_values('datetime').reset_index(drop=True), diag


# ── 圖表 ────────────────────────────────────────────────────

def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        fig.savefig(buf, format='png', dpi=130, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode('utf-8')


def _mark_maintenance(ax, maint_events: list) -> None:
    for dt, name in maint_events:
        ax.axvline(dt, color='crimson', linestyle='--', linewidth=1.0, alpha=0.8, zorder=5)
        ax.text(dt, 1.01, name, transform=ax.get_xaxis_transform(),
                rotation=90, va='bottom', ha='center', fontsize=7, color='crimson')


def plot_timeseries(daily: pd.DataFrame, device_id: str, maint_events: list) -> str:
    _setup_font()
    has_runhr = daily['running_hours'].notna().any()
    nrows = 2 if has_runhr else 1
    fig, axes = plt.subplots(nrows, 1, figsize=(13, 3.6 * nrows), sharex=True)
    axes = list(np.atleast_1d(axes))

    ax1 = axes[0]
    ax1.plot(daily['datetime'], daily['specific_power'], color='seagreen',
              marker='o', markersize=3, lw=1.1, label='比功率')
    ax1.set_ylabel('比功率（流量/度）', color='seagreen')
    ax1.tick_params(axis='y', labelcolor='seagreen')
    ax1.grid(True, alpha=0.3)
    ax1.set_title(f"{device_id}  —  每日比功率", fontsize=11)
    _mark_maintenance(ax1, maint_events)

    if has_runhr:
        ax2 = axes[1]
        ax2.bar(daily['datetime'], daily['running_hours'], color='steelblue', alpha=0.6, width=0.8)
        ax2.set_ylabel('每日運轉時數 (hr)', color='steelblue')
        ax2.tick_params(axis='y', labelcolor='steelblue')
        ax2.grid(True, alpha=0.3)
        ax2.set_title(f"{device_id}  —  每日運轉時數（confound 檢查）", fontsize=11)
        _mark_maintenance(ax2, maint_events)

    fmt = mdates.DateFormatter('%Y/%m/%d')
    axes[-1].xaxis.set_major_formatter(fmt)
    plt.setp(axes[-1].xaxis.get_majorticklabels(), rotation=30, ha='right')
    plt.tight_layout()
    return _fig_to_b64(fig)


# ── 保養前後比較 ─────────────────────────────────────────────

def maintenance_comparison(daily: pd.DataFrame, split_date: pd.Timestamp,
                           price: float | None) -> dict:
    pre  = daily[daily['datetime'] < split_date]
    post = daily[daily['datetime'] >= split_date]

    result = {
        'n_pre': len(pre), 'n_post': len(post),
        'sp_pre': float(pre['specific_power'].median()) if len(pre) else float('nan'),
        'sp_post': float(post['specific_power'].median()) if len(post) else float('nan'),
        'runhr_pre': float(pre['running_hours'].median()) if pre['running_hours'].notna().any() else float('nan'),
        'runhr_post': float(post['running_hours'].median()) if post['running_hours'].notna().any() else float('nan'),
    }
    sp_pre, sp_post = result['sp_pre'], result['sp_post']
    if not np.isnan(sp_pre) and sp_pre != 0 and not np.isnan(sp_post):
        result['improve_pct'] = (sp_post - sp_pre) / sp_pre * 100
    else:
        result['improve_pct'] = float('nan')

    if price is not None and len(post) and not np.isnan(sp_pre) and sp_pre > 0:
        # 若沒有保養、維持保養前比功率，產出保養後同樣總流量需要多少度電
        post_flow_total = post['flow_sum'].sum()
        hypothetical_kwh = post_flow_total / sp_pre
        actual_kwh = post['kwh_sum'].sum()
        kwh_saved = hypothetical_kwh - actual_kwh
        result['kwh_saved'] = float(kwh_saved)
        result['cost_saved'] = float(kwh_saved * price)
    return result


# ── 單設備分析 ───────────────────────────────────────────────

def analyze_device(device_id: str, df_other: pd.DataFrame, tag_map: pd.DataFrame,
                   maint_log: dict, output_dir: str, price: float | None) -> bool:
    logger.info(f"\n{'='*55}")
    logger.info(f"  {device_id}")
    logger.info(f"{'='*55}")

    dev_tags = tag_map[tag_map['device_id'] == device_id]
    if dev_tags.empty:
        logger.warning(f"{device_id}: tag_mapping 中找不到此 device_id")
        return False

    tagnames = dev_tags['tagname'].tolist()
    df_dev = df_other[df_other['tagname'].isin(tagnames)]
    if df_dev.empty:
        logger.warning(f"{device_id}: Other_Data 中找不到 tagname {tagnames}")
        return False

    wide = pivot_scada(df_dev, dev_tags)
    cols = [c for c in wide.columns if c != 'datetime']
    logger.info(f"  SCADA 欄位（variable_type）：{cols}")

    current_col = _resolve_col(cols, CURRENT_KEYWORDS)
    kwh_col     = _resolve_col(cols, KWH_KEYWORDS)
    flow_col    = _resolve_col(cols, FLOW_KEYWORDS)
    runhr_col   = _resolve_col(cols, RUNHR_KEYWORDS)

    logger.info(f"  電流={current_col} / 用電量={kwh_col} / 流量={flow_col} / 運轉時數={runhr_col}")

    if kwh_col is None or flow_col is None:
        logger.warning(f"{device_id}: 缺少用電量或流量欄位，無法計算比功率 "
                       f"(kwh_col={kwh_col}, flow_col={flow_col})")
        return False

    # ── 資料缺漏偵測 ──
    gaps = detect_data_gaps(wide)
    os.makedirs(output_dir, exist_ok=True)
    gaps_path = os.path.join(output_dir, f"{device_id}_gaps.csv")
    gaps.to_csv(gaps_path, index=False)
    if not gaps.empty:
        total_gap_hr = gaps['gap_hours'].sum()
        logger.warning(f"  ⚠ 偵測到 {len(gaps)} 段資料缺漏，合計 {total_gap_hr:.1f} 小時，"
                       f"明細 → {gaps_path}")
        top = gaps.head(3)
        for _, r in top.iterrows():
            logger.warning(f"    {r['gap_start']} ~ {r['gap_end']}（{r['gap_hours']:.1f} hr）")
    else:
        logger.info(f"  未偵測到明顯資料缺漏")

    # ── 每日比功率 ──
    try:
        daily, diag = compute_daily_specific_power(wide, current_col, kwh_col, flow_col, runhr_col)
    except ValueError as e:
        logger.warning(f"{device_id}: {e}")
        return False

    logger.info(f"  運轉狀態判斷：{diag.get('run_detect_method')}")
    if diag.get('run_current_mismatch_pct') is not None:
        logger.info(f"  運轉時數 vs 電流門檻 判斷不一致比例：{diag['run_current_mismatch_pct']}%"
                    f"（差異大時建議向資料源確認『運轉時數』是否含卸載時間）")
    logger.info(f"  運轉狀態資料：{diag['running_rows']}/{diag['total_rows']} 筆，"
                f"共 {len(daily)} 天有效資料")

    csv_path = os.path.join(output_dir, f"{device_id}_daily.csv")
    daily.to_csv(csv_path, index=False)
    logger.info(f"  每日比功率 CSV → {csv_path}")

    # ── 保養事件 ──
    maint_events = maint_log.get(device_id, [])
    dmin, dmax = daily['datetime'].min(), daily['datetime'].max()
    maint_events = [(dt, nm) for dt, nm in maint_events if dmin <= dt <= dmax]

    comparison_html = ''
    if maint_events:
        split_date = maint_events[-1][0]
        cmp = maintenance_comparison(daily, split_date, price)
        logger.info(f"  保養前 n={cmp['n_pre']} 比功率中位數={cmp['sp_pre']:.4f} | "
                    f"保養後 n={cmp['n_post']} 比功率中位數={cmp['sp_post']:.4f} | "
                    f"改善 {cmp['improve_pct']:.1f}%")
        if 'cost_saved' in cmp:
            logger.info(f"  估算節費：{cmp['kwh_saved']:.1f} 度 → {cmp['cost_saved']:.0f} 元")

        runhr_note = ''
        if not np.isnan(cmp['runhr_pre']) and not np.isnan(cmp['runhr_post']):
            runhr_diff = abs(cmp['runhr_pre'] - cmp['runhr_post'])
            runhr_note = (f"<p>每日運轉時數中位數：保養前 {cmp['runhr_pre']:.2f} hr / "
                          f"保養後 {cmp['runhr_post']:.2f} hr"
                          f"{' ⚠ 差異較大，比功率變化可能混雜用氣需求變化' if runhr_diff > 1 else '（相近，比功率變化較能歸因於保養）'}"
                          f"</p>")

        cost_note = ''
        if 'cost_saved' in cmp:
            cost_note = (f"<p><b>估算節費：{cmp['kwh_saved']:.1f} 度電 "
                        f"≈ {cmp['cost_saved']:.0f} 元</b>"
                        f"（以保養前比功率反推：維持原效率產出保養後同樣流量所需的度電 vs 實際用電量）</p>")

        color = '#157f3b' if cmp['improve_pct'] > 0 else '#c0392b'
        comparison_html = f"""
<h2>保養前後比功率比較</h2>
<table class="stats">
  <thead><tr><th></th><th>保養前</th><th>保養後</th><th>改善</th></tr></thead>
  <tbody>
    <tr><td>天數</td><td>{cmp['n_pre']}</td><td>{cmp['n_post']}</td><td>—</td></tr>
    <tr><td>比功率中位數</td><td>{cmp['sp_pre']:.4f}</td><td>{cmp['sp_post']:.4f}</td>
        <td style="color:{color}"><b>{cmp['improve_pct']:+.1f}%</b></td></tr>
  </tbody>
</table>
{runhr_note}
{cost_note}
<p style="font-size:0.85em;color:#666">比功率 = 流量 ÷ 用電量（僅計運轉狀態區間），數字越高代表效率越好。</p>"""
    else:
        comparison_html = '<p style="color:#888">（此設備於資料期間內無保養紀錄，無法比較）</p>'

    gaps_html = ''
    if not gaps.empty:
        rows = ''.join(
            f"<tr><td>{r['gap_start']}</td><td>{r['gap_end']}</td><td>{r['gap_hours']:.1f}</td></tr>"
            for _, r in gaps.head(20).iterrows()
        )
        gaps_html = f"""
<h2>資料缺漏時段</h2>
<table class="stats">
  <thead><tr><th>起</th><th>迄</th><th>時長 (hr)</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="font-size:0.85em;color:#666">共 {len(gaps)} 段，合計 {gaps['gap_hours'].sum():.1f} 小時。
缺漏時段內比功率無法計算，已自動排除，不影響其他日期的統計。</p>"""

    img_ts = plot_timeseries(daily, device_id, maint_events)
    html = _build_html(device_id, daily, img_ts, comparison_html, gaps_html)
    html_path = os.path.join(output_dir, f"{device_id}_report.html")
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html)
    logger.info(f"  HTML → {html_path}")
    return True


def _build_html(device_id: str, daily: pd.DataFrame, img_ts: str,
                comparison_html: str, gaps_html: str) -> str:
    date_range = (f"{daily['datetime'].min().strftime('%Y-%m-%d')} ~ "
                 f"{daily['datetime'].max().strftime('%Y-%m-%d')}")
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{device_id} — 比功率報告</title>
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
<h1>{device_id}  —  比功率（節電效益）報告</h1>

<div class="meta">
<table>
  <tr><th>設備 ID</th><td>{device_id}</td>
      <th>資料期間</th><td>{date_range}</td></tr>
  <tr><th>有效天數</th><td>{len(daily)}</td>
      <th>比功率定義</th><td>流量 ÷ 用電量（僅運轉狀態區間）</td></tr>
</table>
</div>

<h2>每日比功率時序</h2>
<img src="data:image/png;base64,{img_ts}" style="max-width:100%">

{comparison_html}

{gaps_html}

<footer>產生時間：{pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp;
VFDEdgeHealthModel analyze_specific_power.py</footer>
</body>
</html>"""


# ── 入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', default=None, help='只分析指定 device_id')
    parser.add_argument('--price', type=float, default=None, help='電價（元/度），估算節費金額')
    parser.add_argument('--other-data-dir', default=OTHER_DATA_DIR)
    parser.add_argument('--tag-mapping', default=TAG_MAPPING_PATH)
    parser.add_argument('--maintenance', default=MAINTENANCE_PATH)
    args = parser.parse_args()

    df_other = load_other_data(args.other_data_dir)
    tag_map  = load_tag_mapping(args.tag_mapping)
    maint_log = load_maintenance_log(args.maintenance)

    if df_other.empty:
        logger.error(f"'{args.other_data_dir}/' 無資料")
        sys.exit(1)
    if tag_map.empty:
        logger.error(f"'{args.tag_mapping}' 讀取失敗")
        sys.exit(1)

    device_ids = sorted(tag_map['device_id'].unique())
    if args.device:
        device_ids = [d for d in device_ids if d == args.device]
        if not device_ids:
            logger.error(f"tag_mapping 中找不到 device_id='{args.device}'")
            sys.exit(1)

    success = 0
    for device_id in device_ids:
        if analyze_device(device_id, df_other, tag_map, maint_log, OUTPUT_DIR, args.price):
            success += 1

    print(f"\n  完成：{success}/{len(device_ids)} 台設備  →  {OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
