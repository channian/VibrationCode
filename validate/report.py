"""
report.py — 把回測結果轉成人看得懂的報表

輸出到 `output/validation/`，全部繁體中文表頭，四張表 + 一份摘要：

  coverage.csv               每台設備/量測點的資料涵蓋率
  gaps.csv                   斷線／資料不全區段清單，依時長排序
  finding_stats_by_rule.csv  依規則的觸發統計
  finding_stats_by_device.csv 依設備的觸發統計
  trigger_density.csv        每台設備每週觸發密度（判斷會不會誤報洪水的關鍵表）
  threshold_sensitivity.csv  門檻敏感度掃描（有跑掃描才會產生）
  summary.txt / summary.html 摘要

**寫檔一律經過 `_safe_write_*`**：這個專案先前在 Windows 上踩過「檔案被
Excel 開著」導致 `PermissionError` 直接讓整支程式中斷的坑，這裡遇到寫入
被拒時會改寫到帶時間戳的檔名，並記警告，不讓一個被鎖住的檔案拖垮整份
回測報告。
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import pandas as pd

from validate.backtest import BacktestResult, span_weeks
from validate.rule_defaults import RuleConfigRow

logger = logging.getLogger(__name__)

_COVERAGE_COLS = {
    'device_id': '設備代碼', 'device_name': '設備名稱', 'point_id': '量測點ID',
    'position': '安裝位置', 'total_hours': '總時數', 'ok_hours': '正常時數',
    'partial_hours': '資料不全時數', 'no_data_hours': '斷線時數',
    'not_running_hours': '未運轉時數', 'analyzable_ratio': '可分析比例',
    'period_start': '期間起', 'period_end': '期間迄',
}
_GAPS_COLS = {
    'device_id': '設備代碼', 'point_id': '量測點ID', 'position': '安裝位置',
    'gap_start': '起始時間', 'gap_end': '結束時間', 'hours': '時長(小時)',
    'status': '狀態',
}


def _safe_write_csv(df: pd.DataFrame, path: str) -> str:
    return _safe_write(path, lambda p: df.to_csv(p, index=False, encoding='utf-8-sig'))


def _safe_write_text(text: str, path: str) -> str:
    return _safe_write(path, lambda p: open(p, 'w', encoding='utf-8').write(text))


def _safe_write(path: str, writer, max_retry: int = 3) -> str:
    """
    寫檔失敗（最常見是 Windows 上檔案被 Excel 開著）時，改寫到帶時間戳的
    備用檔名，而不是讓整份報告因為一個檔案鎖住而全部生不出來。
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    candidate = path
    for attempt in range(max_retry + 1):
        try:
            writer(candidate)
            if candidate != path:
                logger.warning(f"{path} 無法寫入（可能被其他程式開啟），已改寫至 {candidate}")
            return candidate
        except PermissionError:
            ts = dt.datetime.now().strftime('%H%M%S')
            base, ext = os.path.splitext(path)
            candidate = f"{base}_{ts}_{attempt}{ext}"
            logger.warning(f"寫入 {path} 被拒絕（PermissionError），嘗試改寫至 {candidate}")
    raise PermissionError(f"多次嘗試後仍無法寫入 {path}（或其備用檔名）")


def _finding_stats_by_rule(episodes_df: pd.DataFrame,
                            rule_configs: dict[str, RuleConfigRow]) -> pd.DataFrame:
    active_rules = pd.DataFrame([
        {'rule_code': r.rule_code, 'rule_name': r.rule_name, 'family': r.family, 'severity': r.severity}
        for r in rule_configs.values() if r.is_active
    ])
    if episodes_df.empty:
        stats = active_rules.copy()
        stats['n_episodes'] = 0
        stats['n_devices_affected'] = 0
        stats['total_duration_days'] = 0
        stats['avg_duration_days'] = 0.0
        return stats

    g = episodes_df.groupby('rule_code').agg(
        n_episodes=('rule_code', 'size'),
        n_devices_affected=('device_id', 'nunique'),
        total_duration_days=('duration_days', 'sum'),
        avg_duration_days=('duration_days', 'mean'),
    ).reset_index()
    stats = active_rules.merge(g, on='rule_code', how='left')
    for col in ('n_episodes', 'n_devices_affected', 'total_duration_days'):
        stats[col] = stats[col].fillna(0).astype(int)
    stats['avg_duration_days'] = stats['avg_duration_days'].fillna(0.0).round(2)
    return stats.sort_values('n_episodes', ascending=False).reset_index(drop=True)


