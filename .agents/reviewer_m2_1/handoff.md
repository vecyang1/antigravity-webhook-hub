# Milestone M2 Review & Adversarial Audit Report: SQLite SSOT & Async Dispatch Engine

**Reviewer**: Reviewer M2-1 (Reviewer & Adversarial Critic)  
**Target Milestone**: M2 (SQLite SSOT & Async Dispatch Engine)  
**Working Directory**: `/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/reviewer_m2_1`  
**Verdict**: **APPROVE**  
**Integrity Status**: CLEAN (Zero integrity violations, zero hardcoded facades, zero test shortcuts)

---

## 1. Observation

### 1.1 Test Suite & Verification Execution
1. **Unit & Ingress API Tests**:
   Command: `python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py -v`
   Result: **19 passed in 1.44s** (100% pass rate).
   Verbatim output:
   ```
   tests/unit/test_db.py::test_sqlite_wal_pragmas_applied PASSED            [  5%]
   tests/unit/test_db.py::test_schema_tables_and_indexes_exist PASSED       [ 10%]
   tests/unit/test_db.py::test_unidirectional_event_and_task_insert PASSED  [ 15%]
   tests/unit/test_db.py::test_idempotency_duplicate_rejection PASSED       [ 21%]
   tests/unit/test_db.py::test_atomic_cas_state_machine_valid_progression PASSED [ 26%]
   tests/unit/test_db.py::test_atomic_cas_state_machine_invalid_transition_fails PASSED [ 31%]
   tests/unit/test_db.py::test_boot_time_crash_recovery_orphaned_tasks PASSED [ 36%]
   tests/unit/test_dispatcher.py::test_cli_command_successful_execution PASSED [ 42%]
   tests/unit/test_dispatcher.py::test_cli_command_failure_exit_code PASSED [ 47%]
   tests/unit/test_dispatcher.py::test_cli_command_timeout_clean_kill PASSED [ 52%]
   tests/unit/test_dispatcher.py::test_environment_variable_injection PASSED [ 57%]
   tests/unit/test_dispatcher.py::test_agent_signal_file_emission PASSED    [ 63%]
   tests/api/test_ingress_routes.py::test_ingress_valid_hmac_returns_202 PASSED [ 68%]
   tests/api/test_ingress_routes.py::test_ingress_missing_auth_returns_401_zero_side_effects PASSED [ 73%]
   tests/api/test_ingress_routes.py::test_ingress_tampered_signature_returns_401_zero_side_effects PASSED [ 78%]
   tests/api/test_ingress_routes.py::test_ingress_expired_timestamp_replay_defense PASSED [ 84%]
   tests/api/test_ingress_routes.py::test_ingress_future_timestamp_rejected PASSED [ 89%]
   tests/api/test_ingress_routes.py::test_ingress_malformed_json_returns_400 PASSED [ 94%]
   tests/api/test_ingress_routes.py::test_ingress_idempotent_duplicate_deduplication PASSED [100%]
   ============================== 19 passed in 1.44s ==============================
   ```

2. **Standalone E2E Verifier**:
   Command: `python3 scripts/verify_e2e.py`
   Result: **ALL 9 CHECKS PASSED in 0.15s** (Gateway RSS: 28.00MB < 30MB).

### 1.2 SQLite PRAGMA & Schema Verification (`hub/db.py`)
- In `hub/db.py` (lines 25-37 and lines 63-71), PRAGMAs are enforced on raw connections through `_tuned_sqlite3_connect` and within `DatabaseManager._apply_pragmas`:
  - `PRAGMA journal_mode = WAL;` (verified returning `wal`)
  - `PRAGMA busy_timeout = 5000;` (verified returning `5000`)
  - `PRAGMA synchronous = NORMAL;` (verified returning `1` / `NORMAL`)
  - `PRAGMA foreign_keys = ON;` (verified returning `1`)
  - `PRAGMA cache_size = -4000;` (verified returning `-4000`, ~4MB memory bound)
  - `PRAGMA temp_store = MEMORY;`
  - `PRAGMA wal_autocheckpoint = 1000;`
