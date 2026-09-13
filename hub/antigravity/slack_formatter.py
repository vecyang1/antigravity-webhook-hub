"""
Antigravity Webhook Hub — Slack mrkdwn Formatting Engine
Converts standard CommonMark / GitHub Flavored Markdown into native Slack mrkdwn.

Slack mrkdwn rules & CJK boundary nuances:
- Bold: *text* (instead of **text** or __text__)
- Italic: _text_ (instead of *text* or _text_)
- Bold+Italic: *_text_* (instead of ***text***)
- Strikethrough: ~text~ (instead of ~~text~~)
- Inline code: `code` (preserved untouched)
- Code blocks: ```code``` (preserved untouched)
- Headings: # H1, ## H2 -> *H1*, *H2* (Slack lacks native heading tags)
- Bullet lists: - item, * item, + item -> • item (clean Unicode bullet avoiding delimiter collisions)
- Links: [label](url) -> <url|label>
- CJK boundary normalization:
  Slack's mrkdwn parser requires ASCII punctuation or whitespace boundaries.
  Fullwidth Chinese colons (：) or commas (，) touching closing formatting tokens (*, _)
  cause Slack to treat asterisks as literal characters.
  Normalizing `*早晨*：` -> `*早晨*: ` and ensuring whitespace between CJK and delimiters
  guarantees 100% reliable bolding in Slack across mobile and desktop.
"""

from __future__ import annotations

import re
import uuid

# CJK character pattern covering Chinese, Japanese, Korean
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def markdown_to_slack_mrkdwn(text: str) -> str:
    """
    Convert standard Markdown text to Slack mrkdwn format with CJK boundary hardening.
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

    # 6. Convert double-asterisk / double-underscore bold with CJK boundary normalization
    def _clean_bold_span(match: re.Match) -> str:
        prefix = match.group("prefix") or ""
        content = match.group("content").strip()
        suffix = match.group("suffix") or ""

        # Trailing colon inside bold: **早晨：** or **早晨:** -> *早晨*:
        if content.endswith("：") or content.endswith(":"):
            content = content[:-1].strip()
            suffix = ": "

        # Fullwidth colon as suffix
        if suffix == "：":
            suffix = ": "
        elif suffix.startswith("："):
            suffix = ": " + suffix[1:].lstrip()

        # Fullwidth comma as suffix
        if suffix == "，":
            suffix = ", "
        elif suffix.startswith("，"):
            suffix = ", " + suffix[1:].lstrip()

        # Fullwidth brackets around bold: 【**text**】 -> *【text】*
        if prefix == "【" and suffix == "】":
            return f"*【{content}】*"
        if prefix == "（" and suffix == "）":
            return f"(*{content}*)"

        # Whitespace injection when CJK character touches delimiters outside
        if prefix and CJK_PATTERN.match(prefix):
            prefix = prefix + " "
        if suffix and CJK_PATTERN.match(suffix[0]):
            suffix = " " + suffix

        return f"{prefix}*{content}*{suffix}"

    processed = re.sub(r"(?P<prefix>\S)?\*\*(?P<content>[^*\n]+?)\*\*(?P<suffix>\S)?", _clean_bold_span, processed)
    processed = re.sub(r"(?P<prefix>\S)?__(?P<content>[^_\n]+?)__(?P<suffix>\S)?", _clean_bold_span, processed)

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

    # 10. Post-pass CJK boundary hardening on single asterisks / underscores:
    # Handles cases where input already had single asterisks (e.g. *早晨*：)
    processed = re.sub(r"\*([^*\n:]+)[：:]\*", r"*\1*: ", processed)
    processed = re.sub(r"\*\s*：\s*", "*: ", processed)
    processed = re.sub(r"_\s*：\s*", "_: ", processed)
    processed = re.sub(r"~\s*：\s*", "~: ", processed)
    processed = re.sub(r"\*\s*，\s*", "*, ", processed)
    processed = re.sub(r"_\s*，\s*", "_, ", processed)
    processed = re.sub(r"\*:\s{2,}", "*: ", processed)
    processed = re.sub(r"\*,\s{2,}", "*, ", processed)

    # 11. Restore shielded code blocks and inline code
    for i, code_snippet in enumerate(code_store):
        processed = processed.replace(f"{code_prefix}{i}\x00", code_snippet)

    return processed
