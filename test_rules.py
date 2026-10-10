"""
test_rules.py — 規則層與 ISO 分類的驗收腳本

涵蓋 2026-09 的四項變更（見 db/migration_004）：

  1. 衝擊型指標聚合改 max/median 並存，IMPACT_RISE 判定改用 median
  2. ISO 門檻改為 ISO 10816-3 的 (Group, 基礎剛性) 結構，含適用範圍檢查
  3. VEL_HIGH / ISO_ZONE 的持續性緩衝
  4. 基準期不得早於最後一次保養

大部分測項不需要資料庫（規則層吃的是 DataFrame + DeviceContext）；
需要 DB 的部分（台帳欄位保留、migration 冪等性）會在找不到資料庫時
自動略過並如實回報，不會偽裝成通過。

執行方式：
    python test_rules.py
    python test_rules.py --dbname other_db     # 指定測試資料庫

DB 連線沿用 VIB_DB_HOST / VIB_DB_PORT / VIB_DB_USER / VIB_DB_PASSWORD；
資料庫名稱由本腳本決定（會 DROP 重建），刻意不吃 VIB_DB_NAME。
"""

import argparse
import dataclasses
import datetime as dt
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from vibcore.metrics.baseline import detect_baseline
from vibcore.metrics.iso import (ISO_THRESHOLDS, evaluate_iso, iso_alert_threshold,
                                 iso_scope_reason, resolve_class)
from vibcore.config import resolve_axis_directions
from vibcore.pipeline.aggregate import _aggregate_running
from vibcore.rules.guardrail import check_outcome, check_text
from vibcore.rules.metric_rules import (_DEGRADE_TREND_METRICS, _STEP_CHANGE_FEATURES,
                                        impact_rise, iso_zone, vel_high)
from vibcore.types import BaselineStats, DeviceContext, MetricStats, RuleContext

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_TEST_DB = "vib_rules_test"
NOW = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)

_PASSED: list[str] = []
_SKIPPED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}{('：' + detail) if detail else ''}")
    _PASSED.append(label)
    print(f"  ✓ {label}")


def skip(label: str, why: str) -> None:
    _SKIPPED.append(label)
    print(f"  – {label}（略過：{why}）")


# ──────────────────────────────────────────────────────────
# 測試素材
# ──────────────────────────────────────────────────────────

def device(**kw) -> DeviceContext:
    base = dict(device_id='T1', device_name='測試機', building='A棟', floor='1F',
                system_name='測試', iso_class_source='manual_override',
                rated_power_kw=75.0, rated_rpm=1710.0)
    base.update(kw)
    return DeviceContext(**base)


def ctx(rows: list[dict], stats: dict, params: dict | None = None,
        dev: DeviceContext | None = None) -> RuleContext:
    """組出 RuleContext；`rows` 依序視為每小時一筆、最新的在最後。"""
    agg = pd.DataFrame([
        {**r, 'ts_hour': NOW - dt.timedelta(hours=len(rows) - i), 'data_status': 'ok'}
        for i, r in enumerate(rows)
    ])
    baseline = BaselineStats(
        point_id=1, start_date=dt.date(2026, 8, 1), end_date=dt.date(2026, 8, 15),
        source='auto', stats={k: MetricStats(*v) for k, v in stats.items()}, n_hours=200)
    return RuleContext(device=dev or device(iso_machine_group='2', iso_foundation='rigid'),
                       point_id=1, position='M1', agg=agg, baseline=baseline,
                       params=params or {}, now=NOW)


# ──────────────────────────────────────────────────────────
# 一、衝擊型指標的聚合與 IMPACT_RISE
# ──────────────────────────────────────────────────────────

def test_impact() -> None:
    print("\n[1] 衝擊型指標：聚合 max/median 並存，判定用 median 的 crest")

    # 用真實資料驗證聚合，沒有就退回合成資料（結論相同，只是說服力較低）
    src = 'data/Analytic.csv'
    if os.path.exists(src):
        d = pd.read_csv(src, sep='\t', encoding='utf-8-sig', low_memory=False)
        out = _aggregate_running(d)
        check("聚合同時輸出 max 與 median 兩個通道",
              all(out.get(k) is not None for k in
                  ('acc_kurt', 'acc_kurt_median', 'acc_crest', 'acc_crest_median',
                   'acc_crest_axis_max', 'acc_crest_axis_median')))
        check("median 欄位等於逐筆中位數",
              abs(out['acc_crest_median'] - d['accCREST'].median()) < 1e-9,
              f"{out['acc_crest_median']} vs {d['accCREST'].median()}")
        check("max 欄位等於逐筆最大值",
              abs(out['acc_crest'] - d['accCREST'].max()) < 1e-9)
        check(f"實測 max({out['acc_kurt']:.2f}) 遠高於 median({out['acc_kurt_median']:.2f})"
              "——這是移除絕對門檻的依據",
              out['acc_kurt'] > 4 > out['acc_kurt_median'])
        # kurtosis 欄位仍然聚合入庫（STEP_CHANGE 還在用，且日後可能翻案），
        # 只是不再進 IMPACT_RISE 的判定
        check("accKURT 仍照常聚合入庫（供 STEP_CHANGE 與日後重新評估）",
              out.get('acc_kurt_median') is not None)
    else:
        skip("以真實資料驗證聚合", f"找不到 {src}")

    r = impact_rise(ctx(
        [{'acc_crest_median': 3.4, 'acc_crest': 9.0}],
        {'acc_crest_median': (2.6, 2.6, 0.3, 300)}))
    check("有 median 基準時優先用 median 通道",
          r.triggered and r.evidence['channels']['crest']['metric'] == 'acc_crest_median',
          str(r.evidence['channels']['crest']))

    r = impact_rise(ctx([{'acc_crest_median': 3.4, 'acc_crest': 9.0}],
                        {'acc_crest': (5.0, 5.0, 0.5, 300)}))
    check("舊基準沒有 median 統計量時退回 max 並標明",
          r.triggered and r.evidence['channels']['crest']['metric'] == 'acc_crest',
          str(r.evidence['channels']['crest']))

    # ── 2026-09 專家會議定案：移除 kurtosis 通道 ──────────────
    r = impact_rise(ctx([{'acc_kurt_median': 40.0, 'acc_crest_median': 2.6}],
                        {'acc_kurt_median': (2.4, 2.4, 0.2, 300),
                         'acc_crest_median': (2.6, 2.6, 0.3, 300)}))
    check("kurtosis 暴增但 crest 不動時不再觸發（kurt 通道已移除）",
          not r.triggered)

    r = impact_rise(ctx([{'acc_crest_median': 3.4, 'acc_kurt_median': 40.0}],
                        {'acc_crest_median': (2.6, 2.6, 0.3, 300),
                         'acc_kurt_median': (2.4, 2.4, 0.2, 300)}))
    check("evidence 的 channels 只剩 crest 與 crest_axis",
          r.triggered and set(r.evidence['channels']) == {'crest', 'crest_axis'},
          str(sorted(r.evidence['channels'])))
    for gone in ('kurt_absolute_threshold', 'kurt_absolute_exceeded',
                 'threshold_mode', 'require_both'):
        check(f"evidence 不再含已移除的 {gone}", gone not in r.evidence)
    check("interpretation_limit 不再提 Kurtosis",
          'Kurtosis' not in r.interpretation_limit and '峰度' not in r.interpretation_limit,
          r.interpretation_limit)

    # 逐軸通道仍在：衝擊集中在單一方向時，合成值可能被稀釋
    r = impact_rise(ctx([{'acc_crest_median': 2.6, 'acc_crest_axis_median': 5.0}],
                        {'acc_crest_median': (2.6, 2.6, 0.3, 300),
                         'acc_crest_axis_median': (3.0, 3.0, 0.4, 300)}))
    check("僅逐軸超標時仍觸發並標記 trigger_source=axis_max",
          r.triggered and r.evidence['trigger_source'] == 'axis_max',
          str(r.evidence.get('trigger_source')))

    check("DEGRADE_TREND 的候選指標已移除 acc_kurt",
          'acc_kurt' not in _DEGRADE_TREND_METRICS, str(_DEGRADE_TREND_METRICS))
    check("STEP_CHANGE 仍保留 acc_kurt（多變量特徵，不是衝擊判準）",
          'acc_kurt' in _STEP_CHANGE_FEATURES, str(_STEP_CHANGE_FEATURES))


# ──────────────────────────────────────────────────────────
# 二、ISO 10816-3 分類
# ──────────────────────────────────────────────────────────

def test_iso() -> None:
    print("\n[2] ISO 10816-3：(Group, 基礎剛性) 分類與適用範圍")

    agg1 = [{'vel_rms': 1.76}]
    for foundation, want in (('rigid', 'B'), ('flexible', 'A')):
        res = evaluate_iso(ctx(agg1, {}, dev=device(iso_machine_group='2',
                                                    iso_foundation=foundation)).agg,
                           device(iso_machine_group='2', iso_foundation=foundation), None)
        check(f"Group 2/{foundation}：velRMS 1.76 → Zone {want}"
              f"（A/B 界 {ISO_THRESHOLDS[('2', foundation)]['ab']}）",
              res.applicable and res.zone == want, str(res.zone))

    res = evaluate_iso(pd.DataFrame([{'ts_hour': NOW, 'data_status': 'ok', 'vel_rms': 1.76}]),
                       device(iso_machine_group='2', iso_foundation=None), None)
    check("基礎剛性未填即不判定，且 note 說明缺什麼",
          not res.applicable and res.zone is None and '基礎剛性' in res.note, res.note)

    # 補完台帳之後最可能踩到的坑：群組與基礎剛性都填好了，卻因為
    # iso_class_source 還停在 unset 而整個不套用——台帳看起來補完了，
    # ISO_ZONE 依然 0 次，而且沒有任何訊息說明為什麼。
    res = evaluate_iso(pd.DataFrame([{'ts_hour': NOW, 'data_status': 'ok', 'vel_rms': 1.76}]),
                       device(iso_machine_group='2', iso_foundation='rigid',
                              iso_class_source='unset'), None)
    check("分類已填但 class_source 仍 unset 時，note 講得出是哪裡卡住",
          not res.applicable and 'iso_class_source' in res.note
          and 'manual_override' in res.note, res.note)

    # 回測路徑會依「有沒有分類」自動補上 manual_override——這是 D1
    # 補完台帳之後 ISO_ZONE 能不能真的開始判定的關鍵
    from validate.points import _build_device_context
    ctx_from_ledger = _build_device_context(
        'ZP-TEST', {'Name': 'ZP-TEST', 'RPM': 1775},
        {'iso_machine_group': '3', 'iso_foundation': 'rigid'}, None)
    check("台帳只給群組＋基礎剛性時，class_source 自動成為 manual_override",
          ctx_from_ledger.iso_class_source == 'manual_override',
          ctx_from_ledger.iso_class_source)
    res = evaluate_iso(pd.DataFrame([{'ts_hour': NOW, 'data_status': 'ok', 'vel_rms': 3.2}]),
                       ctx_from_ledger, None)
    check("缺額定功率不影響 Zone 判定成立（缺資料 ≠ 超出範圍）",
          res.applicable and res.zone == 'B' and res.machine_class == '3/rigid',
          f"applicable={res.applicable} zone={res.zone} note={res.note}")

    for kw, keyword in (({'rated_power_kw': 11.0}, '15 kW'), ({'rated_rpm': 60.0}, 'rpm')):
        res = evaluate_iso(pd.DataFrame([{'ts_hour': NOW, 'data_status': 'ok', 'vel_rms': 1.0}]),
                           device(iso_machine_group='2', iso_foundation='rigid', **kw), None)
        check(f"適用範圍外不判定（{keyword}）",
              not res.applicable and keyword in res.note, res.note)

    check("台帳缺功率/轉速時不擋（缺資料 ≠ 超出範圍）",
          iso_scope_reason(device(rated_power_kw=None, rated_rpm=None)) is None)

    th = iso_alert_threshold(2.0, ('3', 'rigid'))
    check("ALARM = 基準 + 0.25 × Zone B 上限（條文範例：2.0 + 0.25×4.5 = 3.125）",
          abs(th - 3.125) < 1e-9, str(th))
    check("ALARM 封頂於 1.25 × Zone B 上限（5.625）",
          abs(iso_alert_threshold(99.0, ('3', 'rigid')) - 5.625) < 1e-9)
    check("門檻隨分類變動（Group2/rigid 1.150 vs Group3/rigid 1.575）",
          abs(iso_alert_threshold(0.45, ('2', 'rigid')) - 1.150) < 1e-9
          and abs(iso_alert_threshold(0.45, ('3', 'rigid')) - 1.575) < 1e-9)

    check("resolve_class 需要兩項齊備",
          resolve_class(device(iso_machine_group='2', iso_foundation='rigid')) == ('2', 'rigid')
          and resolve_class(device(iso_machine_group='2')) is None)


