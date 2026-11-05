"""Patch validation/application, pending expiry, undo, and the amendment flow."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from canvas_mcp import grading as G
from canvas_mcp import report
from canvas_mcp.client import CanvasClient
from canvas_mcp.progress import compute_course_progress

BASE = "https://canvas.example.edu"
CANVAS = [{"id": 1, "name": "A1"}, {"id": 2, "name": "A2"}, {"id": 3, "name": "Quiz 3"}]


def base_grading():
    return {
        "course_id": 1, "course_code": "X", "course_name": "X", "graded": True,
        "items": [
            {"name": "HW", "type": "assignment", "weight": 60,
             "rules": {"drop_lowest": 0, "drop_highest": 0, "never_drop": []}},
            {"name": "Final", "type": "exam", "weight": 40,
             "rules": {"drop_lowest": 0, "drop_highest": 0, "never_drop": []}},
        ],
        "mapping": {"HW": [1, 2]},
        "manual_scores": {},
        "grade_scale": {"A": 85, "A-": 80, "B+": 77},
    }


def patch(*ops):
    return {"operations": list(ops)}


def op(kind, **kw):
    return {"op": kind, "source_text": "…", **kw}


# ---------------------------------------------------------------------------
# One case per operation
# ---------------------------------------------------------------------------

def test_add_item_with_rebalance():
    p = patch(
        op("add_item", name="Participation", type="participation", weight=5),
        op("update_item", name="Final", weight=35),
    )
    g, _ = G.apply_patch(base_grading(), p, CANVAS)
    names = {i["name"]: i["weight"] for i in g["items"]}
    assert names["Participation"] == 5 and names["Final"] == 35


def test_update_item_rules():
    p = patch(op("update_item", name="HW", rules={"drop_lowest": 1}))
    g, _ = G.apply_patch(base_grading(), p, CANVAS)
    hw = next(i for i in g["items"] if i["name"] == "HW")
    assert hw["rules"]["drop_lowest"] == 1


def test_remove_item_with_rebalance():
    p = patch(op("remove_item", name="Final"), op("update_item", name="HW", weight=100))
    g, _ = G.apply_patch(base_grading(), p, CANVAS)
    assert [i["name"] for i in g["items"]] == ["HW"]
    assert g["items"][0]["weight"] == 100


def test_set_manual_score_by_item_name():
    p = patch(op("set_manual_score", key="Final", score=21, out_of=25))
    g, _ = G.apply_patch(base_grading(), p, CANVAS)
    assert g["manual_scores"]["Final"] == {"score": 21, "out_of": 25}


def test_set_manual_score_by_assignment_id():
    p = patch(op("set_manual_score", key=1, score=9, out_of=10))
    g, _ = G.apply_patch(base_grading(), p, CANVAS)
    assert g["manual_scores"]["1"] == {"score": 9, "out_of": 10}


def test_clear_manual_score():
    start = base_grading()
    start["manual_scores"] = {"Final": {"score": 20, "out_of": 25}}
    p = patch(op("clear_manual_score", key="Final"))
    g, _ = G.apply_patch(start, p, CANVAS)
    assert "Final" not in g["manual_scores"]


def test_map_and_unmap_assignment():
    g1, _ = G.apply_patch(base_grading(), patch(op("map_assignment", item="Final", assignment_id=3)), CANVAS)
    assert g1["mapping"]["Final"] == [3]
    g2, _ = G.apply_patch(g1, patch(op("unmap_assignment", item="Final", assignment_id=3)), CANVAS)
    assert g2["mapping"]["Final"] == []


def test_set_grade_scale():
    p = patch(op("set_grade_scale", scale={"A": 90, "B": 80}))
    g, _ = G.apply_patch(base_grading(), p, CANVAS)
    assert g["grade_scale"] == {"A": 90.0, "B": 80.0}


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

def test_reject_weight_sum_not_100():
    p = patch(op("add_item", name="Participation", type="participation", weight=5))
    with pytest.raises(G.PatchError) as e:
        G.apply_patch(base_grading(), p, CANVAS)
    assert "100" in str(e.value)


def test_reject_unknown_assignment_id_on_map():
    p = patch(op("map_assignment", item="Final", assignment_id=999))
    with pytest.raises(G.PatchError):
        G.apply_patch(base_grading(), p, CANVAS)


def test_reject_unknown_assignment_id_on_manual():
    p = patch(op("set_manual_score", key=999, score=1, out_of=2))
    with pytest.raises(G.PatchError):
        G.apply_patch(base_grading(), p, CANVAS)


def test_reject_drop_ge_assignment_count():
    # HW has 2 assignments; drop_lowest 2 is not allowed
    p = patch(op("update_item", name="HW", rules={"drop_lowest": 2}))
    with pytest.raises(G.PatchError) as e:
        G.apply_patch(base_grading(), p, CANVAS)
    assert "drop" in str(e.value)


def test_reject_update_nonexistent_item():
    with pytest.raises(G.PatchError):
        G.apply_patch(base_grading(), patch(op("update_item", name="Nope", weight=10)), CANVAS)


def test_reject_score_gt_out_of():
    with pytest.raises(G.PatchError):
        G.apply_patch(base_grading(), patch(op("set_manual_score", key="Final", score=30, out_of=25)), CANVAS)


def test_reject_empty_patch():
    with pytest.raises(G.PatchError):
        G.apply_patch(base_grading(), {"operations": []}, CANVAS)


# ---------------------------------------------------------------------------
# Pending patches: expiry
# ---------------------------------------------------------------------------

def test_pending_expiry():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    fresh = {"created_at": (now - timedelta(hours=1)).isoformat()}
    stale = {"created_at": (now - timedelta(hours=25)).isoformat()}
    assert G.is_pending_expired(fresh, now) is False
    assert G.is_pending_expired(stale, now) is True


def test_pending_roundtrip(tmp_path):
    payload = {"patch_id": "abc123", "course_code": "X", "created_at": "2026-11-04T12:00:00+00:00"}
    G.save_pending("X", payload, base=tmp_path)
    assert G.load_pending("abc123", base=tmp_path)["patch_id"] == "abc123"
    G.delete_pending("X", "abc123", base=tmp_path)
    assert G.load_pending("abc123", base=tmp_path) is None


# ---------------------------------------------------------------------------
# Undo restores grading AND progress
# ---------------------------------------------------------------------------

def test_undo_restores_progress(tmp_path):
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    g0 = base_grading()
    # baseline
    G._write_history("X", [{"at": "t0", "instruction": "confirmed", "patch": None,
                            "grading_after": g0}], base=tmp_path)
    G.save_grading("X", g0, base=tmp_path)

    recs = [
        {"assignment_id": 1, "name": "A1", "status": "graded", "score": 8, "points_possible": 10},
        {"assignment_id": 2, "name": "A2", "status": "graded", "score": 6, "points_possible": 10},
    ]
    before = compute_course_progress(g0, recs, now)

    # apply a change that shifts weight
    g1, _ = G.apply_patch(g0, patch(op("update_item", name="HW", weight=80),
                                    op("update_item", name="Final", weight=20)), CANVAS)
    G.record_change("X", g1, "shift", {"operations": []}, now, base=tmp_path)
    after = compute_course_progress(G.load_grading("X", base=tmp_path), recs, now)
    assert after["totals"]["earned"] != before["totals"]["earned"]

    restored = G.undo_last("X", base=tmp_path)
    assert restored == g0
    reverted = compute_course_progress(G.load_grading("X", base=tmp_path), recs, now)
    assert reverted["totals"] == before["totals"]


# ---------------------------------------------------------------------------
# Amendment flow (LLM mocked, Canvas via respx)
# ---------------------------------------------------------------------------

@respx.mock
async def test_prepare_amendment_happy_path(tmp_path, monkeypatch):
    G.save_grading("X", base_grading(), base=tmp_path)

    async def fake_parse(instruction, grading, assignments):
        return patch(op("set_manual_score", key="Final", score=21, out_of=25))
    monkeypatch.setattr("canvas_mcp.llm.parse_amendment", fake_parse)

    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "X", "course_code": "X"}])
    )
    respx.get(f"{BASE}/api/v1/courses/1/assignment_groups").mock(
        return_value=httpx.Response(200, json=[{"name": "HW", "group_weight": 60,
            "assignments": [{"id": 1, "name": "A1"}, {"id": 2, "name": "A2"}]}])
    )
    respx.get(f"{BASE}/api/v1/courses/1/students/submissions").mock(
        return_value=httpx.Response(200, json=[{
            "submitted_at": "2026-10-01T00:00:00Z", "score": 8, "points_possible": 10,
            "assignment": {"id": 1, "name": "A1", "submission_types": ["online_upload"]},
        }])
    )
    respx.get(f"{BASE}/api/v1/courses/1/modules").mock(return_value=httpx.Response(200, json=[]))

    client = CanvasClient(BASE, "t")
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    try:
        res = await report.prepare_amendment(
            client, {"id": 1, "course_code": "X", "name": "X"},
            "期中考了 21/25", now, base=tmp_path,
        )
    finally:
        await client.aclose()
    assert res["ok"] is True
    assert res["new_grading"]["manual_scores"]["Final"] == {"score": 21, "out_of": 25}
    assert res["before"] and res["after"]


@respx.mock
async def test_prepare_amendment_returns_clarification(tmp_path, monkeypatch):
    G.save_grading("X", base_grading(), base=tmp_path)

    async def fake_parse(instruction, grading, assignments):
        return {"operations": [], "clarification": "你指的是哪一个 final？"}
    monkeypatch.setattr("canvas_mcp.llm.parse_amendment", fake_parse)

    respx.get(f"{BASE}/api/v1/courses/1/assignment_groups").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{BASE}/api/v1/courses/1/assignments").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = CanvasClient(BASE, "t")
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    try:
        res = await report.prepare_amendment(
            client, {"id": 1, "course_code": "X", "name": "X"},
            "改一下", now, base=tmp_path,
        )
    finally:
        await client.aclose()
    assert res["ok"] is False
    assert "final" in res["clarification"]
