"""
config.py — 振動監測核心設定

此處的感測器常數並非估計值，而是以 data/rawdata.csv 與 data/Analytic.csv
交叉驗證反推所得（詳見 PLAN_agent_platform_refactor.md §三之二）：

  · 量程 ±4g、8192 counts/g  — 以靜止時的直流分量即重力向量反推，誤差 0.24%
  · 加速度單位 m/s²          — 去直流後 RMS 逐軸對照，比值 0.996~1.000
  · 前端滾動窗口 10 秒        — 掃描 0.5~120 秒，10 秒時相關係數 1.000
"""

from dataclasses import dataclass, field

# ──────────────────────────────────────────────────────────
# 感測器與前端處理特性（實測反推，勿隨意更動）
# ──────────────────────────────────────────────────────────

SENSOR_RANGE_G = 4.0                 # 量程 ±4g
SENSOR_COUNTS_PER_G = 32768 / 4.0    # 8192 counts/g
RAW_SAMPLE_RATE_HZ = 2000            # 原始取樣率
G_TO_MS2 = 9.80665

#: 前端輸出頻率（每秒 1 筆）
ANALYTIC_OUTPUT_HZ = 1

#: 前端計算用的滾動窗口長度（秒）。相鄰兩筆共用 90% 的原始資料，
#: 因此每日真正獨立的樣本數為 86400 / 10 = 8640 筆。
#: 趨勢分析必須在聚合後的獨立樣本上進行，否則 R² 與統計信心會嚴重高估。
ROLLING_WINDOW_SEC = 10

#: 前端可能採用的標稱輸出間隔（秒）。實測推估出來的間隔會吸附到最接近的
#: 候選值，避免少數缺漏樣本讓推估值落在 9.7 秒之類無意義的數字上。
#: 目前已知在用的是 1 秒（即時量測）與 600 秒（長期量測）兩種。
NOMINAL_INTERVALS_SEC = (1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600)

#: 感測器飽和判定門檻（佔滿刻度百分比）
SATURATION_PCT = 90.0
FULL_SCALE_MS2 = SENSOR_RANGE_G * G_TO_MS2   # 39.23 m/s²


# ──────────────────────────────────────────────────────────
# 欄位定義
# ──────────────────────────────────────────────────────────

#: Analytic CSV 的 metadata 欄位（非量測值）
META_COLS = (
    'Time', 'Name', 'Label', 'Building', 'Floor', 'System',
    'RPM', 'FMF', 'Ball', 'Vane', 'Gear', 'ISO10816_code',
    'Model_HealthScore', 'Model_FailureMode',
    'Channel_X', 'Channel_Y', 'Channel_Z',
)

#: 聚合方式：欄位語意決定取平均還是取最大值。
#: PEAK / CREST / KURT 類一律取最大值——衝擊事件若被平均掉，
#: 這些指標存在的意義就沒了。
AGG_MEAN = 'mean'      # 代表性水準
AGG_MAX = 'max'        # 極值，不可平均
AGG_AT_MAX = 'at_max'  # 取「主振幅最大時」對應的值

#: 進入 Tier 1 資料庫的精選欄位 → (來源欄位, 聚合方式)
#: 諧波欄位（accH01~H30 / velH01~H10）刻意不納入，定義無法驗證且
#: FREQ 峰值搜尋會鎖錯峰（見計畫書 §一）。
AGG_SPEC: dict[str, tuple[str, str]] = {
    # 速度（mm/s）
    'vel_rms':    ('velRMS',    AGG_MEAN),
    'vel_rms_x':  ('velRMS_x',  AGG_MEAN),
    'vel_rms_y':  ('velRMS_y',  AGG_MEAN),
    'vel_rms_z':  ('velRMS_z',  AGG_MEAN),
    'vel_oa':     ('velOA',     AGG_MEAN),
    'vel_peak':   ('velPEAK',   AGG_MAX),
    # 加速度（m/s²）
    'acc_rms':    ('accRMS',    AGG_MEAN),
    'acc_rms_x':  ('accRMS_x',  AGG_MEAN),
    'acc_rms_y':  ('accRMS_y',  AGG_MEAN),
    'acc_rms_z':  ('accRMS_z',  AGG_MEAN),
    'acc_oa':     ('accOA',     AGG_MEAN),
    'acc_peak':   ('accPEAK',   AGG_MAX),
    'acc_crest':  ('accCREST',  AGG_MAX),
    'acc_kurt':   ('accKURT',   AGG_MAX),
    'acc_skew':   ('accSKEW',   AGG_MEAN),
    # 位移（mm）
    'disp_rms':   ('dispRMS',   AGG_MEAN),
    'disp_p2p':   ('dispP2P',   AGG_MAX),
    # 頻譜摘要純量（穩健，不需辨識個別諧波）
    'acc_mean_peak_freq':     ('accMeanPeakFreq',     AGG_MEAN),
    'acc_weighted_mean_freq': ('accWeightedMeanFreq', AGG_MEAN),
    'acc_top1_freq':          ('accTOP1FREQ',         AGG_AT_MAX),
    'acc_top1_amp':           ('accTOP1FREQ_V',       AGG_MAX),
    'vel_weighted_mean_freq': ('velWeightedMeanFreq', AGG_MEAN),
}