# ──────────────────────────────────────────────────────────
# 三、持續性緩衝
# ──────────────────────────────────────────────────────────

def test_persistence() -> None:
    print("\n[3] 持續性緩衝（ISO 10816-3 §5.4 實務建議）")

    stats = {'vel_rms': (0.45, 0.45, 0.1, 300), 'vel_oa': (0.45, 0.45, 0.1, 300)}
    rows = lambda vs: [{'vel_rms': v, 'vel_oa': v} for v in vs]

    # Group2/rigid：B/C 界 2.8 → Zone C 需 > 2.8；ISO 告警門檻 = 0.45+0.7 = 1.15
    check("ISO_ZONE：單筆尖峰不觸發", not iso_zone(ctx(rows([1.0, 1.0, 3.5]), stats)).triggered)
    r = iso_zone(ctx(rows([3.5, 3.6, 3.5]), stats))
    check("ISO_ZONE：連續三筆達 Zone C 才觸發",
          r.triggered and r.evidence['recent_zones'] == ['C', 'C', 'C'],
          str(r.evidence.get('recent_zones')))
    check("ISO_ZONE：可信資料不足 N 筆視為證據不足，不觸發",
          not iso_zone(ctx(rows([3.5, 3.6]), stats)).triggered)
    check("ISO_ZONE：緩衝可調（設為 1 即回到單筆判定）",
          iso_zone(ctx(rows([1.0, 1.0, 3.5]), stats, {'consecutive_readings': 1})).triggered)

    check("VEL_HIGH：單筆尖峰不觸發", not vel_high(ctx(rows([0.5, 0.5, 2.0]), stats)).triggered)
    r = vel_high(ctx(rows([2.0, 2.1, 2.0]), stats))
    check("VEL_HIGH：連續三筆超標才觸發",
          r.triggered and r.evidence['recent_values'] == [2.0, 2.1, 2.0],
          str(r.evidence.get('recent_values')))


# ──────────────────────────────────────────────────────────
# 四、基準期與保養
# ──────────────────────────────────────────────────────────

def test_baseline_maintenance() -> None:
    print("\n[4] 基準期不得早於最後一次保養（ISO 10816-3 §5.4.1）")

    start = pd.Timestamp('2026-06-01', tz='UTC')
    hours = pd.date_range(start, periods=60 * 24, freq='h')
    maint = pd.Timestamp('2026-07-01', tz='UTC')
    rng = np.random.default_rng(0)
    vals = np.where(hours < maint, 3.0, 1.0) + rng.normal(0, 0.02, len(hours))
    agg = pd.DataFrame({'ts_hour': hours, 'data_status': 'ok', 'vel_rms': vals,
                        'acc_rms': vals * 0.5, 'completeness': 1.0})

    b = detect_baseline(agg, point_id=1, not_before=maint.to_pydatetime())
    check("基準期起點落在保養之後", b is not None and b.start_date >= maint.date(),
          str(b.start_date) if b else 'None')
    check("統計量反映保養後的水準（1.0 而非保養前的 3.0）",
          abs(b.stats['vel_rms'].median - 1.0) < 0.1, str(b.stats['vel_rms'].median))
    check("保養後資料不足時回傳 None（不硬湊）",
          detect_baseline(agg, point_id=1,
                          not_before=pd.Timestamp('2026-07-25', tz='UTC').to_pydatetime()) is None)
    check("保養後完全無資料時回傳 None",
          detect_baseline(agg, point_id=1,
                          not_before=pd.Timestamp('2027-01-01', tz='UTC').to_pydatetime()) is None)
    check("tz-naive 的 not_before 不會因時區型別出錯",
          detect_baseline(agg, point_id=1, not_before=dt.datetime(2026, 7, 1)) is not None)


# ──────────────────────────────────────────────────────────
# 五、護欄
# ──────────────────────────────────────────────────────────

def test_axis_direction() -> None:
    print("\n[5] 感測器軸向（Channel_X/Y/Z 的 4/5/6）")

    check("AHU-601 的 (4,6,5) 解析正確",
          resolve_axis_directions({'Channel_X': 4, 'Channel_Y': 6, 'Channel_Z': 5})
          == {'x': 'vertical_radial', 'y': 'horizontal_radial', 'z': 'axial'})
    check("泵的 (4,5,6) 解析正確——與 AHU 的 Y/Z 相反，證明不可用位置推斷",
          resolve_axis_directions({'Channel_X': 4, 'Channel_Y': 5, 'Channel_Z': 6})
          == {'x': 'vertical_radial', 'y': 'axial', 'z': 'horizontal_radial'})
    check("字串型別也能解析", resolve_axis_directions(
          {'Channel_X': '4', 'Channel_Y': '5', 'Channel_Z': '6'}) is not None)
    for bad, why in (({'Channel_X': 4, 'Channel_Y': 5, 'Channel_Z': 5}, '代碼重複'),
                     ({'Channel_X': 4, 'Channel_Y': 5, 'Channel_Z': 9}, '未知代碼'),
                     ({'Channel_X': 4, 'Channel_Y': 5}, '缺欄位'),
                     ({'Channel_X': None, 'Channel_Y': 5, 'Channel_Z': 6}, '空值')):
        check(f"{why}時回傳 None（不猜方向）", resolve_axis_directions(bad) is None)

    src = 'data/Analytic.csv'
    if os.path.exists(src):
        d = pd.read_csv(src, sep='\t', encoding='utf-8-sig', low_memory=False)
        out = _aggregate_running(d)
        bd = out['axis_energy_by_direction']
        check("聚合輸出依方向的佔比", bd is not None and 'axial' in bd, str(bd))
        check("三個方向佔比加總為 1",
              abs(sum(bd[k] for k in ('axial', 'vertical_radial', 'horizontal_radial')) - 1.0) < 1e-3)
        check("axial_ratio 等於 axial 佔比", abs(bd['axial_ratio'] - bd['axial']) < 1e-9)
        check("排序版仍並存（供 Channel 未設定的設備退回使用）",
              out['axis_energy_sorted'] is not None)
        check("排序版的 major 等於依方向的最大值",
              abs(out['axis_energy_sorted']['major']
                  - max(bd[k] for k in ('axial', 'vertical_radial', 'horizontal_radial'))) < 1e-3)
        check("有記錄衝擊最強的方向",
              out['acc_kurt_max_direction'] in
              ('axial', 'vertical_radial', 'horizontal_radial'),
              str(out['acc_kurt_max_direction']))
    else:
        skip("以真實資料驗證方向聚合", f"找不到 {src}")

    # Channel 缺失時整組退回 None，但排序版仍要算得出來
    d2 = pd.DataFrame({'accRMS_x': [1.0, 1.0], 'accRMS_y': [2.0, 2.0], 'accRMS_z': [3.0, 3.0]})
    out2 = _aggregate_running(d2)
    check("Channel 欄位不存在時方向為 None，排序版仍可用",
          out2['axis_energy_by_direction'] is None and out2['axis_energy_sorted'] is not None)


def test_backtest_instrumentation() -> None:
    """回測輸出的可判讀性——這兩項不改變任何判定，只讓結果解讀得出來。"""
    print("\n[6] 回測輸出：證據欄位與掃描的總持續天數")

    from validate.backtest import (_EVIDENCE_FLAT_KEYS, _episode_evidence,
                                   _make_episode_row)

    # 沒有 outcome（理論上不會發生，但不該讓整份報告炸掉）
    empty = _episode_evidence(None)
    check("outcome 為 None 時所有證據欄位為 None，不拋錯",
          all(empty[k] is None for k in _EVIDENCE_FLAT_KEYS)
          and empty['evidence_json'] is None)

    # VEL_HIGH：判讀「這件是 ISO 判定還是退回相對基準」靠的就是 threshold_mode
    stats = {'vel_rms': (0.45, 0.45, 0.1, 300), 'vel_oa': (0.45, 0.45, 0.1, 300)}
    rows = [{'vel_rms': v, 'vel_oa': v} for v in (2.0, 2.1, 2.0)]
    out = vel_high(ctx(rows, stats))
    ev = _episode_evidence(out)
    check("VEL_HIGH 事件記錄 threshold_mode", ev['threshold_mode'] == 'iso', str(ev['threshold_mode']))
    check("VEL_HIGH 事件記錄 machine_class（可驗證分類假設是否套用）",
          ev['machine_class'] == '2/rigid', str(ev['machine_class']))
    check("VEL_HIGH 事件記錄持續性緩衝筆數", ev['consecutive_readings'] == 3)

    # 未分類設備應標記為 sigma_fallback——這正是上一輪判讀不出來的那件事
    out2 = vel_high(ctx(rows, stats, dev=device(iso_machine_group=None, iso_foundation=None,
                                                iso_class_source='unset')))
    check("未分類設備的事件標記為 sigma_fallback",
          _episode_evidence(out2)['threshold_mode'] == 'sigma_fallback',
          str(_episode_evidence(out2)['threshold_mode']))

    # IMPACT_RISE：判讀走的是 median 通道還是退回 max
    imp = impact_rise(ctx([{'acc_crest_median': 3.4}], {'acc_crest_median': (2.6, 2.6, 0.3, 300)}))
    ev3 = _episode_evidence(imp)
    check("IMPACT_RISE 事件記錄實際採用的欄位（median 或退回 max）",
          ev3['primary_metric'] == 'acc_crest_median', str(ev3['primary_metric']))
    check("完整 evidence 保留在 evidence_json",
          ev3['evidence_json'] and 'channels' in ev3['evidence_json'])

    # 事件列本身要帶上這些欄位
    class _P:
        class point:
            class device:
                device_id, device_name = 'T', 'T'
            point_id, position = 1, 'M1'
    class _R:
        rule_code = rule_name = family = issue_type = 'X'
        severity = 'warn'
    row = _make_episode_row(_P, _R, pd.Timestamp('2026-08-01'), pd.Timestamp('2026-08-03'), out)
    for k in ('threshold_mode', 'machine_class', 'evidence_json', 'duration_days'):
        check(f"事件列含 {k} 欄位", k in row)
    check("duration_days 含頭尾（8/01–8/03 為 3 天）", row['duration_days'] == 3)


