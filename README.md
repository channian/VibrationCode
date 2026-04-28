# VFDEdgeHealthModel v2

VFD 設備振動健康評估系統，以三軸振速、高頻加速度結合馬達電流，對旋轉設備進行分工況健康評分。

---

## 快速開始

### 1. 安裝套件

```bash
pip install -r requirements.txt
```

### 2. 準備資料（見下方資料格式說明）

```
VibrationCode/
├── Vibration_Data/      ← 放振動 CSV
├── Current_Data/        ← 放電流 CSV（選填）
└── device_mapping.csv   ← 編輯設備對應表
```

### 3. 執行完整管線

```bash
# 一次完整執行（所有設備）
python test_model.py

# 只跑指定設備
python test_model.py --device ZP1_2_M1

# 加印模型有效性診斷報告
python test_model.py --diagnose

# 跳過 PNG 產出（純數字驗證）
python test_model.py --no-report
```

---

## 執行流程（五個階段）

| 階段 | 指令 | 說明 | 輸出 |
|------|------|------|------|
| 1 | `python validate_pipeline.py` | 資料驗證，確認讀取正常 | `output/pipeline_validation.csv` |
| 2 | *(整合於管線內)* | 自動偵測基準期 | `output/baseline_candidates/*.csv` |
| 3 | `python test_model.py` | 模型訓練與推論 | `output/scores/*.csv`、`output/models/*.pkl` |
| 4 | `python test_model.py` | 產出 PNG 診斷報告 | `output/reports/*.png`、`output/device_summary.csv` |
| 5 | `python run_pdm.py [--device X]` | 完整一鍵執行 | 同上 |

> 階段 1 是獨立的驗證步驟，不影響主管線。
> 階段 2–4 整合在 `test_model.py` 與 `run_pdm.py` 中自動完成。

---

## 輸出檔案說明

| 檔案 | 說明 |
|------|------|
| `output/scores/{device_id}_scores.csv` | 每筆量測的健康分數（含 load_bin、alert_level）|
| `output/reports/{device_id}_{position}_report.png` | 2×2 PNG 診斷儀表板 |
| `output/models/{device_id}.pkl` | 訓練好的模型（pickle）|
| `output/baseline_candidates/{device_id}_candidates.csv` | 基準期候選（供人工確認）|
| `output/device_summary.csv` | 各設備最新分數彙整（M1/M2 合併）|

### scores CSV 欄位

| 欄位 | 說明 |
|------|------|
| `datetime` | 時間戳記 |
| `device_id` | 設備 ID（如 ZP1_2_M1）|
| `position` | 量測位置（M1 自由端 / M2 驅動端）|
| `load_bin` | 電流工況分層編號（0=低負載，依此類推）|
| `health_score` | 原始健康分數（0–100）|
| `health_score_smooth` | 滾動中位數平滑後分數（建議用於趨勢判斷）|
| `alert_level` | Normal / Warning / Critical |
| `Total_vRMS` | 三軸振速合力 (mm/s) |
| `accOA` | 高頻加速度總量 (g) |
| `Crest_Factor` | 衝擊脈衝指標（峰值/均值）|
| `current_A` | 馬達電流 (A)（有電流資料時填入）|

### device_summary CSV 欄位

| 欄位 | 說明 |
|------|------|
| `machine_id` | 設備唯一識別碼 |
| `model_group` | 型號分組 |
| `latest_score_M1` | M1 最新健康分數 |
| `latest_score_M2` | M2 最新健康分數 |
| `overall_score` | 整體分數（M1/M2 取最小值）|
| `alert_level` | 依 overall_score 判定 |
| `baseline_start` | 基準期起點 |
| `baseline_end` | 基準期終點 |
| `data_count` | 有效量測筆數（M1+M2 合計）|

---

## 資料格式規範

### 振動 CSV（Vibration_Data/）

#### 檔案命名規則（必須符合）

```
[冰水_]{設備名}_{M1|M2}_Analytics{任意後綴}.csv
```

| 範例檔名 | 解析結果 |
|----------|----------|
| `冰水_ZP1_2_M1_Analytics_202601.csv` | device=ZP1_2, position=M1 |
| `ZP1_2_M2_Analytics_2026.csv` | device=ZP1_2, position=M2 |

