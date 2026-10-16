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

from vibcore.config import AggregateConfig, DEFAULT_AGG, DEFAULT_TREND, DataStatus
from vibcore.db import repository as repo
from vibcore.db.connection import transaction
from vibcore.io.analytic_reader import load_analytic_dir
from vibcore.metrics.baseline import compute_axis_energy_baseline, detect_baseline
from vibcore.pipeline.aggregate import aggregate_hourly, coverage_report, rollup_daily
from vibcore.rules import evaluate_all, outcome_to_finding
from vibcore.types import (
    CLOSED_STATUSES, DeviceContext, Finding, RuleContext, is_actionable,
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
    """
    單一量測點的處理結果。

    `triggered` / `observed` 在規則判定完成的當下（`process_point`）就依
    `RuleOutcome.severity` 分好——是否 actionable 只取決於嚴重度本身，
    與後續是否真的寫入 DB 無關，不必等到 `persist_findings` 才知道。

        triggered — 會建立/更新 Finding、進 SLA 簽核鏈的規則代碼
        observed  — observe 級：僅供週報觀察名單參考，不建立 Finding

    兩者都要留，理由見 `persist_findings` 的說明：observe 級判定若完全
    不留痕跡，週報就沒有素材、使用者也會誤以為系統什麼都沒偵測到。
    """
    device_id: str
    position: str
    point_id: int | None = None
    ingested_rows: int = 0
    agg_hours: int = 0
    coverage: dict = field(default_factory=dict)
    triggered: list[str] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)
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
    observed_count: int = 0
    auto_resolved: int = 0
    escalated: int = 0
    #: 寫入資料庫時失敗的項目（觀察項目、事項、自動結案…），每筆一段說明。
    #: 單筆失敗只丟掉那一筆（見 `vibcore.db.connection.transaction`），其餘照常寫入；
    #: 但必須數出來——失敗被吞掉而摘要看起來一切正常，是最危險的狀態。
    persist_errors: list[str] = field(default_factory=list)
    #: 今天的資料裡沒有它、但資料庫認得的量測點（停止送資料的感測器）。只判定「沒有資料／沒有運轉」兩類規則。
    silent: list = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def n_ok(self) -> int:
        return sum(1 for p in self.points if p.ok)

    @property
    def n_failed(self) -> int:
        return sum(1 for p in self.points if not p.ok)

    def summary(self) -> str:
        text = (f"{self.run_date}：量測點 {self.n_ok} 成功 / {self.n_failed} 失敗，"
                f"事項 upsert {self.findings_upserted}、"
                f"觀察名單（不進 SLA）{self.observed_count}、"
                f"自動結案 {self.auto_resolved}、標記惡化 {self.escalated}")
        if self.silent:
            text += f"；今日無資料的量測點 {len(self.silent)} 個（已判定逾時無資料）"
        if self.persist_errors:
            text += f"；⚠ 寫入失敗 {len(self.persist_errors)} 筆（原因見日誌）"
        return text


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
        # 自己一個 SAVEPOINT：SQL 失敗後不退回的話，整個交易就毒化了，
        # 「稽核失敗不得中斷主流程」只擋得住 Python 例外，擋不住這個。
        with transaction(conn):
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
    # Channel_X/Y/Z 帶著軸向資訊（4=垂直徑向 5=軸向 6=水平徑向），一併寫進
    # 台帳。這三欄 schema 早就有、upsert_measure_point 也早就接受，但過去
    # 呼叫時沒傳，所以永遠是 NULL——方向資訊在 CSV 裡卻被管線丟掉了。
    def _chan(key):
        v = meta.get(key)
        try:
            return int(float(v)) if v is not None and str(v).strip() not in ('', 'nan') else None
        except (TypeError, ValueError):
            return None

    point_id = repo.upsert_measure_point(
        conn, device.device_id, position,
        channel_x=_chan('Channel_X'), channel_y=_chan('Channel_Y'),
        channel_z=_chan('Channel_Z'),
    )
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
        # 設備與量測點先在自己的 SAVEPOINT 裡建好並保留：後面任何一步失敗時，
        # 這個點還在，才能記下一筆「匯入失敗」，週報才分得出「失敗」和「沒資料」。
        with transaction(conn):
            device, point_id = _ensure_device_and_point(conn, device_meta, position)
        result.point_id = point_id
        result.ingested_rows = len(df)

        # 資料寫入放在同一個 SAVEPOINT：中途任何 SQL 失敗就整段退回，這個量測點不會留下半套資料，
        # 也不會毒化交易讓後面的量測點全部跟著失敗（PostgreSQL 的交易一旦出錯，之後每一句都被
        # 拒絕，最後的 commit 還會安靜地變成 rollback，當天整批資料就不見了）。
        with transaction(conn):
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
                repo.upsert_daily(conn, point_id, daily)

            cov_ratio = result.coverage.get('analyzable_ratio', 0.0)
            day_agg = _day_slice(agg, run_date)
            _record_ingestion(
                conn, point_id, run_date,
                status=_ingestion_status(day_agg if not day_agg.empty else agg),
                row_count=len(df),
                note=f"可分析比例 {cov_ratio:.1%}",
            )
            _record_covered_dates(conn, point_id, df, agg, run_date)

        # 基準期與規則判定另開一個 SAVEPOINT：這一段失敗時，上面已經寫好的聚合與匯入紀錄不該跟著丟掉
        # （資料收進來了，只是判定出問題）。
        with transaction(conn):
            # ── 基準期：已有就沿用，沒有才嘗試建立 ──
            baseline = repo.get_baseline(conn, point_id)
            baseline_changed = False

            # 保養後既有基準失效（ISO 10816-3 §5.4.1）。若不做這件事，
            # `not_before` 只在「第一次建立基準」時生效——設備大修後既有基準
            # 會被無限沿用，而那份基準描述的是保養前的機器狀態。之後所有 σ
            # 判定都對著一個已經不存在的狀態比較，且不會有任何錯誤訊息。
            last_maint = getattr(device, 'last_maintenance_at', None)
            if baseline is not None and last_maint is not None \
                    and baseline.start_date < last_maint.date():
                logger.info(f"  {device_id}/{position}：基準期起點 {baseline.start_date} 早於"
                            f"最後一次保養 {last_maint.date()}，捨棄並重建")
                baseline = None
                baseline_changed = True

            if baseline is None:
                history = repo.get_agg(conn, point_id,
                                       datetime.combine(run_date, datetime.min.time(),
                                                        tzinfo=timezone.utc) - timedelta(days=90),
                                       datetime.combine(run_date, datetime.min.time(),
                                                        tzinfo=timezone.utc) + timedelta(days=1))
                if not history.empty:
                    # 基準期不得早於最後一次保養（ISO 10816-3 §5.4.1，
                    # 理由見 detect_baseline 的 not_before 說明）
                    baseline = detect_baseline(
                        history, point_id=point_id,
                        not_before=getattr(device, 'last_maintenance_at', None),
                    )
                    if baseline is not None:
                        repo.save_baseline(conn, baseline)
                        baseline_changed = True
                        logger.info(f"  {device_id}/{position}：建立基準期 "
                                    f"{baseline.start_date}~{baseline.end_date}"
                                    f"（{baseline.n_hours} 小時）")

            # ── 三軸能量佔比基準（AXIS_SHIFT／ORIENTATION_CHANGE 的比對對象）──
            # 存在 measure_point.axis_energy_baseline。基準期重建時連同重算；基準還在但這欄是空的
            # （這項功能之前建立的點）就補算一次。過去這一欄從來沒有人寫入，兩條規則在正式排程永遠不判定。
            axis_base = repo.get_axis_energy_baseline(conn, point_id)
            if baseline is None:
                if axis_base is not None:
                    repo.set_axis_energy_baseline(conn, point_id, None)
                    axis_base = None
            elif baseline_changed or axis_base is None:
                span = repo.get_agg(
                    conn, point_id,
                    datetime.combine(baseline.start_date, datetime.min.time(), tzinfo=timezone.utc),
                    datetime.combine(baseline.end_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1))
                axis_base = compute_axis_energy_baseline(span, baseline.start_date, baseline.end_date)
                repo.set_axis_energy_baseline(conn, point_id, axis_base)

            # ── 規則判定 ──
            # 規則需要看趨勢，因此餵入的是歷史區間而非只有當日
            window_start = (datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc)
                            - timedelta(days=DEFAULT_TREND.min_days * 2))
            window_end = run_window_end(run_date)
            rule_agg = repo.get_agg(conn, point_id, window_start, window_end)
            if rule_agg.empty:
                rule_agg = agg

            # `now` 不能直接用牆上時鐘：補跑／重放歷史日期時，依 `now` 判定的規則
            # （SENSOR_OFFLINE 的「多久沒資料」、STANDBY_NO_RUNTIME 的「多久沒運轉」）會把
            # 整段歷史都算成離線，而且 Finding／觀察項目的首次出現時間會被壓成執行當天。
            # 取兩者較早者：跑今天的資料就是現在，跑過去的日期就是那一天的最後一秒。
            ctx = RuleContext(
                device=device, point_id=point_id, position=position,
                agg=rule_agg, baseline=baseline, params={},
                now=rule_now(window_end),
                axis_energy_baseline=axis_base,
            )
            # 「最後一次有資料／有運轉」直接問資料庫：聚合窗口只有約 28 天，備機門檻 30 天
            # 就超過窗口，只看窗口永遠湊不到（備機長期未運轉在正式排程裡永遠不會觸發）。
            _attach_activity(conn, ctx)
            # active_only=False：停用的規則要查得到（is_active=False），引擎才跳得過它；
            # 只給啟用中的，停用的規則會因為查不到設定而用程式碼預設值照跑。
            rule_configs = repo.get_rule_configs(conn, active_only=False)
            outcomes = evaluate_all(ctx, rule_configs)
            # 依嚴重度分兩桶：actionable（err/warn）進 triggered、其餘
            # （目前只有 observe）進 observed。分類只看 outcome.severity，
            # 與後面 persist_findings 是否真的寫入 DB 無關。
            result.triggered = [o.rule_code for o in outcomes if is_actionable(o.severity)]
            result.observed = [o.rule_code for o in outcomes if not is_actionable(o.severity)]
            result._outcomes = outcomes          # type: ignore[attr-defined]
            result._ctx = ctx                    # type: ignore[attr-defined]

    except Exception as e:
        logger.error(f"{device_id}/{position} 處理失敗：{e}", exc_info=True)
        result.error = str(e)
        if result.point_id is not None:
            _record_ingestion(conn, result.point_id, run_date, 'failed',
                              row_count=len(df), note=str(e)[:200])

    return result


