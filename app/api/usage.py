"""
Usage-limits API.

Exposes the latest Claude usage windows (those the Claude Code CLI reported in
its own rate-limit events, with utilization % and reset times) and every
provider's usage snapshot, so the frontend can render the usage pill + popover
on page load, before any live ``usage_snapshot`` WS event arrives.
"""

import logging
import math
import time
from typing import Any, Callable, List, Literal, Optional

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
    # Epoch ms of the CLI event that last reported THIS window. A snapshot's
    # ``fetched_at`` moves with any window, so it cannot date the others.
    observed_at: int | None = None
    # Which route reported it: ``cli_stream`` (the CLI's rate_limit events)
    # or ``claude_mod`` (AI Shuffle's Claude Code mod); None on older rows.
    source: Optional[str] = None

    @field_validator("resets_at", mode="before")
    @classmethod
    def _coerce_resets_at(cls, v: object) -> object:
        return _coerce_epoch_resets_at(v)


class InstallUsageSnapshot(BaseModel):
    """One install's persisted Claude usage snapshot.

    Each install's CLI may be signed in differently, so on a shared database
    each install publishes the windows its own CLI reported, joined to the
    installs registry for ``device_name``/``online``. ``account_key`` is an
    opaque hash of the Claude account the install's CLI is signed in to (never
    the email, org id or a token); null when unknown or from an older build.
    """
    instance_id: str
    # Network device id of this install (sessions name their owner by it).
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    online: bool = False
    windows: List[UsageWindow] = []
    fetched_at: Optional[int] = None
    available: bool = False
    generation: Optional[int] = None
    account_key: Optional[str] = None


class AccountUsageWindow(UsageWindow):
    """A merged window, naming the install whose reading it is."""
    instance_id: Optional[str] = None
    device_id: Optional[str] = None
    device_name: Optional[str] = None


class AccountUsageSnapshot(BaseModel):
    """Installs signed in to one Claude account, merged window by window.

    Each window is the reading with the newest ``observed_at`` among the
    installs listed; only accounts reported by two or more installs appear.
    """
    account_key: str
    instance_ids: List[Optional[str]] = []
    device_ids: List[str] = []
    windows: List[AccountUsageWindow] = []
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


def _provider_windows(windows: object) -> list[ProviderUsageWindow]:
    """The readable windows of a stored row; an unreadable one is dropped alone.

    A window without a type or a finite utilization (NaN would also make the
    response invalid JSON) is skipped rather than failing the whole row.
    """
    valid: list[ProviderUsageWindow] = []
    for window in windows if isinstance(windows, list) else []:
        try:
            parsed = ProviderUsageWindow.model_validate(window)
        except Exception:  # noqa: BLE001 — pydantic.ValidationError and friends
            continue
        if parsed.type and math.isfinite(parsed.utilization):
            valid.append(parsed)
    return valid


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
    # Epoch ms before which a refresh (forced or not) serves this cached row
    # instead of requesting the provider again. Null when a refresh may run
    # now, and while a refused capability is paused (see last_error_kind).
    cooldown_until: int | None = None


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
    self_device_id: Optional[str] = None
    # Installs sharing a Claude account, merged (additive; see
    # ``app.core.usage_limits.merge_account_snapshots``).
    accounts: List[AccountUsageSnapshot] = []
    # Non-Claude provider usage (Codex/ChatGPT, OpenRouter, OpenAI credits,
    # fal.ai credits, SuperGrok, ...). Additive. Null when the cached rows could
    # not be read, so clients keep their last-good rows instead of blanking.
    providers: list[ProviderUsageEntry] | None = []
    provider_refreshing: bool = False
    # Kept for older clients: nothing is polled for Claude, so both stay null.
    # ``claude_source`` is the registry entry for the CLI's rate-limit events
    # and carries the link to the user's usage page.
    attempted_at: float | None = None
    last_error_kind: ErrorKind | None = None
    claude_source: UsageSourceInfo | None = None


