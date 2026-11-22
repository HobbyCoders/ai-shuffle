"""Computer-use control endpoints.

Backs the global computer-use toggle (Settings > Security > Computer use) and the live
status the UI's indicators consume. The toggle is a system setting consulted
by ``query_options`` when wiring the computer-use tools; the per-session halt
switch is driven by chat Stop, physical Escape, and the host stop endpoint.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_admin, require_auth
from app.db.async_database import run_sync
from app.db.settings import get_system_setting, set_system_setting
from app.mcp.computer_use import access, activity, abort, safety, overlay
from app.mcp.computer_use.scaling import computer_use_available, last_availability

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/computer-use", tags=["Computer Use"])

# System-settings key the global toggle persists under. Read on the hot
# query-options path via get_system_setting_cached, so writes here take
# effect immediately in this process (and within its short TTL elsewhere).
ENABLED_SETTING_KEY = "computer_use_enabled"
# Who else uses this worker's desktop. "shared_desktop" (the default): a person
# works at this screen too, so each session's first access request asks them.
# "dedicated_desktop": only agents use it (a VM, a spare machine), so access
# follows the session's permission mode like any other tool.
INPUT_MODE_SETTING_KEY = "computer_use_input_mode"
InputMode = Literal["shared_desktop", "dedicated_desktop"]


class EnabledStateResponse(BaseModel):
    """Current global toggle state."""
    enabled: bool


class SetEnabledRequest(BaseModel):
    """Payload for the global toggle."""
    enabled: bool


class StatusResponse(BaseModel):
    """Host capability + toggle state for the global toggle, plus a live
    'is the agent controlling the screen right now, and which one' signal for
    the in-app notification banner."""
    available: bool
    reason: str  # "ok" when available; otherwise why not (docker, no-display, wayland, ...)
    detail: Optional[str] = None  # when unavailable: what is wrong and how to fix it
    missing: list[str] = []  # packages to install, when that is the cause
    enabled: bool
    active: bool = False               # desktop control is owned by a chat
    display: Optional[int] = None      # which monitor it's acting on (when active)
    display_label: Optional[str] = None  # human label, e.g. "Display 1 · 3440×1440 (primary)"
    action: Optional[str] = None
    busy: bool = False
    cursor: Optional[list[int]] = None
    geometry: Optional[dict] = None
    apps: list[str] = []  # apps the controlling chat was granted (when active)
    seconds_since: Optional[float] = None
    stop_reason: Optional[str] = None
    escape_available: bool = False
    overlay_available: bool = False
    input_mode: InputMode = "shared_desktop"


class InputModeState(BaseModel):
    """Whether a person shares this worker's desktop with the agents."""
    input_mode: InputMode


def get_computer_use_enabled() -> bool:
    """Read the persisted global toggle (the hot-path seam query_options uses)."""
    return get_system_setting(ENABLED_SETTING_KEY) == "true"


def get_computer_use_input_mode() -> InputMode:
    """Read the persisted desktop mode; anything unexpected reads as shared."""
    value = get_system_setting(INPUT_MODE_SETTING_KEY)
    return "dedicated_desktop" if value == "dedicated_desktop" else "shared_desktop"


def is_dedicated_desktop() -> bool:
    """True when only agents use this desktop, so no first-use prompt is needed.

    Fails closed: a read error means shared, which keeps the prompt.
    """
    try:
        return get_computer_use_input_mode() == "dedicated_desktop"
    except Exception:
        logger.warning("computer-use: desktop mode read failed; treating as shared", exc_info=True)
        return False


def _display_label(index: int) -> str:
    """Build a human label for the active display; best-effort."""
    try:
        from app.mcp.computer_use.scaling import enumerate_displays
        for d in enumerate_displays():
            if d.index == index:
                tag = " (primary)" if d.primary else ""
                return f"Display {d.index} · {d.width}×{d.height}{tag}"
    except Exception:
        pass
    return f"Display {index}"


@router.get("/status", response_model=StatusResponse)
async def get_status(token: str = Depends(require_auth)) -> StatusResponse:
    """Report capability, toggle state, and live control activity.

    ``available`` drives the toggle (greyed out + reason on unsupported hosts).
    ``active``/``display_label`` drive the "AI Shuffle is controlling <screen>"
    banner while the agent is acting.
    """
    available, reason = computer_use_available()
    found = None if available else last_availability(reason)
    snap = activity.snapshot()
    label = None
    apps = list(access.info(snap["session_id"])["apps"]) if snap["active"] else []
    if snap["active"] and snap["display"] is not None:
        geo = snap.get("geometry")
        label = (f"Display {snap['display']} · {geo['width']}×{geo['height']}"
                 if geo else _display_label(snap["display"]))
    return StatusResponse(
        available=available,
        reason=reason,
        detail=(found.detail or None) if found else None,
        missing=list(found.missing) if found else [],
        enabled=await run_sync(get_computer_use_enabled),
        active=bool(snap["active"]),
        display=snap["display"],
        display_label=label,
        action=snap["action"], busy=snap["busy"], cursor=snap["cursor"], geometry=snap["geometry"],
        apps=apps, seconds_since=snap["seconds_since"], stop_reason=snap["stop_reason"],
        escape_available=safety.escape_available(), overlay_available=overlay.status()["available"],
        input_mode=await run_sync(get_computer_use_input_mode),
    )


@router.post("/stop")
async def stop_computer_use(token: str = Depends(require_auth)) -> dict:
    """Any signed-in user can release the host. Never grants or resumes access."""
    return {"stopped": abort.stop_active("user")}


@router.get("/enabled", response_model=EnabledStateResponse)
async def get_enabled(token: str = Depends(require_auth)) -> EnabledStateResponse:
    """Report the global toggle."""
    return EnabledStateResponse(enabled=await run_sync(get_computer_use_enabled))


@router.put("/enabled", response_model=EnabledStateResponse)
async def set_enabled(
    payload: SetEnabledRequest, token: str = Depends(require_admin)
) -> EnabledStateResponse:
    """Set the global computer-use toggle (admin — it grants host input access)."""
    await run_sync(
        set_system_setting, ENABLED_SETTING_KEY, "true" if payload.enabled else "false"
    )
    if not payload.enabled:
        abort.stop_active("disabled")
    logger.info("computer-use global toggle set to %s via API", payload.enabled)
    return EnabledStateResponse(enabled=payload.enabled)


@router.get("/input-mode", response_model=InputModeState)
async def get_input_mode(token: str = Depends(require_auth)) -> InputModeState:
    """Report whether a person shares this worker's desktop with the agents."""
    return InputModeState(input_mode=await run_sync(get_computer_use_input_mode))


@router.put("/input-mode", response_model=InputModeState)
async def set_input_mode(
    payload: InputModeState, token: str = Depends(require_admin)
) -> InputModeState:
    """Set the desktop mode (admin: dedicated skips the first-use prompt)."""
    await run_sync(set_system_setting, INPUT_MODE_SETTING_KEY, payload.input_mode)
    logger.info("computer-use desktop mode set to %s via API", payload.input_mode)
    return payload
