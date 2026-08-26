# 振動監測平台 Agent 化重構 — 計畫書

**分支**：`claude/agent-platform-refactor`
**狀態**：規劃稿（第 3 版）
**前提**：本案定位為「常見三軸 SENSOR 的例行監測」，深入檢測由既有的量測專家系統負責。

---

## 一、目標與範圍

### 功能需求
1. **Dashboard** — 設備健康總覽
2. **週報功能** — HTML 格式，含 agent 評論
3. **異常事件回覆** — agent 依事件上下文產出回覆建議

### 分階段策略（因應時程壓力）

| 階段 | 目標 | 內容 |
|------|------|------|
| **Phase 1** | **跑通 agent 驗證迴路** | 聚合管線、ISO 分級 + 趨勢規則、Finding 閉環追蹤、API（比照 HVM）、週報 HTML、簡易 Dashboard |
| **Phase 2** | 診斷深化與正式資料源 | 諧波故障診斷、DB 正式接入、RAG 語料建置、Dashboard 完整化 |

---

## 二、資料資產盤點

### 實際欄位規模（依 AHU 樣本核對）

實際欄位數約 **628 欄**，遠多於初版清單的 ~200 欄：

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

初版清單遺漏兩項重要資產：每個諧波的第三欄 `_OA`，以及**整組速度諧波 velH01~H10**（對低階故障診斷比加速度諧波更直接，ISO 體系的低頻診斷建立在速度域）。

### 關鍵驗證結果

| 驗證項 | 結果 |
|--------|------|
| **H01 = 1X 轉速** | ✅ 1710 rpm → 28.5 Hz，實測 `accH01FREQ` = 28.6~28.7 Hz。**諧波為階次基準，Phase 2 診斷可行** |
| 合成值計算 | `velRMS`、`accRMS` = √(x²+y²+z²)（誤差 0） |
| `accCREST` | = accPEAK / accRMS（誤差 0） |
| `dispRMS` 單位 | **mm** |
| `velRMS` 單位 | 推定 **mm/s** |
| `accOA` 合成 | ⚠️ 非向量和（583.9）亦非直接相加（771.1），標示 665.4 — 待確認 |
| `accRMS` vs `accOA` | ⚠️ 相差 549 倍，單位不同 — 待確認 |

---

## 三、取樣頻率與儲存策略（本版最大變更）

### 每秒一筆的資料量現實

| 項目 | 數量 |
|------|------|
| 單列寬度 | 628 欄 × 8 bytes ≈ **4.9 KB** |
| 單一量測點 | 86,400 列/日 ≈ **414 MB/日** |
| 20 個量測點 | **8.1 GB/日 → 2.9 TB/年** |
| 50 個量測點 | 20.2 GB/日 → 7.2 TB/年 |

**原樣寫進資料庫不可行。**

### 統計上也不應該存每秒

前端為滾動窗口計算（PEAK 類跨筆凍結證實此點）。若窗口為 60 秒而每秒輸出，**相鄰兩筆共用 98.3% 的原始資料**：

| 窗口長度 | 每日「有效獨立樣本」 |
|---------|-------------------|
| 10 秒 | 8,640 筆 |
| 30 秒 | 2,880 筆 |
| **60 秒** | **1,440 筆** |
| 300 秒 | 288 筆 |

也就是說，86,400 筆/日 之中絕大多數是重複資訊。若直接拿每秒資料做趨勢回歸，**樣本數會虛增數十倍，R² 與統計信心嚴重高估，規則層會產生大量假警報**。

因此聚合不只是儲存優化，**在統計上是必要的**。

### 兩層儲存架構

```
Tier 2（檔案，完整保真）              Tier 1（DB，精選聚合）
  原始 CSV 每秒 628 欄        ──聚合──▶   每 N 分鐘 × ~30 欄
  按設備/日期分區存放                      供 Dashboard / API / 規則層查詢
  壓縮保存                                 20 點約 2.3 GB/年（每分鐘聚合）
  Phase 2 諧波診斷按需讀取
```

| 聚合間隔 | 20 點的 DB 體積 |
|---------|----------------|
| 每分鐘 | 6.6 MB/日 → **2.3 GB/年** |
| 每 5 分鐘 | 1.3 MB/日 → 0.5 GB/年 |
| 每 10 分鐘 | 0.7 MB/日 → 0.2 GB/年 |

