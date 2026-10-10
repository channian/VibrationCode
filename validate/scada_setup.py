#!/usr/bin/env python3
"""
scada_setup.py — SCADA tag 對應表的產出與檢查

三個子命令：

    emit    產出 tag 對應表範本，把 Analytic CSV `Label` 欄已有的 tag 預填進去
    check   檢查填好的對應表：涵蓋幾台設備、缺哪些、型別是否合法
    probe   拿讀值檔實際驗一次：刷新週期多久、對齊之後有幾成小時有值

**為什麼先做這一層，而不是直接做工況分層**：分層怎麼切尚未定案，而那個
決定需要先回答「velRMS 到底有沒有隨轉速變化」——那是接上 SCADA 之後才
驗得了的事。把對應與載入獨立出來，讓分層的決定可以基於資料而不是反過來。

用法：

    python -m validate.scada_setup emit  --data-dir data/ --out out/tagmap.csv
    python -m validate.scada_setup check --tagmap out/tagmap.csv --data-dir data/
    python -m validate.scada_setup probe --tagmap out/tagmap.csv --readings out/scada.csv
    # --readings 也可以給放了多個 CSV 的資料夾，會自動合併
    python -m validate.scada_setup probe --tagmap out/tagmap.csv --readings scada_csv/

讀值檔的欄名接受別名，現場匯出的 `DATETIME / TAGNAME / VALUE` 可直接使用。
**時間一律視為廠內當地時間（naive）**，與振動端一致——這一點錯了會安靜地
差 8 小時，讓每個振動樣本配到錯的工況。
"""

from __future__ import annotations

import argparse
import glob
import logging
import os

import pandas as pd

from vibcore.io.analytic_reader import _ENCODINGS
from vibcore.io.scada import (ACTIVE_MIN_CHANGES, REFRESH_TOLERANCE,
                              SCADA_REFRESH_MINUTES, VARIABLE_TYPES,
                              describe_match, describe_speed, effective_sample_count,
                              emit_tagmap_template, estimate_refresh_minutes,
                              match_readings, parse_readings, parse_tagmap,
                              resolve_reading_paths,
                              profile_tag)

logger = logging.getLogger(__name__)

#: 只讀這兩欄——Analytic CSV 有 200~670 欄，全讀很慢且吃記憶體。
_WANTED = ('Name', 'Label')


def collect_labels(data_dir: str, pattern: str = '*.csv') -> dict[str, str | None]:
    """
    掃描 Analytic CSV，取出每台設備的 `Label` 欄（SCADA tag 名稱）。

    實測 `Label` 對同一台設備是固定值（`ZP 3-5_M1` 的 7 列都是同一個 tag），
    所以取眾數即可；完全空白的回 None，代表那台沒有現成的對應。
    """
    paths = sorted(glob.glob(os.path.join(data_dir, pattern)))
    if not paths:
        logger.error(f"{data_dir} 找不到符合 {pattern} 的檔案")
        return {}

    frames = []
    for path in paths:
        df = _read_min(path)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return {}

    all_df = pd.concat(frames, ignore_index=True)
    all_df['Name'] = all_df['Name'].astype(str).str.strip()

    out: dict[str, str | None] = {}
    for name, sub in all_df.groupby('Name', sort=True):
        if 'Label' not in sub.columns:
            out[name] = None
            continue
        labels = sub['Label'].dropna().astype(str).str.strip()
        labels = labels[labels != '']
        out[name] = labels.mode().iloc[0] if not labels.empty else None
    return out


def _read_min(path: str) -> pd.DataFrame | None:
    """只讀 Name/Label 兩欄；編碼與分隔符先探測。沿用 iso_readiness 的做法。"""
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
        if 'Name' not in cols:
            last_err = ValueError('沒有 Name 欄')
            continue
        wanted = [c for c in _WANTED if c in cols]
        try:
            return pd.read_csv(path, sep=sep, usecols=wanted, encoding=enc, dtype=str)
        except Exception as e:                        # noqa: BLE001 - 壞檔不該中斷整批
            last_err = e
            continue
    logger.warning(f"{os.path.basename(path)} 無法解析（{last_err}），略過")
    return None


