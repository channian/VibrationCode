"""
collect.py — 從資料庫收集週報所需的全部資料

**三段式分段在這裡決定，不是 agent 決定**（PLAN §七工作流第 ⑤ 步）：

  本週新發現   first_seen_at 落在本期內（且未結案）
  追蹤中       first_seen_at 早於本期（且未結案）
  本週已解決   status 已結案且 resolved_at 落在本期範圍內 —— 完全從 DB 撈

理由：如果讓 agent 自己判斷「這是新的還是延續的」，牽涉到的其實是
`finding.occurrence_count` 與 `status` 這兩個只有資料庫交易看得到全貌的欄位
（見 `repository.upsert_finding` 對 race condition 的鎖列處理）。agent 每次
呼叫都是無狀態的單次判斷，讓它自己分類，短則跟 DB 的真實狀態打架，長則會
出現「同一件事因為 agent 每週都覺得是新的而重複出現在『新發現』」——那樣
幾週後這份週報就沒人想看了。分類邏輯只能有一份，且必須是資料庫交易看得到
的那份。

「已解決」完全不接受 agent 輸入，是同一個理由的極端版本：結案是需要
簽核權限的動作（`transition_status` / `auto_resolve`），不是 agent 用一句
話就能宣告的事。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg2.extensions
from psycopg2.extras import RealDictCursor

from vibcore.db.repository import find_missing_ingestion, get_ingestion_audit

logger = logging.getLogger(__name__)

Connection = psycopg2.extensions.connection


@dataclass(frozen=True)
class ReportScope:
    """
    週報的範圍篩選（棟別／樓層／系統別），三個欄位皆為 `None` 代表全廠。

    存在的理由是 `send_report` 的 `new_count`/`tracking_count`/
    `resolved_count` 早就支援這三個篩選欄位。若收集層不跟著篩，同一筆
    `weekly_report` 會出現「計數只算 B 棟、HTML 內文卻列出全廠事項」的
    矛盾——那種不一致沒有任何錯誤訊息會提醒讀者，只會讓人以為某一邊算錯。
    要嘛兩邊都篩，要嘛兩邊都不篩，不能只有一邊。

    **匯入稽核（`ingestion_audit`）刻意不套用本篩選**，見
    `_fetch_ingestion_audit` 的說明：那是排程層級的系統面問題，
    「B 棟的排程沒跑」不是一個成立的概念。
    """

    building: str | None = None
    floor: str | None = None
    system_name: str | None = None

    @property
    def is_all(self) -> bool:
        return self.building is None and self.floor is None and self.system_name is None

    @property
    def params(self) -> dict[str, Any]:
        """供 SQL 具名參數展開；鍵名加 `scope_` 前綴避免與既有參數撞名。"""
        return {
            "scope_building": self.building,
            "scope_floor": self.floor,
            "scope_system": self.system_name,
        }

    def label(self) -> str:
        """人可讀的範圍描述，供頁首標示這份報告涵蓋的範圍；全廠回傳空字串。"""
        parts = [p for p in (self.building, self.floor, self.system_name) if p]
        return " · ".join(parts)


#: 三個篩選條件的 SQL 片段。用 `%(x)s::text IS NULL OR ...` 而非在 Python
#: 端動態拼 WHERE，是為了讓 SQL 字串保持固定——參數化的值永遠走 psycopg2
#: 的轉義路徑，不會因為某天有人把使用者輸入接進 `building` 而變成注入點。
#: 需要 `::text` 顯式轉型，否則 PostgreSQL 無法推斷未知型別參數的型別。
_SCOPE_SQL = """
          AND (%(scope_building)s::text IS NULL OR {a}.building = %(scope_building)s)
          AND (%(scope_floor)s::text IS NULL OR {a}.floor = %(scope_floor)s)
          AND (%(scope_system)s::text IS NULL OR {a}.system_name = %(scope_system)s)
