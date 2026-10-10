-- =============================================================
-- migration 006：SCADA tag 與設備改為多對多
--
-- 適用對象：已經用舊版 db/schema.sql 建好資料庫的環境。
-- 全新建庫者不需要執行——schema.sql 已包含本變更。
--
-- 可重複執行：已是新結構（tag_mapping 沒有 variable_type 欄）時整段略過。
--
-- 為什麼要改：舊結構以 tag_id 為 tag_mapping 的主鍵，假設一個 tag 只屬於
-- 一台設備。但一台馬達的驅動端（M1）與非驅動端（M2）是兩個量測點，卻共用
-- 同一組電流與頻率 tag。舊結構下，一次寫兩列會報 CardinalityViolation，
-- 分兩次寫則後寫的覆蓋先寫的——先寫那台的 tag 查不到，而且沒有任何錯誤。
--
-- 舊資料會原樣搬到新結構。實務上舊的 tag_mapping 應該是空的（先前沒有任何
-- 程式讀寫它），但仍然搬，免得假設錯了就丟資料。
-- =============================================================

SET search_path TO vib, public;

BEGIN;

CREATE TABLE IF NOT EXISTS scada_tag (
    tag_id        TEXT PRIMARY KEY,
    variable_type TEXT NOT NULL
                  CHECK (variable_type IN ('current','frequency','power')),
    unit          TEXT
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'vib' AND table_name = 'tag_mapping'
                  AND column_name = 'variable_type') THEN

        CREATE TEMP TABLE _old_tag_mapping ON COMMIT DROP AS
            SELECT tag_id, device_id, variable_type, unit, is_active
              FROM tag_mapping;

        -- 舊資料的 variable_type 是自由文字；不在允許值內的無法搬進有 CHECK
        -- 的新表，直接丟會安靜失去資料，所以先報出來再讓這段失敗。
        IF EXISTS (SELECT 1 FROM _old_tag_mapping
                    WHERE variable_type NOT IN ('current','frequency','power')) THEN
            RAISE EXCEPTION
                'tag_mapping 有 variable_type 不在 current/frequency/power 內的舊資料，'
                '請先人工整理再套用本 migration';
        END IF;

        INSERT INTO scada_tag (tag_id, variable_type, unit)
        SELECT DISTINCT ON (tag_id) tag_id, variable_type, unit
          FROM _old_tag_mapping
         ORDER BY tag_id
        ON CONFLICT (tag_id) DO NOTHING;

        ALTER TABLE scada_reading DROP CONSTRAINT IF EXISTS scada_reading_tag_id_fkey;
        DROP TABLE tag_mapping;

        CREATE TABLE tag_mapping (
            device_id TEXT    NOT NULL REFERENCES device(device_id) ON DELETE CASCADE,
            tag_id    TEXT    NOT NULL REFERENCES scada_tag(tag_id) ON DELETE CASCADE,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            PRIMARY KEY (device_id, tag_id)
        );
        CREATE INDEX idx_tag_mapping_tag ON tag_mapping(tag_id);

        INSERT INTO tag_mapping (device_id, tag_id, is_active)
        SELECT device_id, tag_id, is_active
          FROM _old_tag_mapping
         WHERE device_id IS NOT NULL
        ON CONFLICT DO NOTHING;

        ALTER TABLE scada_reading
            ADD CONSTRAINT scada_reading_tag_id_fkey
            FOREIGN KEY (tag_id) REFERENCES scada_tag(tag_id) ON DELETE CASCADE;
    END IF;
END $$;

COMMIT;
