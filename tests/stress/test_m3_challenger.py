"""
Adversarial & Empirical Challenge Suite for Milestone M3.
Authored by Challenger M3-2.

Challenges:
1. /healthz normal operation (200, DB connected, RSS < 30MB).
2. /healthz simulated DB failure returns 503 with degraded status.
3. /ready normal operation (200) and simulated DB failure returns 503.
4. /metrics Prometheus text format verification & headers.
5. /metrics JSON format (?format=json and Accept: application/json) verification.
6. /metrics task count accuracy under multi-status task distribution.
7. /tasks status filtering (?status=succeeded, ?status=failed, ?status=nonexistent).
8. /tasks pagination (limit, offset, boundary, adversarial inputs).
9. /tasks/{task_id} 404 for nonexistent tasks & SQL injection safety.
10. /tasks/{task_id} full execution logs (stdout AND stderr enrichment).
11. /tasks/{task_id}/stream backlog replay with since_id=0 (chronological order + live event).
12. /tasks/{task_id}/stream backlog replay with Last-Event-ID header.
13. /tasks/{task_id}/stream backlog replay for already-completed task.
14. /tasks/{task_id}/stream 404 for nonexistent task.
15. /tasks/{task_id}/stream subscriber cleanup behavior on client disconnect.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import httpx
import pytest

from hub.broker import EventBroker
from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.routes.observability import register_observability_routes
from hub.routes.sse import register_sse_routes
from hub.server import AsyncHTTPServer


@pytest.fixture
async def challenge_harness(free_port: int, temp_db_path: str):
    """Sets up a complete M3 server harness with DB and Broker."""
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.database.path = temp_db_path

    server = AsyncHTTPServer(config.server)
    db = DatabaseManager(temp_db_path)
    db.init_schema()
    broker = EventBroker()

    register_observability_routes(server, config, db, broker)
    register_sse_routes(server, config, db, broker)
    await server.start()

    base_url = f"http://127.0.0.1:{free_port}"

    yield {
        "base_url": base_url,
        "server": server,
        "db": db,
        "broker": broker,
        "config": config,
    }

    await server.stop()
    db.close()


# ============================================================================
# 1. Healthz & Ready Probes (Normal & Simulated Failure)
# ============================================================================

async def test_challenge_healthz_normal_operation(challenge_harness: dict[str, Any]):
    """Verify /healthz returns 200 with DB status and memory RSS < 30MB."""
    base_url = challenge_harness["base_url"]

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/healthz")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "ok"
        assert data["db"] == "ok"
        assert data["database"]["status"] in ("connected", "ok")
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] >= 0

        # Memory verification (<30MB constraint)
        assert "system" in data
        sys_data = data["system"]
        assert sys_data["memory_rss_mb"] < 30.0, f"RSS must be < 30MB, got {sys_data['memory_rss_mb']}MB"
        assert sys_data["memory_healthy"] is True
        assert sys_data["memory_budget_mb"] == 30.0


async def test_challenge_healthz_simulated_db_failure(challenge_harness: dict[str, Any]):
    """Verify simulated DB failure causes /healthz to return 503 with degraded status."""
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]

    # Simulate DB failure by corrupting / closing underlying connection
    db.close()

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/healthz")
        assert resp.status_code == 503, f"Expected 503 Service Unavailable, got {resp.status_code}"
        data = resp.json()

        assert data["status"] == "degraded"
        assert data["db"] == "error"
        assert "error" in data["database"]["status"].lower()


async def test_challenge_ready_normal_and_failure(challenge_harness: dict[str, Any]):
    """Verify /ready returns 200 normally, and 503 when DB is unresponsive."""
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]

    # 1. Normal state: 200 OK
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

        # 2. Simulated DB lock / closed failure: 503
        db.close()
        resp_fail = await client.get(f"{base_url}/ready")
        assert resp_fail.status_code == 503
        assert resp_fail.json()["status"] == "not_ready"


# ============================================================================
# 2. Metrics (Prometheus Text & JSON Formats, Accuracy)
# ============================================================================

async def test_challenge_metrics_prometheus_text_format(challenge_harness: dict[str, Any]):
    """Verify /metrics returns valid Prometheus exposition text format by default."""
    base_url = challenge_harness["base_url"]

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")

        text = resp.text
        assert "webhook_hub_uptime_seconds" in text
        assert "webhook_hub_process_memory_rss_bytes" in text
        assert "webhook_hub_http_requests_total" in text
        assert "webhook_hub_active_connections" in text
        assert "webhook_hub_tasks_total" in text


async def test_challenge_metrics_json_format_parity(challenge_harness: dict[str, Any]):
    """Verify /metrics?format=json and Accept: application/json return valid JSON metrics."""
    base_url = challenge_harness["base_url"]

    async with httpx.AsyncClient() as client:
        # Test 1: Query param ?format=json
        resp1 = await client.get(f"{base_url}/metrics?format=json")
        assert resp1.status_code == 200
        assert "application/json" in resp1.headers.get("content-type", "")
        data1 = resp1.json()

        assert "uptime_seconds" in data1
        assert "memory_rss_bytes" in data1
        assert "requests_total" in data1
        assert "active_connections" in data1
        assert "tasks" in data1
        assert "by_status" in data1["tasks"]

        # Test 2: Accept: application/json header
        resp2 = await client.get(f"{base_url}/metrics", headers={"Accept": "application/json"})
        assert resp2.status_code == 200
        assert "application/json" in resp2.headers.get("content-type", "")
        data2 = resp2.json()
        assert data2["tasks"]["total"] == data1["tasks"]["total"]


async def test_challenge_metrics_multi_status_accuracy(challenge_harness: dict[str, Any]):
    """Verify task distribution metrics reflect actual DB states accurately."""
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]

    # Seed events
    await db.execute_write(
        """INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status)
           VALUES ('evt_m1', 'src', '', '{}', '{}', 'POST', '/webhook', 'received'),
                  ('evt_m2', 'src', '', '{}', '{}', 'POST', '/webhook', 'received'),
                  ('evt_m3', 'src', '', '{}', '{}', 'POST', '/webhook', 'received'),
                  ('evt_m4', 'src', '', '{}', '{}', 'POST', '/webhook', 'received')"""
    )
    # Seed tasks: 2 succeeded, 1 failed, 1 queued
    await db.execute_write(
        """INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES
           ('tsk_m1', 'evt_m1', 'cli', 'cmd1', 'succeeded'),
           ('tsk_m2', 'evt_m2', 'cli', 'cmd2', 'succeeded'),
           ('tsk_m3', 'evt_m3', 'cli', 'cmd3', 'failed'),
           ('tsk_m4', 'evt_m4', 'cli', 'cmd4', 'queued')"""
    )

    async with httpx.AsyncClient() as client:
        # Check JSON metrics
        resp_json = await client.get(f"{base_url}/metrics?format=json")
        data = resp_json.json()
        by_status = data["tasks"]["by_status"]

        assert by_status["succeeded"] == 2
        assert by_status["failed"] == 1
        assert by_status["queued"] == 1
        assert data["tasks"]["total"] == 4

        # Check Prometheus text metrics
        resp_text = await client.get(f"{base_url}/metrics")
        text = resp_text.text
        assert 'webhook_hub_tasks_total{status="succeeded"} 2' in text
        assert 'webhook_hub_tasks_total{status="failed"} 1' in text
        assert 'webhook_hub_tasks_total{status="queued"} 1' in text


# ============================================================================
# 3. Tasks Pagination & Filtering
# ============================================================================

async def test_challenge_tasks_status_filtering(challenge_harness: dict[str, Any]):
    """Verify GET /tasks correctly filters by ?status=succeeded and ?status=failed."""
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]

    # Seed 10 tasks
    events_tuples = [(f"evt_f_{i}", "src", "", "{}", "{}", "POST", "/webhook", "received") for i in range(10)]
    for e in events_tuples:
        await db.execute_write(
            "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            e,
        )

    # 6 succeeded, 4 failed
    for i in range(6):
        await db.execute_write(
            "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, ?, 'cli', 'echo', 'succeeded')",
            (f"tsk_succ_{i}", f"evt_f_{i}"),
        )
    for i in range(6, 10):
        await db.execute_write(
            "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, ?, 'cli', 'echo', 'failed')",
            (f"tsk_fail_{i}", f"evt_f_{i}"),
        )

    async with httpx.AsyncClient() as client:
        # 1. Filter ?status=succeeded
        r_succ = await client.get(f"{base_url}/tasks?status=succeeded")
        assert r_succ.status_code == 200
        d_succ = r_succ.json()
        assert d_succ["total"] == 6
        assert len(d_succ["tasks"]) == 6
        assert all(t["status"] == "succeeded" for t in d_succ["tasks"])

        # 2. Filter ?status=failed
        r_fail = await client.get(f"{base_url}/tasks?status=failed")
        assert r_fail.status_code == 200
        d_fail = r_fail.json()
        assert d_fail["total"] == 4
        assert len(d_fail["tasks"]) == 4
        assert all(t["status"] == "failed" for t in d_fail["tasks"])

        # 3. Filter nonexistent status
        r_none = await client.get(f"{base_url}/tasks?status=nonexistent_status")
        assert r_none.status_code == 200
        d_none = r_none.json()
        assert d_none["total"] == 0
        assert len(d_none["tasks"]) == 0


async def test_challenge_tasks_pagination_and_boundaries(challenge_harness: dict[str, Any]):
    """Verify limit/offset pagination and adversarial input sanitization."""
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]

    # Seed 12 tasks
    for i in range(12):
        await db.execute_write(
            "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) VALUES (?, 'src', '', '{}', '{}', 'POST', '/webhook', 'received')",
            (f"evt_p_{i}",),
        )
        await db.execute_write(
            "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, ?, 'cli', 'cmd', 'queued')",
            (f"tsk_p_{i:02d}", f"evt_p_{i}"),
        )

    async with httpx.AsyncClient() as client:
        # Page 1: limit 5, offset 0
        p1 = await client.get(f"{base_url}/tasks?limit=5&offset=0")
        assert p1.status_code == 200
        d1 = p1.json()
        assert d1["total"] == 12
        assert d1["count"] == 5
        assert len(d1["tasks"]) == 5
        ids_p1 = [t["task_id"] for t in d1["tasks"]]

        # Page 2: limit 5, offset 5
        p2 = await client.get(f"{base_url}/tasks?limit=5&offset=5")
        assert p2.status_code == 200
        d2 = p2.json()
        assert d2["count"] == 5
        ids_p2 = [t["task_id"] for t in d2["tasks"]]

        # Verify no overlap between page 1 and page 2
        assert len(set(ids_p1).intersection(set(ids_p2))) == 0

        # Page 3: limit 5, offset 10 (should return 2 tasks)
        p3 = await client.get(f"{base_url}/tasks?limit=5&offset=10")
        assert p3.status_code == 200
        d3 = p3.json()
        assert d3["count"] == 2

        # Page beyond total
        p4 = await client.get(f"{base_url}/tasks?limit=5&offset=20")
        assert p4.status_code == 200
        d4 = p4.json()
        assert d4["count"] == 0
        assert len(d4["tasks"]) == 0

        # Adversarial inputs: negative limit & offset, non-numeric strings
        p_adv1 = await client.get(f"{base_url}/tasks?limit=-10&offset=-5")
        assert p_adv1.status_code == 200
        assert p_adv1.json()["limit"] >= 1
        assert p_adv1.json()["offset"] >= 0

        p_adv2 = await client.get(f"{base_url}/tasks?limit=invalid&offset=bad")
        assert p_adv2.status_code == 200
        assert p_adv2.json()["limit"] == 50


# ============================================================================
# 4. Task Detail & Log Enrichment (/tasks/{task_id})
# ============================================================================

async def test_challenge_task_detail_404_and_sanitization(challenge_harness: dict[str, Any]):
    """Verify 404 for nonexistent tasks and path injection resilience."""
    base_url = challenge_harness["base_url"]

    async with httpx.AsyncClient() as client:
        # Standard nonexistent ID
        r1 = await client.get(f"{base_url}/tasks/tsk_does_not_exist_404")
        assert r1.status_code == 404
        data = r1.json()
        assert "not_found" in data.get("error", "").lower() or "not found" in data.get("message", "").lower()

        # Path traversal / SQL injection attempt
        r2 = await client.get(f"{base_url}/tasks/'%20OR%20'1'='1")
        assert r2.status_code == 404


async def test_challenge_task_detail_full_execution_logs_stdout_and_stderr(challenge_harness: dict[str, Any]):
    """
    Verify /tasks/{task_id} returns full execution logs (stdout AND stderr).
    Challenges whether stderr logs are properly preserved when stdout is present.
    """
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]
    task_id = "tsk_both_streams"

    # Insert event & task
    await db.execute_write(
        "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) VALUES ('evt_streams', 'src', '', '{}', '{}', 'POST', '/webhook', 'received')"
    )
    await db.execute_write(
        "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, 'evt_streams', 'cli', 'run.sh', 'succeeded')",
        (task_id,),
    )

    # Insert stdout chunk and stderr chunk into execution_logs
    db.insert_execution_log(task_id, "stdout", "standard output message")
    db.insert_execution_log(task_id, "stderr", "critical error or warning on stderr")

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()

        assert data["task_id"] == task_id
        # Check stdout
        assert "stdout" in data
        assert "standard output message" in data["stdout"]

        # Check stderr — this directly tests whether stderr is dropped when stdout exists!
        assert "stderr" in data, "Task detail must include stderr field"
        assert "critical error or warning on stderr" in data["stderr"], "Task detail must include stderr execution logs"


# ============================================================================
# 5. Backlog Replay (/tasks/{task_id}/stream)
# ============================================================================

async def test_challenge_backlog_replay_chronological_order_and_live_events(challenge_harness: dict[str, Any]):
    """
    Verify /tasks/{task_id}/stream?since_id=0 replays historical execution logs
    in strict chronological order before receiving live event stream.
    """
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]
    broker: EventBroker = challenge_harness["broker"]
    task_id = "tsk_replay_full"

    await db.execute_write(
        "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) VALUES ('evt_r1', 'src', '', '{}', '{}', 'POST', '/webhook', 'received')"
    )
    await db.execute_write(
        "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, 'evt_r1', 'cli', 'multi_step', 'running')",
        (task_id,),
    )

    # Insert 4 historical logs
    expected_chunks = [f"Step {i}: executed successfully" for i in range(1, 5)]
    for chunk in expected_chunks:
        db.insert_execution_log(task_id, "stdout", chunk)

    received_log_chunks: list[str] = []
    received_event_ids: list[str] = []
    live_received = False

    async def read_stream():
        nonlocal live_received
        async with httpx.AsyncClient(timeout=5.0) as client:
            async with client.stream("GET", f"{base_url}/tasks/{task_id}/stream?since_id=0") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("id: "):
                        received_event_ids.append(line.replace("id: ", "").strip())
                    elif line.startswith("data: "):
                        raw_data = line.replace("data: ", "").strip()
                        try:
                            data_obj = json.loads(raw_data)
                            chunk = data_obj.get("chunk")
                            if chunk:
                                received_log_chunks.append(chunk)
                            if chunk == "Live step 5":
                                live_received = True
                                break
                        except Exception:
                            pass

    async def emit_live_later():
        await asyncio.sleep(0.15)
        # Emit live log
        await broker.publish_log(task_id, "stdout", "Live step 5")
        await asyncio.sleep(0.05)
        await broker.publish_status(task_id, "succeeded", exit_code=0)

    await asyncio.gather(read_stream(), emit_live_later())

    # Verify chronological replay: historical steps 1..4 first, then live step 5
    assert len(received_log_chunks) >= 5
    assert received_log_chunks[:4] == expected_chunks, "Historical logs must replay in chronological order"
    assert received_log_chunks[4] == "Live step 5", "Live log must arrive after historical backlog"
    assert live_received is True


async def test_challenge_backlog_replay_with_last_event_id_header(challenge_harness: dict[str, Any]):
    """
    Verify reconnecting with Last-Event-ID header skips already acknowledged logs
    and only streams subsequent logs.
    """
    base_url = challenge_harness["base_url"]
    db: DatabaseManager = challenge_harness["db"]
    task_id = "tsk_resume_test"

    await db.execute_write(
        "INSERT INTO webhook_events (event_id, source, payload_hash, headers_json, raw_payload, method, path, status) VALUES ('evt_res', 'src', '', '{}', '{}', 'POST', '/webhook', 'received')"
    )
    await db.execute_write(
        "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, 'evt_res', 'cli', 'batch', 'succeeded')",
        (task_id,),
    )

    # Insert 5 logs (ids 1..5)
    for i in range(1, 6):
        db.insert_execution_log(task_id, "stdout", f"Batch chunk {i}")

    replayed_chunks = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        # Client reconnects having previously seen event ID 3
        headers = {"Last-Event-ID": "3"}
        async with client.stream("GET", f"{base_url}/tasks/{task_id}/stream", headers=headers) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    try:
                        d = json.loads(line.replace("data: ", "").strip())
                        if "chunk" in d:
                            replayed_chunks.append(d["chunk"])
                    except Exception:
                        pass

    # Must only receive chunks 4 and 5 (skipping 1, 2, 3)
    assert replayed_chunks == ["Batch chunk 4", "Batch chunk 5"]


async def test_challenge_task_stream_nonexistent_returns_404(challenge_harness: dict[str, Any]):
    """Verify /tasks/{task_id}/stream returns 404 immediately for nonexistent task."""
    base_url = challenge_harness["base_url"]

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base_url}/tasks/tsk_nonexistent_stream_id/stream")
        assert resp.status_code == 404
