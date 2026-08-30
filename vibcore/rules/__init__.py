"""
規則層。

匯入本套件時會一併載入各規則模組，讓 @register 裝飾器把規則掛進
engine.REGISTRY——呼叫端只需 `from vibcore.rules import REGISTRY, evaluate_all`，
不必逐一 import 各規則檔。
"""

from vibcore.rules.engine import (  # noqa: F401
    REGISTRY, RuleFunc, evaluate_all, outcome_to_finding, register,
)

# 匯入即註冊；缺任一模組時不讓整個套件失效，仍可跑其餘規則
for _mod in ('metric_rules', 'event_rules', 'temp_rules'):
    try:
        __import__(f'vibcore.rules.{_mod}')
    except ImportError:  # pragma: no cover - 開發期部分模組尚未完成
        import logging
        logging.getLogger(__name__).debug(f'規則模組 {_mod} 尚未就緒')