#: `at_max` 聚合的參考欄位（取此欄最大時對應的值）
AT_MAX_REFERENCE = 'accTOP1FREQ_V'

#: 三軸能量分佈的來源欄位（用於方向無關的軸能量佔比）
AXIS_ENERGY_COLS = ('accRMS_x', 'accRMS_y', 'accRMS_z')


# ──────────────────────────────────────────────────────────
# 聚合與資料品質
# ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AggregateConfig:
    """每小時聚合的參數。"""

    #: 每小時預期樣本數（每秒 1 筆）
    expected_samples_per_hour: int = 3600

    #: 運轉判定門檻（mm/s）。無電流資料時以 velRMS 判斷。
    vel_running_threshold: float = 0.1

    #: 有電流資料時的運轉門檻（A）
    current_running_threshold: float = 1.0

    #: 完整度低於此值標記為 partial，其指標不進入趨勢回歸與規則判定
    partial_threshold: float = 0.5

    #: 運轉樣本數需達「每小時預期樣本數」的此比例，該小時的指標才具代表性。
    #:
    #: 用比例而非絕對筆數，是因為取樣頻率會隨資料版本改變：即時量測是每秒
    #: 一筆（3600 筆/小時），長期量測是每 10 分鐘一筆（6 筆/小時）。若寫死
    #: 「至少 60 筆」，10 分鐘版一小時最多 6 筆，**永遠達不到門檻**，每個
    #: 小時都會被判為 partial，所有指標型規則靜默跳過，整份回測歸零。
    #:
    #: 預設 60/3600 是沿用原本「每秒一筆時至少要有 1 分鐘運轉資料」的判準，
    #: 換算到 10 分鐘版即為「至少 1 筆」。
    min_running_ratio: float = 60 / 3600

    #: 運轉樣本數的絕對下限，避免比例算出 0
    min_running_floor: int = 1

    #: 明確指定運轉樣本數門檻；設定後覆蓋上面的比例計算。
    #: 一般不需要動，除非要針對特定資料集手動調整。
    min_running_samples: int | None = None

    def effective_min_running(self, expected_per_hour: int | None = None) -> int:
        """
        算出這批資料實際適用的「最少運轉樣本數」。

        Args:
            expected_per_hour: 該批資料實測的每小時樣本數；None 表示用設定值
        """
        if self.min_running_samples is not None:
            return self.min_running_samples
        expected = expected_per_hour or self.expected_samples_per_hour
        return max(self.min_running_floor, round(expected * self.min_running_ratio))


@dataclass(frozen=True)
class TrendConfig:
    """趨勢分析參數。"""

    #: 趨勢回歸的最短觀察天數
    min_days: int = 14

    #: 參與回歸的最少有效點數
    min_points: int = 24

    #: R² 低於此值視為趨勢不明確，不可寫成結論
    min_r2: float = 0.3

    #: 資料完整度低於此值的期間不納入回歸
    min_completeness: float = 0.5


DEFAULT_AGG = AggregateConfig()
DEFAULT_TREND = TrendConfig()


# ──────────────────────────────────────────────────────────
# 資料狀態
# ──────────────────────────────────────────────────────────

class DataStatus:
    """
    每小時資料的狀態。

    「斷線」與「未運轉」是完全不同的兩件事，混為一談會同時毀掉趨勢圖
    與規則判定——前者是設備異常需要告警，後者是正常狀態不該判異常。
    """

    OK = 'ok'                    # 資料完整且運轉中
    PARTIAL = 'partial'          # 有資料但筆數不足
    NO_DATA = 'no_data'          # 完全無資料（感測器斷線）
    NOT_RUNNING = 'not_running'  # 有資料但設備未運轉

    #: 可用於趨勢分析與規則判定的狀態
    ANALYZABLE = (OK,)

    #: 代表資料缺失（需要告警）的狀態
    GAP = (NO_DATA, PARTIAL)
