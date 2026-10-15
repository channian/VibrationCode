"""
test_reporting.py — 報表層（vibcore.reporting）與 send_report 的驗收腳本

在一個**獨立的測試資料庫**上建 schema、塞入涵蓋各種情況的樣本資料，
然後驗證報表收集、排版與 API 的行為。會 `DROP` 並重建指定的資料庫，
所以刻意不吃 `VIB_DB_NAME`——避免有人照著 USAGE.md 設好環境變數之後
直接執行本腳本，把正式資料庫清掉。

執行方式：
    python test_reporting.py                     # 用預設的 vib_reporting_test
    python test_reporting.py --dbname other_db   # 指定測試資料庫名稱
    python test_reporting.py --keep              # 跑完保留資料庫供人工查看

連線參數沿用 `VIB_DB_HOST` / `VIB_DB_PORT` / `VIB_DB_USER` /
`VIB_DB_PASSWORD`（見 vibcore/db/connection.py），只有資料庫名稱是本腳本
自己決定的。

驗證項目：
  1. 範圍篩選（ReportScope）在各區塊一致生效——計數、觀察名單、涵蓋率、
     設備摘要必須指向同一個母集合
  2. 匯入稽核的「全廠當日無匯入」判定**不**隨範圍收窄（那是系統層級的事實）
  3. observe 觀察名單不帶任何簽核語彙（status / 指派 / 期限 / 逾期）
  4. agent 送進來的文字只被轉義一次——樣板 autoescape 生效，且沒有雙重轉義
  5. 日報／週報的版面用語與期間標籤各自正確
  6. send_report 存進 weekly_report 的是完整報告，不是佔位內容
  7. ActionItem 新舊兩種契約都收
"""

import argparse
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_TEST_DB = "vib_reporting_test"

