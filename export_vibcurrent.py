"""
export_vibcurrent.py — 振動 + 電流 資料匯出（CSV + 靜態 HTML）

讀取振動資料（Vibration_Data/）與 SCADA 電流資料（Other_Data/），
對齊時間軸後過濾停機狀態，每台設備輸出：
  output/export/{device_id}.csv   — 對齊後的完整數值表
  output/export/{device_id}.html  — 靜態報告（時序圖 + 散佈圖 + 統計表）

若 output/scores/{device_id}_scores.csv 存在，會一併納入 health_score；
若 maintenance_log.csv 存在，會標示保養日期並做「相同負載下保養前後比較」。

執行方式：
    python export_vibcurrent.py                      # 全部設備
    python export_vibcurrent.py --device ZP1_2_M1    # 指定設備
    python export_vibcurrent.py --threshold 5.0      # 自訂電流下限（A）
    python export_vibcurrent.py --current-type 電流  # tag_mapping variable_type 名稱
    python export_vibcurrent.py --maintenance x.csv  # 自訂保養紀錄路徑

maintenance_log.csv 格式（device_id 用基底名，不含 M1/M2）：
    device_id,date,event_name
    ZP1_2,2026-03-15,換油
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

from src.data_loader import load_vibration, safe_read_csv
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
MAINTENANCE_PATH = 'maintenance_log.csv'
SCORE_DIR        = 'output/scores'
OUTPUT_DIR       = 'output/export'
DEFAULT_CURRENT_TYPE = '電流'

# 保養前後比較的分組基準欄（優先序）：頻率 > 功率 > 電流
# 頻率最能確保「相同操作點」下的比較
FREQ_COL_KEYWORDS    = ('頻率', 'freq', 'hz', 'frequency')
LOAD_COL_KEYWORDS    = ('輸入功率', 'power', '功率')
CURRENT_COL_KEYWORDS = ('電流', 'current', '安培', 'amp')

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


# ── 保養紀錄 / 健康分數載入 ──────────────────────────────────

def load_maintenance_log(path: str) -> dict:
    """
    讀取 maintenance_log.csv。
    欄位：device_id（基底名，如 ZP1_2）/ date / event_name（選填）
    回傳：{device_base: [(Timestamp, event_name), ...]}（依日期排序）
    """
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
    logger.info(f"保養紀錄：{len(log)} 台設備，共 {sum(len(v) for v in log.values())} 筆")
    return log


def load_health_score(device_id: str, score_dir: str = SCORE_DIR) -> pd.DataFrame | None:
    """
    讀取 output/scores/{device_id}_scores.csv 的 health_score。
    回傳 df[['datetime', 'health_score']]，不存在則回傳 None。
    """
    path = os.path.join(score_dir, f"{device_id}_scores.csv")
    if not os.path.exists(path):
        return None
    try:
        df = safe_read_csv(path)
    except Exception as e:
        logger.warning(f"{device_id}: health_score 讀取失敗 — {e}")
        return None
    if 'datetime' not in df.columns or 'health_score' not in df.columns:
        return None
    df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce')
    out = df[['datetime', 'health_score']].dropna(subset=['datetime'])
    return out if not out.empty else None


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

def _mark_maintenance(ax, maint_events: list) -> None:
    """在子圖畫保養垂直線（紅虛線）＋頂部標籤。"""
    if not maint_events:
        return
    for dt, name in maint_events:
        ax.axvline(dt, color='crimson', linestyle='--', linewidth=1.0, alpha=0.8, zorder=5)
        ax.text(dt, 1.01, name, transform=ax.get_xaxis_transform(),
                rotation=90, va='bottom', ha='center', fontsize=7, color='crimson')


def _plot_timeseries(df: pd.DataFrame, device_id: str,
                     current_col: str | None,
                     maint_events: list | None = None) -> str:
    """
    時序圖（最多 3 列）：
      ① Total_vRMS [+ accOA 次軸]
      ② 電流（若有）[+ 頻率 次軸（若有）]
      ③ health_score（若有）
    各列以紅虛線標示保養日期。
    """
    _setup_font()
    maint_events = maint_events or []
    has_current = bool(current_col and current_col in df.columns and df[current_col].notna().any())
    has_health  = bool('health_score' in df.columns and df['health_score'].notna().any())
    nrows = 1 + int(has_current) + int(has_health)

    fig, axes = plt.subplots(nrows, 1, figsize=(13, 3.6 * nrows), sharex=False)
    if nrows == 1:
        axes = [axes]
    axes = list(np.atleast_1d(axes))
    ai = 0

    # ── ① 振動 ──
    ax1 = axes[ai]; ai += 1
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
    _mark_maintenance(ax1, maint_events)
    _x_fmt(ax1, df['datetime'])

    # ── ② 電流 ──
    if has_current:
        ax2 = axes[ai]; ai += 1
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
        _mark_maintenance(ax2, maint_events)
        _x_fmt(ax2, df['datetime'])

    # ── ③ 健康分數 ──
    if has_health:
        ax3 = axes[ai]; ai += 1
        ax3.plot(df['datetime'], df['health_score'],
                 color='seagreen', lw=1.1, label='Health Score')
        ax3.axhline(85, color='gray', ls=':', lw=0.8, alpha=0.7)
        ax3.set_ylabel('Health Score', color='seagreen')
        ax3.set_ylim(0, 105)
        ax3.tick_params(axis='y', labelcolor='seagreen')
        ax3.grid(True, alpha=0.3)
        ax3.set_title(f"{device_id}  —  健康分數", fontsize=11)
        _mark_maintenance(ax3, maint_events)
        _x_fmt(ax3, df['datetime'])

    plt.tight_layout()
    return _fig_to_b64(fig)


def _plot_scatter(df: pd.DataFrame, device_id: str,
                  current_col: str | None,
                  split_date: pd.Timestamp | None = None) -> str | None:
    """
    散佈圖：頻率（若有）+ 電流 vs 振動（Total_vRMS / accOA）。
    有 split_date → 保養前(灰) vs 保養後(橘) 分群；無 → 時間著色。
    頻率列放在最上方，讓「相同操作點」的比較一目瞭然。
    """
    _setup_font()

    freq_col = _find_col_by_keywords(df, FREQ_COL_KEYWORDS)
    has_freq = bool(freq_col and df[freq_col].notna().sum() >= 10)
    has_curr = bool(current_col and current_col in df.columns
                    and df[current_col].notna().sum() >= 10)

    if not has_freq and not has_curr:
        return None

    vib_targets = [c for c in ['Total_vRMS', 'accOA']
                   if c in df.columns and df[c].notna().any()]
    if not vib_targets:
        return None

    # x 軸來源：頻率優先，若都有則兩列都畫
    x_sources = []
    if has_freq:
        x_sources.append((freq_col, 'Hz'))
    if has_curr:
        x_sources.append((current_col, 'A'))

    nrows = len(x_sources)
    ncols = len(vib_targets)
    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 4.5 * nrows), squeeze=False)
    use_split = split_date is not None

    if not use_split:
        t_min, t_max = df['datetime'].min(), df['datetime'].max()
        t_span = (t_max - t_min).total_seconds() or 1
        time_norm = ((df['datetime'] - t_min).dt.total_seconds() / t_span).values

    def _trend(ax, x, y, color):
        if len(x) < 2:
            return
        mask = np.isfinite(x) & np.isfinite(y)
        x, y = x[mask], y[mask]
        if len(x) < 2 or np.std(x) < 1e-10:
            return
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                z = np.polyfit(x, y, 1)
            xr = np.linspace(x.min(), x.max(), 200)
            ax.plot(xr, np.poly1d(z)(xr), color=color, lw=2, ls='--')
        except Exception:
            pass

    for ri, (xcol, xunit) in enumerate(x_sources):
        for ci, ycol in enumerate(vib_targets):
            ax = axes[ri][ci]
            valid = df[[xcol, ycol, 'datetime']].dropna()
            if len(valid) < 5:
                ax.set_visible(False)
                continue
            x = valid[xcol].values
            y = valid[ycol].values

            if use_split:
                pre  = valid['datetime'] < split_date
                post = ~pre
                ax.scatter(x[pre.values],  y[pre.values],
                           c='gray',       s=12, alpha=0.45, label=f'保養前 (n={pre.sum()})')
                ax.scatter(x[post.values], y[post.values],
                           c='darkorange', s=12, alpha=0.55, label=f'保養後 (n={post.sum()})')
                if pre.sum()  >= 2:
                    _trend(ax, x[pre.values],  y[pre.values],  'dimgray')
                if post.sum() >= 2:
                    _trend(ax, x[post.values], y[post.values], 'orangered')
                ax.legend(fontsize=8, loc='best')
                ax.set_title(f'{xcol} vs {ycol}', fontsize=10)
            else:
                tn = time_norm[valid.index]
                sc = ax.scatter(x, y, c=tn, cmap='coolwarm_r', s=12, alpha=0.5)
                plt.colorbar(sc, ax=ax, label='早 → 近', fraction=0.04, pad=0.02)
                _trend(ax, x, y, 'black')
                r = np.corrcoef(x, y)[0, 1]
                ax.set_title(f'{xcol} vs {ycol}\nPearson r = {r:.3f}  (n={len(valid)})',
                             fontsize=10)

            ax.set_xlabel(f'{xcol} ({xunit})', fontsize=9)
            ax.set_ylabel(ycol, fontsize=9)
            ax.grid(True, alpha=0.3, linestyle='--')

    sub = ('（灰=保養前  橘=保養後；相同電流下橘點較低 = 有效）' if use_split
           else '（藍=早期  紅=近期）')
    row_labels = ' / '.join(f'{xc}({xu})' for xc, xu in x_sources)
    fig.suptitle(f"{device_id}  —  {row_labels} vs 振動散佈圖 {sub}",
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


def _find_col_by_keywords(df: pd.DataFrame, keywords: tuple) -> str | None:
    """依關鍵字清單找第一個有足夠資料的欄位（不分大小寫）。"""
    for c in df.columns:
        if any(kw.lower() in c.lower() for kw in keywords):
            if df[c].notna().sum() >= 10:
                return c
    return None


def _pick_group_col(df: pd.DataFrame, current_col: str | None) -> str | None:
    """
    挑選保養分析的「分組基準欄」：頻率 > 功率 > 電流。
    頻率是 VFD 最直接的操作點指標，確保相同操作條件下的比較。
    """
    freq = _find_col_by_keywords(df, FREQ_COL_KEYWORDS)
    if freq:
        return freq
    load = _find_col_by_keywords(df, LOAD_COL_KEYWORDS)
    if load:
        return load
    return current_col


def _maintenance_effect_table(df: pd.DataFrame, group_col: str | None,
                              current_col: str | None,
                              split_date: pd.Timestamp, n_bins: int = 4) -> str:
    """
    相同操作點下的保養前後比較表。
    以 group_col（頻率優先 > 功率 > 電流）分箱，前後共用相同區間，
    比較各箱的 accOA、Total_vRMS、電流（確認負載一致）、health_score 中位數。
    """
    if group_col is None or group_col not in df.columns:
        return '<p style="color:#888">（無頻率/負載欄位，無法進行分組比較）</p>'

    data = df[df[group_col].notna()].copy()
    if len(data) < 20:
        return '<p style="color:#888">（資料量不足，無法分箱比較）</p>'

    is_freq = any(kw.lower() in group_col.lower() for kw in FREQ_COL_KEYWORDS)
    group_label = f'頻率（{group_col}）' if is_freq else group_col

    # 整體分箱 → 前後共用相同邊界（這是「相同操作點」的關鍵）
    # 策略 1：等樣本四分位
    binned = False
    bin_method = ''
    try:
        result = pd.qcut(data[group_col], q=n_bins, duplicates='drop')
        if result.notna().sum() >= 10:          # qcut 有時對常數序列產出全 NaN
            data['_bin'] = result
            binned = True
            bin_method = f'四分位（q={n_bins}）'
    except ValueError:
        pass

    # 策略 2（頻率專用）：round 到最近 5Hz 分組（適合固定幾個操作點的 VFD）
    if not binned and is_freq:
        step = 5.0
        data['_bin'] = (data[group_col] / step).round() * step
        n_unique = data['_bin'].nunique()
        if n_unique >= 2:
            binned = True
            bin_method = f'每 {step}Hz 固定分組（{n_unique} 組）'
        else:
            data['_bin'] = 0
            bin_method = f'頻率幾乎固定（{data[group_col].median():.1f} Hz），無法分組'

    # 策略 3：fallback 單一 bin（仍輸出整體比較）
    if not binned:
        data['_bin'] = 0
        bin_method = '無法分箱（全部視為同一組）'

    logger.info(f"  保養分析分箱方式：{bin_method}")

    data['_period'] = np.where(data['datetime'] < split_date, '前', '後')
    n_pre  = (data['_period'] == '前').sum()
    n_post = (data['_period'] == '後').sum()
    dmin, dmax = data['datetime'].min(), data['datetime'].max()
    logger.info(f"  保養比較：split={split_date.date()} | "
                f"資料範圍 {dmin.date()}~{dmax.date()} | "
                f"保養前 {n_pre} 筆 / 保養後 {n_post} 筆")
    if n_post == 0:
        return (f'<p style="color:#c0392b">⚠ 保養日期（{split_date.date()}）在資料結束之後，'
                f'資料最新至 {dmax.date()}，無保養後資料可比較。'
                f'請確認 maintenance_log.csv 的日期是否正確。</p>')

    has_rms  = 'Total_vRMS' in data.columns and data['Total_vRMS'].notna().any()
    has_curr = bool(current_col and current_col in data.columns
                    and data[current_col].notna().any())
    has_hs   = 'health_score' in data.columns and data['health_score'].notna().any()

    def _med(series) -> float:
        """dropna 後再算中位數，避免 Mean of empty slice RuntimeWarning。"""
        v = series.dropna()
        return float(v.median()) if not v.empty else float('nan')

    def _imp(pre_val, post_val):
        if not np.isnan(pre_val) and pre_val != 0:
            pct = (pre_val - post_val) / pre_val * 100
        else:
            pct = float('nan')
        return pct, ('↓' if pct > 0 else '↑'), ('#157f3b' if pct > 0 else '#c0392b')

    rows_html = ''
    for b in sorted(data['_bin'].unique()):
        sub = data[data['_bin'] == b]
        pre, post = sub[sub['_period'] == '前'], sub[sub['_period'] == '後']
        if pre.empty or post.empty:
            continue

        # Interval 型態顯示區間，數值型態（round分組）直接顯示
        if hasattr(b, 'left'):
            bin_label = f'{b.left:.1f} ~ {b.right:.1f}'
        else:
            bin_label = f'{b:.1f} Hz' if is_freq else f'{b:.1f}'
        rows_html += f'<tr><td>{bin_label}</td><td>{len(pre)}</td><td>{len(post)}</td>'

        # accOA
        acc_pre, acc_post = _med(pre['accOA']), _med(post['accOA'])
        pct, arrow, color = _imp(acc_pre, acc_post)
        acc_pre_s  = f'{acc_pre:.4f}'  if not np.isnan(acc_pre)  else '—'
        acc_post_s = f'{acc_post:.4f}' if not np.isnan(acc_post) else '—'
        pct_s = f'{arrow}{abs(pct):.1f}%' if not np.isnan(pct) else '—'
        rows_html += (f'<td>{acc_pre_s}</td><td>{acc_post_s}</td>'
                      f'<td style="color:{color}"><b>{pct_s}</b></td>')

        # Total_vRMS
        if has_rms:
            rms_pre, rms_post = _med(pre['Total_vRMS']), _med(post['Total_vRMS'])
            pct_r, arrow_r, color_r = _imp(rms_pre, rms_post)
            rms_pre_s  = f'{rms_pre:.4f}'  if not np.isnan(rms_pre)  else '—'
            rms_post_s = f'{rms_post:.4f}' if not np.isnan(rms_post) else '—'
            pct_r_s = f'{arrow_r}{abs(pct_r):.1f}%' if not np.isnan(pct_r) else '—'
            rows_html += (f'<td>{rms_pre_s}</td><td>{rms_post_s}</td>'
                          f'<td style="color:{color_r}"><b>{pct_r_s}</b></td>')

        # 電流（確認前後負載是否相近）
        if has_curr:
            curr_pre  = _med(pre[current_col])
            curr_post = _med(post[current_col])
            if not np.isnan(curr_pre) and curr_pre != 0 and not np.isnan(curr_post):
                diff_pct  = abs(curr_pre - curr_post) / curr_pre * 100
                curr_color = '#888' if diff_pct < 10 else '#c0392b'
                diff_s = f'{"≈" if diff_pct < 10 else "⚠"}{diff_pct:.1f}%'
            else:
                diff_s, curr_color = '—', '#888'
            curr_pre_s  = f'{curr_pre:.1f}'  if not np.isnan(curr_pre)  else '—'
            curr_post_s = f'{curr_post:.1f}' if not np.isnan(curr_post) else '—'
            rows_html += (f'<td>{curr_pre_s}</td><td>{curr_post_s}</td>'
                          f'<td style="color:{curr_color}">{diff_s}</td>')

        # Health Score
        if has_hs:
            hs_pre, hs_post = _med(pre['health_score']), _med(post['health_score'])
            if not np.isnan(hs_pre) and not np.isnan(hs_post):
                hs_delta = hs_post - hs_pre
                hs_color = '#157f3b' if hs_delta > 0 else '#c0392b'
                hs_s = f'{"+" if hs_delta >= 0 else ""}{hs_delta:.1f}'
            else:
                hs_delta, hs_color, hs_s = float('nan'), '#888', '—'
            hs_pre_s  = f'{hs_pre:.1f}'  if not np.isnan(hs_pre)  else '—'
            hs_post_s = f'{hs_post:.1f}' if not np.isnan(hs_post) else '—'
            rows_html += (f'<td>{hs_pre_s}</td><td>{hs_post_s}</td>'
                          f'<td style="color:{hs_color}"><b>{hs_s}</b></td>')

        rows_html += '</tr>\n'

    if not rows_html:
        # 診斷：看看每個 bin 的前後分佈
        diag = []
        for b in sorted(data['_bin'].unique()):
            sub = data[data['_bin'] == b]
            pre_n  = (sub['_period'] == '前').sum()
            post_n = (sub['_period'] == '後').sum()
            label  = f'{b.left:.1f}~{b.right:.1f}' if hasattr(b, 'left') else f'{b}'
            diag.append(f'{label}（前{pre_n}/後{post_n}）')
        diag_str = '、'.join(diag)
        logger.warning(f"  保養比較無有效行：各bin分佈 = {diag_str}")
        return (f'<p style="color:#c0392b">⚠ 每個{group_label}區間的保養前後資料無法配對。'
                f'各區間前/後筆數：{diag_str}。<br>'
                f'可能原因：保養前後的操作頻率範圍不重疊，或某一側資料量不足。</p>')

    rms_head  = '<th>RMS前</th><th>RMS後</th><th>RMS改善</th>'          if has_rms  else ''
    curr_head = f'<th>電流前(A)</th><th>電流後(A)</th><th>電流差異</th>' if has_curr else ''
    hs_head   = '<th>HS前</th><th>HS後</th><th>HS變化</th>'              if has_hs   else ''

    curr_note = ('電流差異 &lt;10% 顯示灰色（負載相近，比較有效），⚠ 代表電流差異較大需注意。'
                 if has_curr else '')

    return f"""
