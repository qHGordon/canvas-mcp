"""Render the daily report into Gmail-safe HTML (table layout + inline styles
only — Gmail strips <style> and scripts) plus a plain-text alternative.

Pure functions. All weekdays are derived from dates via daily.weekday_zh; all
times are America/Toronto.
"""

from __future__ import annotations

import html as _html
from datetime import datetime, timedelta
from typing import Any

from .daily import weekday_zh
from .formatters import LOCAL_TZ

GOT, LOST, PENDING, REMAINING = "#1baf7a", "#e34948", "#eda100", "#d3d1c7"
INK, MUTED, LINE, CARD = "#2b2a27", "#726f68", "#e5e3dc", "#ffffff"
MAXW = 600


def _e(s: Any) -> str:
    return _html.escape(str(s if s is not None else ""))


def _num(x: Any, nd: int = 1) -> str:
    if x is None:
        return "—"
    return f"{float(x):.{nd}f}".rstrip("0").rstrip(".")


def _short_code(code: str | None, course: str | None = None) -> str:
    base = code or course or ""
    return base.split()[0] if base else ""


# ---------------------------------------------------------------------------
# Subject
# ---------------------------------------------------------------------------

def build_subject(now: datetime, deadlines: list[dict[str, Any]]) -> str:
    local = now.astimezone(LOCAL_TZ)
    prefix = f"[Canvas 日报 {local.strftime('%m/%d')} {weekday_zh(local)}]"
    if not deadlines:
        return f"{prefix} 今日无紧急事项"
    d = deadlines[0]
    code = _short_code(d.get("course_code"), d.get("course"))
    if d["category"] == "overdue":
        tail = f"⚠ {code} {d['name']} 已逾期"
    else:
        tail = f"⚠ {code} {d['name']} {d['weekday']}截止"
    return f"{prefix} {tail}"


# ---------------------------------------------------------------------------
# Small HTML builders
# ---------------------------------------------------------------------------

def _bar(parts: list[tuple[float, str]]) -> str:
    cells = ""
    for pct, color in parts:
        if pct and pct > 0.01:
            cells += (f'<td style="width:{pct:.3f}%;background:{color};'
                      f'font-size:0;line-height:12px;height:12px">&nbsp;</td>')
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'width="100%" style="border-collapse:collapse;table-layout:fixed;'
            f'border-radius:6px;overflow:hidden"><tr>{cells or "<td>&nbsp;</td>"}</tr></table>')


def _ruler(target_pct: float, label: str) -> str:
    left = max(0.0, min(100.0, target_pct))
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
            f'style="border-collapse:collapse;table-layout:fixed">'
            f'<tr><td style="width:{left:.3f}%;border-right:2px solid {INK};'
            f'height:5px;font-size:0">&nbsp;</td>'
            f'<td style="font-size:0">&nbsp;</td></tr>'
            f'<tr><td colspan="2" style="font-size:11px;color:{MUTED};padding-top:2px">'
            f'目标线 {_e(label)}={_num(target_pct)}</td></tr></table>')


def _arrow(delta: Any) -> str:
    if delta is None or abs(delta) < 0.005:
        return ""
    if delta > 0:
        return f' <span style="color:{GOT}">↑{_num(abs(delta))}</span>'
    return f' <span style="color:{LOST}">↓{_num(abs(delta))}</span>'


def _module_title(text: str) -> str:
    return (f'<tr><td style="padding:16px 0 6px;font:600 15px sans-serif;'
            f'color:{INK};border-top:1px solid {LINE}">{_e(text)}</td></tr>')


# ---------------------------------------------------------------------------
# Module 1: course progress
# ---------------------------------------------------------------------------

def _need_text(progress: dict, target: str) -> str:
    tmap = {t["label"]: t for t in progress.get("targets", [])}
    t = tmap.get(target)
    if not t:
        return ""
    if t["state"] == "locked":
        return f"{target} 已锁定"
    if t["state"] == "impossible":
        # Find the next lower grade that is still reachable.
        lowers = sorted((x for x in progress.get("targets", []) if x["cutoff"] < t["cutoff"]),
                        key=lambda x: -x["cutoff"])
        nxt = next((x for x in lowers if x["state"] == "reachable"), None)
        if nxt:
            return f"{target} 已无法达到（下一可达 {nxt['label']} 需均 {_num(nxt['need_pct'])}%）"
        return f"{target} 已无法达到"
    return f"拿 {target} 剩余需 {_num(t['need_pct'])}%"