def _finding_stats_by_device(episodes_df: pd.DataFrame, result: BacktestResult) -> pd.DataFrame:
    all_devices = sorted({pc.point.device.device_id for pc in result.point_contexts})
    device_names = {pc.point.device.device_id: pc.point.device.device_name for pc in result.point_contexts}
    base = pd.DataFrame({'device_id': all_devices})
    base['device_name'] = base['device_id'].map(device_names)

    if episodes_df.empty:
        base['n_episodes'] = 0
        base['n_err'] = 0
        base['n_warn'] = 0
        base['n_distinct_rules'] = 0
        return base

    g = episodes_df.groupby('device_id').agg(
        n_episodes=('device_id', 'size'),
        n_err=('severity', lambda s: int((s == 'err').sum())),
        n_warn=('severity', lambda s: int((s == 'warn').sum())),
        n_distinct_rules=('rule_code', 'nunique'),
    ).reset_index()
    out = base.merge(g, on='device_id', how='left')
    for col in ('n_episodes', 'n_err', 'n_warn', 'n_distinct_rules'):
        out[col] = out[col].fillna(0).astype(int)
    return out.sort_values('n_episodes', ascending=False).reset_index(drop=True)


def _device_span_weeks(result: BacktestResult) -> dict[str, float]:
    """
    每台設備**自己實際被監測到的期間**（週），而不是全批資料的共同期間。

    合成測試資料就踩過這個坑：設備 A 只有 30 天資料、設備 B 有 55 天，
    若密度分母一律用「全部設備裡最早到最晚」的共同區間，A 的密度會被
    嚴重低估（分母比它實際被監測的時間長）。每台設備必須各自算自己的
    觀測期間，密度數字才反映真實負荷。
    """
    spans: dict[str, list[pd.Timestamp]] = {}
    for pc in result.point_contexts:
        if pc.agg.empty:
            continue
        device_id = pc.point.device.device_id
        lo, hi = pc.agg['ts_hour'].min(), pc.agg['ts_hour'].max()
        bounds = spans.setdefault(device_id, [lo, hi])
        bounds[0] = min(bounds[0], lo)
        bounds[1] = max(bounds[1], hi)
    return {d: span_weeks(lo, hi) for d, (lo, hi) in spans.items()}


def _trigger_density(episodes_df: pd.DataFrame, result: BacktestResult) -> pd.DataFrame:
    """
    每台設備每週觸發密度——**判斷會不會誤報洪水的關鍵指標**。

    分母是**該設備自己**的觀測期間（週），分子是該設備的事件數；沒有
    觸發過的設備也要出現在表裡（值為 0），否則平均值會被「有問題的設備」
    帶偏，看不出真實的全廠負荷。
    """
    device_weeks = _device_span_weeks(result)
    by_device = _finding_stats_by_device(episodes_df, result)
    by_device['span_weeks'] = by_device['device_id'].map(device_weeks).fillna(0.0).round(2)
    by_device['episodes_per_week'] = by_device.apply(
        lambda r: round(r['n_episodes'] / r['span_weeks'], 3) if r['span_weeks'] else 0.0, axis=1)
    return by_device[['device_id', 'device_name', 'n_episodes', 'span_weeks', 'episodes_per_week']] \
        .sort_values('episodes_per_week', ascending=False).reset_index(drop=True)


