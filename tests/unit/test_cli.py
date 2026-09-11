"""
Unit tests for Antigravity Webhook Hub Unified Command Line Interface (hub.cli).
Verifies all 6 subcommands, argument parsing, PID lifecycle, security signing, and offline SQLite inspection.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hub.cli import (
    _add_logs_args,
    _add_send_args,
    _add_start_args,
    _add_status_args,
    _add_stop_args,
    _add_verify_args,
    _is_pid_running,
    _read_pid_file,
    build_parser,
    cmd_logs,
    cmd_start,
    cmd_status,
    cmd_stop,
    cmd_test_send,
    cmd_verify,
    main,
)
from hub.config import AppConfig
from hub.db import DatabaseManager


# ==============================================================================
# 1. PARSER & SUBCOMMAND REGISTRATION TESTS
# ==============================================================================

def test_build_parser_subcommands():
    """Verify all 6 subcommands are registered with appropriate options."""
    parser = build_parser()
    subparsers_action = [
        action for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    assert len(subparsers_action) == 1
    choices = subparsers_action[0].choices
    expected_subcommands = {"start", "stop", "status", "logs", "test-send", "verify", "dashboard", "rerun"}
    assert expected_subcommands.issubset(set(choices.keys()))


def test_main_help(capsys):
    """Verify root help output displays usage and returns 0."""
    with patch.object(sys, "argv", ["webhook-hub", "--help"]):
        code = main(["--help"])
        assert code == 0
        captured = capsys.readouterr()
        assert "Antigravity Webhook Hub" in captured.out
        assert "start" in captured.out
        assert "status" in captured.out
        assert "stop" in captured.out
        assert "logs" in captured.out
        assert "test-send" in captured.out
        assert "verify" in captured.out


def test_main_leading_flag_defaults_to_start():
    """Verify flags like --port without explicit 'start' token are mapped to start."""
    with patch("hub.cli.cmd_start") as mock_start:
        mock_start.return_value = 0
        code = main(["--port", "9876", "--db", "/tmp/nonexistent.db"])
        assert code == 0
        assert mock_start.called
        args = mock_start.call_args[0][0]
        assert args.port == 9876
        assert args.db == "/tmp/nonexistent.db"


def test_main_unknown_command(capsys):
    """Verify unknown command prints help and returns 0."""
    code = main(["unknown-subcommand"])
    assert code == 0
    captured = capsys.readouterr()
    assert "usage:" in captured.out


def test_cmd_start_socket_bind_error_graceful(tmp_path, capsys):
    """Verify graceful handling when port is already bound / OSError on server.start()."""
    pid_path = tmp_path / "test_bind.pid"
    db_path = tmp_path / "test_bind.db"

    with patch("hub.server.AsyncHTTPServer.start", side_effect=OSError(48, "Address already in use")), \
         patch("hub.dispatcher.TaskDispatcher.start"):
        parser = build_parser()
        args = parser.parse_args([
            "start",
            "--host", "127.0.0.1",
            "--port", "9491",
            "--db", str(db_path),
            "--pidfile", str(pid_path),
        ])
        code = cmd_start(args)
        assert code == 1
        captured = capsys.readouterr()
        assert "Unable to bind to http://127.0.0.1:9491" in captured.err
        assert "Address already in use" in captured.err
        assert not pid_path.exists()


def test_cmd_start_daemon_launcher_entrypoint_and_log_cleanup(tmp_path):
    """Verify daemon launcher executes bin/webhook-hub directly and closes log file descriptor."""
    pid_path = tmp_path / "daemon_test.pid"
    db_path = tmp_path / "daemon_test.db"

    mock_proc = MagicMock()
    mock_proc.pid = 44556
    mock_proc.poll.return_value = None

    mock_health_resp = MagicMock()
    mock_health_resp.status = 200
    mock_health_resp.__enter__.return_value = mock_health_resp

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch("urllib.request.urlopen", return_value=mock_health_resp):
        parser = build_parser()
        args = parser.parse_args([
            "start",
            "-d",
            "--host", "127.0.0.1",
            "--port", "9488",
            "--db", str(db_path),
            "--pidfile", str(pid_path),
        ])
        code = cmd_start(args)
        assert code == 0
        assert mock_popen.called

        cmd_spawned = mock_popen.call_args[0][0]
        # Must not use `-m hub`
        assert "-m" not in cmd_spawned
        # Must execute webhook-hub directly or __main__.py
        assert any("webhook-hub" in str(arg) or "__main__.py" in str(arg) for arg in cmd_spawned)

        # Ensure stdout file passed to Popen is closed in parent
        stdout_arg = mock_popen.call_args[1].get("stdout")
        assert stdout_arg is not None
        assert stdout_arg.closed is True


# ==============================================================================
# 2. PID FILE & PROCESS LIFECYCLE TESTS
# ==============================================================================

def test_read_pid_file_lifecycle(tmp_path):
    pid_file = tmp_path / "test.pid"
    # 1. Non-existent file
    assert _read_pid_file(pid_file) is None

    # 2. Valid PID
    pid_file.write_text("12345\n", encoding="utf-8")
    assert _read_pid_file(pid_file) == 12345

    # 3. Corrupt/Invalid PID
    pid_file.write_text("invalid_pid_string\n", encoding="utf-8")
    assert _read_pid_file(pid_file) is None


def test_is_pid_running():
    # Current process PID is definitely running
    current_pid = os.getpid()
    assert _is_pid_running(current_pid) is True

    # PID 0 or negative or absurdly large PID
    assert _is_pid_running(0) is False
    assert _is_pid_running(-1) is False
    assert _is_pid_running(99999999) is False


# ==============================================================================
# 3. STATUS SUBCOMMAND TESTS
# ==============================================================================

def test_cmd_status_server_stopped(tmp_path, capsys):
    """Verify status reports stopped when PID file does not exist."""
    pid_path = tmp_path / "nonexistent.pid"
    parser = build_parser()
    args = parser.parse_args(["status", "--pidfile", str(pid_path), "--host", "127.0.0.1", "--port", "59999"])
    code = cmd_status(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "STOPPED" in captured.out or "not running" in captured.out


def test_cmd_status_stale_pid_file(tmp_path, capsys):
    """Verify status detects dead PID."""
    pid_path = tmp_path / "stale.pid"
    dead_pid = 9999999
    pid_path.write_text(f"{dead_pid}\n", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(["status", "--pidfile", str(pid_path), "--host", "127.0.0.1", "--port", "59999"])
    code = cmd_status(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "Stale PID file" in captured.out


def test_cmd_status_server_running_json(tmp_path, capsys):
    """Verify status --json outputs valid JSON metrics when server responds."""
    pid_path = tmp_path / "active.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    mock_health = {
        "status": "ok",
        "uptime_seconds": 12.34,
        "database": {"status": "connected"},
        "system": {"memory_rss_mb": 22.5, "memory_healthy": True},
    }

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = json.dumps(mock_health).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        parser = build_parser()
        args = parser.parse_args(["status", "--pidfile", str(pid_path), "--json"])
        code = cmd_status(args)
        assert code == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["running"] is True
        assert parsed["pid"] == os.getpid()
        assert parsed["status"] == "ok"


# ==============================================================================
# 4. STOP SUBCOMMAND TESTS
# ==============================================================================

def test_cmd_stop_no_pid(tmp_path, capsys):
    pid_path = tmp_path / "no_hub.pid"
    parser = build_parser()
    args = parser.parse_args(["stop", "--pidfile", str(pid_path)])
    code = cmd_stop(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "No active PID file found" in captured.out


def test_cmd_stop_stale_pid(tmp_path, capsys):
    pid_path = tmp_path / "stale_stop.pid"
    dead_pid = 9999999
    pid_path.write_text(f"{dead_pid}\n", encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(["stop", "--pidfile", str(pid_path)])
    code = cmd_stop(args)
    assert code == 0
    assert not pid_path.exists()
    captured = capsys.readouterr()
    assert "is not running" in captured.out


def test_cmd_stop_active_process(tmp_path):
    pid_path = tmp_path / "active_stop.pid"
    mock_pid = 12345
    pid_path.write_text(f"{mock_pid}\n", encoding="utf-8")

    # Mock _is_pid_running to return True first, then False after SIGTERM
    call_count = [0]
    def fake_running(p):
        call_count[0] += 1
        return call_count[0] == 1

    with patch("hub.cli._is_pid_running", side_effect=fake_running), \
         patch("os.kill") as mock_kill:
        parser = build_parser()
        args = parser.parse_args(["stop", "--pidfile", str(pid_path)])
        code = cmd_stop(args)
        assert code == 0
        assert mock_kill.called
        assert mock_kill.call_args[0] == (mock_pid, signal.SIGTERM)
        assert not pid_path.exists()


# ==============================================================================
# 5. TEST-SEND SUBCOMMAND TESTS
# ==============================================================================

def test_cmd_test_send_hmac_signing(capsys):
    """Verify test-send builds HMAC SHA-256 signature and posts to /webhook."""
    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.read.return_value = json.dumps({"status": "accepted", "task_id": "tsk_test_123"}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        parser = build_parser()
        args = parser.parse_args([
            "test-send",
            "--secret", "test_secret_key_32_bytes_long!!",
            "--action", "cli",
            "--command", "echo 'hello'",
            "--host", "127.0.0.1",
            "--port", "9423",
        ])
        code = cmd_test_send(args)
        assert code == 0
        assert mock_urlopen.called
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"
        assert req.get_header("Content-type") == "application/json"
        assert "x-hub-signature-256" in [k.lower() for k in req.headers]
        assert "x-hub-timestamp" in [k.lower() for k in req.headers]

        captured = capsys.readouterr()
        assert "202" in captured.out
        assert "tsk_test_123" in captured.out


def test_cmd_test_send_bearer_token():
    """Verify test-send sends Bearer authorization when --token is passed."""
    mock_resp = MagicMock()
    mock_resp.status = 202
    mock_resp.read.return_value = b'{"status": "accepted"}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        parser = build_parser()
        args = parser.parse_args([
            "test-send",
            "--token", "secret_bearer_token_abc",
            "--action", "cli",
            "--command", "ls",
        ])
        code = cmd_test_send(args)
        assert code == 0
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer secret_bearer_token_abc"


def test_cmd_test_send_tamper_signature(capsys):
    """Verify --tamper corrupts signature to test 401 Unauthorized rejection."""
    import urllib.error
    mock_err = urllib.error.HTTPError(
        url="http://127.0.0.1:9423/webhook",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"error": "Invalid signature"}'),
    )

    with patch("urllib.request.urlopen", side_effect=mock_err):
        parser = build_parser()
        args = parser.parse_args([
            "test-send",
            "--secret", "valid_secret_12345678",
            "--tamper",
        ])
        code = cmd_test_send(args)
        assert code == 1
        captured = capsys.readouterr()
        assert "401" in (captured.out + captured.err)


# ==============================================================================
# 6. LOGS SUBCOMMAND TESTS (OFFLINE SQLITE SSOT)
# ==============================================================================

def test_cmd_logs_offline_sqlite(tmp_path, capsys):
    """Verify logs subcommand queries offline SQLite tables directly."""
    db_file = tmp_path / "test_logs.db"
    db = DatabaseManager(str(db_file))
    db.init_schema()

    # Insert sample webhook event and task
    event_id = "evt_sample_01"
    db.insert_webhook_event({
        "event_id": event_id,
        "source": "github",
        "payload_hash": "hash123",
        "headers_json": "{}",
        "raw_payload": '{"action": "ping"}',
        "method": "POST",
        "path": "/webhook/github",
        "remote_addr": "127.0.0.1",
        "status": "processed",
    })

    task_id = "tsk_sample_01"
    db.insert_task({
        "task_id": task_id,
        "event_id": event_id,
        "source": "github",
        "action_type": "cli",
        "command": "echo 'logging test'",
        "status": "succeeded",
        "priority": 1,
    })

    db.insert_execution_log(task_id, stream="stdout", chunk="Test output line 1\n")
    db.insert_execution_log(task_id, stream="stderr", chunk="Warning output line 2\n")

    parser = build_parser()
    args = parser.parse_args(["logs", "--db", str(db_file), "--lines", "10"])
    code = cmd_logs(args)
    assert code == 0

    captured = capsys.readouterr()
    assert "tsk_sample_01" in captured.out
    assert "Test output line 1" in captured.out
    assert "Warning output line 2" in captured.out


def test_cmd_logs_missing_db(tmp_path, capsys):
    missing_db = tmp_path / "does_not_exist.db"
    parser = build_parser()
    args = parser.parse_args(["logs", "--db", str(missing_db)])
    code = cmd_logs(args)
    assert code == 1
    captured = capsys.readouterr()
    assert "Database file not found" in (captured.out + captured.err)


# ==============================================================================
# 7. VERIFY SUBCOMMAND TESTS
# ==============================================================================

def test_cmd_verify_delegation():
    """Verify verify command delegates to scripts/verify_e2e.py and returns exit code."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        parser = build_parser()
        args = parser.parse_args(["verify", "--host", "127.0.0.1", "--port", "9423"])
        code = cmd_verify(args)
        assert code == 0
        assert mock_run.called
        cmd_called = mock_run.call_args[0][0]
        assert any("verify_e2e.py" in str(arg) for arg in cmd_called)


