"""
points.py — 把 Analytic CSV 讀出的「一台設備一份資料」切成「量測點」

`vibcore.io.analytic_reader.load_analytic_dir` 依 `Name` 欄把檔案分組成
「一台設備一個 DataFrame」，這對 Tier 2 檔案讀取已經足夠——但規則層是以
`measure_point`（設備 + 安裝位置）為單位運作的（見 `db/schema.sql`：
`UNIQUE (device_id, position)`），一台設備常常有多個量測點（M1 自由端、
M2 驅動端…）。這一層是規則引擎真正落地前，回測框架自己需要的資料整形，
不屬於 `vibcore.io` 的職責，所以放在 `validate/` 而不是去改
`analytic_reader.py`。

切點邏輯：

**整台設備視為單一量測點，position 固定為 `M1`。**

早期版本曾用 `Channel_X/Y/Z` 的組合當切點依據，假設「組合改變代表換了
安裝位置」。**該假設已由使用者確認為錯（2026-09）**：那三個數值是固定的
方向代碼（4=垂直徑向、5=軸向、6=水平徑向），同一台設備的兩個量測點
（馬達驅動端／非驅動端）用的是同一組 4/5/6，組合完全相同。

也就是說那條規則永遠不會生效——生效了反而是錯的（會把方向差異當成
位置差異）。已移除，避免日後有人看到程式碼又以為它能切點。

真正的量測點區分在 `Name` 欄（實測樣本為 `ZP 3-5_M1`、`CP 10_M1` 這種
帶後綴的命名），而 `Name` 在本框架是設備識別碼，因此兩個量測點目前會被
當成兩台獨立設備處理。回測用途上不影響結論（同一物理位置的資料仍不會
被混在一起算基準），但正式串接資料庫後應直接查 `measure_point` 表。

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
from vibcore.metrics.iso import ISO_FOUNDATIONS, ISO_GROUPS
from vibcore.types import DeviceContext

logger = logging.getLogger(__name__)

#: `ISO10816_code` → ISO 10816-3 機器群組的對照。
#:
#: **這是一個未經驗證的假設，預設不啟用。** 兩個理由：
#:
#: 1. 這個欄位的語意沒人確認過。它可能是 ISO 2372 的 Class I~IV（舊分類），
#:    也可能是 ISO 10816-3 的 Group 1~4，兩者的數值範圍剛好都是 1~4。
#:    已知的矛盾線索：ZP 3-5 與 CP 10 都是泵、`ISO10816_code` 都填 2，
#:    但 ISO 10816-3 的泵浦屬 Group 3/4，Group 2 是「中型機／馬達」。
#:    要嘛工程師填的是舊的 Class II，要嘛是依驅動馬達而非依泵本身分類。
#: 2. **就算群組確定了，Zone 判定還缺基礎剛性**（rigid/flexible）——
#:    同一群組下兩者的 A/B 界可差近一倍，前端資料完全沒有這個欄位。
#:
#: 因此預設一律視為未分類，走相對基準與趨勢路徑。要做「若假設是 X 會
#: 如何」的敏感度分析，用 `--assume-iso GROUP/FOUNDATION` 明確指定，
#: 或用 `--device-meta` 逐台指定 `iso_machine_group` / `iso_foundation`。
_ISO_CODE_TO_GROUP = {1: '1', 2: '2', 3: '3', 4: '4'}


@dataclass
class PointSeries:
    """一個量測點的完整每秒序列，供聚合管線使用。"""
    device: DeviceContext
    point_id: int
    position: str
    raw: pd.DataFrame                 # 每秒資料（含 datetime 欄），已依時間排序
    source_files: list[str] = field(default_factory=list)


def _build_device_context(device_id: str, meta: dict, overrides: dict | None,
                          assume_iso: tuple[str, str] | None = None) -> DeviceContext:
    """
    組出單一設備的 `DeviceContext`。

    ISO 分類的來源優先序（由高到低）：

      1. `--device-meta` 逐台指定的 `iso_machine_group` / `iso_foundation`
      2. `--assume-iso` 給的全域假設（供敏感度分析，見 `_ISO_CODE_TO_GROUP`）
      3. 不分類——**不從 `ISO10816_code` 猜**，理由見 `_ISO_CODE_TO_GROUP`

    `assume_iso` 的群組只在 `ISO10816_code` 有值時才套用，讓「台帳有填」
    與「台帳空白」兩種設備在敏感度分析裡仍然分得開；基礎剛性則一律套用
    （前端本來就沒有這個欄位，不套就沒有任何設備能算 Zone）。
    """
    ov = overrides or {}
    iso_code = meta.get('ISO10816_code')
    try:
        iso_code_int = int(iso_code) if iso_code is not None and not pd.isna(iso_code) else 0
    except (TypeError, ValueError):
        iso_code_int = 0

    group = ov.get('iso_machine_group')
    foundation = ov.get('iso_foundation')

    # 假設一律成對套用。只給基礎剛性而沒有群組（或反之）算不出 Zone，
    # 卻會讓台帳看起來「填了一半」——那種半套用狀態沒有任何用處，
    # 只會在除錯時誤導人以為分類生效了。
    from_assumption = False
    if assume_iso is not None and group is None and foundation is None \
            and iso_code_int in _ISO_CODE_TO_GROUP:
        group, foundation = assume_iso
        from_assumption = True

    classified = group is not None and foundation is not None

    rpm = meta.get('RPM')
    fmf = meta.get('FMF')
    power = meta.get('rated_power_kw', ov.get('rated_power_kw'))

    ctx = DeviceContext(
        device_id=device_id,
        device_name=str(ov.get('device_name', meta.get('Name', device_id)) or device_id),
        building=str(ov.get('building', meta.get('Building', '')) or ''),
        floor=str(ov.get('floor', meta.get('Floor', '')) or ''),
        system_name=str(ov.get('system_name', meta.get('System', '')) or ''),
        machine_type=str(ov.get('machine_type', '')),
        # 三態：台帳沒填就給 None（「不知道」），不要一律代 False——
        # False 是「已確認不是備機」，兩者在寫回台帳時的意義不同。
        is_standby=(None if ov.get('is_standby') is None
                    else bool(ov.get('is_standby'))),
        iso_machine_group=group,
        iso_foundation=foundation,
        iso_driver_type=ov.get('iso_driver_type'),
        iso_class_source=ov.get('iso_class_source',
                                'manual_override' if classified else 'unset'),
        rated_power_kw=float(power) if power is not None and not pd.isna(power) else None,
        rated_rpm=float(rpm) if rpm is not None and not pd.isna(rpm) else None,
        fmf_hz=float(fmf) if fmf is not None and not pd.isna(fmf) else None,
    )
    # 供呼叫端統計「假設實際套到幾台」——與「總共幾台已分類」是不同的數字，
    # 後者含 --device-meta 明確指定的設備，會蓋住假設完全沒生效的事實。
    ctx._from_iso_assumption = from_assumption   # type: ignore[attr-defined]
    return ctx


def _position_series(df: pd.DataFrame) -> pd.Series:
    """
    為每一列決定所屬 position 名稱；見模組 docstring 的切點邏輯。

    目前一律回傳 `M1`——`Channel_X/Y/Z` 是方向代碼而非位置代碼，不能拿來
    切點（見模組 docstring）；`Label` 欄存的是電流 TAG 名稱，也不是量測位置。
    """
    return pd.Series('M1', index=df.index)


#: 台帳 CSV 的布林欄位可能出現的寫法。試算表填出來的值形形色色，
#: 全部轉小寫後比對；不在表內的值一律視為「沒填」而非 False——
#: 「沒填」與「確認不是備機」在寫回台帳時的意義不同（見 DeviceContext）。
_TRUE_WORDS = {'true', 't', 'yes', 'y', '1', 'v', '是', '備機'}
_FALSE_WORDS = {'false', 'f', 'no', 'n', '0', 'x', '否', '主機'}

#: 台帳 CSV 中會被讀進來的欄位；其餘欄位（範本裡的參考欄與分隔欄）忽略。
_LEDGER_FIELDS = ('device_name', 'machine_type', 'rated_power_kw',
                  'iso_machine_group', 'iso_foundation', 'iso_driver_type',
                  'iso_class_source', 'is_standby', 'last_maintenance_at',
                  'building', 'floor', 'system_name')


def _ledger_bool(value) -> bool | None:
    """把台帳 CSV 的布林欄轉成三態；認不得的字串記警告並視為沒填。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().lower()
    if text == '' or text == 'nan':
        return None
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    logger.warning(f"台帳的 is_standby 欄位讀到無法判讀的值「{value}」，視為未填")
    return None


