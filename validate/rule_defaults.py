"""
rule_defaults.py — 規則參數的預設值與覆寫機制

離線回測不連 PostgreSQL（`db/schema.sql` 的 `rule_config` 表要等系統上線
才有資料庫可查），但門檻調整正是這次回測要回答的問題本身，所以參數
不能寫死。做法：把 `rule_config` 的 13 筆 seed 資料原樣抄成 Python 常數
（與 `db/schema.sql` 保持一致，變更 schema 的 seed 時記得同步這裡），
再提供「覆寫」機制，讓：

  1. `validate/offline.py --rule-config path/to/override.json` 可以整批
     覆寫（例如把某工廠的 ISO 等級告警閾值改掉再回測一次）。
  2. 門檻敏感度掃描（`validate/backtest.py` 的 `sweep_threshold`）可以
     針對單一規則的單一參數逐步覆寫，跑出「N 個門檻各觸發幾件」的表。

兩者共用這裡的 `load_rule_configs()`，避免覆寫邏輯散落兩處而漂移。
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RuleConfigRow:
    """對應 `db/schema.sql` 的 `rule_config` 一列。"""
    rule_code: str
    rule_name: str
    family: str                      # oscillating | monotonic | event | none
    issue_type: str
    severity: str                    # 'err' | 'warn'
    params: dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    description: str = ''


#: 與 `db/schema.sql` 的 `rule_config` seed 逐筆對應（13 條規則）。
#: **修改 schema 的 seed 時務必同步更新這裡**，否則回測門檻會與上線
#: 後的實際設定值不一致，回測結論就失去意義。
DEFAULT_RULE_CONFIGS: dict[str, RuleConfigRow] = {
    r.rule_code: r for r in [
        RuleConfigRow(
            'ISO_ZONE', 'ISO 位準分級', 'oscillating', 'iso_zone_exceed', 'err',
            {'alert_zone': 'C'},
            description='velRMS 對照機械等級的 Zone A/B/C/D；未分級設備不套用'),
        RuleConfigRow(
            'VEL_HIGH', '速度整體值偏高', 'oscillating', 'vel_high', 'warn',
            {'sigma': 3.0},
            description='velOA 相對基準超過 N 個標準差'),
        RuleConfigRow(
            'IMPACT_RISE', '衝擊性指標上升', 'monotonic', 'impact_rise', 'warn',
            # 逐軸門檻與合成值同量級：沒有證據支持逐軸 kurt 比合成值更敏感
            # 或更不敏感（實測 crest 是逐軸較大、kurt 反而是合成較大），
            # 故不預設偏鬆或偏緊。
            {'crest_sigma': 2.5, 'kurt_sigma': 2.5,
             'crest_axis_sigma': 2.5, 'kurt_axis_sigma': 2.5,
             'require_both': False},
            description='accCREST / accKURT 相對基準顯著上升，常見於軸承或潤滑劣化（不判定成因）'),
        RuleConfigRow(
            'DEGRADE_TREND', '指標持續劣化', 'monotonic', 'degradation_trend', 'observe',
            {'min_days': 14, 'min_r2': 0.3, 'slope_pct_per_month': 10},
            description='回歸斜率持續惡化；須在聚合後的獨立樣本上計算'),
        RuleConfigRow(
            'SPECTRAL_SHIFT', '頻譜重心上移', 'monotonic', 'spectral_shift', 'observe',
            {'shift_pct': 15, 'min_days': 14},
            description='accWeightedMeanFreq 持續上移，代表能量往高頻移動'),
        RuleConfigRow(
            'AXIS_SHIFT', '軸能量分佈偏移', 'monotonic', 'axis_shift', 'observe',
            {'ratio_delta': 0.15},
            description='排序後三軸能量佔比相對基準偏移'),
        RuleConfigRow(
            'STEP_CHANGE', '多變量突變', 'monotonic', 'step_change', 'observe',
            {'mahalanobis_sigma': 3.0},
            description='特徵向量偏離基準；輸出各特徵標準化偏離量而非 0–100 分數'),
        RuleConfigRow(
            'ORIENTATION_CHANGE', '感測器方向改變', 'event', 'orientation_change', 'warn',
            # consecutive_readings / min_energy_ratio 的用意見 event_rules.py
            # 的 orientation_change docstring：前者擋逐日單點擲骰，後者擋
            # 低能量小時的佔比雜訊。兩者需與 db/schema.sql 的 seed 一致。
            {'ratio_delta': 0.25, 'consecutive_readings': 3, 'min_energy_ratio': 0.3},
            description='軸能量分佈排列跳變，疑似感測器重貼或更換'),
        RuleConfigRow(
            'TEMP_RISE', '溫度相對基準上升', 'oscillating', 'temp_rise', 'observe',
            # sigma 與 IMPACT_RISE 同量級；另有 consecutive_readings 把關，
            # 兩道防線一起收斂假警報。vibration_co_rise_sigma 只影響敘述
            # 措辭（同期振動是否也偏離），不影響是否觸發。
            {'sigma': 2.5, 'consecutive_readings': 3, 'vibration_co_rise_sigma': 1.0},
            description='溫度相對基準持續上升；一併呈現同期振動有無同步變化'),
        RuleConfigRow(
            'SENSOR_OFFLINE', '感測器離線', 'event', 'sensor_offline', 'err',
            {'hours': 24},
            description='逾時無資料'),
        RuleConfigRow(
            'DATA_QUALITY', '資料品質異常', 'event', 'data_quality', 'warn',
            {'min_running_ratio': 0.5},
            description='缺漏、零值、運轉樣本數不足'),
        RuleConfigRow(
            'SENSOR_SATURATION', '感測器接近飽和', 'event', 'sensor_saturation', 'warn',
            {'full_scale_pct': 90, 'range_g': 4},
            description='accPEAK 逼近量程滿刻度，峰值類指標將失真'),
        RuleConfigRow(
            'STANDBY_NO_RUNTIME', '備機長期未運轉', 'event', 'standby_no_runtime', 'warn',
            {'days': 30},
            description='備機超過 N 天未運轉，建議試車'),
        RuleConfigRow(
            'ISO_CLASS_SUSPECT', 'ISO 等級存疑', 'event', 'iso_class_suspect', 'warn',
            # frontend_consecutive_readings：與前端 iso10816 不一致需連續
            # 幾筆才算數。太小會被 Zone 邊界抖動觸發，太大則反應太慢；
            # 比照 ORIENTATION_CHANGE 的 consecutive_readings。
            {'frontend_consecutive_readings': 3},
            description='基準期中位數已超過所指派等級的 B/C 界，等級可能填錯或機器本有問題'),
    ]
}


def load_rule_configs(config_path: str | None = None,
                       overrides: dict[str, dict[str, Any]] | None = None) -> dict[str, RuleConfigRow]:
    """
    取得一份規則設定，供本次回測使用。

    Args:
        config_path: JSON 檔路徑，內容為 `{rule_code: {參數覆寫...}}` 或
            `{rule_code: {"params": {...}, "is_active": false, ...}}`。
            用於整批調整（例如「這次回測想看看把 VEL_HIGH.sigma 全面
            調到 3.5 會少多少件」），檔案不存在時只記警告、不中斷。
        overrides: 直接以程式碼傳入的覆寫（門檻敏感度掃描用），格式同
            `config_path` 內容，且**晚於** `config_path` 套用，優先權最高。

    Returns:
        `{rule_code: RuleConfigRow}`，一律回傳深拷貝，呼叫端可放心修改
        而不影響 `DEFAULT_RULE_CONFIGS` 本體。
    """
    configs = copy.deepcopy(DEFAULT_RULE_CONFIGS)

    if config_path:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                file_overrides = json.load(f)
        except FileNotFoundError:
            logger.warning(f"規則設定檔 {config_path} 不存在，使用預設值")
            file_overrides = {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"規則設定檔 {config_path} 讀取失敗（{e}），使用預設值")
            file_overrides = {}
        _apply_overrides(configs, file_overrides)

    if overrides:
        _apply_overrides(configs, overrides)

    return configs


def _apply_overrides(configs: dict[str, RuleConfigRow], overrides: dict[str, dict[str, Any]]) -> None:
    for rule_code, patch in overrides.items():
        if rule_code not in configs:
            logger.warning(f"覆寫設定提到未知規則代碼 {rule_code!r}，略過")
            continue
        row = configs[rule_code]
        if 'params' in patch:
            row.params.update(patch['params'])
        else:
            # 容許直接給 {rule_code: {param: value}} 的精簡寫法
            known_row_fields = {'rule_name', 'family', 'issue_type', 'severity',
                                 'is_active', 'description'}
            for k, v in patch.items():
                if k in known_row_fields:
                    setattr(row, k, v)
                else:
                    row.params[k] = v
