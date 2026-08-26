# 振動監測平台 Agent 化重構 — 計畫書

**分支**：`claude/agent-platform-refactor`
**狀態**：規劃稿，待確認假設後動工
**前提**：本案定位為「常見三軸 SENSOR 的例行監測」，深入檢測由既有的量測專家系統負責，本系統不與其重疊。

---

## 一、目標與範圍

### 功能需求
1. **Dashboard** — 設備健康總覽
2. **週報功能** — HTML 格式，含 agent 評論
3. **異常事件回覆** — agent 依事件上下文產出回覆建議

### 分階段策略（因應時程壓力）

| 階段 | 目標 | 內容 |
|------|------|------|
| **Phase 1** | **跑通 agent 驗證迴路** | 資料層抽象、時域指標 + ISO 分級 + 趨勢規則、API 契約、週報 HTML 模板、簡易 Dashboard |
| **Phase 2** | 正式資料源與診斷深化 | 雙 DB 正式接入與每日排程、諧波故障診斷層、RAG 語料建置、Dashboard 完整化 |

Phase 1 的判準是「agent 能拿到足夠的結構化素材，產出有工程價值的評論」，而不是「所有資料源都接完」。因此 Phase 1 允許以現有檔案作為資料來源，DB 接入平行進行。

---

## 二、資料資產盤點

Sensor 前處理後約 200 個欄位，可分四層：

| 層 | 欄位 | 數量 | Phase |
|----|------|------|-------|
| **T1 時域純量** | accRMS / accPEAK / accCREST / accSKEW / accKURT / velRMS / velPEAK / dispRMS / dispP2P / accOA / velOA（各含 `_x/_y/_z` + 合成值） | ~44 | **P1 主力** |
| **T2 頻譜摘要** | accMeanPeakFreq / accMeanPeakInt / accWeightedMeanFreq（各軸 + 合成） | 12 | **P1 輔助** |
| **T3 TOP5 峰值** | accTOP1~5FREQ + accTOP1~5FREQ_V（各軸） | 30 | P2 |
| **T4 諧波階次** | accH01~H30FREQ_V（各軸） | 90 | P2 |

### Phase 1 使用的核心指標

| 指標 | 用途 |
|------|------|
| `velRMS` | ISO 10816/20816 絕對分級主判定量（Zone A/B/C/D） |
| `velOA`, `accOA` | 整體能量趨勢 |
| `accRMS` | 加速度能量，對高頻劣化較敏感 |
| `accKURT`, `accCREST` | 衝擊性指標，軸承早期損傷的傳統前導指標 |
| `dispP2P` | 低轉速機械的位移判定 |
| `_x/_y/_z` 三軸 | 方向性比較（水平/垂直/軸向的相對關係本身有診斷意義） |

### Phase 2 的診斷價值（先保留接口，不實作）

T3/T4 是故障類型指紋，規則層解析後可產出具體故障假設，而非只有「異常/正常」：

| 樣態 | 對應故障 |
|------|---------|
| 1X 主導、隨轉速平方成長 | 不平衡 |
| 2X 顯著、軸向明顯 | 對心不良 |
| 次諧波（0.5X）、多階諧波 | 機械鬆動 |
| 高階諧波 + accKURT/accCREST 同步上升 | 軸承早期缺陷 |
| 葉片通過頻率（葉片數 × 1X） | 泵浦水力異常 / 氣蝕 |

> **這是本系統與「只有健康分數」最大的差異點**：有了故障假設，agent 的評論才能從「分數偏低，建議檢查」升級為「X 軸 2X 諧波佔比上升且軸向明顯，符合對心不良徵兆」，週報與事件回覆才有工程價值。Phase 1 的資料模型與 API 會預留此欄位群，Phase 2 補上時不需重構。

---

## 三、現有程式碼沿用評估

### 保留（核心邏輯已驗證，遷移進新架構）

