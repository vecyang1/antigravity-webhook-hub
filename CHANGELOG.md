# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-09-10

### Added
- Auto-Picker & Unprocessed Task Sweeper subsystem (`hub.db`, `hub.dispatcher`, `hub.routes.tasks`):
  - Solves the Mac sleep and battery-loss problem: automatically discovers and recovers unhandled, interrupted, or orphaned tasks and webhook events after prolonged laptop sleep (e.g. 3h+), power loss, or unexpected process restarts.
  - Periodic background sweeper loop (`sweeper_interval_seconds: 60`, `stale_task_timeout_seconds: 300`) with monotonic clock jump detection (`time.monotonic()` leap > 60s) triggering instant wake-up sweeps upon resuming from sleep.
  - Orphaned webhook event rehydration (`DatabaseManager.get_orphaned_webhook_events`, `DatabaseManager.rehydrate_orphaned_event`): promotes orphaned `webhook_events` (status `received` with no task) into `queued` tasks.
  - Interrupted running task recovery (`DatabaseManager.sweep_and_requeue_unprocessed`): resets stale `running` tasks to `queued` (or `timed_out` if `max_retries` exhausted) and re-enqueues into in-memory dispatcher queue.
  - HTTP endpoints for agent orchestration:
    - `GET /tasks/unprocessed`: Queries unprocessed/stale tasks and orphaned events with breakdown counts.
    - `POST /tasks/sweep`: Atomically sweeps and enqueues unhandled tasks, supporting `dry_run` mode and retry customization.
  - Unified CLI commands:
    - `./bin/webhook-hub sweep` (with `--dry-run`, `--json`, `--stale-seconds`, `--max-retries`, `--no-auto-retry`).
    - `./bin/webhook-hub pick-unprocessed` (alias).
  - 10-Step Standalone E2E Verification Runner (`scripts/verify_e2e.py`):
    - Added Step 10: Auto-Picker & Unprocessed Task Sweeper integration check, injecting orphaned events, triggering sweep, and asserting live execution to `succeeded` and event transition to `processed`.
    - 10/10 standalone checks passing in <7s with gateway RSS 29.72MB <= 30MB.
    - 184/184 full test suite passing cleanly.

## [1.2.0] - 2026-09-09

### Added
- Antigravity Contact Review Agent subsystem (`hub.contact_review`):
  - Intelligent 5-verdict decision engine (`NO_CHANGE`, `SUPPLEMENT`, `CORRECT`, `MERGE`, `CREATE`) replacing crude blind dedupe/overwrite logic.
  - Live Notion CRM integration for `People ppl[UB3_250711]` (`22ce1b43-2393-81a4-9443-e32e71142e0d`) supporting compound OR candidate queries across name, phone, email, and social URLs.
  - Single Source of Truth (SSOT) read-after-write verification ensuring live database properties match expected mutations before reporting.
  - Threaded Slack notification dispatcher with verdict banners, Notion deep links, and attribute diff highlights.
  - Dedicated CLI command: `./bin/webhook-hub review-contact` (alias: `contact-review`) supporting `--dry-run`, `--json`, `--name`, `--phone`, and payload inputs.
  - Synchronous execution query mode (`POST /webhook/contact-review?sync=true`) returning immediate 200 responses with execution logs and verified verdict state.
  - In-process `contact_review` task dispatching with real-time SSE event streaming and SQLite SSOT log chunks.
- Live n8n workflow modernization:
  - Deployed `Wake Antigravity Contact Review Agent` into workflow `3J5doqEyxA7lT1OO` ("Slack_people ppl people 25.07.20 ✅") on `https://n.worldinspirelab.com`.
  - Replaced legacy blind update/create branches with SSOT Antigravity Review pipeline and threaded Slack feedback.
  - Automated deployment and rollback script (`scripts/patch_n8n_people_workflow.py`) with pre-patch backups.

## [1.1.1] - 2026-09-09

### Added
- GitHub Actions CI/CD pipeline (`.github/workflows/ci.yml`) testing across `macos-latest` and `ubuntu-latest` on Python 3.11 and 3.12 (100% green, 139/139 tests passing).
- Uptime Kuma monitoring integration (`https://monitor.worldinspirelab.com/status/worldinspirelab`): 60-second healthz probe with compact JSON keyword `"status":"ok"`, attached to default alert notifications.

### Fixed
- Packaging: Explicit setuptools package declaration (`packages = ["hub", "hub.routes"]`) resolving flat-layout multi-directory discovery in CI.
- Cross-platform process termination: Accepted POSIX process group termination signals (`-9` and `-15`) across Darwin and Ubuntu (`dash`) shells on task timeout.

