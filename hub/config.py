"""
Antigravity Webhook Hub — Clean Configuration Loader
Zero hardcoded credentials or ports. Implements priority hierarchy:
CLI / Explicit overrides > Environment variables (.env & os.environ) > YAML config > Built-in defaults.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional



@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 9423
    max_body_bytes: int = 1048576  # 1MB
    log_level: str = "INFO"
    keep_alive_timeout: int = 65
    memory_budget_mb: float = 64.0


@dataclass(slots=True)
class SecurityConfig:
    auth_mode: str = "hmac"  # hmac, bearer, any, both
    webhook_secret: str = ""
    bearer_token: str = ""
    timestamp_tolerance_seconds: int = 300
    future_timestamp_tolerance_seconds: int = 60
    signature_header: str = "X-Hub-Signature-256"
    timestamp_header: str = "X-Hub-Timestamp"
    event_id_header: str = "X-Hub-Event-ID"


@dataclass(slots=True)
class DatabaseConfig:
    path: str = str(Path(__file__).resolve().parent.parent / "data" / "webhook_hub.db")
    wal_mode: bool = True
    busy_timeout_ms: int = 5000


@dataclass(slots=True)
class DispatchConfig:
    default_action_timeout_seconds: int = 300
    max_concurrent_tasks: int = 5
    allowed_command_prefixes: list[str] = field(
        default_factory=lambda: ["python3", "bash", "bin/"]
    )


@dataclass(slots=True)
class TunnelConfig:
    enabled: bool = False
    provider: str = "cloudflare"
    tunnel_name: str = "antigravity-webhook-tunnel"
    credentials_file: str = "~/.cloudflared/credentials.json"
    hostname: str = "webhook.yourdomain.com"


@dataclass(slots=True)
class SweeperConfig:
    enabled: bool = True
    interval_seconds: int = 30
    sleep_drift_threshold_seconds: int = 15
    stale_running_seconds: int = 300
    auto_retry_interrupted: bool = True
    max_auto_retries: int = 3
    rehydrate_orphaned_events: bool = True


@dataclass(slots=True)
class ObservabilityConfig:
    enabled: bool = True
    sidecar_slug: str = "webhook-hub-sentinel"
    emit_sidecar_events: bool = True
    sidecar_data_dir: Optional[str] = None


@dataclass(slots=True)
class DashboardConfig:
    auth_enabled: bool = False
    basic_auth_user: str = ""
    basic_auth_pass: str = ""
    auth_token: str = ""
    cloudflare_access_aud: str = ""
    cloudflare_access_team: str = "veecccc"
    allowed_emails: list[str] = field(default_factory=lambda: ["user@example.com"])


@dataclass(slots=True)
class AntigravityWatchdogConfig:
    enabled: bool = True
    interval_seconds: int = 30
    lookback_minutes: int = 720
    stall_grace_seconds: int = 180
    boost_quiet_seconds: int = 900
    max_retries_per_session: int = 3
    auto_resuscitate: bool = True
    probe_host: str = "1.1.1.1"
    probe_port: int = 53
    probe_timeout_seconds: float = 1.0
    brain_dir: Optional[str] = None
    sidecar_data_dir: Optional[str] = None
    conversations_dir: Optional[str] = None


@dataclass(slots=True)
class AntigravityQuotaConfig:
    enabled: bool = True
    scan_interval_seconds: int = 120
    auto_warmup_5h: bool = True
    auto_warmup_weekly: bool = False
    warmup_cooldown_seconds: int = 17700  # 4h 55m cooldown to avoid duplicate warmup within 5h cycle
    tools_api_host: str = "127.0.0.1"
    tools_api_port: int = 8045
    tools_api_timeout_seconds: float = 15.0
    default_gemini_model: str = "gemini-3-flash"
    default_3p_model: str = "claude-sonnet-4-6"
    accounts_dir: Optional[str] = None


@dataclass(slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    sweeper: SweeperConfig = field(default_factory=SweeperConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    antigravity_watchdog: AntigravityWatchdogConfig = field(default_factory=AntigravityWatchdogConfig)
    antigravity_quota: AntigravityQuotaConfig = field(default_factory=AntigravityQuotaConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "max_body_bytes": self.server.max_body_bytes,
                "log_level": self.server.log_level,
                "keep_alive_timeout": self.server.keep_alive_timeout,
            },
            "security": {
                "auth_mode": self.security.auth_mode,
                "webhook_secret": self.security.webhook_secret,
                "bearer_token": self.security.bearer_token,
                "timestamp_tolerance_seconds": self.security.timestamp_tolerance_seconds,
                "future_timestamp_tolerance_seconds": self.security.future_timestamp_tolerance_seconds,
                "signature_header": self.security.signature_header,
                "timestamp_header": self.security.timestamp_header,
                "event_id_header": self.security.event_id_header,
            },
            "database": {
                "path": self.database.path,
                "wal_mode": self.database.wal_mode,
                "busy_timeout_ms": self.database.busy_timeout_ms,
            },
            "dispatch": {
                "default_action_timeout_seconds": self.dispatch.default_action_timeout_seconds,
                "max_concurrent_tasks": self.dispatch.max_concurrent_tasks,
                "allowed_command_prefixes": self.dispatch.allowed_command_prefixes,
            },
            "tunnel": {
                "enabled": self.tunnel.enabled,
                "provider": self.tunnel.provider,
                "tunnel_name": self.tunnel.tunnel_name,
                "credentials_file": self.tunnel.credentials_file,
                "hostname": self.tunnel.hostname,
            },
            "sweeper": {
                "enabled": self.sweeper.enabled,
                "interval_seconds": self.sweeper.interval_seconds,
                "sleep_drift_threshold_seconds": self.sweeper.sleep_drift_threshold_seconds,
                "stale_running_seconds": self.sweeper.stale_running_seconds,
                "auto_retry_interrupted": self.sweeper.auto_retry_interrupted,
                "max_auto_retries": self.sweeper.max_auto_retries,
                "rehydrate_orphaned_events": self.sweeper.rehydrate_orphaned_events,
            },
            "observability": {
                "enabled": self.observability.enabled,
                "sidecar_slug": self.observability.sidecar_slug,
                "emit_sidecar_events": self.observability.emit_sidecar_events,
                "sidecar_data_dir": self.observability.sidecar_data_dir,
            },
            "dashboard": {
                "auth_enabled": self.dashboard.auth_enabled,
                "basic_auth_user": self.dashboard.basic_auth_user,
                "has_basic_auth_pass": bool(self.dashboard.basic_auth_pass),
                "has_auth_token": bool(self.dashboard.auth_token),
                "cloudflare_access_aud": self.dashboard.cloudflare_access_aud,
                "cloudflare_access_team": self.dashboard.cloudflare_access_team,
                "allowed_emails": self.dashboard.allowed_emails,
            },
            "antigravity_watchdog": {
                "enabled": self.antigravity_watchdog.enabled,
                "interval_seconds": self.antigravity_watchdog.interval_seconds,
                "lookback_minutes": self.antigravity_watchdog.lookback_minutes,
                "stall_grace_seconds": self.antigravity_watchdog.stall_grace_seconds,
                "boost_quiet_seconds": self.antigravity_watchdog.boost_quiet_seconds,
                "max_retries_per_session": self.antigravity_watchdog.max_retries_per_session,
                "auto_resuscitate": self.antigravity_watchdog.auto_resuscitate,
                "probe_host": self.antigravity_watchdog.probe_host,
                "probe_port": self.antigravity_watchdog.probe_port,
                "probe_timeout_seconds": self.antigravity_watchdog.probe_timeout_seconds,
                "brain_dir": self.antigravity_watchdog.brain_dir,
                "sidecar_data_dir": self.antigravity_watchdog.sidecar_data_dir,
            },
            "antigravity_quota": {
                "enabled": self.antigravity_quota.enabled,
                "scan_interval_seconds": self.antigravity_quota.scan_interval_seconds,
                "auto_warmup_5h": self.antigravity_quota.auto_warmup_5h,
                "auto_warmup_weekly": self.antigravity_quota.auto_warmup_weekly,
                "warmup_cooldown_seconds": self.antigravity_quota.warmup_cooldown_seconds,
                "tools_api_host": self.antigravity_quota.tools_api_host,
                "tools_api_port": self.antigravity_quota.tools_api_port,
                "tools_api_timeout_seconds": self.antigravity_quota.tools_api_timeout_seconds,
                "default_gemini_model": self.antigravity_quota.default_gemini_model,
                "default_3p_model": self.antigravity_quota.default_3p_model,
                "accounts_dir": self.antigravity_quota.accounts_dir,
            },
        }


def _expand_env_vars(val: str, env_dict: dict[str, str]) -> str:
    """Expand ${VAR} or ${VAR:-default} from env_dict and os.environ."""
    pattern = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) is not None else ""
        if var_name in env_dict:
            return env_dict[var_name]
        return os.environ.get(var_name, default_val)

    return pattern.sub(replacer, val)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse .env file into key-value pairs without external dependencies."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result

    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return result

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        result[key] = val
    return result


def _to_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("true", "1", "yes", "on")


def _to_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _load_yaml_file(path: Path, env_dict: dict[str, str]) -> dict[str, Any]:
    """Load YAML config file with ${ENV_VAR} interpolation."""
    if not path.is_file():
        return {}

    try:
        import yaml  # type: ignore
    except ImportError:
        sys.stderr.write(f"Warning: PyYAML is not installed; cannot load {path}\n")
        return {}

    try:
        raw_text = path.read_text(encoding="utf-8")
        interpolated = _expand_env_vars(raw_text, env_dict)
        loaded = yaml.safe_load(interpolated)
        return loaded if isinstance(loaded, dict) else {}
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to load YAML config {path}: {e}\n")
        return {}


def load_config(
    config_path: Optional[str | Path] = None,
    env_path: Optional[str | Path] = None,
    cli_overrides: Optional[dict[str, Any]] = None,
) -> AppConfig:
    """
    Load configuration with strict priority hierarchy:
    CLI Overrides > Environment (.env / os.environ) > YAML file > Built-in Defaults
    """
    # 1. Start with built-in defaults
    cfg = AppConfig()

    # 2. Determine and parse .env file
    if env_path is not None:
        env_file = Path(env_path) if env_path else Path("")
    elif config_path:
        env_file = Path("")
    else:
        env_file = Path(".env")
    env_dict = _parse_env_file(env_file)

    # Combine .env with active os.environ (os.environ takes precedence over .env file)
    combined_env: dict[str, str] = {**env_dict, **os.environ}

    # 3. Determine and load YAML configuration file
    yaml_file = Path(config_path) if config_path else Path("config.yaml")
    yaml_data = _load_yaml_file(yaml_file, combined_env) if yaml_file.is_file() else {}

    # Apply YAML data over defaults
    if "server" in yaml_data and isinstance(yaml_data["server"], dict):
        s = yaml_data["server"]
        if "host" in s:
            cfg.server.host = str(s["host"])
        if "port" in s:
            cfg.server.port = _to_int(s["port"], cfg.server.port)
        if "max_body_bytes" in s:
            cfg.server.max_body_bytes = _to_int(s["max_body_bytes"], cfg.server.max_body_bytes)
        if "log_level" in s:
            cfg.server.log_level = str(s["log_level"]).upper()
        if "keep_alive_timeout" in s:
            cfg.server.keep_alive_timeout = _to_int(s["keep_alive_timeout"], cfg.server.keep_alive_timeout)
        if "memory_budget_mb" in s:
            try:
                cfg.server.memory_budget_mb = float(s["memory_budget_mb"])
            except (ValueError, TypeError):
                pass

    if "security" in yaml_data and isinstance(yaml_data["security"], dict):
        sec = yaml_data["security"]
        if "auth_mode" in sec:
            cfg.security.auth_mode = str(sec["auth_mode"]).lower()
        if "webhook_secret" in sec:
            cfg.security.webhook_secret = str(sec["webhook_secret"])
        if "bearer_token" in sec:
            cfg.security.bearer_token = str(sec["bearer_token"])
        if "timestamp_tolerance_seconds" in sec:
            cfg.security.timestamp_tolerance_seconds = _to_int(
                sec["timestamp_tolerance_seconds"], cfg.security.timestamp_tolerance_seconds
            )
        if "future_timestamp_tolerance_seconds" in sec:
            cfg.security.future_timestamp_tolerance_seconds = _to_int(
                sec["future_timestamp_tolerance_seconds"], cfg.security.future_timestamp_tolerance_seconds
            )
        if "signature_header" in sec:
            cfg.security.signature_header = str(sec["signature_header"])
        if "timestamp_header" in sec:
            cfg.security.timestamp_header = str(sec["timestamp_header"])
        if "event_id_header" in sec:
            cfg.security.event_id_header = str(sec["event_id_header"])

    if "database" in yaml_data and isinstance(yaml_data["database"], dict):
        d = yaml_data["database"]
        if "path" in d:
            cfg.database.path = str(d["path"])
        if "wal_mode" in d:
            cfg.database.wal_mode = _to_bool(d["wal_mode"])
        if "busy_timeout_ms" in d:
            cfg.database.busy_timeout_ms = _to_int(d["busy_timeout_ms"], cfg.database.busy_timeout_ms)

    if "dispatch" in yaml_data and isinstance(yaml_data["dispatch"], dict):
        disp = yaml_data["dispatch"]
        if "default_action_timeout_seconds" in disp:
            cfg.dispatch.default_action_timeout_seconds = _to_int(
                disp["default_action_timeout_seconds"], cfg.dispatch.default_action_timeout_seconds
            )
        if "max_concurrent_tasks" in disp:
            cfg.dispatch.max_concurrent_tasks = _to_int(
                disp["max_concurrent_tasks"], cfg.dispatch.max_concurrent_tasks
            )
        if "allowed_command_prefixes" in disp and isinstance(disp["allowed_command_prefixes"], list):
            cfg.dispatch.allowed_command_prefixes = [str(x) for x in disp["allowed_command_prefixes"]]

    if "tunnel" in yaml_data and isinstance(yaml_data["tunnel"], dict):
        tun = yaml_data["tunnel"]
        if "enabled" in tun:
            cfg.tunnel.enabled = _to_bool(tun["enabled"])
        if "provider" in tun:
            cfg.tunnel.provider = str(tun["provider"])
        if "tunnel_name" in tun:
            cfg.tunnel.tunnel_name = str(tun["tunnel_name"])
        if "credentials_file" in tun:
            cfg.tunnel.credentials_file = str(tun["credentials_file"])
        if "hostname" in tun:
            cfg.tunnel.hostname = str(tun["hostname"])

    if "sweeper" in yaml_data and isinstance(yaml_data["sweeper"], dict):
        sw = yaml_data["sweeper"]
        if "enabled" in sw:
            cfg.sweeper.enabled = _to_bool(sw["enabled"])
        if "interval_seconds" in sw:
            cfg.sweeper.interval_seconds = _to_int(sw["interval_seconds"], cfg.sweeper.interval_seconds)
        if "sleep_drift_threshold_seconds" in sw:
            cfg.sweeper.sleep_drift_threshold_seconds = _to_int(
                sw["sleep_drift_threshold_seconds"], cfg.sweeper.sleep_drift_threshold_seconds
            )
        if "stale_running_seconds" in sw:
            cfg.sweeper.stale_running_seconds = _to_int(
                sw["stale_running_seconds"], cfg.sweeper.stale_running_seconds
            )
        if "auto_retry_interrupted" in sw:
            cfg.sweeper.auto_retry_interrupted = _to_bool(sw["auto_retry_interrupted"])
        if "max_auto_retries" in sw:
            cfg.sweeper.max_auto_retries = _to_int(sw["max_auto_retries"], cfg.sweeper.max_auto_retries)
        if "rehydrate_orphaned_events" in sw:
            cfg.sweeper.rehydrate_orphaned_events = _to_bool(sw["rehydrate_orphaned_events"])

    if "antigravity_watchdog" in yaml_data and isinstance(yaml_data["antigravity_watchdog"], dict):
        aw = yaml_data["antigravity_watchdog"]
        if "enabled" in aw:
            cfg.antigravity_watchdog.enabled = _to_bool(aw["enabled"])
        if "interval_seconds" in aw:
            cfg.antigravity_watchdog.interval_seconds = _to_int(
                aw["interval_seconds"], cfg.antigravity_watchdog.interval_seconds
            )
        if "lookback_minutes" in aw:
            cfg.antigravity_watchdog.lookback_minutes = _to_int(
                aw["lookback_minutes"], cfg.antigravity_watchdog.lookback_minutes
            )
        if "stall_grace_seconds" in aw:
            cfg.antigravity_watchdog.stall_grace_seconds = _to_int(
                aw["stall_grace_seconds"], cfg.antigravity_watchdog.stall_grace_seconds
            )
        if "boost_quiet_seconds" in aw:
            cfg.antigravity_watchdog.boost_quiet_seconds = _to_int(
                aw["boost_quiet_seconds"], cfg.antigravity_watchdog.boost_quiet_seconds
            )
        if "max_retries_per_session" in aw:
            cfg.antigravity_watchdog.max_retries_per_session = _to_int(
                aw["max_retries_per_session"], cfg.antigravity_watchdog.max_retries_per_session
            )
        if "auto_resuscitate" in aw:
            cfg.antigravity_watchdog.auto_resuscitate = _to_bool(aw["auto_resuscitate"])
        if "probe_host" in aw:
            cfg.antigravity_watchdog.probe_host = str(aw["probe_host"])
        if "probe_port" in aw:
            cfg.antigravity_watchdog.probe_port = _to_int(aw["probe_port"], cfg.antigravity_watchdog.probe_port)
        if "probe_timeout_seconds" in aw:
            try:
                cfg.antigravity_watchdog.probe_timeout_seconds = float(aw["probe_timeout_seconds"])
            except (ValueError, TypeError):
                pass
        if "brain_dir" in aw:
            cfg.antigravity_watchdog.brain_dir = str(aw["brain_dir"])
        if "sidecar_data_dir" in aw:
            cfg.antigravity_watchdog.sidecar_data_dir = str(aw["sidecar_data_dir"])

    # Antigravity Quota Sentinel & 5h Warmup
    if "antigravity_quota" in yaml_data and isinstance(yaml_data["antigravity_quota"], dict):
        aq = yaml_data["antigravity_quota"]
        if "enabled" in aq:
            cfg.antigravity_quota.enabled = _to_bool(aq["enabled"])
        if "scan_interval_seconds" in aq:
            cfg.antigravity_quota.scan_interval_seconds = _to_int(
                aq["scan_interval_seconds"], cfg.antigravity_quota.scan_interval_seconds
            )
        if "auto_warmup_5h" in aq:
            cfg.antigravity_quota.auto_warmup_5h = _to_bool(aq["auto_warmup_5h"])
        if "auto_warmup_weekly" in aq:
            cfg.antigravity_quota.auto_warmup_weekly = _to_bool(aq["auto_warmup_weekly"])
        if "warmup_cooldown_seconds" in aq:
            cfg.antigravity_quota.warmup_cooldown_seconds = _to_int(
                aq["warmup_cooldown_seconds"], cfg.antigravity_quota.warmup_cooldown_seconds
            )
        if "tools_api_host" in aq:
            cfg.antigravity_quota.tools_api_host = str(aq["tools_api_host"])
        if "tools_api_port" in aq:
            cfg.antigravity_quota.tools_api_port = _to_int(
                aq["tools_api_port"], cfg.antigravity_quota.tools_api_port
            )
        if "tools_api_timeout_seconds" in aq:
            try:
                cfg.antigravity_quota.tools_api_timeout_seconds = float(aq["tools_api_timeout_seconds"])
            except (ValueError, TypeError):
                pass
        if "default_gemini_model" in aq:
            cfg.antigravity_quota.default_gemini_model = str(aq["default_gemini_model"])
        if "default_3p_model" in aq:
            cfg.antigravity_quota.default_3p_model = str(aq["default_3p_model"])
        if "accounts_dir" in aq:
            cfg.antigravity_quota.accounts_dir = str(aq["accounts_dir"])

    # 4. Apply environment variables over YAML
    # Server
    if "HOST" in combined_env:
        cfg.server.host = combined_env["HOST"]
    if "PORT" in combined_env:
        cfg.server.port = _to_int(combined_env["PORT"], cfg.server.port)
    if "MAX_BODY_BYTES" in combined_env:
        cfg.server.max_body_bytes = _to_int(combined_env["MAX_BODY_BYTES"], cfg.server.max_body_bytes)
    if "LOG_LEVEL" in combined_env:
        cfg.server.log_level = combined_env["LOG_LEVEL"].upper()
    if "KEEP_ALIVE_TIMEOUT" in combined_env:
        cfg.server.keep_alive_timeout = _to_int(combined_env["KEEP_ALIVE_TIMEOUT"], cfg.server.keep_alive_timeout)
    if "MEMORY_BUDGET_MB" in combined_env:
        try:
            cfg.server.memory_budget_mb = float(combined_env["MEMORY_BUDGET_MB"])
        except (ValueError, TypeError):
            pass

    # Security
    if "AUTH_MODE" in combined_env:
        cfg.security.auth_mode = combined_env["AUTH_MODE"].lower()
    if "WEBHOOK_SECRET" in combined_env:
        cfg.security.webhook_secret = combined_env["WEBHOOK_SECRET"]
    if "BEARER_TOKEN" in combined_env:
        cfg.security.bearer_token = combined_env["BEARER_TOKEN"]
    if "TIMESTAMP_TOLERANCE_SECONDS" in combined_env:
        cfg.security.timestamp_tolerance_seconds = _to_int(
            combined_env["TIMESTAMP_TOLERANCE_SECONDS"], cfg.security.timestamp_tolerance_seconds
        )
    if "FUTURE_TIMESTAMP_TOLERANCE_SECONDS" in combined_env:
        cfg.security.future_timestamp_tolerance_seconds = _to_int(
            combined_env["FUTURE_TIMESTAMP_TOLERANCE_SECONDS"], cfg.security.future_timestamp_tolerance_seconds
        )
    if "SIGNATURE_HEADER" in combined_env:
        cfg.security.signature_header = combined_env["SIGNATURE_HEADER"]
    if "TIMESTAMP_HEADER" in combined_env:
        cfg.security.timestamp_header = combined_env["TIMESTAMP_HEADER"]
    if "EVENT_ID_HEADER" in combined_env:
        cfg.security.event_id_header = combined_env["EVENT_ID_HEADER"]

    # Database
    if "DATABASE_PATH" in combined_env:
        cfg.database.path = combined_env["DATABASE_PATH"]
    if cfg.database.path and cfg.database.path not in (":memory:", "") and not os.path.isabs(cfg.database.path):
        repo_root = Path(__file__).resolve().parent.parent
        cfg.database.path = str(repo_root / cfg.database.path)
    if "DATABASE_WAL_MODE" in combined_env:
        cfg.database.wal_mode = _to_bool(combined_env["DATABASE_WAL_MODE"])
    if "DATABASE_BUSY_TIMEOUT_MS" in combined_env:
        cfg.database.busy_timeout_ms = _to_int(
            combined_env["DATABASE_BUSY_TIMEOUT_MS"], cfg.database.busy_timeout_ms
        )

    # Dispatch
    if "DEFAULT_ACTION_TIMEOUT_SECONDS" in combined_env:
        cfg.dispatch.default_action_timeout_seconds = _to_int(
            combined_env["DEFAULT_ACTION_TIMEOUT_SECONDS"], cfg.dispatch.default_action_timeout_seconds
        )
    if "MAX_CONCURRENT_TASKS" in combined_env:
        cfg.dispatch.max_concurrent_tasks = _to_int(
            combined_env["MAX_CONCURRENT_TASKS"], cfg.dispatch.max_concurrent_tasks
        )

    # Tunnel
    if "CLOUDFLARE_TUNNEL_NAME" in combined_env:
        cfg.tunnel.tunnel_name = combined_env["CLOUDFLARE_TUNNEL_NAME"]
    if "CLOUDFLARE_TUNNEL_HOSTNAME" in combined_env:
        cfg.tunnel.hostname = combined_env["CLOUDFLARE_TUNNEL_HOSTNAME"]
    if "CLOUDFLARE_CREDENTIALS_FILE" in combined_env:
        cfg.tunnel.credentials_file = combined_env["CLOUDFLARE_CREDENTIALS_FILE"]

    # Sweeper / Auto-picker
    if "SWEEPER_ENABLED" in combined_env:
        cfg.sweeper.enabled = _to_bool(combined_env["SWEEPER_ENABLED"])
    if "SWEEPER_INTERVAL_SECONDS" in combined_env:
        cfg.sweeper.interval_seconds = _to_int(combined_env["SWEEPER_INTERVAL_SECONDS"], cfg.sweeper.interval_seconds)
    if "SWEEPER_SLEEP_DRIFT_THRESHOLD_SECONDS" in combined_env:
        cfg.sweeper.sleep_drift_threshold_seconds = _to_int(
            combined_env["SWEEPER_SLEEP_DRIFT_THRESHOLD_SECONDS"], cfg.sweeper.sleep_drift_threshold_seconds
        )
    if "SWEEPER_STALE_RUNNING_SECONDS" in combined_env:
        cfg.sweeper.stale_running_seconds = _to_int(
            combined_env["SWEEPER_STALE_RUNNING_SECONDS"], cfg.sweeper.stale_running_seconds
        )
    if "SWEEPER_AUTO_RETRY_INTERRUPTED" in combined_env:
        cfg.sweeper.auto_retry_interrupted = _to_bool(combined_env["SWEEPER_AUTO_RETRY_INTERRUPTED"])
    if "SWEEPER_MAX_AUTO_RETRIES" in combined_env:
        cfg.sweeper.max_auto_retries = _to_int(combined_env["SWEEPER_MAX_AUTO_RETRIES"], cfg.sweeper.max_auto_retries)
    if "SWEEPER_REHYDRATE_ORPHANED_EVENTS" in combined_env:
        cfg.sweeper.rehydrate_orphaned_events = _to_bool(combined_env["SWEEPER_REHYDRATE_ORPHANED_EVENTS"])

    # Observability & Antigravity sidebar
    if "ANTIGRAVITY_OBSERVABILITY_ENABLED" in combined_env:
        cfg.observability.enabled = _to_bool(combined_env["ANTIGRAVITY_OBSERVABILITY_ENABLED"])
    if "ANTIGRAVITY_SIDECAR_SLUG" in combined_env:
        cfg.observability.sidecar_slug = str(combined_env["ANTIGRAVITY_SIDECAR_SLUG"])
    if "ANTIGRAVITY_EMIT_SIDECAR_EVENTS" in combined_env:
        cfg.observability.emit_sidecar_events = _to_bool(combined_env["ANTIGRAVITY_EMIT_SIDECAR_EVENTS"])
    if "ANTIGRAVITY_SIDECAR_DATA_DIR" in combined_env:
        cfg.observability.sidecar_data_dir = str(combined_env["ANTIGRAVITY_SIDECAR_DATA_DIR"])

    # Dashboard & Zero Trust Auth
    if "DASHBOARD_AUTH_ENABLED" in combined_env:
        cfg.dashboard.auth_enabled = _to_bool(combined_env["DASHBOARD_AUTH_ENABLED"])
    if "DASHBOARD_BASIC_AUTH_USER" in combined_env:
        cfg.dashboard.basic_auth_user = str(combined_env["DASHBOARD_BASIC_AUTH_USER"])
    elif "DASHBOARD_AUTH_USER" in combined_env:
        cfg.dashboard.basic_auth_user = str(combined_env["DASHBOARD_AUTH_USER"])
    if "DASHBOARD_BASIC_AUTH_PASS" in combined_env:
        cfg.dashboard.basic_auth_pass = str(combined_env["DASHBOARD_BASIC_AUTH_PASS"])
    elif "DASHBOARD_AUTH_PASSWORD" in combined_env:
        cfg.dashboard.basic_auth_pass = str(combined_env["DASHBOARD_AUTH_PASSWORD"])
    if "DASHBOARD_AUTH_TOKEN" in combined_env:
        cfg.dashboard.auth_token = str(combined_env["DASHBOARD_AUTH_TOKEN"])
    if "CLOUDFLARE_ACCESS_AUD" in combined_env:
        cfg.dashboard.cloudflare_access_aud = str(combined_env["CLOUDFLARE_ACCESS_AUD"])
    elif "DASHBOARD_CF_ACCESS_AUD" in combined_env:
        cfg.dashboard.cloudflare_access_aud = str(combined_env["DASHBOARD_CF_ACCESS_AUD"])
    if "CLOUDFLARE_ACCESS_TEAM" in combined_env:
        cfg.dashboard.cloudflare_access_team = str(combined_env["CLOUDFLARE_ACCESS_TEAM"])
    if "DASHBOARD_ALLOWED_EMAILS" in combined_env:
        emails_raw = combined_env["DASHBOARD_ALLOWED_EMAILS"]
        if isinstance(emails_raw, str):
            cfg.dashboard.allowed_emails = [e.strip() for e in emails_raw.split(",") if e.strip()]
        elif isinstance(emails_raw, list):
            cfg.dashboard.allowed_emails = emails_raw

    # Antigravity Watchdog
    if "ANTIGRAVITY_WATCHDOG_ENABLED" in combined_env:
        cfg.antigravity_watchdog.enabled = _to_bool(combined_env["ANTIGRAVITY_WATCHDOG_ENABLED"])
    if "ANTIGRAVITY_WATCHDOG_INTERVAL_SECONDS" in combined_env:
        cfg.antigravity_watchdog.interval_seconds = _to_int(
            combined_env["ANTIGRAVITY_WATCHDOG_INTERVAL_SECONDS"], cfg.antigravity_watchdog.interval_seconds
        )
    if "ANTIGRAVITY_WATCHDOG_LOOKBACK_MINUTES" in combined_env:
        cfg.antigravity_watchdog.lookback_minutes = _to_int(
            combined_env["ANTIGRAVITY_WATCHDOG_LOOKBACK_MINUTES"], cfg.antigravity_watchdog.lookback_minutes
        )
    if "ANTIGRAVITY_WATCHDOG_STALL_GRACE_SECONDS" in combined_env:
        cfg.antigravity_watchdog.stall_grace_seconds = _to_int(
            combined_env["ANTIGRAVITY_WATCHDOG_STALL_GRACE_SECONDS"], cfg.antigravity_watchdog.stall_grace_seconds
        )
    if "ANTIGRAVITY_WATCHDOG_BOOST_QUIET_SECONDS" in combined_env:
        cfg.antigravity_watchdog.boost_quiet_seconds = _to_int(
            combined_env["ANTIGRAVITY_WATCHDOG_BOOST_QUIET_SECONDS"], cfg.antigravity_watchdog.boost_quiet_seconds
        )
    if "ANTIGRAVITY_WATCHDOG_MAX_RETRIES" in combined_env:
        cfg.antigravity_watchdog.max_retries_per_session = _to_int(
            combined_env["ANTIGRAVITY_WATCHDOG_MAX_RETRIES"], cfg.antigravity_watchdog.max_retries_per_session
        )
    if "ANTIGRAVITY_WATCHDOG_AUTO_RESUSCITATE" in combined_env:
        cfg.antigravity_watchdog.auto_resuscitate = _to_bool(combined_env["ANTIGRAVITY_WATCHDOG_AUTO_RESUSCITATE"])
    if "ANTIGRAVITY_WATCHDOG_PROBE_HOST" in combined_env:
        cfg.antigravity_watchdog.probe_host = str(combined_env["ANTIGRAVITY_WATCHDOG_PROBE_HOST"])
    if "ANTIGRAVITY_WATCHDOG_PROBE_PORT" in combined_env:
        cfg.antigravity_watchdog.probe_port = _to_int(
            combined_env["ANTIGRAVITY_WATCHDOG_PROBE_PORT"], cfg.antigravity_watchdog.probe_port
        )
    if "ANTIGRAVITY_WATCHDOG_PROBE_TIMEOUT_SECONDS" in combined_env:
        try:
            cfg.antigravity_watchdog.probe_timeout_seconds = float(
                combined_env["ANTIGRAVITY_WATCHDOG_PROBE_TIMEOUT_SECONDS"]
            )
        except (ValueError, TypeError):
            pass
    if "ANTIGRAVITY_WATCHDOG_BRAIN_DIR" in combined_env:
        cfg.antigravity_watchdog.brain_dir = str(combined_env["ANTIGRAVITY_WATCHDOG_BRAIN_DIR"])
    if "ANTIGRAVITY_WATCHDOG_SIDECAR_DATA_DIR" in combined_env:
        cfg.antigravity_watchdog.sidecar_data_dir = str(combined_env["ANTIGRAVITY_WATCHDOG_SIDECAR_DATA_DIR"])

    # Antigravity Quota Sentinel
    if "ANTIGRAVITY_QUOTA_ENABLED" in combined_env:
        cfg.antigravity_quota.enabled = _to_bool(combined_env["ANTIGRAVITY_QUOTA_ENABLED"])
    if "ANTIGRAVITY_QUOTA_SCAN_INTERVAL_SECONDS" in combined_env:
        cfg.antigravity_quota.scan_interval_seconds = _to_int(
            combined_env["ANTIGRAVITY_QUOTA_SCAN_INTERVAL_SECONDS"], cfg.antigravity_quota.scan_interval_seconds
        )
    if "ANTIGRAVITY_QUOTA_AUTO_WARMUP_5H" in combined_env:
        cfg.antigravity_quota.auto_warmup_5h = _to_bool(combined_env["ANTIGRAVITY_QUOTA_AUTO_WARMUP_5H"])
    if "ANTIGRAVITY_QUOTA_AUTO_WARMUP_WEEKLY" in combined_env:
        cfg.antigravity_quota.auto_warmup_weekly = _to_bool(combined_env["ANTIGRAVITY_QUOTA_AUTO_WARMUP_WEEKLY"])
    if "ANTIGRAVITY_QUOTA_WARMUP_COOLDOWN_SECONDS" in combined_env:
        cfg.antigravity_quota.warmup_cooldown_seconds = _to_int(
            combined_env["ANTIGRAVITY_QUOTA_WARMUP_COOLDOWN_SECONDS"], cfg.antigravity_quota.warmup_cooldown_seconds
        )
    if "ANTIGRAVITY_QUOTA_TOOLS_API_HOST" in combined_env:
        cfg.antigravity_quota.tools_api_host = str(combined_env["ANTIGRAVITY_QUOTA_TOOLS_API_HOST"])
    if "ANTIGRAVITY_QUOTA_TOOLS_API_PORT" in combined_env:
        cfg.antigravity_quota.tools_api_port = _to_int(
            combined_env["ANTIGRAVITY_QUOTA_TOOLS_API_PORT"], cfg.antigravity_quota.tools_api_port
        )
    if "ANTIGRAVITY_QUOTA_TOOLS_API_TIMEOUT_SECONDS" in combined_env:
        try:
            cfg.antigravity_quota.tools_api_timeout_seconds = float(
                combined_env["ANTIGRAVITY_QUOTA_TOOLS_API_TIMEOUT_SECONDS"]
            )
        except (ValueError, TypeError):
            pass
    if "ANTIGRAVITY_QUOTA_DEFAULT_GEMINI_MODEL" in combined_env:
        cfg.antigravity_quota.default_gemini_model = str(combined_env["ANTIGRAVITY_QUOTA_DEFAULT_GEMINI_MODEL"])
    if "ANTIGRAVITY_QUOTA_DEFAULT_3P_MODEL" in combined_env:
        cfg.antigravity_quota.default_3p_model = str(combined_env["ANTIGRAVITY_QUOTA_DEFAULT_3P_MODEL"])
    if "ANTIGRAVITY_QUOTA_ACCOUNTS_DIR" in combined_env:
        cfg.antigravity_quota.accounts_dir = str(combined_env["ANTIGRAVITY_QUOTA_ACCOUNTS_DIR"])

    # 5. Apply CLI / Explicit overrides (highest precedence)
    if cli_overrides:
        for k, v in cli_overrides.items():
            if v is None:
                continue
            # Support dot notation e.g. "server.port"
            if "." in k:
                section, key = k.split(".", 1)
                sub_obj = getattr(cfg, section, None)
                if sub_obj is not None and hasattr(sub_obj, key):
                    # Cast if needed
                    current_val = getattr(sub_obj, key)
                    if isinstance(current_val, int):
                        setattr(sub_obj, key, int(v))
                    elif isinstance(current_val, bool):
                        setattr(sub_obj, key, _to_bool(v))
                    else:
                        setattr(sub_obj, key, v)
            else:
                # Flat keys matching attribute names across sections
                if hasattr(cfg.server, k):
                    curr = getattr(cfg.server, k)
                    setattr(cfg.server, k, int(v) if isinstance(curr, int) else v)
                elif hasattr(cfg.security, k):
                    curr = getattr(cfg.security, k)
                    setattr(cfg.security, k, int(v) if isinstance(curr, int) else v)
                elif hasattr(cfg.database, k):
                    curr = getattr(cfg.database, k)
                    setattr(cfg.database, k, int(v) if isinstance(curr, int) else v)
                elif hasattr(cfg.dispatch, k):
                    curr = getattr(cfg.dispatch, k)
                    setattr(cfg.dispatch, k, int(v) if isinstance(curr, int) else v)
                elif hasattr(cfg.tunnel, k):
                    curr = getattr(cfg.tunnel, k)
                    setattr(cfg.tunnel, k, _to_bool(v) if isinstance(curr, bool) else v)
                elif hasattr(cfg.sweeper, k):
                    curr = getattr(cfg.sweeper, k)
                    if isinstance(curr, bool):
                        setattr(cfg.sweeper, k, _to_bool(v))
                    elif isinstance(curr, int):
                        setattr(cfg.sweeper, k, int(v))
                    else:
                        setattr(cfg.sweeper, k, v)
                elif hasattr(cfg.observability, k):
                    curr = getattr(cfg.observability, k)
                    if isinstance(curr, bool):
                        setattr(cfg.observability, k, _to_bool(v))
                    else:
                        setattr(cfg.observability, k, v)
                elif hasattr(cfg.dashboard, k):
                    curr = getattr(cfg.dashboard, k)
                    if isinstance(curr, bool):
                        setattr(cfg.dashboard, k, _to_bool(v))
                    elif isinstance(curr, list) and isinstance(v, str):
                        setattr(cfg.dashboard, k, [x.strip() for x in v.split(",") if x.strip()])
                    else:
                        setattr(cfg.dashboard, k, v)
                elif hasattr(cfg.antigravity_watchdog, k):
                    curr = getattr(cfg.antigravity_watchdog, k)
                    if isinstance(curr, bool):
                        setattr(cfg.antigravity_watchdog, k, _to_bool(v))
                    elif isinstance(curr, int):
                        setattr(cfg.antigravity_watchdog, k, int(v))
                    else:
                        setattr(cfg.antigravity_watchdog, k, v)
                elif hasattr(cfg.antigravity_quota, k):
                    curr = getattr(cfg.antigravity_quota, k)
                    if isinstance(curr, bool):
                        setattr(cfg.antigravity_quota, k, _to_bool(v))
                    elif isinstance(curr, int):
                        setattr(cfg.antigravity_quota, k, int(v))
                    else:
                        setattr(cfg.antigravity_quota, k, v)

    return cfg


def validate_config(config: AppConfig, strict: bool = False) -> list[str]:
    """Validate loaded config and return any errors or warnings."""
    issues: list[str] = []

    if not (1 <= config.server.port <= 65535):
        issues.append(f"Invalid server port: {config.server.port} (must be 1-65535)")

    valid_auth_modes = {"hmac", "bearer", "any", "both"}
    if config.security.auth_mode not in valid_auth_modes:
        issues.append(
            f"Invalid security auth_mode: '{config.security.auth_mode}'. "
            f"Allowed modes: {', '.join(sorted(valid_auth_modes))}"
        )

    if strict:
        if config.security.auth_mode in ("hmac", "both") and not config.security.webhook_secret:
            issues.append("Strict mode error: WEBHOOK_SECRET is required when auth_mode includes hmac.")
        if config.security.auth_mode in ("bearer", "both") and not config.security.bearer_token:
            issues.append("Strict mode error: BEARER_TOKEN is required when auth_mode includes bearer.")

    return issues
