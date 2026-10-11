-- =============================================================
-- migration 008：把舊庫的規則設定種子對齊現行預設（派工政策）
--
-- 適用對象：由舊版 db/schema.sql 建庫、再逐支套用 migration 的環境。
-- 全新建庫者不需要執行——schema.sql 已是對齊後的狀態。
--
-- 為什麼需要：schema.sql 的 INSERT 只在建庫那一刻執行。後來才加入的規則、後來才改成 observe 的
-- 嚴重度、後來才加的參數，舊庫不會自己跟上，而前面的 migration 只動了結構與當時那幾條規則。
-- 實測：從最早的 schema 套滿 001～007 的庫，和全新建的庫相比——
--   · 缺 TEMP_RISE 一列。finding／observation 的 rule_code 都是指向 rule_config 的外鍵，
--     缺這一列時 TEMP_RISE 的觀察項目寫不進去（排程只記錯誤、繼續跑，觀察名單裡就是少了它）
--   · AXIS_SHIFT／DEGRADE_TREND／IMPACT_RISE／SPECTRAL_SHIFT／STEP_CHANGE 的嚴重度仍是 warn
--     （現行為 observe）
--   · ISO_CLASS_SUSPECT 的 params 是空的（程式有預設值 3，結果不變，但與新庫不一致）
-- 嚴重度這一項尤其重要：正式排程現在以 rule_config.severity 為上限（types.effective_severity），
-- 停在 warn 的規則會建立 Finding 派工，等於把「這類只進觀察名單」的政策整個繞過。
--
-- 冪等且保守：
--   · 缺的規則才補（ON CONFLICT DO NOTHING）；已存在的規則不動其 params、is_active、description
--   · 嚴重度：只把六條觀察級規則從 warn 改回 observe；已是 observe、或現場設成 err 的不動。
--     若現場曾刻意把其中某條升為 warn 要派工，執行後請再升回：
--       UPDATE rule_config SET severity = 'warn' WHERE rule_code = '…';
--     （migration_008 之前，正式排程根本不讀 rule_config.severity，所以不會有「刻意升級而生效」的紀錄）
--   · ISO_CLASS_SUSPECT 的 params 只在空物件時才補
--
-- 下方的 INSERT 與 db/schema.sql 逐字相同（只多了 ON CONFLICT）；test_rules.py 會比對兩者。
-- =============================================================

SET search_path TO vib, public;

BEGIN;