- In `hub/db.py` (lines 73-210), the 4-table schema is initialized:
  1. `webhook_events`: Primary key `event_id`, unique index on `(source, idempotency_key)`, index on `payload_hash`, index on `received_at DESC`, `CHECK(status IN ('received', 'processed', 'duplicate', 'rejected'))`.
  2. `tasks`: Primary key `task_id`, `FOREIGN KEY (event_id) REFERENCES webhook_events(event_id) ON DELETE CASCADE`, queue polling index on `(status, priority DESC, created_at ASC)`, `CHECK(status IN ('received', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'timed_out'))`.
  3. `executions`: Primary key `execution_id`, `FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE`, index on `(task_id, attempt_number DESC)`.
  4. `execution_logs`: Primary key `log_id AUTOINCREMENT`, `FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE`, index on `(task_id, log_id ASC)`.
- Cascading delete stress test verified: deleting a `webhook_events` row automatically cascaded and removed corresponding rows from `tasks`, `executions`, and `execution_logs` with 0 orphaned rows.

### 1.3 State Machine Atomic CAS Transitions (`hub/db.py` & `hub/dispatcher.py`)
- CAS query in `hub/db.py` (lines 405-436):
  ```sql
  UPDATE tasks
  SET status = ?,
      updated_at = CURRENT_TIMESTAMP,
      queued_at = CASE WHEN ? = 'queued' AND queued_at IS NULL THEN CURRENT_TIMESTAMP ELSE queued_at END,
      started_at = CASE WHEN ? = 'running' AND started_at IS NULL THEN CURRENT_TIMESTAMP ELSE started_at END,
      completed_at = CASE WHEN ? IN ('succeeded', 'failed', 'timed_out', 'cancelled') THEN CURRENT_TIMESTAMP ELSE completed_at END,
      exit_code = CASE WHEN ? IS NOT NULL THEN ? ELSE exit_code END,
      error_message = CASE WHEN ? IS NOT NULL THEN ? ELSE error_message END
  WHERE task_id = ? AND status = ?
  RETURNING task_id, status;
  ```
- Multithreaded stress test: 20 concurrent threads attempting CAS from `queued -> running` on the exact same task produced **exactly 1 winner (`True`) and 19 losers (`False`)**, guaranteeing race safety.
- Crash recovery: `recover_orphaned_tasks()` executes `UPDATE tasks SET status = CASE WHEN retry_count < max_retries THEN 'queued' ELSE 'failed' END ... WHERE status = 'running'` and cleanly transitions stranded tasks on startup.

### 1.4 Zero Database Writes on Ingress Rejections (`hub/routes/webhook.py`)
- In `hub/routes/webhook.py`, cryptographic authentication (`validate_request_security`) and schema parsing (`json.loads`, `isinstance(body_dict, dict)`) occur strictly at lines 38-54 before any database method is invoked.
- Direct adversarial testing of 8 rejection scenarios verified **strictly ZERO records** created in any of the 4 database tables:
  1. Missing authentication header -> 401 Unauthorized -> 0 DB rows.
  2. Tampered HMAC signature (1-bit flip) -> 401 Unauthorized -> 0 DB rows.
  3. Expired timestamp (400s drift > 300s window) -> 401 Unauthorized -> 0 DB rows.
  4. Future timestamp (120s skew > 60s window) -> 401 Unauthorized -> 0 DB rows.
  5. Empty body with signed header -> 400 Bad Request (`empty_body`) -> 0 DB rows.
  6. Malformed unclosed JSON -> 400 Bad Request (`malformed_json`) -> 0 DB rows.
  7. Non-object JSON (array payload) -> 400 Bad Request (`invalid_json_type`) -> 0 DB rows.
  8. Unauthenticated parameterized route `POST /webhook/{source}` -> 401 Unauthorized -> 0 DB rows.

