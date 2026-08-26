"""
auth.py — X-VIB-API-Key 驗證

比照 HVM 平台：地端 Agent 在每個請求帶入 `X-VIB-API-Key` header。

三種結果（見 PLAN_agent_platform_refactor.md §六）：
  - 環境變數 `VIB_AGENT_API_KEY` 未設定 → **503**。這是部署疏漏，不是呼叫方
    的錯，刻意與「金鑰錯誤」的 401 區分開來——否則 agent 平台看到 401 會
    以為自己的金鑰打錯而反覆重試同一把（其實整組都沒設定的）金鑰，503
    才會讓維運方及早發現「這台服務根本沒配置好」。
  - Header 缺失或與環境變數不符 → 401。
  - 相符 → 放行（回傳 None，FastAPI 的 Depends 不需要回傳值）。

比對用 `secrets.compare_digest`，避免逐字元比較造成的時序側錄風險。
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

#: 讀取 API Key 的環境變數名稱
API_KEY_ENV = "VIB_AGENT_API_KEY"

#: 比照 HVM 的 header 名稱，改為 VIB 前綴
API_KEY_HEADER = "X-VIB-API-Key"


async def verify_api_key(
    x_vib_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> None:
    """FastAPI 依賴：驗證請求帶入的 API Key。驗證失敗直接拋 HTTPException。"""
    expected = os.environ.get(API_KEY_ENV)
    if not expected:
        logger.error(f"{API_KEY_ENV} 未設定，服務無法驗證任何請求")
        raise HTTPException(status_code=503, detail="服務未就緒：API Key 尚未設定")

    if not x_vib_api_key or not secrets.compare_digest(x_vib_api_key, expected):
        logger.warning("收到無效或缺漏的 X-VIB-API-Key")
        raise HTTPException(status_code=401, detail="API Key 無效或缺漏")
