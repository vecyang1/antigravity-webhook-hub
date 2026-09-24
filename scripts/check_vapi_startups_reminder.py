#!/usr/bin/env python3
"""
Antigravity Webhook Hub — Vapi Startups Program Follow-Up & Reply Checker
Checks Spark Desktop CoreData SQLite database (~/Library/Application Support/Spark Mail/core-data/messages.sqlite)
in <2ms for approval, replies, or updates regarding the Vapi Startups Program application (Email PK 725901, Conv PK 1117913).

Can notify Slack channel C096KR96AF7 and register itself into Webhook Hub's SSOT scheduler.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

SPARK_DB_PATH = os.path.expanduser("~/Library/Application Support/Spark Mail/core-data/messages.sqlite")
SUBMISSION_MESSAGE_PK = 725901
SUBMISSION_CONVERSATION_PK = 1117913
# Submission timestamp: 2026-09-24 09:59:12 (epoch: 1790215152)
SUBMISSION_TIMESTAMP = 1790215152
# Expected turnaround: 7 business days -> approx 2026-10-05 10:00:00+08:00
DUE_DATE_ISO = "2026-10-05T10:00:00+08:00"

DEFAULT_SLACK_CHANNEL = "C096KR96AF7"
HUB_URL = "http://127.0.0.1:9423"


def check_spark_vapi_reply(db_path: str = SPARK_DB_PATH) -> dict[str, Any]:
    """
    Fast-path CoreData SQLite query to verify if Vapi has replied or sent updates
    regarding the Startups Program application.
    """
    if not os.path.exists(db_path):
        # Fallback to spark CLI if SQLite not found
        return check_via_spark_cli()

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()

        # 1. Check conversation 1117913 for direct replies
        cur.execute(
            """
            SELECT pk, subject, conversationPk, messageFrom, receivedDate, shortBody
            FROM messages
            WHERE conversationPk = ? AND pk != ?
            ORDER BY receivedDate DESC
            """,
            (SUBMISSION_CONVERSATION_PK, SUBMISSION_MESSAGE_PK),
        )
        conv_replies = cur.fetchall()

        # 2. Check for any new message from vapi.ai received after submission
        cur.execute(
            """
            SELECT pk, subject, conversationPk, messageFrom, receivedDate, shortBody
            FROM messages
            WHERE pk > ?
              AND (messageFrom LIKE '%vapi.ai%' OR subject LIKE '%Vapi%' OR subject LIKE '%Startups%')
              AND receivedDate >= ?
            ORDER BY receivedDate DESC
            """,
            (SUBMISSION_MESSAGE_PK, SUBMISSION_TIMESTAMP),
        )
        vapi_replies = cur.fetchall()
        conn.close()

        # Deduplicate by PK
        seen_pks = set()
        all_rows = []
        for r in conv_replies + vapi_replies:
            if r[0] != SUBMISSION_MESSAGE_PK and r[0] not in seen_pks:
                # Filter out obvious false positives (e.g. unrelated emails)
                subj = r[1] or ""
                sender = r[3] or ""
                if "vapi" not in sender.lower() and "vapi" not in subj.lower() and "startups" not in subj.lower():
                    continue
                seen_pks.add(r[0])
                all_rows.append(r)

        replies = []
        for r in all_rows:
            raw_date = r[4]
            date_str = str(raw_date)
            try:
                if raw_date > 1.5e9:
                    dt = datetime.datetime.fromtimestamp(raw_date, tz=datetime.timezone.utc)
                else:
                    dt = datetime.datetime(2001, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=raw_date)
                date_str = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

            replies.append({
                "pk": r[0],
                "subject": r[1],
                "conversation_pk": r[2],
                "from": r[3],
                "date": date_str,
                "short_body": (r[5] or "")[:350],
            })

        # Calculate deadline status
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        due_dt = datetime.datetime.fromisoformat(DUE_DATE_ISO)
        is_overdue = now > due_dt

        return {
            "status": "success",
            "replied": len(replies) > 0,
            "reply_count": len(replies),
            "replies": replies,
            "submission_pk": SUBMISSION_MESSAGE_PK,
            "conversation_pk": SUBMISSION_CONVERSATION_PK,
            "due_date": DUE_DATE_ISO,
            "is_overdue": is_overdue,
            "method": "sqlite",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"SQLite inspection failed: {e}",
            "replied": False,
            "reply_count": 0,
            "replies": [],
            "submission_pk": SUBMISSION_MESSAGE_PK,
            "conversation_pk": SUBMISSION_CONVERSATION_PK,
            "due_date": DUE_DATE_ISO,
            "is_overdue": False,
            "method": "sqlite_error",
        }


def check_via_spark_cli() -> dict[str, Any]:
    """Fallback search using the spark command line tool."""
    try:
        cmd = ["spark", "emails", "--filter", "from:vapi.ai newer_than:14d", "--json"]
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5)
        data = json.loads(out)
        emails = data if isinstance(data, list) else data.get("emails", [])

        replies = []
        for em in emails:
            pk = em.get("id") or em.get("pk")
            if pk and int(pk) != SUBMISSION_MESSAGE_PK:
                replies.append({
                    "pk": pk,
                    "subject": em.get("subject", ""),
                    "from": em.get("from", ""),
                    "date": em.get("date", ""),
                    "short_body": em.get("body", "")[:300],
                })

        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        due_dt = datetime.datetime.fromisoformat(DUE_DATE_ISO)

        return {
            "status": "success",
            "replied": len(replies) > 0,
            "reply_count": len(replies),
            "replies": replies,
            "submission_pk": SUBMISSION_MESSAGE_PK,
            "conversation_pk": SUBMISSION_CONVERSATION_PK,
            "due_date": DUE_DATE_ISO,
            "is_overdue": now > due_dt,
            "method": "spark_cli",
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Spark CLI query failed: {e}",
            "replied": False,
            "reply_count": 0,
            "replies": [],
            "submission_pk": SUBMISSION_MESSAGE_PK,
            "conversation_pk": SUBMISSION_CONVERSATION_PK,
            "due_date": DUE_DATE_ISO,
            "is_overdue": False,
            "method": "spark_cli_error",
        }


def notify_slack(result: dict[str, Any], channel: str = DEFAULT_SLACK_CHANNEL, force: bool = False) -> bool:
    """Send notification to Slack."""
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

    replied = result.get("replied", False)
    is_overdue = result.get("is_overdue", False)

    # If neither replied nor overdue, only notify if force is True
    if not replied and not is_overdue and not force:
        print("[INFO] No new replies and not overdue yet. Skipping Slack notification.")
        return True

    if replied:
        first_rep = result["replies"][0]
        text = (
            f"🎉 *Vapi Startups Program 审核进展通知 — 已收到官方邮件!*\n"
            f"• *发件人*: `{first_rep['from']}`\n"
            f"• *主题*: {first_rep['subject']}\n"
            f"• *到达时间*: {first_rep['date']}\n"
            f"• *内容摘要*: {first_rep['short_body']}\n\n"
            f"👉 请打开 Spark 客户端查看详情并完成后续入驻步骤（Slack 接入与额度激活）。"
        )
    elif is_overdue:
        text = (
            f"⏳ *Vapi Startups Program 审核进度提醒 — 已满 7 个工作日*\n"
            f"• *申请项目*: Vapi Startups Program (90,000 分钟通话额度支持)\n"
            f"• *申请实体*: World Inspire LLC (https://worldinspirelab.com/)\n"
            f"• *提交时间*: 2026-09-24 09:59 (确认邮件 PK: {SUBMISSION_MESSAGE_PK})\n"
            f"• *当前状态*: 已达到官方承诺的 7 个工作日审核周期，目前暂未收到进一步回复。\n\n"
            f"⚠️ *建议跟进策略*:\n"
            f"可通过 Spark 邮箱向 `startups@mail.vapi.ai` 发送跟进邮件：\n"
            f"> *主题*: `Re: We've received your Vapi Startups Program application! - Follow Up`\n"
            f"> *内容*: “Hi Vapi Startups Team, Following up on our application submitted on Sep 24 for World Inspire Lab. We are preparing to roll out our voice tutor MVP and would love to know if any further details are required. Thank you!”"
        )
    else:
        text = (
            f"ℹ️ *Vapi Startups Program 自动巡检状态报告*\n"
            f"• *申请项目*: Vapi Startups Program\n"
            f"• *申请实体*: World Inspire LLC (https://worldinspirelab.com/)\n"
            f"• *提交时间*: 2026-09-24 09:59 (确认邮件 PK: {SUBMISSION_MESSAGE_PK})\n"
            f"• *官方周期*: 7 个工作日（预计审核节点：2026-10-05）\n"
            f"• *当前状态*: 巡检正常，等待官方审核团队处理中。"
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


def register_with_webhook_hub(hub_url: str = HUB_URL, cron_expr: str = "0 10 * * 1-5") -> bool:
    """Register recurring schedule in Webhook Hub SSOT with idempotency."""
    schedule_name = "Vapi Startups Program 审核结果与邮件跟进提醒"

    # 1. Preflight check for existing active schedule
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

    # 2. Check direct SQLite if gateway offline
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

    # 3. Register recurring schedule
    sched_payload = {
        "name": schedule_name,
        "schedule_type": "recurring",
        "cron_expression": cron_expr,
        "action_type": "cli",
        "command": f"{sys.executable} {os.path.abspath(__file__)} --notify-slack",
        "target_action": "check_vapi_startups_reply",
        "action_params": {
            "conversation_pk": SUBMISSION_CONVERSATION_PK,
            "message_pk": SUBMISSION_MESSAGE_PK,
            "entity": "World Inspire LLC",
            "due_date": DUE_DATE_ISO,
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
        print(f"[INFO] Gateway {req_url} error ({e}). Writing directly to SQLite SSOT...", file=sys.stderr)
        try:
            from hub.db import DatabaseManager
            db = DatabaseManager("data/webhook_hub.db")
            from hub.scheduler import calculate_next_cron_run, format_iso_utc
            next_dt = calculate_next_cron_run(cron_expr)
            iso_utc = format_iso_utc(next_dt)
            sched_record = {
                "name": sched_payload["name"],
                "source": "spark_reminder",
                "schedule_type": "recurring",
                "cron_expression": cron_expr,
                "scheduled_at": iso_utc,
                "next_run_at": iso_utc,
                "action_type": "cli",
                "command": sched_payload["command"],
                "target_action": sched_payload["target_action"],
                "action_params": sched_payload["action_params"],
                "status": "active",
            }
            sch_id = db.insert_schedule(sched_record)
            db.close()
            print(f"[OK] Directly registered schedule in SQLite SSOT: {sch_id} (Next run: {iso_utc})")
            return True
        except Exception as db_err:
            print(f"[ERROR] Direct SQLite registration failed: {db_err}", file=sys.stderr)
            return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Spark Mail for Vapi Startups replies and manage reminders")
    parser.add_argument("--notify-slack", action="store_true", help="Send notification to Slack channel C096KR96AF7")
    parser.add_argument("--force-notify", action="store_true", help="Force send status notification to Slack even if not overdue")
    parser.add_argument("--register-schedule", action="store_true", help="Register schedule in Webhook Hub SSOT")
    parser.add_argument("--cron", default="0 10 * * 1-5", help="Cron expression for recurring schedule (default: 0 10 * * 1-5)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args()

    # 1. Run check
    result = check_spark_vapi_reply()

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("=" * 60)
        print("⚡ Spark Mail Vapi Startups 审核状态检查")
        print("=" * 60)
        print(f"• 提交邮件 PK: {result.get('submission_pk')} (会话: {result.get('conversation_pk')})")
        print(f"• 承诺审核节点: {result.get('due_date')} (已超时: {'⚠️ 是' if result.get('is_overdue') else '否 (周期内)'})")
        print(f"• 是否已收到回复: {'🎉 是' if result.get('replied') else '⏳ 否 (等待审核中)'}")
        print(f"• 新增相关邮件数: {result.get('reply_count')}")
        for idx, rep in enumerate(result.get("replies", []), 1):
            print(f"\n  [邮件 #{idx}]")
            print(f"  来自: {rep['from']}")
            print(f"  时间: {rep['date']}")
            print(f"  主题: {rep['subject']}")
            print(f"  正文摘要: {rep['short_body']}")
        print("=" * 60)

    # 2. Slack notification
    if args.notify_slack:
        notify_slack(result, force=args.force_notify)

    # 3. Schedule registration
    if args.register_schedule:
        register_with_webhook_hub(cron_expr=args.cron)

    return 0


if __name__ == "__main__":
    sys.exit(main())
