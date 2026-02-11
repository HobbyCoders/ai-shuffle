"""
Browser API routes — REST endpoints for browser session state and screenshots.

These endpoints allow the frontend to:
- Get the current browser state (URL, title, active status)
- Get the latest screenshot as JPEG
- List active browser sessions
- Navigate, click, type, and interact with the browser (user-driven actions)
"""

import base64
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.auth import require_auth
from app.mcp.browser_session import browser_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/browser", tags=["Browser"])


@router.get("/{session_id}/state")
async def get_browser_state(
    session_id: str,
    user: dict = Depends(require_auth),
):
    """Get the current state of a browser session."""
    state = browser_session_manager.get_session_state(session_id)
    if not state:
        return {
            "is_active": False,
            "url": "",
            "title": "",
        }

    # Get title asynchronously
    session = await browser_session_manager.get_session(session_id)
    title = ""
    if session:
        title = await session.get_title()

    return {
        "is_active": True,
        "url": state["url"],
        "title": title,
        "created_at": state["created_at"],
        "last_activity": state["last_activity"],
    }


@router.get("/{session_id}/screenshot")
async def get_browser_screenshot(
    session_id: str,
    full_page: bool = False,
    user: dict = Depends(require_auth),
):
    """Get the latest screenshot of the browser session as JPEG."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active browser session")

    import base64
    screenshot_b64 = await session.take_screenshot(full_page=full_page)
    if not screenshot_b64:
        raise HTTPException(status_code=500, detail="Screenshot failed")

    screenshot_bytes = base64.b64decode(screenshot_b64)
    return Response(
        content=screenshot_bytes,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/sessions")
async def list_browser_sessions(
    user: dict = Depends(require_auth),
):
    """List all active browser sessions."""
    sessions = []
    for session_id in list(browser_session_manager._sessions.keys()):
        state = browser_session_manager.get_session_state(session_id)
        if state:
            sessions.append({
                "session_id": session_id,
                **state,
            })
    return {"sessions": sessions}


@router.delete("/{session_id}")
async def close_browser_session(
    session_id: str,
    user: dict = Depends(require_auth),
):
    """Close a browser session."""
    if not browser_session_manager.has_session(session_id):
        raise HTTPException(status_code=404, detail="No active browser session")

    await browser_session_manager.close_session(session_id)
    return {"success": True, "message": f"Browser session {session_id} closed"}


# =========================================================================
# User-driven browser actions
# =========================================================================

class NavigateRequest(BaseModel):
    url: str

class ClickRequest(BaseModel):
    x: int
    y: int

class TypeRequest(BaseModel):
    text: str
    selector: Optional[str] = None


async def _action_response(session_id: str):
    """Build a standard response after a browser action with fresh screenshot."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session lost")

    screenshot_b64 = await session.take_screenshot()
    title = await session.get_title()

    return {
        "success": True,
        "url": session.current_url,
        "title": title,
        "screenshot": screenshot_b64,
    }


@router.post("/{session_id}/navigate")
async def browser_navigate(
    session_id: str,
    req: NavigateRequest,
    user: dict = Depends(require_auth),
):
    """Navigate to a URL."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active browser session")

    try:
        await session.navigate(req.url)
    except Exception as e:
        logger.warning(f"Navigation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    return await _action_response(session_id)


@router.post("/{session_id}/back")
async def browser_back(
    session_id: str,
    user: dict = Depends(require_auth),
):
    """Navigate back in history."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active browser session")

    try:
        await session.go_back()
    except Exception as e:
        logger.warning(f"Back navigation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    return await _action_response(session_id)


@router.post("/{session_id}/forward")
async def browser_forward(
    session_id: str,
    user: dict = Depends(require_auth),
):
    """Navigate forward in history."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active browser session")

    try:
        await session.go_forward()
    except Exception as e:
        logger.warning(f"Forward navigation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    return await _action_response(session_id)


@router.post("/{session_id}/refresh")
async def browser_refresh(
    session_id: str,
    user: dict = Depends(require_auth),
):
    """Reload the current page."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active browser session")

    try:
        await session.reload()
    except Exception as e:
        logger.warning(f"Page refresh failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    return await _action_response(session_id)


@router.post("/{session_id}/click")
async def browser_click(
    session_id: str,
    req: ClickRequest,
    user: dict = Depends(require_auth),
):
    """Click at the given coordinates."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active browser session")

    try:
        await session.click_at(req.x, req.y)
    except Exception as e:
        logger.warning(f"Click failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    return await _action_response(session_id)


@router.post("/{session_id}/type")
async def browser_type(
    session_id: str,
    req: TypeRequest,
    user: dict = Depends(require_auth),
):
    """Type text into the focused element or a specific selector."""
    session = await browser_session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="No active browser session")

    try:
        await session.type_text(req.text, req.selector)
    except Exception as e:
        logger.warning(f"Type failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    return await _action_response(session_id)
