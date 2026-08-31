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

#: 所有 `days` 相對窗口參數共用的上下限。下限 1 天（至少要有一天可報）；
#: 上限 365 天純粹是防呆——避免筆誤傳入天文數字造成整表掃描，不是業務
#: 規則。與 `vibcore.api.service.resolve_period()` 的預期輸入範圍一致，
#: 兩邊都要顧到：這裡擋掉超出範圍的請求（422），`resolve_period()` 本身
#: 不再重複驗證。
DAYS_MIN = 1
DAYS_MAX = 365


class ActionItem(BaseModel):
    """
    單一建議行動：等級 + 敘述，並可帶上對應到系統事項的三個定位欄位。

    `target_type`/`target`/`issue_type` 三者組成 `finding_key`
    （`{target_type}:{target}:{issue_type}`，與 `finding.finding_key`
    同一套格式），報表渲染層據此把 agent 寫的敘述掛到對應的事項卡片上。
    三者都是選填：對應不到任何未結案事項的 action 會被渲染層記一筆
    warning 後忽略，不會讓整份報告產不出來——agent 引用到已結案事項是
    可預期的情況，不該是致命錯誤。

    **嚴重度以規則引擎為準**：`level` 只作為交叉核對，與資料庫不一致時
    渲染層會記 warning 並採用資料庫的值（見 render.`_match_action`）。
    LLM 不做數值判斷，這條在計畫書 §8.1 就定案了。

    `text` 是舊版契約的欄位，保留為 `title` 的別名：本模型設了
    `extra="forbid"`，若直接移除，既有以 `{"level","text"}` 呼叫的
    agent 會開始收到 422，而 `send_report` 是每日排程的終點——那種失敗
    不會有人即時發現，只會變成「週報這幾週怎麼沒出來」。兩者擇一即可，
    同時給則以 `title` 為準。
    """

    model_config = ConfigDict(extra="forbid")

    level: Severity
    title: str | None = Field(default=None, min_length=1, max_length=200)
    text: str | None = Field(
        default=None, min_length=1, max_length=500,
        description="舊版契約欄位，等同 title；新呼叫請改用 title/detail/suggestion。",
    )
    detail: str | None = Field(default=None, max_length=1000)
    suggestion: str | None = Field(default=None, max_length=500)

    target_type: Literal["device", "point", "global"] | None = None
    target: str | None = Field(default=None, max_length=200)
    issue_type: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _require_some_text(self) -> "ActionItem":
        if not (self.title or self.text):
            raise ValueError("action 需要 title（或舊版的 text）")
        return self

    @property
    def display_title(self) -> str:
        """渲染與落庫一律用這個值，呼叫端不必各自處理 title/text 的優先序。"""
        return self.title or self.text or ""


class SendReportRequest(BaseModel):
    """
    `send_report` 的請求 body。

    刻意用 `extra="forbid"`：任何不在契約內的欄位——尤其是收件人相關的
    `to`/`recipients`/`cc`/`bcc`——一律導致 422，而不是被靜默忽略。收件人
    由系統設定決定、主旨由系統產生，兩者都不開放呼叫端指定，避免這支
    API 被挪用成任意寄信的跳板（見計畫書 §六「四道卡控」）。

    只收結構化欄位（verdict/headline/actions/notes），不收 raw HTML；
    所有字串欄位在 `service.send_report()` 中一律做 HTML 轉義後才落庫。

    **時間窗改為 `days`（相對天數），不再收 `period_start`/`period_end`**：
    no-code 平台上的 agent 是靠 LLM 自己組參數，要它「算出上週一是幾號」
    這種日期運算，算錯了不會報任何錯，只會安靜產出涵蓋錯誤區間的報告——
    這是真實發生過的失效模式，而不是假設性的擔憂。相對窗口交給系統側
    的 `service.resolve_period()` 統一換算，agent 只需要給一個整數，
    與週報/日報查詢工具（`get_weekly_report_data`）同一套語彙，也對齊
    HVM 既有的 `days` 慣例。為了保留補產歷史報告的能力，額外開放選填的
    `end_date`（不給則為系統當下日期）。
    """

    model_config = ConfigDict(extra="forbid")

    report_type: Literal["daily", "weekly"] = "weekly"
    days: int = Field(
        default=7, ge=DAYS_MIN, le=DAYS_MAX,
        description="相對窗口天數，預設 7（週報）；日報請傳 1。窗口為 [end_date - days + 1, end_date]，以日曆日切齊。",
    )
    end_date: date | None = Field(
        default=None,
        description="窗口結束日（含），不填則為系統當下日期（UTC）。僅用於補產歷史報告，一般呼叫不需要帶。",
    )
    verdict: Severity
    headline: str = Field(min_length=1, max_length=200)
    actions: list[ActionItem] = Field(default_factory=list, max_length=10)
    notes: str | None = Field(default=None, max_length=4000)

    #: 選填的範圍篩選（用於計算 new_count/tracking_count/resolved_count 的範圍），
    #: 不填代表全廠彙總。與收件人無關，因此不受上述「禁止範圍外欄位」的顧慮影響。
    building: str | None = None
    floor: str | None = None
    system_name: str | None = None
