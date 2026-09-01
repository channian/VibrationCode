-- =============================================================
-- migration 004：ISO 10816-3 分類結構、衝擊型指標 median 通道、
--                保養時點欄位
--
-- 適用對象：已經用舊版 db/schema.sql 建好資料庫的環境。
-- 全新建庫者不需要執行——schema.sql 已包含全部變更。
--
-- 可重複執行：ADD COLUMN IF NOT EXISTS、CREATE TABLE IF NOT EXISTS、
-- DROP ... IF EXISTS 皆為冪等；資料回填用 INSERT ... ON CONFLICT DO NOTHING。
--
-- **本檔案會刪除舊的 iso_threshold 表與 device.iso_machine_class 欄位。**
-- 那兩者的內容是 ISO 2372 / ISO 10816-1 的 Class I~IV 分類，與本系統
-- 引用的告警設定原則（ISO 10816-3 §5.4.1）不是同一份文件，數值也不同
-- （舊 Class II 的 A/B 界 1.12 vs Group 2 剛性基礎 1.40）。留著只會讓
-- 兩套分類並存而沒人知道該信哪一套。**執行前請先備份 device 表。**
--
-- 舊資料無法自動轉換：Class I~IV 對應不到 (Group, 基礎剛性) ——
-- 舊分類沒有「基礎剛性」這個維度，而 Zone 邊界同時取決於它。
-- 因此本檔案把所有設備重設為未分類（iso_class_source='unset'），
-- 需由工程師依 ISO 10816-3 重新填寫。這是刻意的：用猜的對照表把舊值
-- 搬過來，會產生一批看起來已分類、實際上沒有依據的設備。
-- =============================================================

SET search_path TO vib, public;

BEGIN;

-- ── 一、device：ISO 10816-3 分類欄位與保養時點 ──────────────

