"""
env.py — 從 .env 檔載入設定

## 為什麼需要這一層

程式各處是用 `os.environ.get()` 讀設定（連線參數、API Key 等）。在伺服器上
這些由系統環境變數提供沒問題，但本機開發與測試時，每次開新終端都要重設一輪
很麻煩，也容易漏設某一個而連到錯的資料庫。

這個模組讓專案根目錄的 `.env` 檔自動生效，行為與直接設環境變數相同。

## 兩個刻意的行為

**1. 真實環境變數優先，`.env` 不覆蓋既有值。**
正式機上通常由系統或容器注入環境變數，若 `.env` 蓋過去，一個忘了刪的開發用
檔案就會讓服務連到錯的資料庫——而且不會報錯，只會安靜地寫錯地方。

**2. 找不到 `.env` 或未安裝 python-dotenv 都不算錯誤。**
伺服器部署本來就不該有 `.env` 檔，這是正常狀態，不應該讓程式起不來。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: 專案根目錄（此檔案位於 <root>/vibcore/env.py）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 預設的設定檔路徑
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

_loaded = False


def load_env(path: str | Path | None = None, override: bool = False) -> bool:
    """
    載入 .env 檔到環境變數。

    Args:
        path: 設定檔路徑；None 表示用專案根目錄的 `.env`
        override: 是否覆蓋已存在的環境變數。**預設 False**，理由見模組說明——
                  正式機上的系統環境變數必須贏過檔案，否則一個忘了刪的開發用
                  `.env` 會讓服務安靜地連到錯的資料庫

    Returns:
        是否真的載入了檔案（檔案不存在或缺套件時回傳 False，不視為錯誤）
    """
    global _loaded

    env_path = Path(path) if path else DEFAULT_ENV_PATH

    if not env_path.exists():
        logger.debug(f"未找到設定檔 {env_path}，改用系統環境變數")
        return False

    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning(
            f"找到 {env_path} 但未安裝 python-dotenv，設定檔不會生效。"
            f"請執行 pip install python-dotenv，或改用系統環境變數"
        )
        return False

    load_dotenv(env_path, override=override)
    _loaded = True
    logger.debug(f"已載入設定檔 {env_path}（override={override}）")
    return True


def describe_config() -> str:
    """
    產生目前生效設定的摘要，供啟動時列印或排查用。

    **密碼一律不顯示明文**，只回報有沒有設定——排查連線問題時需要知道
    「密碼是不是空的」，但日誌不該留下密碼。
    """
    def _mask(name: str) -> str:
        return "（已設定）" if os.environ.get(name) else "（未設定）"

    return "\n".join([
        f"  VIB_DB_HOST     = {os.environ.get('VIB_DB_HOST', 'localhost（預設）')}",
        f"  VIB_DB_PORT     = {os.environ.get('VIB_DB_PORT', '5432（預設）')}",
        f"  VIB_DB_NAME     = {os.environ.get('VIB_DB_NAME', 'vibration（預設）')}",
        f"  VIB_DB_USER     = {os.environ.get('VIB_DB_USER', 'vibcore（預設）')}",
        f"  VIB_DB_PASSWORD {_mask('VIB_DB_PASSWORD')}",
        f"  VIB_AGENT_API_KEY {_mask('VIB_AGENT_API_KEY')}",
    ])
