"""
Unit Tests: Antigravity Agent Mobile Intake, Prompt Synthesis, Thread Transparency, and Session Linking
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hub.antigravity.agentapi_client import AgentAPIClient
from hub.antigravity.models import AntigravityTaskPayload, ThreadMilestone
from hub.antigravity.prompt_builder import (
    BOOST_DIRECTIVE,
    GOAL_DIRECTIVE,
    PSYCHOLOGICAL_COPYWRITER_DIRECTIVE,
    STRATEGIC_COMPACT_DIRECTIVE,
    TEAMWORK_PREVIEW_DIRECTIVE,
    build_antigravity_prompt,
    build_follow_up_prompt,
    extract_slash_commands,
)
from hub.antigravity.session_manager import execute_antigravity_task
from hub.antigravity.thread_notifier import ThreadNotifier
from hub.db import DatabaseManager


class TestAntigravityModels(unittest.TestCase):
    """Test payload dataclasses and conversion contracts."""

    def test_payload_from_dict_basic(self):
        data = {
            "text": "测试需求 /boost",
            "channel": "C0C1B86AMCN",
            "ts": "1789200000.100",
            "user": "U12345",
        }
        payload = AntigravityTaskPayload.from_dict(data)
        self.assertEqual(payload.text, "测试需求 /boost")
        self.assertEqual(payload.channel, "C0C1B86AMCN")
        self.assertEqual(payload.ts, "1789200000.100")
        self.assertEqual(payload.thread_key, "C0C1B86AMCN:1789200000.100")
        self.assertFalse(payload.is_follow_up)

    def test_payload_from_dict_follow_up(self):
        data = {
            "text": "请在上面的基础上补充说明",
            "channel": "C0C1B86AMCN",
            "ts": "1789200500.200",
            "thread_ts": "1789200000.100",
        }
        payload = AntigravityTaskPayload.from_dict(data)
        self.assertTrue(payload.is_follow_up)
        self.assertEqual(payload.thread_key, "C0C1B86AMCN:1789200000.100")

    def test_payload_with_audio_and_images(self):
        data = {
            "text": "整理这个页面",
            "audio_transcript": "你好这是语音输入",
            "files": [
                {"id": "F1", "name": "shot1.png", "mimetype": "image/png", "url_private_download": "https://slack.com/f1"},
                {"id": "F2", "name": "shot2.jpg", "mimetype": "image/jpeg", "url_private_download": "https://slack.com/f2"},
            ],
            "channel": "C0C1B86AMCN",
            "ts": "1789200000.100",
        }
        payload = AntigravityTaskPayload.from_dict(data)
        self.assertTrue(payload.voice_transcribed)
        self.assertEqual(payload.voice_transcript, "你好这是语音输入")
        self.assertEqual(len(payload.files), 2)


class TestPromptBuilder(unittest.TestCase):
    """Test slash command extraction, directive synthesis, and prompt formatting."""

    def test_extract_slash_commands(self):
        raw = "请帮我写一个推广文案 /psychological-copywriter /strategic-compact 抓紧时间"
        clean, cmds, directives = extract_slash_commands(raw)
        self.assertIn("psychological-copywriter", cmds)
        self.assertIn("strategic-compact", cmds)
        self.assertEqual(len(directives), 2)
        self.assertNotIn("/psychological-copywriter", clean)
        self.assertNotIn("/strategic-compact", clean)
        self.assertIn("请帮我写一个推广文案", clean)
        self.assertIn("抓紧时间", clean)

    def test_build_antigravity_prompt_with_images_and_directives(self):
        payload = AntigravityTaskPayload(
            text="分析此UI布局 /psychological-copywriter",
            voice_transcript="语音补充说要突出高级感",
            channel="C0C1B86AMCN",
            ts="1789200000.100",
        )
        downloaded = ["/tmp/data/attachments/tsk_1/1_ui.png", "/tmp/data/attachments/tsk_1/2_spec.png"]
        prompt = build_antigravity_prompt(payload, downloaded_images=downloaded)

        self.assertIn("分析此UI布局", prompt)
        self.assertIn("[语音转录 / Voice Transcript]:", prompt)
        self.assertIn("语音补充说要突出高级感", prompt)
        self.assertIn("[本地图片素材 / Attached Images (2)]:", prompt)
        self.assertIn("/tmp/data/attachments/tsk_1/1_ui.png", prompt)
        self.assertIn(PSYCHOLOGICAL_COPYWRITER_DIRECTIVE, prompt)

    def test_build_follow_up_prompt(self):
        payload = AntigravityTaskPayload(
            text="第二张图里的配色换成深色系 /strategic-compact",
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            ts="1789200500.200",
            is_follow_up=True,
        )
        follow_up = build_follow_up_prompt(payload)
        self.assertIn("[用户追问 / User Follow-up]:", follow_up)
        self.assertIn("第二张图里的配色换成深色系", follow_up)
        self.assertIn(STRATEGIC_COMPACT_DIRECTIVE, follow_up)


class TestThreadNotifier(unittest.TestCase):
    """Test Slack milestone comment dispatcher."""

    def test_notifier_not_configured_fails_open(self):
        notifier = ThreadNotifier(token="")
        self.assertFalse(notifier.is_configured())
        self.assertFalse(notifier.notify_collected("C0C1B86AMCN", "1789200000.100", "tsk_123"))

    @patch("urllib.request.urlopen")
    def test_notifier_success_posts(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True, "ts": "1789200001.000"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        notifier = ThreadNotifier(token="xoxb-mock-token-12345")
        self.assertTrue(notifier.is_configured())

        # Test milestone 1: collected
        ok1 = notifier.notify_collected("C0C1B86AMCN", "1789200000.100", "tsk_test", title="测试任务", commands=["boost"], files_count=2)
        self.assertTrue(ok1)

        # Test milestone 2: in progress
        ok2 = notifier.notify_in_progress("C0C1B86AMCN", "1789200000.100", "tsk_test", model_tier="pro", active_skills=["psychological-copywriter"])
        self.assertTrue(ok2)

        # Test milestone 3: done
        ok3 = notifier.notify_done("C0C1B86AMCN", "1789200000.100", "tsk_test", conversation_id="conv-1234-5678", elapsed_seconds=2.5)
        self.assertTrue(ok3)

        # Test milestone 4: follow_up
        ok4 = notifier.notify_follow_up("C0C1B86AMCN", "1789200000.100", conversation_id="conv-1234-5678", snippet="补充说明", files_count=1)
        self.assertTrue(ok4)


class TestSessionManagerAndDatabase(unittest.TestCase):
    """Test end-to-end task execution and thread-to-session database mapping."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_hub.db"
        self.db = DatabaseManager(str(self.db_path))
        self.db.init_schema()

    def tearDown(self):
        self.db.close()
        self.tmp_dir.cleanup()

    def _seed_task(self, task_id: str):
        task_rec = {
            "task_id": task_id,
            "source": "slack_agent",
            "action_type": "antigravity",
            "status": "running",
            "command": "agentapi new-conversation",
        }
        self.db.insert_task(task_rec)

    def test_database_session_threads_crud(self):
        self._seed_task("tsk_demo_01")
        # Insert
        rec = {
            "thread_key": "C0C1B86AMCN:1789200000.100",
            "channel_id": "C0C1B86AMCN",
            "root_ts": "1789200000.100",
            "task_id": "tsk_demo_01",
            "conversation_id": "conv-337a5baf-39ac-44b6",
            "source": "slack_agent",
            "model_tier": "pro",
            "title": "测试主线会话",
            "status": "active",
        }
        ok = self.db.upsert_session_thread(rec)
        self.assertTrue(ok)

        # Query
        fetched = self.db.get_session_thread("C0C1B86AMCN:1789200000.100")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["conversation_id"], "conv-337a5baf-39ac-44b6")
        self.assertEqual(fetched["title"], "测试主线会话")

        # Touch
        touched = self.db.touch_session_thread("C0C1B86AMCN:1789200000.100")
        self.assertTrue(touched)

        # List
        threads = self.db.list_session_threads(limit=10)
        self.assertEqual(len(threads), 1)

    def test_execute_antigravity_task_new_conversation(self):
        self._seed_task("tsk_test_new")

        async def _run():
            mock_client = MagicMock(spec=AgentAPIClient)
            mock_client.new_conversation = AsyncMock(return_value=(True, "conv-new-12345", None))
            mock_client.send_message = AsyncMock(return_value=(True, "OK", None))

            mock_notifier = MagicMock(spec=ThreadNotifier)
            mock_notifier.notify_in_progress = MagicMock(return_value=True)
            mock_notifier.notify_done = MagicMock(return_value=True)

            task_data = {
                "task_id": "tsk_test_new",
                "action_type": "antigravity",
                "action_params": {
                    "text": "编写 landing page 核心逻辑 /psychological-copywriter",
                    "channel": "C0C1B86AMCN",
                    "ts": "1789200000.100",
                    "thread_ts": "1789200000.100",
                },
            }

            res = await execute_antigravity_task(
                task_data=task_data,
                db=self.db,
                agentapi_client=mock_client,
                thread_notifier=mock_notifier,
            )

            self.assertTrue(res["success"])
            self.assertEqual(res["conversation_id"], "conv-new-12345")
            self.assertEqual(res["thread_key"], "C0C1B86AMCN:1789200000.100")

            # Verify DB mapping was written
            db_rec = self.db.get_session_thread("C0C1B86AMCN:1789200000.100")
            self.assertIsNotNone(db_rec)
            self.assertEqual(db_rec["conversation_id"], "conv-new-12345")

            # Verify notifier calls
            mock_notifier.notify_in_progress.assert_called_once()
            mock_notifier.notify_done.assert_called_once()

        asyncio.run(_run())

    def test_execute_antigravity_task_follow_up_routes_to_existing_conversation(self):
        self._seed_task("tsk_parent")
        self._seed_task("tsk_followup_02")

        async def _run():
            # Seed existing session in DB
            self.db.upsert_session_thread({
                "thread_key": "C0C1B86AMCN:1789200000.100",
                "channel_id": "C0C1B86AMCN",
                "root_ts": "1789200000.100",
                "task_id": "tsk_parent",
                "conversation_id": "conv-existing-7788",
                "status": "active",
            })

            mock_client = MagicMock(spec=AgentAPIClient)
            mock_client.send_message = AsyncMock(return_value=(True, "Synced", None))

            mock_notifier = MagicMock(spec=ThreadNotifier)
            mock_notifier.notify_follow_up = MagicMock(return_value=True)

            follow_up_task = {
                "task_id": "tsk_followup_02",
                "action_type": "antigravity",
                "action_params": {
                    "text": "把主色调换成哑光黑 /strategic-compact",
                    "channel": "C0C1B86AMCN",
                    "ts": "1789200500.200",
                    "thread_ts": "1789200000.100",
                },
            }

            res = await execute_antigravity_task(
                task_data=follow_up_task,
                db=self.db,
                agentapi_client=mock_client,
                thread_notifier=mock_notifier,
            )

            self.assertTrue(res["success"])
            self.assertTrue(res.get("is_follow_up"))
            self.assertEqual(res["conversation_id"], "conv-existing-7788")

            # Verify send_message was called with existing session ID
            mock_client.send_message.assert_called_once()
            args, kwargs = mock_client.send_message.call_args
            self.assertEqual(kwargs.get("conversation_id"), "conv-existing-7788")
            self.assertIn("把主色调换成哑光黑", kwargs.get("content"))
            self.assertIn(STRATEGIC_COMPACT_DIRECTIVE, kwargs.get("content"))

            # Verify notifier follow up called
            mock_notifier.notify_follow_up.assert_called_once()

        asyncio.run(_run())

    def test_execute_antigravity_task_expired_session_self_healing_recovery(self):
        self._seed_task("tsk_parent_expired")
        self._seed_task("tsk_followup_recover")

        async def _run():
            # Seed existing session in DB
            self.db.upsert_session_thread({
                "thread_key": "C0C1B86AMCN:1789200000.200",
                "channel_id": "C0C1B86AMCN",
                "root_ts": "1789200000.200",
                "task_id": "tsk_parent_expired",
                "conversation_id": "conv-expired-9999",
                "status": "active",
            })

            mock_client = MagicMock(spec=AgentAPIClient)
            # send_message fails with expired session
            mock_client.send_message = AsyncMock(return_value=(False, "", "conversation not found or expired"))
            # new_conversation succeeds for recovered session
            mock_client.new_conversation = AsyncMock(return_value=(True, "conv-recovered-1111", None))

            mock_notifier = MagicMock(spec=ThreadNotifier)
            mock_notifier.notify_in_progress = MagicMock(return_value=True)
            mock_notifier.notify_done = MagicMock(return_value=True)

            mock_broker = MagicMock()
            mock_broker.publish = AsyncMock(return_value=1)

            follow_up_task = {
                "task_id": "tsk_followup_recover",
                "action_type": "antigravity",
                "action_params": {
                    "text": "继续优化排版 /boost",
                    "channel": "C0C1B86AMCN",
                    "ts": "1789200600.300",
                    "thread_ts": "1789200000.200",
                },
            }

            res = await execute_antigravity_task(
                task_data=follow_up_task,
                db=self.db,
                agentapi_client=mock_client,
                thread_notifier=mock_notifier,
                broker=mock_broker,
            )

            self.assertTrue(res["success"])
            self.assertEqual(res["conversation_id"], "conv-recovered-1111")
            self.assertEqual(res.get("recovered_from"), "conv-expired-9999")

            # Check SSOT database mapping has been updated to recovered conversation
            updated_sess = self.db.get_session_thread("C0C1B86AMCN:1789200000.200")
            self.assertIsNotNone(updated_sess)
            self.assertEqual(updated_sess["conversation_id"], "conv-recovered-1111")
            self.assertEqual(updated_sess["status"], "active")

            # Verify broker events
            published_events = [call[0][1].get("event") for call in mock_broker.publish.call_args_list]
            self.assertIn("antigravity_session_recovery_triggered", published_events)
            self.assertIn("antigravity_session_created", published_events)

        asyncio.run(_run())

    def test_execute_antigravity_task_quota_exhaustion_auto_downgrade(self):
        self._seed_task("tsk_quota_429")

        async def _run():
            mock_client = MagicMock(spec=AgentAPIClient)
            # First attempt with pro model fails with 429 quota exhaustion
            # Second attempt with flash_lite succeeds
            mock_client.new_conversation = AsyncMock(side_effect=[
                (False, "", "HTTP 429 Resource exhausted: quota limit exceeded for pro"),
                (True, "conv-downgraded-2222", None),
            ])

            mock_notifier = MagicMock(spec=ThreadNotifier)
            mock_notifier.notify_in_progress = MagicMock(return_value=True)
            mock_notifier.notify_done = MagicMock(return_value=True)

            mock_broker = MagicMock()
            mock_broker.publish = AsyncMock(return_value=1)

            task_data = {
                "task_id": "tsk_quota_429",
                "action_type": "antigravity",
                "action_params": {
                    "text": "高并发测试任务",
                    "channel": "C0C1B86AMCN",
                    "ts": "1789200800.400",
                    "thread_ts": "1789200800.400",
                    "model_tier": "pro",
                },
            }

            res = await execute_antigravity_task(
                task_data=task_data,
                db=self.db,
                agentapi_client=mock_client,
                thread_notifier=mock_notifier,
                broker=mock_broker,
            )

            self.assertTrue(res["success"])
            self.assertEqual(res["conversation_id"], "conv-downgraded-2222")
            self.assertEqual(res["model_tier"], "flash_lite")
            self.assertEqual(mock_client.new_conversation.call_count, 2)

            # Check broker published downgrade event
            published_events = [call[0][1].get("event") for call in mock_broker.publish.call_args_list]
            self.assertIn("antigravity_model_downgraded", published_events)
            self.assertIn("antigravity_session_created", published_events)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
