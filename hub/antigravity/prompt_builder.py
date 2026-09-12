"""
Antigravity Webhook Hub — Prompt & Directive Construction Engine
Parses slash commands, merges voice transcripts & text, references multi-image assets,
and injects domain directives (/psychological-copywriter, /strategic-compact, /boost, /goal).
"""

from __future__ import annotations

import re
from typing import Optional

from hub.antigravity.models import AntigravityTaskPayload

# Domain Skill Directives
PSYCHOLOGICAL_COPYWRITER_DIRECTIVE = """
[技能指令：心理文案引擎 / Psychological Copywriter]
请在输出内容与方案设计中深度遵循高转化心理文案原则：
1. 高端价值锚定（Premium Value Anchoring）：强化工艺、稀缺性、设计哲学与独特审美，建立高溢价心智。
2. 心理账户重构（Mental Accounting Reframing）：将用户感知从“花销/成本”重构为“对自我成长的战略投资与高品质犒赏”。
3. 转化结构（Conversion Architecture）：收益先行，直击核心痛点与心理阻抗，以坚实证据打消疑虑，提供明确行动指引。
""".strip()

STRATEGIC_COMPACT_DIRECTIVE = """
[技能指令：战略精简契约 / Strategic Compact]
请在分析与输出中严格执行极简高密原则：
1. 极致信噪比：结论先行，直出关键判断、架构决策与行动项。
2. 杜绝空话废话：严禁客套寒暄、套话、AI味模板用语和无意义的过渡句。
3. Token 经济学：以最小字数承载最大信息量，保持骨干清晰、易于执行。
""".strip()

BOOST_DIRECTIVE = """
[模式指令：加速执行 / Boost Mode]
以高自主性极速模式推进，端到端闭环交付，遇阻碍自主三步自愈与降级，避免停顿等待。
""".strip()

GOAL_DIRECTIVE = """
[模式指令：目标锚定 / Goal Mode]
严格以可验证的最终完成定义（Definition of Done）为核心牵引，前置交付标准，拒绝半成品。
""".strip()

TEAMWORK_PREVIEW_DIRECTIVE = """
[协作指令：团队编排与预览 / Teamwork Preview]
梳理多智能体与团队协作边界，输出清晰的责任所有者与就绪证明（Readiness Proof）。
""".strip()

# Slash command matcher regex
SLASH_COMMAND_REGEX = re.compile(
    r'(?:^|(?<=[\s,，。、；;]))/(psychological-copywriter|strategic-compact|boost|goal|teamwork-preview|teamwork|play)\b',
    re.IGNORECASE,
)


def extract_slash_commands(raw_text: str) -> tuple[str, list[str], list[str]]:
    """
    Extract slash commands from raw text.
    Returns:
      (cleaned_text, extracted_command_names, active_directive_blocks)
    """
    if not raw_text:
        return "", [], []

    found_commands: list[str] = []
    directives: list[str] = []

    def _replace_match(match: re.Match) -> str:
        cmd_raw = match.group(1).lower()
        if cmd_raw == "teamwork":
            cmd_raw = "teamwork-preview"
        if cmd_raw not in found_commands:
            found_commands.append(cmd_raw)
        return ""

    cleaned_text = SLASH_COMMAND_REGEX.sub(_replace_match, raw_text)
    # Clean up redundant spaces and trailing punctuation
    cleaned_text = re.sub(r'[ \t]+', ' ', cleaned_text).strip()

    # Map commands to corresponding directives
    for cmd in found_commands:
        if cmd == "psychological-copywriter":
            directives.append(PSYCHOLOGICAL_COPYWRITER_DIRECTIVE)
        elif cmd == "strategic-compact":
            directives.append(STRATEGIC_COMPACT_DIRECTIVE)
        elif cmd == "boost":
            directives.append(BOOST_DIRECTIVE)
        elif cmd == "goal":
            directives.append(GOAL_DIRECTIVE)
        elif cmd == "teamwork-preview":
            directives.append(TEAMWORK_PREVIEW_DIRECTIVE)

    return cleaned_text, found_commands, directives


