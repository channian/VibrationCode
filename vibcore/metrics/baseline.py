"""
baseline.py — 基準期偵測與統計

基準期是所有相對判定（σ 偏離、趨勢百分比、ISO 等級合理性檢查……）的參考
原點；原點本身若失真，後面所有比較都失真，且不會有任何症狀提醒你原點錯了。
因此本模組的核心原則只有一句話：**基準統計只能用 `data_status == 'ok'`
的小時**——

- `partial`：樣本數不足，數字本身不可信，混進來會讓 median/std 偏向那些
  不完整的小時。
- `not_running`：正常停機，不是設備本身的振動水準；備機一天可能只跑
  兩小時，把停機時段算進基準會把基準拉到接近零，之後任何真正運轉時的
  讀數都會被誤判成「大幅偏離基準」。
- `no_data`：感測器斷線，根本沒有數字可信。

沿用舊版 `src/baseline_detector.py`「滾動窗口掃描 + 穩定度評分（中位數 ×
變異係數，越低代表越平穩）」的核心概念，但改在每小時聚合資料上運作，並
加入「窗口完整度」門檻：舊版是逐筆時域資料，樣本量大，稀疏缺口不太影響
統計量；新版是每小時一筆，一個窗口動輒缺數十小時就會讓 median/CV 建立在
少數倖存樣本上，因此窗口內 ok 小時佔比不足時，即使殘存樣本看起來很穩定，
也不應被選為基準（見計畫書 §三）。
"""

import logging
from dataclasses import dataclass

import pandas as pd

from vibcore.config import AGG_SPEC, DataStatus
from vibcore.types import BaselineStats, MetricStats

logger = logging.getLogger(__name__)

#: 基準統計建議的最少有效（ok）小時數。低於此值統計量（尤其是 std）
#: 不具代表性；設為一週是因為多數場域至少有日夜/負載週期，一週能涵蓋
#: 一個完整週期，而不是隨機捕捉到某天的偏態分佈。
MIN_BASELINE_HOURS = 24 * 7

#: 自動偵測時，用來評分「這段期間穩不穩」的指標優先序。
#: acc_oa 對應舊版 baseline_detector 的 accOA；若聚合資料缺這個欄位
#: （例如上游 AGG_SPEC 調整），依序退而求其次。
#: 穩定度評分指標的候選順序（依序找資料中實際存在者）。
#: `vel_rms` 排第一：它的單位與定義已用 rawdata 實測驗證，也是 ISO 10816
#: 的判定量。`acc_oa` 刻意排在後面——其合成方式與單位至今無法驗證
#: （與 accRMS 相差 549 倍且非向量和，見計畫書 §三之二），不適合當主要
#: 評分依據。
_SCORE_METRIC_CANDIDATES = ('vel_rms', 'acc_rms', 'acc_oa')


@dataclass(frozen=True)
class BaselineConfig:
    """
    自動基準偵測（`detect_baseline`）的滾動窗口參數。

    型別契約（`vibcore/types.py`）與整體設定（`vibcore/config.py`）已由
    其他模組共用，基準偵測特有的掃描參數獨立放在這裡，避免把單一用途的
    設定塞進共用的 `config.py`。
    """

    #: 滾動窗口大小（天）。14 天沿用舊版 `src/baseline_detector.py` 的
    #: 經驗值——短於一台設備常見的維護/負載週期，但足以平滑掉單日波動。
    window_days: int = 14

    #: 滾動步長（天）
    step_days: int = 1

    #: 窗口內「ok 小時數 / 窗口總小時數」需達此比例，避免選中大量缺口、
    #: 只是殘存樣本恰好穩定的窗口（見模組說明）。
    min_ok_ratio: float = 0.5

    #: 窗口內至少需要這麼多 ok 小時，統計量才有意義
    min_ok_hours: int = MIN_BASELINE_HOURS

    #: 第一層依穩定度分數排序後，保留前 N 個窗口進入最終選定
    #: （目前僅取分數最低者，保留此欄位對齊舊版三層篩選的設計意圖，
    #: 供未來加入第二層人工複核候選清單時使用）
    prefilter_n: int = 10

    #: 穩定度評分依據的指標（須為 `vibcore.config.AGG_SPEC` 的鍵名）
    score_metric: str = 'vel_rms'


DEFAULT_BASELINE_CFG = BaselineConfig()


def _ok_rows(agg: pd.DataFrame) -> pd.DataFrame:
    """篩出可信的列；`data_status` 欄不存在時視為沒有任何可信資料。"""
    if agg is None or agg.empty or 'data_status' not in agg.columns:
        return agg.iloc[0:0] if agg is not None else pd.DataFrame()
    return agg[agg['data_status'] == DataStatus.OK]


