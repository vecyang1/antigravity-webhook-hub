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


def format_social_url(platform: str, handle: str) -> str:
    """Ensure handle is formatted as a valid URL for Notion url property."""
    if not handle:
        return ""
    h = str(handle).strip()
    if not h:
        return ""
    if h.startswith("http://") or h.startswith("https://"):
        return h
    raw = h.lstrip("@").strip()
    p = platform.lower()
    if p == "telegram":
        return f"https://t.me/{raw}"
    if p == "wechat":
        return f"https://weixin.qq.com/{raw}"
    if p == "linkedin":
        return f"https://linkedin.com/in/{raw}"
    if p in ("twitter", "x"):
        return f"https://x.com/{raw}"
    if p == "line":
        return f"https://line.me/ti/p/{raw}"
    if p == "instagram":
        return f"https://instagram.com/{raw}"
    return f"https://{raw}"


PLACEHOLDER_NAMES: set[str] = {
    # Chinese pronouns, demonstratives, and placeholders
    "这个人", "这人", "那个人", "某人", "本人", "此人", "有人", "他人", "人家", "个人",
    "这个", "那个", "谁", "谁啊", "这谁啊", "这谁", "那谁", "这个人是谁", "这人是谁", "这人是谁啊", "这人是谁呢",
    "那个人是谁", "未知", "联系人", "朋友", "同学", "同事", "老师", "学生", "老板",
    "妹子", "美女", "帅哥", "男生", "女生", "小哥哥", "小姐姐", "老哥", "老弟", "大叔",
    "阿姨", "师傅", "对方", "用户", "客户", "成员", "候选人", "新人", "此用户", "该用户",
    "此人是谁", "不知名", "不知名人士", "无名氏", "无名", "匿名", "匿名人士", "未命名",
    "这位朋友", "那位朋友", "这个朋友", "那个朋友", "这朋友", "那朋友", "某朋友", "某位朋友",
    "这位同学", "那位同学", "这个同学", "那个同学", "这位同事", "那位同事", "这个同事", "那个同事",
    "这位老师", "那位老师", "这位先生", "那位先生", "这位女士", "那位女士", "这位小姐", "那位小姐",
    "这位", "那位", "某位", "哪位", "这哥们", "这哥们儿", "那哥们", "这家伙", "那家伙", "这小伙", "这小伙子", "这姑娘",
    # English placeholders
    "unknown person", "unknown", "unknown contact", "unnamed person", "unnamed contact", "anonymous",
    "no name", "unidentified", "user", "friend", "person", "contact", "someone", "somebody",
    "this person", "that person", "none", "n/a", "na", "null", "undefined",
}


def is_placeholder_name(name: Optional[str]) -> bool:
    """Check if name is empty or matches a generic placeholder/pronoun."""
    if not name:
        return True
    s = str(name).strip().lower()
    if not s:
        return True
    # Strip common surrounding quotes and question/exclamation marks
    cleaned = re.sub(r"^[\s\"'“”`*#~]+|[\s\"'“”`*#~?？!！.。;；,，]+$", "", s).strip()
    if not cleaned:
        return True
    normalized = re.sub(r"\s+", " ", cleaned)
    if normalized in PLACEHOLDER_NAMES:
        return True
    no_spaces = re.sub(r"\s+", "", cleaned)
    if no_spaces in PLACEHOLDER_NAMES:
        return True
    # Generic pronoun patterns: 这位, 那位, 某人, 这人是谁, etc.
    if re.match(r"^(这个|这|那个|那|某|本|此|他|她|它|谁)(人|位)?(是谁|是谁啊|是谁呢|朋友|同学|同事|先生|女士)?$", no_spaces):
        return True
    # Sentences starting with demonstratives: e.g. "这个人 她女朋友大学时候就跟他在一起了"
    if re.match(r"^(这个|这|那个|那|某|本|此)人?[\s,，.。!！]+", s):
        return True
    # Long text with sentence punctuation extracted by error
    if len(s) > 12 and re.search(r"[，。！？；\n]", s) and not re.search(r"^[A-Za-z\s'-]+$", s):
        return True
    return False


