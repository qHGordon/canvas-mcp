"""Read-only HTTP client for the Canvas LMS REST API.

Only GET requests are exposed. Configuration comes exclusively from
environment variables:

- ``CANVAS_BASE_URL``  — e.g. ``https://q.utoronto.ca``
- ``CANVAS_TOKEN``     — a Canvas access token, or
- ``CANVAS_TOKEN_FILE`` — path to a file containing the token (stripped)
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import httpx

PER_PAGE = 100
RETRY_AFTER_DEFAULT = 2.0


class CanvasConfigError(Exception):
    """Raised at startup when required environment variables are missing."""


class CanvasAPIError(Exception):
    """A Canvas API error carrying a model-readable message."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def load_config() -> tuple[str, str]:
    """Read base URL and token from the environment. Raise clearly if missing."""
    base_url = os.environ.get("CANVAS_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise CanvasConfigError(
            "CANVAS_BASE_URL is not set. Set it to your Canvas instance URL, "
            "e.g. https://q.utoronto.ca"
        )
    if not base_url.startswith(("http://", "https://")):
        raise CanvasConfigError(
            f"CANVAS_BASE_URL must start with http(s)://, got: {base_url!r}"
        )

    token = os.environ.get("CANVAS_TOKEN", "").strip()
    token_file = os.environ.get("CANVAS_TOKEN_FILE", "").strip()
    if not token and token_file:
        path = Path(token_file)
        if not path.is_file():
            raise CanvasConfigError(
                f"CANVAS_TOKEN_FILE points to a file that does not exist: {token_file}"
            )
        token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise CanvasConfigError(
            "No Canvas token found. Set CANVAS_TOKEN to your access token, or "
            "CANVAS_TOKEN_FILE to a file containing it."
        )
    return base_url, token


_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


def parse_next_link(link_header: str | None) -> str | None:
    """Extract the rel="next" URL from a Canvas ``Link`` header, if any."""
    if not link_header:
        return None
    match = _NEXT_LINK_RE.search(link_header)
    return match.group(1) if match else None


class CanvasClient:
    """Thin async wrapper around httpx that only ever issues GET requests."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0):
        self._base_url = base_url
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=True,
        )

    @classmethod
    def from_env(cls) -> "CanvasClient":
        base_url, token = load_config()
        return cls(base_url, token)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a single Canvas API resource and return parsed JSON.

        Retries once on 429; raises CanvasAPIError with a readable message
        on 401/403/404 and other HTTP errors.
        """
        response = await self._request(path, params)
        return response.json()

    async def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> bytes:
        """GET raw bytes (e.g. a file download). Same retry/error handling."""
        response = await self._request(url, params)
        return response.content

    async def get_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[Any]:
        """GET a list endpoint, following Link rel="next" until exhausted."""
        merged = dict(params or {})
        merged.setdefault("per_page", PER_PAGE)
        results: list[Any] = []
        response = await self._request(path, merged)
        results.extend(response.json())
        next_url = parse_next_link(response.headers.get("Link"))
        while next_url:
            # next_url is absolute and already carries page/per_page params
            response = await self._request(next_url, None)
            results.extend(response.json())
            next_url = parse_next_link(response.headers.get("Link"))
        return results

    async def _request(
        self, url: str, params: dict[str, Any] | None
    ) -> httpx.Response:
        response = await self._http.get(url, params=params)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else RETRY_AFTER_DEFAULT
            except ValueError:
                delay = RETRY_AFTER_DEFAULT
            await asyncio.sleep(min(delay, 10.0))
            response = await self._http.get(url, params=params)
        if response.is_success:
            return response
        raise CanvasAPIError(response.status_code, self._describe_error(response, url))

    def _describe_error(self, response: httpx.Response, url: str) -> str:
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                errors = body.get("errors")
                if isinstance(errors, list) and errors:
                    detail = "; ".join(
                        str(e.get("message", e)) for e in errors if e
                    )
                elif isinstance(errors, dict):
                    detail = str(errors)
                elif "message" in body:
                    detail = str(body["message"])
        except Exception:
            detail = response.text[:200]

        status = response.status_code
        if status == 401:
            hint = (
                "Unauthorized (401): the Canvas token is invalid or expired. "
                "Check CANVAS_TOKEN / CANVAS_TOKEN_FILE."
            )
        elif status == 403:
            hint = (
                "Forbidden (403): the token does not have permission for this "
                "resource, or the course restricts access."
            )
        elif status == 404:
            hint = (
                "Not found (404): the resource does not exist or you are not "
                "enrolled in it. Check the id."
            )
        else:
            hint = f"Canvas API returned HTTP {status}."
        path = url.split("?")[0]
        return f"{hint} [GET {path}]" + (f" Detail: {detail}" if detail else "")
