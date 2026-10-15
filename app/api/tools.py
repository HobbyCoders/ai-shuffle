"""Tool inventory API: GET /api/v1/tools/surface."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import get_api_user_from_request, require_admin, require_auth
from app.core.tool_catalog import build_tool_surface

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tools", tags=["Tools"])


@router.get("/surface")
async def get_tool_surface(
    request: Request,
    refresh: bool = False,
    token: str = Depends(require_auth),
):
    """Every tool a session can be given, grouped and discovered live.

    ``refresh=true`` busts the per-server MCP ``tools/list`` cache (10 minutes
    otherwise). A server that fails to answer still appears — as a group with
    an empty tool list plus an entry in ``errors`` — so the editor can show it
    as broken rather than silently losing it.

    Admin-only groups (``app_config``) are returned to every caller with
    ``admin_only: true``; an API-key session simply never gets those tools, and
    the editor renders them disabled.
    """
    surface = await build_tool_surface(refresh=refresh)
    surface["is_admin"] = get_api_user_from_request(request) is None
    return surface


class PeerMessageRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(min_length=1, max_length=200)


@router.post("/sessions/{sender_id}/peer-message")
async def send_peer_message(
    sender_id: str,
    body: PeerMessageRequest,
    request: Request,
    token: str = Depends(require_admin),
):
    """Local owner's deterministic MessageSession dispatch/receipt bridge.

    Repeating the same request reads its durable queued/begun/consumed/stopped
    receipt. This is NOT a generic tool executor: the normal orchestration
    owner, project, top-level, stop, chain-budget and dedupe checks still apply.
    API-key, preview and remote ingress cannot impersonate a sender here.
    """
    if (request.scope.get("shuffle.ingress") != "local"
            or get_api_user_from_request(request) is not None
            or str(token).startswith("preview:")
            or getattr(request.state, "preview_grant", None) is not None):
        raise HTTPException(403, "Local owner access required")
    from app.api.sessions_helpers import require_transcript_session
    from app.core.session_orchestration import make_handler

    await require_transcript_session(request, sender_id)
    outcome = await make_handler(lambda: sender_id)("message", body.model_dump(), None)
    if outcome.is_error:
        raise HTTPException(409, outcome.text)
    return outcome.ui
