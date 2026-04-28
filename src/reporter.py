"""
reporter.py — 階段四 PNG 報告產出

核心函式：
  generate_report(device_id, position, df_scored, df_baseline, output_dir, maintenance_log)
    → 產出 2×2 PNG 儀表板

  generate_device_summary(score_records, output_path)
    → 彙整各設備最新狀態至 device_summary.csv

四個子圖：
  圖1（左上）：Total vRMS 走勢 + 電流負載背景（雙軸）
  圖2（右上）：Crest Factor + CF=5 紅色虛線警戒
  圖3（左下）：accOA + 健康分數（雙軸），金色區間=基準期
  圖4（右下）：同工況散佈（前50%灰 vs 後50%紅，含趨勢線）
"""

import os
import logging
import warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')                        # headless 伺服器用非互動式後端
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager
from collections import defaultdict

from config import settings

logger = logging.getLogger(__name__)

# 忽略缺少中文字形的 UserWarning（Linux 環境常見）
warnings.filterwarnings('ignore', message='Glyph.*missing', category=UserWarning)

POSITION_LABEL = {'M1': 'M1 自由端', 'M2': 'M2 驅動端'}

_FONT_READY = False


# ── 字型初始化 ──────────────────────────────────────────────

def _setup_font() -> None:
    global _FONT_READY
    if _FONT_READY:
        return
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in settings.FONTS:
        if name in available:
            matplotlib.rcParams['font.family'] = name
            matplotlib.rcParams['axes.unicode_minus'] = False
            logger.debug(f"reporter: using font '{name}'")
            _FONT_READY = True
            return
    matplotlib.rcParams['axes.unicode_minus'] = False
    _FONT_READY = True


# ── 繪圖輔助 ────────────────────────────────────────────────

def _shade_baseline(ax, df_baseline: pd.DataFrame) -> None:
    """金色背景標示基準期。"""
    if df_baseline.empty or 'datetime' not in df_baseline.columns:
        return
    t0 = df_baseline['datetime'].min()
    t1 = df_baseline['datetime'].max()
    ax.axvspan(t0, t1, alpha=0.18, color='gold', label='基準期', zorder=0)


def _mark_maintenance(ax, maintenance_log) -> None:
    """紅色垂直虛線標示保養事件，標籤放在頂部。"""
    if maintenance_log is None:
        return
    if isinstance(maintenance_log, pd.DataFrame) and maintenance_log.empty:
        return
    for _, row in maintenance_log.iterrows():
        ax.axvline(row['datetime'], color='crimson', linestyle='--',
                   linewidth=0.9, alpha=0.75, zorder=5)
        ax.text(
            row['datetime'], 1.01,
            str(row.get('event_name', '保養')),
            transform=ax.get_xaxis_transform(),
            rotation=90, va='bottom', ha='center',
            fontsize=7, color='crimson',
        )


def _fmt_xaxis(ax, dates: pd.Series) -> None:
    """依資料時間跨度自動選擇 x 軸刻度與格式。"""
    valid = dates.dropna()
    if len(valid) < 2:
        return
    span = (valid.max() - valid.min()).days
    if span <= 60:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    elif span <= 365:
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%y/%m'))
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%y/%m'))
    plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right', fontsize=8)


# ── 四個子圖 ────────────────────────────────────────────────

def _plot_vrms(ax, df: pd.DataFrame, df_baseline: pd.DataFrame, maintenance_log) -> None:
    """圖1：Total vRMS 走勢 + 電流背景雙軸。"""
    ax.set_title('Total vRMS 趨勢', fontsize=11)

    if 'Total_vRMS' not in df.columns or df['Total_vRMS'].isna().all():
        ax.text(0.5, 0.5, '無 Total_vRMS 資料', transform=ax.transAxes,
                ha='center', va='center', color='gray')
        return

    ax.plot(df['datetime'], df['Total_vRMS'],
            color='steelblue', linewidth=0.8, label='Total vRMS', zorder=2)
    ax.set_ylabel('Total vRMS (mm/s)', color='steelblue', fontsize=9)
    ax.tick_params(axis='y', labelcolor='steelblue')

    if 'current_A' in df.columns and df['current_A'].notna().any():
        ax2 = ax.twinx()
        ax2.fill_between(df['datetime'], df['current_A'].fillna(0),
                         alpha=0.12, color='darkorange', label='電流 (A)', zorder=1)
        ax2.set_ylabel('電流 (A)', color='darkorange', fontsize=9)
        ax2.tick_params(axis='y', labelcolor='darkorange')

    _shade_baseline(ax, df_baseline)
    _mark_maintenance(ax, maintenance_log)
    _fmt_xaxis(ax, df['datetime'])
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3, linestyle='--')


