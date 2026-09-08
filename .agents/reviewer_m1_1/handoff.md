# Milestone M1 Gate Review & Adversarial Audit Report

## Review Summary
- **Target**: Milestone M1 (Ingress Gateway & Security Core)
- **Reviewer**: Reviewer M1-1 (reviewer & critic)
- **Verdict**: **REQUEST_CHANGES**
- **Integrity Check**: **PASSED** (Zero integrity violations; genuine implementation, no dummy logic or hardcoded test facades)
- **Reason for Verdict**: 1 Major Finding on macOS `/bin/bash` 3.2 where Cloudflare Tunnel scripts fail to export `.env` variables due to subshell process substitution, causing user configurations to be silently ignored. 2 Minor Findings for defense-in-depth.

---

## 1. Observation

### 1.1 Integrity Check
- `hub/server.py`: Fully functional, zero-dependency async HTTP/1.1 server using Python standard library `asyncio`. No mock responses or bypassed logic.
- `hub/security.py`: Real cryptographic HMAC SHA-256 and Bearer token verification using `hashlib`, `hmac.new`, and `hmac.compare_digest`. No hardcoded test tokens or bypass branches.
- `hub/config.py`: Real cascading configuration loader with four-tier priority hierarchy (CLI > .env > YAML > defaults). No hardcoded credentials.
- `hub/models.py`: Clean slots dataclasses and deterministic CAS state machine models.
- **Integrity Finding**: Zero integrity violations found. No cheating, no facade implementations, no fabricated logs.

### 1.2 Independent Build and Test Execution
- **Unit Tests (`pytest tests/unit/ -v`)**:
  ```
  platform darwin -- Python 3.14.7, pytest-9.1.1
  tests/unit/test_config.py::test_default_config PASSED
  tests/unit/test_config.py::test_cli_overrides_precedence PASSED
  tests/unit/test_config.py::test_dot_notation_cli_overrides PASSED
  tests/unit/test_config.py::test_env_file_loading PASSED
  tests/unit/test_config.py::test_yaml_config_with_env_interpolation PASSED
  tests/unit/test_config.py::test_validation PASSED
  tests/unit/test_models.py::test_task_status_state_machine PASSED
  tests/unit/test_models.py::test_webhook_event_serialization PASSED
  tests/unit/test_models.py::test_task_serialization PASSED
  tests/unit/test_models.py::test_http_request_helpers PASSED
  tests/unit/test_models.py::test_http_response_factory PASSED
  tests/unit/test_security.py::test_hmac_valid_signature_with_prefix PASSED
  tests/unit/test_security.py::test_hmac_valid_signature_without_prefix PASSED
  tests/unit/test_security.py::test_hmac_missing_headers PASSED
  tests/unit/test_security.py::test_hmac_invalid_timestamp_format PASSED
  tests/unit/test_security.py::test_hmac_expired_timestamp PASSED
  tests/unit/test_security.py::test_hmac_future_timestamp_rejected PASSED
  tests/unit/test_security.py::test_hmac_acceptable_future_timestamp PASSED
  tests/unit/test_security.py::test_hmac_tampered_payload_rejected PASSED
  tests/unit/test_security.py::test_hmac_tampered_signature_rejected PASSED
  tests/unit/test_security.py::test_bearer_token_valid PASSED
  tests/unit/test_security.py::test_bearer_token_invalid PASSED
  tests/unit/test_security.py::test_bearer_token_missing_or_malformed PASSED
  tests/unit/test_security.py::test_validate_request_security_modes PASSED
  tests/unit/test_security.py::test_hash_utilities PASSED
  tests/unit/test_server.py::test_server_lifecycle_and_routes[asyncio] PASSED
  ======================== 26 passed, 12 skipped in 0.26s ========================
  ```
- **Smoke & Performance Test (`python3 scripts/smoke_test_m1.py`)**:
  - Baseline Idle RSS: **28.62 MB** (Limit: <30.0 MB)
  - Baseline Idle CPU: **0.1%** (Settled)
  - All 6 socket request tests (Healthz, valid HMAC, missing auth, tampered signature, expired timestamp, Bearer auth) returned expected status codes (200, 202, 401).
  - SSE streaming delivered chunked event stream with proper terminating chunk `0\r\n\r\n`.
  - Post-traffic RSS: **28.72 MB** (Well under 30.0 MB budget).

