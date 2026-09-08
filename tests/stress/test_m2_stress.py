"""Challenger M2-1 Empirical Stress & Adversarial Test Suite.

Empirically tests:
1. Concurrent webhook duplicate race conditions ((source, idempotency_key) and payload hash)
2. Zero DB side effects for invalid requests (missing auth, tampered sig, expired/future ts, malformed/non-dict JSON, empty body)
3. Concurrent read/write throughput under SQLite WAL mode without 'database is locked' errors
4. Boot-time crash recovery for stranded running tasks (retries exhausted vs retryable)
5. [Adversarial Challenge] Idempotency key collision when orphan event exists
6. [Adversarial Challenge] Non-integer priority/timeout causing 500 and leaving orphan event
7. [Adversarial Challenge] Boot-time dispatcher rehydration of queued tasks
8. [Adversarial Challenge] Audit trail population in executions table
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.webhook import register_webhook_routes
from hub.security import generate_hmac_signature
from hub.server import AsyncHTTPServer


@pytest.fixture
def stress_config(free_port: int, temp_db_path: str) -> AppConfig:
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.webhook_secret = "stress_test_secret_32bytes_1234"
    config.security.bearer_token = "stress_bearer_token_1234"
    config.database.path = temp_db_path
    return config


@pytest.fixture
async def stress_server(stress_config: AppConfig, temp_db_path: str):
    server = AsyncHTTPServer(stress_config.server)
    db = DatabaseManager(temp_db_path)
    db.init_schema()

    dispatcher = TaskDispatcher(db, config=stress_config)
    await dispatcher.start()

    register_webhook_routes(server, stress_config, db, dispatcher)
    await server.start()

    base_url = f"http://127.0.0.1:{stress_config.server.port}"
    yield base_url, server, stress_config, db, dispatcher

    await server.stop()
    await dispatcher.stop()
    db.close()


# ---------------------------------------------------------------------------
# CHALLENGE 1: Concurrent Duplicate Ingress Race Conditions
# ---------------------------------------------------------------------------


async def test_concurrent_duplicate_idempotency_key_race(stress_server: Any):
    """Fire 20 concurrent requests with identical (source, idempotency_key).

    Assert:
    - Zero server 500 errors or uncaught IntegrityErrors
    - Exactly 1 task created in SQLite database
    - Exactly 1 event created in SQLite database
    - All 20 requests receive matching task_id
    - Exactly 1 request receives duplicate=False (status 202)
    - 19 requests receive duplicate=True (status 200 or 202)
    """
    base_url, server, config, db, dispatcher = stress_server

    body = json.dumps({"action": "cli", "command": "echo concurrent_idemp"}).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)
    idemp_key = "concurrent_idemp_key_001"

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
        "X-Hub-Event-ID": idemp_key,
    }

    concurrency = 20
    async with httpx.AsyncClient(timeout=10.0) as client:
        async def send_req():
            return await client.post(f"{base_url}/webhook/test_src", content=body, headers=headers)

        responses = await asyncio.gather(*[send_req() for _ in range(concurrency)])

    status_codes = [r.status_code for r in responses]
    assert all(code in (200, 202) for code in status_codes), f"Unexpected status codes: {status_codes}"

    json_bodies = [r.json() for r in responses]

    task_ids = {j.get("task_id") for j in json_bodies}
    assert len(task_ids) == 1, f"Expected exactly 1 distinct task_id, got: {task_ids}"
    task_id = list(task_ids)[0]
    assert task_id is not None

    accepted_responses = [j for j in json_bodies if not j.get("duplicate") and not j.get("deduplicated")]
    duplicate_responses = [j for j in json_bodies if j.get("duplicate") or j.get("deduplicated")]

    assert len(accepted_responses) == 1, f"Expected 1 accepted, got {len(accepted_responses)}"
    assert len(duplicate_responses) == concurrency - 1, f"Expected {concurrency - 1} duplicates, got {len(duplicate_responses)}"

    events = await db.execute_read("SELECT count(*) as cnt FROM webhook_events WHERE idempotency_key = ?", (idemp_key,))
    assert events[0]["cnt"] == 1, f"Expected exactly 1 webhook_events row, got {events[0]['cnt']}"

    tasks = await db.execute_read("SELECT count(*) as cnt FROM tasks")
    assert tasks[0]["cnt"] == 1, f"Expected exactly 1 task row in DB, got {tasks[0]['cnt']}"


async def test_concurrent_duplicate_payload_hash_race(stress_server: Any):
    """Fire 20 concurrent requests with identical payload (same payload hash, no explicit event ID).

    Assert:
    - Fallback deterministic deduplication catches all concurrent requests
    - Exactly 1 task created in database
    - All 20 requests receive identical task_id
    - Exactly 1 request duplicate=False, 19 duplicate=True
    """
    base_url, server, config, db, dispatcher = stress_server

    body = json.dumps({"action": "cli", "command": "echo payload_hash_dedup", "rand": 42}).encode("utf-8")
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
    }

    concurrency = 20
    async with httpx.AsyncClient(timeout=10.0) as client:
        async def send_req():
            return await client.post(f"{base_url}/webhook/hash_src", content=body, headers=headers)

        responses = await asyncio.gather(*[send_req() for _ in range(concurrency)])

    status_codes = [r.status_code for r in responses]
    assert all(code in (200, 202) for code in status_codes), f"Non-success status codes: {status_codes}"

    json_bodies = [r.json() for r in responses]
    task_ids = {j.get("task_id") for j in json_bodies}
    assert len(task_ids) == 1, f"Expected 1 unique task_id, got: {task_ids}"

    accepted = [j for j in json_bodies if not j.get("duplicate") and not j.get("deduplicated")]
    assert len(accepted) == 1, f"Expected exactly 1 accepted response, got {len(accepted)}"

    tasks = await db.execute_read("SELECT count(*) as cnt FROM tasks")
    assert tasks[0]["cnt"] == 1, f"Expected exactly 1 task created, got {tasks[0]['cnt']}"


# ---------------------------------------------------------------------------
# CHALLENGE 2: Zero DB Side Effects on Invalid Requests
# ---------------------------------------------------------------------------


async def test_invalid_requests_zero_db_side_effects(stress_server: Any):
    """Verify that all invalid requests leave strictly ZERO rows across all 4 SQLite tables."""
    base_url, server, config, db, dispatcher = stress_server

    valid_body = b'{"action": "cli", "command": "echo test"}'
    ts = int(time.time())
    valid_sig = generate_hmac_signature(config.security.webhook_secret, valid_body, ts)

    invalid_cases = [
        # (name, path, headers, body, expected_status)
        ("missing_auth", "/webhook", {"Content-Type": "application/json"}, valid_body, 401),
        (
            "tampered_hmac",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": valid_sig[:-1] + ("0" if valid_sig[-1] != "0" else "1"),
                "X-Hub-Timestamp": str(ts),
            },
            valid_body,
            401,
        ),
        (
            "expired_timestamp_301s",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": generate_hmac_signature(config.security.webhook_secret, valid_body, ts - 301),
                "X-Hub-Timestamp": str(ts - 301),
            },
            valid_body,
            401,
        ),
        (
            "future_timestamp_65s",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": generate_hmac_signature(config.security.webhook_secret, valid_body, ts + 65),
                "X-Hub-Timestamp": str(ts + 65),
            },
            valid_body,
            401,
        ),
        (
            "malformed_json_unclosed_brace",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": generate_hmac_signature(config.security.webhook_secret, b'{"action": "cli"', ts),
                "X-Hub-Timestamp": str(ts),
            },
            b'{"action": "cli"',
            400,
        ),
        (
            "malformed_json_raw_string",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": generate_hmac_signature(config.security.webhook_secret, b'"just a string"', ts),
                "X-Hub-Timestamp": str(ts),
            },
            b'"just a string"',
            400,
        ),
        (
            "malformed_json_array_instead_of_object",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": generate_hmac_signature(config.security.webhook_secret, b'[1, 2, 3]', ts),
                "X-Hub-Timestamp": str(ts),
            },
            b'[1, 2, 3]',
            400,
        ),
        (
            "empty_body",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": generate_hmac_signature(config.security.webhook_secret, b"", ts),
                "X-Hub-Timestamp": str(ts),
            },
            b"",
            400,
        ),
        (
            "invalid_bearer_token",
            "/webhook",
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer wrong_token_abc",
            },
            valid_body,
            401,
        ),
    ]

    async with httpx.AsyncClient(timeout=5.0) as client:
        for name, path, headers, body, expected_status in invalid_cases:
            resp = await client.post(f"{base_url}{path}", content=body, headers=headers)
            assert resp.status_code == expected_status, (
                f"Case '{name}' expected HTTP {expected_status}, got {resp.status_code}: {resp.text}"
            )

    # Verify zero database rows exist across all tables
    for table in ("webhook_events", "tasks", "executions", "execution_logs"):
        rows = await db.execute_read(f"SELECT count(*) as cnt FROM {table}")
        assert rows[0]["cnt"] == 0, f"Table '{table}' expected 0 rows, found {rows[0]['cnt']}"


# ---------------------------------------------------------------------------
# CHALLENGE 3: Concurrent Read/Write Throughput Under SQLite WAL Mode
# ---------------------------------------------------------------------------


async def test_sqlite_wal_concurrent_throughput_no_locks(tmp_path: Path):
    """Stress test SQLite WAL mode with 100 concurrent workers (50 writers, 50 readers).

    Assert:
    - Zero 'database is locked' or OperationalError failures
    - All write operations committed and verifiable by readers
    - PRAGMA journal_mode is WAL
    """
    db_file = tmp_path / "wal_stress.db"
    mgr = DatabaseManager(str(db_file))
    mgr.init_schema()

    writer_count = 50
    reader_count = 50

    errors: list[Exception] = []

    async def writer(i: int):
        try:
            event_id = f"evt_stress_{i:04d}"
            task_id = f"tsk_stress_{i:04d}"
            event_data = {
                "event_id": event_id,
                "source": "wal_stress",
                "idempotency_key": f"key_{i}",
                "payload_hash": hashlib.sha256(f"payload_{i}".encode()).hexdigest(),
                "headers_json": "{}",
                "raw_payload": json.dumps({"worker": i}),
                "method": "POST",
                "path": "/webhook/wal_stress",
                "status": "received",
            }
            inserted = mgr.insert_webhook_event(event_data)
            assert inserted is True

            task_data = {
                "task_id": task_id,
                "event_id": event_id,
                "source": "wal_stress",
                "action_type": "cli",
                "command": f"echo {i}",
                "status": "received",
            }
            mgr.insert_task(task_data)

            # Atomic CAS transition
            cas_res = mgr.update_task_status_cas(task_id, "received", "queued")
            assert cas_res is True

            # Insert execution log
            log_id = mgr.insert_log_chunk(task_id, "stdout", f"log output from writer {i}")
            assert log_id > 0

        except Exception as e:
            errors.append(e)

    async def reader(i: int):
        try:
            await asyncio.sleep(0.005)
            rows = await mgr.execute_read("SELECT count(*) as cnt FROM tasks")
            assert rows[0]["cnt"] >= 0
            evts = await mgr.execute_read("SELECT count(*) as cnt FROM webhook_events")
            assert evts[0]["cnt"] >= 0
        except Exception as e:
            errors.append(e)

    tasks = [writer(i) for i in range(writer_count)] + [reader(i) for i in range(reader_count)]
    await asyncio.gather(*tasks)

    mgr.close()

    assert len(errors) == 0, f"Encountered {len(errors)} errors during WAL stress test: {errors[:5]}"

    # Verify final counts with raw SQLite connection
    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode;")
        mode = cur.fetchone()[0].lower()
        assert mode == "wal"

        cur.execute("SELECT count(*) FROM webhook_events;")
        event_cnt = cur.fetchone()[0]
        assert event_cnt == writer_count

        cur.execute("SELECT count(*) FROM tasks WHERE status = 'queued';")
        task_cnt = cur.fetchone()[0]
        assert task_cnt == writer_count

        cur.execute("SELECT count(*) FROM execution_logs;")
        log_cnt = cur.fetchone()[0]
        assert log_cnt == writer_count
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CHALLENGE 4: Boot-Time Crash Recovery Matrix
# ---------------------------------------------------------------------------


def test_boot_time_crash_recovery_matrix(tmp_path: Path):
    """Test boot-time crash recovery transitions stranded 'running' tasks properly.

    Matrix:
    - Task A: running, retry_count=0, max_retries=0 -> transitions to 'failed', reason contains 'restart'
    - Task B: running, retry_count=0, max_retries=3 -> transitions to 'queued', retry_count incremented to 1
    - Task C: running, retry_count=3, max_retries=3 -> transitions to 'failed', retries exhausted
    - Task D: queued -> remains 'queued'
    - Task E: succeeded -> remains 'succeeded'
    - Task F: failed -> remains 'failed'
    """
    db_file = tmp_path / "recovery_matrix.db"
    mgr = DatabaseManager(str(db_file))
    mgr.init_schema()

    # Seed event
    event_id = "evt_recovery_matrix"
    mgr.insert_webhook_event({
        "event_id": event_id,
        "source": "matrix_test",
        "idempotency_key": "k_matrix",
        "payload_hash": "f" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook",
        "status": "received",
    })

    # Task A: running, 0 retries
    mgr.insert_task({
        "task_id": "task_a_no_retries",
        "event_id": event_id,
        "action_type": "cli",
        "status": "running",
        "retry_count": 0,
        "max_retries": 0,
    })

    # Task B: running, has retries left (0 of 3)
    mgr.insert_task({
        "task_id": "task_b_has_retries",
        "event_id": event_id,
        "action_type": "cli",
        "status": "running",
        "retry_count": 0,
        "max_retries": 3,
    })

    # Task C: running, retries exhausted (3 of 3)
    mgr.insert_task({
        "task_id": "task_c_exhausted",
        "event_id": event_id,
        "action_type": "cli",
        "status": "running",
        "retry_count": 3,
        "max_retries": 3,
    })

    # Task D: queued
    mgr.insert_task({
        "task_id": "task_d_queued",
        "event_id": event_id,
        "action_type": "cli",
        "status": "queued",
        "retry_count": 0,
        "max_retries": 1,
    })

    # Task E: succeeded
    mgr.insert_task({
        "task_id": "task_e_succeeded",
        "event_id": event_id,
        "action_type": "cli",
        "status": "succeeded",
        "retry_count": 0,
        "max_retries": 1,
    })

    # Task F: failed
    mgr.insert_task({
        "task_id": "task_f_failed",
        "event_id": event_id,
        "action_type": "cli",
        "status": "failed",
        "retry_count": 1,
        "max_retries": 1,
    })

    # Execute crash recovery
    recovered_count = mgr.recover_orphaned_tasks()
    assert recovered_count == 3, f"Expected 3 tasks recovered from running, got {recovered_count}"

    # Verify Task A
    t_a = mgr.get_task("task_a_no_retries")
    assert t_a["status"] == "failed"
    assert "restart" in t_a["error_message"].lower() or "interrupted" in t_a["error_message"].lower()
    assert t_a["completed_at"] is not None

    # Verify Task B: re-queued with retry_count incremented
    t_b = mgr.get_task("task_b_has_retries")
    assert t_b["status"] == "queued"
    assert t_b["retry_count"] == 1

    # Verify Task C: retries exhausted -> failed
    t_c = mgr.get_task("task_c_exhausted")
    assert t_c["status"] == "failed"
    assert t_c["retry_count"] == 3

    # Verify untouched tasks
    t_d = mgr.get_task("task_d_queued")
    assert t_d["status"] == "queued"

    t_e = mgr.get_task("task_e_succeeded")
    assert t_e["status"] == "succeeded"

    t_f = mgr.get_task("task_f_failed")
    assert t_f["status"] == "failed"

    mgr.close()


# ---------------------------------------------------------------------------
# CHALLENGE 5: Empirical Adversarial Failure Reproductions (FINDINGS)
# ---------------------------------------------------------------------------


async def test_adversarial_finding1_idempotent_retry_on_orphan_event_must_not_500(stress_server: Any):
    """ADVERSARIAL PROOF OF FINDING 1:

    Contract: When an event exists in webhook_events without a task (e.g. from partial insert or crash),
    a subsequent request with the same idempotency key must be handled cleanly and must NOT crash with HTTP 500.
    Bug in hub/routes/webhook.py: line 156 calls db.insert_task(task_record) with an uninserted event_id,
    violating foreign key constraint and crashing with HTTP 500.
    """
    base_url, server, config, db, dispatcher = stress_server

    # Pre-seed an orphan event without a task
    orphan_idemp = "orphan_idemp_key_999"
    orphan_eid = "evt_orphan_preexisting"
    db.insert_webhook_event({
        "event_id": orphan_eid,
        "source": "test_src",
        "idempotency_key": orphan_idemp,
        "payload_hash": hashlib.sha256(b"preexisting").hexdigest(),
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook/test_src",
        "status": "received",
    })

    # Now a client sends a webhook with that same idempotency key
    body = b'{"action": "cli", "command": "echo retry"}'
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
        "X-Hub-Event-ID": orphan_idemp,
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook/test_src", content=body, headers=headers)

    assert resp.status_code != 500, (
        f"BUG CONFIRMED (Finding 1): Server crashed with HTTP 500 on duplicate retry with orphan event: {resp.text}"
    )
    assert "FOREIGN KEY" not in resp.text, (
        f"BUG CONFIRMED (Finding 1): FOREIGN KEY constraint violation leaked: {resp.text}"
    )


async def test_adversarial_finding2_boot_time_queued_tasks_must_be_rehydrated(tmp_path: Path):
    """ADVERSARIAL PROOF OF FINDING 2:

    Contract: When server restarts, TaskDispatcher.start() must rehydrate and execute
    tasks that are stranded in status 'queued' in SQLite.
    Bug in hub/dispatcher.py: TaskDispatcher.start() never fetches or enqueues existing
    'queued' tasks into self.queue, leaving them stranded indefinitely.
    """
    db_file = tmp_path / "rehydration.db"
    db = DatabaseManager(str(db_file))
    db.init_schema()

    # Pre-seed an event and a queued task
    db.insert_webhook_event({
        "event_id": "evt_queued_test",
        "source": "test",
        "idempotency_key": "k_queued",
        "payload_hash": "h_queued",
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook",
        "status": "received",
    })
    db.insert_task({
        "task_id": "tsk_should_rehydrate",
        "event_id": "evt_queued_test",
        "source": "test",
        "action_type": "cli",
        "command": "echo rehydrated",
        "status": "queued",
        "retry_count": 0,
        "max_retries": 1,
    })

    dispatcher = TaskDispatcher(db)
    await dispatcher.start()

    # Wait for dispatcher to process
    await asyncio.sleep(0.3)

    qsize = dispatcher.queue.qsize()
    task = db.get_task("tsk_should_rehydrate")
    await dispatcher.stop()
    db.close()

    assert task["status"] in ("running", "succeeded") or qsize > 0, (
        f"BUG CONFIRMED (Finding 2): Stranded 'queued' task was neither dispatched nor enqueued! "
        f"Queue size={qsize}, task status={task['status']}"
    )


async def test_adversarial_finding3_executions_table_must_be_populated(stress_server: Any):
    """ADVERSARIAL PROOF OF FINDING 3:

    Contract: Feature 6 specifies 4-table relational schema (webhook_events, tasks, executions, execution_logs).
    When a task is dispatched, an execution attempt record must be inserted into executions table.
    Bug in hub/dispatcher.py: TaskDispatcher never calls db.insert_execution, leaving executions table empty.
    """
    base_url, server, config, db, dispatcher = stress_server

    body = b'{"action": "cli", "command": "echo execution_audit_check"}'
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
        "X-Hub-Event-ID": "idemp_exec_audit",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

    # Wait for task to finish execution
    for _ in range(20):
        t = db.get_task(task_id)
        if t and t["status"] in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.1)

    # Inspect executions table
    rows = await db.execute_read("SELECT count(*) as cnt FROM executions")
    assert rows[0]["cnt"] > 0, (
        f"BUG CONFIRMED (Finding 3): Executions table has {rows[0]['cnt']} rows. "
        "TaskDispatcher does not record execution attempts into the executions table."
    )


async def test_adversarial_finding4_invalid_task_schema_must_reject_with_400_and_zero_db_writes(stress_server: Any):
    """ADVERSARIAL PROOF OF FINDING 4:

    Contract: Invalid payload fields (e.g. non-integer priority 'urgent') must be rejected
    with HTTP 400 before DB write, leaving strictly ZERO rows in SQLite.
    Bug in hub/routes/webhook.py: Lines 112-156 insert webhook_events FIRST, then call
    int(body_dict['priority']), crashing with ValueError (HTTP 500) and leaving an orphaned event.
    """
    base_url, server, config, db, dispatcher = stress_server

    body = b'{"action": "cli", "command": "echo test", "priority": "urgent"}'
    ts = int(time.time())
    sig = generate_hmac_signature(config.security.webhook_secret, body, ts)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(ts),
        "X-Hub-Event-ID": "idemp_priority_urgent",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)

    events = await db.execute_read("SELECT count(*) as cnt FROM webhook_events WHERE idempotency_key = 'idemp_priority_urgent'")
    tasks = await db.execute_read("SELECT count(*) as cnt FROM tasks")

    assert resp.status_code == 400, (
        f"BUG CONFIRMED (Finding 4): Expected HTTP 400 for invalid priority, got {resp.status_code}: {resp.text}"
    )
    assert events[0]["cnt"] == 0, (
        f"BUG CONFIRMED (Finding 4): Invalid payload left {events[0]['cnt']} orphaned rows in webhook_events!"
    )
