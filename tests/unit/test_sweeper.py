"""
Unit & integration tests for background sweeper, unprocessed task recovery,
and macOS sleep/wake auto-pick detection.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from hub.broker import EventBroker
from hub.config import AppConfig, SweeperConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.observability import register_observability_routes
from hub.routes.tasks import register_task_routes
from hub.server import AsyncHTTPServer


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    db_file = str(tmp_path / "test_sweeper.db")
    mgr = DatabaseManager(db_file, cache_size=-16)
    mgr.init_schema()
    return mgr


@pytest.fixture
def broker() -> EventBroker:
    return EventBroker(default_queue_size=256)


# ---------------------------------------------------------------------------
# 1. Database SSOT Sweeper Tests
# ---------------------------------------------------------------------------

def test_db_get_unprocessed_tasks_empty(db: DatabaseManager):
    res = db.get_unprocessed_tasks()
    assert res["total_unprocessed"] == 0
    assert res["counts"]["queued"] == 0
    assert res["counts"]["received"] == 0
    assert res["counts"]["stale_running"] == 0
    assert res["counts"]["orphaned_events"] == 0
    assert res["counts"]["recoverable_failed"] == 0


def test_db_sweep_stale_running_tasks(db: DatabaseManager):
    # Insert event and stale running task (started 400s ago)
    db.insert_webhook_event({
        "event_id": "evt_stale_1",
        "source": "github",
        "raw_payload": '{"action": "test"}',
    })
    db.insert_task({
        "task_id": "tsk_stale_1",
        "event_id": "evt_stale_1",
        "source": "github",
        "action_type": "cli",
        "command": "echo test",
        "status": "running",
    })
    # Manually backdate started_at by 400s
    past_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 400))
    with db._lock:
        db._conn.execute("UPDATE tasks SET started_at = ? WHERE task_id = ?", (past_ts, "tsk_stale_1"))
        db._conn.commit()

    # Query before sweep
    unprocessed = db.get_unprocessed_tasks(stale_running_seconds=300)
    assert unprocessed["counts"]["stale_running"] == 1

    # Execute sweep
    sweep_res = db.sweep_and_requeue_unprocessed(stale_running_seconds=300)
    assert sweep_res["recovered_stale_running"] == 1
    assert "tsk_stale_1" in sweep_res["task_ids"]

    # Verify task state in DB is now 'queued'
    task = db.get_task("tsk_stale_1")
    assert task is not None
    assert task["status"] == "queued"


def test_db_sweep_orphaned_events(db: DatabaseManager):
    # Insert event with status 'received' and NO task
    db.insert_webhook_event({
        "event_id": "evt_orphan_1",
        "source": "slack",
        "raw_payload": '{"command": "echo orphan", "action_type": "cli"}',
        "status": "received",
    })

    orphans = db.get_orphaned_webhook_events()
    assert len(orphans) == 1
    assert orphans[0]["event_id"] == "evt_orphan_1"

    # Sweep and recover
    sweep_res = db.sweep_and_requeue_unprocessed()
    assert sweep_res["recovered_orphaned_events"] == 1
    assert len(sweep_res["task_ids"]) == 1

    # Verify task was created and linked to orphan event
    new_task_id = sweep_res["task_ids"][0]
    task = db.get_task(new_task_id)
    assert task is not None
    assert task["event_id"] == "evt_orphan_1"
    assert task["status"] == "queued"
    assert task["command"] == "echo orphan"


def test_db_sweep_interrupted_failed_tasks_with_retries(db: DatabaseManager):
    # Insert event and failed task with recoverable error and remaining retries
    db.insert_webhook_event({
        "event_id": "evt_retry_1",
        "source": "default",
        "raw_payload": "{}",
    })
    db.insert_task({
        "task_id": "tsk_retry_1",
        "event_id": "evt_retry_1",
        "source": "default",
        "action_type": "cli",
        "command": "echo retry",
        "status": "failed",
        "error_message": "Connection reset by peer or system sleep interruption",
        "retry_count": 0,
        "max_retries": 3,
    })

    # Sweep
    sweep_res = db.sweep_and_requeue_unprocessed(auto_retry_interrupted=True, max_retries=3)
    assert sweep_res["recovered_interrupted_failed"] == 1
    assert "tsk_retry_1" in sweep_res["task_ids"]

    # Verify task retry_count was incremented and status reset to 'queued'
    task = db.get_task("tsk_retry_1")
    assert task is not None
    assert task["status"] == "queued"
    assert task["retry_count"] == 1


def test_db_sweep_exhausted_retries_not_requeued(db: DatabaseManager):
    # Insert failed task where retry_count == max_retries
    db.insert_webhook_event({
        "event_id": "evt_exhausted_1",
        "source": "default",
        "raw_payload": "{}",
    })
    db.insert_task({
        "task_id": "tsk_exhausted_1",
        "event_id": "evt_exhausted_1",
        "source": "default",
        "action_type": "cli",
        "command": "echo fail",
        "status": "failed",
        "error_message": "connection timeout",
        "retry_count": 3,
        "max_retries": 3,
    })

    sweep_res = db.sweep_and_requeue_unprocessed(auto_retry_interrupted=True, max_retries=3)
    assert sweep_res["recovered_interrupted_failed"] == 0
    assert "tsk_exhausted_1" not in sweep_res["task_ids"]


# ---------------------------------------------------------------------------
# 2. TaskDispatcher Auto-Pick & Sleep Detection Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatcher_sweep_enqueues_and_broadcasts(db: DatabaseManager, broker: EventBroker):
    cfg = AppConfig()
    cfg.sweeper.enabled = True
    dispatcher = TaskDispatcher(db=db, broker=broker, config=cfg)

    # Seed stale task in DB
    db.insert_webhook_event({
        "event_id": "evt_disp_1",
        "source": "default",
        "raw_payload": "{}",
    })
    db.insert_task({
        "task_id": "tsk_disp_1",
        "event_id": "evt_disp_1",
        "source": "default",
        "action_type": "cli",
        "command": "python3 -c \"print('swept')\"",
        "status": "queued",
    })

    # Subscribe to sweeper_run SSE topic
    queue = broker.subscribe("sweeper_run")

    summary = await dispatcher.sweep_unprocessed_tasks(reason="test_manual")
    assert summary["reason"] == "test_manual"
    assert summary["newly_enqueued"] == 1
    assert "tsk_disp_1" in summary["task_ids"]

    # Verify SSE broker received the sweep notification
    msg = await asyncio.wait_for(queue.get(), timeout=2.0)
    assert msg["reason"] == "test_manual"
    assert "tsk_disp_1" in msg["task_ids"]

    # A second sweep should not double-enqueue the same task
    summary2 = await dispatcher.sweep_unprocessed_tasks(reason="test_second")
    assert summary2["newly_enqueued"] == 0


@pytest.mark.asyncio
async def test_dispatcher_sleep_wake_recovery_trigger(db: DatabaseManager, broker: EventBroker):
    """Simulate sleep clock jump triggering recovery."""
    cfg = AppConfig()
    cfg.sweeper.enabled = True
    cfg.sweeper.interval_seconds = 60
    cfg.sweeper.sleep_drift_threshold_seconds = 10
    dispatcher = TaskDispatcher(db=db, broker=broker, config=cfg)

    # Seed orphaned event
    db.insert_webhook_event({
        "event_id": "evt_sleep_1",
        "source": "cron",
        "raw_payload": '{"command": "echo wake"}',
        "status": "received",
    })

    # Trigger sweep with simulated 3-hour sleep duration
    summary = await dispatcher.sweep_unprocessed_tasks(
        reason="sleep_wake_recovery",
        sleep_duration=10800.0,
    )
    assert summary["reason"] == "sleep_wake_recovery"
    assert summary["sleep_duration"] == 10800.0
    assert summary["recovered_orphaned_events"] == 1
    assert dispatcher._sleep_recoveries_count == 1
    assert dispatcher._last_sleep_duration_seconds == 10800.0


# ---------------------------------------------------------------------------
# 3. HTTP REST API Tests (/tasks/unprocessed & /tasks/sweep)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_unprocessed_and_sweep_endpoints(db: DatabaseManager, broker: EventBroker, free_port: int):
    cfg = AppConfig()
    cfg.server.port = free_port
    server = AsyncHTTPServer(cfg.server)
    dispatcher = TaskDispatcher(db=db, broker=broker, config=cfg)
    register_task_routes(server, cfg, db=db, dispatcher=dispatcher, broker=broker)
    register_observability_routes(server, cfg, db=db, broker=broker)

    # Seed an orphaned event
    db.insert_webhook_event({
        "event_id": "evt_api_orphan",
        "source": "api_test",
        "raw_payload": '{"command": "echo api"}',
        "status": "received",
    })

    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Test GET /tasks/unprocessed
            res_get = await client.get(f"{base_url}/tasks/unprocessed")
            assert res_get.status_code == 200
            data_get = res_get.json()
            assert data_get["total_unprocessed"] == 1
            assert data_get["counts"]["orphaned_events"] == 1

            # Test POST /tasks/sweep with dry_run=true
            res_dry = await client.post(
                f"{base_url}/tasks/sweep",
                json={"dry_run": True},
            )
            assert res_dry.status_code == 200
            data_dry = res_dry.json()
            assert data_dry["dry_run"] is True
            assert data_dry["total_unprocessed"] == 1
            # DB state unchanged
            assert len(db.get_orphaned_webhook_events()) == 1

            # Test POST /tasks/sweep live execution
            res_sweep = await client.post(
                f"{base_url}/tasks/sweep",
                json={"dry_run": False},
            )
            assert res_sweep.status_code == 200
            data_sweep = res_sweep.json()
            assert data_sweep["dry_run"] is False
            assert data_sweep["recovered_orphaned_events"] == 1

            # Verify orphan is now resolved
            assert len(db.get_orphaned_webhook_events()) == 0

            # Test GET /healthz includes unprocessed_tasks_count
            res_health = await client.get(f"{base_url}/healthz")
            assert res_health.status_code == 200
            data_health = res_health.json()
            assert "unprocessed_tasks_count" in data_health
            assert "unprocessed" in data_health
    finally:
        await server.stop()