### 1.3 Adversarial Stress Testing Results
- **Concurrency Burst (100 parallel requests)**:
  - Total requests served: 100 / 100
  - Post-traffic RSS: **29.44 MB** (<30.0 MB limit)
  - Active connections returned to 0 without socket leaks.
- **Protocol Bounds**:
  - Header buffer overrun (>64KB without CRLF) handled cleanly by asyncio limit without crashing server.
  - Incomplete client body handled cleanly without hanging worker loop.
  - Chunked request payload exceeding `max_body_bytes` cleanly returns 413 Payload Too Large.
  - Client abrupt disconnect during SSE streaming caught without throwing unhandled exceptions.
  - Uncaught route handler exception returns 500 Internal Server Error without killing server loop.
- **Cryptographic Bounds (`hub/security.py`)**:
  - Exact past drift threshold (`now - 300s`) passes; `now - 301s` rejected with `timestamp_expired`.
  - Exact future threshold (`now + 60s`) passes; `now + 61s` rejected with `timestamp_in_future`.
  - Binary and null-byte payloads authenticated accurately without string truncation.

### 1.4 Observed Defect in Tunnel Scripts
- **File**: `tunnel/start_tunnel.sh` line 17 & `tunnel/quick_tunnel.sh` line 26:
  ```bash
  14: if [[ -f "${ROOT_DIR}/.env" ]]; then
  15:   set -a
  16:   # shellcheck disable=SC1091
  17:   source <(grep -v '^#' "${ROOT_DIR}/.env" | grep -v '^\s*$')
  18:   set +a
  19: fi
  ```
- **Execution under macOS default `/bin/bash` (GNU bash 3.2.57)**:
  ```bash
  $ /bin/bash --version
  GNU bash, version 3.2.57(1)-release (arm64-apple-darwin25)
  ```
- **Direct Test**:
  ```bash
  echo "PORT=9999" > .env
  ./tunnel/start_tunnel.sh --init
  grep "service:" tunnel/config.yml
  # Observed Output:
  # service: http://127.0.0.1:9423
  ```
  `PORT` remained 9423 because process substitution in `source <(...)` in bash 3.2 runs in a subshell, failing to export environment variables into the parent script.

---

## 2. Logic Chain

1. **Adherence to Requirements (R1)**:
   - R1 specifies running on **macOS** and states: *"All configuration parameters (port, secrets, token, log level) are fully configurable via `.env` / config file."*
   - R1 also specifies: *"Provide ingress integration recipes and scripts for Cloudflare Tunnel (`cloudflared`) to expose a secure HTTPS endpoint to the external website without opening inbound router ports."*
2. **Defect Mechanism**:
   - Both `tunnel/start_tunnel.sh` and `tunnel/quick_tunnel.sh` use `#!/usr/bin/env bash`.
   - On macOS, `/usr/bin/env bash` resolves to the system `/bin/bash` (version 3.2.57).
   - In bash 3.2, process substitution `source <(cmd)` does not propagate exported variables to the parent shell.
   - Consequently, when a user defines `PORT=9999`, `CLOUDFLARE_TUNNEL_NAME="custom-tunnel"`, or `CLOUDFLARE_TUNNEL_HOSTNAME="custom.domain.com"` in `.env`, the tunnel scripts silently fail to read them, falling back to built-in default values (`9423`, `antigravity-webhook-tunnel`, `webhook.yourdomain.com`).
3. **Impact**:
   - The user cannot customize the tunnel port or tunnel name through `.env`, leading to traffic being forwarded to the wrong port or domain if a non-default port is used.
   - Because this affects user-facing ingress scripts on the target OS, this constitutes a **Major Finding**.
