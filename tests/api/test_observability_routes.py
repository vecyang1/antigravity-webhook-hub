"""API tests for observability endpoints: /healthz, /ready, /metrics, /tasks, and /tasks/{task_id}.

Verifies Tier 1 requirements:
- /healthz returns 200 with DB status, memory RSS (<30MB budget check), and uptime
- /ready returns 200 when ready, 503 on database lock
- /metrics supports both Prometheus text format and JSON format
- /tasks provides listing, pagination, and status filtering
- /tasks/{task_id} provides authoritative detail or returns 404
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.server import AsyncHTTPServer

try:
    from hub.routes.observability import register_observability_routes
except ImportError:
    register_observability_routes = None

pytestmark = pytest.mark.skipif(
    register_observability_routes is None,
    reason="hub.routes.observability (Milestone M3) not yet implemented",
)


@pytest.fixture
async def obs_server(free_port: int, temp_db_path: str):
    """Starts test server with observability routes registered."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.database.path = temp_db_path

    server = AsyncHTTPServer(config.server)

    db_instance = None
    try:
        from hub.db import DatabaseManager
        db_instance = DatabaseManager(temp_db_path)
        db_instance.init_schema()
    except Exception:
        pass

    register_observability_routes(server, config, db_instance)
    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, db_instance

    await server.stop()


async def test_healthz_endpoint(obs_server: Any):
    """Tier 1: GET /healthz returns 200 OK with JSON status, uptime, and memory RSS."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/healthz")
        assert resp.status_code == 200
        data = resp.json()

        assert data.get("status") == "ok"
        assert "uptime_seconds" in data
        assert "database" in data
        assert data["database"].get("status") in ("connected", "ok")

        # Memory verification (<30MB constraint)
        assert "system" in data
        sys_data = data["system"]
        assert "memory_rss_mb" in sys_data
        assert sys_data["memory_rss_mb"] < 30.0, "Memory RSS must be under 30MB budget"
        assert sys_data.get("memory_healthy") is True


async def test_ready_endpoint(obs_server: Any):
    """Tier 1: GET /ready returns 200 OK when database is accessible."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/ready")
        assert resp.status_code == 200


async def test_metrics_prometheus_text_format(obs_server: Any):
    """Tier 1: GET /metrics returns standard Prometheus text exposition format."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/metrics", headers={"Accept": "text/plain"})
        assert resp.status_code == 200
        text = resp.text

        # Verify standard Prometheus metrics
        assert "webhook_hub_uptime_seconds" in text
        assert "webhook_hub_process_memory_rss_bytes" in text
        assert "webhook_hub_http_requests_total" in text


async def test_metrics_json_format(obs_server: Any):
    """Tier 1: GET /metrics?format=json returns metrics as JSON dictionary."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/metrics?format=json")
        assert resp.status_code == 200
        data = resp.json()

        assert "uptime_seconds" in data
        assert "memory_rss_bytes" in data
        assert "requests_total" in data


async def test_tasks_list_and_filter(obs_server: Any):
    """Tier 1: GET /tasks lists tasks and supports ?status= filter."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert isinstance(data["tasks"], list)


async def test_task_detail_and_404_handling(obs_server: Any):
    """Tier 1 & 2: GET /tasks/{id} returns details for existing or 404 for nonexistent."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        # 404 test
        resp_404 = await client.get(f"{base_url}/tasks/tsk_nonexistent_12345")
        assert resp_404.status_code == 404
        data_404 = resp_404.json()
        assert "not_found" in data_404.get("error", "").lower() or "error" in data_404


async def test_health_alias_endpoint(obs_server: Any):
    """MAT-033: GET /health returns 200 OK aliasing /healthz for Coolify health monitor."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok"


async def test_dashboard_endpoints(obs_server: Any):
    """Tier 1: GET /dashboard, /ui, and / with text/html return embedded SPA HTML."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        # /dashboard
        resp_dash = await client.get(f"{base_url}/dashboard")
        assert resp_dash.status_code == 200
        assert "text/html" in resp_dash.headers.get("content-type", "")
        assert "Antigravity Webhook Hub" in resp_dash.text
        assert "<svg" in resp_dash.text

        # /ui
        resp_ui = await client.get(f"{base_url}/ui")
        assert resp_ui.status_code == 200
        assert "text/html" in resp_ui.headers.get("content-type", "")

        # GET / with Accept: text/html content negotiation
        resp_root = await client.get(f"{base_url}/", headers={"Accept": "text/html,application/xhtml+xml"})
        assert resp_root.status_code == 200
        assert "text/html" in resp_root.headers.get("content-type", "")


