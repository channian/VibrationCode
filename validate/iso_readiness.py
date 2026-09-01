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

from vibcore.io.analytic_reader import _ENCODINGS
from vibcore.metrics.iso import (ISO_FOUNDATIONS, ISO_GROUPS, ISO_THRESHOLDS,
                                 classify_zone, iso_alert_threshold)
from validate.points import _ISO_CODE_TO_GROUP, parse_iso_assumption

logger = logging.getLogger(__name__)

#: 只讀這幾欄。Analytic CSV 有 200~670 欄，全讀會很慢且吃記憶體。
_WANTED = ('Name', 'ISO10816_code', 'RPM', 'velRMS')


def _sniff(path: str) -> tuple[str, str, list[str]] | None:
    """
    找出這個檔案的編碼與分隔符，並回傳欄位清單。

    編碼必須逐一嘗試：現場的匯出檔常是 cp950（繁體 Windows 預設），
    直接用 UTF-8 讀會拋 UnicodeDecodeError。沿用
    `vibcore.io.analytic_reader` 的 `_ENCODINGS` 順序，讓這支工具與
    正式管線對同一批檔案的判讀一致。

    分隔符用「首行的 tab 與逗號孰多」判斷，而不是交給 pandas 的
    `sep=None` 嗅探——後者需要 python engine，在 200~670 欄的檔案上
    明顯較慢，而這裡只是要挑四個欄位。
    """
    last_err = None
    for enc in _ENCODINGS:
        try:
            with open(path, 'r', encoding=enc) as f:
                header = f.readline()
        except (UnicodeDecodeError, OSError) as e:
            last_err = e
            continue
        sep = '\t' if header.count('\t') >= header.count(',') else ','
        cols = [c.strip() for c in header.rstrip('\n\r').split(sep)]
        if len(cols) >= 5:
            return enc, sep, cols
        last_err = ValueError(f"以 {enc} 讀出的首行只切出 {len(cols)} 欄")
    logger.warning(f"{os.path.basename(path)} 無法解析（{last_err}），略過")
    return None


def _read_min(path: str) -> pd.DataFrame | None:
    """只讀需要的四個欄位；編碼與分隔符先探測過再讀。"""
    sniffed = _sniff(path)
    if sniffed is None:
        return None
    enc, sep, cols = sniffed

    wanted = [c for c in _WANTED if c in cols]
    missing = [c for c in ('Name', 'velRMS') if c not in cols]
    if missing:
        logger.warning(f"{os.path.basename(path)} 缺少必要欄位 {missing}，略過"
                       f"（實際欄位共 {len(cols)} 個，前幾個：{cols[:6]}）")
        return None
    try:
        return pd.read_csv(path, sep=sep, usecols=wanted, encoding=enc)
    except Exception as e:
        logger.warning(f"{os.path.basename(path)} 讀取失敗（{type(e).__name__}: {e}），略過")
        return None


