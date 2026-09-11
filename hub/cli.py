"""
Antigravity Webhook Hub — Unified Command Line Interface
Executable via `bin/webhook-hub` or `python -m hub`.

Subcommands:
- `start`: Starts the gateway HTTP server (foreground or daemon mode) with SQLite SSOT and task dispatcher.
- `stop`: Gracefully terminates the running gateway via PID file, escalating to SIGKILL if necessary.
- `status`: Queries gateway process liveness and GET /healthz metrics (port, PID, status, uptime, RSS).
- `logs`: Tails recent logs from SQLite DB or streams live from SSE (/events/stream or /tasks/{id}/stream).
- `test-send`: Signs a test webhook payload with HMAC SHA-256 or Bearer token, sends to /webhook, and displays response.
- `verify`: Runs standalone E2E verification suite (scripts/verify_e2e.py) and exits with its return code.
"""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True

try:
    import threading
    threading.stack_size(131072)
except Exception:
    pass

import time
import types
from pathlib import Path
from typing import Any, Optional

# Zero-overhead lazy SSL provider avoiding heavy OpenSSL/LibreSSL dynamic lib loading (<30MB RAM budget)
if "ssl" not in sys.modules:
    class _LightweightSSL(types.ModuleType):
        def __init__(self):
            super().__init__("ssl")
            self.__dict__["_real"] = None
            for attr in ["SSLWantReadError", "SSLSyscallError", "SSLZeroReturnError", "SSLError"]:
                self.__dict__[attr] = type(attr, (Exception,), {})

        def __getattr__(self, name: str):
            if self.__dict__["_real"] is None:
                import importlib
                sys.modules.pop("ssl", None)
                self.__dict__["_real"] = importlib.import_module("ssl")
                sys.modules["ssl"] = self
            val = getattr(self.__dict__["_real"], name)
            self.__dict__[name] = val
            return val

    sys.modules["ssl"] = _LightweightSSL()

# Zero-dependency RFC 1123 date provider with transparent fallback (<30MB RAM budget)
if "email.utils" not in sys.modules:
    class _LightweightEmailUtils(types.ModuleType):
        def __init__(self):
            super().__init__("email.utils")
            self.__dict__["formatdate"] = lambda timeval=None, localtime=False, usegmt=True: time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(timeval if timeval is not None else time.time())
            )
            self.__dict__["_real"] = None

        def __getattr__(self, name: str):
            if name.startswith("__"):
                raise AttributeError(name)
            if self.__dict__["_real"] is None:
                import importlib
                sys.modules.pop("email.utils", None)
                self.__dict__["_real"] = importlib.import_module("email.utils")
                sys.modules["email.utils"] = self
            val = getattr(self.__dict__["_real"], name)
            self.__dict__[name] = val
            return val

    sys.modules["email.utils"] = _LightweightEmailUtils()

import asyncio
import gc
import logging
import os
import signal

from hub.config import AppConfig, ServerConfig, load_config
from hub.memory import apply_memory_pressure_relief

logger = logging.getLogger("hub.cli")

DEFAULT_PID_FILE = ".webhook-hub.pid"
DEFAULT_VERIFY_SECRET = "verify_secret_32bytes_12345678"


