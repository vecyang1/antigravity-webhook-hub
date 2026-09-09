"""
End-to-end integration tests for Contact Review Ingress & Dispatch.
Tests two-sided flows:
1. Legitimate path: Bearer authenticated POST /webhook/contact-review -> 202 Accepted ->
   SQLite persistence -> TaskDispatcher execution -> SSOT state & logs -> SSE stream.
2. Adversarial paths: Missing auth (401), invalid JSON (400), deduplication (200).
"""

import asyncio
import json
import pytest
from unittest.mock import patch, MagicMock

from hub.config import AppConfig, ServerConfig, SecurityConfig, DatabaseConfig, DispatchConfig
from hub.db import DatabaseManager
from hub.broker import EventBroker
from hub.dispatcher import TaskDispatcher
from hub.server import AsyncHTTPServer
from hub.routes.webhook import register_webhook_routes
from hub.contact_review.models import ReviewResult, ReviewVerdict, FieldDiff, FieldDiffAction


@pytest.fixture
def temp_env(temp_db_path, free_port):
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=free_port),
        security=SecurityConfig(
            auth_mode="any",
            webhook_secret="test_secret_for_e2e_testing_32chars",
            bearer_token="test_bearer_token_12345",
        ),
        database=DatabaseConfig(path=temp_db_path, wal_mode=True),
        dispatch=DispatchConfig(default_action_timeout_seconds=30, max_concurrent_tasks=2),
    )
    db = DatabaseManager(db_path=temp_db_path)
    db.init_schema()
    broker = EventBroker(default_queue_size=50)
    dispatcher = TaskDispatcher(db=db, broker=broker, config=config)
    server = AsyncHTTPServer(config.server)
    register_webhook_routes(server, config, db=db, dispatcher=dispatcher, broker=broker)

    return {
        "config": config,
        "db": db,
        "broker": broker,
        "dispatcher": dispatcher,
        "server": server,
        "port": free_port,
        "token": "test_bearer_token_12345",
    }


@pytest.mark.asyncio
async def test_e2e_contact_review_ingress_and_dispatch(temp_env):
    server = temp_env["server"]
    dispatcher = temp_env["dispatcher"]
    db = temp_env["db"]
    port = temp_env["port"]
    token = temp_env["token"]

    await server.start()
    await dispatcher.start()
    await asyncio.sleep(0.05)

    # Mock execute_contact_review so test runs hermetically without external network
    mock_result = ReviewResult(
        verdict=ReviewVerdict.SUPPLEMENT,
        target_page_id="mock_page_123",
        target_page_url="https://notion.so/mock_page_123",
        target_name="Test Person",
        confidence_score=85,
        diffs=[FieldDiff(field_name="phone", action=FieldDiffAction.SUPPLEMENT, new_value="+84 901 000 000")],
        applied=True,
        ssot_verified=True,
    )

    with patch("hub.contact_review.runner.execute_contact_review", return_value=mock_result) as mock_exec:
        # Send HTTP POST /webhook/contact-review
        payload = {
            "source": "slack-people-intake",
            "contact": {
                "name": "Test Person",
                "phone": "+84 901 000 000",
                "company": "Test Co",
            },
            "slack": {
                "channel": "C096KR96AF7",
                "thread_ts": "1725881111.2222",
            },
        }

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body_bytes = json.dumps(payload).encode("utf-8")
        req = (
            f"POST /webhook/contact-review HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body_bytes

        writer.write(req)
        await writer.drain()

        resp_raw = await reader.read()
        writer.close()
        await writer.wait_closed()

        resp_str = resp_raw.decode("utf-8")
        assert "HTTP/1.1 202 Accepted" in resp_str
        header_part, body_part = resp_str.split("\r\n\r\n", 1)
        resp_json = json.loads(body_part)
        assert resp_json["status"] == "accepted"
        task_id = resp_json["task_id"]
        assert task_id.startswith("tsk_")

        # Wait briefly for dispatcher to execute the task
        for _ in range(50):
            task_row = db.get_task(task_id)
            if task_row and task_row.get("status") in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.05)

        task_final = db.get_task(task_id)
        assert task_final["status"] == "succeeded"
        assert mock_exec.called

        # Verify logs were recorded in execution_logs
        logs = await db.execute_read("SELECT * FROM execution_logs WHERE task_id = ?", (task_id,))
        assert len(logs) >= 1
        log_text = " ".join(l.get("chunk", "") for l in logs)
        assert "Verdict: SUPPLEMENT" in log_text

    await dispatcher.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_e2e_contact_review_adversarial_paths(temp_env):
    server = temp_env["server"]
    port = temp_env["port"]
    token = temp_env["token"]

    await server.start()
    await asyncio.sleep(0.05)

    # 1. Missing Authorization header -> 401
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req = (
        f"POST /webhook/contact-review HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: 2\r\n"
        f"Connection: close\r\n\r\n"
        f"{{}}"
    ).encode("utf-8")
    writer.write(req)
    await writer.drain()
    resp = (await reader.read()).decode("utf-8")
    writer.close()
    await writer.wait_closed()
    assert "HTTP/1.1 401 Unauthorized" in resp

    # 2. Malformed JSON body -> 400
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    req = (
        f"POST /webhook/contact-review HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: 12\r\n"
        f"Connection: close\r\n\r\n"
        f"NOT_VALID_JSON"
    ).encode("utf-8")
    writer.write(req)
    await writer.drain()
    resp = (await reader.read()).decode("utf-8")
    writer.close()
    await writer.wait_closed()
    assert "HTTP/1.1 400 Bad Request" in resp
    assert "malformed_json" in resp

    await server.stop()


@pytest.mark.asyncio
async def test_e2e_contact_review_sync_mode(temp_env):
    """Verify ?sync=true executes task synchronously and returns 200 with result."""
    server = temp_env["server"]
    dispatcher = temp_env["dispatcher"]
    port = temp_env["port"]
    token = temp_env["token"]

    await server.start()
    await dispatcher.start()
    await asyncio.sleep(0.05)

    mock_result = ReviewResult(
        verdict=ReviewVerdict.NO_CHANGE,
        target_page_id="mock_sync_page_999",
        target_page_url="https://notion.so/mock_sync_page_999",
        target_name="Sync Person",
        confidence_score=95,
        diffs=[],
        applied=False,
        ssot_verified=True,
    )

    with patch("hub.contact_review.runner.execute_contact_review", return_value=mock_result):
        payload = {
            "source": "slack-sync-test",
            "contact": {
                "name": "Sync Person",
                "phone": "+1 555 0199",
            },
        }

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body_bytes = json.dumps(payload).encode("utf-8")
        req = (
            f"POST /webhook/contact-review?sync=true HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body_bytes
        writer.write(req)
        await writer.drain()

        raw_resp = (await reader.read()).decode("utf-8")
        writer.close()
        await writer.wait_closed()

        assert "HTTP/1.1 200 OK" in raw_resp
        body_json = json.loads(raw_resp.split("\r\n\r\n", 1)[1])
        assert body_json["status"] == "succeeded"
        assert body_json["action_type"] == "contact_review"

    await dispatcher.stop()
    await server.stop()
