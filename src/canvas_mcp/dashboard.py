"""Render computed progress into a single-file, CDN-free, dark-mode HTML page.

Pure: takes the list of progress dicts (as written to progress.json) and returns
an HTML string. All numbers are read straight from the input — this module never
computes a score. UI copy is in Chinese.
"""

from __future__ import annotations

import html
import json
from typing import Any

from .progress import DASHBOARD_TARGETS, DEFAULT_GRADE_SCALE

COLORS = {
    "earned": "#1baf7a",
    "lost": "#e34948",
    "pending": "#eda100",
    "remaining": "#d3d1c7",
}

_STATUS_LABEL = {
    "counted": "计分",
    "dropped": "已 drop",
    "pending": "待出分",
    "remaining": "未完成",
    "excused": "已豁免",
}


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt(x: Any, nd: int = 1) -> str:
    if x is None:
        return "—"
    return f"{float(x):.{nd}f}".rstrip("0").rstrip(".") if nd else f"{float(x):.0f}"


def _seg(width: float, color: str) -> str:
    if width <= 0:
        return ""
    return f'<span class="seg" style="width:{width:.4f}%;background:{color}"></span>'


def _reach_bar(p: dict[str, Any]) -> str:
    t = p["totals"]
    earned, lost = t["earned"], t["lost"]
    pending, remaining = t["pending"], t["remaining"]
    floor, ceiling = p["floor"], p["ceiling"]
    scale = p.get("grade_scale") or DEFAULT_GRADE_SCALE
    # Target lines (A / A- / B+ by default), switchable; A shown by default.
    lines = ""
    for label in DASHBOARD_TARGETS:
        cutoff = scale.get(label)
        if cutoff is None:
            continue
        active = " active" if label == "A" else ""
        lines += (
            f'<button class="tline-btn{active}" data-cut="{cutoff}" '
            f'data-label="{_esc(label)}">{_esc(label)} {_fmt(cutoff)}</button>'
        )
    default_cut = scale.get("A", 85)
    return f"""
    <div class="reach">
      <div class="bar reachbar">
        {_seg(earned, COLORS['earned'])}
        {_seg(lost, COLORS['lost'])}
        {_seg(pending, COLORS['pending'])}
        {_seg(remaining, COLORS['remaining'])}
        <span class="target-line" style="left:{float(default_cut):.2f}%"></span>
        <span class="floor-tick" style="left:{floor:.2f}%" title="保底 {_fmt(floor)}"></span>
        <span class="ceil-tick" style="left:{ceiling:.2f}%" title="上限 {_fmt(ceiling)}"></span>
      </div>
      <div class="bar-caption">
        <span>保底 <b>{_fmt(floor)}</b></span>
        <span>可能区间 <b>{_fmt(floor)}–{_fmt(ceiling)}</b></span>
        <span>目标线：{lines}</span>
      </div>
    </div>"""


def _stat_cards(p: dict[str, Any]) -> str:
    avg = p.get("graded_avg")
    target_a = next((t for t in p["targets"] if t["label"] == "A"), None)
    need_txt, need_cls = _need_text(target_a)
    locked = p["totals"]["earned"] + p["totals"]["lost"]
    return f"""
    <div class="cards">
      <div class="card"><div class="card-val">{_fmt(avg)}%</div>
        <div class="card-lbl">已评部分平均</div></div>
      <div class="card"><div class="card-val {need_cls}" data-need-card="1">{need_txt}</div>
        <div class="card-lbl">达到 <span data-need-label>A</span> 剩余需平均</div></div>
      <div class="card"><div class="card-val">{_fmt(locked)}</div>
        <div class="card-lbl">已锁定分数（得 {_fmt(p['totals']['earned'])} / 失 {_fmt(p['totals']['lost'])}）</div></div>
    </div>"""


def _need_text(target: dict[str, Any] | None) -> tuple[str, str]:
    if not target:
        return "—", ""
    if target["state"] == "locked":
        return "已锁定", "ok"
    if target["state"] == "impossible":
        return "无法达到", "bad"
    return f"{_fmt(target.get('need_pct'))}%", ""


