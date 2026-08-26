"""
service.py — 組裝各工具回應內容

本檔案是 API 層真正的「業務邏輯」，但這裡的業務邏輯僅限**組裝與呈現**：
呼叫 `vibcore/db/repository.py`、`vibcore/api/queries.py`、
`vibcore/metrics/*`（ISO 分級、趨勢回歸），把結果轉成 JSON 安全的 dict。

**刻意不重算的東西**：
  - SLA 逾期判定一律讀 `v_open_finding`（經 `repository.get_open_findings()`），
    不在這裡用 Python 重寫一份「超過幾天算逾期」的邏輯——三處定義
    （Dashboard／週報／這支 API）不一致是遲早的事，見計畫書 §六。
  - 涵蓋率門檻（`analyzable_ratio >= 0.5`）直接重用
    `vibcore.types.CoverageInfo.is_sufficient`，不在本檔案另訂一次 0.5。
  - ISO Zone 判定與趨勢回歸分別重用 `vibcore.metrics.iso.evaluate_iso()`
    與 `vibcore.metrics.trend.compute_trend()`，這兩個模組本來就是為了
    被消費端（API、規則層、Dashboard）共用而設計。

**護欄**（計畫書 §8.2）：每個 finding 一律帶 `interpretation_limit`；
規則層寫入時若漏填會在 `rules/engine.py` 記警告，但既有資料仍可能是空
字串，這裡再補一層防線（`_FALLBACK_INTERPRETATION_LIMIT`），確保 API
回傳永遠不會讓 agent 拿到「沒有解讀邊界」的證據。
"""

from __future__ import annotations

import html as html_escape
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException

from vibcore.api import queries
from vibcore.api.schemas import SendReportRequest
from vibcore.api.util import jsonable
from vibcore.config import AGG_SPEC
from vibcore.db import repository
from vibcore.metrics import iso as iso_mod
from vibcore.metrics import trend as trend_mod
from vibcore.pipeline.aggregate import coverage_report
from vibcore.types import CoverageInfo

logger = logging.getLogger(__name__)

#: 找不到解讀邊界時的保底文字——不可讓 agent 拿到完全沒有邊界說明的證據。
_FALLBACK_INTERPRETATION_LIMIT = (
    "系統未提供此證據的解讀邊界說明；請勿據此判定故障類型，"
    "建議先確認資料完整性後再引用此筆事項。"
)

#: 貫穿所有回應的護欄提示（計畫書 §8.2）：不得臆測故障類型，只能陳述
#: 現象與建議下一步。放在每個工具的回應裡，而不是只寫在系統提示——
#: API 回應是比系統提示更難被忽略、更容易被審計的護欄落地點。
GUARDRAIL_NOTE = (
    "本系統定位為篩選與預警，不做故障診斷（見 interpretation_limit）。"
    "禁止輸出具體故障類型判定（如「疑似對心不良」「軸承內環缺陷」）"
    "或維修建議（如「建議更換軸承」）；僅可陳述觀測到的現象與建議的下一步"
    "（如「建議安排專家系統複測以確認成因」）。歷史案例可引用，但需標明"
    "為歷史案例而非本次判定。"
)

#: get_device_status / get_event_context 的資料品質觀察窗口（天）
_STATUS_WINDOW_DAYS = 7
_EVENT_CONTEXT_WINDOW_DAYS = 30

#: get_device_status 呈現的「目前指標水準」欄位
_CURRENT_METRIC_FIELDS = (
    "vel_rms", "vel_oa", "acc_rms", "acc_oa", "acc_peak",
    "acc_crest", "acc_kurt", "acc_skew", "disp_rms", "disp_p2p",
    "acc_weighted_mean_freq",
)

#: get_device_trend 回傳的時間序列點數上限，避免單次回應過大
_MAX_TREND_SERIES_POINTS = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _finding_public(row: dict) -> dict:
    """把 finding 相關的 DB row 轉成給 agent 的 dict，強制補上 interpretation_limit。"""
    d = jsonable(dict(row))
    if not d.get("interpretation_limit"):
        logger.warning(
            f"finding_key={d.get('finding_key')} 缺少 interpretation_limit，"
            "已套用保底說明"
        )
        d["interpretation_limit"] = _FALLBACK_INTERPRETATION_LIMIT
    return d