**建議每分鐘聚合**：2.3 GB/年 對任何資料庫都是輕量負擔，且保留足夠解析度供事件回溯。

聚合方式依欄位語意而異：
- RMS / OA 類 → 取窗口內平均（或 RMS 的 RMS）
- PEAK / P2P 類 → 取窗口內最大值
- CREST / KURT 類 → 取最大值（衝擊事件不應被平均掉）
- FREQ 類 → 取對應最大振幅時刻的值

> **這正好對應你說的「程式分析後再決定哪些同步寫進資料庫」**：CSV IO 保留為 Tier 2 的存取層，DB 只放經過篩選的分析必要欄位。Phase 1 先確定規則層實際需要哪些欄位，再定案 Tier 1 的 schema。

---

## 四、健康分數的處置：廢除 0–100 分數，保留底層機制

### 你的質疑成立

現行 0–100 健康分數的三個問題都是設計本身的缺陷，不是調參可以解決的：

| 問題 | 根本原因 |
|------|---------|
| 分數飄來飄去 | 對工況變化敏感，且基準期是自動偵測的，參考點本身不穩 |
| 無法定義 60 分是否真的不好 | λ 校準把「基準期 95th percentile」錨定在 80 分，這個錨點是任意選的，沒有物理或工程意義 |
| 跨設備不可比 | 每台設備各有自己的基準期與 λ，分數尺度不一致 |

### 替代方案（都比 0–100 分有意義）

| 取代物 | 優勢 |
|--------|------|
| **ISO Zone A/B/C/D** | 有國際標準依據、同級設備間可比、維修人員本來就懂這套語言 |
| **趨勢狀態**（劣化 / 穩定 / 改善 + 斜率） | 直接可行動 |
| **Finding 清單** | 具體、有證據數值、可追蹤 |

### 但 Mahalanobis 機制保留，改變輸出形式

單一門檻規則看不到「多個指標同時小幅偏移」的組合性變化，多變量偵測在這點上仍有獨特價值。**問題出在把它包裝成 0–100 分數對外呈現**，而不是機制本身。

改為輸出：

```
偏離基準：是
主要貢獻：accKURT  +2.8σ
          accCREST +2.1σ
          accOA    +1.4σ
          velRMS   +0.3σ（未顯著）
```

對 agent 而言，這種「哪個特徵偏離多少個標準差」的分解，比「72 分」可用得多——它能直接寫進報告，也能對應到具體的故障機理。工況分層（load binning）邏輯同樣保留，用於確保比較的是相同運轉條件。

---

## 五、感測器方向貼錯的處理策略

### 誠實的技術評估

單憑這些前處理過的特徵欄位，**無法可靠地自動判定哪個軸是軸向、哪個是徑向**，因此也無法自動校正。原因：

- 沒有相位資訊（跨軸相位比對是判定方向最可靠的方法）
- 沒有 DC 分量（否則可用重力方向識別垂直軸）
- 「軸向 1X 應該較低」這類物理特徵只在機器健康時成立，一旦有對心不良反而軸向會升高——正好是最需要判斷方向的時候最不可靠

**能做到的是這三件事**：

| 能力 | 可信度 | 做法 |
|------|--------|------|
| **偵測方向改變** | **高** | 比對同一量測點前後期的「排序後三軸能量分佈」，若排列或比例顯著跳變，代表感測器被重貼或更換。這正好對應最常見的實務情境 |
| 偵測與同型設備不一致 | 中 | 同型號/同類設備的軸能量分佈應相似，離群者標記待人工確認 |
| 自動判定軸向並校正 | 低 | **不做**。僅在 Phase 2 需要軸向資訊時，以人工標註 + 一致性檢查處理 |

### 更根本的解法：讓 Phase 1 的分析與方向無關

盤點後發現，**Phase 1 幾乎所有規則都可以只用合成值**，而合成值 √(x²+y²+z²) 本來就與座標方向無關：

| 規則 | 使用欄位 | 方向相依？ |
|------|---------|-----------|
| `ISO_ZONE` | velRMS（合成） | ❌ 無關 |
| `VEL_HIGH` | velOA（合成） | ❌ 無關 |
| `IMPACT_RISE` | accCREST / accKURT（合成） | ❌ 無關 |
| `DEGRADE_TREND` | 合成值趨勢 | ❌ 無關 |
| `STEP_CHANGE` | 合成特徵向量 | ❌ 無關 |
| `AXIS_SHIFT` | 三軸能量 | ⚠️ 改用**排序後**的能量佔比（主軸/次軸/弱軸），即可轉為方向無關 |

