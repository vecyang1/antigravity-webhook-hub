"""
Antigravity Webhook Hub — Thread Transparency & Status Notification Engine
Dispatches real-time milestone comments (collected, in_progress, done, follow_up, failed)
directly into the originating Slack conversation thread for end-to-end transparency.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from hub.contact_review.slack_notifier import resolve_slack_bot_token

logger = logging.getLogger("hub.antigravity.thread_notifier")


class ThreadNotifier:
    """Dispatches milestone status comments under Slack conversation threads."""

    def __init__(self, token: Optional[str] = None):
        self.token = token if token is not None else resolve_slack_bot_token()

    def is_configured(self) -> bool:
        """Check if Slack Bot token is available."""
        return bool(self.token and self.token.startswith("xoxb-"))

    def post_thread_message(
        self,
        channel: str,
        thread_ts: str,
        text: str,
        blocks: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        """
        Post a comment into a specific Slack thread.
        Fails open without crashing background tasks.
        """
        if not self.is_configured():
            logger.debug("ThreadNotifier skipped: SLACK_BOT_TOKEN not configured or invalid.")
            return False

        if not channel or not thread_ts:
            logger.debug("ThreadNotifier skipped: missing channel or thread_ts.")
            return False

        payload: dict[str, Any] = {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if blocks:
            payload["blocks"] = blocks

        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "Antigravity-Webhook-Hub/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    logger.info("Thread comment successfully posted to %s:%s", channel, thread_ts)
                    return True
                else:
                    err = data.get("error")
                    logger.warning("Slack API error posting thread comment to %s:%s: %s", channel, thread_ts, err)
                    return False
        except Exception as e:
            logger.warning("Network error posting thread comment to %s:%s: %s", channel, thread_ts, e)
            return False

    def notify_collected(
        self,
        channel: str,
        thread_ts: str,
        task_id: str,
        title: Optional[str] = None,
        commands: Optional[list[str]] = None,
        files_count: int = 0,
    ) -> bool:
        """Post milestone 1: Task collected and entered queue."""
        cmd_str = ", ".join(f"`/{c}`" for c in commands) if commands else "默认执行"
        title_line = f"• *任务标题*: {title}\n" if title else ""
        msg = (
            f"📥 *[已采集 · Task Collected]*\n"
            f"{title_line}"
            f"• *任务ID*: `{task_id}`\n"
            f"• *识别指令*: {cmd_str}\n"
            f"• *素材附件*: `{files_count}` 个\n"
            f"正在调度启动 Antigravity Agent 会话..."
        )
        return self.post_thread_message(channel, thread_ts, msg)

    def notify_in_progress(
        self,
        channel: str,
        thread_ts: str,
        task_id: str,
        model_tier: str = "pro",
        active_skills: Optional[list[str]] = None,
    ) -> bool:
        """Post milestone 2: Task picked up by dispatcher, executing in Antigravity."""
        skills_str = ", ".join(f"`{s}`" for s in active_skills) if active_skills else "`核心认知`"
        msg = (
            f"⚡ *[处理中 · In Progress]*\n"
            f"• *执行模型*: `{model_tier}`\n"
            f"• *挂载技能*: {skills_str}\n"
            f"Antigravity Agent 正在深度解析上下文与执行任务..."
        )
        return self.post_thread_message(channel, thread_ts, msg)

    def notify_done(
        self,
        channel: str,
        thread_ts: str,
        task_id: str,
        conversation_id: str,
        elapsed_seconds: float = 0.0,
        summary: Optional[str] = None,
    ) -> bool:
        """Post milestone 3: Agent conversation successfully created and ready for follow-up."""
        summary_line = f"\n• *执行摘要*: {summary}" if summary else ""
        time_str = f"{elapsed_seconds:.1f}s" if elapsed_seconds > 0 else "< 1s"
        msg = (
            f"🎉 *[已完成 · Done]*\n"
            f"• *会话ID*: `{conversation_id}`\n"
            f"• *任务状态*: `succeeded`\n"
            f"• *响应耗时*: `{time_str}`"
            f"{summary_line}\n\n"
            f"💬 *追问通道已打通*：直接在此 Thread 下回复文字、发送语音或补充图片，即可无缝对该会话发起【追问】。"
        )
        return self.post_thread_message(channel, thread_ts, msg)

    def notify_follow_up(
        self,
        channel: str,
        thread_ts: str,
        conversation_id: str,
        snippet: str,
        files_count: int = 0,
    ) -> bool:
        """Post follow-up 追问 milestone: inquiry forwarded into existing conversation."""
        files_line = f"（附带 {files_count} 个新素材）" if files_count > 0 else ""
        clean_snippet = snippet.replace("\n", " ").strip()
        if len(clean_snippet) > 80:
            clean_snippet = clean_snippet[:77] + "..."
        msg = (
            f"🔄 *[追问已送达 · Follow-up Synced]*\n"
            f"• *关联会话*: `{conversation_id}`\n"
            f"• *追问内容*: {clean_snippet} {files_line}\n"
            f"已将补充指令同步至 Antigravity 会话中继续执行。"
        )
        return self.post_thread_message(channel, thread_ts, msg)

    def notify_failed(
        self,
        channel: str,
        thread_ts: str,
        task_id: str,
        error_message: str,
    ) -> bool:
        """Post failure notification under thread."""
        msg = (
            f"❌ *[执行异常 · Task Failed]*\n"
            f"• *任务ID*: `{task_id}`\n"
            f"• *原因*: `{error_message}`\n\n"
            f"💡 可在此 Thread 下直接补充更具体的要求重新触发。"
        )
        return self.post_thread_message(channel, thread_ts, msg)