def extract_real_name_from_context(
    notes: str = "",
    source_text: str = "",
    image_text: str = "",
    social_handles: Optional[dict[str, str]] = None,
) -> str:
    """
    Attempt to extract a real person's name or social handle from notes, OCR text, or context
    when the primary name was missing or set to a placeholder pronoun.
    """
    candidates: list[str] = []

    for text in (notes, image_text, source_text):
        if not text:
            continue
        # Profile block matching: "- Name: Adam Walker" or "Romanized name: Adam Walker"
        m = re.search(
            r"(?:^|\n)\s*[-*]?\s*(?:Name|Full Name|Romanized(?:\s*/\s*alternate)?\s*name|Alternate name|English name|姓名|中文名|名字|英文名|外文名)\s*(?:is|为|是|:|=|：|\s*preserved from extraction:)\s*([^\n,;]+)",
            text,
            re.IGNORECASE,
        )
        if m:
            val = m.group(1).strip()
            if not is_placeholder_name(val):
                candidates.append(val)

        # Labeled patterns inside text
        m2 = re.search(
            r"(?:Romanized(?:\s*/\s*alternate)?\s*name|Alternate name|English name|姓名|中文名|名字|英文名)\s*(?:is|为|是|:|=|：|\s*preserved from extraction:)\s*([A-Za-z\s'-]{2,40}|[\u4e00-\u9fa5]{2,6})",
            text,
            re.IGNORECASE,
        )
        if m2:
            val = m2.group(1).strip()
            if not is_placeholder_name(val):
                candidates.append(val)

    if image_text:
        m3 = re.search(r"\bProfile:\s*(?:\n\s*[-*]?\s*Name:\s*([^\n]+))", image_text, re.IGNORECASE)
        if m3:
            val = m3.group(1).strip()
            if not is_placeholder_name(val):
                candidates.append(val)

    for c in candidates:
        cleaned = re.sub(r"[.。!！,，;；]+$", "", c).strip()
        if not is_placeholder_name(cleaned) and len(cleaned) >= 2:
            return cleaned

    # Fallback to social handle (e.g. @adamwalk or handle extracted from notes/url)
    if social_handles:
        for p in ("instagram", "twitter", "x", "telegram", "wechat", "linkedin", "line"):
            h = social_handles.get(p)
            if h and not is_placeholder_name(h):
                h_clean = h.strip()
                if not h_clean.startswith("@") and "/" not in h_clean:
                    h_clean = f"@{h_clean}"
                return h_clean

    # Check for handle in notes / source_text
    for text in (notes, image_text, source_text):
        if not text:
            continue
        m_handle = re.search(r"(?:^|\s)(@[A-Za-z0-9._-]{3,30})(?=[,\s.。!！;；]|$)", text)
        if m_handle:
            h_val = m_handle.group(1).strip()
            if not is_placeholder_name(h_val):
                return h_val

    return ""


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
    image_files: list[dict[str, Any]] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    slack_channel: str = ""
    slack_thread_ts: str = ""
    slack_user: str = ""

    def phone_digits(self) -> str:
        return normalize_phone_digits(self.phone)

    def normalized_email(self) -> str:
        return normalize_email_address(self.email)

    def normalized_url(self) -> str:
        return normalize_url_string(self.url)

    def get_social(self, key: str) -> str:
        """Get social handle by platform key (case-insensitive, x/twitter aliased)."""
        k = key.lower()
        if k in ("x", "twitter"):
            return self.social_handles.get("twitter") or self.social_handles.get("x") or ""
        return self.social_handles.get(k, "")

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
        # Also check nested social or social_handles sub-dictionaries
        sub_social = c_dict.get("social") or c_dict.get("social_handles") or data.get("social") or data.get("social_handles") or {}
        if not isinstance(sub_social, dict):
            sub_social = {}

        for handle_key in ("wechat", "telegram", "line", "linkedin", "instagram", "twitter", "x"):
            val = sub_social.get(handle_key) or c_dict.get(handle_key) or data.get(handle_key)
            if val:
                social[handle_key] = str(val).strip()
        if "x" in social and "twitter" not in social:
            social["twitter"] = social["x"]

        name_val = get_val("name", "full_name", "Full Name", default="Unknown Person")
        notes_val = get_val("notes", "note", "important_info", "Note")
        source_text_val = get_val("source_text", "text", "raw_text")
        image_text_val = get_val("image_text", "image_response", "ocr_text")

        # Supersede placeholder pronoun / generic name if real name found in context
        if is_placeholder_name(name_val):
            real_name = extract_real_name_from_context(
                notes=notes_val,
                source_text=source_text_val,
                image_text=image_text_val,
                social_handles=social,
            )
            if real_name:
                name_val = real_name

        # Parse image files and URLs
        raw_files = (
            data.get("image_files")
            or data.get("files")
            or c_dict.get("image_files")
            or c_dict.get("files")
            or []
        )
        if isinstance(raw_files, str):
            try:
                raw_files = json.loads(raw_files)
            except Exception:
                raw_files = []
        if not isinstance(raw_files, list):
            raw_files = [raw_files] if isinstance(raw_files, dict) else []
        image_files = [f for f in raw_files if isinstance(f, dict)]

        raw_urls = (
            data.get("image_urls")
            or data.get("images")
            or c_dict.get("image_urls")
            or c_dict.get("images")
            or []
        )
        if isinstance(raw_urls, str):
            try:
                raw_urls = json.loads(raw_urls)
            except Exception:
                raw_urls = [raw_urls]
        if not isinstance(raw_urls, list):
            raw_urls = [str(raw_urls)] if raw_urls else []
        image_urls = [str(u).strip() for u in raw_urls if u and str(u).strip().startswith(("http://", "https://"))]

        return cls(
            name=name_val,
            phone=get_val("phone", "phone_number", "Phone"),
            email=get_val("email", "Email"),
            company=get_val("company", "Company"),
            title=get_val("title", "role", "Title"),
            city=get_val("city", "City"),
            country=get_val("country", "Country"),
            birthday=get_val("birthday", "Birthday"),
            url=get_val("url", "URL", "link"),
            entity=get_val("entity", "relationship", "Entity"),
            notes=notes_val,
            source=get_val("source", "Source", default="Slack"),
            source_text=source_text_val,
            source_url=get_val("source_url"),
            image_text=image_text_val,
            social_handles=social,
            image_files=image_files,
            image_urls=image_urls,
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
            "image_files": self.image_files,
            "image_urls": self.image_urls,
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
        if ptype == "multi_select":
            items = prop.get("multi_select", [])
            return ", ".join(str(i.get("name", "")).strip() for i in items if i.get("name")).strip()
        if ptype == "status":
            st = prop.get("status")
            return str(st.get("name") or "").strip() if st else ""
        if ptype == "number":
            num = prop.get("number")
            return str(num) if num is not None else ""
        return ""

    def get_email(self) -> str:
        return self.get_property_plain_text("Email")

    def get_phone(self) -> str:
        return self.get_property_plain_text("Phone")

    def get_url(self) -> str:
        return self.get_property_plain_text("URL")

    def get_birthday(self) -> str:
        return self.get_property_plain_text("Birthday")

    def get_social(self, platform: str) -> str:
        """Get social URL/handle property from Notion page."""
        prop_map = {
            "telegram": "Telegram",
            "wechat": "WeChat",
            "linkedin": "LinkedIn",
            "twitter": "Twitter/X",
            "x": "Twitter/X",
            "line": "LINE",
            "instagram": "Instagram",
        }
        p_name = prop_map.get(platform.lower(), "")
        return self.get_property_plain_text(p_name) if p_name else ""

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
    appended_image_count: int = 0
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
            "appended_image_count": self.appended_image_count,
            "error": self.error,
        }
