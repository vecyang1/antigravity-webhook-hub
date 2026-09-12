# Original User Request

## Initial Request — 2026-09-08T10:27:16Z

A lightweight, zero-footprint local webhook gateway and event dispatcher for Antigravity on macOS that securely receives HTTP requests from external websites and dispatches agent runs, scheduled tasks, and local automation with contract-first type safety and verifiable end-to-end execution.

Working directory: ./
Integrity mode: development

## Requirements

### R1. Minimal-Footprint Webhook Ingress Gateway
- Expose a lightweight HTTP service on macOS that listens on a configurable non-standard local port (e.g. `9423`) and stays under 30MB memory with negligible idle CPU.
- Implement cryptographic request validation (HMAC SHA-256 signature / Bearer token) to authenticate incoming external webhook payloads.
- Provide a clean, documented configuration contract (`.env` / `config.yaml`) with zero hardcoded credentials, ports, or URLs.
- Provide ingress integration recipes and scripts for Cloudflare Tunnel (`cloudflared`) to expose a secure HTTPS endpoint to the external website without opening inbound router ports.

### R2. Single-Source-of-Truth Task State & Dispatch Engine
- Persist all incoming webhook events and task states into an embedded database (SQLite in WAL mode) as the Single Source of Truth before triggering any action.
- Follow strict unidirectional data flow: external request -> API validates & writes to DB -> async dispatcher triggers task execution -> read back state from DB -> emit real-time updates / responses.
- Prevent redundant and duplicated state: status transitions (`received` -> `queued` -> `running` -> `succeeded` / `failed`) must be traceable and idempotent (deduplicating by event ID or hash).
- Dispatch verified webhook events to target actions asynchronously without blocking the HTTP response: support executing local CLI commands, triggering macOS launchd/cron jobs, or creating task signals for Antigravity agents.

### R3. Real-Time Observability & Push-Based Live Queries
- Provide live query / Server-Sent Events (SSE) or WebSocket streaming endpoints for real-time task log and state streaming, allowing local dashboards, CLI observers, or agents to subscribe to live execution updates.
- Provide standard health check and readiness endpoints (`/healthz`, `/metrics`, `/tasks`).

### R4. Toolchain Unification & Agent Discovery Contract
- Reduce ghost logic and snippet rot: package the gateway and dispatch controls into a discoverable CLI interface (`bin/webhook-hub` or `python -m hub`).
- Register discovery documentation (`README.md`, `CHANGELOG.md`, and an agent-callable skill definition or MCP config) in the repo so that Antigravity and future AI agents can discover, query, and trigger tasks through this unified entry point.

### R5. Two-Sided E2E Verification & Test Suite
- Implement a comprehensive TDD and E2E verification suite testing both sides of the contract:
  - Legitimate paths: valid signed payloads, correct SQLite persistence, async task triggering, real-time event streaming, and accurate exit code recording.
  - Adversarial paths: invalid or missing signatures (401/403), replay attack attempts, malformed JSON schemas, duplicate webhook retries, and task execution timeouts/failures with clean state recovery.

## Acceptance Criteria

### Security & Ingress
- [ ] Requests without valid signature or Bearer token are rejected with 401/403 and produce zero side effects in the database.
- [ ] Replay attacks with stale timestamps or duplicate event IDs are rejected or deduplicated idempotently.
- [ ] All configuration parameters (port, secrets, token, log level) are fully configurable via `.env` / config file.

### Performance & System Impact
- [ ] Gateway idle RAM usage remains under 30MB and CPU usage is near 0% when idle on macOS.

### SSOT & Data Flow
- [ ] Every incoming event is saved to SQLite before downstream processing starts.
- [ ] No phantom in-memory state: querying `/tasks/{task_id}` or the database returns the authoritative status.
- [ ] Streaming endpoint pushes live task state updates to connected clients/agents in real-time.

### Tooling & Maintainability
- [ ] Unified CLI tool provided with subcommands (e.g. `start`, `stop`, `status`, `logs`, `test-send`, `verify`).
- [ ] `CHANGELOG.md` created with initial release notes adhering to the A-coding workspace contract.
- [ ] Documentation and agent discovery contract (`SKILL.md` / `README.md`) included for future agent reuse.

### Automated Testing
- [ ] 100% of automated unit and E2E integration test cases pass when running the test suite command (`pytest`).
- [ ] Verification script provided to run an end-to-end simulated webhook lifecycle test from external POST to finished task.
