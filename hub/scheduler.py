"""
Antigravity Webhook Hub — Resilient Scheduled & Delayed Task Engine
Provides SQLite SSOT-backed scheduling for delayed tasks and recurring cron triggers.
Features:
- Single Source of Truth (SSOT): All schedules persist in SQLite `scheduled_tasks` table.
- Monotonic Sleep/Wake Detection ("Don't Miss the Beat"): Recovers and triggers overdue
  schedules immediately upon waking up from macOS system sleep or daemon restarts.
- Pure Python 5-part cron parser (<0.2ms resolution, zero external dependencies).
- Realtime Event Broadcasting: Emits SSE events for schedule creation, triggering,
  execution, status changes, and sleep/wake catch-up recovery.
- Multi-target execution through TaskDispatcher (cli, antigravity agent calls, contact_review).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
import uuid
from typing import Any, Optional, Union

from hub.models import ScheduledTask, ScheduleStatus, ScheduleType

logger = logging.getLogger("hub.scheduler")


def parse_cron_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field (minute, hour, day, month, dow) supporting *, */N, A-B, A,B."""
    values: set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            subparts = part.split("/")
            step = int(subparts[1])
            if subparts[0] == "*":
                start, end = min_val, max_val
            elif "-" in subparts[0]:
                start, end = map(int, subparts[0].split("-"))
            else:
                start, end = int(subparts[0]), max_val
            values.update(range(start, end + 1, step))
        elif "-" in part:
            start, end = map(int, part.split("-"))
            values.update(range(start, end + 1))
        elif part == "*":
            values.update(range(min_val, max_val + 1))
        else:
            values.add(int(part))
    return values


def compute_next_cron_run(
    cron_expr: str,
    start_dt: Optional[datetime.datetime] = None,
    tz_name: str = "Asia/Bangkok",
) -> datetime.datetime:
    """
    Calculate the next occurrence datetime (in UTC) for a standard 5-part cron expression.
    Granularity is minute-level. Evaluates forward up to 366 days.
    """
    if start_dt is None:
        start_dt = datetime.datetime.now(datetime.timezone.utc)
    elif start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)

    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron expression: '{cron_expr}'. Must have 5 fields.")

    minutes = parse_cron_field(parts[0], 0, 59)
    hours = parse_cron_field(parts[1], 0, 23)
    days = parse_cron_field(parts[2], 1, 31)
    months = parse_cron_field(parts[3], 1, 12)

    # Convert cron DOW (0=Sun, 6=Sat, 7=Sun) to Python weekday (0=Mon, 6=Sun)
    raw_dow = parse_cron_field(parts[4], 0, 7)
    py_dow: set[int] = set()
    for d in raw_dow:
        if d in (0, 7):
            py_dow.add(6)  # Sunday
        else:
            py_dow.add(d - 1)

    curr = start_dt.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    # Search forward minute-by-minute with smart date skipping
    for _ in range(366 * 1440):
        if curr.month not in months:
            # Skip to first day of next month
            curr = (curr.replace(day=1, hour=0, minute=0) + datetime.timedelta(days=32)).replace(day=1)
            continue
        if curr.day not in days or curr.weekday() not in py_dow:
            # Skip to start of next day
            curr = curr.replace(hour=0, minute=0) + datetime.timedelta(days=1)
            continue
        if curr.hour not in hours:
            # Skip to start of next hour
            curr = curr.replace(minute=0) + datetime.timedelta(hours=1)
            continue
        if curr.minute not in minutes:
            curr += datetime.timedelta(minutes=1)
            continue
        return curr

    raise ValueError(f"No matching cron time found within 1 year for: '{cron_expr}'")


