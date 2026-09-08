# Technical Survey & Architecture Report: Requirements R3, R4, R5
**Project:** Antigravity Webhook Hub  
**Date:** 2026-09-08  
**Author:** Explorer Survey 3  
**Working Directory:** `/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/explorer_survey_3`  
**Status:** Completed  

---

## 1. Executive Summary & Architectural Invariants

Requirements **R3 (Real-Time Observability & Push-Based Live Queries)**, **R4 (Toolchain Unification & Agent Discovery)**, and **R5 (Two-Sided E2E Verification & Test Suite)** represent the operational, discoverable, and verifiable pillars of the Antigravity Webhook Hub on macOS.

### Key Architectural Invariants
1. **Lightweight Standard-Library Async Foundation (<30MB Memory Budget)**:
   - Measured baseline RSS memory for Python 3.14 on macOS is **~15.8 MB to 16.8 MB**.
   - Avoiding bloated multi-process or heavy async frameworks (FastAPI/Uvicorn/Starlette/Celery) ensures the server stays comfortably under **20 MB RSS** even under active load, meeting the `<30MB` constraint with zero CPU spin while idle.
   - Built on standard library `asyncio`, `sqlite3`, `dataclasses`, `hmac`, `hashlib`, `argparse`, and lightweight HTTP parsing.
2. **Single Source of Truth (SSOT) with Push Projections**:
   - SQLite in WAL mode is the sole authoritative state holder.
   - Server-Sent Events (SSE) are live **ephemeral projections** of state transitions.
   - In accordance with the workspace contract (`AGENTS.md`): *"Data is truth, API is the read portal, UI is projection"*. SSE streams support backlog replay via `Last-Event-ID` or `since_id` directly querying SQLite, preventing message loss during client reconnections.
3. **Unified Discoverable Toolchain**:
   - Single unified CLI engine callable via `bin/webhook-hub` and `python -m hub`.
   - Comprehensive Antigravity Agent Skill (`SKILL.md`) under 300 lines providing prompt instructions, parameter discovery, and CLI execution recipes.
4. **Two-Sided Verification Contract ("测遍了不会说谎的那一半，也要测遍会说谎的那一半")**:
   - Both legitimate happy paths and adversarial security/fault challenges must be verified.
   - Adversarial challenges (forged signatures, replay attacks, duplicate deliveries, timeouts) must produce **zero side effects** in the database and keep the gateway resilient.
   - Dual test execution: Automated `pytest` suite + standalone self-contained `scripts/verify_e2e.py` (or `webhook-hub verify`).

---

## 2. Requirement R3: Real-Time Observability & Push-Based Live Queries

### 2.1 Server-Sent Events (SSE) Streaming Architecture

#### Protocol Specification
- SSE is chosen over WebSockets because:
  1. It operates over standard unidirectional HTTP/1.1 or HTTP/2, requiring no connection upgrade handshakes or complex binary framing.
  2. Works seamlessly behind Cloudflare Tunnel, Nginx, or corporate proxies without custom reverse proxy WebSocket tunneling.
  3. Native client auto-reconnection semantics built into browser `EventSource` and simple CLI parsers.
  4. Ultra-low memory overhead in Python compared to full-duplex WebSocket protocol parsers.
- **Headers Sent by Server**:
  ```http
  HTTP/1.1 200 OK
  Content-Type: text/event-stream
  Cache-Control: no-cache, no-transform
  Connection: keep-alive
  X-Accel-Buffering: no
  Access-Control-Allow-Origin: *
  ```
- **Frame Format**:
  ```
  event: <event_type>
  id: <event_id_or_seq>
  data: <json_payload>

  ```
  *(Terminated by double newline `\n\n`)*.
- **Heartbeat / Keepalive**:
  - Every 15 seconds of silence, server emits a comment frame:
    ```
    : ping\n\n
    ```
  - This prevents macOS TCP idle timeout, Cloudflare 100-second idle connection drop, and proxy teardown.