> **注意**：檔名必須包含 `M1` 或 `M2`，以及 `Analytics` 關鍵字，否則會被跳過。

#### 必要欄位

| 欄位名稱 | 說明 |
|----------|------|
| `Date` 或 `Time`（含此字眼即可）| 時間戳記，格式 `2026/3/1 00:00:00` |
| `accOA` | 高頻加速度總量 (g) |
| `velRMS_x` | X 軸振速 RMS (mm/s) |
| `velRMS_y` | Y 軸振速 RMS (mm/s) |
| `velRMS_z` | Z 軸振速 RMS (mm/s) |
| `velPEAK` | 振速峰值 (mm/s)（計算 Crest Factor 用）|
| `velRMS` | 振速 RMS 整體 (mm/s)（計算 Crest Factor 用）|

> `velRMS_x/y/z` 三欄缺少時，`Total_vRMS` 會為 NaN（不報錯，但模型精度下降）。
> `velPEAK`/`velRMS` 缺少時，`Crest_Factor` 會為 NaN（衝擊特徵無法使用）。

#### 範例（前幾行）

```
Date,accOA,velRMS_x,velRMS_y,velRMS_z,velPEAK,velRMS
2026/1/15 08:00:00,0.45,0.82,1.12,0.67,2.31,1.23
2026/1/15 08:10:00,0.47,0.85,1.15,0.69,2.38,1.26
```

---

### 電流 CSV（Current_Data/）— 選填

#### 必要欄位

| 欄位名稱 | 說明 |
|----------|------|
| `Date` 或 `Time`（含此字眼即可）| 時間戳記 |
| `tagname`（或 `TagName`、`tag`）| 設備電流識別碼（對應 device_mapping.csv 的 tagname）|
| `value`（或 `Value`、`current`）| 電流值（A） |

#### 範例

```
datetime,tagname,value
2026/1/15 08:00:00,CWP_CUR_AVG_01,12.4
2026/1/15 08:00:00,CWP_CUR_AVG_02,11.8
```

> 沒有電流資料時，程式改用 `Total_vRMS > 0.1 mm/s` 判斷開機狀態，健康評分仍可執行，但無法依負載分層。

---

### device_mapping.csv — 設備對應表

