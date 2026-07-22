# 空壓機比功率分析 — 開發計畫書

**目的**：用既有 SCADA 數據（電流／用電量／流量／運轉時數）證明「動態保養」相對「固定週期保養」的節電效益。
**現況**：本文件為規劃稿，尚未實作。預計另開一個 session 進行開發，沿用本專案既有的 `tag_mapping.csv → Other_Data/` 讀取架構。
**對應討論**：本計畫是 VFDEdgeHealthModel 專案的延伸分析，資料流不影響既有的振動健康模型（`src/health_model.py`）與匯出報告（`export_vibcurrent.py`）。

---

## 一、背景與論證邏輯

單靠「振動分數」無法直接證明節電效益——振動反映的是機械健康狀態，跟耗電量沒有直接量綱關聯，硬掛勾容易站不住腳。改採**雙軌論證**：

- **軌道 A（效益的直接證據）**：比功率（Specific Power = 流量 ÷ 用電量，即「一度電產出多少流量」），保養前後在同樣的「運轉狀態」下比較，直接算出節電百分比與金額。
- **軌道 B（機理佐證，非效益本身）**：健康分數的劣化趨勢 與 比功率的下降趨勢是否同步發生——如果同步，代表振動監測抓到的機械劣化（軸承磨損、皮帶打滑、閥片洩漏等）確實會拖累壓縮效率，支撐「振動監測值得做」的敘事，但不用來直接算節電金額。

本計畫先聚焦軌道 A（比功率計算與保養前後比較），軌道 B 待軌道 A 資料備妥後再疊圖比較。

---

## 二、資料現況

| 項目 | 內容 |
|------|------|
| 可用欄位 | 電流、用電量（累積）、流量（累積）、運轉時數（可能為累積小時數） |
| 取樣頻率 | 約每 2 分鐘一筆 |
| 資料來源 | 比照現有架構：`Other_Data/*.csv`（long format：datetime / tagname / value）+ `tag_mapping.csv`（tagname → variable_type → device_id） |
| 保養紀錄 | 沿用既有 `maintenance_log.csv`（device_id, date, event_name） |

### 需在 tag_mapping.csv 新增的列（範例，實際 tagname 待確認）

```
tagname,variable_type,device_id,unit
XXX_CURR,電流,K21_B2F_空壓,A
XXX_KWH,用電量,K21_B2F_空壓,kWh
XXX_FLOW,流量,K21_B2F_空壓,m3
XXX_RUNHR,運轉時數,K21_B2F_空壓,hr
```

> `variable_type` 的命名建議直接用「用電量」「流量」「運轉時數」，跟現有 `_detect_bin_col()` / `LOAD_COL_KEYWORDS` 的關鍵字風格一致，方便日後複用既有的欄位自動偵測邏輯。

---

## 三、核心計算邏輯

### 3.1 為什麼不能直接拿累積值算比功率

`用電量`、`流量`、`運轉時數` 都是累積式讀數（monotonic counter），必須先逐筆差分（Δ）取得區間增量，不能直接拿累積值相除。

### 3.2 為什麼不能用原始 2 分鐘級距直接算比功率

空壓機是固定轉速機型，有負載／卸載（load/unload）循環——卸載時幾乎不產氣但仍在耗電（待機功率）。如果 2 分鐘級距直接算比功率，卸載區間會讓分母（ΔkWh）不為 0 但分子（ΔFlow）趨近 0，比功率數字會失真或爆掉。

### 3.3 處理流程

1. **差分**：對 `用電量`、`流量`、`運轉時數` 三個累積欄位逐筆做 `diff()`，得到區間增量 ΔkWh / ΔFlow / Δ運轉時數。
   - 需處理歸零重置（counter reset）：diff 為負值時視為重置，該筆增量捨棄或以 0 處理，不可當成負消耗。
   - 需處理缺值/斷點：若時間間隔明顯大於 2 分鐘（例如資料缺漏），該筆 diff 不具代表性，需標記排除。

2. **運轉狀態判斷**：優先用 `Δ運轉時數 > 0` 判斷該 2 分鐘區間是否為運轉狀態；可用「電流 > 門檻值」做交叉驗證，兩者不一致時記錄下來（可能代表運轉時數欄位本身有延遲或定義不同，例如「運轉時數」是否含卸載時間，需向資料源確認）。

