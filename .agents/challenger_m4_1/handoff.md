# Empirical Challenge Report — Milestone M4: Unified Toolchain & Agent Discovery Contract

## 1. Observation

### 1.1 Project Test Suite
- Command: `python3 -m pytest tests/ -v`
- Result: 122 passed in 5.24s (100% pass rate for existing unit/stress tests).

### 1.2 Daemon Lifecycle & Verification on Isolated Test Port 9484
- **Start Daemon**:
  - Command: `./bin/webhook-hub start -d --port 9484 --pidfile .test-9484.pid`
  - Output:
    ```
    Starting Antigravity Webhook Hub in daemon mode on port 9484...
    Antigravity Webhook Hub started in daemon mode (PID: 98826, port: 9484).
    ```
  - Verification: `.test-9484.pid` was created with content `98826`, and `ps -p 98826` confirmed a live process.

- **Status (Human Formatted)**:
  - Command: `./bin/webhook-hub status -p 9484 --pidfile .test-9484.pid`
  - Output:
    ```
    ==================================================
     Antigravity Webhook Hub Status: RUNNING
    ==================================================
      PID:         98826
      Endpoint:    http://127.0.0.1:9484
      Status:      DEGRADED
      Uptime:      8.4s
      Memory RSS:  31.02 MB (Budget: < 30.0 MB)
      Database:    connected
    ==================================================
    ```

- **Status (Structured JSON)**:
  - Command: `./bin/webhook-hub status -p 9484 --pidfile .test-9484.pid --json`
  - Output:
    ```json
    {
      "running": true,
      "pid": 98826,
      "host": "127.0.0.1",
      "port": 9484,
      "status": "degraded",
      "uptime_seconds": 11.77,
      "memory_rss_mb": 31.02,
      "database": "connected",
      "healthz": {
        "status": "degraded",
        "db": "ok",
        "uptime_seconds": 11.77,
        "version": "1.0.0",
        "database": {
          "status": "connected"
        },
        "system": {
          "memory_rss_mb": 31.02,
          "memory_healthy": false,
          "memory_budget_mb": 30.0
        },
        "memory_rss_mb": 31.02
      }
    }
    ```

### 1.3 Webhook Dispatch & Log Query
- **Test Send (Normal Execution)**:
  - Command: `./bin/webhook-hub test-send -p 9484 --action cli --command "echo 'hello challenger'"`
  - Output:
    ```
    Sending test webhook to http://127.0.0.1:9484/webhook...
    HTTP Status: 202 Accepted
    Response JSON:
    {
      "status": "accepted",
      "event_id": "evt_2d811bbea8ad49f2",
      "task_id": "tsk_4de4ac76e4314387",
      "duplicate": false
    }
    ```

- **Log Query**:
  - Command: `./bin/webhook-hub logs --lines 5`
  - Output:
    ```
    [2026-09-08 12:24:48] [tsk_197fc73c43954a5c] [stdout] hello from m4 test
    [2026-09-08 12:27:52] [tsk_4de4ac76e4314387] [stdout] hello challenger
    ```
  - Filtered by task: `./bin/webhook-hub logs --task tsk_4de4ac76e4314387` returned `[2026-09-08 12:27:52] [stdout] hello challenger`.

- **Adversarial Webhook Ingress via CLI**:
  - `--tamper`: Rejected with `401 Unauthorized` and `reason: signature_mismatch`.
  - `--stale-timestamp`: Rejected with `401 Unauthorized` and `reason: timestamp_expired`.
  - Malformed payload: Exited with code 1 (`Error: Payload is not valid JSON`).

### 1.4 Clean Teardown
- Command: `./bin/webhook-hub stop --pidfile .test-9484.pid`
- Output:
  ```
  Sending SIGTERM to process 98826...
  Antigravity Webhook Hub (PID: 98826) stopped gracefully.
  ```
- Verification:
  - `ps -p 98826` confirmed process terminated.
  - `test -f .test-9484.pid` confirmed PID file was cleanly deleted.
  - `lsof -i :9484` returned exit code 1 (port 9484 successfully released).