def test_guardrail() -> None:
    print("\n[7] 診斷性用語護欄")

    stats = {'vel_rms': (0.45, 0.45, 0.1, 300), 'vel_oa': (0.45, 0.45, 0.1, 300)}
    rows = lambda vs: [{'vel_rms': v, 'vel_oa': v} for v in vs]
    outcomes = [
        iso_zone(ctx(rows([3.5, 3.6, 3.5]), stats)),
        vel_high(ctx(rows([2.0, 2.1, 2.0]), stats)),
        impact_rise(ctx([{'acc_crest_median': 3.4}], {'acc_crest_median': (2.6, 2.6, 0.3, 300)})),
    ]
    for o in outcomes:
        problems = check_outcome(o)
        check(f"{o.rule_code} 的輸出文字無診斷性斷言", not problems, str(problems))

    check("ISO 分類術語「剛性基礎」不誤判為故障詞彙",
          not check_text('Group 2 中型機 · 剛性基礎，依群組與基礎剛性判定'))
    check("真正的故障斷言仍被擋下", bool(check_text('疑似基礎鬆動')))
    check("帶免責語的可能性列舉仍放行",
          not check_text('可能源自基礎鬆動或負載變化等多種原因，本系統無法區分'))


# ──────────────────────────────────────────────────────────
# 疑似系統層級中斷的偵測
# ──────────────────────────────────────────────────────────

def test_systemic_gaps() -> None:
    print("\n[8] 疑似系統層級中斷（多點共用同一斷線邊界）")

    from validate.report import (_SUSTAINED_MIN_HOURS, _SYSTEMIC_MIN_POINTS,
                                 _SYSTEMIC_TOLERANCE_H, _poisson_sf,
                                 detect_systemic_gaps, summarize_brief_outages)

    # 重現 2026-08-03 那次：14 個點結束於同一刻，起始散在數小時內
    rows = [{'device_id': f'DEV{i:02d}', 'position': 'M1',
             'gap_start': pd.Timestamp('2026-07-07 12:00') + dt.timedelta(hours=i % 4),
             'gap_end': pd.Timestamp('2026-08-03 08:00'),
             'hours': 643.0, 'status': 'no_data'} for i in range(14)]
    # 單點長時間故障——不該被歸為系統事件
    rows.append({'device_id': 'LONE', 'position': 'M1',
                 'gap_start': pd.Timestamp('2026-06-18 19:00'),
                 'gap_end': pd.Timestamp('2026-08-07 20:00'),
                 'hours': 1201.0, 'status': 'no_data'})
    out = detect_systemic_gaps(pd.DataFrame(rows))

    check("抓到同時結束的那一組", (out['kind'] == 'end').any(), str(out['kind'].tolist()))
    check("同時結束組的點數正確（14 點）",
          int(out[out['kind'] == 'end']['n_points'].iloc[0]) == 14)
    check("起始散在容忍窗內仍併成同一組",
          int(out[out['kind'] == 'start']['n_points'].iloc[0]) == 14)
    check("單點長時間故障不被誤判為系統事件",
          'LONE' not in out['points'].str.cat(sep='|'))

    # 低於門檻的不報——2 個點同時斷線可能只是巧合
    few = pd.DataFrame([
        {'device_id': f'D{i}', 'position': 'M1',
         'gap_start': pd.Timestamp('2026-05-01 00:00'),
         'gap_end': pd.Timestamp('2026-05-02 00:00'),
         'hours': 24.0, 'status': 'no_data'}
        for i in range(_SYSTEMIC_MIN_POINTS - 1)])
    check(f"少於 {_SYSTEMIC_MIN_POINTS} 個點不報（避免把巧合講成系統事件）",
          detect_systemic_gaps(few).empty)

    # 邊界差距超過容忍窗就該分成兩組
    spread = pd.DataFrame([
        {'device_id': f'S{i}', 'position': 'M1',
         'gap_start': pd.Timestamp('2026-05-01 00:00'),
         'gap_end': pd.Timestamp('2026-05-02 00:00') + dt.timedelta(
             hours=0 if i < 3 else _SYSTEMIC_TOLERANCE_H * 4),
         'hours': 24.0, 'status': 'no_data'} for i in range(6)])
    ends = detect_systemic_gaps(spread)
    ends = ends[ends['kind'] == 'end']
    check("相隔超過容忍窗的邊界不會被併成同一組",
          len(ends) == 2 and set(ends['n_points']) == {3},
          str(ends[['boundary', 'n_points']].to_dict('records')))

    check("空表不拋錯", detect_systemic_gaps(pd.DataFrame()).empty)

    # ── 兩層分流：實測 66 點 2.6 週跑出 106 組，其中絕大多數是每天好幾次
    # 的 1 小時全廠停頓。混在同一張清單裡逐組列出，真正要處理的那幾筆
    # 會被淹沒——所以依時長分成兩層，短的改用頻率呈現。
    mixed = list(rows)
    base = pd.Timestamp('2026-08-04 00:00')
    for day in range(18):
        for k in range(6):          # 每天 6 次
            t = base + dt.timedelta(days=day, hours=k * 4)
            for i in range(30):     # 每次 30 個點
                mixed.append({'device_id': f'P{i:02d}', 'position': 'M1',
                              'gap_start': t, 'gap_end': t + dt.timedelta(hours=1),
                              'hours': 1.0, 'status': 'no_data'})
    mixed_df = pd.DataFrame(mixed)
    sy = detect_systemic_gaps(mixed_df)

    sustained = sy[sy['tier'] == 'sustained']
    check(f"643 小時的中斷歸為 sustained（≥ {_SUSTAINED_MIN_HOURS:.0f} 小時）",
          len(sustained) == 2 and set(sustained['n_points']) == {14},
          str(sustained[['kind', 'n_points', 'median_hours']].to_dict('records')))
    check("1 小時的全廠停頓歸為 brief，不混進要處理的清單",
          (sy[sy['tier'] == 'brief']['median_hours'] < _SUSTAINED_MIN_HOURS).all())
    check("持續性中斷排在最前面（tier 升冪，sustained < brief）",
          sy['tier'].iloc[0] == 'sustained', str(sy['tier'].head(3).tolist()))

    brief = summarize_brief_outages(mixed_df, period_days=18.0)
    # 頻率要直接從 gaps 以整點重數——沿用 ±6 小時容忍窗的分叢結果會把
    # 相隔 4 小時的相鄰兩次併成一次，把每天 6 次低估成 3 次
    check("短暫停頓的每日次數沒有被容忍窗低估（真值 6.0/天）",
          abs(brief['per_day'] - 6.0) < 0.2, f"{brief['per_day']:.2f}")
    check("短暫停頓的影響點數正確（30 點）", brief['median_points'] == 30.0)
    check("有算出佔全部缺口的比例", 0.0 < brief['missing_share'] < 1.0,
          str(brief['missing_share']))
    check("沒有短暫停頓時回傳 None",
          summarize_brief_outages(pd.DataFrame(rows), period_days=18.0) is None)

    # ── 巧合基準：每個點本來就會零星斷線，光靠機率就會出現「幾個點剛好
    # 同一小時斷線」。實測 66 點 2.6 週、門檻取 3 點時，光是巧合就有約
    # 66 次，而總共只偵測到 146 次——將近一半是假的。把巧合報給 IT
    # 當成系統問題，他們會查不到東西。
    days_r, n_pts_r = 18.2, 66
    rng_r = np.random.default_rng(1)
    noise = []
    for h in range(int(days_r * 24)):
        t = pd.Timestamp('2026-08-03') + dt.timedelta(hours=h)
        for i in rng_r.choice(n_pts_r, size=rng_r.poisson(1.34), replace=False):
            noise.append({'device_id': f'P{i:02d}', 'position': 'M1', 'gap_start': t,
                          'gap_end': t + dt.timedelta(hours=1), 'hours': 1.0})
    check("純隨機的零星斷線不會被報成系統性問題",
          summarize_brief_outages(pd.DataFrame(noise), period_days=days_r) is None)

    # 同一份雜訊 + 每天 2 次真正的 25 點同時停頓 → 要抓得出來且數字要準
    planted = list(noise)
    for day in range(18):
        for hh in (3, 15):
            t = pd.Timestamp('2026-08-03') + dt.timedelta(days=day, hours=hh)
            for i in range(25):
                planted.append({'device_id': f'P{i:02d}', 'position': 'M1', 'gap_start': t,
                                'gap_end': t + dt.timedelta(hours=1), 'hours': 1.0})
    got = summarize_brief_outages(pd.DataFrame(planted), period_days=days_r)
    check("真正的系統性停頓仍抓得出來（真值 2.0/天）",
          got is not None and abs(got['per_day'] - 2.0) < 0.2,
          None if got is None else f"{got['per_day']:.2f}")
    check("影響點數還原正確（真值 25 點）", got['median_points'] == 25.0)
    check("門檻自動升高到巧合可忽略的位置",
          got['threshold_points'] > _SYSTEMIC_MIN_POINTS, str(got['threshold_points']))
    check("超出巧合的次數遠大於巧合本身",
          got['n_excess'] > got['n_expected_by_chance'] * 5,
          f"excess={got['n_excess']} chance={got['n_expected_by_chance']}")

    # 逐筆時間戳：只給「每天幾次」IT 無從查起，要能拿去對 log
    ev = got['events']
    check("有回傳逐筆事件供 IT 對 log", len(ev) == got['n_events'], str(len(ev)))
    check("事件時間戳還原出植入的規律（每天 03:00 與 15:00）",
          set(pd.to_datetime(ev['boundary']).dt.hour.unique()) == {3, 15},
          str(sorted(pd.to_datetime(ev['boundary']).dt.hour.unique())))
    check("事件明細帶得出寫 CSV 需要的欄位",
          {'boundary', 'kind', 'n_points', 'median_hours', 'tier'} <= set(ev.columns),
          str(list(ev.columns)))


# ──────────────────────────────────────────────────────────
# STEP_CHANGE 的特徵集可覆寫（對照回測用）
# ──────────────────────────────────────────────────────────

