"""Notification delivery (email only for now) + config, .ics, error logging.

Pluggable by design: a :class:`Channel` base plus a registry, so ntfy / 微信 /
Telegram can be added later without touching callers. Nothing here talks to
Canvas; it only sends what the report layer produced. Secrets (SMTP password,
Canvas token) are NEVER written to logs.
"""

from __future__ import annotations

import os
import smtplib
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
from typing import Any

from .formatters import LOCAL_TZ

# Default: notify.env sitting next to (outside) the repo directory, so secrets
# never live inside the working tree. Override with NOTIFY_ENV.
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[3] / "notify.env"

CONFIG_DEFAULTS = {
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "",
    "SMTP_APP_PASSWORD": "",
    "MAIL_FROM": "",
    "MAIL_TO": "",
    "LLM_BASE_URL": "http://localhost:1234/v1",
    "LLM_MODEL_REPORT": "qwen/qwen3.5-9b",
    "LMS_PATH": "",
    "REPORT_SKIP_EMPTY": "true",
    "TARGET_GRADE": "A",
}


class NotifyError(Exception):
    """A delivery failure carrying a user-facing (secret-free) message."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_notify_config(path: str | Path | None = None) -> dict[str, str]:
    """Read notify.env (KEY=VALUE, # comments) with env-var / default fallback.

    Resolution order per key: notify.env value → os.environ → built-in default.
    The path comes from `path`, else ``NOTIFY_ENV``, else the repo-external
    default."""
    env_path = Path(path or os.environ.get("NOTIFY_ENV") or DEFAULT_ENV_PATH)
    file_values: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            file_values[key.strip()] = val
    config: dict[str, str] = {}
    for key, default in CONFIG_DEFAULTS.items():
        config[key] = file_values.get(key) or os.environ.get(key) or default
    # Keep any extra keys present in the file.
    for key, val in file_values.items():
        config.setdefault(key, val)
    return config


# ---------------------------------------------------------------------------
# Message + channels
# ---------------------------------------------------------------------------

@dataclass
class Message:
    subject: str
    text_body: str
    html_body: str
    # attachments: list of (filename, content_str, content_type e.g. "text/calendar")
    attachments: list[tuple[str, str, str]] = field(default_factory=list)


CHANNELS: dict[str, type["Channel"]] = {}


def register_channel(name: str):
    def deco(cls: type["Channel"]):
        cls.name = name
        CHANNELS[name] = cls
        return cls
    return deco


class Channel(ABC):
    name: str = ""

    @abstractmethod
    def send(self, msg: Message, config: dict[str, str]) -> None:  # pragma: no cover
        ...


def _build_mime(mail_from: str, mail_to: str, msg: Message) -> MIMEMultipart:
    outer = MIMEMultipart("mixed")
    outer["Subject"] = msg.subject
    outer["From"] = mail_from
    outer["To"] = mail_to
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(msg.text_body, "plain", "utf-8"))
    alt.attach(MIMEText(msg.html_body, "html", "utf-8"))
    outer.attach(alt)
    for filename, content, content_type in msg.attachments:
        maintype, _, subtype = content_type.partition("/")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(content.encode("utf-8"))
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        outer.attach(part)
    return outer


@register_channel("email")
class EmailChannel(Channel):
    def send(
        self, msg: Message, config: dict[str, str], *, retry_wait: float = 30.0
    ) -> None:
        host = config.get("SMTP_HOST") or "smtp.gmail.com"
        port = int(config.get("SMTP_PORT") or 587)
        user = config.get("SMTP_USER") or ""
        password = config.get("SMTP_APP_PASSWORD") or ""
        if not user or not password:
            raise NotifyError("SMTP_USER / SMTP_APP_PASSWORD 未配置（见 notify.env）。")
        mail_from = config.get("MAIL_FROM") or user
        mail_to = config.get("MAIL_TO") or user
        mime = _build_mime(mail_from, mail_to, msg)

        attempts = 0
        while True:
            try:
                with smtplib.SMTP(host, port, timeout=30) as server:
                    server.starttls()
                    server.login(user, password)
                    server.sendmail(mail_from, [mail_to], mime.as_string())
                return
            except smtplib.SMTPAuthenticationError as exc:
                # Never retry auth failures; never echo the password.
                raise NotifyError(
                    f"SMTP 认证失败（{exc.smtp_code}）。请检查应用专用密码 "
                    "SMTP_APP_PASSWORD（Gmail 需要 App Password，不是账号密码）。"
                ) from None
            except (smtplib.SMTPException, OSError) as exc:
                attempts += 1
                if attempts > 2:
                    raise NotifyError(
                        f"发送失败（已重试 {attempts - 1} 次）：{type(exc).__name__}: {exc}"
                    ) from None
                time.sleep(retry_wait)


def send(channel: str, msg: Message, config: dict[str, str], **kwargs) -> None:
    cls = CHANNELS.get(channel)
    if cls is None:
        raise NotifyError(f"未知通知渠道 {channel!r}。可用：{', '.join(CHANNELS)}")
    cls().send(msg, config, **kwargs)


# ---------------------------------------------------------------------------
# .ics (one VEVENT per unsubmitted deadline)
# ---------------------------------------------------------------------------

_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/Toronto
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE"""


def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(
        ",", "\\,").replace("\n", "\\n")


def build_ics(deadline: dict[str, Any], now: datetime) -> str:
    """A single-event VCALENDAR for one unsubmitted assignment deadline.

    DTSTART uses America/Toronto; a VALARM fires 24h before."""
    due_iso = deadline.get("due_at")
    due = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    local = due.astimezone(LOCAL_TZ)
    dtstart = local.strftime("%Y%m%dT%H%M%S")
    dtstamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = f"canvas-{deadline.get('assignment_id')}@canvas-mcp"
    summary = _ics_escape(f"{deadline.get('course_code') or deadline.get('course')} · {deadline.get('name')}")
    url = deadline.get("url") or ""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//canvas-mcp//daily-report//EN",
        "CALSCALE:GREGORIAN",
        _VTIMEZONE,
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID=America/Toronto:{dtstart}",
        f"SUMMARY:{summary}",
    ]
    if url:
        lines.append(f"URL:{_ics_escape(url)}")
    lines += [
        "BEGIN:VALARM",
        "TRIGGER:-P1D",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{summary}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def ics_filename(deadline: dict[str, Any]) -> str:
    code = (deadline.get("course_code") or deadline.get("course") or "course")
    code = "".join(c for c in str(code).split()[0] if c.isalnum()) or "course"
    return f"{code}_A{deadline.get('assignment_id')}.ics"


# ---------------------------------------------------------------------------
# Error log + Windows toast (best effort, never raise)
# ---------------------------------------------------------------------------

def log_error(base_dir: Path, message: str) -> None:
    """Append a timestamped line to data/reports/errors.log. Caller must pass a
    message free of secrets (we never log config values)."""
    path = Path(base_dir) / "reports" / "errors.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] {message}\n")


def windows_toast(title: str, message: str) -> None:  # pragma: no cover - OS dep
    """Pop a Windows notification via BurntToast, falling back to msg.exe."""
    try:
        ps = (
            "if (Get-Module -ListAvailable -Name BurntToast) {"
            f"  Import-Module BurntToast; New-BurntToastNotification -Text '{title}','{message}'"
            "} else { exit 2 }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=20,
        )
        if result.returncode == 0:
            return
    except Exception:
        pass
    try:
        subprocess.run(["msg", "*", f"{title}: {message}"], capture_output=True, timeout=20)
    except Exception:
        pass
