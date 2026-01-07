"""
API endpoint for serving generated videos.

This endpoint serves videos created by the AI video generation tool,
allowing them to be displayed in the chat UI.

Videos are stored in 'generated-videos' subdirectories within project
working directories. The API validates that requested files are within
allowed workspace paths.
"""

from pathlib import Path
from fastapi import APIRouter, HTTPException, status, Query
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter(prefix="/api/generated-videos", tags=["generated-videos"])

# Allowed video extensions
ALLOWED_EXTENSIONS = {'.mp4', '.webm', '.mov', '.avi'}

# Media type mapping
MEDIA_TYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo'
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


@router.get("/by-path")
async def get_video_by_path(path: str = Query(..., description="Full path to the video file")):
    """
    Serve a generated video by its full path.

    Security: Only serves video files from within the workspace directory.
    """
    file_path = Path(path)

    # Validate extension
    suffix = file_path.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type"
        )

    # Security check: must be within workspace
    workspace = settings.effective_workspace_dir
    if not is_path_safe(file_path, workspace):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: file is outside workspace"
        )

    # Additional check: must be in a 'generated-videos' directory
    if 'generated-videos' not in file_path.parts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not a generated video"
        )

    # Check file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )

    media_type = MEDIA_TYPES.get(suffix, 'video/mp4')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name
    )


@router.get("/{filename}")
async def get_generated_video(filename: str):
    """
    Serve a generated video by filename from the default workspace location.

    This is a fallback for simple cases where videos are in the main
    workspace's generated-videos folder.

    Security: Only serves files from the generated-videos directory,
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

    # Only allow video extensions
    suffix = Path(safe_filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type"
        )

    # Look in default workspace generated-videos directory
    generated_videos_dir = settings.effective_workspace_dir / "generated-videos"
    file_path = generated_videos_dir / safe_filename

    # Check if file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )

    media_type = MEDIA_TYPES.get(suffix, 'video/mp4')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=safe_filename
    )


@router.get("/")
async def list_generated_videos():
    """
    List generated videos from the default workspace location.
    For debugging/admin purposes.
    """
    generated_videos_dir = settings.effective_workspace_dir / "generated-videos"

    if not generated_videos_dir.exists():
        return {"videos": [], "directory": str(generated_videos_dir)}

    videos = []
    for f in generated_videos_dir.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
            videos.append({
                "filename": f.name,
                "url": f"/api/generated-videos/{f.name}",
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "created_at": f.stat().st_ctime
            })

    # Sort by creation time, newest first
    videos.sort(key=lambda x: x["created_at"], reverse=True)

    return {"videos": videos, "directory": str(generated_videos_dir)}
