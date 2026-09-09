"""
Antigravity Webhook Hub — Contact Review Engine
Evaluates incoming contact information against live Notion CRM candidates.
Determines intelligent reconciliation verdict: NO_CHANGE, SUPPLEMENT, CORRECT, MERGE, CREATE.
Constructs strictly valid Notion property payloads and audit note blocks.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from hub.contact_review.models import (
    CandidateMatch,
    ContactInput,
    FieldDiff,
    FieldDiffAction,
    ReviewResult,
    ReviewVerdict,
    format_social_url,
    is_placeholder_name,
    normalize_email_address,
    normalize_phone_digits,
    normalize_url_string,
)
from hub.contact_review.notion_client import (
    make_bullet_block,
    make_date,
    make_email,
    make_heading_block,
    make_paragraph_block,
    make_rich_text,
    make_title,
    make_url,
)

logger = logging.getLogger("hub.contact_review.engine")

MIN_CANDIDATE_SCORE = 40
STRONG_MATCH_SCORE = 70


def property_mapping_for_field(field_name: str, value: Any) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Map contact/diff field name to Notion property name and payload object."""
    if not value or not str(value).strip():
        return None, None
    f = field_name.lower().strip()
    v = str(value).strip()

    if f in ("phone", "phone_number"):
        return "Phone", make_rich_text(v)
    elif f == "email":
        return "Email", make_email(v)
    elif f == "company":
        return "Company", make_rich_text(v)
    elif f in ("title", "role"):
        return "Title", make_rich_text(v)
    elif f == "city":
        return "City", make_rich_text(v)
    elif f == "country":
        return "Country", make_rich_text(v)
    elif f == "birthday":
        return "Birthday", make_date(v)
    elif f in ("url", "link", "website"):
        return "URL", make_url(v)
    elif f in ("entity", "relationship"):
        return "Entity", make_rich_text(v)
    elif f in ("notes", "note", "important_info"):
        return "Note", make_rich_text(v)
    elif f == "telegram":
        return "Telegram", make_url(format_social_url("telegram", v))
    elif f == "wechat":
        return "WeChat", make_url(format_social_url("wechat", v))
    elif f == "linkedin":
        return "LinkedIn", make_url(format_social_url("linkedin", v))
    elif f in ("twitter", "x"):
        return "Twitter/X", make_url(format_social_url("twitter", v))
    elif f == "line":
        return "LINE", make_url(format_social_url("line", v))
    elif f == "instagram":
        return "Instagram", make_url(format_social_url("instagram", v))
    elif f in ("name", "full_name"):
        return "Full Name", make_title(v)
    elif f in ("romaji_name", "romaji"):
        return "Romaji Name", make_rich_text(v)
    elif f in ("romaji_source_name", "romaji_source"):
        return "Romaji Source Name", make_rich_text(v)
    return None, None


