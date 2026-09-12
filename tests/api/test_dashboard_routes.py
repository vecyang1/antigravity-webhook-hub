"""
Tests for Antigravity Webhook Hub Observable Activity Dashboard & Sidebar Integration.
Verifies:
- GET /dashboard and GET /ui deliver the zero-dependency embedded SPA HTML.
- GET / content negotiation (HTML vs JSON).
- GET /tasks/summary & GET /activities/summary direct SQLite SSOT aggregations.
- GET /tasks advanced filtering (source, status, action_type, search query q).
- POST /tasks/{id}/rerun lifecycle & state transition.
- Antigravity Sidecar Sentinel event logging in sidecar_data/events/.
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
            assert "real_by_status" in data
            assert "test_by_status" in data
            assert isinstance(data["real_by_status"], dict)
            assert isinstance(data["test_by_status"], dict)
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

        # Verify total and count alignment
        assert data_real["total"] == len(data_real["tasks"])
        resp_summary = await client.get(f"{base_url}/tasks/summary")
        assert resp_summary.status_code == 200
        summary = resp_summary.json()
        assert summary["real_tasks"] == data_real["total"]
        assert summary["test_tasks"] >= 4


async def test_edge_case_classification_no_false_positives(dashboard_test_app: dict[str, Any]):
    """Ensure natural language keywords (e.g. announcement) and valid sources (e.g. latest) are not false-positive test events."""
    base_url = dashboard_test_app["base_url"]
    db: DatabaseManager = dashboard_test_app["db"]

    # 1. Payload with 'announcement' (contains substring 'nonce') should be REAL
    db.insert_task({
        "task_id": "tsk_real_announcement",
        "event_id": "evt_real_announcement",
        "command": "bin/webhook-hub notify",
        "source": "latest_updates",
        "action_type": "cli",
        "action_params_json": json.dumps({"topic": "Important company announcement", "user": "alice"}),
        "status": "succeeded",
    })

    # 2. Synthetic verification tasks from test suites
    db.insert_task({
        "task_id": "tsk_synth_sync_test",
        "event_id": "evt_synth_sync_test",
        "command": "bin/webhook-hub review-contact",
        "source": "contact-review",
        "action_type": "contact_review",
        "action_params_json": json.dumps({"source": "sync-test", "notes": "Testing sync parameter return value"}),
        "status": "succeeded",
    })
    db.insert_task({
        "task_id": "tsk_synth_slack_suppl",
        "event_id": "evt_synth_slack_suppl",
        "command": "bin/webhook-hub review-contact",
        "source": "contact-review",
        "action_type": "contact_review",
        "action_params_json": json.dumps({"source": "slack_supplementary_test", "channel_id": "C096KR96AF7"}),
        "status": "succeeded",
    })

    # 3. Explicit override: is_test: false
    db.insert_task({
        "task_id": "tsk_override_real",
        "event_id": "evt_override_real",
        "command": "echo 'custom task'",
        "source": "api",
        "action_type": "cli",
        "action_params_json": json.dumps({"command": "echo 'custom task'", "is_test": False}),
        "status": "succeeded",
    })

    # 4. Uptime Kuma tasks (both uptime_kuma and uptime-kuma source filters)
    db.insert_task({
        "task_id": "tsk_uptime_alert_1",
        "event_id": "evt_uptime_alert_1",
        "command": 'python3 "audit_maintenance_alert_triage.py"',
        "source": "uptime_kuma",
        "action_type": "cli",
        "action_params_json": json.dumps({"msg": "HTTP 500", "monitor": {"name": "Webhook"}}),
        "status": "succeeded",
    })

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/tasks?filter_test=real")
        assert resp.status_code == 200
        real_tasks = resp.json()["tasks"]
        real_ids = {t["task_id"] for t in real_tasks}

        # Announcement task is NOT a test event
        assert "tsk_real_announcement" in real_ids
        # Synthetic tasks are correctly filtered
        assert "tsk_synth_sync_test" not in real_ids
        assert "tsk_synth_slack_suppl" not in real_ids
        # Explicit override is respected
        assert "tsk_override_real" in real_ids

        # Test source_filter normalization for uptime-kuma vs uptime_kuma
        resp_kuma_dash = await client.get(f"{base_url}/tasks?source=uptime-kuma")
        assert resp_kuma_dash.status_code == 200
        kuma_dash_ids = {t["task_id"] for t in resp_kuma_dash.json()["tasks"]}
        assert "tsk_uptime_alert_1" in kuma_dash_ids

        resp_kuma_under = await client.get(f"{base_url}/tasks?source=uptime_kuma")
        assert resp_kuma_under.status_code == 200
        kuma_under_ids = {t["task_id"] for t in resp_kuma_under.json()["tasks"]}
        assert "tsk_uptime_alert_1" in kuma_under_ids


def _make_mock_jwt(payload: dict[str, Any]) -> str:
    import base64
    def b64url(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).decode("utf-8").rstrip("=")
    h = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    p = b64url(json.dumps(payload).encode("utf-8"))
    return f"{h}.{p}.mock_signature"


async def test_dashboard_log_drawer_ui_ux_rendering(dashboard_test_app: dict[str, Any]):
    """
    Verify /dashboard HTML contains all /ui-ux-pro-max log drawer features:
    - Line number gutter with .log-gutter
    - Search input, match counter, and Cmd/Ctrl+F shortcut
    - Log level filter dropdown
    - Wrap lines toggle
    - Auto-scroll toggle
    - Copy logs button
    - Execution duration header and exit code badge
    - Zero emoji UI icons
    """
    base_url = dashboard_test_app["base_url"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/dashboard")
        assert resp.status_code == 200
        html = resp.text

        # 1. Gutter & terminal structure
        assert "drawerTerminalInner" in html
        assert "log-gutter" in html
        assert "log-row" in html
        assert "log-text" in html

        # 2. Controls & Toolbar
        assert "logSearchInput" in html
        assert "logLevelFilter" in html
        assert "btnToggleWrap" in html
        assert "btnToggleAutoScroll" in html
        assert "btnTogglePrettyJson" in html
        assert "btnCopyLogs" in html
        assert "logMatchesCount" in html

        # 3. Badges & Syntax Highlighting
        assert "drawerDurationBadge" in html
        assert "drawerMetaDuration" in html
        assert "badge-duration" in html
        assert "renderExitBadge" in html
        assert "log-lvl-info" in html
        assert "log-lvl-error" in html
        assert "log-json-key" in html
        assert "is-pretty-json" in html
        assert "log-traceback-title" in html
        assert "log-traceback-line" in html
        assert "log-traceback-func" in html
        assert "log-traceback-exc" in html
        assert "parseAnsi" in html
        assert "is-traceback" in html

        # 4. Zero emoji check
        assert "🚀" not in html
        assert "🎨" not in html
        assert "📋" not in html


async def test_dashboard_authentication_cloudflare_access(free_port: int, temp_db_path: str):
    """
    Verify dashboard route authentication via Cloudflare Access JWT assertion headers:
    - Valid assertion with correct AUD and allowed email -> 200 OK
    - Expired JWT -> 401 Unauthorized
    - Mismatched AUD -> 401 Unauthorized
    - Unauthorized email -> 403 Forbidden
    """
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.database.path = temp_db_path
    config.dashboard.auth_enabled = True
    config.dashboard.cloudflare_access_aud = "test_aud_hash_12345"
    config.dashboard.allowed_emails = ["vec@example.com", "admin@example.org"]

    server = AsyncHTTPServer(config.server)
    register_dashboard_routes(server, config)
    await server.start()

    base_url = f"http://127.0.0.1:{free_port}"
    try:
        async with httpx.AsyncClient() as client:
            # 1. Unauthenticated request -> 401
            r_unauth = await client.get(f"{base_url}/dashboard")
            assert r_unauth.status_code == 401
            assert "WWW-Authenticate" in r_unauth.headers

            # 2. Valid Cloudflare Access JWT
            valid_jwt = _make_mock_jwt({
                "aud": "test_aud_hash_12345",
                "email": "vec@example.com",
                "exp": time.time() + 3600,
            })
            r_valid = await client.get(
                f"{base_url}/dashboard",
                headers={"cf-access-jwt-assertion": valid_jwt},
            )
            assert r_valid.status_code == 200
            assert "Antigravity Webhook Hub" in r_valid.text

            # 3. Expired JWT -> 401
            expired_jwt = _make_mock_jwt({
                "aud": "test_aud_hash_12345",
                "email": "vec@example.com",
                "exp": time.time() - 3600,
            })
            r_exp = await client.get(
                f"{base_url}/dashboard",
                headers={"cf-access-jwt-assertion": expired_jwt},
            )
            assert r_exp.status_code == 401
            assert "expired" in r_exp.text.lower()

            # 4. AUD mismatch -> 401
            bad_aud_jwt = _make_mock_jwt({
                "aud": "wrong_aud_hash",
                "email": "vec@example.com",
                "exp": time.time() + 3600,
            })
            r_aud = await client.get(
                f"{base_url}/dashboard",
                headers={"cf-access-jwt-assertion": bad_aud_jwt},
            )
            assert r_aud.status_code == 401

            # 5. Email not allowed -> 403
            disallowed_jwt = _make_mock_jwt({
                "aud": "test_aud_hash_12345",
                "email": "intruder@example.com",
                "exp": time.time() + 3600,
            })
            r_forbidden = await client.get(
                f"{base_url}/dashboard",
                headers={"cf-access-jwt-assertion": disallowed_jwt},
            )
            assert r_forbidden.status_code == 403
    finally:
        await server.stop()


async def test_dashboard_authentication_basic_and_token(free_port: int, temp_db_path: str):
    """
    Verify dashboard route authentication via HTTP Basic Auth and Token fallback:
    - HTTP Basic Auth with correct credentials -> 200 OK
    - HTTP Basic Auth with wrong credentials -> 401
    - Token via Authorization: Bearer <token> -> 200 OK
    - Token via query parameter ?token=<token> -> 200 OK
    - Token via X-Dashboard-Token header -> 200 OK
    """
    import base64
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.database.path = temp_db_path
    config.dashboard.auth_enabled = True
    config.dashboard.basic_auth_user = "admin"
    config.dashboard.basic_auth_pass = "supersecret123"
    config.dashboard.auth_token = "test-auth-token-xyz-12345"

    server = AsyncHTTPServer(config.server)
    register_dashboard_routes(server, config)
    await server.start()

    base_url = f"http://127.0.0.1:{free_port}"
    try:
        async with httpx.AsyncClient() as client:
            # 1. Unauthenticated -> 401
            r = await client.get(f"{base_url}/dashboard")
            assert r.status_code == 401

            # 2. Basic Auth correct
            b64_creds = base64.b64encode(b"admin:supersecret123").decode()
            r_basic_ok = await client.get(
                f"{base_url}/dashboard",
                headers={"Authorization": f"Basic {b64_creds}"},
            )
            assert r_basic_ok.status_code == 200

            # 3. Basic Auth wrong password
            b64_bad = base64.b64encode(b"admin:wrongpass").decode()
            r_basic_bad = await client.get(
                f"{base_url}/dashboard",
                headers={"Authorization": f"Basic {b64_bad}"},
            )
            assert r_basic_bad.status_code == 401

            # 4. Bearer token
            r_bearer = await client.get(
                f"{base_url}/dashboard",
                headers={"Authorization": "Bearer test-auth-token-xyz-12345"},
            )
            assert r_bearer.status_code == 200

            # 5. Query parameter ?token=
            r_query = await client.get(f"{base_url}/dashboard?token=test-auth-token-xyz-12345")
            assert r_query.status_code == 200

            # 6. X-Dashboard-Token header
            r_hdr = await client.get(
                f"{base_url}/dashboard",
                headers={"X-Dashboard-Token": "test-auth-token-xyz-12345"},
            )
            assert r_hdr.status_code == 200
    finally:
        await server.stop()


async def test_dashboard_log_formatter_unit_and_safety():
    """
    Directly execute the dashboard's JavaScript log formatter in Node.js to verify:
    1. Zero tag collision when JSON stdout contains log levels (e.g. {"status": "SUCCESS"}).
    2. Search query highlighting never corrupts HTML tag names or attributes (e.g. searching "span" or "class").
    3. ANSI escape sequences correctly close all open spans and strip non-SGR sequences.
    4. Python and JS stack traces syntax-highlight headers, file frames, lines, functions, and exceptions.
    5. Embedded and full JSON parsing correctly detects JSON payloads.
    """
    from hub.routes.dashboard_template import render_dashboard_html
    import subprocess
    import tempfile
    import os
    import re

    html = render_dashboard_html()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert len(scripts) > 0, "No <script> tags found in dashboard HTML"

    runner_script = f"""
