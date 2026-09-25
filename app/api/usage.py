"""
Usage-limits API.

Exposes the latest Claude usage windows (those the Claude Code CLI reported in
its own rate-limit events, with utilization % and reset times) and every
provider's usage snapshot, so the frontend can render the usage pill + popover
on page load, before any live ``usage_snapshot`` WS event arrives.
"""

import logging
import time
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from app.api.auth import require_auth
from app.core import usage_sources
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
        "spend_month",
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
    """One install's persisted Claude usage snapshot.

    Each install's CLI may be signed in differently, so on a shared database
    each install publishes the windows its own CLI reported, joined to the
    installs registry for ``device_name``/``online``. No account identity.
    """
    instance_id: str
    device_name: Optional[str] = None
    online: bool = False
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


ErrorKind = Literal["billing_access_denied", "missing_credentials", "timeout", "unavailable", "unsupported"]


class UsageSourceInfo(BaseModel):
    """Registry description of where a usage reading comes from."""
    id: str
    label: str
    scope: str
    scope_label: str
    measure: str
    measure_label: str
    collection: str
    support: str
    support_label: str
    dashboard_url: str
    min_interval_seconds: int | None = None
    unavailable: list[str] = []


def _source_info(source: usage_sources.UsageSource | None) -> UsageSourceInfo | None:
    if source is None:
        return None
    public = source.public()
    return UsageSourceInfo(**{k: public[k] for k in UsageSourceInfo.model_fields})


class ProviderUsageEntry(BaseModel):
    """One non-Claude provider's persisted usage snapshot.

    The debug-only ``raw`` blob stored alongside these fields is NEVER exposed
    here — only the identity fields, ``windows``, ``credits``, ``plan_type``,
    timestamps and the registry description of the source reach the client.
    """
    provider_id: str
    provider_name: Optional[str] = None
    provider_type: Optional[str] = None
    plan_type: Optional[str] = None
    windows: List[ProviderUsageWindow] = []
    credits: Optional[dict] = None
    fetched_at: Optional[int] = None
    status: Literal["fresh", "stale", "unavailable", "never_fetched"] = "never_fetched"
    last_error_kind: ErrorKind | None = None
    # Short labels kept for older clients; ``source_info`` has the full entry.
    source: str | None = None
    scope: str | None = None
    source_info: UsageSourceInfo | None = None
    # Epoch ms. ``fetched_at`` is the last successful observation; an attempt
    # that failed moves only ``last_attempt_at``.
    last_success_at: int | None = None
    last_attempt_at: int | None = None


class UsageSnapshot(BaseModel):
    """Latest usage snapshot across all windows.

    The top-level fields describe the LOCAL install and are kept unchanged for
    old clients. ``snapshots`` carries every install's snapshot and
    ``self_instance_id`` identifies which one is this install. ``providers``
    carries every non-Claude provider's usage snapshot (additive; empty for old
    or provider-less installs).
    """
    windows: List[UsageWindow] = []
    fetched_at: Optional[float] = None
    available: bool = False
    # Monotonic tag; lets clients drop out-of-order snapshots after a switch.
    generation: Optional[int] = None
    # Per-install cluster view. Absent from very old clients.
    snapshots: List[InstallUsageSnapshot] = []
    self_instance_id: Optional[str] = None
    # Non-Claude provider usage (Codex/ChatGPT, OpenRouter, OpenAI credits,
    # fal.ai credits, SuperGrok, ...). Additive.
    providers: List[ProviderUsageEntry] = []
    provider_refreshing: bool = False
    # Kept for older clients: nothing is polled for Claude, so both stay null.
    # ``claude_source`` is the registry entry for the CLI's rate-limit events
    # and carries the link to the user's usage page.
    attempted_at: float | None = None
    last_error_kind: ErrorKind | None = None
    claude_source: UsageSourceInfo | None = None


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
        from app.core.openrouter_usage import SNAPSHOT_PREFIX, supported

        for provider in providers:
            if supported(provider) and provider.get("enabled", True):
                names[SNAPSHOT_PREFIX + provider["id"]] = (provider.get("name") or "OpenRouter") + " account"

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
            # The registry decides visibility. Rate-limit-only sources (xAI and
            # other OpenAI-compatible hosts, Anthropic headers) stay hidden:
            # their only content is per-minute headroom (~always 0%). OpenAI
            # rows are included once Admin usage has a figure or an explicit
            # failure status; their per-minute windows are stripped so the
            # popover headlines reported spend instead of a ~0% requests bar.
            # Capture itself stays; it doubles as the 429 breaker seam.
            ptype = row.get("provider_type")
            credits = row.get("credits")
            windows = row.get("windows") or []
            raw = row.get("raw") or {}
            error = raw.get("usage_error") if isinstance(raw, dict) else None
            if error not in {"billing_access_denied", "missing_credentials", "timeout", "unavailable", "unsupported"}:
                error = None
            source_def = usage_sources.source_for_snapshot(ptype)
            if source_def is not None and not source_def.shown:
                continue
            fetched_at = row.get("fetched_at")
            if ptype == "openai":
                if raw.get("source") != "openai_admin":
                    credits = None
                    error = error or "missing_credentials"
                if not _credits_are_displayable(credits) and error is None:
                    continue
                fetched_at = raw.get("billing_fetched_at", fetched_at if _credits_are_displayable(credits) else None)
                windows = []
            # ChatGPT/Codex header credits (x-codex-credits-balance, often 0)
            # are extra-credit crumbs on the subscription quota row — not
            # prepaid API balance. That figure belongs to configured OpenAI rows.
            if ptype == "chatgpt":
                credits = None
            if ptype == "openrouter" and isinstance(credits, dict):
                # Old snapshots mixed /key limits with account-wide /credits.
                credits = {k: v for k, v in credits.items()
                           if k not in {"total_credits", "total_usage", "balance_remaining"}}
            attempted = raw.get("usage_attempted_at") if error else None
            attempted = attempted if isinstance(attempted, int) and not isinstance(attempted, bool) else None
            last_attempt = max((t for t in (fetched_at, attempted) if t), default=None)
            interval_ms = (source_def.min_interval if source_def and source_def.min_interval else 300) * 1000
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
                            error or time.time() * 1000 - fetched_at > interval_ms
                        ) else "fresh" if fetched_at else
                        "unavailable" if error else "never_fetched"
                    ),
                    last_error_kind=error,
                    source=source_def.label if source_def else None,
                    scope=source_def.scope_label if source_def else None,
                    source_info=_source_info(source_def),
                    last_success_at=fetched_at,
                    last_attempt_at=last_attempt,
                )
            )
        return entries
    except Exception:
        logger.debug("provider usage load failed; returning []", exc_info=True)
        return []


