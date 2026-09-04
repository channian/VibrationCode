"""
temp_rules.py — 溫度型規則（Phase 1 規則集之八）

實作 `TEMP_RISE`：`tempAVG` 相對基準期持續上升。

## 為什麼溫度值得單獨開一條規則

溫度是聚合表裡**唯一與振動獨立**的物理通道（見 `vibcore/config.py` 的
`AGG_SPEC` 註解與 `docs/DATA_CONTRACT.md` §3.1）。「溫度升高但振動持平」
與「溫度與振動一起升高」對現場決定要不要派人做深度量測，價值完全不同：
前者更像是負載或環境因素，後者才是需要提高優先度盯的組合。指出這個對比
本身**不需要推論成因**，因此不違反本系統「篩選而非診斷」的定位——這正是
本規則存在的理由，也是它與其他規則的差異所在：其他規則各自守著單一或
少數幾個振動指標，本規則刻意把同期振動指標一併攤開，讓 agent 自己看到
「有沒有跟著動」，而不是替 agent 下結論。

## 三個判定紀律（與 metric_rules.py / event_rules.py 一致）

1. **只用 `data_status == 'ok'` 的資料**——透過 `ctx.analyzable()`。
2. **持續性要求**——溫度是慢變量，單筆突升多半是量測雜訊或環境變化
   （例如陽光直射、空調排風口變化），不代表設備本身狀態改變。做法比照
   `event_rules.py` 的 `orientation_change`：要求連續 `consecutive_readings`
   筆可信資料都超過門檻，且取這段窗口中**最小**的那個 σ 值來判定觸發
   （全部都要超標才算數），呈現時也用這個最保守的數字，不用尖峰值誇大。
3. **`interpretation_limit` 誠實列出限制**，尤其是溫度來源的不確定性
   （見下方 `temp_rise` docstring）。

## 規則參數（`ctx.params`，由呼叫端 seed，此檔不內建 DB 預設值表）

- `sigma`（建議預設 2.5）：`temp_avg` 相對基準標準差的觸發門檻。與
  `IMPACT_RISE` 的 `crest_sigma` 同量級，而非比照 `VEL_HIGH`
  的 3.0——因為本規則另外用「連續多筆」把關，兩道防線一起收斂假警報，
  單一 σ 門檻不需要拉到最高。
- `consecutive_readings`（建議預設 3）：需連續幾筆 `ok` 資料都超標才觸發，
  與 `ORIENTATION_CHANGE` 的同名參數用途一致（`vibcore/rules/event_rules.py`）。
- `vibration_co_rise_sigma`（建議預設 1.0）：純粹是**敘述用**門檻，不影響
  是否觸發——同期 velRMS/accRMS 的 σ 若落在 `±vibration_co_rise_sigma`
  內，`detail` 措辭寫成「維持在基準附近」，超過則寫成「同步偏離基準」，
  讓文字用詞跟實際數字方向一致。無論落在哪一邊，`evidence` 都會把兩個
  指標的實際數值與 σ 一併寫出，agent 不必依賴這句話也能自己判斷。

## 尚未歸類的分類表

`vibcore/rules/engine.py` 的 `RULE_CATEGORY` 尚未收錄 `TEMP_RISE`（本任務
明確禁止修改 `engine.py`）。`rule_category()` 對未收錄代碼會記警告並暫歸
`equipment` 類——這是該函式本來就設計好的容錯行為，不是本檔的臨時解法；
待其他 agent 或後續任務把 `TEMP_RISE` 排進分類表即可，不影響規則本身的
判定邏輯。
"""

from __future__ import annotations

import logging

import pandas as pd

from vibcore.rules.engine import register
from vibcore.types import BaselineStats, MetricStats, RuleContext, RuleOutcome

logger = logging.getLogger(__name__)

_RULE_CODE = 'TEMP_RISE'
_ISSUE_TYPE = 'temp_rise'
_FAMILY = 'monotonic'

#: 同期振動比對用的指標與顯示名稱。刻意只挑 RMS 類穩健指標，不牽涉
#: crest/kurt 這類衝擊型指標——本規則要回答的是「振動水位有沒有跟著
#: 溫度動」，不是「衝擊性有沒有變化」，兩者是不同規則的職責。
_VIBRATION_METRICS: tuple[tuple[str, str], ...] = (
    ('vel_rms', 'velRMS'),
    ('acc_rms', 'accRMS'),
)