global.window = {{
    addEventListener: () => {{}},
    removeEventListener: () => {{}},
    location: {{ href: 'http://localhost/' }}
}};
global.document = {{
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => ({{
        style: {{}},
        appendChild: () => {{}},
        classList: {{ add: () => {{}}, remove: () => {{}} }}
    }}),
    addEventListener: () => {{}}
}};
global.localStorage = {{
    getItem: () => null,
    setItem: () => {{}},
    removeItem: () => {{}}
}};
global.navigator = {{
    clipboard: {{ writeText: async () => {{}} }}
}};

{scripts[0]}

const assert = require('assert');

// 1. JSON with embedded log levels: NO collision with HTML class attributes
const jsonLine = '{{"status": "SUCCESS", "code": 200, "path": "/api/v1/health"}}';
const formattedJson = formatLogLineHtml(jsonLine, 'stdout');
assert(!formattedJson.includes('<span class="log-json-str">"<span class="'), 'Collision detected in JSON formatting');
assert(formattedJson.includes('log-json-key'), 'Missing log-json-key');
assert(formattedJson.includes('log-json-str'), 'Missing log-json-str');
assert(formattedJson.includes('log-lvl-success'), 'Missing log-lvl-success');

// 2. Safe search highlighting: NEVER corrupts tags
const sampleRow = formatLogLineHtml('[INFO] Process completed with span tag in class', 'stdout');
const searchSpan = highlightSearchQuery(sampleRow, 'span');
assert(!searchSpan.includes('<<mark'), 'Corrupted tag name with search query');
assert(!searchSpan.includes('class="<mark'), 'Corrupted class attribute with search query');
assert(searchSpan.includes('<mark class="log-search-match">span</mark>'), 'Failed to match text content');

