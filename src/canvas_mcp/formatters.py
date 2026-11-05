"""Shaping Canvas API payloads for model consumption.

- Slim large JSON objects down to the fields a model actually needs.
- Convert HTML bodies to plain text.
- Attach an America/Toronto local-time string next to every ISO 8601 time.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

# Local display timezone; override with CANVAS_TZ (IANA name, e.g. "America/New_York").
LOCAL_TZ = ZoneInfo(os.environ.get("CANVAS_TZ", "").strip() or "America/Toronto")

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_zh(dt: datetime) -> str:
    """周X for a date (Monday=周一). Always derive the weekday from the date —
    never hand-write it."""
    return WEEKDAY_ZH[dt.weekday()]

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "table", "tr", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "pre", "hr",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")
        if tag == "li":
            self._chunks.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of blank lines and trailing spaces
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str | None) -> str:
    """Convert a Canvas HTML body to readable plain text."""
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def to_local(iso_time: str | None) -> str | None:
    """Render an ISO 8601 UTC timestamp as an America/Toronto local string."""
    if not iso_time:
        return None
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(LOCAL_TZ)
    return local.strftime("%Y-%m-%d %H:%M %Z")


def with_local_times(obj: dict[str, Any], *fields: str) -> dict[str, Any]:
    """Return obj with ``<field>_local`` strings added for each ISO field present."""
    for field in fields:
        value = obj.get(field)
        if value:
            obj[f"{field}_local"] = to_local(value)
    return obj


def pick(source: dict[str, Any], *fields: str) -> dict[str, Any]:
    """Copy only the named fields that are present and non-null."""
    return {f: source[f] for f in fields if source.get(f) is not None}


# ---------------------------------------------------------------------------
# Per-resource slimmers. Each takes the raw Canvas object and returns a
# compact dict. ``raw=True`` at the tool layer bypasses these entirely.
# ---------------------------------------------------------------------------

def slim_course(c: dict[str, Any]) -> dict[str, Any]:
    out = pick(c, "id", "name", "course_code", "workflow_state", "start_at", "end_at")
    term = c.get("term")
    if isinstance(term, dict):
        out["term"] = term.get("name")
    return with_local_times(out, "start_at", "end_at")


def slim_assignment(a: dict[str, Any]) -> dict[str, Any]:
    out = pick(
        a,
        "id", "name", "due_at", "points_possible", "html_url",
        "workflow_state", "lock_at", "unlock_at", "submission_types",
        "has_submitted_submissions",
    )
    submission = a.get("submission")
    if isinstance(submission, dict):
        out["submission"] = pick(
            submission,
            "workflow_state", "submitted_at", "score", "grade", "late", "missing",
        )
        if out["submission"].get("submitted_at"):
            with_local_times(out["submission"], "submitted_at")
    return with_local_times(out, "due_at", "lock_at", "unlock_at")


def slim_announcement(a: dict[str, Any]) -> dict[str, Any]:
    out = pick(a, "id", "title", "posted_at", "html_url", "author")
    if isinstance(out.get("author"), dict):
        out["author"] = out["author"].get("display_name")
    out["message_text"] = html_to_text(a.get("message"))
    return with_local_times(out, "posted_at")


def slim_module(m: dict[str, Any]) -> dict[str, Any]:
    out = pick(m, "id", "name", "position", "completed_at")
    # Canvas 的 module ``state`` 只代表浏览进度，不代表作业提交。改名以杜绝误用；
    # 判断作业提交状态必须调用 get_assignment_status。
    if m.get("state") is not None:
        out["module_view_progress"] = m["state"]
    items = m.get("items")
    if isinstance(items, list):
        out["items"] = [slim_module_item(i) for i in items]
    return with_local_times(out, "completed_at")


def slim_module_item(i: dict[str, Any]) -> dict[str, Any]:
    out = pick(i, "id", "title", "type", "html_url", "external_url", "page_url")
    # completion_requirement 同样只是浏览进度，改名以免被当成提交状态。
    completion = i.get("completion_requirement")
    if isinstance(completion, dict):
        out["item_view_progress"] = {
            "type": completion.get("type"),
            "completed": completion.get("completed", False),
        }
    return out


def slim_file(f: dict[str, Any]) -> dict[str, Any]:
    out = pick(
        f,
        "id", "display_name", "filename", "size", "updated_at", "url", "folder_id",
    )
    out["content_type"] = f.get("content-type") or f.get("content_type")
    return with_local_times(out, "updated_at")


def slim_page(p: dict[str, Any]) -> dict[str, Any]:
    out = pick(p, "page_id", "url", "title", "updated_at", "html_url")
    out["body_text"] = html_to_text(p.get("body"))
    return with_local_times(out, "updated_at")


def slim_discussion(d: dict[str, Any]) -> dict[str, Any]:
    out = pick(
        d,
        "id", "title", "posted_at", "last_reply_at", "discussion_subentry_count",
        "html_url", "locked", "pinned",
    )
    out["message_text"] = html_to_text(d.get("message"))
    author = d.get("author")
    if isinstance(author, dict):
        out["author"] = author.get("display_name")
    return with_local_times(out, "posted_at", "last_reply_at")


def slim_discussion_entry(e: dict[str, Any]) -> dict[str, Any]:
    out = pick(e, "id", "user_name", "created_at", "parent_id")
    out["message_text"] = html_to_text(e.get("message"))
    replies = e.get("recent_replies") or e.get("replies")
    if isinstance(replies, list):
        out["replies"] = [slim_discussion_entry(r) for r in replies]
    return with_local_times(out, "created_at")


def slim_enrollment(e: dict[str, Any]) -> dict[str, Any]:
    out = pick(e, "id", "type", "enrollment_state", "course_id")
    grades = e.get("grades")
    if isinstance(grades, dict):
        out["grades"] = pick(
            grades,
            "current_score", "current_grade", "final_score", "final_grade",
            "unposted_current_score", "unposted_current_grade",
        )
    out.setdefault(
        "grades",
        pick(e, "computed_current_score", "computed_current_grade",
             "computed_final_score", "computed_final_grade"),
    )
    return out


def slim_calendar_event(ev: dict[str, Any]) -> dict[str, Any]:
    out = pick(
        ev,
        "id", "title", "start_at", "end_at", "location_name", "html_url",
        "context_name", "type",
    )
    assignment = ev.get("assignment")
    if isinstance(assignment, dict):
        out["assignment"] = with_local_times(
            pick(assignment, "id", "name", "due_at", "points_possible"), "due_at"
        )
    return with_local_times(out, "start_at", "end_at")


def slim_user(u: dict[str, Any]) -> dict[str, Any]:
    return pick(u, "id", "name", "short_name", "sortable_name", "login_id", "primary_email")
