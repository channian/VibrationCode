# 振動監測平台 — Agent 工具 API

> 給地端 Agent 平台團隊的整合文件。格式比照公司既有 HVM Agent 平台的技能定義文件；
> 整合模式（Header 驗證、GET 查詢 / POST 寫入、四道卡控）刻意與 HVM 一致，
> 上手成本應與串接 HVM 相同。

- **程式碼位置**：`vibcore/api/`（`main.py` 路由、`service.py` 業務邏輯、`schemas.py` 請求驗證）
- **對應計畫書**：`PLAN_agent_platform_refactor.md` §六、§八、§十一
- **Base URL**：`http://<主機>:8000/api/agent/tools`
- **本機啟動**：`uvicorn vibcore.api.main:app --host 0.0.0.0 --port 8000`

---

## 1. 驗證與全域慣例

### 1.1 Header 驗證

每個請求帶入：

```
X-VIB-API-Key: <API Key>
```

| 情況 | HTTP 狀態 |
|---|---|
| Key 正確 | 依端點正常處理 |
| Key 缺漏或錯誤 | **401** |
| 伺服器端環境變數 `VIB_AGENT_API_KEY` 未設定（部署疏漏） | **503** |

401 與 503 刻意分開：401 代表「你的 Key 打錯了」，503 代表「這台服務本身沒設定好」，
避免 Agent 平台把後者誤判成前者、反覆重試同一把（其實整組都沒配置的）Key。

### 1.2 方法與路徑

- 所有查詢工具一律 **GET**，參數走 query string。
- 唯一寫入型工具 `send_report` 為 **POST**，參數走 JSON body。
- 所有路徑前綴 `/api/agent/tools/`，例如 `GET /api/agent/tools/get_device_status`。

### 1.3 「查無資料」不是 HTTP 錯誤

單筆查詢型端點（`get_device_status`、`get_device_trend`、`get_event_context`）在查無此
設備／量測點／finding 時，回傳 **200** + `{"error": "..."}`，而不是 404。

理由：404 留給「這個 URL 本身不存在」；「有這個資源類型但查無這一筆」是完全不同的
語意，用回應內容表達即可，Agent 端不需要為兩種情況分別寫例外處理。

清單型端點（`get_device_list`、`get_open_findings`）篩選條件不匹配時，回傳 200 +
空陣列，**不是**錯誤——這是正常的「目前沒有符合條件的項目」。

```json
// get_device_status?device_id=NOPE-999
{"error": "設備不存在：NOPE-999"}
```

### 1.4 時間與數值格式

- 所有時間戳一律 ISO 8601、UTC 時區（`2026-08-26T16:00:22+00:00`）。
- 數值缺值一律回傳 JSON `null`，不會是字串 `"NaN"` 或空字串。
- `days_open` / `days_in_stage` 等整數天數已由 DB 端算好，不需要 Agent 自行用時間戳相減。

### 1.5 時間窗一律用 `days`，不要自己算日期

所有涉及時間範圍的工具都以相對天數 `days` 指定窗口，**不接受明確的起訖日期**
（`send_report` 的 `end_date` 是唯一例外，且僅用於補產歷史報告）。

| 用途 | 傳法 |
|---|---|
| 週報 | `days=7` |
| 日報 | `days=1` |
| 單一指標的長期趨勢 | `days=30` 或視需要調整 |

**兩件重要的事**：

1. **不要自行計算日期。** 要 LLM 算出「上週一是幾號」是個真實的失效點——
   算錯不會報錯，只會安靜產出一份涵蓋錯誤區間的報告。把日期運算留給系統，
   你只要傳一個數字。

2. **窗口以日曆日切齊**，不是「現在往前推 N×24 小時」。因此同一批呼叫中，
   `get_weekly_report_data(days=7)` 與 `send_report(days=7)` 涵蓋的期間
   **完全相同**，不會因為兩次呼叫相隔幾分鐘而取到不同資料。這對日報
   （`days=1`）跨午夜時尤其重要。

