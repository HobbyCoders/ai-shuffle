"""Owner-only running-agents watch rows and controls; normal CSRF applies."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from typing import Literal

from app.api.auth import require_admin
from app.core.watches import panel_rows
from app.core.watch_manager import watch_manager
from app.db import watches as store
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/watches",tags=["Watches"],dependencies=[Depends(require_admin)])


class WatchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["paused", "stopped", "active"]


@router.get("")
async def list_watches():
    return {"watches": await run_sync(panel_rows)}


@router.patch("/{watch_id}")
async def update_watch(watch_id: str, body: WatchStatus):
    try:
        row = await run_sync(store.set_status,watch_id,"admin",body.status)
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc
    watch_manager.notify()
    return row
