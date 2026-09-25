"""
scada.py — SCADA tag 對應與讀值的載入、以及與振動資料的時間對齊

這一層只做「把 SCADA 的值正確地掛到每小時聚合上」，**不做工況分層**。
分層怎麼切（轉速區間？負載區間？幾層？）尚未定案，而且那個決定需要先
回答「velRMS 到底有沒有隨轉速變化」——那是本模組接上之後才有辦法驗的事。
把兩者分開，是為了讓分層的決定可以基於資料，而不是反過來。

## 為什麼需要它

振動資料本身**完全沒有即時轉速**：Analytic CSV 的 `RPM` 是靜態銘牌值
（實測 AHU-601 的 228 筆全部都是 1710），`FMF` 也固定。對變頻設備，
SCADA 的頻率 tag 是唯一能知道實際運轉轉速的來源。

還有一個獨立、CP 值更高的用途：**用電流確定設備是否在運轉**。目前運轉
與否是用 velRMS 門檻猜的，而那個分類是整套涵蓋率統計的分母。

## 兩個必須記住的資料特性

1. **`Label` 欄已經帶著 tag 名稱。** Analytic CSV 的 `Label` 欄存的是
   SCADA tag（實測 `ZP 3-5_M1` → `FACCIMTAB.ZONE1_K12_CHS|K12_BF_CHS_ZP350_INV_I`，
   `_INV_I` 看起來是變頻器電流），但**不是每台都有**（`CP 10_M1` 是空的）。
   所以對應表有一部分是現成的，`emit_tagmap_template` 會把它預填進去，
   工程師只要補沒有的、以及補上頻率 tag。

2. **每 2 分鐘一列，但底層更新週期約 15 分鐘。** 連續數列會是同一個值，
   直到下一次刷新。這代表**有效獨立樣本約每 15 分鐘才一個，不是每 2 分鐘**
   ——任何拿這些值做統計的地方都得按 15 分鐘算，否則樣本數會高估約 7 倍。
   這跟既有的「accKURT 滾動 10 秒窗、相鄰兩筆共用 90% 原始資料」是同一類
   陷阱，`effective_sample_count()` 就是為了不讓它被忘記而存在。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)

#: 允許的變數型別。限定成列舉而不是開放字串——開放會讓同一件事被填成
#: 「電流」「current」「I」「Amp」五種寫法，之後每個讀取端都要各自正規化。
VARIABLE_TYPES: tuple[str, ...] = ('current', 'frequency', 'power')

#: 各型別的建議單位，僅供範本預填與檢查提示，不強制。
DEFAULT_UNITS: dict[str, str] = {
    'current': 'A',
    'frequency': 'Hz',
    'power': 'kW',
}

#: SCADA 值的實際刷新週期（分鐘）。檔案裡約每 2 分鐘一列，但連續數列是
#: 同一個值，真正的更新大約 15 分鐘一次（使用者確認）。
SCADA_REFRESH_MINUTES = 15.0

#: 對齊振動資料時，SCADA 值可以「舊到什麼程度」還算數（分鐘）。
#: 比刷新週期多一些餘裕，但必須有上限——沒有上限的話，SCADA 斷線兩小時
#: 期間的所有振動樣本都會被貼上斷線前那一筆值，然後整段被分到錯的工況，
#: 而且不會有任何錯誤訊息。這與備機旗標被覆寫是同一類的安靜失效。
STALENESS_TOLERANCE_MINUTES = 20.0

#: 對應表的欄位。與 `tag_mapping` 資料表一致。
TAGMAP_COLUMNS: tuple[str, ...] = (
    'device_id', 'tag_id', 'variable_type', 'unit', 'is_active', 'note',
)


@dataclass(frozen=True)
class TagMapping:
    """一台設備的一個 SCADA tag。"""
    tag_id: str
    device_id: str
    variable_type: str
    unit: str | None = None
    is_active: bool = True


def _norm_bool(value, default: bool = True) -> bool:
    """試算表填出來的布林值形形色色；認不得的一律回預設值並記警告。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    text = str(value).strip().lower()
    if text in ('', 'nan'):
        return default
    if text in ('true', 't', 'yes', 'y', '1', 'v', '是'):
        return True
    if text in ('false', 'f', 'no', 'n', '0', 'x', '否'):
        return False
    logger.warning(f"tag 對應表的 is_active 讀到無法判讀的值「{value}」，視為 {default}")
    return default


