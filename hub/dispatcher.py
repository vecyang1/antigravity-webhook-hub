"""
Antigravity Webhook Hub — Async Task Dispatcher Engine
Decoupled async worker queue executing tasks without blocking HTTP response.
Supports multi-target dispatch:
- cli_command / cli: process group isolation (os.setsid), environment injection,
  timeout handling via os.killpg, streaming stdout/stderr line capture.
- launchd_job / launchd: macOS launchctl kickstart.
- cron_job / cron: periodic task runner.
- agent_signal: Antigravity AI agent atomic signal JSON drop and trigger.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hub.memory import apply_memory_pressure_relief

logger = logging.getLogger("hub.dispatcher")


@dataclass(slots=True)
class ExecutionResult:
    """Result of a dispatched task execution attempt."""

    task_id: str
    status: str  # 'succeeded', 'failed', 'timed_out'
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error_message: Optional[str] = None
    result_data: Optional[dict[str, Any]] = None


class TaskDispatcher:
    """
    Decoupled Async Task Dispatcher.
    Consumes tasks from an in-memory queue and executes them against target runners.
    Supports concurrent worker loops configured via config.dispatch.max_concurrent_tasks.
    """

    def __init__(self, db: Any, broker: Optional[Any] = None, config: Optional[Any] = None):
        self.db = db
        self.broker = broker
        self.config = config
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._enqueued_task_ids: set[str] = set()
        self._worker_tasks: list[asyncio.Task] = []
        self._worker_task: Optional[asyncio.Task] = None
        self._sweeper_task: Optional[asyncio.Task] = None
        self._is_running: bool = False
        self._last_sweep_time: float = 0.0
        self._last_sweep_result: dict[str, Any] = {}
        self._sleep_recoveries_count: int = 0
        self._total_sweeps_count: int = 0
        self._last_sleep_detected_at: Optional[str] = None
        self._last_sleep_duration_seconds: float = 0.0

    async def start(self) -> None:
        """Start background task dispatcher workers, recover orphaned tasks, and launch unprocessed sweeper."""
        if hasattr(self.db, "recover_orphaned_tasks"):
            try:
                recovered = self.db.recover_orphaned_tasks()
                if asyncio.iscoroutine(recovered):
                    recovered = await recovered
                if recovered:
                    logger.info("Recovered %d orphaned tasks on dispatcher boot", recovered)
            except Exception as e:
                logger.warning("Crash recovery check failed: %s", e)

        # Initial auto-pick sweep on startup
        try:
            await self.sweep_unprocessed_tasks(reason="boot_startup")
        except Exception as e:
            logger.warning("Initial boot-time sweep failed: %s", e)

        # Rehydrate any remaining queued tasks into in-memory dispatch queue
        try:
            queued_tasks = []
            if hasattr(self.db, "get_queued_tasks"):
                res = self.db.get_queued_tasks()
                queued_tasks = await res if asyncio.iscoroutine(res) else res
            elif hasattr(self.db, "execute_read"):
                queued_tasks = await self.db.execute_read(
                    "SELECT task_id FROM tasks WHERE status = 'queued' ORDER BY priority DESC, created_at ASC"
                )

            if queued_tasks:
                for t in queued_tasks:
                    tid = t["task_id"] if isinstance(t, dict) else (getattr(t, "task_id", None) or str(t))
                    await self.enqueue(tid)
                logger.info("Rehydrated %d queued tasks on dispatcher boot", len(queued_tasks))
        except Exception as e:
            logger.warning("Failed to rehydrate queued tasks: %s", e)

        # Determine concurrency limit from config
        if isinstance(self.config, int):
            max_workers = self.config
        elif hasattr(self.config, "dispatch") and hasattr(self.config.dispatch, "max_concurrent_tasks"):
            max_workers = self.config.dispatch.max_concurrent_tasks
        elif hasattr(self.config, "max_concurrent_tasks"):
            max_workers = self.config.max_concurrent_tasks
        else:
            max_workers = 5
        max_workers = max(1, int(max_workers))

        self._is_running = True
        self._worker_tasks = [asyncio.create_task(self._worker_loop()) for _ in range(max_workers)]
        self._worker_task = self._worker_tasks[0] if self._worker_tasks else None
        logger.info("TaskDispatcher background workers started with %d concurrent loops", max_workers)

        # Launch background sweeper loop for sleep/wake detection & unprocessed task recovery
        sweeper_cfg = getattr(self.config, "sweeper", None)
        sweeper_enabled = getattr(sweeper_cfg, "enabled", True) if sweeper_cfg else True
        if sweeper_enabled:
            self._sweeper_task = asyncio.create_task(self._sweeper_loop())

    async def stop(self) -> None:
        """Gracefully stop background workers and sweeper loop."""
        self._is_running = False

        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sweeper_task = None

        if self._worker_tasks:
            for t in self._worker_tasks:
                t.cancel()
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()
        self._worker_task = None
        logger.info("TaskDispatcher background workers stopped")

    async def enqueue(self, task_id: str) -> None:
        """Enqueue task_id for asynchronous background execution with duplicate prevention."""
        if task_id not in self._enqueued_task_ids:
            self._enqueued_task_ids.add(task_id)
            await self.queue.put(task_id)

    def _apply_pressure_relief(self) -> None:
        """Trigger macOS malloc zone pressure relief to keep process RSS strictly < 30MB."""
        apply_memory_pressure_relief()

    async def _worker_loop(self) -> None:
        """Continuous consumer loop dequeuing tasks from queue."""
        while self._is_running:
            try:
                task_id = await self.queue.get()
                try:
                    await self.execute_task(task_id)
                except Exception as task_err:
                    logger.exception("Error executing task %s: %s", task_id, task_err)
                finally:
                    self._enqueued_task_ids.discard(task_id)
                    self.queue.task_done()
                    self._apply_pressure_relief()
                    self._apply_pressure_relief()
                    if hasattr(self.db, "shrink_memory"):
                        try:
                            self.db.shrink_memory()
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                logger.exception("Error in dispatcher worker loop: %s", loop_err)
                await asyncio.sleep(0.1)

    async def sweep_unprocessed_tasks(
        self,
        reason: str = "manual",
        sleep_duration: float = 0.0,
        stale_running_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
        auto_retry_interrupted: Optional[bool] = None,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Auto-pick and recover unprocessed tasks across SQLite SSOT.
        Recovers:
        - Stale running tasks (cut off by system sleep, crash, or timeout)
        - Tasks created in 'received' status
        - Orphaned webhook_events lacking tasks
        - Recoverable failed tasks (network/sleep interruptions)
        Re-queues them into the in-memory execution queue and emits live SSE updates.
        """
        sweeper_cfg = getattr(self.config, "sweeper", None)
        if stale_running_seconds is None:
            stale_running_seconds = getattr(sweeper_cfg, "stale_running_seconds", 300) if sweeper_cfg else 300
        if max_retries is None:
            max_retries = getattr(sweeper_cfg, "max_auto_retries", 3) if sweeper_cfg else 3
        if auto_retry_interrupted is None:
            auto_retry_interrupted = getattr(sweeper_cfg, "auto_retry_interrupted", True) if sweeper_cfg else True

        if sleep_duration > 0:
            self._sleep_recoveries_count += 1
            self._last_sleep_detected_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            self._last_sleep_duration_seconds = sleep_duration

        res = {}
        if hasattr(self.db, "sweep_and_requeue_unprocessed"):
            try:
                db_res = self.db.sweep_and_requeue_unprocessed(
                    stale_running_seconds=stale_running_seconds,
                    max_retries=max_retries,
                    auto_retry_interrupted=auto_retry_interrupted,
                    source=source,
                    limit=limit,
                )
                if asyncio.iscoroutine(db_res):
                    res = await db_res
                else:
                    res = db_res
            except Exception as e:
                logger.error("Error executing DB sweep_and_requeue_unprocessed: %s", e)
                res = {"error": str(e), "task_ids": []}

        task_ids = res.get("task_ids", [])
        enqueued_count = 0
        for tid in task_ids:
            if tid not in self._enqueued_task_ids:
                await self.enqueue(tid)
                enqueued_count += 1

        self._last_sweep_time = time.time()
        self._total_sweeps_count += 1
        self._last_sweep_result = res

        summary = {
            "reason": reason,
            "sleep_duration": sleep_duration,
            "recovered_stale_running": res.get("recovered_stale_running", 0),
            "recovered_received_tasks": res.get("recovered_received_tasks", 0),
            "recovered_orphaned_events": res.get("recovered_orphaned_events", 0),
            "recovered_interrupted_failed": res.get("recovered_interrupted_failed", 0),
            "total_queued": res.get("total_queued", len(task_ids)),
            "newly_enqueued": enqueued_count,
            "task_ids": task_ids,
            "timestamp": self._last_sweep_time,
        }

        if self.broker:
            try:
                await self.broker.publish("sweeper_run", summary)
                # Also publish to 'events' topic so /events/stream subscribers receive it
                sweeper_evt = dict(summary)
                sweeper_evt["event"] = "sweeper_run"
                sweeper_evt["type"] = "sweeper_run"
                await self.broker.publish("events", sweeper_evt)
            except Exception as broker_err:
                logger.debug("Failed to publish sweeper_run to broker: %s", broker_err)

        if enqueued_count > 0 or res.get("recovered_stale_running", 0) > 0:
            logger.info(
                "Auto-picked %d unprocessed tasks into queue (reason: %s, sleep_duration: %.1fs)",
                enqueued_count, reason, sleep_duration
            )

        gc.collect()
        self._apply_pressure_relief()
        if hasattr(self.db, "shrink_memory"):
            try:
                self.db.shrink_memory(truncate_wal=False)
            except Exception:
                pass

        return summary

    async def _sweeper_loop(self) -> None:
        """
        Background monitor for automatic unprocessed task pickup and macOS sleep/wake detection.
        Ticks every tick_seconds (default 5.0s).
        - If elapsed wall-clock/monotonic duration > tick_seconds + sleep_drift_threshold,
          macOS sleep/wake is detected, triggering immediate auto-pick recovery!
        - Periodically (every interval_seconds), performs background sweep for pending tasks.
        """
        sweeper_cfg = getattr(self.config, "sweeper", None)
        interval = getattr(sweeper_cfg, "interval_seconds", 30) if sweeper_cfg else 30
        drift_threshold = getattr(sweeper_cfg, "sleep_drift_threshold_seconds", 15) if sweeper_cfg else 15
        tick_seconds = min(5.0, max(1.0, float(interval) / 6.0))
        last_periodic_sweep = time.time()

        logger.info(
            "Unprocessed message sweeper loop active (interval: %ds, sleep_threshold: %ds)",
            interval, drift_threshold
        )

        while self._is_running:
            t0 = time.monotonic()
            try:
                await asyncio.sleep(tick_seconds)
            except asyncio.CancelledError:
                break
            except Exception:
                continue

            if not self._is_running:
                break

            elapsed = time.monotonic() - t0
            # Detect macOS sleep/wake leap:
            # On macOS Darwin, monotonic time continues across system sleep (mach_continuous_time).
            # When macOS sleeps for 3 hours, asyncio.sleep(5) wakes up with elapsed ~10800 seconds!
            if elapsed > (tick_seconds + drift_threshold):
                logger.warning(
                    "macOS sleep/wake detected: elapsed %.1fs > expected %.1fs (system was asleep). Auto-picking unprocessed messages!",
                    elapsed, tick_seconds
                )
                try:
                    await self.sweep_unprocessed_tasks(
                        reason="sleep_wake_recovery",
                        sleep_duration=elapsed,
                    )
                    last_periodic_sweep = time.time()
                except Exception as e:
                    logger.error("Error during sleep recovery sweep: %s", e)
            elif (time.time() - last_periodic_sweep) >= interval:
                try:
                    await self.sweep_unprocessed_tasks(reason="periodic_sweep")
                    last_periodic_sweep = time.time()
                except Exception as e:
                    logger.error("Error during periodic sweep: %s", e)
                await asyncio.sleep(0.1)

    def _record_log(self, task_id: str, stream: str, chunk: str, execution_id: Optional[str] = None) -> None:
        """Persist output chunk to database."""
        if hasattr(self.db, "insert_log_chunk"):
            try:
                self.db.insert_log_chunk(task_id, stream, chunk, execution_id=execution_id)
            except TypeError:
                try:
                    self.db.insert_log_chunk(task_id, stream, chunk)
                except Exception as e:
                    logger.debug("Failed to insert log chunk: %s", e)
            except Exception as e:
                logger.debug("Failed to insert log chunk: %s", e)
        elif hasattr(self.db, "insert_execution_log"):
            try:
                self.db.insert_execution_log(task_id, stream, chunk, execution_id=execution_id)
            except TypeError:
                try:
                    self.db.insert_execution_log(task_id, stream, chunk)
                except Exception as e:
                    logger.debug("Failed to insert execution log: %s", e)
            except Exception as e:
                logger.debug("Failed to insert execution log: %s", e)

    async def _broadcast_log(self, task_id: str, stream: str, chunk: str) -> None:
        """Forward output line to pubsub broker if registered."""
        if not self.broker:
            return
        payload = {
            "type": "log",
            "task_id": task_id,
            "stream": stream,
            "chunk": chunk,
            "timestamp": time.time(),
        }
        if hasattr(self.broker, "publish_log"):
            try:
                await self.broker.publish_log(task_id, stream, chunk)
            except Exception:
                pass
        elif hasattr(self.broker, "publish_event"):
            try:
                await self.broker.publish_event(task_id, "log", payload)
            except Exception:
                pass

    async def _broadcast_status(self, task_id: str, status: str, exit_code: Optional[int] = None, error_message: Optional[str] = None) -> None:
        """Broadcast state transition to broker."""
        if not self.broker:
            return
        payload = {
            "event": "status_changed" if status not in ("succeeded", "failed", "timed_out") else "completed",
            "type": "status_changed",
            "task_id": task_id,
            "status": status,
            "exit_code": exit_code,
            "error_message": error_message,
            "timestamp": time.time(),
        }
        if hasattr(self.broker, "publish_status"):
            try:
                await self.broker.publish_status(task_id, status, exit_code)
            except Exception:
                pass
        elif hasattr(self.broker, "publish_event"):
            try:
                await self.broker.publish_event(task_id, "status_changed", payload)
            except Exception:
                pass
        try:
            await self.broker.publish("events", payload)
        except Exception:
            pass

    async def execute_task(self, task_id: str) -> ExecutionResult:
        """
        Execute a task through atomic CAS state progression:
        received/queued -> running -> succeeded/failed/timed_out.
        """
        # 1. Fetch task record
        task_data = self.db.get_task(task_id)
        if asyncio.iscoroutine(task_data):
            task_data = await task_data

        if not task_data:
            return ExecutionResult(
                task_id=task_id,
                status="failed",
                exit_code=-1,
                error_message=f"Task {task_id} not found in database",
            )

        current_status = task_data.get("status", "received")
        if current_status in ("succeeded", "failed", "timed_out", "cancelled"):
            return ExecutionResult(
                task_id=task_id,
                status=current_status,
                exit_code=task_data.get("exit_code"),
                error_message=task_data.get("error_message"),
            )

        # 2. Atomic CAS transition to 'running'
        cas_success = self.db.update_task_status_cas(
            task_id,
            expected_status=current_status,
            new_status="running",
        )
        if not cas_success:
            task_latest = self.db.get_task(task_id)
            if asyncio.iscoroutine(task_latest):
                task_latest = await task_latest
            latest_status = task_latest.get("status", "failed") if task_latest else "failed"
            return ExecutionResult(
                task_id=task_id,
                status=latest_status,
                exit_code=task_latest.get("exit_code") if task_latest else None,
                error_message="CAS state transition to running failed (concurrent update)",
            )

        execution_id = f"exec_{uuid.uuid4().hex[:16]}"
        attempt_number = int(task_data.get("retry_count", 0)) + 1
        if hasattr(self.db, "insert_execution"):
            try:
                exec_record = {
                    "execution_id": execution_id,
                    "task_id": task_id,
                    "attempt_number": attempt_number,
                    "pid": None,
                    "status": "running",
                }
                res = self.db.insert_execution(exec_record)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.warning("Failed to insert execution record: %s", e)

        await self._broadcast_status(task_id, "running")

        # 3. Route execution by action_type
        action_type = (task_data.get("action_type") or "cli").lower().strip()
        timeout_seconds = int(task_data.get("timeout_seconds") or 300)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        exit_code: Optional[int] = None
        error_message: Optional[str] = None
        final_status = "failed"
        task_result_data: Optional[dict[str, Any]] = None

        try:
            # Parse action_params from action_params_json or action_params dict
            action_params: dict[str, Any] = {}
            if task_data.get("action_params_json"):
                try:
                    action_params = json.loads(task_data["action_params_json"])
                except Exception:
                    pass
            elif isinstance(task_data.get("action_params"), dict):
                action_params = task_data["action_params"]

            if action_type in ("agent_signal", "signal"):
                # Antigravity AI agent task signal dispatch
                signals_dir_val = task_data.get("target_action")
                if signals_dir_val and Path(signals_dir_val).is_dir() or signals_dir_val and not str(signals_dir_val).endswith(".json"):
                    signals_dir = Path(signals_dir_val)
                elif signals_dir_val and str(signals_dir_val).endswith(".json"):
                    signals_dir = Path(signals_dir_val).parent
                else:
                    signals_dir = Path(".agents/signals")

                signals_dir.mkdir(parents=True, exist_ok=True)

                prompt_val = action_params.get("prompt") or task_data.get("prompt") or task_data.get("command") or task_id
                signal_content = {
                    "signal_version": "1.0",
                    "signal_id": f"sig_{uuid.uuid4().hex[:12]}",
                    "task_id": task_id,
                    "event_id": task_data.get("event_id", ""),
                    "source": task_data.get("source", "default"),
                    "action": task_data.get("action", action_type),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "prompt": prompt_val,
                    "payload": action_params.get("payload") or task_data.get("payload", {}),
                }

                tmp_path = signals_dir / f"{task_id}.tmp"
                final_path = signals_dir / f"{task_id}.signal.json"
                tmp_path.write_text(json.dumps(signal_content, indent=2), encoding="utf-8")
                tmp_path.replace(final_path)

                stdout_lines.append(f"Emitted agent signal: {final_path}")
                self._record_log(task_id, "stdout", stdout_lines[-1], execution_id=execution_id)
                final_status = "succeeded"
            elif action_type in ("contact_review", "review_contact", "contact-review"):
                # Antigravity Contact Review native in-process dispatch with live SSE and DB streaming
                from hub.contact_review.runner import execute_contact_review

                raw_params = task_data.get("action_params_json") or task_data.get("action_params")
                if isinstance(raw_params, str):
                    try:
                        params_dict = json.loads(raw_params)
                    except Exception:
                        params_dict = {}
                elif isinstance(raw_params, dict):
                    params_dict = raw_params
                else:
                    params_dict = {}

                def log_fn(msg: str):
                    stdout_lines.append(msg)
                    self._record_log(task_id, "stdout", msg, execution_id=execution_id)
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self._broadcast_log(task_id, "stdout", msg))
                    except Exception:
                        pass

                is_dry_run = bool(params_dict.get("dry_run") or params_dict.get("no_apply"))
                is_auto_apply = bool(params_dict.get("auto_apply", not is_dry_run))
                is_notify_slack = bool(params_dict.get("notify_slack", not is_dry_run and not params_dict.get("no_slack")))

                review_res = await execute_contact_review(
                    payload=params_dict,
                    auto_apply=is_auto_apply,
                    notify_slack=is_notify_slack,
                    dry_run=is_dry_run,
                    log_callback=log_fn,
                )
                task_result_data = review_res.to_dict()

                if review_res.error:
                    stderr_lines.append(review_res.error)
                    self._record_log(task_id, "stderr", review_res.error, execution_id=execution_id)
                    final_status = "failed"
                    exit_code = 1
                    error_message = review_res.error
                else:
                    summary_msg = (
                        f"Verdict: {review_res.verdict.value.upper()} | "
                        f"Target: {review_res.target_name} | "
                        f"Page: {review_res.target_page_url} | "
                        f"SSOT: {'Verified' if review_res.ssot_verified else 'Unverified'}"
                    )
                    stdout_lines.append(summary_msg)
                    self._record_log(task_id, "stdout", summary_msg, execution_id=execution_id)
                    final_status = "succeeded"
                    exit_code = 0

            elif action_type in ("antigravity", "agent_conversation", "antigravity_task"):
                # Google Antigravity Agent native dispatch with thread linking and Slack milestone notifications
                from hub.antigravity.session_manager import execute_antigravity_task

                def log_fn(msg: str):
                    stdout_lines.append(msg)
                    self._record_log(task_id, "stdout", msg, execution_id=execution_id)
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self._broadcast_log(task_id, "stdout", msg))
                    except Exception:
                        pass

                antigravity_res = await execute_antigravity_task(
                    task_data=task_data,
                    db=self.db,
                    log_callback=log_fn,
                )
                task_result_data = antigravity_res

                if not antigravity_res.get("success"):
                    err_txt = antigravity_res.get("error") or "Antigravity task execution failed"
                    stderr_lines.append(err_txt)
                    self._record_log(task_id, "stderr", err_txt, execution_id=execution_id)
                    final_status = "failed"
                    exit_code = 1
                    error_message = err_txt
                else:
                    final_status = "succeeded"
                    exit_code = 0
                    convo_id = antigravity_res.get("conversation_id", "")
                    summary_msg = f"Antigravity conversation active: {convo_id}"
                    stdout_lines.append(summary_msg)
                    self._record_log(task_id, "stdout", summary_msg, execution_id=execution_id)

            else:
                # Subprocess execution: cli, cli_command, launchd_job, cron_job
                if action_type in ("launchd", "launchd_job"):
                    service = task_data.get("target_action") or task_data.get("command") or ""
                    uid = os.getuid() if hasattr(os, "getuid") else 501
                    cmd = f"launchctl kickstart -k gui/{uid}/{service}"
                else:
                    cmd = task_data.get("command") or task_data.get("target_action") or ""

                if not cmd:
                    raise ValueError(f"No executable command found for task {task_id}")

                # Environment setup with process metadata
                proc_env = dict(os.environ)
                proc_env["ANTIGRAVITY_TASK_ID"] = str(task_id)
                proc_env["ANTIGRAVITY_EVENT_ID"] = str(task_data.get("event_id") or "")
                proc_env["ANTIGRAVITY_SOURCE"] = str(task_data.get("source") or "")

                # Extra environment params from action_params_json if present
                cwd = None
                raw_params = task_data.get("action_params_json") or task_data.get("action_params")
                if raw_params:
                    if isinstance(raw_params, str):
                        try:
                            parsed_params = json.loads(raw_params)
                        except Exception:
                            parsed_params = {}
                    else:
                        parsed_params = raw_params

                    if isinstance(parsed_params, dict):
                        cwd = parsed_params.get("cwd")
                        if "env" in parsed_params and isinstance(parsed_params["env"], dict):
                            for k, v in parsed_params["env"].items():
                                proc_env[str(k)] = str(v)

                extra_kwargs: dict[str, Any] = {}
                if sys.platform != "win32":
                    if sys.version_info >= (3, 11):
                        extra_kwargs["process_group"] = 0
                    elif hasattr(os, "setsid"):
                        extra_kwargs["preexec_fn"] = os.setsid

                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=proc_env,
                    cwd=cwd,
                    **extra_kwargs,
                )

                # Immediately record spawned process PID in executions audit table
                if hasattr(self.db, "update_execution"):
                    try:
                        res = self.db.update_execution(
                            execution_id=execution_id,
                            status="running",
                            pid=proc.pid,
                        )
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as pid_err:
                        logger.debug("Failed to record execution pid %s: %s", proc.pid, pid_err)

                async def read_stream(reader: Optional[asyncio.StreamReader], stream_name: str, collector: list[str]) -> None:
                    if not reader:
                        return
                    while True:
                        line_bytes = await reader.readline()
                        if not line_bytes:
                            break
                        line_str = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                        collector.append(line_str)
                        self._record_log(task_id, stream_name, line_str, execution_id=execution_id)
                        await self._broadcast_log(task_id, stream_name, line_str)

                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            read_stream(proc.stdout, "stdout", stdout_lines),
                            read_stream(proc.stderr, "stderr", stderr_lines),
                            proc.wait(),
                        ),
                        timeout=float(timeout_seconds),
                    )
                    exit_code = proc.returncode
                    if exit_code == 0:
                        final_status = "succeeded"
                    else:
                        final_status = "failed"
                        error_message = f"Process exited with non-zero code {exit_code}"

                except asyncio.TimeoutError:
                    # Cleanly kill the process group via os.killpg
                    final_status = "timed_out"
                    error_message = f"Process exceeded timeout of {timeout_seconds} seconds"
                    self._record_log(task_id, "system", error_message, execution_id=execution_id)

                    try:
                        if hasattr(os, "getpgid") and hasattr(os, "killpg"):
                            pgid = os.getpgid(proc.pid)
                            os.killpg(pgid, signal.SIGTERM)
                        else:
                            proc.terminate()
                    except Exception as kill_err:
                        logger.debug("SIGTERM error: %s", kill_err)

                    # Wait up to 1.5s for process group cleanup
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        try:
                            if hasattr(os, "getpgid") and hasattr(os, "killpg"):
                                pgid = os.getpgid(proc.pid)
                                os.killpg(pgid, signal.SIGKILL)
                            else:
                                proc.kill()
                        except Exception as sigkill_err:
                            logger.debug("SIGKILL error: %s", sigkill_err)
                        try:
                            await proc.wait()
                        except Exception:
                            pass

                    exit_code = proc.returncode
                finally:
                    if proc is not None:
                        try:
                            if proc.stdout and not proc.stdout.at_eof():
                                proc.stdout.feed_eof()
                        except Exception:
                            pass
                        try:
                            if proc.stderr and not proc.stderr.at_eof():
                                proc.stderr.feed_eof()
                        except Exception:
                            pass
                        try:
                            if hasattr(proc, "_transport") and proc._transport is not None:
                                proc._transport.close()
                        except Exception:
                            pass

        except Exception as exc:
            final_status = "failed"
            error_message = str(exc)
            logger.exception("Task %s failed: %s", task_id, exc)

        # Update execution audit record
        if hasattr(self.db, "update_execution"):
            try:
                exec_status = final_status
                if exec_status not in ("running", "succeeded", "failed", "timed_out", "killed"):
                    exec_status = "failed"
                proc_pid = getattr(proc, "pid", None) if "proc" in locals() and proc is not None else None
                res = self.db.update_execution(
                    execution_id=execution_id,
                    status=exec_status,
                    exit_code=exit_code,
                    stdout_tail="\n".join(stdout_lines[-50:]) if stdout_lines else "",
                    stderr_tail="\n".join(stderr_lines[-50:]) if stderr_lines else "",
                    error_message=error_message,
                    pid=proc_pid,
                )
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.warning("Failed to update execution record: %s", e)

        # 4. Atomic terminal CAS transition
        self.db.update_task_status_cas(
            task_id,
            expected_status="running",
            new_status=final_status,
            exit_code=exit_code,
            error_message=error_message,
        )

        # Keep webhook_events status synchronized with task completion
        event_id = task_data.get("event_id") if isinstance(task_data, dict) else None
        if event_id and hasattr(self.db, "update_event_status"):
            if final_status == "succeeded":
                try:
                    self.db.update_event_status(event_id, "processed")
                except Exception as ev_err:
                    logger.debug("Failed to update event status: %s", ev_err)

        await self._broadcast_status(task_id, final_status, exit_code, error_message)

        # Record observable activity in Antigravity sidecar_data store for sidebar visibility
        try:
            self._record_antigravity_sidebar_activity(
                task_id=task_id,
                task_data=task_data if isinstance(task_data, dict) else {},
                status=final_status,
                exit_code=exit_code,
                error_message=error_message,
                task_result_data=task_result_data,
            )
        except Exception as sidecar_err:
            logger.debug("Failed to record Antigravity sidecar activity: %s", sidecar_err)

        res_obj = ExecutionResult(
            task_id=task_id,
            status=final_status,
            exit_code=exit_code,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
            error_message=error_message,
            result_data=task_result_data,
        )

        # Reclaim memory buffers
        del stdout_lines
        del stderr_lines
        del task_data
        if "proc" in locals() and proc is not None:
            del proc
        gc.collect()
        self._apply_pressure_relief()
        if hasattr(self.db, "shrink_memory"):
            try:
                self.db.shrink_memory(truncate_wal=False)
            except Exception:
                pass
        elif hasattr(self.db, "_conn") and hasattr(self.db, "_lock"):
            try:
                with self.db._lock:
                    self.db._conn.execute("PRAGMA shrink_memory;")
            except Exception:
                pass

        return res_obj

    def _record_antigravity_sidebar_activity(
        self,
        task_id: str,
        task_data: dict[str, Any],
        status: str,
        exit_code: Optional[int] = None,
        error_message: Optional[str] = None,
        task_result_data: Optional[dict[str, Any]] = None,
    ) -> Optional[Path]:
        """
        Record observable activity event into Antigravity sidecar_data store
        so webhook-triggered activities render in the Antigravity sidebar (Scheduled Tasks view).
        """
        obs_cfg = getattr(self.config, "observability", None)
        enabled = getattr(obs_cfg, "enabled", True) if obs_cfg else True
        emit = getattr(obs_cfg, "emit_sidecar_events", True) if obs_cfg else True
        if not (enabled and emit):
            return None

        slug = getattr(obs_cfg, "sidecar_slug", "webhook-hub-sentinel") if obs_cfg else "webhook-hub-sentinel"
        custom_dir = getattr(obs_cfg, "sidecar_data_dir", None) if obs_cfg else None

        try:
            if custom_dir:
                base_dir = Path(custom_dir)
            else:
                base_dir = Path.home() / ".gemini" / "antigravity" / "sidecar_data"

            events_dir = base_dir / slug / "events"
            events_dir.mkdir(parents=True, exist_ok=True)

            source = str(task_data.get("source") or "webhook")
            action_type = str(task_data.get("action_type") or "cli")

            # Craft human-readable summary for Antigravity sidebar display
            if action_type in ("agent_signal", "signal"):
                action_params = task_data.get("action_params")
                if not action_params and task_data.get("action_params_json"):
                    action_params = task_data.get("action_params_json")
                if isinstance(action_params, str):
                    try:
                        action_params = json.loads(action_params)
                    except Exception:
                        action_params = {}
                if not isinstance(action_params, dict):
                    action_params = {}
                prompt_hint = action_params.get("prompt") or task_data.get("prompt") or task_data.get("command") or task_id
                summary = f"Agent Signal [{source}]: {prompt_hint}"
            elif action_type in ("contact_review", "review_contact", "contact-review"):
                verdict = (task_result_data.get("verdict") if task_result_data else None) or status.upper()
                target = (task_result_data.get("target_name") if task_result_data else None) or source
                summary = f"Contact Review [{target}]: {verdict}"
            else:
                cmd_hint = task_data.get("command") or task_data.get("target_action") or task_id
                summary = f"Webhook Task [{source}/{action_type}]: {cmd_hint}"

            if status != "succeeded":
                err_text = error_message or (f"Exit code {exit_code}" if exit_code is not None else f"Failed with status {status}")
            else:
                err_text = ""

            now_ms = str(int(time.time() * 1000))
            ts_name = time.strftime("%Y%m%d_%H%M%S", time.gmtime()) + f".{int(now_ms[-3:]):03d}.json"

            event_payload = {
                "timestampMs": now_ms,
                "commandInvocationTimestampMs": now_ms,
                "error": err_text,
                "payload": {
                    "newConversation": {
                        "prompt": f"Antigravity Webhook Activity: {summary}",
                        "conversationId": str(uuid.uuid4()),
                        "taskId": task_id,
                        "source": source,
                        "action": action_type,
                        "status": status,
                        "exitCode": exit_code,
                    }
                },
            }

            event_file = events_dir / ts_name
            event_file.write_text(json.dumps(event_payload), encoding="utf-8")
            return event_file
        except Exception as sidecar_err:
            logger.debug("Failed to record Antigravity sidebar activity: %s", sidecar_err)
            return None