def _load_device_meta_csv(path: str) -> dict[str, dict]:
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
        for field in _LEDGER_FIELDS:
            if field not in row:
                continue
            raw = row[field]
            if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                continue
            text = str(raw).strip()
            if text == '' or text.lower() == 'nan':
                continue
            if field == 'is_standby':
                parsed = _ledger_bool(text)
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


def load_device_meta_overrides(path: str | None) -> dict[str, dict]:
    """
    讀取 `--device-meta`：`{device_id: {is_standby, iso_machine_group,
    iso_foundation, rated_power_kw, ...}}`。副檔名為 `.csv` 時走 CSV 解析，
    其餘一律當 JSON。

    Analytic CSV 沒有攜帶「是否備機」「機器群組」「基礎剛性」這類台帳
    資訊（ISO 分級多數為 0=未設定），要回測 `STANDBY_NO_RUNTIME` /
    `ISO_ZONE` 就需要這份補充資訊。檔案不存在時回傳空字典並只記警告，
    不中斷回測（等於全部設備未分級、備機狀態未知）。

    產生範本：`python -m validate.iso_readiness --data-dir data/
    --emit-ledger out/ledger.csv`。
    """
    if not path:
        return {}
    try:
        if path.lower().endswith('.csv'):
            return _load_device_meta_csv(path)
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"設備補充資訊 {path} 不存在，全部設備視為未分級、備機狀態未知")
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"設備補充資訊 {path} 讀取失敗（{e}），略過")
        return {}


