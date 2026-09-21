"""
Antigravity Webhook Hub — Uptime Kuma Ingress Route
Implements POST /api/webhook/uptime-kuma and POST /webhook/uptime-kuma.
Features:
- Bearer token authentication & HMAC fallback with zero side effects on failure.
- Contract schema validation for Uptime Kuma webhook payloads.
- Anti-flap jitter debounce: immediate double-check probe suppresses momentary network blips.
- Anti-flap cooldown rate limiter preventing triage cascade.
- Dual-channel observability: macOS desktop banner + Slack alert.
- Asynchronous task queuing into SQLite SSOT and dispatcher.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Optional

from hub.alert_filter import AlertFilterResult, JevAlertFilter, get_default_alert_filter
from hub.config import AppConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.security import compute_payload_hash, validate_request_security, verify_bearer_token
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.uptime_kuma")

# Anti-flap in-memory state: monitor_id -> last_down_timestamp
_recent_down_events: dict[str, float] = {}
COOLDOWN_SECONDS = 60.0


def send_desktop_notification(title: str, message: str, sound: str = "Basso") -> None:
    """Send native macOS notification banner via AppleScript osascript."""
    try:
        import subprocess
        clean_title = title.replace("\\", "\\\\").replace('"', '\\"')
        clean_message = message.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{clean_message}" with title "{clean_title}"'
        if sound:
            script += f' sound name "{sound}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception as e:
        logger.warning("Failed to send desktop notification: %s", e)


async def send_slack_alert(
    text: str,
    channel: Optional[str] = None,
    token: Optional[str] = None,
) -> bool:
    """Send alert message to Slack channel."""
    target_token = token or os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    target_channel = channel or os.environ.get("SLACK_DEFAULT_CHANNEL", "C096KR96AF7")

    if not target_token or not target_channel:
        return False

    def _post():
        import urllib.request
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": target_channel, "text": text}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {target_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok", False)

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _post)
    except Exception as e:
        logger.warning("Slack alert failed: %s", e)
        return False


async def probe_url_liveness(url: str, timeout: float = 3.0) -> bool:
    """
    Perform immediate double-check HTTP GET probe to verify if endpoint is truly down.
    Returns True if endpoint responds with 2xx or 3xx; False on network error or 4xx/5xx.
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return False

    def _probe():
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Antigravity-AntiFlap-Probe/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _probe)
    except Exception:
        return False


