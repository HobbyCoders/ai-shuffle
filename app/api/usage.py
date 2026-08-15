"""
Claude usage-limits API.

Exposes the latest subscription usage snapshot (all rate-limit windows with
their utilization % and reset times) so the frontend can render the usage pill
+ popover on page load, before any live ``usage_snapshot`` WS event arrives.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from app.api.auth import require_auth
from app.core.usage_limits import usage_limits_monitor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/usage", tags=["Usage"])


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
    # Non-Claude provider usage (Codex/ChatGPT, OpenRouter, ...). Additive.
    providers: List[ProviderUsageEntry] = []


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

        entries: List[ProviderUsageEntry] = []
        for row in rows:
            entries.append(
                ProviderUsageEntry(
                    provider_id=row["provider_id"],
                    provider_name=names.get(row["provider_id"]),
                    provider_type=row.get("provider_type"),
                    plan_type=row.get("plan_type"),
                    windows=row.get("windows") or [],
                    credits=row.get("credits"),
                    fetched_at=row.get("fetched_at"),
                )
            )
        return entries
    except Exception:
        logger.debug("provider usage load failed; returning []", exc_info=True)
        return []


@router.get("/limits", response_model=UsageSnapshot)
async def get_usage_limits(_: str = Depends(require_auth)) -> UsageSnapshot:
    """Return the latest Claude usage snapshot for every install in the cluster.

    Serves the cached snapshot; fetches on demand if it is stale or has never
    been fetched. Returns ``available: false`` with empty windows when there is
    no usable Claude OAuth credential (never errors). The legacy top-level fields
    describe the LOCAL install; ``snapshots`` adds every install's per-account
    data so offloaded sessions render the correct (owning account's) reset time.
    ``providers`` adds every non-Claude provider's usage snapshot (best-effort).
    """
    snapshot = await usage_limits_monitor.get_cluster_snapshot()
    providers = await _load_provider_usage()
    return UsageSnapshot(**snapshot, providers=providers)
