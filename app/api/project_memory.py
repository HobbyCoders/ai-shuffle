"""
Project memory API routes - manages AGENTS.md files in project directories.

Memory files are plain markdown files at two locations:
- <project>/AGENTS.md         (project-level, shared)
- <project>/.agents/AGENTS.md (local, private)

Legacy CLAUDE.md / .claude/CLAUDE.md files are still read as a fallback when
no AGENTS.md exists at that location, but saves always write AGENTS.md.
"""

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.api.auth import require_auth
from app.api.common import validate_project_access, get_project_path_from_dict
from app.core.project_memory_service import (
    get_project_memory_service,
    MEMORY_LOCATIONS,
)

router = APIRouter(prefix="/api/v1/projects", tags=["Project Memory"])


# ============================================================================
# Request/Response Models
# ============================================================================

class MemoryFileResponse(BaseModel):
    """Memory file response"""
    location: str
    exists: bool
    content: str
    file_path: str
    size: int
    updated_at: Optional[datetime] = None


class MemoryFileSave(BaseModel):
    """Request body for creating/updating a memory file"""
    content: str = Field(..., description="Markdown content for the memory file (AGENTS.md)")


# ============================================================================
# API Endpoints
# ============================================================================

@router.get(
    "/{project_id}/memory",
    response_model=List[MemoryFileResponse],
)
async def list_memory_files(
    project_id: str,
    request: Request,
    token: str = Depends(require_auth),
):
    """
    List all memory file locations with their current state.

    Returns info for both project and local AGENTS.md files,
    including those that don't exist yet.
    """
    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_memory_service(project_path)
    files = service.get_all()

    return [
        MemoryFileResponse(
            location=f.location,
            exists=f.exists,
            content=f.content,
            file_path=f.file_path,
            size=f.size,
            updated_at=f.updated_at,
        )
        for f in files
    ]


@router.get(
    "/{project_id}/memory/{location}",
    response_model=MemoryFileResponse,
)
async def get_memory_file(
    project_id: str,
    location: str,
    request: Request,
    token: str = Depends(require_auth),
):
    """
    Get a specific memory file by location.

    Location must be "project" (AGENTS.md) or "local" (.agents/AGENTS.md).
    """
    if location not in MEMORY_LOCATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid location: '{location}'. Must be one of: {', '.join(MEMORY_LOCATIONS.keys())}",
        )

    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_memory_service(project_path)
    f = service.get(location)

    return MemoryFileResponse(
        location=f.location,
        exists=f.exists,
        content=f.content,
        file_path=f.file_path,
        size=f.size,
        updated_at=f.updated_at,
    )


@router.put(
    "/{project_id}/memory/{location}",
    response_model=MemoryFileResponse,
)
async def save_memory_file(
    project_id: str,
    location: str,
    data: MemoryFileSave,
    request: Request,
    token: str = Depends(require_auth),
):
    """
    Create or update a memory file.

    Location must be "project" (AGENTS.md) or "local" (.agents/AGENTS.md).
    """
    if location not in MEMORY_LOCATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid location: '{location}'. Must be one of: {', '.join(MEMORY_LOCATIONS.keys())}",
        )

    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_memory_service(project_path)
    f = service.save(location, data.content)

    return MemoryFileResponse(
        location=f.location,
        exists=f.exists,
        content=f.content,
        file_path=f.file_path,
        size=f.size,
        updated_at=f.updated_at,
    )


@router.delete(
    "/{project_id}/memory/{location}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_memory_file(
    project_id: str,
    location: str,
    request: Request,
    token: str = Depends(require_auth),
):
    """
    Delete a memory file.

    Location must be "project" (AGENTS.md) or "local" (.agents/AGENTS.md).
    """
    if location not in MEMORY_LOCATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid location: '{location}'. Must be one of: {', '.join(MEMORY_LOCATIONS.keys())}",
        )

    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_memory_service(project_path)

    try:
        service.delete(location)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