def _is_pid_running(pid: int) -> bool:
    """Check if process with given PID exists and is running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid_file(pid_path: Path) -> Optional[int]:
    """Read integer PID from PID file if it exists and is valid."""
    if not pid_path.is_file():
        return None
    try:
        content = pid_path.read_text(encoding="utf-8").strip()
        return int(content) if content else None
    except (ValueError, OSError):
        return None


def _get_process_rss_mb(pid: int) -> float:
    """Query exact resident set size (RSS) of a PID using ps on macOS/Linux."""
    import subprocess
    try:
        out = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)]).decode().strip()
        return int(out) / 1024.0
    except Exception:
        import resource
        raw_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return (raw_rss / (1024.0 * 1024.0)) if sys.platform == "darwin" else (raw_rss / 1024.0)


# ==============================================================================
# SUBCOMMAND: start
# ==============================================================================

async def run_server_foreground(config: AppConfig, pid_path: Optional[Path] = None) -> int:
    """Run gateway server in the foreground with full subsystem wiring."""
    from hub.broker import EventBroker
    from hub.db import DatabaseManager
    from hub.dispatcher import TaskDispatcher
    from hub.routes.dashboard import register_dashboard_routes
    from hub.routes.observability import register_observability_routes
    from hub.routes.sse import register_sse_routes
    from hub.routes.tasks import register_task_routes
    from hub.routes.uptime_kuma import register_uptime_kuma_routes
    from hub.routes.webhook import register_webhook_routes
    from hub.server import AsyncHTTPServer

    pid = os.getpid()

    # Write PID file
    if pid_path:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(f"{pid}\n", encoding="utf-8")

    # 1. Initialize SQLite Database Manager & Migrations
    db_mgr = DatabaseManager(config.database.path, cache_size=-16)
    try:
        db_mgr._conn.execute("PRAGMA cache_size = -16;")
        db_mgr._conn.execute("PRAGMA mmap_size = 0;")
        db_mgr._conn.execute("PRAGMA wal_autocheckpoint = 20;")
    except Exception:
        pass
    db_mgr.init_schema()
    try:
        db_mgr._conn.execute("PRAGMA shrink_memory;")
    except Exception:
        pass

    # 2. Initialize in-memory SSE Event Broker
    broker = EventBroker()

    # 3. Initialize Decoupled Async Task Dispatcher
    dispatcher = TaskDispatcher(
        db=db_mgr,
        broker=broker,
        config=config,
    )
    await dispatcher.start()

    # 4. Initialize Lightweight HTTP/1.1 Server
    server = AsyncHTTPServer(config.server)
    server._db = db_mgr

    # 5. Wire System Routes
    register_webhook_routes(server, config, db_mgr, dispatcher, broker)
    register_uptime_kuma_routes(server, config, db_mgr, dispatcher, broker)
    register_task_routes(server, config, db_mgr, dispatcher, broker)
    register_observability_routes(server, config, db_mgr, broker, dispatcher)
    register_sse_routes(server, config, db_mgr, broker)
    register_dashboard_routes(server, config, db_mgr, broker, dispatcher)

    # 6. Start HTTP Server
    try:
        await server.start()
    except OSError as e:
        print(
            f"Error: Unable to bind to http://{config.server.host}:{config.server.port}: {e}",
            file=sys.stderr,
        )
        if pid_path and pid_path.is_file():
            try:
                pid_path.unlink()
            except OSError:
                pass
        await dispatcher.stop()
        db_mgr.close()
        return 1

    # Reclaim setup allocation memory
    apply_memory_pressure_relief()
    db_mgr.shrink_memory(truncate_wal=False)

    print(f"Antigravity Webhook Hub running at http://{config.server.host}:{config.server.port} (PID: {pid})")

    # 7. Listen for Shutdown Signals
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        print("\nStopping Antigravity Webhook Hub...")
        await server.stop()
        await dispatcher.stop()
        db_mgr.close()
        if pid_path and pid_path.is_file():
            try:
                pid_path.unlink()
            except OSError:
                pass
        print("Antigravity Webhook Hub stopped cleanly.")

    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Handle `webhook-hub start` command."""
    pid_path = Path(args.pidfile)

    # Check if already running
    existing_pid = _read_pid_file(pid_path)
    if existing_pid is not None and _is_pid_running(existing_pid):
        print(f"Error: Antigravity Webhook Hub is already running (PID: {existing_pid}).", file=sys.stderr)
        return 1

    # Load configuration
    cli_overrides = {}
    if args.port is not None:
        cli_overrides["server.port"] = args.port
    if args.host is not None:
        cli_overrides["server.host"] = args.host
    if args.db is not None:
        cli_overrides["database.path"] = args.db
    if getattr(args, "secret", None):
        cli_overrides["security.webhook_secret"] = args.secret

    config = load_config(
        config_path=args.config,
        env_path=args.env_file,
        cli_overrides=cli_overrides,
    )

    # Ensure a non-empty secret is present (defaulting to verify secret if none in config/env)
    if not config.security.webhook_secret:
        config.security.webhook_secret = os.environ.get("WEBHOOK_SECRET", DEFAULT_VERIFY_SECRET)

    if args.daemon:
        import subprocess
        import urllib.request

        # Daemon Mode: spawn detached process and poll /healthz
        print(f"Starting Antigravity Webhook Hub in daemon mode on port {config.server.port}...")

        hub_bin = Path(__file__).resolve().parent.parent / "bin" / "webhook-hub"
        entrypoint = str(hub_bin) if hub_bin.is_file() else str(Path(__file__).resolve().parent / "__main__.py")
        cmd = [
            sys.executable,
            entrypoint,
            "start",
            "--host",
            config.server.host,
            "--port",
            str(config.server.port),
            "--db",
            config.database.path,
            "--pidfile",
            str(pid_path),
            "--secret",
            config.security.webhook_secret,
        ]
        if args.config:
            cmd.extend(["--config", args.config])
        if args.env_file:
            cmd.extend(["--env-file", args.env_file])

        log_path = Path("webhook-hub.log")
        log_file = open(log_path, "a", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=log_file,
            stderr=log_file,
        )
        log_file.close()

        # Poll healthz endpoint up to 5 seconds
        target_url = f"http://{config.server.host}:{config.server.port}/healthz"
        started = False
        for _ in range(50):
            time.sleep(0.1)
            if proc.poll() is not None:
                print(f"Error: Daemon process exited prematurely with code {proc.returncode}. See {log_path} for logs.", file=sys.stderr)
                return 1
            try:
                req = urllib.request.Request(target_url, method="GET")
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    if resp.status == 200:
                        started = True
                        break
            except Exception:
                pass

        if not started:
            print("Error: Gateway server failed to start within 5 seconds. Check webhook-hub.log.", file=sys.stderr)
            return 1

        active_pid = _read_pid_file(pid_path) or proc.pid
        print(f"Antigravity Webhook Hub started in daemon mode (PID: {active_pid}, port: {config.server.port}).")
        return 0

    # Foreground Mode
    try:
        return asyncio.run(run_server_foreground(config, pid_path))
    except (KeyboardInterrupt, SystemExit):
        return 0


# ==============================================================================
# SUBCOMMAND: stop
# ==============================================================================