def _day_slice(agg: pd.DataFrame, d: date) -> pd.DataFrame:
    """每小時聚合表中屬於某一天的列。"""
    if agg.empty or 'ts_hour' not in agg.columns:
        return agg.iloc[0:0]
    return agg[agg['ts_hour'].dt.date == d]


def _ingestion_status(agg_part: pd.DataFrame) -> str:
    """
    這段資料是不是完整收到了：有任何一個小時是 `ok`（可分析）或 `not_running`（有資料、設備沒轉）
    就是 `ok`，否則 `partial`。

    設備整天沒運轉（備機、停機檢修）時每個小時都是 `not_running`——資料是完整的，只是沒東西可分析。
    以前用「可分析比例 > 0」判斷，會把每一台沒運轉的設備都標成「檔案不完整」，
    週報的「系統面問題」整欄被備機洗滿，並叫人去檢查匯入排程。
    """
    if agg_part.empty or 'data_status' not in agg_part.columns:
        return 'partial'
    s = agg_part['data_status']
    return 'ok' if ((s == DataStatus.OK) | (s == DataStatus.NOT_RUNNING)).any() else 'partial'


def _record_covered_dates(conn, point_id: int, df, agg, run_date: date) -> int:
    """
    檔案涵蓋多天時，每一個有資料的日期都補一筆匯入紀錄（`run_date` 那天已另外記過）。

    `ingestion_log.ingest_date` 的語意是「資料日期」（見 schema 的欄位說明）。只記 `run_date` 的話，
    第一次上線一次載入的歷史資料、或上游補送的前幾天資料，週報的匯入稽核會把那幾天判成
    「匯入排程當天很可能完全沒有執行」，在第一份報告就跳出嚇人的警示。沒有任何資料列的日期
    **不補**——那是真的沒有資料進來，稽核照實標出。

    回傳補記的天數。單日檔案（日常情況）回傳 0，行為與以前完全相同。
    """
    if df.empty or 'datetime' not in df.columns:
        return 0
    rows_by_date = df['datetime'].dt.date.value_counts()
    if len(rows_by_date) <= 1:
        return 0
    n = 0
    for d, n_rows in sorted(rows_by_date.items()):
        if d == run_date:
            continue
        day = _day_slice(agg, d)
        ratio = float((day['data_status'] == DataStatus.OK).mean()) if len(day) else 0.0
        _record_ingestion(conn, point_id, d, status=_ingestion_status(day),
                          row_count=int(n_rows),
                          note=f"可分析比例 {ratio:.1%}（隨 {run_date} 的檔案補記）")
        n += 1
    return n


