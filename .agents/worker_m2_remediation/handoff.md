# Milestone M2 Remediation Report: SQLite SSOT & Async Dispatch Engine

**Status**: **COMPLETED / RESOLVED**
**Milestone**: M2 (SQLite SSOT & Async Dispatch Engine)
**Worker**: worker_m2_remediation

---

## 1. Observation

Direct empirical observations of baseline defects and resolution outcomes:

### A. Initial Defect Observations
1. **Defect 1 (Finding 1 - Foreign Key Violation on Duplicate Retry)**:
   - File: `hub/routes/webhook.py:112-156`
   - Test: `tests/stress/test_m2_stress.py::test_adversarial_finding1_idempotent_retry_on_orphan_event_must_not_500`
   - Verbatim failure:
     ```
     sqlite3.IntegrityError: FOREIGN KEY constraint failed
     HTTP Status: 500
     {"error": "error", "message": "Internal Server Error: FOREIGN KEY constraint failed"}
     ```
   - When a pre-existing orphan row in `webhook_events` collided on idempotency key and no task was associated, `handle_webhook` generated a new uninserted UUID for `event_id` and called `db.insert_task(task_record)`, violating foreign key `FOREIGN KEY (event_id) REFERENCES webhook_events(event_id)`.

2. **Defect 2 (Finding 2 - Stranded Queued Tasks Never Rehydrated on Startup)**:
   - File: `hub/dispatcher.py:55-68`
   - Test: `tests/stress/test_m2_stress.py::test_adversarial_finding2_boot_time_queued_tasks_must_be_rehydrated`
   - Verbatim failure:
     ```
     AssertionError: BUG CONFIRMED (Finding 2): Stranded 'queued' task was neither dispatched nor enqueued!
     Queue size=0, task status=queued
     assert ('queued' in ('running', 'succeeded') or 0 > 0)
     ```
   - On server reboot/startup, `TaskDispatcher.start()` did not query SQLite for tasks in `status = 'queued'` (either pre-existing or recovered from `recover_orphaned_tasks()`).

3. **Defect 3 (Finding 3 - Missing Audit Population in `executions` Table)**:
   - File: `hub/dispatcher.py:160-386`
   - Test: `tests/stress/test_m2_stress.py::test_adversarial_finding3_executions_table_must_be_populated`
   - Verbatim failure:
     ```
     AssertionError: BUG CONFIRMED (Finding 3): Executions table has 0 rows.
     TaskDispatcher does not record execution attempts into the executions table.
     assert 0 > 0
     ```
   - During task execution, `TaskDispatcher.execute_task` never called `db.insert_execution` or `db.update_execution`, leaving the table empty.

4. **Defect 4 (Finding 4 - Non-Atomic Schema Validation Leaking Orphan Rows)**:
   - File: `hub/routes/webhook.py:112-156`
   - Test: `tests/stress/test_m2_stress.py::test_adversarial_finding4_invalid_task_schema_must_reject_with_400_and_zero_db_writes`
   - Verbatim failure:
     ```
     ValueError: invalid literal for int() with base 10: 'urgent'
     HTTP Status: 500
     AssertionError: BUG CONFIRMED (Finding 4): Expected HTTP 400 for invalid priority, got 500
     ```
   - Parameter type conversion (`int(body_dict['priority'])`, `int(timeout_sec)`, `int(max_retries)`) was executed after inserting into `webhook_events`, crashing with HTTP 500 and leaving an orphan event row.

### B. Remediation Verification Results
Following surgical implementation across `hub/db.py`, `hub/dispatcher.py`, and `hub/routes/webhook.py`:
1. `python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py tests/stress/test_m2_stress.py -v`
   **Result**: 28 passed in 2.07s.
   - `test_adversarial_finding1_idempotent_retry_on_orphan_event_must_not_500`: PASSED
   - `test_adversarial_finding2_boot_time_queued_tasks_must_be_rehydrated`: PASSED
   - `test_adversarial_finding3_executions_table_must_be_populated`: PASSED
   - `test_adversarial_finding4_invalid_task_schema_must_reject_with_400_and_zero_db_writes`: PASSED
2. `python3 scripts/verify_e2e.py`
   **Result**: ALL 9 CHECKS PASSED (Time: 0.14s, Gateway RSS: 27.62MB < 30MB).

---

## 2. Logic Chain

1. **Defect 1 Resolution (Duplicate Retry on Orphan Event)**:
   - In `hub/db.py`: Added `get_event_by_idempotency` and `get_event_by_payload_hash` to find existing event records when `insert_webhook_event` returns `False`.
   - In `hub/routes/webhook.py`: When an event row already exists (`not inserted`):
     - Query `existing_event = db.get_event_by_idempotency(source, idemp_key)`.
     - Query associated task `existing_task = db.get_task_by_event_id(existing_event["event_id"])`.
     - If `existing_task` exists: return cached `{"status": ..., "task_id": ..., "event_id": ..., "duplicate": True}` with status 200.
     - If `existing_task` does not exist (orphan event): assign `event_id = existing_event["event_id"]` so `db.insert_task` references the existing event row, satisfying SQLite's foreign key constraint without 500 errors.

