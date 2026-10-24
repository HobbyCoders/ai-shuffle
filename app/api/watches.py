"""Owner-only running-agents watch rows and controls; normal CSRF applies."""

from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from typing import Literal

from app.api.auth import require_admin
from app.core import watch_placement
from app.core.watch_manager import watch_manager
from app.network.control import ControlRefused, RunnerUnreachable
from app.db.async_database import run_sync

router = APIRouter(
    prefix="/api/v1/watches", tags=["Watches"], dependencies=[Depends(require_admin)]
)


class WatchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["paused", "stopped", "active"] | None = None
    worker_id: str | None = None


class WatchPreference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str | None = None


@router.get("")
async def list_watches() -> dict[str, Any]:
    from app.db import database

    return {
        "watches": await watch_placement.rows(),
        "preferred_worker_id": await run_sync(
            database.get_system_setting, watch_placement.PREFERENCE
        ),
    }


@router.put("/preference")
async def set_preference(body: WatchPreference) -> dict[str, Any]:
    from app.db import database

    if body.worker_id:
        try:
            await run_sync(watch_placement.resolve, body.worker_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await run_sync(database.set_system_setting, watch_placement.PREFERENCE, body.worker_id)
    else:
        await run_sync(database.delete_system_setting, watch_placement.PREFERENCE)
    return {"preferred_worker_id": body.worker_id}


@router.patch("/{watch_id}")
async def update_watch(watch_id: str, body: WatchStatus) -> Any:
    try:
        if (body.worker_id is None) == (body.status is None):
            raise ValueError("Supply either status or worker_id")
        row = (
            await watch_placement.move(watch_id, body.worker_id) if body.worker_id
            else await watch_placement.change(watch_id, body.status)
        )
    except (ValueError, ControlRefused) as exc:
        # A refusal, local or from the peer: nothing applied that a retry fixes
        # by itself. The watch list says whether a move left the source fenced.
        raise HTTPException(409, str(exc)) from exc
    except RunnerUnreachable as exc:
        # Unreachable, or reached with no answer (MaybeApplied): the outcome is
        # unknown, so the client must not report success. 503, not 409.
        raise HTTPException(503, "Watch worker is unavailable; retry the same operation") from exc
    watch_manager.notify()
    return row
