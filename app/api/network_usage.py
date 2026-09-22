"""Authenticated peer operations for the network's usage budget authority."""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from app.core import usage_polling as polling
from app.db.async_database import run_sync
from app.network import tokens, usage_budget

router = APIRouter(prefix="/peer/usage-budget", include_in_schema=False)
BudgetKey = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]


async def require_authority(device: str = Depends(tokens.require_device)):
    try:
        ctx = await run_sync(usage_budget.coordinator)
    except polling.UsageDeferred:
        raise HTTPException(503, "Usage budget authority unavailable") from None
    if ctx is None or ctx.device_id != ctx.founder_device_id:
        raise HTTPException(409, "This worker is not the usage budget authority")
    member = ctx.member(device)
    if not member or member.status != "active" or member.kind != "worker":
        raise HTTPException(403, "Only active workers can request usage budgets")


class Completion(BaseModel):
    lease: str = Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32)
    status: int | None = Field(default=None, ge=100, le=599)
    retry_after: float = Field(default=0, ge=0, allow_inf_nan=False)
    interval: float = Field(default=300, ge=300, allow_inf_nan=False)


class Claim(BaseModel):
    interval: float = Field(default=300, ge=300, allow_inf_nan=False)


@router.post("/claim/{key}", dependencies=[Depends(require_authority)])
async def claim(key: BudgetKey, body: Claim | None = None):
    return {"lease": await run_sync(polling.claim, key, time.time(), body.interval if body else 300)}


@router.post("/finish/{key}", dependencies=[Depends(require_authority)])
async def finish(key: BudgetKey, body: Completion):
    await run_sync(polling.finish, key, time.time(), body.status,
                   body.retry_after, body.interval, lease=body.lease)
    return {"ok": True}


@router.post("/resume/{key}", dependencies=[Depends(require_authority)])
async def resume(key: BudgetKey):
    await run_sync(polling.resume_identity, key)
    return {"ok": True}
