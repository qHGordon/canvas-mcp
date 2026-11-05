"""Reusable course-crawling and report helpers, shared by scripts/ and tests.

Design notes (see CLAUDE.md "Canvas API 实战经验"):

- ``/courses/{id}/files`` returning 403 only means the Files tab is hidden;
  the same files are still reachable through module items of type "File",
  whose ``url`` field is the file's own API endpoint.
- Every per-course section is fetched with try/continue semantics: a 401/403/
  404 on one section is recorded in that course's ``errors`` list and never
  aborts the crawl.
- ExternalUrl / ExternalTool items live outside Canvas; only their links are
  recorded, never fetched.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .client import CanvasAPIError, CanvasClient
from . import formatters as fmt

CSV_COLUMNS = [
    "course_id", "course_name", "file_id", "display_name", "size",
    "content_type", "updated_at", "source", "module", "url",
]


async def _try(coro, errors: list[dict[str, Any]], section: str):
    """Await coro; on CanvasAPIError record it under `section` and return None."""
    try:
        return await coro
    except CanvasAPIError as exc:
        errors.append(
            {"section": section, "status": exc.status_code, "error": str(exc)}
        )
        return None


async def crawl_course(
    client: CanvasClient, course: dict[str, Any], since: str
) -> dict[str, Any]:
    """Crawl one course into a tree node: tabs, modules+items, files (deduped
    by file id, recovered via modules when /files is hidden), pages,
    assignments, announcements since `since` (YYYY-MM-DD)."""
    cid = course["id"]
    errors: list[dict[str, Any]] = []
    term = course.get("term")
    node: dict[str, Any] = {
        "id": cid,
        "name": course.get("name"),
        "course_code": course.get("course_code"),
        "term": term.get("name") if isinstance(term, dict) else term,
        "tabs": [],
        "modules": [],
        "files": [],
        "pages": [],
        "assignments": [],
        "announcements": [],
        "errors": errors,
    }

    tabs = await _try(
        client.get_paginated(f"/api/v1/courses/{cid}/tabs"), errors, "tabs"
    )
    if tabs:
        node["tabs"] = [
            {"id": t.get("id"), "label": t.get("label")}
            for t in tabs
            if isinstance(t, dict) and not t.get("hidden")
        ]

    files_by_id: dict[Any, dict[str, Any]] = {}
    files = await _try(
        client.get_paginated(f"/api/v1/courses/{cid}/files"), errors, "files"
    )
    for f in files or []:
        slim = fmt.slim_file(f)
        slim["source"] = "files"
        files_by_id[slim.get("id")] = slim

    modules = await _try(
        client.get_paginated(
            f"/api/v1/courses/{cid}/modules", {"include[]": ["items"]}
        ),
        errors,
        "modules",
    )
    for m in modules or []:
        node["modules"].append(fmt.slim_module(m))
        for item in m.get("items") or []:
            if item.get("type") != "File" or not item.get("url"):
                continue
            file_id = item.get("content_id")
            if file_id is not None and file_id in files_by_id:
                continue  # already known from /files; skip the extra request
            detail = await _try(
                client.get(item["url"]),
                errors,
                f"module_file:{item.get('title')}",
            )
            if detail:
                slim = fmt.slim_file(detail)
                slim["source"] = "module"
                slim["module"] = m.get("name")
                files_by_id.setdefault(slim.get("id"), slim)
    node["files"] = sorted(
        files_by_id.values(), key=lambda f: f.get("display_name") or ""
    )

    pages = await _try(
        client.get_paginated(f"/api/v1/courses/{cid}/pages"), errors, "pages"
    )
    node["pages"] = [
        fmt.with_local_times(
            fmt.pick(p, "page_id", "url", "title", "updated_at"), "updated_at"
        )
        for p in pages or []
    ]

    assignments = await _try(
        client.get_paginated(
            f"/api/v1/courses/{cid}/assignments", {"include[]": ["submission"]}
        ),
        errors,
        "assignments",
    )
    node["assignments"] = [fmt.slim_assignment(a) for a in assignments or []]

    announcements = await _try(
        client.get_paginated(
            "/api/v1/announcements",
            {"context_codes[]": f"course_{cid}", "start_date": since},
        ),
        errors,
        "announcements",
    )
    node["announcements"] = [fmt.slim_announcement(a) for a in announcements or []]
    return node


async def crawl_courses(
    client: CanvasClient, since: str, course_id: int | None = None
) -> dict[str, Any]:
    """Crawl all active courses (or just `course_id`) into an index tree."""
    courses = await client.get_paginated(
        "/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]}
    )
    courses = [c for c in courses if isinstance(c, dict) and c.get("name")]
    if course_id is not None:
        courses = [c for c in courses if c.get("id") == course_id]
    return {
        "since": since,
        "courses": [await crawl_course(client, c, since) for c in courses],
    }


def files_to_rows(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten an index tree into CSV rows (one per file)."""
    rows = []
    for course in index.get("courses", []):
        for f in course.get("files", []):
            rows.append({
                "course_id": course.get("id"),
                "course_name": course.get("name"),
                "file_id": f.get("id"),
                "display_name": f.get("display_name"),
                "size": f.get("size"),
                "content_type": f.get("content_type"),
                "updated_at": f.get("updated_at"),
                "source": f.get("source"),
                "module": f.get("module", ""),
                "url": f.get("url"),
            })
    return rows