```csv
tagname,devicename,machine_id,model_group,train_start,train_end
CWP_CUR_AVG_01,ZP1_2,ZP1_2,Pump_A,,
CWP_CUR_AVG_02,ZP1_3,ZP1_3,Pump_A,,
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| `tagname` | ✅ | 電流 CSV 中的 tagname，用於對齊電流 |
| `devicename` | ✅ | **必須與振動檔名解析出的設備名稱完全一致**（如 `ZP1_2`，不含 _M1/_M2）|
| `machine_id` | ✅ | 設備唯一識別碼（輸出報告用）|
| `model_group` | ✅ | 型號分組（如 `Pump_A`），同型號設備共享統計 |
| `train_start` | ⬜ 選填 | 人工確認後填入的基準期起點（格式：`2026-01-01`）|
| `train_end` | ⬜ 選填 | 人工確認後填入的基準期終點 |

> `train_start`/`train_end` 留空時，程式自動偵測基準期。
>
> **重要**：`devicename` 欄位填設備基礎名稱（如 `ZP1_2`），**不要**填 `ZP1_2_M1` 或 `ZP1_2_M2`。
> 同一設備的 M1 和 M2 共用同一筆 mapping 記錄。

---

## 模型有效性診斷（`--diagnose`）

執行 `python test_model.py --diagnose` 會輸出以下七個診斷區塊：

| 區塊 | 內容 | 判讀重點 |
|------|------|---------|
| 【1】Load Bin 邊界 | 電流分層邊界 + 評分期電流是否超出訓練範圍 | 出現 ⚠️ 代表負載補償可能失效 |
| 【2】各 Bin 訓練參數 | λ 值、每層樣本數、基準期均分 | avg_baseline_score 應 >= 85 |
| 【3】基準期特徵 CV | 三個特徵的變異係數 | 超過門檻代表基準期不穩定 |
| 【4】同 Bin accOA 比較 | 基準期 vs 近期同工況 accOA 中位數 | 比值 > 1.15 確認真實劣化 |
| 【5】特徵相關性 | Health_Score 與各特徵的 Pearson r | 應為負相關（r < -0.3）|
| 【6】各 Bin 分數 std | 分數標準差 | std > 15 可能有 Bin 跳動 |
| 【7】平滑效果 | 原始 vs 平滑後分數標準差 | 確認平滑是否有效降噪 |

---

## 常見錯誤排查

### `IndexError: single positional indexer is out-of-bounds`

**原因**：振動 CSV 的時間欄位解析失敗，所有列被丟棄，導致 DataFrame 為空。

**排查步驟**：
1. 確認時間欄位名稱含有 `Date` 或 `Time` 字眼
2. 確認時間格式為 `2026/3/1 00:00:00`（使用 `dayfirst=False` 解析）
3. 確認年份在 2020–2030 之間（超出會印警告但不刪除）

### 電流資料沒有對齊（`has_current=No`）

1. 確認 `Current_Data/` 資料夾存在且有 CSV
2. 確認 `device_mapping.csv` 的 `tagname` 與電流 CSV 的 tagname 欄位值相符
3. 確認 `device_mapping.csv` 的 `devicename` 與振動檔名解析結果相符（不含 _M1/_M2 後綴）
4. 查看 console 輸出，有詳細診斷訊息指出找不到的 tagname

### `No CSV files found` 警告

振動/電流資料夾為空，或 CSV 放錯位置。

### `Cannot parse device info from filename` 警告

檔名不符合命名規則，確認包含 `M1`/`M2` 與 `Analytics` 關鍵字。

### `missing required columns` 錯誤

CSV 缺少必要欄位，對照上方欄位表確認。

---

## 參數設定（config/settings.py）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `CREST_FACTOR_SPIKE_THRESHOLD` | 20 | 超過此值視為感測器突波，丟棄 |
| `CURRENT_ON_THRESHOLD` | 1.0 A | 電流開機判斷門檻 |
| `VTRMS_ON_THRESHOLD` | 0.1 mm/s | 無電流時的開機判斷門檻 |
| `MERGE_TOLERANCE_MIN` | 5 min | 電流對齊允許時間誤差 |
| `BASELINE_WINDOW_DAYS` | 14 | 基準期偵測滾動窗口（天）|
| `CV_THRESHOLDS` | 0.20/0.25/0.30 | 各特徵穩定性門檻（accOA/vRMS/CF）|
| `DEFAULT_N_BINS` | 3 | 電流工況分層數（低/中/高）|
| `MIN_SAMPLES_PER_BIN` | 15 | 每層最低樣本數，不足則合併 |
| `SCORE_AT_95TH` | 80 | λ 校準錨點（基準期 95th pct → 80 分）|
| `SCORE_SMOOTH_WINDOW` | 5 | 輸出分數平滑窗口（筆數）|
| `ALERT_NORMAL` | 80 | 健康分數 >= 80 → Normal |
| `ALERT_WARNING` | 60 | 健康分數 60–79 → Warning |

---

## 目錄結構

```
VibrationCode/
├── test_model.py                # 階段三/四驗證腳本（主要使用）
├── validate_pipeline.py         # 階段一資料驗證腳本
├── run_pdm.py                   # 階段五主程式（CLI）
├── requirements.txt
├── device_mapping.csv           # 設備對應表
├── config/
│   └── settings.py              # 全域參數設定
├── src/
│   ├── device_parser.py         # 檔名解析
│   ├── data_loader.py           # 資料讀取與電流對齊
│   ├── filters.py               # 清洗、衍生特徵、平滑
│   ├── baseline_detector.py     # 基準期自動偵測
│   ├── health_model.py          # VFDEdgeHealthModel（Mahalanobis）
│   └── reporter.py              # PNG 報告與設備總覽產出
├── Vibration_Data/              # 放振動原始 CSV
├── Current_Data/                # 放電流原始 CSV（選填）
└── output/
    ├── reports/                 # PNG 診斷儀表板
    ├── scores/                  # CSV 健康分數明細
    ├── models/                  # 訓練好的模型（.pkl）
    └── baseline_candidates/     # 基準期候選（供人工確認）
```
