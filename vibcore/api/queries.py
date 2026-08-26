"""
queries.py — API 專用的補充查詢與寫入

`vibcore/db/repository.py` 已涵蓋大部分資料存取；此處只補它未提供、但
API 層需要的三類東西：

1. **跨表投影查詢**（`v_device_status` 清單、finding 的完整上下文含
   回覆與狀態歷程）——這些是單純的投影/JOIN，不涉及業務判定，因此不算
   違反「不在 API 層重算業務邏輯」的原則。**SLA 逾期判定一律沿用
   `v_open_finding`**（透過 `repository.get_open_findings()`），本檔案
   不重寫一份。
2. **週報彙總所需的期間統計**（新增/追蹤中/已解決 findings 計數、涵蓋率）。
   `send_report` 與 `get_weekly_report_data` 共用 `finding_summary_for_period()`
   同一份定義，避免兩處各自算出不同的「新增幾件」。
3. **`send_report` 的寫入**（`weekly_report`、`audit_log`）與每日發送次數查詢。

比照 `repository.py` 的規範：函式只接收已開啟的 `conn`，不自行 commit；
交易邊界由呼叫端的 `get_connection()` 決定。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import psycopg2.extensions
from psycopg2.extras import Json

from vibcore.types import CLOSED_STATUSES

logger = logging.getLogger(__name__)

Connection = psycopg2.extensions.connection


def _scope_clause(
    building: str | None, floor: str | None, system_name: str | None, alias: str = "d"
) -> tuple[list[str], list[Any]]:
    """組出 building/floor/system_name 篩選子句；三者皆為選填的 AND 條件。"""
    clauses: list[str] = []
    params: list[Any] = []
    if building:
        clauses.append(f"{alias}.building = %s")
        params.append(building)
    if floor:
        clauses.append(f"{alias}.floor = %s")
        params.append(floor)
    if system_name:
        clauses.append(f"{alias}.system_name = %s")
        params.append(system_name)
    return clauses, params


# =============================================================
# 設備清單（v_device_status）
# =============================================================

def list_device_status(
    conn: Connection,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
    severity: str | None = None,
) -> list[dict]:
    """
    查 `v_device_status` 檢視。`severity` 篩選語意：
      - 'err'  → 至少一件 err 等級的未結案事項
      - 'warn' → 沒有 err，但至少一件 warn
      - 'ok'   → 目前沒有任何未結案事項
    """
    clauses, params = _scope_clause(building, floor, system_name, alias="v")
    if severity == "err":
        clauses.append("v.n_err > 0")
    elif severity == "warn":
        clauses.append("v.n_err = 0 AND v.n_warn > 0")
    elif severity == "ok":
        clauses.append("v.n_err = 0 AND v.n_warn = 0")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT v.* FROM v_device_status v {where} ORDER BY v.device_id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def last_real_data_at(conn: Connection, point_id: int) -> datetime | None:
    """
    該量測點最後一筆「真的有收到資料」的小時（`data_status IN ('ok','partial')`）。

    刻意不是「最新一列 measurement_agg」——聚合管線會把感測器離線的小時
    也填成 `no_data` 佔位列（見 `pipeline/aggregate.py` 的 `_fill_gap_hours`），
    若直接取最新一列的 `ts_hour`，離線很久的感測器仍會被算成「剛剛才有
    資料」，`data_age_minutes` 就完全失去斷線偵測的意義。
    """
    sql = """
        SELECT MAX(ts_hour) AS t FROM measurement_agg
        WHERE point_id = %s AND data_status IN ('ok', 'partial')
    """
    with conn.cursor() as cur:
        cur.execute(sql, (point_id,))
        row = cur.fetchone()
    return row["t"] if row else None


# =============================================================
# Finding 完整上下文（get_event_context）
# =============================================================

def get_finding_by_key(conn: Connection, finding_key: str) -> dict | None:
    """單一 finding 及其設備/量測點資訊、SLA 用時間差。找不到回傳 None。"""
    sql = """
        SELECT f.*, d.device_name, d.building, d.floor, d.system_name, d.is_standby,
               mp.position,
               EXTRACT(DAY FROM now() - f.first_seen_at)::INT    AS days_open,
               EXTRACT(DAY FROM now() - f.stage_entered_at)::INT AS days_in_stage
        FROM finding f
        JOIN device d              ON d.device_id = f.device_id
        LEFT JOIN measure_point mp ON mp.point_id = f.point_id
        WHERE f.finding_key = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, (finding_key,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_finding_notes(conn: Connection, finding_id: int) -> list[dict]:
    """該 finding 各階段的完整回覆歷史，由舊到新。"""
    sql = """
        SELECT n.note_id, n.stage, n.author_role AS role, n.is_human,
               n.note, n.action_taken, n.root_cause, n.created_at,
               u.display_name AS author
        FROM finding_note n
        LEFT JOIN app_user u ON u.user_id = n.author_id
        WHERE n.finding_id = %s
        ORDER BY n.created_at ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (finding_id,))
        return [dict(r) for r in cur.fetchall()]


def get_finding_status_history(conn: Connection, finding_id: int) -> list[dict]:
    """該 finding 的簽核狀態轉換歷史，含各階段停留天數。"""
    sql = """
        SELECT h.from_status, h.to_status, h.changed_at, h.note,
               u.display_name AS changed_by,
               EXTRACT(EPOCH FROM h.duration_in_from_status) / 86400.0 AS duration_days
        FROM finding_status_history h
        LEFT JOIN app_user u ON u.user_id = h.changed_by
        WHERE h.finding_id = %s
        ORDER BY h.changed_at ASC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (finding_id,))
        return [dict(r) for r in cur.fetchall()]


