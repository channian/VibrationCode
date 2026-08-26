# 振動監測平台 Agent 化重構 — 計畫書

**分支**：`claude/agent-platform-refactor`
**狀態**：規劃稿（第 3 版）
**前提**：本案定位為「常見三軸 SENSOR 的例行監測」，深入檢測由既有的量測專家系統負責。

---

## 一、系統定位與範圍

### 定位：篩選與預警，不是診斷

公司已有**量測專家診斷系統**負責深入檢測。本系統使用低成本三軸 SENSOR 的例行監測資料，定位是**分流（triage）**：

> **回答「哪幾台需要注意、有多急」，而不是「是什麼故障」。**
> 判定需要進一步檢查後，由專家系統接手診斷。

這個定位決定了兩件事：**只用既有處理後資料能可靠支撐的結論**，以及**agent 不得臆測故障類型**（見 §五之二護欄）。

### 能力上限（明確界定，避免過度承諾）

| ✅ 本系統可以可靠做到 | ❌ 本系統不做 |
|---------------------|-------------|
| ISO 10816 絕對位準分級（Zone A/B/C/D） | 判定故障類型（不平衡／對心不良／軸承） |
| 趨勢劣化偵測與惡化速率 | 軸承特徵頻率（BPFO/BPFI/BSF）分析 |
| 衝擊性上升偵測（Crest / Kurtosis） | 階次分析與諧波故障指紋 |
| 頻譜重心位移（能量往高頻移動） | 根本原因判定 |
| 多變量突變偵測 | 剩餘壽命預估 |
| 感測器離線／資料品質／安裝異常 | |

### 資料使用範圍（依上述定位收斂）

| 群組 | 使用 | 理由 |
|------|------|------|
| **時域純量**（velRMS/velOA/accRMS/accOA/accCREST/accKURT/dispP2P 等） | ✅ **核心** | 已實測驗證定義與單位，物理意義明確 |
| **頻譜摘要**（accMeanPeakFreq / accWeightedMeanFreq / TOP1FREQ） | ✅ **輔助趨勢** | 是穩健的純量，可偵測「能量往高頻移動」而**不需要辨識個別諧波** |
| **諧波欄位**（accH01~H30、velH01~H10 的 FREQ/V/OA，共 480 欄） | ❌ **不使用** | `_V`/`_OA` 定義無法驗證（最佳假說仍 18.8% 變異）；FREQ 峰值搜尋容差 ±5 Hz，實測 H01 鎖到 25.9 Hz 而非 1X 的 28.5 Hz。**採信將導致誤判**，與「只做可靠的事」相牴觸 |
| **TOP2~5 峰值** | ⚠️ 選用 | 可作為「主頻改變」的輔助訊號，但不做頻率身份判讀 |

> **原 Phase 2（諧波診斷、raw 階次分析）取消。** raw 需工程師現場量測、前端無自動化，且該層次的診斷本就屬專家系統職責。既有的 480 個諧波欄位仍原樣保存於 Tier 2 檔案，未來若前端補上可驗證的定義再議。

### 分階段策略

| 階段 | 目標 | 內容 |
|------|------|------|
| **Phase 1** | **跑通 agent 驗證迴路** | 聚合管線、ISO 分級 + 趨勢規則、Finding 四階段工作流、API（比照 HVM）、週報 HTML |
| **Phase 2** | 上線完備 | 電流 DB 接入、RAG 語料建置、Dashboard 完整化、SLA 與統計 |

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

## 三、取樣頻率與儲存策略

### 規模：120 量測點 / 65 台設備（部分為備機）

| 項目 | 數量 |
|------|------|
| 單列寬度 | 628 欄 × 8 bytes ≈ **4.9 KB** |
| 單一量測點 | 86,400 列/日 ≈ **414 MB/日** |
| **120 個量測點** | **48.5 GB/日 → 17.3 TB/年** |

原始資料原樣入庫完全不可行，Tier 2 檔案端也需要壓縮與保留期政策。

### 統計上也不應該存每秒

前端為滾動窗口計算。若窗口為 60 秒而每秒輸出，**相鄰兩筆共用 98.3% 的原始資料**：

| 窗口長度 | 每日「有效獨立樣本」 |
|---------|-------------------|
| 10 秒 | 8,640 筆 |
| 30 秒 | 2,880 筆 |
| **60 秒** | **1,440 筆** |
| 300 秒 | 288 筆 |

