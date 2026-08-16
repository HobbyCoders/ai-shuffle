"""Artifact download route (v2 core).

``GET /api/v1/artifacts/{hash}`` serves the content-addressed bytes stored by
``app.core.artifact_store``. When this install holds the bytes it serves them
directly; otherwise it 307-redirects to the holder install's ``base_url`` (the
``artifacts.runner_id`` holder attribution, v61) when that install is online,
and returns an honest 404 when the holder is unknown/offline or the hash is
missing. An invalid hash format returns the same 404 as a missing artifact so
validity is never leaked.

The redirect is *signed*: the session cookie is host-only, so a browser sends no
credentials to the holder's origin. The redirecting install appends a
short-lived ``exp``/``sig`` pair minted from the shared cluster secret, which the
holder accepts in place of per-user auth (see ``_require_auth_or_signed``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse

from app.api.auth import require_auth
from app.core.artifact_store import artifact_path
from app.core.cluster import mint_artifact_token, verify_artifact_token
from app.core.config import settings
from app.db.artifacts import get_artifact
from app.db.installs import get_install

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/artifacts", tags=["Artifacts"])

_HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")


def _require_auth_or_signed(
    hash: str,
    request: Request,
    exp: Optional[str] = None,
    sig: Optional[str] = None,
) -> str:
    """Accept a valid cluster-signed redirect, else fall back to normal auth.

    A browser following the holder redirect carries no cookie for that origin
    (host-only session cookie), so the signed ``exp``/``sig`` pair is its only
    credential. Anything else goes through ``require_auth``, which 401s itself.
    """
    if exp and sig and verify_artifact_token(hash, exp, sig):
        return "cluster-signed"
    return require_auth(request)


@router.get("/{hash}")
async def download_artifact(hash: str, token: str = Depends(_require_auth_or_signed)):
    """Serve an artifact's bytes by content hash, or redirect to its holder."""
    if not _HASH_RE.match(hash):
        # Invalid format and missing are indistinguishable by design.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    row = await asyncio.to_thread(get_artifact, hash)
    path = artifact_path(hash)

    if path is not None:
        # We hold the bytes locally — serve them directly.
        media_type = (row.get("mime") if row else None) or "application/octet-stream"
        filename = (row.get("filename") if row else None) or hash
        return FileResponse(path, media_type=media_type, filename=filename)

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # Bytes are not on this install's disk. Redirect to the holder if it is a
    # different, online install with a reachable base_url; otherwise 404.
    runner_id = row.get("runner_id")
    if runner_id and runner_id != settings.effective_instance_id:
        install = await asyncio.to_thread(get_install, runner_id)
        if install and install.get("online") and install.get("base_url"):
            base_url = str(install["base_url"]).rstrip("/")
            exp, sig = mint_artifact_token(hash)
            target = f"{base_url}/api/v1/artifacts/{hash}?exp={exp}&sig={sig}"
            return RedirectResponse(
                url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT
            )

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
