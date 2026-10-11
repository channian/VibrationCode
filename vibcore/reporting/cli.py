"""
不經 Agent 產生週報／日報（試跑用）。

週報主體本來就由資料庫產生（`collect_weekly_data` → `render_weekly_html`），Agent 只補一段導讀。
這支入口把 Agent 那一步拿掉：版面上每一個數字與每一段文字都來自資料庫與規則引擎，
沒有任何模型產生的內容，所以 Agent 護欄（數字白名單、診斷用語檢查套用於 Agent 文字、
總評下限）尚未完成也不影響它。要加上 Agent 導讀，由 Agent 平台呼叫 `send_report`
即可——同一期間會覆蓋這份（`weekly_report` 以期間為唯一鍵）。

    python -m vibcore.reporting.cli --days 7 --out output/weekly.html
    python -m vibcore.reporting.cli --days 7 --end-date 2026-10-11 --out w41.html --save

`--save` 同時寫入 `weekly_report` 並記一筆稽核（`render_report`，操作者 system）。
稽核動作刻意不用 `send_report`：每日寄送上限只計 Agent 的送出，系統自己排程出報告
不該吃掉 Agent 的額度。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from vibcore.api import queries
from vibcore.api.service import report_period_label, resolve_period
from vibcore.reporting.collect import ReportScope, collect_weekly_data
from vibcore.reporting.render import (
    _REPORT_LABELS, _fallback_headline, _infer_verdict, render_weekly_html,
)

logger = logging.getLogger(__name__)

SYSTEM_SOURCE_NOTE = '由系統依資料庫彙整，未附 Agent 導讀'


def build_report(conn, days: int = 7, end_date: date | None = None,
                 report_type: str = 'weekly', scope: ReportScope | None = None) -> dict:
    """收集資料並排版；只讀資料庫，不寫入任何東西。"""
    period_start, period_end = resolve_period(days, end_date=end_date)
    data = collect_weekly_data(conn, period_start, period_end, scope)
    verdict = _infer_verdict(data)
    labels = _REPORT_LABELS.get(report_type) or _REPORT_LABELS['weekly']
    return {
        'html': render_weekly_html(data, None, report_type),
        'verdict': verdict,
        'headline': _fallback_headline(data, labels['period_word']),
        'report_type': report_type,
        'period_label': report_period_label(report_type, period_end),
        'period_start': period_start,
        'period_end': period_end,
        'stats': data['stats'],
        'scope': data.get('scope') or {},
    }


def save_report(conn, rep: dict) -> dict:
    """寫入 `weekly_report` 並記稽核。同一期間重跑會覆蓋（含 Agent 先前送出的）。"""
    s = rep['stats']
    row = queries.upsert_weekly_report(
        conn, report_type=rep['report_type'], period_label=rep['period_label'],
        period_start=rep['period_start'], period_end=rep['period_end'],
        verdict=rep['verdict'], headline=rep['headline'],
        agent_payload={'source': 'system', 'note': SYSTEM_SOURCE_NOTE,
                       'verdict': rep['verdict'], 'headline': rep['headline'],
                       'actions': [], 'notes': None},
        html=rep['html'], new_count=s.get('new_count', 0),
        tracking_count=s['tracking_count'], resolved_count=s['resolved_count'],
    )
    queries.insert_audit_log(
        conn, actor='system', action='render_report',
        target=f"{rep['report_type']}:{rep['period_label']}",
        detail={'report_id': row['report_id'], 'verdict': rep['verdict'],
                'source': 'system', 'scope': rep['scope']},
    )
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='不經 Agent，由資料庫直接產生週報／日報 HTML')
    ap.add_argument('--days', type=int, default=7, help='涵蓋天數（含結束日），預設 7')
    ap.add_argument('--end-date', type=date.fromisoformat, default=None,
                    help='結束日 YYYY-MM-DD（預設今天，UTC）；補產歷史報告用')
    ap.add_argument('--type', dest='report_type', choices=['weekly', 'daily'], default='weekly')
    ap.add_argument('--building', default=None)
    ap.add_argument('--floor', default=None)
    ap.add_argument('--system', dest='system_name', default=None)
    ap.add_argument('--out', required=True, help='輸出的 HTML 檔')
    ap.add_argument('--save', action='store_true', help='同時寫入 weekly_report（並記稽核）')
    ap.add_argument('--log-level', default='WARNING', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format='%(levelname)s %(message)s')

    scope = ReportScope(building=args.building, floor=args.floor, system_name=args.system_name)
    from vibcore.db.connection import get_connection
    with get_connection() as conn:
        rep = build_report(conn, args.days, args.end_date, args.report_type, scope)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rep['html'], encoding='utf-8')
        if args.save:
            save_report(conn, rep)      # 交易由 get_connection 的 with 區塊結束時提交

    s = rep['stats']
    print(f"{rep['period_label']}（{rep['period_start']} ~ {rep['period_end']}）"
          f"總評 {rep['verdict']}：嚴重 {s['err_count']}、需關注 {s['warn_count']}、"
          f"追蹤中 {s['tracking_count']}、已解決 {s['resolved_count']}、"
          f"觀察名單 {s.get('new_observation_count', 0) + s.get('tracking_observation_count', 0)} 項")
    print(f"已寫入 {out}" + ('，並存入 weekly_report' if args.save else '（未存入資料庫；加 --save 才會）'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
