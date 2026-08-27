# 使用步驟

**對象**：負責部署與操作本系統的人
**相關文件**：`PLAN_agent_platform_refactor.md`（設計說明）、`docs/agent_tools.md`（API 參考）、`docs/agent_prompt_weekly.md`（Agent 提示）

---

## 〇、一次看懂：資料怎麼流

```
前端輸出的 Analytic CSV（每秒一筆、628 欄）
        │
        │  ① 每日排程            python -m vibcore.pipeline.daily
        ▼
   每小時聚合（僅運轉樣本）→ PostgreSQL
        │
        │  ② 規則判定（同一支排程內完成）
        ▼
   Finding（四階段簽核）
        │
        ├─→ ③ Agent 呼叫 API 取得結構化上下文 → 撰寫週報 → send_report
        │
        └─→ ④ Dashboard 檢視與簽核（尚未實作）
```

原始 CSV **留在檔案系統**，資料庫只放聚合後的精選欄位（120 個量測點約 0.23 GB/年）。

---

## 一、環境準備

### 1.1 系統需求

| 項目 | 版本 | 備註 |
|------|------|------|
| Python | 3.10+ | 程式使用 `X \| None` 型別語法 |
| PostgreSQL | 14+ | 開發與測試在 16 上進行 |

### 1.2 安裝套件

```bash
pip install -r requirements.txt
```

### 1.3 建立資料庫

```bash
createdb vibration
psql -d vibration -f db/schema.sql
```

`schema.sql` 會建立 `vib` schema 與全部資料表，並寫入預設值：
ISO 10816 四級門檻、13 條規則的參數、各簽核階段 SLA（各 5 天）、5 種角色。

### 1.4 設定連線

以環境變數提供，程式不讀設定檔：

```bash
export VIB_DB_HOST=localhost
export VIB_DB_PORT=5432
export VIB_DB_NAME=vibration
export VIB_DB_USER=vibapp
export VIB_DB_PASSWORD=********
export VIB_AGENT_API_KEY=<發給 Agent 平台的金鑰>
```

> Windows 用 `set` 而非 `export`。建議寫成批次檔或用系統環境變數，
> 不要寫進程式碼。

---

## 二、初次設定

### 2.1 設備台帳

**多數欄位不需要手動建立。** 每日排程會從 Analytic CSV 內建的 metadata
（`Name` / `Building` / `Floor` / `System` / `RPM` / `FMF`）自動建立設備與量測點。

但以下三項 CSV 裡沒有，需要人工補：

| 欄位 | 影響 | 怎麼設 |
|------|------|--------|
| `is_standby` | 備機不判異常，改用試車規則監測 | 見下方 SQL |
| `iso_machine_class` | 未設定時**不套用 ISO Zone 判定**，只做相對趨勢 | 依 ISO 10816 分級 |
| `owner_user_id` | Finding 要指派給誰 | 需先建立使用者 |

```sql
-- 標記備機
UPDATE vib.device SET is_standby = TRUE WHERE device_id IN ('P-Backup-02');

-- 設定 ISO 等級（Class I~IV），並標明來源
UPDATE vib.device
   SET iso_machine_class = 'II', iso_class_source = 'manual_override'
 WHERE device_id = 'AHU-601';
```

> **ISO 等級沒設就不會有 Zone 判定。** 系統會誠實顯示「未分級」而不是猜一個。
> 若填錯，`ISO_CLASS_SUSPECT` 規則會在基準期中位數已超過該等級 B/C 界時
> 提醒複核。

### 2.2 使用者與角色

認證由 AD 負責，資料庫只存對應：

```sql
INSERT INTO vib.app_user (ad_username, ad_sid, display_name, email, department)
VALUES ('k16086', 'S-1-5-21-xxxx', '陳工程師', 'eng@corp', '設備部');

-- 指派角色（scope 皆為 NULL = 不限廠區）
INSERT INTO vib.user_role (user_id, role_code) VALUES (1, 'engineer');

-- 只負責 FAB7 的話
INSERT INTO vib.user_role (user_id, role_code, scope_type, scope_value)
VALUES (1, 'engineer', 'building', 'FAB7');
```

### 2.3 電流資料（選用）

有 SCADA 電流時，運轉判定會改用電流門檻（比 vRMS 準）。需建立對照：

