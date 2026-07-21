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
from app.core import native_chrome
from app.core.builtin_mcp_servers import BROWSER_SERVER_NAME, _browser_mcp_entry
from app.core.mcp_service import get_mcp_service

logger = logging.getLogger(__name__)

# Once-flag so an unreachable browser logs a single warning, not per-poll spam.
_status_probe_warned = False

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
    detail: Optional[str] = None  # human-readable reason when unreachable


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


def _unreachable_detail() -> str:
    """Best explanation for why no browser answers on the CDP port.

    Preference order: the native supervisor's own last detail (crash-loop, no
    executable, external owner…) → the browser-mcp build being absent → a
    generic "nothing answering CDP" message.
    """
    supervisor_detail = native_chrome.get_detail()
    if supervisor_detail:
        return supervisor_detail

    entry = _browser_mcp_entry()
    if not os.path.exists(entry):
        return f"browser-mcp not built: {entry} missing"

    return f"no browser answering CDP on {CDP_HOST}:{CDP_PORT}"


async def _build_status() -> BrowserStatus:
    global _status_probe_warned
    enabled = BROWSER_SERVER_NAME not in set(get_mcp_service().get_disabled_servers())
    # Live proof the supervised Chromium is up: it answers /json/version on the
    # loopback CDP port. (3s timeout via _cdp_get; None on any failure.)
    browser_running = await _cdp_get("/json/version") is not None

    detail: Optional[str] = None
    if not browser_running:
        detail = _unreachable_detail()
        if not _status_probe_warned:
            _status_probe_warned = True
            logger.warning("Browser status probe failed: %s", detail)

    return BrowserStatus(
        enabled=enabled,
        browser_running=browser_running,
        tool_server_ok=enabled and browser_running,
        detail=detail,
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

        # On native installs, (re)launch or tear down the local headless browser
        # to match the new state. Guarded so supervisor issues never 500 the
        # toggle; a no-op inside the container (docker owns Chromium there).
        if not native_chrome._in_container():
            try:
                if request.enabled:
                    await native_chrome.ensure_started()
                else:
                    await native_chrome.stop()
            except Exception:
                logger.warning(
                    "Native Chrome supervisor toggle failed", exc_info=True
                )

        return await _build_status()
