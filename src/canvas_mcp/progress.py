"""Course grade-progress computation. Pure functions — no HTTP, no I/O.

Inputs are a confirmed ``grading.json`` dict plus the assignment-status records
produced by :func:`canvas_mcp.crawl.build_assignment_status`. All scores and
percentages are computed here, in code — never inferred by an LLM.

Weight model (see CLAUDE.md §"课程成绩进度")
-------------------------------------------------
Every item owns a total weight ``W``. With ``n`` effective (non-excused)
assignments and ``d = drop_lowest + drop_highest`` drops, there are ``m = n-d``
scoring slots, each worth ``w = W/m``. The four course buckets — earned, lost,
pending, remaining — always sum to the item's weight, and across items to 100.

Drop selection is redone from scratch on every call: the lowest *graded* scores
are dropped, so a later, lower grade automatically displaces an earlier drop.
Drops that cannot yet be applied (not enough graded work) are held in reserve
and taken from remaining work first, then pending.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

DEFAULT_GRADE_SCALE: dict[str, float] = {
    "A+": 90, "A": 85, "A-": 80, "B+": 77, "B": 73, "B-": 70,
}

# Which scale lines the dashboard offers as switchable target lines.
DASHBOARD_TARGETS = ["A", "A-", "B+"]

_FAR_FUTURE = "9999-12-31T23:59:59Z"


# ---------------------------------------------------------------------------
# Per-assignment unit
# ---------------------------------------------------------------------------

def _rate(score: float | None, out_of: float | None) -> float:
    """Fraction earned. Guards Canvas's points_possible == 0 quirk."""
    if score is None:
        return 0.0
    if not out_of:  # 0 or None
        return 1.0 if score > 0 else 0.0
    return score / out_of


def _manual_lookup(manual: dict[str, Any], *keys: Any) -> dict[str, Any] | None:
    for k in keys:
        if k is None:
            continue
        v = manual.get(k)
        if v is None and not isinstance(k, str):
            v = manual.get(str(k))
        if isinstance(v, dict):
            return v
    return None


