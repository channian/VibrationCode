"""
iso.py — ISO 10816/20816 位準分級

為什麼需要這一層，而不是直接把 velRMS 丟給規則引擎比大小：

1. **分類本身可能是錯的。** `iso_machine_group` 與 `iso_foundation` 是
   工程師依 ISO 10816-3 手動填寫在設備台帳上的，沒有任何機制保證正確。
   Zone 判定完全建立在這組分類之上，一旦填錯，Zone 結論就會系統性地錯
   ——可能把真正異常的設備判成 Zone A（誤填成容許振動較大的組合），
   也可能把健康設備判成 Zone D。因此本模組在算出 Zone 之餘，
   **同時對分類本身做合理性檢查**（`is_class_suspect`），
   而不是天真地信任台帳。

2. **未分類不能硬套。** Zone 判定需要**群組與基礎剛性兩者**：同一個
   Group 2，剛性基礎的 A/B 界是 1.40、柔性是 2.30，差距接近一倍。
   任一項缺失即回傳 `applicable=False`，改用相對基準與趨勢類規則監測
   （見 `vibcore/metrics/deviation.py` 與趨勢規則）——猜一邊等於捏造依據。

3. **範圍外的設備不判定。** ISO 20816-3 適用於額定功率 > 15 kW、
   轉速 120–30000 rpm。低於或超出此範圍的設備即使填了分類也不套用
   Zone（見 `iso_scope_reason`）——標準沒有涵蓋的機器，套用它的門檻
   等於引用一個不存在的依據。

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


#: ISO 10816-3 / 20816-3 的 Zone 界限（velRMS，單位 mm/s），
#: 以 **(機器群組, 基礎剛性)** 為鍵。
#:
#: **2026-09 重大修正**：先前這張表用的是 Class I–IV（依 kW 與基礎剛性），
#: 那是 **ISO 2372 / ISO 10816-1** 的舊分類，與本系統引用的告警設定原則
#: （ISO 10816-3 §5.4.1）不是同一份文件。ISO 10816-3 的分類是
#: **Group 1–4 × rigid/flexible**，邊界值也不同——例如舊表 Class II 的
#: A/B 界為 1.12，而 Group 2 剛性基礎是 1.40、Group 3 剛性基礎是 2.30。
#: 用錯的表會讓 Zone 結論與 VEL_HIGH 門檻同時偏掉（門檻取自 B/C 界，
#: 舊表 2.80 vs Group 3 剛性 4.50，相差 60%）。
#:
#: 數值依使用者提供的 ISO 10816-3 評估分區圖重建。**上線前仍應由振動
#: 專家對照條文原文核對一次**——這張表是所有 Zone 判定與 VEL_HIGH 門檻的
#: 唯一數值來源，錯了不會有任何徵兆，只會安靜地全廠judgement 偏移。
#:
#: 邊界值歸屬「較低（較健康）」的一側——velRMS == ab 時仍判為 Zone A，
#: 超過才進入 Zone B。若貴單位慣例相反請在此統一調整，不要在呼叫端
#: 另行加減 epsilon。
ISO_THRESHOLDS: dict[tuple[str, str], dict] = {
    ('1', 'rigid'):    {'label': 'Group 1 大型機（300 kW–50 MW；馬達軸高 H ≥ 315 mm）· 剛性基礎',
                        'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
    ('1', 'flexible'): {'label': 'Group 1 大型機 · 柔性基礎',
                        'ab': 3.50, 'bc': 7.10, 'cd': 11.00},
    ('2', 'rigid'):    {'label': 'Group 2 中型機（15 kW < P ≤ 300 kW；馬達軸高 160 ≤ H < 315 mm）· 剛性基礎',
                        'ab': 1.40, 'bc': 2.80, 'cd': 4.50},
    ('2', 'flexible'): {'label': 'Group 2 中型機 · 柔性基礎',
                        'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
    ('3', 'rigid'):    {'label': 'Group 3 泵浦（> 15 kW，外接驅動）· 剛性基礎',
                        'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
    ('3', 'flexible'): {'label': 'Group 3 泵浦（外接驅動）· 柔性基礎',
                        'ab': 3.50, 'bc': 7.10, 'cd': 11.00},
    ('4', 'rigid'):    {'label': 'Group 4 泵浦（> 15 kW，整合驅動）· 剛性基礎',
                        'ab': 1.40, 'bc': 2.80, 'cd': 4.50},
    ('4', 'flexible'): {'label': 'Group 4 泵浦（整合驅動）· 柔性基礎',
                        'ab': 2.30, 'bc': 4.50, 'cd': 7.10},
}

#: 合法的群組與基礎剛性取值
ISO_GROUPS = ('1', '2', '3', '4')
ISO_FOUNDATIONS = ('rigid', 'flexible')

#: ISO 20816-3:2022 的適用範圍下限（額定功率，kW）。低於此值的設備
#: **不在標準適用範圍內**，不得套用 Zone 判定——對範圍外的機器硬套一組
#: 門檻，等於捏造一個標準沒有背書的結論。
ISO_MIN_RATED_POWER_KW = 15.0

#: 適用範圍的轉速區間（rpm）。超出範圍同樣不適用。
ISO_MIN_RPM = 120.0
ISO_MAX_RPM = 30000.0


def iso_scope_reason(device: DeviceContext) -> str | None:
    """
    設備是否落在 ISO 20816-3 的適用範圍內；不適用時回傳中文原因，適用回傳 None。

    額定功率或轉速為 None 時**視為通過**——台帳沒填不等於超出範圍，這裡
    只擋「明確知道超出範圍」的情況。缺資料造成的不確定由分級本身
    （`iso_machine_group` 未填即不套用）處理，不在這裡重複把關。
    """
    power = device.rated_power_kw
    if power is not None and not pd.isna(power) and float(power) <= ISO_MIN_RATED_POWER_KW:
        return (f'額定功率 {float(power):.1f} kW 未超過 {ISO_MIN_RATED_POWER_KW:.0f} kW，'
                '不在 ISO 20816-3 適用範圍內')
    rpm = device.rated_rpm
    if rpm is not None and not pd.isna(rpm):
        r = float(rpm)
        if r < ISO_MIN_RPM or r > ISO_MAX_RPM:
            return (f'額定轉速 {r:.0f} rpm 超出 ISO 20816-3 適用範圍 '
                    f'（{ISO_MIN_RPM:.0f}–{ISO_MAX_RPM:.0f} rpm）')
    return None


def resolve_class(device: DeviceContext) -> tuple[str, str] | None:
    """
    取得設備的 (群組, 基礎剛性) 鍵；任一項缺失或不合法即回傳 None。

    **基礎剛性缺失時回傳 None（等同未分級），不預設任何一邊。**
    ISO 10816-3 的 Zone 邊界同時取決於群組與基礎——同一個 Group 2，
    剛性基礎的 A/B 界是 1.40、柔性是 2.30，差距接近一倍。猜一個等於
    捏造依據，寧可退回相對基準與趨勢監測。
    """
    group = device.iso_machine_group
    foundation = device.iso_foundation
    if group is None or foundation is None:
        return None
    key = (str(group), str(foundation))
    return key if key in ISO_THRESHOLDS else None


_ZONE_ORDER = ('A', 'B', 'C', 'D')


def classify_zone(vel_rms: float | None, key: tuple[str, str] | None) -> str | None:
    """
    依 ISO 10816-3 門檻，把單一 velRMS 數值分到 Zone A/B/C/D。

    `key` 為 `(群組, 基礎剛性)`；未知組合或無效數值一律回傳 `None`，而不是
    猜一個等級硬套——呼叫端（尤其是 `evaluate_iso`）負責決定「無法分級」時
    該如何處置，這裡只做單純的數值對照，不夾帶業務判斷。
    """
    if key is None or key not in ISO_THRESHOLDS:
        return None
    if vel_rms is None:
        return None
    try:
        v = float(vel_rms)
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None

    th = ISO_THRESHOLDS[key]
    if v <= th['ab']:
        return 'A'
    if v <= th['bc']:
        return 'B'
    if v <= th['cd']:
        return 'C'
    return 'D'


def iso_alert_threshold(baseline_value: float | None,
                        key: tuple[str, str] | None) -> float | None:
    """
    依 **ISO 10816-3:2009 §5.4.1「Setting of ALARMS」** 把該量測點的基準值
    換算成錨定到 Zone 寬度的告警絕對值，供 `VEL_HIGH` 在
    `threshold_mode='iso'` 時使用。

    公式：
        ALARM = 基準值 + 0.25 × Zone B 上限（bc_boundary）
        且 ALARM ≤ 1.25 × Zone B 上限（封頂）

    條文要旨是「ALARM 應反映這一台機器在這一個量測位置／方向的正常振動
    基準，而不是只照抄固定表格值」，因此基準值須取自該設備穩態、正常運轉
    下的歷史量測（本系統以基準期中位數為之，見 metrics/baseline.py）。

    **出處與版本**：公式依 ISO 10816-3:2009 §5.4.1；該版已被
    ISO 20816-3:2022 取代，係數在新版是否維持相同尚未核對原文，
    上線前應由振動專家確認一次。程式與輸出文字不得寫成新版條文的確定引用。

    **TRIP 刻意不實作**：舊版建議 TRIP ≤ 1.25 × Zone C 上限，但該建議在
    ISO 20816-3 第二版草案中已被移除（6.5.3）。在一個正被撤除的建議上蓋
    保護邏輯不划算，且本系統定位是篩選預警，本來就不該連動停機保護。

    Args:
        baseline_value: 該量測點在基準期的代表值（呼叫端通常傳中位數）。
        key: `(群組, 基礎剛性)`；不在 `ISO_THRESHOLDS` 中（含 None，
            對應未分級或基礎剛性未填）一律回傳 None。

    Returns:
        告警門檻絕對值（與 `baseline_value` 同單位）；無法判定時回傳 None
        ——**不是** 0 或其他預設值，呼叫端必須自行決定「算不出門檻」時要
        不要退回其他判定方式（`VEL_HIGH` 的做法是退回 sigma 模式）。
    """
    if key is None or key not in ISO_THRESHOLDS:
        return None
    if baseline_value is None:
        return None
    try:
        b = float(baseline_value)
    except (TypeError, ValueError):
        return None
    if pd.isna(b):
        return None

    bc = ISO_THRESHOLDS[key]['bc']
    raw = b + 0.25 * bc
    cap = 1.25 * bc
    return min(raw, cap)


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


def _build_suspect_reason(key: tuple[str, str], baseline_median: float) -> str:
    """
    產生「分類疑似誤填」的中文說明：列出實測基準期中位數與各組合門檻的
    對照，讓工程師一眼看出目前指派的分類偏離多離譜，以及換哪個組合可能
    比較合理——而不是只丟一句「數值異常」。
    """
    th = ISO_THRESHOLDS[key]
    lines = [
        f"基準期 velRMS 中位數為 {baseline_median:.2f} mm/s，"
        f"已超過目前指派分類「{th['label']}」的 B/C 界（{th['bc']:.2f} mm/s）。",
        "健康運轉的機器，基準期中位數通常應落在 Zone A 或低 Zone B "
        f"（即 ≤ {th['bc']:.2f} mm/s，理想上接近 {th['ab']:.2f} mm/s 以下）。",
        "此結果代表兩種可能：機器本身已有振動問題，或群組／基礎剛性填寫錯誤"
        "（例如實際功率、驅動型式或基礎剛性與台帳登記不符）；兩者都需要人工"
        "複核，不應由系統自動判定。",
        "各分類門檻對照（velRMS, mm/s；A/B 界｜B/C 界｜C/D 界）：",
    ]
    for k, t in ISO_THRESHOLDS.items():
        marker = '← 目前指派' if k == key else ''
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
        device: 設備資訊，取其 `iso_machine_group` / `iso_foundation` /
                `iso_class_source`，以及適用範圍檢查用的額定功率與轉速。
        baseline: 基準期統計；用於「等級合理性檢查」，可為 None
                  （尚未建立基準期時，僅回傳 Zone 判定，不做合理性檢查）。

    Returns:
        IsoResult。未分級設備固定回傳 `applicable=False`、`zone=None`。
    """
    class_source = device.iso_class_source or 'unset'
    key = resolve_class(device)
    machine_class = '/'.join(key) if key else None

    # ── 適用範圍檢查：ISO 20816-3 只涵蓋 > 15 kW、120–30000 rpm ────
    scope_reason = iso_scope_reason(device)

    # ── 未分級 / 範圍外：一律不套用 Zone 判定 ──────────────────
    if class_source == 'unset' or key is None or scope_reason is not None:
        if scope_reason is not None:
            note = f'{scope_reason}，僅以相對基準與趨勢監測'
        elif key is None and class_source != 'unset':
            # 群組有填但基礎剛性沒填（或反之）是最容易被誤讀的狀況：
            # 台帳看起來「已分類」，實際上算不出 Zone。要講清楚缺什麼。
            note = ('群組或基礎剛性未完整填寫（兩者皆為 Zone 判定的必要條件），'
                    '僅以相對基準與趨勢監測')
        else:
            note = '未分級，僅以相對基準與趨勢監測'
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
            note=note,
        )

    th = ISO_THRESHOLDS[key]
    thresholds = {'ab': th['ab'], 'bc': th['bc'], 'cd': th['cd']}

    vel_rms = _latest_ok_vel_rms(agg)
    zone = classify_zone(vel_rms, key)

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
            suspect_reason = _build_suspect_reason(key, baseline_median)
            logger.warning(
                f"ISO 分類疑似誤填：{machine_class} "
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
