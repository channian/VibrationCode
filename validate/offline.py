#!/usr/bin/env python3
"""
offline.py — 離線回測主程式

用途：拿歷史 Analytic CSV 跑過一次完整管線（聚合 → 涵蓋率 → 基準期 →
規則），回答上線前最關鍵的問題——**這套規則跑過去這幾個月的資料，會噴
幾件 Finding？分佈在哪些設備、哪些規則？門檻該怎麼調？**

`validate/rules_stub.py` 保留了每條規則的簡化版，但只在真實實作尚未存在
時才會被用到——`vibcore.rules` 完成後即自動逐條覆蓋。當前哪幾條是真的、
哪幾條還是簡化版，由執行時實際檢查後寫入 `summary.txt` 開頭的「指標／
規則層實作來源」區塊，**不是寫死的字串**。早期版本把這段寫死，導致真實
規則早已接上、報告卻仍標示為 stub，使用者因而不敢採用正確的門檻建議。
若該區塊指出某幾條仍是簡化版，那幾條的門檻建議只能當量級參考。

用法：
    python -m validate.offline --data-dir data/
    python -m validate.offline --data-dir data/ --device-meta validate/device_meta.example.json
    python -m validate.offline --data-dir data/ --no-sweep         # 略過門檻掃描（較快）
    python -m validate.offline --data-dir data/ --out-dir /tmp/x   # 自訂輸出目錄

詳細說明見 `validate/README.md`。
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys

import pandas as pd

from vibcore.config import DEFAULT_AGG
from validate.backtest import run_backtest, sweep_threshold
from validate.baseline_stub import USING_REAL_BASELINE
from validate.points import load_points, parse_iso_assumption, trim_points
from validate.report import write_reports
from validate.rule_defaults import load_rule_configs
from validate.rules_stub import (
    REAL_RULE_CODES,
    STUB_RULE_CODES,
    USING_REAL_DEVIATION,
    USING_REAL_ISO,
    USING_REAL_TREND,
)

logger = logging.getLogger(__name__)

#: 預設的門檻敏感度掃描——挑選規則集中「以標準差為門檻」的三條規則，
#: 這類規則對誤報洪水最敏感（σ 訂太低，統計上一定會頻繁越界）。
#: 需要掃別的規則／參數，用 `--sweep RULE_CODE:param:v1,v2,v3` 附加。
_DEFAULT_SWEEPS = [
    ('VEL_HIGH', 'sigma', [2.0, 2.5, 3.0, 3.5, 4.0]),
    ('IMPACT_RISE', 'crest_sigma', [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]),
    ('STEP_CHANGE', 'mahalanobis_sigma', [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]),
]


def _parse_sweep_arg(spec: str) -> tuple[str, str, list[float]]:
    try:
        rule_code, param_name, values_raw = spec.split(':')
        values = [float(v) for v in values_raw.split(',')]
        return rule_code, param_name, values
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"--sweep 格式需為 RULE_CODE:param:v1,v2,v3，收到 {spec!r}（{e}）") from e


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description='離線回測：用歷史資料驗證規則集是否合理')
    p.add_argument('--data-dir', required=True, help='存放 Analytic CSV 的資料夾')
    p.add_argument('--pattern', default='*.csv', help='檔名比對樣式（預設 *.csv）')
    p.add_argument('--device-meta', default=None,
                   help='JSON 檔，補充 is_standby / iso_machine_class 等台帳資訊'
                        '（Analytic CSV 本身不含這些欄位）')
    p.add_argument('--assume-iso', default=None, metavar='GROUP/FOUNDATION',
                   help='ISO 分類假設，例如 3/rigid。Analytic CSV 沒有「基礎剛性」欄位，'
                        '不給則所有設備視為未分類（ISO_ZONE 不觸發、VEL_HIGH 走 sigma_fallback）。'
                        '用來做「若這批設備其實是 X 分類，告警量會變多少」的敏感度分析')
    p.add_argument('--rule-config', default=None,
                   help='JSON 檔，整批覆寫規則參數（不給則用 db/schema.sql 的預設 seed）')
    p.add_argument('--out-dir', default='output/validation', help='報告輸出目錄')
    p.add_argument('--no-sweep', action='store_true', help='略過門檻敏感度掃描（較快，但少一張關鍵表）')
    p.add_argument('--samples-per-hour', type=int, default=None,
                   help='每小時預期樣本數（預設 3600，即每秒一筆）。'
                        '合成測試資料若用分鐘級取樣，需相應調低，否則涵蓋率永遠算成 partial')
    p.add_argument('--min-running-samples', type=int, default=None,
                   help='判定「該小時指標具代表性」所需的最少運轉樣本數（預設 60）；'
                        '搭配 --samples-per-hour 一起調整合成資料的取樣密度')
    p.add_argument('--since', default=None, metavar='YYYY-MM-DD',
                   help='只回測此日期（含）之後的資料')
    p.add_argument('--until', default=None, metavar='YYYY-MM-DD',
                   help='只回測此日期（含）之前的資料')
    p.add_argument('--latest-cadence-only', action='store_true',
                   help='匯出檔混雜不同前端版本（每秒／每 10 分鐘）時，'
                        '只保留每個量測點最近一段連續同密度的資料。'
                        '逐點各自裁切，不是統一切一個日期——各設備換版時間不同，'
                        '統一日期會丟掉早就換好版的點的好資料')
    p.add_argument('--sweep', action='append', default=[], metavar='RULE:param:v1,v2,v3',
                   help='額外加入的門檻掃描，可重複給多次')
    p.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    return p


def _report_cadence_mix(points) -> None:
    """
    盤點各量測點的取樣密度並回報，不做任何覆寫。

    早期版本會算出一個「全廠代表密度」去覆寫聚合設定，那是錯的：不同設備
    換版時間不同，同一份匯出裡可能同時有每秒（即時量測）與每 10 分鐘
    （長期量測）兩種資料，強押單一密度必然讓其中一種全部誤判為 partial。
    密度改由聚合層逐日推估（見 `aggregate.detect_cadence_segments`），
    這裡只負責讓使用者看見資料實際長什麼樣。

    特別要點名「單一量測點內密度改變」的情形——這代表該點跨越了前端改版，
    跨段的基準期不可比，尤其 PEAK/CREST/KURT 這類取最大值的指標會隨取樣
    密度系統性偏移，看起來像設備狀態突然改變。
    """
    from vibcore.pipeline.aggregate import detect_cadence_segments

    tally: dict[int, int] = {}
    switched: list[str] = []
    for p in points:
        df = getattr(p, 'raw', None)
        if df is None or df.empty:
            continue
        seg = detect_cadence_segments(df)
        if seg.empty:
            continue
        for sph in seg['samples_per_hour'].unique():
            tally[int(sph)] = tally.get(int(sph), 0) + 1
        if len(seg) > 1:
            switched.append(
                f"{p.device.device_id}/{p.position}："
                + "→".join(f"{int(r.samples_per_hour)}筆/時" for r in seg.itertuples())
            )

    if not tally:
        return

    desc = "、".join(f"每小時 {sph} 筆（{n} 個量測點）"
                     for sph, n in sorted(tally.items(), reverse=True))
    logger.info(f"資料取樣密度盤點：{desc}")

    if switched:
        logger.warning(
            "\n" + "=" * 62 +
            f"\n⚠ {len(switched)} 個量測點在觀測期內取樣密度改變（混雜前端版本）："
            + "".join(f"\n    {s}" for s in switched[:10])
            + (f"\n    …另有 {len(switched) - 10} 個" if len(switched) > 10 else "")
            + "\n  各段已各自套用對應門檻，可分析比例不受影響；但**跨段的基準期"
              "不可比**，\n  這些點的趨勢與突變類判定請保守解讀。"
            + "\n" + "=" * 62
        )


def _abort_if_nothing_analyzable(result) -> None:
    """
    可分析資料為零時大聲警告。

    回測「跑完了、報告也產出了、但零觸發」是最容易被誤讀為「規則沒問題」
    的情境。若實際上沒有任何一小時是 ok 狀態，那是資料或設定的問題，
    結論完全無效，必須明講而不是讓使用者自行從涵蓋率表裡看出來。
    """
    cov = getattr(result, 'coverage_df', None)
    if cov is None or cov.empty:
        return
    col = next((c for c in cov.columns if 'analyzable' in c.lower() or '可分析' in c), None)
    if col is None:
        return
    if float(pd.to_numeric(cov[col], errors='coerce').fillna(0).max()) > 0:
        return

    logger.error(
        '\n' + '=' * 62 +
        '\n⚠ 沒有任何一小時達到可分析（ok）狀態，本次回測結果不具意義。'
        '\n  所有指標型規則都被跳過，「零觸發」不代表規則校準良好。'
        '\n  常見原因：資料密度與 --samples-per-hour 不符，或設備確實整段未運轉。'
        '\n  請檢查 coverage.csv 的資料不全/未運轉時數後重跑。'
        '\n' + '=' * 62
    )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                        datefmt='%H:%M:%S')

    try:
        assume_iso = parse_iso_assumption(args.assume_iso)
    except ValueError as e:
        logger.error(str(e))
        return 2
    if assume_iso is not None:
        logger.info(f"ISO 分類假設：{'/'.join(assume_iso)}"
                    "（這是假設值，不是台帳實際資料——報告引用時務必註明）")
    points = load_points(args.data_dir, args.pattern, args.device_meta, assume_iso=assume_iso)
    if not points:
        logger.error(f"{args.data_dir} 沒有讀到任何量測點資料，中止")
        return 1

    rule_configs = load_rule_configs(args.rule_config)
    n_active = sum(1 for r in rule_configs.values() if r.is_active)
    logger.info(f"載入 {len(rule_configs)} 條規則設定（{n_active} 條啟用中）")

    if args.since or args.until or args.latest_cadence_only:
        points = trim_points(
            points,
            since=pd.Timestamp(args.since) if args.since else None,
            until=pd.Timestamp(args.until) if args.until else None,
            latest_cadence_only=args.latest_cadence_only,
        )
        if not points:
            logger.error("裁切後沒有任何量測點還有資料，請放寬 --since/--until，中止")
            return 1

    _report_cadence_mix(points)

    # 未明確指定時交給聚合層逐日推估密度（可處理同一點內混雜前端版本）；
    # 明確指定時關閉自動偵測，否則使用者給的值會被自動偵測覆蓋掉，
    # 「指定了卻沒作用」是最難查的那種問題。
    agg_overrides = {}
    auto_density = args.samples_per_hour is None
    if args.samples_per_hour is not None:
        agg_overrides['expected_samples_per_hour'] = args.samples_per_hour
    if args.min_running_samples is not None:
        agg_overrides['min_running_samples'] = args.min_running_samples
    agg_cfg = dataclasses.replace(DEFAULT_AGG, **agg_overrides) if agg_overrides else DEFAULT_AGG

    result = run_backtest(points, rule_configs, agg_cfg=agg_cfg,
                          auto_detect_density=auto_density)
    logger.info(f"回測完成：{result.n_devices} 台設備、{result.n_points} 個量測點、"
               f"{len(result.episodes_df)} 個觸發事件")

    _abort_if_nothing_analyzable(result)

    sweep_df = None
    if not args.no_sweep:
        # 使用者指定的掃描優先於預設：同一個 (規則, 參數) 只保留使用者那組，
        # 否則 CSV 會出現同一條規則兩份掃描結果（值還可能不同），分析時
        # groupby 會把兩者混在一起，看起來像資料出錯。
        user_specs = [_parse_sweep_arg(s) for s in args.sweep]
        overridden = {(rc, pn) for rc, pn, _ in user_specs}
        specs = [sp for sp in _DEFAULT_SWEEPS if (sp[0], sp[1]) not in overridden] + user_specs
        frames = []
        for rule_code, param_name, values in specs:
            if rule_code not in rule_configs:
                logger.warning(f"掃描設定提到未知規則 {rule_code}，略過")
                continue
            logger.info(f"門檻敏感度掃描：{rule_code}.{param_name} = {values}")
            frames.append(sweep_threshold(result.point_contexts, rule_code, param_name,
                                          values, rule_configs))
        sweep_df = pd.concat(frames, ignore_index=True) if frames else None

    using_real = {
        'ISO 分級（vibcore.metrics.iso）': USING_REAL_ISO,
        '多變量偏離（vibcore.metrics.deviation）': USING_REAL_DEVIATION,
        '趨勢分析（vibcore.metrics.trend）': USING_REAL_TREND,
        '基準期計算（vibcore.metrics.baseline）': USING_REAL_BASELINE,
    }
    # 規則層據實標示，不寫死。若全部接上真實實作就明講，否則列出還在用
    # 簡化版的規則代碼——使用者需要知道「哪幾條」不可據以定門檻，而不是
    # 看到一句籠統的警告就對整份報告失去信心。
    if STUB_RULE_CODES:
        using_real[f"規則層（{len(STUB_RULE_CODES)} 條仍為簡化版："
                   f"{'、'.join(sorted(STUB_RULE_CODES))}）"] = False
        using_real[f"規則層（其餘 {len(REAL_RULE_CODES)} 條）"] = True
    else:
        using_real[f'規則層 {len(REAL_RULE_CODES)} 條全部（vibcore.rules）'] = True

    written = write_reports(result, rule_configs, args.out_dir, sweep_df, using_real)
    print('\n報告已產出：')
    for name, path in written.items():
        print(f"  {name:24s} {path}")
    print(f"\n摘要：{written.get('summary_txt')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
