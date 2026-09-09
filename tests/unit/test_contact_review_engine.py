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
    properties=None,
    page_name=None,
):
    if properties is not None:
        props = properties
    else:
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
    p_name = page_name if page_name is not None else name
    return CandidateMatch(
        page_id=page_id,
        page_name=p_name,
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


def test_placeholder_title_superseded_by_real_name(engine):
    """
    Empirical test: When an existing Notion contact has a placeholder title like '这个人',
    and incoming contact provides a verified real name like 'Adam Walker',
    the engine MUST decide CORRECT and update Full Name, Romaji Name, and Romaji Source Name.
    """
    cand = make_candidate(
        page_id="page_adam",
        page_name="这个人",
        score=95,
        properties={
            "Full Name": {"type": "title", "title": [{"plain_text": "这个人"}]},
            "Romaji Name": {"type": "rich_text", "rich_text": [{"plain_text": "Adam Walker"}]},
            "Romaji Source Name": {"type": "rich_text", "rich_text": [{"plain_text": "这个人"}]},
            "URL": {"type": "url", "url": "https://www.instagram.com/adamwalk/"},
        },
    )

    contact = ContactInput(
        name="Adam Walker",
        url="https://www.instagram.com/adamwalk/",
    )

    result = engine.evaluate(contact, [cand])
    assert result.verdict == ReviewVerdict.CORRECT

    name_diff = next(d for d in result.diffs if d.field_name == "name")
    assert name_diff.action == FieldDiffAction.CORRECT
    assert name_diff.old_value == "这个人"
    assert name_diff.new_value == "Adam Walker"
    assert "Superseding placeholder" in name_diff.explanation

    props, blocks = engine.prepare_mutation(result, contact, candidate=cand)
    assert "Full Name" in props
    assert props["Full Name"]["title"][0]["text"]["content"] == "Adam Walker"
    assert "Romaji Source Name" in props
    assert props["Romaji Source Name"]["rich_text"][0]["text"]["content"] == "Adam Walker"
    assert "Romaji Name" in props
    assert props["Romaji Name"]["rich_text"][0]["text"]["content"] == "Adam Walker"


def test_placeholder_name_not_overwriting_real_name(engine):
    """
    Protection test: If incoming contact has a placeholder like '这个人'
    and existing candidate has a verified real name like 'Adam Walker',
    the engine must NOT degrade the real name to the placeholder.
    """
    cand = make_candidate(
        page_id="page_adam",
        page_name="Adam Walker",
        score=95,
        properties={
            "Full Name": {"type": "title", "title": [{"plain_text": "Adam Walker"}]},
        },
    )

    contact = ContactInput(
        name="这个人",
        url="https://www.instagram.com/adamwalk/",
    )

    result = engine.evaluate(contact, [cand])
    name_diff = next((d for d in result.diffs if d.field_name == "name"), None)
    if name_diff:
        assert name_diff.action == FieldDiffAction.NO_CHANGE


def test_merge_supersedes_placeholder_title(engine):
    """
    Empirical test: When MERGE occurs between multiple candidates,
    if the canonical record currently has a placeholder title like '这个人',
    and incoming contact or secondary record has a verified real name 'Adam Walker',
    the engine MUST decide MERGE with a CORRECT diff on name and update Full Name and Romaji Name.
    """
    cand_canonical = make_candidate(
        page_id="page_canon",
        page_name="这个人",
        score=140,
        properties={
            "Full Name": {"type": "title", "title": [{"plain_text": "这个人"}]},
            "URL": {"type": "url", "url": "https://www.instagram.com/adamwalk/"},
            "Company": {"type": "rich_text", "rich_text": [{"plain_text": "UNSW"}]},
        },
    )
    cand_secondary = make_candidate(
        page_id="page_secondary",
        page_name="Adam Walker",
        score=140,
        properties={
            "Full Name": {"type": "title", "title": [{"plain_text": "Adam Walker"}]},
            "URL": {"type": "url", "url": "https://www.instagram.com/adamwalk/"},
            "Phone": {"type": "rich_text", "rich_text": [{"plain_text": "+1 415 555 0199"}]},
        },
    )

    contact = ContactInput(
        name="Adam Walker",
        url="https://www.instagram.com/adamwalk/",
    )

    result = engine.evaluate(contact, [cand_canonical, cand_secondary])
    assert result.verdict == ReviewVerdict.MERGE
    assert result.target_name == "Adam Walker"

    name_diff = next((d for d in result.diffs if d.field_name == "name"), None)
    assert name_diff is not None
    assert name_diff.action == FieldDiffAction.CORRECT
    assert name_diff.new_value == "Adam Walker"

    props, blocks = engine.prepare_mutation(result, contact, candidate=cand_canonical)
    assert "Full Name" in props
    assert props["Full Name"]["title"][0]["text"]["content"] == "Adam Walker"
    assert "Romaji Name" in props
    assert props["Romaji Name"]["rich_text"][0]["text"]["content"] == "Adam Walker"
    assert "Romaji Source Name" in props
    assert props["Romaji Source Name"]["rich_text"][0]["text"]["content"] == "Adam Walker"