### 1.5 Standalone Verification Failure (`./bin/webhook-hub verify`)
- Command: `./bin/webhook-hub verify`
- Exit Code: **1** (FAILED: asserted return code 0)
- Verbatim Output:
  ```
  Executing verification runner: /opt/homebrew/opt/python@3.14/bin/python3.14 /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/scripts/verify_e2e.py --host 127.0.0.1 --port 9423 --secret verify_secret_32bytes_12345678

  ================================================================================
  ANTIGRAVITY WEBHOOK HUB — STANDALONE E2E VERIFICATION SUITE
  Target: http://127.0.0.1:9423 | OS: darwin | Python: 3.14.7
  ================================================================================

  No active hub detected at http://127.0.0.1:9423. Starting ephemeral hub...
  [STEP 1/9] Health Check (/healthz) & Memory Budget ..................... [FAIL] (200 OK, RSS: 30.06MB < 30MB)
  [STEP 2/9] Legitimate Webhook Ingress (HMAC Signed) .................... [PASS] (202 Accepted, task_id: tsk_6a6661111fa54263)
  [STEP 3/9] SQLite SSOT Verification (WAL Mode) ......................... [PASS] (WAL mode & busy_timeout verified)
  [STEP 4/9] SSE Stream Protocol & Heartbeat ............................. [PASS] (text/event-stream, X-Accel-Buffering: no)
  [STEP 5/9] Adversarial: Missing Signature Rejection .................... [PASS] (401 Unauthorized, 0 DB Writes)
  [STEP 6/9] Adversarial: Tampered HMAC Rejection ........................ [PASS] (401 Unauthorized, 0 DB Writes)
  [STEP 7/9] Adversarial: Stale Timestamp Replay Attack .................. [PASS] (401 Unauthorized, 0 DB Writes)
  [STEP 8/9] Adversarial: Duplicate Payload Deduplication ................ [PASS] (Deterministic deduplication hash verified)
  [STEP 9/9] Adversarial: Subprocess Timeout & Cleanup ................... [PASS] (Process group terminated cleanly via SIGTERM)

  ================================================================================
  VERIFICATION RESULT: 1/9 CHECKS FAILED (Time: 0.15s, Gateway RSS: 30.23MB)
  ================================================================================
  ```

- Against running daemon on port 9423:
  ```
  [STEP 1/9] Health Check (/healthz) & Memory Budget ..................... [FAIL] (200 OK, RSS: 35.34MB < 30MB)
  VERIFICATION RESULT: 1/9 CHECKS FAILED (Time: 0.04s, Gateway RSS: 37.09MB)
  ```

---

## 2. Logic Chain

1. **Mandate**: `PROJECT.md` specifies a strict memory constraint: "Guarantees <30MB RSS memory budget and strict 0% idle CPU on macOS". The prompt instructions specifically state:
   - "4. Run `./bin/webhook-hub verify` and assert return code is 0."
   - "5. Measure memory RSS during tests to ensure < 30MB."
   - "6. Issue a clear gate verdict: APPROVE or REQUEST_CHANGES."
2. **Worker Claim vs Empirical Reality**:
   - Worker M4 claimed: `Gateway RSS: 29.92MB < 30MB` and `scripts/verify_e2e.py: ALL 9/9 CHECKS PASSED with clean exit code 0`.
   - In empirical testing across multiple test runs:
     - Standalone ephemeral server measured RSS: **30.06 MB to 30.27 MB**.
     - Daemon mode (`./bin/webhook-hub start -d`) measured RSS: **30.73 MB to 31.02 MB**.
     - Active daemon processing requests measured RSS: **35.34 MB to 37.09 MB**.
     - `./bin/webhook-hub verify` consistently exits with **return code 1** due to `[STEP 1/9] Health Check (/healthz) & Memory Budget [FAIL]`.
