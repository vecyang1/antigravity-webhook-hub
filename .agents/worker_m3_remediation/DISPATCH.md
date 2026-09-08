## 2026-09-08T11:46:31Z
You are Worker M3 Remediation for Milestone M3 (Real-Time Observability & Push-Based Live Queries).
Your working directory is: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/worker_m3_remediation
Authoritative User Request: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/ORIGINAL_REQUEST.md
Project Specification: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/PROJECT.md
Challenger M3-1 Report: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/challenger_m3_1/handoff.md
Challenger M3-2 Report: /Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/challenger_m3_2/handoff.md

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Scope & Exclusively Owned Files:
- hub/routes/observability.py
- hub/db.py

Remediation Tasks:
1. In `hub/routes/observability.py`:
   - In `handle_task_detail` (lines ~264-288), extract BOTH `stdout` and `stderr` independently:
     ```python
     stdout_lines = [row["line"] for row in logs if row["stream_type"] == "stdout"]
     stderr_lines = [row["line"] for row in logs if row["stream_type"] == "stderr"]
     if stdout_lines:
         result["stdout"] = "\n".join(stdout_lines)
     if stderr_lines:
         result["stderr"] = "\n".join(stderr_lines)
     ```
     Do NOT use an `if/else` that suppresses `stderr` when `stdout` exists.
2. In `hub/db.py`:
   - In `_apply_pragmas`, enforce `PRAGMA cache_size = -2000;` (2MB memory cache) and `PRAGMA temp_store = FILE;` to ensure physical RSS stays strictly <30MB even under 10-20 concurrent SSE streams.
3. Verification:
   - Run `python3 -m pytest tests/stress/test_m3_challenger.py tests/stress/test_m3_stress.py -v`.
   - Run `python3 -m pytest tests/ -v`.
   - Run `python3 scripts/verify_e2e.py`.
   - Verify that all tests pass 100% and memory RSS is <30MB.

Write your handoff report to:
/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/worker_m3_remediation/handoff.md
Send a completion message back when done.
