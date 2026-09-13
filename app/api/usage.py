"""
Claude usage-limits API.

Exposes the latest subscription usage snapshot (all rate-limit windows with
their utilization % and reset times) so the frontend can render the usage pill
+ popover on page load, before any live ``usage_snapshot`` WS event arrives.
"""

import asyncio
import logging
import time
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from app.api.auth import require_auth
from app.core.usage_limits import usage_limits_monitor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/usage", tags=["Usage"])


def _credits_are_displayable(credits: object) -> bool:
    """True when a provider credits blob has a figure the popover can render."""
    if not isinstance(credits, dict):
        return False
    if credits.get("unlimited") is True:
        return True
    for key in (
        "balance_remaining",
        "limit_remaining",
        "usage",
        "balance",
        "total_available",
    ):
        value = credits.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
    return False


def _coerce_epoch_resets_at(v: object) -> object:
    """Accept int/float epoch-seconds ``resets_at`` and emit them as str.

    Newer usage payloads (Claude CLI cross-install rows, and provider snapshots)
    carry ``resets_at`` as an int epoch instead of an ISO string, and those
    values are persisted verbatim into rows this install cannot retro-fix.
    Coerce to the decimal-string form so the API keeps its declared
    ``string | null`` contract — the frontend's ``parseResetMs`` already parses
    numeric strings as epoch sec/ms.
    """
    if isinstance(v, bool):  # bool is an int subclass; don't stringify it
        return v
    if isinstance(v, (int, float)):
        return str(int(v))
    return v


class UsageWindow(BaseModel):
    """A single rate-limit window."""
    type: str
    utilization: float
    resets_at: Optional[str] = None

    @field_validator("resets_at", mode="before")
    @classmethod
    def _coerce_resets_at(cls, v: object) -> object:
        return _coerce_epoch_resets_at(v)


class InstallUsageSnapshot(BaseModel):
    """One install's persisted per-account usage snapshot.

    The 5-hour Claude window is per-account, so on a shared database each install
    (which may run a different Claude account) publishes its own snapshot here,
    joined to the installs registry for ``device_name``/``online``.
    """
    instance_id: str
    device_name: Optional[str] = None
    online: bool = False
    account_id: Optional[str] = None
    account_email: Optional[str] = None
    account_name: Optional[str] = None
    # Subscription tier of THAT install's account ("max"/"pro"), joined from
    # claude_accounts. Names the plan in the usage popover's Claude header.
    plan_type: Optional[str] = None
    windows: List[UsageWindow] = []
    fetched_at: Optional[int] = None
    available: bool = False
    generation: Optional[int] = None


class ProviderUsageWindow(BaseModel):
    """A single non-Claude provider usage window."""
    type: str
    utilization: float
    resets_at: Optional[str] = None

    @field_validator("resets_at", mode="before")
    @classmethod
    def _coerce_resets_at(cls, v: object) -> object:
        return _coerce_epoch_resets_at(v)


class ProviderUsageEntry(BaseModel):
    """One non-Claude provider's persisted usage snapshot.

    The debug-only ``raw`` blob stored alongside these fields is NEVER exposed
    here — only the identity fields, ``windows``, ``credits``, ``plan_type`` and
    ``fetched_at`` reach the client.
    """
    provider_id: str
    provider_name: Optional[str] = None
    provider_type: Optional[str] = None
    plan_type: Optional[str] = None
    windows: List[ProviderUsageWindow] = []
    credits: Optional[dict] = None
    fetched_at: Optional[int] = None
    status: Literal["fresh", "stale", "unavailable", "never_fetched"] = "never_fetched"
    last_error_kind: Optional[str] = None


class UsageSnapshot(BaseModel):
    """Latest usage snapshot across all windows.

    The top-level fields describe the LOCAL install and are kept unchanged for
    old clients. ``snapshots`` carries every install's per-account snapshot and
    ``self_instance_id`` identifies which one is this install. ``providers``
    carries every non-Claude provider's usage snapshot (additive; empty for old
    or provider-less installs).
    """
    windows: List[UsageWindow] = []
    fetched_at: Optional[float] = None
    available: bool = False
    # Monotonic tag; lets clients drop out-of-order snapshots after a switch.
    generation: Optional[int] = None
    # Per-install cluster view (per-account usage). Absent from very old clients.
    snapshots: List[InstallUsageSnapshot] = []
    self_instance_id: Optional[str] = None
    # Non-Claude provider usage (Codex/ChatGPT, OpenRouter, OpenAI credits,
    # fal.ai credits, SuperGrok, ...). Additive.
    providers: List[ProviderUsageEntry] = []
    provider_refreshing: bool = False


