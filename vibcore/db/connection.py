"""
connection.py — PostgreSQL 連線與交易管理

為何要集中在這裡而不是讓每個呼叫端各自 `psycopg2.connect()`：

1. **連線參數只能來自環境變數**——部署環境（本機測試 / 內網伺服器）的
   DB 位置不同，寫死在程式碼裡會在換機器時忘記改。
2. **交易邊界必須明確**：Finding 的四階段簽核（`transition_status`）要
   同時更新 `finding` 與寫入 `finding_status_history`，兩者必須在同一
   交易內成功或一起失敗，否則歷程表會跟主表對不上。用 context manager
   包住「連線 → 交易 → commit/rollback → 關閉」，呼叫端不需要自己記得
   处理例外時要 rollback。
3. **search_path 固定為 `vib, public`**：`db/schema.sql` 把所有表建在
   `vib` schema 下，但只有執行 migration 的那個 session 設過
   `search_path`；新連線預設是 `public`，若不在連線時重設，
   repository.py 裡不加 schema 前綴的 SQL 會全部查不到表。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg2
import psycopg2.extensions
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

#: schema.sql 建表所在的 schema；連線後固定塞進 search_path，
#: 讓 repository.py 的 SQL 不必到處寫 `vib.xxx` 前綴。
DB_SCHEMA = "vib"


@dataclass(frozen=True)
class DBConfig:
    """
    連線參數。

    刻意用 dataclass 而非直接讀環境變數塞進 connect()——測試時需要覆寫
    port（起臨時 PG instance 常用非預設 port），用 dataclass 可以直接
    建一個測試用的 config 物件，不必動環境變數。
    """

    host: str = "localhost"
    port: int = 5432
    dbname: str = "vibration"
    user: str = "vibcore"
    password: str = ""

    @classmethod
    def from_env(cls) -> "DBConfig":
        """
        從環境變數讀取設定，缺省者採合理預設。

        預設值刻意貼近本機開發情境（localhost / 5432 / 專案慣用的
        db 名稱），部署到伺服器時一定會設環境變數覆蓋，不會誤用預設值
        連到正式機。
        """
        return cls(
            host=os.environ.get("VIB_DB_HOST", cls.host),
            port=int(os.environ.get("VIB_DB_PORT", cls.port)),
            dbname=os.environ.get("VIB_DB_NAME", cls.dbname),
            user=os.environ.get("VIB_DB_USER", cls.user),
            password=os.environ.get("VIB_DB_PASSWORD", cls.password),
        )


def _connect_raw(config: DBConfig) -> psycopg2.extensions.connection:
    """建立底層連線並固定 search_path；不處理交易生命週期。"""
    conn = psycopg2.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
        cursor_factory=RealDictCursor,
    )
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {DB_SCHEMA}, public")
    conn.commit()
    return conn


@contextmanager
def get_connection(config: DBConfig | None = None) -> Iterator[psycopg2.extensions.connection]:
    """
    連線 + 交易的最外層邊界：正常結束時 commit，例外時 rollback，離開時關閉連線。

    這是給呼叫端（排程腳本、API handler）用的入口——一次商業操作（例如
    「跑完一次規則引擎並 upsert 所有 findings」）應該對應一個 with 區塊，
    要嘛全部成功要嘛全部不算數，不會留下「部分 finding 已更新、部分沒有」
    的中間態。

    repository.py 裡的函式一律只接收已開啟的 `conn`，不自己 commit/close，
    交易邊界統一由呼叫端透過這個 context manager 決定。
    """
    cfg = config or DBConfig.from_env()
    conn = _connect_raw(cfg)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("交易失敗，已 rollback")
        raise
    finally:
        conn.close()


@contextmanager
def transaction(conn: psycopg2.extensions.connection) -> Iterator[psycopg2.extensions.connection]:
    """
    在既有連線上開一個 SAVEPOINT，供需要「這一段要嘛全部成功要嘛全部不算數，
    但不想連帶影響呼叫端已經做的其他變更」的情境使用。

    例如：批次匯入時想「這個 point 的資料寫失敗就跳過，其餘 point 繼續」，
    若不用 SAVEPOINT，一次 execute 失敗會讓整個外層交易進入
    「aborted，只能 rollback」的狀態，後面所有操作都會被 PostgreSQL 拒絕。
    """
    savepoint = f"sp_{id(conn):x}_{os.getpid()}"
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {savepoint}")
    try:
        yield conn
        with conn.cursor() as cur:
            cur.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        with conn.cursor() as cur:
            cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        raise
