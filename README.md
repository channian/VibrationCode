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

### 3. 資料驗證（階段一）

```bash
python validate_pipeline.py
```

輸出：`output/pipeline_validation.csv`

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
| `tagname` | 設備電流識別碼（對應 device_mapping.csv 的 tagname）|
| `value` | 電流值（A） |

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
| `devicename` | ✅ | **必須與振動檔名解析出的設備名稱完全一致**（如 `ZP1_2`）|
| `machine_id` | ✅ | 設備唯一識別碼（輸出報告用）|
| `model_group` | ✅ | 型號分組（如 `Pump_A`），同型號設備共享統計 |
| `train_start` | ⬜ 選填 | 人工確認後填入的基準期起點（格式：`2026-01-01`）|
| `train_end` | ⬜ 選填 | 人工確認後填入的基準期終點 |

> `train_start`/`train_end` 留空時，程式自動偵測基準期（見階段二）。

---

## 常見錯誤排查

### `IndexError: single positional indexer is out-of-bounds`

**原因**：振動 CSV 的時間欄位解析失敗，所有列被丟棄，導致 DataFrame 為空。

**排查步驟**：

1. 確認時間欄位名稱含有 `Date` 或 `Time` 字眼
2. 確認時間格式為 `2026/3/1 00:00:00`（使用 `dayfirst=False` 解析）
3. 確認年份在 2020–2030 之間（超出會印警告但不刪除）

### `No CSV files found` 警告

振動/電流資料夾為空，或 CSV 放錯位置。

### `Cannot parse device info from filename` 警告

檔名不符合命名規則，確認包含 `M1`/`M2` 與 `Analytics` 關鍵字。

### `missing required columns` 錯誤

CSV 缺少必要欄位，對照上方欄位表確認。

---

## 執行流程（五個階段）

| 階段 | 指令 | 說明 |
|------|------|------|
| 1 | `python validate_pipeline.py` | 資料驗證，確認讀取正常 |
| 2 | *(開發中)* | 自動偵測基準期 |
| 3 | *(開發中)* | 模型訓練與推論 |
| 4 | *(開發中)* | 產出 PNG 報告與 CSV 分數 |
| 5 | `python run_pdm.py [--device X]` | 完整一鍵執行 |

---

## 目錄結構

```
VibrationCode/
├── run_pdm.py                   # 主程式（階段五）
├── validate_pipeline.py         # 資料驗證腳本
├── requirements.txt
├── device_mapping.csv           # 設備對應表
├── config/
│   └── settings.py              # 全域參數設定
├── src/
│   ├── device_parser.py         # 檔名解析
│   ├── data_loader.py           # 資料讀取與對齊
│   ├── filters.py               # 清洗與衍生欄位
│   ├── baseline_detector.py     # 基準期偵測（階段二）
│   ├── health_model.py          # 健康模型（階段三）
│   └── reporter.py              # 報告產出（階段四）
├── Vibration_Data/              # 放振動原始 CSV
├── Current_Data/                # 放電流原始 CSV
└── output/
    ├── reports/                 # PNG 診斷報告
    ├── scores/                  # CSV 健康分數
    ├── models/                  # 訓練好的模型檔
    └── baseline_candidates/     # 基準期候選（供人工確認）
```

---

## 參數設定（config/settings.py）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `CREST_FACTOR_SPIKE_THRESHOLD` | 20 | 超過此值視為感測器突波，丟棄 |
| `CURRENT_ON_THRESHOLD` | 1.0 A | 電流開機判斷門檻 |
| `VTRMS_ON_THRESHOLD` | 0.1 mm/s | 無電流時的開機判斷門檻 |
| `BASELINE_WINDOW_DAYS` | 14 | 基準期偵測滾動窗口（天）|
| `DEFAULT_N_BINS` | 3 | 電流工況分層數（低/中/高）|
| `ALERT_NORMAL` | 80 | 健康分數 >= 80 → Normal |
| `ALERT_WARNING` | 60 | 健康分數 60–79 → Warning |
