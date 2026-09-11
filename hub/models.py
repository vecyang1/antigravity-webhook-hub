"""
Antigravity Webhook Hub — Data Models
Standard library dataclasses providing zero-dependency, type-safe data structures
with minimal memory footprint (<30MB budget).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Optional


class TaskStatus(str, Enum):
    """Authoritative task status state machine values."""

    RECEIVED = "received"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"

    @classmethod
    def terminal_statuses(cls) -> set[str]:
        return {cls.SUCCEEDED.value, cls.FAILED.value, cls.TIMED_OUT.value}

    @classmethod
    def is_terminal(cls, status: str) -> bool:
        return status in cls.terminal_statuses()

    @classmethod
    def valid_transitions(cls) -> dict[str, set[str]]:
        return {
            cls.RECEIVED.value: {cls.QUEUED.value, cls.RUNNING.value, cls.FAILED.value},
            cls.QUEUED.value: {cls.RUNNING.value, cls.FAILED.value},
            cls.RUNNING.value: {cls.SUCCEEDED.value, cls.FAILED.value, cls.TIMED_OUT.value},
            cls.SUCCEEDED.value: set(),
            cls.FAILED.value: set(),
            cls.TIMED_OUT.value: set(),
        }

    @classmethod
    def can_transition(cls, from_status: str, to_status: str) -> bool:
        valid = cls.valid_transitions().get(from_status, set())
        return to_status in valid


@dataclass(slots=True)
class ValidationResult:
    """Result of cryptographic authentication & security validation."""

    is_valid: bool
    reason: str
    message: str
    status_code: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "reason": self.reason,
            "message": self.message,
            "status_code": self.status_code,
        }


@dataclass(slots=True)
class HTTPRequest:
    """Incoming HTTP request representation."""

    method: str
    path: str
    raw_path: str = ""
    query_params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    remote_addr: str = "127.0.0.1"
    timestamp: float = field(default_factory=time.time)

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """Case-insensitive header lookup."""
        name_lower = name.lower()
        return self.headers.get(name_lower, default)

    def json(self) -> Any:
        """Parse request body as JSON."""
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self, encoding: str = "utf-8") -> str:
        """Decode request body as text."""
        return self.body.decode(encoding)


@dataclass(slots=True)
class HTTPResponse:
    """Outgoing HTTP response representation."""

    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    is_stream: bool = False
    stream_generator: Optional[AsyncIterator[bytes]] = None

    @classmethod
    def json(
        cls,
        data: Any,
        status_code: int = 200,
        headers: Optional[dict[str, str]] = None,
    ) -> HTTPResponse:
        resp_headers = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            resp_headers.update(headers)
        body = json.dumps(data, ensure_ascii=False, indent=None).encode("utf-8")
        return cls(status_code=status_code, headers=resp_headers, body=body)

    @classmethod
    def text(
        cls,
        text_content: str,
        status_code: int = 200,
        content_type: str = "text/plain; charset=utf-8",
        headers: Optional[dict[str, str]] = None,
    ) -> HTTPResponse:
        resp_headers = {"Content-Type": content_type}
        if headers:
            resp_headers.update(headers)
        return cls(status_code=status_code, headers=resp_headers, body=text_content.encode("utf-8"))

    @classmethod
    def stream(
        cls,
        generator: AsyncIterator[bytes],
        content_type: str = "text/event-stream",
        headers: Optional[dict[str, str]] = None,
    ) -> HTTPResponse:
        resp_headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
        if headers:
            resp_headers.update(headers)
        return cls(
            status_code=200,
            headers=resp_headers,
            is_stream=True,
            stream_generator=generator,
        )

    @classmethod
    def empty(
        cls,
        status_code: int = 204,
        headers: Optional[dict[str, str]] = None,
    ) -> HTTPResponse:
        return cls(status_code=status_code, headers=headers or {}, body=b"")

    @classmethod
    def error(
        cls,
        message: str,
        status_code: int = 400,
        reason: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> HTTPResponse:
        payload: dict[str, Any] = {
            "error": "error" if status_code >= 500 else "client_error",
            "message": message,
        }
        if reason:
            payload["reason"] = reason
        if status_code == 401:
            payload["error"] = "unauthorized"
        elif status_code == 403:
            payload["error"] = "forbidden"
        elif status_code == 404:
            payload["error"] = "not_found"
        elif status_code == 413:
            payload["error"] = "payload_too_large"
        return cls.json(payload, status_code=status_code, headers=headers)


@dataclass(slots=True)
class WebhookEvent:
    """Incoming webhook event record persisted to SQLite SSOT."""

    event_id: str
    source: str = "default"
    event_type: str = "webhook"
    raw_payload: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)
    idempotency_key: Optional[str] = None
    payload_hash: str = ""
    status: str = TaskStatus.RECEIVED.value
    task_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source": self.source,
            "event_type": self.event_type,
            "raw_payload": self.raw_payload,
            "payload": self.payload,
            "headers": self.headers,
            "received_at": self.received_at,
            "idempotency_key": self.idempotency_key,
            "payload_hash": self.payload_hash,
            "status": self.status,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebhookEvent:
        return cls(
            event_id=data["event_id"],
            source=data.get("source", "default"),
            event_type=data.get("event_type", "webhook"),
            raw_payload=data.get("raw_payload", ""),
            payload=data.get("payload", {}),
            headers=data.get("headers", {}),
            received_at=data.get("received_at", time.time()),
            idempotency_key=data.get("idempotency_key"),
            payload_hash=data.get("payload_hash", ""),
            status=data.get("status", TaskStatus.RECEIVED.value),
            task_id=data.get("task_id"),
        )


@dataclass(slots=True)
class Task:
    """Task entity managed by the unidirectional SSOT state machine."""

    task_id: str
    event_id: Optional[str] = None
    source: str = "default"
    action_type: str = "cli"
    command: Optional[str] = None
    status: str = TaskStatus.RECEIVED.value
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    timeout_seconds: int = 300
    retry_count: int = 0
    max_retries: int = 0
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "event_id": self.event_id,
            "source": self.source,
            "action_type": self.action_type,
            "command": self.command,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "timeout_seconds": self.timeout_seconds,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            task_id=data["task_id"],
            event_id=data.get("event_id"),
            source=data.get("source", "default"),
            action_type=data.get("action_type", "cli"),
            command=data.get("command"),
            status=data.get("status", TaskStatus.RECEIVED.value),
            priority=data.get("priority", 0),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            timeout_seconds=data.get("timeout_seconds", 300),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 0),
            result=data.get("result"),
            error=data.get("error"),
        )


@dataclass(slots=True)
class TaskExecution:
    """Execution attempt record for a task."""

    execution_id: str
    task_id: str
    attempt: int = 1
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    status: str = TaskStatus.RUNNING.value
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "status": self.status,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskExecution:
        return cls(
            execution_id=data["execution_id"],
            task_id=data["task_id"],
            attempt=data.get("attempt", 1),
            started_at=data.get("started_at", time.time()),
            completed_at=data.get("completed_at"),
            exit_code=data.get("exit_code"),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            status=data.get("status", TaskStatus.RUNNING.value),
            error=data.get("error"),
        )


@dataclass(slots=True)
class ExecutionLog:
    """Individual line or chunk of output from a task execution."""

    log_id: str
    task_id: str
    execution_id: str
    timestamp: float = field(default_factory=time.time)
    stream: str = "stdout"
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "log_id": self.log_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "timestamp": self.timestamp,
            "stream": self.stream,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionLog:
        return cls(
            log_id=data["log_id"],
            task_id=data["task_id"],
            execution_id=data["execution_id"],
            timestamp=data.get("timestamp", time.time()),
            stream=data.get("stream", "stdout"),
            message=data.get("message", ""),
        )


TEST_EVENT_SQL_FILTER = """(
    command LIKE 'echo %'
    OR target_action LIKE 'echo %'
    OR source LIKE '%test%'
    OR source LIKE '%verifier%'
    OR source LIKE '%simulator%'
    OR task_id LIKE '%test%'
    OR event_id LIKE '%test%'
    OR event_id LIKE '%orphan%'
    OR action_params_json LIKE '%"nonce":%'
    OR action_params_json LIKE '%''nonce'':%'
    OR action_params_json LIKE '%"dry_run": true%'
    OR action_params_json LIKE '%"dry_run":true%'
    OR command LIKE '%CF_TUNNEL%'
    OR command LIKE '%BEARER_AUTH%'
    OR command LIKE '%PUBLIC_CF_TUNNEL%'
    OR command LIKE '%PUBLIC_VERIFY%'
    OR command LIKE '%REVERIFY%'
    OR command LIKE '%sweeper_recovered%'
    OR command LIKE '%dedup_test%'
    OR command LIKE '%time.sleep%'
)"""


def is_test_task(task: Optional[dict[str, Any]]) -> bool:
    """Determine if a task record is an automated, synthetic, or verification test event."""
    if not task or not isinstance(task, dict):
        return False

    task_id = (task.get("task_id") or "").lower()
    event_id = (task.get("event_id") or "").lower()
    source = (task.get("source") or "").lower()
    command = str(task.get("command") or "").strip()
    target_action = str(task.get("target_action") or "").strip()
    raw_params = task.get("action_params_json") or task.get("action_params") or ""

    if isinstance(raw_params, dict):
        try:
            params_str = json.dumps(raw_params)
        except Exception:
            params_str = str(raw_params)
    else:
        params_str = str(raw_params).strip()

    cmd_lower = command.lower()
    target_lower = target_action.lower()

    # 1. ID checks
    if "test" in task_id or "test" in event_id or "orphan" in event_id:
        return True

    # 2. Source checks (synthetic E2E runners, simulators, test-send CLI)
    if any(k in source for k in ("test", "simulator", "verifier")):
        return True

    # 3. Command / target_action checks
    if command.startswith("echo ") or target_action.startswith("echo "):
        return True

    if any(k in cmd_lower or k in target_lower for k in (
        "dedup_test",
        "step2_evt_verify",
        "verify_step",
        "sweeper_recovered",
        "cf_tunnel_e2e_verified",
        "bearer_auth_public_verified",
        "public_cf_tunnel_verified",
        "public_verify_success",
        "reverify_success",
        "stress test pass",
        "hello from m4 test",
        "hello challenger",
        "test webhook payload",
        "time.sleep",
    )):
        return True

    # 4. Action params / payload checks
    if params_str:
        params_lower = params_str.lower()
        if '"nonce":' in params_lower or "'nonce':" in params_lower or "nonce" in params_lower:
            return True
        if '"dry_run": true' in params_lower or '"dry_run":true' in params_lower or "'dry_run': true" in params_lower:
            return True
        if any(k in params_lower for k in (
            "test contact review verification",
            "test-verify@worldinspirelab.com",
            "cloudflare-tunnel-test",
            "live-verification-test",
            "sync-test",
            "manual_simulator",
            "slack_supplementary_test",
            "testing tunnel ingress connectivity",
            "testing sync parameter return value",
            "testing uptime kuma ingress",
            "live public tunnel probe",
        )):
            return True

    return False

