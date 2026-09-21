#!/usr/bin/env python3
"""scripts/calibrate_alert_filter.py — Empirical Threshold & Fail-Safe Calibration Test Suite.

Evaluates TypeSafe AI Jev System One noul probability distributions across 15 real-world alert cases
to calibrate:
1. Optimal Critical Threshold (comparing 0.60, 0.70, 0.75, 0.80).
2. Fail-Open vs Heuristic vs Fail-Closed disaster recovery tradeoffs.

Usage:
  python3 scripts/calibrate_alert_filter.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub.alert_filter import JevAlertFilter, AlertFilterResult


# 15 Real-World Infrastructure Alert Scenarios
BENCHMARK_CASES = [
    # Category A: Transient Flaps & Health Check Jitter (Expected: SUPPRESS)
    {
        "id": "NOISE-1",
        "category": "transient_flap",
        "service": "GlintMuse Blog",
        "message": "[🔴 Down] Testing keyword [blog] in body: keyword is not in body (HTTP 200)",
        "url": "https://blog.glintmuse.com",
        "ground_truth_critical": False,
    },
    {
        "id": "NOISE-2",
        "category": "transient_flap",
        "service": "Coolify Healthcheck",
        "message": "Container glintmuse-blog failed health probe: timeout after 5000ms, retrying",
        "url": "https://coolify.worldinspirelab.com",
        "ground_truth_critical": False,
    },
    {
        "id": "NOISE-3",
        "category": "transient_flap",
        "service": "Uptime Kuma",
        "message": "[🔴 Down] Website Ping Timeout: 2000ms threshold exceeded for 1 cycle",
        "url": "https://monitor.worldinspirelab.com",
        "ground_truth_critical": False,
    },
    {
        "id": "NOISE-4",
        "category": "transient_flap",
        "service": "Albato Ingress",
        "message": "Transient connection reset by peer, webhook delivery queued for retry in 30s",
        "url": "https://albato.worldinspirelab.com",
        "ground_truth_critical": False,
    },
    {
        "id": "NOISE-5",
        "category": "transient_flap",
        "service": "GDR Cookie Keeper",
        "message": "Routine health probe returned 200 OK with empty response body",
        "url": "https://gdr.worldinspirelab.com",
        "ground_truth_critical": False,
    },
    {
        "id": "NOISE-6",
        "category": "recovery",
        "service": "Uptime Kuma",
        "message": "[✅ Up] GlintMuse Blog is UP (HTTP 200, 142ms)",
        "url": "https://blog.glintmuse.com",
        "ground_truth_critical": False,
    },

    # Category B: Borderline Warnings & Elevated Pressure (Evaluated for boundary stability)
    {
        "id": "WARN-1",
        "category": "warning",
        "service": "VPS Disk Monitor",
        "message": "Warning: Volume /dev/vda1 usage reached 84% (warning threshold 80%, critical is 95%)",
        "url": "lisahost-vps",
        "ground_truth_critical": False,
    },
    {
        "id": "WARN-2",
        "category": "warning",
        "service": "Memory Sentinel",
        "message": "Host RSS usage elevated to 81% of 128MB budget (98.4 MB in use, GC active)",
        "url": "local-gateway",
        "ground_truth_critical": False,
    },
    {
        "id": "WARN-3",
        "category": "warning",
        "service": "SSL Expiry Monitor",
        "message": "Notice: Let's Encrypt certificate for worldinspirelab.com expires in 18 days (auto-renew scheduled in 4 days)",
        "url": "https://worldinspirelab.com",
        "ground_truth_critical": False,
    },
    {
        "id": "WARN-4",
        "category": "warning",
        "service": "Stripe Webhook Delivery",
        "message": "Transient HTTP 500 error from recipient endpoint, automatic retry 2 of 5 scheduled",
        "url": "https://api.stripe.com",
        "ground_truth_critical": False,
    },

    # Category C: Confirmed Critical Outages (Expected: ESCALATE)
    {
        "id": "CRIT-1",
        "category": "critical_outage",
        "service": "PostgreSQL Primary Cluster",
        "message": "FATAL: remaining connection slots are reserved for non-replication superuser connections. Client connections rejected.",
        "url": "postgresql://postgres:5432/main",
        "ground_truth_critical": True,
    },
    {
        "id": "CRIT-2",
        "category": "critical_outage",
        "service": "Host Hardware & Network",
        "message": "Server unreachable: SSH connection refused on port 22, ping packet loss 100%, host offline",
        "url": "103.137.215.158",
        "ground_truth_critical": True,
    },
    {
        "id": "CRIT-3",
        "category": "critical_outage",
        "service": "Coolify Engine",
        "message": "Docker daemon crashed: /var/run/docker.sock connection refused, all 14 containers halted",
        "url": "https://coolify.worldinspirelab.com",
        "ground_truth_critical": True,
    },
    {
        "id": "CRIT-4",
        "category": "critical_outage",
        "service": "Supabase Production DB",
        "message": "HTTP 503 Service Unavailable: Database disk IOPS exhausted, transaction rate collapsed to 0, read-only emergency mode activated",
        "url": "https://iyoraqmuhacganyjfzek.supabase.co",
        "ground_truth_critical": True,
    },
    {
        "id": "CRIT-5",
        "category": "critical_outage",
        "service": "Payment Gateway",
        "message": "Payment processing halted: Stripe API returned 401 Unauthorized - Account restricted or secret key revoked",
        "url": "https://api.stripe.com/v1/charges",
        "ground_truth_critical": True,
    },
]


def run_calibration():
    print("=" * 75)
    print("  TYPESAFE AI JEV SYSTEM ONE — ALERT FILTER EMPIRICAL CALIBRATION")
    print("=" * 75)

    filter_engine = JevAlertFilter(enabled=True, critical_threshold=0.70)
    print(f"Engine Model:      {filter_engine.model}")
    print(f"Target Endpoint:   {filter_engine.base_url}")
    print(f"API Key Present:   {bool(filter_engine.api_key)}")
    print(f"Test Corpus Size:  {len(BENCHMARK_CASES)} cases")
    print("-" * 75)

    results: List[Dict[str, Any]] = []

    # Step 1: Run Live Evaluation for all 15 cases
    print(f"{'ID':<8} {'Category':<16} {'GroundTruth':<12} {'Noul Score':<12} {'Latency':<10} {'Action (0.70)':<12}")
    print("-" * 75)

    for case in BENCHMARK_CASES:
        t0 = time.perf_counter()
        # Allow 1 quick retry on transient socket reset
        res = None
        for attempt in range(2):
            res = filter_engine.evaluate_alert(
                service=case["service"],
                message=case["message"],
                url=case["url"],
            )
            if res.eval_source == "jev" or attempt == 1:
                break
            time.sleep(0.3)
        dur = (time.perf_counter() - t0) * 1000.0

        gt_str = "CRITICAL" if case["ground_truth_critical"] else "NOISE"
        print(f"{case['id']:<8} {case['category']:<16} {gt_str:<12} {res.noul:<12.3f} {dur:<8.1f}ms {res.action.upper():<12}")

        results.append({
            "case": case,
            "noul": res.noul,
            "duration_ms": dur,
            "action_070": res.action,
            "reason": res.reason,
        })

    print("=" * 75)
    print("\n" + "=" * 75)
    print("  PART 1: THRESHOLD SENSITIVITY & CONFUSION MATRIX COMPARISON")
    print("=" * 75)

    candidate_thresholds = [0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]

    print(f"{'Threshold':<10} {'Suppressed':<12} {'Escalated':<12} {'False Positives':<18} {'False Negatives':<18} {'Accuracy'}")
    print("-" * 75)

    for th in candidate_thresholds:
        suppressed_count = 0
        escalated_count = 0
        fp_list = []  # Expected Noise, but escalated
        fn_list = []  # Expected Critical, but suppressed

        for r in results:
            predicted_critical = r["noul"] >= th
            gt_critical = r["case"]["ground_truth_critical"]

            if predicted_critical:
                escalated_count += 1
                if not gt_critical:
                    fp_list.append(r["case"]["id"])
            else:
                suppressed_count += 1
                if gt_critical:
                    fn_list.append(r["case"]["id"])

        total = len(results)
        correct = total - len(fp_list) - len(fn_list)
        accuracy = (correct / total) * 100.0

        fp_desc = f"{len(fp_list)} ({', '.join(fp_list) if fp_list else 'none'})"
        fn_desc = f"{len(fn_list)} ({', '.join(fn_list) if fn_list else 'none'})"

        marker = " ⭐️ [SELECTED]" if th == 0.70 else ""
        print(f"{th:<10.2f} {suppressed_count:<12} {escalated_count:<12} {fp_desc:<18} {fn_desc:<18} {accuracy:5.1f}%{marker}")

    print("=" * 75)

    # Step 2: Distribution Clusters Analysis
    print("\n" + "=" * 75)
    print("  PART 2: EMPIRICAL NOUL PROBABILITY CLUSTERS")
    print("=" * 75)

    noise_nouls = [r["noul"] for r in results if r["case"]["category"] == "transient_flap" or r["case"]["category"] == "recovery"]
    warn_nouls = [r["noul"] for r in results if r["case"]["category"] == "warning"]
    crit_nouls = [r["noul"] for r in results if r["case"]["category"] == "critical_outage"]

    print(f"1. Transient Flaps / Noise (N={len(noise_nouls)}):")
    print(f"   Min: {min(noise_nouls):.3f} | Max: {max(noise_nouls):.3f} | Avg: {sum(noise_nouls)/len(noise_nouls):.3f}")
    print(f"   Scores: {[round(x, 3) for x in noise_nouls]}")

    print(f"\n2. Warnings & High Load (N={len(warn_nouls)}):")
    print(f"   Min: {min(warn_nouls):.3f} | Max: {max(warn_nouls):.3f} | Avg: {sum(warn_nouls)/len(warn_nouls):.3f}")
    print(f"   Scores: {[round(x, 3) for x in warn_nouls]}")

    print(f"\n3. Confirmed Critical Outages (N={len(crit_nouls)}):")
    print(f"   Min: {min(crit_nouls):.3f} | Max: {max(crit_nouls):.3f} | Avg: {sum(crit_nouls)/len(crit_nouls):.3f}")
    print(f"   Scores: {[round(x, 3) for x in crit_nouls]}")

    print("-" * 75)
    margin = min(crit_nouls) - max(noise_nouls + warn_nouls)
    print(f"👉 Separation Margin (Lowest Critical - Highest Noise/Warning): {margin:+.3f}")
    print("=" * 75)

    # Step 3: Disaster Recovery Strategy (fail_open vs heuristic vs fail_closed)
    print("\n" + "=" * 75)
    print("  PART 3: FAIL-SAFE DISASTER RECOVERY POLICY AUDIT")
    print("=" * 75)

    policies = ["fail_open", "heuristic", "fail_closed"]
    failure_scenarios = [
        {"name": "Transient Jitter during API Outage", "service": "GlintMuse Blog", "msg": "keyword is not in body", "gt_crit": False},
        {"name": "Fatal DB Crash during API Outage", "service": "PostgreSQL", "msg": "FATAL: connection slots full", "gt_crit": True},
        {"name": "Routine Ping Flap during Timeout", "service": "Uptime Kuma", "msg": "Ping timeout after 2000ms", "gt_crit": False},
    ]

    print(f"{'Policy':<14} {'Scenario':<34} {'Decision':<10} {'Safety Verdict':<20}")
    print("-" * 75)

    for pol in policies:
        f_engine = JevAlertFilter(enabled=True, fallback_mode=pol, api_key="invalid_mock_key_for_test", base_url="http://127.0.0.1:9")
        for sc in failure_scenarios:
            res = f_engine.evaluate_alert(service=sc["service"], message=sc["msg"])
            is_safe = (res.action == "escalate") if sc["gt_crit"] else (res.action == "suppress")
            verdict = "✅ SAFE" if is_safe else ("🚨 FATAL LEAK (Dropped Outage!)" if sc["gt_crit"] else "⚠️ NOISE FLAP (Extra Alert)")
            print(f"{pol:<14} {sc['name']:<34} {res.action.upper():<10} {verdict}")

    print("=" * 75)


if __name__ == "__main__":
    run_calibration()
