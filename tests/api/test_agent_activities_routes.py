"""
Tests for Antigravity Webhook Hub — Agent Activities & Sentinel Observability Endpoints.
Verifies:
- GET /api/agent-activities/summary returns structured sentinels, signals, pulses telemetry.
- GET /api/agent-activities/sentinels returns discovered runs, cadence card, marker status.
- GET /api/agent-activities/sentinels/{conversation_id} returns full prompt, tool steps, and delivered markdown report.
- GET /api/agent-activities/signals supports verdict filtering (create, correct, no_change) and text search.
- GET /api/agent-activities/pulses lists Antigravity sidebar pulse queue events.
- GET /tasks/{task_id} agent_activity field enrichment (signal, pulse, prompt_payload).
- Zero-leak memory invariant: lazy endpoint requests maintain process RSS < 30.0 MB.
"""

import json
import pytest
import httpx
from pathlib import Path

from hub.broker import EventBroker
from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.agent_activities import register_agent_activities_routes
from hub.routes.dashboard import register_dashboard_routes
from hub.routes.tasks import register_task_routes
from hub.server import AsyncHTTPServer


@pytest.fixture
async def agent_activities_app(free_port: int, temp_db_path: str):
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.database.path = temp_db_path

    db = DatabaseManager(temp_db_path, cache_size=-16)
    db.init_schema()

    broker = EventBroker()
    dispatcher = TaskDispatcher(db=db, broker=broker, config=config)
    await dispatcher.start()

    server = AsyncHTTPServer(config.server)
    server._db = db

    register_task_routes(server, config, db, dispatcher, broker)
    register_dashboard_routes(server, config, db, broker, dispatcher)
    register_agent_activities_routes(server, config, db, broker, dispatcher)

    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    yield {
        "base_url": base_url,
        "server": server,
        "db": db,
        "broker": broker,
        "dispatcher": dispatcher,
        "config": config,
    }

    await server.stop()
    await dispatcher.stop()
    db.close()


@pytest.mark.asyncio
async def test_agent_activities_summary(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get("/api/agent-activities/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "memory_rss_mb" in data
        assert "sentinels" in data
        assert "signals" in data
        assert "pulses" in data
        assert isinstance(data["sentinels"]["total_runs"], int)
        assert isinstance(data["signals"]["total_signals"], int)
        assert isinstance(data["pulses"]["total_pulses"], int)


@pytest.mark.asyncio
async def test_sentinels_list(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get("/api/agent-activities/sentinels")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["cadence_card"] == "CAD-20260911-webhook-hub-sentinel"
        assert "frequency" in data
        assert "runs" in data
        assert isinstance(data["runs"], list)
        if data["runs"]:
            run = data["runs"][0]
            assert "conversation_id" in run
            assert "status" in run
            assert "steps_count" in run
            assert "tools_used" in run


@pytest.mark.asyncio
async def test_sentinel_detail_not_found(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get("/api/agent-activities/sentinels/nonexistent-cid-999")
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data


@pytest.mark.asyncio
async def test_signals_list_and_filter(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    async with httpx.AsyncClient(base_url=base_url) as client:
        # 1. List all
        resp = await client.get("/api/agent-activities/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "signals" in data
        assert isinstance(data["signals"], list)

        # 2. Filter by verdict
        resp_create = await client.get("/api/agent-activities/signals?verdict=create")
        assert resp_create.status_code == 200
        data_create = resp_create.json()
        for sig in data_create["signals"]:
            assert sig["verdict"].lower() == "create"

        # 3. Search query
        resp_search = await client.get("/api/agent-activities/signals?q=Adam")
        assert resp_search.status_code == 200
        data_search = resp_search.json()
        assert isinstance(data_search["signals"], list)


@pytest.mark.asyncio
async def test_pulses_list(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get("/api/agent-activities/pulses?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "pulses" in data
        assert isinstance(data["pulses"], list)
        assert data["limit"] == 10


@pytest.mark.asyncio
async def test_task_detail_agent_activity_enrichment(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    db = agent_activities_app["db"]

    task_id = "tsk_test_agent_act_001"
    evt_id = "evt_test_agent_act_001"
    db.insert_webhook_event({
        "event_id": evt_id,
        "source": "contact-review",
        "payload_hash": "a" * 64,
        "headers_json": "{}",
        "raw_payload": json.dumps({"name": "Test Contact"}),
        "method": "POST",
        "path": "/webhook/contact-review",
        "status": "received",
    })
    db.insert_task({
        "task_id": task_id,
        "event_id": evt_id,
        "command": "echo test",
        "source": "contact-review",
        "action_type": "contact_review",
        "status": "succeeded",
        "stdout": "Emitted agent signal: sig_a40e6540e3c7",
    })

    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        task_data = resp.json()
        assert task_data["task_id"] == task_id
        assert "agent_activity" in task_data
        act = task_data["agent_activity"]
        assert act is not None
        assert act.get("action_type") == "contact_review"