async def build_upcoming(days: int = 14, ann_days: int = 7) -> dict[str, Any]:
    """Deadlines due within `days` plus announcements from the last `ann_days`
    across all active courses. Uses the server's tool functions (env-configured
    client); per-course failures land in ``errors``."""
    from . import server  # deferred: pulls in the MCP SDK, only needed here

    result: dict[str, Any] = {
        "days": days,
        "announcement_days": ann_days,
        "deadlines": [],
        "announcements": [],
        "errors": [],
    }
    deadlines = await server.get_upcoming_deadlines(days=days)
    if "error" in deadlines:
        result["errors"].append(deadlines["error"])
    else:
        result["deadlines"] = deadlines.get("deadlines", [])
        result["errors"].extend(deadlines.get("partial_errors", []))

    courses = await server.list_courses()
    if isinstance(courses, dict):  # error payload
        result["errors"].append(courses.get("error"))
        courses = []
    for course in courses:
        anns = await server.list_announcements(course["id"], days=ann_days)
        if isinstance(anns, dict):
            result["errors"].append(anns.get("error"))
            continue
        for a in anns:
            a["course"] = course.get("name")
            a["course_id"] = course["id"]
            result["announcements"].append(a)
    result["announcements"].sort(key=lambda a: a.get("posted_at") or "", reverse=True)
    return result


# ---------------------------------------------------------------------------
# Assignment submission status
#
# Canvas 的"模块完成状态"(module state / completion_requirement) 只代表浏览进度，
# 绝不能用来判断作业是否提交。真正的提交状态只能来自 submission 对象。见 CLAUDE.md
# "API 实战经验"。本节把 submission 对象按固定规则映射成有限枚举，判断在 Python 里
# 完成，不交给模型推断。
# ---------------------------------------------------------------------------

STATUS_ZH = {
    "not_submitted": "未完成",
    "submitted_ungraded": "已提交·未出分",
    "graded": "已出分",
    "excused": "已豁免",
    "unknown": "无法判断",
}

# submission_types 全落在这些类型里且没有 submitted_at 时，Canvas 无法记录提交动作，
# 只能判为 unknown（例如纸面提交、无需提交、外部工具）。
_OPAQUE_SUBMISSION_TYPES = {"on_paper", "none", "external_tool"}

# 模块里标题像作业讲义的 File 条目（assignment / 作业 / hw / A<数字>）。
_ASSIGNMENT_TITLE_RE = re.compile(
    r"assignment|作业|\bhw\s*\d+\b|\bhw\b|\bA\d+\b", re.IGNORECASE
)


def _due_passed(due_at: str | None, now: datetime) -> bool:
    if not due_at:
        return False
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due < now


def compute_status(
    assignment: dict[str, Any], submission: dict[str, Any], now: datetime
) -> tuple[str, list[str], list[str]]:
    """Map an (assignment, submission) pair to (status, flags, notes).

    Rules (see CLAUDE.md); evaluated top to bottom:
      excused=true                                        -> excused
      no submitted_at and submission_types ⊆ opaque set   -> unknown
      no submitted_at                                     -> not_submitted
          (+ "overdue" flag if missing=true or due_at past)
      submitted, score & grade both empty                 -> submitted_ungraded
          (+ "可能已批改未公布" note if posted_at empty)
      submitted, score (or grade) present                 -> graded
      any submitted assignment with late=true             -> "late" flag
    """
    assignment = assignment or {}
    submission = submission or {}
    flags: list[str] = []
    notes: list[str] = []

    if submission.get("excused"):
        return "excused", flags, notes

    submitted_at = submission.get("submitted_at")
    sub_types = assignment.get("submission_types") or []

    if not submitted_at:
        if sub_types and set(sub_types) <= _OPAQUE_SUBMISSION_TYPES:
            return "unknown", flags, notes
        if submission.get("missing") or _due_passed(assignment.get("due_at"), now):
            flags.append("overdue")
        return "not_submitted", flags, notes

    if submission.get("late"):
        flags.append("late")

    score = submission.get("score")
    grade = submission.get("grade")
    if score is None and grade in (None, ""):
        if not submission.get("posted_at"):
            notes.append("可能已批改未公布")
        return "submitted_ungraded", flags, notes
    return "graded", flags, notes