# ==============================================================================
# 8. SWEEP SUBCOMMAND TESTS
# ==============================================================================

def test_build_parser_sweep_subcommands():
    """Verify sweep subcommand and aliases are registered."""
    parser = build_parser()
    subparsers_action = [
        action for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    ]
    choices = subparsers_action[0].choices
    assert "sweep" in choices
    assert "pick-unprocessed" in choices
    assert "recover" in choices


def test_cmd_sweep_offline_db(tmp_path, capsys):
    """Test sweep command directly against offline SQLite database."""
    from hub.cli import cmd_sweep

    db_file = tmp_path / "test_cli_sweep.db"
    db = DatabaseManager(str(db_file), cache_size=-16)
    db.init_schema()

    # Seed orphan event
    db.insert_webhook_event({
        "event_id": "evt_cli_orphan",
        "source": "cli_test",
        "raw_payload": '{"command": "echo cli_sweep"}',
        "status": "received",
    })

    parser = build_parser()

    # 1. Dry run
    args_dry = parser.parse_args(["sweep", "--db", str(db_file), "--dry-run"])
    code_dry = cmd_sweep(args_dry)
    assert code_dry == 0
    cap_dry = capsys.readouterr()
    assert "DRY RUN" in cap_dry.out
    assert "Orphaned Events" in cap_dry.out

    # 2. JSON dry run
    args_json = parser.parse_args(["sweep", "--db", str(db_file), "--dry-run", "--json"])
    code_json = cmd_sweep(args_json)
    assert code_json == 0
    cap_json = capsys.readouterr()
    data_json = json.loads(cap_json.out)
    assert data_json["dry_run"] is True

    # 3. Live recovery sweep
    args_live = parser.parse_args(["sweep", "--db", str(db_file)])
    code_live = cmd_sweep(args_live)
    assert code_live == 0
    cap_live = capsys.readouterr()
    assert "Recovered" in cap_live.out

    # Verify event is no longer orphaned
    assert len(db.get_orphaned_webhook_events()) == 0
    db.close()


