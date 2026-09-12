---
name: webhook-hub
description: Control, monitor, and query the local Antigravity Webhook Hub daemon and event dispatcher on macOS.
version: 1.8.1
author: V
date: 2026-09-12
source: local repository
---

# Webhook Hub Skill

Lightweight (<30MB RAM, 0% idle CPU) macOS webhook gateway and event dispatcher for Antigravity AI agents. Provides secure HMAC/Bearer ingress, decoupled execution queues, SQLite SSOT logging, and live SSE streaming.

## 1. Quick Invocations

All commands run via `./bin/webhook-hub <subcommand>` or `python3 -m hub <subcommand>`.

```bash
# macOS Zero-Friction Setup & LaunchAgent Daemon
./bin/webhook-hub setup                 # Self-diagnose environment, scaffold data/ dirs, provision secure .env
./bin/webhook-hub setup --force         # Re-scaffold and regenerate fresh cryptographic tokens
./bin/webhook-hub service install       # Install & activate native macOS launchd LaunchAgent auto-start daemon
./bin/webhook-hub service status        # Check LaunchAgent registration, PID, RSS memory (<30MB), logs
./bin/webhook-hub service logs -n 50    # View last 50 lines of stdout and stderr daemon logs
./bin/webhook-hub service restart       # Cleanly reload daemon via launchctl unload/load
./bin/webhook-hub service uninstall     # Unload LaunchAgent and remove plist

# Daemon management (manual foreground / background)
./bin/webhook-hub start -d             # Start gateway daemon on default port 9423
./bin/webhook-hub status               # Check process liveness & health metrics
./bin/webhook-hub status --json        # Machine-readable JSON status for agents
./bin/webhook-hub stop                 # Gracefully terminate daemon via PID file

# Dashboard & Observable Console
./bin/webhook-hub dashboard            # Print local & tunnel URLs + server liveness
./bin/webhook-hub dashboard --open     # Open embedded SPA dashboard in default browser
./bin/webhook-hub rerun <task_id>      # Re-enqueue failed/interrupted task into dispatcher

# Inspection & Streaming
./bin/webhook-hub logs --lines 20      # Read last 20 execution logs from SQLite
./bin/webhook-hub logs --task tsk_123  # Inspect single task execution output
./bin/webhook-hub logs -f              # Follow live global SSE event stream

# Auto-Picker & Unprocessed Task Sweeper (Mac Sleep / Battery Loss Recovery)
./bin/webhook-hub sweep                   # Recover and requeue all unprocessed/stale tasks
./bin/webhook-hub sweep --dry-run         # Inspect recoverable tasks without mutating state
./bin/webhook-hub sweep --dry-run --json  # Machine-readable JSON summary for agents
./bin/webhook-hub pick-unprocessed        # Inspect unprocessed tasks & orphaned events (alias)

# Testing & Verification
./bin/webhook-hub test-send --action cli --command "echo 'ping'"
./bin/webhook-hub verify               # Run full 10-step standalone E2E suite

# Contact Review & CRM Intelligence
./bin/webhook-hub review-contact --name "Adam Walker" --dry-run
./bin/webhook-hub review-contact --name "Jane Doe" --phone "+1 555 0199" --json
./bin/webhook-hub review-contact --payload '{"name": "Alice", "company": "Acme"}'
```

## 2. HTTP Ingress & API Contracts

- **Local Base URL**: `http://127.0.0.1:9423`
- **Public Cloudflare Ingress**: `https://webhook.worldinspirelab.com` (Cloudflare Tunnel `26.03.08`, proxied, TLS terminated at edge)