def parse_tagmap(path: str) -> list[TagMapping]:
    """
    讀 tag 對應 CSV。

    `device_id`、`tag_id`、`variable_type` 三欄缺一不可——少了任一欄這一列
    都無法使用，**安靜跳過比報錯更糟**（現場會以為填好了，但那台設備永遠
    沒有 SCADA 資料，而且查不出原因），所以逐列記警告說明缺什麼。

    `variable_type` 不在 `VARIABLE_TYPES` 內的一律拒收並記警告，不猜測、
    不自動對應——猜錯會讓頻率被當成電流用，那比沒有資料更危險。
    """
    if not path or not os.path.exists(path):
        logger.warning(f"tag 對應表 {path} 不存在，SCADA 相關功能全部略過")
        return []

    for enc in ('utf-8-sig', 'utf-8', 'cp950'):
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str)
            break
        except UnicodeDecodeError:
            continue
    else:
        logger.error(f"tag 對應表 {path} 無法以 utf-8/cp950 讀取，略過")
        return []

    out: list[TagMapping] = []
    n_blank = 0          # 範本裡「還沒填」的列：只有 device_id，其餘全空
    for i, row in enumerate(df.to_dict('records'), start=2):   # 2 = 表頭之後第一列
        def _get(col: str) -> str:
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ''
            text = str(v).strip()
            return '' if text.lower() == 'nan' else text

        device_id, tag_id = _get('device_id'), _get('tag_id')
        vtype = _get('variable_type').lower()

        missing = [c for c, v in (('device_id', device_id), ('tag_id', tag_id),
                                  ('variable_type', vtype)) if not v]
        if missing:
            # 剛產出的範本每台都有兩列待填，逐列喊會把 log 灌爆而且沒有
            # 資訊量——沒有 tag_id 的列視為「還沒填」，最後統一報一個數字。
            # 有 tag_id 卻缺型別的才逐列點名：那種看起來像填好了、實際
            # 不生效，是會讓人白等的狀況。
            if not tag_id:
                # 沒有 tag_id 就是「還沒填」——範本會預先填好 variable_type
                # 當提示，所以不能拿它當「填了一半」的證據。
                n_blank += 1
            else:
                logger.warning(f"tag 對應表第 {i} 列缺少 {missing}，略過該列"
                               f"（device_id={device_id or '(空)'}）")
            continue

        if vtype not in VARIABLE_TYPES:
            logger.warning(
                f"tag 對應表第 {i} 列（{device_id}）的 variable_type「{vtype}」"
                f"不在允許值 {VARIABLE_TYPES} 內，略過該列——不自動猜測對應，"
                f"猜錯會讓頻率被當成電流用")
            continue

        out.append(TagMapping(
            tag_id=tag_id, device_id=device_id, variable_type=vtype,
            unit=_get('unit') or DEFAULT_UNITS.get(vtype),
            is_active=_norm_bool(row.get('is_active')),
        ))

    if n_blank:
        logger.info(f"tag 對應表有 {n_blank} 列尚未填寫（只有 device_id），已略過")

    dupes = pd.Series([m.tag_id for m in out])
    dup_ids = dupes[dupes.duplicated()].unique().tolist()
    if dup_ids:
        logger.warning(f"tag 對應表有重複的 tag_id {dup_ids[:5]}——"
                       f"tag_id 是主鍵，後面的會覆蓋前面的")
    return out


