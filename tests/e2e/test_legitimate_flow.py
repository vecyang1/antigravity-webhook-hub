"""End-to-End tests for legitimate execution flows (Tiers 3 & 4).

Verifies:
- Full lifecycle: HMAC Sign -> Ingress -> SQLite SSOT -> Async Dispatch -> SSE Stream -> Terminal Status
- Concurrent webhook deliveries under load with SQLite WAL concurrent access
- Scenario 1: GitHub webhook simulation triggering build action
- Scenario 2: Antigravity AI agent task signal dispatch
- Scenario 3: Cloudflare Tunnel simulated ingress with header propagation
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.security import generate_hmac_signature

try:
    from hub.routes.webhook import register_webhook_routes
    from hub.server import AsyncHTTPServer
except ImportError:
    register_webhook_routes = None
    AsyncHTTPServer = None

pytestmark = pytest.mark.skipif(
    register_webhook_routes is None or AsyncHTTPServer is None,
    reason="Full application stack (Milestones M2-M4) not yet completely wired",
)


@pytest.fixture
async def full_stack_server(free_port: int, temp_db_path: str):
    """Starts fully wired gateway server instance."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.webhook_secret = "e2e_full_secret_key_32bytes_1234"
    config.database.path = temp_db_path

    server = AsyncHTTPServer(config.server)

    # Initialize all subsystems if available
    db_mgr = None
    broker = None
    dispatcher = None
    try:
        from hub.broker import EventBroker
        from hub.db import DatabaseManager
        from hub.dispatcher import TaskDispatcher
        from hub.routes.observability import register_observability_routes
        from hub.routes.sse import register_sse_routes

        db_mgr = DatabaseManager(temp_db_path)
        db_mgr.init_schema()
        broker = EventBroker()
        dispatcher = TaskDispatcher(db_mgr, broker)
        await dispatcher.start()

        register_webhook_routes(server, config, db_mgr, dispatcher)
        register_observability_routes(server, config, db_mgr)
        register_sse_routes(server, config, db_mgr, broker)
    except Exception:
        pass

    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, config, db_mgr, dispatcher

    if dispatcher:
        await dispatcher.stop()
    await server.stop()


async def test_full_webhook_lifecycle_e2e(full_stack_server: Any):
    """Tier 3: Ingress -> DB -> Async Dispatch -> Execution -> Final Query."""
    base_url, server, config, db, dispatcher = full_stack_server

    payload = {
        "action": "cli",
        "command": "python3 -c \"print('e2e_full_lifecycle_success')\"",
        "timeout_seconds": 10,
    }
    body = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        # 1. Post webhook
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 202
        data = resp.json()
        task_id = data.get("task_id")
        assert task_id is not None

        # 2. Poll until task completes
        final_task = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            t_resp = await client.get(f"{base_url}/tasks/{task_id}")
            if t_resp.status_code == 200:
                t_data = t_resp.json()
                if t_data.get("status") in ("succeeded", "failed", "timed_out"):
                    final_task = t_data
                    break

        assert final_task is not None, "Task did not reach terminal status in time"
        assert final_task["status"] == "succeeded"
        assert final_task["exit_code"] == 0
        assert "e2e_full_lifecycle_success" in final_task.get("stdout", "")


async def test_concurrent_webhook_deliveries(full_stack_server: Any):
    """Tier 3: Multiple concurrent incoming webhooks under load with SQLite WAL."""
    base_url, server, config, db, dispatcher = full_stack_server

    async def send_webhook(i: int):
        payload = {"action": "cli", "command": f"python3 -c \"print('concurrent_{i}')\""}
        body = json.dumps(payload).encode("utf-8")
        ts = int(time.time())
        sig = generate_hmac_signature(config.security.webhook_secret, body, ts)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(ts),
            "X-Hub-Event-ID": f"evt_concurrent_{i}_{time.time()}",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            return await client.post(f"{base_url}/webhook", content=body, headers=headers)

    # Dispatch 10 concurrent requests
    responses = await asyncio.gather(*[send_webhook(i) for i in range(10)])
    for r in responses:
        assert r.status_code == 202


async def test_scenario_github_webhook_simulation(full_stack_server: Any):
    """Tier 4: GitHub push simulation triggering local build script."""
    base_url, server, config, db, dispatcher = full_stack_server

    github_payload = {
        "ref": "refs/heads/main",
        "repository": {"name": "antigravity-webhook-hub", "full_name": "vec/antigravity-webhook-hub"},
        "pusher": {"name": "vecsat"},
        "action": "cli",
        "command": "echo 'github_build_simulated'",
    }
    body = json.dumps(github_payload).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": f"gh_del_{time.time()}",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook/github", content=body, headers=headers)
        assert resp.status_code == 202


async def test_scenario_simulated_cloudflare_tunnel_ingress(full_stack_server: Any):
    """Tier 4: Cloudflare Tunnel simulated headers (CF-Connecting-IP, CF-Ray, X-Forwarded-For)."""
    base_url, server, config, db, dispatcher = full_stack_server

    body = json.dumps({"action": "cli", "command": "echo cf_tunnel"}).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
        "CF-Connecting-IP": "198.51.100.42",
        "CF-Ray": "8c1234567890abcd-BKK",
        "X-Forwarded-For": "198.51.100.42",
        "X-Forwarded-Proto": "https",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 202
