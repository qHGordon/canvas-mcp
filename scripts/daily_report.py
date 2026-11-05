"""每日 Canvas 邮件日报：汇总变化 → 生成 HTML → 发邮件（可选）。

用途:   复用现有逻辑（assignment_status / upcoming / crawl_all_courses / progress）
        抓取数据，和上一次日报快照对比，只报变化（新出分 / 新课件 / 新公告 / 进度变化），
        固定显示课程进度与未来 7 天 DDL。公告摘要由本地 LLM 生成，不可用时退回原文前
        150 字；日报照发。对 Canvas 只读，只写 data/。发送成功后才推进快照（失败会补报）。
参数:   --dry-run 只生成文件不发送；--no-llm 跳过 LLM 摘要；
        --test-email 只发一封测试邮件验证 SMTP；--force 无变化也发。
输入:   CANVAS_* 环境变量；notify.env（见 notify.env.example，路径可用 NOTIFY_ENV 覆盖）。
输出:   data/reports/report_<date>.json、report_<date>.html；成功发送后更新 report_state.json。
最后验证通过: 2026-11-04（--dry-run --no-llm 真实数据）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from canvas_mcp import daily, notify, report_html
from canvas_mcp.client import CanvasClient, CanvasConfigError


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="只生成文件，不发送")
    p.add_argument("--no-llm", action="store_true", help="跳过公告 LLM 摘要")
    p.add_argument("--test-email", action="store_true", help="只发一封测试邮件")
    p.add_argument("--force", action="store_true", help="即使没有变化也发送")
    return p.parse_args()


def _send_test(config: dict) -> int:
    msg = notify.Message(
        subject="[Canvas 日报] SMTP 测试邮件",
        text_body="这是一封测试邮件。如果你收到了，说明 notify.env 的 SMTP 配置正确。",
        html_body="<p>这是一封测试邮件。如果你收到了，说明 <b>notify.env</b> 的 SMTP 配置正确。</p>",
    )
    try:
        notify.send("email", msg, config)
    except notify.NotifyError as exc:
        print(f"测试邮件发送失败：{exc}", file=sys.stderr)
        return 1
    print(f"测试邮件已发送到 {config.get('MAIL_TO') or config.get('SMTP_USER')}")
    return 0


async def run(args: argparse.Namespace) -> int:
    config = notify.load_notify_config()

    if args.test_email:
        return _send_test(config)

    try:
        client = CanvasClient.from_env()
    except CanvasConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    target = config.get("TARGET_GRADE", "A")
    try:
        report_data = await daily.gather_report_data(
            client, now, base=None, use_llm=not args.no_llm
        )
    finally:
        await client.aclose()

    prev = daily.load_prev_report()
    changes = daily.detect_changes(prev, report_data, target=target)
    html, text = report_html.render_email(report_data, changes, now, config)
    subject = report_html.build_subject(now, report_data["deadlines"])

    json_path, html_path = daily.save_report(None, report_data["date"], report_data, html)
    print(f"生成日报 HTML：{html_path}")

    if args.dry_run:
        print("--dry-run：仅生成文件，未发送，未更新快照。")
        return 0

    skip_empty = str(config.get("REPORT_SKIP_EMPTY", "true")).lower() in ("1", "true", "yes")
    if (not daily.has_content(changes, report_data["deadlines"])
            and not changes.get("first_run") and skip_empty and not args.force):
        print("今日无变化且 REPORT_SKIP_EMPTY=true：不发送（用 --force 可强制发）。")
        return 0

    attachments = [
        (notify.ics_filename(d), notify.build_ics(d, now), "text/calendar")
        for d in report_data["deadlines"]
    ]
    msg = notify.Message(subject=subject, text_body=text, html_body=html,
                         attachments=attachments)
    ok, err = daily.deliver(config, msg, json_path, now, base=None)
    if not ok:
        print(f"发送失败（未更新快照，下次会补报）：{err}", file=sys.stderr)
        return 1
    print(f"已发送到 {config.get('MAIL_TO') or config.get('SMTP_USER')}，快照已更新。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