def emit_tagmap_template(labels: dict[str, str | None], path: str) -> int:
    """
    產出 tag 對應表範本，並把 Analytic CSV `Label` 欄已有的 tag 預填進去。

    Args:
        labels: `{device_id: Label 欄的值或 None}`。
        path: 輸出的 CSV 路徑。

    Returns:
        寫出的資料列數。

    每台設備至少一列。`Label` 有值的預填成 `current`（實測的樣本是
    `..._INV_I`，看起來是變頻器電流），但**標在 note 欄請人確認**——
    這是從命名猜的，不是查證過的，猜錯會讓整個分層用到錯的變數。
    沒有 Label 的留白等人填。另外每台各補一列空白的頻率列，因為頻率 tag
    不在 Analytic CSV 裡，一定要人工補。
    """
    rows = []
    for device_id in sorted(labels):
        raw = labels.get(device_id)
        tag = '' if raw is None or (isinstance(raw, float) and pd.isna(raw)) else str(raw).strip()
        rows.append({
            'device_id': device_id,
            'tag_id': tag,
            'variable_type': 'current' if tag else '',
            'unit': DEFAULT_UNITS['current'] if tag else '',
            'is_active': 'TRUE' if tag else '',
            'note': '由 Analytic CSV 的 Label 欄預填，請確認確實是電流' if tag else '',
        })
        rows.append({
            'device_id': device_id, 'tag_id': '', 'variable_type': 'frequency',
            'unit': DEFAULT_UNITS['frequency'], 'is_active': '',
            'note': '頻率 tag 不在 Analytic CSV 裡，需人工填',
        })

    out = pd.DataFrame(rows, columns=list(TAGMAP_COLUMNS))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    out.to_csv(path, index=False, encoding='utf-8-sig')   # BOM：現場用 Excel 開
    return len(out)


def parse_readings(path: str) -> pd.DataFrame:
    """
    讀 SCADA 讀值 CSV，欄位為 `tag_id, ts, value`。

    Returns:
        欄位 `tag_id`（str）、`ts`（tz-aware UTC）、`value`（float）的
        DataFrame，依 `(tag_id, ts)` 排序並去重。解析不了的列直接丟掉並
        統計筆數——讀值檔動輒數十萬列，逐列記警告只會把 log 灌爆。
    """
    if not path or not os.path.exists(path):
        logger.warning(f"SCADA 讀值檔 {path} 不存在")
        return pd.DataFrame(columns=['tag_id', 'ts', 'value'])

    for enc in ('utf-8-sig', 'utf-8', 'cp950'):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        logger.error(f"SCADA 讀值檔 {path} 無法以 utf-8/cp950 讀取")
        return pd.DataFrame(columns=['tag_id', 'ts', 'value'])

    missing = [c for c in ('tag_id', 'ts', 'value') if c not in df.columns]
    if missing:
        logger.error(f"SCADA 讀值檔 {path} 缺少欄位 {missing}"
                     f"（實際欄位：{list(df.columns)}）")
        return pd.DataFrame(columns=['tag_id', 'ts', 'value'])

    n_raw = len(df)
    out = pd.DataFrame({
        'tag_id': df['tag_id'].astype(str).str.strip(),
        'ts': pd.to_datetime(df['ts'], errors='coerce', utc=True),
        'value': pd.to_numeric(df['value'], errors='coerce'),
    }).dropna(subset=['tag_id', 'ts', 'value'])
    out = out[out['tag_id'] != '']

    n_dropped = n_raw - len(out)
    if n_dropped:
        logger.warning(f"SCADA 讀值檔 {path}：{n_raw} 列中有 {n_dropped} 列"
                       f"因時間或數值無法解析而略過")

    before = len(out)
    out = (out.sort_values(['tag_id', 'ts'])
           .drop_duplicates(subset=['tag_id', 'ts'], keep='last')
           .reset_index(drop=True))
    if len(out) < before:
        logger.info(f"SCADA 讀值檔 {path}：去除 {before - len(out)} 筆重複的 (tag_id, ts)")
    return out


