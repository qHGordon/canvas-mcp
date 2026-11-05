"""Daily-report orchestration: gather (reusing existing logic), diff against the
last report, and assemble the data the email renderer consumes.

Principles: data and computation come entirely from existing functions; the LLM
only summarises announcements (with a template fallback); we report only changes;
Canvas is read-only and we persist only under ``data/``.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import grading, llm, lms, notify, report, schedule
from .crawl import build_assignment_status, build_upcoming, crawl_courses
from .formatters import LOCAL_TZ, WEEKDAY_ZH, weekday_zh  # weekday_zh re-exported here

SUMMARY_FALLBACK_LEN = 150


# ---------------------------------------------------------------------------
# State + report files
# ---------------------------------------------------------------------------

def _reports_dir(base: Path | str | None) -> Path:
    return grading.data_root(base) / "reports"


def _state_path(base: Path | str | None) -> Path:
    return grading.data_root(base) / "report_state.json"


def load_state(base: Path | str | None = None) -> dict | None:
    return grading._read_json(_state_path(base))


def load_prev_report(base: Path | str | None = None) -> dict | None:
    state = load_state(base)
    if not state or not state.get("last_report_path"):
        return None
    return grading._read_json(Path(state["last_report_path"]))


def save_report(
    base: Path | str | None, date: str, report_data: dict, html: str
) -> tuple[Path, Path]:
    d = _reports_dir(base)
    json_path = d / f"report_{date}.json"
    html_path = d / f"report_{date}.html"
    grading._write_json(json_path, report_data)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    return json_path, html_path


def update_state(base: Path | str | None, report_path: Path, sent_at: str) -> None:
    grading._write_json(_state_path(base), {
        "last_report_path": str(report_path),
        "last_sent_at": sent_at,
    })


def deliver(
    config: dict,
    msg: "notify.Message",
    json_path: Path,
    now: datetime,
    base: Path | str | None = None,
    sender=None,
) -> tuple[bool, str | None]:
    """Send, and advance the snapshot ONLY on success. On failure, log (no
    secrets) + Windows toast and leave report_state.json untouched so the next
    run re-reports this run's changes."""
    sender = sender or notify.send
    try:
        sender("email", msg, config)
    except notify.NotifyError as exc:
        notify.log_error(grading.data_root(base), f"发送失败：{exc}")
        notify.windows_toast("Canvas 日报发送失败", str(exc)[:120])
        return False, str(exc)
    update_state(base, json_path, now.astimezone(LOCAL_TZ).isoformat(timespec="seconds"))
    return True, None


# ---------------------------------------------------------------------------
# Announcement summaries (local LLM, with fallback)
# ---------------------------------------------------------------------------

def fallback_summary(text: str) -> str:
    t = (text or "").strip().replace("\r", " ").replace("\n", " ")
    t = " ".join(t.split())
    if not t:
        return ""
    return t[:SUMMARY_FALLBACK_LEN] + ("…" if len(t) > SUMMARY_FALLBACK_LEN else "")


async def summarize_announcements(
    announcements: list[dict[str, Any]],
    use_llm: bool,
    base: Path | str | None,
) -> list[dict[str, Any]]:
    if not announcements:
        return announcements
    model = llm.report_model()
    loaded_by_us = False
    if use_llm:
        try:
            loaded_by_us = lms.ensure_model(model)
        except lms.LmsError as exc:
            notify.log_error(grading.data_root(base), f"LLM 不可用，公告改用模板摘要：{exc}")
            use_llm = False

    out: list[dict[str, Any]] = []
    try:
        for a in announcements:
            body = a.get("message_text") or ""
            summary, summarized = None, False
            if use_llm:
                try:
                    summary = await llm.summarize_announcement(
                        a.get("title") or "", body, model=model, timeout=60.0
                    )
                    summarized = bool(summary and summary.strip())
                except llm.LLMError as exc:
                    notify.log_error(
                        grading.data_root(base),
                        f"公告摘要失败（id={a.get('id')}）：{type(exc).__name__}",
                    )
            if not summarized:
                summary = fallback_summary(body)
            entry = dict(a)
            entry["summary"] = summary
            entry["summarized"] = summarized
            out.append(entry)
    finally:
        if loaded_by_us:
            lms.unload(model)
    return out


