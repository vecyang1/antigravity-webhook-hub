## 2026-09-08T11:32:01Z

You are Worker M3 for Milestone M3 (Real-Time Observability & Push-Based Live Queries).
Your working directory is: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/worker_m3
Authoritative User Request: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/ORIGINAL_REQUEST.md
Project Specification: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/PROJECT.md
Existing Test Contracts:
- tests/api/test_observability_routes.py
- tests/api/test_sse_routes.py
- tests/e2e/test_legitimate_flow.py

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Scope & Exclusively Owned Files:
- hub/broker.py
- hub/routes/sse.py
- hub/routes/observability.py
- hub/server.py (wiring routes and broker)

Requirements:
1. Event Broker (`hub/broker.py`):
   - In-memory bounded PubSub event bus (`asyncio.Queue(maxsize=256)` per subscriber) with fast non-blocking publish.
   - Subscriber routing for specific `task_id` and global `/events/stream`.
   - Backlog replay support via `Last-Event-ID` or `?since_id=`, querying SQLite `execution_logs` or `webhook_events`.
   - Clean unsubscribe on client disconnect to prevent memory leaks.
2. Server-Sent Events (SSE) Routes (`hub/routes/sse.py`):
   - `GET /events/stream`: live event feed.
   - `GET /tasks/{task_id}/stream`: task-specific live log streaming.
   - Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`.
   - Format: `event: <name>\ndata: <json>\nid: <id>\n\n`.
   - Heartbeat: Send `: ping\n\n` comment every 15 seconds to keep connection alive through Cloudflare Tunnel and macOS TCP sockets.
3. Observability Endpoints (`hub/routes/observability.py`):
   - `GET /healthz`: Health check returning 200 OK with `{"status": "ok", "db": "ok", "memory_rss_mb": ...}` (or 503 on failure).
   - `GET /ready`: Readiness probe returning 200 OK when DB and server are ready.
   - `GET /metrics`: Prometheus text format by default, or JSON format if `format=json` query param or `Accept: application/json` is passed. Exposes task count by status, memory RSS bytes, request counts.
   - `GET /tasks`: List tasks with optional `status` filter, `limit`, `offset`.
   - `GET /tasks/{task_id}`: Authoritative task details from DB, returning 404 if not found.
4. Server Integration (`hub/server.py`):
   - Wire all routes into `AsyncHTTPServer`.
   - Connect `TaskDispatcher` log emission and state changes to `EventBroker.publish()`.
5. Verification:
   - Run `python3 -m pytest tests/api/test_observability_routes.py tests/api/test_sse_routes.py -v`.
   - Run `python3 scripts/verify_e2e.py`.
   - Ensure all tests pass cleanly and memory RSS remains <30MB.

Write your handoff report to:
/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/worker_m3/handoff.md
Send a completion message back when done.