def _build_summary_text(result: BacktestResult, rule_configs: dict[str, RuleConfigRow],
                         stats_by_rule: pd.DataFrame, density: pd.DataFrame,
                         sweep_df: pd.DataFrame | None, using_real: dict[str, bool]) -> str:
    lines = []
    lines.append('=' * 60)
    lines.append('  離線回測摘要（validate/offline.py）')
    lines.append('=' * 60)
    lines.append(f"產出時間：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"回測期間：{result.span_start} ～ {result.span_end}"
                 f"（約 {span_weeks(result.span_start, result.span_end):.1f} 週）")
    lines.append(f"設備數：{result.n_devices}　量測點數：{result.n_points}")
    lines.append('')

    lines.append('-- 指標／規則層實作來源（影響本次回測結果的可信度）--')
    for name, is_real in using_real.items():
        lines.append(f"  {name}：{'真實模組' if is_real else '⚠ stub（簡化版，僅供跑通，勿據此定門檻）'}")
    lines.append('')

    if not result.coverage_df.empty:
        avg_ratio = result.coverage_df['analyzable_ratio'].mean()
        low_cov = result.coverage_df[result.coverage_df['analyzable_ratio'] < 0.5]
        lines.append('-- 資料涵蓋率 --')
        lines.append(f"  平均可分析比例：{avg_ratio:.1%}")
        lines.append(f"  可分析比例 < 50% 的量測點：{len(low_cov)} / {len(result.coverage_df)}")
        if not low_cov.empty:
            lines.append("  ⚠ 這些量測點的規則判定結果不可信，見 coverage.csv")
        lines.append('')

    if not result.gaps_df.empty:
        top_gaps = result.gaps_df.head(5)
        lines.append('-- 最長的斷線／資料不全區段（前 5）--')
        for _, g in top_gaps.iterrows():
            lines.append(f"  {g['device_id']} / {g['position']}：{g['status']}　"
                         f"{g['gap_start']} ～ {g['gap_end']}（{g['hours']:.0f} 小時）")
        lines.append('')

    lines.append('-- Finding 觸發統計（依規則，由多到少）--')
    for _, r in stats_by_rule.iterrows():
        lines.append(f"  {r['rule_code']:20s} {r['rule_name']:14s} "
                     f"觸發 {int(r['n_episodes']):4d} 次　影響 {int(r['n_devices_affected']):3d} 台設備")
    lines.append('')

    lines.append('-- 觸發密度（每台設備每週幾件，前 10 高）--')
    for _, d in density.head(10).iterrows():
        lines.append(f"  {d['device_id']:15s} {d['episodes_per_week']:.2f} 件/週"
                     f"（{d['n_episodes']} 件 / {d['span_weeks']:.1f} 週）")
    # 全廠平均用「總事件數 / 各設備觀測週數總和」——每台設備觀測期間可能
    # 不同（新裝設備、中途停用等），用單一共同期間當分母會系統性算錯
    # （見 `_device_span_weeks` 說明），必須逐台加總分母才正確。
    fleet_total = density['n_episodes'].sum()
    fleet_device_weeks = density['span_weeks'].sum()
    fleet_devices = len(density) or 1
    fleet_density = fleet_total / fleet_device_weeks if fleet_device_weeks else 0
    lines.append(f"  全廠平均：{fleet_density:.2f} 件/設備/週"
                 f"（總計 {int(fleet_total)} 件 / {fleet_devices} 台設備 / 觀測週數總和 {fleet_device_weeks:.1f} 週）")
    if fleet_density > 2:
        lines.append("  ⚠ 平均每台每週超過 2 件，四階段簽核可能很快就會塞爆，建議檢視門檻敏感度表後調鬆")
    lines.append('')

    if sweep_df is not None and not sweep_df.empty:
        lines.append('-- 門檻敏感度掃描 --')
        for rule_code, g in sweep_df.groupby('rule_code'):
            lines.append(f"  {rule_code}：")
            for _, r in g.iterrows():
                lines.append(f"    {r['param_name']}={r['param_value']}　"
                             f"→ {int(r['n_episodes'])} 件（{r['episodes_per_device_per_week']:.3f} 件/設備/週）")
        lines.append('')

    lines.append('=' * 60)
    return '\n'.join(lines)


