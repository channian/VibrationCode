"""
vibcore — 振動監測核心套件

匯入本套件時會自動載入專案根目錄的 `.env`（若存在），讓 CLI、API、
測試腳本不必各自處理設定載入。真實環境變數優先，`.env` 不覆蓋既有值
（理由見 `vibcore/env.py`）。
"""

from vibcore.env import load_env as _load_env

_load_env()
