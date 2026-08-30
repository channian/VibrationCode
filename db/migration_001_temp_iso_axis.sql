-- =============================================================
-- migration 001：新增溫度、前端 ISO 分級、逐軸衝擊指標
--
-- 適用對象：已經用舊版 db/schema.sql 建好資料庫的環境。
-- 全新建庫者不需要執行——schema.sql 已包含這些欄位。
--
-- 可重複執行（IF NOT EXISTS），不影響既有資料。
-- 既有列的新欄位會是 NULL，重新匯入該期間的資料即可回填。
-- =============================================================

SET search_path TO vib, public;

ALTER TABLE measurement_agg
    -- 逐軸衝擊指標取「三軸最大」，再取該小時最大值。合成欄是對合成訊號
    -- 另算的，單一方向的衝擊會被其他兩軸稀釋（實測 ZP 3-5 三軸 crest
    -- 4.65/5.01/4.30，合成欄只有 4.08）。只存極值不存是哪一軸——感測器
    -- 可能貼錯方向，軸標籤不可信。
    ADD COLUMN IF NOT EXISTS acc_crest_axis_max NUMERIC(12,6),
    ADD COLUMN IF NOT EXISTS acc_kurt_axis_max  NUMERIC(12,6),
    -- 溫度（°C）。與振動獨立的唯一物理通道。可能是感測器內部溫度而非
    -- 軸承座溫度（見 docs/DATA_CONTRACT.md §3.1），故僅用於與自身基準
    -- 比的相對趨勢，不設絕對門檻。
    ADD COLUMN IF NOT EXISTS temp_avg NUMERIC(8,3),
    ADD COLUMN IF NOT EXISTS temp_max NUMERIC(8,3),
    ADD COLUMN IF NOT EXISTS temp_min NUMERIC(8,3),
    -- 前端已算好的 ISO 分級（1=Zone A … 4=Zone D），取該小時最差值。
    -- 僅作為本系統自行判定的交叉檢查——前端假設的機械等級未知。
    ADD COLUMN IF NOT EXISTS iso_zone_frontend SMALLINT;

COMMENT ON COLUMN measurement_agg.acc_crest_axis_max IS
    '三軸 accCREST 取最大後再取該小時最大；比合成欄敏感，單軸衝擊不會被稀釋';
COMMENT ON COLUMN measurement_agg.acc_kurt_axis_max IS
    '三軸 accKURT 取最大後再取該小時最大';
COMMENT ON COLUMN measurement_agg.temp_avg IS
    '該小時運轉樣本的溫度平均（°C）；用於相對趨勢，非絕對門檻';
COMMENT ON COLUMN measurement_agg.temp_max IS '該小時溫度最大值（°C）';
COMMENT ON COLUMN measurement_agg.temp_min IS '該小時溫度最小值（°C）';
COMMENT ON COLUMN measurement_agg.iso_zone_frontend IS
    '前端計算的 ISO 分級（1=A…4=D），該小時最差值；供交叉檢查用';

-- 日層 rollup 也要跟上。週報與長期趨勢讀的是這張表，小時層有值但日層
-- 沒有的欄位，等於在週報裡不存在。
ALTER TABLE measurement_daily
    ADD COLUMN IF NOT EXISTS acc_crest_axis_max NUMERIC(12,6),
    ADD COLUMN IF NOT EXISTS acc_kurt_axis_max  NUMERIC(12,6),
    ADD COLUMN IF NOT EXISTS temp_avg NUMERIC(8,3),
    ADD COLUMN IF NOT EXISTS temp_max NUMERIC(8,3);
