"""
config.py — 振動監測核心設定

此處的感測器常數以 data/rawdata.csv 與 data/Analytic.csv 交叉驗證反推所得
（詳見 PLAN_agent_platform_refactor.md §三之二）：

  · 量程 ±4g、8192 counts/g  — 以靜止時的直流分量即重力向量反推，誤差 0.24%
  · 加速度單位 m/s²          — 去直流後 RMS 逐軸對照，比值 0.996~1.000
  · 前端滾動窗口 10 秒        — 掃描 0.5~120 秒，10 秒時相關係數 1.000

**適用範圍的重要限制**：上述兩份檔案都來自 **AHU-601**，而該台是
**臨時量測**、不是常態監測的配置（正式監測對象是 ZP／CP 等泵浦與冰水
主機，走每 10 分鐘一筆的長期量測版）。因此這些常數只確定在 AHU-601 的
量測配置下成立，尚未在正式設備上驗證。

哪些結論不受影響（已在 ZP 3-5 與 CP 10 上獨立驗證）：

  · accCREST = accPEAK / accRMS  — 三台相對誤差皆為 0.00%，與絕對單位無關
  · accOA ≈ 646 × accRMS         — 逐筆比值 CV 僅 0.001~0.002、相關 0.998，
    即 accOA 只是 accRMS 的等比縮放，不帶額外資訊
  · velRMS 的單位是 mm/s         — 泵浦實測 0.45~0.68、AHU 1.23，落在該類
    設備的物理合理範圍；且前端自己算的 iso10816 分級與本系統依 mm/s 判定
    的結果一致，等於前端開發者的意圖也是 mm/s

哪些**只有 AHU-601 為據**，要分成兩種情況看：

  · **前端軟體的性質——很可能通用**：加速度單位 m/s²、滾動窗口 10 秒。
    使用者確認 rawdata 的格式在即時量測與長期量測兩種模式下**完全相同**
    （皆為三軸 + 溫度），代表前端跑的是同一套處理程式，這類由「程式怎麼
    算」決定的常數沒有理由因設備而異。

  · **感測器硬體的性質——取決於是否同型號**：量程 ±4g 與由此換算的
    FULL_SCALE_MS2。若全廠佈的是同一款感測器就通用，否則需個別確認。

實務影響有限但不是零：只有 SENSOR_SATURATION 依賴絕對量程，而泵浦的
accPEAK 只到滿刻度的 2~7%（AHU 曾達 56%），離飽和很遠，目前不會因為量程
猜錯而誤判；但若正式設備量程不同，該規則的門檻就是錯的。要完全收斂，
需要一份**泵浦的 rawdata**，比照 AHU 的方式重新反推——由於格式相同，
同一套反推程序可以直接套用。
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
AGG_MIN = 'min'        # 極小值（目前僅溫度用）
AGG_MEDIAN = 'median'  # 代表性水準，且對窗口長度不敏感（見下方衝擊型指標說明）
AGG_AT_MAX = 'at_max'  # 取「主振幅最大時」對應的值

#: 進入 Tier 1 資料庫的精選欄位 → (來源欄位, 聚合方式)
#:
#: 諧波欄位（accH01~H30 / velH01~H10）刻意不納入。以 data/Analytic.csv
#: （AHU-601，228 筆）實測後的依據，分成兩半：
#:
#:   · **頻率欄位（accH*FREQ）其實是可信的** ——RPM 1710 → 轉速頻率
#:     28.5 Hz，實測 H01=29.3、H05=141.4、H30=855.3，偏差在 ±3% 內，
#:     高階更準。早期版本此處寫「FREQ 峰值搜尋會鎖錯峰」，該說法有誤。
#:
#:   · **振幅欄位（_V / _OA）不可信，這才是排除的真正理由**：
#:       1. 逐軸諧波能量總和與該軸 accOA 的相關係數僅 -0.06~+0.13。
#:          若是同一訊號的頻譜分量，兩者必然同步變動。
#:       2. 合成軸欄位無法由三軸推得，且時常小於任一軸（例如 H02 三軸
#:          為 4.5/3.3/7.9，合成欄卻是 2.2），物理上不可能。
#:       3. 該設備 velRMS 的 CV 僅 0.026（極穩定），但諧波佔比向量的
#:          變動達 10%（TV 距離中位 0.104）——行為像雜訊而非頻譜。
#:
#: 上述原本只有單一設備（且振動偏小）為據，後續取得 ZP 3-5（accOA≈129）
#: 與 CP 10（accOA≈503）兩台長期量測樣本後**跨設備複驗，結論不變且更強**：
#: CP 10 的整體振動是 ZP 3-5 的 3.91 倍，TOP1-5 能量隨之放大 6.6 倍（合理），
#: 但 30 階諧波能量僅 1.06 倍（等於沒動）；逐階看更反常——安靜的 ZP 3-5
#: 的 H03 是 128.5，吵的 CP 10 只有 17.3，**方向相反**。
#:
#: 另外，生產環境的長期量測匯出只有 217 欄（即時量測版為 669 欄），
#: **諧波僅保留振幅、頻率欄位整批消失**，連交叉驗證的手段都沒有。
#:
#: 主峰頻率（accTOP*FREQ）則視設備而定，不可一概採用：AHU-601 上看似
#: 極穩定（228 筆僅 10 種取值），但那是每秒取樣配 10 秒滾動窗、相鄰兩筆
#: 共用 90% 原始資料造成的假象。10 分鐘版每筆獨立，泵類設備的 TOP2/TOP1
#: 振幅比達 0.74~0.86（前幾個峰值高度接近），主峰身分每次量測都在翻轉，
#: CP 10 的 TOP1 頻率標準差高達 95 Hz。故 acc_top1_freq 僅以 at_max
#: 方式保留為輔助欄位，不適合單獨作為判定依據。
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
    # 衝擊型指標同時保留 max 與 median，兩者回答不同問題，缺一不可：
    #
    #   max    「這一小時內最劇烈的那一刻有多尖」——衝擊事件不可被平均掉，
    #           這是當初只存 max 的理由，仍然成立。
    #   median 「這一小時的代表性形狀」——判定要比對的是這個。
    #
    # 為什麼非得補上 median：kurtosis 的業界判準（常態=3、超過 4 視為出現
    # 衝擊）講的是**一段訊號的峰度**，不是 3600 個滾動窗峰度的最大值。
    # 以 AHU-601 實測，逐筆中位數 2.37、僅 2.6% 超過 4，但每 30/60/114 筆
    # 取最大值後平均已達 7.17/13.49/19.00（超過 4 的比例 14%/33%/50%）；
    # 中位數則不論區塊多大都穩定在 2.37。正式聚合是每小時 3600 筆取 max，
    # 偏高的幅度只會更大——拿它去比對「>4」這個判準，門檻等於恆為真。
    # 這是規則層 IMPACT_RISE 判定要改用 median 通道的原因。
    'acc_crest':        ('accCREST',  AGG_MAX),
    'acc_kurt':         ('accKURT',   AGG_MAX),
    'acc_crest_median': ('accCREST',  AGG_MEDIAN),
    'acc_kurt_median':  ('accKURT',   AGG_MEDIAN),
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
    # 溫度（°C）——與振動獨立的唯一物理通道。「振動上升但溫度持平」與
    # 「兩者一起上升」對現場的意義不同，而做這個區分不需要推論成因。
    # 注意：可能是感測器內部溫度而非軸承座溫度（見 docs/DATA_CONTRACT.md
    # §3.1），故只用於「與自身基準比」的相對趨勢，不設絕對門檻。
    'temp_avg':   ('tempAVG',   AGG_MEAN),
    'temp_max':   ('tempMAX',   AGG_MAX),
    'temp_min':   ('tempMIN',   AGG_MIN),
    # 前端已算好的 ISO 分級（1=Zone A…4=Zone D）。取該小時最差值。
    # 僅作為本系統自行判定的交叉檢查——前端假設的機械等級未知。
    'iso_zone_frontend': ('iso10816', AGG_MAX),
}

#: 逐軸的衝擊型指標。衝擊常集中在單一方向，只看合成值會被稀釋；
#: 取三軸中的最大值較敏感。沿用方向無關原則——只取極值、不保留
#: x/y/z 標籤，因為感測器可能貼錯方向（見 `_axis_energy_sorted`）。
AXIS_IMPACT_COLS: dict[str, tuple[str, str, str]] = {
    'acc_crest_axis_max': ('accCREST_x', 'accCREST_y', 'accCREST_z'),
    'acc_kurt_axis_max':  ('accKURT_x',  'accKURT_y',  'accKURT_z'),
}

#: 逐軸衝擊指標的 median 版本：仍是逐列先取三軸最大（「哪一個方向最尖」是
#: 每一筆當下的事實，不該被小時內的統計方式改變），但小時層改取中位數。
#: 理由與 AGG_SPEC 的 acc_kurt_median 相同，見該處說明。
AXIS_IMPACT_MEDIAN_COLS: dict[str, tuple[str, str, str]] = {
    'acc_crest_axis_median': ('accCREST_x', 'accCREST_y', 'accCREST_z'),
    'acc_kurt_axis_median':  ('accKURT_x',  'accKURT_y',  'accKURT_z'),
}

#: `at_max` 聚合的參考欄位（取此欄最大時對應的值）
AT_MAX_REFERENCE = 'accTOP1FREQ_V'

#: 三軸能量分佈的來源欄位（用於方向無關的軸能量佔比）
AXIS_ENERGY_COLS = ('accRMS_x', 'accRMS_y', 'accRMS_z')

# ──────────────────────────────────────────────────────────
# 感測器軸向
# ──────────────────────────────────────────────────────────

#: `Channel_X/Y/Z` 的數值 → 該軸的物理方向（使用者 2026-09 提供）。
#:
#: **x/y/z 與方向的對應逐台不同，必須逐台讀 Channel 欄位，不可用位置推斷。**
#: 實測三台：
#:
#:     設備        Channel_X       Channel_Y       Channel_Z
#:     AHU-601     4 垂直徑向      6 水平徑向      5 軸向
#:     ZP 3-5      4 垂直徑向      5 軸向          6 水平徑向
#:     CP 10       4 垂直徑向      5 軸向          6 水平徑向
#:
#: AHU-601 的 Y/Z 與兩台泵相反。這也是既有的 `_axis_energy_sorted`
#: （排序後只留主/次/弱、丟掉標籤）當初的成因——在不讀 Channel 的前提下，
#: 那是唯一安全的做法。讀了 Channel 之後就不必再丟掉方向資訊。
AXIS_DIRECTION_CODES: dict[int, str] = {
    4: 'vertical_radial',      # 垂直徑向
    5: 'axial',                # 馬達軸向
    6: 'horizontal_radial',    # 水平徑向／切線
}

#: 方向代碼的中文顯示名稱
AXIS_DIRECTION_LABELS: dict[str, str] = {
    'vertical_radial':   '垂直徑向',
    'axial':             '軸向',
    'horizontal_radial': '水平徑向',
}

#: `Channel_X/Y/Z` 欄名 → 對應的軸後綴
_CHANNEL_COLS: dict[str, str] = {'Channel_X': 'x', 'Channel_Y': 'y', 'Channel_Z': 'z'}


def resolve_axis_directions(row_or_meta) -> dict[str, str] | None:
    """
    由 `Channel_X/Y/Z` 解析出 `{'x': 方向, 'y': 方向, 'z': 方向}`。

    **三個通道必須恰好湊齊 4/5/6 才回傳結果**，否則回傳 None。理由是
    這三個代碼代表三個互相垂直的方向，缺一或重複都代表台帳設定有問題
    （例如兩軸都填 5），此時任何方向解讀都是錯的——寧可退回方向無關的
    排序佔比，也不要用一組矛盾的設定算出看似合理的數字。

    Args:
        row_or_meta: 任何支援 `[欄名]` 取值的物件（pandas Series、
            `extract_metadata()` 回傳的 dict、DataFrame 的一列皆可）。

    Returns:
        `{'x': 'vertical_radial', ...}`；無法解析時 None。
    """
    codes: dict[str, int] = {}
    for col, axis in _CHANNEL_COLS.items():
        try:
            v = row_or_meta[col]
        except (KeyError, IndexError, TypeError):
            return None
        if v is None:
            return None
        try:
            code = int(float(v))
        except (TypeError, ValueError):
            return None
        if code not in AXIS_DIRECTION_CODES:
            return None
        codes[axis] = code

    if len(codes) != 3 or len(set(codes.values())) != 3:
        return None
    return {axis: AXIS_DIRECTION_CODES[code] for axis, code in codes.items()}


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
