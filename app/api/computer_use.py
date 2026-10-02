"""Computer-use control endpoints.

Backs the global computer-use toggle (Settings > Security > Computer use) and the live
status the UI's indicators consume. The toggle is a system setting consulted
by ``query_options`` when wiring the computer-use tools; the per-session halt
switch is driven by chat Stop, physical Escape, and the host stop endpoint.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_admin, require_auth
from app.db.async_database import run_sync
from app.db.settings import get_system_setting, set_system_setting
from app.mcp.computer_use import activity, abort, safety, overlay
from app.mcp.computer_use.scaling import computer_use_available

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/computer-use", tags=["Computer Use"])

# System-settings key the global toggle persists under. Read on the hot
# query-options path via get_system_setting_cached, so writes here take
# effect immediately in this process (and within its short TTL elsewhere).
ENABLED_SETTING_KEY = "computer_use_enabled"


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
    reason: str  # "ok" when available; otherwise why not (docker, no-display, ...)
    enabled: bool
    active: bool = False               # desktop control is owned by a chat
    display: Optional[int] = None      # which monitor it's acting on (when active)
    display_label: Optional[str] = None  # human label, e.g. "Display 1 · 3440×1440 (primary)"
    action: Optional[str] = None
    busy: bool = False
    cursor: Optional[list[int]] = None
    geometry: Optional[dict] = None
    seconds_since: Optional[float] = None
    stop_reason: Optional[str] = None
    escape_available: bool = False
    overlay_available: bool = False
    input_mode: str = "shared_desktop"


def get_computer_use_enabled() -> bool:
    """Read the persisted global toggle (the hot-path seam query_options uses)."""
    return get_system_setting(ENABLED_SETTING_KEY) == "true"


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
    snap = activity.snapshot()
    label = None
    if snap["active"] and snap["display"] is not None:
        geo = snap.get("geometry")
        label = (f"Display {snap['display']} · {geo['width']}×{geo['height']}"
                 if geo else _display_label(snap["display"]))
    return StatusResponse(
        available=available,
        reason=reason,
        enabled=await run_sync(get_computer_use_enabled),
        active=bool(snap["active"]),
        display=snap["display"],
        display_label=label,
        action=snap["action"], busy=snap["busy"], cursor=snap["cursor"], geometry=snap["geometry"],
        seconds_since=snap["seconds_since"], stop_reason=snap["stop_reason"],
        escape_available=safety.escape_available(), overlay_available=overlay.status()["available"],
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
