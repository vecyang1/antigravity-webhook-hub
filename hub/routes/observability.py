"""
Antigravity Webhook Hub — Observability & Query Endpoints
Implements /healthz, /ready, /metrics (Prometheus text & JSON),
GET /tasks (list & filter), and GET /tasks/{task_id} (detail & logs).
Guarantees <30MB RSS memory budget and strict 0% idle CPU on macOS.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import time
from typing import Any, Optional

from hub.config import AppConfig
from hub.memory import (
    apply_memory_pressure_relief,
    get_memory_budget_mb,
    get_memory_rss_bytes,
    get_memory_rss_mb,
)
from hub.models import HTTPRequest, HTTPResponse
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.observability")

# Backward-compatibility alias
_apply_darwin_pressure_relief = apply_memory_pressure_relief


def register_observability_routes(
    server: AsyncHTTPServer,
    config: AppConfig,
    db: Optional[Any] = None,
    broker: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
) -> None:
    """Register all observability and task inspection endpoints."""

    async def handle_healthz(req: HTTPRequest) -> HTTPResponse:
        """GET /healthz: Liveness & health probe with DB status and memory RSS verification."""
        db_status = "connected"
        db_healthy = True

        if db is not None:
            try:
                if hasattr(db, "execute_read"):
                    await db.execute_read("SELECT 1")
                elif hasattr(db, "_conn"):
                    with db._lock:
                        cur = db._conn.cursor()
                        try:
                            cur.execute("SELECT 1")
                            cur.fetchone()
                        finally:
                            cur.close()
            except Exception as db_err:
                logger.warning("Database health check failed: %s", db_err)
                db_status = f"error: {str(db_err)}"
                db_healthy = False

        unprocessed_summary = {}
        if db is not None and hasattr(db, "get_unprocessed_tasks"):
            try:
                unproc_res = db.get_unprocessed_tasks(limit=0)
                unprocessed_summary = unproc_res.get("counts", {})
            except Exception:
                pass

        if db is not None and hasattr(db, "shrink_memory"):
            try:
                db.shrink_memory(truncate_wal=False)
            except Exception:
                pass

        gc.collect(2)
        _apply_darwin_pressure_relief()

        stats = server.get_stats()
        uptime = stats.get("uptime_seconds", 0.0)
        rss_mb = get_memory_rss_mb()
        budget_limit = get_memory_budget_mb(config)
        if rss_mb > budget_limit:
            if db is not None and hasattr(db, "shrink_memory"):
                try:
                    db.shrink_memory(truncate_wal=True)
                except Exception:
                    pass
            gc.collect(2)
            _apply_darwin_pressure_relief()
            rss_mb = get_memory_rss_mb()
        memory_healthy = rss_mb <= budget_limit

        status_str = "ok" if (db_healthy and memory_healthy) else "degraded"
        status_code = 200 if db_healthy else 503

        response_data = {
            "status": status_str,
            "db": "ok" if db_healthy else "error",
            "uptime_seconds": uptime,
            "version": "1.0.0",
            "database": {
                "status": db_status,
            },
            "system": {
                "memory_rss_mb": rss_mb,
                "memory_healthy": memory_healthy,
                "memory_budget_mb": budget_limit,
            },
            "memory_rss_mb": rss_mb,
            "unprocessed": unprocessed_summary,
            "unprocessed_tasks_count": unprocessed_summary.get("total_unprocessed", 0),
        }

        return HTTPResponse.json(response_data, status_code=status_code)

    async def handle_ready(req: HTTPRequest) -> HTTPResponse:
        """GET /ready: Readiness probe checking database accessibility."""
        if db is not None:
            try:
                if hasattr(db, "execute_read"):
                    await db.execute_read("SELECT 1")
                elif hasattr(db, "_conn"):
                    with db._lock:
                        cur = db._conn.cursor()
                        try:
                            cur.execute("SELECT 1")
                            cur.fetchone()
                        finally:
                            cur.close()
            except Exception as err:
                logger.warning("Readiness probe check failed: %s", err)
                return HTTPResponse.json({"status": "not_ready", "error": str(err)}, status_code=503)

        return HTTPResponse.json({"status": "ready"}, status_code=200)

    async def handle_metrics(req: HTTPRequest) -> HTTPResponse:
        """GET /metrics: Prometheus text exposition format (default) or JSON."""
        stats = server.get_stats()
        uptime = stats.get("uptime_seconds", 0.0)
        total_requests = stats.get("total_requests", 0)
        active_connections = stats.get("active_connections", 0)
        rss_bytes = get_memory_rss_bytes()
        rss_mb = rss_bytes / (1024.0 * 1024.0)

        # Query task counts by status from database
        task_counts = {
            "received": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "timed_out": 0,
        }
        if db is not None and hasattr(db, "execute_read"):
            try:
                rows = await db.execute_read("SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status")
                for r in rows:
                    st = r.get("status")
                    if st in task_counts:
                        task_counts[st] = int(r.get("cnt", 0))
            except Exception as e:
                logger.debug("Failed to query task metrics: %s", e)

        # Check requested output format
        format_param = req.query_params.get("format", "").lower()
        accept_header = req.header("accept", "").lower()
        wants_json = (format_param == "json") or ("application/json" in accept_header)

        if wants_json:
            metrics_dict = {
                "uptime_seconds": uptime,
                "memory_rss_bytes": rss_bytes,
                "memory_rss_mb": round(rss_mb, 2),
                "requests_total": total_requests,
                "active_connections": active_connections,
                "tasks": {
                    "total": sum(task_counts.values()),
                    "by_status": task_counts,
                },
            }
            return HTTPResponse.json(metrics_dict, status_code=200)

        # Prometheus text exposition format (0.0.4)
        lines = [
            "# HELP webhook_hub_uptime_seconds Hub server uptime in seconds",
            "# TYPE webhook_hub_uptime_seconds gauge",
            f"webhook_hub_uptime_seconds {uptime:.2f}",
            "",
            "# HELP webhook_hub_process_memory_rss_bytes Process resident memory in bytes",
            "# TYPE webhook_hub_process_memory_rss_bytes gauge",
            f"webhook_hub_process_memory_rss_bytes {rss_bytes}",
            "",
            "# HELP webhook_hub_http_requests_total Total number of HTTP requests",
            "# TYPE webhook_hub_http_requests_total counter",
            f"webhook_hub_http_requests_total {total_requests}",
            "",
            "# HELP webhook_hub_active_connections Current active HTTP connections",
            "# TYPE webhook_hub_active_connections gauge",
            f"webhook_hub_active_connections {active_connections}",
            "",
            "# HELP webhook_hub_tasks_total Total tasks by status",
            "# TYPE webhook_hub_tasks_total gauge",
        ]
        for st_name, count_val in task_counts.items():
            lines.append(f'webhook_hub_tasks_total{{status="{st_name}"}} {count_val}')
        lines.append("")

        return HTTPResponse.text(
            "\n".join(lines),
            status_code=200,
            content_type="text/plain; version=0.0.4; charset=utf-8",
        )

    async def handle_root(req: HTTPRequest) -> HTTPResponse:
        """GET /: Root service descriptor, dashboard negotiation, and gateway status."""
        accept_header = req.headers.get("accept", "").lower()
        if "text/html" in accept_header:
            try:
                from hub.security import verify_dashboard_auth
                auth_res = verify_dashboard_auth(req, config)
                if not auth_res.is_valid:
                    realm = 'Basic realm="Antigravity Webhook Hub Dashboard"'
                    from hub.routes.dashboard import render_unauthorized_html
                    headers = {"WWW-Authenticate": realm, "Content-Type": "text/html; charset=utf-8"}
                    body = render_unauthorized_html(auth_res)
                    return HTTPResponse(status_code=auth_res.status_code or 401, headers=headers, body=body.encode("utf-8"))

                from hub.routes.dashboard import render_dashboard_html
                html = render_dashboard_html(config, db)
                return HTTPResponse.text(html, status_code=200, content_type="text/html; charset=utf-8")
            except Exception as e:
                logger.warning("Failed to render dashboard on root: %s", e)

        stats = server.get_stats()
        uptime = stats.get("uptime_seconds", 0.0)
        return HTTPResponse.json(
            {
                "service": "Antigravity Webhook Hub",
                "status": "ok",
                "version": "1.0.0",
                "uptime_seconds": round(uptime, 2),
                "endpoints": {
                    "dashboard": "/dashboard",
                    "health": "/healthz",
                    "health_alias": "/health",
                    "readiness": "/ready",
                    "metrics": "/metrics",
                    "webhook": "/webhook",
                    "tasks": "/tasks",
                    "tasks_summary": "/tasks/summary",
                    "events": "/events/stream",
                    "antigravity_status": "/antigravity/status",
                    "antigravity_pull_up": "/antigravity/pull-up",
                },
            },
            status_code=200,
        )

    async def handle_antigravity_status(req: HTTPRequest) -> HTTPResponse:
        """GET /antigravity/status: Diagnostics for Antigravity watchdog and stalled sessions."""
        watchdog = getattr(dispatcher, "antigravity_watchdog", None)
        active_broker = broker or getattr(dispatcher, "broker", None)
        if not watchdog:
            from hub.antigravity.watchdog import AntigravityWatchdog
            watchdog = AntigravityWatchdog(
                db=db,
                config=getattr(config, "antigravity_watchdog", None),
                broker=active_broker,
            )
        elif not getattr(watchdog, "broker", None) and active_broker:
            watchdog.broker = active_broker
        status_data = await asyncio.to_thread(watchdog.get_status)
        return HTTPResponse.json(status_data, status_code=200)

    async def handle_antigravity_pull_up(req: HTTPRequest) -> HTTPResponse:
        """POST /antigravity/pull-up: Trigger automated pull-up / resuscitation."""
        active_broker = broker or getattr(dispatcher, "broker", None)
        watchdog = getattr(dispatcher, "antigravity_watchdog", None)
        if not watchdog:
            from hub.antigravity.watchdog import AntigravityWatchdog
            watchdog = AntigravityWatchdog(
                db=db,
                config=getattr(config, "antigravity_watchdog", None),
                broker=active_broker,
            )
        elif not getattr(watchdog, "broker", None) and active_broker:
            watchdog.broker = active_broker

        body = req.json() if req.body else {}
        convo_id = body.get("conversation_id")
        prompt = body.get("prompt")

        if convo_id:
            stalled = watchdog.scan_stalled_conversations()
            matched = next((s for s in stalled if s.conversation_id == convo_id), None)
            if matched:
                res = await watchdog.resuscitate_session(matched, custom_prompt=prompt)
            else:
                from hub.antigravity.watchdog import StalledSessionInfo
                from hub.antigravity.result_delivery import resolve_transcript_path
                transcript_path = resolve_transcript_path(convo_id, watchdog._brain_dir)
                if not transcript_path or not transcript_path.exists():
                    return HTTPResponse.json(
                        {"success": False, "error": f"Conversation {convo_id} not found"},
                        status_code=404,
                    )
                info = StalledSessionInfo(
                    conversation_id=convo_id,
                    transcript_path=transcript_path,
                    last_step_index=0,
                    last_error="api_pull_up_request",
                    last_error_time=time.time(),
                    is_subagent=False,
                    sidecar_slug=watchdog._find_associated_sidecar(convo_id),
                    attempt_count=db.get_resuscitation_attempts(convo_id) if db else 0,
                    can_resuscitate=True,
                )
                res = await watchdog.resuscitate_session(info, custom_prompt=prompt)

            if active_broker:
                try:
                    await active_broker.publish("events", {
                        "event": "antigravity_resuscitation",
                        "type": "antigravity_resuscitation",
                        "action": "pull_up_completed",
                        "conversation_id": convo_id,
                        "success": res.get("success", False),
                        "status": res.get("status", "unknown"),
                        "timestamp": time.time(),
                    })
                except Exception as b_err:
                    logger.debug("Failed publishing pull-up SSE event: %s", b_err)

            return HTTPResponse.json(res, status_code=200 if res.get("success") else 500)
        else:
            results = await watchdog.resuscitate_stalled_sessions()
            if active_broker:
                try:
                    await active_broker.publish("events", {
                        "event": "antigravity_resuscitation",
                        "type": "antigravity_resuscitation",
                        "action": "pull_up_batch_completed",
                        "resuscitated_count": len(results),
                        "timestamp": time.time(),
                    })
                except Exception as b_err:
                    logger.debug("Failed publishing batch pull-up SSE event: %s", b_err)

            return HTTPResponse.json(
                {"success": True, "resuscitated_count": len(results), "results": results},
                status_code=200,
            )

    async def handle_antigravity_quota(req: HTTPRequest) -> HTTPResponse:
        """GET /antigravity/quota: Retrieve 5h & weekly quota snapshots and countdowns for all accounts."""
        active_broker = broker or getattr(dispatcher, "broker", None)
        sentinel = getattr(dispatcher, "antigravity_quota", None)
        if not sentinel:
            from hub.antigravity.quota_sentinel import AntigravityQuotaSentinel
            sentinel = AntigravityQuotaSentinel(
                config=config,
                db=db,
                broker=active_broker,
            )

        should_refresh = req.query_params.get("refresh") in ("true", "1", "yes")
        if should_refresh:
            try:
                profiles = await asyncio.to_thread(sentinel.scan_accounts, True)
                await asyncio.to_thread(sentinel.sync_quotas_to_db, profiles)
            except Exception as ref_err:
                logger.debug("On-demand quota refresh encountered error: %s", ref_err)

        email_filter = req.query_params.get("account") or req.query_params.get("email")
        overview = sentinel.get_quota_overview(account_email=email_filter)
        return HTTPResponse.json(overview, status_code=200)

    async def handle_antigravity_quota_health(req: HTTPRequest) -> HTTPResponse:
        """GET /antigravity/quota/health: High-signal Uptime Kuma monitoring probe for Antigravity Quota Sentinel."""
        active_broker = broker or getattr(dispatcher, "broker", None)
        sentinel = getattr(dispatcher, "antigravity_quota", None)
        if not sentinel:
            from hub.antigravity.quota_sentinel import AntigravityQuotaSentinel
            sentinel = AntigravityQuotaSentinel(
                config=config,
                db=db,
                broker=active_broker,
            )
        health_data = sentinel.get_quota_health()
        code = 200 if health_data.get("status") == "ok" else 503
        return HTTPResponse.json(health_data, status_code=code)

    async def handle_antigravity_warmup(req: HTTPRequest) -> HTTPResponse:
        """POST /antigravity/warmup: Trigger on-demand token ping warmup for 5h/weekly quotas."""
        active_broker = broker or getattr(dispatcher, "broker", None)
        sentinel = getattr(dispatcher, "antigravity_quota", None)
        if not sentinel:
            from hub.antigravity.quota_sentinel import AntigravityQuotaSentinel
            sentinel = AntigravityQuotaSentinel(
                config=config,
                db=db,
                broker=active_broker,
            )
        body = req.json() if req.body else {}
        email = body.get("account") or body.get("email") or req.query_params.get("account") or req.query_params.get("email")
        bucket = body.get("bucket") or body.get("bucket_id") or req.query_params.get("bucket") or req.query_params.get("bucket_id")
        force = bool(body.get("force", False) or req.query_params.get("force") in ("true", "1", "yes"))
        reason = body.get("reason") or req.query_params.get("reason") or "api_manual_trigger"
        all_accounts_val = body.get("all_accounts")
        if all_accounts_val is None and "all_accounts" in req.query_params:
            all_accounts_val = req.query_params.get("all_accounts") in ("true", "1", "yes")
        dry_run = bool(body.get("dry_run", False) or req.query_params.get("dry_run") in ("true", "1", "yes"))

        res = await sentinel.sweep_and_warmup(
            reason=reason,
            account_email=email,
            bucket_id=bucket,
            force=force,
            live=True,
            all_accounts=all_accounts_val,
            dry_run=dry_run,
        )
        return HTTPResponse.json(res, status_code=200)

    async def handle_dashboard(req: HTTPRequest) -> HTTPResponse:
        """GET /dashboard & GET /ui: Render Observable Activities Web Dashboard."""
        try:
            from hub.security import verify_dashboard_auth
            auth_res = verify_dashboard_auth(req, config)
            if not auth_res.is_valid:
                realm = 'Basic realm="Antigravity Webhook Hub Dashboard"'
                accept = req.header("accept") or ""
                if "application/json" in accept.lower():
                    headers = {"WWW-Authenticate": realm} if auth_res.status_code == 401 else {}
                    return HTTPResponse.json(
                        {"error": "unauthorized", "message": auth_res.message, "reason": auth_res.reason},
                        status_code=auth_res.status_code or 401,
                        headers=headers,
                    )

                from hub.routes.dashboard import render_unauthorized_html
                headers = {"WWW-Authenticate": realm, "Content-Type": "text/html; charset=utf-8"}
                body = render_unauthorized_html(auth_res)
                return HTTPResponse(status_code=auth_res.status_code or 401, headers=headers, body=body.encode("utf-8"))

            from hub.routes.dashboard import render_dashboard_html
            html = render_dashboard_html(config, db)
            return HTTPResponse.text(html, status_code=200, content_type="text/html; charset=utf-8")
        except Exception as e:
            logger.warning("Failed to render dashboard: %s", e)
            return HTTPResponse.error(f"Dashboard error: {e}", status_code=500)

    # Delegate task management routes to dedicated hub.routes.tasks module if not already registered
    if ("GET", "/tasks") not in getattr(server, "_exact_routes", {}):
        try:
            from hub.routes.tasks import register_task_routes
            register_task_routes(server, config, db, dispatcher=dispatcher, broker=broker)
        except ImportError:
            pass

    server.add_route("GET", "/", handle_root)
    server.add_route("GET", "/healthz", handle_healthz)
    server.add_route("GET", "/health", handle_healthz)
    server.add_route("GET", "/ready", handle_ready)
    server.add_route("GET", "/metrics", handle_metrics)
    server.add_route("GET", "/dashboard", handle_dashboard)
    server.add_route("GET", "/ui", handle_dashboard)
    server.add_route("GET", "/antigravity/status", handle_antigravity_status)
    server.add_route("GET", "/antigravity/watchdog", handle_antigravity_status)
    server.add_route("POST", "/antigravity/pull-up", handle_antigravity_pull_up)
    server.add_route("GET", "/antigravity/quota", handle_antigravity_quota)
    server.add_route("GET", "/antigravity/quota/health", handle_antigravity_quota_health)
    server.add_route("POST", "/antigravity/warmup", handle_antigravity_warmup)
    server.add_route("GET", "/antigravity/warmup", handle_antigravity_warmup)
