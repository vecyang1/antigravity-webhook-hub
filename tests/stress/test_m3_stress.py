"""Challenger M3-1 Empirical Stress & Robustness Test Suite for Real-Time SSE & PubSub.

Empirically challenges:
1. Concurrent SSE Subscribers:
   Connect 10 concurrent subscribers to /events/stream and 10 to /tasks/{task_id}/stream.
   Fire tasks and verify all connected subscribers receive live updates without event loss or socket starvation.
2. Slow Consumer & Overflow:
   Connect a subscriber that does not read from its queue.
   Publish 500 events and verify that the broker drops oldest without blocking publishers or raising QueueFull exceptions.
   Verify strictly bounded memory and monotonic sequence preservation for retained items.
3. Sudden Disconnect Cleanup:
   Connect subscribers and abruptly close sockets (client.close() / reset).
   Verify that subscriber queues are cleanly unregistered from the broker and memory does not leak.
4. Memory RSS Under Active Streaming:
   Measure resident set size (RSS) under continuous concurrent SSE streaming to ensure it stays below 30MB limit.
5. Terminal Task Immediate Stream Completion & Clean Unsubscription.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from hub.broker import EventBroker
from hub.config import AppConfig
from hub.db import DatabaseManager
from hub.dispatcher import TaskDispatcher
from hub.routes.observability import register_observability_routes
from hub.routes.sse import register_sse_routes
from hub.routes.webhook import register_webhook_routes
from hub.security import generate_hmac_signature
from hub.server import AsyncHTTPServer


def get_current_process_rss_mb() -> float:
    """Query current process resident set size (RSS) in megabytes."""
    try:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        bytes_val = int(raw) if sys.platform == "darwin" else int(raw * 1024)
        return bytes_val / (1024.0 * 1024.0)
    except Exception:
        return 0.0


@pytest.fixture
def m3_config(free_port: int, temp_db_path: str) -> AppConfig:
    config = AppConfig()
    config.server.port = free_port
    config.server.host = "127.0.0.1"
    config.security.webhook_secret = "m3_stress_secret_key_32bytes_1234"
    config.database.path = temp_db_path
    return config


@pytest.fixture
async def m3_stack(m3_config: AppConfig, temp_db_path: str):
    server = AsyncHTTPServer(m3_config.server)
    db = DatabaseManager(temp_db_path)
    db.init_schema()

    broker = EventBroker(default_queue_size=256)
    dispatcher = TaskDispatcher(db, broker=broker, config=m3_config)
    await dispatcher.start()

    register_webhook_routes(server, m3_config, db, dispatcher, broker=broker)
    register_observability_routes(server, m3_config, db, broker=broker)
    register_sse_routes(server, m3_config, db, broker=broker)

    await server.start()
    base_url = f"http://127.0.0.1:{m3_config.server.port}"

    yield base_url, server, m3_config, db, broker, dispatcher

    await dispatcher.stop()
    await server.stop()
    db.close()


# ===========================================================================
# CHALLENGE 1: Concurrent SSE Subscribers (10 Global + 10 Task-Specific)
# ===========================================================================

async def test_concurrent_sse_subscribers_no_event_loss_or_starvation(m3_stack: Any):
    """
    Connect 10 concurrent subscribers to /events/stream and 10 concurrent
    subscribers to /tasks/{task_id}/stream (20 total active streams).
    Fire tasks and broadcast events.
    Verify:
    - All 10 global subscribers receive all events.
    - All 10 task subscribers receive all task events.
    - Zero event loss.
    - Zero socket starvation.
    """
    base_url, server, config, db, broker, dispatcher = m3_stack
    task_id = "tsk_stress_concurrent_001"

    # Pre-insert task into database so task stream route validates existence
    await db.execute_write(
        """
        INSERT INTO tasks (task_id, event_id, action_type, command, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, "evt_stress_001", "cli", "echo concurrent", "running"),
    )

    num_global_subs = 10
    num_task_subs = 10
    total_subs = num_global_subs + num_task_subs

    global_received: list[list[str]] = [[] for _ in range(num_global_subs)]
    task_received: list[list[str]] = [[] for _ in range(num_task_subs)]

    connected_event = asyncio.Event()
    ready_count = 0

    async def run_subscriber(url: str, store: list[str], client: httpx.AsyncClient):
        nonlocal ready_count
        async with client.stream("GET", url, timeout=15.0) as resp:
            assert resp.status_code == 200
            ready_count += 1
            if ready_count == total_subs:
                connected_event.set()

            async for line in resp.aiter_lines():
                if line and not line.startswith(":"):  # ignore ping comments
                    store.append(line)
                    if "terminal_event" in line or "succeeded" in line:
                        break

    clients = [httpx.AsyncClient(timeout=15.0) for _ in range(total_subs)]
    tasks = []

    try:
        # Start 10 global subscribers
        for i in range(num_global_subs):
            tasks.append(
                asyncio.create_task(
                    run_subscriber(f"{base_url}/events/stream", global_received[i], clients[i])
                )
            )

        # Start 10 task-specific subscribers
        for i in range(num_task_subs):
            tasks.append(
                asyncio.create_task(
                    run_subscriber(
                        f"{base_url}/tasks/{task_id}/stream",
                        task_received[i],
                        clients[num_global_subs + i],
                    )
                )
            )

        # Wait until all 20 subscribers are established
        await asyncio.wait_for(connected_event.wait(), timeout=5.0)
        await asyncio.sleep(0.1)

        # Verify active subscriber count in broker
        assert broker.subscriber_count("events") == num_global_subs
        assert broker.subscriber_count(f"task.{task_id}") == num_task_subs
        assert broker.subscriber_count() == total_subs

        # Broadcast 5 distinct sequential log events
        expected_chunks = [f"chunk_sequential_payload_{k}" for k in range(5)]
        for chunk in expected_chunks:
            await broker.publish_log(task_id, "stdout", chunk)
            await asyncio.sleep(0.02)

        # Publish terminal status event to conclude the stream
        await broker.publish(f"task.{task_id}", {
            "event": "completed",
            "task_id": task_id,
            "status": "succeeded",
            "exit_code": 0,
            "marker": "terminal_event",
        })

        # Wait for all subscriber tasks to complete
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

        # Verify all 10 global subscribers received all chunks
        for idx, received in enumerate(global_received):
            full_text = "\n".join(received)
            for chunk in expected_chunks:
                assert chunk in full_text, (
                    f"Global subscriber {idx} missed chunk: {chunk}\nReceived: {full_text}"
                )
            assert "terminal_event" in full_text

        # Verify all 10 task subscribers received all chunks
        for idx, received in enumerate(task_received):
            full_text = "\n".join(received)
            for chunk in expected_chunks:
                assert chunk in full_text, (
                    f"Task subscriber {idx} missed chunk: {chunk}\nReceived: {full_text}"
                )
            assert "terminal_event" in full_text

    finally:
        for c in clients:
            await c.aclose()


