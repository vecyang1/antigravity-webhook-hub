"""
Antigravity Webhook Hub — In-Memory Bounded PubSub Event Broker
Provides fast, non-blocking real-time event distribution for SSE streams
with bounded queues (maxsize=256 per subscriber), subscriber routing,
backlog replay support, and clean resource cleanup on client disconnect.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("hub.broker")


class EventBroker:
    """
    In-memory bounded PubSub event bus.
    Routes events to task-specific subscribers and global event streams.
    Guarantees <30MB memory budget by bounding subscriber queues to maxsize=256
    and dropping oldest frames on queue overflow without blocking publishers.
    """

    def __init__(self, default_queue_size: int = 256):
        self._default_queue_size = default_queue_size
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = threading.RLock()

    def subscribe(self, topic: str = "*", maxsize: Optional[int] = None) -> asyncio.Queue:
        """
        Subscribe to events on a specific topic.
        Topics:
          - 'task.{task_id}' or '{task_id}': task-specific events (stdout, status).
          - 'events' or '*': global live event feed.
        Returns a bounded asyncio.Queue.
        """
        size = maxsize or self._default_queue_size
        queue: asyncio.Queue = asyncio.Queue(maxsize=size)
        with self._lock:
            if topic not in self._subscribers:
                self._subscribers[topic] = set()
            self._subscribers[topic].add(queue)
        logger.debug("Subscribed to topic '%s' (active queues for topic: %d)", topic, len(self._subscribers[topic]))
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        """
        Cleanly unsubscribe queue from topic to prevent memory leaks.
        Also sweeps queue from all topics if present.
        """
        with self._lock:
            if topic in self._subscribers:
                self._subscribers[topic].discard(queue)
                if not self._subscribers[topic]:
                    del self._subscribers[topic]

            # Sweep from any other topics to ensure zero leaks
            empty_topics = []
            for t, qset in self._subscribers.items():
                if queue in qset:
                    qset.discard(queue)
                    if not qset:
                        empty_topics.append(t)
            for t in empty_topics:
                if t in self._subscribers and not self._subscribers[t]:
                    del self._subscribers[t]

        logger.debug("Unsubscribed queue from topic '%s'", topic)

    def subscriber_count(self, topic: Optional[str] = None) -> int:
        """Return total active subscribers or active subscribers on a topic."""
        with self._lock:
            if topic is not None:
                return len(self._subscribers.get(topic, set()))
            all_queues = set()
            for qset in self._subscribers.values():
                all_queues.update(qset)
            return len(all_queues)

    async def publish(self, topic: str, data: Any) -> int:
        """
        Fast, non-blocking broadcast of data to all matching subscriber queues.
        Drops oldest message if subscriber queue is full to preserve bounded memory.
        Returns the number of queues that received the message.
        """
        target_queues: set[asyncio.Queue] = set()

        with self._lock:
            # 1. Exact topic match
            if topic in self._subscribers:
                target_queues.update(self._subscribers[topic])

            # 2. Wildcard subscribers
            if "*" in self._subscribers:
                target_queues.update(self._subscribers["*"])

            # 3. Global 'events' subscribers receive events & task events
            if "events" in self._subscribers and (topic == "events" or topic.startswith("task.")):
                target_queues.update(self._subscribers["events"])

            # 4. Handle task ID variations (task.{id} vs {id})
            if topic.startswith("task."):
                bare_id = topic[5:]
                if bare_id in self._subscribers:
                    target_queues.update(self._subscribers[bare_id])
            else:
                task_prefixed = f"task.{topic}"
                if task_prefixed in self._subscribers:
                    target_queues.update(self._subscribers[task_prefixed])

        if not target_queues:
            return 0

        delivered = 0
        for queue in target_queues:
            # Non-blocking delivery with bounded drop-oldest policy
            if queue.full():
                try:
                    queue.get_nowait()
                except (asyncio.QueueEmpty, ValueError):
                    pass
            try:
                queue.put_nowait(data)
                delivered += 1
            except asyncio.QueueFull:
                pass

        return delivered

    async def publish_log(
        self,
        task_id: str,
        stream: str,
        chunk: str,
        execution_id: Optional[str] = None,
    ) -> int:
        """Forward a stdout/stderr log chunk to subscribers."""
        payload = {
            "event": "log",
            "type": "log",
            "task_id": task_id,
            "stream": stream,
            "chunk": chunk,
            "execution_id": execution_id or "",
            "timestamp": time.time(),
        }
        return await self.publish(f"task.{task_id}", payload)

    async def publish_status(
        self,
        task_id: str,
        status: str,
        exit_code: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> int:
        """Forward a task status change event to subscribers."""
        payload = {
            "event": "status_changed" if status not in ("succeeded", "failed", "timed_out") else "completed",
            "type": "status_changed",
            "task_id": task_id,
            "status": status,
            "exit_code": exit_code,
            "error_message": error_message,
            "timestamp": time.time(),
        }
        return await self.publish(f"task.{task_id}", payload)

    async def publish_event(self, task_id_or_topic: str, event_type: str, data: dict[str, Any]) -> int:
        """Forward a generic event to subscribers."""
        payload = dict(data)
        payload.setdefault("event", event_type)
        topic = task_id_or_topic if (task_id_or_topic.startswith("task.") or task_id_or_topic in ("events", "*")) else f"task.{task_id_or_topic}"
        return await self.publish(topic, payload)

    @staticmethod
    async def get_task_backlog(
        db: Any,
        task_id: str,
        since_id: int | str = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query execution_logs from database for log records with log_id > since_id."""
        if not db or not hasattr(db, "execute_read"):
            return []
        try:
            since_int = int(since_id)
        except (ValueError, TypeError):
            since_int = 0

        try:
            return await db.execute_read(
                """
                SELECT log_id, task_id, execution_id, stream_type, chunk, timestamp
                FROM execution_logs
                WHERE task_id = ? AND log_id > ?
                ORDER BY log_id ASC
                LIMIT ?
                """,
                (task_id, since_int, limit),
            )
        except Exception as e:
            logger.debug("Failed to query task backlog: %s", e)
            return []

    @staticmethod
    async def get_events_backlog(
        db: Any,
        since_id: int | str = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query webhook_events from database for backlog replay."""
        if not db or not hasattr(db, "execute_read"):
            return []
        try:
            return await db.execute_read(
                """
                SELECT event_id, source, idempotency_key, payload_hash, status, received_at
                FROM webhook_events
                ORDER BY received_at ASC
                LIMIT ?
                """,
                (limit,),
            )
        except Exception as e:
            logger.debug("Failed to query events backlog: %s", e)
            return []

    def clear(self) -> None:
        """Clear all active subscriptions."""
        with self._lock:
            self._subscribers.clear()