def test_step_change_features() -> None:
    print("\n[9] STEP_CHANGE：特徵集可由 params 覆寫")

    from vibcore.rules.metric_rules import step_change

    # STEP_CHANGE 會用基準期內的資料現場擬合協方差，所以資料列必須真的
    # 落在基準期區間裡——共用的 ctx() 把資料放在 NOW 附近、基準期卻在
    # 8/1~8/15，兩者不重疊會讓模型擬合失敗而靜默不觸發。這裡自己組。
    base_day = dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc)
    rng = np.random.default_rng(3)
    agg_rows = []
    for i in range(24):      # 基準期：24 小時的正常資料，帶一點雜訊才估得出協方差
        n = rng.normal(0, 0.02, 4)
        agg_rows.append({'ts_hour': base_day + dt.timedelta(hours=i), 'data_status': 'ok',
                         'vel_rms': 1.0 + n[0], 'acc_rms': 0.5 + n[1],
                         'acc_crest': 3.0 + n[2], 'acc_kurt': 2.5 + n[3]})
    # 基準期之後的一筆明顯偏離
    agg_rows.append({'ts_hour': base_day + dt.timedelta(days=1, hours=12), 'data_status': 'ok',
                     'vel_rms': 3.0, 'acc_rms': 2.0, 'acc_crest': 6.0, 'acc_kurt': 9.0})
    agg = pd.DataFrame(agg_rows)
    stats = {'vel_rms': (1.0, 1.0, 0.02, 300), 'acc_rms': (0.5, 0.5, 0.02, 300),
             'acc_crest': (3.0, 3.0, 0.02, 300), 'acc_kurt': (2.5, 2.5, 0.02, 300)}
    baseline = BaselineStats(
        point_id=1, start_date=dt.date(2026, 8, 25), end_date=dt.date(2026, 8, 25),
        source='auto', stats={k: MetricStats(*v) for k, v in stats.items()}, n_hours=24)

    def sc_ctx(params: dict | None = None) -> RuleContext:
        return RuleContext(
            device=device(iso_machine_group='2', iso_foundation='rigid'),
            point_id=1, position='M1', agg=agg, baseline=baseline,
            params=params or {}, now=base_day + dt.timedelta(days=1, hours=13))

    r4 = step_change(sc_ctx())
    check("預設用四個特徵",
          r4.triggered and r4.evidence['n_features'] == 4, str(r4.evidence.get('features')))

    r3 = step_change(sc_ctx({'features': ['vel_rms', 'acc_rms', 'acc_crest'],
                             'mahalanobis_sigma': 2.71}))
    check("params 指定時只用指定的特徵",
          r3.triggered and r3.evidence['n_features'] == 3
          and 'acc_kurt' not in r3.evidence['features'],
          str(r3.evidence.get('features')))

    # 卡方等效門檻：k=4 的 3.0（尾機率 0.0611）對應 k=3 的 2.71。
    # 設定檔裡那張對照表若被改壞，這裡會抓到。
    import json
    with open('validate/rule_configs/step_change_without_kurt.json', encoding='utf-8') as f:
        cfg = json.load(f)
    check("對照設定檔的特徵集確實少了 acc_kurt",
          cfg['STEP_CHANGE']['features'] == ['vel_rms', 'acc_rms', 'acc_crest'])
    check("對照設定檔的預設門檻用的是卡方等效值 2.71",
          abs(cfg['STEP_CHANGE']['mahalanobis_sigma'] - 2.71) < 1e-9)
    try:
        from scipy import stats as sp_stats
        ok = True
        for d4_str, d3 in cfg['_等效門檻對照']['k=4 → k=3'].items():
            p_tail = sp_stats.chi2.sf(float(d4_str) ** 2, df=4)
            want = float(np.sqrt(sp_stats.chi2.isf(p_tail, df=3)))
            ok = ok and abs(want - d3) < 0.01
        check("等效門檻對照表與卡方分布算出來的一致", ok)
    except ImportError:
        skip("等效門檻對照表驗算", "沒有 scipy")

    # 底線開頭是註解鍵，不該被當成規則代碼
    from validate.rule_defaults import load_rule_configs
    cfgs = load_rule_configs('validate/rule_configs/step_change_without_kurt.json')
    # 掉特徵必須看得出來：門檻的稀有程度隨特徵數而變，某點少一個特徵時
    # 同一個門檻對它比較鬆，跨點就不可比——而且從 n_features 一個數字
    # 分不出「設定檔指定三特徵」與「第四個安靜掉了」。
    r4m = step_change(sc_ctx())
    check("四特徵齊全時 features_missing 為空", r4m.evidence['features_missing'] == {})
    check("四特徵齊全時 features_requested 記錄四個",
          len(r4m.evidence['features_requested']) == 4)

    agg_drop = agg.drop(columns=['acc_kurt'])
    bl_drop = BaselineStats(
        point_id=1, start_date=dt.date(2026, 8, 25), end_date=dt.date(2026, 8, 25),
        source='auto',
        stats={k: v for k, v in baseline.stats.items() if k != 'acc_kurt'}, n_hours=24)
    r_drop = step_change(RuleContext(
        device=device(iso_machine_group='2', iso_foundation='rigid'),
        point_id=1, position='M1', agg=agg_drop, baseline=bl_drop,
        params={}, now=base_day + dt.timedelta(days=1, hours=13)))
    check("特徵安靜掉了會被記錄下來（不是只剩一個數字）",
          r_drop.evidence['n_features'] == 3
          and 'acc_kurt' in r_drop.evidence['features_missing'],
          str(r_drop.evidence.get('features_missing')))
    check("缺特徵的原因有寫清楚",
          '欄位' in r_drop.evidence['features_missing']['acc_kurt'],
          str(r_drop.evidence['features_missing']['acc_kurt']))
    check("分得出「設定檔指定三特徵」與「第四個掉了」",
          len(r3.evidence['features_requested']) == 3
          and r3.evidence['features_missing'] == {}
          and len(r_drop.evidence['features_requested']) == 4,
          f"B={r3.evidence['features_requested']} drop={r_drop.evidence['features_requested']}")

    check("設定檔的註解鍵不會被當成未知規則",
          cfgs['STEP_CHANGE'].params.get('features') == ['vel_rms', 'acc_rms', 'acc_crest'],
          str(cfgs['STEP_CHANGE'].params))


# ──────────────────────────────────────────────────────────
# SCADA 對應與時間對齊（Q3 第一階段）
# ──────────────────────────────────────────────────────────

