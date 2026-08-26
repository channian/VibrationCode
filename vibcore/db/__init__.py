"""
vibcore.db — PostgreSQL 資料存取層

為何獨立成一個套件：規則層、指標層、報告層都需要讀寫同一份 Finding 與
量測資料，若各自組 SQL，簽核狀態機與 upsert 語意（occurrence_count 累加、
JSONB 還原等）會在多處重複實作、彼此漂移。所有 SQL 集中在這裡，其他模組
只透過 `connection` 取得連線、透過 `repository` 的函式操作資料。
"""
