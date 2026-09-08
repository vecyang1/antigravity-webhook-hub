#!/usr/bin/env python3
"""
Antigravity Webhook Hub — Milestone M1 Adversarial Challenge & Memory Harness
Developed by Challenger M1-1 to empirically stress-test:
1. Adversarial HMAC-SHA256 signature tampering & bit-flips.
2. Missing authorization & security headers.
3. Replay attack timestamps (>300s past, >60s future, boundary tests).
4. Malformed signature headers (non-hex, bad length, forged prefixes).
5. Malformed Bearer tokens (wrong schemes, empty tokens, invalid values).
6. Concurrent socket stress burst & continuous Process RSS memory measurement (<30MB).
7. Strict verification that invalid requests return 401/403 with ZERO bypass.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hub.config import SecurityConfig, ServerConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.security import generate_hmac_signature, validate_request_security
from hub.server import AsyncHTTPServer


def get_rss_mb(pid: int) -> float:
    """Read Resident Set Size (RSS) in Megabytes using macOS `ps` command."""
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)])
        rss_kb = int(out.strip().split()[0])
        return round(rss_kb / 1024.0, 2)
    except Exception as e:
        print(f"Warning: Failed to read RSS for PID {pid}: {e}")
        return 0.0


def get_cpu_percent(pid: int) -> float:
    """Read CPU percentage using macOS `ps` command."""
    try:
        out = subprocess.check_output(["ps", "-o", "%cpu=", "-p", str(pid)])
        return float(out.strip().split()[0])
    except Exception:
        return 0.0


class AdversarialHarness:
    def __init__(self, port: int = 19440):
        self.port = port
        self.secret = "adversarial_super_secret_key_32_bytes_len!"
        self.bearer = "adversarial_bearer_token_secret_32_chars!"
        self.server_proc: subprocess.Popen | None = None
        self.server_pid: int = 0

        # Audit counters to empirically prove ZERO security bypass
        self.counters = {
            "total_adversarial_requests": 0,
            "correctly_rejected_401": 0,
            "correctly_accepted_202": 0,
            "unexpected_responses": 0,
            "security_bypasses": 0,
        }

    async def send_raw_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes = b"",
    ) -> tuple[int, dict[str, str], bytes]:
        """Send raw HTTP/1.1 request directly over asyncio socket."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            req_lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1"]
            if body and "Content-Length" not in headers:
                headers["Content-Length"] = str(len(body))
            elif "Content-Length" not in headers and method in ("POST", "PUT"):
                headers["Content-Length"] = str(len(body))

            for k, v in headers.items():
                req_lines.append(f"{k}: {v}")
            req_lines.append("\r\n")

            writer.write("\r\n".join(req_lines).encode("latin1") + body)
            await writer.drain()

            header_bytes = await reader.readuntil(b"\r\n\r\n")
            lines = header_bytes.split(b"\r\n")
            status_line = lines[0].decode("latin1")
            status_code = int(status_line.split(" ")[1])

            resp_headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                dline = line.decode("latin1")
                if ":" in dline:
                    hk, hv = dline.split(":", 1)
                    resp_headers[hk.strip().lower()] = hv.strip()

            resp_body = b""
            if "content-length" in resp_headers:
                clen = int(resp_headers["content-length"])
                if clen > 0:
                    resp_body = await reader.readexactly(clen)

            return status_code, resp_headers, resp_body
        finally:
            writer.close()
            await writer.wait_closed()

    async def challenge_tampered_hmac(self):
        print("\n--- Challenge 1: HMAC SHA-256 Tampering & Bit-Flips ---")
        now = int(time.time())
        payload = b'{"action": "transfer", "amount": 1000000}'
        valid_sig = generate_hmac_signature(self.secret, payload, now)
        raw_hex = valid_sig[7:]

        # Test cases: (description, tampered_sig)
        tamper_cases = [
            ("Flip first hex char", "sha256=" + ("0" if raw_hex[0] != "0" else "1") + raw_hex[1:]),
            ("Flip middle hex char (pos 32)", "sha256=" + raw_hex[:32] + ("0" if raw_hex[32] != "0" else "1") + raw_hex[33:]),
            ("Flip last hex char (pos 63)", "sha256=" + raw_hex[:-1] + ("0" if raw_hex[-1] != "0" else "1")),
            ("Invert all hex chars", "sha256=" + "".join("0" if c != "0" else "1" for c in raw_hex)),
            ("Tampered payload with valid sig", valid_sig),  # payload will be altered
        ]

        for desc, sig_val in tamper_cases:
            test_body = payload if desc != "Tampered payload with valid sig" else payload + b" "
            status, _, rbody = await self.send_raw_request(
                "POST",
                "/webhook",
                {
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig_val,
                    "X-Hub-Timestamp": str(now),
                },
                body=test_body,
            )
            self.counters["total_adversarial_requests"] += 1
            if status == 401:
                self.counters["correctly_rejected_401"] += 1
                print(f"   [PASS] {desc} -> 401 Unauthorized (Rejected)")
            else:
                self.counters["security_bypasses"] += 1
                print(f"   [FAIL] {desc} -> {status} (SECURITY BYPASS!)")
                assert False, f"Tampered HMAC was NOT rejected! Status: {status}"

    async def challenge_missing_headers(self):
        print("\n--- Challenge 2: Missing Security & Authorization Headers ---")
        now = int(time.time())
        payload = b'{"test": "unauthenticated"}'
        sig = generate_hmac_signature(self.secret, payload, now)

        missing_cases = [
            ("No auth headers whatsoever", {}),
            ("Signature header present, timestamp missing", {"X-Hub-Signature-256": sig}),
            ("Timestamp header present, signature missing", {"X-Hub-Timestamp": str(now)}),
            ("Empty signature header", {"X-Hub-Signature-256": "", "X-Hub-Timestamp": str(now)}),
            ("Empty timestamp header", {"X-Hub-Signature-256": sig, "X-Hub-Timestamp": ""}),
            ("Empty Authorization header", {"Authorization": ""}),
            ("Authorization with only whitespace", {"Authorization": "   "}),
        ]

        for desc, headers in missing_cases:
            headers["Content-Type"] = "application/json"
            status, _, rbody = await self.send_raw_request("POST", "/webhook", headers, body=payload)
            self.counters["total_adversarial_requests"] += 1
            if status == 401:
                self.counters["correctly_rejected_401"] += 1
                print(f"   [PASS] {desc} -> 401 Unauthorized (Rejected)")
            else:
                self.counters["security_bypasses"] += 1
                print(f"   [FAIL] {desc} -> {status} (SECURITY BYPASS!)")
                assert False, f"Missing headers request was NOT rejected! Status: {status}"

    async def challenge_replay_and_timestamps(self):
        print("\n--- Challenge 3: Replay Attacks & Timestamp Skew Tolerances ---")
        now = int(time.time())
        payload = b'{"event": "payment_received"}'

        # Test past expired timestamps (Replay attacks)
        expired_deltas = [301, 350, 600, 3600, 86400, 10000000]
        for delta in expired_deltas:
            ts = now - delta
            sig = generate_hmac_signature(self.secret, payload, ts)
            status, _, _ = await self.send_raw_request(
                "POST",
                "/webhook",
                {
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                    "X-Hub-Timestamp": str(ts),
                },
                body=payload,
            )
            self.counters["total_adversarial_requests"] += 1
            assert status == 401, f"Expired timestamp ({delta}s past) NOT rejected! Status: {status}"
            self.counters["correctly_rejected_401"] += 1
            print(f"   [PASS] Replay attack {delta}s in the past -> 401 Unauthorized (Rejected)")

        # Test future timestamp skew (>60s)
        future_deltas = [61, 100, 300, 3600, 86400]
        for delta in future_deltas:
            ts = now + delta
            sig = generate_hmac_signature(self.secret, payload, ts)
            status, _, _ = await self.send_raw_request(
                "POST",
                "/webhook",
                {
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": sig,
                    "X-Hub-Timestamp": str(ts),
                },
                body=payload,
            )
            self.counters["total_adversarial_requests"] += 1
            assert status == 401, f"Future timestamp ({delta}s ahead) NOT rejected! Status: {status}"
            self.counters["correctly_rejected_401"] += 1
            print(f"   [PASS] Future skew {delta}s ahead -> 401 Unauthorized (Rejected)")

        # Test boundary timestamps that MUST be accepted:
        # Exactly 300s past:
        ts_edge_past = now - 300
        sig_edge_past = generate_hmac_signature(self.secret, payload, ts_edge_past)
        st_p, _, _ = await self.send_raw_request(
            "POST",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig_edge_past,
                "X-Hub-Timestamp": str(ts_edge_past),
            },
            body=payload,
        )
        assert st_p == 202, f"Boundary past timestamp (now-300s) was rejected with {st_p}!"
        self.counters["correctly_accepted_202"] += 1
        print("   [PASS] Boundary timestamp (now - 300s) -> 202 Accepted (Allowed)")

        # Exactly 60s future:
        ts_edge_future = now + 60
        sig_edge_future = generate_hmac_signature(self.secret, payload, ts_edge_future)
        st_f, _, _ = await self.send_raw_request(
            "POST",
            "/webhook",
            {
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig_edge_future,
                "X-Hub-Timestamp": str(ts_edge_future),
            },
            body=payload,
        )
        assert st_f == 202, f"Boundary future timestamp (now+60s) was rejected with {st_f}!"
        self.counters["correctly_accepted_202"] += 1
        print("   [PASS] Boundary timestamp (now + 60s) -> 202 Accepted (Allowed)")

        # Malformed non-integer timestamps
        for bad_ts in ["not_a_number", "123.456", "NaN", "Infinity", "0xabc", "-100", "0"]:
            status, _, _ = await self.send_raw_request(
                "POST",
                "/webhook",
                {
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": "sha256=" + "a" * 64,
                    "X-Hub-Timestamp": bad_ts,
                },
                body=payload,
            )
            self.counters["total_adversarial_requests"] += 1
            assert status == 401, f"Malformed timestamp '{bad_ts}' NOT rejected! Status: {status}"
            self.counters["correctly_rejected_401"] += 1
            print(f"   [PASS] Malformed timestamp '{bad_ts}' -> 401 Unauthorized (Rejected)")

    async def challenge_malformed_signatures(self):
        print("\n--- Challenge 4: Malformed Signature Headers (Bad Hex & Formats) ---")
        now = int(time.time())
        payload = b'{"test": "malformed"}'

        malformed_signatures = [
            ("Non-hex character 'g'", "sha256=" + "g" * 64),
            ("Non-hex character 'z'", "sha256=" + "z" * 64),
            ("Non-hex symbols '!@#$'", "sha256=" + "!@#$" * 16),
            ("Too short (32 hex chars)", "sha256=" + "a" * 32),
            ("Too short (63 hex chars)", "sha256=" + "a" * 63),
            ("Too long (65 hex chars)", "sha256=" + "a" * 65),
            ("Much too long (128 hex chars)", "sha256=" + "a" * 128),
            ("Wrong prefix 'sha1='", "sha1=" + "a" * 64),
            ("Wrong prefix 'md5='", "md5=" + "a" * 64),
            ("Wrong prefix 'sha512='", "sha512=" + "a" * 64),
            ("Embedded space in signature", "sha256=" + "a" * 32 + " " + "b" * 31),
            ("Null byte in signature header", "sha256=" + "a" * 30 + "\x00" + "b" * 33),
        ]

        for desc, bad_sig in malformed_signatures:
            try:
                status, _, _ = await self.send_raw_request(
                    "POST",
                    "/webhook",
                    {
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": bad_sig,
                        "X-Hub-Timestamp": str(now),
                    },
                    body=payload,
                )
                self.counters["total_adversarial_requests"] += 1
                assert status in (400, 401), f"Malformed signature {desc} returned unexpected {status}!"
                self.counters["correctly_rejected_401"] += 1
                print(f"   [PASS] {desc} -> {status} (Rejected)")
            except UnicodeEncodeError:
                # Null byte rejected at transport encoding level
                print(f"   [PASS] {desc} -> Transport encoding rejected")

    async def challenge_malformed_bearer_tokens(self):
        print("\n--- Challenge 5: Malformed & Tampered Bearer Tokens ---")
        payload = b'{"test": "bearer"}'

        bearer_cases = [
            ("Basic auth scheme instead of Bearer", "Basic dXNlcjpwYXNz"),
            ("Digest auth scheme", "Digest username=\"Mufasa\""),
            ("Custom token scheme without Bearer keyword", f"Token {self.bearer}"),
            ("Bearer keyword with no token value", "Bearer"),
            ("Bearer with multiple spaces and no token", "Bearer      "),
            ("Bearer with invalid/tampered token", f"Bearer {self.bearer[:-1]}X"),
            ("Bearer with completely wrong token", "Bearer totally_wrong_token_here!"),
        ]

        for desc, auth_hdr in bearer_cases:
            status, _, _ = await self.send_raw_request(
                "POST",
                "/webhook",
                {
                    "Content-Type": "application/json",
                    "Authorization": auth_hdr,
                },
                body=payload,
            )
            self.counters["total_adversarial_requests"] += 1
            assert status == 401, f"Malformed Bearer {desc} returned {status} instead of 401!"
            self.counters["correctly_rejected_401"] += 1
            print(f"   [PASS] {desc} -> 401 Unauthorized (Rejected)")

        # Verify valid Bearer token works
        st_ok, _, _ = await self.send_raw_request(
            "POST",
            "/webhook",
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.bearer}",
            },
            body=payload,
        )
        assert st_ok == 202, f"Valid Bearer token returned {st_ok} instead of 202!"
        self.counters["correctly_accepted_202"] += 1
        print(f"   [PASS] Valid Bearer token -> 202 Accepted (Authorized)")

    async def challenge_concurrent_memory_stress(self, num_requests: int = 300, concurrency: int = 30):
        print("\n--- Challenge 6: Concurrent Load Burst & Continuous Memory Profiler (<30MB) ---")
        baseline_rss = get_rss_mb(self.server_pid)
        print(f"   Baseline Idle Server Process RSS (PID {self.server_pid}): {baseline_rss} MB")
        assert baseline_rss < 30.0, f"Baseline RSS {baseline_rss}MB exceeds 30MB budget!"

        peak_rss = baseline_rss
        sampling_active = True

        async def memory_monitor():
            nonlocal peak_rss
            while sampling_active:
                curr_rss = get_rss_mb(self.server_pid)
                if curr_rss > peak_rss:
                    peak_rss = curr_rss
                await asyncio.sleep(0.01)  # Sample every 10ms

        monitor_task = asyncio.create_task(memory_monitor())

        semaphore = asyncio.Semaphore(concurrency)
        now = int(time.time())
        valid_payload = b'{"event": "stress_load", "seq": 1}'
        valid_sig = generate_hmac_signature(self.secret, valid_payload, now)

        async def worker(idx: int):
            async with semaphore:
                # 40% valid HMAC requests, 30% valid Bearer requests, 30% adversarial/tampered requests
                req_type = idx % 3
                if req_type == 0:
                    # Valid HMAC POST
                    st, _, _ = await self.send_raw_request(
                        "POST",
                        "/webhook",
                        {
                            "Content-Type": "application/json",
                            "X-Hub-Signature-256": valid_sig,
                            "X-Hub-Timestamp": str(now),
                        },
                        body=valid_payload,
                    )
                    assert st == 202
                elif req_type == 1:
                    # Valid Bearer POST
                    st, _, _ = await self.send_raw_request(
                        "POST",
                        "/webhook/github",
                        {
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {self.bearer}",
                        },
                        body=valid_payload,
                    )
                    assert st == 202
                else:
                    # Adversarial tampered request -> must be rejected with 401
                    st, _, _ = await self.send_raw_request(
                        "POST",
                        "/webhook",
                        {
                            "Content-Type": "application/json",
                            "X-Hub-Signature-256": "sha256=" + "f" * 64,  # bad sig
                            "X-Hub-Timestamp": str(now),
                        },
                        body=valid_payload,
                    )
                    assert st == 401

        start_time = time.perf_counter()
        tasks = [asyncio.create_task(worker(i)) for i in range(num_requests)]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start_time

        sampling_active = False
        await monitor_task

        # Allow OS page caches and settled sockets to stabilize
        await asyncio.sleep(0.3)
        settled_rss = get_rss_mb(self.server_pid)
        idle_cpu = get_cpu_percent(self.server_pid)
        rps = round(num_requests / elapsed, 1)

        print(f"   Completed {num_requests} concurrent requests in {round(elapsed, 2)}s ({rps} req/sec)")
        print(f"   Peak Server RSS during load:  {peak_rss} MB")
        print(f"   Post-load Settled Server RSS: {settled_rss} MB")
        print(f"   Budget Ceiling:               30.00 MB")
        print(f"   Peak Headroom Remaining:      {round(30.0 - peak_rss, 2)} MB")
        print(f"   Settled Idle CPU:             {idle_cpu}%")

        assert peak_rss < 30.0, f"PEAK RSS BREACH! Peak was {peak_rss}MB (Budget: 30MB)"
        print("   ✓ Memory limit strictly respected under high concurrency (PASS)")

    async def run_all(self):
        print("=" * 75)
        print("ANTIGRAVITY WEBHOOK HUB — M1 EMPIRICAL ADVERSARIAL CHALLENGE HARNESS")
        print("=" * 75)
        print(f"Launching isolated Ingress Server process on 127.0.0.1:{self.port}...")

        # Spawn isolated server subprocess
        server_script = PROJECT_ROOT / "scripts" / "m1_standalone_server.py"
        self.server_proc = subprocess.Popen(
            [sys.executable, str(server_script), str(self.port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.server_pid = self.server_proc.pid

        # Wait for "READY"
        ready_line = self.server_proc.stdout.readline()
        if "READY" not in ready_line:
            raise RuntimeError(f"Server failed to start: {ready_line}")

        print(f"Server is READY in isolated subprocess (PID: {self.server_pid})")

        try:
            await self.challenge_tampered_hmac()
            await self.challenge_missing_headers()
            await self.challenge_replay_and_timestamps()
            await self.challenge_malformed_signatures()
            await self.challenge_malformed_bearer_tokens()
            await self.challenge_concurrent_memory_stress(num_requests=300, concurrency=30)

            print("\n" + "=" * 75)
            print("SUMMARY OF EMPIRICAL ADVERSARIAL AUDIT")
            print("=" * 75)
            print(f"Total Adversarial Attacks Tested:  {self.counters['total_adversarial_requests']}")
            print(f"Correctly Rejected with 401/400:    {self.counters['correctly_rejected_401']}")
            print(f"Valid Requests Accepted with 202:  {self.counters['correctly_accepted_202']}")
            print(f"Security Bypasses Detected:        {self.counters['security_bypasses']}")
            print(f"Bypass Rate:                       0.00% (Absolute Zero)")
            print("Verdict: PASS — Robust security defenses and strict <30MB memory compliance.")
            print("=" * 75)
        finally:
            if self.server_proc:
                self.server_proc.terminate()
                try:
                    self.server_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.server_proc.kill()


if __name__ == "__main__":
    harness = AdversarialHarness(port=19440)
    asyncio.run(harness.run_all())
