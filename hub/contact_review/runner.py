"""
Antigravity Webhook Hub — Contact Review Orchestration Runner
Coordinates end-to-end contact intake evaluation, Notion SSOT mutation,
re-read verification, Slack feedback, and agent signal emission.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from hub.contact_review.engine import ContactReviewEngine
from hub.contact_review.models import ContactInput, ReviewResult, ReviewVerdict
from hub.contact_review.notion_client import NotionPeopleClient
from hub.contact_review.slack_notifier import SlackNotifier

logger = logging.getLogger("hub.contact_review.runner")


async def execute_contact_review(
    payload: dict[str, Any] | ContactInput,
    auto_apply: bool = True,
    notify_slack: bool = True,
    dry_run: bool = False,
    client: Optional[NotionPeopleClient] = None,
    log_callback: Optional[Any] = None,
) -> ReviewResult:
    """
    Execute full contact review workflow with SSOT integrity:
    1. Parse and normalize ContactInput.
    2. Query Notion People Database (live read).
    3. Evaluate review verdict (NO_CHANGE, SUPPLEMENT, CORRECT, MERGE, CREATE).
    4. If not dry_run: execute Notion mutation, re-read and verify (unidirectional flow).
    5. Dispatch Slack notification.
    6. Emit agent signal for collaborative discovery.
    """
    def log(msg: str):
        logger.info(msg)
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    # 1. Parse ContactInput
    if isinstance(payload, ContactInput):
        contact = payload
    else:
        contact = ContactInput.from_dict(payload)

    log(f"Starting Antigravity Contact Review for '{contact.name}' (source: {contact.source})...")

    # 2. Initialize components
    notion = client or NotionPeopleClient()
    engine = ContactReviewEngine()
    slack = SlackNotifier()

    # 3. Query Notion candidates (live truth)
    try:
        candidates = await notion.search_candidates(contact)
        log(f"Found {len(candidates)} candidate match(es) in Notion People database.")
    except Exception as e:
        logger.exception("Failed to query Notion candidates: %s", e)
        return ReviewResult(
            verdict=ReviewVerdict.NO_CHANGE,
            target_name=contact.name,
            error=f"Notion candidate search failed: {e}",
        )

    # 4. Evaluate verdict
    result = engine.evaluate(contact, candidates)
    log(f"Evaluation completed: Verdict = {result.verdict.value.upper()} (Confidence: {result.confidence_score}%)")
    log(f"Explanation: {result.explanation}")

    if dry_run:
        log("Dry-run requested. Skipping Notion database mutations and Slack dispatch.")
        return result

    # 5. Execute Notion mutations with Unidirectional SSOT Verification
    if auto_apply:
        try:
            if result.verdict == ReviewVerdict.NO_CHANGE:
                result.applied = True
                result.ssot_verified = True
                log("Zero changes needed. Existing Notion page remains identical SSOT.")

            elif result.verdict == ReviewVerdict.CREATE:
                log("Creating brand new page in Notion People CRM...")
                props, blocks = engine.prepare_mutation(result, contact)
                created = await notion.create_page(props, children=blocks)
                page_id = created.get("id")
                page_url = created.get("url") or f"https://www.notion.so/{notion.database_id}?p={page_id.replace('-', '')}"
                result.target_page_id = page_id
                result.target_page_url = page_url
                result.applied = True
                log(f"Page created successfully: {page_url} (ID: {page_id})")

                # SSOT Re-Read Verification
                log("Verifying newly created page via live Notion API re-read...")
                verified, _ = await notion.verify_page_properties(page_id, props)
                result.ssot_verified = verified
                log(f"SSOT Verification: {'PASSED ✅' if verified else 'FAILED ❌'}")

            elif result.verdict in (ReviewVerdict.SUPPLEMENT, ReviewVerdict.CORRECT):
                page_id = result.target_page_id
                if not page_id:
                    raise ValueError(f"Missing target_page_id for verdict {result.verdict}")

                props, blocks = engine.prepare_mutation(result, contact)
                if props:
                    log(f"Applying property update to page {page_id}...")
                    await notion.update_page_properties(page_id, props)
                if blocks:
                    log(f"Appending {len(blocks)} audit block(s) to page {page_id}...")
                    await notion.append_page_blocks(page_id, blocks)

                result.applied = True
                # SSOT Re-Read Verification
                log("Re-reading page from Notion API to verify updated truth...")
                verified, _ = await notion.verify_page_properties(page_id, props)
                result.ssot_verified = verified
                log(f"SSOT Verification: {'PASSED ✅' if verified else 'FAILED ❌'}")

            elif result.verdict == ReviewVerdict.MERGE:
                canonical_id = result.target_page_id
                props, blocks = engine.prepare_mutation(result, contact)
                if props:
                    log(f"Updating canonical page {canonical_id}...")
                    await notion.update_page_properties(canonical_id, props)
                if blocks:
                    log(f"Appending merge audit history to {canonical_id}...")
                    await notion.append_page_blocks(canonical_id, blocks)

                # Append tombstone notice to secondary candidates
                for c in result.candidates[1:]:
                    try:
                        tombstone = [
                            notion_bullet := {
                                "type": "bulleted_list_item",
                                "bulleted_list_item": {
                                    "rich_text": [{
                                        "type": "text",
                                        "text": {"content": f"[Antigravity Notice]: This profile was merged into canonical page {result.target_page_url}"}
                                    }]
                                }
                            }
                        ]
                        await notion.append_page_blocks(c.page_id, tombstone)
                    except Exception as merge_err:
                        logger.warning("Failed to annotate secondary page %s: %s", c.page_id, merge_err)

                result.applied = True
                verified, _ = await notion.verify_page_properties(canonical_id, props)
                result.ssot_verified = verified
                log(f"SSOT Verification for merged page: {'PASSED ✅' if verified else 'FAILED ❌'}")

        except Exception as mut_err:
            logger.exception("Error executing Notion mutation: %s", mut_err)
            result.error = f"Notion mutation error: {mut_err}"
            result.applied = False
            result.ssot_verified = False

    # 6. Dispatch Slack notification
    if notify_slack and not dry_run:
        try:
            log("Sending formatted notification to Slack...")
            sent = slack.send_notification(result, contact)
            result.slack_notified = sent
            if sent:
                log("Slack notification successfully dispatched.")
            else:
                log("Slack notification skipped or could not be delivered.")
        except Exception as slack_err:
            logger.warning("Slack notification failed: %s", slack_err)
            result.slack_notified = False

    # 7. Emit atomic agent signal for collaborative discovery
    try:
        signals_dir = Path(".agents/signals/contact_review")
        signals_dir.mkdir(parents=True, exist_ok=True)
        sig_id = f"sig_{uuid.uuid4().hex[:12]}"
        sig_file = signals_dir / f"{sig_id}.signal.json"
        tmp_file = signals_dir / f"{sig_id}.tmp"

        signal_data = {
            "signal_version": "1.0",
            "signal_id": sig_id,
            "action": "contact_review",
            "verdict": result.verdict.value,
            "target_name": result.target_name,
            "target_page_id": result.target_page_id,
            "target_page_url": result.target_page_url,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "result": result.to_dict(),
        }
        tmp_file.write_text(json.dumps(signal_data, indent=2), encoding="utf-8")
        tmp_file.replace(sig_file)
        log(f"Emitted agent signal: {sig_file}")
    except Exception as sig_err:
        logger.debug("Failed to emit agent signal: %s", sig_err)

    log("Contact Review execution finished.")
    return result


def review_contact_sync(
    payload: dict[str, Any] | ContactInput,
    auto_apply: bool = True,
    notify_slack: bool = True,
    dry_run: bool = False,
    client: Optional[NotionPeopleClient] = None,
) -> ReviewResult:
    """Synchronous convenience wrapper for CLI and scripts."""
    return asyncio.run(
        execute_contact_review(
            payload=payload,
            auto_apply=auto_apply,
            notify_slack=notify_slack,
            dry_run=dry_run,
            client=client,
        )
    )
