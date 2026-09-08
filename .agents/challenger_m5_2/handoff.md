# Challenger M5-2 Handoff Report: Dispatcher Execution & SSE Streaming Adversarial Verification

**Date**: 2026-09-08T20:25:30+07:00  
**Milestone**: M5 Phase 2 (Adversarial Coverage Hardening - Dispatcher & SSE Streaming)  
**Author**: Challenger M5-2 (critic, specialist)  
**Target Components**: `hub/dispatcher.py`, `hub/broker.py`, `hub/routes/sse.py`, `hub/db.py`, `hub/server.py`  
**Gate Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### 1.1 Empirical Verification Test Suites Authored & Executed
- Test Suite: `tests/stress/test_m5_adversarial_dispatcher_sse.py` (8 automated test cases)
- Standalone Verification Script: `scripts/challenge_m5_dispatcher_sse_stress.py` (6 zero-dependency empirical challenges)

Test Execution Command:
```bash
python3 scripts/challenge_m5_dispatcher_sse_stress.py
```
Verbatim Execution Output:
```
================================================================================
CHALLENGER M5-2: EMPIRICAL DISPATCHER & SSE STRESS TEST SUITE
================================================================================
[PASS] Subprocess Concurrency & Cleanup (10 Tasks: 4 Fast, 3 Failing, 3 Timeouts): All 10 tasks executed in 2.61s, clean SIGTERM/SIGKILL, zero orphans
[PASS] Process Group Isolation (os.setsid) Grandchild Termination: Grandchild process group killed cleanly on timeout
[PASS] SSE Streaming Resilience: 20 Subscribers Abrupt Disconnect Leak Check: All 20 SSE subscribers cleaned up, zero broker leaks
[PASS] SSE Slow Consumer Queue Bounding (500 Events, Drop Oldest): Bounded at maxsize=256, dropped oldest 244 frames without blocking
[FAIL] Standalone Server Process Memory RSS Under Load (<30MB Budget): Process RSS breached 30MB under load: 30.16MB (initial: 29.78MB)
[PASS] Dispatcher Queue Concurrency Analysis (config.dispatch.max_concurrent_tasks): Empirically confirmed serial execution: 2x 1.0s tasks took 2.13s (max_concurrent_tasks=5 ignored)
================================================================================
RESULTS: 5 Passed, 1 Failed

EMPIRICAL DEFECTS & FINDINGS IDENTIFIED:
 - [FINDING-PID-OMISSION] Subprocess Concurrency & Cleanup (10 Tasks: 4 Fast, 3 Failing, 3 Timeouts): Subprocess PID is never written to executions table (pid remains NULL for all executions)
 - [FINDING-RSS-BREACH] Standalone Server Process Memory RSS Under Load (<30MB Budget): Process RSS breached 30MB under load: 30.16MB (initial: 29.78MB)
 - [FINDING-SERIAL-DISPATCH-QUEUE] Dispatcher Queue Concurrency Analysis (config.dispatch.max_concurrent_tasks): Dispatcher processes queue strictly serially (2 x 1.0s tasks took 2.13s). config.dispatch.max_concurrent_tasks (=5) is ignored; only 1 worker task runs.
================================================================================
```

Pytest Execution Command:
```bash
python3 -m pytest tests/stress/test_m5_adversarial_dispatcher_sse.py -v
```
Verbatim Failures from Pytest:
```
FAILED tests/stress/test_m5_adversarial_dispatcher_sse.py::test_adversarial_finding1_standalone_server_process_rss_breach - AssertionError: DEFECT CONFIRMED (Finding 1): Standalone server RSS exceeded 30MB budget: 30.00MB (initial: 29.62MB)
FAILED tests/stress/test_m5_adversarial_dispatcher_sse.py::test_adversarial_finding2_dispatcher_queue_concurrency_ignored - AssertionError: DEFECT CONFIRMED (Finding 2): Dispatcher processed 2 x 1.0s tasks sequentially in 2.09s instead of concurrently (<1.6s). max_concurrent_tasks=5 is ignored.
FAILED tests/stress/test_m5_adversarial_dispatcher_sse.py::test_adversarial_finding3_executions_pid_omission - AssertionError: DEFECT CONFIRMED (Finding 3): executions.pid is NULL for task tsk_pid_audit_check. Subprocess PID was not recorded into audit table.
========================= 3 failed, 5 passed in 10.56s =========================
```

---

### 1.2 Detailed Observations by Dimension

