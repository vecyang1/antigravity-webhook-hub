"""
Antigravity Webhook Hub — Antigravity Session & Thread Manager
Orchestrates task execution, thread-to-session linking, follow-up (追问) routing,
multi-image resolution, and Slack thread transparency notifications.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from hub.antigravity.result_delivery import (
    cancel_active_watcher,
    get_latest_step_index,
    watch_and_deliver_result,
)
from hub.antigravity.thread_notifier import ThreadNotifier

logger = logging.getLogger("hub.antigravity.session_manager")


async def execute_antigravity_task(
    task_data: dict[str, Any],
    db: Any,
    log_callback: Optional[Callable[[str], None]] = None,
    agentapi_client: Optional[AgentAPIClient] = None,
    thread_notifier: Optional[ThreadNotifier] = None,
    broker: Optional[Any] = None,
) -> dict[str, Any]:
    """
    Execute an Antigravity task with complete lifecycle transparency,
    SSOT database persistence, self-healing expired session recovery,
    429 quota exhaustion resilience, and thread-linked follow-up routing.
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
        cmd_candidate = str(task_data["command"]).strip()
        if cmd_candidate and not cmd_candidate.startswith("agentapi"):
            payload.text = cmd_candidate

    channel = payload.channel
    ts = payload.ts
    thread_ts = payload.thread_ts
    root_ts = thread_ts or ts or ""
    thread_key = f"{channel}:{root_ts}" if channel and root_ts else ""

    # Check if this is a follow-up (追问)
    is_follow_up = bool(thread_ts and thread_ts != ts)
    payload.is_follow_up = is_follow_up

    # Gating: do not post milestone comments to Slack if stress testing or explicitly muted
    source_str = str(payload.source or task_data.get("source") or "")
    skip_notify = (
        source_str.endswith("_stress")
        or bool(params_dict.get("skip_slack_notify"))
        or bool(task_data.get("skip_slack_notify"))
    )
    should_notify_slack = not skip_notify

    # =========================================================================
    # TOKEN FIREWALL: Strictly bypass agentapi & LLM execution on stress tests / synthetic tasks
    # =========================================================================
    text_str = str(payload.text or "")
    is_synthetic_test = (
        source_str.endswith("_stress")
        or "stress" in source_str.lower()
        or bool(payload.metadata.get("is_test"))
        or bool(payload.metadata.get("dry_run"))
        or bool(params_dict.get("is_test"))
        or bool(params_dict.get("dry_run"))
        or bool(task_data.get("is_test"))
        or bool(task_data.get("dry_run"))
        or text_str.startswith("Stress test")
        or "[STRESS_TEST]" in text_str
    )
    if is_synthetic_test:
        elapsed = time.time() - start_time
        synthetic_convo_id = f"synthetic_test_{task_id[:8]}"
        log(f"[TOKEN_FIREWALL] Synthetic/stress test detected ({task_id}, source={source_str}). Bypassing agentapi LLM call to protect token budget.")
        return {
            "success": True,
            "conversation_id": synthetic_convo_id,
            "thread_key": thread_key,
            "task_id": task_id,
            "elapsed_seconds": elapsed,
            "model_tier": "mock",
            "slash_commands": [],
            "downloaded_images": [],
            "is_test": True,
        }

    # Step 1: Download any image attachments locally
    downloaded_images: list[str] = []
    if payload.files:
        log(f"Downloading {len(payload.files)} attached file(s) for local access...")
        downloaded_images = download_slack_images(payload.files, task_id=task_id)
        payload.downloaded_images = downloaded_images
        log(f"Downloaded {len(downloaded_images)} valid image(s) to local disk.")

    session_recovered_from: Optional[str] = None

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

            # Cancel any prior active watcher for this session immediately to prevent stale delivery while send_message is in flight
            cancel_active_watcher(convo_id)

            follow_up_prompt = build_follow_up_prompt(payload, downloaded_images=downloaded_images)
            latest_step = get_latest_step_index(convo_id)
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
                if should_notify_slack and channel and thread_ts:
                    notifier.notify_follow_up(
                        channel=channel,
                        thread_ts=thread_ts,
                        conversation_id=convo_id,
                        snippet=payload.text or payload.voice_transcript or "已同步附件",
                        files_count=len(downloaded_images),
                    )
                    # Launch background watcher for follow-up progress and result delivery
                    asyncio.create_task(
                        watch_and_deliver_result(
                            notifier=notifier,
                            channel=channel,
                            thread_ts=thread_ts,
                            conversation_id=convo_id,
                            task_id=task_id,
                            start_time=start_time,
                            is_follow_up=True,
                            start_step=max(0, latest_step + 1),
                            broker=broker,
                            db=db,
                        )
                    )

                if broker:
                    evt_data = {
                        "event": "antigravity_followup_synced",
                        "task_id": task_id,
                        "conversation_id": convo_id,
                        "channel": channel,
                        "thread_ts": thread_ts,
                        "elapsed_seconds": elapsed,
                    }
                    try:
                        await broker.publish("events", evt_data)
                        await broker.publish(f"task.{task_id}", evt_data)
                    except Exception as e:
                        logger.debug("Failed to publish followup sync event to broker: %s", e)

                return {
                    "success": True,
                    "is_follow_up": True,
                    "conversation_id": convo_id,
                    "thread_key": thread_key,
                    "elapsed_seconds": elapsed,
                }
            else:
                log(f"Failed to deliver follow-up message to session {convo_id}: {err_msg}")
                # Auto-recovery: if session not found, fall back to creating a new conversation
                if "not found" in (err_msg or "").lower() or "invalid" in (err_msg or "").lower():
                    log(f"Session {convo_id} is dead/expired. Initiating auto-recovery into a fresh session...")
                    if hasattr(db, "update_session_thread_status"):
                        res = db.update_session_thread_status(thread_key, "expired")
                        if asyncio.iscoroutine(res):
                            await res
                    if broker:
                        rec_evt = {
                            "event": "antigravity_session_recovery_triggered",
                            "task_id": task_id,
                            "old_conversation_id": convo_id,
                            "thread_key": thread_key,
                            "reason": err_msg,
                        }
                        try:
                            await broker.publish("events", rec_evt)
                            await broker.publish(f"task.{task_id}", rec_evt)
                        except Exception as e:
                            logger.debug("Failed to publish recovery event to broker: %s", e)

                    # Fall through to Branch B for self-healing
                    session_recovered_from = convo_id
                else:
                    if should_notify_slack and channel and thread_ts:
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
    # BRANCH B: New Antigravity Task Creation (or Recovered Session Migration)
    # =========================================================================
    _, extracted_cmds, _ = extract_slash_commands(payload.text)
    all_cmds = list(dict.fromkeys(payload.slash_commands + extracted_cmds))

    # Milestone: notify in_progress under thread
    if should_notify_slack and channel and root_ts:
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

    orig_model = payload.model_tier
    log(f"Launching Antigravity conversation (model: {payload.model_tier}, commands: {all_cmds})...")
    workspace_dir = (
        payload.metadata.get("workspace")
        or payload.metadata.get("cwd")
        or os.environ.get("ANTIGRAVITY_DEFAULT_WORKSPACE")
        or "/Users/vecsatfoxmailcom/Documents/Cowork/Antigravity Cowork"
    )
    success, convo_id, err_msg = await client.new_conversation(
        prompt=prompt,
        model=payload.model_tier,
        title=task_title,
        cwd=workspace_dir,
    )

    # Quota exhaustion & 429 self-healing auto-downgrade to flash_lite
    if not success and err_msg:
        err_lower = err_msg.lower()
        if any(k in err_lower for k in ("429", "quota", "rate limit", "resource exhausted")):
            if payload.model_tier != "flash_lite":
                log(f"Quota exhaustion / rate limit detected on {orig_model} ({err_msg}). Auto-downgrading to flash_lite...")
                payload.model_tier = "flash_lite"
                if broker:
                    downgrade_evt = {
                        "event": "antigravity_model_downgraded",
                        "task_id": task_id,
                        "original_model": orig_model,
                        "downgraded_model": "flash_lite",
                        "reason": err_msg,
                    }
                    try:
                        await broker.publish("events", downgrade_evt)
                        await broker.publish(f"task.{task_id}", downgrade_evt)
                    except Exception as e:
                        logger.debug("Failed to publish downgrade event to broker: %s", e)

                success, convo_id, err_msg = await client.new_conversation(
                    prompt=prompt,
                    model="flash_lite",
                    title=task_title,
                )

    elapsed = time.time() - start_time

    if not success:
        log(f"Failed to start Antigravity conversation: {err_msg}")
        if should_notify_slack and channel and root_ts:
            notifier.notify_failed(
                channel=channel,
                thread_ts=root_ts,
                task_id=task_id,
                error_message=err_msg or "无法创建 Antigravity 会话",
            )
        if broker:
            fail_evt = {
                "event": "antigravity_session_failed",
                "task_id": task_id,
                "thread_key": thread_key,
                "error": err_msg,
                "elapsed_seconds": elapsed,
            }
            try:
                await broker.publish("events", fail_evt)
                await broker.publish(f"task.{task_id}", fail_evt)
            except Exception as e:
                logger.debug("Failed to publish fail event to broker: %s", e)
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

    # Launch background watcher for live execution progress and true result delivery
    if should_notify_slack and channel and root_ts:
        cancel_active_watcher(convo_id)
        asyncio.create_task(
            watch_and_deliver_result(
                notifier=notifier,
                channel=channel,
                thread_ts=root_ts,
                conversation_id=convo_id,
                task_id=task_id,
                start_time=start_time,
                is_follow_up=False,
                start_step=0,
                broker=broker,
                db=db,
            )
        )

    if broker:
        created_evt = {
            "event": "antigravity_session_created",
            "task_id": task_id,
            "conversation_id": convo_id,
            "thread_key": thread_key,
            "channel": channel,
            "root_ts": root_ts,
            "model_tier": payload.model_tier,
            "elapsed_seconds": elapsed,
        }
        if session_recovered_from:
            created_evt["recovered_from"] = session_recovered_from
        try:
            await broker.publish("events", created_evt)
            await broker.publish(f"task.{task_id}", created_evt)
        except Exception as e:
            logger.debug("Failed to publish created event to broker: %s", e)

    res_dict = {
        "success": True,
        "conversation_id": convo_id,
        "thread_key": thread_key,
        "task_id": task_id,
        "elapsed_seconds": elapsed,
        "model_tier": payload.model_tier,
        "slash_commands": all_cmds,
        "downloaded_images": downloaded_images,
    }
    if session_recovered_from:
        res_dict["recovered_from"] = session_recovered_from
    return res_dict

