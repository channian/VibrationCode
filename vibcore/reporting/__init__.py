"""
vibcore.reporting — 週報渲染器

三支模組職責分明：

  collect.py   從資料庫收集本期資料，決定「新發現 / 追蹤中 / 已解決」三段式分類
  render.py    把 collect 的結果 + agent 的結構化評論排版成一份完整 HTML
  templates.py Jinja2 樣板字串與 CSS

**分工原則**（見 PLAN_agent_platform_refactor.md §七、§8.2）：
Agent 只負責產出 verdict / headline / actions / notes 這類「人話評論」，
三段式分類與涵蓋率統計完全由本模組依資料庫決定——這是刻意的設計，理由見
`collect.collect_weekly_data` 的 docstring。
"""

from vibcore.reporting.collect import collect_weekly_data
from vibcore.reporting.render import render_weekly_html

__all__ = ["collect_weekly_data", "render_weekly_html"]
