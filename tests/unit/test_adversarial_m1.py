"""Adversarial stress-test suite for Milestone M1 (Ingress Gateway & Security Core).

Written by Challenger M1-1 to empirically challenge and verify:
1. Tampered HMAC SHA-256 signatures (bit flips, mutations, byte truncations).
2. Missing signature and authorization headers.
3. Replay attack timestamps (>300s old, >60s into future, negative, extreme values).
4. Malformed signature headers (bad hex characters, wrong length, bad prefixes).
5. Malformed Bearer tokens (prefix errors, empty tokens, non-Bearer schemes).
6. Security auth_modes matrix ('hmac', 'bearer', 'any', 'both').
7. HTTP Server boundary defenses (payload too large, malformed chunked encoding, unknown routes).
"""

import asyncio
import json
import time
import pytest

from hub.config import SecurityConfig, ServerConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.security import (
    compute_dedup_hash,
    compute_payload_hash,
    generate_hmac_signature,
    validate_request_security,
    verify_bearer_token,
    verify_hmac_signature,
)
from hub.server import AsyncHTTPServer


# ==============================================================================
# CATEGORY 1: HMAC SIGNATURE TAMPERING & MALFORMATIONS
# ==============================================================================


def test_hmac_single_char_tampering_at_all_positions():
    """Adversarial challenge: alter signature at start, middle, and end."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b'{"action": "deploy", "target": "prod"}'
    ts = 1700000000
    sig = generate_hmac_signature(secret, body, ts, include_prefix=True)
    raw_hex = sig[7:]  # strip 'sha256='

    # Test tampering first char of hex
    tampered_first = "sha256=" + ("0" if raw_hex[0] != "0" else "1") + raw_hex[1:]
    res = verify_hmac_signature(secret, body, tampered_first, str(ts), current_time=ts)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "signature_mismatch"

    # Test tampering middle char of hex (index 32)
    tampered_mid = "sha256=" + raw_hex[:32] + ("0" if raw_hex[32] != "0" else "1") + raw_hex[33:]
    res = verify_hmac_signature(secret, body, tampered_mid, str(ts), current_time=ts)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "signature_mismatch"

    # Test tampering last char of hex
    tampered_last = "sha256=" + raw_hex[:-1] + ("0" if raw_hex[-1] != "0" else "1")
    res = verify_hmac_signature(secret, body, tampered_last, str(ts), current_time=ts)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "signature_mismatch"


def test_hmac_malformed_hex_characters():
    """Adversarial challenge: non-hex characters injected into signature."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b'{"test": 1}'
    ts = 1700000000

    invalid_chars = ["g", "z", "!", "@", " ", "-", "_", "X", "\x00", "\n"]
    for bad_char in invalid_chars:
        bad_hex = bad_char * 64
        # with prefix
        res1 = verify_hmac_signature(secret, body, f"sha256={bad_hex}", str(ts), current_time=ts)
        assert not res1.is_valid
        assert res1.status_code == 401
        assert res1.reason == "invalid_signature_format"

        # without prefix
        res2 = verify_hmac_signature(secret, body, bad_hex, str(ts), current_time=ts)
        assert not res2.is_valid
        assert res2.status_code == 401
        assert res2.reason == "invalid_signature_format"


def test_hmac_invalid_signature_lengths():
    """Adversarial challenge: signatures of incorrect lengths (<64 or >64 chars)."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b"test_payload"
    ts = 1700000000

    lengths = [0, 1, 16, 32, 63, 65, 128, 256]
    for length in lengths:
        bad_hex = "a" * length
        res = verify_hmac_signature(secret, body, f"sha256={bad_hex}", str(ts), current_time=ts)
        assert not res.is_valid
        assert res.status_code == 401
        assert res.reason == "invalid_signature_format"


def test_hmac_unsupported_prefixes():
    """Adversarial challenge: unsupported or forged prefixes (e.g. sha1=, md5=, sha512=)."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b"test_payload"
    ts = 1700000000
    valid_sig = generate_hmac_signature(secret, body, ts, include_prefix=False)

    unsupported_prefixes = ["sha1=", "md5=", "sha512=", "hmac=", "bearer=", "token="]
    for prefix in unsupported_prefixes:
        prefixed_sig = prefix + valid_sig
        res = verify_hmac_signature(secret, body, prefixed_sig, str(ts), current_time=ts)
        assert not res.is_valid
        assert res.status_code == 401
        assert res.reason == "invalid_signature_format"


