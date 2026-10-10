#!/usr/bin/env python3
"""
speed_effect.py — 轉速變了，振動速度（velRMS）有沒有跟著變？

**這支工具回答的問題**：要不要依工況（轉速）對基準線分層。

分層的代價不小——每多切一層，每層的樣本就少一份、基準線更不穩、
規則參數要重新校準。只有「轉速真的會把 velRMS 推到足以單靠轉速就觸發
警報」的設備才值得付這個代價。所以先量，再決定，而不是反過來。

做法：把每台設備的**每小時 velRMS 中位數**（只取運轉中的樣本）與同一小時的
**SCADA 頻率**配對，比較「頻率低的時段」與「頻率高的時段」的 velRMS 差多少，
並以設備自己的小時間散佈（σ_within）當尺度。

    shift_σ = (高頻時段 velRMS 中位數 − 低頻時段 velRMS 中位數) / σ_within

用法：

    python -m validate.speed_effect --data-dir data/ \\
        --tagmap out/tagmap.csv --readings out/scada.csv \\
        --since 2026-06-01 --until 2026-06-14 --csv out/speed_effect.csv

**解讀前請先讀這幾點**：

1. **這是相關，不是因果。** 頻率與 velRMS 若同時隨季節、負載緩慢上升，
   會得到很漂亮的相關，但與轉速效應無關。因此另外報「同一天之內」的
   相關（`rho_day`）：把每個小時減去當天的中位數，只留日內起伏。
   `rho` 很高但 `rho_day` 接近 0，代表相關是跨日趨勢造成的（`trend?` 欄標 ⚠）。
2. **只量得到資料涵蓋的轉速範圍。** 一段時間內轉速都在 48–50 Hz，
   就推不出 40 Hz 的行為。`freq_span%` 欄顯示實際涵蓋的範圍。
3. **單一時段、單一季節的結論不外推。** 6 月的結果不代表 8 月。
4. **同一台機器的 M1/M2 共用同一個頻率 tag**，兩者各自成一列，不是兩份獨立證據。

判定（`verdict`）：

    insufficient   配對到的小時數不足（< 48），不下結論
    no_variation   運轉時轉速幾乎沒變（涵蓋 < 5%）或高低兩組樣本太少，無從比較
    material       |shift_σ| ≥ 2.5，轉速單獨就足以把 velRMS 推過規則常用的 2.5σ 門檻
    moderate       1 ≤ |shift_σ| < 2.5
    minor          |shift_σ| < 1，相對於日常波動可忽略
    confounded     高低頻差距夠大，但相關只存在於跨日、同一天之內沒有——可能是
                   季節／負載／劣化的緩慢趨勢，與轉速無關。**不計入 material**，
                   否則會灌水「值得分層」的設備數

2.5σ 是「單靠轉速就可能觸發警報」的**粗略**標準，取自規則常用的門檻，
並非嚴格推導——σ_within 是小時層級的散佈，各規則的 σ 定義不盡相同。
"""

from __future__ import annotations

import argparse
import glob
import logging
import os

import numpy as np
import pandas as pd

from validate.iso_readiness import _sniff
from vibcore.io.analytic_reader import parse_datetime
from vibcore.io.scada import (SPEED_VARIATION_MIN_PCT, match_readings,
                              parse_readings, parse_tagmap)

logger = logging.getLogger(__name__)

#: 與 aggregate.mark_running 的預設一致：velRMS 高於此值才算運轉中。
VEL_RUNNING_THRESHOLD = 0.1

#: 一個小時內至少要有幾筆運轉中的振動樣本，該小時才算數。
MIN_VIB_ROWS_PER_HOUR = 3

#: 一個小時內頻率的 (max−min)/中位數 超過此比例，視為那個小時轉速在變，
#: 小時中位數不能代表任何一個實際轉速，整小時剔除。
MAX_INHOUR_FREQ_RANGE = 0.10

MIN_PAIRED_HOURS = 48
MIN_GROUP_HOURS = 12
SIGMA_MATERIAL = 2.5
SIGMA_MODERATE = 1.0
MIN_HOURS_PER_DAY_FOR_WITHIN = 6

