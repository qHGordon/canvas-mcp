"""Pure-function tests for grade-progress computation (no network)."""

import math

from canvas_mcp.progress import compute_course_progress


def rec(aid, status, score=None, pp=None, due=None, flags=None, name=None):
    r = {"assignment_id": aid, "name": name or f"A{aid}", "status": status}
    if score is not None:
        r["score"] = score
    if pp is not None:
        r["points_possible"] = pp
    if due:
        r["due_at"] = due
    if flags:
        r["flags"] = flags
    return r


def grading(items, mapping=None, manual=None, graded=True, scale=None):
    g = {
        "course_id": 1, "course_code": "X", "course_name": "X",
        "graded": graded, "items": items, "mapping": mapping or {},
        "manual_scores": manual or {},
    }
    if scale:
        g["grade_scale"] = scale
    return g


def item(name, weight, rules=None, type="assignment"):
    return {"name": name, "type": type, "weight": weight, "rules": rules or {}}


def _sum_four(p):
    t = p["totals"]
    return t["earned"] + t["lost"] + t["pending"] + t["remaining"]


def by_name(p, name):
    return next(i for i in p["items"] if i["name"] == name)


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------

def test_all_graded():
    g = grading([item("HW", 100)], mapping={"HW": [1, 2]})
    recs = [rec(1, "graded", 8, 10), rec(2, "graded", 6, 10)]
    p = compute_course_progress(g, recs)
    assert p["totals"]["earned"] == 70
    assert p["totals"]["lost"] == 30
    assert p["floor"] == 70 and p["ceiling"] == 70
    assert p["graded_avg"] == 70.0
    assert math.isclose(_sum_four(p), 100)


def test_all_unsubmitted_future():
    g = grading([item("HW", 100)], mapping={"HW": [1, 2]})
    recs = [rec(1, "not_submitted"), rec(2, "not_submitted")]
    p = compute_course_progress(g, recs)
    assert p["totals"]["remaining"] == 100
    assert p["floor"] == 0 and p["ceiling"] == 100
    assert p["graded_avg"] is None


def test_mixed_statuses():
    g = grading([item("HW", 90)], mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "graded", 9, 10), rec(2, "submitted_ungraded"),
            rec(3, "not_submitted")]
    p = compute_course_progress(g, recs)
    t = p["totals"]
    assert math.isclose(t["earned"], 27)
    assert math.isclose(t["lost"], 3)
    assert math.isclose(t["pending"], 30)
    assert math.isclose(t["remaining"], 30)
    assert math.isclose(_sum_four(p), 90)


def test_multiple_items_sum_to_100():
    g = grading(
        [item("Tests", 90), item("Exercises", 10)],
        mapping={"Tests": [1], "Exercises": [2, 3]},
    )
    recs = [rec(1, "graded", 80, 100), rec(2, "graded", 10, 10),
            rec(3, "not_submitted")]
    p = compute_course_progress(g, recs)
    assert math.isclose(_sum_four(p), 100)


def test_manual_score_standalone_item():
    g = grading([item("Midterm", 20, type="exam")],
                manual={"Midterm": {"score": 21, "out_of": 25}})
    p = compute_course_progress(g, [])
    t = p["totals"]
    assert math.isclose(t["earned"], 20 * 21 / 25)
    assert math.isclose(t["lost"], 20 * 4 / 25)


def test_manual_score_overrides_canvas_by_id():
    g = grading([item("HW", 10)], mapping={"HW": [1]},
                manual={"1": {"score": 9, "out_of": 10}})
    recs = [rec(1, "graded", 5, 10)]
    p = compute_course_progress(g, recs)
    assert math.isclose(p["totals"]["earned"], 9)  # manual 9/10 beats canvas 5/10