ALTER TABLE device ADD COLUMN IF NOT EXISTS iso_machine_group   TEXT;
ALTER TABLE device ADD COLUMN IF NOT EXISTS iso_foundation      TEXT;
ALTER TABLE device ADD COLUMN IF NOT EXISTS iso_driver_type     TEXT;
ALTER TABLE device ADD COLUMN IF NOT EXISTS last_maintenance_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_iso_machine_group_check') THEN
        ALTER TABLE device ADD CONSTRAINT device_iso_machine_group_check
            CHECK (iso_machine_group IN ('1','2','3','4'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_iso_foundation_check') THEN
        ALTER TABLE device ADD CONSTRAINT device_iso_foundation_check
            CHECK (iso_foundation IN ('rigid','flexible'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'device_iso_driver_type_check') THEN
        ALTER TABLE device ADD CONSTRAINT device_iso_driver_type_check
            CHECK (iso_driver_type IN ('integrated','external'));
    END IF;
END $$;

COMMENT ON COLUMN device.iso_machine_group IS
    'ISO 10816-3 機器群組 1~4；與 iso_foundation 一起決定套用哪一組 Zone 門檻。任一為 NULL 即不套用 Zone 判定';
COMMENT ON COLUMN device.iso_foundation IS
    '基礎剛性 rigid/flexible；Zone 邊界同時取決於群組與基礎，缺此欄無法判定 Zone';
COMMENT ON COLUMN device.last_maintenance_at IS
    '最後一次保養／大修時點；基準期掃描不得選在此之前的窗口（ISO 10816-3 §5.4.1）';

-- 舊的 Class 分類無法對應到新結構（見檔頭說明），一律重設為未分類。
UPDATE device SET iso_class_source = 'unset'
 WHERE iso_class_source <> 'unset'
   AND iso_machine_group IS NULL;

-- v_device_status 引用了 iso_machine_class，必須先移除檢視才能刪欄位
-- （PostgreSQL 會擋下有相依物件的 DROP COLUMN）。檢視在本檔案最後重建。
DROP VIEW IF EXISTS v_device_status;

ALTER TABLE device DROP COLUMN IF EXISTS iso_machine_class;

-- ── 二、iso_threshold：改為 (群組, 基礎剛性) 主鍵 ──────────────

DROP TABLE IF EXISTS iso_threshold;

CREATE TABLE iso_threshold (
    machine_group TEXT NOT NULL CHECK (machine_group IN ('1','2','3','4')),
    foundation    TEXT NOT NULL CHECK (foundation IN ('rigid','flexible')),
    label         TEXT NOT NULL,
    ab_boundary   NUMERIC(8,3) NOT NULL,
    bc_boundary   NUMERIC(8,3) NOT NULL,
    cd_boundary   NUMERIC(8,3) NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (machine_group, foundation)
);

INSERT INTO iso_threshold (machine_group, foundation, label, ab_boundary, bc_boundary, cd_boundary) VALUES
    ('1', 'rigid',    'Group 1 大型機（300 kW–50 MW；馬達軸高 H ≥ 315 mm）· 剛性基礎', 2.30, 4.50,  7.10),
    ('1', 'flexible', 'Group 1 大型機 · 柔性基礎',                                      3.50, 7.10, 11.00),
    ('2', 'rigid',    'Group 2 中型機（15 kW < P ≤ 300 kW；馬達軸高 160 ≤ H < 315 mm）· 剛性基礎', 1.40, 2.80, 4.50),
    ('2', 'flexible', 'Group 2 中型機 · 柔性基礎',                                      2.30, 4.50,  7.10),
    ('3', 'rigid',    'Group 3 泵浦（> 15 kW，外接驅動）· 剛性基礎',                    2.30, 4.50,  7.10),
    ('3', 'flexible', 'Group 3 泵浦（外接驅動）· 柔性基礎',                              3.50, 7.10, 11.00),
    ('4', 'rigid',    'Group 4 泵浦（> 15 kW，整合驅動）· 剛性基礎',                    1.40, 2.80,  4.50),
    ('4', 'flexible', 'Group 4 泵浦（整合驅動）· 柔性基礎',                              2.30, 4.50,  7.10)
ON CONFLICT (machine_group, foundation) DO NOTHING;

-- ── 三、衝擊型指標的 median 通道 ─────────────────────────────
-- 判定改用 median：max 會系統性偏高（實測 AHU-601 逐筆 accKURT 中位數
-- 2.37、僅 2.6% 超過 4，但每 30/60/114 筆取最大值後平均達
-- 7.17/13.49/19.00，median 恆為 2.37）。詳見 vibcore/config.py。
--
-- **既有資料不回填**：median 無法從已聚合的 max 反推，需要原始每秒資料
-- 重跑聚合。新欄位在重跑前一律是 NULL，此時規則層的 `_impact_channel`
-- 會自動退回 max 通道並在 evidence 標明，不會失效也不會安靜偏掉。

ALTER TABLE measurement_agg   ADD COLUMN IF NOT EXISTS acc_crest_median      NUMERIC(12,6);
ALTER TABLE measurement_agg   ADD COLUMN IF NOT EXISTS acc_kurt_median       NUMERIC(12,6);
ALTER TABLE measurement_agg   ADD COLUMN IF NOT EXISTS acc_crest_axis_median NUMERIC(12,6);
ALTER TABLE measurement_agg   ADD COLUMN IF NOT EXISTS acc_kurt_axis_median  NUMERIC(12,6);

ALTER TABLE measurement_daily ADD COLUMN IF NOT EXISTS acc_crest_median      NUMERIC(12,6);
ALTER TABLE measurement_daily ADD COLUMN IF NOT EXISTS acc_kurt_median       NUMERIC(12,6);
ALTER TABLE measurement_daily ADD COLUMN IF NOT EXISTS acc_crest_axis_median NUMERIC(12,6);
ALTER TABLE measurement_daily ADD COLUMN IF NOT EXISTS acc_kurt_axis_median  NUMERIC(12,6);

-- ── 四、規則設定 ────────────────────────────────────────────
-- IMPACT_RISE 移除失效的 kurt_absolute / threshold_mode；
-- ISO_ZONE 與 VEL_HIGH 加上持續性緩衝（ISO 10816-3 §5.4）。

UPDATE rule_config
   SET params = '{"crest_sigma":2.5,"kurt_sigma":2.5,'
                '"crest_axis_sigma":2.5,"kurt_axis_sigma":2.5,"require_both":false}'::jsonb
 WHERE rule_code = 'IMPACT_RISE';

UPDATE rule_config
   SET params = params - 'consecutive_readings'
                || '{"consecutive_readings":3}'::jsonb
 WHERE rule_code IN ('ISO_ZONE', 'VEL_HIGH');

UPDATE rule_config
   SET description = 'velRMS 對照 ISO 10816-3 的 Zone A/B/C/D；未分類或適用範圍外的設備不套用'
 WHERE rule_code = 'ISO_ZONE';
UPDATE rule_config
   SET description = 'velRMS 相對 ISO 錨定門檻偏高（未分類時退回相對基準 σ 判定）'
 WHERE rule_code = 'VEL_HIGH';

-- ── 五、重建受影響的檢視 ────────────────────────────────────
-- v_device_status 的 SELECT 在建立當下就展開固定，欄位變更後必須重建
-- （migration_002 的教訓：不重建會讓走檢視查資料的程式在新建庫與遷移庫
-- 拿到不同欄位，而且不報錯）。定義須與 db/schema.sql 保持一致。

CREATE VIEW v_device_status AS
SELECT
    d.device_id, d.device_name, d.building, d.floor, d.system_name,
    d.is_standby, d.iso_machine_group, d.iso_foundation, d.iso_class_source,
    COUNT(DISTINCT mp.point_id)                                      AS n_points,
    COUNT(f.finding_id) FILTER (WHERE f.severity = 'err')            AS n_err,
    COUNT(f.finding_id) FILTER (WHERE f.severity = 'warn')           AS n_warn,
    COUNT(f.finding_id) FILTER (WHERE f.escalated_at IS NOT NULL)    AS n_escalated,
    MAX(f.last_seen_at)                                              AS last_finding_at
FROM device d
LEFT JOIN measure_point mp ON mp.device_id = d.device_id AND mp.is_active
LEFT JOIN finding f        ON f.device_id  = d.device_id
       AND f.status NOT IN ('closed','auto_resolved','false_positive')
WHERE d.status = 'active'
GROUP BY d.device_id;

COMMIT;
