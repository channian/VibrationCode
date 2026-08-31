"""
repository.py — 資料存取函式

所有 SQL 集中於此，理由見套件 docstring。這裡再補三個貫穿全檔的設計決定：

1. **函式只接收已開啟的 `conn`，不自己 commit/rollback/close。**
   交易邊界屬於呼叫端（見 `connection.get_connection`）。多個函式常
   需要在同一個交易內合併呼叫（例如 `transition_status` 同時動
   `finding` 與 `finding_status_history`），把 commit 權下放給這裡
   會讓那種合併變得不可能。

2. **Finding 相關操作一律先 `SELECT ... FOR UPDATE` 鎖列再判斷分支。**
   規則引擎是排程觸發，理論上不會同一秒有兩個行程對同一個
   `finding_key` 同時 upsert，但一旦真的並發（例如手動重跑 + 排程重疊），
   不鎖列會讓 `occurrence_count += 1` 兩次讀到同一個舊值、其中一次更新
   被覆蓋掉——這種 race 很難在測試中重現，但成本是後續統計失真，所以
   一開始就把鎖加上。

3. **DataFrame → DB 一律用 `execute_values` 批次寫入，不逐列 `execute`。**
   一次聚合工作可能一次要寫入數千個小時 × 上百個量測點，逐列
   `execute` 在網路往返上的開銷會讓匯入時間從秒級變成分鐘級。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

import numpy as np
import pandas as pd
import psycopg2.extensions
from psycopg2.extras import Json, execute_values

from vibcore.config import AGG_SPEC, AXIS_IMPACT_COLS
from vibcore.types import (
    CLOSED_STATUSES,
    FINDING_AUTO_RESOLVED,
    FINDING_FALSE_POSITIVE,
    FINDING_OPEN,
    SIGNOFF_CHAIN,
    BaselineStats,
    DeviceContext,
    Finding,
    MetricStats,
)

logger = logging.getLogger(__name__)

Connection = psycopg2.extensions.connection

#: measurement_agg 的量測指標欄位，直接沿用 AGG_SPEC 的 key 順序——
#: 兩邊如果各自維護一份欄位清單，聚合層新增/移除欄位時很容易漏改一邊，
#: 讓 bulk_insert_agg 悄悄漏寫某個指標而不報錯（多出的欄位被忽略、
#: 缺少的欄位變成 NULL，兩者都不會讓程式炸掉，只會讓資料悄悄不見）。
#: 寫入 measurement_agg 的指標欄。AGG_SPEC 之外還要加上 AXIS_IMPACT_COLS
#: ——那組是跨三軸取極值算出來的，不是單一來源欄位的聚合，所以不在
#: AGG_SPEC 裡；漏掉的話會安靜地不寫入，欄位永遠是 NULL。
_AGG_METRIC_COLS: tuple[str, ...] = tuple(AGG_SPEC.keys()) + tuple(AXIS_IMPACT_COLS.keys())

#: 每日 rollup 的欄位。週報與長期趨勢讀的是日層，這裡漏掉的欄位在週報裡
#: 等於不存在——小時層算得再細也沒用。
_DAILY_METRIC_COLS: tuple[str, ...] = (
    "running_hours", "vel_rms", "vel_oa", "acc_rms", "acc_oa", "acc_peak",
    "acc_crest", "acc_kurt", "disp_p2p", "acc_weighted_mean_freq",
    "acc_crest_axis_max", "acc_kurt_axis_max", "temp_avg", "temp_max",
)

_SEVERITY_RANK = {"ok": 0, "warn": 1, "err": 2}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _max_severity(a: str | None, b: str | None) -> str:
    """兩個嚴重度取較高者；用於 peak_severity（歷來最壞情況不可被後續好轉蓋掉）。"""
    ra, rb = _SEVERITY_RANK.get(a or "ok", 0), _SEVERITY_RANK.get(b or "ok", 0)
    return a if ra >= rb else b  # type: ignore[return-value]


def _clean_value(v: Any) -> Any:
    """
    把 pandas/numpy 型別轉成 psycopg2 認得的 Python 原生型別。

    NaN 必須轉成 None（SQL NULL）——`_empty_metrics()` 用 NaN 表示
    「無此指標」，若原樣寫進 DB，NUMERIC 欄位會直接報錯（NaN 不是
    合法的 NUMERIC 字面值），且就算硬塞進去，`NaN != NaN` 的語意會讓
    後續所有 `WHERE col = ...` 判斷都失效。
    """
    if v is None:
        return None
    if isinstance(v, dict):
        return Json(v)
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (float, np.floating)):
        fv = float(v)
        return None if math.isnan(fv) else fv
    return v


# =============================================================
# 設備與量測點
# =============================================================

def upsert_device(conn: Connection, device: DeviceContext) -> str:
    """
    寫入/更新設備台帳。

    `DeviceContext` 只涵蓋規則判定所需的子集欄位（見 types.py），
    `device` 表另有 rated_power_kw / owner_group 等純管理性欄位不在
    契約內——這裡刻意不去動那些欄位（不寫入 INSERT 清單），交由台帳
    管理介面直接對 DB 操作，避免這支函式的呼叫端不小心用預設值覆蓋掉
    管理員手動填寫的資料。
    """
    sql = """
        INSERT INTO device (device_id, device_name, building, floor, system_name,
                             machine_type, rated_rpm, fmf_hz, is_standby,
                             iso_machine_class, iso_class_source, updated_at)
        VALUES (%(device_id)s, %(device_name)s, %(building)s, %(floor)s, %(system_name)s,
                %(machine_type)s, %(rated_rpm)s, %(fmf_hz)s, %(is_standby)s,
                %(iso_machine_class)s, %(iso_class_source)s, now())
        ON CONFLICT (device_id) DO UPDATE SET
            device_name       = EXCLUDED.device_name,
            building          = EXCLUDED.building,
            floor             = EXCLUDED.floor,
            system_name       = EXCLUDED.system_name,
            machine_type      = EXCLUDED.machine_type,
            rated_rpm         = EXCLUDED.rated_rpm,
            fmf_hz            = EXCLUDED.fmf_hz,
            is_standby        = EXCLUDED.is_standby,
            iso_machine_class = EXCLUDED.iso_machine_class,
            iso_class_source  = EXCLUDED.iso_class_source,
            updated_at        = now()
        RETURNING device_id
    """
    params = {
        "device_id": device.device_id,
        "device_name": device.device_name or None,
        "building": device.building or None,
        "floor": device.floor or None,
        "system_name": device.system_name or None,
        "machine_type": device.machine_type or None,
        "rated_rpm": device.rated_rpm,
        "fmf_hz": device.fmf_hz,
        "is_standby": device.is_standby,
        "iso_machine_class": device.iso_machine_class,
        "iso_class_source": device.iso_class_source,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()["device_id"]


def _row_to_device_context(row: dict) -> DeviceContext:
    return DeviceContext(
        device_id=row["device_id"],
        device_name=row["device_name"] or "",
        building=row["building"] or "",
        floor=row["floor"] or "",
        system_name=row["system_name"] or "",
        machine_type=row["machine_type"] or "",
        is_standby=row["is_standby"],
        iso_machine_class=row["iso_machine_class"],
        iso_class_source=row["iso_class_source"],
        rated_rpm=float(row["rated_rpm"]) if row["rated_rpm"] is not None else None,
        fmf_hz=float(row["fmf_hz"]) if row["fmf_hz"] is not None else None,
    )


def get_device(conn: Connection, device_id: str) -> DeviceContext | None:
    """單一設備查詢；找不到回傳 None 而非拋例外——呼叫端（規則引擎）常見的
    處理方式是「查不到就跳過這個設備」，用例外會逼每個呼叫端都要包 try/except。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM device WHERE device_id = %s", (device_id,))
        row = cur.fetchone()
    return _row_to_device_context(row) if row else None