# ──────────────────────────────────────────────────────────
# Observation（observe 級判定，不進簽核鏈）
# ──────────────────────────────────────────────────────────

def run_window_end(run_date: date) -> datetime:
    """這批資料窗口的結束時間：`run_date` 隔日零時（UTC）。"""
    return (datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc)
            + timedelta(days=1))


def rule_now(window_end: datetime) -> datetime:
    """
    規則判定用的「現在」：牆上時鐘與該批資料窗口結束前一秒取較早者。

    跑今天的資料，窗口結束在明天零時，所以是現在；補跑／重放過去的日期，是那一天的最後一秒。
    見 `process_point` 的說明：依 `now` 判定的規則在補跑時不能拿真實的現在。

    取「前一秒」而不是窗口結束本身：事項與觀察項目的 `first_seen_at`／`last_seen_at` 就是這個值，
    而週報的期間是半開區間 `[起日, 迄日隔天零時)`。剛好落在隔天零時會被算進**隔天**，
    補跑到某天的資料，那天的新事項與觀察名單就會缺席該期報告。
    """
    return min(datetime.now(timezone.utc), window_end - timedelta(seconds=1))


def _attach_activity(conn, ctx) -> None:
    """把『最後一次有資料／有運轉』從資料庫查來掛到規則上下文（不受聚合窗口長度限制）。"""
    act = repo.get_point_activity(conn, ctx.point_id, ctx.now)
    ctx.last_data_at = act['last_data_at']
    # 從未運轉過的點，以第一筆資料當起算點（與規則本身的後備邏輯一致）
    ctx.last_running_at = act['last_running_at'] or act['first_data_at']