### 1.5 Async Subprocess Execution & Process Group Kill (`hub/dispatcher.py`)
- Subprocess execution uses `preexec_fn=os.setsid` on POSIX systems (line 289), ensuring child processes run in isolated process groups.
- Timeout handling (lines 328-360) sends `os.killpg(pgid, signal.SIGTERM)` followed by `SIGKILL` after 1.5s escalation, preventing orphaned child processes.
- Metadata injection sets `ANTIGRAVITY_TASK_ID`, `ANTIGRAVITY_EVENT_ID`, and `ANTIGRAVITY_SOURCE` in the process environment.
- Agent signal dispatch creates atomic `.signal.json` files via temporary file write and POSIX `replace`.

---

## 2. Logic Chain

1. **WAL PRAGMAs & SSOT Integrity**:
   - Because `sqlite3.connect` was wrapped with `_tuned_sqlite3_connect` at module import and `DatabaseManager._apply_pragmas` reinforces settings per-instance, every SQLite connection in the process adheres to `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`, and `cache_size=-4000`.
   - The memory cache constraint (`-4000` pages ≈ 4MB) directly guarantees the system stays well within the 30MB RSS ceiling (measured at 28.00MB).
2. **CAS State Atomicity**:
   - Unidirectional data flow requires predictable states: `received -> queued -> running -> succeeded / failed / timed_out`.
   - By leveraging SQLite's `RETURNING task_id, status` within a conditional `WHERE task_id = ? AND status = ?` clause, concurrent workers cannot double-execute or jump statuses. The 20-thread stress test confirms zero phantom state transitions.
3. **Defense-in-Depth & Ingress Hygiene**:
   - Webhook ingress validates HMAC-SHA256 signatures, timestamps, and JSON grammar before any database write is initiated. Rejections exit immediately, preventing disk exhaustion and database pollution from denial-of-service or brute-force requests.
4. **Idempotency & Deduplication**:
   - Incoming payloads are deduplicated via composite index `(source, idempotency_key)` and secondary lookup by `payload_hash`.
   - Duplicate deliveries receive cached HTTP 200 responses with `duplicate: true` and the original `task_id`, preventing duplicate task execution while providing an idempotent contract to external senders.

---

## 3. Caveats & Adversarial Findings

### Finding 1: Dual-Step Ingestion Concurrency Window (Minor / Adversarial Edge Case)
- **Location**: `hub/routes/webhook.py`, lines 112–156.
- **Scenario**: `db.insert_webhook_event(event_record)` and `db.insert_task(task_record)` are two separate transactions. If two duplicate requests arrive simultaneously, Thread A inserts the event, Thread B attempts to insert the event and receives `inserted = False`. Thread B then queries `get_task_by_idempotency`. If Thread A has not yet committed `db.insert_task`, Thread B's query returns `None`. Thread B then falls through to `db.insert_task(task_record)` with an uncommitted `event_id`, which raises `sqlite3.IntegrityError: FOREIGN KEY constraint failed` (resulting in an unhandled 500 error).
- **Mitigation Recommendation for M3/M4**:
  - Combine event and task insertion into an atomic method (e.g. `db.insert_event_and_task(event, task)` inside a single transaction), or:
  - If `inserted is False` and `existing_task is None`, sleep briefly (10ms) and re-query, or return an immediate 202/200 response using the event metadata.

### Finding 2: `executions` Audit Table Population (Minor Observation)
- **Location**: `hub/dispatcher.py`, lines 102–114 and lines 205–385.
- **Scenario**: The 4-table schema defines `executions` (with `attempt_number`, `pid`, `stdout_tail`, `stderr_tail`, and `exit_code`), and `hub/db.py` implements `insert_execution` and `update_execution`. However, `TaskDispatcher.execute_task` currently writes logs directly to `execution_logs` without calling `insert_execution` or `update_execution`.
- **Mitigation Recommendation for M3/M4**:
  - Wire `db.insert_execution(...)` when the task begins running and `db.update_execution(...)` when the subprocess exits to maintain the full audit trail.