#### A. Subprocess Concurrency & Cleanup (10 Tasks: 4 Fast, 3 Failing, 3 Timeouts) — PASS
- When dispatched concurrently via `asyncio.gather(*[dispatcher.execute_task(tid) for tid in task_ids])`, all 10 tasks completed in 2.61s (< 4.5s threshold).
- **Fast Commands** (4 tasks): Exited 0 with status `succeeded`, captured stdout in both execution results and SQLite `execution_logs`.
- **Failing Commands** (3 tasks): Exited 1, 42, 127 with status `failed`, captured stderr in both execution results and SQLite `execution_logs`.
- **Standard Timeout Commands** (2 tasks): Process group terminated via `os.killpg(pgid, signal.SIGTERM)`, exit code recorded as `-15`, status `timed_out`.
- **Stubborn Timeout Command** (1 task trapping SIGTERM via `signal.signal(signal.SIGTERM, signal.SIG_IGN)`): Dispatcher waited 1.5s grace period, then escalated to `os.killpg(pgid, signal.SIGKILL)`, exit code recorded as `-9`, status `timed_out`.
- **Grandchild Process Isolation**: A task spawning a background child (`subprocess.Popen(['python3', 'sleep 30'])`) was completely killed on parent timeout; zero orphan background processes survived.
- **Process List Check**: Zero zombie or defunct child processes remained under the parent PID.

#### B. SSE Streaming Resilience & Leak Prevention — PASS
- Connected 20 concurrent subscribers (10 on `/events/stream`, 5 on `/tasks/{task1}/stream`, 5 on `/tasks/{task2}/stream`).
- Abruptly severed client connections via socket close in multiple waves.
- `EventBroker.unsubscribe` swept queues across all topics. `broker.subscriber_count()` returned strictly to `0`, and `len(broker._subscribers) == 0`. Zero subscriber queue reference leaks.
- Slow consumer queue overflow: 500 events published to an undrained queue (`maxsize=256`) took <0.02s (completely non-blocking), raised zero exceptions, and cleanly dropped the oldest 244 frames while preserving the latest 256 frames in strict monotonic order.

#### C. Finding 1: Standalone Server Process RSS Breaches 30MB Budget Under Concurrent Load — DEFECT
- File: `hub/cli.py`, `hub/routes/observability.py`
- In `test_adversarial_finding1_standalone_server_process_rss_breach` and `scripts/challenge_m5_dispatcher_sse_stress.py`:
  - Standalone server starts with idle RSS: ~29.50MB - 29.78MB.
  - Upon receiving 10 concurrent HMAC-signed webhooks with CLI commands, the server process resident set size (RSS) measured via `ps -o rss= -p <pid>` expands to **30.16MB** (and in daemon mode up to **30.94MB** as previously observed by Challenger M4-2).
  - Breaches `ORIGINAL_REQUEST.md` R1: *"Gateway idle RAM usage remains under 30MB"* and *"system stays under 30MB memory"*.
  - Breaches `/healthz`: When RSS is 30.16MB, `/healthz` returns `status: "degraded"` because `memory_healthy` evaluates `rss_mb < 30.0` (False).

#### D. Finding 2: Dispatcher Serial Queue Execution Ignores `max_concurrent_tasks` — DEFECT
- File: `hub/dispatcher.py`, lines 51-53, 87, 106-122
- `hub/config.py` line 49 and `config.yaml.example` line 30 specify:
  ```python
  max_concurrent_tasks: int = 5
  ```
- However, `hub/dispatcher.py` only starts a SINGLE worker task:
  ```python
  self._worker_task = asyncio.create_task(self._worker_loop())
  ```
- And in `_worker_loop`:
  ```python
  while self._is_running:
      task_id = await self.queue.get()
      try:
          await self.execute_task(task_id)
      finally:
          self.queue.task_done()
  ```
  `await self.execute_task(task_id)` is awaited synchronously inside the single worker loop.
- Result: When webhooks enqueue tasks via `await dispatcher.enqueue(task_id)` (line 221 of `hub/routes/webhook.py`), tasks are processed strictly **one at a time (concurrency = 1)**.
- In `test_adversarial_finding2_dispatcher_queue_concurrency_ignored`, two tasks sleeping 1.0s took **2.09s** to finish sequentially instead of ~1.0s concurrently. If one task hangs for 300s, all subsequent tasks are starved in `queued` status.

#### E. Finding 3: Subprocess PID is Never Recorded in `executions` Audit Table — DEFECT
- File: `hub/dispatcher.py`, lines 244 and 421; `hub/db.py`, line 182
- `executions` relational schema defines:
  ```sql
  pid INTEGER
  ```
- In `hub/dispatcher.py`:
  - Line 244 inserts the execution record with `"pid": None`.
  - Line 346 spawns the subprocess `proc = await asyncio.create_subprocess_shell(...)`, obtaining `proc.pid`.
  - Line 421 calls `self.db.update_execution(...)` without passing `pid=proc.pid`.
- Result: Every record in the `executions` audit table has `pid = NULL`.

---

## 2. Logic Chain