def trim_points(points: list[PointSeries],
                since: pd.Timestamp | None = None,
                until: pd.Timestamp | None = None,
                latest_cadence_only: bool = False) -> list[PointSeries]:
    """
    裁掉不想納入回測的資料區間。

    `latest_cadence_only` 是為「匯出檔混雜前端版本」設計的：只保留每個
    量測點最近一段連續同密度的資料。**刻意逐點各自裁切，而不是統一切一個
    日期**——各設備換版時間不同（實測同一批資料裡有 5/18、6/4、3/31 等
    多個切換點），統一日期會把早就換好版的點的好資料一起丟掉。

    為什麼值得裁：混雜密度會同時造成三件事——換版空窗被算成感測器離線、
    涵蓋率分母被撐大、以及基準期偏誤（每小時樣本數多的區段變異天生較小，
    會贏得「最穩定窗口」的選擇，再拿去比對低密度資料就整段看起來在偏離）。
    基準期偏誤已在 `vibcore.metrics.baseline` 內擋掉，但前兩者只能靠裁切。

    Returns:
        裁切後的量測點清單；裁到沒有資料的點會被剔除並記錄。
    """
    from vibcore.pipeline.aggregate import detect_cadence_segments

    out: list[PointSeries] = []
    dropped: list[str] = []
    for p in points:
        df = p.raw
        before = len(df)
        if since is not None:
            df = df[df['datetime'] >= since]
        if until is not None:
            df = df[df['datetime'] <= until]

        if latest_cadence_only and not df.empty:
            seg = detect_cadence_segments(df)
            if len(seg) > 1:
                last = seg.iloc[-1]
                cut = pd.Timestamp(last['start_day'])
                df = df[df['datetime'] >= cut]
                logger.info(
                    f"  {p.device.device_id}/{p.position}：混雜 {len(seg)} 種取樣密度，"
                    f"只保留 {cut:%Y-%m-%d} 起的 {int(last['samples_per_hour'])} 筆/小時區段"
                )

        if df.empty:
            dropped.append(f"{p.device.device_id}/{p.position}")
            continue
        if len(df) != before:
            p = PointSeries(device=p.device, point_id=p.point_id, position=p.position,
                            raw=df.reset_index(drop=True), source_files=p.source_files)
        out.append(p)

    if dropped:
        logger.warning(f"裁切後無資料而剔除的量測點（{len(dropped)} 個）："
                       + "、".join(dropped[:10])
                       + (f" …另有 {len(dropped) - 10} 個" if len(dropped) > 10 else ""))
    return out


