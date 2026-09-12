# Project: Antigravity Webhook Hub

## Architecture
A lightweight, zero-footprint local webhook gateway and event dispatcher for Antigravity on macOS.
- **Language & Runtime**: Python 3.14 on macOS (Darwin arm64).
- **Core Principle**: Zero heavy third-party web frameworks (FastAPI/Uvicorn consume >30MB). Pure standard library `asyncio` HTTP/1.1 server guarantees idle memory < 24MB RSS (well under the 30MB budget) and near 0% idle CPU.
- **Single Source of Truth (SSOT)**: SQLite in WAL mode with tuned PRAGMAs (`journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`, `foreign_keys=ON`, `cache_size=-4000`).
- **Unidirectional Data Flow**: External POST -> Cryptographic & Schema Validation -> SQLite Insert (`webhook_events`, `tasks`) -> HTTP 202 Accepted Response -> Async Queue Dispatch -> Subprocess / Action Execution -> Stream Output & Write Terminal Status to SQLite -> PubSub Push to SSE Subscribers.
- **Real-Time Streaming**: Server-Sent Events (SSE) over standard HTTP with 15-second heartbeat pings (`: ping\n\n`) and `X-Accel-Buffering: no` for Cloudflare Tunnel compatibility.
- **Unified Toolchain**: Executable `bin/webhook-hub` and `python -m hub` CLI with subcommands: `start`, `stop`, `status`, `logs`, `test-send`, `verify`.
- **Discovery Contract**: `SKILL.md` (<300 lines), `README.md`, and `CHANGELOG.md` for AI agent discovery and Antigravity automation.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Minimal-Footprint Async HTTP Gateway | Pure asyncio HTTP/1.1 server listening on port 9423, <30MB RSS, near 0% CPU | M1 | Survey 1 / R1 |
| 2 | Cryptographic HMAC SHA-256 Auth | Request validation with timestamp tolerance (300s window), `hmac.compare_digest` | M1 | Survey 1 / R1 |
| 3 | Bearer Token Auth | Alternative/fallback token authentication | M1 | Survey 1 / R1 |
| 4 | Config Contract (.env / config.yaml) | Zero hardcoded ports or credentials, priority config loader | M1 | Survey 1 / R1 |
| 5 | Cloudflare Tunnel Integration | `config.yml` ingress recipe, launch/quick-tunnel scripts (`cloudflared`) | M1 | Survey 1 / R1 |
| 6 | SQLite WAL Database Layer | 4-table relational schema (`webhook_events`, `tasks`, `executions`, `execution_logs`) | M2 | Survey 2 / R2 |
| 7 | Unidirectional Flow & SSOT Persistence | Event/task saved to SQLite before downstream processing; no phantom state | M2 | Survey 2 / R2 |
| 8 | Idempotent Deduplication | Unique index on idempotency key & SHA-256 payload hash; idempotent cached 200/202 | M2 | Survey 2 / R2 |
| 9 | Atomic CAS State Machine | State transitions (`received` -> `queued` -> `running` -> `succeeded`/`failed`/`timed_out`) via SQL CAS | M2 | Survey 2 / R2 |
| 10 | Async Multi-Target Dispatcher | Async worker executing CLI commands (process group isolation `os.setsid`), launchd kickstart, cron, agent signals | M2 | Survey 2 / R2 |
| 11 | Boot-Time Crash Recovery | Startup recovery marking interrupted tasks as failed with clean reason | M2 | Survey 2 / R2 |
| 12 | Server-Sent Events (SSE) Streaming | `/events/stream` and `/tasks/{task_id}/stream` with PubSub broker and backlog replay | M3 | Survey 3 / R3 |
| 13 | Observability Endpoints | `/healthz` (200/503), `/metrics` (Prometheus text & JSON), `/tasks`, `/tasks/{task_id}` | M3 | Survey 3 / R3 |
| 14 | Unified CLI Entry Point | `bin/webhook-hub` and `python -m hub` supporting start, stop, status, logs, test-send, verify | M4 | Survey 3 / R4 |
| 15 | Agent Discovery Contract | `SKILL.md` (<300 lines), `README.md`, `CHANGELOG.md` adhering to Keep a Changelog | M4 | Survey 3 / R4 |
| 16 | Two-Sided E2E Test Suite | Comprehensive pytest suite covering legitimate and adversarial paths | M5 / Test Track | Survey 3 / R5 |
| 17 | Standalone Verification Script | Zero-dependency end-to-end verification script (`scripts/verify_e2e.py`) | M5 / Test Track | Survey 3 / R5 |
| 18 | Contact Review & CRM Intake | Intelligent 5-verdict decision engine, compound Notion query, SSOT live verification, threaded Slack notifications | M6 | User Request |
| 19 | Auto-Picker & Unprocessed Sweeper | Detects Mac sleep / battery loss via monotonic clock jump, rehydrates orphaned events, resets stale running tasks | M7 | Autonomy / Resiliency |
| 20 | Observable SPA Web Dashboard | Real-time SSE dashboard (`/dashboard`, `/ui`), UI/UX Pro Max OLED theme, live agent activities, sentinels, pulse queue, test-event filter | M8 | Observability |
| 21 | Public GPL-3.0-or-later Release | Open-source release on GitHub with privacy sanitization gates, RFC 2606 mock domains, and 0 privacy leaks | M9 | Open Source |
| 22 | macOS Zero-Friction Engine | One-command setup (`webhook-hub setup`) and native macOS `launchd` LaunchAgent daemon manager (`webhook-hub service`) | M10 | Developer Experience |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| T1 | E2E Testing Suite (Tiers 1-4) | Comprehensive test suite (unit, API, E2E) + verification script | None | DONE |
| M1 | Ingress Gateway & Security Core | Async HTTP server, HMAC SHA-256, Bearer auth, config loader, Cloudflare recipes | None | DONE |
| M2 | SQLite SSOT & Async Dispatch Engine | SQLite WAL schema, unidirectional persistence, idempotent CAS state machine, async runner | M1 | DONE |
| M3 | Real-Time Observability & SSE | PubSub broker, SSE streams (`/events/stream`, `/tasks/{task_id}/stream`), `/healthz`, `/metrics`, `/tasks` | M1, M2 | DONE |
| M4 | Unified CLI & Agent Discovery | `bin/webhook-hub`, `python -m hub`, `SKILL.md`, `README.md`, `CHANGELOG.md` | M1, M2, M3 | DONE |
| M5 | Final E2E Integration & Verification | Execute 100% of E2E test suite + standalone verifier, followed by adversarial hardening | T1, M1, M2, M3, M4 | DONE |
| M6 | Contact Review & CRM SSOT Pipeline | 5-verdict engine (`NO_CHANGE`, `SUPPLEMENT`, `CORRECT`, `MERGE`, `CREATE`), Notion People CRM SSOT verification, n8n workflow integration (`3J5doqEyxA7lT1OO`) | M1-M5 | DONE |
| M7 | Auto-Picker & Sweeper Recovery | Clock jump detection, orphaned event rehydration, stale task resets, sweep CLI subcommands | M1-M6 | DONE |
| M8 | Observable SPA Dashboard & UI/UX Pro Max | `/dashboard`, `/ui`, agent activity feeds, sentinel AI runs, pulse queue, test-event toggle | M1-M7 | DONE |
| M9 | Public Open-Source Release (GPL-3.0) | License transition, privacy sanitization gates, audit receipts, public GitHub release | M1-M8 | DONE |
| M10 | macOS Zero-Friction Out-of-the-Box Engine | Environment diagnostics, directory scaffolding, cryptographic `.env`, native LaunchAgent manager | M1-M9 | DONE |

