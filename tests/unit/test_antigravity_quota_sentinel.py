"""
Unit Tests for Antigravity Quota Sentinel & 5h Rolling Window Warmup Engine
Verifies both sides of truth:
1. Positive path: detecting idle 100% buckets, expired windows, successful 1-token warmup dispatch,
   SQLite SSOT persistence, and broker SSE publishing.
2. Adversarial path: active countdown buckets (<100%) skipped, unexpired future windows skipped,
   disabled accounts ignored, 4h55m cooldown enforced, and HTTP connection failures gracefully recorded.
"""

import asyncio
import io
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hub.antigravity.quota_sentinel import (
    AccountQuotaProfile,
    AntigravityQuotaSentinel,
    QuotaBucketInfo,
    parse_iso_timestamp,
)
from hub.config import AntigravityQuotaConfig, AppConfig
from hub.db import DatabaseManager


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="test_quota_sentinel_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db(temp_dir):
    db_file = temp_dir / "test_hub.db"
    manager = DatabaseManager(str(db_file))
    manager.init_schema()
    return manager


@pytest.fixture
def fake_accounts_dir(temp_dir):
    accounts_dir = temp_dir / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Active account: viinam33@gmail.com
    acc1_id = "acc-active-001"
    acc1 = {
        "id": acc1_id,
        "email": "viinam33@gmail.com",
        "name": "Viinam",
        "disabled": False,
        "proxy_disabled": False,
        "token": {
            "access_token": "fake_token_viinam",
            "project_id": "aicode-consumers",
            "expiry_timestamp": time.time() + 3600,
        },
        "quota": {
            "subscription_tier": "Google AI Ultra",
            "last_updated": int(time.time()),
            "quota_groups": [
                {
                    "name": "group_gemini",
                    "buckets": [
                        {
                            "bucket_id": "gemini-5h",
                            "window": "5h",
                            "remaining_fraction": 1.0,
                            "reset_time": "2026-09-13T10:00:00Z",  # In the past
                        },
                        {
                            "bucket_id": "gemini-weekly",
                            "window": "weekly",
                            "remaining_fraction": 0.5,
                            "reset_time": "2026-09-20T10:00:00Z",
                        },
                    ],
                },
                {
                    "name": "group_3p",
                    "buckets": [
                        {
                            "bucket_id": "3p-5h",
                            "window": "5h",
                            "remaining_fraction": 0.75,  # Active timer running
                            "reset_time": "2026-09-13T20:00:00Z",  # In the future
                        },
                    ],
                },
            ],
        },
    }
    with open(accounts_dir / f"{acc1_id}.json", "w", encoding="utf-8") as f:
        json.dump(acc1, f)

    # 2. Inactive account: standby@gmail.com
    acc2_id = "acc-standby-002"
    acc2 = {
        "id": acc2_id,
        "email": "standby@gmail.com",
        "disabled": False,
        "token": {
            "access_token": "fake_token_standby",
            "project_id": "aicode-consumers",
        },
        "quota": {
            "subscription_tier": "Google AI Pro",
            "quota_groups": [
                {
                    "buckets": [
                        {
                            "bucket_id": "3p-5h",
                            "window": "5h",
                            "remaining_fraction": 1.0,
                            "reset_time": "2026-09-13T09:00:00Z",  # Expired
                        }
                    ]
                }
            ],
        },
    }
    with open(accounts_dir / f"{acc2_id}.json", "w", encoding="utf-8") as f:
        json.dump(acc2, f)

    # 3. Disabled account: disabled@gmail.com
    acc3_id = "acc-disabled-003"
    acc3 = {
        "id": acc3_id,
        "email": "disabled@gmail.com",
        "disabled": True,
        "token": {"access_token": "tok_disabled"},
        "quota": {
            "quota_groups": [
                {
                    "buckets": [
                        {
                            "bucket_id": "gemini-5h",
                            "window": "5h",
                            "remaining_fraction": 1.0,
                            "reset_time": "2026-09-13T08:00:00Z",
                        }
                    ]
                }
            ]
        },
    }
    with open(accounts_dir / f"{acc3_id}.json", "w", encoding="utf-8") as f:
        json.dump(acc3, f)

    # Create accounts.json pointing to acc1_id as active
    accounts_json = temp_dir / "accounts.json"
    with open(accounts_json, "w", encoding="utf-8") as f:
        json.dump({"current_account_id": acc1_id}, f)

    return temp_dir