def _item_row(it: dict[str, Any]) -> str:
    weight = it["weight"]
    e, l, pd, rm = it["earned"], it["lost"], it["pending"], it["remaining"]
    # Mini bar scaled to the item's own weight.
    scale = (100.0 / weight) if weight else 0
    rules = it.get("rules") or {}
    rule_tags = ""
    if rules.get("drop_lowest"):
        rule_tags += f'<span class="tag">drop 低 {rules["drop_lowest"]}</span>'
    if rules.get("drop_highest"):
        rule_tags += f'<span class="tag">drop 高 {rules["drop_highest"]}</span>'
    if it.get("drop_tentative"):
        rule_tags += '<span class="tag tentative">drop 暂定</span>'

    subs = it.get("assignments") or []
    has_subs = len(subs) > 0
    sub_html = ""
    for a in subs:
        st = a.get("status")
        label = _STATUS_LABEL.get(st, st)
        cls = f"sub-{st}"
        score_txt = ""
        if a.get("score") is not None:
            score_txt = f'{_fmt(a.get("score"))}/{_fmt(a.get("points_possible"))}'
            if a.get("source") == "manual":
                score_txt += "（手动）"
        reason = ""
        if st == "dropped":
            tentative = "（暂定）" if a.get("drop_tentative") else ""
            reason = f'<span class="drop-note">已 drop{tentative}·{_esc(a.get("drop_reason", ""))}</span>'
        name_cls = "struck" if st == "dropped" else ""
        sub_html += (
            f'<div class="sub"><span class="{name_cls}">{_esc(a.get("name"))}</span>'
            f'<span class="sub-right"><span class="badge {cls}">{_esc(label)}</span>'
            f'{reason}<span class="sub-score">{score_txt}</span></span></div>'
        )

    toggle = (
        f'<button class="toggle" aria-expanded="false">▸</button>' if has_subs else '<span class="toggle-sp"></span>'
    )
    detail = f'<div class="subs">{sub_html}</div>' if has_subs else ""
    return f"""
      <div class="item">
        <div class="item-head">
          {toggle}
          <div class="item-name">{_esc(it['name'])}<span class="weight">{_fmt(weight)}%</span>{rule_tags}</div>
          <div class="bar minibar">
            {_seg(e * scale, COLORS['earned'])}
            {_seg(l * scale, COLORS['lost'])}
            {_seg(pd * scale, COLORS['pending'])}
            {_seg(rm * scale, COLORS['remaining'])}
          </div>
          <div class="item-num">得 {_fmt(e)} · 失 {_fmt(l)} · 待 {_fmt(pd)} · 余 {_fmt(rm)}</div>
        </div>
        {detail}
      </div>"""


def _course_block(p: dict[str, Any]) -> str:
    name = _esc(p.get("course_name") or p.get("course_code"))
    status = p.get("status")
    if status == "unconfirmed":
        return f"""
    <section class="course unconfirmed">
      <h2>{name}</h2>
      <div class="notice">权重待确认，运行 <code>confirm_grading.py --course-id {_esc(p.get('course_id'))}</code></div>
    </section>"""
    if status == "ungraded":
        return f"""
    <section class="course ungraded">
      <h2>{name}</h2>
      <div class="notice">此课程不计成绩（graded: false），已跳过。</div>
    </section>"""

    targets_json = html.escape(json.dumps(
        {t["label"]: t for t in p.get("targets", [])}, ensure_ascii=False
    ), quote=True)
    items = "".join(_item_row(it) for it in p.get("items", []))
    return f"""
    <section class="course" data-targets="{targets_json}" data-earned="{p['totals']['earned']}"
             data-headroom="{p['totals']['pending'] + p['totals']['remaining']}">
      <h2>{name}</h2>
      {_reach_bar(p)}
      {_stat_cards(p)}
      <div class="items-head">syllabus 分项明细</div>
      <div class="items">{items}</div>
    </section>"""


