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
import json
import logging
import os
import re
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

    # 台帳有填分類、但額定功率 ≤ 15 kW：不在 ISO 20816-3 適用範圍，同樣退回 sigma，
    # 但原因跟「未分級」不同。實測（2026-06～08）台帳完整時 VEL_HIGH 剩下的 41 件
    # 全是這一類；若仍說成「設備未分級」，會讓人去補一份其實早就填好的台帳。
    small = vel_high(ctx(rows, stats, dev=device(iso_machine_group='4', iso_foundation='rigid',
                                                 rated_power_kw=11.0)))
    ev_small = _episode_evidence(small)
    check("額定 11 kW、台帳已有分類 → 仍是 sigma_fallback（標準不適用）",
          ev_small['threshold_mode'] == 'sigma_fallback' and ev_small['machine_class'] is None,
          str((ev_small['threshold_mode'], ev_small['machine_class'])))
    check("退回原因如實記錄為 15 kW 適用範圍，而不是「未分級」",
          '15 kW' in (ev_small['iso_fallback_reason'] or '')
          and '未分級' not in (ev_small['iso_fallback_reason'] or ''),
          str(ev_small['iso_fallback_reason']))
    check("事件說明文字也不說成「設備未分級」",
          '未分級' not in small.detail and '15 kW' in small.detail, small.detail)
    check("真正未分類的設備，退回原因才是「未分級」",
          '未分級' in (_episode_evidence(out2)['iso_fallback_reason'] or ''),
          str(_episode_evidence(out2)['iso_fallback_reason']))
    check("ISO 模式的事件沒有退回原因", ev['iso_fallback_reason'] is None)
    check("退回原因攤平成事件列的獨立欄位（不必解析 evidence_json）",
          'iso_fallback_reason' in _EVIDENCE_FLAT_KEYS)

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


def test_vel_high_policy() -> None:
    """VEL_HIGH 相對基準路徑的兩道把關：實用顯著性下限，與「退回 sigma 只列觀察名單」。"""
    print("\n[6b] VEL_HIGH：實用顯著性下限與退回 sigma 的嚴重度")

    from vibcore.types import SEVERITY_OBSERVE, SEVERITY_WARN

    def vh(rows_v, med, std, dev=None, params=None):
        stats = {'vel_rms': (med, med, std, 300), 'vel_oa': (med, med, std, 300)}
        rows = [{'vel_rms': v, 'vel_oa': v} for v in rows_v]
        return vel_high(ctx(rows, stats, params=params, dev=dev))

    small = device(iso_machine_group='4', iso_foundation='rigid', rated_power_kw=11.0)   # ≤ 15 kW
    unclassified = device(iso_machine_group=None, iso_foundation=None, iso_class_source='unset')
    big = device(iso_machine_group='2', iso_foundation='rigid', rated_power_kw=45.0)

    # ── 實用顯著性下限 ──
    # 實測 CP 07_M1：基準 0.257、標準差 0.0117，當前 0.293 → 3.04σ，但只多了 0.036 mm/s（+14%）。
    tiny = vh([0.293, 0.301, 0.293], 0.257, 0.0117, dev=small)
    check("3σ 以上、但增幅只有 +14%／0.04 mm/s → 不算偏高（實用顯著性下限）", not tiny.triggered)
    old_style = vh([0.293, 0.301, 0.293], 0.257, 0.0117, dev=small,
                   params={'min_rel_change': 0, 'min_abs_change': 0})
    check("下限設為 0 就回到原本純 σ 的行為（可調、可回復）", old_style.triggered)
    only_rel = vh([0.34, 0.34, 0.34], 0.257, 0.0117, dev=small)     # +32%、+0.083 mm/s
    check("相對夠大但絕對不到 0.10 mm/s → 不算（兩個下限要同時達到）", not only_rel.triggered)
    only_abs = vh([1.55, 1.56, 1.55], 1.425, 0.040, dev=small)      # +9%、+0.13 mm/s
    check("絕對夠大但相對不到 25% → 不算（CP 08_M2 的 +10% 小幅超標）", not only_abs.triggered)
    real = vh([1.33, 1.38, 1.48], 0.664, 0.0116, dev=small)         # +123%
    check("增幅同時達標（+123%）→ 觸發", real.triggered, str(real.evidence))
    just_over = vh([0.359, 0.359, 0.359], 0.257, 0.0117, dev=small)     # +0.102、+40%
    just_under = vh([0.355, 0.355, 0.355], 0.257, 0.0117, dev=small)    # +0.098、+38%
    check("增幅剛好跨過 0.10 mm/s 下限 → 算；差一點點 → 不算（浮點邊界不拿來比等號）",
          just_over.triggered and not just_under.triggered)
    dip = vh([1.33, 0.70, 1.48], 0.664, 0.0116, dev=small)
    check("持續性緩衝：三筆中有一筆沒達下限 → 不觸發", not dip.triggered)
    zero_med = vh([0.4, 0.4, 0.4], 0.0, 0.02, dev=small)
    check("基準中位數為 0（無法算相對增幅）→ 只看絕對下限", zero_med.triggered)
    check("門檻參數記進 evidence，事後可知當時用的下限",
          real.evidence['min_rel_change'] == 0.25 and real.evidence['min_abs_change'] == 0.10)

    # ── 嚴重度 ──
    check("額定 ≤ 15 kW（ISO 不適用）退回 sigma 的結果 → observe（不派工）",
          real.severity == SEVERITY_OBSERVE and real.evidence['threshold_mode'] == 'sigma_fallback',
          str((real.severity, real.evidence['threshold_mode'])))
    unc = vh([1.33, 1.38, 1.48], 0.664, 0.0116, dev=unclassified)
    check("未分級退回 sigma 的結果同樣是 observe", unc.triggered and unc.severity == SEVERITY_OBSERVE)
    iso_hit = vh([2.0, 2.1, 2.0], 0.45, 0.1, dev=big)
    check("ISO 錨定的觸發維持 warn（有外部標準可引用）",
          iso_hit.triggered and iso_hit.severity == SEVERITY_WARN
          and iso_hit.evidence['threshold_mode'] == 'iso', str(iso_hit.severity))
    check("ISO 路徑不套用相對基準下限（evidence 為 None）",
          iso_hit.evidence['min_rel_change'] is None and iso_hit.evidence['min_abs_change'] is None)
    explicit = vh([1.33, 1.38, 1.48], 0.664, 0.0116, dev=small, params={'threshold_mode': 'sigma'})
    check("使用者明確指定 threshold_mode='sigma' → 維持 warn（那是他自己選的對照組）",
          explicit.triggered and explicit.severity == SEVERITY_WARN
          and explicit.evidence['threshold_mode'] == 'sigma')
    back = vh([1.33, 1.38, 1.48], 0.664, 0.0116, dev=small, params={'fallback_severity': 'warn'})
    check("fallback_severity='warn' 可改回派工", back.severity == SEVERITY_WARN)
    bad = vh([1.33, 1.38, 1.48], 0.664, 0.0116, dev=small, params={'fallback_severity': 'err'})
    check("fallback_severity 填了不合法的值 → 退回預設 observe，不因此升到 err",
          bad.severity == SEVERITY_OBSERVE)

    # ── 說明文字 ──
    check("說明文字帶出增幅（絕對值與百分比），工程師看得出「多了多少」",
          '+123%' in real.detail and '0.8' in real.detail, real.detail)
    check("observe 的解讀邊界明講沒有外部標準、不建立待辦、沒排除工況與鄰機",
          '觀察名單' in real.interpretation_limit and '工況' in real.interpretation_limit
          and '鄰近機組' in real.interpretation_limit, real.interpretation_limit)
    check("warn 的 ISO 觸發沒有「觀察名單」字樣", '觀察名單' not in iso_hit.interpretation_limit)
    for o in (real, unc, iso_hit):
        problems = check_outcome(o)
        check(f"VEL_HIGH（{o.severity}／{o.evidence['threshold_mode']}）的輸出文字無診斷性斷言",
              not problems, str(problems))

    from validate.rule_defaults import DEFAULT_RULE_CONFIGS
    p = DEFAULT_RULE_CONFIGS['VEL_HIGH'].params
    check("回測用的預設設定與規則層預設一致（避免回測與上線的門檻不同）",
          p.get('min_rel_change') == 0.25 and p.get('min_abs_change') == 0.10
          and p.get('fallback_severity') == 'observe', str(p))


