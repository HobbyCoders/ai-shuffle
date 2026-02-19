"""
Workspace-level file management API routes.

Provides file browsing, upload, download, rename, and delete operations
scoped to the workspace root directory (not project-specific).
"""

import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends, status, UploadFile, File, Query, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.config import settings
from app.api.auth import require_auth
from app.api.projects import format_file_size, get_file_extension

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workspace", tags=["Workspace Files"])


# Request models
class CreateFolderRequest(BaseModel):
    name: str


class RenameRequest(BaseModel):
    new_name: str


# Windows reserved device names — Python's pathlib resolves these to device
# paths instead of actual files, breaking exists/is_file/unlink operations.
_WINDOWS_RESERVED_NAMES = frozenset({
    'CON', 'PRN', 'AUX', 'NUL',
    'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
    'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
})


def _is_windows_reserved_name(name: str) -> bool:
    """Check if a filename is a Windows reserved device name."""
    # Windows treats 'nul', 'nul.txt', 'NUL.anything' all as the NUL device
    stem = name.split('.')[0].upper()
    return sys.platform == 'win32' and stem in _WINDOWS_RESERVED_NAMES


def validate_workspace_path(path: str) -> Path:
    """
    Validate and resolve a path within the workspace directory.
    Prevents path traversal outside the workspace boundary.
    """
    workspace = settings.workspace_dir.resolve()
    raw_path = workspace / path

    # On Windows, resolve() converts reserved names (NUL, CON, etc.) to device
    # paths, breaking the workspace boundary check. Resolve the parent instead.
    if _is_windows_reserved_name(raw_path.name):
        full_path = raw_path.parent.resolve() / raw_path.name
    else:
        full_path = raw_path.resolve()

    try:
        full_path.relative_to(workspace)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path escapes workspace boundary"
        )

    return full_path


@router.get("/files")
async def list_workspace_files(
    path: str = "",
    show_hidden: bool = False,
    token: str = Depends(require_auth)
):
    """List files in the workspace directory."""
    workspace = settings.workspace_dir.resolve()

    if path:
        full_path = validate_workspace_path(path)
    else:
        full_path = workspace

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

    files = []
    for item in full_path.iterdir():
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
                "path": str(item.relative_to(workspace)).replace("\\", "/"),
                "extension": get_file_extension(item.name) if not is_dir else None,
                "modifiedAt": datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                "createdAt": datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            }
            files.append(file_info)
        except (OSError, PermissionError):
            continue

    files.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))

    return {"files": files, "path": path}


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form(default=""),
    token: str = Depends(require_auth)
):
    """Upload a file to the workspace."""
    workspace = settings.workspace_dir.resolve()

    if path:
        target_dir = validate_workspace_path(path)
    else:
        target_dir = workspace

    target_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = Path(file.filename).name
    if not safe_filename or safe_filename.startswith('.'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )

    file_path = target_dir / safe_filename

    if file_path.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        counter = 1
        while file_path.exists():
            file_path = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    try:
        content = await file.read()
        file_path.write_bytes(content)
    except Exception as e:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save file"
        )

    relative_path = file_path.relative_to(workspace)

    return {
        "filename": file_path.name,
        "path": relative_path.as_posix(),
        "size": len(content),
        "sizeFormatted": format_file_size(len(content))
    }


@router.get("/files/download")
async def download_file(
    path: str = Query(..., description="Path to file relative to workspace root"),
    token: str = Depends(require_auth)
):
    """Download a file from the workspace."""
    full_path = validate_workspace_path(path)

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


@router.post("/files/folder")
async def create_folder(
    body: CreateFolderRequest,
    path: str = Query(default="", description="Parent directory path"),
    token: str = Depends(require_auth)
):
    """Create a new folder within the workspace."""
    workspace = settings.workspace_dir.resolve()

    folder_name = body.name.strip()
    if not folder_name or "/" in folder_name or "\\" in folder_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid folder name"
        )

    if path:
        folder_path = validate_workspace_path(f"{path}/{folder_name}")
    else:
        folder_path = validate_workspace_path(folder_name)

    if folder_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Folder already exists"
        )

    try:
        folder_path.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        logger.exception("Failed to create folder")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create folder"
        )

    return {
        "name": folder_name,
        "path": str(folder_path.relative_to(workspace)).replace("\\", "/"),
        "type": "directory"
    }


@router.put("/files/rename")
async def rename_file_or_folder(
    body: RenameRequest,
    path: str = Query(..., description="Path to file or folder to rename"),
    token: str = Depends(require_auth)
):
    """Rename a file or folder within the workspace."""
    workspace = settings.workspace_dir.resolve()

    new_name = body.new_name.strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid name"
        )

    current_path = validate_workspace_path(path)
    if not current_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File or folder not found"
        )

    new_path = current_path.parent / new_name
    if new_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A file or folder with that name already exists"
        )

    try:
        current_path.rename(new_path)
    except Exception as e:
        logger.exception("Failed to rename file or folder")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rename"
        )

    return {
        "name": new_name,
        "path": str(new_path.relative_to(workspace)).replace("\\", "/"),
        "type": "directory" if new_path.is_dir() else "file"
    }


@router.delete("/files")
async def delete_file_or_folder(
    path: str = Query(..., description="Path to file or folder to delete"),
    token: str = Depends(require_auth)
):
    """Delete a file or folder within the workspace."""
    workspace = settings.workspace_dir.resolve()

    target_path = validate_workspace_path(path)
    is_reserved = _is_windows_reserved_name(target_path.name)

    # For Windows reserved names (NUL, CON, etc.), pathlib resolves them to
    # device paths instead of actual files. Use the \\?\ extended path prefix
    # to bypass device name resolution for all operations.
    if is_reserved:
        extended = "\\\\?\\" + str(target_path.parent.resolve()) + "\\" + target_path.name
        if not os.path.exists(extended):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File or folder not found"
            )
    elif not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File or folder not found"
        )

    if not is_reserved and target_path.resolve() == workspace:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete workspace root directory"
        )

    try:
        if is_reserved:
            os.remove(extended)
        elif target_path.is_dir():
            shutil.rmtree(target_path)
        else:
            target_path.unlink()
    except Exception as e:
        logger.exception("Failed to delete file or folder")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete"
        )

    return {"deleted": path}
