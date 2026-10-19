"""Formatter tests: HTML→text, timezone rendering, JSON slimming."""

from canvas_mcp.formatters import (
    html_to_text,
    slim_assignment,
    slim_course,
    slim_file,
    to_local,
    with_local_times,
)


def test_html_to_text_basic():
    html = "<p>Hello <b>world</b></p><ul><li>one</li><li>two</li></ul>"
    text = html_to_text(html)
    assert "Hello world" in text
    assert "- one" in text
    assert "- two" in text
    assert "<" not in text


def test_html_to_text_strips_scripts_and_entities():
    html = "<script>alert(1)</script><p>A &amp; B</p><style>.x{}</style>"
    text = html_to_text(html)
    assert "alert" not in text
    assert ".x" not in text
    assert "A & B" in text


def test_html_to_text_empty():
    assert html_to_text(None) == ""
    assert html_to_text("") == ""


def test_to_local_renders_toronto_time():
    # 2026-01-15 05:00 UTC = midnight EST
    assert to_local("2026-01-15T05:00:00Z") == "2026-01-15 00:00 EST"
    # DST: 2026-07-15 04:00 UTC = midnight EDT
    assert to_local("2026-07-15T04:00:00Z") == "2026-07-15 00:00 EDT"
    assert to_local(None) is None
    assert to_local("garbage") is None


def test_with_local_times_adds_fields():
    obj = {"due_at": "2026-10-21T03:59:00Z", "lock_at": None}
    out = with_local_times(obj, "due_at", "lock_at")
    assert out["due_at_local"] == "2026-10-20 23:59 EDT"
    assert "lock_at_local" not in out


def test_slim_course():
    raw = {
        "id": 1, "name": "RL", "course_code": "APS1080",
        "workflow_state": "available",
        "term": {"id": 9, "name": "2026 Fall", "sis_term_id": "x"},
        "uuid": "noise", "calendar": {"ics": "noise"},
    }
    slim = slim_course(raw)
    assert slim == {
        "id": 1, "name": "RL", "course_code": "APS1080",
        "workflow_state": "available", "term": "2026 Fall",
    }


def test_slim_assignment_keeps_submission_and_local_time():
    raw = {
        "id": 10, "name": "A2", "due_at": "2026-10-21T15:59:00Z",
        "points_possible": 100, "html_url": "https://x/a/10",
        "workflow_state": "published",
        "description": "<p>huge html blob</p>" * 500,
        "submission": {
            "workflow_state": "unsubmitted", "score": None,
            "late": False, "missing": False, "irrelevant": "x",
        },
    }
    slim = slim_assignment(raw)
    assert slim["due_at_local"] == "2026-10-21 11:59 EDT"
    assert slim["submission"]["workflow_state"] == "unsubmitted"
    assert "irrelevant" not in slim["submission"]
    assert "description" not in slim


def test_slim_file_normalizes_content_type():
    slim = slim_file({"id": 5, "display_name": "f.pdf", "content-type": "application/pdf"})
    assert slim["content_type"] == "application/pdf"
