"""
Unit Tests: Antigravity Agent Mobile Intake, Prompt Synthesis, Thread Transparency, and Session Linking
"""

import asyncio
from datetime import datetime, timezone
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from hub.antigravity.agentapi_client import (
    AgentAPIClient,
    discover_active_antigravity_credentials,
    is_connection_error,
    validate_antigravity_address,
)
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

    def test_build_follow_up_prompt_preserves_boost_and_goal(self):
        payload = AntigravityTaskPayload(
            text="继续推进该目标 /boost /goal",
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            ts="1789200500.200",
            is_follow_up=True,
        )
        follow_up = build_follow_up_prompt(payload)
        self.assertIn("[/boost](slashCommand;boost)", follow_up)
        self.assertIn("[/goal](slashCommand;goal)", follow_up)
        self.assertIn("[用户追问 / User Follow-up]:", follow_up)
        self.assertIn("继续推进该目标", follow_up)
        self.assertIn(BOOST_DIRECTIVE, follow_up)
        self.assertIn(GOAL_DIRECTIVE, follow_up)

    def test_build_follow_up_prompt_with_payload_slash_commands(self):
        payload = AntigravityTaskPayload(
            text="追问内容",
            slash_commands=["boost", "goal"],
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            ts="1789200500.200",
            is_follow_up=True,
        )
        follow_up = build_follow_up_prompt(payload)
        self.assertIn("[/boost](slashCommand;boost)", follow_up)
        self.assertIn("[/goal](slashCommand;goal)", follow_up)
        self.assertIn(BOOST_DIRECTIVE, follow_up)
        self.assertIn(GOAL_DIRECTIVE, follow_up)

    def test_build_follow_up_prompt_with_payload_slash_commands_with_leading_slashes(self):
        payload = AntigravityTaskPayload(
            text="追问内容，请全力推进",
            slash_commands=["/boost", "/goal"],
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            ts="1789200500.200",
            is_follow_up=True,
        )
        follow_up = build_follow_up_prompt(payload)
        self.assertIn("[/boost](slashCommand;boost)", follow_up)
        self.assertIn("[/goal](slashCommand;goal)", follow_up)
        self.assertIn(BOOST_DIRECTIVE, follow_up)
        self.assertIn(GOAL_DIRECTIVE, follow_up)

    def test_build_antigravity_prompt_attachment_download_failure_alert(self):
        payload = AntigravityTaskPayload(
            text="分析此UI布局 /boost /goal",
            files=[
                {"name": "diagram.png", "mimetype": "image/png"},
                {"name": "mockup.jpg", "mimetype": "image/jpeg"},
            ],
            channel="C0C1B86AMCN",
            ts="1789200000.100",
        )
        # When downloaded_images is empty (download failure)
        prompt = build_antigravity_prompt(payload, downloaded_images=[])
        self.assertIn("[/boost](slashCommand;boost)", prompt)
        self.assertIn("[/goal](slashCommand;goal)", prompt)
        self.assertIn("[⚠️ 附件告警 / Attachment Download Failure]:", prompt)
        self.assertIn("diagram.png", prompt)
        self.assertIn("mockup.jpg", prompt)
        self.assertIn("但在下载落盘时发生中断", prompt)

    def test_build_follow_up_prompt_attachment_download_failure_alert(self):
        payload = AntigravityTaskPayload(
            text="请看这张新图",
            files=[{"name": "new_screen.png", "mimetype": "image/png"}],
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            ts="1789200500.200",
            is_follow_up=True,
        )
        follow_up = build_follow_up_prompt(payload, downloaded_images=[])
        self.assertIn("[用户追问 / User Follow-up]:", follow_up)
        self.assertIn("[⚠️ 附件告警 / Attachment Download Failure]:", follow_up)
        self.assertIn("new_screen.png", follow_up)


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

        # Test milestone 1: collected with merged commands (e.g. /boost and /goal)
        with patch.object(notifier, "post_thread_message") as mock_post:
            notifier.notify_collected("C0C1B86AMCN", "1789200000.100", "tsk_test_2", title="全流程加速", commands=["boost", "goal"], files_count=1)
            mock_post.assert_called_once()
            call_text = mock_post.call_args[0][2]
            self.assertIn("• *识别指令*: `/boost`, `/goal`", call_text)
            self.assertIn("• *素材附件*: `1` 个", call_text)

        # Test milestone 2: in progress
        ok2 = notifier.notify_in_progress("C0C1B86AMCN", "1789200000.100", "tsk_test", model_tier="pro", active_skills=["psychological-copywriter"])
        self.assertTrue(ok2)

        # Test milestone 3: done
        ok3 = notifier.notify_done("C0C1B86AMCN", "1789200000.100", "tsk_test", conversation_id="conv-1234-5678", elapsed_seconds=2.5)
        self.assertTrue(ok3)

        # Test milestone 4: follow_up
        ok4 = notifier.notify_follow_up("C0C1B86AMCN", "1789200000.100", conversation_id="conv-1234-5678", snippet="补充说明", files_count=1)
        self.assertTrue(ok4)

    def test_milestone_slash_commands_merge_logic(self):
        """Verify that upstream n8n body_dict['slash_commands'] merges with text commands and deduplicates."""
        from hub.antigravity.prompt_builder import extract_slash_commands

        body_dict = {
            "text": "分析此UI布局 /strategic-compact",
            "slash_commands": ["boost", "/goal", "boost"],
            "channel": "C0C1B86AMCN",
            "ts": "1789200000.100",
        }
        _, text_cmds, _ = extract_slash_commands(body_dict.get("text") or "")
        raw_incoming_cmds = body_dict.get("slash_commands") or []
        if isinstance(raw_incoming_cmds, str):
            raw_incoming_cmds = [c.strip() for c in raw_incoming_cmds.split(",") if c.strip()]
        incoming_cmds = [str(c).lstrip("/") for c in raw_incoming_cmds if str(c).strip()]
        cmds = list(dict.fromkeys(incoming_cmds + text_cmds))

        self.assertEqual(cmds, ["boost", "goal", "strategic-compact"])

    def test_milestone_slash_commands_merge_with_nested_data(self):
        """Verify that nested body_dict['data']['slash_commands'] and files are extracted."""
        from hub.antigravity.prompt_builder import extract_slash_commands

        body_dict = {
            "action": "antigravity.run",
            "data": {
                "text": "分析此UI布局 /boost",
                "slash_commands": "goal, /scheduled-task-rescheduler",
                "channel": "C0C1B86AMCN",
                "ts": "1789200000.100",
                "image_urls": ["https://files.slack.com/img1.png", "https://files.slack.com/img2.png"],
            }
        }
        sub_dict = body_dict.get("data") if isinstance(body_dict.get("data"), dict) else {}
        incoming_text = (
            body_dict.get("text")
            or sub_dict.get("text")
            or ""
        )
        _, text_cmds, _ = extract_slash_commands(incoming_text)
        raw_incoming_cmds = (
            body_dict.get("slash_commands")
            or sub_dict.get("slash_commands")
            or []
        )
        if isinstance(raw_incoming_cmds, str):
            raw_incoming_cmds = [c.strip() for c in raw_incoming_cmds.split(",") if c.strip()]
        incoming_cmds = [str(c).strip().lstrip("/") for c in raw_incoming_cmds if str(c).strip().lstrip("/")]
        cmds = list(dict.fromkeys(incoming_cmds + text_cmds))

        raw_files = (
            body_dict.get("files")
            or sub_dict.get("files")
            or body_dict.get("image_urls")
            or sub_dict.get("image_urls")
            or []
        )
        self.assertEqual(cmds, ["goal", "scheduled-task-rescheduler", "boost"])
        self.assertEqual(len(raw_files), 2)

    def test_build_follow_up_prompt_no_attachments_does_not_claim_new_attachments(self):
        """Verify that follow-up prompt without attachments or text does not hallucinate attachment claims."""
        payload = AntigravityTaskPayload(
            text="/boost",
            files=[],
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            ts="1789200500.200",
            is_follow_up=True,
        )
        follow_up = build_follow_up_prompt(payload, downloaded_images=[])
        self.assertNotIn("用户补充了新的素材附件", follow_up)
        self.assertIn("用户继续会话", follow_up)

    def test_payload_from_dict_slash_commands_string_and_slashes_normalization(self):
        """Verify AntigravityTaskPayload.from_dict normalizes strings and slashes in slash_commands."""
        data1 = {"slash_commands": "boost, /goal, boost"}
        p1 = AntigravityTaskPayload.from_dict(data1)
        self.assertEqual(p1.slash_commands, ["boost", "goal"])

        data2 = {"slash_commands": ["/boost", "goal", "/boost"]}
        p2 = AntigravityTaskPayload.from_dict(data2)
        self.assertEqual(p2.slash_commands, ["boost", "goal"])

        data3 = {"data": {"slash_commands": "strategic-compact"}}
        p3 = AntigravityTaskPayload.from_dict(data3)
        self.assertEqual(p3.slash_commands, ["strategic-compact"])


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

            # Verify notifier calls: in_progress is called, premature notify_done is NOT called
            mock_notifier.notify_in_progress.assert_called_once()
            mock_notifier.notify_done.assert_not_called()

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

    def test_follow_up_execution_launches_watcher_with_next_step(self):
        self._seed_task("tsk_parent_01")
        self._seed_task("tsk_followup_02")

        async def _run():
            self.db.upsert_session_thread({
                "thread_key": "C0C1B86AMCN:1789200000.300",
                "channel_id": "C0C1B86AMCN",
                "root_ts": "1789200000.300",
                "task_id": "tsk_parent_01",
                "conversation_id": "conv-target-1234",
                "source": "slack_agent",
                "model_tier": "pro",
                "title": "测试初始会话",
                "status": "active",
            })

            mock_client = MagicMock()
            mock_client.send_message = AsyncMock(return_value=(True, '{"status":"ok"}', None))
            mock_notifier = MagicMock()

            follow_up_task = {
                "task_id": "tsk_followup_02",
                "source": "slack_agent",
                "action_type": "antigravity",
                "action_params_json": json.dumps({
                    "text": "测试追问 /boost",
                    "channel": "C0C1B86AMCN",
                    "thread_ts": "1789200000.300",
                    "ts": "1789200500.400",
                }),
            }

            with patch("hub.antigravity.session_manager.get_latest_step_index", return_value=7), \
                 patch("hub.antigravity.session_manager.watch_and_deliver_result", new_callable=AsyncMock) as mock_watch, \
                 patch("hub.antigravity.session_manager.cancel_active_watcher") as mock_cancel:
                res = await execute_antigravity_task(
                    task_data=follow_up_task,
                    db=self.db,
                    agentapi_client=mock_client,
                    thread_notifier=mock_notifier,
                )
                self.assertTrue(res["success"])
                self.assertTrue(res["is_follow_up"])
                mock_cancel.assert_called_once_with("conv-target-1234")
                # Ensure start_step was passed as latest_step + 1 = 8
                mock_watch.assert_called_once()
                w_kwargs = mock_watch.call_args[1]
                self.assertEqual(w_kwargs.get("start_step"), 8)
                self.assertTrue(w_kwargs.get("is_follow_up"))
                self.assertEqual(w_kwargs.get("conversation_id"), "conv-target-1234")

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