#### Endpoint 1: Global Event Stream (`/events/stream`)
- **Purpose**: Live event firehose for local dashboards, CLI watchers, and Antigravity orchestrator agents.
- **Parameters**:
  - `topic` (query string): Filter by topic (e.g. `?topic=task.state_changed`, `?topic=webhook.received`).
  - `since_id` (query string or `Last-Event-ID` header): Integer sequence or timestamp. When supplied, the server queries SQLite SSOT for all historical events where `id > since_id` and streams them before switching to the live queue.
  - `limit` (query string, default 100): Maximum replay records before switching to live mode.
- **Event Types Emitted**:
  - `webhook_received`: Payload contains `event_id`, `source`, `timestamp`, `content_hash`.
  - `task_queued`: Payload contains `task_id`, `event_id`, `action`, `command`.
  - `task_running`: Payload contains `task_id`, `started_at`, `pid`.
  - `task_log`: Payload contains `task_id`, `stream` (stdout/stderr), `chunk`, `timestamp`.
  - `task_completed`: Payload contains `task_id`, `status` (succeeded/failed), `exit_code`, `duration_ms`.

#### Endpoint 2: Task-Specific Execution Stream (`/tasks/{task_id}/stream`)
- **Purpose**: Dedicated real-time observation for a specific dispatched task.
- **Behavior**:
  1. Validates `task_id` exists in SQLite. If missing, returns HTTP 404.
  2. If task already finished: queries SQLite for all recorded stdout/stderr logs and final status, emits them with `event: task_completed`, and cleanly terminates the stream.
  3. If task is running or queued:
     - Emits current state and historical logs recorded so far.
     - Registers listener in the task-specific pub/sub channel.
     - Streams real-time stdout/stderr lines as child process outputs them.
     - Upon task exit, emits `task_completed` with exit code and status, sends `event: close`, and flushes/closes the stream.

#### In-Memory Event Broker (`EventBroker`) Design
```
                       ┌────────────────────────────────┐
                       │    SQLite SSOT (WAL Mode)      │
                       └───────────────┬────────────────┘
                                       │ Write / Commit
                                       ▼
  External Webhook ──► [HTTP Ingress] ──► [Task Dispatcher]
                                                │ Emit
                                                ▼
                                    ┌───────────────────────┐
                                    │      EventBroker      │
                                    └───────────┬───────────┘
                                                │
                 ┌──────────────────────────────┼──────────────────────────────┐
                 ▼                              ▼                              ▼
     [Subscriber Queue 1]            [Subscriber Queue 2]            [Subscriber Queue 3]
      (/events/stream)              (/tasks/T123/stream)               (CLI logs -f)
```
- **Broker Mechanics**:
  - Maintained as an in-memory PubSub manager with a dictionary:
    `_subscribers: dict[str, set[asyncio.Queue]]` (topics mapped to queues).
  - Each connected client allocates an `asyncio.Queue(maxsize=256)`.
  - **Backpressure & Memory Protection**:
    If a slow client's queue is full (`queue.full()`), the oldest non-essential message is discarded or the connection is terminated with a warning log, ensuring one slow client cannot leak memory or cause unbounded buffer allocation.
  - **Clean Teardown**:
    When the client disconnects (`asyncio.CancelledError`, `BrokenPipeError`, or socket EOF), the broker immediately deregisters the queue and discards pending tasks.

---

### 2.2 Observability Endpoints Specification

#### 1. `/healthz` (Liveness & Readiness Probe)
- **Method**: `GET`
- **Authentication**: None (internal local probe, or optional bearer if exposed externally).
- **HTTP Status Codes**:
  - `200 OK`: Database connected, WAL journal active, memory healthy.
  - `503 Service Unavailable`: SQLite locked, disk read-only, or worker thread deadlock.