def cmd_stop(args: argparse.Namespace) -> int:
    """Handle `webhook-hub stop` command."""
    pid_path = Path(args.pidfile)
    pid = _read_pid_file(pid_path)

    if pid is None:
        print(f"No active PID file found at '{pid_path}'. Gateway may not be running.")
        return 1

    if not _is_pid_running(pid):
        print(f"Process {pid} is not running. Cleaned up stale PID file.")
        try:
            pid_path.unlink()
        except OSError:
            pass
        return 0

    if args.force:
        print(f"Force killing process {pid} with SIGKILL...")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            pid_path.unlink()
        except OSError:
            pass
        print(f"Antigravity Webhook Hub (PID: {pid}) killed.")
        return 0

    print(f"Sending SIGTERM to process {pid}...")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        print(f"Failed to signal process {pid}: {e}", file=sys.stderr)
        return 1

    timeout = float(args.timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_pid_running(pid):
            try:
                pid_path.unlink()
            except OSError:
                pass
            print(f"Antigravity Webhook Hub (PID: {pid}) stopped gracefully.")
            return 0
        time.sleep(0.1)

    print(f"Process {pid} did not stop within {timeout}s. Escalating to SIGKILL...")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        pid_path.unlink()
    except OSError:
        pass
    print(f"Antigravity Webhook Hub (PID: {pid}) killed.")
    return 0


# ==============================================================================
# SUBCOMMAND: status
# ==============================================================================

def cmd_status(args: argparse.Namespace) -> int:
    """Handle `webhook-hub status` command."""
    import json
    import urllib.request

    pid_path = Path(args.pidfile)
    pid = _read_pid_file(pid_path)
    is_alive = _is_pid_running(pid) if pid is not None else False

    host = args.host
    port = args.port
    healthz_url = f"http://{host}:{port}/healthz"

    healthz_data: Optional[dict[str, Any]] = None
    try:
        req = urllib.request.Request(healthz_url, method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                healthz_data = json.loads(body)
    except Exception:
        pass

    if healthz_data is not None:
        uptime = healthz_data.get("uptime_seconds", 0.0)
        status_str = healthz_data.get("status", "ok")
        system_info = healthz_data.get("system", {})
        mem_rss = system_info.get("memory_rss_mb") or (_get_process_rss_mb(pid) if pid else 0.0)
        db_info = healthz_data.get("database", {})
        db_status = db_info.get("status", healthz_data.get("db", "connected"))

        unproc_count = healthz_data.get("unprocessed_tasks_count", 0)
        unproc_info = healthz_data.get("unprocessed", {})

        if args.json:
            out = {
                "running": True,
                "pid": pid,
                "host": host,
                "port": port,
                "status": status_str,
                "uptime_seconds": uptime,
                "memory_rss_mb": round(mem_rss, 2),
                "database": db_status,
                "unprocessed_tasks": unproc_count,
                "unprocessed_details": unproc_info,
                "healthz": healthz_data,
            }
            print(json.dumps(out, indent=2))
        else:
            print("==================================================")
            print(" Antigravity Webhook Hub Status: RUNNING")
            print("==================================================")
            print(f"  PID:         {pid or 'N/A'}")
            print(f"  Endpoint:    http://{host}:{port}")
            print(f"  Status:      {status_str.upper()}")
            print(f"  Uptime:      {uptime:.1f}s")
            print(f"  Memory RSS:  {mem_rss:.2f} MB (Budget: < 30.0 MB)")
            print(f"  Database:    {db_status}")
            print(f"  Unprocessed: {unproc_count} tasks")
            print("==================================================")
        return 0

    # Not running or unreachable
    offline_unproc = 0
    try:
        from hub.db import DatabaseManager
        mgr = DatabaseManager(getattr(args, "db", None) or "data/webhook_hub.db")
        unproc_data = mgr.get_unprocessed_tasks(limit=1)
        offline_unproc = unproc_data.get("counts", {}).get("total_unprocessed", 0)
        mgr.close()
    except Exception:
        pass

    if args.json:
        out = {
            "running": False,
            "pid": pid,
            "is_alive": is_alive,
            "host": host,
            "port": port,
            "status": "stopped",
            "unprocessed_tasks": offline_unproc,
        }
        print(json.dumps(out, indent=2))
    else:
        print("==================================================")
        print(" Antigravity Webhook Hub Status: STOPPED")
        print("==================================================")
        if pid and is_alive:
            print(f"  PID {pid} is alive, but HTTP {healthz_url} is not responding.")
        elif pid:
            print(f"  Stale PID file found pointing to dead process {pid}.")
        else:
            print(f"  No running server detected on http://{host}:{port}.")
        if offline_unproc > 0:
            print(f"  Unprocessed in DB: {offline_unproc} tasks (Run './bin/webhook-hub sweep' or start daemon)")
        print("==================================================")
    return 1


# ==============================================================================
# SUBCOMMAND: logs
# ==============================================================================

def cmd_logs(args: argparse.Namespace) -> int:
    """Handle `webhook-hub logs` command."""
    if args.follow:
        import urllib.error
        import urllib.request

        # Live streaming via SSE
        if args.task:
            stream_url = f"http://{args.host}:{args.port}/tasks/{args.task}/stream"
            print(f"Subscribing to live task stream: {stream_url} (Ctrl+C to exit)...")
        else:
            stream_url = f"http://{args.host}:{args.port}/events/stream"
            print(f"Subscribing to global event stream: {stream_url} (Ctrl+C to exit)...")

        req = urllib.request.Request(
            stream_url,
            headers={"Accept": "text/event-stream"},
            method="GET",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                current_event = "message"
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    if line.startswith(":"):
                        # SSE comment or heartbeat ping
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_content = line[len("data:"):].strip()
                        ts_str = time.strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[{ts_str}] [{current_event}] {data_content}", flush=True)
        except (KeyboardInterrupt, SystemExit):
            print("\nStream closed.")
            return 0
        except urllib.error.URLError as e:
            print(f"Error connecting to stream: {e.reason}", file=sys.stderr)
            return 1
        return 0

    # Offline / Historical Log Query via SQLite DB
    import sqlite3

    cli_overrides = {}
    if args.db:
        cli_overrides["database.path"] = args.db
    config = load_config(config_path=args.config, cli_overrides=cli_overrides)
    db_path = Path(config.database.path)

    if not db_path.is_file():
        print(f"Error: Database file not found at '{db_path}'. Use --follow to stream from running server.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        if args.task:
            cur.execute(
                """
                SELECT log_id, stream_type, chunk, timestamp
                FROM execution_logs
                WHERE task_id = ?
                ORDER BY log_id ASC LIMIT ?
                """,
                (args.task, args.lines),
            )
            rows = cur.fetchall()
            if not rows:
                # Check task summary
                cur.execute("SELECT status, exit_code, stdout_tail, stderr_tail, error_message FROM tasks WHERE task_id = ?", (args.task,))
                t_row = cur.fetchone()
                if t_row:
                    print(f"Task {args.task} status: {t_row['status']}, exit_code: {t_row['exit_code']}")
                    if t_row['stdout_tail']:
                        print(f"--- stdout ---\n{t_row['stdout_tail']}")
                    if t_row['stderr_tail']:
                        print(f"--- stderr ---\n{t_row['stderr_tail']}")
                    if t_row['error_message']:
                        print(f"--- error ---\n{t_row['error_message']}")
                else:
                    print(f"No logs found for task '{args.task}'.")
            else:
                for r in rows:
                    stream_t = r["stream_type"]
                    chunk = r["chunk"].rstrip("\r\n")
                    ts = r["timestamp"]
                    print(f"[{ts}] [{stream_t}] {chunk}")
        else:
            cur.execute(
                """
                SELECT log_id, task_id, stream_type, chunk, timestamp
                FROM execution_logs
                ORDER BY log_id DESC LIMIT ?
                """,
                (args.lines,),
            )
            rows = cur.fetchall()
            if not rows:
                print(f"No execution logs found in {db_path}.")
            else:
                for r in reversed(rows):
                    tid = r["task_id"] or "system"
                    stream_t = r["stream_type"]
                    chunk = r["chunk"].rstrip("\r\n")
                    ts = r["timestamp"]
                    print(f"[{ts}] [{tid}] [{stream_t}] {chunk}")
    finally:
        conn.close()

    return 0


# ==============================================================================
# SUBCOMMAND: test-send
# ==============================================================================

def cmd_test_send(args: argparse.Namespace) -> int:
    """Handle `webhook-hub test-send` command."""
    import json
    import urllib.error
    import urllib.request
    from hub.security import generate_hmac_signature

    cli_overrides = {}
    if args.port:
        cli_overrides["server.port"] = args.port
    if args.host:
        cli_overrides["server.host"] = args.host
    config = load_config(config_path=args.config, env_path=args.env_file, cli_overrides=cli_overrides)

    secret = args.secret or config.security.webhook_secret or os.environ.get("WEBHOOK_SECRET", DEFAULT_VERIFY_SECRET)
    token = args.token or config.security.bearer_token or os.environ.get("WEBHOOK_BEARER_TOKEN", "")

    # Construct JSON payload
    if args.payload:
        if args.payload.startswith("@"):
            payload_file = Path(args.payload[1:])
            if not payload_file.is_file():
                print(f"Error: Payload file '{payload_file}' not found.", file=sys.stderr)
                return 1
            payload_str = payload_file.read_text(encoding="utf-8")
        else:
            payload_str = args.payload
    else:
        payload_obj = {
            "action": args.action,
            "command": args.command,
            "timestamp": time.time(),
            "source": "cli_test_send",
        }
        payload_str = json.dumps(payload_obj)

    try:
        # Validate that payload is parseable JSON
        json.loads(payload_str)
    except Exception as e:
        print(f"Error: Payload is not valid JSON: {e}", file=sys.stderr)
        return 1

    payload_bytes = payload_str.encode("utf-8")

    # Compute timestamp
    if args.stale_timestamp:
        ts = int(time.time()) - 350  # 350s in past, exceeds 300s window
    else:
        ts = int(time.time())

    # Build Headers
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "antigravity-webhook-hub-cli/1.0",
    }

    if secret:
        sig = generate_hmac_signature(secret, payload_bytes, ts)
        if args.tamper:
            # Tamper with the last character of the signature
            last_char = "0" if sig[-1] != "0" else "1"
            sig = sig[:-1] + last_char
        headers[config.security.signature_header] = sig
        headers[config.security.timestamp_header] = str(ts)

    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Build URL
    source_part = f"/{args.source}" if args.source else ""
    target_url = f"http://{args.host}:{args.port}/webhook{source_part}"

    print(f"Sending test webhook to {target_url}...")
    req = urllib.request.Request(target_url, data=payload_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            print(f"HTTP Status: {resp.status} {resp.reason}")
            try:
                parsed = json.loads(resp_body)
                print("Response JSON:")
                print(json.dumps(parsed, indent=2))
            except Exception:
                print(f"Response Body: {resp_body}")
            return 0
    except urllib.error.HTTPError as he:
        err_body = he.read().decode("utf-8", errors="replace")
        print(f"HTTP Error: {he.code} {he.reason}", file=sys.stderr)
        try:
            parsed = json.loads(err_body)
            print("Error Details:", file=sys.stderr)
            print(json.dumps(parsed, indent=2), file=sys.stderr)
        except Exception:
            print(f"Error Body: {err_body}", file=sys.stderr)
        return 1
    except urllib.error.URLError as ue:
        print(f"Network Error: {ue.reason}", file=sys.stderr)
        return 1


# ==============================================================================
# SUBCOMMAND: verify
# ==============================================================================

def cmd_verify(args: argparse.Namespace) -> int:
    """Handle `webhook-hub verify` command."""
    import subprocess
    project_root = Path(__file__).resolve().parent.parent
    verify_script = project_root / "scripts" / "verify_e2e.py"

    if not verify_script.is_file():
        print(f"Error: Standalone verification script not found at {verify_script}", file=sys.stderr)
        return 1

    secret = args.secret
    if secret == "verify_secret_32bytes_12345678":
        config = load_config()
        if config.security.webhook_secret:
            secret = config.security.webhook_secret

    cmd = [
        sys.executable,
        str(verify_script),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--secret",
        secret,
    ]

    print(f"Executing verification runner: {' '.join(cmd)}\n")
    proc = subprocess.run(cmd)
    return proc.returncode


# ==============================================================================
# SUBCOMMAND: review-contact
# ==============================================================================

def cmd_review_contact(args: argparse.Namespace) -> int:
    """Handle `webhook-hub review-contact` command."""
    import json
    from hub.contact_review.runner import execute_contact_review
    from hub.contact_review.notion_client import NotionPeopleClient

    payload_dict = {}

    if getattr(args, "task_id", None):
        from hub.db import DatabaseManager
        db_path = getattr(args, "db", None) or os.environ.get("DATABASE_PATH", "data/webhook_hub.db")
        db = DatabaseManager(db_path=db_path)
        task = db.get_task(args.task_id)
        if not task:
            print(f"Error: Task '{args.task_id}' not found in database.", file=sys.stderr)
            return 1
        raw_params = task.get("action_params_json") or task.get("action_params") or "{}"
        try:
            payload_dict = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
        except Exception:
            payload_dict = {}
    elif getattr(args, "payload", None):
        raw = args.payload
        if raw.startswith("@"):
            p_file = Path(raw[1:])
            if not p_file.is_file():
                print(f"Error: Payload file '{p_file}' not found.", file=sys.stderr)
                return 1
            raw = p_file.read_text(encoding="utf-8")
        try:
            payload_dict = json.loads(raw)
        except Exception as e:
            print(f"Error: Invalid JSON payload: {e}", file=sys.stderr)
            return 1
    else:
        payload_dict = {
            "name": getattr(args, "name", "Unknown Person"),
            "phone": getattr(args, "phone", "") or "",
            "email": getattr(args, "email", "") or "",
            "company": getattr(args, "company", "") or "",
            "title": getattr(args, "title", "") or "",
            "city": getattr(args, "city", "") or "",
            "country": getattr(args, "country", "") or "",
            "birthday": getattr(args, "birthday", "") or "",
            "url": getattr(args, "url", "") or "",
            "notes": getattr(args, "notes", "") or "",
            "slack_channel": getattr(args, "slack_channel", "") or "",
            "slack_thread_ts": getattr(args, "slack_thread_ts", "") or "",
        }

    client = None
    if getattr(args, "database_id", None):
        client = NotionPeopleClient(database_id=args.database_id)

    dry_run = getattr(args, "dry_run", False)
    notify_slack = not getattr(args, "no_slack", False)

    try:
        res = asyncio.run(
            execute_contact_review(
                payload=payload_dict,
                auto_apply=not dry_run,
                notify_slack=notify_slack,
                dry_run=dry_run,
                client=client,
            )
        )
    except Exception as e:
        print(f"Error during contact review: {e}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print("=" * 60)
        print(f" Antigravity Contact Review: {res.verdict.value.upper()}")
        print("=" * 60)
        print(f"  Target Contact:   {res.target_name}")
        if res.target_page_url:
            print(f"  Notion Page URL:  {res.target_page_url}")
        print(f"  Confidence:       {res.confidence_score}%")
        print(f"  SSOT Verified:    {'YES ✅' if res.ssot_verified else 'NO ⚠️'}")
        if res.slack_notified:
            print(f"  Slack Notified:   YES ✅")
        print(f"  Explanation:      {res.explanation}")
        if res.diffs:
            print("  Attribute Diffs:")
            for d in res.diffs:
                if d.action.value == "no_change":
                    continue
                print(f"    - [{d.action.value.upper()}] {d.field_name}: {d.new_value} (was: {d.old_value})")
        print("=" * 60)

    return 0 if not res.error else 1


def cmd_sweep(args: argparse.Namespace) -> int:
    """Handle `webhook-hub sweep` (or `pick-unprocessed`) command."""
    import json
    import urllib.request
    import urllib.error

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9423)
    target_url = f"http://{host}:{port}/tasks/sweep"

    dry_run = getattr(args, "dry_run", False)
    source = getattr(args, "source", None)
    limit = getattr(args, "limit", 100)
    stale_sec = getattr(args, "stale_seconds", 300)
    max_retries = getattr(args, "max_retries", 3)

    auto_retry = getattr(args, "auto_retry", True)

    payload_dict = {
        "dry_run": dry_run,
        "source": source,
        "limit": limit,
        "stale_seconds": stale_sec,
        "max_retries": max_retries,
        "auto_retry_interrupted": auto_retry,
    }

    # Try live gateway first if pid is alive and no custom --db was specified
    db_arg = getattr(args, "db", None)
    pid_path = Path(getattr(args, "pidfile", DEFAULT_PID_FILE))
    pid = _read_pid_file(pid_path)
    is_alive = _is_pid_running(pid) if pid is not None else False

    live_success = False
    sweep_data = None
    if is_alive and db_arg is None:
        try:
            req_data = json.dumps(payload_dict).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            req = urllib.request.Request(target_url, data=req_data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    sweep_data = json.loads(resp.read().decode("utf-8"))
                    live_success = True
        except Exception:
            pass

    # If live gateway wasn't reachable or not running, perform direct local DB sweep
    if not live_success:
        from hub.db import DatabaseManager
        db_path = getattr(args, "db", None)
        if not db_path:
            try:
                cfg = load_config(config_path=getattr(args, "config", None), env_path=getattr(args, "env_file", None))
                db_path = cfg.database.path
            except Exception:
                db_path = "data/webhook_hub.db"

        mgr = DatabaseManager(db_path)
        mgr.init_schema()

        if dry_run:
            unproc = mgr.get_unprocessed_tasks(
                source=source,
                stale_running_seconds=stale_sec,
                limit=limit,
            )
            sweep_data = {
                "status": "success",
                "mode": "offline_db_direct",
                "dry_run": True,
                "would_recover": unproc.get("counts", {}),
                "tasks": unproc.get("tasks", []),
                "orphaned_events": unproc.get("orphaned_events", []),
            }
        else:
            db_res = mgr.sweep_and_requeue_unprocessed(
                stale_running_seconds=stale_sec,
                max_retries=max_retries,
                auto_retry_interrupted=auto_retry,
                source=source,
                limit=limit,
            )
            sweep_data = {
                "status": "success",
                "mode": "offline_db_direct",
                "dry_run": False,
                "recovered": db_res,
            }
        mgr.close()

    if getattr(args, "json", False):
        print(json.dumps(sweep_data, indent=2))
        return 0

    print("==================================================")
    print(f" Antigravity Webhook Hub — Unprocessed Task Sweep {'[DRY RUN]' if dry_run else ''}")
    print("==================================================")
    mode_str = f"LIVE GATEWAY (http://{host}:{port})" if live_success else "OFFLINE DIRECT (SQLite SSOT)"
    print(f"  Mode:         {mode_str}")
    print(f"  Auto-Retry:   {'ENABLED' if auto_retry else 'DISABLED'}")
    if dry_run:
        would = sweep_data.get("would_recover", {})
        total = would.get("total_unprocessed", 0)
        print(f"  Pending:      {total} unprocessed messages/tasks found")
        print(f"    - Queued:                {would.get('queued', 0)}")
        print(f"    - Received:              {would.get('received', 0)}")
        print(f"    - Stale Running (Sleep): {would.get('stale_running', 0)}")
        print(f"    - Orphaned Events:       {would.get('orphaned_events', 0)}")
        print(f"    - Interrupted / Failed:  {would.get('recoverable_failed', 0)}")
        tasks = sweep_data.get("tasks", [])
        if tasks:
            print("\n  Task Candidates:")
            for t in tasks[:10]:
                print(f"    • {t.get('task_id')}: status={t.get('status')}, action={t.get('action_type')}, created={t.get('created_at')}")
            if len(tasks) > 10:
                print(f"    ... and {len(tasks) - 10} more.")
    else:
        rec = sweep_data.get("recovered", {})
        total_q = rec.get("total_queued", 0)
        stale_rec = rec.get("recovered_stale_running", 0)
        orph_rec = rec.get("recovered_orphaned_events", 0)
        failed_rec = rec.get("recovered_interrupted_failed", 0)
        recv_rec = rec.get("recovered_received_tasks", 0)
        print(f"  Status:       SUCCESS")
        print(f"  Total Queued: {total_q} tasks ready for dispatch")
        print(f"  Recovered:")
        print(f"    - Stale Running (Sleep): {stale_rec}")
        print(f"    - Received Tasks:        {recv_rec}")
        print(f"    - Orphaned Webhooks:     {orph_rec}")
        print(f"    - Interrupted / Failed:  {failed_rec}")
        if not live_success:
            print("\n  Note: Gateway daemon is stopped. Start daemon to execute queued tasks:")
            print("        ./bin/webhook-hub start -d")
    print("==================================================")
    return 0


# ==============================================================================
# SUBCOMMAND: dashboard (ui)
# ==============================================================================

def cmd_dashboard(args: argparse.Namespace) -> int:
    """Handle `webhook-hub dashboard` (or `ui`) command."""
    import urllib.request

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9423)
    local_url = f"http://{host}:{port}/dashboard"
    tunnel_url = "https://webhook.worldinspirelab.com:9423/dashboard"

    health_url = f"http://{host}:{port}/healthz"
    is_running = False
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status in (200, 503):
                is_running = True
    except Exception:
        pass

    if getattr(args, "json", False):
        import json
        print(json.dumps({
            "status": "running" if is_running else "stopped",
            "local_url": local_url,
            "tunnel_url": tunnel_url,
            "host": host,
            "port": port,
        }, indent=2))
        return 0

    print("==================================================")
    print("  Antigravity Webhook Hub — Observable Activity Console")
    print("==================================================")
    print(f"  Status:      {'ONLINE' if is_running else 'OFFLINE (Start with ./bin/webhook-hub start -d)'}")
    print(f"  Local URL:   {local_url}")
    print(f"  Tunnel URL:  {tunnel_url}")
    print("==================================================")

    if getattr(args, "open", False):
        try:
            import subprocess
            subprocess.run(["open", local_url], check=False)
            print("  Opened dashboard in default browser.")
        except Exception as e:
            print(f"  Could not open browser: {e}")

    return 0


def _add_dashboard_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Target server host")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Target server port")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    parser.add_argument("--json", action="store_true", help="Output URL info in JSON format")


# ==============================================================================
# SUBCOMMAND: rerun
# ==============================================================================

def cmd_rerun(args: argparse.Namespace) -> int:
    """Handle `webhook-hub rerun <task_id>` command."""
    import json
    import urllib.request
    import urllib.error

    task_id = args.task_id
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9423)
    target_url = f"http://{host}:{port}/tasks/{task_id}/rerun"

    db_arg = getattr(args, "db", None)

    # 1. Try running server via HTTP if no explicit --db was specified
    if db_arg is None:
        try:
            req = urllib.request.Request(target_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read().decode("utf-8"))
                    if getattr(args, "json", False):
                        print(json.dumps(body, indent=2))
                    else:
                        print(f"Task {task_id} successfully re-enqueued (live gateway).")
                    return 0
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"Error: Task {task_id} not found on server.", file=sys.stderr)
                return 1
        except Exception:
            pass

    # 2. Fallback to direct DB update if offline
    from hub.db import DatabaseManager
    db_path = getattr(args, "db", None)
    if not db_path:
        try:
            cfg = load_config(config_path=getattr(args, "config", None), env_path=getattr(args, "env_file", None))
            db_path = cfg.database.path
        except Exception:
            db_path = "data/webhook_hub.db"

    try:
        db_mgr = DatabaseManager(db_path)
        task = db_mgr.get_task(task_id)
        if not task:
            print(f"Error: Task {task_id} not found in database ({db_path}).", file=sys.stderr)
            return 1
        ok = db_mgr.rerun_task(task_id)
        if ok:
            if getattr(args, "json", False):
                print(json.dumps({"status": "queued", "task_id": task_id, "mode": "direct_sqlite"}, indent=2))
            else:
                print(f"Task {task_id} reset to 'queued' in SQLite ({db_path}).")
                print("Start the daemon to execute: ./bin/webhook-hub start -d")
            return 0
        else:
            print(f"Error: Failed to reset task {task_id}.", file=sys.stderr)
            return 1
    except Exception as err:
        print(f"Error accessing database {db_path}: {err}", file=sys.stderr)
        return 1


def _add_rerun_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", help="Task ID to re-enqueue and rerun")
    parser.add_argument("--host", default="127.0.0.1", help="Gateway host when connecting to live server")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Gateway port when connecting to live server")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file when offline")
    parser.add_argument("--config", "-c", type=str, default=None, help="Path to config.yaml file")
    parser.add_argument("--env-file", type=str, default=None, help="Path to .env file")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format")


