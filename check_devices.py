"""暫時診斷腳本：印出所有振動設備的 devicename（= tag_mapping 的 device_id 欄位應填的值）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.data_loader import load_vibration

vib = load_vibration('Vibration_Data')
names = sorted({df['devicename'].iloc[0] for df in vib.values()})
print("\n=== tag_mapping.csv 的 device_id 欄位應填以下名稱 ===")
for n in names:
    print(f"  {n}")
