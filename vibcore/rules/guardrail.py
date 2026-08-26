"""
guardrail.py — 診斷性用語檢查

本系統定位是**篩選預警**而非診斷（見計畫書 §一），因此規則產出的文字
與 Agent 撰寫的評論都不得出現故障類型的**斷言**。

## 為什麼不能用關鍵字黑名單

最直覺的做法是禁掉「軸承」「對心」這類詞，但那會同時擋掉兩種完全不同
的句子：

    ✗ 「軸承內環缺陷」                              ← 斷言，必須擋
    ✓ 「常見於軸承或潤滑劣化，本系統無法區分成因」  ← 有免責的可能性列舉，應該允許

第二種對現場工程師更有價值——它給了明確的查驗方向，同時誠實說明系統的
能力邊界。把它一起擋掉，文字只能退化成「機件表面狀態改變」這種沒有指向
性的說法，反而違背了「讓非振動專業者也能判讀處理」的目的。

因此本模組檢查的是**句式**：出現故障詞彙時，同一句中是否具備免責語。
"""

from __future__ import annotations

import re

#: 故障類型詞彙。單獨出現不必然違規，須看是否具備免責語。
FAULT_TERMS = (
    '軸承', '對心', '不平衡', '鬆動', '氣蝕', '共振',
    '皮帶', '聯軸器', '齒輪', '轉子', '基礎',
)

#: 斷言句式——只要出現就是違規，不論有沒有免責語。
ASSERTIVE_PATTERNS = (
    r'疑似[^，。；]{0,8}(?:' + '|'.join(FAULT_TERMS) + r')',
    r'(?:' + '|'.join(FAULT_TERMS) + r')[^，。；]{0,4}(?:缺陷|損壞|失效|故障|破損)',
    r'(?:研判|判定|確認|判斷)為',
    r'建議(?:立即)?更換',
    r'剩餘(?:可用)?壽命[^，。；]{0,6}(?:約|為|還有)',
    r'(?:即將|將於)[^，。；]{0,10}(?:失效|損壞|故障)',
)

#: 免責語——出現故障詞彙時，需要其中至少一個同時存在。
HEDGE_MARKERS = (
    '無法區分', '不代表', '不判定', '不是成因', '非成因',
    '常見成因', '常見於', '可能源自', '可能來自', '可能為',
    '等多種原因', '仍需', '建議安排', '建議實地', '複測', '確認成因',
)


def check_text(text: str) -> list[str]:
    """
    檢查一段文字是否含有故障類型斷言。

    Returns:
        違規說明清單；空清單代表通過。
    """
    if not text:
        return []

    problems: list[str] = []

    for pattern in ASSERTIVE_PATTERNS:
        for m in re.finditer(pattern, text):
            problems.append(f'斷言句式：「{m.group(0)}」')

    # 故障詞彙需與免責語同時出現
    used = [t for t in FAULT_TERMS if t in text]
    if used and not any(h in text for h in HEDGE_MARKERS):
        problems.append(
            f'提及 {"、".join(used)} 但缺少免責語'
            f'（需說明本系統無法區分成因，或標示為可能成因之一）'
        )

    return problems


def check_outcome(outcome) -> list[str]:
    """檢查一筆 RuleOutcome 的所有對外文字。"""
    problems: list[str] = []
    for field in ('title', 'detail', 'interpretation_limit'):
        text = getattr(outcome, field, '') or ''
        for p in check_text(text):
            problems.append(f'{field}: {p}')
    return problems


def assert_clean(outcome) -> None:
    """測試用：文字含診斷性斷言時直接拋錯。"""
    problems = check_outcome(outcome)
    if problems:
        raise AssertionError(
            f'規則 {getattr(outcome, "rule_code", "?")} 的文字含診斷性斷言：\n  '
            + '\n  '.join(problems)
        )
