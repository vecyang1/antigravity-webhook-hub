"""Unit tests for hub.security cryptographic authentication and validation."""

import time

from hub.config import SecurityConfig
from hub.models import HTTPRequest
from hub.security import (
    compute_dedup_hash,
    compute_payload_hash,
    generate_hmac_signature,
    validate_request_security,
    verify_bearer_token,
    verify_hmac_signature,
)


def test_hmac_valid_signature_with_prefix():
    secret = "a" * 32
    body = b'{"action": "test"}'
    ts = int(time.time())
    sig = generate_hmac_signature(secret, body, ts, include_prefix=True)

    result = verify_hmac_signature(secret, body, sig, str(ts), current_time=ts)
    assert result.is_valid
    assert result.status_code == 200
    assert result.reason == "verified"


def test_hmac_valid_signature_without_prefix():
    secret = "a" * 32
    body = b'{"action": "test"}'
    ts = int(time.time())
    sig = generate_hmac_signature(secret, body, ts, include_prefix=False)

    result = verify_hmac_signature(secret, body, sig, str(ts), current_time=ts)
    assert result.is_valid
    assert result.status_code == 200


def test_hmac_missing_headers():
    secret = "a" * 32
    body = b"data"
    result = verify_hmac_signature(secret, body, None, "123456")
    assert not result.is_valid
    assert result.status_code == 401
    assert result.reason == "missing_security_headers"


def test_hmac_invalid_timestamp_format():
    secret = "a" * 32
    body = b"data"
    result = verify_hmac_signature(secret, body, "sha256=123", "not_an_int")
    assert not result.is_valid
    assert result.status_code == 401
    assert result.reason == "invalid_timestamp_format"


def test_hmac_expired_timestamp():
    secret = "a" * 32
    body = b"data"
    now = 1000000
    old_ts = now - 301  # Exceeds 300s
    sig = generate_hmac_signature(secret, body, old_ts)

    result = verify_hmac_signature(secret, body, sig, str(old_ts), tolerance_seconds=300, current_time=now)
    assert not result.is_valid
    assert result.status_code == 401
    assert result.reason == "timestamp_expired"


def test_hmac_future_timestamp_rejected():
    secret = "a" * 32
    body = b"data"
    now = 1000000
    future_ts = now + 65  # Exceeds 60s future threshold
    sig = generate_hmac_signature(secret, body, future_ts)

    result = verify_hmac_signature(
        secret, body, sig, str(future_ts), future_tolerance_seconds=60, current_time=now
    )
    assert not result.is_valid
    assert result.status_code == 401
    assert result.reason == "timestamp_in_future"


def test_hmac_acceptable_future_timestamp():
    secret = "a" * 32
    body = b"data"
    now = 1000000
    acceptable_future_ts = now + 30  # Within 60s
    sig = generate_hmac_signature(secret, body, acceptable_future_ts)

    result = verify_hmac_signature(
        secret, body, sig, str(acceptable_future_ts), future_tolerance_seconds=60, current_time=now
    )
    assert result.is_valid


def test_hmac_tampered_payload_rejected():
    secret = "a" * 32
    body = b'{"amount": 100}'
    ts = 1000000
    sig = generate_hmac_signature(secret, body, ts)

    # Modify body
    tampered_body = b'{"amount": 1000}'
    result = verify_hmac_signature(secret, tampered_body, sig, str(ts), current_time=ts)
    assert not result.is_valid
    assert result.status_code == 401
    assert result.reason == "signature_mismatch"


def test_hmac_tampered_signature_rejected():
    secret = "a" * 32
    body = b"hello"
    ts = 1000000
    sig = generate_hmac_signature(secret, body, ts)

    tampered_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    result = verify_hmac_signature(secret, body, tampered_sig, str(ts), current_time=ts)
    assert not result.is_valid
    assert result.status_code == 401
    assert result.reason == "signature_mismatch"


def test_bearer_token_valid():
    result = verify_bearer_token("secret_token_123", "Bearer secret_token_123")
    assert result.is_valid
    assert result.status_code == 200


def test_bearer_token_invalid():
    result = verify_bearer_token("secret_token_123", "Bearer wrong_token")
    assert not result.is_valid
    assert result.status_code == 401
    assert result.reason == "invalid_bearer_token"


def test_bearer_token_missing_or_malformed():
    res1 = verify_bearer_token("secret_token", None)
    assert not res1.is_valid

    res2 = verify_bearer_token("secret_token", "Basic abc")
    assert not res2.is_valid
    assert res2.reason == "missing_or_malformed_bearer_token"


def test_validate_request_security_modes():
    secret = "k" * 32
    token = "t" * 32
    ts = 1000000
    body = b'{"test": true}'
    sig = generate_hmac_signature(secret, body, ts)

    # 1. Mode: hmac
    cfg_hmac = SecurityConfig(auth_mode="hmac", webhook_secret=secret)
    req1 = HTTPRequest(
        method="POST",
        path="/webhook",
        headers={"x-hub-signature-256": sig, "x-hub-timestamp": str(ts)},
        body=body,
    )
    assert validate_request_security(req1, cfg_hmac, current_time=ts).is_valid

    # 2. Mode: bearer
    cfg_bearer = SecurityConfig(auth_mode="bearer", bearer_token=token)
    req2 = HTTPRequest(
        method="POST",
        path="/webhook",
        headers={"authorization": f"Bearer {token}"},
        body=body,
    )
    assert validate_request_security(req2, cfg_bearer, current_time=ts).is_valid

    # 3. Mode: any (accepts HMAC)
    cfg_any = SecurityConfig(auth_mode="any", webhook_secret=secret, bearer_token=token)
    assert validate_request_security(req1, cfg_any, current_time=ts).is_valid

    # 3b. Mode: any (accepts Bearer)
    assert validate_request_security(req2, cfg_any, current_time=ts).is_valid

    # 4. Mode: both (requires both)
    cfg_both = SecurityConfig(auth_mode="both", webhook_secret=secret, bearer_token=token)
    req_both = HTTPRequest(
        method="POST",
        path="/webhook",
        headers={
            "x-hub-signature-256": sig,
            "x-hub-timestamp": str(ts),
            "authorization": f"Bearer {token}",
        },
        body=body,
    )
    assert validate_request_security(req_both, cfg_both, current_time=ts).is_valid


def test_hash_utilities():
    body = b"example_payload"
    h1 = compute_payload_hash(body)
    assert len(h1) == 64
    assert h1 == compute_payload_hash(body)

    sig = "sha256=" + "f" * 64
    d1 = compute_dedup_hash(sig, 12345, body)
    d2 = compute_dedup_hash("f" * 64, 12345, body)
    assert d1 == d2
    assert len(d1) == 64
