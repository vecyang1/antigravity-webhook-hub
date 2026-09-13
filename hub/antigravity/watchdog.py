"""
Antigravity Webhook Hub — Antigravity Watchdog & Auto Pull-Up Engine
Provides continuous background health monitoring, network disruption detection,
and automated resuscitation (拉起) for stalled Antigravity sessions and subagents.

Key Capabilities:
1. Fail-closed network health probe: verifies outward internet connectivity before attempting pull-ups.
2. Fast transcript scanning: identifies stream interruptions and terminated agent sessions within lookback window.
3. Subagent & root conversation awareness: seamlessly revives parent sessions, subagents (/boost, teamwork-preview), and sidecars.
4. Circuit breaker & retry limits: caps resuscitation attempts per session (default 3) to prevent runaway loops.
5. Single Source of Truth: records all resuscitation actions and states in SQLite database (antigravity_resuscitations).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from hub.antigravity.agentapi_client import AgentAPIClient

from hub.antigravity.result_delivery import resolve_transcript_path
from hub.config import AntigravityWatchdogConfig
from hub.db import DatabaseManager

logger = logging.getLogger("hub.antigravity.watchdog")

# Common network/transport error signatures recorded in transcript turns
INTERRUPTED_STREAM_PATTERNS = (
    "stream disconnected before completion",
    "connection reset by peer",
    "broken pipe",
    "network is unreachable",
    "timed out waiting for response",
    "transport error",
    "socket error",
    "econnreset",
    "econnrefused",
    "etimedout",
    "network error",
    "failed to connect to language_server",
    "failed to connect to host",
    "client network socket disconnected",
    "tls handshake timeout",
    "temporary failure in name resolution",
)

DEFAULT_RESUSCITATION_PROMPT = (
    "【系统自动网络自愈提醒】\n"
    "检测到底层网络连接此前中断或服务重启，现网络连接已恢复稳定正常。\n"
    "请越过中断错误，继续检查上一步执行进展并执行原定任务计划（请全中文汇报进展）。"
)


@dataclass(slots=True)
class StalledSessionInfo:
    """Detailed metadata for a stalled Antigravity session requiring resuscitation."""

    conversation_id: str
    transcript_path: Path
    last_step_index: int
    last_error: str
    last_error_time: float
    is_subagent: bool
    sidecar_slug: Optional[str] = None
    attempt_count: int = 0
    can_resuscitate: bool = True
    skip_reason: Optional[str] = None


class AntigravityWatchdog:
    """
    24/7 Watchdog engine running inside Webhook Hub daemon.
    Detects dropped Antigravity sessions and revives them without manual user intervention.
    """

    def __init__(
        self,
        db: DatabaseManager,
        config: Optional[AntigravityWatchdogConfig] = None,
        agentapi_client: Optional[Any] = None,
        broker: Optional[Any] = None,
    ):
        self.db = db
        self.config = config or AntigravityWatchdogConfig()
        self._agentapi_client = agentapi_client
        self.broker = broker
        self._brain_dir = Path(
            self.config.brain_dir
            or os.environ.get("ANTIGRAVITY_BRAIN_DIR")
            or os.path.expanduser("~/.gemini/antigravity/brain")
        )
        self._sidecar_data_dir = Path(
            self.config.sidecar_data_dir
            or os.environ.get("ANTIGRAVITY_SIDECAR_DATA_DIR")
            or os.path.expanduser("~/.gemini/antigravity/sidecar_data")
        )

    @property
    def agentapi(self):
        if self._agentapi_client is None:
            from hub.antigravity.agentapi_client import AgentAPIClient
            self._agentapi_client = AgentAPIClient()
        return self._agentapi_client

    @agentapi.setter
    def agentapi(self, value):
        self._agentapi_client = value

    def check_network_health(self) -> bool:
        """
        Fast TCP probe to verify internet connectivity.
        Fails closed if the machine is completely offline or DNS/gateways are down.
        """
        host = self.config.probe_host or "1.1.1.1"
        port = self.config.probe_port or 53
        timeout = self.config.probe_timeout_seconds or 1.0

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()
            return True
        except Exception:
            # Fallback probe to secondary DNS (8.8.8.8)
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect(("8.8.8.8", 53))
                sock.close()
                return True
            except Exception as e:
                logger.debug("Network health check failed: %s", e)
                return False

    def is_agentapi_ready(self) -> bool:
        """Check if agentapi binary is executable and language_server is available."""
        if not self.agentapi.is_available():
            return False
        addr, _ = self.agentapi.ensure_credentials(force=False)
        return bool(addr)

    def _tail_transcript_lines(self, file_path: Path, max_lines: int = 15) -> list[str]:
        """Read the last N lines of a transcript file efficiently."""
        lines: list[str] = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                read_size = min(size, 65536)
                f.seek(size - read_size, os.SEEK_SET)
                block = f.read()
                all_lines = block.splitlines()
                lines = all_lines[-max_lines:]
        except Exception as e:
            logger.debug("Failed reading tail of transcript %s: %s", file_path, e)
        return lines

    def _find_associated_sidecar(self, conversation_id: str) -> Optional[str]:
        """Match conversation_id to any scheduled task or sidecar run."""
        if not self._sidecar_data_dir.exists():
            return None

        try:
            for slug_dir in self._sidecar_data_dir.iterdir():
                if not slug_dir.is_dir():
                    continue
                events_dir = slug_dir / "events"
                if not events_dir.exists():
                    continue
                for ev_file in events_dir.glob("*.json"):
                    try:
                        content = ev_file.read_text(encoding="utf-8", errors="ignore")
                        if conversation_id in content:
                            return slug_dir.name
                    except Exception:
                        continue
        except Exception as e:
            logger.debug("Error resolving sidecar for %s: %s", conversation_id, e)

        return None

    def scan_stalled_conversations(self) -> list[StalledSessionInfo]:
        """
        Scan all conversation folders in ~/.gemini/antigravity/brain/ within lookback window.
        Detects conversations where the last step is an interrupted stream or terminal error,
        and determines whether they are eligible for automated pull-up.
        """
        if not self._brain_dir.exists():
            return []

        now = time.time()
        lookback_seconds = self.config.lookback_minutes * 60
        stalled: list[StalledSessionInfo] = []

        try:
            for convo_dir in self._brain_dir.iterdir():
                if not convo_dir.is_dir():
                    continue
                convo_id = convo_dir.name
                if convo_id == "tempmediaStorage" or len(convo_id) < 20:
                    continue

                try:
                    mtime = convo_dir.stat().st_mtime
                    if now - mtime > lookback_seconds:
                        continue
                except Exception:
                    continue

                transcript_path = resolve_transcript_path(convo_id, self._brain_dir)
                if not transcript_path or not transcript_path.exists():
                    continue

                try:
                    trans_stat = transcript_path.stat()
                    if now - trans_stat.st_mtime > lookback_seconds:
                        continue
                    file_mtime = trans_stat.st_mtime
                except Exception:
                    continue

                lines = self._tail_transcript_lines(transcript_path, max_lines=15)
                if not lines:
                    continue

                parsed_steps: list[dict[str, Any]] = []
                for line in lines:
                    line_clean = line.strip()
                    if not line_clean:
                        continue
                    try:
                        parsed_steps.append(json.loads(line_clean))
                    except Exception:
                        continue

                if not parsed_steps:
                    continue

                last_step = parsed_steps[-1]
                last_step_idx = last_step.get("step_index", 0)
                last_source = last_step.get("source", "")
                last_type = last_step.get("type", "")
                last_status = last_step.get("status", "")
                last_content = last_step.get("content", "")

                # If the last step is a normal completed model planner response with content,
                # the agent has finished its turn and is waiting for the user. It is not stalled.
                if last_source == "MODEL" and last_type == "PLANNER_RESPONSE" and last_status == "DONE" and last_content and not last_step.get("tool_calls"):
                    continue

                # If the last step is an active user input or running tool, it's not stalled.
                if last_status == "RUNNING" or (last_source in ("USER_EXPLICIT", "USER") and now - file_mtime < self.config.stall_grace_seconds):
                    continue

                is_stalled = False
                matched_error = ""

                # Check if the conversation ended in an empty planner response hang
                if len(parsed_steps) >= 2 and last_source == "MODEL" and last_type == "PLANNER_RESPONSE" and not last_content and not last_step.get("tool_calls"):
                    if now - file_mtime > self.config.stall_grace_seconds:
                        is_stalled = True
                        matched_error = "empty_planner_response_hang"

                # If not empty hang, inspect steps backwards for unresolved system/error interruptions
                if not is_stalled:
                    for step in reversed(parsed_steps):
                        s_source = step.get("source", "")
                        s_type = step.get("type", "")
                        s_status = step.get("status", "")
                        s_content = str(step.get("content") or "").lower()

                        # If we encounter a successful MODEL response before encountering any error,
                        # the conversation was already successfully continuing or recovered.
                        # (A completed planner response is valid whether it contains text or tool calls)
                        if s_source == "MODEL" and s_type == "PLANNER_RESPONSE" and s_status == "DONE" and (s_content or step.get("tool_calls")):
                            break

                        # Only match error patterns against SYSTEM or ERROR messages (never against completed MODEL or tool output)
                        if s_source == "SYSTEM" or s_status == "ERROR" or s_type == "ERROR_MESSAGE":
                            for pattern in INTERRUPTED_STREAM_PATTERNS:
                                if pattern in s_content:
                                    is_stalled = True
                                    matched_error = pattern
                                    break
                            if is_stalled:
                                break
                            if s_status == "ERROR" or s_type == "ERROR_MESSAGE":
                                is_stalled = True
                                matched_error = s_content[:150] or f"{s_source}_{s_status}"
                                break

                if not is_stalled:
                    continue

                # 1. Resolve session metadata and prior attempts BEFORE evaluating eligibility
                prior_attempts = self.db.get_resuscitation_attempts(convo_id) if self.db else 0
                sidecar_slug = self._find_associated_sidecar(convo_id)

                is_subagent = False
                first_content = str(parsed_steps[0].get("content") or "")
                if "<original_task>" in first_content or "invoke_subagent" in first_content or "DeepInvestigator" in first_content or "DeepCoder" in first_content:
                    is_subagent = True

                # 2. Check circuit breaker first: if retries already exhausted, do not mask as within_stall_grace_period
                if prior_attempts >= self.config.max_retries_per_session:
                    stalled.append(
                        StalledSessionInfo(
                            conversation_id=convo_id,
                            transcript_path=transcript_path,
                            last_step_index=last_step_idx,
                            last_error=matched_error,
                            last_error_time=file_mtime,
                            is_subagent=is_subagent,
                            sidecar_slug=sidecar_slug,
                            attempt_count=prior_attempts,
                            can_resuscitate=False,
                            skip_reason=f"max_retries_exhausted ({prior_attempts}/{self.config.max_retries_per_session})",
                        )
                    )
                    continue

                # 3. Stall grace period: recent errors (< stall_grace_seconds) are given time to self-heal
                if now - file_mtime < self.config.stall_grace_seconds:
                    stalled.append(
                        StalledSessionInfo(
                            conversation_id=convo_id,
                            transcript_path=transcript_path,
                            last_step_index=last_step_idx,
                            last_error=matched_error,
                            last_error_time=file_mtime,
                            is_subagent=is_subagent,
                            sidecar_slug=sidecar_slug,
                            attempt_count=prior_attempts,
                            can_resuscitate=False,
                            skip_reason="within_stall_grace_period",
                        )
                    )
                    continue

                # 4. Eligible for automated resuscitation
                stalled.append(
                    StalledSessionInfo(
                        conversation_id=convo_id,
                        transcript_path=transcript_path,
                        last_step_index=last_step_idx,
                        last_error=matched_error,
                        last_error_time=file_mtime,
                        is_subagent=is_subagent,
                        sidecar_slug=sidecar_slug,
                        attempt_count=prior_attempts,
                        can_resuscitate=True,
                        skip_reason=None,
                    )
                )

        except Exception as e:
            logger.error("Error during stalled conversation scan: %s", e)

        return stalled

    async def resuscitate_session(
        self,
        session_info: StalledSessionInfo,
        custom_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Pull up / resuscitate a stalled conversation or subagent.
        Records execution attempt to database, dispatches agentapi send-message,
        and records final status.
        """
        convo_id = session_info.conversation_id
        sidecar_slug = session_info.sidecar_slug
        last_error = session_info.last_error
        prompt = custom_prompt or DEFAULT_RESUSCITATION_PROMPT

        record = self.db.record_resuscitation(
            conversation_id=convo_id,
            sidecar_slug=sidecar_slug,
            last_error=last_error,
            status="attempting",
            resuscitation_prompt=prompt,
            last_step_index=session_info.last_step_index,
        )
        res_id = record["resuscitation_id"]

        logger.info(
            "Resuscitating Antigravity session %s (subagent=%s, attempt=%d/%d, error=%s)",
            convo_id,
            session_info.is_subagent,
            record["attempt_count"],
            self.config.max_retries_per_session,
            last_error,
        )

        if self.broker:
            try:
                await self.broker.publish("events", {
                    "event": "antigravity_resuscitation",
                    "type": "antigravity_resuscitation",
                    "action": "attempting",
                    "resuscitation_id": res_id,
                    "conversation_id": convo_id,
                    "attempt_count": record["attempt_count"],
                    "status": "attempting",
                    "timestamp": time.time(),
                })
            except Exception as b_err:
                logger.debug("Failed publishing resuscitation attempting event: %s", b_err)

        try:
            success, stdout, err = await self.agentapi.send_message(
                conversation_id=convo_id,
                content=prompt,
                timeout_seconds=30,
            )

            if success:
                self.db.update_resuscitation_status(res_id, "resuscitated")
                logger.info("Successfully resuscitated session %s (res_id=%s)", convo_id, res_id)
                if self.broker:
                    try:
                        await self.broker.publish("events", {
                            "event": "antigravity_resuscitation",
                            "type": "antigravity_resuscitation",
                            "action": "resuscitated",
                            "resuscitation_id": res_id,
                            "conversation_id": convo_id,
                            "status": "resuscitated",
                            "success": True,
                            "timestamp": time.time(),
                        })
                    except Exception as b_err:
                        logger.debug("Failed publishing resuscitation success event: %s", b_err)
                return {
                    "success": True,
                    "resuscitation_id": res_id,
                    "conversation_id": convo_id,
                    "status": "resuscitated",
                    "output": stdout,
                }
            else:
                new_status = "exhausted" if record["attempt_count"] >= self.config.max_retries_per_session else "failed"
                self.db.update_resuscitation_status(res_id, new_status)
                logger.warning(
                    "Failed to resuscitate session %s: %s (status marked as %s)",
                    convo_id,
                    err,
                    new_status,
                )
                if self.broker:
                    try:
                        await self.broker.publish("events", {
                            "event": "antigravity_resuscitation",
                            "type": "antigravity_resuscitation",
                            "action": new_status,
                            "resuscitation_id": res_id,
                            "conversation_id": convo_id,
                            "status": new_status,
                            "success": False,
                            "error": err,
                            "timestamp": time.time(),
                        })
                    except Exception as b_err:
                        logger.debug("Failed publishing resuscitation failure event: %s", b_err)
                return {
                    "success": False,
                    "resuscitation_id": res_id,
                    "conversation_id": convo_id,
                    "status": new_status,
                    "error": err,
                }

        except Exception as e:
            logger.exception("Unexpected exception resuscitating session %s: %s", convo_id, e)
            self.db.update_resuscitation_status(res_id, "failed")
            if self.broker:
                try:
                    await self.broker.publish("events", {
                        "event": "antigravity_resuscitation",
                        "type": "antigravity_resuscitation",
                        "action": "failed",
                        "resuscitation_id": res_id,
                        "conversation_id": convo_id,
                        "status": "failed",
                        "success": False,
                        "error": str(e),
                        "timestamp": time.time(),
                    })
                except Exception as b_err:
                    logger.debug("Failed publishing resuscitation exception event: %s", b_err)
            return {
                "success": False,
                "resuscitation_id": res_id,
                "conversation_id": convo_id,
                "status": "failed",
                "error": str(e),
            }

    async def resuscitate_stalled_sessions(self) -> list[dict[str, Any]]:
        """
        Main execution tick called by dispatcher sweeper or CLI.
        1. Checks network health (fail-closed if offline).
        2. Scans for stalled sessions and subagents.
        3. Automatically pulls up eligible sessions.
        """
        if not self.config.enabled:
            return []

        if not self.check_network_health():
            logger.debug("Antigravity Watchdog: Network probe unreachable, skipping pull-up tick.")
            return []

        if not self.is_agentapi_ready():
            logger.debug("Antigravity Watchdog: Antigravity language_server not ready, skipping tick.")
            return []

        stalled = self.scan_stalled_conversations()
        results: list[dict[str, Any]] = []

        for item in stalled:
            if not item.can_resuscitate:
                logger.debug(
                    "Skipping session %s: %s",
                    item.conversation_id,
                    item.skip_reason,
                )
                continue

            if self.config.auto_resuscitate:
                res = await self.resuscitate_session(item)
                results.append(res)

        return results

    def get_status(self) -> dict[str, Any]:
        """Comprehensive status report for CLI doctor, API, and dashboards."""
        net_ok = self.check_network_health()
        agentapi_avail = self.agentapi.is_available()
        addr, _ = self.agentapi.ensure_credentials(force=False)
        ls_ok = bool(addr)

        recent_resuscitations = self.db.list_resuscitations(limit=25) if self.db else []
        stalled = self.scan_stalled_conversations()

        stats = (
            self.db.get_resuscitation_stats()
            if self.db and hasattr(self.db, "get_resuscitation_stats")
            else {
                "total": len(recent_resuscitations),
                "resuscitated": sum(1 for r in recent_resuscitations if r.get("status") == "resuscitated"),
                "failed": sum(1 for r in recent_resuscitations if r.get("status") == "failed"),
                "exhausted": sum(1 for r in recent_resuscitations if r.get("status") == "exhausted"),
                "attempting": sum(1 for r in recent_resuscitations if r.get("status") == "attempting"),
                "resolved": sum(1 for r in recent_resuscitations if r.get("status") == "resolved"),
            }
        )

        probe_host = self.config.probe_host or "1.1.1.1"
        probe_port = self.config.probe_port or 53

        return {
            "watchdog_enabled": self.config.enabled,
            "auto_resuscitate": self.config.auto_resuscitate,
            "interval_seconds": self.config.interval_seconds,
            "network_online": net_ok,
            "probe_target": f"{probe_host}:{probe_port}",
            "agentapi_available": agentapi_avail,
            "language_server_connected": ls_ok,
            "language_server_address": addr,
            "stalled_sessions_detected": len(stalled),
            "stalled_sessions": [
                {
                    "conversation_id": s.conversation_id,
                    "last_error": s.last_error,
                    "is_subagent": s.is_subagent,
                    "sidecar_slug": s.sidecar_slug,
                    "attempt_count": s.attempt_count,
                    "can_resuscitate": s.can_resuscitate,
                    "skip_reason": s.skip_reason,
                    "last_step_index": s.last_step_index,
                    "last_error_time": s.last_error_time,
                }
                for s in stalled
            ],
            "resuscitation_stats": stats,
            "recent_resuscitations": recent_resuscitations,
        }
