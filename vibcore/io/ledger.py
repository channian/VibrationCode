"""
ledger.py — 設備台帳 CSV 的讀取與設備名稱比對

台帳（`validate.iso_readiness --emit-ledger` 產出範本、工程師填寫）是這個系統
的 ISO 分級、備機旗標、額定功率的**唯一來源**——Analytic CSV 裡沒有這些資料。
兩條路徑都要讀它：

· 離線回測（`validate.points.load_points`）：讀成 `{device_id: {欄位: 值}}`
  的 override 字典。
· 正式資料庫（`vibcore.ledger_import`）：寫進 `device` 表。

放在 `vibcore.io` 而不是 `validate/`，因為正式路徑不該依賴離線分析工具包；
`validate.points` 仍以原本的名稱匯出這些函式（`_ledger_bool`、
`_load_device_meta_csv`、`match_ledger_ids`），既有呼叫端與測試不受影響。
"""

from __future__ import annotations

import logging
import re

import pandas as pd

logger = logging.getLogger(__name__)

#: 台帳 CSV 的布林欄位可能出現的寫法。試算表填出來的值形形色色，
#: 全部轉小寫後比對；不在表內的值一律視為「沒填」而非 False——
#: 「沒填」與「確認不是備機」在寫回台帳時的意義不同（見 DeviceContext）。
TRUE_WORDS = {'true', 't', 'yes', 'y', '1', 'v', '是', '備機'}
FALSE_WORDS = {'false', 'f', 'no', 'n', '0', 'x', '否', '主機'}

#: 台帳 CSV 中會被讀進來的欄位；其餘欄位（範本裡的參考欄與分隔欄）忽略。
LEDGER_FIELDS = ('device_name', 'machine_type', 'rated_power_kw',
                  'iso_machine_group', 'iso_foundation', 'iso_driver_type',
                  'iso_class_source', 'is_standby', 'last_maintenance_at',
                  'building', 'floor', 'system_name')


def ledger_bool(value) -> bool | None:
    """把台帳 CSV 的布林欄轉成三態；認不得的字串記警告並視為沒填。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().lower()
    if text == '' or text == 'nan':
        return None
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    logger.warning(f"台帳的 is_standby 欄位讀到無法判讀的值「{value}」，視為未填")
    return None


def read_ledger_csv(path: str) -> dict[str, dict]:
    """
    讀取 CSV 版台帳（`validate.iso_readiness --emit-ledger` 產出的格式）。

    **為什麼要收 CSV 而不只是 JSON**：這份資料要由工程師填 68 台，逐台
    手寫 JSON 物件既慢又容易漏逗號，而且錯一個字整份就讀不進去。CSV 可以
    在試算表裡填、排序、整批複製同型號的值。JSON 仍然支援，兩種格式讀出
    來的結構完全相同。

    空字串一律視為「沒填」而不是「填了空值」——留空的欄位不該覆蓋台帳裡
    既有的值。
    """
    for enc in ('utf-8-sig', 'utf-8', 'cp950'):
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str)
            break
        except UnicodeDecodeError:
            continue
    else:
        logger.error(f"台帳 {path} 無法以 utf-8/cp950 讀取，略過")
        return {}

    if 'device_id' not in df.columns:
        logger.error(f"台帳 {path} 缺少 device_id 欄位（實際欄位：{list(df.columns)}），略過")
        return {}

    out: dict[str, dict] = {}
    for row in df.to_dict('records'):
        device_id = str(row.get('device_id') or '').strip()
        if not device_id:
            continue
        ov: dict = {}
        for field in LEDGER_FIELDS:
            if field not in row:
                continue
            raw = row[field]
            if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                continue
            text = str(raw).strip()
            if text == '' or text.lower() == 'nan':
                continue
            if field == 'is_standby':
                parsed = ledger_bool(text)
                if parsed is not None:
                    ov[field] = parsed
            elif field == 'rated_power_kw':
                try:
                    ov[field] = float(text)
                except ValueError:
                    logger.warning(f"{device_id} 的 rated_power_kw「{text}」不是數字，略過該欄")
            else:
                ov[field] = text
        if ov:
            out[device_id] = ov
    return out


def name_key(name: str) -> str:
    """比對用的鍵：去掉空白、底線、連字號並轉小寫。只用來找「看起來是同一台」的近似名稱。"""
    return re.sub(r'[\s_\-]+', '', str(name)).lower()


def match_ledger_ids(ledger_ids, data_ids) -> tuple[dict[str, str], list[str]]:
    """
    台帳的 device_id 與資料裡的設備名稱做比對。

    Returns:
        `(近似對上, 完全沒對上)`。
        · 近似對上：`{台帳 id: 資料 id}`——只差空白／底線／連字號／大小寫，
          幾乎可以確定是同一台，但**精確比對不會套用**（見下）。
        · 完全沒對上：台帳 id 清單；資料裡也找不到相近名稱。

    **為什麼要有這個檢查**：台帳是用 `overrides.get(device_id)` 精確比對的，
    寫成 `CH01_M1` 而資料是 `CH 01_M1`，那一筆台帳（ISO 群組、基礎剛性、
    是否備機）整筆不會生效，**而且不會有任何錯誤**——該設備只是安靜地
    退回「未分類」，VEL_HIGH 走 sigma_fallback，備機旗標變成未知。
    實測就發生在 CH01 上。這裡只報告、不自動套用近似名稱：猜錯會讓台帳
    套到另一台設備上，那比沒套更危險。
    """
    data = [str(d) for d in data_ids]
    exact = set(data)
    by_key: dict[str, list[str]] = {}
    for d in data:
        by_key.setdefault(name_key(d), []).append(d)

    near: dict[str, str] = {}
    missing: list[str] = []
    for lid in ledger_ids:
        lid = str(lid)
        if lid in exact:
            continue
        cands = by_key.get(name_key(lid), [])
        if len(cands) == 1:
            near[lid] = cands[0]
        else:
            missing.append(lid)
    return near, missing
