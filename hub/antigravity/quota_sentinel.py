"""
Antigravity Webhook Hub — Antigravity Quota Sentinel & 5h Rolling Window Warmup Engine
Provides automated background monitoring of Google Antigravity 5-hour and weekly rolling quotas,
eliminating the passive countdown trap by autonomously dispatching minimal token pings
immediately when quota windows reset to 100%, keeping rolling refresh cycles active.

Key Capabilities:
1. Multi-Account Discovery: Parses Antigravity Tools accounts (~/.antigravity_tools/accounts/*.json),
   identifying active account, subscription tier, and token states.
2. Dual Quota Group Tracking: Accurately tracks Gemini Models and Claude/GPT 3P models across
   5-hour and weekly buckets.
3. Idle Window Detection: Detects 100% full buckets with expired/idle reset timestamps where the 5-hour
   countdown timer has halted.
4. Autonomous Warmup Dispatch: Dispatches a 1-token ping (gemini-3-flash / claude-sonnet-4-6) via local
   Antigravity Tools API (port 8045) or AgentAPI fallback, immediately restarting the 5h countdown.
5. Cooldown & Rate Limiting: Strict 4h55m cooldown per bucket prevents runaway ping loops.
6. SQLite SSOT & Observability: Records all snapshots and warmup logs to SQLite database
   and broadcasts real-time SSE updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from hub.config import AppConfig
    from hub.db import DatabaseManager

logger = logging.getLogger("hub.antigravity.quota_sentinel")


@dataclass(slots=True)
class QuotaBucketInfo:
    bucket_id: str
    model_group: str
    window_type: str  # '5h' or 'weekly'
    remaining_fraction: float  # 0.0 to 1.0
    reset_time: Optional[str] = None
    reset_timestamp: Optional[float] = None
    display_name: Optional[str] = None
    server_description: Optional[str] = None
    is_gemini_primary: bool = False


@dataclass(slots=True)
class AccountQuotaProfile:
    account_id: str
    email: str
    is_active: bool
    subscription_tier: Optional[str] = None
    last_updated: Optional[float] = None
    disabled: bool = False
    proxy_disabled: bool = False
    project_id: Optional[str] = None
    access_token: Optional[str] = None
    token_expiry: Optional[float] = None
    buckets: list[QuotaBucketInfo] = field(default_factory=list)
    live_fetched: bool = False


def parse_iso_timestamp(ts_str: Optional[str]) -> Optional[float]:
    """Parse an ISO 8601 / RFC 3339 / SQLite CURRENT_TIMESTAMP string into unix epoch timestamp float."""
    if not ts_str:
        return None
    try:
        clean_str = ts_str.strip()
        if clean_str.endswith("Z"):
            clean_str = clean_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            # SQLite CURRENT_TIMESTAMP stores UTC without tzinfo
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception as e:
        logger.debug("Failed parsing timestamp '%s': %s", ts_str, e)
        return None


class AntigravityQuotaSentinel:
    """
    Antigravity Quota Sentinel & Automated 5h Warmup Engine.
    Monitors account quotas and dispatches minimal token pings to keep the 5-hour
    countdown rolling continuously.
    """

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        db: Optional[DatabaseManager] = None,
        broker: Optional[Any] = None,
    ):
        self.config = config
        self.db = db
        self.broker = broker
        self._lock = asyncio.Lock()
        self._last_sweep_time: float = 0.0
        self._last_pruned_count: int = 0
        self._last_scan_error_count: int = 0

    @property
    def quota_cfg(self) -> Any:
        return getattr(self.config, "antigravity_quota", None)

    @property
    def is_enabled(self) -> bool:
        if self.quota_cfg is None:
            return True
        return bool(getattr(self.quota_cfg, "enabled", True))

    def _resolve_accounts_dir(self) -> Path:
        """Resolve directory containing ~/.antigravity_tools/accounts/*.json."""
        if self.quota_cfg and getattr(self.quota_cfg, "accounts_dir", None):
            custom_dir = Path(self.quota_cfg.accounts_dir).expanduser().resolve()
            if custom_dir.is_dir():
                return custom_dir

        default_dir = Path.home() / ".antigravity_tools" / "accounts"
        return default_dir

    def _resolve_active_account_id(self) -> Optional[str]:
        """Read ~/.antigravity_tools/accounts.json to find current_account_id."""
        accounts_dir = self._resolve_accounts_dir()
        candidate_paths = [
            accounts_dir.parent / "accounts.json",
            Path.home() / ".antigravity_tools" / "accounts.json",
        ]
        for p in candidate_paths:
            if p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    act_id = data.get("current_account_id")
                    if act_id:
                        return act_id
                except Exception as e:
                    logger.debug("Failed reading %s for active id: %s", p, e)
        return None

    def fetch_live_quota(
        self,
        access_token: Optional[str],
        timeout: float = 3.5,
    ) -> Optional[list[QuotaBucketInfo]]:
        """
        Query upstream Google Cloud Code PA endpoint (v1internal:retrieveUserQuotaSummary)
        to fetch authoritative dynamic live quota buckets.
        Returns None on network error, timeout, or unauthorized response.
        """
        if not access_token:
            return None

        url = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity-tools/1.0",
        }
        try:
            req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug("Live quota fetch from cloudcode-pa returned error: %s", e)
            return None

        groups = data.get("groups") or []
        if not groups:
            return None

        live_buckets: list[QuotaBucketInfo] = []
        for g in groups:
            group_name = g.get("displayName") or "Unknown Group"
            is_gemini_group = "gemini" in group_name.lower()

            for b in g.get("buckets") or []:
                b_id = str(b.get("bucketId") or "")
                if not b_id:
                    continue
                window = str(b.get("window") or "5h").lower()
                rem_raw = b.get("remainingFraction") if b.get("remainingFraction") is not None else b.get("remaining_fraction")
                rem_frac = float(rem_raw) if rem_raw is not None else 1.0
                reset_time_str = b.get("resetTime")
                reset_ts = parse_iso_timestamp(reset_time_str)
                d_name = b.get("displayName")
                desc = b.get("description")

                b_is_gemini = is_gemini_group or ("gemini" in b_id.lower())

                live_buckets.append(
                    QuotaBucketInfo(
                        bucket_id=b_id,
                        model_group=group_name,
                        window_type=window,
                        remaining_fraction=rem_frac,
                        reset_time=reset_time_str,
                        reset_timestamp=reset_ts,
                        display_name=d_name,
                        server_description=desc,
                        is_gemini_primary=b_is_gemini,
                    )
                )

        # Ensure Gemini models are sorted FIRST, then 5h window ahead of weekly
        live_buckets.sort(key=lambda b: (not b.is_gemini_primary, b.window_type != "5h", b.bucket_id))
        return live_buckets

    def scan_accounts(self, live: bool = True) -> list[AccountQuotaProfile]:
        """
        Synchronously scan all account JSON files from ~/.antigravity_tools/accounts/.
        Extracts token credentials and quota group buckets.
        When live=True, dynamically queries Google Cloud Code PA endpoint for active account.
        """
        accounts_dir = self._resolve_accounts_dir()
        if not accounts_dir.is_dir():
            logger.debug("Accounts directory does not exist: %s", accounts_dir)
            return []

        active_id = self._resolve_active_account_id()
        profiles_by_email: dict[str, AccountQuotaProfile] = {}
        self._last_scan_error_count = 0

        for json_path in sorted(accounts_dir.glob("*.json")):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                self._last_scan_error_count += 1
                logger.warning("Failed loading account JSON '%s': %s", json_path.name, e)
                continue

            acc_id = str(data.get("id") or json_path.stem)
            email = str(data.get("email") or "").strip()
            if not email:
                continue

            is_active = (acc_id == active_id)
            disabled = bool(data.get("disabled", False))
            proxy_disabled = bool(data.get("proxy_disabled", False))

            token_data = data.get("token") or {}
            access_token = token_data.get("access_token")
            project_id = token_data.get("project_id") or "aicode-consumers"
            token_expiry = token_data.get("expiry_timestamp")

            quota_data = data.get("quota") or {}
            subscription_tier = quota_data.get("subscription_tier")
            last_updated = quota_data.get("last_updated")

            buckets: list[QuotaBucketInfo] = []
            quota_groups = quota_data.get("quota_groups") or []

            for qg in quota_groups:
                for b in qg.get("buckets") or []:
                    bucket_id = str(b.get("bucket_id") or "")
                    if not bucket_id:
                        continue
                    window = str(b.get("window") or "5h").lower()
                    rem_raw = b.get("remainingFraction") if b.get("remainingFraction") is not None else b.get("remaining_fraction")
                    rem_frac = float(rem_raw) if rem_raw is not None else 1.0
                    reset_time_str = b.get("resetTime") or b.get("reset_time")
                    reset_ts = parse_iso_timestamp(reset_time_str)
                    display_name = b.get("displayName") or b.get("display_name")
                    desc = b.get("description")

                    # Deduce model group
                    if "gemini" in bucket_id.lower():
                        model_group = "Gemini Models"
                        is_gemini = True
                    elif "3p" in bucket_id.lower() or "claude" in bucket_id.lower() or "gpt" in bucket_id.lower():
                        model_group = "Claude and GPT models"
                        is_gemini = False
                    else:
                        model_group = "Other Models"
                        is_gemini = False

                    buckets.append(
                        QuotaBucketInfo(
                            bucket_id=bucket_id,
                            model_group=model_group,
                            window_type=window,
                            remaining_fraction=rem_frac,
                            reset_time=reset_time_str,
                            reset_timestamp=reset_ts,
                            display_name=display_name,
                            server_description=desc,
                            is_gemini_primary=is_gemini,
                        )
                    )

            # Sort buckets with Gemini first
            buckets.sort(key=lambda b: (not b.is_gemini_primary, b.window_type != "5h", b.bucket_id))

            profile = AccountQuotaProfile(
                account_id=acc_id,
                email=email,
                is_active=is_active,
                subscription_tier=subscription_tier,
                last_updated=last_updated,
                disabled=disabled,
                proxy_disabled=proxy_disabled,
                project_id=project_id,
                access_token=access_token,
                token_expiry=token_expiry,
                buckets=buckets,
                live_fetched=False,
            )

            # Deterministic deduplication by normalized email:
            # Prefer active account -> enabled account -> newest last_updated
            norm_email = email.lower()
            if norm_email in profiles_by_email:
                existing = profiles_by_email[norm_email]
                replace = False
                if profile.is_active and not existing.is_active:
                    replace = True
                elif not profile.is_active and existing.is_active:
                    replace = False
                elif not profile.disabled and existing.disabled:
                    replace = True
                elif (profile.last_updated or 0) > (existing.last_updated or 0):
                    replace = True

                if replace:
                    logger.debug("Deduplicating accounts: selecting %s over %s for %s", profile.account_id, existing.account_id, email)
                    profiles_by_email[norm_email] = profile
                else:
                    logger.debug("Deduplicating accounts: retaining %s over %s for %s", existing.account_id, profile.account_id, email)
            else:
                profiles_by_email[norm_email] = profile

        profiles = list(profiles_by_email.values())

        # If live querying is enabled, attempt concurrent live fetch across accounts
        if live and profiles:
            import concurrent.futures

            def _fetch_profile_live(prof: AccountQuotaProfile) -> None:
                try:
                    if not prof.access_token or prof.disabled:
                        return
                    is_expired = bool(prof.token_expiry and prof.token_expiry < time.time())
                    if prof.is_active or not is_expired:
                        timeout_val = 3.5 if prof.is_active else 2.0
                        live_b = self.fetch_live_quota(prof.access_token, timeout=timeout_val)
                        if live_b:
                            prof.buckets = live_b
                            prof.live_fetched = True
                            prof.last_updated = time.time()
                except Exception as e:
                    logger.debug("Failed live quota fetch for %s: %s", prof.email, e)

            max_workers = min(8, max(1, len(profiles)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(_fetch_profile_live, profiles))

        # Sort with active account first, then alphabetically by email
        profiles.sort(key=lambda p: (not p.is_active, p.email))
        return profiles

    def prune_stale_accounts(self, current_emails: Optional[set[str]] = None) -> int:
        """
        Remove quota snapshots for accounts that no longer exist on disk.
        If current_emails is None, scans accounts directory to find current emails on disk.
        Returns number of deleted snapshot rows.
        """
        if not self.db:
            return 0
        if current_emails is None:
            accounts_dir = self._resolve_accounts_dir()
            if not accounts_dir.is_dir():
                return 0
            profiles = self.scan_accounts(live=False)
            if getattr(self, "_last_scan_error_count", 0) > 0:
                logger.warning(
                    "Skipping prune_stale_accounts: %d account file(s) failed to parse on disk",
                    self._last_scan_error_count,
                )
                return 0
            current_emails = {p.email for p in profiles if p.email}
        pruned = self.db.prune_stale_quota_snapshots(current_emails)
        self._last_pruned_count = pruned
        return pruned

    def sync_quotas_to_db(
        self,
        profiles: Optional[list[AccountQuotaProfile]] = None,
        prune_stale: bool = True,
    ) -> int:
        """
        Persist scanned account quota snapshots into SQLite SSOT.
        When prune_stale=True (default), also removes stale snapshot records
        for accounts that are no longer present on disk, preventing ghost accounts.
        Returns number of bucket snapshots upserted.
        """
        if not self.db:
            return 0

        accounts_dir = self._resolve_accounts_dir()
        dir_exists = accounts_dir.is_dir()

        full_fleet_scan = (profiles is None)
        if profiles is None:
            profiles = self.scan_accounts()

        upserted = 0
        for p in profiles:
            for b in p.buckets:
                try:
                    self.db.upsert_quota_snapshot(
                        account_email=p.email,
                        bucket_id=b.bucket_id,
                        model_group=b.model_group,
                        window_type=b.window_type,
                        remaining_fraction=b.remaining_fraction,
                        reset_time=b.reset_time,
                        reset_timestamp=b.reset_timestamp,
                        subscription_tier=p.subscription_tier,
                        is_active_account=p.is_active,
                        project_id=p.project_id,
                        raw_summary_json=json.dumps(asdict(b)),
                    )
                    upserted += 1
                except Exception as e:
                    logger.error("Failed to sync quota snapshot for %s/%s: %s", p.email, b.bucket_id, e)

        # Dynamic Pruning on Account Removal (Ghost Account Prevention):
        # Prune if prune_stale is True AND accounts directory exists on disk
        if prune_stale and dir_exists:
            # If files on disk had read/parse errors, skip pruning to prevent accidental deletion
            if getattr(self, "_last_scan_error_count", 0) > 0:
                logger.warning(
                    "Skipping dynamic account pruning: %d account file(s) failed to parse on disk",
                    self._last_scan_error_count,
                )
                self._last_pruned_count = 0
            else:
                if full_fleet_scan:
                    current_emails = {p.email for p in profiles if p.email}
                else:
                    # Partial sync passed by caller: query disk for current accounts
                    # so valid accounts on disk are NOT purged
                    disk_profiles = self.scan_accounts(live=False)
                    if getattr(self, "_last_scan_error_count", 0) > 0:
                        logger.warning("Skipping dynamic account pruning due to disk parse error during partial sync")
                        self._last_pruned_count = 0
                        return upserted
                    current_emails = {p.email for p in disk_profiles if p.email}
                    current_emails.update(p.email for p in profiles if p.email)

                try:
                    pruned = self.db.prune_stale_quota_snapshots(current_emails)
                    self._last_pruned_count = pruned
                    if pruned > 0:
                        logger.info("Dynamic pruning: removed %d stale quota snapshots from SQLite SSOT", pruned)
                except Exception as prune_err:
                    logger.error("Failed auto-pruning stale quota snapshots: %s", prune_err)
        else:
            self._last_pruned_count = 0

        return upserted

    def evaluate_warmup_candidates(
        self,
        profiles: Optional[list[AccountQuotaProfile]] = None,
        account_email: Optional[str] = None,
        bucket_id: Optional[str] = None,
        force: bool = False,
        all_accounts: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        """
        Identify buckets eligible for minimal token warmup across the fleet.
        Eligibility Criteria:
        1. auto_warmup_5h is True (and window_type == '5h'), or auto_warmup_weekly is True.
        2. Bucket has remaining_fraction >= 0.999 (meaning 100% full, window has reset and halted).
        3. Mid-flight countdown check: skips windows actively counting down mid-flight outside the hub.
        4. Cooldown: Strict 4h55m cooldown per account per bucket against SQLite SSOT (unless force=True).
        5. Fleet-wide vs active-only: Configurable via warmup_all_accounts (default True).
        """
        if profiles is None:
            profiles = self.scan_accounts()

        cooldown_sec = 17700  # Default 4h 55m
        auto_5h = True
        auto_weekly = False
        warmup_all = True
        if self.quota_cfg:
            cooldown_sec = getattr(self.quota_cfg, "warmup_cooldown_seconds", 17700)
            auto_5h = getattr(self.quota_cfg, "auto_warmup_5h", True)
            auto_weekly = getattr(self.quota_cfg, "auto_warmup_weekly", False)
            warmup_all = getattr(self.quota_cfg, "warmup_all_accounts", True)

        if all_accounts is not None:
            warmup_all = bool(all_accounts)

        now = time.time()
        candidates: list[dict[str, Any]] = []

        for p in profiles:
            # Skip accounts explicitly marked as disabled or proxy_disabled (e.g. captcha/403/stale)
            if p.disabled or p.proxy_disabled:
                logger.debug(
                    "Skipping disabled/proxy_disabled account %s (disabled=%s, proxy_disabled=%s)",
                    p.email, p.disabled, p.proxy_disabled,
                )
                continue

            # If fleet-wide warmup is disabled and no specific account was targeted, only evaluate active account
            if not warmup_all and not account_email and not p.is_active:
                continue

            # Check specific account email filter if provided
            if account_email and p.email.lower() != account_email.strip().lower():
                continue

            # Account must have an access token to dispatch warmup
            if not p.access_token:
                continue

            for b in p.buckets:
                if bucket_id and b.bucket_id.lower() != bucket_id.strip().lower():
                    continue

                # 1. Window type filter
                if b.window_type == "5h" and not auto_5h and not force:
                    continue
                if b.window_type == "weekly" and not auto_weekly and not force:
                    continue

                # 2. Remaining fraction check: must be at 100% (or forced)
                if not force and b.remaining_fraction < 0.999:
                    # Timer is already running and has consumed quota, skip
                    continue

                # 3. Cooldown check against SQLite SSOT (strict 4h55m cooldown for 5h, ~7d for weekly)
                effective_cooldown = cooldown_sec if b.window_type == "5h" else max(cooldown_sec, 604500)
                if not force and self.db:
                    latest = self.db.get_latest_warmup(p.email, b.bucket_id)
                    if latest and latest.get("status") == "success":
                        created_at = latest.get("created_at")
                        warmup_ts = parse_iso_timestamp(created_at) if isinstance(created_at, str) else None
                        if warmup_ts and (now - warmup_ts) < effective_cooldown:
                            logger.debug(
                                "Bucket %s/%s cooldown active (elapsed %.0fs < %ds), skipping",
                                p.email, b.bucket_id, now - warmup_ts, effective_cooldown
                            )
                            continue
                    elif latest and latest.get("status") == "failed":
                        created_at = latest.get("created_at")
                        warmup_ts = parse_iso_timestamp(created_at) if isinstance(created_at, str) else None
                        # 15m (900s) failure backoff to prevent rapid retries on invalid tokens/401/429
                        if warmup_ts and (now - warmup_ts) < 900.0:
                            logger.debug(
                                "Bucket %s/%s recent warmup failed (elapsed %.0fs < 900s), in failure backoff, skipping",
                                p.email, b.bucket_id, now - warmup_ts
                            )
                            continue

                # 4. Mid-flight countdown check:
                # If window is mid-flight (e.g. started 1-4 hours ago outside this hub or before restart),
                # reset_timestamp will be counting down in the future (between 60s and 4h50m).
                # Unstarted/idle windows have reset_timestamp >= now + 17400s (Google sets to query time + 5h),
                # or reset_timestamp in the past / expired / None.
                if not force and b.reset_timestamp is not None:
                    time_until_reset = b.reset_timestamp - now
                    if b.window_type == "5h":
                        if 60.0 < time_until_reset < (18000.0 - 300.0):
                            logger.debug(
                                "Bucket %s/%s mid-flight countdown active (reset in %.0fs), skipping",
                                p.email, b.bucket_id, time_until_reset
                            )
                            continue
                    elif b.window_type == "weekly":
                        if 60.0 < time_until_reset < (7 * 86400.0 - 300.0):
                            logger.debug(
                                "Weekly bucket %s/%s mid-flight countdown active (reset in %.0fs), skipping",
                                p.email, b.bucket_id, time_until_reset
                            )
                            continue

                # 5. Exhausted weekly pool guard:
                # If the corresponding weekly quota pool for this model group is exhausted (0.0%),
                # Google Cloud Code PA returns HTTP 429. Skip until the weekly window resets.
                if not force and b.window_type == "5h":
                    weekly_b = next(
                        (other for other in p.buckets if other.model_group == b.model_group and other.window_type == "weekly"),
                        None
                    )
                    if weekly_b and weekly_b.remaining_fraction <= 0.001:
                        logger.debug(
                            "Account %s group '%s' weekly limit exhausted (%.1f%%), skipping warmup",
                            p.email, b.model_group, weekly_b.remaining_fraction * 100
                        )
                        continue

                # Determine model to use
                if "gemini" in b.bucket_id.lower():
                    model = getattr(self.quota_cfg, "default_gemini_model", "gemini-3-flash") if self.quota_cfg else "gemini-3-flash"
                else:
                    model = getattr(self.quota_cfg, "default_3p_model", "claude-sonnet-4-6") if self.quota_cfg else "claude-sonnet-4-6"

                candidates.append({
                    "account_id": p.account_id,
                    "account_email": p.email,
                    "is_active": p.is_active,
                    "bucket_id": b.bucket_id,
                    "window_type": b.window_type,
                    "model_group": b.model_group,
                    "model_name": model,
                    "access_token": p.access_token,
                    "project_id": p.project_id or "aicode-consumers",
                    "remaining_fraction": b.remaining_fraction,
                    "reset_time": b.reset_time,
                    "reset_timestamp": b.reset_timestamp,
                })

        # Sort candidates prioritizing active account first, then Gemini models first, then 5h window
        candidates.sort(
            key=lambda c: (
                not c["is_active"],
                not ("gemini" in c["bucket_id"].lower()),
                c["window_type"] != "5h",
                c["bucket_id"],
            )
        )
        return candidates

    def execute_warmup_sync(self, candidate: dict[str, Any], trigger_reason: str = "5h_window_reset") -> dict[str, Any]:
        """
        Synchronously dispatch a warmup ping for a candidate bucket.
        Primary route: HTTP POST to Antigravity Tools port 8045 (/internal/warmup).
        """
        host = getattr(self.quota_cfg, "tools_api_host", "127.0.0.1") if self.quota_cfg else "127.0.0.1"
        port = getattr(self.quota_cfg, "tools_api_port", 8045) if self.quota_cfg else 8045
        timeout = getattr(self.quota_cfg, "tools_api_timeout_seconds", 15.0) if self.quota_cfg else 15.0

        email = candidate["account_email"]
        bucket_id = candidate["bucket_id"]
        model = candidate["model_name"]
        access_token = candidate.get("access_token")
        project_id = candidate.get("project_id", "aicode-consumers")

        url = f"http://{host}:{port}/internal/warmup"
        payload = {
            "email": email,
            "model": model,
            "access_token": access_token or "",
            "project_id": project_id,
        }

        t0 = time.time()
        duration_ms = 0
        status = "failed"
        error_msg = None
        route_used = "tools_api_8045"

        try:
            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                duration_ms = int((time.time() - t0) * 1000)
                resp_bytes = resp.read()
                try:
                    resp_json = json.loads(resp_bytes.decode("utf-8"))
                    if resp_json.get("success", False):
                        status = "success"
                    else:
                        error_msg = resp_json.get("message") or resp_json.get("error") or "Unknown error"
                except Exception:
                    status = "success" if resp.status == 200 else "failed"

        except urllib.error.HTTPError as he:
            duration_ms = int((time.time() - t0) * 1000)
            err_body = he.read().decode("utf-8", errors="replace")
            error_msg = f"HTTP {he.code}: {err_body[:200]}"
            logger.warning("Warmup HTTP error for %s (%s): %s", email, model, error_msg)
        except Exception as e:
            duration_ms = int((time.time() - t0) * 1000)
            error_msg = str(e)
            logger.warning("Warmup connection error for %s (%s): %s", email, model, error_msg)

        # Record audit log to SQLite SSOT
        log_record: dict[str, Any] = {}
        if self.db:
            try:
                log_record = self.db.record_warmup_log(
                    account_email=email,
                    bucket_id=bucket_id,
                    model_name=model,
                    trigger_reason=trigger_reason,
                    route_used=route_used,
                    status=status,
                    error_message=error_msg,
                    duration_ms=duration_ms,
                )
            except Exception as e:
                logger.error("Failed to record warmup log to DB: %s", e)

        result = {
            "account_email": email,
            "bucket_id": bucket_id,
            "model_name": model,
            "model_used": model,
            "trigger_reason": trigger_reason,
            "route_used": route_used,
            "status": status,
            "error_message": error_msg,
            "error": error_msg,
            "duration_ms": duration_ms,
            "log_id": log_record.get("warmup_id"),
        }
        return result

    async def execute_warmup(self, candidate: dict[str, Any], trigger_reason: str = "5h_window_reset") -> dict[str, Any]:
        """Asynchronously execute warmup without blocking event loop."""
        result = await asyncio.to_thread(self.execute_warmup_sync, candidate, trigger_reason)

        # Publish SSE broker event if broker is attached
        if self.broker:
            try:
                await self.broker.publish("events", {
                    "event": "antigravity_quota_warmup",
                    "type": "antigravity_quota_warmup",
                    "account_email": result["account_email"],
                    "bucket_id": result["bucket_id"],
                    "model_name": result["model_name"],
                    "model_used": result["model_used"],
                    "status": result["status"],
                    "duration_ms": result["duration_ms"],
                    "timestamp": time.time(),
                })
            except Exception as b_err:
                logger.debug("Failed publishing warmup event to broker: %s", b_err)

        return result

    async def sweep_and_warmup(
        self,
        reason: str = "periodic_sweep",
        account_email: Optional[str] = None,
        bucket_id: Optional[str] = None,
        force: bool = False,
        live: bool = True,
        all_accounts: Optional[bool] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Main execution loop for Sentinel:
        1. Scans account profiles (authoritative live query when live=True).
        2. Syncs quotas to SQLite SSOT.
        3. Evaluates warmup candidates across fleet (Gemini models prioritized).
        4. Dispatches warmup pings (skipped when dry_run=True).
        5. Emits SSE real-time push events.
        6. Returns structured summary.
        """
        async with self._lock:
            t0 = time.time()
            profiles = await asyncio.to_thread(self.scan_accounts, live)
            upserted = await asyncio.to_thread(self.sync_quotas_to_db, profiles)
            candidates = self.evaluate_warmup_candidates(
                profiles,
                account_email=account_email,
                bucket_id=bucket_id,
                force=force,
                all_accounts=all_accounts,
            )

            warmup_results: list[dict[str, Any]] = []
            if not dry_run:
                for cand in candidates:
                    res = await self.execute_warmup(cand, trigger_reason=reason)
                    warmup_results.append(res)
                    # Small yield between multiple account pings
                    await asyncio.sleep(0.05)

            self._last_sweep_time = time.time()
            total_duration_ms = int((self._last_sweep_time - t0) * 1000)

            summary = {
                "reason": reason,
                "dry_run": dry_run,
                "accounts_scanned": len(profiles),
                "snapshots_upserted": upserted,
                "stale_snapshots_pruned": getattr(self, "_last_pruned_count", 0),
                "candidates_found": len(candidates),
                "warmup_candidates": candidates if dry_run else [],
                "warmups_executed": len(warmup_results),
                "warmup_results": warmup_results,
                "duration_ms": total_duration_ms,
                "timestamp": self._last_sweep_time,
            }

            if self.broker:
                try:
                    await self.broker.publish("events", {
                        "event": "antigravity_quota_update",
                        "type": "antigravity_quota_update",
                        "reason": reason,
                        "accounts_scanned": len(profiles),
                        "stale_snapshots_pruned": getattr(self, "_last_pruned_count", 0),
                        "warmups_executed": len(warmup_results),
                        "timestamp": self._last_sweep_time,
                    })
                except Exception as b_err:
                    logger.debug("Failed publishing quota update event: %s", b_err)

            if warmup_results:
                logger.info(
                    "Antigravity Quota Sentinel (%s): Dispatched %d warmups in %dms",
                    reason, len(warmup_results), total_duration_ms
                )

            import gc
            gc.collect(1)
            from hub.memory import apply_memory_pressure_relief
            apply_memory_pressure_relief()

            return summary

    def get_quota_overview(self, account_email: Optional[str] = None) -> dict[str, Any]:
        """
        Build an operational, human-friendly quota overview from SQLite SSOT and current state.
        Calculates dynamic countdown, formatted percentages, and Gemini-first ordering.
        """
        if not self.db:
            return {"error": "Database not initialized"}

        snapshots = self.db.get_quota_snapshots(account_email=account_email)
        stats = self.db.get_quota_summary_stats()
        recent_warmups = self.db.get_warmup_logs(account_email=account_email, limit=15)

        now = time.time()
        accounts_map: dict[str, dict[str, Any]] = {}

        for s in snapshots:
            email = s["account_email"]
            if email not in accounts_map:
                accounts_map[email] = {
                    "email": email,
                    "is_active_account": bool(s["is_active_account"]),
                    "subscription_tier": s["subscription_tier"],
                    "project_id": s["project_id"],
                    "updated_at": s["updated_at"],
                    "buckets": [],
                }

            rem_pct = round(s["remaining_fraction"] * 100, 1)
            reset_ts = s["reset_timestamp"]
            reset_time_str = s["reset_time"]

            # Calculate remaining seconds until reset
            rem_seconds = 0
            human_reset = "Idle / Full"
            if reset_ts and reset_ts > now:
                rem_seconds = int(reset_ts - now)
                hours = rem_seconds // 3600
                minutes = (rem_seconds % 3600) // 60
                seconds = rem_seconds % 60
                human_reset = f"{hours}h {minutes:02d}m"
            elif rem_pct >= 99.9:
                human_reset = "Ready to Warmup (100% full)"
            else:
                human_reset = "Reset / Idle"

            # Parse raw summary json for description & display name
            raw_meta = {}
            if s.get("raw_summary_json"):
                try:
                    raw_meta = json.loads(s["raw_summary_json"])
                except Exception:
                    raw_meta = {}

            server_desc = raw_meta.get("server_description") or raw_meta.get("description")
            d_name = raw_meta.get("display_name") or s["bucket_id"]
            is_gemini = "gemini" in s["bucket_id"].lower() or "gemini" in s["model_group"].lower()

            # Get latest warmup for this bucket
            latest_w = self.db.get_latest_warmup(email, s["bucket_id"])

            accounts_map[email]["buckets"].append({
                "bucket_id": s["bucket_id"],
                "model_group": s["model_group"],
                "window_type": s["window_type"],
                "remaining_fraction": s["remaining_fraction"],
                "remaining_percent": rem_pct,
                "reset_time": reset_time_str,
                "reset_timestamp": reset_ts,
                "remaining_seconds": rem_seconds,
                "human_countdown": human_reset,
                "server_description": server_desc,
                "display_name": d_name,
                "is_gemini_primary": is_gemini,
                "latest_warmup": latest_w,
            })

        # Ensure Gemini models are sorted FIRST for every account
        for acc in accounts_map.values():
            acc["buckets"].sort(
                key=lambda b: (
                    not b["is_gemini_primary"],
                    b["window_type"] != "5h",
                    b["bucket_id"],
                )
            )

        acc_list = list(accounts_map.values())
        acc_list.sort(key=lambda a: (not a.get("is_active_account"), a.get("email", "")))
        active_acc = next((a for a in acc_list if a.get("is_active_account")), None)
        if not active_acc and acc_list:
            active_acc = acc_list[0]

        return {
            "stats": stats,
            "accounts_count": len(accounts_map),
            "active_account": active_acc,
            "all_accounts": acc_list,
            "accounts": acc_list,
            "recent_warmups": recent_warmups,
            "last_sweep_at": self._last_sweep_time,
        }

    def is_active_account_healthy(self, threshold: float = 0.10, live: bool = False) -> tuple[bool, Optional[str], dict[str, Any]]:
        """
        Check if the active account in ~/.antigravity_tools has healthy quota (> threshold, default 10%).
        Returns (is_healthy, active_email, details_dict).
        """
        profiles = self.scan_accounts(live=live)
        active_profile = next((p for p in profiles if p.is_active), None)
        if not active_profile and profiles:
            active_profile = next((p for p in profiles if not (p.disabled or p.proxy_disabled)), profiles[0])
        if not active_profile:
            return False, None, {"reason": "no_active_profile"}
        if active_profile.disabled or active_profile.proxy_disabled:
            return False, active_profile.email, {"reason": "active_profile_disabled"}

        if not active_profile.buckets:
            return True, active_profile.email, {"reason": "active_profile_no_bucket_restrictions"}

        # 5h rolling window is the primary short-term gate for Antigravity executions
        five_h_buckets = [b for b in active_profile.buckets if b.window_type == "5h"]
        buckets_to_check = five_h_buckets if five_h_buckets else active_profile.buckets

        healthy_buckets = [b for b in buckets_to_check if b.remaining_fraction > threshold]
        is_healthy = len(healthy_buckets) > 0

        return is_healthy, active_profile.email, {
            "healthy_buckets": [b.bucket_id for b in healthy_buckets],
            "all_buckets": {b.bucket_id: b.remaining_fraction for b in active_profile.buckets},
        }

    def get_quota_health(self) -> dict[str, Any]:
        """
        Evaluate operational health of Quota Sentinel for Uptime Kuma keyword monitoring.
        Emits clean, compact JSON with status 'ok' and sentinel 'healthy'.
        """
        if not self.db:
            return {"status": "degraded", "sentinel": "degraded", "error": "Database not attached"}

        try:
            overview = self.get_quota_overview()
            stats = overview.get("stats", {})
            accounts = overview.get("accounts", [])
            if not accounts:
                try:
                    profiles = self.scan_accounts(live=False)
                    if profiles:
                        self.sync_quotas_to_db(profiles)
                        overview = self.get_quota_overview()
                        stats = overview.get("stats", {})
                        accounts = overview.get("accounts", [])
                except Exception:
                    pass

            active_acc = next((a for a in accounts if a.get("is_active_account")), None)
            if not active_acc and accounts:
                active_acc = accounts[0]

            if not active_acc:
                return {
                    "status": "ok",
                    "sentinel": "healthy",
                    "timestamp": time.time(),
                    "active_account": None,
                    "accounts_count": 0,
                    "total_5h_buckets": 0,
                    "ready_5h_buckets": 0,
                    "successful_warmups": 0,
                }

            # Find active Gemini 5h bucket
            gemini_bucket = next(
                (b for b in active_acc.get("buckets", []) if b.get("is_gemini_primary") and b.get("window_type") == "5h"),
                None
            )

            rem_pct = gemini_bucket["remaining_percent"] if gemini_bucket else 100.0
            countdown = gemini_bucket["human_countdown"] if gemini_bucket else "N/A"

            return {
                "status": "ok",
                "sentinel": "healthy",
                "timestamp": time.time(),
                "active_account": active_acc["email"],
                "active_tier": active_acc.get("subscription_tier") or "Unknown",
                "gemini_5h_remaining": rem_pct,
                "gemini_countdown": countdown,
                "accounts_count": len(accounts),
                "total_5h_buckets": stats.get("total_5h_buckets", 0),
                "ready_5h_buckets": stats.get("ready_5h_buckets", 0),
                "successful_warmups": stats.get("successful_warmups", 0),
            }
        except Exception as e:
            logger.exception("Quota Sentinel health check evaluation error: %s", e)
            return {"status": "degraded", "sentinel": "degraded", "error": str(e)}
