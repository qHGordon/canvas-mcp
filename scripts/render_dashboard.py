"""把已算好的 progress.json 渲染成单文件 dashboard.html（不发任何请求）。

用途:   读取 data/courses/*/progress.json，渲染成深色模式、中文、无 CDN 依赖的
        单文件 HTML。只做渲染，不计算、不联网；需要先跑 update_progress.py 生成
        progress.json。
输入:   可选参数: --out DIR（默认仓库根的 data/）。
输出:   data/dashboard.html
幂等:   重复运行覆盖同名文件。
最后验证通过: 2026-11-04
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from canvas_mcp import report
from canvas_mcp.formatters import LOCAL_TZ

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    progress = report.load_all_progress(args.out)
    if not progress:
        print("没有找到任何 progress.json，请先运行 update_progress.py", file=sys.stderr)
        return 1
    # Use the newest per-course generated_at as the page timestamp.
    stamps = [p.get("generated_at") for p in progress if p.get("generated_at")]
    generated = max(stamps) if stamps else datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    path = report.write_dashboard(progress, generated, args.out)
    print(f"wrote {path} ({len(progress)} courses)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