def build_antigravity_prompt(
    payload: AntigravityTaskPayload,
    downloaded_images: Optional[list[str]] = None,
) -> str:
    """
    Synthesize complete multi-modal task prompt for Antigravity new-conversation.
    """
    raw_text = payload.text or ""
    clean_text, commands, directives = extract_slash_commands(raw_text)

    # Combine with any already declared slash commands or active skills
    all_commands = list(dict.fromkeys(payload.slash_commands + commands))
    all_directives = list(directives)
    if "psychological-copywriter" in all_commands and PSYCHOLOGICAL_COPYWRITER_DIRECTIVE not in all_directives:
        all_directives.append(PSYCHOLOGICAL_COPYWRITER_DIRECTIVE)
    if "strategic-compact" in all_commands and STRATEGIC_COMPACT_DIRECTIVE not in all_directives:
        all_directives.append(STRATEGIC_COMPACT_DIRECTIVE)
    if "boost" in all_commands and BOOST_DIRECTIVE not in all_directives:
        all_directives.append(BOOST_DIRECTIVE)
    if "goal" in all_commands and GOAL_DIRECTIVE not in all_directives:
        all_directives.append(GOAL_DIRECTIVE)

    # Core user intent: text + voice transcript
    prompt_parts: list[str] = []

    voice_tx = (payload.voice_transcript or "").strip()
    if clean_text and voice_tx and voice_tx not in clean_text:
        prompt_parts.append(f"{clean_text}\n\n[语音转录 / Voice Transcript]:\n{voice_tx}")
    elif voice_tx and not clean_text:
        prompt_parts.append(f"[语音输入 / Voice Input]:\n{voice_tx}")
    elif clean_text:
        prompt_parts.append(clean_text)
    elif payload.files:
        prompt_parts.append("请分析并处理随附的图片与素材附件。")
    else:
        prompt_parts.append("执行 Antigravity 任务")

    # Image attachments
    images = downloaded_images or payload.downloaded_images
    if images:
        img_section = [f"\n[本地图片素材 / Attached Images ({len(images)})]:"]
        for p in images:
            img_section.append(f"- {p}")
        img_section.append("请在理解任务和给出结论时，查阅并结合以上图片内容。")
        prompt_parts.append("\n".join(img_section))

    # Active directives
    if all_directives:
        prompt_parts.append("\n\n" + "\n\n".join(all_directives))

    return "\n\n".join(prompt_parts).strip()


def build_follow_up_prompt(
    payload: AntigravityTaskPayload,
    downloaded_images: Optional[list[str]] = None,
) -> str:
    """
    Construct prompt for follow-up inquiry (追问) into an existing Antigravity conversation.
    """
    raw_text = payload.text or ""
    clean_text, commands, directives = extract_slash_commands(raw_text)

    prompt_parts: list[str] = ["[用户追问 / User Follow-up]:"]

    voice_tx = (payload.voice_transcript or "").strip()
    if clean_text and voice_tx and voice_tx not in clean_text:
        prompt_parts.append(f"{clean_text}\n\n[语音追问]: {voice_tx}")
    elif voice_tx and not clean_text:
        prompt_parts.append(f"[语音追问]: {voice_tx}")
    elif clean_text:
        prompt_parts.append(clean_text)
    else:
        prompt_parts.append("（用户补充了新的素材附件，请根据上下文结合分析）")

    images = downloaded_images or payload.downloaded_images
    if images:
        img_section = [f"\n[追问附带图片 ({len(images)})]:"]
        for p in images:
            img_section.append(f"- {p}")
        prompt_parts.append("\n".join(img_section))

    if directives:
        prompt_parts.append("\n" + "\n\n".join(directives))

    return "\n\n".join(prompt_parts).strip()
