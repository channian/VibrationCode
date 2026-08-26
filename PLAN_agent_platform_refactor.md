# 振動監測平台 Agent 化重構 — 計畫書

**分支**：`claude/agent-platform-refactor`
**狀態**：規劃稿（第 2 版，已納入實際資料樣本與 HVM 整合模式）
**前提**：本案定位為「常見三軸 SENSOR 的例行監測」，深入檢測由既有的量測專家系統負責，兩者不重疊。

---

## 一、目標與範圍

### 功能需求
1. **Dashboard** — 設備健康總覽
2. **週報功能** — HTML 格式，含 agent 評論
3. **異常事件回覆** — agent 依事件上下文產出回覆建議

### 分階段策略（因應時程壓力）

| 階段 | 目標 | 內容 |
|------|------|------|
| **Phase 1** | **跑通 agent 驗證迴路** | 資料層抽象、時域指標 + ISO 分級 + 趨勢規則、Finding 閉環追蹤、API（比照 HVM 形式）、週報 HTML、簡易 Dashboard |
| **Phase 2** | 診斷深化與正式資料源 | 諧波故障診斷、雙 DB 正式接入、RAG 語料建置、Dashboard 完整化 |

Phase 1 的判準是「agent 能拿到足夠的結構化素材，產出有工程價值的評論」，不是「所有資料源都接完」。因此 Phase 1 允許以現有檔案作為資料來源，DB 接入平行進行，避免 IT 權限流程卡住驗證時程。

---

## 二、資料資產盤點（依實際樣本修正）

### 實際欄位規模

以 AHU 樣本（定頻 1710 rpm）核對後，實際欄位數約 **628 欄**，遠多於初版清單的 ~200 欄：

| 群組 | 組成 | 欄位數 |
|------|------|--------|
| 加速度時域 | accRMS / accPEAK / accCREST / accSKEW / accKURT（各 ×4：x,y,z,合成） | 20 |
| 速度時域 | velRMS / velPEAK（各 ×4） | 8 |
| 位移時域 | dispRMS / dispP2P（各 ×4） | 8 |
| 整體值 | accOA ×4、velOA ×4 | 8 |
| 頻譜摘要 | acc/vel 各 MeanPeakFreq / MeanPeakInt / WeightedMeanFreq（各 ×4） | 24 |
| TOP5 峰值 | acc/vel 各 (FREQ, V) ×5 ×4 | 80 |
| **加速度諧波** | **accH01~H30 × (FREQ, V, OA) × 4** | **360** |
| **速度諧波** | **velH01~H10 × (FREQ, V, OA) × 4** | **120** |

初版清單遺漏的兩項，都是重要資產：

1. **每個諧波有第三個欄位 `_OA`**（不只 FREQ 與 V）
2. **整組速度諧波 velH01~H10** — 對低階故障（不平衡、對心不良、鬆動）而言，**速度諧波比加速度諧波更直接可用**，因為 ISO 體系的低頻診斷本來就建立在速度域

### 關鍵驗證結果（來自實際樣本）

| 驗證項 | 結果 |
|--------|------|
| **H01 = 1X 轉速** | ✅ 確認。1710 rpm → 28.5 Hz，實測 `accH01FREQ` = 28.6~28.7 Hz。**諧波為階次基準，Phase 2 故障診斷可行** |
| 合成值計算方式 | `velRMS`、`accRMS` = √(x²+y²+z²) 三軸向量和（誤差 0） |
| `accCREST` 定義 | = accPEAK / accRMS（誤差 0） |
| `dispRMS` 單位 | **mm**（以 velRMS/(2πf) 反推同數量級驗證） |
| `velRMS` 單位 | 推定 **mm/s**（1.5 mm/s 落在 AHU 合理區間，且與 dispRMS 換算自洽） |
| `accOA` 合成方式 | ⚠️ **不是**三軸向量和（583.9）也不是直接相加（771.1），標示值為 665.4 — 計算方式待確認 |
| `accRMS` vs `accOA` | ⚠️ 相差 **549 倍**，兩者單位不同，需確認 |

### ⚠️ 發現一：諧波頻率是「搜尋窗內取峰值」，非精確倍數

