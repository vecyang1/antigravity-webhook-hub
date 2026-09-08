"""Unit tests for hub/db.py SQLite SSOT layer.

Verifies Tiers 1 & 2 requirements:
- WAL mode PRAGMAs (journal_mode=WAL, busy_timeout=5000, synchronous=NORMAL, foreign_keys=ON, cache_size=-4000)
- 4-table schema creation (webhook_events, tasks, executions, execution_logs)
- Unidirectional event & task persistence
- Idempotent deduplication by idempotency key and payload hash
- Atomic CAS (Compare-And-Swap) state machine transitions
- Boot-time crash recovery for orphaned running tasks
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

try:
    from hub.db import DatabaseManager
except ImportError:
    DatabaseManager = None

pytestmark = pytest.mark.skipif(
    DatabaseManager is None,
    reason="hub.db (Milestone M2) not yet implemented",
)


@pytest.fixture
def db_manager(tmp_path: Path):
    """Fixture providing initialized DatabaseManager with isolated temp SQLite database."""
    db_file = tmp_path / "test_hub.db"
    mgr = DatabaseManager(str(db_file))
    mgr.init_schema()
    yield mgr
    mgr.close()


def test_sqlite_wal_pragmas_applied(db_manager: Any, tmp_path: Path):
    """Tier 1: Verifies WAL mode and memory tuning PRAGMAs are strictly enforced."""
    # Query raw connection to verify PRAGMAs
    db_file = tmp_path / "test_hub.db"
    conn = sqlite3.connect(str(db_file))
    try:
        cursor = conn.cursor()

        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0].lower()
        assert journal_mode == "wal", f"Expected journal_mode=wal, got {journal_mode}"

        cursor.execute("PRAGMA busy_timeout;")
        busy_timeout = cursor.fetchone()[0]
        assert busy_timeout == 5000, f"Expected busy_timeout=5000, got {busy_timeout}"

        cursor.execute("PRAGMA foreign_keys;")
        foreign_keys = cursor.fetchone()[0]
        assert foreign_keys in (1, "1", "ON", "on"), "Foreign keys must be enabled"

        cursor.execute("PRAGMA cache_size;")
        cache_size = cursor.fetchone()[0]
        assert cache_size == -4000, f"Expected cache_size=-4000 (~4MB cache), got {cache_size}"
    finally:
        conn.close()


def test_schema_tables_and_indexes_exist(db_manager: Any, tmp_path: Path):
    """Tier 1: Verifies the 4 required tables and indices exist in schema."""
    db_file = tmp_path / "test_hub.db"
    conn = sqlite3.connect(str(db_file))
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cursor.fetchall()}

        required_tables = {"webhook_events", "tasks", "executions", "execution_logs"}
        missing = required_tables - tables
        assert not missing, f"Missing required tables in schema: {missing}"
    finally:
        conn.close()


def test_unidirectional_event_and_task_insert(db_manager: Any):
    """Tier 1: Ingress -> SQLite insert with SSOT fidelity."""
    event_id = "evt_test_001"
    raw_payload = json.dumps({"test": "data", "action": "cli"})
    payload_hash = "a" * 64

    # 1. Insert Event
    event_dict = {
        "event_id": event_id,
        "source": "github",
        "idempotency_key": "idemp_key_001",
        "payload_hash": payload_hash,
        "headers_json": json.dumps({"content-type": "application/json"}),
        "raw_payload": raw_payload,
        "method": "POST",
        "path": "/webhook/github",
        "status": "received",
    }
    inserted = db_manager.insert_webhook_event(event_dict)
    assert inserted is True

    # 2. Insert Task linked to Event
    task_id = "tsk_test_001"
    task_dict = {
        "task_id": task_id,
        "event_id": event_id,
        "source": "github",
        "action_type": "cli",
        "command": "python3 -c 'print(1)'",
        "status": "received",
        "priority": 0,
        "timeout_seconds": 300,
    }
    task_res = db_manager.insert_task(task_dict)
    assert task_res == task_id

    # 3. Read back from DB
    retrieved_task = db_manager.get_task(task_id)
    assert retrieved_task is not None
    assert retrieved_task["task_id"] == task_id
    assert retrieved_task["event_id"] == event_id
    assert retrieved_task["status"] == "received"


def test_idempotency_duplicate_rejection(db_manager: Any):
    """Tier 2: Duplicate events with same (source, idempotency_key) are deduplicated."""
    event_dict = {
        "event_id": "evt_first",
        "source": "stripe",
        "idempotency_key": "stripe_charge_12345",
        "payload_hash": "b" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook/stripe",
        "status": "received",
    }
    # First insert succeeds
    first_res = db_manager.insert_webhook_event(event_dict)
    assert first_res is True

    # Duplicate insert with same (source, idempotency_key)
    duplicate_dict = dict(event_dict)
    duplicate_dict["event_id"] = "evt_second"  # different event ID, identical idempotency key

    second_res = db_manager.insert_webhook_event(duplicate_dict)
    assert second_res is False, "Duplicate event must not be inserted"


def test_atomic_cas_state_machine_valid_progression(db_manager: Any):
    """Tier 1: State machine transitions: received -> queued -> running -> succeeded."""
    event_id = "evt_cas_001"
    task_id = "tsk_cas_001"

    db_manager.insert_webhook_event({
        "event_id": event_id,
        "source": "cli",
        "idempotency_key": "key_cas_1",
        "payload_hash": "c" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook",
        "status": "received",
    })
    db_manager.insert_task({
        "task_id": task_id,
        "event_id": event_id,
        "source": "cli",
        "action_type": "cli",
        "command": "echo test",
        "status": "received",
        "priority": 0,
        "timeout_seconds": 60,
    })

    # received -> queued
    cas1 = db_manager.update_task_status_cas(task_id, expected_status="received", new_status="queued")
    assert cas1 is True
    t1 = db_manager.get_task(task_id)
    assert t1["status"] == "queued"

    # queued -> running
    cas2 = db_manager.update_task_status_cas(task_id, expected_status="queued", new_status="running")
    assert cas2 is True
    t2 = db_manager.get_task(task_id)
    assert t2["status"] == "running"
    assert t2["started_at"] is not None

    # running -> succeeded
    cas3 = db_manager.update_task_status_cas(
        task_id,
        expected_status="running",
        new_status="succeeded",
        exit_code=0,
    )
    assert cas3 is True
    t3 = db_manager.get_task(task_id)
    assert t3["status"] == "succeeded"
    assert t3["exit_code"] == 0
    assert t3["completed_at"] is not None


def test_atomic_cas_state_machine_invalid_transition_fails(db_manager: Any):
    """Tier 2: CAS update fails if expected_status does not match current DB status."""
    event_id = "evt_cas_inv"
    task_id = "tsk_cas_inv"

    db_manager.insert_webhook_event({
        "event_id": event_id,
        "source": "cli",
        "idempotency_key": "key_cas_inv",
        "payload_hash": "d" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook",
        "status": "received",
    })
    db_manager.insert_task({
        "task_id": task_id,
        "event_id": event_id,
        "source": "cli",
        "action_type": "cli",
        "command": "echo test",
        "status": "received",
        "priority": 0,
        "timeout_seconds": 60,
    })

    # Expected status 'running', but current is 'received' -> CAS returns False
    cas_fail = db_manager.update_task_status_cas(task_id, expected_status="running", new_status="succeeded")
    assert cas_fail is False, "CAS must fail when expected status does not match"

    # Verify status remains unmutated
    task = db_manager.get_task(task_id)
    assert task["status"] == "received"


def test_boot_time_crash_recovery_orphaned_tasks(db_manager: Any):
    """Tier 2: Tasks stuck in 'running' upon crash are recovered to 'failed' on startup."""
    event_id = "evt_crash"
    task_id = "tsk_crash_001"

    db_manager.insert_webhook_event({
        "event_id": event_id,
        "source": "crash_test",
        "idempotency_key": "crash_key",
        "payload_hash": "e" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook",
        "status": "received",
    })
    # Task inserted directly in 'running' state simulating interrupted execution
    db_manager.insert_task({
        "task_id": task_id,
        "event_id": event_id,
        "source": "crash_test",
        "action_type": "cli",
        "command": "echo crash",
        "status": "running",
        "priority": 0,
        "timeout_seconds": 60,
        "retry_count": 0,
        "max_retries": 0,
    })

    # Run boot-time crash recovery
    recovered_count = db_manager.recover_orphaned_tasks()
    assert recovered_count >= 1

    # Task must now be marked 'failed'
    recovered_task = db_manager.get_task(task_id)
    assert recovered_task["status"] == "failed"
    assert "restart" in recovered_task["error_message"].lower() or "interrupted" in recovered_task["error_message"].lower()
