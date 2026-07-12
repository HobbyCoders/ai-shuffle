"""Artifact download route (v2 core).

``GET /api/v1/artifacts/{hash}`` serves the content-addressed bytes stored by
``app.core.artifact_store``. An invalid hash format returns the same 404 as a
missing artifact so validity is never leaked.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from app.api.auth import require_auth
from app.core.artifact_store import artifact_path
from app.db.artifacts import get_artifact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/artifacts", tags=["Artifacts"])


@router.get("/{hash}")
async def download_artifact(hash: str, token: str = Depends(require_auth)):
    """Serve an artifact's bytes by content hash."""
    path = artifact_path(hash)
    if path is None:
        # Invalid format and missing-on-disk are indistinguishable by design.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    row = await asyncio.to_thread(get_artifact, hash)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    return FileResponse(
        path,
        media_type=row.get("mime") or "application/octet-stream",
        filename=row.get("filename") or hash,
    )
