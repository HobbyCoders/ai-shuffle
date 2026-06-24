"""Claude-in-Chrome management + DevTools (CDP) proxy.

Lets admins activate, inspect, and do the one-time browser sign-in/pairing for
the "Claude in Chrome" integration straight from the AI Shuffle UI — replacing
the old ``CHROME_DEBUG=1`` + expose-:9222 + hand-built DevTools URL dance.

How it works
------------
Headless Chrome runs inside the container (``docker/start-chrome.sh``) with a
**localhost-only** DevTools/CDP port (``CHROME_CDP=1`` → 127.0.0.1:9221). That
port is never published to the network; it is reachable only through the
authenticated proxy in this router. The frontend "Set up browser" panel opens a
WebSocket here, speaks CDP (``Page.startScreencast`` + ``Input.*``) through the
proxy, and renders a live, interactive view of the headless browser so the user
can sign the extension into their Claude account and approve pairing — all
behind the normal app login.

Persistence is handled at the storage layer (the ``browser-profile`` volume +
the ``~/.claude.json`` symlink into the ``claude-auth`` volume), so once paired
the browser auto-reconnects on later restarts without prompting. This router
only exists to make that one-time setup, and ongoing status, manageable without
a desktop Chrome UI.

Endpoints (all admin-only):
  GET  /api/v1/chrome-mcp/status    structured chrome-doctor as JSON
  POST /api/v1/chrome-mcp/toggle    enable/disable the claude-in-chrome builtin
  GET  /api/v1/chrome-mcp/targets   list CDP page targets (for the panel)
  WS   /api/v1/chrome-mcp/cdp       proxy a CDP target websocket (screencast)
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional

import httpx
import websockets
from fastapi import APIRouter, Depends, Query, WebSocket
from fastapi.websockets import WebSocketDisconnect
from pydantic import BaseModel

from app.api.auth import require_admin
from app.api.common import api_error_handler
from app.api.ws_auth import authenticate_websocket
from app.core.builtin_mcp_servers import BROWSER_SERVER_NAME, CHROME_EXTENSION_ID
from app.core.mcp_service import get_mcp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chrome-mcp", tags=["Chrome MCP"])

# ---------------------------------------------------------------------------
# Paths / config (mirror docker/start-chrome.sh + chrome-doctor.sh)
# ---------------------------------------------------------------------------
_HOME = Path.home()
CDP_HOST = "127.0.0.1"
CDP_PORT = int(os.environ.get("CHROME_CDP_PORT", "9221"))
CDP_HTTP = f"http://{CDP_HOST}:{CDP_PORT}"

PROFILE_DIR = _HOME / "chrome-profile"
NM_MANIFEST = (
    _HOME
    / ".config/google-chrome/NativeMessagingHosts"
    / "com.anthropic.claude_code_browser_extension.json"
)
NATIVE_HOST_BIN = _HOME / ".claude/chrome/chrome-native-host"
CLAUDE_JSON = _HOME / ".claude.json"
BRIDGE_SOCK_GLOB = "/tmp/claude-mcp-browser-bridge-*"

# CDP target ids are hex; browser-level targets are uuids. Allow both shapes.
_TARGET_ID_RE = re.compile(r"^[0-9A-Za-z-]{1,128}$")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ChromeMcpStatus(BaseModel):
    enabled: bool  # claude-in-chrome builtin is enabled (not in disabledMcpServers)
    chrome_running: bool
    cdp_available: bool  # localhost DevTools port is reachable
    cdp_enabled: bool  # CHROME_CDP env is on (in-app setup possible)
    extension_installed: bool
    native_manifest_present: bool
    native_host_present: bool
    bridge_active: bool  # extension is talking to the native host
    paired: bool
    paired_device_name: Optional[str] = None
    ready: bool  # everything needed for the agent to drive the browser
    next_step: str  # human guidance for the UI
    cdp_port: int = CDP_PORT
    # chrome-extension:// URL of the extension's settings/sign-in page, if the
    # extension is installed and declares one — the setup panel links to it.
    extension_options_url: Optional[str] = None


class ToggleRequest(BaseModel):
    enabled: bool


class CdpTarget(BaseModel):
    id: str
    type: str
    title: str = ""
    url: str = ""


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def _chrome_running() -> bool:
    """True if the supervised headless Chrome is running (scan /proc cmdlines)."""
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read()
            except OSError:
                continue
            low = cmd.lower()
            if b"ozone-platform=headless" in low and b"chrome" in low:
                return True
    except OSError:
        pass
    return False


def _extension_installed() -> bool:
    pattern = str(PROFILE_DIR / "Default/Extensions" / CHROME_EXTENSION_ID / "*/manifest.json")
    return bool(glob.glob(pattern))


def _bridge_active() -> bool:
    return bool(glob.glob(BRIDGE_SOCK_GLOB))


def _extension_options_url() -> Optional[str]:
    """chrome-extension:// URL of the extension's settings page, if declared.

    The Claude extension ships an ``options.html`` where the user signs in /
    manages pairing. It is toolbar-anchored and otherwise unreachable in a
    page screencast, so the setup panel navigates to it directly.
    """
    manifests = sorted(
        glob.glob(str(PROFILE_DIR / "Default/Extensions" / CHROME_EXTENSION_ID / "*/manifest.json"))
    )
    if not manifests:
        return None
    try:
        manifest = json.loads(Path(manifests[-1]).read_text())
    except (OSError, ValueError):
        return None
    page = (
        manifest.get("options_page")
        or (manifest.get("options_ui") or {}).get("page")
        or (manifest.get("action") or {}).get("default_popup")
    )
    if not page:
        return None
    return f"chrome-extension://{CHROME_EXTENSION_ID}/{page}"


def _paired_device() -> Optional[str]:
    """Return the paired device name (or id) the CLI persisted, else None.

    The bundled CLI stores ``chromeExtension.pairedDeviceId`` /
    ``pairedDeviceName`` in ~/.claude.json once a browser is paired; it
    auto-reconnects to that device on later sessions without re-prompting.
    """
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (OSError, ValueError):
        return None
    ext = data.get("chromeExtension") or {}
    device_id = ext.get("pairedDeviceId")
    if not device_id:
        return None
    return ext.get("pairedDeviceName") or device_id


async def _cdp_get(path: str) -> Optional[object]:
    """GET a Chrome DevTools HTTP endpoint (localhost only). None on failure."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{CDP_HTTP}{path}")
            resp.raise_for_status()
            return resp.json()
    except Exception:
        return None


