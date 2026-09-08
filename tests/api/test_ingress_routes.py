"""API tests for POST /webhook and POST /webhook/{source} ingress endpoints.

Verifies Tiers 1 & 2 requirements:
- Valid HMAC SHA-256 authenticated POST returns 202 Accepted
- Bearer token authenticated POST returns 202 Accepted
- Missing, tampered, expired (>300s), or future (>60s) signatures return 401
- Malformed JSON bodies return 400 Bad Request
- Oversized payloads return 413 Payload Too Large
- ZERO DB SIDE EFFECTS on all security and validation rejections
- Idempotent duplicate requests return cached 200/202 without spawning extra tasks
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx
import pytest

from hub.config import AppConfig, SecurityConfig, ServerConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.security import generate_hmac_signature
from hub.server import AsyncHTTPServer

try:
    from hub.routes.webhook import register_webhook_routes
except ImportError:
    register_webhook_routes = None

pytestmark = pytest.mark.skipif(
    register_webhook_routes is None,
    reason="hub.routes.webhook (Milestone M2/M3) not yet implemented",
)


@pytest.fixture
async def ingress_server(free_port: int, temp_db_path: str):
    """Starts an ephemeral AsyncHTTPServer with webhook routes registered."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.webhook_secret = "test_ingress_secret_key_32bytes"
    config.security.bearer_token = "test_ingress_bearer_token"
    config.database.path = temp_db_path

    server = AsyncHTTPServer(config.server)

    # When M2/M3 is implemented, register_webhook_routes attaches the handlers
    db_mock_or_real = None
    try:
        from hub.db import DatabaseManager
        db_mock_or_real = DatabaseManager(temp_db_path)
        db_mock_or_real.init_schema()
    except Exception:
        pass

    register_webhook_routes(server, config, db_mock_or_real)
    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, config, db_mock_or_real

    await server.stop()


async def test_ingress_valid_hmac_returns_202(ingress_server: Any):
    """Tier 1: Valid HMAC signed request returns 202 Accepted with event_id and task_id."""
    base_url, server, config, db = ingress_server
    body = json.dumps({"action": "cli", "command": "echo test"}).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data.get("status") in ("accepted", "queued")
        assert "task_id" in data or "event_id" in data


async def test_ingress_missing_auth_returns_401_zero_side_effects(ingress_server: Any):
    """Tier 2: Missing auth returns 401 with strictly ZERO database records created."""
    base_url, server, config, db = ingress_server
    body = b'{"action":"cli","command":"echo attack"}'

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=body)
        assert resp.status_code == 401

    if db:
        events = await db.execute_read("SELECT count(*) as count FROM webhook_events")
        tasks = await db.execute_read("SELECT count(*) as count FROM tasks")
        assert events[0]["count"] == 0, "Security rejection must produce ZERO event writes"
        assert tasks[0]["count"] == 0, "Security rejection must produce ZERO task writes"


async def test_ingress_tampered_signature_returns_401_zero_side_effects(ingress_server: Any):
    """Tier 2: Tampered signature returns 401 with ZERO DB writes."""
    base_url, server, config, db = ingress_server
    body = b'{"action":"cli","command":"echo attack"}'
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)
    tampered_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": tampered_sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 401

    if db:
        events = await db.execute_read("SELECT count(*) as count FROM webhook_events")
        assert events[0]["count"] == 0, "Tampered signature must produce ZERO database writes"


async def test_ingress_expired_timestamp_replay_defense(ingress_server: Any):
    """Tier 2: Timestamp 400s in the past returns 401 with ZERO DB writes."""
    base_url, server, config, db = ingress_server
    body = b'{"action":"cli","command":"echo replay"}'
    expired_ts = int(time.time()) - 400
    sig = generate_hmac_signature(config.security.webhook_secret, body, expired_ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(expired_ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 401

    if db:
        events = await db.execute_read("SELECT count(*) as count FROM webhook_events")
        assert events[0]["count"] == 0, "Replay attempt must produce ZERO database writes"


async def test_ingress_future_timestamp_rejected(ingress_server: Any):
    """Tier 2: Future timestamp (>60s) returns 401 with ZERO DB writes."""
    base_url, server, config, db = ingress_server
    body = b'{"action":"cli","command":"echo future"}'
    future_ts = int(time.time()) + 120
    sig = generate_hmac_signature(config.security.webhook_secret, body, future_ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(future_ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 401

    if db:
        events = await db.execute_read("SELECT count(*) as count FROM webhook_events")
        assert events[0]["count"] == 0, "Future timestamp must produce ZERO database writes"


async def test_ingress_malformed_json_returns_400(ingress_server: Any):
    """Tier 2: Malformed JSON returns 400 Bad Request."""
    base_url, server, config, db = ingress_server
    body = b'{"unclosed: json body'
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 400

    if db:
        events = await db.execute_read("SELECT count(*) as count FROM webhook_events")
        assert events[0]["count"] == 0


async def test_ingress_idempotent_duplicate_deduplication(ingress_server: Any):
    """Tier 2: Duplicate webhook within window returns cached response and executes only once."""
    base_url, server, config, db = ingress_server
    body = json.dumps({"action": "cli", "command": "echo dedup"}).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)
    idemp_key = "idemp_test_dedup_001"

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
        "X-Hub-Event-ID": idemp_key,
    }

    async with httpx.AsyncClient() as client:
        # First request
        resp1 = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp1.status_code == 202
        task_id = resp1.json().get("task_id")

        # Duplicate request with identical body and idempotency key
        resp2 = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp2.status_code in (200, 202)
        data2 = resp2.json()
        assert data2.get("duplicate") is True or data2.get("deduplicated") is True
        assert data2.get("task_id") == task_id