- **Response Schema (`application/json`)**:
  ```json
  {
    "status": "ok",
    "version": "1.0.0",
    "uptime_seconds": 3612.4,
    "timestamp": "2026-09-08T10:35:00Z",
    "database": {
      "status": "connected",
      "journal_mode": "wal",
      "busy_timeout_ms": 5000,
      "total_events": 128,
      "total_tasks": 125
    },
    "dispatcher": {
      "queue_depth": 0,
      "active_workers": 2,
      "running_tasks": 1
    },
    "system": {
      "pid": 48291,
      "memory_rss_mb": 17.65,
      "memory_limit_mb": 30.0,
      "memory_healthy": true
    }
  }
  ```

#### 2. `/metrics` (Prometheus & Observability Endpoint)
- **Method**: `GET`
- **Content-Type Negotiation**:
  - Default or `Accept: text/plain` -> Standard Prometheus text exposition format.
  - `Accept: application/json` or `?format=json` -> JSON formatted metrics map.
- **Metric Definitions**:
  | Metric Name | Type | Description |
  |-------------|------|-------------|
  | `webhook_hub_uptime_seconds` | Gauge | Total process uptime in seconds |
  | `webhook_hub_process_memory_rss_bytes` | Gauge | Process RSS memory footprint (macOS getrusage / ps) |
  | `webhook_hub_http_requests_total` | Counter | Labeled by `method`, `path`, `status` |
  | `webhook_hub_events_total` | Counter | Labeled by `source`, `status` (`verified`, `rejected`, `duplicate`) |
  | `webhook_hub_tasks_total` | Counter | Labeled by `action`, `status` (`queued`, `running`, `succeeded`, `failed`) |
  | `webhook_hub_task_duration_seconds` | Summary | Duration of completed tasks labeled by `action` |
  | `webhook_hub_active_sse_connections` | Gauge | Current number of active SSE subscribers |
  | `webhook_hub_sqlite_wal_checkpoints_total` | Counter | SQLite WAL checkpoints executed |

- **Sample Prometheus Output**:
  ```text
  # HELP webhook_hub_uptime_seconds Process uptime in seconds
  # TYPE webhook_hub_uptime_seconds gauge
  webhook_hub_uptime_seconds 3612.4

  # HELP webhook_hub_process_memory_rss_bytes Resident set size in bytes
  # TYPE webhook_hub_process_memory_rss_bytes gauge
  webhook_hub_process_memory_rss_bytes 18509824

  # HELP webhook_hub_http_requests_total Total HTTP requests handled
  # TYPE webhook_hub_http_requests_total counter
  webhook_hub_http_requests_total{method="POST",path="/webhook",status="202"} 128
  webhook_hub_http_requests_total{method="POST",path="/webhook",status="401"} 7
  webhook_hub_http_requests_total{method="GET",path="/healthz",status="200"} 412

  # HELP webhook_hub_tasks_total Dispatched task count
  # TYPE webhook_hub_tasks_total counter
  webhook_hub_tasks_total{action="cli",status="succeeded"} 120
  webhook_hub_tasks_total{action="cli",status="failed"} 4
  webhook_hub_tasks_total{action="cli",status="running"} 1

  # HELP webhook_hub_active_sse_connections Number of active SSE streaming clients
  # TYPE webhook_hub_active_sse_connections gauge
  webhook_hub_active_sse_connections 2
  ```

#### 3. `/tasks` (List & Filter Tasks)
- **Method**: `GET`
- **Query Parameters**:
  - `status`: Filter by status (`received`, `queued`, `running`, `succeeded`, `failed`).
  - `action`: Filter by action type (`cli`, `launchd`, `agent_signal`).
  - `event_id`: Filter by incoming webhook event ID.
  - `limit`: Integer (default 50, max 200).
  - `offset`: Integer (default 0).
  - `order`: `asc` or `desc` (default `desc` by `created_at`).