# ---------------------------------------------------------------------------
# Deadlines (unsubmitted only), used by the DDL module and the .ics attachments
# ---------------------------------------------------------------------------

def build_deadlines(
    assignments: list[dict[str, Any]],
    now: datetime,
    base_url: str,
    code_by_id: dict[Any, str] | None = None,
) -> list[dict[str, Any]]:
    """Unsubmitted assignments that are overdue or due within 7 days, overdue
    first then by due date."""
    code_by_id = code_by_id or {}
    now_local = now.astimezone(LOCAL_TZ)
    out: list[dict[str, Any]] = []
    for a in assignments:
        due_iso = a.get("due_at")
        if not due_iso or a.get("status") not in ("not_submitted", "unknown"):
            continue
        due = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        overdue = due < now
        if not overdue and (due - now) > timedelta(days=7):
            continue
        if overdue:
            category = "overdue"
        elif (due - now) <= timedelta(hours=48):
            category = "within_48h"
        else:
            category = "within_7d"
        due_local = due.astimezone(LOCAL_TZ)
        cid = a.get("course_id")
        code = a.get("course_code") or code_by_id.get(cid)
        url = (f"{base_url}/courses/{cid}/assignments/{a.get('assignment_id')}"
               if base_url and cid else a.get("html_url"))
        out.append({
            "course": a.get("course"), "course_code": code, "course_id": cid,
            "assignment_id": a.get("assignment_id"), "name": a.get("name"),
            "due_at": due_iso,
            "due_local": due_local.strftime("%Y-%m-%d %H:%M %Z"),
            "weekday": weekday_zh(due_local),
            "days_left": (due_local.date() - now_local.date()).days,
            "category": category, "url": url,
        })
    order = {"overdue": 0, "within_48h": 1, "within_7d": 2}
    out.sort(key=lambda d: (order[d["category"]], d["due_at"]))
    return out


# ---------------------------------------------------------------------------
# Change detection (pure)
# ---------------------------------------------------------------------------

def _index_by(lst: list[dict], key: str) -> dict:
    return {x[key]: x for x in lst if x.get(key) is not None}


def _need(progress: dict, label: str) -> float | None:
    for t in progress.get("targets", []):
        if t["label"] == label:
            return t.get("need_pct")
    return None


def detect_changes(
    prev: dict | None, curr: dict, target: str = "A"
) -> dict[str, Any]:
    """New grades / courseware / announcements and per-course progress deltas.

    On the very first run (no previous report) the list-style changes are empty
    to avoid a one-time flood; only progress + deadlines are shown."""
    if prev is None:
        return {
            "first_run": True, "new_grades": [], "new_files": [],
            "new_announcements": [], "progress_changes": {},
        }

    prev_a = _index_by(prev.get("assignments", []), "assignment_id")
    new_grades = []
    for a in curr.get("assignments", []):
        if a.get("status") != "graded":
            continue
        pa = prev_a.get(a.get("assignment_id"))
        if pa is None or pa.get("status") != "graded" or pa.get("score") != a.get("score"):
            new_grades.append(a)

    prev_f = _index_by(prev.get("files", []), "file_id")
    new_files = []
    for f in curr.get("files", []):
        pf = prev_f.get(f.get("file_id"))
        if pf is None or pf.get("updated_at") != f.get("updated_at"):
            new_files.append(f)

    prev_ann = {a.get("id") for a in prev.get("announcements", [])}
    new_ann = [a for a in curr.get("announcements", []) if a.get("id") not in prev_ann]

    prev_p = _index_by(prev.get("progress", []), "course_id")
    deltas: dict[Any, dict] = {}
    for p in curr.get("progress", []):
        if p.get("status") != "ok":
            continue
        pp = prev_p.get(p.get("course_id"))
        pp_ok = pp if (pp and pp.get("status") == "ok") else {}
        d: dict[str, Any] = {}
        for key in ("earned", "floor", "ceiling"):
            cur, old = p.get(key), pp_ok.get(key)
            d[key] = {"cur": cur, "prev": old,
                      "delta": None if (old is None or cur is None) else round(cur - old, 2)}
        cur_need = _need(p, target)
        old_need = _need(pp_ok, target) if pp_ok else None
        d["need_pct"] = {"cur": cur_need, "prev": old_need,
                         "delta": None if (old_need is None or cur_need is None)
                         else round(cur_need - old_need, 2)}
        deltas[p["course_id"]] = d

    return {
        "first_run": False, "new_grades": new_grades, "new_files": new_files,
        "new_announcements": new_ann, "progress_changes": deltas,
    }


