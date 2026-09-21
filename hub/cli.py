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
- `schedule`: Manages delayed one-off and recurring cron schedules (list, create, trigger, pause, resume, delete).
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

# Ensure loopback traffic is never intercepted by macOS SystemConfiguration proxies
for _np_key in ("no_proxy", "NO_PROXY"):
    _curr = os.environ.get(_np_key, "")
    if "127.0.0.1" not in _curr or "localhost" not in _curr:
        os.environ[_np_key] = f"{_curr},127.0.0.1,localhost".strip(",")

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
    from hub.routes.webhook import register_webhook_routes
    from hub.server import AsyncHTTPServer

    try:
        gc.set_threshold(100, 5, 5)
    except Exception:
        pass

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
        db_mgr._conn.execute("PRAGMA temp_store = FILE;")
        db_mgr._conn.execute("PRAGMA threads = 0;")
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

    # 3.5. Initialize Resilient Task Scheduler (SSOT + Monotonic Sleep/Wake Recovery)
    from hub.scheduler import TaskScheduler
    scheduler = TaskScheduler(
        db=db_mgr,
        dispatcher=dispatcher,
        broker=broker,
        config=config,
    )
    await scheduler.start()

    # 4. Initialize Lightweight HTTP/1.1 Server
    server = AsyncHTTPServer(config.server)
    server._db = db_mgr
    server._scheduler = scheduler

    # Lazily wire management routes on demand to maintain minimal RSS footprint (<30MB)
    _lazy_loaded: set[str] = set()

    def _dynamic_route_resolver(method: str, path: str) -> None:
        p = path.lower()
        if (p.startswith("/tasks") or p.startswith("/activities") or p.startswith("/api/tasks") or p.startswith("/api/activities")) and "tasks" not in _lazy_loaded:
            _lazy_loaded.add("tasks")
            from hub.routes.tasks import register_task_routes
            register_task_routes(server, config, db_mgr, dispatcher, broker)
        elif (p.startswith("/schedules") or p.startswith("/api/schedules") or p.startswith("/api/v1/schedules") or p.startswith("/webhook/scheduler") or p.startswith("/webhook/cron")) and "schedules" not in _lazy_loaded:
            _lazy_loaded.add("schedules")
            from hub.routes.schedules import register_schedule_routes
            register_schedule_routes(server, config, db_mgr, scheduler, dispatcher, broker)
        elif (p in ("/healthz", "/ready", "/metrics", "/health")) and "obs" not in _lazy_loaded:
            _lazy_loaded.add("obs")
            from hub.routes.observability import register_observability_routes
            register_observability_routes(server, config, db_mgr, broker, dispatcher)
        elif (p.startswith("/events/stream") or p.startswith("/api/events/stream")) and "sse" not in _lazy_loaded:
            _lazy_loaded.add("sse")
            from hub.routes.sse import register_sse_routes
            register_sse_routes(server, config, db_mgr, broker)
        elif (p in ("/dashboard", "/ui") or p.startswith("/dashboard/")) and "dashboard" not in _lazy_loaded:
            _lazy_loaded.add("dashboard")
            from hub.routes.dashboard import register_dashboard_routes
            register_dashboard_routes(server, config, db_mgr, broker, dispatcher)
        elif (p.startswith("/api/agent-activities") or p.startswith("/agent-activities")) and "agent_activities" not in _lazy_loaded:
            _lazy_loaded.add("agent_activities")
            from hub.routes.agent_activities import register_agent_activities_routes
            register_agent_activities_routes(server, config, db_mgr, broker, dispatcher)
        elif ("uptime-kuma" in p or "alert" in p or "coolify" in p) and "kuma" not in _lazy_loaded:
            _lazy_loaded.add("kuma")
            from hub.routes.uptime_kuma import register_uptime_kuma_routes
            register_uptime_kuma_routes(server, config, db_mgr, dispatcher, broker)

    server._fallback_route_resolver = _dynamic_route_resolver

    # 5. Wire System Routes
    # Webhook routes are eagerly wired for immediate ingress availability
    register_webhook_routes(server, config, db_mgr, dispatcher, broker, scheduler)

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
    db_mgr.shrink_memory(truncate_wal=True)
    try:
        gc.freeze()
    except Exception:
        pass

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
        await scheduler.stop()
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
            stdin=subprocess.DEVNULL,
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
        with urllib.request.urlopen(req, timeout=5.0) as resp:
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
            budget_limit = float(healthz_data.get("system", {}).get("memory_budget_mb", 128.0))
            print("==================================================")
            print(" Antigravity Webhook Hub Status: RUNNING")
            print("==================================================")
            print(f"  PID:         {pid or 'N/A'}")
            print(f"  Endpoint:    http://{host}:{port}")
            print(f"  Status:      {status_str.upper()}")
            print(f"  Uptime:      {uptime:.1f}s")
            print(f"  Memory RSS:  {mem_rss:.2f} MB (Budget: < {budget_limit:.1f} MB)")
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
    tunnel_url = "https://webhook.worldinspirelab.com/dashboard"

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
# SUBCOMMAND: tasks
# ==============================================================================

def cmd_tasks(args: argparse.Namespace) -> int:
    """Handle `webhook-hub tasks` command to list, inspect, and review tasks."""
    import json
    import urllib.request
    import urllib.error
    import urllib.parse
    from hub.models import is_test_task

    status = getattr(args, "status", "all")
    real_only = getattr(args, "real_only", False)
    test_only = getattr(args, "test_only", False)
    source = getattr(args, "source", None)
    limit = getattr(args, "limit", 20)
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9423)
    db_arg = getattr(args, "db", None)

    tasks_data = None
    live_success = False

    # 1. Try querying live gateway HTTP API
    if db_arg is None:
        params: dict[str, Any] = {"limit": str(limit)}
        if status and status != "all":
            params["status"] = status
        if source:
            params["source"] = source
        if real_only:
            params["filter_test"] = "real"
        elif test_only:
            params["filter_test"] = "only"

        query_str = urllib.parse.urlencode(params)
        target_url = f"http://{host}:{port}/tasks?{query_str}"
        try:
            req = urllib.request.Request(target_url, method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    tasks_data = json.loads(resp.read().decode("utf-8"))
                    live_success = True
        except Exception:
            pass

    # 2. Offline fallback to SQLite directly
    if tasks_data is None:
        from hub.db import DatabaseManager
        db_path = db_arg
        if not db_path:
            try:
                cfg = load_config(config_path=getattr(args, "config", None), env_path=getattr(args, "env_file", None))
                db_path = cfg.database.path
            except Exception:
                db_path = "data/webhook_hub.db"

        try:
            db_mgr = DatabaseManager(db_path)
            where_clauses = []
            sql_params: list[Any] = []
            if status and status != "all":
                where_clauses.append("status = ?")
                sql_params.append(status)
            if source:
                where_clauses.append("source = ?")
                sql_params.append(source)

            where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            query = f"SELECT * FROM tasks {where_str} ORDER BY created_at DESC LIMIT ?"
            fetch_limit = limit * 4 if (real_only or test_only) else limit
            sql_params.append(fetch_limit)

            with db_mgr._lock:
                cur = db_mgr._conn.cursor()
                try:
                    cur.execute(query, tuple(sql_params))
                    rows = [dict(r) for r in cur.fetchall()]
                finally:
                    cur.close()
            parsed_tasks = []
            for r in rows:
                item = dict(r)
                is_test = is_test_task(item)
                item["is_test"] = is_test
                if real_only and is_test:
                    continue
                if test_only and not is_test:
                    continue
                parsed_tasks.append(item)
                if len(parsed_tasks) >= limit:
                    break

            tasks_data = {
                "tasks": parsed_tasks,
                "total": len(parsed_tasks),
                "count": len(parsed_tasks),
                "limit": limit,
            }
            db_mgr.close()
        except Exception as err:
            print(f"Error reading tasks from database ({db_path}): {err}", file=sys.stderr)
            return 1

    tasks_list = tasks_data.get("tasks", [])

    if getattr(args, "json", False):
        print(json.dumps(tasks_data, indent=2))
        return 0

    print("==================================================")
    print(" Antigravity Webhook Hub — Tasks Review")
    print("==================================================")
    mode_str = f"LIVE GATEWAY (http://{host}:{port})" if live_success else "OFFLINE DIRECT (SQLite SSOT)"
    print(f"  Mode:         {mode_str}")
    print(f"  Status:       {status}")
    filter_label = "Real Events Only" if real_only else ("Test Events Only" if test_only else "All Events")
    print(f"  Filter:       {filter_label}")
    print(f"  Total Found:  {len(tasks_list)} tasks")
    print("==================================================")

    if not tasks_list:
        print("  No tasks matching filter criteria.")
        return 0

    # Header
    print(f"{'TASK ID':<22} {'STATUS':<11} {'EXIT':<5} {'TYPE':<5} {'CREATED':<20} {'COMMAND / ACTION'}")
    print("-" * 88)
    for t in tasks_list:
        tid = t.get("task_id", "")
        st = t.get("status", "")
        code = str(t.get("exit_code") if t.get("exit_code") is not None else "-")
        is_t = "TEST" if t.get("is_test") else "REAL"
        created = str(t.get("created_at", ""))[:19]
        cmd = str(t.get("command") or t.get("target_action") or t.get("action_type") or "").strip().replace("\n", " ")
        if len(cmd) > 35:
            cmd = cmd[:32] + "..."
        print(f"{tid:<22} {st:<11} {code:<5} {is_t:<5} {created:<20} {cmd}")
    print("==================================================")
    return 0


def _add_tasks_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--status", choices=["all", "failed", "succeeded", "running", "queued", "timed_out"], default="all", help="Filter tasks by execution status")
    parser.add_argument("--real", dest="real_only", action="store_true", help="Filter to show only real business tasks (exclude synthetic/test events)")
    parser.add_argument("--test-only", dest="test_only", action="store_true", help="Filter to show only synthetic/test events")
    parser.add_argument("--source", type=str, default=None, help="Filter tasks by ingress source")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Maximum number of tasks to display (default: 20)")
    parser.add_argument("--host", default="127.0.0.1", help="Gateway host when connecting to live server")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Gateway port when connecting to live server")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file when offline")
    parser.add_argument("--config", "-c", type=str, default=None, help="Path to config.yaml file")
    parser.add_argument("--env-file", type=str, default=None, help="Path to .env file")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format for AI agents")