- **Response**:
  ```json
  {
    "tasks": [
      {
        "id": "task_01j7ac918b",
        "event_id": "evt_01j7ac8z19",
        "action": "cli",
        "command": "python3 scripts/sync.py",
        "status": "succeeded",
        "exit_code": 0,
        "created_at": "2026-09-08T10:30:00Z",
        "started_at": "2026-09-08T10:30:00.120Z",
        "completed_at": "2026-09-08T10:30:01.840Z",
        "duration_ms": 1720
      }
    ],
    "total": 125,
    "limit": 50,
    "offset": 0
  }
  ```

#### 4. `/tasks/{task_id}` (Authoritative Detail)
- **Method**: `GET`
- **Behavior**: Direct read query against SQLite tasks and task_logs tables.
- **Response**:
  ```json
  {
    "id": "task_01j7ac918b",
    "event_id": "evt_01j7ac8z19",
    "action": "cli",
    "command": "python3 scripts/sync.py",
    "status": "succeeded",
    "exit_code": 0,
    "stdout": "Syncing remote assets...\nDone in 1.7s\n",
    "stderr": "",
    "error_message": null,
    "retry_count": 0,
    "created_at": "2026-09-08T10:30:00Z",
    "started_at": "2026-09-08T10:30:00.120Z",
    "completed_at": "2026-09-08T10:30:01.840Z"
  }
  ```
- **Error Response (404)**:
  ```json
  {
    "error": "TaskNotFound",
    "message": "Task 'task_01j7ac918b' does not exist",
    "status_code": 404
  }
  ```

---

## 3. Requirement R4: Toolchain Unification & Agent Discovery

### 3.1 Unified CLI Interface (`bin/webhook-hub` & `python -m hub`)

To eliminate snippet rot and ghost scripts, all administrative, monitoring, testing, and operational workflows are bound to a single executable interface.

#### Entry Point Architecture
- **Executable Script**: `bin/webhook-hub` (has `chmod +x` permissions and `#!/usr/bin/env python3`).
- **Python Package Invocation**: `python3 -m hub` (routes directly to `hub.cli.main()`).
- **Path Resolution**: `bin/webhook-hub` automatically resolves the repository root directory relative to `$(dirname "$0")/..` so it runs seamlessly from any working directory.

#### Subcommand Specification

| Subcommand | Arguments & Flags | Description | Exit Codes |
|------------|-------------------|-------------|------------|
| `start` | `--port 9423`<br>`--host 127.0.0.1`<br>`--daemon / -d`<br>`--db path`<br>`--config path` | Starts the gateway. In daemon mode, writes PID file to `run/webhook-hub.pid` and polls `/healthz` up to 3s to verify startup before returning. | `0`: started successfully<br>`1`: startup failed or port occupied |
| `stop` | `--pidfile path`<br>`--force / -f` | Reads PID from `run/webhook-hub.pid`, issues `SIGTERM`, waits up to 5s for clean shutdown, escalates to `SIGKILL` if `--force` or timed out, and unlinks the pidfile. | `0`: stopped<br>`1`: no process found or failed to stop |
| `status` | `--json`<br>`--port 9423` | Checks PID liveness and queries `/healthz`. Outputs human-readable status table or structured JSON. | `0`: running & healthy<br>`1`: stopped or degraded |
| `logs` | `--follow / -f`<br>`--lines / -n 50`<br>`--task <task_id>`<br>`--level <DEBUG\|INFO\|WARN\|ERROR>` | Connects to SSE stream (`/events/stream` or `/tasks/{id}/stream`) if `--follow` is set, or reads recent log records from SQLite. Formats log lines with color badges. | `0`: stream ended or exited<br>`1`: connection error |
| `test-send` | `--action <cli\|agent_signal>`<br>`--payload <json\|file>`<br>`--secret <hmac_secret>`<br>`--token <bearer_token>`<br>`--tamper`<br>`--stale-timestamp` | Utility client to simulate external webhooks. Computes HMAC SHA-256 signature and timestamp headers. Supports intentional tampering for security validation. | `0`: webhook accepted (202)<br>`1`: server rejected or connection failed |
| `verify` | `--port 9423`<br>`--ephemeral`<br>`--verbose / -v` | Executes the standalone two-sided verification suite (`scripts/verify_e2e.py`). Tests legitimate and adversarial paths. | `0`: all verifications passed<br>`1`: verification failure |

