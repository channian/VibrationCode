#!/usr/bin/env python3
"""
review_sheet.py — 讓現場人員用試算表回饋「這件告警對不對」，並算出每條規則的雜訊率

**為什麼需要**

規則門檻沒有外部標準可引用時，唯一能讓它「站得住腳」的是現場回饋：這一件到底是不是
真的？Dashboard 還沒有，簽核鏈（Finding）又只涵蓋會派工的那一小部分，大部分輸出是
觀察名單（不進簽核）。這支工具用一張試算表補上這條回饋迴路：

    export  把要看的項目匯成 CSV，空白的「判定」欄交給現場人員填
    score   讀回填好的 CSV，每條規則算：真實／已知／雜訊／不確定各幾件、雜訊率，
            並對「該調門檻、該維持觀察、可評估升級」給出**建議**（只是建議）

兩種來源，同一張表格式：

    # 試跑前的預演：拿 11 週回測的 episodes 給工程師看（他們記得 8 月中 CP 08 發生過什麼）
    python -m validate.review_sheet export --episodes out/validation/episodes_detail.csv \\
        --ledger ledger.csv --out review_rehearsal.csv

    # 試跑期間：每週從資料庫匯出本期的 Finding 與觀察名單
    python -m validate.review_sheet export --db --days 7 --out review_2026W35.csv

    # 回收後統計
    python -m validate.review_sheet score --sheets review_rehearsal.csv review_2026W35.csv

**抽樣**：每條規則取前 K 筆（`--per-rule`，預設 8），依持續天數與變化幅度排序，同一台設備
在同一條規則最多 2 筆——要讓每條規則都被看到，又不讓一個人看幾百行。事件很少的規則全部列出。

**判定欄的值**（中英文都收，空白＝還沒看）：

    真實    這個狀況是真的，值得注意（不論有沒有處置）
    已知    確實有這個狀況，但現場早就知道（例如已排程維修）——系統講對了，只是沒有新資訊
    雜訊    不是設備的問題（轉速／負載變了、鄰機帶動、備機、感測器、通訊中斷…）
    不確定  看不出來，需要再查

「原因」欄建議以這些詞開頭，統計時會歸類：轉速負載、鄰機、備機停機、維修換件、感測器、
通訊中斷、真實劣化、其他。

**怎麼讀統計**：雜訊率 ＝ 雜訊 ÷（真實＋已知＋雜訊）；「不確定」不算進分母，另外列出。
樣本少時雜訊率會晃得很厲害，所以同時列出 80% 信賴區間，且件數 < 10 時一律標「樣本不足」。
建議只是啟發式：真正的調整仍要看原因欄寫了什麼。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

SHEET_COLUMNS = [
    'item_id', 'item_type', 'rule_code', 'rule_name', 'category', 'severity', 'device_id',
    'first_seen', 'last_seen', 'duration_days', 'baseline', 'current', 'delta_pct', 'unit',
    'summary', 'context',
    '判定', '原因', '處置', '備註', '填寫人',
]
VERDICT_COL, CAUSE_COL = '判定', '原因'

#: 判定欄接受的寫法 → 內部代碼
VERDICT_WORDS = {
    'real': {'真實', '真', 'real', 'y', 'yes', '是', '有'},
    'known': {'已知', 'known', '已處理', '早就知道'},
    'noise': {'雜訊', '誤報', '假', 'noise', 'false', 'fp', 'n', 'no', '否', '不是'},
    'unsure': {'不確定', 'unsure', '?', '？', '不知道', '待查'},
}
VERDICT_LABEL = {'real': '真實', 'known': '已知', 'noise': '雜訊', 'unsure': '不確定',
                 'unreviewed': '未看'}
CAUSE_TAGS = ('轉速負載', '鄰機', '備機停機', '維修換件', '感測器', '通訊中斷', '真實劣化', '其他')

#: 樣本數低於此值一律標「樣本不足」，不給調整建議
MIN_DECIDED = 10
#: 建議用的雜訊率界線
NOISE_HIGH = 0.5
REAL_HIGH = 0.5


def normalize_verdict(raw) -> str:
    """判定欄的原始文字 → real／known／noise／unsure／unreviewed；認不得的文字回傳 'invalid'。"""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return 'unreviewed'
    text = str(raw).strip().lower()
    if text in ('', 'nan'):
        return 'unreviewed'
    for code, words in VERDICT_WORDS.items():
        if text in {w.lower() for w in words}:
            return code
    return 'invalid'


def cause_tag(raw) -> str | None:
    """原因欄文字 → 標準詞（取文字中最先出現的標準詞）；沒有則 None。"""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw)
    hits = [(text.find(t), t) for t in CAUSE_TAGS if t in text]
    return min(hits)[1] if hits else None


# ── 統計 ──────────────────────────────────────────────────

def wilson_interval(k: int, n: int, z: float = 1.2816) -> tuple[float, float]:
    """比例 k/n 的 Wilson 信賴區間（預設 z=1.2816 ＝ 80%）。n=0 時回 (0, 1)。"""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def score(sheets: pd.DataFrame) -> pd.DataFrame:
    """
    每條規則一列：看了幾件、真實／已知／雜訊／不確定、雜訊率與其 80% 區間、建議。

    `sheets` 是一張或多張填好的表格直接串起來。同一個 `item_id` 出現多次（例如兩人都填了）
    時取最後一筆——重複計算會讓雜訊率看起來比實際更有把握。
    """
    df = sheets.copy()
    df['_v'] = df[VERDICT_COL].map(normalize_verdict) if VERDICT_COL in df.columns else 'unreviewed'
    if 'item_id' in df.columns:
        df = df.drop_duplicates('item_id', keep='last')
    rows = []
    for rule, g in df.groupby('rule_code'):
        c = g['_v'].value_counts()
        real, known, noise, unsure = (int(c.get(k, 0)) for k in ('real', 'known', 'noise', 'unsure'))
        decided = real + known + noise
        lo, hi = wilson_interval(noise, decided)
        sev = g['severity'].mode().iat[0] if 'severity' in g.columns and g['severity'].notna().any() else ''
        causes = g[g['_v'] == 'noise'][CAUSE_COL].map(cause_tag).dropna().value_counts() \
            if CAUSE_COL in g.columns else pd.Series(dtype=int)
        rows.append(dict(
            rule_code=rule, severity=sev, items=len(g), unreviewed=int(c.get('unreviewed', 0)),
            invalid=int(c.get('invalid', 0)), real=real, known=known, noise=noise, unsure=unsure,
            decided=decided,
            noise_rate=(noise / decided) if decided else float('nan'),
            noise_lo=lo if decided else float('nan'), noise_hi=hi if decided else float('nan'),
            top_noise_causes='、'.join(f'{k}×{v}' for k, v in causes.head(3).items()),
            suggestion=suggest(rule, sev, real, known, noise, decided),
        ))
    out = pd.DataFrame(rows)
    return out.sort_values(['decided', 'items'], ascending=False).reset_index(drop=True) if len(out) else out


def suggest(rule: str, severity: str, real: int, known: int, noise: int, decided: int) -> str:
    """
    啟發式建議，**不是決定**。樣本少時只說樣本不足——小樣本的比例沒有資訊量。
    """
    if decided < MIN_DECIDED:
        return f'樣本不足（已判 {decided} 件，至少 {MIN_DECIDED}）'
    nr, rr = noise / decided, (real + known) / decided
    if nr >= NOISE_HIGH:
        if severity == 'observe':
            return '雜訊偏多：維持觀察名單；看原因欄找出可排除的類型，或調高門檻／加下限'
        return '雜訊偏多：不該派工——先改成觀察名單，或調高門檻／加下限'
    if rr >= REAL_HIGH and nr < 0.25:
        if severity == 'observe':
            return '多數為真、雜訊低：可評估升為 warn（派工）；先確認「真實」不都是「已知」'
        return '多數為真、雜訊低：維持'
    return '真假參半：看原因欄，若雜訊集中在某一類（如鄰機、轉速）就針對那一類處理'


# ── 匯出 ──────────────────────────────────────────────────

def _rule_names() -> dict[str, str]:
    try:
        from validate.rule_defaults import DEFAULT_RULE_CONFIGS
        return {k: v.rule_name for k, v in DEFAULT_RULE_CONFIGS.items()}
    except Exception:                               # noqa: BLE001 - 名稱只是方便閱讀
        return {}


def _num(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def build_summary(rule_name: str, base, cur, unit: str, days) -> str:
    """給現場人員看的一句話：數值怎麼變、持續多久；沒有數值的事件類就只講規則名與天數。"""
    b, c = _num(base), _num(cur)
    d = _num(days)
    dur = f'，持續 {int(d)} 天' if d else ''
    if b is not None and c is not None and b != 0:
        return f'{rule_name}：{b:.3g} → {c:.3g} {unit}（{(c / b - 1) * 100:+.0f}%）{dur}'.replace(' ,', ',')
    if c is not None:
        return f'{rule_name}：{c:.3g} {unit}{dur}'
    return f'{rule_name}{dur}'


def select_items(df: pd.DataFrame, per_rule: int, max_per_device: int = 2) -> pd.DataFrame:
    """
    每條規則取前 per_rule 筆（持續天數、變化幅度大的優先），同一設備在同一規則最多 max_per_device 筆。
    事件少於 per_rule 的規則全部列出。
    """
    parts = []
    for rule, g in df.groupby('rule_code'):
        g = g.assign(_mag=g['delta_pct'].abs().fillna(0))
        g = g.sort_values(['duration_days', '_mag'], ascending=False)
        g = g.groupby('device_id', sort=False).head(max_per_device).head(per_rule)
        parts.append(g.drop(columns='_mag'))
    return pd.concat(parts) if parts else df.iloc[0:0]


def from_episodes(path: str, ledger: str | None = None, per_rule: int = 8) -> pd.DataFrame:
    """回測 episodes_detail.csv → 試算表列。可帶 `--ledger` 加上「台帳備機／額定功率」的脈絡欄。"""
    from validate.alarm_attribution import read_episodes
    df = read_episodes(path)
    names = _rule_names()
    ctx = {}
    if ledger:
        from vibcore.io.ledger import read_ledger_csv
        for did, row in read_ledger_csv(ledger).items():
            bits = []
            if row.get('is_standby') is True:
                bits.append('台帳標備機')
            if row.get('rated_power_kw') is not None:
                bits.append(f"{row['rated_power_kw']:g} kW")
            ctx[did] = '、'.join(bits)
    out = pd.DataFrame({
        'item_id': [f"{r.rule_code}|{r.device_id}|{r.episode_start:%Y-%m-%d}"
                    for r in df.itertuples()],
        'item_type': 'episode',
        'rule_code': df['rule_code'],
        'rule_name': df['rule_code'].map(lambda c: names.get(c, df.loc[df['rule_code'] == c, 'rule_name'].iat[0]
                                                            if 'rule_name' in df.columns else c)),
        'category': df['category'] if 'category' in df.columns else '',
        'severity': df['severity'] if 'severity' in df.columns else '',
        'device_id': df['device_id'],
        'first_seen': df['episode_start'].dt.strftime('%Y-%m-%d'),
        'last_seen': df['episode_end'].dt.strftime('%Y-%m-%d'),
        'duration_days': df['duration_days'],
        'baseline': df['baseline_value'], 'current': df['current_value'],
        'unit': df['value_unit'].fillna('') if 'value_unit' in df.columns else '',
    })
    out['delta_pct'] = np.where(out['baseline'].abs() > 0, (out['current'] / out['baseline'] - 1) * 100, np.nan)
    out['context'] = out['device_id'].map(ctx).fillna('') if ctx else ''
    out['summary'] = [build_summary(r.rule_name, r.baseline, r.current, r.unit, r.duration_days)
                      for r in out.itertuples()]
    return finish_sheet(select_items(out, per_rule))


def from_db(conn, days: int = 7) -> pd.DataFrame:
    """資料庫 → 試算表列：未結案的 Finding，以及最近 `days` 天內仍出現的觀察名單。"""
    names = _rule_names()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 'finding' AS item_type, finding_key AS item_id, rule_code, severity, device_id,
                   first_seen_at, last_seen_at, baseline_value, current_value, value_unit, occurrence_count
              FROM finding
             WHERE status NOT IN ('closed','auto_resolved','false_positive')
                OR last_seen_at >= now() - make_interval(days => %s)
            UNION ALL
            SELECT 'observation', observation_key, rule_code, 'observe', device_id,
                   first_seen_at, last_seen_at, baseline_value, current_value, value_unit, occurrence_count
              FROM observation
             WHERE last_seen_at >= now() - make_interval(days => %s)
        """, (days, days))
        rows = cur.fetchall()
    df = pd.DataFrame(rows)
    if df.empty:
        return finish_sheet(pd.DataFrame(columns=SHEET_COLUMNS))
    out = pd.DataFrame({
        'item_id': df['item_id'], 'item_type': df['item_type'], 'rule_code': df['rule_code'],
        'rule_name': df['rule_code'].map(lambda c: names.get(c, c)),
        'category': '', 'severity': df['severity'], 'device_id': df['device_id'],
        'first_seen': pd.to_datetime(df['first_seen_at']).dt.strftime('%Y-%m-%d'),
        'last_seen': pd.to_datetime(df['last_seen_at']).dt.strftime('%Y-%m-%d'),
        'duration_days': (pd.to_datetime(df['last_seen_at']) - pd.to_datetime(df['first_seen_at'])).dt.days + 1,
        'baseline': pd.to_numeric(df['baseline_value'], errors='coerce'),
        'current': pd.to_numeric(df['current_value'], errors='coerce'),
        'unit': df['value_unit'].fillna(''),
    })
    out['delta_pct'] = np.where(out['baseline'].abs() > 0, (out['current'] / out['baseline'] - 1) * 100, np.nan)
    out['context'] = ''
    out['summary'] = [build_summary(r.rule_name, r.baseline, r.current, r.unit, r.duration_days)
                      for r in out.itertuples()]
    return finish_sheet(out)


