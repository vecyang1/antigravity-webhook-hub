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
    BOOST_DELEGATION_RESUSCITATION_PROMPT,
    DEFAULT_RESUSCITATION_PROMPT,
    MCP_ERROR_RESUSCITATION_PROMPT,
    QUOTA_RESUSCITATION_PROMPT,
    SCHEDULE_REMOUNT_PROMPT,
    AntigravityWatchdog,
    StalledSessionInfo,
    detect_parent_conversation_id,
    extract_active_schedule_from_transcript,
    parse_cron_interval_seconds,
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
        mock_quota = MagicMock()
        mock_quota.is_active_account_healthy.return_value = (False, None, {})
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi, quota_sentinel=mock_quota)

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

    def test_subagent_delegation_resuscitates_parent_with_boost_prompt(self, db, mock_agentapi, temp_dir):
        """
        Verify that subagents with parent_conversation_id are resuscitated by awaking
        their PARENT session with BOOST_DELEGATION_RESUSCITATION_PROMPT instead of running in solo mode.
        """
        parent_id = "e6910083-d922-44df-9118-4a572a1e8e60"
        subagent_id = "22222222-2222-3333-4444-555555555559"

        # Create subagent session with caller agent ID in transcript
        steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f'<system_reminder>\ncaller agent (name: "parent", id: "{parent_id}")\n</system_reminder>\n<original_task>\nDo research\n</original_task>',
            },
            {
                "step_index": 1,
                "source": "SYSTEM",
                "type": "ERROR_MESSAGE",
                "status": "ERROR",
                "content": "The stream was interrupted. Please continue the task.",
            },
        ]
        _create_fake_session(temp_dir, subagent_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == subagent_id
        assert item.is_subagent is True
        assert item.parent_conversation_id == parent_id
        assert item.can_resuscitate is True

        res = asyncio.run(watchdog.resuscitate_session(item))
        assert res["success"] is True
        assert res["conversation_id"] == subagent_id
        assert res["target_conversation_id"] == parent_id
        assert res["is_delegated_boost"] is True

        # Verify send_message was called on PARENT, not subagent!
        mock_agentapi.send_message.assert_awaited_once()
        call_kwargs = mock_agentapi.send_message.await_args.kwargs
        assert call_kwargs["conversation_id"] == parent_id
        assert subagent_id in call_kwargs["content"]
        assert "Boost/Delegation" in call_kwargs["content"]

        # Verify DB records
        records = db.list_resuscitations(conversation_id=subagent_id)
        assert len(records) == 1
        assert f"delegated_to_parent:{parent_id}" in (records[0]["error_details"] or "")

    def test_resuscitate_stalled_sessions_deduplicates_parent_awakenings(self, db, mock_agentapi, temp_dir):
        """
        Verify that multiple subagents belonging to the same parent do not spam the parent
        multiple times in a single tick.
        """
        parent_id = "e6910083-d922-44df-9118-4a572a1e8e60"
        subagent1 = "11111111-2222-3333-4444-555555555551"
        subagent2 = "11111111-2222-3333-4444-555555555552"

        for s_id in (subagent1, subagent2):
            steps = [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "content": f'<system_reminder>\ncaller agent (name: "parent", id: "{parent_id}")\n</system_reminder>\n<original_task>\nTask\n</original_task>',
                },
                {
                    "step_index": 1,
                    "source": "SYSTEM",
                    "type": "ERROR_MESSAGE",
                    "status": "ERROR",
                    "content": "The stream was interrupted.",
                },
            ]
            _create_fake_session(temp_dir, s_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 2

        results = asyncio.run(watchdog.resuscitate_stalled_sessions())
        # Only 1 parent resuscitation was dispatched in this tick
        assert len(results) == 1
        assert results[0]["target_conversation_id"] == parent_id
        assert mock_agentapi.send_message.await_count == 1

    def test_quota_sentinel_clears_cooldown_when_account_healthy(self, db, mock_agentapi, temp_dir):
        """
        Verify that if QuotaSentinel reports active account is healthy (>10%),
        active quota cooldowns in SQLite SSOT are automatically cleared.
        """
        convo_id = "test-quota-cooldown-cleared-001"
        # Seed an active cooldown
        db.record_resuscitation(
            conversation_id=convo_id,
            status="quota_cooldown",
            last_error="Individual quota reached",
            cooldown_until=time.time() + 3600.0,
        )
        assert len(db.list_quota_cooldowns()) == 1

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        mock_sentinel = MagicMock()
        mock_sentinel.is_active_account_healthy.return_value = (True, "viinam33@gmail.com", {})

        watchdog = AntigravityWatchdog(
            db=db,
            config=config,
            agentapi_client=mock_agentapi,
            quota_sentinel=mock_sentinel,
        )

        is_healthy, email, cleared = watchdog.check_and_clear_quota_cooldowns()
        assert is_healthy is True
        assert email == "viinam33@gmail.com"
        assert cleared == 1

        # Verify DB cooldown was unlocked
        assert len(db.list_quota_cooldowns()) == 0

    def test_language_server_pid_change_triggers_immediate_schedule_recovery(self, db, mock_agentapi, temp_dir):
        """
        Verify that when language_server restarts (PID change), in-memory conversation
        schedules are immediately detected as lost without waiting for standard timeout.
        """
        convo_id = "test-pid-change-schedule-recovery"
        now = time.time()
        # Active schedule in DB, trigger was recent (only 2 minutes ago, normal timeout is > 37.5m)
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/30 * * * *",
            prompt="Run sync",
            expected_interval_seconds=1800,
            last_trigger_at=now - 120.0,
        )

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Schedule sync"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Sync complete", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=120.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        # Set prior PID in DB and watchdog
        watchdog.last_known_ls_pid = 1234
        db.set_metadata("last_known_ls_pid", "1234")

        # Mock agentapi reporting new PID
        mock_agentapi.get_language_server_pid.return_value = 5678

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.has_active_schedule is True
        assert "language_server restarted: PID 1234 -> 5678" in item.last_error
        assert item.can_resuscitate is True

    def test_parse_cron_interval_seconds_various_expressions(self):
        """Verify cron interval parsing for standard cron formats."""
        assert parse_cron_interval_seconds("*/10 * * * *") == 600
        assert parse_cron_interval_seconds("0 * * * *") == 3600
        assert parse_cron_interval_seconds("0 */2 * * *") == 7200
        assert parse_cron_interval_seconds("0 9 * * *") == 86400
        assert parse_cron_interval_seconds("0 0 * * 1") == 604800
        assert parse_cron_interval_seconds("invalid_cron", default_interval=1800) == 1800

    def test_agentapi_resolves_project_id_and_uses_in_env(self, temp_dir):
        """Verify AgentAPIClient respects target conversation's project_id."""
        import sqlite3
        convo_id = "test-project-id-convo"
        conv_dir = temp_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        db_path = conv_dir / f"{convo_id}.db"

        # Create dummy conversation sqlite db with project_id in workspace metadata
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
        # Protobuf data containing project id
        conn.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (b"\x12\x07my-proj-12345678-1234-1234-1234-123456789abc",))
        conn.commit()
        conn.close()

        client = AgentAPIClient()
        resolved_pid = client.resolve_conversation_project_id(convo_id, conversations_dir=conv_dir)
        assert resolved_pid == "12345678-1234-1234-1234-123456789abc"

        env = client._get_env(project_id="explicit-custom-project")
        assert env.get("ANTIGRAVITY_PROJECT_ID") == "explicit-custom-project"

    def test_hallucinated_schedule_text_without_tool_call_is_not_registered(self, temp_dir):
        """
        Verify adversarial case: Agent output claims in text '已调用 schedule 挂载定时任务 task-1212',
        but tool_calls list is empty. extract_active_schedule_from_transcript must return None
        so that false claims are not trusted as genuine schedule mount receipts.
        """
        convo_id = "test-hallucinated-schedule-text"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Set a schedule for every 30 minutes"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "好的，我已经通过 schedule 工具为您挂载了 task-1212 定时任务，每 30 分钟触发一次。",
                "tool_calls": [],  # EMPTY! Hallucination / lie!
            },
        ]
        t_path = _create_fake_session(temp_dir, convo_id, steps)
        sched = extract_active_schedule_from_transcript(t_path)
        assert sched is None




