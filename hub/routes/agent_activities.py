"""
Antigravity Webhook Hub — Agent Activities & Sentinel Observability Endpoints
Provides high-performance, lazy-loaded REST endpoints for observing:
1. Sentinel AI Agent runs (cadence-driven, prompts, autonomous tool steps, reports).
2. Emitted agent signals (Notion CRM contact reviews, verdicts, confidence scores, target URLs).
3. Antigravity sidebar pulse queue events (prompts, execution metadata).
4. Task-level agent activity enrichment for the Log Drawer.

Adheres strictly to:
- Memory Budget: Zero-caching lazy endpoints guaranteeing gateway process RSS < 30.0 MB.
- Single Source of Truth (SSOT): Authoritative disk/file projections.
- Clean JSON responses.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from hub.config import AppConfig
from hub.memory import apply_memory_pressure_relief, get_memory_rss_mb
from hub.models import HTTPRequest, HTTPResponse
from hub.server import AsyncHTTPServer

logger = logging.getLogger("hub.routes.agent_activities")


def _get_sidecar_events_dir(config: Optional[AppConfig] = None) -> Path:
    """Resolve the directory containing Antigravity sidebar pulse events."""
    if config and hasattr(config, "observability") and getattr(config.observability, "sidecar_data_dir", None):
        base_dir = Path(config.observability.sidecar_data_dir)
        slug = getattr(config.observability, "sidecar_slug", "webhook-hub-sentinel") or "webhook-hub-sentinel"
        return base_dir / slug / "events"
    return Path.home() / ".gemini" / "antigravity" / "sidecar_data" / "webhook-hub-sentinel" / "events"


def _get_signals_dir(config: Optional[AppConfig] = None) -> Path:
    """Resolve the directory containing emitted agent signals."""
    # Check current project directory first
    cwd_signals = Path.cwd() / ".agents" / "signals"
    if cwd_signals.exists():
        return cwd_signals
    # Check default known repository path
    default_path = Path("/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.agents/signals")
    if default_path.exists():
        return default_path
    if config and hasattr(config, "database") and getattr(config.database, "path", None):
        cand = Path(config.database.path).resolve().parent.parent / ".agents" / "signals"
        if cand.exists():
            return cand
    return cwd_signals


def _get_brain_dir() -> Path:
    """Resolve the Antigravity conversation brain directory."""
    return Path.home() / ".gemini" / "antigravity" / "brain"


def _get_cadence_dir() -> Path:
    """Resolve the cadence runtime state directory."""
    cwd_cadence = Path.cwd() / ".run" / "cadence"
    if cwd_cadence.exists():
        return cwd_cadence
    default_cadence = Path("/Users/vecsatfoxmailcom/Documents/A-coding/26.09.08-antigravity-webhook-hub/.run/cadence")
    if default_cadence.exists():
        return default_cadence
    return cwd_cadence


def parse_sentinel_transcript(
    conv_id: str,
    brain_dir: Optional[Path] = None,
    cadence_dir: Optional[Path] = None,
    full_steps: bool = True,
) -> Optional[dict[str, Any]]:
    """Parse a conversation transcript JSONL file into a structured Sentinel run report."""
    b_dir = brain_dir or _get_brain_dir()
    c_dir = cadence_dir or _get_cadence_dir()
    t_file = b_dir / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not t_file.exists():
        return None

    prompt = ""
    created_at = None
    cadence_card = "CAD-20260911-webhook-hub-sentinel"
    tool_steps: list[dict[str, Any]] = []
    final_report = ""
    tools_used: set[str] = set()

    try:
        with open(t_file, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    item = json.loads(line_s)
                except Exception:
                    continue

                i_type = item.get("type")
                # Detect prompt
                if i_type == "USER_INPUT" and not prompt:
                    prompt = item.get("content", "")
                    created_at = item.get("created_at")
                    if "CAD-" in prompt:
                        for word in prompt.split():
                            if word.startswith("CAD-"):
                                cadence_card = word.strip("`'\" \n\r:,.")
                                break

                # Detect tool calls
                if item.get("tool_calls"):
                    for tc in item["tool_calls"]:
                        t_name = tc.get("name", "unknown")
                        tools_used.add(t_name)
                        if full_steps:
                            tool_steps.append({
                                "step_index": item.get("step_index", idx),
                                "tool_name": t_name,
                                "tool_args": tc.get("args", {}),
                                "created_at": item.get("created_at"),
                                "output": "",
                                "status": "executed",
                            })
                        else:
                            tool_steps.append({"step_index": item.get("step_index", idx)})
                elif i_type == "GENERIC" and tool_steps and full_steps and not tool_steps[-1]["output"]:
                    out_text = item.get("content") or ""
                    tool_steps[-1]["output"] = out_text[:1200]

                # Detect final report
                if i_type == "PLANNER_RESPONSE" and item.get("content") and not item.get("tool_calls"):
                    final_report = item.get("content", "")
    except Exception as e:
        logger.debug("Failed parsing transcript for %s: %s", conv_id, e)
        return None

    # Check success marker in cadence dir
    success_marker_path = None
    success_marker_exists = False
    c_folder = c_dir / cadence_card
    if c_folder.exists():
        markers = sorted(c_folder.glob("*.success"), reverse=True)
        if markers:
            success_marker_exists = True
            try:
                success_marker_path = str(markers[0].relative_to(Path.cwd()))
            except Exception:
                success_marker_path = str(markers[0])

    prompt_snippet = prompt.replace("\n", " ").strip()
    if len(prompt_snippet) > 180:
        prompt_snippet = prompt_snippet[:180] + "..."

    report_snippet = final_report.replace("\n", " ").strip()
    if len(report_snippet) > 200:
        report_snippet = report_snippet[:200] + "..."

    status = "succeeded" if final_report else ("running" if tool_steps else "unknown")

    res: dict[str, Any] = {
        "conversation_id": conv_id,
        "cadence_card": cadence_card,
        "created_at": created_at,
        "status": status,
        "steps_count": len(tool_steps),
        "tools_used": sorted(list(tools_used)),
        "prompt_snippet": prompt_snippet,
        "report_snippet": report_snippet,
        "success_marker_exists": success_marker_exists,
        "success_marker_path": success_marker_path,
    }

    if full_steps:
        res["prompt"] = prompt
        res["steps"] = tool_steps
        res["final_report"] = final_report

    return res


def normalize_signal_data(data: dict[str, Any], file_path: Optional[Path] = None) -> dict[str, Any]:
    """Normalize signal data so confidence_score, diffs, applied, and explanation are accessible at top level."""
    res = data.get("result")
    if isinstance(res, dict):
        if "confidence_score" not in data or data["confidence_score"] is None:
            data["confidence_score"] = res.get("confidence_score")
        if "diffs" not in data or data["diffs"] is None:
            data["diffs"] = res.get("diffs")
        if "applied" not in data or data["applied"] is None:
            data["applied"] = res.get("applied", False)
        if "explanation" not in data or data["explanation"] is None:
            data["explanation"] = res.get("explanation")
        if "target_page_url" not in data or not data["target_page_url"]:
            data["target_page_url"] = res.get("target_page_url")
    if file_path:
        try:
            data["signal_file"] = str(file_path.relative_to(Path.cwd()))
        except Exception:
            data["signal_file"] = file_path.name
    return data


def discover_sentinel_conversations(
    config: Optional[AppConfig] = None,
    sidecar_dir: Optional[Path] = None,
    brain_dir: Optional[Path] = None,
    limit: int = 20,
) -> list[str]:
    """Discover genuine autonomous Sentinel AI conversations from sidecar events and brain logs.

    Strictly filters out interactive user coding tasks, harness subagents, and parent conversations
    that merely mention 'webhook-hub-sentinel' in their task prompt.
    """
    discovered: list[tuple[float, str]] = []
    seen: set[str] = set()

    s_dir = sidecar_dir or _get_sidecar_events_dir(config)
    b_dir = brain_dir or _get_brain_dir()

    # Method A: Scan sidecar events for newConversation with Sentinel prompts
    if s_dir.exists():
        for ef in sorted(s_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(ef.read_text(encoding="utf-8"))
                nc = data.get("payload", {}).get("newConversation", {})
                cid = nc.get("conversationId")
                prompt = nc.get("prompt", "")
                if any(tag in prompt for tag in ("<original_task>", "<prior_attempt>", "**Task**:", "i want above")):
                    continue
                if cid and cid not in seen and ("Antigravity Webhook Hub — Sentinel" in prompt or "CAD-20260911" in prompt):
                    t_file = b_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
                    mtime = t_file.stat().st_mtime if t_file.exists() else ef.stat().st_mtime
                    discovered.append((mtime, cid))
                    seen.add(cid)
            except Exception:
                pass

    # Method B: Scan brain conversations with transcript matching sentinel keywords
    if b_dir.exists() and len(discovered) < limit:
        for cdir in b_dir.iterdir():
            if not cdir.is_dir() or cdir.name in seen:
                continue
            t_file = cdir / ".system_generated" / "logs" / "transcript.jsonl"
            if not t_file.exists():
                continue
            try:
                with open(t_file, "r", encoding="utf-8", errors="replace") as f:
                    first_line = f.readline()
                    if not first_line:
                        continue
                    if any(tag in first_line for tag in ("<original_task>", "<prior_attempt>", "**Task**:", "i want above")):
                        continue
                    if "Antigravity Webhook Hub — Sentinel" in first_line or "CAD-20260911-webhook-hub-sentinel" in first_line:
                        discovered.append((t_file.stat().st_mtime, cdir.name))
                        seen.add(cdir.name)
            except Exception:
                pass

    discovered.sort(key=lambda x: x[0], reverse=True)
    return [cid for _, cid in discovered[:limit]]


def get_task_agent_activity(
    task_id: str,
    task_data: dict[str, Any],
    db: Optional[Any] = None,
    config: Optional[AppConfig] = None,
) -> Optional[dict[str, Any]]:
    """Enrich task with associated Agent Signal, emitted pulse, or prompt payload."""
    action_type = str(task_data.get("action_type") or "").lower().strip()
    source = str(task_data.get("source") or "").lower().strip()
    signals_dir = _get_signals_dir(config)
    matching_signal = None

    # 1. Search for signal ID in logs, stdout, command, action_params_json, result_json, error_message
    logs_text = ""
    for field in ("stdout", "command", "action_params_json", "result_json", "error_message", "target_action"):
        val = task_data.get(field)
        if val:
            logs_text += str(val) + " "
    if task_data.get("logs"):
        for log_row in task_data["logs"]:
            logs_text += str(log_row.get("line", "")) + " "

    sig_match = re.search(r"\b(sig_[a-zA-Z0-9]+)\b", logs_text)
    if sig_match:
        sig_id = sig_match.group(1)
        target_file = signals_dir / "contact_review" / f"{sig_id}.signal.json"
        if not target_file.exists():
            found = list(signals_dir.glob(f"**/{sig_id}*.json"))
            if found:
                target_file = found[0]
        if target_file.exists():
            try:
                s_raw = json.loads(target_file.read_text(encoding="utf-8"))
                matching_signal = normalize_signal_data(s_raw, target_file)
            except Exception:
                pass

    # 2. Fallback: match by contact name in action_params_json or logs
    if not matching_signal and ("contact" in action_type or "contact" in source):
        params_str = str(task_data.get("action_params_json") or "") + " " + logs_text
        if signals_dir.exists():
            for sig_path in sorted(signals_dir.glob("**/*.signal.json"), reverse=True):
                try:
                    sdata = json.loads(sig_path.read_text(encoding="utf-8"))
                    t_name = sdata.get("target_name")
                    if t_name and t_name in params_str:
                        matching_signal = normalize_signal_data(sdata, sig_path)
                        break
                except Exception:
                    pass

    # 3. Search for matching sidebar pulse event
    events_dir = _get_sidecar_events_dir(config)
    matching_pulse = None
    if events_dir.exists():
        event_files = sorted(events_dir.glob("*.json"), reverse=True)[:80]
        for ef in event_files:
            try:
                ed = json.loads(ef.read_text(encoding="utf-8"))
                new_conv = ed.get("payload", {}).get("newConversation", {})
                if new_conv.get("taskId") == task_id:
                    matching_pulse = {
                        "file_name": ef.name,
                        "timestamp_ms": ed.get("timestampMs"),
                        "error": ed.get("error", ""),
                        "prompt": new_conv.get("prompt", ""),
                        "source": new_conv.get("source"),
                        "action": new_conv.get("action"),
                        "status": new_conv.get("status"),
                        "exit_code": new_conv.get("exitCode"),
                    }
                    break
            except Exception:
                pass

    # 4. Prompt payload if agent_signal
    prompt_payload = None
    if action_type in ("agent_signal", "signal"):
        try:
            params = json.loads(task_data.get("action_params_json") or "{}")
            prompt_payload = params.get("prompt") or params.get("payload")
        except Exception:
            prompt_payload = task_data.get("command")

    if not matching_signal and not matching_pulse and not prompt_payload and action_type not in ("agent_signal", "contact_review"):
        return None

    return {
        "signal": matching_signal,
        "pulse": matching_pulse,
        "prompt_payload": prompt_payload,
        "action_type": action_type,
    }


def register_agent_activities_routes(
    server: AsyncHTTPServer,
    config: AppConfig,
    db: Optional[Any] = None,
    broker: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
) -> None:
    """Register all agent activities and sentinel observability routes."""
    if ("GET", "/api/agent-activities/summary") in server._exact_routes:
        return

    sidecar_events_dir = _get_sidecar_events_dir(config)
    signals_dir = _get_signals_dir(config)
    brain_dir = _get_brain_dir()
    cadence_dir = _get_cadence_dir()

    # 1. GET /api/agent-activities/summary
    async def handle_activities_summary(req: HTTPRequest) -> HTTPResponse:
        """High-level summary of Sentinel runs, Agent Signals, and Pulse Queue."""
        # Sentinel runs count
        sentinel_cids = discover_sentinel_conversations(config=config, sidecar_dir=sidecar_events_dir, brain_dir=brain_dir, limit=10)
        latest_sentinel = None
        if sentinel_cids:
            latest_sentinel = parse_sentinel_transcript(sentinel_cids[0], brain_dir, cadence_dir, full_steps=False)

        # Agent signals
        signals: list[dict[str, Any]] = []
        if signals_dir.exists():
            for p in sorted(signals_dir.glob("**/*.signal.json"), reverse=True):
                try:
                    sd = json.loads(p.read_text(encoding="utf-8"))
                    signals.append(normalize_signal_data(sd, p))
                except Exception:
                    pass

        by_verdict: dict[str, int] = {}
        for s in signals:
            v = str(s.get("verdict") or "unknown").lower()
            by_verdict[v] = by_verdict.get(v, 0) + 1

        # Pulse events
        pulse_count = 0
        latest_pulse = None
        if sidecar_events_dir.exists():
            pulse_files = sorted(sidecar_events_dir.glob("*.json"), reverse=True)
            pulse_count = len(pulse_files)
            if pulse_files:
                try:
                    latest_raw = json.loads(pulse_files[0].read_text(encoding="utf-8"))
                    nc = latest_raw.get("payload", {}).get("newConversation", {})
                    latest_pulse = {
                        "file_name": pulse_files[0].name,
                        "timestamp_ms": latest_raw.get("timestampMs"),
                        "prompt": nc.get("prompt", ""),
                        "status": nc.get("status"),
                        "source": nc.get("source"),
                        "action": nc.get("action"),
                    }
                except Exception:
                    pass

        # Memory relief check
        gc.collect(1)
        apply_memory_pressure_relief()

        summary_data = {
            "status": "ok",
            "memory_rss_mb": get_memory_rss_mb(),
            "sentinels": {
                "total_runs": len(sentinel_cids),
                "latest_run": latest_sentinel,
            },
            "signals": {
                "total_signals": len(signals),
                "by_verdict": by_verdict,
                "latest_signal": signals[0] if signals else None,
            },
            "pulses": {
                "total_pulses": pulse_count,
                "latest_pulse": latest_pulse,
            },
        }
        return HTTPResponse.json(summary_data, status_code=200)

    # 2. GET /api/agent-activities/sentinels
    async def handle_sentinels_list(req: HTTPRequest) -> HTTPResponse:
        """List recent Sentinel runs with conversation ID, cadence card, status, and summary."""
        try:
            limit = min(max(1, int(req.query_params.get("limit", 20))), 100)
        except (ValueError, TypeError):
            limit = 20

        sentinel_cids = discover_sentinel_conversations(config=config, sidecar_dir=sidecar_events_dir, brain_dir=brain_dir, limit=limit)
        runs: list[dict[str, Any]] = []

        for cid in sentinel_cids:
            run_data = parse_sentinel_transcript(cid, brain_dir, cadence_dir, full_steps=False)
            if run_data:
                runs.append(run_data)

        # Check cadence marker directly
        marker_exists = False
        marker_path = None
        c_folder = cadence_dir / "CAD-20260911-webhook-hub-sentinel"
        if c_folder.exists():
            markers = sorted(c_folder.glob("*.success"), reverse=True)
            if markers:
                marker_exists = True
                try:
                    marker_path = str(markers[0].relative_to(Path.cwd()))
                except Exception:
                    marker_path = str(markers[0])

        return HTTPResponse.json({
            "status": "ok",
            "total": len(runs),
            "cadence_card": "CAD-20260911-webhook-hub-sentinel",
            "frequency": "Every 4 Hours (6 times daily)",
            "success_marker_exists": marker_exists,
            "success_marker_path": marker_path,
            "runs": runs,
        }, status_code=200)

    # 3. GET /api/agent-activities/sentinels/{conversation_id}
    async def handle_sentinel_detail(req: HTTPRequest, conversation_id: str) -> HTTPResponse:
        """Full detail of a Sentinel run including full prompt, tool steps, and markdown report."""
        clean_cid = conversation_id.strip()
        run_data = parse_sentinel_transcript(clean_cid, brain_dir, cadence_dir, full_steps=True)
        if not run_data:
            return HTTPResponse.error(f"Sentinel conversation {clean_cid} not found", status_code=404)

        return HTTPResponse.json(run_data, status_code=200)

    # 4. GET /api/agent-activities/signals
    async def handle_signals_list(req: HTTPRequest) -> HTTPResponse:
        """List emitted Agent signals (Notion CRM contact reviews, verdicts, confidence)."""
        verdict_filter = (req.query_params.get("verdict") or "").lower().strip()
        search_query = (req.query_params.get("q") or req.query_params.get("search") or "").lower().strip()

        try:
            limit = min(max(1, int(req.query_params.get("limit", 50))), 200)
        except (ValueError, TypeError):
            limit = 50

        signals: list[dict[str, Any]] = []
        if signals_dir.exists():
            for p in sorted(signals_dir.glob("**/*.signal.json"), reverse=True):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    data = normalize_signal_data(data, p)
                    # Filter by verdict
                    v = str(data.get("verdict") or "").lower()
                    if verdict_filter and v != verdict_filter:
                        continue

                    # Filter by search
                    if search_query:
                        t_name = str(data.get("target_name") or "").lower()
                        s_id = str(data.get("signal_id") or "").lower()
                        exp = str(data.get("explanation") or data.get("result", {}).get("explanation") or "").lower()
                        if search_query not in t_name and search_query not in s_id and search_query not in exp:
                            continue

                    signals.append(data)
                except Exception as e:
                    logger.debug("Failed reading signal %s: %s", p.name, e)

        return HTTPResponse.json({
            "status": "ok",
            "total": len(signals),
            "signals": signals[:limit],
            "limit": limit,
        }, status_code=200)

    # 5. GET /api/agent-activities/pulses
    async def handle_pulses_list(req: HTTPRequest) -> HTTPResponse:
        """List recent Antigravity sidebar pulse queue events."""
        try:
            limit = min(max(1, int(req.query_params.get("limit", 50))), 200)
        except (ValueError, TypeError):
            limit = 50

        pulses: list[dict[str, Any]] = []
        total_count = 0

        if sidecar_events_dir.exists():
            files = sorted(sidecar_events_dir.glob("*.json"), reverse=True)
            total_count = len(files)
            for ef in files[:limit]:
                try:
                    d = json.loads(ef.read_text(encoding="utf-8"))
                    nc = d.get("payload", {}).get("newConversation", {})
                    ts_ms = d.get("timestampMs") or ""
                    ts_iso = ""
                    if ts_ms.isdigit():
                        ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(ts_ms) / 1000.0)) + " UTC"

                    pulses.append({
                        "file_name": ef.name,
                        "timestamp_ms": ts_ms,
                        "timestamp_iso": ts_iso,
                        "error": d.get("error", ""),
                        "prompt": nc.get("prompt", ""),
                        "task_id": nc.get("taskId"),
                        "source": nc.get("source"),
                        "action": nc.get("action"),
                        "status": nc.get("status"),
                        "exit_code": nc.get("exitCode"),
                    })
                except Exception:
                    pass

        return HTTPResponse.json({
            "status": "ok",
            "total": total_count,
            "pulses": pulses,
            "limit": limit,
        }, status_code=200)

    # Register all routes on server
    server.add_route("GET", "/api/agent-activities/summary", handle_activities_summary)
    server.add_route("GET", "/api/agent-activities/sentinels", handle_sentinels_list)
    server.add_route("GET", "/api/agent-activities/sentinels/{conversation_id}", handle_sentinel_detail)
    server.add_route("GET", "/api/agent-activities/signals", handle_signals_list)
    server.add_route("GET", "/api/agent-activities/pulses", handle_pulses_list)
