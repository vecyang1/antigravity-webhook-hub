# E2E Test Infra: Antigravity Webhook Hub

## Test Philosophy
- Opaque-box, requirement-driven, derived strictly from `ORIGINAL_REQUEST.md`.
- Adheres to workspace standard: *"测遍了不会说谎的那一半，也要测遍会说谎的那一半"*.
- Zero side-effect assertion for adversarial challenges.
- Two independent verification layers:
  1. Automated `pytest` suite (`tests/unit/`, `tests/api/`, `tests/e2e/`).
  2. Standalone zero-dependency Python script (`scripts/verify_e2e.py`).

## Feature Inventory & Test Coverage Matrix
| # | Feature | Requirement | Tier 1 (Feature) | Tier 2 (Boundary) | Tier 3 (Cross) | Tier 4 (Scenario) |
|---|---------|-------------|:----------------:|:-----------------:|:--------------:|:-----------------:|
| 1 | Minimal-Footprint HTTP Ingress | R1 | 5 | 5 | ✓ | ✓ |
| 2 | HMAC SHA-256 Validation | R1 | 5 | 5 | ✓ | ✓ |
| 3 | Bearer Token Auth | R1 | 5 | 5 | ✓ | ✓ |
| 4 | Config Contract (.env/yaml) | R1 | 5 | 5 | ✓ | ✓ |
| 5 | Cloudflare Tunnel Integration | R1 | 5 | 5 | ✓ | ✓ |
| 6 | SQLite WAL Database Layer | R2 | 5 | 5 | ✓ | ✓ |
| 7 | Unidirectional Flow & SSOT | R2 | 5 | 5 | ✓ | ✓ |
| 8 | Idempotent Deduplication | R2 | 5 | 5 | ✓ | ✓ |
| 9 | Atomic CAS State Machine | R2 | 5 | 5 | ✓ | ✓ |
| 10 | Async Task Dispatcher | R2 | 5 | 5 | ✓ | ✓ |
| 11 | Crash Recovery | R2 | 5 | 5 | ✓ | ✓ |
| 12 | Real-Time SSE Streaming | R3 | 5 | 5 | ✓ | ✓ |
| 13 | Observability Endpoints | R3 | 5 | 5 | ✓ | ✓ |
| 14 | Unified CLI Entry Point | R4 | 5 | 5 | ✓ | ✓ |
| 15 | Agent Discovery Contract | R4 | 5 | 5 | ✓ | ✓ |

## Test Architecture
- **Test Runner**: `python3 -m pytest tests/ -v` and `python3 scripts/verify_e2e.py`.
- **Pass/Fail Semantics**: 100% pass, zero errors, clean process exit codes.
- **Fixtures (`tests/conftest.py`)**:
  - Temporary isolated SQLite database with WAL pragmas.
  - Test server instance on ephemeral non-standard port.
  - Cryptographic helpers for HMAC generation, tampering, and timestamp manipulation.

## Test Tiers Definition

### Tier 1: Feature Coverage (Happy Path Isolation)
- Valid signed POST `/webhook` returns 202 Accepted.
- Bearer token authenticated POST returns 202 Accepted.
- GET `/healthz` returns 200 OK with `status: ok` and DB liveness.
- GET `/metrics` returns Prometheus text and JSON metrics.
- GET `/tasks` lists tasks with status filter.
- CLI `webhook-hub status` returns liveness.

### Tier 2: Boundary & Corner Cases (Adversarial Path Isolation)
- Missing signature header -> 401 Unauthorized with **0 DB records created**.
- Invalid/tampered signature -> 401 Unauthorized with **0 DB records created**.
- Expired timestamp (>300s old) -> 401 Unauthorized (replay defense) with **0 DB records created**.
- Future timestamp (>60s in future) -> 401 Unauthorized with **0 DB records created**.
- Malformed JSON payload -> 400 Bad Request with **0 DB records created**.
- Duplicate request within idempotency window -> 200/202 with `duplicate: true`, single task execution.
- Subprocess command exceeding timeout -> killed cleanly via `os.killpg`, task status updated to `timed_out` or `failed`.
- Unloaded launchd service -> returns failure cleanly without crashing hub.

### Tier 3: Cross-Feature Combinations
- Webhook Ingress -> DB Write -> Task Dispatch -> SSE Stream -> Final Status Query.
- Concurrent incoming webhooks under load with SQLite WAL concurrent read/write.
- Task execution while SSE subscriber connects and receives live logs.
- Server restart with running tasks -> crash recovery marks orphaned tasks as failed.

### Tier 4: Real-World Application Scenarios
- Scenario 1: GitHub / Stripe style webhook payload ingestion -> triggers local build script -> streams logs to CLI observer.
- Scenario 2: macOS Antigravity agent signal dispatch -> drops task file in `.agents/signals/` -> agent detects and processes.
- Scenario 3: Cloudflare Tunnel simulated ingress -> verifies `X-Forwarded-For` handling and `X-Accel-Buffering: no` SSE streaming.

## Minimum Acceptance Criteria
- All unit, API, and E2E tests pass 100% under `pytest`.
- Memory RSS benchmark verifies idle memory remains strictly under 30MB.
- Standalone verification script (`scripts/verify_e2e.py`) executes end-to-end lifecycle and exits with code 0.
