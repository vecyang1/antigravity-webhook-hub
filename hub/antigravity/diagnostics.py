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

        # 3. Webhook URL (or local path starting with /)
        if clean_target.startswith("http://") or clean_target.startswith("https://") or clean_target.startswith("/"):
            return self._explain_url(clean_target)

        # 4. Email / Spark Target (spark:724913, spark:reply:724913, email:..., or email address)
        if clean_target.startswith("spark:") or clean_target.startswith("email:") or ("@" in clean_target and ":" not in clean_target):
            return self._explain_email(clean_target)

        # 5. Slack Thread (C0C1B86AMCN:178999... or raw ts 178999...)
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
        # Handle relative local routes like /webhook/contact-review
        full_url = url
        if url.startswith("/"):
            full_url = f"http://127.0.0.1:9423{url}"

        parsed = urlparse(full_url)
        host = (parsed.netloc or "").lower()
        path = parsed.path
        query = parsed.query

        # Default classification
        system_owner = "Unknown External Endpoint"
        owning_skill = "none"
        operator_cli = "none"
        boundary = "External Network"
        auth_type = "none"
        is_known_route = False
        target_flow = "Unknown Route"
        details = "Unregistered external URL or endpoint."
        verdict = "UNKNOWN_ROUTE"

        # 1. Cloud n8n Automation Engine (n.worldinspirelab.com)
        if "n.worldinspirelab.com" in host or (host and "n8n" in host):
            system_owner = "n8n Automation Engine (Cloud VPS @ 62.171.132.182)"
            owning_skill = "n8n-automation"
            operator_cli = "python3 ~/.gemini/antigravity/skills/n8n-automation/scripts/n8n_workflow_ops.py"
            boundary = "Cloud Edge Automation / Webhook Router"

            n8n_routes = {
                "/webhook/carradiocodes-notifications": {
                    "name": "Car Radio Codes Unified Notifications",
                    "workflow_id": "carradiocodes-notifications",
                    "purpose": "Dispatches multi-channel alerts (Slack #notification_radiocode, Mikee & Vec email alerts, Supabase ContactMessage persistence).",
                    "auth": "none (public webhook trigger)",
                },
                "/webhook/heartbeat": {
                    "name": "n8n Cloud Webhook Heartbeat",
                    "workflow_id": "heartbeat",
                    "purpose": "Periodic liveness & execution cadence pulse for n8n cloud instance.",
                    "auth": "none (public webhook trigger)",
                },
                "/webhook/growth-hub-intent-align": {
                    "name": "Notion Growth Hub Intent Alignment",
                    "workflow_id": "RyIoWd58HM1JX2oM",
                    "purpose": "Zero-friction intent alignment for Notion Backlog DB (promotes Assignee: Agent to Status: In Progress).",
                    "auth": "none / query token",
                },
                "/webhook/fluentcommunity-member-joined": {
                    "name": "FluentCommunity Member Intake Router",
                    "workflow_id": "fluentcommunity-member-joined",
                    "purpose": "Batched, debounced member join event fan-out to Slack #notification_community.",
                    "auth": "none (webhook trigger)",
                },
                "/webhook/universal-email-gateway": {
                    "name": "Universal Email Ingress & Forwarding Engine",
                    "workflow_id": "23fVikAaauBgnJwO",
                    "purpose": "Deterministic regex 2FA/OTP code extraction and branded Mailbux SMTP forwarding with gated Slack alert.",
                    "auth": "none (webhook trigger)",
                },
                "/webhook/xinchaovi-chatwoot-lead": {
                    "name": "Chatwoot Live Chat Lead Capture Hub",
                    "workflow_id": "RiuBlt4Enf7X5n1V",
                    "purpose": "Universal multi-site live chat inquiry dispatch to WordPress FluentCRM & Slack #notification_lead.",
                    "auth": "none (webhook trigger)",
                },
                "/webhook/glintmuse-subscriber": {
                    "name": "GlintMuse Subscriber Notification",
                    "workflow_id": "f2Tbu87pDQLVHDTR",
                    "purpose": "E-Commerce subscriber capture & multi-tenant transactional email routing.",
                    "auth": "none (webhook trigger)",
                },
                "/webhook/glintmuse-order-paid": {
                    "name": "GlintMuse Paid Customer Order Notification",
                    "workflow_id": "f2Tbu87pDQLVHDTR",
                    "purpose": "E-Commerce order fulfillment & transactional customer alerts.",
                    "auth": "none (webhook trigger)",
                },
                "/webhook/glintmuse-store-alert": {
                    "name": "E-Commerce Store Health & Order Flow Guard",
                    "workflow_id": "f2Tbu87pDQLVHDTR",
                    "purpose": "Autonomous checkout anomalies & unbuyable product audit alerts.",
                    "auth": "none (webhook trigger)",
                },
                "/webhook/xinchaovi-growth-report-remind": {
                    "name": "XinChaoVi Growth Report Ready Reminder",
                    "workflow_id": "OsgxJnpV77TrU6RA",
                    "purpose": "Dual-ingress monthly growth audit alert with 1-to-1 isolated SMTP fanout.",
                    "auth": "none (webhook trigger)",
                },
            }

            matched = n8n_routes.get(path)
            if matched:
                is_known_route = True
                verdict = "ROUTE_RECOGNIZED"
                target_flow = matched["name"]
                details = matched["purpose"]
                auth_type = matched["auth"]
            elif path.startswith("/webhook/"):
                is_known_route = True
                verdict = "N8N_WEBHOOK_ROUTE"
                target_flow = f"n8n Cloud Webhook ({path})"
                details = "Registered n8n webhook endpoint on n.worldinspirelab.com."
                auth_type = "none (public webhook trigger)"
            elif path.startswith("/api/v1/"):
                is_known_route = True
                verdict = "N8N_REST_API"
                target_flow = f"n8n Public REST API ({path})"
                details = "Official n8n management API for workflows, executions, and credentials."
                auth_type = "x_n8n_api_key (Header X-N8N-API-KEY)"
            elif path in ("/healthz", "/"):
                is_known_route = True
                verdict = "N8N_HEALTH_ENDPOINT"
                target_flow = "n8n Health Probe (/healthz)"
                details = "Monitored by central Uptime Kuma #44 ('n8n Automation Engine'). Returns HTTP 200 {'status': 'ok'}."
                auth_type = "none"
            else:
                verdict = "KNOWN_ESTATE_HOST_UNREGISTERED_PATH"
                details = f"Endpoint on {host} but path {path} is not in registered route catalog."

        # 2. Cloudflare Tunnel Ingress to Local Gateway (webhook.worldinspirelab.com)
        elif "webhook.worldinspirelab.com" in host:
            system_owner = "Antigravity Webhook Hub (Local Gateway via Cloudflare Tunnel)"
            owning_skill = "webhook-hub"
            operator_cli = "bin/webhook-hub"
            boundary = "Cloudflare Tunnel -> macOS Local Gateway (127.0.0.1:9423)"

            tunnel_routes = {
                "/webhook/contact-review": {
                    "name": "Notion CRM Contact Review Engine",
                    "purpose": "Inbound contact intake from n8n workflow 3J5doqEyxA7lT1OO. Evaluates Notion profiles, reconciles photos/notes, verifies SSOT, delivers Slack verdict.",
                    "auth": "bearer_token (ANTIGRAVITY_WEBHOOK_BEARER_TOKEN)",
                },
                "/webhook/antigravity": {
                    "name": "Slack-to-Antigravity Mobile Intake",
                    "purpose": "Inbound user message from n8n subflow 5gA4zVOl6MaGE7qq. Writes to SQLite SSOT and dispatches agentapi new-conversation / thread follow-up.",
                    "auth": "bearer_token (ANTIGRAVITY_WEBHOOK_BEARER_TOKEN)",
                },
                "/webhook": {
                    "name": "External Webhook Gateway Ingress",
                    "purpose": "Core signed webhook ingress with HMAC SHA-256 verification and replay protection.",
                    "auth": "hmac_sha256 (WEBHOOK_SECRET)",
                },
                "/api/webhook/uptime-kuma": {
                    "name": "Uptime Kuma Webhook Heartbeat Ingress",
                    "purpose": "Ingests push incident and heartbeat alerts from Uptime Kuma monitoring.",
                    "auth": "token_query_or_header (UPTIME_KUMA_WEBHOOK_TOKEN)",
                },
                "/api/diagnose": {
                    "name": "Diagnostic-First Remote Explainer API",
                    "purpose": "Remote inspection endpoint for tasks, threads, events, and URLs with optional reconciliation.",
                    "auth": "none / read-only",
                },
                "/dashboard": {
                    "name": "Observable Activities Web Dashboard",
                    "purpose": "Real-time HTML monitoring dashboard with live SSE task stream.",
                    "auth": "basic_auth or session cookie",
                },
                "/ui": {
                    "name": "Observable Activities Web Dashboard (Alias)",
                    "purpose": "Alias for /dashboard.",
                    "auth": "basic_auth or session cookie",
                },
            }

            matched = tunnel_routes.get(path)
            if matched:
                is_known_route = True
                verdict = "ROUTE_RECOGNIZED"
                target_flow = matched["name"]
                details = matched["purpose"]
                auth_type = matched["auth"]
            else:
                verdict = "KNOWN_ESTATE_HOST_UNREGISTERED_PATH"
                details = f"Tunnel host webhook.worldinspirelab.com reached, but path {path} is not mapped."

        # 3. Local Webhook Hub Gateway (127.0.0.1:9423 / localhost:9423)
        elif not host or host in ("127.0.0.1:9423", "localhost:9423", "127.0.0.1", "localhost"):
            system_owner = "Antigravity Webhook Hub (macOS Local Gateway)"
            owning_skill = "webhook-hub"
            operator_cli = "bin/webhook-hub"
            boundary = "macOS Localhost Loopback (127.0.0.1:9423)"

            local_routes = {
                "/webhook": ("External Signed Webhook", "hmac_sha256"),
                "/webhook/antigravity": ("Slack-to-Antigravity Mobile Intake", "bearer_token"),
                "/webhook/contact-review": ("Notion CRM Contact Review Engine", "bearer_token"),
                "/api/webhook/uptime-kuma": ("Uptime Kuma Webhook", "token_query_or_header"),
                "/api/diagnose": ("Diagnostic-First Explainer API", "none"),
                "/api/diagnose/slack": ("Slack Thread Diagnostic API", "none"),
                "/healthz": ("Health & Liveness Probe", "none"),
                "/health": ("Health & Liveness Probe (Alias)", "none"),
                "/ready": ("Readiness Probe", "none"),
                "/metrics": ("Metrics & Telemetry Probe", "none"),
                "/tasks": ("Tasks SSOT Query API", "bearer_token"),
                "/schedules": ("Schedules SSOT Query API", "bearer_token"),
                "/dashboard": ("Observable Activities Web Dashboard", "basic_auth"),
                "/ui": ("Observable Activities Web Dashboard (Alias)", "basic_auth"),
                "/antigravity/status": ("Antigravity Language Server Status", "none"),
                "/antigravity/watchdog": ("Antigravity 24/7 Watchdog Probe", "none"),
                "/antigravity/pull-up": ("Antigravity Session Pull-Up Trigger", "none"),
                "/antigravity/quota": ("Antigravity Quota Sentinel State", "none"),
                "/antigravity/warmup": ("Autonomous Antigravity Quota Warmup", "none"),
            }

            if path in local_routes:
                is_known_route = True
                verdict = "ROUTE_RECOGNIZED"
                name, auth = local_routes[path]
                target_flow = name
                auth_type = auth
                details = f"Local Webhook Hub route handling {name} on port 9423."
            else:
                verdict = "UNKNOWN_ROUTE"
                details = f"Local port 9423 path {path} not recognized."

        # 4. Central Uptime Kuma Monitoring Estate
        elif "monitor.worldinspirelab.com" in host:
            system_owner = "Central Uptime Kuma Monitoring Estate"
            owning_skill = "uptime-kuma-ops"
            operator_cli = "none"
            boundary = "Cloud Monitoring Engine"
            is_known_route = True
            verdict = "ESTATE_MONITOR_ROUTE"
            target_flow = "Uptime Kuma Monitoring Dashboard"
            details = "Monitors 90+ production endpoints, including n8n Automation Engine (#44) and Antigravity Webhook Hub (#93)."
            auth_type = "session_auth"

        # 5. Estate Chatwoot Omnichannel
        elif "cc.worldinspirelab.com" in host:
            system_owner = "Chatwoot Omnichannel Customer Support"
            owning_skill = "chatwoot-ops"
            operator_cli = "none"
            boundary = "Cloud Live Chat Engine"
            is_known_route = True
            verdict = "ESTATE_CHAT_ROUTE"
            target_flow = "Chatwoot Ingress / Webhook Fan-out"
            details = "Customer inquiries fanned out to n8n webhook /webhook/xinchaovi-chatwoot-lead."
            auth_type = "api_access_token"

        return {
            "target_type": "url",
            "url": url,
            "host": host,
            "path": path,
            "query": query,
            "system_owner": system_owner,
            "owning_skill": owning_skill,
            "operator_cli": operator_cli,
            "boundary": boundary,
            "is_known_route": is_known_route,
            "auth_type": auth_type,
            "target_flow": target_flow,
            "details": details,
            "verdict": verdict,
        }

    def _explain_email(self, target: str) -> dict[str, Any]:
        """Explain email inquiry thread, Spark database state, or email routing."""
        import sqlite3
        clean = target.strip()
        spark_db_path = os.path.expanduser("~/Library/Application Support/Spark Mail/core-data/messages.sqlite")

        # Check if target is a spark PK or inquiry (e.g. spark:724913 or numeric)
        spark_pk = None
        if clean.startswith("spark:"):
            parts = clean.split(":", 1)[1]
            if parts.startswith("reply:"):
                parts = parts.split(":", 1)[1]
            if parts.isdigit():
                spark_pk = int(parts)
        elif clean.isdigit():
            spark_pk = int(clean)

        if spark_pk is not None:
            # Query Spark CoreData SQLite in read-only fast path (<3ms)
            msg_data = None
            replies = []
            if os.path.exists(spark_db_path):
                try:
                    conn = sqlite3.connect(f"file:{spark_db_path}?mode=ro", uri=True)
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT pk, subject, conversationPk, messageFrom, receivedDate, shortBody FROM messages WHERE pk = ?",
                        (spark_pk,),
                    )
                    row = cur.fetchone()
                    if row:
                        conv_pk = row[2]
                        msg_data = {
                            "pk": row[0],
                            "subject": row[1],
                            "conversation_pk": conv_pk,
                            "from": row[3],
                            "short_body": (row[5] or "")[:200],
                        }
                        if conv_pk:
                            cur.execute(
                                "SELECT pk, subject, messageFrom, receivedDate, shortBody FROM messages WHERE conversationPk = ? AND pk != ? ORDER BY receivedDate DESC",
                                (conv_pk, spark_pk),
                            )
                            reply_rows = cur.fetchall()
                            for r in reply_rows:
                                replies.append({
                                    "pk": r[0],
                                    "subject": r[1],
                                    "from": r[2],
                                    "short_body": (r[4] or "")[:200],
                                })
                    conn.close()
                except Exception as e:
                    logger.warning("Failed to query Spark database: %s", e)

            is_anker = (spark_pk == 724913 or (msg_data and "anker" in (msg_data.get("subject") or "").lower()))
            has_replies = len(replies) > 0

            verdict = "EMAIL_REPLIED" if has_replies else "EMAIL_PENDING_REPLY"
            if not msg_data:
                verdict = "SPARK_MESSAGE_NOT_FOUND"

            return {
                "target_type": "email_spark",
                "target": clean,
                "spark_pk": spark_pk,
                "system_owner": "Spark Desktop & Apple Mail Local Engine",
                "owning_skill": "use-spark & spark-otp",
                "operator_cli": "python3 ~/.gemini/antigravity/skills/use-spark/scripts/check_mail_app.py",
                "is_anker_warranty_inquiry": is_anker,
                "found_in_spark_db": bool(msg_data),
                "message": msg_data,
                "replies_count": len(replies),
                "replies": replies,
                "verdict": verdict,
                "details": f"{'Anker warranty inquiry' if is_anker else 'Spark email thread'} #{spark_pk}: {len(replies)} reply(ies) found." if msg_data else f"Message #{spark_pk} not found in Spark DB.",
            }

        # Handle email address or domain routing (e.g. email:user@domain.com or user@domain.com)
        email_addr = clean.replace("email:", "").strip()
        domain = email_addr.split("@")[-1].lower() if "@" in email_addr else ""

        domain_routes = {
            "worldinspirelab.com": ("World Inspire Lab Operations & AI Infrastructure", "C092WUZ4AJV (#notification_wi)"),
            "xinchaovi.com": ("XinChaoVi EdTech Platform & Student Inquiries", "C0915LNJ3QR (#notification_lead)"),
            "glintmuse.com": ("GlintMuse Brand E-Commerce & Retail", "C0915LNJ3QR (#notification_lead)"),
            "carradiocodes.co.uk": ("Car Radio Codes Automotive Security", "C0C0X9MAPF0 (#notification_radiocode)"),
        }

        matched_brand, target_channel = domain_routes.get(domain, ("Generic External Email", "none"))

        return {
            "target_type": "email_routing",
            "target": clean,
            "email_address": email_addr,
            "domain": domain,
            "brand": matched_brand,
            "target_slack_channel": target_channel,
            "system_owner": "Mailbux SMTP & Spark Email Infrastructure",
            "owning_skill": "email-management / use-spark",
            "operator_cli": "spark emails --filter 'from:" + email_addr + "'",
            "verdict": "DOMAIN_ROUTED" if domain in domain_routes else "EXTERNAL_DOMAIN",
            "details": f"Email address mapped to {matched_brand} (routes to {target_channel}).",
        }


