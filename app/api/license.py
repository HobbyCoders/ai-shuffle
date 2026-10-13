"""/api/v1/license: license status, activation and deactivation for this worker.

Anyone signed in can read Cloud plan information for Settings. Only the owner can
activate, deactivate or free a seat. Nothing here touches user data.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.auth import require_admin, require_auth
from app.core import license as licensing
from app.core import preview_access
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/license", tags=["Cloud"])


class ActivateRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    model_config = {"extra": "forbid"}


class ReleaseRequest(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    install_id: str = Field(min_length=8, max_length=128)
    model_config = {"extra": "forbid"}


def require_license_owner(request: Request, token=Depends(require_admin)):
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot change the Cloud plan")
    return token


def _error(exc: licensing.LicenseError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, **exc.extra})


@router.get("")
async def license_status(_=Depends(require_auth)):
    return await run_sync(licensing.status)


@router.post("/activate")
async def activate_license(body: ActivateRequest, _=Depends(require_license_owner)):
    """Activate with a key. When every seat is taken the answer is 200 with
    ``activated: false`` and the workers holding seats, so the Settings card
    can offer to free one (the API client does not expose error bodies)."""
    try:
        return {"activated": True, **await licensing.activate(body.key)}
    except licensing.LicenseError as exc:
        if exc.extra.get("code") == "seats_full":
            return {"activated": False, "detail": exc.message, **exc.extra}
        return _error(exc)


@router.post("/release")
async def release_seat(body: ReleaseRequest, _=Depends(require_license_owner)):
    try:
        return await licensing.release_seat(body.key, body.install_id)
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
