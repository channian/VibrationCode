"""
engine.py — 規則註冊與執行

**這個檔案是規則層的整合點。** 各規則實作只需用 `@register` 掛上來，
引擎負責挑出啟用中的規則、餵入 `RuleContext`、收集 `RuleOutcome`。

兩個刻意的設計：

1. **單一規則出錯不會拖垮整批。** 規則跑在每日排程裡，一條規則因為
   某台設備的資料形狀特殊而拋例外時，其餘 120 個量測點的判定不應該
   跟著消失——那會變成「當天完全沒有告警」，比少一條規則危險得多。

2. **`interpretation_limit` 缺漏會被擋下。** 本系統定位為篩選預警而非
   診斷，Agent 需要知道每份證據能解讀到什麼程度才不會寫出無法支撐的
   結論。這個欄位是護欄的落地點，不能靠自律。
"""

from __future__ import annotations

import logging
from typing import Callable

from vibcore.types import Finding, RuleContext, RuleOutcome

logger = logging.getLogger(__name__)

#: 規則函式簽章：吃 RuleContext，回傳 RuleOutcome
RuleFunc = Callable[[RuleContext], RuleOutcome]

#: rule_code → 規則函式
REGISTRY: dict[str, RuleFunc] = {}


def register(rule_code: str) -> Callable[[RuleFunc], RuleFunc]:
    """
    把規則函式掛進註冊表。

        @register('ISO_ZONE')
        def iso_zone(ctx: RuleContext) -> RuleOutcome:
            ...

    重複註冊同一個 rule_code 會覆蓋並發出警告——通常代表複製貼上時
    忘了改代碼，靜默覆蓋會讓其中一條規則永遠不執行。
    """
    def deco(fn: RuleFunc) -> RuleFunc:
        if rule_code in REGISTRY:
            logger.warning(f"規則 {rule_code} 重複註冊，{REGISTRY[rule_code].__name__} "
                           f"將被 {fn.__name__} 覆蓋")
        REGISTRY[rule_code] = fn
        return fn
    return deco


def evaluate_all(ctx: RuleContext,
                 rule_configs: dict,
                 rule_codes: list[str] | None = None) -> list[RuleOutcome]:
    """
    對單一量測點執行所有啟用中的規則。

    Args:
        ctx: 該量測點的完整判定上下文
        rule_configs: rule_code → 設定物件（需有 `is_active` 與 `params`）
        rule_codes: 只跑指定的規則；None 表示全部

    Returns:
        有觸發的 RuleOutcome 清單（未觸發者不回傳）
    """
    codes = rule_codes if rule_codes is not None else list(REGISTRY)
    outcomes: list[RuleOutcome] = []

    for code in codes:
        fn = REGISTRY.get(code)
        if fn is None:
            logger.debug(f"規則 {code} 尚未實作，略過")
            continue

        cfg = rule_configs.get(code)
        if cfg is not None and not getattr(cfg, 'is_active', True):
            continue

        # 每條規則拿到自己的參數；沒有設定時給空 dict 讓規則自行取預設
        ctx.params = dict(getattr(cfg, 'params', {}) or {})

        try:
            outcome = fn(ctx)
        except Exception as e:
            # 見模組說明：單一規則失敗不得影響其餘規則與其餘設備
            logger.error(f"規則 {code} 於 {ctx.device.device_id}/{ctx.position} "
                         f"執行失敗，已略過：{e}", exc_info=True)
            continue

        if outcome is None or not outcome.triggered:
            continue

        if not outcome.interpretation_limit:
            logger.warning(f"規則 {code} 觸發但未填 interpretation_limit——"
                           f"Agent 將無從得知這份證據的解讀邊界，請補上")

        outcomes.append(outcome)

    return outcomes


def outcome_to_finding(outcome: RuleOutcome, ctx: RuleContext) -> Finding:
    """把規則判定結果轉成可寫入資料庫的 Finding。"""
    target = (ctx.device.device_id if outcome.target_type == 'device'
              else f'{ctx.device.device_id}_{ctx.position}')

    return Finding(
        finding_key=Finding.make_key(outcome.target_type, target, outcome.issue_type),
        device_id=ctx.device.device_id,
        point_id=ctx.point_id if outcome.target_type == 'point' else None,
        target_type=outcome.target_type,
        target=target,
        issue_type=outcome.issue_type,
        family=outcome.family,
        rule_code=outcome.rule_code,
        title=outcome.title,
        detail=outcome.detail,
        severity=outcome.severity,
        peak_severity=outcome.severity,
        baseline_value=outcome.baseline_value,
        current_value=outcome.current_value,
        value_unit=outcome.value_unit,
        evidence=outcome.evidence,
        interpretation_limit=outcome.interpretation_limit,
        last_seen_at=ctx.now,
        first_seen_at=ctx.now,
        stage_entered_at=ctx.now,
        source='rule_engine',
    )
