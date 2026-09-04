-- =============================================================
-- migration 005：IMPACT_RISE 移除 kurtosis 通道（2026-09 專家會議定案）
--
-- 適用對象：已經用舊版 db/schema.sql 或 migration_004 建好資料庫的環境。
-- 全新建庫者不需要執行——schema.sql 已包含本變更。
--
-- 可重複執行：只有 UPDATE，重跑結果相同。
--
-- **不動任何資料欄位。** acc_kurt / acc_kurt_median / acc_kurt_axis_max /
-- acc_kurt_axis_median 全部保留：STEP_CHANGE 的多變量特徵仍在使用，
-- 且日後若換感測器或前端改算法需要重新評估。這裡只改規則參數。
--
-- 依據（完整紀錄見 docs/DECISIONS_2026-09_expert_review.md 的 Q2）：
--
-- 1. 跨設備反向——三台設備 accOA 1137.6/504.8/129.7 對應 accKURT 中位數
--    2.37/3.60/4.53，排序完全顛倒。成因是 kurtosis 為比值型指標
--    （Pearson m4/m2²），分母含背景振動量：機器越吵，σ 越大，偶發衝擊
--    越被稀釋。它反映的是「哪台比較安靜」，不是「哪台比較有問題」。
-- 2. 改成純相對基準後仍無鑑別力——移除失效的絕對門檻、聚合改用中位數
--    之後觸發結果完全沒變（33 週 511 次 / 60 台，佔機隊 88%），且門檻
--    掃描曲線平滑無斷崖（1.5→4.0 件數 −46%、告警總天數僅 −22%）。
--    問題不在門檻訂多少，在指標本身。
--
-- accCREST 保留：同樣是比值型指標，但沒有實測證據顯示它在本廠失效，
-- 且它是諧波不可用之後僅存的衝擊性通道。
-- =============================================================

SET search_path TO vib, public;

BEGIN;

-- ── IMPACT_RISE：只剩 crest 兩個通道 ────────────────────────
-- 直接覆寫整個 params 而不是逐鍵刪除：舊值可能來自 schema.sql 初始 seed、
-- migration_004、或現場手動調過，逐鍵刪除會留下不確定的殘骸。
UPDATE rule_config
   SET params = '{"crest_sigma":2.5,"crest_axis_sigma":2.5}'::jsonb,
       description = 'accCREST 相對基準顯著上升，常見於軸承或潤滑劣化（不判定成因）'
 WHERE rule_code = 'IMPACT_RISE';

COMMIT;

-- 驗證（應回傳一列，params 只有兩個鍵）：
--   SELECT rule_code, params FROM rule_config WHERE rule_code = 'IMPACT_RISE';