def _request_claude_refresh() -> None:
    # Re-persists and re-broadcasts the CLI-reported windows; no request.
    usage_limits_monitor.request_refresh()


def _claude_fields(snapshot: dict) -> dict:
    return {**snapshot, "claude_source": _source_info(usage_sources.SOURCES["claude_rate_limit_events"])}


@router.get("/limits", response_model=UsageSnapshot)
async def get_usage_limits(
    refresh: bool = False, _: str = Depends(require_auth)
) -> UsageSnapshot:
    """Return the latest Claude usage snapshot for every install in the cluster.

    No read waits on an upstream usage endpoint. Claude windows are read
    passively: they come only from the rate-limit events the Claude Code CLI
    reports while it runs, and a plain GET never refreshes them;
    ``refresh=true`` re-persists and re-broadcasts them. ``available: false``
    with empty windows means the CLI has reported none yet (never errors).
    Provider rows are served from cache and refreshed in the background when
    due; ``refresh=true`` also forces a provider refresh, which cannot bypass
    the request budget. Read again while ``provider_refreshing`` is true to
    receive progressive results. The legacy top-level fields describe the
    LOCAL install; ``snapshots`` adds every install's windows so offloaded
    sessions render the owning install's reset time. ``providers`` adds every
    non-Claude provider's usage snapshot (best-effort).
    """
    from app.core.provider_usage import provider_usage_monitor

    provider_usage_monitor.request_refresh(force=refresh)
    if refresh:
        _request_claude_refresh()
    snapshot = await usage_limits_monitor.get_cluster_snapshot()
    refreshing = provider_usage_monitor.refreshing or usage_limits_monitor.refreshing is True
    providers = await _load_provider_usage()
    return UsageSnapshot(
        **_claude_fields(snapshot), providers=providers, provider_refreshing=refreshing
    )


async def get_cached_usage_snapshot() -> UsageSnapshot:
    """Return the latest persisted usage observation without refreshing it.

    This is intentionally separate from :func:`get_usage_limits`: a model
    tool may inspect cached state, but must not initiate provider billing or
    subscription requests, bypass a refresh cooldown, or use credentials.
    """
    from app.core.provider_usage import provider_usage_monitor

    snapshot = await usage_limits_monitor.get_cached_cluster_snapshot()
    providers = await _load_provider_usage()
    return UsageSnapshot(
        **_claude_fields(snapshot),
        providers=providers,
        provider_refreshing=provider_usage_monitor.refreshing,
    )


async def get_cached_usage_payload() -> dict:
    """JSON-safe cache-only usage payload for native/tool host callbacks."""
    return (await get_cached_usage_snapshot()).model_dump(mode="json")
