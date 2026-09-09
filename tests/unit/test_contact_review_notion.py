"""
Unit tests for Notion People Database Client.
Validates search candidate construction, scoring, SSOT verification,
and error resiliency against API failures.
"""

import pytest
from unittest.mock import MagicMock, patch
from hub.contact_review.models import ContactInput
from hub.contact_review.notion_client import NotionPeopleClient, clean_database_id


def test_clean_database_id():
    assert clean_database_id("22ce1b43-2393-81a4-9443-e32e71142e0d") == "22ce1b43239381a49443e32e71142e0d"
    assert clean_database_id("22ce1b43239381a49443e32e71142e0d") == "22ce1b43239381a49443e32e71142e0d"


@pytest.mark.asyncio
async def test_search_candidates_query_builder():
    client = NotionPeopleClient(api_token="test_secret_token", database_id="fake_db")
    contact = ContactInput(
        name="John Doe",
        phone="+84 901 234 567",
        email="john@example.com",
    )

    mock_response = {
        "results": [
            {
                "id": "page_match_1",
                "properties": {
                    "Full Name": {"type": "title", "title": [{"plain_text": "John Doe"}]},
                    "Email": {"type": "email", "email": "john@example.com"},
                    "Phone": {"type": "rich_text", "rich_text": [{"plain_text": "+84 901 234 567"}]},
                },
            }
        ]
    }

    with patch.object(client, "_request", return_value=mock_response) as mock_req:
        candidates = await client.search_candidates(contact)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.page_id == "page_match_1"
        assert c.page_name == "John Doe"
        assert c.score >= 100  # Email (100) + Phone (90) + Name (70)
        assert "exact_email_match" in c.match_reasons
        assert "phone_digits_match" in c.match_reasons


@pytest.mark.asyncio
async def test_ssot_verification_matching_and_mismatch():
    client = NotionPeopleClient(api_token="test_secret_token")

    live_page = {
        "id": "page_123",
        "properties": {
            "Full Name": {"type": "title", "title": [{"plain_text": "Alice Smith"}]},
            "Email": {"type": "email", "email": "alice@smith.org"},
            "Company": {"type": "rich_text", "rich_text": [{"plain_text": "Smith Consulting"}]},
            "Birthday": {"type": "date", "date": {"start": "1990-05-20"}},
            "URL": {"type": "url", "url": "https://linkedin.com/in/alice/"},
            "Status": {"type": "select", "select": {"name": "Active"}},
        },
    }

    with patch.object(client, "get_page", return_value=live_page):
        # Case 1: Exact expected matches -> Verified
        expected = {
            "Email": {"email": "alice@smith.org"},
            "Company": {"rich_text": [{"type": "text", "text": {"content": "Smith Consulting"}}]},
            "Birthday": {"date": {"start": "1990-05-20"}},
            "URL": {"url": "https://linkedin.com/in/alice"},  # Normalized URL matches trailing slash
            "Status": {"select": {"name": "Active"}},
        }
        verified, _ = await client.verify_page_properties("page_123", expected)
        assert verified is True

        # Case 2: Expected value differs -> Verification fails
        wrong_expected = {
            "Email": {"email": "wrong@different.com"},
        }
        failed, _ = await client.verify_page_properties("page_123", wrong_expected)
        assert failed is False

        # Case 3: Date mismatch -> Verification fails
        wrong_date = {
            "Birthday": {"date": {"start": "1995-01-01"}},
        }
        failed_date, _ = await client.verify_page_properties("page_123", wrong_date)
        assert failed_date is False


@pytest.mark.asyncio
async def test_search_candidates_romaji_name():
    client = NotionPeopleClient(api_token="test_secret_token", database_id="fake_db")
    contact = ContactInput(
        name="Taro Tanaka",
    )

    mock_response = {
        "results": [
            {
                "id": "page_romaji_1",
                "properties": {
                    "Full Name": {"type": "title", "title": [{"plain_text": "田中 太郎"}]},
                    "Romaji Name": {"type": "rich_text", "rich_text": [{"plain_text": "Taro Tanaka"}]},
                },
            }
        ]
    }

    with patch.object(client, "_request", return_value=mock_response) as mock_req:
        candidates = await client.search_candidates(contact)
        assert len(candidates) == 1
        c = candidates[0]
        assert c.page_id == "page_romaji_1"
        assert c.score >= 70
        assert "romaji_name_match" in c.match_reasons


@pytest.mark.asyncio
async def test_notion_client_missing_token_raises():
    client = NotionPeopleClient(api_token="")
    contact = ContactInput(name="Test")
    with pytest.raises((ValueError, RuntimeError)):
        await client.search_candidates(contact)
