"""Computer-use control endpoints.

Backs the UI's "Stop controlling my computer" panic button and the capability
toggle. The emergency stop is a process-global switch (see
``app.mcp.computer_use.abort``) that every computer-use action checks before
running, so engaging it halts all synthetic input immediately.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.auth import require_auth
from app.mcp.computer_use import abort, activity
from app.mcp.computer_use.scaling import computer_use_available

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/computer-use", tags=["Computer Use"])


class StopStateResponse(BaseModel):
    """Current emergency-stop state."""
    stopped: bool


class StatusResponse(BaseModel):
    """Host capability + current stop state for the capability toggle, plus a
    live 'is the agent controlling the screen right now, and which one' signal
    for the in-app notification banner."""
    available: bool
    reason: str  # "ok" when available; otherwise why not (docker, no-display, ...)
    stopped: bool
    active: bool = False               # an action ran within the recent window
    display: Optional[int] = None      # which monitor it's acting on (when active)
    display_label: Optional[str] = None  # human label, e.g. "Display 1 · 3440×1440 (primary)"


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
    """Report capability, stop state, and live control activity.

    ``available`` drives the capability toggle (greyed out + reason on
    unsupported hosts). ``active``/``display_label`` drive the "AI Shuffle is
    controlling <screen>" banner while the agent is acting.
    """
    available, reason = computer_use_available()
    snap = activity.snapshot()
    label = None
    if snap["active"] and snap["display"] is not None:
        label = _display_label(snap["display"])
    return StatusResponse(
        available=available,
        reason=reason,
        stopped=abort.is_stopped(),
        active=bool(snap["active"]),
        display=snap["display"],
        display_label=label,
    )


@router.post("/stop", response_model=StopStateResponse)
async def stop(token: str = Depends(require_auth)) -> StopStateResponse:
    """Engage the emergency stop — all computer-use actions refuse until resumed."""
    abort.request_stop()
    logger.warning("computer-use emergency stop engaged via API")
    return StopStateResponse(stopped=True)


@router.post("/resume", response_model=StopStateResponse)
async def resume(token: str = Depends(require_auth)) -> StopStateResponse:
    """Clear the emergency stop, re-enabling computer-use actions."""
    abort.resume()
    return StopStateResponse(stopped=False)