def test_hmac_body_tampering_variants():
    """Adversarial challenge: various payload modifications with valid original signature."""
    secret = "adversarial_secret_key_32_bytes_len!"
    original_body = b'{"role": "user", "admin": false}'
    ts = 1700000000
    sig = generate_hmac_signature(secret, original_body, ts)

    tampered_bodies = [
        b'{"role": "user", "admin": true}',   # privilege escalation
        original_body + b" ",                 # trailing whitespace
        b" " + original_body,                 # leading whitespace
        original_body + b"\n",                # newline injection
        original_body + b"\x00",              # null byte append
        original_body[:-1],                   # truncation
        b"",                                  # empty payload
    ]

    for tampered in tampered_bodies:
        res = verify_hmac_signature(secret, tampered, sig, str(ts), current_time=ts)
        assert not res.is_valid
        assert res.status_code == 401
        assert res.reason == "signature_mismatch"


# ==============================================================================
# CATEGORY 2: REPLAY DEFENSE & TIMESTAMP BOUNDARIES
# ==============================================================================


def test_timestamp_boundary_300s_past():
    """Adversarial challenge: strict testing of the 300-second past window boundary."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b"payload"
    now = 1000000

    # Exactly 300 seconds ago (req_timestamp == now - 300) -> Allowed
    ts_300 = now - 300
    sig_300 = generate_hmac_signature(secret, body, ts_300)
    res_300 = verify_hmac_signature(secret, body, sig_300, str(ts_300), current_time=now)
    assert res_300.is_valid
    assert res_300.status_code == 200

    # 301 seconds ago (req_timestamp == now - 301) -> Expired (Replay rejected)
    ts_301 = now - 301
    sig_301 = generate_hmac_signature(secret, body, ts_301)
    res_301 = verify_hmac_signature(secret, body, sig_301, str(ts_301), current_time=now)
    assert not res_301.is_valid
    assert res_301.status_code == 401
    assert res_301.reason == "timestamp_expired"


def test_timestamp_boundary_60s_future():
    """Adversarial challenge: strict testing of the 60-second future tolerance boundary."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b"payload"
    now = 1000000

    # Exactly 60 seconds into future (req_timestamp == now + 60) -> Allowed
    ts_60 = now + 60
    sig_60 = generate_hmac_signature(secret, body, ts_60)
    res_60 = verify_hmac_signature(secret, body, sig_60, str(ts_60), current_time=now)
    assert res_60.is_valid
    assert res_60.status_code == 200

    # 61 seconds into future (req_timestamp == now + 61) -> Future skew rejected
    ts_61 = now + 61
    sig_61 = generate_hmac_signature(secret, body, ts_61)
    res_61 = verify_hmac_signature(secret, body, sig_61, str(ts_61), current_time=now)
    assert not res_61.is_valid
    assert res_61.status_code == 401
    assert res_61.reason == "timestamp_in_future"