"""


def _scope_sql(alias: str) -> str:
    """產生指定資料表別名的範圍篩選 SQL 片段。"""
    return _SCOPE_SQL.format(a=alias)

#: 視為已結案的狀態（與 vibcore.types.CLOSED_STATUSES 相同，這裡獨立列出
#: 避免 reporting 模組對 vibcore.types 之外的內部模組產生額外相依）
_CLOSED_STATUSES = ("closed", "auto_resolved", "false_positive")

#: 簽核階段 → 中文顯示名稱，用於「追蹤中」事項的「目前階段」欄位
STAGE_LABELS: dict[str, str] = {
    "open": "待設備工程師回覆",
    "engineer_replied": "待主管審核",
    "supervisor_reviewed": "待專家複審",
    "expert_reviewed": "待結案",
}

#: 結案狀態 → 中文顯示名稱，用於「本週已解決」的標籤
CLOSED_LABELS: dict[str, str] = {
    "closed": "已結案",
    "auto_resolved": "自動結案",
    "false_positive": "誤報",
}

#: STANDBY_NO_RUNTIME 規則的預設門檻天數；僅在 rule_config 查無設定時使用
_DEFAULT_STANDBY_DAYS = 30

#: 涵蓋率報告只列出「有問題」的量測點，以下門檻用來排除雜訊
#: （例如只斷線 1 小時、幾乎不影響判定的量測點，沒必要塞滿清單）
_GAP_NO_DATA_MIN_HOURS = 1
_GAP_PARTIAL_MIN_HOURS = 1
_GAP_LIST_MAX_ITEMS = 20

#: 匯入稽核清單只列出真正需要人看的項目，同理設一個上限避免塞爆週報
_INGEST_ISSUE_MAX_ITEMS = 30

#: ingestion_log.status 中「排程確實跑過，但結果不理想」的三種狀態；
#: 與「完全沒有紀錄」（find_missing_ingestion）合起來才是完整的系統面問題清單
_INGEST_PROBLEM_STATUSES = ("failed", "partial", "no_file")


def _as_aware(ts: Any) -> datetime:
    """把時間值正規化成帶時區的 datetime，供與查詢邊界比較。

    `finding.first_seen_at` 是 TIMESTAMPTZ，正常情況下讀回即帶時區；
    但測試夾具或匯入資料偶爾會給 naive 值，直接比較會拋
    `can't compare offset-naive and offset-aware datetimes`，讓整份
    週報產不出來。這裡一律補上 UTC 而非讓它炸掉。
    """
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _period_bounds(period_start: date, period_end: date) -> tuple[datetime, datetime]:
    """
    把「起訖日期（含）」轉成資料庫查詢用的 [start, end) 半開區間。

    DB 存 UTC（見 db/schema.sql 開頭註記），這裡刻意不做 +8 轉換——
    轉換屬於「呈現層」的事，查詢邊界仍以 UTC 日界為準，避免跨時區的
    半天誤差混進統計。呈現層的日期/時間顯示才轉 +8（見 render.py）。
    """
    start = datetime(period_start.year, period_start.month, period_start.day, tzinfo=timezone.utc)
    end = datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


def _device_label(row: dict) -> str:
    """組出如 `AHU-601 / M1` 的裝置＋量測點標籤；無量測點（device/global 層級）時只顯示裝置。"""
    target_type = row.get("target_type")
    if target_type == "global":
        return row.get("target") or "全廠"
    device_id = row.get("device_id") or row.get("target") or "-"
    position = row.get("position")
    return f"{device_id} / {position}" if position else device_id


def _location_label(row: dict) -> str:
    parts = [row.get("building"), row.get("floor"), row.get("system_name")]
    return " · ".join(p for p in parts if p)


def _normalize_open_finding(row: dict, assignee_names: dict[int, str]) -> dict[str, Any]:
    """
    把 `v_open_finding` 的一列轉成 render.py 消費的正規化結構。

    保留原始欄位名稱不動（severity/status/...），只新增衍生欄位，讓
    render.py 不需要重新理解 DB schema 的細節。
    """
    stage_entered_at = row.get("stage_entered_at")
    sla_days = row.get("sla_days")
    reply_deadline = None
    if stage_entered_at is not None and sla_days is not None:
        reply_deadline = stage_entered_at + timedelta(days=int(sla_days))

    days_in_stage = row.get("days_in_stage")
    overdue_days = None
    if row.get("is_sla_breached") and days_in_stage is not None and sla_days is not None:
        overdue_days = max(0, int(days_in_stage) - int(sla_days))

    assigned_to = row.get("assigned_to")

    return {
        "finding_key": row["finding_key"],
        "severity": row["severity"],
        "device_label": _device_label(row),
        "location": _location_label(row),
        "title": row["title"],
        "detail": row.get("detail") or "",
        "interpretation_limit": row.get("interpretation_limit") or "",
        "evidence": row.get("evidence") or {},
        "current_value": row.get("current_value"),
        "baseline_value": row.get("baseline_value"),
        "value_unit": row.get("value_unit") or "",
        "occurrence_count": row.get("occurrence_count", 1),
        "first_seen_at": row.get("first_seen_at"),
        "last_seen_at": row.get("last_seen_at"),
        "assignee_name": assignee_names.get(assigned_to) if assigned_to else None,
        "reply_deadline": reply_deadline,
        "escalated": row.get("escalated_at") is not None,
        "is_overdue": bool(row.get("is_sla_breached")),
        "overdue_days": overdue_days,
        "days_open": row.get("days_open"),
        "days_in_stage": days_in_stage,
        "sla_days": sla_days,
        "status": row["status"],
        "stage_label": STAGE_LABELS.get(row["status"], row["status"]),
        "latest_note": row.get("latest_note"),
    }


def _normalize_resolved_finding(row: dict) -> dict[str, Any]:
    return {
        "finding_key": row["finding_key"],
        "severity": row["severity"],
        "device_label": _device_label(row),
        "location": _location_label(row),
        "title": row["title"],
        "detail": row.get("detail") or "",
        "evidence": row.get("evidence") or {},
        "current_value": row.get("current_value"),
        "baseline_value": row.get("baseline_value"),
        "value_unit": row.get("value_unit") or "",
        "occurrence_count": row.get("occurrence_count", 1),
        "first_seen_at": row.get("first_seen_at"),
        "resolved_at": row.get("resolved_at"),
        "status": row["status"],
        "status_label": CLOSED_LABELS.get(row["status"], row["status"]),
        "latest_note": row.get("latest_note"),
    }


def _fetch_open_findings(conn: Connection, scope: ReportScope) -> list[dict]:
    """
    查 `v_open_finding`：未結案事項 + SLA 逾期判定 + 最新人工回覆，全部
    交給 SQL 檢視計算（見 repository.get_open_findings 的設計說明）——
    週報、Dashboard、API 三處若各自重算 SLA 邏輯，遲早會兜不起來。

    範圍篩選直接套在檢視上：`v_open_finding` 已經把 `building`/`floor`/
    `system_name` 展開成欄位（見 db/schema.sql），不必再 join 一次 device。
    """
    sql = """
        SELECT * FROM v_open_finding v
        WHERE TRUE
    """ + _scope_sql("v") + """
        ORDER BY v.peak_severity DESC, v.stage_entered_at ASC
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, scope.params)
        return [dict(r) for r in cur.fetchall()]