class TestAgentAPIDynamicCredentialsAndSelfHealing(unittest.TestCase):
    """Test dynamic credential discovery, address validation, error pattern detection, and self-healing retries."""

    def test_validate_antigravity_address(self):
        # Mock responsive 200 server
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.assertTrue(validate_antigravity_address("localhost:63347"))
            self.assertTrue(validate_antigravity_address("127.0.0.1:63347"))

        # Empty address
        self.assertFalse(validate_antigravity_address(""))

        # Error server
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            self.assertFalse(validate_antigravity_address("localhost:56268"))

    def test_is_connection_error(self):
        # True cases
        self.assertTrue(is_connection_error("dial tcp 127.0.0.1:56268: connect: connection refused"))
        self.assertTrue(is_connection_error("rpc error: code = Unavailable desc = connection error"))
        self.assertTrue(is_connection_error("transport: Error while dialing: dial tcp"))
        self.assertTrue(is_connection_error("Broken pipe"))
        self.assertTrue(is_connection_error("Connection reset by peer"))
        self.assertTrue(is_connection_error("channel is in state transient_failure"))

        # False cases
        self.assertFalse(is_connection_error(None))
        self.assertFalse(is_connection_error(""))
        self.assertFalse(is_connection_error("trajectory not found"))
        self.assertFalse(is_connection_error("HTTP 429 quota exceeded"))
        self.assertFalse(is_connection_error("invalid argument"))

    def test_discover_active_antigravity_credentials_from_children(self):
        fake_ps = (
            "USER       PID  %CPU %MEM      VSZ    RSS   TT  STAT STARTED      TIME COMMAND\n"
            "user     12115   0.1  0.5  1234567  50000   ??  S     8:24AM   0:10.00 "
            "/Applications/Antigravity.app/Contents/Resources/bin/language_server --standalone "
            "--csrf_token cc31d02d-949d-468e-a975-2bb657ac01c5 --app_data_dir antigravity\n"
        )
        fake_pgrep = "12187\n12188\n"
        fake_ps_eww = (
            "12187 ANTIGRAVITY_LS_ADDRESS=localhost:63347 "
            "ANTIGRAVITY_CSRF_TOKEN=cc31d02d-949d-468e-a975-2bb657ac01c5 OTHER_VAR=1"
        )

        def mock_check_output(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "ps aux" in cmd_str:
                return fake_ps
            elif "pgrep -P 12115" in cmd_str:
                return fake_pgrep
            elif "ps eww 12187" in cmd_str:
                return fake_ps_eww
            return ""

        with patch("subprocess.check_output", side_effect=mock_check_output):
            with patch("hub.antigravity.agentapi_client.validate_antigravity_address", return_value=True):
                with patch.dict("os.environ", {}, clear=True):
                    addr, token = discover_active_antigravity_credentials(force=True)
                    self.assertEqual(addr, "localhost:63347")
                    self.assertEqual(token, "cc31d02d-949d-468e-a975-2bb657ac01c5")

    def test_discover_active_antigravity_credentials_fallback_lsof(self):
        fake_ps = (
            "user     12115   0.1  0.5  1234567  50000   ??  S     8:24AM   0:10.00 "
            "/Applications/Antigravity.app/Contents/Resources/bin/language_server --standalone "
            "--csrf_token fb001122-3344-5566-7788-99aabbccddeeff\n"
        )
        fake_lsof = (
            "COMMAND     PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "language_ 12115 user    7u  IPv4 0x1234      0t0  TCP 127.0.0.1:63346 (LISTEN)\n"
            "language_ 12115 user    8u  IPv4 0x5678      0t0  TCP 127.0.0.1:63347 (LISTEN)\n"
        )

        def mock_check_output(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "ps aux" in cmd_str:
                return fake_ps
            elif "pgrep" in cmd_str:
                return ""
            elif "lsof" in cmd_str:
                return fake_lsof
            return ""

        def mock_validate(addr, **kwargs):
            return "63347" in addr

        with patch("subprocess.check_output", side_effect=mock_check_output):
            with patch("hub.antigravity.agentapi_client.validate_antigravity_address", side_effect=mock_validate):
                with patch.dict("os.environ", {}, clear=True):
                    addr, token = discover_active_antigravity_credentials(force=True)
                    self.assertEqual(addr, "localhost:63347")
                    self.assertEqual(token, "fb001122-3344-5566-7788-99aabbccddeeff")

    def test_discover_active_antigravity_credentials_absent_returns_none(self):
        with patch("subprocess.check_output", return_value="user 1000 0.0 ps aux\n"):
            with patch.dict("os.environ", {}, clear=True):
                addr, token = discover_active_antigravity_credentials(force=True)
                self.assertIsNone(addr)
                self.assertIsNone(token)

    def test_agentapi_client_new_conversation_self_healing_retry(self):
        client = AgentAPIClient(executable_path="/usr/local/bin/agentapi")
        client.is_available = MagicMock(return_value=True)

        mock_proc_fail = AsyncMock()
        mock_proc_fail.returncode = 1
        mock_proc_fail.communicate = AsyncMock(return_value=(
            b"",
            b'rpc error: code = Unavailable desc = connection error: desc = "transport: Error while dialing: dial tcp 127.0.0.1:56268: connect: connection refused"',
        ))

        mock_proc_ok = AsyncMock()
        mock_proc_ok.returncode = 0
        mock_proc_ok.communicate = AsyncMock(return_value=(
            b'{"response": {"newConversation": {"conversationId": "369d90c6-a9d5-468e-a975-2bb657ac01c5"}}}',
            b"",
        ))

        async def _run_e2e_retry():
            with patch("asyncio.create_subprocess_exec", side_effect=[mock_proc_fail, mock_proc_ok]) as mock_subproc:
                with patch.object(client, "ensure_credentials") as mock_ensure:
                    mock_ensure.side_effect = [
                        ("localhost:56268", "old_token"),
                        ("localhost:63347", "new_token"),
                    ]
                    success, cid, err = await client.new_conversation("阳朔天气", model="pro")
                    self.assertTrue(success)
                    self.assertEqual(cid, "369d90c6-a9d5-468e-a975-2bb657ac01c5")
                    self.assertIsNone(err)
                    self.assertEqual(mock_subproc.call_count, 2)
                    self.assertEqual(mock_ensure.call_count, 2)

        asyncio.run(_run_e2e_retry())

    def test_agentapi_client_send_message_self_healing_retry(self):
        client = AgentAPIClient(executable_path="/usr/local/bin/agentapi")
        client.is_available = MagicMock(return_value=True)

        mock_proc_fail = AsyncMock()
        mock_proc_fail.returncode = 1
        mock_proc_fail.communicate = AsyncMock(return_value=(
            b"",
            b'rpc error: code = Unavailable desc = connection error: desc = "dial tcp 127.0.0.1:56268: connect: connection refused"',
        ))

        mock_proc_ok = AsyncMock()
        mock_proc_ok.returncode = 0
        mock_proc_ok.communicate = AsyncMock(return_value=(b'{"status": "delivered"}', b""))

        async def _run():
            with patch("asyncio.create_subprocess_exec", side_effect=[mock_proc_fail, mock_proc_ok]) as mock_subproc:
                with patch.object(client, "ensure_credentials") as mock_ensure:
                    mock_ensure.side_effect = [
                        ("localhost:56268", "old_token"),
                        ("localhost:63347", "new_token"),
                    ]
                    success, res_msg, err = await client.send_message(
                        conversation_id="conv-12345",
                        content="请补充说明",
                    )
                    self.assertTrue(success)
                    self.assertIn("delivered", res_msg)
                    self.assertIsNone(err)
                    self.assertEqual(mock_subproc.call_count, 2)

        asyncio.run(_run())

    def test_agentapi_client_retry_exhaustion_on_persistent_failure(self):
        client = AgentAPIClient(executable_path="/usr/local/bin/agentapi")
        client.is_available = MagicMock(return_value=True)

        mock_proc_fail1 = AsyncMock()
        mock_proc_fail1.returncode = 1
        mock_proc_fail1.communicate = AsyncMock(return_value=(
            b"",
            b'dial tcp 127.0.0.1:56268: connect: connection refused',
        ))

        mock_proc_fail2 = AsyncMock()
        mock_proc_fail2.returncode = 1
        mock_proc_fail2.communicate = AsyncMock(return_value=(
            b"",
            b'dial tcp 127.0.0.1:63347: connect: connection refused',
        ))

        async def _run():
            with patch("asyncio.create_subprocess_exec", side_effect=[mock_proc_fail1, mock_proc_fail2]) as mock_subproc:
                with patch.object(client, "ensure_credentials") as mock_ensure:
                    mock_ensure.side_effect = [
                        ("localhost:56268", "old_token"),
                        ("localhost:63347", "new_token"),
                    ]
                    success, cid, err = await client.new_conversation("测试重试耗尽")
                    self.assertFalse(success)
                    self.assertEqual(cid, "")
                    self.assertIn("connection refused", err)
                    self.assertEqual(mock_subproc.call_count, 2)

        asyncio.run(_run())

    def test_discover_active_antigravity_credentials_filters_false_positives(self):
        fake_ps = (
            "USER       PID  %CPU %MEM      VSZ    RSS   TT  STAT STARTED      TIME COMMAND\n"
            "user      1000   0.5  0.1  1000000  20000   ??  S     8:20AM   0:01.00 "
            "python3 -c import os; print('language_server --standalone')\n"
            "user      1001   0.0  0.0   500000   5000   ??  S     8:20AM   0:00.01 "
            "grep language_server --standalone\n"
            "user     12115   0.1  0.5  1234567  50000   ??  S     8:24AM   0:10.00 "
            "/Applications/Antigravity.app/Contents/Resources/bin/language_server --standalone "
            "--csrf_token cc31d02d-949d-468e-a975-2bb657ac01c5 --app_data_dir antigravity\n"
        )
        fake_pgrep = "12187\n"
        fake_ps_eww = (
            "12187 ANTIGRAVITY_LS_ADDRESS=localhost:63347 "
            "ANTIGRAVITY_CSRF_TOKEN=cc31d02d-949d-468e-a975-2bb657ac01c5"
        )

        def mock_check_output(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "ps aux" in cmd_str:
                return fake_ps
            elif "pgrep -P 12115" in cmd_str:
                return fake_pgrep
            elif "ps eww 12187" in cmd_str:
                return fake_ps_eww
            return ""

        with patch("subprocess.check_output", side_effect=mock_check_output):
            with patch("hub.antigravity.agentapi_client.validate_antigravity_address", return_value=True):
                with patch.dict("os.environ", {}, clear=True):
                    addr, token = discover_active_antigravity_credentials(force=True)
                    self.assertEqual(addr, "localhost:63347")
                    self.assertEqual(token, "cc31d02d-949d-468e-a975-2bb657ac01c5")

    def test_ensure_credentials_force_clears_on_discovery_failure(self):
        client = AgentAPIClient()
        client.ls_address = "localhost:56268"
        client.csrf_token = "stale-token"

        with patch("hub.antigravity.agentapi_client.discover_active_antigravity_credentials", return_value=(None, None)):
            addr, token = client.ensure_credentials(force=True)
            self.assertIsNone(addr)
            self.assertIsNone(token)
            self.assertIsNone(client.ls_address)
            self.assertIsNone(client.csrf_token)

    def test_agentapi_client_dynamic_availability_recovery(self):
        client = AgentAPIClient(executable_path="/nonexistent/path/agentapi")
        with patch("hub.antigravity.agentapi_client.resolve_agentapi_path", return_value=None):
            self.assertFalse(client.is_available())

        with patch("hub.antigravity.agentapi_client.resolve_agentapi_path", return_value="/bin/sh"):
            self.assertTrue(client.is_available())
            self.assertEqual(client.executable_path, "/bin/sh")

    def test_scheduled_task_rescheduler_slash_command(self):
        from hub.antigravity.prompt_builder import SCHEDULED_TASK_RESCHEDULER_DIRECTIVE
        raw = "请检查当前所有 Cadence 卡片 /scheduled-task-rescheduler"
        clean, cmds, directives = extract_slash_commands(raw)
        self.assertIn("scheduled-task-rescheduler", cmds)
        self.assertIn(SCHEDULED_TASK_RESCHEDULER_DIRECTIVE, directives)
        self.assertNotIn("/scheduled-task-rescheduler", clean)
        self.assertIn("请检查当前所有 Cadence 卡片", clean)

        payload = AntigravityTaskPayload(text=raw, channel="C0C1B86AMCN", ts="1789200000.100")
        prompt = build_antigravity_prompt(payload)
        self.assertIn(SCHEDULED_TASK_RESCHEDULER_DIRECTIVE, prompt)

    def test_rich_slash_command_and_goal_boost_prefixing(self):
        # 1. Plain text /goal should prefix with [/goal](slashCommand;goal)
        p1 = AntigravityTaskPayload(text="阳朔天气 /goal", channel="C0C1B86AMCN", ts="1789200000.101")
        prompt1 = build_antigravity_prompt(p1)
        self.assertTrue(prompt1.startswith("[/goal](slashCommand;goal) 阳朔天气"))

        # 2. Rich format [/boost](slashCommand;boost) in raw text should be extracted and prefixed
        p2 = AntigravityTaskPayload(text="清理垃圾 [/boost](slashCommand;boost)", channel="C0C1B86AMCN", ts="1789200000.102")
        prompt2 = build_antigravity_prompt(p2)
        self.assertTrue(prompt2.startswith("[/boost](slashCommand;boost) 清理垃圾"))

        # 3. Both goal and boost
        p3 = AntigravityTaskPayload(text="/goal /boost 紧急全量扫描", channel="C0C1B86AMCN", ts="1789200000.103")
        prompt3 = build_antigravity_prompt(p3)
        self.assertTrue(prompt3.startswith("[/goal](slashCommand;goal) [/boost](slashCommand;boost) 紧急全量扫描"))

    def test_is_connection_error_extended_patterns(self):
        self.assertTrue(is_connection_error("failed to connect to all addresses"))
        self.assertTrue(is_connection_error("connection closed before server preface received"))
        self.assertTrue(is_connection_error("rpc error: code = Unavailable desc = transport is closing"))
        self.assertTrue(is_connection_error("rpc error: code = DeadlineExceeded"))
        self.assertTrue(is_connection_error("dial tcp 127.0.0.1:63347: connect: network is unreachable"))
        self.assertTrue(is_connection_error("dial tcp 127.0.0.1:63347: connect: no route to host"))
        self.assertFalse(is_connection_error("unknown command: foo"))
        self.assertFalse(is_connection_error(None))


class TestProgressTrackingAndResultDelivery(unittest.TestCase):
    """Test transcript parsing, progress milestone extraction, and true result delivery to Slack."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.brain_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_fake_transcript(self, conversation_id: str, steps: list[dict]) -> Path:
        log_dir = self.brain_dir / conversation_id / ".system_generated" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = log_dir / "transcript_full.jsonl"
        with open(transcript_file, "w", encoding="utf-8") as f:
            for s in steps:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        return transcript_file

    def test_resolve_transcript_path(self):
        from hub.antigravity.result_delivery import resolve_transcript_path
        convo_id = "test-convo-resolve"
        self.assertIsNone(resolve_transcript_path(convo_id, brain_root=self.brain_dir))

        self._create_fake_transcript(convo_id, [{"step_index": 0, "type": "USER_INPUT"}])
        resolved = resolve_transcript_path(convo_id, brain_root=self.brain_dir)
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.name.endswith(".jsonl"))

    def test_get_latest_step_index(self):
        from hub.antigravity.result_delivery import get_latest_step_index
        convo_id = "test-convo-steps"
        steps = [
            {"step_index": 0, "type": "USER_INPUT"},
            {"step_index": 5, "type": "PLANNER_RESPONSE"},
            {"step_index": 9, "type": "PLANNER_RESPONSE"},
        ]
        self._create_fake_transcript(convo_id, steps)
        latest_idx = get_latest_step_index(convo_id, brain_root=self.brain_dir)
        self.assertEqual(latest_idx, 9)

    def test_parse_transcript_events_tool_actions_and_result(self):
        from hub.antigravity.result_delivery import parse_transcript_events
        convo_id = "test-convo-weather"
        steps = [
            {"step_index": 0, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "上海天气"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"toolAction": "Checking weather", "toolSummary": "Weather in Shanghai"},
                    }
                ],
            },
            {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "content": "Sunny 28C"},
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "上海今日晴天，当前气温 28°C，体感舒适。",
            },
        ]
        transcript_file = self._create_fake_transcript(convo_id, steps)
        actions, content, is_done, has_error = parse_transcript_events(transcript_file, start_step=0)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0], "Checking weather")
        self.assertTrue(is_done)
        self.assertFalse(has_error)
        self.assertIn("上海今日晴天", content)

    def test_thread_notifier_notify_progress_and_result_delivery(self):
        notifier = ThreadNotifier(token="xoxb-mock-token")
        notifier.post_thread_message = MagicMock(return_value=True)

        # Progress notification
        ok_prog = notifier.notify_progress("C0C1B86AMCN", "1789200000.100", "正在查询天气数据", elapsed_seconds=5.2)
        self.assertTrue(ok_prog)
        notifier.post_thread_message.assert_called_once()
        prog_call_text = notifier.post_thread_message.call_args[0][2]
        self.assertIn("[执行中 · 步骤进展]", prog_call_text)
        self.assertIn("正在查询天气数据", prog_call_text)
        self.assertIn("5.2s", prog_call_text)

        # Result delivery notification
        notifier.post_thread_message.reset_mock()
        ok_deliv = notifier.notify_result_delivery(
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            conversation_id="conv-weather-9999",
            content="上海现在的天气是晴天，28°C。",
            elapsed_seconds=18.4,
            is_follow_up=False,
        )
        self.assertTrue(ok_deliv)
        notifier.post_thread_message.assert_called_once()
        deliv_text = notifier.post_thread_message.call_args[0][2]
        self.assertIn("[已完成 · 结果交付]", deliv_text)
        self.assertIn("上海现在的天气是晴天", deliv_text)
        self.assertIn("conv-weather-9999", deliv_text)

    def test_watch_and_deliver_result_end_to_end(self):
        from hub.antigravity.result_delivery import watch_and_deliver_result
        convo_id = "test-convo-async-e2e"
        steps = [
            {"step_index": 0, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "阳朔天气"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {"toolAction": "Checking weather"},
                    }
                ],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "阳朔今日晴朗，25°C。",
            },
        ]
        self._create_fake_transcript(convo_id, steps)

        mock_notifier = MagicMock()
        mock_notifier.notify_progress = MagicMock(return_value=True)
        mock_notifier.notify_result_delivery = MagicMock(return_value=True)

        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        async def _run():
            res = await watch_and_deliver_result(
                notifier=mock_notifier,
                channel="C0C1B86AMCN",
                thread_ts="1789200000.100",
                conversation_id=convo_id,
                task_id="tsk_async_test",
                start_time=time.time(),
                is_follow_up=False,
                start_step=0,
                max_wait_seconds=5.0,
                poll_interval=0.05,
                brain_root=self.brain_dir,
                broker=mock_broker,
            )
            self.assertTrue(res["delivered"])
            self.assertEqual(res["conversation_id"], convo_id)
            mock_notifier.notify_result_delivery.assert_called_once()
            mock_broker.publish.assert_called()

        asyncio.run(_run())

    def test_follow_up_start_step_ignores_prior_completed_step(self):
        from hub.antigravity.result_delivery import parse_transcript_events
        convo_id = "test-convo-followup-start-step"
        steps = [
            {"step_index": 0, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "任务1初始请求"},
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "任务1已完成的回复内容",
            },
        ]
        transcript_file = self._create_fake_transcript(convo_id, steps)

        # In a follow-up, latest_step index was 1.
        # With start_step = latest_step + 1 = 2:
        actions, content, is_done, has_error = parse_transcript_events(transcript_file, start_step=2)
        self.assertIsNone(content)
        self.assertFalse(is_done)

        # Later, follow-up steps (2: USER, 3: MODEL) are appended
        steps.extend([
            {"step_index": 2, "source": "USER", "type": "USER_INPUT", "status": "DONE", "content": "任务2追问请求"},
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "任务2追问的全新回复内容",
            },
        ])
        transcript_file = self._create_fake_transcript(convo_id, steps)
        actions, content, is_done, has_error = parse_transcript_events(transcript_file, start_step=2)
        self.assertEqual(content, "任务2追问的全新回复内容")
        self.assertTrue(is_done)

    def test_temporal_guard_rejects_stale_created_at(self):
        from hub.antigravity.result_delivery import parse_transcript_events
        convo_id = "test-convo-stale-temporal"
        now = time.time()
        stale_time = now - 60.0
        steps = [
            {
                "step_index": 0,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": datetime.fromtimestamp(stale_time, tz=timezone.utc).isoformat(),
                "content": "旧回合残留响应内容",
            }
        ]
        transcript_file = self._create_fake_transcript(convo_id, steps)

        # Without temporal guard (min_created_at=None), step 0 is parsed
        _, old_content, old_done, _ = parse_transcript_events(transcript_file, start_step=0, min_created_at=None)
        self.assertTrue(old_done)
        self.assertEqual(old_content, "旧回合残留响应内容")

        # With temporal guard (min_created_at=now), the stale step is rejected
        actions, content, is_done, has_error = parse_transcript_events(
            transcript_file, start_step=0, min_created_at=now
        )
        self.assertIsNone(content)
        self.assertFalse(is_done)

    def test_temporal_guard_clock_skew_tolerance(self):
        from hub.antigravity.result_delivery import parse_transcript_events
        convo_id = "test-convo-clock-skew"
        start_time = time.time()
        # Event 0.5s before start_time: within 1.0s tolerance -> accepted
        steps_ok = [
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": start_time - 0.5,
                "content": "容差内有效响应",
            }
        ]
        file_ok = self._create_fake_transcript(convo_id, steps_ok)
        _, content_ok, done_ok, _ = parse_transcript_events(file_ok, start_step=0, min_created_at=start_time)
        self.assertTrue(done_ok)
        self.assertEqual(content_ok, "容差内有效响应")

        # Event 2.0s before start_time: strictly older than start_time - 1.0 -> rejected
        steps_stale = [
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": start_time - 2.0,
                "content": "超限过期响应",
            }
        ]
        file_stale = self._create_fake_transcript(convo_id, steps_stale)
        _, content_stale, done_stale, _ = parse_transcript_events(file_stale, start_step=0, min_created_at=start_time)
        self.assertFalse(done_stale)
        self.assertIsNone(content_stale)

    def test_active_watcher_cancellation(self):
        from hub.antigravity.result_delivery import (
            _ACTIVE_WATCHERS,
            cancel_active_watcher,
            watch_and_deliver_result,
        )
        convo_id = "test-convo-cancel-watcher"
        mock_notifier = MagicMock()

        async def _run():
            # Launch watcher 1
            task1 = asyncio.create_task(
                watch_and_deliver_result(
                    notifier=mock_notifier,
                    channel="C0C1B86AMCN",
                    thread_ts="1789200000.100",
                    conversation_id=convo_id,
                    task_id="tsk_w1",
                    start_time=time.time(),
                    max_wait_seconds=10.0,
                    poll_interval=0.05,
                    brain_root=self.brain_dir,
                )
            )
            await asyncio.sleep(0.02)
            self.assertIn(convo_id, _ACTIVE_WATCHERS)
            self.assertIs(_ACTIVE_WATCHERS[convo_id], task1)
            self.assertFalse(task1.done())

            # Launch watcher 2 for same convo_id -> should cancel watcher 1
            task2 = asyncio.create_task(
                watch_and_deliver_result(
                    notifier=mock_notifier,
                    channel="C0C1B86AMCN",
                    thread_ts="1789200000.100",
                    conversation_id=convo_id,
                    task_id="tsk_w2",
                    start_time=time.time(),
                    max_wait_seconds=10.0,
                    poll_interval=0.05,
                    brain_root=self.brain_dir,
                )
            )
            await asyncio.sleep(0.02)
            # Task 1 must be cancelled or done
            self.assertTrue(task1.cancelled() or task1.done())
            self.assertIs(_ACTIVE_WATCHERS[convo_id], task2)

            # Test cancel_active_watcher helper
            cancelled = cancel_active_watcher(convo_id)
            self.assertTrue(cancelled)
            await asyncio.sleep(0.02)
            self.assertTrue(task2.cancelled() or task2.done())
            self.assertNotIn(convo_id, _ACTIVE_WATCHERS)

        asyncio.run(_run())

    def test_parse_timestamp_comprehensive(self):
        from hub.antigravity.result_delivery import _parse_timestamp

        # Numeric int / float
        self.assertEqual(_parse_timestamp(1789200000), 1789200000.0)
        self.assertEqual(_parse_timestamp(1789200000.5), 1789200000.5)
        # String float
        self.assertEqual(_parse_timestamp("1789200000.5"), 1789200000.5)
        # UTC ISO string
        self.assertEqual(_parse_timestamp("1970-01-01T00:00:10Z"), 10.0)
        # Naive ISO string (local) matches local timestamp
        now = time.time()
        naive_str = datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S")
        parsed = _parse_timestamp(naive_str)
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed, int(now), delta=2.0)
        # Invalid / Empty / None
        self.assertIsNone(_parse_timestamp(None))
        self.assertIsNone(_parse_timestamp(""))
        self.assertIsNone(_parse_timestamp("invalid-date-format"))

    def test_get_latest_step_index_empty_and_missing(self):
        from hub.antigravity.result_delivery import get_latest_step_index

        # Missing conversation returns -1
        self.assertEqual(get_latest_step_index("nonexistent-convo-xyz", brain_root=self.brain_dir), -1)

        # Empty transcript file returns -1
        convo_id = "test-convo-empty"
        self._create_fake_transcript(convo_id, [])
        self.assertEqual(get_latest_step_index(convo_id, brain_root=self.brain_dir), -1)

        # Transcript with single step 0 returns 0
        convo_step0 = "test-convo-step0"
        self._create_fake_transcript(convo_step0, [{"step_index": 0}])
        self.assertEqual(get_latest_step_index(convo_step0, brain_root=self.brain_dir), 0)

    def test_watch_and_deliver_result_follow_up_e2e(self):
        from hub.antigravity.result_delivery import watch_and_deliver_result
        convo_id = "test-convo-followup-e2e"
        now = time.time()
        # Pre-populate turn 1 (steps 0 and 1) completed in the past
        steps = [
            {
                "step_index": 0,
                "source": "USER",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": datetime.fromtimestamp(now - 30, tz=timezone.utc).isoformat(),
                "content": "Turn 1 request",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": datetime.fromtimestamp(now - 28, tz=timezone.utc).isoformat(),
                "content": "Turn 1 completed response",
            },
            # Turn 2 (steps 2 and 3)
            {
                "step_index": 2,
                "source": "USER",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": datetime.fromtimestamp(now - 2, tz=timezone.utc).isoformat(),
                "content": "Turn 2 follow-up request",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": datetime.fromtimestamp(now - 1, tz=timezone.utc).isoformat(),
                "content": "Turn 2 follow-up new answer",
            },
        ]
        self._create_fake_transcript(convo_id, steps)

        mock_notifier = MagicMock()
        mock_notifier.notify_result_delivery = MagicMock(return_value=True)

        async def _run():
            res = await watch_and_deliver_result(
                notifier=mock_notifier,
                channel="C0C1B86AMCN",
                thread_ts="1789200000.100",
                conversation_id=convo_id,
                task_id="tsk_followup_e2e",
                start_time=now - 5,  # Follow-up started at now - 5
                is_follow_up=True,
                start_step=2,  # Must ignore steps 0 and 1
                max_wait_seconds=5.0,
                poll_interval=0.05,
                brain_root=self.brain_dir,
            )
            self.assertTrue(res["delivered"])
            mock_notifier.notify_result_delivery.assert_called_once()
            args, kwargs = mock_notifier.notify_result_delivery.call_args
            self.assertEqual(kwargs.get("content"), "Turn 2 follow-up new answer")
            self.assertTrue(kwargs.get("is_follow_up"))

        asyncio.run(_run())

    def test_watch_and_deliver_result_detects_step_0_activity(self):
        from hub.antigravity.result_delivery import watch_and_deliver_result
        convo_id = "test-convo-step0-activity"
        mock_notifier = MagicMock()
        mock_notifier.notify_result_delivery = MagicMock(return_value=True)

        # Start with empty transcript
        self._create_fake_transcript(convo_id, [])

        async def _run():
            task = asyncio.create_task(
                watch_and_deliver_result(
                    notifier=mock_notifier,
                    channel="C0C1B86AMCN",
                    thread_ts="1789200000.100",
                    conversation_id=convo_id,
                    task_id="tsk_step0",
                    start_time=time.time(),
                    is_follow_up=False,
                    start_step=0,
                    max_wait_seconds=5.0,
                    poll_interval=0.05,
                    brain_root=self.brain_dir,
                )
            )
            await asyncio.sleep(0.08)
            # Write step 0 and 1
            self._create_fake_transcript(
                convo_id,
                [
                    {"step_index": 0, "source": "USER", "type": "USER_INPUT", "status": "DONE"},
                    {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "content": "Done 0"},
                ],
            )
            res = await task
            self.assertTrue(res["delivered"])

        asyncio.run(_run())




class TestSlackFormatter(unittest.TestCase):
    """Unit tests for Slack mrkdwn formatting engine."""

    def test_markdown_to_slack_mrkdwn_bold_and_headers(self):
        from hub.antigravity.slack_formatter import markdown_to_slack_mrkdwn
        md = "# 主标题\n## 副标题\n**加粗文字** 与 __下划线加粗__"
        slack = markdown_to_slack_mrkdwn(md)
        self.assertIn("*主标题*", slack)
        self.assertIn("*副标题*", slack)
        self.assertIn("*加粗文字*", slack)
        self.assertIn("*下划线加粗*", slack)
        self.assertNotIn("**", slack)
        self.assertNotIn("##", slack)

    def test_markdown_to_slack_mrkdwn_bullets_and_lists(self):
        from hub.antigravity.slack_formatter import markdown_to_slack_mrkdwn
        md = "- 列表条目 1\n* 列表条目 2\n+ 列表条目 3"
        slack = markdown_to_slack_mrkdwn(md)
        self.assertIn("• 列表条目 1", slack)
        self.assertIn("• 列表条目 2", slack)
        self.assertIn("• 列表条目 3", slack)

    def test_markdown_to_slack_mrkdwn_links_and_italic(self):
        from hub.antigravity.slack_formatter import markdown_to_slack_mrkdwn
        md = "[访问官网](https://glintmuse.com) 与 _斜体重点_ 以及 ~~删除文本~~"
        slack = markdown_to_slack_mrkdwn(md)
        self.assertIn("<https://glintmuse.com|访问官网>", slack)
        self.assertIn("_斜体重点_", slack)
        self.assertIn("~删除文本~", slack)

    def test_markdown_to_slack_mrkdwn_code_preservation(self):
        from hub.antigravity.slack_formatter import markdown_to_slack_mrkdwn
        md = "测试保留代码块：\n```python\ndef test():\n    # **not bold**\n    return True\n```\n以及行内 `x = **y**`。"
        slack = markdown_to_slack_mrkdwn(md)
        self.assertIn("```python\ndef test():\n    # **not bold**\n    return True\n```", slack)
        self.assertIn("`x = **y**`", slack)

    def test_markdown_to_slack_mrkdwn_real_weather_payload(self):
        from hub.antigravity.slack_formatter import markdown_to_slack_mrkdwn
        md = """**【杭州 | 9月14日 气象资产与决策参考】**
*(基于高分辨率数值预报与体感实测建模)*

### 1. 核心气象矩阵 (Core Matrix)
- **温感矩阵**: 晨间微凉（约22-24°C），午后最高升至31-32°C，昼夜温差近10°C。
- **降水概率**: 全天多云到晴，降水概率 < 10%，基本无雨。
- **空气质量与紫外线**: AQI 优良（~35-45），午后紫外线等级达到7级（中等偏强）。"""
        slack = markdown_to_slack_mrkdwn(md)
        self.assertIn("*【杭州 | 9月14日 气象资产与决策参考】*", slack)
        self.assertIn("_(基于高分辨率数值预报与体感实测建模)_", slack)
        self.assertIn("*1. 核心气象矩阵 (Core Matrix)*", slack)
        self.assertIn("• *温感矩阵*: 晨间微凉", slack)
        self.assertNotIn("**", slack)
        self.assertNotIn("###", slack)


    def test_markdown_to_slack_mrkdwn_cjk_colons_and_commas(self):
        from hub.antigravity.slack_formatter import markdown_to_slack_mrkdwn
        md = "• **早晨**：局部有雨\n• **中午**：局部有雨\n**温度提示**：近期杭州气温舒适\n前文**加粗**后文\n**重点**，请留意\n【**重要通知**】"
        slack = markdown_to_slack_mrkdwn(md)
        self.assertIn("• *早晨*: 局部有雨", slack)
        self.assertIn("• *中午*: 局部有雨", slack)
        self.assertIn("*温度提示*: 近期杭州气温舒适", slack)
        self.assertIn("前文 *加粗* 后文", slack)
        self.assertIn("*重点*, 请留意", slack)
        self.assertIn("*【重要通知】*", slack)

    def test_markdown_to_slack_mrkdwn_real_hangzhou_2day_weather(self):
        from hub.antigravity.slack_formatter import markdown_to_slack_mrkdwn
        md = """为您查询到杭州近两天的天气预报如下：

### 📍 9月13日（星期日）
- **早晨**：局部有雨 🌦️ | 气温 26°C（体感 28°C）
- **中午**：局部有雨 🌦️ | 气温 29°C（体感 31°C）
- **傍晚**：有烟霾 🌫️ | 气温 26°C（体感 28°C）
- **夜间**：有烟霾 🌫️ | 气温 24°C（体感 26°C）
*今日白天可能伴有阵雨，夜间能见度略有下降，请注意携带雨具。*

**温度提示**：近期杭州气温较为舒适，在 24°C - 29°C 之间。"""
        slack = markdown_to_slack_mrkdwn(md)
        self.assertIn("• *早晨*: 局部有雨", slack)
        self.assertIn("• *中午*: 局部有雨", slack)
        self.assertIn("• *傍晚*: 有烟霾", slack)
        self.assertIn("• *夜间*: 有烟霾", slack)
        self.assertIn("*温度提示*: 近期杭州气温较为舒适", slack)
        self.assertIn("_今日白天可能伴有阵雨", slack)
        self.assertNotIn("**", slack)
        self.assertNotIn("###", slack)

    def test_notify_result_delivery_formats_mrkdwn(self):
        notifier = ThreadNotifier(token="xoxb-mock-token")
        notifier.post_thread_message = MagicMock(return_value=True)

        notifier.notify_result_delivery(
            channel="C0C1B86AMCN",
            thread_ts="1789200000.100",
            conversation_id="conv-formatted-1234",
            content="### 总结报告\n- **重要发现**: 准确率达到 **100%**",
            elapsed_seconds=12.5,
        )
        notifier.post_thread_message.assert_called_once()
        sent_msg = notifier.post_thread_message.call_args[0][2]
        self.assertIn("*总结报告*", sent_msg)
        self.assertIn("• *重要发现*: 准确率达到 *100%*", sent_msg)
        self.assertNotIn("**", sent_msg)
        self.assertNotIn("###", sent_msg)


if __name__ == "__main__":
    unittest.main()
