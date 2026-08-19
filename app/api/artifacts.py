"""Artifact download route (v2 core).

``GET /api/v1/artifacts/{hash}`` serves the content-addressed bytes stored by
``app.core.artifact_store``. When this install holds the bytes it serves them
directly. When the bytes live on another online install, the default is to
proxy them through this origin (the session cookie is host-only, so a 307
cannot carry it). ``CLUSTER_ARTIFACT_PROXY=0`` restores the signed 307 to the
holder's ``base_url``. An honest 404 is returned when the holder is
unknown/offline, the hash is missing, or the proxy fails. An invalid hash
format returns the same 404 as a missing artifact so validity is never leaked.

The holder fetch is *signed*: this install appends a short-lived ``exp``/``sig``
pair minted from the shared cluster secret, which the holder accepts in place
of per-user auth (see ``_require_auth_or_signed``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.api.auth import require_auth
from app.api.projects import MAX_UPLOAD_SIZE
from app.core.artifact_store import artifact_path
from app.core.cluster import mint_artifact_token, verify_artifact_token
from app.core.config import settings
from app.core.http_client import get_http_client
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


def _holder_artifact_url(base_url: str, hash: str) -> Optional[str]:
    """Build the holder GET URL, or None if ``base_url`` is unsafe.

    Validates scheme/userinfo/hostname/dot-segments, then ignores any holder
    path and forces ``{scheme}://{netloc}/api/v1/artifacts/{hash}``. Token
    query params are appended by the caller after minting.
    """
    if not _HASH_RE.match(hash):
        return None
    parts = urlsplit(str(base_url).strip())
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if not parts.hostname:
        return None
    for segment in parts.path.split("/"):
        if segment in (".", ".."):
            return None
    return f"{parts.scheme}://{parts.netloc}/api/v1/artifacts/{hash}"


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _proxy_holder_bytes(target: str, hash: str, runner_id: str) -> bytes:
    """Fetch holder bytes via stream(). Never logs ``target`` (it carries sig)."""
    try:
        async with get_http_client().stream(
            "GET", target, timeout=10.0, follow_redirects=False,
        ) as resp:
            if resp.status_code != 200:
                logger.warning(
                    "artifact proxy: runner_id=%s status=%s",
                    runner_id,
                    resp.status_code,
                )
                raise _not_found()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_UPLOAD_SIZE:
                    await resp.aclose()
                    logger.warning(
                        "artifact proxy: runner_id=%s overflow", runner_id
                    )
                    raise _not_found()
    except HTTPException:
        raise
    except Exception:
        logger.warning("artifact proxy: runner_id=%s fetch failed", runner_id)
        raise _not_found()

    if hashlib.sha256(buf).hexdigest() != hash:
        logger.warning("artifact proxy: runner_id=%s mismatch", runner_id)
        raise _not_found()
    return bytes(buf)


@router.get("/{hash}")
async def download_artifact(hash: str, token: str = Depends(_require_auth_or_signed)):
    """Serve an artifact's bytes by content hash, or proxy/redirect to its holder."""
    if not _HASH_RE.match(hash):
        # Invalid format and missing are indistinguishable by design.
        raise _not_found()

    row = await asyncio.to_thread(get_artifact, hash)
    path = artifact_path(hash)

    if path is not None:
        # We hold the bytes locally — serve them directly.
        media_type = (row.get("mime") if row else None) or "application/octet-stream"
        filename = (row.get("filename") if row else None) or hash
        return FileResponse(path, media_type=media_type, filename=filename)

    if row is None:
        raise _not_found()

    # Bytes are not on this install's disk. Fetch (or, with the flag off,
    # 307) from the holder if it is a different, online install with a
    # reachable base_url; otherwise 404.
    runner_id = row.get("runner_id")
    if runner_id and runner_id != settings.effective_instance_id:
        install = await asyncio.to_thread(get_install, runner_id)
        if install and install.get("online") and install.get("base_url"):
            if not settings.cluster_artifact_proxy:
                base_url = str(install["base_url"]).rstrip("/")
                exp, sig = mint_artifact_token(hash)
                target = f"{base_url}/api/v1/artifacts/{hash}?exp={exp}&sig={sig}"
                return RedirectResponse(
                    url=target, status_code=status.HTTP_307_TEMPORARY_REDIRECT
                )

            origin = _holder_artifact_url(str(install["base_url"]), hash)
            if origin is None:
                raise _not_found()
            exp, sig = mint_artifact_token(hash)
            target = f"{origin}?exp={exp}&sig={sig}"
            body = await _proxy_holder_bytes(target, hash, runner_id)
            media_type = row.get("mime") or "application/octet-stream"
            filename = row.get("filename") or hash
            return Response(
                bytes(body),
                media_type=media_type,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

    raise _not_found()
