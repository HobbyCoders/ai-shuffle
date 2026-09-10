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
