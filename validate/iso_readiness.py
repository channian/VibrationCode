#!/usr/bin/env python3
"""
iso_readiness.py — ISO 判定就緒度檢查

回答兩個問題：

1. **有多少台設備填了機械等級（`ISO10816_code`）？**
   沒填的設備，`ISO_ZONE` 不會做 Zone 判定，`VEL_HIGH` 的 ISO 模式也會
   退回相對基準（sigma_fallback）。若多數設備未分級，回測裡看到的
   `VEL_HIGH` 觸發其實幾乎都來自 sigma 模式，不是 ISO 判定。

2. **已分級的設備，離 ISO 告警門檻還有多遠？**
   門檻 = 基準 + 0.25 × Zone B 上限（封頂 1.25 × Zone B 上限）。
   若全部設備的實測 velRMS 都遠低於門檻，那 ISO 模式本來就不會觸發——
   這不是系統壞掉，而是「照 ISO 看這些設備都健康」的正確結論。分辨這
   兩件事很重要：前者要補台帳，後者要重新想這套系統的價值放在哪。

用法：

    python -m validate.iso_readiness --data-dir data/
    python -m validate.iso_readiness --data-dir data/ --csv out/iso_readiness.csv

只讀取必要欄位，不做聚合，大檔也能快速跑完。
"""

from __future__ import annotations

import argparse
import glob
import logging
import os

import pandas as pd

from vibcore.metrics.iso import ISO_THRESHOLDS, iso_alert_threshold
from validate.points import _ISO_CODE_MAP

logger = logging.getLogger(__name__)

#: 只讀這幾欄。Analytic CSV 有 200~670 欄，全讀會很慢且吃記憶體。
_WANTED = ('Name', 'ISO10816_code', 'RPM', 'velRMS')


def _read_min(path: str) -> pd.DataFrame | None:
    """只讀需要的欄位；分隔符自動判斷（實測檔案為 tab 分隔）。"""
    for sep in ('\t', ','):
        try:
            head = pd.read_csv(path, sep=sep, nrows=0)
        except Exception:
            continue
        if len(head.columns) < 5:
            continue
        cols = [c for c in _WANTED if c in head.columns]
        if 'Name' not in cols or 'velRMS' not in cols:
            logger.warning(f"{os.path.basename(path)} 缺少 Name 或 velRMS 欄，略過")
            return None
        return pd.read_csv(path, sep=sep, usecols=cols)
    logger.warning(f"{os.path.basename(path)} 無法解析，略過")
    return None


def collect(data_dir: str, pattern: str = '*.csv') -> pd.DataFrame:
    """掃描資料夾，彙整每台設備的機械等級與 velRMS 水準。"""
    paths = sorted(glob.glob(os.path.join(data_dir, pattern)))
    if not paths:
        logger.error(f"{data_dir} 找不到符合 {pattern} 的檔案")
        return pd.DataFrame()

    frames = []
    for p in paths:
        df = _read_min(p)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()

    all_df = pd.concat(frames, ignore_index=True)
    all_df['Name'] = all_df['Name'].astype(str).str.strip()

    rows = []
    for name, sub in all_df.groupby('Name', sort=True):
        vel = pd.to_numeric(sub['velRMS'], errors='coerce').dropna()
        # 只取運轉中的樣本估水準——停機時的 velRMS 接近 0，會把中位數拉垮，
        # 讓每台設備看起來都離門檻很遠（判準與 aggregate.mark_running 一致）。
        vel_run = vel[vel > 0.1]

        code = None
        if 'ISO10816_code' in sub.columns:
            codes = pd.to_numeric(sub['ISO10816_code'], errors='coerce').dropna()
            codes = codes[codes > 0]
            if not codes.empty:
                code = int(codes.mode().iloc[0])

        machine_class = _ISO_CODE_MAP.get(code) if code else None
        # 以運轉中 velRMS 的中位數當基準的替代值。真正的基準期由
        # detect_baseline 掃描最穩定窗口而得，這裡只需要一個量級參考。
        baseline_proxy = float(vel_run.median()) if not vel_run.empty else None
        threshold = (iso_alert_threshold(baseline_proxy, machine_class)
                     if machine_class and baseline_proxy is not None else None)
        vel_p95 = float(vel_run.quantile(0.95)) if not vel_run.empty else None

        rows.append({
            'device_id': name,
            'iso_code': code,
            'machine_class': machine_class,
            'n_rows': len(sub),
            'n_running': len(vel_run),
            'vel_rms_median': round(baseline_proxy, 3) if baseline_proxy is not None else None,
            'vel_rms_p95': round(vel_p95, 3) if vel_p95 is not None else None,
            'vel_rms_max': round(float(vel_run.max()), 3) if not vel_run.empty else None,
            'iso_alert_threshold': round(threshold, 3) if threshold is not None else None,
            'headroom_ratio': (round(vel_p95 / threshold, 2)
                               if threshold and vel_p95 is not None and threshold > 0 else None),
        })
    return pd.DataFrame(rows)