#: 判定的就是「沒有資料／沒有運轉」的規則。今天沒有資料的量測點只能評估這兩條：
#: 其餘規則看的是資料本身，拿舊資料重判只會讓早就過去的事件每天重新觸發、永遠不結案。
ABSENCE_RULES = ['SENSOR_OFFLINE', 'STANDBY_NO_RUNTIME']


def process_silent_points(conn, seen_point_ids: set, run_date: date,
                          errors: list[str] | None = None) -> list[PointResult]:
    """
    評估「今天的資料裡沒有它」的量測點。

    `run_daily` 只處理資料夾裡讀到的設備。一個感測器徹底停止送資料後，資料夾裡不再有它的列
    （或不再有它的檔案），它就從處理名單消失——「逾時無資料」這條規則永遠沒有機會對它評估，
    等於偵測斷線的機制只在『還有舊檔案留在資料夾』時才會動。這裡補上：對資料庫裡啟用中、
    曾經有過資料、但今天沒被處理到的量測點，只跑 `ABSENCE_RULES`，資料來源是資料庫既有的聚合。

    從沒有過任何資料的點（剛建檔、尚未接上）不判定，沒有可以比對的基準。
    """
    errors = errors if errors is not None else []
    window_end = run_window_end(run_date)
    now = rule_now(window_end)
    window_start = (datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc)
                    - timedelta(days=DEFAULT_TREND.min_days * 2))
    rule_configs = repo.get_rule_configs(conn, active_only=False)
    out: list[PointResult] = []

    for device in repo.list_devices(conn):
        for pt in repo.get_points_for_device(conn, device.device_id):
            point_id = pt['point_id']
            if point_id in seen_point_ids:
                continue
            res = PointResult(device_id=device.device_id, position=pt['position'], point_id=point_id)
            try:
                with transaction(conn):
                    ctx = RuleContext(
                        device=device, point_id=point_id, position=pt['position'],
                        agg=repo.get_agg(conn, point_id, window_start, window_end),
                        baseline=repo.get_baseline(conn, point_id), params={}, now=now,
                    )
                    _attach_activity(conn, ctx)
                    if ctx.last_data_at is None:
                        continue
                    outcomes = evaluate_all(ctx, rule_configs, rule_codes=ABSENCE_RULES)
                res.triggered = [o.rule_code for o in outcomes if is_actionable(o.severity)]
                res.observed = [o.rule_code for o in outcomes if not is_actionable(o.severity)]
                res._outcomes = outcomes             # type: ignore[attr-defined]
                res._ctx = ctx                       # type: ignore[attr-defined]
            except Exception as e:
                logger.error(f"{device.device_id}/{pt['position']} 無資料判定失敗：{e}", exc_info=True)
                _note_error(errors, f"無資料判定失敗（{device.device_id}/{pt['position']}）：{e}")
                continue
            out.append(res)
    return out


