# 使用步驟

**對象**：負責部署與操作本系統的人
**相關文件**：`PLAN_agent_platform_refactor.md`（設計說明）、`docs/agent_tools.md`（API 參考）、`docs/agent_prompt_weekly.md`（Agent 提示）

---

## 〇、一次看懂：資料怎麼流

```
前端輸出的 Analytic CSV（長期量測每 10 分鐘一筆；即時量測每秒一筆）
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

## 一、安裝與設定（依序執行）

### 步驟 1：確認系統需求

| 項目 | 版本 | 備註 |
|------|------|------|
| Python | 3.10+ | 程式使用 `X \| None` 型別語法，3.9 以下會語法錯誤 |
| PostgreSQL | 14+ | 開發與測試在 16 上進行 |

```bash
python --version      # 應顯示 3.10 以上
psql --version
```

### 步驟 2：安裝 Python 套件

```bash
cd <專案目錄>
pip install -r requirements.txt
```

### 步驟 3：建立資料庫與資料表

```bash
createdb vibration
psql -d vibration -f db/schema.sql
```

這會建立 **`vib` schema**、20 張資料表，並寫入預設資料：
13 條判定規則、ISO 10816 四級門檻、3 個簽核階段的 SLA、5 種角色。

> **表放在 `vib` schema 而非 `public` 是刻意的**，用意是與資料庫裡其他系統
> 的表區隔開。因此手動下 SQL 時要寫 `vib.device`，或先執行
> `SET search_path TO vib;`。程式端已在連線時自動設好，不需額外處理。

> 若 `createdb` 提示權限不足，改用：
> `psql -U postgres -c "CREATE DATABASE vibration;"`

### 步驟 4：建立應用程式帳號並授權

**如果你打算用 `postgres` 帳號直接連線，這步可以跳過**，直接到步驟 5。

較好的做法是給應用程式一個獨立帳號。但要注意：**用 `postgres` 建表、卻用
另一個帳號連線的話，那個帳號預設看不到任何表**——不是表沒建成功，而是沒有
權限。這是最容易踩到的坑。

以**建表的那個帳號**（通常是 `postgres`）執行：

```sql
-- 建立應用程式帳號
CREATE USER vibapp WITH LOGIN PASSWORD '你的密碼';

-- 授權存取 vib schema
GRANT USAGE ON SCHEMA vib TO vibapp;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA vib TO vibapp;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA vib TO vibapp;

-- 讓日後新增的表也自動套用同樣權限
ALTER DEFAULT PRIVILEGES IN SCHEMA vib
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vibapp;
ALTER DEFAULT PRIVILEGES IN SCHEMA vib
    GRANT USAGE, SELECT ON SEQUENCES TO vibapp;
```

最後兩段 `ALTER DEFAULT PRIVILEGES` 容易被略過，但**少了它，將來 schema
變更新增的表，這個帳號一樣會存取不到**，而且症狀跟現在一模一樣。

> 授權指令必須在**該資料庫內**執行（`psql -d vibration`），不是連到
> 預設的 postgres 資料庫執行。

### 步驟 5：設定連線參數

複製設定範本：

```bash
cp .env.example .env
```

編輯 `.env`，填入你的實際值：

```ini
VIB_DB_HOST=localhost
VIB_DB_PORT=5432
VIB_DB_NAME=vibration
VIB_DB_USER=vibapp
VIB_DB_PASSWORD=你的密碼

# 發給 Agent 平台的金鑰，產生方式見下
VIB_AGENT_API_KEY=
```

產生一組隨機 API 金鑰：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**關於設定的優先順序**：系統環境變數優先於 `.env` 檔。這是刻意的——
正式機通常由系統或容器注入環境變數，若 `.env` 蓋過去，一個忘了刪的
開發用檔案會讓服務**安靜地連到錯的資料庫**，而且不會報錯。

`.env` 已列入 `.gitignore`，不會被提交。

<details>
<summary>完整的環境變數清單</summary>

| 變數 | 預設值 | 用途 |
|------|--------|------|
| `VIB_DB_HOST` | `localhost` | 資料庫主機 |
| `VIB_DB_PORT` | `5432` | 資料庫連接埠 |
| `VIB_DB_NAME` | `vibration` | 資料庫名稱 |
| `VIB_DB_USER` | `vibcore` | 連線帳號 |
| `VIB_DB_PASSWORD` | 空字串 | 連線密碼 |
| `VIB_AGENT_API_KEY` | 無 | Agent 呼叫 API 的金鑰。**未設定時 API 一律回 503**（拒絕服務而非放行） |
| `VIB_REPORT_DAILY_LIMIT` | `3` | `send_report` 每日寄送次數上限，超過回 429 |

</details>

### 步驟 6：執行自檢確認

```bash
python -m vibcore.check
```

這支程式**只讀取不修改**，可以隨時重複執行。它會依序檢查設定來源、
資料庫連線、資料表是否齊全、預設資料是否寫入，最後告訴你下一步該做什麼。

順利的話會看到類似輸出：

```
【1】設定來源
  ✓ 找到設定檔 /path/to/.env
  目前生效的設定：
  VIB_DB_HOST     = localhost
  ...
  VIB_DB_PASSWORD （已設定）

