"""
iso.py — ISO 10816/20816 位準分級

為什麼需要這一層，而不是直接把 velRMS 丟給規則引擎比大小：

1. **分級本身可能是錯的。** `iso_machine_class` 是工程師依 ISO 10816 手動
   填寫在設備台帳上的，沒有任何機制保證正確。Zone 判定完全建立在這個
   分類之上，一旦分類錯誤，Zone 結論就會系統性地錯——可能把真正異常的
   設備判成 Zone A（因為誤填成大馬力等級），也可能把健康設備判成 Zone D。
   因此本模組在算出 Zone 之餘，**同時對分類本身做合理性檢查**
   （`is_class_suspect`），而不是天真地信任台帳。

2. **未分級不能硬套等級。** 實測 `ISO10816_code` 目前多數為 0（尚未設定，
   見 PLAN §十二），若對這些設備仍套用某個預設等級的 Zone 門檻，等於是
   捏造一個沒有依據的判定，比不判定更容易誤導 agent 與工程師。所以
   `iso_class_source == 'unset'`（或 `machine_class is None`）時直接
   回傳 `applicable=False`，改用相對基準與趨勢類規則監測（見
   `vibcore/metrics/deviation.py` 與趨勢規則）。

3. **Zone 判定只是眾多證據之一，不是唯一依據。** 計畫書 §十二 特別指出
   AHU 樣本 velRMS 僅 1.51 mm/s（落於 Zone A/B）但 accCREST=16、
   accKURT=68.6 明顯異常——`ISO_ZONE` 規則必須與 `IMPACT_RISE` 等規則
   並行，本模組只負責 ISO 這一部分的證據，不宣稱涵蓋全部異常。

只使用 `data_status == 'ok'` 的列：`partial`/`no_data` 的數字不可信，
`not_running` 是正常狀態不代表機器本身的振動水準。
"""

import logging
from datetime import date

import pandas as pd

from vibcore.types import BaselineStats, DeviceContext, IsoResult

logger = logging.getLogger(__name__)


#: ISO 10816/20816 各機械等級的 Zone 界限（velRMS，單位 mm/s）。
#: 數值取自 db/schema.sql 的 `iso_threshold` seed 資料
#: （亦見計畫書 §十二）。
#:
#: 邊界值歸屬「較低（較健康）」的一側——例如 Class I 的 ab_boundary
#: 為 0.71，代表 velRMS == 0.71 時仍判為 Zone A，超過才進入 Zone B。
#: 這是工程上常見的取法（門檻本身仍在可接受範圍內），若貴單位慣例相反
#: 請在此處統一調整，不要在呼叫端另行加減 epsilon。
ISO_THRESHOLDS: dict[str, dict[str, float]] = {
    'I':   {'label': 'Class I（< 15 kW）',          'ab': 0.71, 'bc': 1.80,  'cd': 4.50},
    'II':  {'label': 'Class II（15–75 kW）',        'ab': 1.12, 'bc': 2.80,  'cd': 7.10},
    'III': {'label': 'Class III（大型剛性基礎）',    'ab': 1.80, 'bc': 4.50,  'cd': 11.20},
    'IV':  {'label': 'Class IV（大型柔性基礎）',     'ab': 2.80, 'bc': 7.10,  'cd': 18.00},
}

_ZONE_ORDER = ('A', 'B', 'C', 'D')


def classify_zone(vel_rms: float | None, machine_class: str | None) -> str | None:
    """
    依 ISO 10816/20816 門檻，把單一 velRMS 數值分到 Zone A/B/C/D。

    未知等級或無效數值一律回傳 `None`，而不是猜一個等級硬套——呼叫端
    （尤其是 `evaluate_iso`）負責決定「無法分級」時該如何處置，這裡只
    做單純的數值對照，不夾帶業務判斷。
    """
    if machine_class not in ISO_THRESHOLDS:
        return None
    if vel_rms is None:
        return None
    try:
        v = float(vel_rms)
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None

    th = ISO_THRESHOLDS[machine_class]
    if v <= th['ab']:
        return 'A'
    if v <= th['bc']:
        return 'B'
    if v <= th['cd']:
        return 'C'
    return 'D'


def _latest_ok_vel_rms(agg: pd.DataFrame) -> float | None:
    """取最近一筆 `data_status == 'ok'` 的 velRMS，作為「目前」水準。"""
    if agg is None or agg.empty or 'vel_rms' not in agg.columns:
        return None
    ok = agg[agg.get('data_status') == 'ok'] if 'data_status' in agg.columns else agg
    ok = ok.dropna(subset=['vel_rms'])
    if ok.empty:
        return None
    if 'ts_hour' in ok.columns:
        ok = ok.sort_values('ts_hour')
    return float(ok['vel_rms'].iloc[-1])


