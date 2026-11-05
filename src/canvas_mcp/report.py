"""Orchestration glue: fetch status → compute progress → write files → render.

Ties together the HTTP layer (:mod:`canvas_mcp.crawl`), the pure math
(:mod:`canvas_mcp.progress`), the config store (:mod:`canvas_mcp.grading`) and
the renderer (:mod:`canvas_mcp.dashboard`). Writes only to ``data/``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import dashboard, grading
from .client import CanvasClient
from .crawl import build_assignment_status
from .progress import compute_course_progress


async def _active_courses(client: CanvasClient, course_id: int | None) -> list[dict]:
    courses = await client.get_paginated(
        "/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]}
    )
    courses = [c for c in courses if isinstance(c, dict) and c.get("name")]
    if course_id is not None:
        courses = [c for c in courses if c.get("id") == course_id]
    return courses


def course_progress_from_config(
    course: dict[str, Any],
    status_records: list[dict[str, Any]],
    now: datetime,
    base: Path | str | None = None,
) -> dict[str, Any]:
    """Progress for one course: compute from confirmed grading.json, or emit an
    `unconfirmed` stub when only a draft (or nothing) exists."""
    code = course.get("course_code")
    confirmed = grading.load_grading(code, base)
    if confirmed:
        return compute_course_progress(confirmed, status_records, now)
    return {
        "course_id": course.get("id"),
        "course_code": code,
        "course_name": course.get("name"),
        "status": "unconfirmed",
        "has_draft": grading.load_draft(code, base) is not None,
    }


async def refresh_progress(
    client: CanvasClient,
    now: datetime,
    base: Path | str | None = None,
    course_id: int | None = None,
    render: bool = True,
) -> list[dict[str, Any]]:
    """Recompute progress for all active courses (or one), writing each
    ``progress.json`` and, when `render`, the combined ``dashboard.html``."""
    status = await build_assignment_status(client, now, course_id=course_id)
    by_course: dict[Any, list] = defaultdict(list)
    for rec in status["assignments"]:
        by_course[rec.get("course_id")].append(rec)

    courses = await _active_courses(client, course_id)
    results: list[dict[str, Any]] = []
    for course in courses:
        p = course_progress_from_config(
            course, by_course.get(course["id"], []), now, base
        )
        p["generated_at"] = now.isoformat(timespec="seconds")
        grading._write_json(
            grading.course_dir(course.get("course_code"), base) / "progress.json", p
        )
        results.append(p)

    if render:
        write_dashboard(results, now.isoformat(timespec="seconds"), base)
    return results


def load_all_progress(base: Path | str | None = None) -> list[dict[str, Any]]:
    """Read every saved progress.json (for re-rendering without hitting Canvas)."""
    root = grading.data_root(base) / "courses"
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        p = grading._read_json(d / "progress.json")
        if p:
            out.append(p)
    return out


def write_dashboard(
    progress_list: list[dict[str, Any]],
    generated_at: str | None,
    base: Path | str | None = None,
) -> Path:
    html = dashboard.render_html(progress_list, generated_at)
    path = grading.data_root(base) / "dashboard.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Amendment flow (shared by scripts/amend_grading.py and the MCP tools)
# ---------------------------------------------------------------------------

def text_summary(progress_list: list[dict[str, Any]]) -> str:
    """A concise Chinese summary, including which assignments are now dropped."""
    lines: list[str] = []
    for p in progress_list:
        code = p.get("course_code")
        if p.get("status") == "unconfirmed":
            lines.append(f"{code}：权重待确认，请先 confirm_grading.py")
            continue
        if p.get("status") == "ungraded":
            lines.append(f"{code}：不计成绩，已跳过")
            continue
        a = next((t for t in p.get("targets", []) if t["label"] == "A"), {})
        if a.get("state") == "locked":
            need = "拿 A 已锁定"
        elif a.get("state") == "impossible":
            need = "拿 A 无法达到"
        else:
            need = f"拿 A 还需平均 {a.get('need_pct')}%"
        drops = []
        for it in p.get("items", []):
            for s in it.get("assignments", []):
                if s.get("status") == "dropped":
                    tent = "（暂定）" if s.get("drop_tentative") else ""
                    drops.append(f"{it['name']}·{s.get('name')}{tent}")
        line = (f"{code}：保底 {p['floor']}，上限 {p['ceiling']}，"
                f"已评均 {p['graded_avg']}%，{need}")
        if drops:
            line += f"；当前 drop：{', '.join(drops)}"
        lines.append(line)
    return "\n".join(lines)


async def get_course(client: CanvasClient, course_id: int) -> dict | None:
    for c in await _active_courses(client, course_id):
        return c
    return None


async def _course_status(client, cid, now) -> list[dict[str, Any]]:
    status = await build_assignment_status(client, now, course_id=cid)
    return status["assignments"]


async def prepare_amendment(
    client: CanvasClient,
    course: dict[str, Any],
    instruction: str,
    now: datetime,
    base: Path | str | None = None,
) -> dict[str, Any]:
    """Parse a NL instruction into a validated patch and compute its impact,
    WITHOUT writing anything. Returns a result dict with one of:
      {"ok": True, patch, new_grading, before, after, impact, diff}
      {"ok": False, "clarification": "..."}  — ambiguous, ask the user
      {"ok": False, "error": "..."}          — hard validation failure
    """
    from . import llm  # deferred

    code = course.get("course_code")
    cid = course.get("id")
    confirmed = grading.load_grading(code, base)
    if not confirmed:
        return {"ok": False, "error": f"{code} 还没有确认的 grading.json，请先 confirm_grading。"}

    assignments = await course_assignments_brief_cached(client, cid)
    try:
        patch = await llm.parse_amendment(instruction, confirmed, assignments)
    except llm.LLMError as exc:
        return {"ok": False, "error": str(exc)}

    if not (patch.get("operations") or []):
        return {"ok": False, "clarification": patch.get("clarification")
                or "没能从这句话里解析出明确的修改，请再具体一点。"}

    try:
        new_grading, _warn = grading.apply_patch(confirmed, patch, assignments)
    except grading.PatchError as exc:
        return {"ok": False, "error": str(exc)}

    recs = await _course_status(client, cid, now)
    before = compute_course_progress(confirmed, recs, now)
    after = compute_course_progress(new_grading, recs, now)
    return {
        "ok": True,
        "course_id": cid,
        "course_code": code,
        "patch": patch,
        "new_grading": new_grading,
        "before": before,
        "after": after,
        "impact": grading.progress_impact(before, after),
        "diff": grading.item_diff(confirmed, new_grading),
    }


async def course_assignments_brief_cached(client, cid):
    from .crawl import course_assignments_brief
    return await course_assignments_brief(client, cid)


async def commit_amendment(
    client: CanvasClient,
    course: dict[str, Any],
    new_grading: dict[str, Any],
    instruction: str,
    patch: dict[str, Any],
    now: datetime,
    base: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Persist the new grading + history, then recompute and re-render."""
    code = course.get("course_code")
    grading.record_change(code, new_grading, instruction, patch, now, base)
    return await refresh_progress(client, now, base=base, course_id=course.get("id"))