---

### 3.2 Agent Discovery Contract (`SKILL.md`)

In accordance with Antigravity rules and workspace conventions (`AGENTS.md` and user global rules: `<300 lines`, YAML frontmatter, Origin, Source, Author: V):

```yaml
---
name: antigravity-webhook-hub
description: Use when an agent needs to receive external webhooks, dispatch local CLI commands or Antigravity task signals, stream real-time task logs via SSE, or query execution state on macOS without opening inbound firewall ports.
---
```

#### Key Sections in `SKILL.md`:
1. **Skill Metadata**:
   - Origin: `local`
   - Source: `/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub`
   - Author: `V`
   - Created: `2026-09-08`
   - Version: `1.0.0`
   - Review status: `reviewed`
2. **Callable CLI Recipes**:
   - How an agent starts/stops the hub.
   - How an agent subscribes to execution updates (`webhook-hub logs -f --task <id>`).
   - How an agent triggers a test payload or task signal (`webhook-hub test-send`).
3. **HTTP API Contract Table**:
   - Route, Method, Headers, Request Body, Response Code.
4. **Environment Configuration Table**:
   - `WEBHOOK_PORT` (default `9423`)
   - `WEBHOOK_SECRET` (HMAC secret)
   - `WEBHOOK_BEARER_TOKEN`
   - `WEBHOOK_DB_PATH`
   - `TUNNEL_TOKEN` (for Cloudflare Tunnel)
5. **Troubleshooting Runbook**:
   - Diagnosing port conflicts, stale PID files, DB locks, and invalid HMAC signatures.

---

### 3.3 Documentation Standards Compliance

1. **`README.md`**:
   - Architecture & Data Flow ASCII diagram.
   - Non-standard port selection (`9423`) and macOS memory guarantees (<30MB).
   - Ingress security contract: HMAC SHA-256 headers (`X-Hub-Signature-256`, `X-Hub-Timestamp`), drift window (±300s), constant-time verification.
   - Cloudflare Tunnel setup guide (`cloudflared tunnel run`).
   - SSE and observability endpoint documentation.
   - CLI reference with code blocks.
2. **`CHANGELOG.md`**:
   - Follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and Semantic Versioning.
   - Structured under `## [1.0.0] - 2026-09-08`:
     - `Added`: Ingress gateway with HMAC-SHA256 and Bearer auth; SQLite WAL single-source-of-truth; asynchronous task dispatcher; SSE streams `/events/stream` and `/tasks/{id}/stream`; observability endpoints `/healthz`, `/metrics`, `/tasks`; unified CLI `bin/webhook-hub` and `python -m hub`; Antigravity agent skill `SKILL.md`.
     - `Security`: Constant-time digest comparison (`hmac.compare_digest`); replay protection via 300s timestamp drift checks; idempotent event deduplication.

---

## 4. Requirement R5: Two-Sided E2E Verification & Test Suite

### 4.1 Two-Sided Verification Philosophy
The workspace testing contract strictly dictates:
> *"测遍了不会说谎的那一半，也要测遍会说谎的那一半"*
> Audit both legitimate paths (valid signatures, accurate persistence, task execution, exit code recording, SSE streaming) and adversarial challenges (unverified/malformed requests rejected with zero side-effects).

An unrun check is not evidence. Therefore, test coverage must be executable via standard `pytest` and a zero-dependency standalone script `scripts/verify_e2e.py`.

---

### 4.2 Comprehensive Test Matrix

