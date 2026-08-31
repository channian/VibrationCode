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


# ──────────────────────────────────────────────────────────
# 規則分類：決定「這件事該由誰處理」
# ──────────────────────────────────────────────────────────
#
# 13 條規則講的其實是兩種不同性質的問題，處置者不同：
#
#   - `DATA_AVAILABILITY`（資料可用性）：問題出在「量測系統有沒有把
#     資料收上來、量得準不準」——感測器離線、資料品質異常、量程配置
#     可能有誤、安裝／量測有效性存疑。這類要處置的人是 IT／儀電，
#     不是叫設備工程師去判斷轉子有沒有異常。
#   - `EQUIPMENT`（設備狀態）：問題出在「設備本身的振動特徵是否在
#     變化」——速度偏高、衝擊上升、趨勢劣化等。這類要處置的人是設備
#     工程師，需要判斷要不要安排巡檢或停機檢查。
#
# 分類分錯的後果：這兩類的「觸發密度」是拿去做完全不同決策的依據——
# 設備狀態類密度用來校準振動門檻、估算工程師要盯多少台設備；資料可用性
# 類密度反映的是佈建品質（感測器安裝、通訊、量程設定），該去改善的是
# 硬體與網路，不是振動門檻。若混在一起算，佔比可能過半的資料可用性
# 問題會把「全廠平均密度」撐高，讓人誤以為設備真的普遍不穩定，因而
# 誤判要加派工程師人力或把振動門檻調鬆——實際上只是感測器常斷線。
class RuleCategory:
    """規則分類代碼（字串常數，供比對用）。"""
    DATA_AVAILABILITY = 'data_availability'
    EQUIPMENT = 'equipment'


#: rule_code → 分類。新增規則時記得同步在這裡歸類，否則會落入
#: `rule_category()` 的「未知代碼」警告分支，統計數字會不準。
RULE_CATEGORY: dict[str, str] = {
    # 資料可用性：處置者是 IT／儀電，回答「資料收得到、收得準嗎」
    'SENSOR_OFFLINE': RuleCategory.DATA_AVAILABILITY,
    'DATA_QUALITY': RuleCategory.DATA_AVAILABILITY,
    # SENSOR_SATURATION 同時可能代表真實的強烈振動（訊號真的頂到滿
    # 刻度），但第一步處置永遠是先確認量程配置對不對——量程設錯，後面
    # 所有峰值類指標都會失真，無從判斷是真異常還是量程問題，這一步是
    # 資料可用性性質的處置，故歸此類而非設備狀態。
    'SENSOR_SATURATION': RuleCategory.DATA_AVAILABILITY,
    'ISO_CLASS_SUSPECT': RuleCategory.DATA_AVAILABILITY,
    # 軸能量分佈排列跳變，代表疑似感測器重貼或更換，是安裝／量測有效性
    # 問題——即使設備振動真的變了，量測基準也已經因為感測器位置改變而
    # 失效，要先確認安裝狀態才能重新比對，不能直接拿新舊資料判定設備
    # 劣化，故歸資料可用性而非設備狀態。
    'ORIENTATION_CHANGE': RuleCategory.DATA_AVAILABILITY,

    # 設備狀態：處置者是設備工程師，回答「設備振動特徵是否在變化」
    'ISO_ZONE': RuleCategory.EQUIPMENT,
    'VEL_HIGH': RuleCategory.EQUIPMENT,
    'IMPACT_RISE': RuleCategory.EQUIPMENT,
    'DEGRADE_TREND': RuleCategory.EQUIPMENT,
    'SPECTRAL_SHIFT': RuleCategory.EQUIPMENT,
    'AXIS_SHIFT': RuleCategory.EQUIPMENT,
    'STEP_CHANGE': RuleCategory.EQUIPMENT,
    'STANDBY_NO_RUNTIME': RuleCategory.EQUIPMENT,
    # 溫度異常的處置者是設備工程師（要不要安排深度量測），不是 IT。
    # 雖然它也可能來自感測器本身的問題，但那屬於判讀時的保留，
    # 不改變「先由設備端看」這個分流結果。
    'TEMP_RISE': RuleCategory.EQUIPMENT,
}


def rule_category(rule_code: str) -> str:
    """
    查詢規則分類。

    未知代碼回傳 `equipment` 並記警告，而不是拋例外——回測與報表不該
    因為一條新規則忘了歸類就整批中斷，但要留下痕跡讓人回頭補上；預設
    歸設備狀態類是刻意選擇「寧可誤令工程師多看一眼，也不要把它算進
    資料可用性、讓人誤以為是佈建問題而漏看」。
    """
    cat = RULE_CATEGORY.get(rule_code)
    if cat is None:
        logger.warning(f"規則 {rule_code} 未列入 RULE_CATEGORY 分類表，暫歸設備狀態類")
        return RuleCategory.EQUIPMENT
    return cat


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
        # 留存觸發當下的門檻，之後才有辦法回溯重算「若門檻是 X 會剩幾件」
        trigger_params=dict(ctx.params or {}),
        interpretation_limit=outcome.interpretation_limit,
        last_seen_at=ctx.now,
        first_seen_at=ctx.now,
        stage_entered_at=ctx.now,
        source='rule_engine',
    )