**結論**：Phase 1 直接迴避貼錯方向的問題——不依賴 x/y/z 標籤的正確性，只用合成值與排序後的分佈。同時加一條 `ORIENTATION_CHANGE` 規則，偵測到方向疑似變動時建立 Finding 提醒人工確認。只有 Phase 2 的進階診斷（例如以軸向 2X 判定對心不良）才需要知道哪個軸是軸向，屆時再處理。

---

## 六、與地端 Agent 的整合模式（比照 HVM）

既有 HVM Agent 平台的設計已在公司內驗證可行，本系統**直接沿用其模式**，讓 agent 平台團隊的上手成本降到最低。

| HVM 設計 | 本系統對應 |
|----------|-----------|
| `X-HVM-API-Key` header、GET 查詢 / POST 寫入、function-calling schema | 相同，改為 `X-VIB-API-Key` |
| 多支唯讀工具 + 唯一寫入型 `send_report` | 相同 |
| **Finding 閉環追蹤**（`finding_key`、`occurrence_count`、`escalated_at`、`latest_note`、狀態機） | **完整沿用**，正好對應「每日套規則、週報時 agent 檢視是否回歸正常」 |
| **三家族判準**（波動型 / 單調累積型 / 事件型） | **完整沿用** |
| 信件三段式（新發現 / 追蹤中 / 已解決）**由系統決定** | 相同 |
| `send_report` 四道卡控 | 相同 |
| 「只給數據，不給標準」→ 另有 `get_alert_thresholds` | 相同：另設 `get_vibration_thresholds` |

### 三家族判準對應振動指標

| 家族 | family | issue_type | 判準邏輯 |
|------|--------|-----------|---------|
| **A 波動型** | `oscillating` | `iso_zone_exceed`、`vel_high`、`acc_high` | 隨負載波動會回落 → **形態判準**（持續超標 vs 零星尖峰） |
| **B 單調累積型** | `monotonic` | `impact_rise`、`degradation_trend`、`axis_shift` | 機械劣化不會自己回復 → **趨勢斜率與到達門檻時間** |
| **C 事件型** | `event` | `sensor_offline`、`data_quality`、`orientation_change` | 二元狀態 |

對應 HVM 的 `get_anomaly_pattern` / `get_capacity_trend`，本系統提供 `get_vibration_pattern`（A 型形態判定）與 `get_degradation_trend`（B 型劣化推估，含 confidence）。

> **`get_degradation_trend` 必須在聚合後的獨立樣本上計算**（見 §三），否則 R² 虛高、推估天數不可信。

---

## 七、工作流設計

```
每日排程（無 LLM）
  ① 讀取當日 CSV（Tier 2）+ 電流資料
  ② 聚合為每分鐘 × 精選欄位 → 寫入 DB（Tier 1）
  ③ 指標層計算（時域統計、趨勢、ISO 分級、工況對照、多變量偏離）
  ④ 規則層判定 → upsert findings
       · 新問題 → 建案（occurrence_count = 1）
       · 既有問題再現 → occurrence_count++、更新 current_value
       · 數值惡化 → 標記 escalated_at（抗抖動：需連續 2 次成立）
       · 回到門檻內 → 自動結案（resolved_by = "auto"）

每週（地端 Agent）
  ① get_open_findings          ← 必須第一步，讀既有事項與工程師回覆
  ② get_weekly_report_data     ← 本週彙總
  ③ get_vibration_thresholds   ← 現行標準
  ④ 產出 verdict / headline / actions / notes
  ⑤ send_report → 本系統排版寄送，依 DB 分三段

異常事件回覆（Dashboard 或 Agent 觸發）
  ① get_event_context/{finding_key}
  ② get_rag_search 找歷史類似案例與技術文獻
  ③ 產出回覆建議
```

---

## 八、核心設計原則：LLM 不做數值判斷

所有閾值判定、分級、異常偵測、故障假設，一律在**規則層**以確定性程式算完，agent 只負責解釋、關聯歷史案例、寫成人話。理由：可稽核、可重現、避免幻覺。此原則與 HVM 文件中「禁止用自己認知的一般業界門檻」一致。

---

## 九、現有程式碼沿用評估

### 保留