<table class="stats">
  <thead>
    <tr><th>{group_label} 區間</th><th>n前</th><th>n後</th>
        <th>accOA前</th><th>accOA後</th><th>accOA改善</th>
        {rms_head}{curr_head}{hs_head}</tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
<p style="font-size:0.85em;color:#666">
  改善 = (前−後)/前；綠色↓振動下降（有效），紅色↑上升。{curr_note}
  {'HS 變化 = 後−前；綠色 + 代表健康分數回升。' if has_hs else ''}
  分組依據：「{group_col}」，方式：{bin_method}。
</p>"""


def _build_html(device_id: str, df: pd.DataFrame,
                img_ts: str, img_sc: str | None,
                current_col: str | None,
                threshold: float,
                maint_events: list | None = None) -> str:
    date_range = (f"{df['datetime'].min().strftime('%Y-%m-%d')} ~ "
                  f"{df['datetime'].max().strftime('%Y-%m-%d')}")

    display_cols = [c for c in
                    ['Total_vRMS', 'accOA', 'Crest_Factor', 'health_score', current_col]
                    if c and c in df.columns]

    stats_html = _stats_table_html(df, display_cols)

    # 散佈圖標題依是否有保養切分而異
    maint_events = maint_events or []
    split_date = maint_events[-1][0] if maint_events else None
    scatter_title = ('保養前後 × 頻率/電流散佈圖' if split_date else '頻率/電流 × 振動散佈圖')
    scatter_section = ''
    if img_sc:
        scatter_section = f"""
