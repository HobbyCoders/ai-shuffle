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
from app.core.builtin_mcp_servers import (
    BROWSER_CLI_FLAG,
    BROWSER_SERVER_NAME,
    CHROME_EXTENSION_ID,
)
from app.core.mcp_service import get_mcp_service

logger = logging.getLogger(__name__)

# Hard ceiling on the pairing query (one-shot SDK turn). The relay round-trip
# (list browsers + select) is normally a few seconds; cap it so a stuck session
# can't hang the request.
PAIR_TIMEOUT_SECONDS = 90

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
    paired: bool  # a specific device is pinned in ~/.claude.json (pairedDeviceId)
    paired_device_id: Optional[str] = None
    paired_device_name: Optional[str] = None
    # Functional even when not pinned: the extension is signed in and reachable
    # (a native-messaging bridge socket exists). With exactly one browser on the
    # account the CLI auto-selects it every session, so this is "working".
    connected: bool = False
    ready: bool  # everything needed for the agent to drive the browser
    next_step: str  # human guidance for the UI
    cdp_port: int = CDP_PORT
    # chrome-extension:// URL of the extension's settings/sign-in page, if the
    # extension is installed and declares one — the setup panel links to it.
    extension_options_url: Optional[str] = None


class ToggleRequest(BaseModel):
    enabled: bool


class PairResult(BaseModel):
    """Outcome of a pair-this-browser attempt + the refreshed status."""

    ok: bool
    message: str
    paired_device_id: Optional[str] = None
    paired_device_name: Optional[str] = None
    status: ChromeMcpStatus


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


def _paired_device() -> tuple[Optional[str], Optional[str]]:
    """Return ``(pairedDeviceId, pairedDeviceName)`` the CLI persisted, else (None, None).

    The bundled CLI stores ``chromeExtension.pairedDeviceId`` /
    ``pairedDeviceName`` in ~/.claude.json once a browser is paired; it
    auto-reconnects to that device on later sessions without re-prompting.
    """
    try:
        data = json.loads(CLAUDE_JSON.read_text())
    except (OSError, ValueError):
        return None, None
    ext = data.get("chromeExtension") or {}
    device_id = ext.get("pairedDeviceId")
    if not device_id:
        return None, None
    return device_id, ext.get("pairedDeviceName")


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
    paired_device_id, paired_device_name = _paired_device()
    paired = paired_device_id is not None

    # "Connected" = the extension is signed in and talking to the native host
    # (a bridge socket exists). This is the real functional signal: with a single
    # browser on the account the CLI auto-selects it every session even without a
    # persisted pairing, so a connected-but-unpinned setup IS working — just not
    # deterministic if a second browser later joins the account.
    connected = enabled and chrome_running and extension_installed and bridge_active

    # Ready when the agent can drive a browser: either it's pinned to this
    # container's device (paired) OR the extension is connected (auto-select
    # works). Pinning is the robust state, but we no longer call a working,
    # connected setup "not ready".
    ready = enabled and chrome_running and extension_installed and (paired or bridge_active)

    if not enabled:
        next_step = "Claude in Chrome is disabled. Enable it to use browser tools."
    elif not chrome_running:
        next_step = "Chrome is not running. Set START_CHROME=1 and restart the container."
    elif not extension_installed:
        next_step = (
            "Waiting for the Claude in Chrome extension to install (needs network "
            "to clients2.google.com). Give it ~1 minute after Chrome starts."
        )
    elif not bridge_active and not paired:
        # Nothing connected and nothing pinned → genuine first-time setup.
        if not _cdp_enabled():
            next_step = (
                "Not set up. In-app setup needs the localhost DevTools port: set "
                "CHROME_CDP=1 and restart the container, then run Set up browser."
            )
        elif not cdp_available:
            next_step = (
                "Not set up. Chrome needs a restart to open its localhost DevTools "
                "port — restart the container (or Chrome), then run Set up browser."
            )
        else:
            next_step = "Not set up yet. Click “Set up browser” to sign the extension in."
    elif paired:
        # Pinned to a specific device — the robust, deterministic state.
        if bridge_active:
            next_step = "Ready. Pinned to this browser — the agent will use it."
        else:
            next_step = (
                "Pinned to this browser. The extension will reconnect to the native "
                "host on the next browser session — no action needed."
            )
    else:
        # Connected (bridge active) but not pinned. Functional via auto-select,
        # but a second browser on the account would make it ambiguous. Offer to
        # pin without alarming the user that it's broken.
        next_step = (
            "Connected and working. Click “Pair this browser” to pin this "
            "container's browser so it stays selected even if you sign another "
            "browser into the same Claude account."
        )

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
        paired_device_id=paired_device_id,
        paired_device_name=paired_device_name,
        connected=connected,
        ready=ready,
        next_step=next_step,
        extension_options_url=_extension_options_url() if extension_installed else None,
    )


# ---------------------------------------------------------------------------
# Pairing — pin this container's own browser as pairedDeviceId
# ---------------------------------------------------------------------------
# The instruction we give the one-shot agent turn. It must:
#   1. list the browsers on the account,
#   2. pick THIS container's own browser (the one with isLocal == true),
#   3. select it (which fires onExtensionPaired → persists pairedDeviceId).
# Kept terse and deterministic; we don't want the model to wander.
_PAIR_PROMPT = (
    "You are pinning this container's own browser for the Claude in Chrome "
    "integration. Do exactly this and nothing else:\n"
    "1. Call `list_connected_browsers`.\n"
    "2. From the result, choose the browser whose `isLocal` is true (that is "
    "this container's own headless browser). If none is local but there is "
    "exactly one browser, choose that one. If there are multiple non-local "
    "browsers and none is local, do NOT select any — reply with the word "
    "AMBIGUOUS and the list of device names.\n"
    "3. Call `select_browser` with that browser's `deviceId` to pin it.\n"
    "4. Reply with the word PAIRED and the pinned device name. Do not call any "
    "other tools."
)

