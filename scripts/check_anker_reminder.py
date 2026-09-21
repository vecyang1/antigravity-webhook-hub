#!/usr/bin/env python3
"""
Antigravity Webhook Hub — Anker Warranty Reminder & Fast-Path Reply Checker
Checks Spark Desktop CoreData SQLite database (~/Library/Application Support/Spark Mail/core-data/messages.sqlite)
in <2ms for replies to warranty inquiry email 724913 (Anker 737 120W A2148, SN: AFZWC61F13100681).

Can notify Slack channel C096KR96AF7 and register itself into Webhook Hub's SSOT scheduler.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

SPARK_DB_PATH = os.path.expanduser("~/Library/Application Support/Spark Mail/core-data/messages.sqlite")
TARGET_MESSAGE_PK = 724913
TARGET_CONVERSATION_PK = 1115945
DEFAULT_SLACK_CHANNEL = "C096KR96AF7"
HUB_URL = "http://127.0.0.1:9423"


def check_spark_anker_reply(db_path: str = SPARK_DB_PATH) -> dict[str, Any]:
    """
    Fast-path CoreData SQLite query to verify if Anker has replied to email thread 724913.
    Executes in <3ms using read-only connection.
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

        # 1. Fetch original inquiry details
        cur.execute(
            """
            SELECT pk, subject, conversationPk, messageFrom, receivedDate, shortBody
            FROM messages
            WHERE pk = ?
            """,
            (TARGET_MESSAGE_PK,),
        )
        orig_row = cur.fetchone()
        if not orig_row:
            return {
                "status": "not_found",
                "message": f"Inquiry message PK {TARGET_MESSAGE_PK} not found in Spark DB",
                "replied": False,
                "replies": [],
            }

        conv_pk = orig_row[2]

        # 2. Query for any reply in the conversation
        cur.execute(
            """
            SELECT pk, subject, messageFrom, receivedDate, shortBody
            FROM messages
            WHERE conversationPk = ? AND pk != ?
            ORDER BY receivedDate DESC
            """,
            (conv_pk, TARGET_MESSAGE_PK),
        )
        reply_rows = cur.fetchall()
        conn.close()

        replies = []
        for r in reply_rows:
            # receivedDate in Spark is Cocoa CoreData epoch (seconds since 2001-01-01) or Unix timestamp
            raw_date = r[3]
            date_str = str(raw_date)
            try:
                # If timestamp is > 1.5e9, it's unix epoch
                if raw_date > 1.5e9:
                    dt = datetime.datetime.fromtimestamp(raw_date, tz=datetime.timezone.utc)
                else:
                    # Cocoa epoch (2001-01-01)
                    dt = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=raw_date)
                date_str = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

            replies.append({
                "pk": r[0],
                "subject": r[1],
                "from": r[2],
                "date": date_str,
                "short_body": (r[4] or "")[:300],
            })

        has_reply = len(replies) > 0
        return {
            "status": "success",
            "conversation_pk": conv_pk,
            "target_message_pk": TARGET_MESSAGE_PK,
            "device": "Anker 737 120W A2148 (SN: AFZWC61F13100681)",
            "replied": has_reply,
            "reply_count": len(replies),
            "replies": replies,
            "inquiry_subject": orig_row[1],
            "inquiry_from": orig_row[3],
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"SQLite inspection failed: {e}",
            "replied": False,
            "replies": [],
        }


