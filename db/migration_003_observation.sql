-- =============================================================
-- migration 003：新增 observation 表（observe 級判定落庫）
--
-- 適用對象：已經用舊版 db/schema.sql 建好資料庫的環境——
--   observe 級判定（IMPACT_RISE/DEGRADE_TREND/SPECTRAL_SHIFT/AXIS_SHIFT/
--   STEP_CHANGE/TEMP_RISE 等規則）先前只在 pipeline/daily.py 計數，
--   不落庫，週報拿不到觀察名單的素材。
-- 全新建庫者不需要執行——schema.sql 已包含這張表。
--
-- 可重複執行：CREATE TABLE / CREATE INDEX 皆用 IF NOT EXISTS，COMMENT ON
-- 本身即為冪等（覆蓋舊註解）。不影響既有資料——這是全新的表，沒有既有
-- 列需要 backfill；先前未落庫的 observe 級判定本來就沒有歷史資料可補。
--
-- 本次變更不影響任何既有 VIEW：v_open_finding / v_device_status 皆未
-- 引用 observation 表，`observation` 也不是既有檢視 `SELECT *` 展開範圍
-- 內的表，故本檔案不需要（也刻意不）重建任何檢視——這正是
-- migration_002 附註提醒要檢查的項目，這裡確認過後記錄下來，
-- 供之後維護者不必重新確認一次。
-- =============================================================

SET search_path TO vib, public;

BEGIN;

-- 表格設計的完整取捨說明見 db/schema.sql「六之二、觀察名單」該節，
-- 這裡不重複貼一次全文，只保留必要的內嵌註解。
CREATE TABLE IF NOT EXISTS observation (
    observation_id   BIGSERIAL PRIMARY KEY,
    observation_key  TEXT NOT NULL UNIQUE,        -- {target_type}:{target}:{issue_type}，格式同 finding_key
    device_id        TEXT   REFERENCES device(device_id) ON DELETE CASCADE,
    point_id         BIGINT REFERENCES measure_point(point_id) ON DELETE SET NULL,
    target_type      TEXT NOT NULL CHECK (target_type IN ('device','point','global')),
    target           TEXT NOT NULL,
    issue_type       TEXT NOT NULL,
    family           TEXT NOT NULL CHECK (family IN ('oscillating','monotonic','event','none')),
    rule_code        TEXT REFERENCES rule_config(rule_code),

    title            TEXT NOT NULL,
    detail           TEXT,

    -- 追蹤（語意同 finding 對應欄位，但沒有 status/簽核相關欄位——
    -- observation 不進簽核鏈，見 db/schema.sql 該表說明）
    occurrence_count INT NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 觸發當下的數值與證據，理由同 finding 對應欄位
    baseline_value   NUMERIC(16,6),
    current_value    NUMERIC(16,6),
    value_unit       TEXT  NOT NULL DEFAULT '',
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,
    trigger_params   JSONB NOT NULL DEFAULT '{}'::jsonb,
    interpretation_limit TEXT NOT NULL DEFAULT '',

    source           TEXT NOT NULL DEFAULT 'rule_engine'
                     CHECK (source IN ('rule_engine','agent','manual')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE observation IS
    'observe 級判定（有偵測價值但沒有可引用外部標準的規則）；不建立 finding、不進簽核鏈、不佔 SLA，'
    '僅供週報觀察名單與日後評估規則精確率使用。去重採 upsert（同 observation_key 累加 occurrence_count），'
    '「目前是否仍存在」以 last_seen_at 是否落在查詢區間內判斷，不設 status 欄位';
COMMENT ON COLUMN observation.observation_key IS
    '{target_type}:{target}:{issue_type}，格式與 finding.finding_key 相同（見 Finding.make_key）';
COMMENT ON COLUMN observation.occurrence_count IS
    '同一觀察項目累計觸發次數；每次 upsert 累加，first_seen_at 不變，用於呈現「已持續多久」';
COMMENT ON COLUMN observation.baseline_value IS
    '觸發當下的基準值，供日後回溯評估「若門檻改成 X 會剩幾件」（是否該升為 warn）';
COMMENT ON COLUMN observation.current_value IS
    '觸發當下的量測值；同一 observation_key 再次觸發時覆寫為最新一次的值';
COMMENT ON COLUMN observation.value_unit IS
    'current_value / baseline_value 的單位';
COMMENT ON COLUMN observation.evidence IS
    '判定依據的完整數值（例如各特徵的 σ 分解），供人工回溯查核';
COMMENT ON COLUMN observation.trigger_params IS
    '觸發當下 rule_config.params 的快照，理由同 finding.trigger_params：只存數值不存門檻，'
    '回溯時才分得清是數值變了還是門檻被調過';
COMMENT ON COLUMN observation.interpretation_limit IS
    '證據的解讀邊界；observe 級規則本身就沒有可對外交代的門檻依據，這欄更不可省略';
COMMENT ON COLUMN observation.source IS
    '建立來源，語意同 finding.source；目前只有 rule_engine 會寫入此表';

CREATE INDEX IF NOT EXISTS idx_observation_device    ON observation(device_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_observation_last_seen ON observation(last_seen_at DESC);

COMMIT;