def _plot_crest_factor(ax, df: pd.DataFrame, df_baseline: pd.DataFrame, maintenance_log) -> None:
    """圖2：Crest Factor 走勢 + CF=5 警戒線。"""
    ax.set_title('Crest Factor（衝擊指標）', fontsize=11)

    if 'Crest_Factor' not in df.columns or df['Crest_Factor'].isna().all():
        ax.text(0.5, 0.5, '無 Crest_Factor 資料', transform=ax.transAxes,
                ha='center', va='center', color='gray')
        return

    ax.plot(df['datetime'], df['Crest_Factor'],
            color='mediumpurple', linewidth=0.8, label='Crest Factor', zorder=2)
    ax.axhline(5.0, color='red', linestyle='--', linewidth=1.2, alpha=0.8,
               label='CF=5 警戒線', zorder=3)
    ax.set_ylabel('Crest Factor', fontsize=9)

    _shade_baseline(ax, df_baseline)
    _mark_maintenance(ax, maintenance_log)
    _fmt_xaxis(ax, df['datetime'])
    ax.legend(loc='upper right', fontsize=8)
    ax.grid(True, alpha=0.3, linestyle='--')


def _plot_accroa_score(ax, df: pd.DataFrame, df_baseline: pd.DataFrame, maintenance_log) -> None:
    """圖3：accOA（主軸）+ 健康分數（副軸），金色區間標示基準期。"""
    ax.set_title('accOA 與健康分數', fontsize=11)

    if 'accOA' in df.columns and not df['accOA'].isna().all():
        ax.plot(df['datetime'], df['accOA'],
                color='teal', linewidth=0.8, label='accOA (g)', zorder=2)
        ax.set_ylabel('accOA (g)', color='teal', fontsize=9)
        ax.tick_params(axis='y', labelcolor='teal')

    score_col = 'health_score_smooth' if 'health_score_smooth' in df.columns else 'Health_Score'
    if score_col in df.columns and not df[score_col].isna().all():
        ax_s = ax.twinx()
        ax_s.plot(df['datetime'], df[score_col],
                  color='darkorange', linewidth=1.2, alpha=0.85,
                  label='健康分數（平滑）', zorder=3)
        ax_s.axhline(settings.ALERT_NORMAL,  color='green', linestyle=':', linewidth=1.0, alpha=0.7)
        ax_s.axhline(settings.ALERT_WARNING, color='red',   linestyle=':', linewidth=1.0, alpha=0.7)
        ax_s.set_ylim(0, 108)
        ax_s.set_ylabel('健康分數', color='darkorange', fontsize=9)
        ax_s.tick_params(axis='y', labelcolor='darkorange')

    _shade_baseline(ax, df_baseline)
    _mark_maintenance(ax, maintenance_log)
    _fmt_xaxis(ax, df['datetime'])
    ax.legend(loc='upper left', fontsize=8)
    ax.grid(True, alpha=0.3, linestyle='--')


