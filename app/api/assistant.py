"""Owner-only routes kept from the retired assistant API; normal auth and same-origin CSRF apply.

The personal assistant's own conversation and settings page were removed in
#371 step 5. What stays under the historical ``/api/v1/assistant`` prefix:

* ``/limits``: the owner's spending and job time limits (Settings > Usage);
* ``/agents``: agent names and avatars (the Agents page and agent card);
* ``/sources``: which connected sources background research may read, the
  research loop's only switch until watches (#371 step 3) replace it.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.auth import require_admin
from app.core import assistant_limits as limits, assistant_service as service
from app.core.assistant_manager import assistant_manager
from app.db import assistant_limits as limits_db
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/assistant", tags=["Assistant"], dependencies=[Depends(require_admin)])


def _avatar(value):
    if value is not None and value not in service.AVATAR_PRESETS:
        raise ValueError("Unknown avatar preset")
    return value


class LimitsUpdate(BaseModel):
    """The owner's spending and job time limits; omitted fields keep their value.

    Switching the spending limit off needs ``confirm_spend_limit_off: true``.
    """
    model_config = ConfigDict(extra="forbid")
    spend_limit_enabled: Optional[bool] = None
    daily_spend_limit_usd: Optional[float] = Field(None, ge=limits_db.SPEND_BOUNDS[0], le=limits_db.SPEND_BOUNDS[1],
                                                   allow_inf_nan=False)
    job_limit_enabled: Optional[bool] = None
    max_job_minutes: Optional[int] = Field(None, ge=limits_db.MINUTE_BOUNDS[0], le=limits_db.MINUTE_BOUNDS[1])
    confirm_spend_limit_off: Optional[bool] = None


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    avatar_id: Optional[str] = None

    @field_validator("name", mode="before")
    @classmethod
    def stripped_name(cls, value):
        # Strip before min_length runs, so a whitespace-only name is a 422.
        return value.strip() if isinstance(value, str) else value

    @field_validator("avatar_id")
    @classmethod
    def avatar(cls, value):
        return _avatar(value)


async def _value_error_call(fn, *args):
    try:
        return await run_sync(fn, *args)
    except ValueError as exc:
        status = 404 if "does not exist" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc


@router.get("/limits")
async def read_limits():
    """The owner's limits and today's spend for coordination work."""
    return await run_sync(limits.status)


@router.put("/limits")
@router.patch("/limits")
async def update_limits(body: LimitsUpdate):
    changes = body.model_dump(exclude_unset=True)
    confirmed = changes.pop("confirm_spend_limit_off", None) is True
    for key, value in changes.items():
        if value is None:
            raise HTTPException(422, f"{key} cannot be null")
    if changes.get("spend_limit_enabled") is False and not confirmed:
        raise HTTPException(409, "Confirm switching the spending limit off (confirm_spend_limit_off: true)")
    await _value_error_call(limits_db.update, service.assistant_id(), changes)
    # Enforce a lowered limit now rather than on the next loop pass.
    assistant_manager.notify()
    return await run_sync(limits.status)


@router.get("/sources")
async def read_sources():
    return await run_sync(service.source_catalog)


@router.patch("/sources/{connector_id}")
@router.put("/sources/{connector_id}")
async def update_source(connector_id: str, body: SourceUpdate):
    source = await _value_error_call(service.set_source, connector_id, body.enabled)
    await assistant_manager.cancel_invalid_runs()
    assistant_manager.notify()
    return source


@router.get("/agents")
async def read_agent_identities():
    return await run_sync(service.agent_identities)


@router.patch("/agents/{agent_id}/identity")
async def update_agent_identity(agent_id: str, body: AgentIdentity):
    return await _value_error_call(service.update_agent_identity, agent_id, body.name, body.avatar_id)
