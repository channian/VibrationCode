"""
templates.py — 週報的 Jinja2 樣板與內嵌 CSS

版面與資訊結構照 `docs/weekly_report_sample.html`；CSS token（`--paper`/
`--ink`/`--crit`/... 的命名與深色模式寫法）直接沿用該範例，理由是那份
範例本身就是設計基準，這裡沒有另立一套的必要。

**樣式必須內嵌**：週報要能作為 email HTML 內文寄送，外部連結的樣式表在
多數郵件用戶端會被整個丟棄。除了 Google Fonts（有明確 fallback，讀不到
時退回系統字型不影響可讀性）之外，所有 CSS 都在同一個 `<style>` 區塊內。

樣板本身不做任何跳脫（escape）決定——那是 render.py 建立 Jinja2
Environment 時開啟 `autoescape=True` 的責任；這裡只負責版面結構。
"""

WEEKLY_REPORT_CSS = r"""
:root {
  --paper:      #F4F6F7;
  --card:       #FFFFFF;
  --band:       #E9EEF0;
  --rule:       #D8E0E3;
  --rule-soft:  #EAEFF1;
  --ink:        #111A20;
  --ink-2:      #3B4C57;
  --ink-3:      #6B7C87;
  --brand:      #23424F;
  --ok:         #1F7A4D;
  --ok-bg:      #E3F2E9;
  --warn:       #8A5D00;
  --warn-bg:    #FBEEDA;
  --crit:       #A32F26;
  --crit-bg:    #FAE5E2;
  --lift:       0 1px 1px rgba(17,26,32,.05), 0 6px 18px -10px rgba(17,26,32,.18);
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper:      #0D1317;
    --card:       #151F25;
    --band:       #1A252C;
    --rule:       #2A373F;
    --rule-soft:  #202C33;
    --ink:        #E6EDF1;
    --ink-2:      #B4C3CC;
    --ink-3:      #8496A2;
    --brand:      #8FB4C4;
    --ok:         #5CB88A;
    --ok-bg:      #14301F;
    --warn:       #D9A648;
    --warn-bg:    #33260C;
    --crit:       #E37C70;
    --crit-bg:    #371915;
    --lift:       0 1px 1px rgba(0,0,0,.4), 0 6px 18px -10px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"] {
  --paper:      #0D1317;
  --card:       #151F25;
  --band:       #1A252C;
  --rule:       #2A373F;
  --rule-soft:  #202C33;
  --ink:        #E6EDF1;
  --ink-2:      #B4C3CC;
  --ink-3:      #8496A2;
  --brand:      #8FB4C4;
  --ok:         #5CB88A;
  --ok-bg:      #14301F;
  --warn:       #D9A648;
  --warn-bg:    #33260C;
  --crit:       #E37C70;
  --crit-bg:    #371915;
  --lift:       0 1px 1px rgba(0,0,0,.4), 0 6px 18px -10px rgba(0,0,0,.7);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 0 20px 80px;
  background: var(--paper);
  color: var(--ink);
  font-family: "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif;
  line-height: 1.8;
  -webkit-font-smoothing: antialiased;
}
.sheet { max-width: 780px; margin: 0 auto; }

.mono { font-family: "IBM Plex Mono", ui-monospace, monospace; font-variant-numeric: tabular-nums; }

header { padding: 34px 0 22px; }
.masthead {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
  border-bottom: 3px solid var(--ink);
  padding-bottom: 12px;
}
.masthead h1 { font-size: 1.35rem; font-weight: 900; letter-spacing: .02em; margin: 0; }
.period { font-size: .82rem; color: var(--ink-3); letter-spacing: .04em; }

.verdict {
  margin-top: 24px;
  background: var(--card);
  border: 1px solid var(--rule);
  border-left: 5px solid var(--ink-3);
  border-radius: 3px;
  box-shadow: var(--lift);
  padding: 22px 24px;
}
.verdict.err { border-left-color: var(--crit); }
.verdict.warn { border-left-color: var(--warn); }
.verdict.ok { border-left-color: var(--ok); }
.verdict .tag {
  display: inline-block;
  font-size: .7rem;
  font-weight: 700;
  letter-spacing: .12em;
  padding: 3px 9px;
  border-radius: 2px;
  background: var(--band);
  color: var(--ink-3);
  margin-bottom: 12px;
}
.verdict.err .tag { background: var(--crit-bg); color: var(--crit); }
.verdict.warn .tag { background: var(--warn-bg); color: var(--warn); }
.verdict.ok .tag { background: var(--ok-bg); color: var(--ok); }
.verdict p { margin: 0; font-size: 1.08rem; font-weight: 500; line-height: 1.75; text-wrap: pretty; }

.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
  gap: 1px;
  background: var(--rule);
  border: 1px solid var(--rule);
  border-radius: 3px;
  margin-top: 18px;
  overflow: hidden;
}
.stat { background: var(--card); padding: 14px 16px; }
.stat .n { font-size: 1.5rem; font-weight: 600; line-height: 1.2; display: block; }
.stat .l { font-size: .76rem; color: var(--ink-3); display: block; margin-top: 2px; }
.stat.c .n { color: var(--crit); }
.stat.w .n { color: var(--warn); }
.stat.g .n { color: var(--ok); }

section { margin-top: 46px; }
.sechead {
  display: flex;
  align-items: baseline;
  gap: 10px;
  border-bottom: 1.5px solid var(--ink);
  padding-bottom: 7px;
  margin-bottom: 18px;
}
.sechead h2 { font-size: 1.06rem; font-weight: 700; margin: 0; letter-spacing: .01em; }
.sechead .count { font-size: .82rem; font-weight: 600; color: var(--ink-3); }
.sechead .src {
  margin-left: auto;
  font-size: .68rem;
  letter-spacing: .06em;
  color: var(--ink-3);
  text-transform: uppercase;
}
.secnote { font-size: .86rem; color: var(--ink-3); margin: -6px 0 18px; }

.item {
  background: var(--card);
  border: 1px solid var(--rule);
  border-left: 4px solid var(--rule);
  border-radius: 3px;
  box-shadow: var(--lift);
  padding: 18px 20px;
  margin-bottom: 12px;
}
.item.err { border-left-color: var(--crit); }
.item.warn { border-left-color: var(--warn); }
.item.ok { border-left-color: var(--ok); }

.row1 { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 7px; }
.sev { font-size: .66rem; font-weight: 700; letter-spacing: .1em; padding: 2px 7px; border-radius: 2px; }
.sev.err { background: var(--crit-bg); color: var(--crit); }
.sev.warn { background: var(--warn-bg); color: var(--warn); }
.sev.ok { background: var(--ok-bg); color: var(--ok); }
.dev { font-size: .84rem; font-weight: 600; color: var(--ink-2); letter-spacing: .02em; }
.loc { font-size: .78rem; color: var(--ink-3); }

.flag {
  font-size: .68rem;
  font-weight: 700;
  letter-spacing: .04em;
  padding: 2px 7px;
  border-radius: 2px;
  border: 1px solid currentColor;
}
.flag.up { color: var(--crit); background: var(--crit-bg); }
.flag.late { color: var(--warn); background: var(--warn-bg); }

.item h3 { font-size: 1.02rem; font-weight: 700; margin: 0 0 8px; line-height: 1.55; }
.item p { margin: 0 0 10px; font-size: .94rem; color: var(--ink-2); line-height: 1.75; }
.item p:last-child { margin-bottom: 0; }

.evidence {
  background: var(--band);
  border-radius: 2px;
  padding: 10px 13px;
  font-size: .84rem;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 20px;
  margin: 0 0 11px;
}
.evidence span { color: var(--ink-3); }
.evidence b { color: var(--ink); font-weight: 600; }

.limit {
  font-size: .84rem;
  color: var(--ink-3);
  border-left: 2px solid var(--rule);
  padding-left: 11px;
  margin: 0 0 11px;
  line-height: 1.7;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 16px;
  font-size: .78rem;
  color: var(--ink-3);
  border-top: 1px solid var(--rule-soft);
  padding-top: 9px;
  margin-top: 11px;
}

.reply {
  background: var(--band);
  border-radius: 2px;
  padding: 11px 13px;
  margin-top: 11px;
  font-size: .87rem;
  line-height: 1.7;
}
.reply .who { font-size: .73rem; color: var(--ink-3); display: block; margin-bottom: 3px; letter-spacing: .03em; }
.reply .said { color: var(--ink-2); }

.quality {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 3px;
  box-shadow: var(--lift);
  padding: 18px 20px;
}
.qbar { display: flex; height: 9px; border-radius: 2px; overflow: hidden; margin: 12px 0 14px; background: var(--rule); }
.qbar i { display: block; height: 100%; }
.qlegend { display: flex; flex-wrap: wrap; gap: 6px 20px; font-size: .8rem; color: var(--ink-3); }
.qlegend b { color: var(--ink); font-weight: 600; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 5px; vertical-align: 0; }
.qlist { list-style: none; padding: 0; margin: 15px 0 0; border-top: 1px solid var(--rule-soft); }
.qlist li { font-size: .88rem; color: var(--ink-2); padding: 9px 0; border-bottom: 1px solid var(--rule-soft); line-height: 1.65; }
.qlist li:last-child { border-bottom: none; }

.notes {
  background: var(--card);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 18px 20px;
  box-shadow: var(--lift);
}
.notes p { margin: 0 0 12px; font-size: .94rem; color: var(--ink-2); line-height: 1.8; }
.notes p:last-child { margin-bottom: 0; }

.empty {
  font-size: .88rem;
  color: var(--ink-3);
  background: var(--band);
  border-radius: 3px;
  padding: 14px 16px;
}

footer {
  margin-top: 46px;
  padding-top: 16px;
  border-top: 1px solid var(--rule);
  font-size: .78rem;
  color: var(--ink-3);
  line-height: 1.7;
}

:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
"""