def _plot_scatter(ax, df: pd.DataFrame) -> None:
    """圖4：同工況散佈圖，前/後 50% 時段比較（含趨勢線）。"""
    ax.set_title('同工況散佈圖（前/後 50%）', fontsize=11)

    has_current = 'current_A' in df.columns and df['current_A'].notna().any()
    x_col = 'current_A' if has_current else 'accOA'
    y_col = 'Total_vRMS'

    if x_col not in df.columns or y_col not in df.columns:
        ax.text(0.5, 0.5, '資料不足', transform=ax.transAxes,
                ha='center', va='center', color='gray')
        return

    valid = df[[x_col, y_col]].dropna()
    if len(valid) < 6:
        ax.text(0.5, 0.5, '有效點數不足 (< 6)', transform=ax.transAxes,
                ha='center', va='center', color='gray')
        return

    n     = len(valid)
    early = valid.iloc[:n // 2]
    late  = valid.iloc[n // 2:]

    ax.scatter(early[x_col], early[y_col],
               c='lightsteelblue', s=14, alpha=0.55, label='前 50%（早期）', zorder=2)
    ax.scatter(late[x_col],  late[y_col],
               c='tomato',        s=14, alpha=0.55, label='後 50%（近期）', zorder=3)

    for subset, color in [(early, 'steelblue'), (late, 'crimson')]:
        if len(subset) >= 2:
            z  = np.polyfit(subset[x_col].values, subset[y_col].values, 1)
            xr = np.linspace(subset[x_col].min(), subset[x_col].max(), 60)
            ax.plot(xr, np.poly1d(z)(xr), color=color, linewidth=1.5, alpha=0.85, zorder=4)

    x_label = '電流 (A)' if has_current else 'accOA (g)'
    ax.set_xlabel(x_label, fontsize=9)
    ax.set_ylabel('Total vRMS (mm/s)', fontsize=9)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3, linestyle='--')


# ── 主函式 ──────────────────────────────────────────────────

def generate_report(
    device_id: str,
    position: str,
    df_scored: pd.DataFrame,
    df_baseline: pd.DataFrame,
    output_dir: str = 'output/reports',
    maintenance_log=None,
) -> str:
    """
    產出 2×2 PNG 振動健康診斷報告。

    Args:
        device_id      : 設備 ID（如 ZP1_2_M1）
        position       : 'M1' 或 'M2'
        df_scored      : model.predict() 輸出（含 Health_Score / health_score_smooth）
        df_baseline    : 基準期 DataFrame（用於金色區間標示）
        output_dir     : 輸出目錄（預設 output/reports）
        maintenance_log: DataFrame，欄位 datetime / event_name（選填）

    Returns:
        已儲存 PNG 的完整路徑
    """
    _setup_font()
    os.makedirs(output_dir, exist_ok=True)

    pos_label = POSITION_LABEL.get(position, position)
    d_min = df_scored['datetime'].min().strftime('%Y/%m/%d') if not df_scored.empty else '–'
    d_max = df_scored['datetime'].max().strftime('%Y/%m/%d') if not df_scored.empty else '–'

    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(
        f"{device_id}  {pos_label}  —  振動健康診斷報告\n{d_min}  ～  {d_max}",
        fontsize=13, fontweight='bold', y=0.99,
    )

    ax1, ax2 = axes[0]
    ax3, ax4 = axes[1]

    _plot_vrms(ax1, df_scored, df_baseline, maintenance_log)
    _plot_crest_factor(ax2, df_scored, df_baseline, maintenance_log)
    _plot_accroa_score(ax3, df_scored, df_baseline, maintenance_log)
    _plot_scatter(ax4, df_scored)

    plt.tight_layout(rect=[0, 0, 1, 0.96])

    out_path = os.path.join(output_dir, f"{device_id}_{position}_report.png")
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        fig.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)

    logger.info(f"Report saved → {out_path}")
    return out_path


def generate_device_summary(
    score_records: list,
    output_path: str = 'output/device_summary.csv',
) -> pd.DataFrame:
    """
    彙整各設備（M1/M2 合併）的最新健康評分至 device_summary.csv。

    Args:
        score_records: list[dict]，每筆對應一個 device_id+position，含：
            machine_id, model_group, position, latest_score,
            baseline_start, baseline_end, data_count

    Returns:
        device_summary DataFrame
    """
    machines: dict = defaultdict(dict)

    for r in score_records:
        mid = r.get('machine_id', r.get('device_id', 'unknown'))
        pos = r.get('position', 'M1')
        machines[mid]['machine_id']  = mid
        machines[mid]['model_group'] = r.get('model_group', '')
        machines[mid][f'latest_score_{pos}']   = r.get('latest_score')
        machines[mid][f'baseline_start_{pos}'] = r.get('baseline_start', '')
        machines[mid][f'baseline_end_{pos}']   = r.get('baseline_end', '')
        machines[mid][f'data_count_{pos}']     = r.get('data_count', 0)

    rows = []
    for mid, m in machines.items():
        s1 = m.get('latest_score_M1')
        s2 = m.get('latest_score_M2')
        valid_scores = [v for v in [s1, s2]
                        if v is not None and not (isinstance(v, float) and np.isnan(v))]
        overall = min(valid_scores) if valid_scores else float('nan')

        if np.isnan(overall):
            alert = 'Unknown'
        elif overall >= settings.ALERT_NORMAL:
            alert = 'Normal'
        elif overall >= settings.ALERT_WARNING:
            alert = 'Warning'
        else:
            alert = 'Critical'

        rows.append({
            'machine_id':      mid,
            'model_group':     m.get('model_group', ''),
            'latest_score_M1': round(s1, 1) if s1 is not None else None,
            'latest_score_M2': round(s2, 1) if s2 is not None else None,
            'overall_score':   round(overall, 1) if not np.isnan(overall) else None,
            'alert_level':     alert,
            'baseline_start':  m.get('baseline_start_M1') or m.get('baseline_start_M2', ''),
            'baseline_end':    m.get('baseline_end_M1')   or m.get('baseline_end_M2',   ''),
            'data_count':      (m.get('data_count_M1') or 0) + (m.get('data_count_M2') or 0),
        })

    df = pd.DataFrame(rows, columns=[
        'machine_id', 'model_group',
        'latest_score_M1', 'latest_score_M2', 'overall_score', 'alert_level',
        'baseline_start', 'baseline_end', 'data_count',
    ])
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    df.to_csv(output_path, index=False)
    logger.info(f"Device summary saved → {output_path}")
    return df