async def resume_active_watchers(db: Any, broker: Optional[Any] = None) -> int:
    """
    On server boot, re-mounts the Slack delivery watcher for all 'active' session threads
    so that if the server restarts while an Antigravity conversation is generating,
    the user will still receive the final reply in Slack.
    """
    count = 0
    try:
        if not hasattr(db, "execute_read"):
            return 0
        active_sessions = await db.execute_read(
            "SELECT * FROM session_threads WHERE status = 'active'"
        )
        if not active_sessions:
            return 0
            
        notifier = ThreadNotifier()
        for sess in active_sessions:
            channel = sess.get("channel_id")
            root_ts = sess.get("root_ts")
            convo_id = sess.get("conversation_id")
            task_id = sess.get("task_id")
            
            if not channel or not root_ts or not convo_id:
                continue
                
            logger.info("Remounting active watcher for recovered session %s (Task: %s)", convo_id, task_id)
            cancel_active_watcher(convo_id)
            asyncio.create_task(
                watch_and_deliver_result(
                    notifier=notifier,
                    channel=channel,
                    thread_ts=root_ts,
                    conversation_id=convo_id,
                    task_id=task_id,
                    start_time=0.0,
                    is_follow_up=False,  
                    start_step=0,
                    broker=broker,
                    db=db,
                )
            )
            count += 1
    except Exception as e:
        logger.exception("Failed to resume active watchers: %s", e)
    return count