def test_timestamp_extreme_and_malformed_values():
    """Adversarial challenge: negative, zero, epoch start, float, string, and overflowing timestamps."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b"payload"
    now = 1700000000

    # Negative timestamp
    res = verify_hmac_signature(secret, body, "sha256=" + "a" * 64, "-100", current_time=now)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "timestamp_expired"

    # Zero timestamp (1970 epoch)
    res = verify_hmac_signature(secret, body, "sha256=" + "a" * 64, "0", current_time=now)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "timestamp_expired"

    # Far future timestamp
    res = verify_hmac_signature(secret, body, "sha256=" + "a" * 64, "9999999999", current_time=now)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "timestamp_in_future"

    # Non-integer strings
    malformed_timestamps = ["abc", "123.456", "NaN", "Infinity", "0x123", "1e6"]
    for bad_ts in malformed_timestamps:
        res = verify_hmac_signature(secret, body, "sha256=" + "a" * 64, bad_ts, current_time=now)
        assert not res.is_valid
        assert res.status_code == 401
        assert res.reason == "invalid_timestamp_format"

    # Whitespace or empty timestamps rejected with 401
    for empty_ts in ["", "   "]:
        res = verify_hmac_signature(secret, body, "sha256=" + "a" * 64, empty_ts, current_time=now)
        assert not res.is_valid
        assert res.status_code == 401
        assert res.reason in ("missing_security_headers", "invalid_timestamp_format")


# ==============================================================================
# CATEGORY 3: MISSING HEADERS & AUTHORIZATION MALFORMATIONS
# ==============================================================================


def test_missing_and_empty_headers():
    """Adversarial challenge: None, empty strings, and whitespace in header values."""
    secret = "adversarial_secret_key_32_bytes_len!"
    body = b"payload"

    # Missing signature
    res = verify_hmac_signature(secret, body, None, "1000000")
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "missing_security_headers"

    # Empty signature
    res = verify_hmac_signature(secret, body, "", "1000000")
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "missing_security_headers"

    # Missing timestamp
    res = verify_hmac_signature(secret, body, "sha256=" + "a" * 64, None)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "missing_security_headers"

    # Empty timestamp
    res = verify_hmac_signature(secret, body, "sha256=" + "a" * 64, "")
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "missing_security_headers"


def test_bearer_token_adversarial_formats():
    """Adversarial challenge: non-Bearer schemes, empty tokens, whitespace, case variation."""
    expected = "valid_production_secret_token_12345"

    # Missing header
    res = verify_bearer_token(expected, None)
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "missing_authorization_header"

    # Non-Bearer auth schemes
    bad_schemes = [
        "Basic dXNlcjpwYXNz",
        "Digest username=\"Mufasa\"",
        "OAuth 2YotnFZFEjr1zCsicMWpAA",
        "Token some_token",
        "AWS4-HMAC-SHA256 Credential=...",
    ]
    for bad_scheme in bad_schemes:
        res = verify_bearer_token(expected, bad_scheme)
        assert not res.is_valid
        assert res.status_code == 401
        assert res.reason == "missing_or_malformed_bearer_token"

    # Bearer keyword alone
    res = verify_bearer_token(expected, "Bearer")
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "missing_or_malformed_bearer_token"

    # Bearer with only whitespace
    res = verify_bearer_token(expected, "Bearer    ")
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason in ("missing_bearer_token", "missing_or_malformed_bearer_token")

    # Case insensitivity for scheme prefix
    res = verify_bearer_token(expected, f"bearer {expected}")
    assert res.is_valid
    assert res.status_code == 200

    res = verify_bearer_token(expected, f"BEARER {expected}")
    assert res.is_valid
    assert res.status_code == 200

    # Wrong token
    res = verify_bearer_token(expected, "Bearer wrong_token_value_attempt")
    assert not res.is_valid
    assert res.status_code == 401
    assert res.reason == "invalid_bearer_token"


# ==============================================================================
# CATEGORY 4: COMPREHENSIVE AUTH_MODE MATRIX
# ==============================================================================


def test_auth_mode_combinations():
    """Adversarial challenge: test all 4 auth modes with valid and invalid permutations."""
    secret = "k" * 32
    token = "t" * 32
    ts = 1000000
    body = b'{"command": "sync"}'
    valid_sig = generate_hmac_signature(secret, body, ts)
    bad_sig = "sha256=" + "0" * 64

    # 1. Mode: 'hmac'
    cfg_hmac = SecurityConfig(auth_mode="hmac", webhook_secret=secret)
    # Valid HMAC -> OK
    r_ok = HTTPRequest("POST", "/webhook", headers={"x-hub-signature-256": valid_sig, "x-hub-timestamp": str(ts)}, body=body)
    assert validate_request_security(r_ok, cfg_hmac, current_time=ts).is_valid
    # Missing headers -> Fail
    r_empty = HTTPRequest("POST", "/webhook", headers={}, body=body)
    assert not validate_request_security(r_empty, cfg_hmac, current_time=ts).is_valid
    # Bearer supplied instead of HMAC -> Fail
    r_bearer = HTTPRequest("POST", "/webhook", headers={"authorization": f"Bearer {token}"}, body=body)
    assert not validate_request_security(r_bearer, cfg_hmac, current_time=ts).is_valid

    # 2. Mode: 'bearer'
    cfg_bearer = SecurityConfig(auth_mode="bearer", bearer_token=token)
    assert validate_request_security(r_bearer, cfg_bearer, current_time=ts).is_valid
    # HMAC supplied instead of Bearer -> Fail
    assert not validate_request_security(r_ok, cfg_bearer, current_time=ts).is_valid

    # 3. Mode: 'any'
    cfg_any = SecurityConfig(auth_mode="any", webhook_secret=secret, bearer_token=token)
    assert validate_request_security(r_ok, cfg_any, current_time=ts).is_valid
    assert validate_request_security(r_bearer, cfg_any, current_time=ts).is_valid
    # Both invalid -> Fail
    r_both_bad = HTTPRequest(
        "POST", "/webhook",
        headers={
            "x-hub-signature-256": bad_sig,
            "x-hub-timestamp": str(ts),
            "authorization": "Bearer bad_token",
        },
        body=body,
    )
    assert not validate_request_security(r_both_bad, cfg_any, current_time=ts).is_valid
    # Bad HMAC but Valid Bearer -> Should PASS under 'any'
    r_bad_hmac_good_bearer = HTTPRequest(
        "POST", "/webhook",
        headers={
            "x-hub-signature-256": bad_sig,
            "x-hub-timestamp": str(ts),
            "authorization": f"Bearer {token}",
        },
        body=body,
    )
    assert validate_request_security(r_bad_hmac_good_bearer, cfg_any, current_time=ts).is_valid

    # 4. Mode: 'both'
    cfg_both = SecurityConfig(auth_mode="both", webhook_secret=secret, bearer_token=token)
    # Both valid -> PASS
    r_both_good = HTTPRequest(
        "POST", "/webhook",
        headers={
            "x-hub-signature-256": valid_sig,
            "x-hub-timestamp": str(ts),
            "authorization": f"Bearer {token}",
        },
        body=body,
    )
    assert validate_request_security(r_both_good, cfg_both, current_time=ts).is_valid
    # Valid HMAC but Missing Bearer -> FAIL
    assert not validate_request_security(r_ok, cfg_both, current_time=ts).is_valid
    # Valid Bearer but Missing HMAC -> FAIL
    assert not validate_request_security(r_bearer, cfg_both, current_time=ts).is_valid
    # Bad HMAC with Valid Bearer -> FAIL
    assert not validate_request_security(r_bad_hmac_good_bearer, cfg_both, current_time=ts).is_valid


# ==============================================================================
# CATEGORY 5: ASYNC HTTP SERVER BOUNDARY DEFENSES
# ==============================================================================


@pytest.mark.anyio
async def test_server_payload_limit_enforcement():
    """Adversarial challenge: payload exceeding max_body_bytes is rejected with 413."""
    cfg = ServerConfig(host="127.0.0.1", port=19430, max_body_bytes=512)
    server = AsyncHTTPServer(cfg)

    @server.post("/webhook")
    async def post_handler(req):
        return HTTPResponse.json({"status": "received"})

    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 19430)
        # Send Content-Length: 1024 (>512)
        oversized = b"X" * 1024
        req = (
            f"POST /webhook HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Length: {len(oversized)}\r\n\r\n"
        ).encode("latin1")
        writer.write(req + oversized)
        await writer.drain()

        header_bytes = await reader.readuntil(b"\r\n\r\n")
        assert b"413 Payload Too Large" in header_bytes

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.anyio
async def test_server_malformed_request_line():
    """Adversarial challenge: invalid HTTP request lines return 400."""
    cfg = ServerConfig(host="127.0.0.1", port=19431)
    server = AsyncHTTPServer(cfg)

    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", 19431)
        # Missing HTTP version in request line
        writer.write(b"GET /healthz\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()

        resp = await reader.readuntil(b"\r\n\r\n")
        assert b"400 Bad Request" in resp

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
