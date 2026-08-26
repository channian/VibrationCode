#!/usr/bin/env python3
"""
offline.py — 離線回測主程式

用途：拿歷史 Analytic CSV 跑過一次完整管線（聚合 → 涵蓋率 → 基準期 →
規則），回答上線前最關鍵的問題——**這套規則跑過去這幾個月的資料，會噴
幾件 Finding？分佈在哪些設備、哪些規則？門檻該怎麼調？**

規則層有 8 條規則（`VEL_HIGH`／`IMPACT_RISE`／`AXIS_SHIFT`／
`ORIENTATION_CHANGE`／`SENSOR_OFFLINE`／`DATA_QUALITY`／
`SENSOR_SATURATION`／`STANDBY_NO_RUNTIME`）在撰寫本檔當下由其他人平行
開發中，尚未完成，本程式透過 `validate.rules_stub` 的可替換 stub 頂上；
另外 5 條（`ISO_ZONE`／`ISO_CLASS_SUSPECT`／`STEP_CHANGE`／
`DEGRADE_TREND`／`SPECTRAL_SHIFT`）與基準期計算已接上真實的
`vibcore.metrics.*` 模組。哪些已經是真實模組、哪些還是 stub，見
`validate/baseline_stub.py` / `validate/rules_stub.py` 檔頭的說明，以及
執行後 `summary.txt` 開頭的「指標／規則層實作來源」區塊——**用 stub 跑
出來的那幾條規則，門檻建議只能當「量級」參考，不能直接拿去上線**，等
真實模組接上後務必重跑一次。

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
from validate.points import load_points
from validate.report import write_reports
from validate.rule_defaults import load_rule_configs
from validate.rules_stub import USING_REAL_DEVIATION, USING_REAL_ISO, USING_REAL_TREND

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
    p.add_argument('--sweep', action='append', default=[], metavar='RULE:param:v1,v2,v3',
                   help='額外加入的門檻掃描，可重複給多次')
    p.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    return p


def _detect_samples_per_hour(points) -> int | None:
    """
    從資料本身推測「每小時應有幾筆」。

    聚合層預設 3600（每秒一筆，對應正式環境的前端輸出），但回測資料
    未必是這個密度——歷史匯出可能降採樣過，合成測試資料也常用每分鐘
    一筆。密度猜錯的後果不是報錯，而是**每一小時都被判為 partial、
    所有指標型規則靜默跳過、報告顯示「零觸發」**，看起來像規則校準良好，
    實際上什麼都沒評估過。這種錯誤比直接崩潰危險得多，所以寧可自動偵測。

    取各量測點「資料最密集的那一小時」的筆數中位數，避免被頭尾不完整的
    小時拉低。
    """
    densities = []
    for p in points:
        df = getattr(p, 'raw', None)
        if df is None or df.empty or 'datetime' not in df.columns:
            continue
        per_hour = df.groupby(df['datetime'].dt.floor('h')).size()
        if not per_hour.empty:
            densities.append(int(per_hour.max()))
    if not densities:
        return None

    detected = int(pd.Series(densities).median())
    if detected >= 3000:
        return None      # 已接近每秒一筆，用預設值即可
    logger.warning(
        f"偵測到資料密度約每小時 {detected} 筆（預設假設為 "
        f"{DEFAULT_AGG.expected_samples_per_hour} 筆）。已自動改用偵測值，"
        f"若不正確請用 --samples-per-hour 明確指定"
    )
    return detected


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

    points = load_points(args.data_dir, args.pattern, args.device_meta)
    if not points:
        logger.error(f"{args.data_dir} 沒有讀到任何量測點資料，中止")
        return 1

    rule_configs = load_rule_configs(args.rule_config)
    n_active = sum(1 for r in rule_configs.values() if r.is_active)
    logger.info(f"載入 {len(rule_configs)} 條規則設定（{n_active} 條啟用中）")

    agg_overrides = {}
    if args.samples_per_hour is not None:
        agg_overrides['expected_samples_per_hour'] = args.samples_per_hour
    else:
        detected = _detect_samples_per_hour(points)
        if detected is not None:
            agg_overrides['expected_samples_per_hour'] = detected
    if args.min_running_samples is not None:
        agg_overrides['min_running_samples'] = args.min_running_samples
    agg_cfg = dataclasses.replace(DEFAULT_AGG, **agg_overrides) if agg_overrides else DEFAULT_AGG

    result = run_backtest(points, rule_configs, agg_cfg=agg_cfg)
    logger.info(f"回測完成：{result.n_devices} 台設備、{result.n_points} 個量測點、"
               f"{len(result.episodes_df)} 個觸發事件")

    _abort_if_nothing_analyzable(result)

    sweep_df = None
    if not args.no_sweep:
        specs = list(_DEFAULT_SWEEPS) + [_parse_sweep_arg(s) for s in args.sweep]
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
        '其餘規則（VEL_HIGH/IMPACT_RISE/軸能量/事件類，暫用 validate/rules_stub.py）': False,
    }

    written = write_reports(result, rule_configs, args.out_dir, sweep_df, using_real)
    print('\n報告已產出：')
    for name, path in written.items():
        print(f"  {name:24s} {path}")
    print(f"\n摘要：{written.get('summary_txt')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
