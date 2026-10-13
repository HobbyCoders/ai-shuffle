"""Tool inventory API: GET /api/v1/tools/surface."""

import logging

from fastapi import APIRouter, Depends, Request

from app.api.auth import get_api_user_from_request, require_auth
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
