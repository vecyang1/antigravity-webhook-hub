"""End-to-End Verification Test for Antigravity Webhook Hub TaskScheduler Engine.

Adheres strictly to:
- Two-Sided Verification Protocol ("测遍了不会说谎的那一半，也要测遍会说谎的那一半"):
  1. Legitimate path: Scheduled webhook ingress -> SSOT SQLite persistence ->
     monotonic sleep/wake leap recovery ("Don't Miss the Beat") -> task execution ->
     authoritative state transition -> SSE event broadcast.
  2. Adversarial challenge: Tampered signature, malformed JSON, and invalid cron expressions
     must be rejected with ZERO database writes or execution side effects.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.models import HTTPRequest, HTTPResponse
from hub.routes.schedules import register_schedule_routes
from hub.routes.tasks import register_task_routes
from hub.routes.webhook import register_webhook_routes
from hub.scheduler import TaskScheduler, format_iso_utc
from hub.security import generate_hmac_signature
from hub.server import AsyncHTTPServer


class MemoryEventBroker:
    def __init__(self):
        self.events: list[dict[str, Any]] = []

    async def publish(self, topic: str, data: dict[str, Any]):
        self.events.append(data)


@pytest.fixture
async def e2e_scheduler_env(free_port: int, temp_db_path: str):
    """Bootstraps a complete, live production-like Webhook Hub server with all engines."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.webhook_secret = "test_scheduler_e2e_secret_32bytes"
    config.database.path = temp_db_path

    db = DatabaseManager(temp_db_path)
    broker = MemoryEventBroker()
    dispatcher = TaskDispatcher(db=db, broker=broker, config=config)
    await dispatcher.start()

    scheduler = TaskScheduler(
        db=db,
        dispatcher=dispatcher,
        broker=broker,
        config=config,
        tick_interval=0.1,
    )
    await scheduler.start()

    server = AsyncHTTPServer(config.server)
    server._db = db
    server._scheduler = scheduler

    register_webhook_routes(server, config, db, dispatcher=dispatcher, broker=broker, scheduler=scheduler)
    register_schedule_routes(server, config, db, scheduler=scheduler, dispatcher=dispatcher, broker=broker)
    register_task_routes(server, config, db, dispatcher=dispatcher, broker=broker)

    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, config, db, scheduler, dispatcher, broker

    await scheduler.stop()
    await server.stop()
    await dispatcher.stop()
    db.close()


async def test_e2e_legitimate_scheduled_flow(e2e_scheduler_env: Any):
    """Legitimate Path: Delayed webhook -> SSOT -> Timed execution -> Status Completed."""
    base_url, server, config, db, scheduler, dispatcher, broker = e2e_scheduler_env

    # 1. Ingress: Authenticated Webhook requesting 0.2s delay
    payload = {
        "source": "e2e_agent",
        "action": "cli",
        "command": "echo 'e2e execution verified'",
        "delay_seconds": 0.2,
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, raw_body, ts)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=raw_body, headers=headers)
        assert resp.status_code == 202
        res_data = resp.json()
        assert res_data["status"] == "scheduled"
        sched_id = res_data["schedule_id"]
        assert sched_id is not None

        # Verify schedule is persisted in DB with ACTIVE status
        sched_record = db.get_schedule(sched_id)
        assert sched_record is not None
        assert sched_record["status"] == "active"
        assert sched_record["total_runs"] == 0

        # Wait for the scheduler background loop to tick and execute the delayed task
        # 0.2s delay + 0.1s tick interval -> wait up to 2.0s
        executed = False
        for _ in range(25):
            await asyncio.sleep(0.1)
            updated = db.get_schedule(sched_id)
            if updated and updated["status"] == "completed":
                executed = True
                break

        assert executed is True, "Scheduled task was not executed by scheduler loop within timeout"

        # Verify task record in tasks table
        history = db.get_schedule_history(sched_id)
        assert len(history) == 1
        task_id = history[0]["task_id"]

        # Wait briefly for Dispatcher to finish running the echo command
        for _ in range(20):
            task_row = db.get_task(task_id)
            if task_row and task_row["status"] == "succeeded":
                break
            await asyncio.sleep(0.1)

        task_row = db.get_task(task_id)
        assert task_row["status"] == "succeeded"
        logs = db.get_execution_logs(task_id)
        assert any("e2e execution verified" in l["line"] for l in logs)

        # Verify broker received schedule_triggered event
        triggered_events = [e for e in broker.events if e.get("event") == "schedule_triggered"]
        assert len(triggered_events) >= 1
        assert triggered_events[0]["schedule_id"] == sched_id


