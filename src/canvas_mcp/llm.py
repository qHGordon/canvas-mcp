"""Local-LLM glue (LM Studio, OpenAI-compatible) used for two narrow jobs:

  1. extract_grading — turn syllabus prose / PDF text into a structured draft
  2. amend_grading   — parse a natural-language instruction into a *patch*

The LLM NEVER computes scores and NEVER writes files. It only drafts structure
and proposes patches; code validates and applies everything. Endpoint and model
come from ``LLM_BASE_URL`` (default http://localhost:1234/v1) and ``LLM_MODEL``.
Output is constrained with a JSON schema.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:1234/v1"


class LLMError(Exception):
    """Any failure talking to the local LLM."""


class LLMUnavailable(LLMError):
    """The local LLM endpoint could not be reached (not running)."""


def llm_config() -> tuple[str, str]:
    base = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    model = os.environ.get("LLM_MODEL", "").strip() or "local-model"
    return base, model


ITEM_TYPES = ["assignment", "exam", "project", "quiz", "participation", "other"]

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ITEM_TYPES},
                    "weight": {"type": "number"},
                    "rules": {
                        "type": "object",
                        "properties": {
                            "drop_lowest": {"type": "integer"},
                            "drop_highest": {"type": "integer"},
                        },
                    },
                    "source_quote": {"type": "string"},
                },
                "required": ["name", "type", "weight"],
            },
        },
        "mapping_guesses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_name": {"type": "string"},
                    "assignment_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["item_name", "assignment_ids"],
            },
        },
        "grade_scale": {"type": "object"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["items", "confidence"],
}

PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": [
                            "add_item", "update_item", "remove_item",
                            "set_manual_score", "clear_manual_score",
                            "map_assignment", "unmap_assignment", "set_grade_scale",
                        ],
                    },
                    "name": {"type": ["string", "null"]},
                    "type": {"type": ["string", "null"]},
                    "weight": {"type": ["number", "null"]},
                    "rules": {"type": ["object", "null"]},
                    "item": {"type": ["string", "null"]},
                    "assignment_id": {"type": ["integer", "null"]},
                    "key": {"type": ["string", "integer", "null"]},
                    "score": {"type": ["number", "null"]},
                    "out_of": {"type": ["number", "null"]},
                    "scale": {"type": ["object", "null"]},
                    "source_text": {"type": ["string", "null"]},
                },
                "required": ["op"],
            },
        },
        "clarification": {"type": ["string", "null"]},
    },
}


async def chat_json(
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    schema_name: str = "output",
    *,
    temperature: float = 0.0,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """POST a chat completion constrained to `schema`; return the parsed JSON."""
    base, model = llm_config()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": schema, "strict": False},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base}/chat/completions", json=payload)
    except httpx.ConnectError as exc:
        raise LLMUnavailable(
            f"无法连接本地 LLM ({base})。请确认 LM Studio 已启动并加载模型，"
            f"或设置 LLM_BASE_URL / LLM_MODEL。原始错误：{exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"调用本地 LLM 失败：{exc}") from exc
    if resp.status_code != 200:
        raise LLMError(f"本地 LLM 返回 HTTP {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMError(f"本地 LLM 输出不是合法 JSON：{content[:300]}") from exc


async def chat_text(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> str:
    """A plain-text chat completion (no JSON schema). Raises LLMUnavailable if
    the endpoint is unreachable, LLMError on other failures."""
    base, default_model = llm_config()
    payload = {
        "model": model or default_model,
        "messages": messages,
        "temperature": temperature,
        # Disable qwen-style thinking to keep per-announcement latency low.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base}/chat/completions", json=payload)
    except httpx.ConnectError as exc:
        raise LLMUnavailable(f"无法连接本地 LLM ({base})：{exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"调用本地 LLM 失败：{exc}") from exc
    if resp.status_code != 200:
        raise LLMError(f"本地 LLM 返回 HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"]


def report_model() -> str:
    """The model id used for daily-report summaries (LLM_MODEL_REPORT)."""
    import os
    return os.environ.get("LLM_MODEL_REPORT", "").strip() or "qwen/qwen3.5-9b"


async def summarize_announcement(
    title: str, body: str, *, model: str | None = None, timeout: float = 60.0
) -> str:
    """1–2 sentence Chinese summary of one announcement, strictly from its text."""
    system = (
        "你是公告摘要助手。只根据给定的公告原文，用 1–2 句中文概括要点，"
        "不得添加原文没有的信息，不要寒暄。/no_think"
    )
    user = f"标题：{title}\n正文：\n{(body or '')[:4000]}"
    text = await chat_text(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=model or report_model(), timeout=timeout,
    )
    # Strip any leftover <think>…</think> block some models still emit.
    import re
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return text


def _assignments_blurb(canvas_assignments: list[dict[str, Any]]) -> str:
    if not canvas_assignments:
        return "（本课程没有可见的 Canvas 作业）"
    return "\n".join(
        f"  id={a.get('id')}  {a.get('name')}"
        for a in canvas_assignments if a.get("id") is not None
    )


async def extract_grading_draft(
    course: dict[str, Any],
    source_text: str,
    canvas_assignments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ask the LLM to structure a syllabus's grading scheme. Pure extraction —
    it must quote the syllabus, never invent weights."""
    system = (
        "你是一个只做信息抽取的助手。从给定的 syllabus 文本里提取成绩构成，"
        "只能引用原文，不能编造权重或规则。每个项目给出 name、type、weight(百分数的数字)、"
        "可选 rules(drop_lowest/drop_highest)、以及 source_quote(原文片段)。"
        "\"best N of M\" 换算成 drop_lowest = M - N。若能把项目对应到下面列出的 Canvas 作业，"
        "在 mapping_guesses 里按 id 关联；对不上就留空。权重之和应接近 100，"
        "若不确定在 warnings 里说明，并给出 confidence。只输出 JSON。"
    )
    user = (
        f"课程：{course.get('name')}（{course.get('course_code')}）\n\n"
        f"Canvas 作业列表：\n{_assignments_blurb(canvas_assignments)}\n\n"
        f"syllabus 文本：\n{source_text[:12000]}"
    )
    return await chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        EXTRACTION_SCHEMA, "grading_draft",
    )


async def parse_amendment(
    instruction: str,
    grading: dict[str, Any],
    canvas_assignments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Turn a natural-language change into a patch. The LLM only emits a patch;
    ambiguity must come back as `clarification`, never a guess."""
    system = (
        "你把用户关于课程成绩构成的自然语言修改，解析成一个补丁(patch)。"
        "补丁只能包含这些操作：add_item / update_item / remove_item / set_manual_score / "
        "clear_manual_score / map_assignment / unmap_assignment / set_grade_scale。"
        "每个操作都要带 source_text（用户原话里对应的片段）。"
        "用下面给出的当前配置和 Canvas 作业列表，把\"期中\"\"A3\"等说法对应到已有 item 和作业 id。"
        "不要自己计算分数，不要平衡权重。只要有歧义、对不上、或信息不足，就把 operations 留空，"
        "在 clarification 里用中文提出需要澄清的问题。只输出 JSON。"
    )
    user = (
        f"当前 grading.json：\n{json.dumps(grading, ensure_ascii=False, indent=2)}\n\n"
        f"Canvas 作业列表：\n{_assignments_blurb(canvas_assignments)}\n\n"
        f"用户指令：{instruction}"
    )
    return await chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        PATCH_SCHEMA, "grading_patch",
    )
