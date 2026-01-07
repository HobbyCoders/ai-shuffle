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
# Subagent CRUD Endpoints
# ============================================================================

@router.get("", response_model=List[SubagentResponse])
async def list_subagents(token: str = Depends(require_auth)):
    """List all global subagents"""
    subagents = database.get_all_subagents()
    return [
        SubagentResponse(
            id=s["id"],
            name=s["name"],
            description=s["description"],
            prompt=s["prompt"],
            tools=s.get("tools"),
            model=s.get("model"),
            is_builtin=s.get("is_builtin", False),
            created_at=s["created_at"],
            updated_at=s["updated_at"]
        )
        for s in subagents
    ]


@router.get("/{subagent_id}", response_model=SubagentResponse)
async def get_subagent(subagent_id: str, token: str = Depends(require_auth)):
    """Get a specific subagent by ID"""
    subagent = database.get_subagent(subagent_id)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    return SubagentResponse(
        id=subagent["id"],
        name=subagent["name"],
        description=subagent["description"],
        prompt=subagent["prompt"],
        tools=subagent.get("tools"),
        model=subagent.get("model"),
        is_builtin=subagent.get("is_builtin", False),
        created_at=subagent["created_at"],
        updated_at=subagent["updated_at"]
    )


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

    return SubagentResponse(
        id=subagent["id"],
        name=subagent["name"],
        description=subagent["description"],
        prompt=subagent["prompt"],
        tools=subagent.get("tools"),
        model=subagent.get("model"),
        is_builtin=subagent.get("is_builtin", False),
        created_at=subagent["created_at"],
        updated_at=subagent["updated_at"]
    )


@router.put("/{subagent_id}", response_model=SubagentResponse)
async def update_subagent(
    subagent_id: str,
    request: SubagentUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update a subagent - Admin only. All subagents are editable."""
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

    return SubagentResponse(
        id=subagent["id"],
        name=subagent["name"],
        description=subagent["description"],
        prompt=subagent["prompt"],
        tools=subagent.get("tools"),
        model=subagent.get("model"),
        is_builtin=subagent.get("is_builtin", False),
        created_at=subagent["created_at"],
        updated_at=subagent["updated_at"]
    )


@router.delete("/{subagent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subagent(subagent_id: str, token: str = Depends(require_admin)):
    """Delete a subagent - Admin only. All subagents can be deleted."""
    existing = database.get_subagent(subagent_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    database.delete_subagent(subagent_id)
