# 離線回測框架（validate/）

上線前的優先項目：**用歷史資料回測規則集，看會噴幾件 Finding，門檻該怎麼調。**

系統會依 `db/schema.sql` 的 13 條規則自動建立 Finding 送進四階段簽核流程。
如果規則太敏感，每週噴出幾百件，就會沒人願意用。這個框架回答的問題就是
「這套規則跑過去 N 個月的資料，量級合不合理」——上線前先看過這份報告，
再決定門檻要不要調。

> **狀態（2026-09）**：14 條規則**全部已接上真實模組**，不再有 stub。
> `validate/rules_stub.py` 保留簡化版僅作為 `vibcore.rules` 無法匯入時的
> 退路，正常執行不會用到；每次執行 `summary.txt` 開頭仍會列出「這次哪些
> 是真實模組、哪些是 stub」，以此為準。

> **ISO 分類的前提**：ISO 10816-3 的 Zone 判定需要「機器群組」與「基礎
> 剛性」兩項，而 Analytic CSV 兩項都沒有（`ISO10816_code` 的語意也未經
> 確認，見 `docs/DATA_CONTRACT.md` §3.1b）。因此**預設所有設備都是未
> 分類**，`ISO_ZONE` 不觸發、`VEL_HIGH` 走 `sigma_fallback`。
> 要跑 ISO 相關的回測，必須明確給定假設：
>
> ```bash
> # 單一假設
> python -m validate.offline --data-dir data/ --assume-iso 3/rigid
>
> # 八種組合的對照表（供專家會議決策，只需 Analytic CSV，不必跑完整回測）
> python -m validate.iso_readiness --data-dir data/ --compare
> ```

## 需要準備什麼資料

- 一個資料夾，裡面放歷史 Analytic CSV（前端每秒輸出的那種，`data/Analytic.csv`
  的格式）。可以是多台設備、多個檔案；同一設備的多個檔案會自動合併。
- **回測涵蓋的時間愈長愈有意義**——只有幾分鐘資料只能驗證程式跑不跑得動，
  看不出真實觸發量級。至少要能涵蓋基準期（14 天）加上一段觀察期（建議
  ≥ 1 個月）才有參考價值。
- （選用）`--device-meta` 一個 JSON 檔，補充 Analytic CSV 沒有的台帳資訊：

  ```json
  {
    "AHU-601": { "is_standby": false },
    "CHW-PUMP-3": { "is_standby": true, "iso_machine_class": "II", "iso_class_source": "frontend" }
  }
  ```

  Analytic CSV 不帶「是否備機」「ISO 等級是否已由工程師確認」這類資訊
  （`ISO10816_code` 目前多數設備仍是 0 = 未設定，見計畫書 §十二），沒有
  這份補充資訊時，`STANDBY_NO_RUNTIME` 會全部判定不適用、`ISO_ZONE` 大多
  會走「未分級」路徑。

## 怎麼跑

```bash
# 最基本用法（沿用 db/schema.sql 的規則預設值，含門檻敏感度掃描）
python -m validate.offline --data-dir data/

# 補上設備台帳資訊
python -m validate.offline --data-dir data/ --device-meta my_device_meta.json

# 整批覆寫規則參數（例如想看看把某條門檻調鬆後的效果）
python -m validate.offline --data-dir data/ --rule-config my_rule_overrides.json

# 略過門檻敏感度掃描（資料量大時這步最花時間）
python -m validate.offline --data-dir data/ --no-sweep

# 自訂輸出目錄
python -m validate.offline --data-dir data/ --out-dir output/validation/2026-08

# 加開自訂的門檻掃描（可重複給多個 --sweep）
python -m validate.offline --data-dir data/ --sweep "SPECTRAL_SHIFT:shift_pct:10,15,20,25"
```

`--rule-config` 的 JSON 格式（key 為 `rule_code`，只需要寫要覆寫的欄位）：

```json
{
  "VEL_HIGH": { "params": { "sigma": 3.5 } },
  "IMPACT_RISE": { "params": { "crest_sigma": 3.0, "kurt_sigma": 3.0 } }
}
```

沒有真實資料、只想確認框架本身能不能用時，用內建的合成資料產生器：

```bash
python -m validate.synthetic --out-dir /tmp/vib_synth
python -m validate.offline --data-dir /tmp/vib_synth \
    --device-meta /tmp/vib_synth/device_meta.json \
    --samples-per-hour 60 --min-running-samples 10 \
    --out-dir output/validation/synthetic_check
```