def finish_sheet(df: pd.DataFrame) -> pd.DataFrame:
    for c in SHEET_COLUMNS:
        if c not in df.columns:
            df[c] = ''
    df = df[SHEET_COLUMNS].copy()
    return df.sort_values(['rule_code', 'first_seen'], kind='stable').reset_index(drop=True)


GUIDE = """\
填寫說明
========
每一列是系統認為「值得注意」的一件事。請在「判定」欄填其中一個字：

  真實    這個狀況是真的，值得注意（不論有沒有處置）
  已知    確實有這個狀況，但現場早就知道（例如已排程維修）
  雜訊    不是設備的問題（轉速／負載變了、鄰機帶動、備機、感測器、通訊中斷…）
  不確定  看不出來，需要再查

「原因」欄請以這些詞開頭再補充：轉速負載、鄰機、備機停機、維修換件、感測器、通訊中斷、真實劣化、其他。
「處置」欄：有做什麼就寫；沒有就空白。 不確定的不要硬猜，填「不確定」就好。

回饋用途：統計每條規則的雜訊率，決定哪些規則該調門檻、哪些可以升為派工。
你的判定不會被拿來考核任何人。
"""


def write_sheet(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    df.to_csv(path, index=False, encoding='utf-8-sig')
    guide = os.path.splitext(path)[0] + '_填寫說明.txt'
    with open(guide, 'w', encoding='utf-8-sig') as f:
        f.write(GUIDE)


# ── 讀回與輸出 ────────────────────────────────────────────

def read_sheets(paths: list[str]) -> pd.DataFrame:
    frames = []
    for p in paths:
        last = None
        for enc in ('utf-8-sig', 'utf-8', 'cp950'):
            try:
                frames.append(pd.read_csv(p, encoding=enc, dtype=str, keep_default_na=False))
                break
            except UnicodeDecodeError as e:
                last = e
        else:
            raise RuntimeError(f'{p} 無法讀取：{last}')
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    miss = {'rule_code', VERDICT_COL} - set(df.columns)
    if miss:
        raise ValueError(f'表格缺少欄位 {sorted(miss)}；請用 export 產出的格式（不要改欄名）')
    return df


def print_score(res: pd.DataFrame, n_invalid_words: dict[str, int] | None = None) -> None:
    print('\n' + '=' * 78 + '\n現場回饋統計：每條規則的雜訊率\n' + '=' * 78)
    if res.empty:
        print('沒有可統計的列。')
        return
    tot = res[['items', 'real', 'known', 'noise', 'unsure', 'unreviewed']].sum()
    print(f"共 {int(tot['items'])} 件：真實 {int(tot['real'])}、已知 {int(tot['known'])}、"
          f"雜訊 {int(tot['noise'])}、不確定 {int(tot['unsure'])}、未看 {int(tot['unreviewed'])}")
    if res['invalid'].sum():
        print(f"⚠ {int(res['invalid'].sum())} 列的「判定」寫法認不得（請填 真實／已知／雜訊／不確定），未計入")
    print(f'\n  {"規則":20s} {"嚴重度":6s} {"件數":>5s} {"已判":>5s} {"真實":>5s} {"已知":>5s} '
          f'{"雜訊":>5s} {"不確定":>6s} {"雜訊率":>7s} {"80%區間":>12s}')
    for r in res.itertuples():
        rate = f'{r.noise_rate * 100:5.0f}%' if r.decided else '    -'
        ci = f'{r.noise_lo * 100:3.0f}–{r.noise_hi * 100:3.0f}%' if r.decided else '          -'
        print(f'  {r.rule_code:20s} {r.severity:6s} {r.items:5d} {r.decided:5d} {r.real:5d} {r.known:5d} '
              f'{r.noise:5d} {r.unsure:6d} {rate:>7s} {ci:>12s}')
        print(f'      → {r.suggestion}' + (f'  ｜雜訊原因：{r.top_noise_causes}' if r.top_noise_causes else ''))
    print('\n讀法：雜訊率 ＝ 雜訊 ÷（真實＋已知＋雜訊），不確定不計入分母。建議只是啟發式，'
          '真正要調什麼請看「原因」欄寫了什麼。')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='現場回饋試算表：匯出與統計')
    sub = ap.add_subparsers(dest='cmd', required=True)

    ex = sub.add_parser('export', help='匯出要請現場人員看的項目')
    src = ex.add_mutually_exclusive_group(required=True)
    src.add_argument('--episodes', help='回測的 episodes_detail.csv（試跑前預演）')
    src.add_argument('--db', action='store_true', help='從資料庫匯出（試跑期間，每週一次）')
    ex.add_argument('--ledger', help='台帳 CSV：給了會在「脈絡」欄標出備機與額定功率（僅 --episodes）')
    ex.add_argument('--per-rule', type=int, default=8, help='每條規則最多幾筆（僅 --episodes），預設 8')
    ex.add_argument('--days', type=int, default=7, help='資料庫來源：回看幾天，預設 7')
    ex.add_argument('--out', required=True, help='輸出 CSV 路徑')

    sc = sub.add_parser('score', help='讀回填好的表格，統計每條規則的雜訊率')
    sc.add_argument('--sheets', nargs='+', required=True, help='填好的 CSV（可多份）')
    sc.add_argument('--csv', help='統計結果輸出路徑')

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')

    if args.cmd == 'export':
        if args.episodes:
            sheet = from_episodes(args.episodes, args.ledger, args.per_rule)
        else:
            from vibcore.db.connection import get_connection
            with get_connection() as conn:
                sheet = from_db(conn, args.days)
        write_sheet(sheet, args.out)
        by_rule = sheet['rule_code'].value_counts()
        print(f'已輸出 {len(sheet)} 列 → {args.out}')
        print('  ' + '、'.join(f'{k} {v}' for k, v in by_rule.items()))
        print(f'填寫說明：{os.path.splitext(args.out)[0]}_填寫說明.txt')
        print('請在「判定」欄填 真實／已知／雜訊／不確定，其餘欄位選填；填完後用 score 統計。')
        return 0

    sheets = read_sheets(args.sheets)
    res = score(sheets)
    print_score(res)
    if args.csv and not res.empty:
        res.to_csv(args.csv, index=False, encoding='utf-8-sig')
        print(f'\n已輸出：{args.csv}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
