"""Owner-controlled, account-synced application updates."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, StrictBool
from app.api.auth import require_admin, require_auth
from app.core import application_updates, preview_access
from app.db import database
from app.db.async_database import run_sync

updates_router = APIRouter()
channel_router = APIRouter(prefix="/api/v1/updates", tags=["Updates"])


class UpdateChannel(BaseModel):
    channel: Literal["stable", "nightly", "edge"]
    model_config = {"extra": "forbid"}


class UpdatePolicy(BaseModel):
    allow_updates: StrictBool | None = None
    update_all_workers: StrictBool | None = None
    model_config = {"extra": "forbid"}


class UpdateAction(BaseModel):
    action: Literal["check", "download", "install", "cancel"]
    # Install only, when sessions are running: restart once they finish, or
    # stop them first. "cancel" drops a waiting "when_idle" restart.
    mode: Literal["when_idle", "stop"] | None = None
    # "current" acts on this worker only, even when "Update all workers" is on.
    # Omitted follows that setting.
    scope: Literal["current"] | None = None


def require_update_owner(request: Request, token=Depends(require_admin)):
    if preview_access.grant(request) is not None:
        raise HTTPException(403, "Agent previews cannot change application updates")
    return token


@channel_router.get("/channel")
async def get_update_channel(_=Depends(require_update_owner)):
    try:
        return await run_sync(application_updates.channel_status)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(409, "Invalid update channel configuration") from exc


@channel_router.put("/channel")
async def put_update_channel(body: UpdateChannel, response: Response, _=Depends(require_update_owner)):
    try:
        result = await run_sync(application_updates.set_channel, body.channel)
        if application_updates.control_root() is None:
            response.status_code = 202  # launcher persists it on the next heartbeat
        return result
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


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
        return await dispatch(body.action, body.mode, body.scope)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@updates_router.get("/application-updates/workers")
async def application_update_workers(_=Depends(require_admin)):
    from app.core.update_workers import snapshot
    return await snapshot()


@updates_router.get("/application-updates/deploy-status")
async def application_deploy_status(pr: int | None = Query(default=None, ge=1),
                                    commit: str | None = Query(default=None, max_length=64),
                                    _=Depends(require_admin)):
    """Each worker's revision against origin/main, and whether it has ``pr`` or ``commit``."""
    if pr is not None and commit:
        raise HTTPException(422, "Supply pr or commit, not both")
    from app.core.deploy_status import deploy_status
    return await deploy_status(pr=pr, commit=commit or None)