## Code Layout
```
antigravity-webhook-hub/
├── bin/
│   └── webhook-hub              # Executable CLI wrapper
├── hub/
│   ├── __init__.py              # Package init
│   ├── __main__.py              # python -m hub entry point
│   ├── config.py                # Configuration loader (.env, config.yaml, defaults)
│   ├── server.py                # Pure asyncio HTTP/1.1 server & router (<30MB RAM)
│   ├── security.py              # HMAC SHA-256, Bearer token, timing-safe validation
│   ├── db.py                    # SQLite connection, WAL mode, schema init, CAS updates
│   ├── models.py                # Zero-dependency dataclasses for events, tasks, states
│   ├── dispatcher.py            # Async task executor (CLI process groups, launchd, agent signals)
│   ├── broker.py                # In-memory bounded PubSub event bus for SSE
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── webhook.py           # Ingress POST /webhook, POST /webhook/{source}
│   │   ├── tasks.py             # GET /tasks, GET /tasks/{task_id}, POST /tasks
│   │   ├── sse.py               # GET /events/stream, GET /tasks/{task_id}/stream
│   │   └── observability.py     # GET /healthz, GET /ready, GET /metrics
│   └── cli.py                   # CLI implementation (start, stop, status, logs, test-send, verify)
├── tunnel/
│   ├── config.yml.template      # Cloudflare Tunnel configuration recipe
│   ├── start_tunnel.sh          # Tunnel launch helper
│   └── quick_tunnel.sh          # Ephemeral trycloudflare tunnel script
├── scripts/
│   └── verify_e2e.py            # Standalone zero-dependency E2E verification script
├── tests/
│   ├── conftest.py              # Fixtures: test db, test server, mock webhooks
│   ├── unit/
│   │   ├── test_config.py
│   │   ├── test_security.py
│   │   ├── test_db.py
│   │   └── test_dispatcher.py
│   ├── api/
│   │   ├── test_ingress_routes.py
│   │   ├── test_observability_routes.py
│   │   └── test_sse_routes.py
│   └── e2e/
│       ├── test_legitimate_flow.py
│       └── test_adversarial_flow.py
├── SKILL.md                     # Agent discovery contract (<300 lines)
├── README.md                    # Project overview, architecture, CLI reference
├── CHANGELOG.md                 # Keep a Changelog format
├── config.yaml.example          # Sample configuration file
├── .env.example                 # Sample environment variables
└── pyproject.toml / requirements.txt
```

## Interface Contracts

### Ingress ↔ Database
- Request payload is validated cryptographically before any DB write.
- DB write stores: `event_id`, `source`, `idempotency_key`, `payload_hash`, `headers_json`, `payload_json`, `created_at`.
- If duplicate `(source, idempotency_key)` or `payload_hash` within active window, return cached response with `{"duplicate": true, "task_id": ...}` and do not create new task.

### Ingress ↔ Dispatcher
- Validated event produces a `Task` entity with initial status `received`.
- Status transitions: `received` -> `queued` -> `running` -> `succeeded` / `failed` / `timed_out`.
- Dispatcher runs asynchronously via `asyncio.create_task` or queue worker, decoupling HTTP response from execution.
- HTTP response returns status 202 Accepted: `{"status": "accepted", "event_id": "...", "task_id": "..."}`.

### Dispatcher ↔ Broker / SSE
- Dispatcher emits progress events (`log`, `status_change`) to `EventBroker`.
- `EventBroker` routes events to subscriber queues matching `task_id` or wildcard `/events/stream`.
- SSE sends event chunks: `event: <name>\ndata: <json>\nid: <event_id>\n\n`.
- Every 15 seconds, send `: ping\n\n` heartbeat comment.
