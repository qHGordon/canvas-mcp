"""Crawl library tests: 403-continue, module file recovery, pagination, upcoming report."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from canvas_mcp import server
from canvas_mcp.client import CanvasClient
from canvas_mcp.crawl import (
    build_assignment_status,
    build_upcoming,
    compute_status,
    crawl_course,
    crawl_courses,
    files_to_rows,
)

BASE = "https://canvas.example.edu"


def file_json(fid: int, name: str) -> dict:
    return {
        "id": fid, "display_name": name, "filename": name, "size": 1000 + fid,
        "content-type": "application/pdf", "url": f"{BASE}/download/{fid}",
        "updated_at": "2026-10-01T00:00:00Z",
    }


@respx.mock
async def test_crawl_course_recovers_files_from_modules_when_403():
    """/files 403 must not abort the course; File items in modules are fetched instead."""
    respx.get(f"{BASE}/api/v1/courses/1/tabs").mock(
        return_value=httpx.Response(200, json=[
            {"id": "home", "label": "Home"},
            {"id": "files", "label": "Files", "hidden": True},
        ])
    )
    respx.get(f"{BASE}/api/v1/courses/1/files").mock(
        return_value=httpx.Response(403, json={"status": "unauthorized"})
    )
    # Modules paginated across two pages
    modules_route = respx.get(f"{BASE}/api/v1/courses/1/modules")
    modules_route.side_effect = [
        httpx.Response(
            200,
            json=[{
                "id": 10, "name": "Week 1", "position": 1,
                "items": [
                    {"id": 100, "title": "slides1.pdf", "type": "File",
                     "content_id": 501, "url": f"{BASE}/api/v1/courses/1/files/501"},
                    {"id": 101, "title": "Lecture video", "type": "ExternalUrl",
                     "external_url": "https://external.example.com/video"},
                ],
            }],
            headers={"Link": f'<{BASE}/api/v1/courses/1/modules?page=2>; rel="next"'},
        ),
        httpx.Response(
            200,
            json=[{
                "id": 11, "name": "Week 2", "position": 2,
                "items": [
                    {"id": 102, "title": "slides2.pdf", "type": "File",
                     "content_id": 502, "url": f"{BASE}/api/v1/courses/1/files/502"},
                ],
            }],
        ),
    ]
    respx.get(f"{BASE}/api/v1/courses/1/files/501").mock(
        return_value=httpx.Response(200, json=file_json(501, "slides1.pdf"))
    )
    respx.get(f"{BASE}/api/v1/courses/1/files/502").mock(
        return_value=httpx.Response(200, json=file_json(502, "slides2.pdf"))
    )
    respx.get(f"{BASE}/api/v1/courses/1/pages").mock(
        return_value=httpx.Response(200, json=[{"page_id": 7, "url": "syllabus", "title": "Syllabus"}])
    )
    respx.get(f"{BASE}/api/v1/courses/1/assignments").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "name": "HW1"}])
    )
    respx.get(f"{BASE}/api/v1/announcements").mock(
        return_value=httpx.Response(200, json=[])
    )

    client = CanvasClient(BASE, "t")
    try:
        node = await crawl_course(client, {"id": 1, "name": "RL"}, since="2026-09-01")
    finally:
        await client.aclose()

    # Files recovered via modules despite the 403
    assert [f["id"] for f in node["files"]] == [501, 502]
    assert all(f["source"] == "module" for f in node["files"])
    assert node["files"][0]["module"] == "Week 1"
    # The 403 is recorded, nothing else failed
    assert [e["section"] for e in node["errors"]] == ["files"]
    assert node["errors"][0]["status"] == 403
    # Pagination followed: both module pages present
    assert [m["name"] for m in node["modules"]] == ["Week 1", "Week 2"]
    assert modules_route.call_count == 2
    # Hidden tab filtered out; ExternalUrl kept as a link, never fetched
    assert node["tabs"] == [{"id": "home", "label": "Home"}]
    ext = node["modules"][0]["items"][1]
    assert ext["external_url"] == "https://external.example.com/video"
    assert node["pages"][0]["url"] == "syllabus"


@respx.mock
async def test_crawl_course_skips_module_file_detail_already_listed():
    """When /files works, module File items with a known content_id cost no extra request."""
    respx.get(f"{BASE}/api/v1/courses/1/tabs").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/api/v1/courses/1/files").mock(
        return_value=httpx.Response(200, json=[file_json(501, "slides1.pdf")])
    )
    respx.get(f"{BASE}/api/v1/courses/1/modules").mock(
        return_value=httpx.Response(200, json=[{
            "id": 10, "name": "Week 1",
            "items": [{"id": 100, "title": "slides1.pdf", "type": "File",
                       "content_id": 501, "url": f"{BASE}/api/v1/courses/1/files/501"}],
        }])
    )
    detail_route = respx.get(f"{BASE}/api/v1/courses/1/files/501").mock(
        return_value=httpx.Response(200, json=file_json(501, "slides1.pdf"))
    )
    respx.get(f"{BASE}/api/v1/courses/1/pages").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/api/v1/courses/1/assignments").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/api/v1/announcements").mock(return_value=httpx.Response(200, json=[]))

    client = CanvasClient(BASE, "t")
    try:
        node = await crawl_course(client, {"id": 1, "name": "RL"}, since="2026-09-01")
    finally:
        await client.aclose()
    assert len(node["files"]) == 1
    assert node["files"][0]["source"] == "files"
    assert detail_route.call_count == 0


@respx.mock
async def test_crawl_course_survives_every_section_failing():
    for path in ("tabs", "files", "modules", "pages", "assignments"):
        respx.get(f"{BASE}/api/v1/courses/1/{path}").mock(
            return_value=httpx.Response(403)
        )
    respx.get(f"{BASE}/api/v1/announcements").mock(return_value=httpx.Response(404))

    client = CanvasClient(BASE, "t")
    try:
        node = await crawl_course(client, {"id": 1, "name": "Locked"}, since="2026-09-01")
    finally:
        await client.aclose()
    assert len(node["errors"]) == 6
    assert node["files"] == [] and node["modules"] == []


@respx.mock
async def test_crawl_courses_filters_by_course_id():
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "name": "RL"}, {"id": 2, "name": "MDP"},
        ])
    )
    for path in ("tabs", "files", "modules", "pages", "assignments"):
        respx.get(f"{BASE}/api/v1/courses/2/{path}").mock(
            return_value=httpx.Response(200, json=[])
        )
    respx.get(f"{BASE}/api/v1/announcements").mock(return_value=httpx.Response(200, json=[]))

    client = CanvasClient(BASE, "t")
    try:
        index = await crawl_courses(client, since="2026-09-01", course_id=2)
    finally:
        await client.aclose()
    assert [c["id"] for c in index["courses"]] == [2]


def test_files_to_rows_flattens_tree():
    index = {"courses": [{
        "id": 1, "name": "RL",
        "files": [{"id": 501, "display_name": "a.pdf", "size": 1,
                   "content_type": "application/pdf",
                   "updated_at": "2026-10-01T00:00:00Z",
                   "source": "module", "module": "Week 1", "url": "dl"}],
    }]}
    rows = files_to_rows(index)
    assert rows == [{
        "course_id": 1, "course_name": "RL", "file_id": 501,
        "display_name": "a.pdf", "size": 1, "content_type": "application/pdf",
        "updated_at": "2026-10-01T00:00:00Z", "source": "module",
        "module": "Week 1", "url": "dl",
    }]


# ---------------------------------------------------------------------------
# build_upcoming (uses the server's env-configured client)
# ---------------------------------------------------------------------------

@pytest.fixture
async def canvas_env(monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", BASE)
    monkeypatch.setenv("CANVAS_TOKEN", "test-token")
    monkeypatch.delenv("CANVAS_TOKEN_FILE", raising=False)
    server._client = None
    yield
    if server._client is not None:
        await server._client.aclose()
        server._client = None


def iso_in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


@respx.mock
async def test_build_upcoming_merges_and_continues_on_403(canvas_env):
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "name": "RL"}, {"id": 2, "name": "MDP"},
        ])
    )
    respx.get(f"{BASE}/api/v1/courses/1/assignments").mock(
        return_value=httpx.Response(
            200, json=[{"id": 11, "name": "A2", "due_at": iso_in(2)}]
        )
    )
    # Course 2 assignments forbidden: recorded, not fatal
    respx.get(f"{BASE}/api/v1/courses/2/assignments").mock(
        return_value=httpx.Response(403)
    )

    def announcements(request):
        codes = request.url.params.get_list("context_codes[]")
        if "course_1" in codes:
            return httpx.Response(200, json=[
                {"id": 31, "title": "Old", "posted_at": "2026-10-10T00:00:00Z"},
                {"id": 32, "title": "New", "posted_at": "2026-10-20T00:00:00Z"},
            ])
        return httpx.Response(200, json=[])

    respx.get(f"{BASE}/api/v1/announcements").mock(side_effect=announcements)

    result = await build_upcoming(days=14, ann_days=7)
    assert [d["name"] for d in result["deadlines"]] == ["A2"]
    assert result["deadlines"][0]["course"] == "RL"
    # Newest announcement first, course attached
    assert [a["title"] for a in result["announcements"]] == ["New", "Old"]
    assert result["announcements"][0]["course"] == "RL"
    # The course-2 403 was recorded but did not abort
    assert any("Forbidden" in str(e) for e in result["errors"])


# ---------------------------------------------------------------------------
# Assignment submission status
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _sub(assignment, **submission):
    """A /students/submissions row: submission fields + embedded assignment."""
    return {**submission, "assignment": assignment}


@respx.mock
async def test_build_assignment_status_all_five_states_and_flags():
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "ABC1001H"}])
    )
    uploads = ["online_upload"]
    respx.get(f"{BASE}/api/v1/courses/1/students/submissions").mock(
        return_value=httpx.Response(200, json=[
            # excused
            _sub({"id": 1, "name": "A1", "submission_types": uploads},
                 excused=True),
            # graded (with score + grade)
            _sub({"id": 2, "name": "A2", "points_possible": 100,
                  "submission_types": uploads},
                 submitted_at="2026-10-02T00:00:00Z", score=95, grade="A"),
            # submitted_ungraded, not yet posted -> note
            _sub({"id": 3, "name": "A3", "submission_types": uploads},
                 submitted_at="2026-10-20T00:00:00Z", score=None, grade=None,
                 posted_at=None),
            # not_submitted + overdue (missing)
            _sub({"id": 4, "name": "A4", "due_at": "2026-10-01T00:00:00Z",
                  "submission_types": uploads},
                 submitted_at=None, missing=True),
            # unknown (on_paper, nothing submitted)
            _sub({"id": 5, "name": "Participation",
                  "submission_types": ["on_paper"]},
                 submitted_at=None),
            # graded + late flag
            _sub({"id": 6, "name": "A6", "points_possible": 100,
                  "submission_types": uploads},
                 submitted_at="2026-10-25T00:00:00Z", score=80, grade="B",
                 late=True),
        ])
    )
    # Modules: one unmatched handout, one matched (A2), one non-assignment file.
    respx.get(f"{BASE}/api/v1/courses/1/modules").mock(
        return_value=httpx.Response(200, json=[{
            "id": 10, "name": "Handouts",
            "items": [
                {"id": 100, "title": "A7 handout.pdf", "type": "File",
                 "url": f"{BASE}/api/v1/courses/1/files/700"},
                {"id": 101, "title": "A2 solutions.pdf", "type": "File",
                 "url": f"{BASE}/api/v1/courses/1/files/702"},
                {"id": 102, "title": "lecture notes.pdf", "type": "File",
                 "url": f"{BASE}/api/v1/courses/1/files/703"},
                {"id": 103, "title": "Reading", "type": "Page"},
            ],
        }])
    )

    client = CanvasClient(BASE, "t")
    try:
        result = await build_assignment_status(client, NOW)
    finally:
        await client.aclose()

    by_name = {a["name"]: a for a in result["assignments"]}
    assert by_name["A1"]["status"] == "excused"
    assert by_name["A1"]["status_zh"] == "已豁免"
    assert by_name["A2"]["status"] == "graded"
    assert by_name["A2"]["score"] == 95 and by_name["A2"]["points_possible"] == 100
    assert by_name["A2"]["grade"] == "A"
    assert by_name["A3"]["status"] == "submitted_ungraded"
    assert "可能已批改未公布" in by_name["A3"]["notes"]
    assert by_name["A4"]["status"] == "not_submitted"
    assert "overdue" in by_name["A4"]["flags"]
    assert by_name["Participation"]["status"] == "unknown"
    assert by_name["A6"]["status"] == "graded"
    assert "late" in by_name["A6"]["flags"]
    # Toronto local time attached next to due_at
    assert by_name["A4"]["due_at_local"].endswith(("EDT", "EST"))

    titles = [u["title"] for u in result["unmatched_assignment_files"]]
    assert titles == ["A7 handout.pdf"]  # A2 matched; lecture notes not assignment-like
    assert result["unmatched_assignment_files"][0]["note"] == (
        "Canvas 上无提交入口，无法判断状态"
    )
    assert result["errors"] == []


@respx.mock
async def test_build_assignment_status_falls_back_to_assignments_on_403():
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[{"id": 2, "name": "XYZ2002H"}])
    )
    respx.get(f"{BASE}/api/v1/courses/2/students/submissions").mock(
        return_value=httpx.Response(403, json={"status": "unauthorized"})
    )
    assigns_route = respx.get(f"{BASE}/api/v1/courses/2/assignments").mock(
        return_value=httpx.Response(200, json=[{
            "id": 9, "name": "Project", "points_possible": 50,
            "submission_types": ["online_upload"],
            "submission": {"submitted_at": "2026-10-10T00:00:00Z",
                           "score": 45, "grade": "90%"},
        }])
    )
    respx.get(f"{BASE}/api/v1/courses/2/modules").mock(
        return_value=httpx.Response(200, json=[])
    )

    client = CanvasClient(BASE, "t")
    try:
        result = await build_assignment_status(client, NOW, course_id=2)
    finally:
        await client.aclose()

    assert assigns_route.called
    assert result["assignments"][0]["status"] == "graded"
    assert result["assignments"][0]["score"] == 45
    # The expected 403 on submissions is recorded, with course context, not fatal
    sections = [(e["section"], e["status"], e["course"]) for e in result["errors"]]
    assert ("submissions", 403, "XYZ2002H") in sections


def test_compute_status_overdue_from_due_date_without_missing_flag():
    """A past due_at alone (no missing flag) still marks overdue."""
    status, flags, _ = compute_status(
        {"due_at": "2026-10-01T00:00:00Z", "submission_types": ["online_upload"]},
        {"submitted_at": None},
        NOW,
    )
    assert status == "not_submitted"
    assert flags == ["overdue"]


def test_compute_status_not_overdue_when_due_in_future():
    status, flags, _ = compute_status(
        {"due_at": "2026-12-01T00:00:00Z", "submission_types": ["online_upload"]},
        {"submitted_at": None},
        NOW,
    )
    assert status == "not_submitted"
    assert flags == []