async def _load_provider_usage() -> List[ProviderUsageEntry]:
    """Read persisted provider usage snapshots and join their display names.

    Best-effort: any DB hiccup yields ``[]`` so the endpoint never errors or
    drops the Claude fields. The snapshot row's ``raw`` (headers-only debug
    blob) is intentionally never read into the response.
    """
    try:
        from app.db import database as db
        from app.db.async_database import run_sync

        rows = await run_sync(db.get_all_provider_usage_snapshots)
        providers = await run_sync(db.get_all_model_providers)
        names = {p["id"]: p.get("name") for p in providers}

        from app.core.provider_usage import (
            FAL_USAGE_PROVIDER_ID,
            FAL_USAGE_PROVIDER_NAME,
            OPENAI_API_USAGE_PROVIDER_ID,
        )

        entries: List[ProviderUsageEntry] = []
        for row in rows:
            # Usage history can outlive a provider (including an in-flight poll
            # finishing after deletion). Only configured accounts belong here;
            # fal.ai is configured separately and has no model-provider row.
            if row["provider_id"] not in names and row["provider_id"] != FAL_USAGE_PROVIDER_ID:
                continue
            # Retired OAuth fallback snapshots are not Platform billing accounts.
            if row["provider_id"] == OPENAI_API_USAGE_PROVIDER_ID:
                continue
            # xAI API-key snapshots stay excluded: their only content is
            # per-minute x-ratelimit headroom (~always 0%), and xAI exposes
            # no credits endpoint a secret key may call. OpenAI rows are
            # included once the credit-grants poller has a balance or an
            # explicit failure status; their per-minute rate-limit windows are
            # stripped so the popover headlines remaining credits instead
            # of a ~0% requests bar. SuperGrok subscription rows
            # (provider_type='grok') are included. Capture itself stays —
            # it doubles as the 429 breaker seam.
            ptype = row.get("provider_type")
            credits = row.get("credits")
            windows = row.get("windows") or []
            if ptype == "xai":
                continue
            raw = row.get("raw") or {}
            error = raw.get("usage_error") if isinstance(raw, dict) else None
            if error not in {"billing_access_denied", "missing_credentials", "timeout", "unavailable"}:
                error = None
            fetched_at = row.get("fetched_at")
            if ptype == "openai":
                if not _credits_are_displayable(credits) and error is None:
                    continue
                fetched_at = raw.get("billing_fetched_at", fetched_at if _credits_are_displayable(credits) else None)
                windows = []
            # ChatGPT/Codex header credits (x-codex-credits-balance, often 0)
            # are extra-credit crumbs on the subscription quota row — not
            # prepaid API balance. That figure belongs to configured OpenAI rows.
            if ptype == "chatgpt":
                credits = None
            name = names.get(row["provider_id"])
            if not name and row["provider_id"] == FAL_USAGE_PROVIDER_ID:
                name = FAL_USAGE_PROVIDER_NAME
            entries.append(
                ProviderUsageEntry(
                    provider_id=row["provider_id"],
                    provider_name=name,
                    provider_type=ptype,
                    plan_type=row.get("plan_type"),
                    windows=windows,
                    credits=credits,
                    fetched_at=fetched_at,
                    status=(
                        "stale" if fetched_at and (
                            error or time.time() * 1000 - fetched_at > 300_000
                        ) else "fresh" if fetched_at else
                        "unavailable" if error else "never_fetched"
                    ),
                    last_error_kind=error,
                )
            )
        return entries
    except Exception:
        logger.debug("provider usage load failed; returning []", exc_info=True)
        return []


_claude_refresh_task: Optional[asyncio.Task] = None


def _request_claude_refresh() -> None:
    global _claude_refresh_task
    if _claude_refresh_task is not None and not _claude_refresh_task.done():
        return

    async def refresh() -> None:
        try:
            await usage_limits_monitor.refresh_and_broadcast()
        except Exception:
            logger.debug("Claude usage refresh failed")

    _claude_refresh_task = asyncio.create_task(refresh())


@router.get("/limits", response_model=UsageSnapshot)
async def get_usage_limits(
    refresh: bool = False, _: str = Depends(require_auth)
) -> UsageSnapshot:
    """Return the latest Claude usage snapshot for every install in the cluster.

    Serves cached snapshots while a coalesced provider poll refreshes stale data.
    Pass ``refresh=true`` to start a forced provider and Claude refresh without
    waiting for upstream billing. Read again while ``provider_refreshing`` is
    true to receive progressive provider results. Returns ``available: false`` with empty
    windows when there is no usable Claude OAuth credential (never errors). The
    legacy top-level fields describe the LOCAL install; ``snapshots`` adds every
    install's per-account data so offloaded sessions render the correct (owning
    account's) reset time. ``providers`` adds every non-Claude provider's usage
    snapshot (best-effort).
    """
    from app.core.provider_usage import provider_usage_monitor

    provider_usage_monitor.request_refresh(force=refresh)
    if refresh:
        _request_claude_refresh()
    snapshot = await usage_limits_monitor.get_cluster_snapshot()
    refreshing = provider_usage_monitor.refreshing
    providers = await _load_provider_usage()
    return UsageSnapshot(
        **snapshot, providers=providers, provider_refreshing=refreshing
    )
