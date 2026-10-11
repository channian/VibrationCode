#!/usr/bin/env python3
"""
alarm_attribution.py — 回測裡的告警，是被什麼驅動的？

**問題**

回測產出的 `episodes_detail.csv` 每一筆是一個告警事件。其中有一部分並不是
設備劣化，而是：

· **停機設備被環境振動撐過運轉門檻**：備機坐在運轉中的機組旁邊，velRMS 落在
  0.1～0.3 mm/s，被當成運轉中；基準的標準差只有 0.001～0.005 mm/s，0.01～0.02
  mm/s 的變化就是 5～12σ。（由 `validate.operating_state` 判定哪些設備屬於此類）
· **轉速變化**：變頻泵多數時間貼在下限轉速，只在高負載時拉高，velRMS 隨之
  倍增；基準落在低轉速水準，高轉速時段就整段告警。
  （由 `validate.speed_effect` 判定哪些設備的 velRMS 隨轉速明顯變化）

這支工具把兩份判定結果對上 episodes，回答「VEL_HIGH 有多少比例落在這兩類」。

**轉速是否真的解釋了某筆 VEL_HIGH**：`speed_effect` 對每台設備給出低轉速時段與
高轉速時段的 velRMS 水準（`vel_low`、`vel_high`）。把該筆事件的基準值與當前值
放到 `vel_low → vel_high` 這條軸上（0 ＝低轉速水準，1 ＝高轉速水準）：

    基準在低轉速端（≤ 0.25）、當前值已走到一半以上（≥ 0.5）→ 轉速解釋了這筆
    基準在高轉速端（≥ 0.75）、當前值落到一半以下（≤ 0.5）→ 同上，方向相反
    且當前值不能超出這條軸太多（超出一個軸長的一半以上不算）

最後一條是為了不灌水：`vel_high` 是「轉速最高那四分之一時段」的中位數，轉速
最高的時段略高於它是正常的；但若當前值比它高出一個軸長的一半以上（例如 CP 08_M1
的 1.80，在軸上的位置是 2.4），那是轉速狀態轉換之外的變化，不能算成轉速造成的。

這個判斷**不需要** SCADA，只靠兩份 CSV。它能成立，是因為實測發現這些泵的
基準中位數幾乎等於 `vel_low`（或 `vel_high`）——基準落在哪個轉速狀態，就決定
了另一個狀態會不會整段告警。（它**不會**驗證那段期間 SCADA 頻率真的有變；
要驗證用 `python -m validate.speed_effect --daily 設備`。）

**輸入檔案的版本檢查**

這份檔案是哪個版本的程式跑出來的、有沒有帶台帳，直接影響能不能拿來當「現況」。
工具會從內容判斷並提醒：

· IMPACT_RISE 的 evidence 還有 `kurt` 通道或 `require_both` ＝ 移除 kurtosis
  （2026-09 專家會議 Q2）之前的程式
· 有沒有帶台帳：看 `machine_class` 有沒有值、有沒有只有帶台帳才會出現的規則
  （ISO_ZONE／ISO_CLASS_SUSPECT／STANDBY_NO_RUNTIME）。**不能只看 VEL_HIGH**：
  帶了台帳的那一輪，ISO 錨定的點若沒有觸發，剩下的 VEL_HIGH 就會全是
  sigma_fallback（ISO 不適用的點），看起來跟「沒帶台帳」一模一樣。

給 `--ledger` 時，會逐點說明 VEL_HIGH 為什麼走 sigma_fallback：額定功率 ≤ 15 kW
不在 ISO 20816-3 適用範圍、群組或基礎剛性沒填完整、台帳找不到這個 device_id，
或是台帳明明有分類卻仍退回（＝那一輪沒套用台帳）。

用法：

    python -m validate.alarm_attribution --episodes out/episodes_detail.csv \\
        --operating-state out/operating_state.csv --speed-effect out/speed_effect.csv \\
        --ledger ledger.csv --csv out/alarm_attribution.csv
"""

