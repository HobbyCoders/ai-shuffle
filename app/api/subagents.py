"""
Global Subagent management API routes
"""

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from app.db import database
from app.api.auth import require_auth, require_admin

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
    is_builtin: bool = False
    is_modified: bool = False  # Only relevant for built-in subagents
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


class SubagentUpdateRequest(BaseModel):
    """Request to update a subagent"""
    name: Optional[str] = None
    description: Optional[str] = None
    prompt: Optional[str] = None
    tools: Optional[List[str]] = None
    model: Optional[str] = None


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
        is_builtin=is_builtin,
        is_modified=is_modified,
        created_at=s["created_at"],
        updated_at=s["updated_at"]
    )


# ============================================================================
# Subagent CRUD Endpoints
# ============================================================================

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

    subagent = database.create_subagent(
        subagent_id=request.id,
        name=request.name,
        description=request.description,
        prompt=request.prompt,
        tools=request.tools,
        model=request.model,
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

    subagent = database.update_subagent(
        subagent_id=subagent_id,
        name=request.name,
        description=request.description,
        prompt=request.prompt,
        tools=request.tools,
        model=request.model
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