86,400 筆/日 之中絕大多數是重複資訊。直接拿每秒資料做趨勢回歸，**樣本數會虛增數十倍，R² 與統計信心嚴重高估，規則層會噴大量假警報**。聚合不只是儲存優化，在統計上是必要的。

### 聚合間隔：建議每小時（而非每日）

振動工程師「一天一筆也足以比對」的判斷，對**趨勢比對**而言正確——軸承劣化以週/月為尺度，不需要高即時性。但那是關於**比對間隔**，不是**儲存解析度**。以下三件事需要日內解析度，而每小時的成本幾乎是零：

| 需求 | 為何需要日內解析度 |
|------|------------------|
| **備機的運轉時段隔離** | 備機一天可能只跑 2 小時。日平均把 22 小時的停機資料混進來，數字完全失真 |
| **A 波動型的形態判準** | 比照 HVM 的 `get_anomaly_pattern`，要區分「每天固定時段超標（排程負載）」與「持續超標（容量不足）」，處置方向完全不同。日資料無法做這個判斷 |
| 事件回溯 | 異常發生時要能看出是當天哪個時段 |

| 聚合間隔 | 120 點的 DB 體積 |
|---------|-----------------|
| 每分鐘 | 39.6 MB/日 → 14.1 GB/年 |
| **每小時** | **0.7 MB/日 → 0.23 GB/年** |
| 每日 | ~0 → 0.01 GB/年 |

**建議 Tier 1 存每小時聚合，另做每日 rollup 供週報使用。** 0.23 GB/年 對任何資料庫都無感，且保留了上述三項能力。

### 備機與停機時段的處理

聚合時**只納入判定為運轉中的樣本**，並記錄樣本數：

```sql
measurement_agg(
  point_id, ts_hour,
  n_samples_total,      -- 該小時原始筆數（資料完整度）
  n_samples_running,    -- 其中運轉中的筆數
  ...指標（僅以運轉中樣本計算）
)
```

`n_samples_running = 0` 代表該小時未運轉，**不產生任何異常 Finding**（沒資料不等於有故障）。運轉樣本數過少時，指標標記為低信心。

> **備機的專屬議題**：長期靜置的備機會有軸承靜置壓痕（brinelling）、潤滑油沉降與鏽蝕風險，定期試車是標準做法。建議加一條 `standby_no_runtime` 規則（備機超過 N 天未運轉 → 建議試車）。需確認貴司是否已有試車排程，若有則對齊該週期。

### 兩層儲存架構

```
Tier 2（檔案，完整保真）              Tier 1（DB，精選聚合）
  原始 CSV 每秒 628 欄        ──聚合──▶   每小時 × ~30 欄（僅運轉樣本）
  按設備/日期分區 + 壓縮                    供 Dashboard / API / 規則層查詢
  Phase 2 諧波診斷按需讀取                  120 點約 0.23 GB/年
```

聚合方式依欄位語意而異：
- RMS / OA 類 → 運轉樣本的平均
- PEAK / P2P 類 → 運轉樣本的最大值
- CREST / KURT 類 → 最大值（衝擊事件不可被平均掉）
- FREQ 類 → 對應最大振幅時刻的值

> **這正好對應你說的「程式分析後再決定哪些同步寫進資料庫」**：CSV IO 保留為 Tier 2 存取層，DB 只放分析必要欄位。Phase 1 先確定規則層實際需要哪些欄位，再定案 Tier 1 的 schema。

---

## 三之二、資料語意反推結果（已用 `data/rawdata.csv` + `data/Analytic.csv` 實測驗證）

樣本：AHU-601（FAB7 6F 空調，RPM 1710、FMF 28.5 Hz），raw 356,000 筆 @2 ksps 約 3 分鐘，Analytic 228 筆 @1 Hz。

### ✅ 已確認（可直接寫進實作）

