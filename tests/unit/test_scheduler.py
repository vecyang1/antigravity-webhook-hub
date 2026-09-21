"""Unit tests for Antigravity Webhook Hub TaskScheduler engine.

Tests cron computation, schedule parsing, SSOT persistence, monotonic sleep/wake detection,
and on-demand triggering.
"""

import asyncio
import datetime
import time
from pathlib import Path

import pytest

from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.models import ScheduledTask, ScheduleStatus, ScheduleType
from hub.scheduler import (
    TaskScheduler,
    compute_next_cron_run,
    format_iso_utc,
    parse_cron_field,
    parse_schedule_time,
)


class MockDispatcher:
    def __init__(self):
        self.enqueued_tasks = []

    async def enqueue(self, task_id: str):
        self.enqueued_tasks.append(task_id)


class MockBroker:
    def __init__(self):
        self.published_events = []

    async def publish(self, topic: str, data: dict):
        self.published_events.append((topic, data))


def test_parse_cron_field():
    assert parse_cron_field("5", 0, 59) == {5}
    assert parse_cron_field("1-3", 0, 59) == {1, 2, 3}
    assert parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}
    assert parse_cron_field("1,3,5", 0, 59) == {1, 3, 5}
    assert parse_cron_field("*", 1, 5) == {1, 2, 3, 4, 5}


def test_compute_next_cron_run_every_minute():
    base = datetime.datetime(2026, 9, 21, 10, 30, 15, tzinfo=datetime.timezone.utc)
    nxt = compute_next_cron_run("* * * * *", start_dt=base)
    assert nxt == datetime.datetime(2026, 9, 21, 10, 31, 0, tzinfo=datetime.timezone.utc)


def test_compute_next_cron_run_daily_9am():
    base = datetime.datetime(2026, 9, 21, 8, 30, 0, tzinfo=datetime.timezone.utc)
    nxt = compute_next_cron_run("0 9 * * *", start_dt=base)
    assert nxt == datetime.datetime(2026, 9, 21, 9, 0, 0, tzinfo=datetime.timezone.utc)

    # If past 9am, roll over to tomorrow
    base_past = datetime.datetime(2026, 9, 21, 9, 30, 0, tzinfo=datetime.timezone.utc)
    nxt_past = compute_next_cron_run("0 9 * * *", start_dt=base_past)
    assert nxt_past == datetime.datetime(2026, 9, 22, 9, 0, 0, tzinfo=datetime.timezone.utc)


def test_compute_next_cron_run_invalid():
    with pytest.raises(ValueError):
        compute_next_cron_run("invalid cron expr")