def report(df: pd.DataFrame) -> None:
    if df.empty:
        print('沒有讀到任何設備資料。')
        return

    n = len(df)
    classified = df[df['machine_class'].notna()]
    unset = df[df['machine_class'].isna()]

    print('=' * 66)
    print('  ISO 判定就緒度檢查')
    print('=' * 66)
    print(f'\n設備數：{n}')
    print(f'  已填機械等級：{len(classified)} 台')
    print(f'  未填（ISO10816_code 為 0 或空白）：{len(unset)} 台')

    if len(classified):
        dist = classified['machine_class'].value_counts().sort_index()
        print('\n  等級分佈：' + '、'.join(
            f"{ISO_THRESHOLDS[k]['label']} {v} 台" for k, v in dist.items()))

    if len(unset):
        print(f'\n⚠ 未分級的 {len(unset)} 台設備：')
        print('    · ISO_ZONE 不做 Zone 判定（這正是它可能觸發 0 次的原因）')
        print('    · VEL_HIGH 的 ISO 模式退回相對基準（evidence 標記 sigma_fallback）')
        print('  ' + '、'.join(unset['device_id'].head(15).tolist())
              + (f' …另有 {len(unset) - 15} 台' if len(unset) > 15 else ''))

    # ── 核心問題：已分級的設備有沒有可能觸發 ──────────────────
    ready = classified[classified['iso_alert_threshold'].notna()].copy()
    if ready.empty:
        # 兩種成因的處置完全不同，不可混為一談：沒分級要補台帳，
        # 有分級但沒運轉資料要查設備是否真的停機或感測器斷線。
        if classified.empty:
            print('\n沒有任何已分級的設備，因此無法計算 ISO 告警門檻。')
            print('  處置：請工程師補填 ISO10816_code（1=Class I…4=Class IV）。')
        else:
            print(f'\n已分級的 {len(classified)} 台都沒有運轉中的 velRMS 資料'
                  '（velRMS > 0.1 mm/s），無法計算門檻。')
            print('  處置：確認這些設備是否整段期間都停機，或感測器是否斷線。')
    else:
        over = ready[ready['vel_rms_p95'] >= ready['iso_alert_threshold']]
        print(f'\n已分級且可算門檻的 {len(ready)} 台中：')
        print(f'  p95 已達或超過 ISO 告警門檻：{len(over)} 台')
        print(f'  尚未達到：{len(ready) - len(over)} 台')

        print('\n  離門檻最近的前 10 台（headroom = p95 ÷ 門檻，≥1 代表已越線）：')
        top = ready.sort_values('headroom_ratio', ascending=False).head(10)
        print(f'  {"設備":16s} {"等級":5s} {"中位":>7s} {"p95":>7s} {"門檻":>7s} {"headroom":>9s}')
        for r in top.itertuples():
            print(f'  {r.device_id:16s} {str(r.machine_class):5s} '
                  f'{r.vel_rms_median:7.3f} {r.vel_rms_p95:7.3f} '
                  f'{r.iso_alert_threshold:7.3f} {r.headroom_ratio:9.2f}')

        if len(over) == 0:
            print('\n  ★ 沒有任何已分級設備接近 ISO 告警門檻。')
            print('    這**不是**系統故障，而是「照 ISO 標準看這些設備都健康」。')
            print('    回測裡 VEL_HIGH 的觸發若仍有件數，來源會是未分級設備的')
            print('    sigma_fallback 路徑，不是 ISO 判定。')
            print('    此時本系統的價值主要在趨勢與觀察名單，而非絕對位準告警。')

    print('\n' + '=' * 66)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='檢查 ISO 機械等級填寫狀況與告警門檻餘裕')
    p.add_argument('--data-dir', required=True, help='存放 Analytic CSV 的資料夾')
    p.add_argument('--pattern', default='*.csv', help='檔名比對樣式（預設 *.csv）')
    p.add_argument('--csv', default=None, help='另存一份明細 CSV')
    p.add_argument('--log-level', default='WARNING',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format='%(levelname)s: %(message)s')

    df = collect(args.data_dir, args.pattern)
    report(df)

    if args.csv and not df.empty:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or '.', exist_ok=True)
        df.to_csv(args.csv, index=False, encoding='utf-8-sig')
        print(f'明細已寫入：{args.csv}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
