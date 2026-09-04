# 2026-09 專家對焦會議：定案紀錄

對焦資料：`專家對焦資料 · 三軸振動指標與判定依據`（Q1–Q6 為需專家判斷事項，
D1–D2 為內部決策）。本檔記錄每一項的結論與對應的程式變更，避免日後重複討論
或反向修改時找不到依據。

---

## Q1 · velRMS 的頻段與 ISO Zone 邊界是否相容 → **可接受**

**背景**：ISO 10816-3 的分區定義在 10–1000 Hz（> 600 rpm）或 2–1000 Hz
（120–600 rpm）的速度 RMS 上。我方以頻域積分（10–1000 Hz）對照前端 velRMS，
比值 1.09–1.16——量綱正確，但這 9–16% 的落差代表前端濾波帶與標準不同，
而前端的濾波設定取不到。

**結論**：專家確認在「篩選預警」的定位下可接受，不需要向前端要濾波設定，
也不需要自行從原始波形重算。

**程式**：無變更。維持以前端 velRMS 直接對照 ISO Zone 邊界。

**要記住的限制**：邊界附近的設備（例如 Group 2 剛性基礎的 A/B 界 1.40 mm/s）
仍可能因這 9–16% 的差異跨區。若日後出現「Zone 判定與現場實測不符」的個案，
這是第一個要回頭檢查的地方。

---

## Q2 · kurtosis 在本廠沒有鑑別力 → **移除 kurtosis 通道**

**背景**（兩項實測）：

1. **跨設備反向**——三台設備 accOA 1137.6/504.8/129.7 對應 accKURT 中位數
   2.37/3.60/4.53，排序完全顛倒。成因是 kurtosis 為比值型指標
   （Pearson 定義 m₄/m₂²），分母含背景振動量：機器越吵，σ 越大，偶發衝擊
   越被稀釋。受控驗證可重現此型態——固定衝擊列、只調背景雜訊，accOA 上升
   8 倍時 accKURT 由 22.8 降至 3.0。
2. **改成純相對基準後仍無鑑別力**——移除失效的絕對門檻、聚合改用中位數後，
   觸發結果完全沒變（33 週 511 次 / 60 台，佔機隊 88%），且門檻掃描曲線
   平滑無斷崖（1.5→4.0 件數 −46%、告警總天數僅 −22%）。

**結論**：kurtosis 不作為衝擊判準。

**程式變更**：

| 位置 | 變更 |
|---|---|
| `vibcore/rules/metric_rules.py` · `_IMPACT_CHANNELS` | 移除 `kurt` / `kurt_axis` 兩個通道 |
| `vibcore/rules/metric_rules.py` · `impact_rise()` | 只判定 `crest` 與 `crest_axis`；連帶移除 `require_both` 參數（它原本是用來要求 crest 與 kurt 同步上升） |
| `vibcore/rules/metric_rules.py` · `_DEGRADE_TREND_METRICS` | 移除 `acc_kurt`——「數值上升＝惡化」這個前提在本廠不成立 |
| `validate/rule_defaults.py` | IMPACT_RISE 參數只剩 `crest_sigma` / `crest_axis_sigma` |
| `validate/rules_stub.py` | 同步改成 crest-only，免得退回 stub 時與真實實作結果不同 |

**刻意沒有變更的部分**：

- **`STEP_CHANGE` 仍使用 `acc_kurt`**。在那裡它的角色是「這台機器的運轉點
  是否偏離自己的常態」的其中一個維度，不是衝擊判準；而且移除會改變
  Mahalanobis 的維度與門檻語意（k 維距離平方服從卡方分布，自由度變了，
  同一個門檻數字對應的稀有程度就不同）。這不在本次定案範圍內。
- **accKURT 欄位仍照常聚合入庫**（`acc_kurt` / `acc_kurt_median` /
  `acc_kurt_axis_max` / `acc_kurt_axis_median`）。不做破壞性 migration——
  STEP_CHANGE 還在用，且日後若換感測器或前端改算法需要重新評估。

**accCREST 為什麼留著**：它同樣是比值型指標（peak/RMS），但沒有對應的
實測證據顯示它在本廠失效，且它是諧波不可用之後僅存的衝擊性通道。
**下一次回測要另外量「只用 crest」的觸發率**——若仍在 80% 以上，就該
一併考慮移除整條 IMPACT_RISE。

---

## Q3 · 是否必須做工況分層 → **納入，但細節待討論**

**結論**：可額外提供 mapping CSV，電流與頻率一同納入。實際欄位格式與
分層方式尚待討論。

**程式**：本次未動。`tag_mapping` / `scada_reading` 兩張表已在 schema 中，
仍無程式讀寫。

**討論前需要釐清的**：

1. mapping CSV 的欄位長相——一台設備對幾個 tag？tag 名稱與 `device_id`
   怎麼對應（Analytic CSV 的 `Label` 欄目前存的就是電流 TAG 名稱，
   見 `validate/points._position_series` 的註解，可能已經是現成的對應）。
2. SCADA 讀值的取樣頻率與時間戳時區——要與 `measurement_agg` 的
   `ts_hour` 對齊才能分層。
3. 分層的切法：轉速區間？負載區間？至少幾層？代價是每層樣本數變少，
   而基準期本來就要求 14 天窗口內 168 小時可信資料。
4. 歷史資料的回補範圍——沒有歷史 SCADA 就只能從接上之後才開始分層，
   既有基準期要不要重算。

---

## Q4 · 備機的監測策略 → **先靜態旗標，之後改電流**

**結論**：mapping 帶一個備機欄位先讓 `is_standby` 有正確來源；等 SCADA
電流接上後改用電流判斷實際有沒有開機，靜態旗標退為 fallback。