#: velRMS 的 σ_within 下限，避免分母趨近 0 時 shift_σ 爆炸（取整體中位數的 1%）。
SIGMA_FLOOR_FRAC = 0.01

#: `rho` 夠大、但日內相關幾乎消失 → 疑似跨日趨勢造成。
TREND_RHO_MIN = 0.3
TREND_RHO_DAY_MAX = 0.15


# ── 載入 ──────────────────────────────────────────────

def _time_col(cols: list[str]) -> str | None:
    for c in cols:
        if c.strip().lower() in ('time', 'datetime', 'timestamp', 'date'):
            return c
    for c in cols:
        if 'time' in c.lower() or 'date' in c.lower():
            return c
    return None


def hourly_vibration(path: str) -> pd.DataFrame:
    """
    讀一個 Analytic CSV，直接壓成每小時 velRMS 中位數。

    只讀 `Name / 時間欄 / velRMS` 三欄（整份動輒 200~670 欄、每秒一列，
    全讀不切實際），並且在檔案層級就壓成每小時，不把逐秒資料累積進記憶體。
    只取運轉中（velRMS > 門檻）的樣本——停機時的 velRMS 接近 0，混進來
    會讓「低轉速」其實只是「停機」。

    Returns:
        欄位 `device_id, ts_hour, vel, n_rows`；讀不了回空表。
    """
    empty = pd.DataFrame(columns=['device_id', 'ts_hour', 'vel', 'n_rows'])
    sniffed = _sniff(path)
    if sniffed is None:
        return empty
    enc, sep, cols = sniffed
    tcol = _time_col(cols)
    if tcol is None or 'Name' not in cols or 'velRMS' not in cols:
        logger.warning(f"{os.path.basename(path)} 缺少 Name / 時間 / velRMS 欄，略過")
        return empty
    try:
        df = pd.read_csv(path, sep=sep, usecols=[tcol, 'Name', 'velRMS'], encoding=enc)
    except Exception as e:
        logger.warning(f"{os.path.basename(path)} 讀取失敗（{type(e).__name__}: {e}），略過")
        return empty

    df['ts'] = parse_datetime(df[tcol], os.path.basename(path))
    df['vel'] = pd.to_numeric(df['velRMS'], errors='coerce')
    df['device_id'] = df['Name'].astype(str).str.strip()
    df = df.dropna(subset=['ts', 'vel'])
    df = df[df['vel'] > VEL_RUNNING_THRESHOLD]
    if df.empty:
        return empty

    df['ts_hour'] = df['ts'].dt.floor('h')
    out = (df.groupby(['device_id', 'ts_hour'])['vel']
           .agg(vel='median', n_rows='size').reset_index())
    return out[out['n_rows'] >= MIN_VIB_ROWS_PER_HOUR].reset_index(drop=True)


def load_vibration(data_dir: str, pattern: str = '*.csv',
                   since: str | None = None, until: str | None = None) -> pd.DataFrame:
    paths = sorted(glob.glob(os.path.join(data_dir, pattern)))
    if not paths:
        logger.error(f"{data_dir} 找不到符合 {pattern} 的檔案")
        return pd.DataFrame(columns=['device_id', 'ts_hour', 'vel', 'n_rows'])
    frames = [f for f in (hourly_vibration(p) for p in paths) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=['device_id', 'ts_hour', 'vel', 'n_rows'])
    v = pd.concat(frames, ignore_index=True)
    # 同一台設備可能分在多個檔案、小時邊界重疊；合併時以樣本數加權取中位數
    # 不嚴謹，這裡取樣本數較多的那筆即可（重疊只發生在檔案交界的單一小時）。
    v = (v.sort_values('n_rows').drop_duplicates(['device_id', 'ts_hour'], keep='last')
         .sort_values(['device_id', 'ts_hour']).reset_index(drop=True))
    if since:
        v = v[v['ts_hour'] >= pd.Timestamp(since)]
    if until:
        v = v[v['ts_hour'] < pd.Timestamp(until) + pd.Timedelta(days=1)]
    return v.reset_index(drop=True)


