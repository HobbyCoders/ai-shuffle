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


class UsageWindow(BaseModel):
    """A single rate-limit window."""
    type: str
    utilization: float
    resets_at: Optional[str] = None

    @field_validator("resets_at", mode="before")
    @classmethod
    def _coerce_resets_at(cls, v: object) -> object:
        """Accept int/float epoch-seconds ``resets_at`` and emit them as str.

        Newer Claude CLI usage payloads carry ``resets_at`` as an int epoch
        instead of an ISO string, and those values are persisted verbatim into
        the shared cross-install ``usage_snapshots`` rows (which this install
        cannot retro-fix). Coerce to the decimal-string form here so the API
        keeps its declared ``string | null`` contract — the frontend's
        ``parseResetMs`` already parses numeric strings as epoch sec/ms.
        """
        if isinstance(v, bool):  # bool is an int subclass; don't stringify it
            return v
        if isinstance(v, (int, float)):
            return str(int(v))
        return v


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
    windows: List[UsageWindow] = []
    fetched_at: Optional[int] = None
    available: bool = False
    generation: Optional[int] = None


class UsageSnapshot(BaseModel):
    """Latest usage snapshot across all windows.

    The top-level fields describe the LOCAL install and are kept unchanged for
    old clients. ``snapshots`` carries every install's per-account snapshot and
    ``self_instance_id`` identifies which one is this install.
    """
    windows: List[UsageWindow] = []
    fetched_at: Optional[float] = None
    available: bool = False
    # Monotonic tag; lets clients drop out-of-order snapshots after a switch.
    generation: Optional[int] = None
    # Per-install cluster view (per-account usage). Absent from very old clients.
    snapshots: List[InstallUsageSnapshot] = []
    self_instance_id: Optional[str] = None


@router.get("/limits", response_model=UsageSnapshot)
async def get_usage_limits(_: str = Depends(require_auth)) -> UsageSnapshot:
    """Return the latest Claude usage snapshot for every install in the cluster.

    Serves the cached snapshot; fetches on demand if it is stale or has never
    been fetched. Returns ``available: false`` with empty windows when there is
    no usable Claude OAuth credential (never errors). The legacy top-level fields
    describe the LOCAL install; ``snapshots`` adds every install's per-account
    data so offloaded sessions render the correct (owning account's) reset time.
    """
    snapshot = await usage_limits_monitor.get_cluster_snapshot()
    return UsageSnapshot(**snapshot)