def _cdp_enabled() -> bool:
    # Mirrors start-chrome.sh: the localhost CDP port is bound unless explicitly
    # turned off. CHROME_DEBUG also implies it.
    return os.environ.get("CHROME_CDP", "1") == "1" or os.environ.get("CHROME_DEBUG", "0") == "1"


async def _build_status() -> ChromeMcpStatus:
    disabled = set(get_mcp_service().get_disabled_servers())
    enabled = BROWSER_SERVER_NAME not in disabled

    chrome_running = _chrome_running()
    cdp_version = await _cdp_get("/json/version") if chrome_running else None
    cdp_available = cdp_version is not None

    extension_installed = _extension_installed()
    native_manifest_present = NM_MANIFEST.is_file()
    native_host_present = NATIVE_HOST_BIN.is_file() and os.access(NATIVE_HOST_BIN, os.X_OK)
    bridge_active = _bridge_active()
    paired_device = _paired_device()
    paired = paired_device is not None

    ready = enabled and chrome_running and extension_installed and paired

    if not enabled:
        next_step = "Claude in Chrome is disabled. Enable it to use browser tools."
    elif not chrome_running:
        next_step = "Chrome is not running. Set START_CHROME=1 and restart the container."
    elif not extension_installed:
        next_step = (
            "Waiting for the Claude in Chrome extension to install (needs network "
            "to clients2.google.com). Give it ~1 minute after Chrome starts."
        )
    elif not paired:
        if not _cdp_enabled():
            next_step = (
                "Not paired. In-app setup needs the localhost DevTools port: set "
                "CHROME_CDP=1 and restart the container, then run Set up browser."
            )
        elif not cdp_available:
            next_step = (
                "Not paired. Chrome needs a restart to open its localhost DevTools "
                "port — restart the container (or Chrome), then run Set up browser."
            )
        else:
            next_step = "Not paired yet. Click “Set up browser” to sign in and pair."
    elif not bridge_active:
        next_step = (
            "Paired. The extension will connect to the native host on the next "
            "browser session — no action needed."
        )
    else:
        next_step = "Ready. The agent can drive the browser."

    return ChromeMcpStatus(
        enabled=enabled,
        chrome_running=chrome_running,
        cdp_available=cdp_available,
        cdp_enabled=_cdp_enabled(),
        extension_installed=extension_installed,
        native_manifest_present=native_manifest_present,
        native_host_present=native_host_present,
        bridge_active=bridge_active,
        paired=paired,
        paired_device_name=paired_device,
        ready=ready,
        next_step=next_step,
        extension_options_url=_extension_options_url() if extension_installed else None,
    )


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------
@router.get("/status", response_model=ChromeMcpStatus)
async def get_status(token: str = Depends(require_admin)) -> ChromeMcpStatus:
    """Structured 'chrome-doctor' for the Claude in Chrome integration."""
    async with api_error_handler("get Chrome MCP status"):
        return await _build_status()