def _item_tags(item: dict) -> str:
    tags = ""
    for a in item.get("assignments", []):
        st = a.get("status")
        if st == "counted":
            txt = f'{_num(a.get("score"))}/{_num(a.get("points_possible"))}'
            bg, fg = GOT, "#fff"
        elif st == "pending":
            txt, bg, fg = "待出分", PENDING, "#000"
        elif st == "remaining":
            txt, bg, fg = "未开始", REMAINING, "#000"
        elif st == "excused":
            txt, bg, fg = "豁免", MUTED, "#fff"
        elif st == "dropped":
            txt, bg, fg = "已drop", MUTED, "#fff"
        else:
            txt, bg, fg = st, REMAINING, "#000"
        deco = "text-decoration:line-through;" if st == "dropped" else ""
        tags += (f'<span style="display:inline-block;margin:2px 4px 2px 0;padding:1px 6px;'
                 f'border-radius:4px;background:{bg};color:{fg};font-size:11px;{deco}">'
                 f'{_e(a.get("name") or item.get("name"))}: {_e(txt)}</span>')
    return tags


def _course_progress_block(p: dict, delta: dict | None, target: str) -> str:
    code = _e(p.get("course_code") or p.get("course_name"))
    status = p.get("status")
    if status == "unconfirmed":
        return (f'<tr><td style="padding:8px 0;color:{MUTED}">{code}：权重待确认'
                f'（confirm_grading.py）</td></tr>')
    if status == "ungraded":
        return f'<tr><td style="padding:8px 0;color:{MUTED}">{code}：不计分，已跳过</td></tr>'

    t = p["totals"]
    ea = _arrow((delta or {}).get("earned", {}).get("delta"))
    need = _need_text(p, target)
    need_arrow = _arrow((delta or {}).get("need_pct", {}).get("delta"))
    scale = p.get("grade_scale") or {}
    target_cut = float(scale.get(target, 85))
    head = (f'<b>{code}</b> · 已得 {_num(p.get("floor"))}{ea} · '
            f'区间 {_num(p.get("floor"))}–{_num(p.get("ceiling"))} · {need}{need_arrow}')
    bar = _bar([(t["earned"], GOT), (t["lost"], LOST),
                (t["pending"], PENDING), (t["remaining"], REMAINING)])
    ruler = _ruler(target_cut, target)
    items = ""
    for it in p.get("items", []):
        items += (f'<div style="margin:4px 0"><span style="font-size:12px;color:{MUTED}">'
                  f'{_e(it.get("name"))} {_num(it.get("weight"))}%</span><br>{_item_tags(it)}</div>')
    return (f'<tr><td style="padding:10px 0;border-bottom:1px solid {LINE}">'
            f'<div style="font-size:14px;color:{INK};margin-bottom:6px">{head}</div>'
            f'{bar}{ruler}<div style="margin-top:6px">{items}</div></td></tr>')


def _legend() -> str:
    pills = [("已得", GOT), ("已失", LOST), ("待出分", PENDING), ("未完成", REMAINING)]
    cells = "".join(
        f'<span style="display:inline-block;margin-right:10px;font-size:11px;color:{MUTED}">'
        f'<span style="display:inline-block;width:10px;height:10px;background:{c};'
        f'border-radius:2px;vertical-align:middle"></span> {t}</span>'
        for t, c in pills)
    return f'<tr><td style="padding-bottom:6px">{cells}</td></tr>'


# ---------------------------------------------------------------------------
# Module 2: upcoming deadlines + calendar strip
# ---------------------------------------------------------------------------

def _calendar_strip(now: datetime, deadlines: list[dict], classes: list[dict]) -> str:
    today = now.astimezone(LOCAL_TZ).date()
    # severity by date: red for overdue/48h, yellow otherwise
    sev: dict[Any, str] = {}
    for d in deadlines:
        due = datetime.fromisoformat(d["due_at"].replace("Z", "+00:00")).astimezone(LOCAL_TZ).date()
        s = "red" if d["category"] in ("overdue", "within_48h") else "yellow"
        if sev.get(due) != "red":
            sev[due] = s
    class_dates = {c["date"] for c in classes}
    cells = ""
    for i in range(7):
        day = today + timedelta(days=i)
        bg = CARD
        if sev.get(day) == "red":
            bg = "#fde8e8"
        elif sev.get(day) == "yellow":
            bg = "#fdf4de"
        border = f"2px solid {INK}" if i == 0 else f"1px solid {LINE}"
        mark = ('<br><span style="color:#3b6ea5;font-size:10px">📚课</span>'
                if day.isoformat() in class_dates else "")
        cells += (f'<td align="center" style="width:14.2%;border:{border};background:{bg};'
                  f'padding:6px 2px;font-size:11px;color:{INK}">'
                  f'{weekday_zh(datetime(day.year, day.month, day.day))}<br>'
                  f'<b>{day.month}/{day.day}</b>{mark}</td>')
    return (f'<tr><td style="padding:6px 0"><table role="presentation" width="100%" '
            f'cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed">'
            f'<tr>{cells}</tr></table></td></tr>')


