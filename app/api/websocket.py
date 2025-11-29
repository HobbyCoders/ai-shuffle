"""
WebSocket API for real-time chat and synchronization.

Primary endpoint: /ws/chat/{session_id}
- Handles all chat interactions via WebSocket
- Streams Claude responses directly to client
- Simple, reliable, no race conditions
"""

import logging
import json
import asyncio
import uuid
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, status
from fastapi.websockets import WebSocketState

from app.core.sync_engine import sync_engine, SyncEvent
from app.db import database
from app.core.auth import auth_service
from app.core.profiles import get_profile_or_builtin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["WebSocket"])

# Track active chat sessions for interruption
_active_chat_sessions: dict[str, asyncio.Task] = {}


async def authenticate_websocket(websocket: WebSocket, token: Optional[str]) -> bool:
    """Validate authentication token for WebSocket connection"""
    if not token:
        return False

    # Check session token
    session = database.get_auth_session(token)
    if session:
        return True

    # Check API key (hashed)
    import hashlib
    key_hash = hashlib.sha256(token.encode()).hexdigest()
    api_user = database.get_api_user_by_key_hash(key_hash)
    if api_user:
        return True

    return False


# =============================================================================
# PRIMARY CHAT WEBSOCKET - Simple, reliable streaming
# =============================================================================

@router.websocket("/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    token: str = Query(..., description="Authentication token")
):
    """
    Primary WebSocket endpoint for chat.

    This is the ONLY streaming mechanism needed. Simple flow:
    1. Client connects
    2. Client sends: {"type": "query", "prompt": "...", "session_id": "...", "profile": "..."}
    3. Server streams response chunks directly
    4. Server sends: {"type": "done", ...} when complete

    Message types FROM server:
    - history: Full message history for session (on connect or session switch)
    - start: Query started, streaming will begin
    - chunk: Text content chunk
    - tool_use: Tool being used
    - tool_result: Tool result
    - done: Query complete with metadata
    - error: Error occurred
    - ping: Keep-alive

    Message types TO server:
    - query: Start a new query
    - stop: Interrupt current query
    - load_session: Load/switch to a session
    - pong: Response to ping
    """
    # Authenticate
    if not await authenticate_websocket(websocket, token):
        await websocket.close(code=4001, reason="Authentication failed")
        return

    await websocket.accept()
    logger.info("Chat WebSocket connected")

    current_session_id: Optional[str] = None
    query_task: Optional[asyncio.Task] = None

    async def send_json(data: dict):
        """Safe JSON send with connection check"""
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json(data)
            except Exception as e:
                logger.warning(f"Failed to send WebSocket message: {e}")

    async def run_query(prompt: str, session_id: str, profile_id: str, project_id: Optional[str]):
        """Execute query and stream results directly to WebSocket"""
        nonlocal current_session_id

        from app.core.query_engine import stream_to_websocket

        try:
            await send_json({"type": "start", "session_id": session_id})

            async for event in stream_to_websocket(
                prompt=prompt,
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id
            ):
                await send_json(event)

        except asyncio.CancelledError:
            await send_json({"type": "stopped", "session_id": session_id})
            logger.info(f"Query cancelled for session {session_id}")

        except Exception as e:
            logger.error(f"Query error: {e}")
            await send_json({"type": "error", "message": str(e)})

        finally:
            # Clean up task reference
            if session_id in _active_chat_sessions:
                del _active_chat_sessions[session_id]

    try:
        # Start ping task
        ping_task = asyncio.create_task(ping_loop(websocket, "chat"))

        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=60.0
                    )

                    msg_type = data.get("type")

                    if msg_type == "query":
                        # Start a new query
                        prompt = data.get("prompt", "").strip()
                        session_id = data.get("session_id")
                        profile_id = data.get("profile", "claude-code")
                        project_id = data.get("project")

                        if not prompt:
                            await send_json({"type": "error", "message": "Empty prompt"})
                            continue

                        # Create or get session
                        if not session_id:
                            # Create new session
                            session_id = str(uuid.uuid4())
                            database.create_session(
                                session_id=session_id,
                                profile_id=profile_id,
                                project_id=project_id
                            )

                        current_session_id = session_id

                        # Store user message
                        database.add_session_message(
                            session_id=session_id,
                            role="user",
                            content=prompt
                        )

                        # Cancel any existing query for this session
                        if session_id in _active_chat_sessions:
                            _active_chat_sessions[session_id].cancel()
                            try:
                                await _active_chat_sessions[session_id]
                            except asyncio.CancelledError:
                                pass

                        # Start new query task
                        query_task = asyncio.create_task(
                            run_query(prompt, session_id, profile_id, project_id)
                        )
                        _active_chat_sessions[session_id] = query_task

                    elif msg_type == "stop":
                        # Stop current query
                        session_id = data.get("session_id") or current_session_id
                        if session_id and session_id in _active_chat_sessions:
                            _active_chat_sessions[session_id].cancel()

                    elif msg_type == "load_session":
                        # Load a session's message history
                        session_id = data.get("session_id")
                        if session_id:
                            session = database.get_session(session_id)
                            if session:
                                messages = database.get_session_messages(session_id)
                                current_session_id = session_id
                                await send_json({
                                    "type": "history",
                                    "session_id": session_id,
                                    "session": session,
                                    "messages": messages
                                })
                            else:
                                await send_json({"type": "error", "message": "Session not found"})

                    elif msg_type == "pong":
                        pass  # Keep-alive response

                except asyncio.TimeoutError:
                    if websocket.client_state != WebSocketState.CONNECTED:
                        break
                    continue

        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

            # Cancel any running query
            if query_task and not query_task.done():
                query_task.cancel()
                try:
                    await query_task
                except asyncio.CancelledError:
                    pass

    except WebSocketDisconnect:
        logger.info("Chat WebSocket disconnected")

    except Exception as e:
        logger.error(f"Chat WebSocket error: {e}")


