"""Browser-automation management API.

Admin surface for the built-in ``browser`` MCP server — a login-less, spawned
STDIO server that drives the *supervised Chromium* over a loopback CDP port
(``docker/start-chrome.sh`` binds 127.0.0.1:${CHROME_CDP_PORT:-9221}, never
published to the network). See the ADR in ``app/core/builtin_mcp_servers.py``.

There is no account, browser extension, native-messaging host, or pairing flow —
all of that machinery (and its CDP screencast setup panel) belonged to the old
"Claude in Chrome" integration and was removed. This router is therefore tiny:

  GET  /api/v1/browser/status   {enabled, browser_running, tool_server_ok}
  POST /api/v1/browser/toggle   enable/disable the browser built-in

``browser_running`` is a live CDP probe of ``GET /json/version`` on the loopback
DevTools port; ``tool_server_ok`` is ``enabled and browser_running`` (the tools
can only actually drive the browser when both hold).

FUTURE (browser-panel live view — out of scope now): the loopback CDP port is the
seam. A live view would re-add a WebSocket proxy in this router that speaks CDP
(``Page.startScreencast`` + ``Input.*``) through to that port behind admin auth;
nothing else here needs to change to support it.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_admin
from app.api.common import api_error_handler
from app.core.builtin_mcp_servers import BROWSER_SERVER_NAME
from app.core.mcp_service import get_mcp_service

logger = logging.getLogger(__name__)

# The supervised Chromium's loopback DevTools/CDP endpoint (localhost only; never
# published to the network — reachable only from inside the container/host).
CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.environ.get("CHROME_CDP_PORT", "9221"))
CDP_HTTP = f"http://{CDP_HOST}:{CDP_PORT}"

router = APIRouter(prefix="/api/v1/browser", tags=["Browser"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class BrowserStatus(BaseModel):
    enabled: bool  # the `browser` builtin is enabled (not in disabledMcpServers)
    browser_running: bool  # supervised Chromium answers on the loopback CDP port
    tool_server_ok: bool  # enabled AND browser_running — tools can drive it


class ToggleRequest(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
async def _cdp_get(path: str) -> Optional[object]:
    """GET a Chrome DevTools HTTP endpoint (localhost only). None on failure."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{CDP_HTTP}{path}")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


async def _build_status() -> BrowserStatus:
    enabled = BROWSER_SERVER_NAME not in set(get_mcp_service().get_disabled_servers())
    # Live proof the supervised Chromium is up: it answers /json/version on the
    # loopback CDP port. (3s timeout via _cdp_get; None on any failure.)
    browser_running = await _cdp_get("/json/version") is not None
    return BrowserStatus(
        enabled=enabled,
        browser_running=browser_running,
        tool_server_ok=enabled and browser_running,
    )


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@router.get("/status", response_model=BrowserStatus)
async def get_status(token: str = Depends(require_admin)) -> BrowserStatus:
    """Report whether browser automation is enabled and the browser reachable."""
    async with api_error_handler("get browser status"):
        return await _build_status()


@router.post("/toggle", response_model=BrowserStatus)
async def toggle(request: ToggleRequest, token: str = Depends(require_admin)) -> BrowserStatus:
    """Enable or disable the `browser` built-in (settings.json), return new status."""
    async with api_error_handler("toggle browser automation"):
        get_mcp_service().set_server_enabled(BROWSER_SERVER_NAME, request.enabled)
        return await _build_status()