def list_devices(
    conn: Connection,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
    status: str | None = "active",
) -> list[DeviceContext]:
    """依範圍篩選設備清單；`status=None` 表示不篩選狀態（含 decommissioned）。"""
    clauses: list[str] = []
    params: list[Any] = []
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    if building:
        clauses.append("building = %s")
        params.append(building)
    if floor:
        clauses.append("floor = %s")
        params.append(floor)
    if system_name:
        clauses.append("system_name = %s")
        params.append(system_name)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM device {where} ORDER BY device_id", params)
        return [_row_to_device_context(r) for r in cur.fetchall()]


def upsert_measure_point(
    conn: Connection,
    device_id: str,
    position: str,
    sensor_id: str | None = None,
    channel_x: int | None = None,
    channel_y: int | None = None,
    channel_z: int | None = None,
    install_date: Any = None,
    axis_energy_baseline: dict | None = None,
) -> int:
    """
    寫入/更新量測點；`(device_id, position)` 為自然鍵。

    `install_date` / `axis_energy_baseline` 用 COALESCE 保留舊值——
    這兩欄通常是一次性設定（安裝日期）或由基準期計算流程另外寫入
    （軸能量基準），呼叫端若只是要更新 sensor_id/channel 而沒帶這兩個
    參數，不該把已經算好的基準值覆蓋成 NULL。
    """
    sql = """
        INSERT INTO measure_point (device_id, position, sensor_id, channel_x, channel_y,
                                    channel_z, install_date, axis_energy_baseline)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (device_id, position) DO UPDATE SET
            sensor_id            = EXCLUDED.sensor_id,
            channel_x            = EXCLUDED.channel_x,
            channel_y            = EXCLUDED.channel_y,
            channel_z            = EXCLUDED.channel_z,
            install_date         = COALESCE(EXCLUDED.install_date, measure_point.install_date),
            axis_energy_baseline = COALESCE(EXCLUDED.axis_energy_baseline,
                                             measure_point.axis_energy_baseline)
        RETURNING point_id
    """
    params = (
        device_id, position, sensor_id, channel_x, channel_y, channel_z, install_date,
        Json(axis_energy_baseline) if axis_energy_baseline is not None else None,
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()["point_id"]


def get_points_for_device(
    conn: Connection, device_id: str, active_only: bool = True
) -> list[dict]:
    sql = "SELECT * FROM measure_point WHERE device_id = %s"
    if active_only:
        sql += " AND is_active"
    sql += " ORDER BY position"
    with conn.cursor() as cur:
        cur.execute(sql, (device_id,))
        return [dict(r) for r in cur.fetchall()]


# =============================================================
# 量測資料（Tier 1）
# =============================================================

def bulk_insert_agg(conn: Connection, point_id: int, agg_df: pd.DataFrame) -> int:
    """
    把 `aggregate_hourly()` 的輸出寫入 `measurement_agg`。

    用 `ON CONFLICT (point_id, ts_hour) DO UPDATE` 而非先刪後插：
    同一小時可能因為當天檔案補齊而重跑聚合，upsert 讓「重新處理某一天」
    是安全、可重複執行的操作，不需要呼叫端自己先算出要刪哪個時間範圍。

    `no_data` / `not_running` 列**必須一起寫入**（不可只寫 `ok` 列並
    跳過其他狀態）——趨勢圖需要靠這些列才知道哪裡該斷線、規則引擎需要
    靠 `not_running` 列排除「未運轉」不判異常，若在這裡就地過濾掉，
    等於在資料層悄悄抹掉聚合層刻意保留的資訊。
    """
    if agg_df is None or agg_df.empty:
        return 0

    cols = [
        "point_id", "ts_hour", "data_status", "completeness",
        "n_samples_total", "n_samples_running",
        *_AGG_METRIC_COLS, "axis_energy_sorted",
    ]
    rows = []
    for _, r in agg_df.iterrows():
        row = [
            point_id,
            _clean_value(r["ts_hour"]),
            r["data_status"],
            _clean_value(r.get("completeness")),
            int(r["n_samples_total"]),
            int(r["n_samples_running"]),
        ]
        row.extend(_clean_value(r.get(c)) for c in _AGG_METRIC_COLS)
        row.append(_clean_value(r.get("axis_energy_sorted")))
        rows.append(tuple(row))

    update_cols = [c for c in cols if c not in ("point_id", "ts_hour")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    sql = (
        f"INSERT INTO measurement_agg ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (point_id, ts_hour) DO UPDATE SET {set_clause}"
    )
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=500)
    return len(rows)


def get_agg(conn: Connection, point_id: int, start: Any, end: Any) -> pd.DataFrame:
    """
    讀回 `[start, end)` 區間的每小時聚合，含所有 `data_status`。

    刻意不在這裡篩掉 `no_data`/`not_running`——要不要排除是消費端的
    決定（趨勢分析要排除、涵蓋率報告反而需要它們），篩選邏輯已經在
    `RuleContext.analyzable()` 裡有明確定義，資料層重複實作一次只會
    讓兩處篩選條件將來對不上。
    """
    sql = """
        SELECT * FROM measurement_agg
        WHERE point_id = %s AND ts_hour >= %s AND ts_hour < %s
        ORDER BY ts_hour
    """
    with conn.cursor() as cur:
        cur.execute(sql, (point_id, start, end))
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([dict(r) for r in rows])


def upsert_daily(conn: Connection, point_id: int, daily_df: pd.DataFrame) -> int:
    """把每日 rollup 寫入 `measurement_daily`；語意與 `bulk_insert_agg` 相同。"""
    if daily_df is None or daily_df.empty:
        return 0

    cols = ["point_id", "date", *_DAILY_METRIC_COLS, "axis_energy_sorted"]
    rows = []
    for _, r in daily_df.iterrows():
        date_val = r["date"]
        if isinstance(date_val, pd.Timestamp):
            date_val = date_val.date()
        row = [point_id, date_val]
        row.extend(_clean_value(r.get(c)) for c in _DAILY_METRIC_COLS)
        row.append(_clean_value(r.get("axis_energy_sorted")))
        rows.append(tuple(row))

    update_cols = [c for c in cols if c not in ("point_id", "date")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    sql = (
        f"INSERT INTO measurement_daily ({', '.join(cols)}) VALUES %s "
        f"ON CONFLICT (point_id, date) DO UPDATE SET {set_clause}"
    )
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=500)
    return len(rows)


# =============================================================
# 匯入稽核（區分「感測器斷線」與「匯入排程沒跑」）
# =============================================================
#
# 三支函式合起來要回答一個現有系統答不出來的問題：某量測點某天沒資料，
# 是「感測器斷線」還是「匯入排程根本沒跑」？兩者在 measurement_agg 裡
# 長得一模一樣，但前者該找現場工程師，後者該找系統排程，處置方向完全
# 相反——誤判的成本不是「少一則告警」，是工程師白跑一趟現場。
#
# find_missing_ingestion 之所以要以 measure_point × 日期為母集合、
# 而不是從 measurement_agg 反推，是因為若排程整天沒跑，
# measurement_agg 連一列（含 no_data）都不會存在，從那張表回推永遠
# 看不到「憑空消失的一天」——這正是 aggregate.py 的 _fill_gap_hours
# 只補「已觀測範圍內」缺口所留下的死角。

def record_ingestion(
    conn: Connection, point_id: int, ingest_date: date, status: str,
    source_file: str = '', row_count: int = 0, note: str = '',
) -> None:
    """
    記錄某量測點某日的匯入結果；同一 (point_id, ingest_date) 直接覆蓋舊紀錄。

    為什麼覆蓋而不是累加一筆新紀錄：匯入排程最常見的操作是「重跑某一
    天」——上游遲到的檔案補齊後重新匯入，或第一次失敗、修正腳本後再跑
    一次。這兩種情況都該讓 ingestion_log 只保留「這一天目前的真實狀態」，
    若疊出一堆舊的 failed 紀錄與新的 ok 紀錄並存，find_missing_ingestion
    與週報都無法判斷「這天到底算不算有問題」。
    """
    sql = """
        INSERT INTO ingestion_log
            (point_id, ingest_date, status, source_file, row_count, note, ingested_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (point_id, ingest_date) DO UPDATE SET
            status      = EXCLUDED.status,
            source_file = EXCLUDED.source_file,
            row_count   = EXCLUDED.row_count,
            note        = EXCLUDED.note,
            ingested_at = now()
    """
    with conn.cursor() as cur:
        cur.execute(sql, (point_id, ingest_date, status, source_file, int(row_count), note))
    logger.info("記錄匯入結果：point_id=%s ingest_date=%s status=%s row_count=%d",
                point_id, ingest_date, status, row_count)


def get_ingestion_audit(
    conn: Connection, start_date: date, end_date: date, point_id: int | None = None,
) -> pd.DataFrame:
    """
    取回 `[start_date, end_date]`（皆含）區間內的匯入紀錄，含量測點／設備標籤。

    直接把 device/measure_point 的標籤欄位一併 join 進來，是因為呼叫端
    （週報收集層）拿到這份稽核紀錄後，第一件事一定是要組出可讀的
    「設備 / 量測點・棟別」標籤——若這裡只回傳 point_id，呼叫端還要再
    查一次 measure_point/device，等於同一份 join 邏輯要維護兩份。
    """
    clauses = ["il.ingest_date >= %(start)s", "il.ingest_date <= %(end)s"]
    params: dict[str, Any] = {"start": start_date, "end": end_date}
    if point_id is not None:
        clauses.append("il.point_id = %(point_id)s")
        params["point_id"] = point_id

    sql = f"""
        SELECT il.point_id, il.ingest_date, il.status, il.source_file, il.row_count,
               il.note, il.ingested_at,
               mp.position, mp.device_id, d.device_name, d.building, d.floor, d.system_name
        FROM ingestion_log il
        JOIN measure_point mp ON mp.point_id = il.point_id
        JOIN device d         ON d.device_id = mp.device_id
        WHERE {' AND '.join(clauses)}
        ORDER BY il.ingest_date, il.point_id
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([dict(r) for r in rows])


def find_missing_ingestion(conn: Connection, start_date: date, end_date: date) -> list[dict]:
    """
    找出「應該要有匯入紀錄但沒有」的 (point_id, date) 組合——這是整個
    稽核機制裡唯一能發現「排程完全沒跑」的入口。

    母集合刻意是「`measure_point` 中 `is_active` 的量測點 × 區間內每一
    天」，不是從 `measurement_agg` 或 `raw_file` 已有的資料反推：若某天
    匯入排程完全沒跑，`measurement_agg` 連一列都不會存在，從那張表出發
    永遠看不到「消失的那一天」。只有從「理論上這一天本來就該處理」出發，
    用 LEFT JOIN 找缺口，才抓得到這種邊界情況。

    只要 `ingestion_log` 對某 (point_id, date) 有任何一筆紀錄——不論
    status 是 ok/partial/failed/no_file——就代表排程當天確實「處理過」
    這個量測點，不算在本函式的缺漏範圍內；那個量測點該日資料好不好，
    是 measurement_agg / 涵蓋率報告該回答的設備面問題，不是這裡要判定
    的事。

    Returns:
        依日期、設備、量測位置排序的 dict 清單，每筆含
        `point_id` / `device_id` / `position` / `date`，並附帶
        `building` / `floor` / `system_name` 供呼叫端直接組地點標籤。
    """
    sql = """
        SELECT mp.point_id, mp.device_id, mp.position, gs.d::date AS date,
               d.building, d.floor, d.system_name
        FROM measure_point mp
        JOIN device d ON d.device_id = mp.device_id
        CROSS JOIN generate_series(%(start)s::timestamp, %(end)s::timestamp, interval '1 day') AS gs(d)
        LEFT JOIN ingestion_log il
               ON il.point_id = mp.point_id AND il.ingest_date = gs.d::date
        WHERE mp.is_active AND il.point_id IS NULL
        ORDER BY gs.d, mp.device_id, mp.position
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"start": start_date, "end": end_date})
        return [dict(r) for r in cur.fetchall()]


