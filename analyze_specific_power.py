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
from src.scada_loader import (load_other_data, load_tag_mapping,
                               daily_sum_by_tag, detect_data_gaps)
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


# ── 檔案寫入保護（Windows 常見：目標檔案被 Excel 等程式開著鎖住）────

def _safe_write_csv(df: pd.DataFrame, path: str) -> bool:
    try:
        df.to_csv(path, index=False)
        return True
    except PermissionError:
        logger.error(f"  ✗ 無法寫入 {path}（檔案可能被其他程式開著，例如 Excel）。"
                    f"請關閉該檔案後重跑，已跳過此檔案。")
        return False


def _safe_write_text(text: str, path: str) -> bool:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return True
    except PermissionError:
        logger.error(f"  ✗ 無法寫入 {path}（檔案可能被其他程式開著，例如瀏覽器）。"
                    f"請關閉該檔案後重跑，已跳過此檔案。")
        return False


# ── tagname 對應 ─────────────────────────────────────────────

def _tagname_for(dev_tags: pd.DataFrame, variable_type: str) -> str | None:
    """依 variable_type 找對應的 tagname；同一 variable_type 對應多個 tagname 時取第一個並警告。"""
    names = dev_tags.loc[dev_tags['variable_type'] == variable_type, 'tagname'].tolist()
    if len(names) > 1:
        logger.warning(f"  variable_type={variable_type!r} 對應多個 tagname {names}，"
                       f"僅使用第一個：{names[0]!r}")
    return names[0] if names else None


# ── 每日比功率計算 ───────────────────────────────────────────

