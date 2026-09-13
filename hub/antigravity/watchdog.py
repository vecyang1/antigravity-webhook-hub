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
import re
import socket
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

QUOTA_RESUSCITATION_PROMPT = (
    "【系统自动配额恢复拉起】\n"
    "检测到此前的账户配额冷却期已结束，现模型调用配额已恢复正常。\n"
    "请越过配额中断报错，检查上一步执行进展并继续推进原定任务（请全中文汇报进展）。"
)

MCP_ERROR_RESUSCITATION_PROMPT = (
    "【系统自动 MCP 故障自愈拉起】\n"
    "检测到上一轮由于 MCP 工具报错或连接中断未能生成有效回复，但你的目标仍处于活跃状态。\n"
    "请越过该 MCP 错误，跳过或规避报错的工具调用，选用本地 CLI、原生命令或直接完成代码，继续完成目标任务（请全中文汇报进展）。"
)

SCHEDULE_REMOUNT_PROMPT = (
    "【系统后台调度守护提醒】\n"
    "检测到 IDE 服务重启导致会话内的后台定时任务（原 Cron: {cron}）已中断未按期触发。\n"
    "正在自动重新激活巡检流程，请立即执行一次巡检并调用 schedule 工具重新挂载该定时任务（请全中文汇报进展）。"
)