4. **Additional Defense-in-Depth Observations**:
   - In `hub/security.py:25-34`, `compute_dedup_hash` computes a hash over the signature hex string. While it strips `sha256=`, it does not convert the signature to `.lower()`. Since `verify_hmac_signature` accepts case-insensitive hex signatures, sending identical requests with uppercase hex would produce differing deduplication hashes.
   - The repository lacks a `.gitignore` to prevent generated deployment secrets (`.env`, `tunnel/config.yml`) from being tracked in version control.

---

## 3. Findings

### [Major] Finding 1: Bash 3.2 Subshell Process Substitution in Tunnel Scripts
- **Where**: `tunnel/start_tunnel.sh:17` and `tunnel/quick_tunnel.sh:26`
- **Why**: `source <(...)` runs in a subshell on macOS bash 3.2, preventing `.env` variables from exporting to the executing script.
- **Suggestion**: Replace lines 14–19 in both scripts with standard dot-sourcing:
  ```bash
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "${ROOT_DIR}/.env"
    set +a
  fi
  ```
  Testing verified that `. "${ROOT_DIR}/.env"` works reliably on macOS bash 3.2, correctly reading and exporting `PORT=9999` and all other variables.

### [Minor] Finding 2: Missing Lowercase Normalization in `compute_dedup_hash`
- **Where**: `hub/security.py:30-34`
- **Why**: If a webhook provider or client transmits signature in uppercase (e.g. `X-Hub-Signature-256: SHA256=A1B2...`), `verify_hmac_signature` validates it successfully, but `compute_dedup_hash` preserves uppercase, producing a different dedup hash than a lowercase signature.
- **Suggestion**: In `compute_dedup_hash`:
  ```python
  cleaned_sig = signature.strip()
  if cleaned_sig.lower().startswith("sha256="):
      cleaned_sig = cleaned_sig[7:]
  cleaned_sig = cleaned_sig.lower()
  ```

### [Minor] Finding 3: Missing `.gitignore` for Deployment Secrets & Database
- **Where**: Repository Root
- **Why**: Running `./tunnel/start_tunnel.sh --init` creates `tunnel/config.yml` which may contain live domain names and credentials paths. Storing local `.env` or `data/webhook_hub.db` could accidentally lead to git commits containing secrets.
- **Suggestion**: Add `.gitignore` ignoring `.env`, `tunnel/config.yml`, `data/`, `*.db*`, `__pycache__/`, `.pytest_cache/`.

---

## 4. Caveats
- Route integration with SQLite SSOT (`hub/db.py`) and async task runner (`hub/dispatcher.py`) belongs to Milestone M2; tests for these modules were skipped as expected.
- Cloudflare Tunnel execution scripts were tested for syntax, help, configuration generation, and binary checks; actual external live routing requires active Cloudflare credentials and domain ownership.

---

## 5. Conclusion
Milestone M1 is well-architected, zero-dependency, and adheres strictly to the memory budget (<30MB) and CPU budget (~0%). There are no integrity violations.

However, because the Cloudflare Tunnel scripts fail to export environment variables from `.env` on macOS default bash 3.2, the gate verdict is **REQUEST_CHANGES**.

Once Worker M1 applies the portable `.env` sourcing fix to `tunnel/start_tunnel.sh` and `tunnel/quick_tunnel.sh` (and addresses the minor signature lowercasing in `hub/security.py`), Milestone M1 will be ready for immediate approval.

---

## 6. Verification Method

To independently verify the reported findings and fixes:

1. **Verify Bash 3.2 Issue in Tunnel Script**:
   ```bash
   echo "PORT=9999" > .env
   ./tunnel/start_tunnel.sh --init
   grep "service:" tunnel/config.yml
   rm -f .env
   ```
   *Expected defect behavior*: Shows `9423` instead of `9999`.
   *Expected post-fix behavior*: Shows `9999`.

2. **Verify Python Test Suite**:
   ```bash
   python3 -m pytest tests/unit/ -v
   ```
   *Expected result*: 26 passed, 12 skipped (M2 tests), 0 failures.

3. **Verify Memory & Idle CPU Benchmark**:
   ```bash
   python3 scripts/smoke_test_m1.py
   ```
   *Expected result*: All 6 assertions pass, Idle RSS < 30MB, process exits with code 0.
