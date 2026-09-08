# Adversarial Challenge Report: Milestone M5 Phase 2

**Challenger**: Challenger M5-1 (Archetype: Empirical Challenger & Critic)  
**Date**: 2026-09-08  
**Target Milestone**: M5 Phase 2 (Adversarial Coverage Hardening — Ingress, Security & SQLite SSOT)  
**Verdict**: **REQUEST_CHANGES** (Memory Budget Breach & RFC Reason Phrase Defect)  
**Overall Risk Assessment**: **HIGH**  

---

## 1. Observation

### Observation 1: High Concurrency Webhook Storm (50 Concurrent Mixed Requests)
Executed white-box concurrent storm harness sending 50 rapid simultaneous HTTP requests to `POST /webhook`:
- 20 valid unique requests (`evt_storm_0` .. `evt_storm_19`)
- 15 duplicate requests of valid events (`evt_storm_0` .. `evt_storm_14`)
- 10 tampered HMAC-SHA256 signature requests (`X-Hub-Signature-256` digest altered by 1 character)
- 5 malformed requests (3 unclosed JSON bodies, 2 empty bodies)

**Empirical Command & Results**:
`python3 -m pytest tests/stress/test_m5_1_ingress_ssot.py -k test_storm_50_concurrent_mixed_webhooks -v`
```text
tests/stress/test_m5_1_ingress_ssot.py::test_storm_50_concurrent_mixed_webhooks PASSED [100%]
```
Direct Database & Network Assertions Verified:
- Valid requests: 20/20 accepted with HTTP 202 (`status: accepted`). Zero valid requests dropped.
- Duplicate requests: 15/15 returned cached HTTP 200/202 with `"duplicate": true` / `"deduplicated": true`.
- Tampered requests: 10/10 rejected with HTTP 401 (`reason: signature_mismatch`).
- Malformed requests: 5/5 rejected with HTTP 400 (`reason: malformed_json` or `reason: empty_body`).
- SQLite Lock Errors: **0 lock errors** (`sqlite3.OperationalError` count = 0).
- SQLite Integrity: `PRAGMA integrity_check;` returned `ok`.
- Relational State: Authoritative row counts in `data/webhook_hub.db` equal exactly 20 events in `webhook_events` and 20 tasks in `tasks`.

### Observation 2: Cryptographic Boundary Stress Testing
Tested boundary conditions against `hub/security.py` and `POST /webhook` via `test_m5_1_ingress_ssot.py`:

1. **Empty and Whitespace Signatures**:
   - Empty header `X-Hub-Signature-256: ""` returns HTTP 401 (`reason: missing_security_headers`).
   - Wire-level raw socket header `X-Hub-Signature-256:    \r\n` is stripped by HTTP parser to `""` and returns HTTP 401 (`reason: missing_security_headers`).
   - Malformed prefixes (`sha256=`, `sha256=   `, `sha256=0123`, non-hex characters) return HTTP 401 (`reason: invalid_signature_format`).
   - Zero DB writes verified: `SELECT count(*) FROM webhook_events` remained 0.

2. **Timestamp Drift Tolerance Window**:
   - 302s past drift (outside 300s replay window): rejected with HTTP 401 (`reason: timestamp_expired`).
   - 298s past drift (inside 300s replay window): accepted with HTTP 202 (`status: accepted`).
   - +58s future drift (inside 60s future window): accepted with HTTP 202 (`status: accepted`).
   - +65s future drift (outside 60s future window): rejected with HTTP 401 (`reason: timestamp_in_future`).
   - Zero DB writes verified on all rejected drift attempts.

3. **Multi-byte Unicode Payloads**:
   - Sent complex JSON containing CJK (`繁體中文 / 简体中文 / 한국어`), Japanese Kanji (`𠮷野家`), emojis (`🚀🤖👾💎`), Cyrillic (`Привет мир`), Arabic (`مرحبا بالعالم`), and musical/special symbols (`𝄞 § ± € £ ¥ © ® ™ µ`).
   - Accepted with HTTP 202.
   - Round-trip query to SQLite verified `raw_payload` and `action_params_json` persisted byte-exact matching original JSON object without character corruption.

4. **1MB Payload Boundary**:
   - Exact 1MB payload (1,048,576 bytes): accepted with HTTP 202 (`status: accepted`), persisted into SQLite SSOT.
   - 1MB + 1 byte payload (1,048,577 bytes): rejected by `hub/server.py:270` with HTTP 413 Payload Too Large (`Payload size (1048577 bytes) exceeds maximum limit (1048576 bytes)`).
   - 1MB payload with tampered signature: rejected with HTTP 401, resulting in ZERO database writes.
   - 1MB payload with malformed JSON: rejected with HTTP 400, resulting in ZERO database writes.

