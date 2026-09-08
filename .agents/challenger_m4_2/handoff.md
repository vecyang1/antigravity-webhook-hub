# Empirical Challenge Report — Milestone M4: Unified Toolchain & Agent Discovery Contract

**Agent**: Challenger M4-2 (Critic & Specialist)  
**Target Milestone**: M4 (Unified Toolchain & Agent Discovery Contract)  
**Gate Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### 1.1 Error and Edge Cases

#### Case A: `stop` when no server is running
- **Command**:
  ```bash
  rm -f .webhook-hub.pid && ./bin/webhook-hub stop
  ```
- **Observed Output**:
  ```
  No active PID file found at '.webhook-hub.pid'. Gateway may not be running.
  ```
- **Exit Code**: `1`
- **Result**: **PASS**. Graceful message emitted to stdout, process exits cleanly with non-zero code, zero unhandled exceptions.

#### Case B: Stale PID handling on `status` and `stop`
- **Setup**: Injected non-existent PID `999999` into `.webhook-hub.pid`:
  ```bash
  echo 999999 > .webhook-hub.pid
  ```
- **Command 1 (`status`)**:
  ```bash
  ./bin/webhook-hub status
  ```
  **Output**:
  ```
  ==================================================
   Antigravity Webhook Hub Status: STOPPED
  ==================================================
    Stale PID file found pointing to dead process 999999.
  ==================================================
  ```
  **Exit Code**: `1`
- **Command 2 (`status --json`)**:
  ```bash
  ./bin/webhook-hub status --json
  ```
  **Output**:
  ```json
  {
    "running": false,
    "pid": 999999,
    "is_alive": false,
    "host": "127.0.0.1",
    "port": 9423,
    "status": "stopped"
  }
  ```
  **Exit Code**: `1`
- **Command 3 (`stop`)**:
  ```bash
  ./bin/webhook-hub stop
  ```
  **Output**:
  ```
  Process 999999 is not running. Cleaned up stale PID file.
  ```
  **Exit Code**: `0`
  **File State**: `.webhook-hub.pid` was removed cleanly.
- **Result**: **PASS**. Stale PID is correctly identified, reported in both human and JSON formats, and cleaned up on `stop`.

#### Case C: Starting server on an already bound port (CRITICAL DEFECT)
- **Setup**: Bound TCP socket to `127.0.0.1:9491`.
- **Command**:
  ```bash
  ./bin/webhook-hub start --port 9491 --pidfile .test-bound.pid
  ```
- **Observed Output (stderr)**:
  ```
  Traceback (most recent call last):
    File "/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/./bin/webhook-hub", line 74, in <module>
      sys.exit(main())
               ~~~~^^
    File "/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/hub/cli.py", line 845, in main
      return cmd_start(args)
    File "/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/hub/cli.py", line 307, in cmd_start
      return asyncio.run(run_server_foreground(config, pid_path))
             ~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/runners.py", line 205, in run
      return runner.run(main)
             ~~~~~~~~~~^^^^^^
    File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/runners.py", line 128, in run
      return self._loop.run_until_complete(task)
             ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^
    File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/base_events.py", line 720, in run_until_complete
      return future.result()
             ~~~~~~~~~~~~~^^
    File "/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/hub/cli.py", line 177, in run_server_foreground
      await server.start()
    File "/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/hub/server.py", line 566, in start
      self._server = await asyncio.start_server(
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^
      ...<4 lines>...
      )
      ^
    File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/streams.py", line 84, in start_server
      return await loop.create_server(factory, host, port, **kwds)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    File "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/asyncio/base_events.py", line 1638, in create_server
      raise OSError(err.errno, msg) from None
  OSError: [Errno 48] error while attempting to bind on address ('127.0.0.1', 9491): [errno 48] address already in use
  ```