def test_parse_schedule_time():
    base = datetime.datetime(2026, 9, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Delay seconds
    res = parse_schedule_time(delay_seconds=3600, base_dt=base)
    assert res == datetime.datetime(2026, 9, 21, 13, 0, 0, tzinfo=datetime.timezone.utc)

    # Delay strings
    assert parse_schedule_time(delay="30s", base_dt=base) == base + datetime.timedelta(seconds=30)
    assert parse_schedule_time(delay="15m", base_dt=base) == base + datetime.timedelta(minutes=15)
    assert parse_schedule_time(delay="2h", base_dt=base) == base + datetime.timedelta(hours=2)
    assert parse_schedule_time(delay="2d", base_dt=base) == base + datetime.timedelta(days=2)
    assert parse_schedule_time(delay="1w", base_dt=base) == base + datetime.timedelta(weeks=1)

    # ISO 8601 string
    iso_res = parse_schedule_time(scheduled_at="2026-09-23T16:18:00+08:00")
    assert iso_res == datetime.datetime(2026, 9, 23, 8, 18, 0, tzinfo=datetime.timezone.utc)


async def test_scheduler_lifecycle_and_crud(tmp_path):
    db_file = tmp_path / "test_hub.db"
    db = DatabaseManager(str(db_file))
    dispatcher = MockDispatcher()
    broker = MockBroker()
    scheduler = TaskScheduler(db=db, dispatcher=dispatcher, broker=broker)

    # Create one-off schedule
    sched = await scheduler.create_schedule(
        name="Test One-off",
        schedule_type="once",
        delay="1h",
        action_type="cli",
        command="echo hello",
    )
    assert sched.schedule_id.startswith("sch_")
    assert sched.status == "active"
    assert sched.max_runs == 1

    # Read from DB
    retrieved = db.get_schedule(sched.schedule_id)
    assert retrieved is not None
    assert retrieved["name"] == "Test One-off"

    # Pause
    paused = await scheduler.pause_schedule(sched.schedule_id)
    assert paused is True
    assert db.get_schedule(sched.schedule_id)["status"] == "paused"

    # Resume
    resumed = await scheduler.resume_schedule(sched.schedule_id)
    assert resumed is True
    assert db.get_schedule(sched.schedule_id)["status"] == "active"

    # Cancel
    cancelled = await scheduler.cancel_schedule(sched.schedule_id)
    assert cancelled is True
    assert db.get_schedule(sched.schedule_id)["status"] == "cancelled"

    db.close()


async def test_scheduler_sweep_and_execution(tmp_path):
    db_file = tmp_path / "test_hub.db"
    db = DatabaseManager(str(db_file))
    dispatcher = MockDispatcher()
    broker = MockBroker()
    scheduler = TaskScheduler(db=db, dispatcher=dispatcher, broker=broker)

    # Create a schedule that is already due (delay_seconds = 0)
    sched = await scheduler.create_schedule(
        name="Immediate Due Task",
        schedule_type="once",
        delay_seconds=0,
        action_type="cli",
        command="python3 -c 'print(1)'",
    )

    # Sweep due
    sweep_res = await scheduler.sweep_due(reason="test_sweep")
    assert sweep_res["triggered_count"] == 1
    assert len(dispatcher.enqueued_tasks) == 1

    # Verify task was inserted in tasks table
    enqueued_task_id = dispatcher.enqueued_tasks[0]
    task_row = db.get_task(enqueued_task_id)
    assert task_row is not None
    assert task_row["command"] == "python3 -c 'print(1)'"

    # Verify schedule status is updated to completed
    updated_sched = db.get_schedule(sched.schedule_id)
    assert updated_sched["status"] == "completed"
    assert updated_sched["total_runs"] == 1

    db.close()


async def test_scheduler_recurring_cron_sweep(tmp_path):
    db_file = tmp_path / "test_hub.db"
    db = DatabaseManager(str(db_file))
    dispatcher = MockDispatcher()
    broker = MockBroker()
    scheduler = TaskScheduler(db=db, dispatcher=dispatcher, broker=broker)

    # Create recurring schedule with past next_run_at to simulate due trigger
    sched = await scheduler.create_schedule(
        name="Recurring Cron",
        schedule_type="recurring",
        cron_expression="*/5 * * * *",
        action_type="cli",
        command="echo recurring",
    )
    # Force next_run_at to past
    past_iso = format_iso_utc(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1))
    db.update_schedule(sched.schedule_id, {"next_run_at": past_iso})

    # Sweep
    sweep_res = await scheduler.sweep_due(reason="cron_tick")
    assert sweep_res["triggered_count"] == 1

    # Verify status is still active and next_run_at advanced
    updated_sched = db.get_schedule(sched.schedule_id)
    assert updated_sched["status"] == "active"
    assert updated_sched["total_runs"] == 1
    assert updated_sched["next_run_at"] > past_iso

    db.close()