def test_parse_iso_timestamp():
    # Valid RFC3339 UTC
    ts1 = parse_iso_timestamp("2026-09-13T21:03:47Z")
    assert ts1 is not None
    assert ts1 > 0

    # Valid with timezone offset
    ts2 = parse_iso_timestamp("2026-09-13T21:03:47+00:00")
    assert ts1 == ts2

    # Invalid string and None
    assert parse_iso_timestamp("invalid-date") is None
    assert parse_iso_timestamp(None) is None


def test_scan_accounts_discovery(fake_accounts_dir, db):
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    # Patch accounts.json path
    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

    assert len(profiles) == 3

    # Active account is sorted first
    assert profiles[0].email == "viinam33@gmail.com"
    assert profiles[0].is_active is True
    assert profiles[0].subscription_tier == "Google AI Ultra"
    assert len(profiles[0].buckets) == 3

    # Check model groups deduced properly
    b_map = {b.bucket_id: b for b in profiles[0].buckets}
    assert b_map["gemini-5h"].model_group == "Gemini Models"
    assert b_map["3p-5h"].model_group == "Claude and GPT models"


def test_sync_quotas_to_db_ssot(fake_accounts_dir, db):
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        upserted = sentinel.sync_quotas_to_db()

    assert upserted == 5  # 3 from acc1, 1 from acc2, 1 from acc3

    # Retrieve from DB
    snaps = db.get_quota_snapshots()
    assert len(snaps) == 5

    # Filter by active account
    acc1_snaps = db.get_quota_snapshots("viinam33@gmail.com")
    assert len(acc1_snaps) == 3

    # Verify idempotency: syncing twice doesn't duplicate rows
    upserted2 = sentinel.sync_quotas_to_db()
    assert upserted2 == 5
    assert len(db.get_quota_snapshots()) == 5


def test_evaluate_warmup_candidates_both_halves(fake_accounts_dir, db):
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")
    cfg.antigravity_quota.auto_warmup_5h = True
    cfg.antigravity_quota.auto_warmup_weekly = False

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        # Evaluation without force:
        # - viinam33 gemini-5h: rem=1.0, reset in past -> ELIGIBLE
        # - viinam33 gemini-weekly: weekly bucket (auto_warmup_weekly=False) -> SKIPPED
        # - viinam33 3p-5h: rem=0.75 (timer active) -> SKIPPED
        # - standby 3p-5h: rem=1.0, reset in past -> ELIGIBLE
        # - disabled gemini-5h: account disabled -> SKIPPED
        candidates = sentinel.evaluate_warmup_candidates(profiles, force=False)
        assert len(candidates) == 2

        cand_keys = {(c["account_email"], c["bucket_id"]) for c in candidates}
        assert ("viinam33@gmail.com", "gemini-5h") in cand_keys
        assert ("standby@gmail.com", "3p-5h") in cand_keys

        # Check model selection
        for c in candidates:
            if c["bucket_id"] == "gemini-5h":
                assert c["model_name"] == "gemini-3-flash"
            elif c["bucket_id"] == "3p-5h":
                assert c["model_name"] == "claude-sonnet-4-6"


def test_cooldown_enforcement(fake_accounts_dir, db):
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")
    cfg.antigravity_quota.warmup_cooldown_seconds = 17700  # 4h 55m

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        # First evaluation: 2 candidates
        candidates = sentinel.evaluate_warmup_candidates(profiles, force=False)
        assert len(candidates) == 2

        # Record a successful warmup for viinam33 gemini-5h
        db.record_warmup_log(
            account_email="viinam33@gmail.com",
            bucket_id="gemini-5h",
            model_name="gemini-3-flash",
            trigger_reason="test",
            route_used="tools_api_8045",
            status="success",
        )

        # Second evaluation: viinam33 gemini-5h must now be skipped due to cooldown!
        candidates_after = sentinel.evaluate_warmup_candidates(profiles, force=False)
        assert len(candidates_after) == 1
        assert candidates_after[0]["account_email"] == "standby@gmail.com"

        # When force=True, cooldown is bypassed
        candidates_forced = sentinel.evaluate_warmup_candidates(profiles, force=True)
        assert len(candidates_forced) == 4  # All non-disabled buckets


