"""
device_parser.py — 從振動 CSV 檔名解析設備識別資訊

支援命名格式範例：
  冰水_ZP1_2_M1_Analytics_202601.csv
  ZP1_2_M2_Analytics_2026.csv
"""

import re
import os

# 匹配規則：可選的「冰水_」前綴，設備基底名稱，M1/M2 位置標記
_PATTERN = re.compile(r"(?:冰水_)?([A-Za-z0-9]+(?:_[A-Za-z0-9]+)*)_(M[12])_Analytics", re.IGNORECASE)

POSITION_LABELS = {
    'M1': '自由端 (Free End)',
    'M2': '驅動端 (Drive End)',
}


def parse_filename(filepath: str) -> tuple[str | None, str | None, str | None]:
    """
    從檔案路徑解析設備資訊。

    Returns:
        (device_base, position, device_id)
        如解析失敗則回傳 (None, None, None)

    Examples:
        >>> parse_filename('冰水_ZP1_2_M1_Analytics_202601.csv')
        ('ZP1_2', 'M1', 'ZP1_2_M1')
    """
    basename = os.path.splitext(os.path.basename(filepath))[0]
    m = _PATTERN.search(basename)
    if m:
        device_base = m.group(1)
        position = m.group(2).upper()
        device_id = f"{device_base}_{position}"
        return device_base, position, device_id
    return None, None, None


def get_position_label(position: str) -> str:
    """回傳位置的中文說明標籤。"""
    return POSITION_LABELS.get(position, position)