#: 樣本資料刻意覆蓋四種情況：正常運轉、全期斷線、資料不全、備機閒置，
#: 且分屬兩個棟別——範圍篩選若有一處漏掉，比對計數就會對不起來。
SEED_SQL = """
SET search_path TO vib, public;

INSERT INTO device (device_id, device_name, building, floor, system_name, status, is_standby,
                    iso_machine_group, iso_foundation, iso_class_source, rated_power_kw, rated_rpm) VALUES
  ('AHU-601','外氣空調箱601','A棟','6F','空調','active',false, '2','rigid','manual_override',75,1710),
  ('AHU-602','外氣空調箱602','A棟','6F','空調','active',false, '2','rigid','manual_override',75,1710),
  ('CHP-101','冰水泵101','B棟','1F','冰水','active',false, '3','rigid','manual_override',45,1750),
  ('CHP-102','冰水泵102（備）','B棟','1F','冰水','active',true, NULL,NULL,'unset',45,1750);

INSERT INTO measure_point (device_id, position, is_active) VALUES
  ('AHU-601','M1',true), ('AHU-602','M1',true),
  ('CHP-101','DE',true), ('CHP-102','DE',true);

INSERT INTO app_user (ad_username, display_name, email)
VALUES ('wang','王工程師','eng@example.com');

INSERT INTO finding (finding_key, device_id, point_id, target_type, target, issue_type, family,
                     rule_code, title, detail, severity, peak_severity, status,
                     first_seen_at, last_seen_at, stage_entered_at, assigned_to,
                     current_value, baseline_value, value_unit, evidence, interpretation_limit)
VALUES
  ('point:AHU-601/M1:VEL_HIGH', 'AHU-601', 1, 'point','AHU-601/M1','VEL_HIGH','monotonic',
   'VEL_HIGH','速度位準超出 ISO 門檻','velRMS 連續三日高於 Zone C 下緣','err','err','open',
   now() - interval '2 day', now(), now() - interval '2 day', 1,
   7.8, 3.2, 'mm/s', '{"zone":"C"}'::jsonb, '本判定僅代表位準偏高，不含故障類型結論。'),
  ('point:CHP-101/DE:DEGRADE_TREND', 'CHP-101', 3, 'point','CHP-101/DE','DEGRADE_TREND','monotonic',
   'DEGRADE_TREND','趨勢緩慢劣化','近 30 日 velRMS 斜率為正','warn','warn','engineer_replied',
   now() - interval '40 day', now(), now() - interval '9 day', 1,
   4.1, 3.0, 'mm/s', '{"slope":0.03}'::jsonb, '趨勢僅代表變化方向。');

INSERT INTO finding (finding_key, device_id, point_id, target_type, target, issue_type, family,
                     rule_code, title, severity, peak_severity, status,
                     first_seen_at, last_seen_at, resolved_at, resolved_by)
VALUES
  ('point:AHU-602/M1:SENSOR_OFFLINE', 'AHU-602', 2, 'point','AHU-602/M1','SENSOR_OFFLINE','event',
   'SENSOR_OFFLINE','感測器離線','warn','warn','auto_resolved',
   now() - interval '10 day', now() - interval '3 day', now() - interval '2 day', 'system');

INSERT INTO observation (observation_key, device_id, point_id, target_type, target, issue_type,
                         family, rule_code, title, detail, occurrence_count,
                         first_seen_at, last_seen_at, current_value, baseline_value, value_unit,
                         evidence, interpretation_limit)
VALUES
  ('point:AHU-601/M1:IMPACT_RISE','AHU-601',1,'point','AHU-601/M1','IMPACT_RISE','monotonic',
   'IMPACT_RISE','衝擊性指標上升','accKURT 由 3.1 升至 4.6',3,
   now() - interval '3 day', now() - interval '1 day', 4.6, 3.1, '',
   '{"acc_kurt":4.6}'::jsonb,'kurtosis 上升代表波形衝擊性增加，不指向特定故障。'),
  ('point:CHP-101/DE:SPECTRAL_SHIFT','CHP-101',3,'point','CHP-101/DE','SPECTRAL_SHIFT','oscillating',
   'SPECTRAL_SHIFT','頻譜重心往高頻移動','accWeightedMeanFreq 上升 18%',12,
   now() - interval '55 day', now() - interval '2 day', 412.0, 349.0, 'Hz',
   '{"shift_pct":18}'::jsonb,'重心位移不等同於軸承故障。');

INSERT INTO measurement_agg (point_id, ts_hour, data_status, completeness,
                             n_samples_total, n_samples_running, vel_rms)
SELECT 1, generate_series(now() - interval '7 day', now(), interval '1 hour'),
       'ok', 1.0, 360, 360, 3.0;
INSERT INTO measurement_agg (point_id, ts_hour, data_status, completeness,
                             n_samples_total, n_samples_running, vel_rms)
SELECT 2, generate_series(now() - interval '7 day', now(), interval '1 hour'),
       'no_data', 0, 0, 0, NULL;
INSERT INTO measurement_agg (point_id, ts_hour, data_status, completeness,
                             n_samples_total, n_samples_running, vel_rms)
SELECT 3, generate_series(now() - interval '7 day', now(), interval '1 hour'),
       'partial', 0.4, 360, 140, 3.5;
INSERT INTO measurement_agg (point_id, ts_hour, data_status, completeness,
                             n_samples_total, n_samples_running, vel_rms)
SELECT 4, generate_series(now() - interval '7 day', now(), interval '1 hour'),
       'not_running', 0, 360, 0, NULL;
"""

_PASSED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}{('：' + detail) if detail else ''}")
    _PASSED.append(label)
    print(f"  ✓ {label}")


# ──────────────────────────────────────────────────────────
# 測試資料庫建置
# ──────────────────────────────────────────────────────────

def _psql_env() -> dict:
    env = dict(os.environ)
    env.setdefault("PGHOST", os.environ.get("VIB_DB_HOST", "localhost"))
    env.setdefault("PGPORT", os.environ.get("VIB_DB_PORT", "5432"))
    env.setdefault("PGUSER", os.environ.get("VIB_DB_USER", "postgres"))
    if os.environ.get("VIB_DB_PASSWORD"):
        env.setdefault("PGPASSWORD", os.environ["VIB_DB_PASSWORD"])
    return env


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


def build_test_db(dbname: str) -> None:
    env = _psql_env()
    print(f"建立測試資料庫 {dbname} …")
    subprocess.run(["dropdb", "--if-exists", dbname], env=env, check=True,
                   capture_output=True)
    subprocess.run(["createdb", dbname], env=env, check=True, capture_output=True)
    r = subprocess.run(["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", dbname,
                        "-f", "db/schema.sql"],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"schema.sql 載入失敗：\n{r.stderr[:2000]}")


def connect(dbname: str):
    return psycopg2.connect(
        host=os.environ.get("VIB_DB_HOST", "localhost"),
        port=int(os.environ.get("VIB_DB_PORT", 5432)),
        user=os.environ.get("VIB_DB_USER", "postgres"),
        password=os.environ.get("VIB_DB_PASSWORD") or None,
        dbname=dbname,
        options="-c search_path=vib,public",
        # 與正式連線一致（見 vibcore/db/connection.py）；repository 層的
        # 部分查詢用 `conn.cursor()` 取列後直接 `dict(row)`，少了這個會炸
        cursor_factory=RealDictCursor,
    )


