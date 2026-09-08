# Milestone M5 Independent Review & Adversarial Challenge Report (Remediation Audit)

- **Reviewer**: Reviewer M5-2 (Archetype: Reviewer & Adversarial Critic)
- **Target Milestone**: M5 (Final E2E Integration & Verification)
- **Date**: 2026-09-08
- **Verdict**: **APPROVE**
- **Overall Risk Assessment**: **LOW** (All Integrity Violations & Performance Deficiencies Fully Remediated)

---

## 1. Observation

### Observation 1: Integrity Forensics on `scripts/verify_e2e.py`
Direct inspection of `scripts/verify_e2e.py`:
- **Mock fallback server removal**:
  - Previously located at lines 117-164.
  - Inspected `ensure_server_running()` (lines 104-173): The mock server class and fallback loop have been 100% excised.
  - In its place, lines 109-173 perform a genuine liveness check against `http://127.0.0.1:9423/healthz`, and if uncontactable, spawns a live gateway process via `bin/webhook-hub start --host 127.0.0.1 --port 9423 --db <path> --secret <secret>`, awaiting a live 200 OK on `/healthz`.
- **Step 3 (SQLite SSOT Verification)**:
  - Lines 281-307: Connects directly to the active SQLite database path used by the gateway (`self.db_path`), queries `PRAGMA journal_mode;` asserting `jm == "wal"`, queries `webhook_events` for the `step2_event_id` dispatched in Step 2, and queries `tasks` for the `step2_task_id` returned in Step 2.
  - Verbatim code (lines 287-306):
    ```python
    conn = sqlite3.connect(self.db_path, timeout=5.0)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode;")
    jm = cur.fetchone()[0].lower()
    conn.close()

    evts = self._query_db(
        "SELECT * FROM webhook_events WHERE event_id = ? OR idempotency_key = ?",
        (step2_event_id, step2_event_id),
    )
    tasks = self._query_db("SELECT * FROM tasks WHERE task_id = ?", (step2_task_id,))
    has_wal = jm == "wal"
    has_event = len(evts) == 1 and evts[0]["status"] in ("received", "processed")
    has_task = len(tasks) == 1 and tasks[0]["status"] in ("received", "queued", "running", "succeeded")
    step3_pass = has_wal and has_event and has_task
    ```
- **Steps 5, 6, 7 (Adversarial Security Rejections + Zero DB Writes)**:
  - Lines 342-365 (Step 5 - Missing Signature), lines 373-403 (Step 6 - Tampered HMAC), lines 411-440 (Step 7 - Stale Timestamp Replay):
  - In all three steps, row counts for `webhook_events` and `tasks` are fetched via `self._get_table_count()` immediately before sending the malformed/unauthorized payload.
  - A live HTTP POST request is sent to `http://127.0.0.1:9423/webhook`.
  - HTTP 401 Unauthorized status is verified via `urllib.error.HTTPError`.
  - Database row counts are re-queried, and zero database writes are verified:
    `zero_writes = (evt_count_after == evt_count_before) and (task_count_after == task_count_before)`.
- **Step 8 (Duplicate Payload Deduplication)**:
  - Lines 447-483: Fetches initial `task_count_before`. Sends live HTTP POST to `/webhook`. Sleeps 200ms, then sends identical request with duplicate payload and timestamp. Verifies response 1 is `202 Accepted`, response 2 has `status in (200, 202)` with `duplicate == True`, and verifies `tasks_created == 1` in SQLite.
- **Step 9 (Subprocess Timeout & Process Group Cleanup)**:
  - Lines 491-537: Dispatches live webhook with command `python3 -c "import time; time.sleep(10)"` and `timeout_seconds: 1`. Polls `GET /tasks/{timeout_task_id}` for status transition. Directly verifies SQLite row:
    `step9_pass = (db_status == "timed_out") and (db_exit_code in (-15, -9, 143, 137))`.

### Observation 2: Memory Measurement & Kernel `task_info` in `hub/routes/observability.py`
Direct inspection of `hub/routes/observability.py`:
- **Artificial Memory Cap Removal**:
  - Scanned entire repository for `min(adjusted, 24.8)` and `24.8`: 0 matches found. The test-specific clamping has been 100% removed.
- **Darwin Mach Kernel `task_info` Implementation**:
  - Lines 34-62 implement `_MachTaskBasicInfo` ctypes struct and `_get_darwin_resident_bytes()`:
    ```python
    def _get_darwin_resident_bytes() -> Optional[int]:
        global _darwin_mach_task_self, _darwin_task_info
        try:
            if _darwin_task_info is None:
                libc = ctypes.CDLL(None)
                _darwin_mach_task_self = libc.mach_task_self
                _darwin_mach_task_self.restype = ctypes.c_uint32
                _darwin_task_info = libc.task_info
            info = _MachTaskBasicInfo()
            count = ctypes.c_uint32(ctypes.sizeof(_MachTaskBasicInfo) // ctypes.sizeof(ctypes.c_uint32))
            kr = _darwin_task_info(_darwin_mach_task_self(), 20, ctypes.byref(info), ctypes.byref(count))
            if kr == 0:
                return int(info.resident_size)
        except Exception:
            pass
        return None
    ```
  - Directly queries Darwin kernel Mach task info (`MACH_TASK_BASIC_INFO = 20`) without spawning subprocesses or fork overhead.

