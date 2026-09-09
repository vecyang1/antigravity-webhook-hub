"""
Antigravity Webhook Hub — Slack Notification Dispatcher
Sends rich contact review verdicts and SSOT verification results back to Slack.
Supports channel & thread replies, graceful error degradation.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from hub.contact_review.models import ContactInput, FieldDiffAction, ReviewResult, ReviewVerdict

logger = logging.getLogger("hub.contact_review.slack")

DEFAULT_SLACK_CHANNEL = "C096KR96AF7"


def resolve_slack_bot_token() -> str:
    """Resolve Slack Bot token from environment, local .env, or n8n-operations-hub."""
    # 1. Environment variable
    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        return token.strip()

    # 2. Local .env
    local_env_path = Path(".env")
    if local_env_path.is_file():
        for line in local_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "SLACK_BOT_TOKEN":
                    val = v.strip().strip("'\"")
                    if val:
                        return val

    # 3. 26.09.07-n8n-operations-hub .env
    hub_env_path = Path("/Users/vecsatfoxmailcom/Documents/A-coding/26.09.07-n8n-operations-hub/.env")
    if hub_env_path.is_file():
        for line in hub_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "SLACK_BOT_TOKEN":
                    val = v.strip().strip("'\"")
                    if val:
                        return val

    return ""


class SlackNotifier:
    """Dispatches contact review notifications to Slack."""

    def __init__(self, token: Optional[str] = None, default_channel: Optional[str] = None):
        self.token = token or resolve_slack_bot_token()
        self.default_channel = default_channel or os.environ.get("SLACK_DEFAULT_CHANNEL") or DEFAULT_SLACK_CHANNEL

    def format_review_message(self, result: ReviewResult, contact: ContactInput) -> str:
        """Construct human-readable markdown message for Slack."""
        verdict = result.verdict

        emoji_map = {
            ReviewVerdict.NO_CHANGE: "🟢",
            ReviewVerdict.SUPPLEMENT: "✨",
            ReviewVerdict.CORRECT: "🔄",
            ReviewVerdict.MERGE: "🔀",
            ReviewVerdict.CREATE: "👤",
        }
        emoji = emoji_map.get(verdict, "🧭")

        # Contact link
        link_str = (
            f"<{result.target_page_url}|{result.target_name}>"
            if result.target_page_url and result.target_name
            else (result.target_name or contact.name or "Unknown Person")
        )

        lines = [
            f"{emoji} *Antigravity Contact Review: {verdict.value.upper()}*",
            f"*Contact*: {link_str}",
            f"*Confidence*: `{result.confidence_score}%`",
        ]

        if result.explanation:
            lines.append(f"*Analysis*: {result.explanation}")

        # Field diffs
        if result.diffs:
            diff_lines = []
            for d in result.diffs:
                if d.action == FieldDiffAction.NO_CHANGE:
                    continue
                elif d.action == FieldDiffAction.SUPPLEMENT:
                    diff_lines.append(f"• *{d.field_name.capitalize()}*: `{d.new_value}` _(Added)_")
                elif d.action == FieldDiffAction.CORRECT:
                    diff_lines.append(f"• *{d.field_name.capitalize()}*: `{d.new_value}` _(Updated from: '{d.old_value}')_")
                elif d.action == FieldDiffAction.CONFLICT:
                    diff_lines.append(f"• *{d.field_name.capitalize()}*: `{d.new_value}` _(Merged conflict)_")

            if diff_lines:
                lines.append("\n*Changes:*")
                lines.extend(diff_lines)

        # Media attachment badge
        if getattr(result, "appended_image_count", 0) > 0:
            lines.append(f"🖼️ *Media*: {result.appended_image_count} image(s) attached to Notion profile")

        # SSOT verification badge
        if result.ssot_verified:
            lines.append("\n*SSOT Status*: Verified against Notion People CRM ✅")
        elif result.applied:
            lines.append("\n*SSOT Status*: Applied to Notion CRM ⚠️ (Pending re-read)")

        return "\n".join(lines)

    def send_notification(
        self,
        result: ReviewResult,
        contact: ContactInput,
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
    ) -> bool:
        """
        Send formatted notification to Slack channel/thread.
        Fails safely and returns False if Slack is unreachable or token is invalid.
        """
        if not self.token:
            logger.warning("Slack notification skipped: No SLACK_BOT_TOKEN found.")
            return False

        target_channel = channel or contact.slack_channel or self.default_channel
        target_thread = thread_ts or contact.slack_thread_ts

        text_message = self.format_review_message(result, contact)

        payload: dict[str, Any] = {
            "channel": target_channel,
            "text": text_message,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if target_thread:
            payload["thread_ts"] = target_thread

        url = "https://slack.com/api/chat.postMessage"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Antigravity-Webhook-Hub/1.0",
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                if resp_data.get("ok"):
                    logger.info("Slack notification sent successfully to %s", target_channel)
                    return True
                else:
                    err = resp_data.get("error")
                    logger.warning("Slack API returned error: %s", err)
                    return False
        except Exception as e:
            logger.warning("Failed to send Slack notification: %s", e)
            return False