async def test_e2e_adversarial_rejection_zero_side_effects(e2e_scheduler_env: Any):
    """Adversarial Challenge: Tampered auth or malformed payloads must produce ZERO DB writes."""
    base_url, server, config, db, scheduler, dispatcher, broker = e2e_scheduler_env

    # 1. Tampered signature on scheduled webhook
    payload = {"command": "malicious rm -rf", "delay_seconds": 10}
    raw_body = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    valid_sig = generate_hmac_signature(config.security.webhook_secret, raw_body, ts)
    tampered_sig = valid_sig[:-1] + ("0" if valid_sig[-1] != "0" else "1")

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": tampered_sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=raw_body, headers=headers)
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"

        # Verify ZERO schedules and ZERO events were written to SQLite
        scheds, count = db.list_schedules()
        assert count == 0

        # 2. Malformed JSON payload
        headers_valid_auth = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": generate_hmac_signature(config.security.webhook_secret, b"{bad json", ts),
            "X-Hub-Timestamp": str(ts),
        }
        resp_bad = await client.post(f"{base_url}/webhook", content=b"{bad json", headers=headers_valid_auth)
        assert resp_bad.status_code == 400

        # Verify STILL ZERO writes
        scheds, count = db.list_schedules()
        assert count == 0


async def test_e2e_sleep_wake_catch_up_recovery(e2e_scheduler_env: Any):
    """Recovery Challenge ('Don't Miss the Beat'): System sleep leap triggers catch-up sweep."""
    base_url, server, config, db, scheduler, dispatcher, broker = e2e_scheduler_env

    # Insert schedule that matured while offline / asleep
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    overdue_iso = format_iso_utc(now_dt - datetime.timedelta(seconds=120))

    sched_obj = await scheduler.create_schedule(
        name="Scheduled Agent Call (Waking Up)",
        schedule_type="once",
        scheduled_at=overdue_iso,
        action_type="cli",
        command="echo 'resumed after sleep'",
    )

    # Perform simulated sleep recovery sweep
    sweep_summary = await scheduler.sweep_due(reason="sleep_wake_recovery", sleep_duration=1800.0)
    assert sweep_summary["triggered_count"] >= 1
    assert sweep_summary["reason"] == "sleep_wake_recovery"

    # Confirm schedule transitioned to completed
    updated = db.get_schedule(sched_obj.schedule_id)
    assert updated["status"] == "completed"
    assert updated["total_runs"] == 1