def _sorted_ok(ctx: RuleContext) -> pd.DataFrame:
    """`ctx.analyzable()` 依 `ts_hour` 排序，方便取尾端的「最近連續 N 筆」。"""
    ok = ctx.analyzable()
    if ok.empty or 'ts_hour' not in ok.columns:
        return ok
    return ok.sort_values('ts_hour')


def _metric_current_and_sigma(
    row: pd.Series, stat: MetricStats | None, metric: str,
) -> tuple[float | None, float | None]:
    """
    取某一列 `metric` 的數值與相對基準的 σ。

    基準缺該指標統計量、欄位不存在、或數值缺漏，一律回傳 (None, None)
    而不是拋錯或硬湊——同期振動比對是本規則的加分資訊，不是觸發條件，
    缺了就照實留白，不能因此讓 `TEMP_RISE` 本身無法判定。
    """
    if stat is None or metric not in row.index:
        return None, None
    val = pd.to_numeric(pd.Series([row[metric]]), errors='coerce').iloc[0]
    if pd.isna(val):
        return None, None
    val = float(val)
    return val, stat.sigma_of(val)


def _vibration_evidence(row: pd.Series, baseline: BaselineStats) -> dict:
    """組出同期振動指標的 evidence 區塊：{metric: {'value':.., 'sigma':..}}。"""
    out: dict = {}
    for metric, _label in _VIBRATION_METRICS:
        stat = baseline.stats.get(metric)
        val, sigma = _metric_current_and_sigma(row, stat, metric)
        out[metric] = {
            'value': val,
            'sigma': round(sigma, 2) if sigma is not None else None,
        }
    return out


def _vibration_phrase(vib_evidence: dict, co_rise_th: float) -> str:
    """
    把 `_vibration_evidence` 轉成給 `detail` 用的中文描述，只陳述數字方向，
    不推論成因（例如絕不寫「振動也開始劣化」這類判斷語氣）。
    """
    parts = []
    for metric, label in _VIBRATION_METRICS:
        info = vib_evidence.get(metric, {})
        sigma = info.get('sigma')
        value = info.get('value')
        if sigma is None or value is None:
            parts.append(f'{label} 無可用基準對照')
            continue
        if abs(sigma) < co_rise_th:
            parts.append(f'{label} 維持在基準 ±{co_rise_th:.1f}σ 內（{sigma:+.1f}σ）')
        else:
            parts.append(f'{label} 亦偏離基準 {sigma:+.1f}σ')
    return '，'.join(parts)