| 模組 | 保留理由 |
|------|---------|
| `src/data_loader.py` 的 `safe_read_csv()` 多編碼 fallback、時間正規化與年份驗證 | **Tier 2 的檔案存取層核心**，處理中文路徑與編碼問題 |
| `src/scada_loader.py` 的 `diff_by_tag()` / `daily_sum_by_tag()` / `detect_data_gaps()` | 累積值差分、計數器重置、逐 tag 缺口偵測，皆為踩坑後的修正 |
| `src/scada_loader.py` 的 tag_mapping + `merge_asof` | 電流對齊機制 |
| `src/health_model.py` 的工況分層 + Mahalanobis | **保留機制，改變輸出形式**（見 §四）：不再輸出 0–100 分，改輸出各特徵標準化偏離量 |
| `src/baseline_detector.py` | 「找最穩定時段當基準」的三層篩選邏輯 |
| `src/filters.py` 的突波過濾、開機判斷 | 資料清洗必要步驟 |
| `analyze_health_score.py` 的趨勢分析 | 成為指標層一部分（改在聚合後的獨立樣本上計算） |
| `export_vibcurrent.py` 的同工況保養前後比較 | 改寫為服務層函式 |
| `src/device_parser.py` | 檔名解析 |

### 汰換

| 現況 | 問題 |
|------|------|
| matplotlib 產 PNG 內嵌 base64 的靜態報表 | Dashboard 需互動式；週報改模板引擎 |
| 多支平行 `analyze_*.py` 各自讀檔各自輸出 | 收斂為「聚合一次 → 多消費端取用」 |
| 各腳本重複的字型設定、`_safe_write_*`、保養紀錄載入 | 抽為共用模組 |
| 散落的 `output/*.csv` 作為模組間傳遞媒介 | 改為 DB 表 / 服務層回傳物件 |
| **0–100 健康分數的對外呈現** | 見 §四 |

---

## 十、資料模型草案