1. **Premise 1 (Memory Budget)**: `ORIGINAL_REQUEST.md` (R1 & Acceptance Criteria) mandates that gateway memory must stay strictly under 30MB.
2. **Observation 1**: `ps -o rss=` for the standalone gateway process executing concurrent tasks registers 30.16MB (exceeding 30.0MB). `/healthz` flags this as `status: "degraded"`.
3. **Inference 1**: The process base footprint (29.5MB) leaves only ~500KB of headroom. Pipe buffers, JSON allocations, and thread pools allocated during concurrent task execution breach the 30.0MB ceiling.

4. **Premise 2 (Concurrency Contract)**: `PROJECT.md` Section 10 and `config.yaml.example` declare `max_concurrent_tasks: 5` for the async task dispatcher.
5. **Observation 2**: `hub/dispatcher.py` initializes only 1 consumer loop (`_worker_task`) and awaits `execute_task` serially.
6. **Inference 2**: All background tasks enqueued via HTTP webhooks run sequentially. A single slow task blocks all subsequent tasks in the queue, rendering `max_concurrent_tasks` inert.

7. **Premise 3 (Audit SSOT Contract)**: `PROJECT.md` Relational Schema 6 specifies that `executions` records OS process metadata (`pid`).
8. **Observation 3**: `hub/dispatcher.py` never passes `proc.pid` to `update_execution` or updates `executions.pid`.
9. **Inference 3**: Database audit trails cannot identify the OS PID of executed or timed-out processes.

---

## 3. Caveats

1. macOS process memory accounting: `ps -o rss=` reflects pages resident in physical RAM. On macOS, Python's memory allocator (`pymalloc`) holds allocated blocks in arenas rather than returning pages immediately to the OS via `madvise`/`vm_deallocate`. To stay under 30MB under load, the base footprint must be reduced (e.g., pruning unnecessary module imports or tuning GC collection during idle periods).
2. macOS `launchd` kickstart tasks (`launchd_job`): Requires elevated service privileges or registered user launch agents; tested via mock and CLI invocation rather than live system daemons.
3. In-process pytest runner RSS: Pytest itself loads ~80MB of test framework modules in the test process; the memory budget tests correctly isolate this by querying the standalone process (`subprocess.Popen`) and using the server's calibrated `/healthz` probe.

---

## 4. Conclusion & Required Remediations

**Verdict**: **REQUEST_CHANGES**

The core dispatcher and SSE streaming architectures demonstrate high robustness:
- Process group isolation cleanly kills stubborn processes and background grandchildren.
- SSE broker cleanly cleans up 20 concurrent subscribers upon abrupt disconnect with zero leaks.
- Slow consumer queues drop oldest frames non-blockingly.

However, three empirical defects must be resolved before Milestone M5 can be approved:

### Actionable Remediation Items for Worker:
1. **Remediate Memory Footprint under Load (Finding 1)**:
   - In `hub/cli.py` and server startup, tune Python garbage collection (`gc.collect()`, `gc.set_threshold(...)`) and call `ctypes.CDLL("libc.dylib").malloc_zone_pressure_relief(0, 0)` on macOS or trim unnecessary imports to lower the idle baseline from ~29.5MB to ~24-26MB. This ensures concurrent task execution stays comfortably below 30.0MB.
2. **Implement Concurrent Worker Pool in `TaskDispatcher` (Finding 2)**:
   - In `hub/dispatcher.py`, read `max_concurrent_tasks = self.config.dispatch.max_concurrent_tasks if self.config else 5`.
   - In `start()`, spawn `self._worker_tasks = [asyncio.create_task(self._worker_loop()) for _ in range(max_concurrent_tasks)]` instead of a single worker task.
   - In `stop()`, cancel and await all worker tasks in `self._worker_tasks`.
3. **Record Subprocess PID in `executions` (Finding 3)**:
   - In `hub/dispatcher.py`, pass `pid=proc.pid` to `self.db.update_execution(...)` (or update `executions` immediately after `create_subprocess_shell`).

---

## 5. Verification Method

To independently reproduce all empirical findings:

1. **Run the Standalone M5 Stress Runner**:
   ```bash
   python3 scripts/challenge_m5_dispatcher_sse_stress.py
   ```
   *Expected Result*: Demonstrates 5 passes, 1 failure (Finding 1: RSS breach), and logs Findings 2 and 3.

2. **Run the Adversarial Pytest Suite**:
   ```bash
   python3 -m pytest tests/stress/test_m5_adversarial_dispatcher_sse.py -v
   ```
   *Expected Result*: 5 passed, 3 failed (the 3 adversarial proof tests fail, proving Findings 1, 2, and 3).

3. **Invalidation Conditions (Post-Remediation Criteria)**:
   - `python3 -m pytest tests/stress/test_m5_adversarial_dispatcher_sse.py -v` passes **8 of 8 tests (100% pass)**.
   - `python3 scripts/challenge_m5_dispatcher_sse_stress.py` passes **6 of 6 checks (100% pass)**.
   - Standalone gateway process RSS remains strictly `< 30.0MB` at all times during concurrent execution.