from __future__ import annotations

import argparse
import logging
import os
import re

import numpy as np
import pandas as pd

from validate.points import load_device_meta_overrides, match_ledger_ids
from vibcore.metrics.iso import ISO_MIN_RATED_POWER_KW, ISO_THRESHOLDS

logger = logging.getLogger(__name__)

#: 基準／當前值在 vel_low→vel_high 軸上的位置門檻
POS_NEAR_LOW = 0.25
POS_NEAR_HIGH = 0.75
POS_MID = 0.5

#: 當前值可以超出 vel_low→vel_high 這條軸的幅度（以軸長為單位）。
#: 超出更多就不是「在兩個轉速狀態之間移動」，轉速解釋不了那麼大的幅度。
#: 實測：CP 04 的 1.14（軸位置 1.22）算；CP 08_M1 的 1.80（2.4）、CP 09_M1 的 0.92（2.3）不算。
POS_OVERSHOOT = 0.5

#: 只有帶了台帳才可能出現的規則（需要分類或 is_standby）。
LEDGER_ONLY_RULES = ('ISO_ZONE', 'ISO_CLASS_SUSPECT', 'STANDBY_NO_RUNTIME')

SEGMENTS = ('idle_looks_running', 'speed_explained', 'speed_device_other', 'other')

_SEG_NOTE = {
    'idle_looks_running': '停機設備被環境振動撐過運轉門檻',
    'speed_explained': '轉速驅動：基準在一個轉速狀態、當前值已走到另一個',
    'speed_device_other': '轉速敏感設備上、但不能由轉速狀態解釋的事件',
    'other': '其他',
}


# ── 載入 ──────────────────────────────────────────────

def read_csv_flexible(path: str) -> pd.DataFrame:
    """CSV 讀取：編碼依序試 utf-8-sig／utf-8／cp950；逗號讀不出多欄就改試 TAB。"""
    last = None
    for enc in ('utf-8-sig', 'utf-8', 'cp950'):
        for sep in (',', '\t'):
            try:
                df = pd.read_csv(path, encoding=enc, sep=sep)
            except UnicodeDecodeError as e:
                last = e
                break
            except Exception as e:      # 欄位數不一致等
                last = e
                continue
            if df.shape[1] > 1:
                return df
    raise RuntimeError(f"{path} 無法解析為 CSV：{last}")


def read_episodes(path: str) -> pd.DataFrame:
    df = read_csv_flexible(path)
    df.columns = [str(c).strip() for c in df.columns]
    need = {'device_id', 'rule_code', 'episode_start', 'episode_end'}
    miss = need - set(df.columns)
    if miss:
        raise ValueError(f"{path} 缺少欄位 {sorted(miss)}；實際欄位：{list(df.columns)[:12]}")
    df['device_id'] = df['device_id'].astype(str).str.strip()
    df['episode_start'] = pd.to_datetime(df['episode_start'], errors='coerce')
    df['episode_end'] = pd.to_datetime(df['episode_end'], errors='coerce')
    if 'duration_days' in df.columns:
        df['duration_days'] = pd.to_numeric(df['duration_days'], errors='coerce')
    else:
        df['duration_days'] = (df['episode_end'] - df['episode_start']).dt.days + 1
    for c in ('baseline_value', 'current_value'):
        df[c] = pd.to_numeric(df[c], errors='coerce') if c in df.columns else np.nan
    return df


# ── 版本檢查 ──────────────────────────────────────────

def _is_blank(col: pd.Series) -> pd.Series:
    """
    空白或缺值。

    **不能靠 `astype(str)` 再比對 'nan'**：pandas 3 起 `astype(str)` 會保留缺值
    （不再變成字串 'nan'），舊版相反。用 `isna()` 明確處理，兩種版本都對。
    """
    return col.isna() | col.astype(str).str.strip().isin(['', 'nan', 'None'])