# =============================================================
# get_vibration_thresholds
# =============================================================

def get_vibration_thresholds(conn) -> dict:
    """現行 ISO 門檻與規則設定；對應 HVM 的 `get_alert_thresholds`。"""
    iso_thresholds = repository.get_iso_thresholds(conn)
    rule_configs = repository.get_rule_configs(conn, active_only=False)
    sla = repository.get_sla_config(conn, active_only=True)
    return {
        "iso_thresholds": jsonable(iso_thresholds),
        "rules": jsonable(rule_configs),
        "sla_days": sla,
        "note": (
            "門檻與規則設定僅在此處查得算數；找不到的規則不代表無限制，"
            "代表該規則尚未啟用或設定，agent 不應自行假設一個業界通用門檻。"
        ),
    }


# =============================================================
# get_device_list
# =============================================================

def get_device_list(
    conn,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
    severity: str | None = None,
) -> dict:
    """設備清單與最新狀態；篩選條件不匹配時回傳空清單（非錯誤）。"""
    rows = queries.list_device_status(
        conn, building=building, floor=floor, system_name=system_name, severity=severity
    )
    devices = []
    for r in rows:
        d = jsonable(r)
        n_err, n_warn = d.get("n_err", 0) or 0, d.get("n_warn", 0) or 0
        d["status"] = "err" if n_err else ("warn" if n_warn else "ok")
        devices.append(d)
    return {"devices": devices, "count": len(devices)}


# =============================================================
# get_device_status（必須含 data_age_minutes；帶涵蓋率與斷線狀況）
# =============================================================

def _latest_ok_metrics(agg) -> dict:
    """取最近一筆 `data_status == 'ok'` 列的關鍵指標；找不到則回傳 None 值並附說明。"""
    if agg is None or agg.empty or "data_status" not in agg.columns:
        return {"as_of": None, "values": None,
                "note": f"近 {_STATUS_WINDOW_DAYS} 天無聚合資料"}
    ok = agg[agg["data_status"] == "ok"]
    if ok.empty:
        return {"as_of": None, "values": None,
                "note": f"近 {_STATUS_WINDOW_DAYS} 天無 data_status=ok 的可信資料"}
    row = ok.sort_values("ts_hour").iloc[-1]
    values = {f: jsonable(row.get(f)) for f in _CURRENT_METRIC_FIELDS if f in row.index}
    return {"as_of": jsonable(row["ts_hour"]), "values": values, "note": ""}


def _point_status(conn, device, point: dict) -> dict:
    point_id = point["point_id"]
    now = _now()
    start = now - timedelta(days=_STATUS_WINDOW_DAYS)
    agg = repository.get_agg(conn, point_id, start, now + timedelta(hours=1))

    last_real = queries.last_real_data_at(conn, point_id)
    data_age_minutes = None
    if last_real is not None:
        if last_real.tzinfo is None:
            last_real = last_real.replace(tzinfo=timezone.utc)
        data_age_minutes = round((now - last_real).total_seconds() / 60.0, 1)

    cov = coverage_report(agg)
    baseline = repository.get_baseline(conn, point_id)
    iso_result = iso_mod.evaluate_iso(agg, device, baseline)

    return {
        "point_id": point_id,
        "position": point.get("position"),
        "data_age_minutes": data_age_minutes,
        "coverage": jsonable(cov),
        "iso": {
            "applicable": iso_result.applicable,
            "machine_class": iso_result.machine_class,
            "class_source": iso_result.class_source,
            "zone": iso_result.zone,
            "vel_rms": iso_result.vel_rms,
            "thresholds": iso_result.thresholds,
            "is_class_suspect": iso_result.is_class_suspect,
            "suspect_reason": iso_result.suspect_reason,
            "note": iso_result.note,
        },
        "current_metrics": _latest_ok_metrics(agg),
    }


