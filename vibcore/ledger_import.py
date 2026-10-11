#!/usr/bin/env python3
"""
ledger_import.py — 把設備台帳 CSV 匯入資料庫的 device 表

**為什麼需要這支**

台帳（ISO 群組、基礎剛性、額定功率、是否備機）是這些資料的唯一來源——Analytic CSV
裡沒有。離線回測讀 CSV 直接套用；但正式的每日排程（`vibcore.pipeline.daily`）只讀
資料庫的 `device` 表。過去這兩條路之間沒有橋：上線只能逐台手寫 UPDATE，而且文件裡的
範例欄位（`iso_machine_class`）早已不存在。

**台帳沒進資料庫的後果是安靜的**：所有設備停留在「未分級」，`ISO_ZONE` 永遠 0 次、
`VEL_HIGH` 全部退回相對基準、`STANDBY_NO_RUNTIME` 完全不判定——沒有任何錯誤訊息，
看起來只是「這段時間設備都很健康」。這支工具的存在就是為了不讓這件事發生。

用法：

    python -m vibcore.ledger_import --csv ledger.csv --dry-run     # 先看會改什麼
    python -m vibcore.ledger_import --csv ledger.csv               # 套用
    python -m vibcore.ledger_import --csv ledger.csv --create-missing   # 讓台帳裡還沒有資料的設備也先建立

連線沿用 VIB_DB_HOST / VIB_DB_PORT / VIB_DB_NAME / VIB_DB_USER / VIB_DB_PASSWORD。
重跑是安全的（冪等）。

**行為**

· 只更新台帳管轄的欄位：額定功率、ISO 群組、基礎剛性、驅動型式、是否備機、最後保養日、
  機型。設備名稱、樓層等來自 Analytic CSV 的欄位不動——每日排程會維護它們，而且它們的
  upsert 對台帳欄位是 COALESCE 保護的，兩者互不覆蓋。
· **留空 ＝ 不動。** 不會把資料庫已有的值清成 NULL；明確填值才覆蓋。
· 群組與基礎剛性（合併資料庫既有值後）兩者齊備 → `iso_class_source` 設為
  `manual_override`。**少了這一步，分類填了也不會生效**（`iso.evaluate_iso` 看到
  `unset` 就整個不套用 Zone 判定，而且沒有錯誤）。
· 值不合法的列整列略過並說明；其餘列照常套用。`--strict` 則任何一列有問題就整批不套用。
· 台帳 id 在資料庫找不到：只差空白／底線／大小寫的近似名稱（例如 `CH01_M1` 對
  `CH 01_M1`）**只報告、不建立**——自動建立會憑空多出一台幽靈設備，真正那台仍然沒有台帳。
  只有 `--create-missing` 才會建立設備，且資料庫已有近似名稱時仍拒絕。
· 全程單一交易，要嘛全部成功要嘛全部不算數。
· 結束時印出覆蓋摘要：多少台的 ISO 判定可啟用、多少台額定 ≤ 15 kW（不在 ISO 20816-3
  適用範圍，**這不是填錯**）、備機台數、資料庫有但台帳沒有的設備。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field

import pandas as pd

from vibcore.io.ledger import match_ledger_ids, read_ledger_csv
from vibcore.metrics.iso import ISO_FOUNDATIONS, ISO_GROUPS, ISO_MIN_RATED_POWER_KW

logger = logging.getLogger(__name__)

#: 台帳管轄、會寫進 `device` 表的欄位。
LEDGER_DEVICE_FIELDS = ('rated_power_kw', 'iso_machine_group', 'iso_foundation',
                        'iso_driver_type', 'is_standby', 'last_maintenance_at', 'machine_type')

_FOUNDATION_WORDS = {'rigid': 'rigid', '剛性': 'rigid', 'flexible': 'flexible', '柔性': 'flexible'}
_DRIVER_WORDS = {'integrated': 'integrated', '整合': 'integrated',
                 'external': 'external', '外接': 'external'}
assert set(_FOUNDATION_WORDS.values()) == set(ISO_FOUNDATIONS)


# ── 欄位正規化：回傳 (值, 錯誤訊息) ───────────────────────

def normalize_group(raw) -> tuple[str | None, str | None]:
    """'2'、2、'2.0'（試算表常把整數存成小數）都接受；只認 1～4。"""
    text = str(raw).strip()
    if text == '' or text.lower() == 'nan':
        return None, None
    m = re.fullmatch(r'(\d+)(?:\.0+)?', text)
    if m and m.group(1) in ISO_GROUPS:
        return m.group(1), None
    return None, f"iso_machine_group「{raw}」不是 1～4"


def normalize_foundation(raw) -> tuple[str | None, str | None]:
    text = str(raw).strip().lower()
    if text == '' or text == 'nan':
        return None, None
    if text in _FOUNDATION_WORDS:
        return _FOUNDATION_WORDS[text], None
    return None, f"iso_foundation「{raw}」不是 rigid／flexible（剛性／柔性）"


def normalize_driver(raw) -> tuple[str | None, str | None]:
    text = str(raw).strip().lower()
    if text == '' or text == 'nan':
        return None, None
    if text in _DRIVER_WORDS:
        return _DRIVER_WORDS[text], None
    return None, f"iso_driver_type「{raw}」不是 integrated／external（整合／外接）"


def normalize_date(raw) -> tuple[str | None, str | None]:
    text = str(raw).strip()
    if text == '' or text.lower() in ('nan', 'nat', 'none'):
        return None, None
    ts = pd.to_datetime(text, errors='coerce')
    if pd.isna(ts):
        return None, f"last_maintenance_at「{raw}」不是日期"
    return ts.strftime('%Y-%m-%d'), None


def normalize_power(raw) -> tuple[float | None, str | None]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None, f"rated_power_kw「{raw}」不是數字"
    if v <= 0:
        return None, f"rated_power_kw「{raw}」必須大於 0"
    return v, None


# ── 單列的合併規劃 ────────────────────────────────────────

@dataclass
class RowPlan:
    device_id: str
    new: dict = field(default_factory=dict)         # 合併後的欄位值（含 iso_class_source）
    changes: dict = field(default_factory=dict)     # {欄位: (舊值, 新值)}，只列有變的
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def plan_row(device_id: str, ledger: dict, current: dict | None) -> RowPlan:
    """
    把一列台帳與資料庫現值合併。`current` 為 None 表示設備尚未存在（`--create-missing`），
    以全空白為基準。留空的欄位保留現值；任何欄位不合法就整列標錯、不套用。
    """
    cur = dict(current or {})
    plan = RowPlan(device_id=device_id)
    problems: list[str] = []

    incoming: dict = {}
    for fld, norm in (('iso_machine_group', normalize_group),
                      ('iso_foundation', normalize_foundation),
                      ('iso_driver_type', normalize_driver),
                      ('last_maintenance_at', normalize_date)):
        if fld in ledger:
            val, err = norm(ledger[fld])
            if err:
                problems.append(err)
            elif val is not None:
                incoming[fld] = val
    if 'rated_power_kw' in ledger:
        val, err = normalize_power(ledger['rated_power_kw'])
        if err:
            problems.append(err)
        elif val is not None:
            incoming['rated_power_kw'] = val
    if ledger.get('is_standby') is not None:        # 台帳讀取時已轉成 True/False/None
        incoming['is_standby'] = bool(ledger['is_standby'])
    mt = str(ledger.get('machine_type') or '').strip()
    if mt and mt.lower() != 'nan':
        incoming['machine_type'] = mt

    if problems:
        plan.error = '；'.join(problems)
        return plan

    merged = {f: cur.get(f) for f in LEDGER_DEVICE_FIELDS}
    merged.update(incoming)

    # 分類：台帳有給群組或基礎剛性、且合併後兩者齊備 → 人工填寫的分類，必須標 manual_override，
    # 否則 evaluate_iso 看到 unset 會整個不套用（分類填了卻沒有任何效果，也沒有錯誤）。
    source = cur.get('iso_class_source') or 'unset'
    contributes = 'iso_machine_group' in incoming or 'iso_foundation' in incoming
    if contributes and merged['iso_machine_group'] and merged['iso_foundation']:
        source = 'manual_override'
    merged['iso_class_source'] = source

    for f in (*LEDGER_DEVICE_FIELDS, 'iso_class_source'):
        old, new = cur.get(f), merged.get(f)
        if f == 'rated_power_kw' and old is not None and new is not None:
            if abs(float(old) - float(new)) < 1e-9:
                continue
        elif f == 'last_maintenance_at' and old is not None and new is not None:
            if str(pd.Timestamp(old).date()) == str(new)[:10]:
                continue
        if old != new:
            plan.changes[f] = (old, new)
    plan.new = merged
    return plan


# ── 覆蓋摘要 ──────────────────────────────────────────────

def machine_of(device_id: str) -> str:
    """M1／M2 是同一台機器的兩個量測點；摘要以機器為單位列名稱。"""
    return re.sub(r'_M\d+$', '', str(device_id))


def coverage(final: dict[str, dict]) -> dict:
    """
    依匯入後的狀態統計：哪些設備的 ISO 判定會啟用、哪些不會、為什麼。

    「額定 ≤ 15 kW」單獨一類：分類填了也不套用，是標準的適用範圍，不是台帳沒填好。
    額定功率未填的設備無法檢查適用範圍（`iso_scope_reason` 視為通過），另外計數。
    """
    out = {k: [] for k in ('iso_ready', 'out_of_scope', 'partial', 'unclassified',
                           'power_missing', 'standby')}
    for did, v in sorted(final.items()):
        grp, fnd = v.get('iso_machine_group'), v.get('iso_foundation')
        kw = v.get('rated_power_kw')
        if v.get('is_standby'):
            out['standby'].append(did)
        if kw is None:
            out['power_missing'].append(did)
        if grp and fnd:
            if kw is not None and float(kw) <= ISO_MIN_RATED_POWER_KW:
                out['out_of_scope'].append(did)
            elif (v.get('iso_class_source') or 'unset') == 'unset':
                out['partial'].append(did)          # 分類齊了但來源仍是 unset：不會生效
            else:
                out['iso_ready'].append(did)
        elif grp or fnd:
            out['partial'].append(did)
        else:
            out['unclassified'].append(did)
    return out


# ── 資料庫存取 ────────────────────────────────────────────

_SELECT_COLS = ('device_id', *LEDGER_DEVICE_FIELDS, 'iso_class_source')


def fetch_devices(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_SELECT_COLS)} FROM device")
        rows = cur.fetchall()
    return {r['device_id']: dict(r) for r in rows}


def apply_plan(conn, plan: RowPlan, create: bool = False) -> None:
    n = plan.new
    with conn.cursor() as cur:
        if create:
            cur.execute(
                """
                INSERT INTO device (device_id, device_name, machine_type, rated_power_kw,
                                    iso_machine_group, iso_foundation, iso_driver_type,
                                    is_standby, last_maintenance_at, iso_class_source)
                VALUES (%(device_id)s, %(device_id)s, %(machine_type)s, %(rated_power_kw)s,
                        %(iso_machine_group)s, %(iso_foundation)s, %(iso_driver_type)s,
                        COALESCE(%(is_standby)s, FALSE), %(last_maintenance_at)s,
                        %(iso_class_source)s)
                """,
                {**n, 'device_id': plan.device_id})
        else:
            cur.execute(
                """
                UPDATE device SET
                    machine_type        = %(machine_type)s,
                    rated_power_kw      = %(rated_power_kw)s,
                    iso_machine_group   = %(iso_machine_group)s,
                    iso_foundation      = %(iso_foundation)s,
                    iso_driver_type     = %(iso_driver_type)s,
                    is_standby          = COALESCE(%(is_standby)s, is_standby),
                    last_maintenance_at = %(last_maintenance_at)s,
                    iso_class_source    = %(iso_class_source)s,
                    updated_at          = now()
                WHERE device_id = %(device_id)s
                """,
                {**n, 'device_id': plan.device_id})


# ── 主流程 ────────────────────────────────────────────────

@dataclass
class ImportReport:
    updated: list = field(default_factory=list)       # [RowPlan]
    unchanged: list = field(default_factory=list)     # [device_id]
    created: list = field(default_factory=list)       # [RowPlan]
    skipped: list = field(default_factory=list)       # [(device_id, 原因)]
    near_miss: dict = field(default_factory=dict)     # {台帳 id: 資料庫 id}
    missing: list = field(default_factory=list)       # 台帳有、資料庫完全找不到
    not_in_ledger: list = field(default_factory=list) # 資料庫有、台帳沒有
    cover: dict = field(default_factory=dict)
    applied: bool = False

    @property
    def n_issues(self) -> int:
        """需要人處理的問題數：不合法的列、疑似寫法不同的 id。

        `missing`（台帳有、資料庫完全沒有相近名稱）不計：台帳通常比實際有資料的設備多
        （96 台對 65 台），那些設備只是還沒被每日排程建立，不是錯。"""
        return len(self.skipped) + len(self.near_miss)


def run_import(conn, ledger: dict[str, dict], dry_run: bool = False,
               create_missing: bool = False, strict: bool = False) -> ImportReport:
    """
    把 `ledger`（`read_ledger_csv` 的輸出）合併進資料庫。`dry_run` 時不寫入。
    呼叫端負責交易邊界（`get_connection()` 結束時 commit）。
    """
    rep = ImportReport()
    db = fetch_devices(conn)
    near, missing = match_ledger_ids(ledger.keys(), db.keys())
    rep.near_miss = near
    # 近似名稱一律只報告。其餘「完全找不到」依 create_missing 決定要不要建立。
    to_create = [m for m in missing] if create_missing else []
    rep.missing = [m for m in missing if m not in to_create]

    plans: list[tuple[RowPlan, bool]] = []
    for did, row in sorted(ledger.items()):
        if did in near:
            continue
        if did in db:
            plan = plan_row(did, row, db[did])
            create = False
        elif did in to_create:
            plan = plan_row(did, row, None)
            create = True
        else:
            continue
        if plan.error:
            rep.skipped.append((did, plan.error))
            continue
        plans.append((plan, create))

    if strict and rep.n_issues:
        rep.cover = {}
        return rep

    final = {k: dict(v) for k, v in db.items()}
    for plan, create in plans:
        if create:
            rep.created.append(plan)
        elif plan.changed:
            rep.updated.append(plan)
        else:
            rep.unchanged.append(plan.device_id)
        if create or plan.changed:
            if not dry_run:
                apply_plan(conn, plan, create=create)
            final[plan.device_id] = {**final.get(plan.device_id, {}), **plan.new}
    rep.applied = not dry_run
    rep.not_in_ledger = sorted(d for d in db if d not in ledger and d not in near.values())
    rep.cover = coverage(final)
    return rep


# ── 輸出 ──────────────────────────────────────────────────

_FIELD_LABEL = {
    'rated_power_kw': '額定功率', 'iso_machine_group': 'ISO 群組', 'iso_foundation': '基礎剛性',
    'iso_driver_type': '驅動型式', 'is_standby': '備機', 'last_maintenance_at': '最後保養',
    'machine_type': '機型', 'iso_class_source': '分類來源',
}


def _fmt(v) -> str:
    return '（空）' if v is None else str(v)[:10] if hasattr(v, 'year') else str(v)


def _names(ids: list[str], limit: int = 12) -> str:
    machines = sorted({machine_of(i) for i in ids})
    shown = '、'.join(machines[:limit])
    return shown + (f' …共 {len(machines)} 台' if len(machines) > limit else '')


def print_report(rep: ImportReport, dry_run: bool, strict: bool = False) -> None:
    head = '台帳匯入（預演，沒有寫入資料庫）' if dry_run else '台帳匯入'
    print('\n' + '=' * 78 + f'\n{head}\n' + '=' * 78)
    if strict and rep.n_issues:
        print(f'⚠ --strict：有 {rep.n_issues} 個問題，整批未套用。請先處理下列項目。')
    else:
        verb = '將更新' if dry_run else '已更新'
        print(f'{verb} {len(rep.updated)} 台、新建 {len(rep.created)} 台、'
              f'已是最新 {len(rep.unchanged)} 台')

    if dry_run:
        for plan in (rep.created + rep.updated)[:40]:
            tag = '新建' if plan in rep.created else '更新'
            diffs = '；'.join(f'{_FIELD_LABEL.get(f, f)} {_fmt(o)} → {_fmt(n)}'
                              for f, (o, n) in plan.changes.items())
            print(f'  [{tag}] {plan.device_id}：{diffs}')
        if len(rep.created) + len(rep.updated) > 40:
            print(f'  …另有 {len(rep.created) + len(rep.updated) - 40} 台')

    if rep.near_miss:
        print('\n⚠ 台帳 id 與資料庫的設備只差空白／底線／大小寫——**這幾筆台帳沒有套用**：')
        for lid, did in sorted(rep.near_miss.items()):
            print(f'    台帳 {lid!r} ≈ 資料庫 {did!r}  → 請把台帳的 device_id 改成與資料庫一致')
    if rep.missing:
        print(f'\nℹ 台帳有 {len(rep.missing)} 筆在資料庫找不到對應設備（未套用）：'
              f'{"、".join(sorted(rep.missing)[:10])}{" …" if len(rep.missing) > 10 else ""}')
        print('    台帳的設備數多於實際有資料的設備數時，這是正常的（那些設備還沒被每日排程建立）。')
        print('    建議順序：先跑一次每日排程（依資料建立設備），再匯入台帳。只有想讓「還沒有資料'
              '的設備」也先進資料庫時才加 --create-missing；')
        print('    若其中有應該有資料的設備，多半是 device_id 寫法不同——請對照上面的近似名稱。')
    if rep.skipped:
        print(f'\n⚠ 以下 {len(rep.skipped)} 列有不合法的值，整列略過：')
        for did, why in rep.skipped[:20]:
            print(f'    {did}：{why}')

    if rep.cover:
        c = rep.cover
        print('\n【覆蓋摘要】（匯入後的狀態）')
        print(f'  ISO 判定會啟用        {len(c["iso_ready"]):3d} 個量測點')
        print(f'  分類齊全但額定 ≤ 15 kW {len(c["out_of_scope"]):3d} 個量測點'
              f'  ← 標準不適用（不是填錯）：{_names(c["out_of_scope"])}')
        if c['partial']:
            print(f'  分類不完整／來源未設   {len(c["partial"]):3d} 個量測點  ← 不會啟用 ISO 判定：'
                  f'{_names(c["partial"])}')
        print(f'  完全未分類            {len(c["unclassified"]):3d} 個量測點'
              + (f'  ← {_names(c["unclassified"])}' if c['unclassified'] else ''))
        if c['power_missing']:
            print(f'  額定功率沒填          {len(c["power_missing"]):3d} 個量測點  ← 無法檢查適用範圍：'
                  f'{_names(c["power_missing"])}')
        print(f'  標記為備機            {len(c["standby"]):3d} 個量測點')
        if rep.not_in_ledger:
            print(f'  資料庫有、台帳沒有    {len(rep.not_in_ledger):3d} 個量測點  ← '
                  f'{_names(rep.not_in_ledger)}')
    if not dry_run and rep.applied and not rep.n_issues:
        print('\n完成。下一次每日排程起，ISO 判定與備機旗標以資料庫為準。')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='把設備台帳 CSV 匯入資料庫的 device 表')
    ap.add_argument('--csv', required=True, help='台帳 CSV（validate.iso_readiness --emit-ledger 的格式）')
    ap.add_argument('--dry-run', action='store_true', help='只印出會改什麼，不寫入')
    ap.add_argument('--create-missing', action='store_true',
                    help='資料庫還沒有的設備一併建立。一般不需要：建議先跑一次每日排程讓設備依資料建立，'
                         '再匯入台帳；近似名稱存在時仍拒絕')
    ap.add_argument('--strict', action='store_true', help='任何一列有問題就整批不套用')
    ap.add_argument('--log-level', default='WARNING', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = ap.parse_args(argv)

    logging.basicConfig(level=args.log_level, format='%(levelname)s %(message)s')
    ledger = read_ledger_csv(args.csv)
    if not ledger:
        print(f'⚠ {args.csv} 讀不到任何設備列（需要 device_id 欄位），沒有做任何事。')
        return 2

    from vibcore.db.connection import get_connection
    with get_connection() as conn:
        rep = run_import(conn, ledger, dry_run=args.dry_run,
                         create_missing=args.create_missing, strict=args.strict)
        if args.strict and rep.n_issues:
            conn.rollback()
    print_report(rep, args.dry_run, args.strict)
    return 1 if rep.n_issues else 0


if __name__ == '__main__':
    sys.exit(main())
