"""
Unit tests for Slack Notifier and message formatting.
"""

from unittest.mock import MagicMock, patch
from hub.contact_review.models import (
    ContactInput,
    FieldDiff,
    FieldDiffAction,
    ReviewResult,
    ReviewVerdict,
)
from hub.contact_review.slack_notifier import SlackNotifier


def test_format_review_message_all_verdicts():
    notifier = SlackNotifier(token="mock_token", default_channel="C123")
    contact = ContactInput(name="Alice Wang", slack_channel="C123")

    for v in ReviewVerdict:
        res = ReviewResult(
            verdict=v,
            target_page_id="p1",
            target_page_url="https://notion.so/p1",
            target_name="Alice Wang",
            confidence_score=92,
            explanation="Test explanation",
            diffs=[
                FieldDiff(
                    field_name="phone",
                    action=FieldDiffAction.SUPPLEMENT if v != ReviewVerdict.CORRECT else FieldDiffAction.CORRECT,
                    old_value="+111" if v == ReviewVerdict.CORRECT else None,
                    new_value="+222",
                )
            ],
            ssot_verified=True,
        )
        msg = notifier.format_review_message(res, contact)
        assert v.value.upper() in msg
        assert "Alice Wang" in msg
        assert "https://notion.so/p1" in msg
        assert "SSOT Status" in msg


def test_send_notification_success():
    notifier = SlackNotifier(token="mock_token")
    contact = ContactInput(name="Bob", slack_channel="C999", slack_thread_ts="12345.6789")
    result = ReviewResult(
        verdict=ReviewVerdict.SUPPLEMENT,
        target_name="Bob",
        confidence_score=88,
    )

    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok": true}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        success = notifier.send_notification(result, contact)
        assert success is True


def test_send_notification_graceful_degradation():
    notifier = SlackNotifier(token="mock_token")
    contact = ContactInput(name="Bob")
    result = ReviewResult(verdict=ReviewVerdict.NO_CHANGE)

    # API returns ok: false
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"ok": false, "error": "channel_not_found"}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        success = notifier.send_notification(result, contact)
        assert success is False  # Does not crash!

    # Network exception
    with patch("urllib.request.urlopen", side_effect=Exception("Network down")):
        success = notifier.send_notification(result, contact)
        assert success is False  # Still does not crash!