def parse_schedule_time(
    scheduled_at: Optional[Any] = None,
    delay_seconds: Optional[Union[int, float]] = None,
    delay: Optional[str] = None,
    base_dt: Optional[datetime.datetime] = None,
) -> datetime.datetime:
    """
    Parse a target execution time from either:
    1. Explicit delay_seconds (e.g. 172800)
    2. String delay format (e.g. "2d", "12h", "30m", "45s")
    3. ISO timestamp string (e.g. "2026-09-23T16:18:00+08:00" or "2026-09-23 16:18:00")
    4. Epoch seconds (int or float)
    Returns UTC datetime object.
    """
    if base_dt is None:
        base_dt = datetime.datetime.now(datetime.timezone.utc)
    elif base_dt.tzinfo is None:
        base_dt = base_dt.replace(tzinfo=datetime.timezone.utc)

    # 1. Direct delay_seconds
    if delay_seconds is not None:
        try:
            sec_val = float(delay_seconds)
            return base_dt + datetime.timedelta(seconds=max(0.0, sec_val))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid delay_seconds value: {delay_seconds}") from e

    # 2. String delay (e.g. "2d", "4h", "15m", "60s", "+2d")
    if delay is not None:
        if not isinstance(delay, str):
            raise ValueError(f"Expected delay string, got {type(delay).__name__}")
        clean_delay = delay.strip().lower().lstrip("+")
        match = re.match(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", clean_delay)
        if match:
            num = float(match.group(1))
            unit = match.group(2)
            multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
            total_sec = num * multipliers.get(unit, 1)
            return base_dt + datetime.timedelta(seconds=total_sec)
        raise ValueError(f"Invalid delay format: '{delay}'. Expected format like '30s', '15m', '2h', '2d', or '1w'.")

    # 3. Explicit scheduled_at string or number
    if scheduled_at is not None:
        if isinstance(scheduled_at, (int, float)):
            return datetime.datetime.fromtimestamp(float(scheduled_at), tz=datetime.timezone.utc)

        str_val = str(scheduled_at).strip()
        if not str_val:
            raise ValueError("scheduled_at cannot be empty")

        # Check if relative format passed inside scheduled_at like "+2d", "2d", "1h"
        clean_rel = str_val.lower().lstrip("+")
        if re.match(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", clean_rel):
            return parse_schedule_time(delay=clean_rel, base_dt=base_dt)

        # Standard ISO 8601 parsing
        iso_clean = str_val.replace(" ", "T")
        try:
            parsed_dt = datetime.datetime.fromisoformat(iso_clean)
            if parsed_dt.tzinfo is None:
                # Local assumption: Asia/Bangkok / +08:00 if timezone not explicitly specified
                parsed_dt = parsed_dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            return parsed_dt.astimezone(datetime.timezone.utc)
        except ValueError:
            pass

        # Formats commonly used across Spark, webhooks, and local inputs
        date_patterns = [
            ("%Y-%m-%d %H:%M:%S", False),
            ("%Y-%m-%d %H:%M", False),
            ("%Y-%m-%d", True),
            ("%Y/%m/%d %H:%M:%S", False),
            ("%Y/%m/%d %H:%M", False),
            ("%Y/%m/%d", True),
            ("%d/%m/%Y %H:%M:%S", False),
            ("%d/%m/%Y %H:%M", False),
            ("%d/%m/%Y", True),
        ]
        for pattern, is_date_only in date_patterns:
            try:
                parsed_dt = datetime.datetime.strptime(str_val, pattern)
                if is_date_only:
                    parsed_dt = parsed_dt.replace(hour=9, minute=0, second=0)
                parsed_dt = parsed_dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
                return parsed_dt.astimezone(datetime.timezone.utc)
            except ValueError:
                continue

        raise ValueError(
            f"Invalid scheduled_at format: '{str_val}'. "
            "Supported formats: ISO-8601 (2026-09-23T16:18:00Z), YYYY-MM-DD, DD/MM/YYYY, or relative delays (2d, 1h, 30m)."
        )

    # Default fallback when no timing param specified: 60 seconds from now
    return base_dt + datetime.timedelta(seconds=60)


def format_iso_utc(dt: datetime.datetime) -> str:
    """Format datetime as UTC ISO-8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskScheduler:
    """
    Decoupled Async Task Scheduler Engine.
    Periodically checks SQLite SSOT for due delayed tasks and recurring cron triggers.
    Ensures zero missed beats by detecting system sleep leaps and daemon restarts.
    """

    def __init__(
        self,
        db: Any,
        dispatcher: Any,
        broker: Optional[Any] = None,
        config: Optional[Any] = None,
        tick_interval: float = 1.0,
    ):
        self.db = db
        self.dispatcher = dispatcher
        self.broker = broker
        self.config = config
        self.tick_interval = float(tick_interval)
        self._loop_task: Optional[asyncio.Task] = None
        self._is_running: bool = False
        self._last_tick_monotonic: float = 0.0
        self._missed_beats_caught_up: int = 0
        self._total_triggers_count: int = 0
        self._last_sleep_recovery_at: Optional[str] = None

    async def start(self) -> None:
        """Start the async scheduler background loop and perform boot-time catch-up."""
        if self._is_running:
            return

        self._is_running = True
        self._last_tick_monotonic = time.monotonic()
        logger.info("TaskScheduler starting background loop (tick interval: %.1fs)", self.tick_interval)

        # Boot-time catch-up: Trigger any overdue tasks that matured while the daemon was offline
        try:
            boot_res = await self.sweep_due(reason="boot_startup")
            if boot_res.get("triggered_count", 0) > 0:
                logger.info(
                    "TaskScheduler: Boot-time catch-up triggered %d overdue schedules!",
                    boot_res["triggered_count"],
                )
        except Exception as e:
            logger.warning("Boot-time schedule catch-up error: %s", e)

        self._loop_task = asyncio.create_task(self._scheduler_loop())

    async def stop(self) -> None:
        """Stop background scheduler loop gracefully."""
        self._is_running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        logger.info("TaskScheduler stopped")

    async def _scheduler_loop(self) -> None:
        """
        Continuous scheduler monitor ticking every tick_interval seconds.
        Detects monotonic clock leap across system sleep, immediately catching up
        overdue tasks ("Don't miss the beat!").
        """
        while self._is_running:
            t0 = time.monotonic()
            try:
                await asyncio.sleep(self.tick_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                continue

            if not self._is_running:
                break

            elapsed = time.monotonic() - t0
            # Sleep/wake drift detection:
            # On macOS Darwin, monotonic clock ticks across sleep.
            # If sleep lasted > tick_interval + 10s, system was suspended!
            is_sleep_recovery = elapsed > (self.tick_interval + 10.0)
            if is_sleep_recovery:
                logger.warning(
                    "TaskScheduler: macOS sleep/wake detected (elapsed: %.1fs > expected %.1fs). Catching up due schedules!",
                    elapsed, self.tick_interval
                )
                self._last_sleep_recovery_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

            try:
                await self.sweep_due(
                    reason="sleep_wake_recovery" if is_sleep_recovery else "periodic_tick",
                    sleep_duration=elapsed if is_sleep_recovery else 0.0,
                )
            except Exception as sweep_err:
                logger.error("TaskScheduler: error during schedule sweep: %s", sweep_err)

    async def sweep_due(
        self,
        reason: str = "manual",
        sleep_duration: float = 0.0,
        as_of_iso: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Authoritative sweep of SQLite SSOT for due schedules.
        Dispatches tasks, updates last_run_at / next_run_at, and broadcasts SSE events.
        """
        if as_of_iso is None:
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            as_of_iso = format_iso_utc(now_dt)
        else:
            now_dt = datetime.datetime.fromisoformat(as_of_iso.replace("Z", "+00:00"))

        due_schedules = []
        if hasattr(self.db, "get_due_schedules"):
            res = self.db.get_due_schedules(as_of_iso)
            due_schedules = await res if asyncio.iscoroutine(res) else res

        triggered: list[dict[str, Any]] = []

        for sched in due_schedules:
            try:
                trigger_res = await self._execute_schedule_trigger(sched, now_dt, reason=reason)
                if trigger_res:
                    triggered.append(trigger_res)
                    self._total_triggers_count += 1
            except Exception as e:
                logger.error("Failed executing schedule %s: %s", sched.get("schedule_id"), e)

        summary = {
            "reason": reason,
            "sleep_duration": sleep_duration,
            "as_of": as_of_iso,
            "triggered_count": len(triggered),
            "triggered": triggered,
            "timestamp": time.time(),
        }

        if triggered and self.broker:
            try:
                await self.broker.publish("events", {
                    "event": "schedules_swept",
                    "type": "schedules_swept",
                    "reason": reason,
                    "triggered_count": len(triggered),
                    "timestamp": time.time(),
                })
            except Exception as b_err:
                logger.debug("Failed publishing schedules_swept to broker: %s", b_err)

        return summary

    async def _execute_schedule_trigger(
        self,
        sched: dict[str, Any],
        now_dt: datetime.datetime,
        reason: str = "scheduled_tick",
    ) -> Optional[dict[str, Any]]:
        """Trigger an individual due schedule: insert Task into SSOT and enqueue to Dispatcher."""
        schedule_id = sched["schedule_id"]
        schedule_type = sched.get("schedule_type", "once")
        action_type = sched.get("action_type", "cli")
        command = sched.get("command") or sched.get("target_action") or ""
        target_action = sched.get("target_action") or command
        action_params = sched.get("action_params") or {}
        if isinstance(action_params, str):
            try:
                action_params = json.loads(action_params)
            except Exception:
                action_params = {}

        # Add schedule metadata into action_params
        action_params["schedule_id"] = schedule_id
        action_params["schedule_name"] = sched.get("name")
        action_params["trigger_reason"] = reason

        # Create concrete task and event IDs
        task_id = f"tsk_sch_{uuid.uuid4().hex[:12]}"
        event_id = f"evt_sch_{uuid.uuid4().hex[:12]}"

        # 1. Insert synthetic event record
        event_record = {
            "event_id": event_id,
            "source": sched.get("source", "scheduler"),
            "idempotency_key": f"{schedule_id}_{now_dt.strftime('%Y%m%d%H%M%S')}_{task_id}",
            "payload_hash": "",
            "headers_json": json.dumps({"X-Hub-Source": "scheduler", "X-Schedule-ID": schedule_id}),
            "raw_payload": json.dumps(action_params),
            "method": "POST",
            "path": f"/schedules/{schedule_id}/trigger",
            "status": "received",
        }
        if hasattr(self.db, "insert_webhook_event"):
            res = self.db.insert_webhook_event(event_record)
            if asyncio.iscoroutine(res):
                await res

        # 2. Insert task record linked to schedule
        task_record = {
            "task_id": task_id,
            "event_id": event_id,
            "source": sched.get("source", "scheduler"),
            "action_type": action_type,
            "command": command,
            "target_action": target_action,
            "action_params_json": json.dumps(action_params),
            "status": "queued",
            "priority": int(sched.get("priority", 0) if "priority" in sched else 0),
            "timeout_seconds": int(sched.get("timeout_seconds", 300) if "timeout_seconds" in sched else 300),
            "retry_count": 0,
            "max_retries": 0,
            "schedule_id": schedule_id,
            "scheduled_at": sched.get("scheduled_at") or sched.get("next_run_at"),
        }
        if hasattr(self.db, "insert_task"):
            res = self.db.insert_task(task_record)
            if asyncio.iscoroutine(res):
                await res

        # 3. Determine next state for schedule
        next_run_iso = None
        new_status = None

        if schedule_type == "once":
            # One-off delayed task completes on execution
            new_status = ScheduleStatus.COMPLETED.value
        elif schedule_type == "recurring":
            cron_expr = sched.get("cron_expression")
            if cron_expr:
                try:
                    next_dt = compute_next_cron_run(
                        cron_expr,
                        start_dt=now_dt,
                        tz_name=sched.get("timezone", "Asia/Bangkok"),
                    )
                    next_run_iso = format_iso_utc(next_dt)
                    new_status = ScheduleStatus.ACTIVE.value
                except Exception as cron_err:
                    logger.error("Failed computing next cron run for %s: %s", schedule_id, cron_err)
                    new_status = ScheduleStatus.FAILED.value
            else:
                new_status = ScheduleStatus.COMPLETED.value

        # Check max_runs constraint if defined
        max_runs = sched.get("max_runs")
        current_runs = int(sched.get("total_runs", 0)) + 1
        if max_runs and current_runs >= int(max_runs):
            new_status = ScheduleStatus.COMPLETED.value

        # 4. Record execution in DB
        if hasattr(self.db, "record_schedule_execution"):
            res = self.db.record_schedule_execution(
                schedule_id=schedule_id,
                task_id=task_id,
                next_run_at=next_run_iso,
                status=new_status,
            )
            if asyncio.iscoroutine(res):
                await res

        # 5. Enqueue task for execution in Dispatcher
        if self.dispatcher:
            await self.dispatcher.enqueue(task_id)

        # 6. Publish real-time events to SSE
        if self.broker:
            evt_data = {
                "event": "schedule_triggered",
                "type": "schedule_triggered",
                "schedule_id": schedule_id,
                "schedule_name": sched.get("name"),
                "task_id": task_id,
                "action_type": action_type,
                "status": new_status or "active",
                "next_run_at": next_run_iso,
                "total_runs": current_runs,
                "timestamp": time.time(),
            }
            try:
                await self.broker.publish("events", evt_data)
            except Exception as b_err:
                logger.debug("Failed publishing schedule_triggered: %s", b_err)

        logger.info(
            "Schedule %s ('%s') triggered -> Task %s [action: %s, next: %s, status: %s]",
            schedule_id, sched.get("name"), task_id, action_type, next_run_iso, new_status
        )

        return {
            "schedule_id": schedule_id,
            "name": sched.get("name"),
            "task_id": task_id,
            "action_type": action_type,
            "next_run_at": next_run_iso,
            "status": new_status,
        }

    async def trigger_now(self, schedule_id: str) -> Optional[dict[str, Any]]:
        """Trigger an immediate, on-demand execution of a schedule, bypassing schedule timer."""
        sched = self.db.get_schedule(schedule_id)
        if asyncio.iscoroutine(sched):
            sched = await sched

        if not sched:
            return None

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        return await self._execute_schedule_trigger(sched, now_dt, reason="manual_trigger_now")

    async def create_schedule(
        self,
        name: str,
        schedule_type: str = "once",
        scheduled_at: Optional[Any] = None,
        delay_seconds: Optional[Union[int, float]] = None,
        delay: Optional[str] = None,
        cron_expression: Optional[str] = None,
        action_type: str = "cli",
        command: Optional[str] = None,
        target_action: Optional[str] = None,
        action_params: Optional[dict[str, Any]] = None,
        source: str = "scheduler",
        timezone: str = "Asia/Bangkok",
        max_runs: Optional[int] = None,
    ) -> ScheduledTask:
        """
        Create and persist a new schedule to SQLite SSOT.
        Calculates authoritative next_run_at and broadcasts creation event.
        """
        schedule_id = f"sch_{uuid.uuid4().hex[:16]}"
        now_dt = datetime.datetime.now(datetime.timezone.utc)

        if schedule_type == "recurring":
            if not cron_expression:
                raise ValueError("cron_expression is required for recurring schedule")
            next_dt = compute_next_cron_run(cron_expression, start_dt=now_dt, tz_name=timezone)
            next_run_iso = format_iso_utc(next_dt)
            scheduled_at_iso = None
        else:
            target_dt = parse_schedule_time(
                scheduled_at=scheduled_at,
                delay_seconds=delay_seconds,
                delay=delay,
                base_dt=now_dt,
            )
            next_run_iso = format_iso_utc(target_dt)
            scheduled_at_iso = next_run_iso
            max_runs = 1

        sched_obj = ScheduledTask(
            schedule_id=schedule_id,
            name=name,
            source=source,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            scheduled_at=scheduled_at_iso,
            next_run_at=next_run_iso,
            last_run_at=None,
            timezone=timezone,
            action_type=action_type,
            command=command,
            target_action=target_action or command,
            action_params=action_params or {},
            status=ScheduleStatus.ACTIVE.value,
            max_runs=max_runs,
            total_runs=0,
            created_at=time.time(),
            updated_at=time.time(),
        )

        if hasattr(self.db, "insert_schedule"):
            res = self.db.insert_schedule(sched_obj.to_dict())
            if asyncio.iscoroutine(res):
                await res

        if self.broker:
            try:
                await self.broker.publish("events", {
                    "event": "schedule_created",
                    "type": "schedule_created",
                    "schedule_id": schedule_id,
                    "name": name,
                    "schedule_type": schedule_type,
                    "next_run_at": next_run_iso,
                    "cron_expression": cron_expression,
                    "timestamp": time.time(),
                })
            except Exception as b_err:
                logger.debug("Failed publishing schedule_created: %s", b_err)

        logger.info("Created schedule %s ('%s') [type: %s, next: %s]", schedule_id, name, schedule_type, next_run_iso)
        return sched_obj

    async def pause_schedule(self, schedule_id: str) -> bool:
        """Pause an active schedule."""
        if hasattr(self.db, "update_schedule_status"):
            res = self.db.update_schedule_status(schedule_id, ScheduleStatus.PAUSED.value)
            success = await res if asyncio.iscoroutine(res) else res
            if success and self.broker:
                try:
                    await self.broker.publish("events", {
                        "event": "schedule_paused",
                        "type": "schedule_paused",
                        "schedule_id": schedule_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
            return bool(success)
        return False

    async def resume_schedule(self, schedule_id: str) -> bool:
        """Resume a paused schedule, recalculating next_run_at if it's recurring."""
        sched = self.db.get_schedule(schedule_id)
        if asyncio.iscoroutine(sched):
            sched = await sched

        if not sched:
            return False

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        updates: dict[str, Any] = {"status": ScheduleStatus.ACTIVE.value}

        if sched.get("schedule_type") == "recurring" and sched.get("cron_expression"):
            try:
                next_dt = compute_next_cron_run(sched["cron_expression"], start_dt=now_dt)
                updates["next_run_at"] = format_iso_utc(next_dt)
            except Exception as e:
                logger.warning("Failed recalculating next run for resumed schedule %s: %s", schedule_id, e)

        if hasattr(self.db, "update_schedule"):
            res = self.db.update_schedule(schedule_id, updates)
            success = await res if asyncio.iscoroutine(res) else res
            if success and self.broker:
                try:
                    await self.broker.publish("events", {
                        "event": "schedule_resumed",
                        "type": "schedule_resumed",
                        "schedule_id": schedule_id,
                        "next_run_at": updates.get("next_run_at"),
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
            return bool(success)
        return False

    async def cancel_schedule(self, schedule_id: str) -> bool:
        """Cancel an active or paused schedule."""
        if hasattr(self.db, "update_schedule_status"):
            res = self.db.update_schedule_status(schedule_id, ScheduleStatus.CANCELLED.value)
            success = await res if asyncio.iscoroutine(res) else res
            if success and self.broker:
                try:
                    await self.broker.publish("events", {
                        "event": "schedule_cancelled",
                        "type": "schedule_cancelled",
                        "schedule_id": schedule_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
            return bool(success)
        return False
