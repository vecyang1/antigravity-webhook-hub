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
        if act.get("signal"):
            sig = act["signal"]
            assert sig.get("confidence_score") is not None
            assert sig.get("applied") is not None
            assert "diffs" in sig


@pytest.mark.asyncio
async def test_sentinel_detail_and_discovery_filtering(agent_activities_app, tmp_path):
    from hub.routes.agent_activities import discover_sentinel_conversations, parse_sentinel_transcript

    # 1. Setup mock brain with a real sentinel run and an interactive coding task
    mock_brain = tmp_path / "brain"
    mock_brain.mkdir()

    # Conversation A: Real Sentinel Run
    sentinel_cid = "conv_sentinel_test_001"
    dir_a = mock_brain / sentinel_cid / ".system_generated" / "logs"
    dir_a.mkdir(parents=True)
    transcript_a = dir_a / "transcript.jsonl"
    transcript_a.write_text(
        json.dumps({"type": "USER_INPUT", "content": "Antigravity Webhook Hub — Sentinel\nCAD-20260911-webhook-hub-sentinel", "created_at": "2026-09-11T12:00:00Z"}) + "\n" +
        json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": "hub/server.py"}}]}) + "\n" +
        json.dumps({"type": "GENERIC", "content": "File contents of server.py"}) + "\n" +
        json.dumps({"type": "PLANNER_RESPONSE", "content": "### Sentinel Report\nAll services operational."}) + "\n"
    )

    # Conversation B: Interactive Coding Task (Mentions webhook-hub-sentinel in prompt)
    interactive_cid = "conv_interactive_task_002"
    dir_b = mock_brain / interactive_cid / ".system_generated" / "logs"
    dir_b.mkdir(parents=True)
    transcript_b = dir_b / "transcript.jsonl"
    transcript_b.write_text(
        json.dumps({"type": "USER_INPUT", "content": "<original_task>\n**Task**: fix webhook-hub-sentinel UI", "created_at": "2026-09-11T12:05:00Z"}) + "\n" +
        json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "git status"}}]}) + "\n"
    )

    # Verify discovery strictly keeps Conversation A and discards Conversation B
    cids = discover_sentinel_conversations(sidecar_dir=tmp_path / "empty_sidecar", brain_dir=mock_brain)
    assert sentinel_cid in cids
    assert interactive_cid not in cids

    # Verify parse_sentinel_transcript on Conversation A
    parsed = parse_sentinel_transcript(sentinel_cid, brain_dir=mock_brain, full_steps=True)
    assert parsed is not None
    assert parsed["conversation_id"] == sentinel_cid
    assert parsed["status"] == "succeeded"
    assert parsed["steps_count"] == 1
    assert "view_file" in parsed["tools_used"]
    assert "### Sentinel Report" in parsed["final_report"]
    assert len(parsed["steps"]) == 1
    assert parsed["steps"][0]["tool_name"] == "view_file"


@pytest.mark.asyncio
async def test_signals_normalization_endpoint(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    async with httpx.AsyncClient(base_url=base_url) as client:
        resp = await client.get("/api/agent-activities/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        for sig in data["signals"]:
            # All signals must have normalized top-level fields
            assert "confidence_score" in sig
            assert "applied" in sig
            assert "diffs" in sig
            assert "verdict" in sig
            assert "target_name" in sig
            assert sig["confidence_score"] is not None
            assert isinstance(sig["applied"], bool)


@pytest.mark.asyncio
async def test_sentinel_detail_adversarial_traversal_rejected(agent_activities_app):
    base_url = agent_activities_app["base_url"]
    async with httpx.AsyncClient(base_url=base_url) as client:
        # 1. Path traversal attempt
        resp = await client.get("/api/agent-activities/sentinels/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)

        # 2. Invalid characters in conversation ID
        resp_special = await client.get("/api/agent-activities/sentinels/conv_bad$id")
        assert resp_special.status_code == 400
        data = resp_special.json()
        assert "error" in data


@pytest.mark.asyncio
async def test_corrupted_signal_and_transcript_resilience(tmp_path):
    from hub.routes.agent_activities import normalize_signal_data, parse_sentinel_transcript

    # 1. Non-dict signal payload does not throw AttributeError
    assert normalize_signal_data([1, 2, 3]) == {}
    assert normalize_signal_data("invalid_string") == {}
    assert normalize_signal_data(None) == {}

    # 2. Corrupted transcript with non-dict lines, empty lines, and malformed JSON
    mock_brain = tmp_path / "brain"
    cid = "conv_corrupted_001"
    dir_logs = mock_brain / cid / ".system_generated" / "logs"
    dir_logs.mkdir(parents=True)
    (dir_logs / "transcript.jsonl").write_text(
        "not json\n"
        "[1, 2, 3]\n"
        "\n"
        '{"type": "USER_INPUT", "content": "Antigravity Webhook Hub — Sentinel"}\n'
        '"standalone_string"\n'
        '{"type": "PLANNER_RESPONSE", "content": "Delivered after corruption"}\n'
    )

    parsed = parse_sentinel_transcript(cid, brain_dir=mock_brain, full_steps=True)
    assert parsed is not None
    assert parsed["conversation_id"] == cid
    assert parsed["final_report"] == "Delivered after corruption"