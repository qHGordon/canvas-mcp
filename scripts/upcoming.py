"""汇总未来 N 天的 DDL 和最近公告，供定时任务/日常查阅。

用途:   跨所有 active 课程：未来 N 天内到期的作业（按 due_at 排序）+
        最近 M 天的公告（按 posted_at 倒序），逐课失败记入 errors 不中断。
输入:   环境变量 CANVAS_BASE_URL + CANVAS_TOKEN 或 CANVAS_TOKEN_FILE。
        可选参数: --days N (DDL 窗口, 默认 14) --ann-days M (公告回看天数,
        默认 7) --out DIR (默认仓库根的 data/)。
输出:   <out>/upcoming_<date>.json
        {generated_at, days, announcement_days, deadlines[], announcements[], errors[]}
幂等:   同一天重复运行覆盖同名文件；严格只读。
最后验证通过: 2026-10-22（真实数据）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from canvas_mcp.client import CanvasConfigError, load_config
from canvas_mcp.crawl import build_upcoming
from canvas_mcp.formatters import LOCAL_TZ

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--ann-days", type=int, default=7)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    try:
        load_config()  # fail fast with a clear message
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    result = await build_upcoming(days=args.days, ann_days=args.ann_days)
    today = datetime.now(LOCAL_TZ)
    result["generated_at"] = today.isoformat(timespec="seconds")

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"upcoming_{today.strftime('%Y-%m-%d')}.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for d in result["deadlines"]:
        print(f"- DDL {d.get('due_at_local') or d.get('due_at')} | "
              f"{d.get('course')} | {d.get('name')}")
    for a in result["announcements"]:
        print(f"- ANN {a.get('posted_at_local') or a.get('posted_at')} | "
              f"{a.get('course')} | {a.get('title')}")
    for err in result["errors"]:
        print(f"! {err}")
    print(f"wrote {out_path} ({len(result['deadlines'])} deadlines, "
          f"{len(result['announcements'])} announcements)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
