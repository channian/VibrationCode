"""
render.py — 把 collect_weekly_data 的結果 + agent 的結構化評論排版成 HTML

**安全要求**：`agent_payload` 來自 LLM 的自由文字輸出，是這支模組唯一的
不信任輸入來源（`data` 已經是資料庫查出來的結構化值）。所有會被寫進
HTML 的 agent 字串一律透過 Jinja2 的 autoescape（本模組建立 Environment
時強制開啟，且全程不使用 `|safe`）過濾，`<script>`、`<b>` 這類標記會被
原樣顯示成文字而不會生效——不在這裡個別呼叫 `html.escape()`，是因為
「這個欄位有沒有轉義」若分散在十幾個字串組裝的地方各自處理，只要漏一處
就是一個注入點；讓樣板引擎統一處理，才能保證沒有漏網之魚。

**agent 內容如何併入畫面**：`agent_payload["actions"]` 只用來補強
「本週新發現／追蹤中」卡片的敘述（標題／說明／建議下一步），不會拿來
決定要顯示哪些卡片——是否顯示、嚴重度、簽核狀態一律以 `data` 為準
（見 collect.py docstring）。`actions` 用 `{target_type}:{target}:
{issue_type}` 比對到 `Finding.finding_key`；比對不到的 action 會被
忽略並記一筆 warning（agent 可能引用了已結案或不存在的事項），不會讓
整個渲染失敗。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import jinja2

from vibcore.reporting.templates import WEEKLY_REPORT_TEMPLATE

logger = logging.getLogger(__name__)

#: send_report 契約上限；超過的 action 直接捨棄而非報錯，理由見模組 docstring
_MAX_ACTIONS = 10

_LOCAL_TZ = timezone(timedelta(hours=8))

SEVERITY_LABELS = {"err": "嚴重", "warn": "需關注", "ok": "正常"}
VERDICT_LABELS = {"err": "嚴重", "warn": "需關注", "ok": "正常"}
ROLE_LABELS = {"engineer": "工程師", "supervisor": "主管", "expert": "專家", "admin": "管理員"}

_env = jinja2.Environment(
    loader=jinja2.BaseLoader(),
    autoescape=True,   # 唯一的注入防線；模組內任何地方都不可對 agent 字串用 |safe
    trim_blocks=True,
    lstrip_blocks=True,
)
_template = _env.from_string(WEEKLY_REPORT_TEMPLATE)


# ──────────────────────────────────────────────────────────
# 基礎格式化
# ──────────────────────────────────────────────────────────

def _coerce_datetime(dt: datetime | str | None) -> datetime | None:
    """
    `latest_note`／`resolved_at` 等欄位若來自 `jsonb_build_object` 組出的 JSONB
    （見 collect.py 的 `_fetch_open_findings`/`_fetch_resolved_findings` SQL），
    psycopg2 會把裡頭的 timestamptz 還原成 ISO 字串而非 datetime 物件——JSONB
    本身不保留欄位型別。這裡統一在格式化前轉回 datetime，讓呼叫端不必逐處判斷
    「這個時間欄位是不是從 JSONB 來的」。
    """
    if dt is None or isinstance(dt, datetime):
        return dt
    if isinstance(dt, str):
        return datetime.fromisoformat(dt)
    return None


def _to_local(dt: datetime | None) -> datetime | None:
    """DB 存 UTC，呈現一律轉 +8（見 db/schema.sql 開頭註記）。"""
    dt = _coerce_datetime(dt)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ)


def _fmt_date(dt: datetime | date | str | None) -> str:
    dt = _coerce_datetime(dt) if isinstance(dt, str) else dt
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        dt = _to_local(dt)
    return dt.strftime("%m/%d")


def _fmt_datetime(dt: datetime | str | None) -> str:
    local = _to_local(dt)
    if local is None:
        return "—"
    return local.strftime("%m/%d %H:%M")


def _fmt_num(v: Any) -> str:
    """數字格式化：整數不留小數點，浮點數保留至多 2 位有效小數並去掉尾端的 0。"""
    if v is None:
        return "—"
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e9:
            return str(int(v))
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def _fmt_pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _fmt_hours(hours: float) -> str:
    """把小時數轉成「N 天 M 小時」或「N 小時」的中文描述，供斷線時長顯示。"""
    hours = max(0.0, hours)
    days = int(hours // 24)
    rem = hours - days * 24
    if days > 0 and rem >= 1:
        return f"{days} 天 {int(round(rem))} 小時"
    if days > 0:
        return f"{days} 天"
    return f"{int(round(hours))} 小時"


def _humanize_key(key: str) -> str:
    return key.replace("_", " ")


def _fmt_evidence_value(v: Any) -> str:
    if isinstance(v, (int, float, Decimal)):
        return _fmt_num(v)
    if isinstance(v, dict):
        return "、".join(f"{k} {_fmt_evidence_value(val)}" for k, val in v.items())
    if isinstance(v, (list, tuple)):
        return "、".join(_fmt_evidence_value(x) for x in v)
    return str(v)


def _evidence_display(item: dict[str, Any]) -> list[tuple[str, str]]:
    """
    把 finding.evidence（規則層自訂的 JSONB）轉成 `(標籤, 數值)` 清單。

    evidence 的 key 由各規則自行決定（見 db/schema.sql rule_config 的
    params 與 RuleOutcome.evidence），這裡沒有一份跨規則的欄位標籤字典
    可查，只能做通用處理（底線轉空白）。若 evidence 為空，退回顯示
    current_value / baseline_value，確保卡片至少有一行可讀的數據佐證。
    """
    out: list[tuple[str, str]] = []
    evidence = item.get("evidence") or {}
    for k, v in evidence.items():
        out.append((_humanize_key(k), _fmt_evidence_value(v)))
    if not out and item.get("current_value") is not None:
        unit = item.get("value_unit") or ""
        out.append(("目前值", f"{_fmt_num(item['current_value'])} {unit}".strip()))
        if item.get("baseline_value") is not None:
            out.append(("基準值", f"{_fmt_num(item['baseline_value'])} {unit}".strip()))
    return out


def _period_label(period_start: date, period_end: date) -> str:
    iso_year, iso_week, _ = period_start.isocalendar()
    return f"{iso_year}-W{iso_week:02d} · {period_start:%m/%d} – {period_end:%m/%d}"


# ──────────────────────────────────────────────────────────
# agent actions 併入
# ──────────────────────────────────────────────────────────

def _index_actions(agent_payload: dict[str, Any] | None) -> dict[str, dict]:
    actions = list((agent_payload or {}).get("actions") or [])
    if len(actions) > _MAX_ACTIONS:
        logger.warning("agent_payload 傳入 %d 筆 actions，超過上限 %d，僅取前 %d 筆",
                        len(actions), _MAX_ACTIONS, _MAX_ACTIONS)
        actions = actions[:_MAX_ACTIONS]

    indexed: dict[str, dict] = {}
    for a in actions:
        if not isinstance(a, dict):
            continue
        key = f"{a.get('target_type', 'point')}:{a.get('target', '')}:{a.get('issue_type', '')}"
        indexed[key] = a
    return indexed


def _match_action(finding_key: str, actions_index: dict[str, dict], severity: str) -> dict | None:
    action = actions_index.pop(finding_key, None)
    if action is None:
        return None
    level = action.get("level")
    if level and level != severity:
        # 嚴重度一律以規則引擎的判定為準（§8.1：LLM 不做數值判斷）；
        # agent 的 level 只作為交叉核對，不覆蓋畫面上的徽章顏色。
        logger.warning("finding %s：agent 給的 level=%r 與資料庫嚴重度 %r 不一致，以資料庫為準",
                        finding_key, level, severity)
    return action


# ──────────────────────────────────────────────────────────
# 卡片呈現
# ──────────────────────────────────────────────────────────

def _present_open_finding(item: dict[str, Any], action: dict | None, kind: str) -> dict[str, Any]:
    title = item["title"]
    narrative = item.get("detail") or ""
    suggestion = ""
    if action:
        title = (action.get("title") or "").strip() or title
        narrative = (action.get("detail") or "").strip() or narrative
        suggestion = (action.get("suggestion") or "").strip()

    limit_text_parts: list[str] = []
    if item.get("interpretation_limit"):
        limit_text_parts.append(item["interpretation_limit"].strip())
    if suggestion:
        limit_text_parts.append(suggestion)
    limit_text = "　".join(limit_text_parts)

    flags = []
    if kind == "tracking" and item.get("escalated"):
        flags.append({"cls": "up", "label": "情況惡化"})
    if kind == "tracking" and item.get("is_overdue"):
        od = item.get("overdue_days")
        flags.append({"cls": "late", "label": f"逾期 {od} 天" if od else "逾期"})

    reply = None
    note = item.get("latest_note")
    if note:
        role_label = ROLE_LABELS.get(note.get("role"), "")
        who = f"{role_label}最後回覆" if role_label else "最後回覆"
        reply = {
            "who": f"{who} · {_fmt_date(note.get('created_at'))} · {note.get('author') or '—'}",
            "said": note.get("note") or "",
        }

    meta: list[dict[str, Any]] = []
    if kind == "new":
        meta.append({"label": "首次發現", "value": _fmt_date(item.get("first_seen_at"))})
        if item.get("assignee_name"):
            meta.append({"label": "指派", "value": item["assignee_name"], "plain": True})
        if item.get("reply_deadline"):
            meta.append({"label": "回覆期限", "value": _fmt_date(item["reply_deadline"])})
    else:
        meta.append({"label": "已開啟", "value": f"{item.get('days_open', 0)} 天"})
        meta.append({"label": "目前階段", "value": item.get("stage_label", ""), "plain": True})
        if item.get("days_in_stage") is not None:
            meta_entry = {"label": "停留", "value": f"{item['days_in_stage']} 天"}
            if item.get("sla_days") is not None:
                meta_entry["suffix"] = f"（SLA {item['sla_days']} 天）"
            meta.append(meta_entry)

    occurrence_suffix = f"（第 {item['occurrence_count']} 次提出）" if kind == "tracking" else ""

    return {
        "severity": item["severity"],
        "severity_label": SEVERITY_LABELS.get(item["severity"], item["severity"]),
        "device_label": item["device_label"],
        "location": item["location"],
        "flags": flags,
        "title": title,
        "occurrence_suffix": occurrence_suffix,
        "evidence": _evidence_display(item),
        "narrative": narrative,
        "limit_text": limit_text,
        "reply": reply,
        "meta": meta,
    }


def _present_resolved_finding(item: dict[str, Any]) -> dict[str, Any]:
    reply = None
    note = item.get("latest_note")
    if note:
        role_label = ROLE_LABELS.get(note.get("role"), "")
        prefix = "誤報紀錄" if item["status"] == "false_positive" else "結案紀錄"
        who = f"{prefix} · {_fmt_date(note.get('created_at'))} · {note.get('author') or '—'}"
        reply = {"who": who, "said": note.get("note") or ""}

    narrative = item.get("detail") or ""
    if not narrative and not reply and item["status"] == "auto_resolved":
        narrative = "數值已連續回到門檻內，由系統自動結案；該期間無人工處置紀錄。"

    return {
        "severity": "ok",
        "status_label": item["status_label"],
        "device_label": item["device_label"],
        "location": item["location"],
        "title": item["title"],
        "evidence": _evidence_display(item),
        "narrative": narrative,
        "reply": reply,
    }


# ──────────────────────────────────────────────────────────
# 資料品質區塊
# ──────────────────────────────────────────────────────────

def _gap_sentence(gap: dict[str, Any]) -> str:
    if gap["kind"] == "offline":
        since_label = _fmt_datetime(gap["since"])
        return f"自 {since_label} 起無資料，已連續 {_fmt_hours(gap['gap_hours'])}。本週該點所有指標未評估。"
    if gap["kind"] == "partial":
        return f"本期累計 {gap['partial_hours']} 小時資料不全，該時段指標已標記為不可信。"
    if gap["kind"] == "standby":
        idle = f"累計未運轉 {gap['idle_days']} 天" if gap.get("idle_days") is not None else "未偵測到運轉紀錄"
        return (f"備機，本週運轉 {gap['this_week_hours']} 小時，{idle}"
                f"（門檻 {gap['threshold_days']} 天）。運轉樣本不足以建立健康基準，"
                f"僅以離線與試車規則監測。")
    return ""


def _present_quality(coverage: dict[str, Any], gaps: list[dict[str, Any]]) -> dict[str, Any]:
    bar_segments = [
        {"var": "--ok", "pct": coverage["ok_ratio"] * 100},
        {"var": "--ink-3", "pct": coverage["not_running_ratio"] * 100},
        {"var": "--warn", "pct": coverage["partial_ratio"] * 100},
        {"var": "--crit", "pct": coverage["no_data_ratio"] * 100},
    ]
    legend = [
        {"var": "--ok", "label": "可分析", "pct_label": _fmt_pct(coverage["ok_ratio"])},
        {"var": "--ink-3", "label": "設備未運轉", "pct_label": _fmt_pct(coverage["not_running_ratio"])},
        {"var": "--warn", "label": "資料不全", "pct_label": _fmt_pct(coverage["partial_ratio"])},
        {"var": "--crit", "label": "感測器斷線", "pct_label": _fmt_pct(coverage["no_data_ratio"])},
    ]
    gap_items = [
        {"device_label": g["device_label"], "location": g["location"], "sentence": _gap_sentence(g)}
        for g in gaps
    ]
    return {
        "has_data": coverage["total_hours"] > 0,
        "bar_segments": bar_segments,
        "legend": legend,
        "gap_items": gap_items,
        "header_ratio_label": _fmt_pct(coverage["header_ratio"]),
    }


# ──────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────

def _infer_verdict(data: dict[str, Any]) -> str:
    stats = data["stats"]
    if stats["err_count"] > 0:
        return "err"
    has_overdue_or_escalated = any(
        f.get("is_overdue") or f.get("escalated") for f in data["tracking_findings"]
    )
    if stats["warn_count"] > 0 or has_overdue_or_escalated:
        return "warn"
    return "ok"


def _fallback_headline(data: dict[str, Any]) -> str:
    s = data["stats"]
    return (f"本週新增 {s['new_count']} 件事項，追蹤中 {s['tracking_count']} 件，"
            f"本週已解決 {s['resolved_count']} 件。")


def render_weekly_html(data: dict[str, Any], agent_payload: dict[str, Any] | None) -> str:
    """
    把 `collect_weekly_data` 的輸出與 agent 的結構化評論排版成完整 HTML。

    `agent_payload` 允許為 `None` 或缺欄位——排版邏輯必須在 agent 完全
    沒有回應（例如上游呼叫失敗）時仍能產出一份可讀的週報，只是「觀察與
    建議」一節與各卡片的敘述會退回顯示資料庫既有的 `detail`/
    `interpretation_limit`，不會整頁失敗。

    Args:
        data: `collect_weekly_data()` 的回傳值。
        agent_payload: `{"verdict","headline","actions","notes"}`，見套件 docstring。

    Returns:
        完整、樣式內嵌的 HTML 字串（可直接存檔或作為 email HTML 內文寄送）。
    """
    agent_payload = agent_payload or {}
    actions_index = _index_actions(agent_payload)

    new_findings = [
        _present_open_finding(f, _match_action(f["finding_key"], actions_index, f["severity"]), "new")
        for f in data["new_findings"]
    ]
    tracking_findings = [
        _present_open_finding(f, _match_action(f["finding_key"], actions_index, f["severity"]), "tracking")
        for f in data["tracking_findings"]
    ]
    resolved_findings = [_present_resolved_finding(f) for f in data["resolved_findings"]]

    if actions_index:
        logger.warning("agent_payload 有 %d 筆 actions 對應不到任何未結案事項，已忽略：%s",
                        len(actions_index), list(actions_index.keys()))

    verdict = agent_payload.get("verdict") or _infer_verdict(data)
    if verdict not in VERDICT_LABELS:
        logger.warning("agent_payload verdict=%r 不是合法值，退回依統計數字判定", verdict)
        verdict = _infer_verdict(data)

    headline = (agent_payload.get("headline") or "").strip() or _fallback_headline(data)

    notes_raw = (agent_payload.get("notes") or "").strip()
    notes_paragraphs = [p.strip() for p in notes_raw.split("\n\n") if p.strip()] if notes_raw else []

    s = data["stats"]
    stat_tiles = [
        {"cls": "c", "value": str(s["err_count"]), "label": "嚴重"},
        {"cls": "w", "value": str(s["warn_count"]), "label": "需關注"},
        {"cls": "", "value": str(s["tracking_count"]), "label": "追蹤中"},
        {"cls": "g", "value": str(s["resolved_count"]), "label": "本週已解決"},
        {"cls": "", "value": _fmt_pct(data["coverage"]["header_ratio"]), "label": "資料涵蓋率"},
    ]

    ctx = {
        "period_label": _period_label(data["period_start"], data["period_end"]),
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS.get(verdict, verdict),
        "headline": headline,
        "stat_tiles": stat_tiles,
        "quality": _present_quality(data["coverage"], data["coverage_gaps"]),
        "new_findings": new_findings,
        "tracking_findings": tracking_findings,
        "resolved_findings": resolved_findings,
        "notes_paragraphs": notes_paragraphs,
        "generated_at_label": _fmt_datetime(datetime.now(timezone.utc)),
    }

    logger.info("週報 HTML 渲染完成：verdict=%s 新發現=%d 追蹤中=%d 已解決=%d",
                verdict, len(new_findings), len(tracking_findings), len(resolved_findings))

    return _template.render(**ctx)