@router.post("/toggle", response_model=ChromeMcpStatus)
async def toggle(request: ToggleRequest, token: str = Depends(require_admin)) -> ChromeMcpStatus:
    """Enable or disable the claude-in-chrome built-in (settings.json)."""
    async with api_error_handler("toggle Claude in Chrome"):
        get_mcp_service().set_server_enabled(BROWSER_SERVER_NAME, request.enabled)
        return await _build_status()


@router.get("/targets", response_model=List[CdpTarget])
async def list_targets(token: str = Depends(require_admin)) -> List[CdpTarget]:
    """List CDP page targets so the setup panel can attach a screencast."""
    async with api_error_handler("list Chrome targets"):
        data = await _cdp_get("/json")
        if not isinstance(data, list):
            return []
        targets = [
            CdpTarget(
                id=str(t.get("id", "")),
                type=str(t.get("type", "")),
                title=str(t.get("title", "")),
                url=str(t.get("url", "")),
            )
            for t in data
            if t.get("id")
        ]
        # Pages first — that's what the panel attaches to.
        targets.sort(key=lambda t: 0 if t.type == "page" else 1)
        return targets


# ---------------------------------------------------------------------------
# CDP WebSocket proxy (screencast + input)
# ---------------------------------------------------------------------------
async def _resolve_target_ws(target_id: Optional[str]) -> Optional[str]:
    """Resolve a CDP page target's websocket URL (localhost only).

    Reconstructs the URL from the target id rather than trusting an arbitrary
    field, and verifies it points at our localhost CDP port.
    """
    data = await _cdp_get("/json")
    if not isinstance(data, list) or not data:
        return None

    chosen = None
    if target_id:
        if not _TARGET_ID_RE.match(target_id):
            return None
        chosen = next((t for t in data if str(t.get("id")) == target_id), None)
    if chosen is None:
        pages = [t for t in data if t.get("type") == "page"]
        chosen = pages[0] if pages else data[0]

    ws_url = chosen.get("webSocketDebuggerUrl")
    if not isinstance(ws_url, str):
        return None
    # Only ever proxy our own localhost DevTools endpoint.
    if not ws_url.startswith(f"ws://{CDP_HOST}:{CDP_PORT}/"):
        return None
    return ws_url


@router.websocket("/cdp")
async def cdp_proxy(
    websocket: WebSocket,
    target: Optional[str] = Query(None),
    token: Optional[str] = Query(None),
) -> None:
    """Bidirectional passthrough between the browser client and a CDP target.

    Admin-only. The client speaks raw CDP (JSON text frames). Frames are relayed
    verbatim in both directions; the proxy adds no protocol logic of its own.
    """
    await websocket.accept()

    ok, api_user = await authenticate_websocket(websocket, token)
    if not ok or api_user is not None:  # admin sessions only (api_user is None)
        await websocket.close(code=4001)
        return

    if not _cdp_enabled():
        await websocket.close(code=1011, reason="CDP disabled (set CHROME_CDP=1)")
        return

    upstream_url = await _resolve_target_ws(target)
    if not upstream_url:
        await websocket.close(code=1011, reason="no CDP target available")
        return

    try:
        upstream = await websockets.connect(upstream_url, max_size=None, open_timeout=5)
    except Exception as exc:
        logger.warning("CDP proxy: failed to connect upstream: %s", exc)
        await websocket.close(code=1011, reason="CDP connect failed")
        return

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await websocket.receive_text()
                await upstream.send(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as exc:  # noqa: BLE001 — best-effort relay
            logger.debug("CDP proxy c2u ended: %s", exc)

    async def upstream_to_client() -> None:
        try:
            async for msg in upstream:
                if isinstance(msg, bytes):
                    await websocket.send_bytes(msg)
                else:
                    await websocket.send_text(msg)
        except Exception as exc:  # noqa: BLE001 — best-effort relay
            logger.debug("CDP proxy u2c ended: %s", exc)

    c2u = asyncio.create_task(client_to_upstream())
    u2c = asyncio.create_task(upstream_to_client())
    try:
        await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (c2u, u2c):
            task.cancel()
        await upstream.close()
        try:
            await websocket.close()
        except RuntimeError:
            pass