（合成資料用「每分鐘一筆」而非真實的「每秒一筆」，所以要用
`--samples-per-hour 60` 告訴聚合管線正確的每小時預期樣本數，否則涵蓋率
會被誤判成 partial；`validate/synthetic.py` 檔頭的 docstring 寫明了五台
合成設備各自的劇本與應該觸發哪些規則，可以拿來核對框架有沒有算對。）

## 報告怎麼看

跑完後，`output/validation/`（或你指定的 `--out-dir`）底下會有：

| 檔案 | 內容 |
|------|------|
| `coverage.csv` | 每台設備/量測點的 ok / partial / no_data / not_running 時數與可分析比例 |
| `gaps.csv` | 斷線／資料不全區段清單，依時長由長到短排序 |
| `finding_stats_by_rule.csv` | 依規則的觸發次數、影響設備數、平均持續天數，含 `category` 欄位 |
| `finding_stats_by_device.csv` | 依設備的觸發次數、err/warn 分布（不分類，合計視角） |
| `trigger_density.csv` | **每台設備每週幾件，依 `category` 分開算**（`equipment_per_week` / `data_availability_per_week` / 合計 `episodes_per_week`）——判斷會不會誤報洪水的關鍵表 |
| `episodes_detail.csv` | 每一個觸發事件的明細（設備、規則、起訖、持續天數、`category`） |
| `threshold_sensitivity.csv` | 門檻敏感度掃描（預設對 `VEL_HIGH.sigma`／`IMPACT_RISE.crest_sigma`／`STEP_CHANGE.mahalanobis_sigma` 各掃一輪） |
| `summary.txt` / `summary.html` | 摘要，含指標／規則層實作來源、涵蓋率、觸發統計（分「設備狀態類」／「資料可用性類」兩區塊）、分開的密度數字、掃描結果的重點整理 |

> **13 條規則分成兩類**（見 `vibcore.rules.engine.RULE_CATEGORY`）：
> `data_availability`（資料可用性，處置者是 IT／儀電：`SENSOR_OFFLINE`／
> `DATA_QUALITY`／`SENSOR_SATURATION`／`ISO_CLASS_SUSPECT`／
> `ORIENTATION_CHANGE`）與 `equipment`（設備狀態，處置者是設備工程師：
> 其餘 8 條）。實測資料裡前者常佔七成以上，混算成單一「觸發密度」會讓
> 數字被資料可用性問題撐大，誤導門檻校準與人力配置——所以報表把兩者
> 拆開，`trigger_density.csv` 的排行榜也改依 `equipment_per_week` 排序。

### 判斷順序建議

1. **先看 `summary.txt` 開頭的「指標／規則層實作來源」**：如果關鍵規則
   還在用 stub，這次報告只能看量級趨勢，不能直接拿數字去定門檻。
2. **再看 `coverage.csv`**：可分析比例太低（< 50%）的量測點，後面的觸發
   統計對這個點沒有意義（`is_sufficient` 的判斷邏輯見
   `vibcore.types.CoverageInfo`）。
3. **看 `trigger_density.csv`**：這是最終要回答的問題，但要看
   `equipment_per_week`（設備狀態類）欄位，不是 `episodes_per_week`
   （合計）——`data_availability_per_week` 反映的是感測器／通訊／量程
   佈建品質，該找 IT／儀電，不是拿來校準振動門檻。經驗法則——四階段
   簽核（工程師 → 主管 → 專家 → 結案）每關 SLA 是 5 天（`db/schema.sql`
   的 `sla_config`），一個工程師若要同時盯著多台設備，每台每週超過
   1～2 件設備狀態類 Finding 大概就會開始積壓。若全廠平均或個別設備的
   `equipment_per_week` 明顯超過這個量級，代表門檻太緊。
4. **`episodes_detail.csv` 抽查幾筆**：尤其是持續天數很短（1～2 天）又
   反覆出現的同一條規則同一台設備——這種「一下觸發一下解除」的抖動，
   在正式系統裡等於重複開單、關單，對工程師是干擾而非有效預警，通常
   代表門檻卡在資料的雜訊帶上，需要調鬆或加入「連續 N 天才算」的緩衝
   （目前的 stub 尚未實作這種持續性判斷，見下方限制說明）。
