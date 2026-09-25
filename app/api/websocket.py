"""
WebSocket API for real-time chat and synchronization.

Primary endpoint: /api/v1/ws/chat (session id supplied per-message)
- Handles all chat interactions via WebSocket
- Streams Claude responses directly to client
- Simple, reliable, no race conditions

Additional endpoints:
- /api/v1/ws/cli/{session_id} - Interactive CLI bridge for commands like /rewind
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, WebSocket, Query

from app.api.chat_ws_handler import ChatWebSocketHandler

# Re-exported for chat_ws_handler and tests.
from app.api.ws_auth import authenticate_websocket  # noqa: F401
from app.api.ws_utils import ping_loop  # noqa: F401
from app.api.ws_cli import (  # noqa: F401
    _active_cli_bridges,
    router as _cli_router,
)
from app.api.ws_sync import router as _sync_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["WebSocket"])

# Include sub-routers so their endpoints are registered under the same prefix.
# The sub-routers have no prefix of their own; they inherit the main router's
# ``/api/v1`` prefix automatically.
router.include_router(_cli_router)
router.include_router(_sync_router)


# =============================================================================
# PRIMARY CHAT WEBSOCKET - Simple, reliable streaming
# =============================================================================

@router.websocket("/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    # SECURITY: Token is passed via query param (unavoidable for WebSocket auth).
    # Never log the raw token value — only log `token is not None` for diagnostics.
    token: Optional[str] = Query(None, description="Authentication token (optional if cookie auth)"),
    device_id: Optional[str] = Query(None, description="Unique device identifier for multi-device sync")
):
    """
    Primary WebSocket endpoint for chat with multi-device sync support.

    Control-plane flow (V2: render deltas ride the session event feed / SSE,
    not this socket):
    1. Client connects (optionally provides device_id for multi-device sync)
    2. Client sends: {"type": "query", "prompt": "...", "session_id": "...", "profile": "..."}
    3. Server runs the turn; the recorder appends render events to the event log
    4. Server sends: {"type": "done", ...} when complete

    Message types FROM server (control plane):
    - history: Full message history for session (on connect or session switch)
    - start: Query started
    - stream_start / done / stopped / error: turn lifecycle
    - permission_request / user_question / queue_updated / etc.
    - ping: Keep-alive

    Message types TO server:
    - query: Start a new query
    - queue_message: Queue a message to send while Claude is working (streaming input)
    - stop: Interrupt current query
    - load_session: Load/switch to a session
    - pong: Response to ping

    Multi-device sync features:
    - Automatic device registration with SyncEngine when session is used
    - Non-streaming broadcasts (message_added, session_updated, queue_updated)
      still fan out to other devices watching the same session
    - Devices are automatically unregistered on disconnect or session switch
    """
    # Must accept connection first before we can send close with reason
    await websocket.accept()

    # Now authenticate
    is_authenticated, api_user = await authenticate_websocket(websocket, token)
    if not is_authenticated:
        logger.warning(f"Chat WebSocket auth failed - token provided: {token is not None}, cookie: {websocket.cookies.get('session') is not None}")
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Extract api_user_id if authenticated as an API user
    api_user_id: Optional[str] = api_user["id"] if api_user else None
    # user_id for user-level broadcasts (admin users use a constant since there's one admin)
    principal = getattr(websocket.state, "mobile_tool_principal", None)
    user_id: Optional[str] = ("paired:" + principal["principal_id"]
                              if isinstance(principal, dict) and principal["kind"] == "paired"
                              else api_user_id or "admin")
    logger.debug(f"Chat WebSocket connected and authenticated (api_user_id={api_user_id})")

    # Generate device_id if not provided
    if not device_id:
        device_id = str(uuid.uuid4())
        logger.debug(f"Generated device_id: {device_id}")

    # Everything after the accept/auth/device_id prologue lives in handler.run().
    handler = ChatWebSocketHandler(
        websocket=websocket,
        device_id=device_id,
        user_id=user_id,
        api_user_id=api_user_id,
        trusted_principal=getattr(websocket.state, "mobile_tool_principal", None),
    )
    await handler.run()
