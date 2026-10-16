# 驗證佐證資料

認證簡報（`docs/AGENT_certification_draft.md`）引用的圖表與原始結果。**全部可重現**，除了圖 3 的數字來自真實生產資料。

| 檔案 | 是什麼 | 產生方式 |
|---|---|---|
| `agent_guard_scenarios.json` / `.csv` | Agent 護欄 9 個情境、61 筆案例的逐筆結果（payload 樣本、設計意圖、實際結果、狀態） | `python -m validate.agent_guard_scenarios` |
| `replay_synthetic.json` | 合成資料逐日重放：每條規則第一次出現的日期（`first_day`）、最終的事項與觀察項目 | `python -m validate.replay_synthetic` |
| `replay_summary.json` | 每台合成設備：劇本預期觸發 vs 實際觸發（標出劇本之外的觸發） | `python -m validate.evidence_charts` |
| `fig1_guard_scenarios.png` | 各情境的案例結果（通過／已知缺口／需真實 Agent 才能測） | 同上 |
| `fig2_guard_effect.png` | 每道機制處理了幾筆壞 payload、合法對照組有沒有被誤擋 | 同上 |
| `fig3_rule_calibration.png` | 規則校正閉環的三次調整（衝擊性、偏高規則、每週工作量） | 同上；**數字來自真實回測，寫在 `validate/evidence_charts.py` 的 `REAL_BACKTEST`** |
| `fig4_synthetic_replay.png` | 合成資料的劇本事件 vs 正式排程實際觸發的時間 | 同上 |

## 這些資料證明什麼、不證明什麼

**證明**
- 護欄**機制**實際生效：診斷用語、數字白名單、總評下限、越權夾帶、姓名去識別、認證，打真實 HTTP 端點、走真實的資料庫寫入路徑。
- 這些通過不是空轉：各道機制單獨停用後重跑，對應案例會失敗（見認證草稿第 5 張）。
- 合成資料的已知答案：正式排程（逐日重放）能不能在劇本植入的事件上觸發對的規則。

**不證明**
- Agent（Qwen）寫出來的內容品質、被擋的比例、被注入後的行為——腳本用的是腳本寫好的 payload，不是模型輸出。這些欄位仍是【待實測】。
- 規則的準確度——合成資料是照規則寫的，驗證的是管線行為。召回（真的壞了抓不抓得到）沒有證據。
- 圖 3 的數字：無法在這個環境重現（真實生產資料）。

## 重放（合成資料逐日走正式排程）

```
python -m validate.replay_synthetic --out docs/evidence     # 會重建 *_test 資料庫，約 5～10 分鐘
python -m validate.evidence_charts                          # 由結果重畫圖
```

腳本做的事：用 `validate/synthetic.py` 的 5 台已知劇本設備，**每天一個只含當天資料的資料夾**（那天沒有資料的設備就沒有檔案），
對每一天跑 `vibcore.pipeline.daily.run_daily`，第一天後匯入台帳，並記下每條規則第一次出現在哪一天。
「每天只放當天資料」是日常排程最可能的用法，也是找出決策紀錄 #13（停止送資料的感測器不會被判定）的原因。
合成資料的 30 天設備在資料結束後，系統會判定它們逾時無資料，這是預期行為（它們真的沒有資料了）。

## 去識別

圖表與 JSON 只含合成設備名（`DEV-*`、`AHU-601` 等範例名稱）與規則代碼，沒有真實設備名、人名、網域或 IP。