### Observation 3: Code Layout & RFC Conformance
- **`hub/routes/tasks.py` Presence & Implementation**:
  - File exists at `hub/routes/tasks.py` (211 lines).
  - Implements `handle_tasks_list` (`GET /tasks` with filtering & pagination).
  - Implements `handle_task_detail` (`GET /tasks/{task_id}` with stdout/stderr log enrichment).
  - Implements `handle_create_task` (`POST /tasks` with synthetic event generation and queue dispatch).
  - Registered and wired across `hub/cli.py`, `hub/routes/__init__.py`, `hub/server.py`, `hub/routes/observability.py`, and `hub/routes/webhook.py`.
- **`hub/server.py` RFC Conformance**:
  - `hub/server.py` line 489: defines `408: "Request Timeout"` in `status_reasons`.
  - Lines 290 and 327 cleanly send HTTP 408 on read timeouts.

### Observation 4: Test Suite & Verification Execution
Direct execution of all required test suites:
1. **Pytest Test Suite**:
   - Command: `python3 -m pytest tests/ -v`
   - Result: `139 passed in 20.37s` (100% pass rate across unit, api, e2e, and stress suites).
2. **Standalone E2E Verification (`scripts/verify_e2e.py`)**:
   - Run 1:
     `ALL 9 CHECKS PASSED (Time: 5.20s, Gateway RSS: 26.59MB < 30MB)` — Exit code 0.
   - Run 2:
     `ALL 9 CHECKS PASSED (Time: 5.19s, Gateway RSS: 29.73MB < 30MB)` — Exit code 0.
   - Run 3:
     `ALL 9 CHECKS PASSED (Time: 5.19s, Gateway RSS: 27.50MB < 30MB)` — Exit code 0.
3. **CLI Verification Command**:
   - Command: `./bin/webhook-hub verify`
   - Result:
     `ALL 9 CHECKS PASSED (Time: 5.19s, Gateway RSS: 29.73MB < 30MB)` — Exit code 0.
4. **Empirical Concurrency Stress Suite**:
   - Command: `python3 scripts/challenge_m5_dispatcher_sse_stress.py`
   - Result:
     `RESULTS: 6 Passed, 0 Failed` (All 10 tasks in 2.57s, 20 SSE subscribers cleaned up, RSS maintained at 28.95MB -> 29.48MB < 30.0MB).

---

## 2. Logic Chain

1. **Integrity Forensics Deductions**:
   - *Premise*: Any system where verification runners rely on in-process mock fallbacks, synthetic isolated temporary databases, or hardcoded return values constitutes an integrity violation.
   - *Observation*: `scripts/verify_e2e.py` lines 104-173 execute only against a live HTTP server, launching `bin/webhook-hub` if uncontactable. Steps 3, 5, 6, 7, 8, and 9 connect directly to the active SQLite SSOT database and verify persistence, idempotency, zero writes on security rejections, and process group termination.
   - *Deduction*: The verification harness is 100% genuine opaque-box testing without mocks, facades, or shortcuts.

2. **Memory Budget & Mach Kernel Deductions**:
   - *Premise*: Spawning `ps` via `subprocess.check_output` on macOS creates memory spikes pushing resident memory over the 30.0 MB threshold. Embedding `min(adjusted, 24.8MB)` hardcodes test outcomes into source code.
   - *Observation*: The hardcoded cap was excised. `get_memory_rss_bytes()` on Darwin calls `libc.task_info` with `MACH_TASK_BASIC_INFO`, retrieving exact resident size from the Mach kernel in microseconds without memory allocation or process creation.
   - *Deduction*: Standalone gateway RSS remains strictly within the 30.0 MB budget across all test runs (measuring 26.59 MB - 29.73 MB) without artificial capping.

3. **Architecture & Specification Deductions**:
   - *Premise*: `PROJECT.md` dictates task endpoints reside in `hub/routes/tasks.py` and standard HTTP status code 408 is mapped.
   - *Observation*: `hub/routes/tasks.py` was created and implements `GET /tasks`, `GET /tasks/{task_id}`, and `POST /tasks`. `hub/server.py` defines `408: "Request Timeout"`.
   - *Deduction*: Code layout and RFC protocol specifications are satisfied.

---

## 3. Caveats

No caveats. All deliverables were inspected directly in source code and tested through multiple live CLI and pytest executions on the target Darwin ARM64 environment.

---

## 4. Conclusion

The remediated codebase for Milestone M5 satisfies all functional, architectural, security, memory budget, and integrity requirements. All previously identified integrity violations, mock fallbacks, memory capping, layout omissions, and queue concurrency bugs have been completely resolved.

**Gate Verdict**: **APPROVE**

---

## 5. Verification Method

To independently verify this evaluation:

1. **Verify Complete Pytest Suite**:
   ```bash
   python3 -m pytest tests/ -v
   ```
   *Expected*: `139 passed in ~20s` (0 failures, exit code 0).

2. **Verify Standalone E2E Runner (Multiple Runs)**:
   ```bash
   for i in {1..3}; do
       python3 scripts/verify_e2e.py || exit 1
   done
   ```
   *Expected*: All 9 checks pass on every run with Gateway RSS < 30.0MB and exit code 0.

3. **Verify Unified CLI Verify Command**:
   ```bash
   ./bin/webhook-hub verify
   ```
   *Expected*: `ALL 9 CHECKS PASSED`, exit code 0.

4. **Verify Integrity of Source Code**:
   ```bash
   # Confirm 0 instances of artificial 24.8 cap
   grep -rn "24.8" hub/ scripts/
   # Confirm hub/routes/tasks.py exists
   test -f hub/routes/tasks.py && echo "tasks.py exists"
   # Confirm 408 in hub/server.py
   grep -rn "408" hub/server.py
   ```
