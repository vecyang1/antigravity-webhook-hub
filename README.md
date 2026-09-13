# Antigravity Webhook Hub

A lightweight, zero-footprint local webhook gateway and decoupled event dispatcher engineered for Antigravity AI agents on macOS.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Memory Budget](https://img.shields.io/badge/RAM-%3C30MB-brightgreen.svg)]()
[![Idle CPU](https://img.shields.io/badge/idle_CPU-0%25-brightgreen.svg)]()

---

## Highlights

- **Ultra-Lean Resource Footprint**: Guarantees `< 30MB` resident set size (RSS) on macOS arm64/x86_64 and `0%` CPU utilization during idle periods.
- **Pure Standard Library**: High-throughput HTTP/1.1 asynchronous server constructed entirely on Python's built-in `asyncio` and `sqlite3` engines—zero heavy web framework dependencies.
- **Cryptographic Ingress Protection**: Enforces HMAC-SHA256 timestamped signatures (constant-time verification) and Bearer token authorization before committing any bytes to disk. Rejections produce zero database writes.
- **Dual-Layer Deduplication**: Automatic deduplication via caller-supplied idempotency keys (`X-Hub-Event-ID` / `idempotency_key`) and SHA-256 payload content hashes.
- **Crash-Resilient State Machine & Auto-Picker**: SQLite Single Source of Truth (SSOT) operating in Write-Ahead Logging (`WAL`) mode with atomic compare-and-swap (CAS) transitions. Includes an autonomous **Auto-Picker & Unprocessed Sweeper** that detects Mac sleep or battery loss (e.g. 3 hours offline) via monotonic clock jump detection, instantly rehydrates orphaned `webhook_events`, resets stale `running` executions, and auto-retries interrupted tasks upon wake.
- **Decoupled Multi-Target Dispatcher**: Asynchronously processes ingress webhooks across multiple runners:
  - Isolated subprocess process groups (`os.setsid`, `os.killpg`) with stdout/stderr capture and configurable timeout enforcement.
  - macOS `launchd` kickstart jobs.
  - Periodic `cron` schedules.
  - Antigravity AI agent atomic JSON signal files.
- **Real-Time Observability & Streaming**: Live Server-Sent Events (`SSE`) endpoints (`/events/stream`, `/tasks/{id}/stream`) with automated 15-second heartbeats and Prometheus metrics exposition (`/metrics`).
- **Autonomous Antigravity Watchdog & Auto Pull-Up**: 24/7 background watchdog engine that continuously monitors Antigravity AI agent sessions and subagents (including `/boost`, `teamwork-preview`, sidecars). When network streams break (`The stream was interrupted`, `Agent execution terminated due to error`, server restarts), the watchdog fail-closes if offline, waits for stable connection, and autonomously revives the session via native `agentapi send-message` with circuit-breaker protection (max 3 retries), completely eliminating manual "Retry" GUI clicks.
- **Unified Toolchain**: Full lifecycle control via executable `./bin/webhook-hub` CLI or `python3 -m hub` (including `sweep`, `pick-unprocessed`, and `antigravity doctor/pull-up`).

---

## Architecture Flow

```text
               +-------------------------------------------------------------+
               |                     Incoming Webhook                        |
               |       (GitHub / Stripe / Slack / Cloudflare Tunnel)         |
               +-------------------------------------------------------------+
                                              |
                                              v
                              +-------------------------------+
                              |   HTTP/1.1 Ingress Gateway    |
                              |   (AsyncHTTPServer :9423)     |
                              +-------------------------------+
                                              |
                       [ 1. Cryptographic Security Check ]
                         - HMAC-SHA256 constant-time
                         - Timestamp tolerance (±300s)
                         - Bearer token validation
                                              |
                       +----------------------+----------------------+
                       |                                             |
             (Invalid / Tampered)                            (Valid Request)
                       |                                             |
                       v                                             v
               +---------------+                     [ 2. Deduplication Check ]
               |  401 / 400    |                       - Idempotency key
               | 0 DB Writes   |                       - SHA-256 Payload Hash
               +---------------+                                     |
                                                     +---------------+---------------+
                                                     |                               |
                                                (Duplicate)                      (Unique)
                                                     |                               |
                                                     v                               v
                                             +---------------+               +---------------+
                                             | 202 Accepted  |               | 202 Accepted  |
                                             |  (Cached)     |               | (Task Enqueue)|
                                             +---------------+               +---------------+
                                                                                     |
                                                                                     v
                                                                     +-------------------------------+
                                                                     |       SQLite SSOT (WAL)       |
                                                                     |   webhook_events, tasks,      |
                                                                     |   executions, execution_logs  |
                                                                     +-------------------------------+
                                                                                     |
                                                                                     v
                                                                     +-------------------------------+
                                                                     |    Async Task Dispatcher      |
                                                                     |   (Decoupled Worker Queue)    |
                                                                     +-------------------------------+
                                                                                     |
                                                 +-------------------+---------------+-------------------+
                                                 |                   |               |                   |
                                                 v                   v               v                   v
                                           [ CLI Runner ]       [ launchd ]     [ cron ]       [ Agent Signal ]
                                            Process Group       launchctl                      Atomic JSON drop
                                            os.killpg timeout                                  .agents/signals/
                                                 |
                                                 v
                                 +-------------------------------+
                                 |       Event Broker (SSE)      |
                                 |  /events/stream, /tasks/{id}  |
                                 +-------------------------------+
```

---

## Quickstart

### 1. Prerequisites

- macOS 12+ (tested on Darwin arm64 Apple Silicon and x86_64) or modern Linux.
- Python 3.10 or newer (tested on Python 3.14).
- No external databases or message brokers required.

### 2. Installation

Clone repository and ensure execution permissions:

```bash
git clone https://github.com/your-org/antigravity-webhook-hub.git
cd antigravity-webhook-hub
chmod +x bin/webhook-hub
```

Optionally create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 3. Zero-Friction Setup (macOS & Linux)

Bootstrap your environment, directories, and cryptographic `.env` with a single command:

```bash
# Automatically diagnose architecture (arm64/x86_64), Python runtime,
# scaffold directories (data/, events/, backups/), and provision secure .env:
./bin/webhook-hub setup
```

To run as a permanent background service on macOS with auto-start on login and auto-restart on exit:

```bash
# Install and activate native macOS launchd LaunchAgent
./bin/webhook-hub service install

# Check LaunchAgent registration, PID, RSS memory (<30MB), and logs
./bin/webhook-hub service status
```

Manual configuration resolution follows strict precedence:
**CLI Arguments > Environment Variables (.env / shell) > config.yaml > Built-in Defaults**

Key settings in `.env`:

```ini
PORT=9423
HOST=127.0.0.1
AUTH_MODE=hmac                  # hmac | bearer | any | both
WEBHOOK_SECRET=your_secret_32_bytes_or_more
BEARER_TOKEN=your_secure_bearer_token
DATABASE_PATH=data/webhook_hub.db
DEFAULT_ACTION_TIMEOUT_SECONDS=300
```

---

## Command Line Interface (CLI)

The CLI is invoked via `./bin/webhook-hub` or `python3 -m hub`.

| Subcommand | Description | Common Flags |
|---|---|---|
| `setup` | Zero-friction setup wizard (scaffolding & `.env`) | `--force`, `-p` / `--port`, `--host` |
| `service` | Native macOS `launchd` LaunchAgent manager | `install`, `status`, `logs`, `restart`, `uninstall` |
| `start` | Start the gateway server | `-d` / `--daemon`, `-p` / `--port`, `--host`, `--db`, `--pidfile` |
| `stop` | Gracefully terminate running instance | `-p` / `--pidfile`, `-f` / `--force`, `-t` / `--timeout` |
| `status` | Query process status and `/healthz` metrics | `--json`, `-p` / `--port`, `--host`, `--pidfile` |
| `dashboard` | View or open Observable Web Dashboard (`/dashboard`) | `--open`, `--host`, `-p` / `--port`, `--json` |
| `rerun` | Re-enqueue failed/interrupted task into dispatcher | `task_id`, `--host`, `-p` / `--port`, `--db`, `--json` |
| `sweep` | Sweep & recover unprocessed/stale tasks | `--dry-run`, `--json`, `--source`, `--limit` |
| `logs` | Inspect SQLite logs or stream live SSE | `-f` / `--follow`, `-t` / `--task`, `-n` / `--lines`, `--db` |
| `test-send` | Cryptographically sign and send test webhook | `--action`, `--command`, `--secret`, `--token`, `--tamper` |
| `review-contact` | Review and merge contact into Notion CRM SSOT | `--name`, `--phone`, `--dry-run`, `--json`, `--payload` |
| `antigravity` | Watchdog diagnostics (`doctor`) and session resuscitation (`pull-up`) | `doctor`, `pull-up`, `--conversation`, `--dry-run`, `--json` |
| `verify` | Execute standalone E2E verification suite | `--host`, `--port`, `--secret` |

### Examples

```bash
# 1. Zero-friction setup & macOS LaunchAgent daemon
./bin/webhook-hub setup
./bin/webhook-hub service install
./bin/webhook-hub service status

# 2. Inspect health & memory budget
./bin/webhook-hub status
./bin/webhook-hub status --json

# 3. Open Observable Activity Web Dashboard
./bin/webhook-hub dashboard --open

# 4. Re-run an existing task
./bin/webhook-hub rerun tsk_01918a24bc10

# 5. Sweep unprocessed / stale tasks (Mac sleep recovery)
./bin/webhook-hub sweep --dry-run

# 6. Send legitimate test webhook signed with HMAC-SHA256
./bin/webhook-hub test-send --action cli --command "echo 'Webhook received'"

# 7. Tail historical execution logs from SQLite
./bin/webhook-hub logs --lines 50

# 8. Follow live SSE event stream
./bin/webhook-hub logs --follow

# 9. Intelligent Contact Review against Notion CRM SSOT
./bin/webhook-hub review-contact --name "Adam Walker" --phone "+1 415 555 0199" --dry-run
./bin/webhook-hub review-contact --payload '{"name": "Alice Smith", "email": "alice@example.com"}'

# 10. Gracefully terminate daemon
./bin/webhook-hub stop
```

---

## HTTP API Reference

All requests and responses use standard JSON encoding.

### Ingress: `POST /webhook` or `POST /webhook/{source}`

Accepts incoming webhook payloads. Returns `202 Accepted` immediately upon cryptographic authentication and enqueues execution.

#### Headers
- `Content-Type: application/json`
- `X-Hub-Signature-256: sha256=<hex_digest>` (required when `auth_mode` is `hmac`)
- `X-Hub-Timestamp: <unix_epoch>` (required when `auth_mode` is `hmac`)
- `Authorization: Bearer <token>` (required when `auth_mode` is `bearer`)
- `X-Hub-Event-ID: <idempotency_key>` (optional, custom deduplication identifier)

#### Request Body Schema
```json
{
  "action": "cli",
  "command": "python3 scripts/sync.py",
  "action_params": {"batch_size": 50},
  "priority": 1,
  "timeout_seconds": 120,
  "idempotency_key": "unique_idempotency_key_123"
}
```

#### Response Body (Status: `202 Accepted`)
```json
{
  "status": "accepted",
  "task_id": "tsk_01918a24bc10",
  "event_id": "evt_9f81a742c018"
}
```

---

### Observability & UI Endpoints

#### `GET /dashboard` or `GET /ui`
Observable Web Dashboard and Activity Console:
- **UI/UX Pro Max Terminal Log Drawer**: Syntax-highlighted log viewer with dedicated line number gutter (`.log-gutter`), semantic level badges (`INFO`, `WARN`, `ERROR`, `SUCCESS`), timestamp and file path highlighting, embedded/full JSON syntax highlighting and pretty formatting (`<> Pretty JSON`), full ANSI terminal color sequences, real-time live search with `<mark>` match highlighting and count badge, wrap/nowrap toggle, auto-scroll lock, copy-to-clipboard, execution duration header, and exit code pill badges. Pure inline SVGs (zero emojis).
- **Test Event Auto-Hide / Filter**: Toggle between `Real Only (Tests Hidden)`, `All Events`, and `Tests Only` with persistent localStorage memory and SSOT count alignment.
- **Multi-Tier Dashboard Authentication**: Protect sensitive dashboard views via Cloudflare Access Zero Trust JWT assertions (`cf-access-jwt-assertion`), HTTP Basic Auth (`Authorization: Basic ...`), and token fallback (`Authorization: Bearer ...`, `?token=...`, `X-Dashboard-Token`). Configurable via environment variables:
  ```ini
  DASHBOARD_AUTH_ENABLED=true
  DASHBOARD_BASIC_AUTH_USER=admin
  DASHBOARD_BASIC_AUTH_PASS=your_secure_password
  DASHBOARD_AUTH_TOKEN=your_secure_dashboard_token
  DASHBOARD_CF_ACCESS_AUD=your_cloudflare_access_aud_tag
  DASHBOARD_ALLOWED_EMAILS=user@example.com,admin@example.org
  ```

#### `GET /healthz` or `GET /health`
Returns system liveness, database status, and memory RSS validation (supports both GET and HEAD methods for uptime monitors).
```json
{
  "status": "ok",
  "version": "1.0.0",
  "uptime_seconds": 3600.5,
  "database": {
    "status": "connected"
  },
  "system": {
    "memory_rss_mb": 19.3,
    "memory_healthy": true,
    "memory_budget_mb": 30.0
  }
}
```

#### `GET /tasks/summary` (or `/activities/summary`)
Returns aggregate counts and status breakdowns directly from the SQLite Single Source of Truth (SSOT).
```json
{
  "status": "success",
  "total_tasks": 100,
  "total_events": 108,
  "by_status": {
    "queued": 0,
    "running": 0,
    "succeeded": 97,
    "failed": 2,
    "timed_out": 1
  },
  "by_source": {"default": 79, "contact-review": 14},
  "by_action": {"cli": 86, "contact_review": 14}
}
```

#### `POST /tasks/{task_id}/rerun`
Re-enqueues an existing task into SQLite SSOT and dispatcher queue for immediate execution retry.

#### `GET /ready`
Readiness probe verifying database connectivity and task dispatcher worker availability. Returns `200 OK` when ready, or `503 Service Unavailable`.

#### `GET /metrics`
Prometheus metrics format by default; returns structured JSON when requested via `Accept: application/json`.
Metrics include:
- `webhook_requests_total{source="...", status="..."}`
- `tasks_total{action_type="...", status="..."}`
- `process_memory_rss_bytes`
- `process_uptime_seconds`

#### `GET /tasks`
List task execution records with optional filtering:
- Query parameters: `status`, `source`, `limit` (default: 50), `offset` (default: 0).

#### `GET /tasks/{task_id}`
Retrieve granular task execution metadata, exit code, execution timestamps, and stdout/stderr tails.

#### `GET /tasks/unprocessed`
Inspect unhandled, orphaned, or stale tasks stranded by Mac sleep, battery exhaustion, or restarts:
- Query parameters: `stale_seconds` (default: 300), `source`, `limit` (default: 100).
- Returns: summary breakdown (`queued`, `received`, `stale_running`, `orphaned_events`, `recoverable_failed`, `total_unprocessed`), task records, and unhandled event IDs.

#### `POST /tasks/sweep`
Atomically recover and re-enqueue all unprocessed tasks and orphaned webhook events into the dispatcher queue:
- Request body: `{"stale_seconds": 300, "max_retries": 3, "auto_retry_interrupted": true, "dry_run": false, "limit": 100}`
- Returns: recovery counters and list of immediately enqueued `task_ids`.

---

### Server-Sent Events (SSE)

#### `GET /events/stream`
Subscribes to global gateway events (ingest, start, progress, complete, fail).
Includes automatic 15-second heartbeat ping comments (`: ping\n\n`) to preserve connection stability through reverse proxies.

#### `GET /tasks/{task_id}/stream`
Subscribes to live execution logs and progress events exclusively scoped to a single `task_id`. Terminates automatically when the task finishes execution.

---

## Cloudflare Tunnel Integration
 
 To expose Antigravity Webhook Hub securely to external webhook providers (GitHub, Stripe, Shopify, Slack, external sites) without opening firewall ports or managing public IP addresses, Cloudflare Tunnel (`cloudflared`) is fully configured and operational.
 
 ### Live Production Ingress
 
 - **Public Ingress Endpoint**: `https://webhook.worldinspirelab.com/webhook`
 - **Health Probe**: `https://webhook.worldinspirelab.com/healthz`
 - **Cloudflare Zone**: `worldinspirelab.com` (Zone ID `0bf97b3b9d0066125df8bf3ce76196bc`)
 - **Tunnel Name**: `26.03.08` (ID `6ede1a86-1b22-456e-9b22-b383ed85c4d1`)
 - **Tunnel Mode**: Remotely Managed via Cloudflare Zero Trust / Cloudflare API
 - **macOS Persistence**: System LaunchDaemon `/Library/LaunchDaemons/com.cloudflare.cloudflared.plist` (auto-starts on boot)
 - **DNS Routing**: CNAME `webhook.worldinspirelab.com` -> `6ede1a86-1b22-456e-9b22-b383ed85c4d1.cfargotunnel.com` (Proxied = True)
 - **Edge Policy & WAF**: Dedicated Cloudflare Page Rule for `*webhook.worldinspirelab.com/*` with `browser_check: off` and `security_level: essentially_off` ensuring zero 1010/403 blocking for machine webhook callers (Python urllib, Go net/http, standard curl, and SaaS webhooks).
 - **Multi-Layer Route Verification**: Passed 100% via `cf-dns verify` (API authoritative state, 1.1.1.1 DoH edge propagation, and live HTTPS/TLS probe).
 
 ### Local Tunnel Runner (`tunnel/`)
 
 For local inspection, custom standalone tunnels, or temporary development:
 - Production config: `tunnel/config.yml`
 - Named tunnel runner & status checker: `./tunnel/start_tunnel.sh` (`--status`, `--check`, `--init`, `--foreground`)
 - Ephemeral quick tunnel: `./tunnel/quick_tunnel.sh` (`*.trycloudflare.com`)

---

## Verification & Testing

### 1. Standalone End-to-End Verification Suite

Run the zero-dependency verification script covering the complete 12-point contract:

```bash
python3 scripts/verify_e2e.py
# Or via CLI alias:
./bin/webhook-hub verify
```

Verification suite checks:
1. Health check & memory RSS budget verification (`< 30MB`)
2. Legitimate webhook ingress & HMAC validation (`202 Accepted`)
3. SQLite SSOT verification in WAL mode
4. SSE stream protocol headers & heartbeat delivery
5. Adversarial: Missing signature rejection (`401 Unauthorized`, zero DB writes)
6. Adversarial: Tampered signature rejection (`401 Unauthorized`, zero DB writes)
7. Adversarial: Stale timestamp replay attack rejection (`401 Unauthorized`, zero DB writes)
8. Adversarial: Duplicate payload deduplication (`202 Accepted`, single execution)
9. Adversarial: Subprocess timeout & process group termination
10. Auto-Picker & Unprocessed Task Sweeper (recovers orphaned events & stale running tasks)
11. Dashboard, Observability UI & HEAD Support (`/dashboard`, `/ui`, `/health`, `/tasks/summary`, `HEAD`)
12. Antigravity Sidebar Sentinel Activity Emission (verifies sidecar JSON files with valid schema)

### 2. Comprehensive Test Suite

Run unit, API integration, adversarial, and stress tests:

```bash
python3 -m pytest tests/ -v
```

---

## License
 
Released under the [GNU General Public License v3.0 or later (GPL-3.0-or-later)](LICENSE).
