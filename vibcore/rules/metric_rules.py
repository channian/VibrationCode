"""
metric_rules.py — 指標型規則（Phase 1 規則集之七）

實作 `ISO_ZONE` / `VEL_HIGH` / `IMPACT_RISE` / `DEGRADE_TREND` /
`SPECTRAL_SHIFT` / `AXIS_SHIFT` / `STEP_CHANGE`，共用一個原則：**直接呼叫
`vibcore/metrics/` 底下已驗證的真實模組**（`iso.evaluate_iso`、
`trend.compute_trend`、`deviation.fit_deviation_model` /
`evaluate_deviation`），不在這裡重新發明一套簡化版統計邏輯——這樣規則層
與指標層對同一個數字的計算方式永遠一致，agent 引用時不會出現「同一個
指標、兩個地方算出不同結果」的矛盾（`validate/rules_stub.py` 的 stub
版本正是因為當時這些模組還沒寫出來才需要內建簡化版，現在不需要了）。

貫穿全部七條規則的三個硬性要求（見任務說明 / PLAN §8.2）：

1. **只用 `data_status == 'ok'` 的資料**——一律透過 `ctx.analyzable()`
   或轉交給已經自行做這件事的指標模組（`iso`/`trend`/`deviation`）。
2. **`interpretation_limit` 一定要填，且誠實。** 本系統是篩選預警，不是
   診斷；每條規則的 `interpretation_limit` 明確講清楚這份證據能撐到
   什麼程度，不多說一句。
3. **title / detail / interpretation_limit 不得出現故障類型判定。**
   只陳述觀察到的現象（哪個指標、偏離/上升了多少、信心度如何）與建議
   的下一步（複測、複核），不點名成因（軸承、對心、不平衡……）。
"""

from __future__ import annotations

import dataclasses
import json
import logging

import pandas as pd

from vibcore.config import DEFAULT_TREND
from vibcore.metrics import deviation as deviation_mod
from vibcore.metrics import iso as iso_mod
from vibcore.metrics import trend as trend_mod
from vibcore.rules.engine import register
from vibcore.types import RuleContext, RuleOutcome

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 共用小工具
# ──────────────────────────────────────────────────────────

#: 指標的中文顯示名稱（含原始欄位名，方便 Dashboard/週報與 rawdata 對照）
_METRIC_LABELS: dict[str, str] = {
    'vel_rms': '速度均方根值（velRMS）',
    'vel_oa': '速度整體值（velOA）',
    'acc_rms': '加速度均方根值（accRMS）',
    'acc_crest': '波峰因子（accCREST）',
    'acc_kurt': '峰度（accKURT）',
    'disp_p2p': '位移峰對峰值（dispP2P）',
    'acc_weighted_mean_freq': '加速度頻譜加權平均頻率（accWeightedMeanFreq）',
    # 逐軸衝擊型指標（三軸取最大，見 config.py 的 AXIS_IMPACT_COLS）：
    # 刻意不在名稱裡標示是哪一軸，理由與 `_axis_energy_sorted` 相同——
    # 軸標籤不可信，這裡的「逐軸」只代表「三軸中取最大值」這個運算方式。
    'acc_crest_axis_max': '波峰因子逐軸最大值（accCREST 三軸取大）',
    'acc_kurt_axis_max': '峰度逐軸最大值（accKURT 三軸取大）',
}

#: 指標的物理單位（純比值型指標如 crest/kurt 沒有單位）
_METRIC_UNITS: dict[str, str] = {
    'vel_rms': 'mm/s',
    'vel_oa': 'mm/s',
    'acc_rms': 'm/s²',
    'acc_crest': '',
    'acc_kurt': '',
    'disp_p2p': 'mm',
    'acc_weighted_mean_freq': 'Hz',
    'acc_crest_axis_max': '',
    'acc_kurt_axis_max': '',
}

#: ISO Zone 的嚴重程度順序（A 最輕、D 最重）
_ZONE_ORDER = ('A', 'B', 'C', 'D')

#: 排序後三軸能量佔比的三個鍵名（見 `vibcore/pipeline/aggregate.py`
#: 的 `_axis_energy_sorted`：方向無關，只看「主軸/次軸/弱軸」佔比）
_AXIS_KEYS = ('major', 'mid', 'minor')

#: AXIS_SHIFT 判定用的 trailing 窗口天數與最少樣本數。取「一段期間的
#: 中位數」而非單一筆，用意是與 event 型的 `ORIENTATION_CHANGE`
#: （單點跳變，偵測感測器重貼）明確區分開——AXIS_SHIFT 要抓的是持續性的
#: 緩慢偏移，一筆雜訊不該觸發。7 天涵蓋常見的週期性負載波動。
_AXIS_SHIFT_WINDOW_DAYS = 7.0
_AXIS_SHIFT_MIN_ROWS = 3

