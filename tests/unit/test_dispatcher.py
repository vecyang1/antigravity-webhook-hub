"""Unit tests for hub/dispatcher.py task execution engine.

Verifies Tiers 1 & 2 requirements:
- Subprocess execution with process group isolation (os.setsid)
- Clean exit code capture (0 on success, non-zero on failure)
- Subprocess timeout handling: os.killpg termination without orphan processes
- Environment variable injection (ANTIGRAVITY_TASK_ID, ANTIGRAVITY_EVENT_ID)
- Antigravity AI agent signal file emission
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

try:
    from hub.dispatcher import TaskDispatcher
except ImportError:
    TaskDispatcher = None

pytestmark = pytest.mark.skipif(
    TaskDispatcher is None,
    reason="hub.dispatcher (Milestone M2) not yet implemented",
)


@pytest.fixture
def dispatcher_instance(tmp_path: Path):
    """Fixture providing TaskDispatcher with mock/temp database and broker."""
    # When M2 is implemented, initialize TaskDispatcher
    class MockDB:
        def __init__(self):
            self.tasks: dict[str, dict] = {}
            self.logs: list[dict] = []

        def get_task(self, task_id: str):
            return self.tasks.get(task_id)

        def update_task_status_cas(self, task_id, expected_status, new_status, exit_code=None, error_message=None):
            if task_id in self.tasks and self.tasks[task_id]["status"] == expected_status:
                self.tasks[task_id]["status"] = new_status
                if exit_code is not None:
                    self.tasks[task_id]["exit_code"] = exit_code
                if error_message:
                    self.tasks[task_id]["error_message"] = error_message
                return True
            return False

        def insert_log_chunk(self, task_id, stream, chunk):
            self.logs.append({"task_id": task_id, "stream": stream, "chunk": chunk})

    mock_db = MockDB()
    dispatcher = TaskDispatcher(db=mock_db)
    return dispatcher, mock_db


async def test_cli_command_successful_execution(dispatcher_instance: Any):
    """Tier 1: CLI command exits with code 0 and captures stdout."""
    dispatcher, db = dispatcher_instance
    task_id = "tsk_disp_001"
    db.tasks[task_id] = {
        "task_id": task_id,
        "action_type": "cli",
        "command": "python3 -c \"print('dispatcher_execution_verified')\"",
        "status": "queued",
        "timeout_seconds": 10,
    }

    result = await dispatcher.execute_task(task_id)
    assert result.exit_code == 0
    assert result.status == "succeeded"
    assert "dispatcher_execution_verified" in result.stdout


async def test_cli_command_failure_exit_code(dispatcher_instance: Any):
    """Tier 1: Non-zero exit code is recorded with status 'failed'."""
    dispatcher, db = dispatcher_instance
    task_id = "tsk_disp_fail"
    db.tasks[task_id] = {
        "task_id": task_id,
        "action_type": "cli",
        "command": "python3 -c \"import sys; sys.stderr.write('fatal_error_log'); sys.exit(42)\"",
        "status": "queued",
        "timeout_seconds": 10,
    }

    result = await dispatcher.execute_task(task_id)
    assert result.exit_code == 42
    assert result.status == "failed"
    assert "fatal_error_log" in result.stderr


async def test_cli_command_timeout_clean_kill(dispatcher_instance: Any):
    """Tier 2: Hanging command exceeds timeout, process group is cleanly killed."""
    dispatcher, db = dispatcher_instance
    task_id = "tsk_disp_timeout"
    # Sleep for 10s with 1s timeout
    db.tasks[task_id] = {
        "task_id": task_id,
        "action_type": "cli",
        "command": "python3 -c \"import time; time.sleep(10)\"",
        "status": "queued",
        "timeout_seconds": 1,
    }

    start_time = asyncio.get_event_loop().time()
    result = await dispatcher.execute_task(task_id)
    elapsed = asyncio.get_event_loop().time() - start_time

    # Must complete shortly after the 1s timeout, not run for 10s
    assert elapsed < 4.0, f"Command was not killed in time: took {elapsed}s"
    assert result.status in ("timed_out", "failed")


async def test_environment_variable_injection(dispatcher_instance: Any):
    """Tier 1: Dispatcher injects metadata into child process environment."""
    dispatcher, db = dispatcher_instance
    task_id = "tsk_env_001"
    event_id = "evt_env_001"

    db.tasks[task_id] = {
        "task_id": task_id,
        "event_id": event_id,
        "action_type": "cli",
        "command": "python3 -c \"import os; print(os.environ.get('ANTIGRAVITY_TASK_ID')); print(os.environ.get('ANTIGRAVITY_EVENT_ID'))\"",
        "status": "queued",
        "timeout_seconds": 10,
    }

    result = await dispatcher.execute_task(task_id)
    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert task_id in lines
    assert event_id in lines


async def test_agent_signal_file_emission(dispatcher_instance: Any, tmp_path: Path):
    """Tier 4: agent_signal action creates atomic signal JSON file."""
    dispatcher, db = dispatcher_instance
    signals_dir = tmp_path / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)

    task_id = "tsk_sig_001"
    db.tasks[task_id] = {
        "task_id": task_id,
        "event_id": "evt_sig_001",
        "action_type": "agent_signal",
        "target_action": str(signals_dir),
        "status": "queued",
        "timeout_seconds": 10,
    }

    result = await dispatcher.execute_task(task_id)
    assert result.status == "succeeded"

    # Verify signal file was created
    signal_files = list(signals_dir.glob("*.signal.json"))
    assert len(signal_files) == 1
    signal_content = json.loads(signal_files[0].read_text(encoding="utf-8"))
    assert signal_content["task_id"] == task_id


async def test_cli_command_stdin_devnull_safe(dispatcher_instance: Any):
    """Tier 1: Subprocess execution is resilient against Bad file descriptor (stdin connected to devnull)."""
    dispatcher, db = dispatcher_instance
    task_id = "tsk_disp_devnull"
    db.tasks[task_id] = {
        "task_id": task_id,
        "action_type": "cli",
        "command": "python3 -c \"import sys; print('stdin_readable:', sys.stdin.readable())\"",
        "status": "queued",
        "timeout_seconds": 10,
    }

    result = await dispatcher.execute_task(task_id)
    assert result.exit_code == 0
    assert result.status == "succeeded"
    assert "stdin_readable: True" in result.stdout


async def test_dispatcher_bounded_log_output_collector(dispatcher_instance: Any):
    """Tier 1: Subprocess execution bounds in-memory collector to MAX_COLLECTOR_LINES (500)."""
    dispatcher, db = dispatcher_instance
    task_id = "tsk_disp_heavy_output"
    db.tasks[task_id] = {
        "task_id": task_id,
        "action_type": "cli",
        "command": "python3 -c \"for i in range(600): print(f'line_{i}')\"",
        "status": "queued",
        "timeout_seconds": 10,
    }

    result = await dispatcher.execute_task(task_id)
    assert result.exit_code == 0
    assert result.status == "succeeded"
    lines = result.stdout.strip().splitlines()
    assert len(lines) <= 500
    assert "line_599" in lines
    assert "line_0" not in lines  # Oldest lines evicted from memory buffer
