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
from hub.models import HTTPRequest, HTTPResponse
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

    async def handle_tasks_list(req: HTTPRequest) -> HTTPResponse:
        """GET /tasks: List tasks with optional status filtering and pagination."""
        status_filter = req.query_params.get("status")
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
                if status_filter:
                    tasks = await db.execute_read(
                        "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        (status_filter, limit, offset),
                    )
                    count_rows = await db.execute_read(
                        "SELECT COUNT(*) as cnt FROM tasks WHERE status = ?",
                        (status_filter,),
                    )
                else:
                    tasks = await db.execute_read(
                        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                        (limit, offset),
                    )
                    count_rows = await db.execute_read("SELECT COUNT(*) as cnt FROM tasks")

                total_count = count_rows[0].get("cnt", len(tasks)) if count_rows else len(tasks)
            except Exception as e:
                logger.warning("Error querying tasks list: %s", e)

        return HTTPResponse.json(
            {
                "tasks": tasks,
                "total": total_count,
                "count": len(tasks),
                "limit": limit,
                "offset": offset,
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

        # Enrich with stdout and stderr from execution_logs or executions table
        if hasattr(db, "execute_read"):
            try:
                logs = await db.execute_read(
                    "SELECT stream_type, chunk AS line FROM execution_logs WHERE task_id = ? ORDER BY log_id ASC",
                    (task_id,),
                )
                stdout_lines = [row["line"] for row in logs if row["stream_type"] == "stdout"]
                stderr_lines = [row["line"] for row in logs if row["stream_type"] == "stderr"]
                if stdout_lines:
                    task_data["stdout"] = "\n".join(stdout_lines)
                if stderr_lines:
                    task_data["stderr"] = "\n".join(stderr_lines)

                # Fallback to executions table if stdout or stderr not present
                if "stdout" not in task_data or "stderr" not in task_data:
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

        return HTTPResponse.json(task_data, status_code=200)

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

        return HTTPResponse.json(
            {
                "status": "queued",
                "task_id": task_id,
                "event_id": event_id,
            },
            status_code=202,
        )

    server.add_route("GET", "/tasks", handle_tasks_list)
    server.add_route("GET", "/tasks/{task_id}", handle_task_detail)
    server.add_route("POST", "/tasks", handle_create_task)