def get_device_status(conn, device_id: str) -> dict:
    """單台設備目前狀態；含 `data_age_minutes`、涵蓋率與斷線狀況。查無設備回 error。"""
    device = repository.get_device(conn, device_id)
    if device is None:
        return {"error": f"設備不存在：{device_id}"}

    points = repository.get_points_for_device(conn, device_id)
    point_blocks = [_point_status(conn, device, p) for p in points]

    ages = [p["data_age_minutes"] for p in point_blocks if p["data_age_minutes"] is not None]
    device_data_age = round(min(ages), 1) if ages else None

    open_rows = repository.get_open_findings(conn) or []
    dev_open = [r for r in open_rows if r.get("device_id") == device_id]
    open_count = Counter(r["severity"] for r in dev_open)

    return {
        "device_id": device.device_id,
        "device_name": device.device_name,
        "building": device.building,
        "floor": device.floor,
        "system_name": device.system_name,
        "is_standby": device.is_standby,
        "iso_machine_class": device.iso_machine_class,
        "iso_class_source": device.iso_class_source,
        "data_age_minutes": device_data_age,
        "points": point_blocks,
        "open_finding_count": {"err": open_count.get("err", 0), "warn": open_count.get("warn", 0)},
        "interpretation_limit": (
            "本回應為設備目前狀態掃描（含最近一筆可信讀數與 ISO Zone），"
            "不構成趨勢或劣化速率結論；歷史趨勢請另呼叫 get_device_trend。"
        ),
    }


# =============================================================
# get_device_trend
# =============================================================

def get_device_trend(
    conn, device_id: str, metric: str, position: str | None = None, days: int = 30
) -> dict:
    """指標歷史趨勢；`metric` 須為 `vibcore.config.AGG_SPEC` 的鍵名。"""
    device = repository.get_device(conn, device_id)
    if device is None:
        return {"error": f"設備不存在：{device_id}"}
    if metric not in AGG_SPEC:
        return {"error": f"未知指標：{metric}；可用指標：{sorted(AGG_SPEC)}"}

    points = repository.get_points_for_device(conn, device_id)
    if not points:
        return {"error": f"設備 {device_id} 沒有啟用中的量測點"}
    point = next((p for p in points if p.get("position") == position), None) if position else points[0]
    if point is None:
        return {"error": f"量測點不存在：{device_id}/{position}"}

    point_id = point["point_id"]
    now = _now()
    start = now - timedelta(days=days)
    agg = repository.get_agg(conn, point_id, start, now + timedelta(hours=1))
    baseline = repository.get_baseline(conn, point_id)
    trend = trend_mod.compute_trend(agg, metric, baseline)
    cov = coverage_report(agg)

    series = []
    if agg is not None and not agg.empty:
        tail = agg.sort_values("ts_hour").tail(_MAX_TREND_SERIES_POINTS)
        for _, r in tail.iterrows():
            series.append({
                "ts_hour": jsonable(r.get("ts_hour")),
                "value": jsonable(r.get(metric)),
                "data_status": r.get("data_status"),
            })

    interp = (
        "此為線性回歸估計出的變化速率，反映觀測到的數值走勢，不代表故障成因，"
        "亦非剩餘壽命預估；confidence 為 low 時不可引用斜率或推估天數作為結論。"
    )
    if trend.note:
        interp = f"{interp}（{trend.note}）"

    return {
        "device_id": device_id,
        "point_id": point_id,
        "position": point.get("position"),
        "metric": metric,
        "days": days,
        "trend": {
            "slope_per_day": jsonable(trend.slope_per_day),
            "slope_per_month": jsonable(trend.slope_per_month),
            "slope_pct_per_month": jsonable(trend.slope_pct_per_month),
            "r2": jsonable(trend.r2),
            "n_points": trend.n_points,
            "span_days": trend.span_days,
            "direction": trend.direction,
            "confidence": trend.confidence,
            "note": trend.note,
        },
        "coverage": jsonable(cov),
        "series": series,
        "interpretation_limit": interp,
    }


# =============================================================
# get_open_findings（產報告前必呼叫）
# =============================================================

def get_open_findings(
    conn,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
    only_escalated: bool = False,
    only_sla_breached: bool = False,
) -> dict:
    """未結案事項 + 工程師最後回覆；SLA 逾期判定沿用 `v_open_finding`。"""
    rows = repository.get_open_findings(
        conn, building=building, floor=floor, system_name=system_name,
        only_escalated=only_escalated, only_sla_breached=only_sla_breached,
    )
    findings = [_finding_public(r) for r in rows]
    return {
        "findings": findings,
        "count": len(findings),
        "agent_guidance": GUARDRAIL_NOTE,
    }


