"""
API & E2E Tests for Uptime Kuma Ingress Endpoints:
- POST /api/webhook/uptime-kuma
- POST /webhook/uptime-kuma

Tests both sides of the contract:
- Legitimate paths:
  * Valid UP heartbeat -> HTTP 200 with status="up"
  * Valid DOWN heartbeat with successful double-check probe -> HTTP 200 debounced flap (suppressed)
  * Valid DOWN heartbeat with failing double-check probe -> HTTP 202 Accepted, triage queued
  * Successive DOWN within cooldown window -> HTTP 200 coalesced
- Adversarial paths:
  * Missing Bearer token -> HTTP 401, ZERO DB writes
  * Invalid Bearer token -> HTTP 401, ZERO DB writes
  * Empty body -> HTTP 400, ZERO DB writes
  * Malformed JSON -> HTTP 400, ZERO DB writes
  * Missing heartbeat or monitor -> HTTP 400, ZERO DB writes
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.uptime_kuma import _recent_down_events, register_uptime_kuma_routes
from hub.server import AsyncHTTPServer


@pytest.fixture(autouse=True)
def reset_cooldown():
    """Clear in-memory cooldown state between tests."""
    _recent_down_events.clear()
    yield
    _recent_down_events.clear()


@pytest.fixture
async def kuma_server_harness(free_port: int, temp_db_path: str):
    """Starts an ephemeral AsyncHTTPServer with Uptime Kuma routes registered."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.bearer_token = "kuma_test_bearer_token_secret_12345"
    config.database.path = temp_db_path

    db = DatabaseManager(temp_db_path)
    db.init_schema()

    dispatcher = TaskDispatcher(db=db, config=config)
    await dispatcher.start()

    server = AsyncHTTPServer(config.server)
    server._db = db

    # Configurable probe mock state
    probe_state = {"alive": False}

    async def mock_probe_fn(url: str, timeout: float = 3.0) -> bool:
        return probe_state["alive"]

    register_uptime_kuma_routes(server, config, db, dispatcher, probe_fn=mock_probe_fn)
    await server.start()

    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, config, db, dispatcher, probe_state

    await dispatcher.stop()
    await server.stop()


# ---------------------------------------------------------------------------
# Adversarial Security & Schema Tests (ZERO DB WRITES)
# ---------------------------------------------------------------------------


async def test_kuma_unauthorized_missing_auth(kuma_server_harness: Any):
    """Adversarial: Request missing Authorization header must be rejected with 401."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness

    payload = {
        "heartbeat": {"status": 0, "time": "2026-09-11 10:00:00", "msg": "Down"},
        "monitor": {"id": 41, "name": "CRM Hub", "url": "https://example.com"},
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/api/webhook/uptime-kuma", json=payload)

    assert resp.status_code == 401
    assert "unauthorized" in resp.text.lower() or "authorization" in resp.text.lower()

    # Zero DB side effects
    assert db._conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0] == 0
    assert db._conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


async def test_kuma_unauthorized_invalid_bearer(kuma_server_harness: Any):
    """Adversarial: Request with tampered Bearer token must be rejected with 401."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness

    payload = {
        "heartbeat": {"status": 0, "time": "2026-09-11 10:00:00", "msg": "Down"},
        "monitor": {"id": 41, "name": "CRM Hub", "url": "https://example.com"},
    }
    headers = {"Authorization": "Bearer wrong_invalid_token"}

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/uptime-kuma",
            json=payload,
            headers=headers,
        )

    assert resp.status_code == 401
    assert db._conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0] == 0


async def test_kuma_empty_body(kuma_server_harness: Any):
    """Adversarial: Empty body must return 400 and write zero records."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/uptime-kuma",
            content=b"",
            headers=headers,
        )

    assert resp.status_code == 400
    assert "empty" in resp.text.lower()
    assert db._conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0] == 0


async def test_kuma_malformed_json(kuma_server_harness: Any):
    """Adversarial: Broken JSON syntax must return 400 and write zero records."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    headers = {
        "Authorization": f"Bearer {config.security.bearer_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/uptime-kuma",
            content=b"{not_json: true,",
            headers=headers,
        )

    assert resp.status_code == 400
    assert "malformed" in resp.text.lower()
    assert db._conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0] == 0


async def test_kuma_invalid_schema_missing_fields(kuma_server_harness: Any):
    """Adversarial: Payload missing 'heartbeat' or 'monitor' must return 400."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/uptime-kuma",
            json={"random_field": "test"},
            headers=headers,
        )

    assert resp.status_code == 400
    assert "schema" in resp.text.lower()
    assert db._conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Legitimate Ingress & Anti-Flap Tests
# ---------------------------------------------------------------------------


async def test_kuma_valid_up_returns_200(kuma_server_harness: Any):
    """Legitimate: UP heartbeat returns 200 and records recovery event."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    payload = {
        "heartbeat": {"status": 1, "time": "2026-09-11 10:00:00", "msg": "200 - OK"},
        "monitor": {"id": 41, "name": "CRM Hub", "url": "https://hi.carradiocodes.co.uk/"},
        "msg": "🟢 [CRM Hub] [Up] 200 - OK",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/uptime-kuma",
            json=payload,
            headers=headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "up"
    assert data["monitor"] == "CRM Hub"

    # Event persisted to DB as processed, no heavy triage task queued, and sweeper finds 0 orphans
    row = db._conn.execute("SELECT status FROM webhook_events").fetchone()
    assert row is not None and row[0] == "processed"
    assert db._conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert len(db.get_orphaned_webhook_events()) == 0