3. **Root Cause Analysis**:
   - **SQLite PRAGMA Cache Allocation (`hub/db.py`)**:
     Lines 32 and 69 of `hub/db.py` apply `PRAGMA cache_size = -4000;` and `PRAGMA cache_size = -2000;` directly inside `sqlite3.connect`. On macOS Darwin, allocating 2MB to 4MB of SQLite page cache at connect time increases RSS by over 1.5MB immediately upon database initialization.
   - **Missing `_LightweightSSL` in `hub/cli.py`**:
     `bin/webhook-hub` and `hub/__main__.py` install `_LightweightSSL`, but `hub/cli.py` omits it. Any code path that imports or touches `ssl` (e.g. `urllib.request` creating default HTTPS contexts) triggers eager loading of OpenSSL/LibreSSL dynamic libraries (~2.25MB).
   - **Lifetime High-Water Mark in Observability Route (`hub/routes/observability.py`)**:
     Line 33 uses `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`. On Darwin arm64, `ru_maxrss` is the permanent high-water peak in bytes and never decreases. Even if garbage collection frees objects, `ru_maxrss` stays frozen at its historical peak, causing `/healthz` to report `status: degraded` and `memory_healthy: false` permanently.
   - **Subprocess Spawning in Dispatcher (`hub/dispatcher.py`)**:
     Using `preexec_fn=os.setsid` on POSIX forces Python's subprocess runner to use `fork()` instead of `posix_spawn()`, copying dirty page tables and temporarily inflating process memory when executing tasks.

---

## 3. Caveats

- Functional implementation of the CLI toolchain (`start`, `stop`, `status`, `logs`, `test-send`, `verify`) is robust, well-structured, and handles edge cases cleanly (graceful shutdown, PID file unlinking, JSON parsing, HMAC signing/tampering, log retrieval).
- Unit tests (`tests/unit/test_cli.py`) and integration tests (`tests/`) all pass (122/122).
- The failure is isolated specifically to the strict `< 30.0 MB` RSS memory budget enforcement and the resulting non-zero exit code of `./bin/webhook-hub verify`.
- No other caveats.

---

## 4. Conclusion

**Gate Verdict: REQUEST_CHANGES**

The work product delivered in Milestone M4 meets all behavioral and functional specifications for the unified CLI and discovery contracts, but fails the required hard gating assertion:
1. `./bin/webhook-hub verify` fails with return code **1** (asserted 0).
2. Memory RSS exceeds the strict **30.0 MB** limit (measured at 30.06MB - 31.02MB idle, 35.34MB under load).
3. The gateway self-reports `status: degraded` on `/healthz` due to `memory_healthy: false`.

### Remediation Plan for Worker:
1. **Reduce SQLite cache size**: In `hub/db.py`, replace `PRAGMA cache_size = -4000;` and `-2000;` with `PRAGMA cache_size = -64;` (or `-32;`), apply `PRAGMA shrink_memory;` after schema init and request cycles.
2. **Add `_LightweightSSL` to `hub/cli.py`**: Ensure all execution entry points benefit from lazy SSL dynamic library loading.
3. **Tune RSS metric in `hub/routes/observability.py`**: Query current RSS via `ps -o rss=` (consistent with `hub/cli.py` and `scripts/verify_e2e.py`) rather than `ru_maxrss` peak, or adjust memory cleanup routines so the peak stays strictly under 28.0MB.
4. **Ensure `./bin/webhook-hub verify` exits with code 0**.

---

## 5. Verification Method

To independently reproduce this finding:

```bash
# 1. Run the verify subcommand and inspect the exit code
./bin/webhook-hub verify
echo "Exit Code: $?"
# Expected by prompt: 0
# Observed: 1 (Step 1 fails: RSS >= 30.0 MB)

# 2. Start daemon on test port 9484 and inspect status
./bin/webhook-hub start -d --port 9484 --pidfile .test-9484.pid
./bin/webhook-hub status -p 9484 --pidfile .test-9484.pid --json
# Observe: "status": "degraded", "memory_healthy": false, "memory_rss_mb": > 30.0

# 3. Clean up test daemon
./bin/webhook-hub stop --pidfile .test-9484.pid
```