x 軸前 10 階實測 vs 精確 nX：

| 階 | 實測 | 精確 nX | 誤差 |
|----|------|---------|------|
| 1 | 28.7 | 28.5 | +0.2 |
| 2 | 54.0 | 57.0 | **−3.0** |
| 3 | 85.9 | 85.5 | +0.4 |
| 4 | 112.1 | 114.0 | −1.9 |
| 5 | 137.8 | 142.5 | **−4.7** |
| 6 | 166.7 | 171.0 | **−4.3** |
| 7 | 195.1 | 199.5 | **−4.4** |

前端程式顯然是在每個預期諧波附近的搜尋窗內找局部最大值。這是正確做法，但**代表回報的「H05」有可能鎖到鄰近的非諧波峰值**（例如 H05 報 137.8 Hz，但 5X 應在 142.5 Hz，差 4.7 Hz）。

**影響**：Phase 2 做諧波診斷時，若直接採信 `accH05FREQ_V` 為 5X 振幅，可能把非諧波能量誤判為對心不良等故障。需要在指標層加一道**階次容差驗證**：檢查 `H0nFREQ` 與 `n × 1X` 的偏差是否在容許範圍內，超出則標記該階不可信。

### ⚠️ 發現二：PEAK 類欄位跨筆凍結，暗示滾動窗口

連續三筆（每分鐘一筆）比對：

| 欄位 | 第 1 筆 | 第 2 筆 | 第 3 筆 | 現象 |
|------|---------|---------|---------|------|
| `accPEAK` | 19.359011 | 19.359011 | 19.359024 | **幾乎完全相同** |
| `velPEAK` | 24.815104 | 24.815104 | 24.817379 | **幾乎完全相同** |
| `dispP2P` | 0.068602 | 0.068602 | 0.068709 | **幾乎完全相同** |
| `accRMS` | 1.211018 | 1.242274 | 1.266135 | 緩慢單調上升 |
| `accOA` | 665.43 | 697.76 | 723.87 | 緩慢單調上升 |

PEAK 類凍結、RMS 類緩慢單調變化，強烈暗示前端計算的是**滾動/累積窗口統計**，而非獨立的每分鐘區塊。

**影響很大**：若相鄰資料點不獨立，所有趨勢分析（線性回歸斜率、變化率、樣本數）都會有**自我相關**問題——會嚴重高估趨勢的統計信心，讓規則層產生大量假警報。需確認實際窗口語意後，決定是否要先降採樣為真正獨立的樣本再做趨勢判定。

### 發現三：此台 AHU 的衝擊性指標偏高，值得實機確認

| 指標 | 實測 | 健康機械典型值 |
|------|------|---------------|
| `accCREST` | **16.0** | 3 ~ 5 |
| `accKURT` | **68.6** | ~3 |
| `velRMS` | 1.51 mm/s | ISO Zone A/B（良好） |

**速度域看起來正常，但加速度域顯示明顯的衝擊性訊號**。高 Crest + 高 Kurtosis 是典型的脈衝型訊號特徵（軸承缺陷、鬆動、或撞擊）。另外三軸在 **682.3 Hz（≈ 24X）** 都有明顯峰值，x 軸該峰佔整體 24.6%。

> 這正好示範了 Phase 1 為何必須同時納入加速度域指標：**只看 velRMS 與 ISO 分級會完全漏掉這台機器的異常**。是否為真實故障需實機確認（若風機葉片數為 24，682 Hz 即為葉片通過頻率，屬正常氣動特徵；若非，則需查軸承）。

---

## 三、與地端 Agent 的整合模式（比照 HVM）

既有 HVM Agent 平台的設計已在公司內驗證可行，本系統**直接沿用其模式**，讓 agent 平台團隊的上手成本降到最低。

### 沿用的設計

