"""
Antigravity Webhook Hub — Cryptographic Request Validation
Implements HMAC SHA-256 validation binding timestamp + raw body,
timing-safe constant-time comparison via hmac.compare_digest,
300s past timestamp drift tolerance, >60s future timestamp rejection,
and Bearer token fallback.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

from hub.config import SecurityConfig
from hub.models import HTTPRequest, ValidationResult


def compute_payload_hash(raw_body: bytes) -> str:
    """Compute SHA-256 hash of the raw request payload."""
    return hashlib.sha256(raw_body).hexdigest()


def compute_dedup_hash(signature: str, timestamp: int, raw_body: bytes) -> str:
    """
    Compute a deterministic deduplication hash binding signature, timestamp, and body.
    Used by SQLite SSOT to enforce idempotency and detect replay attempts within window.
    """
    cleaned_sig = signature.strip().lower()
    if cleaned_sig.startswith("sha256="):
        cleaned_sig = cleaned_sig[7:]
    content = f"{cleaned_sig}:{timestamp}:".encode("utf-8") + raw_body
    return hashlib.sha256(content).hexdigest()


def generate_hmac_signature(
    secret: str | bytes,
    raw_body: bytes,
    timestamp: int,
    include_prefix: bool = True,
) -> str:
    """
    Generate an HMAC SHA-256 signature binding timestamp and raw body.
    Algorithm: HMAC-SHA256(secret, timestamp + "." + raw_body)
    """
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    message = f"{timestamp}.".encode("utf-8") + raw_body
    digest = hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()
    return f"sha256={digest}" if include_prefix else digest


def verify_hmac_signature(
    secret: str | bytes,
    raw_body: bytes,
    signature_header: Optional[str],
    timestamp_header: Optional[str],
    tolerance_seconds: int = 300,
    future_tolerance_seconds: int = 60,
    current_time: Optional[float] = None,
) -> ValidationResult:
    """
    Verify HMAC SHA-256 signature with replay defense.
    - Validates presence of signature and timestamp headers.
    - Rejects timestamps older than tolerance_seconds (default 300s).
    - Rejects timestamps further in the future than future_tolerance_seconds (default 60s).
    - Binds timestamp to raw body: f"{timestamp}." + raw_body.
    - Uses constant-time hmac.compare_digest.
    """
    if not signature_header or not timestamp_header:
        return ValidationResult(
            is_valid=False,
            reason="missing_security_headers",
            message="Missing required signature or timestamp headers",
            status_code=401,
        )

    # 1. Parse integer timestamp
    try:
        req_timestamp = int(timestamp_header.strip())
    except (ValueError, TypeError):
        return ValidationResult(
            is_valid=False,
            reason="invalid_timestamp_format",
            message="Timestamp header must be a valid integer Unix timestamp in seconds",
            status_code=401,
        )

    now = int(current_time if current_time is not None else time.time())

    # 2. Check past timestamp drift (replay attack defense)
    if req_timestamp < (now - tolerance_seconds):
        drift = now - req_timestamp
        return ValidationResult(
            is_valid=False,
            reason="timestamp_expired",
            message=f"Request timestamp delta ({drift}s) exceeds allowed past tolerance window ({tolerance_seconds}s)",
            status_code=401,
        )

    # 3. Check future timestamp drift (>60s rejection)
    if req_timestamp > (now + future_tolerance_seconds):
        future_drift = req_timestamp - now
        return ValidationResult(
            is_valid=False,
            reason="timestamp_in_future",
            message=f"Request timestamp is {future_drift}s in the future (exceeds {future_tolerance_seconds}s tolerance)",
            status_code=401,
        )

    # 4. Clean signature prefix
    received_sig = signature_header.strip()
    if received_sig.lower().startswith("sha256="):
        received_sig = received_sig[7:]

    # Hex verification: must be 64 hex characters
    if len(received_sig) != 64 or not all(c in "0123456789abcdefABCDEF" for c in received_sig):
        return ValidationResult(
            is_valid=False,
            reason="invalid_signature_format",
            message="Signature header must be a 64-character hexadecimal SHA-256 string",
            status_code=401,
        )

    # 5. Compute expected HMAC
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    message = f"{req_timestamp}.".encode("utf-8") + raw_body
    expected_sig = hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()

    # 6. Constant-time comparison
    if not hmac.compare_digest(expected_sig.lower(), received_sig.lower()):
        return ValidationResult(
            is_valid=False,
            reason="signature_mismatch",
            message="Computed HMAC SHA-256 does not match provided signature header",
            status_code=401,
        )

    return ValidationResult(
        is_valid=True,
        reason="verified",
        message="HMAC SHA-256 signature successfully verified",
        status_code=200,
    )


def verify_bearer_token(
    expected_token: str,
    auth_header: Optional[str],
) -> ValidationResult:
    """
    Verify Bearer token authentication header using constant-time comparison.
    Header format: Authorization: Bearer <token>
    """
    if not auth_header:
        return ValidationResult(
            is_valid=False,
            reason="missing_authorization_header",
            message="Missing required Authorization header",
            status_code=401,
        )

    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ValidationResult(
            is_valid=False,
            reason="missing_or_malformed_bearer_token",
            message="Malformed Authorization header (expected format: 'Bearer <token>')",
            status_code=401,
        )

    received_token = parts[1].strip()
    if not received_token:
        return ValidationResult(
            is_valid=False,
            reason="missing_bearer_token",
            message="Bearer token value cannot be empty",
            status_code=401,
        )

    if not expected_token:
        return ValidationResult(
            is_valid=False,
            reason="bearer_token_not_configured",
            message="Server has not configured a Bearer token for authentication",
            status_code=401,
        )

    # Constant-time comparison
    if not hmac.compare_digest(expected_token.encode("utf-8"), received_token.encode("utf-8")):
        return ValidationResult(
            is_valid=False,
            reason="invalid_bearer_token",
            message="Provided Bearer token is invalid",
            status_code=401,
        )

    return ValidationResult(
        is_valid=True,
        reason="verified",
        message="Bearer token successfully verified",
        status_code=200,
    )


def validate_request_security(
    request: HTTPRequest,
    config: SecurityConfig,
    current_time: Optional[float] = None,
) -> ValidationResult:
    """
    Validate incoming request security according to configured auth_mode:
    - 'hmac': strictly requires valid HMAC signature & timestamp
    - 'bearer': strictly requires valid Bearer token
    - 'any': accepts if EITHER HMAC or Bearer is valid
    - 'both': requires BOTH HMAC and Bearer to be valid
    """
    mode = config.auth_mode.lower()

    # Look up headers (case-insensitive)
    sig_header = request.header(config.signature_header)
    if not sig_header:
        # Fallback header names
        sig_header = request.header("x-signature-256") or request.header("x-signature")

    ts_header = request.header(config.timestamp_header)
    if not ts_header:
        # Fallback header name
        ts_header = request.header("x-timestamp")

    auth_header = request.header("authorization")

    if mode == "hmac":
        if not config.webhook_secret:
            return ValidationResult(
                is_valid=False,
                reason="webhook_secret_not_configured",
                message="Server has not configured a WEBHOOK_SECRET for HMAC verification",
                status_code=401,
            )
        return verify_hmac_signature(
            secret=config.webhook_secret,
            raw_body=request.body,
            signature_header=sig_header,
            timestamp_header=ts_header,
            tolerance_seconds=config.timestamp_tolerance_seconds,
            future_tolerance_seconds=config.future_timestamp_tolerance_seconds,
            current_time=current_time,
        )

    elif mode == "bearer":
        return verify_bearer_token(
            expected_token=config.bearer_token,
            auth_header=auth_header,
        )

    elif mode == "any":
        # Check if signature header is present -> attempt HMAC
        if sig_header or ts_header:
            hmac_res = verify_hmac_signature(
                secret=config.webhook_secret,
                raw_body=request.body,
                signature_header=sig_header,
                timestamp_header=ts_header,
                tolerance_seconds=config.timestamp_tolerance_seconds,
                future_tolerance_seconds=config.future_timestamp_tolerance_seconds,
                current_time=current_time,
            )
            if hmac_res.is_valid:
                return hmac_res
            # If Bearer header is also provided, try Bearer before failing
            if auth_header:
                bearer_res = verify_bearer_token(config.bearer_token, auth_header)
                if bearer_res.is_valid:
                    return bearer_res
            return hmac_res

        # If no HMAC headers but Authorization header is present -> attempt Bearer
        if auth_header:
            return verify_bearer_token(config.bearer_token, auth_header)

        # Neither credentials provided
        return ValidationResult(
            is_valid=False,
            reason="missing_credentials",
            message="Request must include either valid HMAC signature headers or Authorization Bearer token",
            status_code=401,
        )

    elif mode == "both":
        # Both HMAC and Bearer required
        hmac_res = verify_hmac_signature(
            secret=config.webhook_secret,
            raw_body=request.body,
            signature_header=sig_header,
            timestamp_header=ts_header,
            tolerance_seconds=config.timestamp_tolerance_seconds,
            future_tolerance_seconds=config.future_timestamp_tolerance_seconds,
            current_time=current_time,
        )
        if not hmac_res.is_valid:
            return hmac_res

        bearer_res = verify_bearer_token(config.bearer_token, auth_header)
        if not bearer_res.is_valid:
            return bearer_res

        return ValidationResult(
            is_valid=True,
            reason="verified",
            message="Both HMAC signature and Bearer token successfully verified",
            status_code=200,
        )

    else:
        return ValidationResult(
            is_valid=False,
            reason="invalid_auth_mode",
            message=f"Unsupported auth_mode configured: {mode}",
            status_code=500,
        )
