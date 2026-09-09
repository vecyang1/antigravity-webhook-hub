"""
Antigravity Webhook Hub — Notion People Database Client
Contract-first REST API client for Notion People database (SSOT).
Implements candidate querying, property patching, block appending,
and post-write verification against live Notion state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from hub.contact_review.models import (
    CandidateMatch,
    ContactInput,
    is_placeholder_name,
    normalize_phone_digits,
    normalize_url_string,
)

logger = logging.getLogger("hub.contact_review.notion")

DEFAULT_PEOPLE_DATABASE_ID = "22ce1b43-2393-81a4-9443-e32e71142e0d"
NOTION_API_VERSION = "2022-06-28"
NOTION_BASE_URL = "https://api.notion.com/v1"


def resolve_notion_token() -> str:
    """
    Resolve Notion API token from environment, local .env, or notion-mcp-connector.
    Fails closed if no token can be located.
    """
    # 1. Direct environment variable
    token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if token:
        return token.strip()

    # 2. Local .env file
    local_env_path = Path(".env")
    if local_env_path.is_file():
        for line in local_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k in ("NOTION_TOKEN", "NOTION_API_KEY"):
                    val = v.strip().strip("'\"")
                    if val:
                        return val

    # 3. notion-mcp-connector skill .env
    skill_env_path = Path("/Users/vecsatfoxmailcom/.gemini/antigravity/skills/notion-mcp-connector/.env")
    if skill_env_path.is_file():
        for line in skill_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k in ("NOTION_TOKEN", "NOTION_API_KEY"):
                    val = v.strip().strip("'\"")
                    if val:
                        return val

    return ""


def clean_database_id(raw_id: str) -> str:
    """Normalize Notion database ID by removing dashes if needed."""
    clean = raw_id.replace("-", "").strip()
    return clean


# Notion Property Helpers
def make_title(text: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": str(text or "").strip()[:1900]}}]}


def make_rich_text(text: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": str(text or "").strip()[:1900]}}]}


def make_email(email: str) -> dict[str, Any]:
    return {"email": str(email or "").strip() or None}


def make_url(url: str) -> dict[str, Any]:
    return {"url": str(url or "").strip() or None}


def make_date(date_str: str) -> dict[str, Any]:
    d = str(date_str or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return {"date": {"start": d}}
    return {"date": None}


def make_select(name: str) -> dict[str, Any]:
    return {"select": {"name": str(name).strip()[:100]} if name else None}


# Notion Block Helpers
def make_heading_block(text: str, level: int = 3) -> dict[str, Any]:
    htype = f"heading_{level}"
    return {
        "type": htype,
        htype: {
            "rich_text": [{"type": "text", "text": {"content": str(text)[:1900]}}]
        },
    }


def make_bullet_block(text: str) -> dict[str, Any]:
    return {
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": str(text)[:1900]}}]
        },
    }


def make_paragraph_block(text: str) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": str(text)[:1900]}}]
        },
    }


def make_external_image_block(url: str, caption: str = "") -> dict[str, Any]:
    b: dict[str, Any] = {
        "type": "image",
        "image": {
            "type": "external",
            "external": {"url": str(url).strip()},
        },
    }
    if caption:
        b["image"]["caption"] = [{"type": "text", "text": {"content": str(caption)[:1900]}}]
    return b


def make_file_upload_image_block(upload_id: str, caption: str = "") -> dict[str, Any]:
    b: dict[str, Any] = {
        "type": "image",
        "image": {
            "type": "file_upload",
            "file_upload": {"id": str(upload_id).strip()},
        },
    }
    if caption:
        b["image"]["caption"] = [{"type": "text", "text": {"content": str(caption)[:1900]}}]
    return b


class NotionPeopleClient:
    """Client for interacting with Notion People database."""

    def __init__(
        self,
        api_token: Optional[str] = None,
        database_id: Optional[str] = None,
        timeout: int = 20,
    ):
        self.api_token = api_token if api_token is not None else resolve_notion_token()
        self.database_id = clean_database_id(
            database_id or os.environ.get("NOTION_PEOPLE_DATABASE_ID") or DEFAULT_PEOPLE_DATABASE_ID
        )
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict[str, Any]] = None,
        api_version: Optional[str] = None,
    ) -> dict[str, Any]:
        """Execute synchronous HTTP request to Notion API."""
        if not self.api_token:
            raise ValueError(
                "Notion API token not found. Please set NOTION_TOKEN or NOTION_API_KEY."
            )

        url = f"{NOTION_BASE_URL}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Notion-Version": api_version or NOTION_API_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "Antigravity-Webhook-Hub/1.0",
        }

        body_bytes = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return json.loads(resp_bytes.decode("utf-8")) if resp_bytes else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            try:
                err_json = json.loads(err_body)
                err_msg = err_json.get("message") or err_body
            except Exception:
                err_msg = err_body
            logger.error("Notion API error %d on %s %s: %s", e.code, method, path, err_msg)
            raise RuntimeError(f"Notion API {e.code} error: {err_msg}") from e
        except Exception as e:
            logger.error("Notion connection error on %s %s: %s", method, path, e)
            raise RuntimeError(f"Notion network error: {e}") from e

    async def search_candidates(self, contact: ContactInput) -> list[CandidateMatch]:
        """
        Query Notion database for candidates matching name, phone, email, or URL.
        Runs asynchronously offloading HTTP request to thread pool.
        """
        filter_clauses: list[dict[str, Any]] = []

        # 1. Email exact match
        if contact.email:
            filter_clauses.append({"property": "Email", "email": {"equals": contact.email.strip()}})

        # 2. Phone match (exact and digits)
        if contact.phone:
            filter_clauses.append({"property": "Phone", "rich_text": {"contains": contact.phone.strip()}})
            digits = contact.phone_digits()
            if digits and len(digits) >= 7 and digits != contact.phone.strip():
                filter_clauses.append({"property": "Phone", "rich_text": {"contains": digits[-8:]}})

        # 3. Full Name & Romaji Name match
        if contact.name and contact.name not in ("Unknown Person", ""):
            c_name = contact.name.strip()
            filter_clauses.append({"property": "Full Name", "title": {"contains": c_name}})
            filter_clauses.append({"property": "Romaji Name", "rich_text": {"contains": c_name}})
            # Also handle reverse name order (e.g. "Taro Tanaka" vs "Tanaka Taro" or "Walker, Adam")
            cleaned_parts = [p.strip().rstrip(",") for p in c_name.replace(",", " ").split() if p.strip()]
            if len(cleaned_parts) == 2:
                reversed_name = f"{cleaned_parts[1]} {cleaned_parts[0]}"
                filter_clauses.append({"property": "Full Name", "title": {"contains": reversed_name}})
                filter_clauses.append({"property": "Romaji Name", "rich_text": {"contains": reversed_name}})

        # 4. URL match
        if contact.url:
            filter_clauses.append({"property": "URL", "url": {"contains": contact.url.strip()}})

        # 5. Social Handle match
        if contact.social_handles:
            for platform, handle in contact.social_handles.items():
                if not handle:
                    continue
                raw_handle = handle.lstrip("@").strip()
                if not raw_handle:
                    continue
                p_lower = platform.lower()
                prop_name = {
                    "telegram": "Telegram",
                    "wechat": "WeChat",
                    "linkedin": "LinkedIn",
                    "twitter": "Twitter/X",
                    "x": "Twitter/X",
                    "line": "LINE",
                    "instagram": "Instagram",
                }.get(p_lower)
                if prop_name:
                    filter_clauses.append({"property": prop_name, "url": {"contains": raw_handle}})

        # If no search criteria available, return empty
        if not filter_clauses:
            return []

        query_payload: dict[str, Any] = {
            "page_size": 25,
            "filter": {"or": filter_clauses} if len(filter_clauses) > 1 else filter_clauses[0],
        }

        loop = asyncio.get_running_loop()
        path = f"databases/{self.database_id}/query"
        data = await loop.run_in_executor(None, self._request, "POST", path, query_payload)

        raw_results = data.get("results", [])
        candidates: list[CandidateMatch] = []

        for p in raw_results:
            page_id = p.get("id")
            props = p.get("properties", {})
            page_url = p.get("url") or f"https://www.notion.so/{self.database_id}?p={page_id.replace('-', '')}"
            name_parts = props.get("Full Name", {}).get("title", [])
            page_name = "".join(t.get("plain_text", "") for t in name_parts).strip() or "Unknown Person"

            # Compute preliminary score and match reasons
            score = 0
            reasons = []

            cand_match = CandidateMatch(
                page_id=page_id,
                page_name=page_name,
                page_url=page_url,
                score=0,
                match_reasons=[],
                properties=props,
            )

            # Email scoring
            cand_email = cand_match.get_email()
            if contact.email and cand_email:
                if contact.normalized_email() == cand_email.strip().lower():
                    score += 100
                    reasons.append("exact_email_match")

            # Phone scoring
            cand_phone = cand_match.get_phone()
            if contact.phone and cand_phone:
                c_digits = normalize_phone_digits(cand_phone)
                i_digits = contact.phone_digits()
                if i_digits and c_digits and (i_digits == c_digits or i_digits.endswith(c_digits[-8:]) or c_digits.endswith(i_digits[-8:])):
                    score += 90
                    reasons.append("phone_digits_match")

            # Social Handle scoring
            if contact.social_handles:
                for platform, handle in contact.social_handles.items():
                    if not handle:
                        continue
                    clean_h = handle.lstrip("@").strip().lower()
                    cand_h = cand_match.get_social(platform).strip().lower()
                    if cand_h and clean_h in cand_h:
                        score += 85
                        reasons.append(f"{platform.lower()}_handle_match")

            # URL scoring
            cand_url = cand_match.get_url()
            if contact.url and cand_url:
                if contact.normalized_url() == cand_url.strip().lower():
                    score += 85
                    reasons.append("url_match")

            # Name scoring (ignore if either is a generic placeholder)
            if contact.name and page_name and not is_placeholder_name(contact.name) and not is_placeholder_name(page_name):
                c_name_clean = page_name.strip().lower()
                i_name_clean = contact.name.strip().lower()
                if i_name_clean == c_name_clean:
                    score += 70
                    reasons.append("exact_name_match")
                elif i_name_clean in c_name_clean or c_name_clean in i_name_clean:
                    score += 45
                    reasons.append("substring_name_match")
                else:
                    i_parts = [p.strip().rstrip(",") for p in i_name_clean.replace(",", " ").split() if p.strip()]
                    c_parts = [p.strip().rstrip(",") for p in c_name_clean.replace(",", " ").split() if p.strip()]
                    if sorted(i_parts) == sorted(c_parts) and len(i_parts) > 1:
                        score += 65
                        reasons.append("reordered_name_match")

            # Romaji Name scoring
            cand_romaji = cand_match.get_property_plain_text("Romaji Name")
            if cand_romaji and contact.name and not is_placeholder_name(contact.name):
                cr_clean = cand_romaji.strip().lower()
                i_clean = contact.name.strip().lower()
                if i_clean == cr_clean:
                    score += 70
                    reasons.append("romaji_name_match")
                    if is_placeholder_name(page_name):
                        score += 30
                        reasons.append("supersedes_placeholder_title")
                elif i_clean in cr_clean or cr_clean in i_clean:
                    score += 45
                    reasons.append("romaji_substring_match")
                else:
                    i_parts = [p.strip().rstrip(",") for p in i_clean.replace(",", " ").split() if p.strip()]
                    cr_parts = [p.strip().rstrip(",") for p in cr_clean.replace(",", " ").split() if p.strip()]
                    if sorted(i_parts) == sorted(cr_parts) and len(i_parts) > 1:
                        score += 65
                        reasons.append("romaji_reordered_name_match")

            # Company match
            cand_company = cand_match.get_property_plain_text("Company")
            if contact.company and cand_company:
                if contact.company.strip().lower() == cand_company.strip().lower():
                    score += 20
                    reasons.append("company_match")

            # City match
            cand_city = cand_match.get_property_plain_text("City")
            if contact.city and cand_city:
                if contact.city.strip().lower() == cand_city.strip().lower():
                    score += 10
                    reasons.append("city_match")

            cand_match.score = score
            cand_match.match_reasons = reasons
            candidates.append(cand_match)

        # Sort candidates descending by score
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """Fetch live page state from Notion (SSOT read)."""
        loop = asyncio.get_running_loop()
        path = f"pages/{page_id}"
        return await loop.run_in_executor(None, self._request, "GET", path, None)

    async def update_page_properties(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        """Update properties on an existing Notion page."""
        loop = asyncio.get_running_loop()
        path = f"pages/{page_id}"
        return await loop.run_in_executor(
            None, self._request, "PATCH", path, {"properties": properties}
        )

    async def create_page(
        self,
        properties: dict[str, Any],
        children: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Create a new page in the Notion People database."""
        loop = asyncio.get_running_loop()
        payload: dict[str, Any] = {
            "parent": {"database_id": self.database_id},
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return await loop.run_in_executor(None, self._request, "POST", "pages", payload)

    async def append_page_blocks(
        self,
        page_id: str,
        blocks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Append blocks (notes, capture record, audit log) to page children."""
        if not blocks:
            return {}
        loop = asyncio.get_running_loop()
        path = f"blocks/{page_id}/children"
        return await loop.run_in_executor(
            None, self._request, "PATCH", path, {"children": blocks}
        )

    async def create_file_upload(self, filename: str, content_type: str = "image/png") -> dict[str, Any]:
        """Create a single_part file_upload reservation with Notion API."""
        loop = asyncio.get_running_loop()
        payload = {"mode": "single_part", "filename": filename, "content_type": content_type}
        return await loop.run_in_executor(
            None,
            lambda: self._request("POST", "file_uploads", payload, api_version="2026-03-11"),
        )

    async def send_file_binary(
        self, upload_id: str, filename: str, file_bytes: bytes, content_type: str = "image/png"
    ) -> bool:
        """Send the file binary multipart/form-data to Notion."""
        loop = asyncio.get_running_loop()

        def _do_send() -> bool:
            boundary = f"----NotionBoundary{uuid.uuid4().hex}"
            body = bytearray()
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(file_bytes)
            body.extend(b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode("utf-8"))

            url = f"{NOTION_BASE_URL}/file_uploads/{upload_id}/send"
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Notion-Version": "2026-03-11",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status in (200, 201, 204)

        return await loop.run_in_executor(None, _do_send)

    async def download_slack_file(
        self, download_url: str, slack_token: Optional[str] = None
    ) -> tuple[bytes, str]:
        """Download private file from Slack using Slack token."""
        token = (
            slack_token
            or os.environ.get("SLACK_USER_TOKEN")
            or os.environ.get("SLACK_BOT_TOKEN")
            or "xoxp-REDACTED-REVOKED-TOKEN"
        )
        loop = asyncio.get_running_loop()

        def _dl() -> tuple[bytes, str]:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            req = urllib.request.Request(download_url, headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get_content_type() or "image/png"
                return resp.read(), ctype

        return await loop.run_in_executor(None, _dl)

    async def append_images_to_page(
        self,
        page_id: str,
        image_urls: Optional[list[str]] = None,
        image_files: Optional[list[dict[str, Any]]] = None,
        slack_token: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Append image blocks to page children.
        Supports both external URLs and Slack private files (downloaded and uploaded to Notion).
        """
        blocks: list[dict[str, Any]] = []

        # 1. External URLs
        for url in (image_urls or []):
            if url and str(url).startswith(("http://", "https://")):
                blocks.append(make_external_image_block(url, caption="Attached Image"))

        # 2. Image files (e.g. from Slack)
        for f in (image_files or []):
            dl_url = f.get("url_private_download") or f.get("url_private") or f.get("url")
            fname = f.get("name") or f.get("title") or "image.png"
            if dl_url:
                try:
                    fbytes, ctype = await self.download_slack_file(dl_url, slack_token=slack_token)
                    upload_res = await self.create_file_upload(fname, content_type=ctype)
                    upload_id = upload_res.get("id")
                    if upload_id:
                        sent = await self.send_file_binary(upload_id, fname, fbytes, content_type=ctype)
                        if sent:
                            caption = f.get("caption") or f"Slack Image: {fname}"
                            blocks.append(make_file_upload_image_block(upload_id, caption=caption))
                except Exception as dl_err:
                    logger.warning("Failed to download and upload image file %s: %s", fname, dl_err)

        if blocks:
            await self.append_page_blocks(page_id, blocks)
        return blocks

    async def verify_page_properties(
        self,
        page_id: str,
        expected_properties: dict[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """
        SSOT Verification: Read page state back from Notion and confirm that
        all patched properties match expected values.
        """
        live_page = await self.get_page(page_id)
        live_props = live_page.get("properties", {})

        for prop_name, expected_obj in expected_properties.items():
            if prop_name not in live_props:
                logger.warning("SSOT Verification failed: %s not in live page", prop_name)
                return False, live_props

            live_obj = live_props[prop_name]
            ptype = live_obj.get("type")

            # Check title / rich_text
            if ptype in ("title", "rich_text") and ptype in expected_obj:
                exp_text = "".join(t.get("text", {}).get("content", "") for t in expected_obj[ptype]).strip()
                live_text = "".join(t.get("plain_text", "") for t in live_obj.get(ptype, [])).strip()
                if exp_text and exp_text not in live_text and live_text != exp_text:
                    logger.warning("SSOT mismatch on %s: expected '%s', got '%s'", prop_name, exp_text, live_text)
                    return False, live_props

            # Check email
            elif ptype == "email" and "email" in expected_obj:
                exp_val = str(expected_obj["email"] or "").strip().lower()
                live_val = str(live_obj.get("email") or "").strip().lower()
                if exp_val and exp_val != live_val:
                    logger.warning("SSOT mismatch on %s: expected email '%s', got '%s'", prop_name, exp_val, live_val)
                    return False, live_props

            # Check url
            elif ptype == "url" and "url" in expected_obj:
                exp_val = normalize_url_string(expected_obj.get("url"))
                live_val = normalize_url_string(live_obj.get("url"))
                if exp_val and exp_val != live_val:
                    logger.warning("SSOT mismatch on %s: expected url '%s', got '%s'", prop_name, exp_val, live_val)
                    return False, live_props

            # Check date
            elif ptype == "date" and "date" in expected_obj:
                exp_d = (expected_obj.get("date") or {}).get("start")
                live_d = (live_obj.get("date") or {}).get("start")
                if exp_d and exp_d != live_d:
                    logger.warning("SSOT mismatch on %s: expected date '%s', got '%s'", prop_name, exp_d, live_d)
                    return False, live_props

            # Check select
            elif ptype == "select" and "select" in expected_obj:
                exp_s = (expected_obj.get("select") or {}).get("name")
                live_s = (live_obj.get("select") or {}).get("name")
                if exp_s and exp_s != live_s:
                    logger.warning("SSOT mismatch on %s: expected select '%s', got '%s'", prop_name, exp_s, live_s)
                    return False, live_props

        return True, live_props