def test_rule_config_plumbing() -> None:
    """正式排程傳進引擎的是資料庫的字典：params／is_active／severity 都必須真的生效。"""
    print("\n[6c] 規則設定的傳遞：資料庫字典要能改變規則的參數、停用與嚴重度")

    import datetime as _dt
    from types import SimpleNamespace

    from vibcore.pipeline.daily import rule_now
    from vibcore.rules import engine
    from vibcore.rules.engine import evaluate_all, outcome_to_finding
    from vibcore.types import RuleOutcome, effective_severity

    # ── 嚴重度上限 ──
    table = [(('warn', 'observe'), 'observe'), (('observe', 'warn'), 'observe'),
             (('err', 'warn'), 'warn'), (('warn', 'err'), 'warn'), (('err', 'err'), 'err'),
             (('warn', 'warn'), 'warn'), ((None, 'observe'), 'observe'), (('warn', None), 'warn'),
             (('bogus', 'observe'), 'observe'), ((None, None), 'warn')]
    bad = [(k, effective_severity(*k), want) for k, want in table if effective_severity(*k) != want]
    check("effective_severity：設定是上限，規則可再往下調；缺值用另一個", not bad, str(bad))

    # ── 用兩條假規則檢查引擎本身 ──
    seen = {}

    def make(code, sev='warn'):
        def fn(c):
            seen[code] = dict(c.params)
            return RuleOutcome(triggered=True, rule_code=code, issue_type=code.lower(),
                               family='event', severity=sev, title='t', detail='d',
                               interpretation_limit='x')
        return fn

    saved = dict(engine.REGISTRY)
    try:
        engine.REGISTRY.clear()
        engine.REGISTRY['AAA'] = make('AAA')
        engine.REGISTRY['BBB'] = make('BBB')
        c0 = ctx([{'vel_rms': 1.0, 'vel_oa': 1.0}], {'vel_rms': (1, 1, .1, 100)})
        db_rows = {                                    # 與 repository.get_rule_configs 同形：字典
            'AAA': {'rule_code': 'AAA', 'is_active': True, 'severity': 'observe',
                    'params': {'sigma': 7.5}},
            'BBB': {'rule_code': 'BBB', 'is_active': True, 'severity': 'warn',
                    'params': {'days': 14}},
        }
        outs = evaluate_all(c0, db_rows)
        by = {o.rule_code: o for o in outs}
        check("資料庫字典裡的 params 真的傳進規則（原本永遠是空的）",
              seen['AAA'] == {'sigma': 7.5} and seen['BBB'] == {'days': 14}, str(seen))
        check("設定的嚴重度當上限：規則回傳 warn、設定為 observe → observe；設定為 warn → warn（可升回）",
              by['AAA'].severity == 'observe' and by['BBB'].severity == 'warn',
              str({k: v.severity for k, v in by.items()}))
        check("每個結果記著判定當下自己的參數（不是最後一條規則的）",
              by['AAA'].params == {'sigma': 7.5} and by['BBB'].params == {'days': 14}
              and c0.params == {'days': 14})
        f = outcome_to_finding(by['AAA'], c0)
        check("Finding 的 trigger_params 是觸發它的那條規則的參數",
              f.trigger_params == {'sigma': 7.5}, str(f.trigger_params))

        seen.clear()
        off = dict(db_rows)
        off['AAA'] = {**db_rows['AAA'], 'is_active': False}
        evaluate_all(c0, off)
        check("is_active=False 的規則不執行", 'AAA' not in seen and 'BBB' in seen, str(sorted(seen)))
        seen.clear()
        evaluate_all(c0, {'BBB': db_rows['BBB']})
        check("設定裡沒有的規則照常以預設值執行（維持原行為，單元測試與新規則上線前都靠它）",
              seen.get('AAA') == {} and 'BBB' in seen)
        seen.clear()
        obj_cfg = {'AAA': SimpleNamespace(is_active=True, params={'sigma': 2.0}, severity='observe')}
        o2 = evaluate_all(c0, obj_cfg, rule_codes=['AAA'])
        check("物件形式的設定（回測用的 RuleConfigRow）同樣有效",
              seen['AAA'] == {'sigma': 2.0} and o2[0].severity == 'observe')
    finally:
        engine.REGISTRY.clear()
        engine.REGISTRY.update(saved)

    # ── 真實規則：資料庫字典改門檻要有效；observe 規則不派工 ──
    stats = {'vel_rms': (0.664, 0.664, 0.0116, 300), 'vel_oa': (0.664, 0.664, 0.0116, 300)}
    rows = [{'vel_rms': v, 'vel_oa': v} for v in (1.33, 1.38, 1.48)]
    small = device(iso_machine_group='4', iso_foundation='rigid', rated_power_kw=11.0)
    c1 = ctx(rows, stats, dev=small)
    cfg_default = {'VEL_HIGH': {'is_active': True, 'severity': 'warn', 'params': {}}}
    cfg_strict = {'VEL_HIGH': {'is_active': True, 'severity': 'warn', 'params': {'min_rel_change': 5.0}}}
    hit = evaluate_all(c1, cfg_default, rule_codes=['VEL_HIGH'])
    miss = evaluate_all(c1, cfg_strict, rule_codes=['VEL_HIGH'])
    check("改 rule_config.params（min_rel_change 5.0）後 VEL_HIGH 不再觸發——改設定真的有效",
          len(hit) == 1 and len(miss) == 0, f"{len(hit)}/{len(miss)}")
    check("VEL_HIGH 退回 sigma 的結果仍是 observe（規則自己往下調，設定的 warn 不會把它拉回去）",
          hit[0].severity == 'observe')
    imp_stats = {'acc_crest_median': (2.6, 2.6, 0.3, 300)}
    c2 = ctx([{'acc_crest_median': 4.0}], imp_stats)
    as_observe = evaluate_all(c2, {'IMPACT_RISE': {'is_active': True, 'severity': 'observe', 'params': {}}},
                              rule_codes=['IMPACT_RISE'])
    as_warn = evaluate_all(c2, {'IMPACT_RISE': {'is_active': True, 'severity': 'warn', 'params': {}}},
                           rule_codes=['IMPACT_RISE'])
    check("IMPACT_RISE 設為 observe → 不派工；改設定為 warn 即升級（不必改程式）",
          as_observe and as_observe[0].severity == 'observe'
          and as_warn and as_warn[0].severity == 'warn',
          str((as_observe and as_observe[0].severity, as_warn and as_warn[0].severity)))

    # ── 回測與正式排程用同一個嚴重度算法 ──
    from validate.backtest import _make_episode_row
    from validate.rule_defaults import RuleConfigRow

    class _P:
        class point:
            class device:
                device_id, device_name = 'T', 'T'
            point_id, position = 1, 'M1'

    def ep(rule_sev, out_sev):
        row = RuleConfigRow('VEL_HIGH', 'x', 'oscillating', 'vel_high', rule_sev)
        o = None if out_sev is None else RuleOutcome(
            triggered=True, rule_code='VEL_HIGH', issue_type='vel_high', family='oscillating',
            severity=out_sev)
        return _make_episode_row(_P, row, pd.Timestamp('2026-08-01'), pd.Timestamp('2026-08-02'), o)
    e1, e2, e3 = ep('warn', 'observe'), ep('observe', 'warn'), ep('observe', None)
    check("回測事件的嚴重度＝規則回傳與設定取較低者：warn／observe → observe、不算可行動",
          e1['severity'] == 'observe' and e1['is_actionable'] is False)
    check("設定 observe、規則回傳 warn → observe；沒有結果（理論上不會）退回設定值",
          e2['severity'] == 'observe' and e3['severity'] == 'observe')
    e4 = ep('warn', 'warn')
    check("兩者都是 warn → warn、可行動", e4['severity'] == 'warn' and e4['is_actionable'] is True)

    # ── 規則判定用的「現在」 ──
    utc = _dt.timezone.utc
    past = _dt.datetime(2026, 6, 2, tzinfo=utc)
    future = _dt.datetime.now(utc) + _dt.timedelta(days=1)
    check("補跑過去的日期：now 是那一天的最後一秒，不是真實的現在（否則整段歷史都算離線）",
          rule_now(past) == past - _dt.timedelta(seconds=1))
    check("且仍落在該日之內（週報期間是半開區間，落在隔天零時會被算進隔天）",
          rule_now(past).date() == (past - _dt.timedelta(days=1)).date())
    check("跑今天的資料：窗口結束在明天，now 是真實的現在",
          abs((rule_now(future) - _dt.datetime.now(utc)).total_seconds()) < 5)
    from vibcore.pipeline.daily import run_window_end
    check("資料窗口結束 ＝ 該日隔日零時（UTC）",
          run_window_end(_dt.date(2026, 6, 2)) == _dt.datetime(2026, 6, 3, tzinfo=utc))


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
        old_level, old_prop = lg.level, lg.propagate
        lg.setLevel(logging.WARNING)
        lg.propagate = False      # 預期中的警告只給測試看，不洗版
        lg.addHandler(h)
        try:
            return parse_readings(path), h.msgs
        finally:
            lg.removeHandler(h)
            lg.setLevel(old_level)
            lg.propagate = old_prop

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
                                       analyze_pair, daily, floor_to_hour, hourly_frequency, run)
    from vibcore.io.analytic_reader import parse_datetime

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
        write_vib(os.path.join(vdir, 'idle.csv'), 'PUMP IDLE', np.full(n_h, 0.02))

        pd.DataFrame([
            ('PUMP A', 'FACC|T_A'), ('PUMP B', 'FACC|T_B'), ('PUMP C', 'FACC|T_C'),
            ('冰水泵 D', 'FACC|T_D'),
            ('PUMP E_M1', 'FACC|T_E'), ('PUMP E_M2', 'FACC|T_E'),    # M1/M2 共用同一個頻率 tag
            ('PUMP GHOST', 'FACC|T_A'),                              # 振動資料裡沒有這台
            ('PUMP IDLE', 'FACC|T_C'),                               # 有這台，但整段沒運轉
        ], columns=['device_id', 'tag_id']).assign(
            variable_type='frequency', unit='Hz', is_active='TRUE', note=''
        ).to_csv(os.path.join(tmp, 'tagmap.csv'), index=False, encoding='utf-8-sig')

        res, info = run(vdir, os.path.join(tmp, 'tagmap.csv'), os.path.join(tmp, 'scada.csv'))
        d_tbl = daily(vdir, os.path.join(tmp, 'tagmap.csv'), os.path.join(tmp, 'scada.csv'), 'PUMP A')
        d_none = daily(vdir, os.path.join(tmp, 'tagmap.csv'), os.path.join(tmp, 'scada.csv'), '不存在的設備')

    v = {r['device_id']: r for _, r in res.iterrows()}

    # ── 逐日對照：回答「那天轉速到底有沒有變」，也要講得出「那天根本沒有 SCADA」──
    dd = d_tbl.set_index('day')
    check("逐日對照：14 天各一列", len(dd) == 14, str(len(dd)))
    check("SCADA 斷線的前 7 天 scada_h 為 0（無從判斷），其餘天數有讀值",
          (dd['scada_h'].iloc[:7] == 0).all() and (dd['scada_h'].iloc[7:] >= 20).all(),
          str(dd['scada_h'].tolist()))
    check("斷線的日子頻率欄是空的，不是補上前一天的值",
          dd['freq_median'].iloc[:7].isna().all() and dd['freq_median'].iloc[7:].notna().all())
    check("有讀值的日子 min < max（PUMP A 的轉速每小時都在 42–58 Hz 之間跳）",
          (dd['freq_min'].iloc[7:] < dd['freq_max'].iloc[7:]).all())
    check("振動端每天都有運轉小時與 velRMS 中位數",
          (dd['vib_h'] >= 20).all() and dd['vel_median'].notna().all())
    check("對應表沒有的設備回傳空表，不拋錯", d_none.empty)

    # 「SCADA 讀值是 0 Hz（說停機）」與「根本沒有讀值（無從判斷）」是兩回事：
    # 台帳備機（如 CP 07）的頻率讀值多半是 0，不能在逐日表上被說成沒有資料。
    with tempfile.TemporaryDirectory() as tmp3:
        v3 = os.path.join(tmp3, 'vib')
        os.makedirs(v3)
        h3 = pd.date_range('2026-08-01', periods=3 * 24, freq='h')
        write_rows = [('STOP S_M1', (h + pd.Timedelta(minutes=m + 3)).strftime('%Y/%m/%d %H:%M:%S'), 0.25)
                      for h in h3 for m in range(0, 60, 10)]
        df3 = pd.DataFrame(write_rows, columns=['Name', 'Time', 'velRMS'])
        for i in range(3):
            df3[f'pad{i}'] = 0.0
        df3.to_csv(os.path.join(v3, 's.csv'), sep='\t', index=False, encoding='cp950')
        sc = []
        for h in h3:
            day_i = (h - h3[0]).days
            if day_i == 2:
                continue                                    # 第 3 天完全沒有讀值
            f0 = 45.0 if day_i == 0 else 0.0                # 第 1 天 45 Hz、第 2 天 0 Hz
            for m in range(0, 60, 10):
                sc.append(('T_S', (h + pd.Timedelta(minutes=m + 2)).strftime('%Y-%m-%d %H:%M:%S'), f0))
        pd.DataFrame(sc, columns=['TAGNAME', 'DATETIME', 'VALUE']).to_csv(
            os.path.join(tmp3, 'scada.csv'), index=False)
        pd.DataFrame([('STOP S_M1', 'FACC|T_S', 'frequency', 'Hz', 'TRUE', '')],
                     columns=['device_id', 'tag_id', 'variable_type', 'unit', 'is_active', 'note']
                     ).to_csv(os.path.join(tmp3, 'tagmap.csv'), index=False, encoding='utf-8-sig')
        d3 = daily(v3, os.path.join(tmp3, 'tagmap.csv'), os.path.join(tmp3, 'scada.csv'), 'STOP S_M1')
    r3 = d3.set_index('day')
    check("運轉日：有讀值、且 > 0 Hz 的小時數等於讀值小時數",
          r3['scada_h'].iloc[0] == 24 and r3['scada_run_h'].iloc[0] == 24)
    check("SCADA 說停機（全 0 Hz）：有讀值（24 小時）但 > 0 Hz 的小時為 0，頻率欄為空",
          r3['scada_h'].iloc[1] == 24 and r3['scada_run_h'].iloc[1] == 0
          and pd.isna(r3['freq_median'].iloc[1]), str(r3.iloc[1].to_dict()))
    check("完全沒有讀值的日子：scada_h 為 0（與 0 Hz 區分）",
          r3['scada_h'].iloc[2] == 0 and r3['scada_run_h'].iloc[2] == 0, str(r3.iloc[2].to_dict()))
    check("停機日振動端仍有「運轉」小時（0.25 > 0.1 門檻）——正是 SCADA 與振動說法不一致的樣子",
          (r3['vib_h'] == 24).all())
    import contextlib
    import io
    buf3 = io.StringIO()
    from validate.speed_effect import report_daily
    with contextlib.redirect_stdout(buf3):
        report_daily('STOP S_M1', d3)
    check("逐日報告把「0 Hz 停機」與「沒有讀值」用不同文字講出來",
          'SCADA 說停機' in buf3.getvalue() and '沒有 SCADA 讀值' in buf3.getvalue(), buf3.getvalue())

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
    # 篩選下推（先濾設備與運轉門檻、再解析時間）之後，表裡只剩有運轉的設備；
    # 若不另外記檔內出現過的設備名，「名稱對不上」與「沒運轉」會混成一種訊息。
    check("名稱對得上但整段沒運轉的設備，歸在 no_running，不算名稱對不上",
          info['no_running'] == ['PUMP IDLE'] and 'PUMP IDLE' not in info['map_only'],
          f"no_running={info['no_running']}, map_only={info['map_only']}")
    check("沒運轉的設備不會出現在結果表", 'PUMP IDLE' not in v)

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

    # ── 單元：貼著下限／上限轉速運轉的變頻泵 ──────────────
    # 實測（2026-06～08）CHP08、CHP09 有七成以上時間在 45 Hz 下限、CWP03 大多在
    # 60 Hz 上限。此時第 25 與第 75 百分位同值，若仍用四分位切，其中一組是空的，
    # 會把明明有變頻、velRMS 也確實隨轉速變的泵判成「轉速沒變」。
    rng2 = np.random.default_rng(3)
    n2 = 1500
    t2 = pd.date_range('2026-06-01', periods=n2, freq='h')

    def clamped(f):
        return pd.DataFrame({'ts_hour': t2, 'freq': f,
                             'vel': 1.5 + 0.10 * (f - 50) + rng2.normal(0, 0.15, n2)})

    floor80 = np.where(rng2.random(n2) < 0.8, 45.0, rng2.uniform(46, 60, n2).round(1))
    ceil80 = np.where(rng2.random(n2) < 0.8, 60.0, rng2.uniform(52, 59.9, n2).round(1))
    r_floor, r_ceil = analyze_pair(clamped(floor80)), analyze_pair(clamped(ceil80))
    check("80% 時間貼在下限轉速、velRMS 隨轉速上升 → 比較得出來（不是 no_variation）",
          r_floor['verdict'] in ('material', 'moderate') and r_floor['shift_sigma'] > 0,
          str((r_floor['verdict'], r_floor.get('n_low'), r_floor.get('n_high'))))
    check("80% 時間貼在上限轉速 → 同樣比較得出來",
          r_ceil['verdict'] in ('material', 'moderate') and r_ceil['n_low'] > 0 and r_ceil['n_high'] > 0,
          str((r_ceil['verdict'], r_ceil.get('n_low'), r_ceil.get('n_high'))))
    check("貼底案例的低轉速組是「貼底那一群」、高轉速組是「高於它」",
          r_floor['n_low'] > r_floor['n_high'] > 0, f"{r_floor['n_low']}/{r_floor['n_high']}")
    check("真正全程定頻仍判 no_variation（修正沒有把它放過）",
          analyze_pair(clamped(np.full(n2, 50.0)))['verdict'] == 'no_variation')

    # ── 單元：時間快速路徑與正式管線的 parse_datetime 一致 ──
    ts_ok = pd.Series(['2026/06/01 08:05:03', '2026/06/01 08:59:59', '2026/06/02 00:00:00',
                       '2026-06-02 23:30:30'])
    check("快速路徑：斜線／連字號格式的整點結果與 parse_datetime 相同",
          (floor_to_hour(ts_ok).reset_index(drop=True)
           == parse_datetime(ts_ok).dt.floor('h')).all())
    ts_odd = pd.Series(['2026/6/1 8:05:03', '2026/6/1 9:10:00'])      # 沒補零 → 必須退回
    check("格式不是預期的（沒補零）→ 退回 parse_datetime，結果仍正確",
          (floor_to_hour(ts_odd).reset_index(drop=True)
           == parse_datetime(ts_odd).dt.floor('h')).all()
          and floor_to_hour(ts_odd).notna().all())
    ts_mixed = pd.Series(['2026/06/01 08:05:03', 'garbage'])          # 混了壞值 → 退回、壞值為 NaT
    out_m = floor_to_hour(ts_mixed)
    check("含無法解析的列時退回完整解析，壞值成 NaT 而不是整批失敗",
          out_m.iloc[0] == pd.Timestamp('2026-06-01 08:00') and pd.isna(out_m.iloc[1]))

    # ── 單元：樣本不足時不下結論 ─────────────────────
    few = pd.DataFrame({'ts_hour': pd.date_range('2026-06-01', periods=MIN_PAIRED_HOURS - 1, freq='h'),
                        'vel': 1.0, 'freq': np.linspace(40, 60, MIN_PAIRED_HOURS - 1)})
    check(f"配對小時數 < {MIN_PAIRED_HOURS} → insufficient",
          analyze_pair(few)['verdict'] == 'insufficient')