const searchClass = highlightSearchQuery(sampleRow, 'class');
assert(!searchClass.includes('<span <mark'), 'Corrupted span tag with class query');
assert(searchClass.includes('<mark class="log-search-match">class</mark>'), 'Failed to match class text');

// 3. ANSI escapes: all spans closed
const ansiText = '\\u001b[1m\\u001b[32m[PASS]\\u001b[0m All tests passed';
const formattedAnsi = formatLogLineHtml(ansiText, 'stdout');
assert(formattedAnsi.includes('ansi-bold'), 'Missing ansi-bold');
assert(formattedAnsi.includes('ansi-32'), 'Missing ansi-32');
const openCount = (formattedAnsi.match(/<span/g) || []).length;
const closeCount = (formattedAnsi.match(/<\\/span>/g) || []).length;
assert.strictEqual(openCount, closeCount, 'Imbalanced HTML spans in ANSI rendering');

// 4. Traceback highlighting
const tbHeader = formatLogLineHtml('Traceback (most recent call last):', 'stderr');
assert(tbHeader.includes('log-traceback-title'), 'Missing traceback title class');

const tbFrame = formatLogLineHtml('  File "/opt/app/main.py", line 42, in handle_task', 'stderr');
assert(tbFrame.includes('log-path'), 'Missing file path class in frame');
assert(tbFrame.includes('log-traceback-line'), 'Missing line number class in frame');
assert(tbFrame.includes('log-traceback-func'), 'Missing function name class in frame');

