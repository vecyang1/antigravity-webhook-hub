"""
Antigravity Webhook Hub — Slack Offline Catch-up & Reconciler
Scans Slack conversation threads (e.g. #input_agent) for unfulfilled offline requests
that occurred while MacBook was sleeping / unreachable, and recovers them into Webhook Hub.

Enforces:
1. SSOT & Idempotency: Checks SQLite tasks/session_threads before dispatching to eliminate spam.
2. Unidirectional Data Flow: Commits/dispatches task first, then posts progress to Slack.
3. Multimodal Protection: Handles pure image messages without prompt truncation or command leaks.
4. Native Toolchain Convergence: Exposes CLI entry `webhook-hub catchup` and eliminates external scripts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hub.antigravity.models import AntigravityTaskPayload
from hub.antigravity.thread_notifier import ThreadNotifier
from hub.contact_review.slack_notifier import resolve_slack_bot_token
from hub.db import DatabaseManager

logger = logging.getLogger("hub.antigravity.reconciler")

DEFAULT_CHANNEL_ID = "C0C1B86AMCN"  # #input_agent
DEFAULT_LOCAL_HUB_URL = "http://127.0.0.1:9423"
DEFAULT_RECONCILE_COOLDOWN_SECONDS = 900  # 15 minutes


def is_offline_notice(text: str) -> bool:
    """Detect if a Slack message contains an offline laptop notice."""
    if not text:
        return False
    return bool(
        re.search(
            r"(?:Antigravity 离线提示|Laptop Offline|离线提示|Webhook unreachable)",
            text,
            re.IGNORECASE,
        )
    )


def is_delivery_completion(text: str) -> bool:
    """Detect if a Slack message contains task completion delivery."""
    if not text:
        return False
    return bool(
        re.search(
            r"(?:已完成 · 结果交付|已完成|结果交付|:white_check_mark:|white_check_mark|🎉.*Done|\[已完成)",
            text,
            re.IGNORECASE,
        )
    )


def is_reconcile_notice(text: str) -> bool:
    """Detect if a Slack message is an auto catch-up reconciliation notice."""
    if not text:
        return False
    return bool(
        re.search(
            r"(?:恢复调度 · Auto Catch-up Reconciled|恢复调度|Auto Catch-up Reconciled|:repeat:)",
            text,
            re.IGNORECASE,
        )
    )


def resolve_hub_bearer_token() -> str:
    """Resolve bearer token to authenticate with local/public Webhook Hub."""
    token = os.getenv("WEBHOOK_HUB_BEARER_TOKEN") or os.getenv("BEARER_TOKEN")
    if token:
        return token
    try:
        from hub.config import load_config
        cfg = load_config()
        if cfg.security.bearer_token:
            return cfg.security.bearer_token
    except Exception:
        pass
    return "86b388ed90b2b226a5322e961a0b816c5da46fdde1893e37760b5d898cf4f7fa"


class SlackReconciler:
    """
    Native Reconciler for unfulfilled offline Slack tasks.
    Coordinates between Slack thread history, SQLite SSOT tasks, and Webhook Hub dispatcher.
    """

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        token: Optional[str] = None,
        hub_url: Optional[str] = None,
        channel_id: Optional[str] = None,
        hub_token: Optional[str] = None,
    ):
        self.db = db or DatabaseManager()
        self.token = token or resolve_slack_bot_token()
        self.hub_url = (hub_url or os.getenv("WEBHOOK_HUB_URL") or DEFAULT_LOCAL_HUB_URL).rstrip("/")
        self.channel_id = channel_id or os.getenv("SLACK_AGENT_CHANNEL_ID") or DEFAULT_CHANNEL_ID
        self.hub_token = hub_token or resolve_hub_bearer_token()
        self.notifier = ThreadNotifier(token=self.token)

    def _slack_api_call(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Perform authenticated call to Slack Web API."""
        if not self.token:
            logger.warning("SLACK_BOT_TOKEN is not configured.")
            return {"ok": False, "error": "no_token"}

        url = f"https://slack.com/api/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "Antigravity-Webhook-Hub-Reconciler/1.0",
        }
        encoded_data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=encoded_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except Exception as e:
            logger.error("Slack API error calling %s: %s", endpoint, e)
            return {"ok": False, "error": str(e)}

    def scan_unfulfilled_threads(
        self,
        channel: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Scan Slack channel history to find threads that received an offline notice
        but have not completed yet, extracting prompts, attachments, and timing.
        """
        target_channel = channel or self.channel_id
        history = self._slack_api_call("conversations.history", {"channel": target_channel, "limit": limit})
        if not history.get("ok"):
            logger.error("Failed to fetch conversations.history: %s", history.get("error"))
            return []

        messages = history.get("messages", [])
        unfulfilled: list[dict[str, Any]] = []

        now_ts = time.time()

        for msg in messages:
            reply_count = msg.get("reply_count", 0)
            ts = msg.get("ts")
            if not ts or reply_count == 0:
                continue

            replies_resp = self._slack_api_call("conversations.replies", {"channel": target_channel, "ts": ts})
            if not replies_resp.get("ok"):
                continue

            thread_msgs = replies_resp.get("messages", [])
            last_offline_ts: Optional[float] = None
            last_completion_ts: Optional[float] = None
            last_reconcile_ts: Optional[float] = None
            user_prompts: list[str] = []
            file_attachments: list[dict[str, Any]] = []
            root_user_id = msg.get("user")

            for m in thread_msgs:
                mtext = m.get("text", "")
                mts_str = m.get("ts", "0")
                try:
                    mts = float(mts_str)
                except ValueError:
                    mts = 0.0

                if is_offline_notice(mtext):
                    if last_offline_ts is None or mts > last_offline_ts:
                        last_offline_ts = mts
                if is_delivery_completion(mtext):
                    if last_completion_ts is None or mts > last_completion_ts:
                        last_completion_ts = mts
                if is_reconcile_notice(mtext):
                    if last_reconcile_ts is None or mts > last_reconcile_ts:
                        last_reconcile_ts = mts

                # Collect user content (skip bots and app integration messages)
                if not m.get("bot_id") and not m.get("app_id") and m.get("user") == root_user_id:
                    if mtext and not is_offline_notice(mtext) and not is_reconcile_notice(mtext):
                        user_prompts.append(mtext)
                    for f in m.get("files", []):
                        file_attachments.append(f)

            is_unfulfilled = bool(
                last_offline_ts is not None
                and (last_completion_ts is None or last_offline_ts > last_completion_ts)
            )

            if is_unfulfilled:
                combined_prompt = " ".join(user_prompts).strip() if user_prompts else str(msg.get("text") or "").strip()
                # If prompt is a generic launcher command, clear it
                if combined_prompt.lower() in ("agentapi new-conversation", "agentapi run"):
                    combined_prompt = ""

                is_in_cooldown = bool(
                    last_reconcile_ts and (now_ts - last_reconcile_ts < DEFAULT_RECONCILE_COOLDOWN_SECONDS)
                )

                unfulfilled.append({
                    "channel_id": target_channel,
                    "thread_ts": ts,
                    "user_id": root_user_id,
                    "prompt": combined_prompt,
                    "files": file_attachments,
                    "last_reconcile_ts": last_reconcile_ts,
                    "is_in_cooldown": is_in_cooldown,
                    "thread_message_count": len(thread_msgs),
                })

        return unfulfilled

    def is_task_already_active_in_db(self, channel: str, thread_ts: str) -> bool:
        """
        SSOT check: query SQLite database to see if a task is already queued/running,
        OR if an active session thread already exists for this channel:thread_ts.
        """
        with self.db._lock:
            cur = self.db._conn.cursor()
            # 1. Check tasks table for queued or running tasks
            query = """
                SELECT task_id, status, created_at FROM tasks
                WHERE status IN ('queued', 'running')
                  AND (
                    action_params_json LIKE ?
                    OR action_params_json LIKE ?
                  )
                ORDER BY created_at DESC LIMIT 1;
            """
            cur.execute(query, (f"%{channel}%{thread_ts}%", f"%{thread_ts}%{channel}%"))
            row = cur.fetchone()
            if row:
                logger.info("Found active task in DB tasks: %s (status=%s)", row["task_id"], row["status"])
                return True

            # 2. Check session_threads table for ongoing active Antigravity session
            try:
                cur.execute(
                    "SELECT conversation_id, status FROM session_threads WHERE channel_id = ? AND root_ts = ? AND status = 'active';",
                    (channel, thread_ts),
                )
                st_row = cur.fetchone()
                if st_row:
                    logger.info("Found active Antigravity session in session_threads for %s:%s", channel, thread_ts)
                    return True
            except Exception as e:
                logger.debug("Error checking session_threads: %s", e)

            return False

    def dispatch_reconciled_task(
        self,
        thread_info: dict[str, Any],
        sync: bool = False,
    ) -> dict[str, Any]:
        """
        Dispatches reconciled task into Webhook Hub and announces status in Slack thread.
        Strictly enforces Unidirectional Data Flow:
        1. Checks cooldown & DB idempotency first (intercepts duplicates before dispatch).
        2. Dispatches / inserts task to Webhook Hub with gateway idempotency headers.
        3. Once accepted, posts the recovery notice to Slack thread with task ID.
        """
        channel = thread_info["channel_id"]
        thread_ts = thread_info["thread_ts"]
        user_id = thread_info.get("user_id", "U_RECONCILE")
        prompt = thread_info.get("prompt", "").strip()
        files = thread_info.get("files", [])

        # Check 1: If thread is in cooldown (reconciled within last 15 min), do NOT dispatch or spam!
        if thread_info.get("is_in_cooldown"):
            logger.info("Skipping dispatch for %s:%s: thread is currently in reconcile cooldown.", channel, thread_ts)
            return {"status": "skipped", "reason": "in_cooldown", "thread_ts": thread_ts}

        # Check 2: Check DB idempotency (active tasks or active session)
        if self.is_task_already_active_in_db(channel, thread_ts):
            logger.info("Skipping dispatch for %s:%s: task is already queued/running or active in DB.", channel, thread_ts)
            return {"status": "skipped", "reason": "already_active_in_db", "thread_ts": thread_ts}

        image_urls = [f.get("url_private") for f in files if f.get("url_private")]
        idemp_key = f"slack_reconcile:{channel}:{thread_ts}"

        payload = {
            "event_id": f"rec_{int(time.time() * 1000)}",
            "timestamp": int(time.time()),
            "source": "slack_reconcile",
            "action": "antigravity.run",
            "action_type": "antigravity",
            "channel": channel,
            "ts": thread_ts,
            "thread_ts": thread_ts,
            "user": user_id,
            "text": prompt,
            "files": files,
            "data": {
                "channel_id": channel,
                "event_ts": thread_ts,
                "thread_ts": thread_ts,
                "user_id": user_id,
                "text": prompt,
                "files": files,
                "image_urls": image_urls,
                "raw_text": prompt,
                "is_followup": False,
            },
        }

        # Step 1: Dispatch to Webhook Hub HTTP endpoint or DB fallback
        url = f"{self.hub_url}/webhook/antigravity?sync={'true' if sync else 'false'}"
        headers = {
            "Authorization": f"Bearer {self.hub_token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Antigravity-Webhook-Hub-Reconciler/1.0",
            "X-Hub-Event-Id": idemp_key,
            "X-Idempotency-Key": idemp_key,
        }
        encoded = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=encoded, headers=headers, method="POST")

        status_code = 0
        resp_data: dict[str, Any] = {}
        assigned_task_id = ""

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status_code = resp.status
                resp_data = json.loads(resp.read().decode("utf-8"))
                assigned_task_id = str(resp_data.get("task_id") or "")
        except Exception as e:
            logger.warning("HTTP dispatch to %s failed (%s), attempting in-process DB fallback...", url, e)
            # In-process DB fallback: directly insert task into SQLite SSOT
            try:
                task_id = f"tsk_{int(time.time() * 1000)}"
                event_id = payload["event_id"]
                event_record = {
                    "event_id": event_id,
                    "source": "slack_reconcile",
                    "idempotency_key": f"slack_reconcile:{channel}:{thread_ts}",
                    "payload_hash": f"hash_{event_id}",
                    "headers_json": "{}",
                    "raw_payload": json.dumps(payload),
                    "method": "INTERNAL",
                    "path": "/webhook/antigravity",
                    "remote_addr": "127.0.0.1",
                    "status": "received",
                }
                self.db.insert_webhook_event(event_record)
                task_record = {
                    "task_id": task_id,
                    "event_id": event_id,
                    "source": "slack_reconcile",
                    "action_type": "antigravity",
                    "command": "agentapi new-conversation",
                    "target_action": "agentapi new-conversation",
                    "action_params_json": json.dumps(payload),
                    "status": "queued",
                    "priority": 1,
                    "timeout_seconds": 600,
                    "retry_count": 0,
                    "max_retries": 3,
                }
                self.db.insert_task(task_record)
                status_code = 200
                assigned_task_id = task_id
                resp_data = {"status": "queued", "task_id": task_id}
            except Exception as db_err:
                logger.error("In-process DB fallback failed: %s", db_err)
                return {"status": "error", "error": str(db_err), "thread_ts": thread_ts}

        # Step 2: Unidirectional Data Flow -> Post Slack notification ONLY if dispatch succeeded
        # and not in recent cooldown spam
        if status_code in (200, 201, 202) and not thread_info.get("is_in_cooldown"):
            if prompt:
                prompt_display = prompt[:100] + ("..." if len(prompt) > 100 else "")
            elif files:
                first_name = files[0].get("name") or "image"
                prompt_display = f"[图片素材: {first_name}] ({len(files)} 个附件)"
            else:
                prompt_display = "[离线待处理任务]"

            tid_str = f"`{assigned_task_id}`" if assigned_task_id else "`已受理`"
            notice_text = (
                ":repeat: *[恢复调度 · Auto Catch-up Reconciled]*\n"
                "• *状态*: 检测到本机已恢复在线，已自动从 Slack 补发并重新调度该离线任务。\n"
                f"• *任务ID*: {tid_str}\n"
                f"• *任务内容*: {prompt_display}"
            )
            self._slack_api_call("chat.postMessage", {
                "channel": channel,
                "thread_ts": thread_ts,
                "text": notice_text,
            })

        return {
            "status": "success" if status_code in (200, 201, 202) else "failed",
            "status_code": status_code,
            "task_id": assigned_task_id,
            "thread_ts": thread_ts,
            "response": resp_data,
        }

    def run_catchup(
        self,
        channel: Optional[str] = None,
        limit: int = 50,
        dry_run: bool = False,
        execute: bool = True,
    ) -> dict[str, Any]:
        """
        Main entry point to reconcile all offline tasks in the specified channel.
        """
        target_channel = channel or self.channel_id
        unfulfilled = self.scan_unfulfilled_threads(channel=target_channel, limit=limit)

        summary: dict[str, Any] = {
            "channel": target_channel,
            "scanned_limit": limit,
            "unfulfilled_count": len(unfulfilled),
            "dry_run": dry_run,
            "dispatched": [],
            "skipped": [],
        }

        if not unfulfilled:
            return summary

        for item in unfulfilled:
            tts = item["thread_ts"]
            if dry_run or not execute:
                summary["skipped"].append({
                    "thread_ts": tts,
                    "reason": "dry_run",
                    "prompt": item.get("prompt"),
                    "files_count": len(item.get("files", [])),
                })
            else:
                res = self.dispatch_reconciled_task(item)
                if res.get("status") == "success":
                    summary["dispatched"].append(res)
                else:
                    summary["skipped"].append(res)

        return summary