def _build_summary_html(text_summary: str, stats_by_rule: pd.DataFrame,
                         density: pd.DataFrame, sweep_df: pd.DataFrame | None) -> str:
    def _df_to_html(df: pd.DataFrame) -> str:
        return df.to_html(index=False, border=0, classes='tbl') if not df.empty else '<p>（無資料）</p>'

    sweep_html = _df_to_html(sweep_df) if sweep_df is not None else '<p>（本次未執行門檻敏感度掃描）</p>'

    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<title>離線回測摘要</title>
<style>
  body {{ font-family: -apple-system, "Microsoft JhengHei", sans-serif; margin: 2rem; color: #1a1a1a; }}
  pre {{ background: #f5f5f5; padding: 1rem; border-radius: 6px; white-space: pre-wrap; }}
  table.tbl {{ border-collapse: collapse; margin: 1rem 0; }}
  table.tbl th, table.tbl td {{ border: 1px solid #ddd; padding: 4px 10px; text-align: right; font-size: 0.9rem; }}
  table.tbl th {{ background: #eee; }}
  h2 {{ border-bottom: 2px solid #ccc; padding-bottom: 4px; margin-top: 2rem; }}
</style></head>
<body>
<h1>離線回測摘要</h1>
<pre>{text_summary}</pre>
<h2>Finding 觸發統計（依規則）</h2>
{_df_to_html(stats_by_rule)}
<h2>觸發密度（依設備）</h2>
{_df_to_html(density)}
<h2>門檻敏感度掃描</h2>
{sweep_html}
</body></html>"""


def write_reports(result: BacktestResult, rule_configs: dict[str, RuleConfigRow],
                   out_dir: str, sweep_df: pd.DataFrame | None = None,
                   using_real: dict[str, bool] | None = None) -> dict[str, str]:
    """產出全部報表檔案，回傳 `{報表名稱: 實際寫入路徑}`（可能因鎖檔而改名）。"""
    os.makedirs(out_dir, exist_ok=True)
    using_real = using_real or {}

    stats_by_rule = _finding_stats_by_rule(result.episodes_df, rule_configs)
    stats_by_device = _finding_stats_by_device(result.episodes_df, result)
    density = _trigger_density(result.episodes_df, result)

    written: dict[str, str] = {}
    written['coverage'] = _safe_write_csv(
        result.coverage_df.rename(columns=_COVERAGE_COLS), os.path.join(out_dir, 'coverage.csv'))
    written['gaps'] = _safe_write_csv(
        result.gaps_df.rename(columns=_GAPS_COLS), os.path.join(out_dir, 'gaps.csv'))
    written['finding_stats_by_rule'] = _safe_write_csv(
        stats_by_rule, os.path.join(out_dir, 'finding_stats_by_rule.csv'))
    written['finding_stats_by_device'] = _safe_write_csv(
        stats_by_device, os.path.join(out_dir, 'finding_stats_by_device.csv'))
    written['trigger_density'] = _safe_write_csv(
        density, os.path.join(out_dir, 'trigger_density.csv'))
    written['episodes'] = _safe_write_csv(
        result.episodes_df, os.path.join(out_dir, 'episodes_detail.csv'))
    if sweep_df is not None and not sweep_df.empty:
        written['threshold_sensitivity'] = _safe_write_csv(
            sweep_df, os.path.join(out_dir, 'threshold_sensitivity.csv'))

    summary_text = _build_summary_text(result, rule_configs, stats_by_rule, density, sweep_df, using_real)
    written['summary_txt'] = _safe_write_text(summary_text, os.path.join(out_dir, 'summary.txt'))
    written['summary_html'] = _safe_write_text(
        _build_summary_html(summary_text, stats_by_rule, density, sweep_df),
        os.path.join(out_dir, 'summary.html'))

    logger.info(f"報告已輸出至 {out_dir}")
    return written
