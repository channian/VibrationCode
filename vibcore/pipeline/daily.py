"""
daily.py — 每日排程協調

把整條鏈路串起來：

    讀取 Analytic CSV → 每小時聚合 → 落庫 → 指標計算 → 規則判定
                                                          ↓
                        自動結案 ← 惡化偵測 ← Finding upsert

這一層本身**不含任何判定邏輯**，只負責呼叫順序、錯誤隔離與稽核。
所有門檻與判準都在規則層（`vibcore.rules`）與指標層（`vibcore.metrics`）。

## 三個刻意的設計

**1. 單一設備失敗不影響其餘設備。**
規則引擎已經做到「單一規則出錯不拖垮整批」（見 `rules/engine.py`），
這裡把同樣的原則往上提一層：某台設備的檔案損毀或資料形狀特殊而拋例外時，
其餘 119 個量測點仍要照常判定。當日完全沒有告警，比少一台設備的告警危險
得多。

**2. 重跑同一天不會產生副作用。**
排程失敗需要補跑是常態。聚合落庫用 ON CONFLICT 覆蓋、Finding 用
`upsert_finding` 依 `finding_key` 收斂，因此同一天重跑只會覆寫，不會
變成兩筆事項或把 occurrence_count 灌成兩倍。

**3. 匯入軌跡一律記錄，成功與失敗都記。**
只記成功的話，「沒有紀錄」會同時代表「排程沒跑」與「跑了但失敗」，
而這兩者跟「感測器斷線」在資料庫裡又長得一樣。三種情況的處置完全不同
（見 `record_ingestion` 的呼叫處說明）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from vibcore.config import AggregateConfig, DEFAULT_AGG, DEFAULT_TREND
from vibcore.db import repository as repo
from vibcore.io.analytic_reader import load_analytic_dir
from vibcore.metrics.baseline import detect_baseline
from vibcore.pipeline.aggregate import aggregate_hourly, coverage_report, rollup_daily
from vibcore.rules import evaluate_all, outcome_to_finding
from vibcore.types import (
    CLOSED_STATUSES, DeviceContext, Finding, RuleContext,
)

logger = logging.getLogger(__name__)

#: 事項連續幾天未再觸發即自動結案。
#: 取 3 天而非 1 天，是因為工況波動可能讓指標短暫回到門檻內；
#: 一天就結案會造成「結案→隔天重開」的來回震盪，簽核鏈會被灌爆。
DEFAULT_QUIET_DAYS = 3

#: 惡化判定的最小變化幅度。低於此值視為量測雜訊，不標記惡化——
#: 否則每日排程會讓幾乎所有事項都被標成「持續惡化」，這個旗標就失去意義。
ESCALATE_MIN_DELTA_PCT = 5.0


@dataclass
class PointResult:
    """單一量測點的處理結果。"""
    device_id: str
    position: str
    point_id: int | None = None
    ingested_rows: int = 0
    agg_hours: int = 0
    coverage: dict = field(default_factory=dict)
    triggered: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class DailyRunResult:
    """整批處理結果，供排程紀錄與人工檢視。"""
    run_date: date
    points: list[PointResult] = field(default_factory=list)
    findings_upserted: int = 0
    auto_resolved: int = 0
    escalated: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def n_ok(self) -> int:
        return sum(1 for p in self.points if p.ok)

    @property
    def n_failed(self) -> int:
        return sum(1 for p in self.points if not p.ok)

    def summary(self) -> str:
        return (f"{self.run_date}：量測點 {self.n_ok} 成功 / {self.n_failed} 失敗，"
                f"事項 upsert {self.findings_upserted}、"
                f"自動結案 {self.auto_resolved}、標記惡化 {self.escalated}")


# ──────────────────────────────────────────────────────────
# 匯入軌跡（介面由 db/repository.py 提供；尚未就緒時降級為僅記錄日誌）
# ──────────────────────────────────────────────────────────

def _record_ingestion(conn, point_id: int, ingest_date: date, status: str,
                      source_file: str = '', row_count: int = 0, note: str = '') -> None:
    """
    記錄匯入軌跡。

    「沒有資料」有三種完全不同的成因，處置也完全不同：

        感測器斷線   → 設備側問題，該去現場查感測器
        匯入未執行   → 系統側問題，該去查排程
        匯入執行但失敗 → 系統側問題，該去查來源檔或程式

    若只在成功時留紀錄，後兩者會無法區分，而且都會被誤報成第一種——
    工程師跑去現場結果感測器好好的。因此失敗也要記。
    """
    fn = getattr(repo, 'record_ingestion', None)
    if fn is None:
        logger.debug("repository.record_ingestion 尚未就緒，僅記錄日誌")
        return
    try:
        fn(conn, point_id=point_id, ingest_date=ingest_date, status=status,
           source_file=source_file, row_count=row_count, note=note)
    except Exception as e:      # 稽核失敗不得中斷主流程
        logger.warning(f"匯入軌跡寫入失敗（{point_id}/{ingest_date}）：{e}")


# ──────────────────────────────────────────────────────────
# 單一量測點
# ──────────────────────────────────────────────────────────

def _ensure_device_and_point(conn, meta: dict, position: str) -> tuple[DeviceContext, int]:
    """依 Analytic CSV 內建的 metadata 建立/更新台帳，回傳設備與量測點。"""
    def _num(v):
        try:
            return float(v) if v is not None and str(v).strip() not in ('', 'nan') else None
        except (TypeError, ValueError):
            return None

    device = DeviceContext(
        device_id=str(meta.get('Name', '')).strip(),
        device_name=str(meta.get('Name', '')).strip(),
        building=str(meta.get('Building', '') or '').strip(),
        floor=str(meta.get('Floor', '') or '').strip(),
        system_name=str(meta.get('System', '') or '').strip(),
        rated_rpm=_num(meta.get('RPM')),
        fmf_hz=_num(meta.get('FMF')),
    )
    repo.upsert_device(conn, device)
    point_id = repo.upsert_measure_point(conn, device.device_id, position)
    # 台帳可能已由管理員設定 ISO 等級與備機旗標，讀回較完整的版本
    stored = repo.get_device(conn, device.device_id)
    return (stored or device), point_id


def process_point(conn, device_meta: dict, df: pd.DataFrame, run_date: date,
                  position: str = 'M1',
                  agg_cfg: AggregateConfig = DEFAULT_AGG) -> PointResult:
    """
    處理單一量測點：聚合 → 落庫 → 基準期 → 規則判定 → Finding。

    回傳結果物件而非拋例外，讓呼叫端能繼續處理其他量測點。
    """
    device_id = str(device_meta.get('Name', '?')).strip()
    result = PointResult(device_id=device_id, position=position)

    try:
        device, point_id = _ensure_device_and_point(conn, device_meta, position)
        result.point_id = point_id
        result.ingested_rows = len(df)

        agg = aggregate_hourly(df, agg_cfg)
        if agg.empty:
            result.error = '聚合後無資料'
            _record_ingestion(conn, point_id, run_date, 'failed',
                              row_count=len(df), note=result.error)
            return result

        repo.bulk_insert_agg(conn, point_id, agg)
        result.agg_hours = len(agg)
        result.coverage = coverage_report(agg)

        # 日層 rollup。週報與長期趨勢讀的是 measurement_daily，這一步漏掉
        # 的話那張表永遠是空的，而且不會有任何錯誤訊息——週報只會顯示
        # 「查無資料」，看起來像設備沒運轉。
        daily = rollup_daily(agg, agg_cfg)
        if not daily.empty:
            repo.bulk_insert_daily(conn, point_id, daily)

        cov_ratio = result.coverage.get('analyzable_ratio', 0.0)
        _record_ingestion(
            conn, point_id, run_date,
            status='ok' if cov_ratio > 0 else 'partial',
            row_count=len(df),
            note=f"可分析比例 {cov_ratio:.1%}",
        )

        # ── 基準期：已有就沿用，沒有才嘗試建立 ──
        baseline = repo.get_baseline(conn, point_id)
        if baseline is None:
            history = repo.get_agg(conn, point_id,
                                   datetime.combine(run_date, datetime.min.time(),
                                                    tzinfo=timezone.utc) - timedelta(days=90),
                                   datetime.combine(run_date, datetime.min.time(),
                                                    tzinfo=timezone.utc) + timedelta(days=1))
            if not history.empty:
                baseline = detect_baseline(history, point_id=point_id)
                if baseline is not None:
                    repo.save_baseline(conn, baseline)
                    logger.info(f"  {device_id}/{position}：建立基準期 "
                                f"{baseline.start_date}~{baseline.end_date}"
                                f"（{baseline.n_hours} 小時）")

        # ── 規則判定 ──
        # 規則需要看趨勢，因此餵入的是歷史區間而非只有當日
        window_start = (datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc)
                        - timedelta(days=DEFAULT_TREND.min_days * 2))
        window_end = datetime.combine(run_date, datetime.min.time(),
                                      tzinfo=timezone.utc) + timedelta(days=1)
        rule_agg = repo.get_agg(conn, point_id, window_start, window_end)
        if rule_agg.empty:
            rule_agg = agg

        ctx = RuleContext(
            device=device, point_id=point_id, position=position,
            agg=rule_agg, baseline=baseline, params={},
            now=datetime.now(timezone.utc),
            axis_energy_baseline=(baseline.stats.get('axis_energy_sorted')
                                  if baseline and hasattr(baseline, 'stats') else None),
        )
        rule_configs = repo.get_rule_configs(conn)
        outcomes = evaluate_all(ctx, rule_configs)
        result.triggered = [o.rule_code for o in outcomes]
        result._outcomes = outcomes          # type: ignore[attr-defined]
        result._ctx = ctx                    # type: ignore[attr-defined]

    except Exception as e:
        logger.error(f"{device_id}/{position} 處理失敗：{e}", exc_info=True)
        result.error = str(e)
        if result.point_id is not None:
            _record_ingestion(conn, result.point_id, run_date, 'failed',
                              row_count=len(df), note=str(e)[:200])

    return result


# ──────────────────────────────────────────────────────────
# Finding 生命週期
# ──────────────────────────────────────────────────────────

def _is_worse(new_value: float | None, old_value: float | None,
              baseline_value: float | None) -> bool:
    """
    判斷數值是否較上次更糟。

    「更糟」的方向由基準決定：多數振動指標是越大越糟，但若基準值高於
    當前值（例如比功率這類越高越好的指標），方向要反過來。以「離基準
    更遠」為準比寫死大小方向穩健。
    """
    if new_value is None or old_value is None:
        return False
    if baseline_value is None:
        delta_pct = abs(new_value - old_value) / abs(old_value or 1) * 100
        return new_value > old_value and delta_pct >= ESCALATE_MIN_DELTA_PCT

    old_dist = abs(old_value - baseline_value)
    new_dist = abs(new_value - baseline_value)
    if old_dist == 0:
        return new_dist > 0
    return (new_dist - old_dist) / old_dist * 100 >= ESCALATE_MIN_DELTA_PCT


def persist_findings(conn, results: list[PointResult]) -> tuple[int, int]:
    """
    把規則判定結果寫入資料庫，並偵測惡化。

    Returns:
        (upsert 筆數, 標記惡化筆數)
    """
    n_upsert = n_escalate = 0
    existing = {f['finding_key']: f for f in repo.get_open_findings(conn)}

    for r in results:
        outcomes = getattr(r, '_outcomes', None)
        ctx = getattr(r, '_ctx', None)
        if not outcomes or ctx is None:
            continue

        for outcome in outcomes:
            finding = outcome_to_finding(outcome, ctx)
            prev = existing.get(finding.finding_key)
            try:
                repo.upsert_finding(conn, finding)
                n_upsert += 1
            except Exception as e:
                logger.error(f"Finding upsert 失敗（{finding.finding_key}）：{e}")
                continue

            # 惡化偵測：與上一次的數值比，而非與基準比
            if prev and _is_worse(finding.current_value,
                                  prev.get('current_value'),
                                  finding.baseline_value):
                try:
                    repo.mark_escalated(
                        conn, finding.finding_key,
                        note=f"數值由 {prev.get('current_value')} 變為 "
                             f"{finding.current_value}，較上次更偏離基準",
                    )
                    n_escalate += 1
                except Exception as e:
                    logger.warning(f"標記惡化失敗（{finding.finding_key}）：{e}")

    return n_upsert, n_escalate


def auto_resolve_quiet(conn, quiet_days: int = DEFAULT_QUIET_DAYS) -> int:
    """
    自動結案：連續 `quiet_days` 天未再觸發的未結案事項。

    判準用 `last_seen_at`——該欄位只在規則再次觸發時更新，因此
    「距今超過 N 天沒更新」就等於「規則已連續 N 天沒再判定為異常」，
    不需要另外維護計數器。

    刻意不要求走完簽核鏈：問題本身消失時直接結案才是對的，卡住的簽核
    步驟不該讓一件已經好了的事永遠掛著。歷程紀錄保留供追溯。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=quiet_days)
    n = 0
    for row in repo.get_open_findings(conn):
        last_seen = row.get('last_seen_at')
        if last_seen is None:
            continue
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen >= cutoff:
            continue
        try:
            repo.auto_resolve(
                conn, row['finding_key'],
                note=f"連續 {quiet_days} 天未再觸發（最後一次 "
                     f"{last_seen.date()}），由系統自動結案",
            )
            n += 1
        except Exception as e:
            logger.warning(f"自動結案失敗（{row['finding_key']}）：{e}")
    return n


