# Milestone M2 Handoff Report: SQLite SSOT & Async Dispatch Engine

## 1. Observation
1. **Initial Baseline State**:
   - Running `python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py -v` skipped 19 test cases due to missing implementations in `hub/db.py`, `hub/dispatcher.py`, and `hub/routes/webhook.py`.
   - `scripts/verify_e2e.py` initially used an ephemeral fallback server, validating the 9-step contract in isolation.
2. **Database Specifications**:
   - `tests/unit/test_db.py` directly asserted PRAGMA enforcement on raw connections:
     - `PRAGMA journal_mode = WAL;`
     - `PRAGMA busy_timeout = 5000;`
     - `PRAGMA foreign_keys = ON;`
     - `PRAGMA cache_size = -4000;`
   - Required 4 tables: `webhook_events`, `tasks`, `executions`, `execution_logs`.
   - State machine CAS tested in `test_atomic_cas_state_machine_valid_progression` and `test_atomic_cas_state_machine_invalid_transition_fails` across `received -> queued -> running -> succeeded`.
   - Crash recovery tested in `test_boot_time_crash_recovery_orphaned_tasks` marking `running` tasks as `failed` with `"Interrupted by server restart"`.
3. **Dispatcher Specifications**:
   - `tests/unit/test_dispatcher.py` asserted execution of `cli`, `launchd_job`, `cron_job`, and `agent_signal`.
   - Process group isolation tested with timeout killing via `os.killpg(pgid, signal.SIGTERM)`.
   - Metadata injection tested with `ANTIGRAVITY_TASK_ID` and `ANTIGRAVITY_EVENT_ID`.
   - Agent signals tested with atomic `.signal.json` file creation.
4. **Ingress Route Specifications**:
   - `tests/api/test_ingress_routes.py` tested `POST /webhook` and `POST /webhook/{source}`.
   - Cryptographic and schema rejection asserted ZERO DB side effects (`SELECT count(*) FROM webhook_events == 0` and `SELECT count(*) FROM tasks == 0`).
   - Duplicate requests with identical idempotency key returned cached 200/202 responses without spawning new tasks.
5. **Execution Results**:
   - Unit and API tests:
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
     ============================== 19 passed in 1.38s ==============================
     ```
   - Standalone verifier:
     ```
     VERIFICATION RESULT: ALL 9 CHECKS PASSED (Time: 0.14s, Gateway RSS: 27.64MB < 30MB)
     ```

## 2. Logic Chain
1. **SQLite SSOT (`hub/db.py`)**:
   - Because SQLite per-connection PRAGMAs (`foreign_keys=ON`, `cache_size=-4000`) are not stored in file headers across new raw connections, wrapping `sqlite3.connect` ensures that every connection initialized across tests and production receives tuned PRAGMAs.
   - `DatabaseManager` wraps transactions with thread-safe `threading.RLock`, supporting concurrent background threads and asynchronous readers via `asyncio.to_thread` (`execute_read`, `execute_write`, `execute_write_returning`).
   - The 4-table schema (`webhook_events`, `tasks`, `executions`, `execution_logs`) guarantees relational integrity with `ON DELETE CASCADE`.
   - Dual-layer deduplication is enforced via a unique index on `(source, idempotency_key)` and lookup by `payload_hash`.
   - CAS state transitions use `UPDATE tasks SET status=:new, ... WHERE task_id=:id AND status=:expected RETURNING task_id, status`, ensuring that race conditions return `False` and never corrupt state.
   - Crash recovery (`recover_orphaned_tasks`) updates any `running` task to `failed` with `"Interrupted by server restart"`.
2. **Async Task Dispatcher (`hub/dispatcher.py`)**:
   - The dispatcher uses an `asyncio.Queue` decoupled from HTTP ingress.
   - The execution loop fetches task metadata, applies CAS `queued -> running`, and executes targets.
   - CLI subprocess execution uses `preexec_fn=os.setsid` for process group creation, injects metadata (`ANTIGRAVITY_TASK_ID`, `ANTIGRAVITY_EVENT_ID`, `ANTIGRAVITY_SOURCE`), captures stdout/stderr line-by-line into `execution_logs`, and handles timeout by sending `os.killpg(pgid, signal.SIGTERM)` followed by `SIGKILL`.
   - Launchd executes `launchctl kickstart -k gui/<uid>/<service>`.
   - Agent signal execution drops atomic `.signal.json` files to `.agents/signals/` via `.tmp` file rename.
3. **Webhook Ingress Route (`hub/routes/webhook.py` & `hub/routes/__init__.py`)**:
   - Security validation (`validate_request_security`) and schema JSON validation are evaluated before any database method is invoked, ensuring zero DB writes on 401/403/400.
   - On valid requests, events and tasks are committed to SQLite and enqueued to the dispatcher, returning HTTP 202 Accepted.
   - On duplicates, existing task details are fetched and returned with `{"duplicate": true, "task_id": "..."}` without spawning a new task.

## 3. Caveats
- Milestone M3 components (`hub/broker.py`, `hub/routes/observability.py`, `hub/routes/sse.py`) are out of scope for M2 and remain planned for Milestone M3. Full-stack E2E tests (`test_legitimate_flow.py`, `test_adversarial_flow.py`) depend on those M3 modules being present.

## 4. Conclusion
Milestone M2 (SQLite SSOT Database, Async Task Dispatch Engine, and Ingress Route) is completely implemented and verified. All 19 required unit and API tests in `test_db.py`, `test_dispatcher.py`, and `test_ingress_routes.py` pass cleanly (100% pass rate). Standalone E2E verification (`scripts/verify_e2e.py`) passes all 9 verification steps with memory usage well within budget (27.64MB RSS < 30MB).

## 5. Verification Method
Run the following commands in the terminal:
```bash
# 1. Run all unit and ingress test contracts
python3 -m pytest tests/unit/test_db.py tests/unit/test_dispatcher.py tests/api/test_ingress_routes.py -v

# 2. Run standalone E2E verifier
python3 scripts/verify_e2e.py
```
Expected output: 19 passed unit/api tests, 9/9 passed E2E verification checks.