def detect_provenance(df: pd.DataFrame) -> dict:
    """
    從內容判斷這份 episodes 是哪個版本的程式、有沒有帶台帳。

    資料夾名稱（例如 `after_switch`，指前端換版後的資料）不是版本資訊，
    不能拿來判斷這份輸出是不是最新一輪。

    **有沒有帶台帳不能只看 VEL_HIGH。** 帶了台帳的那一輪，ISO 錨定的點若沒有
    觸發 VEL_HIGH，剩下的 VEL_HIGH 就只剩 ISO 不適用的點（額定功率 ≤ 15 kW 或
    未分類），全是 sigma_fallback、`machine_class` 全空——跟沒帶台帳長得一樣。
    所以改看整份 episodes 裡有沒有任何「只有帶台帳才會有」的跡象。

    Returns:
        `legacy_kurtosis`：IMPACT_RISE 的 evidence 含 kurt 通道／require_both（None＝無從判斷）
        `ledger_share`：VEL_HIGH 中有 machine_class 的比例（None＝無 VEL_HIGH 或無此欄）
        `vel_high_modes`：VEL_HIGH 的 threshold_mode 分布
        `ledger`：`'yes'`（有帶台帳的跡象）／`'likely_no'`（沒有任何跡象且 VEL_HIGH 全走
            sigma_fallback，**仍可能是帶了台帳但沒有任何東西觸發**，要給 --ledger 才能確認）／
            `'unknown'`（沒有 VEL_HIGH 也沒有其他跡象）
        `ledger_signals`：判斷依據 `{跡象: 筆數}`
        `fallback_reasons`：VEL_HIGH sigma_fallback 的原因分布（新版程式才有，
            來自 evidence 的 `iso_fallback_reason`）
    """
    prov: dict = {'legacy_kurtosis': None, 'ledger_share': None, 'vel_high_modes': {},
                  'ledger': 'unknown', 'ledger_signals': {}, 'fallback_reasons': {}}
    if 'evidence_json' in df.columns:
        imp = df[df['rule_code'] == 'IMPACT_RISE']['evidence_json'].astype(str)
        if len(imp):
            prov['legacy_kurtosis'] = bool(
                imp.str.contains(r'"require_both"|"kurt(?:_axis)?"\s*:', regex=True).any())
    vh = df[df['rule_code'] == 'VEL_HIGH']
    if len(vh):
        if 'threshold_mode' in vh.columns:
            prov['vel_high_modes'] = vh['threshold_mode'].fillna('(空)').value_counts().to_dict()
        if 'machine_class' in vh.columns:
            prov['ledger_share'] = float((~_is_blank(vh['machine_class'])).mean())

    sig: dict[str, int] = {}
    if 'machine_class' in df.columns:
        n = int((~_is_blank(df['machine_class'])).sum())
        if n:
            sig['machine_class 有值'] = n
    for rule in LEDGER_ONLY_RULES:
        n = int((df['rule_code'] == rule).sum())
        if n:
            sig[rule] = n
    if len(vh) and 'threshold_mode' in vh.columns:
        n = int((vh['threshold_mode'].astype(str) == 'iso').sum())
        if n:
            sig['VEL_HIGH(iso 模式)'] = n
    prov['ledger_signals'] = sig
    if sig:
        prov['ledger'] = 'yes'
    elif len(vh):
        prov['ledger'] = 'likely_no'

    if len(vh) and 'evidence_json' in vh.columns:
        reasons: dict[str, int] = {}
        for raw in vh['evidence_json'].dropna().astype(str):
            m = re.search(r'"iso_fallback_reason"\s*:\s*"([^"]*)"', raw)
            if m:
                reasons[m.group(1)] = reasons.get(m.group(1), 0) + 1
        prov['fallback_reasons'] = reasons
    return prov


# ── 分段 ──────────────────────────────────────────────