def cmd_emit(args) -> int:
    labels = collect_labels(args.data_dir, args.pattern)
    if not labels:
        print('沒有讀到任何設備，無法產生範本。')
        return 1
    n = emit_tagmap_template(labels, args.out)
    have = sum(1 for v in labels.values() if v)

    print('=' * 66)
    print('  SCADA tag 對應表範本')
    print('=' * 66)
    print(f'\n已寫出 {n} 列（{len(labels)} 台設備 × 電流/頻率各一列）：{args.out}')
    print(f'\n其中 {have} / {len(labels)} 台的電流 tag 已由 Analytic CSV 的 '
          f'Label 欄預填。')
    if have:
        sample = next(v for v in labels.values() if v)
        print(f'  範例：{sample}')
        print('  ⚠ 預填的型別一律標成 current，那是從 tag 命名猜的（`_INV_I`），')
        print('    **請工程師確認確實是電流**——猜錯會讓整個工況分層用到錯的變數。')
    if have < len(labels):
        missing = [k for k, v in labels.items() if not v]
        print(f'\n沒有現成 tag 的 {len(missing)} 台需人工填：')
        print('  ' + '、'.join(missing[:12])
              + (f' …另有 {len(missing) - 12} 台' if len(missing) > 12 else ''))
    print('\n頻率 tag 一律要人工填——它不在 Analytic CSV 裡。')
    print(f'variable_type 只收 {VARIABLE_TYPES}，其餘值會被拒收並記警告。')
    print('\n填完之後：')
    print(f'  python -m validate.scada_setup check --tagmap {args.out} '
          f'--data-dir {args.data_dir}')
    print('\n' + '=' * 66)
    return 0


def cmd_check(args) -> int:
    mappings = parse_tagmap(args.tagmap)
    if not mappings:
        print(f'{args.tagmap} 沒有讀到任何有效的對應列。')
        return 1

    df = pd.DataFrame([{'device_id': m.device_id, 'variable_type': m.variable_type,
                        'tag_id': m.tag_id, 'is_active': m.is_active}
                       for m in mappings])

    print('=' * 66)
    print('  SCADA tag 對應表檢查')
    print('=' * 66)
    print(f'\n有效對應列：{len(df)}　涵蓋設備：{df["device_id"].nunique()} 台')
    print('\n依變數型別：')
    for vtype in VARIABLE_TYPES:
        sub = df[df['variable_type'] == vtype]
        if len(sub):
            print(f'  {vtype:10s} {len(sub):3d} 個 tag　{sub["device_id"].nunique():3d} 台設備')

    if args.data_dir:
        labels = collect_labels(args.data_dir, args.pattern)
        all_devices = set(labels)
        mapped = set(df['device_id'])
        unknown = mapped - all_devices
        uncovered = all_devices - mapped
        print(f'\n對照 Analytic CSV 的 {len(all_devices)} 台設備：')
        print(f'  已有對應：{len(mapped & all_devices)} 台')
        if uncovered:
            print(f'  ⚠ 尚無任何 tag：{len(uncovered)} 台')
            print('    ' + '、'.join(sorted(uncovered)[:12])
                  + (f' …另有 {len(uncovered) - 12} 台' if len(uncovered) > 12 else ''))
        if unknown:
            print(f'  ⚠ 對應表有、但 Analytic CSV 裡沒有：{len(unknown)} 台'
                  f'——通常是 device_id 拼錯')
            print('    ' + '、'.join(sorted(unknown)[:12]))

        # 只有電流沒有頻率（或反之）的設備：分層需要頻率，只有電流做不了
        by_dev = df.groupby('device_id')['variable_type'].apply(set)
        only_current = [d for d, s in by_dev.items() if s == {'current'}]
        if only_current:
            print(f'\n  只有電流、沒有頻率的設備：{len(only_current)} 台')
            print('    這些可以用來判斷「有沒有在運轉」，但做不了轉速分層。')

    print('\n' + '=' * 66)
    return 0