def test_execute_warmup_success_8045(db):
    cfg = AppConfig()
    sentinel = AntigravityQuotaSentinel(config=cfg, db=db)

    candidate = {
        "account_email": "test@gmail.com",
        "bucket_id": "gemini-5h",
        "model_name": "gemini-3-flash",
        "access_token": "valid_token_xyz",
        "project_id": "aicode-consumers",
    }

    fake_response = io.BytesIO(json.dumps({"success": True, "message": "Warmup triggered"}).encode("utf-8"))
    fake_response.status = 200

    with patch("urllib.request.urlopen", return_value=fake_response):
        res = sentinel.execute_warmup_sync(candidate, trigger_reason="unit_test")

    assert res["status"] == "success"
    assert res["account_email"] == "test@gmail.com"
    assert res["bucket_id"] == "gemini-5h"
    assert res["route_used"] == "tools_api_8045"

    # Verify audit log in SQLite SSOT
    logs = db.get_warmup_logs("test@gmail.com")
    assert len(logs) == 1
    assert logs[0]["status"] == "success"
    assert logs[0]["model_name"] == "gemini-3-flash"


def test_execute_warmup_http_error_resilience(db):
    cfg = AppConfig()
    sentinel = AntigravityQuotaSentinel(config=cfg, db=db)

    candidate = {
        "account_email": "fail@gmail.com",
        "bucket_id": "3p-5h",
        "model_name": "claude-sonnet-4-6",
        "access_token": "expired_token",
        "project_id": "aicode-consumers",
    }

    # Simulate HTTP 401 Unauthorized
    err = urllib.error.HTTPError(
        url="http://127.0.0.1:8045/internal/warmup",
        code=401,
        msg="Unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"error": "invalid_grant"}'),
    )

    with patch("urllib.request.urlopen", side_effect=err):
        res = sentinel.execute_warmup_sync(candidate, trigger_reason="unit_test")

    assert res["status"] == "failed"
    assert "HTTP 401" in res["error_message"]

    # Verify recorded to DB as failed attempt without crashing
    logs = db.get_warmup_logs("fail@gmail.com")
    assert len(logs) == 1
    assert logs[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_sweep_and_warmup_e2e_with_broker(fake_accounts_dir, db):
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    mock_broker = MagicMock()
    mock_broker.publish = AsyncMock()

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db, broker=mock_broker)

        def fake_urlopen(req, *args, **kwargs):
            resp = io.BytesIO(json.dumps({"success": True}).encode("utf-8"))
            resp.status = 200
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            summary = await sentinel.sweep_and_warmup(reason="test_cycle", force=False, live=False)

    assert summary["accounts_scanned"] == 3
    assert summary["candidates_found"] == 2
    assert summary["warmups_executed"] == 2

    # Verify SSE events were published (2 warmups + 1 quota update)
    assert mock_broker.publish.call_count == 3
    published_events = [call.args[1]["event"] for call in mock_broker.publish.call_args_list]
    assert published_events.count("antigravity_quota_warmup") == 2
    assert published_events.count("antigravity_quota_update") == 1


def test_get_quota_overview_formatting(fake_accounts_dir, db):
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        sentinel.sync_quotas_to_db()
        overview = sentinel.get_quota_overview()

    assert overview["accounts_count"] == 3
    assert "stats" in overview
    assert overview["stats"]["total_5h_buckets"] == 4

    # Check formatting of accounts and countdowns
    viinam = next(a for a in overview["accounts"] if a["email"] == "viinam33@gmail.com")
    assert viinam["is_active_account"] is True
    assert len(viinam["buckets"]) == 3

    b_gemini = next(b for b in viinam["buckets"] if b["bucket_id"] == "gemini-5h")
    assert b_gemini["remaining_percent"] == 100.0
    assert "Ready to Warmup" in b_gemini["human_countdown"]