# =============================================================
# 週報彙總（get_weekly_report_data / send_report 共用）
# =============================================================

def point_coverage_for_period(
    conn: Connection,
    start: datetime,
    end: datetime,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
) -> list[dict]:
    """`[start, end)` 期間內，各量測點的 ok 小時數 / 總小時數。"""
    clauses, params = _scope_clause(building, floor, system_name, alias="d")
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT mp.point_id, mp.device_id, mp.position,
               COUNT(*) FILTER (WHERE ma.data_status = 'ok') AS ok_hours,
               COUNT(*)                                       AS total_hours
        FROM measurement_agg ma
        JOIN measure_point mp ON mp.point_id = ma.point_id
        JOIN device d         ON d.device_id = mp.device_id
        WHERE ma.ts_hour >= %s AND ma.ts_hour < %s {extra}
        GROUP BY mp.point_id, mp.device_id, mp.position
        ORDER BY mp.device_id, mp.position
    """
    with conn.cursor() as cur:
        cur.execute(sql, [start, end, *params])
        return [dict(r) for r in cur.fetchall()]


def finding_summary_for_period(
    conn: Connection,
    start: datetime,
    end: datetime,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
) -> dict:
    """
    週報三段式（新發現／追蹤中／已解決）的計數與清單，`[start, end)` 期間。

    `get_weekly_report_data` 與 `send_report` 都呼叫這支函式取得
    new_count/tracking_count/resolved_count，避免兩處對「新增幾件」給出
    不同答案。
    """
    clauses, params = _scope_clause(building, floor, system_name, alias="d")
    extra = (" AND " + " AND ".join(clauses)) if clauses else ""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT f.finding_key, f.device_id, f.target, f.issue_type, f.title,
                   f.severity, f.family, f.first_seen_at
            FROM finding f JOIN device d ON d.device_id = f.device_id
            WHERE f.first_seen_at >= %s AND f.first_seen_at < %s {extra}
            ORDER BY f.first_seen_at
            """,
            [start, end, *params],
        )
        new_findings = [dict(r) for r in cur.fetchall()]

        cur.execute(
            f"""
            SELECT f.finding_key, f.device_id, f.target, f.issue_type, f.title,
                   f.severity, f.family, f.resolved_at, f.resolved_by
            FROM finding f JOIN device d ON d.device_id = f.device_id
            WHERE f.resolved_at >= %s AND f.resolved_at < %s
              AND f.status = ANY(%s) {extra}
            ORDER BY f.resolved_at
            """,
            [start, end, list(CLOSED_STATUSES), *params],
        )
        resolved_findings = [dict(r) for r in cur.fetchall()]

        cur.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM finding f JOIN device d ON d.device_id = f.device_id
            WHERE f.status <> ALL(%s) AND f.first_seen_at < %s {extra}
            """,
            [list(CLOSED_STATUSES), start, *params],
        )
        tracking_count = cur.fetchone()["n"]

    return {
        "new_findings": new_findings,
        "resolved_findings": resolved_findings,
        "new_count": len(new_findings),
        "resolved_count": len(resolved_findings),
        "tracking_count": tracking_count,
    }


# =============================================================
# send_report：每日次數、落庫、稽核
# =============================================================

def count_send_report_today(conn: Connection) -> int:
    """本日曆日（DB session 時區，預設 UTC）已成功寄送次數；只計成功寫入 audit_log 的呼叫。"""
    sql = """
        SELECT COUNT(*) AS n FROM audit_log
        WHERE action = 'send_report'
          AND occurred_at >= date_trunc('day', now())
          AND occurred_at <  date_trunc('day', now()) + interval '1 day'
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()["n"]