| Method | Path | Description | Success Code | Rejection Codes |
|---|---|---|---|---|
| `GET` | `/` | Root service descriptor or SPA HTML (`Accept: text/html`) | `200 OK` | - |
| `GET` | `/dashboard` | Observable Activity Web Dashboard (embedded SPA) | `200 OK` | - |
| `GET` | `/ui` | Dashboard alias | `200 OK` | - |
| `GET` | `/healthz` | Liveness & memory budget check | `200 OK` | `503 Service Unavailable` |
| `GET` | `/health` | Liveness health check (Coolify monitor alias) | `200 OK` | `503 Service Unavailable` |
| `GET` | `/ready` | Dispatcher & DB readiness probe | `200 OK` | `503 Service Unavailable` |
| `GET` | `/metrics` | Prometheus metrics (or JSON via `Accept`) | `200 OK` | - |
| `POST` | `/webhook` | Default ingress for webhook events | `202 Accepted` | `400`, `401`, `413` |
| `POST` | `/webhook/{source}` | Source-tagged ingress (e.g., github, stripe) | `202 Accepted` | `400`, `401`, `413` |
| `GET` | `/tasks` | Query historical tasks (`?status=&source=&q=&limit=`) | `200 OK` | `400 Bad Request` |
| `GET` | `/tasks/{id}` | Detailed task metadata & output logs | `200 OK` | `404 Not Found` |
| `POST` | `/tasks/{id}/rerun` | Re-enqueue existing task into SQLite SSOT & dispatcher | `200 OK` | `404 Not Found` |
| `GET` | `/tasks/summary` | Aggregate metrics derived directly from SQLite SSOT | `200 OK` | - |
| `GET` | `/tasks/unprocessed` | Query unprocessed, orphaned, and stale tasks | `200 OK` | `400 Bad Request` |
| `POST` | `/tasks/sweep` | Sweep, recover, and re-enqueue unprocessed tasks | `200 OK` | `400 Bad Request` |
| `GET` | `/events/stream` | Server-Sent Events live event stream | `200 OK` | - |
| `GET` | `/tasks/{id}/stream` | SSE stream scoped to specific task | `200 OK` | `404 Not Found` |

### Webhook Ingress Payload (`POST /webhook`)

```json
{
  "action": "cli",
  "command": "python3 scripts/sync_data.py --batch 10",
  "priority": 1,
  "timeout_seconds": 60,
  "action_params": {"env": "prod"}
}
```

Ingress response (Status `202 Accepted`):
```json
{
  "status": "accepted",
  "task_id": "tsk_01918a24bc10",
  "event_id": "evt_9f81a742c018"
}
```

## 3. Cryptographic Authentication Contract

Ingress routes enforce authentication before any database write.

### Mode A: HMAC-SHA256 (`auth_mode: hmac` or `both`)
Required headers:
- `X-Hub-Signature-256`: `sha256=<hex_digest>`
- `X-Hub-Timestamp`: Unix epoch integer (`abs(time() - ts) <= 300s`)

Digest algorithm:
`HMAC-SHA256(secret, timestamp + "." + raw_body_bytes)`

Python helper:
```python
import hashlib, hmac, time
body = b'{"action": "cli", "command": "echo 1"}'
ts = int(time.time())
sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
headers = {"X-Hub-Signature-256": f"sha256={sig}", "X-Hub-Timestamp": str(ts)}
```

### Mode B: Bearer Token (`auth_mode: bearer` or `both`)
Required header:
- `Authorization: Bearer <configured_token>`

## 4. Agent Signal Dispatch Pattern

When action type is `agent_signal`, the dispatcher writes an atomic JSON signal file for local Antigravity AI agents:

Target directory: `.agents/signals/<source>/<event_id>.json`
Format:
```json
{
  "signal_id": "sig_948a",
  "event_id": "evt_9f81a742c018",
  "source": "github",
  "payload": {"action": "pr_opened", "number": 42},
  "emitted_at": "2026-09-08T12:00:00Z"
}
```

## 5. SSE Live Log Streaming

Clients connect with header `Accept: text/event-stream`.
- Heartbeat: sent every 15 seconds (`: ping\n\n`) to prevent proxy timeouts.
- Buffer disabling: `X-Accel-Buffering: no` and `Cache-Control: no-cache` are standard.