# Allow only the browser-selection tools for the pairing turn — never the full
# browser-driving surface. (mcp__claude-in-chrome__<tool> form.)
_PAIR_ALLOWED_TOOLS = [
    f"mcp__{BROWSER_SERVER_NAME}__list_connected_browsers",
    f"mcp__{BROWSER_SERVER_NAME}__select_browser",
]


async def _run_pairing_query() -> str:
    """Run a one-shot, non-interactive SDK turn that pins the local browser.

    Reuses the SDK's bundled CLI (auto-resolved) with ``--chrome`` so the
    ``mcp__claude-in-chrome__*`` tools are exposed, restricted to the two
    selection tools, in ``bypassPermissions`` so it never blocks on a prompt.
    Authenticates exactly like every other Claude Code subprocess (connected
    account, or ANTHROPIC_API_KEY if configured). Returns the model's final
    text (best-effort), which we only use for logging — the source of truth for
    success is the persisted ``pairedDeviceId`` in ~/.claude.json.
    """
    # Imported lazily so importing this router never drags in the SDK/heavy deps.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    from app.core.environment import inject_claude_code_auth

    env: dict[str, str] = {"CLAUDECODE": ""}
    try:
        inject_claude_code_auth(env)
    except Exception:  # noqa: BLE001 — auth injection is best-effort
        logger.debug("Could not inject Claude Code auth for pairing", exc_info=True)

    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        allowed_tools=_PAIR_ALLOWED_TOOLS,
        # No setting_sources → don't load user/project settings for this internal
        # one-shot. Suppress plugins so per-query hooks don't slow/interfere.
        setting_sources=["local"],
        settings=json.dumps({"enabledPlugins": {}}),
        max_turns=4,  # list → select → confirm, with a little slack
        include_partial_messages=False,
        continue_conversation=False,
        fork_session=False,
        env=env,
        # Enable the Claude in Chrome built-in → bare ``--chrome`` flag.
        extra_args={BROWSER_CLI_FLAG: None},
    )

    chunks: List[str] = []
    async for message in query(prompt=_PAIR_PROMPT, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks).strip()


async def _pair_local_browser() -> PairResult:
    """Pin this container's own browser; return the outcome + refreshed status.

    Idempotent and defensive:
      * already pinned → no-op success,
      * no browser connected → clear "sign in first" error (we never run the
        relay turn, which would only hang),
      * otherwise run the one-shot selection turn and verify the persisted
        ``pairedDeviceId`` actually changed/appeared.
    """
    status = await _build_status()

    if not status.enabled:
        return PairResult(
            ok=False,
            message="Claude in Chrome is disabled. Enable it first, then pair.",
            status=status,
        )

    # Already pinned — no-op success.
    if status.paired:
        return PairResult(
            ok=True,
            message=f"Already paired to {status.paired_device_name or status.paired_device_id}.",
            paired_device_id=status.paired_device_id,
            paired_device_name=status.paired_device_name,
            status=status,
        )

    # Nothing connected → the relay turn would just hang. Tell the user to sign in.
    if not status.bridge_active:
        return PairResult(
            ok=False,
            message=(
                "No browser is connected yet. Open “Set up browser”, sign the "
                "Claude extension into your account, then try pairing again."
            ),
            status=status,
        )

    try:
        reply = await asyncio.wait_for(_run_pairing_query(), timeout=PAIR_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return PairResult(
            ok=False,
            message=(
                "Pairing timed out waiting for the browser. Make sure exactly one "
                "browser is signed into this Claude account, or use “Set up "
                "browser” to pair manually."
            ),
            status=await _build_status(),
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean message, log detail
        logger.warning("Pairing query failed: %s", exc, exc_info=True)
        return PairResult(
            ok=False,
            message=f"Pairing failed: {exc}",
            status=await _build_status(),
        )

    # Source of truth: did the CLI persist a pairedDeviceId?
    refreshed = await _build_status()
    if refreshed.paired:
        return PairResult(
            ok=True,
            message=f"Paired to {refreshed.paired_device_name or refreshed.paired_device_id}.",
            paired_device_id=refreshed.paired_device_id,
            paired_device_name=refreshed.paired_device_name,
            status=refreshed,
        )

    # No pairing was persisted. The model's reply often explains why (e.g.
    # AMBIGUOUS when multiple non-local browsers are on the account).
    detail = reply or "the browser did not report a device to pin"
    return PairResult(
        ok=False,
        message=(
            "Could not pin a browser automatically "
            f"({detail[:300]}). If multiple browsers are signed into this Claude "
            "account, sign the others out and retry, or use “Set up browser”."
        ),
        status=refreshed,
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


@router.post("/pair", response_model=PairResult)
async def pair(token: str = Depends(require_admin)) -> PairResult:
    """Pin this container's own browser as the persisted ``pairedDeviceId``.

    Runs a one-shot agent turn that lists the browsers on the account and
    ``select_browser``s the local one, so the CLI auto-reconnects to THIS
    container's browser on every later session — even when the user's personal
    Chrome is signed into the same Claude account. Idempotent (already-paired is
    a no-op success) and returns the refreshed status.
    """
    async with api_error_handler("pair Claude in Chrome browser"):
        return await _pair_local_browser()


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