def test_fleet_wide_warmup_all_accounts_toggle(fake_accounts_dir, db):
    """Test warmup_all_accounts config toggle and all_accounts runtime override."""
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")
    cfg.antigravity_quota.warmup_all_accounts = True

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        # 1. Default: fleet-wide warmup enabled -> evaluates both active and standby
        cands_all = sentinel.evaluate_warmup_candidates(profiles)
        cands_emails = {c["account_email"] for c in cands_all}
        assert "viinam33@gmail.com" in cands_emails
        assert "standby@gmail.com" in cands_emails

        # 2. Config toggle: warmup_all_accounts = False -> only active account
        cfg.antigravity_quota.warmup_all_accounts = False
        cands_active_only = sentinel.evaluate_warmup_candidates(profiles)
        assert len(cands_active_only) == 1
        assert cands_active_only[0]["account_email"] == "viinam33@gmail.com"

        # 3. Runtime override: all_accounts=True overrides config
        cands_override = sentinel.evaluate_warmup_candidates(profiles, all_accounts=True)
        assert len(cands_override) == 2


def test_unstarted_idle_window_detection_near_5h(fake_accounts_dir, db):
    """
    Test that Google's live return of reset_time = query_time + 5h (4h 59m, 17950s in future)
    for 100% full unstarted buckets is correctly detected as ELIGIBLE idle window.
    """
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        now = time.time()
        # Set standby bucket reset_timestamp to now + 17950s (4h 59m 10s in future, unstarted window)
        for p in profiles:
            if p.email == "standby@gmail.com":
                for b in p.buckets:
                    if b.bucket_id == "3p-5h":
                        b.remaining_fraction = 1.0
                        b.reset_timestamp = now + 17950.0

        candidates = sentinel.evaluate_warmup_candidates(profiles)
        standby_cand = next((c for c in candidates if c["account_email"] == "standby@gmail.com"), None)
        assert standby_cand is not None, "Unstarted 5h window sitting at 4h59m must be eligible for warmup"
        assert standby_cand["bucket_id"] == "3p-5h"


def test_mid_flight_window_rejection(fake_accounts_dir, db):
    """
    Test that a bucket sitting mid-flight (reset_timestamp between 60s and 4h50m in future)
    is skipped so active countdowns are not redundantly pinged mid-flight.
    """
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        now = time.time()
        # Set standby bucket to mid-flight: reset in 2 hours (7200s)
        for p in profiles:
            if p.email == "standby@gmail.com":
                for b in p.buckets:
                    if b.bucket_id == "3p-5h":
                        b.remaining_fraction = 1.0
                        b.reset_timestamp = now + 7200.0

        candidates = sentinel.evaluate_warmup_candidates(profiles)
        standby_cand = next((c for c in candidates if c["account_email"] == "standby@gmail.com"), None)
        assert standby_cand is None, "Mid-flight countdown (2h remaining) must be skipped"


def test_cooldown_isolation_across_fleet(fake_accounts_dir, db):
    """
    Test that warming up Account A puts ONLY Account A in cooldown,
    leaving Account B immediately eligible.
    """
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")
    cfg.antigravity_quota.warmup_cooldown_seconds = 17700

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        # Both viinam33 and standby are eligible initially
        cands_before = sentinel.evaluate_warmup_candidates(profiles)
        assert len(cands_before) == 2

        # Record successful warmup only for viinam33
        db.record_warmup_log(
            account_email="viinam33@gmail.com",
            bucket_id="gemini-5h",
            model_name="gemini-3-flash",
            trigger_reason="fleet_test",
            route_used="tools_api_8045",
            status="success",
        )

        # Viinam33 is now on cooldown; standby MUST still be eligible
        cands_after = sentinel.evaluate_warmup_candidates(profiles)
        assert len(cands_after) == 1
        assert cands_after[0]["account_email"] == "standby@gmail.com"


def test_fleet_wide_prioritization_order(fake_accounts_dir, db):
    """
    Test that candidates are prioritized in strict order:
    1. Active account first
    2. Primary Gemini models before Claude/3P models
    3. 5h rolling window before weekly
    """
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")
    cfg.antigravity_quota.auto_warmup_weekly = True  # Enable weekly to test window sorting

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        # Set all buckets to 100% full
        for p in profiles:
            if not p.disabled:
                for b in p.buckets:
                    b.remaining_fraction = 1.0
                    b.reset_timestamp = time.time() - 100

        candidates = sentinel.evaluate_warmup_candidates(profiles)
        assert len(candidates) >= 2

        # First candidate MUST be active account
        assert candidates[0]["is_active"] is True
        assert candidates[0]["account_email"] == "viinam33@gmail.com"
        # Within active account, Gemini MUST come first
        assert "gemini" in candidates[0]["bucket_id"].lower()


