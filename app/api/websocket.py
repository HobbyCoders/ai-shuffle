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
import asyncio
import uuid
from typing import Optional, Dict, Any, Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.websockets import WebSocketState

from app.core.sync_engine import sync_engine
from app.core.live_session_store import live_session_store
from app.core.config import settings
from app.db import database
from app.core.webhook_service import dispatch_session_complete, dispatch_session_error
from app.core.cli_bridge import CLIBridge, RewindParser
from app.core.project_utils import get_project_working_dir
from app.core.permission_handler import permission_handler
from app.core.user_question_handler import user_question_handler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["WebSocket"])

# Track active chat sessions for interruption
_active_chat_sessions: dict[str, asyncio.Task] = {}


async def _delayed_session_cleanup(session_id: str, delay_seconds: int = 30) -> None:
    """
    Clean up an ended session from live_session_store after a delay.

    This gives reconnecting clients time to retrieve messages before
    they're removed from memory. After the delay, messages must be
    retrieved from JSONL file instead.
    """
    await asyncio.sleep(delay_seconds)
    await live_session_store.cleanup_ended_session(session_id)


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
    - queue_message: Queue a message to send while Claude is working (streaming input)
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
    # user_id for user-level broadcasts (admin users use a constant since there's one admin)
    user_id: Optional[str] = api_user_id or "admin"
    logger.debug(f"Chat WebSocket connected and authenticated (api_user_id={api_user_id})")

    # Generate device_id if not provided
    if not device_id:
        device_id = str(uuid.uuid4())
        logger.debug(f"Generated device_id: {device_id}")

    # Track if connection is healthy (for detecting silent disconnects during streaming)
    connection_healthy = True

    # Lock to serialize WebSocket sends — prevents concurrent sends from
    # ping_loop, heartbeat, and the main message handler racing each other
    send_lock = asyncio.Lock()

    async def send_json(data: dict) -> bool:
        """
        Safe JSON send with connection check.

        Returns True if send succeeded, False if connection is dead.
        Callers should check return value and abort operations if False.

        Uses a lock to make the state-check + send atomic, preventing
        TOCTOU races where a close frame arrives between the check and send.
        """
        nonlocal connection_healthy

        # Fast path: skip lock acquisition if already known dead
        if not connection_healthy:
            return False

        async with send_lock:
            if not connection_healthy:
                return False

            if websocket.client_state != WebSocketState.CONNECTED:
                connection_healthy = False
                logger.debug(f"WebSocket disconnected, cannot send message type: {data.get('type')}")
                return False

            try:
                await websocket.send_json(data)
                return True
            except Exception as e:
                connection_healthy = False
                logger.warning(f"Failed to send WebSocket message (type={data.get('type')}): {e}")
                return False

    # Register user device for tab sync
    await sync_engine.register_user_device(user_id, device_id, websocket)

    # Always send streaming sessions snapshot on connect so the client can
    # reconcile stale entries from before a disconnect.  Previously this was
    # only sent when there *were* streaming sessions, which meant stale
    # entries from a missed `done` event would persist indefinitely.
    user_streaming = sync_engine.get_user_streaming_sessions(user_id)
    await send_json({
        "type": "user_streaming_sessions",
        "sessions": user_streaming or [],
        "session_ids": [s["session_id"] for s in user_streaming] if user_streaming else [],
    })

    current_session_id: Optional[str] = None
    query_task: Optional[asyncio.Task] = None

    async def run_query(prompt: str, session_id: str, profile_id: str, project_id: Optional[str], overrides: Optional[Dict[str, Any]] = None, worktree_id: Optional[str] = None):
        """Execute query and stream results directly to WebSocket"""
        nonlocal current_session_id

        from app.core.query_engine import stream_to_websocket
        from app.core.jsonl_parser import parse_session_history
        from app.core.config import settings
        import time

        # Generate a message ID for this streaming session
        message_id = str(uuid.uuid4())
        stream_started = False
        connection_lost_logged = False  # Only log WebSocket disconnect once
        query_start_time = time.time()  # Track duration for webhooks
        final_metadata: Dict[str, Any] = {}  # Store metadata from done event
        current_tool_id: Optional[str] = None  # Track current tool for input streaming
        current_tool_name: Optional[str] = None  # Track current tool name for result broadcasts

        # Unregister this device from SyncEngine while streaming
        # This prevents duplicate events (we get direct events via send_json)
        # If the device disconnects and reconnects, it will re-register and get sync events
        await sync_engine.unregister_device(device_id, session_id)
        logger.debug(f"Unregistered device {device_id} during streaming to prevent duplicates")

        # Initialize LiveSessionStore for server-authoritative state
        # IMPORTANT: Check live store first for complete messages from an interrupted session.
        # During interrupt_and_query, end_streaming() preserves messages in memory, but
        # the JSONL file may be incomplete (subprocess killed before flushing).
        # Using live store messages prevents context loss on interrupt transitions.
        existing_live = live_session_store.get_session(session_id)
        if existing_live and not existing_live.is_streaming and existing_live.messages:
            # Use complete messages from the interrupted session's live store
            initial_messages = [m.to_dict() for m in existing_live.messages]
            logger.debug(f"Using {len(initial_messages)} messages from live store (interrupted session) for {session_id}")
        else:
            # Fall back to JSONL parsing (normal path: no interrupted session in memory)
            initial_messages = []
            session_data = database.get_session(session_id)
            if session_data:
                sdk_session_id = session_data.get("sdk_session_id")
                if sdk_session_id:
                    working_dir = str(settings.workspace_dir)
                    if project_id:
                        project = database.get_project(project_id)
                        if project:
                            working_dir = get_project_working_dir(project)
                    try:
                        initial_messages, _usage = parse_session_history(sdk_session_id, working_dir)
                    except Exception as e:
                        logger.warning(f"Failed to load history for live store: {e}")

        # Start live session with existing messages
        await live_session_store.start_streaming(session_id, initial_messages)

        # Add user message to live store
        user_message_id = f"user-{uuid.uuid4()}"
        await live_session_store.add_user_message(session_id, prompt, user_message_id)

        # Heartbeat task to keep WebSocket alive during long operations (tool execution, subagents)
        # The streaming loop blocks when SDK is waiting for tool results, so we send periodic
        # heartbeats to prevent frontend from thinking the connection is dead.
        heartbeat_task: Optional[asyncio.Task] = None
        heartbeat_stop = asyncio.Event()

        async def send_heartbeats():
            """Send periodic heartbeat events while streaming.
            Includes pending permission/question counts so the frontend can
            detect and recover missed permission_request / user_question messages."""
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=5.0)
                    # Event was set, stop sending heartbeats
                    break
                except asyncio.TimeoutError:
                    # Timeout expired, send heartbeat with pending counts
                    heartbeat_data: dict = {
                        "type": "heartbeat",
                        "timestamp": asyncio.get_event_loop().time(),
                    }
                    # Include pending permission/question counts so the frontend
                    # can recover from missed WebSocket messages
                    if session_id:
                        perm_count = permission_handler.get_queue_count(session_id)
                        question_count = len(user_question_handler.get_pending_questions(session_id))
                        if perm_count > 0 or question_count > 0:
                            heartbeat_data["pending_permissions"] = perm_count
                            heartbeat_data["pending_questions"] = question_count
                    await send_json(heartbeat_data)

        try:
            logger.debug(f"Starting query for session {session_id}, profile={profile_id}, project={project_id}, overrides={overrides}")
            if not await send_json({"type": "start", "session_id": session_id, "message_id": message_id}):
                logger.warning(f"Connection lost before query could start for session {session_id}")
                await live_session_store.cleanup_session(session_id)
                return  # Exit early - connection is dead

            # NOTE: Don't call start_assistant_message here!
            # Creating an empty text message before content arrives causes the "Claude [time]"
            # header to appear even when the first content is a thinking block.
            # Let append_text/append_thinking create messages lazily when content arrives.

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(send_heartbeats())

            logger.debug(f"Calling stream_to_websocket for session {session_id}, api_user_id={api_user_id}")
            async for event in stream_to_websocket(
                prompt=prompt,
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                overrides=overrides,
                broadcast_func=send_json,  # Pass send_json for permission requests
                api_user_id=api_user_id  # Pass for user-scoped credential resolution
            ):
                event_type = event.get('type')
                if event_type == 'plugin_message':
                    logger.debug(f"[WS] Sending plugin_message to frontend: {event}")
                else:
                    logger.debug(f"Streaming event for session {session_id}: {event_type}")

                # Send to this websocket - may fail if connection died
                # Important: Do NOT skip store updates if WebSocket fails!
                # The live_session_store must be updated regardless of WebSocket state
                # so that polling clients can receive updates.
                ws_connected = await send_json(event)
                if not ws_connected and not connection_lost_logged:
                    connection_lost_logged = True
                    logger.debug(f"WebSocket disconnected during streaming for session {session_id}, updates continue via sync_engine + polling")

                # Broadcast to other devices via SyncEngine
                # Start streaming on first content event
                # Note: We pass source_device_id=None to NOT exclude any device.
                # The source device gets direct events via send_json(), but if it
                # disconnects and reconnects, its NEW websocket should receive sync events.
                # The frontend handles any potential duplication.
                if not stream_started and event_type in ('stream_start', 'stream_delta', 'chunk', 'tool_use'):
                    # Extract usage data from stream_start event if available
                    usage_data = None
                    if event_type == 'stream_start':
                        message_data = event.get('message', {})
                        if isinstance(message_data, dict):
                            usage_data = message_data.get('usage')
                            # Update live session store with usage for polling clients
                            if usage_data:
                                await live_session_store.update_usage(
                                    session_id,
                                    input_tokens=usage_data.get('input_tokens', 0),
                                    output_tokens=usage_data.get('output_tokens', 0),
                                    cache_creation_tokens=usage_data.get('cache_creation_input_tokens', 0),
                                    cache_read_tokens=usage_data.get('cache_read_input_tokens', 0)
                                )

                    await sync_engine.broadcast_stream_start(
                        session_id=session_id,
                        message_id=message_id,
                        source_device_id=None,  # Don't exclude - let all registered devices receive
                        usage=usage_data,
                        user_id=user_id,  # Enable user-level broadcast for side panel sync
                        project_id=project_id,  # Enable workspace-level streaming awareness
                        worktree_id=worktree_id  # Worktree sessions have independent working dirs
                    )
                    stream_started = True
                elif stream_started and event_type == 'stream_start':
                    # Subsequent stream_start events (agentic loops with tool calls)
                    # Each new assistant turn has updated token usage that should be broadcast
                    message_data = event.get('message', {})
                    if isinstance(message_data, dict):
                        usage_data = message_data.get('usage')
                        if usage_data:
                            # Update live session store with latest usage for polling clients
                            await live_session_store.update_usage(
                                session_id,
                                input_tokens=usage_data.get('input_tokens', 0),
                                output_tokens=usage_data.get('output_tokens', 0),
                                cache_creation_tokens=usage_data.get('cache_creation_input_tokens', 0),
                                cache_read_tokens=usage_data.get('cache_read_input_tokens', 0)
                            )
                            # Broadcast usage update to synced devices
                            await sync_engine.broadcast_usage_update(
                                session_id=session_id,
                                usage=usage_data
                            )
                            logger.debug(f"Broadcast usage update for session {session_id}: {usage_data}")

                # Broadcast stream chunks AND update LiveSessionStore
                # LiveSessionStore is the server-authoritative state for reconnecting clients
                if event_type == 'stream_delta':
                    # Real-time streaming delta - handle different delta types
                    delta_type = event.get('delta_type', 'text')
                    content = event.get('content', '')

                    if delta_type == 'tool_input':
                        # Tool input streaming - update live store
                        if current_tool_id:
                            await live_session_store.update_tool_input_streaming(
                                session_id, current_tool_id, content
                            )
                        # Broadcast as tool_input type
                        chunk_data = {
                            'content': content,
                            'delta_type': delta_type,
                            'index': event.get('index', 0)
                        }
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=message_id,
                            chunk_type='tool_input',
                            chunk_data=chunk_data,
                            source_device_id=None
                        )
                    elif delta_type == 'thinking':
                        # Thinking delta - update live store with thinking type
                        await live_session_store.append_thinking(session_id, content)
                        # Broadcast with thinking chunk_type so StreamingBuffer handles it correctly
                        chunk_data = {
                            'content': content,
                            'delta_type': delta_type,
                            'index': event.get('index', 0)
                        }
                        await sync_engine.broadcast_stream_chunk(
                            session_id=session_id,
                            message_id=message_id,
                            chunk_type='thinking',  # Fixed: was 'text', causing thinking to mix with text
                            chunk_data=chunk_data,
                            source_device_id=None
                        )
                    else:
                        # Text delta - finalize any streaming thinking, then update live store
                        await live_session_store.finalize_thinking(session_id)
                        await live_session_store.append_text(session_id, content)
                        # Broadcast as text
                        chunk_data = {
                            'content': content,
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
                    content = event.get('content', '')
                    # Update live store
                    await live_session_store.append_text(session_id, content)
                    # Broadcast
                    chunk_data = {'content': content}
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='text',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'tool_use':
                    # Tool use event - update live store
                    tool_id = event.get('id')
                    current_tool_id = tool_id  # Track for input streaming
                    current_tool_name = event.get('name')  # Track for result broadcasts
                    await live_session_store.add_tool_use(
                        session_id=session_id,
                        tool_id=tool_id,
                        name=event.get('name'),
                        tool_input=event.get('input', {})
                    )
                    # Broadcast
                    chunk_data = {
                        'content': '',
                        'tool_name': event.get('name'),
                        'tool_id': tool_id,
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
                    # Tool result event - update live store
                    tool_id = event.get('tool_use_id')
                    tool_name = current_tool_name  # Capture before clearing
                    current_tool_id = None  # Clear tool tracking
                    current_tool_name = None
                    await live_session_store.add_tool_result(
                        session_id=session_id,
                        tool_id=tool_id,
                        result=event.get('output', ''),
                        is_error=event.get('is_error', False)
                    )
                    # Broadcast
                    chunk_data = {
                        'content': event.get('output', ''),
                        'tool_id': tool_id,
                        'tool_name': tool_name,
                        'is_error': event.get('is_error', False)
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='tool_result',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'system':
                    # System message - update live store
                    await live_session_store.add_system_message(
                        session_id=session_id,
                        content='',
                        subtype=event.get('subtype'),
                        data=event.get('data')
                    )
                    # Broadcast
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
                    # Subagent task started - update live store
                    # Try to convert the early tool_use (from stream_block_start)
                    # to a subagent message. If no early tool exists, create fresh.
                    agent_id = event.get('agent_id')
                    converted = await live_session_store.convert_tool_to_subagent(
                        session_id=session_id,
                        tool_id=event.get('tool_id') or agent_id,
                        agent_id=agent_id,
                        agent_type=event.get('agent_type', 'unknown'),
                        description=event.get('description', ''),
                        prompt=event.get('prompt', '')
                    )
                    if not converted:
                        await live_session_store.add_subagent_start(
                            session_id=session_id,
                            agent_id=agent_id,
                            agent_type=event.get('agent_type'),
                            description=event.get('description'),
                            prompt=event.get('prompt'),
                            tool_id=event.get('tool_id')
                        )
                    # Broadcast
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
                    # Text chunk from subagent - update live store
                    await live_session_store.append_subagent_text(
                        session_id=session_id,
                        agent_id=event.get('agent_id'),
                        content=event.get('content', '')
                    )
                    # Broadcast
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
                    # Tool use within subagent - update live store
                    await live_session_store.add_subagent_tool_use(
                        session_id=session_id,
                        agent_id=event.get('agent_id'),
                        tool_id=event.get('id'),
                        tool_name=event.get('name'),
                        tool_input=event.get('input', {})
                    )
                    # Broadcast
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
                    # Tool result within subagent - update live store
                    await live_session_store.add_subagent_tool_result(
                        session_id=session_id,
                        agent_id=event.get('agent_id'),
                        tool_id=event.get('tool_use_id'),
                        result=event.get('output', ''),
                        is_error=event.get('is_error', False)
                    )
                    # Broadcast
                    chunk_data = {
                        'agent_id': event.get('agent_id'),
                        'tool_use_id': event.get('tool_use_id'),
                        'output': event.get('output', ''),
                        'is_error': event.get('is_error', False)
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='subagent_tool_result',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type == 'subagent_done':
                    # Subagent completed - update live store
                    await live_session_store.complete_subagent(
                        session_id=session_id,
                        agent_id=event.get('agent_id'),
                        result=event.get('result', ''),
                        is_error=event.get('is_error', False)
                    )
                    # Broadcast
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
                    block_type = event.get('block_type')
                    content_block = event.get('content_block', {})

                    # For tool_use blocks, create the tool message in the live store
                    # immediately so polling clients see it right away instead of
                    # waiting for the full AssistantMessage (which can take seconds
                    # for large tool inputs like Write/Edit/Bash).
                    if block_type == 'tool_use':
                        early_tool_id = content_block.get('id')
                        early_tool_name = content_block.get('name', 'unknown')
                        if early_tool_id:
                            current_tool_id = early_tool_id
                            await live_session_store.add_early_tool_use(
                                session_id=session_id,
                                tool_id=early_tool_id,
                                name=early_tool_name
                            )

                    chunk_data = {
                        'block_type': block_type,
                        'index': event.get('index'),
                        'content_block': content_block
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
                elif event_type == 'stream_message_delta':
                    # Final message metadata with output token usage
                    # Broadcast to sync output tokens across devices
                    chunk_data = {
                        'delta': event.get('delta', {}),
                        'usage': event.get('usage', {})
                    }
                    await sync_engine.broadcast_stream_chunk(
                        session_id=session_id,
                        message_id=message_id,
                        chunk_type='stream_message_delta',
                        chunk_data=chunk_data,
                        source_device_id=None
                    )
                elif event_type in ('done', 'interrupted'):
                    # Store metadata for webhook dispatch
                    is_interrupted = event_type == 'interrupted'
                    final_metadata = event.get('metadata', {})

                    # End live session store streaming
                    # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
                    await live_session_store.end_streaming(
                        session_id=session_id,
                        metadata=final_metadata,
                        interrupted=is_interrupted
                    )

                    # Pre-seed JSONL parse cache with live store messages instead of
                    # invalidating. This avoids expensive JSONL re-parsing on the next
                    # load - the live store already has the final messages in memory.
                    from app.core.jsonl_parser import pre_seed_parse_cache, invalidate_parse_cache
                    session_row = database.get_session(session_id)
                    sdk_sid = session_row.get("sdk_session_id") if session_row else None
                    if sdk_sid:
                        live_data = live_session_store.get_session_dict(session_id)
                        if live_data and live_data.get("messages"):
                            # Build working_dir for JSONL path lookup
                            seed_working_dir = str(settings.workspace_dir)
                            seed_project_id = session_row.get("project_id") if session_row else None
                            if seed_project_id:
                                seed_project = database.get_project(seed_project_id)
                                if seed_project:
                                    seed_working_dir = get_project_working_dir(seed_project)

                            # Build usage_data from final metadata + live store
                            live_usage = live_data.get("usage", {})
                            seed_usage = {
                                "total_tokens_in": live_usage.get("input_tokens", 0),
                                "total_tokens_out": live_usage.get("output_tokens", 0),
                                "cache_creation_tokens": final_metadata.get("cache_creation_tokens", live_usage.get("cache_creation_tokens", 0)),
                                "cache_read_tokens": final_metadata.get("cache_read_tokens", live_usage.get("cache_read_tokens", 0)),
                                "last_input_tokens": final_metadata.get("tokens_in", 0),
                                "model": final_metadata.get("model")
                            }

                            pre_seed_parse_cache(sdk_sid, live_data["messages"], seed_usage, seed_working_dir)
                        else:
                            # Live store empty - fall back to invalidation
                            invalidate_parse_cache(sdk_sid)

                    # Schedule delayed cleanup (gives clients time to reconnect)
                    asyncio.create_task(_delayed_session_cleanup(session_id, delay_seconds=30))

                    # Stream completed/interrupted - broadcast end
                    if stream_started:
                        await sync_engine.broadcast_stream_end(
                            session_id=session_id,
                            message_id=message_id,
                            metadata=final_metadata,
                            interrupted=is_interrupted,
                            source_device_id=None
                        )

            logger.debug(f"Query completed for session {session_id}")

            # Dispatch webhook for session completion (non-blocking)
            import time as time_module
            duration = time_module.time() - query_start_time
            session_data = database.get_session(session_id)
            try:
                asyncio.create_task(dispatch_session_complete(
                    session_id=session_id,
                    title=session_data.get("title") if session_data else None,
                    profile_id=profile_id,
                    total_cost=final_metadata.get("total_cost_usd", 0.0),
                    input_tokens=final_metadata.get("tokens_in", 0),
                    output_tokens=final_metadata.get("tokens_out", 0),
                    duration_seconds=duration
                ))
            except Exception as webhook_error:
                logger.warning(f"Failed to dispatch session.complete webhook: {webhook_error}")

        except asyncio.CancelledError:
            await send_json({"type": "stopped", "session_id": session_id})
            logger.debug(f"Query cancelled for session {session_id}")

            # End live session store with interrupted flag
            # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
            await live_session_store.end_streaming(
                session_id=session_id,
                metadata={},
                interrupted=True
            )

            # Schedule delayed cleanup of ended session (gives clients time to reconnect)
            # This prevents memory leak while ensuring messages are available for ~30 seconds
            asyncio.create_task(_delayed_session_cleanup(session_id, delay_seconds=30))

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

            # End live session store on error
            # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
            await live_session_store.end_streaming(
                session_id=session_id,
                metadata={"error": str(e)},
                interrupted=True
            )

            # Schedule delayed cleanup of ended session (gives clients time to reconnect)
            asyncio.create_task(_delayed_session_cleanup(session_id, delay_seconds=30))

            # Broadcast stream end on error
            if stream_started:
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id=message_id,
                    metadata={"error": str(e)},
                    interrupted=True,
                    source_device_id=None
                )

            # Dispatch webhook for session error (non-blocking)
            session_data = database.get_session(session_id)
            try:
                asyncio.create_task(dispatch_session_error(
                    session_id=session_id,
                    title=session_data.get("title") if session_data else None,
                    profile_id=profile_id,
                    error_message=str(e)
                ))
            except Exception as webhook_error:
                logger.warning(f"Failed to dispatch session.error webhook: {webhook_error}")

        finally:
            # Stop heartbeat task
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_stop.set()
                try:
                    await asyncio.wait_for(heartbeat_task, timeout=1.0)
                except asyncio.TimeoutError:
                    heartbeat_task.cancel()

            # Only cleanup live session store if end_streaming was never called
            # (i.e., an exception occurred before the normal done/error/cancel paths ran).
            # If end_streaming WAS called, the session is kept in memory intentionally
            # for ~30 seconds so reconnecting clients can retrieve messages.
            if live_session_store.has_session(session_id) and live_session_store.is_streaming(session_id):
                logger.debug(f"Cleaning up live session store for session {session_id} (finally block - streaming never ended)")
                await live_session_store.cleanup_session(session_id)

            # ALWAYS ensure streaming state is cleared, even if stream_started was never set
            # This prevents stuck streaming state after errors or early termination
            if sync_engine.is_session_streaming(session_id):
                if not stream_started:
                    # Stream was marked but never actually started - clean up silently
                    logger.debug(f"Cleaning up streaming state for session {session_id} (never started)")
                    await sync_engine.broadcast_stream_end(
                        session_id=session_id,
                        message_id=message_id,
                        metadata={},
                        interrupted=True,
                        source_device_id=None
                    )

            # Re-register device with SyncEngine now that streaming is complete,
            # but ONLY if:
            # 1. This websocket is still connected
            # 2. No newer websocket has been registered for this device (e.g., user refreshed page)
            #
            # If the user disconnected and reconnected during streaming, their NEW websocket
            # is already registered via load_session. We must NOT replace it with this old one.
            if websocket.client_state == WebSocketState.CONNECTED:
                # Check if a different websocket is already registered for this device
                # (This happens when user refreshes the page during streaming)
                current_connection = sync_engine._connections.get(session_id, {}).get(device_id)
                if current_connection is not None and current_connection.websocket is not websocket:
                    # A newer connection exists - don't replace it with this old websocket
                    logger.debug(f"Skipping re-register for device {device_id} - newer connection already exists")
                else:
                    await sync_engine.register_device(device_id, session_id, websocket)
                    logger.debug(f"Re-registered device {device_id} after streaming complete")
            else:
                logger.debug(f"Skipping re-register for device {device_id} - websocket disconnected during streaming")

            # Clean up task reference only if it's still our task (avoid TOCTOU race
            # where a different device started a new query for the same session)
            current_task = asyncio.current_task()
            if _active_chat_sessions.get(session_id) is current_task:
                del _active_chat_sessions[session_id]

            # If no devices are connected after query finished, schedule cleanup
            # (don't destroy immediately — user may reopen within 30s)
            remaining_devices = sync_engine.get_device_count(session_id)
            if remaining_devices == 0:
                logger.debug(f"Query finished but no devices connected for session {session_id}, scheduling cleanup")
                if live_session_store.has_session(session_id) and live_session_store.is_streaming(session_id):
                    # Only force-cleanup if still marked as streaming (abnormal state)
                    await live_session_store.cleanup_session(session_id)
                    logger.debug(f"Cleaned up stuck streaming session {session_id}")

    try:
        # Start ping task
        ping_task = asyncio.create_task(ping_loop(websocket, "chat", send_fn=send_json))

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
                        worktree_id = data.get("worktree_id")  # Optional: link session to existing worktree

                        if not prompt:
                            await send_json({"type": "error", "message": "Empty prompt"})
                            continue

                        # Create or get session
                        if not session_id:
                            # Validate worktree access if provided
                            if worktree_id:
                                worktree = database.get_worktree(worktree_id)
                                if not worktree:
                                    await send_json({"type": "error", "message": f"Worktree not found: {worktree_id}"})
                                    continue
                                # Get repository to verify project ownership
                                repo = database.get_git_repository(worktree["repository_id"])
                                if not repo:
                                    await send_json({"type": "error", "message": "Worktree repository not found"})
                                    continue
                                # Verify worktree belongs to the specified project
                                if project_id and repo["project_id"] != project_id:
                                    logger.warning(f"Authorization denied: worktree {worktree_id} belongs to project {repo['project_id']}, not {project_id}")
                                    await send_json({"type": "error", "message": "Worktree does not belong to this project"})
                                    continue
                                # If no project specified, use the worktree's project
                                if not project_id:
                                    project_id = repo["project_id"]

                            # Create new session, optionally linked to a worktree
                            session_id = str(uuid.uuid4())
                            # Generate title from the user's first message
                            title = prompt[:100].strip()
                            if len(prompt) > 100:
                                title = title.rsplit(' ', 1)[0] + "..."
                            # Get worktree info for persistent storage on session
                            wt_branch = worktree.get("branch_name") if worktree_id and worktree else None
                            wt_path = worktree.get("worktree_path") if worktree_id and worktree else None
                            database.create_session(
                                session_id=session_id,
                                profile_id=profile_id,
                                project_id=project_id,
                                api_user_id=api_user_id,
                                title=title,
                                worktree_id=worktree_id,  # Link to worktree if provided
                                worktree_branch=wt_branch,
                                worktree_path=wt_path
                            )
                            # Immediately notify frontend of new session_id so it persists on refresh
                            await send_json({
                                "type": "session_created",
                                "session_id": session_id,
                                "worktree_id": worktree_id  # Include worktree_id in response
                            })

                            # Notify other devices so their side panels show the new session
                            await sync_engine.broadcast_user_session_created(
                                user_id=user_id,
                                session_id=session_id,
                                source_device_id=device_id
                            )

                        # Register device with session if switching sessions
                        if session_id != current_session_id:
                            # Unregister from old session if any
                            if current_session_id:
                                await sync_engine.unregister_device(device_id, current_session_id)

                            # Register to new session
                            await sync_engine.register_device(device_id, session_id, websocket)
                            current_session_id = session_id

                        # Store user message with profile_id for per-query tracking
                        database.add_session_message(
                            session_id=session_id,
                            role="user",
                            content=prompt,
                            profile_id=profile_id
                        )

                        # Broadcast user message to other devices
                        await sync_engine.broadcast_message_added(
                            session_id=session_id,
                            message={
                                "role": "user",
                                "content": prompt,
                                "profile_id": profile_id
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
                        # Note: broadcast_stream_start is called inside run_query when actual
                        # streaming begins. This prevents stuck streaming state if query fails early.
                        query_task = asyncio.create_task(
                            run_query(prompt, session_id, profile_id, project_id, overrides, worktree_id)
                        )
                        _active_chat_sessions[session_id] = query_task

                    elif msg_type == "stop":
                        # Stop current query - use interrupt_session for proper SDK-level cancellation
                        session_id = data.get("session_id") or current_session_id
                        if session_id:
                            from app.core.query_engine import interrupt_session
                            # Call SDK's interrupt() - this signals the subprocess to stop.
                            # Don't cancel the task immediately: let the streaming loop continue
                            # so remaining in-flight tool calls are shown in the UI before
                            # the SDK fully stops and the "interrupted" event is sent.
                            interrupted = await interrupt_session(session_id)
                            logger.debug(f"Interrupt session {session_id}: {interrupted}")

                            # Safety timeout: force-cancel if SDK doesn't stop within 10s
                            if session_id in _active_chat_sessions:
                                task = _active_chat_sessions[session_id]
                                if not task.done():
                                    async def _safety_cancel(t, sid, timeout=10.0):
                                        try:
                                            await asyncio.sleep(timeout)
                                            if not t.done():
                                                logger.warning(f"SDK interrupt safety timeout for {sid}, force cancelling")
                                                t.cancel()
                                        except asyncio.CancelledError:
                                            pass
                                    asyncio.create_task(_safety_cancel(task, session_id))

                    elif msg_type == "interrupt_and_query":
                        # Interrupt current streaming and immediately send a new query
                        # This is the proper way to handle user input during streaming
                        session_id = data.get("session_id") or current_session_id
                        prompt = data.get("prompt", "").strip()
                        profile_id = data.get("profile", "claude-code")
                        project_id = data.get("project")
                        overrides = data.get("overrides")
                        worktree_id = data.get("worktree_id")

                        if not prompt:
                            await send_json({"type": "error", "message": "Empty prompt"})
                            continue

                        if not session_id:
                            await send_json({"type": "error", "message": "No active session"})
                            continue

                        from app.core.query_engine import interrupt_session

                        # Step 1: Interrupt the SDK-level streaming
                        interrupted = await interrupt_session(session_id)
                        logger.debug(f"Interrupt session {session_id} before new query: {interrupted}")

                        # Step 2: Cancel the asyncio task
                        # Timeout must exceed _SUBPROCESS_EXIT_GRACE_SECONDS (5s) to allow
                        # the subprocess to flush JSONL writes before the new query reads it.
                        # Without sufficient time, the new subprocess reads incomplete JSONL
                        # and loses conversation context.
                        if session_id in _active_chat_sessions:
                            task = _active_chat_sessions[session_id]
                            if not task.done():
                                task.cancel()
                                try:
                                    await asyncio.wait_for(task, timeout=8.0)
                                except (asyncio.CancelledError, asyncio.TimeoutError):
                                    pass

                        # Step 3: Store user message with profile_id for per-query tracking
                        database.add_session_message(
                            session_id=session_id,
                            role="user",
                            content=prompt,
                            profile_id=profile_id
                        )

                        # Step 4: Broadcast user message to other devices
                        await sync_engine.broadcast_message_added(
                            session_id=session_id,
                            message={
                                "role": "user",
                                "content": prompt,
                                "profile_id": profile_id
                            },
                            source_device_id=device_id
                        )

                        # Step 5: Start new query task
                        query_task = asyncio.create_task(
                            run_query(prompt, session_id, profile_id, project_id, overrides, worktree_id)
                        )
                        _active_chat_sessions[session_id] = query_task
                        logger.debug(f"Started new query after interrupt for session {session_id}")

                    elif msg_type == "load_session":
                        # Load a session's message history from JSONL file
                        session_id = data.get("session_id")
                        if session_id:
                            # Try direct lookup by AI Shuffle session ID first
                            session = database.get_session(session_id)

                            # If not found, try fallback lookup by SDK session ID (Claude Code UUID)
                            # This handles cases where old references might use the SDK session ID
                            if not session:
                                session = database.get_session_by_sdk_id(session_id)
                                if session:
                                    logger.debug(f"Session found via SDK session ID fallback: {session_id} -> {session['id']}")
                                    # Update session_id to the actual AI Shuffle ID for consistency
                                    session_id = session['id']

                            if session:
                                # Check if THIS DEVICE already has this session open in another tab
                                # This prevents duplicate tabs with the same session on the same device
                                # But allows DIFFERENT devices to view the same session simultaneously
                                # Also allows the SAME WebSocket to re-load its own session (e.g.,
                                # when user clicks a session that was already opened via tabs_sync)
                                if sync_engine.is_device_watching_session(device_id, session_id):
                                    existing_conn = sync_engine._connections.get(session_id, {}).get(device_id)
                                    if existing_conn and existing_conn.websocket is not websocket:
                                        # Different WebSocket = different tab trying to open same session
                                        await send_json({
                                            "type": "session_already_open",
                                            "session_id": session_id,
                                            "message": "This session is already open in another tab on this device."
                                        })
                                        continue
                                    # Same WebSocket re-loading — proceed normally

                                # Unregister from old session if switching
                                if current_session_id and current_session_id != session_id:
                                    await sync_engine.unregister_device(device_id, current_session_id)

                                # Register to new session
                                await sync_engine.register_device(device_id, session_id, websocket)
                                current_session_id = session_id

                                # Check live_session_store first for recent data.
                                # After streaming ends, the session stays in memory for 30s
                                # with clean, finalized messages. This avoids reading from JSONL
                                # which may have incomplete/partial snapshots (stop_reason: null).
                                sdk_session_id = session.get("sdk_session_id")
                                messages = []
                                live_data = live_session_store.get_session_dict(session_id)
                                if live_data and live_data.get("messages"):
                                    messages = live_data["messages"]
                                    logger.debug(f"Using live_session_store for session {session_id} with {len(messages)} messages")

                                # Get working dir from project if available
                                working_dir = str(settings.workspace_dir)
                                project_id = session.get("project_id")
                                if project_id:
                                    project = database.get_project(project_id)
                                    if project:
                                        working_dir = get_project_working_dir(project)

                                # Fall back to JSONL if live store didn't have data
                                if not messages and sdk_session_id:
                                    try:
                                        from app.core.jsonl_parser import parse_session_history

                                        messages, usage_data = parse_session_history(sdk_session_id, working_dir)
                                        logger.debug(f"Loaded {len(messages)} messages from JSONL for session {session_id}")

                                        # Merge profile_id using cached map (avoids loading all DB messages)
                                        from app.core.cost_utils import get_cached_profile_map
                                        db_profile_map = get_cached_profile_map(session_id)
                                        for msg in messages:
                                            if msg.get("role") == "user":
                                                content = msg.get("content", "")
                                                if content in db_profile_map:
                                                    msg["profile_id"] = db_profile_map[content]

                                        if usage_data:
                                            cache_creation = usage_data.get("cache_creation_tokens", 0)
                                            cache_read = usage_data.get("cache_read_tokens", 0)
                                            last_input = usage_data.get("last_input_tokens", 0)

                                            session["cache_creation_tokens"] = cache_creation
                                            session["cache_read_tokens"] = cache_read
                                            session["context_tokens"] = last_input + cache_creation + cache_read

                                            # Also update total tokens if DB doesn't have them
                                            if session.get("total_tokens_in", 0) == 0 and session.get("total_tokens_out", 0) == 0:
                                                session["total_tokens_in"] = usage_data.get("total_tokens_in", 0)
                                                session["total_tokens_out"] = usage_data.get("total_tokens_out", 0)

                                            logger.debug(f"Loaded context tokens for session {session_id}: context={session['context_tokens']}")
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
                                            "profile_id": m.get("profile_id"),
                                            "streaming": False
                                        })
                                    logger.debug(f"Loaded {len(messages)} messages from DB for session {session_id}")

                                # Attach cost/duration metadata to final assistant messages (cached, O(N))
                                from app.core.cost_utils import attach_cost_metadata
                                attach_cost_metadata(messages, session_id)

                                # Check if session is currently streaming (late-joining device)
                                is_streaming = sync_engine.is_session_streaming(session_id)
                                streaming_buffer = None
                                logger.debug(f"Session {session_id} streaming status: {is_streaming}")

                                # Also check for queries that are about to stream but haven't
                                # started yet (gap between session creation and first stream_start
                                # from the SDK). This ensures late-joining devices see the session
                                # as streaming even during the SDK initialization delay.
                                if not is_streaming:
                                    pending_task = _active_chat_sessions.get(session_id)
                                    if pending_task and not pending_task.done():
                                        is_streaming = True
                                        logger.debug(f"Session {session_id} has pending query task, marking as streaming")

                                # Validate streaming state - if marked streaming but no active task, it's stale
                                streaming_usage = None
                                active_block_type = None
                                if is_streaming:
                                    active_task = _active_chat_sessions.get(session_id)
                                    if active_task is None or active_task.done():
                                        # Stale streaming state - clean it up silently
                                        # Don't broadcast interrupted=True as the stream completed normally,
                                        # the client just missed the completion event while disconnected.
                                        # Use interrupted=False to avoid showing "[Stopped]" incorrectly.
                                        logger.warning(f"Session {session_id} marked as streaming but no active task - clearing stale state (stream completed normally)")
                                        await sync_engine.broadcast_stream_end(
                                            session_id=session_id,
                                            message_id="stale-cleanup",
                                            metadata={},
                                            interrupted=False,  # Changed from True - stream completed, not interrupted
                                            source_device_id=device_id  # Exclude self from broadcast
                                        )
                                        is_streaming = False
                                    else:
                                        # Use live_session_store for complete, authoritative state
                                        # This provides ALL messages with ALL fields (subagent, tool, etc.)
                                        # instead of DB messages (incomplete) + StreamingBuffer (lossy)
                                        live_data = live_session_store.get_session_dict(session_id)
                                        if live_data:
                                            messages = live_data["messages"]
                                            streaming_usage = live_data.get("usage")
                                            # No streamingBuffer needed - messages already include everything
                                            streaming_buffer = None
                                            logger.debug(f"Session {session_id} is streaming, using live_session_store with {len(messages)} messages, usage: {streaming_usage}")
                                        else:
                                            # Fallback to StreamingBuffer if live store doesn't have session
                                            streaming_buffer, streaming_usage, active_block_type = sync_engine.get_streaming_buffer_with_usage(session_id)
                                            logger.debug(f"Session {session_id} is streaming (live store miss), sending buffer with {len(streaming_buffer) if streaming_buffer else 0} messages, usage: {streaming_usage}, activeBlockType: {active_block_type}")

                                # Add worktree info if session is linked to a worktree
                                worktree_id = session.get("worktree_id")
                                if worktree_id:
                                    worktree = database.get_worktree(worktree_id)
                                    if worktree:
                                        session["worktree_id"] = worktree_id
                                        session["worktree_branch"] = worktree.get("branch_name")
                                        session["worktree_path"] = worktree.get("worktree_path")
                                        session["worktree_deleted"] = False
                                    else:
                                        # Worktree was deleted - use stored values from session columns
                                        session["worktree_id"] = None
                                        # worktree_branch and worktree_path are already in session from DB columns
                                        session["worktree_deleted"] = True
                                elif session.get("worktree_branch"):
                                    # Worktree was fully unlinked (ON DELETE SET NULL) but stored info remains
                                    session["worktree_deleted"] = True

                                # Build response with optional streamingUsage and activeBlockType for late-joiners
                                history_response = {
                                    "type": "history",
                                    "session_id": session_id,
                                    "session": session,
                                    "messages": messages,
                                    "isStreaming": is_streaming,
                                    "streamingBuffer": streaming_buffer
                                }
                                if streaming_usage:
                                    history_response["streamingUsage"] = streaming_usage
                                if active_block_type:
                                    history_response["activeBlockType"] = active_block_type
                                # Include pending permissions/questions for reconnecting clients
                                # When user disconnects and reconnects, they need to see any
                                # pending permission requests or user questions that are still
                                # waiting for a response from the frontend
                                pending_perms = permission_handler.get_pending_requests(session_id)
                                pending_qs = user_question_handler.get_pending_questions(session_id)
                                if pending_perms:
                                    history_response["pendingPermissions"] = pending_perms
                                if pending_qs:
                                    history_response["pendingQuestions"] = pending_qs
                                await send_json(history_response)

                                # Broadcast to other devices that this session was opened
                                await sync_engine.broadcast_session_opened(
                                    session_id=session_id,
                                    device_id=device_id,
                                    is_new=False  # Resuming existing session
                                )
                            else:
                                await send_json({"type": "session_not_found", "session_id": session_id})

                    elif msg_type == "close_session":
                        # Close/unload the current session
                        old_session_id = current_session_id
                        if old_session_id:
                            await sync_engine.unregister_device(device_id, old_session_id)
                            # Broadcast to other devices that this session was closed
                            await sync_engine.broadcast_session_closed(
                                session_id=old_session_id,
                                device_id=device_id
                            )

                            current_session_id = None
                            logger.debug(f"Device {device_id} closed session {old_session_id}")
                            # Try to send confirmation, but don't worry if WebSocket is already closed
                            # (common when frontend sends close_session right before disconnecting)
                            await send_json({"type": "session_closed", "session_id": old_session_id})

                    elif msg_type == "permission_response":
                        # Handle permission response from frontend
                        request_id = data.get("request_id")
                        decision = data.get("decision")  # "allow" or "deny"
                        remember = data.get("remember")  # "none", "session", or "profile"
                        pattern = data.get("pattern")  # Optional pattern for the rule

                        if request_id and decision and current_session_id:
                            result = await permission_handler.respond(
                                request_id=request_id,
                                session_id=current_session_id,
                                decision=decision,
                                remember=remember,
                                pattern=pattern,
                                broadcast_func=send_json
                            )
                            logger.debug(f"Permission response processed: {result}")

                            # Send confirmation back
                            await send_json({
                                "type": "permission_response_ack",
                                "request_id": request_id,
                                "result": result
                            })
                        else:
                            await send_json({
                                "type": "error",
                                "message": "Invalid permission response"
                            })

                    elif msg_type == "get_pending_permissions":
                        # Get pending permission requests for current session
                        if current_session_id:
                            pending = permission_handler.get_pending_requests(current_session_id)
                            await send_json({
                                "type": "pending_permissions",
                                "session_id": current_session_id,
                                "requests": pending
                            })

                    elif msg_type == "user_question_response":
                        # Handle user question response from frontend
                        request_id = data.get("request_id")
                        answers = data.get("answers", {})

                        if current_session_id and request_id and answers:
                            result = await user_question_handler.respond(
                                request_id=request_id,
                                session_id=current_session_id,
                                answers=answers
                            )
                            logger.debug(f"User question response processed: {request_id} -> {result}")

                            await send_json({
                                "type": "user_question_response_ack",
                                "request_id": request_id,
                                "success": result
                            })
                        else:
                            await send_json({
                                "type": "error",
                                "message": "Invalid user question response"
                            })

                    elif msg_type == "get_pending_questions":
                        # Get pending user questions for current session
                        if current_session_id:
                            pending = user_question_handler.get_pending_questions(current_session_id)
                            await send_json({
                                "type": "pending_questions",
                                "session_id": current_session_id,
                                "questions": pending
                            })

                    elif msg_type == "pong":
                        pass  # Keep-alive response

                    elif msg_type == "tabs_update":
                        # Broadcast tab state update to other devices for this user
                        tabs_data = data.get("tabs_data", {})
                        await sync_engine.broadcast_tabs_updated(
                            user_id=user_id,
                            tabs_data=tabs_data,
                            source_device_id=device_id
                        )

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

            # NOTE: We intentionally do NOT cancel the query task when the websocket closes.
            # This allows streaming to continue for other devices even if the originating
            # device disconnects (e.g., page refresh, phone locked, network issue).
            # The query will complete naturally and broadcast stream_end to all devices.
            # If the user wants to stop the query, they should use the "stop" message explicitly.
            if query_task and not query_task.done():
                logger.debug("WebSocket closed but query task still running for session - letting it continue for other devices")

            # Unregister user device from tab sync
            await sync_engine.unregister_user_device(user_id, device_id, websocket)

            # Unregister device from sync engine, but only if still using this websocket
            # This prevents race conditions where a new connection was established
            # before this cleanup runs (e.g., page reload during streaming)
            if current_session_id:
                await sync_engine.unregister_device(device_id, current_session_id, websocket)
                logger.debug(f"Cleanup: unregister device {device_id} from session {current_session_id}")

                # Check if this was the last device for this session
                # If so, and no active query is running, cleanup the session resources
                remaining_devices = sync_engine.get_device_count(current_session_id)
                active_task = _active_chat_sessions.get(current_session_id)
                is_task_done = active_task is None or active_task.done()

                if remaining_devices == 0 and is_task_done:
                    # No more devices watching and no active query
                    # Only cleanup sessions still in streaming state (abnormal);
                    # ended sessions are handled by _delayed_session_cleanup
                    logger.debug(f"Last device disconnected from session {current_session_id}")
                    if live_session_store.has_session(current_session_id) and live_session_store.is_streaming(current_session_id):
                        await live_session_store.cleanup_session(current_session_id)
                        logger.debug(f"Cleaned up stuck streaming session {current_session_id}")

                    # Remove from active chat sessions tracking
                    _active_chat_sessions.pop(current_session_id, None)

                    # Clear streaming state in sync engine (no need to broadcast - no devices left)
                    sync_engine.clear_streaming_state(current_session_id)

                    # Cleanup browser session if one exists
                    try:
                        from app.mcp.browser_session import browser_session_manager
                        if browser_session_manager.has_session(current_session_id):
                            await browser_session_manager.close_session(current_session_id)
                            logger.info(f"Cleaned up browser session for {current_session_id}")
                    except Exception:
                        pass
                elif remaining_devices > 0:
                    logger.debug(f"Session {current_session_id} still has {remaining_devices} device(s) connected")
                elif not is_task_done:
                    logger.debug(f"Session {current_session_id} has active query, deferring cleanup")

    except WebSocketDisconnect:
        logger.debug("Chat WebSocket disconnected")

    except RuntimeError as e:
        # Handle "WebSocket is not connected" error that happens when we try to
        # receive after the connection was closed (e.g., after processing close_session)
        if "not connected" in str(e).lower():
            logger.debug("Chat WebSocket disconnected (connection closed)")
        else:
            logger.error(f"Chat WebSocket runtime error: {e}")

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
    auth_ok, _api_user = await authenticate_websocket(websocket, token)
    if not auth_ok:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    # Verify session exists
    session = database.get_session(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    # Accept connection
    await websocket.accept()
    logger.debug(f"WebSocket connected: device={device_id}, session={session_id}")

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
        logger.debug(f"WebSocket disconnected: device={device_id}, session={session_id}")

    except Exception as e:
        logger.error(f"WebSocket error for device={device_id}: {e}")

    finally:
        # Unregister device
        await sync_engine.unregister_device(device_id, session_id)

        # Check if this was the last device and cleanup if needed
        remaining_devices = sync_engine.get_device_count(session_id)
        active_task = _active_chat_sessions.get(session_id)
        is_task_done = active_task is None or active_task.done()

        if remaining_devices == 0 and is_task_done:
            logger.debug(f"Last device disconnected from legacy sync session {session_id}")

            if live_session_store.has_session(session_id) and live_session_store.is_streaming(session_id):
                await live_session_store.cleanup_session(session_id)

            _active_chat_sessions.pop(session_id, None)
            if sync_engine.is_session_streaming(session_id):
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id="legacy-cleanup",
                    metadata={},
                    interrupted=True,
                )


async def ping_loop(
    websocket: WebSocket,
    device_id: str,
    send_fn: Optional[Callable[[dict], Awaitable[bool]]] = None,
):
    """Send periodic pings to keep connection alive.

    When send_fn is provided (e.g. the chat endpoint's send_json), pings go
    through the shared lock/health check. Otherwise falls back to direct
    websocket.send_json.
    """
    try:
        while True:
            await asyncio.sleep(30)  # Ping every 30 seconds

            ping_msg = {"type": "ping", "timestamp": None}

            if send_fn:
                if not await send_fn(ping_msg):
                    break
            else:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
                try:
                    await websocket.send_json(ping_msg)
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
    # Authenticate before accepting
    auth_ok, _api_user = await authenticate_websocket(websocket, token)
    if not auth_ok:
        # Must accept before closing with a reason code
        await websocket.accept()
        await websocket.close(code=4001, reason="Authentication failed")
        return

    await websocket.accept()
    logger.debug(f"Global WebSocket connected: device={device_id}")

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
        logger.debug(f"Global WebSocket disconnected: device={device_id}")

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
    working_dir = str(settings.workspace_dir)
    project_id = session.get("project_id")
    if project_id:
        project = database.get_project(project_id)
        if project:
            working_dir = get_project_working_dir(project)

    logger.debug(f"CLI WebSocket connected for session {session_id}, sdk_session={sdk_session_id}")

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
        logger.debug(f"CLI WebSocket disconnected for session {session_id}")

    except Exception as e:
        logger.error(f"CLI WebSocket error for session {session_id}: {e}", exc_info=True)

    finally:
        # Clean up CLI bridge
        if cli_bridge:
            await cli_bridge.stop()
        if session_id in _active_cli_bridges:
            del _active_cli_bridges[session_id]
