"""
Antigravity Webhook Hub — Asynchronous Progress Tracking & True Result Delivery Engine
Monitors Antigravity conversation transcripts, streams live execution progress milestones
to Slack threads, and delivers the actual markdown results directly into Slack threads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hub.antigravity.thread_notifier import ThreadNotifier

logger = logging.getLogger("hub.antigravity.result_delivery")

# Brain root directory for conversation transcripts
DEFAULT_BRAIN_ROOT = Path(
    os.environ.get(
        "ANTIGRAVITY_BRAIN_DIR",
        os.path.expanduser("~/.gemini/antigravity/brain"),
    )
)


def resolve_transcript_path(conversation_id: str, brain_root: Optional[Path] = None) -> Optional[Path]:
    """
    Locate the transcript_full.jsonl (or fallback transcript.jsonl)
    for a given Antigravity conversation_id.
    """
    root = brain_root or DEFAULT_BRAIN_ROOT
    convo_dir = root / conversation_id / ".system_generated" / "logs"
    if not convo_dir.exists():
        return None

    full_path = convo_dir / "transcript_full.jsonl"
    if full_path.exists() and full_path.stat().st_size > 0:
        return full_path

    compact_path = convo_dir / "transcript.jsonl"
    if compact_path.exists() and compact_path.stat().st_size > 0:
        return compact_path

    return None


def get_latest_step_index(conversation_id: str, brain_root: Optional[Path] = None) -> int:
    """Return the highest step_index currently recorded in the conversation transcript."""
    transcript_path = resolve_transcript_path(conversation_id, brain_root)
    if not transcript_path or not transcript_path.exists():
        return 0

    latest_idx = 0
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    idx = data.get("step_index", 0)
                    if idx > latest_idx:
                        latest_idx = idx
                except Exception:
                    continue
    except Exception as e:
        logger.debug("Error reading latest step index from %s: %s", transcript_path, e)

    return latest_idx


def parse_transcript_events(
    transcript_path: Path,
    start_step: int = 0,
) -> Tuple[List[str], Optional[str], bool, bool]:
    """
    Parse a conversation transcript starting from start_step.
    
    Returns:
        (actions, final_content, is_done, has_error)
        - actions: List of human-readable descriptions of tool actions executed.
        - final_content: Final markdown response text from MODEL PLANNER_RESPONSE.
        - is_done: True if a completed MODEL response has been reached with no pending tools.
        - has_error: True if a terminal error step occurred.
    """
    actions: List[str] = []
    final_content: Optional[str] = None
    is_done: bool = False
    has_error: bool = False

    if not transcript_path.exists():
        return actions, final_content, is_done, has_error

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    step = json.loads(line)
                except Exception:
                    continue

                idx = step.get("step_index", 0)
                if idx < start_step:
                    continue

                source = step.get("source", "")
                step_type = step.get("type", "")
                status = step.get("status", "")

                if source == "MODEL" and status == "ERROR":
                    has_error = True
                elif source == "SYSTEM" and status == "ERROR":
                    has_error = True
                elif status == "DONE":
                    has_error = False

                # Tool calls extraction
                for tc in step.get("tool_calls", []):
                    args = tc.get("args") or {}
                    act = (
                        args.get("toolAction")
                        or args.get("toolSummary")
                        or tc.get("toolAction")
                        or tc.get("toolSummary")
                        or tc.get("name")
                    )
                    if act:
                        actions.append(f"{act}")

                # Model Planner Response extraction
                if source == "MODEL" and step_type == "PLANNER_RESPONSE":
                    content = step.get("content", "").strip()
                    tool_calls = step.get("tool_calls") or []
                    
                    # If this step has markdown content and no pending tool calls
                    if content and not tool_calls:
                        final_content = content
                        if status == "DONE":
                            is_done = True
                            has_error = False
                    elif tool_calls:
                        # Model called tools, answer is not ready yet
                        is_done = False

    except Exception as e:
        logger.warning("Failed to parse transcript %s: %s", transcript_path, e)

    return actions, final_content, is_done, has_error


async def watch_and_deliver_result(
    notifier: ThreadNotifier,
    channel: str,
    thread_ts: str,
    conversation_id: str,
    task_id: str,
    start_time: float,
    is_follow_up: bool = False,
    start_step: int = 0,
    max_wait_seconds: float = 900.0,
    inactivity_timeout: float = 300.0,
    poll_interval: float = 1.5,
    min_progress_interval: float = 4.0,
    brain_root: Optional[Path] = None,
    broker: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Asynchronously monitors an Antigravity conversation transcript in the background,
    emits progress milestones into the Slack thread during tool execution,
    and delivers the full generated result directly into the Slack thread upon completion.
    """
    last_reported_action: Optional[str] = None
    last_progress_time: float = 0.0
    last_activity_time: float = time.time()
    last_seen_step: int = start_step
    start_loop = time.time()

    logger.info(
        "Started conversation watcher for %s (task: %s, thread: %s:%s, is_follow_up: %s, start_step: %d)",
        conversation_id,
        task_id,
        channel,
        thread_ts,
        is_follow_up,
        start_step,
    )

    while True:
        now = time.time()
        if (now - start_loop) >= max_wait_seconds:
            logger.warning("Conversation watcher reached hard max_wait_seconds (%.1fs) for %s", max_wait_seconds, conversation_id)
            break

        if (now - last_activity_time) >= inactivity_timeout:
            logger.warning("Conversation watcher detected inactivity (no progress for %.1fs) for %s", inactivity_timeout, conversation_id)
            break

        await asyncio.sleep(poll_interval)
        elapsed = time.time() - start_time

        transcript_path = resolve_transcript_path(conversation_id, brain_root)
        if not transcript_path:
            continue

        # Check for step progress to renew liveness lease
        current_highest_step = get_latest_step_index(conversation_id, brain_root)
        if current_highest_step > last_seen_step:
            last_seen_step = current_highest_step
            last_activity_time = time.time()

        actions, final_content, is_done, has_error = parse_transcript_events(
            transcript_path,
            start_step=start_step,
        )

        # 1. Live Progress milestone updates
        if actions:
            latest_action = actions[-1]
            now = time.time()
            if (
                latest_action != last_reported_action
                and (now - last_progress_time) >= min_progress_interval
                and not is_done
            ):
                last_reported_action = latest_action
                last_progress_time = now
                logger.debug("Emitting progress update for %s: %s", conversation_id, latest_action)
                notifier.notify_progress(
                    channel=channel,
                    thread_ts=thread_ts,
                    action_summary=latest_action,
                    elapsed_seconds=elapsed,
                )

        # 2. Result Delivery on completion
        if is_done and final_content:
            logger.info("Delivering final Antigravity result for %s to Slack thread %s:%s", conversation_id, channel, thread_ts)
            notifier.notify_result_delivery(
                channel=channel,
                thread_ts=thread_ts,
                conversation_id=conversation_id,
                content=final_content,
                elapsed_seconds=elapsed,
                is_follow_up=is_follow_up,
            )

            if broker:
                evt_data = {
                    "event": "antigravity_result_delivered",
                    "task_id": task_id,
                    "conversation_id": conversation_id,
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "elapsed_seconds": elapsed,
                    "is_follow_up": is_follow_up,
                    "content_length": len(final_content),
                }
                try:
                    await broker.publish("events", evt_data)
                    await broker.publish(f"task.{task_id}", evt_data)
                except Exception as b_err:
                    logger.debug("Failed to publish result delivery event: %s", b_err)

            return {
                "delivered": True,
                "conversation_id": conversation_id,
                "elapsed_seconds": elapsed,
                "content_preview": final_content[:100],
            }

        # 3. Terminal Error Handling
        if has_error and not final_content:
            logger.warning("Antigravity conversation %s encountered a terminal error step", conversation_id)
            notifier.notify_failed(
                channel=channel,
                thread_ts=thread_ts,
                task_id=task_id,
                error_message="Antigravity 会话在执行过程中遭遇异常中断",
            )
            return {
                "delivered": False,
                "conversation_id": conversation_id,
                "error": "terminal_error_step",
            }

    # Watcher timed out - do one final check on the transcript
    elapsed_total = time.time() - start_time
    transcript_path = resolve_transcript_path(conversation_id, brain_root)
    if transcript_path:
        _, final_content_last, is_done_last, _ = parse_transcript_events(
            transcript_path,
            start_step=start_step,
        )
        if final_content_last:
            final_content = final_content_last

    logger.warning("Conversation watcher timed out after %.1fs for %s", elapsed_total, conversation_id)
    
    # If final content was captured, deliver it
    if final_content:
        notifier.notify_result_delivery(
            channel=channel,
            thread_ts=thread_ts,
            conversation_id=conversation_id,
            content=final_content,
            elapsed_seconds=elapsed_total,
            is_follow_up=is_follow_up,
        )
        return {
            "delivered": True,
            "conversation_id": conversation_id,
            "elapsed_seconds": elapsed_total,
            "timed_out": True,
        }

    # Notify failure to avoid silent thread abandonment
    notifier.notify_failed(
        channel=channel,
        thread_ts=thread_ts,
        task_id=task_id,
        error_message=f"Antigravity 会话处理超时（持续运行超过 {int(elapsed_total)}s 未产生终态），请检查桌面端运行状态。",
    )

    return {
        "delivered": False,
        "conversation_id": conversation_id,
        "error": "timed_out",
    }