```sql
INSERT INTO vib.tag_mapping (tag_id, device_id, variable_type, unit)
VALUES ('CWP_CURR_01', 'CWP-203', '電流', 'A');
```

沒有電流資料也能運作，會退回以 `velRMS > 0.1 mm/s` 判定運轉。

---

## 三、上線前必做：離線回測校準門檻

**這一步不能跳過。** 規則門檻的預設值是依合成資料訂的，直接上線很可能造成
誤報洪水——每週噴出幾十上百件 Finding，簽核流程會在第一週就被放棄。

```bash
python -m validate.offline \
    --data-dir /path/to/歷史Analytic資料 \
    --device-meta validate/device_meta.example.json \
    --out-dir output/validation
```

### 3.1 先看這三個數字

報告產在 `output/validation/`：

| 檔案 | 看什麼 |
|------|--------|
| `summary.txt` | 先看開頭的**可分析比例**。若接近 0，本次結果無意義，先查資料密度 |
| `trigger_density.csv` | **每台設備每週幾件 Finding**。這是判斷會不會誤報洪水的關鍵 |
| `threshold_sensitivity.csv` | 同一份資料在不同門檻下會觸發多少件 |

### 3.2 怎麼判斷門檻合不合理

觸發密度沒有絕對標準，但可以這樣抓：

- **健康設備應該接近 0 件/週。** 若一台你知道沒問題的設備每週噴好幾件，
  門檻就太鬆
- 掃描表若在某個門檻附近**觸發數急遽下降**，代表原門檻落在雜訊區
- 合成資料的實測顯示 `STEP_CHANGE` 在 σ=3.0 時明顯偏鬆（σ=3.5 觸發數降到
  約 1/4），但**這個數字必須用你的真實資料重跑才算數**

### 3.3 調整門檻

```sql
UPDATE vib.rule_config
   SET params = '{"mahalanobis_sigma": 3.5}'::jsonb
 WHERE rule_code = 'STEP_CHANGE';
```

改完重跑一次回測確認效果。**規則門檻是設定不是寫死的**，之後隨時可調。

---

## 四、每日排程

```bash
python -m vibcore.pipeline.daily --data-dir /path/to/當日Analytic資料
```

補跑指定日期：

```bash
python -m vibcore.pipeline.daily --data-dir /path/to/資料 --date 2026-08-25
```

### 4.1 這支程式做了什麼

1. 讀取資料夾內所有 Analytic CSV，依設備分組
2. 聚合為每小時（僅計運轉樣本），寫入資料庫
3. 記錄匯入軌跡（成功與失敗都記）
4. 沒有基準期的量測點，嘗試自動建立
5. 執行 13 條規則，建立或更新 Finding
6. 數值較上次更偏離基準 → 標記惡化
7. 連續 3 天未再觸發 → 自動結案

### 4.2 重跑是安全的

聚合用 `ON CONFLICT` 覆蓋、Finding 依 `finding_key` 收斂，**同一天重跑只會
覆寫，不會產生兩筆事項或把發生次數灌成兩倍**。排程失敗補跑不必擔心。

### 4.3 排程設定

Linux（cron，每日 02:00）：
```
0 2 * * * cd /opt/vibration && /usr/bin/python3 -m vibcore.pipeline.daily --data-dir /data/analytic >> /var/log/vib_daily.log 2>&1
```

Windows 工作排程器：動作設為 `python.exe`，引數 `-m vibcore.pipeline.daily --data-dir D:\data\analytic`，起始位置設為專案根目錄。

### 4.4 執行結果怎麼看

結束時會印出一行摘要：

```
2026-09-09：量測點 120 成功 / 0 失敗，事項 upsert 8、自動結案 2、標記惡化 1
```

**有失敗的量測點會逐一列出。** 單一設備失敗不會中斷其餘設備，但要追查——
當日那台設備等於沒有監測。

---

## 五、啟動 API 服務

```bash
uvicorn vibcore.api.main:app --host 0.0.0.0 --port 8000
```

驗證：

```bash
curl -H "X-VIB-API-Key: $VIB_AGENT_API_KEY" \
     http://localhost:8000/api/agent/tools/get_device_list
```

互動式文件在 `http://localhost:8000/docs`。

---

## 六、串接 Agent 平台（Profet AI）

1. 把 `docs/agent_prompt_weekly.md` 的系統提示貼進工作流
2. 依 `docs/agent_tools.md` 設定 8 支工具的呼叫（Base URL、`X-VIB-API-Key`）
3. 平台端設定排程（週報每週一次、日報每日一次）

