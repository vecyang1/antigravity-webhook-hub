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
import subprocess
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
    "the stream was interrupted",
    "stream was interrupted",
    "there was a network issue connecting to the server",
    "agent executor error",
    "calling model: request failed",
    "agent execution terminated due to error",
    "agent terminated due to error",
    "user location is not supported",
    "location is not supported for the api use",
    "failed_precondition",
    "model output error",
    "503 service unavailable",
    "no capacity available for model",
    "lookup oauth2.googleapis.com",
    "no such host",
    "operation timed out",
    "i/o timeout",
    "failed to connect to server",
    "network issue connecting to the server",
    "stop hook blocked termination",
    "blocked termination",
)

COMPLETION_REPORT_PATTERNS = (
    r"<!--\s*goal_complete\s*-->",
    r"<!--\s*goal_finished\s*-->",
    r"\[(?:goal_complete|goal_finished)\]",
    r"【(?:最终完成汇报|哨兵巡检快照|巡检快照|目标完成|闭环汇报|自愈延续确认报告)】",
    r"(?:###|##|#|\*\*)\s*(?:[🟢✅🛡️🚀🎯🏁✨🎉])\s*(?:[^\n]*)(?:all green|哨兵|巡检|快照|汇报|完成|通过|闭环|已闭环|pass)",
    r"(?:[🟢✅🛡️🚀🎯🏁✨🎉])\s*(?:[^\n]{0,80})(?:all green|哨兵|巡检|快照|汇报|完成|通过|闭环|已闭环|正常|pass)",
    r"(?:任务|目标|工作|指令|要求|需求|sentinel|哨兵|巡检|健康|自愈|全部动作|所有指令).{0,30}(?:已完成|圆满完成|已闭环|顺利完成|执行完毕|安全执行完毕|全部安全执行完毕|完成汇报|100%\s*闭环|闭环完成|核实闭环|全部核实闭环|已达成|all\s+green|全部正常|全绿|全部通过|固化报告|确认报告)",
    r"(?:已全部安全执行完毕|全部安全执行完毕|已全部核实闭环|全部核实闭环)",
    r"\b(?:task|goal|work)\s+(?:has\s+been\s+|is\s+)?(?:completed|complete|finished|closed)\b",
    r"\b(?:task\s+complete|task\s+completed|successfully\s+completed|completed\s+successfully)\b",
    r"(?:13/13|\d+/\d+)\s*(?:pass|passed|通过|正常)",
    r"(?:all\s+\d+\s+checks\s+passed|all\s+checks\s+passed|全部检查通过)",
    r"(?:保持全绿常驻待命|系统保持.*待命|已进入待命)",
)