---

## 2. 護欄：Agent 的用語限制（務必先讀）

本系統定位為**篩選與預警**，不是**故障診斷**（計畫書 §一、§8.2）。所有時域統計、
ISO 分級、趨勢回歸都已在規則層用確定性程式算完；Agent 的工作是**解釋、關聯、寫成
人話**，不是重新判斷數值背後的機械故障成因。

### 2.1 禁止與允許的用語

| 禁止（故障類型判定） | 允許（現象 + 下一步） |
|---|---|
| 「疑似對心不良」 | 「整體振動位準上升，衝擊性指標未同步上升」 |
| 「軸承內環缺陷」 | 「衝擊性指標（Crest/Kurtosis）持續上升，常見於軸承或潤滑劣化，本系統無法區分成因」 |
| 「建議更換軸承」 | 「建議安排專家系統複測以確認成因」 |
| 「剩餘壽命約 2 個月」 | 「以目前劣化速率，約 N 天後將觸及 Zone C 門檻（信心度：中）」 |

**輸出格式固定為「觀察到的現象 + 建議的下一步」，絕不輸出「故障類型判定」。**

### 2.2 護欄在 API 回應中的落地

不是只寫在系統提示裡，每一筆證據都在回應本體中帶著邊界說明：

- 每個 **finding**（`get_open_findings`、`get_weekly_report_data` 的 escalated 清單、
  `get_event_context`）都帶 `interpretation_limit` 欄位，**保證非空**——規則層漏填時，
  API 層會補上保底文字並記警告 log，Agent 不會拿到「沒有解讀邊界」的證據。
- `get_open_findings`、`get_weekly_report_data`、`get_event_context` 額外帶
  `agent_guidance` 欄位，重申本節的用語限制。
- `get_device_trend` 的 `interpretation_limit` 會明確標示 `confidence` 為 `low` 時
  不可引用斜率或推估天數作為結論。

### 2.3 歷史案例的引用方式

RAG 檢索到的歷史工程師回覆可以引用（例如「去年 3 月類似樣態，工程師實際發現為
聯軸器鬆脫」），但**必須標明為歷史案例**，不可寫成本次判定。

---

## 3. 工具總覽