def _add_sweep_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true", help="Inspect unprocessed tasks without executing or modifying state")
    parser.add_argument("--json", action="store_true", help="Format output as JSON for AI agents")
    parser.add_argument("--source", type=str, default=None, help="Filter sweep by source (e.g. slack_people, github, default)")
    parser.add_argument("--limit", type=int, default=100, help="Maximum number of tasks to recover in one sweep")
    parser.add_argument("--stale-seconds", type=int, default=300, help="Stale threshold for running tasks in seconds (default: 300s)")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum retry attempts for interrupted tasks")
    parser.add_argument("--auto-retry", dest="auto_retry", action="store_true", default=True, help="Auto-retry interrupted and timed out tasks (default: True)")
    parser.add_argument("--no-auto-retry", dest="auto_retry", action="store_false", help="Do not auto-retry interrupted tasks")
    parser.add_argument("--host", default="127.0.0.1", help="Gateway host when connecting to live server")
    parser.add_argument("--port", type=int, default=9423, help="Gateway port when connecting to live server")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file when offline")
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml file")
    parser.add_argument("--env-file", type=str, default=None, help="Path to .env file")
    parser.add_argument("--pidfile", default=DEFAULT_PID_FILE, help="Path to PID file")


def _add_review_contact_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", default=None, help="Load task payload from SQLite database by task ID")
    parser.add_argument("--payload", default=None, help="Raw JSON payload string or @filepath")
    parser.add_argument("--name", default="Unknown Person", help="Contact full name")
    parser.add_argument("--phone", default="", help="Contact phone number")
    parser.add_argument("--email", default="", help="Contact email address")
    parser.add_argument("--company", default="", help="Contact company / organization")
    parser.add_argument("--title", default="", help="Contact job title")
    parser.add_argument("--city", default="", help="Contact city")
    parser.add_argument("--country", default="", help="Contact country")
    parser.add_argument("--birthday", default="", help="Contact birthday (YYYY-MM-DD)")
    parser.add_argument("--url", default="", help="Contact URL / profile link")
    parser.add_argument("--notes", default="", help="Contact notes / important info")
    parser.add_argument("--database-id", default=None, help="Notion database ID override")
    parser.add_argument("--dry-run", "--no-apply", action="store_true", help="Simulate evaluation without modifying Notion")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--slack-channel", default=None, help="Slack channel override")
    parser.add_argument("--slack-thread-ts", default=None, help="Slack thread timestamp to reply to")
    parser.add_argument("--no-slack", action="store_true", help="Skip sending Slack notification")
    parser.add_argument("--db", default=None, help="Path to SQLite database file")


