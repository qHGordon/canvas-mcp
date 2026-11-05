"""Notification layer: .ics content, SMTP send (mocked), retry/auth, config."""

import smtplib
from datetime import datetime, timezone

import pytest

from canvas_mcp import notify

NOW = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# .ics
# ---------------------------------------------------------------------------

def _deadline():
    return {
        "course_code": "ABC1001H F LEC0101", "course": "MDP", "course_id": 100001,
        "assignment_id": 200001, "name": "Assignment 2",
        "due_at": "2026-11-08T04:59:59Z",  # 2026-11-07 23:59 EST
        "url": "https://canvas.example.edu/courses/100001/assignments/200001",
    }


def test_ics_timezone_and_alarm():
    ics = notify.build_ics(_deadline(), NOW)
    assert "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics
    assert "TZID:America/Toronto" in ics
    # Due 04:59:59 UTC on the 8th == 23:59:59 EST on the 7th, local in DTSTART
    assert "DTSTART;TZID=America/Toronto:20261107T235959" in ics
    assert "TRIGGER:-P1D" in ics and "BEGIN:VALARM" in ics
    assert "SUMMARY:ABC1001H F LEC0101 · Assignment 2" in ics
    assert "URL:https://canvas.example.edu/courses/100001/assignments/200001" in ics


def test_ics_filename():
    assert notify.ics_filename(_deadline()) == "ABC1001H_A200001.ics"


# ---------------------------------------------------------------------------
# Email send (SMTP mocked)
# ---------------------------------------------------------------------------

class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls = []
        self.login_exc = FakeSMTP.login_exc_factory() if FakeSMTP.login_exc_factory else None
        FakeSMTP.instances.append(self)

    login_exc_factory = None
    sendmail_fail_times = 0  # class-level: shared across reconnect attempts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append("login")
        if self.login_exc:
            raise self.login_exc

    def sendmail(self, frm, to, body):
        self.calls.append("sendmail")
        if FakeSMTP.sendmail_fail_times > 0:
            FakeSMTP.sendmail_fail_times -= 1
            raise smtplib.SMTPServerDisconnected("boom")


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances = []
    FakeSMTP.login_exc_factory = None
    FakeSMTP.sendmail_fail_times = 0
    yield


def _config():
    return {"SMTP_HOST": "smtp.test", "SMTP_PORT": "587",
            "SMTP_USER": "u@test.com", "SMTP_APP_PASSWORD": "pw"}


def _msg():
    return notify.Message("subj", "text", "<p>html</p>",
                          attachments=[("a.ics", "BEGIN:VCALENDAR", "text/calendar")])


def test_email_send_success(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    notify.send("email", _msg(), _config())
    assert FakeSMTP.instances[0].calls == ["starttls", "login", "sendmail"]
    assert len(FakeSMTP.instances) == 1


def test_email_auth_failure_no_retry(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.login_exc_factory = lambda: smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(notify.NotifyError) as e:
        notify.send("email", _msg(), _config())
    assert "应用专用密码" in str(e.value)
    assert len(FakeSMTP.instances) == 1  # not retried
    assert "pw" not in str(e.value)  # password never echoed


def test_email_transient_failure_retries_then_raises(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sendmail_fail_times = 99  # always fail
    with pytest.raises(notify.NotifyError):
        notify.send("email", _msg(), _config(), retry_wait=0)
    assert len(FakeSMTP.instances) == 3  # initial + 2 retries


def test_email_recovers_on_second_attempt(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sendmail_fail_times = 1  # fail once, then succeed
    notify.send("email", _msg(), _config(), retry_wait=0)
    assert len(FakeSMTP.instances) == 2


def test_email_missing_credentials():
    with pytest.raises(notify.NotifyError):
        notify.send("email", _msg(), {"SMTP_USER": "", "SMTP_APP_PASSWORD": ""})


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def test_load_config_from_file(tmp_path, monkeypatch):
    env = tmp_path / "notify.env"
    env.write_text(
        "# comment\nSMTP_USER=me@gmail.com\nSMTP_APP_PASSWORD=\"abcd efgh\"\nTARGET_GRADE=A-\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SMTP_USER", raising=False)
    cfg = notify.load_notify_config(env)
    assert cfg["SMTP_USER"] == "me@gmail.com"
    assert cfg["SMTP_APP_PASSWORD"] == "abcd efgh"
    assert cfg["TARGET_GRADE"] == "A-"
    assert cfg["SMTP_HOST"] == "smtp.gmail.com"  # default


def test_mime_has_alternative_and_attachment():
    mime = notify._build_mime("a@x.com", "b@x.com", _msg())
    raw = mime.as_string()
    assert "multipart/alternative" in raw
    assert "text/calendar" in raw
    assert 'filename="a.ics"' in raw