def test_excused_removed_from_item():
    g = grading([item("HW", 100)], mapping={"HW": [1, 2]})
    recs = [rec(1, "graded", 8, 10), rec(2, "excused")]
    p = compute_course_progress(g, recs)
    # Only one effective assignment → it owns the full 100
    assert math.isclose(p["totals"]["earned"], 80)
    assert math.isclose(p["totals"]["lost"], 20)
    hw = by_name(p, "HW")
    assert any(a["status"] == "excused" for a in hw["assignments"])


def test_targets_locked_and_impossible():
    g = grading([item("HW", 100)], mapping={"HW": [1, 2]})
    recs = [rec(1, "graded", 90, 100), rec(2, "not_submitted")]
    p = compute_course_progress(g, recs)
    # earned 45, headroom 50
    tgt = {t["label"]: t for t in p["targets"]}
    assert tgt["B-"]["state"] == "reachable"  # need (70-45)/50 = 50%
    # A+ cutoff 90: need (90-45)/50 = 90% reachable
    assert tgt["A+"]["state"] == "reachable"
    # Lock one up: give nearly everything
    recs2 = [rec(1, "graded", 100, 100), rec(2, "graded", 95, 100)]
    p2 = compute_course_progress(g, recs2)
    tgt2 = {t["label"]: t for t in p2["targets"]}
    assert tgt2["A"]["state"] == "locked"  # earned 97.5 >= 85


def test_target_impossible_when_headroom_too_small():
    g = grading([item("HW", 100)], mapping={"HW": [1, 2]})
    # earned 10, headroom 50 -> A (85) needs 150% -> impossible
    recs = [rec(1, "graded", 20, 100), rec(2, "not_submitted")]
    p = compute_course_progress(g, recs)
    tgt = {t["label"]: t for t in p["targets"]}
    assert tgt["A"]["state"] == "impossible"


def test_ungraded_course_skipped():
    g = grading([], graded=False)
    p = compute_course_progress(g, [])
    assert p["status"] == "ungraded"
    assert "totals" not in p


# ---------------------------------------------------------------------------
# Drop rules
# ---------------------------------------------------------------------------

def test_drop_lowest_basic():
    g = grading([item("HW", 100, rules={"drop_lowest": 1})],
                mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "graded", 9, 10), rec(2, "graded", 6, 10),
            rec(3, "graded", 3, 10)]
    p = compute_course_progress(g, recs)
    # m = 2, w = 50; drop the 0.3, count 0.9 + 0.6
    assert math.isclose(p["totals"]["earned"], 50 * 1.5)
    hw = by_name(p, "HW")
    dropped = [a for a in hw["assignments"] if a["status"] == "dropped"]
    assert len(dropped) == 1 and dropped[0]["assignment_id"] == 3
    assert dropped[0]["drop_reason"] == "当前最低分"
    assert hw["drop_tentative"] is False  # all graded


def test_drop_switches_when_lower_grade_arrives():
    g = grading([item("HW", 100, rules={"drop_lowest": 1})],
                mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "graded", 9, 10), rec(2, "graded", 3, 10),
            rec(3, "graded", 2, 10)]
    p = compute_course_progress(g, recs)
    hw = by_name(p, "HW")
    dropped = [a for a in hw["assignments"] if a["status"] == "dropped"]
    assert dropped[0]["assignment_id"] == 3  # the new lowest (0.2)


def test_drop_g_empty_is_tentative():
    g = grading([item("HW", 100, rules={"drop_lowest": 1})],
                mapping={"HW": [1, 2, 3, 4]})
    recs = [rec(i, "not_submitted") for i in (1, 2, 3, 4)]
    p = compute_course_progress(g, recs)
    hw = by_name(p, "HW")
    assert hw["drop_tentative"] is True
    assert math.isclose(p["totals"]["remaining"], 100)