@register('TEMP_RISE')
def temp_rise(ctx: RuleContext) -> RuleOutcome:
    """
    `tempAVG` 相對基準期連續上升超過 `sigma` 個標準差。

    **判定邏輯**：
    1. 取 `ctx.analyzable()`（`data_status == 'ok'`）依時間排序的最後
       `consecutive_readings` 筆，任一筆缺值即視為「連續性不足」不觸發
       （溫度資料本身就不密集，缺一筆代表這段期間的證據並不連續）。
    2. 這幾筆相對基準 `temp_avg` 統計量的 σ 值中，取**最小值**與 `sigma`
       門檻比較——要求全部都超標，而非平均或取最大值，理由同
       `orientation_change`：避免單筆尖峰把整組窗口拉過門檻。
    3. 基準期沒有 `temp_avg` 統計量（多半是尚未回填溫度欄位的舊基準，或
       該量測點根本沒有溫度感測）時安靜地不觸發、不拋錯——這是資料契約
       的正常情況，不是異常。

    **同期振動比對**：只用最新一筆（窗口尾端）的 `vel_rms`／`acc_rms` 相對
    基準的 σ，寫進 `evidence['vibration']` 並反映在 `detail` 文字裡。這裡
    刻意不對「振動也偏離」設觸發條件——它只是佐證溫度變化的性質，不是本
    規則要不要觸發的判準，避免規則之間互相耦合出難以追溯的邏輯。

    Args:
        ctx: 規則判定上下文，用到 `ctx.baseline`、`ctx.analyzable()`、
             `ctx.params`。

    Returns:
        RuleOutcome：觸發時 `severity='warn'`，`family='monotonic'`
        （持續性緩慢變化，與 `AXIS_SHIFT`／`DEGRADE_TREND` 同類）。
    """
    if ctx.baseline is None:
        logger.debug(f"TEMP_RISE：point={ctx.point_id} 尚無基準期，無法判定")
        return RuleOutcome.no_trigger(_RULE_CODE, _ISSUE_TYPE, _FAMILY)

    temp_stat = ctx.baseline.stats.get('temp_avg')
    if temp_stat is None:
        # 舊基準沒有溫度欄位，或該點位沒有溫度感測——安靜跳過，不是異常。
        logger.debug(f"TEMP_RISE：point={ctx.point_id} 基準期缺少 temp_avg 統計量，不判定")
        return RuleOutcome.no_trigger(_RULE_CODE, _ISSUE_TYPE, _FAMILY)

    sigma_th = float(ctx.params.get('sigma', 2.5))
    consecutive = max(1, int(ctx.params.get('consecutive_readings', 3)))
    co_rise_th = float(ctx.params.get('vibration_co_rise_sigma', 1.0))

    ok = _sorted_ok(ctx)
    if ok.empty or 'temp_avg' not in ok.columns:
        logger.debug(f"TEMP_RISE：point={ctx.point_id} 無可用（ok）的 temp_avg 資料")
        return RuleOutcome.no_trigger(_RULE_CODE, _ISSUE_TYPE, _FAMILY)

    tail = ok.tail(consecutive)
    if len(tail) < consecutive:
        logger.debug(f"TEMP_RISE：point={ctx.point_id} 可信資料僅 {len(tail)} 筆，"
                     f"不足連續 {consecutive} 筆的持續性要求")
        return RuleOutcome.no_trigger(_RULE_CODE, _ISSUE_TYPE, _FAMILY)

    temp_col = pd.to_numeric(tail['temp_avg'], errors='coerce')
    if temp_col.isna().any():
        # 窗口內有缺值，代表這段期間並非「連續」拿到溫度讀數，不構成持續性證據。
        logger.debug(f"TEMP_RISE：point={ctx.point_id} 近 {consecutive} 筆窗口內 temp_avg 有缺值，不判定")
        return RuleOutcome.no_trigger(_RULE_CODE, _ISSUE_TYPE, _FAMILY)

    sigmas = [temp_stat.sigma_of(float(v)) for v in temp_col]
    min_sigma = min(sigmas)
    triggered = min_sigma >= sigma_th
    if not triggered:
        return RuleOutcome.no_trigger(_RULE_CODE, _ISSUE_TYPE, _FAMILY)

    latest_row = tail.iloc[-1]
    current_temp = float(temp_col.iloc[-1])
    latest_sigma = sigmas[-1]

    vib_evidence = _vibration_evidence(latest_row, ctx.baseline)
    vib_phrase = _vibration_phrase(vib_evidence, co_rise_th)

    return RuleOutcome(
        triggered=True,
        rule_code=_RULE_CODE,
        issue_type=_ISSUE_TYPE,
        family=_FAMILY,
        severity='warn',
        title='溫度相對基準持續上升',
        detail=(f'最近連續 {consecutive} 筆可信資料的 tempAVG 皆高於基準期中位數 '
                f'{temp_stat.median:.1f}°C 達 {sigma_th:.1f}σ 以上（窗口內最小 {min_sigma:+.1f}σ，'
                f'最新一筆 {current_temp:.1f}°C，{latest_sigma:+.1f}σ），'
                f'同期 {vib_phrase}。'),
        interpretation_limit=(
            '本量測之溫度可能為感測器內部（晶片）溫度而非軸承座實際溫度——MEMS 加速度計'
            '通常回報的是晶片溫度，兩者是否有穩定對應關係尚待確認（詳見 '
            'docs/DATA_CONTRACT.md §3.1）。因此本判定僅能反映溫度相對該量測點自身歷史'
            '基準的相對趨勢，不可作為絕對溫度門檻使用；溫度上升本身也無法區分是設備發熱、'
            '環境溫度變化、或感測器本身狀態改變所致。同期振動指標僅供對照參考，'
            '用以判斷是否為振動與溫度同時變化的組合，不構成成因判定，如需確認建議安排複測'
            '或現場檢查。'
        ),
        current_value=current_temp,
        baseline_value=temp_stat.median,
        value_unit='°C',
        evidence={
            'sigma': round(latest_sigma, 2),
            'sigma_threshold': sigma_th,
            'min_sigma_in_window': round(min_sigma, 2),
            'consecutive_readings': consecutive,
            'baseline_std': temp_stat.std,
            'baseline_n': temp_stat.n,
            'vibration': vib_evidence,
            'vibration_co_rise_sigma_threshold': co_rise_th,
        },
    )