_CSS = """
:root{--bg:#faf9f5;--fg:#2b2a27;--muted:#726f68;--card:#fff;--line:#e5e3dc;--accent:#3b6ea5}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a18;--fg:#eceae4;--muted:#9c988e;--card:#262420;--line:#3a3833;--accent:#7aa7d6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,"Segoe UI",system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:18px;margin:0 0 14px}
.course{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin:18px 0}
.unconfirmed,.ungraded{opacity:.9}
.notice{color:var(--muted);font-size:14px}
.bar{position:relative;display:flex;height:16px;border-radius:8px;overflow:hidden;background:var(--line)}
.reachbar{height:22px;border-radius:11px;overflow:visible}
.reachbar .seg:first-child{border-radius:11px 0 0 11px}
.seg{display:block;height:100%}
.target-line{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--fg)}
.floor-tick,.ceil-tick{position:absolute;top:-7px;width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent}
.floor-tick{border-top:7px solid var(--accent)}
.ceil-tick{border-top:7px solid var(--muted)}
.bar-caption{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin-top:10px}
.bar-caption b{color:var(--fg)}
.tline-btn{border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:7px;padding:2px 8px;margin-left:4px;cursor:pointer;font-size:12px}
.tline-btn.active{border-color:var(--accent);color:var(--fg)}
.cards{display:flex;gap:12px;margin:18px 0}
.card{flex:1;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px 14px;text-align:center}
.card-val{font-size:22px;font-weight:650}
.card-val.ok{color:#1baf7a}.card-val.bad{color:#e34948}
.card-lbl{color:var(--muted);font-size:12px;margin-top:4px}
.items-head{color:var(--muted);font-size:13px;margin:16px 0 8px}
.item{border-top:1px solid var(--line);padding:10px 0}
.item-head{display:grid;grid-template-columns:22px minmax(150px,1.3fr) 1fr auto;gap:12px;align-items:center}
.item-name{font-weight:600}
.weight{color:var(--muted);font-weight:400;margin-left:8px;font-size:13px}
.tag{display:inline-block;background:var(--line);color:var(--muted);border-radius:5px;padding:1px 6px;margin-left:6px;font-size:11px}
.tag.tentative{color:#eda100}
.minibar{height:12px}
.item-num{color:var(--muted);font-size:12px;white-space:nowrap}
.toggle{background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:0}
.toggle-sp{display:inline-block;width:12px}
.subs{display:none;margin:8px 0 2px 34px}
.subs.open{display:block}
.sub{display:flex;justify-content:space-between;gap:10px;padding:4px 0;font-size:13px;border-top:1px dashed var(--line)}
.sub-right{display:flex;gap:8px;align-items:center}
.struck{text-decoration:line-through;color:var(--muted)}
.badge{border-radius:5px;padding:1px 7px;font-size:11px;color:#fff}
.sub-counted{background:#1baf7a}.sub-dropped{background:#9c988e}.sub-pending{background:#eda100}
.sub-remaining{background:#d3d1c7;color:#2b2a27}.sub-excused{background:var(--accent)}
.drop-note{color:var(--muted);font-size:11px}
.sub-score{color:var(--muted);font-variant-numeric:tabular-nums}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:30px}
"""

_JS = """
document.querySelectorAll('.toggle').forEach(function(b){
  b.addEventListener('click',function(){
    var subs=b.closest('.item').querySelector('.subs');
    if(!subs)return;var open=subs.classList.toggle('open');
    b.textContent=open?'▾':'▸';b.setAttribute('aria-expanded',open);
  });
});
document.querySelectorAll('.course[data-targets]').forEach(function(sec){
  var targets=JSON.parse(sec.getAttribute('data-targets'));
  var earned=parseFloat(sec.getAttribute('data-earned'));
  var headroom=parseFloat(sec.getAttribute('data-headroom'));
  var line=sec.querySelector('.target-line');
  var card=sec.querySelector('[data-need-card]');
  var lbl=sec.querySelector('[data-need-label]');
  sec.querySelectorAll('.tline-btn').forEach(function(btn){
    btn.addEventListener('click',function(){
      sec.querySelectorAll('.tline-btn').forEach(function(x){x.classList.remove('active')});
      btn.classList.add('active');
      var cut=parseFloat(btn.getAttribute('data-cut'));
      var label=btn.getAttribute('data-label');
      if(line)line.style.left=cut+'%';
      if(lbl)lbl.textContent=label;
      var t=targets[label]||{};card.classList.remove('ok','bad');
      if(t.state==='locked'){card.textContent='已锁定';card.classList.add('ok');}
      else if(t.state==='impossible'){card.textContent='无法达到';card.classList.add('bad');}
      else{card.textContent=(t.need_pct==null?'—':t.need_pct)+'%';}
    });
  });
});
"""


def render_html(progress_list: list[dict[str, Any]], generated_at: str | None) -> str:
    blocks = "".join(_course_block(p) for p in progress_list)
    foot = _esc(generated_at or "")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>课程成绩进度</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>课程成绩进度</h1>
{blocks}
<footer>数据时间：{foot}　·　对 Canvas 只读，分数由 progress.py 计算</footer>
</div><script>{_JS}</script></body></html>"""