### 6.1 上線前用這幾個情境各跑一次

- 全週無異常 → 是否正常產出且沒有硬擠建議
- 有惡化中的事項 → 是否拉高呈現並寫出惡化幅度
- 某設備整週斷線 → 是否有明說「沒有警示不代表正常」
- 輸出文字掃一遍 → 有沒有出現禁止的故障類型用語

`docs/agent_prompt_weekly.md` 文末有完整檢查清單。

---

## 七、疑難排解

### 「每天都沒有任何 Finding」

先查**資料密度**。系統預設每小時應有 3600 筆（每秒一筆）。若實際資料
不是這個密度，每一小時都會被判為 `partial`，所有指標型規則靜默跳過。

排程日誌會出現：
```
資料密度約每小時 N 筆，與預設的 3600 筆不符
```
出現這行代表**自動偵測已生效**，屬正常。若完全沒有 Finding 又沒有這行，
用以下 SQL 查資料狀態分佈：

```sql
SELECT data_status, count(*) FROM vib.measurement_agg
 WHERE ts_hour > now() - interval '7 day' GROUP BY 1;
```

`ok` 為 0 表示沒有任何可判定的資料。

### 「某設備從來沒有基準期」

基準期需要 14 天窗口內至少 168 小時的可信資料。**備機通常湊不到**——
一天只跑一兩小時的設備永遠達不到門檻，這是已知限制。這類設備只能靠
`STANDBY_NO_RUNTIME` 與 `SENSOR_OFFLINE` 監測。

一般設備若也建不出基準，查該量測點的 `ok` 小時數是否足夠。

### 「週報的『本週新發現』永遠是空的」

不應該發生。分段依 `first_seen_at` 是否落在本期內判定，與觸發次數無關。
若真的遇到，檢查 `finding.first_seen_at` 是否正確寫入。

### 「某設備報了感測器斷線，但現場感測器是好的」

查匯入軌跡——很可能是**排程沒跑**而不是感測器問題：

```sql
SELECT ingest_date, status, note FROM vib.ingestion_log
 WHERE point_id = (SELECT point_id FROM vib.measure_point
                    WHERE device_id = 'EF-405' AND position = 'M1')
 ORDER BY ingest_date DESC LIMIT 10;
```

週報本身也會把兩者分開列在「設備面問題」與「系統面問題」，但若有疑慮
可直接查表確認。

### 「檔案被 Excel 開著導致寫入失敗」

輸出報表的程式都有 `PermissionError` 保護，會跳過該檔並繼續處理其餘設備，
不會整批中斷。關掉檔案重跑即可。

---

## 八、常用查詢

```sql
-- 待處理事項（含 SLA 逾期判定與最新人工回覆）
SELECT * FROM vib.v_open_finding ORDER BY severity, days_in_stage DESC;

-- 全廠設備狀態總覽
SELECT * FROM vib.v_device_status ORDER BY n_err DESC, n_warn DESC;

-- 近 7 日資料涵蓋率
SELECT mp.device_id, mp.position,
       count(*) FILTER (WHERE data_status = 'ok')          AS 可分析,
       count(*) FILTER (WHERE data_status = 'no_data')     AS 斷線,
       count(*) FILTER (WHERE data_status = 'not_running') AS 未運轉,
       count(*) FILTER (WHERE data_status = 'partial')     AS 資料不全
  FROM vib.measurement_agg m JOIN vib.measure_point mp USING (point_id)
 WHERE ts_hour > now() - interval '7 day'
 GROUP BY 1, 2 ORDER BY 斷線 DESC;

-- 各階段積壓
SELECT status, count(*), round(avg(EXTRACT(DAY FROM now() - stage_entered_at))) AS 平均停留天數
  FROM vib.finding
 WHERE status NOT IN ('closed', 'auto_resolved', 'false_positive')
 GROUP BY 1;
```

---

## 九、目前尚未實作

| 項目 | 狀態 |
|------|------|
| Dashboard | 需求書已備妥（`DASHBOARD_requirements.md`），尚未開發 |
| 週報 Email 寄送 | SMTP 未設定；`send_report` 目前只落庫，寄送介面已預留 |
| 設備 A/B 期間比較 | 需求已納入需求書，尚未實作 |
