"""Daily-report pure logic: diff, weekday, deadlines, subject, modules, LLM fallback."""

from datetime import datetime, timezone

import pytest

from canvas_mcp import daily, report_html


NOW = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)  # Wed, ~12:00 EST Toronto


def areport(assignments=None, files=None, announcements=None, progress=None, deadlines=None):
    return {
        "date": "2026-11-04", "generated_at": "2026-11-04T12:00:00-05:00",
        "assignments": assignments or [], "files": files or [],
        "announcements": announcements or [], "progress": progress or [],
        "deadlines": deadlines or [],
    }


# ---------------------------------------------------------------------------
# weekday (computed, never hand-written)
# ---------------------------------------------------------------------------

def test_weekday_saturday_2026_11_07():
    assert daily.weekday_zh(datetime(2026, 11, 7)) == "周六"


def test_weekday_full_week():
    # 2026-11-02 is a Monday
    got = [daily.weekday_zh(datetime(2026, 11, 2 + i)) for i in range(7)]
    assert got == ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------------------
# change detection
# ---------------------------------------------------------------------------

def test_first_run_no_lists():
    curr = areport(
        assignments=[{"assignment_id": 1, "status": "graded", "score": 9}],
        files=[{"file_id": 1, "updated_at": "t"}],
        announcements=[{"id": 1}],
    )
    ch = daily.detect_changes(None, curr)
    assert ch["first_run"] is True
    assert ch["new_grades"] == [] and ch["new_files"] == [] and ch["new_announcements"] == []


def test_new_grade_status_change():
    prev = areport(assignments=[{"assignment_id": 1, "status": "submitted_ungraded"}])
    curr = areport(assignments=[{"assignment_id": 1, "status": "graded", "score": 8}])
    ch = daily.detect_changes(prev, curr)
    assert [a["assignment_id"] for a in ch["new_grades"]] == [1]


def test_score_change_detected():
    prev = areport(assignments=[{"assignment_id": 1, "status": "graded", "score": 7}])
    curr = areport(assignments=[{"assignment_id": 1, "status": "graded", "score": 9}])
    ch = daily.detect_changes(prev, curr)
    assert ch["new_grades"][0]["score"] == 9


def test_no_grade_change():
    prev = areport(assignments=[{"assignment_id": 1, "status": "graded", "score": 9}])
    curr = areport(assignments=[{"assignment_id": 1, "status": "graded", "score": 9}])
    assert daily.detect_changes(prev, curr)["new_grades"] == []


def test_new_and_updated_files():
    prev = areport(files=[{"file_id": 1, "updated_at": "a"}])
    curr = areport(files=[{"file_id": 1, "updated_at": "b"},   # updated
                          {"file_id": 2, "updated_at": "x"}])  # new
    ids = {f["file_id"] for f in daily.detect_changes(prev, curr)["new_files"]}
    assert ids == {1, 2}


def test_new_announcement_only():
    prev = areport(announcements=[{"id": 1}])
    curr = areport(announcements=[{"id": 1}, {"id": 2, "title": "New"}])
    ch = daily.detect_changes(prev, curr)
    assert [a["id"] for a in ch["new_announcements"]] == [2]


def test_progress_delta():
    prev = areport(progress=[{"course_id": 1, "status": "ok", "earned": 40, "floor": 40,
                              "ceiling": 90, "targets": [{"label": "A", "cutoff": 85, "need_pct": 50}]}])
    curr = areport(progress=[{"course_id": 1, "status": "ok", "earned": 45, "floor": 45,
                              "ceiling": 90, "targets": [{"label": "A", "cutoff": 85, "need_pct": 44}]}])
    d = daily.detect_changes(prev, curr)["progress_changes"][1]
    assert d["earned"]["delta"] == 5
    assert d["need_pct"]["delta"] == -6


# ---------------------------------------------------------------------------
# deadlines: grading, ordering, overdue
# ---------------------------------------------------------------------------

def _a(aid, status, due, flags=None):
    return {"assignment_id": aid, "course_id": 1, "course": "C", "name": f"A{aid}",
            "status": status, "due_at": due, "flags": flags or []}


