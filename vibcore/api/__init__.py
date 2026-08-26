"""
vibcore.api — 給地端 Agent 呼叫的 API 層

比照公司既有 HVM Agent 平台的整合模式（見 PLAN_agent_platform_refactor.md
§六、§十一）：Base URL `/api/agent/tools`、Header `X-VIB-API-Key` 驗證、
查詢一律 GET、唯一寫入型 `send_report` 為 POST。

模組配置：
  - `auth.py`     — API Key 驗證
  - `schemas.py`  — `send_report` 請求體的 Pydantic 模型（四道卡控之一）
  - `queries.py`  — `vibcore/db/repository.py` 未涵蓋的補充查詢與寫入
  - `service.py`  — 組裝各工具回應內容的業務邏輯（呼叫 repository/queries/metrics）
  - `util.py`     — DB 回傳型別 → JSON 安全型別的轉換
  - `main.py`     — FastAPI app 與路由
"""
