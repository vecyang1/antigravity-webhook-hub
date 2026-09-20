"""Unit tests for hub.config configuration loading, interpolation, and hierarchy."""

import os
import tempfile
from pathlib import Path

from hub.config import AppConfig, load_config, validate_config


def test_default_config():
    cfg = load_config(env_path="")
    assert cfg.server.port == 9423
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.max_body_bytes == 1048576
    assert cfg.security.auth_mode == "hmac"
    assert cfg.security.timestamp_tolerance_seconds == 300
    assert cfg.security.future_timestamp_tolerance_seconds == 60
    assert cfg.database.wal_mode is True


def test_cli_overrides_precedence():
    # CLI overrides should beat everything
    cfg = load_config(cli_overrides={"port": 9999, "auth_mode": "bearer"})
    assert cfg.server.port == 9999
    assert cfg.security.auth_mode == "bearer"


def test_dot_notation_cli_overrides():
    cfg = load_config(cli_overrides={"server.port": 8443, "security.webhook_secret": "my_secret"})
    assert cfg.server.port == 8443
    assert cfg.security.webhook_secret == "my_secret"


def test_env_file_loading():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
        f.write("PORT=9876\n")
        f.write("AUTH_MODE=any\n")
        f.write("WEBHOOK_SECRET=env_secret_value_1234567890\n")
        f.write('BEARER_TOKEN="quoted_bearer_token"\n')
        f.write("# This is a comment\n")
        f.write("MAX_BODY_BYTES=524288\n")
        env_path = f.name

    try:
        cfg = load_config(env_path=env_path)
        assert cfg.server.port == 9876
        assert cfg.security.auth_mode == "any"
        assert cfg.security.webhook_secret == "env_secret_value_1234567890"
        assert cfg.security.bearer_token == "quoted_bearer_token"
        assert cfg.server.max_body_bytes == 524288
    finally:
        os.unlink(env_path)


def test_yaml_config_with_env_interpolation():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(
            """
server:
  port: 9555
  host: "0.0.0.0"
security:
  auth_mode: "bearer"
  bearer_token: "${TEST_TOKEN_VAR:-fallback_token}"
database:
  path: "/tmp/custom.db"
"""
        )
        yaml_path = f.name

    try:
        os.environ["TEST_TOKEN_VAR"] = "injected_token_from_env"
        cfg = load_config(config_path=yaml_path)
        assert cfg.server.port == 9555
        assert cfg.server.host == "0.0.0.0"
        assert cfg.security.bearer_token == "injected_token_from_env"
        assert cfg.database.path == "/tmp/custom.db"
    finally:
        os.environ.pop("TEST_TOKEN_VAR", None)
        os.unlink(yaml_path)


def test_validation():
    cfg = AppConfig()
    cfg.server.port = 99999  # Invalid port
    cfg.security.auth_mode = "invalid_mode"
    issues = validate_config(cfg, strict=True)
    assert len(issues) >= 2
    assert any("port" in i for i in issues)
    assert any("auth_mode" in i for i in issues)


def test_memory_budget_config_loading_and_serialization():
    # 1. Default value
    cfg = AppConfig()
    assert cfg.server.memory_budget_mb == 128.0

    # 2. Serialization in to_dict()
    d = cfg.to_dict()
    assert "server" in d
    assert d["server"]["memory_budget_mb"] == 128.0

    # 3. Environment variable loading
    old_env = os.environ.get("MEMORY_BUDGET_MB")
    try:
        os.environ["MEMORY_BUDGET_MB"] = "96.5"
        loaded_cfg = load_config(env_path="")
        assert loaded_cfg.server.memory_budget_mb == 96.5
    finally:
        if old_env is None:
            os.environ.pop("MEMORY_BUDGET_MB", None)
        else:
            os.environ["MEMORY_BUDGET_MB"] = old_env