# ==============================================================================
# SUBCOMMAND: rerun
# ==============================================================================

def cmd_rerun(args: argparse.Namespace) -> int:
    """Handle `webhook-hub rerun <task_id>` or `webhook-hub rerun --failed` command."""
    import json
    import urllib.request
    import urllib.error
    from hub.models import is_test_task

    task_id = getattr(args, "task_id", None)
    rerun_failed = getattr(args, "failed", False)
    real_only = getattr(args, "real_only", False)
    dry_run = getattr(args, "dry_run", False)
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9423)
    db_arg = getattr(args, "db", None)

    if not task_id and not rerun_failed:
        print("Error: Specify a task_id to re-run, or use --failed to re-enqueue all failed tasks.", file=sys.stderr)
        print("Usage: ./bin/webhook-hub rerun <task_id>", file=sys.stderr)
        print("       ./bin/webhook-hub rerun --failed [--real-only] [--dry-run]", file=sys.stderr)
        return 1

    # Single task rerun
    if task_id:
        target_url = f"http://{host}:{port}/tasks/{task_id}/rerun"
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

        from hub.db import DatabaseManager
        db_path = db_arg
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

    # Batch re-run failed tasks
    from hub.db import DatabaseManager
    db_path = db_arg
    if not db_path:
        try:
            cfg = load_config(config_path=getattr(args, "config", None), env_path=getattr(args, "env_file", None))
            db_path = cfg.database.path
        except Exception:
            db_path = "data/webhook_hub.db"

    try:
        db_mgr = DatabaseManager(db_path)
        limit = getattr(args, "limit", 50)
        with db_mgr._lock:
            cur = db_mgr._conn.cursor()
            try:
                cur.execute(
                    "SELECT * FROM tasks WHERE status IN ('failed', 'timed_out') ORDER BY created_at DESC LIMIT ?",
                    (limit * 2,),
                )
                rows = [dict(r) for r in cur.fetchall()]
            finally:
                cur.close()
        candidates = []
        for r in rows:
            item = dict(r)
            if real_only and is_test_task(item):
                continue
            candidates.append(item)
            if len(candidates) >= limit:
                break
    except Exception as err:
        print(f"Error accessing database {db_path}: {err}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        if dry_run:
            print(json.dumps({"mode": "dry_run", "count": len(candidates), "task_ids": [c["task_id"] for c in candidates]}, indent=2))
            return 0

    print("==================================================")
    print(f" Antigravity Webhook Hub — Batch Rerun Failed Tasks {'[DRY RUN]' if dry_run else ''}")
    print("==================================================")
    print(f"  Filter:       {'Real Events Only' if real_only else 'All Failed Events'}")
    print(f"  Candidates:   {len(candidates)} task(s)")
    print("==================================================")

    if not candidates:
        print("  No failed tasks matching criteria found.")
        return 0

    if dry_run:
        for c in candidates:
            cmd = (c.get("command") or "")[:40].replace("\n", " ")
            print(f"  • {c['task_id']} [{c['status']}] (is_test={is_test_task(c)}): {cmd}")
        print("\n  Run without --dry-run to re-enqueue.")
        return 0

    re_enqueued = 0
    for c in candidates:
        tid = c["task_id"]
        re_ok = False
        if db_arg is None:
            try:
                target_url = f"http://{host}:{port}/tasks/{tid}/rerun"
                req = urllib.request.Request(target_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.status == 200:
                        re_ok = True
            except Exception:
                pass
        if not re_ok:
            re_ok = db_mgr.rerun_task(tid)
        if re_ok:
            re_enqueued += 1
            print(f"  [RE-ENQUEUED] {tid}")
        else:
            print(f"  [FAILED]      {tid}")

    print(f"\n  Successfully re-enqueued {re_enqueued}/{len(candidates)} task(s).")
    return 0


# ==============================================================================
# SUBCOMMAND: schedule / schedules
# ==============================================================================

def cmd_schedule(args: argparse.Namespace) -> int:
    """Handle `webhook-hub schedule` command for managing delayed & recurring tasks."""
    import datetime
    import json
    import urllib.error
    import urllib.parse
    import urllib.request
    import uuid

    raw_action = getattr(args, "action", "list") or "list"
    action = raw_action.lower().strip()
    schedule_id = getattr(args, "schedule_id", None) or getattr(args, "flag_schedule_id", None)

    # Convenience: if first positional argument is a schedule_id (e.g. `sch_...`)
    if action.startswith("sch_"):
        schedule_id = action
        action = "show"

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9423)
    db_arg = getattr(args, "db", None)
    is_json = getattr(args, "json", False)

    # Resolve DB path for offline operations
    db_path = db_arg
    if not db_path:
        try:
            cfg = load_config(config_path=getattr(args, "config", None), env_path=getattr(args, "env_file", None))
            db_path = cfg.database.path
        except Exception:
            db_path = "data/webhook_hub.db"

    # --------------------------------------------------------------------------
    # 1. LIST SCHEDULES
    # --------------------------------------------------------------------------
    if action in ("list", "ls"):
        status = getattr(args, "status", "all")
        schedule_type = getattr(args, "schedule_type", "all")
        search = getattr(args, "search", None)
        limit = getattr(args, "limit", 20)
        offset = getattr(args, "offset", 0)

        schedules_data: Optional[dict[str, Any]] = None
        live_success = False

        # Try live gateway HTTP API first
        if db_arg is None:
            params: dict[str, Any] = {"limit": str(limit), "offset": str(offset)}
            if status and status != "all":
                params["status"] = status
            if schedule_type and schedule_type != "all":
                params["type"] = schedule_type
            if search:
                params["q"] = search

            query_str = urllib.parse.urlencode(params)
            target_url = f"http://{host}:{port}/schedules?{query_str}"
            try:
                req = urllib.request.Request(target_url, method="GET")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        schedules_data = json.loads(resp.read().decode("utf-8"))
                        live_success = True
            except Exception:
                pass

        # Offline SQLite fallback
        if schedules_data is None:
            from hub.db import DatabaseManager
            try:
                db_mgr = DatabaseManager(db_path)
                status_filter = status if status != "all" else None
                type_filter = schedule_type if schedule_type != "all" else None
                schedules_list, total_count = db_mgr.list_schedules(
                    status=status_filter,
                    schedule_type=type_filter,
                    search=search,
                    limit=limit,
                    offset=offset,
                )
                stats = db_mgr.get_schedule_summary_stats()
                schedules_data = {
                    "status": "success",
                    "schedules": schedules_list,
                    "total": total_count,
                    "count": len(schedules_list),
                    "limit": limit,
                    "offset": offset,
                    "stats": stats,
                }
                db_mgr.close()
            except Exception as err:
                print(f"Error reading schedules from database ({db_path}): {err}", file=sys.stderr)
                return 1

        if is_json:
            print(json.dumps(schedules_data, indent=2))
            return 0

        sched_items = schedules_data.get("schedules", [])
        stats = schedules_data.get("stats", {})
        total_found = schedules_data.get("total", len(sched_items))

        print("==================================================")
        print(" Antigravity Webhook Hub — Scheduled Tasks")
        print("==================================================")
        mode_str = f"LIVE GATEWAY (http://{host}:{port})" if live_success else f"OFFLINE DIRECT (SQLite SSOT: {db_path})"
        print(f"  Mode:         {mode_str}")
        print(f"  Status:       {status}")
        print(f"  Type:         {schedule_type}")
        print(f"  Total Found:  {total_found} schedule(s)")
        if stats:
            print(f"  Active/Paused/Completed: {stats.get('active', 0)} / {stats.get('paused', 0)} / {stats.get('completed', 0)}")
        print("==================================================")

        if not sched_items:
            print("  No scheduled tasks matching filter criteria.")
            return 0

        def _col_pad(text: str, target_width: int) -> str:
            """Pad or truncate string considering East Asian display widths (CJK = 2 columns)."""
            import unicodedata
            w = 0
            chars = []
            for ch in text:
                cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
                if w + cw > target_width:
                    break
                chars.append(ch)
                w += cw
            truncated = "".join(chars)
            if len(truncated) < len(text) and target_width > 3:
                w = 0
                chars = []
                for ch in text:
                    cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
                    if w + cw > target_width - 3:
                        break
                    chars.append(ch)
                    w += cw
                truncated = "".join(chars) + "..."
                w += 3
            pad = target_width - w
            return truncated + (" " * max(0, pad))

        col_id = f"{'SCHEDULE ID':<22}"
        col_name = _col_pad("NAME", 28)
        col_type = f"{'TYPE':<10}"
        col_status = f"{'STATUS':<10}"
        col_next = f"{'NEXT RUN (UTC)':<20}"
        col_action = "ACTION"
        print(f"{col_id} {col_name} {col_type} {col_status} {col_next} {col_action}")
        print("-" * 105)
        for s in sched_items:
            sid = str(s.get("schedule_id", ""))
            name = str(s.get("name", ""))
            stype = str(s.get("schedule_type", ""))
            status_val = str(s.get("status", ""))
            next_run = str(s.get("next_run_at") or "-")[:19]
            action_desc = str(s.get("command") or s.get("target_action") or s.get("action_type") or "").strip().replace("\n", " ")

            c_sid = f"{sid:<22}"
            c_name = _col_pad(name, 28)
            c_stype = f"{stype:<10}"
            c_status = f"{status_val:<10}"
            c_next = f"{next_run:<20}"
            c_action = _col_pad(action_desc, 30) if len(action_desc) > 30 else action_desc
            print(f"{c_sid} {c_name} {c_stype} {c_status} {c_next} {c_action}")
        print("==================================================")
        return 0

    # --------------------------------------------------------------------------
    # 2. CREATE SCHEDULE
    # --------------------------------------------------------------------------
    elif action in ("create", "add", "new"):
        name = getattr(args, "name", None)
        if not name:
            print("Error: --name is required when creating a schedule.", file=sys.stderr)
            print("Usage: ./bin/webhook-hub schedule create --name \"...\" [--at | --delay | --cron] --command \"...\"", file=sys.stderr)
            return 1

        cron_expression = getattr(args, "cron_expression", None)
        schedule_type = "recurring" if cron_expression else "once"
        scheduled_at = getattr(args, "scheduled_at", None)
        delay = getattr(args, "delay", None)
        delay_seconds = getattr(args, "delay_seconds", None)
        action_type = getattr(args, "action_type", "cli") or "cli"
        command = getattr(args, "command", "") or ""
        target_action = getattr(args, "target_action", None) or command
        source = getattr(args, "source", "scheduler") or "scheduler"
        timezone = getattr(args, "timezone", "Asia/Bangkok") or "Asia/Bangkok"
        max_runs = getattr(args, "max_runs", None)

        action_params: dict[str, Any] = {}
        raw_params = getattr(args, "params", None)
        if raw_params:
            try:
                action_params = json.loads(raw_params)
            except Exception as e:
                print(f"Error: Invalid JSON in --params: {e}", file=sys.stderr)
                return 1

        if getattr(args, "prompt", None):
            action_params["prompt"] = getattr(args, "prompt")
        if getattr(args, "channel", None):
            action_params["channel"] = getattr(args, "channel")
        if getattr(args, "conversation_id", None):
            action_params["conversation_id"] = getattr(args, "conversation_id")

        payload = {
            "name": name,
            "schedule_type": schedule_type,
            "scheduled_at": scheduled_at,
            "delay": delay,
            "delay_seconds": delay_seconds,
            "cron_expression": cron_expression,
            "action_type": action_type,
            "command": command,
            "target_action": target_action,
            "action_params": action_params,
            "source": source,
            "timezone": timezone,
            "max_runs": max_runs,
        }

        # Try live gateway first
        if db_arg is None:
            target_url = f"http://{host}:{port}/schedules"
            try:
                body_bytes = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    target_url,
                    data=body_bytes,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status in (200, 201):
                        resp_data = json.loads(resp.read().decode("utf-8"))
                        if is_json:
                            print(json.dumps(resp_data, indent=2))
                        else:
                            sid = resp_data.get("schedule_id") or resp_data.get("schedule", {}).get("schedule_id")
                            next_run = resp_data.get("next_run_at") or resp_data.get("schedule", {}).get("next_run_at")
                            print(f"Schedule '{name}' successfully created (live gateway).")
                            print(f"  Schedule ID: {sid}")
                            print(f"  Type:        {schedule_type}")
                            print(f"  Next Run:    {next_run}")
                        return 0
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                if is_json:
                    print(json.dumps({"status": "error", "code": e.code, "message": err_text or e.reason}))
                else:
                    print(f"Error from server ({e.code}): {err_text or e.reason}", file=sys.stderr)
                return 1
            except Exception:
                pass

        # Offline SQLite direct insertion
        from hub.db import DatabaseManager
        from hub.scheduler import compute_next_cron_run, format_iso_utc, parse_schedule_time

        try:
            now_dt = datetime.datetime.now(datetime.timezone.utc)
            if schedule_type == "recurring":
                if not cron_expression:
                    print("Error: --cron expression is required for recurring schedule.", file=sys.stderr)
                    return 1
                next_dt = compute_next_cron_run(cron_expression, start_dt=now_dt, tz_name=timezone)
                next_run_iso = format_iso_utc(next_dt)
                scheduled_at_iso = None
            else:
                target_dt = parse_schedule_time(
                    scheduled_at=scheduled_at,
                    delay_seconds=delay_seconds,
                    delay=delay,
                    base_dt=now_dt,
                )
                next_run_iso = format_iso_utc(target_dt)
                scheduled_at_iso = next_run_iso

            db_mgr = DatabaseManager(db_path)
            sched_data = {
                "name": name,
                "source": source,
                "schedule_type": schedule_type,
                "cron_expression": cron_expression,
                "scheduled_at": scheduled_at_iso,
                "next_run_at": next_run_iso,
                "timezone": timezone,
                "action_type": action_type,
                "command": command,
                "target_action": target_action,
                "action_params": action_params,
                "status": "active",
                "max_runs": max_runs,
            }
            sid = db_mgr.insert_schedule(sched_data)
            db_mgr.close()

            if is_json:
                print(json.dumps({"status": "created", "schedule_id": sid, "schedule": sched_data, "mode": "direct_sqlite"}, indent=2))
            else:
                print(f"Schedule '{name}' created and persisted in SQLite SSOT ({db_path}).")
                print(f"  Schedule ID: {sid}")
                print(f"  Type:        {schedule_type}")
                print(f"  Next Run:    {next_run_iso}")
            return 0
        except Exception as err:
            print(f"Error creating schedule in database ({db_path}): {err}", file=sys.stderr)
            return 1

    # --------------------------------------------------------------------------
    # 3. TRIGGER SCHEDULE ON-DEMAND
    # --------------------------------------------------------------------------
    elif action in ("trigger", "run"):
        if not schedule_id:
            msg = "Specify a schedule_id to trigger."
            if is_json:
                print(json.dumps({"status": "error", "message": msg}))
            print(f"Error: {msg}", file=sys.stderr)
            print("Usage: ./bin/webhook-hub schedule trigger <schedule_id>", file=sys.stderr)
            return 1

        if db_arg is None:
            target_url = f"http://{host}:{port}/schedules/{schedule_id}/trigger"
            try:
                req = urllib.request.Request(
                    target_url,
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    if resp.status in (200, 202):
                        resp_data = json.loads(resp.read().decode("utf-8"))
                        if is_json:
                            print(json.dumps(resp_data, indent=2))
                        else:
                            tid = resp_data.get("result", {}).get("task_id") or resp_data.get("task_id", "")
                            print(f"Schedule {schedule_id} triggered on-demand (live gateway).")
                            if tid:
                                print(f"  Enqueued Task ID: {tid}")
                        return 0
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                if is_json:
                    print(json.dumps({"status": "error", "code": e.code, "message": err_text or e.reason}))
                else:
                    if e.code == 404:
                        print(f"Error: Schedule {schedule_id} not found on server.", file=sys.stderr)
                    else:
                        print(f"Error from server ({e.code}): {err_text or e.reason}", file=sys.stderr)
                return 1
            except Exception:
                pass

        # Offline SQLite direct execution
        from hub.db import DatabaseManager
        from hub.scheduler import compute_next_cron_run, format_iso_utc
        try:
            db_mgr = DatabaseManager(db_path)
            sched = db_mgr.get_schedule(schedule_id)
            if not sched:
                msg = f"Schedule {schedule_id} not found in database ({db_path})."
                if is_json:
                    print(json.dumps({"status": "error", "message": msg}))
                print(f"Error: {msg}", file=sys.stderr)
                return 1

            now_dt = datetime.datetime.now(datetime.timezone.utc)
            task_id = f"tsk_sch_{uuid.uuid4().hex[:12]}"
            event_id = f"evt_sch_{uuid.uuid4().hex[:12]}"

            action_params = dict(sched.get("action_params") or {})
            action_params["schedule_id"] = schedule_id
            action_params["schedule_name"] = sched.get("name")
            action_params["trigger_reason"] = "cli_manual_trigger"

            event_record = {
                "event_id": event_id,
                "source": sched.get("source", "scheduler"),
                "idempotency_key": f"{schedule_id}_{now_dt.strftime('%Y%m%d%H%M%S')}_{task_id}",
                "payload_hash": "",
                "headers_json": json.dumps({"X-Hub-Source": "scheduler", "X-Schedule-ID": schedule_id}),
                "raw_payload": json.dumps(action_params),
                "method": "POST",
                "path": f"/schedules/{schedule_id}/trigger",
                "status": "received",
            }
            db_mgr.insert_webhook_event(event_record)

            task_record = {
                "task_id": task_id,
                "event_id": event_id,
                "source": sched.get("source", "scheduler"),
                "action_type": sched.get("action_type", "cli"),
                "command": sched.get("command") or sched.get("target_action") or "",
                "target_action": sched.get("target_action") or sched.get("command") or "",
                "action_params_json": json.dumps(action_params),
                "status": "queued",
                "priority": int(sched.get("priority", 0) if "priority" in sched else 0),
                "timeout_seconds": int(sched.get("timeout_seconds", 300) if "timeout_seconds" in sched else 300),
                "retry_count": 0,
                "max_retries": 0,
                "schedule_id": schedule_id,
                "scheduled_at": sched.get("scheduled_at") or sched.get("next_run_at"),
            }
            db_mgr.insert_task(task_record)

            next_run_iso = None
            new_status = None
            if sched.get("schedule_type") == "once":
                new_status = "completed"
            elif sched.get("schedule_type") == "recurring":
                cron_expr = sched.get("cron_expression")
                if cron_expr:
                    try:
                        next_dt = compute_next_cron_run(cron_expr, start_dt=now_dt, tz_name=sched.get("timezone", "Asia/Bangkok"))
                        next_run_iso = format_iso_utc(next_dt)
                    except Exception:
                        pass

            db_mgr.record_schedule_execution(
                schedule_id=schedule_id,
                task_id=task_id,
                next_run_at=next_run_iso,
                status=new_status,
            )
            db_mgr.close()

            if is_json:
                print(json.dumps({"status": "triggered", "schedule_id": schedule_id, "task_id": task_id, "mode": "direct_sqlite"}, indent=2))
            else:
                print(f"Schedule {schedule_id} triggered and task queued in SQLite ({db_path}).")
                print(f"  Enqueued Task ID: {task_id}")
                print("  Start daemon to execute: ./bin/webhook-hub start -d")
            return 0
        except Exception as err:
            print(f"Error triggering schedule in database ({db_path}): {err}", file=sys.stderr)
            return 1

    # --------------------------------------------------------------------------
    # 4. PAUSE SCHEDULE
    # --------------------------------------------------------------------------
    elif action == "pause":
        if not schedule_id:
            msg = "Specify a schedule_id to pause."
            if is_json:
                print(json.dumps({"status": "error", "message": msg}))
            print(f"Error: {msg}", file=sys.stderr)
            print("Usage: ./bin/webhook-hub schedule pause <schedule_id>", file=sys.stderr)
            return 1

        if db_arg is None:
            target_url = f"http://{host}:{port}/schedules/{schedule_id}/pause"
            try:
                req = urllib.request.Request(target_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        if is_json:
                            print(json.dumps({"status": "paused", "schedule_id": schedule_id}, indent=2))
                        else:
                            print(f"Schedule {schedule_id} paused successfully (live gateway).")
                        return 0
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                if is_json:
                    print(json.dumps({"status": "error", "code": e.code, "message": err_text or e.reason}))
                else:
                    if e.code == 404:
                        print(f"Error: Schedule {schedule_id} not found on server.", file=sys.stderr)
                    else:
                        print(f"Error from server ({e.code}): {err_text or e.reason}", file=sys.stderr)
                return 1
            except Exception:
                pass

        from hub.db import DatabaseManager
        try:
            db_mgr = DatabaseManager(db_path)
            sched = db_mgr.get_schedule(schedule_id)
            if not sched:
                msg = f"Schedule {schedule_id} not found in database ({db_path})."
                if is_json:
                    print(json.dumps({"status": "error", "message": msg}))
                print(f"Error: {msg}", file=sys.stderr)
                return 1
            ok = db_mgr.update_schedule_status(schedule_id, "paused")
            db_mgr.close()
            if ok:
                if is_json:
                    print(json.dumps({"status": "paused", "schedule_id": schedule_id, "mode": "direct_sqlite"}, indent=2))
                else:
                    print(f"Schedule {schedule_id} paused in SQLite ({db_path}).")
                return 0
            else:
                msg = f"Failed to pause schedule {schedule_id}."
                if is_json:
                    print(json.dumps({"status": "error", "message": msg}))
                print(f"Error: {msg}", file=sys.stderr)
                return 1
        except Exception as err:
            print(f"Error accessing database ({db_path}): {err}", file=sys.stderr)
            return 1

    # --------------------------------------------------------------------------
    # 5. RESUME SCHEDULE
    # --------------------------------------------------------------------------
    elif action == "resume":
        if not schedule_id:
            msg = "Specify a schedule_id to resume."
            if is_json:
                print(json.dumps({"status": "error", "message": msg}))
            print(f"Error: {msg}", file=sys.stderr)
            print("Usage: ./bin/webhook-hub schedule resume <schedule_id>", file=sys.stderr)
            return 1

        if db_arg is None:
            target_url = f"http://{host}:{port}/schedules/{schedule_id}/resume"
            try:
                req = urllib.request.Request(target_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        if is_json:
                            print(json.dumps({"status": "active", "schedule_id": schedule_id}, indent=2))
                        else:
                            print(f"Schedule {schedule_id} resumed successfully (live gateway).")
                        return 0
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                if is_json:
                    print(json.dumps({"status": "error", "code": e.code, "message": err_text or e.reason}))
                else:
                    if e.code == 404:
                        print(f"Error: Schedule {schedule_id} not found on server.", file=sys.stderr)
                    else:
                        print(f"Error from server ({e.code}): {err_text or e.reason}", file=sys.stderr)
                return 1
            except Exception:
                pass

        from hub.db import DatabaseManager
        from hub.scheduler import compute_next_cron_run, format_iso_utc
        try:
            db_mgr = DatabaseManager(db_path)
            sched = db_mgr.get_schedule(schedule_id)
            if not sched:
                msg = f"Schedule {schedule_id} not found in database ({db_path})."
                if is_json:
                    print(json.dumps({"status": "error", "message": msg}))
                print(f"Error: {msg}", file=sys.stderr)
                return 1
            updates: dict[str, Any] = {"status": "active"}
            if sched.get("schedule_type") == "recurring" and sched.get("cron_expression"):
                try:
                    now_dt = datetime.datetime.now(datetime.timezone.utc)
                    next_dt = compute_next_cron_run(sched["cron_expression"], start_dt=now_dt, tz_name=sched.get("timezone", "Asia/Bangkok"))
                    updates["next_run_at"] = format_iso_utc(next_dt)
                except Exception:
                    pass
            ok = db_mgr.update_schedule(schedule_id, updates)
            db_mgr.close()
            if ok:
                if is_json:
                    print(json.dumps({"status": "active", "schedule_id": schedule_id, "mode": "direct_sqlite"}, indent=2))
                else:
                    print(f"Schedule {schedule_id} resumed in SQLite ({db_path}).")
                return 0
            else:
                print(f"Error: Failed to resume schedule {schedule_id}.", file=sys.stderr)
                return 1
        except Exception as err:
            print(f"Error accessing database ({db_path}): {err}", file=sys.stderr)
            return 1

    # --------------------------------------------------------------------------
    # 6. DELETE SCHEDULE
    # --------------------------------------------------------------------------
    elif action in ("delete", "remove", "rm", "cancel"):
        if not schedule_id:
            msg = "Specify a schedule_id to delete."
            if is_json:
                print(json.dumps({"status": "error", "message": msg}))
            print(f"Error: {msg}", file=sys.stderr)
            print("Usage: ./bin/webhook-hub schedule delete <schedule_id>", file=sys.stderr)
            return 1

        if db_arg is None:
            target_url = f"http://{host}:{port}/schedules/{schedule_id}"
            try:
                req = urllib.request.Request(target_url, method="DELETE")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        if is_json:
                            print(json.dumps({"status": "deleted", "schedule_id": schedule_id}, indent=2))
                        else:
                            print(f"Schedule {schedule_id} deleted successfully (live gateway).")
                        return 0
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                if is_json:
                    print(json.dumps({"status": "error", "code": e.code, "message": err_text or e.reason}))
                else:
                    if e.code == 404:
                        print(f"Error: Schedule {schedule_id} not found on server.", file=sys.stderr)
                    else:
                        print(f"Error from server ({e.code}): {err_text or e.reason}", file=sys.stderr)
                return 1
            except Exception:
                pass

        from hub.db import DatabaseManager
        try:
            db_mgr = DatabaseManager(db_path)
            sched = db_mgr.get_schedule(schedule_id)
            if not sched:
                msg = f"Schedule {schedule_id} not found in database ({db_path})."
                if is_json:
                    print(json.dumps({"status": "error", "message": msg}))
                print(f"Error: {msg}", file=sys.stderr)
                return 1
            ok = db_mgr.delete_schedule(schedule_id)
            db_mgr.close()
            if ok:
                if is_json:
                    print(json.dumps({"status": "deleted", "schedule_id": schedule_id, "mode": "direct_sqlite"}, indent=2))
                else:
                    print(f"Schedule {schedule_id} deleted from SQLite ({db_path}).")
                return 0
            else:
                msg = f"Failed to delete schedule {schedule_id}."
                if is_json:
                    print(json.dumps({"status": "error", "message": msg}))
                print(f"Error: {msg}", file=sys.stderr)
                return 1
        except Exception as err:
            print(f"Error accessing database ({db_path}): {err}", file=sys.stderr)
            return 1

    # --------------------------------------------------------------------------
    # 7. SHOW / DETAIL SCHEDULE
    # --------------------------------------------------------------------------
    elif action in ("show", "get", "inspect", "detail"):
        if not schedule_id:
            msg = "Specify a schedule_id to show."
            if is_json:
                print(json.dumps({"status": "error", "message": msg}))
            print(f"Error: {msg}", file=sys.stderr)
            print("Usage: ./bin/webhook-hub schedule show <schedule_id>", file=sys.stderr)
            return 1

        detail_data: Optional[dict[str, Any]] = None
        if db_arg is None:
            target_url = f"http://{host}:{port}/schedules/{schedule_id}"
            try:
                req = urllib.request.Request(target_url, method="GET")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        detail_data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                err_text = ""
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    pass
                if is_json:
                    print(json.dumps({"status": "error", "code": e.code, "message": err_text or e.reason}))
                else:
                    if e.code == 404:
                        print(f"Error: Schedule {schedule_id} not found on server.", file=sys.stderr)
                    else:
                        print(f"Error from server ({e.code}): {err_text or e.reason}", file=sys.stderr)
                return 1
            except Exception:
                pass

        if detail_data is None:
            from hub.db import DatabaseManager
            try:
                db_mgr = DatabaseManager(db_path)
                sched = db_mgr.get_schedule(schedule_id)
                if not sched:
                    msg = f"Schedule {schedule_id} not found in database ({db_path})."
                    if is_json:
                        print(json.dumps({"status": "error", "message": msg}))
                    print(f"Error: {msg}", file=sys.stderr)
                    return 1
                history = db_mgr.get_schedule_history(schedule_id, limit=10)
                detail_data = {"status": "success", "schedule": sched, "history": history}
                db_mgr.close()
            except Exception as err:
                print(f"Error accessing database ({db_path}): {err}", file=sys.stderr)
                return 1

        if is_json:
            print(json.dumps(detail_data, indent=2))
            return 0

        sched = detail_data.get("schedule", {})
        history = detail_data.get("history", [])

        print("==================================================")
        print(f" Schedule Detail: {sched.get('schedule_id')}")
        print("==================================================")
        print(f"  Name:           {sched.get('name')}")
        print(f"  Status:         {sched.get('status')}")
        print(f"  Type:           {sched.get('schedule_type')}")
        if sched.get("cron_expression"):
            print(f"  Cron:           {sched.get('cron_expression')}")
        print(f"  Next Run:       {sched.get('next_run_at') or '-'}")
        print(f"  Last Run:       {sched.get('last_run_at') or '-'}")
        print(f"  Total Runs:     {sched.get('total_runs', 0)}")
        print(f"  Action Type:    {sched.get('action_type')}")
        print(f"  Command:        {sched.get('command') or '-'}")
        print(f"  Target Action:  {sched.get('target_action') or '-'}")
        if sched.get("last_error"):
            print(f"  Last Error:     {sched.get('last_error')}")
        print("==================================================")
        if history:
            print(" Execution History (Recent Tasks):")
            for h in history:
                print(f"  • {h.get('task_id')} [{h.get('status')}] exit={h.get('exit_code', '-')} created={h.get('created_at')}")
            print("==================================================")
        return 0

    else:
        print(f"Error: Unknown schedule action '{action}'.", file=sys.stderr)
        print("Available actions: list, create, trigger, pause, resume, delete, show", file=sys.stderr)
        return 1


# ==============================================================================
# MAC SETUP & LAUNCHD SERVICE MANAGEMENT
# ==============================================================================

def cmd_setup(args: Any) -> int:
    """Zero-friction setup wizard for macOS & Linux developers and AI agents."""
    import platform
    import secrets

    print("=" * 60)
    print("  Antigravity Webhook Hub — Environment Setup Wizard")
    print("=" * 60)

    project_dir = Path(getattr(args, "dir", None) or Path(__file__).resolve().parent.parent).resolve()
    os_name = platform.system()
    machine = platform.machine()
    python_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    print(f"\n[1/4] Platform Diagnosis:")
    print(f"  • Operating System: {os_name} ({machine})")
    print(f"  • Python Runtime:   {python_ver} ({sys.executable})")
    print(f"  • Project Root:     {project_dir}")

    if sys.version_info < (3, 10):
        print(f"\n  [ERROR] Python >= 3.10 is required (found {python_ver}).", file=sys.stderr)
        return 1

    # [2/4] Directory Scaffolding
    print(f"\n[2/4] Directory Scaffolding:")
    for dir_name in ("data", "events", "backups"):
        d = project_dir / dir_name
        d.mkdir(parents=True, exist_ok=True)
        print(f"  • Verified directory: {dir_name}/")

    # [3/4] Cryptographic Configuration Provisioning
    print(f"\n[3/4] Configuration Provisioning:")
    env_file = project_dir / ".env"
    if env_file.is_file() and not getattr(args, "force", False):
        print(f"  • Found existing '.env' configuration (preserving).")
        print(f"    (Run with --force to regenerate fresh cryptographic keys)")
    else:
        hmac_secret = secrets.token_hex(32)
        bearer_token = secrets.token_hex(32)
        dash_token = secrets.token_hex(16)
        port = getattr(args, "port", 9423) or 9423
        host = getattr(args, "host", "127.0.0.1") or "127.0.0.1"

        content = f"""# Antigravity Webhook Hub Configuration
# Generated automatically by 'webhook-hub setup'
PORT={port}
HOST={host}
WEBHOOK_SECRET={hmac_secret}
BEARER_TOKEN={bearer_token}
DATABASE_PATH=data/webhook_hub.db
DATABASE_BUSY_TIMEOUT_MS=5000
DASHBOARD_AUTH_TOKEN={dash_token}
"""
        env_file.write_text(content, encoding="utf-8")
        try:
            env_file.chmod(0o600)
        except OSError:
            pass

        print(f"  • Generated secure '.env' file with permissions 0600")
        print(f"    - HMAC Secret:   {hmac_secret[:8]}...{hmac_secret[-8:]} (64 hex chars)")
        print(f"    - Bearer Token:  {bearer_token[:8]}...{bearer_token[-8:]} (64 hex chars)")
        print(f"    - Ingress Port:  {port}")
        print(f"    - Database:      data/webhook_hub.db")

    # [4/4] Next Steps Guide
    print(f"\n[4/4] Setup Complete! Ready for use:")
    print(f"  1. Start Foreground:       ./bin/webhook-hub start")
    if os_name == "Darwin":
        print(f"  2. Install macOS Daemon:   ./bin/webhook-hub service install")
        print(f"  3. Check Daemon Status:    ./bin/webhook-hub service status")
    else:
        print(f"  2. Start Background:       ./bin/webhook-hub start --daemon")
        print(f"  3. Check Server Status:    ./bin/webhook-hub status")
    print(f"  4. Send Test Ingress:      ./bin/webhook-hub test-send")
    print(f"  5. Open Web Dashboard:     ./bin/webhook-hub dashboard --open")
    print(f"  6. Run Full Verification:  ./bin/webhook-hub verify")
    print("=" * 60)
    return 0


def _add_setup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing .env configuration with fresh keys")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Port to configure (default: 9423)")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    parser.add_argument("--dir", default=None, help="Target project root directory (default: auto-detected)")


def cmd_service(args: Any) -> int:
    """Manage native macOS launchd LaunchAgent background daemon service."""
    import subprocess
    import platform

    action = getattr(args, "action", "status") or "status"
    label = getattr(args, "label", "com.antigravity.webhook-hub") or "com.antigravity.webhook-hub"

    if platform.system() != "Darwin":
        print("Error: 'service' commands manage macOS launchd LaunchAgents and are only supported on macOS.", file=sys.stderr)
        print("On Linux systems, consider using systemd or running with './bin/webhook-hub start --daemon'.", file=sys.stderr)
        return 1

    project_dir = Path(getattr(args, "dir", None) or Path(__file__).resolve().parent.parent).resolve()
    bin_path = project_dir / "bin" / "webhook-hub"
    python_bin = sys.executable
    launch_agents_dir = Path(getattr(args, "launch_agents_dir", None) or (Path.home() / "Library" / "LaunchAgents")).resolve()
    plist_path = launch_agents_dir / f"{label}.plist"

    # On macOS, launchd has strict TCC sandbox restrictions and cannot write StandardOutPath/StandardErrorPath
    # directly into ~/Documents or ~/Desktop. Standard macOS convention places user LaunchAgent logs under ~/Library/Logs/.
    if getattr(args, "launch_agents_dir", None) or getattr(args, "dir", None):
        out_log = project_dir / "webhook-hub.log"
        err_log = project_dir / "webhook-hub-err.log"
    else:
        logs_dir = Path.home() / "Library" / "Logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        out_log = logs_dir / "webhook-hub.log"
        err_log = logs_dir / "webhook-hub-err.log"
        local_out = project_dir / "webhook-hub.log"
        local_err = project_dir / "webhook-hub-err.log"
        try:
            if not local_out.exists() and not local_out.is_symlink():
                local_out.symlink_to(out_log)
            if not local_err.exists() and not local_err.is_symlink():
                local_err.symlink_to(err_log)
        except Exception:
            pass

    pid_file = project_dir / DEFAULT_PID_FILE

    def _is_service_loaded() -> tuple[bool, Optional[int]]:
        try:
            res = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] == label:
                        pid_str = parts[0]
                        pid = int(pid_str) if pid_str.isdigit() else None
                        return True, pid
        except Exception:
            pass
        return False, None

    if action in ("install", "enable"):
        print(f"Installing macOS LaunchAgent '{label}'...")
        launch_agents_dir.mkdir(parents=True, exist_ok=True)

        port_arg = getattr(args, "port", None)
        port_flags = f"        <string>--port</string>\n        <string>{port_arg}</string>\n" if port_arg else ""

        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_bin}</string>
        <string>-B</string>
        <string>{bin_path}</string>
        <string>start</string>
        <string>--host</string>
        <string>127.0.0.1</string>
{port_flags}        <string>--db</string>
        <string>data/webhook_hub.db</string>
        <string>--pidfile</string>
        <string>.webhook-hub.pid</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_dir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardInPath</key>
    <string>/dev/null</string>
    <key>StandardOutPath</key>
    <string>{out_log}</string>
    <key>StandardErrorPath</key>
    <string>{err_log}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>ProcessType</key>
    <string>Standard</string>
</dict>
</plist>
"""
        is_loaded, _ = _is_service_loaded()
        if is_loaded:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
            subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True)

        if pid_file.is_file():
            old_pid = _read_pid_file(pid_file)
            if old_pid and _is_pid_running(old_pid):
                try:
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(0.5)
                except OSError:
                    pass

        plist_path.write_text(plist_content, encoding="utf-8")

        load_res = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], capture_output=True, text=True)
        if load_res.returncode != 0:
            load_res = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)
        if load_res.returncode != 0 and not _is_service_loaded()[0]:
            print(f"Error loading launchd plist: {load_res.stderr.strip()}", file=sys.stderr)
            return load_res.returncode

        time.sleep(1.2)
        is_running, running_pid = _is_service_loaded()
        if not running_pid and pid_file.is_file():
            running_pid = _read_pid_file(pid_file)

        print("=" * 60)
        print(f"  macOS LaunchAgent '{label}' Installed Successfully!")
        print("=" * 60)
        print(f"  • Plist Location:     {plist_path}")
        print(f"  • Python Interpreter: {python_bin}")
        print(f"  • Project Directory:  {project_dir}")
        print(f"  • Daemon Status:      {'RUNNING (PID: ' + str(running_pid) + ')' if running_pid else 'LOADED'}")
        print(f"  • Log File:           {out_log}")
        print(f"  • Error Log:          {err_log}")
        print(f"  • Auto-Start:         Enabled on user login with automatic restart on exit")
        print("=" * 60)
        return 0

    elif action in ("uninstall", "disable"):
        print(f"Uninstalling macOS LaunchAgent '{label}'...")
        if plist_path.is_file():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
            subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True)
            try:
                plist_path.unlink()
                print(f"  • Removed plist file: {plist_path}")
            except OSError as e:
                print(f"  • Warning: failed to delete plist: {e}")
        else:
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
            subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True)

        if pid_file.is_file():
            old_pid = _read_pid_file(pid_file)
            if old_pid and _is_pid_running(old_pid):
                try:
                    os.kill(old_pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    pid_file.unlink()
                except OSError:
                    pass

        print(f"  • Service '{label}' successfully uninstalled and disabled.")
        return 0

    elif action == "restart":
        print(f"Restarting macOS LaunchAgent '{label}'...")
        if not plist_path.is_file():
            print(f"Error: Plist {plist_path} not found. Run './bin/webhook-hub service install' first.", file=sys.stderr)
            return 1
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        if pid_file.is_file():
            old_pid = _read_pid_file(pid_file)
            if old_pid and _is_pid_running(old_pid):
                try:
                    os.kill(old_pid, signal.SIGTERM)
                    time.sleep(0.5)
                except OSError:
                    pass
                if _is_pid_running(old_pid):
                    try:
                        os.kill(old_pid, signal.SIGKILL)
                        time.sleep(0.2)
                    except OSError:
                        pass
        time.sleep(1.0)
        res = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], capture_output=True, text=True)
        if res.returncode != 0:
            res = subprocess.run(["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True)
        if res.returncode != 0 and not _is_service_loaded()[0]:
            print(f"Error reloading service: {res.stderr.strip()}", file=sys.stderr)
            return res.returncode
        time.sleep(1.0)
        is_running, running_pid = _is_service_loaded()
        print(f"Service restarted. Status: {'RUNNING (PID: ' + str(running_pid) + ')' if running_pid else 'LOADED'}")
        return 0

    elif action == "status":
        is_loaded, running_pid = _is_service_loaded()
        if not running_pid and pid_file.is_file():
            p = _read_pid_file(pid_file)
            if p and _is_pid_running(p):
                running_pid = p

        print("=" * 60)
        print(f"  macOS LaunchAgent Status: {label}")
        print("=" * 60)
        print(f"  • Plist Installed: {'YES (' + str(plist_path) + ')' if plist_path.is_file() else 'NO'}")
        print(f"  • launchd Loaded:  {'YES' if is_loaded else 'NO'}")
        print(f"  • Process State:   {'RUNNING (PID: ' + str(running_pid) + ')' if running_pid else 'STOPPED'}")
        if running_pid:
            rss = _get_process_rss_mb(running_pid)
            svc_budget = float(os.environ.get("MEMORY_BUDGET_MB", 128.0))
            print(f"  • Process RSS:     {rss:.2f} MB (Budget: < {svc_budget:.1f} MB)")
        print(f"  • Log File:        {out_log}")
        print(f"  • Error Log:       {err_log}")
        print("=" * 60)
        return 0 if running_pid else 1

    elif action == "logs":
        lines = getattr(args, "lines", 30) or 30
        print(f"--- Last {lines} lines of stdout ({out_log}) ---")
        if out_log.is_file():
            content = out_log.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in content[-lines:]:
                print(line)
        else:
            print("(No stdout log file found)")

        print(f"\n--- Last {lines} lines of stderr ({err_log}) ---")
        if err_log.is_file():
            content = err_log.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in content[-lines:]:
                print(line)
        else:
            print("(No stderr log file found)")
        return 0

    return 0


def _add_service_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "action",
        choices=["install", "enable", "uninstall", "disable", "restart", "status", "logs"],
        help="Service action to perform (install, uninstall, restart, status, logs)",
    )
    parser.add_argument(
        "--label",
        default="com.antigravity.webhook-hub",
        help="LaunchAgent label (default: com.antigravity.webhook-hub)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help="Override HTTP port for the daemon",
    )
    parser.add_argument(
        "--lines",
        "-n",
        type=int,
        default=30,
        help="Number of log lines to show with 'logs' action",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Target project root directory (default: auto-detected)",
    )
    parser.add_argument(
        "--launch-agents-dir",
        default=None,
        help="Directory where LaunchAgent plists are stored (default: ~/Library/LaunchAgents)",
    )


def _add_rerun_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("task_id", nargs="?", default=None, help="Task ID to re-enqueue and rerun")
    parser.add_argument("--failed", action="store_true", help="Re-enqueue all failed and timed-out tasks")
    parser.add_argument("--real-only", action="store_true", default=False, help="When using --failed, only re-run real business tasks (exclude synthetic/test events)")
    parser.add_argument("--dry-run", action="store_true", help="Preview which tasks would be re-enqueued without modifying state")
    parser.add_argument("--limit", type=int, default=50, help="Maximum number of failed tasks to re-enqueue")
    parser.add_argument("--host", default="127.0.0.1", help="Gateway host when connecting to live server")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Gateway port when connecting to live server")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file when offline")
    parser.add_argument("--config", "-c", type=str, default=None, help="Path to config.yaml file")
    parser.add_argument("--env-file", type=str, default=None, help="Path to .env file")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format")


def _add_schedule_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "action",
        nargs="?",
        default="list",
        help="Schedule action: list (default), trigger, pause, resume, delete, create, show",
    )
    parser.add_argument(
        "schedule_id",
        nargs="?",
        default=None,
        help="Target Schedule ID for trigger, pause, resume, delete, or show",
    )
    # Query & filter options
    parser.add_argument("--status", default="all", help="Filter schedules by status (all, active, paused, completed, cancelled, failed)")
    parser.add_argument("--type", "--schedule-type", dest="schedule_type", default="all", help="Filter by schedule type (all, once, recurring)")
    parser.add_argument("--search", "-q", default=None, help="Search schedules by keyword in name, id, or command")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Maximum number of schedules to display (default: 20)")
    parser.add_argument("--offset", type=int, default=0, help="Pagination offset")

    # Schedule ID flag alternative
    parser.add_argument("--id", "--schedule-id", dest="flag_schedule_id", type=str, default=None, help="Target schedule ID flag alternative")

    # Creation options
    parser.add_argument("--name", type=str, default=None, help="Schedule name or title (required for create)")
    parser.add_argument("--at", "--scheduled-at", dest="scheduled_at", type=str, default=None, help="Target execution ISO datetime for delayed one-off task")
    parser.add_argument("--delay", type=str, default=None, help="Relative delay string (e.g. '2d', '12h', '30m', '45s')")
    parser.add_argument("--delay-seconds", type=float, default=None, help="Relative delay in seconds")
    parser.add_argument("--cron", "--cron-expression", dest="cron_expression", type=str, default=None, help="5-field cron expression for recurring schedule")
    parser.add_argument("--action-type", "--action", dest="action_type", type=str, default="cli", help="Action type to dispatch (cli, antigravity, contact_review)")
    parser.add_argument("--command", type=str, default="", help="Command line string or target task payload")
    parser.add_argument("--target-action", type=str, default=None, help="Target action name")
    parser.add_argument("--prompt", type=str, default=None, help="Prompt text for antigravity agent actions")
    parser.add_argument("--channel", type=str, default=None, help="Target channel for agent actions")
    parser.add_argument("--conversation-id", type=str, default=None, help="Target conversation ID for agent actions")
    parser.add_argument("--params", type=str, default=None, help="JSON-encoded parameters dictionary")
    parser.add_argument("--source", type=str, default="scheduler", help="Ingress source label (default: scheduler)")
    parser.add_argument("--timezone", type=str, default="Asia/Bangkok", help="Schedule timezone (default: Asia/Bangkok)")
    parser.add_argument("--max-runs", type=int, default=None, help="Max execution runs for recurring schedule")

    # Environment & Connection options
    parser.add_argument("--host", default="127.0.0.1", help="Gateway host when connecting to live server")
    parser.add_argument("--port", "-p", type=int, default=9423, help="Gateway port when connecting to live server")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file when offline")
    parser.add_argument("--config", "-c", type=str, default=None, help="Path to config.yaml file")
    parser.add_argument("--env-file", type=str, default=None, help="Path to .env file")
    parser.add_argument("--json", action="store_true", help="Output result in JSON format for AI agents")


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


def _add_antigravity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "action",
        nargs="?",
        default="doctor",
        choices=["doctor", "status", "health", "pull-up", "resuscitate", "wake", "quota", "warmup"],
        help="Antigravity command: 'doctor' (diagnostics), 'quota' (5h/weekly quota monitor), 'warmup' (ping 1-token refresh), 'pull-up' (revive stalled sessions)",
    )
    parser.add_argument(
        "--conversation", "-c",
        default=None,
        help="Target specific conversation ID for resuscitation",
    )
    parser.add_argument(
        "--account", "-a",
        default=None,
        help="Target specific account email for quota or warmup",
    )
    parser.add_argument(
        "--bucket", "-b",
        default=None,
        help="Target specific bucket ID (e.g. gemini-5h, 3p-5h, gemini-weekly, 3p-weekly)",
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force warmup immediately, ignoring 100%% threshold or cooldown timer",
    )
    parser.add_argument(
        "--all-accounts",
        action="store_true",
        default=None,
        help="Warm up all idle accounts across the fleet (default True)",
    )
    parser.add_argument(
        "--active-only",
        action="store_true",
        default=False,
        help="Warm up active account only, skipping standby accounts",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Custom pull-up message content to send",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report stalled sessions without sending messages",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )


def _add_catchup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--channel", default="C0C1B86AMCN", help="Slack channel ID to scan")
    parser.add_argument("--limit", type=int, default=50, help="Maximum number of recent messages to scan")
    parser.add_argument("--dry-run", action="store_true", help="Inspect offline tasks without dispatching")
    parser.add_argument("--execute", action="store_true", help="Execute live catch-up dispatch for unfulfilled tasks")


def cmd_catchup(args: Any) -> int:
    """Reconcile and catch up unfulfilled offline tasks from Slack #input_agent."""
    from hub.antigravity.reconciler import SlackReconciler
    from hub.db import DatabaseManager

    db_path = getattr(args, "db", "data/webhook_hub.db")
    db = DatabaseManager(db_path=db_path)
    reconciler = SlackReconciler(db=db)

    channel = getattr(args, "channel", "C0C1B86AMCN")
    limit = getattr(args, "limit", 50)
    dry_run = getattr(args, "dry_run", False)
    execute = getattr(args, "execute", False)
    if not dry_run and not execute:
        execute = True

    print(f"🔄 Native Slack Reconciler & Offline Auto Catch-up (channel={channel}, limit={limit}, dry_run={dry_run})")
    res = reconciler.run_catchup(channel=channel, limit=limit, dry_run=dry_run, execute=execute)

    unfulfilled_cnt = res.get("unfulfilled_count", 0)
    dispatched_cnt = len(res.get("dispatched", []))
    skipped_cnt = len(res.get("skipped", []))

    if unfulfilled_cnt == 0:
        print("✅ No unfulfilled offline tasks found. All threads are reconciled and complete!")
        return 0

    print(f"📋 Found {unfulfilled_cnt} unfulfilled offline task thread(s):")
    for d in res.get("dispatched", []):
        print(f"  🚀 Dispatched: thread_ts={d.get('thread_ts')}, task_id={d.get('task_id')}")
    for s in res.get("skipped", []):
        reason = s.get("reason", "unknown")
        print(f"  ⏸️  Skipped: thread_ts={s.get('thread_ts')}, reason={reason}")

    if dry_run:
        print(f"ℹ️  Dry run complete. {unfulfilled_cnt} task(s) inspected, 0 dispatched.")
    else:
        print(f"🎉 Reconcile complete. {dispatched_cnt} task(s) dispatched, {skipped_cnt} skipped/active.")

    return 0


def cmd_antigravity(args: Any) -> int:
    """Execute Antigravity doctor diagnostics or session resuscitation / pull-up."""
    import json
    import time
    from hub.antigravity.watchdog import AntigravityWatchdog, StalledSessionInfo
    from hub.antigravity.result_delivery import resolve_transcript_path
    from hub.db import DatabaseManager
    from hub.config import load_config

    action = getattr(args, "action", "doctor") or "doctor"
    is_json = getattr(args, "json", False)

    config_path = getattr(args, "config", None)
    env_file = getattr(args, "env_file", None)
    cfg = load_config(config_path, env_file)

    db_path = getattr(args, "db", None) or cfg.database.path
    db = DatabaseManager(db_path)

    quota_sentinel = None
    if getattr(cfg, "antigravity_quota", None) and cfg.antigravity_quota.enabled:
        try:
            from hub.antigravity.quota_sentinel import AntigravityQuotaSentinel
            quota_sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        except Exception:
            pass

    watchdog = AntigravityWatchdog(db=db, config=cfg.antigravity_watchdog, quota_sentinel=quota_sentinel)

    if action in ("doctor", "status", "health"):
        status_info = watchdog.get_status()
        if is_json:
            print(json.dumps(status_info, indent=2, ensure_ascii=False))
            return 0

        print("=" * 65)
        print("🤖 Google Antigravity 运行状况与守护诊断 (Doctor)")
        print("=" * 65)
        net_icon = "✅" if status_info["network_online"] else "❌"
        agentapi_icon = "✅" if status_info["agentapi_available"] else "❌"
        ls_icon = "✅" if status_info["language_server_connected"] else "⚠️"

        print(f"• 外部互联网连接 (Probe):         {net_icon} {'畅通稳定' if status_info['network_online'] else '离线不可达 (Fail-Closed)'}")
        print(f"• AgentAPI CLI 可用性:            {agentapi_icon} {'已就绪 (' + (watchdog.agentapi.executable_path or '') + ')' if status_info['agentapi_available'] else '未找到可执行文件'}")
        print(f"• Language Server gRPC 通道:      {ls_icon} {status_info['language_server_address'] or '未发现活跃通道'}")
        print(f"• 后台守护轮询状态:               {'✅ 开启' if status_info['watchdog_enabled'] else '⏸️ 禁用'} (周期: {status_info['interval_seconds']}s, 自动拉起: {status_info['auto_resuscitate']})")
        try:
            q_stats = db.get_quota_summary_stats()
            print(f"• 5小时滚动配额哨兵:             {'✅ 开启' if cfg.antigravity_quota.enabled else '⏸️ 禁用'} (追踪: {q_stats['total_accounts']} 账号, {q_stats['total_5h_buckets']} 配额池, 成功微Ping: {q_stats['successful_warmups']} 次)")
        except Exception:
            pass

        stalled = status_info["stalled_sessions"]
        print("-" * 65)
        print(f"🔍 挂起/中断会话检测 (共发现 {len(stalled)} 个):")
        if not stalled:
            print("  ✅ 未发现网络中断或卡死挂起的会话（系统运行健康）。")
        else:
            for idx, s in enumerate(stalled, 1):
                type_tag = "子代理 (Subagent)" if s["is_subagent"] else "主会话 (Root)"
                slug_tag = f" [Sidecar: {s['sidecar_slug']}]" if s["sidecar_slug"] else ""
                status_tag = "🟢 可自动拉起" if s["can_resuscitate"] else f"⛔ 跳过 ({s['skip_reason']})"
                print(f"  {idx}. [{type_tag}]{slug_tag} {s['conversation_id']}")
                print(f"     中断原因: {s['last_error']}")
                print(f"     状态判断: {status_tag} (历史尝试: {s['attempt_count']} 次)")

        cooldowns = status_info.get("quota_cooldown_sessions") or []
        if cooldowns:
            print("-" * 65)
            print(f"⏳ 配额冷却状态会话 (共 {len(cooldowns)} 个处于冷静期):")
            for idx, c in enumerate(cooldowns, 1):
                cd_until = float(c.get("cooldown_until") or 0.0)
                rem = max(0, int(cd_until - time.time()))
                print(f"  {idx}. 会话: {c.get('conversation_id')} (剩余等待: {rem}s)")
                print(f"     详情: {c.get('error_details') or c.get('last_error')}")

        schedules = status_info.get("active_conversation_schedules") or []
        if schedules:
            print("-" * 65)
            print(f"⏰ 会话后台定时巡检哨兵 (共监控 {len(schedules)} 个会话内定时器):")
            for idx, sc in enumerate(schedules, 1):
                last_trig = float(sc.get("last_trigger_at") or 0.0)
                last_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_trig)) if last_trig > 0 else "尚未触发"
                print(f"  {idx}. 会话: {sc.get('conversation_id')} [Cron: {sc.get('cron_expression')}]")
                print(f"     预期周期: {sc.get('expected_interval_seconds')}s | 最近触发: {last_str}")

        recent = status_info["recent_resuscitations"]
        print("-" * 65)
        print(f"📜 最近自愈恢复记录 (最近 {len(recent)} 条):")
        if not recent:
            print("  (暂无历史拉起自愈记录)")
        else:
            for r in recent:
                st = r.get("status", "unknown")
                if st in ("resuscitated", "schedule_remounted"):
                    st_icon = "✅"
                elif st in ("attempting", "quota_cooldown"):
                    st_icon = "⏳"
                else:
                    st_icon = "❌"
                print(f"  • {r.get('resuscitated_at')} [{st_icon} {st}] 会话: {r.get('conversation_id')} (第 {r.get('attempt_count')} 次)")
                if r.get("last_error"):
                    print(f"    错误原因: {r.get('last_error')}")
        print("=" * 65)
        return 0

    elif action in ("pull-up", "resuscitate", "wake"):
        dry_run = getattr(args, "dry_run", False)
        target_convo = getattr(args, "conversation", None)
        custom_prompt = getattr(args, "prompt", None)

        if not watchdog.check_network_health():
            msg = "错误: 外部网络未连通，拒绝执行会话拉起（Fail-Closed 防止无效唤醒造成状态错乱）。"
            if is_json:
                print(json.dumps({"success": False, "error": msg}, ensure_ascii=False))
            else:
                print(f"❌ {msg}", file=sys.stderr)
            return 1

        if not watchdog.is_agentapi_ready():
            msg = "错误: Antigravity AgentAPI / language_server 尚未就绪，无法派发自愈指令。"
            if is_json:
                print(json.dumps({"success": False, "error": msg}, ensure_ascii=False))
            else:
                print(f"❌ {msg}", file=sys.stderr)
            return 1

        if target_convo:
            transcript_path = resolve_transcript_path(target_convo, watchdog._brain_dir)
            if not transcript_path or not transcript_path.exists():
                msg = f"未在 {watchdog._brain_dir} 找到会话 {target_convo} 的 transcript.jsonl"
                if is_json:
                    print(json.dumps({"success": False, "error": msg}, ensure_ascii=False))
                else:
                    print(f"❌ {msg}", file=sys.stderr)
                return 1

            session_info = StalledSessionInfo(
                conversation_id=target_convo,
                transcript_path=transcript_path,
                last_step_index=0,
                last_error="manual_targeted_pull_up",
                last_error_time=time.time(),
                is_subagent=False,
                sidecar_slug=watchdog._find_associated_sidecar(target_convo),
                attempt_count=db.get_resuscitation_attempts(target_convo),
                can_resuscitate=True,
            )
            if dry_run:
                print(f"[DRY-RUN] 将拉起指定会话: {target_convo}")
                return 0

            res = asyncio.run(watchdog.resuscitate_session(session_info, custom_prompt=custom_prompt))
            if is_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                if res.get("success"):
                    print(f"✅ 成功拉起会话 {target_convo}！(自愈流水号: {res.get('resuscitation_id')})")
                else:
                    print(f"❌ 拉起会话失败: {res.get('error')}")
            return 0 if res.get("success") else 1

        stalled = watchdog.scan_stalled_conversations()
        if dry_run:
            if is_json:
                import dataclasses
                print(json.dumps([dataclasses.asdict(s) for s in stalled], default=str, indent=2, ensure_ascii=False))
            else:
                print(f"🔍 [DRY-RUN] 扫描到 {len(stalled)} 个挂起/中断会话:")
                for s in stalled:
                    type_str = "子代理" if s.is_subagent else "主会话"
                    print(f"  • [{type_str}] {s.conversation_id}: {s.last_error} (可拉起: {s.can_resuscitate})")
            return 0

        results = asyncio.run(watchdog.resuscitate_stalled_sessions())
        if is_json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print(f"🚀 已自动拉起 {len(results)} 个中断会话:")
            for r in results:
                st_icon = "✅" if r.get("success") else "❌"
                print(f"  {st_icon} 会话 {r.get('conversation_id')}: 状态={r.get('status')}")
        return 0

    elif action == "quota":
        from hub.antigravity.quota_sentinel import AntigravityQuotaSentinel
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        # Refresh and sync
        profiles = sentinel.scan_accounts()
        sentinel.sync_quotas_to_db(profiles)
        overview = sentinel.get_quota_overview(account_email=getattr(args, "account", None))

        if is_json:
            print(json.dumps(overview, indent=2, ensure_ascii=False))
            return 0

        stats = overview.get("stats", {})
        accounts = overview.get("accounts", [])
        print("=" * 70)
        print("⚡ Google Antigravity 5小时滚动配额哨兵与账号状态 (Quota Sentinel)")
        print("=" * 70)
        print(f"• 追踪账号总数: {stats.get('total_accounts', len(accounts))} 个 | 活跃账号: {stats.get('active_accounts', 1)} 个")
        print(f"• 5小时滚动配额池: {stats.get('total_5h_buckets', 0)} 个 (就绪可触发: {stats.get('ready_5h_buckets', 0)} 个)")
        print(f"• 历史成功预热保活: {stats.get('successful_warmups', 0)} 次")
        print("-" * 70)

        for acc in accounts:
            active_marker = "🌟 [当前IDE活跃账号]" if acc["is_active_account"] else "  [备用账号]"
            tier = acc.get("subscription_tier") or "Standard"
            print(f"{active_marker} {acc['email']} ({tier})")
            for b in acc.get("buckets", []):
                window = b["window_type"].upper()
                pct = b["remaining_percent"]
                bar_len = int(pct / 10)
                bar = "█" * bar_len + "░" * (10 - bar_len)
                countdown = b["human_countdown"]
                print(f"    ├─ [{window:<6}] {b['bucket_id']:<15} {bar} {pct:>5.1f}% | 重置倒计时: {countdown}")
            print()

        recent_warmups = overview.get("recent_warmups", [])
        if recent_warmups:
            print("-" * 70)
            print(f"📜 最近滚动保活微Ping触发记录 (最近 {len(recent_warmups)} 条):")
            for w in recent_warmups[:5]:
                st_icon = "✅" if w.get("status") == "success" else "❌"
                print(f"  • {w.get('created_at')} [{st_icon} {w.get('status')}] {w.get('account_email')} | {w.get('bucket_id')} ({w.get('model_name')}) - {w.get('duration_ms')}ms")
        print("=" * 70)
        return 0

    elif action == "warmup":
        from hub.antigravity.quota_sentinel import AntigravityQuotaSentinel
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        account_email = getattr(args, "account", None)
        bucket_id = getattr(args, "bucket", None)
        force = getattr(args, "force", False)
        dry_run = getattr(args, "dry_run", False)

        all_accounts = None
        if getattr(args, "active_only", False):
            all_accounts = False
        elif getattr(args, "all_accounts", False):
            all_accounts = True

        target_desc = account_email or ("当前活跃账号" if all_accounts is False else "全舰队就绪账号")
        if not is_json:
            prefix = "🔍 [DRY-RUN] " if dry_run else "🚀 "
            print(f"{prefix}正在为 Antigravity 执行滚动配额最小Token保活 (Force={force}, Target={target_desc}, Bucket={bucket_id or '全部就绪池'})...")

        res = asyncio.run(sentinel.sweep_and_warmup(
            reason="cli_manual_warmup",
            account_email=account_email,
            bucket_id=bucket_id,
            force=force,
            all_accounts=all_accounts,
            dry_run=dry_run,
        ))

        if is_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0

        if dry_run:
            cands = res.get("warmup_candidates", [])
            if not cands:
                print(f"ℹ️ [DRY-RUN] 未发现需要保活的候选配额池（当前配额可能正在消耗倒计时中，或处于冷却期内）。")
            else:
                print(f"🔍 [DRY-RUN] 发现 {len(cands)} 个满足自主保活条件的候选配额池（未实际发送网络请求）:")
                for c in cands:
                    act_tag = " [活跃]" if c.get("is_active") else " [备用]"
                    print(f"  • {c.get('account_email')}{act_tag} | {c.get('bucket_id')} ({c.get('window_type')}) -> 拟调用: {c.get('model_name')}")
            return 0

        warmups = res.get("warmup_results", [])
        if not warmups:
            print(f"ℹ️ 未发现需要保活的配额池（当前配额可能正在消耗倒计时中，或处于 4h55m 冷却期内）。")
            print(f"   提示: 可使用 `--force` 参数强制触发最小Token微Ping测试。")
        else:
            print(f"✅ 成功执行 {len(warmups)} 次滚动保活微Ping:")
            for w in warmups:
                st_icon = "✅" if w.get("status") == "success" else "❌"
                print(f"  {st_icon} {w.get('account_email')} | {w.get('bucket_id')} -> 模型: {w.get('model_name')} ({w.get('duration_ms')}ms)")
        return 0

    return 0