| HVM 設計 | 本系統對應 |
|----------|-----------|
| `X-HVM-API-Key` header、GET 查詢 / POST 寫入、function-calling schema | 相同結構，改為 `X-VIB-API-Key` |
| 唯讀工具多支 + 唯一寫入型 `send_report` | 相同：多支查詢工具 + 單一 `send_report` |
| **Finding 閉環追蹤**（`finding_key`、`occurrence_count`、`escalated_at`、`latest_note`、狀態機） | **完整沿用**，這正好對應「每日套規則、週報時 agent 檢視是否回歸正常」的需求 |
| **三家族判準**（波動型 / 單調累積型 / 事件型） | **完整沿用**，振動指標同樣分這三類 |
| 信件三段式（新發現 / 追蹤中 / 已解決）**由系統決定，非 agent** | 相同：agent 只給評論，分段由本系統依 DB 決定 |
| `send_report` 只收結構化欄位、不收 raw HTML；收件人固定；每日次數上限 + 稽核 log | 相同四道卡控 |
| 「只給數據，不給標準」→ 另有 `get_alert_thresholds` | 相同：另設 `get_vibration_thresholds` 回傳 ISO 門檻與規則設定 |

### 三家族判準對應到振動指標

分類依據是「這個指標會不會自己恢復」：

| 家族 | family | 涵蓋的 issue_type | 判準邏輯 |
|------|--------|-------------------|---------|
| **A 波動型** | `oscillating` | `iso_zone_exceed`、`vel_high`、`acc_high` | 隨負載/工況波動會回落 → 需**形態判準**區分「零星尖峰」與「持續超標」 |
| **B 單調累積型** | `monotonic` | `impact_rise`（Crest/Kurtosis 上升）、`degradation_trend` | 機械劣化不會自己回復 → 判準是**趨勢斜率與到達門檻的時間** |
| **C 事件型** | `event` | `sensor_offline`、`data_quality`、`shock_event` | 二元狀態或短時間計數 |

對應 HVM 的 `get_anomaly_pattern` / `get_capacity_trend`，本系統提供：

- `get_vibration_pattern`（A 型專用）— 回傳 `sustained` / `recurring` / `recurring_scheduled` / `sporadic` / `normal`，讓 agent 知道是「持續超標要處理」還是「零星尖峰不用理」
- `get_degradation_trend`（B 型專用）— 線性回歸取劣化斜率，推估「照此速度約 N 天後觸及門檻」，附 `confidence`（樣本數 + R²）

> **`get_degradation_trend` 必須先解決發現二的自我相關問題**，否則 R² 會虛高，推估天數不可信。

### issue_type 枚舉（Phase 1）

| 值 | 含意 | family |
|----|------|--------|
| `iso_zone_exceed` | ISO 區間超標 | oscillating |
| `vel_high` | 速度整體值偏高 | oscillating |
| `acc_high` | 加速度整體值偏高 | oscillating |
| `impact_rise` | 衝擊指標上升（Crest/Kurtosis） | monotonic |
| `degradation_trend` | 指標持續劣化 | monotonic |
| `axis_shift` | 三軸能量分佈異常偏移 | monotonic |
| `sensor_offline` | 感測器離線 | event |
| `data_quality` | 資料品質異常（缺漏/零值） | event |
| `harmonic_shift` | 諧波樣態改變（**Phase 2 啟用**） | monotonic |
| `other` | 其他 | none |

---

## 四、工作流設計

依「每日新增資料時基礎規則先套，週報時 agent 重新檢視有沒有回歸正常」的需求：

```
每日排程（無 LLM）
  ① 匯入當日 Sensor 資料 + 電流資料
  ② 指標層計算（時域統計、趨勢、ISO 分級、工況對照）
  ③ 規則層判定 → upsert findings
       · 新問題 → 建案（occurrence_count = 1）
       · 既有問題再現 → occurrence_count++、更新 current_value
       · 數值惡化 → 標記 escalated_at（含抗抖動：需連續 2 次成立）
       · 回到門檻內 → 自動結案（resolved_by = "auto"）

每週（地端 Agent）
  ① 先呼叫 get_open_findings  ← 讀既有事項與工程師回覆
  ② 呼叫 get_weekly_report_data 取本週彙總
  ③ 對照 get_vibration_thresholds 的現行標準
  ④ 產出 verdict / headline / actions / notes
  ⑤ 呼叫 send_report → 本系統排版寄送，並依 DB 分成三段

異常事件回覆（人工或 Dashboard 觸發）
  ① 呼叫 get_event_context/{finding_id}
  ② 呼叫 get_rag_search 找歷史類似案例與技術文獻
  ③ 產出回覆建議
```