def upsert_weekly_report(
    conn: Connection,
    *,
    report_type: str,
    period_label: str,
    period_start,
    period_end,
    verdict: str,
    headline: str,
    agent_payload: dict,
    html: str,
    new_count: int,
    tracking_count: int,
    resolved_count: int,
) -> dict:
    """
    寫入/更新 `weekly_report`。`(report_type, period_label)` 為 UNIQUE 鍵，
    同一期間重送視為重新產出該期報告（例如 agent 重跑同一天的日報），
    upsert 讓這個操作冪等；**是否計入每日發送次數由 `audit_log` 另計**，
    重送同一期間仍會消耗一次額度（見模組上方說明）。
    """
    sql = """
        INSERT INTO weekly_report
            (report_type, period_label, period_start, period_end, verdict, headline,
             agent_payload, html, new_count, tracking_count, resolved_count, generated_at)
        VALUES (%(report_type)s, %(period_label)s, %(period_start)s, %(period_end)s,
                %(verdict)s, %(headline)s, %(agent_payload)s, %(html)s,
                %(new_count)s, %(tracking_count)s, %(resolved_count)s, now())
        ON CONFLICT (report_type, period_label) DO UPDATE SET
            period_start   = EXCLUDED.period_start,
            period_end     = EXCLUDED.period_end,
            verdict        = EXCLUDED.verdict,
            headline       = EXCLUDED.headline,
            agent_payload  = EXCLUDED.agent_payload,
            html           = EXCLUDED.html,
            new_count      = EXCLUDED.new_count,
            tracking_count = EXCLUDED.tracking_count,
            resolved_count = EXCLUDED.resolved_count,
            generated_at   = now()
        RETURNING *
    """
    params = {
        "report_type": report_type,
        "period_label": period_label,
        "period_start": period_start,
        "period_end": period_end,
        "verdict": verdict,
        "headline": headline,
        "agent_payload": Json(agent_payload),
        "html": html,
        "new_count": new_count,
        "tracking_count": tracking_count,
        "resolved_count": resolved_count,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return dict(cur.fetchone())


def insert_audit_log(
    conn: Connection, actor: str, action: str, target: str | None, detail: dict
) -> None:
    """寫入稽核紀錄；`send_report` 每次成功呼叫都必須寫一筆（見計畫書「四道卡控」）。"""
    sql = "INSERT INTO audit_log (actor, action, target, detail) VALUES (%s, %s, %s, %s)"
    with conn.cursor() as cur:
        cur.execute(sql, (actor, action, target, Json(detail)))
