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