# ──────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────

def run_daily(conn, data_dir: str, run_date: date | None = None,
              pattern: str = '*.csv',
              agg_cfg: AggregateConfig = DEFAULT_AGG,
              quiet_days: int = DEFAULT_QUIET_DAYS) -> DailyRunResult:
    """
    每日排程主流程。

    Args:
        conn: 資料庫連線
        data_dir: 存放 Analytic CSV 的資料夾
        run_date: 這批資料所屬的日期；None 表示今日
        pattern: 檔名比對樣式
        agg_cfg: 聚合參數
        quiet_days: 連續幾天未觸發即自動結案

    Returns:
        DailyRunResult，含各量測點結果與整批統計
    """
    run_date = run_date or datetime.now(timezone.utc).date()
    result = DailyRunResult(run_date=run_date, started_at=datetime.now(timezone.utc))

    logger.info(f"每日排程開始：{run_date}，資料來源 {data_dir}")

    vib_data = load_analytic_dir(data_dir, pattern)
    if not vib_data:
        logger.warning(f"{data_dir} 沒有讀到任何資料，本次無事可做")
        result.finished_at = datetime.now(timezone.utc)
        return result

    for device_id, df in sorted(vib_data.items()):
        meta = {'Name': device_id}
        for col in ('Building', 'Floor', 'System', 'RPM', 'FMF'):
            if col in df.columns and len(df):
                meta[col] = df[col].iloc[0]
        # 單一設備失敗不影響其餘設備（見模組說明）
        result.points.append(process_point(conn, meta, df, run_date, agg_cfg=agg_cfg))

    result.findings_upserted, result.escalated = persist_findings(conn, result.points)
    result.auto_resolved = auto_resolve_quiet(conn, quiet_days)
    result.finished_at = datetime.now(timezone.utc)

    logger.info(result.summary())
    if result.n_failed:
        for p in result.points:
            if not p.ok:
                logger.error(f"  失敗：{p.device_id}/{p.position} — {p.error}")
    return result


def main() -> int:
    """CLI 入口：python -m vibcore.pipeline.daily --data-dir Vibration_Data"""
    import argparse
    from vibcore.db.connection import get_connection

    p = argparse.ArgumentParser(description='每日排程：匯入、聚合、判定、落庫')
    p.add_argument('--data-dir', required=True, help='存放 Analytic CSV 的資料夾')
    p.add_argument('--date', default=None, help='資料所屬日期 YYYY-MM-DD，預設今日')
    p.add_argument('--pattern', default='*.csv')
    p.add_argument('--quiet-days', type=int, default=DEFAULT_QUIET_DAYS,
                   help=f'連續幾天未觸發即自動結案，預設 {DEFAULT_QUIET_DAYS}')
    p.add_argument('--log-level', default='INFO',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = p.parse_args()

    logging.basicConfig(level=args.log_level,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                        datefmt='%H:%M:%S')

    run_date = date.fromisoformat(args.date) if args.date else None
    with get_connection() as conn:
        result = run_daily(conn, args.data_dir, run_date,
                           pattern=args.pattern, quiet_days=args.quiet_days)

    print('\n  ' + result.summary())
    return 1 if result.n_failed and not result.n_ok else 0


if __name__ == '__main__':
    raise SystemExit(main())