**程式變更**（第一階段，已完成）：

| 位置 | 變更 |
|---|---|
| `vibcore/types.py` · `DeviceContext.is_standby` | `bool` → `bool \| None`，預設 `None`。三態：True/False 是台帳明確填寫，None 是「這個來源不知道」 |
| `vibcore/db/repository.py` · `upsert_device` | INSERT 用 `COALESCE(%(is_standby)s, FALSE)`（DB 欄位是 NOT NULL）；ON CONFLICT 用 `COALESCE(%(is_standby)s, device.is_standby)` 保留台帳值 |
| `validate/points.py` | 台帳沒填時給 `None` 而非 `False` |

**修掉的是什麼 bug**：每日排程用 Analytic CSV 組出的 `DeviceContext`
不可能知道備機與否，但舊版預設 `False`，upsert 時會把管理員設好的
`True` 重設為 `False`，**全程沒有任何錯誤訊息**。這也是回測中
`STANDBY_NO_RUNTIME` 觸發 0 次的原因之一。

ON CONFLICT 子句讀的是原始參數而不是 `EXCLUDED.is_standby`——`EXCLUDED`
取的是 VALUES 算完之後的值，那個 `COALESCE` 已經把 NULL 變成 FALSE，
用它就分不出「確認不是備機」與「不知道」了。

**第二階段（待 Q3 定案後）**：用電流判斷運轉狀態。這件事的 CP 值比工況
分層更高——目前「設備是否在運轉」是用 velRMS 門檻猜的，而那個分類是整套
涵蓋率統計的分母。

---

## Q5 · 本系統與專家系統的轉手判準 → **先試跑一陣子再決定**

**結論**：先累積實際運行資料，再回頭定義「什麼情況該直接排實測」。

**程式**：無變更。`Finding.needs_expert_measurement` 欄位保留，目前仍
沒有任何規則會設定它。

---

## Q6 · 觀察名單的升級門檻 → **先試跑一陣子再決定**

**結論**：同 Q5。六條 observe 規則維持現況，等累積誤報回饋後再逐條評估。

**程式**：無變更。

**下次評估要看的數字**：移除 kurtosis 通道之後 IMPACT_RISE 的觸發量會下降，
但下降多少要等回測。若觀察名單仍觸及大半機隊，Q6 就不只是「升級門檻訂多少」
的問題，而是規則本身要不要留。

---

## D1 · 要不要補 ISO 台帳 → **需要補**

**依據**：68 台 33 週的實測，台帳空白時工程師每週處理 13 件，填了之後
3 件（VEL_HIGH 366 次 / 58 台 vs 85 次 / 16 台；告警總天數 4,401 vs 858）。
未分類時 VEL_HIGH 退回相對基準 3σ，而那遠比 ISO 錨定門檻敏感——不補台帳
省下的是填兩個欄位的功夫，換來的是 4.3 倍的工作量。

**程式變更**（補台帳的路徑）：

| 位置 | 變更 |
|---|---|
| `validate/iso_readiness.py` | 新增 `--emit-ledger PATH`，產出待填 CSV 範本：預填 `device_id` / `rated_rpm` / `n_running` / velRMS 中位與 p95（判斷群組要看轉速與功率，判斷「這台有沒有在跑」要看運轉樣本數），空出六個待填欄位 |
| `validate/points.py` | `--device-meta` 除 `.json` 外新增 `.csv` 解析。空字串一律視為「沒填」而非「填了空值」；`is_standby` 接受 TRUE/true/Y/是/1 等寫法，認不得的值記警告並視為未填 |
| `validate/device_meta.example.json` | 改用 `iso_machine_group` / `iso_foundation`（舊的 `iso_machine_class` 是誤用 ISO 2372 時期留下的欄位），補上欄位說明 |

**為什麼是 CSV**：這份資料要由工程師填 68 台。逐台手寫 JSON 物件既慢又
容易漏逗號，錯一個字整份讀不進去；CSV 可以在試算表裡填、排序、整批複製
同型號的值。

**要填的六欄與填法**見 `validate/README.md` 的「需要準備什麼資料」。
重點：群組與基礎剛性**必須成對填**（只填一邊算不出 Zone，程式會整台視為
未分類）；同型號、同安裝方式可整批填；不必一次填到完美，
`ISO_CLASS_SUSPECT` 會回頭比對基準水準與所填分類是否矛盾。

---

## D2 · 資料可用性 → **持續觀察確認**

**現況**：排除 IT 換版斷層（2026-06-05 之前）後，平均可分析比例
51.1% → 73.8%，低於 50% 的量測點 27 → 15。另有 2 個量測點換版後完全
沒有資料，需確認是故障還是除役。

**程式**：無變更。回測與涵蓋率統計一律加 `--since 2026-06-05` 排除換版
斷層（見 `validate/README.md`）。

---

## 本次一併處理的技術債

- **`is_standby` 覆寫**：已修，見 Q4。
- **`device_meta.example.json` 欄位過時**：已改，見 D1。

## 仍未處理的技術債

| 項目 | 狀況 |
|---|---|
| TEMP_RISE 未扣除環境溫度 | 晶片溫度受環境影響，夏天全廠一起升溫會同時觸發。建議用同區域設備的溫度中位數當代理（`building`/`floor` 欄位已有，不需額外資料源）。改變判定語意，待確認方向 |
| `mahalanobis_sigma` 命名誤導 | 這是直接對 Mahalanobis 距離設的門檻，不是常態分布的 σ 分位數 |
| SCADA 未接上 | 見 Q3 |
| 諧波結論的翻案條件 | 「振幅不可用」建立在三台設備上。若前端日後修正，軸承特徵頻率分析才有可能納入 |
