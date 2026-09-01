"""
rules_stub.py — 13 條規則的可替換 stub 實作

**設計依據**：`vibcore/types.py` 的 `RuleContext`／`RuleOutcome` 是規則層的
正式契約——文件明講「`RuleContext` 是一條規則能看到的全部資訊」、
「`params` 來自 `rule_config.params`」，代表真正的規則引擎會對每個
`(量測點, 規則代碼)` 組合各自組一份 `RuleContext`（帶該規則自己的參數），
交給對應的判定函式，得到一個 `RuleOutcome`。本檔完全比照這個形狀寫，
所以等真實的 `vibcore.rules` 套件完成後，只要它也提供
`{rule_code: Callable[[RuleContext], RuleOutcome]}` 這種登錄表（或個別
`evaluate(ctx)` 函式），下面 `_import_real_registry()` 改一下匯入路徑，
`validate/offline.py` 完全不需要改。

**已經是真實模組、不是 stub 的部分**：`vibcore/metrics/iso.py`
（ISO 分級）、`vibcore/metrics/deviation.py`（Mahalanobis 偏離）與
`vibcore/metrics/trend.py`（線性趨勢）在撰寫本檔當下已經存在且介面穩定，
因此 `ISO_ZONE` / `ISO_CLASS_SUSPECT` / `STEP_CHANGE` / `DEGRADE_TREND` /
`SPECTRAL_SHIFT` 五條規則**直接呼叫這些真實模組**，不是自己重新發明一套
簡化邏輯——這樣回測結果對這五條規則才有參考價值。若之後這些模組介面
改變或暫時不可匯入，會自動退回本檔內建的簡化版（見各自函式內的
try/except），確保框架不會因此打不通，但退回時的結果**僅供跑通驗證，
不能拿來做真的門檻判斷**（報告會標註 `is_real=False`）。

**其餘規則（VEL_HIGH、IMPACT_RISE、AXIS_SHIFT、ORIENTATION_CHANGE、
SENSOR_OFFLINE、DATA_QUALITY、SENSOR_SATURATION、STANDBY_NO_RUNTIME）
目前沒有對應的 vibcore 模組**，以下全部是簡化 stub，邏輯直接寫在本檔、
不去猜真實模組路徑（因為根本還沒人在寫）。每個函式的 docstring 都寫明
簡化了什麼、真正的規則層完成後這裡的判準需要注意哪些差異。
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Callable

import numpy as np
import pandas as pd

from vibcore.config import DEFAULT_TREND, DataStatus, FULL_SCALE_MS2, TrendConfig
from vibcore.types import DeviationResult, RuleContext, RuleOutcome, TrendResult

logger = logging.getLogger(__name__)

RuleFunc = Callable[[RuleContext], RuleOutcome]

_TRIAGE_LIMIT = '本系統定位為篩選預警，不做故障診斷；此證據僅代表統計異常，成因需由專家系統或現場檢查確認。'


# ──────────────────────────────────────────────────────────
# 共用小工具
# ──────────────────────────────────────────────────────────

def _latest_ok_row(agg: pd.DataFrame) -> pd.Series | None:
    if agg is None or agg.empty or 'data_status' not in agg.columns:
        return None
    ok = agg[agg['data_status'] == DataStatus.OK]
    if ok.empty:
        return None
    return ok.sort_values('ts_hour').iloc[-1]


def _recent_ok(agg: pd.DataFrame, now: pd.Timestamp, window_days: float) -> pd.DataFrame:
    if agg is None or agg.empty or 'data_status' not in agg.columns:
        return agg.iloc[0:0] if agg is not None else pd.DataFrame()
    ok = agg[agg['data_status'] == DataStatus.OK]
    cutoff = now - pd.Timedelta(days=window_days)
    return ok[(ok['ts_hour'] > cutoff) & (ok['ts_hour'] <= now)]


def _linear_trend_stub(ok: pd.DataFrame, metric: str, now: pd.Timestamp,
                        min_days: int, min_points: int, min_r2: float,
                        baseline_median: float | None) -> TrendResult:
    """
    簡化線性趨勢：對 `metric` 在 `ok` 樣本上跑一次最小平方回歸。

    **這不是正式的趨勢模組**——真實實作應該處理離群值、分工況、以及
    「聚合後仍相鄰高度相關」等問題（見計畫書 §三：即使聚合成每小時，
    仍需注意樣本間是否獨立）。這裡只求量級正確：資料量夠、噴出的
    Finding 數量級是否合理，不追求統計嚴謹度。
    """
    d = ok.dropna(subset=[metric]).sort_values('ts_hour')
    d = d[d['ts_hour'] <= now]
    n = len(d)
    if n < min_points:
        return TrendResult(metric=metric, slope_per_day=0.0, slope_per_month=0.0,
                            slope_pct_per_month=0.0, intercept=0.0, r2=0.0, n_points=n,
                            span_days=0.0, direction='unknown', confidence='low',
                            note=f'可用樣本僅 {n} 筆（需要 {min_points}），無法回歸')

    t0 = d['ts_hour'].iloc[0]
    span_days = (d['ts_hour'].iloc[-1] - t0).total_seconds() / 86400
    if span_days < min_days:
        return TrendResult(metric=metric, slope_per_day=0.0, slope_per_month=0.0,
                            slope_pct_per_month=0.0, intercept=0.0, r2=0.0, n_points=n,
                            span_days=span_days, direction='unknown', confidence='low',
                            note=f'觀察跨度僅 {span_days:.1f} 天（需要 {min_days} 天）')

    t = (d['ts_hour'] - t0).dt.total_seconds().to_numpy() / 86400.0
    y = d[metric].to_numpy(dtype=float)
    slope, intercept = np.polyfit(t, y, 1)
    fitted = slope * t + intercept
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    ref = baseline_median if baseline_median else (float(y[0]) if y[0] != 0 else np.nan)
    slope_per_month = slope * 30
    slope_pct_per_month = (slope_per_month / ref * 100) if ref and not np.isnan(ref) and ref != 0 else 0.0

    eps = 1e-9
    direction = 'up' if slope > eps else ('down' if slope < -eps else 'flat')
    confidence = 'high' if r2 >= min_r2 else 'low'
    note = '' if confidence == 'high' else f'R²={r2:.2f} 低於門檻 {min_r2}，趨勢不明確'

    return TrendResult(metric=metric, slope_per_day=float(slope), slope_per_month=float(slope_per_month),
                        slope_pct_per_month=float(slope_pct_per_month), intercept=float(intercept),
                        r2=round(r2, 4), n_points=n, span_days=round(span_days, 2),
                        direction=direction, confidence=confidence, note=note)


def _axis_now(agg: pd.DataFrame, now: pd.Timestamp, window_days: float | None) -> dict | None:
    """
    取「目前」的三軸能量佔比：`window_days` 給定時取trailing window內
    ok 樣本的中位數（AXIS_SHIFT 用，看持續性偏移）；`None` 時只取最新一筆
    （ORIENTATION_CHANGE 用，看單點跳變）。
    """
    if window_days is None:
        row = _latest_ok_row(agg[agg['ts_hour'] <= now] if agg is not None and not agg.empty else agg)
        val = row.get('axis_energy_sorted') if row is not None else None
        return val if isinstance(val, dict) else None

    recent = _recent_ok(agg, now, window_days)
    rows = [r for r in recent.get('axis_energy_sorted', []) if isinstance(r, dict)]
    if len(rows) < 3:
        return None
    return {k: float(np.median([r[k] for r in rows if k in r])) for k in ('major', 'mid', 'minor')}


# ──────────────────────────────────────────────────────────
# ISO_ZONE / ISO_CLASS_SUSPECT — 直接使用真實模組 vibcore.metrics.iso
# ──────────────────────────────────────────────────────────

def _get_iso_evaluator():
    try:
        from vibcore.metrics.iso import evaluate_iso
        return evaluate_iso, True
    except ImportError:
        logger.warning("vibcore.metrics.iso 無法匯入，ISO_ZONE/ISO_CLASS_SUSPECT 使用內建簡化版")
        return None, False


_evaluate_iso, USING_REAL_ISO = _get_iso_evaluator()

#: 內建簡化版門檻，僅在 vibcore.metrics.iso 無法匯入時使用（見上方模組說明）。
#: 鍵為 '群組/基礎剛性'，數值同 ISO 10816-3（與真實模組保持一致，
#: 否則 stub 與真實模組會給出不同的 Zone，讓回測結果無法互相比較）。
_FALLBACK_ISO_THRESHOLDS = {
    '1/rigid':    {'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
    '1/flexible': {'ab': 3.50, 'bc': 7.10, 'cd': 11.00},
    '2/rigid':    {'ab': 1.40, 'bc': 2.80, 'cd': 4.50},
    '2/flexible': {'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
    '3/rigid':    {'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
    '3/flexible': {'ab': 3.50, 'bc': 7.10, 'cd': 11.00},
    '4/rigid':    {'ab': 1.40, 'bc': 2.80, 'cd': 4.50},
    '4/flexible': {'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
}


def _device_iso_key(device) -> str | None:
    """設備的 '群組/基礎剛性' 鍵；任一缺失回傳 None（等同未分類）。"""
    g, f = device.iso_machine_group, device.iso_foundation
    return f'{g}/{f}' if g and f else None
_ZONE_ORDER = ('A', 'B', 'C', 'D')


def _fallback_zone(vel_rms: float | None, iso_key: str | None) -> str | None:
    if iso_key not in _FALLBACK_ISO_THRESHOLDS or vel_rms is None or pd.isna(vel_rms):
        return None
    th = _FALLBACK_ISO_THRESHOLDS[iso_key]
    if vel_rms <= th['ab']:
        return 'A'
    if vel_rms <= th['bc']:
        return 'B'
    if vel_rms <= th['cd']:
        return 'C'
    return 'D'


def rule_iso_zone(ctx: RuleContext) -> RuleOutcome:
    """velRMS 對照 ISO 10816 機械等級的 Zone A/B/C/D；未分級設備不觸發。"""
    agg_asof = ctx.agg[ctx.agg['ts_hour'] <= ctx.now] if not ctx.agg.empty else ctx.agg
    alert_zone = str(ctx.params.get('alert_zone', 'C'))

    if _evaluate_iso is not None:
        iso = _evaluate_iso(agg_asof, ctx.device, ctx.baseline)
        if not iso.applicable or iso.zone is None:
            return RuleOutcome.no_trigger('ISO_ZONE', 'iso_zone_exceed', 'oscillating')
        triggered = _ZONE_ORDER.index(iso.zone) >= _ZONE_ORDER.index(alert_zone)
        return RuleOutcome(
            triggered=triggered, rule_code='ISO_ZONE', issue_type='iso_zone_exceed',
            family='oscillating', severity='err' if triggered else 'warn',
            title=f'ISO 位準 Zone {iso.zone}' if triggered else '',
            detail=f'velRMS={iso.vel_rms}，ISO 分類 {iso.machine_class}，Zone {iso.zone}',
            interpretation_limit=_TRIAGE_LIMIT,
            current_value=iso.vel_rms, value_unit='mm/s',
            evidence={'zone': iso.zone, 'thresholds': iso.thresholds, 'alert_zone': alert_zone},
        )

    # ── 內建簡化版（vibcore.metrics.iso 不可用時）──────────────
    row = _latest_ok_row(agg_asof)
    vel_rms = float(row['vel_rms']) if row is not None and not pd.isna(row.get('vel_rms')) else None
    if ctx.device.iso_class_source == 'unset' or _device_iso_key(ctx.device) is None:
        return RuleOutcome.no_trigger('ISO_ZONE', 'iso_zone_exceed', 'oscillating')
    zone = _fallback_zone(vel_rms, _device_iso_key(ctx.device))
    triggered = zone is not None and _ZONE_ORDER.index(zone) >= _ZONE_ORDER.index(alert_zone)
    return RuleOutcome(triggered=triggered, rule_code='ISO_ZONE', issue_type='iso_zone_exceed',
                        family='oscillating', severity='err' if triggered else 'warn',
                        title=f'ISO 位準 Zone {zone}' if triggered else '',
                        current_value=vel_rms, value_unit='mm/s',
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'zone': zone, 'note': 'fallback（未接上 vibcore.metrics.iso）'})


def rule_iso_class_suspect(ctx: RuleContext) -> RuleOutcome:
    """基準期 velRMS 中位數已超過所指派等級的 B/C 界 → 等級可能填錯。"""
    agg_asof = ctx.agg[ctx.agg['ts_hour'] <= ctx.now] if not ctx.agg.empty else ctx.agg
    if _evaluate_iso is not None:
        iso = _evaluate_iso(agg_asof, ctx.device, ctx.baseline)
        return RuleOutcome(
            triggered=iso.is_class_suspect, rule_code='ISO_CLASS_SUSPECT',
            issue_type='iso_class_suspect', family='event',
            severity='warn', title='ISO 等級疑似誤填' if iso.is_class_suspect else '',
            detail=iso.suspect_reason, interpretation_limit=_TRIAGE_LIMIT,
        )
    if ctx.baseline is None or ctx.device.iso_class_source == 'unset' \
            or _device_iso_key(ctx.device) is None:
        return RuleOutcome.no_trigger('ISO_CLASS_SUSPECT', 'iso_class_suspect', 'event')
    stat = ctx.baseline.stats.get('vel_rms')
    th = _FALLBACK_ISO_THRESHOLDS.get(_device_iso_key(ctx.device))
    triggered = bool(stat and th and stat.median > th['bc'])
    return RuleOutcome(triggered=triggered, rule_code='ISO_CLASS_SUSPECT',
                        issue_type='iso_class_suspect', family='event', severity='warn',
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'note': 'fallback（未接上 vibcore.metrics.iso）'})


# ──────────────────────────────────────────────────────────
# STEP_CHANGE — 直接使用真實模組 vibcore.metrics.deviation
# ──────────────────────────────────────────────────────────

def _get_deviation_funcs():
    try:
        from vibcore.metrics.deviation import fit_deviation_model, evaluate_deviation
        return fit_deviation_model, evaluate_deviation, True
    except ImportError:
        logger.warning("vibcore.metrics.deviation 無法匯入，STEP_CHANGE 使用內建簡化版（對角近似）")
        return None, None, False


_fit_deviation_model, _evaluate_deviation, USING_REAL_DEVIATION = _get_deviation_funcs()


def _get_trend_evaluator():
    try:
        from vibcore.metrics.trend import compute_trend
        return compute_trend, True
    except ImportError:
        logger.warning("vibcore.metrics.trend 無法匯入，DEGRADE_TREND/SPECTRAL_SHIFT 使用內建簡化版")
        return None, False


_compute_trend, USING_REAL_TREND = _get_trend_evaluator()

#: STEP_CHANGE 的候選特徵；實際會用哪些取交集（agg 有該欄且基準期有統計量）
_STEP_CHANGE_FEATURES = ['vel_rms', 'acc_rms', 'acc_crest', 'acc_kurt', 'acc_weighted_mean_freq']

#: 每個量測點的 Mahalanobis 模型只需要用基準期資料 fit 一次，同一份基準期
#: 重複 fit 沒有意義還浪費時間——用 (point_id, 基準期起訖) 當 key 快取。
_deviation_model_cache: dict[tuple, dict | None] = {}


def _get_or_fit_deviation_model(ctx: RuleContext) -> tuple[dict | None, list[str]]:
    features = [f for f in _STEP_CHANGE_FEATURES
                if f in ctx.agg.columns and ctx.baseline and f in ctx.baseline.stats]
    if len(features) < 2 or ctx.baseline is None:
        return None, features

    key = (ctx.point_id, ctx.baseline.start_date, ctx.baseline.end_date, tuple(features))
    if key in _deviation_model_cache:
        return _deviation_model_cache[key], features

    model = None
    if _fit_deviation_model is not None:
        try:
            model = _fit_deviation_model(ctx.agg, features, ctx.baseline)
        except ValueError as e:
            logger.debug(f"point {ctx.point_id} STEP_CHANGE 模型擬合失敗：{e}")
    _deviation_model_cache[key] = model
    return model, features


def rule_step_change(ctx: RuleContext) -> RuleOutcome:
    """多變量偏離基準（Mahalanobis 距離），輸出各特徵 σ 分解而非單一分數。"""
    sigma_th = float(ctx.params.get('mahalanobis_sigma', 3.0))
    agg_asof = ctx.agg[ctx.agg['ts_hour'] <= ctx.now]

    if _evaluate_deviation is not None:
        model, features = _get_or_fit_deviation_model(ctx)
        if model is None:
            return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')
        result: DeviationResult = _evaluate_deviation(agg_asof, model, ctx.baseline, threshold_sigma=sigma_th)
        return RuleOutcome(
            triggered=result.is_deviated, rule_code='STEP_CHANGE', issue_type='step_change',
            family='monotonic', severity='warn',
            title='多變量偏離基準' if result.is_deviated else '',
            detail=result.describe(), interpretation_limit=_TRIAGE_LIMIT,
            current_value=result.distance, baseline_value=sigma_th,
            evidence={'per_feature_sigma': result.per_feature_sigma,
                      'top_contributors': result.top_contributors},
        )

    # ── 內建簡化版：對角近似（忽略特徵間相關性）──────────────
    row = _latest_ok_row(agg_asof)
    if row is None or ctx.baseline is None:
        return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')
    sigmas = {}
    for f in _STEP_CHANGE_FEATURES:
        stat = ctx.baseline.stats.get(f)
        if stat and f in row.index and not pd.isna(row[f]):
            sigmas[f] = stat.sigma_of(float(row[f]))
    if not sigmas:
        return RuleOutcome.no_trigger('STEP_CHANGE', 'step_change', 'monotonic')
    distance = float(np.sqrt(np.mean(np.square(list(sigmas.values())))))
    triggered = distance > sigma_th
    return RuleOutcome(triggered=triggered, rule_code='STEP_CHANGE', issue_type='step_change',
                        family='monotonic', severity='warn', current_value=distance,
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'per_feature_sigma': sigmas, 'note': 'fallback 對角近似'})


# ──────────────────────────────────────────────────────────
# 以下為純 stub（尚無對應 vibcore 模組）
# ──────────────────────────────────────────────────────────

def rule_vel_high(ctx: RuleContext) -> RuleOutcome:
    """velOA 相對基準超過 N 個標準差。"""
    sigma_th = float(ctx.params.get('sigma', 3.0))
    row = _latest_ok_row(ctx.agg[ctx.agg['ts_hour'] <= ctx.now])
    if row is None or ctx.baseline is None or 'vel_oa' not in ctx.baseline.stats:
        return RuleOutcome.no_trigger('VEL_HIGH', 'vel_high', 'oscillating')
    val = row.get('vel_oa')
    if val is None or pd.isna(val):
        return RuleOutcome.no_trigger('VEL_HIGH', 'vel_high', 'oscillating')
    stat = ctx.baseline.stats['vel_oa']
    sigma = stat.sigma_of(float(val))
    triggered = sigma >= sigma_th
    return RuleOutcome(triggered=triggered, rule_code='VEL_HIGH', issue_type='vel_high',
                        family='oscillating', severity='warn',
                        title='velOA 偏高' if triggered else '',
                        current_value=float(val), baseline_value=stat.median, value_unit='mm/s',
                        interpretation_limit=_TRIAGE_LIMIT, evidence={'sigma': round(sigma, 2)})


def rule_impact_rise(ctx: RuleContext) -> RuleOutcome:
    """accCREST / accKURT 相對基準顯著上升（不判定成因，常見於軸承/潤滑劣化）。"""
    crest_th = float(ctx.params.get('crest_sigma', 2.5))
    kurt_th = float(ctx.params.get('kurt_sigma', 2.5))
    require_both = bool(ctx.params.get('require_both', False))

    row = _latest_ok_row(ctx.agg[ctx.agg['ts_hour'] <= ctx.now])
    if row is None or ctx.baseline is None:
        return RuleOutcome.no_trigger('IMPACT_RISE', 'impact_rise', 'monotonic')

    crest_stat, kurt_stat = ctx.baseline.stats.get('acc_crest'), ctx.baseline.stats.get('acc_kurt')
    crest_sigma = crest_stat.sigma_of(float(row['acc_crest'])) \
        if crest_stat and not pd.isna(row.get('acc_crest')) else None
    kurt_sigma = kurt_stat.sigma_of(float(row['acc_kurt'])) \
        if kurt_stat and not pd.isna(row.get('acc_kurt')) else None

    crest_up = crest_sigma is not None and crest_sigma >= crest_th
    kurt_up = kurt_sigma is not None and kurt_sigma >= kurt_th
    triggered = (crest_up and kurt_up) if require_both else (crest_up or kurt_up)

    return RuleOutcome(triggered=triggered, rule_code='IMPACT_RISE', issue_type='impact_rise',
                        family='monotonic', severity='warn',
                        title='衝擊性指標上升' if triggered else '',
                        current_value=float(row.get('acc_crest')) if not pd.isna(row.get('acc_crest')) else None,
                        value_unit='', interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'crest_sigma': crest_sigma, 'kurt_sigma': kurt_sigma})


def _trend_asof(ctx: RuleContext, metric: str, cfg: TrendConfig) -> TrendResult:
    """
    以 `ctx.now` 為截止時間算趨勢——優先用真實模組 `vibcore.metrics.trend`
    （以實際天數為 x 軸、有完整的涵蓋率把關，見該模組說明），不可用時退回
    本檔的簡化線性回歸 `_linear_trend_stub`。

    真實模組吃「整份 agg，自己篩 ok 列」，因此這裡只需要把 `ts_hour > now`
    的列剪掉（模擬「這天規則引擎只看得到當下已入庫的資料」），不必自己
    先篩 `data_status`。
    """
    agg_asof = ctx.agg[ctx.agg['ts_hour'] <= ctx.now]
    if _compute_trend is not None:
        return _compute_trend(agg_asof, metric, ctx.baseline, cfg=cfg)

    ok = agg_asof[agg_asof['data_status'] == DataStatus.OK] if 'data_status' in agg_asof.columns else agg_asof
    baseline_median = ctx.baseline.stats[metric].median if ctx.baseline and metric in ctx.baseline.stats else None
    return _linear_trend_stub(ok, metric, ctx.now, cfg.min_days, cfg.min_points, cfg.min_r2, baseline_median)


def rule_degrade_trend(ctx: RuleContext, metric: str = 'acc_rms') -> RuleOutcome:
    """
    回歸斜率持續惡化（單一代表性指標：acc_rms，整體加速度能量水準）。

    真實規則層很可能對多個指標分別跑趨勢、挑最劣化的一個回報；這裡先
    固定用 `acc_rms` 當代表，避免同一小時對同一設備因為多指標各自觸發
    而虛增 Finding 數量，回測時對「量級」判斷更保守（寧可少算不要多算）。
    """
    min_days = int(ctx.params.get('min_days', DEFAULT_TREND.min_days))
    min_r2 = float(ctx.params.get('min_r2', DEFAULT_TREND.min_r2))
    slope_th = float(ctx.params.get('slope_pct_per_month', 10))
    cfg = dataclasses.replace(DEFAULT_TREND, min_days=min_days, min_r2=min_r2)

    trend = _trend_asof(ctx, metric, cfg)
    triggered = (trend.is_reliable and trend.direction == 'up'
                 and not pd.isna(trend.slope_pct_per_month)
                 and abs(trend.slope_pct_per_month) >= slope_th)
    return RuleOutcome(triggered=triggered, rule_code='DEGRADE_TREND', issue_type='degradation_trend',
                        family='monotonic', severity='warn',
                        title=f'{metric} 持續劣化' if triggered else '',
                        detail=(f'斜率 {trend.slope_pct_per_month:+.1f}%/月，R²={trend.r2:.2f}，'
                                f'span={trend.span_days:.0f} 天') if trend.n_points >= 2 else trend.note,
                        current_value=trend.slope_pct_per_month, interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'metric': metric, 'confidence': trend.confidence, 'n_points': trend.n_points})


def rule_spectral_shift(ctx: RuleContext) -> RuleOutcome:
    """
    accWeightedMeanFreq 相對基準持續上移（能量往高頻移動，不辨識個別諧波）。

    `rule_config` 的 `shift_pct` 是「累計位移百分比」而非「每月變化率」，
    但趨勢模組（無論真實版或 stub）算的是速率（`slope_pct_per_month`）；
    這裡用 `速率 × (觀察天數 / 30)` 換算回累計位移，語意上等於「這段觀察期
    以來，頻譜重心總共移動了多少百分比」。
    """
    metric = 'acc_weighted_mean_freq'
    shift_pct_th = float(ctx.params.get('shift_pct', 15))
    min_days = int(ctx.params.get('min_days', DEFAULT_TREND.min_days))
    cfg = dataclasses.replace(DEFAULT_TREND, min_days=min_days)

    trend = _trend_asof(ctx, metric, cfg)
    if not trend.is_reliable or pd.isna(trend.slope_pct_per_month) or trend.direction != 'up':
        return RuleOutcome.no_trigger('SPECTRAL_SHIFT', 'spectral_shift', 'monotonic')

    cumulative_shift_pct = trend.slope_pct_per_month * (trend.span_days / 30.0)
    triggered = cumulative_shift_pct >= shift_pct_th
    return RuleOutcome(triggered=triggered, rule_code='SPECTRAL_SHIFT', issue_type='spectral_shift',
                        family='monotonic', severity='warn',
                        title='頻譜重心上移' if triggered else '',
                        detail=f'累計位移 {cumulative_shift_pct:+.1f}%，span={trend.span_days:.0f} 天',
                        current_value=cumulative_shift_pct, value_unit='%',
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'cumulative_shift_pct': round(cumulative_shift_pct, 1),
                                  'confidence': trend.confidence})


def rule_axis_shift(ctx: RuleContext) -> RuleOutcome:
    """排序後三軸能量佔比相對基準持續偏移（trailing 7 天平均 vs 基準）。"""
    ratio_delta_th = float(ctx.params.get('ratio_delta', 0.15))
    if ctx.axis_energy_baseline is None:
        return RuleOutcome.no_trigger('AXIS_SHIFT', 'axis_shift', 'monotonic')
    current = _axis_now(ctx.agg, ctx.now, window_days=7)
    if current is None:
        return RuleOutcome.no_trigger('AXIS_SHIFT', 'axis_shift', 'monotonic')
    deltas = {k: current[k] - ctx.axis_energy_baseline[k] for k in ('major', 'mid', 'minor')
              if k in current and k in ctx.axis_energy_baseline}
    max_delta = max((abs(v) for v in deltas.values()), default=0.0)
    triggered = max_delta >= ratio_delta_th
    return RuleOutcome(triggered=triggered, rule_code='AXIS_SHIFT', issue_type='axis_shift',
                        family='monotonic', severity='warn',
                        title='軸能量分佈偏移' if triggered else '',
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'deltas': {k: round(v, 3) for k, v in deltas.items()}})


def rule_orientation_change(ctx: RuleContext) -> RuleOutcome:
    """軸能量分佈排列跳變（單點 vs 基準），疑似感測器重貼或更換。"""
    ratio_delta_th = float(ctx.params.get('ratio_delta', 0.25))
    if ctx.axis_energy_baseline is None:
        return RuleOutcome.no_trigger('ORIENTATION_CHANGE', 'orientation_change', 'event')
    current = _axis_now(ctx.agg, ctx.now, window_days=None)
    if current is None:
        return RuleOutcome.no_trigger('ORIENTATION_CHANGE', 'orientation_change', 'event')
    deltas = {k: current[k] - ctx.axis_energy_baseline[k] for k in ('major', 'mid', 'minor')
              if k in current and k in ctx.axis_energy_baseline}
    max_delta = max((abs(v) for v in deltas.values()), default=0.0)
    triggered = max_delta >= ratio_delta_th
    return RuleOutcome(triggered=triggered, rule_code='ORIENTATION_CHANGE',
                        issue_type='orientation_change', family='event', severity='warn',
                        title='疑似感測器方向改變' if triggered else '',
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'deltas': {k: round(v, 3) for k, v in deltas.items()}})


def rule_sensor_offline(ctx: RuleContext) -> RuleOutcome:
    """逾時無資料（感測器離線）。優先用 `last_data_at`，沒有則從 agg 推算。"""
    hours_th = float(ctx.params.get('hours', 24))
    last_data_at = ctx.last_data_at
    if last_data_at is None:
        d = ctx.agg[ctx.agg['ts_hour'] <= ctx.now]
        has_data = d[d['data_status'] != DataStatus.NO_DATA] if 'data_status' in d.columns else d
        last_data_at = has_data['ts_hour'].max() if not has_data.empty else None
    if last_data_at is None:
        return RuleOutcome.no_trigger('SENSOR_OFFLINE', 'sensor_offline', 'event')
    hours_since = (ctx.now - last_data_at).total_seconds() / 3600
    triggered = hours_since >= hours_th
    return RuleOutcome(triggered=triggered, rule_code='SENSOR_OFFLINE', issue_type='sensor_offline',
                        family='event', severity='err', title='感測器離線' if triggered else '',
                        detail=f'距上次收到資料已 {hours_since:.1f} 小時' if triggered else '',
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'hours_since_last_data': round(hours_since, 1)})


def rule_data_quality(ctx: RuleContext) -> RuleOutcome:
    """
    近期（trailing 7 天）資料品質：`ok` 佔「應有資料」時數的比例過低。

    分母刻意排除 `not_running`——未運轉是正常狀態，不該被算成資料品質差
    （見 aggregate.py / config.py 對 `DataStatus` 的說明）。
    """
    min_ratio_th = float(ctx.params.get('min_running_ratio', 0.5))
    window = ctx.agg[(ctx.agg['ts_hour'] > ctx.now - pd.Timedelta(days=7)) & (ctx.agg['ts_hour'] <= ctx.now)]
    if window.empty or 'data_status' not in window.columns:
        return RuleOutcome.no_trigger('DATA_QUALITY', 'data_quality', 'event')
    counts = window['data_status'].value_counts()
    ok_hours = int(counts.get(DataStatus.OK, 0))
    denom = ok_hours + int(counts.get(DataStatus.PARTIAL, 0)) + int(counts.get(DataStatus.NO_DATA, 0))
    if denom == 0:
        return RuleOutcome.no_trigger('DATA_QUALITY', 'data_quality', 'event')
    ratio = ok_hours / denom
    triggered = ratio < min_ratio_th
    return RuleOutcome(triggered=triggered, rule_code='DATA_QUALITY', issue_type='data_quality',
                        family='event', severity='warn', title='資料品質不足' if triggered else '',
                        current_value=round(ratio, 3), interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'ok_ratio_7d': round(ratio, 3)})


def rule_sensor_saturation(ctx: RuleContext) -> RuleOutcome:
    """accPEAK 逼近量程滿刻度（trailing 7 天內任一筆），峰值類指標將失真。"""
    pct_th = float(ctx.params.get('full_scale_pct', 90)) / 100.0
    recent = _recent_ok(ctx.agg, ctx.now, window_days=7)
    if recent.empty or 'acc_peak' not in recent.columns or recent['acc_peak'].isna().all():
        return RuleOutcome.no_trigger('SENSOR_SATURATION', 'sensor_saturation', 'event')
    max_peak = float(recent['acc_peak'].max())
    threshold = FULL_SCALE_MS2 * pct_th
    triggered = max_peak >= threshold
    return RuleOutcome(triggered=triggered, rule_code='SENSOR_SATURATION', issue_type='sensor_saturation',
                        family='event', severity='warn', title='感測器接近飽和' if triggered else '',
                        current_value=max_peak, baseline_value=threshold, value_unit='m/s²',
                        interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'pct_of_full_scale': round(max_peak / FULL_SCALE_MS2 * 100, 1)})


def rule_standby_no_runtime(ctx: RuleContext) -> RuleOutcome:
    """備機超過 N 天未運轉，建議試車。非備機一律不適用。"""
    if not ctx.device.is_standby:
        return RuleOutcome.no_trigger('STANDBY_NO_RUNTIME', 'standby_no_runtime', 'event')
    days_th = float(ctx.params.get('days', 30))
    d = ctx.agg[ctx.agg['ts_hour'] <= ctx.now]
    running = d[d.get('n_samples_running', 0) > 0] if 'n_samples_running' in d.columns else d.iloc[0:0]
    if running.empty:
        last_run = d['ts_hour'].min() if not d.empty else ctx.now
    else:
        last_run = running['ts_hour'].max()
    days_since = (ctx.now - last_run).total_seconds() / 86400
    triggered = days_since >= days_th
    return RuleOutcome(triggered=triggered, rule_code='STANDBY_NO_RUNTIME', issue_type='standby_no_runtime',
                        family='event', severity='warn', title='備機長期未運轉' if triggered else '',
                        current_value=round(days_since, 1), interpretation_limit=_TRIAGE_LIMIT,
                        evidence={'days_since_last_run': round(days_since, 1)})


# ──────────────────────────────────────────────────────────
# 登錄表 — 之後 vibcore.rules 完成後，改這裡的匯入來源即可
# ──────────────────────────────────────────────────────────

REGISTRY: dict[str, RuleFunc] = {
    'ISO_ZONE': rule_iso_zone,
    'ISO_CLASS_SUSPECT': rule_iso_class_suspect,
    'VEL_HIGH': rule_vel_high,
    'IMPACT_RISE': rule_impact_rise,
    'DEGRADE_TREND': rule_degrade_trend,
    'SPECTRAL_SHIFT': rule_spectral_shift,
    'AXIS_SHIFT': rule_axis_shift,
    'STEP_CHANGE': rule_step_change,
    'ORIENTATION_CHANGE': rule_orientation_change,
    'SENSOR_OFFLINE': rule_sensor_offline,
    'DATA_QUALITY': rule_data_quality,
    'SENSOR_SATURATION': rule_sensor_saturation,
    'STANDBY_NO_RUNTIME': rule_standby_no_runtime,
}


def _try_import_real_registry() -> tuple[dict[str, RuleFunc], frozenset[str]]:
    """
    嘗試整批接上真實規則引擎（例如 `vibcore.rules.REGISTRY`）。

    找不到時安靜地維持 stub 登錄表；找到但只涵蓋部分規則代碼時，
    採「逐條覆蓋」而非整批取代——已完工的規則優先用真的，其餘繼續用
    stub 頂著，讓回測不會因為某條規則還沒寫完就整個跑不動。

    第二個回傳值是「實際接上真實實作的規則代碼」。報告必須據此標示，
    不能寫死——寫死的字串會隨程式演進而過時，把可信的結果標成 stub
    （使用者因此不敢採用正確的門檻），或把 stub 標成真實（更糟）。
    """
    merged = dict(REGISTRY)
    real_codes: set[str] = set()
    try:
        from vibcore.rules import REGISTRY as real_registry  # type: ignore
        for rule_code, fn in real_registry.items():
            merged[rule_code] = fn
            real_codes.add(rule_code)
            logger.info(f"規則 {rule_code} 已接上真實實作 vibcore.rules")
    except ImportError:
        pass
    return merged, frozenset(real_codes)


REGISTRY, REAL_RULE_CODES = _try_import_real_registry()

#: 仍使用 validate/rules_stub.py 簡化版的規則代碼（正常情況應為空集合）
STUB_RULE_CODES = frozenset(REGISTRY) - REAL_RULE_CODES
