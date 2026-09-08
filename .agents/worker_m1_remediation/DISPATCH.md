## 2026-09-08T11:00:27Z
You are Worker M1 Remediation for Milestone M1 (Ingress Gateway & Security Core).
Your working directory is: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/worker_m1_remediation
Authoritative User Request: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/ORIGINAL_REQUEST.md
Project Specification: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/PROJECT.md
Reviewer M1-1 Report: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/reviewer_m1_1/handoff.md
Challenger M1-2 Report: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/challenger_m1_2/handoff.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Scope & Exclusively Owned Files:
- hub/server.py
- hub/security.py
- tunnel/start_tunnel.sh
- tunnel/quick_tunnel.sh
- .gitignore

Remediation Tasks:
1. In `hub/server.py`:
   a) Shutdown Deadlock Fix: In `AsyncHTTPServer.stop()`, cancel all active tasks (`self._active_tasks`) and close connection transports BEFORE `await self._server.wait_closed()`. This ensures that shutdown does not hang when keepalive or SSE connections are active.
   b) Negative Content-Length Validation: In request parsing, validate that `content_length >= 0`. If `content_length < 0`, immediately return `400 Bad Request`.
   c) Request Smuggling Protection: If a request contains BOTH `Content-Length` and `Transfer-Encoding`, reject with `400 Bad Request`.
   d) Timeout on Body Reads: Apply a timeout wrapper (`asyncio.wait_for(..., timeout=self.request_timeout)`) on body reading loops (`readexactly` and chunked reading).
2. In `hub/security.py`:
   - In `compute_dedup_hash`, normalize the signature to lowercase (`sig.strip().lower()`) before hashing.
3. In `tunnel/start_tunnel.sh` and `tunnel/quick_tunnel.sh`:
   - Fix macOS bash 3.2 incompatibility with `source <(...)`. Use portable dot-sourcing:
     `[ -f "${ROOT_DIR}/.env" ] && set -a && . "${ROOT_DIR}/.env" && set +a`
4. Add `.gitignore` at project root:
   - Ignore `.env`, `tunnel/config.yml`, `*.db`, `*.db-wal`, `*.db-shm`, `__pycache__/`, `.pytest_cache/`, `*.pyc`.
5. Verification:
   - Run `python3 scripts/challenge_m1_server_stress.py` to ensure all 16 stress tests pass.
   - Run `python3 -m pytest tests/unit/ -v` to ensure all unit tests pass.
   - Run `python3 scripts/verify_e2e.py` to verify the 9-step E2E checks pass.

Write your handoff report to:
/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/worker_m1_remediation/handoff.md
Send a completion message back when done.