3. **只保留運轉狀態的增量，逐日加總**：
   ```
   當日比功率 = Σ(運轉區間的 ΔFlow) / Σ(運轉區間的 ΔkWh)
   ```
   同時保留「當日總運轉時數」作為對照欄位。

4. **保養前後比較**：沿用 `export_vibcurrent.py` 裡 `_maintenance_effect_table()` 的保養切分邏輯（`maintenance_log.csv` 最後一筆日期為分界），輸出：
   - 保養前 / 保養後 每日比功率中位數、平均
   - 保養前 / 保養後 每日運轉時數（confound 檢查：如果運轉時數變化很大，比功率的差異可能來自用氣需求變化而非保養效果，需要在報告中註明）
   - 節電百分比 = (保養前比功率 − 保養後比功率) / 保養前比功率（注意方向：比功率若定義為「流量/度」，數字是**越高越好**，保養後應該上升）

5. **金額換算（選用）**：若能取得電價（元/度），可推算「若維持原比功率，達成同樣流量產出需多花多少電費」，換算成節費金額。此參數需在新 session 向使用者確認。

---

## 四、輸出項目

- `output/specific_power/{device_id}_daily.csv`：每日 ΔFlow、ΔkWh、比功率、運轉時數
- `output/specific_power/{device_id}_report.html`：比照 `export_vibcurrent.py` 風格，包含：
  - 時序圖：比功率 + 運轉時數雙軸，標示保養日期（沿用 `_mark_maintenance()`）
  - 保養前後比較表（比照 `_maintenance_effect_table()` 樣式）
  - 統計摘要

---

## 五、實作建議（給新 session 的技術備忘）

- 新開獨立腳本 `analyze_specific_power.py`，架構比照 `export_vibcurrent.py` / `analyze_correlation.py`：讀取 `Other_Data/` + `tag_mapping.csv`，用 `src/scada_loader.py` 的 `load_other_data()` / `load_tag_mapping()` / `pivot_scada()`。
- `scada_loader.py` 目前只處理瞬時值（電流、頻率、功率、溫度），**沒有**累積值差分邏輯，需要新增一個處理函式（例如 `diff_cumulative(df_wide, cols)`），內含 counter reset 偵測與缺值排除。
- 不要改動 `src/health_model.py`、`export_vibcurrent.py`、`analyze_correlation*.py` 這幾支既有檔案的核心邏輯，本分析應該是**獨立新增**，避免影響現有振動健康模型的既有功能（比照當初 `analyze_correlation_hs.py` 獨立於 `analyze_correlation.py` 的做法）。
- 保養標記、HTML 樣式可直接複製 `export_vibcurrent.py` 的 `_mark_maintenance()`、`_stats_table_html()`、CSS 區塊，維持報告風格一致。

---

## 六、新 session 開始前，需要向使用者確認的參數

1. 四個 tag（電流／用電量／流量／運轉時數）實際的 `tagname` 是什麼、對應的 `device_id` 怎麼寫（比照 `device_parser.py` 解析出來的 devicename）。
2. `用電量`、`流量`、`運轉時數` 是否都確定是累積值？重置週期（每日歸零？永不歸零？）。
3. 流量單位（Nm³、CMM 等）。
4. 電價（元/度），若要換算金額效益。
5. 這台空壓機的 `maintenance_log.csv` 是否已有正確的保養日期紀錄。

---

## 七、已知限制（誠實揭露，避免論證被戳破）

- 目前大概率只有 1 台設備、1 次保養事件（n=1），統計上無法證明「未來每年都能延長保養間隔」，只能呈現「這個週期的條件式監測案例」。
- 比功率若受下游系統漏氣、產線用氣需求波動影響，會混雜進「保養效果」裡，需靠「運轉時數是否穩定」做初步排除，無法完全隔離。
- 若電流與運轉時數兩個訊號對「運轉狀態」的判定不一致，需要先向資料源釐清「運轉時數」的實際定義（是否含卸載時間），否則會影響比功率分母/分子的正確性。
