"""FastMCP entry point exposing read-only Canvas LMS tools over stdio."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import CanvasAPIError, CanvasClient, CanvasConfigError, load_config
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
    """List a course's modules and their items (type, title, url, completion state)."""
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
