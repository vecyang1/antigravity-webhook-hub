# Progress — Challenger M5-2

Last visited: 2026-09-08T20:25:30+07:00

## Status: Empirical Verification Complete — 3 Findings Identified (REQUEST_CHANGES)
- [x] Inspect codebase: Dispatcher/Executor implementation, EventBroker, SSE routes, process management, and existing tests
- [x] Construct empirical stress tests:
  - [x] Test 1: Subprocess concurrency & cleanup (10 concurrent tasks: mix of fast, failing, timeout; SIGTERM/SIGKILL verification, exit codes in DB, log persistence, zero zombie/orphaned processes) -> PASS
  - [x] Test 2: Process group isolation (os.setsid) and grandchild termination on timeout -> PASS
  - [x] Test 3: SSE streaming resilience (abrupt subscriber disconnects on /events/stream and /tasks/{id}/stream, verify no subscriber leaks in EventBroker) -> PASS
  - [x] Test 4: SSE slow consumer queue overflow & drop oldest -> PASS
  - [x] Test 5: RSS memory profiling during and after high concurrency dispatch (< 30MB) -> FAIL (Standalone server process breaches 30MB budget: 30.16MB >= 30.0MB)
  - [x] Test 6: Dispatcher queue worker loop concurrency behavior -> FAIL (Enqueued tasks run strictly serially, max_concurrent_tasks=5 ignored)
  - [x] Test 7: Subprocess PID recorded in executions audit table -> FAIL (executions.pid remains NULL for all tasks)
- [x] Document findings and produce handoff report
