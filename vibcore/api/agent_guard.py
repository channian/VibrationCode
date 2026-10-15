"""
agent_guard.py — Agent 送回的文字寫進資料庫之前，伺服器端的把關

Agent 平台是 no-code 的，prompt 只是請求，不是機制。本模組把「請求」變成「檢查」：

    1. 診斷性用語（`diagnostic_violations`）——Agent 的每個文字欄位都過一次與規則文字同一套的
       `vibcore.rules.guardrail.check_text`。有違規整份退回（HTTP 422），不落庫。
    2. 總評下限（`apply_verdict_floor`）——總評不得低於規則引擎依資料庫推算的結果。
       Agent 把有嚴重事項的一期總評為 ok 時，系統改用推算值並留下紀錄，不是照單全收。
    3. 數字白名單（`unverified_tokens`）——Agent 文字裡的每個數字、日期、時間，都必須出現在
       它能查到的本期事實裡（`get_weekly_report_data`、`get_open_findings`、
       `get_vibration_thresholds` 三支工具的回傳）。Agent 不做數值判斷，也不改寫數字。

數字白名單有三種模式（環境變數 `VIB_GUARDRAIL_NUMBERS`）：

    off      不檢查
    report   （預設）檢查並把未能核對的數字回報給呼叫端、記進稽核，但不退回。
             Agent 測試期用：先量出誤退率，再決定要不要強制。
    enforce  有任何未核對的數字就整份退回

預設不是 enforce 的理由：白名單怎麼比對（四捨五入、百分比、日期格式）會決定誤退率，
而 Agent 實際會怎麼寫，只有跑過才知道。強制模式在量到可接受的誤退率之前不應開啟。

**已知的範圍限制**（不是疏漏，見認證草稿〈已知限制〉）：
  · 中文數字（「六成」「三台」）不檢查——硬擋會誤傷非數量用法
  · 只檢查「有沒有出現在事實裡」，不檢查「引用得對不對」（把 A 設備的數字寫到 B 設備上）
  · Agent 若用了上述三支以外的工具，那些工具的數字不在白名單內，會被列為未核對
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from vibcore.rules.guardrail import check_text

VERDICT_RANK = {"ok": 0, "warn": 1, "err": 2}

NUMBER_MODES = ("off", "report", "enforce")
DEFAULT_NUMBER_MODE = "report"


def number_mode() -> str:
    mode = os.environ.get("VIB_GUARDRAIL_NUMBERS", DEFAULT_NUMBER_MODE).strip().lower()
    return mode if mode in NUMBER_MODES else DEFAULT_NUMBER_MODE


# ──────────────────────────────────────────────────────────
# 取出 Agent 的文字欄位
# ──────────────────────────────────────────────────────────

def agent_texts(payload) -> list[tuple[str, str]]:
    """`SendReportRequest` 裡所有會進報告的自由文字，(欄位名, 內容)。"""
    out: list[tuple[str, str]] = [("headline", payload.headline)]
    if payload.notes:
        out.append(("notes", payload.notes))
    for i, a in enumerate(payload.actions):
        for fld in ("title", "text", "detail", "suggestion"):
            v = getattr(a, fld, None)
            if v:
                out.append((f"actions[{i}].{fld}", v))
    return out


# ──────────────────────────────────────────────────────────
# 1. 診斷性用語
# ──────────────────────────────────────────────────────────

def diagnostic_violations(texts: Iterable[tuple[str, str]]) -> list[str]:
    return [f"{fld}: {p}" for fld, text in texts for p in check_text(text)]


# ──────────────────────────────────────────────────────────
# 2. 總評下限
# ──────────────────────────────────────────────────────────

def apply_verdict_floor(submitted: str, inferred: str) -> tuple[str, bool]:
    """回傳 (採用的總評, 是否被調高)。Agent 可以比推算值更嚴，不可以更鬆。"""
    if VERDICT_RANK.get(submitted, 0) >= VERDICT_RANK.get(inferred, 0):
        return submitted, False
    return inferred, True


# ──────────────────────────────────────────────────────────
# 3. 數字／日期／時間白名單
# ──────────────────────────────────────────────────────────

#: 設備代號、量測位置、週次、ISO 標準編號——裡面的數字是名稱的一部分，不是數量
_IDENT = re.compile(
    r"ISO\s?\d{4,5}(?:-\d)?"
    r"|(?<![A-Za-z])[A-Z]{1,4}[\s-]?\d{1,3}(?:-\d{1,3}){0,3}(?:_[A-Z]\d)?"
)
_DATETIME = re.compile(
    r"(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:[T ](\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}(?::?\d{2})?)?)?"
)
_MD_SLASH = re.compile(r"(?<![0-9/.])(\d{1,2})/(\d{1,2})(?![0-9/.])")
_MD_CJK = re.compile(r"(\d{1,2})\s?月\s?(\d{1,2})\s?日")
_TIME = re.compile(r"(?<![0-9:])(\d{1,2}):(\d{2})(?![0-9:])")
_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])[-+−]?\d[\d,]*(?:\.\d+)?")
# 條列編號（行首的「1.」「2、」，或句中前後有空白的「2.」）不是數量
_LIST_BULLET = re.compile(r"(?m)^\s*\d{1,2}\s*[.)、．]\s*|(?<=\s)\d{1,2}[.)、．](?=\s)")


def normalize_number(raw: str) -> str | None:
    """'207.0' → '207'、'4.60' → '4.6'、'1,234' → '1234'；符號不比（-3.9σ 與 3.9σ 視為同一個數）。"""
    s = raw.replace(",", "").lstrip("+-−")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if d == d.to_integral_value():
        return format(d.quantize(Decimal(1)), "f")
    return format(d.normalize(), "f")


@dataclass
class Tokens:
    numbers: set[str] = field(default_factory=set)
    dates: set[tuple[int, int]] = field(default_factory=set)      # (月, 日)
    times: set[str] = field(default_factory=set)                  # 'HH:MM'


def _scan(text: str, out: Tokens, *, with_bullets: bool = False) -> None:
    """從一段文字抽出日期、時間、數字，加進 out。先抽日期時間、再遮掉代號，剩下的才是數量。"""
    s = unicodedata.normalize("NFKC", text)          # 全形數字先轉半形，不能靠全形字繞過比對
    if with_bullets:
        s = _LIST_BULLET.sub("", s)

    def take_datetime(m: re.Match) -> str:
        out.dates.add((int(m.group(2)), int(m.group(3))))
        if m.group(4) is not None:
            out.times.add(f"{int(m.group(4)):02d}:{m.group(5)}")
        return " "

    s = _DATETIME.sub(take_datetime, s)
    s = _MD_CJK.sub(lambda m: (out.dates.add((int(m.group(1)), int(m.group(2)))), " ")[1], s)
    s = _MD_SLASH.sub(lambda m: (out.dates.add((int(m.group(1)), int(m.group(2)))), " ")[1]
                      if 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31 else m.group(0), s)
    s = _TIME.sub(lambda m: (out.times.add(f"{int(m.group(1)):02d}:{m.group(2)}"), " ")[1], s)
    s = _IDENT.sub(" ", s)
    for m in _NUMBER.finditer(s):
        n = normalize_number(m.group(0))
        if n is not None:
            out.numbers.add(n)


def _add_float_forms(n: float | Decimal, key: str | None, out: Tokens) -> None:
    d = Decimal(str(n))
    if not d.is_finite():
        return
    norm = normalize_number(str(d))
    if norm is not None:
        out.numbers.add(norm)
    # 浮點運算算出來、帶一長串小數的值（0.7312345678）本來就不是給人原樣照抄的，
    # 收進四捨五入到 0～3 位的寫法；資料庫直接讀出的 NUMERIC 沒有這個問題。
    if d.as_tuple().exponent < -6:
        for places in (0, 1, 2, 3):
            q = normalize_number(str(round(d, places)))
            if q is not None:
                out.numbers.add(q)
    # 比例欄位（0～1）Agent 會寫成百分比
    if key and ("ratio" in key or "pct" in key) and Decimal(0) <= d <= Decimal(1):
        for places in (0, 1):
            q = normalize_number(str(round(d * 100, places)))
            if q is not None:
                out.numbers.add(q)


def facts_tokens(*payloads: Any) -> Tokens:
    """走過 Agent 查得到的工具回傳，收集所有出現過的數字、日期、時間。"""
    out = Tokens()

    def walk(obj: Any, key: str | None = None) -> None:
        if isinstance(obj, bool) or obj is None:
            return
        if isinstance(obj, (int, float, Decimal)):
            _add_float_forms(obj, key, out)
        elif isinstance(obj, str):
            _scan(obj, out)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k))
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                walk(v, key)

    for p in payloads:
        walk(p)
    return out


def unverified_tokens(texts: Iterable[tuple[str, str]], facts: Tokens) -> list[str]:
    """Agent 文字裡找不到對應事實的數字、日期、時間，每一項一句說明（同欄位同內容只列一次）。"""
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for fld, text in texts:
        used = Tokens()
        _scan(text, used, with_bullets=True)
        for n in sorted(used.numbers - facts.numbers, key=lambda x: (len(x), x)):
            if (fld, n) not in seen:
                seen.add((fld, n))
                problems.append(f"{fld}: 數字 {n} 不在本期事實內")
        for md in sorted(used.dates - facts.dates):
            tag = f"{md[0]}/{md[1]}"
            if (fld, tag) not in seen:
                seen.add((fld, tag))
                problems.append(f"{fld}: 日期 {tag} 不在本期事實內")
        for t in sorted(used.times - facts.times):
            if (fld, t) not in seen:
                seen.add((fld, t))
                problems.append(f"{fld}: 時間 {t} 不在本期事實內")
    return problems
