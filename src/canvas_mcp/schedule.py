"""Per-course recurring class schedule (lectures / tutorials / tests).

Canvas rarely stores weekly meeting times in a structured field — they usually
live in free text on the course front page or an "intro" page. So we keep a
confirmed, deterministic ``schedule.json`` per course instead of re-parsing prose
on every run. This module expands those weekly sessions into concrete dated
occurrences for the daily report's calendar. Pure functions; times in LOCAL_TZ (CANVAS_TZ).

schedule.json shape::

    {
      "course_id": 100002,
      "course_code": "XYZ2002H F LEC0101",
      "sessions": [
        {"type": "lecture", "weekday": 2, "start": "19:00", "end": "21:00",
         "location": "Teams (online)", "note": "synchronous"}
      ]
    }

``weekday``: Monday=0 … Sunday=6 (matches datetime.weekday()).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .formatters import WEEKDAY_ZH, LOCAL_TZ

SESSION_TYPE_ZH = {
    "lecture": "上课", "tutorial": "辅导", "test": "测验",
    "office_hours": "答疑", "lab": "实验",
}


def load_schedule(course_code: str, base: Path | str | None = None) -> dict | None:
    from . import grading
    return grading._read_json(grading.course_dir(course_code, base) / "schedule.json")


def upcoming_sessions(
    sessions: list[dict[str, Any]], now: datetime, days: int = 7
) -> list[dict[str, Any]]:
    """Expand weekly sessions into concrete occurrences on the dates in the
    window [today, today+days-1] (America/Toronto), sorted by date then start."""
    today = now.astimezone(LOCAL_TZ).date()
    out: list[dict[str, Any]] = []
    for i in range(days):
        d = today + timedelta(days=i)
        for s in sessions or []:
            try:
                wd = int(s.get("weekday"))
            except (TypeError, ValueError):
                continue
            if wd != d.weekday():
                continue
            stype = s.get("type", "lecture")
            out.append({
                "type": stype,
                "type_zh": SESSION_TYPE_ZH.get(stype, stype),
                "date": d.isoformat(),
                "weekday": WEEKDAY_ZH[d.weekday()],
                "start": s.get("start"),
                "end": s.get("end"),
                "location": s.get("location"),
                "note": s.get("note"),
            })
    out.sort(key=lambda x: (x["date"], x.get("start") or ""))
    return out
