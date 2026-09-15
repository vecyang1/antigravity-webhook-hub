"""
Unit Tests for Native Slack Reconciler, Contract Normalization, and Anti-Spam SSOT
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from hub.antigravity.models import AntigravityTaskPayload
from hub.antigravity.prompt_builder import build_antigravity_prompt
from hub.antigravity.reconciler import (
    SlackReconciler,
    is_delivery_completion,
    is_offline_notice,
    is_reconcile_notice,
)
from hub.db import DatabaseManager


class TestSlackReconcilerAndContractNormalization(unittest.TestCase):
    """Test suite for offline reconciliation, data contract normalization, and idempotency."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_hub.db")
        self.db = DatabaseManager(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_nested_contract_normalization(self):
        """Test that nested data dictionary from external/legacy scripts is properly unwrapped."""
        nested_payload = {
            "action": "antigravity.run",
            "source": "slack_reconcile",
            "data": {
                "channel_id": "C0C1B86AMCN",
                "event_ts": "1789444532.016789",
                "thread_ts": "1789444532.016789",
                "user_id": "U05C95HQDV0",
                "text": "",
                "image_urls": ["https://files.slack.com/files-pri/T05C2FLHA2J-F0C1XDQHFTK/img_4072.png?t=xoxe"],
            },
        }

        payload = AntigravityTaskPayload.from_dict(nested_payload)

        self.assertEqual(payload.channel, "C0C1B86AMCN")
        self.assertEqual(payload.ts, "1789444532.016789")
        self.assertEqual(payload.thread_ts, "1789444532.016789")
        self.assertEqual(payload.user, "U05C95HQDV0")
        self.assertEqual(payload.source, "slack_reconcile")
        self.assertEqual(payload.text, "")
        self.assertEqual(len(payload.files), 1)
        self.assertEqual(
            payload.files[0]["url_private"],
            "https://files.slack.com/files-pri/T05C2FLHA2J-F0C1XDQHFTK/img_4072.png?t=xoxe",
        )
        self.assertEqual(payload.files[0]["name"], "img_4072.png")

    def test_multimodal_pure_image_prompt_synthesis(self):
        """Test that pure image uploads synthesize visual analysis instructions instead of falling back to empty/CLI."""
        payload = AntigravityTaskPayload(
            text="",
            files=[{"url_private": "https://example.com/error.png", "name": "error.png"}],
            channel="C0C1B86AMCN",
            ts="1789444532.016789",
        )

        prompt = build_antigravity_prompt(payload)
        self.assertIn("请仔细审查随附的图片与报错截图", prompt)
        self.assertNotIn("agentapi new-conversation", prompt)

    def test_disallow_command_leak_into_prompt(self):
        """Test that internal CLI command strings ('agentapi new-conversation') never pollute user prompt."""
        payload = AntigravityTaskPayload(
            text="agentapi new-conversation",
            files=[{"url_private": "https://example.com/screenshot.png", "name": "screenshot.png"}],
            channel="C0C1B86AMCN",
            ts="1789444532.016789",
        )

        prompt = build_antigravity_prompt(payload)
        self.assertNotIn("agentapi new-conversation", prompt)
        self.assertIn("请仔细审查随附的图片与报错截图", prompt)

    def test_notice_detection_regexes(self):
        """Test regex pattern matching for offline notices, completion deliveries, and catch-up notices."""
        offline_text = (
            "⚠️ [Antigravity 离线提示 · Laptop Offline]\n"
            "• 状态: MacBook 当前未处于活动连接或正处于休眠状态\n"
            "• 网关响应: Webhook unreachable"
        )
        self.assertTrue(is_offline_notice(offline_text))
        self.assertFalse(is_delivery_completion(offline_text))

        completion_text = "✅ [已完成 · 结果交付]\nAntigravity 已分析完成并修复相关问题。"
        self.assertTrue(is_delivery_completion(completion_text))
        self.assertFalse(is_offline_notice(completion_text))

        reconcile_text = ":repeat: *[恢复调度 · Auto Catch-up Reconciled]*\n检测到本机已恢复在线..."
        self.assertTrue(is_reconcile_notice(reconcile_text))

        normal_text = "你好，请帮我排查独立站 500 报错问题"
        self.assertFalse(is_offline_notice(normal_text))
        self.assertFalse(is_delivery_completion(normal_text))
        self.assertFalse(is_reconcile_notice(normal_text))

    def test_reconciler_idempotency_skips_active_db_task(self):
        """SSOT: If a task for the channel:thread_ts is already queued or running in DB, skip dispatch."""
        reconciler = SlackReconciler(db=self.db, token="xoxb-fake-token")

        # Insert webhook event first to satisfy foreign key constraint
        self.db.insert_webhook_event({
            "event_id": "evt_test_123",
            "source": "slack_reconcile",
            "idempotency_key": "idemp_123",
            "payload_hash": "hash_123",
            "status": "received",
        })

        # Insert active task in DB
        task_data = {
            "channel_id": "C0C1B86AMCN",
            "thread_ts": "1789444532.016789",
            "text": "analyze 500 error",
        }
        self.db.insert_task({
            "task_id": "tsk_test_active_123",
            "event_id": "evt_test_123",
            "source": "slack_reconcile",
            "action_type": "antigravity",
            "command": "agentapi new-conversation",
            "action_params_json": json.dumps(task_data),
            "status": "queued",
            "priority": 1,
            "timeout_seconds": 300,
            "retry_count": 0,
            "max_retries": 3,
        })

        # Verify SSOT check
        is_active = reconciler.is_task_already_active_in_db("C0C1B86AMCN", "1789444532.016789")
        self.assertTrue(is_active)

        # Dispatch should be safely skipped
        res = reconciler.dispatch_reconciled_task({
            "channel_id": "C0C1B86AMCN",
            "thread_ts": "1789444532.016789",
            "prompt": "analyze 500 error",
        })
        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "already_active_in_db")

    @patch("urllib.request.urlopen")
    def test_reconciler_anti_spam_cooldown_intercepts_dispatch(self, mock_urlopen):
        """Verify that when a thread is in cooldown, both HTTP dispatch and Slack notification are intercepted."""
        reconciler = SlackReconciler(db=self.db, token="xoxb-fake-token")

        # Simulate thread info that is in cooldown
        thread_info = {
            "channel_id": "C0C1B86AMCN",
            "thread_ts": "1789444532.999999",
            "prompt": "Test prompt",
            "is_in_cooldown": True,
        }

        # Mock Slack API call
        reconciler._slack_api_call = MagicMock()

        res = reconciler.dispatch_reconciled_task(thread_info)
        self.assertEqual(res.get("status"), "skipped")
        self.assertEqual(res.get("reason"), "in_cooldown")

        # Neither HTTP dispatch nor Slack postMessage should be called
        mock_urlopen.assert_not_called()
        reconciler._slack_api_call.assert_not_called()

    def test_reconciler_idempotency_checks_session_threads(self):
        """Verify that an active session in session_threads table is recognized as active by reconciler, even if task is succeeded."""
        reconciler = SlackReconciler(db=self.db, token="xoxb-fake-token")

        # Insert webhook event and task first to satisfy foreign key constraints
        self.db.insert_webhook_event({
            "event_id": "evt_session_123",
            "source": "slack_task",
            "idempotency_key": "idemp_session_123",
            "payload_hash": "hash_session_123",
            "status": "received",
        })
        self.db.insert_task({
            "task_id": "tsk_session_active",
            "event_id": "evt_session_123",
            "source": "slack_task",
            "action_type": "antigravity",
            "command": "agentapi new-conversation",
            "status": "succeeded",
        })

        # Insert active session in session_threads
        self.db.upsert_session_thread({
            "thread_key": "C0C1B86AMCN:1789444532.016789",
            "channel_id": "C0C1B86AMCN",
            "root_ts": "1789444532.016789",
            "task_id": "tsk_session_active",
            "conversation_id": "conv_active_123",
            "status": "active",
        })

        is_active = reconciler.is_task_already_active_in_db("C0C1B86AMCN", "1789444532.016789")
        self.assertTrue(is_active)

    def test_follow_up_prompt_cleans_command_leak(self):
        """Verify build_follow_up_prompt filters out internal agentapi command string."""
        from hub.antigravity.prompt_builder import build_follow_up_prompt

        payload = AntigravityTaskPayload(
            text="agentapi new-conversation",
            channel="C0C1B86AMCN",
            ts="1789444532.016789",
            files=[{"url_private": "https://example.com/followup.png", "name": "followup.png"}],
        )

        prompt = build_follow_up_prompt(payload)
        self.assertNotIn("agentapi new-conversation", prompt)
        self.assertIn("用户补充了新的素材附件", prompt)