def compute_daily_specific_power(df_dev: pd.DataFrame,
                                 kwh_tag: str,
                                 flow_tag: str,
                                 runhr_tag: str | None) -> tuple[pd.DataFrame, dict]:
    """
    每個 tag 各自在自己的原始時間軸上差分＋依日期加總，最後才合併成每日比功率。

    不做跨 tag 逐筆對齊（不 pivot 成寬表再逐列篩選），因為真實 SCADA 歷史
    資料庫裡不同 tag 通常各自獨立的時間戳，逐列對齊會導致大量欄位互相 NaN。

    比功率 = 當日總流量增量 ÷ 當日總用電量增量。
    注意：此處用電量為「全天用電」（含待機/卸載功耗），未依運轉狀態濾除
    ——因為若要濾除待機用電，需要用電量與運轉時數逐筆對齊，而這正是上述
    時間戳不同步問題無法可靠做到的部分。運轉時數改為輔助對照欄位，
    保養前後若運轉時數差異很大，代表比功率變化可能混雜用氣需求變化。

    Returns:
        (daily_df, diag) — daily_df 欄位：date, datetime, flow_sum, kwh_sum,
                            specific_power, running_hours
                            diag：診斷資訊字典
    """
    diag: dict = {}

    daily_kwh  = daily_sum_by_tag(df_dev, kwh_tag)
    daily_flow = daily_sum_by_tag(df_dev, flow_tag)
    daily_runhr = daily_sum_by_tag(df_dev, runhr_tag) if runhr_tag else None

    if daily_kwh.empty or daily_flow.empty:
        raise ValueError("compute_daily_specific_power: 用電量或流量無有效資料")

    idx = daily_kwh.index.union(daily_flow.index)
    if daily_runhr is not None:
        idx = idx.union(daily_runhr.index)
    idx = sorted(idx)

    daily = pd.DataFrame({'date': idx})
    daily['kwh_sum']  = daily['date'].map(daily_kwh)
    daily['flow_sum'] = daily['date'].map(daily_flow)
    daily['running_hours'] = daily['date'].map(daily_runhr) if daily_runhr is not None else np.nan
    daily['specific_power'] = daily['flow_sum'] / daily['kwh_sum'].replace(0, np.nan)
    daily['datetime'] = pd.to_datetime(daily['date'])
    daily = daily.sort_values('datetime').reset_index(drop=True)

    diag['n_days']         = len(daily)
    diag['n_missing_kwh']  = int(daily['kwh_sum'].isna().sum())
    diag['n_missing_flow'] = int(daily['flow_sum'].isna().sum())
    return daily, diag


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

    var_types = dev_tags['variable_type'].unique().tolist()
    logger.info(f"  variable_type：{var_types}")

    current_type = _resolve_col(var_types, CURRENT_KEYWORDS)
    kwh_type     = _resolve_col(var_types, KWH_KEYWORDS)
    flow_type    = _resolve_col(var_types, FLOW_KEYWORDS)
    runhr_type   = _resolve_col(var_types, RUNHR_KEYWORDS)

    logger.info(f"  電流={current_type} / 用電量={kwh_type} / 流量={flow_type} / 運轉時數={runhr_type}")

    if kwh_type is None or flow_type is None:
        logger.warning(f"{device_id}: 缺少用電量或流量欄位，無法計算比功率 "
                       f"(kwh={kwh_type}, flow={flow_type})")
        return False

    kwh_tag   = _tagname_for(dev_tags, kwh_type)
    flow_tag  = _tagname_for(dev_tags, flow_type)
    runhr_tag = _tagname_for(dev_tags, runhr_type) if runhr_type else None

    # ── 資料缺漏偵測（各 tag 各自的時間軸分開偵測，避免掩蓋單一 tag 斷線）──
    os.makedirs(output_dir, exist_ok=True)
    gap_frames = []
    for label, tag in [('用電量', kwh_tag), ('流量', flow_tag)] + (
        [('運轉時數', runhr_tag)] if runhr_tag else []
    ):
        sub = df_dev[df_dev['tagname'] == tag][['datetime']]
        g = detect_data_gaps(sub)
        if not g.empty:
            g.insert(0, 'tag', label)
            gap_frames.append(g)
    gaps = (pd.concat(gap_frames, ignore_index=True)
           if gap_frames else pd.DataFrame(columns=['tag', 'gap_start', 'gap_end', 'gap_hours']))

    gaps_path = os.path.join(output_dir, f"{device_id}_gaps.csv")
    _safe_write_csv(gaps, gaps_path)
    if not gaps.empty:
        total_gap_hr = gaps['gap_hours'].sum()
        logger.warning(f"  ⚠ 偵測到 {len(gaps)} 段資料缺漏，合計 {total_gap_hr:.1f} 小時，"
                       f"明細 → {gaps_path}")
        for _, r in gaps.head(3).iterrows():
            logger.warning(f"    [{r['tag']}] {r['gap_start']} ~ {r['gap_end']}（{r['gap_hours']:.1f} hr）")
    else:
        logger.info(f"  未偵測到明顯資料缺漏")

    # ── 每日比功率 ──
    try:
        daily, diag = compute_daily_specific_power(df_dev, kwh_tag, flow_tag, runhr_tag)
    except ValueError as e:
        logger.warning(f"{device_id}: {e}")
        return False

    if diag['n_missing_kwh'] or diag['n_missing_flow']:
        logger.warning(f"  ⚠ {diag['n_days']} 天中，有 {diag['n_missing_kwh']} 天無用電量資料、"
                       f"{diag['n_missing_flow']} 天無流量資料（比功率為 NaN，已從比較中排除）")
    logger.info(f"  共 {diag['n_days']} 天資料（{daily['datetime'].min().date()} ~ "
                f"{daily['datetime'].max().date()}）")

    csv_path = os.path.join(output_dir, f"{device_id}_daily.csv")
    if _safe_write_csv(daily, csv_path):
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
<p style="font-size:0.85em;color:#666">比功率 = 流量 ÷ 用電量（含待機/卸載用電，未依運轉狀態濾除——
原始資料各 tag 時間戳未同步，無法可靠逐筆比對運轉狀態；
請搭配上方「每日運轉時數」判斷保養前後用氣型態是否一致），數字越高代表效率越好。</p>"""
    else:
        comparison_html = '<p style="color:#888">（此設備於資料期間內無保養紀錄，無法比較）</p>'

    gaps_html = ''
    if not gaps.empty:
        rows = ''.join(
            f"<tr><td>{r['tag']}</td><td>{r['gap_start']}</td><td>{r['gap_end']}</td><td>{r['gap_hours']:.1f}</td></tr>"
            for _, r in gaps.head(20).iterrows()
        )
        gaps_html = f"""
<h2>資料缺漏時段</h2>
<table class="stats">
  <thead><tr><th>欄位</th><th>起</th><th>迄</th><th>時長 (hr)</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
<p style="font-size:0.85em;color:#666">共 {len(gaps)} 段，合計 {gaps['gap_hours'].sum():.1f} 小時，
各欄位（用電量／流量／運轉時數）分開偵測，因為各 tag 斷線時間不一定相同。
缺漏當天若導致該日總量無法計算，比功率會是 NaN，已自動排除，不影響其他日期的統計。</p>"""

    img_ts = plot_timeseries(daily, device_id, maint_events)
    html = _build_html(device_id, daily, img_ts, comparison_html, gaps_html)
    html_path = os.path.join(output_dir, f"{device_id}_report.html")
    if _safe_write_text(html, html_path):
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
      <th>比功率定義</th><td>流量 ÷ 用電量（全天，含待機用電）</td></tr>
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