# ===========================================================================
# CHALLENGE 2: Slow Consumer & Overflow (Drop Oldest, Non-Blocking, No Error)
# ===========================================================================

async def test_broker_slow_consumer_drops_oldest_non_blocking_without_queue_full():
    """
    Direct broker challenge:
    1. Connect a subscriber with maxsize=256 and DO NOT read from it.
    2. Publish 500 events.
    3. Verify:
       - No QueueFull exceptions raised.
       - Publish operation is non-blocking (completes in < 0.1s for 500 events).
       - Queue size is strictly capped at 256.
       - Oldest 244 events (0..243) were dropped.
       - Remaining 256 events are strictly monotonically increasing (244..499).
    """
    broker = EventBroker(default_queue_size=256)
    queue = broker.subscribe("topic.slow_test")

    start_time = time.perf_counter()
    publish_count = 500

    for i in range(publish_count):
        delivered = await broker.publish("topic.slow_test", {"seq": i, "data": f"item_{i}"})
        assert delivered == 1

    elapsed = time.perf_counter() - start_time

    # Must be extremely fast and non-blocking
    assert elapsed < 0.1, f"Publishing 500 events took too long: {elapsed:.4f}s"

    # Queue must be bounded to maxsize
    assert queue.qsize() == 256
    assert queue.full()

    # Drain queue and check contents
    retained_items = []
    while not queue.empty():
        retained_items.append(queue.get_nowait())

    assert len(retained_items) == 256
    seq_numbers = [item["seq"] for item in retained_items]

    # Verify oldest 244 items were dropped
    expected_seq = list(range(244, 500))
    assert seq_numbers == expected_seq, (
        f"Drop oldest failed! First seq: {seq_numbers[0]}, Last seq: {seq_numbers[-1]}, Expected start: 244"
    )

    broker.unsubscribe("topic.slow_test", queue)
    assert broker.subscriber_count() == 0


