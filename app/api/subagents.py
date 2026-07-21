"""
Global Subagent management API routes
"""

import json
from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from app.db import database
from app.api.auth import require_auth, require_admin
from app.core.environment import is_gateway_servable

router = APIRouter(prefix="/api/v1/subagents", tags=["Subagents"])


# ============================================================================
# Request/Response Models
# ============================================================================

class SubagentResponse(BaseModel):
    """Subagent response"""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    skills: Optional[List[str]] = None
    memory: Optional[str] = None
    mcp_servers: Optional[list] = None
    is_builtin: bool = False
    is_modified: bool = False  # Whether built-in subagent has been modified from defaults
    created_at: datetime
    updated_at: datetime


class SubagentCreateRequest(BaseModel):
    """Request to create a new subagent"""
    id: str = Field(..., pattern=r'^[a-z0-9-]+$', min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    skills: Optional[List[str]] = None
    memory: Optional[str] = None
    mcp_servers: Optional[list] = None


class SubagentUpdateRequest(BaseModel):
    """Request to update a subagent"""
    name: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    skills: Optional[List[str]] = None
    memory: Optional[str] = None
    mcp_servers: Optional[list] = None


# ============================================================================
# Helper Functions
# ============================================================================

def _build_subagent_response(s: dict) -> SubagentResponse:
    """Build a SubagentResponse from a database record"""
    is_builtin = s.get("is_builtin", False)
    is_modified = False
    if is_builtin:
        is_modified = database.has_subagent_been_modified(s["id"])

    return SubagentResponse(
        id=s["id"],
        name=s["name"],
        description=s["description"],
        prompt=s["prompt"],
        tools=s.get("tools"),
        model=s.get("model"),
        skills=s.get("skills"),
        memory=s.get("memory"),
        mcp_servers=s.get("mcp_servers"),
        is_builtin=is_builtin,
        is_modified=is_modified,
        created_at=s["created_at"],
        updated_at=s["updated_at"]
    )


# ============================================================================
# Subagent CRUD Endpoints
# ============================================================================

def _validate_subagent_model(model: Optional[str]) -> None:
    """Validate a subagent's stored ``model`` value at create/update time.

    A subagent ``model`` may take three shapes:
      * ``None`` / empty — inherit the profile/session model (no-op here),
      * a Claude alias or id (e.g. ``"sonnet"``, ``"claude-opus-4-8"``) — passed
        through untouched; this shape is never validated here,
      * a provider composite ``prov:<provider-id>:<model-id>`` — the only shape
        validated: the provider must exist, be enabled, be gateway-servable
        (OpenAI-, ChatGPT-, or Anthropic-API-type — the model routes through the
        translator gateway), and ``<model-id>`` must be in the provider's curated
        model list.

    The MAIN provider's servability is deliberately NOT checked here — it is
    profile-dependent and unknowable at subagent-edit time; that gate lives at
    spawn as a silent leg-drop (see docs/SUBAGENT_MODEL_CONFIG_PLAN.md §S4).

    Raises HTTP 400 on an invalid composite. No-op for inherit/Claude shapes.
    """
    if model is None or not model.strip():
        return
    if not model.startswith("prov:"):
        return
    remainder = model[len("prov:"):]
    pid, sep, mid = remainder.partition(":")
    if not pid or not sep or not mid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid provider model reference '{model}': expected the form "
                f"'prov:<provider-id>:<model-id>'."
            ),
        )
    row = database.get_model_provider(pid, include_token=False)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The provider '{pid}' does not exist.",
        )
    if not row.get("enabled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"The provider '{row.get('name') or pid}' is disabled; enable it "
                f"or pick a different provider."
            ),
        )
    if not is_gateway_servable(row):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"A provider-pinned subagent model requires an OpenAI-, ChatGPT-, "
                f"or Anthropic-API-type provider (the model routes through the "
                f"translator gateway); provider '{row.get('name') or pid}' cannot "
                f"serve subagent models."
            ),
        )
    # Curated model ids — parsed the same defensive way as
    # app.core.environment._first_curated_model_id (``models_json`` may be a
    # parsed list of {id, label} dicts, a raw JSON string, or absent), but
    # collecting ALL ids. An absent/empty/unparseable list means no composite
    # can validate.
    models = row.get("models_json")
    if isinstance(models, str):
        try:
            models = json.loads(models)
        except Exception:
            models = None
    curated_ids = set()
    if isinstance(models, list):
        for entry in models:
            if isinstance(entry, dict):
                entry_id = entry.get("id")
                if isinstance(entry_id, str) and entry_id:
                    curated_ids.add(entry_id)
    if mid not in curated_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Model '{mid}' is not in provider "
                f"'{row.get('name') or pid}''s curated model list."
            ),
        )