def _fetch_assignee_names(conn: Connection, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT user_id, display_name FROM app_user WHERE user_id = ANY(%s)",
            (list(user_ids),),
        )
        return {r["user_id"]: r["display_name"] for r in cur.fetchall()}


def _fetch_resolved_findings(
    conn: Connection, start: datetime, end: datetime, scope: ReportScope
) -> list[dict]:
    """
    本週轉為結案（closed / auto_resolved / false_positive）的事項。

    刻意獨立於 `v_open_finding` 之外查詢（該檢視只涵蓋未結案列）；
    `latest_note` 沿用同樣「只取人工回覆」的規則，好讓「自動結案且無人
    介入」與「工程師處置後結案」在週報上一望即知是哪一種。
    """
    sql = """
        SELECT
            f.*,
            d.device_name, d.building, d.floor, d.system_name, d.is_standby,
            mp.position,
            (SELECT jsonb_build_object(
                        'author', u.display_name, 'role', n.author_role,
                        'note', n.note, 'created_at', n.created_at)
               FROM finding_note n
               LEFT JOIN app_user u ON u.user_id = n.author_id
              WHERE n.finding_id = f.finding_id AND n.is_human
              ORDER BY n.created_at DESC LIMIT 1) AS latest_note
        FROM finding f
        LEFT JOIN device d ON d.device_id = f.device_id
        LEFT JOIN measure_point mp ON mp.point_id = f.point_id
        WHERE f.status IN %(closed)s
          AND f.resolved_at >= %(start)s AND f.resolved_at < %(end)s
    """ + _scope_sql("d") + """
        ORDER BY f.resolved_at DESC
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql,
            {"closed": _CLOSED_STATUSES, "start": start, "end": end, **scope.params},
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_coverage(
    conn: Connection, start: datetime, end: datetime, scope: ReportScope
) -> dict[str, Any]:
    """
    全廠資料涵蓋率：本期每小時聚合列依 `data_status` 分組計數。

    四種狀態刻意分開回傳，不合併成單一「涵蓋率」數字——`no_data`（斷線）
    是設備異常需要現場處理，`not_running`（未運轉）是正常狀態，兩者混在
    一起會讓「涵蓋率偏低」這件事失去指向性（見 PLAN §需求：斷線與未運轉
    是完全不同的事）。
    """
    sql = """
        SELECT ma.data_status, COUNT(*) AS hours
        FROM measurement_agg ma
        JOIN measure_point mp ON mp.point_id = ma.point_id
        JOIN device d ON d.device_id = mp.device_id
        WHERE ma.ts_hour >= %(start)s AND ma.ts_hour < %(end)s
          AND d.status = 'active' AND mp.is_active
    """ + _scope_sql("d") + """
        GROUP BY ma.data_status
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"start": start, "end": end, **scope.params})
        by_status = {r["data_status"]: int(r["hours"]) for r in cur.fetchall()}

    ok_hours = by_status.get("ok", 0)
    partial_hours = by_status.get("partial", 0)
    no_data_hours = by_status.get("no_data", 0)
    not_running_hours = by_status.get("not_running", 0)
    total_hours = ok_hours + partial_hours + no_data_hours + not_running_hours

    def _ratio(n: int) -> float:
        return (n / total_hours) if total_hours else 0.0

    return {
        "total_hours": total_hours,
        "ok_hours": ok_hours,
        "partial_hours": partial_hours,
        "no_data_hours": no_data_hours,
        "not_running_hours": not_running_hours,
        "ok_ratio": _ratio(ok_hours),
        "partial_ratio": _ratio(partial_hours),
        "no_data_ratio": _ratio(no_data_hours),
        "not_running_ratio": _ratio(not_running_hours),
        # 可分析比例（供 CoverageInfo.is_sufficient 語意對齊）：只計真正
        # 判定得出結論的 ok 小時，門檻判定要嚴格就該用這個。
        "analyzable_ratio": _ratio(ok_hours),
        # 頁首「資料涵蓋率」用的是較寬鬆的定義：扣掉「資料有問題」的兩類
        # （no_data / partial），未運轉不算資料品質的錯，不該拖累這個數字。
        "header_ratio": _ratio(ok_hours + not_running_hours),
    }