def compute_baseline_stats(agg: pd.DataFrame,
                            metrics: list[str],
                            start,
                            end,
                            point_id: int | str = '') -> BaselineStats:
    """
    計算指定期間 `[start, end)` 內各指標的基準統計量。

    只採計 `data_status == 'ok'` 的列（見模組說明）；不論呼叫端是人工
    指定的基準期，還是 `detect_baseline()` 內部逐窗口評分，都走同一套
    「只信任 ok 列」規則，確保兩條路徑產出的基準統計標準一致。

    有效小時數不足 `MIN_BASELINE_HOURS` 時**不會拒絕計算**（呼叫端可能
    是人工指定、明知期間偏短仍要看一下大致水準），但會記錄警告並在
    `note` 中說明，避免統計量被無聲地當成可靠基準使用。

    Args:
        agg: 每小時聚合結果（含 `ts_hour` / `data_status` 與各指標欄）。
        metrics: 要計算統計量的指標名稱清單（`vibcore.config.AGG_SPEC`
                 的鍵名，例如 `'vel_rms'`）。
        start, end: 基準期範圍，`start` 含、`end` 不含（`[start, end)`）。
        point_id: 量測點識別碼。**型別契約缺口**：`BaselineStats.point_id`
                  為必填欄位，但本函式簽章未包含此參數；呼叫端若不提供，
                  將以空字串佔位，回傳物件的 `point_id` 需由上游自行補上
                  才能安全寫入 DB（見本檔案最下方的整合說明）。

    Returns:
        BaselineStats。輸入為空或期間內完全沒有 ok 資料時，`stats` 為
        空字典、`n_hours` 為 0，並在 `note` 中說明原因——**不會**回傳
        None（回傳型別不允許），呼叫端須自行檢查 `n_hours` 是否為 0。
    """
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    if agg is None or agg.empty or 'ts_hour' not in agg.columns:
        logger.warning("compute_baseline_stats：輸入聚合資料為空或缺少 ts_hour 欄，無法計算基準統計")
        return BaselineStats(
            point_id=point_id, start_date=start_ts.date(), end_date=end_ts.date(),
            source='manual', stats={}, n_hours=0,
            note='輸入聚合資料為空或缺少 ts_hour 欄，無法計算基準統計',
        )

    window = agg[(agg['ts_hour'] >= start_ts) & (agg['ts_hour'] < end_ts)]
    ok = _ok_rows(window)
    n_hours = len(ok)

    note = ''
    if n_hours == 0:
        note = f'期間 {start_ts.date()}~{end_ts.date()} 內沒有 data_status == ok 的小時，無法計算基準統計'
        logger.warning(f"compute_baseline_stats：{note}")
    elif n_hours < MIN_BASELINE_HOURS:
        note = (f'期間內僅 {n_hours} 小時為可信（ok）資料，'
                f'低於建議下限 {MIN_BASELINE_HOURS} 小時（約一週），統計量（尤其 std）可能不穩定')
        logger.warning(f"compute_baseline_stats：{note}（{start_ts.date()}~{end_ts.date()}）")

    stats: dict[str, MetricStats] = {}
    for metric in metrics:
        if metric not in ok.columns:
            continue
        col = pd.to_numeric(ok[metric], errors='coerce').dropna()
        if col.empty:
            continue
        stats[metric] = MetricStats(
            median=float(col.median()),
            mean=float(col.mean()),
            std=float(col.std()) if len(col) > 1 else 0.0,
            n=int(len(col)),
        )

    return BaselineStats(
        point_id=point_id, start_date=start_ts.date(), end_date=end_ts.date(),
        source='manual', stats=stats, n_hours=n_hours, note=note,
    )


def _candidate_windows(t_min: pd.Timestamp, t_max: pd.Timestamp,
                        cfg: BaselineConfig) -> tuple[list[tuple[pd.Timestamp, pd.Timestamp]], float]:
    """
    產生滾動窗口的 (start, end) 清單，回傳 (窗口清單, 每個窗口的名目總小時數)。

    資料跨度不足一個完整窗口時，退而求其次改用整個觀測範圍當唯一候選——
    對應舊版 `BaselineDetector.detect()` 的相同處理，讓資料量不足時仍能
    嘗試一次評分，而不是直接放棄。
    """
    window = pd.Timedelta(days=cfg.window_days)
    step = pd.Timedelta(days=cfg.step_days)
    span_hours = (t_max - t_min) / pd.Timedelta(hours=1)
    window_hours = cfg.window_days * 24

    if span_hours < window_hours:
        logger.warning(
            f"detect_baseline：資料時間跨度僅 {span_hours:.1f} 小時，"
            f"小於窗口 {window_hours} 小時，改以整個觀測範圍作為唯一候選窗口"
        )
        return [(t_min, t_max + pd.Timedelta(hours=1))], max(span_hours, 1.0)

    windows = []
    current = t_min
    while current + window <= t_max + step:
        windows.append((current, current + window))
        current += step
    return windows, float(window_hours)


