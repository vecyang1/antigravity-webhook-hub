"""
Antigravity Webhook Hub — Routes Package
Implements PEP 562 lazy module loading to keep startup memory strictly <30MB.
"""

from typing import Any

__all__ = [
    "register_agent_activities_routes",
    "register_dashboard_routes",
    "register_observability_routes",
    "register_schedule_routes",
    "register_sse_routes",
    "register_task_routes",
    "register_uptime_kuma_routes",
    "register_webhook_routes",
]


def __getattr__(name: str) -> Any:
    if name == "register_agent_activities_routes":
        from hub.routes.agent_activities import register_agent_activities_routes
        return register_agent_activities_routes
    elif name == "register_dashboard_routes":
        from hub.routes.dashboard import register_dashboard_routes
        return register_dashboard_routes
    elif name == "register_observability_routes":
        from hub.routes.observability import register_observability_routes
        return register_observability_routes
    elif name == "register_schedule_routes":
        from hub.routes.schedules import register_schedule_routes
        return register_schedule_routes
    elif name == "register_sse_routes":
        from hub.routes.sse import register_sse_routes
        return register_sse_routes
    elif name == "register_task_routes":
        from hub.routes.tasks import register_task_routes
        return register_task_routes
    elif name == "register_uptime_kuma_routes":
        from hub.routes.uptime_kuma import register_uptime_kuma_routes
        return register_uptime_kuma_routes
    elif name == "register_webhook_routes":
        from hub.routes.webhook import register_webhook_routes
        return register_webhook_routes
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