def _fetch_coverage_gaps(
    conn: Connection, start: datetime, end: datetime, period_end: date, scope: ReportScope
) -> list[dict[str, Any]]:
    """
    逐量測點列出本期「涵蓋率有問題」的明細，供資料品質區塊的條列說明。

    只列出真正需要人看的項目（斷線、資料不全、備機閒置超標），
    正常運轉的量測點不出現在清單裡。
    """
    sql = """
        SELECT
            mp.point_id, mp.position, d.device_id, d.device_name,
            d.building, d.floor, d.system_name, d.is_standby,
            COUNT(*) FILTER (WHERE ma.data_status = 'no_data')      AS no_data_hours,
            COUNT(*) FILTER (WHERE ma.data_status = 'partial')      AS partial_hours,
            COUNT(*) FILTER (WHERE ma.data_status = 'ok')           AS ok_hours,
            MAX(ma.ts_hour) FILTER (WHERE ma.data_status IN ('ok','partial')) AS last_data_at
        FROM measurement_agg ma
        JOIN measure_point mp ON mp.point_id = ma.point_id
        JOIN device d ON d.device_id = mp.device_id
        WHERE ma.ts_hour >= %(start)s AND ma.ts_hour < %(end)s
          AND d.status = 'active' AND mp.is_active
    """ + _scope_sql("d") + """
        GROUP BY mp.point_id, mp.position, d.device_id, d.device_name,
                 d.building, d.floor, d.system_name, d.is_standby
        ORDER BY no_data_hours DESC, partial_hours DESC
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"start": start, "end": end, **scope.params})
        rows = [dict(r) for r in cur.fetchall()]

    period_end_dt = datetime(period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc) + timedelta(days=1)

    gaps: list[dict[str, Any]] = []
    for row in rows:
        label = f'{row["device_id"]} / {row["position"]}' if row.get("position") else row["device_id"]
        location = _location_label(row)

        if row["no_data_hours"] >= _GAP_NO_DATA_MIN_HOURS:
            last_data_at = row.get("last_data_at")
            gap_since = last_data_at if last_data_at is not None else start
            gap_hours = max(0.0, (period_end_dt - gap_since).total_seconds() / 3600.0)
            gaps.append({
                "kind": "offline",
                "device_label": label,
                "location": location,
                "since": gap_since,
                "gap_hours": gap_hours,
                "no_data_hours": row["no_data_hours"],
            })

        if row["partial_hours"] >= _GAP_PARTIAL_MIN_HOURS:
            gaps.append({
                "kind": "partial",
                "device_label": label,
                "location": location,
                "partial_hours": row["partial_hours"],
            })

    # 備機長期未運轉：獨立查一次「最後一次真正運轉」的時間，
    # 因為這需要看回本期以外的歷史，不能只看本期的 ok_hours。
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT mp.point_id, mp.position, d.device_id, d.building, d.floor, d.system_name,
                   MAX(ma.ts_hour) FILTER (WHERE ma.data_status = 'ok') AS last_run_at
            FROM measure_point mp
            JOIN device d ON d.device_id = mp.device_id
            LEFT JOIN measurement_agg ma ON ma.point_id = mp.point_id AND ma.ts_hour < %(end)s
            WHERE d.status = 'active' AND mp.is_active AND d.is_standby
            """ + _scope_sql("d") + """
            GROUP BY mp.point_id, mp.position, d.device_id, d.building, d.floor, d.system_name
            """,
            {"end": end, **scope.params},
        )
        standby_rows = [dict(r) for r in cur.fetchall()]

    this_week_run_hours = {
        row["point_id"]: row["ok_hours"] for row in rows
    }
    standby_days = _get_standby_threshold_days(conn)
    for row in standby_rows:
        last_run_at = row.get("last_run_at")
        idle_days = (period_end_dt - last_run_at).days if last_run_at else None
        if idle_days is not None and idle_days < standby_days:
            continue
        label = f'{row["device_id"]} / {row["position"]}' if row.get("position") else row["device_id"]
        gaps.append({
            "kind": "standby",
            "device_label": label,
            "location": _location_label(row),
            "idle_days": idle_days,
            "this_week_hours": round(this_week_run_hours.get(row["point_id"], 0), 1),
            "threshold_days": standby_days,
        })

    return gaps[:_GAP_LIST_MAX_ITEMS]