| 項目 | 結論 | 驗證方法與誤差 |
|------|------|--------------|
| **感測器量程** | **±4g** | raw 直流分量即重力向量，實測 8212.1 counts；±4g 理論值 8192 counts/g，**誤差 0.24%** |
| **靈敏度** | **8192 counts/g** | 同上 |
| **加速度單位** | **m/s²** | raw 去直流後 RMS 換算與 Analytic 逐軸對照，**比值 0.996~1.000** |
| **耦合方式** | **AC-coupled（已去除直流）** | 含直流的 RMS 與報告值差距極大，去直流後完全吻合 |
| **合成值** | **三軸向量和 √(x²+y²+z²)** | 誤差 0 |
| **`accKURT` 定義** | **Pearson（常態 = 3）** | 實測 2.07 vs 報告 2.06 |
| **滾動窗口長度** | **10 秒（20,000 樣本）** | 掃描 0.5~120 秒，10 秒時 RMSE 0.084%、**相關係數 1.000**，三軸交叉驗證一致 |
| **輸出頻率** | 每秒 1 筆 | Analytic 每分鐘 60 筆 |
| **`velRMS` 單位** | **mm/s** | 頻域積分（10–1000 Hz）對照，比值 1.09~1.16（濾波帶略有差異，量綱確認） |
| **`dispRMS` 單位** | **mm** | 二次積分對照，量綱確認 |
| **量程餘裕** | 充足，**無飽和風險** | ±4g 滿刻度 39.23 m/s²，先前樣本峰值僅佔 49.4%。先前「疑似 ±2g 飽和」的擔憂**排除** |

### 🔑 滾動窗口 10 秒的含意

- 每秒輸出 1 筆、窗口 10 秒 → **相鄰兩筆共用 90% 的原始資料**
- 每日真正獨立的樣本數 = 86,400 / 10 = **8,640 筆**
- 趨勢分析若直接用每秒資料，樣本數會虛增 10 倍 → §三 的聚合策略成立且必要
- 直接證據：`accPEAK_z` 在同一分鐘內連續 9 筆完全相同（4.75851）後才跳變，為典型滾動最大值行為；同分鐘 60 筆中只有 25 個相異值

### ⚠️ 未能反推（不阻擋 Phase 1）

| 項目 | 狀況 |
|------|------|
| **`_V` / `_OA` 的定義** | 測試多種假說（不同 FFT 長度單格峰值、Welch PSD、各種帶寬的頻帶 RMS），**最佳者仍有 18.8% 變異**，無法確定。可能涉及未知的前置濾波或加權 |
| **`accOA` 的合成與單位** | 報告值 143.68 vs 時域 RMS 30.4 mg，比值 4.7；與諧波 `_V` 的比值也不自洽 |

### 🚨 諧波 FREQ 欄位不可靠（重要）

前端在預期諧波附近做峰值搜尋，容差約 **±5 Hz**，**足以鎖到鄰近的非諧波峰**：

| 階 | 精確 nX | 報告 FREQ | 誤差 |
|----|---------|-----------|------|
| **1** | **28.5** | **25.9** | **−9.1%** |
| 2 | 57.0 | 54.5 | −4.4% |
| 4 | 114.0 | 109.1 | −4.3% |
| 5 | 142.5 | 147.2 | +3.3% |
| 8 | 228.0 | 223.5 | −2.0% |

**H01 根本沒鎖到 1X**（報 25.9 Hz，實際 1X 為 28.5 Hz）。若 Phase 2 直接採信 `accH01FREQ_V` 為 1X 振幅，將把非諧波能量誤判為不平衡等故障。

### ✅ 建議：Phase 2 自行從 raw 計算階次分析

既然 `_V`/`_OA` 定義不明、諧波 FREQ 又會抓錯峰，**與其反推無法驗證的黑箱欄位，不如自行計算**。可行性已驗證：

| 方式 | 資料量（120 點） |
|------|-----------------|
| raw 連續保存 | 6.9 GB/日/點 → **0.8 TB/日** ✗ 不可行 |
| **每日 10 秒 raw 快照** | **99 MB/日 → 35 GB/年** ✓ 完全可行 |

每日一次 10 秒快照即可支撐完整的階次分析（1X~30X、TOP 峰值、包絡分析），且所有定義由我們掌握、可驗證、可稽核。**需確認前端是否支援排程觸發 raw 擷取。**

### 📋 Analytic 檔案已內建的 metadata 欄位

