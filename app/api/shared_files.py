"""
API endpoint for serving shared/downloadable files.

This endpoint serves files that Claude creates for users to download,
allowing them to be displayed in the chat UI with download cards.

Files are stored in 'shared-files' subdirectories within project
working directories. The API validates that requested files are within
allowed workspace paths.
"""

import os
from pathlib import Path
from fastapi import APIRouter, HTTPException, status, Query
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter(prefix="/api/files", tags=["files"])

# File extension to MIME type mapping for common file types
MEDIA_TYPES = {
    # Documents
    '.pdf': 'application/pdf',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.odt': 'application/vnd.oasis.opendocument.text',
    '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
    '.odp': 'application/vnd.oasis.opendocument.presentation',
    # Text
    '.txt': 'text/plain',
    '.md': 'text/markdown',
    '.csv': 'text/csv',
    '.log': 'text/plain',
    '.rtf': 'application/rtf',
    # Code/Config
    '.json': 'application/json',
    '.xml': 'application/xml',
    '.yaml': 'application/x-yaml',
    '.yml': 'application/x-yaml',
    '.toml': 'application/toml',
    '.ini': 'text/plain',
    '.cfg': 'text/plain',
    '.conf': 'text/plain',
    '.env': 'text/plain',
    # Scripts
    '.py': 'text/x-python',
    '.js': 'text/javascript',
    '.mjs': 'text/javascript',
    '.ts': 'text/typescript',
    '.sh': 'text/x-shellscript',
    '.bash': 'text/x-shellscript',
    '.bat': 'text/x-batch',
    '.ps1': 'text/plain',
    '.sql': 'application/sql',
    '.html': 'text/html',
    '.css': 'text/css',
    # Archives
    '.zip': 'application/zip',
    '.tar': 'application/x-tar',
    '.gz': 'application/gzip',
    '.tgz': 'application/gzip',
    '.bz2': 'application/x-bzip2',
    '.7z': 'application/x-7z-compressed',
    '.rar': 'application/vnd.rar',
    # Data
    '.sqlite': 'application/x-sqlite3',
    '.db': 'application/x-sqlite3',
    '.parquet': 'application/vnd.apache.parquet',
    # Other
    '.exe': 'application/x-msdownload',
    '.dmg': 'application/x-apple-diskimage',
    '.deb': 'application/x-deb',
    '.rpm': 'application/x-rpm',
    '.apk': 'application/vnd.android.package-archive',
}

# File extension to icon mapping (for frontend)
FILE_ICONS = {
    'pdf': '📄',
    'doc': '📝', 'docx': '📝', 'odt': '📝', 'rtf': '📝',
    'xls': '📊', 'xlsx': '📊', 'ods': '📊', 'csv': '📊',
    'ppt': '📽️', 'pptx': '📽️', 'odp': '📽️',
    'txt': '📃', 'md': '📃', 'log': '📃',
    'json': '🔧', 'xml': '🔧', 'yaml': '🔧', 'yml': '🔧', 'toml': '🔧',
    'py': '🐍', 'js': '💛', 'ts': '💙', 'mjs': '💛',
    'sh': '⚙️', 'bash': '⚙️', 'bat': '⚙️', 'ps1': '⚙️',
    'html': '🌐', 'css': '🎨',
    'sql': '🗃️', 'sqlite': '🗃️', 'db': '🗃️',
    'zip': '📦', 'tar': '📦', 'gz': '📦', 'tgz': '📦', '7z': '📦', 'rar': '📦',
    'exe': '⚡', 'dmg': '💿', 'deb': '📦', 'rpm': '📦', 'apk': '📱',
}


def is_path_safe(file_path: Path, base_path: Path) -> bool:
    """
    Check if a file path is safely within the base path.
    Prevents path traversal attacks.
    """
    try:
        # Resolve both paths to absolute paths
        resolved_file = file_path.resolve()
        resolved_base = base_path.resolve()

        # Check if the file is within the base directory
        return resolved_file.is_relative_to(resolved_base)
    except (ValueError, RuntimeError):
        return False


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


