-- =============================================================
-- migration 002：finding 留存觸發當下的數值與門檻快照，新增 observe 嚴重度
--
-- 適用對象：已經用舊版 db/schema.sql 建好資料庫的環境——
--   finding.severity / finding.peak_severity 只接受 ('err','warn','ok')、
--   rule_config.severity 只接受 ('err','warn')、
--   finding 表沒有 trigger_params 欄位，
--   baseline_value/current_value/value_unit/evidence/interpretation_limit
--   雖然已存在，但仍是可為 NULL、無預設值的舊版定義。
-- 全新建庫者不需要執行——schema.sql 已包含這些欄位與約束（惟目前
-- schema.sql 的 finding 表對這五欄有重複宣告的問題，見檔尾附註）。
--
-- 可重複執行：ADD COLUMN 用 IF NOT EXISTS；CHECK 約束的 DROP 用
-- pg_constraint 動態比對欄位（見下方說明），不依賴約束名稱；
-- backfill 與 SET NOT NULL / SET DEFAULT 天生冪等。不影響既有資料——
-- 既有列的 trigger_params 會是空物件 '{}'，其餘既有欄位維持原值。
-- =============================================================

SET search_path TO vib, public;

BEGIN;

-- ─────────────────────────────────────────────────────────
-- 一、finding 表新增欄位
-- ─────────────────────────────────────────────────────────
--
-- baseline_value / current_value / value_unit / evidence /
-- interpretation_limit 這五欄在更早的版本就已存在（僅 trigger_params
-- 是這次真正新增的欄位），但當時是「可為 NULL、無預設值」的寬鬆定義。
-- 這裡用 ADD COLUMN IF NOT EXISTS 涵蓋兩種情境：
--   (a) 資料庫從更早的版本升級，五欄都還沒有 → 一併補上
--   (b) 資料庫已有這五欄 → IF NOT EXISTS 讓這幾行變成無害的 no-op，
--       真正要補的是後面的 NOT NULL / DEFAULT，不能只做 ADD COLUMN。
ALTER TABLE finding
    ADD COLUMN IF NOT EXISTS baseline_value NUMERIC(16,6),
    ADD COLUMN IF NOT EXISTS current_value  NUMERIC(16,6),
    ADD COLUMN IF NOT EXISTS value_unit     TEXT,
    ADD COLUMN IF NOT EXISTS evidence       JSONB,
    ADD COLUMN IF NOT EXISTS interpretation_limit TEXT,
    -- 觸發當下 rule_config.params 的快照。規則參數日後會被調整，只存
    -- 數值而不存當時的門檻，回溯時就分不清是數值變了還是門檻被調過。
    ADD COLUMN IF NOT EXISTS trigger_params JSONB;

-- 補齊既有列（新欄位、或舊版遺留的 NULL）再收緊為 NOT NULL DEFAULT，
-- 順序必須是「先 backfill 再 SET NOT NULL」——否則既有列若真有 NULL，
-- SET NOT NULL 會直接失敗，卡住整支 migration。
UPDATE finding SET value_unit = ''      WHERE value_unit IS NULL;
UPDATE finding SET evidence   = '{}'::jsonb WHERE evidence   IS NULL;
UPDATE finding SET trigger_params      = '{}'::jsonb WHERE trigger_params IS NULL;
UPDATE finding SET interpretation_limit = '' WHERE interpretation_limit IS NULL;

ALTER TABLE finding
    ALTER COLUMN value_unit           SET DEFAULT '',
    ALTER COLUMN value_unit           SET NOT NULL,
    ALTER COLUMN evidence             SET DEFAULT '{}'::jsonb,
    ALTER COLUMN evidence             SET NOT NULL,
    ALTER COLUMN trigger_params       SET DEFAULT '{}'::jsonb,
    ALTER COLUMN trigger_params       SET NOT NULL,
    ALTER COLUMN interpretation_limit SET DEFAULT '',
    ALTER COLUMN interpretation_limit SET NOT NULL;
-- baseline_value / current_value 刻意維持可為 NULL、無預設值：規則第
-- 一次判定時可能就是沒有基準（尚未建立基準期）或沒有單一數值可報
-- （例如純事件型判定），NOT NULL 會逼著在那種情況塞一個假數字，
-- 之後回溯分析反而分不清「真的是 0」還是「當初沒有值」。

COMMENT ON COLUMN finding.baseline_value IS
    '觸發當下的基準值（來自 point_baseline），供之後回溯重算「若門檻改成 X 會剩幾件」';
COMMENT ON COLUMN finding.current_value IS
    '觸發當下的量測值；同一 finding_key 再次觸發時覆寫為最新一次的值（見下方 upsert 語意說明）';
COMMENT ON COLUMN finding.value_unit IS
    'current_value / baseline_value 的單位，避免只看數字誤判量級';
COMMENT ON COLUMN finding.evidence IS
    '判定依據的完整數值（例如各特徵的 σ 分解），供 agent 或人工回溯查核';