def _class_list(classes: list[dict]) -> str:
    if not classes:
        return ""
    rows = (f'<tr><td style="padding:8px 0 2px;font-size:12px;color:{MUTED}">本周上课</td></tr>')
    for c in classes:
        code = _short_code(c.get("course_code"), c.get("course"))
        when = f'{_e(c.get("start"))}–{_e(c.get("end"))}' if c.get("start") else ""
        loc = f' · {_e(c.get("location"))}' if c.get("location") else ""
        rows += (f'<tr><td style="padding:3px 0;font-size:13px;color:{INK}">'
                 f'<span style="display:inline-block;padding:1px 6px;border-radius:4px;'
                 f'background:#3b6ea5;color:#fff;font-size:11px">{_e(c.get("type_zh"))}</span> '
                 f'<b>{_e(code)}</b> · {_e(c.get("weekday"))} {when}{loc}</td></tr>')
    return rows


_CAT_LABEL = {"overdue": ("逾期", LOST), "within_48h": ("48h内", LOST),
              "within_7d": ("7天内", PENDING)}


def _deadline_list(deadlines: list[dict]) -> str:
    if not deadlines:
        return f'<tr><td style="padding:6px 0;color:{MUTED}">未来 7 天无 DDL</td></tr>'
    rows = ""
    for d in deadlines:
        label, color = _CAT_LABEL.get(d["category"], ("", MUTED))
        code = _short_code(d.get("course_code"), d.get("course"))
        if d["category"] == "overdue":
            left = f'逾期 {abs(d["days_left"])} 天'
        else:
            left = f'剩 {d["days_left"]} 天'
        rows += (f'<tr><td style="padding:4px 0;font-size:13px;color:{INK}">'
                 f'<span style="display:inline-block;padding:1px 6px;border-radius:4px;'
                 f'background:{color};color:#fff;font-size:11px">{label}</span> '
                 f'<b>{_e(code)}</b> · {_e(d["name"])} — {_e(d["due_local"])}'
                 f'（{_e(d["weekday"])}，{left}）</td></tr>')
    rows += (f'<tr><td style="padding-top:6px;color:{MUTED};font-size:12px">'
             f'📎 未提交作业的日历文件见附件（.ics，提前 24h 提醒）</td></tr>')
    return rows


# ---------------------------------------------------------------------------
# Modules 3–5
# ---------------------------------------------------------------------------

def _new_grades(items: list[dict]) -> str:
    rows = ""
    for a in items:
        code = _short_code(a.get("course_code"), a.get("course"))
        pp = a.get("points_possible")
        note = "（满分未设置，需手动补）" if not pp else ""
        rows += (f'<tr><td style="padding:3px 0;font-size:13px">'
                 f'<b>{_e(code)}</b> · {_e(a.get("name"))} — '
                 f'{_num(a.get("score"))}/{_num(pp)} {note}</td></tr>')
    return rows


def _new_files(items: list[dict]) -> str:
    by_course: dict[str, list] = {}
    for f in items:
        by_course.setdefault(f.get("course_code") or f.get("course") or "", []).append(f)
    rows = ""
    for course, fs in by_course.items():
        rows += f'<tr><td style="padding:4px 0 2px;font-size:13px;color:{MUTED}">{_e(course)}</td></tr>'
        for f in fs:
            size = f.get("size")
            size_txt = f"{round(size/1024/1024, 1)}MB" if size else ""
            rows += (f'<tr><td style="padding:2px 0 2px 12px;font-size:13px">'
                     f'{_e(f.get("display_name"))} <span style="color:{MUTED}">· '
                     f'{_e(f.get("module") or "")} {size_txt}</span></td></tr>')
    return rows


def _new_announcements(items: list[dict]) -> str:
    rows = ""
    for a in items:
        date = (a.get("posted_at_local") or a.get("posted_at") or "")[:16]
        tag = "" if a.get("summarized") else (
            f' <span style="color:{MUTED};font-size:11px">[未摘要]</span>')
        url = a.get("html_url") or ""
        link = (f' · <a href="{_e(url)}" style="color:#3b6ea5">在 Canvas 查看原文</a>'
                if url else "")
        rows += (f'<tr><td style="padding:5px 0;font-size:13px;border-bottom:1px solid {LINE}">'
                 f'<b>{_e(a.get("title"))}</b>{tag}<br>'
                 f'<span style="color:{MUTED};font-size:12px">{_e(date)}{link}</span><br>'
                 f'{_e(a.get("summary"))}</td></tr>')
    return rows


# ---------------------------------------------------------------------------
# Top-level render
# ---------------------------------------------------------------------------