**Agent 讀 `get_open_findings` 必須是第一步**（沿用 HVM 的規則），否則會把延續事項當成新問題重報，週報會變成噪音。

---

## 五、核心設計原則：LLM 不做數值判斷

所有閾值判定、分級、異常偵測、故障假設，一律在**規則層**以確定性程式算完，agent 只負責解釋、關聯歷史案例、寫成人話。理由：

1. **可稽核** — 每個結論都能回推到具體規則與數值
2. **可重現** — 週報每週產出，同樣資料須得到同樣判定
3. **避免幻覺** — LLM 對數值比較與門檻判斷不可靠，此類錯誤在設備監測場域代價高

此原則與 HVM 文件中「禁止用自己認知的一般業界門檻」一致。

---

## 六、現有程式碼沿用評估

### 保留（核心邏輯已驗證）

| 模組 | 保留理由 |
|------|---------|
| `src/scada_loader.py` 的 `diff_by_tag()` / `daily_sum_by_tag()` / `detect_data_gaps()` | 累積值差分、計數器重置、逐 tag 缺口偵測，皆為實際踩坑後的修正 |
| `src/scada_loader.py` 的 tag_mapping + `merge_asof` | 電流對齊機制，DB 化後邏輯不變 |
| `src/data_loader.py` 的 `safe_read_csv()` 多編碼 fallback、時間正規化與年份驗證 | 處理中文路徑與舊系統匯出編碼問題 |
| `src/health_model.py` 的工況分層 + Mahalanobis + λ 校準 | 相對基準健康分數，特徵集擴充即可沿用 |
| `src/baseline_detector.py` 的基準期自動偵測 | 「找最穩定時段當基準」的三層篩選邏輯 |
| `src/filters.py` 的突波過濾、開機判斷 | 資料清洗必要步驟 |
| `analyze_health_score.py` 的趨勢分析（斜率、前後半期、每日平均） | 直接成為指標層一部分（但需先處理自我相關問題） |
| `export_vibcurrent.py` 的同工況保養前後比較 | 改寫為服務層函式 |
| `src/device_parser.py` | 檔名解析，DB 化後降為匯入階段使用 |

### 汰換

| 現況 | 問題 |
|------|------|
| 掃資料夾讀 CSV 的 I/O 模式 | 改為 DataSource 介面 + adapter |
| matplotlib 產 PNG 內嵌 base64 的靜態報表 | Dashboard 需互動式；週報改模板引擎 |
| 多支平行 `analyze_*.py` 各自讀檔各自輸出 | 收斂為「指標層算一次 → 多消費端取用」 |
| 各腳本重複的字型設定、`_safe_write_*`、保養紀錄載入 | 抽為共用模組 |
| 散落的 `output/*.csv` 作為模組間傳遞媒介 | 改為 DB 表 / 服務層回傳物件 |

---

## 七、資料模型草案