# ──────────────────────────────────────────────────────────
# 驗證
# ──────────────────────────────────────────────────────────

def test_scope(conn) -> None:
    from vibcore.reporting import ReportScope, collect_weekly_data

    print("\n[1] 範圍篩選在各區塊一致生效")
    today = dt.date.today()
    start = today - dt.timedelta(days=6)

    allp = collect_weekly_data(conn, start, today)
    check("全廠：1 新 / 1 追蹤 / 1 結案",
          (allp["stats"]["new_count"], allp["stats"]["tracking_count"],
           allp["stats"]["resolved_count"]) == (1, 1, 1), str(allp["stats"]))
    check("全廠：觀察名單 2 項、分佈 2 台",
          allp["observations_by_device"] == {"AHU-601": 1, "CHP-101": 1},
          str(allp["observations_by_device"]))
    check("全廠：4 台設備", allp["device_status_summary"]["total_active_devices"] == 4)

    a = collect_weekly_data(conn, start, today, ReportScope(building="A棟"))
    check("A棟：只剩 A 棟的事項與觀察",
          a["observations_by_device"] == {"AHU-601": 1}
          and a["stats"]["tracking_count"] == 0
          and a["device_status_summary"]["total_active_devices"] == 2,
          str(a["stats"]))
    check("A棟：涵蓋率母集合同步收窄",
          a["coverage"]["total_hours"] == allp["coverage"]["total_hours"] // 2,
          f'{a["coverage"]["total_hours"]} vs {allp["coverage"]["total_hours"]}')
    check("A棟：涵蓋率明細不含 B 棟設備",
          all("CHP" not in g["device_label"] for g in a["coverage_gaps"]))
    check("A棟：匯入稽核明細不含 B 棟設備",
          all("CHP" not in it["device_label"] for it in a["ingestion_audit"]["issues"]))
    check("A棟：『全廠當日無匯入』判定不隨範圍收窄",
          a["ingestion_audit"]["all_missing_dates"]
          == allp["ingestion_audit"]["all_missing_dates"])

    b = collect_weekly_data(conn, start, today,
                            ReportScope(building="B棟", floor="1F", system_name="冰水"))
    check("B棟 1F 冰水：三個條件同時生效",
          b["observations_by_device"] == {"CHP-101": 1}
          and b["stats"]["new_count"] == 0 and b["stats"]["tracking_count"] == 1,
          str(b["stats"]))

    z = collect_weekly_data(conn, start, today, ReportScope(building="不存在"))
    check("範圍查無設備時全部為空，不報錯",
          z["stats"]["new_count"] == 0 and z["coverage"]["total_hours"] == 0
          and z["coverage_gaps"] == [])