def format_diagnostic_report(diag: dict[str, Any]) -> str:
    """Format diagnostic result into clean human-readable terminal output."""
    target_type = diag.get("target_type", "unknown")
    verdict = diag.get("verdict", "UNKNOWN")

    lines = []
    lines.append("=" * 64)
    lines.append("🔍 ANTIGRAVITY WEBHOOK HUB — DIAGNOSTIC & EXPLAIN REPORT")
    lines.append(f"   Target:  {diag.get('target') or diag.get('task_id') or diag.get('event_id') or diag.get('url')}")
    lines.append(f"   Type:    {target_type.upper()}")
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
        lines.append("\n🌐 URL ROUTING & ARCHITECTURE ANALYSIS")
        lines.append(f"  • Target URL:     {diag.get('url')}")
        lines.append(f"  • Host:           {diag.get('host')}")
        lines.append(f"  • Path:           {diag.get('path')}")
        lines.append(f"  • System Owner:   {diag.get('system_owner')}")
        lines.append(f"  • Owning Skill:   {diag.get('owning_skill')}")
        lines.append(f"  • Operator CLI:   {diag.get('operator_cli')}")
        lines.append(f"  • Boundary:       {diag.get('boundary')}")
        lines.append(f"  • Auth Scheme:    {diag.get('auth_type')}")
        lines.append(f"  • Target Flow:    {diag.get('target_flow')}")
        lines.append(f"  • Route Recognized: {'✅ Yes' if diag.get('is_known_route') else '❌ No'}")
        lines.append(f"  • Details:        {diag.get('details')}")

    elif target_type == "email_spark":
        lines.append("\n📧 SPARK EMAIL INQUIRY ANALYSIS")
        lines.append(f"  • Target:         {diag.get('target')}")
        lines.append(f"  • Spark PK:       {diag.get('spark_pk')}")
        lines.append(f"  • System Owner:   {diag.get('system_owner')}")
        lines.append(f"  • Owning Skill:   {diag.get('owning_skill')}")
        lines.append(f"  • Operator CLI:   {diag.get('operator_cli')}")
        lines.append(f"  • Anker Inquiry:  {'✅ Yes' if diag.get('is_anker_warranty_inquiry') else '❌ No'}")
        lines.append(f"  • Found in DB:    {'✅ Yes' if diag.get('found_in_spark_db') else '❌ No'}")
        msg = diag.get("message")
        if msg:
            lines.append(f"  • Subject:        {msg.get('subject')}")
            lines.append(f"  • From:           {msg.get('from')}")
            lines.append(f"  • Replies Count:  {diag.get('replies_count')}")
            if diag.get("replies"):
                lines.append("  • Latest Reply:")
                rep = diag.get("replies")[0]
                lines.append(f"    - From:    {rep.get('from')}")
                lines.append(f"    - Subject: {rep.get('subject')}")
                lines.append(f"    - Snippet: {rep.get('short_body')[:100]}...")
        lines.append(f"  • Details:        {diag.get('details')}")

    elif target_type == "email_routing":
        lines.append("\n📧 DOMAIN & EMAIL ROUTING ANALYSIS")
        lines.append(f"  • Address:        {diag.get('email_address')}")
        lines.append(f"  • Domain:         {diag.get('domain')}")
        lines.append(f"  • Matched Brand:  {diag.get('brand')}")
        lines.append(f"  • Target Channel: {diag.get('target_slack_channel')}")
        lines.append(f"  • System Owner:   {diag.get('system_owner')}")
        lines.append(f"  • Operator CLI:   {diag.get('operator_cli')}")
        lines.append(f"  • Details:        {diag.get('details')}")

    lines.append("=" * 64)
    return "\n".join(lines)

