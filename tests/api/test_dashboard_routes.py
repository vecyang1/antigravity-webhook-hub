"""
Tests for Antigravity Webhook Hub Observable Activity Dashboard & Sidebar Integration.
Verifies:
- GET /dashboard and GET /ui deliver the zero-dependency embedded SPA HTML.
- GET / content negotiation (HTML vs JSON).
- GET /tasks/summary & GET /activities/summary direct SQLite SSOT aggregations.
- GET /tasks advanced filtering (source, status, action_type, search query q).
- POST /tasks/{id}/rerun lifecycle & state transition.
- Antigravity Sidecar Sentinel event logging in ~/.gemini/antigravity/sidecar_data/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from hub.broker import EventBroker
from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.dashboard import register_dashboard_routes
from hub.routes.observability import register_observability_routes
from hub.routes.sse import register_sse_routes
from hub.routes.tasks import register_task_routes
from hub.routes.webhook import register_webhook_routes
from hub.server import AsyncHTTPServer


@pytest.fixture
async def dashboard_test_app(free_port: int, temp_db_path: str):
    """Start full test server with all wired routes, dispatcher, and broker."""
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

    register_webhook_routes(server, config, db, dispatcher, broker)
    register_task_routes(server, config, db, dispatcher, broker)
    register_observability_routes(server, config, db, broker, dispatcher)
    register_sse_routes(server, config, db, broker)
    register_dashboard_routes(server, config, db, broker, dispatcher)

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

    await dispatcher.stop()
    await server.stop()
    db.close()


async def test_dashboard_and_ui_html_rendering(dashboard_test_app: dict[str, Any]):
    """Verify /dashboard and /ui return dark slate HTML with inline SVGs and zero emoji icons."""
    base_url = dashboard_test_app["base_url"]

    async with httpx.AsyncClient() as client:
        # 1. /dashboard
        resp = await client.get(f"{base_url}/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        body = resp.text
        assert "Antigravity Webhook Hub" in body
        assert "Observable Activity Console" in body
        assert "sidebar" in body
        assert "<svg" in body
        # Ensure no UI emojis are present
        assert "🚀" not in body
        assert "🎨" not in body

        # Ensure filter toggle and pill controls are present
        assert "toggleFilterBtn" in body
        assert "filter-pill-group" in body
        assert "pillReal" in body
        assert "pillTest" in body
        assert "statRealTasks" in body
        assert "badge-test" in body
        assert "badge-real" in body

        # 2. /ui alias
        resp_ui = await client.get(f"{base_url}/ui")
        assert resp_ui.status_code == 200
        assert "text/html" in resp_ui.headers["content-type"]
        assert resp_ui.text == body


async def test_root_content_negotiation(dashboard_test_app: dict[str, Any]):
    """Verify GET / returns HTML for browser navigation and JSON for machine API calls."""
    base_url = dashboard_test_app["base_url"]

    async with httpx.AsyncClient() as client:
        # Browser request with Accept: text/html
        resp_html = await client.get(f"{base_url}/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert resp_html.status_code == 200
        assert "text/html" in resp_html.headers["content-type"]
        assert "Antigravity Webhook Hub" in resp_html.text

        # API request without text/html
        resp_json = await client.get(f"{base_url}/", headers={"Accept": "application/json"})
        assert resp_json.status_code == 200
        assert "application/json" in resp_json.headers["content-type"]
        data = resp_json.json()
        assert data.get("service") == "Antigravity Webhook Hub"
        assert "/dashboard" in data.get("endpoints", {}).values()


async def test_activities_and_tasks_summary_api(dashboard_test_app: dict[str, Any]):
    """Verify GET /tasks/summary and GET /activities/summary derive stats from SQLite SSOT."""
    base_url = dashboard_test_app["base_url"]
    db: DatabaseManager = dashboard_test_app["db"]

    # Seed events and tasks
    for i, status in enumerate(["queued", "running", "succeeded", "failed"]):
        evt_id = f"evt_seed_{i}"
        task_id = f"tsk_seed_{i}"
        src = "agent_signal" if i % 2 == 0 else "contact_review"
        db.insert_webhook_event({
            "event_id": evt_id,
            "source": src,
            "payload_hash": f"{i}" * 64,
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": f"/webhook/{src}",
            "status": "received",
        })
        db.insert_task({
            "task_id": task_id,
            "event_id": evt_id,
            "command": f"echo seed {i}",
            "source": src,
            "action_type": "cli",
            "status": status,
        })

    async with httpx.AsyncClient() as client:
        for path in ["/tasks/summary", "/activities/summary"]:
            resp = await client.get(f"{base_url}{path}")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("status") == "success"
            assert data["total_tasks"] >= 4
            assert data["by_status"]["queued"] >= 1
            assert data["by_status"]["running"] >= 1
            assert data["by_status"]["succeeded"] >= 1
            assert data["by_status"]["failed"] >= 1
            assert "agent_signal" in data["by_source"]
            assert "contact_review" in data["by_source"]


async def test_tasks_filtering_and_search_query(dashboard_test_app: dict[str, Any]):
    """Verify /tasks search query q, status filter, and source filter."""
    base_url = dashboard_test_app["base_url"]
    db: DatabaseManager = dashboard_test_app["db"]

    db.insert_webhook_event({
        "event_id": "evt_search_1",
        "source": "uptime_kuma",
        "payload_hash": "e" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook/uptime_kuma",
        "status": "received",
    })
    db.insert_task({
        "task_id": "tsk_search_unique_xyz",
        "event_id": "evt_search_1",
        "command": "check_service_health",
        "source": "uptime_kuma",
        "action_type": "cli",
        "status": "succeeded",
    })

    async with httpx.AsyncClient() as client:
        # Search by unique task id substring
        resp_q = await client.get(f"{base_url}/tasks?q=unique_xyz")
        assert resp_q.status_code == 200
        data_q = resp_q.json()
        assert len(data_q["tasks"]) == 1
        assert data_q["tasks"][0]["task_id"] == "tsk_search_unique_xyz"

        # Search by command keyword
        resp_cmd = await client.get(f"{base_url}/tasks?q=check_service")
        assert resp_cmd.status_code == 200
        data_cmd = resp_cmd.json()
        assert any(t["task_id"] == "tsk_search_unique_xyz" for t in data_cmd["tasks"])

        # Filter by source
        resp_src = await client.get(f"{base_url}/tasks?source=uptime_kuma")
        assert resp_src.status_code == 200
        data_src = resp_src.json()
        assert all(t["source"] == "uptime_kuma" for t in data_src["tasks"])


async def test_task_rerun_end_to_end(dashboard_test_app: dict[str, Any]):
    """Verify POST /tasks/{id}/rerun resets task to queued and notifies dispatcher."""
    base_url = dashboard_test_app["base_url"]
    db: DatabaseManager = dashboard_test_app["db"]

    evt_id = "evt_rerun_e2e_1"
    task_id = "tsk_rerun_e2e_1"
    db.insert_webhook_event({
        "event_id": evt_id,
        "source": "cli",
        "payload_hash": "f" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook/cli",
        "status": "received",
    })
    db.insert_task({
        "task_id": task_id,
        "event_id": evt_id,
        "command": "echo rerun_test",
        "source": "cli",
        "action_type": "cli",
        "status": "failed",
        "exit_code": 127,
        "error_message": "command not found",
    })

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/tasks/{task_id}/rerun")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["task_id"] == task_id

        # Wait briefly for dispatcher execution of the rerun task
        for _ in range(20):
            task = db.get_task(task_id)
            if task["status"] in ("succeeded", "running"):
                break
            time.sleep(0.05)

        task = db.get_task(task_id)
        assert task["status"] in ("queued", "running", "succeeded")
        assert task["retry_count"] >= 1


async def test_agent_signal_sidecar_event_emission(dashboard_test_app: dict[str, Any], tmp_path: Path):
    """Verify task dispatcher emits Antigravity sidecar activity events for agent signals."""
    dispatcher: TaskDispatcher = dashboard_test_app["dispatcher"]
    db: DatabaseManager = dashboard_test_app["db"]

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()

    with patch("hub.dispatcher.Path.home", return_value=fake_home):
        evt_id = "evt_signal_test_1"
        task_id = "tsk_signal_test_1"
        db.insert_webhook_event({
            "event_id": evt_id,
            "source": "test_signal",
            "payload_hash": "9" * 64,
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": "/webhook",
            "status": "received",
        })
        db.insert_task({
            "task_id": task_id,
            "event_id": evt_id,
            "command": "signal",
            "target_action": str(tmp_path / "signals"),
            "source": "test_signal",
            "action_type": "agent_signal",
            "action_params": {"prompt": "Inspect high error rate on cluster"},
            "status": "queued",
        })

        # Run task execution via dispatcher
        await dispatcher.execute_task(task_id)

        # Verify sidecar event json file was created
        events_dir = fake_home / ".gemini" / "antigravity" / "sidecar_data" / "webhook-hub-sentinel" / "events"
        assert events_dir.exists(), f"Expected {events_dir} to exist"
        event_files = list(events_dir.glob("*.json"))
        assert len(event_files) >= 1

        content = json.loads(event_files[0].read_text(encoding="utf-8"))
        assert "payload" in content
        assert "newConversation" in content["payload"]
        prompt_text = content["payload"]["newConversation"]["prompt"]
        assert "Inspect high error rate" in prompt_text
        assert content["error"] == ""


async def test_cli_failure_sidecar_event_records_error(dashboard_test_app: dict[str, Any], tmp_path: Path):
    """Verify task dispatcher emits Antigravity sidecar activity events with error for failed tasks."""
    dispatcher: TaskDispatcher = dashboard_test_app["dispatcher"]
    db: DatabaseManager = dashboard_test_app["db"]

    fake_home = tmp_path / "fake_home_err"
    fake_home.mkdir()

    with patch("hub.dispatcher.Path.home", return_value=fake_home):
        evt_id = "evt_fail_test_1"
        task_id = "tsk_fail_test_1"
        db.insert_webhook_event({
            "event_id": evt_id,
            "source": "cli_monitor",
            "payload_hash": "8" * 64,
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": "/webhook",
            "status": "received",
        })
        db.insert_task({
            "task_id": task_id,
            "event_id": evt_id,
            "command": "python3 -c 'import sys; sys.exit(42)'",
            "source": "cli_monitor",
            "action_type": "cli",
            "status": "queued",
        })

        # Run task execution via dispatcher
        await dispatcher.execute_task(task_id)

        # Verify sidecar event json file was created with error populated
        events_dir = fake_home / ".gemini" / "antigravity" / "sidecar_data" / "webhook-hub-sentinel" / "events"
        assert events_dir.exists()
        event_files = list(events_dir.glob("*.json"))
        assert len(event_files) >= 1

        content = json.loads(event_files[0].read_text(encoding="utf-8"))
        assert "payload" in content
        assert content["error"] != ""
        assert "42" in content["error"]
        assert content["payload"]["newConversation"]["status"] == "failed"
        assert content["payload"]["newConversation"]["exitCode"] == 42


async def test_sse_global_events_receive_status_changed_and_sweeper(dashboard_test_app: dict[str, Any]):
    """Verify global events broker receives status_changed and sweeper_run events."""
    dispatcher: TaskDispatcher = dashboard_test_app["dispatcher"]
    broker: EventBroker = dashboard_test_app["broker"]
    db: DatabaseManager = dashboard_test_app["db"]

    q = broker.subscribe("events")
    try:
        evt_id = "evt_sse_test_1"
        task_id = "tsk_sse_test_1"
        db.insert_webhook_event({
            "event_id": evt_id,
            "source": "sse_source",
            "payload_hash": "7" * 64,
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": "/webhook",
            "status": "received",
        })
        db.insert_task({
            "task_id": task_id,
            "event_id": evt_id,
            "command": "echo sse_test_output",
            "source": "sse_source",
            "action_type": "cli",
            "status": "queued",
        })

        await dispatcher.execute_task(task_id)

        # Drain events received on global queue
        received_events = []
        for _ in range(5):
            try:
                msg = q.get_nowait()
                received_events.append(msg)
            except Exception:
                break

        # Check status_changed or completed is among events
        status_types = [m.get("event") or m.get("type") for m in received_events if isinstance(m, dict)]
        assert any(t in ("status_changed", "completed") for t in status_types), f"Got {status_types}"

        # Trigger sweep and verify sweeper_run received on events topic
        await dispatcher.sweep_unprocessed_tasks(reason="test_sse")
        sweep_events = []
        for _ in range(5):
            try:
                msg = q.get_nowait()
                sweep_events.append(msg)
            except Exception:
                break

        sweep_types = [m.get("event") or m.get("type") for m in sweep_events if isinstance(m, dict)]
        assert "sweeper_run" in sweep_types, f"Got {sweep_types}"

    finally:
        broker.unsubscribe("events", q)


async def test_tasks_test_event_filtering_and_classification(dashboard_test_app: dict[str, Any]):
    """Verify test event detection, SQL/in-memory filtering, and summary metrics."""
    base_url = dashboard_test_app["base_url"]
    db: DatabaseManager = dashboard_test_app["db"]

    # 1. Insert genuine production tasks
    db.insert_webhook_event({
        "event_id": "evt_real_prod_1",
        "source": "slack_people",
        "payload_hash": "a" * 64,
        "headers_json": "{}",
        "raw_payload": '{"contact": {"name": "Alice Wong", "company": "Inspire Labs"}}',
        "method": "POST",
        "path": "/webhook/slack_people",
        "status": "received",
    })
    db.insert_task({
        "task_id": "tsk_real_prod_1",
        "event_id": "evt_real_prod_1",
        "command": "bin/webhook-hub review-contact",
        "source": "slack_people",
        "action_type": "contact_review",
        "action_params_json": json.dumps({"contact": {"name": "Alice Wong", "company": "Inspire Labs"}}),
        "status": "succeeded",
    })

    # 2. Insert synthetic / test tasks
    # 2a. Echo command verification task
    db.insert_task({
        "task_id": "tsk_synth_echo",
        "event_id": "evt_synth_echo",
        "command": "echo 'step2_evt_verify_123456'",
        "target_action": "echo 'step2_evt_verify_123456'",
        "source": "default",
        "action_type": "cli",
        "action_params_json": "{}",
        "status": "succeeded",
    })
    # 2b. Nonce test task
    db.insert_task({
        "task_id": "tsk_synth_nonce",
        "event_id": "evt_synth_nonce",
        "command": "echo 'dedup_test'",
        "source": "default",
        "action_type": "cli",
        "action_params_json": json.dumps({"action": "cli", "nonce": "abc12345"}),
        "status": "succeeded",
    })
    # 2c. CLI test-send source task
    db.insert_task({
        "task_id": "tsk_synth_cli_send",
        "event_id": "evt_synth_cli_send",
        "command": "echo 'test payload'",
        "source": "cli_test_send",
        "action_type": "cli",
        "action_params_json": json.dumps({"source": "cli_test_send"}),
        "status": "succeeded",
    })
    # 2d. Sweeper orphan recovered task
    db.insert_task({
        "task_id": "tsk_synth_sweeper",
        "event_id": "evt_orphan_99999",
        "command": "echo 'sweeper_recovered_evt_orphan_99999'",
        "source": "default",
        "action_type": "cli",
        "action_params_json": "{}",
        "status": "succeeded",
    })

    async with httpx.AsyncClient() as client:
        # Default GET /tasks: returns all tasks with is_test tag
        resp_all = await client.get(f"{base_url}/tasks")
        assert resp_all.status_code == 200
        data_all = resp_all.json()
        all_tasks = data_all["tasks"]
        task_map = {t["task_id"]: t for t in all_tasks}

        assert task_map["tsk_real_prod_1"]["is_test"] is False
        assert task_map["tsk_synth_echo"]["is_test"] is True
        assert task_map["tsk_synth_nonce"]["is_test"] is True
        assert task_map["tsk_synth_cli_send"]["is_test"] is True
        assert task_map["tsk_synth_sweeper"]["is_test"] is True

        # Filter mode 'real': hide test tasks
        resp_real = await client.get(f"{base_url}/tasks?filter_test=real")
        assert resp_real.status_code == 200
        data_real = resp_real.json()
        real_ids = [t["task_id"] for t in data_real["tasks"]]
        assert "tsk_real_prod_1" in real_ids
        assert "tsk_synth_echo" not in real_ids
        assert "tsk_synth_nonce" not in real_ids
        assert "tsk_synth_cli_send" not in real_ids
        assert "tsk_synth_sweeper" not in real_ids
        assert all(t["is_test"] is False for t in data_real["tasks"])

        # Also support hide_test=true
        resp_hide = await client.get(f"{base_url}/tasks?hide_test=true")
        assert resp_hide.status_code == 200
        assert [t["task_id"] for t in resp_hide.json()["tasks"]] == real_ids

        # Filter mode 'test': only test tasks
        resp_test = await client.get(f"{base_url}/tasks?filter_test=test")
        assert resp_test.status_code == 200
        data_test = resp_test.json()
        test_ids = [t["task_id"] for t in data_test["tasks"]]
        assert "tsk_real_prod_1" not in test_ids
        assert "tsk_synth_echo" in test_ids
        assert "tsk_synth_nonce" in test_ids
        assert "tsk_synth_cli_send" in test_ids
        assert "tsk_synth_sweeper" in test_ids
        assert all(t["is_test"] is True for t in data_test["tasks"])

        # GET /tasks/summary includes real_tasks and test_tasks
        resp_summary = await client.get(f"{base_url}/tasks/summary")
        assert resp_summary.status_code == 200
        summary = resp_summary.json()
        assert "real_tasks" in summary
        assert "test_tasks" in summary
        assert summary["real_tasks"] >= 1
        assert summary["test_tasks"] >= 4