# =============================================================
# 基準期
# =============================================================

def save_baseline(conn: Connection, baseline: BaselineStats) -> None:
    """
    寫入/更新基準期統計。

    `point_baseline.point_id` 是主鍵（一個量測點只有一份「目前生效」的
    基準），所以是整列覆蓋式的 upsert，不像 Finding 需要保留歷史；
    若未來需要追溯「基準期曾經改過幾次」，應另開一張歷史表，而不是把
    這張表改成可疊加多筆——那會讓「目前基準是哪一筆」變得要另外查詢。
    """
    sql = """
        INSERT INTO point_baseline
            (point_id, start_date, end_date, source, stats, n_hours, note, computed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (point_id) DO UPDATE SET
            start_date  = EXCLUDED.start_date,
            end_date    = EXCLUDED.end_date,
            source      = EXCLUDED.source,
            stats       = EXCLUDED.stats,
            n_hours     = EXCLUDED.n_hours,
            note        = EXCLUDED.note,
            computed_at = now()
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            baseline.point_id, baseline.start_date, baseline.end_date,
            baseline.source, Json(baseline.to_jsonb()),
            int(baseline.n_hours or 0), baseline.note or '',
        ))


def get_baseline(conn: Connection, point_id: int) -> BaselineStats | None:
    """
    讀回基準期統計，並把 JSONB 還原成 `dict[str, MetricStats]`。

    `n_hours` 與 `note` 一併持久化：基準是用多少可信小時算出來的，
    直接決定所有以它為準的 σ 比較可不可信，必須能被查詢與呈現。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT point_id, start_date, end_date, source, stats, n_hours, note "
            "FROM point_baseline WHERE point_id = %s",
            (point_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    stats = {
        k: MetricStats(median=v["median"], mean=v["mean"], std=v["std"], n=v["n"])
        for k, v in (row["stats"] or {}).items()
    }
    return BaselineStats(
        point_id=row["point_id"], start_date=row["start_date"], end_date=row["end_date"],
        source=row["source"], stats=stats,
        n_hours=row.get("n_hours", 0) or 0, note=row.get("note", "") or "",
    )


# =============================================================
# Finding 四階段簽核
# =============================================================

def _row_to_finding(row: dict) -> Finding:
    return Finding(
        finding_key=row["finding_key"], device_id=row["device_id"],
        target_type=row["target_type"], target=row["target"], issue_type=row["issue_type"],
        family=row["family"], rule_code=row["rule_code"], title=row["title"],
        severity=row["severity"], peak_severity=row["peak_severity"],
        point_id=row["point_id"], detail=row["detail"] or "", status=row["status"],
        stage_entered_at=row["stage_entered_at"], assigned_to=row["assigned_to"],
        occurrence_count=row["occurrence_count"], first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        baseline_value=float(row["baseline_value"]) if row["baseline_value"] is not None else None,
        current_value=float(row["current_value"]) if row["current_value"] is not None else None,
        value_unit=row["value_unit"] or "", evidence=row["evidence"] or {},
        trigger_params=row["trigger_params"] or {},
        interpretation_limit=row["interpretation_limit"] or "",
        escalated_at=row["escalated_at"],
        needs_expert_measurement=row["needs_expert_measurement"], source=row["source"],
        resolved_at=row["resolved_at"], resolved_by=row["resolved_by"],
    )


def _insert_status_history(
    cur, finding_id: int, from_status: str | None, to_status: str,
    changed_by: int | None, note: str | None, duration=None,
) -> None:
    cur.execute(
        """
        INSERT INTO finding_status_history
            (finding_id, from_status, to_status, changed_at, changed_by,
             duration_in_from_status, note)
        VALUES (%s, %s, %s, now(), %s, %s, %s)
        """,
        (finding_id, from_status, to_status, changed_by, duration, note),
    )


def _insert_note(
    cur, finding_id: int, stage: str, author_id: int | None, author_role: str | None,
    note: str, action_taken: str | None = None, root_cause: str | None = None,
    is_human: bool = True,
) -> int:
    cur.execute(
        """
        INSERT INTO finding_note (finding_id, stage, author_id, author_role, is_human,
                                   note, action_taken, root_cause)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING note_id
        """,
        (finding_id, stage, author_id, author_role, is_human, note, action_taken, root_cause),
    )
    return cur.fetchone()["note_id"]


def _insert_new_finding(cur, finding: Finding) -> dict:
    sql = """
        INSERT INTO finding (
            finding_key, device_id, point_id, target_type, target, issue_type, family,
            rule_code, title, detail, severity, peak_severity, status, stage_entered_at,
            assigned_to, occurrence_count, first_seen_at, last_seen_at, baseline_value,
            current_value, value_unit, evidence, trigger_params, interpretation_limit,
            needs_expert_measurement, source
        ) VALUES (
            %(finding_key)s, %(device_id)s, %(point_id)s, %(target_type)s, %(target)s,
            %(issue_type)s, %(family)s, %(rule_code)s, %(title)s, %(detail)s, %(severity)s,
            %(peak_severity)s, %(status)s, COALESCE(%(seen_at)s, now()), %(assigned_to)s, 1,
            COALESCE(%(seen_at)s, now()), COALESCE(%(seen_at)s, now()),
            %(baseline_value)s, %(current_value)s, %(value_unit)s, %(evidence)s,
            %(trigger_params)s, %(interpretation_limit)s, %(needs_expert_measurement)s,
            %(source)s
        ) RETURNING *
    """
    cur.execute(sql, {
        "finding_key": finding.finding_key, "device_id": finding.device_id,
        "point_id": finding.point_id, "target_type": finding.target_type,
        "target": finding.target, "issue_type": finding.issue_type, "family": finding.family,
        "rule_code": finding.rule_code, "title": finding.title, "detail": finding.detail,
        "severity": finding.severity, "peak_severity": finding.peak_severity,
        "status": finding.status, "assigned_to": finding.assigned_to,
        "baseline_value": finding.baseline_value, "current_value": finding.current_value,
        "value_unit": finding.value_unit, "evidence": Json(finding.evidence or {}),
        "trigger_params": Json(finding.trigger_params or {}),
        "interpretation_limit": finding.interpretation_limit,
        "needs_expert_measurement": finding.needs_expert_measurement,
        # 判定當下的時間。正式排程每天跑一次，這與 now() 幾乎相同；但匯入
        # 歷史資料補資料庫時差很多——若一律用 now()，整批歷史事項都會被
        # 壓成執行當天，任何過去期間的週報都會查不到東西。
        "seen_at": finding.last_seen_at,
        "source": finding.source,
    })
    return cur.fetchone()


def _bump_existing_finding(cur, existing: dict, finding: Finding) -> dict:
    """
    既有問題再現：只累加次數、更新最新讀數，不動 first_seen_at / stage_entered_at。

    觸發當下的數值／門檻／證據欄位（baseline_value, current_value, value_unit,
    evidence, trigger_params, interpretation_limit）一律覆寫成本次觸發的值，
    刻意不比照 `upsert_measure_point` 對 install_date / axis_energy_baseline
    採用的 COALESCE 保留舊值寫法：那兩欄是「一次性設定、不該被平常的更新
    覆蓋」，但這裡工程師開單頁看到的是「現在多嚴重」，不是「第一次觸發時
    多嚴重」——尤其 baseline 可能因為重新偵測而變動、trigger_params 是
    「這一次」的門檻快照，若沿用舊值，前面提到的『回溯重算』就會用錯門檻。
    真正需要「保留第一次」語意的是 first_seen_at / stage_entered_at，
    這兩欄本來就不在這次 UPDATE 的欄位清單內，維持原樣。
    """
    new_peak = _max_severity(existing["peak_severity"], finding.severity)
    cur.execute(
        """
        UPDATE finding SET
            occurrence_count     = occurrence_count + 1,
            baseline_value       = %(baseline_value)s,
            current_value        = %(current_value)s,
            value_unit           = %(value_unit)s,
            last_seen_at         = COALESCE(%(seen_at)s, now()),
            severity             = %(severity)s,
            peak_severity        = %(peak_severity)s,
            detail               = %(detail)s,
            evidence             = %(evidence)s,
            trigger_params       = %(trigger_params)s,
            interpretation_limit = %(interpretation_limit)s,
            updated_at           = now()
        WHERE finding_id = %(finding_id)s
        RETURNING *
        """,
        {
            "baseline_value": finding.baseline_value, "current_value": finding.current_value,
            "value_unit": finding.value_unit, "severity": finding.severity,
            "peak_severity": new_peak, "detail": finding.detail,
            "evidence": Json(finding.evidence or {}),
            "trigger_params": Json(finding.trigger_params or {}),
            "interpretation_limit": finding.interpretation_limit,
            # 見 _insert_new_finding 的說明
            "seen_at": finding.last_seen_at,
            "finding_id": existing["finding_id"],
        },
    )
    return cur.fetchone()


def _reopen_finding(cur, existing: dict, finding: Finding) -> dict:
    """已結案問題重新出現：沿用同一個 finding_key（UNIQUE 限制），但各項追蹤欄位重新起算。"""
    status = finding.status if finding.status not in CLOSED_STATUSES else FINDING_OPEN
    cur.execute(
        """
        UPDATE finding SET
            status                   = %(status)s,
            stage_entered_at         = COALESCE(%(seen_at)s, now()),
            assigned_to              = NULL,
            occurrence_count         = 1,
            first_seen_at            = COALESCE(%(seen_at)s, now()),
            last_seen_at             = COALESCE(%(seen_at)s, now()),
            baseline_value           = %(baseline_value)s,
            current_value            = %(current_value)s,
            value_unit               = %(value_unit)s,
            severity                 = %(severity)s,
            peak_severity            = %(severity)s,
            detail                   = %(detail)s,
            evidence                 = %(evidence)s,
            trigger_params           = %(trigger_params)s,
            interpretation_limit     = %(interpretation_limit)s,
            escalated_at             = NULL,
            needs_expert_measurement = %(needs_expert_measurement)s,
            resolved_at              = NULL,
            resolved_by              = NULL,
            updated_at               = now()
        WHERE finding_id = %(finding_id)s
        RETURNING *
        """,
        {
            "status": status, "baseline_value": finding.baseline_value,
            "current_value": finding.current_value, "value_unit": finding.value_unit,
            "severity": finding.severity,
            "detail": finding.detail, "evidence": Json(finding.evidence or {}),
            "trigger_params": Json(finding.trigger_params or {}),
            "interpretation_limit": finding.interpretation_limit,
            "needs_expert_measurement": finding.needs_expert_measurement,
        # 判定當下的時間。正式排程每天跑一次，這與 now() 幾乎相同；但匯入
        # 歷史資料補資料庫時差很多——若一律用 now()，整批歷史事項都會被
        # 壓成執行當天，任何過去期間的週報都會查不到東西。
        "seen_at": finding.last_seen_at,
            "finding_id": existing["finding_id"],
        },
    )
    return cur.fetchone()


def upsert_finding(conn: Connection, finding: Finding) -> Finding:
    """
    規則引擎每次判定觸發時呼叫；三種情境的分派邏輯見模組上方設計說明。

    Returns:
        寫入後的完整 Finding（含 DB 產生的時間戳與累加後的 occurrence_count）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM finding WHERE finding_key = %s FOR UPDATE",
            (finding.finding_key,),
        )
        existing = cur.fetchone()

        if existing is None:
            row = _insert_new_finding(cur, finding)
            _insert_status_history(cur, row["finding_id"], None, row["status"],
                                    changed_by=None, note="規則引擎建案")
            return _row_to_finding(row)

        if existing["status"] not in CLOSED_STATUSES:
            row = _bump_existing_finding(cur, existing, finding)
            return _row_to_finding(row)

        # 已結案 → 視為新案重新開始。用「已結案多久」當作 duration_in_from_status，
        # 讓歷程表看得出「這問題曾經消失了 N 天又回來」，而不是留一段無意義的 0。
        closed_since = existing["resolved_at"] or existing["stage_entered_at"]
        duration = _now_utc() - closed_since
        row = _reopen_finding(cur, existing, finding)
        _insert_status_history(cur, row["finding_id"], existing["status"], row["status"],
                                changed_by=None, note="結案後重新觸發，開新案", duration=duration)
        return _row_to_finding(row)


def _validate_transition(from_status: str, to_status: str) -> None:
    """
    合法流轉只有兩種：沿 `SIGNOFF_CHAIN` 前進一步，或從任一未結案狀態轉 `false_positive`。

    `auto_resolved` 刻意不開放由此驗證放行——它是系統判定數值回歸的
    獨立路徑（`auto_resolve()`），不是人工簽核鏈的一部分，若允許
    `transition_status` 也能設成 `auto_resolved`，`resolved_by` 該填
    使用者還是 'auto' 就會產生歧義。
    """
    if from_status in CLOSED_STATUSES:
        raise ValueError(f"已結案的事項不可再轉換狀態（目前狀態：{from_status}）")
    if to_status == FINDING_FALSE_POSITIVE:
        return
    if to_status == FINDING_AUTO_RESOLVED:
        raise ValueError("auto_resolved 只能由 auto_resolve() 設定，不可經 transition_status 指定")
    if from_status not in SIGNOFF_CHAIN:
        raise ValueError(f"未知的來源狀態：{from_status}")
    if to_status not in SIGNOFF_CHAIN:
        raise ValueError(f"未知的目標狀態：{to_status}")
    idx_from, idx_to = SIGNOFF_CHAIN.index(from_status), SIGNOFF_CHAIN.index(to_status)
    if idx_to != idx_from + 1:
        raise ValueError(
            f"不合法的狀態流轉：{from_status} → {to_status}；"
            f"簽核鏈只能依序前進（{' → '.join(SIGNOFF_CHAIN)}）"
        )


def transition_status(
    conn: Connection, finding_key: str, to_status: str,
    changed_by: int | None, note: str | None = None, role: str | None = None,
) -> Finding:
    """
    四階段簽核的狀態轉換；同一交易內完成「驗證合法性 → 更新 finding →
    寫入 finding_status_history（含停留時間）→（若有 note）寫入
    finding_note」，理由見模組 docstring 第 2 點。

    `duration_in_from_status` 用「進入目前這關的時間到現在」計算——
    這正是 `stage_entered_at` 存在的目的：它在每次轉換時被重設為
    `now()`，所以下一次轉換時 `now() - stage_entered_at` 就是這次
    在該關卡的停留時間，不需要另外去 history 表回溯上一筆記錄。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM finding WHERE finding_key = %s FOR UPDATE", (finding_key,),
        )
        existing = cur.fetchone()
        if existing is None:
            raise ValueError(f"finding_key 不存在：{finding_key}")

        from_status = existing["status"]
        _validate_transition(from_status, to_status)
        duration = _now_utc() - existing["stage_entered_at"]

        set_resolved = to_status in CLOSED_STATUSES
        cur.execute(
            """
            UPDATE finding SET
                status           = %(to_status)s,
                stage_entered_at = now(),
                updated_at       = now(),
                resolved_at      = CASE WHEN %(set_resolved)s THEN now() ELSE resolved_at END,
                resolved_by      = CASE WHEN %(set_resolved)s THEN %(resolved_by)s
                                        ELSE resolved_by END
            WHERE finding_id = %(finding_id)s
            RETURNING *
            """,
            {
                "to_status": to_status, "set_resolved": set_resolved,
                "resolved_by": str(changed_by) if changed_by is not None else None,
                "finding_id": existing["finding_id"],
            },
        )
        row = cur.fetchone()

        _insert_status_history(cur, row["finding_id"], from_status, to_status,
                                changed_by, note, duration=duration)

        if note:
            _insert_note(cur, row["finding_id"], stage=to_status, author_id=changed_by,
                         author_role=role, note=note, is_human=True)

        return _row_to_finding(row)


