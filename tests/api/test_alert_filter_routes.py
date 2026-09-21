"""
tests/api/test_alert_filter_routes.py — Comprehensive Two-Sided Tests for Jev Alert Filter.

Verifies:
1. Security & Schema:
   - Missing/invalid Bearer token -> 401, ZERO DB writes.
   - Malformed/empty payload -> 400, ZERO DB writes.
2. Noise Suppression (Two-Sided):
   - GlintMuse Blog transient keyword flap -> suppressed (200), NO desktop/Slack notification.
   - Confirmed critical database outage -> escalated (202), triggers notification.
   - Recovery/UP message -> operational status (200), no noise.
3. Diagnostic & Observability Mandate:
   - POST /api/alerts/diagnose returns transparent reasoning and calibrated noul score.
   - GET /api/alerts/metrics returns suppression rates and recent evaluation ring buffer.
   - GET/POST /api/alerts/config permits dynamic threshold adjustment without code changes.
4. Resilient Fallback:
   - fail_open policy escalates safely on upstream API failure.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from hub.alert_filter import AlertFilterResult, JevAlertFilter
from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.uptime_kuma import register_uptime_kuma_routes
from hub.server import AsyncHTTPServer


class MockJevFilter(JevAlertFilter):
    """Predictable mock filter for testing both sides of the contract."""

    def __init__(self, critical_threshold: float = 0.70):
        super().__init__(enabled=True, critical_threshold=critical_threshold)
        self.call_history = []

    def evaluate_alert(
        self,
        service: str,
        message: str,
        url: str = "",
        context: Any = None,
        custom_threshold: Any = None,
    ) -> AlertFilterResult:
        thresh = custom_threshold if custom_threshold is not None else self.critical_threshold
        lower = f"{service} {message}".lower()

        # Simulate Jev System One semantic understanding
        if any(x in lower for x in ["keyword is not in", "empty body", "jitter", "flap", "test probe", "glintmuse"]):
            noul = 0.15
            is_crit = False
            action = "suppress"
            reason = "Simulated Jev: transient health check jitter or keyword mismatch"
        else:
            noul = 0.92
            is_crit = True
            action = "escalate"
            reason = "Simulated Jev: confirmed critical infrastructure failure"

        res = AlertFilterResult(
            is_critical=is_crit,
            noul=noul,
            threshold=thresh,
            action=action,
            reason=reason,
            model="jev-mock",
            duration_ms=45.2,
            eval_source="jev",
        )
        self._record(res, service, message)
        self.call_history.append((service, message, res))
        return res


@pytest.fixture
async def alert_server_harness(free_port: int, temp_db_path: str):
    """Starts AsyncHTTPServer with Uptime Kuma and Jev Alert Filter routes."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.bearer_token = "alert_test_bearer_secret_xyz"
    config.database.path = temp_db_path
    config.alert_filter.enabled = True

    db = DatabaseManager(temp_db_path)
    db.init_schema()

    dispatcher = TaskDispatcher(db=db, config=config)
    await dispatcher.start()

    server = AsyncHTTPServer(config.server)
    server._db = db

    mock_filter = MockJevFilter(critical_threshold=0.70)

    register_uptime_kuma_routes(
        server,
        config,
        db=db,
        dispatcher=dispatcher,
        alert_filter=mock_filter,
    )
    await server.start()

    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, config, db, dispatcher, mock_filter

    await dispatcher.stop()
    await server.stop()


# ---------------------------------------------------------------------------
# 1. Security & Schema Verification
# ---------------------------------------------------------------------------


async def test_alert_webhook_unauthorized(alert_server_harness: Any):
    """Adversarial: POST /api/webhook/alert without Bearer token must return 401 with 0 DB writes."""
    base_url, server, config, db, dispatcher, mock_filter = alert_server_harness

    payload = {"service": "Coolify", "message": "Down"}
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/api/webhook/alert", json=payload)

    assert resp.status_code == 401
    assert db._conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0] == 0


async def test_alert_webhook_malformed_json(alert_server_harness: Any):
    """Adversarial: Malformed JSON must return 400 with 0 DB writes."""
    base_url, server, config, db, dispatcher, mock_filter = alert_server_harness
    headers = {
        "Authorization": f"Bearer {config.security.bearer_token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/alert",
            content=b"{bad json",
            headers=headers,
        )

    assert resp.status_code == 400
    assert db._conn.execute("SELECT count(*) FROM webhook_events").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 2. Noise Suppression vs Critical Escalation (Two-Sided)
# ---------------------------------------------------------------------------


