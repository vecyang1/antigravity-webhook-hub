"""
Antigravity Webhook Hub — AgentAPI Programmatic Bridge Client
Wraps Google Antigravity's native `agentapi` CLI tool to launch new conversations,
dispatch follow-up messages into existing sessions, and inspect conversation metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hub.antigravity.agentapi")

DEFAULT_AGENTAPI_PATH = os.path.expanduser("~/.gemini/antigravity/bin/agentapi")


def resolve_agentapi_path() -> Optional[str]:
    """Find the verified agentapi executable on macOS."""
    env_path = os.environ.get("ANTIGRAVITY_AGENTAPI_PATH")
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        return env_path

    if os.path.isfile(DEFAULT_AGENTAPI_PATH) and os.access(DEFAULT_AGENTAPI_PATH, os.X_OK):
        return DEFAULT_AGENTAPI_PATH

    which_path = shutil.which("agentapi")
    if which_path:
        return which_path

    return None


class AgentAPIClient:
    """Client for interacting with the Google Antigravity AgentAPI CLI."""

    def __init__(self, executable_path: Optional[str] = None):
        self.executable_path = executable_path or resolve_agentapi_path()
        if not self.executable_path:
            logger.warning("agentapi executable not found at default paths.")

    def is_available(self) -> bool:
        """Check if agentapi binary is accessible and executable."""
        return bool(self.executable_path and os.path.isfile(self.executable_path) and os.access(self.executable_path, os.X_OK))

    async def new_conversation(
        self,
        prompt: str,
        model: str = "pro",
        title: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout_seconds: int = 60,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Create a new Antigravity agent conversation.
        Returns: (success, conversation_id, error_message)
        """
        if not self.is_available():
            return False, "", f"agentapi executable not available at '{self.executable_path}'"

        cmd = [self.executable_path, "new-conversation"]
        if model in ("flash_lite", "flash", "pro"):
            cmd.append(f"--model={model}")
        else:
            cmd.append("--model=pro")

        if title:
            cmd.append(f"--title={title}")

        cmd.append(prompt)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout_seconds),
            )
            stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                err_msg = stderr_str or stdout_str or f"agentapi exited with code {proc.returncode}"
                logger.error("agentapi new-conversation failed: %s", err_msg)
                return False, "", err_msg

            # Parse JSON output from agentapi
            try:
                data = json.loads(stdout_str)
                conv_info = data.get("response", {}).get("newConversation", {})
                convo_id = conv_info.get("conversationId")
                if convo_id:
                    logger.info("Successfully created Antigravity conversation: %s", convo_id)
                    return True, str(convo_id), None
            except json.JSONDecodeError:
                pass

            # Fallback if raw output format contains conversationId
            for line in stdout_str.splitlines():
                if "conversationId" in line:
                    import re
                    m = re.search(r'[\'"]?([0-9a-fA-F-]{36})[\'"]?', line)
                    if m:
                        return True, m.group(1), None

            return False, "", f"Unable to parse conversationId from output: {stdout_str[:200]}"

        except asyncio.TimeoutError:
            return False, "", f"agentapi new-conversation timed out after {timeout_seconds}s"
        except Exception as e:
            logger.exception("Unexpected error in agentapi new_conversation: %s", e)
            return False, "", str(e)

    async def send_message(
        self,
        conversation_id: str,
        content: str,
        title: Optional[str] = None,
        timeout_seconds: int = 60,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Send a follow-up inquiry (追问) or message into an existing Antigravity conversation.
        Returns: (success, result_message, error_message)
        """
        if not self.is_available():
            return False, "", f"agentapi executable not available at '{self.executable_path}'"

        cmd = [self.executable_path, "send-message"]
        if title:
            cmd.append(f"--title={title}")

        cmd.extend([conversation_id, content])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout_seconds),
            )
            stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                err_msg = stderr_str or stdout_str or f"agentapi exited with code {proc.returncode}"
                logger.error("agentapi send-message failed: %s", err_msg)
                return False, "", err_msg

            logger.info("Successfully sent follow-up message to conversation %s", conversation_id)
            return True, stdout_str, None

        except asyncio.TimeoutError:
            return False, "", f"agentapi send-message timed out after {timeout_seconds}s"
        except Exception as e:
            logger.exception("Unexpected error in agentapi send_message: %s", e)
            return False, "", str(e)

    async def get_metadata(self, conversation_id: str, timeout_seconds: int = 15) -> Optional[dict[str, Any]]:
        """Retrieve conversation metadata."""
        if not self.is_available():
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                self.executable_path,
                "get-conversation-metadata",
                conversation_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=float(timeout_seconds))
            if proc.returncode == 0:
                return json.loads(stdout_bytes.decode("utf-8"))
        except Exception as e:
            logger.debug("Failed to get conversation metadata for %s: %s", conversation_id, e)
        return None