def test_operating_state() -> None:
    print("\n[11b] 運轉狀態對照（validate.operating_state）與告警歸因（validate.alarm_attribution）")

    import tempfile

    from validate.alarm_attribution import (attribute, axis_position, detect_provenance,
                                            explain_fallback, explained_by_speed, read_episodes,
                                            report as attribution_report)
    from validate.operating_state import (classify_state, hourly_vibration_all, ledger_check,
                                          level_evidence, load_ledger, run, tri)

    rng = np.random.default_rng(11)
    hours = pd.date_range('2026-06-01', periods=10 * 24, freq='h')
    H = len(hours)

    def write_vib(path, name, vel_h):
        rows = [(name, (h + pd.Timedelta(minutes=m + 3)).strftime('%Y/%m/%d %H:%M:%S'),
                 max(0.0, v + rng.normal(0, 0.01)))
                for h, v in zip(hours, vel_h) for m in range(0, 60, 10)]
        df = pd.DataFrame(rows, columns=['Name', 'Time', 'velRMS'])
        for i in range(3):
            df[f'pad{i}'] = 0.0
        df.to_csv(path, sep='\t', index=False, encoding='cp950')

    def scada(tag, vals):
        return [(tag, (h + pd.Timedelta(minutes=m + 2)).strftime('%Y-%m-%d %H:%M:%S'), v)
                for h, v in zip(hours, vals) for m in range(0, 60, 10)]

    on = rng.random(H) < 0.8
    d_on = np.ones(H, bool)
    d_on[::20] = False

    with tempfile.TemporaryDirectory() as tmp:
        vdir = os.path.join(tmp, 'vib')
        os.makedirs(vdir)
        rows = []
        spec = [   # (名稱, 振動序列, SCADA 序列 or None, 台帳 standby, kW, rpm)
            ('RUN A_M1', np.where(on, 1.0, 0.02), np.where(on, 52.0, 0.0), 'FALSE', 30, 1760),
            ('RUN A_M2', np.where(on, 1.1, 0.02), np.where(on, 52.0, 0.0), 'FALSE', 30, 1760),
            ('IDLE B_M1', np.full(H, 0.15), np.zeros(H), 'TRUE', 30, 1760),          # 停機但被撐過 0.1
            ('BYPASS C_M1', np.full(H, 1.0), np.zeros(H), 'FALSE', 30, 1760),        # 旁路？水準像在轉
            ('CONFLICT D_M1', np.where(d_on, 1.0, 0.02), np.where(d_on, 50.0, 0.0), 'TRUE', 30, 1760),
            ('PRIMARY E_M1', np.full(H, 0.12), np.zeros(H), 'FALSE', 30, 1760),
            ('CONST F_M1', np.full(H, 0.8), np.full(H, 60.0), 'FALSE', 45, 1180),    # 定頻 tag 無資訊
            ('QUIET G_M1', np.full(H, 0.03), np.zeros(H), 'TRUE', 45, 1180),
            ('LONE I_M1', np.full(H, 0.15), np.zeros(H), 'TRUE', 99, 900),           # 無同型可比，台帳標備機
            ('LONE J_M1', np.full(H, 0.15), np.zeros(H), 'FALSE', 98, 900),          # 無同型可比，台帳非備機
        ]
        ledger_rows = []
        for i, (name, v, f, sb, kw, rpm) in enumerate(spec):
            write_vib(os.path.join(vdir, f'{i}.csv'), name, v)
            rows += scada(f'T{i}', f)
            ledger_rows.append((name, rpm, kw, sb))
        write_vib(os.path.join(vdir, 'h.csv'), 'NAME MISMATCH H_M1', np.where(on, 1.0, 0.02))
        rows += scada('TH', np.where(on, 52.0, 0.0))
        ledger_rows.append(('NAMEMISMATCH H_M1', 1760, 30, 'FALSE'))      # 台帳寫法少一個空白

        os.makedirs(os.path.join(tmp, 'scada'))
        pd.DataFrame(rows, columns=['TAGNAME', 'DATETIME', 'VALUE']).to_csv(
            os.path.join(tmp, 'scada', 'all.csv'), index=False)
        tm = [(name, f'F|T{i}') for i, (name, *_r) in enumerate(spec)] + [('NAME MISMATCH H_M1', 'F|TH')]
        pd.DataFrame([(d, t, 'frequency', 'Hz', 'TRUE', '') for d, t in tm],
                     columns=['device_id', 'tag_id', 'variable_type', 'unit', 'is_active', 'note']
                     ).to_csv(os.path.join(tmp, 'tagmap.csv'), index=False, encoding='utf-8-sig')
        pd.DataFrame(ledger_rows, columns=['device_id', 'rated_rpm', 'rated_power_kw', 'is_standby']
                     ).to_csv(os.path.join(tmp, 'ledger.csv'), index=False, encoding='utf-8-sig')

        table, info = run(vdir, os.path.join(tmp, 'tagmap.csv'), os.path.join(tmp, 'scada'),
                          os.path.join(tmp, 'ledger.csv'), stride=1)
        ledger_df = load_ledger(os.path.join(tmp, 'ledger.csv'))

        # ── 為 alarm_attribution 準備 episodes ──
        old_ev = ('{"primary_metric": "acc_kurt_median", "channels": {"crest": {}, "kurt": {"exceeded": true}, '
                  '"crest_axis": {}, "kurt_axis": {}}, "require_both": false}')
        new_ev = '{"primary_metric": "acc_crest_median", "channels": {"crest": {}, "crest_axis": {}}}'

        def episodes(ev, mc):
            recs = []
            for dev, rule, day, n, b, c in (
                    ('IDLE B_M1', 'VEL_HIGH', 5, 12, 0.102, 0.115),
                    ('IDLE B_M1', 'IMPACT_RISE', 8, 1, 3.4, 4.3),
                    ('SPEED P_M1', 'VEL_HIGH', 5, 12, 0.556, 1.142),
                    ('SPEED P_M1', 'VEL_HIGH', 18, 1, 0.556, 0.62),
                    ('SPEED Q_M1', 'VEL_HIGH', 8, 1, 0.6737, 0.7035),
                    ('NORMAL N_M1', 'VEL_HIGH', 1, 3, 0.8, 0.95),
                    ('CONFOUNDED K_M1', 'VEL_HIGH', 1, 2, 0.5, 0.9)):
                s0 = pd.Timestamp('2026-06-01 23:00') + pd.Timedelta(days=day)
                recs.append(dict(device_id=dev, rule_code=rule, severity='warn', is_actionable=True,
                                 episode_start=s0.strftime('%Y/%m/%d %H:%M'),
                                 episode_end=(s0 + pd.Timedelta(days=n - 1)).strftime('%Y/%m/%d %H:%M'),
                                 duration_days=n, threshold_mode='sigma_fallback' if mc == '' else 'iso',
                                 machine_class=mc, baseline_value=b, current_value=c,
                                 evidence_json=ev if rule == 'IMPACT_RISE' else ''))
            return pd.DataFrame(recs)

        epi_old = os.path.join(tmp, 'epi_old.csv')
        epi_new = os.path.join(tmp, 'epi_new.csv')
        episodes(old_ev, '').to_csv(epi_old, index=False, encoding='utf-8-sig')
        episodes(new_ev, 'G2/rigid').to_csv(epi_new, index=False, encoding='utf-8-sig')
        # 使用者貼上的是 TAB 分隔（從試算表複製）；也要讀得進來
        episodes(old_ev, '').to_csv(os.path.join(tmp, 'epi_tab.csv'), index=False, sep='\t')

        epi = read_episodes(epi_old)
        epi_tab = read_episodes(os.path.join(tmp, 'epi_tab.csv'))
        prov_old = detect_provenance(epi)
        prov_new = detect_provenance(read_episodes(epi_new))
        speed = pd.DataFrame([
            dict(device_id='SPEED P_M1', verdict='material', vel_low=0.555, vel_high=1.034),
            dict(device_id='SPEED Q_M1', verdict='material', vel_low=0.497, vel_high=0.673),
            dict(device_id='CONFOUNDED K_M1', verdict='confounded', vel_low=0.5, vel_high=0.9),
            dict(device_id='NORMAL N_M1', verdict='minor', vel_low=0.8, vel_high=0.81)])
        seg = attribute(epi, table, speed)

    t = table.set_index('device_id')

    # ── 三方對照 ──
    check("有開有關的泵：SCADA 與振動都說在轉 → agree_running",
          t.loc['RUN A_M1', 'state'] == 'agree_running' and t.loc['RUN A_M1', 'finding'] == 'running_confirmed',
          str(t.loc['RUN A_M1', ['state', 'finding']].to_dict()))
    check("備機：SCADA 全零、振動 0.15（> 0.1 門檻）→ stopped_but_vib_running，水準遠低於同型運轉泵 → idle_looks_running",
          t.loc['IDLE B_M1', 'finding'] == 'idle_looks_running'
          and t.loc['IDLE B_M1', 'level_ratio'] < 0.5 and t.loc['IDLE B_M1', 'state'] == 'stopped_but_vib_running',
          str(t.loc['IDLE B_M1', ['state', 'finding', 'level_ratio']].to_dict()))
    check("SCADA 全零、振動水準與同型運轉泵相近、台帳非備機 → unexplained_running（旁路或 tag 問題，不下結論）",
          t.loc['BYPASS C_M1', 'finding'] == 'unexplained_running', str(t.loc['BYPASS C_M1', 'finding']))
    check("台帳標備機卻整段在轉 → standby_but_running",
          t.loc['CONFLICT D_M1', 'ledger_check'] == 'standby_but_running',
          str(t.loc['CONFLICT D_M1', ['state', 'ledger_check']].to_dict()))
    check("台帳標非備機、SCADA 整段沒轉 → primary_but_stopped（且水準像環境振動）",
          t.loc['PRIMARY E_M1', 'ledger_check'] == 'primary_but_stopped'
          and t.loc['PRIMARY E_M1', 'finding'] == 'idle_looks_running')
    check("定頻 tag（恆為 60）沒有運轉資訊 → no_scada，不拿來判斷",
          t.loc['CONST F_M1', 'state'] == 'no_scada', str(t.loc['CONST F_M1', 'state']))
    check("SCADA 全零、振動也安靜 → agree_stopped", t.loc['QUIET G_M1', 'state'] == 'agree_stopped')
    check("沒有同型運轉泵可比、但台帳標備機 → 仍可判 idle_looks_running（有台帳佐證）",
          t.loc['LONE I_M1', 'finding'] == 'idle_looks_running' and t.loc['LONE I_M1', 'level_evidence'] == 'unknown',
          str(t.loc['LONE I_M1', ['finding', 'level_evidence']].to_dict()))
    check("沒有同型可比、台帳也沒說備機 → unclear，不硬猜",
          t.loc['LONE J_M1', 'finding'] == 'unclear', str(t.loc['LONE J_M1', 'finding']))
    check("台帳 id 少一個空白 → 被認出是近似名稱而不是靜默忽略",
          info['ledger_near'] == {'NAMEMISMATCH H_M1': 'NAME MISMATCH H_M1'}, str(info['ledger_near']))

    # 回歸：台帳每一格都有填時 pandas 轉成 numpy 布林，`is True` 會落空，備機被當成非備機
    check("numpy 布林的備機旗標被正確讀成 True（回歸）",
          tri(np.True_) is True and tri(np.False_) is False and tri(None) is None and tri(float('nan')) is None)
    check("台帳讀進來是 Python 布林／None，不是 numpy 布林",
          all(isinstance(x, bool) or x is None for x in ledger_df['is_standby']))
    check("台帳標備機 + SCADA 說停 → ledger_check 為 ok，而不是被誤判成 primary_but_stopped",
          t.loc['IDLE B_M1', 'ledger_check'] == 'ok' and t.loc['QUIET G_M1', 'ledger_check'] == 'ok',
          str(t.loc[['IDLE B_M1', 'QUIET G_M1'], 'ledger_check'].to_dict()))

    # 分類邊界
    check("SCADA 運轉 30% → intermittent", classify_state(0.3, 100, 0.9, 0.9) == 'intermittent')
    check("SCADA 說在轉、振動沒過門檻 → scada_on_vib_quiet（運轉但很安靜，與反向的失效分開）",
          classify_state(0.9, 100, 0.1, 0.1) == 'scada_on_vib_quiet')
    check("SCADA 小時數不足 → no_scada", classify_state(0.0, 10, 1.0, 1.0) == 'no_scada')
    check("水準比值 0.3 → ambient_like；0.9 → running_like；缺值 → unknown",
          level_evidence(0.3) == 'ambient_like' and level_evidence(0.9) == 'running_like'
          and level_evidence(None) == 'unknown')
    check("ledger_check 未填 → unfilled", ledger_check(None, 'agree_stopped') == 'unfilled')

    # stride：逐秒資料抽樣不改變小時中位數
    with tempfile.TemporaryDirectory() as tmp2:
        pth = os.path.join(tmp2, 'x.csv')
        write_vib(pth, 'S_M1', np.full(H, 0.5))
        f1, _ = hourly_vibration_all(pth, 1)
        f2, _ = hourly_vibration_all(pth, 2)
    check("抽樣（stride=2）後筆數減半、小時中位數不變",
          abs(float(f2['vel_med'].median()) - float(f1['vel_med'].median())) < 0.005
          and int(f2['n_rows'].median()) * 2 == int(f1['n_rows'].median()),
          f"{f1['n_rows'].median()} vs {f2['n_rows'].median()}")

    # ── 告警歸因 ──
    check("軸位置：低端 0、高端 1、超過高端 > 1；兩端幾乎相同時回 NaN",
          axis_position(0.5, 0.5, 1.0) == 0 and axis_position(1.0, 0.5, 1.0) == 1
          and axis_position(1.5, 0.5, 1.0) > 1 and np.isnan(axis_position(1.0, 0.5, 0.5)))
    check("基準在低轉速端、當前走到高轉速端 → 由轉速解釋",
          explained_by_speed(0.556, 1.142, 0.555, 1.034))
    check("基準在高轉速端、當前掉到低轉速端 → 同樣算（方向相反）",
          explained_by_speed(1.0, 0.55, 0.5, 1.0))
    check("基準本來就在高轉速端、只是小幅超標 → 不算由轉速解釋",
          not explained_by_speed(0.6737, 0.7035, 0.497, 0.673))
    check("基準在低端、當前只動一點點 → 不算", not explained_by_speed(0.556, 0.62, 0.555, 1.034))
    # 實測（2026-10-11）：只看「走到另一端」會把 CP 08_M1（高轉速水準 1.43，當前 1.80、
    # 軸位置 2.4）與 CP 09_M1（0.845，當前 0.92、軸位置 2.3）也算成轉速造成的。
    check("當前值遠超過高轉速水準 → 不算（CP 08_M1：1.16→1.80，高轉速水準只有 1.43）",
          not explained_by_speed(1.159, 1.798, 1.162, 1.427))
    check("略高於高轉速水準仍算（CP 04_M1：1.14，軸位置 1.22）",
          explained_by_speed(0.556, 1.142, 0.555, 1.034))
    check("超出一個軸長的一半是邊界：1.5 算、1.6 不算",
          explained_by_speed(0.0, 1.5, 0.0, 1.0) and not explained_by_speed(0.0, 1.6, 0.0, 1.0))
    check("方向相反（基準在高端、當前往下掉）同樣有下界：−0.5 算、−0.6 不算",
          explained_by_speed(1.0, -0.5, 0.0, 1.0) and not explained_by_speed(1.0, -0.6, 0.0, 1.0))

    sg = seg.set_index(['device_id', 'rule_code', 'duration_days'])['segment']
    check("停機被撐過門檻的設備：VEL_HIGH 與其他規則的事件都歸 idle_looks_running",
          (seg[seg['device_id'] == 'IDLE B_M1']['segment'] == 'idle_looks_running').all())
    check("轉速敏感設備的大事件（12 天、0.56→1.14）→ speed_explained",
          sg[('SPEED P_M1', 'VEL_HIGH', 12)] == 'speed_explained')
    check("同一台設備的小幅超標（0.56→0.62）→ speed_device_other",
          sg[('SPEED P_M1', 'VEL_HIGH', 1)] == 'speed_device_other'
          and (seg[seg['device_id'] == 'SPEED Q_M1']['segment'] == 'speed_device_other').all())
    check("confounded／minor 的設備不歸入轉速驅動 → other",
          (seg[seg['device_id'].isin(['CONFOUNDED K_M1', 'NORMAL N_M1'])]['segment'] == 'other').all())

    check("版本檢查：IMPACT_RISE 帶 kurt 通道／require_both ＝ 移除 kurtosis 之前的程式",
          prov_old['legacy_kurtosis'] is True and prov_new['legacy_kurtosis'] is False,
          f"{prov_old} / {prov_new}")
    # pandas 3 的 astype(str) 不再把缺值變成 'nan'；整欄空白的 machine_class 必須仍被認成「沒帶台帳」
    check("整欄 machine_class 空白 ＝ 這輪沒帶台帳（不因 pandas 版本而失效）",
          prov_old['ledger_share'] == 0.0 and prov_new['ledger_share'] == 1.0,
          f"{prov_old['ledger_share']} / {prov_new['ledger_share']}")
    check("TAB 分隔（從試算表複製貼上）的 episodes 也讀得進來，欄位數正確",
          len(epi_tab) == len(epi) and 'device_id' in epi_tab.columns and epi_tab.shape[1] == epi.shape[1],
          f"{epi_tab.shape} vs {epi.shape}")

    # ── 有沒有帶台帳：不能只看 VEL_HIGH ──
    # 帶了台帳的那一輪，ISO 錨定的點若沒觸發，剩下的 VEL_HIGH 全是 sigma_fallback、
    # machine_class 全空，跟沒帶台帳長得一樣。實測（2026-10-11）就被誤報成「沒帶台帳」。
    vh_rows = [dict(device_id='SMALL S_M1', rule_code='VEL_HIGH', threshold_mode='sigma_fallback',
                    machine_class='', evidence_json='{"iso_fallback_reason": "額定功率 11.0 kW 未超過 15 kW"}'),
               dict(device_id='SMALL S_M2', rule_code='VEL_HIGH', threshold_mode='sigma_fallback',
                    machine_class='', evidence_json='{"iso_fallback_reason": "額定功率 11.0 kW 未超過 15 kW"}')]
    ledger_run = pd.DataFrame(vh_rows + [
        dict(device_id='BIG B_M1', rule_code='STANDBY_NO_RUNTIME', threshold_mode='', machine_class=''),
        dict(device_id='BIG C_M1', rule_code='ISO_CLASS_SUSPECT', threshold_mode='', machine_class='2/rigid')])
    p_led = detect_provenance(ledger_run)
    p_no = detect_provenance(pd.DataFrame(vh_rows))
    p_unk = detect_provenance(pd.DataFrame([dict(device_id='X', rule_code='TEMP_RISE')]))
    check("VEL_HIGH 全是 sigma_fallback、但有 STANDBY_NO_RUNTIME／ISO_CLASS_SUSPECT → 判為有帶台帳（回歸）",
          p_led['ledger'] == 'yes' and p_led['ledger_share'] == 0.0
          and 'STANDBY_NO_RUNTIME' in p_led['ledger_signals'] and 'ISO_CLASS_SUSPECT' in p_led['ledger_signals'],
          str(p_led))
    check("只有 sigma_fallback 的 VEL_HIGH、沒有任何帶台帳的跡象 → likely_no（不說死）",
          p_no['ledger'] == 'likely_no', str(p_no['ledger']))
    check("沒有 VEL_HIGH 也沒有其他跡象 → unknown", p_unk['ledger'] == 'unknown')
    check("舊版（kurt）那份 episodes 沒有帶台帳的跡象 → likely_no",
          prov_old['ledger'] == 'likely_no' and prov_new['ledger'] == 'yes',
          f"{prov_old['ledger']} / {prov_new['ledger']}")
    check("evidence 裡的退回原因被統計出來（新版程式才有）",
          p_led['fallback_reasons'] == {'額定功率 11.0 kW 未超過 15 kW': 2}, str(p_led['fallback_reasons']))

    # ── 逐點說明 VEL_HIGH 為什麼走 sigma_fallback ──
    overrides = {
        'SMALL S_M1': dict(rated_power_kw=11.0, iso_machine_group='4', iso_foundation='rigid', is_standby=True),
        'SMALL S_M2': dict(rated_power_kw=15.0, iso_machine_group='4', iso_foundation='rigid', is_standby=False),
        'HALF H_M1': dict(rated_power_kw=30.0, iso_machine_group='2'),
        'BIG B_M1': dict(rated_power_kw=45.0, iso_machine_group='2', iso_foundation='rigid'),
    }
    t0 = pd.Timestamp('2026-06-05')
    fb_rows = [dict(device_id=d, rule_code='VEL_HIGH', threshold_mode='sigma_fallback',
                    episode_start=t0, episode_end=t0, duration_days=2, baseline_value=1.0, current_value=1.2)
               for d in ('SMALL S_M1', 'SMALL S_M2', 'HALF H_M1', 'BIG B_M1', 'GHOST G_M1')]
    fb_rows.append(dict(device_id='ISO I_M1', rule_code='VEL_HIGH', threshold_mode='iso',
                        episode_start=t0, episode_end=t0, duration_days=1, baseline_value=1.0, current_value=2.0))
    fb_df = pd.DataFrame(fb_rows)
    fb = explain_fallback(fb_df, overrides)
    why = fb.set_index('device_id')['reason'].to_dict()
    check("額定 11 kW／恰為 15 kW（標準寫「大於 15 kW」）→ out_of_scope_power，台帳有分類也一樣",
          why['SMALL S_M1'] == 'out_of_scope_power' and why['SMALL S_M2'] == 'out_of_scope_power', str(why))
    check("基礎剛性沒填 → unclassified；台帳沒有這個 id → not_in_ledger",
          why['HALF H_M1'] == 'unclassified' and why['GHOST G_M1'] == 'not_in_ledger', str(why))
    check("分類齊全、功率也在範圍內卻仍退回 → classified_but_fallback（那一輪沒套用台帳）",
          why['BIG B_M1'] == 'classified_but_fallback', str(why))
    check("ISO 模式的點不在 sigma_fallback 說明裡", 'ISO I_M1' not in why)

    import contextlib
    import io
    # 全部有值的備機欄（真實台帳填完整時就是這樣）也要印得出「備機」標記。
    fb_bool = explain_fallback(fb_df[fb_df['device_id'].isin(['SMALL S_M1', 'SMALL S_M2'])], overrides)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        attribution_report(fb_df.assign(machine_class=''), detect_provenance(fb_df.assign(machine_class='')),
                           False, False, fb_bool, {'CH01_M1': 'CH 01_M1'})
    out_txt = buf.getvalue()
    check("報告逐點列出原因與備機標記",
          'out_of_scope_power' in out_txt and 'SMALL S_M1(11kW·備機)' in out_txt
          and 'SMALL S_M2(15kW)' in out_txt, out_txt[-600:])
    check("台帳 id 寫法對不上時在報告裡明講沒生效", "CH01_M1" in out_txt and '沒有生效' in out_txt)

    check("歸因結果記下轉速敏感點數／總點數（供「是否偏多」計算）",
          seg.attrs.get('speed_points') == (2, 4), str(seg.attrs.get('speed_points')))
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        attribution_report(seg, prov_old, True, True)
    check("給了 speed-effect 就印出「轉速敏感設備的告警是否偏多」",
          '轉速敏感設備的告警是否偏多' in buf2.getvalue())


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


