"""診斷腳本：比對振動 devicename 與 tag_mapping device_id 是否真的一致"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.data_loader import load_vibration, safe_read_csv

# 振動設備名稱
vib = load_vibration('Vibration_Data')
vib_names = sorted({df['devicename'].iloc[0] for df in vib.values()})

# tag_mapping device_id
tm = safe_read_csv('tag_mapping.csv')
tm.columns = [c.strip() for c in tm.columns]
# 找 device_id 欄（不分大小寫）
did_col = next((c for c in tm.columns if c.lower().replace(' ','_') == 'device_id'), None)
if did_col is None:
    print(f"找不到 device_id 欄，現有欄位：{list(tm.columns)}")
    sys.exit(1)
tm_ids = sorted(tm[did_col].astype(str).unique())

print("\n=== 振動 devicename (repr) ===")
for n in vib_names:
    print(f"  {repr(n)}")

print("\n=== tag_mapping device_id (repr) ===")
for n in tm_ids:
    print(f"  {repr(n)}")

print("\n=== 比對結果 ===")
for n in vib_names:
    matched = any(n == t.strip() for t in tm_ids)
    print(f"  {'✅' if matched else '❌'} {n}")