def test_coverage_calendar(conn) -> None:
    """涵蓋率以日曆為準：感測器停掉之後根本沒有列的小時，也要算缺資料；窗口超出資料所及之處不算缺。"""
    from vibcore.reporting import collect_weekly_data
    from vibcore.api import service

    print("\n[1b] 涵蓋率：死掉的感測器不能從分母裡消失")
    today = dt.date.today()
    start = today - dt.timedelta(days=6)
    base = collect_weekly_data(conn, start, today)

    with conn.cursor() as cur:
        cur.execute("""
            SET search_path TO vib, public;
            INSERT INTO device (device_id, device_name, building, floor, system_name, status, is_standby)
            VALUES ('DEAD-1', '停掉的泵', 'A棟', '6F', '空調', 'active', false),
                   ('REC-1',  '缺過又恢復的泵', 'A棟', '6F', '空調', 'active', false);
            INSERT INTO measure_point (device_id, position, is_active)
            VALUES ('DEAD-1', 'M1', true), ('REC-1', 'M1', true);
        """)
        cur.execute("SELECT point_id, device_id FROM measure_point WHERE device_id IN ('DEAD-1', 'REC-1')")
        pid = {r["device_id"]: r["point_id"] for r in cur.fetchall()}
        win0 = f"(date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' - interval '6 day')"
        # DEAD-1：窗口前 72 小時正常，之後完全沒有列（不是 no_data 列，是根本沒有）
        cur.execute(f"""
            INSERT INTO measurement_agg (point_id, ts_hour, data_status, completeness, n_samples_total,
                                         n_samples_running, vel_rms)
            SELECT %s, generate_series({win0}, {win0} + interval '71 hour', interval '1 hour'),
                   'ok', 1.0, 360, 360, 2.0
        """, (pid["DEAD-1"],))
        # REC-1：整個窗口都有列，中間有 5 小時 no_data，之後恢復一路到最後
        cur.execute(f"""
            INSERT INTO measurement_agg (point_id, ts_hour, data_status, completeness, n_samples_total,
                                         n_samples_running, vel_rms)
            SELECT %s, g, CASE WHEN g >= {win0} + interval '40 hour' AND g < {win0} + interval '45 hour'
                               THEN 'no_data' ELSE 'ok' END, 1.0, 360, 360, 2.0
            FROM generate_series({win0}, date_trunc('hour', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
                                 interval '1 hour') g
        """, (pid["REC-1"],))
    conn.commit()

    d = collect_weekly_data(conn, start, today)
    gaps = {g["device_label"]: g for g in d["coverage_gaps"]}
    dead = gaps.get("DEAD-1 / M1")
    rec = gaps.get("REC-1 / M1")
    win_start = dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc)
    check("死掉的感測器（之後完全沒有列）出現在資料品質清單，標為還在斷線",
          dead is not None and dead["kind"] == "offline", str(sorted(gaps)))
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(ts_hour) AS t FROM measurement_agg WHERE data_status <> 'no_data'")
        horizon_end = cur.fetchone()["t"] + dt.timedelta(hours=1)
    since = win_start + dt.timedelta(hours=72)
    check("斷線起點是最後一筆資料的下一個小時（窗口起點 + 72 小時），已連續的時數量到全廠資料所及之處"
          "（不是量到窗口結尾）",
          dead is not None and dead["since"] == since
          and dead["gap_hours"] == int((horizon_end - since).total_seconds() // 3600), str(dead))
    added = d["coverage"]["no_data_hours"] - base["coverage"]["no_data_hours"]
    check("涵蓋率的缺資料小時數把這些根本沒有列的小時算進去（只數既有的列會偏高）",
          dead is not None and added >= dead["gap_hours"] + 5, f"新增 {added} 小時")
    check("死掉的點在分母裡佔滿整個窗口（72 小時有資料 ＋ 之後缺的小時），不是只佔 72 小時",
          dead is not None and d["coverage"]["total_hours"] - base["coverage"]["total_hours"]
          >= 72 + dead["gap_hours"],
          f'{d["coverage"]["total_hours"]} vs {base["coverage"]["total_hours"]}')
    check("缺過又恢復的點標為「已恢復」，不是「已連續 N 小時無資料」",
          rec is not None and rec["kind"] == "recovered" and rec["no_data_hours"] >= 5, str(rec))
    check("資料到全廠最新一筆的點不會因為窗口算到隔天零時而被標成斷線",
          "AHU-601 / M1" not in gaps, str(sorted(gaps)))

    api = service.get_weekly_report_data(conn, days=7)
    bad_points = {(p["device_id"]) for p in api["coverage"]["points_with_insufficient_data"]}
    check("API 的涵蓋率摘要用同一套算法：死掉的點被列為資料不足",
          "DEAD-1" in bad_points, str(bad_points))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM device WHERE device_id IN ('DEAD-1', 'REC-1')")
    conn.commit()


def test_outage_group_and_labels(conn) -> None:
    """共因中斷合併卡、沒有 Agent 時不掛 Agent 標籤、空值不顯示。"""
    from vibcore.reporting import collect_weekly_data, render_weekly_html
    from vibcore.reporting.render import _evidence_display, _group_common_cause_offline

    print("\n[1c] 版面：共因中斷合併、無 Agent 的誠實標示、空證據不顯示")

    def f(key, label, t, rule="SENSOR_OFFLINE", hours=200):
        return {"finding_key": key, "rule_code": rule, "severity": "err", "device_label": label,
                "location": "A棟", "title": "感測器逾時無資料", "detail": "d", "interpretation_limit": "x",
                "evidence": {"last_data_at": t, "hours_since_last_data": hours} if t else {},
                "first_seen_at": dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
                "reply_deadline": dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc),
                "escalated": False, "is_overdue": False, "status": "open", "stage_label": "待回覆"}

    items = [f("k1", "A / M1", "2026-09-22 17:00:00+08:00"),
             f("k2", "B / M1", "2026-09-22 16:00:00+08:00"),
             f("kx", "X / M1", "2026-09-09 11:00:00+08:00"),            # 離群：兩週前就停了
             f("kv", "V / M1", None, rule="VEL_HIGH"),                   # 別的規則
             f("k3", "C / M1", "2026-09-22 15:00:00+08:00"),
             f("k4", "D / M1", "2026-09-22 17:00:00+08:00")]
    out = _group_common_cause_offline(items)
    grp = [x for x in out if x.get("_members")]
    check("同一時段停止的 4 個點合併成一張，離群的與別的規則維持單獨一張（6 件 → 3 張）",
          len(out) == 3 and len(grp) == 1 and len(grp[0]["_members"]) == 4
          and {x["finding_key"] for x in out} >= {"kx", "kv"}, str([x["finding_key"] for x in out]))
    check("合併卡放在它涵蓋的第一件的位置，其餘順序不變",
          [x["finding_key"].split(":")[0] for x in out][0] == "group", str([x["finding_key"] for x in out]))
    check("不到 3 個點同時停止時不合併", _group_common_cause_offline(items[:2]) == items[:2])
    check("空值不顯示（None、空字串、空容器、全空的巢狀容器）",
          _evidence_display({"evidence": {"a": None, "b": 1, "c": {"x": None, "y": 2}, "d": [], "e": {"z": None}}})
          == [("b", "1"), ("c", "y 2")])

    # 端到端：資料庫裡有 4 件同時停止的離線事項 ＋ 1 件離群的
    with conn.cursor() as cur:
        cur.execute("SET search_path TO vib, public")
        for i, hhmm in enumerate(["17:00", "17:00", "16:00", "15:00", None]):
            dev = f"OFF-{i}"
            cur.execute("INSERT INTO device (device_id, device_name, building, floor, system_name) "
                        "VALUES (%s, %s, 'C棟', '2F', '冰水')", (dev, dev))
            cur.execute("INSERT INTO measure_point (device_id, position, is_active) VALUES (%s, 'M1', true) "
                        "RETURNING point_id", (dev,))
            pid = cur.fetchone()["point_id"]
            last = f"2026-09-22 {hhmm}:00+08:00" if hhmm else "2026-09-09 11:00:00+08:00"
            cur.execute("""
                INSERT INTO finding (finding_key, device_id, point_id, target_type, target, issue_type, family,
                                     rule_code, title, severity, peak_severity, status, first_seen_at, last_seen_at,
                                     stage_entered_at, evidence, interpretation_limit)
                VALUES (%s, %s, %s, 'point', %s, 'sensor_offline', 'event', 'SENSOR_OFFLINE', '感測器逾時無資料',
                        'err', 'err', 'open', now(), now(), now(), %s::jsonb, '斷線期間不代表設備正常')
            """, (f"point:{dev}_M1:sensor_offline", dev, pid, f"{dev}_M1",
                  json.dumps({"last_data_at": last, "hours_since_last_data": 200 + i, "hours_threshold": 24,
                              "last_data_at_is_lower_bound": False, "note": None})))
    conn.commit()
    today = dt.date.today()
    data = collect_weekly_data(conn, today - dt.timedelta(days=6), today)
    html = render_weekly_html(data, None, "weekly")
    new_n = data["stats"]["new_count"]
    check("端到端：合併卡出現，標題帶點數，且說明資料庫裡仍是個別事項",
          "4 個量測點約在同一時間停止送資料" in html and "仍是 4 件個別事項" in html)
    check("合併卡逐點列出最後資料時間；離群的那一件仍是單獨一張卡",
          "最後資料 09/22 17:00" in html and "OFF-4" in html and html.count("感測器逾時無資料") >= 1)
    check("區塊的件數仍是資料庫的真實件數（不因合併而變少）",
          f'<span class="count mono">{new_n} 件</span>' in html, f"new_count={new_n}")
    check("證據欄沒有「note None」這種空值行（卡片上的證據是給人讀的）", "note None" not in html)

    # 沒有 Agent 時不能掛著 Agent 的標籤
    check("無 Agent 導讀：版面沒有「Agent 撰寫」，頁尾寫明本期未附導讀",
          "Agent 撰寫" not in html and "本期未附 Agent 導讀" in html and "由 Agent 依上述判定結果撰寫" not in html)
    with_agent = render_weekly_html(data, {"verdict": "err", "headline": "測試", "notes": "補充說明"}, "weekly")
    check("有 Agent 導讀時照舊標示",
          "Agent 撰寫" in with_agent and "由 Agent 依上述判定結果撰寫" in with_agent and "補充說明" in with_agent)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM finding WHERE device_id LIKE 'OFF-%'")
        cur.execute("DELETE FROM device WHERE device_id LIKE 'OFF-%'")
    conn.commit()


def test_render(conn) -> None:
    from vibcore.reporting import ReportScope, collect_weekly_data, render_weekly_html

    print("\n[2] 排版：觀察名單、轉義、日報／週報用語")
    today = dt.date.today()
    data = collect_weekly_data(conn, today - dt.timedelta(days=6), today)

    agent = {
        "verdict": "err",
        "headline": "AHU-601 速度位準越過 ISO 門檻。",
        "actions": [{
            "target_type": "point", "target": "AHU-601/M1", "issue_type": "VEL_HIGH",
            "level": "err", "title": "AHU-601 M1 速度位準超標",
            "detail": "velRMS 已連續三日高於 Zone C 下緣。",
            "suggestion": "安排專家系統複測，並確認基座固定狀況。",
        }],
        "notes": "涵蓋率 50%。<script>alert(1)</script> 與 <b>粗體</b> 應顯示為文字。",
    }
    html = render_weekly_html(data, agent, "weekly")

    check("觀察名單自成一區且列出項目",
          "觀察名單" in html and "衝擊性指標上升" in html
          and "頻譜重心往高頻移動" in html)
    check("觀察名單標示本期新增／持續觀察次數",
          "本期新增" in html and "已觀察 12 次" in html)

    items = "".join(html.split('<h2>觀察名單</h2>')[1]
                    .split('<h2>本週已解決</h2>')[0]
                    .split('<div class="obs">')[1:])
    for banned in ("回覆期限", "指派", "目前階段", "逾期", "sev "):
        check(f"觀察名單卡片不含簽核語彙「{banned.strip()}」", banned not in items)

    check("agent 的建議文字併入對應事項卡片",
          "安排專家系統複測，並確認基座固定狀況。" in html)
    check("agent 送入的標記被轉義為文字",
          "<script>alert(1)</script>" not in html
          and "&lt;script&gt;alert(1)&lt;/script&gt;" in html)
    check("沒有雙重轉義", "&amp;lt;" not in html)

    daily = render_weekly_html(collect_weekly_data(conn, today, today), None, "daily")
    check("日報用語正確且不殘留「本週」",
          "設備振動日報" in daily and "本日新發現" in daily and "本週" not in daily)
    check("日報期間標籤為日期而非 ISO 週次",
          daily.split("<title>")[1].split("</title>")[0].endswith(str(today)))
    check("週報期間標籤為 ISO 週次",
          "-W" in html.split("<title>")[1].split("</title>")[0])
    from vibcore.api.service import report_period_label
    check("報告標題的週次與 weekly_report 的期間鍵同一個算法（跨週的滾動窗口也不會有兩個名字）",
          report_period_label("weekly", today) in html.split("<title>")[1].split("</title>")[0],
          html.split("<title>")[1].split("</title>")[0])
    cross = render_weekly_html(collect_weekly_data(conn, dt.date(2026, 9, 24), dt.date(2026, 9, 30)), None, "weekly")
    check("09/24～09/30 跨 W39／W40：標題與資料庫鍵都是 W40",
          "2026-W40" in cross.split("<title>")[1].split("</title>")[0]
          and report_period_label("weekly", dt.date(2026, 9, 30)) == "2026-W40")
    check("非法 report_type 退回週報用語，不丟例外",
          "設備振動週報" in render_weekly_html(data, None, "monthly"))

    scoped = render_weekly_html(
        collect_weekly_data(conn, today - dt.timedelta(days=6), today,
                            ReportScope(building="A棟")), None, "weekly")
    check("有範圍時頁首標示範圍且不含範圍外設備",
          "範圍：A棟" in scoped and "CHP-101" not in scoped)


def test_api(conn) -> None:
    from vibcore.api import service
    from vibcore.api.schemas import ActionItem, SendReportRequest
    import pydantic

    print("\n[3] API：觀察名單交付、send_report 落庫、契約相容")

    d = service.get_weekly_report_data(conn, days=7)
    obs = d["observations"]
    check("get_weekly_report_data 帶出觀察名單",
          obs["new_count"] == 1 and obs["tracking_count"] == 1, str(obs["by_device"]))
    check("觀察名單附上「不要開 action」的指引", "不要為這些項目開 action" in obs["note"])
    for o in obs["new_this_period"] + obs["tracking"]:
        for banned in ("status", "assigned_to", "assignee_name", "reply_deadline"):
            check(f"觀察名單不外洩簽核欄位 {banned}", banned not in o)
    check("get_weekly_report_data 的範圍篩選同步套用到觀察名單",
          service.get_weekly_report_data(conn, days=7, building="A棟")
                 ["observations"]["by_device"] == {"AHU-601": 1})

    req = SendReportRequest(
        report_type="weekly", days=7, verdict="err",
        headline="AHU-601 速度位準越過 ISO 門檻。",
        actions=[{
            "level": "err", "title": "AHU-601 M1 速度位準超標",
            "detail": "velRMS 連續三日高於 Zone C 下緣。",
            "suggestion": "安排專家系統複測。",
            "target_type": "point", "target": "AHU-601/M1", "issue_type": "VEL_HIGH",
        }],
        notes="另有 2 台設備在觀察中。<b>標記測試</b>",
    )
    res = service.send_report(conn, req, daily_limit=3)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT html FROM weekly_report WHERE report_id = %s", (res["report_id"],))
        stored = cur.fetchone()["html"]

    check("落庫的是完整報告而非佔位內容", len(stored) > 5000, f"{len(stored)} bytes")
    check("落庫報告含觀察名單與 agent 建議",
          "觀察名單" in stored and "安排專家系統複測。" in stored
          and "衝擊性指標上升" in stored)
    check("報告 HTML 的標記只轉義一次",
          "&lt;b&gt;標記測試&lt;/b&gt;" in stored and "&amp;lt;" not in stored)
    check("API 回傳與落庫的 agent_payload 維持轉義（卡控 #3）",
          res["notes"].endswith("&lt;b&gt;標記測試&lt;/b&gt;"), res["notes"])

    req2 = SendReportRequest(report_type="daily", days=1, verdict="warn",
                             headline="今日 1 台設備需關注。",
                             actions=[{"level": "warn", "text": "AHU-601 M1：衝擊性上升"}])
    res2 = service.send_report(conn, req2, daily_limit=3)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT html FROM weekly_report WHERE report_id = %s", (res2["report_id"],))
        stored2 = cur.fetchone()["html"]
    check("舊契約 {level, text} 仍可送出", res2["report_id"] is not None)
    check("舊契約的回傳同時帶 title 與 text",
          res2["actions"][0]["title"] == res2["actions"][0]["text"])
    check("日報落庫用日報版面", "設備振動日報" in stored2 and "本週" not in stored2)

    check("新契約可只給 title", ActionItem(level="ok", title="T").display_title == "T")
    check("title 與 text 皆缺時拒收",
          _raises(pydantic.ValidationError, lambda: ActionItem(level="ok")))
    check("收件人欄位仍被擋下（卡控 #1）",
          _raises(pydantic.ValidationError,
                  lambda: SendReportRequest(verdict="ok", headline="x",
                                            to="boss@example.com")))


def test_cli(conn) -> None:
    """不經 Agent 的報告入口：只靠資料庫就能產出完整報告，且不動 Agent 的每日額度。"""
    from vibcore.api import queries, service
    from vibcore.api.schemas import SendReportRequest
    from vibcore.reporting import cli

    print("\n[4] 不經 Agent 產生報告（試跑用）")

    def n_rows(table: str) -> int:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            return cur.fetchone()["n"]

    before_reports, before_audit = n_rows("weekly_report"), n_rows("audit_log")
    rep = cli.build_report(conn, days=7)
    html = rep["html"]
    check("build_report 只讀不寫（weekly_report、audit_log 筆數不變）",
          (n_rows("weekly_report"), n_rows("audit_log")) == (before_reports, before_audit))
    check("沒有 Agent 也產出完整報告：事項、觀察名單、涵蓋率都在",
          "速度位準超出 ISO 門檻" in html and "衝擊性指標上升" in html and "資料涵蓋率" in html
          and len(html) > 5000, f"{len(html)} bytes")
    check("總評由資料庫推算（有未結案 err 事項 → err）", rep["verdict"] == "err", rep["verdict"])
    check("標題列用系統產生的摘要（數字來自資料庫），不是模型文字",
          rep["headline"].startswith("本週新增") and "件事項" in rep["headline"], rep["headline"])
    today = dt.date.today()
    check("期間鍵與 send_report 同一套規則（ISO 週次）",
          rep["period_label"] == service.report_period_label("weekly", today)
          and re.fullmatch(r"\d{4}-W\d{2}", rep["period_label"]) is not None, rep["period_label"])
    check("日報的期間鍵是日期",
          cli.build_report(conn, days=1, report_type="daily")["period_label"] == today.isoformat())

    cli.save_report(conn, rep)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT agent_payload, verdict, length(html) AS n FROM weekly_report "
                    "WHERE report_type = 'weekly' AND period_label = %s", (rep["period_label"],))
        saved = cur.fetchall()
        cur.execute("SELECT actor, action FROM audit_log WHERE action = 'render_report'")
        audit = cur.fetchall()
    check("--save 寫入一筆 weekly_report，來源標為 system",
          len(saved) == 1 and saved[0]["agent_payload"]["source"] == "system"
          and saved[0]["verdict"] == "err" and saved[0]["n"] > 5000, str(saved))
    check("稽核動作是 render_report、操作者 system",
          audit == [{"actor": "system", "action": "render_report"}], str(audit))
    check("系統自己出報告不吃 Agent 的每日寄送額度", queries.count_send_report_today(conn) == 0)

    cli.save_report(conn, cli.build_report(conn, days=7))
    conn.commit()
    check("同一期間重跑是覆蓋，不是多一筆", n_rows("weekly_report") == before_reports + 1)

    res = service.send_report(conn, SendReportRequest(
        report_type="weekly", days=7, verdict="err", headline="Agent 導讀標題。", actions=[]),
        daily_limit=10)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT agent_payload FROM weekly_report WHERE report_id = %s", (res["report_id"],))
        after_agent = cur.fetchone()["agent_payload"]
    check("之後 Agent 送出同一期間：覆蓋系統版，仍是同一筆",
          n_rows("weekly_report") == before_reports + 1 and "source" not in after_agent
          and after_agent["headline"] == "Agent 導讀標題。", str(after_agent))

    import tempfile
    out = os.path.join(tempfile.mkdtemp(), "sub", "w.html")
    env_before = os.environ.get("VIB_DB_NAME")
    os.environ["VIB_DB_NAME"] = conn.get_dsn_parameters()["dbname"]
    try:
        rc = cli.main(["--days", "7", "--out", out])
    finally:
        if env_before is None:
            os.environ.pop("VIB_DB_NAME", None)
        else:
            os.environ["VIB_DB_NAME"] = env_before
    check("命令列入口：寫出 HTML 檔（目錄不存在會建立）",
          rc == 0 and os.path.getsize(out) > 5000)
    check("命令列入口沒帶 --save 時不寫資料庫", n_rows("weekly_report") == before_reports + 1)