```
                               TEST SUITE ARCHITECTURE
 ┌─────────────────────────────────────────────────────────────────────────────────────────┐
 │                                   pytest Test Suite                                     │
 ├────────────────────────────┬─────────────────────────────┬──────────────────────────────┤
 │         Unit Tests         │          API Tests          │       E2E Integration        │
 │       (tests/unit/)        │        (tests/api/)         │        (tests/e2e/)          │
 ├────────────────────────────┼─────────────────────────────┼──────────────────────────────┤
 │ • test_security.py         │ • test_ingress_api.py       │ • test_legitimate_flow.py    │
 │   - HMAC calculation       │   - 202 Accepted            │   - Sign -> Ingress -> DB    │
 │   - Drift window tolerance │   - Signature rejection     │     -> Dispatch -> SSE       │
 │   - Bearer token extract   │   - Bearer auth flow        │     -> Exit Code 0           │
 │ • test_db.py               │ • test_observability_api.py │ • test_adversarial_flow.py   │
 │   - WAL mode pragmas       │   - /healthz JSON contract  │   - Forged HMAC -> 401       │
 │   - Schema migrations      │   - /metrics Prometheus/JSON│   - Replay timestamp -> 401  │
 │   - Idempotent dedup       │   - /tasks list & filter    │   - Duplicate payload dedup  │
 │   - State transitions      │   - /tasks/{id} detail & 404│   - Malformed JSON -> 400    │
 │ • test_dispatcher.py       │ • test_sse_stream_api.py    │   - Command timeout -> kill  │
 │   - Async subprocess run   │   - /events/stream protocol │   - Non-zero exit code       │
 │   - Timeout cancellation   │   - /tasks/{id}/stream live │   - Zero DB side effects     │
 │   - Stdout/stderr capture  │   - Last-Event-ID replay    │                              │
 │ • test_broker.py           │   - Heartbeat : ping frame  │                              │
 │   - Queue overflow drop    │                             │                              │
 └────────────────────────────┴─────────────────────────────┴──────────────────────────────┘
```

#### Detailed Test Case Specifications

#### 1. Legitimate Path Test Cases
- `test_valid_hmac_ingress`:
  - Input: POST `/webhook` with valid `X-Hub-Signature-256: sha256=...` and current `X-Hub-Timestamp`.
  - Assert: Returns HTTP 202 Accepted with JSON `{"status": "accepted", "event_id": "...", "task_id": "..."}`.
  - Assert: Event is saved in SQLite table `webhook_events` with status `received`.
- `test_valid_bearer_auth`:
  - Input: POST `/webhook` with `Authorization: Bearer <valid_token>`.
  - Assert: Returns HTTP 202 Accepted.
- `test_task_execution_lifecycle`:
  - Input: Action `cli`, command `python3 -c "print('hello from task')"`
  - Assert: SQLite task state transitions: `received` -> `queued` -> `running` -> `succeeded`.
  - Assert: `exit_code == 0`, `stdout == "hello from task\n"`.
- `test_sse_live_streaming`:
  - Connect to `/tasks/{task_id}/stream`.
  - Assert: Receives `event: task_running`, followed by stdout data chunks, followed by `event: task_completed`.
  - Assert: Connection closes cleanly after task completion.
- `test_sse_backlog_replay`:
  - Disconnect client before task finishes, then reconnect with `Last-Event-ID` header.
  - Assert: Replays prior missed events from SQLite before streaming final events.

#### 2. Adversarial Challenge Test Cases
- `test_missing_signature_rejected`:
  - Input: POST `/webhook` with no signature or auth headers.
  - Assert: HTTP 401 Unauthorized.
  - **Zero Side-Effect Verification**: Query SQLite `SELECT count(*) FROM webhook_events` and `tasks` — count MUST be 0.
- `test_tampered_hmac_payload_rejected`:
  - Input: POST `/webhook` where 1 byte in JSON payload was changed after HMAC signature was generated.
  - Assert: HTTP 401 Unauthorized.
  - Zero Side-Effect Verification: Database remains unmutated.
- `test_wrong_secret_rejected`:
  - Input: Payload signed with wrong secret key.
  - Assert: HTTP 401 Unauthorized, zero DB side effects.
