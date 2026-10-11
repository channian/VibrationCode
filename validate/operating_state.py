#!/usr/bin/env python3
"""
operating_state.py — 台帳、SCADA、振動：三方對「這台有沒有在運轉」的說法對得上嗎？

**為什麼要做這件事**

「這台設備有沒有在轉」是好幾條規則的前提，但現行系統只有一個判斷來源：
振動速度 `velRMS > 0.1`（`pipeline.aggregate.mark_running`；Analytic 檔沒有
電流欄時的預設）。這個門檻在備機上不可靠——停機的泵坐在運轉中的機組旁邊，
管路與基座會把振動傳過來，讀值落在 0.1～0.3 mm/s，剛好壓過門檻。後果：

· `STANDBY_NO_RUNTIME`（備機長期未啟動）用同一個門檻判斷「有沒有運轉」，
  對這類備機是瞎的；
· 這些機器被當成運轉中，基準期落在環境振動水準，標準差只有 0.001～0.005
  mm/s，於是 0.01～0.02 mm/s 的變化就算成 5～12σ，觸發 `VEL_HIGH`
  （sigma_fallback 模式）。

另一個來源是台帳的 `is_standby`，但那是靜態旗標，備機與主機可能輪替。
SCADA 的頻率／電流是第三個來源。三者互相核對，才知道誰說得對。

**這支工具做的事**

對每個量測點算：
· SCADA 說有運轉的小時比例
· 振動說有運轉的小時比例（兩種定義：小時內任何一筆 > 門檻 ＝ 管線的
  `n_samples_running > 0`；小時中位數 > 門檻）
· SCADA 判定停止時的 velRMS 水準，以及同型運轉泵（同額定功率與轉速）的
  水準——若遠低於同型運轉泵，是環境振動；若相近，是真的在轉
· 台帳的 `is_standby` 是否與 SCADA 衝突

然後分類（`state`）：

    agree_running            SCADA 與振動都說在轉
    agree_stopped            SCADA 與振動都說沒在轉
    stopped_but_vib_running  SCADA 說停，振動說在轉 ← 本工具最想抓的
    scada_on_vib_quiet       SCADA 說在轉，振動卻沒過門檻（運轉但很安靜）
    intermittent             SCADA 運轉比例介於 5%～50%
    no_scada                 沒有可用的 SCADA 訊號（沒對應、或 tag 恆定不變）

`stopped_but_vib_running` 再依水準細分 `finding`：

    idle_looks_running   水準遠低於同型運轉泵（或台帳標備機且水準不像運轉）
                         → 停機設備被環境振動撐過門檻
    unexplained_running  水準與同型運轉泵相近，台帳也沒說是備機
                         → 可能走旁路／工頻（變頻器顯示 0），或 SCADA tag 不對
    unclear              證據不足（沒有同型運轉泵可比、台帳也沒填）

用法：

    python -m validate.operating_state --data-dir data/ \\
        --tagmap out/tagmap.csv --readings scada_csv/ --ledger out/ledger.csv \\
        --since 2026-06-01 --until 2026-08-20 --csv out/operating_state.csv

**限制（解讀前請先讀）**

1. SCADA 的「停止」只在 tag 真的代表該設備時成立。變頻器 tag 在旁路／工頻
   運轉時會是 0——那時「SCADA 說停」本身就是錯的。`unexplained_running`
   正是在標這種情形，不是下結論。
2. 恆為同一個非零值的 tag（例如定頻冰水機的 60 Hz）不含運轉資訊，不列入判斷。
3. 「同型」只用額定功率與轉速（來自台帳）近似；沒有同型運轉泵可比時，
   水準證據為 unknown，不硬猜。
4. 為了速度，預設每 5 筆取 1 筆（`--stride`）。逐秒資料每小時仍有數百筆，
   對小時中位數沒有影響，對「小時內任何一筆」的判定略偏保守。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

import numpy as np
import pandas as pd

from validate.iso_readiness import _sniff
from validate.points import _ledger_bool, match_ledger_ids
from validate.speed_effect import (MIN_VIB_ROWS_PER_HOUR, _time_col, floor_to_hour,
                                   hourly_frequency)
from vibcore.config import DEFAULT_AGG
from vibcore.io.scada import match_readings, parse_readings, parse_tagmap

logger = logging.getLogger(__name__)

# 運轉門檻直接取自正式管線的設定，不在這裡另訂一份——兩邊數字不同，
# 這支工具就失去「對照現行判定」的意義。
VEL_ON = DEFAULT_AGG.vel_running_threshold
CURRENT_ON = DEFAULT_AGG.current_running_threshold
FREQ_ON_HZ = 1.0      # 變頻器停機時輸出 0；留 1 Hz 的餘裕避免讀值雜訊

_ON_THRESHOLD = {'frequency': FREQ_ON_HZ, 'current': CURRENT_ON}

# tag 的資訊量：運轉比例幾乎永遠是 1（定頻 tag）就沒有資訊
TAG_ALWAYS_ON = 0.98
TAG_ALWAYS_OFF = 0.02

MIN_KNOWN_HOURS = 48      # SCADA 小時數不足就不下結論
MIN_LEVEL_HOURS = 12      # 算「運轉時／停止時的水準」至少要的小時數

ON_SHARE_RUNNING = 0.5
ON_SHARE_STOPPED = 0.05
VIB_RUNNING_SHARE = 0.5
VIB_STOPPED_SHARE = 0.2

LEVEL_RATIO_AMBIENT = 0.5   # 水準 < 同型運轉泵的 50% → 環境振動
LEVEL_RATIO_RUNNING = 0.8   # ≥ 80% → 像是真的在轉
PEER_RPM_TOL = 30.0
PEER_KW_TOL = 0.01


# ── 載入 ──────────────────────────────────────────────

def tri(x) -> bool | None:
    """
    台帳布林欄的三態：True／False／None（沒填）。

    **為什麼不直接用 `is True`**：台帳欄位每一格都有填時，pandas 會把整欄轉成
    numpy 布林，逐格取出來是 `numpy.bool_`，`np.True_ is True` 為 False——
    備機會被悄悄當成非備機。真實台帳恰好是每格都填，所以一定要正規化。
    """
    if x is None:
        return None
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    return None        # NaN 或其他：視為沒填


def machine_of(device_id: str) -> str:
    """量測點名稱 → 機器名稱（去掉 `_M1`／`_M2` 後綴）。同一台機器的兩個點不互當同型。"""
    return re.sub(r'_M\d+$', '', str(device_id))


def hourly_vibration_all(path: str, stride: int = 1) -> tuple[pd.DataFrame, set[str]]:
    """
    讀一個 Analytic CSV，壓成每小時：筆數、超過運轉門檻的筆數、中位數。

    與 `speed_effect.hourly_vibration` 的差別：**不先濾掉低振動的列**——要看
    的正是「停機時振動是多少」，濾掉就看不到了。

    Returns:
        `(欄位 device_id / ts_hour / n_rows / n_over / vel_med 的表, 檔內出現的設備名)`
    """
    empty = pd.DataFrame(columns=['device_id', 'ts_hour', 'n_rows', 'n_over', 'vel_med'])
    sniffed = _sniff(path)
    if sniffed is None:
        return empty, set()
    enc, sep, cols = sniffed
    tcol = _time_col(cols)
    if tcol is None or 'Name' not in cols or 'velRMS' not in cols:
        logger.warning(f"{os.path.basename(path)} 缺少 Name / 時間 / velRMS 欄，略過")
        return empty, set()
    try:
        df = pd.read_csv(path, sep=sep, usecols=[tcol, 'Name', 'velRMS'], encoding=enc,
                         dtype={'Name': 'category', tcol: str})
    except Exception as e:
        logger.warning(f"{os.path.basename(path)} 讀取失敗（{type(e).__name__}: {e}），略過")
        return empty, set()

    names = {str(c).strip() for c in df['Name'].cat.categories}
    if stride > 1:
        df = df.iloc[::stride]
    vel = pd.to_numeric(df['velRMS'], errors='coerce')
    ok = vel.notna()
    if not ok.any():
        return empty, names

    sub = pd.DataFrame({
        'device_id': df.loc[ok, 'Name'].astype(str).str.strip(),
        'vel': vel[ok],
        'ts_hour': floor_to_hour(df.loc[ok, tcol], os.path.basename(path)),
    }).dropna(subset=['ts_hour'])
    sub = sub[sub['device_id'].ne('nan') & sub['device_id'].ne('')]
    if sub.empty:
        return empty, names
    sub['over'] = sub['vel'] > VEL_ON

    out = (sub.groupby(['device_id', 'ts_hour'])
           .agg(n_rows=('vel', 'size'), n_over=('over', 'sum'), vel_med=('vel', 'median'))
           .reset_index())
    return out[out['n_rows'] >= MIN_VIB_ROWS_PER_HOUR].reset_index(drop=True), names


def load_vibration_all(data_dir: str, pattern: str = '*.csv', stride: int = 1,
                       since: str | None = None, until: str | None = None,
                       progress: bool = False) -> tuple[pd.DataFrame, set[str]]:
    import glob
    paths = sorted(glob.glob(os.path.join(data_dir, pattern)))
    cols = ['device_id', 'ts_hour', 'n_rows', 'n_over', 'vel_med']
    if not paths:
        logger.error(f"{data_dir} 找不到符合 {pattern} 的檔案")
        return pd.DataFrame(columns=cols), set()
    frames, names = [], set()
    for i, p in enumerate(paths, 1):
        if progress:
            print(f'\r讀取振動資料 {i}/{len(paths)} …', end='', file=sys.stderr, flush=True)
        f, n = hourly_vibration_all(p, stride)
        names |= n
        if not f.empty:
            frames.append(f)
    if progress:
        print(file=sys.stderr)
    if not frames:
        return pd.DataFrame(columns=cols), names
    v = pd.concat(frames, ignore_index=True)
    v = (v.sort_values('n_rows').drop_duplicates(['device_id', 'ts_hour'], keep='last')
         .sort_values(['device_id', 'ts_hour']).reset_index(drop=True))
    if since:
        v = v[v['ts_hour'] >= pd.Timestamp(since)]
    if until:
        v = v[v['ts_hour'] < pd.Timestamp(until) + pd.Timedelta(days=1)]
    return v.reset_index(drop=True), names


def load_ledger(path: str | None) -> pd.DataFrame:
    """
    台帳 CSV → 欄位 `device_id, is_standby, rated_power_kw, rated_rpm`。
    空白一律視為沒填（is_standby 為三態，None 代表台帳沒說）。
    """
    cols = ['device_id', 'is_standby', 'rated_power_kw', 'rated_rpm']
    if not path:
        return pd.DataFrame(columns=cols)
    df = None
    for enc in ('utf-8-sig', 'utf-8', 'cp950'):
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str)
            break
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            logger.error(f"台帳 {path} 不存在")
            return pd.DataFrame(columns=cols)
    if df is None or 'device_id' not in df.columns:
        logger.error(f"台帳 {path} 無法讀取或缺少 device_id 欄")
        return pd.DataFrame(columns=cols)

    def num(x):
        try:
            return float(str(x).strip())
        except (TypeError, ValueError):
            return np.nan

    standby = ([_ledger_bool(x) for x in df['is_standby']] if 'is_standby' in df.columns
               else [None] * len(df))
    out = pd.DataFrame({
        'device_id': df['device_id'].astype(str).str.strip(),
        'is_standby': pd.Series(standby, index=df.index, dtype=object),
        'rated_power_kw': df['rated_power_kw'].map(num) if 'rated_power_kw' in df.columns else np.nan,
        'rated_rpm': df['rated_rpm'].map(num) if 'rated_rpm' in df.columns else np.nan,
    })
    return out[out['device_id'].ne('')].reset_index(drop=True)


# ── SCADA 這一側 ───────────────────────────────────────

def classify_tags(hv: pd.DataFrame, vtype_of: dict[str, str]) -> dict[str, dict]:
    """
    每個 tag 的資訊量。

    · `toggling`   有開有關，最有資訊
    · `always_off` 幾乎全程為 0：資訊量夠（它說「沒運轉」），但可能是旁路或 tag 本身有問題
    · `always_on`  幾乎全程非零（定頻 tag 如 60 Hz）：不含運轉資訊，不列入判斷
    · `sparse`     小時數不足
    """
    info: dict[str, dict] = {}
    for tag, g in hv.groupby('tag_id'):
        vt = vtype_of.get(tag)
        thr = _ON_THRESHOLD.get(vt)
        if thr is None:
            continue
        n = len(g)
        share = float((g['freq'] > thr).mean()) if n else float('nan')
        if n < MIN_KNOWN_HOURS:
            kind = 'sparse'
        elif share >= TAG_ALWAYS_ON:
            kind = 'always_on'
        elif share <= TAG_ALWAYS_OFF:
            kind = 'always_off'
        else:
            kind = 'toggling'
        info[tag] = {'kind': kind, 'n_hours': n, 'share_on': share, 'vtype': vt}
    return info


def device_scada_series(hv: pd.DataFrame, tag_info: dict[str, dict],
                        tags: list[str]) -> pd.Series | None:
    """
    一台設備在每個小時的 SCADA 運轉與否（布林，索引為 ts_hour）；
    沒有任何有資訊的 tag 回 None。任何一個有資訊的 tag 說「在轉」就算在轉。
    """
    usable = [t for t in tags if tag_info.get(t, {}).get('kind') in ('toggling', 'always_off')]
    if not usable:
        return None
    sub = hv[hv['tag_id'].isin(usable)].copy()
    if sub.empty:
        return None
    sub['on'] = [v > _ON_THRESHOLD[tag_info[t]['vtype']] for v, t in zip(sub['freq'], sub['tag_id'])]
    return sub.groupby('ts_hour')['on'].max()


# ── 分類 ──────────────────────────────────────────────

def classify_state(on_share: float | None, known_hours: int,
                   vib_any_share: float, vib_med_share: float) -> str:
    if on_share is None or known_hours < MIN_KNOWN_HOURS:
        return 'no_scada'
    if on_share >= ON_SHARE_RUNNING:
        return 'agree_running' if vib_any_share >= VIB_RUNNING_SHARE else 'scada_on_vib_quiet'
    if on_share <= ON_SHARE_STOPPED:
        return 'agree_stopped' if vib_med_share <= VIB_STOPPED_SHARE else 'stopped_but_vib_running'
    return 'intermittent'


def level_evidence(ratio: float | None) -> str:
    if ratio is None or not np.isfinite(ratio):
        return 'unknown'
    if ratio < LEVEL_RATIO_AMBIENT:
        return 'ambient_like'
    if ratio >= LEVEL_RATIO_RUNNING:
        return 'running_like'
    return 'ambiguous'


def classify_finding(state: str, ledger_standby, evidence: str) -> str:
    ledger_standby = tri(ledger_standby)
    if state == 'agree_running':
        return 'running_confirmed'
    if state == 'agree_stopped':
        return 'stopped_confirmed'
    if state == 'stopped_but_vib_running':
        if evidence == 'ambient_like' or (ledger_standby is True and evidence != 'running_like'):
            return 'idle_looks_running'
        if evidence == 'running_like' and ledger_standby is not True:
            return 'unexplained_running'
        return 'unclear'
    return state


def ledger_check(ledger_standby, state: str) -> str:
    """台帳的備機旗標與 SCADA 衝突嗎？以 SCADA 的運轉判定為準。"""
    ledger_standby = tri(ledger_standby)
    if ledger_standby is None:
        return 'unfilled'
    running = {'agree_running'}
    stopped = {'agree_stopped', 'stopped_but_vib_running'}
    if ledger_standby is True:
        if state in running:
            return 'standby_but_running'
        return 'ok' if state in stopped else 'n/a'
    if state in stopped:
        return 'primary_but_stopped'
    return 'ok' if state in running else 'n/a'


# ── 主流程 ────────────────────────────────────────────

def analyze(vib: pd.DataFrame, tags_by_device: dict[str, list[str]], hv: pd.DataFrame,
            tag_info: dict[str, dict], ledger: pd.DataFrame) -> pd.DataFrame:
    led = ledger.drop_duplicates('device_id').set_index('device_id') if not ledger.empty else None

    rows = []
    for dev, g in vib.groupby('device_id'):
        n_vib = len(g)
        row = {
            'device_id': dev,
            'vib_hours': n_vib,
            'vib_any_share': float((g['n_over'] >= 1).mean()),
            'vib_med_share': float((g['vel_med'] > VEL_ON).mean()),
            'vel_med_all': float(g['vel_med'].median()),
        }
        if led is not None and dev in led.index:
            lr = led.loc[dev]
            row['ledger_standby'] = tri(lr['is_standby'])
            row['rated_power_kw'] = lr['rated_power_kw']
            row['rated_rpm'] = lr['rated_rpm']
        else:
            row['ledger_standby'], row['rated_power_kw'], row['rated_rpm'] = None, np.nan, np.nan

        s = device_scada_series(hv, tag_info, tags_by_device.get(dev, []))
        row['scada_tags'] = sum(1 for t in tags_by_device.get(dev, [])
                                if tag_info.get(t, {}).get('kind') in ('toggling', 'always_off'))
        if s is None:
            row.update(scada_hours=0, scada_on_share=np.nan, vel_when_on=np.nan, vel_when_off=np.nan)
        else:
            m = g.merge(s.rename('on').reset_index(), on='ts_hour', how='inner')
            on_m, off_m = m[m['on']], m[~m['on']]
            row.update(
                scada_hours=int(len(s)), scada_on_share=float(s.mean()),
                vel_when_on=float(on_m['vel_med'].median()) if len(on_m) >= MIN_LEVEL_HOURS else np.nan,
                vel_when_off=float(off_m['vel_med'].median()) if len(off_m) >= MIN_LEVEL_HOURS else np.nan)
        row['state'] = classify_state(
            None if s is None else row['scada_on_share'], row['scada_hours'],
            row['vib_any_share'], row['vib_med_share'])
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # 同型運轉泵的水準：同額定功率、轉速相近、不是同一台機器、SCADA 與振動都說在轉
    df['machine'] = df['device_id'].map(machine_of)
    runners = df[(df['state'] == 'agree_running') & df['vel_when_on'].notna()]
    peer_level, peer_n = [], []
    for _, r in df.iterrows():
        kw, rpm = r['rated_power_kw'], r['rated_rpm']
        if not (np.isfinite(kw) and np.isfinite(rpm)):
            peer_level.append(np.nan)
            peer_n.append(0)
            continue
        p = runners[(runners['machine'] != r['machine'])
                    & ((runners['rated_power_kw'] - kw).abs() <= PEER_KW_TOL)
                    & ((runners['rated_rpm'] - rpm).abs() <= PEER_RPM_TOL)]
        peer_level.append(float(p['vel_when_on'].median()) if len(p) else np.nan)
        peer_n.append(len(p))
    df['peer_level'], df['peer_n'] = peer_level, peer_n

    # 水準：停止時的 velRMS（沒有就用全程中位數）對同型運轉泵
    own = df['vel_when_off'].where(df['vel_when_off'].notna(), df['vel_med_all'])
    df['level_ratio'] = own / df['peer_level']
    df['level_evidence'] = df['level_ratio'].map(level_evidence)
    df['ledger_standby'] = pd.Series([tri(x) for x in df['ledger_standby']], index=df.index, dtype=object)
    df['finding'] = [classify_finding(s, l, e)
                     for s, l, e in zip(df['state'], df['ledger_standby'], df['level_evidence'])]
    df['ledger_check'] = [ledger_check(l, s) for l, s in zip(df['ledger_standby'], df['state'])]
    return df.drop(columns=['machine']).sort_values('device_id').reset_index(drop=True)


def run(data_dir: str, tagmap_path: str, readings_path: str, ledger_path: str | None,
        pattern: str = '*.csv', since: str | None = None, until: str | None = None,
        stride: int = 5, progress: bool = False) -> tuple[pd.DataFrame, dict]:
    mappings = [m for m in parse_tagmap(tagmap_path)
                if m.is_active and m.variable_type in _ON_THRESHOLD]
    readings = parse_readings(readings_path)
    readings, _ = match_readings(readings, mappings)
    hv = hourly_frequency(readings)
    if since:
        hv = hv[hv['ts_hour'] >= pd.Timestamp(since)]
    if until:
        hv = hv[hv['ts_hour'] < pd.Timestamp(until) + pd.Timedelta(days=1)]
    vtype_of = {m.tag_id: m.variable_type for m in mappings}
    tag_info = classify_tags(hv, vtype_of)

    tags_by_device: dict[str, list[str]] = {}
    for m in mappings:
        tags_by_device.setdefault(m.device_id, []).append(m.tag_id)

    vib, names = load_vibration_all(data_dir, pattern, stride, since, until, progress)
    ledger = load_ledger(ledger_path)
    table = analyze(vib, tags_by_device, hv, tag_info, ledger)

    near_l, miss_l = match_ledger_ids(ledger['device_id'], names) if not ledger.empty else ({}, [])
    near_t, miss_t = match_ledger_ids(tags_by_device.keys(), names)
    info = {
        'n_vib_devices': len(names),
        'n_ledger': len(ledger),
        'n_tag_devices': len(tags_by_device),
        'ledger_near': near_l, 'ledger_missing': miss_l,
        'tagmap_near': near_t, 'tagmap_missing': miss_t,
        'tag_kinds': pd.Series({t: i['kind'] for t, i in tag_info.items()}).value_counts().to_dict(),
        'stride': stride,
    }
    return table, info


# ── 輸出 ──────────────────────────────────────────────

_STATE_NOTE = {
    'agree_running': 'SCADA 與振動都說在轉',
    'agree_stopped': 'SCADA 與振動都說沒在轉',
    'stopped_but_vib_running': 'SCADA 說停，振動卻說在轉',
    'scada_on_vib_quiet': 'SCADA 說在轉，振動沒過門檻（運轉但很安靜）',
    'intermittent': 'SCADA 運轉比例 5%～50%',
    'no_scada': '沒有可用的 SCADA 訊號',
}
_FINDING_NOTE = {
    'idle_looks_running': '停機設備被環境振動撐過門檻（水準遠低於同型運轉泵，或台帳標備機）',
    'unexplained_running': '水準像在轉、台帳也沒說是備機：可能旁路／工頻運轉，或 SCADA tag 不對',
    'unclear': '證據不足（沒有同型運轉泵可比、台帳也沒填）',
}
_LEDGER_NOTE = {
    'standby_but_running': '台帳標備機，但 SCADA 顯示大部分時間在轉',
    'primary_but_stopped': '台帳標非備機，但 SCADA 顯示整段沒在轉',
}


def _pct(x) -> str:
    return '  —' if x is None or (isinstance(x, float) and np.isnan(x)) else f'{x * 100:3.0f}%'


def _num(x, fmt='{:.2f}') -> str:
    return '  —' if x is None or (isinstance(x, float) and np.isnan(x)) else fmt.format(x)


def report(df: pd.DataFrame, info: dict) -> None:
    print('\n' + '=' * 78)
    print('運轉狀態對照：台帳 × SCADA × 振動')
    print('=' * 78)
    print(f"振動資料 {info['n_vib_devices']} 個點；SCADA 對應表涵蓋 {info['n_tag_devices']} 個點；"
          f"台帳 {info['n_ledger']} 筆")
    kinds = info['tag_kinds']
    print(f"SCADA tag 資訊量：toggling {kinds.get('toggling', 0)}、always_off {kinds.get('always_off', 0)}、"
          f"always_on（定頻，不採用）{kinds.get('always_on', 0)}、sparse {kinds.get('sparse', 0)}")

    # ── 名稱對不上 ──
    for label, near, miss in (('台帳', info['ledger_near'], info['ledger_missing']),
                              ('SCADA 對應表', info['tagmap_near'], info['tagmap_missing'])):
        for k, v in sorted(near.items()):
            print(f"⚠ {label}的 {k!r} 對不上，振動資料裡是 {v!r}（只差空白／底線／大小寫）——該筆沒有生效")
        if miss:
            print(f"ℹ {label}有 {len(miss)} 筆在振動資料裡找不到對應："
                  f"{'、'.join(sorted(miss)[:6])}{' …' if len(miss) > 6 else ''}")

    if df.empty:
        print('\n沒有任何振動資料可分析。')
        return

    print(f"\n【SCADA × 振動】共 {len(df)} 個點：")
    for st, note in _STATE_NOTE.items():
        n = int((df['state'] == st).sum())
        if n:
            print(f'  {st:24s} {n:3d}  {note}')

    sbr = df[df['state'] == 'stopped_but_vib_running']
    if not sbr.empty:
        print(f'\n【SCADA 說停、振動說在轉】{len(sbr)} 個點')
        for f, note in _FINDING_NOTE.items():
            n = int((sbr['finding'] == f).sum())
            if n:
                print(f'  {f:20s} {n:3d}  {note}')
        print(f'\n  {"點":14s} {"備機":>4s} {"SCADA運轉":>9s} {"振動運轉(任一/中位)":>18s} '
              f'{"停止時velRMS":>12s} {"同型運轉泵":>10s} {"比值":>5s}  判定')
        print('  ' + '-' * 100)
        for _, r in sbr.sort_values(['finding', 'device_id']).iterrows():
            sb = {True: '是', False: '否'}.get(r['ledger_standby'], ' —')
            print(f"  {r['device_id']:14.14s} {sb:>4s} {_pct(r['scada_on_share']):>9s} "
                  f"{_pct(r['vib_any_share']):>9s}/{_pct(r['vib_med_share']):<8s} "
                  f"{_num(r['vel_when_off'] if pd.notna(r['vel_when_off']) else r['vel_med_all']):>12s} "
                  f"{_num(r['peer_level']):>10s} {_num(r['level_ratio']):>5s}  {r['finding']}")

    conflicts = df[df['ledger_check'].isin(_LEDGER_NOTE)]
    if not conflicts.empty:
        print(f'\n【台帳與 SCADA 衝突】{len(conflicts)} 個點')
        for chk, note in _LEDGER_NOTE.items():
            sub = conflicts[conflicts['ledger_check'] == chk]
            if sub.empty:
                continue
            print(f'  {note}（{len(sub)}）：')
            for _, r in sub.iterrows():
                print(f"    {r['device_id']:14.14s} SCADA 運轉 {_pct(r['scada_on_share'])}，"
                      f"振動運轉 {_pct(r['vib_any_share'])}，velRMS 中位 {_num(r['vel_med_all'])}")

    # 對現行規則的影響：台帳標備機、SCADA 也說沒在轉，卻被速度門檻判成幾乎一直在運轉。
    # 只算 SCADA 也說停的——台帳標備機但 SCADA 顯示在轉的是台帳錯誤，那是另一件事。
    standby_pts = df[df['ledger_standby'].map(lambda x: tri(x) is True)]
    stopped = standby_pts[standby_pts['state'].isin(['agree_stopped', 'stopped_but_vib_running'])]
    blind = stopped[stopped['vib_any_share'] >= VIB_RUNNING_SHARE]
    if len(stopped):
        print(f'\n【對現行規則的影響】台帳標備機、SCADA 也顯示沒在轉的 {len(stopped)} 個點中，'
              f'{len(blind)} 個被速度門檻（{VEL_ON} mm/s）判成超過一半的小時都在運轉。')
        print('  STANDBY_NO_RUNTIME 以同一個門檻判斷「有沒有運轉」，對這些備機等於失效；')
        print('  它們的基準落在環境振動水準，VEL_HIGH（sigma_fallback）會被 0.01～0.02 mm/s 的變化觸發。')

    print('\n讀法：')
    print('  · 「同型運轉泵」＝同額定功率、轉速相近、SCADA 與振動都說在轉的其他機器；比值 < 0.5 ＝')
    print('    停機時的 velRMS 不到運轉時的一半，是環境振動的特徵。沒有同型可比時顯示 —，不硬猜。')
    print('  · unexplained_running 不是結論：旁路運轉時變頻器顯示 0，那時 SCADA 說「停」本身就是錯的。')
    print(f"  · 為了速度每 {info['stride']} 筆取 1 筆；對小時中位數沒有影響。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='台帳 × SCADA × 振動：運轉狀態對照')
    ap.add_argument('--data-dir', required=True, help='Analytic CSV 資料夾')
    ap.add_argument('--tagmap', required=True)
    ap.add_argument('--readings', required=True, help='SCADA 讀值：CSV、資料夾或萬用字元')
    ap.add_argument('--ledger', help='台帳 CSV（device_id / is_standby / rated_power_kw / rated_rpm）')
    ap.add_argument('--pattern', default='*.csv')
    ap.add_argument('--since')
    ap.add_argument('--until')
    ap.add_argument('--stride', type=int, default=5, help='逐秒資料每幾筆取 1 筆（預設 5）')
    ap.add_argument('--csv', help='結果表輸出路徑')
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format='%(levelname)s %(message)s')
    df, info = run(args.data_dir, args.tagmap, args.readings, args.ledger, args.pattern,
                   args.since, args.until, max(1, args.stride), progress=True)
    report(df, info)
    if args.csv and not df.empty:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or '.', exist_ok=True)
        df.to_csv(args.csv, index=False, encoding='utf-8-sig')
        print(f'\n已輸出：{args.csv}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
