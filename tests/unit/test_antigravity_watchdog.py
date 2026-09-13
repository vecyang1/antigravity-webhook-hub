"""
Tests for Antigravity Watchdog & Auto Pull-Up Engine
Verifies both sides of truth:
1. Positive path: detecting interrupted streams, terminated subagents, and resuscitating them.
2. Adversarial path: healthy sessions ignored, fail-closed when offline, circuit breaker on max retries,
   and grace period protection against race conditions.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub.antigravity.agentapi_client import AgentAPIClient
from hub.antigravity.watchdog import (
    DEFAULT_RESUSCITATION_PROMPT,
    AntigravityWatchdog,
    StalledSessionInfo,
)
from hub.config import AntigravityWatchdogConfig
from hub.db import DatabaseManager


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="test_watchdog_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db(temp_dir):
    db_file = temp_dir / "test_watchdog.db"
    manager = DatabaseManager(str(db_file))
    manager.init_schema()
    return manager


@pytest.fixture
def mock_agentapi():
    client = MagicMock(spec=AgentAPIClient)
    client.executable_path = "/fake/bin/agentapi"
    client.is_available.return_value = True
    client.ensure_credentials.return_value = ("127.0.0.1:50858", "csrf_token_xyz")
    client.send_message = AsyncMock(return_value=(True, '{"status": "ok"}', None))
    return client


def _create_fake_session(
    brain_dir: Path,
    convo_id: str,
    lines: list[dict],
    mtime_offset_seconds: float = 30.0,
):
    log_dir = brain_dir / convo_id / ".system_generated" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    trans_file = log_dir / "transcript.jsonl"
    with open(trans_file, "w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    # Adjust mtime
    new_mtime = time.time() - mtime_offset_seconds
    os.utime(trans_file, (new_mtime, new_mtime))
    os.utime(brain_dir / convo_id, (new_mtime, new_mtime))
    return trans_file


class TestAntigravityWatchdog:

    def test_network_health_check_online_and_offline(self, db, mock_agentapi, temp_dir):
        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            probe_host="1.1.1.1",
            probe_port=53,
            probe_timeout_seconds=0.1,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        # Mock socket success
        with patch("socket.socket") as mock_sock:
            inst = MagicMock()
            mock_sock.return_value = inst
            inst.connect.return_value = None
            assert watchdog.check_network_health() is True

        # Mock socket failure (both primary and fallback offline)
        with patch("socket.socket") as mock_sock:
            inst = MagicMock()
            mock_sock.return_value = inst
            inst.connect.side_effect = OSError("Network is unreachable")
            assert watchdog.check_network_health() is False

    def test_positive_path_detect_interrupted_stream_and_resuscitate(self, db, mock_agentapi, temp_dir):
        """Positive path: interrupted stream detected and successfully pulled up via agentapi."""
        convo_id = "test-convo-interrupted-001"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Run report"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "The stream was interrupted. Please continue the task you were working on."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
            lookback_minutes=60,
            max_retries_per_session=3,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].conversation_id == convo_id
        assert stalled[0].can_resuscitate is True
        assert "interrupted" in stalled[0].last_error.lower()

        # Resuscitate
        res = asyncio.run(watchdog.resuscitate_session(stalled[0]))
        assert res["success"] is True
        assert res["status"] == "resuscitated"
        mock_agentapi.send_message.assert_awaited_once()

        # Verify DB SSOT record
        records = db.list_resuscitations(conversation_id=convo_id)
        assert len(records) == 1
        assert records[0]["status"] == "resuscitated"
        assert records[0]["attempt_count"] == 1

    def test_detect_terminated_subagent_from_screenshot_pattern(self, db, mock_agentapi, temp_dir):
        """Verify that subagent termination ('Agent execution terminated due to error') is detected."""
        subagent_id = "dfbc889e-dc9c-44da-a8c4-32d6507d3858"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "<original_task>\nTask: /boost DeepInvestigator\n</original_task>"},
            {"step_index": 100, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "curl output"},
            {"step_index": 101, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "Error Unknown: Agent execution terminated due to error. Error ID: 3444af61-7f2c"},
        ]
        _create_fake_session(temp_dir, subagent_id, steps, mtime_offset_seconds=25.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == subagent_id
        assert item.is_subagent is True
        assert item.can_resuscitate is True

        res = asyncio.run(watchdog.resuscitate_session(item))
        assert res["success"] is True

    def test_adversarial_healthy_session_not_touched(self, db, mock_agentapi, temp_dir):
        """Adversarial check: normal active or finished sessions MUST NOT be interrupted or sent messages."""
        convo_id = "test-healthy-convo-002"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Hello"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Here is the completed answer."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=40.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0

    def test_adversarial_stall_grace_period_protection(self, db, mock_agentapi, temp_dir):
        """Adversarial check: recent errors within grace period (e.g. 5s ago) should not be rushed into."""
        convo_id = "test-recent-error-003"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Do work"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "The stream was interrupted."},
        ]
        # Only 3 seconds ago, while grace period is 15s
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=3.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].can_resuscitate is False
        assert stalled[0].skip_reason == "within_stall_grace_period"

    def test_adversarial_fail_closed_when_network_offline(self, db, mock_agentapi, temp_dir):
        """Adversarial check: if network is offline, watchdog fails closed and does not dispatch messages."""
        convo_id = "test-offline-convo-004"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Do work"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "The stream was interrupted."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        with patch.object(watchdog, "check_network_health", return_value=False):
            results = asyncio.run(watchdog.resuscitate_stalled_sessions())
            assert len(results) == 0
            mock_agentapi.send_message.assert_not_called()

    def test_circuit_breaker_max_retries_exhausted(self, db, mock_agentapi, temp_dir):
        """Circuit breaker check: sessions reaching max retries are marked exhausted and not repeated."""
        convo_id = "test-infinite-loop-breaker-005"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Do work"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "The stream was interrupted."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        # Pre-seed 3 prior failed attempts in database
        for _ in range(3):
            db.record_resuscitation(
                conversation_id=convo_id,
                sidecar_slug=None,
                last_error="stream interrupted",
                status="failed",
                resuscitation_prompt="prompt",
                last_step_index=1,
            )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), max_retries_per_session=3)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].can_resuscitate is False
        assert "max_retries_exhausted" in stalled[0].skip_reason

        # Attempting resuscitation directly
        res = asyncio.run(watchdog.resuscitate_stalled_sessions())
        assert len(res) == 0
        mock_agentapi.send_message.assert_not_called()

    def test_watchdog_status_diagnostics(self, db, mock_agentapi, temp_dir):
        """Verify status reporting contract."""
        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        with patch.object(watchdog, "check_network_health", return_value=True):
            status = watchdog.get_status()
            assert status["watchdog_enabled"] is True
            assert status["network_online"] is True
            assert status["agentapi_available"] is True
            assert status["language_server_connected"] is True
            assert "stalled_sessions" in status
            assert "recent_resuscitations" in status

    def test_adversarial_completed_turn_mentioning_error_keyword_not_flagged(self, db, mock_agentapi, temp_dir):
        """A normal completed model turn that discusses 'the stream was interrupted' in text or code must not be flagged."""
        convo_id = "test-completed-discussing-error"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Explain watchdog"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "I fixed the bug where the stream was interrupted."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0

    def test_adversarial_recovered_session_after_resuscitation_not_flagged(self, db, mock_agentapi, temp_dir):
        """If an error occurred earlier but the model subsequently answered, it is recovered and must not be flagged."""
        convo_id = "test-already-recovered"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Execute task"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "The stream was interrupted."},
            {"step_index": 2, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "content": "System network recovery reminder"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "I have resumed and completed the work."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0

