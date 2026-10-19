"""Client tests: config loading, pagination, error handling, 429 retry."""

import httpx
import pytest
import respx

from canvas_mcp.client import (
    CanvasAPIError,
    CanvasClient,
    CanvasConfigError,
    load_config,
    parse_next_link,
)

BASE = "https://canvas.example.edu"


def make_client() -> CanvasClient:
    return CanvasClient(BASE, "test-token")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_load_config_requires_base_url(monkeypatch):
    monkeypatch.delenv("CANVAS_BASE_URL", raising=False)
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    monkeypatch.delenv("CANVAS_TOKEN_FILE", raising=False)
    with pytest.raises(CanvasConfigError, match="CANVAS_BASE_URL"):
        load_config()


def test_load_config_requires_token(monkeypatch):
    monkeypatch.setenv("CANVAS_BASE_URL", BASE)
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    monkeypatch.delenv("CANVAS_TOKEN_FILE", raising=False)
    with pytest.raises(CanvasConfigError, match="token"):
        load_config()


def test_load_config_reads_token_file_and_strips(monkeypatch, tmp_path):
    token_file = tmp_path / "Token.txt"
    token_file.write_text("  secret-token\n", encoding="utf-8")
    monkeypatch.setenv("CANVAS_BASE_URL", BASE + "/")
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    monkeypatch.setenv("CANVAS_TOKEN_FILE", str(token_file))
    base_url, token = load_config()
    assert base_url == BASE  # trailing slash stripped
    assert token == "secret-token"


def test_load_config_missing_token_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CANVAS_BASE_URL", BASE)
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    monkeypatch.setenv("CANVAS_TOKEN_FILE", str(tmp_path / "nope.txt"))
    with pytest.raises(CanvasConfigError, match="does not exist"):
        load_config()


# ---------------------------------------------------------------------------
# Link header parsing
# ---------------------------------------------------------------------------

def test_parse_next_link():
    header = (
        f'<{BASE}/api/v1/courses?page=2&per_page=100>; rel="next",'
        f'<{BASE}/api/v1/courses?page=1&per_page=100>; rel="first"'
    )
    assert parse_next_link(header) == f"{BASE}/api/v1/courses?page=2&per_page=100"
    assert parse_next_link('<x>; rel="first"') is None
    assert parse_next_link(None) is None


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

@respx.mock
async def test_get_paginated_follows_all_pages():
    route = respx.get(f"{BASE}/api/v1/courses")
    route.side_effect = [
        httpx.Response(
            200,
            json=[{"id": 1}, {"id": 2}],
            headers={"Link": f'<{BASE}/api/v1/courses?page=2&per_page=100>; rel="next"'},
        ),
        httpx.Response(
            200,
            json=[{"id": 3}],
            headers={"Link": f'<{BASE}/api/v1/courses?page=1>; rel="prev"'},
        ),
    ]
    client = make_client()
    try:
        results = await client.get_paginated("/api/v1/courses")
    finally:
        await client.aclose()
    assert [r["id"] for r in results] == [1, 2, 3]
    assert route.call_count == 2
    # First request must ask for 100 per page; second follows the next link
    assert "per_page=100" in str(route.calls[0].request.url)
    assert "page=2" in str(route.calls[1].request.url)


@respx.mock
async def test_get_paginated_single_page():
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    client = make_client()
    try:
        results = await client.get_paginated("/api/v1/courses")
    finally:
        await client.aclose()
    assert results == [{"id": 1}]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.parametrize(
    "status,expected",
    [(401, "Unauthorized"), (403, "Forbidden"), (404, "Not found")],
)
async def test_http_errors_are_readable(status, expected):
    respx.get(f"{BASE}/api/v1/users/self").mock(
        return_value=httpx.Response(
            status, json={"errors": [{"message": "Invalid access token."}]}
        )
    )
    client = make_client()
    try:
        with pytest.raises(CanvasAPIError) as excinfo:
            await client.get("/api/v1/users/self")
    finally:
        await client.aclose()
    assert expected in str(excinfo.value)
    assert "Invalid access token." in str(excinfo.value)
    assert excinfo.value.status_code == status


@respx.mock
async def test_429_retried_once_then_succeeds():
    route = respx.get(f"{BASE}/api/v1/courses")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=[{"id": 7}]),
    ]
    client = make_client()
    try:
        result = await client.get("/api/v1/courses")
    finally:
        await client.aclose()
    assert result == [{"id": 7}]
    assert route.call_count == 2


@respx.mock
async def test_429_twice_raises():
    respx.get(f"{BASE}/api/v1/courses").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    client = make_client()
    try:
        with pytest.raises(CanvasAPIError) as excinfo:
            await client.get("/api/v1/courses")
    finally:
        await client.aclose()
    assert excinfo.value.status_code == 429


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------

def test_client_exposes_no_write_methods():
    client = CanvasClient(BASE, "t")
    public = [m for m in dir(client) if not m.startswith("_")]
    for verb in ("post", "put", "delete", "patch"):
        assert not any(verb in m.lower() for m in public), f"write method {verb} exposed"


def test_auth_header_set():
    client = CanvasClient(BASE, "tok-123")
    assert client._http.headers["Authorization"] == "Bearer tok-123"