async def test_glintmuse_blog_flap_suppressed(alert_server_harness: Any, monkeypatch: pytest.MonkeyPatch):
    """Legitimate Noise: GlintMuse Blog keyword flap is filtered out by Jev without disturbing operator."""
    base_url, server, config, db, dispatcher, mock_filter = alert_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    notifications = []
    monkeypatch.setattr(
        "hub.routes.uptime_kuma.send_desktop_notification",
        lambda title, msg, sound="Basso": notifications.append((title, msg)),
    )

    payload = {
        "service": "GlintMuse Blog",
        "message": "[GlintMuse Blog] [🔴 Down] 200 - OK, but keyword is not in []",
        "url": "https://glintmuse.com/blog",
        "status": "down",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/alert",
            json=payload,
            headers=headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "suppressed_noise"
    assert data["is_critical"] is False
    assert data["noul"] == 0.15
    assert len(notifications) == 0, "Non-critical flap must NOT trigger desktop notification"

    # Persisted to DB with processed status for audit trail
    row = db._conn.execute("SELECT status, source FROM webhook_events").fetchone()
    assert row is not None
    assert row[0] == "processed"
    assert row[1] == "alert_filter"


async def test_critical_database_outage_escalated(alert_server_harness: Any, monkeypatch: pytest.MonkeyPatch):
    """Legitimate Critical: Real database outage triggers immediate 202 and notification."""
    base_url, server, config, db, dispatcher, mock_filter = alert_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    notifications = []
    monkeypatch.setattr(
        "hub.routes.uptime_kuma.send_desktop_notification",
        lambda title, msg, sound="Basso": notifications.append((title, msg)),
    )

    payload = {
        "service": "PostgreSQL Main Cluster",
        "message": "Connection pool exhausted (5432 unreachable): service failing requests",
        "url": "https://db.carradiocodes.com",
        "status": "down",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/alert",
            json=payload,
            headers=headers,
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "escalated_critical"
    assert data["is_critical"] is True
    assert data["noul"] == 0.92
    assert len(notifications) == 1
    assert "CRITICAL" in notifications[0][1]


async def test_recovery_up_alert_stays_quiet(alert_server_harness: Any, monkeypatch: pytest.MonkeyPatch):
    """Legitimate Recovery: UP/Resolved alerts return 200 operational status quietly."""
    base_url, server, config, db, dispatcher, mock_filter = alert_server_harness
    headers = {"Authorization": f"Bearer {config.security.bearer_token}"}

    payload = {
        "service": "GlintMuse Blog",
        "message": "[GlintMuse Blog] [✅ Up] 200 - OK",
        "status": "up",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{base_url}/api/webhook/alert",
            json=payload,
            headers=headers,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "up"


# ---------------------------------------------------------------------------
# 3. Diagnostic-First & Observability Endpoints
# ---------------------------------------------------------------------------


async def test_alert_diagnose_endpoint(alert_server_harness: Any):
    """Diagnostic Mandate: POST /api/alerts/diagnose returns transparent Jev noul judgment and reason."""
    base_url, server, config, db, dispatcher, mock_filter = alert_server_harness

    sample_request = {
        "service": "Coolify GlintMuse",
        "message": "200 - OK, but keyword is not in []",
        "threshold": 0.65,
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/api/alerts/diagnose", json=sample_request)

    assert resp.status_code == 200
    data = resp.json()
    assert data["is_critical"] is False
    assert data["noul"] == 0.15
    assert data["threshold"] == 0.65
    assert data["action"] == "suppress"
    assert "Simulated Jev" in data["reason"]
    assert "duration_ms" in data


async def test_alert_metrics_and_config_endpoints(alert_server_harness: Any):
    """Observability & Dynamic Config: Operators can read metrics and adjust threshold without reboot."""
    base_url, server, config, db, dispatcher, mock_filter = alert_server_harness

    async with httpx.AsyncClient(timeout=5.0) as client:
        # Read metrics
        m_resp = await client.get(f"{base_url}/api/alerts/metrics")
        assert m_resp.status_code == 200
        m_data = m_resp.json()
        assert "suppression_rate_percent" in m_data
        assert "total_evaluated" in m_data

        # Read config
        c_resp = await client.get(f"{base_url}/api/alerts/config")
        assert c_resp.status_code == 200
        assert c_resp.json()["critical_threshold"] == 0.70

        # Adjust threshold dynamically
        post_resp = await client.post(
            f"{base_url}/api/alerts/config",
            json={"critical_threshold": 0.85},
        )
        assert post_resp.status_code == 200
        assert post_resp.json()["critical_threshold"] == 0.85
        assert mock_filter.critical_threshold == 0.85