async def test_scheduler_sleep_recovery_dont_miss_the_beat(tmp_path):
    db_file = tmp_path / "test_hub.db"
    db = DatabaseManager(str(db_file))
    dispatcher = MockDispatcher()
    broker = MockBroker()
    scheduler = TaskScheduler(db=db, dispatcher=dispatcher, broker=broker)

    # Create schedule due during simulated sleep
    sched = await scheduler.create_schedule(
        name="Agent Call During Sleep",
        schedule_type="once",
        delay_seconds=0,
        action_type="antigravity",
        command="agentapi new-conversation",
    )

    # Simulate sleep/wake catch-up sweep
    sweep_res = await scheduler.sweep_due(reason="sleep_wake_recovery", sleep_duration=3600.0)
    assert sweep_res["reason"] == "sleep_wake_recovery"
    assert sweep_res["triggered_count"] == 1
    assert len(dispatcher.enqueued_tasks) == 1

    db.close()


async def test_scheduler_trigger_now(tmp_path):
    db_file = tmp_path / "test_hub.db"
    db = DatabaseManager(str(db_file))
    dispatcher = MockDispatcher()
    broker = MockBroker()
    scheduler = TaskScheduler(db=db, dispatcher=dispatcher, broker=broker)

    # Create future schedule (2 days from now)
    sched = await scheduler.create_schedule(
        name="Future Schedule",
        schedule_type="once",
        delay="2d",
        action_type="cli",
        command="echo future",
    )

    # Trigger immediately on-demand
    res = await scheduler.trigger_now(sched.schedule_id)
    assert res is not None
    assert res["schedule_id"] == sched.schedule_id
    assert len(dispatcher.enqueued_tasks) == 1

    db.close()


def test_parse_schedule_time_validation_and_formats():
    """Verify strict validation and diverse date formats for schedule parsing."""
    base = datetime.datetime(2026, 9, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # 1. Spark dd/MM/yyyy date format
    dt_spark = parse_schedule_time(scheduled_at="23/09/2026", base_dt=base)
    # Default 9am local (+08:00) -> 01:00 UTC
    assert dt_spark == datetime.datetime(2026, 9, 23, 1, 0, 0, tzinfo=datetime.timezone.utc)

    # 2. Spark yyyy-MM-ddTHH:mm format
    dt_spark_iso = parse_schedule_time(scheduled_at="2026-09-23T16:18", base_dt=base)
    assert dt_spark_iso == datetime.datetime(2026, 9, 23, 8, 18, 0, tzinfo=datetime.timezone.utc)

    # 3. yyyy/MM/dd format
    dt_slash = parse_schedule_time(scheduled_at="2026/09/23", base_dt=base)
    assert dt_slash == datetime.datetime(2026, 9, 23, 1, 0, 0, tzinfo=datetime.timezone.utc)

    # 4. Strict failure on completely invalid string
    with pytest.raises(ValueError, match="Invalid scheduled_at format"):
        parse_schedule_time(scheduled_at="invalid-garbage-date")

    # 5. Strict failure on invalid delay string
    with pytest.raises(ValueError, match="Invalid delay format"):
        parse_schedule_time(delay="2lightyears")


async def test_scheduler_task_schedule_id_linkage_and_history(tmp_path):
    """Verify that triggered tasks preserve schedule_id and are queryable via schedule history."""
    db_file = tmp_path / "test_hub_linkage.db"
    db = DatabaseManager(str(db_file))
    dispatcher = MockDispatcher()
    scheduler = TaskScheduler(db=db, dispatcher=dispatcher)

    sched = await scheduler.create_schedule(
        name="Linked Scheduled Task",
        schedule_type="once",
        delay_seconds=0,
        action_type="cli",
        command="echo linked",
    )

    await scheduler.sweep_due()
    assert len(dispatcher.enqueued_tasks) == 1
    task_id = dispatcher.enqueued_tasks[0]

    # Verify task row in SQLite SSOT has schedule_id populated
    task_row = db.get_task(task_id)
    assert task_row is not None
    assert task_row["schedule_id"] == sched.schedule_id
    assert task_row["scheduled_at"] is not None

    # Verify get_schedule_history queries by schedule_id accurately
    history = db.get_schedule_history(sched.schedule_id)
    assert len(history) == 1
    assert history[0]["task_id"] == task_id
    assert history[0]["schedule_id"] == sched.schedule_id

    db.close()

