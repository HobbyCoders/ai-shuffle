"""Owner-only persistent assistant API; normal auth and same-origin CSRF apply."""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.auth import require_admin
from app.core import assistant_introduction as introductions, assistant_limits as limits, assistant_service as service
from app.core import assistant_worker
from app.core.assistant_manager import assistant_manager
from app.db import assistant as store, assistant_limits as limits_db
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/assistant", tags=["Assistant"], dependencies=[Depends(require_admin)])


class Introduction(BaseModel):
    """Owner corrections to the detected introduction; empty restores detection."""
    model_config = ConfigDict(extra="forbid")
    preferred_name: Optional[str] = Field(None, max_length=80)
    email: Optional[str] = Field(None, max_length=254)
    github_username: Optional[str] = Field(None, max_length=39)
    repository_refs: Optional[list[str]] = Field(None, max_length=20)
    project_ids: Optional[list[str]] = Field(None, max_length=20)
    notes: Optional[str] = Field(None, max_length=1000)

    @field_validator("preferred_name", "notes")
    @classmethod
    def printable(cls, value):
        if value is not None and any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ValueError("Introduction must be printable text")
        return value.strip() if value is not None else value

    @field_validator("email")
    @classmethod
    def email_address(cls, value):
        if value is None:
            return value
        value = value.strip()
        if value and not introductions.clean_email(value):
            raise ValueError("Use an email address")
        return value

    @field_validator("github_username")
    @classmethod
    def github_name(cls, value):
        if value and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", value):
            raise ValueError("Use a GitHub username, not a URL or credential")
        return value

    @field_validator("repository_refs")
    @classmethod
    def repository_names(cls, value):
        for ref in value or []:
            if not re.fullmatch(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}", ref):
                raise ValueError("Repository references must be owner/repository, not URLs or credentials")
        return value

    @field_validator("project_ids")
    @classmethod
    def bounded_ids(cls, value):
        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", ref) for ref in value or []):
            raise ValueError("Invalid project reference")
        return value


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Older clients chose which agent runs the assistant. It always runs on
    # its built-in agent now: either field only means "set the assistant up".
    agent_id: Optional[str] = Field(None, min_length=1, max_length=128)
    create_agent: Optional[bool] = None
    enabled: Optional[bool] = None
    paused: Optional[bool] = None
    max_runs_per_day: Optional[int] = Field(None, ge=1, le=100)
    max_concurrent_runs: Optional[int] = Field(None, ge=1, le=3)
    max_turns: Optional[int] = Field(None, ge=1, le=20)
    timeout_seconds: Optional[int] = Field(None, ge=30, le=900)
    max_cost_per_run: Optional[float] = Field(None, gt=0, le=100, allow_inf_nan=False)
    max_cost_per_day: Optional[float] = Field(None, gt=0, le=1000, allow_inf_nan=False)
    # enabled, paused and the limits are still validated so an older client
    # gets the same errors, then ignored: research is automatic
    # (store.research_allowed) under the fixed store.RESEARCH_LIMITS.
    # introduction carries the owner's corrections to the detected one.
    introduction: Optional[Introduction] = None
    assistant_name: Optional[str] = Field(None, min_length=1, max_length=80)
    avatar_id: Optional[str] = None

    @field_validator("avatar_id")
    @classmethod
    def avatar(cls, value):
        if value is not None and value not in service.AVATAR_PRESETS:
            raise ValueError("Unknown avatar preset")
        return value


class OpenRequest(BaseModel):
    """``model`` is the client's main chat model (the composer's pick for new
    chats). It seeds a home that has no model yet and is ignored otherwise."""
    model_config = ConfigDict(extra="forbid")
    model: Optional[str] = Field(None, max_length=300)


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


class WorkerChoice(BaseModel):
    """The worker the assistant's work runs on; null or empty is this worker."""
    model_config = ConfigDict(extra="forbid")
    device_id: Optional[str] = Field(None, max_length=200)


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
        return Configuration.avatar(value)


async def _value_error_call(fn, *args):
    try:
        return await run_sync(fn, *args)
    except ValueError as exc:
        status = 404 if "does not exist" in str(exc) else 409
        raise HTTPException(status, str(exc)) from exc


# How long GET ?detect=true waits for a fresh owner-detection pass before
# answering with what is stored; the pass keeps running either way.
DETECT_WAIT_SECONDS = 4


@router.get("")
async def read_assistant(detect: bool = False):
    if detect:
        # Opening the Assistant page re-reads the owner's details, so a
        # connection made elsewhere shows up without waiting for the loop.
        task = assistant_manager.detect_owner(max_age=30)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=DETECT_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
    return await run_sync(service.get_state)


@router.put("")
@router.patch("")
async def configure_assistant(body: Configuration):
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key != "agent_id":
            raise HTTPException(422, f"{key} cannot be null; use empty text/lists to clear introduction values")
    if "introduction" in changes:
        for key, value in changes["introduction"].items():
            if value is None:
                raise HTTPException(422, f"introduction.{key} cannot be null")
    for key in (*store.LIMIT_KEYS, "enabled", "paused"):
        changes.pop(key, None)
    if changes.get("create_agent") is False:
        changes.pop("create_agent")
    state = await _value_error_call(service.configure, changes)
    await assistant_manager.cancel_invalid_runs()
    assistant_manager.notify()
    if changes.keys() & {"agent_id", "create_agent", "assistant_name", "avatar_id"}:
        # A new assistant learns about its owner right away.
        assistant_manager.detect_owner()
    return state


@router.post("/open")
async def open_assistant(body: Optional[OpenRequest] = Body(None)):
    session = await _value_error_call(service.open_home, body.model if body else None)
    return {"session_id": session["id"], "home_session_id": session["id"], "session": session}


# Older clients still call these. Research has no owner switch any more, so
# both return the current state and change nothing.
@router.post("/pause")
@router.post("/resume")
async def pause_or_resume_assistant():
    return await run_sync(service.get_state)


@router.get("/limits")
async def read_limits():
    """The owner's limits and today's spend for the assistant and its jobs."""
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


@router.get("/worker")
async def read_work_worker():
    """Where the assistant's work runs, and the other workers it could use."""
    return await run_sync(assistant_worker.read)


@router.put("/worker")
@router.patch("/worker")
async def update_work_worker(body: WorkerChoice):
    try:
        return await run_sync(assistant_worker.write, body.device_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


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


@router.get("/activity")
async def read_activity(limit: int = Query(50, ge=1, le=100)):
    return await run_sync(service.activity, limit)


@router.get("/agents")
async def read_agent_identities():
    return await run_sync(service.agent_identities)


@router.patch("/agents/{agent_id}/identity")
async def update_agent_identity(agent_id: str, body: AgentIdentity):
    return await _value_error_call(service.update_agent_identity, agent_id, body.name, body.avatar_id)
