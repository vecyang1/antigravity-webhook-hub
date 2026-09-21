"""API tests for /schedules and scheduled webhook ingress endpoints.

Verifies:
- GET /schedules, GET /schedules/summary
- POST /schedules (one-off delayed and recurring cron)
- GET /schedules/{id}, POST /trigger, /pause, /resume, DELETE
- POST /schedules/sweep
- POST /webhook with schedule_at, delay_seconds, delay, cron
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.routes.schedules import register_schedule_routes
from hub.routes.webhook import register_webhook_routes
from hub.scheduler import TaskScheduler
from hub.security import generate_hmac_signature
from hub.server import AsyncHTTPServer


class MockDispatcher:
    def __init__(self):
        self.enqueued_tasks = []

    async def enqueue(self, task_id: str):
        self.enqueued_tasks.append(task_id)


class MockBroker:
    def __init__(self):
        self.events = []

    async def publish(self, topic: str, data: dict):
        self.events.append((topic, data))


@pytest.fixture
async def schedules_server(free_port: int, temp_db_path: str):
    """Starts an AsyncHTTPServer with DB, TaskScheduler, Schedules & Webhook routes registered."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.webhook_secret = "test_schedules_secret_key_32bytes"
    config.database.path = temp_db_path

    db = DatabaseManager(temp_db_path)
    dispatcher = MockDispatcher()
    broker = MockBroker()
    scheduler = TaskScheduler(db=db, dispatcher=dispatcher, broker=broker, config=config)

    server = AsyncHTTPServer(config.server)
    server._db = db
    server._scheduler = scheduler

    register_schedule_routes(server, config, db, scheduler=scheduler, dispatcher=dispatcher, broker=broker)
    register_webhook_routes(server, config, db, dispatcher=dispatcher, broker=broker, scheduler=scheduler)

    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, config, db, scheduler, dispatcher, broker

    await server.stop()
    db.close()


async def test_schedules_list_and_summary_empty(schedules_server: Any):
    base_url, server, config, db, scheduler, dispatcher, broker = schedules_server
    async with httpx.AsyncClient() as client:
        # GET /schedules
        resp = await client.get(f"{base_url}/schedules")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["schedules"] == []
        assert data["total"] == 0

        # GET /schedules/summary
        resp_sum = await client.get(f"{base_url}/schedules/summary")
        assert resp_sum.status_code == 200
        sum_data = resp_sum.json()
        assert sum_data["status"] == "success"
        assert sum_data["stats"]["total"] == 0


async def test_create_and_manage_schedules(schedules_server: Any):
    base_url, server, config, db, scheduler, dispatcher, broker = schedules_server
    async with httpx.AsyncClient() as client:
        # 1. POST /schedules (One-off delayed)
        body_once = {
            "name": "Anker Reminder",
            "schedule_type": "once",
            "delay": "2d",
            "action_type": "cli",
            "command": "python3 scripts/check_anker_reminder.py",
            "source": "spark_reminder",
        }
        resp = await client.post(f"{base_url}/schedules", json=body_once)
        assert resp.status_code == 201
        once_data = resp.json()
        sched_id = once_data["schedule_id"]
        assert sched_id.startswith("sch_")
        assert once_data["next_run_at"] is not None

        # 2. POST /schedules (Recurring cron)
        body_cron = {
            "name": "Daily Health Check",
            "schedule_type": "recurring",
            "cron": "0 9 * * *",
            "action_type": "cli",
            "command": "echo health",
            "source": "cron_worker",
        }
        resp_cron = await client.post(f"{base_url}/schedules", json=body_cron)
        assert resp_cron.status_code == 201
        cron_data = resp_cron.json()
        cron_id = cron_data["schedule_id"]

        # 3. GET /schedules - verify both exist
        resp_list = await client.get(f"{base_url}/schedules")
        assert resp_list.status_code == 200
        list_data = resp_list.json()
        assert list_data["total"] == 2
        assert len(list_data["schedules"]) == 2

        # 4. GET /schedules/{schedule_id}
        resp_detail = await client.get(f"{base_url}/schedules/{sched_id}")
        assert resp_detail.status_code == 200
        detail_data = resp_detail.json()
        assert detail_data["schedule"]["name"] == "Anker Reminder"

        # 5. POST /schedules/{schedule_id}/trigger (Trigger immediate on-demand execution)
        resp_trig = await client.post(f"{base_url}/schedules/{sched_id}/trigger")
        assert resp_trig.status_code == 202
        assert len(dispatcher.enqueued_tasks) == 1

        # 6. POST /schedules/{cron_id}/pause
        resp_pause = await client.post(f"{base_url}/schedules/{cron_id}/pause")
        assert resp_pause.status_code == 200
        assert resp_pause.json()["status"] == "paused"

        # 7. POST /schedules/{cron_id}/resume
        resp_resume = await client.post(f"{base_url}/schedules/{cron_id}/resume")
        assert resp_resume.status_code == 200
        assert resp_resume.json()["status"] == "active"

        # 8. DELETE /schedules/{sched_id}
        resp_del = await client.delete(f"{base_url}/schedules/{sched_id}")
        assert resp_del.status_code == 200
        assert resp_del.json()["status"] == "deleted"

        # Verify list now has 1
        resp_list_after = await client.get(f"{base_url}/schedules")
        assert resp_list_after.json()["total"] == 1