def test_scada() -> None:
    print("\n[10] SCADA：tag 對應、有效樣本數與時間對齊")

    import tempfile

    from vibcore.io.scada import (REFRESH_TOLERANCE, SCADA_REFRESH_MINUTES,
                                  STALENESS_TOLERANCE_MINUTES, attach_to_agg,
                                  effective_sample_count, emit_tagmap_template,
                                  describe_speed, estimate_refresh_minutes,
                                  parse_readings, parse_tagmap, profile_tag,
                                  resolve_reading_paths)

    # ── 對應表：範本預填、型別把關 ──────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'tagmap.csv')
        n = emit_tagmap_template(
            {'ZP 3-5_M1': 'FACC|K12_ZP350_INV_I', 'CP 10_M1': None}, path)
        check("範本每台設備兩列（電流＋頻率）", n == 4, str(n))
        written = pd.read_csv(path, encoding='utf-8-sig', dtype=str)
        cur = written[(written['device_id'] == 'ZP 3-5_M1')
                      & (written['variable_type'] == 'current')]
        check("Label 有值的設備預填成 current",
              len(cur) == 1 and cur.iloc[0]['tag_id'] == 'FACC|K12_ZP350_INV_I')
        check("預填列有標明「請確認」（型別是從 tag 命名猜的）",
              '確認' in str(cur.iloc[0]['note']), str(cur.iloc[0]['note']))
        check("沒有 Label 的設備留白等人填",
              written[(written['device_id'] == 'CP 10_M1')
                      & written['tag_id'].isna()].shape[0] == 2)
        # 範本預填的那一列本來就是有效的——預填的用意就是讓它直接可用。
        # 其餘三列（缺 tag_id）視為待填，不逐列噴警告。
        fresh = parse_tagmap(path)
        check("剛產出的範本，預填那列直接可用、其餘視為待填",
              len(fresh) == 1 and fresh[0].device_id == 'ZP 3-5_M1'
              and fresh[0].variable_type == 'current',
              str([(m.device_id, m.variable_type) for m in fresh]))

        # 填好之後
        with open(path, 'w', encoding='utf-8-sig') as f:
            f.write('device_id,tag_id,variable_type,unit,is_active,note\n')
            f.write('ZP 3-5_M1,T.I,current,A,TRUE,\n')
            f.write('ZP 3-5_M1,T.F,frequency,Hz,TRUE,\n')
            f.write('CP 10_M1,T.BAD,電流,A,TRUE,型別用中文\n')
            f.write('CP 10_M1,,frequency,Hz,,還沒填\n')
        got = parse_tagmap(path)
        check("合法的兩列讀得回來", len(got) == 2, str([m.tag_id for m in got]))

        # 一台馬達的驅動端（M1）與非驅動端（M2）是兩個量測點，卻共用同一組
        # 電流與頻率。早期假設「一個 tag 只屬於一台設備」，實際的對應表
        # 一寫就會撞到。
        with open(path, 'w', encoding='utf-8-sig') as f:
            f.write('device_id,tag_id,variable_type,unit,is_active,note\n')
            f.write('CWP 01_M1,FACC|CWP01_INV_I,current,A,TRUE,\n')
            f.write('CWP 01_M2,FACC|CWP01_INV_I,current,A,TRUE,\n')      # 共用
            f.write('CWP 01_M2,cwp01_inv_i,current,A,TRUE,\n')            # 同一列換寫法：重複
            f.write('CWP 01_M2,FACC|CWP01_INV_I,frequency,Hz,TRUE,\n')    # 同訊號填成別的型別
        shared = parse_tagmap(path)
        pairs = sorted((m.device_id, m.variable_type) for m in shared)
        check("同一個 tag 對應多台設備是正常的，兩台都保留",
              pairs == [('CWP 01_M1', 'current'), ('CWP 01_M2', 'current')], str(pairs))
        check("同一台設備的重複列（換了寫法）靜默去重", len(shared) == 2, str(len(shared)))
        check("同一個訊號被填成兩種型別時拒收那一列（不可能同時成立）",
              all(m.variable_type == 'current' for m in shared))
        check("同一個訊號只留一種寫法（資料庫只能有一種）",
              len({m.tag_id for m in shared}) == 1, str({m.tag_id for m in shared}))
        check("不在允許值內的 variable_type 被拒收，不自動對應",
              all(m.tag_id != 'T.BAD' for m in got),
              "「電流」被當成 current 了——猜錯型別會讓頻率被當電流用")

    # ── 有效樣本數：一列 ≠ 一個獨立樣本 ───────────────────
    # 這是本模組存在的主要理由之一：匯出檔每分鐘一列，但值約 10 分鐘才真的
    # 更新一次，直接拿列數當樣本數會高估一個數量級。期望值由常數推導而不是
    # 寫死——刷新週期曾由口述的 15 分鐘依實測改為 10 分鐘，寫死的數字
    # 會在改常數時無謂地失敗，也掩蓋「測的是公式不是某個數字」。
    n_rows, span = 2000, 2000 * 2.0
    eff = effective_sample_count(n_rows, span)
    check("有效樣本數 = 時間跨度能容納幾個刷新週期，而不是列數",
          eff == int(span // SCADA_REFRESH_MINUTES) and eff < n_rows,
          f"eff={eff}, 列數={n_rows}")
    check("資料稀疏時不因公式而虛報（列數才 5 筆）",
          effective_sample_count(5, span) == 5)
    check("跨度為 0 或列數為 0 時回 0（不除以零）",
          effective_sample_count(0, span) == 0 and effective_sample_count(10, 0) == 0)

    # ── 刷新週期的估計與判定 ────────────────────────────────
    # 先前 probe 用「差距超過 5 分鐘」判相符：實測 10.2 對假設的 15 差 4.8，
    # 剛好沒過門檻，於是打了「✓ 相符」——實際差了三分之一。改成相對容忍度。
    measured, assumed = 10.2, 15.0
    check("回歸：舊的絕對門檻會把 10.2 對 15 誤判為相符",
          abs(measured - assumed) <= 5)
    check("新的相對門檻能抓到這個落差",
          abs(measured - assumed) / assumed > REFRESH_TOLERANCE,
          f"{abs(measured - assumed) / assumed:.2f} vs {REFRESH_TOLERANCE}")
    check("程式內的刷新週期與 2026-06 實測（10.2 分鐘）相符",
          abs(measured - SCADA_REFRESH_MINUTES) / SCADA_REFRESH_MINUTES <= REFRESH_TOLERANCE,
          str(SCADA_REFRESH_MINUTES))

    # ── 訊號輪廓：不變動的 tag 要分得出是關機、定頻還是凍結 ─────
    rng = np.random.default_rng(2)
    minutes = pd.date_range('2026-06-01', periods=14 * 1440, freq='min')

    def held(period, gen):
        out, cur = np.empty(len(minutes)), gen()
        for i in range(len(minutes)):
            if i % period == 0:
                cur = gen()
            out[i] = cur
        return out

    def frame(v):
        return pd.DataFrame({'ts': minutes, 'value': v})

    p_fast = profile_tag('fast', frame(held(10, lambda: float(rng.normal(60, 4)))))
    p_slow = profile_tag('slow', frame(held(110, lambda: float(rng.choice([42., 45., 50.])))))
    p_zero = profile_tag('zero', frame(np.zeros(len(minutes))))
    p_const = profile_tag('const', frame(np.full(len(minutes), 50.0)))
    p_rare = profile_tag('rare', frame(np.r_[np.full(5000, 59.9), np.full(len(minutes) - 5000, 60.0)]))

    check("持續變動的 tag 歸為 varying", p_fast.kind == 'varying', p_fast.kind)
    check("恆為 0 歸為 constant_zero（設備沒開機）", p_zero.kind == 'constant_zero', p_zero.kind)
    check("恆為非零歸為 constant_nonzero（定頻或凍結）",
          p_const.kind == 'constant_nonzero', p_const.kind)
    check("只變動幾次的歸為 rare", p_rare.kind == 'rare', p_rare.kind)
    check("變得慢的訊號（頻率設定值）不拿來估刷新週期",
          p_fast.tracks_refresh and not p_slow.tracks_refresh,
          f"fast={p_fast.median_gap_min}, slow={p_slow.median_gap_min}")

    # ── 頻率 tag：只看運轉時，停機不能算成「轉速有變化」 ──────
    # 一台 40% 時間開機、開機時固定 50 Hz 的泵，含停機的 p5–p95 是 0–50 Hz，
    # 單看範圍會誤判成變頻調速。這個陷阱就是 describe_speed 存在的原因。
    n_min = len(minutes)
    on = np.zeros(n_min, bool)
    on[:int(n_min * 0.4)] = True
    p_dutycycle = profile_tag('duty', frame(np.where(on, 50.0, 0.0)))
    p_vfd = profile_tag('vfd', frame(np.where(on, rng.normal(50, 4, n_min).round(1), 0.0)))
    p_off = profile_tag('off', frame(np.r_[np.full(n_min - 30, 0.0), np.full(30, 50.0)]))
    p_small = profile_tag('small', frame(np.where(on, 50.0 + rng.normal(0, 0.3, n_min).round(1), 0.0)))

    note, ok = describe_speed(p_dutycycle)
    check("40% 時間開機、開機時固定 50 Hz → 定頻，不算有轉速變化",
          not ok and '定頻' in note, note)
    check("同一支 tag 含停機的 p5–p95 確實會被 0 拉寬（陷阱真的存在）",
          p_dutycycle.v_p05 == 0.0 and p_dutycycle.v_p95 == 50.0,
          f"{p_dutycycle.v_p05}–{p_dutycycle.v_p95}")
    note, ok = describe_speed(p_vfd)
    check("開機時轉速真的在變（σ≈4 Hz）→ 有足夠變動", ok and '有變化' in note, note)
    check("運轉佔比正確（約 40%）", abs(p_vfd.run_share - 0.4) < 0.01, str(p_vfd.run_share))
    note, ok = describe_speed(p_off)
    check("幾乎沒開機 → 無從判斷，不給「有變化」", not ok and '沒開機' in note, note)
    note, ok = describe_speed(p_small)
    check("運轉時只有 ±0.3 Hz 的抖動 → 變化很小，不值得驗證",
          not ok and '很小' in note, note)
    check("整段都是 0 的 tag：沒有運轉統計，不會丟例外",
          describe_speed(p_zero)[1] is False and p_zero.run_median is None)

    est, n_used = estimate_refresh_minutes([p_fast, p_slow, p_zero, p_const, p_rare])
    check("刷新週期只由持續變動的 tag 估出（約 10 分鐘，且只用 1 個 tag）",
          est is not None and abs(est - 10.0) < 1.0 and n_used == 1, f"{est}, n={n_used}")
    check("沒有任何 tag 夠格時回 None 而不是亂估",
          estimate_refresh_minutes([p_zero, p_const, p_rare]) == (None, 0))

    # ── 讀值來源可以是資料夾：不用先手動合併 ──────────────────
    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.msgs: list[str] = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    def read_quiet(path):
        # 測試套件把 root 設成 ERROR；要接到 WARNING 得調這個 logger 自己的層級
        h, lg = _Capture(), logging.getLogger('vibcore.io.scada')
        old_level = lg.level
        lg.setLevel(logging.WARNING)
        lg.addHandler(h)
        try:
            return parse_readings(path), h.msgs
        finally:
            lg.removeHandler(h)
            lg.setLevel(old_level)

    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, 'scada')
        os.makedirs(d)

        def write(name, rows, cols=('DATETIME', 'TAGNAME', 'VALUE'), enc='utf-8'):
            pd.DataFrame(rows, columns=list(cols)).to_csv(
                os.path.join(d, name), index=False, encoding=enc)

        # 三個檔案，各自涵蓋不同日期；07-03 整天刻意不給（漏檔）。
        # 第二個檔案用別的欄名、cp950 編碼，驗證逐檔判斷。
        write('a_0701.csv', [('2026-07-01 08:00:00', 'T.I', 10.0), ('2026-07-01 08:10:00', 'T.I', 11.0),
                             ('2026-07-02 08:00:00', 'T.I', 12.0)])
        write('b_0702.csv', [('2026-07-02 08:00:00', 'T.I', 12.0),            # 與 a 重疊、數值相同
                             ('2026-07-02 08:10:00', 'T.I', 13.0)],
              cols=('time', 'tag', 'value'), enc='cp950')
        write('c_0704.csv', [('2026-07-04 08:00:00', 'T.I', 14.0),
                             ('2026-07-02 08:10:00', 'T.I', 99.0)])            # 與 b 重疊、數值衝突
        write('tagmap.csv', [('X', 'Y')], cols=('device_id', 'tag_id'))       # 混進資料夾的非讀值檔
        pd.DataFrame({'x': [1]}).to_csv(os.path.join(d, 'notes.txt'), index=False)   # 非 csv，不該被讀

        check("資料夾 → 列出所有 .csv，依檔名排序，不含非 csv",
              [os.path.basename(x) for x in resolve_reading_paths(d)]
              == ['a_0701.csv', 'b_0702.csv', 'c_0704.csv', 'tagmap.csv'],
              str([os.path.basename(x) for x in resolve_reading_paths(d)]))
        check("萬用字元也可以",
              len(resolve_reading_paths(os.path.join(d, '[abc]_*.csv'))) == 3)
        check("不存在的路徑 → 空清單，不丟例外", resolve_reading_paths(os.path.join(tmp, 'nope')) == [])

        merged, msgs = read_quiet(d)
        check("資料夾合併：重疊的 (tag, 時間) 只留一筆",
              len(merged) == 5 and merged.duplicated(['tag_id', 'ts']).sum() == 0, str(len(merged)))
        check("輸出依 (tag, 時間) 排序", merged['ts'].is_monotonic_increasing)
        row = merged[merged['ts'] == pd.Timestamp('2026-07-02 08:10:00')].iloc[0]
        check("數值衝突時以檔名排序較後的檔案為準（c 的 99，不是 b 的 13）",
              row['value'] == 99.0, str(row['value']))
        check("數值衝突有警告（兩次匯出對同一時刻說法不同）",
              any('不一致' in m for m in msgs), str(msgs))
        check("相同數值的重疊不算衝突（只有 1 個 (tag, 時間) 衝突，不是 2 個）",
              any('1 個' in m and '不一致' in m for m in msgs), str(msgs))
        check("混進資料夾的非讀值 CSV 被略過、有說明，不讓整批失敗",
              any('tagmap.csv' in m for m in msgs) and len(merged) == 5, str(msgs))
        check("整天沒有讀值的日期有警告（07-03 漏檔）",
              any('07-03' in m for m in msgs), str(msgs))

        # 單一檔案的行為不變，也不做「漏日」檢查（單檔的空白日更可能是真的斷線）
        single, msgs1 = read_quiet(os.path.join(d, 'a_0701.csv'))
        check("單一檔案仍可直接讀", len(single) == 3)
        check("單一檔案不做漏日檢查", not any('完全沒有讀值' in m for m in msgs1), str(msgs1))

        empty_dir = os.path.join(tmp, 'empty')
        os.makedirs(empty_dir)
        e, emsgs = read_quiet(empty_dir)
        check("空資料夾 → 空結果並說明是資料夾內沒有 csv",
              e.empty and any('沒有 .csv' in m for m in emsgs), str(emsgs))

    # ── 時間對齊：同小時取中位數、過期值不硬貼 ──────────────
    # naive = 廠內當地時間，與振動端（analytic_reader.parse_datetime 不指定
    # 時區）一致。這裡刻意不用 tz-aware——用了就測不到真正的使用情境。
    base = pd.Timestamp('2026-08-01 00:00')
    readings = pd.DataFrame([
        {'tag_id': 'T.I', 'ts': base + dt.timedelta(minutes=m), 'value': v}
        # 第 0 小時內四筆；最後一筆刻意落在 00:55，讓第 1 小時距它只有
        # 5 分鐘，在容忍窗內——這樣才走得到「沿用前一筆」那條路徑。
        for m, v in ((2, 40.0), (17, 44.0), (32, 60.0), (55, 44.0))
    ])
    agg = pd.DataFrame({'ts_hour': [base,
                                    base + dt.timedelta(hours=1),
                                    base + dt.timedelta(hours=5)]})
    out = attach_to_agg(agg, readings, 'current')

    check("同一小時內取中位數（40/44/60/44 → 44）",
          float(out['scada_current'].iloc[0]) == 44.0, str(out['scada_current'].iloc[0]))
    check("該小時沒有讀值但在容忍窗內時，沿用最近一筆並標記已過期",
          pd.notna(out['scada_current'].iloc[1])
          and float(out['scada_current_stale_min'].iloc[1]) > 0,
          str(out[['scada_current', 'scada_current_stale_min']].iloc[1].to_dict()))
    # 這一項是整個模組最重要的防線：沒有它，SCADA 斷線期間的所有振動樣本
    # 都會被貼上斷線前那一筆值，整段被分到錯的工況，而且不會有錯誤訊息。
    check(f"超過 {STALENESS_TOLERANCE_MINUTES:.0f} 分鐘的過期值不硬貼，留 NaN",
          pd.isna(out['scada_current'].iloc[2]),
          f"5 小時前的值被貼上了：{out['scada_current'].iloc[2]}")

    check("沒有讀值時欄位仍建立（分得出「沒接上」與「沒資料」）",
          all(c in attach_to_agg(agg, pd.DataFrame(), 'current').columns
              for c in ('scada_current', 'scada_current_n', 'scada_current_stale_min')))

    # ── tag 寫法不同時要對得上，對不上時要講得出來 ──────────
    # 對應表的 tag 來自 Analytic CSV 的 Label 欄（`資料表名|tag名`），
    # 讀值檔的 TAGNAME 多半只有 tag 名那一半。純字串比對會一筆都對不上，
    # 而且只會表現成「沒有資料」——查不出是對不上還是真的沒有。
    from vibcore.io.scada import (TagMapping, describe_match, match_readings,
                                  normalize_tag_id)

    long_id = 'FACCIMTAB.ZONE1_K12_CHS|K12_BF_CHS_ZP350_INV_I'
    check("正規化忽略「資料表名|」前綴",
          normalize_tag_id(long_id) == normalize_tag_id('K12_BF_CHS_ZP350_INV_I'))
    check("正規化忽略大小寫與前後空白",
          normalize_tag_id('  k12_bf_chs_zp350_inv_i ') == normalize_tag_id(long_id))
    check("tag 名本身含 | 時只取最後一段", normalize_tag_id('A|B|C') == 'c')

    maps = [TagMapping(long_id, 'ZP 3-5_M1', 'current'),
            TagMapping('TBL|K12_NOT_IN_READINGS', 'ZP 3-5_M1', 'frequency')]
    short = pd.DataFrame([
        {'tag_id': 'K12_BF_CHS_ZP350_INV_I', 'ts': base, 'value': 40.0},
        {'tag_id': 'SOMETHING_ELSE', 'ts': base, 'value': 1.0},
    ])
    matched, rep = match_readings(short, maps)
    check("讀值只有短名時仍對得上對應表的長名", rep.n_readings_matched == 1)
    check("對上之後改寫成對應表的正式寫法（外鍵才對得上）",
          matched['tag_id'].tolist() == [long_id], str(matched['tag_id'].tolist()))
    check("點得出「對應表有、讀值檔沒有」的 tag",
          rep.unmatched_mappings == ('TBL|K12_NOT_IN_READINGS',),
          str(rep.unmatched_mappings))
    check("點得出「讀值檔有、對應表沒有」的 tag（這種正常，匯出涵蓋整廠）",
          rep.unmatched_readings == ('SOMETHING_ELSE',), str(rep.unmatched_readings))

    # 完全對不上時，訊息要能讓人當場看出兩邊長什麼樣
    _, rep_bad = match_readings(
        pd.DataFrame([{'tag_id': 'TOTALLY_OTHER', 'ts': base, 'value': 1.0}]), maps)
    text = '\n'.join(describe_match(rep_bad))
    check("一筆都對不上時，報告同時列出兩邊的 tag 範例",
          not rep_bad.ok and 'TOTALLY_OTHER' in text and long_id in text, text[:120])

    # ── 時區：兩邊必須是同一個時鐘 ──────────────────────────
    # 這是整個模組最容易安靜出錯的地方：振動端是 naive 當地時間，
    # SCADA 若被讀成 UTC，對齊時要嘛拋錯、要嘛整批錯開 8 小時，
    # 然後每個振動樣本都配到 8 小時前的工況，而且沒有任何訊息。
    aware = readings.assign(ts=readings['ts'].dt.tz_localize('UTC'))
    mixed = attach_to_agg(agg, aware, 'current')
    check("聚合是 naive、讀值帶時區時拒絕對齊（而不是硬轉或拋錯）",
          mixed['scada_current'].isna().all(),
          str(mixed['scada_current'].tolist()))

    # ── 讀值檔解析 ──────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        rp = os.path.join(tmp, 'r.csv')
        # 現場實際匯出的欄名是 DATETIME / TAGNAME / VALUE，不是我們原本
        # 假設的 tag_id / ts / value。與其要求現場重新匯出，不如在這裡收。
        with open(rp, 'w', encoding='utf-8') as f:
            f.write('DATETIME,TAGNAME,VALUE\n')
            f.write('2026/8/1 00:00,T.I,40\n')
            f.write('2026/8/1 00:00,T.I,41\n')            # 重複時間戳
            f.write('壞掉的時間,T.I,42\n')                  # 解析不了
            f.write('2026/8/1 00:02,T.I,沒有數字\n')
        r = parse_readings(rp)
        check("現場的 DATETIME/TAGNAME/VALUE 欄名讀得進來",
              list(r.columns) == ['tag_id', 'ts', 'value'], str(list(r.columns)))
        check("解析出來是 naive 當地時間（與振動端同一個時鐘）",
              getattr(r['ts'].dtype, 'tz', None) is None, str(r['ts'].dtype))
        check("重複的 (tag_id, ts) 去重且保留後者",
              len(r) == 1 and float(r['value'].iloc[0]) == 41.0,
              str(r.to_dict('records')))
        check("解析不了的列丟掉而不是讓整份失敗", not r.empty)