class ContactReviewEngine:
    """Core evaluation and reconciliation engine for contact records."""

    def evaluate(
        self,
        contact: ContactInput,
        candidates: list[CandidateMatch],
    ) -> ReviewResult:
        """
        Evaluate candidate matches against contact input and decide the reconciliation verdict.
        """
        # Filter candidates above minimum threshold
        valid_candidates = [c for c in candidates if c.score >= MIN_CANDIDATE_SCORE]
        high_matches = [c for c in valid_candidates if c.score >= STRONG_MATCH_SCORE]

        # Case 1: Zero Candidates -> CREATE
        if not valid_candidates:
            diffs = []
            for fname in ("name", "phone", "email", "company", "title", "city", "country", "birthday", "url", "entity", "notes"):
                val = getattr(contact, fname, "")
                if val:
                    diffs.append(
                        FieldDiff(
                            field_name=fname,
                            action=FieldDiffAction.SUPPLEMENT,
                            old_value=None,
                            new_value=val,
                            explanation=f"New field '{fname}' for created contact",
                        )
                    )
            for sname in ("telegram", "wechat", "linkedin", "twitter", "line", "instagram"):
                val = contact.get_social(sname)
                if val:
                    diffs.append(
                        FieldDiff(
                            field_name=sname,
                            action=FieldDiffAction.SUPPLEMENT,
                            old_value=None,
                            new_value=val,
                            explanation=f"New social handle '{sname}' for created contact",
                        )
                    )

            return ReviewResult(
                verdict=ReviewVerdict.CREATE,
                target_page_id=None,
                target_page_url=None,
                target_name=contact.name,
                confidence_score=100,
                diffs=diffs,
                candidates=[],
                explanation=(
                    f"No matching contact found in Notion CRM for '{contact.name}'. "
                    "Action: Create new person record."
                ),
            )

        # Case 2: Multiple Strong Candidates -> MERGE
        if len(high_matches) >= 2:
            canonical = high_matches[0]
            secondaries = high_matches[1:]

            merge_diffs = []
            merge_props = [
                ("Phone", "phone"),
                ("Email", "email"),
                ("Company", "company"),
                ("Title", "title"),
                ("City", "city"),
                ("Country", "country"),
                ("Birthday", "birthday"),
                ("URL", "url"),
                ("Entity", "entity"),
                ("Note", "notes"),
                ("Telegram", "telegram"),
                ("WeChat", "wechat"),
                ("LinkedIn", "linkedin"),
                ("Twitter/X", "twitter"),
                ("LINE", "line"),
                ("Instagram", "instagram"),
            ]

            for notion_prop, prop_key in merge_props:
                can_val = canonical.get_property_plain_text(notion_prop)
                inp_val = contact.get_social(prop_key) if prop_key in ("telegram", "wechat", "linkedin", "twitter", "line", "instagram") else getattr(contact, prop_key, "")

                sec_val = ""
                for sec in secondaries:
                    sv = sec.get_property_plain_text(notion_prop)
                    if sv:
                        sec_val = sv
                        break

                effective_val = inp_val or sec_val
                if effective_val and not can_val:
                    merge_diffs.append(
                        FieldDiff(
                            field_name=prop_key,
                            action=FieldDiffAction.SUPPLEMENT,
                            old_value=None,
                            new_value=effective_val,
                            explanation=f"Merged '{notion_prop}' from secondary record into canonical",
                        )
                    )
                elif effective_val and can_val and effective_val.strip().lower() != can_val.strip().lower():
                    merge_diffs.append(
                        FieldDiff(
                            field_name=prop_key,
                            action=FieldDiffAction.CONFLICT,
                            old_value=can_val,
                            new_value=effective_val,
                            explanation=f"Conflicting '{notion_prop}' preserved in notes history",
                        )
                    )

            # Name resolution in MERGE: If canonical has placeholder title, supersede with verified real name
            target_name = canonical.page_name
            if is_placeholder_name(target_name):
                verified_name = ""
                if not is_placeholder_name(contact.name):
                    verified_name = contact.name
                else:
                    for sec in secondaries:
                        if not is_placeholder_name(sec.page_name):
                            verified_name = sec.page_name
                            break
                if verified_name:
                    target_name = verified_name
                    merge_diffs.append(
                        FieldDiff(
                            field_name="name",
                            action=FieldDiffAction.CORRECT,
                            old_value=canonical.page_name,
                            new_value=verified_name,
                            explanation=f"Superseding placeholder title '{canonical.page_name}' with verified real name '{verified_name}' during merge",
                        )
                    )

            return ReviewResult(
                verdict=ReviewVerdict.MERGE,
                target_page_id=canonical.page_id,
                target_page_url=canonical.page_url,
                target_name=target_name,
                confidence_score=canonical.score,
                diffs=merge_diffs,
                candidates=high_matches,
                explanation=(
                    f"Found multiple candidate records in Notion ('{canonical.page_name}' score={canonical.score} "
                    f"and '{secondaries[0].page_name}' score={secondaries[0].score}). "
                    "Action: Merge profiles into canonical record."
                ),
            )

        # Case 3: Single Primary Candidate -> Compare Fields (NO_CHANGE vs SUPPLEMENT vs CORRECT)
        primary = valid_candidates[0]
        diffs: list[FieldDiff] = []

        field_mappings = [
            ("name", "Full Name", "title"),
            ("phone", "Phone", "phone"),
            ("email", "Email", "email"),
            ("company", "Company", "text"),
            ("title", "Title", "text"),
            ("city", "City", "text"),
            ("country", "Country", "text"),
            ("birthday", "Birthday", "date"),
            ("url", "URL", "url"),
            ("entity", "Entity", "text"),
            ("notes", "Note", "text"),
            ("telegram", "Telegram", "social_url"),
            ("wechat", "WeChat", "social_url"),
            ("linkedin", "LinkedIn", "social_url"),
            ("twitter", "Twitter/X", "social_url"),
            ("line", "LINE", "social_url"),
            ("instagram", "Instagram", "social_url"),
        ]

        has_correct = False
        has_supplement = False

        for attr_name, notion_prop, pkind in field_mappings:
            if pkind == "social_url":
                inp_val = contact.get_social(attr_name)
            else:
                inp_val = getattr(contact, attr_name, "")
            if not inp_val or not str(inp_val).strip():
                continue  # Input does not provide this field

            cand_val = primary.get_property_plain_text(notion_prop)

            # Check if Notion currently has no value for this field
            if not cand_val:
                has_supplement = True
                diffs.append(
                    FieldDiff(
                        field_name=attr_name,
                        action=FieldDiffAction.SUPPLEMENT,
                        old_value=None,
                        new_value=inp_val,
                        explanation=f"Adding missing field '{notion_prop}': '{inp_val}'",
                    )
                )
                continue

            # Both have values: check semantic equivalence
            is_equivalent = False
            if pkind == "phone":
                cand_digits = normalize_phone_digits(cand_val)
                inp_digits = contact.phone_digits()
                is_equivalent = bool(
                    cand_digits
                    and inp_digits
                    and (cand_digits == inp_digits or cand_digits.endswith(inp_digits[-8:]) or inp_digits.endswith(cand_digits[-8:]))
                )
            elif pkind == "email":
                is_equivalent = normalize_email_address(cand_val) == contact.normalized_email()
            elif pkind == "url":
                is_equivalent = normalize_url_string(cand_val) == normalize_url_string(str(inp_val))
            elif pkind == "social_url":
                i_raw = str(inp_val).strip().lstrip("@").lower().rstrip("/")
                c_raw = str(cand_val).strip().lstrip("@").lower().rstrip("/")
                is_equivalent = (i_raw == c_raw) or (bool(i_raw) and i_raw in c_raw) or (bool(c_raw) and c_raw in i_raw)
            elif pkind == "date":
                is_equivalent = cand_val.strip() == str(inp_val).strip()
            elif pkind == "title":
                cand_is_ph = is_placeholder_name(cand_val)
                inp_is_ph = is_placeholder_name(inp_val)
                if cand_is_ph and not inp_is_ph:
                    is_equivalent = False
                elif not cand_is_ph and inp_is_ph:
                    is_equivalent = True  # Do not degrade verified real name to placeholder
                else:
                    is_equivalent = cand_val.strip().lower() == str(inp_val).strip().lower()
            elif pkind == "text":
                c_clean = cand_val.strip().lower()
                i_clean = str(inp_val).strip().lower()
                is_equivalent = (c_clean == i_clean) or (attr_name == "notes" and i_clean in c_clean)

            if is_equivalent:
                diffs.append(
                    FieldDiff(
                        field_name=attr_name,
                        action=FieldDiffAction.NO_CHANGE,
                        old_value=cand_val,
                        new_value=inp_val,
                        explanation=f"Field '{notion_prop}' matches existing value",
                    )
                )
            else:
                has_correct = True
                expl = (
                    f"Superseding placeholder '{cand_val}' with verified real name '{inp_val}'"
                    if attr_name == "name" and is_placeholder_name(cand_val) and not is_placeholder_name(inp_val)
                    else f"Updating '{notion_prop}' from '{cand_val}' to '{inp_val}'"
                )
                diffs.append(
                    FieldDiff(
                        field_name=attr_name,
                        action=FieldDiffAction.CORRECT,
                        old_value=cand_val,
                        new_value=inp_val,
                        explanation=expl,
                    )
                )

        # Decide verdict based on diffs
        if has_correct:
            verdict = ReviewVerdict.CORRECT
            correct_fields = [d.field_name for d in diffs if d.action == FieldDiffAction.CORRECT]
            explanation = (
                f"Existing contact '{primary.page_name}' found (score={primary.score}). "
                f"Input corrects existing field(s): {', '.join(correct_fields)}. "
                "Action: Update properties and record audit history in notes."
            )
        elif has_supplement:
            verdict = ReviewVerdict.SUPPLEMENT
            supp_fields = [d.field_name for d in diffs if d.action == FieldDiffAction.SUPPLEMENT]
            explanation = (
                f"Existing contact '{primary.page_name}' found (score={primary.score}). "
                f"Input supplements missing field(s): {', '.join(supp_fields)}. "
                "Action: Enrich contact record."
            )
        else:
            verdict = ReviewVerdict.NO_CHANGE
            explanation = (
                f"Existing contact '{primary.page_name}' (score={primary.score}) already contains "
                "all provided information. Action: No changes required."
            )

        return ReviewResult(
            verdict=verdict,
            target_page_id=primary.page_id,
            target_page_url=primary.page_url,
            target_name=primary.page_name,
            confidence_score=primary.score,
            diffs=diffs,
            candidates=valid_candidates,
            explanation=explanation,
        )

    def prepare_mutation(
        self,
        result: ReviewResult,
        contact: ContactInput,
        candidate: Optional[CandidateMatch] = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """
        Build Notion PATCH properties dictionary and block children list
        based on the review verdict and diffs.
        """
        now_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

        # 1. NO_CHANGE: Zero mutations
        if result.verdict == ReviewVerdict.NO_CHANGE:
            return {}, []

        # 2. CREATE: Full properties + initial capture block
        if result.verdict == ReviewVerdict.CREATE:
            props: dict[str, Any] = {
                "Full Name": make_title(contact.name or "Unknown Person"),
            }
            if not is_placeholder_name(contact.name):
                props["Romaji Name"] = make_rich_text(contact.name)
                props["Romaji Source Name"] = make_rich_text(contact.name)

            for fname in ("phone", "email", "company", "title", "city", "country", "birthday", "url", "entity", "notes"):
                val = getattr(contact, fname, "")
                pname, pobj = property_mapping_for_field(fname, val)
                if pname and pobj:
                    props[pname] = pobj

            for sname in ("telegram", "wechat", "linkedin", "twitter", "line", "instagram"):
                val = contact.get_social(sname)
                pname, pobj = property_mapping_for_field(sname, val)
                if pname and pobj:
                    props[pname] = pobj

            if contact.source:
                props["Source"] = make_rich_text(contact.source)

            blocks: list[dict[str, Any]] = [
                make_heading_block("Antigravity Contact Intake Note", level=3),
                make_bullet_block(f"Created: {now_str}"),
                make_bullet_block(f"Source: {contact.source or 'Slack'}"),
            ]
            if contact.source_text:
                blocks.append(make_bullet_block(f"Original Text: {contact.source_text}"))
            if contact.image_text:
                blocks.append(make_bullet_block(f"OCR Context: {contact.image_text}"))

            return props, blocks

        # 3. SUPPLEMENT: Fill only missing properties + note
        if result.verdict == ReviewVerdict.SUPPLEMENT:
            props = {}
            supp_notes = []

            for d in result.diffs:
                if d.action == FieldDiffAction.SUPPLEMENT and d.new_value:
                    pname, pobj = property_mapping_for_field(d.field_name, d.new_value)
                    if pname and pobj:
                        props[pname] = pobj
                        supp_notes.append(f"{pname}: {d.new_value}")

            blocks = [
                make_heading_block(f"Antigravity Supplementary Note ({now_str})", level=3),
                make_paragraph_block(f"Enriched contact with {len(supp_notes)} missing attribute(s)."),
            ]
            for n in supp_notes:
                blocks.append(make_bullet_block(n))
            if contact.source_text:
                blocks.append(make_bullet_block(f"Source: {contact.source_text}"))

            return props, blocks

        # 4. CORRECT: Update existing properties + archive previous in audit block
        if result.verdict == ReviewVerdict.CORRECT:
            props = {}
            audit_lines = []

            for d in result.diffs:
                if d.action in (FieldDiffAction.CORRECT, FieldDiffAction.SUPPLEMENT) and d.new_value:
                    pname, pobj = property_mapping_for_field(d.field_name, d.new_value)
                    if pname and pobj:
                        props[pname] = pobj
                    if d.action == FieldDiffAction.CORRECT:
                        audit_lines.append(f"{d.field_name}: '{d.new_value}' (previous: '{d.old_value}')")
                    else:
                        audit_lines.append(f"{d.field_name}: '{d.new_value}' (newly supplemented)")

            # If Full Name is updated from a placeholder, ensure Romaji Source Name & Romaji Name are clean
            if "Full Name" in props and candidate:
                props["Romaji Source Name"] = make_rich_text(contact.name)
                props["Romaji Name"] = make_rich_text(contact.name)

            blocks = [
                make_heading_block(f"Antigravity Contact Correction Audit ({now_str})", level=3),
                make_paragraph_block("Updated verified attributes with audit history:"),
            ]
            for line in audit_lines:
                blocks.append(make_bullet_block(line))
            if contact.source_text:
                blocks.append(make_bullet_block(f"Correction Source: {contact.source_text}"))

            return props, blocks

        # 5. MERGE: Update canonical page with combined info + merge record
        if result.verdict == ReviewVerdict.MERGE:
            props = {}
            merge_lines = []

            for d in result.diffs:
                if d.action in (FieldDiffAction.SUPPLEMENT, FieldDiffAction.CORRECT) and d.new_value:
                    pname, pobj = property_mapping_for_field(d.field_name, d.new_value)
                    if pname and pobj:
                        props[pname] = pobj
                    if d.action == FieldDiffAction.CORRECT:
                        merge_lines.append(f"Corrected {d.field_name}: '{d.new_value}' (was '{d.old_value}')")
                    else:
                        merge_lines.append(f"Supplemented {d.field_name}: '{d.new_value}'")
                elif d.action == FieldDiffAction.CONFLICT and d.new_value:
                    merge_lines.append(f"Conflict on {d.field_name}: secondary '{d.new_value}' (canonical retained '{d.old_value}')")

            # If Full Name is updated during merge, ensure Romaji Name & Source Name are synchronized
            if "Full Name" in props:
                full_name_val = props["Full Name"]["title"][0]["text"]["content"]
                props["Romaji Source Name"] = make_rich_text(full_name_val)
                props["Romaji Name"] = make_rich_text(full_name_val)

            blocks = [
                make_heading_block(f"Antigravity Profile Merge Audit ({now_str})", level=3),
                make_paragraph_block("Merged attributes from secondary candidate profiles:"),
            ]
            for line in merge_lines:
                blocks.append(make_bullet_block(line))
            for c in result.candidates:
                blocks.append(make_bullet_block(f"Merged Candidate: {c.page_name} ({c.page_url})"))

            return props, blocks

        return {}, []