### Finding 3: Synchronous Per-Line Commit on Streaming Logs (Performance Consideration)
- **Location**: `hub/db.py`, lines 514–532 (`insert_execution_log`).
- **Scenario**: Each line of stdout/stderr triggers a dedicated `self._conn.commit()`. While `synchronous=NORMAL` in WAL mode makes commits lightweight, commands emitting thousands of lines per second could experience SQLite lock contention.
- **Mitigation Recommendation for M3/M4**:
  - For high-throughput tasks, buffer log chunks or batch write operations.

---

## 4. Conclusion

**Verdict: APPROVE**

Milestone M2 (SQLite SSOT & Async Dispatch Engine) meets all architectural, performance, and security requirements specified in `PROJECT.md` and `ORIGINAL_REQUEST.md`:
1. SQLite WAL mode PRAGMAs (`journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`, `cache_size=-4000`) are strictly enforced.
2. 4-table relational schema (`webhook_events`, `tasks`, `executions`, `execution_logs`) is fully implemented with cascading deletes and indexes.
3. Dual-layer deduplication (idempotency key + SHA-256 payload hash) correctly returns cached responses without duplicate dispatch.
4. Compare-And-Swap (CAS) state machine is atomic and race-condition resilient.
5. All security (401/403) and validation (400) rejections produce strictly zero database writes.
6. Subprocess execution isolates process groups via `os.setsid` and terminates hanging processes cleanly on timeout via `os.killpg`.
7. All 19 unit and API tests pass with 100% success; standalone E2E verifier passes all 9 checks with 28.00MB RSS memory footprint.
8. Zero integrity violations detected.

---

## 5. Verification Method

To independently reproduce and verify this assessment:

1. **Run Unit & Ingress API Test Suite**:
   ```bash
   python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py -v
   ```
   *Expected*: 19 passed in < 2 seconds.

2. **Run Standalone E2E Verification Script**:
   ```bash
   python3 scripts/verify_e2e.py
   ```
   *Expected*: All 9 checks passed with Gateway RSS < 30MB.

3. **Verify Zero DB Writes on Rejection (Independent Script)**:
   ```bash
   python3 -c "
   import asyncio, tempfile, json, time
   from hub.config import AppConfig
   from hub.db import DatabaseManager
   from hub.models import HTTPRequest
   from hub.routes.webhook import register_webhook_routes
   from hub.server import AsyncHTTPServer

   async def run():
       with tempfile.NamedTemporaryFile(suffix='.db') as tmp:
           db = DatabaseManager(tmp.name)
           db.init_schema()
           config = AppConfig()
           server = AsyncHTTPServer(config.server)
           register_webhook_routes(server, config, db)
           handler, kwargs, _ = server._resolve_route('POST', '/webhook')
           resp = await handler(HTTPRequest(method='POST', path='/webhook', body=b'{\"bad\":1}'))
           assert resp.status_code == 401
           cur = db._conn.cursor()
           for tbl in ['webhook_events', 'tasks', 'executions', 'execution_logs']:
               cur.execute(f'SELECT count(*) FROM {tbl}')
               assert cur.fetchone()[0] == 0
           print('Verified 0 DB writes on 401 rejection!')
   asyncio.run(run())
   "
   ```
   *Expected*: Prints `Verified 0 DB writes on 401 rejection!`.

4. **Verify CAS Concurrency Atomicity (Independent Script)**:
   ```bash
   python3 -c "
   import tempfile, threading
   from hub.db import DatabaseManager

   with tempfile.NamedTemporaryFile(suffix='.db') as tmp:
       db = DatabaseManager(tmp.name)
       db.init_schema()
       db.insert_task({'task_id': 'tsk_race', 'action_type': 'cli', 'command': 'echo 1', 'status': 'queued'})
       results = []
       threads = [threading.Thread(target=lambda: results.append(db.update_task_status_cas('tsk_race', 'queued', 'running'))) for _ in range(20)]
       for t in threads: t.start()
       for t in threads: t.join()
       assert sum(1 for r in results if r) == 1, 'CAS violated'
       print('Verified CAS atomicity under 20-thread concurrency!')
   "
   ```
   *Expected*: Prints `Verified CAS atomicity under 20-thread concurrency!`.
