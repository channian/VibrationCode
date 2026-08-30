-- =============================================================
-- 振動監測平台 — PostgreSQL Schema
-- 對應文件：PLAN_agent_platform_refactor.md / DASHBOARD_requirements.md
--
-- 設計要點：
--   · 時間一律 TIMESTAMPTZ（DB 存 UTC，呈現層轉 +8）
--   · 量測資料只存「每小時聚合 × 精選欄位」，原始 CSV 留在檔案系統（Tier 2）
--   · 聚合僅計運轉中樣本；n_samples_running = 0 表示未運轉，不判異常
--   · 使用者與角色走 DB，帳號由 AD 認證後對應
-- =============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS vib;
SET search_path TO vib, public;


-- =============================================================
-- 一、使用者與角色（AD 認證後對應）
-- =============================================================

CREATE TABLE app_user (
    user_id       BIGSERIAL PRIMARY KEY,
    ad_username   TEXT        NOT NULL UNIQUE,   -- AD 帳號（登入識別）
    ad_sid        TEXT        UNIQUE,            -- AD SID，帳號改名時仍可對應
    display_name  TEXT        NOT NULL,
    email         TEXT,
    department    TEXT,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE  app_user IS '系統使用者；認證由 AD 負責，此表僅存對應與屬性';
COMMENT ON COLUMN app_user.ad_sid IS 'AD SID 為不變識別碼，優先以此比對，避免帳號改名失聯';

CREATE TABLE app_role (
    role_code   TEXT PRIMARY KEY,
    role_name   TEXT NOT NULL,
    description TEXT,
    sort_order  INT  NOT NULL DEFAULT 0
);
COMMENT ON TABLE app_role IS '角色定義；簽核鏈的每一關對應一個角色';

INSERT INTO app_role (role_code, role_name, description, sort_order) VALUES
    ('engineer',   '設備工程師', '處理指派的異常事件、現場確認與回覆', 1),
    ('supervisor', '工程師主管', '審核工程師回覆、追蹤積壓',           2),
    ('expert',     '專家',       '複審、判定是否需轉專家系統實測',     3),
    ('manager',    '管理層',     '檢視整體狀態與處理效率（唯讀）',     4),
    ('admin',      '系統管理員', '設定台帳、門檻、角色指派',           5);

-- 角色指派；可限定範圍（廠區／樓層／系統別），scope 為 NULL 代表全廠
CREATE TABLE user_role (
    user_role_id BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    role_code   TEXT   NOT NULL REFERENCES app_role(role_code),
    scope_type  TEXT   CHECK (scope_type IN ('building','floor','system')),
    scope_value TEXT,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by  BIGINT REFERENCES app_user(user_id),
    CHECK ((scope_type IS NULL) = (scope_value IS NULL))
);
-- 同一使用者在同一範圍內不重複指派同一角色（scope 為 NULL 時視為全廠）
CREATE UNIQUE INDEX uq_user_role
    ON user_role (user_id, role_code, COALESCE(scope_type,''), COALESCE(scope_value,''));
COMMENT ON TABLE user_role IS '使用者角色指派；scope 皆為 NULL 表示不限範圍';


-- =============================================================
-- 二、設備台帳與量測點
-- =============================================================

CREATE TABLE device (
    device_id         TEXT PRIMARY KEY,           -- 對應 Analytic.csv 的 Name，如 'AHU-601'
    device_name       TEXT,
    building          TEXT,                       -- Analytic.csv: Building
    floor             TEXT,                       -- Analytic.csv: Floor
    system_name       TEXT,                       -- Analytic.csv: System，如 '空調'
    machine_type      TEXT,                       -- AHU / 泵浦 / 風機 / 空壓機
    rated_power_kw    NUMERIC(10,2),
    rated_rpm         NUMERIC(10,2),              -- Analytic.csv: RPM
    fmf_hz            NUMERIC(10,3),              -- Analytic.csv: FMF = RPM/60
    is_vfd            BOOLEAN NOT NULL DEFAULT FALSE,
    is_standby        BOOLEAN NOT NULL DEFAULT FALSE,   -- 備機
    -- ISO 10816 分級
    iso_machine_class TEXT CHECK (iso_machine_class IN ('I','II','III','IV')),
    iso_class_source  TEXT NOT NULL DEFAULT 'unset'
                      CHECK (iso_class_source IN ('unset','frontend','manual_override')),
    iso_class_verified_at TIMESTAMPTZ,
    -- 歸屬
    owner_group       TEXT,
    owner_user_id     BIGINT REFERENCES app_user(user_id),
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','inactive','decommissioned')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN device.iso_class_source IS
    'unset=未分級（不套 Zone 判定）；frontend=前端程式由工程師分類；manual_override=人工覆寫';
COMMENT ON COLUMN device.is_standby IS '備機；未運轉不判異常，另受 STANDBY_NO_RUNTIME 規則監測';
COMMENT ON COLUMN device.iso_machine_class IS
    'ISO 10816 機械等級 I~IV，決定 iso_threshold 套用哪一組 Zone 門檻；為 NULL 時等同 iso_class_source=unset，不套用 Zone 判定';
COMMENT ON COLUMN device.status IS
    'active=正常監測中；inactive=暫停監測（不觸發新 Finding，但保留歷史資料）；decommissioned=已報廢（不再匯入新資料）';

CREATE INDEX idx_device_owner   ON device(owner_user_id);
CREATE INDEX idx_device_scope   ON device(building, floor, system_name);
CREATE INDEX idx_device_standby ON device(is_standby) WHERE is_standby;

CREATE TABLE measure_point (
    point_id     BIGSERIAL PRIMARY KEY,
    device_id    TEXT NOT NULL REFERENCES device(device_id) ON DELETE CASCADE,
    position     TEXT NOT NULL,                  -- M1 自由端 / M2 驅動端 等
    sensor_id    TEXT,
    channel_x    INT,                            -- Analytic.csv: Channel_X/Y/Z
    channel_y    INT,
    channel_z    INT,
    install_date DATE,
    -- 軸能量分佈基準（供 ORIENTATION_CHANGE 比對；排序後佔比，與座標方向無關）
    axis_energy_baseline JSONB,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (device_id, position)
);
COMMENT ON COLUMN measure_point.axis_energy_baseline IS
    '排序後的三軸能量佔比基準，如 {"major":0.72,"mid":0.19,"minor":0.09}；用於偵測感測器重貼';


-- =============================================================
-- 三、量測資料（Tier 1：每小時聚合，僅計運轉樣本）
-- =============================================================

CREATE TABLE measurement_agg (
    point_id            BIGINT      NOT NULL REFERENCES measure_point(point_id) ON DELETE CASCADE,
    ts_hour             TIMESTAMPTZ NOT NULL,
    -- 資料狀態：三種情況必須明確區分，混為一談會讓趨勢圖與規則同時出錯
    data_status         TEXT        NOT NULL CHECK (data_status IN (
                          'ok',           -- 資料完整且運轉中
                          'partial',      -- 有資料但筆數不足（斷斷續續）
                          'no_data',      -- 該小時完全無資料（感測器斷線）
                          'not_running')),-- 有資料但設備未運轉（正常狀態，非異常）
    completeness        NUMERIC(5,4) NOT NULL DEFAULT 0,  -- n_samples_total / 3600
    n_samples_total     INT         NOT NULL,     -- 該小時原始筆數（每秒 1 筆，滿載 3600）
    n_samples_running   INT         NOT NULL,     -- 其中判定運轉中的筆數；0 = 未運轉
    -- 速度（mm/s）
    vel_rms             NUMERIC(12,6),
    vel_rms_x           NUMERIC(12,6),
    vel_rms_y           NUMERIC(12,6),
    vel_rms_z           NUMERIC(12,6),
    vel_oa              NUMERIC(12,6),
    vel_peak            NUMERIC(12,6),
    -- 加速度（m/s²）
    acc_rms             NUMERIC(12,6),
    acc_rms_x           NUMERIC(12,6),
    acc_rms_y           NUMERIC(12,6),
    acc_rms_z           NUMERIC(12,6),
    acc_oa              NUMERIC(14,6),
    acc_peak            NUMERIC(12,6),
    acc_crest           NUMERIC(12,6),            -- = acc_peak / acc_rms
    acc_kurt            NUMERIC(12,6),            -- Pearson 定義，常態 = 3
    acc_skew            NUMERIC(12,6),
    -- 位移（mm）
    disp_rms            NUMERIC(12,8),
    disp_p2p            NUMERIC(12,8),
    -- 頻譜摘要純量（不使用個別諧波欄位，見計畫書 §一）
    acc_mean_peak_freq      NUMERIC(10,3),
    acc_weighted_mean_freq  NUMERIC(10,3),        -- 頻譜重心，SPECTRAL_SHIFT 規則使用
    acc_top1_freq           NUMERIC(10,3),
    acc_top1_amp            NUMERIC(14,6),
    vel_weighted_mean_freq  NUMERIC(10,3),
    -- 軸能量分佈（排序後佔比，方向無關）
    axis_energy_sorted  JSONB,
    -- 逐軸衝擊指標取「三軸最大」，再取該小時最大值。合成欄是對合成訊號
    -- 另算的，單一方向的衝擊會被其他兩軸稀釋（實測 ZP 3-5 三軸 crest
    -- 4.65/5.01/4.30，合成欄只有 4.08）。只存極值不存是哪一軸——感測器
    -- 可能貼錯方向，軸標籤不可信。
    acc_crest_axis_max  NUMERIC(12,6),
    acc_kurt_axis_max   NUMERIC(12,6),
    -- 溫度（°C）。與振動獨立的唯一物理通道：「振動上升但溫度持平」與
    -- 「兩者一起上升」對現場的意義不同。可能是感測器內部溫度而非軸承座
    -- 溫度（見 docs/DATA_CONTRACT.md §3.1），故僅用於與自身基準比的相對
    -- 趨勢，不設絕對門檻。
    temp_avg            NUMERIC(8,3),
    temp_max            NUMERIC(8,3),
    temp_min            NUMERIC(8,3),
    -- 前端已算好的 ISO 分級（1=Zone A … 4=Zone D），取該小時最差值。
    -- 僅作為本系統自行判定的交叉檢查——前端假設的機械等級未知，
    -- 兩者不一致時由 ISO_CLASS_SUSPECT 提出。
    iso_zone_frontend   SMALLINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (point_id, ts_hour)
);
COMMENT ON TABLE measurement_agg IS
    'Tier 1 每小時聚合。RMS/OA 類取運轉樣本平均，PEAK/CREST/KURT 類取最大值（衝擊事件不可被平均掉）';
COMMENT ON COLUMN measurement_agg.data_status IS
    '斷線（no_data）與未運轉（not_running）是完全不同的事：前者是設備異常需告警，'
    '後者是正常狀態。趨勢圖不可跨 no_data 連線，規則不可對 not_running 判異常';
COMMENT ON COLUMN measurement_agg.completeness IS
    '資料完整度；partial 狀態的指標僅供參考，不應作為趨勢回歸與規則判定的依據';

CREATE INDEX idx_agg_ts      ON measurement_agg(ts_hour DESC);
CREATE INDEX idx_agg_running ON measurement_agg(point_id, ts_hour DESC)
    WHERE data_status = 'ok';
CREATE INDEX idx_agg_gap     ON measurement_agg(point_id, ts_hour)
    WHERE data_status IN ('no_data','partial');

-- 每日 rollup（供週報與長期趨勢）
-- 各數值欄位定義與單位同 measurement_agg（vel_rms 為 mm/s、acc_* 為 m/s²、
-- disp_p2p 為 mm），此處為當日彙整值，聚合方式（RMS 類平均／PEAK 類取最大）
-- 亦與 measurement_agg 相同，這裡不重複列出。
CREATE TABLE measurement_daily (
    point_id      BIGINT NOT NULL REFERENCES measure_point(point_id) ON DELETE CASCADE,
    date          DATE   NOT NULL,
    running_hours NUMERIC(6,2) NOT NULL DEFAULT 0,   -- 當日運轉時數（備機判定）
    vel_rms       NUMERIC(12,6),
    vel_oa        NUMERIC(12,6),
    acc_rms       NUMERIC(12,6),
    acc_oa        NUMERIC(14,6),
    acc_peak      NUMERIC(12,6),
    acc_crest     NUMERIC(12,6),
    acc_kurt      NUMERIC(12,6),
    disp_p2p      NUMERIC(12,8),
    acc_weighted_mean_freq NUMERIC(10,3),
    axis_energy_sorted JSONB,
    -- 逐軸衝擊指標與溫度。週報與長期趨勢是看日層資料，日層漏掉的欄位
    -- 等於在週報裡不存在——小時層有值也沒用。
    acc_crest_axis_max NUMERIC(12,6),
    acc_kurt_axis_max  NUMERIC(12,6),
    temp_avg      NUMERIC(8,3),
    temp_max      NUMERIC(8,3),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (point_id, date)
);
COMMENT ON TABLE measurement_daily IS
    '每日 rollup，供週報與長期趨勢使用；各數值欄位定義與單位同 measurement_agg，此處為當日彙整值';

-- Tier 2 檔案索引（只記位置，不存內容）
CREATE TABLE raw_file (
    file_id     BIGSERIAL PRIMARY KEY,
    point_id    BIGINT REFERENCES measure_point(point_id) ON DELETE SET NULL,
    device_id   TEXT   REFERENCES device(device_id) ON DELETE SET NULL,
    file_date   DATE   NOT NULL,
    file_path   TEXT   NOT NULL UNIQUE,
    row_count   BIGINT,
    file_bytes  BIGINT,
    file_kind   TEXT NOT NULL DEFAULT 'analytic' CHECK (file_kind IN ('analytic','raw')),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE raw_file IS 'Tier 2 檔案索引；analytic=每秒處理後資料，raw=現場量測的原始三軸';

CREATE INDEX idx_rawfile_date ON raw_file(file_date DESC);


-- 每日匯入排程的執行紀錄（區分「感測器斷線」與「匯入排程沒跑」）
--
-- 動機：measurement_agg 的 no_data 只回答「這小時沒有數字」，答不出
-- 「為什麼沒有」。感測器斷線與匯入排程沒跑，在 measurement_agg 裡長得
-- 一模一樣（同樣是那個量測點、那段時間全是 no_data），但前者要現場查
-- 感測器、後者要查系統排程，兩者對調是白跑一趟。更嚴重的是邊界情況：
-- 若某天完全沒有來源檔、排程整個沒跑，聚合流程根本不會被觸發，
-- measurement_agg 連一列（含 no_data）都不會寫入——現有的缺口偵測只補
-- 「已觀測範圍內」的空洞（見 aggregate.py 的 _fill_gap_hours），對這種
-- 整天憑空消失的情況完全無感。
--
-- ingestion_log 記錄「匯入這件事本身有沒有發生」，且獨立於
-- measurement_agg 之外——找缺漏時是拿「量測點 × 應涵蓋的每一天」當
-- 母集合去比對這張表，不是反過來從 measurement_agg 回推，才抓得到
-- 上述「連一列都不存在」的邊界情況。
CREATE TABLE ingestion_log (
    point_id    BIGINT      NOT NULL REFERENCES measure_point(point_id) ON DELETE CASCADE,
    ingest_date DATE        NOT NULL,   -- 這筆匯入涵蓋的「資料日期」，不是執行匯入當下的日期
    status      TEXT        NOT NULL CHECK (status IN ('ok','partial','failed','no_file')),
    source_file TEXT        NOT NULL DEFAULT '',
    row_count   BIGINT      NOT NULL DEFAULT 0,
    note        TEXT        NOT NULL DEFAULT '',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (point_id, ingest_date)
);
COMMENT ON TABLE ingestion_log IS
    '每日匯入排程的執行紀錄，一個量測點一天一列。此表沒有紀錄，即代表匯入排程當天'
    '根本沒有處理過這個量測點——這與「排程跑了但該點沒資料」（感測器斷線）是完全不同'
    '的問題，前者查系統排程，後者查現場設備，不可混為一談。';
COMMENT ON COLUMN ingestion_log.status IS
    'ok=正常匯入；partial=有匯入但檔案不完整；failed=匯入過程出錯；'
    'no_file=該日根本沒有來源檔（上游未產生），四者皆代表「排程確實處理過這一天」，'
    '差別只在結果好不好——真正的系統面死角是完全沒有這一列的 (point_id, ingest_date)';
COMMENT ON COLUMN ingestion_log.ingest_date IS
    '資料所屬日期，不是 ingested_at 的日期；補跑匯入時，同一天執行可能涵蓋更早的資料日';

CREATE INDEX idx_ingestion_date       ON ingestion_log(ingest_date);
CREATE INDEX idx_ingestion_point_date ON ingestion_log(point_id, ingest_date DESC);
CREATE INDEX idx_ingestion_problem    ON ingestion_log(ingest_date, point_id)
    WHERE status IN ('partial','failed','no_file');


-- =============================================================
-- 四、SCADA（電流等）
-- =============================================================

CREATE TABLE tag_mapping (
    tag_id        TEXT PRIMARY KEY,
    device_id     TEXT REFERENCES device(device_id) ON DELETE CASCADE,
    variable_type TEXT NOT NULL,                  -- 電流 / 頻率 / 輸入功率 …
    unit          TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE scada_reading (
    tag_id TEXT        NOT NULL REFERENCES tag_mapping(tag_id) ON DELETE CASCADE,
    ts     TIMESTAMPTZ NOT NULL,
    value  NUMERIC(16,6),
    PRIMARY KEY (tag_id, ts)
);
CREATE INDEX idx_scada_ts ON scada_reading(ts DESC);


-- =============================================================
-- 五、基準期與門檻設定
-- =============================================================

CREATE TABLE point_baseline (
    point_id     BIGINT PRIMARY KEY REFERENCES measure_point(point_id) ON DELETE CASCADE,
    start_date   DATE NOT NULL,
    end_date     DATE NOT NULL,
    source       TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto','manual')),
    -- 各指標的基準統計（中位數與標準差，供 σ 分解與趨勢比較）
    stats        JSONB NOT NULL,
    -- 基準是用多少可信（ok）小時算出來的。基準品質決定了所有相對比較的
    -- 可信度，這個數字必須能被查詢與呈現，不可只存在於記憶體物件裡。
    n_hours      INT  NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_date >= start_date)
);
COMMENT ON COLUMN point_baseline.n_hours IS
    '建立此基準所用的 ok 小時數；偏低時所有以此為準的 σ 比較都需標示信心度';
COMMENT ON COLUMN point_baseline.stats IS
    '如 {"vel_rms":{"median":1.51,"std":0.08}, "acc_kurt":{"median":2.05,"std":0.11}, ...}';
COMMENT ON COLUMN point_baseline.source IS
    'auto=由 detect_baseline() 自動掃描選定；manual=人工指定的基準期範圍';

-- ISO 10816 / 20816 Zone 門檻（velRMS mm/s），可由管理員調整
CREATE TABLE iso_threshold (
    machine_class TEXT PRIMARY KEY CHECK (machine_class IN ('I','II','III','IV')),
    label         TEXT NOT NULL,
    ab_boundary   NUMERIC(8,3) NOT NULL,
    bc_boundary   NUMERIC(8,3) NOT NULL,
    cd_boundary   NUMERIC(8,3) NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN iso_threshold.ab_boundary IS
    'Zone A/B 分界（velRMS mm/s）；低於此值為 Zone A（良好，新機或大修後的典型水準）';
COMMENT ON COLUMN iso_threshold.bc_boundary IS
    'Zone B/C 分界；超過即進入 Zone C（不宜長期運轉，ISO_ZONE 規則預設在此門檻觸發告警）';
COMMENT ON COLUMN iso_threshold.cd_boundary IS
    'Zone C/D 分界；超過即 Zone D（可能造成損壞，通常需立即處理）';

INSERT INTO iso_threshold (machine_class, label, ab_boundary, bc_boundary, cd_boundary) VALUES
    ('I',   'Class I（< 15 kW）',        0.71,  1.80,  4.50),
    ('II',  'Class II（15–75 kW）',      1.12,  2.80,  7.10),
    ('III', 'Class III（大型剛性基礎）',  1.80,  4.50, 11.20),
    ('IV',  'Class IV（大型柔性基礎）',   2.80,  7.10, 18.00);

-- 規則門檻設定（比照 HVM 的 get_alert_thresholds：只有查到的閾值才算數）
CREATE TABLE rule_config (
    rule_code   TEXT PRIMARY KEY,
    rule_name   TEXT NOT NULL,
    family      TEXT NOT NULL CHECK (family IN ('oscillating','monotonic','event','none')),
    issue_type  TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'warn' CHECK (severity IN ('err','warn')),
    params      JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE rule_config IS
    '規則設定；params 為各規則實際使用的門檻參數（JSONB），改這裡不需要改程式碼';
COMMENT ON COLUMN rule_config.rule_code IS
    '規則代碼，對應 vibcore/rules/ 內以 @register(rule_code) 註冊的判定函式';
COMMENT ON COLUMN rule_config.family IS
    '判準家族：oscillating=波動型（隨工況起伏會自行回落，需形態判準區分零星尖峰與持續超標）；'
    'monotonic=單調累積型（不會自行回落，看趨勢斜率與到達門檻的時間）；'
    'event=事件型（二元狀態或計數，如斷線／未運轉）；none=不適用三家族判準';
COMMENT ON COLUMN rule_config.issue_type IS
    '對應 finding.issue_type，與 target_type/target 一起組出 finding_key';
COMMENT ON COLUMN rule_config.severity IS
    '此規則觸發時預設的嚴重度；err=需立即處理，warn=需關注';
COMMENT ON COLUMN rule_config.params IS
    '規則判定用的門檻參數（σ、天數、百分比等），鍵名由各規則函式自行定義並讀取；'
    '此表的值即為現行有效門檻，API 的 get_vibration_thresholds 直接回傳此表內容';
COMMENT ON COLUMN rule_config.is_active IS
    '停用時規則引擎會直接跳過此規則，不對任何設備產生此類 Finding';
COMMENT ON COLUMN rule_config.description IS
    '規則的中文說明，供管理介面與人工複核參考，不影響程式判定邏輯';

INSERT INTO rule_config (rule_code, rule_name, family, issue_type, severity, params, description) VALUES
    ('ISO_ZONE',           'ISO 位準分級',     'oscillating', 'iso_zone_exceed',  'err',
     '{"alert_zone":"C"}',
     'velRMS 對照機械等級的 Zone A/B/C/D；未分級設備不套用'),
    ('VEL_HIGH',           '速度整體值偏高',   'oscillating', 'vel_high',         'warn',
     '{"sigma":3.0}',
     'velOA 相對基準超過 N 個標準差'),
    ('IMPACT_RISE',        '衝擊性指標上升',   'monotonic',   'impact_rise',      'warn',
     -- 逐軸門檻與合成值同量級：實測 crest 是逐軸較大、kurt 反而是合成
     -- 較大（見 docs/DATA_CONTRACT.md §3.3），沒有證據支持哪一邊該偏鬆，
     -- 故給相同預設值。
     '{"crest_sigma":2.5,"kurt_sigma":2.5,'
     '"crest_axis_sigma":2.5,"kurt_axis_sigma":2.5,"require_both":false}',
     'accCREST / accKURT 相對基準顯著上升，常見於軸承或潤滑劣化（不判定成因）'),
    ('DEGRADE_TREND',      '指標持續劣化',     'monotonic',   'degradation_trend','warn',
     '{"min_days":14,"min_r2":0.3,"slope_pct_per_month":10}',
     '回歸斜率持續惡化；須在聚合後的獨立樣本上計算'),
    ('SPECTRAL_SHIFT',     '頻譜重心上移',     'monotonic',   'spectral_shift',   'warn',
     '{"shift_pct":15,"min_days":14}',
     'accWeightedMeanFreq 持續上移，代表能量往高頻移動'),
    ('AXIS_SHIFT',         '軸能量分佈偏移',   'monotonic',   'axis_shift',       'warn',
     '{"ratio_delta":0.15}',
     '排序後三軸能量佔比相對基準偏移'),
    ('STEP_CHANGE',        '多變量突變',       'monotonic',   'step_change',      'warn',
     '{"mahalanobis_sigma":3.0}',
     '特徵向量偏離基準；輸出各特徵標準化偏離量而非 0–100 分數'),
    ('ORIENTATION_CHANGE', '感測器方向改變',   'event',       'orientation_change','warn',
     '{"ratio_delta":0.25,"consecutive_readings":3,"min_energy_ratio":0.3}',
     -- consecutive_readings：需連續幾筆可信資料都超出門檻才觸發。只看單筆
     --   會變成每天拿一個隨機小時擲骰子（實測 33 週誤觸發 93 次）。
     -- min_energy_ratio：當下三軸合成量值需達基準期的此比例，佔比才具可比性。
     --   接近停機時三軸都貼近雜訊，歸一化後的佔比會劇烈跳動。
     '軸能量分佈排列跳變，疑似感測器重貼或更換'),
    ('SENSOR_OFFLINE',     '感測器離線',       'event',       'sensor_offline',   'err',
     '{"hours":24}',
     '逾時無資料'),
    ('DATA_QUALITY',       '資料品質異常',     'event',       'data_quality',     'warn',
     '{"min_running_ratio":0.5}',
     '缺漏、零值、運轉樣本數不足'),
    ('SENSOR_SATURATION',  '感測器接近飽和',   'event',       'sensor_saturation','warn',
     '{"full_scale_pct":90,"range_g":4}',
     'accPEAK 逼近量程滿刻度，峰值類指標將失真'),
    ('STANDBY_NO_RUNTIME', '備機長期未運轉',   'event',       'standby_no_runtime','warn',
     '{"days":30}',
     '備機超過 N 天未運轉，建議試車'),
    ('ISO_CLASS_SUSPECT',  'ISO 等級存疑',     'event',       'iso_class_suspect','warn',
     '{"frontend_consecutive_readings":3}',
     '基準期中位數已超過所指派等級的 B/C 界，等級可能填錯或機器本有問題'),
    ('TEMP_RISE',          '溫度相對基準上升', 'oscillating', 'temp_rise',        'warn',
     -- sigma 與 IMPACT_RISE 同量級；另有 consecutive_readings 把關，
     -- 兩道防線一起收斂假警報。vibration_co_rise_sigma 只影響敘述措辭
     -- （同期振動是否也偏離），不影響是否觸發。
     '{"sigma":2.5,"consecutive_readings":3,"vibration_co_rise_sigma":1.0}',
     'tempAVG 相對基準持續上升；一併呈現同期振動有無同步變化（不判定成因）');

-- 各簽核階段的 SLA
CREATE TABLE sla_config (
    stage      TEXT PRIMARY KEY,
    sla_days   INT  NOT NULL,
    is_active  BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO sla_config (stage, sla_days) VALUES
    ('open',                 5),   -- 待設備工程師回覆
    ('engineer_replied',     5),   -- 待主管審核
    ('supervisor_reviewed',  5);   -- 待專家複審


-- =============================================================
-- 六、Finding 與四階段簽核
-- =============================================================

CREATE TABLE finding (
    finding_id       BIGSERIAL PRIMARY KEY,
    finding_key      TEXT NOT NULL UNIQUE,        -- {target_type}:{target}:{issue_type}
    device_id        TEXT   REFERENCES device(device_id) ON DELETE CASCADE,
    point_id         BIGINT REFERENCES measure_point(point_id) ON DELETE SET NULL,
    target_type      TEXT NOT NULL CHECK (target_type IN ('device','point','global')),
    target           TEXT NOT NULL,
    issue_type       TEXT NOT NULL,
    family           TEXT NOT NULL CHECK (family IN ('oscillating','monotonic','event','none')),
    rule_code        TEXT REFERENCES rule_config(rule_code),

    title            TEXT NOT NULL,
    detail           TEXT,
    severity         TEXT NOT NULL CHECK (severity IN ('err','warn','ok')),
    peak_severity    TEXT NOT NULL CHECK (peak_severity IN ('err','warn','ok')),

    -- 四階段簽核
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                        'open',                 -- 待設備工程師回覆
                        'engineer_replied',     -- 待工程師主管審核
                        'supervisor_reviewed',  -- 待專家複審
                        'expert_reviewed',      -- 待結案
                        'closed',               -- 已結案
                        'auto_resolved',        -- 數值回歸門檻內，系統自動結案
                        'false_positive')),     -- 誤報
    stage_entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_to      BIGINT REFERENCES app_user(user_id),

    -- 追蹤
    occurrence_count INT NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    baseline_value   NUMERIC(16,6),
    current_value    NUMERIC(16,6),
    value_unit       TEXT,
    evidence         JSONB,                       -- 判定依據的完整數值

    -- Agent 護欄（計畫書 §8.2）：此證據能支撐到什麼程度
    interpretation_limit TEXT,

    escalated_at     TIMESTAMPTZ,                 -- 非 NULL 代表處理中但仍持續惡化
    expected_resolution_date DATE,
    needs_expert_measurement BOOLEAN NOT NULL DEFAULT FALSE,

    source           TEXT NOT NULL DEFAULT 'rule_engine'
                     CHECK (source IN ('rule_engine','agent','manual')),
    resolved_at      TIMESTAMPTZ,
    resolved_by      TEXT,                        -- 'auto' 或 user_id 字串
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN finding.interpretation_limit IS
    '明確標示此證據的解讀邊界，供 agent 遵守「不臆測故障類型」的護欄';
COMMENT ON COLUMN finding.escalated_at IS
    '系統判定持續惡化；週報不可因狀態為「處理中」而輕描淡寫';
COMMENT ON COLUMN finding.target_type IS
    '判定對象層級：device=整台設備（如 STANDBY_NO_RUNTIME）；'
    'point=特定量測點（多數規則屬此類）；global=全廠層級，不對應特定設備';
COMMENT ON COLUMN finding.family IS
    '沿用觸發規則的 rule_config.family（見該欄位說明）；獨立存一份是因為規則設定'
    '未來可能異動，但既有 Finding 應保留當初觸發時的判準家族，不隨設定變更回溯改變';
COMMENT ON COLUMN finding.source IS
    '此 Finding 的建立來源：rule_engine=每日排程的規則引擎自動建立；'
    'agent=由 Agent 產生（規劃中，尚未啟用）；manual=人工建立';

CREATE INDEX idx_finding_open     ON finding(status, severity, stage_entered_at)
    WHERE status NOT IN ('closed','auto_resolved','false_positive');
CREATE INDEX idx_finding_device   ON finding(device_id, status);
CREATE INDEX idx_finding_assigned ON finding(assigned_to)
    WHERE status NOT IN ('closed','auto_resolved','false_positive');
CREATE INDEX idx_finding_escalated ON finding(escalated_at) WHERE escalated_at IS NOT NULL;

-- 各階段回覆
CREATE TABLE finding_note (
    note_id     BIGSERIAL PRIMARY KEY,
    finding_id  BIGINT NOT NULL REFERENCES finding(finding_id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,                    -- 寫入當下的簽核階段
    author_id   BIGINT REFERENCES app_user(user_id),
    author_role TEXT REFERENCES app_role(role_code),
    is_human    BOOLEAN NOT NULL DEFAULT TRUE,    -- FALSE = 系統或 agent 產出
    note        TEXT NOT NULL,
    action_taken TEXT,
    root_cause   TEXT,                            -- 現場確認的實際原因（RAG 語料核心）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN finding_note.root_cause IS
    '工程師現場確認的實際原因；累積後成為 RAG 歷史案例庫，是系統長期價值所在';

CREATE INDEX idx_note_finding ON finding_note(finding_id, created_at);
CREATE INDEX idx_note_human   ON finding_note(finding_id, created_at DESC) WHERE is_human;

-- 狀態轉換歷史（SLA 與積壓統計的依據）
CREATE TABLE finding_status_history (
    history_id  BIGSERIAL PRIMARY KEY,
    finding_id  BIGINT NOT NULL REFERENCES finding(finding_id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by  BIGINT REFERENCES app_user(user_id),
    duration_in_from_status INTERVAL,             -- 停留在前一階段的時間
    note        TEXT
);
CREATE INDEX idx_status_hist ON finding_status_history(finding_id, changed_at);


-- =============================================================
-- 七、報告
-- =============================================================

CREATE TABLE weekly_report (
    report_id      BIGSERIAL PRIMARY KEY,
    report_type    TEXT NOT NULL DEFAULT 'weekly' CHECK (report_type IN ('weekly','daily')),
    period_label   TEXT NOT NULL,                 -- 如 '2026-W35'
    period_start   DATE NOT NULL,
    period_end     DATE NOT NULL,
    verdict        TEXT CHECK (verdict IN ('err','warn','ok')),
    headline       TEXT,
    agent_payload  JSONB,                         -- agent 回傳的 verdict/headline/actions/notes
    html           TEXT,                          -- 系統排版後的成品
    new_count      INT DEFAULT 0,
    tracking_count INT DEFAULT 0,
    resolved_count INT DEFAULT 0,
    generated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (report_type, period_label)
);


-- =============================================================
-- 八、稽核
-- =============================================================

CREATE TABLE audit_log (
    audit_id   BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,                     -- user_id / 'agent' / 'system'
    action     TEXT NOT NULL,
    target     TEXT,
    detail     JSONB
);
CREATE INDEX idx_audit_time ON audit_log(occurred_at DESC);


-- =============================================================
-- 九、便利檢視
-- =============================================================

-- 未結案事項 + 最新人工回覆（對應 API 的 get_open_findings）
CREATE VIEW v_open_finding AS
SELECT
    f.*,
    d.device_name, d.building, d.floor, d.system_name, d.is_standby,
    mp.position,
    EXTRACT(DAY FROM now() - f.first_seen_at)::INT       AS days_open,
    EXTRACT(DAY FROM now() - f.stage_entered_at)::INT    AS days_in_stage,
    s.sla_days,
    (s.sla_days IS NOT NULL
     AND now() - f.stage_entered_at > (s.sla_days || ' days')::INTERVAL) AS is_sla_breached,
    (SELECT jsonb_build_object(
                'author', u.display_name, 'role', n.author_role,
                'note', n.note, 'created_at', n.created_at)
       FROM finding_note n
       LEFT JOIN app_user u ON u.user_id = n.author_id
      WHERE n.finding_id = f.finding_id AND n.is_human
      ORDER BY n.created_at DESC LIMIT 1)                AS latest_note
FROM finding f
JOIN device d        ON d.device_id = f.device_id
LEFT JOIN measure_point mp ON mp.point_id = f.point_id
LEFT JOIN sla_config s     ON s.stage = f.status AND s.is_active
WHERE f.status NOT IN ('closed','auto_resolved','false_positive');

-- 設備最新狀態（Dashboard 全廠總覽）
CREATE VIEW v_device_status AS
SELECT
    d.device_id, d.device_name, d.building, d.floor, d.system_name,
    d.is_standby, d.iso_machine_class, d.iso_class_source,
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
