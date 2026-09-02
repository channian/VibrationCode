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
from vibcore.rules.metric_rules import impact_rise, iso_zone, vel_high
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
    print("\n[1] 衝擊型指標：聚合 max/median 並存，判定用 median")

    # 用真實資料驗證聚合，沒有就退回合成資料（結論相同，只是說服力較低）
    src = 'data/Analytic.csv'
    if os.path.exists(src):
        d = pd.read_csv(src, sep='\t', encoding='utf-8-sig', low_memory=False)
        out = _aggregate_running(d)
        check("聚合同時輸出 max 與 median 兩個通道",
              all(out.get(k) is not None for k in
                  ('acc_kurt', 'acc_kurt_median', 'acc_crest', 'acc_crest_median',
                   'acc_kurt_axis_max', 'acc_kurt_axis_median')))
        check("median 欄位等於逐筆中位數",
              abs(out['acc_kurt_median'] - d['accKURT'].median()) < 1e-9,
              f"{out['acc_kurt_median']} vs {d['accKURT'].median()}")
        check("max 欄位等於逐筆最大值",
              abs(out['acc_kurt'] - d['accKURT'].max()) < 1e-9)
        check(f"實測 max({out['acc_kurt']:.2f}) 遠高於 median({out['acc_kurt_median']:.2f})"
              "——這是移除絕對門檻的依據",
              out['acc_kurt'] > 4 > out['acc_kurt_median'])
    else:
        skip("以真實資料驗證聚合", f"找不到 {src}")

    r = impact_rise(ctx(
        [{'acc_kurt_median': 4.0, 'acc_kurt': 40.0, 'acc_crest_median': 2.6}],
        {'acc_kurt_median': (2.4, 2.4, 0.2, 300), 'acc_crest_median': (2.6, 2.6, 0.3, 300)}))
    check("有 median 基準時優先用 median 通道",
          r.triggered and r.evidence['channels']['kurt']['metric'] == 'acc_kurt_median',
          str(r.evidence['channels']['kurt']))

    r = impact_rise(ctx([{'acc_kurt_median': 4.0, 'acc_kurt': 40.0}],
                        {'acc_kurt': (20.0, 20.0, 2.0, 300)}))
    check("舊基準沒有 median 統計量時退回 max 並標明",
          r.triggered and r.evidence['channels']['kurt']['metric'] == 'acc_kurt',
          str(r.evidence['channels']['kurt']))

    r = impact_rise(ctx([{'acc_kurt_median': 4.6}],
                        {'acc_kurt_median': (4.53, 4.53, 0.5, 300)}))
    check("基線本來就高的機器不因絕對值觸發（ZP 3-5 情境，中位數 4.53）",
          not r.triggered)

    r = impact_rise(ctx([{'acc_kurt_median': 4.0}], {'acc_kurt_median': (2.4, 2.4, 0.2, 300)}))
    for gone in ('kurt_absolute_threshold', 'kurt_absolute_exceeded', 'threshold_mode'):
        check(f"evidence 不再含已移除的 {gone}", gone not in r.evidence)


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
    imp = impact_rise(ctx([{'acc_kurt_median': 4.0}], {'acc_kurt_median': (2.4, 2.4, 0.2, 300)}))
    ev3 = _episode_evidence(imp)
    check("IMPACT_RISE 事件記錄實際採用的欄位（median 或退回 max）",
          ev3['primary_metric'] == 'acc_kurt_median', str(ev3['primary_metric']))
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
        impact_rise(ctx([{'acc_kurt_median': 4.0}], {'acc_kurt_median': (2.4, 2.4, 0.2, 300)})),
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
# 六、資料庫層（需要 PostgreSQL）
# ──────────────────────────────────────────────────────────

def _psql_env() -> dict:
    env = dict(os.environ)
    env.setdefault("PGHOST", os.environ.get("VIB_DB_HOST", "localhost"))
    env.setdefault("PGPORT", os.environ.get("VIB_DB_PORT", "5432"))
    env.setdefault("PGUSER", os.environ.get("VIB_DB_USER", "postgres"))
    if os.environ.get("VIB_DB_PASSWORD"):
        env.setdefault("PGPASSWORD", os.environ["VIB_DB_PASSWORD"])
    return env


def test_db(dbname: str) -> None:
    print("\n[8] 資料庫：台帳欄位保留與 migration 冪等性")
    env = _psql_env()
    try:
        subprocess.run(["dropdb", "--if-exists", dbname], env=env, check=True, capture_output=True)
        subprocess.run(["createdb", dbname], env=env, check=True, capture_output=True)
        r = subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname,
                            "-f", "db/schema.sql"], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[:500])
    except (FileNotFoundError, subprocess.CalledProcessError, RuntimeError) as e:
        skip("台帳欄位保留", f"無法建立測試資料庫（{type(e).__name__}）")
        skip("migration_004 冪等", "同上")
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
    conn.close()

    mig = "db/migration_004_iso10816_3_and_median.sql"
    if not os.path.exists(mig):
        skip("migration_004 冪等", f"找不到 {mig}")
        return
    outs = [subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname, "-f", mig],
                           env=env, capture_output=True, text=True) for _ in range(2)]
    check("migration_004 可重複套用於新建庫（冪等）",
          all(o.returncode == 0 for o in outs),
          '；'.join(o.stderr[:200] for o in outs if o.returncode != 0))

    subprocess.run(["dropdb", "--if-exists", dbname], env=env, capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbname", default=DEFAULT_TEST_DB,
                    help=f"測試資料庫名稱（預設 {DEFAULT_TEST_DB}）；執行時會先 DROP 再重建")
    args = ap.parse_args()

    try:
        test_impact()
        test_iso()
        test_persistence()
        test_baseline_maintenance()
        test_axis_direction()
        test_backtest_instrumentation()
        test_guardrail()
        test_db(args.dbname)
    except AssertionError as e:
        print(f"\n❌ 驗收失敗：{e}")
        return 1

    tail = f"，略過 {len(_SKIPPED)} 項" if _SKIPPED else ""
    print(f"\n✅ 全部通過（{len(_PASSED)} 項{tail}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