def hourly_frequency(readings: pd.DataFrame) -> pd.DataFrame:
    """
    SCADA 頻率讀值壓成每小時：中位數與小時內的 (max−min)/中位數。

    **嚴格同小時，不沿用前一小時的值。** 工況分層可以容忍短暫缺值（標成未知），
    但這裡是在量關係——拿過期的頻率去配當下的振動，會把關係量糊，而且不會有
    任何錯誤訊息。寧可少幾個小時的樣本。
    """
    if readings.empty:
        return pd.DataFrame(columns=['tag_id', 'ts_hour', 'freq', 'inhour_range'])
    r = readings.assign(ts_hour=readings['ts'].dt.floor('h'))
    g = r.groupby(['tag_id', 'ts_hour'])['value']
    out = g.agg(freq='median', vmin='min', vmax='max').reset_index()
    out['inhour_range'] = np.where(out['freq'] > 0,
                                   (out['vmax'] - out['vmin']) / out['freq'], np.nan)
    return out[['tag_id', 'ts_hour', 'freq', 'inhour_range']]


# ── 分析 ──────────────────────────────────────────────

def _robust_sigma(x: np.ndarray) -> float:
    if len(x) == 0:
        return float('nan')
    return float(np.median(np.abs(x - np.median(x))) * 1.4826)


def _spearman(a: pd.Series, b: pd.Series) -> float:
    if len(a) < 3 or a.nunique() < 2 or b.nunique() < 2:
        return float('nan')
    return float(a.corr(b, method='spearman'))


def analyze_pair(paired: pd.DataFrame) -> dict:
    """
    單一 (設備, 頻率 tag) 的配對資料 → 統計量與判定。

    Args:
        paired: 欄位 `ts_hour, vel, freq`；已是運轉中、頻率 > 0 的小時。
    """
    n = len(paired)
    res: dict = {'n_hours': n, 'verdict': 'insufficient'}
    if n < MIN_PAIRED_HOURS:
        return res

    f, v = paired['freq'], paired['vel']
    p05, p50, p95 = np.percentile(f, [5, 50, 95])
    span_pct = (p95 - p05) / p50 * 100.0 if p50 else 0.0
    res.update(freq_p05=float(p05), freq_median=float(p50), freq_p95=float(p95),
               freq_span_pct=float(span_pct), vel_median=float(v.median()))

    if span_pct < SPEED_VARIATION_MIN_PCT:
        res['verdict'] = 'no_variation'
        return res

    q25, q75 = np.percentile(f, [25, 75])
    if q25 == q75:        # 設定值離散、中間一大塊同一個值：取兩側
        low, high = v[f < q25], v[f > q75]
    else:
        low, high = v[f <= q25], v[f >= q75]
    res.update(n_low=int(len(low)), n_high=int(len(high)))
    if len(low) < MIN_GROUP_HOURS or len(high) < MIN_GROUP_HOURS:
        res['verdict'] = 'no_variation'
        return res

    vel_low, vel_high = float(low.median()), float(high.median())
    resid = np.r_[low.to_numpy() - vel_low, high.to_numpy() - vel_high]
    sigma = max(_robust_sigma(resid), SIGMA_FLOOR_FRAC * float(v.median()))
    shift = (vel_high - vel_low) / sigma
    res.update(vel_low=vel_low, vel_high=vel_high, sigma_within=float(sigma),
               shift_sigma=float(shift),
               shift_pct=(vel_high - vel_low) / vel_low * 100.0 if vel_low else float('nan'))

    res['rho'] = _spearman(f, v)

    # 日內相關：每小時減去當天中位數，只留一天之內的起伏。
    d = paired.assign(day=paired['ts_hour'].dt.floor('D'))
    d = d[d.groupby('day')['vel'].transform('size') >= MIN_HOURS_PER_DAY_FOR_WITHIN]
    if len(d) >= MIN_PAIRED_HOURS // 2:
        fd = d['freq'] - d.groupby('day')['freq'].transform('median')
        vd = d['vel'] - d.groupby('day')['vel'].transform('median')
        res['rho_day'] = _spearman(fd, vd)
    else:
        res['rho_day'] = float('nan')

    rho, rho_day = res['rho'], res['rho_day']
    res['trend_suspect'] = bool(
        pd.notna(rho) and pd.notna(rho_day)
        and abs(rho) >= TREND_RHO_MIN and abs(rho_day) < TREND_RHO_DAY_MAX)

    a = abs(shift)
    verdict = ('material' if a >= SIGMA_MATERIAL
               else 'moderate' if a >= SIGMA_MODERATE else 'minor')
    # 有差距、但只有跨日相關：無法分辨是轉速還是趨勢。不能算成 material。
    if res['trend_suspect'] and verdict in ('material', 'moderate'):
        verdict = 'confounded'
    res['verdict'] = verdict
    return res