def _raises(exc, fn) -> bool:
    try:
        fn()
    except exc:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbname", default=DEFAULT_TEST_DB,
                    help=f"測試資料庫名稱（預設 {DEFAULT_TEST_DB}）；執行時會先 DROP 再重建")
    ap.add_argument("--keep", action="store_true", help="跑完保留資料庫供人工查看")
    args = ap.parse_args()

    refusal = ensure_disposable_dbname(args.dbname)
    if refusal:
        print(refusal)
        return 2

    try:
        build_test_db(args.dbname)
    except FileNotFoundError:
        # 本檔的驗收全部要走資料庫（週報是從 SQL 組出來的），沒有可以
        # 退而求其次只跑一部分的路徑。所以這裡不是「略過」，是真的跑不了
        # ——但要講清楚這是環境缺件，不是程式有問題。
        print("找不到 psql / createdb：這是環境缺件，不是驗收失敗。\n"
              "  本檔的驗收全部需要資料庫（週報內容是從 SQL 組出來的），\n"
              "  沒有用戶端就一項都跑不了。\n"
              "\n"
              "  安裝用戶端即可，不需要在本機跑資料庫伺服器：\n"
              "    Ubuntu/Debian：sudo apt install postgresql-client\n"
              "    macOS：brew install libpq && brew link --force libpq\n"
              "\n"
              "  要接既有的資料庫伺服器，設這幾個環境變數：\n"
              "    VIB_DB_HOST / VIB_DB_PORT / VIB_DB_USER / VIB_DB_PASSWORD\n"
              "  （本檔會建立一個獨立的測試資料庫再刪掉，不會動到既有資料）")
        return 2
    except Exception as e:
        print(f"測試資料庫建置失敗：{e}")
        return 2

    conn = connect(args.dbname)
    with conn.cursor() as cur:
        cur.execute(SEED_SQL)
    conn.commit()

    try:
        test_scope(conn)
        test_coverage_calendar(conn)
        test_outage_group_and_labels(conn)
        test_render(conn)
        test_cli(conn)
        test_api(conn)
    except AssertionError as e:
        print(f"\n❌ 驗收失敗：{e}")
        return 1
    finally:
        conn.close()
        if not args.keep:
            subprocess.run(["dropdb", "--if-exists", args.dbname],
                           env=_psql_env(), capture_output=True)
        else:
            print(f"\n（--keep）測試資料庫 {args.dbname} 已保留。")

    print(f"\n✅ 全部通過（{len(_PASSED)} 項）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
