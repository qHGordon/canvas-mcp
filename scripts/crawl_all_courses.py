"""遍历所有 active 课程，建立课程材料索引。

用途:   tabs → modules(items) → File 详情 → pages → assignments → announcements，
        合并成 course → module → item 的树；/files 403 时自动从模块恢复文件列表。
输入:   环境变量 CANVAS_BASE_URL + CANVAS_TOKEN 或 CANVAS_TOKEN_FILE。
        可选参数: --course-id N (只爬一门课) --since YYYY-MM-DD (公告起始日,
        默认 120 天前) --out DIR (默认仓库根的 data/)。
输出:   <out>/index_<date>.json  完整索引树（每课带 errors 列表）
        <out>/files_<date>.csv   扁平文件清单 (course_id, course_name, file_id,
        display_name, size, content_type, updated_at, source, module, url)
幂等:   同一天重复运行覆盖同名文件；严格只读。
最后验证通过: 2026-10-22（3 门课真实数据）
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from canvas_mcp.client import CanvasClient, CanvasConfigError
from canvas_mcp.crawl import CSV_COLUMNS, crawl_courses, files_to_rows
from canvas_mcp.formatters import LOCAL_TZ

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--course-id", type=int, default=None)
    parser.add_argument("--since", default=None, help="announcements start date, YYYY-MM-DD")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    today = datetime.now(LOCAL_TZ)
    since = args.since or (today - timedelta(days=120)).strftime("%Y-%m-%d")
    try:
        client = CanvasClient.from_env()
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    try:
        index = await crawl_courses(client, since=since, course_id=args.course_id)
    finally:
        await client.aclose()
    index["generated_at"] = today.isoformat(timespec="seconds")

    args.out.mkdir(parents=True, exist_ok=True)
    date = today.strftime("%Y-%m-%d")
    json_path = args.out / f"index_{date}.json"
    csv_path = args.out / f"files_{date}.csv"

    json_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows = files_to_rows(index)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    for course in index["courses"]:
        print(
            f"- {course['id']} {course.get('course_code') or course['name']}: "
            f"{len(course['modules'])} modules, {len(course['files'])} files, "
            f"{len(course['pages'])} pages, {len(course['assignments'])} assignments, "
            f"{len(course['announcements'])} announcements, "
            f"{len(course['errors'])} errors"
        )
        for err in course["errors"]:
            print(f"    ! {err['section']}: HTTP {err['status']}")
    print(f"wrote {json_path} and {csv_path} ({len(rows)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
