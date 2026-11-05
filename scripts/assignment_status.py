"""汇总所有（或单门）active 课程的作业提交状态，供定时任务/日常查阅。

用途:   跨所有 active 课程（或 --course-id 指定的一门）用 submission 对象计算每个
        作业的真实提交状态。状态在 Python 里按固定规则判定，枚举为 not_submitted /
        submitted_ungraded / graded / excused / unknown，附 overdue/late 标记。
        额外列出模块里像作业讲义却在 Canvas 上找不到对应 assignment 的 File 条目
        （unmatched_assignment_files）。逐课失败记入 errors 不中断。
        注意：模块的 module_view_progress 只是浏览进度，绝不用来判断提交，见 CLAUDE.md。
输入:   环境变量 CANVAS_BASE_URL + CANVAS_TOKEN 或 CANVAS_TOKEN_FILE。
        可选参数: --course-id N (只查一门课) --out DIR (默认仓库根的 data/)。
输出:   <out>/status_<date>.json
        {generated_at, assignments[], unmatched_assignment_files[], errors[]}
幂等:   同一天重复运行覆盖同名文件；严格只读。
最后验证通过: 2026-11-04（真实数据）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from canvas_mcp.client import CanvasClient, CanvasConfigError
from canvas_mcp.crawl import build_assignment_status
from canvas_mcp.formatters import LOCAL_TZ

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--course-id", type=int, default=None)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    try:
        client = CanvasClient.from_env()
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    try:
        result = await build_assignment_status(
            client, datetime.now(timezone.utc), course_id=args.course_id
        )
    finally:
        await client.aclose()

    today = datetime.now(LOCAL_TZ)
    result["generated_at"] = today.isoformat(timespec="seconds")

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"status_{today.strftime('%Y-%m-%d')}.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for a in result["assignments"]:
        marks = "".join(f" [{f}]" for f in a.get("flags", []))
        print(
            f"- {a.get('status_zh')}{marks} | {a.get('course')} | {a.get('name')}"
            f" | due {a.get('due_at_local') or a.get('due_at') or '—'}"
        )
    for u in result["unmatched_assignment_files"]:
        print(f"? 无提交入口 | {u.get('course')} | {u.get('title')} ({u.get('module')})")
    for err in result["errors"]:
        print(f"! {err.get('course')} {err.get('section')}: HTTP {err.get('status')}")
    print(
        f"wrote {out_path} ({len(result['assignments'])} assignments, "
        f"{len(result['unmatched_assignment_files'])} unmatched files)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