<h2>{scatter_title}</h2>
<img src="data:image/png;base64,{img_sc}" style="max-width:100%">"""

    # 保養資訊 + 有效性比較表
    maint_section = ''
    if maint_events:
        events_str = '、'.join(f"{dt.strftime('%Y-%m-%d')}（{name}）"
                              for dt, name in maint_events)
        group_col = _pick_group_col(df, current_col)
        is_freq   = bool(group_col and any(kw.lower() in group_col.lower()
                                           for kw in FREQ_COL_KEYWORDS))
        group_desc = '頻率' if is_freq else '負載'
        eff_table = _maintenance_effect_table(df, group_col, current_col, split_date)
        maint_section = f"""
<h2>保養有效性分析（相同{group_desc}分組）</h2>
<p>保養紀錄：<b>{events_str}</b><br>
以最近一次保養（{split_date.strftime('%Y-%m-%d')}）為界，
在<b>相同{group_desc}區間</b>下比較振動變化；電流欄顯示前後負載是否相近。</p>
{eff_table}"""

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

{maint_section}

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
                  threshold: float,
                  maint_log: dict | None = None) -> bool:
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

            # 時間重疊確認 + 詳細診斷
            vib_range   = (df_vib['datetime'].min(), df_vib['datetime'].max())
            scada_range = (df_wide['datetime'].min(), df_wide['datetime'].max())
            logger.info(f"  SCADA 原始時間範圍：{scada_range[0].date()} ~ {scada_range[1].date()} "
                        f"（{len(df_wide)} 筆）")
            if vib_range[0] <= scada_range[1] and scada_range[0] <= vib_range[1]:
                df_vib = merge_vib_scada(df_vib, df_wide)
                current_col = _resolve_current_col(scada_cols, current_type)
                if current_col is None:
                    logger.warning(f"  找不到電流欄位（指定 current_type='{current_type}'）；"
                                   f"現有 variable_type：{scada_cols}。"
                                   f"請用 --current-type 指定正確名稱")
                else:
                    if current_col != current_type:
                        logger.info(f"  電流欄位以部分比對找到：'{current_col}'")
                    # ── merge 後診斷：確認電流資料是否完整對齊 ──
                    valid_curr = df_vib[current_col].notna()
                    if valid_curr.any():
                        curr_max = df_vib.loc[valid_curr, 'datetime'].max()
                        curr_pct = valid_curr.mean() * 100
                        logger.info(f"  電流對齊：{valid_curr.sum()}/{len(df_vib)} 筆（{curr_pct:.1f}%）"
                                    f"，有效電流最晚至 {curr_max.date()}")
                        if curr_max < vib_range[1] - pd.Timedelta(days=3):
                            logger.warning(
                                f"  ⚠ 電流資料在 {curr_max.date()} 後斷掉，"
                                f"但振動資料還有到 {vib_range[1].date()}。"
                                f"請確認 Other_Data 的新 CSV 時間格式是否與舊資料一致。"
                            )
            else:
                logger.warning(f"{device_id}: 振動 {vib_range[0].date()}~{vib_range[1].date()} "
                               f"與 SCADA {scada_range[0].date()}~{scada_range[1].date()} "
                               f"無時間重疊，跳過 SCADA 對齊")
    else:
        logger.warning(f"{device_id}: tag_mapping 中找不到 device_id='{device_base}'。"
                       f"請確認 tag_mapping 的 device_id 值與振動 devicename 一致")

    # ── 開機篩選 ──
    # 有電流的列用電流門檻；電流 NaN（SCADA 資料結束後）改用 vRMS 門檻，
    # 確保 SCADA 期間結束後的新振動資料不被誤殺。
    if current_col and current_col in df_vib.columns and df_vib[current_col].notna().any():
        has_curr_mask = df_vib[current_col].notna()
        curr_on  = has_curr_mask & (df_vib[current_col] > threshold)
        vrms_on  = ~has_curr_mask & (df_vib['Total_vRMS'] > settings.VTRMS_ON_THRESHOLD)
        mask = curr_on | vrms_on
        n_curr = curr_on.sum(); n_vrms = vrms_on.sum()
        logger.info(f"  開機篩選：電流 {n_curr} 筆（>{threshold} A）"
                    f" + vRMS fallback {n_vrms} 筆（SCADA 結束後）"
                    f" = 共 {mask.sum()}/{len(df_vib)} 筆")
        df_vib = df_vib[mask].reset_index(drop=True)
    else:
        mask = df_vib['Total_vRMS'] > settings.VTRMS_ON_THRESHOLD
        logger.info(f"  開機篩選（Total_vRMS > {settings.VTRMS_ON_THRESHOLD}）："
                    f"{mask.sum()}/{len(df_vib)} 筆通過")
        df_vib = df_vib[mask].reset_index(drop=True)

    if df_vib.empty:
        logger.warning(f"{device_id}: 開機篩選後無資料，跳過")
        return False

    # ── 合併 health_score（若 output/scores/ 已有評分結果）──
    hs = load_health_score(device_id)
    if hs is not None:
        df_vib = pd.merge_asof(
            df_vib.sort_values('datetime'),
            hs.sort_values('datetime'),
            on='datetime', tolerance=MERGE_TOL, direction='nearest',
        )
        n_hs = df_vib['health_score'].notna().sum()
        logger.info(f"  health_score 對齊：{n_hs}/{len(df_vib)} 筆")
    else:
        logger.info(f"  無 {device_id}_scores.csv，HTML 不含健康分數"
                    f"（可先跑 test_model.py 產生）")

    # ── 保養事件（用基底名查；套用到 M1/M2 兩個量測點）──
    maint_events = []
    if maint_log:
        maint_events = maint_log.get(device_base, [])
        # 只保留落在資料期間內的保養日期
        dmin, dmax = df_vib['datetime'].min(), df_vib['datetime'].max()
        maint_events = [(dt, nm) for dt, nm in maint_events if dmin <= dt <= dmax]
        if maint_events:
            logger.info(f"  保養事件：{[dt.strftime('%Y-%m-%d') for dt, _ in maint_events]}")

    # ── CSV 匯出 ──
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join(output_dir, f"{device_id}.csv")
    export_cols = ['datetime'] + [c for c in df_vib.columns if c != 'datetime']
    df_vib[export_cols].to_csv(csv_path, index=False)
    logger.info(f"  CSV → {csv_path}  ({len(df_vib)} 筆)")

    # ── 圖表 ──
    split_date = maint_events[-1][0] if maint_events else None
    img_ts = _plot_timeseries(df_vib, device_id, current_col, maint_events)
    img_sc = _plot_scatter(df_vib, device_id, current_col, split_date)

    # ── HTML 匯出 ──
    html = _build_html(device_id, df_vib, img_ts, img_sc,
                       current_col, threshold, maint_events)
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
    parser.add_argument('--maintenance', default=MAINTENANCE_PATH,
                        help=f'保養紀錄 CSV，預設「{MAINTENANCE_PATH}」')
    args = parser.parse_args()

    vib_data = load_vibration('Vibration_Data')
    if not vib_data:
        logger.error("找不到振動資料，請確認 Vibration_Data/ 資料夾")
        sys.exit(1)

    df_other  = load_other_data(OTHER_DATA_DIR)
    tag_map   = load_tag_mapping(TAG_MAPPING_PATH)
    maint_log = load_maintenance_log(args.maintenance)

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
            OUTPUT_DIR, args.current_type, args.threshold, maint_log,
        )
        if ok:
            success += 1

    print(f"\n  完成：{success} 台設備  →  {OUTPUT_DIR}/")


if __name__ == '__main__':
    main()