Event format:
```
event: task_started
data: {"task_id": "tsk_01", "action_type": "cli", "status": "running"}

event: log
data: {"task_id": "tsk_01", "stream": "stdout", "chunk": "Processing batch 1..."}

event: task_completed
data: {"task_id": "tsk_01", "status": "succeeded", "exit_code": 0}
```

## 6. Troubleshooting Runbook

| Symptom | Cause | Remedy |
|---|---|---|
| `401 Unauthorized` | Signature mismatch or expired timestamp | Ensure clock sync within 300s; verify `HMAC(secret, ts + '.' + body)`. Use `./bin/webhook-hub test-send` to isolate. |
| `413 Payload Too Large` | Request body exceeds 1MB limit | Adjust `server.max_body_bytes` in `.env` or `config.yaml`. |
| Port in use (`errno 48`) | Existing process or zombie instance | Run `./bin/webhook-hub status` or `./bin/webhook-hub stop -f`. |
| High RAM usage (>30MB) | Worker thread stack or cache bloat | Verify SQLite PRAGMAs (`cache_size=-16`, `mmap_size=0`) and run `./bin/webhook-hub verify`. |
| Tasks stuck in `queued` | Dispatcher stopped or queue saturated | Check `./bin/webhook-hub logs` or restart hub daemon via `restart`. |
| Database locked | Concurrent long transaction | WAL mode auto-applied. Verify `data/webhook_hub.db` is on local APFS/ext4 filesystem. |
| `403 Forbidden (1010)` | Cloudflare Browser Integrity Check | Controlled by Page Rule `*webhook.worldinspirelab.com/*` (`browser_check: off`). Check with `./tunnel/start_tunnel.sh --status`. |

## 7. Contact Review & CRM Intake Pipeline (Antigravity Review Agent)

Provides intelligent deduplication, contradiction detection, property supplementation, and SSOT read-after-write verification for contacts ingested from Slack, n8n, and web forms.

### Ingress Endpoint
- **URL**: `POST /webhook/contact-review` (or `POST /webhook/contact-review?sync=true`)
- **Public Tunnel**: `https://webhook.worldinspirelab.com/webhook/contact-review`
- **Auth**: Bearer token (`Authorization: Bearer <token>`) or HMAC-SHA256 signature
- **Database**: Notion People database (`22ce1b43-2393-81a4-9443-e32e71142e0d`)

### Decision Engine & Review Verdicts
| Verdict | Trigger Condition | Execution Action |
|---|---|---|
| `NO_CHANGE` | Candidate exists; all incoming fields already match SSOT properties. | No property patch; skip mutation. |
| `SUPPLEMENT` | Candidate exists; incoming fields provide new info without conflict. | PATCH new properties, append audit note to page. |
| `CORRECT` | Candidate exists; incoming fields contradict existing properties. | Update properties to new truth, record diff history. |
| `MERGE` | Multiple candidates match or explicit merge requested. | Consolidate fields, update primary page, record audit. |
| `CREATE` | No matching candidate found in Notion database. | Create new database page with structured attributes. |

### Payload Contract
```json
{
  "source": "slack_people",
  "channel_id": "C096KR96AF7",
  "thread_ts": "1788879178.867219",
  "contact": {
    "name": "Jane Doe",
    "phone": "+1 555 0199",
    "email": "jane@example.com",
    "company": "Acme Labs",
    "title": "Head of AI",
    "city": "San Francisco",
    "country": "USA",
    "birthday": "1995-04-12",
    "url": "https://linkedin.com/in/janedoe",
    "note": "Met at AI Conference 2026"
  }
}
```

