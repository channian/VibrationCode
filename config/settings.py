# ============================================================
# VFDEdgeHealthModel v2 — 全域設定
# ============================================================

# --- 資料管線 ---
CREST_FACTOR_SPIKE_THRESHOLD = 20   # Crest Factor 超過此值視為感測器突波，丟棄
CURRENT_ON_THRESHOLD = 1.0          # A，電流判斷開機門檻
VTRMS_ON_THRESHOLD = 0.1            # mm/s，無電流時以 Total_vRMS 判斷開機
MERGE_TOLERANCE_MIN = 5             # 電流對齊允許誤差（分鐘）

# --- 基準期偵測 ---
BASELINE_WINDOW_DAYS = 14           # 滾動窗口大小（天）
BASELINE_STEP_DAYS = 1              # 滾動步長（天）
BASELINE_TOP_N = 3                  # 回傳前 N 個候選基準期
BASELINE_PREFILTER_N = 10           # 第一層掃描保留前 N 個，再進入第二層過濾
CV_THRESHOLDS = {
    'accOA':       0.20,            # 高頻摩擦穩定性門檻
    'Total_vRMS':  0.25,            # 結構振動穩定性門檻
    'Crest_Factor': 0.30,           # 衝擊脈衝穩定性門檻
}

# --- 健康模型 ---
DEFAULT_N_BINS = 3                  # 電流工況分層數（低/中/高負載）
MIN_SAMPLES_PER_BIN = 15            # 每層最低樣本數，不足則合併鄰近層
SCORE_AT_95TH = 80                  # 基準期 95th percentile 對應健康分數（λ 校準錨點）
FEATURES = ['Total_vRMS', 'accOA', 'Crest_Factor']  # 進入特徵向量的欄位

# --- 滾動基準微調（本次不實作，保留開關）---
ENABLE_ROLLING_UPDATE = False       # True 時每 30 天自動微調基準池
ROLLING_INTERVAL_DAYS = 30
ROLLING_HIGH_SCORE_THRESHOLD = 85   # 分數高於此值的資料才加入基準池
ROLLING_NEW_WEIGHT = 0.2            # 新資料權重（原基準 0.8 + 新資料 0.2）

# --- 警戒等級 ---
ALERT_NORMAL = 80                   # >= 80 → Normal
ALERT_WARNING = 60                  # 60~79 → Warning
                                    # < 60  → Critical

# --- 圖表字型（依序嘗試）---
FONTS = [
    'Microsoft JhengHei',
    'Microsoft YaHei',
    'SimHei',
    'Arial Unicode MS',
]
