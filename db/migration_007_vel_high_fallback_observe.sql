-- =============================================================
-- migration 007：VEL_HIGH 相對基準路徑的把關（試跑前的告警品質調整）
--
-- 適用對象：已經用舊版 db/schema.sql 或 migration_004～006 建好資料庫的環境。
-- 全新建庫者不需要執行——schema.sql 已包含本變更。
--
-- 可重複執行：只補「還不存在的鍵」，現場已經手動調過的值一律保留。
--
-- 補上三個參數（理由與實測數字見 vibcore/rules/metric_rules.py 的 vel_high
-- docstring 與 docs/DECISIONS_2026-09_expert_review.md 的「新一輪 episodes」一節）：
--
--   min_rel_change     0.25   相對基準的增幅下限
--   min_abs_change     0.10   絕對增幅下限（mm/s）
--   fallback_severity  observe  退回 sigma 的結果只列觀察名單，不派工
--
-- 行為改變：規則程式碼在沒有這三個鍵時也採用同樣的預設值，所以**即使沒有執行本
-- migration，行為也相同**；執行它是為了讓 rule_config 與 API 的
-- get_vibration_thresholds 如實顯示現行門檻，並讓之後要調整的人看得到有這幾個旋鈕。
--
-- 要改回「退回 sigma 的結果也派工」：
--   UPDATE vib.rule_config
--      SET params = params || '{"fallback_severity":"warn"}'::jsonb
--    WHERE rule_code = 'VEL_HIGH';
-- 要取消實用顯著性下限（回到純 σ）：把 min_rel_change、min_abs_change 設為 0。
-- =============================================================

SET search_path TO vib, public;

BEGIN;

-- 預設值放在 || 的左邊、既有 params 放右邊：同名鍵以既有值為準。
UPDATE rule_config
   SET params = jsonb_build_object(
                    'min_rel_change', 0.25,
                    'min_abs_change', 0.10,
                    'fallback_severity', 'observe') || params,
       description = 'velRMS 相對 ISO 錨定門檻偏高（不適用 ISO 時退回相對基準，預設只列觀察名單）',
       updated_at = now()
 WHERE rule_code = 'VEL_HIGH'
   AND NOT (params ? 'min_rel_change' AND params ? 'min_abs_change'
            AND params ? 'fallback_severity');

COMMIT;

-- 驗證（應回傳一列，params 含上述三個鍵）：
--   SELECT rule_code, params FROM rule_config WHERE rule_code = 'VEL_HIGH';
