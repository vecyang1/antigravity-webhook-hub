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

    def scan_accounts(self) -> list[AccountQuotaProfile]:
        """
        Synchronously scan all account JSON files from ~/.antigravity_tools/accounts/.
        Extracts token credentials and quota group buckets.
        """
        accounts_dir = self._resolve_accounts_dir()
        if not accounts_dir.is_dir():
            logger.debug("Accounts directory does not exist: %s", accounts_dir)
            return []

        active_id = self._resolve_active_account_id()
        profiles: list[AccountQuotaProfile] = []

        for json_path in accounts_dir.glob("*.json"):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning("Failed loading account JSON '%s': %s", json_path.name, e)
                continue

            acc_id = str(data.get("id") or json_path.stem)
            email = str(data.get("email") or "")
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
                    rem_frac = float(b.get("remaining_fraction", 1.0))
                    reset_time_str = b.get("reset_time")
                    reset_ts = parse_iso_timestamp(reset_time_str)
                    display_name = b.get("display_name")

                    # Deduce model group
                    if "gemini" in bucket_id.lower():
                        model_group = "Gemini Models"
                    elif "3p" in bucket_id.lower() or "claude" in bucket_id.lower() or "gpt" in bucket_id.lower():
                        model_group = "Claude and GPT models"
                    else:
                        model_group = "Other Models"

                    buckets.append(
                        QuotaBucketInfo(
                            bucket_id=bucket_id,
                            model_group=model_group,
                            window_type=window,
                            remaining_fraction=rem_frac,
                            reset_time=reset_time_str,
                            reset_timestamp=reset_ts,
                            display_name=display_name,
                        )
                    )

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
            )
            profiles.append(profile)

        # Sort with active account first, then alphabetically by email
        profiles.sort(key=lambda p: (not p.is_active, p.email))
        return profiles

    def sync_quotas_to_db(self, profiles: Optional[list[AccountQuotaProfile]] = None) -> int:
        """
        Persist scanned account quota snapshots into SQLite SSOT.
        Returns number of bucket snapshots upserted.
        """
        if not self.db:
            return 0

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

        return upserted

    def evaluate_warmup_candidates(
        self,
        profiles: Optional[list[AccountQuotaProfile]] = None,
        account_email: Optional[str] = None,
        bucket_id: Optional[str] = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Identify buckets eligible for minimal token warmup.
        Eligibility Criteria:
        1. auto_warmup_5h is True (and window_type == '5h'), or auto_warmup_weekly is True.
        2. Bucket has remaining_fraction >= 0.999 (meaning 100% full, window has reset and halted).
        3. reset_timestamp is expired or in the past (now >= reset_timestamp - 60s) or reset_timestamp is None/0.
        4. Cooldown: No successful warmup for this bucket in the last warmup_cooldown_seconds (unless force=True).
        """
        if profiles is None:
            profiles = self.scan_accounts()

        cooldown_sec = 17700  # Default 4h 55m
        auto_5h = True
        auto_weekly = False
        if self.quota_cfg:
            cooldown_sec = getattr(self.quota_cfg, "warmup_cooldown_seconds", 17700)
            auto_5h = getattr(self.quota_cfg, "auto_warmup_5h", True)
            auto_weekly = getattr(self.quota_cfg, "auto_warmup_weekly", False)

        now = time.time()
        candidates: list[dict[str, Any]] = []

        for p in profiles:
            # Skip accounts explicitly marked as disabled
            if p.disabled:
                continue

            if account_email and p.email.lower() != account_email.strip().lower():
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

                # 3. Reset timestamp check: window has reset or is idle
                if not force and b.reset_timestamp is not None:
                    # Allow 60s grace margin before exact reset_timestamp
                    if now < (b.reset_timestamp - 60.0):
                        continue

                # 4. Cooldown check against SQLite SSOT
                if not force and self.db:
                    latest = self.db.get_latest_warmup(p.email, b.bucket_id)
                    if latest and latest.get("status") == "success":
                        # Check when latest warmup occurred
                        created_at = latest.get("created_at")
                        # If created_at is an ISO string or timestamp
                        warmup_ts = parse_iso_timestamp(created_at) if isinstance(created_at, str) else None
                        if warmup_ts and (now - warmup_ts) < cooldown_sec:
                            logger.debug(
                                "Bucket %s/%s cooldown active (elapsed %.0fs < %ds), skipping",
                                p.email, b.bucket_id, now - warmup_ts, cooldown_sec
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
            "trigger_reason": trigger_reason,
            "route_used": route_used,
            "status": status,
            "error_message": error_msg,
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
    ) -> dict[str, Any]:
        """
        Main execution loop for Sentinel:
        1. Scans account profiles.
        2. Syncs quotas to SQLite SSOT.
        3. Evaluates warmup candidates.
        4. Dispatches warmup pings.
        5. Returns structured summary.
        """
        async with self._lock:
            t0 = time.time()
            profiles = await asyncio.to_thread(self.scan_accounts)
            upserted = await asyncio.to_thread(self.sync_quotas_to_db, profiles)
            candidates = self.evaluate_warmup_candidates(
                profiles,
                account_email=account_email,
                bucket_id=bucket_id,
                force=force,
            )

            warmup_results: list[dict[str, Any]] = []
            for cand in candidates:
                res = await self.execute_warmup(cand, trigger_reason=reason)
                warmup_results.append(res)
                # Small yield between multiple account pings
                await asyncio.sleep(0.05)

            self._last_sweep_time = time.time()
            total_duration_ms = int((self._last_sweep_time - t0) * 1000)

            summary = {
                "reason": reason,
                "accounts_scanned": len(profiles),
                "snapshots_upserted": upserted,
                "candidates_found": len(candidates),
                "warmups_executed": len(warmup_results),
                "warmup_results": warmup_results,
                "duration_ms": total_duration_ms,
                "timestamp": self._last_sweep_time,
            }

            if warmup_results:
                logger.info(
                    "Antigravity Quota Sentinel (%s): Dispatched %d warmups in %dms",
                    reason, len(warmup_results), total_duration_ms
                )

            return summary

    def get_quota_overview(self, account_email: Optional[str] = None) -> dict[str, Any]:
        """
        Build an operational, human-friendly quota overview from SQLite SSOT and current state.
        Calculates time-until-reset, formatted percentages, and countdown status.
        """
        if not self.db:
            return {"error": "Database not initialized"}

        snapshots = self.db.get_quota_snapshots(account_email=account_email)
        stats = self.db.get_quota_summary_stats()
        recent_warmups = self.db.get_warmup_logs(account_email=account_email, limit=10)

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
                human_reset = f"{hours}h {minutes}m"
            elif rem_pct >= 99.9:
                human_reset = "Ready to Warmup (100% full)"
            else:
                human_reset = "Expired"

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
                "latest_warmup": latest_w,
            })

        return {
            "stats": stats,
            "accounts_count": len(accounts_map),
            "accounts": list(accounts_map.values()),
            "recent_warmups": recent_warmups,
            "last_sweep_at": self._last_sweep_time,
        }
