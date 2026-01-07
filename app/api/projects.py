"""
Project management API routes
"""

import shutil
from datetime import datetime
from typing import List, Optional
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File, Request, Query, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.models import Project, ProjectCreate, ProjectUpdate
from app.core.config import settings
from app.db import database
from app.api.auth import require_auth, require_admin, get_api_user_from_request, is_admin_request


# File operation request models
class CreateFolderRequest(BaseModel):
    name: str


class RenameRequest(BaseModel):
    new_name: str

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


def check_project_access(request: Request, project_id: str) -> None:
    """Check if the user has access to the project. Raises HTTPException if not."""
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )


@router.get("", response_model=List[Project])
async def list_projects(request: Request, token: str = Depends(require_auth)):
    """List all projects. API users only see their assigned project."""
    api_user = get_api_user_from_request(request)

    if api_user and api_user.get("project_id"):
        # API user with assigned project - only return that project
        project = database.get_project(api_user["project_id"])
        return [project] if project else []

    # Admin or unrestricted API user - return all projects
    projects = database.get_all_projects()
    return projects


@router.get("/{project_id}", response_model=Project)
async def get_project(request: Request, project_id: str, token: str = Depends(require_auth)):
    """Get a specific project. API users can only access their assigned project."""
    check_project_access(request, project_id)

    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )
    return project


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(request: ProjectCreate, token: str = Depends(require_admin)):
    """Create a new project - Admin only"""
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
    token: str = Depends(require_admin)
):
    """Update a project - Admin only"""
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
async def delete_project(project_id: str, token: str = Depends(require_admin)):
    """Delete a project (database record only, not files) - Admin only"""
    existing = database.get_project(project_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    database.delete_project(project_id)


def get_file_extension(name: str) -> str:
    """Get lowercase file extension without the dot"""
    ext = Path(name).suffix.lower()
    return ext[1:] if ext else ""


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


@router.get("/{project_id}/files")
async def list_project_files(
    request: Request,
    project_id: str,
    path: str = "",
    show_hidden: bool = False,
    token: str = Depends(require_auth)
):
    """List files in a project directory. API users can only access their assigned project."""
    check_project_access(request, project_id)
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
        # Skip hidden files unless requested
        if item.name.startswith('.') and not show_hidden:
            continue

        try:
            stat_info = item.stat()
            is_dir = item.is_dir()

            file_info = {
                "name": item.name,
                "type": "directory" if is_dir else "file",
                "size": stat_info.st_size if not is_dir else None,
                "sizeFormatted": format_file_size(stat_info.st_size) if not is_dir else None,
                "path": str(item.relative_to(base_path)),
                "extension": get_file_extension(item.name) if not is_dir else None,
                "modifiedAt": datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                "createdAt": datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            }
            files.append(file_info)
        except (OSError, PermissionError):
            # Skip files we can't access
            continue

    # Sort: directories first, then files
    files.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))

    return {"files": files, "path": path, "projectId": project_id}


@router.post("/{project_id}/upload")
async def upload_file(
    request: Request,
    project_id: str,
    file: UploadFile = File(...),
    path: str = Form(default=""),
    token: str = Depends(require_auth)
):
    """Upload a file to a specific path within the project. API users can only access their assigned project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Build target directory path
    if path:
        target_dir = validate_project_path(f"{project['path']}/{path}")
    else:
        # Default to uploads subdirectory to keep project root clean
        target_dir = validate_project_path(f"{project['path']}/uploads")

    # Ensure directory exists
    target_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename
    safe_filename = Path(file.filename).name
    if not safe_filename or safe_filename.startswith('.'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )

    # Full file path
    file_path = target_dir / safe_filename

    # Handle duplicate filenames by adding counter
    if file_path.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        counter = 1
        while file_path.exists():
            file_path = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    # Save the file
    try:
        content = await file.read()
        file_path.write_bytes(content)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save file: {str(e)}"
        )

    # Return relative path for reference
    base_path = settings.workspace_dir / project["path"]
    relative_path = file_path.relative_to(base_path)

    return {
        "filename": file_path.name,
        "path": str(relative_path),
        "full_path": str(file_path),
        "size": len(content),
        "sizeFormatted": format_file_size(len(content))
    }


@router.get("/{project_id}/files/download")
async def download_file(
    request: Request,
    project_id: str,
    path: str = Query(..., description="Path to file relative to project root"),
    token: str = Depends(require_auth)
):
    """Download a file from the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Validate and build full path
    full_path = validate_project_path(f"{project['path']}/{path}")

    if not full_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found"
        )

    if not full_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a file"
        )

    return FileResponse(
        path=full_path,
        filename=full_path.name,
        media_type="application/octet-stream"
    )


