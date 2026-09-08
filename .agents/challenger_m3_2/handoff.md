# Empirical Challenge Report: Milestone M3 (Observability & Backlog Replay)

**Verdict**: `REQUEST_CHANGES`
**Challenger**: Challenger M3-2
**Target Components**: Observability routes (`hub/routes/observability.py`), Server-Sent Events backlog replay (`hub/routes/sse.py`), Event Broker (`hub/broker.py`).

---

## 1. Observation

### 1.1 Tool Commands and Results
1. **Empirical Challenge Suite Execution**:
   Command:
   ```bash
   python3 -m pytest tests/stress/test_m3_challenger.py -v
   ```
   Output:
   ```
   tests/stress/test_m3_challenger.py::test_challenge_healthz_normal_operation PASSED [  7%]
   tests/stress/test_m3_challenger.py::test_challenge_healthz_simulated_db_failure PASSED [ 15%]
   tests/stress/test_m3_challenger.py::test_challenge_ready_normal_and_failure PASSED [ 23%]
   tests/stress/test_m3_challenger.py::test_challenge_metrics_prometheus_text_format PASSED [ 30%]
   tests/stress/test_m3_challenger.py::test_challenge_metrics_json_format_parity PASSED [ 38%]
   tests/stress/test_m3_challenger.py::test_challenge_metrics_multi_status_accuracy PASSED [ 46%]
   tests/stress/test_m3_challenger.py::test_challenge_tasks_status_filtering PASSED [ 53%]
   tests/stress/test_m3_challenger.py::test_challenge_tasks_pagination_and_boundaries PASSED [ 61%]
   tests/stress/test_m3_challenger.py::test_challenge_task_detail_404_and_sanitization PASSED [ 69%]
   tests/stress/test_m3_challenger.py::test_challenge_task_detail_full_execution_logs_stdout_and_stderr FAILED [ 76%]
   tests/stress/test_m3_challenger.py::test_challenge_backlog_replay_chronological_order_and_live_events PASSED [ 84%]
   tests/stress/test_m3_challenger.py::test_challenge_backlog_replay_with_last_event_id_header PASSED [ 92%]
   tests/stress/test_m3_challenger.py::test_challenge_task_stream_nonexistent_returns_404 PASSED [100%]

   =================================== FAILURES ===================================
   _______ test_challenge_task_detail_full_execution_logs_stdout_and_stderr _______
   >           assert "stderr" in data, "Task detail must include stderr field"
   E           AssertionError: Task detail must include stderr field
   E           assert 'stderr' in {'task_id': 'tsk_both_streams', 'event_id': 'evt_streams', 'source': 'default', 'action_type': 'cli', ...}
   =========================== short test summary info ============================
   FAILED tests/stress/test_m3_challenger.py::test_challenge_task_detail_full_execution_logs_stdout_and_stderr
   ========================= 1 failed, 12 passed in 0.51s =========================
   ```

2. **Standalone Verifier**:
   Command:
   ```bash
   python3 scripts/verify_e2e.py
   ```
   Output:
   `VERIFICATION RESULT: ALL 9 CHECKS PASSED (Time: 0.25s, Gateway RSS: 27.78MB < 30MB)`

### 1.2 Direct Code Inspections
1. **`hub/routes/observability.py:263-288` (`handle_task_detail`)**:
   ```python
   # Enrich with stdout and stderr from execution_logs or executions table
   if "stdout" not in task_data or not task_data["stdout"]:
       if hasattr(db, "execute_read"):
           try:
               log_rows = await db.execute_read(
                   "SELECT chunk FROM execution_logs WHERE task_id = ? AND stream_type = 'stdout' ORDER BY log_id ASC",
                   (task_id,),
               )
               if log_rows:
                   task_data["stdout"] = "\n".join(r["chunk"] for r in log_rows)
               else:
                   exec_rows = await db.execute_read(
                       "SELECT stdout_tail, stderr_tail FROM executions WHERE task_id = ? ORDER BY attempt_number DESC LIMIT 1",
                       (task_id,),
                   )
                   if exec_rows:
                       task_data["stdout"] = exec_rows[0].get("stdout_tail") or ""
                       task_data["stderr"] = exec_rows[0].get("stderr_tail") or ""
                   else:
                       task_data["stdout"] = ""
                       task_data["stderr"] = ""
           except Exception as log_err:
               logger.debug("Failed to enrich task logs: %s", log_err)
               task_data["stdout"] = ""
               task_data["stderr"] = ""
   ```
2. **`hub/routes/sse.py:142-183` (`handle_task_stream`)**:
   - Lines 148-164: Replays `execution_logs` where `log_id > since_id` in `ORDER BY log_id ASC`.
   - Lines 166-182: Emits terminal `completed` frame if task is already terminal.
   - Lines 188-205: Streams live events and terminates on completion.

---

## 2. Logic Chain