def _build_suspect_reason(machine_class: str, baseline_median: float) -> str:
    """
    產生「等級疑似誤填」的中文說明：列出實測基準期中位數與各等級門檻的
    對照，讓工程師一眼看出目前指派的等級偏離多離譜，以及換哪個等級可能
    比較合理——而不是只丟一句「數值異常」。
    """
    th = ISO_THRESHOLDS[machine_class]
    lines = [
        f"基準期 velRMS 中位數為 {baseline_median:.2f} mm/s，"
        f"已超過目前指派等級「{th['label']}」的 B/C 界（{th['bc']:.2f} mm/s）。",
        "健康運轉的機器，基準期中位數通常應落在 Zone A 或低 Zone B "
        f"（即 ≤ {th['bc']:.2f} mm/s，理想上接近 {th['ab']:.2f} mm/s 以下）。",
        "此結果代表兩種可能：機器本身已有振動問題，或 ISO 等級填寫錯誤"
        "（例如實際馬力/基礎剛性與台帳登記不符）；兩者都需要人工複核，"
        "不應由系統自動判定。",
        "各等級門檻對照（velRMS, mm/s；A/B 界｜B/C 界｜C/D 界）：",
    ]
    for cls, t in ISO_THRESHOLDS.items():
        marker = '← 目前指派' if cls == machine_class else ''
        lines.append(
            f"  {t['label']}：{t['ab']:.2f}｜{t['bc']:.2f}｜{t['cd']:.2f} {marker}"
        )
    return '\n'.join(lines)


def evaluate_iso(agg: pd.DataFrame,
                  device: DeviceContext,
                  baseline: BaselineStats | None) -> IsoResult:
    """
    對單一量測點執行 ISO 10816/20816 位準分級。

    Args:
        agg: 該量測點的每小時聚合結果（含 `data_status` 與 `vel_rms`）。
        device: 設備資訊，取其 `iso_machine_class` / `iso_class_source`。
        baseline: 基準期統計；用於「等級合理性檢查」，可為 None
                  （尚未建立基準期時，僅回傳 Zone 判定，不做合理性檢查）。

    Returns:
        IsoResult。未分級設備固定回傳 `applicable=False`、`zone=None`。
    """
    machine_class = device.iso_machine_class
    class_source = device.iso_class_source or 'unset'

    # ── 未分級：不套用 Zone 判定 ──────────────────────────────
    if class_source == 'unset' or machine_class is None or machine_class not in ISO_THRESHOLDS:
        vel_rms = _latest_ok_vel_rms(agg)
        return IsoResult(
            applicable=False,
            machine_class=machine_class,
            class_source=class_source,
            zone=None,
            vel_rms=vel_rms,
            thresholds={},
            is_class_suspect=False,
            suspect_reason='',
            note='未分級，僅以相對基準與趨勢監測',
        )

    th = ISO_THRESHOLDS[machine_class]
    thresholds = {'ab': th['ab'], 'bc': th['bc'], 'cd': th['cd']}

    vel_rms = _latest_ok_vel_rms(agg)
    zone = classify_zone(vel_rms, machine_class)

    note = ''
    if vel_rms is None:
        note = '無可用（data_status == ok）的 velRMS 資料，無法判定 Zone'

    # ── 等級合理性檢查：基準期中位數是否已超過 B/C 界 ──────────
    is_class_suspect = False
    suspect_reason = ''
    if baseline is not None and 'vel_rms' in baseline.stats:
        baseline_median = baseline.stats['vel_rms'].median
        if baseline_median is not None and not pd.isna(baseline_median) \
                and baseline_median > th['bc']:
            is_class_suspect = True
            suspect_reason = _build_suspect_reason(machine_class, baseline_median)
            logger.warning(
                f"ISO 等級疑似誤填：machine_class={machine_class} "
                f"baseline_median={baseline_median:.2f} > bc={th['bc']:.2f}"
            )

    return IsoResult(
        applicable=True,
        machine_class=machine_class,
        class_source=class_source,
        zone=zone,
        vel_rms=vel_rms,
        thresholds=thresholds,
        is_class_suspect=is_class_suspect,
        suspect_reason=suspect_reason,
        note=note,
    )