def _add_alert_diagnose_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--message", "-m", required=True, help="Alert message text or snippet to evaluate")
    parser.add_argument("--service", "-s", default="Server Alert", help="Service or monitor name")
    parser.add_argument("--url", "-u", default="", help="Target endpoint URL (if any)")
    parser.add_argument("--threshold", "-t", type=float, default=None, help="Criticality threshold override (default: 0.70)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON diagnostic result")


def cmd_alert_diagnose(args: Any) -> int:
    """Diagnose and explain an alert using Jev System One noise filter."""
    import json
    from hub.alert_filter import JevAlertFilter

    message = getattr(args, "message", None) or ""
    service = getattr(args, "service", None) or "Diagnose Alert"
    url = getattr(args, "url", None) or ""
    threshold = getattr(args, "threshold", None)
    is_json = getattr(args, "json", False)

    if not message:
        print("❌ Error: --message is required. Example: --message '[GlintMuse Blog] [🔴 Down] 200 - OK, but keyword is not in []'", file=sys.stderr)
        return 1

    filter_engine = JevAlertFilter()
    res = filter_engine.evaluate_alert(
        service=service,
        message=message,
        url=url,
        custom_threshold=threshold,
    )

    if is_json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return 0

    decision_color = "🔴" if res.is_critical else "🟢"
    action_text = "ESCALATE (Critical Alert)" if res.is_critical else "SUPPRESS (Non-Critical Noise)"

    print("\n" + "=" * 62)
    print("        JEV SYSTEM ONE — ALERT DE-NOISING DIAGNOSTIC")
    print("=" * 62)
    print(f"Service:     {service}")
    print(f"Message:     {message}")
    if url:
        print(f"URL:         {url}")
    print("-" * 62)
    print(f"Model:       {res.model} ({res.eval_source})")
    print(f"Latency:     {res.duration_ms:0.1f}ms")
    print(f"Calibrated:  noul={res.noul:0.2f} (Threshold: {res.threshold:0.2f})")
    print(f"Decision:    {decision_color} {action_text}")
    print(f"Reason:      {res.reason}")
    print("=" * 62 + "\n")
    return 0


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

    p_tasks = subparsers.add_parser("tasks", help="List, inspect, and review webhook tasks (filter failed, real vs test)")
    _add_tasks_args(p_tasks)

    p_schedule = subparsers.add_parser(
        "schedule",
        aliases=["schedules"],
        help="List, create, trigger, pause, resume, and delete scheduled tasks",
    )
    _add_schedule_args(p_schedule)

    p_rerun = subparsers.add_parser("rerun", help="Re-enqueue an existing task for re-execution")
    _add_rerun_args(p_rerun)

    p_setup = subparsers.add_parser("setup", aliases=["init"], help="Bootstrap environment, directories, and cryptographic .env for Mac users")
    _add_setup_args(p_setup)

    p_service = subparsers.add_parser("service", help="Manage native macOS launchd LaunchAgent background daemon")
    _add_service_args(p_service)

    p_antigravity = subparsers.add_parser(
        "antigravity",
        aliases=["ag", "watchdog"],
        help="Antigravity watchdog diagnostics, network check, and session pull-up",
    )
    _add_antigravity_args(p_antigravity)

    p_catchup = subparsers.add_parser(
        "catchup",
        aliases=["reconcile"],
        help="Reconcile and catch up unfulfilled offline tasks from Slack #input_agent",
    )
    _add_catchup_args(p_catchup)

    p_alert = subparsers.add_parser(
        "alert-diagnose",
        aliases=["alert", "diagnose-alert"],
        help="Diagnose and explain system alert using Jev System One noise filter",
    )
    _add_alert_diagnose_args(p_alert)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI execution entry point."""
    if argv is None:
        argv = sys.argv[1:]

    # If flags like --port or --db are provided without a subcommand, default to 'start'
    known_commands = {
        "start", "stop", "status", "logs", "test-send", "verify",
        "review-contact", "contact-review", "sweep", "pick-unprocessed", "recover",
        "dashboard", "ui", "tasks", "schedule", "schedules", "rerun", "setup", "init", "service",
        "antigravity", "ag", "watchdog", "catchup", "reconcile",
        "alert-diagnose", "alert", "diagnose-alert",
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

    elif subcommand == "tasks":
        p = argparse.ArgumentParser(prog="webhook-hub tasks")
        _add_tasks_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "tasks"
        del p
        gc.collect()
        return cmd_tasks(args)

    elif subcommand in ("schedule", "schedules"):
        p = argparse.ArgumentParser(prog=f"webhook-hub {subcommand}")
        _add_schedule_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = subcommand
        del p
        gc.collect()
        return cmd_schedule(args)

    elif subcommand == "rerun":
        p = argparse.ArgumentParser(prog="webhook-hub rerun")
        _add_rerun_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "rerun"
        del p
        gc.collect()
        return cmd_rerun(args)

    elif subcommand in ("setup", "init"):
        p = argparse.ArgumentParser(prog="webhook-hub setup")
        _add_setup_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "setup"
        del p
        gc.collect()
        return cmd_setup(args)

    elif subcommand == "service":
        p = argparse.ArgumentParser(prog="webhook-hub service")
        _add_service_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "service"
        del p
        gc.collect()
        return cmd_service(args)

    elif subcommand in ("antigravity", "ag", "watchdog"):
        p = argparse.ArgumentParser(prog="webhook-hub antigravity")
        _add_antigravity_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "antigravity"
        del p
        gc.collect()
        return cmd_antigravity(args)

    elif subcommand in ("catchup", "reconcile"):
        p = argparse.ArgumentParser(prog="webhook-hub catchup")
        _add_catchup_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "catchup"
        del p
        gc.collect()
        return cmd_catchup(args)

    elif subcommand in ("alert-diagnose", "alert", "diagnose-alert"):
        p = argparse.ArgumentParser(prog="webhook-hub alert-diagnose")
        _add_alert_diagnose_args(p)
        args = p.parse_args(sub_args)
        args.subcommand = "alert-diagnose"
        del p
        gc.collect()
        return cmd_alert_diagnose(args)

    else:
        parser = build_parser()
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
