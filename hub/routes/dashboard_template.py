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

import json
import logging
from typing import Any, Optional

from hub.config import AppConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.dashboard")


def render_dashboard_html(config: Optional[AppConfig] = None, db: Optional[Any] = None) -> str:
    """Generate the self-contained SPA HTML string for the Webhook Hub dashboard."""
    server_port = getattr(config.server, "port", 9423) if config and hasattr(config, "server") else 9423
    tunnel_host = "webhook.worldinspirelab.com"

    return f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Antigravity Webhook Hub — Observable Activity Console</title>
  <style>
    :root {{
      --bg-base: #090d16;
      --bg-surface: #0f172a;
      --bg-card: #1e293b;
      --bg-hover: #334155;
      --border-color: #1e293b;
      --border-subtle: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-subtle: #64748b;
      --accent-blue: #3b82f6;
      --accent-blue-hover: #2563eb;
      --status-success: #10b981;
      --status-warning: #f59e0b;
      --status-danger: #ef4444;
      --status-running: #6366f1;
      --status-queued: #0ea5e9;
      --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body {{
      background-color: var(--bg-base);
      color: var(--text-main);
      font-family: var(--font-sans);
      font-size: 14px;
      line-height: 1.5;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
    }}
    /* Layout */
    .app-container {{
      display: flex;
      flex: 1;
      height: 100vh;
      overflow: hidden;
    }}
    /* Sidebar */
    .sidebar {{
      width: 290px;
      background: var(--bg-surface);
      border-right: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      transition: transform 0.2s ease;
      z-index: 40;
    }}
    .sidebar-header {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .brand-title {{
      font-size: 15px;
      font-weight: 700;
      letter-spacing: -0.01em;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .brand-title svg {{
      width: 22px;
      height: 22px;
      color: var(--accent-blue);
    }}
    .live-badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 11px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 9999px;
      background: rgba(16, 185, 129, 0.12);
      color: var(--status-success);
      border: 1px solid rgba(16, 185, 129, 0.25);
    }}
    .live-pulse {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background-color: var(--status-success);
      animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; transform: scale(1); }}
      50% {{ opacity: 0.35; transform: scale(0.85); }}
    }}
    .sidebar-content {{
      flex: 1;
      overflow-y: auto;
      padding: 16px 14px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}
    .sidebar-section-title {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--text-subtle);
      margin-bottom: 8px;
      padding-left: 8px;
    }}
    .nav-list {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .nav-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 12px;
      border-radius: 6px;
      color: var(--text-muted);
      cursor: pointer;
      text-decoration: none;
      font-weight: 500;
      transition: background 0.15s ease, color 0.15s ease;
    }}
    .nav-item:hover {{
      background: var(--bg-card);
      color: var(--text-main);
    }}
    .nav-item.active {{
      background: rgba(59, 130, 246, 0.15);
      color: #60a5fa;
      border-left: 3px solid var(--accent-blue);
    }}
    .nav-item-left {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .nav-item svg {{
      width: 16px;
      height: 16px;
      flex-shrink: 0;
    }}
    .nav-count {{
      font-size: 11px;
      font-weight: 600;
      padding: 1px 7px;
      border-radius: 9999px;
      background: var(--border-subtle);
      color: var(--text-main);
    }}
    .nav-item.active .nav-count {{
      background: var(--accent-blue);
      color: #ffffff;
    }}
    .telemetry-card {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .telemetry-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 12px;
    }}
    .telemetry-label {{
      color: var(--text-muted);
    }}
    .telemetry-value {{
      font-family: var(--font-mono);
      font-weight: 600;
      color: var(--text-main);
    }}
    .progress-bar-container {{
      width: 100%;
      height: 6px;
      background: var(--border-color);
      border-radius: 3px;
      overflow: hidden;
      margin-top: 2px;
    }}
    .progress-bar-fill {{
      height: 100%;
      background: var(--accent-blue);
      border-radius: 3px;
      transition: width 0.3s ease;
    }}
    .sidebar-footer {{
      padding: 14px 16px;
      border-top: 1px solid var(--border-color);
      background: var(--bg-surface);
      display: flex;
      flex-direction: column;
      gap: 8px;
      font-size: 11px;
      color: var(--text-subtle);
    }}
    .endpoint-link {{
      color: var(--text-muted);
      text-decoration: none;
      display: flex;
      align-items: center;
      gap: 6px;
      word-break: break-all;
    }}
    .endpoint-link:hover {{
      color: var(--accent-blue);
    }}
    .endpoint-link svg {{
      width: 12px;
      height: 12px;
      flex-shrink: 0;
    }}
    /* Main Area */
    .main-area {{
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg-base);
    }}
    .topbar {{
      height: 56px;
      border-bottom: 1px solid var(--border-color);
      background: var(--bg-surface);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 24px;
      flex-shrink: 0;
    }}
    .topbar-left {{
      display: flex;
      align-items: center;
      gap: 16px;
    }}
    .view-title {{
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.01em;
    }}
    .topbar-right {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .search-input-wrapper {{
      position: relative;
      display: flex;
      align-items: center;
    }}
    .search-icon {{
      position: absolute;
      left: 10px;
      width: 14px;
      height: 14px;
      color: var(--text-subtle);
      pointer-events: none;
    }}
    .search-input {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      color: var(--text-main);
      padding: 6px 12px 6px 32px;
      border-radius: 6px;
      font-size: 13px;
      width: 220px;
      outline: none;
      transition: border-color 0.15s ease, width 0.2s ease;
    }}
    .search-input:focus {{
      border-color: var(--accent-blue);
      width: 280px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      border: 1px solid transparent;
      outline: none;
      transition: all 0.15s ease;
    }}
    .btn svg {{
      width: 14px;
      height: 14px;
    }}
    .btn-primary {{
      background: var(--accent-blue);
      color: #ffffff;
    }}
    .btn-primary:hover {{
      background: var(--accent-blue-hover);
    }}
    .btn-secondary {{
      background: var(--bg-card);
      border-color: var(--border-subtle);
      color: var(--text-main);
    }}
    .btn-secondary:hover {{
      background: var(--bg-hover);
    }}
    .content-viewport {{
      flex: 1;
      overflow-y: auto;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}
    /* Stats Row */
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 16px;
    }}
    .metric-box {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .metric-box-title {{
      font-size: 12px;
      color: var(--text-muted);
      font-weight: 500;
    }}
    .metric-box-value {{
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }}
    /* Table Card */
    .table-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    .table-header {{
      padding: 14px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .table-title {{
      font-size: 14px;
      font-weight: 600;
    }}
    .table-wrapper {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 13px;
    }}
    th {{
      padding: 10px 16px;
      background: rgba(15, 23, 42, 0.6);
      border-bottom: 1px solid var(--border-color);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      white-space: nowrap;
    }}
    td {{
      padding: 12px 16px;
      border-bottom: 1px solid var(--border-color);
      color: var(--text-main);
      white-space: nowrap;
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    tbody tr:hover {{
      background: rgba(30, 41, 59, 0.4);
      cursor: pointer;
    }}
    /* Badges */
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 2px 8px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 600;
      text-transform: capitalize;
    }}
    .badge-succeeded {{
      background: rgba(16, 185, 129, 0.15);
      color: var(--status-success);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }}
    .badge-failed {{
      background: rgba(239, 68, 68, 0.15);
      color: var(--status-danger);
      border: 1px solid rgba(239, 68, 68, 0.3);
    }}
    .badge-running {{
      background: rgba(99, 102, 241, 0.15);
      color: var(--status-running);
      border: 1px solid rgba(99, 102, 241, 0.3);
    }}
    .badge-queued {{
      background: rgba(14, 165, 233, 0.15);
      color: var(--status-queued);
      border: 1px solid rgba(14, 165, 233, 0.3);
    }}
    .badge-received {{
      background: rgba(148, 163, 184, 0.15);
      color: var(--text-muted);
      border: 1px solid var(--border-subtle);
    }}
    .badge-timed_out {{
      background: rgba(245, 158, 11, 0.15);
      color: var(--status-warning);
      border: 1px solid rgba(245, 158, 11, 0.3);
    }}
    .source-tag {{
      display: inline-block;
      font-size: 11px;
      font-family: var(--font-mono);
      padding: 1px 6px;
      border-radius: 4px;
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      color: var(--text-muted);
    }}
    .task-id-code {{
      font-family: var(--font-mono);
      font-size: 12px;
      color: #93c5fd;
      text-decoration: none;
    }}
    .task-id-code:hover {{
      text-decoration: underline;
    }}
    /* Event Filter Controls */
    .filter-pill-group {{
      display: inline-flex;
      align-items: center;
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 2px;
      gap: 2px;
    }}
    .filter-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 600;
      border-radius: 4px;
      border: none;
      background: transparent;
      color: var(--text-muted);
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .filter-pill:hover {{
      color: var(--text-main);
      background: var(--bg-hover);
    }}
    .filter-pill.active {{
      background: var(--accent-blue);
      color: #ffffff;
    }}
    .filter-pill .pill-badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      border-radius: 9px;
      font-size: 10px;
      font-weight: 700;
      background: rgba(0, 0, 0, 0.35);
      color: var(--text-muted);
    }}
    .filter-pill.active .pill-badge {{
      background: rgba(255, 255, 255, 0.25);
      color: #ffffff;
    }}
    .badge-test {{
      background: rgba(148, 163, 184, 0.15);
      color: #94a3b8;
      border: 1px solid rgba(148, 163, 184, 0.3);
      font-size: 10px;
      padding: 1px 6px;
      text-transform: uppercase;
    }}
    .badge-real {{
      background: rgba(16, 185, 129, 0.15);
      color: #10b981;
      border: 1px solid rgba(16, 185, 129, 0.3);
      font-size: 10px;
      padding: 1px 6px;
      text-transform: uppercase;
    }}
    .btn-filter-active {{
      background: rgba(59, 130, 246, 0.2) !important;
      border-color: var(--accent-blue) !important;
      color: #93c5fd !important;
    }}
    /* Quota Sentinel & 5h Warmup UI Styles */
    .quota-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }}
    .quota-card {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 20px;
      position: relative;
      transition: all 0.2s ease;
    }}
    .quota-card-primary {{
      border-color: rgba(59, 130, 246, 0.4);
      background: linear-gradient(180deg, rgba(30, 41, 59, 0.7) 0%, var(--bg-card) 100%);
      box-shadow: 0 4px 20px -2px rgba(59, 130, 246, 0.15);
    }}
    .quota-badge-gemini {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 700;
      background: rgba(59, 130, 246, 0.2);
      color: #93c5fd;
      border: 1px solid rgba(59, 130, 246, 0.35);
    }}
    .quota-progress-track {{
      height: 10px;
      background: #090d16;
      border-radius: 9999px;
      overflow: hidden;
      margin: 10px 0 6px 0;
      border: 1px solid rgba(255, 255, 255, 0.05);
    }}
    .quota-progress-fill {{
      height: 100%;
      border-radius: 9999px;
      transition: width 0.4s ease, background 0.4s ease;
    }}
    .quota-desc-text {{
      font-size: 11px;
      color: var(--text-muted);
      line-height: 1.4;
      margin-top: 6px;
      font-style: italic;
    }}
    .countdown-pill {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 4px;
      background: rgba(15, 23, 42, 0.8);
      border: 1px solid var(--border-subtle);
      color: #38bdf8;
    }}
    /* Drawer */
    .drawer-overlay {{
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.6);
      backdrop-filter: blur(2px);
      z-index: 50;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }}
    .drawer-overlay.open {{
      opacity: 1;
      pointer-events: auto;
    }}
    .drawer {{
      position: fixed;
      top: 0;
      right: 0;
      bottom: 0;
      width: 780px;
      max-width: 95vw;
      background: var(--bg-surface);
      border-left: 1px solid var(--border-color);
      z-index: 60;
      display: flex;
      flex-direction: column;
      transform: translateX(100%);
      transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      box-shadow: -15px 0 35px rgba(0, 0, 0, 0.6);
    }}
    .drawer.open {{
      transform: translateX(0);
    }}
    .drawer-header {{
      padding: 14px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: rgba(15, 23, 42, 0.95);
      flex-shrink: 0;
    }}
    .drawer-title {{
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .drawer-close {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      padding: 6px;
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: all 0.15s ease;
    }}
    .drawer-close:hover {{
      color: var(--text-main);
      background: var(--bg-card);
    }}
    .drawer-body {{
      flex: 1;
      overflow: hidden;
      padding: 14px 18px 18px 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .drawer-meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
      gap: 8px;
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 10px 14px;
      flex-shrink: 0;
    }}
    .drawer-meta-item {{
      display: flex;
      flex-direction: column;
      gap: 2px;
    }}
    .drawer-meta-label {{
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-subtle);
    }}
    .drawer-meta-val {{
      font-size: 12px;
      font-weight: 500;
      color: var(--text-main);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-family: var(--font-mono);
    }}
    /* Log Section & Toolbar */
    .log-section {{
      flex: 1;
      display: flex;
      flex-direction: column;
      min-height: 0;
      gap: 8px;
    }}
    .log-toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
      background: rgba(15, 23, 42, 0.7);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 6px 10px;
      flex-shrink: 0;
    }}
    .log-toolbar-left {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
      min-width: 220px;
    }}
    .log-toolbar-right {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .log-search-box {{
      position: relative;
      display: flex;
      align-items: center;
      flex: 1;
      max-width: 260px;
    }}
    .log-search-box svg {{
      position: absolute;
      left: 8px;
      width: 13px;
      height: 13px;
      color: var(--text-subtle);
      pointer-events: none;
    }}
    .log-search-input {{
      width: 100%;
      background: #06090f;
      border: 1px solid var(--border-subtle);
      border-radius: 5px;
      padding: 4px 8px 4px 28px;
      font-size: 11px;
      color: #f8fafc;
      font-family: var(--font-sans);
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
    }}
    .log-search-input:focus {{
      outline: none;
      border-color: var(--accent-blue);
      box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2);
    }}
    /* Filter Pills */
    .log-filter-pills {{
      display: inline-flex;
      align-items: center;
      background: #080d1a;
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 2px;
      gap: 2px;
    }}
    .log-filter-pill {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 500;
      padding: 3px 8px;
      border-radius: 4px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      transition: all 0.15s ease;
      user-select: none;
    }}
    .log-filter-pill:hover {{
      color: var(--text-main);
      background: rgba(255, 255, 255, 0.05);
    }}
    .log-filter-pill.active {{
      background: #1e293b;
      color: #f8fafc;
      font-weight: 600;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }}
    .pill-badge {{
      font-size: 9.5px;
      background: rgba(255, 255, 255, 0.08);
      padding: 1px 5px;
      border-radius: 9999px;
      font-variant-numeric: tabular-nums;
      color: var(--text-muted);
    }}
    .log-filter-pill.active .pill-badge {{
      background: rgba(255, 255, 255, 0.15);
      color: #f8fafc;
    }}
    .log-filter-pill.pill-err.has-errs .pill-badge {{
      background: rgba(244, 63, 94, 0.25);
      color: #fb7185;
      font-weight: 700;
    }}
    .log-filter-pill.pill-warn.has-warns .pill-badge {{
      background: rgba(251, 191, 36, 0.25);
      color: #fcd34d;
      font-weight: 700;
    }}
    .log-search-clear {{
      position: absolute;
      right: 6px;
      background: transparent;
      border: none;
      color: var(--text-subtle);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 2px;
      border-radius: 3px;
      transition: color 0.15s ease;
    }}
    .log-search-clear:hover {{
      color: var(--text-main);
    }}
    .log-tool-btn {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      background: #0f172a;
      border: 1px solid var(--border-subtle);
      border-radius: 5px;
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 500;
      padding: 4px 8px;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
      user-select: none;
    }}
    .log-tool-btn:hover {{
      background: var(--bg-hover);
      color: var(--text-main);
      border-color: #475569;
    }}
    .log-tool-btn.active {{
      background: rgba(59, 130, 246, 0.18);
      color: #93c5fd;
      border-color: var(--accent-blue);
    }}
    .log-tool-btn svg {{
      width: 13px;
      height: 13px;
      flex-shrink: 0;
    }}
    .log-matches-count {{
      font-size: 11px;
      font-weight: 600;
      color: #f59e0b;
      white-space: nowrap;
      padding: 1px 6px;
      border-radius: 4px;
      background: rgba(245, 158, 11, 0.12);
    }}
    /* Terminal Shell Prompt & Window */
    .terminal-window {{
      background: #030712;
      border: 1px solid #1e293b;
      border-radius: 8px;
      flex: 1;
      min-height: 280px;
      overflow-y: auto;
      overflow-x: auto;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      box-shadow: inset 0 2px 6px rgba(0, 0, 0, 0.6);
      position: relative;
    }}
    .terminal-window.density-comfortable {{
      font-size: 12px;
      line-height: 1.65;
    }}
    .terminal-window.density-compact {{
      font-size: 11px;
      line-height: 1.45;
    }}
    .term-prompt-banner {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: #060913;
      border-bottom: 1px solid #1e293b;
      padding: 8px 12px;
      gap: 12px;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, monospace;
      font-size: 11px;
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    .term-prompt-left {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex: 1;
    }}
    .term-prompt-symbol {{
      color: #10b981;
      font-weight: 800;
      font-size: 13px;
      user-select: none;
    }}
    .term-prompt-cmd {{
      color: #93c5fd;
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      user-select: all;
    }}
    .term-prompt-copy-btn {{
      background: transparent;
      border: none;
      color: #64748b;
      cursor: pointer;
      padding: 2px 4px;
      display: inline-flex;
      align-items: center;
      border-radius: 3px;
      transition: color 0.15s, background 0.15s;
    }}
    .term-prompt-copy-btn:hover {{
      color: #f8fafc;
      background: rgba(255, 255, 255, 0.08);
    }}
    .term-prompt-meta {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }}
    .term-meta-pill {{
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 4px;
      background: #0f172a;
      border: 1px solid #334155;
      color: #94a3b8;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
    }}
    .terminal-inner {{
      min-width: 100%;
      width: max-content;
      padding: 6px 0;
    }}
    .log-row {{
      display: flex;
      align-items: flex-start;
      min-height: 20px;
      width: 100%;
      min-width: 100%;
      transition: background 0.1s ease;
      border-left: 3px solid transparent;
    }}
    .log-row:hover {{
      background: rgba(255, 255, 255, 0.04);
    }}
    .log-row.is-stderr {{
      background: rgba(244, 63, 94, 0.05);
      border-left-color: #f43f5e;
    }}
    .log-row.is-traceback {{
      background: rgba(244, 63, 94, 0.09);
      border-left-color: #fb7185;
    }}
    .log-row.is-pretty-json {{
      background: rgba(15, 23, 42, 0.4);
    }}
    .log-gutter,
    .log-gutter-col {{
      width: 76px;
      min-width: 76px;
      padding: 0 6px;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 6px;
      background: #050814;
      user-select: none;
      -webkit-user-select: none;
      border-right: 1px solid #1e293b;
      flex-shrink: 0;
      line-height: inherit;
    }}
    .terminal-window.density-compact .log-gutter,
    .terminal-window.density-compact .log-gutter-col {{
      width: 68px;
      min-width: 68px;
      padding: 0 4px;
      gap: 4px;
    }}
    .log-line-num {{
      color: #475569;
      font-size: 11px;
      font-variant-numeric: tabular-nums;
      text-align: right;
    }}
    .stream-pill {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 9px;
      font-weight: 700;
      letter-spacing: 0.03em;
      border-radius: 3px;
      padding: 0 3px;
      height: 14px;
      line-height: 1;
      text-transform: uppercase;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", monospace;
    }}
    .stream-pill-out {{
      color: #94a3b8;
      background: rgba(148, 163, 184, 0.1);
      border: 1px solid rgba(148, 163, 184, 0.2);
    }}
    .stream-pill-err {{
      color: #fb7185;
      background: rgba(244, 63, 94, 0.18);
      border: 1px solid rgba(244, 63, 94, 0.35);
    }}
    .stream-pill-sys {{
      color: #c084fc;
      background: rgba(192, 132, 252, 0.16);
      border: 1px solid rgba(192, 132, 252, 0.35);
    }}
    .log-text {{
      flex: 1;
      padding: 0 12px;
      white-space: pre;
      word-break: normal;
      overflow-x: visible;
      line-height: inherit;
    }}
    .terminal-window.wrap-mode .log-text {{
      white-space: pre-wrap;
      word-break: break-word;
    }}
    /* Token Highlighting */
    .log-lvl {{
      display: inline-block;
      font-size: 10.5px;
      font-weight: 700;
      padding: 0 5px;
      border-radius: 3px;
      line-height: 1.35;
      margin-right: 5px;
      letter-spacing: 0.02em;
    }}
    .log-lvl-info {{
      color: #38bdf8;
      background: rgba(56, 189, 248, 0.12);
      border: 1px solid rgba(56, 189, 248, 0.25);
    }}
    .log-lvl-warn {{
      color: #fbbf24;
      background: rgba(251, 191, 36, 0.12);
      border: 1px solid rgba(251, 191, 36, 0.25);
    }}
    .log-lvl-error {{
      color: #f87171;
      background: rgba(239, 68, 68, 0.16);
      border: 1px solid rgba(239, 68, 68, 0.3);
    }}
    .log-lvl-success {{
      color: #34d399;
      background: rgba(52, 211, 153, 0.12);
      border: 1px solid rgba(52, 211, 153, 0.25);
    }}
    .log-lvl-debug {{
      color: #c084fc;
      background: rgba(192, 132, 252, 0.12);
      border: 1px solid rgba(192, 132, 252, 0.25);
    }}
    .log-ts {{
      color: #64748b;
      font-size: 10.5px;
      margin-right: 6px;
      user-select: none;
    }}
    .log-path {{
      color: #93c5fd;
      text-decoration: underline dotted;
      text-underline-offset: 3px;
    }}
    .log-cmd {{
      color: #fcd34d;
      font-weight: 600;
    }}
    .log-method {{
      color: #2dd4bf;
      font-weight: 700;
    }}
    .log-json-key {{
      color: #38bdf8;
      font-weight: 600;
    }}
    .log-json-str {{
      color: #fde047;
    }}
    .log-json-num {{
      color: #c084fc;
      font-weight: 600;
    }}
    .log-json-bool {{
      color: #34d399;
      font-weight: 700;
    }}
    .log-json-null {{
      color: #94a3b8;
      font-style: italic;
    }}
    .log-search-match {{
      background: #f59e0b !important;
      color: #090d16 !important;
      font-weight: 700 !important;
      border-radius: 2px;
      padding: 0 2px;
      box-shadow: 0 0 6px rgba(245, 158, 11, 0.7);
    }}
    /* Traceback & JSON Cards */
    .traceback-card {{
      margin: 4px 8px;
      background: rgba(225, 29, 72, 0.06);
      border: 1px solid rgba(225, 29, 72, 0.28);
      border-radius: 6px;
      overflow: hidden;
    }}
    .traceback-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: rgba(225, 29, 72, 0.14);
      padding: 5px 10px;
      font-size: 11px;
      font-weight: 700;
      color: #fda4af;
      border-bottom: 1px solid rgba(225, 29, 72, 0.2);
    }}
    .traceback-header-left {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .traceback-body {{
      padding: 6px 10px;
      font-size: 11.5px;
      line-height: 1.55;
    }}
    .traceback-copy-btn {{
      background: rgba(225, 29, 72, 0.2);
      border: 1px solid rgba(225, 29, 72, 0.35);
      color: #fecdd3;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 10px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: background 0.15s;
    }}
    .traceback-copy-btn:hover {{
      background: rgba(225, 29, 72, 0.35);
    }}
    .json-card {{
      margin: 4px 8px;
      background: rgba(15, 23, 42, 0.7);
      border: 1px solid #334155;
      border-radius: 6px;
      overflow: hidden;
    }}
    .json-card-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: #0b1120;
      padding: 4px 10px;
      border-bottom: 1px solid #1e293b;
      font-size: 10.5px;
      color: #94a3b8;
    }}
    .json-card-body {{
      padding: 8px 12px;
      white-space: pre;
      overflow-x: auto;
      font-size: 11px;
      line-height: 1.5;
    }}
    .json-copy-btn {{
      background: #1e293b;
      border: 1px solid #334155;
      color: #cbd5e1;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 10px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: background 0.15s;
    }}
    .json-copy-btn:hover {{
      background: #334155;
      color: #f8fafc;
    }}
    .log-traceback-title {{
      color: #f87171;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}
    .log-traceback-line {{
      color: #38bdf8;
      font-weight: 600;
    }}
    .log-traceback-func {{
      color: #c084fc;
      font-weight: 600;
    }}
    .log-traceback-exc {{
      color: #f87171;
      font-weight: 700;
      margin-right: 4px;
    }}
    .log-traceback-msg {{
      color: #fca5a5;
    }}
    .badge-duration {{
      background: rgba(59, 130, 246, 0.12);
      color: #93c5fd;
      border: 1px solid rgba(59, 130, 246, 0.25);
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}
    /* ANSI Colors */
    .ansi-black, .ansi-30, .ansi-90 {{ color: #64748b; }}
    .ansi-red, .ansi-31, .ansi-91 {{ color: #f87171; }}
    .ansi-green, .ansi-32, .ansi-92 {{ color: #34d399; }}
    .ansi-yellow, .ansi-33, .ansi-93 {{ color: #fbbf24; }}
    .ansi-blue, .ansi-34, .ansi-94 {{ color: #60a5fa; }}
    .ansi-magenta, .ansi-35, .ansi-95 {{ color: #c084fc; }}
    .ansi-cyan, .ansi-36, .ansi-96 {{ color: #38bdf8; }}
    .ansi-white, .ansi-37, .ansi-97 {{ color: #f8fafc; }}
    .ansi-bold {{ font-weight: 700; }}
    .ansi-dim {{ opacity: 0.6; }}
    .ansi-italic {{ font-style: italic; }}
    .ansi-underline {{ text-decoration: underline; }}
    /* Modal */
    .modal-overlay {{
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.6);
      backdrop-filter: blur(2px);
      z-index: 70;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }}
    .modal-overlay.open {{
      opacity: 1;
      pointer-events: auto;
    }}
    .modal {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      width: 540px;
      max-width: 100%;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.6);
    }}
    .modal-header {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .modal-body {{
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .form-group {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .form-label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
    }}
    .form-input, .form-select, .form-textarea {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 8px 12px;
      color: var(--text-main);
      font-size: 13px;
      outline: none;
    }}
    .form-textarea {{
      font-family: var(--font-mono);
      font-size: 12px;
      resize: vertical;
      min-height: 90px;
    }}
    .form-input:focus, .form-select:focus, .form-textarea:focus {{
      border-color: var(--accent-blue);
    }}
    .modal-footer {{
      padding: 14px 20px;
      border-top: 1px solid var(--border-color);
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      background: rgba(15, 23, 42, 0.5);
    }}
    /* Toast */
    .toast-container {{
      position: fixed;
      bottom: 24px;
      right: 24px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      z-index: 100;
      pointer-events: none;
    }}
    .toast {{
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 10px 16px;
      font-size: 13px;
      font-weight: 500;
      color: var(--text-main);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
      display: flex;
      align-items: center;
      gap: 8px;
      animation: slideIn 0.2s ease forwards;
    }}
    @keyframes slideIn {{
      from {{ transform: translateY(20px); opacity: 0; }}
      to {{ transform: translateY(0); opacity: 1; }}
    }}
    /* Responsive */
    @media (max-width: 860px) {{
      .sidebar {{
        position: fixed;
        top: 0;
        bottom: 0;
        left: 0;
        transform: translateX(-100%);
      }}
      .sidebar.mobile-open {{
        transform: translateX(0);
      }}
      .topbar-menu-btn {{
        display: inline-flex !important;
      }}
    }}
    /* Agent Activities & Sentinels Views */
    .view-panel {{
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}
    .view-panel[style*="display: none"] {{
      display: none !important;
    }}
    .agent-telemetry-banner {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      padding: 16px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 16px;
    }}
    .agent-telemetry-item {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .agent-telemetry-label {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-subtle);
      font-weight: 600;
    }}
    .agent-telemetry-val {{
      font-size: 14px;
      font-weight: 700;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    /* Sentinel Card */
    .sentinel-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      border-radius: 12px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      transition: border-color 0.15s ease, box-shadow 0.15s ease;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    }}
    .sentinel-card:hover {{
      border-color: var(--border-subtle);
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
    }}
    .sentinel-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.05);
      padding-bottom: 12px;
    }}
    .sentinel-meta-group {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .sentinel-actions-group {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .btn-sm {{
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 500;
      border-radius: 6px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      border: 1px solid var(--border-color);
      background: var(--bg-card);
      color: var(--text-main);
      transition: background 0.15s ease, border-color 0.15s ease;
    }}
    .btn-sm:hover {{
      background: var(--bg-hover);
      border-color: var(--border-subtle);
    }}
    .btn-sm.active {{
      background: rgba(59, 130, 246, 0.2);
      border-color: var(--accent-blue);
      color: #93c5fd;
    }}
    /* Collapsible Box */
    .collapsible-box {{
      border: 1px solid var(--border-color);
      border-radius: 8px;
      background: var(--bg-card);
      overflow: hidden;
      margin-top: 6px;
    }}
    .collapsible-header {{
      background: rgba(15, 23, 42, 0.7);
      padding: 10px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: pointer;
      user-select: none;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
    }}
    .collapsible-header:hover {{
      color: var(--text-main);
      background: rgba(30, 41, 59, 0.8);
    }}
    .collapsible-body {{
      padding: 14px;
      font-family: var(--font-mono);
      font-size: 12px;
      line-height: 1.6;
      max-height: 380px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-word;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      background: #070a12;
      color: #cbd5e1;
    }}
    /* Stepper Timeline */
    .stepper-timeline {{
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 8px;
      position: relative;
      padding-left: 24px;
    }}
    .stepper-timeline::before {{
      content: "";
      position: absolute;
      top: 10px;
      bottom: 10px;
      left: 10px;
      width: 2px;
      background: rgba(255, 255, 255, 0.1);
    }}
    .stepper-step {{
      position: relative;
      background: rgba(15, 23, 42, 0.5);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .stepper-dot {{
      position: absolute;
      left: -20px;
      top: 12px;
      width: 14px;
      height: 14px;
      border-radius: 50%;
      background: var(--bg-surface);
      border: 2px solid var(--accent-blue);
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 0 6px rgba(59, 130, 246, 0.4);
    }}
    .stepper-title-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 12px;
    }}
    .tool-pill {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      font-family: var(--font-mono);
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 4px;
      background: rgba(59, 130, 246, 0.15);
      color: #93c5fd;
      border: 1px solid rgba(59, 130, 246, 0.25);
    }}
    .stepper-output {{
      font-family: var(--font-mono);
      font-size: 11px;
      line-height: 1.5;
      background: #070a12;
      border: 1px solid rgba(255, 255, 255, 0.05);
      border-radius: 6px;
      padding: 8px 10px;
      color: #94a3b8;
      max-height: 160px;
      overflow-y: auto;
      white-space: pre-wrap;
    }}
    /* Verdict Badges */
    .verdict-badge {{
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 9999px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .verdict-create {{
      background: rgba(168, 85, 247, 0.15);
      color: #c084fc;
      border: 1px solid rgba(168, 85, 247, 0.3);
    }}
    .verdict-correct {{
      background: rgba(16, 185, 129, 0.15);
      color: #6ee7b7;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }}
    .verdict-no_change {{
      background: rgba(148, 163, 184, 0.15);
      color: #cbd5e1;
      border: 1px solid rgba(148, 163, 184, 0.3);
    }}
    .verdict-supplement {{
      background: rgba(245, 158, 11, 0.15);
      color: #fcd34d;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }}
    .notion-btn {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      font-weight: 500;
      color: #93c5fd;
      text-decoration: none;
      padding: 3px 8px;
      border-radius: 6px;
      background: rgba(59, 130, 246, 0.1);
      border: 1px solid rgba(59, 130, 246, 0.25);
      transition: background 0.15s ease;
    }}
    .notion-btn:hover {{
      background: rgba(59, 130, 246, 0.2);
      color: #ffffff;
    }}
    /* Agent Activity Card inside Log Drawer */
    .agent-activity-card {{
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(59, 130, 246, 0.3);
      border-radius: 8px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25);
    }}
    .agent-activity-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      padding-bottom: 8px;
    }}
    .agent-activity-body {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      font-size: 12px;
    }}
  </style>
</head>
<body>
  <div class="app-container">
    <!-- Sidebar -->
    <aside class="sidebar" id="appSidebar">
      <div class="sidebar-header">
        <div class="brand-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path>
          </svg>
          <span>Webhook Hub</span>
        </div>
        <div class="live-badge" title="Live Server-Sent Events stream connected">
          <span class="live-pulse" id="ssePulse"></span>
          <span id="sseStatusText">LIVE</span>
        </div>
      </div>

      <div class="sidebar-content">
        <!-- Telemetry -->
        <div class="telemetry-card">
          <div class="telemetry-row">
            <span class="telemetry-label">Memory RSS</span>
            <span class="telemetry-value" id="telemetryMemory">-- MB / 30 MB</span>
          </div>
          <div class="progress-bar-container">
            <div class="progress-bar-fill" id="telemetryMemoryBar" style="width: 0%;"></div>
          </div>
          <div class="telemetry-row">
            <span class="telemetry-label">Uptime</span>
            <span class="telemetry-value" id="telemetryUptime">--</span>
          </div>
          <div class="telemetry-row">
            <span class="telemetry-label">DB Engine</span>
            <span class="telemetry-value" style="color: var(--status-success);">SQLite WAL</span>
          </div>
          <div class="telemetry-row">
            <span class="telemetry-label">Watchdog Probe</span>
            <span class="telemetry-value" id="telemetryWatchdogProbe" style="color: var(--status-success);">Online</span>
          </div>
        </div>

        <!-- Antigravity Agent & Sentinel Hub -->
        <div>
          <div class="sidebar-section-title">Agent &amp; Sentinel Hub</div>
          <ul class="nav-list">
            <li class="nav-item" id="navItemQuota" data-nav="quota" onclick="switchMainView('quota', this)" title="Google Antigravity 5-Hour &amp; Weekly Quota Sentinel, live countdowns &amp; autonomous warmup">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                <span>5h Quota &amp; Warmup</span>
              </div>
              <span class="nav-count" id="countQuotaStatus" style="background: rgba(16, 185, 129, 0.15); color: var(--status-success);">Live</span>
            </li>
            <li class="nav-item" id="navItemSentinels" data-nav="sentinels" onclick="switchMainView('sentinels', this)" title="View autonomous Sentinel runs &amp; reports">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2Zm0 18a8 8 0 1 1 8-8 8 8 0 0 1-8 8Z"/><path d="m9 12 2 2 4-4"/></svg>
                <span>Sentinel AI Runs</span>
              </div>
              <span class="nav-count" id="countSentinels">0</span>
            </li>
            <li class="nav-item" id="navItemSignals" data-nav="signals" onclick="switchMainView('signals', this)" title="View Notion CRM contact review signals">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>
                <span>CRM Agent Signals</span>
              </div>
              <span class="nav-count" id="countSignals">0</span>
            </li>
            <li class="nav-item" id="navItemPulses" data-nav="pulses" onclick="switchMainView('pulses', this)" title="View Antigravity sidebar pulse queue">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
                <span>Sidebar Pulse Queue</span>
              </div>
              <span class="nav-count" id="countPulses">0</span>
            </li>
            <li class="nav-item" id="navItemWatchdog" data-nav="watchdog" onclick="switchMainView('watchdog', this)" title="Antigravity 24/7 Watchdog, network probes &amp; auto-resuscitation">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/></svg>
                <span>Watchdog &amp; Health</span>
              </div>
              <span class="nav-count" id="countWatchdogStalled" style="background: rgba(59, 130, 246, 0.15); color: var(--accent-blue);">0</span>
            </li>
          </ul>
        </div>

        <!-- Navigation Sections -->
        <div>
          <div class="sidebar-section-title">Observable Activities</div>
          <ul class="nav-list">
            <li class="nav-item active" id="navItemAll" data-nav="tasks" data-filter="all" onclick="setCategoryFilter('all', this)">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>
                <span>All Activities</span>
              </div>
              <span class="nav-count" id="countAll">0</span>
            </li>
            <li class="nav-item" data-filter="agent_signal" onclick="setCategoryFilter('agent_signal', this)">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>
                <span>Agent Signals</span>
              </div>
              <span class="nav-count" id="countAgent">0</span>
            </li>
            <li class="nav-item" data-filter="contact-review" onclick="setCategoryFilter('contact-review', this)">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
                <span>Contact Review</span>
              </div>
              <span class="nav-count" id="countContact">0</span>
            </li>
            <li class="nav-item" data-filter="uptime-kuma" onclick="setCategoryFilter('uptime-kuma', this)">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/></svg>
                <span>Uptime Kuma</span>
              </div>
              <span class="nav-count" id="countKuma">0</span>
            </li>
            <li class="nav-item" data-filter="cli" onclick="setCategoryFilter('cli', this)">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>
                <span>CLI Executions</span>
              </div>
              <span class="nav-count" id="countCli">0</span>
            </li>
            <li class="nav-item" data-filter="failed" onclick="setCategoryFilter('failed', this)">
              <div class="nav-item-left">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" x2="9" y1="9" y2="15"/><line x1="9" x2="15" y1="9" y2="15"/></svg>
                <span>Failed / Blocked</span>
              </div>
              <span class="nav-count" id="countFailed" style="background: rgba(239, 68, 68, 0.2); color: var(--status-danger);">0</span>
            </li>
          </ul>
        </div>

        <!-- Quick Actions -->
        <div>
          <div class="sidebar-section-title">Control Actions</div>
          <div style="display: flex; flex-direction: column; gap: 8px;">
            <button class="btn btn-primary" style="justify-content: center; width: 100%;" onclick="openTestModal()">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
              <span>Simulate Webhook</span>
            </button>
            <button class="btn btn-secondary" style="justify-content: center; width: 100%;" onclick="triggerSweep()">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
              <span>Sweep Unprocessed</span>
            </button>
            <button class="btn btn-secondary" style="justify-content: center; width: 100%;" onclick="pullUpAllSessions(this)" title="Scan and pull up all stalled sessions immediately">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
              <span>1-Click Pull-Up</span>
            </button>
          </div>
        </div>
      </div>

      <div class="sidebar-footer">
        <div>Public Ingress:</div>
        <a class="endpoint-link" href="https://{tunnel_host}" target="_blank">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/></svg>
          <span>https://{tunnel_host}</span>
        </a>
        <div style="margin-top: 4px;">Local Port: <code>{server_port}</code></div>
      </div>
    </aside>

    <!-- Main Viewport -->
    <main class="main-area">
      <!-- Topbar -->
      <header class="topbar">
        <div class="topbar-left">
          <button class="topbar-menu-btn" onclick="toggleMobileSidebar()">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
          </button>
          <div class="view-title" id="viewTitle">All Activities</div>
        </div>
        <div class="topbar-right">
          <button id="toggleFilterBtn" class="btn btn-secondary btn-filter-active" onclick="toggleTestFilter()" title="Toggle: Auto-hide synthetic test events vs show all events">
            <svg id="filterEyeIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>
            <span id="toggleFilterText">Real Only (Tests Hidden)</span>
          </button>
          <div class="search-input-wrapper">
            <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
            <input type="text" id="taskSearchInput" class="search-input" placeholder="Search tasks, events, actions..." oninput="handleSearchInput(this.value)">
          </div>
          <button class="btn btn-secondary" onclick="refreshTasksAuthoritative()" title="Re-fetch authoritative state from API">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
            <span>Refresh</span>
          </button>
        </div>
      </header>

      <!-- Content Viewport -->
      <div class="content-viewport">
        <!-- View 1: Tasks / Activity Feed -->
        <div id="viewContainerTasks" class="view-panel active">
          <!-- Watchdog Warning Banner (Visible only when stalled sessions detected) -->
          <div id="watchdogAlertBanner" style="display: none; background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; align-items: center; justify-content: space-between; gap: 12px;">
            <div style="display: flex; align-items: center; gap: 10px;">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--status-danger); flex-shrink: 0;"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
              <div>
                <span style="font-weight: 600; color: #fca5a5;" id="watchdogAlertTitle">Antigravity Watchdog Alert</span>
                <span style="font-size: 13px; color: #cbd5e1; margin-left: 6px;" id="watchdogAlertText">Stalled or dropped sessions detected.</span>
              </div>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
              <button class="btn btn-primary" style="padding: 4px 12px; font-size: 12px;" onclick="pullUpAllSessions(this)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
                <span>1-Click Pull-Up</span>
              </button>
              <button class="btn btn-secondary" style="padding: 4px 10px; font-size: 12px;" onclick="switchMainView('watchdog')">
                <span>View Watchdog &rarr;</span>
              </button>
            </div>
          </div>

          <!-- Metric Cards -->
          <div class="metric-grid">
            <div class="metric-box">
              <span class="metric-box-title">Real Tasks (Production)</span>
              <span class="metric-box-value" style="color: var(--status-success);" id="statRealTasks">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Total Tasks</span>
              <span class="metric-box-value" id="statTotalTasks">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Webhook Events</span>
              <span class="metric-box-value" id="statTotalEvents">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Succeeded Tasks</span>
              <span class="metric-box-value" style="color: var(--status-success);" id="statSucceeded">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Active / Running</span>
              <span class="metric-box-value" style="color: var(--status-running);" id="statRunning">0</span>
            </div>
          </div>

          <!-- Task Table Card -->
          <div class="table-card">
            <div class="table-header">
              <div>
                <div class="table-title">Activity Feed &amp; Execution Registry</div>
                <div style="font-size: 12px; color: var(--text-muted);" id="tableSubtitle">Showing latest tasks</div>
              </div>
              <div class="filter-pill-group" role="group" aria-label="Event Filter Mode">
                <button id="pillReal" class="filter-pill active" onclick="setEventFilterMode('real')" title="Show only real production events">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
                  <span>Real Only</span>
                  <span class="pill-badge" id="pillRealCount">0</span>
                </button>
                <button id="pillAll" class="filter-pill" onclick="setEventFilterMode('all')" title="Show all activities (real and tests)">
                  <span>All Events</span>
                  <span class="pill-badge" id="pillAllCount">0</span>
                </button>
                <button id="pillTest" class="filter-pill" onclick="setEventFilterMode('test')" title="Show only synthetic test events">
                  <span>Tests Only</span>
                  <span class="pill-badge" id="pillTestCount">0</span>
                </button>
              </div>
            </div>
            <div class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Task ID</th>
                    <th>Source</th>
                    <th>Action Type</th>
                    <th>Target / Command</th>
                    <th>Created</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody id="tasksTableBody">
                  <tr>
                    <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">
                      Loading authoritative activities from SQLite SSOT...
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- View 2: Sentinel AI Runs View -->
        <div id="viewContainerSentinels" class="view-panel" style="display: none;">
          <div class="agent-telemetry-banner">
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Cadence Identity</span>
              <span class="agent-telemetry-val">
                <span class="badge" style="background: rgba(99, 102, 241, 0.15); color: #a5b4fc; border: 1px solid rgba(99, 102, 241, 0.25);" id="sentinelCadenceCard">CAD-20260911-webhook-hub-sentinel</span>
              </span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Inspection Schedule</span>
              <span class="agent-telemetry-val" style="color: #93c5fd;">Every 4 Hours (6x / day)</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Success Marker</span>
              <span class="agent-telemetry-val" id="sentinelSuccessMarker">
                <span class="badge badge-succeeded">Active</span>
              </span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Memory Budget</span>
              <span class="agent-telemetry-val" style="color: var(--status-success);" id="sentinelMemoryBudget">&lt; 30.0 MB RSS</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Runs Tracked</span>
              <span class="agent-telemetry-val" id="sentinelTotalRuns">0</span>
            </div>
          </div>

          <div id="sentinelsListContainer" style="display: flex; flex-direction: column; gap: 16px;">
            <div style="padding: 40px; text-align: center; color: var(--text-muted);">
              Loading Sentinel AI Agent runs...
            </div>
          </div>
        </div>

        <!-- View 3: CRM Agent Signals & Dispatches View -->
        <div id="viewContainerSignals" class="view-panel" style="display: none;">
          <div class="agent-telemetry-banner">
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Signal Source</span>
              <span class="agent-telemetry-val">Notion People CRM</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Total Signals Emitted</span>
              <span class="agent-telemetry-val" id="signalsTotalCount">0</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">CREATE Verdicts</span>
              <span class="agent-telemetry-val" style="color: #c084fc;" id="signalsCountCreate">0</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">CORRECT Verdicts</span>
              <span class="agent-telemetry-val" style="color: #6ee7b7;" id="signalsCountCorrect">0</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">NO_CHANGE Verdicts</span>
              <span class="agent-telemetry-val" style="color: #cbd5e1;" id="signalsCountNoChange">0</span>
            </div>
          </div>

          <div class="table-card">
            <div class="table-header">
              <div>
                <div class="table-title">Notion CRM Contact Review Signals</div>
                <div style="font-size: 12px; color: var(--text-muted);">Authoritative audit log from .agents/signals/contact_review/</div>
              </div>
              <div class="filter-pill-group" role="group" aria-label="Signal Filter Mode">
                <button class="filter-pill active" id="pillSigAll" onclick="filterSignals('all')">
                  <span>All</span>
                </button>
                <button class="filter-pill" id="pillSigCreate" onclick="filterSignals('create')">
                  <span class="verdict-badge verdict-create" style="padding: 1px 6px; font-size: 10px;">CREATE</span>
                </button>
                <button class="filter-pill" id="pillSigCorrect" onclick="filterSignals('correct')">
                  <span class="verdict-badge verdict-correct" style="padding: 1px 6px; font-size: 10px;">CORRECT</span>
                </button>
                <button class="filter-pill" id="pillSigNoChange" onclick="filterSignals('no_change')">
                  <span class="verdict-badge verdict-no_change" style="padding: 1px 6px; font-size: 10px;">NO_CHANGE</span>
                </button>
              </div>
            </div>
            <div class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Signal ID</th>
                    <th>Target Contact</th>
                    <th>Verdict</th>
                    <th>Confidence</th>
                    <th>Notion Page</th>
                    <th>Applied</th>
                    <th>Emitted Time</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody id="signalsTableBody">
                  <tr>
                    <td colspan="8" style="text-align: center; color: var(--text-muted); padding: 32px;">
                      Loading CRM Agent signals...
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- View 4: Sidebar Pulse Queue View -->
        <div id="viewContainerPulses" class="view-panel" style="display: none;">
          <div class="agent-telemetry-banner">
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Sidebar Sidecar Slug</span>
              <span class="agent-telemetry-val">webhook-hub-sentinel</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Events Directory</span>
              <span class="agent-telemetry-val" style="font-family: var(--font-mono); font-size: 12px; color: #94a3b8;">$HOME/.antigravity/sidecar_data/webhook-hub-sentinel/events/</span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Total Pulses Queued</span>
              <span class="agent-telemetry-val" style="color: var(--accent-blue);" id="pulsesTotalCount">0</span>
            </div>
          </div>

          <div class="table-card">
            <div class="table-header">
              <div>
                <div class="table-title">Antigravity Sidebar Pulse Queue</div>
                <div style="font-size: 12px; color: var(--text-muted);">Events dispatched for the Antigravity conversation sidebar</div>
              </div>
              <button class="btn btn-secondary" onclick="loadPulses()">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
                <span>Refresh Pulses</span>
              </button>
            </div>
            <div class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Timestamp (UTC)</th>
                    <th>Task ID</th>
                    <th>Source</th>
                    <th>Action</th>
                    <th>Status</th>
                    <th>Prompt Preview</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody id="pulsesTableBody">
                  <tr>
                    <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">
                      Loading sidebar pulse queue...
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- View 5: Antigravity Watchdog & Resuscitation Center -->
        <div id="viewContainerWatchdog" class="view-panel" style="display: none;">
          <!-- Telemetry Banner -->
          <div class="agent-telemetry-banner">
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Watchdog Engine</span>
              <span class="agent-telemetry-val" id="watchdogEngineStatus">
                <span class="pulse-indicator" style="background-color: var(--status-success);"></span>
                <span>Active 24/7</span>
              </span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Network Probe</span>
              <span class="agent-telemetry-val" id="watchdogNetworkStatus">
                <span class="pulse-indicator" style="background-color: var(--status-success);"></span>
                <span id="watchdogProbeTarget">Online (1.1.1.1:53)</span>
              </span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Language Server</span>
              <span class="agent-telemetry-val" id="watchdogLsStatus">
                <span class="pulse-indicator" style="background-color: var(--status-success);"></span>
                <span id="watchdogLsAddress">Connected</span>
              </span>
            </div>
            <div class="agent-telemetry-item">
              <span class="agent-telemetry-label">Auto-Pull-Up Policy</span>
              <span class="agent-telemetry-val" id="watchdogAutoPolicy" style="color: var(--accent-blue);">
                Enabled (30s Tick)
              </span>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
              <button class="btn btn-primary" onclick="pullUpAllSessions(this)" id="btnWatchdogPullUpTop">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
                <span>1-Click Pull-Up All</span>
              </button>
              <button class="btn btn-secondary" onclick="loadWatchdogStatus()" title="Refresh Watchdog Status">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
                <span>Refresh</span>
              </button>
            </div>
          </div>

          <!-- Watchdog Metric Cards -->
          <div class="metric-grid">
            <div class="metric-box">
              <span class="metric-box-title">Stalled / Interrupted</span>
              <span class="metric-box-value" style="color: var(--status-success);" id="statWatchdogStalled">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Total Resuscitation Attempts</span>
              <span class="metric-box-value" id="statWatchdogTotalAttempts">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Successfully Revived</span>
              <span class="metric-box-value" style="color: var(--status-success);" id="statWatchdogRevived">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Failed / Exhausted</span>
              <span class="metric-box-value" style="color: var(--text-muted);" id="statWatchdogFailed">0</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Watchdog Scan Interval</span>
              <span class="metric-box-value" style="color: var(--accent-blue);" id="statWatchdogInterval">30s</span>
            </div>
          </div>

          <!-- Section 1: Active Stalled / Hung Sessions -->
          <div class="table-card">
            <div class="table-header">
              <div>
                <div class="table-title">Detected Stalled Sessions &amp; Suspended Turns</div>
                <div style="font-size: 12px; color: var(--text-muted);" id="stalledSubtitle">Sessions scanned in ~/.gemini/antigravity/brain/ needing resuscitation</div>
              </div>
              <span class="badge badge-queued" id="badgeStalledCount">0 Detected</span>
            </div>
            <div class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Conversation ID</th>
                    <th>Type</th>
                    <th>Sidecar Slug</th>
                    <th>Last Error / Reason</th>
                    <th>Prior Attempts</th>
                    <th>Recovery Eligibility</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody id="stalledTableBody">
                  <tr>
                    <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">
                      Scanning for stalled Antigravity sessions...
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Section 2: Resuscitation History (SQLite SSOT) -->
          <div class="table-card">
            <div class="table-header">
              <div>
                <div class="table-title">Resuscitation Audit Trail &amp; Self-Healing History</div>
                <div style="font-size: 12px; color: var(--text-muted);">Authoritative recovery event log from SQLite antigravity_resuscitations table</div>
              </div>
              <div class="filter-pill-group" role="group" aria-label="Resuscitation Filter">
                <button id="pillResuscitationAll" class="filter-pill active" onclick="setResuscitationFilter('all')">
                  <span>All Attempts</span>
                  <span class="pill-badge" id="pillResuscitationAllCount">0</span>
                </button>
                <button id="pillResuscitationSuccess" class="filter-pill" onclick="setResuscitationFilter('resuscitated')">
                  <span>Revived</span>
                  <span class="pill-badge" id="pillResuscitationSuccessCount">0</span>
                </button>
                <button id="pillResuscitationFailed" class="filter-pill" onclick="setResuscitationFilter('failed')">
                  <span>Failed / Exhausted</span>
                  <span class="pill-badge" id="pillResuscitationFailedCount">0</span>
                </button>
              </div>
            </div>
            <div class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Timestamp (UTC)</th>
                    <th>Resuscitation ID</th>
                    <th>Conversation ID</th>
                    <th>Sidecar</th>
                    <th>Outcome</th>
                    <th>Attempt</th>
                    <th>Last Error / Trigger</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody id="resuscitationsTableBody">
                  <tr>
                    <td colspan="8" style="text-align: center; color: var(--text-muted); padding: 32px;">
                      Loading resuscitation history...
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <!-- View 6: Antigravity 5h Quota Sentinel & Warmup Console -->
        <div id="viewContainerQuota" class="view-panel" style="display: none;">
          <!-- Telemetry & Actions Header -->
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; flex-wrap: wrap; gap: 12px;">
            <div>
              <div style="font-size: 18px; font-weight: 700; color: #ffffff; display: flex; align-items: center; gap: 8px;">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--status-success);"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                <span>Antigravity Quota Sentinel &amp; 5-Hour Autonomous Warmup</span>
              </div>
              <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">
                Autonomous sentinel eliminating the 5-hour countdown freeze via minimal token pings, prioritizing user's primary Gemini models.
              </div>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <button class="btn btn-secondary" onclick="refreshQuotaLive(this)" title="Synchronize quotas live directly from Google Cloud Code PA API">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
                <span>Sync Live Quotas</span>
              </button>
              <button class="btn btn-primary" onclick="triggerAllReadyWarmups(this)" title="Trigger token ping warmup for all pools at 100% with expired timer">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                <span>Warmup All Idle Pools</span>
              </button>
            </div>
          </div>

          <!-- Top Quota Metric Summary Row -->
          <div class="metric-row" style="margin-bottom: 24px;">
            <div class="metric-box">
              <span class="metric-box-title">Active IDE Account</span>
              <span class="metric-box-value" style="font-size: 15px; color: #60a5fa;" id="statQuotaActiveEmail">Loading...</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Gemini 5h Limit (Primary)</span>
              <span class="metric-box-value" style="color: var(--status-success);" id="statQuotaGemini5h">--%</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Gemini Reset Countdown</span>
              <span class="metric-box-value" style="color: #38bdf8;" id="statQuotaGeminiCountdown">--:--</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Claude &amp; GPT 5h Limit</span>
              <span class="metric-box-value" style="color: #c084fc;" id="statQuota3p5h">--%</span>
            </div>
            <div class="metric-box">
              <span class="metric-box-title">Successful Warmups</span>
              <span class="metric-box-value" style="color: var(--status-success);" id="statQuotaWarmupCount">0</span>
            </div>
          </div>

          <!-- Featured Active Account Card (Gemini Highlighted) -->
          <div id="quotaActiveAccountContainer">
            <!-- Dynamically populated -->
          </div>

          <!-- Fleet Multi-Account Matrix Table -->
          <div class="table-card" style="margin-bottom: 24px;">
            <div class="table-header">
              <div>
                <div class="table-title">Fleet Quota Matrix (All 8 Accounts)</div>
                <div style="font-size: 12px; color: var(--text-muted);">Authoritative multi-account quota status synced into SQLite SSOT</div>
              </div>
              <span class="badge" id="badgeQuotaAccountsCount">8 Accounts Tracked</span>
            </div>
            <div class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Account &amp; Status</th>
                    <th>Subscription Tier</th>
                    <th>Gemini 5h (Primary)</th>
                    <th>Gemini Countdown</th>
                    <th>Claude/GPT 5h</th>
                    <th>Claude Countdown</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody id="quotaFleetTableBody">
                  <tr>
                    <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">
                      Loading fleet quota status...
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- Warmup Audit Log History Table -->
          <div class="table-card">
            <div class="table-header">
              <div>
                <div class="table-title">Autonomous 5h Warmup Audit Log</div>
                <div style="font-size: 12px; color: var(--text-muted);">Authoritative audit trail from SQLite antigravity_warmup_logs table</div>
              </div>
              <span class="badge badge-success" id="badgeWarmupLogsCount">0 Records</span>
            </div>
            <div class="table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th>Timestamp</th>
                    <th>Account Email</th>
                    <th>Bucket ID</th>
                    <th>Model Used</th>
                    <th>Trigger Reason</th>
                    <th>Status</th>
                    <th>Latency</th>
                  </tr>
                </thead>
                <tbody id="quotaWarmupLogsTableBody">
                  <tr>
                    <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">
                      Loading warmup history...
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>

  <!-- Slide-out Log Drawer -->
  <div class="drawer-overlay" id="drawerOverlay" onclick="closeDrawer()"></div>
  <aside class="drawer" id="taskDrawer">
    <div class="drawer-header">
      <div class="drawer-title">
        <span id="drawerTaskId" style="cursor: pointer;" onclick="copyTaskIdToClipboard()" title="Click to copy Task ID">tsk_...</span>
        <span class="badge" id="drawerTaskStatus">queued</span>
        <span class="badge" id="drawerExitBadge" style="display: none;">--</span>
        <span class="badge" id="drawerDurationBadge" style="display: none;">--</span>
      </div>
      <button class="drawer-close" onclick="closeDrawer()" title="Close drawer (Esc)">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="drawer-body">
      <div>
        <div class="sidebar-section-title" style="margin-bottom: 6px;">Task Telemetry &amp; Parameters</div>
        <div class="drawer-meta-grid" id="drawerMetaGrid">
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Source</span>
            <span class="drawer-meta-val" id="drawerMetaSource">--</span>
          </div>
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Action</span>
            <span class="drawer-meta-val" id="drawerMetaAction">--</span>
          </div>
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Created At</span>
            <span class="drawer-meta-val" id="drawerMetaCreated">--</span>
          </div>
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Duration</span>
            <span class="drawer-meta-val" id="drawerMetaDuration">--</span>
          </div>
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Exit Code</span>
            <span class="drawer-meta-val" id="drawerMetaExit">--</span>
          </div>
          <div class="drawer-meta-item" style="grid-column: 1 / -1;">
            <span class="drawer-meta-label">Executed Command</span>
            <span class="drawer-meta-val" id="drawerMetaCommand" style="color: #93c5fd; font-size: 11px;">--</span>
          </div>
        </div>
      </div>

      <!-- Agent Activity & Signal Card -->
      <div id="drawerAgentActivityCard" class="agent-activity-card" style="display: none; margin-top: 14px; margin-bottom: 6px;">
        <div class="agent-activity-header">
          <div style="display: flex; align-items: center; gap: 8px;">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--accent-blue);"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>
            <span style="font-weight: 600; font-size: 12px; color: #ffffff;">Associated Agent Activity &amp; Dispatches</span>
          </div>
          <div style="display: flex; align-items: center; gap: 6px;">
            <div id="drawerAgentPills" style="display: flex; align-items: center; gap: 6px;"></div>
            <div id="drawerAgentActions" style="display: flex; align-items: center; gap: 4px; margin-left: 4px;"></div>
          </div>
        </div>
        <div class="agent-activity-body" id="drawerAgentActivityBody"></div>
      </div>

      <div class="log-section">
        <div style="display: flex; align-items: center; justify-content: space-between;">
          <div class="sidebar-section-title" style="margin-bottom: 0;">Live Output &amp; Execution Logs</div>
          <div style="display: flex; align-items: center; gap: 8px;">
            <span id="drawerLineCount" style="font-size: 11px; color: var(--text-subtle); font-weight: 500;">0 lines</span>
            <div id="drawerLiveIndicator" class="live-badge" style="display: none; padding: 1px 6px; font-size: 10px;">
              <span class="live-pulse"></span>
              <span>LIVE</span>
            </div>
          </div>
        </div>

        <div class="log-toolbar">
          <div class="log-toolbar-left">
            <div class="log-search-box">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
              <input type="text" class="log-search-input" id="logSearchInput" placeholder="Filter logs... (Cmd/Ctrl+F)" oninput="onLogSearchChange(this.value)">
              <button class="log-search-clear" id="btnLogSearchClear" onclick="clearLogSearch()" style="display: none;" title="Clear search">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <span class="log-matches-count" id="logMatchesCount" style="display: none;">0 matches</span>
            
            <div class="log-filter-pills" id="logFilterPills">
              <input type="hidden" id="logLevelFilter" value="all">
              <button type="button" class="log-filter-pill active" id="filterPillAll" onclick="setLogLevelFilter('all')" title="Show all log streams">
                <span>All</span>
                <span class="pill-badge" id="badgeCountAll">0</span>
              </button>
              <button type="button" class="log-filter-pill pill-err" id="filterPillError" onclick="setLogLevelFilter('error')" title="Filter errors and stderr">
                <span>Errors</span>
                <span class="pill-badge" id="badgeCountError">0</span>
              </button>
              <button type="button" class="log-filter-pill pill-warn" id="filterPillWarn" onclick="setLogLevelFilter('warn')" title="Filter warnings and errors">
                <span>Warnings</span>
                <span class="pill-badge" id="badgeCountWarn">0</span>
              </button>
              <button type="button" class="log-filter-pill" id="filterPillStdout" onclick="setLogLevelFilter('stdout')" title="Filter standard output">
                <span>OUT</span>
                <span class="pill-badge" id="badgeCountStdout">0</span>
              </button>
              <button type="button" class="log-filter-pill" id="filterPillStderr" onclick="setLogLevelFilter('stderr')" title="Filter standard error">
                <span>ERR</span>
                <span class="pill-badge" id="badgeCountStderr">0</span>
              </button>
            </div>
          </div>
          <div class="log-toolbar-right">
            <button class="log-tool-btn" id="btnToggleDensity" onclick="toggleLogDensity()" title="Toggle density (Comfortable / Compact)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="21" y1="10" x2="3" y2="10"/><line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="14" x2="3" y2="14"/><line x1="21" y1="18" x2="3" y2="18"/></svg>
              <span id="densityBtnLabel">Compact</span>
            </button>
            <button class="log-tool-btn active" id="btnToggleWrap" onclick="toggleLogWrap()" title="Toggle soft line wrap">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="15" y2="12"/><polyline points="18 9 21 12 18 15"/><path d="M21 12h-7a4 4 0 0 0-4 4v2"/><line x1="3" y1="18" x2="7" y2="18"/></svg>
              <span>Wrap</span>
            </button>
            <button class="log-tool-btn active" id="btnToggleAutoScroll" onclick="toggleLogAutoScroll()" title="Auto-scroll to bottom on incoming logs">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="7 13 12 18 17 13"/><polyline points="7 6 12 11 17 6"/></svg>
              <span>Auto-Scroll</span>
            </button>
            <button class="log-tool-btn active" id="btnTogglePrettyJson" onclick="togglePrettyJson()" title="Toggle pretty indentation for JSON lines">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
              <span>Pretty JSON</span>
            </button>
            <button class="log-tool-btn" id="btnDownloadLogs" onclick="downloadTaskLogs()" title="Download raw log file">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              <span>Download</span>
            </button>
            <button class="log-tool-btn" id="btnCopyLogs" onclick="copyDrawerLogs()" title="Copy logs to clipboard">
              <svg id="copyLogsIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              <span id="copyLogsLabel">Copy</span>
            </button>
            <button class="btn btn-secondary" style="font-size: 11px; padding: 4px 9px;" onclick="rerunCurrentDrawerTask()" title="Re-execute this task">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              <span>Re-run</span>
            </button>
          </div>
        </div>

        <div class="terminal-window wrap-mode density-comfortable" id="drawerTerminal" onscroll="handleTerminalScroll()">
          <div class="term-prompt-banner" id="drawerTermPromptBanner">
            <div class="term-prompt-left">
              <span class="term-prompt-symbol">❯</span>
              <span class="term-prompt-cmd" id="termPromptCmd">--</span>
              <button class="term-prompt-copy-btn" onclick="copyPromptCommand()" title="Copy executed command">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              </button>
            </div>
            <div class="term-prompt-meta">
              <span class="term-meta-pill" id="termMetaTime">--:--:--</span>
              <span class="term-meta-pill" id="termMetaDuration">--</span>
              <span class="term-meta-pill" id="termMetaExit">--</span>
            </div>
          </div>
          <div class="terminal-inner" id="drawerTerminalInner">
            <div style="padding: 24px; text-align: center; color: var(--text-subtle);">Connecting to live stream...</div>
          </div>
        </div>
      </div>
    </div>
  </aside>

  <!-- Ingress Webhook Simulator Modal -->
  <div class="modal-overlay" id="testModalOverlay">
    <div class="modal">
      <div class="modal-header">
        <div style="font-weight: 700; font-size: 15px;">Simulate Ingress Webhook</div>
        <button class="drawer-close" onclick="closeTestModal()">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">Source Preset</label>
          <select class="form-select" id="modalSourceSelect" onchange="handlePresetChange(this.value)">
            <option value="agent_signal">Antigravity Agent Signal (agent_signal)</option>
            <option value="contact-review">Contact Review & Notion CRM (contact_review)</option>
            <option value="uptime-kuma">Uptime Kuma Estate Alert (uptime-kuma)</option>
            <option value="cli">Custom CLI Command (cli)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Action Type</label>
          <input type="text" class="form-input" id="modalActionType" value="agent_signal">
        </div>
        <div class="form-group">
          <label class="form-label">JSON Payload</label>
          <textarea class="form-textarea" id="modalPayload" rows="5"></textarea>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeTestModal()">Cancel</button>
        <button class="btn btn-primary" onclick="submitTestWebhook()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          <span>Send Ingress Request</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Signal Detail Modal -->
  <div class="modal-overlay" id="signalModalOverlay" onclick="if (event.target === this) closeSignalModal()">
    <div class="modal" style="max-width: 680px;">
      <div class="modal-header">
        <div style="display: flex; align-items: center; gap: 8px;">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--accent-blue);"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>
          <span style="font-weight: 700; font-size: 15px;" id="signalModalTitle">CRM Signal Details</span>
        </div>
        <button class="drawer-close" onclick="closeSignalModal()">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body" id="signalModalBody" style="display: flex; flex-direction: column; gap: 14px; max-height: 70vh; overflow-y: auto;">
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeSignalModal()">Close</button>
        <button class="btn btn-primary" id="btnSignalModalCopy" onclick="copyActiveSignalJson()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          <span>Copy Signal JSON</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Resuscitation Detail Modal -->
  <div class="modal-overlay" id="resuscitationModalOverlay" onclick="if (event.target === this) closeResuscitationModal()">
    <div class="modal" style="max-width: 680px;">
      <div class="modal-header">
        <div style="display: flex; align-items: center; gap: 8px;">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--accent-blue);"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/></svg>
          <span style="font-weight: 700; font-size: 15px;" id="resuscitationModalTitle">Resuscitation Record Details</span>
        </div>
        <button class="drawer-close" onclick="closeResuscitationModal()">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="modal-body" id="resuscitationModalBody" style="display: flex; flex-direction: column; gap: 14px; max-height: 70vh; overflow-y: auto;">
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeResuscitationModal()">Close</button>
        <button class="btn btn-primary" id="btnResuscitationModalCopy" onclick="copyResuscitationPrompt()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          <span>Copy Prompt</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Toast notifications -->
  <div class="toast-container" id="toastContainer"></div>

  <!-- Type-safe Client Logic adhering to SSOT & Unidirectional Data Flow -->
  <script>
    function getStoredFilterMode() {{
      try {{
        return localStorage.getItem('antigravity_hub_filter_mode') || 'real';
      }} catch (_) {{
        return 'real';
      }}
    }}

    function setStoredFilterMode(mode) {{
      try {{
        localStorage.setItem('antigravity_hub_filter_mode', mode);
      }} catch (_) {{}}
    }}

    // State management: SSOT - local state is strictly derived from API responses
    const state = {{
      tasks: [],
      categoryFilter: 'all',
      eventFilterMode: getStoredFilterMode(),
      searchQuery: '',
      activeTaskId: null,
      drawerEventSource: null,
      globalEventSource: null,
      drawerLogs: [],
      drawerSearchQuery: '',
      drawerLevelFilter: 'all',
      drawerWrapLines: true,
      drawerAutoScroll: true,
      currentMainView: 'tasks',
      sentinels: [],
      signals: [],
      signalsFilter: 'all',
      pulses: [],
      watchdogStatus: null,
      resuscitationFilter: 'all',
      activeResuscitationRecord: null,
      quotaOverview: null,
      quotaClockInterval: null,
      openSections: new Set(),
      drawerDurationTimer: null,
      drawerRenderScheduled: false,
    }};

    const PRESETS = {{
      agent_signal: {{
        action_type: "agent_signal",
        payload: JSON.stringify({{
          action: "agent_signal",
          prompt: "Verify health of all services and summarize in Antigravity sidebar",
          priority: 1,
          payload: {{ source: "manual_simulator", scope: "healthz" }}
        }}, null, 2)
      }},
      "contact-review": {{
        action_type: "contact_review",
        payload: JSON.stringify({{
          name: "Dr. Evelyn Reed",
          company: "Nova Biomaterials",
          phone: "+84 90 123 4567",
          source: "contact-review",
          dry_run: true
        }}, null, 2)
      }},
      "uptime-kuma": {{
        action_type: "cli",
        payload: JSON.stringify({{
          msg: "Testing Uptime Kuma Ingress",
          monitor: {{ name: "Webhook Gateway", url: "https://webhook.worldinspirelab.com/healthz" }},
          heartbeat: {{ status: 1, msg: "OK", ping: 12 }}
        }}, null, 2)
      }},
      cli: {{
        action_type: "cli",
        payload: JSON.stringify({{
          action: "cli",
          command: "echo 'Antigravity Webhook Execution Verified'",
          timeout_seconds: 30
        }}, null, 2)
      }}
    }};

    function showToast(message, type = 'info') {{
      const container = document.getElementById('toastContainer');
      const toast = document.createElement('div');
      toast.className = 'toast';
      toast.innerText = message;
      container.appendChild(toast);
      setTimeout(() => {{
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s ease';
        setTimeout(() => toast.remove(), 300);
      }}, 3500);
    }}

    // Unidirectional Data Flow: Re-pull authoritative projection from API
    async function refreshTasksAuthoritative() {{
      try {{
        let url = '/tasks?limit=100';
        if (state.categoryFilter === 'agent_signal') {{
          url += '&action_type=agent_signal';
        }} else if (state.categoryFilter === 'contact-review') {{
          url += '&source=contact-review';
        }} else if (state.categoryFilter === 'uptime-kuma') {{
          url += '&source=uptime_kuma';
        }} else if (state.categoryFilter === 'cli') {{
          url += '&action_type=cli';
        }} else if (state.categoryFilter === 'failed') {{
          url += '&status=failed';
        }}

        if (state.eventFilterMode) {{
          url += '&filter_test=' + encodeURIComponent(state.eventFilterMode);
        }}

        if (state.searchQuery.trim()) {{
          url += '&q=' + encodeURIComponent(state.searchQuery.trim());
        }}

        const [tasksResp, summaryResp, healthResp, agentSummaryResp] = await Promise.all([
          fetch(url),
          fetch('/tasks/summary'),
          fetch('/healthz'),
          fetch('/api/agent-activities/summary').catch(() => null)
        ]);

        if (tasksResp.ok) {{
          const data = await tasksResp.json();
          state.tasks = data.tasks || [];
          renderTasksTable();
        }}

        if (summaryResp.ok) {{
          const summary = await summaryResp.json();
          renderSummary(summary);
        }}

        if (healthResp.ok) {{
          const health = await healthResp.json();
          renderTelemetry(health);
        }}

        if (agentSummaryResp && agentSummaryResp.ok) {{
          const agentSummary = await agentSummaryResp.json();
          renderAgentSummary(agentSummary);
        }}

        // Push-based & authoritative synchronization of currently active tab view
        if (state.currentMainView === 'sentinels') {{
          loadSentinels();
        }} else if (state.currentMainView === 'signals') {{
          loadSignals();
        }} else if (state.currentMainView === 'pulses') {{
          loadPulses();
        }}
      }} catch (err) {{
        console.error('Failed to pull authoritative state:', err);
      }}
    }}

    function renderAgentSummary(summary) {{
      if (!summary) return;
      const sentinels = summary.sentinels || {{}};
      const signals = summary.signals || {{}};
      const pulses = summary.pulses || {{}};

      const elSentinels = document.getElementById('countSentinels');
      if (elSentinels) elSentinels.innerText = sentinels.total_runs || 0;

      const elSignals = document.getElementById('countSignals');
      if (elSignals) elSignals.innerText = signals.total_signals || 0;

      const elPulses = document.getElementById('countPulses');
      if (elPulses) elPulses.innerText = pulses.total_pulses || 0;

      const elSigTotal = document.getElementById('signalsTotalCount');
      if (elSigTotal) elSigTotal.innerText = signals.total_signals || 0;

      const byVerdict = signals.by_verdict || {{}};
      const elCreate = document.getElementById('signalsCountCreate');
      if (elCreate) elCreate.innerText = byVerdict.create || 0;
      const elCorrect = document.getElementById('signalsCountCorrect');
      if (elCorrect) elCorrect.innerText = byVerdict.correct || 0;
      const elNoChange = document.getElementById('signalsCountNoChange');
      if (elNoChange) elNoChange.innerText = byVerdict.no_change || 0;

      const elPulseTotal = document.getElementById('pulsesTotalCount');
      if (elPulseTotal) elPulseTotal.innerText = pulses.total_pulses || 0;

      const elSentTotal = document.getElementById('sentinelTotalRuns');
      if (elSentTotal) elSentTotal.innerText = sentinels.total_runs || 0;
    }}

    function renderSummary(summary) {{
      if (!summary) return;
      state.latestSummary = summary;
      const totalTasks = summary.total_tasks || 0;
      const realTasks = summary.real_tasks !== undefined ? summary.real_tasks : 0;
      const testTasks = summary.test_tasks !== undefined ? summary.test_tasks : Math.max(0, totalTasks - realTasks);

      let activeByStatus = summary.by_status || {{}};
      if (state.eventFilterMode === 'real' && summary.real_by_status) {{
        activeByStatus = summary.real_by_status;
      }} else if (state.eventFilterMode === 'test' && summary.test_by_status) {{
        activeByStatus = summary.test_by_status;
      }}

      document.getElementById('statTotalTasks').innerText = state.eventFilterMode === 'real' ? realTasks : (state.eventFilterMode === 'test' ? testTasks : totalTasks);
      const elRealTasks = document.getElementById('statRealTasks');
      if (elRealTasks) elRealTasks.innerText = realTasks;
      document.getElementById('statTotalEvents').innerText = summary.total_events || 0;
      document.getElementById('statSucceeded').innerText = activeByStatus.succeeded || 0;
      document.getElementById('statRunning').innerText = (activeByStatus.running || 0) + (activeByStatus.queued || 0);

      document.getElementById('countAll').innerText = state.eventFilterMode === 'real' ? realTasks : (state.eventFilterMode === 'test' ? testTasks : totalTasks);
      document.getElementById('countAgent').innerText = (summary.by_action || {{}}).agent_signal || 0;
      document.getElementById('countContact').innerText = (summary.by_source || {{}})['contact-review'] || (summary.by_source || {{}})['contact_review'] || 0;
      document.getElementById('countKuma').innerText = (summary.by_source || {{}})['uptime_kuma'] || (summary.by_source || {{}})['uptime-kuma'] || 0;
      document.getElementById('countCli').innerText = (summary.by_action || {{}}).cli || 0;

      const failedCount = (activeByStatus.failed || 0) + (activeByStatus.timed_out || 0);
      const failedEl = document.getElementById('countFailed');
      if (failedEl) {{
        failedEl.innerText = failedCount;
        if (failedCount === 0) {{
          failedEl.style.background = 'rgba(148, 163, 184, 0.15)';
          failedEl.style.color = '#94a3b8';
        }} else {{
          failedEl.style.background = 'rgba(239, 68, 68, 0.2)';
          failedEl.style.color = 'var(--status-danger)';
        }}
      }}

      const pillReal = document.getElementById('pillRealCount');
      const pillAll = document.getElementById('pillAllCount');
      const pillTest = document.getElementById('pillTestCount');
      if (pillReal) pillReal.innerText = realTasks;
      if (pillAll) pillAll.innerText = totalTasks;
      if (pillTest) pillTest.innerText = testTasks;
    }}

    function renderTelemetry(health) {{
      const memMb = health.memory_rss_mb || 0;
      const budgetMb = (health.system && health.system.memory_budget_mb) || 64;
      document.getElementById('telemetryMemory').innerText = memMb.toFixed(1) + ' MB / ' + budgetMb + ' MB';
      const pct = Math.min(100, Math.round((memMb / budgetMb) * 100));
      const bar = document.getElementById('telemetryMemoryBar');
      bar.style.width = pct + '%';
      bar.style.background = pct > 85 ? 'var(--status-danger)' : (pct > 70 ? 'var(--status-warning)' : 'var(--accent-blue)');

      const uptimeSec = Math.floor(health.uptime_seconds || 0);
      const hours = Math.floor(uptimeSec / 3600);
      const mins = Math.floor((uptimeSec % 3600) / 60);
      const secs = uptimeSec % 60;
      document.getElementById('telemetryUptime').innerText = hours + 'h ' + mins + 'm ' + secs + 's';
    }}

    function renderTasksTable() {{
      const tbody = document.getElementById('tasksTableBody');
      const subtitle = document.getElementById('tableSubtitle');
      if (subtitle) {{
        if (state.eventFilterMode === 'real') {{
          subtitle.innerText = `Showing ${{state.tasks.length}} real production activities (test events hidden)`;
        }} else if (state.eventFilterMode === 'test') {{
          subtitle.innerText = `Showing ${{state.tasks.length}} synthetic / verification test activities`;
        }} else {{
          subtitle.innerText = `Showing all ${{state.tasks.length}} activities (real & synthetic tests)`;
        }}
      }}

      if (!state.tasks || state.tasks.length === 0) {{
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 40px;">No observable activities matching current filter.</td></tr>`;
        return;
      }}

      tbody.innerHTML = state.tasks.map(t => {{
        const isTest = t.is_test || false;
        const typeBadge = isTest
          ? `<span class="badge badge-test" title="Automated / Synthetic verification test">TEST</span>`
          : `<span class="badge badge-real" title="Real production webhook event">REAL</span>`;
        const statusBadge = `<span class="badge badge-${{t.status}}">${{t.status}}</span>`;
        const sourceTag = `<span class="source-tag">${{escapeHtml(t.source || 'default')}}</span>`;
        const actionType = escapeHtml(t.action_type || 'cli');
        const target = escapeHtml(t.target_action || t.command || (t.action_params_json ? t.action_params_json.substring(0, 40) + '...' : '-'));
        const created = escapeHtml(t.created_at || '').replace('T', ' ').substring(0, 19);

        return `
          <tr onclick="openDrawer('${{t.task_id}}')">
            <td>${{statusBadge}} ${{typeBadge}}</td>
            <td><span class="task-id-code">${{escapeHtml(t.task_id)}}</span></td>
            <td>${{sourceTag}}</td>
            <td><strong>${{actionType}}</strong></td>
            <td style="max-width: 260px; overflow: hidden; text-overflow: ellipsis;" title="${{target}}">${{target}}</td>
            <td style="color: var(--text-muted); font-size: 12px;">${{created}}</td>
            <td onclick="event.stopPropagation()">
              <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="openDrawer('${{t.task_id}}')">Logs</button>
              <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px; margin-left: 4px;" onclick="rerunTask('${{t.task_id}}')">Re-run</button>
            </td>
          </tr>
        `;
      }}).join('');
    }}

    function setEventFilterMode(mode) {{
      state.eventFilterMode = mode;
      setStoredFilterMode(mode);
      syncFilterUI();
      if (state.latestSummary) {{
        renderSummary(state.latestSummary);
      }}
      if (mode === 'real') {{
        showToast('Filter: Showing real production activities only', 'info');
      }} else if (mode === 'test') {{
        showToast('Filter: Showing synthetic test events only', 'info');
      }} else {{
        showToast('Filter: Showing all activities', 'info');
      }}
      refreshTasksAuthoritative();
    }}

    function toggleTestFilter() {{
      if (state.eventFilterMode === 'real') {{
        setEventFilterMode('all');
      }} else {{
        setEventFilterMode('real');
      }}
    }}

    function syncFilterUI() {{
      const mode = state.eventFilterMode;
      document.querySelectorAll('.filter-pill').forEach(btn => btn.classList.remove('active'));
      const activePill = document.getElementById(
        mode === 'real' ? 'pillReal' : (mode === 'test' ? 'pillTest' : 'pillAll')
      );
      if (activePill) activePill.classList.add('active');

      const toggleBtn = document.getElementById('toggleFilterBtn');
      const toggleText = document.getElementById('toggleFilterText');
      const eyeIcon = document.getElementById('filterEyeIcon');

      if (mode === 'real') {{
        if (toggleBtn) toggleBtn.classList.add('btn-filter-active');
        if (toggleText) toggleText.innerText = 'Real Only (Tests Hidden)';
        if (eyeIcon) eyeIcon.innerHTML = '<path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/>';
      }} else if (mode === 'test') {{
        if (toggleBtn) toggleBtn.classList.remove('btn-filter-active');
        if (toggleText) toggleText.innerText = 'Tests Only';
        if (eyeIcon) eyeIcon.innerHTML = '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>';
      }} else {{
        if (toggleBtn) toggleBtn.classList.remove('btn-filter-active');
        if (toggleText) toggleText.innerText = 'All Events (Show All)';
        if (eyeIcon) eyeIcon.innerHTML = '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>';
      }}
    }}

    function escapeHtml(str) {{
      if (!str) return '';
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }}

    function formatMarkdown(md) {{
      if (!md) return '';
      let s = escapeHtml(md);
      // Code blocks ```code```
      s = s.replace(/```([a-zA-Z0-9_-]*)\\n([\\s\\S]*?)```/g, (m, lang, code) => {{
        return `<pre style="background:#070a12;border:1px solid rgba(255,255,255,0.08);border-radius:6px;padding:10px;overflow-x:auto;color:#cbd5e1;font-family:var(--font-mono);font-size:11px;margin:8px 0;"><code>${{code}}</code></pre>`;
      }});
      // Inline code `code`
      s = s.replace(/`([^`]+)`/g, '<code style="background:rgba(255,255,255,0.1);padding:1px 5px;border-radius:3px;font-family:var(--font-mono);font-size:11px;color:#93c5fd;">$1</code>');
      // Headers #, ##, ###
      s = s.replace(/^### (.*$)/gim, '<h4 style="color:#ffffff;font-size:13px;font-weight:700;margin:12px 0 6px 0;">$1</h4>');
      s = s.replace(/^## (.*$)/gim, '<h3 style="color:#ffffff;font-size:14px;font-weight:700;margin:14px 0 6px 0;border-bottom:1px solid rgba(255,255,255,0.08);padding-bottom:4px;">$1</h3>');
      s = s.replace(/^# (.*$)/gim, '<h2 style="color:#ffffff;font-size:15px;font-weight:800;margin:16px 0 8px 0;">$1</h2>');
      // Bold **text**
      s = s.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong style="color:#ffffff;">$1</strong>');
      // Bullet lists
      s = s.replace(/^\\s*[-*]\\s+(.*$)/gim, '<li style="margin-left:18px;list-style-type:disc;color:#cbd5e1;margin-bottom:2px;">$1</li>');
      // Blockquotes > text
      s = s.replace(/^\\s*&gt;\\s+(.*$)/gim, '<blockquote style="border-left:3px solid var(--accent-blue);padding-left:10px;color:#94a3b8;margin:6px 0;">$1</blockquote>');
      // Paragraphs
      s = s.replace(/\\n\\n/g, '<div style="height:8px;"></div>');
      return s;
    }}

    function copyText(str) {{
      if (!str) return;
      navigator.clipboard.writeText(str).then(() => {{
        showToast('Copied to clipboard', 'info');
      }}).catch(() => {{
        showToast('Copy failed', 'error');
      }});
    }}

    function escapeJsString(str) {{
      if (!str) return '';
      return String(str).replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'").replace(/\\n/g, '\\\\n').replace(/\\r/g, '');
    }}

    function switchMainView(viewName, el) {{
      state.currentMainView = viewName;

      // Clear active from all sidebar nav items
      document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
      if (el) {{
        el.classList.add('active');
      }} else {{
        const targetNav = document.getElementById(
          viewName === 'quota' ? 'navItemQuota' :
          viewName === 'sentinels' ? 'navItemSentinels' :
          viewName === 'signals' ? 'navItemSignals' :
          viewName === 'pulses' ? 'navItemPulses' :
          viewName === 'watchdog' ? 'navItemWatchdog' : 'navItemAll'
        );
        if (targetNav) targetNav.classList.add('active');
      }}

      // Toggle view panels
      const panels = {{
        tasks: document.getElementById('viewContainerTasks'),
        quota: document.getElementById('viewContainerQuota'),
        sentinels: document.getElementById('viewContainerSentinels'),
        signals: document.getElementById('viewContainerSignals'),
        pulses: document.getElementById('viewContainerPulses'),
        watchdog: document.getElementById('viewContainerWatchdog')
      }};

      Object.keys(panels).forEach(key => {{
        if (panels[key]) {{
          if (key === viewName) {{
            panels[key].style.display = 'block';
            panels[key].classList.add('active');
          }} else {{
            panels[key].style.display = 'none';
            panels[key].classList.remove('active');
          }}
        }}
      }});

      // Update topbar title & controls
      const titleEl = document.getElementById('viewTitle');
      const toggleFilterBtn = document.getElementById('toggleFilterBtn');
      const searchInput = document.getElementById('taskSearchInput');

      // Auto-dismiss mobile sidebar if open
      const sb = document.getElementById('appSidebar');
      if (sb) sb.classList.remove('mobile-open');

      if (viewName === 'quota') {{
        if (titleEl) titleEl.innerText = 'Antigravity Quota Sentinel & 5-Hour Warmup';
        if (toggleFilterBtn) toggleFilterBtn.style.display = 'none';
        if (searchInput) searchInput.placeholder = 'Search quota accounts, models...';
        loadQuota();
      }} else if (viewName === 'sentinels') {{
        if (titleEl) titleEl.innerText = 'Sentinel AI Agent Runs';
        if (toggleFilterBtn) toggleFilterBtn.style.display = 'none';
        if (searchInput) searchInput.placeholder = 'Search sentinel runs, prompts...';
        loadSentinels();
      }} else if (viewName === 'signals') {{
        if (titleEl) titleEl.innerText = 'CRM Agent Signals & Dispatches';
        if (toggleFilterBtn) toggleFilterBtn.style.display = 'none';
        if (searchInput) searchInput.placeholder = 'Search signals, contacts, explanations...';
        loadSignals();
      }} else if (viewName === 'pulses') {{
        if (titleEl) titleEl.innerText = 'Antigravity Sidebar Pulse Queue';
        if (toggleFilterBtn) toggleFilterBtn.style.display = 'none';
        if (searchInput) searchInput.placeholder = 'Search pulse queue events...';
        loadPulses();
      }} else if (viewName === 'watchdog') {{
        if (titleEl) titleEl.innerText = 'Antigravity 24/7 Watchdog & Resuscitation Center';
        if (toggleFilterBtn) toggleFilterBtn.style.display = 'none';
        if (searchInput) searchInput.placeholder = 'Search stalled sessions, resuscitations...';
        loadWatchdogStatus();
      }} else {{
        if (titleEl) titleEl.innerText = 'All Activities';
        if (toggleFilterBtn) toggleFilterBtn.style.display = 'inline-flex';
        if (searchInput) searchInput.placeholder = 'Search tasks, events, actions...';
        refreshTasksAuthoritative();
      }}
    }}

    function setCategoryFilter(filter, el) {{
      const sb = document.getElementById('appSidebar');
      if (sb) sb.classList.remove('mobile-open');
      if (state.currentMainView !== 'tasks') {{
        switchMainView('tasks');
      }}
      document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
      el.classList.add('active');
      state.categoryFilter = filter;
      document.getElementById('viewTitle').innerText = el.querySelector('span').innerText;
      refreshTasksAuthoritative();
    }}

    let searchDebounceTimeout = null;
    function handleSearchInput(val) {{
      clearTimeout(searchDebounceTimeout);
      searchDebounceTimeout = setTimeout(() => {{
        state.searchQuery = val;
        if (state.currentMainView === 'quota') {{
          renderQuotaOverview();
        }} else if (state.currentMainView === 'signals') {{
          loadSignals();
        }} else if (state.currentMainView === 'sentinels') {{
          renderSentinels();
        }} else if (state.currentMainView === 'pulses') {{
          loadPulses();
        }} else if (state.currentMainView === 'watchdog') {{
          renderWatchdogStatus();
        }} else {{
          refreshTasksAuthoritative();
        }}
      }}, 250);
    }}

    // Sentinel AI Runs Logic
    state.sentinelDetails = {{}};

    async function loadSentinels() {{
      const container = document.getElementById('sentinelsListContainer');
      try {{
        const resp = await fetch('/api/agent-activities/sentinels?limit=20');
        if (!resp.ok) {{
          container.innerHTML = `<div style="padding: 40px; text-align: center; color: var(--status-danger);">Failed to load Sentinel runs: ${{resp.statusText}}</div>`;
          return;
        }}
        const data = await resp.json();
        state.sentinels = data.runs || [];

        const cardEl = document.getElementById('sentinelCadenceCard');
        if (cardEl && data.cadence_card) cardEl.innerText = data.cadence_card;

        const totalEl = document.getElementById('sentinelTotalRuns');
        if (totalEl) totalEl.innerText = data.total || state.sentinels.length;

        const markerEl = document.getElementById('sentinelSuccessMarker');
        if (markerEl) {{
          if (data.success_marker_exists) {{
            markerEl.innerHTML = `<span class="badge badge-succeeded" title="${{escapeHtml(data.success_marker_path || '')}}">Success Marker Verified</span>`;
          }} else {{
            markerEl.innerHTML = `<span class="badge badge-queued">No Marker</span>`;
          }}
        }}

        renderSentinels();
      }} catch (err) {{
        container.innerHTML = `<div style="padding: 40px; text-align: center; color: var(--status-danger);">Error fetching Sentinel runs: ${{err.message}}</div>`;
      }}
    }}

    function renderStepsHtml(steps) {{
      if (!steps || steps.length === 0) {{
        return `<div style="color: var(--text-muted); padding: 12px;">No tool execution steps recorded.</div>`;
      }}
      return `
        <div class="stepper-timeline">
          ${{steps.map((st, idx) => `
            <div class="stepper-step">
              <div class="stepper-dot"></div>
              <div class="stepper-title-row">
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);">#${{st.step_index !== undefined ? st.step_index : (idx + 1)}}</span>
                  <span class="tool-pill">${{escapeHtml(st.tool_name || 'tool')}}</span>
                </div>
                <span style="font-size: 11px; color: var(--text-muted);">${{escapeHtml(st.created_at || '').slice(11, 19)}}</span>
              </div>
              ${{st.tool_args ? `
                <div style="font-family: var(--font-mono); font-size: 11px; color: #93c5fd; background: rgba(59,130,246,0.08); padding: 4px 8px; border-radius: 4px;">
                  <span style="color: var(--text-muted);">Args: </span>
                  <code>${{escapeHtml(JSON.stringify(st.tool_args))}}</code>
                </div>
              ` : ''}}
              ${{st.output ? `
                <div class="stepper-output">${{escapeHtml(st.output)}}</div>
              ` : ''}}
            </div>
          `).join('')}}
        </div>
      `;
    }}

    function renderSentinels() {{
      const container = document.getElementById('sentinelsListContainer');
      if (!state.sentinels || state.sentinels.length === 0) {{
        container.innerHTML = `<div style="padding: 40px; text-align: center; color: var(--text-muted);">No Sentinel AI Agent runs found.</div>`;
        return;
      }}

      let filtered = state.sentinels;
      if (state.searchQuery.trim()) {{
        const q = state.searchQuery.trim().toLowerCase();
        filtered = state.sentinels.filter(s =>
          (s.conversation_id && s.conversation_id.toLowerCase().includes(q)) ||
          (s.cadence_card && s.cadence_card.toLowerCase().includes(q)) ||
          (s.prompt_snippet && s.prompt_snippet.toLowerCase().includes(q)) ||
          (s.report_snippet && s.report_snippet.toLowerCase().includes(q))
        );
      }}

      container.innerHTML = filtered.map(s => {{
        const cid = s.conversation_id;
        const statusBadge = `<span class="badge badge-${{s.status === 'succeeded' ? 'succeeded' : (s.status === 'running' ? 'running' : 'queued')}}">${{escapeHtml(s.status)}}</span>`;
        const toolsPills = (s.tools_used || []).map(t =>
          `<span class="tool-pill"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg><span>${{escapeHtml(t)}}</span></span>`
        ).join(' ');

        const isPromptOpen = state.openSections.has('prompt_' + cid);
        const isStepsOpen = state.openSections.has('steps_' + cid);
        const isReportOpen = state.openSections.has('report_' + cid);

        const cached = state.sentinelDetails[cid];
        const promptContent = isPromptOpen
          ? (cached && cached.prompt ? `<pre style="margin:0; white-space:pre-wrap; font-family:var(--font-mono); font-size:11px; color:#cbd5e1;">${{escapeHtml(cached.prompt)}}</pre>` : 'Loading full prompt...')
          : 'Loading full prompt...';

        const stepsContent = isStepsOpen
          ? (cached && cached.steps ? renderStepsHtml(cached.steps) : 'Loading autonomous tool execution steps...')
          : 'Loading autonomous tool execution steps...';

        const reportContent = isReportOpen
          ? (cached && cached.final_report ? formatMarkdown(cached.final_report) : 'Loading report...')
          : 'Loading report...';

        return `
          <div class="sentinel-card" id="sentinelCard_${{cid}}">
            <div class="sentinel-card-header">
              <div class="sentinel-card-title">
                ${{statusBadge}}
                <span class="task-id-code" style="font-size: 13px; cursor: pointer;" onclick="copyText('${{cid}}')" title="Click to copy Conversation ID">${{escapeHtml(cid)}}</span>
                <span class="badge" style="background: rgba(99, 102, 241, 0.15); color: #a5b4fc; border: 1px solid rgba(99, 102, 241, 0.25); font-size: 11px;">${{escapeHtml(s.cadence_card)}}</span>
              </div>
              <div style="display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--text-muted);">
                <span title="Execution steps count">
                  <strong style="color: #ffffff;">${{s.steps_count}}</strong> Steps
                </span>
                <span style="color: var(--border-subtle);">|</span>
                <span>${{escapeHtml(s.created_at || '').replace('T', ' ').substring(0, 19)}}</span>
              </div>
            </div>

            ${{toolsPills ? `<div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px;">${{toolsPills}}</div>` : ''}}

            <!-- Collapsible: Injected Prompt -->
            <div class="collapsible-section" style="margin-top: 10px;">
              <div class="collapsible-header" onclick="toggleSentinelSection('${{cid}}', 'prompt')">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--accent-blue);"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                  <span style="font-weight: 600;">Injected Prompt (agentapi new-conversation)</span>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span style="font-size: 11px; color: var(--text-muted);">${{escapeHtml(s.prompt_snippet).substring(0, 60)}}...</span>
                  <svg id="chevron_prompt_${{cid}}" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="transition: transform 0.2s ease; transform: rotate(${{isPromptOpen ? '180deg' : '0deg'}});"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
              </div>
              <div class="collapsible-body" id="body_prompt_${{cid}}" style="display: ${{isPromptOpen ? 'block' : 'none'}};">
                <div style="display: flex; justify-content: flex-end; margin-bottom: 6px;">
                  <button class="btn btn-secondary" style="font-size: 10px; padding: 2px 8px;" onclick="copySentinelField('${{cid}}', 'prompt')">Copy Prompt</button>
                </div>
                <div id="content_prompt_${{cid}}">${{promptContent}}</div>
              </div>
            </div>

            <!-- Collapsible: Tool Steps Timeline -->
            <div class="collapsible-section">
              <div class="collapsible-header" onclick="toggleSentinelSteps('${{cid}}')">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--accent-blue);"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                  <span style="font-weight: 600;">Autonomous Tool Execution Timeline (${{s.steps_count}} steps)</span>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span style="font-size: 11px; color: var(--text-muted);">Inspect steps &amp; outputs</span>
                  <svg id="chevron_steps_${{cid}}" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="transition: transform 0.2s ease; transform: rotate(${{isStepsOpen ? '180deg' : '0deg'}});"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
              </div>
              <div class="collapsible-body" id="body_steps_${{cid}}" style="display: ${{isStepsOpen ? 'block' : 'none'}}; max-height: 480px;">
                <div id="content_steps_${{cid}}">${{stepsContent}}</div>
              </div>
            </div>

            <!-- Collapsible: Delivered Markdown Report -->
            <div class="collapsible-section">
              <div class="collapsible-header" onclick="toggleSentinelSection('${{cid}}', 'report')">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--status-success);"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/></svg>
                  <span style="font-weight: 600;">Delivered Markdown Report</span>
                </div>
                <div style="display: flex; align-items: center; gap: 6px;">
                  <span style="font-size: 11px; color: var(--text-muted);">${{escapeHtml(s.report_snippet).substring(0, 50)}}...</span>
                  <svg id="chevron_report_${{cid}}" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="transition: transform 0.2s ease; transform: rotate(${{isReportOpen ? '180deg' : '0deg'}});"><polyline points="6 9 12 15 18 9"/></svg>
                </div>
              </div>
              <div class="collapsible-body" id="body_report_${{cid}}" style="display: ${{isReportOpen ? 'block' : 'none'}}; max-height: 480px;">
                <div style="display: flex; justify-content: flex-end; margin-bottom: 6px;">
                  <button class="btn btn-secondary" style="font-size: 10px; padding: 2px 8px;" onclick="copySentinelField('${{cid}}', 'final_report')">Copy Report</button>
                </div>
                <div id="content_report_${{cid}}" style="font-family: var(--font-sans); font-size: 12px; line-height: 1.6; color: #cbd5e1;">${{reportContent}}</div>
              </div>
            </div>
          </div>
        `;
      }}).join('');

      // Auto-fetch details for open sections that need hydration
      filtered.forEach(s => {{
        const cid = s.conversation_id;
        if (state.openSections.has('prompt_' + cid) || state.openSections.has('steps_' + cid) || state.openSections.has('report_' + cid)) {{
          if (!state.sentinelDetails[cid]) {{
            ensureSentinelDetails(cid).then(detail => {{
              if (!detail) return;
              if (state.openSections.has('prompt_' + cid)) {{
                const el = document.getElementById('content_prompt_' + cid);
                if (el) el.innerHTML = `<pre style="margin:0; white-space:pre-wrap; font-family:var(--font-mono); font-size:11px; color:#cbd5e1;">${{escapeHtml(detail.prompt || '--')}}</pre>`;
              }}
              if (state.openSections.has('steps_' + cid)) {{
                const el = document.getElementById('content_steps_' + cid);
                if (el) el.innerHTML = renderStepsHtml(detail.steps || []);
              }}
              if (state.openSections.has('report_' + cid)) {{
                const el = document.getElementById('content_report_' + cid);
                if (el) el.innerHTML = formatMarkdown(detail.final_report || 'No delivered report found.');
              }}
            }});
          }}
        }}
      }});
    }}

    async function ensureSentinelDetails(cid) {{
      if (state.sentinelDetails[cid]) return state.sentinelDetails[cid];
      try {{
        const resp = await fetch('/api/agent-activities/sentinels/' + encodeURIComponent(cid));
        if (resp.ok) {{
          const data = await resp.json();
          state.sentinelDetails[cid] = data;
          return data;
        }}
      }} catch (e) {{
        console.error('Failed to load sentinel details for', cid, e);
      }}
      return null;
    }}

    async function toggleSentinelSection(cid, field) {{
      const secKey = field + '_' + cid;
      const body = document.getElementById('body_' + secKey);
      const chevron = document.getElementById('chevron_' + secKey);
      if (!body) return;
      const isOpen = body.style.display !== 'none';

      if (isOpen) {{
        state.openSections.delete(secKey);
        body.style.display = 'none';
        if (chevron) chevron.style.transform = 'rotate(0deg)';
      }} else {{
        state.openSections.add(secKey);
        body.style.display = 'block';
        if (chevron) chevron.style.transform = 'rotate(180deg)';

        const detail = await ensureSentinelDetails(cid);
        if (detail) {{
          const contentEl = document.getElementById('content_' + secKey);
          if (contentEl) {{
            if (field === 'prompt') {{
              contentEl.innerHTML = `<pre style="margin:0; white-space:pre-wrap; font-family:var(--font-mono); font-size:11px; color:#cbd5e1;">${{escapeHtml(detail.prompt || '--')}}</pre>`;
            }} else if (field === 'report') {{
              contentEl.innerHTML = formatMarkdown(detail.final_report || 'No delivered report found.');
            }}
          }}
        }}
      }}
    }}

    async function toggleSentinelSteps(cid) {{
      const secKey = 'steps_' + cid;
      const body = document.getElementById('body_steps_' + cid);
      const chevron = document.getElementById('chevron_steps_' + cid);
      if (!body) return;
      const isOpen = body.style.display !== 'none';

      if (isOpen) {{
        state.openSections.delete(secKey);
        body.style.display = 'none';
        if (chevron) chevron.style.transform = 'rotate(0deg)';
      }} else {{
        state.openSections.add(secKey);
        body.style.display = 'block';
        if (chevron) chevron.style.transform = 'rotate(180deg)';

        const detail = await ensureSentinelDetails(cid);
        const contentEl = document.getElementById('content_steps_' + cid);
        if (!contentEl) return;

        if (!detail || !detail.steps || detail.steps.length === 0) {{
          contentEl.innerHTML = `<div style="color: var(--text-muted); padding: 12px;">No tool execution steps recorded.</div>`;
          return;
        }}

        contentEl.innerHTML = renderStepsHtml(detail.steps);
      }}
    }}

    function copySentinelField(cid, field) {{
      const detail = state.sentinelDetails[cid];
      if (!detail || !detail[field]) {{
        showToast('Field not loaded yet', 'warning');
        return;
      }}
      navigator.clipboard.writeText(detail[field]).then(() => {{
        showToast('Copied to clipboard', 'info');
      }}).catch(() => {{
        showToast('Copy failed', 'error');
      }});
    }}

    // CRM Agent Signals Logic
    async function loadSignals() {{
      const tbody = document.getElementById('signalsTableBody');
      try {{
        let url = '/api/agent-activities/signals?limit=100';
        if (state.searchQuery.trim()) {{
          url += '&q=' + encodeURIComponent(state.searchQuery.trim());
        }}
        const resp = await fetch(url);
        if (!resp.ok) {{
          tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--status-danger);padding:32px;">Failed loading signals: ${{resp.statusText}}</td></tr>`;
          return;
        }}
        const data = await resp.json();
        state.signals = data.signals || [];
        renderSignals();
      }} catch (err) {{
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--status-danger);padding:32px;">Error loading signals: ${{err.message}}</td></tr>`;
      }}
    }}

    function filterSignals(verdict) {{
      state.signalsFilter = verdict;
      ['All', 'Create', 'Correct', 'NoChange'].forEach(k => {{
        const btn = document.getElementById('pillSig' + k);
        if (btn) {{
          const isMatch = (k === 'All' && verdict === 'all') ||
                          (k === 'Create' && verdict === 'create') ||
                          (k === 'Correct' && verdict === 'correct') ||
                          (k === 'NoChange' && verdict === 'no_change');
          btn.classList.toggle('active', isMatch);
        }}
      }});
      renderSignals();
    }}

    function renderSignals() {{
      const tbody = document.getElementById('signalsTableBody');
      if (!state.signals || state.signals.length === 0) {{
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:32px;">No CRM Agent signals found.</td></tr>`;
        return;
      }}

      let list = state.signals;
      if (state.signalsFilter && state.signalsFilter !== 'all') {{
        list = list.filter(s => (s.verdict || '').toLowerCase() === state.signalsFilter.toLowerCase());
      }}

      if (list.length === 0) {{
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:32px;">No signals matching verdict "${{escapeHtml(state.signalsFilter)}}".</td></tr>`;
        return;
      }}

      tbody.innerHTML = list.map(s => {{
        const v = (s.verdict || 'unknown').toLowerCase();
        const confScore = s.confidence_score !== undefined && s.confidence_score !== null
          ? s.confidence_score
          : (s.result && s.result.confidence_score);
        const conf = confScore !== undefined && confScore !== null
          ? Math.round((confScore <= 1 ? confScore * 100 : confScore)) + '%'
          : '--';
        const isApplied = s.applied !== undefined && s.applied !== null
          ? s.applied
          : (s.result && s.result.applied);
        const applied = isApplied
          ? `<span class="badge badge-succeeded">Applied</span>`
          : `<span class="badge badge-queued">Pending</span>`;
        const notionBtn = s.target_page_url
          ? `<a href="${{escapeHtml(s.target_page_url)}}" target="_blank" class="notion-btn">
               <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/></svg>
               <span>Notion Page</span>
             </a>`
          : `<span style="color:var(--text-muted);">--</span>`;
        const emitted = escapeHtml(s.created_at || s.timestamp || '').replace('T', ' ').substring(0, 19);

        return `
          <tr onclick="inspectSignal('${{s.signal_id}}')">
            <td><span class="task-id-code" style="cursor: pointer;" onclick="event.stopPropagation(); copySignalJson('${{s.signal_id}}')">${{escapeHtml(s.signal_id)}}</span></td>
            <td><strong style="color: #ffffff;">${{escapeHtml(s.target_name || '--')}}</strong></td>
            <td><span class="verdict-badge verdict-${{v}}">${{escapeHtml(v.toUpperCase())}}</span></td>
            <td><span class="badge" style="background:rgba(99,102,241,0.2);color:#a5b4fc;">${{conf}}</span></td>
            <td onclick="event.stopPropagation()">${{notionBtn}}</td>
            <td>${{applied}}</td>
            <td style="color: var(--text-muted); font-size: 12px;">${{emitted}}</td>
            <td onclick="event.stopPropagation()">
              <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="inspectSignal('${{s.signal_id}}')">Inspect</button>
              <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px; margin-left: 4px;" onclick="copySignalJson('${{s.signal_id}}')">Copy</button>
            </td>
          </tr>
        `;
      }}).join('');
    }}

    let activeSignalObj = null;
    function inspectSignal(sigId) {{
      const sig = (state.signals || []).find(s => s.signal_id === sigId) ||
                  (state.activeTask && state.activeTask.agent_activity && state.activeTask.agent_activity.signal && state.activeTask.agent_activity.signal.signal_id === sigId ? state.activeTask.agent_activity.signal : null);
      if (!sig) return;
      activeSignalObj = sig;
      const modal = document.getElementById('signalModalOverlay');
      const title = document.getElementById('signalModalTitle');
      const body = document.getElementById('signalModalBody');
      if (title) title.innerText = 'Signal: ' + sig.signal_id;

      const v = (sig.verdict || 'unknown').toLowerCase();
      const confScore = sig.confidence_score !== undefined && sig.confidence_score !== null
        ? sig.confidence_score
        : (sig.result && sig.result.confidence_score);
      const confPct = Math.round((confScore <= 1 ? confScore * 100 : confScore) || 0);
      const isApplied = sig.applied !== undefined && sig.applied !== null
        ? sig.applied
        : (sig.result && sig.result.applied);
      const explanation = (sig.result && sig.result.explanation) || sig.explanation || '';
      const diffs = (sig.result && sig.result.diffs) || sig.diffs;
      const hasDiffs = Array.isArray(diffs) ? diffs.length > 0 : (diffs && typeof diffs === 'object' && Object.keys(diffs).length > 0);
      const pageUrl = sig.target_page_url || (sig.result && sig.result.target_page_url);

      body.innerHTML = `
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span class="verdict-badge verdict-${{v}}">${{escapeHtml(v.toUpperCase())}}</span>
            <span class="badge" style="background:rgba(99,102,241,0.2);color:#a5b4fc;">${{confPct}}% Confidence</span>
            <span class="badge ${{isApplied ? 'badge-succeeded' : 'badge-queued'}}">${{isApplied ? 'Applied' : 'Not Applied'}}</span>
          </div>
          ${{pageUrl ? `
            <a href="${{escapeHtml(pageUrl)}}" target="_blank" class="notion-btn">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/></svg>
              <span>Open in Notion</span>
            </a>
          ` : ''}}
        </div>

        <div class="drawer-meta-grid">
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Target Name</span>
            <span class="drawer-meta-val" style="color:#ffffff; font-weight:600;">${{escapeHtml(sig.target_name || '--')}}</span>
          </div>
          <div class="drawer-meta-item">
            <span class="drawer-meta-label">Emitted At</span>
            <span class="drawer-meta-val">${{escapeHtml(sig.created_at || sig.timestamp || '--')}}</span>
          </div>
          <div class="drawer-meta-item" style="grid-column: 1 / -1;">
            <span class="drawer-meta-label">Signal File</span>
            <span class="drawer-meta-val" style="font-family:var(--font-mono); font-size:11px; color:#93c5fd;">${{escapeHtml(sig.signal_file || '--')}}</span>
          </div>
        </div>

        ${{explanation ? `
          <div>
            <div class="sidebar-section-title" style="margin-bottom: 4px;">Auditor Explanation</div>
            <div style="background:#070a12; border:1px solid rgba(255,255,255,0.08); border-radius:6px; padding:10px 12px; font-size:12px; color:#cbd5e1; line-height:1.5;">
              ${{escapeHtml(explanation)}}
            </div>
          </div>
        ` : ''}}

        ${{hasDiffs ? `
          <div>
            <div class="sidebar-section-title" style="margin-bottom: 4px;">Proposed Diffs</div>
            <pre style="background:#070a12; border:1px solid rgba(255,255,255,0.08); border-radius:6px; padding:10px 12px; font-family:var(--font-mono); font-size:11px; color:#94a3b8; white-space:pre-wrap; margin:0;">${{escapeHtml(JSON.stringify(diffs, null, 2))}}</pre>
          </div>
        ` : ''}}

        <div>
          <div class="sidebar-section-title" style="margin-bottom: 4px;">Raw Payload JSON</div>
          <pre style="background:#070a12; border:1px solid rgba(255,255,255,0.08); border-radius:6px; padding:10px 12px; font-family:var(--font-mono); font-size:10px; color:#64748b; max-height:180px; overflow-y:auto; white-space:pre-wrap; margin:0;">${{escapeHtml(JSON.stringify(sig, null, 2))}}</pre>
        </div>
      `;

      modal.classList.add('open');
    }}

    function closeSignalModal() {{
      const modal = document.getElementById('signalModalOverlay');
      if (modal) modal.classList.remove('open');
      activeSignalObj = null;
    }}

    function copyActiveSignalJson() {{
      if (!activeSignalObj) return;
      navigator.clipboard.writeText(JSON.stringify(activeSignalObj, null, 2)).then(() => {{
        showToast('Copied signal JSON to clipboard', 'info');
      }}).catch(() => {{
        showToast('Failed copying JSON', 'error');
      }});
    }}

    function copySignalJson(sigId) {{
      const sig = (state.signals || []).find(s => s.signal_id === sigId) ||
                  (state.activeTask && state.activeTask.agent_activity && state.activeTask.agent_activity.signal && state.activeTask.agent_activity.signal.signal_id === sigId ? state.activeTask.agent_activity.signal : null);
      if (!sig) return;
      navigator.clipboard.writeText(JSON.stringify(sig, null, 2)).then(() => {{
        showToast('Copied signal ' + sigId + ' JSON', 'info');
      }}).catch(() => {{
        showToast('Failed copying JSON', 'error');
      }});
    }}

    // Sidebar Pulse Queue Logic
    async function loadPulses() {{
      const tbody = document.getElementById('pulsesTableBody');
      try {{
        let url = '/api/agent-activities/pulses?limit=50';
        if (state.searchQuery && state.searchQuery.trim()) {{
          url += '&q=' + encodeURIComponent(state.searchQuery.trim());
        }}
        const resp = await fetch(url);
        if (!resp.ok) {{
          tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--status-danger);padding:32px;">Failed loading pulse queue: ${{resp.statusText}}</td></tr>`;
          return;
        }}
        const data = await resp.json();
        state.pulses = data.pulses || [];
        const totalEl = document.getElementById('pulsesTotalCount');
        if (totalEl) totalEl.innerText = data.total || state.pulses.length;
        renderPulses();
      }} catch (err) {{
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--status-danger);padding:32px;">Error loading pulse queue: ${{err.message}}</td></tr>`;
      }}
    }}

    function renderPulses() {{
      const tbody = document.getElementById('pulsesTableBody');
      let pulses = state.pulses || [];
      if (state.searchQuery && state.searchQuery.trim()) {{
        const q = state.searchQuery.trim().toLowerCase();
        pulses = pulses.filter(p => {{
          const match = `${{p.prompt || ''}} ${{p.task_id || ''}} ${{p.source || ''}} ${{p.action || ''}} ${{p.status || ''}} ${{p.file_name || ''}}`.toLowerCase();
          return match.includes(q);
        }});
      }}
      if (!pulses || pulses.length === 0) {{
        tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:32px;">No matching sidebar pulses found.</td></tr>`;
        return;
      }}

      tbody.innerHTML = pulses.map(p => {{
        const taskLink = p.task_id
          ? `<a href="javascript:void(0)" onclick="openDrawer('${{p.task_id}}')" class="task-id-code">${{escapeHtml(p.task_id)}}</a>`
          : `<span style="color:var(--text-muted);">--</span>`;
        const sourceTag = `<span class="source-tag">${{escapeHtml(p.source || 'default')}}</span>`;
        const actionType = `<strong>${{escapeHtml(p.action || '-')}}</strong>`;
        const statusBadge = `<span class="badge badge-${{p.status || 'queued'}}">${{escapeHtml(p.status || 'queued')}}</span>`;
        const promptSnippet = escapeHtml(p.prompt || '').substring(0, 100) + (p.prompt && p.prompt.length > 100 ? '...' : '');

        return `
          <tr>
            <td style="color: var(--text-muted); font-size: 12px; white-space: nowrap;">${{escapeHtml(p.timestamp_iso || p.timestamp_ms || '-')}}</td>
            <td>${{taskLink}}</td>
            <td>${{sourceTag}}</td>
            <td>${{actionType}}</td>
            <td>${{statusBadge}}</td>
            <td style="max-width: 320px; overflow: hidden; text-overflow: ellipsis; font-family: var(--font-mono); font-size: 11px;" title="${{escapeHtml(p.prompt || '')}}">${{promptSnippet}}</td>
            <td>
              <div style="display: inline-flex; gap: 4px; align-items: center;">
                ${{p.task_id ? `<button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="openDrawer('${{p.task_id}}')" title="View execution logs">Logs</button>` : ''}}
                <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="copyText('${{escapeJsString(p.prompt || '')}}')" title="Copy pulse prompt">Copy</button>
              </div>
            </td>
          </tr>
        `;
      }}).join('');
    }}

    let tasksRefreshTimeout = null;
    function refreshTasksDebounced(delay = 150) {{
      clearTimeout(tasksRefreshTimeout);
      tasksRefreshTimeout = setTimeout(() => {{
        refreshTasksAuthoritative();
      }}, delay);
    }}

    // Antigravity Watchdog & Resuscitation Logic (SSOT from /antigravity/status)
    let watchdogStatusTimeout = null;
    function loadWatchdogStatusDebounced(delay = 100) {{
      clearTimeout(watchdogStatusTimeout);
      watchdogStatusTimeout = setTimeout(() => {{
        loadWatchdogStatus();
      }}, delay);
    }}

    async function loadWatchdogStatus() {{
      try {{
        const resp = await fetch('/antigravity/status');
        if (!resp.ok) {{
          console.warn('Failed to load watchdog status:', resp.statusText);
          return;
        }}
        const data = await resp.json();
        state.watchdogStatus = data;
        renderWatchdogStatus();
      }} catch (err) {{
        console.warn('Error fetching watchdog status:', err);
      }}
    }}

    function renderWatchdogStatus() {{
      const data = state.watchdogStatus;
      if (!data) return;

      // 1. Sidebar count and telemetry
      const countEl = document.getElementById('countWatchdogStalled');
      if (countEl) {{
        const stalledCount = (data.stalled_sessions && data.stalled_sessions.length) || data.stalled_sessions_detected || 0;
        countEl.innerText = stalledCount;
        if (stalledCount > 0) {{
          countEl.style.backgroundColor = 'rgba(239, 68, 68, 0.2)';
          countEl.style.color = 'var(--status-danger)';
        }} else {{
          countEl.style.backgroundColor = 'rgba(59, 130, 246, 0.15)';
          countEl.style.color = 'var(--accent-blue)';
        }}
      }}

      const telemProbe = document.getElementById('telemetryWatchdogProbe');
      if (telemProbe) {{
        if (data.network_online) {{
          telemProbe.innerText = 'Online';
          telemProbe.style.color = 'var(--status-success)';
        }} else {{
          telemProbe.innerText = 'Offline';
          telemProbe.style.color = 'var(--status-danger)';
        }}
      }}

      // 2. Telemetry Banner on Watchdog view
      const engineEl = document.getElementById('watchdogEngineStatus');
      if (engineEl) {{
        engineEl.innerHTML = data.watchdog_enabled
          ? `<span class="pulse-indicator" style="background-color: var(--status-success);"></span><span>Active 24/7</span>`
          : `<span class="pulse-indicator" style="background-color: var(--status-danger);"></span><span>Disabled</span>`;
      }}

      const netEl = document.getElementById('watchdogNetworkStatus');
      if (netEl) {{
        const probeTarget = data.probe_target || '1.1.1.1:53';
        netEl.innerHTML = data.network_online
          ? `<span class="pulse-indicator" style="background-color: var(--status-success);"></span><span>Online (${{escapeHtml(probeTarget)}})</span>`
          : `<span class="pulse-indicator" style="background-color: var(--status-danger);"></span><span>Offline</span>`;
      }}

      const lsEl = document.getElementById('watchdogLsStatus');
      if (lsEl) {{
        const lsAddr = data.language_server_address || '';
        lsEl.innerHTML = data.language_server_connected
          ? `<span class="pulse-indicator" style="background-color: var(--status-success);"></span><span title="${{escapeHtml(lsAddr)}}">Connected</span>`
          : `<span class="pulse-indicator" style="background-color: var(--status-warning);"></span><span>Unavailable</span>`;
      }}

      const autoPolicyEl = document.getElementById('watchdogAutoPolicy');
      if (autoPolicyEl) {{
        autoPolicyEl.innerText = data.auto_resuscitate ? `Enabled (${{data.interval_seconds || 30}}s Tick)` : 'Manual Only';
      }}

      // 3. Metric boxes
      const stalledCount = (data.stalled_sessions && data.stalled_sessions.length) || data.stalled_sessions_detected || 0;
      const statStalled = document.getElementById('statWatchdogStalled');
      if (statStalled) {{
        statStalled.innerText = stalledCount;
        statStalled.style.color = stalledCount > 0 ? 'var(--status-danger)' : 'var(--status-success)';
      }}

      const stats = data.resuscitation_stats || {{}};
      const statTotal = document.getElementById('statWatchdogTotalAttempts');
      if (statTotal) statTotal.innerText = stats.total || 0;

      const statRevived = document.getElementById('statWatchdogRevived');
      if (statRevived) statRevived.innerText = stats.resuscitated || 0;

      const statFailed = document.getElementById('statWatchdogFailed');
      if (statFailed) {{
        const failedCount = (stats.failed || 0) + (stats.exhausted || 0);
        statFailed.innerText = failedCount;
        statFailed.style.color = failedCount > 0 ? 'var(--status-danger)' : 'var(--text-muted)';
      }}

      const statInterval = document.getElementById('statWatchdogInterval');
      if (statInterval) statInterval.innerText = (data.interval_seconds || 30) + 's';

      // Toggle alert banner on main tasks view
      const alertBanner = document.getElementById('watchdogAlertBanner');
      if (alertBanner) {{
        if (stalledCount > 0) {{
          alertBanner.style.display = 'flex';
          const alertText = document.getElementById('watchdogAlertText');
          if (alertText) alertText.innerText = `${{stalledCount}} Antigravity session(s) stalled or interrupted in brain/ needing pull-up.`;
        }} else {{
          alertBanner.style.display = 'none';
        }}
      }}

      // 4. Stalled sessions table
      const badgeStalled = document.getElementById('badgeStalledCount');
      if (badgeStalled) {{
        badgeStalled.innerText = `${{stalledCount}} Detected`;
        badgeStalled.className = stalledCount > 0 ? 'badge badge-danger' : 'badge badge-queued';
      }}

      const stalledTbody = document.getElementById('stalledTableBody');
      if (stalledTbody) {{
        let list = data.stalled_sessions || [];
        const q = (state.searchQuery || '').trim().toLowerCase();
        if (q) {{
          list = list.filter(s =>
            (s.conversation_id && s.conversation_id.toLowerCase().includes(q)) ||
            (s.sidecar_slug && s.sidecar_slug.toLowerCase().includes(q)) ||
            (s.last_error && s.last_error.toLowerCase().includes(q)) ||
            (s.skip_reason && s.skip_reason.toLowerCase().includes(q))
          );
        }}
        if (list.length === 0) {{
          stalledTbody.innerHTML = `
            <tr>
              <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 36px 20px;">
                <div style="display: flex; flex-direction: column; align-items: center; gap: 8px;">
                  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--status-success);"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/></svg>
                  <span style="font-weight: 500; color: var(--text-main);">${{q ? 'No matching stalled sessions found' : 'All Sessions Healthy'}}</span>
                  <span style="font-size: 12px; color: var(--text-subtle);">${{q ? 'Try clearing or changing your search query.' : 'No stalled turns, hung network sockets, or interrupted subagents detected.'}}</span>
                </div>
              </td>
            </tr>
          `;
        }} else {{
          stalledTbody.innerHTML = list.map(s => {{
            const convoIdEsc = escapeHtml(s.conversation_id);
            const typeBadge = s.is_subagent
              ? '<span class="badge" style="background: rgba(147, 51, 234, 0.15); color: #c084fc;">Subagent</span>'
              : '<span class="badge" style="background: rgba(59, 130, 246, 0.15); color: var(--accent-blue);">Main Session</span>';
            const sidecar = escapeHtml(s.sidecar_slug || 'antigravity');
            const errEsc = escapeHtml(s.last_error || 'interrupted');
            const attempts = s.attempt_count || 0;
            const canRevive = s.can_resuscitate;
            const eligBadge = canRevive
              ? '<span class="badge badge-success">Eligible</span>'
              : `<span class="badge badge-warning" title="${{escapeHtml(s.skip_reason || '')}}">${{escapeHtml(s.skip_reason || 'Skipped')}}</span>`;

            return `
              <tr>
                <td>
                  <span class="task-id-code" style="cursor: pointer;" onclick="copyText('${{escapeJsString(s.conversation_id)}}')" title="Click to copy Conversation ID">${{convoIdEsc}}</span>
                </td>
                <td>${{typeBadge}}</td>
                <td><span class="source-tag">${{sidecar}}</span></td>
                <td style="max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 11px;" title="${{errEsc}}">${{errEsc}}</td>
                <td><strong>${{attempts}}</strong></td>
                <td>${{eligBadge}}</td>
                <td>
                  <button class="btn btn-primary" style="padding: 3px 10px; font-size: 11px;" onclick="pullUpSingleSession('${{escapeJsString(s.conversation_id)}}', this)" ${{canRevive ? '' : 'disabled'}}>
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>
                    <span>Pull-Up Now</span>
                  </button>
                </td>
              </tr>
            `;
          }}).join('');
        }}
      }}

      // 5. Resuscitation History Table
      const historyTbody = document.getElementById('resuscitationsTableBody');
      if (historyTbody) {{
        let history = data.recent_resuscitations || [];
        const filter = state.resuscitationFilter;

        const allCountEl = document.getElementById('pillResuscitationAllCount');
        if (allCountEl) allCountEl.innerText = stats.total || (data.recent_resuscitations ? data.recent_resuscitations.length : 0);
        const succCountEl = document.getElementById('pillResuscitationSuccessCount');
        if (succCountEl) succCountEl.innerText = stats.resuscitated || 0;
        const failCountEl = document.getElementById('pillResuscitationFailedCount');
        if (failCountEl) failCountEl.innerText = (stats.failed || 0) + (stats.exhausted || 0);

        if (filter === 'resuscitated') {{
          history = history.filter(r => r.status === 'resuscitated');
        }} else if (filter === 'failed') {{
          history = history.filter(r => r.status === 'failed' || r.status === 'exhausted');
        }}

        const q = (state.searchQuery || '').trim().toLowerCase();
        if (q) {{
          history = history.filter(r =>
            (r.conversation_id && r.conversation_id.toLowerCase().includes(q)) ||
            (r.resuscitation_id && r.resuscitation_id.toLowerCase().includes(q)) ||
            (r.sidecar_slug && r.sidecar_slug.toLowerCase().includes(q)) ||
            (r.last_error && r.last_error.toLowerCase().includes(q)) ||
            (r.status && r.status.toLowerCase().includes(q)) ||
            (r.resuscitation_prompt && r.resuscitation_prompt.toLowerCase().includes(q))
          );
        }}

        if (history.length === 0) {{
          historyTbody.innerHTML = `
            <tr>
              <td colspan="8" style="text-align: center; color: var(--text-muted); padding: 32px;">
                ${{q ? 'No resuscitation records matching "' + escapeHtml(q) + '"' : 'No resuscitation records found for current filter.'}}
              </td>
            </tr>
          `;
        }} else {{
          historyTbody.innerHTML = history.map(r => {{
            const resIdEsc = escapeHtml(r.resuscitation_id);
            const convoIdEsc = escapeHtml(r.conversation_id);
            const sidecar = escapeHtml(r.sidecar_slug || '-');
            const timeStr = escapeHtml(r.resuscitated_at || '-');
            const status = r.status || 'unknown';
            let badgeClass = 'badge-queued';
            if (status === 'resuscitated') badgeClass = 'badge-success';
            else if (status === 'failed') badgeClass = 'badge-danger';
            else if (status === 'exhausted') badgeClass = 'badge-warning';
            else if (status === 'attempting') badgeClass = 'badge-running';

            const errSnippet = escapeHtml(r.last_error || '-');

            return `
              <tr>
                <td style="color: var(--text-muted); font-size: 12px; white-space: nowrap;">${{timeStr}}</td>
                <td><span class="task-id-code" style="cursor: pointer;" onclick="copyText('${{escapeJsString(r.resuscitation_id)}}')" title="Click to copy">${{resIdEsc}}</span></td>
                <td><span class="task-id-code" style="cursor: pointer;" onclick="copyText('${{escapeJsString(r.conversation_id)}}')" title="Click to copy">${{convoIdEsc}}</span></td>
                <td><span class="source-tag">${{sidecar}}</span></td>
                <td><span class="badge ${{badgeClass}}">${{escapeHtml(status)}}</span></td>
                <td><strong>${{r.attempt_count || 1}}</strong></td>
                <td style="max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 11px;" title="${{errSnippet}}">${{errSnippet}}</td>
                <td>
                  <div style="display: inline-flex; gap: 4px; align-items: center;">
                    <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="openResuscitationModal('${{escapeJsString(r.resuscitation_id)}}')">Details</button>
                    <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="pullUpSingleSession('${{escapeJsString(r.conversation_id)}}', this)">Re-pull Up</button>
                  </div>
                </td>
              </tr>
            `;
          }}).join('');
        }}
      }}
    }}

    function setResuscitationFilter(filter) {{
      state.resuscitationFilter = filter;
      document.querySelectorAll('#pillResuscitationAll, #pillResuscitationSuccess, #pillResuscitationFailed').forEach(b => b.classList.remove('active'));
      const activeBtn = document.getElementById(
        filter === 'resuscitated' ? 'pillResuscitationSuccess' :
        filter === 'failed' ? 'pillResuscitationFailed' : 'pillResuscitationAll'
      );
      if (activeBtn) activeBtn.classList.add('active');
      renderWatchdogStatus();
    }}

    async function pullUpAllSessions(btnEl) {{
      if (btnEl) {{
        btnEl.disabled = true;
        btnEl.dataset.originalHtml = btnEl.innerHTML;
        btnEl.innerHTML = `<span class="pulse-indicator" style="background-color: #fff;"></span><span>Pulling up...</span>`;
      }}
      showToast('Triggering automated pull-up across all stalled sessions...', 'info');

      try {{
        const resp = await fetch('/antigravity/pull-up', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{}})
        }});
        const data = await resp.json();
        if (resp.ok && data.success) {{
          const count = data.resuscitated_count || (data.results && data.results.length) || 0;
          showToast(`Pull-up completed: ${{count}} session(s) processed`, 'success');
        }} else {{
          showToast('Pull-up returned notice: ' + (data.error || resp.statusText), 'warning');
        }}
      }} catch (err) {{
        showToast('Pull-up request failed: ' + err.message, 'error');
      }} finally {{
        if (btnEl) {{
          btnEl.disabled = false;
          btnEl.innerHTML = btnEl.dataset.originalHtml || '1-Click Pull-Up';
        }}
        // Unidirectional data flow: re-read authoritative state from SSOT API
        await loadWatchdogStatus();
        refreshTasksDebounced(150);
      }}
    }}

    async function pullUpSingleSession(convoId, btnEl) {{
      if (!convoId) return;
      if (btnEl) {{
        btnEl.disabled = true;
        btnEl.dataset.originalHtml = btnEl.innerHTML;
        btnEl.innerHTML = `<span class="pulse-indicator" style="background-color: currentColor;"></span><span>Pulling up...</span>`;
      }}
      showToast(`Initiating pull-up for session ${{convoId.substring(0, 10)}}...`, 'info');

      try {{
        const resp = await fetch('/antigravity/pull-up', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ conversation_id: convoId }})
        }});
        const data = await resp.json();
        if (resp.ok && data.success) {{
          showToast(`Session ${{convoId.substring(0, 8)}} resuscitated successfully!`, 'success');
        }} else {{
          showToast(`Pull-up failed: ${{data.error || data.status || resp.statusText}}`, 'error');
        }}
      }} catch (err) {{
        showToast(`Pull-up error: ${{err.message}}`, 'error');
      }} finally {{
        if (btnEl) {{
          btnEl.disabled = false;
          btnEl.innerHTML = btnEl.dataset.originalHtml || '<span>Pull-Up Now</span>';
        }}
        // Unidirectional data flow: re-read authoritative state from SSOT API
        await loadWatchdogStatus();
        refreshTasksDebounced(150);
      }}
    }}

    function openResuscitationModal(resId) {{
      const modal = document.getElementById('resuscitationModalOverlay');
      const body = document.getElementById('resuscitationModalBody');
      if (!modal || !body) return;

      const data = state.watchdogStatus;
      const history = (data && data.recent_resuscitations) || [];
      const rec = history.find(r => r.resuscitation_id === resId);

      if (!rec) {{
        body.innerHTML = `<div style="color: var(--status-danger);">Record ${{escapeHtml(resId)}} not found in local cache.</div>`;
        modal.classList.add('open');
        return;
      }}

      state.activeResuscitationRecord = rec;
      const promptText = rec.resuscitation_prompt || 'Default system prompt';

      body.innerHTML = `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; background: var(--bg-base); padding: 14px; border-radius: 8px; border: 1px solid var(--border-color);">
          <div>
            <span style="font-size: 11px; text-transform: uppercase; color: var(--text-subtle);">Resuscitation ID</span>
            <div style="font-family: var(--font-mono); font-size: 12px; font-weight: 600; color: var(--text-main);">${{escapeHtml(rec.resuscitation_id)}}</div>
          </div>
          <div>
            <span style="font-size: 11px; text-transform: uppercase; color: var(--text-subtle);">Timestamp</span>
            <div style="font-size: 12px; color: var(--text-muted);">${{escapeHtml(rec.resuscitated_at || '-')}}</div>
          </div>
          <div>
            <span style="font-size: 11px; text-transform: uppercase; color: var(--text-subtle);">Conversation ID</span>
            <div style="font-family: var(--font-mono); font-size: 12px; color: var(--accent-blue); word-break: break-all;">${{escapeHtml(rec.conversation_id)}}</div>
          </div>
          <div>
            <span style="font-size: 11px; text-transform: uppercase; color: var(--text-subtle);">Outcome Status</span>
            <div><span class="badge ${{rec.status === 'resuscitated' ? 'badge-success' : 'badge-danger'}}">${{escapeHtml(rec.status || 'unknown')}}</span></div>
          </div>
          <div style="grid-column: span 2;">
            <span style="font-size: 11px; text-transform: uppercase; color: var(--text-subtle);">Last Detected Error</span>
            <div style="font-family: var(--font-mono); font-size: 12px; color: #fca5a5; background: rgba(239, 68, 68, 0.1); padding: 6px 10px; border-radius: 6px; margin-top: 4px;">${{escapeHtml(rec.last_error || '-')}}</div>
          </div>
        </div>

        <div style="margin-top: 10px;">
          <span style="font-size: 11px; text-transform: uppercase; font-weight: 600; color: var(--text-subtle); letter-spacing: 0.05em;">Resuscitation Prompt Injected (agentapi payload)</span>
          <pre style="background: var(--bg-base); border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; font-family: var(--font-mono); font-size: 12px; color: #cbd5e1; white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow-y: auto; margin-top: 6px;">${{escapeHtml(promptText)}}</pre>
        </div>
      `;

      modal.classList.add('open');
    }}

    function closeResuscitationModal() {{
      const modal = document.getElementById('resuscitationModalOverlay');
      if (modal) modal.classList.remove('open');
      state.activeResuscitationRecord = null;
    }}

    function copyResuscitationPrompt() {{
      if (!state.activeResuscitationRecord) return;
      const prompt = state.activeResuscitationRecord.resuscitation_prompt || '';
      copyText(prompt);
      showToast('Copied resuscitation prompt to clipboard', 'info');
    }}

    // Antigravity 5h Quota Sentinel & Autonomous Warmup Logic (SSOT from /antigravity/quota)
    let quotaDebounceTimeout = null;
    function loadQuotaDebounced(delay = 100) {{
      clearTimeout(quotaDebounceTimeout);
      quotaDebounceTimeout = setTimeout(() => {{
        loadQuota(false);
      }}, delay);
    }}

    async function refreshQuotaLive(btnEl) {{
      if (btnEl) {{
        btnEl.disabled = true;
        btnEl.dataset.originalHtml = btnEl.innerHTML;
        btnEl.innerHTML = '<span class="loading-spinner"></span> Syncing...';
      }}
      showToast('Synchronizing live quotas with Google Cloud Code PA API...', 'info');
      try {{
        await loadQuota(true);
      }} finally {{
        if (btnEl) {{
          btnEl.disabled = false;
          btnEl.innerHTML = btnEl.dataset.originalHtml || '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg> <span>Sync Live Quotas</span>';
        }}
      }}
    }}

    async function loadQuota(forceLive = false) {{
      try {{
        const url = forceLive ? '/antigravity/quota?refresh=true' : '/antigravity/quota';
        const resp = await fetch(url);
        if (!resp.ok) {{
          console.warn('Failed to load quota overview:', resp.statusText);
          return;
        }}
        const data = await resp.json();
        state.quotaOverview = data;
        renderQuotaOverview();
        startQuotaClockTicker();
        if (forceLive) {{
          showToast('Live quota synchronization complete', 'info');
        }}
      }} catch (err) {{
        console.warn('Error fetching quota overview:', err);
        showToast('Failed to fetch quota overview: ' + err.message, 'error');
      }}
    }}

    function formatRemainingClock(sec) {{
      if (sec === null || sec === undefined) return '--:--';
      if (sec <= 0) return 'Reset ready';
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      if (h > 0) {{
        return `${{h}}h ${{String(m).padStart(2, '0')}}m ${{String(s).padStart(2, '0')}}s`;
      }}
      return `${{String(m).padStart(2, '0')}}m ${{String(s).padStart(2, '0')}}s`;
    }}

    function startQuotaClockTicker() {{
      if (state.quotaClockInterval) {{
        clearInterval(state.quotaClockInterval);
      }}
      state.quotaClockInterval = setInterval(() => {{
        if (!state.quotaOverview) return;

        // 1. Tick active account buckets
        const active = state.quotaOverview.active_account;
        if (active && active.buckets) {{
          active.buckets.forEach(b => {{
            if (b.remaining_seconds && b.remaining_seconds > 0) {{
              b.remaining_seconds -= 1;
            }}
          }});
        }}

        // 2. Tick all fleet accounts
        if (state.quotaOverview.all_accounts) {{
          state.quotaOverview.all_accounts.forEach(acc => {{
            if (acc.buckets) {{
              acc.buckets.forEach(b => {{
                if (b.remaining_seconds && b.remaining_seconds > 0) {{
                  b.remaining_seconds -= 1;
                }}
              }});
            }}
          }});
        }}

        // Update DOM clocks directly without destroying elements
        updateClockElements();
      }}, 1000);
    }}

    function updateClockElements() {{
      if (!state.quotaOverview) return;
      const active = state.quotaOverview.active_account;
      if (active && active.buckets) {{
        const gemini5h = active.buckets.find(b => b.bucket_id === 'gemini-5h');
        if (gemini5h) {{
          const txt = formatRemainingClock(gemini5h.remaining_seconds);
          const topClock = document.getElementById('statQuotaGeminiCountdown');
          if (topClock) topClock.innerText = txt;
          const activeClock = document.getElementById('clockActiveGemini5h');
          if (activeClock) activeClock.innerText = txt;
        }}
        const p3_5h = active.buckets.find(b => b.bucket_id === '3p-5h');
        if (p3_5h) {{
          const txt = formatRemainingClock(p3_5h.remaining_seconds);
          const activeClock3p = document.getElementById('clockActive3p5h');
          if (activeClock3p) activeClock3p.innerText = txt;
        }}
      }}

      // Fleet table clocks
      if (state.quotaOverview.all_accounts) {{
        state.quotaOverview.all_accounts.forEach((acc, idx) => {{
          const accKey = (acc.account_id || acc.email || ('acc_' + idx)).replace(/[^a-zA-Z0-9_-]/g, '_');
          const g5 = acc.buckets && acc.buckets.find(b => b.bucket_id === 'gemini-5h');
          if (g5) {{
            const el = document.getElementById(`clockFleetGemini_${{accKey}}`);
            if (el) el.innerText = formatRemainingClock(g5.remaining_seconds);
          }}
          const c5 = acc.buckets && acc.buckets.find(b => b.bucket_id === '3p-5h');
          if (c5) {{
            const el = document.getElementById(`clockFleet3p_${{accKey}}`);
            if (el) el.innerText = formatRemainingClock(c5.remaining_seconds);
          }}
        }});
      }}
    }}

    function renderQuotaOverview() {{
      const data = state.quotaOverview;
      if (!data) return;

      const active = data.active_account;
      const allAccs = data.all_accounts || [];
      const recentWarmups = data.recent_warmups || [];

      // 1. Top metric row
      const statEmail = document.getElementById('statQuotaActiveEmail');
      if (statEmail) {{
        statEmail.innerText = active ? active.email : 'None';
      }}

      let gemini5hPct = '--%';
      let geminiCountdown = '--:--';
      let p3_5hPct = '--%';

      if (active && active.buckets) {{
        const g5 = active.buckets.find(b => b.bucket_id === 'gemini-5h');
        if (g5) {{
          gemini5hPct = (g5.remaining_percent !== null && g5.remaining_percent !== undefined) ? g5.remaining_percent.toFixed(1) + '%' : '--%';
          geminiCountdown = formatRemainingClock(g5.remaining_seconds);
        }}
        const c5 = active.buckets.find(b => b.bucket_id === '3p-5h');
        if (c5) {{
          p3_5hPct = (c5.remaining_percent !== null && c5.remaining_percent !== undefined) ? c5.remaining_percent.toFixed(1) + '%' : '--%';
        }}
      }}

      const statG5 = document.getElementById('statQuotaGemini5h');
      if (statG5) statG5.innerText = gemini5hPct;

      const statGCount = document.getElementById('statQuotaGeminiCountdown');
      if (statGCount) statGCount.innerText = geminiCountdown;

      const stat3p = document.getElementById('statQuota3p5h');
      if (stat3p) stat3p.innerText = p3_5hPct;

      const statWarmups = document.getElementById('statQuotaWarmupCount');
      if (statWarmups) {{
        statWarmups.innerText = recentWarmups.filter(w => w.status === 'success').length;
      }}

      // 2. Featured Active Account Card (Gemini highlighted)
      const activeCardContainer = document.getElementById('quotaActiveAccountContainer');
      if (activeCardContainer) {{
        if (!active) {{
          activeCardContainer.innerHTML = '<div style="padding: 24px; text-align: center; color: var(--text-muted);">No active IDE account detected in ~/.gemini/antigravity.</div>';
        }} else {{
          const g5 = (active.buckets || []).find(b => b.bucket_id === 'gemini-5h');
          const gWeek = (active.buckets || []).find(b => b.bucket_id === 'gemini-weekly');
          const c5 = (active.buckets || []).find(b => b.bucket_id === '3p-5h');
          const cWeek = (active.buckets || []).find(b => b.bucket_id === '3p-weekly');

          const serverDesc = (g5 && g5.server_description) || (gWeek && gWeek.server_description) || '';

          const g5Pct = g5 ? (g5.remaining_percent || 0).toFixed(1) : '100.0';
          const c5Pct = c5 ? (c5.remaining_percent || 0).toFixed(1) : '100.0';
          const gWeekPct = gWeek ? (gWeek.remaining_percent || 0).toFixed(1) : '100.0';
          const cWeekPct = cWeek ? (cWeek.remaining_percent || 0).toFixed(1) : '100.0';

          const g5Sec = g5 ? g5.remaining_seconds : null;
          const c5Sec = c5 ? c5.remaining_seconds : null;

          activeCardContainer.innerHTML = `
            <div class="quota-card quota-card-primary" style="margin-bottom: 24px;">
              <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 16px;">
                <div style="display: flex; align-items: center; gap: 10px;">
                  <span class="quota-badge-gemini">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                    ACTIVE IDE POOL
                  </span>
                  <span style="font-size: 16px; font-weight: 700; color: #ffffff;">${{escapeHtml(active.email)}}</span>
                  <span class="badge" style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3);">${{escapeHtml(active.subscription_tier || 'Subscription')}}</span>
                </div>
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span class="badge" style="background: rgba(16, 185, 129, 0.15); color: #34d399; display: inline-flex; align-items: center; gap: 6px;">
                    <span class="pulse-indicator" style="background-color: var(--status-success); width: 6px; height: 6px;"></span>
                    Google Cloud Code PA: Live
                  </span>
                </div>
              </div>

              ${{serverDesc ? `
                <div style="background: rgba(16, 185, 129, 0.08); border-left: 3px solid var(--status-success); padding: 10px 14px; border-radius: 4px; margin-bottom: 16px; font-size: 13px; color: #a7f3d0; display: flex; align-items: center; gap: 8px;">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink: 0;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
                  <span><strong>Upstream Status:</strong> "${{escapeHtml(serverDesc)}}"</span>
                </div>
              ` : ''}}

              <div class="quota-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 16px;">
                <!-- Gemini 5h Card (Primary) -->
                <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 8px; padding: 16px;">
                  <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 6px;">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: var(--status-success);"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
                      <span style="font-weight: 600; color: #ffffff;">Gemini Models (Primary)</span>
                    </div>
                    <span class="countdown-pill" id="clockActiveGemini5h">${{formatRemainingClock(g5Sec)}}</span>
                  </div>
                  <div style="font-size: 11px; color: var(--text-muted); margin-bottom: 12px;">5-Hour Rolling Limit (Autonomous Warmup Target)</div>
                  
                  <div style="display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px;">
                    <span style="font-size: 24px; font-weight: 700; color: var(--status-success);">${{g5Pct}}%</span>
                    <span style="font-size: 11px; color: var(--text-muted);">Remaining</span>
                  </div>
                  <div class="quota-progress-track" style="margin-bottom: 14px;">
                    <div class="quota-progress-fill quota-progress-gemini" style="width: ${{g5Pct}}%;"></div>
                  </div>

                  <button class="btn btn-secondary" style="width: 100%; justify-content: center; font-size: 12px; padding: 6px;" onclick="triggerBucketWarmup('${{escapeJsString(active.email)}}', 'gemini-5h', this)">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                    <span>Warmup Gemini 5h Ping</span>
                  </button>
                </div>

                <!-- Claude & GPT 5h Card -->
                <div style="background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(192, 132, 252, 0.3); border-radius: 8px; padding: 16px;">
                  <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
                    <div style="display: flex; align-items: center; gap: 6px;">
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color: #c084fc;"><circle cx="12" cy="12" r="10"/><path d="m4.93 4.93 4.24 4.24"/><path d="m14.83 9.17 4.24-4.24"/><path d="m14.83 14.83 4.24 4.24"/><path d="m9.17 14.83-4.24 4.24"/></svg>
                      <span style="font-weight: 600; color: #ffffff;">Claude &amp; GPT Models</span>
                    </div>
                    <span class="countdown-pill" id="clockActive3p5h">${{formatRemainingClock(c5Sec)}}</span>
                  </div>
                  <div style="font-size: 11px; color: var(--text-muted); margin-bottom: 12px;">5-Hour Rolling Limit</div>
                  
                  <div style="display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px;">
                    <span style="font-size: 24px; font-weight: 700; color: #c084fc;">${{c5Pct}}%</span>
                    <span style="font-size: 11px; color: var(--text-muted);">Remaining</span>
                  </div>
                  <div class="quota-progress-track" style="margin-bottom: 14px;">
                    <div class="quota-progress-fill" style="width: ${{c5Pct}}%; background: linear-gradient(90deg, #a855f7, #c084fc);"></div>
                  </div>

                  <button class="btn btn-secondary" style="width: 100%; justify-content: center; font-size: 12px; padding: 6px;" onclick="triggerBucketWarmup('${{escapeJsString(active.email)}}', '3p-5h', this)">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                    <span>Warmup Claude 5h Ping</span>
                  </button>
                </div>
              </div>

              <!-- Weekly Secondary Row -->
              <div style="display: flex; gap: 16px; flex-wrap: wrap; padding-top: 12px; border-top: 1px solid rgba(255, 255, 255, 0.08); font-size: 12px; color: var(--text-muted);">
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span>Gemini Weekly Pool:</span>
                  <span style="font-weight: 600; color: #ffffff;">${{gWeekPct}}%</span>
                  <span style="font-size: 11px; color: var(--text-muted);">${{gWeek ? (gWeek.human_countdown || '') : ''}}</span>
                </div>
                <div style="display: flex; align-items: center; gap: 8px;">
                  <span>Claude &amp; GPT Weekly Pool:</span>
                  <span style="font-weight: 600; color: #ffffff;">${{cWeekPct}}%</span>
                  <span style="font-size: 11px; color: var(--text-muted);">${{cWeek ? (cWeek.human_countdown || '') : ''}}</span>
                </div>
              </div>
            </div>
          `;
        }}
      }}

      // 3. Fleet Multi-Account Matrix Table
      const fleetTbody = document.getElementById('quotaFleetTableBody');
      const badgeAccounts = document.getElementById('badgeQuotaAccountsCount');
      if (badgeAccounts) {{
        badgeAccounts.innerText = `${{allAccs.length}} Accounts Tracked`;
      }}

      if (fleetTbody) {{
        const query = (state.searchQuery || '').trim().toLowerCase();
        const filtered = allAccs.filter(acc => {{
          if (!query) return true;
          return (acc.email || '').toLowerCase().includes(query) ||
                 (acc.subscription_tier || '').toLowerCase().includes(query);
        }});

        if (filtered.length === 0) {{
          fleetTbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">No matching accounts found.</td></tr>';
        }} else {{
          fleetTbody.innerHTML = filtered.map((acc, idx) => {{
            const accKey = (acc.account_id || acc.email || ('acc_' + idx)).replace(/[^a-zA-Z0-9_-]/g, '_');
            const g5 = (acc.buckets || []).find(b => b.bucket_id === 'gemini-5h');
            const c5 = (acc.buckets || []).find(b => b.bucket_id === '3p-5h');

            const g5Pct = g5 ? (g5.remaining_percent || 0).toFixed(1) : '--';
            const c5Pct = c5 ? (c5.remaining_percent || 0).toFixed(1) : '--';

            const g5Sec = g5 ? g5.remaining_seconds : null;
            const c5Sec = c5 ? c5.remaining_seconds : null;

            return `
              <tr style="${{acc.is_active_account ? 'background: rgba(16, 185, 129, 0.05);' : ''}}">
                <td>
                  <div style="display: flex; align-items: center; gap: 8px;">
                    ${{acc.is_active_account ? '<span class="quota-badge-gemini" style="font-size: 10px; padding: 2px 6px;">ACTIVE</span>' : ''}}
                    <span style="font-weight: ${{acc.is_active_account ? '600' : '400'}}; color: #ffffff;">${{escapeHtml(acc.email)}}</span>
                  </div>
                </td>
                <td>
                  <span class="badge" style="font-size: 11px;">${{escapeHtml(acc.subscription_tier || 'Free')}}</span>
                </td>
                <td>
                  <div style="display: flex; align-items: center; gap: 8px; min-width: 120px;">
                    <div class="quota-progress-track" style="flex: 1; height: 6px;">
                      <div class="quota-progress-fill quota-progress-gemini" style="width: ${{g5Pct === '--' ? 0 : g5Pct}}%;"></div>
                    </div>
                    <span style="font-size: 12px; font-weight: 600; color: var(--status-success); min-width: 42px;">${{g5Pct}}%</span>
                  </div>
                </td>
                <td>
                  <span class="countdown-pill" id="clockFleetGemini_${{accKey}}">${{formatRemainingClock(g5Sec)}}</span>
                </td>
                <td>
                  <div style="display: flex; align-items: center; gap: 8px; min-width: 120px;">
                    <div class="quota-progress-track" style="flex: 1; height: 6px;">
                      <div class="quota-progress-fill" style="width: ${{c5Pct === '--' ? 0 : c5Pct}}%; background: #c084fc;"></div>
                    </div>
                    <span style="font-size: 12px; font-weight: 600; color: #c084fc; min-width: 42px;">${{c5Pct}}%</span>
                  </div>
                </td>
                <td>
                  <span class="countdown-pill" id="clockFleet3p_${{accKey}}">${{formatRemainingClock(c5Sec)}}</span>
                </td>
                <td>
                  <div style="display: inline-flex; gap: 6px; align-items: center;">
                    <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px;" title="Warmup Gemini 5h pool" onclick="triggerBucketWarmup('${{escapeJsString(acc.email)}}', 'gemini-5h', this)" ${{g5 ? '' : 'disabled'}}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                      <span>Gemini</span>
                    </button>
                    <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px;" title="Warmup Claude/GPT 5h pool" onclick="triggerBucketWarmup('${{escapeJsString(acc.email)}}', '3p-5h', this)" ${{c5 ? '' : 'disabled'}}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="m4.93 4.93 4.24 4.24"/><path d="m14.83 9.17 4.24-4.24"/><path d="m14.83 14.83 4.24 4.24"/><path d="m9.17 14.83-4.24 4.24"/></svg>
                      <span>Claude</span>
                    </button>
                  </div>
                </td>
              </tr>
            `;
          }}).join('');
        }}
      }}

      // 4. Warmup Audit Log Table
      const warmupTbody = document.getElementById('quotaWarmupLogsTableBody');
      const badgeWarmup = document.getElementById('badgeWarmupLogsCount');
      if (badgeWarmup) {{
        badgeWarmup.innerText = `${{recentWarmups.length}} Records`;
      }}

      if (warmupTbody) {{
        if (recentWarmups.length === 0) {{
          warmupTbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 32px;">No warmup executions logged yet.</td></tr>';
        }} else {{
          warmupTbody.innerHTML = recentWarmups.map(w => {{
            const isSuccess = w.status === 'success';
            const badgeClass = isSuccess ? 'badge-success' : 'badge-danger';
            return `
              <tr>
                <td style="font-size: 12px; color: var(--text-muted); white-space: nowrap;">${{escapeHtml(w.created_at)}}</td>
                <td style="font-weight: 500; color: #ffffff;">${{escapeHtml(w.account_email)}}</td>
                <td><span class="badge" style="font-size: 11px;">${{escapeHtml(w.bucket_id)}}</span></td>
                <td><span style="font-family: monospace; font-size: 11px; color: #38bdf8;">${{escapeHtml(w.model_name || '--')}}</span></td>
                <td style="font-size: 12px; color: var(--text-muted);">${{escapeHtml(w.trigger_reason)}}</td>
                <td><span class="badge ${{badgeClass}}">${{escapeHtml(w.status)}}</span></td>
                <td style="font-size: 12px; font-family: monospace; color: var(--text-muted);">${{w.duration_ms}}ms</td>
              </tr>
            `;
          }}).join('');
        }}
      }}
    }}

    async function triggerBucketWarmup(accountEmail, bucketId, btnEl) {{
      if (btnEl) {{
        btnEl.disabled = true;
        btnEl.innerHTML = '<span class="loading-spinner"></span> Warming...';
      }}
      try {{
        showToast(`Sending autonomous warmup ping for ${{accountEmail}} (${{bucketId}})...`, 'info');
        const resp = await fetch('/antigravity/warmup', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            account: accountEmail,
            bucket: bucketId,
            force: true,
            reason: 'ui_manual_bucket'
          }})
        }});
        if (resp.ok) {{
          const res = await resp.json();
          if (res.warmup_results && res.warmup_results.length > 0) {{
            const first = res.warmup_results[0];
            if (first.status === 'success') {{
              showToast(`Warmup successful (${{first.duration_ms}}ms, model: ${{first.model_used}})`, 'info');
            }} else {{
              showToast(`Warmup failed: ${{first.error || 'Check logs'}}`, 'error');
            }}
          }} else {{
            showToast('Warmup command processed', 'info');
          }}
          loadQuotaDebounced(100);
        }} else {{
          showToast('Failed to trigger warmup: ' + resp.statusText, 'error');
        }}
      }} catch (err) {{
        showToast('Warmup dispatch error: ' + err.message, 'error');
      }} finally {{
        if (btnEl) {{
          btnEl.disabled = false;
          btnEl.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg> <span>Warmup</span>';
        }}
      }}
    }}

    async function triggerAllReadyWarmups(btnEl) {{
      if (btnEl) {{
        btnEl.disabled = true;
        btnEl.innerHTML = '<span class="loading-spinner"></span> Warming Pools...';
      }}
      try {{
        showToast('Evaluating and warming up all idle quota pools...', 'info');
        const resp = await fetch('/antigravity/warmup', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            force: false,
            reason: 'ui_warmup_all_idle'
          }})
        }});
        if (resp.ok) {{
          const res = await resp.json();
          const executed = res.warmups_executed || 0;
          showToast(`Warmup pass complete: ${{executed}} pool(s) refreshed`, 'info');
          loadQuotaDebounced(100);
        }} else {{
          showToast('Failed to trigger fleet warmup: ' + resp.statusText, 'error');
        }}
      }} catch (err) {{
        showToast('Fleet warmup error: ' + err.message, 'error');
      }} finally {{
        if (btnEl) {{
          btnEl.disabled = false;
          btnEl.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg> <span>Warmup All Idle Pools</span>';
        }}
      }}
    }}

    // Real-time Push Subscription via Server-Sent Events (SSE)
    function setupGlobalEventSource() {{
      if (state.globalEventSource) {{
        state.globalEventSource.close();
      }}

      const sse = new EventSource('/events/stream');
      state.globalEventSource = sse;

      sse.onopen = () => {{
        document.getElementById('ssePulse').style.backgroundColor = 'var(--status-success)';
        document.getElementById('sseStatusText').innerText = 'LIVE';
      }};

      sse.onerror = () => {{
        document.getElementById('ssePulse').style.backgroundColor = 'var(--status-danger)';
        document.getElementById('sseStatusText').innerText = 'RECONNECT';
      }};

      // Reactive update: when an event arrives, re-read authoritative state from SSOT
      const onEventUpdate = () => refreshTasksDebounced(150);
      sse.addEventListener('status_changed', onEventUpdate);
      sse.addEventListener('completed', onEventUpdate);
      sse.addEventListener('status_change', onEventUpdate);
      sse.addEventListener('task_created', onEventUpdate);
      sse.addEventListener('event', onEventUpdate);
      sse.addEventListener('message', onEventUpdate);

      sse.addEventListener('sweeper_run', (e) => {{
        refreshTasksDebounced(100);
        showToast('Auto-picker sweeper executed recovery pass', 'info');
      }});

      sse.addEventListener('antigravity_resuscitation', (e) => {{
        loadWatchdogStatusDebounced(100);
        refreshTasksDebounced(150);
        try {{
          const data = JSON.parse(e.data);
          const action = data.action || data.status || 'resuscitation';
          showToast('Antigravity Watchdog: ' + action, 'info');
        }} catch (_) {{
          showToast('Antigravity Watchdog activity detected', 'info');
        }}
      }});

      sse.addEventListener('antigravity_watchdog_sweep', (e) => {{
        loadWatchdogStatusDebounced(100);
        refreshTasksDebounced(150);
        showToast('Antigravity Watchdog sweeper run completed', 'info');
      }});

      sse.addEventListener('antigravity_status', () => {{
        loadWatchdogStatusDebounced(100);
      }});

      sse.addEventListener('antigravity_quota_update', (e) => {{
        loadQuotaDebounced(100);
        showToast('Antigravity live quota matrix updated', 'info');
      }});

      sse.addEventListener('antigravity_quota_warmup', (e) => {{
        loadQuotaDebounced(100);
        try {{
          const data = JSON.parse(e.data);
          const email = data.account_email || 'pool';
          const model = data.model_name || data.model_used || '';
          showToast(`Autonomous warmup succeeded: ${{email}} (${{model}})`, 'info');
        }} catch (_) {{
          showToast('Autonomous quota warmup executed', 'info');
        }}
      }});
    }}

    // Task Drawer & Live Stream Logs
    const MAX_TERMINAL_LINES = 500;

    function copyTaskIdToClipboard() {{
      if (!state.activeTaskId) return;
      navigator.clipboard.writeText(state.activeTaskId).then(() => {{
        showToast('Copied Task ID: ' + state.activeTaskId, 'info');
      }}).catch(() => {{
        showToast('Task ID: ' + state.activeTaskId, 'info');
      }});
    }}

    function parseAnsi(text, tokens) {{
      if (!text || (text.indexOf('\u001b[') === -1 && text.indexOf('\\u001b[') === -1 && text.indexOf('\\033[') === -1 && text.indexOf('\x1b[') === -1)) {{
        return text;
      }}
      const ansiRegex = /(?:\u001b|\x1b|\\u001b|\\x1b|\\033)\\[([0-9;]*)([A-Za-z])/g;
      let openSpans = 0;
      let out = '';
      let lastIndex = 0;
      let match;

      while ((match = ansiRegex.exec(text)) !== null) {{
        out += text.slice(lastIndex, match.index);
        lastIndex = ansiRegex.lastIndex;
        const command = match[2];
        if (command !== 'm') continue;

        const parts = (match[1] || '0').split(';').map(p => parseInt(p, 10) || 0);
        for (let i = 0; i < parts.length; i++) {{
          const code = parts[i];
          if (code === 0) {{
            while (openSpans > 0) {{
              const id = tokens.length;
              tokens.push('</span>');
              out += '\x01TOK' + id + '\x02';
              openSpans--;
            }}
          }} else if (code === 1) {{
            const id = tokens.length;
            tokens.push('<span class="ansi-bold">');
            out += '\x01TOK' + id + '\x02';
            openSpans++;
          }} else if (code === 2) {{
            const id = tokens.length;
            tokens.push('<span class="ansi-dim">');
            out += '\x01TOK' + id + '\x02';
            openSpans++;
          }} else if (code === 3) {{
            const id = tokens.length;
            tokens.push('<span class="ansi-italic">');
            out += '\x01TOK' + id + '\x02';
            openSpans++;
          }} else if (code === 4) {{
            const id = tokens.length;
            tokens.push('<span class="ansi-underline">');
            out += '\x01TOK' + id + '\x02';
            openSpans++;
          }} else if ((code >= 30 && code <= 37) || (code >= 90 && code <= 97)) {{
            const id = tokens.length;
            tokens.push('<span class="ansi-' + code + '">');
            out += '\x01TOK' + id + '\x02';
            openSpans++;
          }}
        }}
      }}
      out += text.slice(lastIndex);
      while (openSpans > 0) {{
        const id = tokens.length;
        tokens.push('</span>');
        out += '\x01TOK' + id + '\x02';
        openSpans--;
      }}
      return out;
    }}

    function detectLogLevel(text, stream) {{
      if (stream === 'stderr') return 'error';
      const upper = (text || '').toUpperCase();
      if (/\\[?(ERROR|FATAL|FAIL|FAILED)\\]?/.test(upper) || upper.includes('TRACEBACK') || /\\w+ERROR:/.test(text)) return 'error';
      if (/\\[?(WARN|WARNING)\\]?/.test(upper)) return 'warn';
      if (/\\[?(SUCCESS|PASS|PASSED|OK)\\]?/.test(upper)) return 'success';
      if (/\\[?DEBUG\\]?/.test(upper)) return 'debug';
      if (/\\[?INFO\\]?/.test(upper)) return 'info';
      return 'default';
    }}

    function escapeLogText(str) {{
      if (!str) return '';
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}

    function formatLogLineHtml(rawText, stream) {{
      if (!rawText && rawText !== '') return '';
      let tokens = [];
      function addTok(html) {{
        const id = tokens.length;
        tokens.push(html);
        return '\x01TOK' + id + '\x02';
      }}

      let s = escapeLogText(rawText);
      s = parseAnsi(s, tokens);

      // 1. Python & JS Stack Trace Highlights
      s = s.replace(/Traceback \\(most recent call last\\):/g, (m) => {{
        return addTok('<span class="log-traceback-title"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="display:inline-block;vertical-align:middle;margin-right:4px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' + m + '</span>');
      }});

      s = s.replace(/File "([^"]+)", line (\\d+)(?:, in (.+))?/g, (m, file, line, func) => {{
        let res = 'File "' + addTok('<span class="log-path">' + file + '</span>') + '", line ' + addTok('<span class="log-traceback-line">' + line + '</span>');
        if (func) res += ', in ' + addTok('<span class="log-traceback-func">' + func + '</span>');
        return res;
      }});

      s = s.replace(/^(\\s*)([A-Z][a-zA-Z0-9]*(?:Error|Exception|Exit|Warning)):(.*)$/g, (m, indent, exc, rest) => {{
        return indent + addTok('<span class="log-lvl log-lvl-error">' + exc + ':</span>') + addTok('<span class="log-traceback-msg">' + rest + '</span>');
      }});

      s = s.replace(/^(\\s*at )(?:async )?([a-zA-Z0-9_\\.$<>]+ )?\\(([^:\\)]+):(\\d+):(\\d+)\\)/g, (m, atPrefix, func, file, line, col) => {{
        let res = atPrefix;
        if (func) res += addTok('<span class="log-traceback-func">' + func.trim() + '</span>') + ' (';
        else res += '(';
        res += addTok('<span class="log-path">' + file + '</span>') + ':' + addTok('<span class="log-traceback-line">' + line + ':' + col + '</span>') + ')';
        return res;
      }});

      // 2. Embedded JSON formatting
      s = s.replace(/"([a-zA-Z0-9_\\-]+)"\\s*:/g, (m, k) => {{
        return addTok('<span class="log-json-key">"' + k + '"</span>') + ':';
      }});
      s = s.replace(/(:\\s*)"([^"\\\\]*)"/g, (m, pfx, v) => {{
        const upper = v.trim().toUpperCase();
        let lvlCls = '';
        if (upper === 'INFO') lvlCls = 'log-lvl-info';
        else if (upper === 'WARN' || upper === 'WARNING') lvlCls = 'log-lvl-warn';
        else if (upper === 'ERROR' || upper === 'FATAL' || upper === 'FAIL' || upper === 'FAILED') lvlCls = 'log-lvl-error';
        else if (upper === 'SUCCESS' || upper === 'PASS' || upper === 'PASSED') lvlCls = 'log-lvl-success';
        else if (upper === 'DEBUG') lvlCls = 'log-lvl-debug';

        if (lvlCls) {{
          return pfx + addTok('<span class="log-json-str ' + lvlCls + '">"' + v + '"</span>');
        }}
        return pfx + addTok('<span class="log-json-str">"' + v + '"</span>');
      }});
      s = s.replace(/(:\\s*)(-?\\d+(?:\\.\\d+)?)([, \\n\\r}}\\]])/g, (m, pfx, num, term) => {{
        return pfx + addTok('<span class="log-json-num">' + num + '</span>') + term;
      }});
      s = s.replace(/(:\\s*)(true|false)([, \\n\\r}}\\]])/g, (m, pfx, b, term) => {{
        return pfx + addTok('<span class="log-json-bool">' + b + '</span>') + term;
      }});
      s = s.replace(/(:\\s*)(null)([, \\n\\r}}\\]])/g, (m, pfx, n, term) => {{
        return pfx + addTok('<span class="log-json-null">' + n + '</span>') + term;
      }});

      // 3. Log Levels ([INFO], [WARN], [ERROR], [DEBUG], [SUCCESS], PASS, FAIL)
      s = s.replace(/(\\[(?:INFO|DEBUG|WARN|WARNING|ERROR|FATAL|SUCCESS)\\]|\\b(?:INFO|WARN|WARNING|ERROR|FATAL|DEBUG|SUCCESS|PASS|FAIL|PASSED|FAILED)\\b)/g, (match) => {{
        const clean = match.replace(/[\\[\\]]/g, '').toUpperCase();
        let cls = '';
        if (clean === 'INFO') cls = 'log-lvl-info';
        else if (clean === 'WARN' || clean === 'WARNING') cls = 'log-lvl-warn';
        else if (clean === 'ERROR' || clean === 'FATAL' || clean === 'FAIL' || clean === 'FAILED') cls = 'log-lvl-error';
        else if (clean === 'SUCCESS' || clean === 'PASS' || clean === 'PASSED') cls = 'log-lvl-success';
        else if (clean === 'DEBUG') cls = 'log-lvl-debug';
        return cls ? addTok('<span class="log-lvl ' + cls + '">' + match + '</span>') : match;
      }});

      // 4. Timestamps
      s = s.replace(/\\b(\\d{{4}}-\\d{{2}}-\\d{{2}}[T ]\\d{{2}}:\\d{{2}}:\\d{{2}}(?:\\.\\d+)?(?:Z|[+-]\\d{{2}}:?\\d{{2}})?|\\d{{2}}:\\d{{2}}:\\d{{2}}(?:\\.\\d+)?)\\b/g, (m) => {{
        return addTok('<span class="log-ts">' + m + '</span>');
      }});

      // 5. URLs & Paths
      s = s.replace(/(https?:\\/\\/[^\\s"'<>]+)/g, (m) => {{
        return addTok('<span class="log-path">' + m + '</span>');
      }});
      s = s.replace(/(^|[\\s"'`\\(\\[\\{{])((?:\\/[a-zA-Z0-9_\\.-]+)+|[a-zA-Z0-9_\\.-]+\\/(?:[a-zA-Z0-9_\\.-]+\\/)*[a-zA-Z0-9_\\.-]+\\.(?:py|json|md|yaml|yml|log|sh|txt|html|js|ts)(?::\\d+)?)\\b/g, (m, pfx, p) => {{
        return pfx + addTok('<span class="log-path">' + p + '</span>');
      }});

      // 6. HTTP Methods
      s = s.replace(/\\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\\b/g, (m) => {{
        return addTok('<span class="log-method">' + m + '</span>');
      }});

      // Restore all shielded tokens
      while (s.includes('\x01TOK')) {{
        s = s.replace(/\\x01TOK(\\d+)\\x02/g, (_, id) => tokens[id]);
      }}
      return s;
    }}

    function highlightSearchQuery(htmlText, rawQuery) {{
      if (!rawQuery) return htmlText;
      const escapedQ = escapeLogText(rawQuery).replace(/[-\\[\\]{{}}()*+?.,\\\\^$|#\\s]/g, '\\\\$&');
      try {{
        const regex = new RegExp('(<[^>]+>|&[a-zA-Z0-9#]+;)|(' + escapedQ + ')', 'gi');
        return htmlText.replace(regex, (m, tagOrEntity, matchText) => {{
          if (tagOrEntity) return tagOrEntity;
          return '<mark class="log-search-match">' + matchText + '</mark>';
        }});
      }} catch (_) {{
        return htmlText;
      }}
    }}

    function tryParseJson(text) {{
      if (!text) return null;
      const trimmed = text.trim();
      if ((trimmed.startsWith('{{') && trimmed.endsWith('}}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {{
        try {{
          const obj = JSON.parse(trimmed);
          return {{ type: 'full', data: obj }};
        }} catch (_) {{}}
      }}
      const braceIdx = text.indexOf('{{');
      if (braceIdx >= 0) {{
        const candidate = text.slice(braceIdx).trim();
        if (candidate.endsWith('}}')) {{
          try {{
            const obj = JSON.parse(candidate);
            return {{ type: 'embedded', prefix: text.slice(0, braceIdx), data: obj }};
          }} catch (_) {{}}
        }}
      }}
      return null;
    }}

    function togglePrettyJson() {{
      state.drawerPrettyJson = !state.drawerPrettyJson;
      const btn = document.getElementById('btnTogglePrettyJson');
      if (btn) btn.classList.toggle('active', state.drawerPrettyJson);
      renderDrawerLogs();
    }}

    function renderDrawerLogs() {{
      const inner = document.getElementById('drawerTerminalInner');
      if (!inner) return;

      const q = state.drawerSearchQuery ? state.drawerSearchQuery.toLowerCase() : '';
      const levelFilter = state.drawerLevelFilter || 'all';

      // 1. Calculate live badge counts across all streams
      const countAll = state.drawerLogs.length;
      let countError = 0;
      let countWarn = 0;
      let countStdout = 0;
      let countStderr = 0;

      state.drawerLogs.forEach((l) => {{
        if (l.level === 'error' || l.stream === 'stderr') countError++;
        if (l.level === 'warn') countWarn++;
        if (l.stream === 'stdout') countStdout++;
        if (l.stream === 'stderr') countStderr++;
      }});

      const bAll = document.getElementById('badgeCountAll');
      if (bAll) bAll.innerText = countAll;
      const bErr = document.getElementById('badgeCountError');
      if (bErr) bErr.innerText = countError;
      const bWarn = document.getElementById('badgeCountWarn');
      if (bWarn) bWarn.innerText = countWarn;
      const bOut = document.getElementById('badgeCountStdout');
      if (bOut) bOut.innerText = countStdout;
      const bStderr = document.getElementById('badgeCountStderr');
      if (bStderr) bStderr.innerText = countStderr;

      const pillErr = document.getElementById('filterPillError');
      if (pillErr) pillErr.classList.toggle('has-errs', countError > 0);
      const pillWarn = document.getElementById('filterPillWarn');
      if (pillWarn) pillWarn.classList.toggle('has-warns', countWarn > 0);

      let matchCount = 0;
      let visibleRows = [];

      // 2. Filter and render rows
      state.drawerLogs.forEach((line) => {{
        if (levelFilter === 'error' && line.level !== 'error' && line.stream !== 'stderr') return;
        if (levelFilter === 'warn' && line.level !== 'error' && line.level !== 'warn' && line.stream !== 'stderr') return;
        if (levelFilter === 'stdout' && line.stream !== 'stdout') return;
        if (levelFilter === 'stderr' && line.stream !== 'stderr') return;

        let matchesQuery = true;
        if (q) {{
          matchesQuery = line.text.toLowerCase().includes(q);
          if (matchesQuery) {{
            matchCount++;
          }} else {{
            return;
          }}
        }}

        const isTraceback = line.text.includes('Traceback (most recent call last):') || line.text.startsWith('  File "') || /\\w+Error:/.test(line.text);
        const rowClass = 'log-row' + (line.stream === 'stderr' ? ' is-stderr' : '') + (isTraceback ? ' is-traceback' : '');
        const streamClass = line.stream === 'stderr' ? 'err' : (line.stream === 'sys' ? 'sys' : 'out');
        const streamLabel = line.stream === 'stderr' ? 'ERR' : (line.stream === 'sys' ? 'SYS' : 'OUT');

        // Pretty JSON expansion when enabled
        const jsonMatch = state.drawerPrettyJson ? tryParseJson(line.text) : null;
        if (jsonMatch) {{
          const jsonCardId = 'json_card_' + line.lineNum;
          if (jsonMatch.type === 'full') {{
            const pretty = JSON.stringify(jsonMatch.data, null, 2);
            let pContent = formatLogLineHtml(pretty, line.stream);
            if (q) pContent = highlightSearchQuery(pContent, state.drawerSearchQuery);
            const rawEscaped = escapeLogText(pretty);
            visibleRows.push(
              `<div class="${{rowClass}} is-pretty-json">` +
                `<div class="log-gutter log-gutter-col">` +
                  `<span class="log-line-num">${{line.lineNum}}</span>` +
                  `<span class="stream-pill stream-pill-${{streamClass}}">${{streamLabel}}</span>` +
                `</div>` +
                `<div class="log-text" style="padding: 0 4px;">` +
                  `<div class="json-card">` +
                    `<div class="json-card-header">` +
                      `<span>JSON Payload</span>` +
                      `<button class="json-copy-btn" onclick="copyJsonPayload('${{jsonCardId}}')" title="Copy JSON">` +
                        `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>` +
                        `<span>Copy JSON</span>` +
                      `</button>` +
                    `</div>` +
                    `<div class="json-card-body" id="${{jsonCardId}}" data-raw-json="${{rawEscaped}}">${{pContent}}</div>` +
                  `</div>` +
                `</div>` +
              `</div>`
            );
          }} else if (jsonMatch.type === 'embedded') {{
            let pfxContent = formatLogLineHtml(jsonMatch.prefix, line.stream);
            if (q) pfxContent = highlightSearchQuery(pfxContent, state.drawerSearchQuery);
            visibleRows.push(
              `<div class="${{rowClass}}">` +
                `<div class="log-gutter log-gutter-col">` +
                  `<span class="log-line-num">${{line.lineNum}}</span>` +
                  `<span class="stream-pill stream-pill-${{streamClass}}">${{streamLabel}}</span>` +
                `</div>` +
                `<div class="log-text">${{pfxContent}}</div>` +
              `</div>`
            );
            const pretty = JSON.stringify(jsonMatch.data, null, 2);
            let pContent = formatLogLineHtml(pretty, line.stream);
            if (q) pContent = highlightSearchQuery(pContent, state.drawerSearchQuery);
            const rawEscaped = escapeLogText(pretty);
            visibleRows.push(
              `<div class="${{rowClass}} is-pretty-json">` +
                `<div class="log-gutter log-gutter-col">` +
                  `<span class="log-line-num">·</span>` +
                  `<span class="stream-pill stream-pill-${{streamClass}}">${{streamLabel}}</span>` +
                `</div>` +
                `<div class="log-text" style="padding: 0 4px;">` +
                  `<div class="json-card">` +
                    `<div class="json-card-header">` +
                      `<span>Embedded JSON</span>` +
                      `<button class="json-copy-btn" onclick="copyJsonPayload('${{jsonCardId}}')" title="Copy JSON">` +
                        `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>` +
                        `<span>Copy JSON</span>` +
                      `</button>` +
                    `</div>` +
                    `<div class="json-card-body" id="${{jsonCardId}}" data-raw-json="${{rawEscaped}}">${{pContent}}</div>` +
                  `</div>` +
                `</div>` +
              `</div>`
            );
          }}
        }} else {{
          let contentHtml = formatLogLineHtml(line.text, line.stream);
          if (q) {{
            contentHtml = highlightSearchQuery(contentHtml, state.drawerSearchQuery);
          }}
          visibleRows.push(
            `<div class="${{rowClass}}">` +
              `<div class="log-gutter log-gutter-col">` +
                `<span class="log-line-num">${{line.lineNum}}</span>` +
                `<span class="stream-pill stream-pill-${{streamClass}}">${{streamLabel}}</span>` +
              `</div>` +
              `<div class="log-text">${{contentHtml}}</div>` +
            `</div>`
          );
        }}
      }});

      const matchBadge = document.getElementById('logMatchesCount');
      if (matchBadge) {{
        if (q) {{
          matchBadge.style.display = 'inline-block';
          matchBadge.innerText = matchCount + (matchCount === 1 ? ' match' : ' matches');
        }} else {{
          matchBadge.style.display = 'none';
        }}
      }}

      const clearBtn = document.getElementById('btnLogSearchClear');
      if (clearBtn) {{
        clearBtn.style.display = q ? 'inline-flex' : 'none';
      }}

      const lineCounter = document.getElementById('drawerLineCount');
      if (lineCounter) {{
        lineCounter.innerText = state.drawerLogs.length + ' lines';
      }}

      if (state.drawerLogs.length === 0) {{
        inner.innerHTML = '<div style="padding: 36px 20px; text-align: center; color: var(--text-subtle); font-size: 12px;"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="margin: 0 auto 8px auto; opacity: 0.5; display: block;"><polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/></svg>Waiting for live execution output...</div>';
      }} else if (visibleRows.length === 0) {{
        inner.innerHTML = '<div style="padding: 36px 20px; text-align: center; color: var(--text-subtle); font-size: 12px;"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="margin: 0 auto 8px auto; opacity: 0.5; display: block;"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>No log lines match current filter or search criteria.</div>';
      }} else {{
        inner.innerHTML = visibleRows.join('');
      }}

      if (state.drawerAutoScroll) {{
        const term = document.getElementById('drawerTerminal');
        if (term) {{
          term.scrollTop = term.scrollHeight;
        }}
      }}
    }}

    function scheduleLogRender() {{
      if (state.drawerRenderScheduled) return;
      state.drawerRenderScheduled = true;
      requestAnimationFrame(() => {{
        state.drawerRenderScheduled = false;
        renderDrawerLogs();
      }});
    }}

    function appendDrawerLine(text, stream = 'stdout', timestamp = null) {{
      if (text === undefined || text === null) return;
      const lineNum = state.drawerLogs.length + 1;
      const level = detectLogLevel(text, stream);
      state.drawerLogs.push({{
        lineNum: lineNum,
        text: text,
        stream: stream,
        level: level,
        timestamp: timestamp
      }});
      while (state.drawerLogs.length > MAX_TERMINAL_LINES) {{
        state.drawerLogs.shift();
      }}
    }}

    async function copyDrawerLogs() {{
      const q = state.drawerSearchQuery ? state.drawerSearchQuery.toLowerCase() : '';
      const filter = state.drawerLevelFilter || 'all';
      const linesToCopy = state.drawerLogs.filter(line => {{
        if (filter === 'error' && line.level !== 'error' && line.stream !== 'stderr') return false;
        if (filter === 'warn' && line.level !== 'warn' && line.level !== 'error' && line.stream !== 'stderr') return false;
        if (filter === 'stdout' && line.stream !== 'stdout') return false;
        if (filter === 'stderr' && line.stream !== 'stderr') return false;
        if (q && !line.text.toLowerCase().includes(q)) return false;
        return true;
      }});

      if (!linesToCopy.length) {{
        showToast('No matching logs to copy', 'info');
        return;
      }}
      const visibleText = linesToCopy.map(l => l.text).join('\\n');
      try {{
        await navigator.clipboard.writeText(visibleText);
        const copyLabel = document.getElementById('copyLogsLabel');
        const copyIcon = document.getElementById('copyLogsIcon');
        if (copyLabel) copyLabel.innerText = 'Copied!';
        if (copyIcon) copyIcon.innerHTML = '<polyline points="20 6 9 17 4 12"/>';
        const countMsg = linesToCopy.length === state.drawerLogs.length
          ? `Copied ${{linesToCopy.length}} lines to clipboard`
          : `Copied ${{linesToCopy.length}} filtered lines to clipboard`;
        showToast(countMsg, 'info');
        setTimeout(() => {{
          if (copyLabel) copyLabel.innerText = 'Copy';
          if (copyIcon) copyIcon.innerHTML = '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>';
        }}, 2000);
      }} catch (err) {{
        showToast('Failed to copy: ' + err.message, 'error');
      }}
    }}

    async function copyJsonPayload(jsonId) {{
      const el = document.getElementById(jsonId);
      if (!el) return;
      try {{
        await navigator.clipboard.writeText(el.dataset.rawJson || el.innerText);
        showToast('JSON payload copied to clipboard', 'info');
      }} catch (_) {{}}
    }}

    async function copyPromptCommand() {{
      const cmdEl = document.getElementById('termPromptCmd');
      if (!cmdEl) return;
      const text = cmdEl.innerText.trim();
      if (!text || text === '--') return;
      try {{
        await navigator.clipboard.writeText(text);
        showToast('Executed command copied to clipboard', 'info');
      }} catch (_) {{}}
    }}

    function downloadTaskLogs() {{
      if (!state.drawerLogs || !state.drawerLogs.length) {{
        showToast('No logs to download', 'info');
        return;
      }}
      const lines = state.drawerLogs.map(l => {{
        const ts = l.timestamp ? `[${{l.timestamp}}] ` : '';
        return `${{ts}}[${{l.stream.toUpperCase()}}] ${{l.text}}`;
      }});
      const blob = new Blob([lines.join('\\n')], {{ type: 'text/plain;charset=utf-8' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${{state.activeTaskId || 'task'}}.log`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast('Log file downloaded', 'info');
    }}

    function setLogLevelFilter(filterVal) {{
      state.drawerLevelFilter = filterVal;
      const hiddenInput = document.getElementById('logLevelFilter');
      if (hiddenInput) hiddenInput.value = filterVal;
      ['all', 'error', 'warn', 'stdout', 'stderr'].forEach(k => {{
        const btn = document.getElementById('filterPill' + k.charAt(0).toUpperCase() + k.slice(1));
        if (btn) btn.classList.toggle('active', k === filterVal);
      }});
      renderDrawerLogs();
    }}

    function onLogLevelFilterChange(val) {{
      setLogLevelFilter(val);
    }}

    function clearLogSearch() {{
      state.drawerSearchQuery = '';
      const input = document.getElementById('logSearchInput');
      if (input) input.value = '';
      const clearBtn = document.getElementById('btnLogSearchClear');
      if (clearBtn) clearBtn.style.display = 'none';
      renderDrawerLogs();
    }}

    function toggleLogDensity() {{
      const term = document.getElementById('drawerTerminal');
      const label = document.getElementById('densityBtnLabel');
      if (!term) return;
      const isCompact = term.classList.contains('density-compact');
      if (isCompact) {{
        term.classList.remove('density-compact');
        term.classList.add('density-comfortable');
        if (label) label.innerText = 'Compact';
        localStorage.setItem('wh_log_density', 'comfortable');
      }} else {{
        term.classList.remove('density-comfortable');
        term.classList.add('density-compact');
        if (label) label.innerText = 'Comfortable';
        localStorage.setItem('wh_log_density', 'compact');
      }}
    }}

    function toggleLogWrap() {{
      state.drawerWrapLines = !state.drawerWrapLines;
      const term = document.getElementById('drawerTerminal');
      const btn = document.getElementById('btnToggleWrap');
      if (term) {{
        if (state.drawerWrapLines) {{
          term.classList.add('wrap-mode');
        }} else {{
          term.classList.remove('wrap-mode');
        }}
      }}
      if (btn) {{
        btn.classList.toggle('active', state.drawerWrapLines);
      }}
    }}

    function toggleLogAutoScroll() {{
      state.drawerAutoScroll = !state.drawerAutoScroll;
      const btn = document.getElementById('btnToggleAutoScroll');
      if (btn) {{
        btn.classList.toggle('active', state.drawerAutoScroll);
      }}
      if (state.drawerAutoScroll) {{
        const term = document.getElementById('drawerTerminal');
        if (term) term.scrollTop = term.scrollHeight;
      }}
    }}

    function handleTerminalScroll() {{
      const term = document.getElementById('drawerTerminal');
      if (!term) return;
      const isAtBottom = term.scrollHeight - term.scrollTop - term.clientHeight <= 30;
      const btn = document.getElementById('btnToggleAutoScroll');
      if (!isAtBottom && state.drawerAutoScroll) {{
        state.drawerAutoScroll = false;
        if (btn) btn.classList.remove('active');
      }} else if (isAtBottom && !state.drawerAutoScroll) {{
        state.drawerAutoScroll = true;
        if (btn) btn.classList.add('active');
      }}
    }}

    function onLogSearchChange(val) {{
      state.drawerSearchQuery = (val || '').trim();
      const clearBtn = document.getElementById('btnLogSearchClear');
      if (clearBtn) clearBtn.style.display = state.drawerSearchQuery ? 'inline-flex' : 'none';
      renderDrawerLogs();
    }}

    function formatTaskDuration(startStr, endStr) {{
      if (!startStr) return '--';
      try {{
        const s = new Date(startStr).getTime();
        const e = endStr ? new Date(endStr).getTime() : Date.now();
        const diff = e - s;
        if (isNaN(diff) || diff < 0) return '--';
        if (diff < 1000) return diff + 'ms';
        if (diff < 60000) return (diff / 1000).toFixed(2) + 's';
        const mins = Math.floor(diff / 60000);
        const secs = ((diff % 60000) / 1000).toFixed(1);
        return mins + 'm ' + secs + 's';
      }} catch (_) {{
        return '--';
      }}
    }}

    function updateDrawerDurationBadge(durStr) {{
      const durBadge = document.getElementById('drawerDurationBadge');
      if (!durBadge) return;
      if (durStr && durStr !== '--') {{
        durBadge.style.display = 'inline-flex';
        durBadge.className = 'badge badge-duration';
        durBadge.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><span>' + durStr + '</span>';
      }} else {{
        durBadge.style.display = 'none';
      }}
    }}

    function renderExitBadge(code, status) {{
      const badge = document.getElementById('drawerExitBadge');
      if (!badge) return;
      if (code === null || code === undefined) {{
        if (status === 'running') {{
          badge.style.display = 'inline-flex';
          badge.className = 'badge badge-running';
          badge.innerHTML = '<span class="live-pulse" style="width:5px;height:5px;background:#818cf8;"></span><span>running</span>';
        }} else {{
          badge.style.display = 'none';
        }}
        return;
      }}
      badge.style.display = 'inline-flex';
      const numCode = parseInt(code, 10);
      if (numCode === 0) {{
        badge.className = 'badge badge-succeeded';
        badge.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg><span>exit 0</span>';
      }} else if (numCode < 0) {{
        badge.className = 'badge badge-warning';
        badge.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>SIG (' + numCode + ')</span>';
      }} else {{
        badge.className = 'badge badge-failed';
        badge.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg><span>exit ' + numCode + '</span>';
      }}
    }}

    function renderDrawerAgentActivity(act) {{
      const agentCard = document.getElementById('drawerAgentActivityCard');
      const agentPills = document.getElementById('drawerAgentPills');
      const agentActions = document.getElementById('drawerAgentActions');
      const agentBody = document.getElementById('drawerAgentActivityBody');
      if (!agentCard || !agentPills || !agentBody) return;

      if (!act) {{
        agentCard.style.display = 'none';
        return;
      }}

      agentCard.style.display = 'flex';
      agentPills.innerHTML = '';
      if (agentActions) agentActions.innerHTML = '';
      let bodyHtml = '';

      if (act.signal) {{
        const sig = act.signal;
        const v = (sig.verdict || 'unknown').toLowerCase();
        agentPills.innerHTML += `<span class="verdict-badge verdict-${{v}}">${{escapeHtml(v.toUpperCase())}}</span>`;
        const confScore = sig.confidence_score !== undefined && sig.confidence_score !== null
          ? sig.confidence_score
          : (sig.result && sig.result.confidence_score);
        if (confScore !== undefined && confScore !== null) {{
          const confPct = Math.round((confScore <= 1 ? confScore * 100 : confScore));
          agentPills.innerHTML += `<span class="badge" style="background:rgba(99,102,241,0.2);color:#a5b4fc;">${{confPct}}% Conf</span>`;
        }}
        const isApplied = sig.applied !== undefined && sig.applied !== null
          ? sig.applied
          : (sig.result && sig.result.applied);
        if (isApplied) {{
          agentPills.innerHTML += `<span class="badge badge-succeeded">Applied</span>`;
        }}

        if (agentActions) {{
          agentActions.innerHTML += `
            <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="inspectSignal('${{sig.signal_id}}')" title="Quick inspect signal diffs and full metadata">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
              <span>Inspect</span>
            </button>
            <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="copySignalJson('${{sig.signal_id}}')" title="One-click copy signal JSON">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
              <span>Copy</span>
            </button>
          `;
        }}

        const targetUrl = sig.target_page_url || (sig.result && sig.result.target_page_url);
        bodyHtml += `
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;">
            <div>
              <span style="color: var(--text-muted); font-size: 11px;">Target Contact: </span>
              <strong style="color: #ffffff;">${{escapeHtml(sig.target_name || '--')}}</strong>
              <span style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); margin-left: 6px;">(${{escapeHtml(sig.signal_id || '')}})</span>
            </div>
            ${{targetUrl ? `
              <a href="${{escapeHtml(targetUrl)}}" target="_blank" class="notion-btn">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" x2="21" y1="14" y2="3"/></svg>
                <span>Open in Notion</span>
              </a>
            ` : ''}}
          </div>
        `;

        const explanation = (sig.result && sig.result.explanation) || sig.explanation;
        if (explanation) {{
          bodyHtml += `<div style="font-size: 11px; color: #cbd5e1; background: #070a12; padding: 8px 10px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); line-height: 1.4;">${{escapeHtml(explanation)}}</div>`;
        }}

        const diffs = (sig.result && sig.result.diffs) || sig.diffs;
        const hasDiffs = Array.isArray(diffs) ? diffs.length > 0 : (diffs && typeof diffs === 'object' && Object.keys(diffs).length > 0);
        if (hasDiffs) {{
          bodyHtml += `
            <div style="font-size: 11px; font-family: var(--font-mono); background: #070a12; padding: 6px 10px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); color: #94a3b8;">
              <div style="color: var(--accent-blue); font-weight: 600; margin-bottom: 4px; font-size: 10px; text-transform: uppercase;">Proposed Property Diffs</div>
              <pre style="margin:0; white-space:pre-wrap; font-size: 10px; color: #cbd5e1;">${{escapeHtml(JSON.stringify(diffs, null, 2))}}</pre>
            </div>
          `;
        }}
      }}

      if (act.pulse) {{
        agentPills.innerHTML += `<span class="badge" style="background:rgba(59,130,246,0.2);color:#93c5fd;">Pulse Queued</span>`;
        if (!act.signal && agentActions) {{
          agentActions.innerHTML += `
            <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="copyText('${{escapeJsString(act.pulse.prompt || '')}}')" title="Copy pulse prompt">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
              <span>Copy Prompt</span>
            </button>
          `;
        }}
        bodyHtml += `
          <div style="font-size: 11px; color: #94a3b8;">
            <span style="color: var(--text-muted);">Sidecar Pulse Event: </span>
            <code style="color: #93c5fd;">${{escapeHtml(act.pulse.file_name)}}</code>
          </div>
        `;
        if (act.pulse.prompt) {{
          bodyHtml += `
            <div style="font-size: 11px; font-family: var(--font-mono); background: #070a12; padding: 6px 10px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); color: #cbd5e1; max-height: 80px; overflow-y: auto;">
              ${{escapeHtml(act.pulse.prompt)}}
            </div>
          `;
        }}
      }}

      if (act.prompt_payload && !act.pulse) {{
        agentPills.innerHTML += `<span class="badge" style="background:rgba(168,85,247,0.2);color:#c084fc;">Agent Prompt</span>`;
        const promptText = typeof act.prompt_payload === 'string' ? act.prompt_payload : JSON.stringify(act.prompt_payload, null, 2);
        if (!act.signal && agentActions) {{
          agentActions.innerHTML += `
            <button class="btn btn-secondary" style="padding: 2px 8px; font-size: 11px;" onclick="copyText('${{escapeJsString(promptText)}}')" title="Copy prompt payload">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
              <span>Copy Prompt</span>
            </button>
          `;
        }}
        bodyHtml += `
          <div style="font-size: 11px; font-family: var(--font-mono); background: #070a12; padding: 6px 10px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.05); color: #cbd5e1; max-height: 80px; overflow-y: auto;">
            ${{escapeHtml(promptText)}}
          </div>
        `;
      }}

      agentBody.innerHTML = bodyHtml;
    }}

    async function openDrawer(taskId) {{
      state.activeTaskId = taskId;
      state.drawerLogs = [];
      state.drawerSearchQuery = '';
      state.drawerLevelFilter = 'all';
      state.drawerAutoScroll = true;

      if (state.drawerDurationTimer) {{
        clearInterval(state.drawerDurationTimer);
        state.drawerDurationTimer = null;
      }}

      const searchInput = document.getElementById('logSearchInput');
      if (searchInput) searchInput.value = '';
      const clearBtn = document.getElementById('btnLogSearchClear');
      if (clearBtn) clearBtn.style.display = 'none';

      setLogLevelFilter('all');

      // Initialize Prompt Banner
      const promptCmd = document.getElementById('termPromptCmd');
      if (promptCmd) promptCmd.innerText = '--';
      const promptTime = document.getElementById('termMetaTime');
      if (promptTime) promptTime.innerText = '--:--:--';
      const promptDur = document.getElementById('termMetaDuration');
      if (promptDur) promptDur.innerText = '--';
      const promptExit = document.getElementById('termMetaExit');
      if (promptExit) promptExit.innerText = '--';

      // Apply stored density mode
      const storedDensity = localStorage.getItem('wh_log_density') || 'comfortable';
      const term = document.getElementById('drawerTerminal');
      const densityLabel = document.getElementById('densityBtnLabel');
      if (term) {{
        term.classList.toggle('density-compact', storedDensity === 'compact');
        term.classList.toggle('density-comfortable', storedDensity !== 'compact');
      }}
      if (densityLabel) {{
        densityLabel.innerText = storedDensity === 'compact' ? 'Comfortable' : 'Compact';
      }}

      document.getElementById('drawerTaskId').innerText = taskId;
      document.getElementById('drawerOverlay').classList.add('open');
      document.getElementById('taskDrawer').classList.add('open');

      const liveIndicator = document.getElementById('drawerLiveIndicator');
      if (liveIndicator) liveIndicator.style.display = 'none';

      renderDrawerLogs();

      try {{
        const resp = await fetch('/tasks/' + taskId);
        if (resp.ok) {{
          const task = await resp.json();
          state.activeTask = task;
          document.getElementById('drawerTaskStatus').innerText = task.status;
          document.getElementById('drawerTaskStatus').className = 'badge badge-' + task.status;
          document.getElementById('drawerMetaSource').innerText = task.source || 'default';
          document.getElementById('drawerMetaAction').innerText = task.action_type || 'cli';
          document.getElementById('drawerMetaCreated').innerText = task.created_at || '--';
          document.getElementById('drawerMetaExit').innerText = task.exit_code !== null && task.exit_code !== undefined ? task.exit_code : '--';
          
          const durStr = formatTaskDuration(task.created_at, task.completed_at);
          document.getElementById('drawerMetaDuration').innerText = durStr;
          updateDrawerDurationBadge(durStr);

          // Update terminal prompt banner
          const cmdStr = task.command || (task.action_params ? JSON.stringify(task.action_params) : '--');
          if (promptCmd) promptCmd.innerText = cmdStr;
          if (promptTime) promptTime.innerText = task.created_at ? task.created_at.slice(11, 19) : '--:--:--';
          if (promptDur) promptDur.innerText = durStr;
          if (promptExit) {{
            promptExit.innerText = task.exit_code !== null && task.exit_code !== undefined
              ? 'exit ' + task.exit_code
              : (task.status === 'running' ? 'running' : task.status);
          }}

          if (task.status === 'running') {{
            state.drawerDurationTimer = setInterval(() => {{
              if (state.activeTaskId !== taskId) {{
                clearInterval(state.drawerDurationTimer);
                state.drawerDurationTimer = null;
                return;
              }}
              const liveDur = formatTaskDuration(task.created_at, null);
              document.getElementById('drawerMetaDuration').innerText = liveDur;
              if (promptDur) promptDur.innerText = liveDur;
              updateDrawerDurationBadge(liveDur);
            }}, 1000);
          }}

          const cmdEl = document.getElementById('drawerMetaCommand');
          if (cmdEl) {{
            cmdEl.innerText = cmdStr;
          }}

          renderExitBadge(task.exit_code, task.status);

          // Render Agent Activity & Signal Card in Drawer
          renderDrawerAgentActivity(task.agent_activity);

          // AUTHORITATIVE CHRONOLOGICAL LOG INGESTION
          state.drawerLogs = [];
          if (task.logs && Array.isArray(task.logs) && task.logs.length > 0) {{
            task.logs.forEach(item => {{
              appendDrawerLine(item.line !== undefined ? item.line : (item.text || ''), item.stream || 'stdout', item.timestamp);
            }});
          }} else {{
            if (task.stdout) {{
              task.stdout.split('\\n').forEach(line => {{
                if (line.length) appendDrawerLine(line, 'stdout');
              }});
            }}
            if (task.stderr) {{
              task.stderr.split('\\n').forEach(line => {{
                if (line.length) appendDrawerLine(line, 'stderr');
              }});
            }}
          }}
          renderDrawerLogs();
        }}
      }} catch (err) {{
        appendDrawerLine('Error loading task: ' + err.message, 'stderr');
        renderDrawerLogs();
      }}

      // Subscribe to live task SSE stream
      if (state.drawerEventSource) {{
        state.drawerEventSource.close();
      }}
      try {{
        const taskSse = new EventSource('/tasks/' + taskId + '/stream');
        state.drawerEventSource = taskSse;
        if (liveIndicator) liveIndicator.style.display = 'inline-flex';

        const handleChunk = (e) => {{
          try {{
            const chunk = JSON.parse(e.data);
            const streamClass = chunk.stream === 'stderr' ? 'stderr' : 'stdout';
            const chunkLines = (chunk.line || chunk.chunk || e.data).split('\\n');
            chunkLines.forEach(l => appendDrawerLine(l, streamClass, chunk.timestamp));
            scheduleLogRender();
            if (chunk.status) {{
              document.getElementById('drawerTaskStatus').innerText = chunk.status;
              document.getElementById('drawerTaskStatus').className = 'badge badge-' + chunk.status;
              if (promptExit && chunk.status === 'running') promptExit.innerText = 'running';
            }}
            if (chunk.exit_code !== undefined) {{
              document.getElementById('drawerMetaExit').innerText = chunk.exit_code;
              renderExitBadge(chunk.exit_code, chunk.status);
              if (promptExit) promptExit.innerText = 'exit ' + chunk.exit_code;
            }}
          }} catch (_) {{
            appendDrawerLine(e.data, 'stdout');
            scheduleLogRender();
          }}
        }};
        taskSse.addEventListener('log', handleChunk);
        taskSse.addEventListener('message', handleChunk);
        taskSse.addEventListener('status_changed', (e) => {{
          handleChunk(e);
          refreshTasksDebounced(200);
        }});
        taskSse.addEventListener('completed', async (e) => {{
          handleChunk(e);
          if (liveIndicator) liveIndicator.style.display = 'none';
          if (state.drawerDurationTimer) {{
            clearInterval(state.drawerDurationTimer);
            state.drawerDurationTimer = null;
          }}
          // Re-pull authoritative state from SSOT upon task completion
          try {{
            const resp = await fetch('/tasks/' + taskId);
            if (resp.ok) {{
              const updatedTask = await resp.json();
              if (state.activeTaskId === taskId) {{
                if (updatedTask.exit_code !== undefined && updatedTask.exit_code !== null) {{
                  renderExitBadge(updatedTask.exit_code, updatedTask.status);
                }}
                if (promptExit && updatedTask.exit_code !== undefined && updatedTask.exit_code !== null) {{
                  promptExit.innerText = 'exit ' + updatedTask.exit_code;
                }}
                renderDrawerAgentActivity(updatedTask.agent_activity);
              }}
            }}
          }} catch (_) {{}}
          refreshTasksDebounced(100);
        }});
        taskSse.onmessage = handleChunk;
        taskSse.onerror = () => {{
          if (liveIndicator) liveIndicator.style.display = 'none';
        }};
      }} catch (_) {{}}
    }}

    function closeDrawer() {{
      document.getElementById('drawerOverlay').classList.remove('open');
      document.getElementById('taskDrawer').classList.remove('open');
      const agentCard = document.getElementById('drawerAgentActivityCard');
      if (agentCard) agentCard.style.display = 'none';
      if (state.drawerDurationTimer) {{
        clearInterval(state.drawerDurationTimer);
        state.drawerDurationTimer = null;
      }}
      if (state.drawerEventSource) {{
        state.drawerEventSource.close();
        state.drawerEventSource = null;
      }}
      const liveIndicator = document.getElementById('drawerLiveIndicator');
      if (liveIndicator) liveIndicator.style.display = 'none';
      state.activeTaskId = null;
      state.activeTask = null;
      state.drawerLogs = [];
    }}

    // Global keyboard shortcuts (Esc to close drawer, Cmd/Ctrl+F to search logs when drawer is open)
    window.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') {{
        if (document.getElementById('taskDrawer').classList.contains('open')) {{
          closeDrawer();
        }}
        if (document.getElementById('testModalOverlay').classList.contains('open')) {{
          closeTestModal();
        }}
        if (document.getElementById('signalModalOverlay') && document.getElementById('signalModalOverlay').classList.contains('open')) {{
          closeSignalModal();
        }}
        if (document.getElementById('resuscitationModalOverlay') && document.getElementById('resuscitationModalOverlay').classList.contains('open')) {{
          closeResuscitationModal();
        }}
      }}
      if ((e.metaKey || e.ctrlKey) && e.key === 'f') {{
        if (document.getElementById('taskDrawer').classList.contains('open')) {{
          e.preventDefault();
          const searchInput = document.getElementById('logSearchInput');
          if (searchInput) {{
            searchInput.focus();
            searchInput.select();
          }}
        }}
      }}
    }});

    // Re-run Action (Unidirectional: POST -> Re-read SSOT)
    async function rerunTask(taskId) {{
      try {{
        showToast('Re-running task ' + taskId + '...', 'info');
        const resp = await fetch('/tasks/' + taskId + '/rerun', {{ method: 'POST' }});
        if (resp.ok) {{
          showToast('Task re-enqueued successfully', 'info');
          if (state.activeTaskId === taskId) {{
            openDrawer(taskId);
          }}
          await refreshTasksAuthoritative();
        }} else {{
          showToast('Failed to re-run task', 'error');
        }}
      }} catch (err) {{
        showToast('Re-run error: ' + err.message, 'error');
      }}
    }}

    function rerunCurrentDrawerTask() {{
      if (state.activeTaskId) {{
        rerunTask(state.activeTaskId);
      }}
    }}

    // Sweep Unprocessed
    async function triggerSweep() {{
      try {{
        showToast('Sweeping unprocessed tasks & recovering state...', 'info');
        const resp = await fetch('/tasks/sweep', {{ method: 'POST' }});
        if (resp.ok) {{
          const res = await resp.json();
          showToast(`Sweep complete: ${{res.total_queued || 0}} tasks enqueued`, 'info');
          await refreshTasksAuthoritative();
        }}
      }} catch (err) {{
        showToast('Sweep error: ' + err.message, 'error');
      }}
    }}

    // Test Simulator Modal
    function openTestModal() {{
      handlePresetChange(document.getElementById('modalSourceSelect').value);
      document.getElementById('testModalOverlay').classList.add('open');
    }}

    function closeTestModal() {{
      document.getElementById('testModalOverlay').classList.remove('open');
    }}

    function handlePresetChange(presetKey) {{
      const preset = PRESETS[presetKey] || PRESETS.agent_signal;
      document.getElementById('modalActionType').value = preset.action_type;
      document.getElementById('modalPayload').value = preset.payload;
    }}

    async function submitTestWebhook() {{
      const source = document.getElementById('modalSourceSelect').value;
      const payloadStr = document.getElementById('modalPayload').value;

      let payloadObj;
      try {{
        payloadObj = JSON.parse(payloadStr);
      }} catch (e) {{
        alert('Invalid JSON in payload field: ' + e.message);
        return;
      }}

      try {{
        closeTestModal();
        showToast('Dispatching webhook to /webhook/' + source + '...', 'info');
        const resp = await fetch('/webhook/' + source, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payloadObj)
        }});

        if (resp.status === 202 || resp.status === 200) {{
          const res = await resp.json();
          showToast('Webhook accepted! Task ID: ' + res.task_id, 'info');
          // Wait briefly for SQLite write, then re-pull authoritative SSOT
          setTimeout(refreshTasksAuthoritative, 150);
        }} else {{
          const err = await resp.json().catch(() => ({{ error: resp.statusText }}));
          showToast('Webhook rejected: ' + (err.error || resp.status), 'error');
        }}
      }} catch (err) {{
        showToast('Ingress error: ' + err.message, 'error');
      }}
    }}

    function toggleMobileSidebar() {{
      document.getElementById('appSidebar').classList.toggle('mobile-open');
    }}

    // Bootstrap
    window.addEventListener('DOMContentLoaded', () => {{
      syncFilterUI();
      refreshTasksAuthoritative();
      setupGlobalEventSource();
      loadWatchdogStatus();
      loadQuota();
      // Periodic fallback polling every 10s if SSE reconnects
      setInterval(refreshTasksAuthoritative, 10000);
      setInterval(loadWatchdogStatus, 15000);
      setInterval(() => {{
        if (state.currentMainView === 'quota') {{
          loadQuota(false);
        }}
      }}, 30000);
    }});
  </script>
</body>
</html>
"""