async def test_kuma_down_with_flap_suppressed(kuma_server_harness: Any):
    """Legitimate Anti-Flap: When double-check probe succeeds, DOWN alert is suppressed as momentary jitter."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    probe_state["alive"] = True  # Double-check probe succeeds!

    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}
    payload = {
        "heartbeat": {"status": 0, "time": "2026-09-11 10:01:00", "msg": "Connection reset"},
        "monitor": {"id": 41, "name": "CRM Hub", "url": "https://hi.carradiocodes.co.uk/"},
        "msg": "🔴 [CRM Hub] [Down] Connection reset",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/uptime-kuma",
            json=payload,
            headers=headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "debounced_flap"
    assert data["flap_suppressed"] is True

    # Event logged as processed flap, but NO heavy triage task created
    row = db._conn.execute("SELECT status FROM webhook_events").fetchone()
    assert row is not None and row[0] == "processed"
    assert db._conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


async def test_kuma_down_confirmed_outage_queues_triage(kuma_server_harness: Any):
    """Legitimate: When double-check probe fails, outage is confirmed and triage task is queued."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    probe_state["alive"] = False  # Double-check probe fails (confirmed DOWN)

    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}
    payload = {
        "heartbeat": {"status": 0, "time": "2026-09-11 10:02:00", "msg": "HTTP 502 Bad Gateway"},
        "monitor": {"id": 41, "name": "CRM Hub", "url": "https://hi.carradiocodes.co.uk/"},
        "msg": "🔴 [CRM Hub] [Down] HTTP 502 Bad Gateway",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/uptime-kuma",
            json=payload,
            headers=headers,
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["outage_confirmed"] is True
    assert "task_id" in data

    # Task was created and queued in DB
    task_row = db._conn.execute("SELECT source, action_type, command FROM tasks").fetchone()
    assert task_row is not None
    assert task_row[0] == "uptime_kuma"
    assert task_row[1] == "cli"
    assert "audit_maintenance_alert_triage.py" in task_row[2]


async def test_kuma_down_cooldown_rate_limiting(kuma_server_harness: Any):
    """Legitimate Anti-Flap: Successive DOWN payloads within 60s cooldown coalesce into single triage."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    probe_state["alive"] = False  # Outage confirmed

    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}
    payload = {
        "heartbeat": {"status": 0, "time": "2026-09-11 10:03:00", "msg": "Timeout"},
        "monitor": {"id": 41, "name": "CRM Hub", "url": "https://hi.carradiocodes.co.uk/"},
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        # First alert -> queues task
        resp1 = await client.post(f"{base_url}/api/webhook/uptime-kuma", json=payload, headers=headers)
        assert resp1.status_code == 202

        # Second alert 1s later -> coalesced by anti-flap cooldown
        resp2 = await client.post(f"{base_url}/api/webhook/uptime-kuma", json=payload, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "cooldown_coalesced"
        assert resp2.json()["coalesced"] is True

    # Exactly 1 task was queued, preventing triage cascade
    assert db._conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


async def test_kuma_legacy_path_compatibility(kuma_server_harness: Any):
    """Compatibility: POST /webhook/uptime-kuma works identically to /api/webhook/uptime-kuma."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    payload = {
        "heartbeat": {"status": 1, "time": "2026-09-11 10:04:00", "msg": "OK"},
        "monitor": {"id": 41, "name": "CRM Hub", "url": "https://hi.carradiocodes.co.uk/"},
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook/uptime-kuma", json=payload, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "up"


async def test_kuma_up_silenced_by_default_and_recovery_notified(
    kuma_server_harness: Any, monkeypatch: pytest.MonkeyPatch
):
    """Health probe alerting discipline: UP heartbeats stay quiet by default; recovery after confirmed DOWN alerts."""
    base_url, server, config, db, dispatcher, probe_state = kuma_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    notifications = []
    monkeypatch.setattr(
        "hub.routes.uptime_kuma.send_desktop_notification",
        lambda title, msg, sound="Basso": notifications.append((title, msg, sound)),
    )

    # 1. Routine UP heartbeat (no prior outage) -> must produce ZERO desktop notifications
    payload_up = {
        "heartbeat": {"status": 1, "time": "2026-09-15 10:00:00", "msg": "200 OK"},
        "monitor": {"id": "mon_probe_99", "name": "Health Probe", "url": "https://example.com/healthz"},
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/api/webhook/uptime-kuma", json=payload_up, headers=headers)
        assert resp.status_code == 200
    assert len(notifications) == 0, "Routine UP heartbeat must stay quiet by default"

    # 2. DOWN outage confirmed -> triggers DOWN alert notification
    probe_state["alive"] = False
    payload_down = {
        "heartbeat": {"status": 0, "time": "2026-09-15 10:01:00", "msg": "503 Service Unavailable"},
        "monitor": {"id": "mon_probe_99", "name": "Health Probe", "url": "https://example.com/healthz"},
    }
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/api/webhook/uptime-kuma", json=payload_down, headers=headers)
        assert resp.status_code == 202
    assert len(notifications) == 1
    assert "DOWN" in notifications[0][1]

    # 3. Recovery UP heartbeat after prior DOWN outage -> sends RECOVERED notification
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/api/webhook/uptime-kuma", json=payload_up, headers=headers)
        assert resp.status_code == 200
    assert len(notifications) == 2
    assert "RECOVERED" in notifications[1][1]

    # 4. Successive routine UP heartbeat -> quiet again
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/api/webhook/uptime-kuma", json=payload_up, headers=headers)
        assert resp.status_code == 200
    assert len(notifications) == 2, "Subsequent UP heartbeats must remain quiet"