def register_uptime_kuma_routes(
    server: AsyncHTTPServer,
    config: AppConfig,
    db: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
    broker: Optional[Any] = None,
    probe_fn: Optional[Callable[[str, float], Any]] = None,
    alert_filter: Optional[Any] = None,
) -> None:
    """Register Uptime Kuma and Jev Alert Filter webhook receiver endpoints."""

    active_probe_fn = probe_fn or probe_url_liveness
    if alert_filter is not None:
        active_alert_filter = alert_filter
    else:
        active_alert_filter = get_default_alert_filter(config)

    async def handle_uptime_kuma_webhook(req: HTTPRequest) -> HTTPResponse:
        # 1. Authentication & Security Validation
        # MUST happen before ANY database interaction. Rejections produce ZERO DB writes.
        auth_header = req.header("authorization")
        expected_bearer = config.security.bearer_token or os.environ.get("BEARER_TOKEN", "")

        val = validate_request_security(req, config.security)
        if not val.is_valid:
            # Check direct Bearer token fallback if configured
            bearer_val = verify_bearer_token(expected_bearer, auth_header)
            if not bearer_val.is_valid:
                return HTTPResponse.error(
                    val.message or bearer_val.message,
                    status_code=401,
                    reason="unauthorized",
                )

        # 2. Body & Schema Validation
        if not req.body:
            return HTTPResponse.error("Request body is empty", status_code=400, reason="empty_body")

        try:
            body_dict = json.loads(req.body.decode("utf-8"))
        except Exception:
            return HTTPResponse.error("Malformed JSON payload", status_code=400, reason="malformed_json")

        if not isinstance(body_dict, dict):
            return HTTPResponse.error("Payload must be a JSON object", status_code=400, reason="invalid_json_type")

        # Contract Schema Check: Uptime Kuma format
        # Must contain 'heartbeat' dict and 'monitor' dict
        heartbeat = body_dict.get("heartbeat")
        monitor = body_dict.get("monitor")

        if not isinstance(heartbeat, dict) or not isinstance(monitor, dict):
            return HTTPResponse.error(
                "Invalid schema: missing 'heartbeat' or 'monitor' object",
                status_code=400,
                reason="invalid_schema",
            )

        raw_status = heartbeat.get("status")
        try:
            status_code = int(raw_status)
        except (ValueError, TypeError):
            return HTTPResponse.error(
                "Invalid schema: 'heartbeat.status' must be an integer (0=DOWN, 1=UP)",
                status_code=400,
                reason="invalid_schema",
            )

        monitor_name = str(monitor.get("name") or "Unknown Monitor")
        monitor_url = str(monitor.get("url") or "")
        monitor_id = str(monitor.get("id") or monitor_name)
        msg = str(body_dict.get("msg") or heartbeat.get("msg") or "")

        payload_hash = compute_payload_hash(req.body)
        idemp_key = req.header("x-kuma-event-id") or hashlib.sha256(
            f"kuma:{monitor_id}:{status_code}:{payload_hash}".encode()
        ).hexdigest()

        # 3. Status Branching & Anti-Flap Debounce
        # Status codes in Uptime Kuma: 0=DOWN, 1=UP, 2=PENDING, 3=MAINTENANCE
        is_down = (status_code == 0)
        is_up = (status_code == 1)

        # Case A: Service is UP (Healthy / Recovery)
        if is_up:
            was_down = _recent_down_events.pop(monitor_id, None) is not None
            logger.info("Uptime Kuma UP received for monitor: %s (%s)", monitor_name, monitor_url)

            # Persist event to DB
            event_id = f"evt_{uuid.uuid4().hex[:16]}"
            if db is not None:
                db.insert_webhook_event({
                    "event_id": event_id,
                    "source": "uptime_kuma",
                    "idempotency_key": idemp_key,
                    "payload_hash": payload_hash,
                    "headers_json": json.dumps(dict(req.headers)),
                    "raw_payload": req.text(),
                    "method": req.method,
                    "path": req.path,
                    "remote_addr": req.remote_addr,
                    "status": "processed",
                })

            if broker is not None:
                await broker.publish("events", {
                    "type": "kuma_up",
                    "monitor": monitor_name,
                    "url": monitor_url,
                    "msg": msg,
                })

            # Health probe alerting discipline: only alert when unhealthy (or recovering from outage).
            # Routine periodic healthy heartbeats stay quiet to prevent alert fatigue.
            notify_on_up = os.environ.get("UPTIME_KUMA_NOTIFY_ON_UP", "false").lower() in ("true", "1", "yes")
            notify_on_recovery = os.environ.get("UPTIME_KUMA_NOTIFY_ON_RECOVERY", "true").lower() in ("true", "1", "yes")
            if notify_on_up:
                send_desktop_notification("Antigravity Webhook Hub", f"🟢 [UP] {monitor_name} is operational", sound="Glass")
            elif was_down and notify_on_recovery:
                send_desktop_notification("Antigravity Webhook Hub", f"🟢 [RECOVERED] {monitor_name} is back operational", sound="Glass")

            return HTTPResponse.json({
                "status": "up",
                "monitor": monitor_name,
                "msg": f"{monitor_name} is UP",
                "event_id": event_id,
            }, status_code=200)

        # Case B: Service is DOWN (Outage Detected) -> Execute Anti-Flap Verification
        if is_down:
            # 3.1 Immediate Double-Check Probe
            if monitor_url:
                probe_res = active_probe_fn(monitor_url, 3.0)
                if asyncio.iscoroutine(probe_res):
                    probe_alive = await probe_res
                else:
                    probe_alive = bool(probe_res)

                if probe_alive:
                    # Momentary network jitter / flap detected!
                    logger.info(
                        "Anti-flap debounce: %s reported DOWN by Kuma, but double-check probe succeeded. Flap suppressed.",
                        monitor_name,
                    )
                    event_id = f"evt_{uuid.uuid4().hex[:16]}"
                    if db is not None:
                        db.insert_webhook_event({
                            "event_id": event_id,
                            "source": "uptime_kuma",
                            "idempotency_key": idemp_key,
                            "payload_hash": payload_hash,
                            "headers_json": json.dumps(dict(req.headers)),
                            "raw_payload": req.text(),
                            "method": req.method,
                            "path": req.path,
                            "remote_addr": req.remote_addr,
                            "status": "processed",
                        })

                    if broker is not None:
                        await broker.publish("events", {
                            "type": "kuma_flap_suppressed",
                            "monitor": monitor_name,
                            "url": monitor_url,
                            "reason": "Double-check probe succeeded (momentary jitter)",
                        })

                    return HTTPResponse.json({
                        "status": "debounced_flap",
                        "monitor": monitor_name,
                        "url": monitor_url,
                        "flap_suppressed": True,
                        "reason": "Double-check probe succeeded (momentary network jitter suppressed)",
                        "event_id": event_id,
                    }, status_code=200)

            # 3.2 Cooldown Debounce (Rate Limiting per Monitor)
            now = time.time()
            last_alert = _recent_down_events.get(monitor_id, 0.0)
            if (now - last_alert) < COOLDOWN_SECONDS:
                logger.info(
                    "Anti-flap cooldown: %s triggered DOWN within %0.1fs. Coalescing alert.",
                    monitor_name,
                    COOLDOWN_SECONDS,
                )
                return HTTPResponse.json({
                    "status": "cooldown_coalesced",
                    "monitor": monitor_name,
                    "cooldown_remaining": round(COOLDOWN_SECONDS - (now - last_alert), 1),
                    "coalesced": True,
                }, status_code=200)

            _recent_down_events[monitor_id] = now

            # 3.25 Intelligent Jev System One Noise Evaluation & Flap Suppression
            filter_res = None
            if active_alert_filter and getattr(active_alert_filter, "enabled", True):
                filter_res = await active_alert_filter.evaluate_alert_async(
                    service=monitor_name,
                    message=msg,
                    url=monitor_url,
                    context={"status_code": status_code, "monitor_id": monitor_id},
                )
                if not filter_res.is_critical:
                    logger.info(
                        "Jev Alert Filter: Suppressed non-critical alert for %s (noul=%0.2f < threshold=%0.2f, reason: %s)",
                        monitor_name, filter_res.noul, filter_res.threshold, filter_res.reason
                    )
                    event_id = f"evt_{uuid.uuid4().hex[:16]}"
                    if db is not None:
                        db.insert_webhook_event({
                            "event_id": event_id,
                            "source": "uptime_kuma",
                            "idempotency_key": idemp_key,
                            "payload_hash": payload_hash,
                            "headers_json": json.dumps(dict(req.headers)),
                            "raw_payload": req.text(),
                            "method": req.method,
                            "path": req.path,
                            "remote_addr": req.remote_addr,
                            "status": "processed",
                        })
                    if broker is not None:
                        await broker.publish("events", {
                            "type": "kuma_suppressed_by_jev",
                            "monitor": monitor_name,
                            "url": monitor_url,
                            "noul": filter_res.noul,
                            "threshold": filter_res.threshold,
                            "reason": filter_res.reason,
                        })
                    return HTTPResponse.json({
                        "status": "suppressed_noise",
                        "monitor": monitor_name,
                        "url": monitor_url,
                        "is_critical": False,
                        "noul": filter_res.noul,
                        "threshold": filter_res.threshold,
                        "reason": filter_res.reason,
                        "model": filter_res.model,
                        "event_id": event_id,
                    }, status_code=200)

            # 3.3 Confirmed Outage -> Immediate Dual-Channel Observability
            jev_tag = f" [Jev: {filter_res.noul:0.2f}]" if filter_res else ""
            outage_desc = f"🔴 [DOWN] {monitor_name}: {msg}{jev_tag}"
            logger.warning("Confirmed outage: %s", outage_desc)

            # Desktop Banner
            send_desktop_notification("Antigravity Alert Hub", outage_desc, sound="Basso")

            # Slack Alert
            asyncio.create_task(send_slack_alert(f"*CRITICAL ALERT*: {outage_desc}\nURL: {monitor_url}"))

            # 3.4 Persist Event & Enqueue Self-Healing Triage Task
            event_id = f"evt_{uuid.uuid4().hex[:16]}"
            task_id = f"tsk_{uuid.uuid4().hex[:16]}"

            triage_script = os.getenv(
                "UPTIME_KUMA_TRIAGE_SCRIPT",
                "/Users/vecsatfoxmailcom/Documents/Cowork/Antigravity Cowork/26.06.06 2nd Brain/00 - System/scripts/audit_maintenance_alert_triage.py",
            )
            command = f'python3 "{triage_script}" --no-success-marker'

            if db is not None:
                db.insert_webhook_event({
                    "event_id": event_id,
                    "source": "uptime_kuma",
                    "idempotency_key": idemp_key,
                    "payload_hash": payload_hash,
                    "headers_json": json.dumps(dict(req.headers)),
                    "raw_payload": req.text(),
                    "method": req.method,
                    "path": req.path,
                    "remote_addr": req.remote_addr,
                    "status": "received",
                })

                db.insert_task({
                    "task_id": task_id,
                    "event_id": event_id,
                    "source": "uptime_kuma",
                    "action_type": "cli",
                    "command": command,
                    "target_action": command,
                    "action_params_json": json.dumps({
                        "monitor": monitor,
                        "heartbeat": heartbeat,
                        "msg": msg,
                    }),
                    "status": "queued",
                    "priority": 10,
                    "timeout_seconds": 180,
                    "retry_count": 0,
                    "max_retries": 1,
                })

            if dispatcher is not None:
                await dispatcher.enqueue(task_id)

            if broker is not None:
                await broker.publish("events", {
                    "type": "kuma_down_confirmed",
                    "monitor": monitor_name,
                    "url": monitor_url,
                    "task_id": task_id,
                    "event_id": event_id,
                })

            return HTTPResponse.json({
                "status": "accepted",
                "outage_confirmed": True,
                "event_id": event_id,
                "task_id": task_id,
                "monitor": monitor_name,
                "action": "triage_and_heal_queued",
            }, status_code=202)

        # Other statuses (e.g. pending / maintenance)
        return HTTPResponse.json({
            "status": "ignored_status",
            "kuma_status": status_code,
            "monitor": monitor_name,
        }, status_code=200)

    async def handle_alert_webhook(req: HTTPRequest) -> HTTPResponse:
        """Universal Alert Ingress Route (Coolify, VPS Sentinel, Email Interceptor)."""
        auth_header = req.header("authorization")
        expected_bearer = config.security.bearer_token or os.environ.get("BEARER_TOKEN", "")

        val = validate_request_security(req, config.security)
        if not val.is_valid:
            bearer_val = verify_bearer_token(expected_bearer, auth_header)
            if not bearer_val.is_valid:
                return HTTPResponse.error(
                    val.message or bearer_val.message,
                    status_code=401,
                    reason="unauthorized",
                )

        if not req.body:
            return HTTPResponse.error("Request body is empty", status_code=400, reason="empty_body")

        try:
            body_dict = json.loads(req.body.decode("utf-8"))
        except Exception:
            return HTTPResponse.error("Malformed JSON payload", status_code=400, reason="malformed_json")

        if not isinstance(body_dict, dict):
            return HTTPResponse.error("Payload must be a JSON object", status_code=400, reason="invalid_json_type")

        service_name = str(body_dict.get("service") or body_dict.get("monitor") or body_dict.get("source") or "Server Alert")
        alert_msg = str(body_dict.get("message") or body_dict.get("msg") or body_dict.get("error") or "")
        alert_url = str(body_dict.get("url") or "")
        raw_status = str(body_dict.get("status") or "down").lower()

        # Check recovery / up
        if raw_status in ("up", "ok", "recovered", "resolved"):
            return HTTPResponse.json({
                "status": "up",
                "service": service_name,
                "msg": f"{service_name} status is operational",
            }, status_code=200)

        # Run Jev Intelligent Alert Filter
        if active_alert_filter and getattr(active_alert_filter, "enabled", True):
            filter_res = await active_alert_filter.evaluate_alert_async(
                service=service_name,
                message=alert_msg,
                url=alert_url,
                context=body_dict,
            )
        else:
            filter_res = AlertFilterResult(
                is_critical=True,
                noul=1.0,
                threshold=getattr(active_alert_filter, "critical_threshold", 0.70) if active_alert_filter else 0.70,
                action="escalate",
                reason="Alert filter disabled or not configured; escalated for safety.",
                model="disabled",
                duration_ms=0.0,
                eval_source="fallback",
            )

        event_id = f"evt_{uuid.uuid4().hex[:16]}"
        payload_hash = compute_payload_hash(req.body)
        idemp_key = req.header("x-alert-id") or hashlib.sha256(
            f"alert:{service_name}:{payload_hash}".encode()
        ).hexdigest()

        if not filter_res.is_critical:
            # Suppressed noise
            logger.info(
                "Jev Alert Filter: Suppressed VPS alert for %s (noul=%0.2f < threshold=%0.2f): %s",
                service_name, filter_res.noul, filter_res.threshold, filter_res.reason
            )
            if db is not None:
                db.insert_webhook_event({
                    "event_id": event_id,
                    "source": "alert_filter",
                    "idempotency_key": idemp_key,
                    "payload_hash": payload_hash,
                    "headers_json": json.dumps(dict(req.headers)),
                    "raw_payload": req.text(),
                    "method": req.method,
                    "path": req.path,
                    "remote_addr": req.remote_addr,
                    "status": "processed",
                })
            if broker is not None:
                await broker.publish("events", {
                    "type": "alert_suppressed_by_jev",
                    "service": service_name,
                    "noul": filter_res.noul,
                    "reason": filter_res.reason,
                })
            return HTTPResponse.json({
                "status": "suppressed_noise",
                "service": service_name,
                "is_critical": False,
                "noul": filter_res.noul,
                "threshold": filter_res.threshold,
                "reason": filter_res.reason,
                "model": filter_res.model,
                "event_id": event_id,
            }, status_code=200)

        # Escalated Critical Alert
        outage_desc = f"🔴 [CRITICAL ALERT] {service_name}: {alert_msg} [Jev: {filter_res.noul:0.2f}]"
        logger.warning("Confirmed critical alert: %s", outage_desc)
        send_desktop_notification("Antigravity Alert Hub", outage_desc, sound="Basso")
        asyncio.create_task(send_slack_alert(f"*CRITICAL ALERT*: {outage_desc}\nURL: {alert_url}"))

        if db is not None:
            db.insert_webhook_event({
                "event_id": event_id,
                "source": "alert_filter",
                "idempotency_key": idemp_key,
                "payload_hash": payload_hash,
                "headers_json": json.dumps(dict(req.headers)),
                "raw_payload": req.text(),
                "method": req.method,
                "path": req.path,
                "remote_addr": req.remote_addr,
                "status": "received",
            })

        return HTTPResponse.json({
            "status": "escalated_critical",
            "service": service_name,
            "is_critical": True,
            "noul": filter_res.noul,
            "threshold": filter_res.threshold,
            "reason": filter_res.reason,
            "model": filter_res.model,
            "event_id": event_id,
        }, status_code=202)

    async def handle_alert_diagnose(req: HTTPRequest) -> HTTPResponse:
        """Diagnostic Endpoint: Test any alert against live Jev System One model."""
        nonlocal active_alert_filter
        if not active_alert_filter:
            active_alert_filter = get_default_alert_filter(config)

        if not req.body:
            return HTTPResponse.error("Request body is empty", status_code=400, reason="empty_body")
        try:
            body_dict = json.loads(req.body.decode("utf-8"))
        except Exception:
            return HTTPResponse.error("Malformed JSON payload", status_code=400, reason="malformed_json")

        if not isinstance(body_dict, dict):
            return HTTPResponse.error("Payload must be a JSON object", status_code=400, reason="invalid_json_type")

        service_name = str(body_dict.get("service") or "Diagnostic Test")
        alert_msg = str(body_dict.get("message") or body_dict.get("msg") or "")
        alert_url = str(body_dict.get("url") or "")
        custom_thresh = body_dict.get("threshold")
        thresh_val = None
        if custom_thresh is not None:
            try:
                thresh_val = float(custom_thresh)
            except (ValueError, TypeError):
                return HTTPResponse.error("threshold must be a valid float", status_code=400)

        res = await active_alert_filter.evaluate_alert_async(
            service=service_name,
            message=alert_msg,
            url=alert_url,
            context=body_dict,
            custom_threshold=thresh_val,
        )
        return HTTPResponse.json(res.to_dict(), status_code=200)

    async def handle_alert_metrics(req: HTTPRequest) -> HTTPResponse:
        """Observability Endpoint: Return alert suppression and escalation statistics."""
        nonlocal active_alert_filter
        if not active_alert_filter:
            active_alert_filter = get_default_alert_filter(config)
        return HTTPResponse.json(active_alert_filter.get_metrics(), status_code=200)

    async def handle_alert_config(req: HTTPRequest) -> HTTPResponse:
        """Config Endpoint: Inspect (GET) or update (POST) alert filter runtime configuration."""
        nonlocal active_alert_filter
        if not active_alert_filter:
            active_alert_filter = get_default_alert_filter(config)

        if req.method == "POST":
            if not req.body:
                return HTTPResponse.error("Request body is empty", status_code=400)
            try:
                data = json.loads(req.body.decode("utf-8"))
            except Exception as e:
                return HTTPResponse.error(f"Malformed JSON: {e}", status_code=400)

            if not isinstance(data, dict):
                return HTTPResponse.error("Config payload must be a JSON object", status_code=400)

            if "critical_threshold" in data:
                try:
                    ct = float(data["critical_threshold"])
                    if not (0.0 <= ct <= 1.0):
                        return HTTPResponse.error("critical_threshold must be between 0.0 and 1.0", status_code=400)
                    active_alert_filter.critical_threshold = ct
                except (ValueError, TypeError):
                    return HTTPResponse.error("critical_threshold must be a valid float", status_code=400)

            if "enabled" in data:
                active_alert_filter.enabled = bool(data["enabled"])

            if "fallback_mode" in data:
                fb = str(data["fallback_mode"]).lower().strip()
                if fb not in ("fail_open", "fail_closed", "heuristic"):
                    return HTTPResponse.error("fallback_mode must be one of: 'fail_open', 'fail_closed', 'heuristic'", status_code=400)
                active_alert_filter.fallback_mode = fb

            if "timeout_seconds" in data:
                try:
                    to_s = float(data["timeout_seconds"])
                    if not (0.1 <= to_s <= 60.0):
                        return HTTPResponse.error("timeout_seconds must be between 0.1 and 60.0", status_code=400)
                    active_alert_filter.timeout_seconds = to_s
                except (ValueError, TypeError):
                    return HTTPResponse.error("timeout_seconds must be a valid float", status_code=400)

        return HTTPResponse.json({
            "enabled": active_alert_filter.enabled,
            "critical_threshold": active_alert_filter.critical_threshold,
            "fallback_mode": active_alert_filter.fallback_mode,
            "model": active_alert_filter.model,
            "timeout_seconds": active_alert_filter.timeout_seconds,
            "has_api_key": bool(active_alert_filter.api_key),
        }, status_code=200)

    # Register routes
    server.add_route("POST", "/api/webhook/uptime-kuma", handle_uptime_kuma_webhook)
    server.add_route("POST", "/webhook/uptime-kuma", handle_uptime_kuma_webhook)
    server.add_route("POST", "/api/webhook/alert", handle_alert_webhook)
    server.add_route("POST", "/webhook/alert", handle_alert_webhook)
    server.add_route("POST", "/api/webhook/coolify", handle_alert_webhook)
    server.add_route("POST", "/webhook/coolify", handle_alert_webhook)
    server.add_route("POST", "/api/alerts/diagnose", handle_alert_diagnose)
    server.add_route("GET", "/api/alerts/metrics", handle_alert_metrics)
    server.add_route("GET", "/api/alerts/config", handle_alert_config)
    server.add_route("POST", "/api/alerts/config", handle_alert_config)
