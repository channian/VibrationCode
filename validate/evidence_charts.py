"""
evidence_charts.py — 驗證報告的佐證圖表

從兩份可重現的產出畫圖，不手填任何數字：

  docs/evidence/agent_guard_scenarios.json   ← python -m validate.agent_guard_scenarios
  docs/evidence/replay_synthetic.json        ← 合成資料逐日重放的資料庫摘要（見 docs/evidence/README.md）

另有一張圖（圖 3）的數字來自**真實生產資料的離線回測**，這些數字寫在決策紀錄裡、無法在這個環境重現，
所以直接以常數列在本檔 `REAL_BACKTEST`，並在圖上標明來源。要改請連同決策紀錄一起改。

執行：
    python -m validate.evidence_charts                # 讀寫 docs/evidence
    python -m validate.evidence_charts --dir DIR

圖檔：
    fig1_guard_scenarios.png     九個情境、逐筆案例的結果（通過／已知缺口／需真實 Agent 才能測）
    fig2_guard_effect.png        每道機制處理了幾筆壞 payload，以及合法對照組有沒有被誤擋
    fig3_rule_calibration.png    規則校正閉環：以真實回測數據改變判定規則的三次調整
    fig4_synthetic_replay.png    已知答案的合成資料：劇本事件 vs 正式排程實際觸發的時間

配色與圖面規則依 dataviz 技能：狀態色固定（通過／警示／嚴重），狀態一律同時有文字標籤，不單靠顏色。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from matplotlib import font_manager

# ── 版面常數（淺色圖面；PNG 要貼進簡報，不做深色版）──
SURFACE = "#fcfcfb"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8985", "#e6e5e1"
BLUE, AQUA, ORANGE = "#2a78d6", "#1baf7a", "#eb6834"          # 類別色 1、3、2
GOOD, WARN, CRIT, NEUTRAL = "#0ca30c", "#fab219", "#d03b3b", "#b9b8b0"   # 狀態色（固定，不佈景）

STATUS_STYLE = {
    "PASS": ("通過", GOOD),
    "GAP": ("已知缺口", WARN),
    "NOT_TESTED": ("需真實 Agent 才能測", NEUTRAL),
    "FAIL": ("未通過", CRIT),
}

#: 圖 3 的數字：真實生產資料離線回測（66 台、11 週，2026-06-05 起），出處見 docs/AGENT_certification_draft.md 第 3 張
#: 與 docs/DECISIONS_2026-09_expert_review.md Q2／D1。不是這個環境算出來的，改動時要連同出處一起改。
REAL_BACKTEST = {
    "impact": {"title": "第 1 次調整：衝擊性規則移除無鑑別力的峰度通道", "unit": "觸發次數",
               "steps": [("移除前", 193), ("移除後", 144)]},
    "iso": {"title": "第 2、3 次調整：補 ISO 台帳，再加實用顯著性下限（偏高規則）", "unit": "事件件數",
            "steps": [("未分類\n（相對基準 3σ）", 117), ("補台帳後", 41), ("加下限後\n（轉觀察，不派工）", 19)]},
    "workload": {"title": "第 2 次調整的效果：全廠每週新增工作量", "unit": "件／週",
                 "steps": [("補台帳前", 10.6), ("補台帳後", 3.8)]},
}

DEVICES = ["DEV-STABLE", "DEV-GAP", "DEV-STANDBY", "DEV-DEGRADE", "DEV-ORIENT"]


def setup_font() -> None:
    for name in ("Noto Sans CJK TC", "Noto Sans CJK JP", "WenQuanYi Zen Hei", "Microsoft JhengHei", "PingFang TC"):
        if any(f.name == name for f in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


def base_fig(w: float, h: float):
    fig = plt.figure(figsize=(w, h), dpi=150, facecolor=SURFACE)
    return fig


def style_axes(ax, grid_axis: str = "x") -> None:
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0, labelsize=9)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def headline(fig, title: str, sub: str) -> None:
    fig.text(0.04, 0.965, title, fontsize=15, fontweight="bold", color=INK, va="top")
    fig.text(0.04, 0.915, sub, fontsize=9.5, color=INK2, va="top")


def footer(fig, text: str) -> None:
    fig.text(0.04, 0.02, text, fontsize=8, color=MUTED, va="bottom")


# ──────────────────────────────────────────────────────────
# 圖 1：情境 × 結果
# ──────────────────────────────────────────────────────────

def fig1(data: dict, out: Path) -> None:
    meta, cases = data["meta"], data["cases"]
    by = meta["by_scenario"]
    ids = list(by.keys())[::-1]
    fig = base_fig(11, 6.2)
    ax = fig.add_axes([0.27, 0.17, 0.68, 0.64])
    style_axes(ax, "x")
    for i, sid in enumerate(ids):
        left = 0
        for st in ("PASS", "GAP", "NOT_TESTED", "FAIL"):
            n = by[sid][st]
            if not n:
                continue
            label, color = STATUS_STYLE[st]
            ax.barh(i, n, left=left, height=0.62, color=color, edgecolor=SURFACE, linewidth=2)
            ax.text(left + n / 2, i, str(n), ha="center", va="center", fontsize=9.5, color=INK, fontweight="bold")
            left += n
        ax.text(left + 0.4, i, f"共 {sum(by[sid][k] for k in STATUS_STYLE)} 筆", va="center", fontsize=8.5, color=INK2)
    ax.set_yticks(range(len(ids)))
    ax.set_yticklabels([f"{sid}  {by[sid]['scenario']}" for sid in ids], fontsize=10, color=INK)
    ax.set_xlim(0, max(sum(by[s][k] for k in STATUS_STYLE) for s in ids) + 4)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("案例數", fontsize=9, color=INK2)
    t = meta["totals"]
    total = sum(t.values())
    headline(fig, f"Agent 護欄情境驗證：{total} 筆案例中 {t['PASS']} 筆符合設計意圖",
             f"{t['GAP']} 筆是已知缺口（機制擋不住，如實列出）；{t['NOT_TESTED']} 筆需要真實 Agent 才能測，不在本次結果內。"
             "內容為腳本寫好的 payload，不是模型輸出。")
    handles = [plt.Rectangle((0, 0), 1, 1, color=STATUS_STYLE[k][1]) for k in ("PASS", "GAP", "NOT_TESTED")]
    ax.legend(handles, [STATUS_STYLE[k][0] for k in ("PASS", "GAP", "NOT_TESTED")], loc="lower right",
              bbox_to_anchor=(1.0, 1.0), ncol=3, frameon=False, fontsize=9, labelcolor=INK2)
    footer(fig, f"來源：validate.agent_guard_scenarios（{meta['run_at_utc'][:10]}）；"
                "打真實 HTTP 端點與獨立測試庫，數字白名單為預設 report 模式與 enforce 模式各測一次。")
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)


# ──────────────────────────────────────────────────────────
# 圖 2：每道機制處理了幾筆壞 payload
# ──────────────────────────────────────────────────────────

MECH_LABEL = {
    "rejected:diagnostic_language": "診斷性用語 → 整份退回（422）",
    "rejected:unverified_numbers": "數字不在事實內（強制模式）→ 整份退回（422）",
    "flagged": "數字不在事實內（回報模式）→ 標記並記稽核",
    "rejected:schema": "契約外欄位／超界值 → 422",
    "adjusted": "總評低於規則引擎推算 → 調高並留紀錄",
    "escaped": "注入的 HTML 標記 → 跳脫，不生效",
    "http_401": "金鑰缺漏或錯誤 → 401",
    "http_503": "服務端未設定金鑰 → 503（不放行）",
    "http_429": "超過每日寄送上限 → 429",
}


def fig2(data: dict, out: Path) -> None:
    cases = data["cases"]
    handled = Counter(c["observed"] for c in cases if c["status"] == "PASS" and c["ideal"] != "accepted"
                      and c["observed"] in MECH_LABEL)
    controls = [c for c in cases if c["ideal"] == "accepted"]
    ctrl_ok = sum(1 for c in controls if c["observed"] == "accepted")
    gaps = [c for c in cases if c["status"] == "GAP"]
    rows = [(MECH_LABEL[k], handled[k], BLUE) for k in MECH_LABEL if handled.get(k)]
    rows.sort(key=lambda r: -r[1])           # 由上而下：處理最多的在最上面
    rows += [(f"合法內容（對照組）→ 放行，沒有誤擋", ctrl_ok, AQUA)]
    rows += [("已知缺口：機制擋不住，被放行", len(gaps), WARN)]
    rows = rows[::-1]                        # barh 的 y=0 在下方
    fig = base_fig(11, 6.4)
    ax = fig.add_axes([0.40, 0.14, 0.55, 0.68])
    style_axes(ax, "x")
    for i, (label, n, color) in enumerate(rows):
        ax.barh(i, n, height=0.6, color=color, edgecolor=SURFACE, linewidth=2)
        ax.text(n + 0.3, i, str(n), va="center", fontsize=10, color=INK, fontweight="bold")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows], fontsize=9.5, color=INK)
    ax.set_xlim(0, max(r[1] for r in rows) + 3)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("案例數", fontsize=9, color=INK2)
    bad_total = sum(handled.values())
    headline(fig, f"每道機制各處理了幾筆壞 payload：共 {bad_total} 筆被擋下或標記",
             f"合法對照組 {ctrl_ok}/{len(controls)} 筆全部放行（沒有誤擋）；{len(gaps)} 筆是目前擋不住的缺口"
             "（不含數字的敘述性腦補、涵蓋率不足仍下結論）。")
    footer(fig, "來源：同圖 1。一筆案例只計入一種處理結果；圖中數字為案例數，不是真實事件數。")
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)


# ──────────────────────────────────────────────────────────
# 圖 3：規則校正閉環（真實回測的既有數字）
# ──────────────────────────────────────────────────────────

def fig3(out: Path) -> None:
    fig = base_fig(11, 5.2)
    keys = ["impact", "iso", "workload"]
    widths = [0.30, 0.38, 0.24]
    x0 = 0.05
    for k, w in zip(keys, widths):
        spec = REAL_BACKTEST[k]
        ax = fig.add_axes([x0, 0.16, w - 0.025, 0.58])
        style_axes(ax, "y")
        steps = spec["steps"]
        for i, (lab, v) in enumerate(steps):
            shade = ["#86b6ef", "#3987e5", "#1c5cab"][i] if len(steps) == 3 else ["#86b6ef", "#256abf"][i]
            ax.bar(i, v, width=0.6, color=shade, edgecolor=SURFACE, linewidth=2)
            ax.text(i, v + max(s[1] for s in steps) * 0.02, f"{v:g}", ha="center", fontsize=11, color=INK, fontweight="bold")
        ax.set_xticks(range(len(steps)))
        ax.set_xticklabels([s[0] for s in steps], fontsize=9, color=INK)
        ax.set_ylim(0, max(s[1] for s in steps) * 1.18)
        ax.set_title(spec["title"], fontsize=9.5, color=INK, loc="left", pad=18, wrap=True)
        ax.text(0, 1.02, spec["unit"], transform=ax.transAxes, fontsize=8.5, color=INK2)
        ax.yaxis.set_visible(False)
        x0 += w
    headline(fig, "規則校正閉環：以真實回測數據改變判定規則，三次調整",
             "每一次都是『實測 → 專家會議 → 調整 → 同一批資料重跑驗證』；移除峰度的理由是指標沒有鑑別力，不是為了壓件數。")
    footer(fig, "來源：真實生產資料離線回測（66 台設備、11 週，2026-06-05 起），數字摘自決策紀錄與認證草稿第 3 張，無法在本環境重現。\n"
                "『加下限後』的下限數值是依分布選的起點，不是專家定的標準，由試跑回饋驗證。")
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)


# ──────────────────────────────────────────────────────────
# 圖 4：合成資料的劇本 vs 實際觸發
# ──────────────────────────────────────────────────────────

D = lambda s: dt.datetime.fromisoformat(s)

#: 劇本事件（取自 validate/synthetic.py；起點 2026-05-01 00:00）
PLANTED = {
    "DEV-STABLE": [],
    "DEV-GAP": [("2026-05-06 00:00", "2026-05-06 12:00", "斷線 12 小時（低於 24 小時門檻，不應觸發）"),
                ("2026-05-16 00:00", "2026-05-18 02:00", "斷線 50 小時")],
    "DEV-STANDBY": [("2026-05-21 00:00", "2026-06-24 23:00", "不再運轉（第 20 天起）")],
    "DEV-DEGRADE": [("2026-05-15 00:00", "2026-06-14 00:00", "指標線性劣化（第 14～45 天）")],
    "DEV-ORIENT": [("2026-05-21 00:00", "2026-05-31 00:00", "軸能量分佈形狀跳變（第 20 天起）")],
}
#: 合成資料的長度（天）。資料結束後，系統判定它們逾時無資料是預期行為（它們真的沒有資料了）
DATA_DAYS = {"DEV-STABLE": 30, "DEV-GAP": 30, "DEV-STANDBY": 55, "DEV-DEGRADE": 45, "DEV-ORIENT": 30}
START = dt.datetime(2026, 5, 1)

#: 劇本預期會觸發（validate/synthetic.py 的標準答案）
from validate.synthetic import SCENARIOS  # noqa: E402

EXPECTED = {dev: list(spec["expect_rule_triggered"]) for dev, spec in SCENARIOS.items() if "expect_rule_triggered" in spec}
#: 同一份說明（validate/synthetic.py 檔頭）還寫了這些是劇本的連帶結果，不算「劇本之外」
ALSO_EXPECTED = {"DEV-DEGRADE": ["DEGRADE_TREND", "STEP_CHANGE"], "DEV-ORIENT": ["AXIS_SHIFT"]}


def fig4(replay: dict, out: Path) -> dict:
    events: dict[str, list[dict]] = {d: [] for d in DEVICES}
    for key, day in replay["first_day"].items():
        kind, dev, rule = key.split("|")
        t = dt.datetime.fromisoformat(day)
        data_end = START + dt.timedelta(days=DATA_DAYS[dev])
        after_end = rule == "SENSOR_OFFLINE" and t >= data_end
        expected = rule in EXPECTED.get(dev, [])
        also = rule in ALSO_EXPECTED.get(dev, [])
        events[dev].append({"rule": rule, "kind": kind, "date": day, "expected": expected,
                            "also_expected": also, "after_data_end": after_end,
                            "unplanned": not (expected or also or after_end)})
    for dev in events:
        events[dev].sort(key=lambda e: e["date"])

    step = 1.5
    fig = base_fig(12.5, 7.4)
    ax = fig.add_axes([0.13, 0.14, 0.84, 0.64])
    style_axes(ax, "x")
    ax.set_ylim(len(DEVICES) * step - 0.4, -0.9)
    ax.set_yticks([i * step for i in range(len(DEVICES))])
    ax.set_yticklabels(DEVICES, fontsize=10, color=INK)
    summary = {}
    for i, dev in enumerate(DEVICES):
        y = i * step
        for s_, e_, label in PLANTED[dev]:
            ax.barh(y, mdates.date2num(D(e_)) - mdates.date2num(D(s_)), left=mdates.date2num(D(s_)), height=0.36,
                    color="#e3e2dc", edgecolor="#c9c8c0", linewidth=1, hatch="///", zorder=1)
            ax.text(mdates.date2num(D(s_)), y - 0.26, label, fontsize=8, color=INK2, va="bottom")
        de = START + dt.timedelta(days=DATA_DAYS[dev])
        if de < dt.datetime(2026, 6, 26):
            ax.plot([mdates.date2num(de)] * 2, [y - 0.3, y + 0.3], color=MUTED, linewidth=1.5, zorder=1)
            ax.text(mdates.date2num(de) + 0.4, y - 0.26, "資料結束", fontsize=7.5, color=MUTED, va="bottom")
        for n, ev in enumerate(events[dev]):
            x = mdates.date2num(D(ev["date"]))
            finding = ev["kind"] == "finding"
            if ev["after_data_end"]:
                ax.plot([x], [y], marker="x", markersize=8, color=MUTED, markeredgewidth=2, zorder=3)
                label_color, suffix = MUTED, "（資料結束後，預期）"
            elif ev["unplanned"]:
                ax.plot([x], [y], marker="D" if finding else "o", markersize=9, markerfacecolor=SURFACE,
                        markeredgecolor=ORANGE, markeredgewidth=2, zorder=3)
                label_color, suffix = ORANGE, "（劇本之外）"
            else:
                ax.plot([x], [y], marker="D" if finding else "o", markersize=9,
                        markerfacecolor=CRIT if finding else SURFACE,
                        markeredgecolor=CRIT if finding else BLUE, markeredgewidth=2, zorder=3)
                label_color, suffix = INK, ""
            d_ = dt.datetime.fromisoformat(ev["date"])
            ax.text(x, y + 0.27 + 0.2 * (n % 3), f"{ev['rule']} {d_:%m/%d}{suffix}", fontsize=7.5, color=label_color,
                    ha="center", va="top")
        got = {e["rule"] for e in events[dev] if not e["after_data_end"]}
        summary[dev] = {"expected": EXPECTED.get(dev, []),
                        "expected_missing": [r for r in EXPECTED.get(dev, []) if r not in got],
                        "events": events[dev]}
    ax.set_xlim(mdates.date2num(D("2026-04-29")), mdates.date2num(D("2026-06-28")))
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    n_exp = sum(len(v["expected"]) for v in summary.values())
    n_ok = n_exp - sum(len(v["expected_missing"]) for v in summary.values())
    n_unplanned = sum(1 for evs in events.values() for e in evs if e["unplanned"])
    n_unplanned_dispatch = sum(1 for evs in events.values() for e in evs if e["unplanned"] and e["kind"] == "finding")
    headline(fig, "已知答案的合成資料：劇本事件 vs 正式排程實際觸發",
             f"劇本預期的觸發 {n_exp} 項，正式排程（逐日重放 55 天）實際觸發 {n_ok} 項；劇本之外的觸發 {n_unplanned} 筆"
             f"（其中會派工的 {n_unplanned_dispatch} 筆）。◆ 事項（會派工）　○ 觀察名單（不派工）")
    fig.text(0.04, 0.875, "合成資料是照規則寫的，驗證的是管線行為，不是規則準確度。灰色 × 是合成資料結束後的『逾時無資料』（它們真的沒有資料了）。",
             fontsize=9, color=INK2, va="top")
    ax.legend([plt.Rectangle((0, 0), 1, 1, facecolor="#e3e2dc", edgecolor="#c9c8c0", hatch="///")],
              ["劇本植入的事件（灰色斜線）"], loc="lower right", frameon=False, fontsize=9, labelcolor=INK2)
    footer(fig, "來源：validate.replay_synthetic（5 台設備 × 30～55 天，逐日跑 vibcore.pipeline.daily，每天的資料夾只含當天資料，"
                "第一天後匯入台帳）；DEV-STABLE 為負控制組。橘色＝劇本沒有預期、但實際觸發的項目。")
    fig.savefig(out, facecolor=SURFACE)
    plt.close(fig)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="docs/evidence")
    args = ap.parse_args()
    d = Path(args.dir)
    setup_font()
    scen = json.loads((d / "agent_guard_scenarios.json").read_text(encoding="utf-8"))
    fig1(scen, d / "fig1_guard_scenarios.png")
    fig2(scen, d / "fig2_guard_effect.png")
    fig3(d / "fig3_rule_calibration.png")
    rp = d / "replay_synthetic.json"
    if rp.exists():
        s = fig4(json.loads(rp.read_text(encoding="utf-8")), d / "fig4_synthetic_replay.png")
        (d / "replay_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        for dev, v in s.items():
            evs = ", ".join(f"{e['rule']}{'*' if e['unplanned'] else ''}@{e['date'][5:]}" for e in v["events"])
            print(f"{dev}: 預期 {v['expected']} → 實際 {evs}" + (f"  缺：{v['expected_missing']}" if v["expected_missing"] else ""))
    print(f"圖已寫入 {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
