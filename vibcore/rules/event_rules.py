"""
event_rules.py — 六條事件型規則（family='event'）

事件型規則判定的不是「振動水準本身異常」，而是「這份資料還能不能相信、
這台機器是不是照預期的樣子在運作」。這一層若判錯，後果比其他規則更嚴重：

- 把 `not_running`（正常停機）誤判成 `SENSOR_OFFLINE`，週報會被假警報洗版，
  很快就沒人看；
- 把 `no_data`（真的斷線）漏判，設備會在「沒有任何告警」的假象下持續失聯，
  比誤報更危險——沒有異常，最容易被誤讀成一切正常。

因此本檔的核心紀律只有一條：**`data_status` 的四種狀態意義完全不同，
不可互相替代**（見 `vibcore/pipeline/aggregate.py` 與 `vibcore/config.py`
的 `DataStatus` 說明）：

    ok           資料完整且運轉中 → 可信、可判定
    partial      有資料但筆數不足 → 數字不可信，不能拿來判定「異常」
    no_data      完全無資料（斷線）→ 設備/通訊異常，SENSOR_OFFLINE 抓這個
    not_running  有資料但未運轉 → 正常狀態，絕對不能判異常

依此，`SENSOR_OFFLINE` 只認 `no_data`；`DATA_QUALITY` 計算涵蓋率時把
`not_running` 排除在分母之外（備機一週只跑兩小時不代表資料品質差）；
`STANDBY_NO_RUNTIME` 反過來——它就是為了逮到「一直 `not_running`」的
備機而存在，不能被 `SENSOR_OFFLINE`/`DATA_QUALITY` 搶走判定權。

另外兩條護欄（見 `vibcore/types.py` 與計畫書 §8.2）：

1. **`interpretation_limit` 一律必填**，且必須針對「這個證據能撐到哪裡」
   寫具體說明，不能套用同一句空話——尤其 `SENSOR_OFFLINE`：斷線期間沒有
   任何告警，最容易被誤讀成「設備正常」，必須在這裡把話說死。
2. **title / detail 不做故障類型判定**，不出現「軸承／對心／不平衡／
   鬆動／故障／更換／壽命」等字眼。本檔定位是分流（triage），不是診斷。
"""

from __future__ import annotations

import logging

import pandas as pd

from vibcore.config import DataStatus, FULL_SCALE_MS2, G_TO_MS2, SATURATION_PCT, SENSOR_RANGE_G
from vibcore.metrics.iso import classify_zone, evaluate_iso
from vibcore.rules.engine import register
from vibcore.types import RuleContext, RuleOutcome

logger = logging.getLogger(__name__)

#: DATA_QUALITY / SENSOR_SATURATION 預設觀察窗口。事件型規則看的是
#: 「現在還信不信得過這個量測點」，用太長的窗口會讓早已恢復正常的舊
#: 問題繼續拖著告警；一週對齊週報節奏，也是多數場域的日夜/負載週期。
_DEFAULT_WINDOW_DAYS = 7

#: DATA_QUALITY 判定涵蓋率時，窗口內至少要有這麼多小時的「應有資料」
#: （ok+partial+no_data，不含 not_running）才判定——否則一台設備才剛
#: 開始監測兩小時、其中一小時剛好斷訊，比例算出來就是 50%，會誤觸發。
_DATA_QUALITY_MIN_DENOM_HOURS = 12

#: 判定「零值卡死」的容差；浮點數比較不可直接用 == 0。
_ZERO_EPS = 1e-9

#: 前端 `iso10816`（聚合後欄位 `iso_zone_frontend`）為 1..4 的數字分級，
#: 本系統內部一律用字母 Zone；交叉比對前先在此統一轉換，避免兩種表示法
#: 在各處分別互轉、彼此漂移。語意見 db/schema.sql 的欄位註解與
#: docs/DATA_CONTRACT.md §3.2：1=Zone A（最佳）...4=Zone D（最差）。
_FRONTEND_ZONE_MAP: dict[int, str] = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}

#: Zone 字母的嚴重度排序，只用來判斷「哪一邊判得比較嚴重」，不用於
#: 其他數值運算。
_ZONE_RANK: dict[str, int] = {'A': 0, 'B': 1, 'C': 2, 'D': 3}


# ──────────────────────────────────────────────────────────
# 共用小工具
# ──────────────────────────────────────────────────────────

