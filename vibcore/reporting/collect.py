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
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg2.extensions
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

Connection = psycopg2.extensions.connection

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


def _fetch_open_findings(conn: Connection) -> list[dict]:
    """
    查 `v_open_finding`：未結案事項 + SLA 逾期判定 + 最新人工回覆，全部
    交給 SQL 檢視計算（見 repository.get_open_findings 的設計說明）——
    週報、Dashboard、API 三處若各自重算 SLA 邏輯，遲早會兜不起來。
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM v_open_finding ORDER BY peak_severity DESC, stage_entered_at ASC")
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


def _fetch_resolved_findings(conn: Connection, start: datetime, end: datetime) -> list[dict]:
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
        ORDER BY f.resolved_at DESC
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"closed": _CLOSED_STATUSES, "start": start, "end": end})
        return [dict(r) for r in cur.fetchall()]


def _fetch_coverage(conn: Connection, start: datetime, end: datetime) -> dict[str, Any]:
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
        GROUP BY ma.data_status
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"start": start, "end": end})
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
    conn: Connection, start: datetime, end: datetime, period_end: date
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
        GROUP BY mp.point_id, mp.position, d.device_id, d.device_name,
                 d.building, d.floor, d.system_name, d.is_standby
        ORDER BY no_data_hours DESC, partial_hours DESC
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"start": start, "end": end})
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
            GROUP BY mp.point_id, mp.position, d.device_id, d.building, d.floor, d.system_name
            """,
            {"end": end},
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


def _fetch_device_status_summary(conn: Connection) -> dict[str, Any]:
    """全廠設備狀態摘要（供頁首補充統計，不含個別事項明細）。"""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM v_device_status")
        rows = [dict(r) for r in cur.fetchall()]
    return {
        "total_active_devices": len(rows),
        "devices_with_err": sum(1 for r in rows if (r.get("n_err") or 0) > 0),
        "devices_with_warn": sum(1 for r in rows if (r.get("n_warn") or 0) > 0),
        "devices_escalated": sum(1 for r in rows if (r.get("n_escalated") or 0) > 0),
    }


def collect_weekly_data(conn: Connection, period_start: date, period_end: date) -> dict[str, Any]:
    """
    收集週報所需的全部資料（本期範圍為 `[period_start, period_end]`，皆含）。

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
          "coverage": {...},           # 四態涵蓋率統計
          "coverage_gaps": [...],      # 涵蓋率問題明細（斷線/資料不全/備機閒置）
          "device_status_summary": {...},
          "stats": {"err_count","warn_count","tracking_count","resolved_count","affected_devices"},
        }
    """
    if period_end < period_start:
        raise ValueError(f"period_end ({period_end}) 早於 period_start ({period_start})")

    start, end = _period_bounds(period_start, period_end)

    open_rows = _fetch_open_findings(conn)
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

    resolved_rows = _fetch_resolved_findings(conn, start, end)
    resolved_findings = [_normalize_resolved_finding(r) for r in resolved_rows]

    coverage = _fetch_coverage(conn, start, end)
    coverage_gaps = _fetch_coverage_gaps(conn, start, end, period_end)
    device_status_summary = _fetch_device_status_summary(conn)

    affected_devices = {f["device_label"].split(" / ")[0] for f in new_findings + tracking_findings}

    stats = {
        "err_count": sum(1 for f in new_findings + tracking_findings if f["severity"] == "err"),
        "warn_count": sum(1 for f in new_findings + tracking_findings if f["severity"] == "warn"),
        "new_count": len(new_findings),
        "tracking_count": len(tracking_findings),
        "resolved_count": len(resolved_findings),
        "affected_devices": len(affected_devices),
    }

    logger.info(
        "週報資料收集完成：新發現 %d、追蹤中 %d、已解決 %d、涵蓋率問題 %d 筆（%s ~ %s）",
        len(new_findings), len(tracking_findings), len(resolved_findings), len(coverage_gaps),
        period_start, period_end,
    )

    return {
        "period_start": period_start,
        "period_end": period_end,
        "new_findings": new_findings,
        "tracking_findings": tracking_findings,
        "resolved_findings": resolved_findings,
        "coverage": coverage,
        "coverage_gaps": coverage_gaps,
        "device_status_summary": device_status_summary,
        "stats": stats,
    }