- **PID File State**: `.test-bound.pid` **was left on disk** containing the PID of the aborted process!
- **Same Behavior on Privileged Port**: Running `./bin/webhook-hub start --port 80` similarly dumps an unhandled `PermissionError: [Errno 13] error while attempting to bind on address ('127.0.0.1', 80): [errno 13] permission denied` traceback and leaves a stale PID file.
- **Result**: **FAIL**. Direct violation of challenge requirement: *"must emit clean error and exit with non-zero code, without unhandled traceback"*.

#### Case D: `test-send` with `--tamper` flag
- **Command**:
  ```bash
  ./bin/webhook-hub test-send -p 9483 --tamper --secret verify_secret_32bytes_12345678
  ```
- **Observed Output**:
  ```
  Sending test webhook to http://127.0.0.1:9483/webhook...
  HTTP Error: 401 Unauthorized
  Error Details:
  {
    "error": "unauthorized",
    "message": "Computed HMAC SHA-256 does not match provided signature header",
    "reason": "signature_mismatch"
  }
  ```
- **Exit Code**: `1`
- **Result**: **PASS**. Signature tampering was immediately caught, returned HTTP 401 with JSON error explanation, and exited with 1.

#### Case E: `test-send` when server is down
- **Command**:
  ```bash
  ./bin/webhook-hub test-send -p 9483
  ```
- **Observed Output**:
  ```
  Sending test webhook to http://127.0.0.1:9483/webhook...
  Network Error: [Errno 61] Connection refused
  ```
- **Exit Code**: `1`
- **Result**: **PASS**. Handled cleanly without raw traceback, emitted informative network error, and exited with 1.

---

### 1.2 Entrypoint Consistency (`./bin/webhook-hub` vs `python3 -m hub`)

Tested identical argument arrays across all commands and invalid inputs:
- `['--help']` -> Exit 0, identical output.
- `['-h']` -> Exit 0, identical output.
- `['start', '--help']` -> Exit 0, identical output.
- `['stop', '--help']` -> Exit 0, identical output.
- `['status', '--help']` -> Exit 0, identical output.
- `['logs', '--help']` -> Exit 0, identical output.
- `['test-send', '--help']` -> Exit 0, identical output.
- `['verify', '--help']` -> Exit 0, identical output.
- `['--unknown-flag']` -> Exit 2, identical usage error.
- `['unknown-command']` -> Exit 0, identical help display.
- `['status']` -> Exit 1, identical output.
- `['status', '--json']` -> Exit 1, identical JSON structure.
- `['stop']` -> Exit 1, identical message.
- `['test-send', '--port', '9495']` -> Exit 1, identical network error.
- **Result**: **PASS**. 100% CLI argument, flag parsing, and output parity confirmed between executable wrapper and module entrypoint.

---

### 1.3 Memory RSS & Standalone E2E Verification (`scripts/verify_e2e.py`)

- **Standalone Script Run**:
  ```bash
  python3 scripts/verify_e2e.py
  ```
  **Output**:
  ```
  [STEP 1/9] Health Check (/healthz) & Memory Budget ..................... [PASS] (200 OK, RSS: 29.70MB < 30MB)
  [STEP 2/9] Legitimate Webhook Ingress (HMAC Signed) .................... [PASS] (202 Accepted, task_id: tsk_89304413e5f94fd3)
  [STEP 3/9] SQLite SSOT Verification (WAL Mode) ......................... [PASS] (WAL mode & busy_timeout verified)
  [STEP 4/9] SSE Stream Protocol & Heartbeat ............................. [PASS] (text/event-stream, X-Accel-Buffering: no)
  [STEP 5/9] Adversarial: Missing Signature Rejection .................... [PASS] (401 Unauthorized, 0 DB Writes)
  [STEP 6/9] Adversarial: Tampered HMAC Rejection ........................ [PASS] (401 Unauthorized, 0 DB Writes)
  [STEP 7/9] Adversarial: Stale Timestamp Replay Attack .................. [PASS] (401 Unauthorized, 0 DB Writes)
  [STEP 8/9] Adversarial: Duplicate Payload Deduplication ................ [PASS] (Deterministic deduplication hash verified)
  [STEP 9/9] Adversarial: Subprocess Timeout & Cleanup ................... [PASS] (Process group terminated cleanly via SIGTERM)
  VERIFICATION RESULT: ALL 9 CHECKS PASSED (Time: 0.15s, Gateway RSS: 29.92MB < 30MB)
  ```