```sql
-- 設備台帳
device(
  device_id PK, device_name, plant, area,
  machine_type,            -- AHU / 泵浦 / 風機 / 空壓機
  rated_power_kw,
  iso_machine_class,       -- Class I~IV（前端已選，需複核）
  iso_class_source,        -- 'frontend' / 'manual_override'
  mount_type, is_vfd,
  rated_rpm,               -- 階次分析基準
  impeller_blades,         -- 葉片通過頻率判定用
  status
)

measure_point(point_id PK, device_id FK, position, sensor_id, install_date)

-- Tier 1：聚合後的精選欄位（每分鐘一筆）
measurement_agg(
  point_id FK, ts_minute,
  n_samples,               -- 該分鐘聚合的原始筆數（資料完整度）
  vel_rms, vel_oa, vel_peak,
  acc_rms, acc_oa, acc_peak, acc_crest, acc_kurt, acc_skew,
  disp_rms, disp_p2p,
  acc_mean_peak_freq, acc_weighted_mean_freq,
  axis_energy_sorted JSONB,  -- 排序後的三軸能量佔比（方向無關）
  PRIMARY KEY(point_id, ts_minute)
)

-- Tier 2 索引：原始檔案位置（不存內容）
raw_file(file_id PK, point_id FK, date, path, row_count, imported_at)

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

---

## 十一、API 契約草案（比照 HVM）

```
Base URL : http://<主機>:8000/api/agent/tools
驗證     : X-VIB-API-Key
查詢     : GET；唯一寫入型 send_report 為 POST
```

| 工具 | 用途 |
|------|------|
| `get_vibration_thresholds` | 現行 ISO 門檻與規則設定 |
| `get_device_list` | 設備清單與最新狀態 |
| `get_device_status` | 單台設備狀態（含 `data_age_minutes`） |
| `get_device_trend` | 指標歷史趨勢 |
| `get_open_findings` | **未結案事項 + 工程師最後回覆（產報告前必呼叫）** |
| `get_vibration_pattern` | A 波動型形態判定 |
| `get_degradation_trend` | B 單調型劣化推估（含 confidence） |
| `get_weekly_report_data` | 週報彙總（日報傳 `days=1`） |
| `get_measure_point_detail` | 單一量測點明細（Phase 2 含諧波） |
| `get_rag_search` | 檢索歷史工程師回覆與技術文獻 |
| `send_report` (POST) | 收 `verdict`/`headline`/`actions[]`/`notes`/`report_type`，系統負責排版分段寄送 |

---

## 十二、Phase 1 規則集

| 規則 | family | 判定 | 方向相依 |
|------|--------|------|---------|
| `ISO_ZONE` | oscillating | velRMS 對照機械等級的 Zone A/B/C/D | 否 |
| `VEL_HIGH` | oscillating | velOA 超過設定門檻 | 否 |
| `IMPACT_RISE` | monotonic | accCREST / accKURT 相對基準顯著上升 | 否 |
| `DEGRADE_TREND` | monotonic | 回歸斜率持續惡化（在聚合後樣本上計算） | 否 |
| `AXIS_SHIFT` | monotonic | **排序後**三軸能量佔比偏移 | 否 |
| `STEP_CHANGE` | monotonic | 多變量偏離基準（輸出各特徵 σ 分解） | 否 |
| `ORIENTATION_CHANGE` | event | 軸能量分佈排列跳變 → 疑似重貼/換感測器 | — |
| `SENSOR_OFFLINE` | event | 逾時無資料 | — |
| `DATA_QUALITY` | event | 缺漏、零值、`n_samples` 不足 | — |

**ISO 10816/20816 Zone 門檻（velRMS mm/s）**

| 等級 | A/B 界 | B/C 界 | C/D 界 |
|------|--------|--------|--------|
| Class I（< 15 kW） | 0.71 | 1.8 | 4.5 |
| Class II（15–75 kW） | 1.12 | 2.8 | 7.1 |
| Class III（大型剛性基礎） | 1.8 | 4.5 | 11.2 |
| Class IV（大型柔性基礎） | 2.8 | 7.1 | 18.0 |

> **不可只靠 ISO 分級**：AHU 樣本 velRMS 僅 1.51 mm/s（Zone A/B），但 accCREST=16、accKURT=68.6 明顯異常，三軸在 682.3 Hz（≈24X）都有峰值。`IMPACT_RISE` 必須與 `ISO_ZONE` 並行，否則會完全漏掉這台。

---

## 十三、待確認事項

### 阻擋 Phase 1

1. **`_V` 與 `_OA` 兩欄的定義** — H01_x：V=18.4、OA=8.9，18.4/accOA_x=12.2% 對不上 8.9；H24_x：V=39.2、OA=24.6，39.2/151.36=25.9% 卻很接近。關係不一致，需前端定義說明
2. **加速度單位與 `accOA` 合成方式** — `accRMS`=1.21 vs `accOA`=665.4 相差 549 倍；`accOA` 合成既非向量和亦非相加
3. **滾動窗口長度** — 決定聚合間隔與獨立樣本數（見 §三）
4. **諧波峰值搜尋容差** — 固定 Hz 還是 % of 1X？影響 Phase 2 診斷可靠度
5. **異常事件回覆的輸出去向** — 只顯示在 Dashboard、寫入 `finding_note`、還是要寄出？
6. **量測點總數** — 影響儲存與聚合間隔的最終定案

### 可平行進行

7. 電流 DB 的種類與連線方式（Sensor 端 Phase 1 走檔案，暫不需要）
8. RAG 語料現況
9. Dashboard 技術標準與帳號整合
10. 部署環境與 Python 版本
11. API Key 發放管理方式

### 暫定假設

- 地端 agent 自帶 LLM，本系統僅提供結構化上下文與檢索
- API 以 FastAPI 實作，形式比照 HVM
- `velRMS` 單位 mm/s、`dispRMS` 單位 mm
- **Tier 1 聚合間隔暫定每分鐘**，待窗口長度確認後定案
- Phase 1 以檔案為資料源，DB 只放聚合後的精選欄位

---

## 十四、Phase 1 工作項目

1. 專案結構重整（`core/` 指標與規則、`api/`、`ingestion/`、`reporting/`）
2. **聚合管線**：CSV（Tier 2）→ 每分鐘精選欄位（Tier 1），含欄位語意對應的聚合方式
3. 指標層：時域統計、趨勢、ISO 分級、工況對照、多變量偏離（σ 分解）
4. 規則層 + Finding 閉環追蹤（自動建案 / 惡化偵測 / 自動結案）
5. API 層：上述工具，形式比照 HVM
6. 週報 HTML 模板與三段式渲染
7. 簡易 Dashboard
8. 與地端 agent 的整合驗證

**先做 1–5**，即達成 Phase 1 驗證目標；6–8 隨後補上。