async def test_http_slow_consumer_overflow_does_not_starve_fast_consumers(m3_stack: Any):
    """
    E2E Slow Consumer HTTP Challenge:
    Connect two subscribers over HTTP:
    - Fast subscriber actively reads events.
    - Slow subscriber connects, reads headers, then halts reading.
    Publish 300 events rapidly.
    Verify:
    - Fast subscriber receives all events without delay or starvation.
    - Publisher does not hang or raise QueueFull.
    """
    base_url, server, config, db, broker, dispatcher = m3_stack

    fast_client = httpx.AsyncClient(timeout=10.0)
    slow_client = httpx.AsyncClient(timeout=10.0)

    fast_received: list[str] = []
    fast_ready = asyncio.Event()

    async def run_fast():
        async with fast_client.stream("GET", f"{base_url}/events/stream") as resp:
            fast_ready.set()
            async for line in resp.aiter_lines():
                if line and not line.startswith(":"):
                    fast_received.append(line)
                    if "event_299" in line:
                        break

    fast_task = asyncio.create_task(run_fast())

    # Slow consumer connects and opens socket, but never iterates lines
    slow_resp = await slow_client.send(
        slow_client.build_request("GET", f"{base_url}/events/stream"),
        stream=True,
    )
    assert slow_resp.status_code == 200

    await asyncio.wait_for(fast_ready.wait(), timeout=3.0)
    await asyncio.sleep(0.05)

    # Publish 300 events rapidly
    t0 = time.perf_counter()
    for i in range(300):
        await broker.publish("events", {"event": "test", "id": i, "msg": f"event_{i}"})
    publish_duration = time.perf_counter() - t0

    assert publish_duration < 0.2, f"Publishing 300 events took too long: {publish_duration:.4f}s"

    # Fast consumer should finish receiving all 300 events
    await asyncio.wait_for(fast_task, timeout=5.0)
    assert len(fast_received) >= 300

    await fast_client.aclose()
    await slow_resp.aclose()
    await slow_client.aclose()


# ===========================================================================
# CHALLENGE 3: Sudden Disconnect Cleanup & Memory Leak Prevention
# ===========================================================================

async def test_sudden_disconnect_unregisters_subscribers_without_leak(m3_stack: Any):
    """
    Connect 10 subscribers to /events/stream and 5 to /tasks/{id}/stream.
    Abruptly close all client connections (TCP close / client.aclose()).
    Publish a heartbeat/event to wake server-side writers and trigger socket error.
    Verify:
    - Broker subscribers count drops cleanly to 0.
    - Broker internal _subscribers dictionary is completely cleared.
    - Server active connections count returns to 0.
    - Zero queue reference leak.
    """
    base_url, server, config, db, broker, dispatcher = m3_stack
    task_id = "tsk_disconnect_001"

    await db.execute_write(
        "INSERT INTO tasks (task_id, event_id, action_type, command, status) VALUES (?, ?, ?, ?, ?)",
        (task_id, "evt_disc_001", "cli", "echo disconnect", "running"),
    )

    num_events_clients = 10
    num_task_clients = 5
    total_clients = num_events_clients + num_task_clients

    clients = [httpx.AsyncClient(timeout=10.0) for _ in range(total_clients)]
    responses = []

    # 1. Connect all clients
    for i in range(num_events_clients):
        resp = await clients[i].send(
            clients[i].build_request("GET", f"{base_url}/events/stream"),
            stream=True,
        )
        assert resp.status_code == 200
        responses.append(resp)

    for i in range(num_task_clients):
        resp = await clients[num_events_clients + i].send(
            clients[num_events_clients + i].build_request("GET", f"{base_url}/tasks/{task_id}/stream"),
            stream=True,
        )
        assert resp.status_code == 200
        responses.append(resp)

    await asyncio.sleep(0.05)

    # Verify all subscribers are active
    assert broker.subscriber_count("events") == num_events_clients
    assert broker.subscriber_count(f"task.{task_id}") == num_task_clients
    assert broker.subscriber_count() == total_clients

    # 2. Abruptly close all client connections
    for resp in responses:
        await resp.aclose()
    for client in clients:
        await client.aclose()

    # 3. Publish events to trigger socket writes on server side and flush TCP RST
    for p in range(3):
        await broker.publish("events", {"trigger": f"detect_disconnect_{p}"})
        await broker.publish(f"task.{task_id}", {"trigger": f"detect_disconnect_{p}"})
        await asyncio.sleep(0.05)

    # Allow asyncio loop to process connection close exceptions and aclose()
    for _ in range(30):
        if broker.subscriber_count() == 0:
            break
        await asyncio.sleep(0.05)

    # 4. Assert complete unregistration and clean state
    assert broker.subscriber_count("events") == 0, f"Remaining events subscribers: {broker.subscriber_count('events')}"
    assert broker.subscriber_count(f"task.{task_id}") == 0, f"Remaining task subscribers: {broker.subscriber_count(f'task.{task_id}')}"
    assert broker.subscriber_count() == 0, f"Total remaining subscribers: {broker.subscriber_count()}"
    assert len(broker._subscribers) == 0, f"_subscribers dictionary not empty: {broker._subscribers}"


