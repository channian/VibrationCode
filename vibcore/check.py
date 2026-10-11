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
    "scada_tag", "tag_mapping", "scada_reading",
    "point_baseline", "iso_threshold", "rule_config", "sla_config",
    "finding", "finding_note", "finding_status_history", "observation",
    "weekly_report", "audit_log",
)

#: 這些表不是 schema.sql 的原始內容，是後來才加的；舊庫缺它們時要補套用對應的 migration，
#: 而不是重跑 schema.sql（重跑會在既有物件上報錯，叫使用者做一件沒有用的事）。
TABLE_MIGRATION = {
    "observation": "migration_003_observation.sql",
    "scada_tag": "migration_006_tag_many_to_many.sql",
}

#: 現行的 14 條規則。migration_008 補的就是舊庫缺的那幾列。
EXPECTED_RULES = (
    "ISO_ZONE", "VEL_HIGH", "IMPACT_RISE", "DEGRADE_TREND", "SPECTRAL_SHIFT", "AXIS_SHIFT",
    "STEP_CHANGE", "ORIENTATION_CHANGE", "SENSOR_OFFLINE", "DATA_QUALITY", "SENSOR_SATURATION",
    "STANDBY_NO_RUNTIME", "ISO_CLASS_SUSPECT", "TEMP_RISE",
)

