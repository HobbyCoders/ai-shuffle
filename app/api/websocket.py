"""
WebSocket API for real-time chat and synchronization.

Primary endpoint: /ws/chat/{session_id}
- Handles all chat interactions via WebSocket
- Streams Claude responses directly to client
- Simple, reliable, no race conditions

Additional endpoints:
- /ws/cli/{session_id} - Interactive CLI bridge for commands like /rewind
"""

import logging
import json
import asyncio
import uuid
from typing import Optional, Dict, Any
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, status
from fastapi.websockets import WebSocketState

from app.core.sync_engine import sync_engine, SyncEvent
from app.db import database
from app.core.auth import auth_service
from app.core.profiles import get_profile
from app.core.cli_bridge import CLIBridge, RewindParser
from app.core.slash_commands import (
    discover_commands, get_command_by_name, is_slash_command,
    parse_command_input, is_interactive_command, get_all_commands
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["WebSocket"])

# Track active chat sessions for interruption
_active_chat_sessions: dict[str, asyncio.Task] = {}


async def authenticate_websocket(websocket: WebSocket, token: Optional[str]) -> tuple[bool, Optional[dict]]:
    """
    Validate authentication token for WebSocket connection.

    Returns:
        tuple[bool, Optional[dict]]: (is_authenticated, api_user_or_none)
        - For admin sessions: (True, None)
        - For API user sessions: (True, api_user_dict)
        - For failed auth: (False, None)
    """
    import hashlib

    # First try the token from query parameter
    if token:
        # Check admin session token
        session = database.get_auth_session(token)
        if session:
            return (True, None)  # Admin user

        # Check API key web session token
        api_key_session = database.get_api_key_session(token)
        if api_key_session:
            api_user = database.get_api_user(api_key_session["api_user_id"])
            if api_user and api_user.get("is_active", True):
                return (True, api_user)

        # Check raw API key (hashed)
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        api_user = database.get_api_user_by_key_hash(key_hash)
        if api_user and api_user.get("is_active", True):
            return (True, api_user)

    # Also check the cookie directly (for httpOnly cookies that JS can't read)
    cookie_token = websocket.cookies.get("session")
    if cookie_token:
        # Check admin session
        session = database.get_auth_session(cookie_token)
        if session:
            return (True, None)  # Admin user

        # Check API key web session
        api_key_session = database.get_api_key_session(cookie_token)
        if api_key_session:
            api_user = database.get_api_user(api_key_session["api_user_id"])
            if api_user and api_user.get("is_active", True):
                return (True, api_user)

    return (False, None)


# =============================================================================
# PRIMARY CHAT WEBSOCKET - Simple, reliable streaming
# =============================================================================