def axis_position(x: float, lo: float, hi: float) -> float:
    """x 在 lo→hi 軸上的位置（0＝lo，1＝hi）；lo 與 hi 幾乎相同時回 NaN。"""
    if not (np.isfinite(x) and np.isfinite(lo) and np.isfinite(hi)) or abs(hi - lo) < 1e-9:
        return float('nan')
    return float((x - lo) / (hi - lo))


def explained_by_speed(baseline: float, current: float, lo: float, hi: float) -> bool:
    """
    基準在一個轉速狀態、當前值已走到另一個，**且當前值沒有超出轉速範圍太多**。

    只看「走到另一端」會過度歸因：基準在低端，當前值比高轉速水準還高出好幾倍軸長，
    代表有轉速以外的變化（見 `POS_OVERSHOOT`）。
    """
    tb, tc = axis_position(baseline, lo, hi), axis_position(current, lo, hi)
    if np.isnan(tb) or np.isnan(tc):
        return False
    if not (-POS_OVERSHOOT <= tc <= 1.0 + POS_OVERSHOOT):
        return False
    return (tb <= POS_NEAR_LOW and tc >= POS_MID) or (tb >= POS_NEAR_HIGH and tc <= POS_MID)


def attribute(df: pd.DataFrame, opstate: pd.DataFrame | None,
              speed: pd.DataFrame | None) -> pd.DataFrame:
    """每筆事件加上 `segment`（見 SEGMENTS）。"""
    idle = set()
    if opstate is not None and 'finding' in opstate.columns:
        idle = set(opstate.loc[opstate['finding'] == 'idle_looks_running', 'device_id'].astype(str))
    spd: dict[str, dict] = {}
    if speed is not None and 'verdict' in speed.columns:
        for _, r in speed.drop_duplicates('device_id').iterrows():
            if r['verdict'] == 'material':
                spd[str(r['device_id'])] = {'lo': r.get('vel_low'), 'hi': r.get('vel_high')}

    segs, expl = [], []
    for _, r in df.iterrows():
        dev = r['device_id']
        if dev in idle:
            segs.append('idle_looks_running')
            expl.append(False)
        elif dev in spd:
            ok = (r['rule_code'] == 'VEL_HIGH'
                  and explained_by_speed(r['baseline_value'], r['current_value'],
                                         spd[dev]['lo'], spd[dev]['hi']))
            segs.append('speed_explained' if ok else 'speed_device_other')
            expl.append(bool(ok))
        else:
            segs.append('other')
            expl.append(False)
    out = df.copy()
    out['segment'] = segs
    out['explained_by_speed'] = expl
    if speed is not None and 'device_id' in speed.columns:
        out.attrs['speed_points'] = (len(spd), int(speed['device_id'].nunique()))
    return out


# ── 台帳對照：VEL_HIGH 為什麼走 sigma_fallback ───────────

_FALLBACK_NOTE = {
    'out_of_scope_power': f'額定功率 ≤ {ISO_MIN_RATED_POWER_KW:.0f} kW，不在 ISO 20816-3 適用範圍'
                          '（台帳有填分類也不套用）',
    'unclassified': '群組或基礎剛性沒填完整（兩者缺一就算不出 Zone）',
    'not_in_ledger': '台帳裡找不到這個 device_id（寫法不一致時，台帳整筆不生效）',
    'classified_but_fallback': '台帳有分類、也在適用範圍內，卻仍走 sigma_fallback：'
                               '這一輪沒有套用這份台帳',
}