| 欄位 | 樣本值 | 用途 |
|------|--------|------|
| `RPM` / `FMF` | 1710 / 28.5 | **轉速與 1X 基頻已提供**，階次分析可直接使用 |
| `Ball` / `Vane` / `Gear` | 0 / 0 / 0 | 軸承、葉片、齒輪特徵頻率**尚未設定**。Phase 2 診斷需要，可由軸承型號推算 |
| `ISO10816_code` | 0 | ISO 等級**尚未設定** |
| `Model_HealthScore` / `Model_FailureMode` | nan / nan | 欄位已預留，供本系統回寫 |
| `Channel_X/Y/Z` | 4 / 6 / 5 | 硬體通道對應 |
| `Building` / `Floor` / `System` | FAB7 / 6F / 空調 | 設備台帳資訊已在檔案中，可直接建表 |

### 🔄 修正先前結論：感測器方向**可以**自動判定（需 raw）

先前基於處理後特徵判斷「無法可靠自動判定軸向」。**有了 raw 資料，此結論需修正**：raw 保留直流分量，而直流即重力向量，可直接定出感測器姿態。

實測：重力單位向量 x = −0.433、y = +0.901、z = +0.030 → **y 為垂直方向**（重力 90% 落在 y 軸），z 位於水平面內（重力分量僅 3%），x 有 43% 分量代表安裝面有傾斜。

因此建議：
- 每日 raw 快照同時計算重力方向，寫入量測點資料
- **方向改變偵測**：重力向量顯著轉向 → 感測器被重貼或更換，建立 Finding
- **安裝品質檢查**：可判斷是否貼歪（如本例 x 有 43% 重力分量）
- 仍**不建議自動「校正」**軸標籤，因為知道垂直方向不等於知道機器的軸向；但可提供人工確認所需的完整資訊

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
  ③ 產出回覆建議（受 §8.2 護欄約束）
```

### 異常事件的四階段簽核流程

```
  規則引擎建案
       │  status = open
       ▼
  ① 設備工程師回覆        engineer_replied
       │  （agent 提供回覆建議草稿供參考）
       ▼
  ② 工程師主管審核        supervisor_reviewed
       │
       ▼
  ③ 專家複審              expert_reviewed
       │
       ▼
  ④ 結案                  closed
