"""
API endpoint for serving generated audio.

This endpoint serves audio created by the AI audio-generation tools
(generateAudio, audioToAudio) and the text-to-speech feature, allowing
them to be played inline in the chat UI.

Audio is stored in 'generated-audio' subdirectories within project
working directories. The API validates that requested files are within
allowed workspace paths.
"""

from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import FileResponse

from app.core.config import settings
from app.api.common import is_path_safe
from app.api.auth import require_auth

router = APIRouter(prefix="/api/generated-audio", tags=["generated-audio"])

# Allowed audio extensions
ALLOWED_EXTENSIONS = {'.mp3', '.wav', '.ogg', '.oga', '.opus', '.flac', '.m4a', '.aac', '.pcm'}

# Media type mapping
MEDIA_TYPES = {
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg',
    '.opus': 'audio/opus',
    '.flac': 'audio/flac',
    '.m4a': 'audio/mp4',
    '.aac': 'audio/aac',
    '.pcm': 'audio/L16',
}


@router.get("/by-path")
async def get_audio_by_path(path: str = Query(..., description="Full path to the audio file"), token: str = Depends(require_auth)):
    """
    Serve a generated audio file by its full path.

    Security: Only serves audio files from within the workspace directory.
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

    # Additional check: must be in a 'generated-audio' directory
    if 'generated-audio' not in file_path.parts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not a generated audio file"
        )

    # Check file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Audio not found"
        )

    media_type = MEDIA_TYPES.get(suffix, 'audio/mpeg')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name
    )


@router.get("/{filename}")
async def get_generated_audio(filename: str, token: str = Depends(require_auth)):
    """
    Serve a generated audio file by filename from the default workspace location.

    This is a fallback for simple cases where audio is in the main
    workspace's generated-audio folder.

    Security: Only serves files from the generated-audio directory,
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

    # Only allow audio extensions
    suffix = Path(safe_filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type"
        )

    # Look in default workspace generated-audio directory
    generated_audio_dir = settings.effective_workspace_dir / "generated-audio"
    file_path = generated_audio_dir / safe_filename

    # Check if file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Audio not found"
        )

    media_type = MEDIA_TYPES.get(suffix, 'audio/mpeg')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=safe_filename
    )


@router.get("/")
async def list_generated_audio(token: str = Depends(require_auth)):
    """
    List generated audio files from the default workspace location.
    For debugging/admin purposes.
    """
    generated_audio_dir = settings.effective_workspace_dir / "generated-audio"

    if not generated_audio_dir.exists():
        return {"audio": []}

    audio_files = []
    for f in generated_audio_dir.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
            audio_files.append({
                "filename": f.name,
                "url": f"/api/generated-audio/{f.name}",
                "size_bytes": f.stat().st_size,
                "created_at": f.stat().st_ctime
            })

    # Sort by creation time, newest first
    audio_files.sort(key=lambda x: x["created_at"], reverse=True)

    return {"audio": audio_files}