【2】資料庫連線
  ✓ 連線成功（PostgreSQL 16.13 ...）

【3】資料表
  ✓ 20 張表齊全

【4】預設資料
  ✓ 判定規則：13 筆
  ✓ ISO 門檻等級：4 筆
  ✓ 簽核階段 SLA：3 筆
  ✓ 角色定義：5 筆

【5】營運資料
  · 設備：0 筆
  ...

【6】下一步
  尚未匯入任何資料。建議順序：
    1. 先用歷史資料跑離線回測校準門檻 ...
```

任何一項顯示 `✗` 時，程式會直接印出常見原因與修正指令，不會讓你自己猜。

**到這裡安裝就完成了。** 接下來依 §二 補台帳資料，然後進行 §三 的門檻校準。

---

## 二、初次設定（補台帳資料）

> 這一節的操作都是對資料庫下 SQL。可用 `psql -d vibration` 進入互動介面，
> 或用你慣用的資料庫工具。所有表都在 `vib` schema 下，所以表名要寫成
> `vib.device`（或先執行 `SET search_path TO vib;` 之後就能省略前綴）。

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

先查**資料密度**。前端有兩種輸出版本，密度差很多：

| 版本 | 取樣間隔 | 每小時筆數 |
|------|---------|-----------|
| 長期量測 | 10 分鐘 | 6 |
| 即時量測 | 1 秒 | 3600 |

程式預設假設每小時 3600 筆，但會**自動偵測實際密度並改用偵測值**，同時
連動調整「運轉樣本數門檻」——這兩者必須一起調整，只改其中一個的話，
每一小時都會被判為 `partial`，所有指標型規則靜默跳過。

排程日誌出現這行代表自動偵測已生效，屬正常：
```
資料密度約每小時 N 筆，與預設的 3600 筆不符
已改用偵測到的資料密度：每小時 N 筆
```

若完全沒有 Finding 又沒有這行，用以下 SQL 查資料狀態分佈：

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

### 「自檢說 vib schema 內沒有任何資料表，但我確定建過了」

先確認表到底在不在——用**建表時的那個帳號**（通常是 `postgres`）查：

```sql
SELECT count(*) FROM pg_tables WHERE schemaname = 'vib';
```

- **回傳 0** → 表真的沒建成功，重跑 `psql -d <資料庫名> -f db/schema.sql`
- **回傳 20** → 表在，是**連線帳號沒有權限**。依 §一步驟 4 執行 GRANT

`pg_tables` 是系統目錄不受權限過濾，所以能問出「事實」；而
`information_schema.tables` 會依權限過濾，沒權限時即使表就在那裡也查不到。

新版自檢已能自動區分這兩種情況並給出對應指令，若你的輸出仍只說「沒有任何
資料表」，代表用的是舊版，更新後重跑即可。

### 「改了 .env 但沒有生效」

最常見的原因是**同名的系統環境變數蓋過了 `.env`**。這是刻意的優先順序
（見 §一步驟 5），但排查時容易困惑。確認方式：

```bash
python -m vibcore.check      # 【1】區塊會列出目前實際生效的值
```

若顯示的值不是 `.env` 裡寫的，檢查系統環境變數：

```bash
# Linux / macOS
env | grep VIB_

# Windows PowerShell
Get-ChildItem Env: | Where-Object Name -like "VIB_*"
```

其他可能：`.env` 不在專案根目錄（必須與 `vibcore/` 同層）、
或未安裝 `python-dotenv`（此時 `vibcore.check` 會出現警告）。

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
