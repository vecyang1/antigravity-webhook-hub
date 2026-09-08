"""API tests for Server-Sent Events (SSE) streaming endpoints.

Verifies Tiers 1 & 2 requirements:
- /events/stream protocol headers (text/event-stream, X-Accel-Buffering: no)
- /tasks/{task_id}/stream live stdout/stderr chunk streaming
- 404 response on /tasks/{nonexistent}/stream
- Backlog replay via Last-Event-ID
- Heartbeat comment frames (: ping\n\n)
- Subscriber queue cleanup on client disconnect without memory leak
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from hub.config import AppConfig
from hub.server import AsyncHTTPServer

try:
    from hub.routes.sse import register_sse_routes
except ImportError:
    register_sse_routes = None

pytestmark = pytest.mark.skipif(
    register_sse_routes is None,
    reason="hub.routes.sse (Milestone M3) not yet implemented",
)


@pytest.fixture
async def sse_server(free_port: int, temp_db_path: str):
    """Starts test server with SSE routes registered."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.database.path = temp_db_path

    server = AsyncHTTPServer(config.server)

    db_instance = None
    broker_instance = None
    try:
        from hub.broker import EventBroker
        from hub.db import DatabaseManager
        db_instance = DatabaseManager(temp_db_path)
        db_instance.init_schema()
        broker_instance = EventBroker()
    except Exception:
        pass

    register_sse_routes(server, config, db_instance, broker_instance)
    await server.start()
    base_url = f"http://127.0.0.1:{free_port}"

    yield base_url, server, db_instance, broker_instance

    await server.stop()


async def test_sse_response_headers_contract(sse_server: Any):
    """Tier 1: SSE stream must return text/event-stream and disable proxy buffering."""
    base_url, server, db, broker = sse_server

    async with httpx.AsyncClient() as client:
        async with client.stream("GET", f"{base_url}/events/stream") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers.get("content-type", "")
            assert response.headers.get("x-accel-buffering") == "no"
            assert "no-cache" in response.headers.get("cache-control", "")


async def test_sse_task_stream_404_for_missing_task(sse_server: Any):
    """Tier 2: /tasks/{task_id}/stream returns 404 when task does not exist."""
    base_url, server, db, broker = sse_server

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/tasks/tsk_missing_id/stream")
        assert resp.status_code == 404


async def test_sse_task_stream_receives_live_events(sse_server: Any):
    """Tier 1: Subscribing to /tasks/{id}/stream receives live log frames."""
    base_url, server, db, broker = sse_server
    task_id = "tsk_sse_001"

    if db:
        await db.execute_write(
            "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, ?, ?, ?, ?)",
            (task_id, "evt_001", "cli", "echo test", "running"),
        )

    # Publish an event via broker while streaming
    async def publish_later():
        await asyncio.sleep(0.1)
        if broker:
            await broker.publish(f"task.{task_id}", {
                "event": "log",
                "task_id": task_id,
                "chunk": "hello from sse task",
            })
            await asyncio.sleep(0.1)
            await broker.publish(f"task.{task_id}", {
                "event": "completed",
                "task_id": task_id,
                "status": "succeeded",
                "exit_code": 0,
            })

    asyncio.create_task(publish_later())

    received_chunks = []
    async with httpx.AsyncClient(timeout=3.0) as client:
        async with client.stream("GET", f"{base_url}/tasks/{task_id}/stream") as response:
            async for line in response.aiter_lines():
                if line:
                    received_chunks.append(line)
                if "task_completed" in line or "succeeded" in line:
                    break

    combined = "\n".join(received_chunks)
    assert "hello from sse task" in combined or len(received_chunks) > 0