COMMENT ON COLUMN finding.trigger_params IS
    '觸發當下 rule_config.params 的快照。只存數值而不存當時的門檻，'
    '回溯時就分不清是數值變了還是門檻被調過——門檻要靠實際誤報率迭代，'
    '沒有這份快照，事後只能重跑整條管線（而歷史原始檔未必還在）';
COMMENT ON COLUMN finding.interpretation_limit IS
    '明確標示此證據的解讀邊界，供 agent 遵守「不臆測故障類型」的護欄';

-- ─────────────────────────────────────────────────────────
-- 二、放寬 CHECK 約束以接受新嚴重度 'observe'
-- ─────────────────────────────────────────────────────────
--
-- PostgreSQL 修改 CHECK 約束必須先 DROP 再 ADD，但 CHECK 約束的名稱是
-- 系統自動產生的（例如 finding_severity_check），不同版本的 PostgreSQL
-- 或不同的建表方式（CREATE TABLE 內聯 vs. 事後 ALTER TABLE ADD
-- CONSTRAINT）可能產生不同名稱，寫死名稱在「不同環境都能執行」這個
-- 要求下不可靠。
--
-- 這裡改用 pg_constraint 動態找出「掛在指定欄位上的單欄 CHECK 約束」：
-- 用 conkey（約束涉及的欄位 attnum 陣列）比對該欄位的 attnum，只要
-- conkey 剛好等於「該欄位」這一個元素的陣列，就認定是我們要換掉的
-- 約束——不靠名稱、也不靠猜測約束內容的文字（LIKE '%severity%' 這種
-- 寫法在 severity / peak_severity 兩欄位互相是子字串時會誤配對）。
--
-- 找到後一律 DROP，再用固定名稱重新 ADD——固定名稱只是為了讓約束在
-- \d 底下有可讀的名字，函式本身不依賴這個名稱來判斷是否要 DROP，
-- 所以就算重複執行本 migration、或約束名稱在別的環境本來就不同，
-- 都能正常收斂到「只剩一個涵蓋 observe 的約束」。
DO $mig$
DECLARE
    v_relid   regclass;
    v_attnum  smallint;
    v_conname name;
BEGIN
    -- finding.severity
    v_relid  := 'finding'::regclass;
    SELECT attnum INTO v_attnum FROM pg_attribute
        WHERE attrelid = v_relid AND attname = 'severity' AND NOT attisdropped;
    FOR v_conname IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = v_relid AND contype = 'c' AND conkey = ARRAY[v_attnum]
    LOOP
        EXECUTE format('ALTER TABLE finding DROP CONSTRAINT %I', v_conname);
    END LOOP;
    ALTER TABLE finding ADD CONSTRAINT finding_severity_check
        CHECK (severity IN ('err','warn','observe','ok'));

    -- finding.peak_severity
    SELECT attnum INTO v_attnum FROM pg_attribute
        WHERE attrelid = v_relid AND attname = 'peak_severity' AND NOT attisdropped;
    FOR v_conname IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = v_relid AND contype = 'c' AND conkey = ARRAY[v_attnum]
    LOOP
        EXECUTE format('ALTER TABLE finding DROP CONSTRAINT %I', v_conname);
    END LOOP;
    ALTER TABLE finding ADD CONSTRAINT finding_peak_severity_check
        CHECK (peak_severity IN ('err','warn','observe','ok'));

    -- rule_config.severity（不含 'ok'——rule_config 描述的是規則設定，
    -- 不是判定結果，本來就沒有 'ok' 這個狀態）
    v_relid := 'rule_config'::regclass;
    SELECT attnum INTO v_attnum FROM pg_attribute
        WHERE attrelid = v_relid AND attname = 'severity' AND NOT attisdropped;
    FOR v_conname IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = v_relid AND contype = 'c' AND conkey = ARRAY[v_attnum]
    LOOP
        EXECUTE format('ALTER TABLE rule_config DROP CONSTRAINT %I', v_conname);
    END LOOP;
    ALTER TABLE rule_config ADD CONSTRAINT rule_config_severity_check
        CHECK (severity IN ('err','warn','observe'));
END;
$mig$;

COMMIT;

-- =============================================================
-- 附註：db/schema.sql 目前的已知問題（不在本 migration 處理範圍內，
-- 因為 db/schema.sql 不屬於這次任務的檔案歸屬）
--
-- CREATE TABLE finding 內，baseline_value / current_value / value_unit /
-- evidence / interpretation_limit 這五欄各被宣告了兩次（一次在「追蹤」
-- 區塊、一次在「觸發當下的數值、門檻與證據」區塊），會讓 PostgreSQL
-- 直接報錯 "column ... specified more than once"，導致任何全新環境
-- 用 schema.sql 建庫都會整支交易失敗、完全無法建表（本檔案已用
-- BEGIN/COMMIT 包住整段 CREATE，一報錯就整個 ROLLBACK）。
-- 這與本 migration（給「已建好庫」的環境用）無關，但會讓「全新建庫」
-- 這條路徑目前是壞的，請務必請 schema.sql 的維護者（本次任務要求
-- 不得由本檔案的作者修改）合併這兩個重複區塊。
-- =============================================================