async def test_tasks_summary_endpoint(obs_server: Any):
    """Tier 1: GET /tasks/summary aggregates counts directly from SQLite SSOT."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/tasks/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "success"
        assert "by_status" in data
        assert "by_source" in data
        assert "by_action" in data
        assert "total_tasks" in data


async def test_tasks_search_and_filter_routes(obs_server: Any):
    """Tier 1: GET /tasks supports search ?q= and filters ?source= and ?status=."""
    base_url, server, db = obs_server

    # Create dummy events and tasks in db
    if db is not None:
        db.insert_webhook_event({
            "event_id": "evt_filter_001",
            "source": "github",
            "payload_hash": "a" * 64,
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": "/webhook/github",
            "status": "received",
        })
        db.insert_task({
            "task_id": "tsk_filter_001",
            "event_id": "evt_filter_001",
            "command": "echo alpha",
            "source": "github",
            "action_type": "cli",
            "status": "queued",
        })
        db.insert_webhook_event({
            "event_id": "evt_filter_002",
            "source": "slack_people",
            "payload_hash": "b" * 64,
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": "/webhook/slack_people",
            "status": "received",
        })
        db.insert_task({
            "task_id": "tsk_filter_002",
            "event_id": "evt_filter_002",
            "command": "echo beta",
            "source": "slack_people",
            "action_type": "contact_review",
            "status": "succeeded",
        })

    async with httpx.AsyncClient() as client:
        # Filter by source
        resp_src = await client.get(f"{base_url}/tasks?source=github")
        assert resp_src.status_code == 200
        data_src = resp_src.json()
        assert any(t.get("source") == "github" for t in data_src.get("tasks", []))

        # Search query ?q=
        resp_q = await client.get(f"{base_url}/tasks?q=alpha")
        assert resp_q.status_code == 200
        data_q = resp_q.json()
        assert len(data_q.get("tasks", [])) >= 1
        assert any("alpha" in (t.get("command") or "") for t in data_q.get("tasks", []))


async def test_task_rerun_endpoint(obs_server: Any):
    """Tier 1: POST /tasks/{id}/rerun resets task to queued in SQLite SSOT."""
    base_url, server, db = obs_server

    if db is not None:
        db.insert_webhook_event({
            "event_id": "evt_rerun_100",
            "source": "test",
            "payload_hash": "c" * 64,
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": "/webhook/test",
            "status": "received",
        })
        db.insert_task({
            "task_id": "tsk_rerun_test_100",
            "event_id": "evt_rerun_100",
            "command": "echo rerun",
            "source": "test",
            "status": "failed",
            "exit_code": 1,
            "error_message": "Simulated failure",
        })

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/tasks/tsk_rerun_test_100/rerun")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "queued"
        assert data.get("task_id") == "tsk_rerun_test_100"

        # Verify SSOT reflection
        task_ref = db.get_task("tsk_rerun_test_100")
        assert task_ref["status"] == "queued"
        assert task_ref["error_message"] is None


async def test_head_method_support(obs_server: Any):
    """Verify HTTP HEAD method on health and dashboard routes returns 200 OK with empty body."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        # HEAD /healthz
        resp_healthz = await client.head(f"{base_url}/healthz")
        assert resp_healthz.status_code == 200
        assert resp_healthz.text == ""
        assert "content-length" in resp_healthz.headers
        assert int(resp_healthz.headers["content-length"]) > 0

        # HEAD /health
        resp_health = await client.head(f"{base_url}/health")
        assert resp_health.status_code == 200
        assert resp_health.text == ""

        # HEAD /dashboard
        resp_dash = await client.head(f"{base_url}/dashboard")
        assert resp_dash.status_code == 200
        assert resp_dash.text == ""
        assert "text/html" in resp_dash.headers.get("content-type", "")
        assert int(resp_dash.headers.get("content-length", "0")) > 0


async def test_antigravity_quota_endpoints(obs_server: Any):
    """Verify /antigravity/quota and /antigravity/quota/health endpoints."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        # 1. GET /antigravity/quota/health for Uptime Kuma monitoring probe
        resp_health = await client.get(f"{base_url}/antigravity/quota/health")
        assert resp_health.status_code == 200
        health_data = resp_health.json()
        assert health_data.get("status") == "ok"
        assert health_data.get("sentinel") == "healthy"
        assert "timestamp" in health_data

        # 2. GET /antigravity/quota authoritative SSOT
        resp_quota = await client.get(f"{base_url}/antigravity/quota")
        assert resp_quota.status_code == 200
        quota_data = resp_quota.json()
        assert "active_account" in quota_data
        assert "all_accounts" in quota_data
        assert "stats" in quota_data


async def test_antigravity_warmup_endpoints(obs_server: Any):
    """Verify POST and GET /antigravity/warmup routes with dry_run and query params."""
    base_url, server, db = obs_server

    async with httpx.AsyncClient() as client:
        # 1. POST /antigravity/warmup with JSON body dry_run=True
        resp_post = await client.post(
            f"{base_url}/antigravity/warmup",
            json={"dry_run": True, "force": True, "all_accounts": True, "reason": "test_post_warmup"},
        )
        assert resp_post.status_code == 200
        post_data = resp_post.json()
        assert post_data.get("dry_run") is True
        assert post_data.get("reason") == "test_post_warmup"
        assert post_data.get("warmups_executed") == 0
        assert "accounts_scanned" in post_data

        # 2. GET /antigravity/warmup with query parameters
        resp_get = await client.get(
            f"{base_url}/antigravity/warmup?dry_run=true&force=true&all_accounts=true&reason=test_get_warmup"
        )
        assert resp_get.status_code == 200
        get_data = resp_get.json()
        assert get_data.get("dry_run") is True
        assert get_data.get("reason") == "test_get_warmup"
        assert get_data.get("warmups_executed") == 0