# ==============================================================================
# MAIN CLI ENTRY POINT & PARSER
# ==============================================================================

def _add_start_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Port to listen on")
    parser.add_argument("--daemon", "-d", action="store_true", help="Run detached in background")
    parser.add_argument("--db", default=None, help="Path to SQLite database")
    parser.add_argument("--secret", default=None, help="HMAC SHA-256 secret (overrides config)")
    parser.add_argument("--config", "-c", default=None, help="Path to config.yaml")
    parser.add_argument("--env-file", default=None, help="Path to .env file")
    parser.add_argument("--pidfile", default=DEFAULT_PID_FILE, help="Path to PID file")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log level")


def _add_stop_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pidfile", default=DEFAULT_PID_FILE, help="Path to PID file")
    parser.add_argument("--timeout", type=float, default=5.0, help="Graceful shutdown wait timeout in seconds")
    parser.add_argument("--force", "-f", action="store_true", help="Escalate immediately to SIGKILL")


def _add_status_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Target server host")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Target server port")
    parser.add_argument("--pidfile", default=DEFAULT_PID_FILE, help="Path to PID file")
    parser.add_argument("--json", action="store_true", help="Output status as structured JSON")


def _add_logs_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--follow", "-f", action="store_true", help="Stream live logs via SSE")
    parser.add_argument("--task", "-t", default=None, help="Filter logs by task ID")
    parser.add_argument("--lines", "-n", type=int, default=50, help="Number of historical lines to read")
    parser.add_argument("--host", default="127.0.0.1", help="Target server host for SSE")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Target server port for SSE")
    parser.add_argument("--db", default=None, help="Path to SQLite database for offline reading")
    parser.add_argument("--config", "-c", default=None, help="Path to config.yaml")