def test_remaining_fraction_null_safety(fake_accounts_dir, db):
    """Test that Google API returning null remainingFraction does not crash and defaults safely to 1.0."""
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)

        # Mock urllib response with null remainingFraction
        mock_resp_data = {
            "groups": [{
                "displayName": "Gemini Models",
                "buckets": [{
                    "bucketId": "gemini-5h",
                    "window": "5h",
                    "remainingFraction": None,
                    "resetTime": "2026-09-14T15:00:00Z",
                }]
            }]
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_ctx = MagicMock()
            mock_ctx.status = 200
            mock_ctx.read.return_value = json.dumps(mock_resp_data).encode("utf-8")
            mock_ctx.__enter__.return_value = mock_ctx
            mock_ctx.__exit__.return_value = False
            mock_urlopen.return_value = mock_ctx

            buckets = sentinel.fetch_live_quota("mock_token")
            assert buckets is not None
            assert len(buckets) == 1
            assert buckets[0].remaining_fraction == 1.0


def test_weekly_window_cooldown_7d(fake_accounts_dir, db):
    """Test that weekly buckets enforce a 7-day cooldown rather than 4h55m in SQLite."""
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")
    cfg.antigravity_quota.auto_warmup_weekly = True

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()

        for p in profiles:
            for b in p.buckets:
                b.remaining_fraction = 1.0
                b.reset_timestamp = None

        # Simulate warmup 1 day ago (86400s ago)
        # 1 day > 4h55m (17700s), but < 7 days (604800s)
        yesterday_iso = datetime.now(timezone.utc) - timedelta(days=1)
        yesterday_str = yesterday_iso.strftime("%Y-%m-%d %H:%M:%S")

        db.record_warmup_log(
            account_email="viinam33@gmail.com",
            bucket_id="gemini-weekly",
            model_name="gemini-3-flash",
            trigger_reason="weekly_test",
            route_used="tools_api_8045",
            status="success",
        )
        # Manually update created_at to 1 day ago
        with db._lock:
            db._conn.execute(
                "UPDATE antigravity_warmup_logs SET created_at = ? WHERE account_email = 'viinam33@gmail.com' AND bucket_id = 'gemini-weekly'",
                (yesterday_str,)
            )
            db._conn.commit()

        cands = sentinel.evaluate_warmup_candidates(profiles)
        viinam_weekly = next(
            (c for c in cands if c["account_email"] == "viinam33@gmail.com" and c["bucket_id"] == "gemini-weekly"),
            None
        )
        assert viinam_weekly is None, "Weekly bucket warmed up 1 day ago must still be in cooldown"


@pytest.mark.asyncio
async def test_sweep_and_warmup_dry_run(fake_accounts_dir, db):
    """Test that sweep_and_warmup with dry_run=True evaluates candidates without dispatching pings."""
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)

        with patch.object(sentinel, "execute_warmup") as mock_warmup:
            res = await sentinel.sweep_and_warmup(
                reason="dry_run_test",
                force=True,
                live=False,
                dry_run=True,
            )
            assert res["dry_run"] is True
            assert res["warmups_executed"] == 0
            assert len(res["warmup_candidates"]) > 0
            assert mock_warmup.call_count == 0


def test_get_quota_overview_account_sorting(fake_accounts_dir, db):
    """Test that get_quota_overview always guarantees active account is at index 0."""
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        profiles = sentinel.scan_accounts()
        sentinel.sync_quotas_to_db(profiles)

        overview = sentinel.get_quota_overview()
        accounts = overview["all_accounts"]
        assert len(accounts) >= 2
        assert accounts[0]["is_active_account"] is True
        assert accounts[0]["email"] == "viinam33@gmail.com"