| 模組 | 保留理由 |
|------|---------|
| `src/scada_loader.py` 的 `diff_by_tag()` / `daily_sum_by_tag()` / `detect_data_gaps()` | 累積值差分、計數器重置偵測、逐 tag 缺口偵測，都是實際踩坑後修正的邏輯 |
| `src/scada_loader.py` 的 tag_mapping + `merge_asof` 對齊 | 電流資料對齊的既有機制，DB 化後邏輯不變 |
| `src/data_loader.py` 的 `safe_read_csv()` 多編碼 fallback、時間欄位正規化與年份驗證 | 處理中文路徑與舊系統匯出的編碼問題 |
| `src/health_model.py` 的工況分層 + Mahalanobis + λ 校準 | 相對基準的健康分數邏輯，特徵集擴充即可沿用 |
| `src/baseline_detector.py` 的基準期自動偵測 | 「找最穩定時段當基準」的概念與三層篩選邏輯 |
| `src/filters.py` 的突波過濾、開機判斷 | 資料清洗必要步驟 |
| `src/device_parser.py` | 檔名解析（DB 化後降為匯入階段使用） |
| `export_vibcurrent.py` 的保養前後同工況比較邏輯 | 有價值的分析方法，改寫為服務層函式 |
| `analyze_health_score.py` 的趨勢分析（斜率、前後半期、每日平均） | 直接成為指標層的一部分 |

### 汰換

| 現況 | 問題 |
|------|------|
| 掃資料夾讀 CSV 的 I/O 模式 | 改為 DataSource 介面 + DB adapter |
| matplotlib 產 PNG 內嵌 base64 的靜態報表 | Dashboard 需要互動式；週報 HTML 改用模板引擎 + 輕量圖表 |
| 多支平行的 `analyze_*.py` 腳本各自讀檔、各自輸出 | 收斂為「指標層算一次 → 多個消費端取用」 |
| 各腳本重複的字型設定、`_safe_write_*`、保養紀錄載入 | 抽為共用模組 |
| 散落的 `output/*.csv` 作為模組間傳遞媒介 | 改為 DB 表 / 服務層回傳物件 |

### 重寫但保留概念
- 保養有效性分析（同工況分箱比較）→ 成為 API endpoint
- 比功率分析（`analyze_specific_power.py`）→ 空壓機類設備適用，列為選用模組

---

## 四、目標架構

```
┌─────────────────────────────────────────────────────────┐
│  地端 AGENT（公司既有，內含 LLM）                          │
│    呼叫本系統 API 取得結構化上下文 → 產出評論/週報/事件回覆   │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTP API
┌───────────────────────▼─────────────────────────────────┐
│  ⑥ API 層（FastAPI）                                     │
│     設備狀態 / 規則判定結果 / 週報上下文 / RAG 檢索         │
├─────────────────────────────────────────────────────────┤
│  ⑤ Dashboard（讀 ③④）                                    │
├─────────────────────────────────────────────────────────┤
│  ④ 規則/判定層（確定性，不含 LLM）                          │
│     ISO 分級 / 趨勢劣化 / 突變偵測 / 資料品質              │
│     → 產出結構化 Finding（含證據數值與判定依據）            │
├─────────────────────────────────────────────────────────┤
│  ③ 指標層                                                │
│     時域統計 / 趨勢斜率 / 健康分數 / 工況對照 / 每日聚合     │
├─────────────────────────────────────────────────────────┤
│  ② 統一資料層（分析用 DB）                                 │
│     device / measurement / current / finding / event      │
├─────────────────────────────────────────────────────────┤
│  ① Ingestion（DataSource 介面）                          │
│     Sensor DB（每日排程）│ 電流 DB（既有排程）│ 檔案（P1）  │
└─────────────────────────────────────────────────────────┘
```

### 核心設計原則：LLM 不做數值判斷

所有閾值判定、分級、異常偵測、故障假設，一律在 **④ 規則層**以確定性程式算完，agent 只負責解釋、關聯歷史案例、寫成人話。理由：

1. **可稽核** — 每個結論都能回推到具體規則與數值，工業場域必要
2. **可重現** — 週報每週產出，同樣資料須得到同樣判定
3. **避免幻覺** — LLM 對數值比較與門檻判斷不可靠，這類錯誤在設備監測場域代價高