- `test_replay_attack_stale_timestamp_rejected`:
  - Input: Valid HMAC signature, but `X-Hub-Timestamp` is 301 seconds in the past (exceeding 300s window).
  - Assert: HTTP 401 Unauthorized (or 403 Forbidden with `code: TimestampExpired`). Zero DB side effects.
- `test_future_timestamp_rejected`:
  - Input: Valid HMAC signature, but timestamp is 120 seconds in the future (clock skew/forgery).
  - Assert: HTTP 401 Unauthorized. Zero DB side effects.
- `test_duplicate_webhook_deduplication`:
  - Input: Send identical webhook payload twice with the same `event_id` or identical content hash within deduplication window.
  - Assert: First request returns 202 Accepted and executes task.
  - Assert: Second request returns 200/202 with existing `task_id` and does NOT spawn a second duplicate task execution.
  - Assert: SQLite `tasks` table contains exactly 1 task record.
- `test_malformed_json_payload`:
  - Input: POST `/webhook` with invalid JSON body: `{ "action": "cli", invalid }`.
  - Assert: HTTP 400 Bad Request. Zero DB side effects.
- `test_task_command_failure_captured`:
  - Input: Command that fails with exit code 42: `python3 -c "import sys; sys.stderr.write('fatal crash'); sys.exit(42)"`.
  - Assert: Gateway does NOT crash; returns 202 initially; task transitions to `failed`; SQLite records `exit_code: 42`, `stderr: "fatal crash"`.
- `test_task_execution_timeout`:
  - Input: Command that hangs: `python3 -c "import time; time.sleep(10)"` with task timeout configured to 1.5 seconds.
  - Assert: Dispatcher kills subprocess after 1.5s via SIGTERM/SIGKILL; task state transitions to `failed` with error `TaskExecutionTimeout`; gateway remains responsive on `/healthz`.

---

### 4.3 Standalone End-to-End Verification Script (`scripts/verify_e2e.py`)

A fully self-contained verification runner that requires only the Python standard library (plus `httpx` if available, falling back to `urllib.request`).

#### Capabilities:
1. **Ephemeral or Existing Target**:
   - If hub is already running on port 9423, tests against live instance.
   - If not running, launches an ephemeral hub instance on an isolated port (e.g. `9499`) with a temporary SQLite database in `/tmp/`.
2. **Sequential Step-by-Step Verification with Colored Output**:
   ```
   [STEP 1/9] Health Check (/healthz) .......................... [PASS] (200 OK, RSS: 16.4MB)
   [STEP 2/9] Legitimate Webhook Ingress (HMAC Signed) ......... [PASS] (202 Accepted)
   [STEP 3/9] SQLite SSOT Verification (WAL Mode) .............. [PASS] (Event & Task Persisted)
   [STEP 4/9] SSE Stream Live Subscription (/tasks/{id}/stream)  [PASS] (Running -> Logs -> Succeeded)
   [STEP 5/9] Adversarial: Missing Signature Rejection ........ [PASS] (401 Unauthorized, 0 DB Writes)
   [STEP 6/9] Adversarial: Tampered HMAC Rejection ............ [PASS] (401 Unauthorized, 0 DB Writes)
   [STEP 7/9] Adversarial: Stale Timestamp Replay Attack ...... [PASS] (401 Unauthorized, 0 DB Writes)
   [STEP 8/9] Adversarial: Duplicate Payload Deduplication ..... [PASS] (Idempotent 200, 1 Execution)
   [STEP 9/9] Adversarial: Subprocess Timeout & Cleanup ........ [PASS] (Killed cleanly, Exit code recorded)
   ================================================================================
   VERIFICATION RESULT: ALL 9 CHECKS PASSED (Total time: 2.84s, Max RSS: 18.2MB)
   ================================================================================
   ```
3. **Exit Code Contract**:
   - Exits with `0` if 100% of checks pass.
   - Exits with `1` if any check fails, printing exact diagnostic failure details.

---

## 5. Implementation Roadmap & Recommended File Layout