def run(data_dir: str, tagmap_path: str, readings_path: str, pattern: str = '*.csv',
        since: str | None = None, until: str | None = None) -> tuple[pd.DataFrame, dict]:
    """
    Returns:
        `(結果表, 對應資訊)`。對應資訊含兩邊對不上的設備清單，呼叫端要印出來
        ——設備名稱對不上時結果表會是空的，必須講得出是「沒對上」而不是「沒效應」。
    """
    mappings = [m for m in parse_tagmap(tagmap_path)
                if m.variable_type == 'frequency' and m.is_active]
    readings = parse_readings(readings_path)
    readings, _ = match_readings(readings, mappings)
    freq_h = hourly_frequency(readings)
    if since:
        freq_h = freq_h[freq_h['ts_hour'] >= pd.Timestamp(since)]
    if until:
        freq_h = freq_h[freq_h['ts_hour'] < pd.Timestamp(until) + pd.Timedelta(days=1)]

    vib = load_vibration(data_dir, pattern, since, until)

    vib_devices = set(vib['device_id'].unique()) if not vib.empty else set()
    map_devices = {m.device_id for m in mappings}
    info = {
        'n_vib_devices': len(vib_devices),
        'n_map_devices': len(map_devices),
        'map_only': sorted(map_devices - vib_devices),
        'vib_only': sorted(vib_devices - map_devices),
        'n_freq_tags': len({m.tag_id for m in mappings}),
    }

    rows = []
    # 對應表記錄的是 mapping 側的 tag_id；match_readings 已把讀值改寫成同一種寫法
    for m in sorted(mappings, key=lambda x: (x.device_id, x.tag_id)):
        if m.device_id not in vib_devices:
            continue
        fh = freq_h[freq_h['tag_id'] == m.tag_id]
        fh = fh[(fh['freq'] > 0) & ~(fh['inhour_range'] > MAX_INHOUR_FREQ_RANGE)]
        vh = vib[vib['device_id'] == m.device_id]
        paired = vh.merge(fh, on='ts_hour', how='inner')[['ts_hour', 'vel', 'freq']]
        res = analyze_pair(paired)
        res.update(device_id=m.device_id, tag_id=m.tag_id, n_vib_hours=len(vh))
        rows.append(res)

    return pd.DataFrame(rows), info


# ── 輸出 ──────────────────────────────────────────────

_VERDICT_ORDER = ['material', 'moderate', 'minor', 'confounded', 'no_variation', 'insufficient']
_VERDICT_NOTE = {
    'material': '轉速單獨就足以把 velRMS 推過 2.5σ（分層才有明確收益）',
    'moderate': '有影響但不大（1–2.5σ）',
    'minor': '相對日常波動可忽略（< 1σ）',
    'confounded': '高低頻有差距，但只存在於跨日、同一天之內沒有——無法分辨是轉速還是趨勢',
    'no_variation': '這段期間運轉時轉速幾乎沒變，量不到（不代表沒效應）',
    'insufficient': '配對到的小時數不足，不下結論',
}


def _short(tag: str) -> str:
    return tag.rsplit('|', 1)[-1]


