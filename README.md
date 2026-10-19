# canvas-mcp

A **read-only** [MCP](https://modelcontextprotocol.io) server for Canvas LMS. It lets Claude Desktop / Claude Code query your courses, assignments, deadlines, announcements, modules, files, pages, discussions, grades and calendar through your own Canvas API token.

- **Read-only by design**: the HTTP layer only exposes `GET`. There is no code path that can create, modify or delete anything in Canvas.
- Handles Canvas pagination (`Link: rel="next"`) — list tools always return all pages.
- Slims Canvas's large JSON payloads down to the fields a model needs; pass `raw=true` to any tool to get the untouched Canvas response.
- All timestamps are ISO 8601, with an added `*_local` string in America/Toronto time.

## Security

> **Your Canvas access token inherits ALL permissions of your account** — including write access. The read-only guarantee comes from this server's design (it never issues anything but GET), not from the token itself. Treat the token like a password:
>
> - **Never commit it.** This repo's `.gitignore` excludes `Token.txt`, `.env` and `*.token`, but stay vigilant.
> - Prefer `CANVAS_TOKEN_FILE` over pasting the token into config files.
> - Revoke the token in Canvas (Account → Settings → Approved Integrations) if it ever leaks.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo>
cd canvas-mcp
uv sync
```

Generate a Canvas access token: in Canvas, go to **Account → Settings → Approved Integrations → New Access Token**.

### Configuration (environment variables only)

| Variable | Required | Meaning |
| --- | --- | --- |
| `CANVAS_BASE_URL` | yes | Your Canvas instance, e.g. `https://q.utoronto.ca` |
| `CANVAS_TOKEN` | one of the two | The access token itself |
| `CANVAS_TOKEN_FILE` | one of the two | Path to a file containing the token (whitespace is stripped) |

The server exits at startup with a clear error if the base URL or token is missing.

## Run

```sh
CANVAS_BASE_URL=https://q.utoronto.ca CANVAS_TOKEN_FILE=/path/to/Token.txt uv run canvas-mcp
```

Debug interactively with the MCP inspector:

```sh
CANVAS_BASE_URL=https://q.utoronto.ca CANVAS_TOKEN_FILE=/path/to/Token.txt \
  npx @modelcontextprotocol/inspector uv run canvas-mcp
```

## Claude Desktop

Add to `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "canvas": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/canvas-mcp", "canvas-mcp"],
      "env": {
        "CANVAS_BASE_URL": "https://q.utoronto.ca",
        "CANVAS_TOKEN_FILE": "/absolute/path/to/Token.txt"
      }
    }
  }
}
```

Or with the token inline (less recommended): replace `CANVAS_TOKEN_FILE` with `"CANVAS_TOKEN": "<your token>"`.

## Claude Code

```sh
claude mcp add canvas \
  --env CANVAS_BASE_URL=https://q.utoronto.ca \
  --env CANVAS_TOKEN_FILE=/absolute/path/to/Token.txt \
  -- uv run --directory /absolute/path/to/canvas-mcp canvas-mcp
```

## Tools

| Tool | Description |
| --- | --- |
| `whoami()` | Verify the token — returns your Canvas profile |
| `list_courses(include_past=False)` | Active courses (id, name, course_code, term) |
| `list_assignments(course_id, bucket="upcoming")` | Assignments; bucket: upcoming / past / undated / unsubmitted / all |
| `get_upcoming_deadlines(days=14)` | Deadlines across all active courses, sorted by due date |
| `list_announcements(course_id, days=30)` | Announcements, HTML converted to plain text |
| `list_modules(course_id)` | Modules and their items with completion state |
| `list_files(course_id, folder_id=None)` | Course files with size, content type, download url |
| `get_page(course_id, page_url)` | Wiki page body as plain text |
| `list_discussions(course_id)` / `get_discussion(course_id, topic_id)` | Discussion topics and replies |
| `get_grades(course_id)` | Your grade summary for a course |
| `get_calendar_events(days=14)` | Calendar + assignment events for the next N days |

Every tool accepts `raw=true` to return Canvas's original JSON. Errors (401/403/404…) come back as readable `{"error": ...}` objects instead of exceptions; 429 is retried once.

## Tests

```sh
uv run pytest
```

Tests mock the Canvas API with [respx](https://lundberg.github.io/respx/) — no network or token needed.

## License

MIT
