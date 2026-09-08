---
name: webhook-hub
description: Control, monitor, and query the local Antigravity Webhook Hub daemon and event dispatcher on macOS.
version: 1.0.0
author: V
date: 2026-09-08
source: local repository
---

# Webhook Hub Skill

Lightweight (<30MB RAM, 0% idle CPU) macOS webhook gateway and event dispatcher for Antigravity AI agents. Provides secure HMAC/Bearer ingress, decoupled execution queues, SQLite SSOT logging, and live SSE streaming.

## 1. Quick Invocations

All commands run via `./bin/webhook-hub <subcommand>` or `python3 -m hub <subcommand>`.

```bash
# Daemon management
./bin/webhook-hub start -d             # Start gateway daemon on default port 9423
./bin/webhook-hub status               # Check process liveness & health metrics
./bin/webhook-hub status --json        # Machine-readable JSON status for agents
./bin/webhook-hub stop                 # Gracefully terminate daemon via PID file

# Inspection & Streaming
./bin/webhook-hub logs --lines 20      # Read last 20 execution logs from SQLite
./bin/webhook-hub logs --task tsk_123  # Inspect single task execution output
./bin/webhook-hub logs -f              # Follow live global SSE event stream

# Testing & Verification
./bin/webhook-hub test-send --action cli --command "echo 'ping'"
./bin/webhook-hub verify               # Run full 9-step standalone E2E suite
```

## 2. HTTP Ingress & API Contracts

- **Local Base URL**: `http://127.0.0.1:9423`
- **Public Cloudflare Ingress**: `https://webhook.worldinspirelab.com` (Cloudflare Tunnel `26.03.08`, proxied, TLS terminated at edge)

| Method | Path | Description | Success Code | Rejection Codes |
|---|---|---|---|---|
| `GET` | `/` | Root service descriptor & running status | `200 OK` | - |
| `GET` | `/healthz` | Liveness & memory budget check | `200 OK` | `503 Service Unavailable` |
| `GET` | `/ready` | Dispatcher & DB readiness probe | `200 OK` | `503 Service Unavailable` |
| `GET` | `/metrics` | Prometheus metrics (or JSON via `Accept`) | `200 OK` | - |
| `POST` | `/webhook` | Default ingress for webhook events | `202 Accepted` | `400`, `401`, `413` |
| `POST` | `/webhook/{source}` | Source-tagged ingress (e.g., github, stripe) | `202 Accepted` | `400`, `401`, `413` |
| `GET` | `/tasks` | Query historical tasks (`?status=&limit=`) | `200 OK` | `400 Bad Request` |
| `GET` | `/tasks/{id}` | Detailed task metadata & output logs | `200 OK` | `404 Not Found` |
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