def mark_escalated(
    conn: Connection, finding_key: str, note: str | None = None,
    changed_by: int | None = None,
) -> Finding:
    """
    標記「處理中但持續惡化」。

    刻意不是狀態轉換，而是獨立於簽核狀態之外的旗標——一個 finding 可以
    同時是 `supervisor_reviewed` 又 `escalated_at` 非 NULL，週報要能
    同時呈現這兩件事（計畫書 §七：不因「處理中」而輕描淡寫）。
    `COALESCE(escalated_at, now())` 讓函式冪等：同一個 finding 被規則
    引擎連續多天判定持續惡化時，`escalated_at` 只記第一次發生的時間。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE finding SET escalated_at = COALESCE(escalated_at, now()), updated_at = now()
            WHERE finding_key = %s AND status NOT IN %s
            RETURNING *
            """,
            (finding_key, CLOSED_STATUSES),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"finding_key 不存在或已結案，無法標記惡化：{finding_key}")
        if note:
            _insert_note(cur, row["finding_id"], stage=row["status"], author_id=changed_by,
                         author_role=None, note=note, is_human=changed_by is not None)
        return _row_to_finding(row)


def auto_resolve(conn: Connection, finding_key: str, note: str | None = None) -> Finding:
    """
    數值回歸門檻內的系統自動結案，與簽核鏈並行（計畫書 §七）。

    不論卡在四階段的哪一關，只要規則引擎判定數值已回到門檻內，直接
    結案不必補完剩餘步驟；已產生的 finding_note / status_history 予以
    保留供追溯，不做刪除或改寫。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM finding WHERE finding_key = %s FOR UPDATE", (finding_key,),
        )
        existing = cur.fetchone()
        if existing is None:
            raise ValueError(f"finding_key 不存在：{finding_key}")
        if existing["status"] in CLOSED_STATUSES:
            raise ValueError(f"finding 已結案（{existing['status']}），無需自動結案")

        duration = _now_utc() - existing["stage_entered_at"]
        cur.execute(
            """
            UPDATE finding SET
                status = %s, stage_entered_at = now(), updated_at = now(),
                resolved_at = now(), resolved_by = 'auto'
            WHERE finding_id = %s
            RETURNING *
            """,
            (FINDING_AUTO_RESOLVED, existing["finding_id"]),
        )
        row = cur.fetchone()
        _insert_status_history(cur, row["finding_id"], existing["status"], FINDING_AUTO_RESOLVED,
                                changed_by=None, note=note or "數值回歸門檻內，系統自動結案",
                                duration=duration)
        return _row_to_finding(row)


def add_note(
    conn: Connection, finding_key: str, stage: str, author_id: int | None, role: str | None,
    note: str, action_taken: str | None = None, root_cause: str | None = None,
    is_human: bool = True,
) -> int:
    """
    寫入單一階段的回覆，不改變 finding 狀態（狀態轉換請用 `transition_status`，
    它會在轉換的同時自動寫入對應的 note）。

    這支函式的用途是「同一階段內的多次補充」——例如工程師在 `open`
    階段先問了問題、又追加一則現場照片說明，這些都停留在同一
    `stage`，不構成狀態轉換。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT finding_id FROM finding WHERE finding_key = %s", (finding_key,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"finding_key 不存在：{finding_key}")
        return _insert_note(cur, row["finding_id"], stage, author_id, role, note,
                            action_taken, root_cause, is_human)


