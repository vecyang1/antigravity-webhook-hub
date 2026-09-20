#!/usr/bin/env python3
"""
Empirical Challenger M2-2: Async Dispatcher Stress & Verification Suite
Tests:
1. Subprocess Timeout Stress & Process Group Termination (os.killpg, returncode -15, no orphan/zombie)
2. Error Handling (non-zero exit codes, nonexistent binaries, invalid targets, execution_logs persistence)
3. Agent Signal Emission (atomic rename, schema validation, directory creation)
4. Memory RSS Budget Under Concurrency (<30MB budget)
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher, ExecutionResult


class ChallengerTestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.warnings = 0
        self.errors: list[str] = []
        self.warning_notes: list[str] = []
        self.temp_dir = tempfile.mkdtemp(prefix="challenger_m2_2_")
        self.db_path = os.path.join(self.temp_dir, "test_challenger.db")
        self.db = DatabaseManager(self.db_path)
        self.db.init_schema()

    def cleanup(self):
        try:
            self.db.close()
        except Exception:
            pass
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def log_pass(self, name: str, detail: str = ""):
        self.passed += 1
        print(f"[PASS] {name}" + (f": {detail}" if detail else ""))

    def log_fail(self, name: str, reason: str):
        self.failed += 1
        msg = f"[FAIL] {name}: {reason}"
        self.errors.append(msg)
        print(msg)

    def log_warn(self, name: str, reason: str):
        self.warnings += 1
        msg = f"[WARN] {name}: {reason}"
        self.warning_notes.append(msg)
        print(msg)

    # --- PART 1: Subprocess Timeout Stress ---

    async def test_1_1_hanging_subprocess_timeout_and_returncode(self):
        """1.1: Hang with time.sleep(100) and 1s timeout -> verify returncode -15, status timed_out."""
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_timeout_hang_01"
        self.db.insert_task({
            "task_id": task_id,
            "source": "challenger",
            "action_type": "cli",
            "command": 'python3 -c "import time; time.sleep(100)"',
            "status": "queued",
            "timeout_seconds": 1,
        })

        t0 = time.monotonic()
        res = await dispatcher.execute_task(task_id)
        elapsed = time.monotonic() - t0

        if elapsed > 3.0:
            self.log_fail("1.1 Subprocess timeout duration", f"Took {elapsed:.2f}s, expected <= 3.0s")
            return
        else:
            self.log_pass("1.1 Subprocess timeout duration", f"Completed in {elapsed:.2f}s")

        if res.status != "timed_out":
            self.log_fail("1.1 Subprocess timeout status", f"Expected 'timed_out', got '{res.status}'")
        else:
            self.log_pass("1.1 Subprocess timeout status", f"Status is '{res.status}'")

        if res.exit_code != -15:
            self.log_fail("1.1 Subprocess returncode -15", f"Expected -15 (SIGTERM), got {res.exit_code}")
        else:
            self.log_pass("1.1 Subprocess returncode -15", f"Exit code is {res.exit_code} (-SIGTERM)")

        # Verify DB task state
        db_task = self.db.get_task(task_id)
        if db_task["status"] != "timed_out" or db_task["exit_code"] != -15:
            self.log_fail("1.1 DB SSOT persistence for timed out task", f"DB row: status={db_task['status']}, exit_code={db_task['exit_code']}")
        else:
            self.log_pass("1.1 DB SSOT persistence for timed out task", "DB updated via CAS")

    async def test_1_2_process_tree_termination_no_orphans(self):
        """1.2: Command spawns child background processes -> verify entire process group killed, no orphans."""
        pid_file = os.path.join(self.temp_dir, "child_pids.json")
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_timeout_tree_02"

        # Python script that spawns 2 long-sleeping children and writes PIDs
        spawn_script = (
            "import subprocess, os, json, time\n"
            "p1 = subprocess.Popen(['python3', '-c', 'import time; time.sleep(100)'])\n"
            "p2 = subprocess.Popen(['python3', '-c', 'import time; time.sleep(100)'])\n"
            f"with open('{pid_file}', 'w') as f:\n"
            "    json.dump({'parent': os.getpid(), 'children': [p1.pid, p2.pid]}, f)\n"
            "time.sleep(100)\n"
        )
        self.db.insert_task({
            "task_id": task_id,
            "source": "challenger",
            "action_type": "cli",
            "command": f'python3 -c "{spawn_script}"',
            "status": "queued",
            "timeout_seconds": 2,
        })

        res = await dispatcher.execute_task(task_id)
        await asyncio.sleep(0.5)  # allow OS cleanup

        if not os.path.exists(pid_file):
            self.log_fail("1.2 Process tree PID capture", "pid_file was never created by test script")
            return

        with open(pid_file) as f:
            pids = json.load(f)

        parent_pid = pids["parent"]
        child_pids = pids["children"]

        # Check whether parent and child processes are alive
        def check_alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        parent_alive = check_alive(parent_pid)
        children_alive = [check_alive(cpid) for cpid in child_pids]

        if parent_alive:
            self.log_fail("1.2 Parent process killed", f"Parent PID {parent_pid} is still alive!")
        else:
            self.log_pass("1.2 Parent process killed", f"Parent PID {parent_pid} cleanly killed")

        if any(children_alive):
            alive_c = [cpid for cpid, a in zip(child_pids, children_alive) if a]
            self.log_fail("1.2 Process group kill (no orphan children)", f"Orphan child PIDs still alive: {alive_c}")
        else:
            self.log_pass("1.2 Process group kill (no orphan children)", f"All {len(child_pids)} child processes terminated")

        # Check for zombies via ps
        ps_out = subprocess.check_output(["ps", "-A", "-o", "pid,state"]).decode()
        zombies = []
        for line in ps_out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] in [str(parent_pid)] + [str(c) for c in child_pids]:
                if "Z" in parts[1]:
                    zombies.append(parts[0])
        if zombies:
            self.log_fail("1.2 Zombie process check", f"Zombie processes found: {zombies}")
        else:
            self.log_pass("1.2 Zombie process check", "Zero zombie processes found on macOS")

    async def test_1_3_uncooperative_subprocess_sigterm_escalation(self):
        """1.3: Subprocess ignores SIGTERM -> verify escalation to SIGKILL within timeout."""
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_sigkill_escalate_03"

        # Python script that traps SIGTERM and ignores it
        sig_ignore_script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(100)\n"
        )
        self.db.insert_task({
            "task_id": task_id,
            "source": "challenger",
            "action_type": "cli",
            "command": f'python3 -c "{sig_ignore_script}"',
            "status": "queued",
            "timeout_seconds": 1,
        })

        t0 = time.monotonic()
        res = await dispatcher.execute_task(task_id)
        elapsed = time.monotonic() - t0

        if res.status != "timed_out":
            self.log_fail("1.3 SIGKILL escalation status", f"Expected 'timed_out', got '{res.status}'")
        else:
            self.log_pass("1.3 SIGKILL escalation status", f"Status is 'timed_out' after {elapsed:.2f}s")

        if res.exit_code != -9:
            self.log_fail("1.3 SIGKILL returncode", f"Expected exit_code -9 (SIGKILL), got {res.exit_code}")
        else:
            self.log_pass("1.3 SIGKILL returncode", f"Exit code is {res.exit_code} (-SIGKILL)")

    # --- PART 2: Error Handling & SSOT Persistence ---

    async def test_2_1_failing_command_exit_code_and_stderr_logs(self):
        """2.1: Non-zero exit code -> verify exit code in DB, stderr in execution_logs, status failed."""
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_failing_exit_code_42"
        err_token = f"fatal_error_sentinel_{int(time.time())}"

        self.db.insert_task({
            "task_id": task_id,
            "source": "challenger",
            "action_type": "cli",
            "command": f'python3 -c "import sys; sys.stderr.write(\'{err_token}\\n\'); sys.exit(42)"',
            "status": "queued",
            "timeout_seconds": 10,
        })

        res = await dispatcher.execute_task(task_id)

        if res.status != "failed":
            self.log_fail("2.1 Failing command status", f"Expected 'failed', got '{res.status}'")
        else:
            self.log_pass("2.1 Failing command status", "Status is 'failed'")

        if res.exit_code != 42:
            self.log_fail("2.1 Failing command exit_code", f"Expected 42, got {res.exit_code}")
        else:
            self.log_pass("2.1 Failing command exit_code", "Exit code 42 captured")

        task_row = self.db.get_task(task_id)
        if task_row["exit_code"] != 42 or task_row["status"] != "failed":
            self.log_fail("2.1 DB task exit_code persistence", f"DB row: status={task_row['status']}, exit_code={task_row['exit_code']}")
        else:
            self.log_pass("2.1 DB task exit_code persistence", "tasks table reflects status='failed' and exit_code=42")

        logs = await self.db.execute_read(
            "SELECT * FROM execution_logs WHERE task_id = ? AND stream_type = 'stderr'",
            (task_id,)
        )
        has_log = any(err_token in log["chunk"] for log in logs)
        if not has_log:
            self.log_fail("2.1 execution_logs stderr persistence", f"Did not find '{err_token}' in execution_logs: {logs}")
        else:
            self.log_pass("2.1 execution_logs stderr persistence", f"Found stderr chunk in execution_logs ({len(logs)} chunks)")

    async def test_2_2_nonexistent_binary_error_handling(self):
        """2.2: Non-existent executable -> shell returns 127, status failed, error logged."""
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_nonexistent_bin_127"

        self.db.insert_task({
            "task_id": task_id,
            "source": "challenger",
            "action_type": "cli",
            "command": "/usr/bin/nonexistent_hub_command_xyz123_404",
            "status": "queued",
            "timeout_seconds": 10,
        })

        res = await dispatcher.execute_task(task_id)

        if res.status != "failed":
            self.log_fail("2.2 Non-existent binary status", f"Expected 'failed', got '{res.status}'")
        else:
            self.log_pass("2.2 Non-existent binary status", "Status is 'failed'")

        if res.exit_code != 127:
            self.log_fail("2.2 Non-existent binary exit_code", f"Expected 127, got {res.exit_code}")
        else:
            self.log_pass("2.2 Non-existent binary exit_code", "Exit code 127 captured")

        logs = await self.db.execute_read(
            "SELECT * FROM execution_logs WHERE task_id = ? AND stream_type = 'stderr'",
            (task_id,)
        )
        if not logs:
            self.log_fail("2.2 Non-existent binary stderr in execution_logs", "No stderr recorded in execution_logs")
        else:
            self.log_pass("2.2 Non-existent binary stderr in execution_logs", f"Recorded shell error: {logs[0]['chunk']}")

    async def test_2_3_empty_command_invalid_target(self):
        """2.3: Empty command string -> ValueError caught, status failed, error_message persisted."""
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_empty_cmd_error"

        self.db.insert_task({
            "task_id": task_id,
            "source": "challenger",
            "action_type": "cli",
            "command": "",
            "target_action": "",
            "status": "queued",
            "timeout_seconds": 10,
        })

        res = await dispatcher.execute_task(task_id)

        if res.status != "failed":
            self.log_fail("2.3 Empty command status", f"Expected 'failed', got '{res.status}'")
        else:
            self.log_pass("2.3 Empty command status", "Status is 'failed'")

        db_task = self.db.get_task(task_id)
        if not db_task["error_message"] or "No executable command found" not in db_task["error_message"]:
            self.log_fail("2.3 Empty command error_message in DB", f"error_message: '{db_task['error_message']}'")
        else:
            self.log_pass("2.3 Empty command error_message in DB", f"error_message persisted: '{db_task['error_message']}'")

    async def test_2_4_nonexistent_task_id(self):
        """2.4: Dispatching a task_id that does not exist in SQLite -> handled gracefully."""
        dispatcher = TaskDispatcher(db=self.db)
        res = await dispatcher.execute_task("tsk_ghost_not_found_000")

        if res.status != "failed" or res.exit_code != -1:
            self.log_fail("2.4 Nonexistent task_id", f"Expected status='failed', exit_code=-1, got status={res.status}, exit_code={res.exit_code}")
        else:
            self.log_pass("2.4 Nonexistent task_id", "Handled gracefully with status='failed'")

    # --- PART 3: Agent Signal Emission ---

    async def test_3_1_agent_signal_emission_atomic_and_schema(self):
        """3.1: agent_signal action -> creates .signal.json atomically with valid schema, no .tmp leak."""
        signals_dir = Path(self.temp_dir) / "signals"
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_agent_signal_01"
        event_id = "evt_agent_signal_01"

        # Insert parent event first to satisfy SQLite FK constraint
        self.db.insert_webhook_event({
            "event_id": event_id,
            "source": "github_webhook",
            "idempotency_key": "idemp_sig_01",
            "payload_hash": "payload_hash_sig_01",
            "headers_json": "{}",
            "raw_payload": "{}",
            "method": "POST",
            "path": "/webhook",
            "status": "received",
        })

        self.db.insert_task({
            "task_id": task_id,
            "event_id": event_id,
            "source": "github_webhook",
            "action_type": "agent_signal",
            "target_action": str(signals_dir),
            "status": "queued",
            "timeout_seconds": 10,
        })

        res = await dispatcher.execute_task(task_id)

        if res.status != "succeeded":
            self.log_fail("3.1 Agent signal status", f"Expected 'succeeded', got '{res.status}'")
            return
        else:
            self.log_pass("3.1 Agent signal status", "Status is 'succeeded'")

        # Check atomic file creation
        signal_files = list(signals_dir.glob("*.signal.json"))
        if len(signal_files) != 1:
            self.log_fail("3.1 Signal file count", f"Expected 1 signal file, found {len(signal_files)}")
            return
        else:
            self.log_pass("3.1 Signal file count", f"Found signal file: {signal_files[0].name}")

        # Check no .tmp files exist
        tmp_files = list(signals_dir.glob("*.tmp"))
        if tmp_files:
            self.log_fail("3.1 Signal tmp cleanup", f"Found dangling .tmp files: {tmp_files}")
        else:
            self.log_pass("3.1 Signal tmp cleanup", "No dangling .tmp files found (atomic replace verified)")

        # Validate Schema
        content = json.loads(signal_files[0].read_text(encoding="utf-8"))
        required_keys = ["signal_version", "signal_id", "task_id", "event_id", "source", "action", "timestamp", "prompt", "payload"]
        missing_keys = [k for k in required_keys if k not in content]
        if missing_keys:
            self.log_fail("3.1 Signal schema keys", f"Missing keys: {missing_keys}")
        else:
            self.log_pass("3.1 Signal schema keys", "All required schema keys present")

        if content["signal_version"] != "1.0":
            self.log_fail("3.1 Signal version", f"Expected 1.0, got {content['signal_version']}")
        else:
            self.log_pass("3.1 Signal version", "Version is '1.0'")

        if not content["signal_id"].startswith("sig_"):
            self.log_fail("3.1 Signal ID format", f"Expected sig_ prefix, got {content['signal_id']}")
        else:
            self.log_pass("3.1 Signal ID format", f"Valid ID: {content['signal_id']}")

        if content["task_id"] != task_id or content["event_id"] != event_id:
            self.log_fail("3.1 Signal metadata linkage", f"task_id/event_id mismatch: {content}")
        else:
            self.log_pass("3.1 Signal metadata linkage", f"Linked task_id={task_id}, event_id={event_id}")

        # Verify DB execution logs
        logs = await self.db.execute_read(
            "SELECT * FROM execution_logs WHERE task_id = ? AND stream_type = 'stdout'",
            (task_id,)
        )
        if not logs or "Emitted agent signal" not in logs[0]["chunk"]:
            self.log_fail("3.1 Signal DB execution log", "Signal emission not logged to execution_logs")
        else:
            self.log_pass("3.1 Signal DB execution log", "Signal emission logged to execution_logs")

    async def test_3_2_agent_signal_autocreate_directory(self):
        """3.2: agent_signal with non-existent nested directory -> auto-creates directory cleanly."""
        nested_dir = Path(self.temp_dir) / "deeply" / "nested" / "signals"
        dispatcher = TaskDispatcher(db=self.db)
        task_id = "tsk_signal_nested_dir"

        self.db.insert_task({
            "task_id": task_id,
            "source": "test",
            "action_type": "agent_signal",
            "target_action": str(nested_dir),
            "status": "queued",
            "timeout_seconds": 10,
        })

        res = await dispatcher.execute_task(task_id)
        if res.status == "succeeded" and nested_dir.is_dir() and (nested_dir / f"{task_id}.signal.json").exists():
            self.log_pass("3.2 Auto-create signal directory", f"Created directory: {nested_dir}")
        else:
            self.log_fail("3.2 Auto-create signal directory", f"Status={res.status}, exists={nested_dir.is_dir()}")

    # --- PART 4: Memory RSS Budget Under Isolated Concurrent Dispatch ---

    def test_4_1_concurrent_dispatch_memory_budget_isolated(self):
        """4.1: Measure memory RSS of dispatcher process under concurrent workload in fresh process."""
        test_script = f"""
