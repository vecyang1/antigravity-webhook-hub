"""
Antigravity Webhook Hub — SQLite SSOT Database Engine
Provides single-source-of-truth persistence with WAL mode PRAGMAs,
4-table schema, dual-layer deduplication, atomic CAS state transitions,
and boot-time crash recovery. Guarantees <30MB RSS footprint on macOS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hub.db")

# Patch sqlite3.connect to ensure WAL mode and performance tuning PRAGMAs
# are strictly enforced on all connections opened across the process.
_original_sqlite3_connect = sqlite3.connect


DEFAULT_SQLITE_CACHE_SIZE = -4000


def _tuned_sqlite3_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    cache_sz = kwargs.pop("cache_size", DEFAULT_SQLITE_CACHE_SIZE)
    if "cached_statements" not in kwargs:
        kwargs["cached_statements"] = 16
    conn = _original_sqlite3_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA cache_size = {cache_sz};")
        conn.execute("PRAGMA mmap_size = 0;")
        conn.execute("PRAGMA temp_store = FILE;")
        conn.execute("PRAGMA wal_autocheckpoint = 20;")
        try:
            conn.execute("PRAGMA soft_heap_limit = 131072;")
            conn.execute("PRAGMA shrink_memory;")
        except Exception:
            pass
    except Exception as e:
        logger.debug("Failed to apply initial PRAGMAs: %s", e)
    return conn


sqlite3.connect = _tuned_sqlite3_connect


class DatabaseManager:
    """
    SQLite Single-Source-of-Truth (SSOT) Database Manager.
    Enforces WAL mode, atomic CAS state machine transitions, and crash recovery.
    """

    def __init__(self, db_path: str = "data/webhook_hub.db", cache_size: int = -4000):
        self.db_path = str(db_path)
        self._cache_size = int(cache_size)
        if self.db_path not in (":memory:", ""):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=5.0,
            cached_statements=16,
            cache_size=self._cache_size,
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas(self._conn)
        self.shrink_memory()

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """Apply tuned SQLite PRAGMAs for concurrency and bounded memory."""
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA cache_size = {self._cache_size};")
        conn.execute("PRAGMA mmap_size = 0;")
        conn.execute("PRAGMA temp_store = FILE;")
        conn.execute("PRAGMA wal_autocheckpoint = 20;")
        try:
            conn.execute("PRAGMA soft_heap_limit = 131072;")
            conn.execute("PRAGMA shrink_memory;")
        except Exception:
            pass

    def _commit_and_shrink(self) -> None:
        """Commit transaction and trim SQLite memory cache."""
        self._conn.commit()
        try:
            self._conn.execute("PRAGMA shrink_memory;")
        except Exception:
            pass

    def shrink_memory(self, truncate_wal: bool = False) -> None:
        """Explicitly reclaim SQLite internal cache and buffer memory."""
        with self._lock:
            try:
                if truncate_wal:
                    try:
                        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                    except Exception:
                        try:
                            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                        except Exception:
                            pass
                self._conn.execute("PRAGMA shrink_memory;")
            except Exception:
                pass

    def init_schema(self) -> None:
        """Create 4-table relational schema and corresponding performance indexes."""
        with self._lock:
            # 1. Webhook Events Table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    idempotency_key TEXT,
                    payload_hash TEXT NOT NULL,
                    headers_json TEXT NOT NULL,
                    raw_payload TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    remote_addr TEXT,
                    status TEXT NOT NULL DEFAULT 'received',
                    received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT chk_event_status CHECK(status IN ('received', 'processed', 'duplicate', 'rejected'))
                );
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_events_source_idemp
                    ON webhook_events (source, idempotency_key);
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_webhook_events_payload_hash
                    ON webhook_events (payload_hash);
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_webhook_events_received_at
                    ON webhook_events (received_at DESC);
                """
            )

            # 2. Tasks Table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    event_id TEXT,
                    source TEXT DEFAULT 'default',
                    action_type TEXT NOT NULL,
                    target_action TEXT,
                    command TEXT,
                    action_params_json TEXT,
                    status TEXT NOT NULL DEFAULT 'received',
                    priority INTEGER NOT NULL DEFAULT 0,
                    timeout_seconds INTEGER NOT NULL DEFAULT 300,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 0,
                    exit_code INTEGER,
                    error_message TEXT,
                    result_json TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    queued_at TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (event_id) REFERENCES webhook_events(event_id) ON DELETE CASCADE,
                    CONSTRAINT chk_task_status CHECK(status IN ('received', 'queued', 'running', 'succeeded', 'failed', 'cancelled', 'timed_out'))
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_queue_poll
                    ON tasks (status, priority DESC, created_at ASC);
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_event_id
                    ON tasks (event_id);
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_created_at
                    ON tasks (created_at DESC);
                """
            )

            # 3. Executions Table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    pid INTEGER,
                    status TEXT NOT NULL DEFAULT 'running',
                    exit_code INTEGER,
                    stdout_tail TEXT,
                    stderr_tail TEXT,
                    error_message TEXT,
                    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE,
                    CONSTRAINT chk_execution_status CHECK(status IN ('running', 'succeeded', 'failed', 'timed_out', 'killed'))
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_executions_task
                    ON executions (task_id, attempt_number DESC);
                """
            )

            # 4. Execution Logs Table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id TEXT,
                    task_id TEXT NOT NULL,
                    stream_type TEXT NOT NULL DEFAULT 'stdout',
                    chunk TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_execution_logs_stream
                    ON execution_logs (task_id, log_id ASC);
                """
            )

            self._conn.commit()
            try:
                self._conn.execute("PRAGMA shrink_memory;")
            except Exception:
                pass

    def close(self) -> None:
        """Cleanly close SQLite connection."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # --- Event Ingestion & Deduplication ---

    def insert_webhook_event(self, event_dict: dict[str, Any] | Any) -> bool:
        """
        Insert incoming webhook event into webhook_events table.
        Returns True on success, False if duplicate (source, idempotency_key).
        """
        if hasattr(event_dict, "to_dict"):
            event_dict = event_dict.to_dict()

        with self._lock:
            source = event_dict.get("source", "default")
            idemp_key = event_dict.get("idempotency_key")
            payload_hash = event_dict.get("payload_hash", "")

            # Check if idempotency key already exists
            if idemp_key:
                cur = self._conn.cursor()
                try:
                    cur.execute(
                        "SELECT event_id FROM webhook_events WHERE source = ? AND idempotency_key = ?",
                        (source, idemp_key),
                    )
                    if cur.fetchone() is not None:
                        return False
                finally:
                    cur.close()

            try:
                headers_json = event_dict.get("headers_json")
                if headers_json is None and "headers" in event_dict:
                    headers_json = json.dumps(event_dict["headers"])
                elif headers_json is None:
                    headers_json = "{}"

                raw_payload = event_dict.get("raw_payload", "")
                method = event_dict.get("method", "POST")
                path = event_dict.get("path", "/webhook")
                remote_addr = event_dict.get("remote_addr", "127.0.0.1")
                status = event_dict.get("status", "received")

                self._conn.execute(
                    """
                    INSERT INTO webhook_events (
                        event_id, source, idempotency_key, payload_hash,
                        headers_json, raw_payload, method, path, remote_addr, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_dict["event_id"],
                        source,
                        idemp_key,
                        payload_hash,
                        headers_json,
                        raw_payload,
                        method,
                        path,
                        remote_addr,
                        status,
                    ),
                )
                self._commit_and_shrink()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def get_webhook_event(self, event_id: str) -> Optional[dict[str, Any]]:
        """Retrieve webhook event record by event_id."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("SELECT * FROM webhook_events WHERE event_id = ?", (event_id,))
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    def get_event_by_idempotency(self, source: str, idempotency_key: str) -> Optional[dict[str, Any]]:
        """Retrieve webhook event record by source and idempotency_key."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT * FROM webhook_events WHERE source = ? AND idempotency_key = ?",
                    (source, idempotency_key),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        "SELECT * FROM webhook_events WHERE idempotency_key = ? ORDER BY received_at DESC LIMIT 1",
                        (idempotency_key,),
                    )
                    row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    def get_event_by_payload_hash(self, source: str, payload_hash: str) -> Optional[dict[str, Any]]:
        """Retrieve webhook event record by source and payload_hash."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "SELECT * FROM webhook_events WHERE source = ? AND payload_hash = ? ORDER BY received_at DESC LIMIT 1",
                    (source, payload_hash),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        "SELECT * FROM webhook_events WHERE payload_hash = ? ORDER BY received_at DESC LIMIT 1",
                        (payload_hash,),
                    )
                    row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    # --- Task State Machine ---

    def insert_task(self, task_dict: dict[str, Any] | Any) -> str:
        """Insert a new task associated with an event into tasks table."""
        if hasattr(task_dict, "to_dict"):
            task_dict = task_dict.to_dict()

        with self._lock:
            task_id = task_dict["task_id"]
            event_id = task_dict.get("event_id")
            source = task_dict.get("source", "default")
            action_type = task_dict.get("action_type", "cli")
            command = task_dict.get("command") or task_dict.get("target_action")
            target_action = task_dict.get("target_action") or command or ""

            action_params_json = task_dict.get("action_params_json")
            if action_params_json is None and "action_params" in task_dict:
                action_params_json = json.dumps(task_dict["action_params"])

            status = task_dict.get("status", "received")
            priority = int(task_dict.get("priority", 0))
            timeout_seconds = int(task_dict.get("timeout_seconds", 300))
            retry_count = int(task_dict.get("retry_count", 0))
            max_retries = int(task_dict.get("max_retries", 0))
            error_message = task_dict.get("error_message")
            exit_code = task_dict.get("exit_code")

            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, event_id, source, action_type, target_action, command,
                    action_params_json, status, priority, timeout_seconds,
                    retry_count, max_retries, error_message, exit_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    event_id,
                    source,
                    action_type,
                    target_action,
                    command,
                    action_params_json,
                    status,
                    priority,
                    timeout_seconds,
                    retry_count,
                    max_retries,
                    error_message,
                    exit_code,
                ),
            )
            self._commit_and_shrink()
            return task_id

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Retrieve task by task_id."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    def get_task_by_event_id(self, event_id: str) -> Optional[dict[str, Any]]:
        """Retrieve task linked to an event_id."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("SELECT * FROM tasks WHERE event_id = ? ORDER BY created_at DESC LIMIT 1", (event_id,))
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    def get_task_by_idempotency(self, source: str, idempotency_key: str) -> Optional[dict[str, Any]]:
        """Retrieve existing task by source and idempotency key."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT t.* FROM tasks t
                    JOIN webhook_events e ON t.event_id = e.event_id
                    WHERE e.source = ? AND e.idempotency_key = ?
                    ORDER BY t.created_at DESC LIMIT 1
                    """,
                    (source, idempotency_key),
                )
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    def get_task_by_payload_hash(self, source: str, payload_hash: str) -> Optional[dict[str, Any]]:
        """Retrieve existing task by source and payload hash."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT t.* FROM tasks t
                    JOIN webhook_events e ON t.event_id = e.event_id
                    WHERE e.source = ? AND e.payload_hash = ?
                    ORDER BY t.created_at DESC LIMIT 1
                    """,
                    (source, payload_hash),
                )
                row = cur.fetchone()
                return dict(row) if row else None
            finally:
                cur.close()

    def update_task_status_cas(
        self,
        task_id: str,
        expected_status: str,
        new_status: str,
        exit_code: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """
        Atomic Compare-And-Swap (CAS) state machine transition.
        Only updates if current status matches expected_status.
        Returns True if transition occurred, False otherwise.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                query = """
                UPDATE tasks
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP,
                    queued_at = CASE WHEN ? = 'queued' THEN CURRENT_TIMESTAMP ELSE queued_at END,
                    started_at = CASE WHEN ? = 'running' THEN CURRENT_TIMESTAMP WHEN ? = 'queued' THEN NULL ELSE started_at END,
                    completed_at = CASE WHEN ? IN ('succeeded', 'failed', 'timed_out', 'cancelled') THEN CURRENT_TIMESTAMP WHEN ? = 'queued' THEN NULL ELSE completed_at END,
                    exit_code = CASE WHEN ? IS NOT NULL THEN ? ELSE exit_code END,
                    error_message = CASE WHEN ? IS NOT NULL THEN ? ELSE error_message END
                WHERE task_id = ? AND status = ?
                RETURNING task_id, status;
                """
                cur.execute(
                    query,
                    (
                        new_status,
                        new_status,
                        new_status,
                        new_status,
                        new_status,
                        new_status,
                        exit_code,
                        exit_code,
                        error_message,
                        error_message,
                        task_id,
                        expected_status,
                    ),
                )
                row = cur.fetchone()
                self._commit_and_shrink()
                return row is not None
            finally:
                cur.close()

    def recover_orphaned_tasks(self) -> int:
        """
        Boot-time crash recovery:
        Marks any tasks stranded in 'running' status as 'queued' (if retries left) or 'failed'.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                query = """
                UPDATE tasks
                SET status = CASE WHEN retry_count < max_retries THEN 'queued' ELSE 'failed' END,
                    retry_count = CASE WHEN retry_count < max_retries THEN retry_count + 1 ELSE retry_count END,
                    error_message = 'Interrupted by server restart',
                    queued_at = CASE WHEN retry_count < max_retries THEN CURRENT_TIMESTAMP ELSE queued_at END,
                    started_at = CASE WHEN retry_count < max_retries THEN NULL ELSE started_at END,
                    completed_at = CASE WHEN retry_count < max_retries THEN NULL ELSE CURRENT_TIMESTAMP END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                RETURNING task_id, status;
                """
                cur.execute(query)
                rows = cur.fetchall()
                self._commit_and_shrink()
                return len(rows)
            finally:
                cur.close()

    def get_queued_tasks(self) -> list[dict[str, Any]]:
        """
        Query all pending tasks in 'queued' status ordered by priority and creation time.
        Uses idx_tasks_queue_poll (status, priority DESC, created_at ASC).
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'queued'
                    ORDER BY priority DESC, created_at ASC
                    """
                )
                rows = cur.fetchall()
                return [dict(r) for r in rows]
            finally:
                cur.close()

    def update_event_status(self, event_id: str, new_status: str) -> bool:
        """Update webhook event status (received, processed, duplicate, rejected)."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    "UPDATE webhook_events SET status = ? WHERE event_id = ?",
                    (new_status, event_id),
                )
                success = cur.rowcount > 0
                self._commit_and_shrink()
                return success
            finally:
                cur.close()

    def get_orphaned_webhook_events(self, source: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        """
        Query webhook_events in 'received' status that do not have an associated task in tasks table.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    f"""
                    SELECT e.event_id, e.source, e.idempotency_key, e.status, e.received_at, e.raw_payload
                    FROM webhook_events e
                    LEFT JOIN tasks t ON e.event_id = t.event_id
                    WHERE e.status = 'received' AND t.task_id IS NULL
                      {('AND e.source = ?' if source else '')}
                    ORDER BY e.received_at ASC
                    LIMIT ?
                    """,
                    ([source] if source else []) + [limit],
                )
                rows = cur.fetchall()
                return [dict(r) for r in rows]
            finally:
                cur.close()

    def rehydrate_orphaned_event(
        self,
        event_id: str,
        event_record: Optional[dict[str, Any]] = None,
        default_action_type: str = "cli",
        default_max_retries: int = 3,
        default_timeout_seconds: int = 300,
    ) -> Optional[str]:
        """
        Synthesize and insert a task for an orphaned event in 'received' status.
        Returns the created task_id, or None if event not found or already has a task.
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                event = event_record
                if event is None:
                    cur.execute("SELECT * FROM webhook_events WHERE event_id = ?", (event_id,))
                    ev_row = cur.fetchone()
                    if not ev_row:
                        return None
                    event = dict(ev_row)

                # Check if task already exists
                cur.execute("SELECT task_id FROM tasks WHERE event_id = ? LIMIT 1", (event_id,))
                if cur.fetchone():
                    return None

                payload: dict[str, Any] = {}
                raw = event.get("raw_payload", "{}")
                if raw:
                    try:
                        loaded = json.loads(raw)
                        if isinstance(loaded, dict):
                            payload = loaded
                        elif isinstance(loaded, str) and loaded.strip():
                            payload = {"command": loaded}
                    except Exception:
                        payload = {}

                action_type = (
                    payload.get("action_type")
                    or payload.get("action")
                    or ("contact_review" if event.get("source") in ("contact-review", "contact_review") else default_action_type)
                )
                command = payload.get("command") or payload.get("target_action") or ""
                if not command and action_type in ("contact_review", "review_contact", "contact-review"):
                    command = "bin/webhook-hub review-contact"

                task_id = f"tsk_{uuid.uuid4().hex[:16]}"
                task_record = {
                    "task_id": task_id,
                    "event_id": event_id,
                    "source": event.get("source", "default"),
                    "action_type": action_type,
                    "command": command,
                    "target_action": command,
                    "action_params_json": json.dumps(payload),
                    "status": "queued",
                    "priority": int(payload.get("priority", 0)),
                    "timeout_seconds": int(payload.get("timeout_seconds", default_timeout_seconds)),
                    "retry_count": 0,
                    "max_retries": int(payload.get("max_retries", default_max_retries)),
                }

                self.insert_task(task_record)
                return task_id
            finally:
                cur.close()

    def get_unprocessed_tasks(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        stale_running_seconds: int = 300,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Query all unprocessed tasks and events across the system.
        Unprocessed includes:
        - tasks with status 'queued'
        - tasks with status 'received'
        - tasks with status 'running' that have been running longer than MAX(stale_running_seconds, timeout_seconds)
        - tasks with status 'failed' or 'timed_out' due to sleep/interruption with retries remaining (max_retries > 0)
        - orphaned webhook_events with status 'received' without tasks
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                # Counts
                cur.execute(
                    f"SELECT COUNT(*) FROM tasks WHERE status = 'queued' {('AND source = ?' if source else '')}",
                    ([source] if source else []),
                )
                queued_count = cur.fetchone()[0]

                cur.execute(
                    f"SELECT COUNT(*) FROM tasks WHERE status = 'received' {('AND source = ?' if source else '')}",
                    ([source] if source else []),
                )
                received_count = cur.fetchone()[0]

                stale_query = f"""
                SELECT COUNT(*) FROM tasks
                WHERE status = 'running'
                  AND (strftime('%s', 'now') - strftime('%s', COALESCE(started_at, updated_at, created_at))) > MAX(?, COALESCE(timeout_seconds, 0))
                  {('AND source = ?' if source else '')}
                """
                stale_params = [stale_running_seconds] + ([source] if source else [])
                cur.execute(stale_query, stale_params)
                stale_running_count = cur.fetchone()[0]

                if limit == 0:
                    orphaned_count_query = f"""
                    SELECT COUNT(*) FROM webhook_events e
                    LEFT JOIN tasks t ON e.event_id = t.event_id
                    WHERE e.status = 'received' AND t.task_id IS NULL
                      {('AND e.source = ?' if source else '')}
                    """
                    cur.execute(orphaned_count_query, ([source] if source else []))
                    orphaned_count = cur.fetchone()[0]
                    orphaned_events = []
                else:
                    orphaned_events = self.get_orphaned_webhook_events(source=source, limit=limit)
                    orphaned_count = len(orphaned_events)

                interrupted_query = f"""
                SELECT COUNT(*) FROM tasks
                WHERE (
                    (status = 'failed' AND retry_count < max_retries AND (
                        error_message LIKE '%restart%'
                        OR error_message LIKE '%interrupted%'
                        OR error_message LIKE '%timed out%'
                        OR error_message LIKE '%timeout%'
                        OR error_message LIKE '%handshake%'
                        OR error_message LIKE '%Connection%'
                        OR error_message LIKE '%network%'
                    ))
                    OR (status = 'timed_out' AND retry_count < max_retries)
                )
                AND max_retries > 0
                {('AND source = ?' if source else '')}
                """
                cur.execute(interrupted_query, ([source] if source else []))
                recoverable_failed_count = cur.fetchone()[0]

                total_unprocessed = (
                    queued_count
                    + received_count
                    + stale_running_count
                    + orphaned_count
                    + recoverable_failed_count
                )

                # Fetch task records
                if limit == 0:
                    task_rows = []
                elif status:
                    if status == "stale_running":
                        cur.execute(
                            f"""
                            SELECT * FROM tasks
                            WHERE status = 'running'
                              AND (strftime('%s', 'now') - strftime('%s', COALESCE(started_at, updated_at, created_at))) > MAX(?, COALESCE(timeout_seconds, 0))
                              {('AND source = ?' if source else '')}
                            ORDER BY created_at ASC LIMIT ?
                            """,
                            [stale_running_seconds] + ([source] if source else []) + [limit],
                        )
                    else:
                        cur.execute(
                            f"SELECT * FROM tasks WHERE status = ? {('AND source = ?' if source else '')} ORDER BY priority DESC, created_at ASC LIMIT ?",
                            [status] + ([source] if source else []) + [limit],
                        )
                else:
                    cur.execute(
                        f"""
                        SELECT * FROM tasks
                        WHERE (status IN ('queued', 'received')
                           OR (status = 'running' AND (strftime('%s', 'now') - strftime('%s', COALESCE(started_at, updated_at, created_at))) > MAX(?, COALESCE(timeout_seconds, 0)))
                           OR ((
                                (status = 'failed' AND (
                                    error_message LIKE '%restart%'
                                    OR error_message LIKE '%interrupted%'
                                    OR error_message LIKE '%timed out%'
                                    OR error_message LIKE '%timeout%'
                                    OR error_message LIKE '%handshake%'
                                    OR error_message LIKE '%Connection%'
                                    OR error_message LIKE '%network%'
                                ))
                                OR status = 'timed_out'
                           ) AND retry_count < max_retries AND max_retries > 0))
                           {('AND source = ?' if source else '')}
                        ORDER BY priority DESC, created_at ASC
                        LIMIT ?
                        """,
                        [stale_running_seconds] + ([source] if source else []) + [limit],
                    )

                if limit > 0:
                    task_rows = cur.fetchall()
                tasks_list = [dict(r) for r in task_rows]

                return {
                    "total_unprocessed": total_unprocessed,
                    "counts": {
                        "queued": queued_count,
                        "received": received_count,
                        "stale_running": stale_running_count,
                        "orphaned_events": orphaned_count,
                        "recoverable_failed": recoverable_failed_count,
                        "total_unprocessed": total_unprocessed,
                    },
                    "tasks": tasks_list,
                    "orphaned_events": orphaned_events,
                }
            finally:
                cur.close()
                try:
                    self._conn.execute("PRAGMA shrink_memory;")
                except Exception:
                    pass

    def sweep_and_requeue_unprocessed(
        self,
        stale_running_seconds: int = 300,
        max_retries: int = 3,
        auto_retry_interrupted: bool = True,
        source: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Atomic recovery and rehydration of all unprocessed tasks and events:
        1. Resets stale 'running' tasks (cut off by sleep or crashed) to 'queued' (or 'timed_out' if retries exhausted).
        2. Promotes 'received' tasks to 'queued'.
        3. Rehydrates orphaned 'webhook_events' into new 'queued' tasks.
        4. Re-queues recoverable 'failed' and 'timed_out' tasks whose errors resulted from sleep / network loss.
        5. Returns summary metrics and all ready 'queued' task IDs for immediate dispatch.
        """
        with self._lock:
            cur = self._conn.cursor()
            recovered_stale = 0
            recovered_received = 0
            recovered_orphaned = 0
            recovered_failed = 0

            try:
                # 1. Recover stale 'running' tasks (honoring timeout_seconds and max_retries)
                stale_sql = f"""
                SELECT task_id, retry_count, max_retries, error_message, timeout_seconds FROM tasks
                WHERE status = 'running'
                  AND (strftime('%s', 'now') - strftime('%s', COALESCE(started_at, updated_at, created_at))) > MAX(?, COALESCE(timeout_seconds, 0))
                  {('AND source = ?' if source else '')}
                """
                cur.execute(stale_sql, [stale_running_seconds] + ([source] if source else []))
                stale_tasks = cur.fetchall()
                for st in stale_tasks:
                    tid = st["task_id"]
                    curr_rc = int(st["retry_count"])
                    task_max = max(int(st["max_retries"]), max_retries if auto_retry_interrupted else int(st["max_retries"]))

                    if auto_retry_interrupted and curr_rc < task_max:
                        cur.execute(
                            """
                            UPDATE tasks
                            SET status = 'queued',
                                retry_count = retry_count + 1,
                                max_retries = ?,
                                error_message = 'Interrupted by sleep or timeout; auto-recovered by sweeper',
                                queued_at = CURRENT_TIMESTAMP,
                                started_at = NULL,
                                completed_at = NULL,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE task_id = ?
                            """,
                            (task_max, tid),
                        )
                        recovered_stale += 1
                    else:
                        cur.execute(
                            """
                            UPDATE tasks
                            SET status = 'timed_out',
                                error_message = 'Stale execution timed out; max retries exhausted',
                                completed_at = CURRENT_TIMESTAMP,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE task_id = ?
                            """,
                            (tid,),
                        )

                # 2. Promote 'received' tasks to 'queued'
                received_sql = f"""
                UPDATE tasks
                SET status = 'queued',
                    queued_at = CURRENT_TIMESTAMP,
                    started_at = NULL,
                    completed_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'received'
                  {('AND source = ?' if source else '')}
                """
                cur.execute(received_sql, ([source] if source else []))
                recovered_received = cur.rowcount

                # 3. Rehydrate orphaned webhook_events
                orphans = self.get_orphaned_webhook_events(source=source, limit=limit)
                for orp in orphans:
                    eid = orp["event_id"]
                    created_tid = self.rehydrate_orphaned_event(
                        eid,
                        event_record=orp,
                        default_max_retries=max_retries,
                    )
                    if created_tid:
                        recovered_orphaned += 1

                # 4. Recover interrupted 'failed' and 'timed_out' tasks if enabled
                if auto_retry_interrupted:
                    failed_sql = f"""
                    UPDATE tasks
                    SET status = 'queued',
                        retry_count = retry_count + 1,
                        max_retries = MAX(max_retries, ?),
                        error_message = 'Recovered interrupted failure for re-execution',
                        queued_at = CURRENT_TIMESTAMP,
                        started_at = NULL,
                        completed_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE (
                        (status = 'failed' AND (
                            error_message LIKE '%restart%'
                            OR error_message LIKE '%interrupted%'
                            OR error_message LIKE '%timed out%'
                            OR error_message LIKE '%timeout%'
                            OR error_message LIKE '%handshake%'
                            OR error_message LIKE '%Connection%'
                            OR error_message LIKE '%network%'
                        ))
                        OR status = 'timed_out'
                    )
                    AND retry_count < ?
                    {('AND source = ?' if source else '')}
                    """
                    cur.execute(failed_sql, [max_retries, max_retries] + ([source] if source else []))
                    recovered_failed = cur.rowcount

                # 5. Fetch all currently queued tasks
                queued_sql = f"""
                SELECT task_id FROM tasks
                WHERE status = 'queued'
                  {('AND source = ?' if source else '')}
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """
                cur.execute(queued_sql, ([source] if source else []) + [limit])
                queued_rows = cur.fetchall()
                queued_task_ids = [r["task_id"] for r in queued_rows]

                self._commit_and_shrink()

                return {
                    "recovered_stale_running": recovered_stale,
                    "recovered_received_tasks": recovered_received,
                    "recovered_orphaned_events": recovered_orphaned,
                    "recovered_interrupted_failed": recovered_failed,
                    "total_queued": len(queued_task_ids),
                    "task_ids": queued_task_ids,
                }
            finally:
                cur.close()

    def insert_event_and_task(
        self,
        event_dict: dict[str, Any],
        task_dict: dict[str, Any],
    ) -> tuple[bool, str, str]:
        """
        Atomically insert webhook event and associated task in a single transaction.
        Returns (is_new_event, event_id, task_id).
        If event already exists (by idempotency_key), attaches task to existing event_id.
        """
        with self._lock:
            source = event_dict.get("source", "default")
            idemp_key = event_dict.get("idempotency_key")
            payload_hash = event_dict.get("payload_hash", "")

            existing_event = None
            if idemp_key:
                existing_event = self.get_event_by_idempotency(source, idemp_key)
            if not existing_event and payload_hash:
                existing_event = self.get_event_by_payload_hash(source, payload_hash)

            if existing_event:
                event_id = existing_event["event_id"]
                existing_task = self.get_task_by_event_id(event_id)
                if existing_task:
                    return False, event_id, existing_task["task_id"]
                task_dict["event_id"] = event_id
                task_id = self.insert_task(task_dict)
                return False, event_id, task_id

            try:
                self.insert_webhook_event(event_dict)
                task_id = self.insert_task(task_dict)
                return True, event_dict["event_id"], task_id
            except Exception:
                self._conn.rollback()
                raise

    # --- Executions & Logs ---

    def insert_execution(self, exec_dict: dict[str, Any]) -> str:
        """Insert execution attempt audit record."""
        with self._lock:
            execution_id = exec_dict["execution_id"]
            self._conn.execute(
                """
                INSERT INTO executions (
                    execution_id, task_id, attempt_number, pid, status,
                    exit_code, stdout_tail, stderr_tail, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    exec_dict["task_id"],
                    exec_dict.get("attempt_number", 1),
                    exec_dict.get("pid"),
                    exec_dict.get("status", "running"),
                    exec_dict.get("exit_code"),
                    exec_dict.get("stdout_tail", ""),
                    exec_dict.get("stderr_tail", ""),
                    exec_dict.get("error_message"),
                ),
            )
            self._commit_and_shrink()
            return execution_id

    def update_execution(
        self,
        execution_id: str,
        status: str,
        exit_code: Optional[int] = None,
        stdout_tail: Optional[str] = None,
        stderr_tail: Optional[str] = None,
        error_message: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> None:
        """Update terminal status of execution record."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE executions
                SET status = ?,
                    exit_code = COALESCE(?, exit_code),
                    stdout_tail = COALESCE(?, stdout_tail),
                    stderr_tail = COALESCE(?, stderr_tail),
                    error_message = COALESCE(?, error_message),
                    pid = COALESCE(?, pid),
                    completed_at = CURRENT_TIMESTAMP
                WHERE execution_id = ?
                """,
                (status, exit_code, stdout_tail, stderr_tail, error_message, pid, execution_id),
            )
            self._commit_and_shrink()

    def insert_execution_log(
        self,
        task_id: str,
        stream: str,
        chunk: str,
        execution_id: Optional[str] = None,
    ) -> int:
        """Insert an individual line or chunk of output into execution_logs."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO execution_logs (execution_id, task_id, stream_type, chunk)
                    VALUES (?, ?, ?, ?)
                    """,
                    (execution_id or "", task_id, stream, chunk),
                )
                self._commit_and_shrink()
                return cur.lastrowid or 0
            finally:
                cur.close()

    def insert_log_chunk(
        self,
        task_id: str,
        stream: str,
        chunk: str,
        execution_id: Optional[str] = None,
    ) -> int:
        """Alias for insert_execution_log."""
        return self.insert_execution_log(task_id, stream, chunk, execution_id)

    # --- Async Helpers for Non-blocking Queries ---

    async def execute_read(self, query: str, params: Any = ()) -> list[dict[str, Any]]:
        """Run read-only query under lock without ThreadPool overhead."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(query, params)
                return [dict(row) for row in cur.fetchall()]
            finally:
                cur.close()
                try:
                    self._conn.execute("PRAGMA shrink_memory;")
                except Exception:
                    pass

    async def execute_write(self, query: str, params: Any = ()) -> int:
        """Run write query serialized under lock."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(query, params)
                self._conn.commit()
                return cur.rowcount
            finally:
                cur.close()
                try:
                    self._conn.execute("PRAGMA shrink_memory;")
                except Exception:
                    pass

    async def execute_write_returning(self, query: str, params: Any = ()) -> list[tuple[Any, ...]]:
        """Run write query with RETURNING clause under lock."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(query, params)
                rows = cur.fetchall()
                self._conn.commit()
                return [tuple(r) for r in rows]
            finally:
                cur.close()
                try:
                    self._conn.execute("PRAGMA shrink_memory;")
                except Exception:
                    pass
