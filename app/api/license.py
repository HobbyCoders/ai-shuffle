"""/api/v1/license: license status, activation and deactivation for this worker.

Anyone signed in can read the status (the banner needs it). Only the owner can
activate or deactivate. Nothing here touches user data.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import require_admin, require_auth
from app.core import license as licensing
from app.core import preview_access
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/license", tags=["License"])


class ActivateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    model_config = {"extra": "forbid"}


def require_license_owner(request: Request, token=Depends(require_admin)):
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot change the license")
    return token


def _error(exc: licensing.LicenseError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, **exc.extra})


@router.get("")
async def license_status(_=Depends(require_auth)):
    return await run_sync(licensing.status)


@router.post("/activate")
async def activate_license(body: ActivateRequest, _=Depends(require_license_owner)):
    try:
        return await licensing.activate(body.key)
    except licensing.LicenseError as exc:
        return _error(exc)


@router.post("/deactivate")
async def deactivate_license(_=Depends(require_license_owner)):
    try:
        return await licensing.deactivate()
    except licensing.LicenseError as exc:
        return _error(exc)


@router.post("/refresh")
async def refresh_license(_=Depends(require_license_owner)):
    return await licensing.refresh()
