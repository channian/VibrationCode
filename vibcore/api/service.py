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
from vibcore.reporting import collect_weekly_data, render_weekly_html
from vibcore.reporting.collect import ReportScope, collect_observations
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


def resolve_period(days: int, end_date: date | None = None) -> tuple[date, date]:
    """
    把「相對天數」的 `days` 參數換算成 `[period_start, period_end]`（皆為日期、
    含頭尾）——本模組所有時間窗查詢共用的**唯一**入口，不得各自實作。

    為什麼要抽成單一函式：no-code agent 平台上，日期運算是留給 LLM 自己
    算的話，算錯了不會報錯，只會安靜產出涵蓋錯誤區間的報告；改成「相對
    天數」後，日期運算移回系統側，但前提是所有端點都用**同一套**換算
    邏輯——如果 `get_weekly_report_data` 與 `send_report` 各自用
    `datetime.now() - timedelta(days=days)` 實作，兩次呼叫之間只要相差
    幾分鐘，算出來的窗口就會不一樣：週報影響不大，但日報（`days=1`）
    剛好跨過午夜時，兩邊會拿到完全不同的一天。

    做法是把窗口錨定在「日曆日」而非「呼叫當下的時間點」：以 `end_date`
    （預設今天，UTC）往前推 `days - 1` 天。同一個 UTC 日曆日之內，不論
    呼叫幾次、相差幾分鐘，都會得到一模一樣的 `(period_start, period_end)`。

    Args:
        days: 窗口天數（含 `end_date` 當天）。呼叫端（FastAPI Query / Pydantic
            Field）已驗證落在合理範圍（建議 1–365），本函式不重複驗證。
        end_date: 窗口結束日（含）；預設為 UTC 今天，供 `send_report`
            補產歷史報告時指定過去某天。

    Returns:
        `(period_start, period_end)`，皆為日期，`period_end` 一律等於
        `end_date`（或今天），`period_start = end_date - (days - 1)`。
    """
    if end_date is None:
        end_date = _now().date()
    period_start = end_date - timedelta(days=days - 1)
    return period_start, end_date