def _add_send_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--action", default="cli", help="Action type to request (cli, agent_signal)")
    parser.add_argument("--command", default="echo 'test webhook payload'", help="CLI command to execute")
    parser.add_argument("--payload", default=None, help="Custom JSON payload string or @filepath")
    parser.add_argument("--secret", default=None, help="HMAC SHA-256 secret (overrides config)")
    parser.add_argument("--token", default=None, help="Bearer authorization token")
    parser.add_argument("--host", default="127.0.0.1", help="Target server host")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Target server port")
    parser.add_argument("--source", default="", help="Optional webhook source path segment (e.g. github)")
    parser.add_argument("--tamper", action="store_true", help="Adversarial: intentionally tamper with HMAC signature")
    parser.add_argument("--stale-timestamp", action="store_true", help="Adversarial: send timestamp > 300s old")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP request timeout in seconds")
    parser.add_argument("--config", "-c", default=None, help="Path to config.yaml")
    parser.add_argument("--env-file", default=None, help="Path to .env file")


def _add_verify_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="127.0.0.1", help="Target server host")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Target server port")
    parser.add_argument("--secret", default="verify_secret_32bytes_12345678", help="HMAC secret for verification")


class _StartArgsNamespace:
    __slots__ = ("subcommand", "host", "port", "daemon", "db", "secret", "config", "env_file", "pidfile", "log_level")
    def __init__(self):
        self.subcommand = "start"
        self.host = "127.0.0.1"
        self.port = 9423
        self.daemon = False
        self.db = None
        self.secret = None
        self.config = None
        self.env_file = None
        self.pidfile = DEFAULT_PID_FILE
        self.log_level = "INFO"


