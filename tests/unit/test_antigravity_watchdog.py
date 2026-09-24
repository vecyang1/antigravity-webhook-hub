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
    BOOST_GOAL_RESUSCITATION_PROMPT,
    BOOST_SERVER_RESTART_RESUSCITATION_PROMPT,
    DEFAULT_RESUSCITATION_PROMPT,
    EMPTY_RESPONSE_RESUSCITATION_PROMPT,
    MCP_ERROR_RESUSCITATION_PROMPT,
    NETWORK_ERROR_RESUSCITATION_PROMPT,
    QUOTA_RESUSCITATION_PROMPT,
    SCHEDULE_REMOUNT_PROMPT,
    SERVER_RESTART_RESUSCITATION_PROMPT,
    TOOL_RESULT_RESUSCITATION_PROMPT,
    AntigravityWatchdog,
    StalledSessionInfo,
    check_session_has_boost_or_goal,
    detect_parent_conversation_id,
    extract_active_schedule_from_transcript,
    get_language_server_start_time,
    get_unfinished_conversations_from_summaries,
    inspect_conversation_db_for_terminal_network_error,
    is_subagent_active,
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
    client.get_language_server_pid.return_value = None
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
        parent_id = "e073afd7-2ae8-4c32-9dd9-4ba7b851d3e3"
        steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f'<subagent_reminder>\ninvoked by a caller agent (name: "parent", id: "{parent_id}")\n</subagent_reminder>\n<original_task>\nTask: /boost DeepInvestigator\n</original_task>',
            },
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

    def test_scan_detects_stop_hook_blocked_termination_and_empty_hang(self, db, mock_agentapi, temp_dir):
        """
        Verify that a session blocked by stop hook followed by an empty planner response
        is accurately detected as stop_hook_hang and marked for resuscitation.
        """
        convo_id = "test-stop-hook-hang-001"
        steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "/goal complete setup /boost if need",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "I will conclude here.",
                "tool_calls": [],
            },
            {
                "step_index": 2,
                "source": "SYSTEM",
                "type": "GENERIC",
                "status": "DONE",
                "content": "Stop hook blocked termination: You are still working toward the user's goal. Continue executing.",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.is_stop_hook_hang is True
        assert item.is_boost_goal is True
        assert item.last_error == "stop_hook_hang"
        assert item.can_resuscitate is True

    def test_scan_detects_stop_hook_as_last_step_halt(self, db, mock_agentapi, temp_dir):
        """
        Verify that if the last step in a session is a Stop Hook block and agent halts,
        watchdog identifies it as stop_hook_hang after stall grace seconds.
        """
        convo_id = "test-stop-hook-last-step-002"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Run tests"},
            {"step_index": 1, "source": "SYSTEM", "type": "GENERIC", "status": "DONE", "content": "Stop hook blocked termination: You are still working toward the user's goal."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.last_error == "stop_hook_hang"
        assert item.is_stop_hook_hang is True
        assert item.can_resuscitate is True

    def test_resuscitate_boost_goal_session_dispatches_boost_goal_prompt(self, db, mock_agentapi, temp_dir):
        """
        Verify that resuscitating a session with /boost or /goal dispatches BOOST_GOAL_RESUSCITATION_PROMPT
        to prevent degrading into a solo uncoordinated state.
        """
        convo_id = "test-boost-goal-dispatch-003"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "/adspower-browser e2e tested /boost if need; /goal"},
            {"step_index": 1, "source": "SYSTEM", "type": "GENERIC", "status": "DONE", "content": "Stop hook blocked termination: You are still working toward the user's goal."},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.is_boost_goal is True

        res = asyncio.run(watchdog.resuscitate_session(item))
        assert res["success"] is True
        mock_agentapi.send_message.assert_awaited_once()
        sent_content = mock_agentapi.send_message.await_args.kwargs["content"]
        assert sent_content == BOOST_GOAL_RESUSCITATION_PROMPT
        assert sent_content != EMPTY_RESPONSE_RESUSCITATION_PROMPT
        assert "严禁降级 Solo 模式" in sent_content
        assert "统一工具链与原生收敛规范" in sent_content

    def test_mcp_error_detected_across_wide_window_with_stop_hook_loop(self, db, mock_agentapi, temp_dir):
        """
        Verify that if an MCP tool error happened earlier in context followed by stop hook blocks,
        the widened 15-step detection window correctly tags it as stop_hook_mcp_hang.
        """
        convo_id = "test-mcp-stop-hook-loop-004"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Automate browser"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Opening browser",
                "tool_calls": [{"name": "mcp_browser_action", "args": {}}],
            },
            {"step_index": 2, "source": "SYSTEM", "type": "GENERIC", "status": "DONE", "content": "⚠️ MCP Error: Browser disconnected"},
            {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Trying to exit"},
            {"step_index": 4, "source": "SYSTEM", "type": "GENERIC", "status": "DONE", "content": "Stop hook blocked termination: You are still working toward the user's goal."},
            {"step_index": 5, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "The stream was interrupted."},
            {"step_index": 6, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.is_mcp_error is True
        assert item.is_stop_hook_hang is True
        assert item.last_error == "stop_hook_mcp_hang"
        assert item.can_resuscitate is True

    def test_interrupted_stream_patterns_matches_the_stream_was_interrupted(self, db, mock_agentapi, temp_dir):
        """
        Verify that 'Error: The stream was interrupted' is matched by INTERRUPTED_STREAM_PATTERNS.
        """
        convo_id = "test-stream-interrupted-005"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Process data"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "Error: The stream was interrupted"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].last_error == "the stream was interrupted"
        assert stalled[0].can_resuscitate is True

    def test_resuscitate_failure_records_error_details_in_db(self, db, mock_agentapi, temp_dir):
        """
        Verify that when agentapi send_message fails, the error_details string is correctly
        persisted into the SQLite resuscitation record for auditing.
        """
        convo_id = "test-resuscitate-failure-audit-006"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Execute task"},
            {"step_index": 1, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "network error"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        mock_agentapi.send_message = AsyncMock(
            return_value=(False, None, "Connection refused: language server offline")
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1

        res = asyncio.run(watchdog.resuscitate_session(stalled[0]))
        assert res["success"] is False

        records = db.list_resuscitations(conversation_id=convo_id)
        assert len(records) == 1
        assert records[0]["status"] == "failed"
        assert records[0]["error_details"] == "Connection refused: language server offline"

    def test_schedule_mount_sets_baseline_and_prevents_spurious_pullup(self, db, mock_agentapi, temp_dir):
        """
        Verify Bug Fix: When a schedule is mounted (e.g. 5 minutes ago),
        an old trigger from > 40 minutes ago does NOT cause a spurious pull-up
        because baseline_time considers max(last_trigger_time, last_schedule_time).
        """
        from datetime import datetime, timezone
        convo_id = "test-schedule-baseline-no-spurious-pullup"
        now = time.time()
        # Old trigger was 45 minutes ago (longer than 37.5m timeout for 30m cron)
        old_trig_time = now - 2700.0
        old_trig_iso = datetime.fromtimestamp(old_trig_time, tz=timezone.utc).isoformat()
        # Schedule was mounted 5 minutes ago (300s)
        sched_time = now - 300.0
        sched_iso = datetime.fromtimestamp(sched_time, tz=timezone.utc).isoformat()

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Schedule check"},
            {"step_index": 1, "source": "SYSTEM", "type": "CRON_TRIGGER", "status": "DONE", "content": "巡检汇报", "created_at": old_trig_iso},
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": sched_iso,
                "tool_calls": [{"name": "schedule", "args": {"CronExpression": "*/30 * * * *", "Prompt": "Check email"}}],
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": sched_iso,
                "content": "### 巡检与后台守护重新挂载汇报\n已重新挂载定时任务 task-1212",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=300.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        # MUST NOT be flagged as stalled because the schedule was mounted only 5m ago!
        assert len(stalled) == 0

    def test_schedule_ls_pid_persistence_resilient_to_interim_status_checks(self, db, mock_agentapi, temp_dir):
        """
        Verify Bug Fix: When language_server restarts (PID 1000 -> 2000),
        an interim read-only get_status() or /antigravity/status query does NOT
        swallow the restart event. The schedule's ls_pid in DB ensures background
        resuscitation still recognizes and pulls up the session.
        """
        convo_id = "test-ls-pid-persistence"
        now = time.time()
        # Schedule was mounted in language_server PID 1000
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/30 * * * *",
            prompt="Check email",
            expected_interval_seconds=1800,
            last_trigger_at=now - 100.0,
            ls_pid=1000,
        )

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Schedule check"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Running schedule",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=100.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.last_known_ls_pid = 1000
        db.set_metadata("last_known_ls_pid", "1000")

        # language_server restarted with PID 2000
        mock_agentapi.get_language_server_pid.return_value = 2000

        # 1. External read-only status query happens first
        status = watchdog.get_status()
        assert status["stalled_sessions_detected"] == 1
        assert watchdog.last_known_ls_pid == 2000

        # 2. Background worker runs afterwards
        # Even though last_known_ls_pid is now 2000 (pid_changed would be False),
        # the schedule's ls_pid in DB is still 1000 != 2000!
        res = asyncio.run(watchdog.resuscitate_stalled_sessions())
        assert len(res) == 1
        assert res[0]["success"] is True
        assert res[0]["conversation_id"] == convo_id

        # 3. Verify schedule in DB now has ls_pid updated to 2000
        sched = db.get_conversation_schedule(convo_id)
        assert sched["ls_pid"] == 2000

        # 4. Subsequent tick does NOT repeat resuscitation
        res2 = asyncio.run(watchdog.resuscitate_stalled_sessions())
        assert len(res2) == 0

    def test_save_conversation_schedule_monotonic_timestamp(self, db):
        """Verify DB timestamps never roll backward upon repeated upserts."""
        convo_id = "test-monotonic-ts"
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/30 * * * *",
            prompt="Test",
            last_trigger_at=1000.0,
        )
        s1 = db.get_conversation_schedule(convo_id)
        assert s1["last_trigger_at"] == 1000.0

        # Attempt to overwrite with older timestamp (500.0)
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/30 * * * *",
            prompt="Test",
            last_trigger_at=500.0,
        )
        s2 = db.get_conversation_schedule(convo_id)
        # MUST remain 1000.0!
        assert s2["last_trigger_at"] == 1000.0

    def test_stop_hook_halt_with_text_without_goal_complete_is_detected(self, db, mock_agentapi, temp_dir):
        """
        Verify that if the model was blocked by Stop Hook, then outputs explanatory text
        without tool calls and without <!-- GOAL_COMPLETE -->, it is NOT falsely treated
        as a clean completion at line 898; it must be flagged as stop_hook_hang and pulled up.
        """
        convo_id = "test-stop-hook-text-halt-007"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Set up payment gateway /goal"},
            {"step_index": 1, "source": "SYSTEM", "type": "GENERIC", "status": "DONE", "content": "Stop hook blocked termination: You are still working toward the user's goal."},
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "I am unable to proceed because of network connection issues.",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.is_stop_hook_hang is True
        assert item.last_error == "stop_hook_hang"
        assert item.can_resuscitate is True

    def test_scan_detects_completed_tool_result_hang(self, db, mock_agentapi, temp_dir):
        """
        Verify that when a tool call completes successfully (MODEL GENERIC DONE),
        but the model never initiates or completes the subsequent planner turn,
        Watchdog identifies it as tool_result_hang (or tool_result_boost_goal_hang)
        and resuscitates with the appropriate prompt.
        """
        convo_id = "test-tool-result-hang-008"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Search mail for xinchaovi"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Searching mail now",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "python3 check_mail.py"}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "content": "Clean HTML Body: Welcome Dear Vector Yang... exit code 0",
                "tool_calls": None,
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=15)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.last_error == "tool_result_hang"
        assert item.can_resuscitate is True

        res = asyncio.run(watchdog.resuscitate_session(item))
        assert res["success"] is True
        mock_agentapi.send_message.assert_awaited_once()
        sent_content = mock_agentapi.send_message.await_args.kwargs["content"]
        assert sent_content == TOOL_RESULT_RESUSCITATION_PROMPT
        assert "工具已执行完成并返回结果" in sent_content

    def test_schedule_remount_updates_ls_pid_and_prevents_looping(self, db, mock_agentapi, temp_dir):
        """
        Verify that once an agent mounts a schedule or is remounted on the current language_server,
        the ls_pid in SQLite is updated so that subsequent ticks do not get caught in a false
        lost_due_to_pid loop.
        """
        convo_id = "test-schedule-pid-remount-loop-009"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Monitor inbox"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Mounted cron",
                "tool_calls": [{"name": "schedule", "args": {"CronExpression": "*/30 * * * *", "Prompt": "Check email"}}],
            },
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Schedule active", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        # Pre-seed DB with old PID 6936
        db.set_metadata("last_known_ls_pid", "6936")
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/30 * * * *",
            prompt="Check email",
            expected_interval_seconds=1800,
            last_trigger_at=time.time() - 30.0,
            ls_pid=6936,
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        mock_agentapi.get_language_server_pid.return_value = 26788
        mock_agentapi.send_message.return_value = (True, "OK", "")
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        # First scan: should detect schedule lost due to PID restart (6936 -> 26788)
        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].has_active_schedule is True
        assert "lost_schedule_after_restart" in stalled[0].last_error

        # Resuscitate session: remounts schedule and updates ls_pid to 26788 in DB
        res = asyncio.run(watchdog.resuscitate_session(stalled[0]))
        assert res["success"] is True

        # Verify DB was updated to current PID 26788
        sched = db.get_conversation_schedule(convo_id)
        assert sched["ls_pid"] == 26788

        # Second scan: should NOT treat it as lost again (prevents infinite loop!)
        stalled_after = watchdog.scan_stalled_conversations()
        assert len(stalled_after) == 0

    def test_quota_restored_pull_up_respects_circuit_breaker_max_retries(self, db, mock_agentapi, temp_dir):
        """
        Verify that when active account has healthy quota (>10%), sessions that previously
        hit quota exhaustion do NOT pull up infinitely if attempt_count >= max_retries_per_session.
        Circuit breaker must mark can_resuscitate = False and skip_reason = max_retries_exhausted.
        """
        convo_id = "test-quota-pullup-circuit-breaker"
        # Create a session that hit quota
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Execute job"},
            {
                "step_index": 1,
                "source": "SYSTEM",
                "type": "ERROR_MESSAGE",
                "status": "ERROR",
                "content": "Individual quota reached. Resets in 3600s",
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=30.0)

        # Record 3 prior attempts in DB
        for _ in range(3):
            db.record_resuscitation(
                conversation_id=convo_id,
                status="failed",
                last_error="quota_restored_pull_up",
            )

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
            conversations_dir=str(temp_dir / "conversations"),
            max_retries_per_session=3,
        )
        mock_sentinel = MagicMock()
        mock_sentinel.is_active_account_healthy.return_value = (True, "viinam33@gmail.com", {})

        watchdog = AntigravityWatchdog(
            db=db,
            config=config,
            agentapi_client=mock_agentapi,
            quota_sentinel=mock_sentinel,
        )

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        item = stalled[0]
        assert item.conversation_id == convo_id
        assert item.attempt_count == 3
        assert item.can_resuscitate is False
        assert "max_retries_exhausted (3/3)" in item.skip_reason

    def test_fake_remount_text_hallucination_rejected(self, db, mock_agentapi, temp_dir):
        """
        Adversarial Test: Agent replies with text claiming it remounted the schedule,
        but does NOT execute the schedule tool call after language_server restart.
        Watchdog MUST NOT promote ls_pid and must keep the schedule flagged as dropped.
        """
        from datetime import datetime, timezone
        convo_id = "test-fake-remount-hallucination"
        now = time.time()
        old_sched_time = now - 3600.0  # 1 hour ago
        old_sched_iso = datetime.fromtimestamp(old_sched_time, tz=timezone.utc).isoformat()

        # Step 1: Initial schedule created 1 hour ago on PID 1000
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Schedule check"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": old_sched_iso,
                "content": "Mounted cron",
                "tool_calls": [{"name": "schedule", "args": {"CronExpression": "*/30 * * * *", "Prompt": "Check email"}}],
            },
            # Step 2: Agent hallucinates text claim after restart WITHOUT calling schedule tool
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": datetime.fromtimestamp(now - 10.0, tz=timezone.utc).isoformat(),
                "content": "I have successfully rescheduled the task for every 30 minutes! All set.",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=10.0)

        db.set_metadata("last_known_ls_pid", "1000")
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/30 * * * *",
            prompt="Check email",
            expected_interval_seconds=1800,
            last_trigger_at=old_sched_time,
            ls_pid=1000,
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        mock_agentapi.get_language_server_pid.return_value = 2000
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.check_language_server_lifecycle()

        # Scan conversations: must detect schedule loss and NOT be fooled by text claim
        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].has_active_schedule is True
        assert "lost_schedule_after_restart" in stalled[0].last_error

        # Ensure ls_pid in SQLite was NOT promoted
        sched = db.get_conversation_schedule(convo_id)
        assert sched["ls_pid"] == 1000

    def test_genuine_remount_after_restart_promotes_pid(self, db, mock_agentapi, temp_dir):
        """
        Verify genuine remount: Agent executes schedule tool call after language_server
        restart. Watchdog verifies genuine tool evidence and promotes ls_pid to current PID.
        """
        from datetime import datetime, timezone
        convo_id = "test-genuine-remount-promoted"
        now = time.time()
        old_sched_time = now - 3600.0

        db.set_metadata("last_known_ls_pid", "1000")
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/30 * * * *",
            prompt="Check email",
            expected_interval_seconds=1800,
            last_trigger_at=old_sched_time,
            ls_pid=1000,
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        mock_agentapi.get_language_server_pid.return_value = 2000
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.check_language_server_lifecycle()

        # Agent executes genuine schedule tool call AFTER restart
        new_sched_iso = datetime.fromtimestamp(now + 5.0, tz=timezone.utc).isoformat()
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Schedule check"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": new_sched_iso,
                "content": "Mounted cron again",
                "tool_calls": [{"name": "schedule", "args": {"CronExpression": "*/30 * * * *", "Prompt": "Check email"}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": new_sched_iso,
                "content": "Done",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=5.0)

        # Scan should recognize genuine mount and promote ls_pid to 2000
        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0

        sched = db.get_conversation_schedule(convo_id)
        assert sched["ls_pid"] == 2000

    def test_detect_parent_from_parent_references_sqlite_table(self, temp_dir):
        """
        Verify detect_parent_conversation_id reads authoritative parent UUID
        from the parent_references table in Antigravity conversation SQLite databases.
        """
        import sqlite3
        convo_id = "test-subagent-convo-pr"
        conv_dir = temp_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        db_path = conv_dir / f"{convo_id}.db"

        parent_uuid = "5eaeebfd-5424-4261-b593-0d940cf1ee71"
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("CREATE TABLE parent_references (idx INTEGER, data BLOB)")
        # Real Antigravity protobuf structure
        blob = b"\n$" + parent_uuid.encode("ascii") + b"\x10\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01\x18\x04(\x022$" + convo_id.encode("ascii")
        cur.execute("INSERT INTO parent_references (idx, data) VALUES (0, ?)", (blob,))
        conn.commit()
        conn.close()

        detected = detect_parent_conversation_id(convo_id, conversations_dir=conv_dir)
        assert detected == parent_uuid

    def test_bidirectional_parent_deduplication(self, db, mock_agentapi, temp_dir):
        """
        Verify that when parent is encountered before child or child before parent,
        the parent is awakened exactly ONCE in that tick and child is skipped.
        """
        parent_id = "parent-uuid-dedup-001"
        sub_id = "child-uuid-dedup-002"

        # Create parent session
        parent_steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Top-level plan"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, parent_id, parent_steps, mtime_offset_seconds=50.0)

        # Create child session
        child_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f'<subagent_reminder>\ninvoked by a caller agent (name: "parent", id: "{parent_id}")\n</subagent_reminder>',
            },
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, sub_id, child_steps, mtime_offset_seconds=50.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        # Run tick
        res = asyncio.run(watchdog.resuscitate_stalled_sessions())
        # Exactly 1 message sent to parent_id
        assert len(res) == 1
        assert res[0]["target_conversation_id"] == parent_id
        mock_agentapi.send_message.assert_awaited_once()

    def test_recent_resuscitation_cooldown_blocks_rapid_spam(self, db, mock_agentapi, temp_dir):
        """
        Verify that a session that was resuscitated within stall_grace_seconds (45s)
        is placed into cooldown to prevent rapid spamming.
        """
        convo_id = "test-recent-cooldown-spam-prevent"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Execute task"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=50.0)

        # Record recent resuscitation 10 seconds ago
        db.record_resuscitation(
            conversation_id=convo_id,
            last_error="empty_planner_response_hang",
            status="resuscitated",
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir), stall_grace_seconds=45)
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].can_resuscitate is False
        assert "recent_resuscitation_cooldown" in (stalled[0].skip_reason or "")

    def test_orphaned_subagent_prevented_from_solo_pullup(self, db, mock_agentapi, temp_dir):
        """
        Verify that if a session has subagent markers but its parent UUID cannot be found,
        it is marked can_resuscitate=False with orphaned_subagent_cannot_determine_parent
        to prevent split-brain Solo mode degradation.
        """
        convo_id = "test-orphaned-subagent-001"
        steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "<subagent_reminder>\nYou are running as a subagent.\n</subagent_reminder>",
            },
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=50.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].is_subagent is True
        assert stalled[0].parent_conversation_id is None
        assert stalled[0].can_resuscitate is False
        assert stalled[0].skip_reason == "orphaned_subagent_cannot_determine_parent"

    def test_parent_waiting_on_completed_subagent_resuscitated_with_delegation_report(self, db, mock_agentapi, temp_dir):
        """
        Verify that when a parent conversation delegates to a child subagent, and the child subagent
        completes, but the parent never received the notification (e.g. dropped IPC),
        the watchdog detects parent_waiting_subagent_completed_hang and resuscitates the parent
        with PARENT_DELEGATION_COMPLETED_PROMPT containing the child's summary.
        """
        parent_id = "parent-waiting-subagent-001"
        child_id = "child-completed-worker-002"

        # Parent conversation: invoked child subagent and is waiting for completion report
        parent_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Run complex multi-agent coding task ; /goal",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "content": f'Created the following subagents:\n{{\n  "conversationId": "{child_id}"\n}}',
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": f"The task has been delegated to the **DeepCoder** subagent (`{child_id}`). I am waiting for its completion report.",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, parent_id, parent_steps, mtime_offset_seconds=60.0)

        # Child conversation: finished and reported completion
        child_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f"Subagent task instructions for {child_id}",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "DeepCoder pipeline completed successfully. All 37/37 tests passed. <!-- GOAL_COMPLETE -->",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, child_id, child_steps, mtime_offset_seconds=50.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=10,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        # 1. Scan detects parent as stalled waiting on completed subagent
        stalled = watchdog.scan_stalled_conversations()
        parent_stalled = [s for s in stalled if s.conversation_id == parent_id]
        assert len(parent_stalled) == 1
        assert parent_stalled[0].completed_child_id == child_id
        assert "parent_waiting_subagent_completed_hang" in parent_stalled[0].last_error
        assert parent_stalled[0].can_resuscitate is True

        # 2. Resuscitate parent: dispatches PARENT_DELEGATION_COMPLETED_PROMPT to parent_id
        res = asyncio.run(watchdog.resuscitate_session(parent_stalled[0]))
        assert res["status"] == "resuscitated"
        mock_agentapi.send_message.assert_called_once()
        call_kwargs = mock_agentapi.send_message.call_args[1]
        assert call_kwargs["conversation_id"] == parent_id
        assert "【系统自动拉起：子代理完成通知】" in call_kwargs["content"]
        assert child_id in call_kwargs["content"]
        assert "All 37/37 tests passed" in call_kwargs["content"]

    def test_extended_lookback_allows_boost_goal_sessions_within_24h(self, db, mock_agentapi, temp_dir):
        """
        Verify that sessions with /boost or /goal are not dropped by the 60m lookback window
        if their transcript was updated within 24h (e.g. overnight 8-hour sessions).
        """
        convo_id = "overnight-goal-session-001"
        steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Run overnight long batch ; /goal",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "",
                "tool_calls": [],
            },
        ]
        # Set mtime to 3 hours ago (10800s > 3600s lookback_seconds)
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=10800.0)

        # Default lookback is 60 minutes
        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            lookback_minutes=60,
            stall_grace_seconds=10,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        assert stalled[0].conversation_id == convo_id
        assert stalled[0].is_boost_goal is True

    def test_boost_active_subagents_protect_parent_from_premature_interruption(self, db, mock_agentapi, temp_dir):
        """
        Critical Boost Protection:
        When a parent conversation is in /boost mode and delegates to a child subagent,
        the parent legitimately stops and yields control waiting for the subagent to finish and report.
        If the child subagent is still active (e.g. running Playwright/bash/editing code),
        the watchdog MUST NOT interrupt or wake up the parent, even if the parent has been idle for a long time.
        """
        parent_id = "parent-boost-orchestrator-001"
        child_id = "child-active-worker-002"

        parent_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Perform deep research and automation ; /boost",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "content": f'Created the following subagents:\n{{\n  "conversationId": "{child_id}"\n}}',
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": f"I have spawned the worker (`{child_id}`) to begin working. Since this subagent runs asynchronously in the background, I will now yield control and wait for it to report back.",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, parent_id, parent_steps, mtime_offset_seconds=400.0)

        child_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Subagent instructions",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Analyzing files...",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "python3 script.py"}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "RUNNING",
                "content": "Running background test...",
            },
        ]
        _create_fake_session(temp_dir, child_id, child_steps, mtime_offset_seconds=10.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=30,
            boost_quiet_seconds=900,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()

        parent_stalled = [s for s in stalled if s.conversation_id == parent_id]
        assert len(parent_stalled) == 0, f"Parent was erroneously flagged as stalled: {parent_stalled}"

        child_stalled = [s for s in stalled if s.conversation_id == child_id]
        assert len(child_stalled) == 0, f"Child was erroneously flagged as stalled: {child_stalled}"

    def test_boost_intermediate_planner_response_does_not_falsely_complete(self, db, mock_agentapi, temp_dir):
        """
        Verify that an intermediate PLANNER_RESPONSE (e.g. step 1 of 200) in a child subagent
        is NEVER falsely treated as a completed subagent that pulls up the waiting parent.
        """
        parent_id = "parent-boost-orchestrator-002"
        child_id = "child-midway-worker-003"

        parent_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Do task ; /boost",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "content": f'{{"conversationId": "{child_id}"}}',
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": f"Delegated to `{child_id}`, yielding control.",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, parent_id, parent_steps, mtime_offset_seconds=600.0)

        child_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "Run Playwright tasks",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Navigating to Google Ads dashboard...",
                "tool_calls": [{"name": "click", "args": {}}],
            },
        ]
        _create_fake_session(temp_dir, child_id, child_steps, mtime_offset_seconds=120.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=30,
            boost_quiet_seconds=900,
            conversations_dir=str(temp_dir / "conversations"),
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        parent_stalled = [s for s in stalled if s.conversation_id == parent_id]
        assert len(parent_stalled) == 0, "Parent was prematurely flagged as stalled while child was still working!"

    def test_inspect_conversation_db_for_terminal_network_error(self, temp_dir):
        """
        Verify that inspect_conversation_db_for_terminal_network_error accurately detects
        step_type = 17 network issues (e.g. There was a network issue connecting to the server).
        """
        import sqlite3
        convo_id = "test-network-terminal-error-convo"
        conv_dir = temp_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        db_path = conv_dir / f"{convo_id}.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
        # Insert normal step
        conn.execute("INSERT INTO steps VALUES (1, 1, ?)", (b"normal step",))
        # Insert terminal error step (step_type=17) with network issue text
        err_msg = b"prefix... Error Unknown: There was a network issue connecting to the server, please try again. ...suffix"
        conn.execute("INSERT INTO steps VALUES (2, 17, ?)", (err_msg,))
        conn.commit()
        conn.close()

        res = inspect_conversation_db_for_terminal_network_error(convo_id, conversations_dir=conv_dir)
        assert res is not None
        assert res["step_index"] == 2
        assert res["error"] == "network_issue_server_error"
        assert "network issue" in res["details"].lower()

    def test_get_resuscitation_attempts_resets_on_forward_step_progress(self, db):
        """
        Verify that DatabaseManager.get_resuscitation_attempts resets the consecutive
        resuscitation count to 0 when the conversation makes forward step progress.
        """
        convo_id = "test-forward-progress-reset"
        # Record 3 resuscitation attempts at step 174
        db.record_resuscitation(convo_id, last_step_index=174)
        db.record_resuscitation(convo_id, last_step_index=174)
        db.record_resuscitation(convo_id, last_step_index=174)

        # Same step index: returns 3
        assert db.get_resuscitation_attempts(convo_id, current_step_index=174) == 3

        # Forward progress (step 195): consecutive attempts reset to 0!
        assert db.get_resuscitation_attempts(convo_id, current_step_index=195) == 0

    def test_watchdog_resets_attempts_after_backoff_cooldown_expires(self, db, mock_agentapi, temp_dir):
        """
        Verify that after backoff_cooldown_seconds expires (e.g. 1800s),
        the session is NOT permanently locked out and can be resuscitated again.
        """
        convo_id = "test-backoff-cooldown-expiry"
        # Seed 3 attempts at step 174
        for _ in range(3):
            db.record_resuscitation(
                convo_id,
                last_step_index=174,
            )

        # Set the resuscitated_at timestamp to 2000s in the past (longer than 1800s cooldown)
        old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 2000.0))
        with db._lock:
            db._conn.execute(
                "UPDATE antigravity_resuscitations SET resuscitated_at = ? WHERE conversation_id = ?",
                (old_ts, convo_id),
            )
            db._conn.commit()

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Keep running"},
            {"step_index": 174, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "ERROR", "content": "There was a network issue connecting to the server", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=120.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=30,
            backoff_cooldown_seconds=1800,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        target = next((s for s in stalled if s.conversation_id == convo_id), None)
        assert target is not None
        # Should NOT be blocked by max_retries_exhausted!
        assert target.can_resuscitate is True
        assert "max_retries_exhausted" not in (target.skip_reason or "")

    def test_resuscitate_session_prioritizes_network_error_prompt(self, db, mock_agentapi, temp_dir):
        """
        Verify that resuscitate_session uses NETWORK_ERROR_RESUSCITATION_PROMPT
        when is_network_issue is True.
        """
        convo_id = "test-network-prompt-selection"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Run long task"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "ERROR", "content": "Network timeout", "tool_calls": []},
        ]
        t_path = _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=120.0)

        session = StalledSessionInfo(
            conversation_id=convo_id,
            transcript_path=t_path,
            last_step_index=1,
            last_error="There was a network issue connecting to the server",
            last_error_time=time.time() - 120.0,
            is_subagent=False,
            is_network_issue=True,
            can_resuscitate=True,
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        mock_agentapi.send_message.return_value = (True, "OK", "")

        res = asyncio.run(watchdog.resuscitate_session(session))
        assert res["status"] == "resuscitated"

        mock_agentapi.send_message.assert_called_once()
        called_prompt = mock_agentapi.send_message.call_args[1]["content"]
        assert "【系统自动网络/服务故障自愈拉起提醒】" in called_prompt

    def test_inspect_conversation_db_for_agent_execution_terminated_and_location(self, temp_dir):
        """
        Verify that inspect_conversation_db_for_terminal_network_error accurately detects
        'Agent execution terminated due to error' and 'User location is not supported',
        extracting the exact error ID.
        """
        import sqlite3
        convo_id = "test-agent-execution-location-error"
        conv_dir = temp_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        db_path = conv_dir / f"{convo_id}.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
        # Insert incident payload
        payload = (
            b"(Agent execution terminated due to error. "
            b"OFAILED_PRECONDITION (code 400): User location is not supported for the API use. "
            b"TraceID: 0xc895b8d75a1080cc 2'6aa506b4-cbf0-4cdd-86af-5086a23c541d-278 JOFAILED_PRECONDITION"
        )
        conn.execute("INSERT INTO steps VALUES (27, 17, ?)", (payload,))
        conn.commit()
        conn.close()

        res = inspect_conversation_db_for_terminal_network_error(convo_id, conversations_dir=conv_dir)
        assert res is not None
        assert res["step_index"] == 27
        assert res["error"] == "network_issue_server_error"
        assert res["error_id"] == "6aa506b4-cbf0-4cdd-86af-5086a23c541d-27"

    def test_terminal_db_error_bypasses_stall_grace_seconds(self, db, temp_dir):
        """
        Verify that a conversation with terminal execution error recorded in SQLite DB
        bypasses the 180s stall grace period and is immediately marked can_resuscitate=True.
        """
        import sqlite3
        convo_id = "test-terminal-grace-bypass"
        conv_dir = temp_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        db_path = conv_dir / f"{convo_id}.db"

        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, step_payload BLOB)")
        payload = b"Agent execution terminated due to error. User location is not supported for the API use. 6aa506b4-cbf0-4cdd-86af-5086a23c541d-27"
        conn.execute("INSERT INTO steps VALUES (27, 17, ?)", (payload,))
        conn.commit()
        conn.close()

        # Transcript only up to step 26 or user prompt
        steps = [
            {"step_index": 25, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "Done task"},
            {"step_index": 26, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": None, "tool_calls": []},
        ]
        # Modified only 10 seconds ago (far less than stall_grace_seconds = 180s)
        t_path = _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=10.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            stall_grace_seconds=180,
        )
        watchdog = AntigravityWatchdog(db=db, config=config)
        watchdog._conversations_dir = conv_dir

        stalled = watchdog.scan_stalled_conversations()
        matched = [s for s in stalled if s.conversation_id == convo_id]
        assert len(matched) == 1
        assert matched[0].can_resuscitate is True
        assert matched[0].skip_reason is None
        assert matched[0].is_network_issue is True
        assert matched[0].last_error == "network_issue_server_error"

    def test_boost_goal_prioritized_over_network_prompt(self, db, mock_agentapi, temp_dir):
        """
        Verify that a /goal or /boost session that hits a network/location error
        retains the BOOST_GOAL_RESUSCITATION_PROMPT to enforce team coordination discipline.
        """
        convo_id = "test-boost-goal-network-priority"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "[/goal] Autonomous Audit Task"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "ERROR", "content": "Agent execution terminated due to error", "tool_calls": []},
        ]
        t_path = _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=120.0)

        session = StalledSessionInfo(
            conversation_id=convo_id,
            transcript_path=t_path,
            last_step_index=1,
            last_error="network_issue_server_error",
            last_error_time=time.time() - 120.0,
            is_subagent=False,
            is_boost_goal=True,
            is_network_issue=True,
            can_resuscitate=True,
        )

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        mock_agentapi.send_message.return_value = (True, "OK", "")

        res = asyncio.run(watchdog.resuscitate_session(session))
        assert res["status"] == "resuscitated"

        mock_agentapi.send_message.assert_called_once()
        called_prompt = mock_agentapi.send_message.call_args[1]["content"]
        assert "【系统自动自愈拉起提醒：/boost 目标自治与协同推进延续】" in called_prompt
        assert "严禁退化为单兵等待或打假卡" in called_prompt

    def test_goal_complete_archives_conversation_schedule_and_prevents_resuscitation(self, db, temp_dir):
        """
        Verify that when a session contains <!-- GOAL_COMPLETE -->,
        scan_stalled_conversations automatically archives any active schedule in DB
        and skips resuscitation completely.
        """
        convo_id = "test-goal-complete-session"
        db.save_conversation_schedule(
            conversation_id=convo_id,
            cron_expression="*/10 * * * *",
            prompt="Heartbeat",
            expected_interval_seconds=600,
            last_trigger_at=time.time() - 3600,
            ls_pid=99999,
        )
        assert len(db.list_active_conversation_schedules()) == 1

        steps = [
            {"step_index": 1, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "Run goal"},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "<!-- GOAL_COMPLETE -->\nTask finished!", "tool_calls": []},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=500.0)

        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config)

        stalled = watchdog.scan_stalled_conversations()
        # Session with GOAL_COMPLETE should be completely skipped
        assert not any(s.conversation_id == convo_id for s in stalled)

        # Active conversation schedule must have been marked as completed
        active_scheds = db.list_active_conversation_schedules()
        assert not any(s["conversation_id"] == convo_id for s in active_scheds)

    def test_get_language_server_start_time(self):
        """Verify get_language_server_start_time parses ps lstart output correctly."""
        assert get_language_server_start_time(None) is None
        assert get_language_server_start_time(0) is None

        with patch("subprocess.check_output", return_value="Thu Sep 24 09:28:14 2026\n"):
            t = get_language_server_start_time(12345)
            assert t is not None
            expected = time.mktime(time.strptime("Thu Sep 24 09:28:14 2026", "%a %b %d %H:%M:%S %Y"))
            assert t == expected

        with patch("subprocess.check_output", side_effect=Exception("process not found")):
            assert get_language_server_start_time(99999) is None

    def test_get_unfinished_conversations_from_summaries(self, temp_dir):
        """Verify querying conversation_summaries.db extracts running and not_fully_idle sessions."""
        import sqlite3
        db_path = temp_dir / "conversation_summaries.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                not_fully_idle INTEGER,
                killed INTEGER,
                parent_conversation_id TEXT,
                last_modified_time TEXT,
                step_count INTEGER
            )
            """
        )
        cur.executemany(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("c-running", "Running Task", "CASCADE_RUN_STATUS_RUNNING", 1, 0, None, "2026-09-24 01:28:14", 45),
                ("c-not-idle", "Subagent Task", "CASCADE_RUN_STATUS_IDLE", 1, 0, "parent-1", "2026-09-24 01:25:00", 12),
                ("c-killed", "Killed Task", "CASCADE_RUN_STATUS_RUNNING", 1, 1, None, "2026-09-24 01:20:00", 5),
                ("c-clean-idle", "Finished Task", "CASCADE_RUN_STATUS_IDLE", 0, 0, None, "2026-09-24 01:10:00", 30),
            ],
        )
        conn.commit()
        conn.close()

        res = get_unfinished_conversations_from_summaries(db_path)
        assert "c-running" in res
        assert res["c-running"]["title"] == "Running Task"
        assert res["c-running"]["parent_conversation_id"] is None

        assert "c-not-idle" in res
        assert res["c-not-idle"]["parent_conversation_id"] == "parent-1"

        # Killed and clean idle sessions must NOT be returned
        assert "c-killed" not in res
        assert "c-clean-idle" not in res

    def test_is_subagent_active_with_server_restart(self, temp_dir):
        """
        Verify that subagents predating language_server restart are recognized as stopped,
        while subagents modified on the current server process are recognized as active.
        """
        subagent_id = "test-subagent-lifecycle-001"
        now = time.time()
        restart_time = now - 300.0  # Server restarted 5 minutes ago

        # 1. Subagent last modified 10m ago (before restart) -> stopped by server restart
        steps_running = [
            {"step_index": 1, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "work"},
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "RUNNING", "content": "executing"},
        ]
        _create_fake_session(temp_dir, subagent_id, steps_running, mtime_offset_seconds=600.0)
        is_act, reason = is_subagent_active(subagent_id, temp_dir, now=now, ls_restart_time=restart_time)
        assert is_act is False
        assert "stopped_by_server_restart" in reason

        # 2. Subagent modified 60s ago (after restart) with RUNNING status -> active
        _create_fake_session(temp_dir, subagent_id, steps_running, mtime_offset_seconds=60.0)
        is_act2, reason2 = is_subagent_active(subagent_id, temp_dir, now=now, ls_restart_time=restart_time)
        assert is_act2 is True
        assert "subagent_step_running" in reason2

        # 3. Subagent modified 60s ago with GOAL_COMPLETE -> inactive
        steps_complete = [
            {"step_index": 1, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "work"},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "<!-- GOAL_COMPLETE --> finished"},
        ]
        _create_fake_session(temp_dir, subagent_id, steps_complete, mtime_offset_seconds=60.0)
        is_act3, reason3 = is_subagent_active(subagent_id, temp_dir, now=now, ls_restart_time=restart_time)
        assert is_act3 is False
        assert reason3 == "subagent_goal_completed"

    def test_auto_pullup_unfinished_conversation_after_restart(self, db, mock_agentapi, temp_dir):
        """
        Verify that a conversation left in RUNNING status when Antigravity restarted
        is automatically detected and resuscitated with SERVER_RESTART_RESUSCITATION_PROMPT.
        """
        convo_id = "test-convo-restart-vpn-001"
        now = time.time()
        restart_time = now - 180.0  # Restarted 3 mins ago

        # Fake transcript that was in RUNNING status 10 mins ago (prior to restart)
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "vpn fix"},
            {"step_index": 48, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Checking vpn"},
            {"step_index": 49, "source": "MODEL", "type": "GENERIC", "status": "RUNNING", "content": "Connecting..."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=600.0)

        # Create conversation_summaries.db
        import sqlite3
        db_path = temp_dir / "conversation_summaries.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                not_fully_idle INTEGER,
                killed INTEGER,
                parent_conversation_id TEXT,
                last_modified_time TEXT,
                step_count INTEGER
            )
            """
        )
        cur.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (convo_id, "vpn fix", "CASCADE_RUN_STATUS_RUNNING", 1, 0, None, "2026-09-24 01:28:14", 49),
        )
        conn.commit()
        conn.close()

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            summaries_db_path=str(db_path),
            stall_grace_seconds=90,
            lookback_minutes=60,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        s = stalled[0]
        assert s.conversation_id == convo_id
        assert s.is_server_restart is True
        assert s.can_resuscitate is True
        assert s.last_error == "server_restart_aborted_running_step"

        # Resuscitate
        res = asyncio.run(watchdog.resuscitate_session(s))
        assert res["success"] is True
        assert res["status"] == "resuscitated"

        mock_agentapi.send_message.assert_called_once()
        called_prompt = mock_agentapi.send_message.call_args[1]["content"]
        assert "【系统自动自愈拉起：服务重启任务恢复】" in called_prompt
        assert "检测到 Antigravity 应用/服务此前已重启" in called_prompt

    def test_boost_mode_preserved_on_server_restart(self, db, mock_agentapi, temp_dir):
        """
        Verify that in /boost or teamwork mode after server restart:
        1. Dead subagent from before restart does not freeze the parent forever.
        2. Parent is resuscitated with BOOST_SERVER_RESTART_RESUSCITATION_PROMPT.
        3. Solo mode degradation is strictly prohibited.
        """
        parent_id = "11111111-2222-3333-4444-555555555555"
        child_id = "66666666-7777-8888-9999-000000000000"
        now = time.time()
        restart_time = now - 120.0  # Restarted 2 mins ago

        # Parent transcript references child_id and contains /boost
        parent_steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "/boost Refactor module"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": f'Invoked subagent {{"conversationId": "{child_id}"}}',
                "tool_calls": [{"name": "invoke_subagent"}],
            },
        ]
        _create_fake_session(temp_dir, parent_id, parent_steps, mtime_offset_seconds=500.0)

        # Child transcript last modified before restart (500s ago)
        child_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f'<subagent_reminder>\ninvoked by a caller agent (name: "parent", id: "{parent_id}")\n</subagent_reminder>',
            },
            {"step_index": 1, "source": "MODEL", "type": "GENERIC", "status": "RUNNING", "content": "editing file"},
        ]
        _create_fake_session(temp_dir, child_id, child_steps, mtime_offset_seconds=500.0)

        # Create conversation_summaries.db
        import sqlite3
        db_path = temp_dir / "conversation_summaries.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                not_fully_idle INTEGER,
                killed INTEGER,
                parent_conversation_id TEXT,
                last_modified_time TEXT,
                step_count INTEGER
            )
            """
        )
        cur.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (parent_id, "Boost Refactor", "CASCADE_RUN_STATUS_RUNNING", 1, 0, None, "2026-09-24 01:28:14", 2),
        )
        cur.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (child_id, "Subagent Refactor", "CASCADE_RUN_STATUS_RUNNING", 1, 0, parent_id, "2026-09-24 01:28:14", 2),
        )
        conn.commit()
        conn.close()

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            summaries_db_path=str(db_path),
            stall_grace_seconds=90,
            lookback_minutes=60,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) >= 1

        # Locate the parent session
        parent_items = [s for s in stalled if s.conversation_id == parent_id]
        assert len(parent_items) == 1

        target_item = parent_items[0]
        assert target_item.is_server_restart is True
        assert target_item.is_boost_goal is True

        res = asyncio.run(watchdog.resuscitate_session(target_item))
        assert res["success"] is True

        mock_agentapi.send_message.assert_called_once()
        called_prompt = mock_agentapi.send_message.call_args[1]["content"]
        assert "【系统自动自愈拉起：/boost 服务重启延续】" in called_prompt
        assert "严禁降级 Solo 模式" in called_prompt
        assert "请检查此前委派的子 Agent 执行状态" in called_prompt

    def test_retry_counter_and_debounce_reset_across_restarts(self, db, mock_agentapi, temp_dir):
        """
        Verify that failed attempts that occurred BEFORE language_server restart
        do not count against the retry limit on the new server instance.
        """
        convo_id = "test-session-prior-failures"
        now = time.time()
        restart_time = now - 60.0  # Restarted 1 min ago

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "analyze data"},
            {"step_index": 10, "source": "SYSTEM", "type": "ERROR_MESSAGE", "status": "ERROR", "content": "Connection reset by peer"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=1200.0)

        # Record 3 failed attempts in DB occurring 20 minutes ago (before restart)
        old_epoch = now - 1200.0
        for i in range(3):
            db.record_resuscitation(
                conversation_id=convo_id,
                last_error="Connection reset by peer",
                status="failed",
                last_step_index=10,
            )
        # Update last resuscitation to old epoch
        last_res = db.get_last_resuscitation(convo_id)
        assert last_res is not None
        db.update_resuscitation_status(last_res["resuscitation_id"], "failed", cooldown_until=old_epoch)
        old_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(old_epoch))
        with db._lock:
            db._conn.execute(
                "UPDATE antigravity_resuscitations SET resuscitated_at = ? WHERE resuscitation_id = ?",
                (old_ts, last_res["resuscitation_id"]),
            )
            db._conn.commit()

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=30,
            max_retries_per_session=3,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        s = stalled[0]
        # Should be eligible for pull-up on the new server instance despite 3 past attempts
        assert s.can_resuscitate is True
        assert s.attempt_count == 0

    def test_get_status_includes_restart_metadata(self, db, mock_agentapi, temp_dir):
        """Verify get_status output includes ls_restart_time and is_server_restart flag."""
        config = AntigravityWatchdogConfig(brain_dir=str(temp_dir))
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = 1790213359.0

        status = watchdog.get_status(force=True)
        assert "ls_restart_time" in status
        assert status["ls_restart_time"] == 1790213359.0

    def test_unanswered_user_prompt_resuscitated_after_restart(self, db, mock_agentapi, temp_dir):
        """
        Verify that a normal conversation where the user entered a prompt right
        before restart (last_source == USER_EXPLICIT) is detected as stalled and
        resuscitated with SERVER_RESTART_RESUSCITATION_PROMPT so user typing is not needed.
        """
        convo_id = "test-unanswered-user-prompt-restart"
        now = time.time()
        restart_time = now - 100.0  # Restarted 100s ago

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "vpn fix"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "I am checking VPN configuration..."},
            {"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "please scan patterns and audit sidecard if need"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=300.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
            lookback_minutes=60,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        s = stalled[0]
        assert s.conversation_id == convo_id
        assert s.is_server_restart is True
        assert s.can_resuscitate is True
        assert s.last_error == "server_restart_unanswered_user_prompt"

        res = asyncio.run(watchdog.resuscitate_session(s))
        assert res["success"] is True
        mock_agentapi.send_message.assert_called_once()
        prompt = mock_agentapi.send_message.call_args[1]["content"]
        assert "【系统自动自愈拉起：服务重启任务恢复】" in prompt

    def test_subagent_from_before_restart_does_not_disturb_active_parent(self, db, mock_agentapi, temp_dir):
        """
        Verify that if a subagent died before restart, but its parent is ALREADY
        active on the current server process, the watchdog does NOT send a resuscitation
        prompt to the parent, preventing corruption of active teamwork.
        """
        parent_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        child_id = "ffffffff-1111-2222-3333-444444444444"
        now = time.time()
        restart_time = now - 200.0  # Restarted 200s ago

        # Parent was active 20 seconds ago on the CURRENT server instance!
        parent_steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "/boost Complex task"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": f'Invoked subagent {{"conversationId": "{child_id}"}}',
                "tool_calls": [{"name": "invoke_subagent"}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "Working on current server task...",
                "tool_calls": [],
            },
        ]
        _create_fake_session(temp_dir, parent_id, parent_steps, mtime_offset_seconds=20.0)

        # Child was last modified 400s ago (before restart)
        child_steps = [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": f'<subagent_reminder>\ninvoked by a caller agent (name: "parent", id: "{parent_id}")\n</subagent_reminder>',
            },
            {"step_index": 1, "source": "MODEL", "type": "GENERIC", "status": "RUNNING", "content": "old task"},
        ]
        _create_fake_session(temp_dir, child_id, child_steps, mtime_offset_seconds=400.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
            lookback_minutes=60,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        # Child session should be detected as stalled, but can_resuscitate MUST be False
        child_items = [s for s in stalled if s.conversation_id == child_id]
        assert len(child_items) == 1
        assert child_items[0].can_resuscitate is False
        assert child_items[0].skip_reason == "parent_already_active_on_current_server"

    def test_actively_running_step_on_current_server_not_killed_after_90s(self, db, mock_agentapi, temp_dir):
        """
        Verify that a long-running step (e.g. tests running for 120s) on the CURRENT server
        is NOT falsely classified as running_step_hung after stall_grace_seconds (90s).
        """
        convo_id = "test-active-long-running-step"
        now = time.time()
        restart_time = now - 3600.0  # Restarted 1 hour ago

        # Step started 120s ago on CURRENT server (after restart)
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "run tests"},
            {"step_index": 1, "source": "MODEL", "type": "GENERIC", "status": "RUNNING", "content": "pytest tests/unit/"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=120.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
            boost_quiet_seconds=900,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        # Should NOT be in stalled list because it is legitimately executing on the current server
        assert len(stalled) == 0

    def test_dangling_tool_result_resuscitated_after_restart(self, db, mock_agentapi, temp_dir):
        """
        Verify that a conversation where a tool finished right before restart
        (last_source == MODEL, type == GENERIC, status == DONE) is detected as
        server_restart_aborted_tool_followup and immediately resuscitated.
        """
        convo_id = "test-dangling-tool-restart"
        now = time.time()
        restart_time = now - 150.0

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "diagnose proxy"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "checking...", "tool_calls": [{"name": "run_command"}]},
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "Command finished with return code 0."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=300.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        s = stalled[0]
        assert s.conversation_id == convo_id
        assert s.is_server_restart is True
        assert s.can_resuscitate is True
        assert s.last_error == "server_restart_aborted_tool_followup"

    def test_boost_session_done_waiting_resuscitated_even_without_summaries_db(self, db, mock_agentapi, temp_dir):
        """
        Verify that a /boost session that was waiting for subagents before restart
        is resuscitated with BOOST_SERVER_RESTART_RESUSCITATION_PROMPT even if
        conversation_summaries.db does not contain an active entry.
        """
        convo_id = "test-boost-session-without-summaries-entry"
        now = time.time()
        restart_time = now - 100.0

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "/boost Implement feature"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Waiting for delegation..."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=300.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 1
        s = stalled[0]
        assert s.conversation_id == convo_id
        assert s.is_server_restart is True
        assert s.is_boost_goal is True
        assert s.can_resuscitate is True

        res = asyncio.run(watchdog.resuscitate_session(s))
        assert res["success"] is True
        prompt = mock_agentapi.send_message.call_args[1]["content"]
        assert "【系统自动自愈拉起：/boost 服务重启延续】" in prompt
        assert "严禁降级 Solo 模式" in prompt

    def test_sentinel_all_green_completion_not_resuscitated_on_server_restart(self, db, mock_agentapi, temp_dir):
        """
        Verify that a Sentinel or health-check session that claimed completion
        with '🟢 Antigravity Webhook Hub 哨兵巡检快照 (All Green)' is never flagged as stalled
        or resuscitated on server restart.
        """
        convo_id = "test-sentinel-all-green-session"
        now = time.time()
        restart_time = now - 100.0

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Antigravity Webhook Hub Sentinel health check"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "### 🟢 Antigravity Webhook Hub 哨兵巡检快照 (All Green)\n13/13 Checks Passed."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=300.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0, f"Expected 0 stalled sessions, got: {stalled}"

    def test_post_restart_all_green_not_repeatedly_resuscitated(self, db, mock_agentapi, temp_dir):
        """
        Verify that a session that previously recovered post-restart and posted
        '🟢 服务自愈与哨兵巡检汇报 (Post-Restart All Green)' is recognized as completed
        and not repeatedly resuscitated.
        """
        convo_id = "test-post-restart-all-green-session"
        now = time.time()
        restart_time = now - 500.0

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "/boost Sentinel"},
            {"step_index": 1, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "【系统自动自愈拉起：/boost 服务重启延续】"},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "### 🟢 服务自愈与哨兵巡检汇报 (Post-Restart All Green)\n所有哨兵组件运行正常。"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=50.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=45,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0

    def test_once_per_restart_gate_blocks_duplicate_pullup_for_same_restart(self, db, mock_agentapi, temp_dir):
        """
        Verify that a genuine unfinished session before restart is resuscitated ONCE,
        and subsequent scans/calls for the same server restart are strictly blocked.
        """
        convo_id = "test-once-per-restart-session"
        now = time.time()
        restart_time = now - 100.0

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "/boost Run migration"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Waiting for worker..."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=300.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)
        watchdog.ls_restart_time = restart_time

        # 1. First scan should detect the session as eligible
        stalled1 = watchdog.scan_stalled_conversations()
        assert len(stalled1) == 1
        assert stalled1[0].can_resuscitate is True

        # 2. Resuscitate once
        res1 = asyncio.run(watchdog.resuscitate_session(stalled1[0]))
        assert res1["success"] is True

        # 3. Second scan for the same server restart MUST mark can_resuscitate = False
        stalled2 = watchdog.scan_stalled_conversations()
        assert len(stalled2) == 1
        assert stalled2[0].can_resuscitate is False
        assert stalled2[0].skip_reason == "already_resuscitated_for_current_server_restart"

        # 4. Direct call to resuscitate_session must also be rejected by defense-in-depth preflight
        res2 = asyncio.run(watchdog.resuscitate_session(stalled2[0]))
        assert res2["success"] is False
        assert res2["error"] == "already_resuscitated_for_current_server_restart"

    def test_in_flight_mutex_blocks_concurrent_resuscitation(self, db, mock_agentapi, temp_dir):
        """
        Verify that concurrent calls to resuscitate_session for the same session
        are blocked by the in-flight mutex.
        """
        convo_id = "test-in-flight-mutex-session"
        now = time.time()

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Do task"},
            {"step_index": 1, "source": "MODEL", "type": "ERROR", "status": "ERROR", "content": "network error"},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=200.0)

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            stall_grace_seconds=90,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        # Artificially acquire in-flight lock
        watchdog._in_flight_resuscitations.add(convo_id)

        info = StalledSessionInfo(
            conversation_id=convo_id,
            transcript_path=temp_dir / convo_id / "transcript.jsonl",
            last_step_index=1,
            last_error="network_error",
            last_error_time=now - 200.0,
            is_subagent=False,
            can_resuscitate=True,
        )

        res = asyncio.run(watchdog.resuscitate_session(info))
        assert res["success"] is False
        assert res["status"] == "in_flight_skip"
        assert res["error"] == "resuscitation_already_in_flight"

    def test_stale_conversation_summaries_ignored_when_transcript_is_cleanly_done(self, db, mock_agentapi, temp_dir):
        """
        Verify that stale entries in conversation_summaries.db (e.g. not_fully_idle=1 or CASCADE_RUN_STATUS_RUNNING)
        do NOT falsely flag a cleanly completed MODEL PLANNER_RESPONSE DONE turn as stalled.
        """
        convo_id = "test-stale-summaries-clean-turn"
        now = time.time()

        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Analyze logs"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Analysis completed. Here are the findings."},
        ]
        _create_fake_session(temp_dir, convo_id, steps, mtime_offset_seconds=120.0)

        # Create conversation_summaries.db with stale running entry
        import sqlite3
        db_path = temp_dir / "conversation_summaries.db"
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                conversation_id TEXT PRIMARY KEY,
                title TEXT,
                status TEXT,
                not_fully_idle INTEGER,
                killed INTEGER,
                parent_conversation_id TEXT,
                last_modified_time TEXT,
                step_count INTEGER
            )
            """
        )
        cur.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (convo_id, "Analyze logs", "CASCADE_RUN_STATUS_RUNNING", 1, 0, None, "2026-09-24 01:28:14", 2),
        )
        conn.commit()
        conn.close()

        config = AntigravityWatchdogConfig(
            brain_dir=str(temp_dir),
            conversations_dir=str(temp_dir),
            summaries_db_path=str(db_path),
            stall_grace_seconds=45,
        )
        watchdog = AntigravityWatchdog(db=db, config=config, agentapi_client=mock_agentapi)

        stalled = watchdog.scan_stalled_conversations()
        assert len(stalled) == 0, f"Expected 0 stalled, got: {stalled}"

    def test_watchdog_prompts_do_not_contaminate_boost_detection(self, temp_dir):
        """
        Verify that a non-boost session which received a prior watchdog prompt
        containing '【系统自动自愈拉起：/boost 服务重启延续】' is NOT falsely categorized as a boost session.
        """
        convo_id = "test-no-boost-contamination"
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "content": "Regular non-boost question"},
            {"step_index": 1, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "【系统自动自愈拉起：/boost 服务重启延续】\n检测到服务重启..."},
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Regular answer"},
        ]
        tpath = _create_fake_session(temp_dir, convo_id, steps)
        is_boost = check_session_has_boost_or_goal(tpath, steps)
        assert is_boost is False, "Watchdog prompt contaminated session into boost detection!"