def report(df: pd.DataFrame, info: dict) -> None:
    print('\n' + '=' * 78)
    print('轉速對 velRMS 的影響')
    print('=' * 78)
    print(f"振動端設備 {info['n_vib_devices']} 台；對應表有頻率 tag 的設備 "
          f"{info['n_map_devices']} 台（{info['n_freq_tags']} 個 tag）")
    if info['map_only']:
        print(f"\n⚠ 對應表有、但振動資料找不到的設備（{len(info['map_only'])}）："
              f"{info['map_only'][:8]}{' …' if len(info['map_only']) > 8 else ''}")
        print('  → 多半是 device_id 寫法與振動檔 Name 欄不一致（大小寫、空白、M1/M2 後綴）')
    if info['vib_only']:
        print(f"ℹ 振動資料有、但對應表沒有頻率 tag 的設備：{len(info['vib_only'])} 台"
              f"（定頻設備本來就可能沒填，不一定是漏）")

    if df.empty:
        print('\n沒有任何一台能配對——結果是空的是因為「沒對上」，不是「沒有效應」。')
        return

    counts = df['verdict'].value_counts()
    print(f"\n判定分布（{len(df)} 個 設備×tag）：")
    for v in _VERDICT_ORDER:
        if counts.get(v, 0):
            print(f'  {v:13s} {counts[v]:3d}  {_VERDICT_NOTE[v]}')

    shown = df[df['verdict'].isin(['material', 'moderate', 'minor', 'confounded'])].copy()
    if not shown.empty:
        shown['_abs'] = shown['shift_sigma'].abs()
        shown = shown.sort_values('_abs', ascending=False)
        print('\n可比較的設備（依 |shift_σ| 由大到小）：')
        print(f'  {"device":16s} {"tag":24s} {"小時":>5s} {"freq 涵蓋":>14s} '
              f'{"span%":>6s} {"velRMS 低→高":>16s} {"變化%":>7s} {"shift_σ":>8s} '
              f'{"rho":>6s} {"rho_day":>8s}  verdict')
        print('  ' + '-' * 128)
        for _, r in shown.iterrows():
            warn = ' ⚠' if r.get('trend_suspect') else ''
            print(f'  {r["device_id"]:16.16s} {_short(r["tag_id"]):24.24s} '
                  f'{int(r["n_hours"]):5d} '
                  f'{r["freq_p05"]:5.1f}–{r["freq_p95"]:5.1f} Hz '
                  f'{r["freq_span_pct"]:6.1f} '
                  f'{r["vel_low"]:7.2f}→{r["vel_high"]:6.2f} '
                  f'{r["shift_pct"]:+7.1f} {r["shift_sigma"]:+8.2f} '
                  f'{r["rho"]:+6.2f} {r["rho_day"]:+8.2f}  {r["verdict"]}{warn}')

    skipped = df[df['verdict'].isin(['no_variation', 'insufficient'])]
    if not skipped.empty:
        print(f'\n量不到的 {len(skipped)} 個（轉速沒變或小時數不足），僅列名稱：')
        for v in ('no_variation', 'insufficient'):
            sub = skipped[skipped['verdict'] == v]
            if len(sub):
                names = [f"{r['device_id']}({int(r['n_hours'])}h)" for _, r in sub.iterrows()]
                print(f'  {v}: {", ".join(names[:12])}{" …" if len(names) > 12 else ""}')

    print('\n讀法：')
    print('  · shift_σ 正＝轉速高時 velRMS 較大。rho 為 Spearman 相關。')
    print('  · ⚠ / confounded ＝ rho 大、但同一天之內的 rho_day 幾乎為 0：相關可能來自')
    print('    跨日的緩慢趨勢（季節、負載、劣化），不是轉速效應，不可據以分層。')
    print('  · 這份只涵蓋指定時段。「量不到」不等於「沒效應」，換季或調速範圍')
    print('    變寬後要重量。是否分層請拿 material 的設備數量與工作量一起討論。')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='velRMS 對轉速（SCADA 頻率）的相依性')
    ap.add_argument('--data-dir', required=True, help='Analytic CSV 資料夾')
    ap.add_argument('--tagmap', required=True, help='tag 對應表 CSV')
    ap.add_argument('--readings', required=True, help='SCADA 讀值 CSV（DATETIME/TAGNAME/VALUE）')
    ap.add_argument('--pattern', default='*.csv')
    ap.add_argument('--since', help='起日 YYYY-MM-DD（含）')
    ap.add_argument('--until', help='迄日 YYYY-MM-DD（含）')
    ap.add_argument('--csv', help='結果表輸出路徑')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format='%(levelname)s %(message)s')
    df, info = run(args.data_dir, args.tagmap, args.readings, args.pattern,
                   args.since, args.until)
    report(df, info)
    if args.csv and not df.empty:
        df.to_csv(args.csv, index=False, encoding='utf-8-sig')
        print(f'\n已輸出：{args.csv}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
