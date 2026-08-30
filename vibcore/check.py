"""
check.py — 環境自檢

用途：建好資料庫、設好 `.env` 之後，用這支確認整套設定接得起來，
並告訴你下一步該做什麼。

    python -m vibcore.check

刻意設計成**不會修改任何資料**——只做讀取與檢查，可以隨時重複執行。
"""

from __future__ import annotations

import logging
import sys

from vibcore.env import DEFAULT_ENV_PATH, describe_config

logger = logging.getLogger(__name__)

OK = "✓"
FAIL = "✗"
WARN = "!"

#: schema.sql 應建立的資料表；缺任何一張都代表 schema 沒套用完整
EXPECTED_TABLES = (
    "app_user", "app_role", "user_role",
    "device", "measure_point",
    "measurement_agg", "measurement_daily", "raw_file", "ingestion_log",
    "tag_mapping", "scada_reading",
    "point_baseline", "iso_threshold", "rule_config", "sla_config",
    "finding", "finding_note", "finding_status_history",
    "weekly_report", "audit_log",
)


def _line(mark: str, text: str) -> None:
    print(f"  {mark} {text}")


def check_env() -> None:
    print("\n【1】設定來源")
    if DEFAULT_ENV_PATH.exists():
        _line(OK, f"找到設定檔 {DEFAULT_ENV_PATH}")
    else:
        _line(WARN, f"沒有 {DEFAULT_ENV_PATH}，將使用系統環境變數或預設值")
        _line("", "  本機開發可執行：cp .env.example .env")
    print("\n  目前生效的設定：")
    print(describe_config())


def check_db() -> bool:
    """回傳連線是否成功。"""
    print("\n【2】資料庫連線")
    try:
        from vibcore.db.connection import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                ver = cur.fetchone()["version"].split(",")[0]
        _line(OK, f"連線成功（{ver}）")
        return True
    except Exception as e:
        _line(FAIL, f"連線失敗：{e}")
        print("\n  常見原因：")
        print("    · PostgreSQL 未啟動，或 VIB_DB_HOST / VIB_DB_PORT 不正確")
        print("    · 資料庫尚未建立 → createdb <VIB_DB_NAME>")
        print("    · 帳號密碼錯誤，或該帳號沒有連線權限")
        return False


def check_schema() -> bool:
    """
    檢查資料表是否齊全，並區分「表不存在」與「表存在但沒權限」。

    這兩種情況的處置完全相反——前者要跑 schema.sql，後者要 GRANT——但用
    `information_schema.tables` 查都是回傳空集合，因為該檢視會依當前帳號的
    權限過濾。若把「查不到」一律當成「沒建表」，就會叫使用者重跑 schema.sql，
    而那完全解決不了權限問題（實際情境：用 postgres 建表、用 vibapp 連線）。

    因此改為三個獨立來源交叉判斷：

      pg_tables            系統目錄，不受權限過濾 → 表到底存不存在
      has_table_privilege  逐表查 SELECT 權限     → 當前帳號能不能讀
      pg_namespace         → schema 本身在不在

    `has_table_privilege` 而非 `information_schema` 是刻意的：後者只要帳號
    擁有任一種權限就會列出，即使 SELECT 已被收回也照樣可見，程式跑起來才會
    炸。這裡問的是實際會用到的那個權限。
    """
    print("\n【3】資料表")
    from vibcore.db.connection import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 事實：表是否存在（系統目錄，不受權限影響）
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'vib'")
            actual = {r["tablename"] for r in cur.fetchall()}

            # 當前帳號實際有沒有 SELECT 權限。
            # 刻意不用 information_schema.tables 判斷——該檢視只要帳號擁有
            # 任一種權限就會列出，即使 SELECT 已被收回也照樣可見，程式跑起來
            # 才會炸。has_table_privilege 問的是真正需要的那個權限。
            cur.execute(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'vib' "
                "  AND has_table_privilege(current_user, "
                "        quote_ident(schemaname) || '.' || quote_ident(tablename), 'SELECT')"
            )
            visible = {r["tablename"] for r in cur.fetchall()}

            cur.execute("SELECT current_user AS u")
            current_user = cur.fetchone()["u"]

            cur.execute("SELECT count(*) AS n FROM pg_namespace WHERE nspname = 'vib'")
            schema_exists = cur.fetchone()["n"] > 0

            # 表在 public 而非 vib？（schema.sql 被改動或手動建表時可能發生）
            cur.execute(
                "SELECT count(*) AS n FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = ANY(%s)",
                (list(EXPECTED_TABLES),),
            )
            in_public = cur.fetchone()["n"]

    # ── 情況一：schema 或表根本不存在 ──
    if not schema_exists or not actual:
        if in_public:
            _line(FAIL, f"表建在 public schema（{in_public} 張），但本系統預期在 vib")
            print("\n  db/schema.sql 開頭會 CREATE SCHEMA vib 並切換 search_path。")
            print("  若表跑到 public，可能是執行了修改過的版本。建議重新建庫：")
            print("    dropdb <資料庫名> && createdb <資料庫名>")
            print("    psql -d <資料庫名> -f db/schema.sql")
        else:
            _line(FAIL, "vib schema 內沒有任何資料表")
            print("\n  請執行：psql -d <資料庫名> -f db/schema.sql")
        return False

    # ── 情況二：表存在，但當前帳號看不到 → 權限問題 ──
    if actual and not visible:
        _line(FAIL, f"表存在（{len(actual)} 張）但帳號 {current_user} 沒有 SELECT 權限")
        print("\n  這不是建表問題，重跑 schema.sql 沒有用。")
        print(f"  多數情況是用 postgres 建表、但 .env 設定用 {current_user} 連線。")
        print("\n  請以建表的帳號（通常是 postgres）執行授權：")
        print(f"""
    psql -d <資料庫名> <<'SQL'
    GRANT USAGE ON SCHEMA vib TO {current_user};
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vib TO {current_user};
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vib TO {current_user};
    ALTER DEFAULT PRIVILEGES IN SCHEMA vib
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {current_user};
    ALTER DEFAULT PRIVILEGES IN SCHEMA vib
        GRANT USAGE, SELECT ON SEQUENCES TO {current_user};
    SQL
""")
        print("  或改用建表的帳號連線（修改 .env 的 VIB_DB_USER）。")
        return False

    # ── 情況三：部分可見 → 權限給了一半 ──
    missing_from_view = actual - visible
    if missing_from_view:
        _line(FAIL, f"帳號 {current_user} 只對 {len(visible)}/{len(actual)} 張表有 SELECT 權限")
        print(f"\n  缺少權限的表：{', '.join(sorted(missing_from_view))}")
        print("  權限可能只授權了部分表，建議用上方的 GRANT ... ON ALL TABLES 重新授權。")
        return False

    # ── 情況四：真的缺表 ──
    missing = [t for t in EXPECTED_TABLES if t not in actual]
    if missing:
        _line(FAIL, f"缺少 {len(missing)} 張表：{', '.join(missing)}")
        print("\n  schema.sql 可能只套用了一部分，請重新完整執行一次")
        return False

    _line(OK, f"{len(EXPECTED_TABLES)} 張表齊全，帳號 {current_user} 可正常存取")
    return True


