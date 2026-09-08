"""Unit tests for hub.models dataclasses and state transitions."""

import json
from hub.models import (
    ExecutionLog,
    HTTPRequest,
    HTTPResponse,
    Task,
    TaskExecution,
    TaskStatus,
    WebhookEvent,
)


def test_task_status_state_machine():
    assert TaskStatus.is_terminal(TaskStatus.SUCCEEDED.value)
    assert TaskStatus.is_terminal(TaskStatus.FAILED.value)
    assert TaskStatus.is_terminal(TaskStatus.TIMED_OUT.value)
    assert not TaskStatus.is_terminal(TaskStatus.RUNNING.value)
    assert not TaskStatus.is_terminal(TaskStatus.QUEUED.value)

    assert TaskStatus.can_transition("received", "queued")
    assert TaskStatus.can_transition("queued", "running")
    assert TaskStatus.can_transition("running", "succeeded")
    assert TaskStatus.can_transition("running", "failed")
    assert not TaskStatus.can_transition("succeeded", "running")


def test_webhook_event_serialization():
    evt = WebhookEvent(
        event_id="evt_123",
        source="github",
        payload={"action": "opened"},
        headers={"content-type": "application/json"},
    )
    data = evt.to_dict()
    assert data["event_id"] == "evt_123"
    assert data["source"] == "github"

    restored = WebhookEvent.from_dict(data)
    assert restored.event_id == evt.event_id
    assert restored.payload == evt.payload


def test_task_serialization():
    task = Task(
        task_id="tsk_1",
        event_id="evt_1",
        command="echo hello",
        status=TaskStatus.QUEUED.value,
    )
    d = task.to_dict()
    assert d["task_id"] == "tsk_1"
    assert d["status"] == "queued"

    t2 = Task.from_dict(d)
    assert t2.task_id == "tsk_1"
    assert t2.command == "echo hello"


def test_http_request_helpers():
    body = json.dumps({"key": "value"}).encode("utf-8")
    req = HTTPRequest(
        method="POST",
        path="/test",
        headers={"x-custom-header": "test_val"},
        body=body,
    )
    assert req.header("X-Custom-Header") == "test_val"
    assert req.header("non-existent", "default") == "default"
    assert req.json() == {"key": "value"}
    assert "key" in req.text()


def test_http_response_factory():
    # JSON response
    r1 = HTTPResponse.json({"status": "ok"}, status_code=202)
    assert r1.status_code == 202
    assert r1.headers["Content-Type"].startswith("application/json")
    assert b'"status": "ok"' in r1.body

    # Text response
    r2 = HTTPResponse.text("hello world")
    assert r2.status_code == 200
    assert r2.body == b"hello world"

    # Error responses
    r3 = HTTPResponse.error("Not found", status_code=404)
    assert r3.status_code == 404
    assert b"not_found" in r3.body

    r4 = HTTPResponse.error("Too large", status_code=413)
    assert r4.status_code == 413
    assert b"payload_too_large" in r4.body