Agent 收到的是「已判定完成的結構化事實」，不是原始數值表。

---

## 五、資料模型草案

```sql
-- 設備台帳（需人工建立，ISO 分級與診斷都依賴此表）
device(
  device_id PK, device_name, plant, area,
  machine_type,          -- 泵浦/風機/空壓機...
  rated_power_kw,        -- ISO 機械等級判定
  iso_machine_class,     -- Class I~IV
  mount_type,            -- 剛性/柔性基礎
  is_vfd,                -- 是否變頻
  rated_rpm, pole_pairs, -- P2 階次分析用
  impeller_blades,       -- P2 葉片通過頻率用
  status
)

-- 量測點（一台設備可有多個位置，如 M1 自由端 / M2 驅動端）
measure_point(point_id PK, device_id FK, position, sensor_id, install_date)

-- 量測資料（Sensor DB 每日匯入）
measurement(
  point_id FK, ts,
  vel_rms, vel_rms_x/y/z, vel_oa,
  acc_rms, acc_oa, acc_kurt, acc_crest, acc_peak,
  disp_rms, disp_p2p,
  acc_mean_peak_freq, acc_weighted_mean_freq,
  harmonics JSONB,       -- P2：accH01~H30 三軸
  top_peaks JSONB,       -- P2：TOP5 頻率與振幅
  raw JSONB,             -- 其餘欄位原樣保留，避免日後回頭補匯入
  PRIMARY KEY(point_id, ts)
)

-- 電流/SCADA（既有排程來源）
scada_reading(tag_id, ts, value)
tag_mapping(tag_id PK, device_id FK, variable_type, unit)

-- 規則層產出
finding(
  finding_id PK, device_id FK, point_id FK,
  detected_at, rule_code, severity,
  metric, value, threshold, baseline_value,
  evidence JSONB,        -- 判定依據的完整數值
  status                 -- open/acknowledged/closed
)

-- 事件與工程師回覆（RAG 語料來源）
event(event_id PK, device_id FK, finding_id FK, opened_at, closed_at, category, status)
event_reply(reply_id PK, event_id FK, replied_at, author, content, action_taken, root_cause)
```

> `raw JSONB` 是刻意設計：Phase 1 不解析 T3/T4，但完整保留原始欄位，Phase 2 要用時不需重新匯入歷史資料。

---

## 六、API 契約草案

假設地端 agent 自帶 LLM，本系統提供結構化上下文與檢索，不呼叫 LLM。

### 資料查詢
```
GET  /api/devices                          設備清單與最新狀態
GET  /api/devices/{id}/status              單台設備即時狀態摘要
GET  /api/devices/{id}/metrics?from=&to=   指標時序（供圖表）
GET  /api/devices/{id}/findings?status=    規則層判定結果
```

### Agent 專用（結構化上下文包）
```
GET  /api/agent/weekly-context?week=2026-W35
     → 該週全廠結構化摘要：各設備狀態、新增/持續 findings、
       趨勢變化、資料品質、與前週對比、相關歷史案例
     → agent 據此產出評論

GET  /api/agent/event-context/{event_id}
     → 單一事件上下文：觸發規則與證據、該設備歷史軌跡、
       同型設備類似案例、過去工程師回覆
     → agent 據此產出回覆建議

GET  /api/agent/rag/search?q=&device_type=&top_k=
     → 檢索歷史事件回覆與技術文獻
```

### 報告產出
```
POST /api/reports/weekly
     body: { week, agent_commentary: {...} }
     → 套用 HTML 模板，回傳報告 URL
GET  /api/reports/weekly/{week}
```

> **待確認**：`agent_commentary` 的欄位結構需與地端 agent 對齊。你提供既有系統的呼叫範例後即可定案。

---

## 七、規則層 Phase 1 規則集

