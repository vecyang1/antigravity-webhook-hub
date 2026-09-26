"""
Antigravity Webhook Hub — Multi-Image Local Attachment Downloader
Downloads private Slack image files and web image assets locally into the data/attachments/
directory, giving local Antigravity agents direct filesystem access to all visual inputs.
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from hub.contact_review.slack_notifier import resolve_slack_bot_token

logger = logging.getLogger("hub.antigravity.image_downloader")

IMAGE_MIMETYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
}


def sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent path traversal and shell injection."""
    pure_name = Path(name).name
    cleaned = re.sub(r'[^a-zA-Z0-9._-]', '_', pure_name)
    cleaned = cleaned.lstrip('.')
    return cleaned[:60] if cleaned else "attachment.png"


def download_file_with_curl(
    url: str,
    dest_path: Path,
    bot_token: Optional[str] = None,
    timeout: int = 30,
) -> bool:
    """
    Download a remote file using system curl with Bearer authorization and redirects.
    Ensures robust streaming from Slack's CloudFront/Envoy CDN (files.slack.com)
    where Python urllib may encounter IncompleteRead on macOS.
    """
    cmd = [
        "curl",
        "--fail",
        "--max-time", str(timeout),
        "-sS",
        "-L",
        "--location-trusted",
        "-A", "Antigravity-Webhook-Hub/1.0",
        "-o", str(dest_path),
    ]
    if bot_token and "slack.com" in url:
        cmd.extend(["-H", f"Authorization: Bearer {bot_token}"])
    cmd.extend(["--", url])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        if res.returncode == 0 and dest_path.is_file() and dest_path.stat().st_size > 0:
            # Detect HTML error pages or Slack error JSON disguised as 200 OK
            header_sample = b""
            try:
                with open(dest_path, "rb") as f:
                    header_sample = f.read(256)
            except Exception:
                pass

            if header_sample.startswith((b"<!DOCTYPE", b"<html", b"<HTML", b'{"ok":false', b'{"ok": false', b"The requested file could not be found")):
                logger.warning(
                    "curl downloaded error/HTML content instead of image for %s (sample: %s)",
                    url, header_sample[:60],
                )
                dest_path.unlink(missing_ok=True)
                return False

            return True

        logger.warning(
            "curl download failed for %s (exit code %d): %s",
            url, res.returncode, res.stderr.strip() if res.stderr else "empty destination",
        )
        dest_path.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.warning("curl execution error downloading %s: %s", url, e)
        dest_path.unlink(missing_ok=True)
        return False


def download_slack_images(
    files: list[dict[str, Any]],
    task_id: str,
    target_base_dir: Optional[Path] = None,
    token: Optional[str] = None,
    max_images: int = 10,
) -> list[str]:
    """
    Download private Slack images to local storage for Antigravity access.
    Returns list of local absolute file paths.
    """
    if not files:
        return []

    bot_token = token or resolve_slack_bot_token()
    base_dir = target_base_dir or (Path("data") / "attachments" / task_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    downloaded_paths: list[str] = []

    count = 0
    for f in files:
        if count >= max_images:
            break

        mimetype = str(f.get("mimetype") or "").lower()
        # Accept images or files with image extensions
        name = str(f.get("name") or "image.png")
        ext = Path(name).suffix.lower()
        if mimetype not in IMAGE_MIMETYPES and ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            continue

        download_url = f.get("url_private_download") or f.get("url_private") or f.get("url")
        if not download_url:
            continue

        safe_name = f"{count + 1}_{sanitize_filename(name)}"
        dest_path = base_dir / safe_name

        # If already exists and has size > 0, reuse
        if dest_path.is_file() and dest_path.stat().st_size > 0:
            downloaded_paths.append(str(dest_path.resolve()))
            count += 1
            continue

        headers = {
            "User-Agent": "Antigravity-Webhook-Hub/1.0",
        }
        if bot_token and "slack.com" in download_url:
            headers["Authorization"] = f"Bearer {bot_token}"

        req = urllib.request.Request(download_url, headers=headers, method="GET")

        downloaded = False
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                if data:
                    if data[:64].startswith((b"<!DOCTYPE", b"<html", b"<HTML", b'{"ok":false', b'{"ok": false', b"The requested file could not be found")):
                        logger.warning("urllib downloaded error/HTML content for %s", download_url)
                        dest_path.unlink(missing_ok=True)
                    else:
                        dest_path.write_bytes(data)
                        if dest_path.is_file() and dest_path.stat().st_size > 0:
                            downloaded = True
                            downloaded_paths.append(str(dest_path.resolve()))
                            count += 1
                            logger.info("Successfully downloaded image %s (%d bytes)", safe_name, len(data))
        except Exception as e:
            logger.warning("urllib download failed for %s (%s), attempting curl fallback", download_url, e)

        if not downloaded:
            dest_path.unlink(missing_ok=True)
            if download_file_with_curl(download_url, dest_path, bot_token=bot_token, timeout=30):
                downloaded_paths.append(str(dest_path.resolve()))
                count += 1
                logger.info("Successfully downloaded image %s via curl fallback (%d bytes)", safe_name, dest_path.stat().st_size)
            else:
                dest_path.unlink(missing_ok=True)
                logger.warning("Failed to download image from %s via both urllib and curl", download_url)

    return downloaded_paths