def cmd_probe(args) -> int:
    """拿實際讀值驗證：tag 對得上嗎、訊號有沒有在動、刷新週期是多少。"""
    mappings = parse_tagmap(args.tagmap)
    n_src = len(resolve_reading_paths(args.readings))
    readings = parse_readings(args.readings)
    if readings.empty:
        print('讀值檔沒有可用資料。')
        return 1
    if n_src > 1:
        print(f'讀值來源：{n_src} 個檔案，合併後 {len(readings):,} 筆')

    # 對應表的 tag 來自 Analytic CSV 的 Label 欄（`資料表名|tag名`），
    # 讀值檔的 TAGNAME 多半只有 tag 名那一半。比對會自動忽略前綴與大小寫，
    # 對不上時把兩邊的範例印出來，而不是只說「沒有資料」。
    readings, report = match_readings(readings, mappings)

    print('=' * 66)
    print('  SCADA 讀值實測')
    print('=' * 66)
    print()
    for line in describe_match(report):
        print('  ' + line if line else '')
    if not report.ok:
        print()
        return 1

    print(f'\n可用讀值 {len(readings):,} 筆，涵蓋 {readings["tag_id"].nunique()} 個 tag')
    print(f'期間 {readings["ts"].min()} ～ {readings["ts"].max()}')

    vtype_of = {m.tag_id: m.variable_type for m in mappings}
    profiles = [profile_tag(tag, sub) for tag, sub in readings.groupby('tag_id')]
    row_gap = (readings.sort_values(['tag_id', 'ts']).groupby('tag_id')['ts']
               .diff().dt.total_seconds().median() / 60.0)

    def _short(tag: str, width: int = 34) -> str:
        t = tag.split('|')[-1]
        return t if len(t) <= width else '…' + t[-(width - 1):]

    # ── 1. 訊號輪廓：這個 tag 到底有沒有在動 ───────────────────
    kinds = {'varying': '持續變動', 'rare': '很少變動（< %d 次）' % ACTIVE_MIN_CHANGES,
             'constant_zero': '全程恆為 0', 'constant_nonzero': '全程恆為同一個非零值'}
    print(f'\n檔案列間隔中位數 {row_gap:.1f} 分鐘（這是匯出的取樣密度，不是刷新週期）')
    print('\n訊號輪廓：')
    for k, label in kinds.items():
        group = [p for p in profiles if p.kind == k]
        if group:
            print(f'  {label:22s} {len(group):3d} 個 tag')

    # ── 2. 不怎麼動的 tag：要人判斷是關機、定頻還是凍結 ─────────
    flat = [p for p in profiles if p.kind != 'varying']
    if flat:
        print(f'\n不怎麼動的 tag（{len(flat)} 個）——這些要人判斷：')
        print(f'  {"tag":36s} {"型別":10s} {"狀況":18s} {"值（p5 / 中位 / p95）":>24s}')
        print('  ' + '-' * 92)
        for p in sorted(flat, key=lambda x: (x.kind, x.tag_id)):
            vt = vtype_of.get(p.tag_id, '?')
            if p.kind == 'constant_zero':
                state = '全程恆為 0'
            elif p.kind == 'constant_nonzero':
                state = '全程恆為非零'
            else:
                state = f'僅變動 {p.n_changes} 次'
            vals = f'{p.v_p05:.1f} / {p.v_median:.1f} / {p.v_p95:.1f}'
            print(f'  {_short(p.tag_id):36s} {vt:10s} {state:18s} {vals:>24s}')
        print('\n  怎麼讀：')
        print('    · 電流恆為 0 → 這段期間設備沒開機（備機的線索，Q4 第二階段會用到）')
        print('    · 頻率恆為非零 → 變頻器定頻運轉，**沒有轉速變化可供分層**')
        print('    · 電流恆為非零且完全不動 → 訊號可能凍結失效，要回頭查 tag 是否正確')

    # ── 3. 頻率 tag：能不能拿來分層，只看「運轉時」的變動 ───────
    freq = [p for p in profiles if vtype_of.get(p.tag_id) == 'frequency']
    if freq:
        rows = [(p, *describe_speed(p)) for p in sorted(freq, key=lambda x: x.tag_id)]
        n_ok = sum(1 for _, _, ok in rows if ok)
        print(f'\n頻率 tag 的轉速變動（{len(freq)} 個）——只看運轉時（> 0 Hz），'
              f'停機不算轉速變化：')
        print(f'  {"tag":34s} {"運轉佔比":>8s} {"p5":>6s} {"中位":>6s} {"p95":>6s}  說明')
        print('  ' + '-' * 90)
        for p, note, ok in rows:
            if p.run_median is None:
                cells = f'{"—":>6s} {"—":>6s} {"—":>6s}'
            else:
                cells = f'{p.run_p05:6.1f} {p.run_median:6.1f} {p.run_p95:6.1f}'
            mark = '★ ' if ok else '  '
            print(f'{mark}{_short(p.tag_id):34s} {p.run_share * 100:7.0f}% {cells}  {note}')
        print(f'\n  ★ = 運轉時轉速有足夠變動、值得進一步驗證：{n_ok} / {len(freq)} 個 tag')
        print('    （這是篩選。轉速變了、振動有沒有跟著變，要用 validate.speed_effect 驗。）')

    # ── 4. 刷新週期：只用變動節奏反映系統刷新的 tag 來估 ─────────
    est, n_used = estimate_refresh_minutes(profiles)
    print('\n' + '-' * 66)
    if est is None:
        print('沒有足夠頻繁變動的 tag，無法估計刷新週期。')
    else:
        rel = abs(est - SCADA_REFRESH_MINUTES) / SCADA_REFRESH_MINUTES
        print(f'實測刷新週期：{est:.1f} 分鐘'
              f'（由 {n_used} 個持續變動的 tag 的中位數得出）')
        print(f'程式假設：{SCADA_REFRESH_MINUTES:.0f} 分鐘　'
              f'差距 {rel * 100:.0f}%（容忍 {REFRESH_TOLERANCE * 100:.0f}%）')
        if rel > REFRESH_TOLERANCE:
            print(f'  ⚠ 差距超過容忍度。SCADA_REFRESH_MINUTES 應改成 {round(est):.0f}')
            print('    ——這個常數決定「有效獨立樣本數」怎麼算，訂錯會讓所有以它')
            print('    為分母的統計（標準差、分層後每層夠不夠樣本）跟著失真。')
        else:
            print('  ✓ 與程式假設相符，有效樣本數的換算成立。')
        span_min = (readings['ts'].max() - readings['ts'].min()).total_seconds() / 60.0
        print(f'  一個 {len(readings) // max(len(profiles), 1):,} 列的 tag，有效獨立樣本約 '
              f'{effective_sample_count(len(readings) // max(len(profiles), 1), span_min):,} 個。')

    print('\n' + '=' * 66)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='SCADA tag 對應表的產出與檢查')
    p.add_argument('--log-level', default='WARNING',
                   choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    sub = p.add_subparsers(dest='cmd', required=True)

    e = sub.add_parser('emit', help='產出 tag 對應表範本')
    e.add_argument('--data-dir', required=True)
    e.add_argument('--pattern', default='*.csv')
    e.add_argument('--out', required=True, help='輸出的 CSV 路徑')
    e.set_defaults(func=cmd_emit)

    c = sub.add_parser('check', help='檢查填好的對應表')
    c.add_argument('--tagmap', required=True)
    c.add_argument('--data-dir', default=None, help='一併對照 Analytic CSV 的設備清單')
    c.add_argument('--pattern', default='*.csv')
    c.set_defaults(func=cmd_check)

    r = sub.add_parser('probe', help='拿讀值檔實測刷新週期')
    r.add_argument('--tagmap', required=True)
    r.add_argument('--readings', required=True,
               help='SCADA 讀值：單一 CSV、放了多個 CSV 的資料夾（自動合併），或萬用字元'
                    '（如 scada/2026-*.csv）。欄名接受別名——現場的 DATETIME/TAGNAME/VALUE 可直接使用')
    r.set_defaults(func=cmd_probe)

    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format='%(levelname)s: %(message)s')
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
