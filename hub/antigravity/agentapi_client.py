"""
Antigravity Webhook Hub — AgentAPI Programmatic Bridge Client
Wraps Google Antigravity's native `agentapi` CLI tool to launch new conversations,
dispatch follow-up messages into existing sessions, and inspect conversation metadata.
Includes deterministic dynamic credential discovery and self-healing auto-retry
when language_server dynamically rebinds its gRPC port on Electron restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Optional, Tuple

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


def validate_antigravity_address(address: str, timeout: float = 0.3) -> bool:
    """
    Check if address points to a responsive cleartext gRPC HTTP server.
    Cleartext gRPC server returns HTTP 200 OK on HEAD /; TLS port returns 400 Bad Request.
    """
    if not address:
        return False
    try:
        clean = address.strip().replace("http://", "").replace("https://", "").rstrip("/")
        if ":" not in clean:
            return False
        host, port = clean.replace("localhost", "127.0.0.1").split(":", 1)
        req = urllib.request.Request(f"http://{host}:{port}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def discover_active_antigravity_credentials(force: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """
    Deterministically discover live Antigravity language_server gRPC address and CSRF token on macOS.
    1. Validate existing environment variables (ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN).
       If force=False and address is valid via HTTP probe, return them immediately (~15ms).
    2. Read `ps auxww` for `language_server --standalone` process:
       Extract server_pid and --csrf_token argument, filtering out false-positive runners (python, grep).
    3. Check child processes of server_pid via `pgrep -P <pid>` and `ps eww <child_pid>`
       for ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN.
    4. Fallback: inspect listening TCP ports on server_pid via `lsof -a -p <pid> -iTCP -sTCP:LISTEN -nP`.
       Probe http://127.0.0.1:{port}/ with HEAD request. The cleartext gRPC HTTP server returns 200 OK.
    5. Cache and export discovered credentials to os.environ so child subprocesses inherit them.
    Returns: (address, csrf_token) or (None, None)
    """
    env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    env_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if not force and env_addr and env_token:
        if validate_antigravity_address(env_addr):
            return env_addr, env_token

    server_pid: Optional[int] = None
    csrf_token: Optional[str] = env_token
    ls_address: Optional[str] = None

    # Step 1: Find standalone language_server PID and CSRF token
    try:
        try:
            ps_out = subprocess.check_output(["ps", "auxww"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            ps_out = subprocess.check_output(["ps", "aux"], text=True, stderr=subprocess.DEVNULL)

        for line in ps_out.splitlines():
            line_clean = line.strip()
            if not line_clean:
                continue
            if "language_server" not in line_clean or "--standalone" not in line_clean:
                continue
            # Avoid matching python runners, test scripts, or shell grep
            if re.search(r'\b(python\d*|pytest|grep|sh|bash|zsh)\b', line_clean) and "/bin/language_server" not in line_clean:
                continue
            # Ensure command path actually looks like language_server executable
            if not re.search(r'(?:^|\s)(?:/\S*/)?language_server\s+--standalone', line_clean):
                continue

            parts = line_clean.split()
            if len(parts) > 1 and parts[1].isdigit():
                server_pid = int(parts[1])
            m_token = re.search(r"--csrf_token(?:=|\s+)([0-9a-fA-F-]+)", line_clean)
            if m_token:
                csrf_token = m_token.group(1)
            break
    except Exception as e:
        logger.debug("Failed to inspect ps for language_server: %s", e)

    if server_pid:
        # Step 2: Check child processes for exported environment variables
        try:
            pgrep_out = subprocess.check_output(
                ["pgrep", "-P", str(server_pid)],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            child_pids = [int(p) for p in pgrep_out.split() if p.isdigit()]
            for cpid in child_pids:
                try:
                    ps_eww = subprocess.check_output(
                        ["ps", "eww", str(cpid)],
                        text=True,
                        stderr=subprocess.DEVNULL,
                    )
                    m_addr = re.search(r"ANTIGRAVITY_LS_ADDRESS=([^\s]+)", ps_eww)
                    m_csrf = re.search(r"ANTIGRAVITY_CSRF_TOKEN=([^\s]+)", ps_eww)
                    if m_csrf and not csrf_token:
                        csrf_token = m_csrf.group(1)
                    if m_addr:
                        cand_addr = m_addr.group(1)
                        if validate_antigravity_address(cand_addr):
                            ls_address = cand_addr
                            break
                except Exception:
                    continue
        except Exception as e:
            logger.debug("Failed to inspect child processes of server PID %d: %s", server_pid, e)

        # Step 3: Fallback to lsof listening ports if child inspection did not yield valid address
        if not ls_address:
            try:
                lsof_out = subprocess.check_output(
                    ["lsof", "-a", "-p", str(server_pid), "-iTCP", "-sTCP:LISTEN", "-nP"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                ports = re.findall(r":(\d+)\s+\(LISTEN\)", lsof_out)
                for port in ports:
                    candidate = f"localhost:{port}"
                    if validate_antigravity_address(candidate):
                        ls_address = candidate
                        break
            except Exception as e:
                logger.debug("Failed to probe lsof listening ports for server PID %d: %s", server_pid, e)

    if ls_address and csrf_token:
        os.environ["ANTIGRAVITY_LS_ADDRESS"] = ls_address
        os.environ["ANTIGRAVITY_CSRF_TOKEN"] = csrf_token
        logger.info(
            "Discovered active Antigravity credentials: address=%s, csrf_token=%s",
            ls_address,
            csrf_token[:8] + "...",
        )
        return ls_address, csrf_token

    if ls_address and not csrf_token:
        os.environ["ANTIGRAVITY_LS_ADDRESS"] = ls_address
        return ls_address, None

    return None, None


def is_connection_error(err_msg: Optional[str]) -> bool:
    """Check if an error string indicates language_server connection failure or port mismatch."""
    if not err_msg:
        return False
    msg_lower = err_msg.lower()
    return any(
        pattern in msg_lower
        for pattern in (
            "connection refused",
            "code = unavailable",
            "unavailable",
            "connection error",
            "error while dialing",
            "dial tcp",
            "broken pipe",
            "reset by peer",
            "transport: error while dialing",
            "channel is in state transient_failure",
            "failed to connect",
            "connection closed",
            "transport is closing",
            "deadlineexceeded",
            "network is unreachable",
            "no route to host",
        )
    )


def find_active_language_server_pid() -> Optional[int]:
    """Find live PID of running language_server --standalone process on macOS."""
    try:
        try:
            ps_out = subprocess.check_output(["ps", "auxww"], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            ps_out = subprocess.check_output(["ps", "aux"], text=True, stderr=subprocess.DEVNULL)

        for line in ps_out.splitlines():
            line_clean = line.strip()
            if not line_clean or "language_server" not in line_clean or "--standalone" not in line_clean:
                continue
            if re.search(r'\b(python\d*|pytest|grep|sh|bash|zsh)\b', line_clean) and "/bin/language_server" not in line_clean:
                continue
            if not re.search(r'(?:^|\s)(?:/\S*/)?language_server\s+--standalone', line_clean):
                continue

            parts = line_clean.split()
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
    except Exception as e:
        logger.debug("Failed checking language_server PID: %s", e)
    return None


class AgentAPIClient:
    """Client for interacting with the Google Antigravity AgentAPI CLI."""

    def __init__(self, executable_path: Optional[str] = None):
        self.executable_path = executable_path or resolve_agentapi_path()
        if not self.executable_path:
            logger.warning("agentapi executable not found at default paths.")
        self.ls_address: Optional[str] = None
        self.csrf_token: Optional[str] = None

    def is_available(self) -> bool:
        """Check if agentapi binary is accessible and executable."""
        if not self.executable_path or not (os.path.isfile(self.executable_path) and os.access(self.executable_path, os.X_OK)):
            resolved = resolve_agentapi_path()
            if resolved:
                self.executable_path = resolved
        return bool(self.executable_path and os.path.isfile(self.executable_path) and os.access(self.executable_path, os.X_OK))

    def ensure_credentials(self, force: bool = False) -> Tuple[Optional[str], Optional[str]]:
        """Validate current credentials or discover active language_server credentials."""
        addr, token = discover_active_antigravity_credentials(force=force)
        if force:
            self.ls_address = addr
            self.csrf_token = token
        else:
            if addr:
                self.ls_address = addr
            if token:
                self.csrf_token = token
        return addr, token

    def get_language_server_pid(self) -> Optional[int]:
        """Return live PID of the running language_server process."""
        return find_active_language_server_pid()

    def resolve_conversation_project_id(
        self,
        conversation_id: str,
        conversations_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """
        Extract the target session's project ID from its local conversation SQLite DB.
        Falls back to None if not found or unreadable.
        """
        c_dir = conversations_dir or Path(os.path.expanduser("~/.gemini/antigravity/conversations"))
        db_path = c_dir / f"{conversation_id}.db"
        if db_path.exists():
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
                cur = conn.cursor()
                cur.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'")
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    # Protobuf field 18 wire type 2: 0x92 0x01 0x24 (length 36)
                    m = re.search(rb"\x92\x01\$([0-9a-fA-F-]{36})", row[0])
                    if m:
                        return m.group(1).decode("ascii")
                    # Fallback to general UUID regex in last 250 bytes
                    m_fb = re.search(rb"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", row[0][-250:])
                    if m_fb:
                        return m_fb.group(1).decode("ascii")
            except Exception as e:
                logger.debug("Failed extracting project_id from db for %s: %s", conversation_id, e)
        return None

    def _get_env(self, project_id: Optional[str] = None) -> dict[str, str]:
        """Build execution environment injecting active credentials and target session project ID."""
        env = dict(os.environ)
        # Strip host-session environment variables that cause cross-conversation collisions
        for key in (
            "ANTIGRAVITY_CONVERSATION_ID",
            "ANTIGRAVITY_SOURCE_METADATA",
            "ANTIGRAVITY_TRAJECTORY_ID",
        ):
            env.pop(key, None)

        # Prioritize target session's project ID over global environment override
        target_pid = project_id or os.environ.get("ANTIGRAVITY_PROJECT_ID") or "bb66eaa1-71f2-46f3-8deb-c00846bdd779"
        env["ANTIGRAVITY_PROJECT_ID"] = target_pid

        if self.ls_address:
            env["ANTIGRAVITY_LS_ADDRESS"] = self.ls_address
        if self.csrf_token:
            env["ANTIGRAVITY_CSRF_TOKEN"] = self.csrf_token
        return env

    async def _execute_with_retry(
        self,
        cmd: list[str],
        cwd: Optional[str] = None,
        timeout_seconds: float = 60.0,
        project_id: Optional[str] = None,
    ) -> tuple[int, str, str]:
        """
        Execute an agentapi CLI command with automatic credential discovery,
        detecting connection/Unavailable errors, re-discovering credentials,
        and retrying once before returning.
        Returns: (returncode, stdout_str, stderr_str)
        """
        self.ensure_credentials(force=False)

        async def _run() -> tuple[int, str, str]:
            actual_cmd = list(cmd)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *actual_cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=self._get_env(project_id=project_id),
                )
            except OSError as oe:
                if oe.errno == 8:
                    actual_cmd = ["/bin/sh"] + actual_cmd
                    proc = await asyncio.create_subprocess_exec(
                        *actual_cmd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=cwd,
                        env=self._get_env(project_id=project_id),
                    )
                else:
                    raise
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(timeout_seconds),
            )
            stdout_str = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()
            return proc.returncode, stdout_str, stderr_str

        try:
            returncode, stdout_str, stderr_str = await _run()
        except asyncio.TimeoutError:
            raise

        err_output = stderr_str or stdout_str
        if returncode != 0 and is_connection_error(err_output):
            logger.warning(
                "agentapi connection error detected (code %d: %s). Re-discovering active Antigravity credentials and retrying once...",
                returncode,
                err_output,
            )
            new_addr, new_token = self.ensure_credentials(force=True)
            if new_addr and new_token:
                logger.info(
                    "Re-discovered Antigravity credentials (addr=%s). Retrying command...",
                    new_addr,
                )
                try:
                    returncode, stdout_str, stderr_str = await _run()
                except Exception as retry_err:
                    logger.error("Retry of agentapi command failed: %s", retry_err)
                    stderr_str = f"{stderr_str}\nRetry failed: {retry_err}"

        return returncode, stdout_str, stderr_str

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
            returncode, stdout_str, stderr_str = await self._execute_with_retry(
                cmd,
                cwd=cwd,
                timeout_seconds=float(timeout_seconds),
            )

            if returncode != 0:
                err_msg = stderr_str or stdout_str or f"agentapi exited with code {returncode}"
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
        project_id: Optional[str] = None,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Send a follow-up inquiry (追问) or message into an existing Antigravity conversation.
        Returns: (success, result_message, error_message)
        """
        if not self.is_available():
            return False, "", f"agentapi executable not available at '{self.executable_path}'"

        target_pid = project_id or self.resolve_conversation_project_id(conversation_id)

        cmd = [self.executable_path, "send-message"]
        if title:
            cmd.append(f"--title={title}")

        cmd.extend([conversation_id, content])

        try:
            returncode, stdout_str, stderr_str = await self._execute_with_retry(
                cmd,
                timeout_seconds=float(timeout_seconds),
                project_id=target_pid,
            )

            if returncode != 0:
                err_msg = stderr_str or stdout_str or f"agentapi exited with code {returncode}"
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
            returncode, stdout_str, _ = await self._execute_with_retry(
                [self.executable_path, "get-conversation-metadata", conversation_id],
                timeout_seconds=float(timeout_seconds),
            )
            if returncode == 0:
                return json.loads(stdout_str)
        except Exception as e:
            logger.debug("Failed to get conversation metadata for %s: %s", conversation_id, e)
        return None

