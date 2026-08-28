"""
report.py — 把回測結果轉成人看得懂的報表

輸出到 `output/validation/`，全部繁體中文表頭，四張表 + 一份摘要：

  coverage.csv               每台設備/量測點的資料涵蓋率
  gaps.csv                   斷線／資料不全區段清單，依時長排序
  finding_stats_by_rule.csv  依規則的觸發統計（含 category 欄位）
  finding_stats_by_device.csv 依設備的觸發統計
  trigger_density.csv        每台設備每週觸發密度，依 RuleCategory 分開算
                              （判斷會不會誤報洪水的關鍵表）
  episodes_detail.csv        每一個觸發事件的明細（含 category 欄位）
  threshold_sensitivity.csv  門檻敏感度掃描（有跑掃描才會產生）
  summary.txt / summary.html 摘要

**寫檔一律經過 `_safe_write_*`**：這個專案先前在 Windows 上踩過「檔案被
Excel 開著」導致 `PermissionError` 直接讓整支程式中斷的坑，這裡遇到寫入
被拒時會改寫到帶時間戳的檔名，並記警告，不讓一個被鎖住的檔案拖垮整份
回測報告。

**為什麼觸發統計與密度都要依 `RuleCategory` 分開呈現**：13 條規則裡，
`SENSOR_OFFLINE`／`DATA_QUALITY` 這類「資料收不到」的規則，處置者是
IT／儀電；其餘「設備狀態可能有變化」的規則，處置者是設備工程師。實測
資料裡前兩者常佔全部 Finding 的七成以上，若把兩類混算成單一「觸發
密度」，數字會被資料可用性問題撐大，讓人誤以為設備普遍不穩定、進而
誤判要加派工程師人力或調鬆振動門檻——但真正該處理的是感測器佈建。
分類依據見 `vibcore.rules.engine.RULE_CATEGORY` 的說明。
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import pandas as pd

from vibcore.rules.engine import RULE_CATEGORY, RuleCategory, rule_category

from validate.backtest import BacktestResult, span_weeks
from validate.rule_defaults import RuleConfigRow

logger = logging.getLogger(__name__)

#: 分類代碼 → 報表顯示用的中文標籤（含處置者提示，讓人一眼看出誰要處理）
_CATEGORY_LABELS: dict[str, str] = {
    RuleCategory.EQUIPMENT: '設備狀態類（設備工程師判讀）',
    RuleCategory.DATA_AVAILABILITY: '資料可用性類（IT／儀電處置）',
}

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
        {'rule_code': r.rule_code, 'rule_name': r.rule_name, 'family': r.family, 'severity': r.severity,
         'category': rule_category(r.rule_code)}
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
    # 分類內部仍依觸發次數由多到少排——category 分組時（見
    # `_build_summary_text`）沿用這個順序，不必再排一次
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


def _finding_stats_by_device_category(episodes_df: pd.DataFrame, result: BacktestResult) -> pd.DataFrame:
    """
    每台設備依 `RuleCategory` 分開的觸發次數——密度必須分開算（見模組
    docstring），所以在合計之前就要先把「這台設備這次回測期間各分類
    各觸發幾件」拆出來，供 `_trigger_density` 使用。
    """
    all_devices = sorted({pc.point.device.device_id for pc in result.point_contexts})
    device_names = {pc.point.device.device_id: pc.point.device.device_name for pc in result.point_contexts}
    base = pd.DataFrame({'device_id': all_devices})
    base['device_name'] = base['device_id'].map(device_names)
    base['n_equipment'] = 0
    base['n_data_availability'] = 0

    if episodes_df.empty:
        return base

    df = episodes_df.copy()
    df['category'] = df['rule_code'].map(rule_category)
    g = (df.groupby(['device_id', 'category']).size().unstack(fill_value=0)
         .reindex(columns=[RuleCategory.EQUIPMENT, RuleCategory.DATA_AVAILABILITY], fill_value=0)
         .rename(columns={RuleCategory.EQUIPMENT: 'n_equipment',
                          RuleCategory.DATA_AVAILABILITY: 'n_data_availability'})
         .reset_index())
    out = base.drop(columns=['n_equipment', 'n_data_availability']).merge(g, on='device_id', how='left')
    for col in ('n_equipment', 'n_data_availability'):
        out[col] = out[col].fillna(0).astype(int)
    return out


def _trigger_density(episodes_df: pd.DataFrame, result: BacktestResult) -> pd.DataFrame:
    """
    每台設備每週觸發密度——**判斷會不會誤報洪水的關鍵指標**。

    分母是**該設備自己**的觀測期間（週），分子是該設備的事件數；沒有
    觸發過的設備也要出現在表裡（值為 0），否則平均值會被「有問題的設備」
    帶偏，看不出真實的全廠負荷。

    **`equipment_per_week` 與 `data_availability_per_week` 分開算、不合併
    成單一密度**：兩者處置者不同（設備工程師 vs IT／儀電），合併計算會
    讓佔比常過半的資料可用性問題把「工程師該擔心的密度」灌爆（見模組
    docstring）。`episodes_per_week` 仍保留原本的合計密度，供想看整體
    告警量的人參考，但校準振動門檻、估算工程師工作量請用
    `equipment_per_week`。
    """
    device_weeks = _device_span_weeks(result)
    by_device = _finding_stats_by_device_category(episodes_df, result)
    by_device['n_episodes'] = by_device['n_equipment'] + by_device['n_data_availability']
    by_device['span_weeks'] = by_device['device_id'].map(device_weeks).fillna(0.0).round(2)

    def _density(n: int, weeks: float) -> float:
        return round(n / weeks, 3) if weeks else 0.0

    by_device['equipment_per_week'] = by_device.apply(
        lambda r: _density(r['n_equipment'], r['span_weeks']), axis=1)
    by_device['data_availability_per_week'] = by_device.apply(
        lambda r: _density(r['n_data_availability'], r['span_weeks']), axis=1)
    by_device['episodes_per_week'] = by_device.apply(
        lambda r: _density(r['n_episodes'], r['span_weeks']), axis=1)

    cols = ['device_id', 'device_name', 'n_equipment', 'n_data_availability', 'n_episodes',
            'span_weeks', 'equipment_per_week', 'data_availability_per_week', 'episodes_per_week']
    # 依「設備狀態類」密度排序——那才是工程師要看、決定要不要調門檻或
    # 加派人力的數字（見 `_build_summary_text` 的排行榜說明）
    return by_device[cols].sort_values('equipment_per_week', ascending=False).reset_index(drop=True)


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

    lines.append('-- Finding 觸發統計 --')
    lines.append('  兩類處置者不同（見 vibcore.rules.engine.RULE_CATEGORY），分開列示、各自小計，')
    lines.append('  不要把兩邊的次數加在一起當「觸發密度」看，否則會誤判設備狀態的嚴重程度。')
    grand_total = 0
    for cat in (RuleCategory.EQUIPMENT, RuleCategory.DATA_AVAILABILITY):
        subset = stats_by_rule[stats_by_rule['category'] == cat] if 'category' in stats_by_rule.columns \
            else stats_by_rule.iloc[0:0]
        subtotal = int(subset['n_episodes'].sum()) if not subset.empty else 0
        grand_total += subtotal
        lines.append('')
        lines.append(f"  【{_CATEGORY_LABELS[cat]}】小計 {subtotal} 件")
        for _, r in subset.iterrows():
            lines.append(f"    {r['rule_code']:20s} {r['rule_name']:14s} "
                         f"觸發 {int(r['n_episodes']):4d} 次　影響 {int(r['n_devices_affected']):3d} 台設備")
    lines.append('')
    lines.append(f"  合計：{grand_total} 件")
    lines.append('')

    lines.append('-- 觸發密度 --')
    lines.append('  「設備狀態類」密度才是拿來校準振動門檻、估算工程師工作量的依據；')
    lines.append('  「資料可用性類」密度反映的是感測器／通訊／量程等佈建品質，該處置的是')
    lines.append('  IT／儀電，不能拿來加派工程師人力或調鬆振動門檻。')
    lines.append('')
    lines.append('  前 10 高「設備狀態類」觸發密度（依此排序，工程師優先看這裡；')
    lines.append('  同列附上同期資料可用性件數供對照）：')
    for _, d in density.sort_values('equipment_per_week', ascending=False).head(10).iterrows():
        lines.append(f"  {d['device_id']:15s} 設備狀態 {d['equipment_per_week']:.2f} 件/週"
                     f"（{int(d['n_equipment'])} 件 / {d['span_weeks']:.1f} 週）"
                     f"　同期資料可用性 {int(d['n_data_availability'])} 件")
    lines.append('')
    # 全廠平均用「總事件數 / 各設備觀測週數總和」——每台設備觀測期間可能
    # 不同（新裝設備、中途停用等），用單一共同期間當分母會系統性算錯
    # （見 `_device_span_weeks` 說明），必須逐台加總分母才正確。
    fleet_device_weeks = density['span_weeks'].sum()
    fleet_equipment = int(density['n_equipment'].sum())
    fleet_data_availability = int(density['n_data_availability'].sum())
    fleet_total = fleet_equipment + fleet_data_availability
    fleet_devices = len(density) or 1

    def _fleet_density(n: int) -> float:
        return n / fleet_device_weeks if fleet_device_weeks else 0.0

    equipment_density = _fleet_density(fleet_equipment)
    lines.append(f"  全廠平均－設備狀態類：{equipment_density:.2f} 件/設備/週"
                 f"（{fleet_equipment} 件 / {fleet_devices} 台設備 / 觀測週數總和 {fleet_device_weeks:.1f} 週）"
                 "　★ 校準門檻、估算工程師工作量請用這個數字")
    lines.append(f"  全廠平均－資料可用性類：{_fleet_density(fleet_data_availability):.2f} 件/設備/週"
                 f"（{fleet_data_availability} 件）　→ 反映佈建品質，處置者是 IT／儀電")
    lines.append(f"  全廠平均－合計：{_fleet_density(fleet_total):.2f} 件/設備/週"
                 f"（{fleet_total} 件，兩類相加僅供參考整體告警量，不可直接用於門檻校準）")
    if equipment_density > 2:
        lines.append("  ⚠ 設備狀態類平均每台每週超過 2 件，四階段簽核可能很快就會塞爆，"
                     "建議檢視門檻敏感度表後調鬆")
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

    # 兩張表分開放，呼應摘要文字：處置者不同，不該放在同一張表裡讓人
    # 順手把兩邊的次數加總來看
    if 'category' in stats_by_rule.columns:
        stats_equipment = stats_by_rule[stats_by_rule['category'] == RuleCategory.EQUIPMENT]
        stats_data_avail = stats_by_rule[stats_by_rule['category'] == RuleCategory.DATA_AVAILABILITY]
    else:
        stats_equipment = stats_by_rule.iloc[0:0]
        stats_data_avail = stats_by_rule.iloc[0:0]

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
  p.note {{ color: #555; font-size: 0.9rem; }}
</style></head>
<body>
<h1>離線回測摘要</h1>
<pre>{text_summary}</pre>
<h2>Finding 觸發統計－{_CATEGORY_LABELS[RuleCategory.EQUIPMENT]}</h2>
{_df_to_html(stats_equipment)}
<h2>Finding 觸發統計－{_CATEGORY_LABELS[RuleCategory.DATA_AVAILABILITY]}</h2>
{_df_to_html(stats_data_avail)}
<h2>觸發密度（依設備，依設備狀態類密度排序）</h2>
<p class="note">equipment_per_week 才是校準門檻、估算工程師工作量的依據；data_availability_per_week 反映佈建品質，處置者是 IT／儀電。</p>
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
    # 明細表補上 category 欄位——單看 rule_code 要對照分類表才知道處置者
    # 是誰，直接落欄位讓人在 Excel 裡就能篩選/樞紐分析，不必回頭查表
    episodes_out = result.episodes_df.copy()
    episodes_out['category'] = episodes_out['rule_code'].map(rule_category)
    written['episodes'] = _safe_write_csv(
        episodes_out, os.path.join(out_dir, 'episodes_detail.csv'))
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
