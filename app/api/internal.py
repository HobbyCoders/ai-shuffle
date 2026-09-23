"""
Internal API endpoints

Endpoints used internally by the application.
These are not intended for external consumption.
"""

import logging

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal", tags=["Internal"])

# The desktop passes a fresh secret only to the worker process it launches.
# Attached workers and remote ingress must never accept lifecycle commands.
def require_desktop(request):
    import os
    import secrets
    from fastapi import HTTPException

    token = os.environ.get("SHUFFLE_DESKTOP_LIFECYCLE_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    if (
        not token
        or request.scope.get("shuffle.ingress") != "local"
        or not secrets.compare_digest(authorization, f"Bearer {token}")
    ):
        raise HTTPException(status_code=403, detail="Desktop lifecycle access denied")


@router.get("/desktop/update-policy")
async def desktop_update_policy(request: Request):
    require_desktop(request)
    from app.core.application_updates import mirror_policy
    from app.db.async_database import run_sync
    return {"allow_updates": await run_sync(mirror_policy)}


@router.get("/desktop/source-lease")
async def desktop_source_lease(request: Request):
    """The license lease for the hub's gated source feed (D8), as the
    ``X-Shuffle-Lease`` header value, or null in beta mode or without a lease.
    Only the desktop that launched this worker may read it; the desktop sends
    it to the hub's manifest and archive requests and nowhere else."""
    require_desktop(request)
    from app.core.license import lease_header
    from app.db.async_database import run_sync
    return {"lease": await run_sync(lease_header)}


from typing import Literal
from pydantic import BaseModel, Field


class DesktopUpdateState(BaseModel):
    phase: Literal["idle", "checking", "available", "downloading", "ready", "error", "unsupported"]
    version: str | None = Field(default=None, max_length=64)
    running_version: str | None = Field(default=None, max_length=64)
    percent: float | None = Field(default=None, ge=0, le=100)
    error: str | None = Field(default=None, max_length=4096)
    message: str | None = Field(default=None, max_length=4096)
    # No policy fields: the native heartbeat cannot overwrite account preferences.


@router.post("/desktop/update-bridge")
async def desktop_update_bridge(body: DesktopUpdateState, request: Request):
    require_desktop(request)
    from app.core.application_updates import desktop_exchange
    from app.db.async_database import run_sync
    return await run_sync(desktop_exchange, body.model_dump())


_shutdown_task = None


@router.get("/desktop/active-sessions")
async def desktop_active_sessions(request: Request):
    require_desktop(request)
    from app.core.desktop_lifecycle import active_session_ids

    return {"active_sessions": len(active_session_ids())}


@router.post("/desktop/stop-sessions")
async def desktop_stop_sessions(request: Request):
    import asyncio
    from app.core.desktop_lifecycle import stop_running_sessions

    require_desktop(request)
    global _shutdown_task
    if _shutdown_task is None or _shutdown_task.cancelled() or (
        _shutdown_task.done() and _shutdown_task.exception() is not None
    ):
        _shutdown_task = asyncio.create_task(stop_running_sessions())
    await asyncio.shield(_shutdown_task)
    return {"stopped": True}
