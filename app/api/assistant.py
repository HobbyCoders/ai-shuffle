"""Owner-only persistent assistant API; normal auth and same-origin CSRF apply."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.auth import require_admin
from app.core import assistant_service as service
from app.core.assistant_manager import assistant_manager
from app.db import assistant as store
from app.db.async_database import run_sync

router = APIRouter(prefix="/api/v1/assistant", tags=["Assistant"], dependencies=[Depends(require_admin)])


class Introduction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_name: Optional[str] = Field(None, max_length=80)
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
    agent_id: Optional[str] = Field(None, min_length=1, max_length=128)
    enabled: Optional[bool] = None
    paused: Optional[bool] = None
    max_runs_per_day: Optional[int] = Field(None, ge=1, le=100)
    max_concurrent_runs: Optional[int] = Field(None, ge=1, le=3)
    max_turns: Optional[int] = Field(None, ge=1, le=20)
    timeout_seconds: Optional[int] = Field(None, ge=30, le=900)
    max_cost_per_run: Optional[float] = Field(None, gt=0, le=100, allow_inf_nan=False)
    max_cost_per_day: Optional[float] = Field(None, gt=0, le=1000, allow_inf_nan=False)
    introduction: Optional[Introduction] = None
    assistant_name: Optional[str] = Field(None, min_length=1, max_length=80)
    avatar_id: Optional[str] = None

    @field_validator("avatar_id")
    @classmethod
    def avatar(cls, value):
        if value is not None and value not in service.AVATAR_PRESETS:
            raise ValueError("Unknown avatar preset")
        return value


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    avatar_id: Optional[str] = None

    @field_validator("avatar_id")
    @classmethod
    def avatar(cls, value):
        return Configuration.avatar(value)


async def _value_error_call(fn, *args):
    try:
        return await run_sync(fn, *args)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("")
async def read_assistant():
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
    state = await _value_error_call(service.configure, changes)
    await assistant_manager.cancel_invalid_runs()
    assistant_manager.notify()
    return state


@router.post("/open")
async def open_assistant():
    session = await _value_error_call(service.open_home)
    return {"session_id": session["id"], "home_session_id": session["id"], "session": session}


@router.post("/pause")
async def pause_assistant():
    state = await _value_error_call(service.configure, {"paused": True})
    await assistant_manager.cancel_invalid_runs()
    assistant_manager.notify()
    return state


@router.post("/resume")
async def resume_assistant():
    state = await run_sync(service.get_state)
    if not state["configured"]:
        raise HTTPException(409, "Choose an assistant agent before enabling it")
    state = await _value_error_call(service.configure, {"enabled": True, "paused": False})
    assistant_manager.notify()
    return state


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
    return await run_sync(store.get_activity, service.assistant_id(), limit)


@router.get("/agents")
async def read_agent_identities():
    return await run_sync(service.agent_identities)


@router.patch("/agents/{agent_id}/identity")
async def update_agent_identity(agent_id: str, body: AgentIdentity):
    return await _value_error_call(service.update_agent_identity, agent_id, body.name, body.avatar_id)