def _asof(agg: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """只看得到 `now` 之前（含）已入庫的資料，避免規則偷看未來。"""
    if agg is None or agg.empty or 'ts_hour' not in agg.columns:
        return agg if agg is not None else pd.DataFrame()
    return agg[agg['ts_hour'] <= now]


def _window(agg: pd.DataFrame, now: pd.Timestamp, days: float) -> pd.DataFrame:
    """截取 `(now - days, now]` 的資料，作為「近期」窗口。"""
    d = _asof(agg, now)
    if d.empty:
        return d
    cutoff = now - pd.Timedelta(days=days)
    return d[d['ts_hour'] > cutoff]


def _latest_row(d: pd.DataFrame) -> pd.Series | None:
    """依 `ts_hour` 取最新一列；空 DataFrame 回傳 None。"""
    if d is None or d.empty or 'ts_hour' not in d.columns:
        return None
    return d.sort_values('ts_hour').iloc[-1]


def _has_data_mask(d: pd.DataFrame) -> pd.Series:
    """`data_status` 欄不存在時保守視為『有資料』，避免誤判整批斷線。"""
    if 'data_status' not in d.columns:
        return pd.Series(True, index=d.index)
    return d['data_status'] != DataStatus.NO_DATA


# ──────────────────────────────────────────────────────────
# SENSOR_OFFLINE — 逾時無資料
# ──────────────────────────────────────────────────────────

@register('SENSOR_OFFLINE')
def sensor_offline(ctx: RuleContext) -> RuleOutcome:
    """
    逾時無資料（感測器/通訊斷線）。

    抓的是 `no_data`，不是 `not_running`——備機整週停機是它該有的樣子，
    不該被這條規則盯上。判定「最後一次收到資料」優先用 `ctx.last_data_at`
    （由呼叫端從資料庫查得，通常比聚合表更即時）；沒有時退而求其次，從
    `agg` 找最後一列 `data_status != no_data` 的 `ts_hour`——`ok` /
    `partial` / `not_running` 都算「有收到資料」，只有 `no_data` 才是真的
    什麼都沒收到。
    """
    rule_code, issue_type, family = 'SENSOR_OFFLINE', 'sensor_offline', 'event'
    hours_th = float(ctx.params.get('hours', 24))

    is_lower_bound = False
    last_data_at = ctx.last_data_at
    if last_data_at is None:
        d = _asof(ctx.agg, ctx.now)
        if d.empty:
            return RuleOutcome.no_trigger(rule_code, issue_type, family)
        with_data = d[_has_data_mask(d)]
        if not with_data.empty:
            last_data_at = with_data['ts_hour'].max()
        else:
            # 觀測窗口內從頭到尾都是 no_data——沒有任何一筆「有資料」的
            # 列可供對照。這正是最需要告警的情況：若因此判定「無從得知」
            # 而放棄觸發，斷線最久的設備反而永遠不會被抓到。改用窗口內
            # 最早一筆 no_data 的時刻當保守下界——真正的斷線起點只會更早
            # 不會更晚，用它算出的 hours_since 絕不會虛報過長，觸發時是
            # 站得住腳的最低限度證據。
            last_data_at = d['ts_hour'].min()
            is_lower_bound = True

    if last_data_at is None or pd.isna(last_data_at):
        # 完全沒有任何一筆資料可供比對——無從判定「逾時」，交由資料
        # 品質/上線流程處理，不硬算出一個沒有意義的「斷線」。
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    hours_since = (ctx.now - last_data_at).total_seconds() / 3600
    triggered = hours_since >= hours_th
    if not triggered:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    return RuleOutcome(
        triggered=True, rule_code=rule_code, issue_type=issue_type, family=family,
        severity='err',
        title='感測器逾時無資料',
        detail=(f'距上次收到資料已{"至少" if is_lower_bound else ""} {hours_since:.1f} 小時'
                f'（門檻 {hours_th:.0f} 小時）。'),
        interpretation_limit=(
            '斷線期間所有健康指標均無法評估，不代表設備狀態正常；'
            '在確認通訊、供電與感測器本身恢復正常前，不可用「沒有其他告警」'
            '推論設備目前狀態良好。'
        ),
        current_value=round(hours_since, 1), baseline_value=hours_th, value_unit='hr',
        evidence={
            'hours_since_last_data': round(hours_since, 1),
            'hours_threshold': hours_th,
            'last_data_at': str(last_data_at),
            'last_data_at_is_lower_bound': is_lower_bound,
        },
        target_type='point',
    )


# ──────────────────────────────────────────────────────────
# DATA_QUALITY — 缺漏、零值、運轉樣本不足
# ──────────────────────────────────────────────────────────

@register('DATA_QUALITY')
def data_quality(ctx: RuleContext) -> RuleOutcome:
    """
    近期資料品質不足：`ok` 佔「應有資料」時數的比例過低，或關鍵指標
    長時間卡在零值（感測器可能未實際量到訊號）。

    分母刻意排除 `not_running`——未運轉是正常狀態，不能因為一台備機一週
    只跑兩小時就判定它「資料品質異常」。分母只計 `ok + partial + no_data`
    （三者都是「這個小時本該有可信資料」的情境，差別只在有沒有達標）。
    """
    rule_code, issue_type, family = 'DATA_QUALITY', 'data_quality', 'event'
    min_ratio_th = float(ctx.params.get('min_running_ratio', 0.5))
    window_days = float(ctx.params.get('window_days', _DEFAULT_WINDOW_DAYS))
    zero_ratio_th = float(ctx.params.get('zero_value_ratio', 0.3))

    win = _window(ctx.agg, ctx.now, window_days)
    if win.empty or 'data_status' not in win.columns:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    counts = win['data_status'].value_counts()
    ok_hours = int(counts.get(DataStatus.OK, 0))
    partial_hours = int(counts.get(DataStatus.PARTIAL, 0))
    no_data_hours = int(counts.get(DataStatus.NO_DATA, 0))
    denom = ok_hours + partial_hours + no_data_hours

    if denom < _DATA_QUALITY_MIN_DENOM_HOURS:
        # 觀察窗口內「應有資料」的時數太少，比例會被單一小時放大失真，
        # 硬算沒有意義，讓 SENSOR_OFFLINE / 之後的窗口再判定。
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    ok_ratio = ok_hours / denom
    coverage_bad = ok_ratio < min_ratio_th

    # 零值卡死：即使小時被判為 ok（樣本數足夠），但關鍵指標長期是 0，
    # 代表感測器很可能沒有實際量到訊號（脫落、接線異常），這是
    # data_status 判不出來的另一種資料品質問題。
    zero_ratio = None
    zero_bad = False
    ok_rows = win[win['data_status'] == DataStatus.OK]
    for metric in ('vel_rms', 'acc_rms'):
        if metric not in ok_rows.columns or ok_rows.empty:
            continue
        vals = ok_rows[metric].dropna()
        if len(vals) == 0:
            continue
        ratio = float((vals.abs() < _ZERO_EPS).mean())
        if zero_ratio is None or ratio > zero_ratio:
            zero_ratio = ratio
        if ratio >= zero_ratio_th:
            zero_bad = True

    triggered = coverage_bad or zero_bad
    if not triggered:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    reasons = []
    if coverage_bad:
        reasons.append(f'可信資料佔比 {ok_ratio:.0%}（門檻 {min_ratio_th:.0%}）')
    if zero_bad:
        reasons.append(f'關鍵指標零值佔比 {zero_ratio:.0%}（門檻 {zero_ratio_th:.0%}）')

    return RuleOutcome(
        triggered=True, rule_code=rule_code, issue_type=issue_type, family=family,
        severity='warn',
        title='資料品質不足',
        detail=f'近 {window_days:.0f} 天：' + '；'.join(reasons) + '。',
        interpretation_limit=(
            '此區間內的數值與趨勢皆不可採信，不得據此判定設備振動狀態的好壞；'
            '須先排除量測本身的問題（例如接線、供電、感測器固定狀態），'
            '待資料品質恢復後才能重新評估。'
        ),
        current_value=round(ok_ratio, 3), baseline_value=min_ratio_th,
        evidence={
            'window_days': window_days,
            'ok_hours': ok_hours, 'partial_hours': partial_hours, 'no_data_hours': no_data_hours,
            'ok_ratio': round(ok_ratio, 3),
            'zero_value_ratio': round(zero_ratio, 3) if zero_ratio is not None else None,
        },
        target_type='point',
    )


# ──────────────────────────────────────────────────────────
# SENSOR_SATURATION — acc_peak 逼近量程滿刻度
# ──────────────────────────────────────────────────────────

@register('SENSOR_SATURATION')
def sensor_saturation(ctx: RuleContext) -> RuleOutcome:
    """
    `acc_peak` 逼近感測器量程滿刻度（預設 ±4g = 39.23 m/s²）。

    一旦訊號被裁切，`acc_peak` 本身以及依賴它的衍生指標（crest 等）都會
    失真——這不是「振動變大」的證據，是「量測本身已經量不準了」的證據，
    兩者的處置完全不同，所以獨立成一條 event 規則而不是併進 `VEL_HIGH`
    之類的位準規則。

    看的是近期窗口內的最大值而非單一最新一筆：飽和往往是瞬時衝擊造成，
    只看最新一小時容易錯過幾天前發生過的裁切事件。
    """
    rule_code, issue_type, family = 'SENSOR_SATURATION', 'sensor_saturation', 'event'
    pct_th = float(ctx.params.get('full_scale_pct', SATURATION_PCT)) / 100.0
    range_g = float(ctx.params.get('range_g', SENSOR_RANGE_G))
    full_scale = range_g * G_TO_MS2 if range_g != SENSOR_RANGE_G else FULL_SCALE_MS2
    window_days = float(ctx.params.get('window_days', _DEFAULT_WINDOW_DAYS))

    win = _window(ctx.agg, ctx.now, window_days)
    if win.empty or 'acc_peak' not in win.columns:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    peaks = win.dropna(subset=['acc_peak'])
    if peaks.empty:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    idx = peaks['acc_peak'].idxmax()
    max_peak = float(peaks.loc[idx, 'acc_peak'])
    at_hour = peaks.loc[idx, 'ts_hour'] if 'ts_hour' in peaks.columns else None
    threshold = full_scale * pct_th
    triggered = max_peak >= threshold
    if not triggered:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    pct_of_full = max_peak / full_scale * 100
    return RuleOutcome(
        triggered=True, rule_code=rule_code, issue_type=issue_type, family=family,
        severity='warn',
        title='感測器接近飽和',
        detail=(f'近 {window_days:.0f} 天內 accPEAK 最高達 {max_peak:.2f} m/s²，'
                f'約為量程滿刻度的 {pct_of_full:.0f}%（門檻 {pct_th:.0%}）。'),
        interpretation_limit=(
            '峰值已逼近或超過感測器量程，accPEAK 與依賴峰值的衍生指標'
            '（如 crest 相關量）可能已被裁切失真，此區間不可用於判定振動'
            '是否異常，也不可拿來與歷史趨勢比較；需確認安裝方式與量測範圍'
            '是否適合現場工況。'
        ),
        current_value=round(max_peak, 2), baseline_value=round(threshold, 2), value_unit='m/s²',
        evidence={
            'window_days': window_days,
            'full_scale_ms2': round(full_scale, 2),
            'pct_of_full_scale': round(pct_of_full, 1),
            'at_hour': str(at_hour) if at_hour is not None else None,
        },
        target_type='point',
    )


# ──────────────────────────────────────────────────────────
# STANDBY_NO_RUNTIME — 備機超過 N 天未運轉
# ──────────────────────────────────────────────────────────

@register('STANDBY_NO_RUNTIME')
def standby_no_runtime(ctx: RuleContext) -> RuleOutcome:
    """
    備機超過 N 天未運轉，建議安排試車。

    只對 `ctx.device.is_standby` 為 True 的設備判定——正常運轉設備長期
    不運轉是別的問題（多半已經是 `SENSOR_OFFLINE` 或維修停機），不該套
    用這條規則的邏輯與措辭。判定「最後一次運轉」用 `n_samples_running`
    是否 > 0，而不是 `data_status == ok`：只要那個小時裡有任何運轉樣本
    就算開機過，即使樣本數不足以達到 `ok`（例如剛啟動沒多久又停機）。
    """
    rule_code, issue_type, family = 'STANDBY_NO_RUNTIME', 'standby_no_runtime', 'event'
    if not ctx.device.is_standby:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    days_th = float(ctx.params.get('days', 30))
    d = _asof(ctx.agg, ctx.now)
    if d.empty or 'n_samples_running' not in d.columns:
        # 沒有任何聚合資料可供比對——無從判定運轉與否，不硬算。
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    running = d[d['n_samples_running'] > 0]
    if running.empty:
        # 觀測範圍內從未偵測到運轉樣本：以資料起點當作「已知未運轉」的
        # 起算點（無法斷言更早之前的狀態），而不是憑空給一個 0 天。
        last_run = d['ts_hour'].min()
    else:
        last_run = running['ts_hour'].max()

    days_since = (ctx.now - last_run).total_seconds() / 86400
    triggered = days_since >= days_th
    if not triggered:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    return RuleOutcome(
        triggered=True, rule_code=rule_code, issue_type=issue_type, family=family,
        severity='warn',
        title='備機長期未啟動',
        detail=f'距上次偵測到運轉樣本已 {days_since:.1f} 天（門檻 {days_th:.0f} 天）。',
        interpretation_limit=(
            '本判定僅代表這台備機長期靜置，不代表其目前的機械狀態；'
            '長期靜置設備重新啟動時，建議先以低負載試車觀察，再視情況'
            '恢復正常運轉排程。'
        ),
        current_value=round(days_since, 1), baseline_value=days_th, value_unit='day',
        evidence={'days_since_last_run': round(days_since, 1), 'last_run_at': str(last_run)},
        target_type='device',
    )


# ──────────────────────────────────────────────────────────
# ORIENTATION_CHANGE — 軸能量分佈排列跳變
# ──────────────────────────────────────────────────────────

def _axis_deltas(current: dict, baseline: dict) -> dict[str, float]:
    return {k: float(current[k]) - float(baseline[k])
            for k in ('major', 'mid', 'minor') if k in current and k in baseline}


@register('ORIENTATION_CHANGE')
def orientation_change(ctx: RuleContext) -> RuleOutcome:
    """
    軸能量分佈（排序後 major/mid/minor 佔比）相對基準期出現跳變。

    抓的是「跳變」；持續性、緩慢的偏移屬於 `AXIS_SHIFT`（monotonic）的
    職責，兩者刻意分開，因為成因推論的措辭完全不同——跳變較像一次性的
    安裝變動，緩慢偏移較像漸進的工況或結構變化。

    有兩道防線，都是實測回測後補上的（68 台設備 33 週觸發 93 次，平均每台
    5.5 次，遠高於「重貼感測器」這種罕見事件該有的頻率）：

    1. **能量門檻**：佔比是歸一化的結果，設備接近停機時三軸都貼近雜訊，
       佔比會劇烈跳動。只要當下的三軸合成量值明顯低於基準期水準，這組
       佔比就不具可比性，直接不判定。單看佔比無從察覺這件事。
    2. **持續性**：早期版本只比對最新一筆可信資料，而規則是每天跑一次，
       等於每天拿一個隨機小時去擲骰子。改為要求連續數筆都超出門檻——
       感測器真的被移位不會只有一小時異常，雜訊則不會連續數筆同向偏離。

    誠實的限制：本系統**無法區分**這個跳變是感測器被重新黏貼/移位造成，
    還是設備本身振動方向真的改變了——排序後的能量分佈刻意不保留 x/y/z
    座標語意（見 `aggregate.py`），這正是它的價值（與貼裝方向無關）也
    是它的極限（回推不出「發生了什麼」）。因此判定結果只能提示「去核對
    近期是否動過感測器」，不能直接寫成任何一種結論。
    """
    rule_code, issue_type, family = 'ORIENTATION_CHANGE', 'orientation_change', 'event'
    ratio_delta_th = float(ctx.params.get('ratio_delta', 0.25))
    consecutive = max(1, int(ctx.params.get('consecutive_readings', 3)))
    min_energy_ratio = float(ctx.params.get('min_energy_ratio', 0.3))

    baseline = ctx.axis_energy_baseline
    if not baseline:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    d = _asof(ctx.agg, ctx.now)
    if d.empty or 'data_status' not in d.columns or 'axis_energy_sorted' not in d.columns:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    ok = d[d['data_status'] == DataStatus.OK].sort_values('ts_hour')
    recent = [v for v in ok['axis_energy_sorted'].tail(consecutive) if isinstance(v, dict)]
    if len(recent) < consecutive:
        # 資料不足以判斷持續性，寧可不判定也不要拿單筆下結論
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    # 能量門檻：基準期沒帶 energy 的舊資料（例如既有 DB 中的基準）跳過此檢查，
    # 維持相容；有帶的話，能量明顯偏低的小時其佔比不可比，直接不判定。
    base_energy = baseline.get('energy')
    if isinstance(base_energy, (int, float)) and base_energy > 0:
        energies = [c.get('energy') for c in recent]
        if any(not isinstance(e, (int, float)) or e < base_energy * min_energy_ratio
               for e in energies):
            return RuleOutcome.no_trigger(rule_code, issue_type, family)

    per_reading = [_axis_deltas(c, baseline) for c in recent]
    if any(not p for p in per_reading):
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    # 取「連續數筆中最小的那個最大偏移」——全部都要超標才算數，
    # 回報時用最保守的數字，避免用尖峰值誇大變化幅度。
    per_reading_max = [max(abs(v) for v in p.values()) for p in per_reading]
    max_delta = min(per_reading_max)
    if max_delta < ratio_delta_th:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    current = recent[-1]
    deltas = per_reading[-1]

    return RuleOutcome(
        triggered=True, rule_code=rule_code, issue_type=issue_type, family=family,
        severity='warn',
        title='軸能量分佈排列跳變',
        detail=(f'三軸能量佔比（排序後）與基準期相比最大變化 {max_delta:.0%}'
                f'（門檻 {ratio_delta_th:.0%}），且連續 {consecutive} 筆可信'
                f'資料皆超出門檻。'),
        interpretation_limit=(
            '軸能量分佈改變可能來自感測器重新黏貼或鬆脫移位，也可能來自'
            '設備本身振動方向改變，本系統無法區分兩者成因；建議先確認'
            '近期是否更換或重新安裝過感測器，再判斷是否需要重建基準期或'
            '進一步檢查。'
        ),
        current_value=round(max_delta, 3), baseline_value=ratio_delta_th,
        evidence={
            'current': {k: round(v, 4) for k, v in current.items()},
            'baseline': {k: round(float(v), 4) for k, v in baseline.items() if k in ('major', 'mid', 'minor')},
            'deltas': {k: round(v, 4) for k, v in deltas.items()},
        },
        target_type='point',
    )


# ──────────────────────────────────────────────────────────
# ISO_CLASS_SUSPECT — ISO 等級疑似填錯
# ──────────────────────────────────────────────────────────

def _frontend_zone_cross_check(agg_asof: pd.DataFrame,
                                machine_class: str,
                                consecutive: int) -> dict | None:
    """
    比對「本系統依 `evaluate_iso()` 同一套門檻算出的 Zone」與「前端已算好
    寫在 `iso_zone_frontend` 欄位的 Zone」，找持續性的不一致。

    這條檢查的立足點跟 `evaluate_iso()` 裡原本的合理性檢查不同：原本那條
    只看「我們自己這一套是否內部矛盾」（基準期實測值 vs 我們指派的等級）；
    這裡看的是「我們這一套跟前端那一套，結論是否對得起來」——兩邊各自
    可能對，也可能都有各自的機械等級假設但其中一邊填錯了，本函式本身
    無從分辨，只負責找出「持續對不起來」這件事。

    只在能同時取得可信 velRMS（`data_status == ok`）與有效前端 Zone
    （`iso_zone_frontend` ∈ {1,2,3,4}）的小時上比較；任一邊缺席就跳過
    該小時，不強行湊數，也不把「缺席」當成「一致」或「不一致」。

    要求連續 `consecutive` 筆「有效可比對」的小時都不一致，且不一致的
    方向（本系統較嚴 / 前端較嚴）前後一致，才視為持續性落差：
    - 單筆不一致：兩邊的取樣視窗、資料到齊時間點本來就可能有些微差異，
      落在 Zone 邊界附近時一筆抖動很常見，不足以下「台帳有問題」的結論；
    - 方向忽正忽負：代表兩邊在邊界附近彼此穿插，比較像雜訊而非穩定的
      系統性落差（例如兩套機械等級假設，換算後門檻剛好很接近）。

    Args:
        agg_asof: 已用 `_asof()` 篩過「不偷看未來」的聚合資料。
        machine_class: 本系統依台帳認定的機械等級（呼叫端已確認
            `evaluate_iso(...).applicable` 為 True 才會傳進來）。
        consecutive: 連續筆數門檻（對應規則參數
            `frontend_consecutive_readings`）。

    Returns:
        None 代表沒有觸發（含：欄位不存在、資料不足、方向不一致等，
        全部安靜地視為「沒有可用的交叉檢查結果」，不拋錯）；否則回傳
        dict，含不一致方向與最新一筆兩邊的 Zone，供組裝 `RuleOutcome`。
    """
    if agg_asof is None or agg_asof.empty:
        return None
    if 'iso_zone_frontend' not in agg_asof.columns:
        # 前端沒有帶這個欄位（舊資料、或該版本的前端未輸出）——這是
        # 「無法比對」，不是「比對後一致」，兩者不可混為一談，但兩者的
        # 處置一樣：安靜地不觸發，退回只看本系統自己判定的行為。
        return None
    if 'data_status' not in agg_asof.columns or 'vel_rms' not in agg_asof.columns:
        return None

    ok = agg_asof[agg_asof['data_status'] == DataStatus.OK].sort_values('ts_hour')
    if ok.empty:
        return None

    comparable = []
    for _, row in ok.iterrows():
        vel_rms = row.get('vel_rms')
        fz_raw = row.get('iso_zone_frontend')
        if pd.isna(vel_rms) or pd.isna(fz_raw):
            continue
        try:
            fz_int = int(fz_raw)
        except (TypeError, ValueError):
            continue
        if fz_int not in _FRONTEND_ZONE_MAP:
            continue
        our_zone = classify_zone(float(vel_rms), machine_class)
        if our_zone is None:
            continue
        comparable.append({
            'ts_hour': row.get('ts_hour'),
            'vel_rms': float(vel_rms),
            'our_zone': our_zone,
            'frontend_zone': _FRONTEND_ZONE_MAP[fz_int],
        })

    if len(comparable) < consecutive:
        # 有欄位、但可比對的小時數不夠——多半是這個功能剛上線沒多久，
        # 舊資料沒有回填 iso_zone_frontend，寧可等資料夠了再判斷。
        return None

    recent = comparable[-consecutive:]
    diffs = [_ZONE_RANK[r['our_zone']] - _ZONE_RANK[r['frontend_zone']] for r in recent]
    if any(d == 0 for d in diffs):
        # 連續窗口內只要有一筆兩邊一致，就不構成「持續」不一致。
        return None
    signs = {1 if d > 0 else -1 for d in diffs}
    if len(signs) != 1:
        # 方向不穩定：這一筆我們比較嚴、下一筆前端比較嚴，不像是穩定的
        # 系統性落差（例如兩邊機械等級假設不同），較像邊界附近的雜訊。
        return None

    direction = 'ours_worse' if signs.pop() > 0 else 'frontend_worse'
    latest = recent[-1]
    return {
        'direction': direction,
        'consecutive': consecutive,
        'our_zone': latest['our_zone'],
        'frontend_zone': latest['frontend_zone'],
        'vel_rms': latest['vel_rms'],
        'at_hour': str(latest['ts_hour']) if latest['ts_hour'] is not None else None,
        'readings': [
            {'ts_hour': str(r['ts_hour']) if r['ts_hour'] is not None else None,
             'vel_rms': round(r['vel_rms'], 3),
             'our_zone': r['our_zone'], 'frontend_zone': r['frontend_zone']}
            for r in recent
        ],
    }


@register('ISO_CLASS_SUSPECT')
def iso_class_suspect(ctx: RuleContext) -> RuleOutcome:
    """
    ISO 等級疑似填錯——兩條獨立的判準，任一成立即觸發：

    1. **基準期水準檢查**（原有邏輯，委由 `evaluate_iso()` 執行，見該
       模組說明）：基準期 velRMS 中位數已超過所指派等級的 B/C 界。這一
       條檢查的是「我們自己這套判定內部有沒有矛盾」——不重新發明一套
       簡化版判準，避免兩處邏輯漂移。

    2. **前端交叉檢查**（新增，見 `_frontend_zone_cross_check()`）：同一
       小時，本系統用 `evaluate_iso()` 同一套門檻算出的 Zone，是否與前端
       已經算好、隨資料一起送來的 `iso_zone_frontend` 欄位**持續**兜不
       起來。連續不一致要求 `consecutive` 筆、且方向一致，見該函式
       docstring；單筆不一致可能只是門檻邊界抖動，刻意不觸發。

    **這兩條檢查在定位上其實是同一件事的兩種偵測方式，但成因推論完全
    不同，務必分開理解：**

    - 條件 1 抓的是「我們指派的等級，跟我們自己量到的振動水準對不起來」；
    - 條件 2 抓的是「我們指派的等級，跟前端那一套算出來的結論對不起來」，
      而**本系統不知道前端用的是哪一個機械等級**在算 `iso10816`——換言之，
      條件 2 不一定代表本系統的等級填錯，也可能是前端那一側的假設有誤，
      甚至兩邊都有各自的依據但對同一台設備做了不同認定。**這是台帳
      （設備機械等級）資訊本身的問題，不是設備振動異常的問題**——
      因此：
        * severity 維持 `warn`（不是 `err`）；
        * 文字導向「請核對這台設備的機械等級（ISO10816_code）是否填
          對」，而不是「請工程師去現場複測」；
        * 觸發文字會明講不一致的**方向**（本系統判得比較嚴重、或前端
          判得比較嚴重）——兩者代表的落差方向不同，但**哪一邊的等級
          假設是對的，本系統無法判斷**，這一點寫在 `interpretation_limit`。

    **前提與退回行為**（安靜，不拋錯）：
    - `evaluate_iso()` 對未分級設備（`class_source == 'unset'`）回傳
      `applicable=False`；此時本系統自己都算不出 Zone 可比，條件 2 不
      執行——沒有可比較的基準，勉強比對只會產生沒有意義的雜訊。
    - `iso_zone_frontend` 欄位不存在（舊資料、或該版本前端未輸出這個
      欄位）時，條件 2 安靜地視為「無法比對」而不觸發，行為等同這個
      功能上線前的舊版——不會因為欄位不存在而誤觸發，也不會因此拋錯。
    - 兩條檢查共用同一道既有的 `ctx.baseline is None` 前置檢查（本規則
      原本就只在基準期已建立後才判定）；代價是設備剛上線、基準期尚未
      建立前，即使前端與本系統的 Zone 已經持續兜不起來也不會提早示警，
      但此時任何判定的證據都還不穩固，與本規則其餘部分的節奏一致，
      故沿用而不特別放寬。

    參數（皆讀自 `ctx.params`，seed 由呼叫端維護）：
    - `frontend_consecutive_readings`（建議預設 3）：條件 2 要求的連續
      可比對小時數，設計取捨同 `ORIENTATION_CHANGE` 的
      `consecutive_readings`——太小易被邊界抖動觸發，太大則反應太慢。

    **強制欄位**：`interpretation_limit` 誠實說明本系統不知道前端算
    `iso10816` 時假設了哪一個機械等級，因此無法判斷條件 2 觸發時是
    「本系統的台帳填錯」還是「前端的等級假設有誤」，只能提示雙方都去
    核對台帳，不可據此直接認定任何一邊的 Zone 結論可信。
    """
    rule_code, issue_type, family = 'ISO_CLASS_SUSPECT', 'iso_class_suspect', 'event'

    if ctx.baseline is None:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    agg_asof = _asof(ctx.agg, ctx.now)
    iso = evaluate_iso(agg_asof, ctx.device, ctx.baseline)

    cross = None
    if iso.applicable:
        consecutive = max(1, int(ctx.params.get('frontend_consecutive_readings', 3)))
        cross = _frontend_zone_cross_check(agg_asof, iso.machine_class, consecutive)

    if not iso.is_class_suspect and cross is None:
        return RuleOutcome.no_trigger(rule_code, issue_type, family)

    baseline_median = ctx.baseline.stats['vel_rms'].median if 'vel_rms' in ctx.baseline.stats else None
    bc_threshold = iso.thresholds.get('bc')

    detail_parts: list[str] = []
    limit_parts: list[str] = []
    evidence: dict = {
        'machine_class': iso.machine_class,
        'class_source': iso.class_source,
    }

    if iso.is_class_suspect:
        # `iso.suspect_reason`（來自 iso.py，不在本檔修改範圍內）會提到
        # 「基礎剛性」等具體規格字眼，但本身沒有帶免責語——單獨看會被
        # `guardrail.check_outcome()` 判定為「提及故障相關詞彙卻缺少免責
        # 語」。這裡補一句涵蓋整個 detail 欄位的免責語，不改動 iso.py
        # 產生的原文，只是把「本系統無法區分兩者」講清楚、講完整。
        detail_parts.append(iso.suspect_reason)
        detail_parts.append(
            '（本系統無法區分是台帳等級填寫錯誤、還是設備規格本身特殊，'
            '兩者都需人工複核，不由系統自動判定。）'
        )
        limit_parts.append(
            '基準期水準檢查：此判定僅指出基準期實測水準與目前指派的機械'
            '等級不吻合，本系統無法區分是台帳等級填寫錯誤、還是設備本身'
            '振動水準原本就偏高（例如馬力、基礎剛性等規格特殊）；兩者都'
            '需要人工核對台帳與現場設備規格後才能確認，不應直接採信目前'
            '的 Zone 分級結論。'
        )
        evidence['baseline_median_vel_rms'] = baseline_median
        evidence['thresholds'] = iso.thresholds

    if cross is not None:
        if cross['direction'] == 'ours_worse':
            direction_text = (
                f"本系統依台帳機械等級判為 Zone {cross['our_zone']}，"
                f"前端同一時段判為 Zone {cross['frontend_zone']}，"
                '本系統的判定持續比前端更嚴重'
            )
        else:
            direction_text = (
                f"前端判為 Zone {cross['frontend_zone']}，"
                f"本系統依台帳機械等級同一時段判為 Zone {cross['our_zone']}，"
                '前端的判定持續比本系統更嚴重'
            )
        detail_parts.append(
            f"前端交叉檢查：連續 {cross['consecutive']} 筆可信資料中，"
            f"{direction_text}（最新一筆 velRMS {cross['vel_rms']:.2f} mm/s）。"
        )
        limit_parts.append(
            '前端交叉檢查：本系統與前端對同一時段判出不同 Zone，本系統'
            '不知道前端計算 iso10816 時假設了哪一個機械等級，因此無法'
            '判斷是本系統登記的機械等級（ISO10816_code）填錯、還是前端'
            '的等級假設有誤——這是台帳資訊本身的問題，不代表設備振動'
            '異常，也不需要另外派工程師到現場複測；請負責維護設備台帳'
            '的人員核對這台設備登記的機械等級是否正確。'
        )
        evidence['frontend_cross_check'] = cross

    return RuleOutcome(
        triggered=True, rule_code=rule_code, issue_type=issue_type, family=family,
        severity='warn',
        title='ISO 等級疑似需複核',
        detail='\n'.join(detail_parts),
        interpretation_limit='\n'.join(limit_parts),
        current_value=baseline_median, baseline_value=bc_threshold, value_unit='mm/s',
        evidence=evidence,
        target_type='point',
    )
