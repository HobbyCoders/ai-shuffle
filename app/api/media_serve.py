"""
Generic media-serving API factory.

Serves AI-generated media (images, videos, audio) created by the generation
tools so they can be displayed/played inline in the chat UI without base64
encoding in the model response (which would exceed context limits).

Media is stored in ``generated-<kind>`` subdirectories within project working
directories. The API validates that requested files are within allowed
workspace paths and inside a ``generated-<kind>`` directory.

Each media kind (images / videos / audio) gets its own router with an
identical route surface; the only differences are the URL prefix, the set of
allowed extensions, the MIME-type table, the default MIME-type fallback, the
directory name, the list-response key, and the human-readable noun used in
error messages. All of those are captured by ``MediaConfig`` so the routing
logic lives in exactly one place.

The three concrete modules (``generated_images``, ``generated_videos``,
``generated_audio``) are thin shims over this factory; they exist so that the
public URLs and the module symbols (``router``, ``settings``, ``is_path_safe``,
``ALLOWED_EXTENSIONS``, ``MEDIA_TYPES`` and the named handlers) that the
frontend, ``app.main`` and the test-suite rely on remain byte-identical.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import FileResponse

from app.core.config import settings
from app.api.common import is_path_served
from app.api.auth import require_auth

# Re-exported so importers can keep doing ``from app.api.media_serve import ...``.
__all__ = ["MediaConfig", "build_media_module", "settings", "is_path_safe", "is_path_served"]


@dataclass(frozen=True)
class MediaConfig:
    """Per-media-kind configuration for the generic serve factory."""

    # URL prefix, e.g. "/api/generated-images"
    prefix: str
    # OpenAPI tag, e.g. "generated-images"
    tag: str
    # Name of the on-disk subdirectory, e.g. "generated-images"
    directory: str
    # Allowed lowercase file extensions (with leading dot)
    allowed_extensions: Set[str]
    # Extension -> MIME type
    media_types: Dict[str, str]
    # MIME type used when an allowed extension is missing from media_types
    default_media_type: str
    # Key used in the list-endpoint JSON response, e.g. "images"
    list_key: str
    # Human noun for "<Noun> not found" errors, e.g. "Image"
    noun: str
    # Lowercase noun for "not a generated <noun>" errors, e.g. "image"
    noun_lower: str


def build_media_module(
    config: MediaConfig,
    workspace_dir_getter: Optional[Callable[[], Path]] = None,
):
    """
    Build a router plus the three named handlers for one media kind.

    ``workspace_dir_getter`` returns the effective workspace directory. The
    concrete shim modules pass a lambda that reads *their own* module-level
    ``settings`` object, so that test code which patches e.g.
    ``app.api.generated_images.settings`` still influences the served path.
    When omitted it falls back to this module's ``settings``.

    Returns a dict with keys ``router``, ``by_path``, ``by_filename`` and
    ``list_media`` so the concrete shim module can bind them to the exact
    symbol names the tests and callers expect.
    """
    if workspace_dir_getter is None:
        workspace_dir_getter = lambda: settings.effective_workspace_dir  # noqa: E731

    router = APIRouter(prefix=config.prefix, tags=[config.tag])

    ALLOWED_EXTENSIONS = config.allowed_extensions
    MEDIA_TYPES = config.media_types

    @router.get("/by-path")
    async def by_path(
        path: str = Query(..., description=f"Full path to the {config.noun_lower} file"),
        token: str = Depends(require_auth),
    ):
        """
        Serve a generated media file by its full path.

        Security: Only serves files from within the workspace directory and
        from inside a ``generated-<kind>`` directory.
        """
        file_path = Path(path)

        # Validate extension
        suffix = file_path.suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file type",
            )

        # Security check: must be within workspace or a registered project/worktree
        workspace = workspace_dir_getter()
        if not is_path_served(file_path, workspace):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: file is outside workspace",
            )

        # Additional check: must be in a 'generated-<kind>' directory
        if config.directory not in file_path.parts:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: not a generated {config.noun_lower}",
            )

        # Check file exists
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{config.noun} not found",
            )

        media_type = MEDIA_TYPES.get(suffix, config.default_media_type)

        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=file_path.name,
        )

    @router.get("/{filename}")
    async def by_filename(filename: str, token: str = Depends(require_auth)):
        """
        Serve a generated media file by filename from the default workspace.

        This is a fallback for simple cases where the file is in the main
        workspace's ``generated-<kind>`` folder.

        Security: Only serves files from the ``generated-<kind>`` directory,
        preventing path traversal attacks.
        """
        # Sanitize filename - remove any path components
        safe_filename = Path(filename).name

        # Ensure no path traversal attempts
        if ".." in filename or "/" in filename or "\\" in filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid filename",
            )

        # Only allow the media kind's extensions
        suffix = Path(safe_filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid file type",
            )

        # Look in default workspace generated-<kind> directory
        media_dir = workspace_dir_getter() / config.directory
        file_path = media_dir / safe_filename

        # Check if file exists
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{config.noun} not found",
            )

        media_type = MEDIA_TYPES.get(suffix, config.default_media_type)

        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=safe_filename,
        )

    @router.get("/")
    async def list_media(token: str = Depends(require_auth)):
        """
        List generated media files from the default workspace location.
        For debugging/admin purposes.
        """
        media_dir = workspace_dir_getter() / config.directory

        if not media_dir.exists():
            return {config.list_key: []}

        items = []
        for f in media_dir.iterdir():
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS:
                items.append({
                    "filename": f.name,
                    "url": f"{config.prefix}/{f.name}",
                    "size_bytes": f.stat().st_size,
                    "created_at": f.stat().st_ctime,
                })

        # Sort by creation time, newest first
        items.sort(key=lambda x: x["created_at"], reverse=True)

        return {config.list_key: items}

    return {
        "router": router,
        "by_path": by_path,
        "by_filename": by_filename,
        "list_media": list_media,
    }
