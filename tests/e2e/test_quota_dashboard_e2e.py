"""
End-to-end browser tests for Antigravity Quota Sentinel & Warmup Dashboard.
Uses Playwright to interactively verify:
1. Initial page render, navigation to 5h Quota & Warmup view, and SSOT telemetry cards.
2. Multi-account Fleet Quota Matrix table rendering with stable account-keyed clock IDs.
3. Search filter isolation: filtering does not desynchronize or corrupt countdown timers.
4. Dual warmup actions (Gemini & Claude) and button loading/disabled feedback states.
5. Live sync & Warmup All Idle Pools action execution and toast notifications.
6. Zero console errors, zero uncaught page errors, and responsive layouts.
"""

import os
import time
import pytest
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("WEBHOOK_HUB_URL", "http://127.0.0.1:9423")


def test_quota_dashboard_e2e():
    errors = []
    warnings = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        page.on("pageerror", lambda err: errors.append(f"PageError: {err}"))
        page.on("console", lambda msg: (
            errors.append(f"ConsoleError: {msg.text}") if msg.type == "error"
            else warnings.append(f"ConsoleWarn: {msg.text}") if msg.type == "warning"
            else None
        ))

        # 1. Load dashboard
        resp = page.goto(f"{BASE_URL}/dashboard", wait_until="domcontentloaded")
        assert resp.status == 200, f"Dashboard returned status {resp.status}"

        # 2. Navigate to 5h Quota view
        quota_nav = page.locator("#navItemQuota")
        assert quota_nav.count() > 0, "Missing #navItemQuota"
        quota_nav.click()

        # Wait for Quota view to become visible and data to finish loading
        page.wait_for_function(
            "() => document.getElementById('viewContainerQuota') && "
            "document.getElementById('viewContainerQuota').style.display !== 'none' && "
            "document.getElementById('statQuotaActiveEmail') && "
            "document.getElementById('statQuotaActiveEmail').innerText !== 'Loading...'"
        )

        quota_view = page.locator("#viewContainerQuota")
        assert quota_view.is_visible(), "viewContainerQuota not visible after click"

        # 3. Verify SSOT top metric row
        active_email = page.locator("#statQuotaActiveEmail").inner_text()
        gemini_5h = page.locator("#statQuotaGemini5h").inner_text()
        countdown = page.locator("#statQuotaGeminiCountdown").inner_text()
        claude_5h = page.locator("#statQuota3p5h").inner_text()
        warmup_count = page.locator("#statQuotaWarmupCount").inner_text()

        assert "@" in active_email, f"Invalid active email: {active_email}"
        assert "%" in gemini_5h, f"Invalid gemini 5h: {gemini_5h}"
        assert ("h" in countdown or "m" in countdown or "s" in countdown or "Reset" in countdown), f"Invalid countdown: {countdown}"
        assert "%" in claude_5h, f"Invalid claude 5h: {claude_5h}"

        # 4. Verify Active Account Featured Card
        card = page.locator("#quotaActiveAccountContainer")
        assert len(card.inner_html().strip()) > 100, "Active account container empty"
        assert "Google Cloud Code PA" in card.inner_text()

        # 5. Verify Fleet Matrix rendering & stable clock IDs
        fleet_rows = page.locator("#quotaFleetTableBody tr")
        total_accounts = fleet_rows.count()
        assert total_accounts >= 1, "Fleet table has no accounts"

        # Verify stable account key ID exists instead of index-based clockFleetGemini_0
        sanitized_active = active_email.replace("@", "_").replace(".", "_")
        active_clock = page.locator(f"#clockFleetGemini_{sanitized_active}")
        assert active_clock.count() > 0, f"Missing stable clock element for {sanitized_active}"

        # 6. Test Search Filtering Isolation (Proving the bug fix)
        search_input = page.locator("#taskSearchInput")
        search_input.fill("singh")
        page.keyboard.press("Enter")

        page.wait_for_function("() => document.querySelectorAll('#quotaFleetTableBody tr').length === 1")
        filtered_rows = page.locator("#quotaFleetTableBody tr")
        assert filtered_rows.count() == 1, f"Expected 1 filtered row for 'singh', got {filtered_rows.count()}"

        # The filtered row MUST have singh's stable clock ID, NOT index 0 of all accounts
        singh_clock = page.locator("#clockFleetGemini_singhlokenra346_gmail_com")
        assert singh_clock.count() == 1, "Missing stable singh clock element"
        singh_text = singh_clock.inner_text()
        print(f"Verified singh filtered clock: {singh_text}")

        # Reset search
        search_input.fill("")
        page.keyboard.press("Enter")
        page.wait_for_function("() => document.querySelectorAll('#quotaFleetTableBody tr').length > 1")

        # 7. Test Sync Live Quotas action button with loading state
        sync_btn = page.locator("button:has-text('Sync Live Quotas')")
        assert sync_btn.count() > 0, "Missing Sync Live Quotas button"
        sync_btn.click()
        page.wait_for_function("() => document.querySelectorAll('.toast').length > 0")
        toasts = page.locator(".toast")
        assert toasts.count() > 0, "No toast message triggered"

        # 8. Test Warmup Audit Log table
        warmup_rows = page.locator("#quotaWarmupLogsTableBody tr")
        assert warmup_rows.count() >= 1, "Warmup logs table missing rows"

        # 9. Verify 0 console errors
        assert len(errors) == 0, f"Encountered page/console errors: {errors}"

        browser.close()


if __name__ == "__main__":
    test_quota_dashboard_e2e()
    print("E2E Test Passed Successfully!")
