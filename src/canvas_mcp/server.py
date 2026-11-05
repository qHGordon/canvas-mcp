"""FastMCP entry point exposing read-only Canvas LMS tools over stdio."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import CanvasAPIError, CanvasClient, CanvasConfigError, load_config
from . import crawl
from . import grading as grading_mod
from . import report
from . import formatters as fmt

mcp = MCPServer("canvas-mcp")

_client: CanvasClient | None = None

ASSIGNMENT_BUCKETS = {"upcoming", "past", "undated", "unsubmitted", "all"}


def get_client() -> CanvasClient:
    global _client
    if _client is None:
        _client = CanvasClient.from_env()
    return _client


def _error(exc: CanvasAPIError) -> dict[str, Any]:
    return {"error": str(exc), "status_code": exc.status_code}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _active_courses() -> list[dict[str, Any]]:
    return await get_client().get_paginated(
        "/api/v1/courses",
        {"enrollment_state": "active", "include[]": ["term"]},
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def whoami(raw: bool = False) -> dict[str, Any]:
    """Fetch the Canvas profile of the token's owner. Use to verify the token works."""
    try:
        user = await get_client().get("/api/v1/users/self")
    except CanvasAPIError as exc:
        return _error(exc)
    return user if raw else fmt.slim_user(user)


