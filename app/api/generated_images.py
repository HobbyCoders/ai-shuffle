"""
API endpoint for serving generated images.

This endpoint serves images created by the AI image generation tool,
allowing them to be displayed in the chat UI without base64 encoding
in the response (which would exceed context limits).

Images are stored in 'generated-images' subdirectories within project
working directories. The API validates that requested files are within
allowed workspace paths.
"""

from pathlib import Path
from fastapi import APIRouter, HTTPException, status, Query
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter(prefix="/api/generated-images", tags=["generated-images"])

# Allowed image extensions
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}

# Media type mapping
MEDIA_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp'
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
async def get_image_by_path(path: str = Query(..., description="Full path to the image file")):
    """
    Serve a generated image by its full path.

    Security: Only serves image files from within the workspace directory.
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

    # Additional check: must be in a 'generated-images' directory
    if 'generated-images' not in file_path.parts:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: not a generated image"
        )

    # Check file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found"
        )

    media_type = MEDIA_TYPES.get(suffix, 'application/octet-stream')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=file_path.name
    )


@router.get("/{filename}")
async def get_generated_image(filename: str):
    """
    Serve a generated image by filename from the default workspace location.

    This is a fallback for simple cases where images are in the main
    workspace's generated-images folder.

    Security: Only serves files from the generated-images directory,
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

    # Only allow image extensions
    suffix = Path(safe_filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type"
        )

    # Look in default workspace generated-images directory
    generated_images_dir = settings.effective_workspace_dir / "generated-images"
    file_path = generated_images_dir / safe_filename

    # Check if file exists
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found"
        )

    media_type = MEDIA_TYPES.get(suffix, 'application/octet-stream')

    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=safe_filename
    )


@router.get("/")
async def list_generated_images():
    """
    List generated images from the default workspace location.
    For debugging/admin purposes.
    """
    generated_images_dir = settings.effective_workspace_dir / "generated-images"

    if not generated_images_dir.exists():
        return {"images": [], "directory": str(generated_images_dir)}

    images = []
    for f in generated_images_dir.iterdir():
        if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
            images.append({
                "filename": f.name,
                "url": f"/api/generated-images/{f.name}",
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "created_at": f.stat().st_ctime
            })

    # Sort by creation time, newest first
    images.sort(key=lambda x: x["created_at"], reverse=True)

    return {"images": images, "directory": str(generated_images_dir)}