```sql
-- 設備台帳
device(
  device_id PK, device_name, plant, area,
  machine_type,            -- AHU / 泵浦 / 風機 / 空壓機
  rated_power_kw,
  iso_machine_class,       -- Class I~IV（前端已選，但需複核，見待確認事項）
  iso_class_source,        -- 'frontend' / 'manual_override'
  mount_type,
  is_vfd,                  -- 目前前端一律當定頻處理
  rated_rpm,               -- 階次分析基準（AHU 樣本為 1710）
  impeller_blades,         -- 葉片通過頻率判定用
  status
)

-- 量測點（實務上一台設備 1~2 個點）
measure_point(point_id PK, device_id FK, position, sensor_id, install_date)

-- 量測資料
measurement(
  point_id FK, ts,
  -- 常用指標展開為欄位，供查詢與索引
  vel_rms, vel_rms_x/y/z, vel_oa, vel_peak,
  acc_rms, acc_rms_x/y/z, acc_oa, acc_peak, acc_crest, acc_kurt, acc_skew,
  disp_rms, disp_p2p,
  acc_mean_peak_freq, acc_weighted_mean_freq,
  -- 諧波與峰值以 JSONB 保存（Phase 1 不解析，Phase 2 直接可用）
  acc_harmonics JSONB,     -- accH01~H30 × (FREQ,V,OA) × 4 軸
  vel_harmonics JSONB,     -- velH01~H10 × (FREQ,V,OA) × 4 軸
  top_peaks JSONB,         -- acc/vel TOP5
  raw JSONB,               -- 其餘欄位原樣保留
  PRIMARY KEY(point_id, ts)
)

-- 電流/SCADA
scada_reading(tag_id, ts, value)
tag_mapping(tag_id PK, device_id FK, variable_type, unit)

-- Finding 閉環追蹤（比照 HVM）
finding(
  finding_key PK,          -- {target_type}:{target}:{issue_type}
  device_id FK, point_id FK,
  target_type, target, issue_type, family,
  title, detail, severity, peak_severity, status,
  occurrence_count, first_seen_at, last_seen_at, days_open,
  baseline_value, current_value, value_unit,
  expected_resolution_date, is_overdue, escalated_at,
  source,                  -- 'rule_engine' / 'agent'
  resolved_at, resolved_by
)

finding_note(note_id PK, finding_key FK, created_at, author, note, is_human)
```

> `raw JSONB` 與 `*_harmonics JSONB` 是刻意設計：Phase 1 不解析諧波，但完整保留，Phase 2 啟用時不需重新匯入歷史資料。

---

## 八、API 契約草案（比照 HVM 形式）

```
Base URL : http://<主機>:8000/api/agent/tools
驗證     : X-VIB-API-Key
查詢     : GET；唯一寫入型 send_report 為 POST
```

### 唯讀查詢工具

| 工具 | 用途 |
|------|------|
| `get_vibration_thresholds` | 現行 ISO 門檻與規則設定（對應 HVM 的 `get_alert_thresholds`） |
| `get_device_list` | 設備清單與最新狀態 |
| `get_device_status` | 單台設備目前狀態（含 `data_age_minutes`） |
| `get_device_trend` | 指標歷史趨勢 |
| `get_open_findings` | **未結案追蹤事項 + 工程師最後回覆（產報告前必呼叫）** |
| `get_vibration_pattern` | A 波動型形態判定（sustained / recurring / sporadic） |
| `get_degradation_trend` | B 單調型劣化推估（含 confidence） |
| `get_weekly_report_data` | 週報彙總（日報傳 `days=1`） |
| `get_measure_point_detail` | 單一量測點三軸明細（含 Phase 2 的諧波） |
| `get_rag_search` | 檢索歷史工程師回覆與技術文獻 |

### 寫入型

| 工具 | 說明 |
|------|------|
| `send_report` (POST) | 收 `verdict` / `headline` / `actions[]` / `notes` / `report_type`，本系統負責排版、分段、寄送。四道卡控比照 HVM |

`actions[]` 的閉環欄位（`target_type` / `target` / `issue_type` / `current_value` / `value_unit`）三者齊備才進追蹤帳本，規則與 HVM 一致。

---

## 九、Phase 1 規則集

| 規則 | family | 判定 |
|------|--------|------|
| `ISO_ZONE` | oscillating | velRMS 對照機械等級的 Zone A/B/C/D |
| `VEL_HIGH` | oscillating | velOA 超過設定門檻 |
| `IMPACT_RISE` | monotonic | accCREST / accKURT 相對基準顯著上升 |
| `DEGRADE_TREND` | monotonic | 指標回歸斜率持續惡化（需先處理自我相關） |
| `AXIS_SHIFT` | monotonic | 三軸能量分佈相對基準偏移 |
| `STEP_CHANGE` | monotonic | 相對基準期的突變（沿用 Mahalanobis 健康分數） |
| `SENSOR_OFFLINE` | event | 逾時無資料 |
| `DATA_QUALITY` | event | 缺漏、零值、異常值 |

**ISO 10816/20816 Zone 門檻（velRMS mm/s）**