EMPTY_RESPONSE_RESUSCITATION_PROMPT = (
    "【系统自动自愈拉起提醒】\n"
    "检测到上一轮模型生成由于偶发超时或响应中断未输出有效内容，但你的上下文与环境完全完好。\n"
    "请根据上一轮工具执行结果与当前任务计划，直接继续推进下一步（请全中文汇报进展）。"
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
    is_quota_exhausted: bool = False
    quota_resets_in_seconds: Optional[float] = None
    quota_reset_timestamp: Optional[str] = None
    is_mcp_error: bool = False
    has_active_schedule: bool = False
    active_cron_expression: Optional[str] = None


def parse_quota_reset_seconds(text: str) -> Optional[float]:
    """
    Extract reset delay in seconds from error text or JSON.
    Examples:
      - 'Resets in 1h54m13s' -> 6853.0
      - 'Resets in 45m10s' -> 2710.0
      - 'Resets in 30s' -> 30.0
      - 'retryDelay": "6853.667943359s"' -> 6853.66
    """
    if not text:
        return None

    # 1. Direct retryDelay matching (Google Error Info)
    m_delay = re.search(r'retryDelay["\s:]+([0-9\.]+)s', text)
    if m_delay:
        try:
            return float(m_delay.group(1))
        except ValueError:
            pass

    # 2. Resets in / quotaResetDelay matching
    m_resets = re.search(
        r'(?:Resets in|quotaResetDelay["\s:]+)\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([0-9\.]+)s)?',
        text,
        re.IGNORECASE,
    )
    if m_resets and any(m_resets.groups()):
        hours = float(m_resets.group(1) or 0)
        minutes = float(m_resets.group(2) or 0)
        seconds = float(m_resets.group(3) or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0:
            return total

    return None


def inspect_conversation_db_for_quota(
    convo_id: str,
    conversations_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Inspect SQLite database in ~/.gemini/antigravity/conversations/<id>.db for genuine QUOTA_EXHAUSTED errors."""
    c_dir = conversations_dir or Path(os.path.expanduser("~/.gemini/antigravity/conversations"))
    db_file = c_dir / f"{convo_id}.db"
    if not db_file.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        # step_type 17 is error message step; checks only the latest 5 error steps
        cur.execute(
            "SELECT idx, step_payload FROM steps WHERE step_type = 17 ORDER BY idx DESC LIMIT 5"
        )
        rows = cur.fetchall()
        conn.close()

        for row in rows:
            payload_bytes = row[1]
            if not payload_bytes:
                continue
            payload_str = payload_bytes.decode("utf-8", errors="ignore")
            if "QUOTA_EXHAUSTED" in payload_str or "Individual quota reached" in payload_str:
                sec = parse_quota_reset_seconds(payload_str)
                m_ts = re.search(r'quotaResetTimeStamp["\s:]+([0-9T:\-Z]+)', payload_str)
                reset_ts = m_ts.group(1) if m_ts else None
                return {
                    "step_index": row[0],
                    "resets_in_seconds": sec or 6853.0,
                    "reset_timestamp": reset_ts,
                }
    except Exception as e:
        logger.debug("Error checking conversation db %s for quota: %s", convo_id, e)
    return None


def extract_active_schedule_from_transcript(transcript_path: Path) -> Optional[dict[str, Any]]:
    """Scan transcript for the last schedule tool call with CronExpression and last trigger time."""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_size = min(size, 262144)  # read last 256KB
            f.seek(size - read_size, os.SEEK_SET)
            block = f.read()

        lines = block.splitlines()
        last_schedule = None
        last_trigger_time = 0.0

        for line in reversed(lines):
            line_clean = line.strip()
            if not line_clean:
                continue

            if not last_schedule and ('"name":"schedule"' in line_clean or '"name": "schedule"' in line_clean):
                try:
                    obj = json.loads(line_clean)
                    calls = obj.get("tool_calls") or []
                    for call in calls:
                        if call.get("name") == "schedule":
                            args = call.get("args") or {}
                            cron = args.get("CronExpression")
                            prompt = args.get("Prompt")
                            if cron:
                                last_schedule = {
                                    "cron": str(cron).strip(' "'),
                                    "prompt": str(prompt or "").strip(' "'),
                                }
                                break
                except Exception:
                    pass

            if "Cron trigger #" in line_clean or "巡检汇报" in line_clean:
                try:
                    obj = json.loads(line_clean)
                    ts_str = obj.get("created_at")
                    if ts_str:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        t_sec = dt.timestamp()
                        if t_sec > last_trigger_time:
                            last_trigger_time = t_sec
                except Exception:
                    pass

        if last_schedule:
            cron_expr = last_schedule["cron"]
            interval = 1800
            m = re.search(r'\*/(\d+)', cron_expr)
            if m:
                interval = int(m.group(1)) * 60
            return {
                "cron": cron_expr,
                "prompt": last_schedule["prompt"],
                "interval_seconds": interval,
                "last_trigger_time": last_trigger_time,
            }
    except Exception as e:
        logger.debug("Failed extracting schedule from %s: %s", transcript_path, e)
    return None


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

                prior_attempts = self.db.get_resuscitation_attempts(convo_id) if self.db else 0
                sidecar_slug = self._find_associated_sidecar(convo_id)

                is_subagent = False
                first_content = str(parsed_steps[0].get("content") or "")
                if "<original_task>" in first_content or "invoke_subagent" in first_content or "DeepInvestigator" in first_content or "DeepCoder" in first_content:
                    is_subagent = True

                # --- 1. Check Active Quota Cooldown in DB ---
                active_cd = self.db.get_active_quota_cooldown(convo_id) if self.db else None
                if active_cd:
                    cd_step = active_cd.get("last_step_index")
                    if cd_step is not None and last_step_idx > cd_step + 1:
                        # Session advanced past the recorded cooldown step!
                        active_cd = None
                    else:
                        db_q = inspect_conversation_db_for_quota(convo_id)
                        has_recent_quota = (
                            any(
                                (s.get("source") in ("SYSTEM", "ERROR") or s.get("status") == "ERROR")
                                and ("individual quota reached" in str(s.get("content") or "").lower() or "resource_exhausted" in str(s.get("content") or "").lower())
                                for s in parsed_steps[-4:]
                            )
                            or (db_q and abs(db_q.get("step_index", 0) - last_step_idx) <= 2)
                        )
                        if not has_recent_quota:
                            active_cd = None

                if active_cd:
                    cooldown_until = float(active_cd.get("cooldown_until") or 0.0)
                    if now < cooldown_until and not (now - file_mtime > 300 and prior_attempts == 0):
                        remaining = int(cooldown_until - now)
                        stalled.append(
                            StalledSessionInfo(
                                conversation_id=convo_id,
                                transcript_path=transcript_path,
                                last_step_index=last_step_idx,
                                last_error=active_cd.get("last_error") or "Individual quota reached",
                                last_error_time=file_mtime,
                                is_subagent=is_subagent,
                                sidecar_slug=sidecar_slug,
                                attempt_count=prior_attempts,
                                can_resuscitate=False,
                                skip_reason=f"quota_cooldown (resets in {remaining}s)",
                                is_quota_exhausted=True,
                                quota_resets_in_seconds=remaining,
                            )
                        )
                        continue
                    else:
                        # Cooldown expired or probe allowed! Eligible for immediate automated pull-up
                        stalled.append(
                            StalledSessionInfo(
                                conversation_id=convo_id,
                                transcript_path=transcript_path,
                                last_step_index=last_step_idx,
                                last_error="quota_restored_pull_up",
                                last_error_time=file_mtime,
                                is_subagent=is_subagent,
                                sidecar_slug=sidecar_slug,
                                attempt_count=prior_attempts,
                                can_resuscitate=True,
                                skip_reason=None,
                                is_quota_exhausted=False,
                            )
                        )
                        continue

                # --- 2. Check for Newly Encountered Quota Exhaustion ---
                quota_detected = False
                quota_sec = None
                quota_ts = None
                for s in reversed(parsed_steps[-6:]):
                    # Only check genuine error/system steps, never user prompts or tool output text discussing quota
                    s_source = s.get("source", "")
                    s_type = s.get("type", "")
                    s_status = s.get("status", "")
                    if s_source not in ("SYSTEM", "ERROR") and s_type != "ERROR_MESSAGE" and s_status != "ERROR":
                        continue
                    s_content = str(s.get("content") or "")
                    if "individual quota reached" in s_content.lower() or "resource_exhausted" in s_content.lower():
                        quota_detected = True
                        quota_sec = parse_quota_reset_seconds(s_content)
                        break

                if not quota_detected:
                    db_quota = inspect_conversation_db_for_quota(convo_id)
                    if db_quota and abs(db_quota.get("step_index", 0) - last_step_idx) <= 2:
                        quota_detected = True
                        quota_sec = db_quota.get("resets_in_seconds")
                        quota_ts = db_quota.get("reset_timestamp")

                if quota_detected:
                    wait_sec = quota_sec or 6853.0
                    if quota_ts:
                        try:
                            dt = datetime.fromisoformat(quota_ts.replace("Z", "+00:00"))
                            cooldown_until = dt.timestamp()
                        except Exception:
                            cooldown_until = file_mtime + wait_sec + 30.0
                    else:
                        cooldown_until = file_mtime + wait_sec + 30.0

                    # If cooldown has expired OR stalled for > 300s without prior live attempts, allow pull-up probe!
                    if now >= cooldown_until or (now - file_mtime > 300 and prior_attempts == 0):
                        stalled.append(
                            StalledSessionInfo(
                                conversation_id=convo_id,
                                transcript_path=transcript_path,
                                last_step_index=last_step_idx,
                                last_error="quota_restored_pull_up",
                                last_error_time=file_mtime,
                                is_subagent=is_subagent,
                                sidecar_slug=sidecar_slug,
                                attempt_count=prior_attempts,
                                can_resuscitate=True,
                                skip_reason=None,
                                is_quota_exhausted=False,
                            )
                        )
                        continue

                    remaining = max(0, int(cooldown_until - now))
                    if self.db:
                        self.db.record_resuscitation(
                            conversation_id=convo_id,
                            sidecar_slug=sidecar_slug,
                            last_error=f"Individual quota reached (Resets in {int(wait_sec)}s)",
                            status="quota_cooldown",
                            cooldown_until=cooldown_until,
                            error_details=f"Quota reset delay {int(wait_sec)}s, timestamp: {quota_ts}",
                            last_step_index=last_step_idx,
                        )
                    stalled.append(
                        StalledSessionInfo(
                            conversation_id=convo_id,
                            transcript_path=transcript_path,
                            last_step_index=last_step_idx,
                            last_error="Individual quota reached",
                            last_error_time=file_mtime,
                            is_subagent=is_subagent,
                            sidecar_slug=sidecar_slug,
                            attempt_count=prior_attempts,
                            can_resuscitate=False,
                            skip_reason=f"quota_cooldown (resets in {remaining}s)",
                            is_quota_exhausted=True,
                            quota_resets_in_seconds=wait_sec,
                            quota_reset_timestamp=quota_ts,
                        )
                    )
                    continue

                # --- 3. Normal Active / Completed State Checks ---
                if last_source == "MODEL" and last_type == "PLANNER_RESPONSE" and last_status == "DONE" and last_content and not last_step.get("tool_calls"):
                    # Check if this session has an in-memory schedule dropped after restart
                    sched_info = extract_active_schedule_from_transcript(transcript_path)
                    if sched_info:
                        if self.db:
                            self.db.save_conversation_schedule(
                                conversation_id=convo_id,
                                cron_expression=sched_info["cron"],
                                prompt=sched_info["prompt"],
                                expected_interval_seconds=sched_info["interval_seconds"],
                                last_trigger_at=sched_info["last_trigger_time"],
                            )
                        interval = sched_info["interval_seconds"]
                        last_trig = sched_info["last_trigger_time"]
                        if last_trig > 0 and (now - last_trig > interval * 1.25) and (now - file_mtime > 300):
                            stalled.append(
                                StalledSessionInfo(
                                    conversation_id=convo_id,
                                    transcript_path=transcript_path,
                                    last_step_index=last_step_idx,
                                    last_error=f"lost_schedule_after_restart (cron: {sched_info['cron']})",
                                    last_error_time=file_mtime,
                                    is_subagent=is_subagent,
                                    sidecar_slug=sidecar_slug,
                                    attempt_count=prior_attempts,
                                    can_resuscitate=True,
                                    has_active_schedule=True,
                                    active_cron_expression=sched_info["cron"],
                                )
                            )
                    continue

                # If the last step is an active user input or running tool, it's not stalled.
                if last_status == "RUNNING" or (last_source in ("USER_EXPLICIT", "USER") and now - file_mtime < self.config.stall_grace_seconds):
                    continue

                is_stalled = False
                matched_error = ""
                is_mcp = False

                # Check if the conversation ended in an empty planner response hang
                if len(parsed_steps) >= 2 and last_source == "MODEL" and last_type == "PLANNER_RESPONSE" and not last_content and not last_step.get("tool_calls"):
                    if now - file_mtime > self.config.stall_grace_seconds:
                        is_stalled = True
                        for s in reversed(parsed_steps[-5:]):
                            t_calls = s.get("tool_calls") or []
                            for tc in t_calls:
                                tc_name = str(tc.get("name") or "").lower()
                                if tc_name.startswith("mcp_") or "mcp" in tc_name:
                                    is_mcp = True
                                    break
                            if "mcp" in str(s.get("content") or "").lower():
                                is_mcp = True
                                break
                        matched_error = "mcp_error_hang" if is_mcp else "empty_planner_response_hang"

                # If not empty hang, inspect steps backwards for unresolved system/error interruptions
                if not is_stalled:
                    for step in reversed(parsed_steps):
                        s_source = step.get("source", "")
                        s_type = step.get("type", "")
                        s_status = step.get("status", "")
                        s_content = str(step.get("content") or "").lower()

                        if s_source == "MODEL" and s_type == "PLANNER_RESPONSE" and s_status == "DONE" and (s_content or step.get("tool_calls")):
                            break

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

                # Circuit breaker
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
                            is_mcp_error=is_mcp,
                        )
                    )
                    continue

                # Stall grace period: recent errors (< stall_grace_seconds) are given time to self-heal
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
                            is_mcp_error=is_mcp,
                        )
                    )
                    continue

                # Eligible for automated resuscitation
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
                        is_mcp_error=is_mcp,
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

        # Determine tailored prompt based on scenario
        if session_info.is_quota_exhausted or session_info.last_error == "quota_restored_pull_up":
            prompt = custom_prompt or QUOTA_RESUSCITATION_PROMPT
        elif session_info.is_mcp_error or "mcp" in str(session_info.last_error).lower():
            prompt = custom_prompt or MCP_ERROR_RESUSCITATION_PROMPT
        elif session_info.has_active_schedule or "schedule" in str(session_info.last_error).lower():
            cron_str = session_info.active_cron_expression or "*/30 * * * *"
            prompt = custom_prompt or SCHEDULE_REMOUNT_PROMPT.format(cron=cron_str)
        elif session_info.last_error == "empty_planner_response_hang":
            prompt = custom_prompt or EMPTY_RESPONSE_RESUSCITATION_PROMPT
        else:
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
                new_st = "schedule_remounted" if session_info.has_active_schedule else "resuscitated"
                self.db.update_resuscitation_status(res_id, new_st)
                if session_info.has_active_schedule and self.db:
                    self.db.update_conversation_schedule_trigger(convo_id, time.time())
                logger.info("Successfully resuscitated session %s (res_id=%s, status=%s)", convo_id, res_id, new_st)
                if self.broker:
                    try:
                        await self.broker.publish("events", {
                            "event": "antigravity_resuscitation",
                            "type": "antigravity_resuscitation",
                            "action": new_st,
                            "resuscitation_id": res_id,
                            "conversation_id": convo_id,
                            "status": new_st,
                            "success": True,
                            "timestamp": time.time(),
                        })
                    except Exception as b_err:
                        logger.debug("Failed publishing resuscitation success event: %s", b_err)
                return {
                    "success": True,
                    "resuscitation_id": res_id,
                    "conversation_id": convo_id,
                    "status": new_st,
                    "output": stdout,
                }
            else:
                # Check if failure was caused by Individual quota reached
                if err and ("individual quota reached" in err.lower() or "resource_exhausted" in err.lower()):
                    wait_sec = parse_quota_reset_seconds(err) or 6853.0
                    cooldown_until = time.time() + wait_sec + 30.0
                    self.db.update_resuscitation_status(
                        res_id,
                        "quota_cooldown",
                        cooldown_until=cooldown_until,
                        error_details=err[:500],
                    )
                    logger.warning(
                        "Session %s hit quota exhaustion during pull-up; quarantined into quota_cooldown for %ds",
                        convo_id,
                        int(wait_sec),
                    )
                    return {
                        "success": False,
                        "resuscitation_id": res_id,
                        "conversation_id": convo_id,
                        "status": "quota_cooldown",
                        "error": err,
                        "cooldown_until": cooldown_until,
                    }

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
                "quota_cooldown": sum(1 for r in recent_resuscitations if r.get("status") == "quota_cooldown"),
                "schedule_remounted": sum(1 for r in recent_resuscitations if r.get("status") == "schedule_remounted"),
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
                    "is_quota_exhausted": s.is_quota_exhausted,
                    "quota_resets_in_seconds": s.quota_resets_in_seconds,
                    "quota_reset_timestamp": s.quota_reset_timestamp,
                    "is_mcp_error": s.is_mcp_error,
                    "has_active_schedule": s.has_active_schedule,
                    "active_cron_expression": s.active_cron_expression,
                }
                for s in stalled
            ],
            "quota_cooldown_sessions": self.db.list_quota_cooldowns() if self.db else [],
            "active_conversation_schedules": self.db.list_active_conversation_schedules() if self.db else [],
            "resuscitation_stats": stats,
            "recent_resuscitations": recent_resuscitations,
        }
