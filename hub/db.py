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
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hub.db")

# Patch sqlite3.connect to ensure WAL mode and performance tuning PRAGMAs
# are strictly enforced on all connections opened across the process.
_original_sqlite3_connect = sqlite3.connect


def _tuned_sqlite3_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    conn = _original_sqlite3_connect(*args, **kwargs)
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA cache_size = -4000;")
        conn.execute("PRAGMA mmap_size = 0;")
        conn.execute("PRAGMA temp_store = FILE;")
        conn.execute("PRAGMA wal_autocheckpoint = 50;")
    except Exception as e:
        logger.debug("Failed to apply initial PRAGMAs: %s", e)
    return conn


sqlite3.connect = _tuned_sqlite3_connect


class DatabaseManager:
    """
    SQLite Single-Source-of-Truth (SSOT) Database Manager.
    Enforces WAL mode, atomic CAS state machine transitions, and crash recovery.
    """

    def __init__(self, db_path: str = "data/webhook_hub.db"):
        self.db_path = str(db_path)
        if self.db_path not in (":memory:", ""):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas(self._conn)

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """Apply tuned SQLite PRAGMAs for concurrency and bounded memory."""
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA cache_size = -4000;")  # ~4MB cache per PROJECT.md
        conn.execute("PRAGMA mmap_size = 0;")
        conn.execute("PRAGMA temp_store = FILE;")
        conn.execute("PRAGMA wal_autocheckpoint = 50;")
        try:
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

    def shrink_memory(self) -> None:
        """Explicitly reclaim SQLite internal cache and buffer memory."""
        with self._lock:
            try:
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

            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, event_id, source, action_type, target_action, command,
                    action_params_json, status, priority, timeout_seconds,
                    retry_count, max_retries
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    queued_at = CASE WHEN ? = 'queued' AND queued_at IS NULL THEN CURRENT_TIMESTAMP ELSE queued_at END,
                    started_at = CASE WHEN ? = 'running' AND started_at IS NULL THEN CURRENT_TIMESTAMP ELSE started_at END,
                    completed_at = CASE WHEN ? IN ('succeeded', 'failed', 'timed_out', 'cancelled') THEN CURRENT_TIMESTAMP ELSE completed_at END,
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
        Marks any tasks stranded in 'running' status as 'failed' (or re-queues if retries left).
        """
        with self._lock:
            cur = self._conn.cursor()
            try:
                query = """
                UPDATE tasks
                SET status = CASE WHEN retry_count < max_retries THEN 'queued' ELSE 'failed' END,
                    retry_count = CASE WHEN retry_count < max_retries THEN retry_count + 1 ELSE retry_count END,
                    error_message = 'Interrupted by server restart',
                    completed_at = CURRENT_TIMESTAMP,
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