#: STEP_CHANGE 的候選特徵：只挑時域純量中定義與單位已實測驗證的指標
#: （見 PLAN §一「能力上限」表），刻意不用 acc_oa/vel_weighted_mean_freq
#: 這類定義尚未驗證的欄位，避免把不可信的數字餵進 Mahalanobis 距離。
#: 實際使用時取「特徵存在於 agg 欄位、且基準期有該指標統計量」的交集，
#: 至少需要 2 個特徵才有意義做「多變量」偏離判定。
_STEP_CHANGE_FEATURES = ('vel_rms', 'acc_rms', 'acc_crest', 'acc_kurt')


def _label(metric: str) -> str:
    return _METRIC_LABELS.get(metric, metric)


def _unit(metric: str) -> str:
    return _METRIC_UNITS.get(metric, '')


def _latest_ok_value(ctx: RuleContext, metric: str) -> float | None:
    """
    取 `ctx.analyzable()`（`data_status == 'ok'`）中最新一筆 `metric` 的
    有效數值；找不到欄位、沒有 ok 資料、或全為缺值時回傳 None，交由呼叫端
    決定輸出「資料不足」而非硬湊一個數字。
    """
    ok = ctx.analyzable()
    if ok.empty or metric not in ok.columns:
        return None
    col = pd.to_numeric(ok[metric], errors='coerce')
    sub = ok.assign(_v=col).dropna(subset=['_v'])
    if sub.empty:
        return None
    if 'ts_hour' in sub.columns:
        sub = sub.sort_values('ts_hour')
    return float(sub['_v'].iloc[-1])


def _sigma_channel(
    ctx: RuleContext, metric: str, threshold: float,
) -> tuple[float | None, object | None, float | None, bool]:
    """
    算單一指標欄位相對基準的 (最新值, 基準統計量, σ, 是否達門檻)。

    抽成共用函式是因為 `IMPACT_RISE` 現在要對合成值與逐軸最大值各算一次
    完全相同的流程；欄位或基準統計量任一缺失都回傳 `(val, None, None,
    False)`——**安靜地視為該通道不可用，不是錯誤**，這樣舊基準（沒有
    `acc_crest_axis_max` / `acc_kurt_axis_max` 統計量）餵進來時，呼叫端
    不需要另外寫防呆，新通道自然退化成「沒有這個證據」。
    """
    val = _latest_ok_value(ctx, metric)
    stat = ctx.baseline.stats.get(metric) if ctx.baseline is not None else None
    if stat is None or val is None:
        return val, None, None, False
    sigma = stat.sigma_of(val)
    return val, stat, sigma, sigma >= threshold