#: 事項卡片（新發現／追蹤中共用）；resolved 卡片版面較簡單，直接寫在主樣板裡不另立巨集
_FINDING_CARD_MACRO = r"""
{% macro finding_card(item) %}
<article class="item {{ item.severity }}">
  <div class="row1">
    <span class="sev {{ item.severity }}">{{ item.severity_label }}</span>
    <span class="dev mono">{{ item.device_label }}</span>
    {% if item.location %}<span class="loc">{{ item.location }}</span>{% endif %}
    {% for f in item.flags %}<span class="flag {{ f.cls }}">{{ f.label }}</span>{% endfor %}
  </div>
  <h3>{{ item.title }}{{ item.occurrence_suffix }}</h3>
  {% if item.evidence %}
  <div class="evidence">
    {% for label, value in item.evidence %}
    <span>{{ label }} <b class="mono">{{ value }}</b></span>
    {% endfor %}
  </div>
  {% endif %}
  {% if item.narrative %}<p>{{ item.narrative }}</p>{% endif %}
  {% if item.limit_text %}<p class="limit">{{ item.limit_text }}</p>{% endif %}
  {% if item.reply %}
  <div class="reply">
    <span class="who">{{ item.reply.who }}</span>
    <span class="said">{{ item.reply.said }}</span>
  </div>
  {% endif %}
  {% if item.meta %}
  <div class="meta">
    {% for m in item.meta %}
    <span>{{ m.label }} {% if m.plain %}<b>{{ m.value }}</b>{% else %}<b class="mono">{{ m.value }}</b>{% endif %}{% if m.suffix %}{{ m.suffix }}{% endif %}</span>
    {% endfor %}
  </div>
  {% endif %}
</article>
{% endmacro %}
"""


