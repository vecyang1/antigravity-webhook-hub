#!/usr/bin/env python3
"""
Antigravity Webhook Hub — Automated Playwright Dashboard Verification & Visual Capture
Renders and snapshots all 4 dashboard views plus the Signal Detail Modal and Task Drawer.
"""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCREENSHOT_DIR = Path("docs/screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

URL = "http://127.0.0.1:9423/dashboard"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 920},
            device_scale_factor=2,  # Retina high-DPI capture
        )
        page = context.new_page()

        print(f"Navigating to {URL}...")
        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector("#tasksTableBody tr", timeout=10000)
        time.sleep(1.0)  # Settle animations

        # 1. Activity Feed / Tasks view
        p1 = SCREENSHOT_DIR / "01_dashboard_activity_feed.png"
        page.screenshot(path=str(p1), full_page=False)
        print(f"Saved: {p1}")

        # 2. Sentinel AI Runs view
        print("Switching to Sentinel AI Runs...")
        page.click("#navItemSentinels")
        page.wait_for_selector(".sentinel-card", timeout=10000)
        time.sleep(1.0)

        # Expand prompt, tool steps, and report on the first sentinel card
        cards = page.query_selector_all(".sentinel-card")
        if cards:
            first_card = cards[0]
            # Click prompt collapsible header
            headers = first_card.query_selector_all(".collapsible-header")
            if len(headers) >= 3:
                # 0: Prompt, 1: Steps, 2: Report
                headers[0].click()
                time.sleep(0.4)
                headers[1].click()
                time.sleep(0.8)  # Steps lazy load from API
                headers[2].click()
                time.sleep(0.4)

        time.sleep(1.0)
        p2 = SCREENSHOT_DIR / "02_sentinel_ai_runs_expanded.png"
        page.screenshot(path=str(p2), full_page=False)
        print(f"Saved: {p2}")

        # 3. CRM Agent Signals view
        print("Switching to CRM Agent Signals...")
        page.click("#navItemSignals")
        page.wait_for_selector("#signalsTableBody tr", timeout=10000)
        time.sleep(1.0)
        p3 = SCREENSHOT_DIR / "03_crm_agent_signals.png"
        page.screenshot(path=str(p3), full_page=False)
        print(f"Saved: {p3}")

        # 4. Signal Detail Modal
        print("Inspecting first signal...")
        inspect_btns = page.query_selector_all("#signalsTableBody button:has-text('Inspect')")
        if inspect_btns:
            inspect_btns[0].click()
            page.wait_for_selector("#signalModalOverlay.open", timeout=5000)
            time.sleep(0.5)
            p4 = SCREENSHOT_DIR / "04_signal_detail_modal.png"
            page.screenshot(path=str(p4), full_page=False)
            print(f"Saved: {p4}")
            # Close modal
            page.click("#signalModalOverlay .drawer-close")
            time.sleep(0.4)

        # 5. Sidebar Pulse Queue view
        print("Switching to Sidebar Pulse Queue...")
        page.click("#navItemPulses")
        page.wait_for_selector("#pulsesTableBody tr", timeout=10000)
        time.sleep(1.0)
        p5 = SCREENSHOT_DIR / "05_sidebar_pulse_queue.png"
        page.screenshot(path=str(p5), full_page=False)
        print(f"Saved: {p5}")

        # 6. Task Drawer with Associated Agent Activity
        print("Opening task drawer for contact-review task...")
        # Switch back to Tasks view
        page.click(".nav-item[data-nav='tasks']")
        time.sleep(0.5)
        # Open drawer directly via JS for tsk_668fcdefceeb46cd
        page.evaluate("openDrawer('tsk_668fcdefceeb46cd')")
        page.wait_for_selector("#drawerAgentActivityCard[style*='display: flex']", timeout=5000)
        time.sleep(1.0)
        p6 = SCREENSHOT_DIR / "06_task_drawer_agent_activity.png"
        page.screenshot(path=str(p6), full_page=False)
        print(f"Saved: {p6}")

        browser.close()
        print("All 6 screenshots captured successfully!")

if __name__ == "__main__":
    main()
