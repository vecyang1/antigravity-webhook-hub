"""
Antigravity Webhook Hub — Antigravity Agent & Thread Data Contracts
Type-safe dataclasses for mobile intake, multi-modal prompts, session links, and thread milestones.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ThreadMilestone(str, Enum):
    """Execution lifecycle milestones for Slack thread transparency."""

    COLLECTED = "collected"       # 任务已采集入库，准备调度
    IN_PROGRESS = "in_progress"   # 任务处理中，Agent会话已启动
    DONE = "done"                 # 任务完成，会话建立成功
    FOLLOW_UP = "follow_up"       # 收到追问并已同步至会话上下文
    FAILED = "failed"             # 任务执行异常


@dataclass(slots=True)
class AntigravityTaskPayload:
    """Normalized payload representing an incoming task from mobile or external webhook."""

    text: str = ""
    voice_transcript: Optional[str] = None
    voice_transcribed: bool = False
    files: list[dict[str, Any]] = field(default_factory=list)
    channel: Optional[str] = None
    ts: Optional[str] = None
    thread_ts: Optional[str] = None
    user: Optional[str] = None
    source: str = "slack"
    model_tier: str = "pro"
    title: Optional[str] = None
    slash_commands: list[str] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)
    is_follow_up: bool = False
    downloaded_images: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def thread_key(self) -> str:
        """Derive the canonical thread key for session mapping."""
        root_ts = self.thread_ts or self.ts or ""
        ch = self.channel or "default"
        return f"{ch}:{root_ts}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "voice_transcript": self.voice_transcript,
            "voice_transcribed": self.voice_transcribed,
            "files": self.files,
            "channel": self.channel,
            "ts": self.ts,
            "thread_ts": self.thread_ts,
            "user": self.user,
            "source": self.source,
            "model_tier": self.model_tier,
            "title": self.title,
            "slash_commands": self.slash_commands,
            "active_skills": self.active_skills,
            "is_follow_up": self.is_follow_up,
            "downloaded_images": self.downloaded_images,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AntigravityTaskPayload:
        raw_text = str(data.get("text") or "").strip()
        voice_tx = data.get("voice_transcript") or data.get("audio_transcript")
        if voice_tx:
            voice_tx = str(voice_tx).strip()

        # Files normalization
        raw_files = data.get("files") or data.get("audio_files") or []
        files: list[dict[str, Any]] = []
        if isinstance(raw_files, list):
            for f in raw_files:
                if isinstance(f, dict):
                    files.append(f)

        ts = str(data.get("ts") or data.get("event_ts") or "")
        thread_ts = data.get("thread_ts")
        if thread_ts:
            thread_ts = str(thread_ts).strip()

        # Determine if this is a follow-up 追问
        is_follow_up = bool(thread_ts and thread_ts != ts)

        return cls(
            text=raw_text,
            voice_transcript=voice_tx,
            voice_transcribed=bool(data.get("voice_transcribed") or voice_tx),
            files=files,
            channel=data.get("channel"),
            ts=ts or None,
            thread_ts=thread_ts or None,
            user=data.get("user"),
            source=data.get("source") or "slack",
            model_tier=str(data.get("model_tier") or data.get("model") or "pro").lower(),
            title=data.get("title"),
            slash_commands=list(data.get("slash_commands") or []),
            active_skills=list(data.get("active_skills") or []),
            is_follow_up=is_follow_up,
            downloaded_images=list(data.get("downloaded_images") or []),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class SessionThreadRecord:
    """Database record mapping a communication thread to an Antigravity agent session."""

    thread_key: str
    channel_id: str
    root_ts: str
    task_id: str
    conversation_id: str
    source: str = "slack"
    model_tier: str = "pro"
    title: Optional[str] = None
    status: str = "active"
    last_active_at: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_key": self.thread_key,
            "channel_id": self.channel_id,
            "root_ts": self.root_ts,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "source": self.source,
            "model_tier": self.model_tier,
            "title": self.title,
            "status": self.status,
            "last_active_at": self.last_active_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> SessionThreadRecord:
        d = dict(row)
        return cls(
            thread_key=d["thread_key"],
            channel_id=d["channel_id"],
            root_ts=d["root_ts"],
            task_id=d["task_id"],
            conversation_id=d["conversation_id"],
            source=d.get("source", "slack"),
            model_tier=d.get("model_tier", "pro"),
            title=d.get("title"),
            status=d.get("status", "active"),
            last_active_at=d.get("last_active_at", ""),
            created_at=d.get("created_at", ""),
        )


@dataclass(slots=True)
class QuotaCooldownInfo:
    """Metadata tracking quota exhaustion and reset cooldown."""

    conversation_id: str
    resets_in_seconds: float
    reset_timestamp: Optional[str] = None
    reason: str = "QUOTA_EXHAUSTED"
    error_id: Optional[str] = None
    cooldown_until: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "resets_in_seconds": self.resets_in_seconds,
            "reset_timestamp": self.reset_timestamp,
            "reason": self.reason,
            "error_id": self.error_id,
            "cooldown_until": self.cooldown_until,
        }


@dataclass(slots=True)
class ConversationScheduleRecord:
    """Database record tracking in-memory recurring cron timers within an Antigravity conversation."""

    conversation_id: str
    cron_expression: str
    prompt: str
    expected_interval_seconds: int
    last_trigger_at: float
    status: str = "active"
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "cron_expression": self.cron_expression,
            "prompt": self.prompt,
            "expected_interval_seconds": self.expected_interval_seconds,
            "last_trigger_at": self.last_trigger_at,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> ConversationScheduleRecord:
        d = dict(row)
        return cls(
            conversation_id=d["conversation_id"],
            cron_expression=d["cron_expression"],
            prompt=d.get("prompt", ""),
            expected_interval_seconds=int(d.get("expected_interval_seconds", 1800)),
            last_trigger_at=float(d.get("last_trigger_at", 0.0)),
            status=d.get("status", "active"),
            created_at=d.get("created_at", ""),
        )