# ──────────────────────────────────────────────────────────
# 台帳補填：CSV 範本與匯入（D1）
# ──────────────────────────────────────────────────────────

def test_speed_effect() -> None:
    print("\n[11] 轉速對 velRMS 的影響（validate.speed_effect）")

    import tempfile

    from validate.speed_effect import (MAX_INHOUR_FREQ_RANGE, MIN_PAIRED_HOURS,
                                       analyze_pair, hourly_frequency, run)

    rng = np.random.default_rng(7)
    hours = pd.date_range('2026-06-01', periods=14 * 24, freq='h')
    n_h = len(hours)
    day = np.arange(n_h) // 24

    def scada_rows(tag, freq_h, skip=None):
        rows = []
        for i, (h, f) in enumerate(zip(hours, freq_h)):
            if skip is not None and skip(i):
                continue
            for m in range(0, 60, 10):
                rows.append((tag, (h + pd.Timedelta(minutes=m + 2)).strftime('%Y-%m-%d %H:%M:%S'),
                             f + rng.normal(0, 0.05)))
        return rows

    def write_vib(path, name, vel_h):
        rows = [(name, (h + pd.Timedelta(minutes=m + 3)).strftime('%Y/%m/%d %H:%M:%S'),
                 v + rng.normal(0, 0.01))
                for h, v in zip(hours, vel_h) for m in range(0, 60, 10)]
        df = pd.DataFrame(rows, columns=['Name', 'Time', 'velRMS'])
        for i in range(3):          # 真檔有數百欄；這裡補到 _sniff 要求的欄數
            df[f'pad{i}'] = 0.0
        # 現場匯出檔是 cp950 + TAB；設備名含中文，驗證編碼路徑
        df.to_csv(path, sep='\t', index=False, encoding='cp950')

    f_a = rng.uniform(42, 58, n_h).round(1)
    f_e = rng.uniform(42, 58, n_h).round(1)
    f_d = 45 + 10 * day / 13 + rng.normal(0, 0.3, n_h)       # 緩慢爬升，日內幾乎不動

    with tempfile.TemporaryDirectory() as tmp:
        vdir = os.path.join(tmp, 'vib')
        os.makedirs(vdir)
        readings = []
        # A 的 SCADA 前 7 天斷線：不得被前面的值（或之後的值）補上
        readings += scada_rows('T_A', f_a, skip=lambda i: i < 7 * 24)
        readings += scada_rows('T_B', rng.uniform(42, 58, n_h).round(1))
        readings += scada_rows('T_C', np.full(n_h, 60.0))
        readings += scada_rows('T_D', f_d)
        readings += scada_rows('T_E', f_e)
        pd.DataFrame(readings, columns=['TAGNAME', 'DATETIME', 'VALUE']).to_csv(
            os.path.join(tmp, 'scada.csv'), index=False)

        write_vib(os.path.join(vdir, 'a.csv'), 'PUMP A', 1.5 + 0.10 * (f_a - 50) + rng.normal(0, 0.15, n_h))
        write_vib(os.path.join(vdir, 'b.csv'), 'PUMP B', 2.0 + rng.normal(0, 0.15, n_h))
        write_vib(os.path.join(vdir, 'c.csv'), 'PUMP C', 1.5 + rng.normal(0, 0.15, n_h))
        write_vib(os.path.join(vdir, 'd.csv'), '冰水泵 D', 1.0 + 0.05 * day + rng.normal(0, 0.10, n_h))
        write_vib(os.path.join(vdir, 'e1.csv'), 'PUMP E_M1', 1.2 + 0.08 * (f_e - 50) + rng.normal(0, 0.12, n_h))
        write_vib(os.path.join(vdir, 'e2.csv'), 'PUMP E_M2', 0.9 + 0.06 * (f_e - 50) + rng.normal(0, 0.10, n_h))

        pd.DataFrame([
            ('PUMP A', 'FACC|T_A'), ('PUMP B', 'FACC|T_B'), ('PUMP C', 'FACC|T_C'),
            ('冰水泵 D', 'FACC|T_D'),
            ('PUMP E_M1', 'FACC|T_E'), ('PUMP E_M2', 'FACC|T_E'),    # M1/M2 共用同一個頻率 tag
            ('PUMP GHOST', 'FACC|T_A'),                              # 振動資料裡沒有這台
        ], columns=['device_id', 'tag_id']).assign(
            variable_type='frequency', unit='Hz', is_active='TRUE', note=''
        ).to_csv(os.path.join(tmp, 'tagmap.csv'), index=False, encoding='utf-8-sig')

        res, info = run(vdir, os.path.join(tmp, 'tagmap.csv'), os.path.join(tmp, 'scada.csv'))

    v = {r['device_id']: r for _, r in res.iterrows()}

    check("速度與轉速正相關（vel ≈ 0.1×Δf）→ material 且 shift_σ 為正",
          v['PUMP A']['verdict'] == 'material' and v['PUMP A']['shift_sigma'] > 2.5,
          str(v['PUMP A'].to_dict()))
    check("轉速在變、velRMS 完全無關 → minor（不能因為「有變動」就判有影響）",
          v['PUMP B']['verdict'] == 'minor' and abs(v['PUMP B']['shift_sigma']) < 1.0,
          str(v['PUMP B'].to_dict()))
    check("運轉時定頻 → no_variation，不是 minor（量不到不等於沒影響）",
          v['PUMP C']['verdict'] == 'no_variation', str(v['PUMP C'].to_dict()))
    # 這項是整支工具最重要的防線：頻率與 velRMS 同時緩慢爬升，高低頻組之間
    # 差距很大（shift_σ 輕易 > 2.5），但同一天之內毫無關係。若算成 material，
    # 「值得分層的設備數」會被趨勢灌水。
    check("兩者各自緩慢爬升（跨日趨勢）→ confounded，不計入 material",
          v['冰水泵 D']['verdict'] == 'confounded' and bool(v['冰水泵 D']['trend_suspect']),
          str({k: v['冰水泵 D'][k] for k in ('verdict', 'rho', 'rho_day', 'shift_sigma')}))
    check("該趨勢案例的跨日 rho 很高、日內 rho_day 接近 0",
          v['冰水泵 D']['rho'] > 0.8 and abs(v['冰水泵 D']['rho_day']) < 0.15,
          f"rho={v['冰水泵 D']['rho']:.2f}, rho_day={v['冰水泵 D']['rho_day']:.2f}")
    check("M1/M2 共用同一個頻率 tag，兩台各自有結果（不是只剩一台）",
          'PUMP E_M1' in v and 'PUMP E_M2' in v
          and v['PUMP E_M1']['verdict'] == v['PUMP E_M2']['verdict'] == 'material',
          f"{sorted(v)}")
    check("中文設備名＋cp950 檔案能讀、能對上對應表", '冰水泵 D' in v)
    check("對應表有、振動資料沒有的設備明確列出（不是默默少一列）",
          info['map_only'] == ['PUMP GHOST'], str(info['map_only']))

    # 前 7 天沒有 SCADA：配對小時數只剩約一半。若沿用過期值硬配，會是 336。
    check("SCADA 斷線的 7 天不被補上（嚴格同小時，不沿用前值）",
          v['PUMP A']['n_hours'] <= 7 * 24 + 4, str(v['PUMP A']['n_hours']))

    # ── 單元：小時內轉速在跳的小時整個剔除 ───────────────
    r = pd.DataFrame({
        'tag_id': ['t'] * 6,
        'ts': pd.to_datetime(['2026-06-01 00:05', '2026-06-01 00:15', '2026-06-01 00:25',
                              '2026-06-01 01:05', '2026-06-01 01:15', '2026-06-01 01:25']),
        'value': [40.0, 60.0, 50.0, 50.0, 50.1, 49.9],
    })
    hf = hourly_frequency(r).set_index('ts_hour')
    check("小時內 40→60 Hz 的跳動被標出（(max−min)/中位數 ≫ 門檻）",
          hf['inhour_range'].iloc[0] > MAX_INHOUR_FREQ_RANGE, str(hf['inhour_range'].iloc[0]))
    check("穩定的小時不被標", hf['inhour_range'].iloc[1] < MAX_INHOUR_FREQ_RANGE)

    # ── 單元：樣本不足時不下結論 ─────────────────────
    few = pd.DataFrame({'ts_hour': pd.date_range('2026-06-01', periods=MIN_PAIRED_HOURS - 1, freq='h'),
                        'vel': 1.0, 'freq': np.linspace(40, 60, MIN_PAIRED_HOURS - 1)})
    check(f"配對小時數 < {MIN_PAIRED_HOURS} → insufficient",
          analyze_pair(few)['verdict'] == 'insufficient')


