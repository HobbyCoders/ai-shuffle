"""
Claude Status API proxy — checks status.claude.com for service issues.

Proxies the Statuspage.io API to avoid CORS issues from the frontend.
Caches results for 60 seconds to avoid hammering the upstream API.
"""

import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/claude-status", tags=["Claude Status"])

STATUSPAGE_URL = "https://status.claude.com/api/v2/summary.json"
CACHE_TTL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10

# In-memory cache
_cache: dict = {
    "data": None,
    "fetched_at": 0.0,
}


class ComponentStatus(BaseModel):
    """Status of a single Claude service component."""
    name: str
    status: str  # operational, degraded_performance, partial_outage, major_outage


class IncidentUpdate(BaseModel):
    """A single update within an incident."""
    status: str
    body: str
    created_at: str


class Incident(BaseModel):
    """An active incident affecting Claude services."""
    name: str
    status: str  # investigating, identified, monitoring, resolved
    impact: str  # none, minor, major, critical
    shortlink: str
    started_at: str
    latest_update: Optional[str] = None


class ClaudeStatusResponse(BaseModel):
    """Aggregated Claude service status."""
    ok: bool  # True if all systems operational, no active incidents
    indicator: str  # none, minor, major, critical
    description: str  # Human-readable summary
    components: list[ComponentStatus]
    incidents: list[Incident]
    fetched_at: str  # ISO timestamp of when the data was fetched


def _parse_status(raw: dict) -> ClaudeStatusResponse:
    """Parse the raw Statuspage.io summary into our response model."""
    status_block = raw.get("status", {})
    indicator = status_block.get("indicator", "none")
    description = status_block.get("description", "All Systems Operational")

    # Parse components (only showcased ones)
    components = []
    for comp in raw.get("components", []):
        if comp.get("showcase", False) and not comp.get("group", False):
            components.append(ComponentStatus(
                name=comp["name"],
                status=comp["status"],
            ))

    # Parse active incidents (unresolved only)
    incidents = []
    for inc in raw.get("incidents", []):
        if inc.get("resolved_at") is not None:
            continue

        # Get the latest update body
        latest_update = None
        updates = inc.get("incident_updates", [])
        if updates:
            # Updates are ordered newest-first from the API
            latest_update = updates[0].get("body", "")

        incidents.append(Incident(
            name=inc["name"],
            status=inc.get("status", "investigating"),
            impact=inc.get("impact", "none"),
            shortlink=inc.get("shortlink", ""),
            started_at=inc.get("started_at", ""),
            latest_update=latest_update,
        ))

    ok = indicator == "none" and len(incidents) == 0

    return ClaudeStatusResponse(
        ok=ok,
        indicator=indicator,
        description=description,
        components=components,
        incidents=incidents,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


async def _fetch_status() -> ClaudeStatusResponse:
    """Fetch status from upstream, using cache if fresh enough."""
    now = time.time()

    if _cache["data"] is not None and (now - _cache["fetched_at"]) < CACHE_TTL_SECONDS:
        return _cache["data"]

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            resp = await client.get(STATUSPAGE_URL)
            resp.raise_for_status()
            raw = resp.json()

        parsed = _parse_status(raw)
        _cache["data"] = parsed
        _cache["fetched_at"] = now
        return parsed

    except Exception as e:
        logger.warning(f"Failed to fetch Claude status: {e}")

        # Return cached data if we have any, even if stale
        if _cache["data"] is not None:
            return _cache["data"]

        # No cache at all — return an "unknown" response
        return ClaudeStatusResponse(
            ok=True,
            indicator="none",
            description="Unable to fetch status",
            components=[],
            incidents=[],
            fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )


@router.get("")
async def get_claude_status(token: str = Depends(require_auth)) -> ClaudeStatusResponse:
    """
    Get current Claude service status.

    Proxies status.claude.com with a 60-second server-side cache.
    Returns aggregated status including active incidents and component health.
    """
    return await _fetch_status()
