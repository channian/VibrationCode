"""
points.py — 把 Analytic CSV 讀出的「一台設備一份資料」切成「量測點」

`vibcore.io.analytic_reader.load_analytic_dir` 依 `Name` 欄把檔案分組成
「一台設備一個 DataFrame」，這對 Tier 2 檔案讀取已經足夠——但規則層是以
`measure_point`（設備 + 安裝位置）為單位運作的（見 `db/schema.sql`：
`UNIQUE (device_id, position)`），一台設備常常有多個量測點（M1 自由端、
M2 驅動端…）。這一層是規則引擎真正落地前，回測框架自己需要的資料整形，
不屬於 `vibcore.io` 的職責，所以放在 `validate/` 而不是去改
`analytic_reader.py`。

切點邏輯（依可信度由高到低）：
1. 若 `Channel_X/Y/Z` 三軸組合在檔案內有多種 → 以組合當 position
   （同一物理感測點的三軸配線應該固定，組合改變代表換了安裝位置）。
2. 否則整台設備視為單一量測點，position 固定為 `M1`。

**`Label` 欄刻意不使用。** 前端開發時把它拿來存電流 TAG 名稱（例如
`FACCIMTAB.ZONE1_K12_CHS|K12_BF_CHS_PMS_CH01_I_AVG`），與量測位置無關。
早期版本曾用它切點，結果同一台設備被切成兩個假的量測點——有 TAG 的列
歸到一個以 TAG 命名的 position，沒有的列歸到 `M1`，基準與統計因此被
拆散。本改版不使用電流 TAG，此欄一律忽略。

**已知限制**：真實台帳（`measure_point`）由工程師維護、位置命名有意義
（"M1 自由端"）；這裡是資料驅動的猜測，只求回測時「同一物理位置的資料
不要被混在一起算基準與統計」，不保證切出來的 position 名稱與正式台帳
一致。正式串接資料庫後，量測點應直接查 `measure_point` 表，不需要這層。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import pandas as pd

from vibcore.io.analytic_reader import load_analytic_dir
from vibcore.types import DeviceContext

logger = logging.getLogger(__name__)

#: ISO10816_code → iso_machine_class 的推測對照。實測樣本此欄目前多數為 0
#: （未設定，見計畫書 §十二），對照表尚未經工程台帳驗證，僅供回測時若
#: 欄位剛好有值可以利用；若貴司代碼定義不同，用 `--device-meta` 覆寫即可。
_ISO_CODE_MAP = {1: 'I', 2: 'II', 3: 'III', 4: 'IV'}


@dataclass
class PointSeries:
    """一個量測點的完整每秒序列，供聚合管線使用。"""
    device: DeviceContext
    point_id: int
    position: str
    raw: pd.DataFrame                 # 每秒資料（含 datetime 欄），已依時間排序
    source_files: list[str] = field(default_factory=list)


def _build_device_context(device_id: str, meta: dict, overrides: dict | None) -> DeviceContext:
    ov = overrides or {}
    iso_code = meta.get('ISO10816_code')
    try:
        iso_code_int = int(iso_code) if iso_code is not None and not pd.isna(iso_code) else 0
    except (TypeError, ValueError):
        iso_code_int = 0
    guessed_class = _ISO_CODE_MAP.get(iso_code_int)

    rpm = meta.get('RPM')
    fmf = meta.get('FMF')

    ctx = DeviceContext(
        device_id=device_id,
        device_name=str(ov.get('device_name', meta.get('Name', device_id)) or device_id),
        building=str(ov.get('building', meta.get('Building', '')) or ''),
        floor=str(ov.get('floor', meta.get('Floor', '')) or ''),
        system_name=str(ov.get('system_name', meta.get('System', '')) or ''),
        machine_type=str(ov.get('machine_type', '')),
        is_standby=bool(ov.get('is_standby', False)),
        iso_machine_class=ov.get('iso_machine_class', guessed_class),
        iso_class_source=ov.get('iso_class_source', 'unset' if guessed_class is None else 'frontend'),
        rated_rpm=float(rpm) if rpm is not None and not pd.isna(rpm) else None,
        fmf_hz=float(fmf) if fmf is not None and not pd.isna(fmf) else None,
    )
    return ctx


def _position_series(df: pd.DataFrame) -> pd.Series:
    """
    為每一列決定所屬 position 名稱；見模組 docstring 的切點邏輯。

    `Label` 欄刻意不納入判斷——它存的是電流 TAG 名稱而非量測位置。
    """
    chan_cols = [c for c in ('Channel_X', 'Channel_Y', 'Channel_Z') if c in df.columns]
    if len(chan_cols) == 3:
        combo = df[chan_cols].astype('string').agg('-'.join, axis=1)
        if combo.nunique(dropna=True) > 1:
            return 'CH' + combo

    return pd.Series('M1', index=df.index)


def load_device_meta_overrides(path: str | None) -> dict[str, dict]:
    """
    讀取 `--device-meta` JSON：`{device_id: {is_standby, iso_machine_class,
    iso_class_source, machine_type, device_name, ...}}`。

    Analytic CSV 沒有攜帶「是否備機」「機械等級是否已由工程師確認」這類
    台帳資訊（`is_standby` 全部只能猜 False，ISO 分級多數為 0=未設定），
    要回測 `STANDBY_NO_RUNTIME` / `ISO_ZONE` 就需要這份補充資訊。檔案不存在
    時回傳空字典並只記警告，不中斷回測（等於全部設備視為非備機、未分級）。
    """
    if not path:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"設備補充資訊 {path} 不存在，全部設備視為非備機、未分級")
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"設備補充資訊 {path} 讀取失敗（{e}），略過")
        return {}


def load_points(folder: str, pattern: str = '*.csv',
                 device_meta_path: str | None = None) -> list[PointSeries]:
    """
    讀取資料夾內所有 Analytic CSV，切分為量測點清單。

    這是回測框架讀資料的唯一入口——`offline.py` 之後對每個 `PointSeries`
    各自跑「聚合 → 涵蓋率 → 基準期 → 規則」，彼此獨立、互不影響。
    """
    from vibcore.io.analytic_reader import load_analytic_file
    import glob
    import os

    overrides = load_device_meta_overrides(device_meta_path)

    paths = sorted(glob.glob(os.path.join(folder, pattern)))
    if not paths:
        logger.warning(f"{folder} 找不到符合 {pattern} 的檔案")
        return []

    # 逐檔讀取以保留檔名（load_analytic_dir 合併後就無法回溯來源檔案，
    # 缺口清單／debug 時常需要知道某段資料來自哪個檔案）。
    per_device: dict[str, list[tuple[pd.DataFrame, dict, str]]] = {}
    for path in paths:
        try:
            df, meta = load_analytic_file(path)
        except Exception as e:
            logger.error(f"{os.path.basename(path)} 讀取失敗，已跳過：{e}")
            continue
        if df.empty:
            continue
        device_id = str(meta.get('Name', '')).strip() or os.path.splitext(os.path.basename(path))[0]
        per_device.setdefault(device_id, []).append((df, meta, path))

    points: list[PointSeries] = []
    next_point_id = 1
    for device_id, entries in sorted(per_device.items()):
        merged = pd.concat([e[0] for e in entries], ignore_index=True) \
            .sort_values('datetime').reset_index(drop=True)
        meta = entries[0][1]
        source_files = [e[2] for e in entries]
        device_ctx = _build_device_context(device_id, meta, overrides.get(device_id))

        merged = merged.assign(_position=_position_series(merged))
        for position, sub in merged.groupby('_position', sort=True):
            points.append(PointSeries(
                device=device_ctx,
                point_id=next_point_id,
                position=str(position),
                raw=sub.drop(columns=['_position']).reset_index(drop=True),
                source_files=source_files,
            ))
            next_point_id += 1

    logger.info(f"共載入 {len(per_device)} 台設備、切出 {len(points)} 個量測點")
    return points