@router.get("", response_model=List[SubagentResponse])
async def list_subagents(token: str = Depends(require_auth)):
    """List all global subagents"""
    subagents = database.get_all_subagents()
    return [_build_subagent_response(s) for s in subagents]


@router.get("/{subagent_id}", response_model=SubagentResponse)
async def get_subagent(subagent_id: str, token: str = Depends(require_auth)):
    """Get a specific subagent by ID"""
    subagent = database.get_subagent(subagent_id)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    return _build_subagent_response(subagent)


@router.post("", response_model=SubagentResponse, status_code=status.HTTP_201_CREATED)
async def create_subagent(request: SubagentCreateRequest, token: str = Depends(require_admin)):
    """Create a new global subagent - Admin only"""
    # Check if ID already exists
    existing = database.get_subagent(request.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Subagent already exists: {request.id}"
        )

    _validate_subagent_model(request.model)

    subagent = database.create_subagent(
        subagent_id=request.id,
        name=request.name,
        description=request.description,
        prompt=request.prompt,
        tools=request.tools,
        model=request.model,
        skills=request.skills,
        memory=request.memory,
        mcp_servers=request.mcp_servers,
        is_builtin=False
    )

    return _build_subagent_response(subagent)


@router.put("/{subagent_id}", response_model=SubagentResponse)
async def update_subagent(
    subagent_id: str,
    request: SubagentUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update a subagent - Admin only. All subagents are editable (including built-in)."""
    existing = database.get_subagent(subagent_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    # Only pass fields that were explicitly provided in the request
    # This allows distinguishing between "not provided" and "set to null"
    fields = request.model_fields_set
    if 'model' in fields:
        _validate_subagent_model(request.model)
    subagent = database.update_subagent(
        subagent_id=subagent_id,
        name=request.name if 'name' in fields else ...,
        description=request.description if 'description' in fields else ...,
        prompt=request.prompt if 'prompt' in fields else ...,
        tools=request.tools if 'tools' in fields else ...,
        model=request.model if 'model' in fields else ...,
        skills=request.skills if 'skills' in fields else ...,
        memory=request.memory if 'memory' in fields else ...,
        mcp_servers=request.mcp_servers if 'mcp_servers' in fields else ...,
    )

    return _build_subagent_response(subagent)


@router.post("/{subagent_id}/revert", response_model=SubagentResponse)
async def revert_subagent(subagent_id: str, token: str = Depends(require_admin)):
    """
    Revert a built-in subagent to its original default settings.

    Only works for built-in subagents. Admin only.
    """
    existing = database.get_subagent(subagent_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    if not existing.get("is_builtin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only built-in subagents can be reverted to defaults"
        )

    subagent = database.revert_subagent_to_defaults(subagent_id)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revert subagent - no defaults stored"
        )

    return _build_subagent_response(subagent)


@router.delete("/{subagent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subagent(subagent_id: str, token: str = Depends(require_admin)):
    """Delete a subagent - Admin only. Built-in subagents cannot be deleted."""
    existing = database.get_subagent(subagent_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    if existing.get("is_builtin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Built-in subagents cannot be deleted. You can disable them in profile settings."
        )

    database.delete_subagent(subagent_id)