def check_seed() -> None:
    print("\n【4】預設資料")
    from vibcore.db.connection import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table, label, expect in (
                ("rule_config", "判定規則", 14),
                ("iso_threshold", "ISO 門檻等級", 4),
                ("sla_config", "簽核階段 SLA", 3),
                ("app_role", "角色定義", 5),
            ):
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                n = cur.fetchone()["n"]
                mark = OK if n >= expect else WARN
                _line(mark, f"{label}：{n} 筆" + ("" if n >= expect else f"（預期 {expect}）"))


def check_data() -> dict:
    print("\n【5】營運資料")
    from vibcore.db.connection import get_connection
    stats = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for key, table, label in (
                ("devices", "device", "設備"),
                ("points", "measure_point", "量測點"),
                ("agg", "measurement_agg", "每小時聚合"),
                ("findings", "finding", "事項"),
                ("users", "app_user", "使用者"),
            ):
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                stats[key] = cur.fetchone()["n"]
                _line("·", f"{label}：{stats[key]:,} 筆")

            # 台帳完整度：這兩項 CSV 裡沒有，需人工補，漏掉會影響判定
            if stats["devices"]:
                cur.execute(
                    "SELECT count(*) AS n FROM device WHERE iso_class_source = 'unset'"
                )
                unset = cur.fetchone()["n"]
                if unset:
                    _line(WARN, f"{unset} 台設備未設定 ISO 等級 → 不會套用 Zone 判定，"
                                f"僅以相對趨勢監測")
    return stats


def suggest_next(stats: dict) -> None:
    print("\n【6】下一步")
    if not stats.get("devices"):
        print("  尚未匯入任何資料。建議順序：")
        print("    1. 先用歷史資料跑離線回測校準門檻（重要，見 docs/USAGE.md §三）")
        print("       python -m validate.offline --data-dir <歷史資料夾>")
        print("    2. 確認門檻合理後，執行每日排程")
        print("       python -m vibcore.pipeline.daily --data-dir <當日資料夾>")
    elif not stats.get("findings"):
        print("  已有設備資料但尚無事項。可能是資料還不足以判定，或確實無異常。")
        print("    · 查資料狀態分佈：見 docs/USAGE.md §七「每天都沒有任何 Finding」")
    else:
        print("  資料鏈路已運作。可以：")
        print("    · 啟動 API 供 Agent 呼叫")
        print("      uvicorn vibcore.api.main:app --host 0.0.0.0 --port 8000")
        print("    · 查看待處理事項")
        print("      SELECT * FROM vib.v_open_finding;")


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")

    print("=" * 60)
    print("  振動監測平台 — 環境自檢")
    print("=" * 60)

    check_env()

    if not check_db():
        print("\n" + "=" * 60)
        print("  連線失敗，後續檢查略過")
        print("=" * 60)
        return 1

    if not check_schema():
        print("\n" + "=" * 60)
        print("  schema 不完整，後續檢查略過")
        print("=" * 60)
        return 1

    check_seed()
    stats = check_data()
    suggest_next(stats)

    print("\n" + "=" * 60)
    print("  自檢完成")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