def explain_fallback(df: pd.DataFrame, overrides: dict) -> pd.DataFrame:
    """
    VEL_HIGH 走 sigma_fallback 的點，逐點用台帳說明原因。

    **為什麼值得單獨看**：sigma_fallback 的 σ 來自基準期的標準差，小機器穩定運轉時
    只有 0.01～0.02 mm/s，一點點變化就是十幾個 σ；ISO 錨定則是以 Zone 寬度為尺度，
    不會有這個問題。所以「哪些點走 sigma_fallback、為什麼」直接決定 VEL_HIGH 的
    告警量。實測（2026-06～08）：台帳完整時 VEL_HIGH 剩下的全是額定 ≤ 15 kW 的泵，
    不是台帳沒填。
    """
    vh = df[df['rule_code'] == 'VEL_HIGH']
    if 'threshold_mode' in vh.columns:
        vh = vh[vh['threshold_mode'].astype(str) != 'iso']
    rows = []
    for dev, g in vh.groupby('device_id'):
        ov = overrides.get(str(dev))
        kw = grp = fnd = None
        if ov is None:
            reason = 'not_in_ledger'
        else:
            kw, grp, fnd = ov.get('rated_power_kw'), ov.get('iso_machine_group'), ov.get('iso_foundation')
            try:
                small = kw is not None and float(kw) <= ISO_MIN_RATED_POWER_KW
            except (TypeError, ValueError):
                small = False
            if small:
                reason = 'out_of_scope_power'
            elif not grp or not fnd or (str(grp), str(fnd)) not in ISO_THRESHOLDS:
                reason = 'unclassified'
            else:
                reason = 'classified_but_fallback'
        rows.append(dict(device_id=str(dev), reason=reason, rated_power_kw=kw,
                         iso_group=grp, iso_foundation=fnd,
                         is_standby=(ov or {}).get('is_standby'),
                         episodes=len(g), days=int(g['duration_days'].sum())))
    return pd.DataFrame(rows, columns=['device_id', 'reason', 'rated_power_kw', 'iso_group',
                                       'iso_foundation', 'is_standby', 'episodes', 'days'])


# ── 輸出 ──────────────────────────────────────────────

