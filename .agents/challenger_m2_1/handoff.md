# Milestone M2 Empirical Challenge Report: SQLite SSOT & Async Dispatch Engine

**Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### A. Baseline Test Execution
1. Baseline unit and API tests executed via pytest:
   ```bash
   python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py -v
   ```
   **Result**: 19 passed in 1.34s.
   - WAL pragmas (`journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `cache_size=-4000`) verified in `tests/unit/test_db.py`.
   - Basic state machine CAS progression (`received -> queued -> running -> succeeded`) verified in `test_atomic_cas_state_machine_valid_progression`.
   - Basic security rejections producing zero DB writes verified in `tests/api/test_ingress_routes.py`.

2. High-concurrency WAL stress test executed with 100 concurrent workers (50 writers, 50 readers) in `tests/stress/test_m2_stress.py::test_sqlite_wal_concurrent_throughput_no_locks`:
   **Result**: PASSED. 0 `database is locked` or `OperationalError` errors observed. SQLite WAL handles concurrent reads and serialized writes smoothly.

3. Invalid request rejection matrix (missing HMAC, tampered signature, expired timestamp -301s, future timestamp +65s, malformed JSON, empty body, wrong Bearer) in `tests/stress/test_m2_stress.py::test_invalid_requests_zero_db_side_effects`:
   **Result**: PASSED. 0 rows created across all 4 tables (`webhook_events`, `tasks`, `executions`, `execution_logs`).

---

### B. Empirical Defect Discoveries & Verifications

#### Defect 1 (CRITICAL): Unhandled Foreign Key Violation Crash on Duplicate Retry
- **Location**: `hub/routes/webhook.py`, lines 112–156:
  ```python
  112: inserted = db.insert_webhook_event(event_record)
  113: if not inserted:
  114:     # Concurrent duplicate collision detected by UNIQUE constraint
  115:     existing_task = None
  116:     if hasattr(db, "get_task_by_idempotency"):
  117:         existing_task = db.get_task_by_idempotency(source, idemp_key)
  118:     if not existing_task and hasattr(db, "get_task_by_payload_hash"):
  119:         existing_task = db.get_task_by_payload_hash(source, payload_hash)
  120: 
  121:     if existing_task:
  122:         return HTTPResponse.json(...)
  ...
  143: task_record = {
  144:     "task_id": task_id,
  145:     "event_id": event_id,  # Newly generated UUID for this request
  ...
  156: db.insert_task(task_record)
  ```
- **Observed Behavior**:
  When an event row already exists in `webhook_events` (from a prior request that crashed before task creation, a partial failure, or a concurrent collision where the task is not yet committed), `inserted` evaluates to `False`.
  If `existing_task` is `None`, line 121 evaluates to `False`. The handler does not return an error or recover the existing event; instead, it falls through to line 156 and attempts to call `db.insert_task(task_record)` using the new, uninserted `event_id`.
- **Verbatim Error**:
  ```
  ERROR    hub.server:server.py:417 Handler exception: FOREIGN KEY constraint failed
  Traceback (most recent call last):
    File "hub/server.py", line 400, in _handle_client
      result = await result
    File "hub/routes/webhook.py", line 156, in handle_webhook
      db.insert_task(task_record)
    File "hub/db.py", line 317, in insert_task
      self._conn.execute(...)
  sqlite3.IntegrityError: FOREIGN KEY constraint failed
  HTTP Status: 500
  HTTP Response: {"error": "error", "message": "Internal Server Error: FOREIGN KEY constraint failed"}
  ```
- **Empirical Test**: `tests/stress/test_m2_stress.py::test_adversarial_finding1_idempotent_retry_on_orphan_event_must_not_500` FAILED with HTTP 500.

---

#### Defect 2 (HIGH): Stranded Queued Tasks Never Executed After Boot / Crash Recovery
- **Location**: `hub/dispatcher.py`, lines 55–68 & 86–96:
  ```python
  55: async def start(self) -> None:
  56:     if hasattr(self.db, "recover_orphaned_tasks"):
  57:         try:
  58:             recovered = self.db.recover_orphaned_tasks()
  ...
  65:     self._is_running = True
  66:     self._worker_task = asyncio.create_task(self._worker_loop())
  ```
  `hub/db.py`, line 446:
  ```sql
  UPDATE tasks
  SET status = CASE WHEN retry_count < max_retries THEN 'queued' ELSE 'failed' END,
      retry_count = CASE WHEN retry_count < max_retries THEN retry_count + 1 ELSE retry_count END,
      error_message = 'Interrupted by server restart',
  WHERE status = 'running'
  ```
- **Observed Behavior**:
  When `recover_orphaned_tasks()` runs, retryable tasks are transitioned to `status = 'queued'`. However, `TaskDispatcher.start()` **never** queries SQLite for tasks in `status = 'queued'` to rehydrate its in-memory `asyncio.Queue()`.
  Furthermore, any tasks that were in `status = 'queued'` prior to server shutdown/crash are also never loaded.
  `self.queue.qsize()` remains `0`.
  The tasks remain stranded in SQLite in `'queued'` state indefinitely and are never executed.
- **Verbatim Test Failure**:
  ```
  AssertionError: BUG CONFIRMED (Finding 2): Stranded 'queued' task was neither dispatched nor enqueued!
  Queue size=0, task status=queued
  assert ('queued' in ('running', 'succeeded') or 0 > 0)
  ```
- **Empirical Test**: `tests/stress/test_m2_stress.py::test_adversarial_finding2_boot_time_queued_tasks_must_be_rehydrated` FAILED.

---

#### Defect 3 (MEDIUM): Missing Audit Population in `executions` Table
- **Location**: `hub/dispatcher.py`, lines 160–386 (`execute_task`).
  `hub/db.py`, lines 461–512 (`insert_execution`, `update_execution`).
  `PROJECT.md`, line 21 (Feature 6: "4-table relational schema (webhook_events, tasks, executions, execution_logs)").
- **Observed Behavior**:
  While `hub/db.py` creates the `executions` table and implements `insert_execution` and `update_execution`, `TaskDispatcher` never invokes these methods. It only calls `_record_log` (`insert_log_chunk`).
  Executing a task from start to finish leaves the `executions` table completely empty (0 rows).
- **Verbatim Test Failure**:
  ```
  AssertionError: BUG CONFIRMED (Finding 3): Executions table has 0 rows. TaskDispatcher does not record execution attempts into the executions table.
  assert 0 > 0
  ```
- **Empirical Test**: `tests/stress/test_m2_stress.py::test_adversarial_finding3_executions_table_must_be_populated` FAILED.

---

#### Defect 4 (MEDIUM): Non-Atomic Task Parameter Parsing Leaking Orphan DB Rows
- **Location**: `hub/routes/webhook.py`, lines 112–156:
  ```python
  112: inserted = db.insert_webhook_event(event_record)
  ...
  151: "priority": int(body_dict.get("priority", 0)),
  152: "timeout_seconds": int(timeout_sec),
  ...
  156: db.insert_task(task_record)
  ```
- **Observed Behavior**:
  If a client sends an HMAC-valid payload containing non-integer values for `priority` or `timeout_seconds` (e.g. `{"priority": "urgent"}` or `{"timeout_seconds": "none"}`), the event is committed to `webhook_events` at line 112.
  Then at line 151, `int(body_dict.get("priority", 0))` raises `ValueError: invalid literal for int() with base 10: 'urgent'`.
  The request crashes with HTTP 500 (Internal Server Error) rather than HTTP 400 Bad Request.
  The database is left with 1 row in `webhook_events` and 0 rows in `tasks`. Any future request with that idempotency key now triggers Defect 1 (FOREIGN KEY crash on retry).
- **Verbatim Error**:
  ```
  ERROR    hub.server:server.py:417 Handler exception: invalid literal for int() with base 10: 'urgent'
  ValueError: invalid literal for int() with base 10: 'urgent'
  HTTP Status: 500
  ```
- **Empirical Test**: `tests/stress/test_m2_stress.py::test_adversarial_finding4_invalid_task_schema_must_reject_with_400_and_zero_db_writes` FAILED.

---

## 2. Logic Chain

1. **Premise**: In an SSOT architecture with unidirectional data flow (POST -> Validate -> SQLite -> 202 -> Dispatch), database transactions must be atomic, idempotent retries must never crash, and server crashes must not drop queued work.
2. **Analysis of Defect 1**:
   - `handle_webhook` assumes that if `insert_webhook_event` returns `False`, `get_task_by_idempotency` will always find an existing task.
   - If a prior insert was interrupted, or if validation failed between event and task insertion, `existing_task` is `None`.
   - Instead of raising an error or associating with the pre-existing event ID, `handle_webhook` generates a new `event_id` and calls `db.insert_task(task_record)`.
   - Because `tasks(event_id)` enforces foreign keys to `webhook_events(event_id)`, SQLite rejects the insertion with `FOREIGN KEY constraint failed`.
   - This invalidates the core idempotency contract: retries of interrupted webhooks crash permanently with 500.
3. **Analysis of Defect 2**:
   - SQLite holds the SSOT state. Tasks in `status = 'queued'` (either newly arrived before a restart or recovered by `recover_orphaned_tasks()`) reside in SQLite.
   - `TaskDispatcher` only reads from its in-memory `asyncio.Queue()`.
   - On boot, `TaskDispatcher.start()` never queries `SELECT task_id FROM tasks WHERE status = 'queued' ORDER BY priority DESC, created_at ASC` (even though index `idx_tasks_queue_poll` was specifically created for this).
   - Therefore, queued tasks are never executed following any restart or crash.
4. **Analysis of Defect 3**:
   - `PROJECT.md` mandates a 4-table relational model (`webhook_events`, `tasks`, `executions`, `execution_logs`).
   - Audit trail and multi-attempt tracking rely on `executions`.
   - Because `dispatcher.py` omits calls to `insert_execution` and `update_execution`, the table is orphaned and 100% unused.
5. **Analysis of Defect 4**:
   - Data validation for task configuration fields (`priority`, `timeout_seconds`, `max_retries`) is performed after the event row is inserted into SQLite rather than during request schema validation.
   - Type errors raise unhandled Python exceptions, creating orphan event rows and returning 500 instead of 400.

---

## 3. Caveats
- Baseline single-request happy path workflows and sequential duplicate deduplication pass as claimed by Worker M2.
- The single-process asyncio event loop serializes request handler execution ticks between non-await statements, which masks the duplicate race condition under synthetic in-process load unless partial DB states exist.
- No third-party web frameworks or heavy dependencies were introduced. Memory footprint remains well within budget (<30MB RSS).

---

## 4. Conclusion
While Milestone M2 passes the basic happy-path unit test suite, it contains **four confirmed defects** that compromise system reliability:
1. **Critical**: Duplicate retries crash with HTTP 500 and `sqlite3.IntegrityError: FOREIGN KEY constraint failed` whenever an event exists without a task.
2. **High**: Startup crash recovery and pending queued tasks are never rehydrated or executed by `TaskDispatcher`.
3. **Medium**: The `executions` audit table is never populated.
4. **Medium**: Task parameters are parsed post-DB-write, returning HTTP 500 and leaking orphan event rows.

**Recommendation**: **REQUEST_CHANGES**. Direct Worker M2 to address the remediation items below.

### Actionable Remediation Items for Worker M2:
1. **Atomic Event & Task Ingestion (`hub/db.py` & `hub/routes/webhook.py`)**:
   - Create an atomic method `insert_event_and_task(event_record, task_record)` in `hub/db.py` that inserts both within a single SQLite transaction (`BEGIN IMMEDIATE` ... `COMMIT`).
   - If `source, idempotency_key` already exists, query the existing event and task. If the event exists but task is missing, attach the new task to the *existing* `event_id`, never an uninserted UUID.
2. **Pre-DB Schema Validation (`hub/routes/webhook.py`)**:
   - Validate and convert `priority`, `timeout_seconds`, `max_retries` into integers *before* calling any database insert method. Return HTTP 400 Bad Request if invalid.
3. **Boot-Time Queue Hydration (`hub/dispatcher.py`)**:
   - In `TaskDispatcher.start()`, after calling `recover_orphaned_tasks()`, query SQLite:
     `SELECT task_id FROM tasks WHERE status = 'queued' ORDER BY priority DESC, created_at ASC`
     and enqueue each recovered task into `self.queue`.
4. **Execution Audit Population (`hub/dispatcher.py`)**:
   - In `TaskDispatcher.execute_task()`, insert a record into `executions` via `db.insert_execution(...)` when execution starts, and update it via `db.update_execution(...)` upon completion.

---

## 5. Verification Method

Run the newly created empirical stress and adversarial challenge test suite:
```bash
# 1. Run baseline stress tests (5 passing)
python3 -m pytest tests/stress/test_m2_stress.py -k "not adversarial" -v

# 2. Run adversarial bug proofs (demonstrating the 4 failures)
python3 -m pytest tests/stress/test_m2_stress.py -k "adversarial" -v

# 3. Run all existing unit and API test contracts (19 passing)
python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py -v
```

### Invalidation Conditions:
- If `test_adversarial_finding1_idempotent_retry_on_orphan_event_must_not_500` passes without throwing 500, Defect 1 is resolved.
- If `test_adversarial_finding2_boot_time_queued_tasks_must_be_rehydrated` passes (task status becomes running/succeeded and queue is populated), Defect 2 is resolved.
- If `test_adversarial_finding3_executions_table_must_be_populated` passes (`executions` count > 0), Defect 3 is resolved.
- If `test_adversarial_finding4_invalid_task_schema_must_reject_with_400_and_zero_db_writes` returns 400 with 0 DB writes, Defect 4 is resolved.
