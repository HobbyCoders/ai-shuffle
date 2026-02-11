"""
Browser API routes — REST endpoints for browser session state and screenshots.

These endpoints allow the frontend to:
- Get the current browser state (URL, title, active status)
- Get the latest screenshot as JPEG
- List active browser sessions
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

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