- **Unit Test Suite**: 122/122 passed in 5.18s.
- **CRITICAL DAEMON MODE DISCREPANCY (HIGH SEVERITY FINDING)**:
  - While `scripts/verify_e2e.py` launches `sys.executable hub/__main__.py` with an empty `/tmp` database and passes at 29.70MB, running the standard CLI command:
    ```bash
    ./bin/webhook-hub start -d --port 9483
    ./bin/webhook-hub status -p 9483 --json
    ```
    Consistently yields:
    ```json
    "status": "degraded",
    "uptime_seconds": 1.16,
    "memory_rss_mb": 30.94,
    "healthz": {
      "status": "degraded",
      "system": {
        "memory_rss_mb": 30.94,
        "memory_healthy": false,
        "memory_budget_mb": 30.0
      }
    }
    ```
  - **Empirical Cause**:
    1. In `hub/cli.py` lines 249–265, daemon mode spawns:
       `[sys.executable, "-m", "hub", "start", ...]`
    2. Invoking `-m hub` forces Python to import `runpy`, `importlib.machinery`, and dynamic loader machinery.
    3. The default database `data/webhook_hub.db` already has schema tables and WAL shared memory pages mapped.
    4. Together, this pushes daemon process RSS to **30.77 MB – 30.94 MB**, immediately failing the 30.0 MB budget and putting `/healthz` into `DEGRADED` status during normal operational use.
    5. Proof: When daemon mode was tested by executing `bin/webhook-hub` directly instead of `-m hub`, RSS dropped to **29.62 MB** (`memory_healthy: true`).

---

## 2. Logic Chain

1. **Port Bind Traceback Root Cause (`hub/cli.py:118-121` & `hub/cli.py:177`)**:
   - In `run_server_foreground`:
     ```python
     if pid_path:
         pid_path.parent.mkdir(parents=True, exist_ok=True)
         pid_path.write_text(f"{pid}\n", encoding="utf-8")
     ...
     await server.start()
     ```
   - Socket binding occurs inside `await server.start()` (`hub/server.py:566`).
   - If the address is already in use (`Errno 48`) or unprivileged binding is rejected (`Errno 13`), `asyncio.start_server` raises `OSError`.
   - `run_server_foreground` does not catch `OSError` around `server.start()`.
   - In `cmd_start` (`hub/cli.py:307`):
     ```python
     try:
         return asyncio.run(run_server_foreground(config, pid_path))
     except (KeyboardInterrupt, SystemExit):
         return 0
     ```
     Only `KeyboardInterrupt` and `SystemExit` are caught.
   - `OSError` escapes all callers, dumping a 20-line traceback to stderr.
   - Crucially, the cleanup block in `run_server_foreground` (`finally: pid_path.unlink()`) is placed around `stop_event.wait()` (lines 195–206), which is never reached because `server.start()` threw before the `try:` block.
   - **Impact**: A user who tries to start the hub on a busy port receives an unhandled crash traceback AND a corrupted stale PID file that blocks subsequent daemon invocations with: `Error: Antigravity Webhook Hub is already running (PID: ...)`.

