"""
Antigravity Webhook Hub — Webhook Ingress Routes
Implements POST /webhook and POST /webhook/{source}.
Enforces cryptographic and schema validation before any database write.
Dual-layer deduplication by idempotency key and payload hash.
Asynchronously enqueues accepted tasks to dispatcher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from hub.config import AppConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.security import compute_payload_hash, validate_request_security
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.webhook")


def register_webhook_routes(
    server: AsyncHTTPServer,
    config: AppConfig,
    db: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
    broker: Optional[Any] = None,
) -> None:
    """Register POST /webhook and POST /webhook/{source} ingress endpoints."""

    async def handle_webhook(req: HTTPRequest, source: str = "default") -> HTTPResponse:
        # 1. Cryptographic Authentication & Security Validation
        # MUST happen before ANY database interaction. Rejections produce ZERO DB writes.
        val = validate_request_security(req, config.security)
        if not val.is_valid:
            return HTTPResponse.error(val.message, status_code=val.status_code, reason=val.reason)

        # 2. Schema Validation: Parse & Validate JSON payload
        # MUST happen before ANY database interaction. Rejections produce ZERO DB writes.
        if not req.body:
            return HTTPResponse.error("Request body is empty", status_code=400, reason="empty_body")

        try:
            body_dict = json.loads(req.body.decode("utf-8"))
        except Exception:
            return HTTPResponse.error("Malformed JSON payload", status_code=400, reason="malformed_json")

        if not isinstance(body_dict, dict):
            return HTTPResponse.error("Payload must be a JSON object", status_code=400, reason="invalid_json_type")

        # 3. Determine Source and Pre-DB Task Schema Validation (Defect 4 remediation)
        # Priority: URL path param > payload "source" > "default"
        if not source or source == "default":
            source = body_dict.get("source") or "default"

        action_type = body_dict.get("action_type") or body_dict.get("action")
        if not action_type:
            if source in ("contact-review", "contact_review") or req.path.endswith("/contact-review"):
                action_type = "contact_review"
            elif source in ("antigravity", "slack_task", "slack-task") or req.path.endswith("/antigravity"):
                action_type = "antigravity"
            elif source in ("surecart", "surecart-acs", "surecart_acs", "stripe-acs", "stripe_acs") or "/surecart" in req.path:
                action_type = "cli"
            else:
                action_type = "cli"
        command = body_dict.get("command") or body_dict.get("target_action")
        if not command and action_type in ("contact_review", "review_contact", "contact-review"):
            command = "bin/webhook-hub review-contact"
        elif not command and action_type in ("antigravity", "agent_conversation", "antigravity_task"):
            command = "agentapi new-conversation"
        elif not command and (source in ("surecart", "surecart-acs", "surecart_acs", "stripe-acs", "stripe_acs") or "/surecart" in req.path):
            command = "python3 /Users/vecsatfoxmailcom/.agents/skills/stripe-agentic-commerce/scripts/stripe_acs_cli.py surecart sync"

        raw_priority = body_dict.get("priority", 0)
        try:
            priority_val = int(raw_priority)
        except (ValueError, TypeError):
            return HTTPResponse.error("Invalid 'priority' field: must be an integer", status_code=400, reason="invalid_task_schema")

        raw_timeout = body_dict.get("timeout_seconds")
        if raw_timeout is None and hasattr(config, "dispatch"):
            raw_timeout = getattr(config.dispatch, "default_action_timeout_seconds", 300)
        elif raw_timeout is None:
            raw_timeout = 300
        try:
            timeout_seconds_val = int(raw_timeout)
        except (ValueError, TypeError):
            return HTTPResponse.error("Invalid 'timeout_seconds' field: must be an integer", status_code=400, reason="invalid_task_schema")

        raw_max_retries = body_dict.get("max_retries", 0)
        try:
            max_retries_val = int(raw_max_retries)
        except (ValueError, TypeError):
            return HTTPResponse.error("Invalid 'max_retries' field: must be an integer", status_code=400, reason="invalid_task_schema")

        payload_hash = compute_payload_hash(req.body)

        # Header lookup for idempotency key
        idemp_key = (
            req.header("x-hub-event-id")
            or req.header("x-idempotency-key")
            or req.header("idempotency-key")
            or req.header("x-github-delivery")
            or req.header("x-webhook-id")
        )
        if not idemp_key:
            # Fallback deterministic key: hash(source:path:payload_hash)
            idemp_key = compute_payload_hash(f"{source}:{req.path}:{payload_hash}".encode())

        # 5. Dual-Layer Deduplication Check in Database
        if db is not None:
            existing_task = None
            if hasattr(db, "get_task_by_idempotency"):
                res = db.get_task_by_idempotency(source, idemp_key)
                existing_task = await res if asyncio.iscoroutine(res) else res
            if not existing_task and hasattr(db, "get_task_by_payload_hash"):
                res = db.get_task_by_payload_hash(source, payload_hash)
                existing_task = await res if asyncio.iscoroutine(res) else res

            if existing_task:
                tid = existing_task["task_id"] if isinstance(existing_task, dict) else getattr(existing_task, "task_id", None)
                eid = existing_task["event_id"] if isinstance(existing_task, dict) else getattr(existing_task, "event_id", None)
                st = existing_task["status"] if isinstance(existing_task, dict) else getattr(existing_task, "status", "accepted")
                if hasattr(st, "value"):
                    st = st.value
                return HTTPResponse.json(
                    {
                        "status": st,
                        "task_id": tid,
                        "event_id": eid,
                        "duplicate": True,
                        "deduplicated": True,
                    },
                    status_code=200,
                )

        # 6. Persist to SQLite SSOT
        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        task_id = f"tsk_{uuid.uuid4().hex[:16]}"
        is_duplicate = False

        if db is not None:
            event_record = {
                "event_id": event_id,
                "source": source,
                "idempotency_key": idemp_key,
                "payload_hash": payload_hash,
                "headers_json": json.dumps(dict(req.headers)),
                "raw_payload": req.text(),
                "method": req.method,
                "path": req.path,
                "remote_addr": req.remote_addr,
                "status": "received",
            }
            inserted = db.insert_webhook_event(event_record)
            if asyncio.iscoroutine(inserted):
                inserted = await inserted

            if not inserted:
                # Concurrent duplicate collision or retry of an existing event
                is_duplicate = True
                existing_event = None
                if hasattr(db, "get_event_by_idempotency"):
                    res = db.get_event_by_idempotency(source, idemp_key)
                    existing_event = await res if asyncio.iscoroutine(res) else res
                if not existing_event and hasattr(db, "get_event_by_payload_hash"):
                    res = db.get_event_by_payload_hash(source, payload_hash)
                    existing_event = await res if asyncio.iscoroutine(res) else res

                existing_event_id = None
                if existing_event:
                    existing_event_id = (
                        existing_event["event_id"]
                        if isinstance(existing_event, dict)
                        else getattr(existing_event, "event_id", None)
                    )

                existing_task = None
                if existing_event_id and hasattr(db, "get_task_by_event_id"):
                    res = db.get_task_by_event_id(existing_event_id)
                    existing_task = await res if asyncio.iscoroutine(res) else res

                if not existing_task and hasattr(db, "get_task_by_idempotency"):
                    res = db.get_task_by_idempotency(source, idemp_key)
                    existing_task = await res if asyncio.iscoroutine(res) else res

                if existing_task:
                    tid = existing_task["task_id"] if isinstance(existing_task, dict) else getattr(existing_task, "task_id", None)
                    eid = existing_task["event_id"] if isinstance(existing_task, dict) else getattr(existing_task, "event_id", existing_event_id)
                    st = existing_task["status"] if isinstance(existing_task, dict) else getattr(existing_task, "status", "accepted")
                    if hasattr(st, "value"):
                        st = st.value
                    return HTTPResponse.json(
                        {
                            "status": st,
                            "task_id": tid,
                            "event_id": eid,
                            "duplicate": True,
                            "deduplicated": True,
                        },
                        status_code=200,
                    )

                # If no task exists for that existing event (e.g. edge-case previous crash):
                # Attach new task referencing existing event_id to prevent foreign key failure!
                if existing_event_id:
                    event_id = existing_event_id

            # Insert task linked to event
            task_record = {
                "task_id": task_id,
                "event_id": event_id,
                "source": source,
                "action_type": action_type,
                "command": command,
                "target_action": command or "",
                "action_params_json": json.dumps(body_dict),
                "status": "queued",
                "priority": priority_val,
                "timeout_seconds": timeout_seconds_val,
                "retry_count": 0,
                "max_retries": max_retries_val,
            }
            res = db.insert_task(task_record)
            if asyncio.iscoroutine(res):
                await res

        # For Antigravity tasks originating from Slack, announce milestone 1 (collected) under thread
        if action_type in ("antigravity", "agent_conversation", "antigravity_task") and not source.endswith("_stress"):
            channel = body_dict.get("channel")
            ts = str(body_dict.get("ts") or body_dict.get("event_ts") or "")
            thread_ts = body_dict.get("thread_ts")
            root_ts = str(thread_ts or ts or "")
            is_follow_up = bool(thread_ts and str(thread_ts) != ts)
            if channel and root_ts and not is_follow_up:
                try:
                    from hub.antigravity.prompt_builder import extract_slash_commands
                    from hub.antigravity.thread_notifier import ThreadNotifier
                    _, cmds, _ = extract_slash_commands(body_dict.get("text") or "")
                    files_cnt = len(body_dict.get("files") or [])
                    notifier = ThreadNotifier()
                    asyncio.create_task(
                        asyncio.to_thread(
                            notifier.notify_collected,
                            channel=channel,
                            thread_ts=root_ts,
                            task_id=task_id,
                            title=body_dict.get("title"),
                            commands=cmds,
                            files_count=files_cnt,
                        )
                    )
                except Exception as notify_err:
                    logger.debug("Failed to post collected comment: %s", notify_err)

        # 7. Asynchronously or Synchronously Execute Task
        is_sync = (
            getattr(req, "query_params", {}).get("sync", "").lower() in ("true", "1", "yes")
            or body_dict.get("sync") is True
        )
        if is_sync and dispatcher is not None:
            exec_res = await dispatcher.execute_task(task_id)
            latest_task = db.get_task(task_id) if db else {}
            if asyncio.iscoroutine(latest_task):
                latest_task = await latest_task
            resp_body = {
                "status": exec_res.status,
                "event_id": event_id,
                "task_id": task_id,
                "action_type": action_type,
                "exit_code": exec_res.exit_code,
                "stdout": exec_res.stdout,
                "stderr": exec_res.stderr,
                "task": latest_task,
            }
            if getattr(exec_res, "result_data", None) is not None:
                resp_body["review_result"] = exec_res.result_data
            return HTTPResponse.json(
                resp_body,
                status_code=200 if exec_res.status == "succeeded" else 500,
            )

        if dispatcher is not None:
            await dispatcher.enqueue(task_id)

        if broker is not None:
            try:
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

        # 8. Immediate HTTP 202 Accepted Response
        return HTTPResponse.json(
            {
                "status": "accepted",
                "event_id": event_id,
                "task_id": task_id,
                "duplicate": is_duplicate,
            },
            status_code=202,
        )

    # Register handlers for both /webhook and /webhook/{source}
    server.add_route("POST", "/webhook", handle_webhook)
    server.add_route("POST", "/webhook/{source}", handle_webhook)

    # Ensure tasks routes are registered via hub.routes.tasks per PROJECT.md (if no lazy fallback resolver)
    if getattr(server, "_fallback_route_resolver", None) is None and ("GET", "/tasks") not in server._exact_routes:
        try:
            from hub.routes.tasks import register_task_routes
            register_task_routes(server, config, db, dispatcher=dispatcher, broker=broker)
        except ImportError:
            pass
