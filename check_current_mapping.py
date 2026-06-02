"""
check_current_mapping.py — 診斷「為何電流對應不上」

逐台檢查整條對應鏈：
  1. 振動 devicename 是否在 tag_mapping 的 device_id 中
  2. 該設備的 tagname 是否真的出現在 Other_Data
  3. 該設備有哪些 variable_type，是否包含電流
  4. 振動與 SCADA 時間是否重疊

執行：python check_current_mapping.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
logging.disable(logging.INFO)   # 安靜一點

from src.data_loader import load_vibration
from src.scada_loader import load_other_data, load_tag_mapping

CURRENT_KEYWORDS = ('電流', 'current', '安培', 'amp')

vib = load_vibration('Vibration_Data')
other = load_other_data('Other_Data')
tm = load_tag_mapping('tag_mapping.csv')

print(f"\n振動設備：{len(vib)} 台")
print(f"Other_Data：{len(other):,} 筆，{other['tagname'].nunique()} 個 tagname")
print(f"tag_mapping：{len(tm)} 列，{tm['device_id'].nunique()} 個 device_id")

# Other_Data 實際存在的 tagname 集合
other_tags = set(other['tagname'].unique())

# tag_mapping 的 variable_type 全貌
print("\n=== tag_mapping 出現的所有 variable_type ===")
for vt in sorted(tm['variable_type'].unique()):
    is_curr = any(k.lower() in vt.lower() for k in CURRENT_KEYWORDS)
    print(f"  {'⚡' if is_curr else '  '} {repr(vt)}")

print("\n=== 逐台對應鏈 ===")
print(f"{'設備':<16} {'①device一致':<11} {'②tag在Other':<13} {'③有電流type':<12} {'④時間重疊'}")
print("─" * 70)

for device_id, df_raw in sorted(vib.items()):
    device_base = df_raw['devicename'].iloc[0]

    # ① device_id 一致
    dev_tags = tm[tm['device_id'] == device_base]
    s1 = '✅' if not dev_tags.empty else f'❌({device_base})'
    if dev_tags.empty:
        print(f"{device_id:<16} {s1}")
        continue

    # ② tagname 是否在 Other_Data
    tagnames = dev_tags['tagname'].tolist()
    found = [t for t in tagnames if t in other_tags]
    s2 = f'✅{len(found)}/{len(tagnames)}' if found else f'❌0/{len(tagnames)}'

    # ③ 是否有電流 variable_type
    vtypes = dev_tags['variable_type'].tolist()
    curr_vt = [v for v in vtypes if any(k.lower() in v.lower() for k in CURRENT_KEYWORDS)]
    s3 = f'✅{repr(curr_vt[0])}' if curr_vt else '❌'

    # ④ 時間重疊（只在 tag 找得到時才算）
    s4 = '—'
    if found:
        df_dev = other[other['tagname'].isin(found)]
        if not df_dev.empty:
            vmin, vmax = df_raw['datetime'].min(), df_raw['datetime'].max()
            smin, smax = df_dev['datetime'].min(), df_dev['datetime'].max()
            overlap = vmin <= smax and smin <= vmax
            s4 = '✅' if overlap else f'❌(振{vmin.date()}~{vmax.date()} / SCADA{smin.date()}~{smax.date()})'

    print(f"{device_id:<16} {s1:<11} {s2:<13} {s3:<12} {s4}")

# 額外提示：tag_mapping 裡有、但 Other_Data 沒有的 tagname
mapped_tags = set(tm['tagname'].unique())
missing_in_other = mapped_tags - other_tags
if missing_in_other:
    print(f"\n⚠ tag_mapping 有列、但 Other_Data 找不到的 tagname（前 20 個）：")
    for t in sorted(missing_in_other)[:20]:
        print(f"    {repr(t)}")

# 額外提示：Other_Data 有、但 tag_mapping 沒對應的 tagname
unmapped = other_tags - mapped_tags
if unmapped:
    print(f"\n⚠ Other_Data 有、但 tag_mapping 沒對應的 tagname（前 20 個）：")
    for t in sorted(unmapped)[:20]:
        print(f"    {repr(t)}")
