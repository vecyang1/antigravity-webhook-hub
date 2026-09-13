"""
Antigravity Webhook Hub — Antigravity Agent Operations Package
Provides multi-modal task ingestion, session-thread linking, follow-up (追问) routing,
and Slack thread milestone notifications for Google Antigravity.
"""

from typing import Any

__all__ = [
    "AgentAPIClient",
    "AntigravityTaskPayload",
    "AntigravityWatchdog",
    "SessionThreadRecord",
    "StalledSessionInfo",
    "ThreadMilestone",
    "ThreadNotifier",
    "build_antigravity_prompt",
    "build_follow_up_prompt",
    "discover_active_antigravity_credentials",
    "execute_antigravity_task",
    "extract_slash_commands",
    "get_latest_step_index",
    "parse_transcript_events",
    "resolve_agentapi_path",
    "resolve_transcript_path",
    "validate_antigravity_address",
    "watch_and_deliver_result",
]

_LAZY_MODULES: dict[str, tuple[str, str]] = {
    "AgentAPIClient": ("hub.antigravity.agentapi_client", "AgentAPIClient"),
    "discover_active_antigravity_credentials": ("hub.antigravity.agentapi_client", "discover_active_antigravity_credentials"),
    "resolve_agentapi_path": ("hub.antigravity.agentapi_client", "resolve_agentapi_path"),
    "validate_antigravity_address": ("hub.antigravity.agentapi_client", "validate_antigravity_address"),
    "AntigravityTaskPayload": ("hub.antigravity.models", "AntigravityTaskPayload"),
    "SessionThreadRecord": ("hub.antigravity.models", "SessionThreadRecord"),
    "ThreadMilestone": ("hub.antigravity.models", "ThreadMilestone"),
    "build_antigravity_prompt": ("hub.antigravity.prompt_builder", "build_antigravity_prompt"),
    "build_follow_up_prompt": ("hub.antigravity.prompt_builder", "build_follow_up_prompt"),
    "extract_slash_commands": ("hub.antigravity.prompt_builder", "extract_slash_commands"),
    "get_latest_step_index": ("hub.antigravity.result_delivery", "get_latest_step_index"),
    "parse_transcript_events": ("hub.antigravity.result_delivery", "parse_transcript_events"),
    "resolve_transcript_path": ("hub.antigravity.result_delivery", "resolve_transcript_path"),
    "watch_and_deliver_result": ("hub.antigravity.result_delivery", "watch_and_deliver_result"),
    "execute_antigravity_task": ("hub.antigravity.session_manager", "execute_antigravity_task"),
    "ThreadNotifier": ("hub.antigravity.thread_notifier", "ThreadNotifier"),
    "AntigravityWatchdog": ("hub.antigravity.watchdog", "AntigravityWatchdog"),
    "StalledSessionInfo": ("hub.antigravity.watchdog", "StalledSessionInfo"),
}


def __getattr__(name: str) -> Any:
    """Lazy-load public package attributes on access (PEP 562) to maintain strict <30MB RSS footprint."""
    if name in _LAZY_MODULES:
        mod_name, attr_name = _LAZY_MODULES[name]
        import importlib
        mod = importlib.import_module(mod_name)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'hub.antigravity' has no attribute '{name}'")


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals().keys()))
