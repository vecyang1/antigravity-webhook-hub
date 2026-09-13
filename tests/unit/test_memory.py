"""Unit tests for hub.memory centralized memory management and footprint controller."""

from __future__ import annotations

import inspect
import os
import sys

import pytest

import hub
from hub.config import AppConfig
from hub.memory import (
    apply_memory_pressure_relief,
    get_memory_budget_mb,
    get_memory_rss_bytes,
    get_memory_rss_mb,
)


def test_memory_pressure_relief_executes_safely():
    # Should execute without raising exceptions across platforms
    apply_memory_pressure_relief()


def test_get_memory_rss_bytes_and_mb():
    b = get_memory_rss_bytes()
    assert isinstance(b, int)
    assert b > 0

    mb = get_memory_rss_mb()
    assert isinstance(mb, float)
    assert mb > 0.0


def test_get_memory_budget_mb_resolution():
    # 1. Default
    assert get_memory_budget_mb() == 64.0

    # 2. Config override
    cfg = AppConfig()
    cfg.server.memory_budget_mb = 45.0
    assert get_memory_budget_mb(cfg) == 45.0

    # 3. Env override
    old_env = os.environ.get("MEMORY_BUDGET_MB")
    try:
        os.environ["MEMORY_BUDGET_MB"] = "25.5"
        assert get_memory_budget_mb(cfg) == 25.5
    finally:
        if old_env is None:
            os.environ.pop("MEMORY_BUDGET_MB", None)
        else:
            os.environ["MEMORY_BUDGET_MB"] = old_env


def test_hub_pep562_lazy_exports_and_annotations():
    # PEP 649 annotation introspection must not raise NameError
    ann = inspect.get_annotations(hub.__getattr__)
    assert ann.get("name") in ("str", str)
    assert ann.get("return") in ("Any", getattr(__import__("typing"), "Any"))

    # Memory functions exported and lazy-loaded
    assert callable(hub.apply_memory_pressure_relief)
    assert callable(hub.get_memory_rss_bytes)
    assert callable(hub.get_memory_rss_mb)
    assert callable(hub.get_memory_budget_mb)

    # __dir__ includes exports
    d = dir(hub)
    assert "apply_memory_pressure_relief" in d
    assert "AsyncHTTPServer" in d
    assert "TaskDispatcher" not in d  # only in __all__
