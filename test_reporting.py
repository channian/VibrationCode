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
import logging
import os
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

INSERT INTO device (device_id, device_name, building, floor, system_name, status, is_standby) VALUES
  ('AHU-601','外氣空調箱601','A棟','6F','空調','active',false),
  ('AHU-602','外氣空調箱602','A棟','6F','空調','active',false),
  ('CHP-101','冰水泵101','B棟','1F','冰水','active',false),
  ('CHP-102','冰水泵102（備）','B棟','1F','冰水','active',true);

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

    try:
        build_test_db(args.dbname)
    except FileNotFoundError:
        print("找不到 psql / createdb，請確認 PostgreSQL 客戶端已安裝並在 PATH 上。")
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
        test_render(conn)
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