def collect(data_dir: str, pattern: str = '*.csv',
            assume: tuple[str, str] | None = None) -> pd.DataFrame:
    """
    掃描資料夾，彙整每台設備的 ISO 分類與 velRMS 水準。

    `assume` 為 `(群組, 基礎剛性)`；不給則所有設備都算未分類，
    這是預設也是唯一誠實的預設值——前端資料沒有基礎剛性欄位。
    """
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

        # 分類一律來自明確給定的假設，不從 ISO10816_code 猜——該欄位語意
        # 未經確認，且就算確認了也還缺基礎剛性（見 points._ISO_CODE_TO_GROUP）。
        # 有填 code 的設備才套用假設，好讓「台帳有填」與「空白」仍分得開。
        iso_key = assume if (assume is not None and code in _ISO_CODE_TO_GROUP) else None
        machine_class = '/'.join(iso_key) if iso_key else None
        # 以運轉中 velRMS 的中位數當基準的替代值。真正的基準期由
        # detect_baseline 掃描最穩定窗口而得，這裡只需要一個量級參考。
        baseline_proxy = float(vel_run.median()) if not vel_run.empty else None
        threshold = (iso_alert_threshold(baseline_proxy, iso_key)
                     if iso_key and baseline_proxy is not None else None)
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
            # 兩個 Zone 分開看：中位數代表「這台平常在哪一區運轉」，
            # p95 代表「偶爾會衝到哪一區」。ISO_ZONE 規則預設在 Zone C
            # 才告警，所以長期待在 Zone B 的設備不會有任何 Finding——
            # 那不是異常，但也不是 Zone A，值得在報告裡讓人看見。
            'zone_median': classify_zone(baseline_proxy, iso_key),
            'zone_p95': classify_zone(vel_p95, iso_key),
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
    print(f'  套用到 ISO 分類：{len(classified)} 台')
    print(f'  未分類（ISO10816_code 為 0/空白，或未給 --assume-iso）：{len(unset)} 台')

    if len(classified):
        dist = classified['machine_class'].value_counts().sort_index()
        print('\n  分類分佈：' + '、'.join(
            f"{ISO_THRESHOLDS[tuple(k.split('/'))]['label']} {v} 台" for k, v in dist.items()))

    if len(unset):
        print(f'\n⚠ 未分類的 {len(unset)} 台設備：')
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
            print('  處置：ISO 10816-3 的 Zone 判定需要「機器群組」與「基礎剛性」兩項，')
            print('        前端資料兩項都沒有。請補台帳，或用 --assume-iso 做敏感度分析。')
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
        print(f'  {"設備":16s} {"分類":12s} {"中位":>7s} {"p95":>7s} {"門檻":>7s} {"headroom":>9s}')
        for r in top.itertuples():
            print(f'  {r.device_id:16s} {str(r.machine_class):12s} '
                  f'{r.vel_rms_median:7.3f} {r.vel_rms_p95:7.3f} '
                  f'{r.iso_alert_threshold:7.3f} {r.headroom_ratio:9.2f}')

        # ── 全廠 Zone 分佈 ──────────────────────────────────────
        zc = ready['zone_median'].value_counts()
        print('\n  平常運轉所在的 ISO Zone（以運轉中 velRMS 中位數判定）：')
        for z in ('A', 'B', 'C', 'D'):
            n_z = int(zc.get(z, 0))
            if n_z:
                print(f'    Zone {z}：{n_z} 台')
        in_b_plus = ready[ready['zone_median'].isin(['B', 'C', 'D'])]
        if not in_b_plus.empty:
            print(f'\n  ⚠ {len(in_b_plus)} 台的**正常運轉水準**已不在 Zone A：')
            for r in in_b_plus.sort_values('vel_rms_median', ascending=False).head(10).itertuples():
                print(f'    {r.device_id:16s} {r.machine_class:12s} 中位 {r.vel_rms_median:.3f} mm/s'
                      f'　Zone {r.zone_median}')
            print('    ISO 對 Zone B 的定義是「可長期不受限運轉」，因此 ISO_ZONE')
            print('    （預設 Zone C 才告警）不會為這些設備開單——這是設計如此。')
            print('    但它們的劣化餘裕比 Zone A 的設備小，趨勢類判定要優先看。')

        if len(over) == 0:
            print('\n  ★ 沒有任何已分級設備接近 ISO 告警門檻。')
            print('    這**不是**系統故障，而是「照 ISO 標準看這些設備都健康」。')
            print('    回測裡 VEL_HIGH 的觸發若仍有件數，來源會是未分級設備的')
            print('    sigma_fallback 路徑，不是 ISO 判定。')
            print('    此時本系統的價值主要在趨勢與觀察名單，而非絕對位準告警。')

    print('\n' + '=' * 66)