def _parse_start_subargs(sub_args: list[str]) -> _StartArgsNamespace:
    args = _StartArgsNamespace()
    i = 0
    n = len(sub_args)
    while i < n:
        a = sub_args[i]
        if a == "--host" and i + 1 < n:
            i += 1
            args.host = sub_args[i]
        elif (a == "--port" or a == "-p") and i + 1 < n:
            i += 1
            try:
                args.port = int(sub_args[i])
            except ValueError:
                pass
        elif a in ("--daemon", "-d"):
            args.daemon = True
        elif a == "--db" and i + 1 < n:
            i += 1
            args.db = sub_args[i]
        elif a == "--secret" and i + 1 < n:
            i += 1
            args.secret = sub_args[i]
        elif (a == "--config" or a == "-c") and i + 1 < n:
            i += 1
            args.config = sub_args[i]
        elif a == "--env-file" and i + 1 < n:
            i += 1
            args.env_file = sub_args[i]
        elif a == "--pidfile" and i + 1 < n:
            i += 1
            args.pidfile = sub_args[i]
        elif a == "--log-level" and i + 1 < n:
            i += 1
            args.log_level = sub_args[i].upper()
        elif a in ("-h", "--help"):
            import argparse
            p = argparse.ArgumentParser(prog="webhook-hub start")
            _add_start_args(p)
            p.print_help()
            sys.exit(0)
        i += 1
    return args