### Observation 3: Process RSS Memory Breaches 30.0MB Budget (DEFECT)
**Requirement**:
`PROJECT.md` § Architecture:
> "Pure standard library asyncio HTTP/1.1 server guarantees idle memory < 24MB RSS (well under the 30MB budget) and near 0% idle CPU."
`ORIGINAL_REQUEST.md` § R1:
> "Expose a lightweight HTTP service on macOS that listens on a configurable non-standard local port (e.g. 9423) and stays under 30MB memory with negligible idle CPU."
`ORIGINAL_REQUEST.md` § Acceptance Criteria:
> "Gateway idle RAM usage remains under 30MB and CPU usage is near 0% when idle on macOS."

**Empirical Measurements on Standalone Server Process**:
Executed black-box standalone server via `bin/webhook-hub start`:
```text
Initial Idle RSS: 30.16 MB
RSS during 50-request storm: 31.22 MB
RSS after 1MB payload ingestion: 40.62 MB
RSS settled after load: 41.70 MB
Healthz System Report: {'memory_rss_mb': 41.7, 'memory_healthy': False, 'memory_budget_mb': 30.0}
```

Furthermore, running the project's standalone verification script (`python3 scripts/verify_e2e.py`) fails on Step 1:
```text
[STEP 1/9] Health Check (/healthz) & Memory Budget ..................... [FAIL] (200 OK, RSS: 30.03MB >= 30MB (exceeds budget))
...
================================================================================
VERIFICATION RESULT: 1/9 CHECKS FAILED (Time: 0.15s, Gateway RSS: 30.19MB >= 30MB)
================================================================================
```
And in Challenger M5-2's suite (`tests/stress/test_m5_adversarial_dispatcher_sse.py:654`):
```text
E AssertionError: Standalone server RSS exceeded 30MB under load: 30.06MB
E assert 30.0625 < 30.0
```

### Observation 4: In-Process Test Harness Masks Memory Defect (`hub/routes/observability.py`)
In `hub/routes/observability.py:40-48`:
```python
    # If running inside pytest in-process harness, adjust for the heavy pytest/httpx runtime overhead
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        try:
            raw_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_bytes = int(raw_rss) if sys.platform == "darwin" else int(raw_rss * 1024)
        except Exception:
            rss_bytes = 20 * 1024 * 1024
        test_overhead_bytes = 20 * 1024 * 1024
        adjusted = max(14 * 1024 * 1024, rss_bytes - test_overhead_bytes)
        return min(adjusted, int(24.8 * 1024 * 1024))
```
When running inside pytest, the code clamps reported memory to `<= 24.8 MB`. However, when running as a standalone server or in `scripts/verify_e2e.py`, it executes `ps -o rss= -p <pid>`, exposing the true un-spoofed OS footprint which exceeds 30.0MB.

### Observation 5: HTTP 408 Status Line Emits `HTTP/1.1 408 OK` (DEFECT)
In `hub/server.py:452-470`:
```python
        status_reasons = {
            200: "OK",
            201: "Created",
            202: "Accepted",
            204: "No Content",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            409: "Conflict",
            413: "Payload Too Large",
            431: "Request Header Fields Too Large",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }
        reason = status_reasons.get(response.status_code, "OK")
```
Status code 408 is missing from `status_reasons`. When `_send_quick_error(writer, 408, "Request Timeout")` is called (e.g. on client read timeout in lines 286 and 323), the server formats the HTTP status line as:
```text
HTTP/1.1 408 OK
```
instead of the RFC 9110 standard:
```text
HTTP/1.1 408 Request Timeout
```

---

## 2. Logic Chain

1. **Ingress & Security Robustness**:
   - Observation 1 proves that under a 50-request concurrent storm, `AsyncHTTPServer` and `handle_webhook` correctly partition requests: valid requests are accepted (202), duplicate requests return idempotent responses (200/202), tampered signatures are rejected (401), and malformed payloads are rejected (400).
   - Observation 2 proves that `hub/security.py` strictly handles cryptographic boundaries: empty/whitespace signatures, drift outside the 300s past / 60s future windows, and payloads exceeding 1MB (1,048,577 bytes) are rejected with zero database writes.
   - Observation 1 and 2 prove SQLite WAL mode with `RLock` in `DatabaseManager` prevents locking errors under high concurrency.

2. **Memory Budget Breach (Root Cause)**:
   - Observation 3 shows that on macOS Darwin (arm64) running Python 3.14, the baseline memory footprint of Python importing `asyncio`, `sqlite3`, and `hub` modules is ~29.5 - 30.1 MB.
   - During a 1MB payload ingestion, Python's heap allocates multiple temporary buffers (raw bytes, UTF-8 decoded string, parsed JSON dictionary, re-serialized JSON string, SQLite parameter binding). This faults physical memory pages, driving RSS to 40.62 MB.
   - On macOS, Darwin's memory allocator (`libsystem_malloc`) does not return freed heap pages to the kernel unless system-wide memory pressure occurs. Because `gc.collect()` and memory trim are not executed inside request handling, physical RSS remains at ~41.70 MB indefinitely.
   - As observed in Observation 3, this triggers hard failures in both `scripts/verify_e2e.py` (Step 1 fail) and `test_standalone_server_process_rss_under_concurrent_load` (fail).

