#!/usr/bin/env python3
"""
offline.py — 離線回測主程式

用途：拿歷史 Analytic CSV 跑過一次完整管線（聚合 → 涵蓋率 → 基準期 →
規則），回答上線前最關鍵的問題——**這套規則跑過去這幾個月的資料，會噴
幾件 Finding？分佈在哪些設備、哪些規則？門檻該怎麼調？**

指標層（基準期、趨勢）與規則層在撰寫本檔當下由其他人平行開發中，尚未
完成，因此本程式透過 `validate.baseline_stub` / `validate.rules_stub`
的可替換 stub 頂上；哪些已經是真實模組、哪些還是 stub，見這兩個檔案的
說明，以及執行後 `summary.txt` 開頭的「指標／規則層實作來源」區塊——
**用 stub 跑出來的門檻建議只能當「量級」參考，不能直接拿去上線**，等
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
import logging
import sys

import pandas as pd

from validate.backtest import run_backtest, sweep_threshold
from validate.baseline_stub import USING_REAL_BASELINE
from validate.points import load_points
from validate.report import write_reports
from validate.rule_defaults import load_rule_configs
from validate.rules_stub import USING_REAL_DEVIATION, USING_REAL_ISO

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
    p.add_argument('--sweep', action='append', default=[], metavar='RULE:param:v1,v2,v3',
                   help='額外加入的門檻掃描，可重複給多次')
    p.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    return p


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

    result = run_backtest(points, rule_configs)
    logger.info(f"回測完成：{result.n_devices} 台設備、{result.n_points} 個量測點、"
               f"{len(result.episodes_df)} 個觸發事件")

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
        '基準期計算（尚無 vibcore 模組，暫用 validate/baseline_stub.py）': USING_REAL_BASELINE,
        '其餘規則（VEL_HIGH/IMPACT_RISE/趨勢/軸能量/事件類，暫用 validate/rules_stub.py）': False,
    }

    written = write_reports(result, rule_configs, args.out_dir, sweep_df, using_real)
    print('\n報告已產出：')
    for name, path in written.items():
        print(f"  {name:24s} {path}")
    print(f"\n摘要：{written.get('summary_txt')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
