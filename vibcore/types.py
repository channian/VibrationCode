"""
types.py — 跨模組共用的資料型別契約

**這個檔案是整合的基準。** 指標層、規則層、資料存取層、驗證框架都依此
溝通；平行開發時各模組只需遵守這裡的定義，不需要知道彼此的實作。

修改此檔會影響所有模組，變更前需確認相依方。
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

import pandas as pd


# ──────────────────────────────────────────────────────────
# 指標層輸出
# ──────────────────────────────────────────────────────────

@dataclass
class MetricStats:
    """單一指標在基準期的統計量。"""
    median: float
    mean: float
    std: float
    n: int

    def sigma_of(self, value: float) -> float:
        """某數值相對基準的標準化偏離量（σ）。std 為 0 時回傳 0。"""
        if self.std is None or self.std <= 0 or value is None:
            return 0.0
        return (value - self.median) / self.std


@dataclass
class BaselineStats:
    """
    量測點的基準期與各指標統計。

    對應 DB 的 `point_baseline`；`stats` 序列化後存入 JSONB。
    """
    point_id: int | str
    start_date: date
    end_date: date
    source: str                      # 'auto' | 'manual'
    stats: dict[str, MetricStats]
    n_hours: int = 0
    note: str = ''

    def to_jsonb(self) -> dict:
        return {k: {'median': v.median, 'mean': v.mean, 'std': v.std, 'n': v.n}
                for k, v in self.stats.items()}


@dataclass
class TrendResult:
    """
    單一指標的趨勢分析結果。

    **必須在 `data_status == 'ok'` 的獨立樣本上計算。** 前端輸出為每秒一筆
    但滾動窗口 10 秒，相鄰資料高度重疊；用未聚合的資料回歸會讓 R² 與
    統計信心嚴重高估（見計畫書 §三）。
    """
    metric: str
    slope_per_day: float             # 單位/天
    slope_per_month: float           # 單位/月（= slope_per_day × 30）
    slope_pct_per_month: float       # 相對基準中位數的百分比變化/月
    intercept: float
    r2: float
    n_points: int
    span_days: float
    direction: str                   # 'up' | 'down' | 'flat' | 'unknown'
    confidence: str                  # 'high' | 'medium' | 'low'
    note: str = ''                   # 樣本不足、R² 偏低等中文說明

    @property
    def is_reliable(self) -> bool:
        """confidence 為 low 時，推估結果不可寫成結論。"""
        return self.confidence != 'low'


@dataclass
class IsoResult:
    """
    ISO 10816/20816 位準分級結果。

    未分級設備（`iso_class_source == 'unset'`）**不套用 Zone 判定**，
    此時 `zone` 為 None、`applicable` 為 False。
    """
    applicable: bool
    machine_class: str | None        # 'I' | 'II' | 'III' | 'IV'
    class_source: str                # 'unset' | 'frontend' | 'manual_override'
    zone: str | None                 # 'A' | 'B' | 'C' | 'D'
    vel_rms: float | None
    thresholds: dict[str, float] = field(default_factory=dict)   # ab/bc/cd
    is_class_suspect: bool = False   # 基準期中位數已超過 B/C 界 → 等級可能填錯
    suspect_reason: str = ''
    note: str = ''


@dataclass
class DeviationResult:
    """
    多變量偏離偵測結果。

    刻意**不輸出 0–100 健康分數**——該分數錨點任意、跨設備不可比、對工況
    敏感（見計畫書 §四）。改為輸出「是否偏離」加上各特徵的 σ 分解，
    對 agent 而言可直接寫進報告，比單一分數可用得多。
    """
    is_deviated: bool
    distance: float                          # Mahalanobis 距離
    threshold: float
    per_feature_sigma: dict[str, float]      # {'acc_kurt': 2.8, 'vel_rms': 0.3, ...}
    top_contributors: list[str] = field(default_factory=list)
    note: str = ''

    #: 是否真的算出了距離。**無資料可判定時必須設為 False。**
    #: 若沿用預設的 distance=0.0 表示「未評估」，語意會與「完全貼合基準」
    #: 混淆——備機從未運轉時，agent 可能寫成「偏離距離 0，狀態極佳」，
    #: 但實際上根本沒有評估過。呼叫端一律先檢查此旗標。
    computable: bool = True

    def describe(self) -> str:
        """產生給 agent 使用的中文摘要（僅陳述現象，不判定故障類型）。"""
        if not self.computable:
            return f'未評估（{self.note}）' if self.note else '未評估：無足夠資料'
        if not self.is_deviated:
            return '未偵測到顯著偏離'
        parts = [f'{k} {v:+.1f}σ' for k, v in
                 sorted(self.per_feature_sigma.items(), key=lambda kv: -abs(kv[1]))[:3]]
        return '偏離主要來自 ' + '、'.join(parts)


# ──────────────────────────────────────────────────────────
# 規則層
# ──────────────────────────────────────────────────────────

@dataclass
class DeviceContext:
    """規則判定所需的設備資訊。"""
    device_id: str
    device_name: str = ''
    building: str = ''
    floor: str = ''
    system_name: str = ''
    machine_type: str = ''
    is_standby: bool = False
    iso_machine_class: str | None = None
    iso_class_source: str = 'unset'
    rated_rpm: float | None = None
    fmf_hz: float | None = None


@dataclass
class RuleContext:
    """
    規則引擎的輸入。一條規則能看到的全部資訊都在這裡。

    `agg` 為每小時聚合結果，**含 `data_status` 欄**；規則實作必須自行
    篩掉 `not_running`（正常狀態，不判異常）與 `no_data`/`partial`
    （資料不可信）的列。
    """
    device: DeviceContext
    point_id: int | str
    position: str
    agg: pd.DataFrame                        # 每小時聚合（含缺口列）
    baseline: BaselineStats | None
    params: dict[str, Any]                   # 來自 rule_config.params
    now: datetime
    axis_energy_baseline: dict | None = None
    last_data_at: datetime | None = None

    def analyzable(self) -> pd.DataFrame:
        """只取可用於判定的列（data_status == 'ok'）。"""
        if self.agg.empty or 'data_status' not in self.agg.columns:
            return self.agg
        return self.agg[self.agg['data_status'] == 'ok']


@dataclass
class RuleOutcome:
    """
    單一規則的判定結果。

    `interpretation_limit` 是**強制欄位**：本系統定位為篩選預警而非診斷，
    agent 必須據此知道該證據能解讀到什麼程度，不得臆測故障類型
    （見計畫書 §8.2）。
    """
    triggered: bool
    rule_code: str
    issue_type: str
    family: str                              # oscillating | monotonic | event | none
    severity: str                            # 'err' | 'warn'
    title: str = ''
    detail: str = ''
    interpretation_limit: str = ''
    current_value: float | None = None
    baseline_value: float | None = None
    value_unit: str = ''
    evidence: dict = field(default_factory=dict)
    target_type: str = 'point'               # 'device' | 'point' | 'global'

    @staticmethod
    def no_trigger(rule_code: str, issue_type: str = '', family: str = 'none') -> 'RuleOutcome':
        return RuleOutcome(triggered=False, rule_code=rule_code,
                           issue_type=issue_type, family=family, severity='warn')


# ──────────────────────────────────────────────────────────
# Finding（對應 DB 的 finding 表）
# ──────────────────────────────────────────────────────────

#: 四階段簽核的狀態流轉
FINDING_OPEN = 'open'
FINDING_ENGINEER = 'engineer_replied'
FINDING_SUPERVISOR = 'supervisor_reviewed'
FINDING_EXPERT = 'expert_reviewed'
FINDING_CLOSED = 'closed'
FINDING_AUTO_RESOLVED = 'auto_resolved'
FINDING_FALSE_POSITIVE = 'false_positive'

#: 正常簽核鏈的順序
#: 嚴重度。err / warn 會建立 Finding 並進入簽核鏈；observe 不會。
#:
#: `observe` 是給「有偵測價值但沒有可引用的外部標準」的規則用的
#: （例如多變量偏離、頻譜重心位移、溫度相對上升）。這類判定只進週報的
#: 觀察名單，不建立 Finding、不佔 SLA、不需簽核。
#:
#: 為什麼要分這一級：ISO_ZONE 的門檻是國際標準訂的、VEL_HIGH 可錨定
#: 到 ISO 的告警設定原則，這些拿去派工，工程師問「門檻哪來的」我們答得
#: 出來。但 Mahalanobis 距離超過 3σ 是我們自己訂的，答不出來。用答不出
#: 來的數字派工，簽核鏈很快就會失去公信力，連帶拖累有依據的那幾條。
#:
#: 等累積足夠的 false_positive 回饋、門檻站得住腳之後，再逐條升為 warn。
SEVERITY_ERR = 'err'
SEVERITY_WARN = 'warn'
SEVERITY_OBSERVE = 'observe'

#: 會建立 Finding 並進入簽核流程的嚴重度
ACTIONABLE_SEVERITIES = (SEVERITY_ERR, SEVERITY_WARN)


def is_actionable(severity: str) -> bool:
    """此嚴重度是否該建立 Finding 並進入 SLA。"""
    return severity in ACTIONABLE_SEVERITIES


SIGNOFF_CHAIN = (FINDING_OPEN, FINDING_ENGINEER, FINDING_SUPERVISOR,
                 FINDING_EXPERT, FINDING_CLOSED)

#: 視為已結案的狀態
CLOSED_STATUSES = (FINDING_CLOSED, FINDING_AUTO_RESOLVED, FINDING_FALSE_POSITIVE)


@dataclass
class Finding:
    """對應 DB `finding` 表的一列。"""
    finding_key: str                         # {target_type}:{target}:{issue_type}
    device_id: str
    target_type: str
    target: str
    issue_type: str
    family: str
    rule_code: str
    title: str
    severity: str
    peak_severity: str
    point_id: int | None = None
    detail: str = ''
    status: str = FINDING_OPEN
    stage_entered_at: datetime | None = None
    assigned_to: int | None = None
    occurrence_count: int = 1
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    baseline_value: float | None = None
    current_value: float | None = None
    value_unit: str = ''
    evidence: dict = field(default_factory=dict)
    #: 觸發當下 rule_config.params 的快照。只存數值而不存當時的門檻，
    #: 回溯時就分不清是數值變了還是門檻被調過。
    trigger_params: dict = field(default_factory=dict)
    interpretation_limit: str = ''
    escalated_at: datetime | None = None
    needs_expert_measurement: bool = False
    source: str = 'rule_engine'
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    @staticmethod
    def make_key(target_type: str, target: str, issue_type: str) -> str:
        return f'{target_type}:{target}:{issue_type}'

    @property
    def is_open(self) -> bool:
        return self.status not in CLOSED_STATUSES


# ──────────────────────────────────────────────────────────
# 資料品質
# ──────────────────────────────────────────────────────────

@dataclass
class CoverageInfo:
    """
    資料涵蓋率。週報與 Dashboard 都必須呈現——涵蓋率不足時的結論不可信，
    agent 需據此標明信心度。
    """
    total_hours: int
    ok_hours: int
    partial_hours: int
    no_data_hours: int
    not_running_hours: int
    analyzable_ratio: float
    period_start: datetime | None = None
    period_end: datetime | None = None

    @property
    def is_sufficient(self) -> bool:
        """可分析時數比例是否足以支撐結論。"""
        return self.analyzable_ratio >= 0.5