def get_file_icon(filename: str) -> str:
    """Get emoji icon for a file based on extension."""
    ext = Path(filename).suffix.lower().lstrip('.')
    return FILE_ICONS.get(ext, '📁')


@router.get("/by-path")
async def get_file_by_path(path: str = Query(..., description="Full path to the file")):
    """
    Serve a shared file by its full path.

    Security: Only serves files from within the workspace directory
    and specifically from 'shared-files' directories.
    """
    file_path = Path(path)

    # Security check: must be within workspace
    workspace = settings.effective_workspace_dir
    if not is_path_safe(file_path, workspace):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: file is outside workspace"
        )

    # Additional check: must be in a 'shared-files' directory
    if 'shared-files' not in file_path.parts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not a shared file"
        )

    # Check file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found"
        )

    suffix = file_path.suffix.lower()
    media_type = MEDIA_TYPES.get(suffix, 'application/octet-stream')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name
    )


@router.get("/info")
async def get_file_info(path: str = Query(..., description="Full path to the file")):
    """
    Get metadata about a shared file without downloading it.

    Returns file name, size, type, and icon information.
    """
    file_path = Path(path)

    # Security check: must be within workspace
    workspace = settings.effective_workspace_dir
    if not is_path_safe(file_path, workspace):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: file is outside workspace"
        )

    # Additional check: must be in a 'shared-files' directory
    if 'shared-files' not in file_path.parts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not a shared file"
        )

    # Check file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found"
        )

    stat = file_path.stat()
    suffix = file_path.suffix.lower()

    return {
        "filename": file_path.name,
        "path": str(file_path),
        "size_bytes": stat.st_size,
        "size_formatted": format_file_size(stat.st_size),
        "extension": suffix.lstrip('.'),
        "mime_type": MEDIA_TYPES.get(suffix, 'application/octet-stream'),
        "icon": get_file_icon(file_path.name),
        "created_at": stat.st_ctime,
        "modified_at": stat.st_mtime,
        "download_url": f"/api/files/by-path?path={path}"
    }


@router.get("/{filename}")
async def get_shared_file(filename: str):
    """
    Serve a shared file by filename, searching in shared-files directories.

    Searches for the file in:
    1. Root workspace shared-files directory
    2. All project subdirectory shared-files directories

    Security: Only serves files from shared-files directories,
    preventing path traversal attacks.
    """
    # Sanitize filename - remove any path components
    safe_filename = Path(filename).name

    # Ensure no path traversal attempts
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename"
        )

    workspace = settings.effective_workspace_dir

    # Search locations: root workspace first, then subdirectories
    search_paths = []

    # 1. Root workspace shared-files
    root_shared = workspace / "shared-files" / safe_filename
    search_paths.append(root_shared)

    # 2. Search all subdirectories for shared-files folders
    if workspace.exists():
        for subdir in workspace.iterdir():
            if subdir.is_dir() and not subdir.name.startswith('.'):
                candidate = subdir / "shared-files" / safe_filename
                search_paths.append(candidate)

    # Find the first existing file
    file_path = None
    for path in search_paths:
        if path.exists() and path.is_file():
            file_path = path
            break

    if not file_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found"
        )

    suffix = file_path.suffix.lower()
    media_type = MEDIA_TYPES.get(suffix, 'application/octet-stream')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=safe_filename
    )


@router.get("/")
async def list_shared_files():
    """
    List shared files from the default workspace location.
    For debugging/admin purposes.
    """
    shared_files_dir = settings.effective_workspace_dir / "shared-files"

    if not shared_files_dir.exists():
        return {"files": [], "directory": str(shared_files_dir)}

    files = []
    for f in shared_files_dir.iterdir():
        if f.is_file():
            stat = f.stat()
            files.append({
                "filename": f.name,
                "url": f"/api/files/{f.name}",
                "path": str(f),
                "size_bytes": stat.st_size,
                "size_formatted": format_file_size(stat.st_size),
                "icon": get_file_icon(f.name),
                "extension": f.suffix.lower().lstrip('.'),
                "created_at": stat.st_ctime
            })

    # Sort by creation time, newest first
    files.sort(key=lambda x: x["created_at"], reverse=True)

    return {"files": files, "directory": str(shared_files_dir)}
