"""Owner-authorized worker machine settings, not a generic settings proxy."""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import require_admin
from app.core import preview_access, worker_settings

router = APIRouter()


@router.get("/worker-settings")
async def get_worker_settings(_=Depends(require_admin)):
    return await worker_settings.snapshot()


@router.post("/worker-settings/action")
async def act_on_worker(body: worker_settings.Dispatch, request: Request, _=Depends(require_admin)):
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot change machine settings")
    try:
        return await worker_settings.dispatch(body)
    except LookupError as exc:
        raise HTTPException(404, "Unknown worker; no action was sent") from exc
