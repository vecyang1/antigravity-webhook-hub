"""
Antigravity Webhook Hub — Unified Diagnostic & Explain Engine (对账器优先)
Provides deep inspection and explanation for tasks, Slack conversation threads, events,
and inbound URLs, verifying real execution logic, SSOT state, and rule compliance.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from hub.db import DatabaseManager
from hub.antigravity.reconciler import SlackReconciler, DEFAULT_CHANNEL_ID

logger = logging.getLogger("hub.antigravity.diagnostics")


class DiagnosticInspector:
    """
    Diagnostic-First inspector that traces and explains real execution flow,
    rules hit, SSOT persistence, and Slack thread state.
    """

    def __init__(self, db: Optional[DatabaseManager] = None, reconciler: Optional[SlackReconciler] = None):
        self.db = db or DatabaseManager()
        self.reconciler = reconciler or SlackReconciler(db=self.db)

    def explain(
        self,
        target: str,
        reconcile: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Explain and diagnose a target (task_id, Slack thread_ts / channel:ts, event_id, or URL).
        """
        clean_target = (target or "").strip()
        if not clean_target:
            return {"error": "Target must not be empty", "verdict": "INVALID_INPUT"}

        # 1. Task ID
        if clean_target.startswith("tsk_"):
            return self._explain_task(clean_target, reconcile=reconcile, force=force)

        # 2. Event ID
        if clean_target.startswith("evt_"):
            return self._explain_event(clean_target)

        # 3. Webhook URL
        if clean_target.startswith("http://") or clean_target.startswith("https://"):
            return self._explain_url(clean_target)

        # 4. Slack Thread (C0C1B86AMCN:178999... or raw ts 178999...)
        return self._explain_slack(clean_target, reconcile=reconcile, force=force)

    def _explain_task(self, task_id: str, reconcile: bool = False, force: bool = False) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        if not task:
            return {
                "target_type": "task",
                "task_id": task_id,
                "found_in_db": False,
                "verdict": "TASK_NOT_FOUND",
                "message": f"Task {task_id} was not found in SQLite SSOT tasks table.",
            }

        event_id = task.get("event_id")
        event = self.db.get_webhook_event(event_id) if event_id else None
        logs = self.db.get_execution_logs(task_id)

        # Parse action parameters
        channel_id = ""
        thread_ts = ""
        user_prompt = ""
        action_params = {}
        if task.get("action_params_json"):
            try:
                action_params = json.loads(task["action_params_json"])
                channel_id = action_params.get("channel") or action_params.get("channel_id") or ""
                thread_ts = action_params.get("thread_ts") or action_params.get("ts") or ""
                user_prompt = action_params.get("text") or action_params.get("prompt") or ""
            except Exception:
                pass

        slack_diag = None
        if channel_id and thread_ts:
            slack_diag = self.reconciler.diagnose_thread(channel=channel_id, thread_ts=thread_ts)
            if reconcile and slack_diag.get("is_unfulfilled"):
                reconcile_res = self.reconciler.dispatch_reconciled_task(slack_diag, force=force)
                slack_diag["reconciled"] = True
                slack_diag["reconcile_result"] = reconcile_res

        # Determine verdict
        task_status = task.get("status")
        verdict = "TASK_" + task_status.upper()
        if slack_diag:
            verdict = f"{task_status.upper()}_{slack_diag.get('verdict', 'UNKNOWN')}"

        return {
            "target_type": "task",
            "task_id": task_id,
            "found_in_db": True,
            "verdict": verdict,
            "task": task,
            "event": event,
            "log_lines_count": len(logs),
            "recent_logs": [l.get("line") for l in logs[-5:]],
            "slack_diagnosis": slack_diag,
        }

    def _explain_event(self, event_id: str) -> dict[str, Any]:
        event = self.db.get_webhook_event(event_id)
        if not event:
            return {
                "target_type": "event",
                "event_id": event_id,
                "found_in_db": False,
                "verdict": "EVENT_NOT_FOUND",
            }

        # Query all tasks for this event
        with self.db._lock:
            cur = self.db._conn.cursor()
            cur.execute("SELECT * FROM tasks WHERE event_id = ? ORDER BY created_at ASC;", (event_id,))
            tasks = [dict(r) for r in cur.fetchall()]

        return {
            "target_type": "event",
            "event_id": event_id,
            "found_in_db": True,
            "verdict": f"EVENT_{event.get('status', 'UNKNOWN').upper()}",
            "event": event,
            "tasks_count": len(tasks),
            "tasks": tasks,
        }

    def _explain_slack(self, target: str, reconcile: bool = False, force: bool = False) -> dict[str, Any]:
        channel = DEFAULT_CHANNEL_ID
        thread_ts = target

        if ":" in target:
            parts = target.split(":", 1)
            channel = parts[0]
            thread_ts = parts[1]

        diag = self.reconciler.diagnose_thread(channel=channel, thread_ts=thread_ts)
        diag["target_type"] = "slack_thread"

        if reconcile and diag.get("is_unfulfilled"):
            reconcile_res = self.reconciler.dispatch_reconciled_task(diag, force=force)
            diag["reconciled"] = True
            diag["reconcile_result"] = reconcile_res
            # Re-read diagnostic state after reconciliation
            fresh_diag = self.reconciler.diagnose_thread(channel=channel, thread_ts=thread_ts)
            diag["verdict_post_reconcile"] = fresh_diag.get("verdict")

        return diag

    def _explain_url(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        path = parsed.path
        is_known_route = path in (
            "/webhook",
            "/webhook/antigravity",
            "/api/webhook/uptime-kuma",
            "/healthz",
            "/ready",
            "/metrics",
            "/tasks",
            "/schedules",
        )

        auth_type = "none"
        if path == "/webhook":
            auth_type = "hmac_sha256"
        elif path in ("/webhook/antigravity", "/tasks", "/schedules"):
            auth_type = "bearer_token"
        elif path == "/api/webhook/uptime-kuma":
            auth_type = "token_query_or_header"

        return {
            "target_type": "url",
            "url": url,
            "host": parsed.netloc,
            "path": path,
            "is_known_route": is_known_route,
            "auth_type": auth_type,
            "verdict": "ROUTE_RECOGNIZED" if is_known_route else "UNKNOWN_ROUTE",
        }


def format_diagnostic_report(diag: dict[str, Any]) -> str:
    """Format diagnostic result into clean human-readable terminal output."""
    target_type = diag.get("target_type", "unknown")
    verdict = diag.get("verdict", "UNKNOWN")

    lines = []
    lines.append("=" * 64)
    lines.append(f"🔍 ANTIGRAVITY WEBHOOK HUB — DIAGNOSTIC & EXPLAIN REPORT")
    lines.append(f"   Target: {diag.get('target') or diag.get('task_id') or diag.get('event_id') or diag.get('url')}")
    lines.append(f"   Type:   {target_type.upper()}")
    lines.append(f"   Verdict: {verdict}")
    lines.append("=" * 64)

    if target_type == "slack_thread":
        lines.append("\n💬 SLACK CONVERSATION THREAD")
        lines.append(f"  • Channel:        {diag.get('channel_id')}")
        lines.append(f"  • Thread Ts:      {diag.get('thread_ts')}")
        lines.append(f"  • Root User:      {diag.get('user_id') or 'unknown'}")
        lines.append(f"  • Prompt Snippet: {diag.get('prompt')[:80] if diag.get('prompt') else '(empty)'}")
        lines.append(f"  • Attachments:    {len(diag.get('files', []))} file(s)")
        lines.append(f"  • In Cooldown:    {diag.get('is_in_cooldown')}")
        lines.append(f"  • Unfulfilled:    {diag.get('is_unfulfilled')}")
        lines.append(f"  • Action Advice:  {diag.get('recommended_action').upper()}")

        ms = diag.get("milestones", {})
        lines.append("\n🚩 DETECTED MILESTONES")
        lines.append(f"  • [已采集] Collected:   {'✅' if ms.get('has_collection') else '❌'}")
        lines.append(f"  • [已完成] Delivered:   {'✅' if ms.get('has_completion') else '❌'}")
        lines.append(f"  • [离线提示] Offline:   {'⚠️' if ms.get('has_offline') else '❌'}")
        lines.append(f"  • [恢复调度] Reconciled:{'🔄' if ms.get('has_reconcile') else '❌'}")
        lines.append(f"  • [执行异常] Failure:   {'❌' if ms.get('has_failure') else '—'}")

        db_state = diag.get("db_state", {})
        lines.append("\n📊 SQLITE SSOT STATE")
        if db_state.get("task_id"):
            lines.append(f"  • Task ID:        {db_state.get('task_id')}")
            lines.append(f"  • Task Status:    {db_state.get('status')}")
            lines.append(f"  • Error Message:  {db_state.get('error_message') or 'none'}")
            lines.append(f"  • Created At:     {db_state.get('created_at')}")
            lines.append(f"  • Completed At:   {db_state.get('completed_at') or 'in-flight/pending'}")
            lines.append(f"  • Session Thread: {db_state.get('session_thread_status') or 'none'}")
        else:
            lines.append("  • Task in DB:     ❌ No matching task found in database")

        timeline = diag.get("timeline", [])
        if timeline:
            lines.append("\n⏱️ THREAD TIMELINE")
            for item in timeline:
                lines.append(f"  [{item.get('ts')}] {item.get('user')[:12]} ({item.get('milestone')}): {item.get('text_preview')}")

        if diag.get("reconciled"):
            lines.append("\n🚀 RECONCILIATION RESULT")
            rec_res = diag.get("reconcile_result", {})
            lines.append(f"  • Status:      {rec_res.get('status')}")
            lines.append(f"  • Task ID:     {rec_res.get('task_id') or 'none'}")
            lines.append(f"  • Post-Verdict:{diag.get('verdict_post_reconcile') or 'updated'}")

    elif target_type == "task":
        task = diag.get("task", {})
        lines.append("\n📊 TASK METADATA")
        lines.append(f"  • Task ID:        {task.get('task_id')}")
        lines.append(f"  • Action Type:    {task.get('action_type')}")
        lines.append(f"  • Status:         {task.get('status')}")
        lines.append(f"  • Exit Code:      {task.get('exit_code')}")
        lines.append(f"  • Error:          {task.get('error_message') or 'none'}")
        lines.append(f"  • Created:        {task.get('created_at')}")
        lines.append(f"  • Completed:      {task.get('completed_at')}")

        slack_diag = diag.get("slack_diagnosis")
        if slack_diag:
            lines.append("\n💬 ASSOCIATED SLACK THREAD")
            lines.append(f"  • Thread:         {slack_diag.get('channel_id')}:{slack_diag.get('thread_ts')}")
            lines.append(f"  • Thread Verdict: {slack_diag.get('verdict')}")
            lines.append(f"  • Unfulfilled:    {slack_diag.get('is_unfulfilled')}")

        recent_logs = diag.get("recent_logs", [])
        if recent_logs:
            lines.append("\n📜 RECENT EXECUTION LOGS")
            for log in recent_logs:
                lines.append(f"  {log}")

    elif target_type == "url":
        lines.append("\n🌐 URL ROUTING ANALYSIS")
        lines.append(f"  • Host:           {diag.get('host')}")
        lines.append(f"  • Path:           {diag.get('path')}")
        lines.append(f"  • Recognized:     {diag.get('is_known_route')}")
        lines.append(f"  • Auth Scheme:    {diag.get('auth_type')}")

    lines.append("=" * 64)
    return "\n".join(lines)
