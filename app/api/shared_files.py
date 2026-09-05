"""
API endpoint for serving downloadable files.

This endpoint serves files from the workspace that the agent makes
available for download, displaying them in the chat UI with download cards.

Security: Files must be within the workspace directory. The API validates
that requested file paths don't escape the workspace boundary. All endpoints
require authentication via session token.
"""

from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import FileResponse

from app.api.auth import require_auth
from app.db.async_database import run_sync
from app.api.common import require_file_access

from app.core.config import settings
from app.api.common import is_path_safe, is_path_served, format_file_size  # noqa: F401 (is_path_safe re-exported for tests)

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
    # Images
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
    '.svg': 'image/svg+xml',
    '.avif': 'image/avif',
    '.ico': 'image/x-icon',
    '.tif': 'image/tiff',
    '.tiff': 'image/tiff',
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


def get_file_icon(filename: str) -> str:
    """Get emoji icon for a file based on extension."""
    ext = Path(filename).suffix.lower().lstrip('.')
    return FILE_ICONS.get(ext, '📁')


@router.get("/by-path")
async def get_file_by_path(path: str = Query(..., description="Full path to the file"), _token: str = Depends(require_auth), request: Request = None):
    """
    Serve a file by its full path.

    Security: Only serves files from within the workspace directory.
    Any file in the workspace can be made downloadable by the agent.
    """
    file_path = Path(path)

    # Security check: must be within workspace or a registered project/worktree
    workspace = settings.effective_workspace_dir
    await run_sync(require_file_access, file_path, workspace, request)

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
async def get_file_info(path: str = Query(..., description="Full path to the file"), _token: str = Depends(require_auth), request: Request = None):
    """
    Get metadata about a file without downloading it.

    Returns file name, size, type, and icon information.
    """
    file_path = Path(path)

    # Security check: must be within workspace or a registered project/worktree
    workspace = settings.effective_workspace_dir
    await run_sync(require_file_access, file_path, workspace, request)

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
async def get_shared_file(filename: str, _token: str = Depends(require_auth), request: Request = None):
    """
    Serve a shared file by filename from the root workspace shared-files directory.

    This is a simple fallback for files in the main workspace.
    For project-specific files, use /api/files/by-path?path=... instead.

    Security: Only serves files from the shared-files directory,
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

    # Only look in root workspace shared-files directory
    shared_files_dir = settings.effective_workspace_dir / "shared-files"
    file_path = shared_files_dir / safe_filename
    await run_sync(require_file_access, file_path, settings.effective_workspace_dir, request)

    # Check if file exists
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
        filename=safe_filename
    )


@router.get("/")
async def list_shared_files(_token: str = Depends(require_auth), request: Request = None):
    """
    List shared files from the default workspace location.
    For debugging/admin purposes.
    """
    shared_files_dir = settings.effective_workspace_dir / "shared-files"

    await run_sync(require_file_access, shared_files_dir, settings.effective_workspace_dir, request)

    if not shared_files_dir.exists():
        return {"files": [], "directory": str(shared_files_dir)}

    files = []
    for f in shared_files_dir.iterdir():
        if f.is_file() and is_path_safe(f, shared_files_dir):
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