```

| 狀態 | 說明 |
|------|------|
| `open` | 規則引擎建案，待設備工程師處理 |
| `engineer_replied` | 設備工程師已回覆現場確認結果與處置 |
| `supervisor_reviewed` | 工程師主管已審核 |
| `expert_reviewed` | 專家已複審（可在此判定需否轉專家系統實測） |
| `closed` | 結案 |
| `auto_resolved` | 數值回歸門檻內，系統自動結案（不需走簽核鏈） |
| `false_positive` | 判定為誤報，可於任一階段標記並結案 |

**設計要點**：

- 每一階段的回覆寫入 `finding_note`，含 `stage`（哪一關）、`author`、`role`
- **各階段的停留時間需追蹤**，供週報彙整積壓情形（例如「3 件卡在主管審核超過 7 天」）
- 若指標在簽核過程中**持續惡化**，`escalated_at` 標記並在週報拉高呈現，不因「處理中」而輕描淡寫（沿用 HVM 規則）
- `auto_resolved` 與簽核鏈並行：數值回歸正常時自動結案，但若已進入簽核流程則保留紀錄供追溯
- **這條簽核鏈本身就是 RAG 的語料來源**——每一關的回覆累積成歷史案例庫，是本系統長期價值的核心

---

## 八、核心設計原則

### 8.1 LLM 不做數值判斷

所有閾值判定、分級、異常偵測，一律在**規則層**以確定性程式算完，agent 只負責解釋、關聯歷史案例、寫成人話。理由：可稽核、可重現、避免幻覺。此原則與 HVM 文件中「禁止用自己認知的一般業界門檻」一致。

### 8.2 Agent 不得臆測故障類型（本案關鍵護欄）

依 §一 的定位，本系統的資料**不足以支撐故障類型判定**。但 LLM 的天性就是會寫出「疑似對心不良」「軸承內環缺陷」這類具體結論——這些話語出現在週報上，一旦與專家系統的實測結果不符，**整個系統的可信度就毀了**。

因此必須在 agent 的系統提示與 API 回傳內容中明確約束：

| 禁止 | 允許 |
|------|------|
| 「疑似對心不良」 | 「整體振動位準上升，衝擊性指標未同步上升」 |
| 「軸承內環缺陷」 | 「衝擊性指標（Crest/Kurtosis）持續上升，常見於軸承或潤滑劣化，本系統無法區分成因」 |
| 「建議更換軸承」 | 「建議安排專家系統複測以確認成因」 |
| 「剩餘壽命約 2 個月」 | 「以目前劣化速率，約 N 天後將觸及 Zone C 門檻（信心度：中）」 |

**輸出格式為「觀察到的現象 + 建議的下一步」，不輸出「故障類型判定」。**

實作上，API 回傳的每個 Finding 都附帶 `interpretation_limit` 欄位，明確標示該證據能支撐到什麼程度，agent 據此撰寫。RAG 檢索到的歷史案例可以引用（「去年 3 月類似樣態，工程師實際發現為聯軸器鬆脫」），但需標明為歷史案例而非本次判定。

### 8.3 方向性提示的分寸

在不指名故障的前提下，仍可提供有價值的方向性資訊：

| 觀察樣態 | 可以這樣寫 |
|---------|-----------|
| velRMS 上升、Crest/Kurtosis 平穩 | 「整體位準上升但無衝擊性增強，傾向結構或負載面因素」 |
| Crest + Kurtosis 同步上升、頻譜重心上移 | 「衝擊性與高頻能量同步增加，建議優先複測」 |
| 僅單一指標跳動、其餘平穩 | 「單一指標變動，其餘穩定，建議先確認量測與安裝狀態」 |

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

-- Tier 1：聚合後的精選欄位（每小時一筆，僅計運轉樣本）
measurement_agg(
  point_id FK, ts_hour,
  n_samples_total,         -- 該小時原始筆數（資料完整度）
  n_samples_running,       -- 其中運轉中的筆數；= 0 表示未運轉，不判異常
  vel_rms, vel_oa, vel_peak,
  acc_rms, acc_oa, acc_peak, acc_crest, acc_kurt, acc_skew,
  disp_rms, disp_p2p,
  acc_mean_peak_freq, acc_weighted_mean_freq,
  axis_energy_sorted JSONB,  -- 排序後的三軸能量佔比（方向無關）
  PRIMARY KEY(point_id, ts_hour)
)

-- 每日 rollup（供週報與長期趨勢）
measurement_daily(
  point_id FK, date,
  running_hours,           -- 當日運轉時數（備機判定用）
  ...同上指標的日聚合,
  PRIMARY KEY(point_id, date)
)

-- Tier 2 索引：原始檔案位置（不存內容）
raw_file(file_id PK, point_id FK, date, path, row_count, imported_at)

scada_reading(tag_id, ts, value)
tag_mapping(tag_id PK, device_id FK, variable_type, unit)

-- Finding 閉環追蹤（比照 HVM，擴充四階段簽核）
finding(
  finding_key PK,          -- {target_type}:{target}:{issue_type}
  device_id FK, point_id FK,
  target_type, target, issue_type, family,
  title, detail, severity, peak_severity,
  status,                  -- open / engineer_replied / supervisor_reviewed
                           -- / expert_reviewed / closed / auto_resolved / false_positive
  stage_entered_at,        -- 進入目前階段的時間（供 SLA 與積壓統計）
  occurrence_count, first_seen_at, last_seen_at, days_open,
  baseline_value, current_value, value_unit,
  interpretation_limit,    -- 該證據能支撐到什麼程度（供 agent 遵守 §8.2 護欄）
  expected_resolution_date, is_overdue, escalated_at,
  needs_expert_measurement,-- 專家複審判定是否需轉專家系統實測
  source,                  -- 'rule_engine' / 'agent'
  resolved_at, resolved_by
)

finding_note(
  note_id PK, finding_key FK, created_at,
  stage,                   -- 對應簽核階段
  author, role,            -- engineer / supervisor / expert / system / agent
  note, action_taken, root_cause,
  is_human
)
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
| `DATA_QUALITY` | event | 缺漏、零值、`n_samples_running` 不足 | — |
| `SPECTRAL_SHIFT` | monotonic | `accWeightedMeanFreq` 重心持續上移 → 能量往高頻移動 | 否 |
| `SENSOR_SATURATION` | event | accPEAK 逼近量程滿刻度 → 峰值類指標失真 | — |
| `STANDBY_NO_RUNTIME` | event | 備機超過 N 天未運轉 → 建議試車（待確認是否已有試車排程） | — |
| `ISO_CLASS_SUSPECT` | event | ISO 等級疑似誤填（見下） | — |

**ISO 10816/20816 Zone 門檻（velRMS mm/s）**

| 等級 | A/B 界 | B/C 界 | C/D 界 |
|------|--------|--------|--------|
| Class I（< 15 kW） | 0.71 | 1.8 | 4.5 |
| Class II（15–75 kW） | 1.12 | 2.8 | 7.1 |
| Class III（大型剛性基礎） | 1.8 | 4.5 | 11.2 |
| Class IV（大型柔性基礎） | 2.8 | 7.1 | 18.0 |

> **不可只靠 ISO 分級**：AHU 樣本 velRMS 僅 1.51 mm/s（Zone A/B），但 accCREST=16、accKURT=68.6 明顯異常。`IMPACT_RISE` 必須與 `ISO_ZONE` 並行，否則會完全漏掉這台。

### ISO 等級誤填的處理（`ISO_CLASS_SUSPECT`）

ISO 等級由工程師依 ISO 10816 自行分類，無法保證正確，而 Zone 判定完全建立在這個分類上。因此需要一道**合理性檢查**：

**判準**：一台健康運轉的機器，其基準期 velRMS 中位數應落在所指派等級的 **Zone A 或低 Zone B**。若基準期中位數已經超過該等級的 **B/C 界**，則兩種可能——機器本來就有問題，或等級填錯。兩者都需要人工確認。

| 情境 | 處理 |
|------|------|
| 基準期中位數 > 指派等級的 B/C 界 | 建立 `ISO_CLASS_SUSPECT` Finding，列出實測分佈與各等級門檻，請工程師複核 |
| 未填等級（`ISO10816_code = 0`） | **不套用 Zone 判定**，僅以相對基準與趨勢規則監測，並在報告中標明「未分級」 |

**設計原則**：Dashboard 與週報顯示 Zone 時，一律標註分類來源（`iso_class_source`）。分類存疑或未分級的設備，其 Zone 結論需明確標示信心度，避免誤導。

> 由於 `ISO10816_code` 目前實測為 0（尚未設定），**Phase 1 初期預期多數設備走「未分級」路徑**，以相對基準與趨勢為主。ISO 絕對分級的價值需等台帳補齊後才能發揮。

---

## 十三、待確認事項

> **§三之二 的實測已解決原先列為「最高優先」的多數項目**：加速度單位、量程、滾動窗口長度、合成方式、飽和疑慮、感測器方向偵測可行性。以下為尚未解決者。

### 已因範圍收斂而消除

原列的「前端 raw 擷取排程」「軸承型號」「`_V`/`_OA` 定義」三項，**隨諧波診斷取消而不再需要**（見 §一）。

### 阻擋 Phase 1

1. **備機清單與試車排程** — 工程師整理中；試車制度待確認，用以對齊 `STANDBY_NO_RUNTIME` 規則
2. **簽核鏈的角色與權限對應** — 設備工程師／工程師主管／專家分別是哪些人？是否需與公司帳號系統整合？
3. **各階段的 SLA 期望** — 每一關期望多久內處理完？用以設定積壓告警門檻

### 可平行進行

4. 電流 DB 的種類與連線方式（Sensor 端 Phase 1 走檔案，暫不需要）
5. RAG 語料現況（簽核鏈上線後會自行累積，初期可先只接技術文獻）
6. Dashboard 技術標準與帳號整合 — 見另份《Dashboard 需求書》
7. 部署環境與 Python 版本
8. API Key 發放管理方式

### 已確立的事實（不再是假設，見 §三之二）

- 加速度單位 **m/s²**、速度 **mm/s**、位移 **mm**
- 感測器量程 **±4g**、靈敏度 **8192 counts/g**、AC-coupled
- 前端滾動窗口 **10 秒**，每秒輸出 1 筆 → 每日獨立樣本 8,640
- 合成值為三軸向量和；`accKURT` 為 Pearson 定義（常態 = 3）
- 無飽和風險（峰值僅佔滿刻度 49%）
- raw 含直流分量 → **感測器姿態可自動判定**

### 仍為假設

- 地端 agent 自帶 LLM，本系統僅提供結構化上下文與檢索
- API 以 FastAPI 實作，形式比照 HVM
- **Tier 1 聚合間隔為每小時**（僅計運轉樣本），另做每日 rollup
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
