"""
Unit tests for Contact Review data models and normalization utilities.
"""

import pytest
from hub.contact_review.models import (
    CandidateMatch,
    ContactInput,
    FieldDiff,
    FieldDiffAction,
    ReviewResult,
    ReviewVerdict,
    extract_real_name_from_context,
    is_placeholder_name,
    normalize_email_address,
    normalize_phone_digits,
    normalize_url_string,
)


def test_phone_normalization():
    assert normalize_phone_digits("+84 (901) 234-567") == "84901234567"
    assert normalize_phone_digits("090-123-4567") == "0901234567"
    assert normalize_phone_digits("") == ""
    assert normalize_phone_digits(None) == ""


def test_email_normalization():
    assert normalize_email_address("  User.Test@Example.COM  ") == "user.test@example.com"
    assert normalize_email_address("") == ""
    assert normalize_email_address(None) == ""


def test_url_normalization():
    assert normalize_url_string("https://instagram.com/johndoe/") == "instagram.com/johndoe"
    assert normalize_url_string("HTTP://WWW.EXAMPLE.COM/path/") == "www.example.com/path"
    assert normalize_url_string("example.com/about") == "example.com/about"
    assert normalize_url_string("") == ""


def test_format_social_url():
    from hub.contact_review.models import format_social_url
    assert format_social_url("telegram", "@johndoe") == "https://t.me/johndoe"
    assert format_social_url("telegram", "johndoe") == "https://t.me/johndoe"
    assert format_social_url("wechat", "wx_id_123") == "https://weixin.qq.com/wx_id_123"
    assert format_social_url("linkedin", "https://linkedin.com/in/custom") == "https://linkedin.com/in/custom"
    assert format_social_url("x", "elonofficial") == "https://x.com/elonofficial"
    assert format_social_url("twitter", "@jack") == "https://x.com/jack"
    assert format_social_url("line", "lineid123") == "https://line.me/ti/p/lineid123"
    assert format_social_url("instagram", "insta_user") == "https://instagram.com/insta_user"
    assert format_social_url("telegram", "") == ""


def test_contact_input_from_dict_flat():
    data = {
        "name": "Jane Smith",
        "phone": "+1 555 123 4567",
        "email": "jane@example.com",
        "company": "Acme Inc",
        "city": "Da Nang",
        "country": "Vietnam",
        "birthday": "1992-04-10",
        "url": "https://linkedin.com/in/janesmith",
        "notes": "Met at coffee shop",
        "channel": "C096KR96AF7",
        "thread_ts": "1725881234.5678",
    }
    c = ContactInput.from_dict(data)
    assert c.name == "Jane Smith"
    assert c.phone_digits() == "15551234567"
    assert c.normalized_email() == "jane@example.com"
    assert c.company == "Acme Inc"
    assert c.city == "Da Nang"
    assert c.country == "Vietnam"
    assert c.birthday == "1992-04-10"
    assert c.slack_channel == "C096KR96AF7"
    assert c.slack_thread_ts == "1725881234.5678"


def test_contact_input_from_dict_nested_n8n():
    data = {
        "source": "slack-intake",
        "contact": {
            "name": "David Miller",
            "phone_number": "+84 999 888 777",
            "email": "david@tech.io",
            "company": "Tech Innovations",
            "entity": "Partner",
            "instagram": "david_tech",
        },
        "slack": {
            "channel": "C096KR96AF7",
            "ts": "1725889999.0001",
            "user": "U12345",
        },
        "source_text": "David Miller's contact details: +84 999 888 777",
    }
    c = ContactInput.from_dict(data)
    assert c.name == "David Miller"
    assert c.phone == "+84 999 888 777"
    assert c.phone_digits() == "84999888777"
    assert c.email == "david@tech.io"
    assert c.company == "Tech Innovations"
    assert c.entity == "Partner"
    assert c.social_handles.get("instagram") == "david_tech"
    assert c.slack_channel == "C096KR96AF7"
    assert c.slack_thread_ts == "1725889999.0001"
    assert c.source_text == "David Miller's contact details: +84 999 888 777"


def test_candidate_match_properties():
    props = {
        "Full Name": {"type": "title", "title": [{"plain_text": "Alice Johnson"}]},
        "Phone": {"type": "rich_text", "rich_text": [{"plain_text": "+1 234 567 8900"}]},
        "Email": {"type": "email", "email": "alice@corp.com"},
        "Company": {"type": "rich_text", "rich_text": [{"plain_text": "Corp LLC"}]},
        "Birthday": {"type": "date", "date": {"start": "1988-12-01"}},
        "URL": {"type": "url", "url": "https://alice.dev"},
        "Telegram": {"type": "url", "url": "https://t.me/alice_tg"},
        "WeChat": {"type": "url", "url": "https://weixin.qq.com/alicewx"},
    }
    m = CandidateMatch(
        page_id="page_123",
        page_name="Alice Johnson",
        page_url="https://notion.so/page_123",
        score=95,
        match_reasons=["exact_email_match"],
        properties=props,
    )
    assert m.page_name == "Alice Johnson"
    assert m.get_email() == "alice@corp.com"
    assert m.get_phone() == "+1 234 567 8900"
    assert m.get_birthday() == "1988-12-01"
    assert m.get_url() == "https://alice.dev"
    assert m.get_social("telegram") == "https://t.me/alice_tg"
    assert m.get_social("wechat") == "https://weixin.qq.com/alicewx"
    assert m.get_property_plain_text("Company") == "Corp LLC"