def build_parser() -> Any:
    """Construct complete argument parser with all subcommands (for help and full inspection)."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="webhook-hub",
        description="Antigravity Webhook Hub — Zero-Footprint Ingress & Dispatcher CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", title="Subcommands", required=False)

    p_start = subparsers.add_parser("start", help="Start the webhook hub server")
    _add_start_args(p_start)

    p_stop = subparsers.add_parser("stop", help="Stop the running webhook hub server")
    _add_stop_args(p_stop)

    p_status = subparsers.add_parser("status", help="Check server liveness and metrics")
    _add_status_args(p_status)

    p_logs = subparsers.add_parser("logs", help="View or stream execution logs")
    _add_logs_args(p_logs)

    p_send = subparsers.add_parser("test-send", help="Send an authenticated test webhook request")
    _add_send_args(p_send)

    p_verify = subparsers.add_parser("verify", help="Execute the standalone E2E verification test suite")
    _add_verify_args(p_verify)

    p_review = subparsers.add_parser("review-contact", aliases=["contact-review"], help="Review and reconcile contact against Notion CRM (SSOT)")
    _add_review_contact_args(p_review)

    p_sweep = subparsers.add_parser("sweep", aliases=["pick-unprocessed", "recover"], help="Auto-pick and recover unprocessed messages and tasks")
    _add_sweep_args(p_sweep)

    p_dashboard = subparsers.add_parser("dashboard", aliases=["ui"], help="Open or display Webhook Hub Observable Dashboard URL")
    _add_dashboard_args(p_dashboard)

    p_rerun = subparsers.add_parser("rerun", help="Re-enqueue an existing task for re-execution")
    _add_rerun_args(p_rerun)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI execution entry point."""
    if argv is None:
        argv = sys.argv[1:]

    # If flags like --port or --db are provided without a subcommand, default to 'start'
    known_commands = {
        "start", "stop", "status", "logs", "test-send", "verify",
        "review-contact", "contact-review", "sweep", "pick-unprocessed", "recover",
        "dashboard", "ui", "rerun",
        "-h", "--help"
    }
    if argv and argv[0] not in known_commands and argv[0].startswith("-"):
        argv = ["start"] + argv

    if not argv or "-h" in argv or "--help" in argv:
        parser = build_parser()
        if not argv:
            parser.print_help()
            return 0
        try:
            parser.parse_args(argv)
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 0
        return 0

    subcommand = argv[0]
    sub_args = argv[1:]

    # Zero-overhead lightweight parser for 'start' to strictly maintain <30MB RSS budget
    if subcommand == "start":
        args = _parse_start_subargs(sub_args)
        return cmd_start(args)

    import argparse

    if subcommand == "stop":
        p = argparse.ArgumentParser(prog="webhook-hub stop")
        _add_stop_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "stop"
        del p
        gc.collect()
        return cmd_stop(args)

    elif subcommand == "status":
        p = argparse.ArgumentParser(prog="webhook-hub status")
        _add_status_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "status"
        del p
        gc.collect()
        return cmd_status(args)

    elif subcommand == "logs":
        p = argparse.ArgumentParser(prog="webhook-hub logs")
        _add_logs_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "logs"
        del p
        gc.collect()
        return cmd_logs(args)

    elif subcommand == "test-send":
        p = argparse.ArgumentParser(prog="webhook-hub test-send")
        _add_send_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "test-send"
        del p
        gc.collect()
        return cmd_test_send(args)

    elif subcommand == "verify":
        p = argparse.ArgumentParser(prog="webhook-hub verify")
        _add_verify_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "verify"
        del p
        gc.collect()
        return cmd_verify(args)

    elif subcommand in ("review-contact", "contact-review"):
        p = argparse.ArgumentParser(prog="webhook-hub review-contact")
        _add_review_contact_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "review-contact"
        del p
        gc.collect()
        return cmd_review_contact(args)

    elif subcommand in ("sweep", "pick-unprocessed", "recover"):
        p = argparse.ArgumentParser(prog="webhook-hub sweep")
        _add_sweep_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "sweep"
        del p
        gc.collect()
        return cmd_sweep(args)

    elif subcommand in ("dashboard", "ui"):
        p = argparse.ArgumentParser(prog="webhook-hub dashboard")
        _add_dashboard_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "dashboard"
        del p
        gc.collect()
        return cmd_dashboard(args)

    elif subcommand == "rerun":
        p = argparse.ArgumentParser(prog="webhook-hub rerun")
        _add_rerun_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "rerun"
        del p
        gc.collect()
        return cmd_rerun(args)

    else:
        parser = build_parser()
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