1. **Defect in `handle_task_detail` (`GET /tasks/{task_id}`)**:
   - **Requirement**: The Authoritative User Request and M3 Dispatch specify:
     > "Verify `/tasks/{task_id}` returns 404 for nonexistent tasks and full execution logs for valid tasks."
   - **Flaw**:
     1. When a task generates stdout logs, `log_rows` is non-empty.
     2. `task_data["stdout"]` is populated with `"\n".join(r["chunk"] for r in log_rows)`.
     3. The handler executes NO query for `stream_type = 'stderr'` against `execution_logs`.
     4. Because `if log_rows:` evaluated to `True`, the `else` branch (which inspects `executions` table for `stderr_tail`) is skipped.
     5. Consequently, `task_data` completely omits the `"stderr"` key whenever `stdout` logs are present.
     6. If a task generates only stderr logs (and 0 stdout logs), `log_rows` is empty, falling into the `else` branch which reads `executions.stderr_tail` (truncated tail only), rather than querying `execution_logs` for full stderr chunks.
   - **Consequence**: Clients querying `GET /tasks/{task_id}` cannot observe stderr logs for tasks that produce both stdout and stderr, violating the contract of returning full execution logs.

2. **Verified Components**:
   - `/healthz`: Returns 200 with `status: ok`, `db: ok`, `database: {"status": "connected"}`, `system.memory_rss_mb < 30.0`, and `uptime_seconds`. Returns 503 with `status: degraded`, `db: error` when database connection is closed or locked.
   - `/ready`: Returns 200 when DB is responsive; returns 503 with `status: not_ready` when DB fails.
   - `/metrics`:
     - Default returns standard Prometheus 0.0.4 text format with metrics `webhook_hub_uptime_seconds`, `webhook_hub_process_memory_rss_bytes`, `webhook_hub_http_requests_total`, `webhook_hub_active_connections`, and `webhook_hub_tasks_total{status="..."}`.
     - `?format=json` and `Accept: application/json` return valid JSON with identical metric values.
     - Accurately tracks task count distributions across multiple statuses (`succeeded`, `failed`, `queued`).
   - `/tasks`: Correctly filters by status (`?status=succeeded`, `?status=failed`) and paginates (`limit`, `offset`) with defensive boundary sanitization against negative and non-numeric inputs.
   - `/tasks/{task_id}`: Correctly returns 404 for nonexistent tasks and resists path injection attempts.
   - `/tasks/{task_id}/stream`:
     - Chronological backlog replay: Replays historical logs in ascending order by `log_id ASC` before live events stream.
     - `Last-Event-ID` header: Correctly resumes playback strictly after the acknowledged event ID.
     - Completed tasks: Yields backlog followed by `completed` terminal event and cleanly closes stream.
     - Nonexistent tasks: Returns 404 immediately.

---

## 3. Caveats

- **SSE Idle Disconnect Detection**: In `hub/server.py`, when a client disconnects without sending further frames, the server's streaming loop is suspended on `await queue.get(timeout=15.0)`. Disconnection is detected when the 15-second heartbeat ping write fails (BrokenPipeError/ConnectionResetError), triggering clean unsubscription. This bounded 15-second latency is standard for HTTP/1.1 chunked SSE without a concurrent socket reader, but Worker M3 could consider monitoring socket EOF concurrently for instantaneous cleanup.

---

## 4. Conclusion

**Verdict**: `REQUEST_CHANGES`.

All observability and SSE stream features are functional, performant (RSS 27.78MB < 30MB), and resilient, EXCEPT for a defect in `hub/routes/observability.py:handle_task_detail`:
- `GET /tasks/{task_id}` fails to query and enrich `stderr` from `execution_logs`, omitting the `stderr` field whenever stdout is present.

### Required Changes for Worker M3:
In `hub/routes/observability.py` lines 263-288, replace the one-sided `stream_type = 'stdout'` logic with bidirectional enrichment:
```python
        if hasattr(db, "execute_read"):
            try:
                stdout_rows = await db.execute_read(
                    "SELECT chunk FROM execution_logs WHERE task_id = ? AND stream_type = 'stdout' ORDER BY log_id ASC",
                    (task_id,),
                )
                stderr_rows = await db.execute_read(
                    "SELECT chunk FROM execution_logs WHERE task_id = ? AND stream_type = 'stderr' ORDER BY log_id ASC",
                    (task_id,),
                )
                if stdout_rows or stderr_rows:
                    task_data["stdout"] = "\n".join(r["chunk"] for r in stdout_rows)
                    task_data["stderr"] = "\n".join(r["chunk"] for r in stderr_rows)
                else:
                    exec_rows = await db.execute_read(
                        "SELECT stdout_tail, stderr_tail FROM executions WHERE task_id = ? ORDER BY attempt_number DESC LIMIT 1",
                        (task_id,),
                    )
                    if exec_rows:
                        task_data["stdout"] = exec_rows[0].get("stdout_tail") or ""
                        task_data["stderr"] = exec_rows[0].get("stderr_tail") or ""
                    else:
                        task_data["stdout"] = ""
                        task_data["stderr"] = ""
            except Exception as log_err:
                logger.debug("Failed to enrich task logs: %s", log_err)
                task_data.setdefault("stdout", "")
                task_data.setdefault("stderr", "")
```

---

## 5. Verification Method

To independently reproduce this finding:
```bash
python3 -m pytest tests/stress/test_m3_challenger.py -k "test_challenge_task_detail_full_execution_logs_stdout_and_stderr" -v
```
- **Expected Failure**:
  `AssertionError: Task detail must include stderr field`
  `assert 'stderr' in {'task_id': 'tsk_both_streams', ...}`
- **Post-Fix Success Condition**:
  `python3 -m pytest tests/stress/test_m3_challenger.py -v` must yield `13 passed in <1.0s`.
