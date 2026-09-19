"""
Antigravity Webhook Hub — Tasks API Endpoints
Implements GET /tasks (list & filter), GET /tasks/{task_id} (detail & logs),
and POST /tasks (direct task creation & dispatch).
Conforms strictly to PROJECT.md § Code Layout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from hub.config import AppConfig
from hub.models import HTTPRequest, HTTPResponse, TEST_EVENT_SQL_FILTER, is_test_task
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.tasks")


def register_task_routes(
    server: AsyncHTTPServer,
    config: AppConfig,
    db: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
    broker: Optional[Any] = None,
) -> None:
    """Register all task management routes: GET /tasks, GET /tasks/{task_id}, POST /tasks."""
    if ("GET", "/tasks") in server._exact_routes:
        return

    async def handle_tasks_list(req: HTTPRequest) -> HTTPResponse:
        """GET /tasks: List tasks with optional status, source, action, search filtering and pagination."""
        status_filter = req.query_params.get("status")
        source_filter = req.query_params.get("source")
        action_filter = req.query_params.get("action_type") or req.query_params.get("action")
        search_query = req.query_params.get("q") or req.query_params.get("search")

        filter_test = (req.query_params.get("filter_test") or req.query_params.get("test_filter") or "").lower().strip()
        hide_test_param = (req.query_params.get("hide_test") or req.query_params.get("hide_tests") or "").lower().strip()
        is_test_param = (req.query_params.get("is_test") or "").lower().strip()

        test_mode = "all"
        if hide_test_param in ("true", "1", "yes") or filter_test in ("hide", "real", "production") or is_test_param in ("0", "false"):
            test_mode = "real"
        elif filter_test in ("only", "test", "synthetic") or is_test_param in ("1", "true"):
            test_mode = "test"

        try:
            limit = min(max(1, int(req.query_params.get("limit", 50))), 500)
        except (ValueError, TypeError):
            limit = 50

        try:
            offset = max(0, int(req.query_params.get("offset", 0)))
        except (ValueError, TypeError):
            offset = 0

        tasks = []
        total_count = 0

        if db is not None and hasattr(db, "execute_read"):
            try:
                where_clauses: list[str] = []
                params: list[Any] = []

                if status_filter:
                    where_clauses.append("status = ?")
                    params.append(status_filter)
                if source_filter:
                    if source_filter in ("uptime_kuma", "uptime-kuma"):
                        where_clauses.append("(source = ? OR source = ?)")
                        params.extend(["uptime_kuma", "uptime-kuma"])
                    elif source_filter in ("contact_review", "contact-review"):
                        where_clauses.append("(source = ? OR source = ?)")
                        params.extend(["contact_review", "contact-review"])
                    else:
                        where_clauses.append("source = ?")
                        params.append(source_filter)
                if action_filter:
                    where_clauses.append("action_type = ?")
                    params.append(action_filter)
                if test_mode == "real":
                    where_clauses.append(f"NOT {TEST_EVENT_SQL_FILTER}")
                elif test_mode == "test":
                    where_clauses.append(TEST_EVENT_SQL_FILTER)
                if search_query:
                    where_clauses.append(
                        "(task_id LIKE ? OR event_id LIKE ? OR command LIKE ? OR target_action LIKE ?)"
                    )
                    pattern = f"%{search_query.strip()}%"
                    params.extend([pattern, pattern, pattern, pattern])

                where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
                count_sql = f"SELECT COUNT(*) as cnt FROM tasks {where_sql}"
                query_sql = f"SELECT * FROM tasks {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"

                count_params = list(params)
                fetch_params = list(params) + [limit, offset]

                count_rows = await db.execute_read(count_sql, tuple(count_params))
                total_count = count_rows[0].get("cnt", 0) if count_rows else 0

                raw_tasks = await db.execute_read(query_sql, tuple(fetch_params))
                tasks = [dict(t) for t in raw_tasks]
                for t in tasks:
                    t["is_test"] = is_test_task(t)

                if test_mode == "real":
                    tasks = [t for t in tasks if not t["is_test"]]
                elif test_mode == "test":
                    tasks = [t for t in tasks if t["is_test"]]
            except Exception as e:
                logger.warning("Error querying tasks list: %s", e)

        return HTTPResponse.json(
            {
                "tasks": tasks,
                "total": total_count,
                "count": len(tasks),
                "limit": limit,
                "offset": offset,
                "filter_test": test_mode,
            },
            status_code=200,
        )

    async def handle_task_detail(req: HTTPRequest, task_id: str) -> HTTPResponse:
        """GET /tasks/{task_id}: Authoritative task detail enriched with stdout/stderr."""
        if db is None:
            return HTTPResponse.error(f"Task {task_id} not found", status_code=404)

        task = db.get_task(task_id)
        if asyncio.iscoroutine(task):
            task = await task

        if not task:
            return HTTPResponse.error(f"Task {task_id} not found", status_code=404)

        task_data = dict(task)

        # Enrich with chronological logs, stdout, and stderr from execution_logs or executions table
        task_data["logs"] = []
        try:
            raw_logs = []
            if hasattr(db, "get_execution_logs"):
                res = db.get_execution_logs(task_id)
                raw_logs = await res if asyncio.iscoroutine(res) else res
            elif hasattr(db, "execute_read"):
                raw_logs = await db.execute_read(
                    "SELECT stream_type, chunk AS line, timestamp FROM execution_logs WHERE task_id = ? ORDER BY log_id ASC",
                    (task_id,),
                )
            if raw_logs:
                task_data["logs"] = [
                    {
                        "stream": str(r.get("stream_type") or r.get("stream") or "stdout"),
                        "line": str(r.get("line") or r.get("chunk") or ""),
                        "timestamp": str(r.get("timestamp") or ""),
                    }
                    for r in raw_logs
                ]
                stdout_lines = [r["line"] for r in task_data["logs"] if r["stream"] == "stdout"]
                stderr_lines = [r["line"] for r in task_data["logs"] if r["stream"] == "stderr"]
                if stdout_lines:
                    task_data["stdout"] = "\n".join(stdout_lines)
                if stderr_lines:
                    task_data["stderr"] = "\n".join(stderr_lines)

            # Fallback to executions table if stdout or stderr not present
            if ("stdout" not in task_data or not task_data["stdout"]) and ("stderr" not in task_data or not task_data["stderr"]):
                if hasattr(db, "execute_read"):
                    exec_rows = await db.execute_read(
                        "SELECT stdout_tail, stderr_tail FROM executions WHERE task_id = ? ORDER BY attempt_number DESC LIMIT 1",
                        (task_id,),
                    )
                    if exec_rows:
                        if "stdout" not in task_data and exec_rows[0].get("stdout_tail"):
                            task_data["stdout"] = exec_rows[0].get("stdout_tail") or ""
                        if "stderr" not in task_data and exec_rows[0].get("stderr_tail"):
                            task_data["stderr"] = exec_rows[0].get("stderr_tail") or ""
        except Exception as log_err:
            logger.debug("Failed to enrich task logs: %s", log_err)

        task_data.setdefault("stdout", "")
        task_data.setdefault("stderr", "")

        # Enrich with Agent Activity (Signal, Sidebar Pulse, Prompt Payload)
        try:
            from hub.routes.agent_activities import get_task_agent_activity
            act = get_task_agent_activity(task_id, task_data, db, config)
            if act:
                task_data["agent_activity"] = act
        except Exception as act_err:
            logger.debug("Failed to enrich agent activity for task %s: %s", task_id, act_err)

        resp = HTTPResponse.json(task_data, status_code=200)
        del task_data
        return resp

    async def handle_create_task(req: HTTPRequest) -> HTTPResponse:
        """POST /tasks: Direct task submission and enqueueing."""
        try:
            body_dict = req.json()
            if not isinstance(body_dict, dict):
                return HTTPResponse.error("Request body must be a JSON object", status_code=400)
        except Exception:
            return HTTPResponse.error("Malformed JSON body", status_code=400)

        action_type = (body_dict.get("action") or body_dict.get("action_type") or "cli").lower().strip()
        command = body_dict.get("command") or body_dict.get("target_action") or ""
        source = body_dict.get("source") or "api"

        try:
            timeout_sec = int(body_dict.get("timeout_seconds", 300))
        except (ValueError, TypeError):
            timeout_sec = 300

        try:
            priority = int(body_dict.get("priority", 0))
        except (ValueError, TypeError):
            priority = 0

        try:
            max_retries = int(body_dict.get("max_retries", 0))
        except (ValueError, TypeError):
            max_retries = 0

        task_id = f"tsk_{uuid.uuid4().hex[:16]}"
        event_id = f"evt_{uuid.uuid4().hex[:16]}"

        if db is not None:
            # Create synthetic event record for foreign key integrity
            event_record = {
                "event_id": event_id,
                "source": source,
                "idempotency_key": body_dict.get("idempotency_key") or task_id,
                "payload_hash": "",
                "headers_json": json.dumps(dict(req.headers)),
                "raw_payload": req.text() if hasattr(req, "text") else json.dumps(body_dict),
                "method": "POST",
                "path": "/tasks",
                "status": "received",
            }
            if hasattr(db, "insert_event"):
                res = db.insert_event(event_record)
                if asyncio.iscoroutine(res):
                    await res

            task_record = {
                "task_id": task_id,
                "event_id": event_id,
                "source": source,
                "action_type": action_type,
                "command": command,
                "target_action": command,
                "action_params_json": json.dumps(body_dict),
                "status": "queued",
                "priority": priority,
                "timeout_seconds": timeout_sec,
                "retry_count": 0,
                "max_retries": max_retries,
            }
            if hasattr(db, "insert_task"):
                res = db.insert_task(task_record)
                if asyncio.iscoroutine(res):
                    await res

        if dispatcher is not None:
            await dispatcher.enqueue(task_id)

        if broker is not None:
            try:
                import time
                await broker.publish("events", {
                    "event_type": "task_created",
                    "task_id": task_id,
                    "event_id": event_id,
                    "source": source,
                    "action_type": action_type,
                    "status": "queued",
                    "timestamp": time.time(),
                })
            except Exception:
                pass

        return HTTPResponse.json(
            {
                "status": "queued",
                "task_id": task_id,
                "event_id": event_id,
            },
            status_code=202,
        )

    async def handle_unprocessed_tasks(req: HTTPRequest) -> HTTPResponse:
        """GET /tasks/unprocessed: Return all unprocessed tasks and orphaned events from SQLite SSOT."""
        status_filter = req.query_params.get("status")
        source_filter = req.query_params.get("source")
        try:
            limit = min(max(1, int(req.query_params.get("limit", 100))), 500)
        except (ValueError, TypeError):
            limit = 100
        try:
            stale_sec = max(10, int(req.query_params.get("stale_seconds", 300)))
        except (ValueError, TypeError):
            stale_sec = 300

        if db is not None and hasattr(db, "get_unprocessed_tasks"):
            data = db.get_unprocessed_tasks(
                status=status_filter,
                source=source_filter,
                stale_running_seconds=stale_sec,
                limit=limit,
            )
            return HTTPResponse.json(
                {
                    "status": "success",
                    "counts": data.get("counts", {}),
                    "total_unprocessed": data.get("counts", {}).get("total_unprocessed", 0),
                    "tasks": data.get("tasks", []),
                    "orphaned_events": data.get("orphaned_events", []),
                },
                status_code=200,
            )

        return HTTPResponse.json(
            {
                "status": "success",
                "counts": {"total_unprocessed": 0},
                "total_unprocessed": 0,
                "tasks": [],
                "orphaned_events": [],
            },
            status_code=200,
        )

    async def handle_sweep_tasks(req: HTTPRequest) -> HTTPResponse:
        """POST /tasks/sweep: Trigger immediate sweep and recovery of unprocessed tasks."""
        dry_run = False
        source_filter = None
        stale_sec = 300
        max_retries = 3
        limit = 100
        auto_retry = True

        if req.query_params.get("dry_run", "").lower() in ("true", "1", "yes"):
            dry_run = True
        if "auto_retry" in req.query_params:
            auto_retry = req.query_params.get("auto_retry", "").lower() not in ("false", "0", "no")
        if "auto_retry_interrupted" in req.query_params:
            auto_retry = req.query_params.get("auto_retry_interrupted", "").lower() not in ("false", "0", "no")
        source_filter = req.query_params.get("source")

        if req.body:
            try:
                body = req.json()
                if isinstance(body, dict):
                    if "dry_run" in body:
                        dry_run = bool(body["dry_run"])
                    if "source" in body:
                        source_filter = str(body["source"])
                    if "stale_seconds" in body:
                        stale_sec = int(body["stale_seconds"])
                    if "max_retries" in body:
                        max_retries = int(body["max_retries"])
                    if "limit" in body:
                        limit = int(body["limit"])
                    if "auto_retry" in body:
                        auto_retry = bool(body["auto_retry"])
                    if "auto_retry_interrupted" in body:
                        auto_retry = bool(body["auto_retry_interrupted"])
            except Exception:
                pass

        if dry_run:
            if db is not None and hasattr(db, "get_unprocessed_tasks"):
                unproc = db.get_unprocessed_tasks(
                    source=source_filter,
                    stale_running_seconds=stale_sec,
                    limit=limit,
                )
                return HTTPResponse.json(
                    {
                        "status": "success",
                        "dry_run": True,
                        "total_unprocessed": unproc.get("total_unprocessed", unproc.get("counts", {}).get("total_unprocessed", 0)),
                        "would_recover": unproc.get("counts", {}),
                        "tasks": unproc.get("tasks", []),
                        "orphaned_events": unproc.get("orphaned_events", []),
                    },
                    status_code=200,
                )
            return HTTPResponse.json({"status": "success", "dry_run": True, "would_recover": {}}, status_code=200)

        if dispatcher is not None and hasattr(dispatcher, "sweep_unprocessed_tasks"):
            sweep_summary = await dispatcher.sweep_unprocessed_tasks(
                reason="api_request",
                stale_running_seconds=stale_sec,
                max_retries=max_retries,
                auto_retry_interrupted=auto_retry,
                source=source_filter,
                limit=limit,
            )
            return HTTPResponse.json(
                {
                    "status": "success",
                    "dry_run": False,
                    "recovered": sweep_summary,
                    "recovered_orphaned_events": sweep_summary.get("recovered_orphaned_events", 0),
                    "recovered_stale_running": sweep_summary.get("recovered_stale_running", 0),
                    "recovered_interrupted_failed": sweep_summary.get("recovered_interrupted_failed", 0),
                    "total_queued": sweep_summary.get("total_queued", 0),
                },
                status_code=200,
            )

        if db is not None and hasattr(db, "sweep_and_requeue_unprocessed"):
            res = db.sweep_and_requeue_unprocessed(
                stale_running_seconds=stale_sec,
                max_retries=max_retries,
                auto_retry_interrupted=auto_retry,
                source=source_filter,
                limit=limit,
            )
            return HTTPResponse.json(
                {
                    "status": "success",
                    "dry_run": False,
                    "recovered": res,
                    "recovered_orphaned_events": res.get("recovered_orphaned_events", 0),
                    "recovered_stale_running": res.get("recovered_stale_running", 0),
                    "recovered_interrupted_failed": res.get("recovered_interrupted_failed", 0),
                    "total_queued": res.get("total_queued", 0),
                },
                status_code=200,
            )

        return HTTPResponse.error("Neither dispatcher nor db available for sweep", status_code=500)

    async def handle_tasks_summary(req: HTTPRequest) -> HTTPResponse:
        """GET /tasks/summary & GET /activities/summary: Aggregate metrics derived directly from SQLite SSOT."""
        counts_by_status = {
            "received": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "timed_out": 0,
            "cancelled": 0,
        }
        counts_by_source: dict[str, int] = {}
        counts_by_action: dict[str, int] = {}
        total_tasks = 0
        total_events = 0

        if db is not None and hasattr(db, "execute_read"):
            try:
                real_by_status = {
                    "received": 0, "queued": 0, "running": 0,
                    "succeeded": 0, "failed": 0, "timed_out": 0, "cancelled": 0,
                }
                test_by_status = {
                    "received": 0, "queued": 0, "running": 0,
                    "succeeded": 0, "failed": 0, "timed_out": 0, "cancelled": 0,
                }
                real_tasks = 0
                test_tasks = 0

                task_rows = await db.execute_read(
                    "SELECT task_id, event_id, source, command, target_action, action_params_json, status FROM tasks"
                )
                for r in task_rows:
                    st = r.get("status")
                    is_t = is_test_task(r)
                    if st in counts_by_status:
                        counts_by_status[st] += 1
                    total_tasks += 1
                    if is_t:
                        if st in test_by_status:
                            test_by_status[st] += 1
                        test_tasks += 1
                    else:
                        if st in real_by_status:
                            real_by_status[st] += 1
                        real_tasks += 1

                source_rows = await db.execute_read(
                    "SELECT source, COUNT(*) as cnt FROM tasks GROUP BY source ORDER BY cnt DESC LIMIT 10"
                )
                for r in source_rows:
                    src = r.get("source") or "default"
                    counts_by_source[src] = int(r.get("cnt", 0))

                action_rows = await db.execute_read(
                    "SELECT action_type, COUNT(*) as cnt FROM tasks GROUP BY action_type ORDER BY cnt DESC LIMIT 10"
                )
                for r in action_rows:
                    act = r.get("action_type") or "cli"
                    counts_by_action[act] = int(r.get("cnt", 0))

                evt_rows = await db.execute_read("SELECT COUNT(*) as cnt FROM webhook_events")
                if evt_rows:
                    total_events = int(evt_rows[0].get("cnt", 0))
            except Exception as e:
                logger.warning("Error aggregating tasks summary: %s", e)
                real_tasks = 0
                test_tasks = 0
                real_by_status = {}
                test_by_status = {}
        else:
            real_tasks = 0
            test_tasks = 0
            real_by_status = {}
            test_by_status = {}

        return HTTPResponse.json(
            {
                "status": "success",
                "total_tasks": total_tasks,
                "total_events": total_events,
                "real_tasks": real_tasks,
                "test_tasks": test_tasks,
                "by_status": counts_by_status,
                "real_by_status": real_by_status,
                "test_by_status": test_by_status,
                "by_source": counts_by_source,
                "by_action": counts_by_action,
            },
            status_code=200,
        )

    async def handle_task_rerun(req: HTTPRequest, task_id: str) -> HTTPResponse:
        """POST /tasks/{task_id}/rerun: Re-enqueue an existing task into SQLite SSOT and dispatcher."""
        if db is None:
            return HTTPResponse.error("Database not available", status_code=500)

        task = db.get_task(task_id)
        if asyncio.iscoroutine(task):
            task = await task

        if not task:
            return HTTPResponse.error(f"Task {task_id} not found", status_code=404)

        if hasattr(db, "rerun_task"):
            rerun_res = db.rerun_task(task_id)
            if asyncio.iscoroutine(rerun_res):
                await rerun_res
        elif hasattr(db, "execute_write"):
            await db.execute_write(
                """
                UPDATE tasks
                SET status = 'queued',
                    retry_count = retry_count + 1,
                    exit_code = NULL,
                    error_message = NULL,
                    result_json = NULL,
                    started_at = NULL,
                    completed_at = NULL,
                    queued_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (task_id,),
            )

        if dispatcher is not None:
            await dispatcher.enqueue(task_id)

        if broker is not None:
            try:
                import time
                await broker.publish("events", {
                    "event_type": "status_change",
                    "task_id": task_id,
                    "status": "queued",
                    "timestamp": time.time(),
                })
            except Exception:
                pass

        updated_task = db.get_task(task_id)
        if asyncio.iscoroutine(updated_task):
            updated_task = await updated_task

        return HTTPResponse.json(
            {
                "status": "queued",
                "task_id": task_id,
                "task": dict(updated_task) if updated_task else {},
            },
            status_code=200,
        )

    server.add_route("GET", "/tasks", handle_tasks_list)
    server.add_route("GET", "/tasks/summary", handle_tasks_summary)
    server.add_route("GET", "/activities/summary", handle_tasks_summary)
    server.add_route("GET", "/api/tasks/summary", handle_tasks_summary)
    server.add_route("GET", "/api/activities/summary", handle_tasks_summary)
    server.add_route("GET", "/tasks/unprocessed", handle_unprocessed_tasks)
    server.add_route("POST", "/tasks/sweep", handle_sweep_tasks)
    server.add_route("GET", "/tasks/{task_id}", handle_task_detail)
    server.add_route("POST", "/tasks/{task_id}/rerun", handle_task_rerun)
    server.add_route("POST", "/tasks", handle_create_task)