def test_ledger_import_logic() -> None:
    print("\n[12c] 台帳匯入資料庫：欄位正規化、合併規則、覆蓋摘要（不需要資料庫的部分）")

    from vibcore.ledger_import import (coverage, normalize_date, normalize_driver,
                                       normalize_foundation, normalize_group, normalize_power,
                                       plan_row)

    check("群組：'2'、2、'2.0'（試算表把整數存成小數）都認得為 '2'",
          normalize_group('2')[0] == '2' and normalize_group(2)[0] == '2'
          and normalize_group('2.0')[0] == '2')
    check("群組：空白＝沒填（不是錯）；0、5、'二' 是錯",
          normalize_group('') == (None, None) and normalize_group('nan') == (None, None)
          and all(normalize_group(x)[1] for x in ('0', '5', '二', '2.5')))
    check("基礎剛性：rigid／剛性／Rigid 都認得；亂填是錯",
          normalize_foundation('Rigid')[0] == 'rigid' and normalize_foundation('柔性')[0] == 'flexible'
          and normalize_foundation('hard')[1] is not None)
    check("驅動型式：外接／整合／external 都認得",
          normalize_driver('外接')[0] == 'external' and normalize_driver('整合')[0] == 'integrated'
          and normalize_driver('x')[1] is not None)
    check("日期：2026-04-02 認得；不是日期是錯；空白＝沒填",
          normalize_date('2026-04-02') == ('2026-04-02', None) and normalize_date('昨天')[1]
          and normalize_date('') == (None, None))
    check("功率：正數才接受；0 與負數是錯", normalize_power(45.0)[0] == 45.0
          and normalize_power(0)[1] and normalize_power(-3)[1])

    # ── 合併規則 ──
    cur = {'rated_power_kw': 45, 'iso_machine_group': None, 'iso_foundation': None,
           'iso_driver_type': None, 'is_standby': False, 'last_maintenance_at': None,
           'machine_type': None, 'iso_class_source': 'unset'}
    full = plan_row('A_M1', {'iso_machine_group': '2.0', 'iso_foundation': '剛性',
                             'iso_driver_type': '外接', 'is_standby': True,
                             'rated_power_kw': 45.0}, cur)
    check("群組與基礎剛性都給了 → 分類來源設為 manual_override（否則分類不會生效）",
          full.new['iso_class_source'] == 'manual_override'
          and full.changes['iso_class_source'] == ('unset', 'manual_override'), str(full.changes))
    check("寫入值已正規化（'2.0'→'2'、剛性→rigid、外接→external）",
          full.new['iso_machine_group'] == '2' and full.new['iso_foundation'] == 'rigid'
          and full.new['iso_driver_type'] == 'external')
    check("相同的值不算變更（額定功率 45 → 45.0 沒有變）", 'rated_power_kw' not in full.changes)

    only_group = plan_row('A_M1', {'iso_machine_group': '2'}, cur)
    check("只給群組、基礎剛性沒有 → 分類來源維持 unset（分類不齊就不會啟用）",
          only_group.new['iso_class_source'] == 'unset')
    cur_f = {**cur, 'iso_foundation': 'rigid'}
    check("資料庫已有基礎剛性、台帳補群組 → 合併後齊備，分類來源設為 manual_override",
          plan_row('A_M1', {'iso_machine_group': '2'}, cur_f).new['iso_class_source'] == 'manual_override')

    cur_ok = {**cur, 'iso_machine_group': '2', 'iso_foundation': 'rigid',
              'iso_class_source': 'manual_override', 'is_standby': True}
    blank = plan_row('A_M1', {'iso_machine_group': '', 'iso_foundation': '', 'is_standby': None}, cur_ok)
    check("台帳留空＝不動：不會把資料庫已有的群組、剛性、備機清掉",
          not blank.changed and blank.new['iso_machine_group'] == '2' and blank.new['is_standby'] is True,
          str(blank.changes))
    flip = plan_row('A_M1', {'is_standby': False}, cur_ok)
    check("明確填 FALSE 仍可把備機旗標改回來（只擋留空，不擋『確認不是備機』）",
          flip.new['is_standby'] is False and 'is_standby' in flip.changes)
    check("某欄不合法 → 整列標錯、不套用（群組 9）",
          plan_row('A_M1', {'iso_machine_group': '9', 'is_standby': True}, cur).error is not None)
    check("重複套用同一份台帳沒有變更（冪等）",
          not plan_row('A_M1', {'iso_machine_group': '2', 'iso_foundation': 'rigid'}, cur_ok).changed)
    new_dev = plan_row('N_M1', {'iso_machine_group': '4', 'iso_foundation': 'rigid',
                                'rated_power_kw': 11.0}, None)
    check("設備尚未存在（--create-missing）：以全空白為基準合併", new_dev.new['iso_machine_group'] == '4'
          and new_dev.new['iso_class_source'] == 'manual_override')

    # ── 覆蓋摘要 ──
    cov = coverage({
        'OK_M1': {'iso_machine_group': '2', 'iso_foundation': 'rigid', 'rated_power_kw': 45,
                  'iso_class_source': 'manual_override', 'is_standby': True},
        'SMALL_M1': {'iso_machine_group': '4', 'iso_foundation': 'rigid', 'rated_power_kw': 11,
                     'iso_class_source': 'manual_override'},
        'EDGE_M1': {'iso_machine_group': '4', 'iso_foundation': 'rigid', 'rated_power_kw': 15,
                    'iso_class_source': 'manual_override'},
        'HALF_M1': {'iso_machine_group': '2', 'iso_foundation': None, 'rated_power_kw': 30},
        'UNSET_M1': {'iso_machine_group': '2', 'iso_foundation': 'rigid', 'rated_power_kw': 30,
                     'iso_class_source': 'unset'},
        'NONE_M1': {'rated_power_kw': None},
    })
    check("覆蓋摘要：ISO 會啟用／額定 ≤15 kW（含恰為 15）／分類不完整／來源未設／未分類各歸各類",
          cov['iso_ready'] == ['OK_M1'] and cov['out_of_scope'] == ['EDGE_M1', 'SMALL_M1']
          and cov['partial'] == ['HALF_M1', 'UNSET_M1'] and cov['unclassified'] == ['NONE_M1'],
          str({k: v for k, v in cov.items()}))
    check("額定功率沒填的單獨列出（無法檢查適用範圍）；備機另計",
          cov['power_missing'] == ['NONE_M1'] and cov['standby'] == ['OK_M1'])


