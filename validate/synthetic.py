"""
synthetic.py — 合成多設備多天測試資料

**用途**：`data/Analytic.csv` 只有 4 分鐘的真實資料，跑得通框架但驗證不了
「涵蓋率統計對不對」「缺口清單對不對」「觸發密度算得對不對」「門檻掃描
真的會隨門檻變化」——這些都需要跨天、跨設備、且已知答案的資料才能檢查。
這個檔案就是為此而生：造 5 台「劇本」不同的假設備，每台的異常都是刻意
安排、答案已知，跑完回測後可以直接核對數字對不對。

五台設備的劇本（`--out-dir` 底下對應同名 CSV）：

  DEV-STABLE     30 天全程正常，無缺口 → 應該幾乎不觸發任何規則（負控制組，
                 驗證 stub 的門檻不會對正常資料本身噴警報）
  DEV-GAP        30 天，中間有一段 12 小時、一段 50 小時的斷線
                 → 驗證 `summarize_gaps` 抓得到兩段、時長正確；
                   50 小時的那段應觸發 `SENSOR_OFFLINE`（門檻 24 小時）
  DEV-STANDBY    55 天備機：前 20 天每天僅運轉 1 小時，之後完全不運轉
                 → 驗證 `not_running` 判定、`STANDBY_NO_RUNTIME`
                   （閒置 ≥30 天）於第 20+30=50 天後開始觸發
  DEV-DEGRADE    45 天：前 14 天穩定（供基準期使用），之後
                 accCREST/accKURT/velOA/accRMS 線性劣化，並指派
                 ISO Class I → 應觸發 `IMPACT_RISE`／`DEGRADE_TREND`／
                 `VEL_HIGH`／`STEP_CHANGE`／`ISO_ZONE`，且**不**觸發
                 `ISO_CLASS_SUSPECT`（基準期本身仍健康，等級沒有填錯，
                 純粹是後來真的劣化——兩者訊號來源不同，這是關鍵區辨）
  DEV-ORIENT     30 天：前 20 天三軸能量分佈固定「x 軸為主」，第 20 天起
                 瞬間切成「z 軸為主」（模擬感測器重貼）→ 應觸發
                 `ORIENTATION_CHANGE`（即時 vs 基準）與稍後的
                 `AXIS_SHIFT`（trailing 7 天平均 vs 基準）

執行方式：
    python -m validate.synthetic --out-dir /tmp/vib_synth
輸出：`{out-dir}/*.csv`（每台設備一個檔）+ `{out-dir}/device_meta.json`
（標記 DEV-STANDBY 為備機、DEV-DEGRADE 的 ISO 等級），可直接餵給
`validate/offline.py` 的 `--data-dir` 與 `--device-meta`。
"""

from __future__ import annotations

import argparse
import json
import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_FREQ_MIN = 1  # 每分鐘一筆（真實前端是每秒一筆；用分鐘級足以驗證邏輯又不會產出上百萬列）
_START = pd.Timestamp('2026-05-01 00:00:00')
_META_BASE = dict(Building='FAB7', Floor='6F', System='空調', Ball=0, Vane=0, Gear=0,
                  Model_HealthScore=0, Model_FailureMode='')


def _base_columns(n: int, name: str, rpm: float, iso_code: int,
                   channel: tuple[int, int, int] = (4, 6, 5)) -> dict:
    return {
        'Name': name, 'Label': np.nan, 'RPM': rpm, 'FMF': round(rpm / 60, 2),
        'ISO10816_code': iso_code,
        'Channel_X': channel[0], 'Channel_Y': channel[1], 'Channel_Z': channel[2],
        **_META_BASE,
    }


def _axis_split(total_rms: np.ndarray, ratios: tuple[float, float, float], rng: np.random.Generator) -> tuple:
    """把合成的總 RMS 依 (x,y,z) 能量佔比拆開，外加一點雜訊避免三軸完全等比例。"""
    noise = rng.normal(1.0, 0.03, size=(3, len(total_rms)))
    energy_total = total_rms ** 2
    x = np.sqrt(energy_total * ratios[0]) * noise[0]
    y = np.sqrt(energy_total * ratios[1]) * noise[1]
    z = np.sqrt(energy_total * ratios[2]) * noise[2]
    return x, y, z


