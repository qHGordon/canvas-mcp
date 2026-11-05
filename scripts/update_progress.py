"""定时任务入口：assignment_status → progress → dashboard，全程不需要 LLM。

用途:   抓取所有（或单门）active 课程的作业提交状态，用已确认的 grading.json 计算
        progress，写每课的 progress.json，再渲染 data/dashboard.html。未确认权重的
        课程输出 status=unconfirmed 并在页面上提示；graded:false 的课程标记跳过。
        不调用任何 LLM，对 Canvas 严格只读。
输入:   可选参数: --course-id N（只更新一门）--out DIR（默认仓库根的 data/）。
        需要 CANVAS_* 环境变量。
输出:   data/courses/<code>/progress.json、data/dashboard.html
幂等:   重复运行覆盖同名文件；只读 Canvas。
最后验证通过: 2026-11-04（真实数据，3 门课）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from canvas_mcp import report
from canvas_mcp.client import CanvasClient, CanvasConfigError
from canvas_mcp.formatters import LOCAL_TZ

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--course-id", type=int, default=None)
    p.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return p.parse_args()


async def run(args: argparse.Namespace) -> int:
    try:
        client = CanvasClient.from_env()
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    try:
        results = await report.refresh_progress(
            client, now, base=args.out, course_id=args.course_id
        )
    finally:
        await client.aclose()

    for p in results:
        if p.get("status") == "ok":
            a = next((t for t in p["targets"] if t["label"] == "A"), {})
            need = a.get("need_pct")
            need_txt = ("已锁定" if a.get("state") == "locked"
                        else "无法达到" if a.get("state") == "impossible"
                        else f"需均 {need}%")
            print(f"- {p.get('course_code')}: 保底 {p['floor']} / 上限 {p['ceiling']}"
                  f" / 已评均 {p['graded_avg']} / 拿A {need_txt}")
        else:
            print(f"- {p.get('course_code')}: {p.get('status')}")
    local = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    print(f"dashboard 更新于 {local} → {args.out / 'dashboard.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