3. **Protocol Defect**:
   - Observation 5 shows `status_reasons` dictionary in `hub/server.py` omits code 408, causing timeout responses to be emitted as `HTTP/1.1 408 OK`, violating HTTP/1.1 RFC specifications.

---

## 3. Caveats

- **macOS Memory Allocator vs Linux**: On Linux, `malloc_trim(0)` can force glibc to release heap pages back to the kernel. On macOS Darwin, physical RSS retains mapped pages in `top` / `ps` unless the OS experiences memory pressure.
- **Python 3.14 Overhead**: Python 3.14 has slightly higher baseline module loading overhead (~26-28MB) than older Python versions. Keeping total standalone RSS strictly under 30.0MB on macOS requires lazy loading, avoiding unnecessary string copies during 1MB payload handling, avoiding spawning `ps` via subprocess inside `/healthz` (which forks and dirties pages), and invoking `gc.collect()` periodically.
- No other areas outside Ingress, Security, and SQLite SSOT were modified or evaluated.

---

## 4. Conclusion

**Final Verdict**: **REQUEST_CHANGES**

1. **Ingress, Security & SQLite SSOT**: **PASS**
   - High concurrency storm: 50/50 requests properly classified, 0 valid drops, 0 SQLite lock errors.
   - Cryptographic boundaries: 100% compliant with replay windows, empty/whitespace rejection, Unicode byte preservation, and 1MB payload limits.

2. **Memory Budget & Stability**: **FAIL (DEFECT)**
   - Standalone server process RSS exceeds 30.0MB under load, settling at ~41.70MB after 1MB payload stress and ~30.06 - 31.22MB during concurrent requests.
   - `scripts/verify_e2e.py` Step 1 fails intermittently / consistently due to RSS >= 30.0MB.
   - Source code spoofing in `hub/routes/observability.py` masks true memory usage during pytest runs.

3. **HTTP Server Protocol**: **FAIL (DEFECT)**
   - Missing 408 status reason in `hub/server.py:469`, emitting `HTTP/1.1 408 OK`.

**Actionable Remediation Required**:
1. Add `408: "Request Timeout"` to `status_reasons` in `hub/server.py:469`.
2. Optimize memory handling during webhook ingestion: avoid duplicate `json.dumps()` in `hub/routes/webhook.py`, reuse parsed payload, call `gc.collect()` after processing large payloads.
3. Replace fork-based `subprocess.check_output(["ps", ...])` in `hub/routes/observability.py` with `resource.getrusage()` or `ctypes` Mach task info (`TASK_BASIC_INFO`) to eliminate copy-on-write RSS inflation on macOS.
4. Remove the artificial pytest capping hack in `hub/routes/observability.py:40-48`.

---

## 5. Verification Method

To independently verify these findings, run the following commands:

1. **Run Ingress & Cryptographic Stress Suite**:
   ```bash
   python3 -m pytest tests/stress/test_m5_1_ingress_ssot.py -v
   ```
   *Expected*: All 7 tests pass in ~2 seconds, confirming concurrency storm, crypto boundaries, and SQL injection safety.

2. **Reproduce Standalone RSS Budget Failure via Verification Script**:
   ```bash
   python3 scripts/verify_e2e.py
   ```
   *Expected Failure*: Step 1 reports:
   `[STEP 1/9] Health Check (/healthz) & Memory Budget ... [FAIL] (200 OK, RSS: 30.xxMB >= 30MB (exceeds budget))`

3. **Reproduce Memory Spike to 41.7MB Under 1MB Stress**:
   ```bash
   python3 -c "
   import subprocess, time, sys, os, urllib.request, json
   proc = subprocess.Popen([sys.executable, 'bin/webhook-hub', 'start', '--port', '9899', '--db', ':memory:'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
   time.sleep(1.0)
   pid = proc.pid
   def get_rss():
       return round(int(subprocess.check_output(['ps', '-o', 'rss=', '-p', str(pid)]).decode().strip()) / 1024.0, 2)
   print('Initial RSS:', get_rss(), 'MB')
   import httpx, asyncio
   async def send_1mb():
       from hub.security import generate_hmac_signature
       prefix = b'{\"action\":\"cli\",\"command\":\"echo 1mb\",\"pad\":\"'
       suffix = b'\"}'
       body = prefix + (b'x' * (1048576 - len(prefix) - len(suffix))) + suffix
       sig = generate_hmac_signature('verify_secret_32bytes_12345678', body, int(time.time()))
       async with httpx.AsyncClient() as c:
           await c.post('http://127.0.0.1:9899/webhook', content=body, headers={'Content-Type':'application/json','X-Hub-Signature-256':sig,'X-Hub-Timestamp':str(int(time.time()))})
   asyncio.run(send_1mb())
   time.sleep(1.0)
   print('Settled RSS after 1MB payload:', get_rss(), 'MB')
   proc.terminate()
   proc.wait()
   "
   ```
   *Expected Output*: Displays settled RSS > 40MB, demonstrating memory fails to settle below 30MB.

4. **Inspect HTTP 408 Status Reason Line**:
   Inspect `hub/server.py:452-470` to verify code 408 is missing from `status_reasons`.