def compare(data_dir: str, pattern: str, assumptions: list[tuple[str, str]]) -> pd.DataFrame:
    """
    在多個分類假設下各跑一次，輸出對照表。

    **這是給專家會議用的。** 「這些泵到底算 Group 幾、基礎算剛性還柔性」
    是一個抽象的分類學問題，專家不見得能立刻決定；但「你選這個，正常運轉
    就已經不在 Zone A 的設備有 N 台、已越過告警門檻的有 M 台」是具體後果，
    看著數字一句話就能決定。沒有這張表，同樣的討論可能繞很久還沒共識。
    """
    rows = []
    for assume in assumptions:
        df = collect(data_dir, pattern, assume=assume)
        if df.empty:
            continue
        key = tuple(assume)
        th = ISO_THRESHOLDS[key]
        ready = df[df['iso_alert_threshold'].notna()]
        over = ready[ready['vel_rms_p95'] >= ready['iso_alert_threshold']]
        zone_med = ready['zone_median'].value_counts()
        rows.append({
            '假設': '/'.join(assume),
            'A/B 界': th['ab'],
            'B/C 界': th['bc'],
            '告警門檻中位': (round(float(ready['iso_alert_threshold'].median()), 3)
                             if not ready.empty else None),
            '可評估台數': len(ready),
            '平常在 Zone A': int(zone_med.get('A', 0)),
            '平常在 Zone B': int(zone_med.get('B', 0)),
            '平常在 Zone C+': int(zone_med.get('C', 0)) + int(zone_med.get('D', 0)),
            'p95 已越門檻': len(over),
        })
    return pd.DataFrame(rows)


def report_compare(df: pd.DataFrame) -> None:
    if df.empty:
        print('沒有可比較的結果。')
        return
    print('=' * 92)
    print('  ISO 分類假設敏感度對照')
    print('=' * 92)
    print('\n同一批資料，在不同的「機器群組 / 基礎剛性」假設下的判定結果：\n')
    cols = list(df.columns)
    widths = {c: max(len(c), *(len(str(v)) for v in df[c])) + 2 for c in cols}
    print('  ' + ''.join(f'{c:<{widths[c]}}' for c in cols))
    print('  ' + ''.join('-' * widths[c] for c in cols))
    for r in df.itertuples(index=False):
        print('  ' + ''.join(f'{str(v):<{widths[c]}}' for c, v in zip(cols, r)))
    print('\n判讀方式：')
    print('  · 「平常在 Zone B」是關鍵欄位——ISO_ZONE 預設 Zone C 才告警，')
    print('    所以這些設備長期不會產生任何 Finding。數字在不同假設下差很多，')
    print('    代表「它們是否長期在 Zone B」這個結論完全取決於分類怎麼填。')
    print('  · 「p95 已越門檻」約略對應 VEL_HIGH 的觸發量級（實際觸發還要看')
    print('    基準期與持續性條件，這裡只用 p95 當快速代理）。')
    print('  · 兩欄都大幅變動，就代表分類必須先確認，否則所有 ISO 相關結論都是浮的。')


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='檢查 ISO 機械等級填寫狀況與告警門檻餘裕')
    p.add_argument('--data-dir', required=True, help='存放 Analytic CSV 的資料夾')
    p.add_argument('--pattern', default='*.csv', help='檔名比對樣式（預設 *.csv）')
    p.add_argument('--csv', default=None, help='另存一份明細 CSV')
    p.add_argument('--assume-iso', default=None, metavar='GROUP/FOUNDATION',
                   help='假設全部已填 ISO10816_code 的設備為此分類，例如 3/rigid。'
                        '不給則所有設備視為未分類（前端沒有基礎剛性欄位）')
    p.add_argument('--compare', action='store_true',
                   help='在所有 8 種分類組合下各跑一次並輸出對照表，供專家會議決策')
    p.add_argument('--log-level', default='WARNING',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format='%(levelname)s: %(message)s')

    if args.compare:
        assumptions = [(g, f) for g in ISO_GROUPS for f in ISO_FOUNDATIONS]
        cmp_df = compare(args.data_dir, args.pattern, assumptions)
        report_compare(cmp_df)
        if args.csv and not cmp_df.empty:
            os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or '.', exist_ok=True)
            cmp_df.to_csv(args.csv, index=False, encoding='utf-8-sig')
            print(f'\n對照表已寫入：{args.csv}')
        return 0

    try:
        assume = parse_iso_assumption(args.assume_iso)
    except ValueError as e:
        print(f'參數錯誤：{e}')
        return 2

    df = collect(args.data_dir, args.pattern, assume=assume)
    if assume is not None:
        print(f'（分類假設：{"/".join(assume)}——這是假設值，不是台帳實際資料）\n')
    report(df)

    if args.csv and not df.empty:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or '.', exist_ok=True)
        df.to_csv(args.csv, index=False, encoding='utf-8-sig')
        print(f'明細已寫入：{args.csv}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
