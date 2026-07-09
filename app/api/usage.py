"""
Claude usage-limits API.

Exposes the latest subscription usage snapshot (all rate-limit windows with
their utilization % and reset times) so the frontend can render the usage pill
+ popover on page load, before any live ``usage_snapshot`` WS event arrives.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_auth
from app.core.usage_limits import usage_limits_monitor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/usage", tags=["Usage"])


class UsageWindow(BaseModel):
    """A single rate-limit window."""
    type: str
    utilization: float
    resets_at: Optional[str] = None


class UsageSnapshot(BaseModel):
    """Latest usage snapshot across all windows."""
    windows: List[UsageWindow] = []
    fetched_at: Optional[float] = None
    available: bool = False
    # Monotonic tag; lets clients drop out-of-order snapshots after a switch.
    generation: Optional[int] = None


@router.get("/limits", response_model=UsageSnapshot)
async def get_usage_limits(_: str = Depends(require_auth)) -> UsageSnapshot:
    """Return the latest Claude usage snapshot.

    Serves the cached snapshot; fetches on demand if it is stale or has never
    been fetched. Returns ``available: false`` with empty windows when there is
    no usable Claude OAuth credential (never errors).
    """
    snapshot = await usage_limits_monitor.get_snapshot()
    return UsageSnapshot(**snapshot)
