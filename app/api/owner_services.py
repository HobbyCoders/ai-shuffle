"""Owner spending limits and agent identities, with installed-client HTTP aliases.

Normal owner authentication and same-origin CSRF apply to both prefixes. Only
these shared endpoints have aliases; the hidden home and research API stay gone.
The legacy aliases may be removed in a later release after clients have upgraded.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.auth import require_admin
from app.core import owner_services as service
from app.core import spending_limits as limits
from app.core.owner_compat import LEGACY_HTTP_PREFIX
from app.core.watch_manager import watch_manager
from app.db import spending_limits as limits_db
from app.db.async_database import run_sync

_routes = APIRouter(dependencies=[Depends(require_admin)])
router = APIRouter(tags=["Owner coordination"])


def _avatar(value: str | None) -> str | None:
    if value is not None and value not in service.AVATAR_PRESETS:
        raise ValueError("Unknown avatar preset")
    return value


class LimitsUpdate(BaseModel):
    """The owner's spending and job time limits; omitted fields keep their value.

    Switching the spending limit off needs ``confirm_spend_limit_off: true``.
    """
    model_config = ConfigDict(extra="forbid")
    spend_limit_enabled: bool | None = None
    daily_spend_limit_usd: float | None = Field(
        None, ge=limits_db.SPEND_BOUNDS[0], le=limits_db.SPEND_BOUNDS[1], allow_inf_nan=False,
    )
    job_limit_enabled: bool | None = None
    max_job_minutes: int | None = Field(
        None, ge=limits_db.MINUTE_BOUNDS[0], le=limits_db.MINUTE_BOUNDS[1],
    )
    confirm_spend_limit_off: bool | None = None


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, min_length=1, max_length=80)
    avatar_id: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def stripped_name(cls: type[AgentIdentity], value: Any) -> Any:
        # Strip before min_length runs, so a whitespace-only name is a 422.
        return value.strip() if isinstance(value, str) else value

    @field_validator("avatar_id")
    @classmethod
    def avatar(cls: type[AgentIdentity], value: str | None) -> str | None:
        return _avatar(value)


async def _value_error_call(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return await run_sync(fn, *args)
    except ValueError as exc:
        status = 404 if "does not exist" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc


@_routes.get("/limits")
async def read_limits() -> Any:
    """The owner's limits and today's spend for coordination work."""
    return await run_sync(limits.status)


@_routes.put("/limits")
@_routes.patch("/limits")
async def update_limits(body: LimitsUpdate) -> Any:
    changes = body.model_dump(exclude_unset=True)
    confirmed = changes.pop("confirm_spend_limit_off", None) is True
    for key, value in changes.items():
        if value is None:
            raise HTTPException(422, f"{key} cannot be null")
    if changes.get("spend_limit_enabled") is False and not confirmed:
        raise HTTPException(
            409, "Confirm switching the spending limit off (confirm_spend_limit_off: true)",
        )
    await _value_error_call(limits_db.update, service.assistant_id(), changes)
    # Enforce a lowered limit now rather than on the next loop pass.
    watch_manager.notify()
    return await run_sync(limits.status)


@_routes.get("/agents")
async def read_agent_identities() -> Any:
    return await run_sync(service.agent_identities)


@_routes.patch("/agents/{agent_id}/identity")
async def update_agent_identity(agent_id: str, body: AgentIdentity) -> Any:
    return await _value_error_call(service.update_agent_identity, agent_id, body.name, body.avatar_id)


router.include_router(_routes, prefix="/api/v1/owner")
# Thin aliases for installed desktop/mobile clients and older workers.
router.include_router(_routes, prefix=LEGACY_HTTP_PREFIX, include_in_schema=False)