| 規則 | 判定 | 依據 |
|------|------|------|
| `ISO_ZONE` | velRMS 對照機械等級的 Zone A/B/C/D | ISO 10816/20816 |
| `TREND_DEGRADE` | 指標斜率持續惡化超過穩定帶 | 現有趨勢分析邏輯 |
| `STEP_CHANGE` | 相對基準期的突變 | 現有 Mahalanobis 健康分數 |
| `IMPACT_RISE` | accKURT / accCREST 同步上升 | 軸承早期劣化傳統指標 |
| `AXIS_IMBALANCE` | 三軸能量分佈異常偏移 | 方向性變化 |
| `DATA_QUALITY` | 斷線、0 值、缺漏時段 | 現有 gap 偵測邏輯 |

每條規則產出的 Finding 都帶完整證據數值，agent 據此撰寫說明。

**ISO 10816 Zone 門檻（velRMS mm/s）**

| 等級 | A/B 界 | B/C 界 | C/D 界 |
|------|--------|--------|--------|
| Class I（< 15 kW） | 0.71 | 1.8 | 4.5 |
| Class II（15–75 kW） | 1.12 | 2.8 | 7.1 |
| Class III（大型剛性基礎） | 1.8 | 4.5 | 11.2 |
| Class IV（大型柔性基礎） | 2.8 | 7.1 | 18.0 |

> 需要設備台帳提供 `rated_power_kw` 與基礎型式才能套用，這是 Phase 1 就需要的資料。

---

## 八、待確認事項

### 阻擋 Phase 1 設計（優先）

1. **地端 Agent 呼叫範例** — 你提到可提供。需要看：認證方式、request/response 格式、是否有既有的上下文結構慣例。這決定 API 契約定案。
2. **單位確認** — `velRMS` 是否為 mm/s？`accRMS`/`accOA` 是否為 g？`dispP2P` 是否為 µm？ISO 分級與門檻設定直接依賴此項。
3. **資料時間粒度** — 一列資料代表多長的量測窗口？多久一筆？
4. **設備台帳** — 現有幾台設備、幾個量測點？是否已有額定功率/機械等級/基礎型式的資料？無此資料則 ISO 分級無法套用，需先以相對基準為主。
5. **異常事件回覆的工作流** — 誰觸發（人工/系統自動）？輸入是什麼？agent 產出的回覆要寫回哪裡？

### 可平行進行（不阻擋 Phase 1）

6. **兩個 DB 的種類與存取方式** — 資料庫類型、是否可直連、是否允許建表或僅能讀取。
7. **RAG 語料現況** — 工程師回覆是否有結構化的歷史紀錄？現存於何處？格式為何？若無歷史語料，Phase 1 的 RAG 可先只接學術/技術文獻。
8. **Dashboard 技術限制** — 公司是否有既有 BI 工具或前端技術標準？是否需與內部帳號系統整合？
9. **部署環境** — 本系統跑在哪（地端伺服器？容器？）、Python 版本限制。

### 假設（未收到回覆前依此設計，請直接修正）

- 地端 agent 自帶 LLM，本系統不呼叫 LLM，僅提供結構化上下文與檢索
- API 以 FastAPI 實作，回傳 JSON
- 分析用 DB 與兩個來源 DB 分離，透過每日排程同步
- Phase 1 可先以現有檔案為資料源跑通 agent 迴路，不等待 DB 權限
- 週報為每週固定產出，涵蓋全廠設備

---

## 九、Phase 1 工作項目

1. 專案結構重整（`core/` 指標與規則、`api/`、`ingestion/`、`reporting/`）
2. DataSource 介面與檔案 adapter（沿用現有讀取邏輯）
3. 指標層：時域統計、趨勢、健康分數、工況對照（遷移現有邏輯）
4. 規則層：Phase 1 規則集，產出結構化 Finding
5. API 層：上述 endpoints
6. 週報 HTML 模板與渲染
7. 簡易 Dashboard
8. 與地端 agent 的整合驗證

**先做 1–5**，讓 agent 能取得上下文並產出評論，即達成 Phase 1 的驗證目標；6–8 隨後補上。