def _make_device_df(name: str, n_days: int, rng: np.random.Generator,
                     vel_oa_fn, acc_rms_fn, acc_crest_fn, acc_kurt_fn,
                     running_fn, axis_ratio_fn,
                     rpm: float = 1710.0, iso_code: int = 0,
                     gap_windows: list[tuple[pd.Timestamp, pd.Timestamp]] | None = None) -> pd.DataFrame:
    """
    共用的設備資料產生器；各劇本只需傳入「隨時間變化」的小函式。

    每個 `*_fn(t_days: np.ndarray) -> np.ndarray` 吃「距起始日的天數」，
    回傳對應長度的數值陣列，讓劇本（穩定/劣化/斷線…）用簡單的算式表達，
    不必自己重寫整個資料框架的組裝邏輯。
    """
    idx = pd.date_range(_START, periods=n_days * 24 * 60 // _FREQ_MIN, freq=f'{_FREQ_MIN}min')
    t_days = (idx - _START).total_seconds().to_numpy() / 86400.0
    n = len(idx)

    running = running_fn(t_days)
    vel_oa = vel_oa_fn(t_days)
    acc_rms = acc_rms_fn(t_days)
    acc_crest = acc_crest_fn(t_days)
    acc_kurt = acc_kurt_fn(t_days)

    # 未運轉時振動應趨近於量測噪音水準，而非延續運轉中的數值——
    # 否則 `mark_running`（見 aggregate.py）判定失準，not_running 測不出來。
    idle_mask = ~running
    vel_oa = np.where(idle_mask, rng.normal(0.02, 0.005, n).clip(min=0), vel_oa)
    vel_rms = vel_oa * rng.normal(1.0, 0.02, n)
    acc_rms = np.where(idle_mask, rng.normal(0.05, 0.01, n).clip(min=0), acc_rms)
    acc_crest = np.where(idle_mask, rng.normal(3.0, 0.1, n), acc_crest)
    acc_kurt = np.where(idle_mask, rng.normal(3.0, 0.1, n), acc_kurt)

    acc_peak = acc_rms * acc_crest
    acc_skew = rng.normal(0.1, 0.05, n)
    vel_peak = vel_rms * rng.normal(3.0, 0.1, n)
    disp_rms = vel_rms * rng.normal(0.02, 0.002, n)
    disp_p2p = disp_rms * rng.normal(3.0, 0.1, n)
    acc_oa = acc_rms * rng.normal(500.0, 5.0, n)  # 沿用計畫書提到的量級落差，非精確反推
    fmf = round(rpm / 60, 2)
    acc_mean_peak_freq = np.full(n, fmf) + rng.normal(0, 0.5, n)
    acc_weighted_mean_freq = np.full(n, fmf * 2.2) + rng.normal(0, 1.0, n)
    acc_top1_freq = acc_mean_peak_freq.copy()
    acc_top1_amp = acc_rms * rng.normal(0.8, 0.05, n)
    vel_weighted_mean_freq = np.full(n, fmf * 1.5) + rng.normal(0, 0.5, n)

    ratios = axis_ratio_fn(t_days)  # (n,3)
    ax, ay, az = _axis_split(acc_rms, (1, 1, 1), rng)  # 佔位，下方立即依 ratios 覆寫
    energy_total = acc_rms ** 2
    noise = rng.normal(1.0, 0.02, size=(3, n))
    ax = np.sqrt(np.clip(energy_total * ratios[:, 0], 0, None)) * noise[0]
    ay = np.sqrt(np.clip(energy_total * ratios[:, 1], 0, None)) * noise[1]
    az = np.sqrt(np.clip(energy_total * ratios[:, 2], 0, None)) * noise[2]

    vel_rms_x, vel_rms_y, vel_rms_z = vel_rms / 1.8, vel_rms / 1.9, vel_rms / 2.1

    base = _base_columns(n, name, rpm, iso_code)
    df = pd.DataFrame({
        'Time': idx.strftime('%Y/%m/%d %H:%M'),
        **{k: v for k, v in base.items()},
        'velRMS_x': vel_rms_x, 'velRMS_y': vel_rms_y, 'velRMS_z': vel_rms_z, 'velRMS': vel_rms,
        'velPEAK_x': vel_peak, 'velPEAK_y': vel_peak, 'velPEAK_z': vel_peak, 'velPEAK': vel_peak,
        'velOA_x': vel_oa, 'velOA_y': vel_oa, 'velOA_z': vel_oa, 'velOA': vel_oa,
        'accRMS_x': ax, 'accRMS_y': ay, 'accRMS_z': az, 'accRMS': acc_rms,
        'accPEAK_x': acc_peak, 'accPEAK_y': acc_peak, 'accPEAK_z': acc_peak, 'accPEAK': acc_peak,
        'accCREST_x': acc_crest, 'accCREST_y': acc_crest, 'accCREST_z': acc_crest, 'accCREST': acc_crest,
        'accSKEW_x': acc_skew, 'accSKEW_y': acc_skew, 'accSKEW_z': acc_skew, 'accSKEW': acc_skew,
        'accKURT_x': acc_kurt, 'accKURT_y': acc_kurt, 'accKURT_z': acc_kurt, 'accKURT': acc_kurt,
        'dispRMS_x': disp_rms, 'dispRMS_y': disp_rms, 'dispRMS_z': disp_rms, 'dispRMS': disp_rms,
        'dispP2P_x': disp_p2p, 'dispP2P_y': disp_p2p, 'dispP2P_z': disp_p2p, 'dispP2P': disp_p2p,
        'accOA_x': acc_oa, 'accOA_y': acc_oa, 'accOA_z': acc_oa, 'accOA': acc_oa,
        'accMeanPeakFreq': acc_mean_peak_freq, 'accWeightedMeanFreq': acc_weighted_mean_freq,
        'accTOP1FREQ': acc_top1_freq, 'accTOP1FREQ_V': acc_top1_amp,
        'velWeightedMeanFreq': vel_weighted_mean_freq,
    })
    df.insert(0, 'datetime_helper', idx)  # 供下方挖缺口用，寫檔前會丟棄

    if gap_windows:
        keep = pd.Series(True, index=df.index)
        for start, end in gap_windows:
            keep &= ~((df['datetime_helper'] >= start) & (df['datetime_helper'] < end))
        df = df[keep].reset_index(drop=True)

    return df.drop(columns=['datetime_helper'])


def _dev_stable(rng: np.random.Generator) -> pd.DataFrame:
    n_days = 30
    return _make_device_df(
        'DEV-STABLE', n_days, rng,
        vel_oa_fn=lambda t: np.full_like(t, 0.6) + rng.normal(0, 0.03, len(t)),
        acc_rms_fn=lambda t: np.full_like(t, 0.4) + rng.normal(0, 0.02, len(t)),
        acc_crest_fn=lambda t: np.full_like(t, 3.2) + rng.normal(0, 0.15, len(t)),
        acc_kurt_fn=lambda t: np.full_like(t, 3.1) + rng.normal(0, 0.15, len(t)),
        running_fn=lambda t: np.full_like(t, True, dtype=bool),
        axis_ratio_fn=lambda t: np.tile([0.6, 0.25, 0.15], (len(t), 1)),
    )


def _dev_gap() -> pd.DataFrame:
    rng = np.random.default_rng(2)
    n_days = 30
    gaps = [
        (_START + pd.Timedelta(days=5), _START + pd.Timedelta(days=5, hours=12)),   # 12 小時
        (_START + pd.Timedelta(days=15), _START + pd.Timedelta(days=17, hours=2)),  # 50 小時
    ]
    return _make_device_df(
        'DEV-GAP', n_days, rng,
        vel_oa_fn=lambda t: np.full_like(t, 0.7) + rng.normal(0, 0.03, len(t)),
        acc_rms_fn=lambda t: np.full_like(t, 0.45) + rng.normal(0, 0.02, len(t)),
        acc_crest_fn=lambda t: np.full_like(t, 3.3) + rng.normal(0, 0.15, len(t)),
        acc_kurt_fn=lambda t: np.full_like(t, 3.2) + rng.normal(0, 0.15, len(t)),
        running_fn=lambda t: np.full_like(t, True, dtype=bool),
        axis_ratio_fn=lambda t: np.tile([0.55, 0.3, 0.15], (len(t), 1)),
        gap_windows=gaps,
    )


def _dev_standby() -> pd.DataFrame:
    rng = np.random.default_rng(3)
    n_days = 55

    def running_fn(t: np.ndarray) -> np.ndarray:
        hour_of_day = (t % 1.0) * 24
        return (t < 20) & (hour_of_day >= 8) & (hour_of_day < 9)

    return _make_device_df(
        'DEV-STANDBY', n_days, rng,
        vel_oa_fn=lambda t: np.full_like(t, 0.5) + rng.normal(0, 0.03, len(t)),
        acc_rms_fn=lambda t: np.full_like(t, 0.35) + rng.normal(0, 0.02, len(t)),
        acc_crest_fn=lambda t: np.full_like(t, 3.0) + rng.normal(0, 0.1, len(t)),
        acc_kurt_fn=lambda t: np.full_like(t, 3.0) + rng.normal(0, 0.1, len(t)),
        running_fn=running_fn,
        axis_ratio_fn=lambda t: np.tile([0.5, 0.3, 0.2], (len(t), 1)),
    )


def _dev_degrade() -> pd.DataFrame:
    rng = np.random.default_rng(4)
    n_days = 45

    def ramp(t: np.ndarray, before: float, after: float, start_day: float = 14, end_day: float = 45) -> np.ndarray:
        frac = np.clip((t - start_day) / (end_day - start_day), 0, 1)
        return before + frac * (after - before)

    return _make_device_df(
        'DEV-DEGRADE', n_days, rng,
        vel_oa_fn=lambda t: ramp(t, 0.5, 2.6) + rng.normal(0, 0.04, len(t)),
        acc_rms_fn=lambda t: ramp(t, 0.4, 2.2) + rng.normal(0, 0.03, len(t)),
        acc_crest_fn=lambda t: ramp(t, 3.2, 9.0) + rng.normal(0, 0.2, len(t)),
        acc_kurt_fn=lambda t: ramp(t, 3.1, 11.0) + rng.normal(0, 0.2, len(t)),
        running_fn=lambda t: np.full_like(t, True, dtype=bool),
        axis_ratio_fn=lambda t: np.tile([0.6, 0.25, 0.15], (len(t), 1)),
        iso_code=1,
    )


def _dev_orientation() -> pd.DataFrame:
    rng = np.random.default_rng(5)
    n_days = 30

    def axis_ratio_fn(t: np.ndarray) -> np.ndarray:
        # 注意：aggregate.py 的軸能量佔比是「排序後」的 major/mid/minor（刻意去除
        # x/y/z 標籤，見 aggregate.py `_axis_energy_sorted`），因此單純把能量在
        # 三軸間「互換」（形狀不變、只是換了哪一軸最大）對排序後的統計量是隱形的
        # ——這是這個指標設計上的已知取捨，不是本檔的 bug。要讓 ORIENTATION_CHANGE/
        # AXIS_SHIFT 這兩條規則有訊號可偵測，合成劇本必須讓「分佈形狀」本身改變
        # （例如從「主軸集中度中等」變成「幾乎只剩一軸有能量」），而不只是換軸。
        out = np.tile([0.65, 0.25, 0.10], (len(t), 1))
        flipped = t >= 20
        out[flipped] = [0.92, 0.06, 0.02]
        return out

    return _make_device_df(
        'DEV-ORIENT', n_days, rng,
        vel_oa_fn=lambda t: np.full_like(t, 0.55) + rng.normal(0, 0.03, len(t)),
        acc_rms_fn=lambda t: np.full_like(t, 0.4) + rng.normal(0, 0.02, len(t)),
        acc_crest_fn=lambda t: np.full_like(t, 3.2) + rng.normal(0, 0.15, len(t)),
        acc_kurt_fn=lambda t: np.full_like(t, 3.1) + rng.normal(0, 0.15, len(t)),
        running_fn=lambda t: np.full_like(t, True, dtype=bool),
        axis_ratio_fn=axis_ratio_fn,
    )


#: 每台設備的已知劇本答案，供自動化驗證腳本比對（不是報表的一部分，
#: 純粹是測試用的「標準答案」）。
SCENARIOS = {
    'DEV-STABLE': {'expect_min_findings': 0, 'expect_max_findings': 0},
    'DEV-GAP': {'gap_hours': [12, 50], 'expect_rule_triggered': ['SENSOR_OFFLINE']},
    'DEV-STANDBY': {'expect_rule_triggered': ['STANDBY_NO_RUNTIME']},
    'DEV-DEGRADE': {'expect_rule_triggered': ['IMPACT_RISE', 'VEL_HIGH', 'ISO_ZONE'],
                    'expect_rule_not_triggered': ['ISO_CLASS_SUSPECT']},
    'DEV-ORIENT': {'expect_rule_triggered': ['ORIENTATION_CHANGE']},
}


def generate_all() -> dict[str, pd.DataFrame]:
    return {
        'DEV-STABLE': _dev_stable(np.random.default_rng(1)),
        'DEV-GAP': _dev_gap(),
        'DEV-STANDBY': _dev_standby(),
        'DEV-DEGRADE': _dev_degrade(),
        'DEV-ORIENT': _dev_orientation(),
    }


def write_dataset(out_dir: str) -> tuple[list[str], str]:
    """把 5 台設備的 CSV 與 device_meta.json 寫到 `out_dir`，回傳 (CSV 路徑清單, meta 路徑)。"""
    os.makedirs(out_dir, exist_ok=True)
    devices = generate_all()
    paths = []
    for name, df in devices.items():
        path = os.path.join(out_dir, f'{name}.csv')
        df.to_csv(path, index=False)
        paths.append(path)
        logger.info(f"寫出 {path}（{len(df)} 列）")

    meta = {
        'DEV-STANDBY': {'is_standby': True},
        'DEV-DEGRADE': {'iso_machine_class': 'I', 'iso_class_source': 'frontend'},
    }
    meta_path = os.path.join(out_dir, 'device_meta.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    logger.info(f"寫出 {meta_path}")
    return paths, meta_path


def main() -> None:
    logging.basicConfig(level='INFO', format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
    parser = argparse.ArgumentParser(description='產生合成多設備多天測試資料')
    parser.add_argument('--out-dir', required=True)
    args = parser.parse_args()
    write_dataset(args.out_dir)


if __name__ == '__main__':
    main()