@router.post("/{project_id}/files/folder")
async def create_folder(
    request: Request,
    project_id: str,
    body: CreateFolderRequest,
    path: str = Query(default="", description="Parent directory path"),
    token: str = Depends(require_auth)
):
    """Create a new folder within the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Validate folder name
    folder_name = body.name.strip()
    if not folder_name or "/" in folder_name or "\\" in folder_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid folder name"
        )

    # Build the folder path
    if path:
        folder_path = validate_project_path(f"{project['path']}/{path}/{folder_name}")
    else:
        folder_path = validate_project_path(f"{project['path']}/{folder_name}")

    if folder_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Folder already exists"
        )

    try:
        folder_path.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create folder: {str(e)}"
        )

    base_path = settings.workspace_dir / project["path"]
    return {
        "name": folder_name,
        "path": str(folder_path.relative_to(base_path)),
        "type": "directory"
    }


@router.put("/{project_id}/files/rename")
async def rename_file_or_folder(
    request: Request,
    project_id: str,
    body: RenameRequest,
    path: str = Query(..., description="Path to file or folder to rename"),
    token: str = Depends(require_auth)
):
    """Rename a file or folder within the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Validate new name
    new_name = body.new_name.strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid name"
        )

    # Get current path
    current_path = validate_project_path(f"{project['path']}/{path}")
    if not current_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File or folder not found"
        )

    # Build new path
    new_path = current_path.parent / new_name
    if new_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A file or folder with that name already exists"
        )

    try:
        current_path.rename(new_path)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to rename: {str(e)}"
        )

    base_path = settings.workspace_dir / project["path"]
    return {
        "name": new_name,
        "path": str(new_path.relative_to(base_path)),
        "type": "directory" if new_path.is_dir() else "file"
    }


@router.delete("/{project_id}/files")
async def delete_file_or_folder(
    request: Request,
    project_id: str,
    path: str = Query(..., description="Path to file or folder to delete"),
    token: str = Depends(require_auth)
):
    """Delete a file or folder within the project."""
    check_project_access(request, project_id)
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Get the path to delete
    target_path = validate_project_path(f"{project['path']}/{path}")
    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File or folder not found"
        )

    # Don't allow deleting the project root
    base_path = settings.workspace_dir / project["path"]
    if target_path.resolve() == base_path.resolve():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete project root directory"
        )

    try:
        if target_path.is_dir():
            shutil.rmtree(target_path)
        else:
            target_path.unlink()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete: {str(e)}"
        )

    return {"deleted": path}


# ============================================================================
# Git/Worktree Sync
# ============================================================================

class WorktreeSyncResponse(BaseModel):
    """Response from worktree sync operation"""
    is_git_repo: bool
    active_worktrees: int
    cleaned_up: List[dict]


@router.post("/{project_id}/sync-worktrees", response_model=WorktreeSyncResponse)
async def sync_project_worktrees(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """
    Sync worktree database records with actual state on disk.

    Should be called when loading a chat card for a project to ensure
    the database reflects reality (worktrees deleted outside the app).

    Returns:
        is_git_repo: Whether this project is a git repository
        active_worktrees: Count of valid active worktrees
        cleaned_up: List of stale worktree records that were cleaned up
    """
    from app.core.worktree_manager import worktree_manager

    # Check project access
    check_project_access(request, project_id)

    # Verify project exists
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    # Run sync
    result = worktree_manager.sync_worktrees_for_project(project_id)

    return WorktreeSyncResponse(
        is_git_repo=result.get("is_git_repo", False),
        active_worktrees=result.get("active", 0),
        cleaned_up=result.get("cleaned_up", [])
    )