async def _load_provider_usage() -> list[ProviderUsageEntry] | None:
    """Read persisted provider usage snapshots and join their display names.

    Best-effort: any DB hiccup yields ``None`` so the endpoint never errors or
    drops the Claude fields, and clients keep the rows they already show. The
    snapshot row's ``raw`` (headers-only debug blob) is intentionally never
    read into the response.
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
                names[SNAPSHOT_PREFIX + provider["id"]] = provider.get("name") or "OpenRouter"

        from app.core.provider_usage import (
            FAL_USAGE_PROVIDER_ID,
            FAL_USAGE_PROVIDER_NAME,
            OPENAI_API_USAGE_PROVIDER_ID,
        )

        entries: List[ProviderUsageEntry] = []
        now_ms = time.time() * 1000
        for row in rows:
            try:
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
                        # Rate-limit/header and retired billing snapshots are not
                        # balances. A local estimate is attached below, if set.
                        continue
                    if not _credits_are_displayable(credits) and error is None:
                        continue
                    fetched_at = raw.get("billing_fetched_at", fetched_at if _credits_are_displayable(credits) else None)
                    windows = []
                # Codex reports extra subscription credits, not USD or an OpenAI
                # Platform balance. Only a positive, finite balance is useful here.
                if ptype == "chatgpt":
                    balance = credits.get("balance") if isinstance(credits, dict) else None
                    credits = {"balance": balance} if (
                        isinstance(balance, (int, float)) and not isinstance(balance, bool)
                        and math.isfinite(balance) and balance > 0
                    ) else None
                if ptype == "openrouter" and isinstance(credits, dict):
                    # Old snapshots mixed /key limits with account-wide /credits.
                    credits = {k: v for k, v in credits.items()
                               if k not in {"total_credits", "total_usage", "balance_remaining"}}
                attempted = raw.get("usage_attempted_at") if error else None
                attempted = attempted if isinstance(attempted, int) and not isinstance(attempted, bool) else None
                last_attempt = max((t for t in (fetched_at, attempted) if t), default=None)
                interval_ms = (source_def.min_interval if source_def and source_def.min_interval else 300) * 1000
                cooldown_until = (last_attempt + interval_ms) if last_attempt else None
                if (cooldown_until is not None and cooldown_until <= now_ms) or error in {
                    "billing_access_denied", "unsupported",
                }:
                    cooldown_until = None
                name = names.get(row["provider_id"])
                if not name and row["provider_id"] == FAL_USAGE_PROVIDER_ID:
                    name = FAL_USAGE_PROVIDER_NAME
                entries.append(
                    ProviderUsageEntry(
                        provider_id=row["provider_id"],
                        provider_name=name,
                        provider_type=ptype,
                        plan_type=row.get("plan_type"),
                        windows=_provider_windows(windows),
                        credits=credits,
                        fetched_at=fetched_at,
                        status=(
                            "stale" if fetched_at and (
                                error or now_ms - fetched_at > interval_ms
                            ) else "fresh" if fetched_at else
                            "unavailable" if error else "never_fetched"
                        ),
                        last_error_kind=error,
                        source=source_def.label if source_def else None,
                        scope=source_def.scope_label if source_def else None,
                        source_info=_source_info(source_def),
                        last_success_at=fetched_at,
                        last_attempt_at=last_attempt,
                        cooldown_until=cooldown_until,
                    )
                )
            except Exception:  # noqa: BLE001 — one unreadable row never hides the others
                logger.debug("provider usage row %s skipped", row.get("provider_id"), exc_info=True)

        from app.db.analytics import get_manual_balance_credits

        by_id = {entry.provider_id: entry for entry in entries}
        for provider in providers:
            if (not provider.get("enabled", True)
                    or not usage_sources.is_openai_platform_row(provider)
                    or provider.get("auth_mode") in {"codex", "chatgpt"}):
                continue
            manual = await run_sync(get_manual_balance_credits, provider)
            existing = by_id.get(provider["id"])
            if existing is not None:
                if manual is not None:
                    existing.credits = {**manual, **(existing.credits or {})}
                continue
            if manual is not None:
                source_def = usage_sources.SOURCES["openai_manual"]
                entries.append(ProviderUsageEntry(
                    provider_id=provider["id"], provider_name=provider.get("name"),
                    provider_type="openai", status="fresh", credits=manual,
                    fetched_at=int(now_ms), source=source_def.label,
                    scope=source_def.scope_label, source_info=_source_info(source_def),
                ))
            elif provider.get("openai_billing_org_id"):
                source_def = usage_sources.SOURCES["openai_admin"]
                entries.append(ProviderUsageEntry(
                    provider_id=provider["id"], provider_name=provider.get("name"),
                    provider_type="openai", status="unavailable",
                    source=source_def.label, scope=source_def.scope_label,
                    source_info=_source_info(source_def), last_error_kind="unsupported",
                ))
        return entries
    except Exception:
        logger.debug("provider usage load failed; keeping client rows", exc_info=True)
        return None


def _request_claude_refresh() -> None:
    # Re-persists and re-broadcasts the CLI-reported windows; no request. At
    # most once per cooldown, so repeated popover opens do not fan an
    # unchanged snapshot out to every client.
    from app.core.provider_usage import FORCED_REFRESH_COOLDOWN_SECONDS

    usage_limits_monitor.request_refresh(cooldown=FORCED_REFRESH_COOLDOWN_SECONDS)


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
    from app.core.provider_usage import (
        FORCED_REFRESH_COOLDOWN_SECONDS,
        provider_usage_monitor,
    )

    # Every row here is the owner's account data: an API key reads none of
    # it, and never starts the owner's provider polling (see
    # API_KEY_USAGE_REASON).
    if _.startswith("api_key:"):
        return UsageSnapshot(providers=[])
    # A forced refresh inside the cooldown serves the cached rows (with
    # ``cooldown_until``); the request budget still limits each credential.
    provider_usage_monitor.request_refresh(force=refresh, cooldown=FORCED_REFRESH_COOLDOWN_SECONDS)
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


# Why an API key's session gets no usage. Every snapshot row is the owner's
# account data: the Claude subscription windows, and the billing of the model
# providers only the owner can list or configure (require_admin). Nothing is
# collected per key, so there is no subset a key owns; like the install-wide
# logs (GetAppErrors/GetAppLogs), GetUsage is refused for non-owner sessions.
API_KEY_USAGE_REASON = (
    "usage belongs to the owner's subscription and provider accounts; "
    "an API key's session cannot read it"
)


def key_denied_usage_result() -> dict:
    """GetUsage's formatted result for an API key's session."""
    return {
        "available": False,
        "claude_available": False,
        "source": "host_snapshot",
        "freshness": "unavailable",
        "windows": [],
        "accounts": [],
        "providers": [],
        "session_usage": {"available": False, "reason": API_KEY_USAGE_REASON},
        "denied": True,
        "reason": API_KEY_USAGE_REASON,
    }


def _key_denied_raw_payload() -> dict:
    """An empty host snapshot for Fluid's GetUsage formatter.

    ``providers`` is ``[]``, not ``None``: ``None`` makes the formatter replay
    the last provider rows this process read, which may be the owner's.
    """
    return {
        "available": False,
        "windows": [],
        "snapshots": [],
        "providers": [],
        "provider_refreshing": False,
        "session_usage": {"available": False, "reason": API_KEY_USAGE_REASON},
        "denied": True,
        "reason": API_KEY_USAGE_REASON,
    }


def _owner_session(session_id: Optional[str]) -> bool:
    """True only for a known session no API key owns. Fails closed."""
    if not session_id:
        return False
    try:
        from app.db import database

        row = database.get_session(session_id)
    except Exception:  # noqa: BLE001 - unknown ownership reads as "not the owner"
        logger.debug("GetUsage could not read session %s", session_id, exc_info=True)
        return False
    return bool(row) and not row.get("api_user_id")


async def get_session_usage_payload(session_id: Optional[str]) -> dict:
    """GetUsage's host snapshot for one app session.

    The owner's session gets :func:`get_cached_usage_payload`; an API key's
    session (or one whose owner cannot be read) gets an empty snapshot.
    """
    from app.db.async_database import run_sync

    if not await run_sync(_owner_session, session_id):
        return _key_denied_raw_payload()
    payload = await get_cached_usage_payload()
    return await with_assistant_spend(payload, session_id)


async def with_assistant_spend(payload: dict, session_id: Optional[str]) -> dict:
    """Put today's assistant spend in ``session_usage`` for its home and jobs.

    Read-only: the assistant answers "how much have you spent today?" from
    the real figures, and only the owner can change the limits.
    """
    from app.core import assistant_limits
    from app.db.async_database import run_sync

    try:
        block = await run_sync(assistant_limits.session_usage, session_id)
    except Exception:  # noqa: BLE001 - usage is informational
        logger.debug("GetUsage could not read assistant spend for %s", session_id, exc_info=True)
        block = None
    if block is not None:
        payload = dict(payload, session_usage=block)
    return payload


def session_usage_callback(resolve_session_id: Callable[[], Optional[str]]) -> Callable[[], Any]:
    """A GetUsage host callback bound to a session resolved at call time."""

    async def _callback() -> dict:
        try:
            session_id = resolve_session_id()
        except Exception:  # noqa: BLE001 - unresolvable reads as "not the owner"
            session_id = None
        return await get_session_usage_payload(session_id)

    return _callback