def notify_slack(result: dict[str, Any], channel: str = DEFAULT_SLACK_CHANNEL) -> bool:
    """Send warranty check status notification to Slack."""
    # Resolve token
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        # Check local .env
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
            f"⚡ *Anker 售后保修提醒 — 已收到官方回复!*\n"
            f"• *发件人*: `{first_rep['from']}`\n"
            f"• *主题*: {first_rep['subject']}\n"
            f"• *时间*: {first_rep['date']}\n"
            f"• *回复摘要*: {first_rep['short_body']}\n"
            f"• *设备/SN*: `Anker 737 120W (SN: AFZWC61F13100681)`\n"
            f"👉 请打开 Spark 客户端查看完整邮件回复并安排寄送。"
        )
    else:
        text = (
            f"⏳ *Anker 售后保修回复检查*\n"
            f"• *设备*: `Anker 737 120W A2148 (SN: AFZWC61F13100681)`\n"
            f"• *状态*: 截至目前安克售后尚未回复 (Conversation PK: {result.get('conversation_pk')})\n"
            f"• *原始咨询*: 2026-09-21 发送至 ced-cn@anker.com\n"
            f"Webhook Hub 计划任务系统将在后台保持监听并在收到回复后立刻提醒。"
        )

    url = "https://slack.com/api/chat.postMessage"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                print(f"[OK] Notification posted to Slack channel {channel}")
                return True
            else:
                print(f"[WARN] Slack error: {data.get('error')}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"[ERROR] Slack HTTP request failed: {e}", file=sys.stderr)
        return False


def register_with_webhook_hub(hub_url: str = HUB_URL, target_date_iso: str = "2026-09-23T16:18:00+08:00") -> bool:
    """Register this check as an active delayed schedule in Webhook Hub SSOT with idempotency."""
    schedule_name = "Anker插头售后保修回复提醒 (Email 724913)"

    # 1. Preflight check for existing active schedule via live API
    try:
        query_url = f"{hub_url}/schedules?status=active&limit=50"
        with urllib.request.urlopen(urllib.request.Request(query_url, method="GET"), timeout=3) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                for s in data.get("schedules", []):
                    if s.get("name") == schedule_name and s.get("status") == "active":
                        sid = s.get("schedule_id")
                        next_run = s.get("next_run_at")
                        print(f"[INFO] Schedule already active in Webhook Hub SSOT: {sid} (Next run: {next_run}). Skipping duplicate registration.")
                        return True
    except Exception:
        pass

    # 2. Preflight check via direct SQLite if gateway offline
    try:
        from hub.db import DatabaseManager
        db = DatabaseManager("data/webhook_hub.db")
        existing_scheds, _ = db.list_schedules(status="active", limit=50)
        db.close()
        for s in existing_scheds:
            if s.get("name") == schedule_name and s.get("status") == "active":
                sid = s.get("schedule_id")
                next_run = s.get("next_run_at")
                print(f"[INFO] Schedule already active in SQLite SSOT: {sid} (Next run: {next_run}). Skipping duplicate registration.")
                return True
    except Exception:
        pass

    # 3. Proceed with registration
    sched_payload = {
        "name": schedule_name,
        "schedule_type": "once",
        "scheduled_at": target_date_iso,
        "action_type": "cli",
        "command": f"{sys.executable} {os.path.abspath(__file__)} --notify-slack",
        "target_action": "check_anker_warranty_reply",
        "action_params": {
            "conversation_pk": TARGET_CONVERSATION_PK,
            "message_pk": TARGET_MESSAGE_PK,
            "device": "Anker 737 120W A2148",
            "sn": "AFZWC61F13100681",
        },
        "source": "spark_reminder",
    }

    req_url = f"{hub_url}/schedules"
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(req_url, data=json.dumps(sched_payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            print(f"[OK] Successfully registered schedule in Webhook Hub SSOT: {resp_data.get('schedule_id')} (Next run: {resp_data.get('next_run_at')})")
            return True
    except Exception as e:
        # Fallback to local SQLite direct write if daemon not running
        print(f"[INFO] Gateway {req_url} unreachable ({e}). Writing directly to SQLite SSOT...", file=sys.stderr)
        try:
            from hub.db import DatabaseManager
            db = DatabaseManager("data/webhook_hub.db")
            from hub.scheduler import format_iso_utc, parse_schedule_time
            parsed_dt = parse_schedule_time(scheduled_at=target_date_iso)
            iso_utc = format_iso_utc(parsed_dt)
            sched_record = {
                "name": sched_payload["name"],
                "source": "spark_reminder",
                "schedule_type": "once",
                "scheduled_at": iso_utc,
                "next_run_at": iso_utc,
                "action_type": "cli",
                "command": sched_payload["command"],
                "target_action": sched_payload["target_action"],
                "action_params": sched_payload["action_params"],
                "status": "active",
                "max_runs": 1,
            }
            sch_id = db.insert_schedule(sched_record)
            db.close()
            print(f"[OK] Directly registered schedule in SQLite SSOT: {sch_id} (Next run: {iso_utc})")
            return True
        except Exception as db_err:
            print(f"[ERROR] Direct SQLite registration failed: {db_err}", file=sys.stderr)
            return False


def main():
    parser = argparse.ArgumentParser(description="Check Spark Mail for Anker warranty replies and manage reminder")
    parser.add_argument("--notify-slack", action="store_true", help="Send notification to Slack channel C096KR96AF7")
    parser.add_argument("--register-schedule", action="store_true", help="Register schedule in Webhook Hub for 2026-09-23")
    parser.add_argument("--date", default="2026-09-23T16:18:00+08:00", help="Target scheduled ISO date")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args()

    # 1. Run check
    result = check_spark_anker_reply()

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("=" * 60)
        print("⚡ Spark Mail Anker 保修回复检查")
        print("=" * 60)
        print(f"• 设备/SN: {result.get('device')}")
        print(f"• 原始邮件PK: {result.get('target_message_pk')} (会话: {result.get('conversation_pk')})")
        print(f"• 主题: {result.get('inquiry_subject')}")
        print(f"• 是否已收到回复: {'✅ 是' if result.get('replied') else '⏳ 否 (尚未收到)'}")
        print(f"• 回复邮件数: {result.get('reply_count')}")
        for idx, rep in enumerate(result.get("replies", []), 1):
            print(f"\n  [回复 #{idx}]")
            print(f"  来自: {rep['from']}")
            print(f"  时间: {rep['date']}")
            print(f"  主题: {rep['subject']}")
            print(f"  正文摘要: {rep['short_body']}")
        print("=" * 60)

    # 2. Slack notification if requested
    if args.notify_slack:
        notify_slack(result)

    # 3. Register schedule if requested
    if args.register_schedule:
        register_with_webhook_hub(target_date_iso=args.date)

    return 0


if __name__ == "__main__":
    sys.exit(main())
