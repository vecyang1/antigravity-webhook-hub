"""
Antigravity Webhook Hub — Antigravity Session & Thread Manager
Orchestrates task execution, thread-to-session linking, follow-up (追问) routing,
multi-image resolution, and Slack thread transparency notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

from hub.antigravity.agentapi_client import AgentAPIClient
from hub.antigravity.image_downloader import download_slack_images
from hub.antigravity.models import AntigravityTaskPayload, SessionThreadRecord
from hub.antigravity.prompt_builder import (
    build_antigravity_prompt,
    build_follow_up_prompt,
    extract_slash_commands,
)
from hub.antigravity.thread_notifier import ThreadNotifier

logger = logging.getLogger("hub.antigravity.session_manager")


async def execute_antigravity_task(
    task_data: dict[str, Any],
    db: Any,
    log_callback: Optional[Callable[[str], None]] = None,
    agentapi_client: Optional[AgentAPIClient] = None,
    thread_notifier: Optional[ThreadNotifier] = None,
) -> dict[str, Any]:
    """
    Execute an Antigravity task with complete lifecycle transparency,
    SSOT database persistence, and thread-linked follow-up routing.
    """
    start_time = time.time()

    def log(msg: str) -> None:
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    client = agentapi_client or AgentAPIClient()
    notifier = thread_notifier or ThreadNotifier()

    task_id = str(task_data.get("task_id") or "")
    raw_params = task_data.get("action_params_json") or task_data.get("action_params") or {}
    if isinstance(raw_params, str):
        try:
            params_dict = json.loads(raw_params)
        except Exception:
            params_dict = {}
    elif isinstance(raw_params, dict):
        params_dict = raw_params
    else:
        params_dict = {}

    # Extract or synthesize payload
    payload = AntigravityTaskPayload.from_dict(params_dict)
    if not payload.text and task_data.get("command"):
        payload.text = str(task_data["command"])

    channel = payload.channel
    ts = payload.ts
    thread_ts = payload.thread_ts
    root_ts = thread_ts or ts or ""
    thread_key = f"{channel}:{root_ts}" if channel and root_ts else ""

    # Check if this is a follow-up (追问)
    is_follow_up = bool(thread_ts and thread_ts != ts)
    payload.is_follow_up = is_follow_up

    # Step 1: Download any image attachments locally
    downloaded_images: list[str] = []
    if payload.files:
        log(f"Downloading {len(payload.files)} attached file(s) for local access...")
        downloaded_images = download_slack_images(payload.files, task_id=task_id)
        payload.downloaded_images = downloaded_images
        log(f"Downloaded {len(downloaded_images)} valid image(s) to local disk.")

    # =========================================================================
    # BRANCH A: Follow-up Inquiry (追问) under an existing Thread
    # =========================================================================
    if is_follow_up and thread_key:
        log(f"Detected follow-up inquiry under thread {thread_key}. Looking up linked Antigravity session...")
        existing_session = None
        if hasattr(db, "get_session_thread"):
            res = db.get_session_thread(thread_key)
            existing_session = await res if asyncio.iscoroutine(res) else res

        if existing_session:
            convo_id = existing_session["conversation_id"]
            log(f"Found existing Antigravity session: {convo_id}. Dispatching follow-up prompt...")

            follow_up_prompt = build_follow_up_prompt(payload, downloaded_images=downloaded_images)
            success, resp_str, err_msg = await client.send_message(
                conversation_id=convo_id,
                content=follow_up_prompt,
                title=f"Follow-up on {thread_ts}",
            )

            elapsed = time.time() - start_time
            if success:
                log(f"Follow-up message successfully delivered to session {convo_id} ({elapsed:.1f}s)")
                # Touch session in DB
                if hasattr(db, "touch_session_thread"):
                    touch_res = db.touch_session_thread(thread_key)
                    if asyncio.iscoroutine(touch_res):
                        await touch_res

                # Post thread reply
                if channel and thread_ts:
                    notifier.notify_follow_up(
                        channel=channel,
                        thread_ts=thread_ts,
                        conversation_id=convo_id,
                        snippet=payload.text or payload.voice_transcript or "已同步附件",
                        files_count=len(downloaded_images),
                    )

                return {
                    "success": True,
                    "is_follow_up": True,
                    "conversation_id": convo_id,
                    "thread_key": thread_key,
                    "elapsed_seconds": elapsed,
                    "message": "Follow-up synced to existing Antigravity session",
                }
            else:
                log(f"Failed to send follow-up message to session {convo_id}: {err_msg}")
                if channel and thread_ts:
                    notifier.notify_failed(channel=channel, thread_ts=thread_ts, task_id=task_id, error_message=err_msg or "发送追问失败")
                return {
                    "success": False,
                    "is_follow_up": True,
                    "error": err_msg,
                    "conversation_id": convo_id,
                    "thread_key": thread_key,
                }
        else:
            log(f"No existing session found for thread {thread_key}. Promoting follow-up to new conversation...")

    # =========================================================================
    # BRANCH B: New Antigravity Task Creation
    # =========================================================================
    _, extracted_cmds, _ = extract_slash_commands(payload.text)
    all_cmds = list(dict.fromkeys(payload.slash_commands + extracted_cmds))

    # Milestone: notify in_progress under thread
    if channel and root_ts:
        notifier.notify_in_progress(
            channel=channel,
            thread_ts=root_ts,
            task_id=task_id,
            model_tier=payload.model_tier,
            active_skills=all_cmds,
        )

    # Build full multi-modal prompt
    prompt = build_antigravity_prompt(payload, downloaded_images=downloaded_images)
    task_title = payload.title or (payload.text[:40].replace("\n", " ") if payload.text else f"Task {task_id[:8]}")

    log(f"Launching Antigravity conversation (model: {payload.model_tier}, commands: {all_cmds})...")
    success, convo_id, err_msg = await client.new_conversation(
        prompt=prompt,
        model=payload.model_tier,
        title=task_title,
    )

    elapsed = time.time() - start_time

    if not success:
        log(f"Failed to start Antigravity conversation: {err_msg}")
        if channel and root_ts:
            notifier.notify_failed(
                channel=channel,
                thread_ts=root_ts,
                task_id=task_id,
                error_message=err_msg or "无法创建 Antigravity 会话",
            )
        return {
            "success": False,
            "error": err_msg,
            "task_id": task_id,
            "elapsed_seconds": elapsed,
        }

    log(f"Antigravity conversation successfully created: {convo_id} ({elapsed:.1f}s)")

    # SSOT writeback: Save session mapping to database
    if thread_key and hasattr(db, "upsert_session_thread"):
        session_record = {
            "thread_key": thread_key,
            "channel_id": channel or "default",
            "root_ts": root_ts,
            "task_id": task_id,
            "conversation_id": convo_id,
            "source": payload.source,
            "model_tier": payload.model_tier,
            "title": task_title,
            "status": "active",
        }
        res = db.upsert_session_thread(session_record)
        if asyncio.iscoroutine(res):
            await res
        log(f"Linked thread {thread_key} -> conversation {convo_id} in SSOT database.")

    # Milestone: notify done under thread
    if channel and root_ts:
        notifier.notify_done(
            channel=channel,
            thread_ts=root_ts,
            task_id=task_id,
            conversation_id=convo_id,
            elapsed_seconds=elapsed,
            summary=f"已成功加载并指派执行。挂载技能: {', '.join(all_cmds) or '标准模式'}",
        )

    return {
        "success": True,
        "conversation_id": convo_id,
        "thread_key": thread_key,
        "task_id": task_id,
        "elapsed_seconds": elapsed,
        "model_tier": payload.model_tier,
        "slash_commands": all_cmds,
        "downloaded_images": downloaded_images,
    }