@mcp.tool()
async def list_courses(include_past: bool = False, raw: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """List the user's courses (active by default; include_past adds completed ones)."""
    params: dict[str, Any] = {"include[]": ["term"]}
    if not include_past:
        params["enrollment_state"] = "active"
    try:
        courses = await get_client().get_paginated("/api/v1/courses", params)
    except CanvasAPIError as exc:
        return _error(exc)
    # Canvas can return access-restricted stubs with no name; drop them.
    courses = [c for c in courses if isinstance(c, dict) and c.get("name")]
    return courses if raw else [fmt.slim_course(c) for c in courses]


@mcp.tool()
async def list_assignments(
    course_id: int, bucket: str = "upcoming", raw: bool = False
) -> list[dict[str, Any]] | dict[str, Any]:
    """List a course's assignments with due dates, points and submission status.

    bucket: upcoming | past | undated | unsubmitted | all
    """
    if bucket not in ASSIGNMENT_BUCKETS:
        return {
            "error": f"Invalid bucket {bucket!r}. "
            f"Choose one of: {', '.join(sorted(ASSIGNMENT_BUCKETS))}."
        }
    params: dict[str, Any] = {"include[]": ["submission"], "order_by": "due_at"}
    if bucket != "all":
        params["bucket"] = bucket
    try:
        assignments = await get_client().get_paginated(
            f"/api/v1/courses/{course_id}/assignments", params
        )
    except CanvasAPIError as exc:
        return _error(exc)
    return assignments if raw else [fmt.slim_assignment(a) for a in assignments]


@mcp.tool()
async def get_assignment_status(course_id: int | None = None) -> dict[str, Any]:
    """Determine real assignment submission status from Canvas submission objects.

    Use this — not list_modules — to answer "did I submit / is it graded".
    Module completion (module_view_progress) only reflects browsing progress and
    says nothing about submission. Status is computed in Python from the
    submission object, never inferred, into one of five values:

      not_submitted (未完成) / submitted_ungraded (已提交·未出分) /
      graded (已出分) / excused (已豁免) / unknown (无法判断)

    A missing/overdue not_submitted carries an "overdue" flag; a late submission
    a "late" flag. Pass course_id for one course, omit it to scan all active
    courses.

    Returns {assignments: [...], unmatched_assignment_files: [...], errors: [...]}.
    unmatched_assignment_files lists module File items that look like assignment
    handouts but have no matching Canvas assignment — Canvas offers no submission
    entry for them, so their status cannot be judged.
    """
    try:
        return await crawl.build_assignment_status(
            get_client(), _now_utc(), course_id=course_id
        )
    except CanvasAPIError as exc:
        return _error(exc)


@mcp.tool()
async def get_upcoming_deadlines(days: int = 14) -> dict[str, Any]:
    """Aggregate assignments due within the next N days across all active courses, sorted by due date."""
    client = get_client()
    try:
        courses = await _active_courses()
    except CanvasAPIError as exc:
        return _error(exc)

    cutoff = _now_utc() + timedelta(days=days)
    now = _now_utc()
    deadlines: list[dict[str, Any]] = []
    errors: list[str] = []
    for course in courses:
        if not isinstance(course, dict) or not course.get("name"):
            continue
        try:
            assignments = await client.get_paginated(
                f"/api/v1/courses/{course['id']}/assignments",
                {"bucket": "upcoming", "include[]": ["submission"]},
            )
        except CanvasAPIError as exc:
            errors.append(str(exc))
            continue
        for a in assignments:
            due_at = a.get("due_at")
            if not due_at:
                continue
            try:
                due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if now <= due <= cutoff:
                item = fmt.slim_assignment(a)
                item["course"] = course.get("name")
                item["course_id"] = course["id"]
                deadlines.append(item)

    deadlines.sort(key=lambda d: d.get("due_at") or "")
    result: dict[str, Any] = {"days": days, "deadlines": deadlines}
    if errors:
        result["partial_errors"] = errors
    return result


@mcp.tool()
async def list_announcements(
    course_id: int, days: int = 30, raw: bool = False
) -> list[dict[str, Any]] | dict[str, Any]:
    """List a course's announcements from the last N days, with HTML converted to plain text."""
    now = _now_utc()
    params = {
        "context_codes[]": f"course_{course_id}",
        "start_date": _iso(now - timedelta(days=days)),
        "end_date": _iso(now + timedelta(days=1)),
    }
    try:
        announcements = await get_client().get_paginated("/api/v1/announcements", params)
    except CanvasAPIError as exc:
        return _error(exc)
    return announcements if raw else [fmt.slim_announcement(a) for a in announcements]


@mcp.tool()
async def list_modules(course_id: int, raw: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """List a course's modules and their items (type, title, url).

    Each module carries ``module_view_progress`` and each item an
    ``item_view_progress`` field. WARNING: module_view_progress only reflects
    browsing progress, NOT assignment submission. To determine whether an
    assignment has been submitted, you MUST call get_assignment_status.
    """
    try:
        modules = await get_client().get_paginated(
            f"/api/v1/courses/{course_id}/modules",
            {"include[]": ["items", "content_details"]},
        )
    except CanvasAPIError as exc:
        return _error(exc)
    return modules if raw else [fmt.slim_module(m) for m in modules]


@mcp.tool()
async def list_files(
    course_id: int, folder_id: int | None = None, raw: bool = False
) -> list[dict[str, Any]] | dict[str, Any]:
    """List a course's files (size, content type, updated time, download url).

    Pass folder_id to list a single folder instead of the whole course.
    """
    path = (
        f"/api/v1/folders/{folder_id}/files"
        if folder_id is not None
        else f"/api/v1/courses/{course_id}/files"
    )
    try:
        files = await get_client().get_paginated(path, {"sort": "updated_at", "order": "desc"})
    except CanvasAPIError as exc:
        return _error(exc)
    return files if raw else [fmt.slim_file(f) for f in files]


@mcp.tool()
async def get_page(course_id: int, page_url: str, raw: bool = False) -> dict[str, Any]:
    """Fetch a Canvas wiki page's body as plain text. page_url is the page's URL slug."""
    try:
        page = await get_client().get(f"/api/v1/courses/{course_id}/pages/{page_url}")
    except CanvasAPIError as exc:
        return _error(exc)
    return page if raw else fmt.slim_page(page)


@mcp.tool()
async def list_discussions(course_id: int, raw: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """List a course's discussion topics."""
    try:
        topics = await get_client().get_paginated(
            f"/api/v1/courses/{course_id}/discussion_topics",
            {"order_by": "recent_activity"},
        )
    except CanvasAPIError as exc:
        return _error(exc)
    return topics if raw else [fmt.slim_discussion(t) for t in topics]


@mcp.tool()
async def get_discussion(course_id: int, topic_id: int, raw: bool = False) -> dict[str, Any]:
    """Fetch one discussion topic with its replies."""
    client = get_client()
    try:
        topic = await client.get(
            f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}"
        )
        view = await client.get(
            f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view"
        )
    except CanvasAPIError as exc:
        return _error(exc)
    if raw:
        return {"topic": topic, "view": view}
    result = fmt.slim_discussion(topic)
    entries = view.get("view") if isinstance(view, dict) else None
    result["entries"] = [
        fmt.slim_discussion_entry(e) for e in (entries or []) if isinstance(e, dict)
    ]
    return result


@mcp.tool()
async def get_grades(course_id: int, raw: bool = False) -> list[dict[str, Any]] | dict[str, Any]:
    """Fetch the user's grade summary (current/final score and grade) for a course."""
    try:
        enrollments = await get_client().get_paginated(
            f"/api/v1/courses/{course_id}/enrollments", {"user_id": "self"}
        )
    except CanvasAPIError as exc:
        return _error(exc)
    return enrollments if raw else [fmt.slim_enrollment(e) for e in enrollments]


@mcp.tool()
async def get_calendar_events(days: int = 14, raw: bool = False) -> dict[str, Any]:
    """List calendar events (and assignment events) for the next N days across active courses."""
    client = get_client()
    try:
        courses = await _active_courses()
    except CanvasAPIError as exc:
        return _error(exc)
    context_codes = ["user_self"] + [
        f"course_{c['id']}" for c in courses if isinstance(c, dict) and c.get("id")
    ]
    now = _now_utc()
    base_params = {
        "start_date": _iso(now),
        "end_date": _iso(now + timedelta(days=days)),
        "context_codes[]": context_codes,
    }
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for event_type in ("event", "assignment"):
        try:
            batch = await client.get_paginated(
                "/api/v1/calendar_events", {**base_params, "type": event_type}
            )
            events.extend(batch)
        except CanvasAPIError as exc:
            errors.append(str(exc))
    if not raw:
        events = [fmt.slim_calendar_event(e) for e in events]
    events.sort(key=lambda e: e.get("start_at") or "")
    result: dict[str, Any] = {"days": days, "events": events}
    if errors:
        result["partial_errors"] = errors
    return result


@mcp.tool()
async def get_course_progress(course_id: int | None = None) -> dict[str, Any]:
    """Return pre-computed grade progress (floor / ceiling / graded average /
    how much is needed for each target) for one course or all active courses.

    The numbers come straight from each course's computed progress.json. You
    MUST use the scores and percentages from this tool verbatim — do not compute
    or estimate grades yourself. Weights come only from a confirmed grading.json;
    courses without one are returned with status "unconfirmed".
    """
    saved = report.load_all_progress()
    want = [p for p in saved
            if course_id is None or p.get("course_id") == course_id]
    if not want:
        try:
            want = await report.refresh_progress(
                get_client(), _now_utc(), course_id=course_id
            )
        except CanvasAPIError as exc:
            return _error(exc)
    summary = report.text_summary(want)
    if course_id is not None:
        one = next((p for p in want if p.get("course_id") == course_id), None)
        if one is None:
            return {"error": f"找不到 course_id {course_id} 的进度数据。"}
        return {"progress": one, "summary": summary}
    return {"progress": want, "summary": summary}


@mcp.tool()
async def propose_grading_change(course_id: int, instruction: str) -> dict[str, Any]:
    """Parse a natural-language grading change into a validated patch and PREVIEW
    its effect — without writing anything.

    Returns a patch_id plus a diff and the impact on progress, or a clarification
    question when the instruction is ambiguous. The patch is staged under
    data/courses/<code>/pending/ and expires after 24 hours. To actually apply
    it, call apply_grading_change(patch_id) — but ONLY after the user has
    explicitly confirmed they want this change.
    """
    now = _now_utc()
    try:
        course = await report.get_course(get_client(), course_id)
    except CanvasAPIError as exc:
        return _error(exc)
    if not course:
        return {"error": f"找不到 active 课程 {course_id}。"}
    res = await report.prepare_amendment(get_client(), course, instruction, now)
    if not res["ok"]:
        return {"ok": False, "clarification": res.get("clarification"),
                "error": res.get("error")}
    patch_id = grading_mod.new_patch_id()
    code = course.get("course_code")
    grading_mod.save_pending(code, {
        "patch_id": patch_id,
        "course_id": course_id,
        "course_code": code,
        "created_at": now.isoformat(),
        "instruction": instruction,
        "patch": res["patch"],
        "diff": res["diff"],
        "impact": res["impact"],
    })
    return {
        "ok": True,
        "patch_id": patch_id,
        "diff": res["diff"],
        "impact": res["impact"],
        "note": "预览而已，尚未写入。只有在用户明确回复确认之后，才能调用 "
                "apply_grading_change(patch_id)。",
    }


@mcp.tool()
async def apply_grading_change(patch_id: str) -> dict[str, Any]:
    """Apply a previously proposed grading change, then recompute and re-render.

    Call this ONLY after the user has explicitly confirmed the change previewed
    by propose_grading_change. Re-validates against the current config before
    writing; rejects expired (>24h) or already-consumed patches.
    """
    now = _now_utc()
    payload = grading_mod.load_pending(patch_id)
    if not payload:
        return {"error": f"找不到补丁 {patch_id}（可能已应用或已过期）。"}
    if grading_mod.is_pending_expired(payload, now):
        grading_mod.delete_pending(payload["course_code"], patch_id)
        return {"error": f"补丁 {patch_id} 已过期（超过 24 小时），请重新 propose。"}
    try:
        course = await report.get_course(get_client(), payload["course_id"])
    except CanvasAPIError as exc:
        return _error(exc)
    if not course:
        return {"error": f"找不到 active 课程 {payload['course_id']}。"}
    code = course.get("course_code")
    confirmed = grading_mod.load_grading(code)
    if not confirmed:
        return {"error": f"{code} 没有已确认的 grading.json。"}
    assignments = await report.course_assignments_brief_cached(
        get_client(), payload["course_id"]
    )
    try:
        new_grading, _ = grading_mod.apply_patch(
            confirmed, payload["patch"], assignments
        )
    except grading_mod.PatchError as exc:
        return {"error": str(exc)}
    results = await report.commit_amendment(
        get_client(), course, new_grading, payload["instruction"], payload["patch"], now
    )
    grading_mod.delete_pending(code, patch_id)
    one = next((p for p in results if p.get("course_id") == payload["course_id"]), None)
    return {"ok": True, "progress": one, "summary": report.text_summary(results)}


@mcp.tool()
async def undo_grading_change(course_id: int) -> dict[str, Any]:
    """Undo the most recent confirmed grading change for a course, then recompute
    and re-render. Reverts to the previous history snapshot."""
    now = _now_utc()
    try:
        course = await report.get_course(get_client(), course_id)
    except CanvasAPIError as exc:
        return _error(exc)
    if not course:
        return {"error": f"找不到 active 课程 {course_id}。"}
    code = course.get("course_code")
    restored = grading_mod.undo_last(code)
    if restored is None:
        return {"ok": False, "message": "没有可撤销的修改（只剩确认基线或无 history）。"}
    results = await report.refresh_progress(get_client(), now, course_id=course_id)
    one = next((p for p in results if p.get("course_id") == course_id), None)
    return {"ok": True, "progress": one, "summary": report.text_summary(results)}


def main() -> None:
    """Validate configuration, then serve over stdio."""
    try:
        load_config()
    except CanvasConfigError as exc:
        print(f"canvas-mcp: configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
