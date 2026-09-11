"""
Antigravity Webhook Hub — Observable Activity Web Dashboard
Real-time web interface providing deep observability into webhook-triggered Antigravity activities.
Adheres strictly to:
- Single Source of Truth (SSOT): UI renders authoritative projection from SQLite via API.
- Unidirectional Data Flow: UI actions -> API write -> SQLite persist -> re-read authoritative API -> re-render.
- Real-time Push Subscription: Server-Sent Events (SSE) via /events/stream.
- Zero-dependency footprint: Lightweight embedded HTML/CSS/JS maintaining <30MB RSS budget.
- Premium aesthetics: Slate dark mode, responsive layout, inline SVG icons, zero emoji icons.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from hub.config import AppConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.dashboard")


def render_dashboard_html(config: Optional[AppConfig] = None, db: Optional[Any] = None) -> str:
    """Generate the self-contained SPA HTML string for the Webhook Hub dashboard (lazily imported)."""
    from hub.routes.dashboard_template import render_dashboard_html as _render
    return _render(config, db)


def render_unauthorized_html(auth_res: Any) -> str:
    """Render a premium dark-mode 401 Unauthorized page adhering to /ui-ux-pro-max styling."""
    return (
        f"<!DOCTYPE html><html lang=\"en\" class=\"dark\"><head><meta charset=\"UTF-8\">"
        f"<title>401 Unauthorized — Antigravity Webhook Hub</title>"
        f"<style>body{{background:#090d16;color:#f8fafc;font-family:system-ui,-apple-system,sans-serif;"
        f"display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:20px;}}"
        f".card{{background:#0f172a;padding:32px 36px;border-radius:12px;border:1px solid #1e293b;"
        f"max-width:440px;width:100%;text-align:center;box-shadow:0 10px 25px -5px rgba(0,0,0,0.5);}}"
        f"h1{{font-size:18px;margin:0 0 8px 0;color:#ef4444;font-weight:600;}}"
        f"p{{color:#94a3b8;font-size:14px;line-height:1.6;margin:0 0 20px 0;}}"
        f".badge{{display:inline-block;padding:4px 10px;border-radius:9999px;font-size:12px;font-weight:500;"
        f"background:rgba(239,68,68,0.15);color:#fca5a5;border:1px solid rgba(239,68,68,0.25);margin-bottom:16px;}}"
        f"code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;background:#1e293b;"
        f"padding:2px 6px;border-radius:4px;color:#cbd5e1;}}</style></head>"
        f"<body><div class=\"card\">"
        f"<div class=\"badge\">Authentication Required</div>"
        f"<h1>Access Denied</h1>"
        f"<p>{auth_res.message}</p>"
        f"<p style=\"font-size:12px;color:#64748b;\">Reason: <code>{auth_res.reason}</code></p>"
        f"</div></body></html>"
    )


def register_dashboard_routes(
    server: AsyncHTTPServer,
    config: AppConfig,
    db: Optional[Any] = None,
    broker: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
) -> None:
    """Register Webhook Hub Dashboard routes: GET /dashboard and GET /ui."""

    async def handle_dashboard(req: HTTPRequest) -> HTTPResponse:
        """Serve the Observable Activities Web Dashboard HTML."""
        from hub.security import verify_dashboard_auth

        auth_res = verify_dashboard_auth(req, config)
        if not auth_res.is_valid:
            realm = 'Basic realm="Antigravity Webhook Hub Dashboard"'
            accept = req.header("accept") or ""
            if "application/json" in accept.lower():
                headers = {"WWW-Authenticate": realm} if auth_res.status_code == 401 else {}
                return HTTPResponse.json(
                    {"error": "unauthorized", "message": auth_res.message, "reason": auth_res.reason},
                    status_code=auth_res.status_code or 401,
                    headers=headers,
                )

            headers = {
                "WWW-Authenticate": realm,
                "Content-Type": "text/html; charset=utf-8",
            }
            body = render_unauthorized_html(auth_res)
            return HTTPResponse(status_code=auth_res.status_code or 401, headers=headers, body=body.encode("utf-8"))

        from hub.routes.dashboard_template import render_dashboard_html as _render
        html = _render(config, db)
        return HTTPResponse.text(html, status_code=200, content_type="text/html; charset=utf-8")

    server.add_route("GET", "/dashboard", handle_dashboard)
    server.add_route("GET", "/ui", handle_dashboard)

    try:
        from hub.routes.agent_activities import register_agent_activities_routes
        register_agent_activities_routes(server, config, db, broker, dispatcher)
    except Exception as act_err:
        logger.debug("Failed registering agent activities routes in dashboard: %s", act_err)
