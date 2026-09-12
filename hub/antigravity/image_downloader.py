"""
Antigravity Webhook Hub — Multi-Image Local Attachment Downloader
Downloads private Slack image files and web image assets locally into the data/attachments/
directory, giving local Antigravity agents direct filesystem access to all visual inputs.
"""

from __future__ import annotations

import logging
import os
import re
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
    cleaned = re.sub(r'[^a-zA-Z0-9._-]', '_', name)
    return cleaned[:60] if cleaned else "attachment.png"


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

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                if data:
                    dest_path.write_bytes(data)
                    downloaded_paths.append(str(dest_path.resolve()))
                    count += 1
                    logger.info("Successfully downloaded image %s (%d bytes)", safe_name, len(data))
        except Exception as e:
            logger.warning("Failed to download image from %s: %s", download_url, e)

    return downloaded_paths