def test_drop_g_less_than_k():
    g = grading([item("HW", 100, rules={"drop_lowest": 2})],
                mapping={"HW": [1, 2, 3, 4]})
    recs = [rec(1, "graded", 5, 10), rec(2, "not_submitted"),
            rec(3, "not_submitted"), rec(4, "not_submitted")]
    p = compute_course_progress(g, recs)
    # n=4, drops=2, m=2, w=50. The one graded (0.5) is the lowest -> dropped.
    # 1 unused drop taken from remaining. r_eff = 2 -> remaining 100.
    assert math.isclose(p["totals"]["earned"], 0)
    assert math.isclose(p["totals"]["remaining"], 100)
    assert by_name(p, "HW")["drop_tentative"] is True


def test_drop_g_greater_than_k():
    g = grading([item("HW", 100, rules={"drop_lowest": 1})],
                mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "graded", 9, 10), rec(2, "graded", 8, 10),
            rec(3, "graded", 7, 10)]
    p = compute_course_progress(g, recs)
    # drop 0.7, count 0.9 + 0.8, m=2 w=50
    assert math.isclose(p["totals"]["earned"], 50 * 1.7)


def test_never_drop_excluded():
    g = grading([item("HW", 100, rules={"drop_lowest": 1, "never_drop": [1]})],
                mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "graded", 2, 10), rec(2, "graded", 9, 10),
            rec(3, "graded", 6, 10)]
    p = compute_course_progress(g, recs)
    hw = by_name(p, "HW")
    dropped = [a for a in hw["assignments"] if a["status"] == "dropped"]
    # id 1 (lowest) is protected -> drop next lowest, id 3 (0.6)
    assert dropped[0]["assignment_id"] == 3


def test_drop_tie_breaks_by_earlier_due():
    g = grading([item("HW", 100, rules={"drop_lowest": 1})],
                mapping={"HW": [1, 2]})
    recs = [rec(1, "graded", 5, 10, due="2026-10-01T00:00:00Z"),
            rec(2, "graded", 5, 10, due="2026-09-01T00:00:00Z")]
    p = compute_course_progress(g, recs)
    hw = by_name(p, "HW")
    dropped = [a for a in hw["assignments"] if a["status"] == "dropped"]
    assert dropped[0]["assignment_id"] == 2  # earlier due date


def test_overdue_zero_dropped_first():
    g = grading([item("HW", 100, rules={"drop_lowest": 1})],
                mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "not_submitted", due="2026-01-01T00:00:00Z", flags=["overdue"]),
            rec(2, "graded", 5, 10), rec(3, "graded", 9, 10)]
    p = compute_course_progress(g, recs)
    hw = by_name(p, "HW")
    dropped = [a for a in hw["assignments"] if a["status"] == "dropped"]
    assert dropped[0]["assignment_id"] == 1  # the overdue zero
    assert math.isclose(p["totals"]["earned"], 50 * (0.5 + 0.9))


def test_unused_drop_from_remaining_before_pending():
    g = grading([item("HW", 100, rules={"drop_lowest": 2})],
                mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "graded", 5, 10), rec(2, "submitted_ungraded"),
            rec(3, "not_submitted")]
    p = compute_course_progress(g, recs)
    # n=3 drops=2 m=1 w=100. Drop the graded (0.5); 1 unused drop hits
    # remaining first -> remaining 0, pending keeps its slot -> pending 100.
    t = p["totals"]
    assert math.isclose(t["earned"], 0)
    assert math.isclose(t["pending"], 100)
    assert math.isclose(t["remaining"], 0)


def test_drop_highest():
    g = grading([item("HW", 100, rules={"drop_highest": 1})],
                mapping={"HW": [1, 2, 3]})
    recs = [rec(1, "graded", 9, 10), rec(2, "graded", 6, 10),
            rec(3, "graded", 3, 10)]
    p = compute_course_progress(g, recs)
    hw = by_name(p, "HW")
    dropped = [a for a in hw["assignments"] if a["status"] == "dropped"]
    assert dropped[0]["assignment_id"] == 1  # highest
    assert dropped[0]["drop_reason"] == "当前最高分"
    # count 0.6 + 0.3, m=2 w=50
    assert math.isclose(p["totals"]["earned"], 50 * 0.9)
