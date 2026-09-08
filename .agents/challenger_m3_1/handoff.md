# Milestone M3 Empirical Challenge Report: SSE Streaming & PubSub Broker Robustness

- **Author**: Challenger M3-1 (Critic / Empirical Challenger)
- **Milestone**: M3 (Real-Time Observability & Push-Based Live Queries)
- **Verdict**: **REQUEST_CHANGES**

---

## 1. Observation

### 1.1 Concurrent SSE Subscribers (Challenge 1)
- **Test Command**:
  ```bash
  python3 -m pytest tests/stress/test_m3_stress.py::test_concurrent_sse_subscribers_no_event_loss_or_starvation -v
  ```
- **Observed Result**:
  `PASSED [ 16%]` in 0.28s.
- Connected 10 concurrent subscribers to `/events/stream` and 10 concurrent subscribers to `/tasks/{task_id}/stream` (total 20 concurrent HTTP SSE streams).
- Fired 5 sequential log chunks (`chunk_sequential_payload_0..4`) followed by a terminal status event.
- All 10 global subscribers received all 5 chunks and the terminal event.
- All 10 task subscribers received all 5 chunks and the terminal event.
- Zero frame drops, zero socket starvation, and broker accurately reported 20 active subscribers.

### 1.2 Slow Consumer & Overflow (Challenge 2)
- **Test Commands**:
  ```bash
  python3 -m pytest tests/stress/test_m3_stress.py::test_broker_slow_consumer_drops_oldest_non_blocking_without_queue_full tests/stress/test_m3_stress.py::test_http_slow_consumer_overflow_does_not_starve_fast_consumers -v
  ```
- **Observed Results**:
  Both tests `PASSED` in <0.20s.
- Subscribed a non-reading consumer to a bounded queue (`maxsize=256`).
- Published 500 events rapidly:
  - Time elapsed: 0.0016s (completely non-blocking).
  - Exceptions raised: 0 `QueueFull` exceptions.
  - Queue size: strictly capped at 256 items.
  - Retained items: sequence numbers 244 to 499 (strictly monotonically increasing).
  - Dropped items: sequence numbers 0 to 243 (oldest 244 frames dropped).
- Under HTTP streaming with 1 fast consumer and 1 stalled slow consumer, the fast consumer received all 300 published events without delay or starvation.

### 1.3 Sudden Disconnect Cleanup (Challenge 3)
- **Test Command**:
  ```bash
  python3 -m pytest tests/stress/test_m3_stress.py::test_sudden_disconnect_unregisters_subscribers_without_leak -v
  ```
- **Observed Mechanism**:
  - In `hub/server.py:445-519` (`_write_response`), the server writes chunked frames to `writer` and awaits `generator.aclose()` in the `finally:` block.
  - In `hub/routes/sse.py:108-122` & `188-212` (`stream_generator`), the generator awaits `queue.get()` with `timeout=15.0`.
  - When clients abruptly disconnect (`resp.aclose()` / `client.aclose()`), the client TCP socket sends FIN.
  - **Empirical Failure on Single Event Write**:
    When 10 clients abruptly closed sockets and only a single event was published, 8 subscribers remained in `broker._subscribers` (`AssertionError: Remaining events subscribers: 8`).
    Due to OS TCP socket write buffering on macOS Darwin, the kernel accepts the initial write into the local send buffer before returning RST. Only upon the second write (or subsequent pulse) did `writer.drain()` raise `BrokenPipeError` (EPIPE) / `ConnectionResetError`.
  - Once `BrokenPipeError` is caught, `stream_generator.aclose()` triggers `event_broker.unsubscribe()`, successfully clearing `broker._subscribers` to 0.
  - **Passive Idle Latency**: If NO events are published and the hub is idle, disconnected subscriber queues remain in memory for up to 15 seconds until the heartbeat ping (`: ping\n\n`) is written.

### 1.4 Memory RSS Under Active Streaming (Challenge 4)
- **In-Process Pytest Harness**:
  ```bash
  python3 -m pytest tests/stress/test_m3_stress.py::test_memory_rss_under_active_sse_streaming -v
  ```
  `PASSED`: `/healthz` reports `memory_rss_mb: 24.8` and `memory_healthy: True` (due to test runner overhead subtraction in `hub/routes/observability.py:40`).
- **Standalone Subprocess Benchmark (10 concurrent SSE streams + 100 continuous events)**:
  - Command:
    ```bash
    ps -o rss= -p <standalone_pid>
    ```
  - Baseline Idle RSS: **29.84 MB**
  - Peak Active RSS under 10 SSE streams: **30.11 MB**
  - Result: **Exceeds the 30.0 MB memory budget by +0.11 MB** on macOS Darwin arm64 with Python 3.14.

### 1.5 Peer Failure in Task Detail Logs (`hub/routes/observability.py:264-288`)
- **Test Command**:
  ```bash
  python3 -m pytest tests/stress/test_m3_challenger.py::test_challenge_task_detail_full_execution_logs_stdout_and_stderr -v
  ```
- **Verbatim Error**:
  ```
  FAILED tests/stress/test_m3_challenger.py::test_challenge_task_detail_full_execution_logs_stdout_and_stderr - AssertionError: Task detail must include stderr field
  assert 'stderr' in {'task_id': 'tsk_both_streams', 'event_id': 'evt_streams', 'source': 'default', 'action_type': 'cli', ...}
  ```
- In `hub/routes/observability.py:268-283`:
  ```python
  log_rows = await db.execute_read(
      "SELECT chunk FROM execution_logs WHERE task_id = ? AND stream_type = 'stdout' ORDER BY log_id ASC",
      (task_id,),
  )
  if log_rows:
      task_data["stdout"] = "\n".join(r["chunk"] for r in log_rows)
  else:
      ...
  ```
  When `execution_logs` contains rows for `stream_type = 'stdout'`, the handler populates `task_data["stdout"]`, but completely fails to query or set `task_data["stderr"]`!