def test_review_result_serialization():
    res = ReviewResult(
        verdict=ReviewVerdict.SUPPLEMENT,
        target_page_id="page_999",
        target_page_url="https://notion.so/page_999",
        target_name="Bob Brown",
        confidence_score=90,
        diffs=[
            FieldDiff(
                field_name="phone",
                action=FieldDiffAction.SUPPLEMENT,
                old_value=None,
                new_value="+84 901 000 111",
                explanation="Added missing phone",
            )
        ],
        explanation="Enriched contact with 1 missing attribute.",
        applied=True,
        ssot_verified=True,
        slack_notified=True,
    )
    d = res.to_dict()
    assert d["verdict"] == "supplement"
    assert d["target_page_id"] == "page_999"
    assert d["applied"] is True
    assert d["ssot_verified"] is True
    assert len(d["diffs"]) == 1
    assert d["diffs"][0]["action"] == "supplement"


def test_placeholder_name_detection():
    """Verify is_placeholder_name flags pronouns and generic placeholders."""
    assert is_placeholder_name("这个人") is True
    assert is_placeholder_name("这人") is True
    assert is_placeholder_name("那个人") is True
    assert is_placeholder_name("某人") is True
    assert is_placeholder_name("本人") is True
    assert is_placeholder_name("谁啊") is True
    assert is_placeholder_name("Unknown Person") is True
    assert is_placeholder_name("User") is True
    assert is_placeholder_name("朋友") is True
    assert is_placeholder_name("") is True
    assert is_placeholder_name(None) is True

    # Real names must NOT be flagged
    assert is_placeholder_name("Adam Walker") is False
    assert is_placeholder_name("胡国正") is False
    assert is_placeholder_name("Alice Johnson") is False


def test_extract_real_name_from_context():
    """Verify extracting real names from notes, OCR context, and source text."""
    # From OCR context
    ocr_text = """**Image 1 (Instagram Profile):**
**Profile:**
- Name: Adam Walker
- Handle: @adamwalk
- Bio: Adam Driver"""
    assert extract_real_name_from_context(image_text=ocr_text) == "Adam Walker"

    # From notes
    notes = "Handle: @adamwalk. Bio: Adam Driver. Romanized name: Adam Walker. UNSW."
    assert extract_real_name_from_context(notes=notes) == "Adam Walker"


def test_contact_input_supersedes_placeholder_with_real_name():
    """
    Empirical test: When incoming payload sets name to '这个人' (pronoun placeholder),
    but OCR context or notes contains 'Adam Walker',
    ContactInput.from_dict MUST supersede the placeholder with 'Adam Walker'.
    """
    payload = {
        "source": "slack_people",
        "contact": {
            "name": "这个人",
            "url": "https://www.instagram.com/adamwalk/",
            "note": "Handle: @adamwalk. Bio: Adam Driver. Romanized name: Adam Walker.",
        },
        "source_text": "这个人 她女朋友大学时候就跟他在一起了",
        "image_text": "**Profile:**\n- Name: Adam Walker\n- Handle: @adamwalk",
    }
    c = ContactInput.from_dict(payload)
    assert c.name == "Adam Walker"
    assert c.url == "https://www.instagram.com/adamwalk/"
    assert c.source == "slack_people"


def test_contact_input_multi_image_parsing():
    """Verify ContactInput.from_dict parses multiple image files and URLs."""
    payload = {
        "contact": {
            "name": "Adam Walker",
        },
        "image_files": [
            {"id": "F1", "name": "img1.png", "url_private_download": "https://files.slack.com/1"},
            {"id": "F2", "name": "img2.jpg", "url_private_download": "https://files.slack.com/2"},
        ],
        "image_urls": [
            "https://example.com/photo1.png",
            "https://example.com/photo2.png",
        ],
    }
    c = ContactInput.from_dict(payload)
    assert len(c.image_files) == 2
    assert c.image_files[0]["id"] == "F1"
    assert c.image_files[1]["id"] == "F2"
    assert len(c.image_urls) == 2
    assert c.image_urls[0] == "https://example.com/photo1.png"