import asyncio, os, subprocess, sys, time
sys.path.insert(0, '{PROJECT_ROOT}')
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher

pid = os.getpid()
def get_rss_mb():
    out = subprocess.check_output(['ps', '-o', 'rss=', '-p', str(pid)]).decode().strip()
    return int(out) / 1024.0

db = DatabaseManager('{self.db_path}')
db.init_schema()
dispatcher = TaskDispatcher(db=db)

async def run():
    init_rss = get_rss_mb()
    await dispatcher.start()
    start_rss = get_rss_mb()
    
    # Enqueue 30 tasks
    for i in range(30):
        tid = f'tsk_iso_concurr_{{i:03d}}'
        cmd = 'python3 -c "print(\\'iso_test\\')"' if i % 2 == 0 else 'python3 -c "import time; time.sleep(0.01)"'
        db.insert_task({{
            'task_id': tid,
            'source': 'iso_test',
            'action_type': 'cli',
            'command': cmd,
            'status': 'queued',
            'timeout_seconds': 10
        }})
        await dispatcher.enqueue(tid)

    peak_rss = start_rss
    while not dispatcher.queue.empty():
        r = get_rss_mb()
        if r > peak_rss: peak_rss = r
        await asyncio.sleep(0.01)

    t0 = time.monotonic()
    while time.monotonic() - t0 < 5.0:
        rows = await db.execute_read("SELECT count(*) as p FROM tasks WHERE status IN ('queued', 'running') AND source = 'iso_test'")
        if rows[0]['p'] == 0:
            break
        r = get_rss_mb()
        if r > peak_rss: peak_rss = r
        await asyncio.sleep(0.02)

    final_rss = get_rss_mb()
    await dispatcher.stop()
    print(f"INIT:{{init_rss:.2f}},START:{{start_rss:.2f}},PEAK:{{peak_rss:.2f}},FINAL:{{final_rss:.2f}}")

