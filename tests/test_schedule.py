"""Class-schedule expansion into dated occurrences (pure)."""

from datetime import datetime, timezone

from canvas_mcp import schedule


# 2026-11-04 is a Wednesday (weekday()==2) in Toronto around midday UTC.
NOW = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)


def test_wed_session_includes_today():
    sessions = [{"type": "lecture", "weekday": 2, "start": "19:00", "end": "21:00",
                 "location": "Teams"}]
    occ = schedule.upcoming_sessions(sessions, NOW, days=7)
    # Window is today..+6 (11-04..11-10); only one Wednesday in it: today
    assert [o["date"] for o in occ] == ["2026-11-04"]
    assert occ[0]["weekday"] == "周三"
    assert occ[0]["type_zh"] == "上课"
    assert occ[0]["start"] == "19:00" and occ[0]["location"] == "Teams"


def test_monday_session_next_occurrence():
    sessions = [{"type": "lecture", "weekday": 0, "start": "13:00", "end": "16:00"}]
    occ = schedule.upcoming_sessions(sessions, NOW, days=7)
    # Next Monday after Wed 11-04 is 11-09
    assert [o["date"] for o in occ] == ["2026-11-09"]
    assert occ[0]["weekday"] == "周一"


def test_multiple_sessions_sorted():
    sessions = [
        {"type": "tutorial", "weekday": 3, "start": "11:00", "end": "12:00"},  # Thu 11-05
        {"type": "lecture", "weekday": 2, "start": "19:00", "end": "21:00"},   # Wed 11-04
    ]
    occ = schedule.upcoming_sessions(sessions, NOW, days=7)
    assert [o["date"] for o in occ] == ["2026-11-04", "2026-11-05"]
    assert occ[1]["type_zh"] == "辅导"


def test_no_session_in_window():
    # Sunday session (weekday 6): 11-08 is within today..+6, so it SHOULD appear;
    # use a weekday that is excluded only if window missed — pick Tuesday (1):
    # next Tuesday after 11-04 is 11-10, still in window -> appears. So test an
    # empty sessions list instead for the true "nothing" case.
    assert schedule.upcoming_sessions([], NOW, days=7) == []


def test_bad_weekday_skipped():
    sessions = [{"type": "lecture", "weekday": None, "start": "9:00"}]
    assert schedule.upcoming_sessions(sessions, NOW, days=7) == []
