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
            secondary = high_matches[1]

            merge_diffs = []
            # Check fields in secondary that canonical might lack
            for prop in ("Phone", "Email", "Company", "Title", "City", "Country", "Birthday", "URL", "Entity", "Note"):
                can_val = canonical.get_property_plain_text(prop)
                sec_val = secondary.get_property_plain_text(prop)
                inp_val = getattr(contact, prop.lower(), "")

                effective_val = inp_val or sec_val
                if effective_val and not can_val:
                    merge_diffs.append(
                        FieldDiff(
                            field_name=prop.lower(),
                            action=FieldDiffAction.SUPPLEMENT,
                            old_value=None,
                            new_value=effective_val,
                            explanation=f"Merged '{prop}' from secondary record into canonical",
                        )
                    )
                elif effective_val and can_val and effective_val.strip() != can_val.strip():
                    merge_diffs.append(
                        FieldDiff(
                            field_name=prop.lower(),
                            action=FieldDiffAction.CONFLICT,
                            old_value=can_val,
                            new_value=effective_val,
                            explanation=f"Conflicting '{prop}' merged into notes history",
                        )
                    )

            return ReviewResult(
                verdict=ReviewVerdict.MERGE,
                target_page_id=canonical.page_id,
                target_page_url=canonical.page_url,
                target_name=canonical.page_name,
                confidence_score=canonical.score,
                diffs=merge_diffs,
                candidates=high_matches,
                explanation=(
                    f"Found multiple candidate records in Notion ('{canonical.page_name}' score={canonical.score} "
                    f"and '{secondary.page_name}' score={secondary.score}). "
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
        ]

        has_correct = False
        has_supplement = False

        for attr_name, notion_prop, pkind in field_mappings:
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
                is_equivalent = normalize_url_string(cand_val) == contact.normalized_url()
            elif pkind == "date":
                is_equivalent = cand_val.strip() == str(inp_val).strip()
            elif pkind == "title":
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
                diffs.append(
                    FieldDiff(
                        field_name=attr_name,
                        action=FieldDiffAction.CORRECT,
                        old_value=cand_val,
                        new_value=inp_val,
                        explanation=f"Updating '{notion_prop}' from '{cand_val}' to '{inp_val}'",
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
            if contact.phone:
                props["Phone"] = make_rich_text(contact.phone)
            if contact.email:
                props["Email"] = make_email(contact.email)
            if contact.company:
                props["Company"] = make_rich_text(contact.company)
            if contact.title:
                props["Title"] = make_rich_text(contact.title)
            if contact.city:
                props["City"] = make_rich_text(contact.city)
            if contact.country:
                props["Country"] = make_rich_text(contact.country)
            if contact.birthday:
                props["Birthday"] = make_date(contact.birthday)
            if contact.url:
                props["URL"] = make_url(contact.url)
            if contact.entity:
                props["Entity"] = make_rich_text(contact.entity)
            if contact.notes:
                props["Note"] = make_rich_text(contact.notes)
            if contact.source:
                props["Source"] = make_rich_text(contact.source)

            # Block children
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
                    if d.field_name == "phone":
                        props["Phone"] = make_rich_text(d.new_value)
                        supp_notes.append(f"Phone: {d.new_value}")
                    elif d.field_name == "email":
                        props["Email"] = make_email(d.new_value)
                        supp_notes.append(f"Email: {d.new_value}")
                    elif d.field_name == "company":
                        props["Company"] = make_rich_text(d.new_value)
                        supp_notes.append(f"Company: {d.new_value}")
                    elif d.field_name == "title":
                        props["Title"] = make_rich_text(d.new_value)
                        supp_notes.append(f"Title: {d.new_value}")
                    elif d.field_name == "city":
                        props["City"] = make_rich_text(d.new_value)
                        supp_notes.append(f"City: {d.new_value}")
                    elif d.field_name == "country":
                        props["Country"] = make_rich_text(d.new_value)
                        supp_notes.append(f"Country: {d.new_value}")
                    elif d.field_name == "birthday":
                        props["Birthday"] = make_date(d.new_value)
                        supp_notes.append(f"Birthday: {d.new_value}")
                    elif d.field_name == "url":
                        props["URL"] = make_url(d.new_value)
                        supp_notes.append(f"URL: {d.new_value}")
                    elif d.field_name == "entity":
                        props["Entity"] = make_rich_text(d.new_value)
                        supp_notes.append(f"Entity: {d.new_value}")
                    elif d.field_name == "notes":
                        # If existing note was empty, set it
                        props["Note"] = make_rich_text(d.new_value)
                        supp_notes.append(f"Note: {d.new_value}")

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
                if d.action == FieldDiffAction.CORRECT and d.new_value:
                    if d.field_name == "phone":
                        props["Phone"] = make_rich_text(d.new_value)
                    elif d.field_name == "email":
                        props["Email"] = make_email(d.new_value)
                    elif d.field_name == "company":
                        props["Company"] = make_rich_text(d.new_value)
                    elif d.field_name == "title":
                        props["Title"] = make_rich_text(d.new_value)
                    elif d.field_name == "city":
                        props["City"] = make_rich_text(d.new_value)
                    elif d.field_name == "country":
                        props["Country"] = make_rich_text(d.new_value)
                    elif d.field_name == "birthday":
                        props["Birthday"] = make_date(d.new_value)
                    elif d.field_name == "url":
                        props["URL"] = make_url(d.new_value)
                    elif d.field_name == "entity":
                        props["Entity"] = make_rich_text(d.new_value)
                    elif d.field_name == "name":
                        props["Full Name"] = make_title(d.new_value)

                    audit_lines.append(f"{d.field_name}: '{d.new_value}' (previous: '{d.old_value}')")

                elif d.action == FieldDiffAction.SUPPLEMENT and d.new_value:
                    # Also include supplemented fields if any
                    if d.field_name == "phone":
                        props["Phone"] = make_rich_text(d.new_value)
                    elif d.field_name == "email":
                        props["Email"] = make_email(d.new_value)
                    elif d.field_name == "company":
                        props["Company"] = make_rich_text(d.new_value)
                    elif d.field_name == "title":
                        props["Title"] = make_rich_text(d.new_value)
                    elif d.field_name == "city":
                        props["City"] = make_rich_text(d.new_value)
                    elif d.field_name == "country":
                        props["Country"] = make_rich_text(d.new_value)
                    elif d.field_name == "birthday":
                        props["Birthday"] = make_date(d.new_value)
                    elif d.field_name == "url":
                        props["URL"] = make_url(d.new_value)
                    elif d.field_name == "entity":
                        props["Entity"] = make_rich_text(d.new_value)
                    audit_lines.append(f"{d.field_name}: '{d.new_value}' (newly supplemented)")

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
                if d.action in (FieldDiffAction.SUPPLEMENT, FieldDiffAction.CONFLICT) and d.new_value:
                    if d.field_name == "phone" and not candidate.get_phone() if candidate else True:
                        props["Phone"] = make_rich_text(d.new_value)
                    elif d.field_name == "email" and not candidate.get_email() if candidate else True:
                        props["Email"] = make_email(d.new_value)
                    elif d.field_name == "company":
                        props["Company"] = make_rich_text(d.new_value)
                    merge_lines.append(f"{d.field_name}: '{d.new_value}'")

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
