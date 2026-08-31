"""
backtest.py — 回測核心：把「量測點的完整歷史」跑成「規則會觸發幾次」

**為什麼要逐日重新評估，而不是對整段歷史跑一次規則就好**：正式上線後
規則引擎是週期性執行的（例如每天跑一次），每次執行只看「當下累積到的
歷史」。同一段資料，在第 10 天評估跟在第 90 天評估，能看到的歷史長度不同，
趨勢類規則（`DEGRADE_TREND`／`SPECTRAL_SHIFT`）算出來的斜率也會不同——
只跑一次「事後諸葛」式的規則判定，會嚴重低估真實觸發次數（因為用了
「未來」才有的資料）。所以這裡用 `now` 逐日往前推進，每次只把
`ts_hour <= now` 的資料交給規則，模擬「這天規則引擎真的跑一次」會看到
什麼。

**Finding 數量的計法**：正式系統的 Finding 是「同一個 (target, issue_type)
持續存在時只累加 occurrence_count，直到條件不再成立才會在下次觸發時開一筆
新的」（見 `db/schema.sql` 的 `finding_key` 唯一鍵設計）。因此回測不能用
「觸發的小時數」當 Finding 數，那會嚴重高估；必須把連續觸發的區段合併成
一個「事件」（episode），一個事件約當一筆會被建立的 Finding。這是本檔
`_build_episodes` 存在的原因，也是判斷「會不會誤報洪水」時最關鍵的一步。

基準期採**固定一次**（用該量測點最早期的資料算出，見
`validate/baseline_stub.py`），不隨 `now` 重算——這對應真實系統「基準期
建立後只在明顯過時才重算」的行為，也讓回測不必為每個評估日都重新擬合
一次基準，效能才可能負擔得起。
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

import pandas as pd

from vibcore.config import DEFAULT_AGG, DEFAULT_TREND, AggregateConfig
from vibcore.pipeline.aggregate import aggregate_hourly, coverage_report, summarize_gaps
from vibcore.types import RuleContext, is_actionable

from validate.baseline_stub import compute_axis_energy_baseline, compute_baseline
from validate.points import PointSeries
from validate.rule_defaults import RuleConfigRow
from validate.rules_stub import REGISTRY, RuleFunc

logger = logging.getLogger(__name__)


@dataclass
class PointContext:
    """一個量測點回測所需的全部前置計算（聚合、基準期）只做一次。"""
    point: PointSeries
    agg: pd.DataFrame
    baseline: object  # BaselineStats | None，型別見 vibcore.types
    axis_baseline: dict | None
    eval_timestamps: list[pd.Timestamp]
    _asof_cache: dict = field(default_factory=dict, repr=False)

    def agg_asof(self, now) -> pd.DataFrame:
        """
        只取 `now`（含）之前的聚合資料。

        **這是回測正確性的關鍵**。指標型規則刻意不依賴 `ctx.now`，而是取
        「資料裡最後一筆 ok」當作現況（見 `vibcore/rules/metric_rules.py`
        的 `_latest_ok_value`）。正式環境沒問題——每天跑一次，傳進去的
        就是到今天為止的資料，「最後一筆」自然等於今天。

        但回測是拿**完整序列**逐日重放的。若整份 agg 直接傳進去，每一個
        評估日看到的都是同一個資料末端值，等於偷看未來：只要設備在觀測期
        「最後」是偏離的，回測就會判定它從第一天起就一直偏離。

        實測後果：異常只發生在最後 3 天，VEL_HIGH 與 STEP_CHANGE 卻在兩個
        月前的評估日就觸發。連續觸發被 `build_episodes` 合併成一段，於是
        每台設備恰好產生一個事件——這正是「N 次 / N 台」那種整齊到不像
        真實故障分布的統計外觀的成因。

        逐日切片會重複很多次（量測點 × 規則 × 天數），故快取結果；
        同一個量測點的所有規則共用同一份切片。
        """
        key = pd.Timestamp(now)
        cached = self._asof_cache.get(key)
        if cached is None:
            cached = self.agg[self.agg['ts_hour'] <= key]
            self._asof_cache[key] = cached
        return cached


@dataclass
class BacktestResult:
    coverage_df: pd.DataFrame
    gaps_df: pd.DataFrame
    episodes_df: pd.DataFrame
    point_contexts: list[PointContext] = field(default_factory=list)
    span_start: pd.Timestamp | None = None
    span_end: pd.Timestamp | None = None
    n_devices: int = 0
    n_points: int = 0


def build_point_context(point: PointSeries, agg_cfg: AggregateConfig = DEFAULT_AGG,
                        auto_detect_density: bool = True) -> PointContext:
    """
    對單一量測點跑「聚合 → 基準期」，供後續逐日規則評估重複使用。

    `auto_detect_density` 預設開啟，讓聚合層逐日推估取樣密度——同一量測點
    的匯出檔可能混雜每秒與每 10 分鐘兩種前端版本。使用者以
    `--samples-per-hour` 明確指定時才關閉，讓明確指定的值真正生效。
    """
    agg = aggregate_hourly(point.raw, cfg=agg_cfg, fill_gaps=True,
                           auto_detect_density=auto_detect_density)
    if agg.empty:
        return PointContext(point=point, agg=agg, baseline=None, axis_baseline=None, eval_timestamps=[])

    baseline = compute_baseline(agg, point.point_id)
    axis_baseline = compute_axis_energy_baseline(agg, DEFAULT_TREND.min_days)

    # 每個「有資料的日曆日」評估一次，取當天最後一筆 ts_hour 當作「現在」——
    # 逼近真實系統「一天跑一次規則引擎，看得到當天所有已入庫的資料」。
    eval_timestamps = (
        agg.groupby(agg['ts_hour'].dt.date)['ts_hour'].max().sort_index().tolist()
    )
    return PointContext(point=point, agg=agg, baseline=baseline,
                         axis_baseline=axis_baseline, eval_timestamps=eval_timestamps)


def _make_episode_row(pc: PointContext, rule_row: RuleConfigRow,
                       start: pd.Timestamp, end: pd.Timestamp) -> dict:
    duration_days = (end.normalize() - start.normalize()).days + 1
    return {
        'device_id': pc.point.device.device_id,
        'device_name': pc.point.device.device_name,
        'point_id': pc.point.point_id,
        'position': pc.point.position,
        'rule_code': rule_row.rule_code,
        'rule_name': rule_row.rule_name,
        'family': rule_row.family,
        'issue_type': rule_row.issue_type,
        'severity': rule_row.severity,
        # 是否會在正式系統建立 Finding、進入簽核鏈——直接在事件層算好，
        # 下游（report.py）就不必各自 import vibcore.types 重算一次判準，
        # 也不會有兩處判準漂移的風險。
        'is_actionable': is_actionable(rule_row.severity),
        'episode_start': start,
        'episode_end': end,
        'duration_days': duration_days,
    }


def build_episodes(pc: PointContext, rule_row: RuleConfigRow, fn: RuleFunc) -> list[dict]:
    """
    對一個 (量測點, 規則) 組合逐日評估，把連續觸發的日子合併成一個事件。

    一個事件 ≈ 正式系統會建立的一筆 Finding（見模組 docstring）。
    """
    if not pc.eval_timestamps:
        return []

    episodes: list[dict] = []
    open_start: pd.Timestamp | None = None
    prev_ts: pd.Timestamp = pc.eval_timestamps[0]

    for ts in pc.eval_timestamps:
        ctx = RuleContext(
            device=pc.point.device, point_id=pc.point.point_id, position=pc.point.position,
            agg=pc.agg_asof(ts), baseline=pc.baseline, params=rule_row.params, now=ts,
            axis_energy_baseline=pc.axis_baseline, last_data_at=None,
        )
        try:
            outcome = fn(ctx)
        except Exception:
            logger.exception(f"規則 {rule_row.rule_code} 於 point={pc.point.point_id} "
                              f"now={ts} 評估失敗，本次評估點視為未觸發")
            outcome = None

        triggered = bool(outcome and outcome.triggered)
        if triggered and open_start is None:
            open_start = ts
        elif not triggered and open_start is not None:
            episodes.append(_make_episode_row(pc, rule_row, open_start, prev_ts))
            open_start = None
        prev_ts = ts

    if open_start is not None:
        episodes.append(_make_episode_row(pc, rule_row, open_start, prev_ts))

    return episodes


def run_backtest(points: list[PointSeries],
                  rule_configs: dict[str, RuleConfigRow],
                  agg_cfg: AggregateConfig = DEFAULT_AGG,
                  auto_detect_density: bool = True) -> BacktestResult:
    """對所有量測點跑完整回測：聚合 → 涵蓋率 → 缺口 → 基準期 → 逐日規則評估。"""
    point_contexts = [build_point_context(p, agg_cfg, auto_detect_density)
                      for p in points]

    coverage_rows, gap_rows, episode_rows = [], [], []
    for pc in point_contexts:
        cov = coverage_report(pc.agg)
        coverage_rows.append({
            'device_id': pc.point.device.device_id,
            'device_name': pc.point.device.device_name,
            'point_id': pc.point.point_id,
            'position': pc.point.position,
            **cov,
        })

        gaps = summarize_gaps(pc.agg)
        for _, g in gaps.iterrows():
            gap_rows.append({
                'device_id': pc.point.device.device_id,
                'point_id': pc.point.point_id,
                'position': pc.point.position,
                'gap_start': g['gap_start'], 'gap_end': g['gap_end'],
                'hours': g['hours'], 'status': g['status'],
            })

        for rule_code, row in rule_configs.items():
            if not row.is_active:
                continue
            fn = REGISTRY.get(rule_code)
            if fn is None:
                logger.warning(f"規則 {rule_code} 沒有對應的評估函式，略過")
                continue
            episode_rows.extend(build_episodes(pc, row, fn))

    coverage_df = pd.DataFrame(coverage_rows)
    gaps_df = (pd.DataFrame(gap_rows).sort_values('hours', ascending=False).reset_index(drop=True)
               if gap_rows else pd.DataFrame(columns=['device_id', 'point_id', 'position',
                                                        'gap_start', 'gap_end', 'hours', 'status']))
    episodes_df = (pd.DataFrame(episode_rows).sort_values('episode_start').reset_index(drop=True)
                   if episode_rows else pd.DataFrame(columns=[
                       'device_id', 'device_name', 'point_id', 'position', 'rule_code', 'rule_name',
                       'family', 'issue_type', 'severity', 'is_actionable', 'episode_start', 'episode_end',
                       'duration_days']))

    non_empty = [pc for pc in point_contexts if not pc.agg.empty]
    span_start = min((pc.agg['ts_hour'].min() for pc in non_empty), default=None)
    span_end = max((pc.agg['ts_hour'].max() for pc in non_empty), default=None)
    n_devices = len({pc.point.device.device_id for pc in point_contexts})

    return BacktestResult(
        coverage_df=coverage_df, gaps_df=gaps_df, episodes_df=episodes_df,
        point_contexts=point_contexts, span_start=span_start, span_end=span_end,
        n_devices=n_devices, n_points=len(point_contexts),
    )


def span_weeks(span_start: pd.Timestamp | None, span_end: pd.Timestamp | None) -> float:
    """回測涵蓋的週數；不足 1 天以 1 天計，避免除以極小值把密度灌爆。"""
    if span_start is None or span_end is None:
        return 0.0
    days = max((span_end - span_start).total_seconds() / 86400, 1.0)
    return days / 7.0


def sweep_threshold(point_contexts: list[PointContext],
                     rule_code: str, param_name: str, values: list[float],
                     base_rule_configs: dict[str, RuleConfigRow]) -> pd.DataFrame:
    """
    門檻敏感度掃描：固定其他一切，只改一條規則的一個參數，看觸發量怎麼變。

    這是回答「σ 該設多少」的直接依據——若某個門檻附近觸發量斷崖式下降，
    代表資料本身在那個水準附近有一群「剛好卡在邊緣」的樣本，通常是比較
    安全的切點；若觸發量隨門檴平滑遞減，則要另外看「調到多少才落在可
    負荷的每週件數」。
    """
    fn = REGISTRY.get(rule_code)
    if fn is None:
        raise ValueError(f"未知規則代碼：{rule_code}")
    base_row = base_rule_configs[rule_code]

    # 分母用「各設備觀測週數總和」而非「共同期間 × 設備數」——設備各自的
    # 資料起訖不一定相同（新裝設備、資料量不齊），用單一共同期間乘設備數
    # 會系統性算錯每設備每週的密度（見 `validate/report.py` 的
    # `_device_span_weeks` docstring，同樣的道理）。
    device_spans: dict[str, list[pd.Timestamp]] = {}
    for pc in point_contexts:
        if pc.agg.empty:
            continue
        device_id = pc.point.device.device_id
        lo, hi = pc.agg['ts_hour'].min(), pc.agg['ts_hour'].max()
        bounds = device_spans.setdefault(device_id, [lo, hi])
        bounds[0] = min(bounds[0], lo)
        bounds[1] = max(bounds[1], hi)
    total_device_weeks = sum(span_weeks(lo, hi) for lo, hi in device_spans.values())

    rows = []
    for v in values:
        row = copy.deepcopy(base_row)
        row.params[param_name] = v
        episodes: list[dict] = []
        for pc in point_contexts:
            episodes.extend(build_episodes(pc, row, fn))
        n_devices_affected = len({e['device_id'] for e in episodes})
        n_episodes = len(episodes)
        rows.append({
            'rule_code': rule_code,
            'param_name': param_name,
            'param_value': v,
            'n_episodes': n_episodes,
            'n_devices_affected': n_devices_affected,
            'episodes_per_device_per_week': round(n_episodes / total_device_weeks, 4)
            if total_device_weeks else 0.0,
        })
    return pd.DataFrame(rows)
