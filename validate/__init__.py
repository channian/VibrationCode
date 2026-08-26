"""
validate — 離線回測框架（Phase 1 上線前驗證）

**為什麼需要獨立於 vibcore 的一個套件**：規則引擎會依規則自動建立 Finding
送進四階段簽核流程，一旦規則太敏感，系統會被 Finding 洪水淹沒而沒人願意用。
上線前必須先問「這套規則跑過去 N 個月會噴幾件」，而不是等正式上線後才發現。

這個套件只依賴 `vibcore` 的公開契約（`vibcore.types` / `vibcore.config` /
`vibcore.pipeline.aggregate` / `vibcore.io.analytic_reader`），不修改
`vibcore` 底下任何檔案。指標層（基準期、趨勢）與規則層在本檔案撰寫當下
尚未完成，因此本套件用可替換的 stub 頂上，並在每處明確標註替換點——
見 `validate/baseline_stub.py` 與 `validate/rules_stub.py` 開頭的說明。
"""