@router.websocket("/ws/chat")
async def chat_websocket(
    websocket: WebSocket,
    token: Optional[str] = Query(None, description="Authentication token (optional if cookie auth)"),
    device_id: Optional[str] = Query(None, description="Unique device identifier for multi-device sync")
):
    """
    Primary WebSocket endpoint for chat with multi-device sync support.

    This is the ONLY streaming mechanism needed. Simple flow:
    1. Client connects (optionally provides device_id for multi-device sync)
    2. Client sends: {"type": "query", "prompt": "...", "session_id": "...", "profile": "..."}
    3. Server streams response chunks directly
    4. Server sends: {"type": "done", ...} when complete
    5. Server broadcasts streaming events to other connected devices via SyncEngine

    Message types FROM server:
    - history: Full message history for session (on connect or session switch)
      - Includes isStreaming and streamingBuffer for late-joining devices
    - start: Query started, streaming will begin
    - chunk: Text content chunk
    - stream_delta: Real-time streaming delta (when include_partial_messages=True)
    - tool_use: Tool being used
    - tool_result: Tool result
    - done: Query complete with metadata
    - error: Error occurred
    - ping: Keep-alive
    - Sync events (from SyncEngine for other devices):
      - stream_start: Another device started streaming
      - stream_chunk: Streaming chunk from another device
      - stream_end: Another device finished streaming

    Message types TO server:
    - query: Start a new query
    - stop: Interrupt current query
    - load_session: Load/switch to a session
    - pong: Response to ping

    Multi-device sync features:
    - Automatic device registration with SyncEngine when session is used
    - Broadcasts all streaming events to other devices watching same session
    - Late-joining devices receive buffered streaming content
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
    logger.info(f"Chat WebSocket connected and authenticated (api_user_id={api_user_id})")

    # Generate device_id if not provided
    if not device_id:
        device_id = str(uuid.uuid4())
        logger.info(f"Generated device_id: {device_id}")

    current_session_id: Optional[str] = None
    query_task: Optional[asyncio.Task] = None

    async def send_json(data: dict):
        """Safe JSON send with connection check"""
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.send_json(data)
            except Exception as e:
                logger.warning(f"Failed to send WebSocket message: {e}")

    async def run_query(prompt: str, session_id: str, profile_id: str, project_id: Optional[str], overrides: Optional[Dict[str, Any]] = None):
        """Execute query and stream results directly to WebSocket"""
        nonlocal current_session_id

        from app.core.query_engine import stream_to_websocket

        # Generate a message ID for this streaming session
        message_id = str(uuid.uuid4())
        stream_started = False

        # Unregister this device from SyncEngine while streaming
        # This prevents duplicate events (we get direct events via send_json)
        # If the device disconnects and reconnects, it will re-register and get sync events
        await sync_engine.unregister_device(device_id, session_id)
        logger.info(f"Unregistered device {device_id} during streaming to prevent duplicates")

        try:
            logger.info(f"Starting query for session {session_id}, profile={profile_id}, project={project_id}, overrides={overrides}")
            await send_json({"type": "start", "session_id": session_id, "message_id": message_id})

            logger.info(f"Calling stream_to_websocket for session {session_id}")
            async for event in stream_to_websocket(
                prompt=prompt,
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                overrides=overrides
            ):
                event_type = event.get('type')
                logger.debug(f"Streaming event for session {session_id}: {event_type}")

                # Send to this websocket
                await send_json(event)

                # Broadcast to other devices via SyncEngine
                # Start streaming on first content event
                # Note: We pass source_device_id=None to NOT exclude any device.
                # The source device gets direct events via send_json(), but if it
                # disconnects and reconnects, its NEW websocket should receive sync events.
                # The frontend handles any potential duplication.
                if not stream_started and event_type in ('stream_start', 'stream_delta', 'chunk', 'tool_use'):
                    await sync_engine.broadcast_stream_start(
                        session_id=session_id,
                        message_id=message_id,
                        source_device_id=None  # Don't exclude - let all registered devices receive
                    )
                    stream_started = True

                # Broadcast stream chunks (source_device_id=None to not exclude any device)
                if event_type == 'stream_delta':
                    # Real-time streaming delta
                    delta_type = event.get('delta_type', 'text')
                    chunk_data = {
                        'content': event.get('content', ''),
                        'delta_type': delta_type,
                        'index': event.get('index', 0)
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='text',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'chunk':
                    # Legacy chunk event (when include_partial_messages=False)
                    chunk_data = {
                        'content': event.get('content', '')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='text',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'tool_use':
                    # Tool use event
                    chunk_data = {
                        'content': '',
                        'tool_name': event.get('name'),
                        'tool_id': event.get('id'),
                        'tool_input': event.get('input')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='tool_use',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'tool_result':
                    # Tool result event
                    chunk_data = {
                        'content': event.get('output', ''),
                        'tool_id': event.get('tool_use_id')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='tool_result',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'system':
                    # System message (e.g., /context output, local commands)
                    chunk_data = {
                        'content': '',
                        'subtype': event.get('subtype'),
                        'data': event.get('data')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='system',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'subagent_start':
                    # Subagent task started
                    chunk_data = {
                        'agent_id': event.get('agent_id'),
                        'agent_type': event.get('agent_type'),
                        'description': event.get('description'),
                        'prompt': event.get('prompt'),
                        'tool_id': event.get('tool_id')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='subagent_start',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'subagent_chunk':
                    # Text chunk from subagent
                    chunk_data = {
                        'agent_id': event.get('agent_id'),
                        'content': event.get('content', '')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='subagent_chunk',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'subagent_tool_use':
                    # Tool use within subagent
                    chunk_data = {
                        'agent_id': event.get('agent_id'),
                        'name': event.get('name'),
                        'id': event.get('id'),
                        'input': event.get('input')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='subagent_tool_use',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'subagent_tool_result':
                    # Tool result within subagent
                    chunk_data = {
                        'agent_id': event.get('agent_id'),
                        'tool_use_id': event.get('tool_use_id'),
                        'output': event.get('output', '')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='subagent_tool_result',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'subagent_done':
                    # Subagent completed
                    chunk_data = {
                        'agent_id': event.get('agent_id'),
                        'result': event.get('result'),
                        'is_error': event.get('is_error', False)
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='subagent_done',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'stream_block_start':
                    # Start of a content block (text, tool_use, etc.)
                    chunk_data = {
                        'block_type': event.get('block_type'),
                        'index': event.get('index'),
                        'content_block': event.get('content_block')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='stream_block_start',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'stream_block_stop':
                    # End of a content block
                    chunk_data = {
                        'index': event.get('index')
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='stream_block_stop',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'done':
                    # Stream completed - broadcast end
                    if stream_started:
                        await sync_engine.broadcast_stream_end(
                            session_id=session_id,
                            message_id=message_id,
                            metadata=event.get('metadata', {}),
                            interrupted=False,
                            source_device_id=None
                        )

            logger.info(f"Query completed for session {session_id}")

        except asyncio.CancelledError:
            await send_json({"type": "stopped", "session_id": session_id})
            logger.info(f"Query cancelled for session {session_id}")
            # Broadcast stream end with interrupted flag
            if stream_started:
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id=message_id,
                    metadata={},
                    interrupted=True,
                    source_device_id=None
                )

        except Exception as e:
            logger.error(f"Query error for session {session_id}: {e}", exc_info=True)
            await send_json({"type": "error", "message": str(e)})
            # Broadcast stream end on error
            if stream_started:
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id=message_id,
                    metadata={"error": str(e)},
                    interrupted=True,
                    source_device_id=None
                )

        finally:
            # Re-register device with SyncEngine now that streaming is complete,
            # but ONLY if the websocket is still connected.
            # If the user disconnected during streaming (phone locked, etc.),
            # they will re-register when they reconnect via load_session.
            if websocket.client_state == WebSocketState.CONNECTED:
                await sync_engine.register_device(device_id, session_id, websocket)
                logger.info(f"Re-registered device {device_id} after streaming complete")
            else:
                logger.info(f"Skipping re-register for device {device_id} - websocket disconnected during streaming")

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
                        overrides = data.get("overrides")  # Optional: {model, permission_mode}

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
                                project_id=project_id,
                                api_user_id=api_user_id
                            )

                        # Register device with session if switching sessions
                        if session_id != current_session_id:
                            # Unregister from old session if any
                            if current_session_id:
                                await sync_engine.unregister_device(device_id, current_session_id)

                            # Register to new session
                            await sync_engine.register_device(device_id, session_id, websocket)
                            current_session_id = session_id

                        # Store user message
                        database.add_session_message(
                            session_id=session_id,
                            role="user",
                            content=prompt
                        )

                        # Broadcast user message to other devices
                        await sync_engine.broadcast_message_added(
                            session_id=session_id,
                            message={
                                "role": "user",
                                "content": prompt
                            },
                            source_device_id=device_id  # Exclude the sender
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
                            run_query(prompt, session_id, profile_id, project_id, overrides)
                        )
                        _active_chat_sessions[session_id] = query_task

                    elif msg_type == "stop":
                        # Stop current query - use interrupt_session for proper SDK-level cancellation
                        session_id = data.get("session_id") or current_session_id
                        if session_id:
                            from app.core.query_engine import interrupt_session
                            # First, try to interrupt at the SDK level (this signals Claude to stop)
                            interrupted = await interrupt_session(session_id)
                            logger.info(f"Interrupt session {session_id}: {interrupted}")

                            # Then cancel the asyncio task as a backup
                            if session_id in _active_chat_sessions:
                                task = _active_chat_sessions[session_id]
                                if not task.done():
                                    task.cancel()
                                    try:
                                        await asyncio.wait_for(task, timeout=2.0)
                                    except (asyncio.CancelledError, asyncio.TimeoutError):
                                        pass

                    elif msg_type == "load_session":
                        # Load a session's message history from JSONL file
                        session_id = data.get("session_id")
                        if session_id:
                            session = database.get_session(session_id)
                            if session:
                                # Unregister from old session if switching
                                if current_session_id and current_session_id != session_id:
                                    await sync_engine.unregister_device(device_id, current_session_id)

                                # Register to new session
                                await sync_engine.register_device(device_id, session_id, websocket)
                                current_session_id = session_id

                                # Try to load from JSONL file first (source of truth)
                                sdk_session_id = session.get("sdk_session_id")
                                messages = []

                                if sdk_session_id:
                                    try:
                                        from app.core.jsonl_parser import parse_session_history
                                        # Get working dir from project if available
                                        working_dir = "/workspace"
                                        project_id = session.get("project_id")
                                        if project_id:
                                            project = database.get_project(project_id)
                                            if project:
                                                from app.core.config import settings
                                                working_dir = str(settings.workspace_dir / project["path"])

                                        messages = parse_session_history(sdk_session_id, working_dir)
                                        logger.info(f"Loaded {len(messages)} messages from JSONL for session {session_id}")
                                    except Exception as e:
                                        logger.error(f"Failed to parse JSONL for session {session_id}: {e}")
                                        messages = []

                                # Fall back to database if JSONL not available or failed
                                if not messages:
                                    db_messages = database.get_session_messages(session_id)
                                    # Transform DB messages to streaming format
                                    for m in db_messages:
                                        msg_type_value = None
                                        if m.get("tool_name"):
                                            msg_type_value = "tool_use"
                                        elif m.get("role") == "assistant":
                                            msg_type_value = "text"
                                        elif m.get("role") in ("tool_use", "tool_result"):
                                            msg_type_value = m.get("role")

                                        messages.append({
                                            "id": f"msg-{m.get('id', 0)}",
                                            "role": "assistant" if m.get("role") in ("tool_use", "tool_result") else m.get("role"),
                                            "content": m.get("content", ""),
                                            "type": msg_type_value,
                                            "toolName": m.get("tool_name"),
                                            "toolId": m.get("tool_id"),
                                            "toolInput": m.get("tool_input"),
                                            "metadata": m.get("metadata"),
                                            "streaming": False
                                        })
                                    logger.info(f"Loaded {len(messages)} messages from DB for session {session_id}")

                                # Check if session is currently streaming (late-joining device)
                                is_streaming = sync_engine.is_session_streaming(session_id)
                                streaming_buffer = None
                                if is_streaming:
                                    streaming_buffer = sync_engine.get_streaming_buffer(session_id)
                                    logger.info(f"Session {session_id} is streaming, sending buffer to late-joining device")

                                await send_json({
                                    "type": "history",
                                    "session_id": session_id,
                                    "session": session,
                                    "messages": messages,
                                    "isStreaming": is_streaming,
                                    "streamingBuffer": streaming_buffer
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

            # Unregister device from sync engine
            if current_session_id:
                await sync_engine.unregister_device(device_id, current_session_id)
                logger.info(f"Unregistered device {device_id} from session {current_session_id}")

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


# =============================================================================
# CLI BRIDGE WEBSOCKET - Interactive terminal for /rewind and similar commands
# =============================================================================

# Track active CLI bridges
_active_cli_bridges: Dict[str, CLIBridge] = {}


@router.websocket("/ws/cli/{session_id}")
async def cli_websocket(
    websocket: WebSocket,
    session_id: str,
    token: Optional[str] = Query(None, description="Authentication token")
):
    """
    WebSocket endpoint for interactive CLI commands like /rewind.

    This creates a PTY bridge to the Claude CLI, allowing the frontend
    to render a terminal and interact with commands that require
    keyboard input (arrow keys, Enter, etc.).

    Message types FROM server:
    - output: Terminal output data (may include ANSI codes)
    - ready: CLI is ready for input
    - exit: CLI process has exited
    - error: Error occurred
    - rewind_complete: Rewind operation completed with checkpoint info

    Message types TO server:
    - input: Raw text input
    - key: Special key (up, down, enter, escape, etc.)
    - resize: Terminal resize {cols, rows}
    - start: Start CLI with command {command: "/rewind"}
    - stop: Stop the CLI process
    """
    await websocket.accept()

    is_authenticated, _ = await authenticate_websocket(websocket, token)
    if not is_authenticated:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Get session info
    session = database.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    sdk_session_id = session.get("sdk_session_id")
    if not sdk_session_id:
        await websocket.send_json({
            "type": "error",
            "message": "Session has no SDK session ID - cannot use CLI commands"
        })
        await websocket.close(code=4005, reason="No SDK session")
        return

    # Get working directory from project or profile
    working_dir = "/workspace"
    project_id = session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            from app.core.config import settings
            working_dir = str(settings.workspace_dir / project["path"])

    logger.info(f"CLI WebSocket connected for session {session_id}, sdk_session={sdk_session_id}")

    cli_bridge: Optional[CLIBridge] = None
    output_buffer = ""  # For parsing rewind output

    async def on_output(data: str):
        """Handle CLI output"""
        nonlocal output_buffer
        output_buffer += data

        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({
                "type": "output",
                "data": data
            })

            # Check if rewind completed
            if RewindParser.is_rewind_complete(output_buffer):
                checkpoint_msg = RewindParser.get_selected_checkpoint_message(output_buffer)
                selected_option = RewindParser.parse_selected_option(output_buffer)

                await websocket.send_json({
                    "type": "rewind_complete",
                    "checkpoint_message": checkpoint_msg,
                    "selected_option": selected_option,
                    "options": {
                        1: "Restore code and conversation",
                        2: "Restore conversation",
                        3: "Restore code",
                        4: "Never mind"
                    }
                })

    async def on_exit(exit_code: int):
        """Handle CLI exit"""
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.send_json({
                "type": "exit",
                "exit_code": exit_code
            })

    try:
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=300.0  # 5 minute timeout for CLI operations
                )

                msg_type = data.get("type")

                if msg_type == "start":
                    # Start CLI with specified command
                    command = data.get("command", "/rewind")

                    if cli_bridge and cli_bridge.is_running:
                        await cli_bridge.stop()

                    output_buffer = ""  # Reset buffer

                    cli_bridge = CLIBridge(
                        session_id=session_id,
                        sdk_session_id=sdk_session_id,
                        working_dir=working_dir,
                        on_output=on_output,
                        on_exit=on_exit
                    )

                    success = await cli_bridge.start(command)
                    if success:
                        _active_cli_bridges[session_id] = cli_bridge
                        await websocket.send_json({
                            "type": "ready",
                            "command": command
                        })
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Failed to start CLI"
                        })

                elif msg_type == "input":
                    # Send raw input
                    if cli_bridge and cli_bridge.is_running:
                        await cli_bridge.send_input(data.get("data", ""))

                elif msg_type == "key":
                    # Send special key
                    if cli_bridge and cli_bridge.is_running:
                        await cli_bridge.send_key(data.get("key", ""))

                elif msg_type == "resize":
                    # Resize terminal
                    if cli_bridge and cli_bridge.is_running:
                        cols = data.get("cols", 80)
                        rows = data.get("rows", 24)
                        await cli_bridge.resize(cols, rows)

                elif msg_type == "stop":
                    # Stop CLI
                    if cli_bridge:
                        await cli_bridge.stop()
                        cli_bridge = None

                elif msg_type == "pong":
                    pass

            except asyncio.TimeoutError:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
                # Send ping
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        logger.info(f"CLI WebSocket disconnected for session {session_id}")

    except Exception as e:
        logger.error(f"CLI WebSocket error for session {session_id}: {e}", exc_info=True)

    finally:
        # Clean up CLI bridge
        if cli_bridge:
            await cli_bridge.stop()
        if session_id in _active_cli_bridges:
            del _active_cli_bridges[session_id]