2. **Daemon Mode RSS Breach Root Cause (`hub/cli.py:249-265`)**:
   - Spawning `sys.executable -m hub` invokes `runpy` module execution. On macOS Darwin arm64 with Python 3.14, `runpy` allocates ~1.3MB of auxiliary module metadata.
   - Combined with active SQLite WAL shared memory mapping on `data/webhook_hub.db`, the gateway process uses ~30.9MB RSS.
   - The `/healthz` endpoint strictly evaluates `system.memory_healthy = (rss <= 30.0)`. Because 30.94 > 30.0, the hub starts in `DEGRADED` health status.
   - Executing the script path directly (`bin/webhook-hub` or `hub/__main__.py`) bypasses `runpy`, yielding 29.62MB RSS and keeping the hub healthy.

---

## 3. Caveats

- Tests were performed on macOS Darwin arm64 with Python 3.14.7.
- `scripts/verify_e2e.py` passes 9/9 checks when executed standalone because it uses a clean ephemeral database in `/tmp/e2e_verify.db` and launches the script without `-m hub`. The memory budget breach specifically surfaces during real daemon CLI execution (`webhook-hub start -d`) on the default repository database.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

Worker M4 must implement two targeted fixes before M4 can be approved:

1. **Handle Socket Binding Errors Gracefully (`hub/cli.py`)**:
   - In `run_server_foreground`, wrap `await server.start()` in a `try / except OSError as e:` block.
   - If an `OSError` is caught:
     - Print a clean, informative error message to `sys.stderr`, for example:  
       `Error: Unable to bind to http://{config.server.host}:{config.server.port}: {e}`
     - Immediately unlink `pid_path` if it was created so no stale PID file remains on disk.
     - Gracefully stop already started subsystems (`await dispatcher.stop()`, close database connections).
     - Return exit code `1` cleanly without printing any Python traceback.

2. **Fix Daemon Execution & Enforce <30.0MB RSS in Daemon Mode (`hub/cli.py`)**:
   - In `cmd_start` (daemon mode launcher, line 250):
     Instead of `cmd = [sys.executable, "-m", "hub", "start", ...]`, invoke the script directly to avoid `runpy` overhead:
     ```python
     hub_bin = Path(__file__).resolve().parent.parent / "bin" / "webhook-hub"
     entrypoint = str(hub_bin) if hub_bin.is_file() else str(Path(__file__).resolve().parent / "__main__.py")
     cmd = [sys.executable, entrypoint, "start", ...]
     ```
   - Ensure that after running `./bin/webhook-hub start -d`, `./bin/webhook-hub status --json` reports `"status": "ok"` and `"memory_healthy": true` (RSS < 30.0MB).

3. **Add Unit Regression Test (`tests/unit/test_cli.py`)**:
   - Add a unit test verifying that when `server.start()` raises `OSError` (e.g. port already bound), `cmd_start` emits a clean error message, unlinks the PID file, and returns exit code 1 without raising an unhandled exception.

---

## 5. Verification Method

To reproduce the findings and independently verify fixes:

```bash
# 1. Reproduce Bound Port Unhandled Traceback and Stale PID leak:
python3 -c '
import socket, subprocess, sys
from pathlib import Path
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 9491))
s.listen(1)
res = subprocess.run(["./bin/webhook-hub", "start", "--port", "9491", "--pidfile", ".test-bound.pid"], capture_output=True, text=True)
s.close()
print("STDERR:\n", res.stderr)
print("PID file left behind?", Path(".test-bound.pid").exists())
Path(".test-bound.pid").unlink(missing_ok=True)
'

# 2. Reproduce Daemon Memory Budget Breach:
./bin/webhook-hub start -d --port 9483 --pidfile .hub-test.pid
sleep 1
./bin/webhook-hub status -p 9483 --pidfile .hub-test.pid --json | grep -E "status|memory"
./bin/webhook-hub stop --pidfile .hub-test.pid

# Invalidation conditions:
# - `./bin/webhook-hub start --port <bound_port>` dumps any Python traceback.
# - Stale PID file is left on disk after bind failure.
# - `./bin/webhook-hub start -d` produces memory_rss_mb >= 30.0 MB or status == "degraded".
```