def _period_utc_bounds(period_start: date, period_end: date) -> tuple[datetime, datetime]:
    """
    把 `resolve_period()` 算出的日曆日窗口，轉成查詢用的 `[start, end)` UTC
    時間戳範圍——`period_end` 當天整天都要涵蓋，所以上界是 `period_end`
    隔天的 00:00（不含）。所有時間戳皆已是 UTC（見 docs/agent_tools.md §1.4），
    「日曆日」在此即為 UTC 日曆日，與其餘端點的時間格式一致。
    """
    start = datetime.combine(period_start, time.min, tzinfo=timezone.utc)
    end = datetime.combine(period_end, time.min, tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


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
    period_start, period_end = resolve_period(_STATUS_WINDOW_DAYS)
    start, end = _period_utc_bounds(period_start, period_end)
    agg = repository.get_agg(conn, point_id, start, end)

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
    """
    指標歷史趨勢；`metric` 須為 `vibcore.config.AGG_SPEC` 的鍵名。

    `days` 一律經 `resolve_period()` 換算成日曆日切齊的窗口，不用「現在
    往前推 N×24 小時」——理由見 `resolve_period()` docstring。
    """
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
    period_start, period_end = resolve_period(days)
    start, end = _period_utc_bounds(period_start, period_end)
    agg = repository.get_agg(conn, point_id, start, end)
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
        "period": {"start": jsonable(period_start), "end": jsonable(period_end)},
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
    """
    週報彙總；`days=1` 供日報使用。

    窗口一律經 `resolve_period()` 換算成日曆日切齊的區間，與 `send_report`
    共用同一函式——同一輪流程中先呼叫本工具、幾分鐘後再呼叫
    `send_report(days=N)`，兩者算出的期間保證相同，不會因為呼叫時間點
    相差幾分鐘而各自對到不同的一天（`days=1` 跨午夜時尤其關鍵）。
    """
    period_start, period_end = resolve_period(days)
    start, end = _period_utc_bounds(period_start, period_end)

    summary = queries.finding_summary_for_period(
        conn, start, end, building=building, floor=floor, system_name=system_name
    )
    open_rows = repository.get_open_findings(
        conn, building=building, floor=floor, system_name=system_name
    )
    open_by_severity = Counter(r["severity"] for r in open_rows)
    escalated_rows = [r for r in open_rows if r.get("escalated_at")]

    coverage_rows = queries.point_coverage_for_period(
        conn, start, end, building=building, floor=floor, system_name=system_name
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

    # observe 級觀察名單：規則判定到、但未達提報門檻的項目。agent 需要看得到
    # 這一層才能在 notes 裡寫「有幾台在觀察中」，但它**不是待辦清單**——
    # 回傳結構刻意不含 status/assignee/期限，且在 `note` 明說不要為這些項目
    # 開 action，免得觀察名單被寫成一堆需要有人回覆的事項。
    observations = collect_observations(
        conn, period_start, period_end,
        ReportScope(building=building, floor=floor, system_name=system_name),
    )

    return {
        "period": {"start": jsonable(period_start), "end": jsonable(period_end), "days": days},
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
        "observations": {
            "new_this_period": jsonable(observations["new_observations"]),
            "tracking": jsonable(observations["tracking_observations"]),
            "by_device": observations["observations_by_device"],
            "new_count": len(observations["new_observations"]),
            "tracking_count": len(observations["tracking_observations"]),
            "note": (
                "observe 級判定：規則觸發了但未達提報門檻，沒有簽核流程、"
                "沒有負責人、沒有回覆期限。**不要為這些項目開 action**——"
                "action 的定義是需要有人處理的事，observe 的定義正好是還不需要。"
                "要提及請寫在 notes，例如「另有 N 台設備 M 項指標在觀察中」。"
                "項目若持續惡化越過門檻，規則引擎會自動把它升級成 finding，"
                "屆時自然會出現在 new_this_period。"
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
        period_start, period_end = resolve_period(_EVENT_CONTEXT_WINDOW_DAYS)
        start, end = _period_utc_bounds(period_start, period_end)
        agg = repository.get_agg(conn, row["point_id"], start, end)
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

def _render_report_html(
    conn,
    payload: SendReportRequest,
    period_start: date,
    period_end: date,
    raw_agent_payload: dict,
) -> str:
    """
    產出完整報告 HTML：資料由 `collect_weekly_data` 從資料庫收集，
    agent 的評論只負責補強敘述（見 vibcore.reporting 的分工原則）。

    **這裡傳給渲染層的是未轉義的原文**，與落庫用的 `agent_payload`
    刻意不同一份。`vibcore.reporting.render` 的 Jinja2 已強制開啟
    autoescape，再餵它一份 `html.escape()` 過的字串會轉義兩次，讀者在
    報告上看到的就是 `&lt;b&gt;` 這串字面值。落庫那份維持轉義（見
    `send_report` 的四道卡控 #3），兩邊的差別只在轉義與否，內容相同。

    渲染失敗不讓整支 API 掛掉：報告內容產不出來時退回一段最小可讀的
    純文字摘要並記 exception。`send_report` 是排程流程的終點，這裡丟出
    500 只會讓當期報告整份消失，而 headline 與 verdict 這兩個最關鍵的
    結論其實不依賴渲染就已經拿到手了。
    """
    try:
        data = collect_weekly_data(
            conn, period_start, period_end,
            ReportScope(
                building=payload.building,
                floor=payload.floor,
                system_name=payload.system_name,
            ),
        )
        return render_weekly_html(data, raw_agent_payload, payload.report_type)
    except Exception:
        logger.exception(
            "報告 HTML 渲染失敗（%s %s ~ %s），改存純文字摘要",
            payload.report_type, period_start, period_end,
        )
        return _render_fallback_html(payload)


def _render_fallback_html(payload: SendReportRequest) -> str:
    """
    渲染失敗時的最小替代內容；字串在這裡自行轉義（此處是字串拼接，
    沒有樣板引擎的 autoescape 可倚賴）。
    """
    items = "".join(
        f"<li>{html_escape.escape(a.display_title)}</li>" for a in payload.actions
    )
    notes = f"<p>{html_escape.escape(payload.notes)}</p>" if payload.notes else ""
    return (
        "<div class='vib-report'>"
        "<p><b>報告排版失敗，以下為未排版的內容。</b></p>"
        f"<p>總評：{html_escape.escape(payload.verdict)}</p>"
        f"<p>{html_escape.escape(payload.headline)}</p>"
        f"<ul>{items}</ul>{notes}</div>"
    )


def send_report(conn, payload: SendReportRequest, daily_limit: int) -> dict:
    """
    收 verdict/headline/actions/notes/days，本系統負責排版與（未來）寄送。

    時間窗改為 `days`（相對天數，預設 7）+ 選填 `end_date`，一律經
    `resolve_period()` 換算成日曆日切齊的 `[period_start, period_end]`——
    與 `get_weekly_report_data` 共用同一函式，同一輪流程中兩者呼叫時間
    相差幾分鐘也不影響算出的期間（見 `resolve_period()` docstring）。
    不給 `end_date` 時預設為系統當下日期；給了則用於補產歷史報告。

    四道卡控：
      1. 收件人由系統設定決定——`SendReportRequest` 結構本身不接受收件人欄位
         （`extra="forbid"`，見 schemas.py）。
      2. 主旨由系統產生（報告 HTML 的標題列，見 vibcore.reporting），呼叫方不可自訂。
      3. 只收結構化欄位；`headline`/`notes`/`actions` 的文字欄位落庫與回傳前
         一律 HTML 轉義（渲染路徑改走樣板引擎的 autoescape，見
         `_render_report_html` 為何刻意不共用同一份字串）。
      4. 每日發送次數上限（預設 3，環境變數 `VIB_REPORT_DAILY_LIMIT` 可調），
         超過回 429；每次成功呼叫寫入 `audit_log`。

    報告內容本身（三段式分類、涵蓋率、觀察名單）一律由
    `vibcore.reporting.collect_weekly_data` 從資料庫收集，agent 送進來的
    `actions` 只用來補強對應事項卡片的敘述——哪些事項要出現、嚴重度多少、
    走到哪個簽核階段，都不受 agent 輸入影響（見 collect.py 的 docstring）。
    `building`/`floor`/`system_name` 同時套用到計數與報告內容，兩者範圍
    一致（見 `reporting.ReportScope`）。

    SMTP 尚未設定：報告存進 `weekly_report` 並回傳結果，`delivery.sent`
    固定為 False，寄送介面已預留。
    """
    sent_today = queries.count_send_report_today(conn)
    if sent_today >= daily_limit:
        raise HTTPException(
            status_code=429,
            detail=f"已達每日寄送上限（{daily_limit} 次），請明日再試",
        )

    # 兩份 actions：`raw_actions` 給渲染層（Jinja2 autoescape 會處理），
    # `actions` 是轉義後的版本，供落庫與 API 回傳（四道卡控 #3）。
    # 差別只在轉義與否，內容相同；理由見 `_render_report_html`。
    raw_actions = [
        {
            "level": a.level,
            "title": a.display_title,
            "detail": a.detail,
            "suggestion": a.suggestion,
            "target_type": a.target_type,
            "target": a.target,
            "issue_type": a.issue_type,
        }
        for a in payload.actions
    ]
    headline = html_escape.escape(payload.headline)
    notes = html_escape.escape(payload.notes) if payload.notes else None

    def _esc(v: str | None) -> str | None:
        return html_escape.escape(v) if v else v

    actions = [
        {**a,
         "title": _esc(a["title"]), "detail": _esc(a["detail"]),
         "suggestion": _esc(a["suggestion"]),
         # `text` 保留在回傳裡，讓既有以舊契約呼叫的 agent 讀回傳時
         # 仍看得到自己送出去的欄位，不必改讀 title
         "text": _esc(a["title"])}
        for a in raw_actions
    ]

    period_start, period_end = resolve_period(payload.days, end_date=payload.end_date)
    start_dt, end_dt = _period_utc_bounds(period_start, period_end)
    summary = queries.finding_summary_for_period(
        conn, start_dt, end_dt,
        building=payload.building, floor=payload.floor, system_name=payload.system_name,
    )

    if payload.report_type == "weekly":
        iso_cal = period_end.isocalendar()
        period_label = f"{iso_cal[0]}-W{iso_cal[1]:02d}"
    else:
        period_label = period_end.isoformat()

    agent_payload = {
        "verdict": payload.verdict,
        "headline": headline,
        "actions": actions,
        "notes": notes,
        "submitted_at": jsonable(_now()),
    }
    report_html = _render_report_html(
        conn, payload, period_start, period_end,
        raw_agent_payload={
            "verdict": payload.verdict,
            "headline": payload.headline,
            "actions": raw_actions,
            "notes": payload.notes,
        },
    )

    row = queries.upsert_weekly_report(
        conn,
        report_type=payload.report_type,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
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
            "scope": {"building": payload.building, "floor": payload.floor,
                      "system_name": payload.system_name},
        },
    )

    return {
        "report_id": row["report_id"],
        "report_type": row["report_type"],
        "period_label": row["period_label"],
        "period_start": jsonable(row["period_start"]),
        "period_end": jsonable(row["period_end"]),
        "days": payload.days,
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
