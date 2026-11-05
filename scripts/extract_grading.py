"""提取每门 active 课程的评分构成，写成待确认草稿（grading.draft.json）。

用途:   按优先级找权重来源：
        a. apply_assignment_group_weights=true → 用 assignment_groups 的 group_weight
           和 rules（纯 Canvas，不用 LLM，confidence=high）
        b. syllabus_body 文本 → 本地 LLM 结构化提取
        c. 模块里 syllabus 的 File（PDF）→ pymupdf 抽文本 → 本地 LLM 提取
        LLM 只做抽取，所有权重都要能引用原文；分数计算不在这里。
        永远不写 grading.json（只写 draft），不覆盖已确认配置。
输入:   环境变量 CANVAS_BASE_URL + CANVAS_TOKEN[_FILE]；本地 LLM 用
        LLM_BASE_URL（默认 http://localhost:1234/v1）、LLM_MODEL。
        可选参数: --course-id N（只处理一门）--out DIR（默认仓库根的 data/）。
输出:   data/courses/<course_code>/grading.draft.json
        {items[{name,type,weight,rules,source_quote}], mapping, source, confidence, warnings}
幂等:   重复运行覆盖同名 draft；严格对 Canvas 只读。
最后验证通过: 2026-11-04（assignment_groups 加权路径，真实数据）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from canvas_mcp import grading as G
from canvas_mcp.client import CanvasClient, CanvasConfigError
from canvas_mcp.crawl import fetch_grading_context
from canvas_mcp.formatters import LOCAL_TZ, html_to_text

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--course-id", type=int, default=None)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return parser.parse_args()


def _pdf_text(data: bytes) -> str:
    import pymupdf  # imported lazily so the assignment-groups path never needs it

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)


async def extract_one(client: CanvasClient, course: dict, base: Path) -> dict:
    code = course.get("course_code")
    ctx = await fetch_grading_context(client, course)
    meta, groups = ctx["meta"], ctx["groups"]
    assignments = ctx["assignments"]
    warnings: list[str] = [f"{e['section']}: HTTP {e['status']}" for e in ctx["errors"]]

    # a. weighted assignment groups
    if meta.get("apply_assignment_group_weights") and groups:
        draft = G.draft_from_assignment_groups(course, groups)
        draft["warnings"] = warnings + draft.get("warnings", [])
        return draft

    # b/c. syllabus text or PDF → local LLM
    from canvas_mcp import llm  # deferred: only the fallback path needs httpx→LLM

    source_text, source = "", None
    syllabus_body = (meta.get("syllabus_body") or "").strip()
    if syllabus_body:
        source_text, source = html_to_text(syllabus_body), "syllabus_body"
    elif ctx["syllabus_file"] and ctx["syllabus_file"].get("url"):
        try:
            data = await client.get_bytes(ctx["syllabus_file"]["url"])
            source_text, source = _pdf_text(data), "syllabus_pdf"
        except Exception as exc:  # noqa: BLE001 - degrade with a warning
            warnings.append(f"syllabus PDF 读取失败：{exc}")

    if not source_text:
        return {
            "course_id": course.get("id"), "course_code": code,
            "course_name": course.get("name"), "graded": False,
            "items": [], "mapping": {}, "manual_scores": {},
            "source": None, "confidence": "low",
            "warnings": warnings + ["未找到评分构成来源（assignment_groups 未加权，"
                                    "syllabus_body 为空，模块里也没有 syllabus 文件）。"],
        }

    try:
        result = await llm.extract_grading_draft(course, source_text, assignments)
        draft = G.draft_from_llm(course, result, source, assignments)
        draft["warnings"] = warnings + draft.get("warnings", [])
        return draft
    except llm.LLMError as exc:
        return {
            "course_id": course.get("id"), "course_code": code,
            "course_name": course.get("name"), "graded": None,
            "items": [], "mapping": {}, "manual_scores": {},
            "source": source, "confidence": "low",
            "warnings": warnings + [
                f"本地 LLM 不可用，{source} 文本已取到但未能结构化：{exc}。"
                "请启动 LM Studio 后重跑，或手动编辑 draft。"],
            "source_text_preview": source_text[:600],
        }


async def run(args: argparse.Namespace) -> int:
    try:
        client = CanvasClient.from_env()
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    try:
        courses = await client.get_paginated(
            "/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]}
        )
        courses = [c for c in courses if isinstance(c, dict) and c.get("name")]
        if args.course_id is not None:
            courses = [c for c in courses if c.get("id") == args.course_id]
        for course in courses:
            code = course.get("course_code")
            if G.load_grading(code, args.out) is not None:
                print(f"[{code}] 已有确认的 grading.json，draft 仅供参考（不会覆盖确认配置）。")
            draft = await extract_one(client, course, args.out)
            path = G.save_draft(code, draft, args.out)
            print(f"[{code}] source={draft.get('source')} "
                  f"confidence={draft.get('confidence')} "
                  f"items={len(draft.get('items', []))} → {path}")
            for w in draft.get("warnings", []):
                print(f"    ! {w}")
    finally:
        await client.aclose()
    print("\n下一步：检查 draft 后运行 confirm_grading.py --course-id <id> 确认。")
    return 0


if __name__ == "__main__":
    print(f"generated at {datetime.now(LOCAL_TZ).isoformat(timespec='seconds')}")
    sys.exit(asyncio.run(run(parse_args())))