async def test_schedules_validation_errors(schedules_server: Any):
    base_url, server, config, db, scheduler, dispatcher, broker = schedules_server
    async with httpx.AsyncClient() as client:
        # Missing name
        resp = await client.post(f"{base_url}/schedules", json={"schedule_type": "once"})
        assert resp.status_code == 400

        # Invalid schedule_type
        resp_type = await client.post(f"{base_url}/schedules", json={"name": "Test", "schedule_type": "hourly"})
        assert resp_type.status_code == 400

        # Recurring missing cron
        resp_nocron = await client.post(f"{base_url}/schedules", json={"name": "Test", "schedule_type": "recurring"})
        assert resp_nocron.status_code == 400


async def test_webhook_ingress_with_scheduling(schedules_server: Any):
    """Verify that incoming webhooks with schedule_at or delay_seconds are scheduled in SSOT."""
    base_url, server, config, db, scheduler, dispatcher, broker = schedules_server

    # 1. Webhook with delay_seconds
    body_delay = {
        "source": "slack_agent",
        "action": "antigravity",
        "prompt": "Remind me in 2 days",
        "delay_seconds": 172800,
    }
    raw_body = json.dumps(body_delay).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, raw_body, ts)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook/agent", content=raw_body, headers=headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "scheduled"
        assert "schedule_id" in data
        assert data["schedule_id"].startswith("sch_")
        assert data["next_run_at"] is not None

        # Verify nothing was immediately enqueued to dispatcher!
        assert len(dispatcher.enqueued_tasks) == 0

        # 2. Webhook with cron expression header
        body_cron = {
            "source": "cron_webhook",
            "action": "cli",
            "command": "echo cron-from-header",
        }
        raw_cron = json.dumps(body_cron).encode("utf-8")
        ts = int(time.time())
        sig_cron = generate_hmac_signature(config.security.webhook_secret, raw_cron, ts)
        headers_cron = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig_cron,
            "X-Hub-Timestamp": str(ts),
            "X-Cron": "0 9 * * *",
        }

        resp_cron = await client.post(f"{base_url}/webhook", content=raw_cron, headers=headers_cron)
        assert resp_cron.status_code == 202
        cron_data = resp_cron.json()
        assert cron_data["status"] == "scheduled"
        assert cron_data["schedule_type"] == "recurring"
        assert cron_data["cron_expression"] == "0 9 * * *"


async def test_schedules_sweep_endpoint(schedules_server: Any):
    base_url, server, config, db, scheduler, dispatcher, broker = schedules_server
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/schedules/sweep")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "sweep" in data
        assert "triggered_count" in data["sweep"]
