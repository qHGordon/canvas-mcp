"""Manage a local LM Studio instance through its `lms` CLI.

Only used by the daily report's announcement-summary step, and only best-effort:
every call can fail, and the caller falls back to a template summary. We start
the server and load the report model if needed, but on cleanup we only unload a
model WE loaded — never one the user already had running.

The `lms` path comes from ``LMS_PATH`` (scheduled tasks often have an incomplete
PATH), defaulting to ``%USERPROFILE%\\.lmstudio\\bin\\lms.exe``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class LmsError(Exception):
    pass


def lms_path() -> str:
    env = os.environ.get("LMS_PATH", "").strip()
    if env:
        return env
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return str(Path(home) / ".lmstudio" / "bin" / "lms.exe")


def _run(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [lms_path(), *args], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise LmsError(f"找不到 lms 可执行文件（LMS_PATH={lms_path()!r}）：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise LmsError(f"lms {' '.join(args)} 超时") from exc


def server_running() -> bool:
    res = _run(["server", "status"], timeout=20)
    out = (res.stdout + res.stderr).lower()
    return "running" in out and "not running" not in out


def ensure_server() -> None:
    if server_running():
        return
    _run(["server", "start"], timeout=40)
    if not server_running():
        raise LmsError("lms server 未能启动。")


def loaded_models() -> list[str]:
    res = _run(["ps"], timeout=20)
    return res.stdout


def is_loaded(model: str) -> bool:
    return model.split("/")[-1].lower() in loaded_models().lower()


def ensure_model(model: str) -> bool:
    """Make sure `model` is loaded. Returns True iff we loaded it (so the caller
    knows to unload it afterwards). Raises LmsError if it can't be made ready."""
    ensure_server()
    if is_loaded(model):
        return False
    res = _run(["load", model, "--yes"], timeout=180)
    if res.returncode != 0 or not is_loaded(model):
        raise LmsError(f"加载模型 {model!r} 失败：{(res.stderr or res.stdout)[:200]}")
    return True


def unload(model: str) -> None:
    try:
        _run(["unload", model], timeout=30)
    except LmsError:
        pass