# =============================================================
# get_weekly_report_data（涵蓋率與斷線狀況）
# =============================================================

def get_weekly_report_data(
    conn,
    days: int = 7,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
) -> dict:
    """週報彙總；`days=1` 供日報使用。"""
    now = _now()
    start = now - timedelta(days=days)

    summary = queries.finding_summary_for_period(
        conn, start, now, building=building, floor=floor, system_name=system_name
    )
    open_rows = repository.get_open_findings(
        conn, building=building, floor=floor, system_name=system_name
    )
    open_by_severity = Counter(r["severity"] for r in open_rows)
    escalated_rows = [r for r in open_rows if r.get("escalated_at")]

    coverage_rows = queries.point_coverage_for_period(
        conn, start, now, building=building, floor=floor, system_name=system_name
    )
    coverage_items = []
    insufficient = []
    ratios = []
    for r in coverage_rows:
        total = int(r["total_hours"] or 0)
        ok = int(r["ok_hours"] or 0)
        ratio = ok / total if total else 0.0
        info = CoverageInfo(
            total_hours=total, ok_hours=ok, partial_hours=0, no_data_hours=0,
            not_running_hours=0, analyzable_ratio=ratio,
        )
        ratios.append(ratio)
        item = {
            "device_id": r["device_id"], "point_id": r["point_id"], "position": r["position"],
            "analyzable_ratio": round(ratio, 4), "is_sufficient": info.is_sufficient,
        }
        coverage_items.append(item)
        if not info.is_sufficient:
            insufficient.append(item)

    org_ratio = round(sum(ratios) / len(ratios), 4) if ratios else 0.0

    return {
        "period": {"start": jsonable(start), "end": jsonable(now), "days": days},
        "findings_summary": {
            "currently_open": {
                "err": open_by_severity.get("err", 0),
                "warn": open_by_severity.get("warn", 0),
                "total": len(open_rows),
            },
            "new_this_period": jsonable(summary["new_findings"]),
            "new_count": summary["new_count"],
            "resolved_this_period": jsonable(summary["resolved_findings"]),
            "resolved_count": summary["resolved_count"],
            "tracking_count": summary["tracking_count"],
            "escalated": [_finding_public(r) for r in escalated_rows],
            "escalated_count": len(escalated_rows),
        },
        "coverage": {
            "total_points": len(coverage_items),
            "points_with_insufficient_data": insufficient,
            "org_analyzable_ratio": org_ratio,
            "note": (
                "analyzable_ratio 低於 50% 的量測點，其期間內的趨勢/位準結論"
                "信心度不足，週報應標示信心度或略過結論。"
            ),
        },
        "interpretation_limit": GUARDRAIL_NOTE,
    }


# =============================================================
# get_event_context
# =============================================================

def get_event_context(conn, finding_key: str) -> dict:
    """單一 finding 的完整上下文（回覆歷史、狀態歷程、資料品質），供產出回覆建議。"""
    row = queries.get_finding_by_key(conn, finding_key)
    if row is None:
        return {"error": f"finding_key 不存在：{finding_key}"}

    finding_id = row["finding_id"]
    notes = queries.get_finding_notes(conn, finding_id)
    history = queries.get_finding_status_history(conn, finding_id)

    data_quality = None
    if row.get("point_id"):
        now = _now()
        start = now - timedelta(days=_EVENT_CONTEXT_WINDOW_DAYS)
        agg = repository.get_agg(conn, row["point_id"], start, now + timedelta(hours=1))
        data_quality = jsonable(coverage_report(agg))

    finding_public = _finding_public(row)
    return {
        "finding": finding_public,
        "notes": jsonable(notes),
        "status_history": jsonable(history),
        "data_quality": data_quality,
        "interpretation_limit": finding_public["interpretation_limit"],
        "agent_guidance": GUARDRAIL_NOTE,
    }


# =============================================================
# send_report（四道卡控）
# =============================================================

