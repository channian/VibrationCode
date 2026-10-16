"""
replay_synthetic.py — 合成資料逐日走一遍正式排程，記錄每條規則第一次出現在哪一天

回測直接呼叫規則函式；正式排程走 `run_daily → process_point → evaluate_all`，兩條路不一樣
（見決策紀錄〈上線前補洞〉）。這支腳本用 `validate/synthetic.py` 的 5 台**已知劇本**設備
（穩定、斷線、備機、漸進劣化、軸能量分佈跳變），像真的上線那樣：

  1. 每天一個資料夾，**只含當天的列**（那天沒有資料的設備就沒有檔案——停止送資料的感測器就是這個樣子）
  2. 對每一天跑 `vibcore.pipeline.daily.run_daily`
  3. 第一天跑完後匯入台帳（模擬上線流程：先跑一次建立設備，再匯入台帳）
  4. 每天跑完記下哪些事項、觀察項目已經存在，得到「每條規則第一次出現在哪一天」

合成資料是照規則寫的，這驗證的是**管線行為**（該觸發的有沒有觸發、不該觸發的有沒有安靜），不是規則準確度。

執行（會 DROP 並重建 `--dbname`，名稱必須以 _test 結尾）：

    python -m validate.replay_synthetic                       # 預設庫 vib_replay_test
    python -m validate.replay_synthetic --out docs/evidence

產出 `replay_synthetic.json`；圖與「劇本預期 vs 實際」摘要由 `python -m validate.evidence_charts` 畫出。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

import test_reporting as T  # 沿用測試庫建置與「必須 _test 結尾」的保護
from vibcore.types import CLOSED_STATUSES
from validate import synthetic

#: 台帳：第一天之後匯入。DEV-STANDBY 標為備機、DEV-DEGRADE 指定 Group 2 剛性基礎（B/C 邊界 2.8 mm/s）
LEDGER = [
    ("device_id", "rated_power_kw", "iso_machine_group", "iso_foundation", "iso_driver_type", "is_standby"),
    ("DEV-STABLE", 45, 2, "rigid", "external", "FALSE"),
    ("DEV-GAP", 11, 4, "rigid", "external", "FALSE"),
    ("DEV-STANDBY", 30, 2, "rigid", "external", "TRUE"),
    ("DEV-DEGRADE", 45, 2, "rigid", "external", "FALSE"),
    ("DEV-ORIENT", 45, 2, "rigid", "external", "FALSE"),
]


def _jsonable(o):
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return float(o)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dbname", default="vib_replay_test")
    ap.add_argument("--out", default="docs/evidence")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    refusal = T.ensure_disposable_dbname(args.dbname)
    if refusal:
        print(refusal)
        return 2
    logging.getLogger("vibcore").setLevel(logging.ERROR)
    os.environ["VIB_DB_NAME"] = args.dbname          # ledger_import 走 get_connection，吃環境變數
    T.build_test_db(args.dbname)

    from vibcore import ledger_import
    from vibcore.pipeline import daily

    frames = synthetic.generate_all()
    for name, df in frames.items():
        df["_date"] = df["Time"].str.slice(0, 10).str.replace("/", "-")
    dates = sorted({d for df in frames.values() for d in df["_date"].unique()})
    print(f"合成資料：{len(frames)} 台設備、{len(dates)} 天（{dates[0]} ～ {dates[-1]}）")

    conn = T.connect(args.dbname)
    first_day: dict[str, str] = {}
    try:
        for n, day in enumerate(dates):
            with tempfile.TemporaryDirectory() as d:
                for name, df in frames.items():
                    rows = df[df["_date"] == day].drop(columns=["_date"])
                    if len(rows):                      # 那天沒有資料的設備，不給檔案
                        rows.to_csv(os.path.join(d, f"{name}.csv"), index=False, encoding="utf-8-sig")
                res = daily.run_daily(conn, d, dt.date.fromisoformat(day))
            conn.commit()
            if n == 0:
                with tempfile.TemporaryDirectory() as d:
                    path = os.path.join(d, "ledger.csv")
                    with open(path, "w", newline="", encoding="utf-8-sig") as f:
                        csv.writer(f).writerows(LEDGER)
                    ledger_import.main(["--csv", path])
            with conn.cursor() as cur:
                cur.execute("SELECT device_id, rule_code FROM finding WHERE status <> ALL(%s)", (list(CLOSED_STATUSES),))
                for r in cur.fetchall():
                    first_day.setdefault(f"finding|{r['device_id']}|{r['rule_code']}", day)
                cur.execute("SELECT device_id, rule_code FROM observation")
                for r in cur.fetchall():
                    first_day.setdefault(f"observation|{r['device_id']}|{r['rule_code']}", day)
            if res.persist_errors or res.n_failed:
                print(f"  ⚠ {day}：失敗 {res.n_failed} 點、寫入失敗 {len(res.persist_errors)} 筆")
            if (n + 1) % 10 == 0:
                print(f"  … {day}（{n + 1}/{len(dates)}）")

        def q(sql):
            with conn.cursor() as cur:
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]

        out = {
            "meta": {"what": "合成資料逐日重放（每天的資料夾只含當天的列）；first_day 是每條規則第一次以『未結案事項』或『觀察項目』出現的重放日期",
                     "days": len(dates), "first_date": dates[0], "last_date": dates[-1]},
            "first_day": first_day,
            "findings": q("SELECT device_id, rule_code, severity, status, first_seen_at, last_seen_at, occurrence_count "
                          "FROM finding ORDER BY first_seen_at"),
            "observations": q("SELECT device_id, rule_code, first_seen_at, last_seen_at, occurrence_count "
                              "FROM observation ORDER BY first_seen_at"),
        }
    finally:
        conn.close()
        if not args.keep:
            subprocess.run(["dropdb", "--if-exists", args.dbname], env=T._psql_env(), capture_output=True)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "replay_synthetic.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=_jsonable), encoding="utf-8")
    for k, v in sorted(first_day.items(), key=lambda kv: (kv[0].split("|")[1], kv[1])):
        kind, dev, rule = k.split("|")
        print(f"  {v}  {dev:<12} {rule:<20} {'事項' if kind == 'finding' else '觀察'}")
    print(f"\n已寫入 {outdir}/replay_synthetic.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