def parse_iso_assumption(text: str | None) -> tuple[str, str] | None:
    """
    解析 `--assume-iso` 的 `GROUP/FOUNDATION` 字串，例如 `3/rigid`。

    刻意要求同時給群組與基礎剛性——只給其中一個算不出 Zone，
    讓使用者以為設定生效了卻毫無作用是最糟的失敗模式。
    """
    if not text:
        return None
    parts = str(text).strip().split('/')
    if len(parts) != 2:
        raise ValueError(f"--assume-iso 格式應為 GROUP/FOUNDATION（例如 3/rigid），收到：{text!r}")
    group, foundation = parts[0].strip(), parts[1].strip().lower()
    if group not in ISO_GROUPS:
        raise ValueError(f"群組須為 {'/'.join(ISO_GROUPS)} 之一，收到：{group!r}")
    if foundation not in ISO_FOUNDATIONS:
        raise ValueError(f"基礎剛性須為 {'/'.join(ISO_FOUNDATIONS)} 之一，收到：{foundation!r}")
    return group, foundation


def load_points(folder: str, pattern: str = '*.csv',
                 device_meta_path: str | None = None,
                 assume_iso: tuple[str, str] | None = None) -> list[PointSeries]:
    """
    讀取資料夾內所有 Analytic CSV，切分為量測點清單。

    這是回測框架讀資料的唯一入口——`offline.py` 之後對每個 `PointSeries`
    各自跑「聚合 → 涵蓋率 → 基準期 → 規則」，彼此獨立、互不影響。

    `assume_iso` 供 ISO 分類的敏感度分析：前端資料沒有基礎剛性欄位、
    `ISO10816_code` 的語意也未經確認，所以預設所有設備都是未分類。
    要回答「若這些設備其實是 Group 3 剛性基礎，告警量會變多少」這種問題，
    就用這個參數跑多次再比較（見 `_build_device_context`）。
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
    n_classified = 0
    n_from_assumption = 0
    for device_id, entries in sorted(per_device.items()):
        merged = pd.concat([e[0] for e in entries], ignore_index=True) \
            .sort_values('datetime').reset_index(drop=True)
        meta = entries[0][1]
        source_files = [e[2] for e in entries]
        device_ctx = _build_device_context(device_id, meta, overrides.get(device_id),
                                            assume_iso=assume_iso)
        if device_ctx.iso_machine_group and device_ctx.iso_foundation:
            n_classified += 1
        if getattr(device_ctx, '_from_iso_assumption', False):
            n_from_assumption += 1

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

    logger.info(f"共載入 {len(per_device)} 台設備、切出 {len(points)} 個量測點；"
                f"其中 {n_classified} 台有完整的 ISO 分類（群組＋基礎剛性）"
                + (f"，{n_from_assumption} 台來自 --assume-iso "
                   f"{'/'.join(assume_iso)}" if assume_iso else ""))

    # 給了假設卻一台都沒套上，代表這批資料的 ISO10816_code 全是 0 或空白。
    # 不講清楚的話，敏感度分析會「跑完、沒報錯、結果完全沒變」——
    # 使用者只會以為分類不影響結果，而真相是假設根本沒生效。
    if assume_iso is not None and n_from_assumption == 0:
        logger.warning(
            f"--assume-iso {'/'.join(assume_iso)} 沒有套用到任何設備！"
            "假設只會套用在 ISO10816_code 有值（1~4）的設備上，而這批資料"
            "全部為 0 或空白。ISO_ZONE 不會觸發、VEL_HIGH 會走 sigma_fallback，"
            "本次結果與不給假設完全相同。若要強制指定，請改用 --device-meta 逐台設定。"
        )
    return points