5. **`threshold_sensitivity.csv` 決定新門檻**：找「觸發量斷崖式下降」
   的那個轉折點，通常代表門檻附近有一群「剛好卡在邊緣」的正常樣本，是
   比較安全的切點；同時要確認調整後的觸發密度落在第 3 點的可負荷範圍。

## 已知限制

- **基準期演算法對「低運轉率設備」很嚴格**（`vibcore.metrics.baseline`）：
  要求 14 天滾動窗口內至少有 168 小時（一週）`ok` 資料才建立基準期。備機
  或低負載設備若一天只運轉 1～2 小時，窗口內的 `ok` 小時數可能永遠湊不到
  168，導致**這類設備可能一直拿不到基準期**，所有依賴基準的規則
  （`VEL_HIGH`／`IMPACT_RISE`／`STEP_CHANGE`／`ISO_CLASS_SUSPECT`）會靜默
  地不觸發——這不是 bug，是演算法拒絕用太少的樣本硬湊一個不可靠的基準，
  但代表**這類設備幾乎只能靠 `STANDBY_NO_RUNTIME`／`SENSOR_OFFLINE`／
  `DATA_QUALITY` 這幾條不需要基準的事件類規則監測**，回測合成資料的
  `DEV-STANDBY` 就是這個情境的示範，見 `trigger_density.csv` 只有
  `STANDBY_NO_RUNTIME` 一條規則觸發。若貴司備機占比不低，這點需要提前
  跟工程團隊對齊監測策略的期待。
- 尚未接上真實模組的 8 條規則（見上方「重要」段落）用的是簡化 stub，
  邏輯直接寫在 `validate/rules_stub.py`，未處理離群值、未分工況。
- **`STEP_CHANGE` 的 `mahalanobis_sigma` 命名容易誤解**：這是直接對
  Mahalanobis 距離設的門檻，**不是**常態分布的 σ 分位數（`k` 維距離平方
  服從卡方分布，尾巴比常態厚很多）。用了幾個特徵，同一個門檻數字對應的
  「有多稀有」就不一樣——回測若發現某台健康設備仍時不時觸發
  `STEP_CHANGE`，先看 `threshold_sensitivity.csv` 裡這條規則在多高的值
  才穩定下來，不要直接套用預設的 `3.0`。
- **逐日評估、不含持續性緩衝**：目前一旦某天判定觸發即開始計一個事件，
  沒有「連續 N 天才算數」的緩衝。真實規則層若加上這類緩衝，觸發量會比
  這份回測的數字更低（更保守），可以把這份報告的密度數字當作「不加緩衝
  情境下的上限」。
- **量測點切分是猜的**（`validate/points.py`）：Analytic CSV 沒有明確的
  「量測點」欄位，框架依 `Label` 或 `Channel_X/Y/Z` 組合猜測，不保證與
  正式台帳（`measure_point` 表）一致。

## 模組如何逐一換成真實實作

不需要改 `validate/offline.py` 或任何呼叫端，只需要在各自的檔案裡把
`try: from vibcore.xxx import yyy` 的匯入路徑改成真正的位置：

| 尚缺的模組 | 目前的 stub 位置 | 真實模組完成後 |
|-----------|-----------------|----------------|
| 其餘 8 條規則 | `validate/rules_stub.py` 的 `rule_*` 函式 | `_try_import_real_registry()` 會自動嘗試匯入 `vibcore.rules.REGISTRY`，逐條規則覆蓋——已完工的規則優先用真的，其餘繼續用 stub，不需要等全部規則都寫完才能重跑回測 |
| 三軸能量佔比基準 | `validate/baseline_stub.py` 的 `_stub_axis_energy_baseline` | 若之後出現 `measure_point.axis_energy_baseline` 的離線等價模組，在 `_import_real_baseline_fn()` 比照基準期的接法補上 |

`ISO_ZONE` / `ISO_CLASS_SUSPECT`（`vibcore.metrics.iso`）、`STEP_CHANGE`
（`vibcore.metrics.deviation`）、`DEGRADE_TREND` / `SPECTRAL_SHIFT`
（`vibcore.metrics.trend`）與基準期計算（`vibcore.metrics.baseline`）
都已經接上真實模組，不需要改。

## 只碰 validate/ 目錄

本框架不修改 `vibcore/` 或其他既有檔案，只依賴 `vibcore.types` /
`vibcore.config` / `vibcore.pipeline.aggregate` / `vibcore.io.analytic_reader`
（以及已存在的 `vibcore.metrics.iso` / `vibcore.metrics.deviation`）這些
公開契約。
