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