def test_review_sheet() -> None:
    print("\n[12d] 現場回饋試算表：匯出、判定欄正規化、雜訊率統計（validate.review_sheet）")

    import tempfile

    from validate.review_sheet import (MIN_DECIDED, SHEET_COLUMNS, build_summary, cause_tag,
                                       from_episodes, normalize_verdict, read_sheets, score,
                                       select_items, wilson_interval, write_sheet)

    # ── 判定欄的各種寫法 ──
    check("中英文判定都認得：真實／real／是 → real；雜訊／誤報／noise → noise",
          normalize_verdict('真實') == 'real' and normalize_verdict(' Real ') == 'real'
          and normalize_verdict('誤報') == 'noise' and normalize_verdict('NOISE') == 'noise'
          and normalize_verdict('已知') == 'known' and normalize_verdict('不確定') == 'unsure')
    check("空白＝未看；認不得的字＝invalid（不能悄悄算成某一類）",
          normalize_verdict('') == 'unreviewed' and normalize_verdict(None) == 'unreviewed'
          and normalize_verdict(float('nan')) == 'unreviewed' and normalize_verdict('大概吧') == 'invalid')
    check("原因欄歸類：取最先出現的標準詞；沒有標準詞回 None",
          cause_tag('鄰機帶動，CP 08 升速') == '鄰機' and cause_tag('轉速負載變了') == '轉速負載'
          and cause_tag('不知道為什麼') is None and cause_tag('') is None)

    # ── 信賴區間 ──
    lo0, hi0 = wilson_interval(0, 0)
    lo1, hi1 = wilson_interval(5, 10)
    lo2, hi2 = wilson_interval(50, 100)
    check("Wilson 區間：n=0 回 (0,1)；n 越大區間越窄；永遠在 0～1 內",
          (lo0, hi0) == (0.0, 1.0) and (hi2 - lo2) < (hi1 - lo1)
          and 0 <= lo1 < 0.5 < hi1 <= 1 and wilson_interval(0, 5)[0] < 1e-9
          and wilson_interval(5, 5)[1] > 1 - 1e-9)

    # ── 統計 ──
    def sheet(rule, sev, verdicts, cause=''):
        return pd.DataFrame([{'item_id': f'{rule}|{i}', 'rule_code': rule, 'severity': sev,
                              '判定': v, '原因': cause} for i, v in enumerate(verdicts)])
    allv = pd.concat([
        sheet('NOISY', 'observe', ['雜訊'] * 12 + ['真實'] * 3, '鄰機'),
        sheet('GOOD', 'observe', ['真實'] * 11 + ['雜訊']),
        sheet('MIXED', 'warn', ['真實'] * 5 + ['雜訊'] * 5),
        sheet('SMALL', 'observe', ['雜訊'] * 4),
        sheet('UNK', 'observe', ['不確定'] * 6 + ['已知'] * 2 + [''] * 4 + ['亂寫']),
    ], ignore_index=True)
    r = score(allv).set_index('rule_code')
    check("雜訊率＝雜訊÷(真實＋已知＋雜訊)：NOISY 12/15",
          abs(r.loc['NOISY', 'noise_rate'] - 12 / 15) < 1e-9 and r.loc['NOISY', 'decided'] == 15)
    check("不確定與未看不算進分母，且另外計數；寫不出來的字記為 invalid",
          r.loc['UNK', 'decided'] == 2 and r.loc['UNK', 'unsure'] == 6
          and r.loc['UNK', 'unreviewed'] == 4 and r.loc['UNK', 'invalid'] == 1, str(r.loc['UNK'].to_dict()))
    check("雜訊偏多的觀察規則：建議維持觀察、不升級", '維持觀察名單' in r.loc['NOISY', 'suggestion'])
    check("雜訊偏多的派工規則（warn）：建議不該派工", '不該派工' in r.loc['MIXED', 'suggestion']
          or '真假參半' in r.loc['MIXED', 'suggestion'])
    check("多數為真、雜訊低的觀察規則：建議評估升級，並提醒先分清楚『已知』",
          '評估升為 warn' in r.loc['GOOD', 'suggestion'] and '已知' in r.loc['GOOD', 'suggestion'])
    check(f"已判不到 {MIN_DECIDED} 件：只說樣本不足，不給調整建議",
          '樣本不足' in r.loc['SMALL', 'suggestion'] and '樣本不足' in r.loc['UNK', 'suggestion'])
    check("雜訊原因按標準詞彙總", r.loc['NOISY', 'top_noise_causes'] == '鄰機×12', r.loc['NOISY', 'top_noise_causes'])
    dup = pd.concat([sheet('D', 'observe', ['雜訊'] * 10), sheet('D', 'observe', ['真實'] * 10)],
                    ignore_index=True)
    rd = score(dup).set_index('rule_code')
    check("同一個 item_id 出現兩次（兩人都填）取最後一筆，不重複計算（否則雜訊率看起來比實際更有把握）",
          rd.loc['D', 'items'] == 10 and rd.loc['D', 'real'] == 10)

    # ── 匯出：抽樣、欄位、摘要 ──
    check("摘要句帶出數值變化與持續天數",
          build_summary('速度整體值偏高', 0.556, 1.142, 'mm/s', 12) == '速度整體值偏高：0.556 → 1.14 mm/s（+105%），持續 12 天',
          build_summary('速度整體值偏高', 0.556, 1.142, 'mm/s', 12))
    check("沒有數值的事件類只講規則名與天數",
          build_summary('感測器離線', None, None, '', 3) == '感測器離線，持續 3 天')
    check("基準欄存的是門檻的規則，寫成「目前 X（門檻 Y）」，不是「Y → X（+n%）」",
          build_summary('軸能量分佈偏移', 0.15, 0.1503, '', 61, 'AXIS_SHIFT')
          == '軸能量分佈偏移：目前 0.1503（門檻 0.15），持續 61 天'
          and build_summary('感測器離線', 24, 29, 'hr', 2, 'SENSOR_OFFLINE')
          == '感測器離線：目前 29 hr（門檻 24），持續 2 天',
          build_summary('感測器離線', 24, 29, 'hr', 2, 'SENSOR_OFFLINE'))
    check("一般規則不受影響（有基準的仍寫「基準 → 目前」）",
          build_summary('速度整體值偏高', 0.556, 1.142, 'mm/s', 12, 'VEL_HIGH').startswith('速度整體值偏高：0.556 → 1.14'))

    recs = []
    for dev, rule, day, n, b, c, sev in (
            ('P1_M1', 'VEL_HIGH', 5, 12, 0.556, 1.142, 'observe'),
            ('P1_M1', 'VEL_HIGH', 20, 7, 0.556, 0.901, 'observe'),
            ('P1_M1', 'VEL_HIGH', 30, 1, 0.556, 0.662, 'observe'),
            ('P2_M1', 'VEL_HIGH', 8, 2, 0.30, 0.40, 'observe'),
            ('P3_M1', 'VEL_HIGH', 9, 1, 1.4, 1.56, 'observe'),
            ('P1_M1', 'SENSOR_OFFLINE', 3, 4, None, 29, 'err')):
        s0 = pd.Timestamp('2026-06-01 23:00') + pd.Timedelta(days=day)
        recs.append(dict(device_id=dev, rule_code=rule, rule_name=rule, severity=sev, is_actionable=sev != 'observe',
                         episode_start=s0.strftime('%Y/%m/%d %H:%M'),
                         episode_end=(s0 + pd.Timedelta(days=n - 1)).strftime('%Y/%m/%d %H:%M'),
                         duration_days=n, baseline_value=b, current_value=c,
                         value_unit='mm/s' if rule == 'VEL_HIGH' else 'hr', category='equipment'))
    with tempfile.TemporaryDirectory() as tmp:
        epi = os.path.join(tmp, 'epi.csv')
        pd.DataFrame(recs).to_csv(epi, index=False, encoding='utf-8-sig')
        led = os.path.join(tmp, 'ledger.csv')
        pd.DataFrame([dict(device_id='P1_M1', rated_power_kw=11, is_standby='TRUE')]).to_csv(
            led, index=False, encoding='utf-8-sig')
        full = from_episodes(epi, led, per_rule=8)
        top2 = from_episodes(epi, None, per_rule=2)
        out = os.path.join(tmp, 'sheet.csv')
        write_sheet(top2, out)
        guide_exists = os.path.exists(os.path.join(tmp, 'sheet_填寫說明.txt'))
        back = read_sheets([out])

    check("匯出的欄位固定且順序不變（回收時靠欄名）", list(full.columns) == SHEET_COLUMNS)
    check("判定／原因／處置欄是空白，等人填", (full['判定'] == '').all() and (full['原因'] == '').all())
    check("每條規則都被看到（VEL_HIGH 與 SENSOR_OFFLINE 各自出現）",
          set(full['rule_code']) == {'VEL_HIGH', 'SENSOR_OFFLINE'})
    check("同一台設備在同一條規則最多 2 筆（P1_M1 有 3 筆 VEL_HIGH，只列最長的 2 筆）",
          (full[full['rule_code'] == 'VEL_HIGH']['device_id'] == 'P1_M1').sum() == 2
          and set(full[(full['rule_code'] == 'VEL_HIGH') & (full['device_id'] == 'P1_M1')]['duration_days']) == {12, 7})
    check("--per-rule 限制每條規則的筆數", (top2['rule_code'] == 'VEL_HIGH').sum() == 2)
    check("脈絡欄帶出台帳備機與額定功率；沒給台帳就空白",
          full[full['device_id'] == 'P1_M1']['context'].iloc[0] == '台帳標備機、11 kW'
          and (top2['context'] == '').all())
    check("item_id 穩定可回收（規則|設備|起日）", 'VEL_HIGH|P1_M1|2026-06-06' in set(full['item_id']),
          str(sorted(full['item_id'])[:3]))
    check("寫出的檔案附填寫說明，且能被讀回（欄名不變）", guide_exists and list(back.columns) == SHEET_COLUMNS)
    check("select_items 對空表不拋錯", len(select_items(full.iloc[0:0].assign(delta_pct=0.0), 3)) == 0)

    # 填寫 → 回收 → 統計 的完整一圈
    filled = full.copy()
    filled.loc[filled['rule_code'] == 'VEL_HIGH', '判定'] = ['雜訊', '真實', '已知', '雜訊'][:int((filled['rule_code'] == 'VEL_HIGH').sum())]
    res = score(filled).set_index('rule_code')
    check("一圈走完：VEL_HIGH 已判 4 件、SENSOR_OFFLINE 未看", res.loc['VEL_HIGH', 'decided'] == 4
          and res.loc['SENSOR_OFFLINE', 'unreviewed'] == 1, str(res[['decided', 'unreviewed']].to_dict()))


def test_loader_empty_file() -> None:
    """整個檔案沒有可用讀值列時要說出來——否則這台設備憑空消失，沒有量測點，也就沒有離線告警。"""
    import logging
    import tempfile
    from vibcore.io.analytic_reader import load_analytic_dir

    print("\n[12e] 每日排程讀檔：沒有可用讀值的檔案要有警告")
    msgs: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            msgs.append(record.getMessage())
    lg = logging.getLogger('vibcore.io.analytic_reader')
    h, old_level, old_prop = _H(), lg.level, lg.propagate
    lg.addHandler(h)
    lg.setLevel(logging.WARNING)
    try:
        with tempfile.TemporaryDirectory() as d:
            good = pd.DataFrame({'Time': ['2026-06-01 00:00:00', '2026-06-01 00:10:00'],
                                 'Name': ['OK_M1', 'OK_M1'], 'velRMS': [0.5, 0.6]})
            good.to_csv(os.path.join(d, 'ok.csv'), index=False, encoding='utf-8-sig')
            pd.DataFrame({'Time': [], 'Name': [], 'velRMS': []}).to_csv(
                os.path.join(d, 'empty_CP01.csv'), index=False, encoding='utf-8-sig')
            pd.DataFrame({'Time': ['not a time', ''], 'Name': ['BAD_M1', 'BAD_M1'], 'velRMS': [1.0, 2.0]}).to_csv(
                os.path.join(d, 'badtime_CH06.csv'), index=False, encoding='utf-8-sig')
            got = load_analytic_dir(d)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
        lg.propagate = old_prop
    check("有讀值的檔案照常載入，沒有可用列的檔案不產生設備", sorted(got) == ['OK_M1'], str(sorted(got)))
    check("沒有可用列的檔案各有一則警告，帶出檔名",
          any('empty_CP01.csv' in m and '沒有可用的讀值列' in m for m in msgs)
          and any('badtime_CH06.csv' in m and '沒有可用的讀值列' in m for m in msgs), str(msgs))


