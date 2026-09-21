"""
Antigravity Webhook Hub — Scheduled Tasks API Endpoints
Implements RESTful schedule management conforming strictly to SSOT and Unidirectional Data Flow:
- GET /schedules, GET /api/v1/schedules: List and filter scheduled tasks with pagination.
- GET /schedules/summary, GET /api/v1/schedules/summary: Real-time summary statistics.
- POST /schedules, POST /api/v1/schedules: Submit and persist delayed or recurring schedules.
- GET /schedules/{schedule_id}: Schedule detail enriched with execution history.
- POST /schedules/{schedule_id}/trigger: Trigger immediate on-demand run.
- POST /schedules/{schedule_id}/pause: Pause active schedule.
- POST /schedules/{schedule_id}/resume: Resume paused schedule with recalculation.
- DELETE /schedules/{schedule_id}: Cancel and purge schedule.
- POST /schedules/sweep, POST /schedules/trigger-due: Manual / external heartbeat sweep.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from hub.config import AppConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.schedules")


def register_schedule_routes(
    server: AsyncHTTPServer,
    config: AppConfig,
    db: Optional[Any] = None,
    scheduler: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
    broker: Optional[Any] = None,
) -> None:
    """Register all scheduled task routes on the HTTP server."""
    if ("GET", "/schedules") in server._exact_routes:
        return

    async def handle_list_schedules(req: HTTPRequest) -> HTTPResponse:
        """GET /schedules: List schedules with status/type filter and search query."""
        status_filter = req.query_params.get("status")
        type_filter = req.query_params.get("type") or req.query_params.get("schedule_type")
        search_query = req.query_params.get("q") or req.query_params.get("search")

        try:
            limit = min(max(1, int(req.query_params.get("limit", 50))), 500)
        except (ValueError, TypeError):
            limit = 50

        try:
            offset = max(0, int(req.query_params.get("offset", 0)))
        except (ValueError, TypeError):
            offset = 0

        schedules = []
        total_count = 0
        stats = {}

        if db is not None:
            if hasattr(db, "list_schedules"):
                res = db.list_schedules(
                    status=status_filter,
                    schedule_type=type_filter,
                    search=search_query,
                    limit=limit,
                    offset=offset,
                )
                if asyncio.iscoroutine(res):
                    schedules, total_count = await res
                else:
                    schedules, total_count = res

            if hasattr(db, "get_schedule_summary_stats"):
                st_res = db.get_schedule_summary_stats()
                stats = await st_res if asyncio.iscoroutine(st_res) else st_res

        return HTTPResponse.json(
            {
                "status": "success",
                "schedules": schedules,
                "total": total_count,
                "count": len(schedules),
                "limit": limit,
                "offset": offset,
                "stats": stats,
            },
            status_code=200,
        )

    async def handle_schedules_summary(req: HTTPRequest) -> HTTPResponse:
        """GET /schedules/summary: Return real-time summary statistics across all schedules."""
        stats = {}
        if db is not None and hasattr(db, "get_schedule_summary_stats"):
            st_res = db.get_schedule_summary_stats()
            stats = await st_res if asyncio.iscoroutine(st_res) else st_res

        return HTTPResponse.json(
            {
                "status": "success",
                "stats": stats,
            },
            status_code=200,
        )

    async def handle_create_schedule(req: HTTPRequest) -> HTTPResponse:
        """POST /schedules: Create a new delayed one-off or recurring cron schedule."""
        try:
            body_dict = req.json()
            if not isinstance(body_dict, dict):
                return HTTPResponse.error("Request body must be a JSON object", status_code=400)
        except Exception:
            return HTTPResponse.error("Malformed JSON payload", status_code=400)

        name = body_dict.get("name") or body_dict.get("title")
        if not name:
            return HTTPResponse.error("Field 'name' is required for a schedule", status_code=400)

        schedule_type = (body_dict.get("schedule_type") or body_dict.get("type") or "once").lower().strip()
        if schedule_type not in ("once", "recurring"):
            return HTTPResponse.error("Invalid 'schedule_type': must be 'once' or 'recurring'", status_code=400)

        action_type = (body_dict.get("action_type") or body_dict.get("action") or "cli").lower().strip()
        command = body_dict.get("command") or body_dict.get("target_action") or ""
        target_action = body_dict.get("target_action") or command
        source = body_dict.get("source") or "scheduler"
        timezone = body_dict.get("timezone") or "Asia/Bangkok"

        action_params = body_dict.get("action_params") or {}
        if not isinstance(action_params, dict):
            action_params = {}

        # Allow top-level prompt or text to pass to action_params for antigravity agent calls
        if "prompt" in body_dict and "prompt" not in action_params:
            action_params["prompt"] = body_dict["prompt"]
        if "text" in body_dict and "text" not in action_params:
            action_params["text"] = body_dict["text"]
        if "channel" in body_dict and "channel" not in action_params:
            action_params["channel"] = body_dict["channel"]
        if "conversation_id" in body_dict and "conversation_id" not in action_params:
            action_params["conversation_id"] = body_dict["conversation_id"]

        try:
            if scheduler is not None:
                sched_obj = await scheduler.create_schedule(
                    name=name,
                    schedule_type=schedule_type,
                    scheduled_at=body_dict.get("scheduled_at"),
                    delay_seconds=body_dict.get("delay_seconds"),
                    delay=body_dict.get("delay"),
                    cron_expression=body_dict.get("cron_expression") or body_dict.get("cron"),
                    action_type=action_type,
                    command=command,
                    target_action=target_action,
                    action_params=action_params,
                    source=source,
                    timezone=timezone,
                    max_runs=body_dict.get("max_runs"),
                )
                sched_data = sched_obj.to_dict()
            elif db is not None:
                from hub.scheduler import compute_next_cron_run, format_iso_utc, parse_schedule_time
                import datetime

                now_dt = datetime.datetime.now(datetime.timezone.utc)
                cron_expr = body_dict.get("cron_expression") or body_dict.get("cron")

                if schedule_type == "recurring":
                    if not cron_expr:
                        return HTTPResponse.error("Field 'cron_expression' required for recurring schedule", status_code=400)
                    next_dt = compute_next_cron_run(cron_expr, start_dt=now_dt, tz_name=timezone)
                    next_run_iso = format_iso_utc(next_dt)
                    scheduled_at_iso = None
                else:
                    target_dt = parse_schedule_time(
                        scheduled_at=body_dict.get("scheduled_at"),
                        delay_seconds=body_dict.get("delay_seconds"),
                        delay=body_dict.get("delay"),
                        base_dt=now_dt,
                    )
                    next_run_iso = format_iso_utc(target_dt)
                    scheduled_at_iso = next_run_iso

                sched_data = {
                    "name": name,
                    "source": source,
                    "schedule_type": schedule_type,
                    "cron_expression": cron_expr,
                    "scheduled_at": scheduled_at_iso,
                    "next_run_at": next_run_iso,
                    "timezone": timezone,
                    "action_type": action_type,
                    "command": command,
                    "target_action": target_action,
                    "action_params": action_params,
                    "status": "active",
                }
                sch_id = db.insert_schedule(sched_data)
                sched_data["schedule_id"] = sch_id
            else:
                return HTTPResponse.error("Database or scheduler not initialized", status_code=500)

            return HTTPResponse.json(
                {
                    "status": "created",
                    "schedule": sched_data,
                    "schedule_id": sched_data.get("schedule_id"),
                    "next_run_at": sched_data.get("next_run_at"),
                },
                status_code=201,
            )
        except Exception as e:
            logger.error("Failed creating schedule: %s", e)
            return HTTPResponse.error(f"Failed to create schedule: {e}", status_code=400)

    async def handle_schedule_detail(req: HTTPRequest, schedule_id: str) -> HTTPResponse:
        """GET /schedules/{schedule_id}: Fetch schedule detail and execution history."""
        if db is None:
            return HTTPResponse.error("Database not available", status_code=500)

        sched = db.get_schedule(schedule_id)
        if asyncio.iscoroutine(sched):
            sched = await sched

        if not sched:
            return HTTPResponse.error(f"Schedule {schedule_id} not found", status_code=404)

        history = []
        if hasattr(db, "get_schedule_history"):
            h_res = db.get_schedule_history(schedule_id, limit=20)
            history = await h_res if asyncio.iscoroutine(h_res) else h_res

        return HTTPResponse.json(
            {
                "status": "success",
                "schedule": sched,
                "history": history,
            },
            status_code=200,
        )

    async def handle_schedule_trigger(req: HTTPRequest, schedule_id: str) -> HTTPResponse:
        """POST /schedules/{schedule_id}/trigger: Trigger immediate on-demand execution."""
        if scheduler is not None:
            res = await scheduler.trigger_now(schedule_id)
            if res is None:
                return HTTPResponse.error(f"Schedule {schedule_id} not found", status_code=404)
            return HTTPResponse.json(
                {
                    "status": "triggered",
                    "schedule_id": schedule_id,
                    "result": res,
                },
                status_code=202,
            )

        return HTTPResponse.error("Scheduler engine not active", status_code=500)

    async def handle_schedule_pause(req: HTTPRequest, schedule_id: str) -> HTTPResponse:
        """POST /schedules/{schedule_id}/pause: Pause an active schedule."""
        if scheduler is not None:
            success = await scheduler.pause_schedule(schedule_id)
        elif db is not None:
            success = db.update_schedule_status(schedule_id, "paused")
        else:
            success = False

        if not success:
            return HTTPResponse.error(f"Failed to pause schedule {schedule_id}", status_code=400)

        return HTTPResponse.json(
            {
                "status": "paused",
                "schedule_id": schedule_id,
            },
            status_code=200,
        )

    async def handle_schedule_resume(req: HTTPRequest, schedule_id: str) -> HTTPResponse:
        """POST /schedules/{schedule_id}/resume: Resume a paused schedule."""
        if scheduler is not None:
            success = await scheduler.resume_schedule(schedule_id)
        elif db is not None:
            success = db.update_schedule_status(schedule_id, "active")
        else:
            success = False

        if not success:
            return HTTPResponse.error(f"Failed to resume schedule {schedule_id}", status_code=400)

        return HTTPResponse.json(
            {
                "status": "active",
                "schedule_id": schedule_id,
            },
            status_code=200,
        )

    async def handle_schedule_delete(req: HTTPRequest, schedule_id: str) -> HTTPResponse:
        """DELETE /schedules/{schedule_id}: Cancel and remove schedule."""
        if scheduler is not None:
            success = await scheduler.cancel_schedule(schedule_id)
            if hasattr(db, "delete_schedule"):
                db.delete_schedule(schedule_id)
        elif db is not None:
            success = db.delete_schedule(schedule_id)
        else:
            success = False

        if not success:
            return HTTPResponse.error(f"Schedule {schedule_id} not found", status_code=404)

        return HTTPResponse.json(
            {
                "status": "deleted",
                "schedule_id": schedule_id,
            },
            status_code=200,
        )

    async def handle_schedule_sweep(req: HTTPRequest) -> HTTPResponse:
        """POST /schedules/sweep: Run on-demand sweep of due schedules (heartbeat endpoint)."""
        if scheduler is not None:
            res = await scheduler.sweep_due(reason="external_heartbeat_sweep")
            return HTTPResponse.json(
                {
                    "status": "success",
                    "sweep": res,
                },
                status_code=200,
            )

        return HTTPResponse.error("Scheduler engine not active", status_code=500)

    # Register routes
    server.add_route("GET", "/schedules", handle_list_schedules)
    server.add_route("GET", "/api/schedules", handle_list_schedules)
    server.add_route("GET", "/api/v1/schedules", handle_list_schedules)
    server.add_route("GET", "/schedules/summary", handle_schedules_summary)
    server.add_route("GET", "/api/schedules/summary", handle_schedules_summary)
    server.add_route("GET", "/api/v1/schedules/summary", handle_schedules_summary)
    server.add_route("POST", "/schedules", handle_create_schedule)
    server.add_route("POST", "/api/schedules", handle_create_schedule)
    server.add_route("POST", "/api/v1/schedules", handle_create_schedule)
    server.add_route("GET", "/schedules/{schedule_id}", handle_schedule_detail)
    server.add_route("POST", "/schedules/{schedule_id}/trigger", handle_schedule_trigger)
    server.add_route("POST", "/schedules/{schedule_id}/pause", handle_schedule_pause)
    server.add_route("POST", "/schedules/{schedule_id}/resume", handle_schedule_resume)
    server.add_route("DELETE", "/schedules/{schedule_id}", handle_schedule_delete)
    server.add_route("POST", "/schedules/sweep", handle_schedule_sweep)
    server.add_route("POST", "/schedules/trigger-due", handle_schedule_sweep)
    server.add_route("POST", "/webhook/scheduler", handle_schedule_sweep)
    server.add_route("POST", "/webhook/cron", handle_schedule_sweep)
