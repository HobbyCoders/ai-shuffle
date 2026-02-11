"""
Browser API routes — REST endpoints for browser session state and interaction.

These endpoints serve two callers:
1. Frontend (user auth via Bearer token) — URL bar, click, navigate, etc.
2. MCP browser subprocess (internal token via X-Internal-Token header) — proxied AI tool calls

Both callers share the same Playwright browser instance per session.
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.auth import require_auth
from app.core.config import INTERNAL_BROWSER_TOKEN
from app.mcp.browser_session import browser_session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/browser", tags=["Browser"])


# =========================================================================
# Auth — accepts user Bearer token OR internal MCP token
# =========================================================================

def require_browser_auth(request: Request):
    """Accept either normal user auth or internal MCP subprocess token."""
    internal_token = request.headers.get("X-Internal-Token")
    if internal_token and internal_token == INTERNAL_BROWSER_TOKEN:
        return "internal"
    return require_auth(request)


# =========================================================================
# Helper — execute a browser tool and return flat JSON
# =========================================================================

async def _execute_tool(session_id: str, tool_name: str, args: dict) -> dict:
    """Execute a browser tool via browser_tools and return flat JSON with screenshot."""
    from app.mcp.browser_tools import execute_browser_tool

    result = await execute_browser_tool(tool_name, args, session_id=session_id)

    text_data = {}
    screenshot = None
    for block in result.get("content", []):
        if block["type"] == "text":
            try:
                text_data = json.loads(block["text"])
            except (json.JSONDecodeError, TypeError):
                text_data = {"success": False, "error": block.get("text", "")}
        elif block["type"] == "image":
            screenshot = block.get("data")

    if screenshot:
        text_data["screenshot"] = screenshot
    return text_data


async def _get_or_create_session(session_id: str):
    """Get existing session or create a new one."""
    return await browser_session_manager.get_or_create_session(session_id)


# =========================================================================
# Session management
# =========================================================================

@router.post("/{session_id}/create")
async def create_browser_session(
    session_id: str,
    _auth=Depends(require_browser_auth),
):
    """Create a browser session without navigating. Returns empty state."""
    session = await _get_or_create_session(session_id)
    title = await session.get_title()
    return {
        "success": True,
        "url": session.current_url,
        "title": title,
        "is_active": True,
    }


@router.get("/{session_id}/state")
async def get_browser_state(
    session_id: str,
    _auth=Depends(require_browser_auth),
):
    """Get the current state of a browser session."""
    state = browser_session_manager.get_session_state(session_id)
    if not state:
        return {"is_active": False, "url": "", "title": ""}

    session = await browser_session_manager.get_session(session_id)
    title = await session.get_title() if session else ""

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
    _auth=Depends(require_browser_auth),
):
    """Get the latest screenshot as JPEG binary."""
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
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/{session_id}/screenshot/json")
async def get_browser_screenshot_json(
    session_id: str,
    full_page: bool = False,
    _auth=Depends(require_browser_auth),
):
    """Get screenshot as JSON with base64 data (for MCP proxy)."""
    return await _execute_tool(session_id, "browser_screenshot", {"full_page": full_page})


@router.get("/sessions")
async def list_browser_sessions(
    _auth=Depends(require_browser_auth),
):
    """List all active browser sessions."""
    sessions = []
    for sid in list(browser_session_manager._sessions.keys()):
        state = browser_session_manager.get_session_state(sid)
        if state:
            sessions.append({"session_id": sid, **state})
    return {"sessions": sessions}


@router.delete("/{session_id}")
async def close_browser_session(
    session_id: str,
    _auth=Depends(require_browser_auth),
):
    """Close a browser session."""
    if not browser_session_manager.has_session(session_id):
        raise HTTPException(status_code=404, detail="No active browser session")

    await browser_session_manager.close_session(session_id)
    return {"success": True, "message": f"Browser session {session_id} closed"}


# =========================================================================
# Browser tool endpoints — used by both frontend and MCP proxy
# =========================================================================

class NavigateRequest(BaseModel):
    url: str

class ClickRequest(BaseModel):
    selector: Optional[str] = None
    text: Optional[str] = None
    element_index: Optional[int] = None
    x: Optional[int] = None
    y: Optional[int] = None

class TypeRequest(BaseModel):
    text: str
    selector: Optional[str] = None
    press_enter: bool = False

class ScrollRequest(BaseModel):
    direction: str = "down"
    amount: int = 3

class ContentRequest(BaseModel):
    selector: Optional[str] = None

class ElementsRequest(BaseModel):
    selector: Optional[str] = None

class EvaluateRequest(BaseModel):
    script: str

class FillRequest(BaseModel):
    label: str
    value: str

class SelectRequest(BaseModel):
    selector: str
    value: str

class WaitRequest(BaseModel):
    selector: Optional[str] = None
    state: str = "visible"
    timeout: int = 10000

class TabNewRequest(BaseModel):
    url: Optional[str] = None

class TabSwitchRequest(BaseModel):
    index: int


@router.post("/{session_id}/navigate")
async def browser_navigate(
    session_id: str,
    req: NavigateRequest,
    _auth=Depends(require_browser_auth),
):
    """Navigate to a URL."""
    return await _execute_tool(session_id, "browser_navigate", {"url": req.url})


@router.post("/{session_id}/back")
async def browser_back(
    session_id: str,
    _auth=Depends(require_browser_auth),
):
    """Navigate back in history."""
    return await _execute_tool(session_id, "browser_back", {})


@router.post("/{session_id}/forward")
async def browser_forward(
    session_id: str,
    _auth=Depends(require_browser_auth),
):
    """Navigate forward in history."""
    return await _execute_tool(session_id, "browser_forward", {})


@router.post("/{session_id}/refresh")
async def browser_refresh(
    session_id: str,
    _auth=Depends(require_browser_auth),
):
    """Reload the current page."""
    # browser_tools doesn't have a dedicated refresh, use navigate to current URL
    session = await browser_session_manager.get_session(session_id)
    if not session:
        # Create session first
        session = await _get_or_create_session(session_id)
    try:
        await session.reload()
    except Exception as e:
        return {"success": False, "error": str(e)}

    screenshot_b64 = await session.take_screenshot()
    title = await session.get_title()
    return {
        "success": True,
        "url": session.current_url,
        "title": title,
        "screenshot": screenshot_b64,
    }


@router.post("/{session_id}/click")
async def browser_click(
    session_id: str,
    req: ClickRequest,
    _auth=Depends(require_browser_auth),
):
    """Click an element by selector, text, element_index, or coordinates."""
    args = {}
    if req.selector is not None:
        args["selector"] = req.selector
    if req.text is not None:
        args["text"] = req.text
    if req.element_index is not None:
        args["element_index"] = req.element_index
    if req.x is not None:
        args["x"] = req.x
    if req.y is not None:
        args["y"] = req.y
    return await _execute_tool(session_id, "browser_click", args)


@router.post("/{session_id}/type")
async def browser_type(
    session_id: str,
    req: TypeRequest,
    _auth=Depends(require_browser_auth),
):
    """Type text into the focused element or a specific selector."""
    args = {"text": req.text}
    if req.selector:
        args["selector"] = req.selector
    if req.press_enter:
        args["press_enter"] = req.press_enter
    return await _execute_tool(session_id, "browser_type", args)


@router.post("/{session_id}/scroll")
async def browser_scroll(
    session_id: str,
    req: ScrollRequest,
    _auth=Depends(require_browser_auth),
):
    """Scroll the page up or down."""
    return await _execute_tool(session_id, "browser_scroll", {
        "direction": req.direction,
        "amount": req.amount,
    })


@router.post("/{session_id}/get-content")
async def browser_get_content(
    session_id: str,
    req: ContentRequest,
    _auth=Depends(require_browser_auth),
):
    """Get readable text content from the page."""
    args = {}
    if req.selector:
        args["selector"] = req.selector
    return await _execute_tool(session_id, "browser_get_content", args)


@router.post("/{session_id}/get-elements")
async def browser_get_elements(
    session_id: str,
    req: ElementsRequest,
    _auth=Depends(require_browser_auth),
):
    """Get interactive elements on the page."""
    args = {}
    if req.selector:
        args["selector"] = req.selector
    return await _execute_tool(session_id, "browser_get_elements", args)


@router.post("/{session_id}/evaluate")
async def browser_evaluate(
    session_id: str,
    req: EvaluateRequest,
    _auth=Depends(require_browser_auth),
):
    """Execute JavaScript in the browser."""
    return await _execute_tool(session_id, "browser_evaluate", {"script": req.script})


@router.post("/{session_id}/fill")
async def browser_fill(
    session_id: str,
    req: FillRequest,
    _auth=Depends(require_browser_auth),
):
    """Fill a form field by label."""
    return await _execute_tool(session_id, "browser_fill", {
        "label": req.label,
        "value": req.value,
    })


@router.post("/{session_id}/select")
async def browser_select(
    session_id: str,
    req: SelectRequest,
    _auth=Depends(require_browser_auth),
):
    """Select a dropdown option."""
    return await _execute_tool(session_id, "browser_select", {
        "selector": req.selector,
        "value": req.value,
    })


@router.post("/{session_id}/wait")
async def browser_wait(
    session_id: str,
    req: WaitRequest,
    _auth=Depends(require_browser_auth),
):
    """Wait for a condition."""
    args = {"state": req.state, "timeout": req.timeout}
    if req.selector:
        args["selector"] = req.selector
    return await _execute_tool(session_id, "browser_wait", args)


@router.post("/{session_id}/tab/new")
async def browser_tab_new(
    session_id: str,
    req: TabNewRequest,
    _auth=Depends(require_browser_auth),
):
    """Open a new browser tab."""
    args = {}
    if req.url:
        args["url"] = req.url
    return await _execute_tool(session_id, "browser_tab_new", args)


@router.get("/{session_id}/tab/list")
async def browser_tab_list(
    session_id: str,
    _auth=Depends(require_browser_auth),
):
    """List all open browser tabs."""
    return await _execute_tool(session_id, "browser_tab_list", {})


@router.post("/{session_id}/tab/switch")
async def browser_tab_switch(
    session_id: str,
    req: TabSwitchRequest,
    _auth=Depends(require_browser_auth),
):
    """Switch to a different browser tab."""
    return await _execute_tool(session_id, "browser_tab_switch", {"index": req.index})
