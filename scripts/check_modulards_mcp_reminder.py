#!/usr/bin/env python3
"""
Antigravity Webhook Hub — Modular DS MCP Access Follow-Up & Reminder
Checks Spark Desktop CoreData SQLite database for replies from Modular DS support (help@modulards.com / Crisp),
probes the MCP endpoint, and notifies Slack after the 1-week follow-up period.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

SPARK_DB_PATH = os.path.expanduser("~/Library/Application Support/Spark Mail/core-data/messages.sqlite")
DEFAULT_SLACK_CHANNEL = "C096KR96AF7"
HUB_URL = "http://127.0.0.1:9423"
# Inquiry timestamp: Sep 22, 2026 19:30:00 (epoch: 1790076600)
INQUIRY_TIMESTAMP = 1790076600


def check_spark_modulards_replies(db_path: str = SPARK_DB_PATH) -> dict[str, Any]:
    """
    Query Spark Mail CoreData SQLite database for messages from Modular DS or Crisp support
    received after the inquiry timestamp.
    """
    if not os.path.exists(db_path):
        return {
            "status": "error",
            "message": f"Spark database not found at '{db_path}'",
            "replied": False,
            "replies": [],
        }

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()

        cur.execute(
            """
            SELECT pk, subject, conversationPk, messageFrom, receivedDate, shortBody
            FROM messages
            WHERE (messageFrom LIKE '%modulards%' OR messageFrom LIKE '%crisp%')
              AND receivedDate >= ?
            ORDER BY receivedDate DESC
            """,
            (INQUIRY_TIMESTAMP,),
        )
        rows = cur.fetchall()
        conn.close()

        replies = []
        for r in rows:
            # Filter out automated login alerts
            subject = r[1] or ""
            if "New login to Modular" in subject or "is up again" in subject or "seems down" in subject:
                continue

            dt = datetime.datetime.fromtimestamp(r[4], datetime.timezone.utc)
            replies.append({
                "pk": r[0],
                "subject": subject,
                "conversation_pk": r[2],
                "from": r[3],
                "date": dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "short_body": (r[5] or "")[:300],
            })

        return {
            "status": "success",
            "replied": len(replies) > 0,
            "reply_count": len(replies),
            "replies": replies,
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"SQLite query failed: {e}",
            "replied": False,
            "replies": [],
        }


def notify_slack(result: dict[str, Any], channel: str = DEFAULT_SLACK_CHANNEL) -> bool:
    """Send follow-up status notification to Slack."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        env_p = Path(__file__).resolve().parent.parent / ".env"
        if env_p.is_file():
            for line in env_p.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("SLACK_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip("'\"")
                    break

    if not token:
        print("[WARN] No SLACK_BOT_TOKEN found. Skipping Slack notification.", file=sys.stderr)
        return False

    if result.get("replied"):
        first_rep = result["replies"][0]
        text = (
            f"🎉 *Modular DS MCP 权限跟进 — 已收到官方客服回复!*\n"
            f"• *发件人*: `{first_rep['from']}`\n"
            f"• *主题*: {first_rep['subject']}\n"
            f"• *时间*: {first_rep['date']}\n"
            f"• *回复摘要*: {first_rep['short_body']}\n\n"
            f"👉 *下一步*: 请检查回复内容。若已通过白名单，直接在终端执行：\n"
            f"```claude mcp add --transport http modulards https://mcp.modulards.com/mcp```\n"
            f"或通过 `mcp_config.json` 连接完成 OAuth 绑定。"
        )
    else:
        text = (
            f"⏳ *Modular DS MCP 申请满 1 周提醒 — 尚未收到官方回复*\n"
            f"• *申请事项*: Modular DS Model Context Protocol (MCP) 访问白名单\n"
            f"• *申请渠道*: Modular DS 官网在线客服 (Crisp Chat)\n"
            f"• *状态*: 距 2026-09-22 申请已达 1 周，目前邮箱中未检索到官方客服跟进邮件。\n\n"
            f"⚠️ *建议操作*:\n"
            f"1. 登录 `https://app.modulards.com`，点击右下角聊天小挂件向客服发送 Follow-up：\n"
            f"> _'Hi! Following up on my request for MCP server access submitted last week. Any update on enabling it for my account? Thanks!'_\n"
            f"2. 检查垃圾邮件箱或 Crisp 自动邮件回复通知。"
        )

    try:
        payload = json.dumps({
            "channel": channel,
            "text": text,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                print(f"[INFO] Successfully posted reminder to Slack channel {channel}")
                return True
            else:
                print(f"[ERROR] Slack API returned error: {data.get('error')}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"[ERROR] Failed to post Slack message: {e}", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Modular DS MCP 1-week follow-up reminder")
    parser.add_argument("--notify-slack", action="store_true", help="Send result notification to Slack")
    parser.add_argument("--channel", default=DEFAULT_SLACK_CHANNEL, help="Target Slack channel")
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    args = parser.parse_args()

    result = check_spark_modulards_replies()

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Modular DS MCP Inquiry Check: replied={result['replied']}, count={result.get('reply_count', 0)}")
        if result["replied"]:
            for rep in result["replies"]:
                print(f" - [{rep['date']}] {rep['from']}: {rep['subject']}")
                print(f"   {rep['short_body'][:150]}...")
        else:
            print(" No customer support reply detected yet since inquiry.")

    if args.notify_slack:
        notify_slack(result, channel=args.channel)

    return 0


if __name__ == "__main__":
    sys.exit(main())