| 工具 | 方法 | 用途 |
|---|---|---|
| [`get_vibration_thresholds`](#get_vibration_thresholds) | GET | 現行 ISO 門檻與規則設定 |
| [`get_device_list`](#get_device_list) | GET | 設備清單與最新狀態 |
| [`get_device_status`](#get_device_status) | GET | 單台設備目前狀態（含 `data_age_minutes`） |
| [`get_device_trend`](#get_device_trend) | GET | 單一指標的歷史趨勢 |
| [`get_open_findings`](#get_open_findings) | GET | 未結案事項 + 工程師最後回覆（**產報告前必呼叫**） |
| [`get_weekly_report_data`](#get_weekly_report_data) | GET | 週報彙總（日報傳 `days=1`） |
| [`get_event_context`](#get_event_context) | GET | 單一 finding 的完整上下文 |
| [`send_report`](#send_report) | POST | 送出報告（系統負責排版與寄送） |

> **計畫書 §十一 提到但本階段未實作**：`get_vibration_pattern`（A 波動型形態判定）、
> `get_degradation_trend`（獨立於 `get_device_trend` 的 B 型劣化推估）、
> `get_measure_point_detail`、`get_rag_search`。這四支待後續階段補上；`get_device_trend`
> 目前已涵蓋基本的趨勢回歸（含 `confidence`），可作為過渡。

---

## 4. 建議工作流程

```
每週 / 每日（地端 Agent）
  ① get_open_findings          ← 必須第一步，讀既有事項與工程師回覆
  ② get_weekly_report_data     ← 本期彙總（days=7 週報 / days=1 日報）
  ③ get_vibration_thresholds   ← 需要時查現行標準
  ④ 依 §2 的用語限制，產出 verdict / headline / actions / notes
  ⑤ send_report                ← 本系統排版寄送

異常事件回覆建議
  ① get_event_context/{finding_key}
  ② （未來）get_rag_search 找歷史類似案例
  ③ 依 §2 產出回覆建議草稿
```

---

## 5. 工具詳述

### `get_vibration_thresholds`

現行 ISO 10816 門檻與規則設定；對應 HVM 的 `get_alert_thresholds`。**只有查到的
門檻才算數**——找不到的規則不代表無限制，代表尚未啟用或設定，Agent 不應自行假設
一個業界通用門檻頂替。

**參數**：無。

**回傳範例**（節錄）：

```json
{
  "iso_thresholds": {
    "II": {
      "machine_class": "II",
      "label": "Class II（15–75 kW）",
      "ab_boundary": 1.12,
      "bc_boundary": 2.8,
      "cd_boundary": 7.1,
      "updated_at": "2026-08-26T16:00:22+00:00"
    }
  },
  "rules": {
    "IMPACT_RISE": {
      "rule_code": "IMPACT_RISE",
      "rule_name": "衝擊性指標上升",
      "family": "monotonic",
      "issue_type": "impact_rise",
      "severity": "warn",
      "params": {"crest_sigma": 2.5, "kurt_sigma": 2.5, "require_both": false},
      "is_active": true,
      "description": "accCREST / accKURT 相對基準顯著上升，常見於軸承或潤滑劣化（不判定成因）"
    }
  },
  "sla_days": {"open": 5, "engineer_replied": 5, "supervisor_reviewed": 5},
  "note": "門檻與規則設定僅在此處查得算數；找不到的規則不代表無限制……"
}
```

**給 Agent 的提示**：`rules[*].description` 本身已用「不判定成因」的措辭撰寫，
撰寫報告時可直接沿用這個口吻。

---

### `get_device_list`

設備清單與最新狀態，支援篩選。

**參數**（皆選填）：

| 參數 | 型別 | 說明 |
|---|---|---|
| `building` | string | 廠區 |
| `floor` | string | 樓層 |
| `system` | string | 系統別（如「空調」） |
| `severity` | `err` \| `warn` \| `ok` | `err`＝至少一件 err 未結案；`warn`＝無 err 但有 warn；`ok`＝目前無未結案事項 |

**回傳範例**：

```json
{
  "devices": [
    {
      "device_id": "AHU-602",
      "device_name": "6F 空調箱 602",
      "building": "FAB7", "floor": "6F", "system_name": "空調",
      "is_standby": false,
      "iso_machine_class": null, "iso_class_source": "unset",
      "n_points": 1, "n_err": 1, "n_warn": 0, "n_escalated": 1,
      "last_finding_at": "2026-08-26T16:00:22+00:00",
      "status": "err"
    }
  ],
  "count": 3
}
```

**給 Agent 的提示**：`iso_class_source: "unset"` 代表這台設備尚未分級，`get_device_status`
與 `get_device_trend` 對它不會給出 ISO Zone；報告中應寫「未分級，以相對基準與趨勢監測」，
不可假設一個等級。

---

### `get_device_status`

單台設備目前狀態；**必須含 `data_age_minutes`**，並帶涵蓋率與斷線狀況。

**參數**：

| 參數 | 型別 | 必填 |
|---|---|---|
| `device_id` | string | 是 |

**回傳範例**：

```json
{
  "device_id": "AHU-601",
  "device_name": "6F 空調箱 601",
  "building": "FAB7", "floor": "6F", "system_name": "空調",
  "is_standby": false,
  "iso_machine_class": "II", "iso_class_source": "frontend",
  "data_age_minutes": 61.2,
  "points": [
    {
      "point_id": 1, "position": "M1",
      "data_age_minutes": 61.2,
      "coverage": {
        "total_hours": 167, "ok_hours": 167, "partial_hours": 0,
        "no_data_hours": 0, "not_running_hours": 0,
        "analyzable_ratio": 1.0,
        "period_start": "2026-08-19T17:00:00+00:00",
        "period_end": "2026-08-26T15:00:00+00:00"
      },
      "iso": {
        "applicable": true, "machine_class": "II", "class_source": "frontend",
        "zone": "B", "vel_rms": 1.468756,
        "thresholds": {"ab": 1.12, "bc": 2.8, "cd": 7.1},
        "is_class_suspect": false, "suspect_reason": "", "note": ""
      },
      "current_metrics": {
        "as_of": "2026-08-26T15:00:00+00:00",
        "values": {
          "vel_rms": 1.468756, "vel_oa": 1.615632, "acc_rms": 0.734378,
          "acc_crest": 5.876436, "acc_kurt": 4.45832,
          "acc_weighted_mean_freq": 30.959
        },
        "note": ""
      }
    }
  ],
  "open_finding_count": {"err": 0, "warn": 1},
  "interpretation_limit": "本回應為設備目前狀態掃描（含最近一筆可信讀數與 ISO Zone），不構成趨勢或劣化速率結論；歷史趨勢請另呼叫 get_device_trend。"
}
```

**欄位重點**：

- `data_age_minutes`（設備層級）＝該設備所有量測點中「最新一次真的收到資料」的最短
  距今分鐘數；每個量測點在 `points[].data_age_minutes` 也各自提供一份。**這個數字看
  的是「最後一次真的有資料」的小時，不是聚合管線補上的斷線佔位列**——感測器離線
  很久時，這個數字會如實變大，不會被佔位資料掩蓋。
- `points[].coverage`：近 7 天的資料狀態統計。`no_data_hours` 高代表斷線；
  `analyzable_ratio` 低於 0.5 時，該點的 `current_metrics`／`iso` 結論信心度不足。
- `points[].iso.applicable = false` 代表未分級，`zone` 固定為 `null`，不可自行推斷等級。
- `points[].current_metrics.values` 為 None（且 `note` 有說明）時，代表近 7 天無可信
  資料，不可捏造數值。

**給 Agent 的提示**：報告中提到「目前狀態」時引用本工具；提到「這陣子的走勢」時
改呼叫 `get_device_trend`，兩者不要混用同一段落。

---

### `get_device_trend`

單一指標的歷史趨勢（線性回歸），支援 `days` 參數。

**參數**：

| 參數 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `device_id` | string | 是 | |
| `metric` | string | 是 | `vibcore.config.AGG_SPEC` 的鍵名，如 `vel_rms`、`acc_crest`、`acc_kurt`、`acc_weighted_mean_freq` 等 |
| `position` | string | 否 | 量測點位置（如 `M1`）；省略則取該設備第一個量測點 |
| `days` | int | 否 | 觀察期天數，預設 30，範圍 1–365 |

**回傳範例**（`series` 節錄至 3 筆）：

```json
{
  "device_id": "AHU-601", "point_id": 1, "position": "M1",
  "metric": "acc_crest", "days": 14,
  "trend": {
    "slope_per_day": 0.0482, "slope_per_month": 1.445, "slope_pct_per_month": 36.13,
    "r2": 0.935, "n_points": 335, "span_days": 13.9,
    "direction": "up", "confidence": "low",
    "note": "信心度低（觀察期太短：僅 13.9 天，低於建議下限 14 天）"
  },
  "coverage": {
    "total_hours": 335, "ok_hours": 335, "partial_hours": 0,
    "no_data_hours": 0, "not_running_hours": 0, "analyzable_ratio": 1.0,
    "period_start": "2026-08-12T17:00:00+00:00", "period_end": "2026-08-26T15:00:00+00:00"
  },
  "series": [
    {"ts_hour": "2026-08-12T17:00:00+00:00", "value": 5.286491, "data_status": "ok"},
    {"ts_hour": "2026-08-12T18:00:00+00:00", "value": 5.170696, "data_status": "ok"}
  ],
  "interpretation_limit": "此為線性回歸估計出的變化速率，反映觀測到的數值走勢，不代表故障成因，亦非剩餘壽命預估；confidence 為 low 時不可引用斜率或推估天數作為結論。（信心度低（觀察期太短：僅 13.9 天，低於建議下限 14 天））"
}
```

**`trend.confidence` 的使用規則（硬性）**：

| confidence | Agent 可以怎麼寫 |
|---|---|
| `low` | **不可**引用 `slope_*`／`r2`／推估天數作為結論，只能說「目前資料尚不足以判斷趨勢」 |
| `medium` | 可陳述方向（上升／下降／持平），避免給出精確推估天數 |
| `high` | 可陳述方向與速率，仍不可換算成「故障」「剩餘壽命」等診斷語彙 |

未知 `metric` 或設備／量測點不存在時，回傳 `{"error": "..."}"`（見 §1.3）。

---

### `get_open_findings`

未結案事項 + 工程師最後回覆。**產報告前必呼叫**——只有先看過既有事項與最新回覆，
才知道哪些是「新發現」、哪些是「追蹤中」。SLA 逾期判定固定使用 DB 的 `v_open_finding`
檢視，本工具與 Dashboard 共用同一份定義，不會出現兩邊算出不同逾期天數的情況。

**參數**（皆選填）：

| 參數 | 型別 | 說明 |
|---|---|---|
| `building` / `floor` / `system` | string | 範圍篩選 |
| `only_escalated` | bool | 只回「處理中但持續惡化」（`escalated_at` 非空）的事項 |
| `only_sla_breached` | bool | 只回已逾 SLA 的事項 |

**回傳範例**（`findings[0]`）：

```json
{
  "findings": [
    {
      "finding_key": "point:AHU-601_M1:impact_rise",
      "device_id": "AHU-601", "point_id": 1, "position": "M1",
      "device_name": "6F 空調箱 601", "building": "FAB7", "floor": "6F", "system_name": "空調",
      "issue_type": "impact_rise", "family": "monotonic", "rule_code": "IMPACT_RISE",
      "title": "AHU-601 M1 衝擊性指標上升",
      "detail": "accCREST 相對基準 +2.6σ，accKURT 相對基準 +2.1σ",
      "severity": "warn", "peak_severity": "warn", "status": "open",
      "occurrence_count": 1,
      "first_seen_at": "2026-08-26T16:00:22+00:00", "last_seen_at": "2026-08-26T16:00:22+00:00",
      "baseline_value": 4.0, "current_value": 5.8, "value_unit": "",
      "evidence": {"acc_crest_sigma": 2.6, "acc_kurt_sigma": 2.1},
      "interpretation_limit": "衝擊性指標（Crest/Kurtosis）持續上升，常見於軸承或潤滑劣化，本系統無法區分成因，建議安排專家系統複測以確認成因。",
      "escalated_at": null, "needs_expert_measurement": false,
      "days_open": 0, "days_in_stage": 0, "sla_days": 5, "is_sla_breached": false,
      "latest_note": null
    }
  ],
  "count": 3,
  "agent_guidance": "本系統定位為篩選與預警，不做故障診斷……（見 §2）"
}
```

**給 Agent 的提示**：

- `latest_note` 為 `null` 代表工程師尚未回覆過；非 `null` 時是
  `{"author", "role", "note", "created_at"}`，寫報告時應摘要「工程師最新回覆」而非
  重複規則引擎的判定。
- `is_sla_breached = true` 時，週報應明確標示逾期，不因「已在簽核流程中」而輕描淡寫
  （見計畫書 §七）。
- 逐筆檢查 `interpretation_limit` 並據此措辭；不要對整批 findings 套用同一句結論。

---

### `get_weekly_report_data`

週報彙總；`days=1` 供日報使用。含資料涵蓋率與斷線狀況。

**參數**（皆選填）：

| 參數 | 型別 | 說明 |
|---|---|---|
| `days` | int | 期間長度，預設 7，範圍 1–90；日報傳 1 |
| `building` / `floor` / `system` | string | 範圍篩選 |

**回傳範例**（節錄）：

```json
{
  "period": {"start": "2026-08-19T16:01:14+00:00", "end": "2026-08-26T16:01:14+00:00", "days": 7},
  "findings_summary": {
    "currently_open": {"err": 1, "warn": 2, "total": 3},
    "new_this_period": [
      {"finding_key": "point:AHU-601_M1:impact_rise", "device_id": "AHU-601",
       "issue_type": "impact_rise", "title": "AHU-601 M1 衝擊性指標上升",
       "severity": "warn", "family": "monotonic", "first_seen_at": "2026-08-26T16:00:22+00:00"}
    ],
    "new_count": 4,
    "resolved_this_period": [
      {"finding_key": "point:AHU-601_M1:vel_high", "device_id": "AHU-601",
       "issue_type": "vel_high", "severity": "warn",
       "resolved_at": "2026-08-26T16:00:22+00:00", "resolved_by": "auto"}
    ],
    "resolved_count": 1,
    "tracking_count": 0,
    "escalated": ["...同 get_open_findings 的 finding 結構，含 interpretation_limit..."],
    "escalated_count": 1
  },
  "coverage": {
    "total_points": 3,
    "points_with_insufficient_data": [
      {"device_id": "AHU-602", "point_id": 2, "position": "M1", "analyzable_ratio": 0.71, "is_sufficient": true}
    ],
    "org_analyzable_ratio": 0.83,
    "note": "analyzable_ratio 低於 50% 的量測點，其期間內的趨勢/位準結論信心度不足，週報應標示信心度或略過結論。"
  },
  "interpretation_limit": "本系統定位為篩選與預警，不做故障診斷……（見 §2）"
}
```

**三段式對應**（計畫書 §六）：`new_this_period`＝新發現、目前仍開單但
`first_seen_at` 早於本期＝追蹤中（`tracking_count`）、`resolved_this_period`＝已解決。
`send_report` 送出的報告會用同一份計算結果落庫到 `weekly_report.new_count` /
`tracking_count` / `resolved_count`，兩處數字保證一致。

**給 Agent 的提示**：`coverage.points_with_insufficient_data` 非空時，週報開頭應先
交代「以下設備本期資料不足，結論僅供參考」，再進入細節，不要把低涵蓋率的數字
當成正常讀數陳述。

---

### `get_event_context`

單一 finding 的完整上下文（簽核回覆歷史、狀態轉換歷程、資料品質），供產出回覆建議。

**參數**：

| 參數 | 型別 | 必填 |
|---|---|---|
| `finding_key` | string | 是（格式 `{target_type}:{target}:{issue_type}`，如 `point:AHU-601_M1:impact_rise`） |

**回傳範例**：

```json
{
  "finding": {
    "finding_key": "point:AHU-601_M1:impact_rise",
    "device_id": "AHU-601", "point_id": 1, "position": "M1",
    "device_name": "6F 空調箱 601", "building": "FAB7", "floor": "6F", "system_name": "空調",
    "issue_type": "impact_rise", "family": "monotonic", "rule_code": "IMPACT_RISE",
    "title": "AHU-601 M1 衝擊性指標上升",
    "detail": "accCREST 相對基準 +2.6σ，accKURT 相對基準 +2.1σ",
    "severity": "warn", "status": "open", "occurrence_count": 1,
    "baseline_value": 4.0, "current_value": 5.8,
    "evidence": {"acc_crest_sigma": 2.6, "acc_kurt_sigma": 2.1},
    "interpretation_limit": "衝擊性指標（Crest/Kurtosis）持續上升，常見於軸承或潤滑劣化，本系統無法區分成因，建議安排專家系統複測以確認成因。",
    "days_open": 0, "days_in_stage": 0
  },
  "notes": [],
  "status_history": [
    {"from_status": null, "to_status": "open", "changed_at": "2026-08-26T16:00:22+00:00",
     "note": "規則引擎建案", "changed_by": null, "duration_days": null}
  ],
  "data_quality": {
    "total_hours": 719, "ok_hours": 719, "analyzable_ratio": 1.0,
    "period_start": "2026-07-27T17:00:00+00:00", "period_end": "2026-08-26T15:00:00+00:00"
  },
  "interpretation_limit": "衝擊性指標（Crest/Kurtosis）持續上升，常見於軸承或潤滑劣化，本系統無法區分成因，建議安排專家系統複測以確認成因。",
  "agent_guidance": "本系統定位為篩選與預警，不做故障診斷……（見 §2）"
}
```

**欄位重點**：

- `notes[]`：各簽核階段的完整回覆歷史（含 `stage`、`author`、`role`、`is_human`、
  `root_cause`），由舊到新。`root_cause` 是工程師現場確認的實際原因，若非空，
  回覆建議應優先參考它而不是重新臆測。
- `status_history[]`：簽核狀態轉換歷程，`duration_days` 為停留在前一階段的天數
  （單位換算自 DB 的 `INTERVAL`），可用於「已卡在某階段 N 天」的措辭。
- `data_quality`：該量測點近 30 天的涵蓋率，用於判斷回覆建議該不該提「建議先確認
  資料完整性」。

**給 Agent 的提示**：本工具是「產出回覆建議」的主要輸入，`agent_guidance` 在此格外
重要——回覆建議是最容易被誤讀為正式診斷結論的輸出場景。

---

### `send_report`

收 `verdict` / `headline` / `actions[]` / `notes`，本系統負責排版與（未來）寄送。
**SMTP 尚未設定**：目前僅落庫到 `weekly_report` 表，`delivery.sent` 固定為 `false`，
寄送介面已預留。

**請求 Body**：

| 欄位 | 型別 | 必填 | 限制 |
|---|---|---|---|
| `report_type` | `"daily"` \| `"weekly"` | 否（預設 `weekly`） | |
| `days` | int | 否（預設 7） | 相對窗口天數，範圍 1–365；**日報傳 1**。窗口為 `[end_date - days + 1, end_date]`，以日曆日切齊 |
| `end_date` | date（`YYYY-MM-DD`） | 否（預設今日） | 僅在補產歷史報告時使用，一般情況不要帶 |
| `verdict` | `"err"` \| `"warn"` \| `"ok"` | 是 | 其他值 → 422 |
| `headline` | string | 是 | 1–200 字元 |
| `actions` | array | 否（預設空陣列） | **最多 10 筆**；每筆 `{"level": "err"\|"warn"\|"ok", "text": "1–500 字元"}` |
| `notes` | string \| null | 否 | 最多 4000 字元 |
| `building` / `floor` / `system_name` | string \| null | 否 | 供計算 `new_count`/`tracking_count`/`resolved_count` 的範圍篩選 |

**請求範例**：

```json
{
  "report_type": "daily",
  "days": 1,
  "verdict": "warn",
  "headline": "今日 3 台設備有未結案事項，1 台衝擊性指標上升需留意",
  "actions": [
    {"level": "warn", "text": "AHU-601 M1：衝擊性指標持續上升，建議安排專家系統複測"},
    {"level": "err",  "text": "AHU-602 M1：感測器離線已逾 48 小時，請確認接線與供電"}
  ],
  "notes": "本日資料涵蓋率正常，AHU-602 涵蓋率不足（見 get_weekly_report_data）"
}
```

**回傳範例**（200）：

```json
{
  "report_id": 4,
  "report_type": "daily",
  "period_label": "2026-08-26",
  "period_start": "2026-08-26",
  "period_end": "2026-08-26",
  "verdict": "warn",
  "headline": "今日 3 台設備有未結案事項，1 台衝擊性指標上升需留意",
  "actions": [
    {"level": "warn", "text": "AHU-601 M1：衝擊性指標持續上升，建議安排專家系統複測"},
    {"level": "err",  "text": "AHU-602 M1：感測器離線已逾 48 小時，請確認接線與供電"}
  ],
  "notes": "本日資料涵蓋率正常，AHU-602 涵蓋率不足（見 get_weekly_report_data）",
  "new_count": 4, "tracking_count": 0, "resolved_count": 1,
  "generated_at": "2026-08-26T16:01:38+00:00",
  "delivery": {
    "sent": false, "channel": "email",
    "note": "SMTP 尚未設定，本次僅落庫，尚未寄出。寄送介面已預留，待 SMTP 設定後啟用。"
  },
  "daily_send_count": 1,
  "daily_send_limit": 3
}
```

#### 四道卡控

| # | 卡控 | 實作方式 |
|---|---|---|
| 1 | **不接受收件人欄位** | 請求 body 的 Pydantic 模型設 `extra="forbid"`：任何不在契約內的欄位（`to`/`recipients`/`cc` 等）一律 **422**，不是被靜默忽略 |
| 2 | **主旨由系統產生** | 呼叫方不可指定主旨；系統依 `report_type` + `period_label` 產生 |
| 3 | **只收結構化欄位，不收 raw HTML** | `headline`／`notes`／`actions[].text` 一律在寫入前用 `html.escape()` 轉義；即使傳入 `<script>...</script>`，落庫與回傳內容都會是 `&lt;script&gt;...&lt;/script&gt;` |
| 4 | **每日發送次數上限** | 預設 **3** 次／日曆日，環境變數 `VIB_REPORT_DAILY_LIMIT` 可調；超過回 **429**；每次成功呼叫寫入 `audit_log`（`action='send_report'`），失敗（422/429）不計入次數也不寫稽核 |

**驗證錯誤範例**（422，`verdict` 非法值）：

```json
{"detail": [{"type": "literal_error", "loc": ["body", "verdict"],
             "msg": "Input should be 'err', 'warn' or 'ok'", "input": "critical"}]}
```

**超過每日上限**（429）：

```json
{"detail": "已達每日寄送上限（3 次），請明日再試"}
```

**給 Agent 的提示**：

- 呼叫前務必先跑過 §4 的建議工作流程（`get_open_findings` → `get_weekly_report_data`），
  `headline`/`actions` 的措辭一律遵守 §2 的用語限制——本工具**不會**幫忙過濾故障類型
  判定用語，寫進 `headline`/`actions[].text` 的內容會照樣落庫並（未來）寄出。
  Agent 的系統提示必須自律，API 只保證「結構合法、不含可執行 HTML」。
- 同一 `(report_type, period_label)` 重送視為重新產出該期報告（upsert），但**仍會
  消耗一次每日發送額度**——不要把「重送」當成免費操作。
- `429` 發生時不會寫入任何資料，可安全重試（隔日或提高限制後）。

---

## 6. 附錄：與 HVM 的差異對照

| 項目 | HVM | 本系統 |
|---|---|---|
| Header | `X-HVM-API-Key` | `X-VIB-API-Key` |
| 門檻查詢 | `get_alert_thresholds` | `get_vibration_thresholds` |
| 資料時效 | `data_age` | `data_age_minutes`（單位固定為分鐘） |
| 診斷語彙護欄 | （依 HVM 業務性質而定） | **禁止故障類型判定**，見 §2；`interpretation_limit` 為强制欄位 |
| `send_report` 四道卡控 | 收件人系統決定／主旨系統產生／結構化欄位／每日上限 | 完全相同（見 §5 `send_report`） |
