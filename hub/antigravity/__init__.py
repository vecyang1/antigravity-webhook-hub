"""
Antigravity Webhook Hub — Antigravity Agent Operations Package
Provides multi-modal task ingestion, session-thread linking, follow-up (追问) routing,
and Slack thread milestone notifications for Google Antigravity.
"""

from hub.antigravity.agentapi_client import (
    AgentAPIClient,
    discover_active_antigravity_credentials,
    resolve_agentapi_path,
    validate_antigravity_address,
)
from hub.antigravity.models import AntigravityTaskPayload, SessionThreadRecord, ThreadMilestone
from hub.antigravity.prompt_builder import (
    build_antigravity_prompt,
    build_follow_up_prompt,
    extract_slash_commands,
)
from hub.antigravity.session_manager import execute_antigravity_task
from hub.antigravity.thread_notifier import ThreadNotifier

__all__ = [
    "AgentAPIClient",
    "AntigravityTaskPayload",
    "SessionThreadRecord",
    "ThreadMilestone",
    "ThreadNotifier",
    "build_antigravity_prompt",
    "build_follow_up_prompt",
    "discover_active_antigravity_credentials",
    "execute_antigravity_task",
    "extract_slash_commands",
    "resolve_agentapi_path",
    "validate_antigravity_address",
]