def effective_sample_count(n_rows: int, span_minutes: float,
                           refresh_minutes: float = SCADA_REFRESH_MINUTES) -> int:
    """
    把「列數」換算成「有效獨立樣本數」。

    SCADA 檔案約每 2 分鐘一列，但值大約每 15 分鐘才真的更新一次，連續數列
    是同一個值。直接拿列數當樣本數會高估約 7 倍，任何以它為分母的統計
    （標準差、信心度、分層後每層夠不夠樣本）都會跟著失真。

    取「時間跨度能容納幾個刷新週期」與「實際列數」的較小者——資料稀疏時
    不該因為公式而虛報，連續完整時也不該因為列數多而灌水。
    """
    if n_rows <= 0 or span_minutes <= 0 or refresh_minutes <= 0:
        return 0
    return max(1, min(n_rows, int(span_minutes // refresh_minutes)))


def attach_to_agg(agg: pd.DataFrame, readings: pd.DataFrame,
                  variable_type: str,
                  tolerance_minutes: float = STALENESS_TOLERANCE_MINUTES) -> pd.DataFrame:
    """
    把某一型別的 SCADA 讀值對齊到每小時聚合上。

    對每個 `ts_hour`，取**該小時之內**的讀值算中位數；該小時完全沒有讀值
    時，往回找最近一筆、但不得超過 `tolerance_minutes`。

    **為什麼要有容忍上限**：沒有上限的話，SCADA 斷線兩小時期間的每一個
    振動樣本都會被貼上斷線前那一筆值，然後整段被分到錯的工況——而且不會
    有任何錯誤訊息。寧可標成「工況未知」（NaN），也不要硬貼一個過期值。

    **為什麼用中位數而不是平均**：一小時內若剛好跨越啟停，平均會落在
    兩個實際狀態之間的無人地帶；中位數會落在其中一邊。

    Args:
        agg: 含 `ts_hour` 的每小時聚合。
        readings: `parse_readings` 的輸出（已過濾成單一 tag 或同型別多 tag）。
        variable_type: 產生的欄位名為 `scada_{variable_type}`，
            另加 `scada_{variable_type}_n`（該小時的有效獨立樣本數）
            與 `scada_{variable_type}_stale_min`（值比該小時舊幾分鐘）。

    Returns:
        `agg` 的副本，多出上述三欄。`agg` 為空或沒有可用讀值時，
        欄位仍會建立但全為 NaN——**下游要分得出「沒有這個欄位」與
        「有欄位但沒有值」**，前者是程式沒接上，後者是這段時間真的沒資料。
    """
    col = f'scada_{variable_type}'
    out = agg.copy() if agg is not None else pd.DataFrame()
    for suffix in ('', '_n', '_stale_min'):
        out[f'{col}{suffix}'] = pd.NA

    if out.empty or 'ts_hour' not in out.columns or readings is None or readings.empty:
        return out

    ts_hour = pd.to_datetime(out['ts_hour'], errors='coerce', utc=True)
    r = readings.dropna(subset=['ts', 'value']).sort_values('ts')
    if r.empty:
        return out

    # 該小時內的讀值：以整點對齊分組，一次算完所有小時
    binned = r.assign(_h=r['ts'].dt.floor('h'))
    per_hour = binned.groupby('_h').agg(
        v=('value', 'median'),
        n_rows=('value', 'size'),
        t_min=('ts', 'min'),
        t_max=('ts', 'max'),
    )

    values, counts, stales = [], [], []
    for t in ts_hour:
        if pd.isna(t):
            values.append(pd.NA); counts.append(pd.NA); stales.append(pd.NA)
            continue
        if t in per_hour.index:
            row = per_hour.loc[t]
            span = (row['t_max'] - row['t_min']).total_seconds() / 60.0
            values.append(float(row['v']))
            counts.append(effective_sample_count(int(row['n_rows']), max(span, 60.0)))
            stales.append(0.0)
            continue
        # 該小時沒有讀值：往回找最近一筆，但不得超過容忍窗
        prior = r[r['ts'] <= t + pd.Timedelta(hours=1)]
        if prior.empty:
            values.append(pd.NA); counts.append(pd.NA); stales.append(pd.NA)
            continue
        last = prior.iloc[-1]
        stale_min = (t - last['ts']).total_seconds() / 60.0
        if stale_min > tolerance_minutes:
            values.append(pd.NA); counts.append(pd.NA); stales.append(pd.NA)
        else:
            values.append(float(last['value']))
            counts.append(0)          # 這一小時沒有自己的樣本，只是沿用前一筆
            stales.append(round(max(stale_min, 0.0), 1))

    out[col] = values
    out[f'{col}_n'] = counts
    out[f'{col}_stale_min'] = stales
    return out