-- ── 一、補上缺的規則 ────────────────────────────────────────
INSERT INTO rule_config (rule_code, rule_name, family, issue_type, severity, params, description) VALUES
    ('ISO_ZONE',           'ISO 位準分級',     'oscillating', 'iso_zone_exceed',  'err',
     -- consecutive_readings：需連續幾筆可信資料都達告警 Zone 才觸發。
     --   依 ISO 10816-3 §5.4 的實務建議（設定確認時間，避免啟動／停機／
     --   製程瞬變誤動作）。小時級資料上只看單筆等於每天擲骰子。
     '{"alert_zone":"C","consecutive_readings":3}',
     'velRMS 對照 ISO 10816-3 的 Zone A/B/C/D；未分類或適用範圍外的設備不套用'),
    ('VEL_HIGH',           '速度整體值偏高',   'oscillating', 'vel_high',         'warn',
     -- threshold_mode='iso'：門檻 = 基準 + 0.25 × Zone B 上限（封頂 1.25 倍），
     --   隨機械等級而異，來源是國際標準的分級架構而非我們自訂的統計量。
     --   注意 iso 模式判的是 velRMS——Zone 邊界就是定義在速度 RMS 上，
     --   拿去比 velOA（實測約為 velRMS 的 0.75 倍）會讓門檻悄悄鬆掉三分之一。
     --   設備未分級、或額定功率 ≤ 15 kW（不在 ISO 20816-3 適用範圍）時自動退回
     --   sigma，並在 evidence 標記 sigma_fallback 與退回原因 iso_fallback_reason。
     -- sigma 保留為對照組，供敏感度掃描並列比較。
     -- consecutive_readings：同 ISO_ZONE，見該處說明。
     -- min_rel_change／min_abs_change：相對基準路徑（sigma、sigma_fallback）的
     --   實用顯著性下限——增幅須同時達到 +25% 與 0.10 mm/s。σ 以基準期標準差為
     --   尺度，小機器穩定運轉時只有 0.01～0.02 mm/s，一點點變化就是十幾個 σ。
     --   兩個數字都沒有外部標準可引用，依試跑期的誤報回饋調整（見 migration_007）。
     -- fallback_severity：退回 sigma 的結果用什麼嚴重度。預設 observe＝只列週報的
     --   觀察名單、不建立 Finding、不進簽核與 SLA：這條路徑的門檻是我們自訂的統計量，
     --   沒有外部標準可引用。實測 11 週全部 41 件可行動的設備類告警都出自這條路徑，
     --   且多半能由轉速狀態轉換或鄰機環境振動解釋。累積回饋後可改回 warn。
     '{"threshold_mode":"iso","sigma":3.0,"consecutive_readings":3,"min_rel_change":0.25,"min_abs_change":0.10,"fallback_severity":"observe"}',
     'velRMS 相對 ISO 錨定門檻偏高（不適用 ISO 時退回相對基準，預設只列觀察名單）'),
    ('IMPACT_RISE',        '衝擊性指標上升',   'monotonic',   'impact_rise',      'observe',
     -- 判定用 median 通道（acc_*_median），不用 max：小時內 3600 個滾動窗
     --   取最大值本質上是極值統計量，隨樣本數單調上升，跨設備不可比。
     -- kurtosis 通道已於 2026-09 專家會議移除（原本的 kurt_sigma /
     --   kurt_axis_sigma / kurt_absolute / threshold_mode / require_both
     --   全數不再使用）。依據：本廠實測 kurtosis 與振動量值反向（三台
     --   accOA 1137.6/504.8/129.7 對應 accKURT 中位數 2.37/3.60/4.53），
     --   改成純相對判定後觸發結果完全沒變（33 週視窗：511 次 / 60 台，佔機隊 88%），
     --   且門檻掃描曲線平滑無斷崖——問題不在門檻訂多少，在指標本身。
     --   完整紀錄見 docs/DECISIONS_2026-09_expert_review.md 的 Q2。
     --   accKURT 欄位仍照常聚合入庫，STEP_CHANGE 的多變量特徵還在用。
     -- 逐軸門檻與合成值同量級：實測 crest 是逐軸較大（ZP 3-5 三軸
     --   4.65/5.01/4.30 vs 合成 4.08），但只有三台樣本，不足以支持
     --   預設偏鬆或偏緊。
     -- 嚴重度維持 observe：移除 kurtosis 後的觸發量尚待回測，仍需累積
     --   誤報回饋才知道精確率（見 vibcore/types.py 的 SEVERITY_OBSERVE）。
     '{"crest_sigma":2.5,"crest_axis_sigma":2.5}',
     'accCREST 相對基準顯著上升，常見於軸承或潤滑劣化（不判定成因）'),
    ('DEGRADE_TREND',      '指標持續劣化',     'monotonic',   'degradation_trend','observe',
     '{"min_days":14,"min_r2":0.3,"slope_pct_per_month":10}',
     '回歸斜率持續惡化；須在聚合後的獨立樣本上計算'),
    ('SPECTRAL_SHIFT',     '頻譜重心上移',     'monotonic',   'spectral_shift',   'observe',
     '{"shift_pct":15,"min_days":14}',
     'accWeightedMeanFreq 持續上移，代表能量往高頻移動'),
    ('AXIS_SHIFT',         '軸能量分佈偏移',   'monotonic',   'axis_shift',       'observe',
     '{"ratio_delta":0.15}',
     '排序後三軸能量佔比相對基準偏移'),
    ('STEP_CHANGE',        '多變量突變',       'monotonic',   'step_change',      'observe',
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
    ('TEMP_RISE',          '溫度相對基準上升', 'oscillating', 'temp_rise',        'observe',
     -- sigma 與 IMPACT_RISE 同量級；另有 consecutive_readings 把關，
     -- 兩道防線一起收斂假警報。vibration_co_rise_sigma 只影響敘述措辭
     -- （同期振動是否也偏離），不影響是否觸發。
     '{"sigma":2.5,"consecutive_readings":3,"vibration_co_rise_sigma":1.0}',
     'tempAVG 相對基準持續上升；一併呈現同期振動有無同步變化（不判定成因）')
ON CONFLICT (rule_code) DO NOTHING;

-- ── 二、觀察級規則的嚴重度回到 observe ────────────────────────
UPDATE rule_config
   SET severity = 'observe', updated_at = now()
 WHERE rule_code IN ('IMPACT_RISE', 'DEGRADE_TREND', 'SPECTRAL_SHIFT',
                     'AXIS_SHIFT', 'STEP_CHANGE', 'TEMP_RISE')
   AND severity = 'warn';

-- ── 三、補上預設參數（只補缺的，不覆蓋現場的值）─────────────────
UPDATE rule_config
   SET params = '{"frontend_consecutive_readings":3}'::jsonb, updated_at = now()
 WHERE rule_code = 'ISO_CLASS_SUSPECT'
   AND params = '{}'::jsonb;

-- VEL_HIGH 的 threshold_mode：程式缺這個鍵時預設 iso，行為不變；寫明白讓
-- get_vibration_thresholds 回傳給 Agent 的內容與新庫一致。現場明確設成 sigma 的不動。
UPDATE rule_config
   SET params = '{"threshold_mode":"iso"}'::jsonb || params, updated_at = now()
 WHERE rule_code = 'VEL_HIGH'
   AND NOT (params ? 'threshold_mode');

-- VEL_HIGH 的說明文字（API 會把它交給 Agent）：只在還是 migration_004 寫的舊字句時才換，現場改過的不動。
UPDATE rule_config
   SET description = 'velRMS 相對 ISO 錨定門檻偏高（不適用 ISO 時退回相對基準，預設只列觀察名單）',
       updated_at = now()
 WHERE rule_code = 'VEL_HIGH'
   AND description = 'velRMS 相對 ISO 錨定門檻偏高（未分類時退回相對基準 σ 判定）';

COMMIT;