def _get_standby_threshold_days(conn: Connection) -> int:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT params FROM rule_config WHERE rule_code = 'STANDBY_NO_RUNTIME' AND is_active"
        )
        row = cur.fetchone()
    if row and row.get("params") and "days" in row["params"]:
        return int(row["params"]["days"])
    return _DEFAULT_STANDBY_DAYS


def _in_scope(row: dict, scope: ReportScope) -> bool:
    """
    位置欄位是否落在範圍內；用於已經查回記憶體、不值得為了篩選再跑一次
    SQL 的小結果集（目前只有匯入稽核的問題清單）。
    """
    if scope.building is not None and row.get("building") != scope.building:
        return False
    if scope.floor is not None and row.get("floor") != scope.floor:
        return False
    if scope.system_name is not None and row.get("system_name") != scope.system_name:
        return False
    return True


def _fetch_ingestion_audit(
    conn: Connection, period_start: date, period_end: date, scope: ReportScope
) -> dict[str, Any]:
    """
    收集本期的匯入稽核資訊，供週報把「感測器斷線」與「匯入排程沒跑」分開呈現。

    **範圍篩選在這裡只套用到逐點的問題清單（`issues`），不套用到「當日全廠
    是否都沒有匯入紀錄」的判定。** 兩者問的不是同一件事：後者是「排程那天
    到底有沒有跑」，那是一個系統層級的事實，母集合必須是全廠所有量測點——
    若只看 A 棟的 20 個點都沒紀錄就宣告「全廠皆無匯入」，那句話會在其他棟
    其實正常匯入的情況下變成假警報，而它的用詞（「當日所有設備的判定不具
    參考價值」）重到不能出錯。反過來，`issues` 是逐台設備列出來、和涵蓋率
    清單並排呈現的明細，一份標了「範圍：A 棟」的報告卻在裡面列出 B 棟的
    設備，讀者只會以為範圍篩選壞了。

    刻意獨立於 `_fetch_coverage_gaps` 之外，兩者互不交叉比對——原因見
    `repository.find_missing_ingestion` 的設計說明：只要某量測點某天
    `ingestion_log` 完全沒有紀錄，不論 `measurement_agg` 那天是否存在
    `no_data` 列，都直接算系統面問題；反過來，`measurement_agg` 顯示
    `no_data` 但 `ingestion_log` 確實有紀錄（不論好壞），代表排程當天
    確實處理過這個量測點，那是設備面問題，交給 `_fetch_coverage_gaps`
    的既有邏輯呈現，這裡不重複判定。兩條路徑天生正交，不需要互相比對
    就能正確分流，也正因如此才能不遺漏「連一列聚合資料都不存在」的
    整日缺漏（見 aggregate.py `_fill_gap_hours` 的已知限制）。
    """
    missing = find_missing_ingestion(conn, period_start, period_end)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM measure_point WHERE is_active")
        total_active_points = int(cur.fetchone()["n"])

    # 逐日統計缺漏量測點數，找出「全廠都無匯入紀錄」的日子——那種日子
    # 全廠的判定都不可信，不能只是清單裡多幾行字，必須在週報上特別標示。
    missing_by_date: dict[date, int] = {}
    for m in missing:
        missing_by_date[m["date"]] = missing_by_date.get(m["date"], 0) + 1

    missing_dates: list[dict[str, Any]] = []
    all_missing_dates: list[date] = []
    for d in sorted(missing_by_date):
        cnt = missing_by_date[d]
        all_missing = total_active_points > 0 and cnt >= total_active_points
        missing_dates.append({
            "date": d, "missing_count": cnt,
            "total_points": total_active_points, "all_missing": all_missing,
        })
        if all_missing:
            all_missing_dates.append(d)

    # 逐點清單套範圍篩選；上面的 missing_by_date / all_missing_dates 刻意
    # 用未篩選的 `missing`，理由見本函式 docstring。
    issues: list[dict[str, Any]] = [
        {
            "date": m["date"],
            "kind": "no_import",
            "device_label": f'{m["device_id"]} / {m["position"]}' if m.get("position") else m["device_id"],
            "location": _location_label(m),
            "note": "",
        }
        for m in missing if _in_scope(m, scope)
    ]

    audit_df = get_ingestion_audit(conn, period_start, period_end)
    has_logged_problem = False
    if not audit_df.empty:
        problem_mask = audit_df["status"].isin(_INGEST_PROBLEM_STATUSES)
        has_logged_problem = bool(problem_mask.any())
        for row in audit_df[problem_mask].to_dict("records"):
            if not _in_scope(row, scope):
                continue
            label = f'{row["device_id"]} / {row["position"]}' if row.get("position") else row["device_id"]
            issues.append({
                "date": row["ingest_date"],
                "kind": row["status"],
                "device_label": label,
                "location": _location_label(row),
                "note": row.get("note") or "",
            })

    issues.sort(key=lambda it: (it["date"], it["device_label"]))

    return {
        "has_missing": bool(missing) or has_logged_problem,
        "total_missing": len(missing),
        "issues": issues[:_INGEST_ISSUE_MAX_ITEMS],
        "missing_dates": missing_dates,
        "all_missing_dates": all_missing_dates,
    }