def render_email(
    report: dict, changes: dict, now: datetime, config: dict | None = None
) -> tuple[str, str]:
    config = config or {}
    target = config.get("TARGET_GRADE", "A")
    deadlines = report.get("deadlines", [])
    subject = build_subject(now, deadlines)
    content = changes.get("new_grades") or changes.get("new_files") or \
        changes.get("new_announcements") or deadlines

    body = f'<tr><td style="padding:14px 0 4px;font:700 18px sans-serif;color:{INK}">{_e(subject)}</td></tr>'
    if changes.get("first_run"):
        body += (f'<tr><td style="padding:4px 0;color:{MUTED};font-size:12px">'
                 f'首次运行，下次起开始报告变化。</td></tr>')
    elif not content:
        body += (f'<tr><td style="padding:4px 0;color:{MUTED}">今日无变化。</td></tr>')

    # Module 1 — course progress (always)
    body += _module_title("① 课程进度")
    body += _legend()
    delta_map = changes.get("progress_changes", {})
    for p in report.get("progress", []):
        body += _course_progress_block(p, delta_map.get(p.get("course_id")), target)

    # Module 2 — deadlines + class schedule (always)
    classes = report.get("classes", [])
    body += _module_title("② 临近 DDL / 上课")
    body += _calendar_strip(now, deadlines, classes)
    body += _class_list(classes)
    body += _deadline_list(deadlines)

    # Module 3 — new grades
    if changes.get("new_grades"):
        body += _module_title("③ 新出分")
        body += _new_grades(changes["new_grades"])
    # Module 4 — new courseware
    if changes.get("new_files"):
        body += _module_title("④ 新课件")
        body += _new_files(changes["new_files"])
    # Module 5 — new announcements
    if changes.get("new_announcements"):
        body += _module_title("⑤ 新公告")
        body += _new_announcements(changes["new_announcements"])

    foot = _e(report.get("generated_at") or "")
    body += (f'<tr><td style="padding:16px 0 4px;border-top:1px solid {LINE};'
             f'color:{MUTED};font-size:11px">数据时间 {foot}　·　对 Canvas 只读，'
             f'分数由 progress.py 计算</td></tr>')

    html = (f'<!doctype html><html><body style="margin:0;background:#faf9f5">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:#faf9f5"><tr><td align="center">'
            f'<table role="presentation" width="{MAXW}" cellpadding="0" cellspacing="0" '
            f'style="max-width:{MAXW}px;width:100%;background:{CARD};'
            f'padding:0 20px 20px;font-family:sans-serif">{body}</table>'
            f'</td></tr></table></body></html>')
    return html, render_text(report, changes, now, target)


def render_text(report: dict, changes: dict, now: datetime, target: str) -> str:
    lines = [build_subject(now, report.get("deadlines", [])), ""]
    if changes.get("first_run"):
        lines.append("（首次运行，下次起开始报告变化）")
    lines.append("① 课程进度")
    for p in report.get("progress", []):
        if p.get("status") == "unconfirmed":
            lines.append(f"  {p.get('course_code')}：权重待确认")
            continue
        if p.get("status") == "ungraded":
            lines.append(f"  {p.get('course_code')}：不计分，已跳过")
            continue
        lines.append(f"  {p.get('course_code')}：已得 {_num(p.get('floor'))}，"
                     f"区间 {_num(p.get('floor'))}–{_num(p.get('ceiling'))}，{_need_text(p, target)}")
    lines.append("")
    lines.append("② 临近 DDL / 上课")
    classes = report.get("classes", [])
    if classes:
        lines.append("  本周上课：")
        for c in classes:
            lines.append(f"    {c.get('type_zh')} {_short_code(c.get('course_code'), c.get('course'))} "
                         f"{c.get('weekday')} {c.get('start')}–{c.get('end')}"
                         f"{(' · ' + c['location']) if c.get('location') else ''}")
    dls = report.get("deadlines", [])
    if not dls:
        lines.append("  未来 7 天无 DDL")
    for d in dls:
        lines.append(f"  [{_CAT_LABEL.get(d['category'], ('', ''))[0]}] "
                     f"{_short_code(d.get('course_code'), d.get('course'))} · {d['name']} — "
                     f"{d['due_local']}（{d['weekday']}）")
    if changes.get("new_grades"):
        lines += ["", "③ 新出分"] + [
            f"  {_short_code(a.get('course_code'), a.get('course'))} · {a.get('name')} — "
            f"{_num(a.get('score'))}/{_num(a.get('points_possible'))}"
            for a in changes["new_grades"]]
    if changes.get("new_files"):
        lines += ["", "④ 新课件"] + [
            f"  {f.get('course_code')} · {f.get('display_name')}" for f in changes["new_files"]]
    if changes.get("new_announcements"):
        lines += ["", "⑤ 新公告"] + [
            f"  {a.get('title')}: {a.get('summary')}" for a in changes["new_announcements"]]
    lines += ["", f"数据时间 {report.get('generated_at') or ''}"]
    return "\n".join(lines)