## [1.1.0] - 2026-09-09
 
 ### Added
 - Cloudflare Zero Trust Tunnel ingress integration routing `https://webhook.worldinspirelab.com` directly to local port 9423 via persistent macOS LaunchDaemon (`/Library/LaunchDaemons/com.cloudflare.cloudflared.plist`, tunnel ID `6ede1a86-1b22-456e-9b22-b383ed85c4d1`).
 - Authoritative proxied Cloudflare CNAME record for `webhook.worldinspirelab.com` (Zone ID `0bf97b3b9d0066125df8bf3ce76196bc`) with 100% 3-layer route verification (`cf-dns verify` API, 1.1.1.1 DoH, and live HTTPS TLS handshake).
 - Cloudflare Page Rule for `*webhook.worldinspirelab.com/*` disabling Browser Integrity Check and setting Security Level to essentially off, resolving Cloudflare Edge Error 1010 for automated webhook callers (Python urllib, Go net/http, standard curl, and external SaaS platforms).
 - Enhanced `tunnel/start_tunnel.sh` with `--status` monitoring, graceful LaunchDaemon collision avoidance, and `--foreground` override flag.
 - Isolated test `--pidfile` in `tests/stress/test_m5_adversarial_dispatcher_sse.py` ensuring zero collision between running background daemons and test suite executions.
 - Root service descriptor route (`GET /`) returning service status, uptime, and available API routes.
 - Concrete production tunnel configuration file at `tunnel/config.yml`.
 - Comprehensive live public HTTPS verification covering legitimate HMAC/Bearer webhooks, 401 signature tampering rejection, and SQLite SSOT state persistence.
 
 ## [1.0.0] - 2026-09-08

### Added
- Pure Python standard-library asynchronous HTTP/1.1 server (`hub.server.AsyncHTTPServer`) with zero heavy framework dependencies, running with `<30MB` resident RAM and `0%` idle CPU on macOS.
- Unified Command Line Interface executable via `bin/webhook-hub` and `python3 -m hub` supporting 6 primary subcommands:
  - `start`: Foreground or background daemon runner with automatic PID tracking (`.webhook-hub.pid`).
  - `stop`: Graceful shutdown with escalation to `SIGKILL` on unresponsive processes.
  - `status`: Process liveness probe and health diagnostics with structured `--json` output.
  - `logs`: Dual-mode log viewer supporting offline SQLite historical querying and live SSE tailing (`--follow`).
  - `test-send`: Cryptographic webhook testing utility with automatic HMAC-SHA256 calculation, Bearer token injection, and intentional `--tamper` simulation.
  - `verify`: Standalone end-to-end test execution delegating to `scripts/verify_e2e.py`.
- Relational SQLite Single Source of Truth (`hub.db.DatabaseManager`) configured in Write-Ahead Logging (`WAL`) mode with schema tables for `webhook_events`, `tasks`, `executions`, and `execution_logs`.
- Decoupled asynchronous task dispatcher (`hub.dispatcher.TaskDispatcher`) executing ingress tasks in background worker queues without blocking HTTP response times.
- Multi-target runner execution support:
  - Isolated subprocess process groups (`os.setsid`, `os.killpg`) with stdout/stderr stream capture and timeout handling.
  - macOS `launchd` kickstart integration.
  - Periodic `cron` runner support.
  - Atomic Antigravity AI agent signal file emission (`.agents/signals/`).
- Real-time Server-Sent Events (`hub.routes.sse` and `hub.broker.EventBroker`) with broadcast topics, 15-second proxy keepalive heartbeats, and per-task log streaming (`/tasks/{id}/stream`).
- Observability and health monitoring endpoints (`/healthz`, `/ready`, `/metrics`, `/tasks`, `/tasks/{id}`) with Prometheus metrics exposition and RSS budget verification.
- Comprehensive agent discovery contract (`SKILL.md`) providing actionable recipes and troubleshooting runbooks under 300 lines.
- Complete 9-point standalone verification suite (`scripts/verify_e2e.py`) validating all architectural guarantees with zero third-party dependencies.

### Security
- Ingress cryptographic authentication gate rejecting unauthorized webhooks before any database writes occur.
- Timing-attack-resistant HMAC-SHA256 signature verification using `hmac.compare_digest`.
- Replay attack mitigation enforcing strict timestamp expiration windows (`timestamp_tolerance_seconds: 300`) and future skew rejection (`future_timestamp_tolerance_seconds: 60`).
- Bearer token authentication support (`Authorization: Bearer <token>`).
- Dual-layer deduplication engine using caller-provided idempotency keys and SHA-256 payload content hashes to prevent duplicate task execution.
- HTTP Request Smuggling protection rejecting conflicting `Content-Length` and `Transfer-Encoding` headers (RFC 7230 / RFC 9112).
- Hard payload size limits (`max_body_bytes: 1MB`) guarding against unbounded memory exhaustion denial-of-service attacks.
- Subprocess process group isolation (`os.setsid`) preventing child process leaks upon task cancellation or timeout.
