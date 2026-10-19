"""Tool-level tests with a mocked Canvas API."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from canvas_mcp import server

BASE = "https://canvas.example.edu"


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
async def test_whoami_slim_and_raw(canvas_env):
    respx.get(f"{BASE}/api/v1/users/self").mock(
        return_value=httpx.Response(
            200, json={"id": 1, "name": "Test User", "avatar_url": "noise"}
        )
    )
    slim = await server.whoami()
    assert slim == {"id": 1, "name": "Test User"}
    raw = await server.whoami(raw=True)
    assert raw["avatar_url"] == "noise"


@respx.mock
async def test_whoami_bad_token_returns_error_dict(canvas_env):
    respx.get(f"{BASE}/api/v1/users/self").mock(
        return_value=httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]})
    )
    result = await server.whoami()
    assert result["status_code"] == 401
    assert "Unauthorized" in result["error"]


async def test_list_assignments_rejects_bad_bucket(canvas_env):
    result = await server.list_assignments(1, bucket="bogus")
    assert "Invalid bucket" in result["error"]


@respx.mock
async def test_list_assignments_passes_bucket_and_slims(canvas_env):
    route = respx.get(f"{BASE}/api/v1/courses/42/assignments").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": 9, "name": "HW1", "due_at": "2026-10-21T15:59:00Z",
                "points_possible": 10, "html_url": "u", "workflow_state": "published",
                "description": "x" * 10000,
                "submission": {"workflow_state": "submitted", "late": False},
            }],
        )
    )
    result = await server.list_assignments(42, bucket="unsubmitted")
    assert "bucket=unsubmitted" in str(route.calls[0].request.url)
    assert result[0]["submission"]["workflow_state"] == "submitted"
    assert "description" not in result[0]


@respx.mock
async def test_list_assignments_all_omits_bucket(canvas_env):
    route = respx.get(f"{BASE}/api/v1/courses/42/assignments").mock(
        return_value=httpx.Response(200, json=[])
    )
    await server.list_assignments(42, bucket="all")
    assert "bucket=" not in str(route.calls[0].request.url)


@respx.mock
async def test_list_announcements_converts_html(canvas_env):
    route = respx.get(f"{BASE}/api/v1/announcements").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": 3, "title": "Midterm moved", "posted_at": "2026-10-01T12:00:00Z",
                "html_url": "u",
                "message": "<p>Now on <b>Friday</b>.</p>",
                "author": {"display_name": "Prof X"},
            }],
        )
    )
    result = await server.list_announcements(42, days=30)
    url = str(route.calls[0].request.url)
    assert "context_codes%5B%5D=course_42" in url or "context_codes[]=course_42" in url
    assert "start_date=" in url and "end_date=" in url
    assert result[0]["message_text"] == "Now on Friday."
    assert result[0]["author"] == "Prof X"
    assert result[0]["posted_at_local"].endswith(("EDT", "EST"))


@respx.mock
async def test_get_upcoming_deadlines_filters_and_sorts(canvas_env):
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "name": "RL"}, {"id": 2, "name": "MDP"}]
        )
    )
    respx.get(f"{BASE}/api/v1/courses/1/assignments").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 11, "name": "Due soon", "due_at": iso_in(2)},
                {"id": 12, "name": "Too far", "due_at": iso_in(30)},
                {"id": 13, "name": "No date", "due_at": None},
            ],
        )
    )
    respx.get(f"{BASE}/api/v1/courses/2/assignments").mock(
        return_value=httpx.Response(
            200, json=[{"id": 21, "name": "Due sooner", "due_at": iso_in(1)}]
        )
    )
    result = await server.get_upcoming_deadlines(days=14)
    names = [d["name"] for d in result["deadlines"]]
    assert names == ["Due sooner", "Due soon"]
    assert result["deadlines"][0]["course"] == "MDP"
    assert "partial_errors" not in result


@respx.mock
async def test_list_modules_items_completion(canvas_env):
    respx.get(f"{BASE}/api/v1/courses/1/modules").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": 5, "name": "Week 1", "position": 1, "state": "started",
                "items": [{
                    "id": 50, "title": "Intro", "type": "Page",
                    "html_url": "u", "page_url": "intro",
                    "completion_requirement": {"type": "must_view", "completed": True},
                }],
            }],
        )
    )
    result = await server.list_modules(1)
    item = result[0]["items"][0]
    assert item["completed"] is True
    assert item["type"] == "Page"


@respx.mock
async def test_list_files_folder_vs_course(canvas_env):
    course_route = respx.get(f"{BASE}/api/v1/courses/1/files").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": 7, "display_name": "slides.pdf", "size": 123,
                   "content-type": "application/pdf", "url": "dl",
                   "updated_at": "2026-10-01T00:00:00Z"}],
        )
    )
    folder_route = respx.get(f"{BASE}/api/v1/folders/99/files").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await server.list_files(1)
    assert result[0]["content_type"] == "application/pdf"
    assert course_route.called
    await server.list_files(1, folder_id=99)
    assert folder_route.called


@respx.mock
async def test_get_page_body_text(canvas_env):
    respx.get(f"{BASE}/api/v1/courses/1/pages/syllabus").mock(
        return_value=httpx.Response(
            200,
            json={"page_id": 2, "url": "syllabus", "title": "Syllabus",
                  "body": "<h1>Rules</h1><p>Be on time.</p>"},
        )
    )
    result = await server.get_page(1, "syllabus")
    assert "Rules" in result["body_text"]
    assert "Be on time." in result["body_text"]
    assert "body" not in result


@respx.mock
async def test_get_discussion_combines_topic_and_entries(canvas_env):
    respx.get(f"{BASE}/api/v1/courses/1/discussion_topics/8").mock(
        return_value=httpx.Response(
            200, json={"id": 8, "title": "Q&A", "message": "<p>Ask here</p>"}
        )
    )
    respx.get(f"{BASE}/api/v1/courses/1/discussion_topics/8/view").mock(
        return_value=httpx.Response(
            200,
            json={"view": [{
                "id": 100, "user_name": "Alice", "message": "<p>Hi</p>",
                "created_at": "2026-10-01T00:00:00Z",
                "replies": [{"id": 101, "user_name": "Bob", "message": "<p>Yo</p>"}],
            }]},
        )
    )
    result = await server.get_discussion(1, 8)
    assert result["message_text"] == "Ask here"
    assert result["entries"][0]["message_text"] == "Hi"
    assert result["entries"][0]["replies"][0]["user_name"] == "Bob"


@respx.mock
async def test_get_grades_slims_enrollment(canvas_env):
    respx.get(f"{BASE}/api/v1/courses/1/enrollments").mock(
        return_value=httpx.Response(
            200,
            json=[{
                "id": 1, "type": "StudentEnrollment", "enrollment_state": "active",
                "course_id": 1,
                "grades": {"current_score": 92.5, "current_grade": "A",
                           "final_score": 88.0, "html_url": "noise"},
            }],
        )
    )
    result = await server.get_grades(1)
    assert result[0]["grades"]["current_score"] == 92.5
    assert "html_url" not in result[0]["grades"]


@respx.mock
async def test_get_calendar_events_contexts_and_sort(canvas_env):
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "RL"}])
    )
    def events_for(request):
        etype = request.url.params.get("type")
        if etype == "event":
            return httpx.Response(
                200, json=[{"id": 1, "title": "Office hours", "start_at": iso_in(3)}]
            )
        return httpx.Response(
            200,
            json=[{"id": 2, "title": "A2 due", "start_at": iso_in(1),
                   "assignment": {"id": 9, "name": "A2", "due_at": iso_in(1),
                                  "points_possible": 100}}],
        )
    route = respx.get(f"{BASE}/api/v1/calendar_events").mock(side_effect=events_for)
    result = await server.get_calendar_events(days=14)
    assert [e["title"] for e in result["events"]] == ["A2 due", "Office hours"]
    url = str(route.calls[0].request.url)
    assert "user_self" in url and "course_1" in url