WEEKLY_REPORT_TEMPLATE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>設備振動週報 · {{ period_label }}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@300;400;500;700;900&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>""" + WEEKLY_REPORT_CSS + r"""</style>
</head>
<body>
""" + _FINDING_CARD_MACRO + r"""
<div class="sheet">

<header>
  <div class="masthead">
    <h1>設備振動週報</h1>
    <span class="period mono">{{ period_label }}</span>
  </div>

  <div class="verdict {{ verdict }}">
    <span class="tag">{{ verdict_label }}</span>
    <p>{{ headline }}</p>
  </div>

  <div class="stats">
    {% for t in stat_tiles %}
    <div class="stat {{ t.cls }}"><span class="n mono">{{ t.value }}</span><span class="l">{{ t.label }}</span></div>
    {% endfor %}
  </div>
</header>

<section>
  <div class="sechead">
    <h2>資料品質</h2>
    <span class="src">系統統計</span>
  </div>
  <p class="secnote">涵蓋率不足的量測點，其判定結果不具參考價值，本週報中已排除其結論。</p>

  {% if quality.has_data %}
  <div class="quality">
    <div class="qbar">
      {% for seg in quality.bar_segments %}
      <i style="width:{{ '%.1f'|format(seg.pct) }}%;background:var({{ seg.var }})"></i>
      {% endfor %}
    </div>
    <div class="qlegend">
      {% for leg in quality.legend %}
      <span><i class="dot" style="background:var({{ leg.var }})"></i>{{ leg.label }} <b class="mono">{{ leg.pct_label }}</b></span>
      {% endfor %}
    </div>
    {% if quality.gap_items %}
    <ul class="qlist">
      {% for g in quality.gap_items %}
      <li><b class="mono">{{ g.device_label }}</b>{% if g.location %}　<span class="loc">{{ g.location }}</span>{% endif %}<br>{{ g.sentence }}</li>
      {% endfor %}
    </ul>
    {% endif %}
  </div>
  {% else %}
  <p class="empty">本期無量測資料。</p>
  {% endif %}