const tbExc = formatLogLineHtml('ValueError: Invalid configuration option provided', 'stderr');
assert(tbExc.includes('log-lvl-error'), 'Missing exception level error class');
assert(tbExc.includes('log-traceback-msg'), 'Missing exception message class');

// 5. JSON detection
const parsedFull = tryParseJson('{{"test": 123}}');
assert(parsedFull && parsedFull.type === 'full', 'Failed to detect full JSON');

const parsedEmbedded = tryParseJson('[INFO] Result: {{"status": "ok"}}');
assert(parsedEmbedded && parsedEmbedded.type === 'embedded', 'Failed to detect embedded JSON');

console.log("NODE_EVAL_ALL_PASSED");
"""

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(runner_script)
        temp_path = f.name

    try:
        proc = subprocess.run(["node", temp_path], capture_output=True, text=True, check=True)
        assert "NODE_EVAL_ALL_PASSED" in proc.stdout
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


async def test_observability_unauthorized_rendering(free_port: int, temp_db_path: str):
    """Verify handle_root and handle_dashboard in observability.py render the styled 401 card."""
    from hub.routes.observability import register_observability_routes
    from hub.routes.dashboard import render_unauthorized_html
    from hub.security import ValidationResult

    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.database.path = temp_db_path
    config.dashboard.auth_enabled = True
    config.dashboard.basic_auth_user = "secadmin"
    config.dashboard.basic_auth_pass = "pass12345"

    server = AsyncHTTPServer(config.server)
    register_observability_routes(server, config)
    await server.start()

    base_url = f"http://127.0.0.1:{free_port}"
    try:
        async with httpx.AsyncClient() as client:
            # GET / with text/html -> 401 styled card
            r_root = await client.get(f"{base_url}/", headers={"Accept": "text/html"})
            assert r_root.status_code == 401
            assert "Authentication Required" in r_root.text
            assert "Access Denied" in r_root.text
            assert "WWW-Authenticate" in r_root.headers

            # GET /dashboard without auth -> 401 styled card
            r_dash = await client.get(f"{base_url}/dashboard", headers={"Accept": "text/html"})
            assert r_dash.status_code == 401
            assert "Authentication Required" in r_dash.text
            assert "Access Denied" in r_dash.text
    finally:
        await server.stop()



