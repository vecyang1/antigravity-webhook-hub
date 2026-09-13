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
    MCP_ERROR_RESUSCITATION_PROMPT,
    QUOTA_RESUSCITATION_PROMPT,
    SCHEDULE_REMOUNT_PROMPT,
    AntigravityWatchdog,
    StalledSessionInfo,
    extract_active_schedule_from_transcript,
    parse_quota_reset_seconds,
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

    def test_parse_quota_reset_seconds(self):
        """Verify parsing of various Google RPC quota reset delay formats."""
        assert parse_quota_reset_seconds("Error Individual quota reached. Resets in 1h54m13s. Error ID: xyz") == 6853.0
        assert parse_quota_reset_seconds("Resets in 45m10s") == 2710.0
        assert parse_quota_reset_seconds("Resets in 30s") == 30.0
        assert parse_quota_reset_seconds('retryDelay": "6853.667943359s"') == 6853.667943359
        assert parse_quota_reset_seconds('quotaResetDelay": "1h0m0s"') == 3600.0
        assert parse_quota_reset_seconds("Unrelated error message") is None
        assert parse_quota_reset_seconds("") is None

    def test_quota_exhaustion_quarantine_and_pull_up_after_cooldown(self, db, mock_agentapi, temp_dir):
        """
        Verify that sessions hitting quota limits enter quota_cooldown,
        do NOT exhaust normal retry counts, and automatically pull up with
        QUOTA_RESUSCITATION_PROMPT once cooldown expires.
        """
        convo_id = "test-quota-cooldown-session"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Generate 100 images"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "Error Individual quota reached. Resets in 1h54m13s. Error ID: 21fddb5c-89f8-488e-b690-33cee371397f-593"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        # 1. Initial scan detects quota exhaustion -> registers quota_cooldown
        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.is_quota_exhausted is True
        assert item.quota_resets_in_seconds == 6853.0
        assert item.can_resuscitate is False
        assert "quota_cooldown" in item.skip_reason

        # Verify DB records
        active_cd = db.get_active_quota_cooldown(convo_id)
        assert active_cd is not None
        assert active_cd["status"] == "quota_cooldown"
        assert active_cd["cooldown_until"] > time.time()
        # Cooldown does not consume retry attempts
        assert db.get_resuscitation_attempts(convo_id) == 0

        # Attempting resuscitation during cooldown must not fire messages
        res = asyncio.run(watchdog.resuscitate_stalled_sessions())
        assert len(res) == 0
        mock_agentapi.send_message.assert_not_called()

        # 2. Advance time past cooldown_until
        db.update_resuscitation_status(
            active_cd["resuscitation_id"],
            "quota_cooldown",
            cooldown_until=time.time() - 10.0,
        )

        # 3. Next scan detects cooldown expired -> eligible for pull-up
        stalled_after = watchdog.scan_stalled_conversations()
        assert len(stalled_after) == 1
        item_after = stalled_after[0]
        assert item_after.can_resuscitate is True
        assert item_after.last_error == "quota_restored_pull_up"

        # 4. Pull-up triggers specialized QUOTA_RESUSCITATION_PROMPT
        pull_up_res = asyncio.run(watchdog.resuscitate_session(item_after))
        assert pull_up_res["success"] is True
        mock_agentapi.send_message.assert_awaited_once_with(
            conversation_id=convo_id,
            content=QUOTA_RESUSCITATION_PROMPT,
            timeout_seconds=30,
        )

    def test_resuscitation_failure_due_to_quota_enters_cooldown(self, db, mock_agentapi, temp_dir):
        """
        Verify that if agentapi returns a 429 quota exhaustion error during resuscitation,
        the watchdog safely transitions the session to quota_cooldown without consuming retry budget.
        """
        convo_id = "test-pullup-quota-error"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Execute task"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "The stream was interrupted."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        # Mock agentapi failure with 429 quota exhaustion
        mock_agentapi.send_message = AsyncMock(
            return_value=(False, None, "429 Client Error: Error Individual quota reached. Resets in 1h30m0s. Error ID: test-err")
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        res = asyncio.run(watchdog.resuscitate_session(stalled[0]))

        assert res["success"] is False
        assert res["status"] == "quota_cooldown"
        assert res["cooldown_until"] > time.time()

        # Check DB state
        cd = db.get_active_quota_cooldown(convo_id)
        assert cd is not None
        assert cd["status"] == "quota_cooldown"
        assert db.get_resuscitation_attempts(convo_id) == 0

    def test_mcp_error_hang_detection_and_prompt(self, db, mock_agentapi, temp_dir):
        """
        Verify Scenario 2: empty planner response hang following MCP tool call is detected
        as an MCP error and receives MCP_ERROR_RESUSCITATION_PROMPT.
        """
        convo_id = "test-mcp-error-hang-session"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Browse page"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Calling browser tool",
                "tool_calls": [{"name": "mcp_browser_click", "args": {}}],
            },
            {"step_index": 2, "source": "SYSTEM", "type": "GENERIC", "status": "DONE", "content": "⚠️ MCP Error: Connection refused"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.is_mcp_error is True
        assert item.last_error == "mcp_error_hang"
        assert item.can_resuscitate is True

        res = asyncio.run(watchdog.resuscitate_session(item))
        assert res["success"] is True
        mock_agentapi.send_message.assert_awaited_once_with(
            conversation_id=convo_id,
            content=MCP_ERROR_RESUSCITATION_PROMPT,
            timeout_seconds=30,
        )

    def test_in_memory_schedule_loss_after_restart_detection_and_remount(self, db, mock_agentapi, temp_dir):
        """
        Verify Scenario 3: session with an in-memory /schedule cron that has dropped
        heartbeats after restart is detected and receives SCHEDULE_REMOUNT_PROMPT.
        """
        from datetime import datetime, timezone
        convo_id = "test-lost-schedule-restart-session"
        last_trig_time = time.time() - 4000  # > 1 hour ago
        created_iso = datetime.fromtimestamp(last_trig_time, tz=timezone.utc).isoformat()

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Monitor inbox"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Scheduling check",
                "tool_calls": [{"name": "schedule", "args": {"CronExpression": "*/30 * * * *", "Prompt": "Check email"}}],
            },
            {"step_index": 2, "source": "SYSTEM", "type": "CRON_TRIGGER", "status": "DONE", "content": "Cron trigger #1 for inbox check", "created_at": created_iso},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Checked inbox, nothing new.", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=4000.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), lookback_minutes=120)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.has_active_schedule is True
        assert item.active_cron_expression == "*/30 * * * *"
        assert item.can_resuscitate is True

        # Verify schedule was saved in DB
        sched = db.get_conversation_schedule(convo_id)
        assert sched is not None
        assert sched["cron_expression"] == "*/30 * * * *"

        # Resuscitate
        res = asyncio.run(watchdog.resuscitate_session(item))
        assert res["success"] is True
        assert res["status"] == "schedule_remounted"
        expected_prompt = SCHEDULE_REMOUNT_PROMPT.format(cron="*/30 * * * *")
        mock_agentapi.send_message.assert_awaited_once_with(
            conversation_id=convo_id,
            content=expected_prompt,
            timeout_seconds=30,
        )

        # Verify schedule trigger time in DB updated to now
        sched_updated = db.get_conversation_schedule(convo_id)
        assert sched_updated["last_trigger_at"] > last_trig_time

    def test_checkpoint_compaction_with_quota_text_not_false_positive(self, db, mock_agentapi, temp_dir):
        """
        Verify that a compaction CHECKPOINT step mentioning 'Individual quota reached' in its
        historical summary does NOT trigger false-positive quota quarantine on active sessions.
        """
        convo_id = "test-compaction-summary-quota-text"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "How to handle 429?"},
            {
                "step_index": 1,
                "source": "SYSTEM",
                "type": "CHECKPOINT",
                "status": "DONE",
                "content": "# Resuming from a compaction\nSummary: Previously discussed Error Individual quota reached. Resets in 1h54m13s.",
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Here is the solution to handle it cleanly.",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=5.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0
        assert db.list_quota_cooldowns() == []



