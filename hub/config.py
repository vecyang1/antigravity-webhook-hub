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
    path: str = "data/webhook_hub.db"
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
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    dispatch: DispatchConfig = field(default_factory=DispatchConfig)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)

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