def _resolve_score_metric(agg: pd.DataFrame, cfg: 'BaselineConfig') -> str | None:
    """
    決定穩定度評分要用哪個指標。

    依「設定值 → 候選清單」的順序，挑第一個**在資料中真的存在且有非空值**
    的欄位。找不到時回傳 None，由呼叫端明確報告原因，而不是讓後續流程
    默默地每個窗口都失敗。
    """
    ordered = [cfg.score_metric, *(_m for _m in _SCORE_METRIC_CANDIDATES
                                   if _m != cfg.score_metric)]
    for metric in ordered:
        if metric in agg.columns and agg[metric].notna().any():
            if metric != cfg.score_metric:
                logger.info(f"detect_baseline：設定的評分指標 {cfg.score_metric!r} "
                            f"在資料中無值，改用 {metric!r}")
            return metric
    return None


def detect_baseline(agg: pd.DataFrame,
                     cfg: BaselineConfig = DEFAULT_BASELINE_CFG,
                     point_id: int | str = '') -> BaselineStats | None:
    """
    自動掃描 `agg` 找出最穩定的期間作為基準。

    沿用舊版 `src/baseline_detector.py` 的核心概念——滾動窗口掃描，取
    `median × CV`（穩定度分數，越低代表水準低且波動小）最低者——但做了
    兩項調整以配合每小時聚合資料：

    1. 評分前先用 `compute_baseline_stats()` 只取 `data_status == 'ok'`
       的小時，`partial`/`not_running`/`no_data` 一律不計入（見模組說明）。
    2. 額外要求窗口的「ok 小時佔窗口總小時數」比例達到 `cfg.min_ok_ratio`。
       每小時一筆的資料量遠小於逐秒資料，一個窗口若有大半是斷線或停機，
       殘存的少數 ok 樣本可能剛好很穩定（因為根本沒幾筆），穩定度分數會
       失真地低——這正是「窗口內含大量缺口不應被選為基準」的直接原因。

    找不到任何滿足門檻的窗口時（資料太短、太多缺口，或全設備幾乎沒運轉
    過），回傳 `None` 並記錄警告，**不會**硬選一個不可靠的窗口充數。

    Args:
        agg: 該量測點的每小時聚合結果。
        cfg: 滾動窗口掃描參數，見 `BaselineConfig`。
        point_id: 量測點識別碼，會寫入回傳的 `BaselineStats.point_id`
                  （型別契約缺口，見 `compute_baseline_stats` 的說明）。

    Returns:
        穩定度分數最低、且通過完整度與樣本數門檻的 `BaselineStats`
        （`source='auto'`）；找不到合格候選時回傳 `None`。
    """
    if agg is None or agg.empty or 'ts_hour' not in agg.columns:
        logger.warning("detect_baseline：輸入聚合資料為空或缺少 ts_hour 欄，無法偵測基準期")
        return None

    metrics = list(AGG_SPEC.keys())
    # 評分指標必須以「資料中實際存在且有值」為準，不能只看設定檔有沒有列。
    # 否則設定指向一個該設備沒有的欄位時，每個窗口都會取不到分數而被略過，
    # 最終錯報成「ok 小時數不足」——診斷訊息會把人帶往完全錯誤的方向。
    score_metric = _resolve_score_metric(agg, cfg)
    if score_metric is None:
        logger.warning(
            f"detect_baseline：資料中找不到任何可用的穩定度評分指標"
            f"（設定為 {cfg.score_metric!r}，候選 {_SCORE_METRIC_CANDIDATES}），"
            f"現有欄位：{[c for c in agg.columns if c in AGG_SPEC]}。回傳 None"
        )
        return None

    t_min = agg['ts_hour'].min()
    t_max = agg['ts_hour'].max()
    windows, window_hours = _candidate_windows(t_min, t_max, cfg)

    scored = []
    for w_start, w_end in windows:
        stats = compute_baseline_stats(agg, metrics, w_start, w_end, point_id=point_id)

        if stats.n_hours < cfg.min_ok_hours:
            continue
        ok_ratio = stats.n_hours / window_hours if window_hours > 0 else 0.0
        if ok_ratio < cfg.min_ok_ratio:
            continue

        ms = stats.stats.get(score_metric)
        if ms is None or ms.mean == 0 or pd.isna(ms.mean):
            continue
        cv = ms.std / ms.mean
        if pd.isna(cv):
            continue

        score = ms.median * cv
        scored.append((score, ok_ratio, stats))

    if not scored:
        logger.warning(
            "detect_baseline：掃描全部窗口後，沒有任何候選同時滿足「ok 小時數 "
            f">= {cfg.min_ok_hours}」與「完整度 >= {cfg.min_ok_ratio:.0%}」，"
            "回傳 None（拒絕硬選一個不可靠的基準期）"
        )
        return None

    scored.sort(key=lambda t: t[0])
    best_score, best_ratio, best_stats = scored[0]
    best_stats.source = 'auto'

    logger.info(
        f"detect_baseline：選定 {best_stats.start_date}~{best_stats.end_date} 為基準期，"
        f"score={best_score:.4f}（{score_metric}），ok_hours={best_stats.n_hours}，"
        f"窗口完整度={best_ratio:.1%}，候選窗口總數={len(scored)}"
    )
    return best_stats
