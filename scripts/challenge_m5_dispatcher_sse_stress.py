#!/usr/bin/env python3
"""
Empirical Challenger M5-2: Dispatcher & SSE Streaming Adversarial Stress Suite.
Standalone Zero-Dependency Empirical Verification Runner.

Challenges:
1. Subprocess Concurrency & Process Group Cleanup (10 concurrent CLI tasks: fast, failing, timeouts, stubborn)
2. Process Group Isolation (os.setsid) & Grandchild Termination on Timeout
3. SSE Streaming Resilience: 20 concurrent subscribers & abrupt socket disconnects
4. SSE Slow Consumer Queue Bounding: 500 events, drop-oldest, non-blocking
5. Standalone Server Process Memory RSS Under Concurrent Load (< 30.0MB)
6. Dispatcher Queue Concurrency Analysis (Sequential worker loop vs max_concurrent_tasks)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import json
import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hub.broker import EventBroker
from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.observability import register_observability_routes
from hub.routes.sse import register_sse_routes
from hub.routes.webhook import register_webhook_routes
from hub.server import AsyncHTTPServer


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


class M5EmpiricalRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.findings: list[dict[str, str]] = []
        self.temp_dir = tempfile.mkdtemp(prefix="challenger_m5_2_")
        self.db_path = os.path.join(self.temp_dir, "m5_challenge.db")
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
        print(f"{GREEN}[PASS]{RESET} {BOLD}{name}{RESET}" + (f": {detail}" if detail else ""))

    def log_fail(self, name: str, reason: str, finding_id: str = ""):
        self.failed += 1
        print(f"{RED}[FAIL]{RESET} {BOLD}{name}{RESET}: {reason}")
        if finding_id:
            self.findings.append({"id": finding_id, "test": name, "detail": reason})

    def log_info(self, msg: str):
        print(f"{YELLOW}[INFO]{RESET} {msg}")

    async def run_all(self):
        print("=" * 80)
        print(f"{BOLD}CHALLENGER M5-2: EMPIRICAL DISPATCHER & SSE STRESS TEST SUITE{RESET}")
        print("=" * 80)

        await self.test_1_subprocess_concurrency_and_cleanup()
        await self.test_2_process_group_grandchild_cleanup()
        await self.test_3_sse_streaming_abrupt_disconnect_no_leaks()
        await self.test_4_sse_slow_consumer_queue_overflow()
        await self.test_5_standalone_server_memory_rss_under_load()
        await self.test_6_dispatcher_queue_concurrency_behavior()

        print("=" * 80)
        print(f"RESULTS: {GREEN}{self.passed} Passed{RESET}, {RED if self.failed else GREEN}{self.failed} Failed{RESET}")
        if self.findings:
            print(f"\n{RED}{BOLD}EMPIRICAL DEFECTS & FINDINGS IDENTIFIED:{RESET}")
            for f in self.findings:
                print(f" - [{f['id']}] {f['test']}: {f['detail']}")
        print("=" * 80)

    # ------------------------------------------------------------------------
    # Test 1: Subprocess Concurrency & Cleanup (10 Tasks)
    # ------------------------------------------------------------------------
    async def test_1_subprocess_concurrency_and_cleanup(self):
        name = "Subprocess Concurrency & Cleanup (10 Tasks: 4 Fast, 3 Failing, 3 Timeouts)"
        dispatcher = TaskDispatcher(self.db)
        await dispatcher.start()

        # Seed 10 events
        for i in range(1, 11):
            await self.db.execute_write(
                "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) "
                "VALUES (?, 'm5', '', '{}', '{}', 'POST', '/webhook', 'received')",
                (f"evt_c1_{i:02d}",),
            )

        tasks = [
            ("tsk_c1_01", "evt_c1_01", "python3 -c \"print('FAST_01')\"", 10),
            ("tsk_c1_02", "evt_c1_02", "python3 -c \"print('FAST_02')\"", 10),
            ("tsk_c1_03", "evt_c1_03", "python3 -c \"print('FAST_03')\"", 10),
            ("tsk_c1_04", "evt_c1_04", "python3 -c \"print('FAST_04')\"", 10),
            ("tsk_c1_05", "evt_c1_05", "python3 -c \"import sys; sys.stderr.write('ERR_05\\n'); sys.exit(1)\"", 10),
            ("tsk_c1_06", "evt_c1_06", "python3 -c \"import sys; sys.stderr.write('ERR_06\\n'); sys.exit(42)\"", 10),
            ("tsk_c1_07", "evt_c1_07", "python3 -c \"import sys; sys.stderr.write('ERR_07\\n'); sys.exit(127)\"", 10),
            ("tsk_c1_08", "evt_c1_08", "python3 -c \"import time; time.sleep(15)\"", 1),
            ("tsk_c1_09", "evt_c1_09", "python3 -c \"import time; time.sleep(15)\"", 1),
            ("tsk_c1_10", "evt_c1_10", "python3 -c \"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(15)\"", 1),
        ]

        task_ids = [t[0] for t in tasks]
        for tid, eid, cmd, tout in tasks:
            await self.db.execute_write(
                "INSERT INTO tasks (task_id, event_id, action_type, command, status, timeout_seconds) "
                "VALUES (?, ?, 'cli', ?, 'queued', ?)",
                (tid, eid, cmd, tout),
            )

        t0 = time.perf_counter()
        results = await asyncio.gather(*[dispatcher.execute_task(tid) for tid in task_ids])
        elapsed = time.perf_counter() - t0
        await dispatcher.stop()

        if elapsed > 5.0:
            self.log_fail(name, f"Execution took {elapsed:.2f}s > 5.0s limit (not concurrent)")
            return

        res_map = {r.task_id: r for r in results}

        # Check fast
        for i in range(1, 5):
            tid = f"tsk_c1_{i:02d}"
            r = res_map[tid]
            if r.status != "succeeded" or r.exit_code != 0 or f"FAST_{i:02d}" not in r.stdout:
                self.log_fail(name, f"Fast task {tid} failed verification: status={r.status}, code={r.exit_code}")
                return

        # Check failing
        if res_map["tsk_c1_05"].exit_code != 1 or res_map["tsk_c1_06"].exit_code != 42 or res_map["tsk_c1_07"].exit_code != 127:
            self.log_fail(name, "Failing tasks did not return expected non-zero exit codes")
            return

        # Check timeouts
        if res_map["tsk_c1_08"].status != "timed_out" or res_map["tsk_c1_08"].exit_code not in (-15, -9):
            self.log_fail(name, f"Timeout task 08 did not terminate cleanly: {res_map['tsk_c1_08']}")
            return

        if res_map["tsk_c1_10"].status != "timed_out" or res_map["tsk_c1_10"].exit_code != -9:
            self.log_fail(name, f"Stubborn task 10 was not killed via SIGKILL (-9): {res_map['tsk_c1_10']}")
            return

        # Check executions table in DB
        db_execs = await self.db.execute_read("SELECT execution_id, task_id, pid, status, exit_code FROM executions")
        if len(db_execs) != 10:
            self.log_fail(name, f"Executions table has {len(db_execs)} rows, expected 10")
            return

        # Check if pid is populated in executions table
        pids = [e["pid"] for e in db_execs]
        if all(p is None for p in pids):
            self.findings.append({
                "id": "FINDING-PID-OMISSION",
                "test": name,
                "detail": "Subprocess PID is never written to executions table (pid remains NULL for all executions)",
            })

        # Check execution_logs table
        db_logs = await self.db.execute_read("SELECT count(*) as cnt FROM execution_logs")
        if db_logs[0]["cnt"] == 0:
            self.log_fail(name, "Execution logs table has 0 rows")
            return

        # Verify no zombie child processes
        await asyncio.sleep(0.3)
        ps_out = subprocess.check_output(["ps", "-ef"]).decode()
        for tid in task_ids:
            if tid in ps_out:
                self.log_fail(name, f"Orphaned task process {tid} still running!")
                return

        my_pid = str(os.getpid())
        defunct_lines = [
            l for l in ps_out.splitlines()
            if ("<defunct>" in l or " Z " in l) and len(l.split()) >= 3 and l.split()[2] == my_pid
        ]
        if defunct_lines:
            self.log_fail(name, f"Zombie processes left under PID {my_pid}: {defunct_lines}")
            return

        self.log_pass(name, f"All 10 tasks executed in {elapsed:.2f}s, clean SIGTERM/SIGKILL, zero orphans")

    # ------------------------------------------------------------------------
    # Test 2: Process Group Grandchild Cleanup
    # ------------------------------------------------------------------------
    async def test_2_process_group_grandchild_cleanup(self):
        name = "Process Group Isolation (os.setsid) Grandchild Termination"
        dispatcher = TaskDispatcher(self.db)
        await dispatcher.start()

        tid = "tsk_gc_test"
        await self.db.execute_write(
            "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) "
            "VALUES ('evt_gc', 'm5', '', '{}', '{}', 'POST', '/webhook', 'received')"
        )
        cmd = "python3 -c \"import subprocess, time; subprocess.Popen(['python3', '-c', 'import time; time.sleep(25)']); time.sleep(25)\""
        await self.db.execute_write(
            "INSERT INTO tasks (task_id, event_id, action_type, command, status, timeout_seconds) "
            "VALUES (?, 'evt_gc', 'cli', ?, 'queued', 1)",
            (tid, cmd),
        )

        res = await dispatcher.execute_task(tid)
        await dispatcher.stop()

        if res.status != "timed_out":
            self.log_fail(name, f"Expected status timed_out, got {res.status}")
            return

        await asyncio.sleep(0.5)
        ps_out = subprocess.check_output(["ps", "-ef"]).decode()
        if "time.sleep(25)" in ps_out:
            self.log_fail(name, "Grandchild process sleep(25) leaked after timeout!")
            return

        self.log_pass(name, "Grandchild process group killed cleanly on timeout")

    # ------------------------------------------------------------------------
    # Test 3: SSE Streaming Resilience & Leak Prevention
    # ------------------------------------------------------------------------
    async def test_3_sse_streaming_abrupt_disconnect_no_leaks(self):
        name = "SSE Streaming Resilience: 20 Subscribers Abrupt Disconnect Leak Check"
        broker = EventBroker(default_queue_size=256)
        config = AppConfig()
        config.server.port = 63123
        server = AsyncHTTPServer(config.server)

        register_sse_routes(server, config, self.db, broker)
        await server.start()
        base_url = f"http://127.0.0.1:{config.server.port}"

        # Seed task
        task_id = "tsk_sse_stream_test"
        await self.db.execute_write(
            "INSERT INTO tasks (task_id, event_id, action_type, command, status) "
            "VALUES (?, 'evt_sse', 'cli', 'echo test', 'running')",
            (task_id,),
        )

        import httpx
        clients = [httpx.AsyncClient(timeout=10.0) for _ in range(20)]
        responses = []

        try:
            # 10 global, 10 task
            for i in range(10):
                r = await clients[i].send(clients[i].build_request("GET", f"{base_url}/events/stream"), stream=True)
                assert r.status_code == 200
                responses.append(r)
            for i in range(10):
                idx = 10 + i
                r = await clients[idx].send(clients[idx].build_request("GET", f"{base_url}/tasks/{task_id}/stream"), stream=True)
                assert r.status_code == 200
                responses.append(r)

            await asyncio.sleep(0.1)
            if broker.subscriber_count() != 20:
                self.log_fail(name, f"Expected 20 active subscribers, got {broker.subscriber_count()}")
                return

            # Abrupt disconnect all
            for r in responses:
                await r.aclose()
            for c in clients:
                await c.aclose()

            # Flush
            for _ in range(3):
                await broker.publish("events", {"ping": "flush"})
                await broker.publish(f"task.{task_id}", {"ping": "flush"})
                await asyncio.sleep(0.05)

            # Wait for disconnect detection
            for _ in range(30):
                if broker.subscriber_count() == 0:
                    break
                await asyncio.sleep(0.05)

            if broker.subscriber_count() != 0 or len(broker._subscribers) != 0:
                self.log_fail(name, f"Subscriber leak! Count={broker.subscriber_count()}, map={broker._subscribers}")
                return

            self.log_pass(name, "All 20 SSE subscribers cleaned up, zero broker leaks")

        finally:
            await server.stop()
            for c in clients:
                try:
                    await c.aclose()
                except Exception:
                    pass

    # ------------------------------------------------------------------------
    # Test 4: SSE Slow Consumer Queue Overflow
    # ------------------------------------------------------------------------
    async def test_4_sse_slow_consumer_queue_overflow(self):
        name = "SSE Slow Consumer Queue Bounding (500 Events, Drop Oldest)"
        broker = EventBroker(default_queue_size=256)
        q = broker.subscribe("topic.slow", maxsize=256)

        t0 = time.perf_counter()
        for i in range(500):
            await broker.publish("topic.slow", {"seq": i})
        dur = time.perf_counter() - t0

        if dur > 0.1:
            self.log_fail(name, f"Publishing 500 events took {dur:.4f}s > 0.1s (blocked)")
            return

        if q.qsize() != 256:
            self.log_fail(name, f"Queue size {q.qsize()} != 256")
            return

        items = []
        while not q.empty():
            items.append(q.get_nowait())

        seqs = [item["seq"] for item in items]
        if seqs != list(range(244, 500)):
            self.log_fail(name, f"Oldest frames not dropped correctly: {seqs[0]}..{seqs[-1]}")
            return

        broker.unsubscribe("topic.slow", q)
        self.log_pass(name, "Bounded at maxsize=256, dropped oldest 244 frames without blocking")

    # ------------------------------------------------------------------------
    # Test 5: Standalone Server Process Memory RSS Under Load (<30MB)
    # ------------------------------------------------------------------------
    async def test_5_standalone_server_memory_rss_under_load(self):
        name = "Standalone Server Process Memory RSS Under Load (<30MB Budget)"
        secret = "standalone_m5_secret_32bytes_1234"
        port = 63124
        db_file = os.path.join(self.temp_dir, "standalone_rss.db")

        env = dict(os.environ)
        env["WEBHOOK_SECRET"] = secret

        proc = subprocess.Popen(
            [sys.executable, "-m", "hub.cli", "start", "--port", str(port), "--db", db_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        try:
            time.sleep(1.0)
            if proc.poll() is not None:
                self.log_fail(name, "Server failed to start")
                return

            out_init = subprocess.check_output(["ps", "-o", "rss=", "-p", str(proc.pid)]).decode().strip()
            rss_init = int(out_init) / 1024.0

            # Fire 10 HMAC webhooks
            for i in range(10):
                body = json.dumps({"action": "cli", "command": f"python3 -c \"print({i})\""}).encode()
                ts = int(time.time())
                sig = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/webhook/m5_src",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": sig,
                        "X-Hub-Timestamp": str(ts),
                        "X-Hub-Event-ID": f"evt_rss_{i}",
                    },
                )
                with urllib.request.urlopen(req) as resp:
                    assert resp.status == 202

            time.sleep(1.5)
            out_after = subprocess.check_output(["ps", "-o", "rss=", "-p", str(proc.pid)]).decode().strip()
            rss_after = int(out_after) / 1024.0

            if rss_after >= 30.0:
                self.log_fail(
                    name,
                    f"Process RSS breached 30MB under load: {rss_after:.2f}MB (initial: {rss_init:.2f}MB)",
                    finding_id="FINDING-RSS-BREACH",
                )
            else:
                self.log_pass(name, f"RSS maintained under budget: {rss_init:.2f}MB -> {rss_after:.2f}MB < 30.0MB")

        finally:
            proc.terminate()
            proc.wait()

    # ------------------------------------------------------------------------
    # Test 6: Dispatcher Queue Concurrency Analysis
    # ------------------------------------------------------------------------
    async def test_6_dispatcher_queue_concurrency_behavior(self):
        name = "Dispatcher Queue Concurrency Analysis (config.dispatch.max_concurrent_tasks)"
        cfg = AppConfig()
        cfg.dispatch.max_concurrent_tasks = 5
        dispatcher = TaskDispatcher(self.db, config=cfg)
        await dispatcher.start()

        for i in (1, 2):
            await self.db.execute_write(
                "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) "
                "VALUES (?, 'm5_q', '', '{}', '{}', 'POST', '/webhook', 'received')",
                (f"evt_q_{i}",),
            )
            await self.db.execute_write(
                "INSERT INTO tasks (task_id, event_id, action_type, command, status, timeout_seconds) "
                "VALUES (?, ?, 'cli', 'python3 -c \"import time; time.sleep(1.0)\"', 'queued', 10)",
                (f"tsk_q_{i}", f"evt_q_{i}"),
            )

        t0 = time.perf_counter()
        await dispatcher.enqueue("tsk_q_1")
        await dispatcher.enqueue("tsk_q_2")
        await dispatcher.queue.join()
        elapsed = time.perf_counter() - t0
        await dispatcher.stop()

        if elapsed >= 1.8:
            self.findings.append({
                "id": "FINDING-SERIAL-DISPATCH-QUEUE",
                "test": name,
                "detail": (
                    f"Dispatcher processes queue strictly serially (2 x 1.0s tasks took {elapsed:.2f}s). "
                    "config.dispatch.max_concurrent_tasks (=5) is ignored; only 1 worker task runs."
                ),
            })
            self.log_pass(name, f"Empirically confirmed serial execution: 2x 1.0s tasks took {elapsed:.2f}s (max_concurrent_tasks=5 ignored)")
        else:
            self.log_pass(name, f"Concurrent queue execution verified: 2x 1.0s tasks took {elapsed:.2f}s")


if __name__ == "__main__":
    runner = M5EmpiricalRunner()
    try:
        asyncio.run(runner.run_all())
    finally:
        runner.cleanup()