# =============================================================================
# LEGACY SYNC WEBSOCKET - Kept for backwards compatibility
# =============================================================================

@router.websocket("/ws/sessions/{session_id}")
async def session_sync_websocket(
    websocket: WebSocket,
    session_id: str,
    device_id: str = Query(..., description="Unique device identifier"),
    token: str = Query(..., description="Authentication token")
):
    """
    WebSocket endpoint for real-time session synchronization.

    Connect to this endpoint to:
    - Receive real-time message updates from other devices
    - See streaming content as it's generated
    - Get notified of session state changes

    Query Parameters:
        - device_id: Unique identifier for this device (generated on frontend)
        - token: Authentication token (session cookie or API key)

    Messages from server:
        - stream_start: Streaming has begun
        - stream_chunk: A piece of streaming content
        - stream_end: Streaming has completed
        - message_added: A new message was added
        - session_updated: Session metadata changed
        - state: Current session state (sent on connect)
        - ping: Keep-alive message

    Messages to server:
        - pong: Response to ping
        - subscribe: Start watching a session (automatic on connect)
    """
    # Authenticate
    if not await authenticate_websocket(websocket, token):
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Verify session exists
    session = database.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    # Accept connection
    await websocket.accept()
    logger.info(f"WebSocket connected: device={device_id}, session={session_id}")

    # Register device with sync engine
    connection = await sync_engine.register_device(
        device_id=device_id,
        session_id=session_id,
        websocket=websocket
    )

    try:
        # Send initial state
        state = await sync_engine.get_session_state(session_id)
        state["session"] = session
        state["messages"] = database.get_session_messages(session_id)

        await websocket.send_json({
            "event_type": "state",
            "session_id": session_id,
            "data": state,
            "timestamp": None
        })

        # Keep connection alive and handle client messages
        ping_task = asyncio.create_task(ping_loop(websocket, device_id))

        try:
            while True:
                try:
                    # Wait for messages from client
                    data = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=60.0  # 60 second timeout
                    )

                    # Handle client messages
                    msg_type = data.get("type")

                    if msg_type == "pong":
                        # Client responding to ping - connection is alive
                        connection.last_activity = connection.connected_at.__class__.utcnow()

                    elif msg_type == "request_state":
                        # Client requesting current state
                        state = await sync_engine.get_session_state(session_id)
                        state["session"] = database.get_session(session_id)
                        state["messages"] = database.get_session_messages(session_id)
                        await websocket.send_json({
                            "event_type": "state",
                            "session_id": session_id,
                            "data": state,
                            "timestamp": None
                        })

                except asyncio.TimeoutError:
                    # No message received, check if connection is still alive
                    if websocket.client_state != WebSocketState.CONNECTED:
                        break
                    continue

        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: device={device_id}, session={session_id}")

    except Exception as e:
        logger.error(f"WebSocket error for device={device_id}: {e}")

    finally:
        # Unregister device
        await sync_engine.unregister_device(device_id, session_id)


async def ping_loop(websocket: WebSocket, device_id: str):
    """Send periodic pings to keep connection alive"""
    try:
        while True:
            await asyncio.sleep(30)  # Ping every 30 seconds

            if websocket.client_state != WebSocketState.CONNECTED:
                break

            try:
                await websocket.send_json({
                    "event_type": "ping",
                    "timestamp": None
                })
            except Exception:
                break

    except asyncio.CancelledError:
        pass


@router.websocket("/ws/global")
async def global_sync_websocket(
    websocket: WebSocket,
    device_id: str = Query(..., description="Unique device identifier"),
    token: str = Query(..., description="Authentication token")
):
    """
    Global WebSocket for receiving updates across all sessions.

    Useful for:
    - Updating session list when new sessions are created
    - Getting notified when any watched session changes
    - Global notifications

    This is lighter weight than per-session WebSockets when you just need
    to know that something changed (then fetch details via REST).
    """
    # Authenticate
    if not await authenticate_websocket(websocket, token):
        await websocket.close(code=4001, reason="Authentication failed")
        return

    await websocket.accept()
    logger.info(f"Global WebSocket connected: device={device_id}")

    # Track sessions this device is interested in
    watched_sessions: set = set()

    try:
        ping_task = asyncio.create_task(ping_loop(websocket, device_id))

        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        websocket.receive_json(),
                        timeout=60.0
                    )

                    msg_type = data.get("type")

                    if msg_type == "watch":
                        # Start watching a session
                        session_id = data.get("session_id")
                        if session_id:
                            watched_sessions.add(session_id)
                            await websocket.send_json({
                                "event_type": "watching",
                                "session_id": session_id
                            })

                    elif msg_type == "unwatch":
                        # Stop watching a session
                        session_id = data.get("session_id")
                        if session_id:
                            watched_sessions.discard(session_id)

                    elif msg_type == "pong":
                        pass

                except asyncio.TimeoutError:
                    if websocket.client_state != WebSocketState.CONNECTED:
                        break
                    continue

        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        logger.info(f"Global WebSocket disconnected: device={device_id}")

    except Exception as e:
        logger.error(f"Global WebSocket error for device={device_id}: {e}")
