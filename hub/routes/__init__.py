"""
Antigravity Webhook Hub — Routes Package
"""

from hub.routes.observability import register_observability_routes
from hub.routes.sse import register_sse_routes
from hub.routes.tasks import register_task_routes
from hub.routes.uptime_kuma import register_uptime_kuma_routes
from hub.routes.webhook import register_webhook_routes

__all__ = [
    "register_observability_routes",
    "register_sse_routes",
    "register_task_routes",
    "register_uptime_kuma_routes",
    "register_webhook_routes",
]

