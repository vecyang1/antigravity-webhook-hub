"""
Unit tests for Contact Review Engine decision logic and mutation builder.
Tests positive paths and edge cases for all 5 verdicts:
NO_CHANGE, SUPPLEMENT, CORRECT, MERGE, CREATE.
"""

import pytest
from hub.contact_review.engine import ContactReviewEngine
from hub.contact_review.models import (
    CandidateMatch,
    ContactInput,
    FieldDiffAction,
    ReviewVerdict,
)


@pytest.fixture
def engine():
    return ContactReviewEngine()


def make_candidate(
    page_id="page_1",
    name="Nguyen Van A",
    score=85,
    phone="+84 901 111 222",
    email="nguyen@example.com",
    company="Vietnam Software Ltd",
    city="Da Nang",
    country="Vietnam",
    birthday="1990-01-15",
    url="https://linkedin.com/in/nguyen",
    notes="Met at Da Nang conference",
):
    props = {
        "Full Name": {"type": "title", "title": [{"plain_text": name}]},
        "Phone": {"type": "rich_text", "rich_text": [{"plain_text": phone}]},
        "Email": {"type": "email", "email": email},
        "Company": {"type": "rich_text", "rich_text": [{"plain_text": company}]},
        "City": {"type": "rich_text", "rich_text": [{"plain_text": city}]},
        "Country": {"type": "rich_text", "rich_text": [{"plain_text": country}]},
        "Birthday": {"type": "date", "date": {"start": birthday} if birthday else None},
        "URL": {"type": "url", "url": url},
        "Note": {"type": "rich_text", "rich_text": [{"plain_text": notes}]},
    }
    return CandidateMatch(
        page_id=page_id,
        page_name=name,
        page_url=f"https://notion.so/{page_id}",
        score=score,
        match_reasons=["test_match"],
        properties=props,
    )


def test_verdict_create_when_no_candidates(engine):
    contact = ContactInput(
        name="Brand New Person",
        phone="+84 987 654 321",
        email="brandnew@company.com",
        company="Startup Co",
    )
    result = engine.evaluate(contact, candidates=[])
    assert result.verdict == ReviewVerdict.CREATE
    assert result.confidence_score == 100
    assert result.target_name == "Brand New Person"
    assert any(d.field_name == "name" for d in result.diffs)
    assert any(d.field_name == "phone" for d in result.diffs)

    # Mutation test
    props, blocks = engine.prepare_mutation(result, contact)
    assert "Full Name" in props
    assert "Phone" in props
    assert "Email" in props
    assert len(blocks) >= 2


def test_verdict_no_change_when_identical_info(engine):
    cand = make_candidate()
    contact = ContactInput(
        name="Nguyen Van A",
        phone="+84 (901) 111-222",  # Different formatting, same digits
        email="NGUYEN@EXAMPLE.COM",  # Different case, same email
        company="Vietnam Software Ltd",
    )
    result = engine.evaluate(contact, [cand])
    assert result.verdict == ReviewVerdict.NO_CHANGE
    assert result.target_page_id == "page_1"
    assert "already contains all provided information" in result.explanation

    # Mutation should be empty
    props, blocks = engine.prepare_mutation(result, contact)
    assert props == {}
    assert blocks == []


def test_verdict_supplement_when_missing_fields_provided(engine):
    # Candidate lacks birthday and url
    cand = make_candidate(birthday="", url="")
    contact = ContactInput(
        name="Nguyen Van A",
        phone="+84 901 111 222",
        birthday="1990-01-15",
        url="https://linkedin.com/in/nguyen",
    )
    result = engine.evaluate(contact, [cand])
    assert result.verdict == ReviewVerdict.SUPPLEMENT
    assert result.target_page_id == "page_1"

    supp_fields = [d.field_name for d in result.diffs if d.action == FieldDiffAction.SUPPLEMENT]
    assert "birthday" in supp_fields
    assert "url" in supp_fields

    props, blocks = engine.prepare_mutation(result, contact)
    assert "Birthday" in props
    assert props["Birthday"]["date"]["start"] == "1990-01-15"
    assert "URL" in props
    assert len(blocks) >= 1


def test_verdict_correct_when_existing_field_updated(engine):
    cand = make_candidate(company="Old Company Inc", phone="+84 111 222 333")
    contact = ContactInput(
        name="Nguyen Van A",
        company="New Enterprise Corp",  # Updated company!
        phone="+84 999 888 777",        # Updated phone!
    )
    result = engine.evaluate(contact, [cand])
    assert result.verdict == ReviewVerdict.CORRECT
    assert result.target_page_id == "page_1"

    correct_diffs = {d.field_name: d for d in result.diffs if d.action == FieldDiffAction.CORRECT}
    assert "company" in correct_diffs
    assert correct_diffs["company"].old_value == "Old Company Inc"
    assert correct_diffs["company"].new_value == "New Enterprise Corp"
    assert "phone" in correct_diffs

    props, blocks = engine.prepare_mutation(result, contact)
    assert "Company" in props
    assert "Phone" in props
    assert len(blocks) >= 1
    # Audit note should contain old value
    block_texts = [str(b) for b in blocks]
    assert any("Old Company Inc" in t for t in block_texts)


def test_verdict_merge_when_multiple_strong_candidates(engine):
    cand1 = make_candidate(page_id="page_1", score=90, phone="+84 901 111 222", email="")
    cand2 = make_candidate(page_id="page_2", score=85, phone="", email="nguyen@example.com")

    contact = ContactInput(
        name="Nguyen Van A",
        phone="+84 901 111 222",
        email="nguyen@example.com",
    )
    result = engine.evaluate(contact, [cand1, cand2])
    assert result.verdict == ReviewVerdict.MERGE
    assert result.target_page_id == "page_1"  # Canonical highest score
    assert len(result.candidates) == 2

    props, blocks = engine.prepare_mutation(result, contact, candidate=cand1)
    assert len(blocks) >= 1
    # Email was missing on cand1, so it should be populated in props
    assert "Email" in props
    assert props["Email"] == {"email": "nguyen@example.com"}
    # Phone was already on cand1, so it must NOT be overwritten or corrupted
    assert "Phone" not in props


def test_social_handles_diff_and_mutation(engine):
    cand = make_candidate(page_id="page_1", score=85)
    contact = ContactInput(
        name="Nguyen Van A",
        phone="+84 901 111 222",
        social_handles={
            "telegram": "nguyen_tg",
            "wechat": "nguyen_wx",
        },
    )
    result = engine.evaluate(contact, [cand])
    assert result.verdict == ReviewVerdict.SUPPLEMENT
    diff_fields = [d.field_name for d in result.diffs]
    assert "telegram" in diff_fields
    assert "wechat" in diff_fields

    props, blocks = engine.prepare_mutation(result, contact, candidate=cand)
    assert "Telegram" in props
    assert props["Telegram"]["url"] == "https://t.me/nguyen_tg"
    assert "WeChat" in props
    assert props["WeChat"]["url"] == "https://weixin.qq.com/nguyen_wx"
    assert len(blocks) >= 1
