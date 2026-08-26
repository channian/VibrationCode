"""
schemas.py — Pydantic 模型

只有 `send_report` 的請求體需要嚴格的結構驗證（四道卡控中的「只收結構化
欄位」），其餘查詢型工具的回傳直接以 dict 組裝（見 `service.py`），型別
以範例呈現於 `docs/agent_tools.md`，不在此重複定義第二份契約——避免
「Pydantic 模型」與「手動組的 dict」兩份說法彼此漂移。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: err/warn/ok 是全系統唯一合法的嚴重度詞彙（對應 finding.severity 的 CHECK 限制）
Severity = Literal["err", "warn", "ok"]


class ActionItem(BaseModel):
    """單一建議行動：等級 + 文字描述。"""

    model_config = ConfigDict(extra="forbid")

    level: Severity
    text: str = Field(min_length=1, max_length=500)


class SendReportRequest(BaseModel):
    """
    `send_report` 的請求 body。

    刻意用 `extra="forbid"`：任何不在契約內的欄位——尤其是收件人相關的
    `to`/`recipients`/`cc`/`bcc`——一律導致 422，而不是被靜默忽略。收件人
    由系統設定決定、主旨由系統產生，兩者都不開放呼叫端指定，避免這支
    API 被挪用成任意寄信的跳板（見計畫書 §六「四道卡控」）。

    只收結構化欄位（verdict/headline/actions/notes），不收 raw HTML；
    所有字串欄位在 `service.send_report()` 中一律做 HTML 轉義後才落庫。
    """

    model_config = ConfigDict(extra="forbid")

    report_type: Literal["daily", "weekly"] = "weekly"
    period_start: date
    period_end: date
    verdict: Severity
    headline: str = Field(min_length=1, max_length=200)
    actions: list[ActionItem] = Field(default_factory=list, max_length=10)
    notes: str | None = Field(default=None, max_length=4000)

    #: 選填的範圍篩選（用於計算 new_count/tracking_count/resolved_count 的範圍），
    #: 不填代表全廠彙總。與收件人無關，因此不受上述「禁止範圍外欄位」的顧慮影響。
    building: str | None = None
    floor: str | None = None
    system_name: str | None = None

    @model_validator(mode="after")
    def _check_period(self) -> "SendReportRequest":
        if self.period_end < self.period_start:
            raise ValueError("period_end 不可早於 period_start")
        return self