def report(df: pd.DataFrame, prov: dict, have_opstate: bool, have_speed: bool,
           fallback: pd.DataFrame | None = None, ledger_near: dict | None = None) -> None:
    print('\n' + '=' * 78)
    print('告警歸因：episodes × 運轉狀態 × 轉速')
    print('=' * 78)
    span = ''
    if df['episode_start'].notna().any():
        span = f"；{df['episode_start'].min():%Y-%m-%d} ~ {df['episode_end'].max():%Y-%m-%d}"
    print(f"episodes {len(df)} 筆、{df['device_id'].nunique()} 個點{span}")
    by_rule = df['rule_code'].value_counts()
    print('規則分布：' + '、'.join(f'{k} {v}' for k, v in by_rule.items()))

    print('\n【這份 episodes 是哪一輪跑出來的】')
    if prov['legacy_kurtosis']:
        print('  ⚠ IMPACT_RISE 的 evidence 還有 kurt 通道／require_both：')
        print('    這份是**移除 kurtosis 之前**的程式跑出來的，IMPACT_RISE 件數不能代表現況。')
    elif prov['legacy_kurtosis'] is False:
        print('  ✓ IMPACT_RISE 沒有 kurt 通道（已是移除 kurtosis 之後的程式）')

    share, modes = prov['ledger_share'], prov['vel_high_modes']
    if prov['ledger'] == 'yes':
        sig = '、'.join(f'{k} {v}' for k, v in prov['ledger_signals'].items())
        print(f'  ✓ 這輪有帶台帳（跡象：{sig}）')
        if share is not None and share < 1:
            print(f"  ℹ VEL_HIGH 只有 {share * 100:.0f}% 帶 ISO 分類（模式：{modes}）。ISO 錨定的點沒有")
            print('    觸發，剩下的是 ISO 不適用或未分類的點——不是「沒帶台帳」。')
            print('    給 --ledger 可逐點確認原因（額定 ≤ 15 kW？沒填？id 寫法？）。')
    elif prov['ledger'] == 'likely_no':
        print('  ⚠ 找不到任何帶台帳的跡象（machine_class 全空、也沒有 ISO_ZONE／ISO_CLASS_SUSPECT／')
        print(f"    STANDBY_NO_RUNTIME），VEL_HIGH 全走 sigma_fallback（{modes}）：這輪**可能沒有帶台帳**。")
        print('    （也可能帶了台帳、但所有 ISO 錨定的點都沒觸發；給 --ledger 可確認。）')
    if prov['fallback_reasons']:
        print('  VEL_HIGH 退回 sigma 的原因（evidence.iso_fallback_reason）：')
        for k, v in sorted(prov['fallback_reasons'].items(), key=lambda kv: -kv[1]):
            print(f'    {v:3d} 筆  {k}')
    print('  ※ 資料夾名稱（如 after_switch）是資料來源，不是程式版本；以上述內容判斷為準。')

    if ledger_near:
        for lid, did in sorted(ledger_near.items()):
            print(f"  ⚠ 台帳的 {lid!r} 對不上資料裡的 {did!r}：那一筆台帳**沒有生效**，請改成一致。")

    if fallback is not None and not fallback.empty:
        print('\n【VEL_HIGH 為什麼走 sigma_fallback（逐點對照台帳）】')
        for reason, g in fallback.groupby('reason'):
            print(f"  {reason:24s} {len(g):3d} 點 {int(g['episodes'].sum()):4d} 筆 "
                  f"{int(g['days'].sum()):4d} 天  {_FALLBACK_NOTE[reason]}")
            names = []
            for _, r in g.sort_values('device_id').iterrows():
                kw = f"{float(r['rated_power_kw']):g}kW" if pd.notna(r['rated_power_kw']) else '?kW'
                # DataFrame 欄位全是布林時會變成 numpy 布林，`is True` 會落空（見 operating_state.tri）
                sb = '·備機' if (pd.notna(r['is_standby']) and bool(r['is_standby'])) else ''
                names.append(f"{r['device_id']}({kw}{sb})")
            print('      ' + '、'.join(names))

    if not (have_opstate or have_speed):
        print('\n（沒有給 --operating-state / --speed-effect，無法歸因。）')
        return

    print('\n【依規則 × 區段】筆數（事件天數）')
    segs = [s for s in SEGMENTS if (df['segment'] == s).any() or s == 'other']
    print(f'  {"規則":18s} {"合計":>12s}' + ''.join(f'{s[:14]:>20s}' for s in segs))
    for rule in by_rule.index:
        sub = df[df['rule_code'] == rule]
        row = f'  {rule:18s} {len(sub):4d}({int(sub["duration_days"].sum()):4d}天)'
        for s in segs:
            ss = sub[sub['segment'] == s]
            row += f'{len(ss):9d}({int(ss["duration_days"].sum()):4d}天)'
        print(row)

    sp_pts = df.attrs.get('speed_points')
    if have_speed and sp_pts and sp_pts[1]:
        n_sp, n_all = sp_pts
        base = n_sp / n_all
        print(f'\n【轉速敏感設備的告警是否偏多】（轉速敏感 {n_sp} 點 / 共 {n_all} 點 = {base * 100:.0f}%；'
              f'件數 ≥ 8 的規則）')
        print(f'  {"規則":18s} {"件數":>6s} {"在轉速敏感點":>12s} {"佔比":>6s} {"倍數":>6s}')
        on_sp = df['segment'].isin(['speed_explained', 'speed_device_other'])
        for rule in by_rule.index:
            sub = df[df['rule_code'] == rule]
            if len(sub) < 8:
                continue
            k = int(on_sp[sub.index].sum())
            print(f'  {rule:18s} {len(sub):6d} {k:12d} {k / len(sub) * 100:5.0f}% {k / len(sub) / base:5.1f}×')
        print('  （倍數 ≈ 1 表示沒有偏多；明顯 > 1 才表示轉速敏感設備貢獻了額外的告警）')

    vh = df[df['rule_code'] == 'VEL_HIGH']
    if not vh.empty:
        n = len(vh)
        print(f'\n【VEL_HIGH 的歸因】共 {n} 筆、{vh["device_id"].nunique()} 個點、'
              f'{int(vh["duration_days"].sum())} 事件天數')
        for s in segs:
            ss = vh[vh['segment'] == s]
            print(f'  {s:20s} {len(ss):4d} 筆 ({len(ss) / n * 100:4.0f}%)  '
                  f'{ss["device_id"].nunique():3d} 點  {int(ss["duration_days"].sum()):4d} 天  '
                  f'{_SEG_NOTE[s]}')
        explained = vh[vh['segment'].isin(['idle_looks_running', 'speed_explained'])]
        print(f'  → 前兩類合計 {len(explained)} 筆（{len(explained) / n * 100:.0f}%），'
              f'即使這些設備完全正常也會出現。')

        sp = vh[vh['segment'].isin(['speed_explained', 'speed_device_other'])]
        if not sp.empty:
            print('\n  轉速敏感設備的 VEL_HIGH（基準→當前，天數；* ＝由轉速解釋）：')
            for dev, g in sp.sort_values(['device_id', 'episode_start']).groupby('device_id'):
                items = '；'.join(
                    f"{r['episode_start']:%m/%d}({int(r['duration_days'])}天) "
                    f"{r['baseline_value']:.2f}→{r['current_value']:.2f}{'*' if r['explained_by_speed'] else ''}"
                    for _, r in g.iterrows())
                print(f'    {dev:14.14s} {items}')

    print('\n讀法：')
    print('  · idle_looks_running 與 speed_explained 都是「設備沒壞、只是工況或環境讓數字動了」。')
    print('    這個比例就是通用的工況處理（或更可靠的運轉判定）能消掉的告警上限。')
    print('  · speed_device_other 不代表「轉速不影響」，只代表這筆的基準與當前值不符合')
    print('    「在兩個轉速狀態之間移動」的型態（例如只是小幅超標，或比高轉速水準還高出一大截）。')
    print('  · speed_explained 只看幅度，不驗證那段期間 SCADA 頻率真的有變；')
    print('    要驗證用 python -m validate.speed_effect --daily 設備名。')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='告警歸因：停機被環境振動撐過門檻／轉速驅動')
    ap.add_argument('--episodes', required=True, help='回測的 episodes_detail.csv')
    ap.add_argument('--operating-state', help='validate.operating_state 輸出的 CSV')
    ap.add_argument('--speed-effect', help='validate.speed_effect 輸出的 CSV')
    ap.add_argument('--ledger', help='設備台帳 CSV：用來說明 VEL_HIGH 為什麼走 sigma_fallback')
    ap.add_argument('--csv', help='逐筆歸因結果輸出路徑')
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    df = read_episodes(args.episodes)
    opstate = read_csv_flexible(args.operating_state) if args.operating_state else None
    speed = read_csv_flexible(args.speed_effect) if args.speed_effect else None
    for name, d in (('operating-state', opstate), ('speed-effect', speed)):
        if d is not None and 'device_id' in d.columns:
            d['device_id'] = d['device_id'].astype(str).str.strip()
    prov = detect_provenance(df)
    df = attribute(df, opstate, speed)

    fallback, near = None, None
    if args.ledger:
        overrides = load_device_meta_overrides(args.ledger)
        if overrides:
            fallback = explain_fallback(df, overrides)
            near, _ = match_ledger_ids(overrides.keys(), df['device_id'].unique())
        else:
            print(f'⚠ 台帳 {args.ledger} 讀不到任何設備，略過逐點對照。')
    report(df, prov, opstate is not None, speed is not None, fallback, near)
    if args.csv:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)) or '.', exist_ok=True)
        df.drop(columns=[c for c in ('evidence_json',) if c in df.columns]).to_csv(
            args.csv, index=False, encoding='utf-8-sig')
        print(f'\n已輸出：{args.csv}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