2. **Defect 2 Resolution (Startup Task Rehydration)**:
   - In `hub/db.py`: Added `get_queued_tasks()` to query `SELECT * FROM tasks WHERE status = 'queued' ORDER BY priority DESC, created_at ASC` leveraging index `idx_tasks_queue_poll`.
   - In `hub/dispatcher.py`: In `TaskDispatcher.start()`, after executing `recover_orphaned_tasks()`, query `get_queued_tasks()` from `self.db` and push each `task_id` into `self.queue`. All tasks stranded from prior runs or recovered from crash recovery are rehydrated and executed.

3. **Defect 3 Resolution (`executions` Audit Table Population)**:
   - In `hub/dispatcher.py`: In `execute_task(task_id)`:
     - Upon successful CAS transition to `running`, generate `execution_id = f"exec_{uuid.uuid4().hex[:16]}"` and insert record via `self.db.insert_execution(...)`.
     - Forward `execution_id` to `_record_log` calls so all log chunks in `execution_logs` link to the execution attempt.
     - Upon completion (or timeout/failure/exception), invoke `self.db.update_execution(execution_id=execution_id, status=exec_status, exit_code=exit_code, stdout_tail=..., stderr_tail=..., error_message=...)`.
   - In `hub/db.py`: Updated `update_execution` signature to accept optional `pid`.

4. **Defect 4 Resolution (Validation Order)**:
   - In `hub/routes/webhook.py`: Extracted and cast `priority`, `timeout_seconds`, and `max_retries` to integers *before* invoking any database method.
   - If casting fails (`ValueError` / `TypeError`), immediately return `HTTPResponse.error(..., status_code=400, reason="invalid_task_schema")`.
   - Zero rows are inserted into `webhook_events` or `tasks`.

5. **Memory Tuning**:
   - In `hub/db.py`: In `DatabaseManager._apply_pragmas`, set `conn.execute("PRAGMA cache_size = -2000;")` (2MB page cache).
   - This provides bounded memory consumption under high concurrency, maintaining Gateway RSS well below the 30MB limit (observed ~27.6MB).

---

## 3. Caveats

- `tests/e2e/test_legitimate_flow.py` and `tests/e2e/test_adversarial_flow.py` require Milestone M3 modules (`hub.broker`, `hub.routes.sse`, `hub.routes.observability`) which are scheduled for Milestone M3 implementation.
- Milestone M2 target verification test suites (`tests/unit/test_db.py`, `tests/unit/test_dispatcher.py`, `tests/api/test_ingress_routes.py`, `tests/stress/test_m2_stress.py`, and `scripts/verify_e2e.py`) are 100% passing.

---

## 4. Conclusion

All four empirical defects reported in Challenger M2-1 handoff report and the memory tuning requirement have been fully resolved:
1. Duplicate retry handling on orphan events no longer crashes with 500 foreign key error.
2. Queued tasks are cleanly rehydrated from SQLite into the dispatcher queue on startup.
3. Execution attempts and logs are fully recorded in the `executions` audit table.
4. Pre-DB schema validation rejects invalid task parameters with 400 Bad Request and zero database side effects.
5. SQLite cache size is tuned to -2000 (2MB page cache), maintaining comfortable RSS headroom below 30MB.

---

## 5. Verification Method

To independently verify the remediation:

1. **Run full M2 unit, API, stress, and adversarial challenge tests**:
   ```bash
   python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py tests/stress/test_m2_stress.py -v
   ```
   *Expected*: 28 passed.

2. **Run standalone 9-step E2E verification suite**:
   ```bash
   python3 scripts/verify_e2e.py
   ```
   *Expected*: ALL 9 CHECKS PASSED (Gateway RSS < 30MB).

3. **Inspect modified files**:
   - `hub/db.py`
   - `hub/dispatcher.py`
   - `hub/routes/webhook.py`

### Invalidation Conditions:
- If `test_adversarial_finding1_idempotent_retry_on_orphan_event_must_not_500` returns 500 or mentions FOREIGN KEY, Finding 1 is invalidated.
- If `test_adversarial_finding2_boot_time_queued_tasks_must_be_rehydrated` leaves task in status `queued`, Finding 2 is invalidated.
- If `test_adversarial_finding3_executions_table_must_be_populated` finds 0 rows in `executions`, Finding 3 is invalidated.
- If `test_adversarial_finding4_invalid_task_schema_must_reject_with_400_and_zero_db_writes` returns anything other than 400 or inserts rows, Finding 4 is invalidated.