def test_cmd_sweep_no_auto_retry_and_source_flags(tmp_path, capsys):
    """Verify --no-auto-retry and --source flags in CLI parser and command execution."""
    from hub.cli import cmd_sweep

    db_file = tmp_path / "test_cli_flags.db"
    db = DatabaseManager(str(db_file), cache_size=-16)
    db.init_schema()

    parser = build_parser()
    args = parser.parse_args(["sweep", "--db", str(db_file), "--no-auto-retry", "--source", "custom_source"])
    assert args.auto_retry is False
    assert args.source == "custom_source"

    code = cmd_sweep(args)
    assert code == 0
    captured = capsys.readouterr()
    assert "DISABLED" in captured.out
    db.close()


def test_cmd_dashboard_output(capsys):
    """Verify `webhook-hub dashboard` command outputs local and tunnel URLs."""
    code = main(["dashboard"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Antigravity Webhook Hub" in captured.out
    assert "http://127.0.0.1:9423/dashboard" in captured.out
    assert "https://webhook.worldinspirelab.com/dashboard" in captured.out

    # JSON mode
    code_json = main(["dashboard", "--json"])
    assert code_json == 0
    cap_json = capsys.readouterr()
    data = json.loads(cap_json.out)
    assert "local_url" in data
    assert "tunnel_url" in data


def test_cmd_rerun_direct_sqlite(tmp_path, capsys):
    """Verify `webhook-hub rerun` directly modifies SQLite SSOT when gateway is offline."""
    db_file = tmp_path / "test_cli_rerun.db"
    db = DatabaseManager(str(db_file), cache_size=-16)
    db.init_schema()

    db.insert_webhook_event({
        "event_id": "evt_cli_rerun_1",
        "source": "test",
        "payload_hash": "d" * 64,
        "headers_json": "{}",
        "raw_payload": "{}",
        "method": "POST",
        "path": "/webhook/test",
        "status": "received",
    })
    db.insert_task({
        "task_id": "tsk_cli_rerun_1",
        "event_id": "evt_cli_rerun_1",
        "command": "echo cli_rerun",
        "source": "test",
        "status": "failed",
        "exit_code": 1,
        "error_message": "failed previously",
    })

    # Execute rerun CLI command targeting offline SQLite db
    code = main(["rerun", "tsk_cli_rerun_1", "--db", str(db_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "tsk_cli_rerun_1" in captured.out
    assert "queued" in captured.out

    # Verify task state transitioned to queued
    updated = db.get_task("tsk_cli_rerun_1")
    assert updated["status"] == "queued"
    assert updated["error_message"] is None
    assert updated["retry_count"] == 1
    db.close()



