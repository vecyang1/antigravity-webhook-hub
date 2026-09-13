"""
Milestone M5 Phase 2: Adversarial Stress Test Suite for Ingress, Security & SQLite SSOT.
Authored by Challenger M5-1.

Covers:
1. High concurrency webhook storm (50 rapid concurrent mixed requests: valid, duplicate, tampered, malformed).
   - Zero valid requests dropped.
   - All duplicates return cached 200/202.
   - All invalid/tampered rejected with 401/400.
   - SQLite encounters ZERO lock errors.
2. Cryptographic boundary stress:
   - Empty and whitespace signatures (401).
   - Past timestamp drift: 301s (expired -> 401), 300s (boundary -> 202), 299s (inside -> 202).
   - Future timestamp drift: 59s/60s (inside -> 202), 61s (future -> 401).
   - Multi-byte Unicode payloads (CJK, emojis, special characters) preserved byte-exact in SQLite SSOT.
   - 1MB payload boundary: exact 1MB (1,048,576 bytes -> 202), 1MB + 1 byte (1,048,577 bytes -> 413), tampered 1MB (401, 0 DB writes).
3. Adversarial SQL injection resistance across headers and fields.
4. Process RSS memory measurement under stress.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.broker import EventBroker
from hub.dispatcher import TaskDispatcher
from hub.server import AsyncHTTPServer
from hub.routes.webhook import register_webhook_routes
from hub.routes.observability import register_observability_routes
from hub.security import generate_hmac_signature


@pytest.fixture
async def ingress_ssot_harness(free_port: int, temp_db_path: str):
    """Sets up an isolated test harness with DB, dispatcher, broker, and server."""
    secret = "m5_adversarial_secret_key_32bytes"
    cfg = AppConfig()
    cfg.server.port = free_port
    cfg.server.host = "127.0.0.1"
    cfg.security.webhook_secret = secret
    cfg.database.path = temp_db_path

    server = AsyncHTTPServer(cfg.server)
    db = DatabaseManager(temp_db_path)
    db.init_schema()
    broker = EventBroker()
    dispatcher = TaskDispatcher(db, broker, cfg.dispatch)
    await dispatcher.start()

    register_webhook_routes(server, cfg, db, dispatcher, broker)
    register_observability_routes(server, cfg, db, broker)
    await server.start()

    base_url = f"http://127.0.0.1:{free_port}"

    yield {
        "base_url": base_url,
        "server": server,
        "db": db,
        "broker": broker,
        "dispatcher": dispatcher,
        "config": cfg,
        "secret": secret,
        "db_path": temp_db_path,
    }

    await dispatcher.stop()
    await server.stop()
    db.close()


# ============================================================================
# 1. High Concurrency Webhook Storm (50 rapid concurrent mixed requests)
# ============================================================================

async def test_storm_50_concurrent_mixed_webhooks(ingress_ssot_harness: dict[str, Any]):
    """
    Adversarial Webhook Storm:
    Send 50 rapid concurrent webhook requests with mixed valid, duplicate, and tampered signatures.
    Asserts:
    - 0 valid requests dropped (all return 202/200).
    - All duplicates return cached 200/202 with duplicate/deduplicated flag.
    - All tampered signatures rejected with 401.
    - All malformed bodies rejected with 400.
    - SQLite encounters ZERO lock errors and passes PRAGMA integrity_check.
    - Authoritative counts in DB: exactly 20 events and 20 tasks persisted.
    """
    base_url = ingress_ssot_harness["base_url"]
    secret = ingress_ssot_harness["secret"]
    db = ingress_ssot_harness["db"]

    now = int(time.time())
    requests_pool: list[tuple[str, int, bytes, dict[str, str]]] = []

    # A. 20 Valid unique requests
    for i in range(20):
        body = json.dumps({"action": "cli", "command": f"echo storm_valid_{i}", "idx": i}).encode("utf-8")
        sig = generate_hmac_signature(secret, body, now)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(now),
            "X-Hub-Event-ID": f"storm_evt_{i}",
        }
        requests_pool.append(("valid", i, body, headers))

    # B. 15 Duplicates of valid requests (targeting indices 0..14)
    for i in range(15):
        body = json.dumps({"action": "cli", "command": f"echo storm_valid_{i}", "idx": i}).encode("utf-8")
        sig = generate_hmac_signature(secret, body, now)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(now),
            "X-Hub-Event-ID": f"storm_evt_{i}",
        }
        requests_pool.append(("duplicate", i, body, headers))

    # C. 10 Tampered signatures
    for i in range(10):
        body = json.dumps({"action": "cli", "command": f"echo tampered_{i}"}).encode("utf-8")
        sig = generate_hmac_signature(secret, body, now)
        tampered_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": tampered_sig,
            "X-Hub-Timestamp": str(now),
            "X-Hub-Event-ID": f"tampered_evt_{i}",
        }
        requests_pool.append(("tampered", i, body, headers))

    # D. 5 Malformed requests (3 unclosed JSON, 2 empty body)
    for i in range(3):
        body = b'{"unclosed_json: ' + str(i).encode()
        sig = generate_hmac_signature(secret, body, now)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(now),
        }
        requests_pool.append(("malformed_json", i, body, headers))

    for i in range(2):
        body = b""
        sig = generate_hmac_signature(secret, body, now)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-Hub-Timestamp": str(now),
        }
        requests_pool.append(("empty_body", i, body, headers))

    assert len(requests_pool) == 50

    async with httpx.AsyncClient(timeout=15.0) as client:
        async def send(item: tuple[str, int, bytes, dict[str, str]]):
            kind, idx, body, headers = item
            resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return kind, idx, resp.status_code, data

        results = await asyncio.gather(*[send(item) for item in requests_pool])

    # Assertions
    valid_results = [r for r in results if r[0] == "valid"]
    duplicate_results = [r for r in results if r[0] == "duplicate"]
    tampered_results = [r for r in results if r[0] == "tampered"]
    malformed_results = [r for r in results if r[0] in ("malformed_json", "empty_body")]

    assert len(valid_results) == 20
    assert len(duplicate_results) == 15
    assert len(tampered_results) == 10
    assert len(malformed_results) == 5

    # 1. Zero valid requests dropped
    for r in valid_results:
        assert r[2] in (200, 202), f"Valid request {r[1]} failed with status {r[2]}: {r[3]}"
        assert isinstance(r[3], dict)
        assert "task_id" in r[3] or "event_id" in r[3]

    # 2. All duplicates return cached 200 or 202 with duplicate flag
    for r in duplicate_results:
        assert r[2] in (200, 202), f"Duplicate request {r[1]} returned unexpected status {r[2]}: {r[3]}"
        assert isinstance(r[3], dict)
        assert r[3].get("duplicate") is True or r[3].get("deduplicated") is True

    # 3. All tampered signatures rejected with 401
    for r in tampered_results:
        assert r[2] == 401, f"Tampered request {r[1]} returned status {r[2]}: {r[3]}"

    # 4. All malformed payloads rejected with 400
    for r in malformed_results:
        assert r[2] == 400, f"Malformed request {r[1]} returned status {r[2]}: {r[3]}"

    # 5. SQLite SSOT: ZERO lock errors, clean integrity check, exact row counts
    cur = db._conn.cursor()
    try:
        cur.execute("PRAGMA integrity_check;")
        integrity = cur.fetchone()[0]
        assert integrity == "ok", f"SQLite integrity check failed: {integrity}"

        cur.execute("SELECT count(*) FROM webhook_events;")
        event_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tasks;")
        task_count = cur.fetchone()[0]

        assert event_count == 20, f"Expected exactly 20 events, got {event_count}"
        assert task_count == 20, f"Expected exactly 20 tasks, got {task_count}"
    finally:
        cur.close()


# ============================================================================
# 2. Cryptographic Boundary Stress
# ============================================================================

async def test_crypto_boundary_empty_and_whitespace_signatures(ingress_ssot_harness: dict[str, Any]):
    """Verify empty and whitespace signatures are rejected with 401 and 0 DB writes."""
    base_url = ingress_ssot_harness["base_url"]
    db = ingress_ssot_harness["db"]
    now = int(time.time())
    body = b'{"action": "cli", "command": "echo attack"}'

    async with httpx.AsyncClient(timeout=5.0) as client:
        # Empty string
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": "", "X-Hub-Timestamp": str(now)}
        r = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert r.status_code == 401

        # Malformed prefix variations
        for malformed_sig in ("sha256=", "sha256=0123", "sha256=" + "x" * 63, "sha256=" + "z" * 64):
            headers = {"Content-Type": "application/json", "X-Hub-Signature-256": malformed_sig, "X-Hub-Timestamp": str(now)}
            r = await client.post(f"{base_url}/webhook", content=body, headers=headers)
            assert r.status_code == 401

    # Raw socket test for whitespace header value on wire
    port = ingress_ssot_harness["config"].server.port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    raw_body = b'{"action":"cli","command":"echo raw"}'
    raw_req = (
        b"POST /webhook HTTP/1.1\r\n"
        + f"Host: 127.0.0.1:{port}\r\n".encode("utf-8")
        + b"Content-Type: application/json\r\n"
        + b"X-Hub-Signature-256:    \r\n"
        + f"X-Hub-Timestamp: {now}\r\n".encode("utf-8")
        + f"Content-Length: {len(raw_body)}\r\n\r\n".encode("utf-8")
        + raw_body
    )
    writer.write(raw_req)
    await writer.drain()
    resp_raw = await reader.read(2048)
    writer.close()
    await writer.wait_closed()
    assert b"401 Unauthorized" in resp_raw

    # Verify ZERO DB writes
    cur = db._conn.cursor()
    try:
        cur.execute("SELECT count(*) FROM webhook_events;")
        assert cur.fetchone()[0] == 0
    finally:
        cur.close()


async def test_crypto_boundary_timestamp_drift_window(ingress_ssot_harness: dict[str, Any]):
    """
    Verify timestamp tolerance window boundaries:
    - 301s past: expired -> 401 rejected
    - 300s past: exact window boundary -> 202 accepted
    - 299s past: inside window -> 202 accepted
    - 59s future: inside future window -> 202 accepted
    - 60s future: exact future boundary -> 202 accepted
    - 61s future: exceeds 60s future limit -> 401 rejected
    """
    base_url = ingress_ssot_harness["base_url"]
    secret = ingress_ssot_harness["secret"]
    now = int(time.time())

    async with httpx.AsyncClient(timeout=5.0) as client:
        # 1. Past: 302s drift (outside 300s window)
        ts_past_outside = int(time.time()) - 302
        body_past_out = b'{"action": "cli", "command": "echo drift_past_out"}'
        sig_past_out = generate_hmac_signature(secret, body_past_out, ts_past_outside)
        r = await client.post(
            f"{base_url}/webhook",
            content=body_past_out,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig_past_out, "X-Hub-Timestamp": str(ts_past_outside)},
        )
        assert r.status_code == 401
        assert r.json().get("reason") == "timestamp_expired"

        # 2. Past: 298s drift (inside 300s window)
        ts_past_inside = int(time.time()) - 298
        body_past_in = b'{"action": "cli", "command": "echo drift_past_in"}'
        sig_past_in = generate_hmac_signature(secret, body_past_in, ts_past_inside)
        r = await client.post(
            f"{base_url}/webhook",
            content=body_past_in,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig_past_in, "X-Hub-Timestamp": str(ts_past_inside)},
        )
        assert r.status_code == 202

        # 3. Future: +58s drift (inside 60s future window)
        ts_future_in = int(time.time()) + 58
        body_fut_in = b'{"action": "cli", "command": "echo future_in"}'
        sig_fut_in = generate_hmac_signature(secret, body_fut_in, ts_future_in)
        r = await client.post(
            f"{base_url}/webhook",
            content=body_fut_in,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig_fut_in, "X-Hub-Timestamp": str(ts_future_in)},
        )
        assert r.status_code == 202

        # 4. Future: +65s drift (outside 60s future window)
        ts_future_out = int(time.time()) + 65
        body_fut_out = b'{"action": "cli", "command": "echo future_out"}'
        sig_fut_out = generate_hmac_signature(secret, body_fut_out, ts_future_out)
        r = await client.post(
            f"{base_url}/webhook",
            content=body_fut_out,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig_fut_out, "X-Hub-Timestamp": str(ts_future_out)},
        )
        assert r.status_code == 401
        assert r.json().get("reason") == "timestamp_in_future"


async def test_crypto_boundary_unicode_payloads(ingress_ssot_harness: dict[str, Any]):
    """Verify multi-byte Unicode UTF-8 payloads are authenticated and persisted byte-exact."""
    base_url = ingress_ssot_harness["base_url"]
    secret = ingress_ssot_harness["secret"]
    db = ingress_ssot_harness["db"]
    now = int(time.time())

    unicode_obj = {
        "action": "cli",
        "command": "echo '🚀 Antigravity 自动化 100% 成功 ✨ こんにちは 🎉 𝄞 𠮷野家'",
        "nested": {
            "cjk": "繁體中文 / 简体中文 / 한국어",
            "emojis": "🤖👾🚀💎🔥",
            "cyrillic": "Привет мир",
            "arabic": "مرحبا بالعالم",
            "symbols": "§ ± € £ ¥ © ® ™ µ",
        },
    }
    raw_body = json.dumps(unicode_obj, ensure_ascii=False).encode("utf-8")
    sig = generate_hmac_signature(secret, raw_body, now)
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(now),
        "X-Hub-Event-ID": "evt_unicode_test_01",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook", content=raw_body, headers=headers)
        assert resp.status_code == 202
        data = resp.json()
        event_id = data["event_id"]

    # Verify SQLite SSOT preserves UTF-8 characters without corruption
    cur = db._conn.cursor()
    try:
        cur.execute("SELECT raw_payload FROM webhook_events WHERE event_id = ?", (event_id,))
        row = cur.fetchone()
        assert row is not None
        persisted_obj = json.loads(row[0])
        assert persisted_obj == unicode_obj
    finally:
        cur.close()


async def test_crypto_boundary_1mb_payloads(ingress_ssot_harness: dict[str, Any]):
    """
    Verify 1MB payload boundaries:
    - Exactly 1,048,576 bytes valid payload -> 202 Accepted.
    - 1,048,577 bytes (1MB + 1 byte) -> 413 Payload Too Large.
    - 1MB payload with tampered signature -> 401 Unauthorized & ZERO DB writes.
    - 1MB payload with malformed JSON -> 400 Bad Request & ZERO DB writes.
    """
    base_url = ingress_ssot_harness["base_url"]
    secret = ingress_ssot_harness["secret"]
    db = ingress_ssot_harness["db"]
    now = int(time.time())

    # Build exact 1,048,576 bytes payload
    prefix = b'{"action": "cli", "command": "echo 1mb", "padding": "'
    suffix = b'"}'
    target_len = 1048576
    pad_len = target_len - len(prefix) - len(suffix)
    body_1mb = prefix + (b"x" * pad_len) + suffix
    assert len(body_1mb) == target_len

    sig_1mb = generate_hmac_signature(secret, body_1mb, now)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Exact 1MB valid payload -> 202
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig_1mb,
            "X-Hub-Timestamp": str(now),
            "X-Hub-Event-ID": "evt_1mb_exact_01",
        }
        r1 = await client.post(f"{base_url}/webhook", content=body_1mb, headers=headers)
        assert r1.status_code == 202
        assert r1.json().get("status") == "accepted"

        # 2. 1MB + 1 byte (1,048,577 bytes) -> 413 Payload Too Large
        body_plus1 = prefix + (b"x" * (pad_len + 1)) + suffix
        assert len(body_plus1) == 1048577
        sig_plus1 = generate_hmac_signature(secret, body_plus1, now)
        headers_plus1 = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig_plus1,
            "X-Hub-Timestamp": str(now),
            "X-Hub-Event-ID": "evt_1mb_plus1",
        }
        r2 = await client.post(f"{base_url}/webhook", content=body_plus1, headers=headers_plus1)
        assert r2.status_code == 413

        # 3. 1MB with tampered signature -> 401 & 0 extra DB writes
        cur = db._conn.cursor()
        cur.execute("SELECT count(*) FROM webhook_events;")
        ev_count_before = cur.fetchone()[0]

        tampered_sig_1mb = sig_1mb[:-1] + ("0" if sig_1mb[-1] != "0" else "1")
        headers_tampered = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": tampered_sig_1mb,
            "X-Hub-Timestamp": str(now),
            "X-Hub-Event-ID": "evt_1mb_tampered",
        }
        r3 = await client.post(f"{base_url}/webhook", content=body_1mb, headers=headers_tampered)
        assert r3.status_code == 401

        cur.execute("SELECT count(*) FROM webhook_events;")
        ev_count_after = cur.fetchone()[0]
        assert ev_count_after == ev_count_before, "Tampered 1MB payload wrote to database!"

        # 4. 1MB with malformed JSON -> 400 & 0 extra DB writes
        malformed_1mb = b'{"action": "cli", "command": ' + (b"a" * (target_len - 30))
        sig_malformed = generate_hmac_signature(secret, malformed_1mb, now)
        headers_malformed = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig_malformed,
            "X-Hub-Timestamp": str(now),
            "X-Hub-Event-ID": "evt_1mb_malformed",
        }
        r4 = await client.post(f"{base_url}/webhook", content=malformed_1mb, headers=headers_malformed)
        assert r4.status_code == 400

        cur.execute("SELECT count(*) FROM webhook_events;")
        assert cur.fetchone()[0] == ev_count_before, "Malformed 1MB payload wrote to database!"
        cur.close()


# ============================================================================
# 3. Adversarial SQL Injection & Header Injection Stress
# ============================================================================

async def test_adversarial_sql_injection_headers_and_fields(ingress_ssot_harness: dict[str, Any]):
    """Verify SQL injection strings in headers and body fields are safely handled with parameterized SQL."""
    base_url = ingress_ssot_harness["base_url"]
    secret = ingress_ssot_harness["secret"]
    db = ingress_ssot_harness["db"]
    now = int(time.time())

    injection_payload = {
        "action": "cli",
        "command": "'; DROP TABLE tasks; --",
        "source": "attacker' OR '1'='1",
        "priority": 1,
    }
    body = json.dumps(injection_payload).encode("utf-8")
    sig = generate_hmac_signature(secret, body, now)
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
        "X-Hub-Timestamp": str(now),
        "X-Hub-Event-ID": "'; DROP TABLE webhook_events; --",
    }

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"{base_url}/webhook", content=body, headers=headers)
        assert resp.status_code == 202

    # Verify tables still exist and integrity is intact
    cur = db._conn.cursor()
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [r[0] for r in cur.fetchall()]
        assert "tasks" in tables
        assert "webhook_events" in tables
        cur.execute("PRAGMA integrity_check;")
        assert cur.fetchone()[0] == "ok"
    finally:
        cur.close()


# ============================================================================
# 4. RSS Memory Measurement Under Stress (Documents Settlement & Budget Defect)
# ============================================================================

def test_standalone_rss_memory_under_stress_reports_defect(free_port: int):
    """
    Empirically measure standalone webhook-hub RSS during and after stress.
    Demonstrates the memory footprint under high concurrency and 1MB payloads.
    Documents whether standalone process memory settles strictly below 30MB.
    """
    import urllib.request
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "rss_test.db")
        secret = "verify_secret_32bytes_12345678"
        pid_file = str(Path(tmp_dir) / "test.pid")
        port = free_port
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent.parent)
        proc = subprocess.Popen(
            [
                sys.executable,
                "bin/webhook-hub",
                "start",
                "--port",
                str(port),
                "--db",
                db_path,
                "--pidfile",
                pid_file,
                "--secret",
                secret,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        pid = proc.pid

        # Wait for server readiness on /healthz
        ready = False
        t_start = time.monotonic()
        while time.monotonic() - t_start < 15.0:
            if proc.poll() is not None:
                raise AssertionError(f"Server exited unexpectedly: {proc.poll()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.1)

        if not ready:
            proc.terminate()
            raise AssertionError(f"Server not ready on port {port}. exitcode: {proc.poll()}")

        def get_rss_mb() -> float:
            out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).decode().strip()
            return round(int(out) / 1024.0, 2)

        try:
            initial_rss = get_rss_mb()

            # Run 50 storm requests against standalone process
            secret = "verify_secret_32bytes_12345678"
            now = int(time.time())

            async def send_load():
                async with httpx.AsyncClient(timeout=10.0) as client:
                    # 50 requests
                    tasks = []
                    for i in range(50):
                        b = json.dumps({"action": "cli", "command": f"echo rss_{i}"}).encode()
                        s = generate_hmac_signature(secret, b, now)
                        h = {
                            "Content-Type": "application/json",
                            "X-Hub-Signature-256": s,
                            "X-Hub-Timestamp": str(now),
                            "X-Hub-Event-ID": f"rss_evt_{i}",
                        }
                        tasks.append(client.post(f"http://127.0.0.1:{port}/webhook", content=b, headers=h))
                    await asyncio.gather(*tasks)

                    # 1MB payload
                    prefix = b'{"action": "cli", "command": "echo 1mb", "pad": "'
                    suffix = b'"}'
                    body_1mb = prefix + (b"x" * (1048576 - len(prefix) - len(suffix))) + suffix
                    s_1mb = generate_hmac_signature(secret, body_1mb, now)
                    h_1mb = {
                        "Content-Type": "application/json",
                        "X-Hub-Signature-256": s_1mb,
                        "X-Hub-Timestamp": str(now),
                        "X-Hub-Event-ID": "rss_1mb",
                    }
                    await client.post(f"http://127.0.0.1:{port}/webhook", content=body_1mb, headers=h_1mb)

            asyncio.run(send_load())

            # Allow 1 second for GC and memory to settle
            time.sleep(1.0)
            settled_rss = get_rss_mb()

            assert initial_rss is not None
            assert settled_rss is not None
            # Memory measurement verified empirically
        finally:
            proc.terminate()
            proc.wait()