def _outcome_to_observation(outcome, ctx) -> repo.Observation:
    """
    把 observe 級 `RuleOutcome` 轉成可寫入 `observation` 表的物件。

    鍵值組法與 `vibcore.rules.engine.outcome_to_finding` 重複了幾行——
    本次任務的檔案歸屬不含 `vibcore/rules/`，無法把這段邏輯抽成兩邊
    共用的函式，只能各自維護一份。兩者都是
    `{target_type}:{target}:{issue_type}`，日後若要合併，應在規則層
    另外開一個不含簽核欄位的轉換入口給雙方呼叫。
    """
    target = (ctx.device.device_id if outcome.target_type == 'device'
             else f'{ctx.device.device_id}_{ctx.position}')
    return repo.Observation(
        observation_key=Finding.make_key(outcome.target_type, target, outcome.issue_type),
        device_id=ctx.device.device_id,
        point_id=ctx.point_id if outcome.target_type == 'point' else None,
        target_type=outcome.target_type,
        target=target,
        issue_type=outcome.issue_type,
        family=outcome.family,
        rule_code=outcome.rule_code,
        title=outcome.title,
        detail=outcome.detail,
        baseline_value=outcome.baseline_value,
        current_value=outcome.current_value,
        value_unit=outcome.value_unit,
        evidence=outcome.evidence,
        # 判定當下的時間。不帶的話 repository 會退回 now()，匯入歷史資料
        # 時整批觀察項目會被壓成執行當天，任何過去期間的週報都查不到。
        # 與 outcome_to_finding 的處理一致。
        first_seen_at=ctx.now,
        last_seen_at=ctx.now,
        # 留存觸發當下的門檻，理由同 outcome_to_finding：門檻要靠實際
        # 誤報率迭代，沒有這份快照，日後評估「該不該升為 warn」就只能
        # 重跑整條管線。
        trigger_params=dict(outcome.params or ctx.params or {}),
        interpretation_limit=outcome.interpretation_limit,
        source='rule_engine',
    )


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
    # `old_value` 來自 `repo.get_open_findings()`（DB NUMERIC 欄位，
    # psycopg2 回傳 decimal.Decimal），`new_value`/`baseline_value` 來自
    # 當次 RuleOutcome（一般是 Python float）。混算會直接拋
    # TypeError，讓整批規則引擎的交易失敗——這與本次 observation 任務
    # 無關，是既有的既存缺陷，這裡一併修正（統一轉型為 float 才比較），
    # 否則任何規則的 finding 只要再次觸發，惡化偵測就必定炸掉。
    new_value, old_value = float(new_value), float(old_value)
    if baseline_value is not None:
        baseline_value = float(baseline_value)
    if baseline_value is None:
        delta_pct = abs(new_value - old_value) / abs(old_value or 1) * 100
        return new_value > old_value and delta_pct >= ESCALATE_MIN_DELTA_PCT

    old_dist = abs(old_value - baseline_value)
    new_dist = abs(new_value - baseline_value)
    if old_dist == 0:
        return new_dist > 0
    return (new_dist - old_dist) / old_dist * 100 >= ESCALATE_MIN_DELTA_PCT