NETWORK_ERROR_RESUSCITATION_PROMPT = (
    "【系统自动网络/服务故障自愈拉起提醒】\n"
    "检测到底层与模型服务通信发生偶发网络中断、地域路由抖动或超时（Network Issue / User Location / Server Error）。\n"
    "现网络与接口通信已恢复正常。\n"
    "请越过偶发网络与终端中断报错，检查上一步执行进展并直接继续推进原定任务计划（请全中文汇报进展）。"
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

BOOST_DELEGATION_RESUSCITATION_PROMPT = (
    "【系统自动 Boost/Delegation 委派协同拉起提醒】\n"
    "检测到当前任务处于 Boost/Delegation 多 Agent 委派协同状态。\n"
    "此前委派的子 Agent（会话 ID: {subagent_id}）曾因配额用尽或服务重启中断。\n"
    "【关键环境状态更新】：\n"
    "1. 当前账户已切换/恢复至健康可用状态（当前活跃账户: {active_account}，配额充足已解除限制）。\n"
    "2. 请切勿降级为 Solo 模式自行执行子任务，请保持 Boost 架构与分工！\n"
    "3. 请直接通过 send_message 向子 Agent（{subagent_id}）发送指令唤醒并继续委派，或重新调用 invoke_subagent 继续推进原定协同任务（请全中文汇报进展）。"
)

BOOST_GOAL_RESUSCITATION_PROMPT = (
    "【系统自动自愈拉起提醒：/boost 目标自治与协同推进延续】\n"
    "检测到本会话正在执行 /boost、/goal 或多 Agent 协同任务，此前因偶发网络超时/流中断、工具报错或 Stop Hook 阻断暂留。\n"
    "⚠️ 关键执行纪律（严禁降级 Solo 模式）：\n"
    "1. 本会话处于高阶自治推进状态，严禁退化为单兵等待或打假卡，继续贯彻团队/委派协同推进。\n"
    "2. 若遇到 ⚠️ MCP 报错或连接异常，切勿陷入重复重试死循环。根据《统一工具链与原生收敛规范》，请直接降级使用原生 CLI 命令行、系统脚本或标准 API 绕过 MCP 摩擦点。\n"
    "3. 若遇到 Stop Hook 阻断，说明任务目标尚未完全闭环。请依据最新排查结果与项目规范，持续推进直至完全交付并产出真实闭环凭据。\n"
    "请全中文汇报当前诊断与下一步执行计划，并立即继续执行。"
)

TOOL_RESULT_RESUSCITATION_PROMPT = (
    "【系统自动工具响应自愈拉起】\n"
    "检测到上一步工具已执行完成并返回结果，但底层模型推理中断未触发后续回复生成。\n"
    "请越过偶发中断，根据上一步工具返回的结果以及原定任务计划，直接继续推进下一步（请全中文汇报进展）。"
)

PARENT_DELEGATION_COMPLETED_PROMPT = (
    "【系统自动拉起：子代理完成通知】\n"
    "检测到本会话此前委派的子 Agent（ID: {subagent_id}）已完成执行，但其完成通知因系统重启或 IPC 偶发丢包未送达。\n"
    "子 Agent 最新执行汇报如下：\n"
    "----------------------------------------\n"
    "{subagent_summary}\n"
    "----------------------------------------\n"
    "请根据子 Agent 的上述成果继续推进下一步、更新 walkthrough.md 成果记录，并在任务完成后以 <!-- GOAL_COMPLETE --> 闭环活跃目标（请全中文汇报进展）。"
)

SERVER_RESTART_RESUSCITATION_PROMPT = (
    "【系统自动自愈拉起：服务重启任务恢复】\n"
    "检测到 Antigravity 应用/服务此前已重启，本会话正在推进的任务已自动恢复连接。\n"
    "请越过重启中断，检查上一步执行进展并直接继续推进原定任务计划（请全中文汇报进展）。"
)

BOOST_SERVER_RESTART_RESUSCITATION_PROMPT = (
    "【系统自动自愈拉起：/boost 服务重启延续】\n"
    "检测到 Antigravity 应用/服务此前已重启，未完成的 /boost、/goal 或多 Agent 协同任务已自动恢复连接。\n"
    "⚠️ 关键执行纪律（严禁降级 Solo 模式）：\n"
    "1. 本会话处于高阶自治与团队协同推进状态，严禁退化为单兵等待或打假卡，继续贯彻团队/委派协同推进。\n"
    "2. 请检查此前委派的子 Agent 执行状态与最新记录（如 walkthrough.md）。若子 Agent 被中断，请直接通过 send_message 或 invoke_subagent 继续唤醒推进。\n"
    "3. 请全中文汇报当前进展与下一步执行计划，并直接继续执行原定任务。"
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
    parent_conversation_id: Optional[str] = None
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
    is_boost_goal: bool = False
    is_stop_hook_hang: bool = False
    completed_child_id: Optional[str] = None
    completed_child_summary: Optional[str] = None
    is_network_issue: bool = False
    is_server_restart: bool = False


def check_session_has_boost_or_goal(transcript_path: Path, parsed_steps: list[dict[str, Any]]) -> bool:
    """
    Determine whether a session is operating in /boost, /goal, /teamwork-preview,
    or multi-agent delegation mode, even if it is a root conversation.
    Filters out automated watchdog prompts to prevent self-contamination.
    """
    for s in parsed_steps:
        cnt = str(s.get("content") or "")
        # Filter out automated watchdog resuscitation prompts to avoid self-contamination
        if "【系统自动" in cnt or "【系统后台" in cnt or "自愈拉起" in cnt or "服务重启延续" in cnt:
            continue
        cnt_lower = cnt.lower()
        if any(k in cnt_lower for k in ("/boost", "/goal", "/teamwork-preview", "delegated agents")):
            return True
        if "stop hook blocked termination" in cnt_lower:
            return True
        for tc in s.get("tool_calls") or []:
            tc_name = str(tc.get("name") or "").lower()
            if tc_name in ("invoke_subagent", "define_subagent"):
                return True

    # Check first line (step 0 prompt) if loaded window didn't capture it
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as fp:
            first_line = fp.readline()
            if first_line:
                first_data = json.loads(first_line)
                first_content = str(first_data.get("content") or "")
                if not ("【系统自动" in first_content or "【系统后台" in first_content or "自愈拉起" in first_content):
                    first_content_lower = first_content.lower()
                    if any(k in first_content_lower for k in ("/boost", "/goal", "/teamwork-preview", "delegated agents")):
                        return True
    except Exception:
        pass

    return False


def check_session_claimed_completion(
    transcript_path: Optional[Path] = None,
    parsed_steps: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """
    Check if a session has explicitly claimed completion.
    A session is considered completed if:
    1. Any step in the window contains explicit completion marker (<!-- goal_complete -->)
       that was not followed by subsequent user prompts.
    2. The last substantive MODEL turn contains completion signatures (e.g. All Green sentinel snapshot,
       13/13 PASS, task completion report, 100% 闭环, etc.) without active tool calls.
    """
    steps = parsed_steps if parsed_steps is not None else []
    if not steps and transcript_path and transcript_path.exists():
        steps = []
        for line in tail_transcript_lines(transcript_path, max_lines=30):
            line_s = line.strip()
            if line_s:
                try:
                    steps.append(json.loads(line_s))
                except Exception:
                    pass

    if not steps:
        return False

    # 1. First check if any step contains explicit goal_complete marker
    last_user_idx = -1
    last_complete_idx = -1
    for idx, s in enumerate(steps):
        s_src = s.get("source", "")
        if s_src in ("USER_EXPLICIT", "USER"):
            last_user_idx = idx
        # CRITICAL PROTECTION: Only MODEL steps can claim completion!
        # SYSTEM messages (e.g. system prompts instructing agent to use <!-- GOAL_COMPLETE -->)
        # and USER prompts MUST NEVER be treated as agent completion!
        if s_src == "MODEL":
            cnt_lower = str(s.get("content") or "").lower()
            if "<!-- goal_complete -->" in cnt_lower or "<!-- goal_finished -->" in cnt_lower or "[goal_complete]" in cnt_lower:
                last_complete_idx = idx

    if last_complete_idx != -1 and last_complete_idx > last_user_idx:
        return True

    # 2. Check for unanswered user message at the end
    # Walk backwards to find the last substantive turn (skipping checkpoints, ephemeral messages, system notifications)
    last_model_step = None
    has_subsequent_user = False
    for s in reversed(steps):
        s_src = s.get("source", "")
        s_type = s.get("type", "")
        if s_src in ("USER_EXPLICIT", "USER"):
            has_subsequent_user = True
            break
        if s_src == "MODEL" and s_type == "PLANNER_RESPONSE":
            last_model_step = s
            break

    if has_subsequent_user or not last_model_step:
        return False

    last_status = last_model_step.get("status", "")
    last_content = str(last_model_step.get("content") or "")
    has_tool_calls = bool(last_model_step.get("tool_calls"))

    if last_status == "DONE" and not has_tool_calls:
        cnt_clean = last_content.strip()
        for pat in COMPLETION_REPORT_PATTERNS:
            if re.search(pat, cnt_clean, re.IGNORECASE | re.DOTALL):
                return True

    return False


def tail_transcript_lines(file_path: Path, max_lines: int = 15) -> list[str]:
    """Read the last N lines of a transcript file efficiently."""
    lines: list[str] = []
    if not file_path or not file_path.exists():
        return lines
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


_subagent_id_cache: dict[str, tuple[float, int, list[str]]] = {}


def extract_subagent_ids_from_transcript(transcript_path: Path, current_convo_id: str) -> list[str]:
    """
    Extract all unique subagent conversation IDs spawned or referenced by this session.
    Inspects tool_calls, conversationId JSON blocks, conversation:// links, and subagent mentions.
    Streams line-by-line with mtime/size caching to prevent repetitive file reads and high RSS churn.
    """
    child_ids: list[str] = []
    if not transcript_path or not transcript_path.exists():
        return child_ids

    cache_key = f"{transcript_path}:{current_convo_id}"
    mtime, size = 0.0, 0
    try:
        st = transcript_path.stat()
        mtime, size = st.st_mtime, st.st_size
        cached = _subagent_id_cache.get(cache_key)
        if cached is not None and cached[0] == mtime and cached[1] == size:
            return list(cached[2])
    except Exception:
        pass

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if (
                    "conversationId" not in line
                    and "conversation://" not in line
                    and "subagent" not in line
                ):
                    continue
                for m in re.finditer(r'["\']conversationId["\']\s*:\s*["\']([0-9a-zA-Z-]{20,45})["\']', line):
                    cid = m.group(1).strip()
                    if cid != current_convo_id and cid not in child_ids:
                        child_ids.append(cid)
                for m in re.finditer(r'conversation://([0-9a-zA-Z-]{20,45})', line):
                    cid = m.group(1).strip()
                    if cid != current_convo_id and cid not in child_ids:
                        child_ids.append(cid)
                for m in re.finditer(r'subagent\s*\(?[`\'"]?([0-9a-fA-F-]{36})[`\'"]?\)?', line, re.IGNORECASE):
                    cid = m.group(1).strip()
                    if cid != current_convo_id and cid not in child_ids:
                        child_ids.append(cid)
    except Exception as e:
        logger.debug("Failed extracting subagent IDs from %s: %s", transcript_path, e)

    if len(_subagent_id_cache) > 256:
        _subagent_id_cache.clear()
    if mtime > 0:
        _subagent_id_cache[cache_key] = (mtime, size, list(child_ids))

    return child_ids


def get_language_server_start_time(pid: Optional[int]) -> Optional[float]:
    """Get the start time (epoch seconds) of language_server process on macOS / Unix."""
    if not pid:
        return None
    try:
        env = dict(os.environ)
        env["LC_ALL"] = "C"
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
            env=env,
        ).strip()
        if out:
            # Format: 'Thu Sep 24 09:28:14 2026'
            return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except Exception as e:
        logger.debug("Failed getting lstart for pid %s: %s", pid, e)
    return None


def get_unfinished_conversations_from_summaries(
    summaries_db_path: Optional[Path] = None,
) -> dict[str, dict[str, Any]]:
    """
    Query ~/.gemini/antigravity/conversation_summaries.db for conversations
    flagged as CASCADE_RUN_STATUS_RUNNING or not_fully_idle = 1 (authoritative Antigravity state).
    """
    db_file = summaries_db_path or Path(os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db"))
    if not db_file.exists():
        return {}

    unfinished: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT conversation_id, title, status, not_fully_idle, killed, parent_conversation_id, last_modified_time, step_count
            FROM conversation_summaries
            WHERE (status = 'CASCADE_RUN_STATUS_RUNNING' OR not_fully_idle = 1)
              AND (killed IS NULL OR killed = 0)
            """
        )
        rows = cur.fetchall()
        conn.close()
        for row in rows:
            cid = str(row[0])
            mod_ts = 0.0
            if row[6]:
                try:
                    ts_str = str(row[6]).replace(" ", "T")
                    dt = datetime.fromisoformat(ts_str if "+" in ts_str else ts_str + "+00:00")
                    mod_ts = dt.timestamp()
                except Exception:
                    pass
            unfinished[cid] = {
                "conversation_id": cid,
                "title": row[1] or "",
                "status": row[2] or "",
                "not_fully_idle": bool(row[3]),
                "killed": bool(row[4]),
                "parent_conversation_id": row[5] or None,
                "last_modified_time": mod_ts,
                "step_count": row[7] or 0,
            }
    except Exception as e:
        logger.debug("Failed querying conversation_summaries.db: %s", e)
    return unfinished


def is_zombie_or_archived_subagent(
    subagent_id: str,
    parent_id: Optional[str] = None,
    brain_dir: Optional[Path] = None,
    now: Optional[float] = None,
    ls_restart_time: float = 0.0,
    summaries_db_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """
    Evaluate whether a subagent is dead, terminated, killed, superseded, or archived.
    Dead subagents are immune to resuscitation and must NEVER trigger resuscitation
    or delegation prompts back into their parent session.

    Principles & Checks:
    1. Authoritative DB status: If conversation_summaries.db flags killed=1,
       or status is finished/completed and not_fully_idle=0, it is dead.
    2. Explicit completion: If the subagent's transcript contains <!-- GOAL_COMPLETE -->
       or check_session_claimed_completion returns True, it completed its job.
    3. Parent termination: If the parent's transcript contains an explicit kill command
       (e.g. manage_subagents action=kill, manage_task kill, or 'Successfully killed' for this subagent).
    4. Turn supersession: If the parent received a subsequent USER / USER_INPUT / USER_EXPLICIT turn
       after the subagent was spawned or last active, the subagent belongs to a superseded earlier turn.
    5. Parent already completed: If parent's transcript indicates overall goal completion.
    6. Language server restart: If subagent mtime predates ls_restart_time.
    """
    if now is None:
        now = time.time()

    # 1. Authoritative SQLite conversation_summaries.db check
    s_db = summaries_db_path or Path(os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db"))
    if s_db.exists():
        try:
            conn = sqlite3.connect(f"file:{s_db}?mode=ro", uri=True, timeout=1.0)
            cur = conn.cursor()
            cur.execute(
                "SELECT killed, status, not_fully_idle, parent_conversation_id FROM conversation_summaries WHERE conversation_id = ?",
                (subagent_id,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                killed_flag, status_str, not_fully_idle, p_id_db = row
                if killed_flag:
                    return True, "killed_in_conversation_summaries"
                if status_str in ("CASCADE_RUN_STATUS_COMPLETED", "CASCADE_RUN_STATUS_FINISHED") and not not_fully_idle:
                    return True, f"completed_in_conversation_summaries ({status_str})"
                if not parent_id and p_id_db:
                    parent_id = str(p_id_db)
        except Exception as e:
            logger.debug("Failed querying conversation_summaries.db for %s: %s", subagent_id, e)

    # 2. Check subagent transcript and verify subagent identity
    b_dir = brain_dir or Path(os.path.expanduser("~/.gemini/antigravity/brain"))
    sub_path = resolve_transcript_path(subagent_id, b_dir)

    is_sub = bool(parent_id)
    if not is_sub and sub_path and sub_path.exists():
        parent_id = detect_parent_conversation_id(subagent_id, transcript_path=sub_path)
        if parent_id:
            is_sub = True
        else:
            try:
                with open(sub_path, "r", encoding="utf-8", errors="ignore") as f:
                    first_line = f.readline(4096)
                if any(k in first_line for k in ("<subagent_reminder>", "You are running as a subagent", "invoked by a caller agent", "<original_task>", "invoke_subagent")):
                    is_sub = True
            except Exception:
                pass

    if not is_sub and not parent_id:
        return False, "not_a_subagent"

    if not sub_path or not sub_path.exists():
        sub_dir = b_dir / subagent_id
        if not sub_dir.exists():
            return True, "subagent_directory_not_found"
        try:
            d_mtime = sub_dir.stat().st_mtime
            if ls_restart_time > 0 and d_mtime < ls_restart_time:
                return True, f"subagent_predates_server_restart (mtime={int(d_mtime)} < restart={int(ls_restart_time)})"
            if (now - d_mtime) > 900:
                return True, f"subagent_empty_and_stale ({int(now - d_mtime)}s old)"
        except Exception:
            pass
    else:
        try:
            sub_mtime = sub_path.stat().st_mtime
            if ls_restart_time > 0 and sub_mtime < ls_restart_time:
                return True, f"subagent_stopped_by_server_restart (mtime={int(sub_mtime)} < restart={int(ls_restart_time)})"
            if check_session_claimed_completion(sub_path, None):
                return True, "subagent_goal_completed"
        except Exception as e:
            logger.debug("Failed checking subagent transcript %s: %s", sub_path, e)

    # 3. Check Parent relationship if parent_id is available
    if parent_id:
        p_path = resolve_transcript_path(parent_id, b_dir)
        if p_path and p_path.exists():
            # 3a. Parent already completed overarching task
            if check_session_claimed_completion(p_path, None):
                return True, "parent_already_completed"

            try:
                p_text = p_path.read_text(encoding="utf-8", errors="ignore")

                # 3b. Check for explicit parent kill commands
                if (
                    ("manage_subagents" in p_text and subagent_id in p_text and ('"kill"' in p_text or "'kill'" in p_text))
                    or ("manage_task" in p_text and subagent_id in p_text and ('"kill"' in p_text or "'kill'" in p_text))
                    or ("Successfully killed" in p_text and subagent_id in p_text)
                    or ("Killed roles:" in p_text and subagent_id in p_text)
                ):
                    return True, "parent_explicitly_killed_subagent"

                # 3c. Turn supersession:
                # Find the first line where the subagent was spawned / referenced by parent
                lines = p_text.splitlines()
                subagent_first_seen_line = -1
                latest_user_line = -1
                for idx, line in enumerate(lines):
                    if subagent_id in line and subagent_first_seen_line == -1:
                        subagent_first_seen_line = idx
                    if (
                        '"type":"USER_INPUT"' in line
                        or '"type": "USER_INPUT"' in line
                        or '"source":"USER_EXPLICIT"' in line
                        or '"source": "USER_EXPLICIT"' in line
                        or '"type":"USER"' in line
                        or '"type": "USER"' in line
                    ):
                        latest_user_line = idx

                if subagent_first_seen_line != -1 and latest_user_line > subagent_first_seen_line:
                    return True, f"subagent_superseded_by_subsequent_user_turn (spawn_line={subagent_first_seen_line} < latest_user_line={latest_user_line})"
            except Exception as ex:
                logger.debug("Failed inspecting parent transcript %s: %s", p_path, ex)

    return False, ""


def is_subagent_active(
    cid: str,
    brain_dir: Path,
    quiet_seconds: float = 900.0,
    now: Optional[float] = None,
    ls_restart_time: float = 0.0,
    parent_id: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Check if a child subagent is actively working.
    Returns (is_active, reason).
    
    Principles:
    1. If subagent is dead/zombie/killed/superseded (is_zombie_or_archived_subagent), it is NOT active.
    2. If language_server restarted and subagent was last modified BEFORE restart,
       it was terminated by the server restart and cannot be running on the current server.
    3. If the subagent's transcript was modified within quiet_seconds (default 15m),
       and it has NOT explicitly completed with <!-- GOAL_COMPLETE -->, it is ACTIVE.
    4. If the subagent's last step status is "RUNNING", it is ACTIVE.
    5. Only when the subagent explicitly completed (<!-- GOAL_COMPLETE --> or final shutdown),
       or the entire session has been silent for > quiet_seconds with no running tasks,
       is it considered inactive.
    """
    if now is None:
        now = time.time()

    is_zombie, z_reason = is_zombie_or_archived_subagent(
        subagent_id=cid,
        parent_id=parent_id,
        brain_dir=brain_dir,
        now=now,
        ls_restart_time=ls_restart_time,
    )
    if is_zombie:
        return False, z_reason

    c_path = resolve_transcript_path(cid, brain_dir)
    if not c_path or not c_path.exists():
        c_dir = brain_dir / cid
        if c_dir.exists():
            try:
                d_mtime = c_dir.stat().st_mtime
                if ls_restart_time > 0 and d_mtime < ls_restart_time:
                    return False, f"subagent_directory_predates_server_restart (mtime={int(d_mtime)} < restart={int(ls_restart_time)})"
                if now - d_mtime < quiet_seconds:
                    return True, f"subagent_directory_recently_created ({int(now - d_mtime)}s ago)"
            except Exception:
                pass
        return False, "transcript_not_found"

    try:
        c_mtime = c_path.stat().st_mtime
    except Exception:
        return False, "stat_failed"

    # If subagent was last modified BEFORE language_server restarted,
    # its process was killed by the server restart and cannot be running on the current server.
    if ls_restart_time > 0 and c_mtime < ls_restart_time:
        return False, f"subagent_stopped_by_server_restart (mtime={int(c_mtime)} < restart={int(ls_restart_time)})"

    time_since_mod = now - c_mtime

    tail_lines = tail_transcript_lines(c_path, max_lines=15)
    if not tail_lines:
        if time_since_mod < quiet_seconds:
            return True, f"empty_transcript_recent_mod ({int(time_since_mod)}s ago)"
        return False, "empty_transcript_stale"

    parsed_tail = []
    for line in tail_lines:
        try:
            parsed_tail.append(json.loads(line.strip()))
        except Exception:
            continue

    if not parsed_tail:
        if time_since_mod < quiet_seconds:
            return True, f"recent_mod_unparsed ({int(time_since_mod)}s ago)"
        return False, "unparseable_transcript"

    last_step = parsed_tail[-1]
    last_status = last_step.get("status")

    # A. Check for explicit goal completion
    has_goal_complete = False
    for step in reversed(parsed_tail):
        cnt = str(step.get("content") or "").lower()
        if "<!-- goal_complete -->" in cnt:
            has_goal_complete = True
            break

    if has_goal_complete:
        return False, "subagent_goal_completed"

    # B. Actively running step
    if last_status == "RUNNING":
        if time_since_mod < quiet_seconds:
            return True, f"subagent_step_running (step {last_step.get('step_index', '?')})"
        return False, f"subagent_running_step_stale ({int(time_since_mod)}s >= {int(quiet_seconds)}s)"

    # C. Within quiet_seconds window and not explicitly completed -> actively running
    if time_since_mod < quiet_seconds:
        return True, f"subagent_actively_working ({int(time_since_mod)}s ago < {int(quiet_seconds)}s)"

    return False, f"subagent_silent_timeout ({int(time_since_mod)}s >= {int(quiet_seconds)}s)"


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


def inspect_conversation_db_for_terminal_network_error(
    convo_id: str,
    conversations_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """
    Inspect SQLite database in ~/.gemini/antigravity/conversations/<id>.db for terminal errors
    recorded in step_type = 17 (e.g. 'There was a network issue connecting to the server',
    'agent executor error: calling model: request failed', dial tcp timeouts, DNS lookup failures).
    These error steps are stored in the DB while often skipped or truncated in transcript.jsonl.
    """
    c_dir = conversations_dir or Path(os.path.expanduser("~/.gemini/antigravity/conversations"))
    db_file = c_dir / f"{convo_id}.db"
    if not db_file.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT idx, step_payload FROM steps WHERE step_type = 17 ORDER BY idx DESC LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            return None

        payload_bytes = row[1]
        if not payload_bytes:
            return None
        payload_str = payload_bytes.decode("utf-8", errors="ignore")
        p_lower = payload_str.lower()

        has_terminal_issue = (
            "there was a network issue connecting to the server" in p_lower
            or "agent executor error: calling model: request failed" in p_lower
            or "agent executor error" in p_lower
            or ("dial tcp" in p_lower and ("operation timed out" in p_lower or "i/o timeout" in p_lower or "no such host" in p_lower or "connect" in p_lower))
            or "lookup oauth2.googleapis.com" in p_lower
            or "no capacity available for model" in p_lower
            or "agent execution terminated due to error" in p_lower
            or "agent terminated due to error" in p_lower
            or "user location is not supported" in p_lower
            or "location is not supported for the api use" in p_lower
            or "failed_precondition" in p_lower
            or "model output error" in p_lower
            or "503 service unavailable" in p_lower
            or "broken pipe" in p_lower
            or "stream was interrupted" in p_lower
            or "the stream was interrupted" in p_lower
            or "streamgeneratecontent?alt=sse" in p_lower
        )

        if has_terminal_issue:
            # Error ID is formatted as <trajectory_id>-<step_index>. In protobuf byte stream,
            # the step_index digits may immediately precede next field tag byte (e.g. \x38 / '8').
            # Prioritize matching exact step_index from row[0].
            m_exact = re.search(rf'([0-9a-fA-F-]{{36}})-({row[0]})', payload_str)
            if m_exact:
                error_id = f"{m_exact.group(1)}-{row[0]}"
            else:
                m_eid = re.search(r'([0-9a-fA-F-]{36}-\d+)', payload_str)
                error_id = m_eid.group(1) if m_eid else None
            return {
                "step_index": row[0],
                "error": "network_issue_server_error",
                "error_id": error_id,
                "details": "Agent execution terminated due to error / network issue connecting to the server",
            }
    except Exception as e:
        logger.debug("Error checking conversation db %s for terminal network error: %s", convo_id, e)
    return None


def parse_cron_interval_seconds(cron_expr: str, default_interval: int = 1800) -> int:
    """
    Parse a standard 5-part cron expression into approximate recurrence interval in seconds.
    Supports standard intervals:
      - '* * * * *' -> 60s (every minute)
      - '*/5 * * * *' -> 300s (every 5 min)
      - '*/15 * * * *' -> 900s (every 15 min)
      - '*/30 * * * *' -> 1800s (every 30 min)
      - '0 * * * *' or '15 * * * *' -> 3600s (hourly)
      - '0 */2 * * *' -> 7200s (every 2 hours)
      - '0 */4 * * *' -> 14400s (every 4 hours)
      - '0 0 * * *' or '0 9 * * *' -> 86400s (daily)
      - '0,30 * * * *' -> 1800s (twice an hour)
    """
    if not cron_expr:
        return default_interval

    clean = cron_expr.strip()

    parts = clean.split()
    if len(parts) >= 5:
        minute_part, hour_part, dom, month, dow = parts[:5]

        # 1. Every minute
        if minute_part == "*" and hour_part == "*":
            return 60

        # 2. Every N minutes: */N * * * *
        m_step = re.match(r'^\*/(\d+)$', minute_part)
        if m_step and hour_part == "*":
            return max(60, int(m_step.group(1)) * 60)

        # 3. List of minutes, e.g. 0,30 * * * *
        if "," in minute_part and hour_part == "*":
            try:
                mins = sorted([int(x) for x in minute_part.split(",") if x.strip().isdigit()])
                if len(mins) > 1:
                    diffs = [mins[i + 1] - mins[i] for i in range(len(mins) - 1)]
                    diffs.append(60 - mins[-1] + mins[0])
                    avg_gap = sum(diffs) / len(diffs)
                    return max(60, int(avg_gap * 60))
            except Exception:
                pass

        # 4. Hourly: N * * * * (e.g. 0 * * * * or 30 * * * *)
        if minute_part.isdigit() and hour_part == "*":
            return 3600

        # 5. Every N hours: 0 */N * * *
        h_step = re.match(r'^\*/(\d+)$', hour_part)
        if h_step and minute_part.isdigit():
            return max(3600, int(h_step.group(1)) * 3600)

        # 6. Weekly: N N * * N (e.g. 0 0 * * 1)
        if minute_part.isdigit() and hour_part.isdigit() and dow != "*":
            return 86400 * 7

        # 7. Daily: N N * * * (e.g. 0 0 * * * or 0 9 * * *)
        if minute_part.isdigit() and hour_part.isdigit() and dom == "*" and month == "*" and dow == "*":
            return 86400

    # Fallback to regex search for */N if not standard 5-part
    m = re.search(r'\*/(\d+)', clean)
    if m:
        return max(60, int(m.group(1)) * 60)

    return default_interval


def detect_parent_conversation_id(
    convo_id: str,
    conversations_dir: Optional[Path] = None,
    agentapi: Optional[Any] = None,
    transcript_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Detect parentConversationId for a given session.
    1. Check SQLite db ~/.gemini/antigravity/conversations/<convo_id>.db:
       a. Check parent_references table (authoritative SSOT for Antigravity subagents).
       b. Fallback to trajectory_metadata_blob for field 5 protobuf tag (b'\\*\\$([0-9a-fA-F-]{36})').
    2. Fallback to transcript header scanning (up to 128KB) for caller agent ID.
    """
    # 1. SQLite DB check
    c_dir = conversations_dir or Path(os.path.expanduser("~/.gemini/antigravity/conversations"))
    db_file = c_dir / f"{convo_id}.db"
    if db_file.exists():
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=1.0)
            cur = conn.cursor()
            # 1a. Authoritative Antigravity parent_references table
            try:
                cur.execute("SELECT data FROM parent_references ORDER BY idx DESC LIMIT 1")
                row_pr = cur.fetchone()
                if row_pr and row_pr[0]:
                    m_pr = re.search(rb"\n\$([0-9a-fA-F-]{36})", row_pr[0])
                    if m_pr:
                        conn.close()
                        return m_pr.group(1).decode("ascii")
            except Exception as e_pr:
                logger.debug("Error querying parent_references in db: %s", e_pr)

            # 1b. Trajectory metadata blob fallback
            cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                m = re.search(rb"\*\$([0-9a-fA-F-]{36})", row[0])
                if m:
                    return m.group(1).decode("ascii")
        except Exception as e:
            logger.debug("Error checking conversation db for parent id: %s", e)

    # 2. Transcript check
    if transcript_path and transcript_path.exists():
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(131072)
                m = re.search(r'caller agent\s*\([^)]*id:\s*\\?["\']?([0-9a-fA-F-]{36})', head, re.IGNORECASE)
                if m:
                    return m.group(1)
                m_sub = re.search(r'invoked by a caller agent \(name: [^,]+,\s*id:\s*\\?["\']?([0-9a-fA-F-]{36})', head, re.IGNORECASE)
                if m_sub:
                    return m_sub.group(1)
                m_convo = re.search(r'parentConversationId\\?["\s:]+([0-9a-fA-F-]{36})', head)
                if m_convo:
                    return m_convo.group(1)
        except Exception:
            pass

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
        last_schedule_time = 0.0
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
                                ts_str = obj.get("created_at")
                                if ts_str:
                                    try:
                                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                        last_schedule_time = dt.timestamp()
                                    except Exception:
                                        pass
                                break
                except Exception:
                    pass

            if any(k in line_clean for k in ("Cron trigger #", "巡检汇报", "(iteration ", "/task-", "CRON_TRIGGER")) or (last_schedule and last_schedule.get("prompt") and last_schedule["prompt"][:25] in line_clean):
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
            interval = parse_cron_interval_seconds(cron_expr)
            return {
                "cron": cron_expr,
                "prompt": last_schedule["prompt"],
                "interval_seconds": interval,
                "last_trigger_time": last_trigger_time,
                "last_schedule_time": last_schedule_time,
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
        quota_sentinel: Optional[Any] = None,
    ):
        self.db = db
        self.config = config or AntigravityWatchdogConfig()
        self._agentapi_client = agentapi_client
        self.broker = broker
        self._quota_sentinel = quota_sentinel
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
        self._conversations_dir = Path(
            getattr(self.config, "conversations_dir", None)
            or os.environ.get("ANTIGRAVITY_CONVERSATIONS_DIR")
            or os.path.expanduser("~/.gemini/antigravity/conversations")
        )
        self._summaries_db_path = Path(
            getattr(self.config, "summaries_db_path", None)
            or os.environ.get("ANTIGRAVITY_SUMMARIES_DB_PATH")
            or (self._conversations_dir.parent / "conversation_summaries.db")
        )
        self.last_known_ls_pid: Optional[int] = None
        self.ls_restart_time: float = 0.0
        if self.db and hasattr(self.db, "get_metadata"):
            saved_pid = self.db.get_metadata("last_known_ls_pid")
            if saved_pid and saved_pid.isdigit():
                self.last_known_ls_pid = int(saved_pid)
            saved_rt = self.db.get_metadata("ls_restart_time")
            if saved_rt:
                try:
                    self.ls_restart_time = float(saved_rt)
                except ValueError:
                    pass
        self._in_flight_resuscitations: set[str] = set()
        self._recently_resuscitated: dict[str, float] = {}

    @property
    def quota_sentinel(self):
        if self._quota_sentinel is None:
            try:
                from hub.antigravity.quota_sentinel import AntigravityQuotaSentinel
                self._quota_sentinel = AntigravityQuotaSentinel(db=self.db, broker=self.broker)
            except Exception as e:
                logger.debug("Auto-initializing default AntigravityQuotaSentinel skipped: %s", e)
            return self._quota_sentinel
        return self._quota_sentinel

    @quota_sentinel.setter
    def quota_sentinel(self, value):
        self._quota_sentinel = value

    def check_language_server_lifecycle(self) -> tuple[bool, Optional[int], Optional[int]]:
        """
        Monitor language_server standalone process PID.
        When PID changes, in-memory Node.js timers and schedules are wiped,
        and unclosed tasks are stopped.
        Returns (pid_changed, current_pid, old_pid).
        """
        raw_pid = self.agentapi.get_language_server_pid() if hasattr(self.agentapi, "get_language_server_pid") else None
        current_pid: Optional[int] = None
        if raw_pid is not None:
            try:
                if isinstance(raw_pid, (int, str)):
                    parsed = int(raw_pid)
                    current_pid = parsed if parsed > 1 else None
            except (ValueError, TypeError):
                current_pid = None

        if not current_pid:
            return False, None, self.last_known_ls_pid

        old_pid = self.last_known_ls_pid
        start_t = get_language_server_start_time(current_pid)

        if old_pid is not None and current_pid != old_pid:
            logger.warning(
                "Antigravity language_server restarted! PID changed from %d to %d. In-memory timers wiped and unclosed tasks stopped.",
                old_pid, current_pid,
            )
            self.last_known_ls_pid = current_pid
            self.ls_restart_time = start_t or time.time()
            if self.db and hasattr(self.db, "set_metadata"):
                self.db.set_metadata("last_known_ls_pid", str(current_pid))
                self.db.set_metadata("ls_restart_time", str(self.ls_restart_time))
            return True, current_pid, old_pid

        if old_pid is None:
            self.last_known_ls_pid = current_pid
            if not self.ls_restart_time:
                self.ls_restart_time = start_t or 0.0
            if self.db and hasattr(self.db, "set_metadata"):
                self.db.set_metadata("last_known_ls_pid", str(current_pid))
                if self.ls_restart_time > 0:
                    self.db.set_metadata("ls_restart_time", str(self.ls_restart_time))

        return False, current_pid, old_pid

    def check_and_clear_quota_cooldowns(self) -> tuple[bool, Optional[str], int]:
        """
        Check if the active account has healthy quota (>10%).
        If so, automatically clear active quota_cooldown locks in SQLite SSOT.
        Returns (is_healthy, active_email, cleared_count).
        """
        if not self.quota_sentinel:
            return False, None, 0

        try:
            is_healthy, active_email, details = self.quota_sentinel.is_active_account_healthy(threshold=0.10)
            if is_healthy and self.db and hasattr(self.db, "clear_quota_cooldown"):
                cleared = self.db.clear_quota_cooldown()
                if cleared > 0:
                    logger.info(
                        "Antigravity Quota Sentinel verified healthy active account (%s). Automatically cleared %d quota_cooldown lock(s).",
                        active_email, cleared,
                    )
                return True, active_email, cleared
            return is_healthy, active_email, 0
        except Exception as e:
            logger.debug("Failed checking active account quota health: %s", e)
            return False, None, 0

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

        is_connected = False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()
            is_connected = True
        except Exception:
            # Fallback probe to secondary DNS (8.8.8.8)
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect(("8.8.8.8", 53))
                sock.close()
                is_connected = True
            except Exception as e:
                logger.debug("Network health check failed: %s", e)
                return False

        if not is_connected:
            return False

        # Verify DNS resolution for Google API endpoints
        try:
            socket.getaddrinfo("daily-cloudcode-pa.googleapis.com", 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except (socket.gaierror, socket.herror) as dns_err:
            logger.debug("DNS resolution probe failed for Google endpoints: %s", dns_err)
            return False
        except Exception:
            pass

        return True

    def is_agentapi_ready(self) -> bool:
        """Check if agentapi binary is executable and language_server is available."""
        if not self.agentapi.is_available():
            return False
        addr, _ = self.agentapi.ensure_credentials(force=False)
        return bool(addr)

    def _tail_transcript_lines(self, file_path: Path, max_lines: int = 15) -> list[str]:
        """Read the last N lines of a transcript file efficiently."""
        return tail_transcript_lines(file_path, max_lines=max_lines)

    def _find_associated_sidecar(self, conversation_id: str) -> Optional[str]:
        """Match conversation_id to any scheduled task or sidecar run with TTL caching."""
        if not self._sidecar_data_dir.exists():
            return None

        now = time.time()
        cache = getattr(self, "_sidecar_map_cache", None)
        last_built = getattr(self, "_sidecar_map_last_built", 0.0)

        if cache is None or (now - last_built) > 60.0:
            cache = {}
            try:
                for slug_dir in self._sidecar_data_dir.iterdir():
                    if not slug_dir.is_dir():
                        continue
                    events_dir = slug_dir / "events"
                    if not events_dir.exists():
                        continue
                    for ev_file in events_dir.glob("*.json"):
                        try:
                            with open(ev_file, "r", encoding="utf-8", errors="ignore") as f:
                                content = f.read(4096)
                            idx = content.find('"conversationId":"')
                            if idx != -1:
                                end_idx = content.find('"', idx + 18)
                                if end_idx != -1:
                                    cid = content[idx + 18 : end_idx]
                                    cache[cid] = slug_dir.name
                            elif '"conversation_id":"' in content:
                                idx2 = content.find('"conversation_id":"')
                                end_idx2 = content.find('"', idx2 + 19)
                                if end_idx2 != -1:
                                    cid = content[idx2 + 19 : end_idx2]
                                    cache[cid] = slug_dir.name
                        except Exception:
                            continue
            except Exception as e:
                logger.debug("Error building sidecar map cache: %s", e)
            self._sidecar_map_cache = cache
            self._sidecar_map_last_built = now

        return cache.get(conversation_id)

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

        # 1. Proactively verify and clear quota cooldowns if active account has healthy quota (>10%)
        active_quota_healthy, active_account_email, _ = self.check_and_clear_quota_cooldowns()

        # 2. Monitor language_server standalone process PID lifecycle
        pid_changed, current_ls_pid, old_ls_pid = self.check_language_server_lifecycle()

        # Query authoritative conversation summaries DB for unfinished conversations
        summaries_map = get_unfinished_conversations_from_summaries(self._summaries_db_path)

        # Gather monitored in-memory schedules to check across full brain directory
        active_sched_map: dict[str, dict[str, Any]] = {}
        if self.db and hasattr(self.db, "list_active_conversation_schedules"):
            try:
                for s in self.db.list_active_conversation_schedules():
                    active_sched_map[s["conversation_id"]] = s
            except Exception as e:
                logger.debug("Error loading active conversation schedules: %s", e)

        candidate_convo_ids = set()
        if self._brain_dir.exists():
            try:
                for convo_dir in self._brain_dir.iterdir():
                    if convo_dir.is_dir() and convo_dir.name != "tempmediaStorage" and len(convo_dir.name) >= 20:
                        candidate_convo_ids.add(convo_dir.name)
            except Exception as e:
                logger.debug("Error listing brain dir: %s", e)
        for cid in summaries_map:
            candidate_convo_ids.add(cid)

        try:
            for convo_id in candidate_convo_ids:
                transcript_path = resolve_transcript_path(convo_id, self._brain_dir)
                if not transcript_path or not transcript_path.exists():
                    continue

                try:
                    trans_stat = transcript_path.stat()
                    # Do not skip sessions with monitored active schedules, unfinished summaries in DB, or unclosed /boost /goal sessions within 24h
                    if convo_id not in active_sched_map and convo_id not in summaries_map:
                        age = now - trans_stat.st_mtime
                        if age > max(lookback_seconds, 86400):
                            continue
                        elif age > lookback_seconds:
                            if not check_session_has_boost_or_goal(transcript_path, []):
                                continue
                    else:
                        age = now - trans_stat.st_mtime
                        if age > max(lookback_seconds, 86400):
                            continue
                    file_mtime = trans_stat.st_mtime
                except Exception:
                    continue

                lines = self._tail_transcript_lines(transcript_path, max_lines=30)
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

                # --- GOAL COMPLETE ARCHIVE GUARD ---
                # If a session has explicitly recorded completion (<!-- GOAL_COMPLETE --> or sentinel all green), it has cleanly finished.
                # Auto-archive any lingering conversation schedule in the DB and skip resuscitation immediately.
                if check_session_claimed_completion(transcript_path, parsed_steps):
                    if self.db and hasattr(self.db, "complete_conversation_schedule"):
                        self.db.complete_conversation_schedule(convo_id)
                    continue

                # --- CRITICAL BOOST / MULTI-AGENT SUBAGENT ACTIVE GUARD ---
                # In Boost / Multi-Agent delegation mode, the parent agent MUST yield and wait for subagents
                # to run, communicate natively, and report. If ANY child subagent is actively working
                # (active tool execution, recent transcript writes, or unfinished goal), the parent's waiting
                # is normal and expected. NEVER interrupt or wake up the parent prematurely!
                child_ids = extract_subagent_ids_from_transcript(transcript_path, convo_id)
                if child_ids:
                    boost_quiet = float(getattr(self.config, "boost_quiet_seconds", 900))
                    active_children = []
                    for cid in child_ids:
                        is_act, reason = is_subagent_active(
                            cid,
                            self._brain_dir,
                            quiet_seconds=boost_quiet,
                            now=now,
                            ls_restart_time=self.ls_restart_time,
                            parent_id=convo_id,
                        )
                        if is_act:
                            active_children.append((cid, reason))
                    if active_children:
                        logger.debug(
                            "Session %s is legitimately waiting on %d active subagent(s): %s. Skipping watchdog intervention.",
                            convo_id, len(active_children), active_children
                        )
                        continue

                prior_attempts = self.db.get_resuscitation_attempts(convo_id, current_step_index=last_step_idx) if self.db else 0
                if self.ls_restart_time > 0 and self.db and hasattr(self.db, "get_last_resuscitation"):
                    last_res_chk = self.db.get_last_resuscitation(convo_id)
                    if last_res_chk:
                        chk_epoch = float(last_res_chk.get("resuscitated_epoch") or 0.0)
                        if chk_epoch == 0.0 and last_res_chk.get("resuscitated_at"):
                            try:
                                ts_s = str(last_res_chk["resuscitated_at"]).replace(" ", "T")
                                dt_s = datetime.fromisoformat(ts_s if "+" in ts_s else ts_s + "+00:00")
                                chk_epoch = dt_s.timestamp()
                            except Exception:
                                pass
                        if chk_epoch > 0.0 and chk_epoch < self.ls_restart_time:
                            prior_attempts = 0

                sidecar_slug: Optional[str] = None

                parent_convo_id = None
                is_subagent = False
                _parent_resolved = False
                completed_child_id: Optional[str] = None
                completed_child_summary: str = ""

                def _resolve_subagent_and_parent():
                    nonlocal parent_convo_id, is_subagent, _parent_resolved, sidecar_slug
                    if not _parent_resolved:
                        _parent_resolved = True
                        if sidecar_slug is None:
                            sidecar_slug = self._find_associated_sidecar(convo_id)
                        if convo_id in summaries_map and summaries_map[convo_id].get("parent_conversation_id"):
                            parent_convo_id = summaries_map[convo_id]["parent_conversation_id"]
                        else:
                            parent_convo_id = detect_parent_conversation_id(
                                convo_id,
                                conversations_dir=self._conversations_dir,
                                transcript_path=transcript_path,
                            )
                        is_subagent = bool(parent_convo_id)
                        if not is_subagent and parsed_steps:
                            first_content = str(parsed_steps[0].get("content") or "")
                            if (
                                "<subagent_reminder>" in first_content
                                or "You are running as a subagent" in first_content
                                or "invoked by a caller agent" in first_content
                                or "<original_task>" in first_content
                                or "invoke_subagent" in first_content
                                or "DeepInvestigator" in first_content
                                or "DeepCoder" in first_content
                            ):
                                is_subagent = True
                    return parent_convo_id, is_subagent

                def _evaluate_resuscitation_eligibility(
                    cid: str,
                    attempts: int,
                    subagent: bool,
                    parent_id: Optional[str],
                    error_hint: Optional[str] = None,
                ) -> tuple[bool, Optional[str]]:
                    # 1. Orphaned subagent check: prevent split-brain solo mode degradation
                    if subagent and not parent_id:
                        return False, "orphaned_subagent_cannot_determine_parent"

                    # 1b. Completed parent protection: if subagent was stopped/terminated,
                    # but its parent already claimed completion, DO NOT resuscitate or notify parent!
                    if subagent and parent_id:
                        p_path = resolve_transcript_path(parent_id, self._brain_dir)
                        if p_path and p_path.exists():
                            if check_session_claimed_completion(p_path, None):
                                return False, "parent_already_completed"

                    # 1c. Active parent protection: if subagent was stopped by server restart,
                    # but the parent is already actively working on the current server process,
                    # or the parent is waiting on another active subagent on the current server,
                    # DO NOT interrupt or confuse the parent!
                    if subagent and parent_id and is_prior_to_restart:
                        p_path = resolve_transcript_path(parent_id, self._brain_dir)
                        if p_path and p_path.exists():
                            try:
                                p_mtime = p_path.stat().st_mtime
                                if self.ls_restart_time > 0 and p_mtime >= self.ls_restart_time:
                                    return False, "parent_already_active_on_current_server"
                            except Exception:
                                pass
                            p_children = extract_subagent_ids_from_transcript(p_path, parent_id)
                            boost_quiet = float(getattr(self.config, "boost_quiet_seconds", 900))
                            for pch_id in p_children:
                                if pch_id != cid:
                                    is_act, _ = is_subagent_active(
                                        pch_id,
                                        self._brain_dir,
                                        quiet_seconds=boost_quiet,
                                        now=now,
                                        ls_restart_time=self.ls_restart_time,
                                        parent_id=parent_id,
                                    )
                                    if is_act:
                                        return False, "parent_has_other_active_subagent"

                    # 1d. Dead Subagent Zombie Immunity Gate:
                    # If subagent was killed, finished, or superseded by subsequent parent turns,
                    # DO NOT resuscitate it and DO NOT delegate to parent!
                    if subagent:
                        is_zombie, z_reason = is_zombie_or_archived_subagent(
                            subagent_id=cid,
                            parent_id=parent_id,
                            brain_dir=self._brain_dir,
                            now=now,
                            ls_restart_time=self.ls_restart_time,
                            summaries_db_path=self._summaries_db_path,
                        )
                        if is_zombie:
                            return False, f"dead_subagent_zombie_immunity ({z_reason})"

                    # 2. Check last resuscitation record
                    last_res = self.db.get_last_resuscitation(cid) if (self.db and hasattr(self.db, "get_last_resuscitation")) else None
                    res_epoch = 0.0
                    if last_res:
                        status = last_res.get("status")
                        if status == "attempting":
                            return False, "resuscitation_in_progress"
                        res_epoch = float(last_res.get("resuscitated_epoch") or 0.0)
                        if res_epoch == 0.0 and last_res.get("resuscitated_at"):
                            try:
                                ts_str = str(last_res["resuscitated_at"]).replace(" ", "T")
                                dt = datetime.fromisoformat(ts_str if "+" in ts_str else ts_str + "+00:00")
                                res_epoch = dt.timestamp()
                            except Exception:
                                pass

                    # 2b. Strict Once-Per-Restart Gate:
                    # A session or its target parent must be resuscitated at most ONCE per server restart event.
                    if self.ls_restart_time > 0:
                        target_cid = parent_id if (subagent and parent_id) else cid
                        has_prior_restart_res = False
                        if self.db and hasattr(self.db, "has_resuscitation_since"):
                            if self.db.has_resuscitation_since(cid, self.ls_restart_time) or (
                                target_cid != cid and self.db.has_resuscitation_since(target_cid, self.ls_restart_time)
                            ):
                                has_prior_restart_res = True
                        elif res_epoch >= (self.ls_restart_time - 5.0):
                            has_prior_restart_res = True

                        if has_prior_restart_res:
                            if is_prior_to_restart:
                                return False, "already_resuscitated_for_current_server_restart"
                            err_str = str(error_hint or "").lower()
                            if "server_restart" in err_str or "unfinished_conversation_in_summaries" in err_str:
                                return False, "already_resuscitated_for_current_server_restart"

                    # 2c. Completed session gate:
                    if check_session_claimed_completion(transcript_path, parsed_steps):
                        return False, "session_already_completed"

                    effective_attempts = attempts
                    if is_prior_to_restart:
                        if self.ls_restart_time > 0 and res_epoch > 0.0 and res_epoch < self.ls_restart_time:
                            effective_attempts = 0
                            res_epoch = 0.0
                        elif res_epoch == 0.0:
                            effective_attempts = 0
                    elif self.ls_restart_time > 0 and res_epoch > 0.0 and res_epoch < self.ls_restart_time:
                        effective_attempts = 0
                        res_epoch = 0.0

                    # 3. Circuit breaker & Exponential Backoff
                    backoff_window = float(getattr(self.config, "backoff_cooldown_seconds", 1800))
                    max_total = int(getattr(self.config, "max_total_attempts", 5))
                    if effective_attempts >= max_total:
                        return False, f"max_retries_permanently_exhausted ({effective_attempts}/{max_total})"
                    if effective_attempts >= self.config.max_retries_per_session:
                        if res_epoch > 0.0:
                            elapsed = now - res_epoch
                            if elapsed < backoff_window:
                                remaining = int(backoff_window - elapsed)
                                return False, f"max_retries_exhausted ({effective_attempts}/{self.config.max_retries_per_session}) (in backoff cooldown for {remaining}s)"
                            else:
                                logger.info(
                                    "Session %s backoff cooldown (%ds) expired after %d consecutive attempts. Granting probe pull-up.",
                                    cid, int(backoff_window), effective_attempts,
                                )
                        else:
                            return False, f"max_retries_exhausted ({effective_attempts}/{self.config.max_retries_per_session})"

                    # 4. Debounce step cooldown to prevent rapid double-burning of retries at the same step
                    if res_epoch > 0.0 and last_res and last_res.get("status") in ("resuscitated", "failed", "exhausted", "schedule_remounted"):
                        multiplier = 1.0 if effective_attempts <= 1 else (1.5 if effective_attempts == 2 else 2.5)
                        step_cooldown = self.config.stall_grace_seconds * multiplier
                        if now - res_epoch < step_cooldown:
                            return False, f"recent_resuscitation_cooldown ({int(now - res_epoch)}s ago < {int(step_cooldown)}s)"

                    return True, None

                is_prior_to_restart = bool(self.ls_restart_time > 0 and file_mtime < self.ls_restart_time)

                # --- 1. Check Active Quota Cooldown in DB ---
                if active_quota_healthy:
                    active_cd = None
                else:
                    active_cd = self.db.get_active_quota_cooldown(convo_id) if self.db else None

                if active_cd:
                    cd_step = active_cd.get("last_step_index")
                    if cd_step is not None and last_step_idx > cd_step:
                        # Session advanced past the recorded cooldown step!
                        active_cd = None
                    else:
                        db_q = inspect_conversation_db_for_quota(convo_id, conversations_dir=self._conversations_dir)
                        has_recent_quota = (
                            any(
                                s.get("type") != "CHECKPOINT"
                                and (s.get("type") in ("ERROR_MESSAGE", "ERROR") or s.get("status") == "ERROR" or (s.get("source") == "SYSTEM" and s.get("type") == "SYSTEM_MESSAGE" and "failed with error" in str(s.get("content") or "").lower()))
                                and ("individual quota reached" in str(s.get("content") or "").lower() or "resource_exhausted" in str(s.get("content") or "").lower())
                                for s in parsed_steps[-4:]
                            )
                            or (db_q and abs(db_q.get("step_index", 0) - last_step_idx) <= 2)
                        )
                        if not has_recent_quota:
                            active_cd = None

                if active_cd:
                    cooldown_until = float(active_cd.get("cooldown_until") or 0.0)
                    if not is_prior_to_restart and now < cooldown_until and not (now - file_mtime > 300 and prior_attempts == 0):
                        _resolve_subagent_and_parent()
                        remaining = int(cooldown_until - now)
                        stalled.append(
                            StalledSessionInfo(
                                conversation_id=convo_id,
                                transcript_path=transcript_path,
                                last_step_index=last_step_idx,
                                last_error=active_cd.get("last_error") or "Individual quota reached",
                                last_error_time=file_mtime,
                                is_subagent=is_subagent,
                                parent_conversation_id=parent_convo_id,
                                sidecar_slug=sidecar_slug,
                                attempt_count=prior_attempts,
                                can_resuscitate=False,
                                skip_reason=f"quota_cooldown (resets in {remaining}s)",
                                is_quota_exhausted=True,
                                quota_resets_in_seconds=remaining,
                                is_server_restart=False,
                            )
                        )
                        continue
                    else:
                        # Cooldown expired, probe allowed, or server restarted! Eligible for immediate automated pull-up
                        _resolve_subagent_and_parent()
                        can_res, skip_reason = _evaluate_resuscitation_eligibility(convo_id, prior_attempts, is_subagent, parent_convo_id)
                        stalled.append(
                            StalledSessionInfo(
                                conversation_id=convo_id,
                                transcript_path=transcript_path,
                                last_step_index=last_step_idx,
                                last_error="server_restart_pull_up" if is_prior_to_restart else "quota_restored_pull_up",
                                last_error_time=file_mtime,
                                is_subagent=is_subagent,
                                parent_conversation_id=parent_convo_id,
                                sidecar_slug=sidecar_slug,
                                attempt_count=prior_attempts,
                                can_resuscitate=can_res,
                                skip_reason=skip_reason,
                                is_quota_exhausted=False,
                                is_server_restart=is_prior_to_restart,
                            )
                        )
                        continue

                # --- 2. Check for Newly Encountered Quota Exhaustion ---
                quota_detected = False
                quota_sec = None
                quota_ts = None
                for s in reversed(parsed_steps[-6:]):
                    # Only check genuine error/system steps; strictly skip compaction checkpoints, user inputs, and model responses
                    s_type = s.get("type", "")
                    s_status = s.get("status", "")
                    s_source = s.get("source", "")
                    if s_type == "CHECKPOINT":
                        continue
                    if s_type not in ("ERROR_MESSAGE", "ERROR") and s_status != "ERROR":
                        if not (s_source == "SYSTEM" and s_type == "SYSTEM_MESSAGE" and "failed with error" in str(s.get("content") or "").lower()):
                            continue
                    s_content = str(s.get("content") or "")
                    if "individual quota reached" in s_content.lower() or "resource_exhausted" in s_content.lower():
                        quota_detected = True
                        quota_sec = parse_quota_reset_seconds(s_content)
                        break

                if not quota_detected:
                    db_quota = inspect_conversation_db_for_quota(convo_id, conversations_dir=self._conversations_dir)
                    if db_quota and abs(db_quota.get("step_index", 0) - last_step_idx) <= 2:
                        quota_detected = True
                        quota_sec = db_quota.get("resets_in_seconds")
                        quota_ts = db_quota.get("reset_timestamp")

                if quota_detected:
                    _resolve_subagent_and_parent()
                    if active_quota_healthy:
                        # Active account has healthy quota (>10%), pull up if eligible
                        can_res, skip_reason = _evaluate_resuscitation_eligibility(convo_id, prior_attempts, is_subagent, parent_convo_id)
                        stalled.append(
                            StalledSessionInfo(
                                conversation_id=convo_id,
                                transcript_path=transcript_path,
                                last_step_index=last_step_idx,
                                last_error="server_restart_pull_up" if is_prior_to_restart else "quota_restored_pull_up",
                                last_error_time=file_mtime,
                                is_subagent=is_subagent,
                                parent_conversation_id=parent_convo_id,
                                sidecar_slug=sidecar_slug,
                                attempt_count=prior_attempts,
                                can_resuscitate=can_res,
                                skip_reason=skip_reason,
                                is_quota_exhausted=False,
                                is_server_restart=is_prior_to_restart,
                            )
                        )
                        continue

                    wait_sec = quota_sec or 6853.0
                    if quota_ts:
                        try:
                            dt = datetime.fromisoformat(quota_ts.replace("Z", "+00:00"))
                            cooldown_until = dt.timestamp()
                        except Exception:
                            cooldown_until = file_mtime + wait_sec + 30.0
                    else:
                        cooldown_until = file_mtime + wait_sec + 30.0

                    # If server restarted, or cooldown has expired OR stalled for > 300s without prior live attempts, allow pull-up probe!
                    if is_prior_to_restart or now >= cooldown_until or (now - file_mtime > 300 and prior_attempts == 0):
                        can_res, skip_reason = _evaluate_resuscitation_eligibility(convo_id, prior_attempts, is_subagent, parent_convo_id)
                        stalled.append(
                            StalledSessionInfo(
                                conversation_id=convo_id,
                                transcript_path=transcript_path,
                                last_step_index=last_step_idx,
                                last_error="server_restart_pull_up" if is_prior_to_restart else "quota_restored_pull_up",
                                last_error_time=file_mtime,
                                is_subagent=is_subagent,
                                parent_conversation_id=parent_convo_id,
                                sidecar_slug=sidecar_slug,
                                attempt_count=prior_attempts,
                                can_resuscitate=can_res,
                                skip_reason=skip_reason,
                                is_quota_exhausted=False,
                                is_server_restart=is_prior_to_restart,
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
                            parent_conversation_id=parent_convo_id,
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
                    if not sched_info and convo_id in active_sched_map:
                        db_s = active_sched_map[convo_id]
                        sched_info = {
                            "cron": db_s["cron_expression"],
                            "prompt": db_s["prompt"],
                            "interval_seconds": db_s["expected_interval_seconds"],
                            "last_trigger_time": db_s["last_trigger_at"],
                            "last_schedule_time": 0.0,
                        }

                    if sched_info:
                        existing_s = active_sched_map.get(convo_id, {})
                        db_last_trig = existing_s.get("last_trigger_at", 0.0)
                        last_sched = sched_info.get("last_schedule_time", 0.0)
                        last_trig = sched_info.get("last_trigger_time", 0.0)
                        baseline_time = max(last_trig, last_sched, db_last_trig)
                        if baseline_time == 0.0:
                            baseline_time = file_mtime

                        sched_ls_pid = existing_s.get("ls_pid", 0)
                        interval = sched_info["interval_seconds"]

                        # Genuine evidence check:
                        # Has the agent executed the schedule tool (or a trigger fired) on the CURRENT language_server process?
                        has_genuine_mount_evidence = False
                        sched_call_time = sched_info.get("last_schedule_time", 0.0)
                        sched_trig_time = sched_info.get("last_trigger_time", 0.0)
                        if current_ls_pid:
                            if self.ls_restart_time > 0:
                                if sched_call_time >= self.ls_restart_time or sched_trig_time >= self.ls_restart_time:
                                    has_genuine_mount_evidence = True
                            elif sched_call_time > 0 and (now - sched_call_time <= interval * 1.5):
                                has_genuine_mount_evidence = True

                        if has_genuine_mount_evidence:
                            sched_ls_pid = current_ls_pid
                            if self.db and hasattr(self.db, "get_last_resuscitation") and hasattr(self.db, "update_resuscitation_status"):
                                last_res = self.db.get_last_resuscitation(convo_id)
                                if last_res and last_res.get("status") in ("attempting", "resuscitated"):
                                    self.db.update_resuscitation_status(last_res["resuscitation_id"], "schedule_remounted")

                        if self.db:
                            self.db.save_conversation_schedule(
                                conversation_id=convo_id,
                                cron_expression=sched_info["cron"],
                                prompt=sched_info["prompt"],
                                expected_interval_seconds=sched_info["interval_seconds"],
                                last_trigger_at=baseline_time,
                                ls_pid=sched_ls_pid,
                            )

                        lost_due_to_pid = False if has_genuine_mount_evidence else bool(
                            current_ls_pid and (
                                pid_changed
                                or (sched_ls_pid and sched_ls_pid != current_ls_pid)
                            )
                        )
                        lost_due_to_timeout = (
                            (not current_ls_pid or sched_ls_pid == current_ls_pid)
                            and baseline_time > 0
                            and (now - baseline_time > interval * 1.25)
                            and (now - file_mtime > 300)
                        )

                        if lost_due_to_pid or lost_due_to_timeout:
                            _resolve_subagent_and_parent()
                            error_reason = (
                                f"lost_schedule_after_restart (language_server restarted: PID {old_ls_pid or sched_ls_pid} -> {current_ls_pid}, cron: {sched_info['cron']})"
                                if lost_due_to_pid
                                else f"lost_schedule_after_restart (cron: {sched_info['cron']})"
                            )
                            can_res, skip_reason = _evaluate_resuscitation_eligibility(convo_id, prior_attempts, is_subagent, parent_convo_id)
                            stalled.append(
                                StalledSessionInfo(
                                    conversation_id=convo_id,
                                    transcript_path=transcript_path,
                                    last_step_index=last_step_idx,
                                    last_error=error_reason,
                                    last_error_time=file_mtime,
                                    is_subagent=is_subagent,
                                    parent_conversation_id=parent_convo_id,
                                    sidecar_slug=sidecar_slug,
                                    attempt_count=prior_attempts,
                                    can_resuscitate=can_res,
                                    skip_reason=skip_reason,
                                    has_active_schedule=True,
                                    active_cron_expression=sched_info["cron"],
                                )
                            )
                    if not sched_info:
                        # Check if session has already closed with goal complete
                        if check_session_claimed_completion(transcript_path, parsed_steps):
                            continue

                        # Check 1: Is this session waiting on a child subagent that already completed?
                        child_ids: list[str] = []
                        for s in parsed_steps:
                            s_content = str(s.get("content") or "")
                            for m in re.finditer(r'["\']conversationId["\']\s*:\s*["\']([0-9a-zA-Z-]{20,45})["\']', s_content):
                                cid = m.group(1)
                                if cid != convo_id and cid not in child_ids:
                                    child_ids.append(cid)
                            for m in re.finditer(r'subagent\s*\(?[`\'"]?([0-9a-zA-Z-]{20,45})[`\'"]?\)?', s_content, re.IGNORECASE):
                                cid = m.group(1)
                                if cid != convo_id and cid not in child_ids:
                                    child_ids.append(cid)

                        already_notified_cids = set()
                        for s in parsed_steps:
                            s_content = str(s.get("content") or "")
                            for cid in child_ids:
                                if cid in s_content and (
                                    "子代理完成通知" in s_content
                                    or "已完成执行" in s_content
                                    or f"sender={cid}" in s_content
                                    or f"sender: {cid}" in s_content
                                    or f"sender={cid}" in s_content.lower()
                                ):
                                    already_notified_cids.add(cid)

                        if self.db and hasattr(self.db, "has_resuscitation_since"):
                            for cid in child_ids:
                                if cid not in already_notified_cids:
                                    if self.db.has_resuscitation_since(convo_id, 0.0, error_prefix=f"parent_waiting_subagent_completed_hang:{cid}"):
                                        already_notified_cids.add(cid)

                        for cid in child_ids:
                            if cid in already_notified_cids:
                                continue
                            c_path = resolve_transcript_path(cid, self._brain_dir)
                            if c_path and c_path.exists():
                                c_lines = self._tail_transcript_lines(c_path, max_lines=10)
                                for cl in reversed(c_lines):
                                    try:
                                        c_step = json.loads(cl.strip())
                                        c_cnt = str(c_step.get("content") or "")
                                        # Subagent completion MUST be genuine and explicit (goal_complete marker)
                                        # NEVER treat an intermediate PLANNER_RESPONSE turn as completed!
                                        if "<!-- goal_complete -->" in c_cnt.lower() or "<!-- goal_finished -->" in c_cnt.lower() or "[goal_complete]" in c_cnt.lower():
                                            completed_child_id = cid
                                            completed_child_summary = c_cnt[:500]
                                            break
                                    except Exception:
                                        continue
                                if completed_child_id:
                                    break

                        has_stop_hook = any("stop hook blocked termination" in str(s.get("content") or "").lower() for s in parsed_steps)

                        is_boost_goal = check_session_has_boost_or_goal(transcript_path, parsed_steps)
                        if completed_child_id and (now - file_mtime > self.config.stall_grace_seconds):
                            pass
                        elif has_stop_hook:
                            pass
                        elif is_prior_to_restart and is_boost_goal and not check_session_claimed_completion(transcript_path, parsed_steps):
                            pass
                        else:
                            continue

                terminal_db_err = inspect_conversation_db_for_terminal_network_error(
                    convo_id, conversations_dir=self._conversations_dir
                )
                is_terminal_db_error = False
                if terminal_db_err:
                    err_idx = terminal_db_err.get("step_index", 0)
                    if err_idx >= last_step_idx - 2:
                        is_terminal_db_error = True

                # If the last step is an active user input or running tool, check if actively running.
                if not is_terminal_db_error:
                    is_actively_running = False
                    running_quiet = float(getattr(self.config, "boost_quiet_seconds", 900))
                    if last_status == "RUNNING":
                        # Only actively running if on current server process AND within running_quiet window
                        if not is_prior_to_restart and (now - file_mtime < running_quiet):
                            is_actively_running = True
                    elif last_source in ("USER_EXPLICIT", "USER") and (now - file_mtime < self.config.stall_grace_seconds):
                        is_actively_running = True

                    if is_actively_running:
                        continue

                is_stalled = False
                matched_error = ""
                is_mcp = False
                is_network_issue = False
                is_server_restart = is_prior_to_restart

                if is_terminal_db_error:
                    is_network_issue = True
                    matched_error = "network_issue_server_error"
                    is_stalled = True

                is_boost_goal = check_session_has_boost_or_goal(transcript_path, parsed_steps)
                has_stop_hook = any("stop hook blocked termination" in str(s.get("content") or "").lower() for s in parsed_steps)
                if check_session_claimed_completion(transcript_path, parsed_steps):
                    continue
                is_stop_hook_halt = has_stop_hook and not last_step.get("tool_calls")

                # Check if aborted by server restart or hung in RUNNING state
                if last_status == "RUNNING":
                    is_stalled = True
                    if is_prior_to_restart:
                        matched_error = "server_restart_aborted_running_step"
                    else:
                        matched_error = "running_step_hung"

                # Check authoritative conversation_summaries state
                sum_info = summaries_map.get(convo_id)
                if (
                    not is_stalled
                    and sum_info
                    and (sum_info.get("status") == "CASCADE_RUN_STATUS_RUNNING" or sum_info.get("not_fully_idle"))
                    and not check_session_claimed_completion(transcript_path, parsed_steps)
                ):
                    is_cleanly_done = (
                        last_source == "MODEL"
                        and last_type == "PLANNER_RESPONSE"
                        and last_status == "DONE"
                        and not last_step.get("tool_calls")
                        and not has_stop_hook
                    )
                    if not is_cleanly_done:
                        if is_prior_to_restart or (now - file_mtime >= self.config.stall_grace_seconds):
                            is_stalled = True
                            if is_prior_to_restart:
                                matched_error = "server_restart_unfinished_conversation"
                            else:
                                matched_error = "unfinished_conversation_in_summaries"

                # Check if pre-restart boost / goal session passed through (and not already completed)
                if not is_stalled and is_prior_to_restart and is_boost_goal and not check_session_claimed_completion(transcript_path, parsed_steps):
                    is_stalled = True
                    matched_error = "server_restart_unfinished_conversation"

                # Check if session ended on an unanswered user prompt
                if not is_stalled and last_source in ("USER_EXPLICIT", "USER"):
                    if is_prior_to_restart:
                        is_stalled = True
                        matched_error = "server_restart_unanswered_user_prompt"
                    elif (now - file_mtime >= self.config.stall_grace_seconds):
                        is_stalled = True
                        matched_error = "unanswered_user_prompt_hang"

                # Widen MCP error detection across all loaded parsed steps (up to 15 steps)
                for s in reversed(parsed_steps):
                    t_calls = s.get("tool_calls") or []
                    for tc in t_calls:
                        tc_name = str(tc.get("name") or "").lower()
                        if tc_name.startswith("mcp_") or "mcp" in tc_name:
                            is_mcp = True
                            break
                    if is_mcp:
                        break
                    s_cnt = str(s.get("content") or "").lower()
                    if "mcp error" in s_cnt or "mcp_" in s_cnt or "failed to call tool" in s_cnt:
                        is_mcp = True
                        break

                # 1. Check if the conversation ended in an empty planner response hang, halted after Stop Hook, or waiting on completed subagent
                if len(parsed_steps) >= 2 and last_source == "MODEL" and last_type == "PLANNER_RESPONSE" and (not last_content or is_stop_hook_halt or completed_child_id) and not last_step.get("tool_calls"):
                    if is_server_restart or (now - file_mtime > self.config.stall_grace_seconds):
                        is_stalled = True
                        if completed_child_id:
                            matched_error = f"parent_waiting_subagent_completed_hang:{completed_child_id}"
                        elif has_stop_hook:
                            matched_error = "stop_hook_mcp_hang" if is_mcp else "stop_hook_hang"
                        elif is_boost_goal:
                            matched_error = "boost_goal_mcp_hang" if is_mcp else "boost_goal_hang"
                        elif is_mcp:
                            matched_error = "mcp_error_hang"
                        else:
                            matched_error = "empty_planner_response_hang"

                # 2. Check if the conversation halted on a completed tool execution output without subsequent model response
                elif len(parsed_steps) >= 2 and last_source == "MODEL" and last_type == "GENERIC" and last_status == "DONE":
                    if is_server_restart or (now - file_mtime > self.config.stall_grace_seconds):
                        is_stalled = True
                        if is_server_restart:
                            matched_error = "server_restart_aborted_tool_followup"
                        elif has_stop_hook:
                            matched_error = "stop_hook_mcp_hang" if is_mcp else "stop_hook_hang"
                        elif is_boost_goal:
                            matched_error = "tool_result_boost_goal_hang"
                        elif is_mcp:
                            matched_error = "mcp_error_hang"
                        else:
                            matched_error = "tool_result_hang"

                # 3. Check if the conversation halted on a Stop Hook block without subsequent agent progress
                elif last_source == "SYSTEM" and "stop hook blocked termination" in str(last_content).lower():
                    if is_server_restart or (now - file_mtime > self.config.stall_grace_seconds):
                        is_stalled = True
                        matched_error = "stop_hook_mcp_hang" if is_mcp else "stop_hook_hang"

                # 3. If not empty hang or stop hook last step, inspect steps backwards for unresolved system/error interruptions
                if not is_stalled:
                    for step in reversed(parsed_steps):
                        s_source = step.get("source", "")
                        s_type = step.get("type", "")
                        s_status = step.get("status", "")
                        s_content = str(step.get("content") or "").lower()

                        if s_source == "MODEL" and s_type == "PLANNER_RESPONSE" and s_status == "DONE" and (s_content or step.get("tool_calls")):
                            break

                        if s_source == "SYSTEM" or s_status == "ERROR" or s_type == "ERROR_MESSAGE":
                            if "network issue" in s_content or "agent executor error" in s_content:
                                is_network_issue = True
                            for pattern in INTERRUPTED_STREAM_PATTERNS:
                                if pattern in s_content:
                                    is_stalled = True
                                    if "network" in pattern or "server" in pattern or "host" in pattern:
                                        is_network_issue = True
                                    if has_stop_hook:
                                        matched_error = "stop_hook_mcp_hang" if is_mcp else "stop_hook_hang"
                                    elif is_boost_goal:
                                        matched_error = "boost_goal_mcp_hang" if is_mcp else "boost_goal_hang"
                                    else:
                                        matched_error = pattern
                                    break
                            if is_stalled:
                                break
                            if s_status == "ERROR" or s_type == "ERROR_MESSAGE":
                                is_stalled = True
                                if "network" in s_content or "server" in s_content or "host" in s_content:
                                    is_network_issue = True
                                if has_stop_hook:
                                    matched_error = "stop_hook_mcp_hang" if is_mcp else "stop_hook_hang"
                                elif is_boost_goal:
                                    matched_error = "boost_goal_mcp_hang" if is_mcp else "boost_goal_hang"
                                else:
                                    matched_error = s_content[:150] or f"{s_source}_{s_status}"
                                break

                if not is_stalled:
                    continue

                _resolve_subagent_and_parent()

                can_res, skip_reason = _evaluate_resuscitation_eligibility(
                    convo_id, prior_attempts, is_subagent, parent_convo_id, error_hint=matched_error
                )
                if not can_res:
                    stalled.append(
                        StalledSessionInfo(
                            conversation_id=convo_id,
                            transcript_path=transcript_path,
                            last_step_index=last_step_idx,
                            last_error=matched_error,
                            last_error_time=file_mtime,
                            is_subagent=is_subagent,
                            parent_conversation_id=parent_convo_id,
                            sidecar_slug=sidecar_slug,
                            attempt_count=prior_attempts,
                            can_resuscitate=False,
                            skip_reason=skip_reason,
                            is_mcp_error=is_mcp,
                            is_boost_goal=is_boost_goal,
                            is_stop_hook_hang=has_stop_hook,
                            completed_child_id=completed_child_id,
                            completed_child_summary=completed_child_summary,
                            is_network_issue=is_network_issue,
                            is_server_restart=is_server_restart,
                        )
                    )
                    continue

                # Stall grace period: recent errors (< stall_grace_seconds) are given time to self-heal.
                # However, deterministic terminal execution errors recorded in SQLite DB (step_type = 17)
                # and server restart interrupted tasks bypass the stall grace period.
                if not is_terminal_db_error and not is_server_restart and (now - file_mtime < self.config.stall_grace_seconds):
                    stalled.append(
                        StalledSessionInfo(
                            conversation_id=convo_id,
                            transcript_path=transcript_path,
                            last_step_index=last_step_idx,
                            last_error=matched_error,
                            last_error_time=file_mtime,
                            is_subagent=is_subagent,
                            parent_conversation_id=parent_convo_id,
                            sidecar_slug=sidecar_slug,
                            attempt_count=prior_attempts,
                            can_resuscitate=False,
                            skip_reason="within_stall_grace_period",
                            is_mcp_error=is_mcp,
                            is_boost_goal=is_boost_goal,
                            is_stop_hook_hang=has_stop_hook,
                            completed_child_id=completed_child_id,
                            completed_child_summary=completed_child_summary,
                            is_network_issue=is_network_issue,
                            is_server_restart=is_server_restart,
                        )
                    )
                    continue

                stalled.append(
                    StalledSessionInfo(
                        conversation_id=convo_id,
                        transcript_path=transcript_path,
                        last_step_index=last_step_idx,
                        last_error=matched_error,
                        last_error_time=file_mtime,
                        is_subagent=is_subagent,
                        parent_conversation_id=parent_convo_id,
                        sidecar_slug=sidecar_slug,
                        attempt_count=prior_attempts,
                        can_resuscitate=True,
                        skip_reason=None,
                        is_mcp_error=is_mcp,
                        is_boost_goal=is_boost_goal,
                        is_stop_hook_hang=has_stop_hook,
                        completed_child_id=completed_child_id,
                        completed_child_summary=completed_child_summary,
                        is_network_issue=is_network_issue,
                        is_server_restart=is_server_restart,
                    )
                )

        except Exception as e:
            logger.error("Error during stalled conversation scan: %s", e)

        import gc
        gc.collect()
        from hub.memory import apply_memory_pressure_relief
        apply_memory_pressure_relief()
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
        is_delegated_boost = bool(session_info.is_subagent and session_info.parent_conversation_id)
        target_convo_id = session_info.parent_conversation_id if is_delegated_boost else convo_id
        last_error = session_info.last_error

        # 1. In-flight concurrency mutex: prevent duplicate parallel pull-up calls
        if convo_id in self._in_flight_resuscitations or target_convo_id in self._in_flight_resuscitations:
            logger.warning(
                "Session %s (target %s) is already in-flight for resuscitation. Skipping duplicate concurrent call.",
                convo_id, target_convo_id,
            )
            return {
                "success": False,
                "conversation_id": convo_id,
                "target_conversation_id": target_convo_id,
                "is_delegated_boost": is_delegated_boost,
                "status": "in_flight_skip",
                "error": "resuscitation_already_in_flight",
            }

        # 2. Completed session pre-flight check
        if check_session_claimed_completion(session_info.transcript_path, None):
            logger.info("Session %s has already claimed completion. Skipping resuscitation.", convo_id)
            return {
                "success": False,
                "conversation_id": convo_id,
                "target_conversation_id": target_convo_id,
                "is_delegated_boost": is_delegated_boost,
                "status": "session_already_completed",
                "error": "task_claimed_completion",
            }

        # 2b. Dead Subagent Zombie Immunity Gate
        if is_delegated_boost or session_info.is_subagent:
            is_zombie, z_reason = is_zombie_or_archived_subagent(
                subagent_id=convo_id,
                parent_id=session_info.parent_conversation_id,
                brain_dir=self._brain_dir,
                now=time.time(),
                ls_restart_time=self.ls_restart_time,
                summaries_db_path=self._summaries_db_path,
            )
            if is_zombie:
                logger.info(
                    "Subagent %s is zombie/archived (%s). Skipping resuscitation/delegation.",
                    convo_id,
                    z_reason,
                )
                return {
                    "success": False,
                    "conversation_id": convo_id,
                    "target_conversation_id": target_convo_id,
                    "is_delegated_boost": is_delegated_boost,
                    "status": "dead_subagent_zombie_immunity",
                    "error": f"dead_subagent_zombie_immunity ({z_reason})",
                }

        # 3. Once-Per-Restart Pre-flight Defense-In-Depth
        if self.ls_restart_time > 0:
            if self.db and hasattr(self.db, "has_resuscitation_since"):
                if self.db.has_resuscitation_since(convo_id, self.ls_restart_time) or (
                    target_convo_id != convo_id and self.db.has_resuscitation_since(target_convo_id, self.ls_restart_time)
                ):
                    if (
                        session_info.is_server_restart
                        or "server_restart" in str(last_error).lower()
                        or "unfinished_conversation_in_summaries" in str(last_error).lower()
                    ):
                        logger.info(
                            "Session %s (target %s) already resuscitated for current server restart (ls_restart_time=%s). Skipping duplicate resuscitation.",
                            convo_id, target_convo_id, self.ls_restart_time,
                        )
                        return {
                            "success": False,
                            "conversation_id": convo_id,
                            "target_conversation_id": target_convo_id,
                            "is_delegated_boost": is_delegated_boost,
                            "status": "already_resuscitated_for_restart",
                            "error": "already_resuscitated_for_current_server_restart",
                        }

        # 4. Debounce step cooldown (< stall_grace_seconds)
        now_ts = time.time()
        recent_ts = max(
            self._recently_resuscitated.get(convo_id, 0.0),
            self._recently_resuscitated.get(target_convo_id, 0.0),
        )
        if (now_ts - recent_ts) < self.config.stall_grace_seconds:
            logger.info(
                "Session %s (target %s) was resuscitated %ds ago (< %ds). Skipping debounce.",
                convo_id, target_convo_id, int(now_ts - recent_ts), int(self.config.stall_grace_seconds),
            )
            return {
                "success": False,
                "conversation_id": convo_id,
                "target_conversation_id": target_convo_id,
                "is_delegated_boost": is_delegated_boost,
                "status": "debounced",
                "error": "recent_resuscitation_cooldown",
            }

        self._in_flight_resuscitations.add(convo_id)
        self._in_flight_resuscitations.add(target_convo_id)
        try:
            return await self._execute_resuscitation(
                session_info=session_info,
                target_convo_id=target_convo_id,
                is_delegated_boost=is_delegated_boost,
                custom_prompt=custom_prompt,
            )
        finally:
            self._in_flight_resuscitations.discard(convo_id)
            self._in_flight_resuscitations.discard(target_convo_id)
            self._recently_resuscitated[convo_id] = time.time()
            self._recently_resuscitated[target_convo_id] = time.time()

    async def _execute_resuscitation(
        self,
        session_info: StalledSessionInfo,
        target_convo_id: str,
        is_delegated_boost: bool,
        custom_prompt: Optional[str] = None,
    ) -> dict[str, Any]:
        convo_id = session_info.conversation_id
        sidecar_slug = session_info.sidecar_slug
        last_error = session_info.last_error

        # Determine tailored prompt based on scenario
        if session_info.completed_child_id:
            prompt = custom_prompt or PARENT_DELEGATION_COMPLETED_PROMPT.format(
                subagent_id=session_info.completed_child_id,
                subagent_summary=session_info.completed_child_summary or "（已圆满完成原定子任务）",
            )
        elif session_info.is_server_restart:
            if session_info.is_boost_goal or is_delegated_boost:
                prompt = custom_prompt or BOOST_SERVER_RESTART_RESUSCITATION_PROMPT
            else:
                prompt = custom_prompt or SERVER_RESTART_RESUSCITATION_PROMPT
        elif is_delegated_boost:
            # Active account info from QuotaSentinel
            active_account_email = "已恢复活跃账户"
            if self.quota_sentinel and hasattr(self.quota_sentinel, "is_active_account_healthy"):
                try:
                    _, email, _ = self.quota_sentinel.is_active_account_healthy()
                    if email:
                        active_account_email = email
                except Exception:
                    pass
            prompt = custom_prompt or BOOST_DELEGATION_RESUSCITATION_PROMPT.format(
                subagent_id=convo_id,
                active_account=active_account_email,
            )
        elif session_info.is_quota_exhausted or session_info.last_error == "quota_restored_pull_up":
            prompt = custom_prompt or QUOTA_RESUSCITATION_PROMPT
        elif session_info.is_boost_goal or session_info.is_stop_hook_hang or "stop_hook" in str(session_info.last_error).lower() or "boost" in str(session_info.last_error).lower() or "goal" in str(session_info.last_error).lower():
            prompt = custom_prompt or BOOST_GOAL_RESUSCITATION_PROMPT
        elif session_info.is_network_issue or "network" in str(session_info.last_error).lower() or "server_error" in str(session_info.last_error).lower():
            prompt = custom_prompt or NETWORK_ERROR_RESUSCITATION_PROMPT
        elif session_info.is_mcp_error or "mcp" in str(session_info.last_error).lower():
            prompt = custom_prompt or MCP_ERROR_RESUSCITATION_PROMPT
        elif session_info.has_active_schedule or "schedule" in str(session_info.last_error).lower():
            cron_str = session_info.active_cron_expression or "*/30 * * * *"
            prompt = custom_prompt or SCHEDULE_REMOUNT_PROMPT.format(cron=cron_str)
        elif session_info.last_error == "empty_planner_response_hang":
            prompt = custom_prompt or EMPTY_RESPONSE_RESUSCITATION_PROMPT
        elif "tool_result" in str(session_info.last_error).lower():
            prompt = custom_prompt or TOOL_RESULT_RESUSCITATION_PROMPT
        else:
            prompt = custom_prompt or DEFAULT_RESUSCITATION_PROMPT

        record = self.db.record_resuscitation(
            conversation_id=convo_id,
            sidecar_slug=sidecar_slug,
            last_error=last_error,
            status="attempting",
            resuscitation_prompt=prompt,
            last_step_index=session_info.last_step_index,
            error_details=f"delegated_to_parent:{target_convo_id}" if is_delegated_boost else None,
        )
        res_id = record["resuscitation_id"]

        logger.info(
            "Resuscitating Antigravity session %s (target=%s, subagent=%s, parent=%s, attempt=%d/%d, error=%s)",
            convo_id,
            target_convo_id,
            session_info.is_subagent,
            session_info.parent_conversation_id,
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
                    "target_conversation_id": target_convo_id,
                    "is_delegated_boost": is_delegated_boost,
                    "attempt_count": record["attempt_count"],
                    "status": "attempting",
                    "timestamp": time.time(),
                })
            except Exception as b_err:
                logger.debug("Failed publishing resuscitation attempting event: %s", b_err)

        try:
            success, stdout, err = await self.agentapi.send_message(
                conversation_id=target_convo_id,
                content=prompt,
                timeout_seconds=30,
            )

            if success:
                new_st = "schedule_remounted" if session_info.has_active_schedule else "resuscitated"
                self.db.update_resuscitation_status(res_id, new_st)
                if session_info.has_active_schedule and self.db:
                    self.db.update_conversation_schedule_trigger(convo_id, time.time(), ls_pid=self.last_known_ls_pid)
                
                # Remount Slack watcher if this is an active session thread
                if hasattr(self.db, "execute_read"):
                    try:
                        active_threads = await self.db.execute_read(
                            "SELECT * FROM session_threads WHERE conversation_id = ? AND status = 'active'", 
                            (target_convo_id,)
                        )
                        if active_threads:
                            from hub.antigravity.result_delivery import watch_and_deliver_result, cancel_active_watcher
                            from hub.antigravity.thread_notifier import ThreadNotifier
                            import asyncio
                            
                            notifier = ThreadNotifier()
                            for sess in active_threads:
                                channel = sess.get("channel_id")
                                root_ts = sess.get("root_ts")
                                task_id = sess.get("task_id")
                                if channel and root_ts:
                                    logger.info("Remounting Slack watcher for resuscitated session %s (Task: %s)", target_convo_id, task_id)
                                    cancel_active_watcher(target_convo_id)
                                    asyncio.create_task(
                                        watch_and_deliver_result(
                                            notifier=notifier,
                                            channel=channel,
                                            thread_ts=root_ts,
                                            conversation_id=target_convo_id,
                                            task_id=task_id,
                                            start_time=0.0,
                                            is_follow_up=False,
                                            start_step=0,
                                            broker=self.broker,
                                        )
                                    )
                    except Exception as e:
                        logger.warning("Failed to remount Slack watcher during resuscitation for %s: %s", target_convo_id, e)

                logger.info("Successfully resuscitated session %s via target %s (res_id=%s, status=%s)", convo_id, target_convo_id, res_id, new_st)
                if self.broker:
                    try:
                        await self.broker.publish("events", {
                            "event": "antigravity_resuscitation",
                            "type": "antigravity_resuscitation",
                            "action": new_st,
                            "resuscitation_id": res_id,
                            "conversation_id": convo_id,
                            "target_conversation_id": target_convo_id,
                            "is_delegated_boost": is_delegated_boost,
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
                    "target_conversation_id": target_convo_id,
                    "is_delegated_boost": is_delegated_boost,
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
                        "target_conversation_id": target_convo_id,
                        "is_delegated_boost": is_delegated_boost,
                        "status": "quota_cooldown",
                        "error": err,
                        "cooldown_until": cooldown_until,
                    }

                new_status = "exhausted" if record["attempt_count"] >= self.config.max_retries_per_session else "failed"
                self.db.update_resuscitation_status(res_id, new_status, error_details=err[:500] if err else None)
                logger.warning(
                    "Failed to resuscitate session %s via target %s: %s (status marked as %s)",
                    convo_id,
                    target_convo_id,
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
                            "target_conversation_id": target_convo_id,
                            "is_delegated_boost": is_delegated_boost,
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
                    "target_conversation_id": target_convo_id,
                    "is_delegated_boost": is_delegated_boost,
                    "status": new_status,
                    "error": err,
                }

        except Exception as e:
            logger.exception("Unexpected exception resuscitating session %s via target %s: %s", convo_id, target_convo_id, e)
            self.db.update_resuscitation_status(res_id, "failed", error_details=str(e)[:500])
            if self.broker:
                try:
                    await self.broker.publish("events", {
                        "event": "antigravity_resuscitation",
                        "type": "antigravity_resuscitation",
                        "action": "failed",
                        "resuscitation_id": res_id,
                        "conversation_id": convo_id,
                        "target_conversation_id": target_convo_id,
                        "is_delegated_boost": is_delegated_boost,
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
                "target_conversation_id": target_convo_id,
                "is_delegated_boost": is_delegated_boost,
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

        stalled = await asyncio.to_thread(self.scan_stalled_conversations)
        results: list[dict[str, Any]] = []
        awakened_targets: set[str] = set()

        for item in stalled:
            if not item.can_resuscitate:
                logger.debug(
                    "Skipping session %s: %s",
                    item.conversation_id,
                    item.skip_reason,
                )
                continue

            target_id = item.parent_conversation_id if (item.is_subagent and item.parent_conversation_id) else item.conversation_id

            # Deduplicate awakenings per tick:
            # If target session, or item conversation itself, or its parent was already contacted in this tick, skip
            if target_id in awakened_targets or item.conversation_id in awakened_targets:
                logger.debug(
                    "Skipping session %s (target=%s) because conversation was already contacted in this tick",
                    item.conversation_id,
                    target_id,
                )
                continue
            if item.parent_conversation_id and item.parent_conversation_id in awakened_targets:
                logger.debug(
                    "Skipping session %s because parent %s was already contacted in this tick",
                    item.conversation_id,
                    item.parent_conversation_id,
                )
                continue

            # Cross-sweep debounce: skip if resuscitated within stall_grace_seconds
            now_sweep = time.time()
            if (now_sweep - self._recently_resuscitated.get(item.conversation_id, 0.0) < self.config.stall_grace_seconds) or (
                now_sweep - self._recently_resuscitated.get(target_id, 0.0) < self.config.stall_grace_seconds
            ):
                logger.debug(
                    "Skipping session %s due to cross-sweep debounce (< %ds)",
                    item.conversation_id, self.config.stall_grace_seconds,
                )
                continue

            if self.config.auto_resuscitate:
                res = await self.resuscitate_session(item)
                results.append(res)
                if res.get("success"):
                    awakened_targets.add(target_id)
                    awakened_targets.add(item.conversation_id)
                    if item.parent_conversation_id:
                        awakened_targets.add(item.parent_conversation_id)
                await asyncio.sleep(0.25)

        from hub.memory import apply_memory_pressure_relief
        apply_memory_pressure_relief()
        return results

    def get_status(self, force: bool = False) -> dict[str, Any]:
        """Comprehensive status report for CLI doctor, API, and dashboards."""
        now = time.time()
        if not force and getattr(self, "_status_cache", None) and (now - getattr(self, "_status_cache_time", 0.0) < 20.0):
            return self._status_cache

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

        res = {
            "watchdog_enabled": self.config.enabled,
            "auto_resuscitate": self.config.auto_resuscitate,
            "interval_seconds": self.config.interval_seconds,
            "network_online": net_ok,
            "probe_target": f"{probe_host}:{probe_port}",
            "agentapi_available": agentapi_avail,
            "language_server_connected": ls_ok,
            "language_server_address": addr,
            "last_known_ls_pid": self.last_known_ls_pid,
            "ls_restart_time": self.ls_restart_time,
            "stalled_sessions_detected": len(stalled),
            "stalled_sessions": [
                {
                    "conversation_id": s.conversation_id,
                    "last_error": s.last_error,
                    "is_subagent": s.is_subagent,
                    "parent_conversation_id": s.parent_conversation_id,
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
                    "is_server_restart": s.is_server_restart,
                }
                for s in stalled
            ],
            "quota_cooldown_sessions": self.db.list_quota_cooldowns() if self.db else [],
            "active_conversation_schedules": self.db.list_active_conversation_schedules() if self.db else [],
            "resuscitation_stats": stats,
            "recent_resuscitations": recent_resuscitations,
        }
        self._status_cache = res
        self._status_cache_time = now
        import gc
        gc.collect()
        return res