def test_deadlines_classify_and_sort():
    assigns = [
        _a(1, "not_submitted", "2026-11-05T12:00:00Z"),   # ~19h -> 48h
        _a(2, "not_submitted", "2026-11-10T12:00:00Z"),   # ~6d -> 7d
        _a(3, "not_submitted", "2026-11-01T00:00:00Z", ["overdue"]),  # overdue
        _a(4, "graded", "2026-11-05T00:00:00Z"),          # excluded (submitted)
        _a(5, "not_submitted", "2026-12-20T00:00:00Z"),   # beyond 7d -> excluded
    ]
    dls = daily.build_deadlines(assigns, NOW, "https://canvas.example.edu")
    assert [d["assignment_id"] for d in dls] == [3, 1, 2]
    assert dls[0]["category"] == "overdue"
    assert dls[1]["category"] == "within_48h"
    assert dls[2]["category"] == "within_7d"
    assert dls[0]["url"].endswith("/courses/1/assignments/3")


def test_deadlines_empty_when_all_submitted():
    assigns = [_a(1, "graded", "2026-11-05T12:00:00Z")]
    assert daily.build_deadlines(assigns, NOW, "") == []


# ---------------------------------------------------------------------------
# subject priority
# ---------------------------------------------------------------------------

def test_subject_overdue_wins():
    dls = [{"category": "overdue", "course_code": "ABC1001H F", "name": "A2", "weekday": "周六",
            "due_at": "2026-11-01T00:00:00Z"}]
    assert "已逾期" in report_html.build_subject(NOW, dls)
    assert "ABC1001H" in report_html.build_subject(NOW, dls)


def test_subject_none():
    s = report_html.build_subject(NOW, [])
    assert "今日无紧急事项" in s
    assert "周三" in s  # 2026-11-04 is a Wednesday


# ---------------------------------------------------------------------------
# modules show only when there is content
# ---------------------------------------------------------------------------

def test_modules_hidden_when_empty():
    rep = areport(progress=[], deadlines=[])
    changes = {"first_run": False, "new_grades": [], "new_files": [],
               "new_announcements": [], "progress_changes": {}}
    html, text = report_html.render_email(rep, changes, NOW, {})
    assert "③ 新出分" not in html and "④ 新课件" not in html and "⑤ 新公告" not in html
    assert "今日无变化" in html
    assert "② 临近 DDL" in html  # always shown
    assert "未来 7 天无 DDL" in html


def test_modules_shown_when_content():
    rep = areport(
        deadlines=[{"category": "within_7d", "course_code": "C", "course": "C", "name": "A1",
                    "weekday": "周五", "due_at": "2026-11-06T12:00:00Z",
                    "due_local": "2026-11-06 07:00 EST", "days_left": 2, "url": "u"}],
    )
    changes = {"first_run": False,
               "new_grades": [{"course_code": "C", "name": "A1", "score": 9, "points_possible": 10}],
               "new_files": [{"course_code": "C", "display_name": "s.pdf", "module": "W1", "size": 1048576}],
               "new_announcements": [{"id": 1, "title": "T", "summary": "s", "summarized": True,
                                      "posted_at": "2026-11-03T00:00:00Z", "html_url": "u"}],
               "progress_changes": {}}
    html, _ = report_html.render_email(rep, changes, NOW, {})
    assert "③ 新出分" in html and "④ 新课件" in html and "⑤ 新公告" in html


def test_class_schedule_rendered():
    rep = areport(
        deadlines=[],
    )
    rep["classes"] = [{
        "type": "lecture", "type_zh": "上课", "date": "2026-11-04", "weekday": "周三",
        "start": "19:00", "end": "21:00", "location": "Teams (online)",
        "course_code": "XYZ2002H F LEC0101", "course": "XYZ2002H",
    }]
    changes = {"first_run": False, "new_grades": [], "new_files": [],
               "new_announcements": [], "progress_changes": {}}
    html, text = report_html.render_email(rep, changes, NOW, {})
    assert "本周上课" in html
    assert "XYZ2002H" in html and "19:00–21:00" in html
    assert "📚课" in html  # the calendar strip marks the class day
    assert "本周上课" in text