def _build_unit(
    assignment_id: Any,
    name: str | None,
    rec: dict[str, Any] | None,
    manual: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalise one assignment into a unit the item math consumes.

    bucket is one of: "graded" (has a numeric score, incl. overdue-as-0),
    "pending" (submitted, awaiting grade), "remaining" (not yet done / unknown),
    "excused" (removed entirely)."""
    unit: dict[str, Any] = {
        "assignment_id": assignment_id,
        "name": name or (rec or {}).get("name"),
        "score": None,
        "points_possible": None,
        "rate": None,
        "due_at": (rec or {}).get("due_at") if rec else None,
        "source": "canvas" if rec else None,
    }

    if manual:
        score = manual.get("score")
        out_of = manual.get("out_of")
        unit.update(
            score=score, points_possible=out_of, rate=_rate(score, out_of),
            bucket="graded", source="manual",
        )
        return unit

    if rec is None:
        # Mapped id with no status record — treat as not-yet-done.
        unit["bucket"] = "remaining"
        return unit

    status = rec.get("status")
    flags = rec.get("flags") or []
    if status == "excused":
        unit["bucket"] = "excused"
        return unit
    if status == "graded":
        score = rec.get("score")
        out_of = rec.get("points_possible")
        unit.update(
            score=score, points_possible=out_of, rate=_rate(score, out_of),
            bucket="graded",
        )
        return unit
    if status == "not_submitted" and "overdue" in flags:
        # Past due and never submitted → a locked-in zero.
        unit.update(
            score=0, points_possible=rec.get("points_possible"), rate=0.0,
            bucket="graded", zero_from="overdue",
        )
        return unit
    if status == "submitted_ungraded":
        unit["bucket"] = "pending"
        return unit
    # not_submitted (not overdue) or unknown
    unit["bucket"] = "remaining"
    return unit


# ---------------------------------------------------------------------------
# Item computation
# ---------------------------------------------------------------------------

def _rules(item: dict[str, Any]) -> tuple[int, int, set]:
    r = item.get("rules") or {}
    drop_lowest = int(r.get("drop_lowest") or 0)
    drop_highest = int(r.get("drop_highest") or 0)
    never_drop = set(r.get("never_drop") or [])
    return drop_lowest, drop_highest, never_drop


def _compute_item(
    item: dict[str, Any],
    units: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an item result dict with earned/lost/pending/remaining (summing to
    the item weight) and per-assignment statuses."""
    weight = float(item.get("weight") or 0)
    drop_lowest, drop_highest, never_drop = _rules(item)

    effective = [u for u in units if u.get("bucket") != "excused"]
    excused = [u for u in units if u.get("bucket") == "excused"]
    n = len(effective)

    result: dict[str, Any] = {
        "name": item.get("name"),
        "type": item.get("type"),
        "weight": weight,
        "rules": item.get("rules") or {},
        "earned": 0.0, "lost": 0.0, "pending": 0.0, "remaining": 0.0,
        "assignments": [],
        "drop_tentative": False,
    }

    if n == 0:
        # No real sub-assignments: the whole item is remaining weight.
        result["remaining"] = weight
        for u in excused:
            result["assignments"].append(_unit_out(u, "excused", 0.0))
        return result

    total_drops = max(0, min(drop_lowest + drop_highest, n - 1)) if n > 1 else 0
    # Keep drop_lowest/highest split consistent with the clamp.
    if total_drops < drop_lowest + drop_highest:
        # Clamp proportionally, favouring drop_lowest.
        drop_lowest = min(drop_lowest, total_drops)
        drop_highest = total_drops - drop_lowest
    m = n - total_drops
    w = weight / m if m > 0 else 0.0

    graded = [u for u in effective if u["bucket"] == "graded"]
    pending = [u for u in effective if u["bucket"] == "pending"]
    remaining = [u for u in effective if u["bucket"] == "remaining"]

    droppable = [u for u in graded if u["assignment_id"] not in never_drop]

    def sort_key(u: dict[str, Any]) -> tuple[float, str]:
        return (u["rate"], u.get("due_at") or _FAR_FUTURE)

    dropped_ids: dict[Any, str] = {}
    # drop_lowest: lowest rate first; ties broken by earlier due date.
    low_sorted = sorted(droppable, key=sort_key)
    n_low = min(drop_lowest, len(low_sorted))
    for u in low_sorted[:n_low]:
        dropped_ids[id(u)] = "当前最低分"
    # drop_highest: from the still-counted graded, highest rate first.
    high_candidates = [u for u in droppable if id(u) not in dropped_ids]
    high_sorted = sorted(high_candidates, key=sort_key, reverse=True)
    n_high = min(drop_highest, len(high_sorted))
    for u in high_sorted[:n_high]:
        dropped_ids[id(u)] = "当前最高分"

    counted = [u for u in graded if id(u) not in dropped_ids]
    sum_rate = sum(u["rate"] for u in counted)
    earned = w * sum_rate
    lost = w * (len(counted) - sum_rate)

    # Drops we could not apply to graded work are held and taken from the
    # future pool — remaining first, then pending.
    drops_applied_to_graded = len(dropped_ids)
    unused_drops = total_drops - drops_applied_to_graded
    drop_from_remaining = min(unused_drops, len(remaining))
    drop_from_pending = min(unused_drops - drop_from_remaining, len(pending))
    r_eff = len(remaining) - drop_from_remaining
    p_eff = len(pending) - drop_from_pending

    result["earned"] = earned
    result["lost"] = lost
    result["pending"] = w * p_eff
    result["remaining"] = w * r_eff
    result["drop_tentative"] = total_drops > 0 and len(graded) < n

    # Per-assignment output. Future units share their bucket's effective weight
    # evenly so the per-row shares still sum to the item total.
    pending_share = (w * p_eff / len(pending)) if pending else 0.0
    remaining_share = (w * r_eff / len(remaining)) if remaining else 0.0
    for u in effective:
        if u["bucket"] == "graded":
            if id(u) in dropped_ids:
                out = _unit_out(u, "dropped", 0.0)
                out["drop_reason"] = dropped_ids[id(u)]
                if result["drop_tentative"]:
                    out["drop_tentative"] = True
            else:
                out = _unit_out(u, "counted", w)
        elif u["bucket"] == "pending":
            out = _unit_out(u, "pending", pending_share)
        else:
            out = _unit_out(u, "remaining", remaining_share)
        result["assignments"].append(out)
    for u in excused:
        result["assignments"].append(_unit_out(u, "excused", 0.0))
    return result


def _unit_out(u: dict[str, Any], status: str, share: float) -> dict[str, Any]:
    out = {
        "assignment_id": u.get("assignment_id"),
        "name": u.get("name"),
        "status": status,
        "weight_share": round(share, 4),
    }
    if u.get("score") is not None:
        out["score"] = u["score"]
        out["points_possible"] = u.get("points_possible")
    if u.get("rate") is not None:
        out["rate"] = round(u["rate"], 4)
    if u.get("zero_from"):
        out["zero_from"] = u["zero_from"]
    if u.get("source") == "manual":
        out["source"] = "manual"
    if u.get("due_at"):
        out["due_at"] = u["due_at"]
    return out


# ---------------------------------------------------------------------------
# Course computation
# ---------------------------------------------------------------------------

def _units_for_item(
    item: dict[str, Any],
    mapping: dict[str, Any],
    status_by_id: dict[Any, dict[str, Any]],
    manual_scores: dict[str, Any],
) -> list[dict[str, Any]]:
    name = item.get("name")
    ids = mapping.get(name) or []
    units: list[dict[str, Any]] = []
    for aid in ids:
        rec = status_by_id.get(aid) or status_by_id.get(str(aid))
        manual = _manual_lookup(manual_scores, aid)
        units.append(_build_unit(aid, (rec or {}).get("name"), rec, manual))
    # Item-level manual score (a standalone exam/paper not on Canvas).
    if not ids:
        manual = _manual_lookup(manual_scores, name)
        if manual:
            units.append(_build_unit(name, name, None, manual))
    return units


def _targets(
    earned: float, pending: float, remaining: float, scale: dict[str, float]
) -> list[dict[str, Any]]:
    headroom = pending + remaining
    out = []
    for label, cutoff in scale.items():
        cutoff = float(cutoff)
        entry: dict[str, Any] = {"label": label, "cutoff": cutoff}
        if earned >= cutoff:
            entry.update(need_pct=None, state="locked")
        elif headroom <= 0:
            entry.update(need_pct=None, state="impossible")
        else:
            need = (cutoff - earned) / headroom
            entry["need_pct"] = round(need * 100, 1)
            entry["state"] = "impossible" if need > 1 else "reachable"
        out.append(entry)
    return out


def compute_course_progress(
    grading: dict[str, Any],
    status_records: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute one course's progress from its confirmed grading config and the
    course's assignment-status records.

    Returns status "ungraded" for ``graded: false`` courses and "ok" otherwise.
    (Unconfirmed courses — draft only — are handled by the caller, which has no
    confirmed grading dict to pass here.)"""
    base = {
        "course_id": grading.get("course_id"),
        "course_code": grading.get("course_code"),
        "course_name": grading.get("course_name"),
        "grade_scale": grading.get("grade_scale") or DEFAULT_GRADE_SCALE,
    }
    if grading.get("graded") is False:
        return {**base, "status": "ungraded"}

    mapping = grading.get("mapping") or {}
    manual_scores = grading.get("manual_scores") or {}
    status_by_id = {}
    for rec in status_records:
        aid = rec.get("assignment_id")
        if aid is not None:
            status_by_id[aid] = rec

    items_out = []
    totals = {"earned": 0.0, "lost": 0.0, "pending": 0.0, "remaining": 0.0}
    for item in grading.get("items") or []:
        units = _units_for_item(item, mapping, status_by_id, manual_scores)
        res = _compute_item(item, units)
        items_out.append(res)
        for key in totals:
            totals[key] += res[key]

    for key in totals:
        totals[key] = round(totals[key], 4)
    earned = totals["earned"]
    lost = totals["lost"]
    pending = totals["pending"]
    remaining = totals["remaining"]
    scale = base["grade_scale"]

    return {
        **base,
        "status": "ok",
        "totals": totals,
        "floor": round(earned, 2),
        "ceiling": round(earned + pending + remaining, 2),
        "graded_avg": round(earned / (earned + lost) * 100, 2)
        if (earned + lost) > 0 else None,
        "weight_total": round(sum(float(i.get("weight") or 0)
                                  for i in grading.get("items") or []), 4),
        "targets": _targets(earned, pending, remaining, scale),
        "items": items_out,
    }
