"""把评分草稿确认为 grading.json（首次确认入口）。

用途:   打印 grading.draft.json 的表格（项目、类型、权重、规则、对应 Canvas 作业、
        原文引用），确认后另存为 grading.json，写入 confirmed_at，并记入 history 作为基线。
        - 不计成绩的课（如不计分的培训项目）用 --ungraded 标记 graded:false，后续跳过。
        - grading.json 支持 manual_scores（不在 Canvas 上的考试/纸质作业），确认后可用
          amend_grading.py 追加，或直接编辑文件。
输入:   可选参数: --course-id N（必填其一）或 --course-code CODE；--yes 跳过交互确认；
        --ungraded 标记为不计成绩；--out DIR（默认仓库根的 data/）。
        需要 CANVAS_* 环境变量来把 course-id 解析成 course_code。
输出:   data/courses/<course_code>/grading.json（含 confirmed_at）
        data/courses/<course_code>/grading.history.jsonl（基线快照）
幂等:   重复确认会覆盖 grading.json 并重置 history 基线（会提示）。
最后验证通过: 2026-11-04（逻辑；真实确认由用户执行）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from canvas_mcp import grading as G
from canvas_mcp.client import CanvasClient, CanvasConfigError
from canvas_mcp.formatters import LOCAL_TZ

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--course-id", type=int, default=None)
    p.add_argument("--course-code", default=None)
    p.add_argument("--yes", action="store_true", help="跳过交互确认")
    p.add_argument("--ungraded", action="store_true", help="标记为不计成绩 graded:false")
    p.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return p.parse_args()


async def resolve_code(course_id: int, out: Path) -> str | None:
    try:
        client = CanvasClient.from_env()
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return None
    try:
        courses = await client.get_paginated(
            "/api/v1/courses", {"enrollment_state": "active", "include[]": ["term"]}
        )
    finally:
        await client.aclose()
    for c in courses:
        if isinstance(c, dict) and c.get("id") == course_id:
            return c.get("course_code")
    return None


def print_draft(draft: dict) -> None:
    mapping = draft.get("mapping") or {}
    print(f"\n课程：{draft.get('course_name')}（{draft.get('course_code')}）")
    print(f"来源：{draft.get('source')}　置信度：{draft.get('confidence')}")
    print("-" * 78)
    print(f"{'项目':<22}{'类型':<12}{'权重':>6}  {'规则':<14}{'Canvas 作业'}")
    print("-" * 78)
    for it in draft.get("items", []):
        rules = it.get("rules") or {}
        rtag = []
        if rules.get("drop_lowest"):
            rtag.append(f"drop低{rules['drop_lowest']}")
        if rules.get("drop_highest"):
            rtag.append(f"drop高{rules['drop_highest']}")
        ids = mapping.get(it.get("name")) or []
        print(f"{(it.get('name') or '')[:21]:<22}{(it.get('type') or ''):<12}"
              f"{it.get('weight'):>5}%  {','.join(rtag) or '—':<14}{ids}")
        if it.get("source_quote"):
            print(f"    “{str(it['source_quote'])[:70]}”")
    total = sum(float(i.get("weight") or 0) for i in draft.get("items", []))
    print("-" * 78)
    print(f"权重之和：{round(total, 2)}%")
    for w in draft.get("warnings", []):
        print(f"! {w}")


async def run(args: argparse.Namespace) -> int:
    code = args.course_code
    if code is None:
        if args.course_id is None:
            print("需要 --course-id 或 --course-code", file=sys.stderr)
            return 2
        code = await resolve_code(args.course_id, args.out)
        if not code:
            print(f"找不到 course-id {args.course_id} 对应的课程", file=sys.stderr)
            return 1

    draft = G.load_draft(code, args.out)
    if draft is None:
        print(f"没有 {code} 的 draft，请先运行 extract_grading.py", file=sys.stderr)
        return 1

    print_draft(draft)
    if G.load_grading(code, args.out) is not None:
        print("\n注意：该课已有 grading.json，确认将覆盖它并重置 history 基线。")

    if not args.yes:
        reply = input("\n确认保存为 grading.json？[y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("已取消，未写入。")
            return 0

    now = datetime.now(LOCAL_TZ)
    grading = dict(draft)
    grading.pop("source_text_preview", None)
    grading["confirmed_at"] = now.isoformat(timespec="seconds")
    grading.setdefault("manual_scores", {})
    if args.ungraded:
        grading["graded"] = False
    path = G.save_grading(code, grading, args.out)
    # Reset history with this confirmation as the baseline.
    G._write_history(code, [{
        "at": now.isoformat(timespec="seconds"),
        "instruction": "confirmed",
        "patch": None,
        "grading_after": grading,
    }], args.out)
    print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