def _normalize_observation(row: dict) -> dict[str, Any]:
    """
    把 `observation` 的一列轉成週報消費用的正規化結構。

    刻意不沿用 `_normalize_open_finding` 那一套欄位（status/assignee/
    reply_deadline/is_overdue…）——observation 沒有簽核狀態，硬套上去
    只會讓它在版面上看起來也需要有人回覆、逾期。這裡只留「這是什麼、
    數值多少、已經持續多久」，呈現成觀察素材而非待辦事項的取捨要從
    資料結構這一層就做出來，不能指望版面另外遮起來。
    """
    return {
        "observation_key": row["observation_key"],
        "rule_code": row.get("rule_code"),
        "device_label": _device_label(row),
        "location": _location_label(row),
        "title": row["title"],
        "detail": row.get("detail") or "",
        "interpretation_limit": row.get("interpretation_limit") or "",
        "evidence": row.get("evidence") or {},
        "current_value": row.get("current_value"),
        "baseline_value": row.get("baseline_value"),
        "value_unit": row.get("value_unit") or "",
        "occurrence_count": row.get("occurrence_count", 1),
        "first_seen_at": row.get("first_seen_at"),
        "last_seen_at": row.get("last_seen_at"),
    }


def _fetch_observations(
    conn: Connection, start: datetime, end: datetime, scope: ReportScope
) -> list[dict]:
    """
    取回本期「仍在觀察中」的 observe 級判定：`last_seen_at` 落在本期內，
    即代表本期至少再被判定一次；`observation` 沒有 status 欄位，
    「目前是否還在」就是靠這個時間窗口界定，早於本期就不撈——理由見
    db/schema.sql `observation` 表的說明，不另外維護一套結案流程。

    JOIN device 用 LEFT JOIN 而非 INNER JOIN，理由與
    `_fetch_resolved_findings` 相同：`observation.device_id` 允許 NULL
    （對應 `target_type='global'` 的判定，雖目前的 observe 規則都是
    point 層級，但不假設未來永遠如此）。
    """
    sql = """
        SELECT
            o.*,
            d.device_name, d.building, d.floor, d.system_name, d.is_standby,
            mp.position
        FROM observation o
        LEFT JOIN device d ON d.device_id = o.device_id
        LEFT JOIN measure_point mp ON mp.point_id = o.point_id
        WHERE o.last_seen_at >= %(start)s AND o.last_seen_at < %(end)s
    """ + _scope_sql("d") + """
        ORDER BY o.last_seen_at DESC
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"start": start, "end": end, **scope.params})
        return [dict(r) for r in cur.fetchall()]


def _fetch_device_status_summary(conn: Connection, scope: ReportScope) -> dict[str, Any]:
    """
    設備狀態摘要（供頁首補充統計，不含個別事項明細）。

    範圍未指定時即為全廠；指定時只計入範圍內的設備，好讓這裡的
    「共 N 台」與同一份報告其他區塊的事項清單指涉同一個母集合。
    """
    sql = "SELECT * FROM v_device_status v WHERE TRUE" + _scope_sql("v")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, scope.params)
        rows = [dict(r) for r in cur.fetchall()]
    return {
        "total_active_devices": len(rows),
        "devices_with_err": sum(1 for r in rows if (r.get("n_err") or 0) > 0),
        "devices_with_warn": sum(1 for r in rows if (r.get("n_warn") or 0) > 0),
        "devices_escalated": sum(1 for r in rows if (r.get("n_escalated") or 0) > 0),
    }


def collect_observations(
    conn: Connection,
    period_start: date,
    period_end: date,
    scope: ReportScope | None = None,
) -> dict[str, Any]:
    """
    只收集 observe 級觀察名單（`collect_weekly_data` 的一個子集）。

    獨立成公開函式，是因為 API 的 `get_weekly_report_data` 需要把觀察
    名單交給 agent，但不需要（也不該重複計算）整份週報的三段式分類與
    涵蓋率統計。兩處共用同一份查詢與分段邏輯，避免「agent 看到的觀察
    名單」和「報告上印出來的觀察名單」在同一輪流程裡對不起來。

    分段依據與 finding 相同（`first_seen_at` 是否落在本期），但沒有
    「已解決」——observation 沒有結案動作，判定沒再觸發就自然不會出現在
    下一期的查詢結果裡（母集合是 `last_seen_at` 落在本期內）。

    Returns:
        `{"new_observations", "tracking_observations", "observations_by_device"}`
    """
    scope = scope or ReportScope()
    start, end = _period_bounds(period_start, period_end)

    observation_rows = _fetch_observations(conn, start, end, scope)
    new_observations: list[dict[str, Any]] = []
    tracking_observations: list[dict[str, Any]] = []
    for row in observation_rows:
        normalized = _normalize_observation(row)
        first_seen = row.get("first_seen_at")
        is_new = first_seen is not None and start <= _as_aware(first_seen) <= end
        if is_new:
            new_observations.append(normalized)
        else:
            tracking_observations.append(normalized)

    # 依設備彙總計數，供週報用一行文字呈現「哪些設備本期有觀察項目」，
    # 不必逐條列出仍能讓人掌握分佈——這正是「素材而非待辦清單」的呈現方式。
    observations_by_device: dict[str, int] = {}
    for o in new_observations + tracking_observations:
        dev_key = o["device_label"].split(" / ")[0]
        observations_by_device[dev_key] = observations_by_device.get(dev_key, 0) + 1

    return {
        "new_observations": new_observations,
        "tracking_observations": tracking_observations,
        "observations_by_device": observations_by_device,
    }


def collect_weekly_data(
    conn: Connection,
    period_start: date,
    period_end: date,
    scope: ReportScope | None = None,
) -> dict[str, Any]:
    """
    收集週報所需的全部資料（本期範圍為 `[period_start, period_end]`，皆含）。

    `scope` 不給（或三個欄位皆 `None`）代表全廠；給了則各區塊一律只計入
    範圍內的設備，理由見 `ReportScope` 的說明——唯一的例外是
    `ingestion_audit`，那是排程層級的系統面問題，不隨設備位置切分。

    回傳的 dict 直接餵給 `render.render_weekly_html`；刻意不回傳 dataclass
    ——這份結構只在 collect → render 這一段內部流動，不是跨模組契約，
    用 dict 讓 render.py 的樣板可以直接以屬性存取方式使用（Jinja2 對
    dict 與物件屬性存取語法相同），減少一層轉換。

    Returns:
        {
          "period_start", "period_end",
          "new_findings": [...],       # 本期首次發現，未結案
          "tracking_findings": [...],  # 本期之前就存在，未結案
          "resolved_findings": [...],  # 本期內結案，完全來自 DB
          "new_observations": [...],       # observe 級：本期首次觀察到
          "tracking_observations": [...],  # observe 級：本期之前就存在，本期仍再現
          "observations_by_device": {...}, # {device_id: 本期仍在觀察中的項目數}，供一行式彙總
          "coverage": {...},           # 四態涵蓋率統計
          "coverage_gaps": [...],      # 涵蓋率問題明細（斷線/資料不全/備機閒置，設備面）
          "ingestion_audit": {...},    # 匯入稽核（排程沒跑，系統面；與上者刻意分開判定）
          "device_status_summary": {...},
          "scope": {"building","floor","system_name","label"},  # 全廠時 label 為空字串
          "stats": {"err_count","warn_count","tracking_count","resolved_count","affected_devices",
                    "new_observation_count","tracking_observation_count","observed_devices"},
        }

        observation 的「新／持續中」分段方式與 finding 相同（依 first_seen_at
        是否落在本期），但沒有「已解決」——observation 沒有結案動作，判定
        沒再觸發就自然不會出現在下一期的查詢結果裡，不需要額外的第三類。
    """
    if period_end < period_start:
        raise ValueError(f"period_end ({period_end}) 早於 period_start ({period_start})")

    scope = scope or ReportScope()
    start, end = _period_bounds(period_start, period_end)

    open_rows = _fetch_open_findings(conn, scope)
    assignee_ids = {r["assigned_to"] for r in open_rows if r.get("assigned_to")}
    assignee_names = _fetch_assignee_names(conn, assignee_ids)

    # 分段依據為「首次發現是否落在本期內」，不是 occurrence_count。
    #
    # 規則引擎每日執行，同一個問題只要還在，occurrence_count 每天都會 +1。
    # 若沿用「occurrence_count == 1 才算新發現」，週一發現、持續到週日的
    # 問題在週報產出時已經是第 7 次，會被歸進「追蹤中」——結果是「本週
    # 新發現」永遠空白，三段式結構等於失效。
    #
    # （HVM 的 occurrence_count == 1 判準成立，是因為它的事項由每週執行的
    #  agent 提出，計數以週為單位遞增；本系統的引擎是每日，語意不同。）
    new_findings: list[dict[str, Any]] = []
    tracking_findings: list[dict[str, Any]] = []
    for row in open_rows:
        normalized = _normalize_open_finding(row, assignee_names)
        first_seen = row.get("first_seen_at")
        is_new = first_seen is not None and start <= _as_aware(first_seen) <= end
        if is_new:
            new_findings.append(normalized)
        else:
            tracking_findings.append(normalized)

    resolved_rows = _fetch_resolved_findings(conn, start, end, scope)
    resolved_findings = [_normalize_resolved_finding(r) for r in resolved_rows]

    # observe 級觀察名單：與 API 的 get_weekly_report_data 共用同一份收集
    # 邏輯（見 collect_observations），確保 agent 看到的名單與報告上印出來
    # 的名單在同一輪流程裡一致。
    obs = collect_observations(conn, period_start, period_end, scope)
    new_observations = obs["new_observations"]
    tracking_observations = obs["tracking_observations"]
    observations_by_device = obs["observations_by_device"]

    coverage = _fetch_coverage(conn, start, end, scope)
    coverage_gaps = _fetch_coverage_gaps(conn, start, end, period_end, scope)
    ingestion_audit = _fetch_ingestion_audit(conn, period_start, period_end, scope)
    device_status_summary = _fetch_device_status_summary(conn, scope)

    affected_devices = {f["device_label"].split(" / ")[0] for f in new_findings + tracking_findings}

    observed_devices = {o["device_label"].split(" / ")[0]
                        for o in new_observations + tracking_observations}

    stats = {
        "err_count": sum(1 for f in new_findings + tracking_findings if f["severity"] == "err"),
        "warn_count": sum(1 for f in new_findings + tracking_findings if f["severity"] == "warn"),
        "new_count": len(new_findings),
        "tracking_count": len(tracking_findings),
        "resolved_count": len(resolved_findings),
        "affected_devices": len(affected_devices),
        "new_observation_count": len(new_observations),
        "tracking_observation_count": len(tracking_observations),
        "observed_devices": len(observed_devices),
    }

    logger.info(
        "週報資料收集完成：新發現 %d、追蹤中 %d、已解決 %d、觀察名單新增 %d、"
        "觀察名單持續中 %d、涵蓋率問題 %d 筆、匯入稽核問題 %d 筆（%s ~ %s）",
        len(new_findings), len(tracking_findings), len(resolved_findings),
        len(new_observations), len(tracking_observations), len(coverage_gaps),
        len(ingestion_audit["issues"]), period_start, period_end,
    )
    if ingestion_audit["all_missing_dates"]:
        logger.warning(
            "本期有 %d 天全廠皆無匯入紀錄，當日判定不具參考價值：%s",
            len(ingestion_audit["all_missing_dates"]), ingestion_audit["all_missing_dates"],
        )

    return {
        "period_start": period_start,
        "period_end": period_end,
        "new_findings": new_findings,
        "tracking_findings": tracking_findings,
        "resolved_findings": resolved_findings,
        "new_observations": new_observations,
        "tracking_observations": tracking_observations,
        "observations_by_device": observations_by_device,
        "coverage": coverage,
        "coverage_gaps": coverage_gaps,
        "ingestion_audit": ingestion_audit,
        "device_status_summary": device_status_summary,
        "scope": {
            "building": scope.building,
            "floor": scope.floor,
            "system_name": scope.system_name,
            "label": scope.label(),
        },
        "stats": stats,
    }