def test_prune_stale_quota_snapshots_db(db):
    """Direct verification of db.prune_stale_quota_snapshots: removes stale rows, keeps active, handles empty/case-insensitivity."""
    # Seed 3 accounts
    db.upsert_quota_snapshot("alpha@test.com", "gemini-5h", "Gemini Models", "5h", 1.0)
    db.upsert_quota_snapshot("alpha@test.com", "gemini-weekly", "Gemini Models", "weekly", 0.5)
    db.upsert_quota_snapshot("beta@test.com", "3p-5h", "Claude and GPT models", "5h", 0.8)
    db.upsert_quota_snapshot("gamma@test.com", "gemini-5h", "Gemini Models", "5h", 1.0)

    assert len(db.get_quota_snapshots()) == 4
    assert db.get_quota_summary_stats()["total_accounts"] == 3

    # 1. Prune with None -> no-op
    assert db.prune_stale_quota_snapshots(None) == 0
    assert len(db.get_quota_snapshots()) == 4

    # 2. Prune removing beta@test.com with case variation
    pruned = db.prune_stale_quota_snapshots({"ALPHA@TEST.COM", "gamma@test.com"})
    assert pruned == 1
    remaining = db.get_quota_snapshots()
    assert len(remaining) == 3
    remaining_emails = {r["account_email"] for r in remaining}
    assert "beta@test.com" not in remaining_emails
    assert "alpha@test.com" in remaining_emails
    assert "gamma@test.com" in remaining_emails

    # 3. Prune removing gamma
    pruned2 = db.prune_stale_quota_snapshots({"alpha@test.com"})
    assert pruned2 == 1
    assert len(db.get_quota_snapshots()) == 2

    # 4. Prune with empty set -> deletes all
    pruned3 = db.prune_stale_quota_snapshots(set())
    assert pruned3 == 2
    assert len(db.get_quota_snapshots()) == 0


@pytest.mark.asyncio
async def test_dynamic_account_addition(fake_accounts_dir, db):
    """
    Test dynamic ingestion on account addition:
    When a new account JSON is placed in ~/.antigravity_tools/accounts/*.json,
    the sentinel sweep automatically discovers it, syncs snapshots to SQLite SSOT,
    and includes it in warmup candidates without server restart.
    """
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")
    cfg.antigravity_quota.auto_warmup_5h = True

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)

        # Baseline: initial sweep with 3 accounts
        with patch.object(sentinel, "execute_warmup", new_callable=AsyncMock) as mock_warmup:
            mock_warmup.return_value = {"status": "success", "account_email": "test", "bucket_id": "b1"}
            init_res = await sentinel.sweep_and_warmup(reason="initial_test", force=True, live=False)
            assert init_res["accounts_scanned"] == 3
            assert db.get_quota_summary_stats()["total_accounts"] == 3

        # Dynamically add a 4th account JSON on disk
        acc4_path = fake_accounts_dir / "accounts" / "acc-new-004.json"
        acc4_data = {
            "id": "acc-new-004",
            "email": "brand_new_user@gmail.com",
            "name": "Brand New",
            "disabled": False,
            "token": {
                "access_token": "token_brand_new",
                "project_id": "aicode-consumers",
                "expiry_timestamp": time.time() + 7200,
            },
            "quota": {
                "subscription_tier": "Google AI Pro",
                "last_updated": int(time.time()),
                "quota_groups": [
                    {
                        "name": "group_gemini",
                        "buckets": [
                            {
                                "bucket_id": "gemini-5h",
                                "window": "5h",
                                "remaining_fraction": 1.0,
                                "reset_time": "2026-09-14T00:00:00Z",
                            }
                        ],
                    }
                ],
            },
        }
        with open(acc4_path, "w", encoding="utf-8") as f:
            json.dump(acc4_data, f)

        # Next periodic sweep fires: immediately discovers the new account
        with patch.object(sentinel, "execute_warmup", new_callable=AsyncMock) as mock_warmup:
            mock_warmup.return_value = {
                "status": "success",
                "account_email": "brand_new_user@gmail.com",
                "bucket_id": "gemini-5h",
            }
            res = await sentinel.sweep_and_warmup(reason="periodic_sweep_after_add", force=True, live=False)

            # Assert dynamic discovery and SSOT persistence
            assert res["accounts_scanned"] == 4
            assert res["stale_snapshots_pruned"] == 0
            assert db.get_quota_summary_stats()["total_accounts"] == 4

            # Verify the new account is present in DB snapshots
            new_snaps = db.get_quota_snapshots("brand_new_user@gmail.com")
            assert len(new_snaps) == 1
            assert new_snaps[0]["bucket_id"] == "gemini-5h"
            assert new_snaps[0]["remaining_fraction"] == 1.0

            # Verify the new account is reflected in overview
            overview = sentinel.get_quota_overview()
            assert overview["accounts_count"] == 4
            found_emails = [a["email"] for a in overview["accounts"]]
            assert "brand_new_user@gmail.com" in found_emails

            # Verify it was dispatched for warmup
            warmed_emails = [call.args[0]["account_email"] for call in mock_warmup.call_args_list]
            assert "brand_new_user@gmail.com" in warmed_emails


