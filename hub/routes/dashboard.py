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
      width: 600px;
      max-width: 90vw;
      background: var(--bg-surface);
      border-left: 1px solid var(--border-color);
      z-index: 60;
      display: flex;
      flex-direction: column;
      transform: translateX(100%);
      transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      box-shadow: -10px 0 30px rgba(0, 0, 0, 0.5);
    }}
    .drawer.open {{
      transform: translateX(0);
    }}
    .drawer-header {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .drawer-title {{
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .drawer-close {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      cursor: pointer;
      padding: 4px;
      border-radius: 4px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .drawer-close:hover {{
      color: var(--text-main);
      background: var(--bg-card);
    }}
    .drawer-body {{
      flex: 1;
      overflow-y: auto;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .terminal-window {{
      background: #06090f;
      border: 1px solid #1e293b;
      border-radius: 6px;
      padding: 14px;
      font-family: var(--font-mono);
      font-size: 12px;
      color: #e2e8f0;
      min-height: 220px;
      max-height: 420px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
      line-height: 1.45;
    }}
    .terminal-line-stdout {{ color: #cbd5e1; }}
    .terminal-line-stderr {{ color: #f87171; }}
    .terminal-line-system {{ color: #60a5fa; }}
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
    .topbar-menu-btn {{
      display: none;
      background: transparent;
      border: none;
      color: var(--text-main);
      cursor: pointer;
      padding: 4px;
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
        </div>

        <!-- Navigation Sections -->
        <div>
          <div class="sidebar-section-title">Observable Activities</div>
          <ul class="nav-list">
            <li class="nav-item active" data-filter="all" onclick="setCategoryFilter('all', this)">
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
        <!-- Metric Cards -->
        <div class="metric-grid">
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
            <div class="table-title">Activity Feed & Execution Registry</div>
            <div style="font-size: 12px; color: var(--text-muted);" id="tableSubtitle">Showing latest tasks</div>
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
    </main>
  </div>

  <!-- Slide-out Log Drawer -->
  <div class="drawer-overlay" id="drawerOverlay" onclick="closeDrawer()"></div>
  <aside class="drawer" id="taskDrawer">
    <div class="drawer-header">
      <div class="drawer-title">
        <span id="drawerTaskId">tsk_...</span>
        <span class="badge" id="drawerTaskStatus">queued</span>
      </div>
      <button class="drawer-close" onclick="closeDrawer()">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="drawer-body">
      <div>
        <div class="sidebar-section-title">Task Parameters</div>
        <div class="telemetry-card" id="drawerMetaCard">
          <div class="telemetry-row"><span class="telemetry-label">Source</span><span class="telemetry-value" id="drawerMetaSource">--</span></div>
          <div class="telemetry-row"><span class="telemetry-label">Action</span><span class="telemetry-value" id="drawerMetaAction">--</span></div>
          <div class="telemetry-row"><span class="telemetry-label">Created At</span><span class="telemetry-value" id="drawerMetaCreated">--</span></div>
          <div class="telemetry-row"><span class="telemetry-label">Exit Code</span><span class="telemetry-value" id="drawerMetaExit">--</span></div>
        </div>
      </div>

      <div style="flex: 1; display: flex; flex-direction: column;">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
          <div class="sidebar-section-title" style="margin-bottom: 0;">Live Output & Execution Logs</div>
          <button class="btn btn-secondary" style="font-size: 11px; padding: 2px 8px;" onclick="rerunCurrentDrawerTask()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            <span>Re-run Task</span>
          </button>
        </div>
        <div class="terminal-window" id="drawerTerminal">Connecting to live SSE stream...</div>
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

  <!-- Toast notifications -->
  <div class="toast-container" id="toastContainer"></div>

  <!-- Type-safe Client Logic adhering to SSOT & Unidirectional Data Flow -->
  <script>
    // State management: SSOT - local state is strictly derived from API responses
    const state = {{
      tasks: [],
      categoryFilter: 'all',
      searchQuery: '',
      activeTaskId: null,
      drawerEventSource: null,
      globalEventSource: null,
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
          url += '&source=uptime-kuma';
        }} else if (state.categoryFilter === 'cli') {{
          url += '&action_type=cli';
        }} else if (state.categoryFilter === 'failed') {{
          url += '&status=failed';
        }}

        if (state.searchQuery.trim()) {{
          url += '&q=' + encodeURIComponent(state.searchQuery.trim());
        }}

        const [tasksResp, summaryResp, healthResp] = await Promise.all([
          fetch(url),
          fetch('/tasks/summary'),
          fetch('/healthz')
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
      }} catch (err) {{
        console.error('Failed to pull authoritative state:', err);
      }}
    }}

    function renderSummary(summary) {{
      document.getElementById('statTotalTasks').innerText = summary.total_tasks || 0;
      document.getElementById('statTotalEvents').innerText = summary.total_events || 0;
      const byStatus = summary.by_status || {{}};
      document.getElementById('statSucceeded').innerText = byStatus.succeeded || 0;
      document.getElementById('statRunning').innerText = (byStatus.running || 0) + (byStatus.queued || 0);

      document.getElementById('countAll').innerText = summary.total_tasks || 0;
      document.getElementById('countAgent').innerText = (summary.by_action || {{}}).agent_signal || 0;
      document.getElementById('countContact').innerText = (summary.by_source || {{}})['contact-review'] || 0;
      document.getElementById('countKuma').innerText = (summary.by_source || {{}})['uptime-kuma'] || 0;
      document.getElementById('countCli').innerText = (summary.by_action || {{}}).cli || 0;
      document.getElementById('countFailed').innerText = (byStatus.failed || 0) + (byStatus.timed_out || 0);
    }}

    function renderTelemetry(health) {{
      const memMb = health.memory_rss_mb || 0;
      const budgetMb = (health.system && health.system.memory_budget_mb) || 30;
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
      if (!state.tasks || state.tasks.length === 0) {{
        tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 40px;">No observable activities matching current filter.</td></tr>`;
        return;
      }}

      tbody.innerHTML = state.tasks.map(t => {{
        const statusBadge = `<span class="badge badge-${{t.status}}">${{t.status}}</span>`;
        const sourceTag = `<span class="source-tag">${{escapeHtml(t.source || 'default')}}</span>`;
        const actionType = escapeHtml(t.action_type || 'cli');
        const target = escapeHtml(t.target_action || t.command || (t.action_params_json ? t.action_params_json.substring(0, 40) + '...' : '-'));
        const created = escapeHtml(t.created_at || '').replace('T', ' ').substring(0, 19);

        return `
          <tr onclick="openDrawer('${{t.task_id}}')">
            <td>${{statusBadge}}</td>
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

    function escapeHtml(str) {{
      if (!str) return '';
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }}

    function setCategoryFilter(filter, el) {{
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
        refreshTasksAuthoritative();
      }}, 250);
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
      sse.addEventListener('status_change', (e) => {{
        refreshTasksAuthoritative();
      }});

      sse.addEventListener('task_created', (e) => {{
        refreshTasksAuthoritative();
      }});

      sse.addEventListener('sweeper_run', (e) => {{
        refreshTasksAuthoritative();
        showToast('Auto-picker sweeper executed recovery pass', 'info');
      }});
    }}

    // Task Drawer & Live Stream Logs
    async function openDrawer(taskId) {{
      state.activeTaskId = taskId;
      document.getElementById('drawerTaskId').innerText = taskId;
      document.getElementById('drawerOverlay').classList.add('open');
      document.getElementById('taskDrawer').classList.add('open');

      const terminal = document.getElementById('drawerTerminal');
      terminal.innerHTML = '<span class="terminal-line-system">Fetching task details & logs...</span>\\n';

      try {{
        const resp = await fetch('/tasks/' + taskId);
        if (resp.ok) {{
          const task = await resp.json();
          document.getElementById('drawerTaskStatus').innerText = task.status;
          document.getElementById('drawerTaskStatus').className = 'badge badge-' + task.status;
          document.getElementById('drawerMetaSource').innerText = task.source || 'default';
          document.getElementById('drawerMetaAction').innerText = task.action_type || 'cli';
          document.getElementById('drawerMetaCreated').innerText = task.created_at || '--';
          document.getElementById('drawerMetaExit').innerText = task.exit_code !== null && task.exit_code !== undefined ? task.exit_code : '--';

          terminal.innerHTML = '';
          if (task.stdout) {{
            task.stdout.split('\\n').forEach(line => {{
              terminal.innerHTML += `<div class="terminal-line-stdout">${{escapeHtml(line)}}</div>`;
            }});
          }}
          if (task.stderr) {{
            task.stderr.split('\\n').forEach(line => {{
              terminal.innerHTML += `<div class="terminal-line-stderr">${{escapeHtml(line)}}</div>`;
            }});
          }}
          if (!task.stdout && !task.stderr) {{
            terminal.innerHTML = '<span class="terminal-line-system">No output logged yet.</span>\\n';
          }}
          terminal.scrollTop = terminal.scrollHeight;
        }}
      }} catch (err) {{
        terminal.innerHTML = `<span class="terminal-line-stderr">Error loading task: ${{escapeHtml(err.message)}}</span>`;
      }}

      // Subscribe to individual task stream if active
      if (state.drawerEventSource) {{
        state.drawerEventSource.close();
      }}
      try {{
        const taskSse = new EventSource('/tasks/' + taskId + '/stream');
        state.drawerEventSource = taskSse;
        taskSse.onmessage = (e) => {{
          try {{
            const chunk = JSON.parse(e.data);
            const lineClass = chunk.stream === 'stderr' ? 'terminal-line-stderr' : 'terminal-line-stdout';
            terminal.innerHTML += `<div class="${{lineClass}}">${{escapeHtml(chunk.line || chunk.chunk || e.data)}}</div>`;
            terminal.scrollTop = terminal.scrollHeight;
          }} catch (_) {{
            terminal.innerHTML += `<div class="terminal-line-stdout">${{escapeHtml(e.data)}}</div>`;
            terminal.scrollTop = terminal.scrollHeight;
          }}
        }};
      }} catch (_) {{}}
    }}

    function closeDrawer() {{
      document.getElementById('drawerOverlay').classList.remove('open');
      document.getElementById('taskDrawer').classList.remove('open');
      if (state.drawerEventSource) {{
        state.drawerEventSource.close();
        state.drawerEventSource = null;
      }}
      state.activeTaskId = null;
    }}

    // Re-run Action (Unidirectional: POST -> Re-read SSOT)
    async function rerunTask(taskId) {{
      try {{
        showToast('Re-running task ' + taskId + '...', 'info');
        const resp = await fetch('/tasks/' + taskId + '/rerun', {{ method: 'POST' }});
        if (resp.ok) {{
          showToast('Task re-enqueued successfully', 'info');
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
      refreshTasksAuthoritative();
      setupGlobalEventSource();
      // Periodic fallback polling every 10s if SSE reconnects
      setInterval(refreshTasksAuthoritative, 10000);
    }});
  </script>
</body>
</html>
"""


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
        html = render_dashboard_html(config, db)
        return HTTPResponse.text(html, status_code=200, content_type="text/html; charset=utf-8")

    server.add_route("GET", "/dashboard", handle_dashboard)
    server.add_route("GET", "/ui", handle_dashboard)