def test_ledger_id_match() -> None:
    print("\n[12b] 台帳 device_id 與資料對不上：不能安靜失效")

    from validate.points import match_ledger_ids, warn_unmatched_overrides

    data_ids = ['CH 01_M1', 'CH 01_M2', 'CH03_M1', 'ZP 3-1-2_M1']
    ledger_ids = ['CH01_M1', 'CH01_M2', 'CH03_M1', 'ZP 3-1-2_M1', 'ZP 9-9_M1']
    near, missing = match_ledger_ids(ledger_ids, data_ids)
    check("只差空白的台帳 id 被認出是近似名稱（CH01_M1 ≈ CH 01_M1）",
          near == {'CH01_M1': 'CH 01_M1', 'CH01_M2': 'CH 01_M2'}, str(near))
    check("完全對得上的不列入；找不到任何相近名稱的列入 missing",
          missing == ['ZP 9-9_M1'], str(missing))
    check("近似比對不會把 id 悄悄改掉——只回報，由使用者去改台帳",
          'CH01_M1' not in data_ids)

    # 同一個近似鍵在資料裡對到兩個以上的名稱時，不猜（猜錯會套到另一台）
    near2, missing2 = match_ledger_ids(['ZP 3-1-1_M1'], ['ZP 3-1-1_M1x', 'ZP3-1-1_M1', 'ZP 3 1 1_M1'])
    check("近似名稱有多個候選時不猜，歸為 missing", near2 == {} and missing2 == ['ZP 3-1-1_M1'],
          f"{near2} {missing2}")

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__(logging.WARNING)
            self.msgs: list[str] = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    cap, lg = _Cap(), logging.getLogger('validate.points')
    old_level, old_prop = lg.level, lg.propagate
    lg.setLevel(logging.WARNING)
    lg.propagate = False          # 測試輸出不要被這些預期中的警告洗版
    lg.addHandler(cap)
    try:
        warn_unmatched_overrides({k: {'is_standby': True} for k in ledger_ids}, data_ids)
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)
        lg.propagate = old_prop
    check("近似名稱的警告明講「這筆台帳沒有生效」",
          any('CH01_M1' in m and '沒有生效' in m and 'CH 01_M1' in m for m in cap.msgs), str(cap.msgs))
    check("完全找不到的另用一行警告帶出名稱",
          any('ZP 9-9_M1' in m for m in cap.msgs), str(cap.msgs))
    quiet = _Cap()
    lg.setLevel(logging.WARNING)
    lg.propagate = False
    lg.addHandler(quiet)
    try:
        warn_unmatched_overrides({'A_M1': {}}, ['A_M1'])
    finally:
        lg.removeHandler(quiet)
        lg.setLevel(old_level)
        lg.propagate = old_prop
    check("全部對得上時不吵（沒有警告）", quiet.msgs == [], str(quiet.msgs))


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

    # ── 規則設定從資料庫到引擎：停用、參數、嚴重度 ──
    from vibcore.rules.engine import evaluate_all as _eval_all
    with conn.cursor() as cur:
        cur.execute("UPDATE rule_config SET is_active = FALSE WHERE rule_code = 'VEL_HIGH'")
        cur.execute("UPDATE rule_config SET severity = 'warn' WHERE rule_code = 'IMPACT_RISE'")
    conn.commit()
    only_active = repo.get_rule_configs(conn)
    every = repo.get_rule_configs(conn, active_only=False)
    check("active_only=False 才查得到被停用的規則（引擎要看得到 is_active=False 才跳得過）",
          'VEL_HIGH' not in only_active and every['VEL_HIGH']['is_active'] is False)
    _stats = {'vel_rms': (0.664, 0.664, 0.0116, 300), 'vel_oa': (0.664, 0.664, 0.0116, 300),
              'acc_crest_median': (2.6, 2.6, 0.3, 300)}
    _c = ctx([{'vel_rms': v, 'vel_oa': v, 'acc_crest_median': 4.0} for v in (1.33, 1.38, 1.48)],
             _stats, dev=device(iso_machine_group='4', iso_foundation='rigid', rated_power_kw=11.0))
    _out = {o.rule_code: o for o in _eval_all(_c, every, rule_codes=['VEL_HIGH', 'IMPACT_RISE'])}
    check("資料庫停用的 VEL_HIGH 不執行（原本用 active_only 查詢時，停用的規則因查不到設定而照跑）",
          'VEL_HIGH' not in _out)
    check("資料庫把 IMPACT_RISE 設為 warn → 觸發結果就是 warn（升級不必改程式）",
          _out['IMPACT_RISE'].severity == 'warn', str(_out['IMPACT_RISE'].severity))
    with conn.cursor() as cur:
        cur.execute("UPDATE rule_config SET is_active = TRUE WHERE rule_code = 'VEL_HIGH'")
        cur.execute("UPDATE rule_config SET severity = 'observe' WHERE rule_code = 'IMPACT_RISE'")
    conn.commit()

    # ── 正式排程這一條路：process_point 傳給引擎的設定與「現在」 ──
    # 規則設定的傳遞問題只出現在這條路（回測傳的是物件，單元測試直接給參數），
    # 所以必須走一次真的 process_point，用 spy 看它交給 evaluate_all 的是什麼。
    from vibcore.pipeline import daily as daily_mod
    from validate.synthetic import _dev_stable
    from vibcore.io.analytic_reader import load_analytic_dir
    import tempfile as _tf
    syn = _dev_stable(np.random.default_rng(1)).assign(Name='SYN-1')
    day2_raw = syn[pd.to_datetime(syn['Time']).dt.strftime('%Y-%m-%d') == '2026-05-02']
    with _tf.TemporaryDirectory() as _d:
        day2_raw.to_csv(os.path.join(_d, 'syn.csv'), index=False, encoding='utf-8-sig')
        day2 = load_analytic_dir(_d)['SYN-1']
    with conn.cursor() as cur:
        cur.execute("UPDATE rule_config SET is_active = FALSE WHERE rule_code = 'STEP_CHANGE'")
    conn.commit()
    caught: dict = {}
    real_eval = daily_mod.evaluate_all

    def _spy(ctx_, cfgs, *a, **k):
        caught['cfgs'], caught['now'] = cfgs, ctx_.now
        return real_eval(ctx_, cfgs, *a, **k)
    daily_mod.evaluate_all = _spy
    try:
        pr = daily_mod.process_point(conn, {'Name': 'SYN-1', 'Building': 'F7', 'Floor': '6F',
                                            'System': '空調', 'RPM': 1710, 'FMF': 28.5},
                                     day2, dt.date(2026, 5, 2))
    finally:
        daily_mod.evaluate_all = real_eval
    conn.commit()
    check("（準備）process_point 跑到了規則判定", 'cfgs' in caught, str(pr.error))
    check("process_point 把含停用規則的設定交給引擎（STEP_CHANGE 的 is_active 是 False）",
          caught['cfgs'].get('STEP_CHANGE', {}).get('is_active') is False, str(sorted(caught['cfgs'])))
    check("補跑過去的日期：規則的 now 是該日（2026-05-02）的最後一秒，不是真實的現在",
          caught['now'] == dt.datetime(2026, 5, 2, 23, 59, 59, tzinfo=dt.timezone.utc), str(caught['now']))
    with conn.cursor() as cur:
        cur.execute("UPDATE rule_config SET is_active = TRUE WHERE rule_code = 'STEP_CHANGE'")
        cur.execute("SELECT count(*) AS n FROM ingestion_log WHERE point_id = %s", (pr.point_id,))
        n_single = cur.fetchone()['n']
    conn.commit()
    check("單日檔案：匯入紀錄只有 run_date 那一天（日常行為與以前相同）", n_single == 1, str(n_single))

    # ── 一次載入多天的歷史：每個有資料的日期都要有匯入紀錄 ──
    # ingestion_log 的語意是「資料日期」。只記 run_date 的話，第一次上線載入的歷史、或上游補送的
    # 前幾天，週報的匯入稽核會把那幾天判成「排程當天沒有執行」，第一份報告就跳出警示。
    multi_raw = syn[pd.to_datetime(syn['Time']).dt.strftime('%Y-%m-%d').isin(
        ['2026-05-04', '2026-05-05', '2026-05-07'])]          # 05-06 刻意缺：真的沒資料進來
    with _tf.TemporaryDirectory() as _d:
        multi_raw.to_csv(os.path.join(_d, 'syn.csv'), index=False, encoding='utf-8-sig')
        multi = load_analytic_dir(_d)['SYN-1']
    pr2 = daily_mod.process_point(conn, {'Name': 'SYN-2', 'Building': 'F7', 'Floor': '6F',
                                         'System': '空調', 'RPM': 1710, 'FMF': 28.5},
                                  multi, dt.date(2026, 5, 7))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT ingest_date, status, row_count FROM ingestion_log "
                    "WHERE point_id = %s ORDER BY ingest_date", (pr2.point_id,))
        ing = cur.fetchall()
    check("（準備）多天檔案處理成功", pr2.error is None, str(pr2.error))
    check("多天檔案：有資料的三天各有一筆匯入紀錄，沒資料的 05-06 不補",
          [r['ingest_date'] for r in ing] == [dt.date(2026, 5, 4), dt.date(2026, 5, 5), dt.date(2026, 5, 7)],
          str([str(r['ingest_date']) for r in ing]))
    # 設備整天沒運轉：資料完整，不是「檔案不完整」（否則備機每天都被標成匯入問題）
    from vibcore.config import DataStatus as _DS
    def _hours(*statuses):
        return pd.DataFrame({'ts_hour': pd.date_range('2026-05-04', periods=len(statuses), freq='h'),
                             'data_status': list(statuses)})
    check("整天都沒運轉（備機）→ ok，不是 partial",
          daily_mod._ingestion_status(_hours(*[_DS.NOT_RUNNING] * 24)) == 'ok')
    check("有可分析的小時 → ok", daily_mod._ingestion_status(_hours(_DS.OK, _DS.NO_DATA)) == 'ok')
    check("整天都是缺資料 → partial", daily_mod._ingestion_status(_hours(*[_DS.NO_DATA] * 24)) == 'partial')
    check("只有資料不全的小時 → partial（樣本不足，檔案確實有缺）",
          daily_mod._ingestion_status(_hours(_DS.PARTIAL, _DS.PARTIAL)) == 'partial')
    check("沒有任何小時 → partial", daily_mod._ingestion_status(_hours()) == 'partial')

    expect_rows = multi['datetime'].dt.date.value_counts().to_dict()
    check("補記日期的筆數等於該日的資料列數、狀態合法",
          all(r['row_count'] == expect_rows[r['ingest_date']] and r['status'] in ('ok', 'partial')
              for r in ing if r['ingest_date'] != dt.date(2026, 5, 7)), str(ing))

    # ── 補跑歷史日期時，自動結案要用該批資料的時間，不是牆上時鐘 ──
    # 事項的 last_seen_at 是規則判定時的 now（歷史日期）；若拿真實的現在去比，
    # 每件剛建立的事項都會當天被結案，隔天再觸發又重開，首次出現時間被洗掉。
    from vibcore.types import Finding as _Finding
    t_seen = dt.datetime(2026, 5, 25, 12, 0, tzinfo=dt.timezone.utc)
    repo.upsert_device(conn, DeviceContext(device_id='BF', device_name='BF', building='A棟',
                                           floor='1F', system_name='冰水'))
    repo.upsert_finding(conn, _Finding(
        finding_key='point:BF_M1:vel_high', device_id='BF', target_type='point', target='BF_M1',
        issue_type='vel_high', family='oscillating', rule_code='VEL_HIGH', title='補跑測試',
        severity='warn', peak_severity='warn', first_seen_at=t_seen, last_seen_at=t_seen))
    conn.commit()
    n_replay = daily_mod.auto_resolve_quiet(conn, 3, now=daily_mod.rule_now(
        daily_mod.run_window_end(dt.date(2026, 5, 25))))
    check("補跑 5/25：事項才剛出現，不自動結案（原本拿真實的現在比，當天就被結案）",
          n_replay == 0, str(n_replay))
    n_replay = daily_mod.auto_resolve_quiet(conn, 3, now=daily_mod.rule_now(
        daily_mod.run_window_end(dt.date(2026, 5, 28))))
    check("補跑到 5/28（已連續 3 天以上沒再觸發）才自動結案", n_replay == 1, str(n_replay))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, first_seen_at FROM finding WHERE finding_key = %s",
                    ('point:BF_M1:vel_high',))
        fk = cur.fetchone()
    check("結案後狀態為 auto_resolved，首次出現時間仍是 5/25",
          fk['status'] == 'auto_resolved' and fk['first_seen_at'].date() == dt.date(2026, 5, 25),
          str(fk))

    # ── 現場回饋表：從資料庫匯出 Finding 與觀察名單 ──
    from validate.review_sheet import from_db
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO finding (finding_key, device_id, target_type, target, issue_type, family,
                                 rule_code, title, severity, peak_severity,
                                 baseline_value, current_value, value_unit)
            VALUES ('point:P1_M1:sensor_offline', 'P1', 'point', 'P1_M1', 'sensor_offline', 'event',
                    'SENSOR_OFFLINE', '感測器離線', 'err', 'err', 24, 29, 'hr');
            INSERT INTO observation (observation_key, device_id, target_type, target, issue_type, family,
                                     rule_code, title, baseline_value, current_value, value_unit)
            VALUES ('point:P1_M1:vel_high', 'P1', 'point', 'P1_M1', 'vel_high', 'oscillating',
                    'VEL_HIGH', '速度整體值相對基準偏高', 0.556, 1.142, 'mm/s');
        """)
    conn.commit()
    sheet_db = from_db(conn, days=7)
    check("資料庫匯出：Finding 與觀察名單各一列，item_type 標明",
          set(sheet_db['item_type']) == {'finding', 'observation'}
          and set(sheet_db['item_id']) == {'point:P1_M1:sensor_offline', 'point:P1_M1:vel_high'},
          str(sheet_db[['item_type', 'item_id']].values.tolist()))
    vh_row = sheet_db[sheet_db['rule_code'] == 'VEL_HIGH'].iloc[0]
    check("觀察名單列帶 observe 嚴重度與變化幅度（+105%）；判定欄空白",
          vh_row['severity'] == 'observe' and abs(float(vh_row['delta_pct']) - 105.4) < 0.5
          and vh_row['判定'] == '', str(vh_row.to_dict()))
    check("匯出範圍：回看 0 天時，只剩未結案的 Finding（觀察名單超出範圍）",
          set(from_db(conn, days=0)['item_type']) <= {'finding'})
    conn.commit()

    # ── 台帳匯入資料庫（vibcore.ledger_import）──────────────
    from vibcore.io.ledger import read_ledger_csv
    from vibcore.ledger_import import run_import
    from vibcore.metrics.iso import evaluate_iso, resolve_class

    for did in ('L1_M1', 'L1_M2', 'L2_M1', 'CH 01_M1'):
        repo.upsert_device(conn, DeviceContext(      # 模擬每日排程第一次看到這些設備
            device_id=did, device_name=did, building='A棟', floor='1F', system_name='冰水',
            rated_rpm=1750.0, fmf_hz=29.2))
    conn.commit()
    ledger_rows = {
        'L1_M1': {'rated_power_kw': 45.0, 'iso_machine_group': '2.0', 'iso_foundation': '剛性',
                  'iso_driver_type': '外接', 'is_standby': True},
        'L1_M2': {'rated_power_kw': 11.0, 'iso_machine_group': '4', 'iso_foundation': 'rigid'},
        'L2_M1': {'iso_machine_group': '9', 'is_standby': True},          # 不合法 → 略過
        'CH01_M1': {'iso_machine_group': '2', 'iso_foundation': 'rigid'},  # 近似 'CH 01_M1'
        'GHOST_M1': {'is_standby': True},                                  # 完全找不到
    }
    dry = run_import(conn, ledger_rows, dry_run=True)
    conn.commit()
    check("預演（dry-run）不寫入：資料庫裡 L1_M1 仍是未分級",
          repo.get_device(conn, 'L1_M1').iso_machine_group is None
          and repo.get_device(conn, 'L1_M1').iso_class_source == 'unset')
    check("預演也算得出會更新哪幾台", {p.device_id for p in dry.updated} == {'L1_M1', 'L1_M2'},
          str([p.device_id for p in dry.updated]))

    rep = run_import(conn, ledger_rows)
    conn.commit()
    d1 = repo.get_device(conn, 'L1_M1')
    check("匯入後 L1_M1 的群組、剛性、驅動型式、備機都進了資料庫（已正規化）",
          (d1.iso_machine_group, d1.iso_foundation, d1.iso_driver_type, d1.is_standby)
          == ('2', 'rigid', 'external', True), str((d1.iso_machine_group, d1.iso_foundation)))
    check("匯入後 iso_class_source 為 manual_override（沒有這步，分類填了也不生效）",
          d1.iso_class_source == 'manual_override', d1.iso_class_source)
    check("匯入後規則層真的認得這個分類：resolve_class 與 evaluate_iso(applicable)",
          resolve_class(d1) == ('2', 'rigid') and evaluate_iso(
              pd.DataFrame({'ts_hour': [pd.Timestamp('2026-08-01')], 'data_status': ['ok'],
                            'vel_rms': [0.5]}), d1, None).applicable is True)
    d2 = repo.get_device(conn, 'L1_M2')
    check("額定 11 kW 的設備：分類寫進去了，但 ISO 判定因適用範圍而不啟用",
          d2.iso_machine_group == '4' and float(d2.rated_power_kw) == 11.0 and evaluate_iso(
              pd.DataFrame({'ts_hour': [pd.Timestamp('2026-08-01')], 'data_status': ['ok'],
                            'vel_rms': [0.5]}), d2, None).applicable is False)
    check("不合法的列整列略過：L2_M1 的備機旗標沒有被一起寫入",
          repo.get_device(conn, 'L2_M1').is_standby is False
          and [d for d, _ in rep.skipped] == ['L2_M1'], str(rep.skipped))
    check("近似名稱（CH01_M1 ≈ CH 01_M1）只報告、不套用也不建立",
          rep.near_miss == {'CH01_M1': 'CH 01_M1'}
          and repo.get_device(conn, 'CH 01_M1').iso_machine_group is None
          and repo.get_device(conn, 'CH01_M1') is None)
    check("完全找不到的 id 不會憑空建立設備", repo.get_device(conn, 'GHOST_M1') is None
          and rep.missing == ['GHOST_M1'])
    check("覆蓋摘要：L1_M1 會啟用 ISO、L1_M2 是適用範圍外",
          'L1_M1' in rep.cover['iso_ready'] and 'L1_M2' in rep.cover['out_of_scope'])

    again = run_import(conn, ledger_rows)
    conn.commit()
    check("重跑同一份台帳沒有任何更新（冪等）", not again.updated and not again.created,
          str([p.device_id for p in again.updated]))
    # 每日排程之後再 upsert 一次（台帳欄位皆 None）：匯入的值要留著
    repo.upsert_device(conn, DeviceContext(
        device_id='L1_M1', device_name='L1_M1', building='A棟', floor='1F', system_name='冰水',
        rated_rpm=1750.0, fmf_hz=29.2))
    conn.commit()
    d1b = repo.get_device(conn, 'L1_M1')
    check("匯入之後每日排程 upsert 不會把台帳清掉",
          (d1b.iso_machine_group, d1b.iso_foundation, d1b.is_standby, d1b.iso_class_source)
          == ('2', 'rigid', True, 'manual_override'))
    run_import(conn, {'L1_M1': {'iso_machine_group': '', 'iso_foundation': '', 'is_standby': None}})
    conn.commit()
    check("台帳留空不會把資料庫已有的值清掉", repo.get_device(conn, 'L1_M1').iso_machine_group == '2')

    strict = run_import(conn, {'L1_M2': {'is_standby': True}, 'CH01_M1': {'is_standby': True}},
                        strict=True)
    conn.commit()
    check("--strict：有問題（近似名稱）就整批不套用，連合法的那列也不動",
          repo.get_device(conn, 'L1_M2').is_standby is False and strict.n_issues == 1)

    made = run_import(conn, {'NEW_M1': {'iso_machine_group': '2', 'iso_foundation': 'rigid',
                                       'rated_power_kw': 30.0},
                             'CH01_M1': {'is_standby': True}}, create_missing=True)
    conn.commit()
    nd = repo.get_device(conn, 'NEW_M1')
    check("--create-missing：資料庫沒有的設備才建立，且帶著台帳（device_name 暫用 id）",
          nd is not None and nd.iso_class_source == 'manual_override' and nd.device_name == 'NEW_M1')
    check("--create-missing 對近似名稱仍然拒絕（CH01_M1 不會在 'CH 01_M1' 旁多出幽靈設備）",
          repo.get_device(conn, 'CH01_M1') is None and made.near_miss == {'CH01_M1': 'CH 01_M1'})
    # 設備先由台帳建立、之後每日排程補上 metadata：台帳欄位要留著
    repo.upsert_device(conn, DeviceContext(device_id='NEW_M1', device_name='新泵', building='B棟',
                                           floor='2F', system_name='冷卻水', rated_rpm=1180.0))
    conn.commit()
    nd2 = repo.get_device(conn, 'NEW_M1')
    check("先由台帳建立、之後排程補 metadata：兩邊都在（名稱來自排程、分類來自台帳）",
          nd2.device_name == '新泵' and nd2.iso_machine_group == '2' and nd2.building == 'B棟')

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
        from vibcore import check as _chk
        _c6 = _connect_db(env, old_db)
        with _c6.cursor() as _cur:
            before6 = {k: ok for k, _, _, ok in _chk.migration_status(_cur)}
        check("舊的 SCADA 結構 → 自檢指出 006 尚未套用，其餘照常",
              before6['006'] is False and all(v is True for k, v in before6.items() if k != '006'),
              str(before6))
        runs = [_psql("-f", mig6) for _ in range(2)]
        with _c6.cursor() as _cur:
            after6 = {k: ok for k, _, _, ok in _chk.migration_status(_cur)}
        _c6.close()
        check("套用 migration_006 後 → 自檢回報 006 已套用", after6['006'] is True, str(after6))
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
                       ("db/migration_005_drop_kurtosis_channel.sql", "migration_005"),
                       ("db/migration_007_vel_high_fallback_observe.sql", "migration_007")):
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

    # ── migration_007：VEL_HIGH 三個新參數 ──
    def _vh_params() -> str:
        q = subprocess.run(["psql", "-tAq", "-d", dbname, "-c",
                            "set search_path to vib,public; "
                            "select params::text from rule_config where rule_code='VEL_HIGH';"],
                           env=env, capture_output=True, text=True)
        return q.stdout.strip().splitlines()[-1] if q.stdout.strip() else q.stderr[:200]

    seeded = json.loads(_vh_params())
    check("新建庫的 VEL_HIGH seed 就帶著實用顯著性下限與退回嚴重度",
          seeded.get('min_rel_change') == 0.25 and seeded.get('min_abs_change') == 0.10
          and seeded.get('fallback_severity') == 'observe', str(seeded))
    from validate.rule_defaults import DEFAULT_RULE_CONFIGS
    check("schema seed 與回測預設（rule_defaults）的 VEL_HIGH 參數逐鍵一致",
          seeded == DEFAULT_RULE_CONFIGS['VEL_HIGH'].params,
          f"{seeded} vs {DEFAULT_RULE_CONFIGS['VEL_HIGH'].params}")

    mig7 = "db/migration_007_vel_high_fallback_observe.sql"
    if os.path.exists(mig7):
        # 模擬舊庫：只有舊的三個鍵，且現場把 sigma 調成了 3.5
        subprocess.run(["psql", "-q", "-d", dbname, "-c",
                        "set search_path to vib,public; update rule_config set params = "
                        "'{\"threshold_mode\":\"iso\",\"sigma\":3.5,\"consecutive_readings\":3}' "
                        "where rule_code='VEL_HIGH';"], env=env, capture_output=True)
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname, "-f", mig7],
                       env=env, capture_output=True, text=True)
        once = json.loads(_vh_params())
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname, "-f", mig7],
                       env=env, capture_output=True, text=True)
        twice = json.loads(_vh_params())
        check("migration_007 對舊庫補上三個新參數",
              once.get('min_rel_change') == 0.25 and once.get('min_abs_change') == 0.10
              and once.get('fallback_severity') == 'observe', str(once))
        check("現場已調過的值不被覆蓋（sigma 3.5 仍是 3.5）", once.get('sigma') == 3.5, str(once))
        check("重跑 migration_007 結果不變（冪等）", once == twice, f"{once} vs {twice}")
        # 現場把 fallback 改回 warn 之後，重跑 migration 不能把它改回 observe
        subprocess.run(["psql", "-q", "-d", dbname, "-c",
                        "set search_path to vib,public; update rule_config set params = "
                        "params || '{\"fallback_severity\":\"warn\"}' where rule_code='VEL_HIGH';"],
                       env=env, capture_output=True)
        subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname, "-f", mig7],
                       env=env, capture_output=True, text=True)
        check("現場改成 warn 之後重跑 migration_007，不會被改回 observe",
              json.loads(_vh_params()).get('fallback_severity') == 'warn')
    else:
        skip("migration_007 行為", f"找不到 {mig7}")

    _test_migration_status_and_008(dbname, env)
    _test_error_isolation(dbname, env)

    subprocess.run(["dropdb", "--if-exists", dbname], env=env, capture_output=True)


def _connect_db(env: dict, name: str):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    return psycopg2.connect(
        host=env["PGHOST"], port=int(env["PGPORT"]), user=env["PGUSER"],
        password=os.environ.get("VIB_DB_PASSWORD") or None, dbname=name,
        options="-c search_path=vib,public", cursor_factory=RealDictCursor)


def _test_migration_status_and_008(dbname: str, env: dict) -> None:
    """
    舊庫（由舊版 schema.sql 建庫、再逐支套 migration）與全新建庫的種子資料會分岔：缺 TEMP_RISE 一列、
    觀察級規則的嚴重度仍是 warn。正式排程以 rule_config.severity 為上限，停在 warn 的規則會建立
    Finding 派工，等於繞過「只進觀察名單」的政策；缺的那一列則讓該規則的觀察項目因外鍵而寫不進去。
    這一節驗：自檢能指出缺哪支 migration，migration_008 能把舊庫對齊且不蓋掉現場的設定。
    （從最早的 schema 依序套滿 001～008，與全新建庫逐項相同，另以手動對照實驗確認過。）
    """
    from vibcore import check as chk

    print("\n[13b] 舊庫對齊：結構版本自檢與 migration_008")
    mig8 = "db/migration_008_rule_config_seed_alignment.sql"
    if not os.path.exists(mig8):
        skip("migration_008 與結構版本自檢", f"找不到 {mig8}")
        return

    def run_sql(name, *a):
        return subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", name, *a],
                              env=env, capture_output=True, text=True)

    # ── 規範狀態：另建一個全新的庫，不受前面測試對本庫的改動影響 ──
    canon_db = dbname.replace('_test', '') + "_canon_test"
    subprocess.run(["dropdb", "--if-exists", canon_db], env=env, capture_output=True)
    subprocess.run(["createdb", canon_db], env=env, capture_output=True)
    r = run_sql(canon_db, "-f", "db/schema.sql")
    check("（準備）規範庫建得起來", r.returncode == 0, r.stderr[:200])
    cc = _connect_db(env, canon_db)
    with cc.cursor() as cur:
        cur.execute("SELECT rule_code, severity, params, is_active, description FROM rule_config")
        canon = {x['rule_code']: dict(x) for x in cur.fetchall()}
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'vib'")
        canon_tables = {x['tablename'] for x in cur.fetchall()}
        fresh_status = {k: ok for k, _, _, ok in chk.migration_status(cur)}
        fresh_dev = chk.policy_deviations(cur)
        # 找不到規則設定列時要回「無法判斷」，不是「未套用」或「已套用」
        cur.execute("DELETE FROM rule_config WHERE rule_code IN ('IMPACT_RISE', 'VEL_HIGH')")
        gone = {k: ok for k, _, _, ok in chk.migration_status(cur)}
    cc.close()
    subprocess.run(["dropdb", "--if-exists", canon_db], env=env, capture_output=True)

    check("自檢的必要資料表清單與 schema.sql 一致（漏了 observation 時，缺這張表的庫會被誤判為正常）",
          set(chk.EXPECTED_TABLES) == canon_tables,
          str(sorted(canon_tables ^ set(chk.EXPECTED_TABLES))))
    check("自檢的 14 條規則清單與 schema.sql 的種子一致", set(chk.EXPECTED_RULES) == set(canon),
          str(sorted(set(canon) ^ set(chk.EXPECTED_RULES))))
    check("觀察級政策的規則清單 ＝ schema.sql 種子裡嚴重度為 observe 的規則",
          set(chk.OBSERVE_POLICY_RULES) == {c for c, x in canon.items() if x['severity'] == 'observe'})
    check("全新建庫：每一項 migration 探針都回「已套用」",
          fresh_status and all(v is True for v in fresh_status.values()), str(fresh_status))
    check("全新建庫：沒有任何與派工政策不一致的設定",
          fresh_dev == {'observe_but_dispatching': [], 'disabled': [], 'vel_high_fallback': 'observe'},
          str(fresh_dev))
    check("找不到 IMPACT_RISE／VEL_HIGH 設定列時，005／007 回「無法判斷」(None)，不亂報",
          gone['005'] is None and gone['007'] is None, str(gone))

    schema_sql = open("db/schema.sql", encoding="utf-8").read()
    mig_sql = open(mig8, encoding="utf-8").read()
    pat = re.compile(r"INSERT INTO rule_config \(rule_code.*?VALUES\n.*?;\n", re.S)
    seed_a = pat.search(schema_sql).group(0)
    seed_b = pat.search(mig_sql.replace("\nON CONFLICT (rule_code) DO NOTHING;", ";")).group(0)
    check("migration_008 裡的種子與 schema.sql 逐字相同（只多了 ON CONFLICT）", seed_a == seed_b)

    # ── 把本庫弄成「舊庫」的樣子，再套 migration_008 ──
    five = [c for c in chk.OBSERVE_POLICY_RULES if c != 'TEMP_RISE']
    oc = _connect_db(env, dbname)
    with oc.cursor() as cur:
        cur.execute("DELETE FROM observation WHERE rule_code = 'TEMP_RISE'")
        cur.execute("DELETE FROM finding WHERE rule_code = 'TEMP_RISE'")
        cur.execute("DELETE FROM rule_config WHERE rule_code = 'TEMP_RISE'")
        cur.execute("UPDATE rule_config SET severity = 'warn' WHERE rule_code = ANY(%s)", (five,))
        cur.execute("UPDATE rule_config SET params = '{}'::jsonb WHERE rule_code = 'ISO_CLASS_SUSPECT'")
        cur.execute("UPDATE rule_config SET params = params - 'threshold_mode', description = %s "
                    "WHERE rule_code = 'VEL_HIGH'",
                    ('velRMS 相對 ISO 錨定門檻偏高（未分類時退回相對基準 σ 判定）',))
        # 現場的刻意設定：migration_008 不該蓋掉
        cur.execute("UPDATE rule_config SET params = '{\"crest_sigma\":3.1,\"crest_axis_sigma\":2.5}'::jsonb, "
                    "is_active = FALSE WHERE rule_code = 'IMPACT_RISE'")
        cur.execute("UPDATE rule_config SET severity = 'warn', params = params || '{\"hours\":36}'::jsonb "
                    "WHERE rule_code = 'SENSOR_OFFLINE'")
        old_status = {k: ok for k, _, _, ok in chk.migration_status(cur)}
        old_dev = chk.policy_deviations(cur)
    oc.commit()
    check("自檢：缺 TEMP_RISE 這一列 → 只指出 migration_008 尚未套用，其餘都已套用",
          old_status['008'] is False and all(v is True for k, v in old_status.items() if k != '008'),
          str(old_status))
    check("自檢：指出被設成會派工的觀察級規則，並列出停用的規則",
          old_dev['observe_but_dispatching'] == sorted(five) and old_dev['disabled'] == ['IMPACT_RISE'],
          str(old_dev))

    runs = [run_sql(dbname, "-f", mig8) for _ in range(2)]
    check("migration_008 可套用於舊庫，且可重複套用（冪等）", all(x.returncode == 0 for x in runs),
          '；'.join(x.stderr[:200] for x in runs if x.returncode != 0))
    with oc.cursor() as cur:
        cur.execute("SELECT rule_code, severity, params, is_active, description FROM rule_config")
        rows = {x['rule_code']: dict(x) for x in cur.fetchall()}
        after_status = {k: ok for k, _, _, ok in chk.migration_status(cur)}
        after_dev = chk.policy_deviations(cur)
    oc.commit()
    check("補回缺的 TEMP_RISE，內容與規範庫相同",
          'TEMP_RISE' in rows and rows['TEMP_RISE']['severity'] == canon['TEMP_RISE']['severity']
          and rows['TEMP_RISE']['params'] == canon['TEMP_RISE']['params'])
    check("五條觀察級規則的嚴重度從 warn 回到 observe", all(rows[c]['severity'] == 'observe' for c in five),
          str({c: rows[c]['severity'] for c in five}))
    check("現場刻意的設定不被蓋掉：IMPACT_RISE 的自訂門檻與停用狀態、SENSOR_OFFLINE 的 warn 與 36 小時",
          rows['IMPACT_RISE']['params'].get('crest_sigma') == 3.1 and rows['IMPACT_RISE']['is_active'] is False
          and rows['SENSOR_OFFLINE']['severity'] == 'warn' and rows['SENSOR_OFFLINE']['params'].get('hours') == 36,
          str({c: (rows[c]['severity'], rows[c]['params'], rows[c]['is_active'])
               for c in ('IMPACT_RISE', 'SENSOR_OFFLINE')}))
    check("ISO_CLASS_SUSPECT 的空參數補上預設；VEL_HIGH 補上 threshold_mode 與新的說明文字",
          rows['ISO_CLASS_SUSPECT']['params'] == canon['ISO_CLASS_SUSPECT']['params']
          and rows['VEL_HIGH']['params'].get('threshold_mode') == 'iso'
          and rows['VEL_HIGH']['description'] == canon['VEL_HIGH']['description'])
    check("套用後：每一項 migration 探針都回「已套用」，沒有與派工政策不一致的觀察級規則",
          all(v is True for v in after_status.values()) and after_dev['observe_but_dispatching'] == [],
          f"{after_status} {after_dev}")

    # ── 其餘探針：改回舊樣子就要被抓到 ──
    with oc.cursor() as cur:
        cur.execute("UPDATE rule_config SET params = params || '{\"kurt_sigma\":2.5}'::jsonb "
                    "WHERE rule_code = 'IMPACT_RISE'")
        with_kurt = {k: ok for k, _, _, ok in chk.migration_status(cur)}
        cur.execute("UPDATE rule_config SET params = params - 'kurt_sigma' WHERE rule_code = 'IMPACT_RISE'")
        cur.execute("UPDATE rule_config SET params = params - 'min_abs_change' WHERE rule_code = 'VEL_HIGH'")
        no_floor = {k: ok for k, _, _, ok in chk.migration_status(cur)}
    oc.rollback()
    check("IMPACT_RISE 帶回 kurtosis 通道 → 005 回未套用（migration_004 重跑的後果）",
          with_kurt['005'] is False and with_kurt['007'] is True, str(with_kurt))
    check("VEL_HIGH 少了實用顯著性下限 → 007 回未套用", no_floor['007'] is False and no_floor['005'] is True,
          str(no_floor))
    oc.close()


def _test_error_isolation(dbname: str, env: dict) -> None:
    """
    每日排程用同一條連線、同一個交易處理所有量測點。PostgreSQL 在交易內遇到任何 SQL 錯誤
    （外鍵、約束、缺表）後，整個交易進入「只能 rollback」的狀態：後面每一句都被拒絕，
    最後的 commit 也變成 rollback，而且不會拋例外。Python 層級的 try/except 擋不住這件事——
    結果是「單一設備失敗不影響其餘設備」只在 Python 例外時成立，SQL 錯誤會讓當天整批資料安靜地消失。
    這一節用 SAVEPOINT 隔離：一筆寫入失敗只丟掉那一筆，其餘照常 commit，且失敗要被數出來。
    """
    from vibcore.db import repository as repo
    from vibcore.io.analytic_reader import load_analytic_dir
    from vibcore.pipeline import daily as daily_mod
    from vibcore.types import RuleContext, RuleOutcome
    from validate.synthetic import _dev_stable
    import tempfile as _tf

    print("\n[13c] 錯誤隔離：一筆 SQL 失敗不能讓當天整批資料消失")
    conn = _connect_db(env, dbname)
    repo.upsert_device(conn, DeviceContext(device_id='ISO1', device_name='隔離測試', building='A棟',
                                           floor='1F', system_name='冰水'))
    conn.commit()
    dev = repo.get_device(conn, 'ISO1')

    def outcome(code, issue, severity):
        return RuleOutcome(triggered=True, rule_code=code, issue_type=issue, family='monotonic',
                           severity=severity, title=f'{code} 測試', detail='d',
                           interpretation_limit='測試用', target_type='device')

    cx = RuleContext(device=dev, point_id=0, position='M1', agg=pd.DataFrame(), baseline=None,
                     params={}, now=dt.datetime(2026, 6, 2, 23, 59, 59, tzinfo=dt.timezone.utc))
    pr = daily_mod.PointResult(device_id='ISO1', position='M1')
    pr._ctx = cx
    # 第一筆的 rule_code 不存在（外鍵違反），後面兩筆是正常的
    pr._outcomes = [outcome('NO_SUCH_RULE', 'bad_obs', 'observe'),
                    outcome('IMPACT_RISE', 'good_obs', 'observe'),
                    outcome('NO_SUCH_RULE', 'bad_finding', 'warn'),
                    outcome('SENSOR_OFFLINE', 'good_finding', 'err')]
    errors: list = []
    n_up, n_esc, n_obs = daily_mod.persist_findings(conn, [pr], errors=errors)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT issue_type FROM observation WHERE device_id = 'ISO1'")
        obs_rows = [x['issue_type'] for x in cur.fetchall()]
        cur.execute("SELECT issue_type FROM finding WHERE device_id = 'ISO1'")
        fin_rows = [x['issue_type'] for x in cur.fetchall()]
    check("壞的觀察項目之後，正常的觀察項目與事項照常寫入（交易沒有被毒化）",
          obs_rows == ['good_obs'] and fin_rows == ['good_finding'] and (n_up, n_obs) == (1, 1),
          f"obs={obs_rows} finding={fin_rows} n_up={n_up} n_obs={n_obs}")
    check("兩筆失敗都被數出來，帶出是哪一筆（排程摘要與結束碼據此提醒）",
          len(errors) == 2 and any('bad_obs' in e for e in errors) and any('bad_finding' in e for e in errors),
          str(errors))

    # ── 單一量測點處理到一半 SQL 失敗：該點整個退回，其餘量測點照常 ──
    def one_day(name):
        syn = _dev_stable(np.random.default_rng(3)).assign(Name=name)
        raw = syn[pd.to_datetime(syn['Time']).dt.strftime('%Y-%m-%d') == '2026-05-02']
        with _tf.TemporaryDirectory() as d:
            raw.to_csv(os.path.join(d, 'x.csv'), index=False, encoding='utf-8-sig')
            return load_analytic_dir(d)[name]

    meta = lambda n: {'Name': n, 'Building': 'F7', 'Floor': '6F', 'System': '空調', 'RPM': 1710, 'FMF': 28.5}
    real_upsert_daily = repo.upsert_daily

    def exploding_upsert_daily(c, *a, **k):          # 在已寫入設備、量測點與聚合之後才失敗
        with c.cursor() as cur:
            cur.execute("SELECT 1/0")
    repo.upsert_daily = exploding_upsert_daily
    try:
        bad = daily_mod.process_point(conn, meta('ISO-BAD'), one_day('ISO-BAD'), dt.date(2026, 5, 2))
    finally:
        repo.upsert_daily = real_upsert_daily
    good = daily_mod.process_point(conn, meta('ISO-OK'), one_day('ISO-OK'), dt.date(2026, 5, 2))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT device_id FROM device WHERE device_id IN ('ISO-BAD', 'ISO-OK') ORDER BY 1")
        left = [x['device_id'] for x in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM measurement_agg a JOIN measure_point p USING (point_id) "
                    "WHERE p.device_id = 'ISO-OK'")
        n_agg_ok = cur.fetchone()['n']
        cur.execute("SELECT count(*) AS n FROM measurement_agg a JOIN measure_point p USING (point_id) "
                    "WHERE p.device_id = 'ISO-BAD'")
        n_agg_bad = cur.fetchone()['n']
        cur.execute("SELECT i.status FROM ingestion_log i JOIN measure_point p USING (point_id) "
                    "WHERE p.device_id = 'ISO-BAD'")
        bad_ingest = [x['status'] for x in cur.fetchall()]
    res = daily_mod.DailyRunResult(run_date=dt.date(2026, 6, 2), persist_errors=list(errors))
    check("排程摘要把寫入失敗數出來（不能讓摘要看起來一切正常）",
          '寫入失敗 2 筆' in res.summary() and '寫入失敗' not in
          daily_mod.DailyRunResult(run_date=dt.date(2026, 6, 2)).summary(), res.summary())
    check("處理到一半 SQL 失敗的量測點被標為失敗，並帶出原因", bad.error is not None, str(bad.error))
    check("失敗點的資料寫入整段退回（沒有留下半套聚合），但設備與量測點還在，並記一筆「匯入失敗」",
          left == ['ISO-BAD', 'ISO-OK'] and n_agg_bad == 0 and bad_ingest == ['failed'],
          f"devices={left} agg_bad={n_agg_bad} ingest={bad_ingest}")
    check("失敗點之後的量測點照常處理並 commit（聚合都在）", good.ok and n_agg_ok > 0,
          f"{good.error} agg_ok={n_agg_ok}")
    conn.close()


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
        test_vel_high_policy()
        test_rule_config_plumbing()
        test_guardrail()
        test_systemic_gaps()
        test_step_change_features()
        test_scada()
        test_speed_effect()
        test_operating_state()
        test_ledger()
        test_loader_empty_file()
        test_ledger_id_match()
        test_ledger_import_logic()
        test_review_sheet()
        test_db(args.dbname)
    except AssertionError as e:
        print(f"\n❌ 驗收失敗：{e}")
        return 1

    tail = f"，略過 {len(_SKIPPED)} 項" if _SKIPPED else ""
    print(f"\n✅ 全部通過（{len(_PASSED)} 項{tail}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