def test_ledger() -> None:
    print("\n[12] 台帳補填：CSV 範本產出與匯入")

    import tempfile

    from validate.iso_readiness import (_LEDGER_FILL_COLS, _LEDGER_REFERENCE_COLS,
                                        emit_ledger_template)
    from validate.points import load_device_meta_overrides

    src = pd.DataFrame([
        {'device_id': 'ZP 3-5', 'rated_rpm': 1750.0, 'n_running': 4210,
         'vel_rms_median': 0.412, 'vel_rms_p95': 0.981},
        {'device_id': 'AHU-601', 'rated_rpm': 1710.0, 'n_running': 3012,
         'vel_rms_median': 1.204, 'vel_rms_p95': 2.310},
    ])

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'ledger.csv')
        n = emit_ledger_template(src, path)
        check("範本列數等於設備數", n == 2, str(n))

        written = pd.read_csv(path, encoding='utf-8-sig', dtype=str)
        check("範本含全部參考欄",
              all(c in written.columns for c in _LEDGER_REFERENCE_COLS),
              str(list(written.columns)))
        check("範本含全部待填欄（且為空）",
              all(c in written.columns and written[c].isna().all()
                  for c in _LEDGER_FILL_COLS),
              str(list(written.columns)))
        check("範本依 device_id 排序，方便在試算表裡整批填同型號",
              list(written['device_id']) == ['AHU-601', 'ZP 3-5'],
              str(list(written['device_id'])))

        # 模擬工程師填完：只填其中一台，另一台整列留空
        written.loc[written['device_id'] == 'ZP 3-5',
                    ['rated_power_kw', 'iso_machine_group', 'iso_foundation',
                     'is_standby', 'last_maintenance_at']] = \
            ['45', '3', 'rigid', 'TRUE', '2026-03-14']
        written.to_csv(path, index=False, encoding='utf-8-sig')

        ov = load_device_meta_overrides(path)
        check("填好的設備讀得回來", 'ZP 3-5' in ov, str(sorted(ov)))
        got = ov.get('ZP 3-5', {})
        check("群組與基礎剛性讀成字串（與 ISO_THRESHOLDS 的鍵型別一致）",
              got.get('iso_machine_group') == '3' and got.get('iso_foundation') == 'rigid',
              str(got))
        check("rated_power_kw 轉成數字", got.get('rated_power_kw') == 45.0, str(got))
        check("is_standby 的 TRUE 轉成布林", got.get('is_standby') is True, str(got))
        check("整列留空的設備不產生 override（留空 ≠ 填了空值）",
              'AHU-601' not in ov, str(sorted(ov)))
        check("參考欄不會被當成台帳欄位讀進來",
              'vel_rms_median' not in got and 'n_running' not in got, str(sorted(got)))

    # iso_readiness 要讀得回填好的台帳——台帳補完之後，「有幾台真的
    # 分類成功」是 D1 驗收的前提；若 ISO_ZONE 回傳 0 次，得先分得出是
    # 「全部健康」還是「台帳根本沒生效」。
    from validate.iso_readiness import collect as iso_collect
    if os.path.isdir('data'):
        led = {
            'AHU-601':   {'iso_machine_group': '2', 'iso_foundation': 'rigid',
                          'rated_power_kw': 22.0},
            'ZP 3-5_M1': {'iso_machine_group': '3', 'iso_foundation': 'rigid',
                          'rated_power_kw': 45.0},
            'CP 10_M1':  {'iso_machine_group': '3', 'rated_power_kw': 11.0},
        }
        got = iso_collect('data', overrides=led).set_index('device_id')
        check("台帳齊全的設備分類成功",
              got.loc['AHU-601', 'machine_class'] == '2/rigid'
              and got.loc['AHU-601', 'class_source'] == 'ledger',
              str(got.loc['AHU-601', ['machine_class', 'class_source']].to_dict()))
        check("只填一半的設備標成 ledger_partial 且算不出 Zone",
              got.loc['CP 10_M1', 'class_source'] == 'ledger_partial'
              and pd.isna(got.loc['CP 10_M1', 'machine_class']),
              str(got.loc['CP 10_M1', ['machine_class', 'class_source']].to_dict()))
        check("≤ 15 kW 標成適用範圍外（不是漏判）",
              bool(got.loc['CP 10_M1', 'out_of_scope'])
              and not bool(got.loc['AHU-601', 'out_of_scope']))
        check("沒有台帳時所有設備維持未分類（誠實的預設）",
              (iso_collect('data')['class_source'] == 'unset').all())
    else:
        skip("iso_readiness 讀回台帳", "找不到 data/ 樣本資料")

    # 布林欄的各種寫法
    from validate.points import _ledger_bool
    for raw, want in (('TRUE', True), ('true', True), ('Y', True), ('是', True), ('1', True),
                      ('FALSE', False), ('no', False), ('否', False), ('0', False),
                      ('', None), ('  ', None), ('不確定', None), (None, None)):
        check(f"is_standby「{raw}」→ {want}", _ledger_bool(raw) is want, str(_ledger_bool(raw)))


# ──────────────────────────────────────────────────────────
# 六、資料庫層（需要 PostgreSQL）
# ──────────────────────────────────────────────────────────

def ensure_disposable_dbname(dbname: str) -> str | None:
    """
    擋掉「把測試指向正式資料庫」這個會毀資料的操作。

    這兩支驗收腳本的第一個動作是 `dropdb --if-exists <dbname>` 再
    `createdb`。把 `--dbname` 填成正式庫的名字，正式資料會**當場被刪掉且
    無法復原**——而這個誤用很自然：使用者想「用實際的資料庫測試」，
    指的其實是「連到實際那台伺服器」，不是「拿正式庫當測試對象」。
    連伺服器是安全的（腳本會在該台上另建一個獨立的測試庫再刪掉），
    拿正式庫當對象則不是。

    規則：名稱必須以 `_test` 結尾。這不留例外開關——沒有任何正當理由要
    在非 `_test` 的資料庫上跑這些腳本，真的想換名字，取成 `xxx_test` 即可。

    Returns:
        None 表示通過；否則回傳要印給使用者的錯誤訊息。
    """
    if dbname.endswith("_test"):
        return None
    return (f"拒絕在資料庫「{dbname}」上執行：名稱必須以 _test 結尾。\n"
            f"\n"
            f"  這支腳本開頭會 DROP 掉指定的資料庫再重建。若這是正式庫，\n"
            f"  資料會當場消失且無法復原，所以這裡直接擋下來。\n"
            f"\n"
            f"  要連到正式那台伺服器測試是安全的做法——設環境變數指向它，\n"
            f"  不要動 --dbname：\n"
            f"    VIB_DB_HOST / VIB_DB_PORT / VIB_DB_USER / VIB_DB_PASSWORD\n"
            f"  腳本會在該台伺服器上另建一個獨立的測試資料庫，跑完自動刪除，\n"
            f"  完全不碰既有資料（需要該帳號有 CREATE DATABASE 權限）。")


def _db_hint(e: Exception) -> str:
    """把「跑不起來」翻成「該怎麼辦」。

    這一節需要 PostgreSQL 用戶端（psql / createdb / dropdb）。找不到執行檔
    與連不上伺服器是兩種完全不同的處置，訊息要分得開——否則使用者會以為
    自己的資料庫壞了，而其實只是沒裝 client。
    """
    if isinstance(e, FileNotFoundError):
        return ("找不到 psql / createdb。安裝用戶端即可，不需要在本機跑資料庫："
                "Ubuntu/Debian `sudo apt install postgresql-client`、"
                "macOS `brew install libpq && brew link --force libpq`；"
                "連遠端資料庫請設 VIB_DB_HOST / VIB_DB_PORT / VIB_DB_USER / VIB_DB_PASSWORD")
    return f"無法建立測試資料庫（{type(e).__name__}：{str(e)[:120]}）"


def _psql_env() -> dict:
    env = dict(os.environ)
    env.setdefault("PGHOST", os.environ.get("VIB_DB_HOST", "localhost"))
    env.setdefault("PGPORT", os.environ.get("VIB_DB_PORT", "5432"))
    env.setdefault("PGUSER", os.environ.get("VIB_DB_USER", "postgres"))
    if os.environ.get("VIB_DB_PASSWORD"):
        env.setdefault("PGPASSWORD", os.environ["VIB_DB_PASSWORD"])
    return env


