"""
main.py — FastAPI app：地端 Agent 呼叫的 API 層

比照公司既有 HVM Agent 平台的整合模式（見 PLAN_agent_platform_refactor.md
§六、§十一）：
  - Base URL `/api/agent/tools`
  - Header `X-VIB-API-Key` 驗證（見 `auth.py`）
  - 查詢一律 GET；唯一寫入型 `send_report` 為 POST
  - **查無資料回 200 + `{"error": ...}`，不是 HTTP 4xx**——404 留給「這個
    URL 本身不存在」，「有這個資源類型但查無此筆」是完全不同的語意，
    交由回應內容表達，讓 agent 端不必為兩種情況各寫一套錯誤處理

`verify_api_key` 刻意放在每個路徑函式的**第一個**參數，確保金鑰驗證先於
`get_db`（開啟 DB 連線）執行——金鑰錯誤或未設定時，不需要為了回錯誤訊息
而白開一條 DB 連線。

啟動方式（本機測試）：
    uvicorn vibcore.api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from typing import Iterator, Literal

from fastapi import Depends, FastAPI, Query
from psycopg2.extensions import connection as Connection

from vibcore.api import service
from vibcore.api.auth import verify_api_key
from vibcore.api.schemas import DAYS_MAX, DAYS_MIN, SendReportRequest
from vibcore.db.connection import get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#: send_report 每日發送上限的預設值；環境變數 VIB_REPORT_DAILY_LIMIT 可覆寫。
DEFAULT_DAILY_REPORT_LIMIT = 3

TOOLS_PREFIX = "/api/agent/tools"


def get_db() -> Iterator[Connection]:
    """
    每個請求一條連線，交易邊界對齊單次請求：正常結束 commit，
    例外則 rollback（見 `vibcore/db/connection.py` 的 `get_connection()`）。
    """
    with get_connection() as conn:
        yield conn


app = FastAPI(
    title="振動監測平台 — Agent 工具 API",
    description=(
        "供地端 Agent 平台呼叫的唯讀查詢與 send_report 寫入端點，"
        "整合模式比照公司既有 HVM Agent 平台。"
    ),
)


@app.get(f"{TOOLS_PREFIX}/get_vibration_thresholds")
def get_vibration_thresholds(
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """現行 ISO 門檻與規則設定（對應 HVM 的 get_alert_thresholds）。"""
    return service.get_vibration_thresholds(conn)


@app.get(f"{TOOLS_PREFIX}/get_device_list")
def get_device_list(
    building: str | None = None,
    floor: str | None = None,
    system: str | None = Query(default=None, description="System 別（對應 device.system_name）"),
    severity: Literal["err", "warn", "ok"] | None = None,
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """設備清單與最新狀態，支援 building/floor/system/severity 篩選。"""
    return service.get_device_list(
        conn, building=building, floor=floor, system_name=system, severity=severity
    )


@app.get(f"{TOOLS_PREFIX}/get_device_status")
def get_device_status(
    device_id: str,
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """單台設備目前狀態，含 data_age_minutes、涵蓋率與斷線狀況。查無設備回 200+error。"""
    return service.get_device_status(conn, device_id)


@app.get(f"{TOOLS_PREFIX}/get_device_trend")
def get_device_trend(
    device_id: str,
    metric: str = Query(..., description="vibcore.config.AGG_SPEC 的鍵名，例如 vel_rms"),
    position: str | None = None,
    days: int = Query(
        default=30, ge=DAYS_MIN, le=DAYS_MAX,
        description="觀察期天數，以日曆日切齊（見 service.resolve_period）",
    ),
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """指標歷史趨勢，支援 days 參數（日曆日切齊，見 service.resolve_period）。"""
    return service.get_device_trend(
        conn, device_id=device_id, metric=metric, position=position, days=days
    )


@app.get(f"{TOOLS_PREFIX}/get_open_findings")
def get_open_findings(
    building: str | None = None,
    floor: str | None = None,
    system: str | None = None,
    only_escalated: bool = False,
    only_sla_breached: bool = False,
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """未結案事項 + 工程師最後回覆；產報告前必呼叫。"""
    return service.get_open_findings(
        conn, building=building, floor=floor, system_name=system,
        only_escalated=only_escalated, only_sla_breached=only_sla_breached,
    )


@app.get(f"{TOOLS_PREFIX}/get_weekly_report_data")
def get_weekly_report_data(
    days: int = Query(
        default=7, ge=DAYS_MIN, le=DAYS_MAX,
        description="窗口天數，以日曆日切齊（見 service.resolve_period）；日報傳 1",
    ),
    building: str | None = None,
    floor: str | None = None,
    system: str | None = None,
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """週報彙總，支援 days 參數（日報傳 days=1；日曆日切齊，見 service.resolve_period）。"""
    return service.get_weekly_report_data(
        conn, days=days, building=building, floor=floor, system_name=system
    )


@app.get(f"{TOOLS_PREFIX}/get_event_context")
def get_event_context(
    finding_key: str,
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """單一 finding 的完整上下文（回覆歷史、簽核歷程、資料品質），供產出回覆建議。"""
    return service.get_event_context(conn, finding_key)


@app.post(f"{TOOLS_PREFIX}/send_report")
def send_report(
    payload: SendReportRequest,
    _: None = Depends(verify_api_key),
    conn: Connection = Depends(get_db),
) -> dict:
    """
    收 verdict/headline/actions/notes/days（選填 end_date），本系統負責排版
    寄送（SMTP 尚未設定，目前僅落庫）。時間窗一律用 `days` 相對天數表示，
    與 `get_weekly_report_data` 共用 `service.resolve_period()` 做日曆日切齊，
    確保同一輪流程中兩者算出同一段期間。四道卡控：不接受收件人欄位、
    主旨系統產生、只收結構化欄位並一律 HTML 轉義、每日發送次數上限
    （超過回 429，每次成功寫稽核 log）。
    """
    limit = int(os.environ.get("VIB_REPORT_DAILY_LIMIT", DEFAULT_DAILY_REPORT_LIMIT))
    return service.send_report(conn, payload, daily_limit=limit)