</section>

<section>
  <div class="sechead">
    <h2>本週新發現</h2>
    <span class="count mono">{{ new_findings|length }} 件</span>
    <span class="src">系統依資料庫判定</span>
  </div>
  {% if new_findings %}
    {% for item in new_findings %}{{ finding_card(item) }}{% endfor %}
  {% else %}
  <p class="empty">本週無新增事項。</p>
  {% endif %}
</section>

<section>
  <div class="sechead">
    <h2>追蹤中</h2>
    <span class="count mono">{{ tracking_findings|length }} 件</span>
    <span class="src">依簽核進度</span>
  </div>
  {% if tracking_findings %}
    {% for item in tracking_findings %}{{ finding_card(item) }}{% endfor %}
  {% else %}
  <p class="empty">目前無延續中的追蹤事項。</p>
  {% endif %}
</section>

<section>
  <div class="sechead">
    <h2>本週已解決</h2>
    <span class="count mono">{{ resolved_findings|length }} 件</span>
    <span class="src">系統自資料庫擷取</span>
  </div>
  {% if resolved_findings %}
    {% for item in resolved_findings %}
    <article class="item ok">
      <div class="row1">
        <span class="sev ok">{{ item.status_label }}</span>
        <span class="dev mono">{{ item.device_label }}</span>
        {% if item.location %}<span class="loc">{{ item.location }}</span>{% endif %}
      </div>
      <h3>{{ item.title }}</h3>
      {% if item.evidence %}
      <div class="evidence">
        {% for label, value in item.evidence %}
        <span>{{ label }} <b class="mono">{{ value }}</b></span>
        {% endfor %}
      </div>
      {% endif %}
      {% if item.narrative %}<p>{{ item.narrative }}</p>{% endif %}
      {% if item.reply %}
      <div class="reply">
        <span class="who">{{ item.reply.who }}</span>
        <span class="said">{{ item.reply.said }}</span>
      </div>
      {% endif %}
    </article>
    {% endfor %}
  {% else %}
  <p class="empty">本週無事項結案。</p>
  {% endif %}
</section>

<section>
  <div class="sechead">
    <h2>觀察與建議</h2>
    <span class="src">Agent 撰寫</span>
  </div>
  {% if notes_paragraphs %}
  <div class="notes">
    {% for p in notes_paragraphs %}<p>{{ p }}</p>{% endfor %}
  </div>
  {% else %}
  <p class="empty">本週無額外觀察與建議。</p>
  {% endif %}
</section>

<footer>
  本報告由設備振動監測系統自動產生（產出時間 {{ generated_at_label }} UTC+8）。
  異常判定、嚴重度分級與簽核狀態由規則引擎依現行門檻設定計算；
  「觀察與建議」一節由 Agent 依上述判定結果撰寫，不含故障類型判定。
  深度診斷請另行安排專家量測系統。
</footer>

</div>
</body>
</html>
"""