def test_db(dbname: str) -> None:
    print("\n[13] 資料庫：台帳欄位保留與 migration 冪等性")
    env = _psql_env()
    try:
        subprocess.run(["dropdb", "--if-exists", dbname], env=env, check=True, capture_output=True)
        subprocess.run(["createdb", dbname], env=env, check=True, capture_output=True)
        r = subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname,
                            "-f", "db/schema.sql"], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[:500])
    except (FileNotFoundError, subprocess.CalledProcessError, RuntimeError) as e:
        why = _db_hint(e)
        # 逐項列出「這次沒驗到什麼」。只印一句「略過」會讓人以為無關緊要，
        # 但這一節驗的是台帳欄位保留與備機旗標——那正是每日排程最容易安靜
        # 弄壞的東西，沒驗到就該講清楚是哪幾項沒驗到。
        for label in ("台帳欄位保留（含備機旗標不被覆寫）",
                      "migration_004 冪等",
                      "migration_005 冪等",
                      "migration_005 後 IMPACT_RISE 參數不含 kurt"):
            skip(label, why)
            why = "同上"
        return

    import psycopg2
    from psycopg2.extras import RealDictCursor
    from vibcore.db import repository as repo

    conn = psycopg2.connect(
        host=env["PGHOST"], port=int(env["PGPORT"]), user=env["PGUSER"],
        password=os.environ.get("VIB_DB_PASSWORD") or None, dbname=dbname,
        options="-c search_path=vib,public", cursor_factory=RealDictCursor)

    admin = DeviceContext(
        device_id='P1', device_name='泵1', building='A棟', floor='1F', system_name='冰水',
        iso_machine_group='3', iso_foundation='rigid', iso_driver_type='external',
        iso_class_source='manual_override', rated_power_kw=75.0, rated_rpm=1750.0,
        is_standby=True,
        last_maintenance_at=dt.datetime(2026, 4, 2, tzinfo=dt.timezone.utc))
    repo.upsert_device(conn, admin)
    # 模擬每日排程：用 CSV 組出的 context 再 upsert 一次（台帳欄位皆 None）
    repo.upsert_device(conn, DeviceContext(
        device_id='P1', device_name='泵1', building='A棟', floor='1F',
        system_name='冰水', rated_rpm=1750.0, fmf_hz=29.2))
    conn.commit()

    got = repo.get_device(conn, 'P1')
    for field in ('iso_machine_group', 'iso_foundation', 'iso_driver_type',
                  'rated_power_kw', 'last_maintenance_at'):
        check(f"每日排程 upsert 不會清掉台帳欄位 {field}",
              getattr(got, field) is not None, f"{field} 被清成 NULL")
    check("iso_class_source 不被 'unset' 覆蓋",
          got.iso_class_source == 'manual_override', got.iso_class_source)
    # is_standby 是 NOT NULL DEFAULT false，2026-09 前每日排程會安靜地
    # 把管理員設的 true 重設成 false（DeviceContext 側預設就是 False）
    check("每日排程 upsert 不會把備機旗標重設為 false",
          got.is_standby is True, f"is_standby = {got.is_standby}")
    # 明確填 false 仍要能改掉——COALESCE 只擋 None，不該連「確認不是備機」
    # 也一起擋掉，否則備機一旦設成 true 就永遠改不回來
    repo.upsert_device(conn, dataclasses.replace(admin, is_standby=False))
    conn.commit()
    check("台帳明確填 false 時仍可把備機旗標改回來",
          repo.get_device(conn, 'P1').is_standby is False)
    # ── SCADA：tag 對應與讀值的落庫（Q3 第一階段）──────────
    from vibcore.io.scada import TagMapping, attach_to_agg

    repo.upsert_tag_mappings(conn, [
        TagMapping(tag_id='T.I', device_id='P1', variable_type='current', unit='A'),
        TagMapping(tag_id='T.F', device_id='P1', variable_type='frequency', unit='Hz'),
    ])
    conn.commit()
    got_tags = repo.get_tag_mappings(conn, device_id='P1')
    check("tag 對應寫得進去、讀得回來", len(got_tags) == 2, str(got_tags))
    check("可依變數型別過濾",
          len(repo.get_tag_mappings(conn, device_id='P1', variable_type='current')) == 1)

    base_ts = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    readings = pd.DataFrame([
        {'tag_id': 'T.I', 'ts': base_ts + dt.timedelta(minutes=2 * i),
         'value': 40.0 + (i % 5)} for i in range(30)
    ])
    n_ins = repo.bulk_insert_scada(conn, readings)
    conn.commit()
    check("讀值寫得進去", n_ins == 30, str(n_ins))

    # 重覆匯入同一段期間要是安全的（同 bulk_insert_agg 的理由）
    repo.bulk_insert_scada(conn, readings.assign(value=readings['value'] + 1))
    conn.commit()
    back = repo.get_scada(conn, 'P1', 'current',
                          base_ts, base_ts + dt.timedelta(hours=2))
    check("重覆匯入是 upsert 而非重複插入（30 筆仍是 30 筆）",
          len(back) == 30, str(len(back)))
    check("重覆匯入會更新數值", float(back['value'].iloc[0]) == 41.0,
          str(back['value'].iloc[0]))

    # 對應表沒有的 tag 應被外鍵擋下，而不是安靜丟掉
    try:
        repo.bulk_insert_scada(conn, pd.DataFrame([
            {'tag_id': 'T.UNKNOWN', 'ts': base_ts, 'value': 1.0}]))
        conn.commit()
        fk_blocked = False
    except Exception:                       # noqa: BLE001 - 就是要確認會擋
        conn.rollback()
        fk_blocked = True
    check("對應表沒有的 tag_id 被擋下（而非安靜丟掉，那樣會讓漏填查不出來）",
          fk_blocked)

    # ── 多對多：M1 與 M2 共用同一組 tag ─────────────────────
    # 舊結構以 tag_id 為 tag_mapping 的主鍵：一次寫兩列報 CardinalityViolation，
    # 分兩次寫則後寫的覆蓋先寫的——先寫那台的 tag 查不到，而且沒有任何錯誤。
    for d in ('M1', 'M2'):
        repo.upsert_device(conn, DeviceContext(device_id=f'CWP 01_{d}',
                                               device_name=f'CWP 01_{d}'))
    conn.commit()
    shared_tag = 'FACC|CWP01_INV_I'
    n_links = repo.upsert_tag_mappings(conn, [
        TagMapping(shared_tag, 'CWP 01_M1', 'current', 'A'),
        TagMapping(shared_tag, 'CWP 01_M2', 'current', 'A')])
    conn.commit()
    check("同一次寫入兩台共用同一個 tag 不報錯", n_links == 2, str(n_links))
    for d in ('CWP 01_M1', 'CWP 01_M2'):
        check(f"共用 tag 的 {d} 查得到",
              [r['tag_id'] for r in repo.get_tag_mappings(conn, device_id=d)] == [shared_tag])

    # 分兩次寫（先 M1、後 M2）不能讓先寫的那台消失
    repo.upsert_device(conn, DeviceContext(device_id='CWP 02_M1', device_name='CWP 02_M1'))
    repo.upsert_device(conn, DeviceContext(device_id='CWP 02_M2', device_name='CWP 02_M2'))
    conn.commit()
    repo.upsert_tag_mappings(conn, [TagMapping('FACC|CWP02_I', 'CWP 02_M1', 'current')])
    conn.commit()
    repo.upsert_tag_mappings(conn, [TagMapping('FACC|CWP02_I', 'CWP 02_M2', 'current')])
    conn.commit()
    check("分兩次寫入時，先寫的設備不會被後寫的覆蓋",
          len(repo.get_tag_mappings(conn, device_id='CWP 02_M1')) == 1
          and len(repo.get_tag_mappings(conn, device_id='CWP 02_M2')) == 1)

    # 同一個訊號被填成兩種型別 = 填錯，不該安靜擇一
    try:
        repo.upsert_tag_mappings(conn, [
            TagMapping('FACC|CONFLICT', 'CWP 01_M1', 'current'),
            TagMapping('FACC|CONFLICT', 'CWP 01_M2', 'frequency')])
        conflict_raised = False
    except ValueError:
        conn.rollback()
        conflict_raised = True
    check("同一個 tag 在同批資料被填成不同型別時拒絕寫入", conflict_raised)

    # is_active 在連結上：某台設備填錯而停用，不影響共用同一個 tag 的另一台
    repo.upsert_tag_mappings(conn, [TagMapping(shared_tag, 'CWP 01_M1', 'current',
                                               is_active=False)])
    conn.commit()
    check("停用其中一台的連結不影響共用同一個 tag 的另一台",
          len(repo.get_tag_mappings(conn, device_id='CWP 01_M1')) == 0
          and len(repo.get_tag_mappings(conn, device_id='CWP 01_M2')) == 1)

    # 從資料庫讀回來的格式要能直接餵進對齊函式
    agg_db = pd.DataFrame({'ts_hour': [base_ts]})
    joined = attach_to_agg(agg_db, back, 'current')
    check("get_scada 的輸出可直接餵給 attach_to_agg",
          pd.notna(joined['scada_current'].iloc[0]),
          str(joined.iloc[0].to_dict()))

    conn.close()

    # ── migration 006：從舊結構（tag_id 為主鍵）搬到多對多 ──────
    mig6 = "db/migration_006_tag_many_to_many.sql"
    old_db = dbname.replace('_test', '') + "_mig6_test"
    if not os.path.exists(mig6):
        skip("migration_006 由舊結構搬資料", f"找不到 {mig6}")
    else:
        def _psql(*a):
            return subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", old_db, *a],
                                  env=env, capture_output=True, text=True)
        subprocess.run(["dropdb", "--if-exists", old_db], env=env, capture_output=True)
        subprocess.run(["createdb", old_db], env=env, capture_output=True)
        _psql("-f", "db/schema.sql")
        # 把 SCADA 三張表還原成舊結構，並塞一筆舊資料
        r0 = _psql("-c", """
            SET search_path TO vib, public;
            DROP TABLE scada_reading; DROP TABLE tag_mapping; DROP TABLE scada_tag;
            CREATE TABLE tag_mapping (
                tag_id TEXT PRIMARY KEY,
                device_id TEXT REFERENCES device(device_id) ON DELETE CASCADE,
                variable_type TEXT NOT NULL, unit TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE);
            CREATE TABLE scada_reading (
                tag_id TEXT NOT NULL REFERENCES tag_mapping(tag_id) ON DELETE CASCADE,
                ts TIMESTAMPTZ NOT NULL, value NUMERIC(16,6), PRIMARY KEY (tag_id, ts));
            INSERT INTO device (device_id, device_name) VALUES ('OLD_DEV', 'OLD_DEV');
            INSERT INTO tag_mapping VALUES ('OLD|TAG_I', 'OLD_DEV', 'current', 'A', TRUE);
            INSERT INTO scada_reading VALUES ('OLD|TAG_I', '2026-08-01 00:00+08', 41.5);
        """)
        check("（準備）舊結構的測試資料建得起來", r0.returncode == 0, r0.stderr[:200])
        runs = [_psql("-f", mig6) for _ in range(2)]
        check("migration_006 由舊結構套用成功，且可重複套用（冪等）",
              all(r.returncode == 0 for r in runs),
              '；'.join(r.stderr[:200] for r in runs if r.returncode != 0))
        q = subprocess.run(["psql", "-tAq", "-d", old_db, "-c", """
            SET search_path TO vib, public;
            SELECT (SELECT count(*) FROM scada_tag WHERE tag_id='OLD|TAG_I'),
                   (SELECT count(*) FROM tag_mapping WHERE device_id='OLD_DEV'),
                   (SELECT count(*) FROM scada_reading WHERE tag_id='OLD|TAG_I');"""],
                           env=env, capture_output=True, text=True)
        check("舊的對應與讀值原樣搬到新結構（訊號 1、連結 1、讀值 1）",
              q.stdout.strip().splitlines()[-1] == '1|1|1', q.stdout.strip() or q.stderr[:200])
        two = _psql("-c", """SET search_path TO vib, public;
            INSERT INTO device (device_id, device_name) VALUES ('OLD_DEV2', 'OLD_DEV2');
            INSERT INTO tag_mapping (device_id, tag_id) VALUES ('OLD_DEV2', 'OLD|TAG_I');""")
        check("搬完之後同一個 tag 可以再對應第二台設備", two.returncode == 0, two.stderr[:200])
        subprocess.run(["dropdb", "--if-exists", old_db], env=env, capture_output=True)

    for mig, label in (("db/migration_004_iso10816_3_and_median.sql", "migration_004"),
                       ("db/migration_005_drop_kurtosis_channel.sql", "migration_005")):
        if not os.path.exists(mig):
            skip(f"{label} 冪等", f"找不到 {mig}")
            continue
        outs = [subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname, "-f", mig],
                               env=env, capture_output=True, text=True) for _ in range(2)]
        check(f"{label} 可重複套用於新建庫（冪等）",
              all(o.returncode == 0 for o in outs),
              '；'.join(o.stderr[:200] for o in outs if o.returncode != 0))

    # migration_005 之後，IMPACT_RISE 不該再帶任何 kurt 參數——留著會讓
    # 現場以為門檻還在生效（規則層只是安靜地忽略它們）
    r = subprocess.run(["psql", "-tAq", "-d", dbname, "-c",
                        "set search_path to vib,public; "
                        "select params::text from rule_config where rule_code='IMPACT_RISE';"],
                       env=env, capture_output=True, text=True)
    check("migration_005 後 IMPACT_RISE 的參數不含 kurt / require_both",
          r.returncode == 0 and 'kurt' not in r.stdout and 'require_both' not in r.stdout,
          r.stdout.strip() or r.stderr[:200])

    subprocess.run(["dropdb", "--if-exists", dbname], env=env, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbname", default=DEFAULT_TEST_DB,
                    help=f"測試資料庫名稱（預設 {DEFAULT_TEST_DB}）；執行時會先 DROP 再重建")
    args = ap.parse_args()

    refusal = ensure_disposable_dbname(args.dbname)
    if refusal:
        print(refusal)
        return 2

    try:
        test_impact()
        test_iso()
        test_persistence()
        test_baseline_maintenance()
        test_axis_direction()
        test_backtest_instrumentation()
        test_guardrail()
        test_systemic_gaps()
        test_step_change_features()
        test_scada()
        test_speed_effect()
        test_ledger()
        test_db(args.dbname)
    except AssertionError as e:
        print(f"\n❌ 驗收失敗：{e}")
        return 1

    tail = f"，略過 {len(_SKIPPED)} 項" if _SKIPPED else ""
    print(f"\n✅ 全部通過（{len(_PASSED)} 項{tail}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