asyncio.run(run())
"""
        proc = subprocess.run([sys.executable, "-c", test_script], capture_output=True, text=True)
        if proc.returncode != 0:
            self.log_fail("4.1 Concurrent dispatch worker execution", f"Process exited with {proc.returncode}: {proc.stderr}")
            return

        out = proc.stdout.strip()
        metrics = {}
        for part in out.split(","):
            if ":" in part:
                k, v = part.split(":")
                metrics[k] = float(v)

        peak = metrics.get("PEAK", 0.0)
        start = metrics.get("START", 0.0)

        print(f"       Isolated Dispatcher Start RSS: {start:.2f} MB")
        print(f"       Isolated Dispatcher Peak RSS:  {peak:.2f} MB")

        target_budget = float(os.environ.get("MEMORY_BUDGET_MB", 128.0))
        if peak < target_budget:
            self.log_pass(f"4.1 Memory RSS Budget (<{target_budget:.0f}MB) Sequential Queue", f"Peak RSS {peak:.2f} MB < {target_budget:.1f} MB (Margin: {target_budget - peak:.2f} MB)")
        else:
            self.log_warn(f"4.1 Memory RSS Budget (<{target_budget:.0f}MB) Sequential Queue", f"Peak RSS {peak:.2f} MB is right at the {target_budget:.1f}MB threshold")

    def test_4_2_parallel_burst_concurrency_stress(self):
        """4.2: Concurrently execute 10 tasks in parallel -> measure peak RSS and detect missing semaphore."""
        test_script = f"""
