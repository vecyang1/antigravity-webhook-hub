"""
Antigravity Webhook Hub — Slack mrkdwn Formatting Engine
Converts standard CommonMark / GitHub Flavored Markdown into native Slack mrkdwn.

Slack mrkdwn rules:
- Bold: *text* (instead of **text** or __text__)
- Italic: _text_ (instead of *text* or _text_)
- Bold+Italic: *_text_* (instead of ***text***)
- Strikethrough: ~text~ (instead of ~~text~~)
- Inline code: `code` (preserved untouched)
- Code blocks: ```code``` (preserved untouched)
- Headings: # H1, ## H2 -> *H1*, *H2* (Slack lacks native heading tags)
- Bullet lists: - item, * item, + item -> • item (clean Unicode bullet avoiding delimiter collisions)
- Links: [label](url) -> <url|label>
"""

from __future__ import annotations

import re
import uuid


def markdown_to_slack_mrkdwn(text: str) -> str:
    """
    Convert standard Markdown text to Slack mrkdwn format.
    Preserves code blocks and inline code untouched.
    """
    if not text or not isinstance(text, str):
        return ""

    # 1. Shield code blocks and inline code with collision-free placeholders
    code_store: list[str] = []
    token_seed = uuid.uuid4().hex[:8]
    code_prefix = f"\x00SLACK_CODE_{token_seed}_"

    def _shield_code(match: re.Match) -> str:
        idx = len(code_store)
        code_store.append(match.group(0))
        return f"{code_prefix}{idx}\x00"

    # Multi-line code blocks ``` ... ```
    processed = re.sub(r"```.*?```", _shield_code, text, flags=re.DOTALL)
    # Inline code ` ... `
    processed = re.sub(r"`[^`\n]+`", _shield_code, processed)

    # 2. Convert Markdown links: [label](url) -> <url|label>
    processed = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"<\2|\1>", processed)

    # 3. Normalize bullet lists at line starts: -, *, + -> •
    processed = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", processed, flags=re.MULTILINE)

    # 4. Convert bold-italic: ***text*** or ___text___ -> *_text_*
    processed = re.sub(r"\*\*\*([^*\n]+)\*\*\*", r"*_\1_*", processed)
    processed = re.sub(r"___([^_]+)___", r"*_\1_*", processed)

    # 5. Convert single-asterisk italic before double-asterisk bold:
    # Match *italic* where * is not preceded or followed by another *
    processed = re.sub(r"(?<!\*)\*([^*\n\s](?:[^*\n]*[^*\n\s])?)\*(?!\*)", r"_\1_", processed)

    # 6. Convert double-asterisk / double-underscore bold: **text** / __text__ -> *text*
    processed = re.sub(r"\*\*([^*\n]+)\*\*", r"*\1*", processed)
    processed = re.sub(r"__([^_]+)__", r"*\1*", processed)

    # 7. Convert Markdown headings (# H1, ## H2, etc.) to bold lines (*H1*, *H2*)
    def _format_heading(match: re.Match) -> str:
        indent = match.group(1)
        heading_text = match.group(2).strip()
        # Clean redundant surrounding asterisks inside the heading
        heading_text = re.sub(r"^\*+|\*+$", "", heading_text).strip()
        return f"{indent}*{heading_text}*"

    processed = re.sub(r"^(\s*)#{1,6}\s+(.+)$", _format_heading, processed, flags=re.MULTILINE)

    # 8. Convert strikethrough: ~~text~~ -> ~text~
    processed = re.sub(r"~~([^~\n]+)~~", r"~\1~", processed)

    # 9. Normalize horizontal rules: ---, ***, ___ -> ──────────────────
    processed = re.sub(r"^\s*([-*_]){3,}\s*$", r"──────────────────", processed, flags=re.MULTILINE)

    # 10. Restore shielded code blocks and inline code
    for i, code_snippet in enumerate(code_store):
        processed = processed.replace(f"{code_prefix}{i}\x00", code_snippet)

    return processed