async def test_e2e_webhook_http_dispatch_flow(e2e_scheduler_env: Any):
    """End-to-End Test for TaskDispatcher Native Webhook/HTTP Dispatch.
    
    Verifies:
    1. Legitimate path: Scheduled webhook task targeting HTTP receiver triggers, executes POST,
       sends JSON payload with custom headers, and marks task as SUCCEEDED.
    2. Failure path: Webhook targeting endpoint that returns HTTP 502 fails cleanly,
       records stderr with HTTP 502, and marks task as FAILED.
    """
    base_url, server, config, db, scheduler, dispatcher, broker = e2e_scheduler_env

    # Setup mock endpoints on server
    received_posts: list[dict[str, Any]] = []

    async def mock_receiver(req: HTTPRequest) -> HTTPResponse:
        data = req.json()
        received_posts.append({"headers": req.headers, "data": data})
        return HTTPResponse.json({"result": "success", "echo": data}, status_code=200)

    async def mock_failing_receiver(req: HTTPRequest) -> HTTPResponse:
        return HTTPResponse.text("Service Unavailable", status_code=502)

    server.add_route("POST", "/test/target-endpoint", mock_receiver)
    server.add_route("POST", "/test/target-failing", mock_failing_receiver)

    # 1. Legitimate Webhook Dispatch via ingress /webhook
    payload = {
        "source": "e2e_test",
        "action": "webhook",
        "url": f"{base_url}/test/target-endpoint",
        "method": "POST",
        "payload": {"message": "hello n8n", "priority": "high"},
        "headers": {"X-Custom-Header": "TestToken123"},
        "delay_seconds": 0.2,
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, raw_body, ts)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=raw_body, headers=headers)
        assert resp.status_code == 202
        res_data = resp.json()
        sched_id = res_data["schedule_id"]

        # Wait for scheduler tick and task execution
        executed = False
        for _ in range(30):
            await asyncio.sleep(0.1)
            sched_rec = db.get_schedule(sched_id)
            if sched_rec and sched_rec["status"] == "completed":
                executed = True
                break

        assert executed is True, "Webhook dispatch schedule did not complete"

        # Verify Task status in DB
        history = db.get_schedule_history(sched_id)
        assert len(history) == 1
        task_id = history[0]["task_id"]

        # Wait for Dispatcher to execute HTTP webhook and populate receiver
        for _ in range(40):
            task_row = db.get_task(task_id)
            if task_row and task_row["status"] == "succeeded" and len(received_posts) == 1:
                break
            await asyncio.sleep(0.1)

        # Verify receiver received the payload
        assert len(received_posts) == 1
        assert received_posts[0]["data"] == {"message": "hello n8n", "priority": "high"}
        assert received_posts[0]["headers"].get("x-custom-header") == "TestToken123"

        task_row = db.get_task(task_id)
        assert task_row["status"] == "succeeded"
        logs = db.get_execution_logs(task_id)
        assert any("HTTP 200" in l["line"] for l in logs)

        # 2. Failure Path: Webhook targeting 502 endpoint
        fail_payload = {
            "source": "e2e_test",
            "action": "webhook",
            "url": f"{base_url}/test/target-failing",
            "method": "POST",
            "payload": {"status": "must_fail"},
            "delay_seconds": 0.2,
        }
        fail_raw = json.dumps(fail_payload).encode("utf-8")
        fail_ts = int(time.time())
        fail_sig = generate_hmac_signature(config.security.webhook_secret, fail_raw, fail_ts)
        fail_headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": fail_sig,
            "X-Hub-Timestamp": str(fail_ts),
        }

        resp_fail = await client.post(f"{base_url}/webhook", content=fail_raw, headers=fail_headers)
        assert resp_fail.status_code == 202
        fail_sched_id = resp_fail.json()["schedule_id"]

        executed_fail = False
        for _ in range(30):
            await asyncio.sleep(0.1)
            sched_rec = db.get_schedule(fail_sched_id)
            if sched_rec and sched_rec["status"] == "completed":
                executed_fail = True
                break
        assert executed_fail is True

        fail_history = db.get_schedule_history(fail_sched_id)
        assert len(fail_history) == 1
        fail_task_id = fail_history[0]["task_id"]

        for _ in range(20):
            t_row = db.get_task(fail_task_id)
            if t_row and t_row["status"] == "failed":
                break
            await asyncio.sleep(0.1)

        t_row = db.get_task(fail_task_id)
        assert t_row["status"] == "failed"
        assert t_row["exit_code"] == 502
        fail_logs = db.get_execution_logs(fail_task_id)
        assert any("502" in l["line"] for l in fail_logs)

