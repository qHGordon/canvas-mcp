"""用自然语言修改已确认的 grading.json：解析→校验→预览→确认→写入→重算→渲染。

用途:   把一句话（如 "期中考了 21/25"、"加一项 participation 占 5%，从 final 里扣"）
        交给本地 LLM 解析成补丁；补丁只能是 add_item/update_item/remove_item/
        set_manual_score/clear_manual_score/map_assignment/unmap_assignment/set_grade_scale。
        校验（项目/作业 id 存在、score≤out_of、drop<作业数、权重之和=100）全部由代码完成，
        不自动平衡；有歧义时 LLM 返回澄清问题而不是猜。确认后写入并记 history，
        随后自动重算 progress 并重渲染 dashboard。LLM 不参与任何分数计算。
输入:   --course-id N "指令"；--yes 跳过确认（供脚本调用）；--undo 撤销最近一次修改。
        需要 CANVAS_* 和本地 LLM（LLM_BASE_URL/LLM_MODEL）环境变量。
输出:   更新 data/courses/<code>/grading.json、grading.history.jsonl、progress.json、
        data/dashboard.html
幂等:   写入是状态变更（有 history/undo 保护）；对 Canvas 严格只读。
最后验证通过: 2026-11-04（逻辑 + 单测；真实解析需本地 LLM）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from canvas_mcp import grading as G
from canvas_mcp import report
from canvas_mcp.client import CanvasClient, CanvasConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--course-id", type=int, required=True)
    p.add_argument("instruction", nargs="?", default=None)
    p.add_argument("--yes", action="store_true", help="跳过确认直接写入")
    p.add_argument("--undo", action="store_true", help="撤销最近一次修改")
    p.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    return p.parse_args()


def print_preview(res: dict) -> None:
    print("\n将要应用的修改：")
    for line in res["diff"] or ["（无结构变化）"]:
        print(f"  {line}")
    imp = res["impact"]
    b, a = imp["before"], imp["after"]
    print(f"\n对进度的影响（目标 {imp['target']}）：")
    if b and a:
        print(f"  保底   {b['floor']} → {a['floor']}")
        print(f"  上限   {b['ceiling']} → {a['ceiling']}")
        print(f"  已评均 {b['graded_avg']} → {a['graded_avg']}")
        print(f"  拿 {imp['target']} 还需平均 {b['need_pct']}% → {a['need_pct']}% "
              f"（{b['target_state']} → {a['target_state']}）")
    else:
        print("  （无法计算前后对比）")


async def run(args: argparse.Namespace) -> int:
    try:
        client = CanvasClient.from_env()
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc)
    try:
        course = await report.get_course(client, args.course_id)
        if not course:
            print(f"找不到 active 课程 {args.course_id}", file=sys.stderr)
            return 1
        code = course.get("course_code")

        if args.undo:
            restored = G.undo_last(code, args.out)
            if restored is None:
                print("没有可撤销的修改（只剩确认基线或无 history）。")
                return 0
            await report.refresh_progress(client, now, base=args.out, course_id=args.course_id)
            print("已撤销最近一次修改，并重算 progress + dashboard。")
            return 0

        if not args.instruction:
            print("需要提供指令，或使用 --undo", file=sys.stderr)
            return 2

        res = await report.prepare_amendment(client, course, args.instruction, now, args.out)
        if not res["ok"]:
            if res.get("clarification"):
                print(f"需要澄清：{res['clarification']}")
            else:
                print(f"无法应用：{res['error']}")
            return 1

        print_preview(res)
        if not args.yes:
            reply = input("\n确认写入？[y/N] ").strip().lower()
            if reply not in ("y", "yes"):
                print("已取消。")
                return 0

        await report.commit_amendment(
            client, course, res["new_grading"], args.instruction, res["patch"], now, args.out
        )
        print("已写入 grading.json，并重算 progress + dashboard。")
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
