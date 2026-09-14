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

from fastapi import (
    APIRouter, HTTPException, Depends, status, Request, UploadFile, File, Query, Form
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.config import settings
from app.db.async_database import run_sync
from app.api.auth import require_auth
from app.api.common import require_file_access
# The size cap comes from the project upload rather than being restated here:
# the composer's attach button posts to one endpoint or the other depending on
# the session's lane, and a file the user can attach in one lane and not the
# other would be a difference with no explanation behind it.
from app.api.projects import (
    MAX_UPLOAD_SIZE,
    _read_upload_within_limit,
    format_file_size,
    get_file_extension,
)

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
    workspace = settings.effective_workspace_dir.resolve()
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


async def authorize_workspace_path(request: Request, full_path: Path) -> None:
    """Keep a project-scoped API key inside its own project directories.

    validate_workspace_path only stops traversal out of the workspace; a key
    scoped to one project must not reach the sibling projects that live under
    the same workspace root. Unscoped callers keep whole-workspace access.
    """
    await run_sync(require_file_access, full_path, settings.effective_workspace_dir.resolve(), request)


@router.get("/files")
async def list_workspace_files(
    request: Request,
    path: str = "",
    show_hidden: bool = False,
    token: str = Depends(require_auth)
):
    """List files in the workspace directory."""
    workspace = settings.effective_workspace_dir.resolve()

    if path:
        full_path = validate_workspace_path(path)
    else:
        full_path = workspace
    await authorize_workspace_path(request, full_path)

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
    request: Request,
    file: UploadFile = File(...),
    path: str = Form(default=""),
    token: str = Depends(require_auth)
):
    """Upload a file to the workspace."""
    # Same two-stage cap as the project upload: a Content-Length pre-check so an
    # oversized body is refused before it is streamed at all, then a chunked
    # read that enforces the real limit without trusting that (spoofable)
    # header. This endpoint used to be reachable only from the workspace file
    # browser; the chat composer now points every attachment at it, so an
    # uncapped ``await file.read()`` here would buffer whatever the user dragged
    # onto the composer into the worker's memory.
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum upload size is {MAX_UPLOAD_SIZE // (1024 * 1024)}MB"
        )

    workspace = settings.effective_workspace_dir.resolve()

    if path:
        target_dir = validate_workspace_path(path)
    else:
        target_dir = workspace
    await authorize_workspace_path(request, target_dir)

    safe_filename = Path(file.filename or "upload").name
    if not safe_filename or safe_filename.startswith('.'):
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Outside the try: an over-cap read raises HTTPException(413), and the
    # blanket handler below would otherwise relabel it as a 500 "Failed to save
    # file" — the one message that tells the user nothing about what to change.
    content = await _read_upload_within_limit(file)
    try:
        from app.core.confined_files import upload
        file_path = await run_sync(upload, workspace, path, safe_filename, content)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid upload path")
    except Exception:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save file"
        )

    relative_path = file_path.relative_to(workspace)

    return {
        "filename": file_path.name,
        "path": relative_path.as_posix(),
        # Absolute path too, matching the project upload's shape: the composer
        # turns an attachment into an ``@<fullPath>`` reference for the agent to
        # open, and a workspace-relative path would resolve against the wrong
        # root for anything that is not running from the workspace itself.
        # Forward slashes so the reference survives a round trip through the
        # prompt text on Windows.
        "full_path": file_path.as_posix(),
        "size": len(content),
        "sizeFormatted": format_file_size(len(content))
    }


@router.get("/files/download")
async def download_file(
    request: Request,
    path: str = Query(..., description="Path to file relative to workspace root"),
    token: str = Depends(require_auth)
):
    """Download a file from the workspace."""
    full_path = validate_workspace_path(path)
    await authorize_workspace_path(request, full_path)

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
    request: Request,
    body: CreateFolderRequest,
    path: str = Query(default="", description="Parent directory path"),
    token: str = Depends(require_auth)
):
    """Create a new folder within the workspace."""
    workspace = settings.effective_workspace_dir.resolve()

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
    await authorize_workspace_path(request, folder_path)

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
    request: Request,
    body: RenameRequest,
    path: str = Query(..., description="Path to file or folder to rename"),
    token: str = Depends(require_auth)
):
    """Rename a file or folder within the workspace."""
    workspace = settings.effective_workspace_dir.resolve()

    new_name = body.new_name.strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid name"
        )

    current_path = validate_workspace_path(path)
    await authorize_workspace_path(request, current_path)
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
    request: Request,
    path: str = Query(..., description="Path to file or folder to delete"),
    token: str = Depends(require_auth)
):
    """Delete a file or folder within the workspace."""
    workspace = settings.effective_workspace_dir.resolve()

    target_path = validate_workspace_path(path)
    await authorize_workspace_path(request, target_path)
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

    def _delete() -> None:
        if is_reserved:
            os.remove(extended)
        elif target_path.is_dir():
            shutil.rmtree(target_path)
        else:
            target_path.unlink()

    try:
        # Offloaded: rmtree walks the whole subtree, so a big folder would
        # otherwise stall every other request on this worker.
        await run_sync(_delete)
    except Exception as e:
        logger.exception("Failed to delete file or folder")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete"
        )

    return {"deleted": path}