def _parse_axis_dict(value) -> dict[str, float] | None:
    """把單一列的 `axis_energy_sorted` 轉成 `{'major':.., 'mid':.., 'minor':..}`。

    正常路徑（DB 讀出的 JSONB／`aggregate_hourly` 直接產出）已經是 dict；
    容錯處理 JSON 字串與缺鍵/非數值的情況，寧可回傳 None 讓上層跳過這筆，
    也不要讓一筆髒資料整條規則爆掉。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(value, dict):
        return None
    try:
        return {k: float(value[k]) for k in _AXIS_KEYS if k in value and value[k] is not None}
    except (TypeError, ValueError):
        return None


def _axis_energy_recent_median(ctx: RuleContext) -> tuple[dict[str, float] | None, int]:
    """
    取 trailing `_AXIS_SHIFT_WINDOW_DAYS` 天內（以資料本身最新一筆 ok 的
    時間往回算，不依賴 `ctx.now`——與其餘直接呼叫 `metrics/` 模組的規則
    做法一致，讓「哪個時間點算最新」統一由資料本身決定）各 ok 列
    `axis_energy_sorted` 的中位數。回傳 (中位數 dict 或 None, 實際採用的
    列數)，列數不足 `_AXIS_SHIFT_MIN_ROWS` 時回傳 (None, n)。
    """
    ok = ctx.analyzable()
    if ok.empty or 'axis_energy_sorted' not in ok.columns or 'ts_hour' not in ok.columns:
        return None, 0

    ok = ok.sort_values('ts_hour')
    last_ts = ok['ts_hour'].iloc[-1]
    window = ok[ok['ts_hour'] > last_ts - pd.Timedelta(days=_AXIS_SHIFT_WINDOW_DAYS)]

    parsed = [d for d in (_parse_axis_dict(v) for v in window['axis_energy_sorted']) if d is not None]
    if len(parsed) < _AXIS_SHIFT_MIN_ROWS:
        return None, len(parsed)

    df = pd.DataFrame(parsed)
    median = {k: float(df[k].median()) for k in _AXIS_KEYS if k in df.columns}
    if len(median) < len(_AXIS_KEYS):
        return None, len(parsed)
    return median, len(parsed)


# ──────────────────────────────────────────────────────────
# ISO_ZONE — velRMS 對照機械等級 Zone（oscillating）
# ──────────────────────────────────────────────────────────

@register('ISO_ZONE')
def iso_zone(ctx: RuleContext) -> RuleOutcome:
    """
    velRMS 對照 ISO 10816/20816 機械等級的 Zone，達 `alert_zone`（預設 C）
    以上觸發。

    判定完全委派給 `vibcore.metrics.iso.evaluate_iso`——分級門檻、未分級
    設備的處理、等級合理性檢查都已在那裡實作，這裡只做「Zone 是否達到
    告警門檻」這一步業務判斷。**未分級設備（`applicable is False`）
    不得觸發**：對沒有可靠等級依據的設備硬套 Zone 判定，等於捏造一個沒有
    依據的告警。
    """
    result = iso_mod.evaluate_iso(ctx.agg, ctx.device, ctx.baseline)

    if not result.applicable:
        logger.debug(f"ISO_ZONE：point={ctx.point_id} 未分級（{result.class_source}），不套用 Zone 判定")
        return RuleOutcome.no_trigger('ISO_ZONE', 'iso_zone_exceed', 'oscillating')

    if result.zone is None:
        logger.debug(f"ISO_ZONE：point={ctx.point_id} 無可用 velRMS 資料，無法判定 Zone")
        return RuleOutcome.no_trigger('ISO_ZONE', 'iso_zone_exceed', 'oscillating')

    alert_zone = str(ctx.params.get('alert_zone', 'C')).upper()
    if alert_zone not in _ZONE_ORDER:
        logger.warning(f"ISO_ZONE：設定的 alert_zone={alert_zone!r} 不是合法 Zone，改用預設 C")
        alert_zone = 'C'

    triggered = _ZONE_ORDER.index(result.zone) >= _ZONE_ORDER.index(alert_zone)
    if not triggered:
        return RuleOutcome.no_trigger('ISO_ZONE', 'iso_zone_exceed', 'oscillating')

    return RuleOutcome(
        triggered=True,
        rule_code='ISO_ZONE',
        issue_type='iso_zone_exceed',
        family='oscillating',
        severity='err',
        title=f'整體振動速度達 ISO Zone {result.zone}',
        detail=(f'最近一次量測 velRMS = {result.vel_rms:.2f} mm/s，'
                f'依機械等級 {result.machine_class} 之 ISO 10816/20816 門檻對照，'
                f'落於 Zone {result.zone}（已達或超過告警門檻 Zone {alert_zone}）。'),
        interpretation_limit=(
            'ISO 10816/20816 Zone 僅反映整體振動位準是否超出通用門檻，是單一指標的絕對水準判定，'
            '不代表特定成因，也不能單獨作為判斷依據——需與衝擊性、趨勢等其他指標並行參考，'
            '並視情況安排複測確認。'
        ),
        current_value=result.vel_rms,
        value_unit='mm/s',
        evidence={
            'zone': result.zone,
            'alert_zone': alert_zone,
            'machine_class': result.machine_class,
            'class_source': result.class_source,
            'thresholds_mm_s': result.thresholds,
        },
    )


# ──────────────────────────────────────────────────────────
# VEL_HIGH — velOA 相對基準超過 N 個標準差（oscillating）
# ──────────────────────────────────────────────────────────

@register('VEL_HIGH')
def vel_high(ctx: RuleContext) -> RuleOutcome:
    """
    velOA 相對基準超過 `sigma`（預設 3.0）個標準差。

    只判「偏高」方向——σ 為負代表比基準低，不是本規則要抓的現象。
    """
    if ctx.baseline is None:
        logger.debug(f"VEL_HIGH：point={ctx.point_id} 尚無基準期，無法判定")
        return RuleOutcome.no_trigger('VEL_HIGH', 'vel_high', 'oscillating')

    stat = ctx.baseline.stats.get('vel_oa')
    if stat is None:
        logger.debug(f"VEL_HIGH：point={ctx.point_id} 基準期缺少 vel_oa 統計量")
        return RuleOutcome.no_trigger('VEL_HIGH', 'vel_high', 'oscillating')

    current = _latest_ok_value(ctx, 'vel_oa')
    if current is None:
        logger.debug(f"VEL_HIGH：point={ctx.point_id} 無可用（ok）的 vel_oa 資料")
        return RuleOutcome.no_trigger('VEL_HIGH', 'vel_high', 'oscillating')

    sigma_th = float(ctx.params.get('sigma', 3.0))
    sigma = stat.sigma_of(current)
    triggered = sigma >= sigma_th
    if not triggered:
        return RuleOutcome.no_trigger('VEL_HIGH', 'vel_high', 'oscillating')

    return RuleOutcome(
        triggered=True,
        rule_code='VEL_HIGH',
        issue_type='vel_high',
        family='oscillating',
        severity='warn',
        title='速度整體值相對基準偏高',
        detail=(f'最近一次量測 velOA = {current:.3f} mm/s，相對基準期中位數 '
                f'{stat.median:.3f} mm/s 高出 {sigma:.1f}σ（門檻 {sigma_th:.1f}σ）。'),
        interpretation_limit=(
            '本判定僅反映整體振動速度相對該量測點自身歷史基準的統計偏離，不涉及特定成因判別，'
            '亦未與其他指標交叉比對；建議參考同一量測點的其他指標並視情況安排複測。'
        ),
        current_value=current,
        baseline_value=stat.median,
        value_unit='mm/s',
        evidence={
            'sigma': round(sigma, 2),
            'sigma_threshold': sigma_th,
            'baseline_std': stat.std,
            'baseline_n': stat.n,
        },
    )


# ──────────────────────────────────────────────────────────
# IMPACT_RISE — accCREST / accKURT 相對基準顯著上升（monotonic）
# ──────────────────────────────────────────────────────────

@register('IMPACT_RISE')
def impact_rise(ctx: RuleContext) -> RuleOutcome:
    """
    accCREST / accKURT 相對基準顯著上升；同時比對**合成通道**
    （`acc_crest` / `acc_kurt`，對合成訊號算的）與**逐軸通道**
    （`acc_crest_axis_max` / `acc_kurt_axis_max`，三軸取最大值，見
    `pipeline/aggregate.py` 的 `_axis_impact_max`）。

    **為什麼兩種通道都要看，而不是直接用逐軸值取代合成值**：兩者衡量的
    是不同的東西，敏感度方向也不一致，不能假設其中一種必然比較敏感。
    實測 ZP 3-5 同一筆資料：三軸 crest 為 4.65/5.01/4.30，合成欄卻只有
    4.08（低於任一軸——單一方向的衝擊被其他兩軸稀釋掉了）；但同一筆的
    kurt 恰好相反，三軸為 3.15/3.37/2.87，合成欄卻是 4.53（高於任一軸）。
    所以本規則對兩種通道分別判定門檻、任一通道超標即觸發（OR），並在
    evidence／detail 裡標明是哪個通道——「衝擊集中在單一方向」只是對
    資料型態的客觀描述，不是成因推論，兩者觸發的意義不同：
    只有逐軸通道超標，代表衝擊可能集中在單一方向；合成與逐軸都超標，
    代表整體性的變化。

    判定邏輯：合成通道與逐軸通道各自沿用原本的「`require_both=False`
    時任一指標（crest 或 kurt）達門檻即算該通道觸發，`require_both=True`
    時需兩者同步上升」規則，兩個通道的判定結果再取 OR 作為最終是否觸發。
    這樣當基準期沒有逐軸統計量時（見下），逐軸通道恆為不觸發，整條規則
    會自然退化成加入逐軸通道之前的原始行為，不需要另外分支。

    參數（沿用既有的 `crest_sigma` 命名風格）：
    - `crest_sigma`（預設 2.5）：合成 accCREST 的 σ 門檻
    - `kurt_sigma`（預設 2.5）：合成 accKURT 的 σ 門檻
    - `crest_axis_sigma`（建議預設 2.5）：逐軸 acc_crest_axis_max 的 σ 門檻
    - `kurt_axis_sigma`（建議預設 2.5）：逐軸 acc_kurt_axis_max 的 σ 門檻
      （與 crest_axis_sigma 給同一預設值，是因為目前沒有證據支持逐軸
      kurt 比合成值更敏感或更不敏感——見上方 ZP 3-5 的反例，故不預設
      偏鬆或偏緊，留給使用端依實際資料校準）
    - `require_both`（預設 False）：語意同原本，只是分別套用在合成與
      逐軸兩個通道上（見上）

    基準期缺少 `acc_crest_axis_max` / `acc_kurt_axis_max` 的統計量時
    （既有資料庫的基準期都是這種情況，因為這兩個聚合欄位是後來才加的），
    `_sigma_channel` 會安靜回傳「該通道不可用」，本規則照常只用合成值
    判定，不拋錯、不整條規則跳過。
    """
    if ctx.baseline is None:
        logger.debug(f"IMPACT_RISE：point={ctx.point_id} 尚無基準期，無法判定")
        return RuleOutcome.no_trigger('IMPACT_RISE', 'impact_rise', 'monotonic')

    crest_th = float(ctx.params.get('crest_sigma', 2.5))
    kurt_th = float(ctx.params.get('kurt_sigma', 2.5))
    crest_axis_th = float(ctx.params.get('crest_axis_sigma', 2.5))
    kurt_axis_th = float(ctx.params.get('kurt_axis_sigma', 2.5))
    require_both = bool(ctx.params.get('require_both', False))

    crest_val, crest_stat, crest_sigma, crest_up = _sigma_channel(ctx, 'acc_crest', crest_th)
    kurt_val, kurt_stat, kurt_sigma, kurt_up = _sigma_channel(ctx, 'acc_kurt', kurt_th)
    crest_axis_val, crest_axis_stat, crest_axis_sigma, crest_axis_up = \
        _sigma_channel(ctx, 'acc_crest_axis_max', crest_axis_th)
    kurt_axis_val, kurt_axis_stat, kurt_axis_sigma, kurt_axis_up = \
        _sigma_channel(ctx, 'acc_kurt_axis_max', kurt_axis_th)

    if all(s is None for s in (crest_sigma, kurt_sigma, crest_axis_sigma, kurt_axis_sigma)):
        logger.debug(f"IMPACT_RISE：point={ctx.point_id} 無可用的衝擊性指標資料或基準統計量")
        return RuleOutcome.no_trigger('IMPACT_RISE', 'impact_rise', 'monotonic')

    composite_triggered = (crest_up and kurt_up) if require_both else (crest_up or kurt_up)
    axis_triggered = (crest_axis_up and kurt_axis_up) if require_both else (crest_axis_up or kurt_axis_up)
    triggered = composite_triggered or axis_triggered

    if not triggered:
        return RuleOutcome.no_trigger('IMPACT_RISE', 'impact_rise', 'monotonic')

    # 挑 σ 最大者作為呈現用的主要數值（不代表「哪個更重要」，只是取較顯著者）。
    # 四個候選中缺資料/缺基準統計量的通道 sigma 為 None 已被濾掉；只剩合成
    # 兩通道時，這一步與加入逐軸通道之前完全等價。
    candidates = [
        ('acc_crest', crest_val, crest_stat, crest_sigma),
        ('acc_kurt', kurt_val, kurt_stat, kurt_sigma),
        ('acc_crest_axis_max', crest_axis_val, crest_axis_stat, crest_axis_sigma),
        ('acc_kurt_axis_max', kurt_axis_val, kurt_axis_stat, kurt_axis_sigma),
    ]
    primary_metric, primary_val, primary_stat, primary_sigma = max(
        (c for c in candidates if c[3] is not None), key=lambda c: c[3],
    )

    # 標明觸發來源，讓 evidence／detail 能區分「合成」「逐軸」「兩者都有」
    # ——這個區分只描述資料型態，不推論成因（見函式 docstring）。
    if composite_triggered and axis_triggered:
        trigger_source = 'both'
    elif axis_triggered:
        trigger_source = 'axis_max'
    else:
        trigger_source = 'composite'

    detail = (f'{_label(primary_metric)} 相對基準期中位數 {primary_stat.median:.2f} '
              f'上升至 {primary_val:.2f}（{primary_sigma:+.1f}σ）。')
    if crest_up and kurt_up:
        detail += '合成訊號的波峰因子與峰度同步上升。'
    if trigger_source == 'axis_max':
        detail += '僅逐軸最大值（三軸取大）超標、合成訊號未達門檻，衝擊可能集中在單一方向。'
    elif trigger_source == 'both':
        detail += '合成訊號與逐軸最大值同步超標，屬整體性變化。'

    return RuleOutcome(
        triggered=True,
        rule_code='IMPACT_RISE',
        issue_type='impact_rise',
        family='monotonic',
        severity='warn',
        title='衝擊性指標相對基準顯著上升',
        detail=detail,
        interpretation_limit=(
            '波峰因子（Crest）與峰度（Kurtosis）反映振動訊號中衝擊成分的強弱，兩者上升通常代表'
            '週期性衝擊事件增加，常見於軸承劣化、潤滑不足或機件鬆動等情況，'
            '本系統無法區分具體成因；逐軸最大值可用於察覺衝擊是否集中在單一方向，'
            '但感測器可能貼錯方向，本系統刻意不保留是哪一軸，因此無法指出實際方向，'
            '仍建議安排專家量測系統複測以確認。'
        ),
        current_value=primary_val,
        baseline_value=primary_stat.median,
        value_unit=_unit(primary_metric),
        evidence={
            'primary_metric': primary_metric,
            'trigger_source': trigger_source,
            'crest_current': crest_val,
            'crest_sigma': round(crest_sigma, 2) if crest_sigma is not None else None,
            'crest_threshold_sigma': crest_th,
            'kurt_current': kurt_val,
            'kurt_sigma': round(kurt_sigma, 2) if kurt_sigma is not None else None,
            'kurt_threshold_sigma': kurt_th,
            'crest_axis_current': crest_axis_val,
            'crest_axis_sigma': round(crest_axis_sigma, 2) if crest_axis_sigma is not None else None,
            'crest_axis_threshold_sigma': crest_axis_th,
            'kurt_axis_current': kurt_axis_val,
            'kurt_axis_sigma': round(kurt_axis_sigma, 2) if kurt_axis_sigma is not None else None,
            'kurt_axis_threshold_sigma': kurt_axis_th,
            'require_both': require_both,
        },
    )


# ──────────────────────────────────────────────────────────
# DEGRADE_TREND — 指標回歸斜率持續惡化（monotonic）
# ──────────────────────────────────────────────────────────

#: 檢查趨勢的候選指標：時域純量中「數值上升＝振動水準惡化」方向明確者。
#: 不含 vel_oa/acc_oa（定義未驗證，見 config.py）與頻譜指標
#: （由 SPECTRAL_SHIFT 另外處理，避免同一份訊號被兩條規則各自判定成
#: 「劣化」與「頻譜上移」重複告警）。
_DEGRADE_TREND_METRICS = ('vel_rms', 'acc_rms', 'acc_crest', 'acc_kurt', 'disp_p2p')


@register('DEGRADE_TREND')
def degrade_trend(ctx: RuleContext) -> RuleOutcome:
    """
    指標回歸斜率持續惡化：對候選指標逐一呼叫 `metrics.trend.compute_trend`，
    只要有任一指標同時滿足「信心度非 low」「方向為上升」「斜率達
    `slope_pct_per_month` 門檻」即觸發，取其中斜率百分比最大者作為代表。

    **低信心度趨勢一律不觸發**——`compute_trend` 的 `confidence` 已經把
    樣本數不足、觀察期太短、R² 偏低、期間涵蓋率不足（可能是感測器斷線）
    四種情況都算進去，這裡用 `TrendResult.is_reliable` 直接把關，不重新
    判斷任何一項，避免規則層與指標層對「夠不夠可信」有兩套邏輯。
    """
    if ctx.agg is None or ctx.agg.empty:
        logger.debug(f"DEGRADE_TREND：point={ctx.point_id} 無聚合資料")
        return RuleOutcome.no_trigger('DEGRADE_TREND', 'degradation_trend', 'monotonic')

    min_days = int(ctx.params.get('min_days', DEFAULT_TREND.min_days))
    min_r2 = float(ctx.params.get('min_r2', DEFAULT_TREND.min_r2))
    slope_th = float(ctx.params.get('slope_pct_per_month', 10))
    cfg = dataclasses.replace(DEFAULT_TREND, min_days=min_days, min_r2=min_r2)

    candidates = [m for m in _DEGRADE_TREND_METRICS if m in ctx.agg.columns]
    if not candidates:
        logger.debug(f"DEGRADE_TREND：point={ctx.point_id} 聚合資料缺少全部候選指標欄位")
        return RuleOutcome.no_trigger('DEGRADE_TREND', 'degradation_trend', 'monotonic')

    trends = trend_mod.compute_trends(ctx.agg, candidates, ctx.baseline, cfg=cfg)

    qualifying = {
        m: t for m, t in trends.items()
        if t.is_reliable and t.direction == 'up'
        and not pd.isna(t.slope_pct_per_month) and t.slope_pct_per_month >= slope_th
    }
    if not qualifying:
        return RuleOutcome.no_trigger('DEGRADE_TREND', 'degradation_trend', 'monotonic')

    metric, best = max(qualifying.items(), key=lambda kv: kv[1].slope_pct_per_month)
    current_val = _latest_ok_value(ctx, metric)
    baseline_stat = ctx.baseline.stats.get(metric) if ctx.baseline is not None else None

    return RuleOutcome(
        triggered=True,
        rule_code='DEGRADE_TREND',
        issue_type='degradation_trend',
        family='monotonic',
        severity='warn',
        title=f'{_label(metric)} 呈現持續上升趨勢',
        detail=(f'過去 {best.span_days:.0f} 天迴歸斜率為每月 {best.slope_pct_per_month:+.1f}%'
                f'（相對基準中位數；R²={best.r2:.2f}，信心度：{best.confidence}），'
                f'高於門檻每月 {slope_th:.1f}%。'),
        interpretation_limit=(
            '本判定僅反映該指標隨時間的統計變化速率與信心度，是趨勢方向與速度的描述，'
            '不代表特定成因，也不能用於估計剩餘可用時間；如需精確評估，'
            '請安排專家系統複測。'
        ),
        current_value=current_val,
        baseline_value=baseline_stat.median if baseline_stat is not None else None,
        value_unit=_unit(metric),
        evidence={
            'metric': metric,
            'slope_pct_per_month': round(best.slope_pct_per_month, 2),
            'slope_threshold_pct_per_month': slope_th,
            'r2': round(best.r2, 3),
            'n_points': best.n_points,
            'span_days': round(best.span_days, 1),
            'confidence': best.confidence,
            'other_qualifying_metrics': [m for m in qualifying if m != metric],
        },
    )


# ──────────────────────────────────────────────────────────
# SPECTRAL_SHIFT — accWeightedMeanFreq 重心持續上移（monotonic）
# ──────────────────────────────────────────────────────────

@register('SPECTRAL_SHIFT')
def spectral_shift(ctx: RuleContext) -> RuleOutcome:
    """
    `accWeightedMeanFreq`（頻譜加權平均頻率，即頻譜重心）持續上移，代表
    能量往高頻移動。刻意**不**辨識這個位移對應哪個諧波或故障頻率——
    諧波欄位定義未經驗證（見 config.py），本規則只用這個穩健的純量趨勢。

    與 `DEGRADE_TREND` 相同，低信心度趨勢不觸發。
    """
    metric = 'acc_weighted_mean_freq'
    if ctx.agg is None or ctx.agg.empty or metric not in ctx.agg.columns:
        logger.debug(f"SPECTRAL_SHIFT：point={ctx.point_id} 聚合資料缺少 {metric} 欄位")
        return RuleOutcome.no_trigger('SPECTRAL_SHIFT', 'spectral_shift', 'monotonic')

    shift_th = float(ctx.params.get('shift_pct', 15))
    min_days = int(ctx.params.get('min_days', DEFAULT_TREND.min_days))
    cfg = dataclasses.replace(DEFAULT_TREND, min_days=min_days)

    result = trend_mod.compute_trend(ctx.agg, metric, ctx.baseline, cfg=cfg)

    if not result.is_reliable:
        logger.debug(f"SPECTRAL_SHIFT：point={ctx.point_id} 趨勢信心度低（{result.note}），不觸發")
        return RuleOutcome.no_trigger('SPECTRAL_SHIFT', 'spectral_shift', 'monotonic')

    if result.direction != 'up' or pd.isna(result.slope_pct_per_month) \
            or result.slope_pct_per_month < shift_th:
        return RuleOutcome.no_trigger('SPECTRAL_SHIFT', 'spectral_shift', 'monotonic')

    current_val = _latest_ok_value(ctx, metric)
    baseline_stat = ctx.baseline.stats.get(metric) if ctx.baseline is not None else None

    return RuleOutcome(
        triggered=True,
        rule_code='SPECTRAL_SHIFT',
        issue_type='spectral_shift',
        family='monotonic',
        severity='warn',
        title='頻譜重心持續上移',
        detail=(f'加速度頻譜加權平均頻率過去 {result.span_days:.0f} 天以每月 '
                f'{result.slope_pct_per_month:+.1f}% 的速率上移（R²={result.r2:.2f}，'
                f'信心度：{result.confidence}），高於門檻每月 {shift_th:.1f}%，'
                '代表振動能量分布正往高頻方向移動。'),
        interpretation_limit=(
            '本判定僅反映頻譜能量重心的整體移動方向與速率，屬於穩健的頻譜摘要指標，'
            '不辨識個別諧波或對應特定機械頻率，也不判定成因；建議安排專家系統做完整頻譜分析。'
        ),
        current_value=current_val,
        baseline_value=baseline_stat.median if baseline_stat is not None else None,
        value_unit=_unit(metric),
        evidence={
            'metric': metric,
            'slope_pct_per_month': round(result.slope_pct_per_month, 2),
            'shift_threshold_pct_per_month': shift_th,
            'r2': round(result.r2, 3),
            'n_points': result.n_points,
            'span_days': round(result.span_days, 1),
            'confidence': result.confidence,
        },
    )


# ──────────────────────────────────────────────────────────
# AXIS_SHIFT — 排序後三軸能量佔比相對基準偏移（monotonic）
# ──────────────────────────────────────────────────────────

@register('AXIS_SHIFT')
def axis_shift(ctx: RuleContext) -> RuleOutcome:
    """
    排序後三軸能量佔比（主軸/次軸/弱軸，方向無關，見
    `vibcore/pipeline/aggregate.py` 的 `_axis_energy_sorted`）相對基準偏移。

    取 trailing 窗口的**中位數**與基準比較，而非單筆——這是本規則與 event
    型 `ORIENTATION_CHANGE`（單點跳變，疑似感測器重貼/更換）的關鍵區別：
    後者抓的是突然的排列跳變，本規則抓的是持續性的緩慢漂移，用中位數
    可以自然過濾掉單筆雜訊，也不會把 ORIENTATION_CHANGE 該處理的突發事件
    重複判成 AXIS_SHIFT。
    """
    if ctx.axis_energy_baseline is None:
        logger.debug(f"AXIS_SHIFT：point={ctx.point_id} 尚無軸能量基準（axis_energy_baseline），無法判定")
        return RuleOutcome.no_trigger('AXIS_SHIFT', 'axis_shift', 'monotonic')

    baseline_energy = _parse_axis_dict(ctx.axis_energy_baseline)
    if baseline_energy is None or len(baseline_energy) < len(_AXIS_KEYS):
        logger.debug(f"AXIS_SHIFT：point={ctx.point_id} 軸能量基準缺鍵或格式不正確")
        return RuleOutcome.no_trigger('AXIS_SHIFT', 'axis_shift', 'monotonic')

    current_energy, n_rows = _axis_energy_recent_median(ctx)
    if current_energy is None:
        logger.debug(f"AXIS_SHIFT：point={ctx.point_id} 近 {_AXIS_SHIFT_WINDOW_DAYS:.0f} 天內"
                     f"可用的 axis_energy_sorted 樣本僅 {n_rows} 筆，不足以判定")
        return RuleOutcome.no_trigger('AXIS_SHIFT', 'axis_shift', 'monotonic')

    ratio_delta_th = float(ctx.params.get('ratio_delta', 0.15))
    deltas = {k: current_energy[k] - baseline_energy[k] for k in _AXIS_KEYS}
    max_key = max(deltas, key=lambda k: abs(deltas[k]))
    max_delta = abs(deltas[max_key])

    triggered = max_delta >= ratio_delta_th
    if not triggered:
        return RuleOutcome.no_trigger('AXIS_SHIFT', 'axis_shift', 'monotonic')

    return RuleOutcome(
        triggered=True,
        rule_code='AXIS_SHIFT',
        issue_type='axis_shift',
        family='monotonic',
        severity='warn',
        title='三軸能量分佈相對基準偏移',
        detail=(f'排序後三軸能量佔比中「{max_key}」相對基準已偏移 {deltas[max_key]:+.1%}'
                f'（門檻 ±{ratio_delta_th:.0%}），取近 {_AXIS_SHIFT_WINDOW_DAYS:.0f} 天'
                f'（{n_rows} 筆有效樣本）中位數與基準期比較。'),
        interpretation_limit=(
            '本判定僅反映排序後三軸能量佔比的持續性變化，方向無關，可能源自負載型態改變、'
            '運轉工況變化或感測器安裝狀態改變等多種原因，本系統無法區分具體成因；'
            '如需確認，建議實地檢查現場工況與感測器安裝狀態。'
        ),
        current_value=max_delta,
        baseline_value=ratio_delta_th,
        value_unit='',
        evidence={
            'baseline': {k: round(v, 4) for k, v in baseline_energy.items()},
            'current_median': {k: round(v, 4) for k, v in current_energy.items()},
            'deltas': {k: round(v, 4) for k, v in deltas.items()},
            'max_shift_key': max_key,
            'ratio_delta_threshold': ratio_delta_th,
            'window_days': _AXIS_SHIFT_WINDOW_DAYS,
            'n_rows': n_rows,
        },
    )


# ──────────────────────────────────────────────────────────
# STEP_CHANGE — 多變量偏離基準（monotonic）
# ──────────────────────────────────────────────────────────

@register('STEP_CHANGE')
def step_change(ctx: RuleContext) -> RuleOutcome:
    """
    多變量（Mahalanobis 距離）偏離基準。每次呼叫都以 `ctx.baseline` 期間的
    資料現場擬合模型（`fit_deviation_model`）——Phase 1 的 `RuleContext`
    契約沒有預先擬合好的模型可用，此處為求正確性優先，接受重複擬合的成本；
    若之後效能吃緊，可比照 `validate/rules_stub.py` 的做法用
    `(point_id, 基準期起訖)` 快取模型，屬於呼叫端可自行加上的最佳化，
    不影響本函式的判定邏輯。

    `DeviationResult.computable is False` 時**視為「未評估」而非「正常」
    ——不觸發，但也不能被下游誤讀成距離為 0 的健康狀態**（`computable`
    欄位存在的意義正是避免這種混淆，見 `metrics/deviation.py`）。
    """
    if ctx.baseline is None:
        logger.debug(f"STEP_CHANGE：point={ctx.point_id} 尚無基準期，無法判定")
        return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')

    features = [f for f in _STEP_CHANGE_FEATURES
                if f in ctx.agg.columns and f in ctx.baseline.stats]
    if len(features) < 2:
        logger.debug(f"STEP_CHANGE：point={ctx.point_id} 可用特徵不足 2 個"
                     f"（agg 欄位與基準統計量交集：{features}），無法做多變量判定")
        return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')

    threshold = float(ctx.params.get('mahalanobis_sigma', 3.0))

    try:
        model = deviation_mod.fit_deviation_model(ctx.agg, features, ctx.baseline)
    except ValueError as e:
        logger.debug(f"STEP_CHANGE：point={ctx.point_id} 模型擬合失敗（{e}），不觸發")
        return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')

    result = deviation_mod.evaluate_deviation(ctx.agg, model, ctx.baseline, threshold_sigma=threshold)

    if not result.computable:
        logger.debug(f"STEP_CHANGE：point={ctx.point_id} 未評估（{result.note}），不觸發")
        return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')

    if not result.is_deviated:
        return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')

    return RuleOutcome(
        triggered=True,
        rule_code='STEP_CHANGE',
        issue_type='step_change',
        family='monotonic',
        severity='warn',
        title='多變量特徵組合偏離基準',
        detail=(f'綜合 {"、".join(_label(f) for f in features)} 的 Mahalanobis 距離為 '
                f'{result.distance:.2f}（門檻 {result.threshold:.1f}）。{result.describe()}'),
        interpretation_limit=(
            '多變量偏離僅代表所選特徵的整體組合相對歷史基準出現統計上的顯著偏離，'
            '不是成因判定；各特徵的標準化偏離量（σ）可用於判斷哪些指標貢獻較大，'
            '具體成因仍需結合其他證據並由專家系統複測確認。'
        ),
        current_value=result.distance,
        baseline_value=result.threshold,
        value_unit='',
        evidence={
            'features': features,
            'distance': result.distance,
            'threshold': result.threshold,
            'per_feature_sigma': result.per_feature_sigma,
            'top_contributors': result.top_contributors,
        },
    )