### Candidate Matching & URL Normalization Contract
To prevent duplicate records from URL formatting differences:
- **Symmetric URL Normalization**: URLs must be normalized via `normalize_url_for_comparison()` on both incoming and stored candidate properties, stripping `http://`, `https://`, `www.` prefixes, and trailing slashes.
- **Social Handle Extraction**: When querying Notion candidates, social URLs (Instagram, Twitter/X, LinkedIn) must have their handles extracted (e.g. `adamwalk` from `instagram.com/adamwalk/`) and queried against both `URL` and respective social properties.
- **Cross-Property Scoring**: Incoming URLs must be cross-checked against candidate social properties (e.g. `Instagram`) with >=85 points awarded for a handle/slug match.
- **Native Name Extraction Guard**: Upstream extractors must NEVER greedily extract unconstrained conversational text or note bullets as native names. Only explicitly labeled patterns (e.g. `姓名:`, `中文名:`, `名前:` or parentheses) are valid native names.

### Hybrid Cloud/Local Ingress Fault-Tolerance Contract
When a 24/7 cloud workflow (e.g. n8n on `n.worldinspirelab.com`) calls a local gateway via tunnel (`webhook.worldinspirelab.com`):
- **Laptop Sleep / 502 Edge Behavior**: When the laptop sleeps, Cloudflare edge returns `502 Bad Gateway`. The cloud HTTP node MUST have `neverError: true` and `onError: continueRegularOutput`.
- **Graceful Degradation**: Cloud workflows must never drop events on 502; they must either route to a cloud fallback database write or post an offline alert guiding the user to wake the laptop.

### SSOT Verification & Notification
1. **Live Re-read Verification**: After property mutation, the pipeline re-reads the page from the Notion API to verify property persistence before concluding.
2. **Slack Thread Feedback**: Posts formatted verdict banner, property diffs, and Notion deep links directly to the originating Slack channel & thread.
3. **Agent Signal**: Emits atomic JSON event to `.agents/signals/contact_review/` for downstream agents.

## 8. Mac Sleep & Battery Loss Auto-Recovery (Unprocessed Sweeper)

When a Mac sleeps or loses battery for hours (e.g. 3 hours), in-flight tasks and incoming events can become orphaned or stalled. The Webhook Hub includes built-in recovery:

1. **Monotonic Clock Jump Detection**: The dispatcher monitors elapsed time between loops. If a leap (`time.monotonic()` jump > 3x interval or > 60s) occurs due to Mac sleep or hibernation, an immediate sweep cycle is automatically triggered upon wake.
2. **Periodic Background Sweeper**: Automatically inspects SQLite SSOT every 60s (`sweeper_interval_seconds`) for tasks stranded in `running` longer than `stale_task_timeout_seconds` (default 300s).
3. **Orphaned Webhook Rehydration**: Any `webhook_events` with `status = 'received'` that never had a task created (e.g., sudden power loss) are promoted to tasks and enqueued.
4. **Interrupted Task Retry**: Any tasks that failed with network/restart/timeout signatures are automatically retried up to `max_retries`.
5. **On-Demand Sweeping**: Agents or cron jobs can inspect or trigger recovery via `./bin/webhook-hub sweep` (or `POST /tasks/sweep`) at any time.

## 9. Antigravity Sidebar & Observable Activity Console

Activities triggered by Webhook Hub are observable through two synchronized surfaces:

1. **Antigravity IDE Sidebar**: Registered as scheduled sentinel `webhook-hub-sentinel` (Cadence Card `CAD-20260911-webhook-hub-sentinel`). When incoming webhooks trigger `agent_signal` or review tasks, event snapshots are recorded to `$HOME/.antigravity/sidecar_data/webhook-hub-sentinel/events/*.json`, making activities directly visible in the IDE's Scheduled Tasks / Sidecars panel.
2. **Embedded Web Console (`/dashboard` & `/ui`)**: Zero-dependency dark slate web interface with live SSE streaming (`/events/stream`), real-time RSS memory gauge (<30MB budget), task filtering, live stdout/stderr inspection drawer, one-click re-run, and interactive webhook simulator.