@pytest.mark.asyncio
async def test_dynamic_account_removal_and_pruning(fake_accounts_dir, db):
    """
    Test dynamic pruning on account removal (Ghost Account Prevention):
    When an account JSON is deleted or unlinked from ~/.antigravity_tools/accounts/*.json,
    the periodic sweep detects it is no longer on disk, and proactively prunes its stale
    snapshot records from SQLite SSOT, preventing ghost accounts in Web dashboard and CLI.
    """
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)

        # Baseline: sweep with 3 accounts
        with patch.object(sentinel, "execute_warmup", new_callable=AsyncMock):
            res_init = await sentinel.sweep_and_warmup(reason="setup", live=False)
            assert res_init["accounts_scanned"] == 3
            assert db.get_quota_summary_stats()["total_accounts"] == 3
            assert len(db.get_quota_snapshots("standby@gmail.com")) == 1

        # User deletes acc2 (standby@gmail.com) from disk
        acc2_path = fake_accounts_dir / "accounts" / "acc-standby-002.json"
        assert acc2_path.is_file()
        acc2_path.unlink()
        assert not acc2_path.exists()

        # Next periodic sweep runs: detects account is gone and prunes SQLite SSOT
        with patch.object(sentinel, "execute_warmup", new_callable=AsyncMock):
            res_sweep = await sentinel.sweep_and_warmup(reason="periodic_sweep_after_remove", live=False)

            assert res_sweep["accounts_scanned"] == 2
            assert res_sweep["stale_snapshots_pruned"] == 1
            assert db.get_quota_summary_stats()["total_accounts"] == 2

            # Deleted account snapshots are completely purged
            stale_snaps = db.get_quota_snapshots("standby@gmail.com")
            assert len(stale_snaps) == 0

            # Overview contains NO ghost accounts
            overview = sentinel.get_quota_overview()
            assert overview["accounts_count"] == 2
            overview_emails = [a["email"] for a in overview["accounts"]]
            assert "standby@gmail.com" not in overview_emails
            assert "viinam33@gmail.com" in overview_emails

            # No phantom warmup can target the deleted account
            cands = sentinel.evaluate_warmup_candidates(force=True)
            cand_emails = [c["account_email"] for c in cands]
            assert "standby@gmail.com" not in cand_emails


def test_prune_stale_accounts_sentinel_helper(fake_accounts_dir, db):
    """Test sentinel.prune_stale_accounts method directly."""
    cfg = AppConfig()
    cfg.antigravity_quota.accounts_dir = str(fake_accounts_dir / "accounts")

    with patch("pathlib.Path.home", return_value=fake_accounts_dir):
        sentinel = AntigravityQuotaSentinel(config=cfg, db=db)
        sentinel.sync_quotas_to_db()

        # Inject a ghost snapshot not on disk
        db.upsert_quota_snapshot("ghost@defunct.com", "gemini-5h", "Gemini Models", "5h", 1.0)
        assert len(db.get_quota_snapshots("ghost@defunct.com")) == 1
        assert db.get_quota_summary_stats()["total_accounts"] == 4

        # Run prune_stale_accounts helper
        pruned = sentinel.prune_stale_accounts()
        assert pruned == 1
        assert len(db.get_quota_snapshots("ghost@defunct.com")) == 0
        assert db.get_quota_summary_stats()["total_accounts"] == 3