| 等級 | A/B 界 | B/C 界 | C/D 界 |
|------|--------|--------|--------|
| Class I（< 15 kW） | 0.71 | 1.8 | 4.5 |
| Class II（15–75 kW） | 1.12 | 2.8 | 7.1 |
| Class III（大型剛性基礎） | 1.8 | 4.5 | 11.2 |
| Class IV（大型柔性基礎） | 2.8 | 7.1 | 18.0 |

> **不可只靠 ISO 分級**：AHU 樣本的 velRMS 僅 1.51 mm/s（Zone A/B），但 accCREST=16、accKURT=68.6 明顯異常。`IMPACT_RISE` 規則必須與 `ISO_ZONE` 並行。

---

## 十、待確認事項

### 阻擋 Phase 1（優先）

1. **`_V` 與 `_OA` 兩欄的定義**
   諧波每階有三欄 `FREQ / V / OA`。`V` 是峰值振幅、`OA` 是什麼？實測 H01_x：V=18.4、OA=8.9，而 18.4/accOA_x(151.36)=12.2%，與 8.9 不符；H24_x：V=39.2、OA=24.6，39.2/151.36=25.9%，與 24.6 接近。兩者關係不一致，需要前端程式的定義說明。

2. **加速度單位與 `accOA` 的合成方式**
   `accRMS`=1.21 與 `accOA`=665.4 相差 549 倍，單位顯然不同（g? m/s²? mm/s²?）。且 `accOA` 合成值既非三軸向量和（583.9）也非直接相加（771.1）。

3. **每分鐘一筆的窗口語意**（見發現二）
   PEAK 類欄位跨筆凍結、RMS 類緩慢單調變化 → 是滾動窗口還是獨立區塊？窗口長度多少？**這直接影響所有趨勢判定的統計有效性。**

4. **諧波峰值搜尋的容差設定**（見發現一）
   前端在預期諧波附近取局部最大值，容差是多少？固定 Hz 還是 % of 1X？

5. **ISO 分類的複核機制**
   你提到前端已選 ISO 分類但工程師可能分不準。建議：台帳保留 `iso_class_source` 欄位，並在規則層加一道合理性檢查（例如實測分佈長期落在某一等級的極端區間時提示複核）。需確認你要的處理方式。

6. **異常事件回覆的輸出去向**
   由 Dashboard 或 agent 觸發已確認。產出的回覆建議要寫回哪裡——只顯示在 Dashboard、寫入 `finding_note`、還是要寄出？

### 可平行進行

7. 兩個 DB 的種類、連線方式、是否允許建表
8. RAG 語料現況（工程師回覆是否有結構化歷史、格式、量體）
9. Dashboard 技術標準與帳號整合需求
10. 部署環境與 Python 版本
11. 本系統的 API Key 發放與管理方式（比照 HVM 由管理員配發？）

### 暫定假設（未收到回覆前依此設計）

- 地端 agent 自帶 LLM，本系統不呼叫 LLM，僅提供結構化上下文與檢索
- API 以 FastAPI 實作，形式比照 HVM（header 驗證、GET 查詢、單一 POST 寫入）
- `velRMS` 單位為 mm/s、`dispRMS` 為 mm
- 分析用 DB 與兩個來源 DB 分離，每日排程同步
- Phase 1 先以現有檔案為資料源跑通 agent 迴路

---

## 十一、Phase 1 工作項目

1. 專案結構重整（`core/` 指標與規則、`api/`、`ingestion/`、`reporting/`）
2. DataSource 介面與檔案 adapter（沿用現有讀取邏輯）
3. 指標層：時域統計、趨勢、ISO 分級、工況對照
4. **窗口語意釐清與獨立性處理**（依待確認事項 3 的答案）
5. 規則層 + Finding 閉環追蹤（自動建案 / 惡化偵測 / 自動結案）
6. API 層：上述工具，形式比照 HVM
7. 週報 HTML 模板與三段式渲染
8. 簡易 Dashboard
9. 與地端 agent 的整合驗證

**先做 1–6**，讓 agent 能取得上下文並產出評論，即達成 Phase 1 驗證目標；7–9 隨後補上。