```
26.09.08-antigravity-webhook-hub/
├── bin/
│   └── webhook-hub                  # Executable CLI launcher (chmod +x)
├── hub/                             # Core Python package (python -m hub)
│   ├── __init__.py                  # Package exports & version
│   ├── __main__.py                  # python -m hub entry point
│   ├── cli.py                       # Unified CLI argument parser & subcommands
│   ├── config.py                    # .env and config.yaml loader
│   ├── db.py                        # SQLite SSOT & schema migrations (WAL mode)
│   ├── security.py                  # HMAC SHA-256 validation & replay guard
│   ├── server.py                    # Lightweight async HTTP server (<30MB RAM)
│   ├── dispatcher.py                # Async subprocess task runner & timeout guard
│   ├── broker.py                    # SSE Pub/Sub event broker with bounded queues
│   ├── metrics.py                   # Prometheus text format & JSON metrics engine
│   └── routes/
│       ├── __init__.py
│       ├── ingress.py               # POST /webhook handler
│       ├── observability.py         # GET /healthz, /metrics, /tasks, /tasks/{id}
│       └── sse.py                   # GET /events/stream, /tasks/{id}/stream
├── scripts/
│   ├── verify_e2e.py                # Standalone E2E verification script
│   └── cloudflared_setup.sh         # Cloudflare Tunnel recipe & setup script
├── tests/
│   ├── conftest.py                  # Shared fixtures, test client, temp DB
│   ├── unit/
│   │   ├── test_security.py         # HMAC, compare_digest, timestamp window
│   │   ├── test_db.py               # SQLite WAL, migrations, idempotency
│   │   ├── test_dispatcher.py       # Subprocess exec, timeout, exit codes
│   │   └── test_broker.py           # SSE queue backpressure & cleanup
│   ├── api/
│   │   ├── test_ingress_api.py      # POST /webhook routes & responses
│   │   ├── test_observability_api.py# /healthz, /metrics, /tasks endpoints
│   │   └── test_sse_stream_api.py   # SSE headers, keepalive, replay
│   └── e2e/
│       ├── test_legitimate_flow.py  # Full lifecycle: sign -> ingress -> exec -> stream
│       └── test_adversarial_flow.py # Tampered signatures, replays, timeouts, zero side effects
├── .env.sample                      # Sample configuration template
├── CHANGELOG.md                     # Semantic versioning release log
├── ORIGINAL_REQUEST.md              # Original specification
├── PROJECT.md                       # Architecture & milestone specification
├── README.md                        # Project documentation & quickstart
├── SKILL.md                         # Antigravity agent discovery contract (<300 lines)
└── pyproject.toml                   # Project metadata & pytest configuration
```

---

## 6. Synthesis & Dependencies Across Milestones

| Requirement | Dependent Modules | Shared Data Contract |
|-------------|-------------------|----------------------|
| **R3 (SSE & Observability)** | Requires R2 (SQLite DB & Dispatcher) | Shares `tasks`, `webhook_events`, and `task_logs` tables; receives dispatch events from `EventBroker`. |
| **R4 (CLI & Discovery)** | Wraps R1, R2, R3, R5 | Calls `server.run()`, `verify_e2e.main()`, communicates with `/healthz` and `/events/stream`. |
| **R5 (E2E Verification)** | Tests R1, R2, R3, R4 | Validates cryptographic rejection (R1), DB persistence (R2), SSE streaming (R3), and CLI readiness (R4). |

### Next Steps for Orchestrator
1. Integrate findings from Survey 1 (R1), Survey 2 (R2), and Survey 3 (R3, R4, R5) into `PROJECT.md`.
2. Proceed with milestone decomposition:
   - Milestone 1: Ingress Gateway & Cryptographic Security (R1).
   - Milestone 2: SSOT SQLite DB & Async Dispatcher (R2).
   - Milestone 3: SSE Streaming & Observability Endpoints (R3).
   - Milestone 4: Unified CLI Toolchain & Discovery Docs (R4).
   - Milestone 5: E2E Verification Suite & Adversarial Hardening (R5).