def get_open_findings(
    conn: Connection,
    building: str | None = None,
    floor: str | None = None,
    system_name: str | None = None,
    assigned_to: int | None = None,
    only_escalated: bool = False,
    only_sla_breached: bool = False,
) -> list[dict]:
    """
    查 `v_open_finding` 檢視（含 SLA 逾期判定與最新人工回覆），不在 Python
    重算——理由見模組上方設計說明第 2 點的姊妹論點：SLA/逾期定義只能
    有一份，寫在 SQL 檢視裡才能保證 Dashboard、週報、這支 API 三處一致。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if building:
        clauses.append("building = %s")
        params.append(building)
    if floor:
        clauses.append("floor = %s")
        params.append(floor)
    if system_name:
        clauses.append("system_name = %s")
        params.append(system_name)
    if assigned_to is not None:
        clauses.append("assigned_to = %s")
        params.append(assigned_to)
    if only_escalated:
        clauses.append("escalated_at IS NOT NULL")
    if only_sla_breached:
        clauses.append("is_sla_breached")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM v_open_finding {where} ORDER BY stage_entered_at ASC"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# =============================================================
# 觀察名單（observe 級判定，不進簽核鏈）
# =============================================================
#
# `Observation` 定義在這裡而非 `vibcore/types.py`：概念上它與 `Finding`
# 屬於同一層級的跨模組契約，但本次任務的檔案歸屬明確排除 types.py，
# 而目前只有這支模組的 `upsert_observation` 與 `pipeline/daily.py` 需要
# 用到這個型別，先以資料存取層的區域型別滿足即可，不強行擠進 types.py。

@dataclass
class Observation:
    """
    對應 DB `observation` 表的一列（observe 級判定）。

    刻意不比照 `Finding` 帶 status / stage_entered_at / assigned_to 等
    簽核欄位——observation 不進簽核鏈，帶著那些欄位只會讓人以為它該被
    處理（見 db/schema.sql 該表的說明）。證據欄位（rule_code 起到
    interpretation_limit 為止）則與 Finding 對稱保留，理由相同：日後
    要回溯評估「門檻改成 X 會剩幾件」，沒有這些數值就只能重跑整條管線。
    """
    observation_key: str                     # {target_type}:{target}:{issue_type}
    device_id: str
    target_type: str
    target: str
    issue_type: str
    family: str
    rule_code: str
    title: str
    point_id: int | None = None
    detail: str = ''
    occurrence_count: int = 1
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    baseline_value: float | None = None
    current_value: float | None = None
    value_unit: str = ''
    evidence: dict = field(default_factory=dict)
    trigger_params: dict = field(default_factory=dict)
    interpretation_limit: str = ''
    source: str = 'rule_engine'


def upsert_observation(conn: Connection, observation: Observation) -> dict:
    """
    寫入/更新 observe 級判定；去重語意比照 `upsert_finding` 的「bump」
    路徑——同一 `observation_key` 持續存在時只累加 `occurrence_count`
    並覆寫最新數值，不逐日新增一整筆歷史列。

    與 `upsert_finding` 不同的是，這裡**不需要先 `SELECT ... FOR UPDATE`
    再依分支處理**：`upsert_finding` 要鎖列是因為它有三種分支（新建／
    未結案再現／已結案重開），分支判斷本身就有 race window；而
    observation 沒有 status，永遠只有一種語意——「這件事現在還在，
    累加一次」，單一 `INSERT ... ON CONFLICT DO UPDATE` 陳述式在
    PostgreSQL 底下本身就是原子操作，不會有兩個行程同時讀到舊
    `occurrence_count` 的問題，額外加鎖只是白增加一次往返。

    `title` 刻意不在 `DO UPDATE` 的欄位清單內——title 是規則的固定描述
    文字，理由與 `_bump_existing_finding` 對 title 的處理相同：會隨每次
    觸發變動的是 detail／數值，不是規則本身叫什麼名字。`first_seen_at`
    同理不在清單內，維持第一次寫入的值，讓「已持續多久」算得出來。

    Returns:
        寫入後的完整資料列（dict），含 DB 產生的時間戳與累加後的 occurrence_count。
    """
    sql = """
        INSERT INTO observation (
            observation_key, device_id, point_id, target_type, target, issue_type,
            family, rule_code, title, detail, occurrence_count, first_seen_at, last_seen_at,
            baseline_value, current_value, value_unit, evidence, trigger_params,
            interpretation_limit, source
        ) VALUES (
            %(observation_key)s, %(device_id)s, %(point_id)s, %(target_type)s, %(target)s,
            %(issue_type)s, %(family)s, %(rule_code)s, %(title)s, %(detail)s, 1,
            COALESCE(%(seen_at)s, now()), COALESCE(%(seen_at)s, now()),
            %(baseline_value)s, %(current_value)s, %(value_unit)s, %(evidence)s,
            %(trigger_params)s, %(interpretation_limit)s, %(source)s
        )
        ON CONFLICT (observation_key) DO UPDATE SET
            detail                = EXCLUDED.detail,
            occurrence_count      = observation.occurrence_count + 1,
            last_seen_at          = COALESCE(%(seen_at)s, now()),
            baseline_value        = EXCLUDED.baseline_value,
            current_value         = EXCLUDED.current_value,
            value_unit            = EXCLUDED.value_unit,
            evidence              = EXCLUDED.evidence,
            trigger_params        = EXCLUDED.trigger_params,
            interpretation_limit  = EXCLUDED.interpretation_limit,
            updated_at            = now()
        RETURNING *
    """
    params = {
        "observation_key": observation.observation_key, "device_id": observation.device_id,
        "point_id": observation.point_id, "target_type": observation.target_type,
        "target": observation.target, "issue_type": observation.issue_type,
        "family": observation.family, "rule_code": observation.rule_code,
        "title": observation.title, "detail": observation.detail,
        "baseline_value": observation.baseline_value, "current_value": observation.current_value,
        "value_unit": observation.value_unit, "evidence": Json(observation.evidence or {}),
        # 見 _insert_new_finding 的說明：匯入歷史資料時若一律用 now()，
        # 整批觀察項目都會被壓成執行當天。
        "seen_at": observation.last_seen_at,
        "trigger_params": Json(observation.trigger_params or {}),
        "interpretation_limit": observation.interpretation_limit,
        "source": observation.source,
    }
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return dict(cur.fetchone())


# =============================================================
# 設定
# =============================================================

def get_rule_configs(conn: Connection, active_only: bool = True) -> dict[str, dict]:
    """回傳 `{rule_code: 設定}`；比照 HVM 的 `get_alert_thresholds`——只有查到的閾值才算數，
    找不到的規則不該由呼叫端猜一個預設值頂替。"""
    sql = "SELECT * FROM rule_config" + (" WHERE is_active" if active_only else "")
    with conn.cursor() as cur:
        cur.execute(sql)
        return {r["rule_code"]: dict(r) for r in cur.fetchall()}


def get_iso_thresholds(conn: Connection) -> dict[str, dict]:
    """回傳 `{machine_class: {ab_boundary, bc_boundary, cd_boundary, label, ...}}`。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM iso_threshold")
        return {r["machine_class"]: dict(r) for r in cur.fetchall()}


def get_sla_config(conn: Connection, active_only: bool = True) -> dict[str, int]:
    """回傳 `{stage: sla_days}`，供 `v_open_finding` 之外需要單獨引用 SLA 天數的地方使用
    （例如產生「還剩幾天」的提醒文案）。"""
    sql = "SELECT stage, sla_days FROM sla_config" + (" WHERE is_active" if active_only else "")
    with conn.cursor() as cur:
        cur.execute(sql)
        return {r["stage"]: r["sla_days"] for r in cur.fetchall()}