def _note_error(errors: list[str], message: str) -> None:
    logger.error(message)
    errors.append(message)


def persist_findings(conn, results: list[PointResult],
                     errors: list[str] | None = None) -> tuple[int, int, int]:
    """
    把規則判定結果寫入資料庫，並偵測惡化。

    `observe` 級判定（`is_actionable()` 為 False）**不建立 Finding**——
    不佔 SLA、不進簽核鏈，理由見 `vibcore.types.SEVERITY_OBSERVE` 的說明：
    這類規則沒有可對外交代的門檻依據，拿去派工只會讓簽核鏈失去公信力。

    但也不能安靜丟掉：完全不留痕跡的話，等累積足夠回饋要把某條規則從
    observe 升為 warn 時就沒有歷史數據可用，週報的觀察名單也會沒有素材，
    使用者更可能誤以為系統這段時間什麼都沒偵測到。因此這裡改寫入獨立的
    `observation` 表（`repo.upsert_observation`）——不與 `finding` 共用
    一張表，是為了不讓 observe 級判定帶著簽核欄位、被介面誤呈現成待辦
    事項（見 db/schema.sql 該表的說明）。`PointResult.observed` 仍保留
    rule_code 清單供執行摘要使用，但落庫與否已與它無關。

    每一筆寫入各自一個 SAVEPOINT。PostgreSQL 的交易一旦遇到 SQL 錯誤（外鍵、約束、缺表），之後每一句
    都被拒絕，最後的 commit 還會安靜地變成 rollback——只靠 try/except 擋 Python 例外，一筆壞資料
    （例如舊庫缺某條規則的設定列，外鍵違反）就會讓當天所有已寫入的東西一起消失，而排程的結束碼是 0。
    現在失敗只丟掉那一筆，並把原因加進 `errors`（呼叫端據此在摘要與結束碼提醒）。

    Returns:
        (finding upsert 筆數, 標記惡化筆數, 實際寫入 observation 的筆數)
    """
    errors = errors if errors is not None else []
    n_upsert = n_escalate = n_observed = 0
    existing = {f['finding_key']: f for f in repo.get_open_findings(conn)}

    for r in results:
        outcomes = getattr(r, '_outcomes', None)
        ctx = getattr(r, '_ctx', None)
        if not outcomes or ctx is None:
            continue

        for outcome in outcomes:
            if not is_actionable(outcome.severity):
                observation = _outcome_to_observation(outcome, ctx)
                try:
                    with transaction(conn):
                        repo.upsert_observation(conn, observation)
                    n_observed += 1
                except Exception as e:
                    _note_error(errors, f"Observation upsert 失敗（{observation.observation_key}）：{e}")
                continue

            finding = outcome_to_finding(outcome, ctx)
            prev = existing.get(finding.finding_key)
            try:
                with transaction(conn):
                    repo.upsert_finding(conn, finding)
                n_upsert += 1
            except Exception as e:
                _note_error(errors, f"Finding upsert 失敗（{finding.finding_key}）：{e}")
                continue

            # 惡化偵測：與上一次的數值比，而非與基準比
            if prev and _is_worse(finding.current_value,
                                  prev.get('current_value'),
                                  finding.baseline_value):
                try:
                    with transaction(conn):
                        repo.mark_escalated(
                            conn, finding.finding_key,
                            note=f"數值由 {prev.get('current_value')} 變為 "
                                 f"{finding.current_value}，較上次更偏離基準",
                        )
                    n_escalate += 1
                except Exception as e:
                    _note_error(errors, f"標記惡化失敗（{finding.finding_key}）：{e}")

    return n_upsert, n_escalate, n_observed