def _render_placeholder_html(
    report_type: str, period_label: str, verdict: str, headline: str,
    actions: list[dict], notes: str | None,
) -> str:
    """
    產生佔位用的排版 HTML；`headline`/`notes`/`actions[].text` 在呼叫前已
    完成 HTML 轉義，這裡直接內插不會產生 XSS 風險。SMTP 尚未設定，此
    HTML 目前只落庫供之後的寄送介面使用，本次呼叫不會真的寄出。
    """
    action_items = "".join(f"<li class='lvl-{a['level']}'>{a['text']}</li>" for a in actions)
    notes_html = f"<p class='notes'>{notes}</p>" if notes else ""
    label = "週報" if report_type == "weekly" else "日報"
    return (
        f"<div class='vib-report'><h2>{label} {period_label}</h2>"
        f"<p class='verdict verdict-{verdict}'>總評：{verdict}</p>"
        f"<p class='headline'>{headline}</p>"
        f"<ul class='actions'>{action_items}</ul>{notes_html}</div>"
    )


def send_report(conn, payload: SendReportRequest, daily_limit: int) -> dict:
    """
    收 verdict/headline/actions/notes，本系統負責排版與（未來）寄送。

    四道卡控：
      1. 收件人由系統設定決定——`SendReportRequest` 結構本身不接受收件人欄位
         （`extra="forbid"`，見 schemas.py）。
      2. 主旨由系統產生（`_render_placeholder_html` 內的標題列），呼叫方不可自訂。
      3. 只收結構化欄位；`headline`/`notes`/`actions[].text` 一律 HTML 轉義。
      4. 每日發送次數上限（預設 3，環境變數 `VIB_REPORT_DAILY_LIMIT` 可調），
         超過回 429；每次成功呼叫寫入 `audit_log`。

    SMTP 尚未設定：報告存進 `weekly_report` 並回傳結果，`delivery.sent`
    固定為 False，寄送介面已預留（見 `_render_placeholder_html`）。
    """
    sent_today = queries.count_send_report_today(conn)
    if sent_today >= daily_limit:
        raise HTTPException(
            status_code=429,
            detail=f"已達每日寄送上限（{daily_limit} 次），請明日再試",
        )

    headline = html_escape.escape(payload.headline)
    notes = html_escape.escape(payload.notes) if payload.notes else None
    actions = [
        {"level": a.level, "text": html_escape.escape(a.text)} for a in payload.actions
    ]

    start_dt = datetime.combine(payload.period_start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(payload.period_end, time.min, tzinfo=timezone.utc) + timedelta(days=1)
    summary = queries.finding_summary_for_period(
        conn, start_dt, end_dt,
        building=payload.building, floor=payload.floor, system_name=payload.system_name,
    )

    if payload.report_type == "weekly":
        iso_cal = payload.period_end.isocalendar()
        period_label = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
    else:
        period_label = payload.period_end.isoformat()

    agent_payload = {
        "verdict": payload.verdict,
        "headline": headline,
        "actions": actions,
        "notes": notes,
        "submitted_at": jsonable(_now()),
    }
    report_html = _render_placeholder_html(
        payload.report_type, period_label, payload.verdict, headline, actions, notes
    )

    row = queries.upsert_weekly_report(
        conn,
        report_type=payload.report_type,
        period_label=period_label,
        period_start=payload.period_start,
        period_end=payload.period_end,
        verdict=payload.verdict,
        headline=headline,
        agent_payload=agent_payload,
        html=report_html,
        new_count=summary["new_count"],
        tracking_count=summary["tracking_count"],
        resolved_count=summary["resolved_count"],
    )

    queries.insert_audit_log(
        conn, actor="agent", action="send_report",
        target=f"{payload.report_type}:{period_label}",
        detail={
            "report_id": row["report_id"], "verdict": payload.verdict,
            "headline": headline, "n_actions": len(actions),
        },
    )

    return {
        "report_id": row["report_id"],
        "report_type": row["report_type"],
        "period_label": row["period_label"],
        "period_start": jsonable(row["period_start"]),
        "period_end": jsonable(row["period_end"]),
        "verdict": row["verdict"],
        "headline": row["headline"],
        "actions": actions,
        "notes": notes,
        "new_count": row["new_count"],
        "tracking_count": row["tracking_count"],
        "resolved_count": row["resolved_count"],
        "generated_at": jsonable(row["generated_at"]),
        "delivery": {
            "sent": False,
            "channel": "email",
            "note": "SMTP 尚未設定，本次僅落庫，尚未寄出。寄送介面已預留，待 SMTP 設定後啟用。",
        },
        "daily_send_count": sent_today + 1,
        "daily_send_limit": daily_limit,
    }