def test_new_grade_zero_points_note():
    changes = {"first_run": False, "new_grades": [
        {"course_code": "C", "name": "A1", "score": 100, "points_possible": 0}],
        "new_files": [], "new_announcements": [], "progress_changes": {}}
    html, _ = report_html.render_email(areport(), changes, NOW, {})
    assert "满分未设置" in html


def test_has_content_and_skip():
    empty = {"first_run": False, "new_grades": [], "new_files": [],
             "new_announcements": [], "progress_changes": {}}
    assert daily.has_content(empty, []) is False
    assert daily.has_content(empty, [{"x": 1}]) is True
    assert daily.has_content({**empty, "new_grades": [{"a": 1}]}, []) is True


# ---------------------------------------------------------------------------
# LLM summary fallback
# ---------------------------------------------------------------------------

async def test_summary_fallback_no_llm(tmp_path):
    anns = [{"id": 1, "title": "T", "message_text": "x" * 300}]
    out = await daily.summarize_announcements(anns, use_llm=False, base=tmp_path)
    assert out[0]["summarized"] is False
    assert out[0]["summary"] == "x" * 150 + "…"


async def test_summary_fallback_when_llm_errors(tmp_path, monkeypatch):
    from canvas_mcp import lms, llm

    monkeypatch.setattr(lms, "ensure_model", lambda m: False)

    async def boom(title, body, **kw):
        raise llm.LLMError("timeout")
    monkeypatch.setattr(llm, "summarize_announcement", boom)

    anns = [{"id": 1, "title": "T", "message_text": "hello world"}]
    out = await daily.summarize_announcements(anns, use_llm=True, base=tmp_path)
    assert out[0]["summarized"] is False
    assert out[0]["summary"] == "hello world"


async def test_summary_uses_llm_when_available(tmp_path, monkeypatch):
    from canvas_mcp import lms, llm

    monkeypatch.setattr(lms, "ensure_model", lambda m: False)

    async def ok(title, body, **kw):
        return "这是摘要。"
    monkeypatch.setattr(llm, "summarize_announcement", ok)

    anns = [{"id": 1, "title": "T", "message_text": "long body"}]
    out = await daily.summarize_announcements(anns, use_llm=True, base=tmp_path)
    assert out[0]["summarized"] is True
    assert out[0]["summary"] == "这是摘要。"


# ---------------------------------------------------------------------------
# deliver: success advances state, failure does not
# ---------------------------------------------------------------------------

def test_deliver_updates_state_on_success(tmp_path):
    from canvas_mcp import notify
    msg = notify.Message("s", "t", "<p>t</p>")
    json_path = tmp_path / "reports" / "report_2026-11-04.json"
    json_path.parent.mkdir(parents=True)
    json_path.write_text("{}", encoding="utf-8")
    ok, err = daily.deliver({}, msg, json_path, NOW, base=tmp_path, sender=lambda *a, **k: None)
    assert ok is True
    assert daily.load_state(tmp_path)["last_report_path"] == str(json_path)


def test_deliver_keeps_state_on_failure(tmp_path):
    from canvas_mcp import notify

    def boom(*a, **k):
        raise notify.NotifyError("认证失败，请检查应用专用密码")
    msg = notify.Message("s", "t", "<p>t</p>")
    json_path = tmp_path / "reports" / "r.json"
    json_path.parent.mkdir(parents=True)
    json_path.write_text("{}", encoding="utf-8")
    ok, err = daily.deliver({"SMTP_APP_PASSWORD": "secretpw"}, msg, json_path, NOW,
                            base=tmp_path, sender=boom)
    assert ok is False
    assert daily.load_state(tmp_path) is None  # state never written
    # error log must not contain the password
    log = (tmp_path / "reports" / "errors.log").read_text(encoding="utf-8")
    assert "secretpw" not in log
