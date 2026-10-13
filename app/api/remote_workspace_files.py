"""The Files page against another of the owner's workers.

``/api/v1/workspace/workers/{device_id}/...`` mirrors the local workspace file
routes and relays each call over the existing worker-to-worker link to the
target's ``/api/v1/network/peer/workspace/...`` mirror (see
app.api.workspace_files.peer_router). The target applies its own path
allow-list; its refusals pass through unchanged.

Only the owner reaches another worker's files. API keys, non-owner paired
clients and the agent's session-preview cookie are refused here, before any
request leaves this worker.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.auth import require_admin
from app.api.projects import MAX_UPLOAD_SIZE
from app.network import engine, membership
from app.network.browser_panel import forward_buffered
from app.network.files import forward_download

WORKER_UNAVAILABLE = "That worker is offline or unreachable."
PEER_PREFIX = "/api/v1/network/peer/workspace"
# Multipart framing around the largest file the peer itself accepts.
MAX_UPLOAD_BODY = MAX_UPLOAD_SIZE + 64 * 1024
LIST_BODY = 64 * 1024


def require_remote_files_owner(request: Request, device_id: str) -> str:
    """The owner, asking about an active member worker other than this one."""
    principal = require_admin(request)
    # A valid preview cookie on the request is refused whatever else admitted
    # it (require_admin's enforce_limits records it as ``preview_grant``).
    if str(principal).startswith("preview:") or str(
        getattr(request.state, "auth_token", "") or ""
    ).startswith("preview:") or getattr(request.state, "preview_grant", None) is not None:
        raise HTTPException(403, "Another worker's files need the owner's own sign-in")
    ctx = engine.context()
    if ctx is None:
        raise HTTPException(404, "This worker is not paired with any other worker")
    if device_id == ctx.device_id:
        raise HTTPException(400, "This is the current worker; use its own files")
    member = ctx.member(device_id)
    if (member is None or member.status != membership.STATUS_ACTIVE
            or member.kind != membership.KIND_WORKER):
        raise HTTPException(404, "Unknown worker")
    return device_id


router = APIRouter(
    prefix="/api/v1/workspace/workers/{device_id}",
    tags=["Workspace Files"],
    dependencies=[Depends(require_remote_files_owner)],
)


def _worker_error(exc: HTTPException) -> HTTPException:
    if exc.status_code == 503:
        return HTTPException(503, WORKER_UNAVAILABLE)
    return exc


async def _relay(request: Request, device_id: str, path: str, max_body: int,
                 timeout: float = 60) -> Response:
    try:
        return await forward_buffered(
            request, device_id, PEER_PREFIX + path,
            max_body=max_body, noun="file", timeout=timeout,
        )
    except HTTPException as exc:
        raise _worker_error(exc) from None


@router.get("/files")
async def list_files(request: Request, device_id: str) -> Response:
    return await _relay(request, device_id, "/files", LIST_BODY)


@router.post("/files/folder")
async def create_folder(request: Request, device_id: str) -> Response:
    return await _relay(request, device_id, "/files/folder", LIST_BODY)


@router.put("/files/rename")
async def rename(request: Request, device_id: str) -> Response:
    return await _relay(request, device_id, "/files/rename", LIST_BODY)


@router.delete("/files")
async def delete(request: Request, device_id: str) -> Response:
    return await _relay(request, device_id, "/files", LIST_BODY)


@router.post("/upload")
async def upload(request: Request, device_id: str) -> Response:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_UPLOAD_BODY:
        raise HTTPException(
            413, f"File too large. Maximum upload size is {MAX_UPLOAD_SIZE // (1024 * 1024)}MB"
        )
    return await _relay(request, device_id, "/upload", MAX_UPLOAD_BODY, timeout=300)


@router.get("/files/download")
async def download(request: Request, device_id: str,
                   path: str = Query(..., description="Path relative to that worker's workspace")):
    try:
        return await forward_download(
            request, device_id, PEER_PREFIX + "/files/download", {"path": path}
        )
    except HTTPException as exc:
        raise _worker_error(exc) from None
