"""Owner-controlled, account-synced application updates."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool
from app.api.auth import require_admin, require_auth
from app.core import application_updates, preview_access
from app.db import database
from app.db.async_database import run_sync

updates_router = APIRouter()


class UpdatePolicy(BaseModel):
    allow_updates: StrictBool | None = None
    update_all_workers: StrictBool | None = None
    model_config = {"extra": "forbid"}


class UpdateAction(BaseModel):
    action: Literal["check", "download", "install"]


def require_update_owner(request: Request, token=Depends(require_admin)):
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot change application updates")
    return token


@updates_router.get("/application-updates")
async def get_application_updates(_=Depends(require_auth)):
    return await run_sync(application_updates.status)


@updates_router.put("/application-updates")
async def set_application_updates(body: UpdatePolicy, _=Depends(require_update_owner)):
    if body.allow_updates is None and body.update_all_workers is None:
        raise HTTPException(422, "Supply an update setting")
    def save():
        if body.allow_updates is not None:
            database.set_system_setting(application_updates.KEY, "true" if body.allow_updates else "false")
        if body.update_all_workers is not None:
            database.set_system_setting(application_updates.ALL_KEY, "true" if body.update_all_workers else "false")
        return application_updates.status()
    return await run_sync(save)


@updates_router.post("/application-updates")
async def application_update_action(body: UpdateAction, _=Depends(require_update_owner)):
    from app.core.update_workers import dispatch
    try:
        return await dispatch(body.action)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@updates_router.get("/application-updates/workers")
async def application_update_workers(_=Depends(require_admin)):
    from app.core.update_workers import snapshot
    return await snapshot()