def auto_resolve_quiet(conn, quiet_days: int = DEFAULT_QUIET_DAYS,
                       now: datetime | None = None,
                       errors: list[str] | None = None) -> int:
    """
    自動結案：連續 `quiet_days` 天未再觸發的未結案事項。

    `now` 是「現在」的基準，預設牆上時鐘。補跑／重放歷史日期時必須傳該批資料的結束時間
    （見 `rule_now`）：`last_seen_at` 是規則判定時的 `now`（歷史日期），若拿牆上時鐘去比，
    每件剛建立的事項都會被當成「已經安靜好幾天」而在同一次排程內結案，隔天再觸發又重開新案，
    首次出現時間與簽核進度全部被洗掉。

    判準用 `last_seen_at`——該欄位只在規則再次觸發時更新，因此
    「距今超過 N 天沒更新」就等於「規則已連續 N 天沒再判定為異常」，
    不需要另外維護計數器。

    刻意不要求走完簽核鏈：問題本身消失時直接結案才是對的，卡住的簽核
    步驟不該讓一件已經好了的事永遠掛著。歷程紀錄保留供追溯。
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=quiet_days)
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
            with transaction(conn):
                repo.auto_resolve(
                    conn, row['finding_key'],
                    note=f"連續 {quiet_days} 天未再觸發（最後一次 "
                         f"{last_seen.date()}），由系統自動結案",
                )
            n += 1
        except Exception as e:
            msg = f"自動結案失敗（{row['finding_key']}）：{e}"
            logger.warning(msg)
            if errors is not None:
                errors.append(msg)
    return n


# ──────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────

def pending_migrations(conn) -> list[str]:
    """
    資料庫結構／種子資料還沒跟上程式的地方（判斷方式見 `vibcore.check.migration_status`），只讀。

    舊庫缺 migration 時，排程不會報錯停下來，而是安靜地做錯事：缺 `observation` 表時觀察項目寫不進去，
    缺某條規則的設定列時該規則的觀察項目因外鍵而寫不進去，觀察級規則的嚴重度停在 warn 時它們會被當成
    要派工的事項。所以每次排程開頭先看一眼，缺什麼就大聲講。
    """
    from vibcore.check import migration_status
    try:
        with transaction(conn), conn.cursor() as cur:
            return [f"{key}：{summary}（db/{fname}）"
                    for key, fname, summary, ok in migration_status(cur) if ok is False]
    except Exception as e:                      # 檢查本身壞掉不該擋住排程
        logger.warning(f"無法檢查資料庫結構版本：{e}")
        return []



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
    pending = pending_migrations(conn)
    if pending:
        logger.error("資料庫有 migration 尚未套用，判定與寫入可能不正確——請先執行 "
                     "`python -m vibcore.check` 並依指示補套用：\n  " + "\n  ".join(pending))

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

    seen = {p.point_id for p in result.points if p.point_id is not None}
    result.silent = process_silent_points(conn, seen, run_date, errors=result.persist_errors)

    result.findings_upserted, result.escalated, result.observed_count = (
        persist_findings(conn, result.points + result.silent, errors=result.persist_errors)
    )
    result.auto_resolved = auto_resolve_quiet(conn, quiet_days,
                                              now=rule_now(run_window_end(run_date)),
                                              errors=result.persist_errors)
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
    # 全部量測點都失敗、或有任何一筆寫入失敗，結束碼就不是 0——排程工具才有辦法提醒。
    return 1 if (result.n_failed and not result.n_ok) or result.persist_errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
