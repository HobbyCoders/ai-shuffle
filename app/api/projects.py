"""
Project management API routes
"""

from typing import List
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends, status

from app.core.models import Project, ProjectCreate, ProjectUpdate
from app.core.config import settings
from app.db import database
from app.api.auth import require_auth

router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])


def validate_project_path(path: str) -> Path:
    """Ensure path is within workspace and normalized"""
    # Normalize and resolve the path
    full_path = (settings.workspace_dir / path).resolve()

    # Security check - ensure path is within workspace
    try:
        full_path.relative_to(settings.workspace_dir.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes workspace boundary"
        )

    return full_path


@router.get("", response_model=List[Project])
async def list_projects(token: str = Depends(require_auth)):
    """List all projects"""
    projects = database.get_all_projects()
    return projects


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, token: str = Depends(require_auth)):
    """Get a specific project"""
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )
    return project


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(request: ProjectCreate, token: str = Depends(require_auth)):
    """Create a new project"""
    # Check if ID already exists
    existing = database.get_project(request.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Project already exists: {request.id}"
        )

    # Validate and create project directory
    project_path = validate_project_path(request.id)
    project_path.mkdir(parents=True, exist_ok=True)

    # Create project in database
    settings_dict = None
    if request.settings:
        settings_dict = request.settings.model_dump(exclude_none=True)

    project = database.create_project(
        project_id=request.id,
        name=request.name,
        description=request.description,
        path=request.id,  # Path relative to /workspace
        settings_dict=settings_dict
    )

    return project


@router.put("/{project_id}", response_model=Project)
async def update_project(
    project_id: str,
    request: ProjectUpdate,
    token: str = Depends(require_auth)
):
    """Update a project"""
    existing = database.get_project(project_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    settings_dict = None
    if request.settings:
        settings_dict = request.settings.model_dump(exclude_none=True)

    project = database.update_project(
        project_id=project_id,
        name=request.name,
        description=request.description,
        settings_dict=settings_dict
    )

    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: str, token: str = Depends(require_auth)):
    """Delete a project (database record only, not files)"""
    existing = database.get_project(project_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    database.delete_project(project_id)


@router.get("/{project_id}/files")
async def list_project_files(
    project_id: str,
    path: str = "",
    token: str = Depends(require_auth)
):
    """List files in a project directory"""
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Build the full path
    base_path = settings.workspace_dir / project["path"]
    if path:
        full_path = validate_project_path(f"{project['path']}/{path}")
    else:
        full_path = base_path

    if not full_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Directory not found"
        )

    if not full_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory"
        )

    # List directory contents
    files = []
    for item in full_path.iterdir():
        # Skip hidden files
        if item.name.startswith('.'):
            continue

        files.append({
            "name": item.name,
            "type": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
            "path": str(item.relative_to(base_path))
        })

    # Sort: directories first, then files
    files.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))

    return {"files": files, "path": path}