#: 派工政策：這六條沒有外部標準可引用，預設只進週報的觀察名單（observe），不建立 Finding。
#: 在資料庫被設成 warn／err 的話，正式排程會照設定派工——試跑期不建議（docs/TRIAL_RUN_PLAN.md §三）。
OBSERVE_POLICY_RULES = (
    "IMPACT_RISE", "DEGRADE_TREND", "SPECTRAL_SHIFT", "AXIS_SHIFT", "STEP_CHANGE", "TEMP_RISE",
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
        from_migration = sorted({TABLE_MIGRATION[t] for t in missing if t in TABLE_MIGRATION})
        if from_migration:
            print("\n  缺的表是後來的 migration 才加入的，既有的庫請補套用（不要重跑 schema.sql）：")
            for f in from_migration:
                print(f"    db/{f}")
            print("  沒有 psql 的環境：用 SQL 用戶端把整支檔案當一份腳本執行。")
        if set(missing) - set(TABLE_MIGRATION):
            print("\n  schema.sql 可能只套用了一部分，請重新完整執行一次")
        return False

    _line(OK, f"{len(EXPECTED_TABLES)} 張表齊全，帳號 {current_user} 可正常存取")
    return True


# ──────────────────────────────────────────────────────────
# 結構版本：舊庫缺了哪些 migration
# ──────────────────────────────────────────────────────────
#
# 資料庫沒有記錄「套用過哪些 migration」，而且有些 migration 重跑並不安全：
# migration_004 會把 IMPACT_RISE 的參數改回含 kurtosis 通道的舊版、把 ISO_ZONE／VEL_HIGH 的
# consecutive_readings 重設為 3（實測：現場調成 5 的值被改回 3）。所以不能叫使用者「全部重跑一遍」，
# 只能問「結構與資料現在是不是長這樣」，缺哪支補哪支。
# 對照實驗：從最早的 schema.sql 依序套滿 001～008，結構與種子資料和全新建庫逐項相同。

def _has_table(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'vib' AND tablename = %s", (name,))
    return cur.fetchone() is not None


def _has_column(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'vib' AND table_name = %s AND column_name = %s", (table, column))
    return cur.fetchone() is not None


def _rule_params(cur, rule_code: str) -> dict | None:
    cur.execute("SELECT params FROM rule_config WHERE rule_code = %s", (rule_code,))
    row = cur.fetchone()
    return None if row is None else dict(row["params"] or {})


def _probe_004(cur) -> bool:
    return (_has_column(cur, "device", "iso_machine_group")
            and _has_column(cur, "iso_threshold", "machine_group")
            and not _has_column(cur, "device", "iso_machine_class")
            and _has_column(cur, "measurement_agg", "acc_crest_median"))


def _probe_005(cur) -> bool | None:
    p = _rule_params(cur, "IMPACT_RISE")
    return None if p is None else not ({"kurt_sigma", "kurt_axis_sigma", "require_both"} & p.keys())


def _probe_006(cur) -> bool:
    return _has_table(cur, "scada_tag") and not _has_column(cur, "tag_mapping", "variable_type")


def _probe_007(cur) -> bool | None:
    p = _rule_params(cur, "VEL_HIGH")
    return None if p is None else {"min_rel_change", "min_abs_change", "fallback_severity"} <= p.keys()


def _probe_008(cur) -> bool:
    cur.execute("SELECT count(*) AS n FROM rule_config WHERE rule_code = ANY(%s)",
                (list(EXPECTED_RULES),))
    return cur.fetchone()["n"] == len(EXPECTED_RULES)


#: (編號, 檔名, 說明, 探針)；探針回傳 True＝已套用、False＝尚未、None＝無法判斷
MIGRATIONS = (
    ("001", "migration_001_temp_iso_axis.sql", "溫度、前端 ISO 分級、逐軸衝擊指標欄位",
     lambda c: _has_column(c, "measurement_agg", "temp_avg")),
    ("002", "migration_002_finding_trigger_values.sql", "事項留存觸發當下的門檻快照、observe 嚴重度",
     lambda c: _has_column(c, "finding", "trigger_params")),
    ("003", "migration_003_observation.sql", "觀察名單表",
     lambda c: _has_table(c, "observation")),
    ("004", "migration_004_iso10816_3_and_median.sql", "ISO 10816-3 分類結構、median 通道", _probe_004),
    ("004b", "migration_004_iso10816_3_and_median.sql 檔尾的「migration 004b」區段（三個 ADD COLUMN）",
     "感測器軸向欄位",
     lambda c: _has_column(c, "measurement_agg", "axis_energy_by_direction")),
    ("005", "migration_005_drop_kurtosis_channel.sql", "IMPACT_RISE 移除 kurtosis 通道", _probe_005),
    ("006", "migration_006_tag_many_to_many.sql", "SCADA tag 與設備改多對多", _probe_006),
    ("007", "migration_007_vel_high_fallback_observe.sql", "VEL_HIGH 實用顯著性下限與退回嚴重度", _probe_007),
    ("008", "migration_008_rule_config_seed_alignment.sql", "規則設定對齊現行預設（補缺的規則、觀察級嚴重度）",
     _probe_008),
)


def migration_status(cur) -> list[tuple[str, str, str, bool | None]]:
    """每支 migration 的套用狀態：(編號, 檔名, 說明, True／False／None)。"""
    return [(key, fname, summary, probe(cur)) for key, fname, summary, probe in MIGRATIONS]


def check_migrations() -> bool:
    """回傳是否所有 migration 都已套用（無法判斷的不算未套用，但會提醒）。"""
    print("\n【3b】結構版本（migration）")
    from vibcore.db.connection import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            status = migration_status(cur)

    todo = [(k, f, s) for k, f, s, ok in status if ok is False]
    unknown = [(k, f, s) for k, f, s, ok in status if ok is None]
    if not todo and not unknown:
        _line(OK, f"migration 001～008 都已套用（共 {len(status)} 項檢查）")
        return True
    for k, f, s, ok in status:
        if ok is False:
            _line(FAIL, f"{k} 尚未套用：{s}  → db/{f}")
        elif ok is None:
            _line(WARN, f"{k} 無法判斷：{s}（找不到對應的規則設定列）")
    if todo:
        print("\n  請依編號順序，只執行上面標 ✗ 的檔案（沒有 psql 就用 SQL 用戶端把整支檔案當一份腳本）。")
        print("  已經套用的不要重跑：migration_004 會把 IMPACT_RISE 與 ISO_ZONE／VEL_HIGH 的")
        print("  consecutive_readings 重設為預設值，現場調過的值會被安靜地蓋掉。")
    return not todo


def policy_deviations(cur) -> dict[str, list]:
    """資料庫設定與派工政策不一致的地方（只讀）。"""
    cur.execute("SELECT rule_code, severity, is_active, params FROM rule_config")
    rows = {r["rule_code"]: r for r in cur.fetchall()}
    return {
        "observe_but_dispatching": sorted(
            c for c in OBSERVE_POLICY_RULES if c in rows and rows[c]["severity"] != "observe"),
        "disabled": sorted(c for c, r in rows.items() if not r["is_active"]),
        "vel_high_fallback": (
            (rows["VEL_HIGH"]["params"] or {}).get("fallback_severity")
            if "VEL_HIGH" in rows else None),
    }


def check_policy(cur) -> None:
    dev = policy_deviations(cur)
    bad = dev["observe_but_dispatching"]
    if bad:
        _line(WARN, f"設定為觀察級政策的規則在資料庫被設成會派工：{', '.join(bad)}")
        _line("·", "這些規則沒有外部標準可引用，正式排程會照設定建立 Finding 派工。試跑期建議維持 observe：")
        _line("·", "UPDATE vib.rule_config SET severity = 'observe' WHERE rule_code IN ("
                   + ", ".join(f"'{c}'" for c in bad) + ");")
        _line("·", "（若是試跑後依回饋刻意升級的，請忽略這一行）")
    else:
        _line(OK, "觀察級規則（衝擊性、趨勢、頻譜、軸向、突變、溫升）都只進觀察名單，不派工")
    if dev["disabled"]:
        _line("·", f"已停用的規則：{', '.join(dev['disabled'])}")
    fb = dev["vel_high_fallback"]
    if fb not in (None, "observe"):
        _line(WARN, f"VEL_HIGH 退回相對基準時的嚴重度是 {fb}（預設 observe）→ 額定 ≤ 15 kW 的泵會派工")


def check_seed() -> None:
    print("\n【4】預設資料")
    from vibcore.db.connection import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table, label, expect in (
                ("rule_config", "判定規則", len(EXPECTED_RULES)),
                ("iso_threshold", "ISO 門檻等級", 8),
                ("sla_config", "簽核階段 SLA", 3),
                ("app_role", "角色定義", 5),
            ):
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                n = cur.fetchone()["n"]
                mark = OK if n >= expect else WARN
                _line(mark, f"{label}：{n} 筆" + ("" if n >= expect else f"（預期 {expect}）"))
            check_policy(cur)


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
                    _line("·", "台帳在 CSV 裡的話，用這支匯入（先加 --dry-run 預演）："
                               "python -m vibcore.ledger_import --csv <台帳.csv>")
                cur.execute("SELECT count(*) AS n FROM device WHERE is_standby")
                n_standby = cur.fetchone()["n"]
                _line("·", f"標為備機：{n_standby} 台")
                if n_standby == 0:
                    _line(WARN, "沒有任何設備標為備機 → STANDBY_NO_RUNTIME 完全不會判定。"
                                "若現場確有備機，請補台帳（這項安靜失效，不會有任何錯誤訊息）")
    return stats


def suggest_next(stats: dict) -> None:
    print("\n【6】下一步")
    if not stats.get("devices"):
        print("  尚未匯入任何資料。建議順序（完整說明見 docs/TRIAL_RUN_PLAN.md §四）：")
        print("    1. 把最近 6～8 週的歷史 Analytic 檔放進一個資料夾，用最新一天的日期跑一次每日排程")
        print("       （整份檔案都會入庫、基準期自動建立；第一次執行也會依資料建立設備與量測點）")
        print("       python -m vibcore.pipeline.daily --data-dir <歷史資料夾> --date <最新一天 YYYY-MM-DD>")
        print("    2. 匯入台帳（ISO 群組、基礎剛性、額定功率、是否備機）——不做這步，所有設備")
        print("       停在「未分級」，ISO 判定與備機規則都不會運作，而且沒有任何錯誤訊息")
        print("       python -m vibcore.ledger_import --csv <台帳.csv> --dry-run   # 先預演")
        print("       python -m vibcore.ledger_import --csv <台帳.csv>")
        print("    3. 再跑一次同樣的每日排程指令（讓台帳的分類與備機旗標生效）")
        print("    4. 產出第一份週報（不經 Agent）")
        print("       python -m vibcore.reporting.cli --days 7 --end-date <最新一天> --out output/weekly.html --save")
        print("    （離線回測校準門檻見 docs/USAGE.md §三；建議在正式跑之前做過）")
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

    migrations_ok = check_migrations()
    check_seed()
    stats = check_data()
    suggest_next(stats)

    print("\n" + "=" * 60)
    if migrations_ok:
        print("  自檢完成")
    else:
        print("  自檢完成，但有 migration 尚未套用（見【3b】）——補套用之前，不建議開始跑每日排程")
    print("=" * 60)
    return 0 if migrations_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