def has_content(changes: dict, deadlines: list) -> bool:
    """Whether there is anything worth an email beyond 'no change'."""
    return bool(
        changes.get("new_grades") or changes.get("new_files")
        or changes.get("new_announcements") or deadlines
    )


# ---------------------------------------------------------------------------
# Gather (async; reuses existing data functions)
# ---------------------------------------------------------------------------

async def gather_report_data(
    client, now: datetime, base: Path | str | None, use_llm: bool
) -> dict[str, Any]:
    base_url = os.environ.get("CANVAS_BASE_URL", "").strip().rstrip("/")

    status = await build_assignment_status(client, now)
    courses = await client.get_paginated(
        "/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]}
    )
    courses = [c for c in courses if isinstance(c, dict) and c.get("name")]
    code_by_id = {c["id"]: c.get("course_code") for c in courses}

    by_course: dict[Any, list] = defaultdict(list)
    for r in status["assignments"]:
        r["course_code"] = code_by_id.get(r.get("course_id"))
        by_course[r["course_id"]].append(r)

    progress = []
    for c in courses:
        p = report.course_progress_from_config(c, by_course.get(c["id"], []), now, base)
        p["generated_at"] = now.astimezone(LOCAL_TZ).isoformat(timespec="seconds")
        grading._write_json(
            grading.course_dir(c.get("course_code"), base) / "progress.json", p
        )
        progress.append(p)

    up = await build_upcoming(days=7, ann_days=7)
    announcements = await summarize_announcements(
        up.get("announcements", []), use_llm, base
    )

    since = (now.astimezone(LOCAL_TZ) - timedelta(days=120)).strftime("%Y-%m-%d")
    index = await crawl_courses(client, since=since)
    files = []
    for node in index.get("courses", []):
        for f in node.get("files", []):
            files.append({
                "course_id": node.get("id"), "course": node.get("name"),
                "course_code": code_by_id.get(node.get("id")),
                "file_id": f.get("id"), "display_name": f.get("display_name"),
                "module": f.get("module", ""), "size": f.get("size"),
                "updated_at": f.get("updated_at"),
            })

    deadlines = build_deadlines(status["assignments"], now, base_url, code_by_id)

    # Recurring class schedule (lectures/tutorials/tests) from each course's
    # confirmed schedule.json, expanded to the next 7 days.
    classes: list[dict[str, Any]] = []
    for c in courses:
        sch = schedule.load_schedule(c.get("course_code"), base)
        if not sch:
            continue
        for occ in schedule.upcoming_sessions(sch.get("sessions", []), now, 7):
            occ["course_code"] = c.get("course_code")
            occ["course"] = c.get("name")
            classes.append(occ)
    classes.sort(key=lambda x: (x["date"], x.get("start") or ""))

    return {
        "date": now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d"),
        "generated_at": now.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
        "assignments": status["assignments"],
        "files": files,
        "announcements": announcements,
        "progress": progress,
        "deadlines": deadlines,
        "classes": classes,
        "errors": (status.get("errors") or []) + (up.get("errors") or []),
    }