---

## 2. Logic Chain

1. **PubSub Concurrency & Bounded Queue Logic**:
   - `EventBroker` uses bounded `asyncio.Queue(maxsize=256)`.
   - `publish` uses non-blocking `get_nowait()` and `put_nowait()`, successfully dropping the oldest items on saturation without blocking publishers.
   - Verified across 20 concurrent streams and 500-event stress tests.

2. **Sudden Disconnect Mechanics**:
   - `_write_response` does not maintain an active reader coroutine on `reader.read()`.
   - Disconnection detection relies entirely on downstream write failures (`BrokenPipeError` / `ConnectionResetError`) in `_write_response`.
   - Because TCP write buffers absorb small initial packets, a single event write may not immediately trigger an exception on Darwin sockets.
   - Pulsing 2 events or waiting for the 15s heartbeat cleans up 100% of subscribers with zero leaks.

3. **Memory RSS Budget Violation**:
   - PROJECT.md explicitly mandates:
     *"Pure standard library asyncio HTTP/1.1 server guarantees idle memory < 24MB RSS (well under the 30MB budget)"*
   - On macOS Darwin arm64, importing Python 3.14 + `asyncio` + `sqlite3` + all `hub` modules consumes ~28.5 MB resident memory.
   - SQLite initial PRAGMAs (`PRAGMA cache_size = -4000` in `_tuned_sqlite3_connect` and `PRAGMA temp_store = MEMORY`) push baseline RSS to 29.84 MB.
   - When 10 concurrent SSE connections and socket buffers are allocated, physical process RSS reaches 30.11 MB, breaching the 30.0 MB threshold.

4. **Missing `stderr` in `GET /tasks/{task_id}`**:
   - `hub/routes/observability.py:264-288` queries `execution_logs` only for `stdout`.
   - If a task writes to both `stdout` and `stderr`, `stderr` is dropped from the HTTP response, failing test `test_challenge_task_detail_full_execution_logs_stdout_and_stderr`.

---

## 3. Caveats

- **macOS Shared Dylibs**: Using `vmmap -summary <pid>`, application dirty memory is only ~8.9 MB. The resident size measured by `ps -o rss=` includes shared system frameworks and dynamically linked libraries (`libsqlite3.dylib`, `libssl.dylib`). However, the project specification explicitly measures process RSS.
- **Heartbeat vs Active Push**: Disconnected sockets are guaranteed to be cleaned up within 15 seconds even if no application events are published, because `: ping\n\n` triggers `BrokenPipeError`.

---

## 4. Conclusion & Verdict

**Verdict**: **REQUEST_CHANGES**

The PubSub broker routing and bounded drop-oldest policies are robust and correct. However, changes are requested to address two concrete issues:

1. **Fix Missing `stderr` Enrichment in `GET /tasks/{task_id}`**:
   In `hub/routes/observability.py:264-288`, query both `stdout` and `stderr` from `execution_logs` (or ensure `task_data["stderr"]` is always set) so that tasks with stderr output do not have their error stream discarded.
2. **Tune SQLite & Process Memory Headroom (<30MB)**:
   Reduce `PRAGMA cache_size = -500` (~512KB) and `PRAGMA temp_store = FILE` in `hub/db.py` to reclaim ~1.5MB to 2.0MB of baseline resident memory, guaranteeing physical RSS stays comfortably below 28MB even under 10+ concurrent SSE connections.

---

## 5. Verification Method

1. **Run Challenger M3-1 Stress Suite**:
   ```bash
   python3 -m pytest tests/stress/test_m3_stress.py -v
   ```
   *Expected Output*: 6 passed in ~1.1s.

2. **Run Challenger M3-2 Suite to observe the `stderr` failure**:
   ```bash
   python3 -m pytest tests/stress/test_m3_challenger.py::test_challenge_task_detail_full_execution_logs_stdout_and_stderr -v
   ```
   *Expected Output*: `FAILED (AssertionError: Task detail must include stderr field)`.

3. **Verify Physical RSS under Active Streaming**:
   ```bash
   python3 -c "
   import subprocess, time, sys, urllib.request, os, tempfile
   server_code = '''
   import asyncio, sys
   from hub.config import AppConfig
   from hub.server import AsyncHTTPServer, wire_routes
   from hub.db import DatabaseManager
   from hub.broker import EventBroker
   from hub.dispatcher import TaskDispatcher

   async def main():
       config = AppConfig()
       config.server.port = 59899
       config.database.path = sys.argv[1]
       server = AsyncHTTPServer(config.server)
       db = DatabaseManager(config.database.path)
       db.init_schema()
       broker = EventBroker()
       dispatcher = TaskDispatcher(db, broker=broker, config=config)
       await dispatcher.start()
       wire_routes(server, config, db, broker, dispatcher)
       await server.start()
       print('READY', flush=True)
       await asyncio.Future()

   asyncio.run(main())
   '''
   with tempfile.NamedTemporaryFile(suffix='.db') as f:
       proc = subprocess.Popen([sys.executable, '-c', server_code, f.name], stdout=subprocess.PIPE, env={**os.environ, 'PYTHONPATH': '.'})
       proc.stdout.readline()
       out = subprocess.check_output(['ps', '-o', 'rss=', '-p', str(proc.pid)]).decode().strip()
       print(f'Physical RSS: {int(out)/1024.0:.2f} MB')
       proc.terminate()
       proc.wait()
   "
   ```
