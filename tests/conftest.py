"""Shared pytest fixtures and configuration for Antigravity Webhook Hub test suite.

Supports Tiers 1-4 opaque-box testing across unit, api, and e2e boundaries.
Provides cryptographic signature generators, isolated temporary SQLite databases,
and an ephemeral HTTP test server harness.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Generator

import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def pytest_pyfunc_call(pyfuncitem: Any) -> bool | None:
    """Enables native execution of `async def test_*` without external pytest-asyncio plugin."""
    testfunction = pyfuncitem.obj
    if inspect.iscoroutinefunction(testfunction):
        argnames = pyfuncitem._fixtureinfo.argnames
        funcargs = {arg: pyfuncitem.funcargs[arg] for arg in argnames}
        asyncio.run(testfunction(**funcargs))
        return True
    return None


class AuthHelper:
    """Authoritative cryptographic test helper for HMAC SHA-256 and Bearer auth."""

    DEFAULT_SECRET = "test_hmac_secret_32bytes_12345678"
    DEFAULT_BEARER = "test_bearer_token_secret_12345678"

    @staticmethod
    def sign_payload(
        secret: str | bytes,
        body: bytes | str,
        timestamp: int | None = None,
        prefix: bool = True,
    ) -> tuple[str, int]:
        """Calculates authoritative HMAC-SHA256 signature binding timestamp and raw body.

        Signature = HMAC-SHA256(secret, timestamp + '.' + raw_body)
        """
        sec_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        ts = int(time.time()) if timestamp is None else timestamp
        message = f"{ts}.".encode("utf-8") + body_bytes
        digest = hmac.new(sec_bytes, message, hashlib.sha256).hexdigest()
        sig = f"sha256={digest}" if prefix else digest
        return sig, ts

    @classmethod
    def make_headers(
        cls,
        body: bytes | str,
        secret: str | bytes = DEFAULT_SECRET,
        timestamp: int | None = None,
        event_id: str | None = None,
        prefix: bool = True,
    ) -> dict[str, str]:
        """Creates valid HMAC signature headers."""
        sig, ts = cls.sign_payload(secret, body, timestamp, prefix=prefix)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(ts),
        }
        if event_id:
            headers["X-Hub-Event-ID"] = event_id
        return headers

    @classmethod
    def make_tampered_headers(
        cls,
        body: bytes | str,
        secret: str | bytes = DEFAULT_SECRET,
        timestamp: int | None = None,
    ) -> dict[str, str]:
        """Creates headers where the signature has been tampered with by 1 character."""
        sig, ts = cls.sign_payload(secret, body, timestamp)
        # Flip the last character of the hex digest
        last_char = "0" if sig[-1] != "0" else "1"
        tampered_sig = sig[:-1] + last_char
        return {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": tampered_sig,
            "X-Hub-Timestamp": str(ts),
        }

    @classmethod
    def make_expired_headers(
        cls,
        body: bytes | str,
        secret: str | bytes = DEFAULT_SECRET,
        drift_seconds: int = 350,
    ) -> dict[str, str]:
        """Creates headers with timestamp older than the 300s replay window."""
        expired_ts = int(time.time()) - drift_seconds
        sig, ts = cls.sign_payload(secret, body, timestamp=expired_ts)
        return {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(ts),
        }

    @classmethod
    def make_future_headers(
        cls,
        body: bytes | str,
        secret: str | bytes = DEFAULT_SECRET,
        drift_seconds: int = 120,
    ) -> dict[str, str]:
        """Creates headers with timestamp too far into the future (>60s)."""
        future_ts = int(time.time()) + drift_seconds
        sig, ts = cls.sign_payload(secret, body, timestamp=future_ts)
        return {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(ts),
        }

    @classmethod
    def make_bearer_headers(cls, token: str = DEFAULT_BEARER) -> dict[str, str]:
        """Creates valid Bearer authorization headers."""
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }


@pytest.fixture
def auth_helpers() -> AuthHelper:
    """Fixture providing cryptographic auth utilities."""
    return AuthHelper()


@pytest.fixture
def temp_db_path(tmp_path: Path) -> str:
    """Provides isolated path for temporary SQLite database."""
    db_file = tmp_path / "test_hub.db"
    return str(db_file)


@pytest.fixture
def free_port() -> int:
    """Allocates a free ephemeral TCP port for local server testing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mock_webhook_payload() -> dict[str, Any]:
    """Sample valid webhook payload dictionary."""
    return {
        "event": "push",
        "action": "cli",
        "command": "echo 'antigravity_hub_test_ok'",
        "repository": {"name": "antigravity-webhook-hub", "owner": "vec"},
        "sender": {"login": "vecsat"},
    }


@pytest.fixture
def mock_task_payload() -> dict[str, Any]:
    """Sample valid task execution payload dictionary."""
    return {
        "action": "cli",
        "command": "python3 -c \"print('task_execution_verified')\"",
        "timeout_seconds": 10,
    }
