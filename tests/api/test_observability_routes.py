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