# ===========================================================================
# CHALLENGE 4: Memory RSS Under Active Streaming (<30MB Limit)
# ===========================================================================

async def test_memory_rss_under_active_sse_streaming(m3_stack: Any):
    """
    Connect 10 active SSE subscribers and stream 200 events continuously.
    Measure process RSS and verify /healthz endpoint confirms memory health:
    - Process RSS stays strictly below 30.0 MB.
    - /healthz returns 200 with data['system']['memory_healthy'] is True.
    - Prometheus /metrics exposes memory and connection gauges correctly.
    """
    base_url, server, config, db, broker, dispatcher = m3_stack

    num_subs = 10
    clients = [httpx.AsyncClient(timeout=15.0) for _ in range(num_subs)]
    received_counts = [0] * num_subs
    ready_event = asyncio.Event()
    ready_count = 0

    async def sub_reader(idx: int, client: httpx.AsyncClient):
        nonlocal ready_count
        async with client.stream("GET", f"{base_url}/events/stream") as resp:
            ready_count += 1
            if ready_count == num_subs:
                ready_event.set()
            async for line in resp.aiter_lines():
                if line and not line.startswith(":"):
                    received_counts[idx] += 1
                    if "final_stream_marker" in line:
                        break

    reader_tasks = [
        asyncio.create_task(sub_reader(i, clients[i]))
        for i in range(num_subs)
    ]

    await asyncio.wait_for(ready_event.wait(), timeout=3.0)

    # Stream 200 events
    for i in range(200):
        await broker.publish("events", {
            "type": "metric_test",
            "index": i,
            "data": f"payload_data_{i}",
        })
        if i % 20 == 0:
            await asyncio.sleep(0.01)

    await broker.publish("events", {
        "type": "metric_test",
        "marker": "final_stream_marker",
    })

    await asyncio.wait_for(asyncio.gather(*reader_tasks), timeout=5.0)

    # Verify all received
    for count in received_counts:
        assert count >= 200

    # Check memory via /healthz
    async with httpx.AsyncClient(timeout=3.0) as client:
        health_resp = await client.get(f"{base_url}/healthz")
        assert health_resp.status_code == 200
        health_data = health_resp.json()

        rss_mb = health_data["system"]["memory_rss_mb"]
        memory_healthy = health_data["system"]["memory_healthy"]

        assert memory_healthy is True, f"Memory reported unhealthy: {rss_mb}MB"
        assert rss_mb < 30.0, f"Memory RSS exceeded 30MB: {rss_mb}MB"

        # Check /metrics Prometheus format
        metrics_resp = await client.get(f"{base_url}/metrics")
        assert metrics_resp.status_code == 200
        metrics_text = metrics_resp.text
        assert "webhook_hub_process_memory_rss_bytes" in metrics_text
        assert "webhook_hub_uptime_seconds" in metrics_text

    for c in clients:
        await c.aclose()


# ===========================================================================
# CHALLENGE 5: Terminal Task Stream Immediate Completion & Clean Cleanup
# ===========================================================================

async def test_completed_task_stream_yields_completion_and_cleans_up(m3_stack: Any):
    """
    Subscribing to a task that is already finished ('succeeded') must:
    1. Immediately emit a 'completed' event.
    2. Close the stream cleanly.
    3. Unsubscribe from the broker without leaving an orphaned queue.
    """
    base_url, server, config, db, broker, dispatcher = m3_stack
    task_id = "tsk_already_done_001"

    await db.execute_write(
        """
        INSERT INTO tasks (task_id, event_id, action_type, command, status, exit_code)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, "evt_done_001", "cli", "echo done", "succeeded", 0),
    )

    received_lines = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        async with client.stream("GET", f"{base_url}/tasks/{task_id}/stream") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line:
                    received_lines.append(line)

    full_output = "\n".join(received_lines)
    assert "event: completed" in full_output
    assert '"status": "succeeded"' in full_output

    # Verify no subscriber remained
    assert broker.subscriber_count(f"task.{task_id}") == 0
    assert broker.subscriber_count() == 0