def status_record(
    course: dict[str, Any],
    assignment: dict[str, Any],
    submission: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """One assignment's status row: course, name, due_at (+local), status,
    中文状态, flags, submitted_at (+local), score/points_possible, grade."""
    assignment = assignment or {}
    submission = submission or {}
    status, flags, notes = compute_status(assignment, submission, now)
    rec: dict[str, Any] = {
        "course": course.get("name"),
        "course_id": course.get("id"),
        "assignment_id": assignment.get("id"),
        "name": assignment.get("name"),
        "due_at": assignment.get("due_at"),
        "status": status,
        "status_zh": STATUS_ZH[status],
    }
    if flags:
        rec["flags"] = flags
    if notes:
        rec["notes"] = notes
    if submission.get("submitted_at"):
        rec["submitted_at"] = submission["submitted_at"]
    score = submission.get("score")
    if score is not None:
        rec["score"] = score
        rec["points_possible"] = assignment.get("points_possible")
    grade = submission.get("grade")
    if grade not in (None, ""):
        rec["grade"] = grade
    return fmt.with_local_times(rec, "due_at", "submitted_at")


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _nums(s: str | None) -> set[str]:
    return set(re.findall(r"\d+", s or ""))


def _file_matches_assignment(file_title: str, assignment_names: list[str]) -> bool:
    """True if a module File title plausibly corresponds to a Canvas assignment.

    Matches on normalized-substring containment or a shared integer (e.g. the
    "2" in "A2 handout.pdf" and "Assignment 2")."""
    stem = file_title.rsplit(".", 1)[0]
    fnorm = _norm(stem)
    fnums = _nums(stem)
    for name in assignment_names:
        nnorm = _norm(name)
        if not nnorm:
            continue
        if fnorm and (fnorm in nnorm or nnorm in fnorm):
            return True
        if fnums and fnums & _nums(name):
            return True
    return False


async def course_assignment_status(
    client: CanvasClient, course: dict[str, Any], now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Status rows + unmatched module files + errors for one course.

    Primary source: ``/students/submissions?student_ids[]=self&include[]=assignment``
    (每个 submission 带 assignment)。403 时退回 ``/assignments?include[]=submission``
    (每个 assignment 带 submission)。两条路都归一成 (assignment, submission) 对。"""
    cid = course["id"]
    cname = course.get("name")
    errors: list[dict[str, Any]] = []
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

    subs = await _try(
        client.get_paginated(
            f"/api/v1/courses/{cid}/students/submissions",
            {"student_ids[]": ["self"], "include[]": ["assignment"]},
        ),
        errors,
        "submissions",
    )
    if subs is not None:
        for s in subs:
            if not isinstance(s, dict):
                continue
            a = s.get("assignment")
            if isinstance(a, dict):
                pairs.append((a, s))
    else:
        # 403 fallback: assignments carry an embedded submission.
        assigns = await _try(
            client.get_paginated(
                f"/api/v1/courses/{cid}/assignments", {"include[]": ["submission"]}
            ),
            errors,
            "assignments",
        )
        for a in assigns or []:
            if not isinstance(a, dict):
                continue
            sub = a.get("submission")
            pairs.append((a, sub if isinstance(sub, dict) else {}))

    records = [status_record(course, a, s, now) for a, s in pairs]
    records.sort(key=lambda r: (r.get("due_at") or "9999", r.get("name") or ""))
    assignment_names = [a.get("name") for a, _ in pairs if a.get("name")]

    unmatched: list[dict[str, Any]] = []
    modules = await _try(
        client.get_paginated(
            f"/api/v1/courses/{cid}/modules", {"include[]": ["items"]}
        ),
        errors,
        "modules",
    )
    for m in modules or []:
        for item in m.get("items") or []:
            if item.get("type") != "File":
                continue
            title = item.get("title") or ""
            if not _ASSIGNMENT_TITLE_RE.search(title):
                continue
            if _file_matches_assignment(title, assignment_names):
                continue
            unmatched.append({
                "course": cname,
                "course_id": cid,
                "title": title,
                "module": m.get("name"),
                "type": "File",
                "note": "Canvas 上无提交入口，无法判断状态",
            })

    for e in errors:
        e.setdefault("course", cname)
        e.setdefault("course_id", cid)
    return records, unmatched, errors


async def build_assignment_status(
    client: CanvasClient, now: datetime, course_id: int | None = None
) -> dict[str, Any]:
    """Compute assignment submission status across all active courses (or just
    ``course_id``). Per-course failures land in ``errors``; the top-level course
    list failure propagates as a CanvasAPIError for the caller to handle."""
    courses = await client.get_paginated(
        "/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]}
    )
    courses = [c for c in courses if isinstance(c, dict) and c.get("name")]
    if course_id is not None:
        courses = [c for c in courses if c.get("id") == course_id]

    assignments: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for c in courses:
        recs, unm, errs = await course_assignment_status(client, c, now)
        assignments.extend(recs)
        unmatched.extend(unm)
        errors.extend(errs)
    return {
        "assignments": assignments,
        "unmatched_assignment_files": unmatched,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Grading-scheme sources (for extract_grading / amend)
# ---------------------------------------------------------------------------

_SYLLABUS_RE = re.compile(r"syllabus|课程大纲|教学大纲|大纲|评分|grading", re.IGNORECASE)


async def course_assignments_brief(
    client: CanvasClient, cid: int, errors: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """A flat [{id, name}] list of a course's assignments, for mapping context.

    Prefers assignment_groups (works even when weights are off); falls back to
    /assignments."""
    groups = await _try(
        client.get_paginated(
            f"/api/v1/courses/{cid}/assignment_groups", {"include[]": ["assignments"]}
        ),
        errors if errors is not None else [],
        "assignment_groups",
    )
    brief: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for g in groups or []:
        for a in g.get("assignments") or []:
            aid = a.get("id")
            if aid is not None and aid not in seen:
                seen.add(aid)
                brief.append({"id": aid, "name": a.get("name")})
    if brief:
        return brief
    assigns = await _try(
        client.get_paginated(f"/api/v1/courses/{cid}/assignments"),
        errors if errors is not None else [],
        "assignments",
    )
    return [
        {"id": a.get("id"), "name": a.get("name")}
        for a in assigns or [] if isinstance(a, dict) and a.get("id") is not None
    ]


async def find_syllabus_file(
    client: CanvasClient, cid: int, errors: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Locate a syllabus-like File item in the modules and return its file
    detail (with a download ``url``), or None."""
    modules = await _try(
        client.get_paginated(
            f"/api/v1/courses/{cid}/modules", {"include[]": ["items"]}
        ),
        errors, "modules",
    )
    for m in modules or []:
        for item in m.get("items") or []:
            if item.get("type") != "File" or not item.get("url"):
                continue
            if not _SYLLABUS_RE.search(item.get("title") or ""):
                continue
            detail = await _try(
                client.get(item["url"]), errors, f"syllabus_file:{item.get('title')}"
            )
            if detail:
                return detail
    return None


async def fetch_grading_context(
    client: CanvasClient, course: dict[str, Any]
) -> dict[str, Any]:
    """Everything extract_grading needs for one course, in one call:
    course meta (syllabus_body + apply_assignment_group_weights), weighted
    assignment_groups (if weights are on), a flat assignment list, and a
    syllabus File (if one is reachable). Per-section failures land in errors."""
    cid = course["id"]
    errors: list[dict[str, Any]] = []
    meta = await _try(
        client.get(f"/api/v1/courses/{cid}", {"include[]": ["syllabus_body"]}),
        errors, "course_meta",
    ) or {}
    groups = None
    if meta.get("apply_assignment_group_weights"):
        groups = await _try(
            client.get_paginated(
                f"/api/v1/courses/{cid}/assignment_groups",
                {"include[]": ["assignments"]},
            ),
            errors, "assignment_groups",
        )
    assignments = await course_assignments_brief(client, cid, errors)
    syllabus_file = await find_syllabus_file(client, cid, errors)
    return {
        "meta": meta,
        "groups": groups,
        "assignments": assignments,
        "syllabus_file": syllabus_file,
        "errors": errors,
    }