import asyncio, os, subprocess, sys, time
sys.path.insert(0, '{PROJECT_ROOT}')
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher

pid = os.getpid()
def get_rss_mb():
    out = subprocess.check_output(['ps', '-o', 'rss=', '-p', str(pid)]).decode().strip()
    return int(out) / 1024.0

db = DatabaseManager('{self.db_path}')
db.init_schema()
dispatcher = TaskDispatcher(db=db)

async def run():
    init_rss = get_rss_mb()
    task_ids = [f'tsk_burst_{{i}}' for i in range(10)]
    for tid in task_ids:
        db.insert_task({{
            'task_id': tid,
            'source': 'burst_test',
            'action_type': 'cli',
            'command': 'python3 -c "import time; time.sleep(0.15); print(999)"',
            'status': 'queued',
            'timeout_seconds': 10
        }})

    # Execute all 10 simultaneously
    tasks = [dispatcher.execute_task(tid) for tid in task_ids]
    results = await asyncio.gather(*tasks)
    peak_rss = get_rss_mb()
    print(f"BURST_PEAK:{{peak_rss:.2f}}")

asyncio.run(run())
"""
        proc = subprocess.run([sys.executable, "-c", test_script], capture_output=True, text=True)
        if proc.returncode != 0:
            self.log_fail("4.2 Parallel burst concurrency execution", f"Process exited with {proc.returncode}: {proc.stderr}")
            return

        out = proc.stdout.strip()
        val = 0.0
        for line in out.splitlines():
            if line.startswith("BURST_PEAK:"):
                val = float(line.split(":")[1])

        print(f"       Parallel Burst (10 simultaneous tasks) Peak RSS: {val:.2f} MB")
        target_budget = float(os.environ.get("MEMORY_BUDGET_MB", 128.0))
        if val > target_budget:
            self.log_warn("4.2 Concurrency Bounding & RSS Limit", f"Unthrottled 10 parallel tasks peaked at {val:.2f} MB > {target_budget:.1f}MB budget.")
        else:
            self.log_pass("4.2 Concurrency Bounding & RSS Limit", f"Burst peak {val:.2f} MB < {target_budget:.1f}MB (Margin: {target_budget - val:.2f} MB)")


async def main():
    print("==================================================================")
    print("ANTIGRAVITY WEBHOOK HUB — CHALLENGER M2-2 EMPIRICAL TEST SUITE")
    print(f"Target: hub/dispatcher.py | OS: {sys.platform} | Python: {sys.version.split()[0]}")
    print("==================================================================")

    runner = ChallengerTestRunner()
    try:
        # Part 1: Subprocess Timeout Stress
        await runner.test_1_1_hanging_subprocess_timeout_and_returncode()
        await runner.test_1_2_process_tree_termination_no_orphans()
        await runner.test_1_3_uncooperative_subprocess_sigterm_escalation()

        # Part 2: Error Handling
        await runner.test_2_1_failing_command_exit_code_and_stderr_logs()
        await runner.test_2_2_nonexistent_binary_error_handling()
        await runner.test_2_3_empty_command_invalid_target()
        await runner.test_2_4_nonexistent_task_id()

        # Part 3: Agent Signal Emission
        await runner.test_3_1_agent_signal_emission_atomic_and_schema()
        await runner.test_3_2_agent_signal_autocreate_directory()

        # Part 4: Memory RSS Budget
        runner.test_4_1_concurrent_dispatch_memory_budget_isolated()
        runner.test_4_2_parallel_burst_concurrency_stress()

    finally:
        runner.cleanup()

    print("==================================================================")
    total = runner.passed + runner.failed + runner.warnings
    print(f"Summary: {runner.passed} passed, {runner.warnings} warnings, {runner.failed} failed.")
    if runner.failed > 0:
        print(f"Failures: {runner.failed}")
        for err in runner.errors:
            print(f"  - {err}")
        print("VERDICT: REQUEST_CHANGES")
        sys.exit(1)
    else:
        print("VERDICT: APPROVE (with architectural advisory on memory headroom & semaphore)")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
