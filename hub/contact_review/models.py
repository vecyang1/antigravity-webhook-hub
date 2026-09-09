"""
Antigravity Webhook Hub — Contact Review Data Models
Strict dataclasses and enums for contact review, deduplication, reconciliation,
and SSOT verification against Notion People database.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ReviewVerdict(str, Enum):
    """Reconciliation action decided by the review engine."""

    NO_CHANGE = "no_change"
    SUPPLEMENT = "supplement"
    CORRECT = "correct"
    MERGE = "merge"
    CREATE = "create"


class FieldDiffAction(str, Enum):
    """Field-level diff determination."""

    NO_CHANGE = "no_change"
    SUPPLEMENT = "supplement"
    CORRECT = "correct"
    CONFLICT = "conflict"


def normalize_phone_digits(val: Optional[str]) -> str:
    """Extract numeric digits from phone string, stripping formatting."""
    if not val:
        return ""
    return re.sub(r"\D", "", str(val))


def normalize_email_address(val: Optional[str]) -> str:
    """Normalize email address to trimmed lowercase."""
    if not val:
        return ""
    return str(val).strip().lower()


def normalize_url_string(val: Optional[str]) -> str:
    """Normalize URL by stripping protocol, trailing slashes, and lowercase domain."""
    if not val:
        return ""
    val = str(val).strip()
    if not val:
        return ""
    if not re.match(r"^https?://", val, re.IGNORECASE):
        parsed = urllib.parse.urlparse("https://" + val)
    else:
        parsed = urllib.parse.urlparse(val)
    netloc = parsed.netloc.lower().rstrip("/")
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{netloc}{path}{query}"


@dataclass(slots=True)
class FieldDiff:
    """Represents a diff on a single contact property."""

    field_name: str
    action: FieldDiffAction
    old_value: Any = None
    new_value: Any = None
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "action": self.action.value,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "explanation": self.explanation,
        }


@dataclass(slots=True)
class ContactInput:
    """Normalized input payload for contact review."""

    name: str
    phone: str = ""
    email: str = ""
    company: str = ""
    title: str = ""
    city: str = ""
    country: str = ""
    birthday: str = ""
    url: str = ""
    entity: str = ""
    notes: str = ""
    source: str = "Slack"
    source_text: str = ""
    source_url: str = ""
    image_text: str = ""
    social_handles: dict[str, str] = field(default_factory=dict)
    slack_channel: str = ""
    slack_thread_ts: str = ""
    slack_user: str = ""

    def phone_digits(self) -> str:
        return normalize_phone_digits(self.phone)

    def normalized_email(self) -> str:
        return normalize_email_address(self.email)

    def normalized_url(self) -> str:
        return normalize_url_string(self.url)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContactInput:
        """Robust parser extracting contact fields from varied payload shapes."""
        if not isinstance(data, dict):
            return cls(name="Unknown Person")

        c_dict = data.get("contact") or data.get("output") or data.get("person") or {}
        if not isinstance(c_dict, dict):
            c_dict = {}

        def get_val(*keys: str, default: str = "") -> str:
            for k in keys:
                if k in c_dict and c_dict[k] is not None:
                    val = str(c_dict[k]).strip()
                    if val:
                        return val
                if k in data and data[k] is not None:
                    val = str(data[k]).strip()
                    if val:
                        return val
            return default

        s_dict = data.get("slack") or {}
        if not isinstance(s_dict, dict):
            s_dict = {}

        slack_channel = str(
            s_dict.get("channel")
            or data.get("channel")
            or data.get("channel_id")
            or data.get("channelId")
            or data.get("slack_channel")
            or ""
        ).strip()
        slack_thread_ts = str(
            s_dict.get("thread_ts")
            or s_dict.get("ts")
            or data.get("thread_ts")
            or data.get("ts")
            or data.get("slack_thread_ts")
            or ""
        ).strip()
        slack_user = str(
            s_dict.get("user") or data.get("user") or data.get("slack_user") or ""
        ).strip()

        social = {}
        for handle_key in ("wechat", "telegram", "line", "linkedin", "instagram", "twitter"):
            val = c_dict.get(handle_key) or data.get(handle_key)
            if val:
                social[handle_key] = str(val).strip()

        return cls(
            name=get_val("name", "full_name", "Full Name", default="Unknown Person"),
            phone=get_val("phone", "phone_number", "Phone"),
            email=get_val("email", "Email"),
            company=get_val("company", "Company"),
            title=get_val("title", "role", "Title"),
            city=get_val("city", "City"),
            country=get_val("country", "Country"),
            birthday=get_val("birthday", "Birthday"),
            url=get_val("url", "URL", "link"),
            entity=get_val("entity", "relationship", "Entity"),
            notes=get_val("notes", "note", "important_info", "Note"),
            source=get_val("source", "Source", default="Slack"),
            source_text=get_val("source_text", "text", "raw_text"),
            source_url=get_val("source_url"),
            image_text=get_val("image_text", "image_response", "ocr_text"),
            social_handles=social,
            slack_channel=slack_channel,
            slack_thread_ts=slack_thread_ts,
            slack_user=slack_user,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "phone": self.phone,
            "email": self.email,
            "company": self.company,
            "title": self.title,
            "city": self.city,
            "country": self.country,
            "birthday": self.birthday,
            "url": self.url,
            "entity": self.entity,
            "notes": self.notes,
            "source": self.source,
            "source_text": self.source_text,
            "source_url": self.source_url,
            "image_text": self.image_text,
            "social_handles": self.social_handles,
            "slack_channel": self.slack_channel,
            "slack_thread_ts": self.slack_thread_ts,
            "slack_user": self.slack_user,
        }


@dataclass(slots=True)
class CandidateMatch:
    """An existing contact page found in Notion People database."""

    page_id: str
    page_name: str
    page_url: str
    score: int
    match_reasons: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)

    def get_property_plain_text(self, prop_name: str) -> str:
        """Extract plain text from rich_text or title Notion property."""
        prop = self.properties.get(prop_name, {})
        ptype = prop.get("type")
        if ptype in ("rich_text", "title"):
            items = prop.get(ptype, [])
            return "".join(i.get("plain_text", "") for i in items).strip()
        if ptype == "email":
            return str(prop.get("email") or "").strip()
        if ptype == "url":
            return str(prop.get("url") or "").strip()
        if ptype == "phone_number":
            return str(prop.get("phone_number") or "").strip()
        if ptype == "date":
            d = prop.get("date")
            return str(d.get("start") or "").strip() if d else ""
        if ptype == "select":
            s = prop.get("select")
            return str(s.get("name") or "").strip() if s else ""
        return ""

    def get_email(self) -> str:
        return self.get_property_plain_text("Email")

    def get_phone(self) -> str:
        return self.get_property_plain_text("Phone")

    def get_url(self) -> str:
        return self.get_property_plain_text("URL")

    def get_birthday(self) -> str:
        return self.get_property_plain_text("Birthday")

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "page_name": self.page_name,
            "page_url": self.page_url,
            "score": self.score,
            "match_reasons": self.match_reasons,
        }


@dataclass(slots=True)
class ReviewResult:
    """Complete result of contact review & reconciliation."""

    verdict: ReviewVerdict
    target_page_id: Optional[str] = None
    target_page_url: Optional[str] = None
    target_name: str = ""
    confidence_score: int = 0
    diffs: list[FieldDiff] = field(default_factory=list)
    candidates: list[CandidateMatch] = field(default_factory=list)
    explanation: str = ""
    applied: bool = False
    ssot_verified: bool = False
    slack_notified: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "target_page_id": self.target_page_id,
            "target_page_url": self.target_page_url,
            "target_name": self.target_name,
            "confidence_score": self.confidence_score,
            "diffs": [d.to_dict() for d in self.diffs],
            "candidates": [c.to_dict() for c in self.candidates],
            "explanation": self.explanation,
            "applied": self.applied,
            "ssot_verified": self.ssot_verified,
            "slack_notified": self.slack_notified,
            "error": self.error,
        }
