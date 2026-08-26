"""
util.py — API 回傳值的型別正規化

`psycopg2`（`RealDictCursor`）與 `pandas` 回傳的型別（`Decimal`、
`datetime`/`date`、`pandas.Timestamp`、`numpy` 純量、`NaN`/`NaT`）在組進
巢狀 dict 後，FastAPI 預設的 JSON 編碼器不一定能正確處理每一種組合
（尤其是塞在自訂 dict 裡的 numpy 純量）。`jsonable()` 遞迴地把這些型別
轉成 JSON 原生型別，並把所有「缺值」統一轉成 `None`——見
`vibcore/db/repository.py` 的 `_clean_value()`，這裡是同一個理由在 API
回傳方向上的對應。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd


def jsonable(value: Any) -> Any:
    """把任意值遞迴轉成 JSON 安全的原生型別；NaN/NaT/None 一律回傳 None。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        v = float(value)
        return None if math.isnan(v) else v
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, bool, int)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value
