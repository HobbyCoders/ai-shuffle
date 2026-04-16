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
from typing import Optional, Dict, Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.websockets import WebSocketState

from app.core.sync_engine import sync_engine
from app.core.live_session_store import live_session_store
from app.core.config import settings
from app.core.streaming_context import StreamingContext
from app.db import database
from app.db.async_database import run_sync
from app.db.base import WorktreeStatus
from app.core.webhook_service import dispatch_session_complete, dispatch_session_error
from app.core.connectors.manager import connector_manager
from app.core.project_utils import get_project_working_dir
from app.core.permission_handler import permission_handler
from app.core.user_question_handler import user_question_handler
from app.api.chat_ws_handler import WebSocketRateLimiter

# Import from split modules — these are the canonical definitions.
# Re-exported here so that existing ``from app.api.websocket import ...``
# statements throughout the codebase continue to work without changes.
from app.api.ws_auth import authenticate_websocket  # noqa: F401
from app.api.ws_cleanup import (  # noqa: F401
    _active_chat_sessions,
    _active_sessions_lock,
    _pending_cleanup_tasks,
    cancel_pending_cleanup,
    cleanup_session_handlers,
    schedule_delayed_cleanup,
)
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

    This is the ONLY streaming mechanism needed. Simple flow:
    1. Client connects (optionally provides device_id for multi-device sync)
    2. Client sends: {"type": "query", "prompt": "...", "session_id": "...", "profile": "..."}
    3. Server streams response chunks directly
    4. Server sends: {"type": "done", ...} when complete
    5. Server broadcasts streaming events to other connected devices via SyncEngine

    Message types FROM server:
    - history: Full message history for session (on connect or session switch)
      - Includes isStreaming for late-joining devices
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
    - Late-joining devices receive streaming state from LiveSessionStore
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

    # Per-connection rate limiter for inbound messages
    rate_limiter = WebSocketRateLimiter(
        max_messages=settings.ws_rate_limit_messages,
        window_seconds=settings.ws_rate_limit_window_seconds,
    )

    async def run_query(prompt: str, session_id: str, profile_id: str, project_id: Optional[str], overrides: Optional[Dict[str, Any]] = None, worktree_id: Optional[str] = None):
        """Execute query and stream results directly to WebSocket"""
        nonlocal current_session_id

        from app.core.query_engine import stream_to_websocket
        from app.core.jsonl_parser import parse_session_history
        from app.core.config import settings
        from app.api.ws_event_handlers import EventContext, MutableStreamState, dispatch_event
        import time

        # Generate a message ID for this streaming session
        message_id = str(uuid.uuid4())

        # Structured logging context for this streaming request
        ctx = StreamingContext(
            session_id=session_id,
            device_id=device_id,
            message_id=message_id,
            user_id=user_id,
        )

        # Event dispatch context and mutable state
        event_ctx = EventContext(
            ctx=ctx,
            session_id=session_id,
            message_id=message_id,
            user_id=user_id,
            project_id=project_id,
            worktree_id=worktree_id,
        )
        stream_state = MutableStreamState()

        # Unregister this device from SyncEngine while streaming
        # This prevents duplicate events (we get direct events via send_json)
        # If the device disconnects and reconnects, it will re-register and get sync events
        await sync_engine.unregister_device(device_id, session_id)
        ctx.debug(logger, "Unregistered device during streaming to prevent duplicates")

        # Initialize LiveSessionStore for server-authoritative state
        # IMPORTANT: Check live store first for complete messages from an interrupted session.
        # During interrupt_and_query, end_streaming() preserves messages in memory, but
        # the JSONL file may be incomplete (subprocess killed before flushing).
        # Using live store messages prevents context loss on interrupt transitions.
        existing_live = live_session_store.get_session(session_id)
        if existing_live and not existing_live.is_streaming and existing_live.messages:
            # Use complete messages from the interrupted session's live store
            initial_messages = [m.to_dict() for m in existing_live.messages]
            ctx.debug(logger, f"Using {len(initial_messages)} messages from live store (interrupted session)")
        else:
            # DB-first loading (fast path: indexed query + simple transform)
            from app.core.db_message_loader import load_messages_from_db

            initial_messages = []
            try:
                db_start = time.monotonic()
                db_messages, has_db = await run_sync(load_messages_from_db, session_id)
                db_elapsed = time.monotonic() - db_start
                if has_db:
                    initial_messages = db_messages
                    ctx.debug(logger, f"DB message load: {db_elapsed:.2f}s ({len(db_messages)} messages)")
            except Exception as e:
                ctx.warning(logger, f"DB message load failed, falling back to JSONL: {e}")

            # Fallback to JSONL for legacy sessions without DB messages
            if not initial_messages:
                session_data = await run_sync(database.get_session, session_id)
                if session_data:
                    sdk_session_id = session_data.get("sdk_session_id")
                    if sdk_session_id:
                        working_dir = str(settings.workspace_dir)
                        if project_id:
                            project = await run_sync(database.get_project, project_id)
                            if project:
                                working_dir = get_project_working_dir(project)
                        try:
                            jsonl_start = time.monotonic()
                            initial_messages, _usage = await run_sync(parse_session_history, sdk_session_id, working_dir)
                            jsonl_elapsed = time.monotonic() - jsonl_start
                            if jsonl_elapsed > 1.0:
                                ctx.warning(logger, f"JSONL fallback took {jsonl_elapsed:.1f}s ({len(initial_messages)} messages)")
                            else:
                                ctx.debug(logger, f"JSONL fallback: {jsonl_elapsed:.2f}s ({len(initial_messages)} messages)")
                        except Exception as e:
                            ctx.warning(logger, f"Failed to load history for live store: {e}")

        # Strip the current user message from DB-loaded initial messages.
        # The user message was already written to DB (above) before run_query starts,
        # so load_messages_from_db includes it. But add_user_message (below) re-adds
        # it with a proper live-store ID. Without this filter, the live store ends up
        # with two copies of the user message — one from DB load, one from add_user_message.
        # JSONL and live-store paths don't have this issue (JSONL hasn't been written yet,
        # and ended live-store messages predate the current prompt).
        if initial_messages:
            # Remove the last message if it matches the current prompt (DB-loaded duplicate).
            # The current user message is always the most recently inserted DB row, so it
            # will be the last element in the list.
            last = initial_messages[-1]
            if (
                last.get("role") == "user"
                and last.get("content") == prompt
            ):
                initial_messages = initial_messages[:-1]

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
                        "timestamp": asyncio.get_running_loop().time(),
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

        query_wall_start = time.monotonic()
        try:
            ctx.info(logger, f"Query start profile={profile_id} project={project_id}")
            if not await send_json({"type": "start", "session_id": session_id, "message_id": message_id}):
                ctx.warning(logger, "Connection lost before query could start")
                await live_session_store.cleanup_session(session_id)
                await cleanup_session_handlers(session_id)
                return  # Exit early - connection is dead

            # NOTE: Don't call start_assistant_message here!
            # Creating an empty text message before content arrives causes the "Claude [time]"
            # header to appear even when the first content is a thinking block.
            # Let append_text/append_thinking create messages lazily when content arrives.

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(send_heartbeats())

            ctx.debug(logger, f"Calling stream_to_websocket, api_user_id={api_user_id}")
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
                    ctx.debug(logger, f"Sending plugin_message to frontend: {event}")
                else:
                    ctx.debug(logger, f"Streaming event: {event_type}")

                # Send to this websocket - may fail if connection died
                # Important: Do NOT skip store updates if WebSocket fails!
                # The live_session_store must be updated regardless of WebSocket state
                # so that polling clients can receive updates.
                ws_connected = await send_json(event)
                if not ws_connected and not stream_state.connection_lost_logged:
                    stream_state.connection_lost_logged = True
                    ctx.debug(logger, "WebSocket disconnected during streaming, updates continue via sync_engine + polling")

                # Dispatch to handler — updates live_session_store and broadcasts via sync_engine
                await dispatch_event(event, event_ctx, stream_state)

                # --- Turn-boundary queue check ---
                # When a turn ends with stop_reason "tool_use", the SDK is about
                # to execute tools and start another API call.  If there are
                # queued messages waiting, interrupt now so the queued message is
                # sent at the turn boundary (matching Claude Code behaviour)
                # instead of waiting for the entire multi-turn query to finish.
                if event_type == "stream_message_delta":
                    _stop_reason = event.get("delta", {}).get("stop_reason")
                    if _stop_reason and _stop_reason != "end_turn":
                        _queued = await run_sync(database.get_queued_messages, session_id)
                        if _queued:
                            from app.core.query_engine import interrupt_session
                            ctx.debug(
                                logger,
                                f"Interrupting at turn boundary — "
                                f"{len(_queued)} queued message(s) waiting, "
                                f"stop_reason={_stop_reason}"
                            )
                            await interrupt_session(session_id)

            query_wall_elapsed = time.monotonic() - query_wall_start
            ctx.info(logger, f"Query completed in {query_wall_elapsed:.1f}s")

            # Dispatch notifications for session completion (non-blocking)
            import time as time_module
            duration = time_module.time() - stream_state.query_start_time
            final_metadata = stream_state.final_metadata
            session_data = await run_sync(database.get_session, session_id)
            try:
                # Legacy webhook dispatch
                asyncio.create_task(dispatch_session_complete(
                    session_id=session_id,
                    title=session_data.get("title") if session_data else None,
                    profile_id=profile_id,
                    total_cost=final_metadata.get("total_cost_usd", 0.0),
                    input_tokens=final_metadata.get("tokens_in", 0),
                    output_tokens=final_metadata.get("tokens_out", 0),
                    duration_seconds=duration
                ))
                # Connector dispatch
                asyncio.create_task(connector_manager.dispatch_event(
                    "session.complete",
                    {
                        "session_id": session_id,
                        "title": session_data.get("title") if session_data else None,
                        "profile_id": profile_id,
                        "total_cost_usd": final_metadata.get("total_cost_usd", 0.0),
                        "input_tokens": final_metadata.get("tokens_in", 0),
                        "output_tokens": final_metadata.get("tokens_out", 0),
                        "duration_seconds": duration,
                    }
                ))
            except Exception as webhook_error:
                ctx.warning(logger, f"Failed to dispatch session.complete notifications: {webhook_error}")

        except asyncio.CancelledError:
            await send_json({"type": "stopped", "session_id": session_id})
            query_wall_elapsed = time.monotonic() - query_wall_start
            ctx.info(logger, f"Query cancelled after {query_wall_elapsed:.1f}s")

            # End live session store with interrupted flag
            # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
            await live_session_store.end_streaming(
                session_id=session_id,
                metadata={},
                interrupted=True
            )

            # Schedule tracked delayed cleanup (cancellable if a new query starts)
            schedule_delayed_cleanup(session_id, delay_seconds=30)

            # Broadcast stream end with interrupted flag
            if stream_state.stream_started:
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id=message_id,
                    metadata={},
                    interrupted=True,
                    source_device_id=None
                )

        except Exception as e:
            query_wall_elapsed = time.monotonic() - query_wall_start
            ctx.error(logger, f"Query error after {query_wall_elapsed:.1f}s: {e}", exc_info=True)
            error_detail = str(e) if str(e) else type(e).__name__
            await send_json({"type": "error", "message": f"Error: {error_detail}"})

            # End live session store on error
            # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
            await live_session_store.end_streaming(
                session_id=session_id,
                metadata={"error": str(e)},
                interrupted=True
            )

            # Schedule tracked delayed cleanup (cancellable if a new query starts)
            schedule_delayed_cleanup(session_id, delay_seconds=30)

            # Broadcast stream end on error
            if stream_state.stream_started:
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id=message_id,
                    metadata={"error": error_detail},
                    interrupted=True,
                    source_device_id=None
                )

            # Dispatch notifications for session error (non-blocking)
            session_data = await run_sync(database.get_session, session_id)
            try:
                # Legacy webhook dispatch
                asyncio.create_task(dispatch_session_error(
                    session_id=session_id,
                    title=session_data.get("title") if session_data else None,
                    profile_id=profile_id,
                    error_message=str(e)
                ))
                # Connector dispatch
                asyncio.create_task(connector_manager.dispatch_event(
                    "session.error",
                    {
                        "session_id": session_id,
                        "title": session_data.get("title") if session_data else None,
                        "profile_id": profile_id,
                        "error_message": str(e),
                    }
                ))
            except Exception as webhook_error:
                ctx.warning(logger, f"Failed to dispatch session.error notifications: {webhook_error}")

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
                ctx.debug(logger, "Cleaning up live session store (finally block - streaming never ended)")
                await live_session_store.cleanup_session(session_id)
                await cleanup_session_handlers(session_id)

            # ALWAYS ensure streaming state is cleared, even if stream_started was never set
            # This prevents stuck streaming state after errors or early termination
            if sync_engine.is_session_streaming(session_id):
                if not stream_state.stream_started:
                    # Stream was marked but never actually started - clean up silently
                    ctx.debug(logger, "Cleaning up streaming state (never started)")
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
                    ctx.debug(logger, "Skipping re-register - newer connection already exists")
                else:
                    await sync_engine.register_device(device_id, session_id, websocket)
                    ctx.debug(logger, "Re-registered device after streaming complete")
            else:
                ctx.debug(logger, "Skipping re-register - websocket disconnected during streaming")

            # Clean up task reference only if it's still our task (avoid TOCTOU race
            # where a different device started a new query for the same session)
            current_task = asyncio.current_task()
            async with _active_sessions_lock:
                if _active_chat_sessions.get(session_id) is current_task:
                    del _active_chat_sessions[session_id]
                ctx.info(logger, f"Query teardown: active_sessions={len(_active_chat_sessions)} pending_cleanups={len(_pending_cleanup_tasks)}")

            # If no devices are connected after query finished, schedule cleanup
            # (don't destroy immediately — user may reopen within 30s)
            remaining_devices = sync_engine.get_device_count(session_id)
            if remaining_devices == 0:
                ctx.debug(logger, "Query finished but no devices connected, scheduling cleanup")
                if live_session_store.has_session(session_id) and live_session_store.is_streaming(session_id):
                    # Only force-cleanup if still marked as streaming (abnormal state)
                    await live_session_store.cleanup_session(session_id)
                    await cleanup_session_handlers(session_id)
                    ctx.debug(logger, "Cleaned up stuck streaming session")

            # Auto-process next queued message (server-side, no browser needed).
            # Skip if another task already took over for this session (e.g.,
            # interrupt_and_query stored a new task while we were finishing up).
            async with _active_sessions_lock:
                successor = _active_chat_sessions.get(session_id)
            if successor is None or (isinstance(successor, asyncio.Task) and successor.done()):
                await asyncio.sleep(0.5)
                try:
                    msg = await run_sync(database.pop_next_queued_message, session_id)
                    if msg:
                        q_content = msg["content"]
                        q_profile = msg.get("profile_id") or "claude-code"
                        q_project = msg.get("project_id")
                        logger.info(f"[Queue] Auto-processing queued message for {session_id}: {q_content[:80]}...")

                        # Broadcast updated queue
                        queue = await run_sync(database.get_queued_messages, session_id)
                        await sync_engine.broadcast_queue_updated(session_id, queue)

                        # Store user message in DB
                        await run_sync(
                            database.add_session_message,
                            session_id=session_id,
                            role="user",
                            content=q_content,
                            profile_id=q_profile,
                        )

                        # Broadcast user message to all devices
                        await sync_engine.broadcast_message_added(
                            session_id=session_id,
                            message={"role": "user", "content": q_content, "profile_id": q_profile},
                            source_device_id=None,
                        )

                        # Look up worktree_id from session
                        q_session = await run_sync(database.get_session, session_id)
                        q_worktree = q_session.get("worktree_id") if q_session else None

                        # Start the queued query
                        q_task = asyncio.create_task(
                            run_query(q_content, session_id, q_profile, q_project, None, q_worktree)
                        )
                        async with _active_sessions_lock:
                            _active_chat_sessions[session_id] = q_task
                except Exception as queue_err:
                    logger.warning(f"[Queue] Failed to auto-process next queued message: {queue_err}")

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

                    # Rate-limit user-initiated messages (skip keepalive pongs)
                    if msg_type != "pong" and not rate_limiter.check():
                        logger.warning(
                            f"WebSocket rate limited: device={device_id}, "
                            f"msg_type={msg_type}"
                        )
                        await send_json({
                            "type": "error",
                            "error": "Rate limited: too many messages. Please slow down.",
                        })
                        continue

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
                                worktree = await run_sync(database.get_worktree, worktree_id)
                                if not worktree:
                                    await send_json({"type": "error", "message": f"Worktree not found: {worktree_id}"})
                                    continue
                                # Get repository to verify project ownership
                                repo = await run_sync(database.get_git_repository, worktree["repository_id"])
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
                            await run_sync(
                                database.create_session,
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
                        await run_sync(
                            database.add_session_message,
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
                        async with _active_sessions_lock:
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
                        async with _active_sessions_lock:
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
                            async with _active_sessions_lock:
                                task = _active_chat_sessions.get(session_id)
                            if task and not task.done():
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

                        # Step 2: Wait for the streaming task to finish naturally.
                        # The SDK interrupt() causes receive_response() to end with a
                        # ResultMessage.  We MUST let receive_response() drain fully
                        # before sending a new query — cancelling the task prematurely
                        # leaves unread messages in the subprocess pipe, and the next
                        # receive_response() reads the stale ResultMessage and completes
                        # instantly (0 events / empty response).
                        #
                        # Use asyncio.wait() (not wait_for/cancel) because wait_for
                        # cancels the inner task on timeout, which is exactly what we
                        # want to avoid until we're sure the drain didn't happen.
                        async with _active_sessions_lock:
                            task = _active_chat_sessions.get(session_id)
                        if task and not task.done():
                            done, pending = await asyncio.wait({task}, timeout=8.0)
                            if pending:
                                # Interrupt didn't cause natural completion — force cancel
                                logger.warning(f"Interrupt drain timed out for {session_id}, force cancelling")
                                task.cancel()
                                try:
                                    await task
                                except (asyncio.CancelledError, Exception):
                                    pass

                        # Step 3: Store user message with profile_id for per-query tracking
                        await run_sync(
                            database.add_session_message,
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
                        async with _active_sessions_lock:
                            _active_chat_sessions[session_id] = query_task
                        logger.debug(f"Started new query after interrupt for session {session_id}")

                    elif msg_type == "load_session":
                        # Load a session's message history from JSONL file
                        session_id = data.get("session_id")
                        if session_id:
                            # Try direct lookup by AI Shuffle session ID first
                            session = await run_sync(database.get_session, session_id)

                            # If not found, try fallback lookup by SDK session ID (Claude Code UUID)
                            # This handles cases where old references might use the SDK session ID
                            if not session:
                                session = await run_sync(database.get_session_by_sdk_id, session_id)
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
                                # The force_jsonl flag bypasses this cache (used after rewind to
                                # ensure we read the truncated JSONL, not stale in-memory data).
                                sdk_session_id = session.get("sdk_session_id")
                                messages = []
                                force_jsonl = data.get("force_jsonl", False)
                                if not force_jsonl:
                                    live_data = live_session_store.get_session_dict(session_id)
                                    if live_data and live_data.get("messages"):
                                        messages = live_data["messages"]
                                        logger.debug(f"Using live_session_store for session {session_id} with {len(messages)} messages")

                                # Get working dir from project if available
                                working_dir = str(settings.workspace_dir)
                                project_id = session.get("project_id")
                                if project_id:
                                    project = await run_sync(database.get_project, project_id)
                                    if project:
                                        working_dir = get_project_working_dir(project)

                                # DB-first loading (fast path: ~10-60ms)
                                if not messages:
                                    from app.core.db_message_loader import load_messages_from_db
                                    try:
                                        db_msgs, has_db = await run_sync(load_messages_from_db, session_id)
                                        if has_db:
                                            messages = db_msgs
                                            logger.debug(f"Loaded {len(messages)} messages from DB for session {session_id}")
                                    except Exception as e:
                                        logger.warning(f"DB message load failed for {session_id}: {e}")

                                # Fallback: JSONL parsing (legacy sessions)
                                if not messages and sdk_session_id:
                                    try:
                                        from app.core.jsonl_parser import parse_session_history

                                        messages, usage_data = await run_sync(parse_session_history, sdk_session_id, working_dir)
                                        logger.debug(f"JSONL fallback: {len(messages)} messages for session {session_id}")

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

                                # Attach cost/duration metadata to final assistant messages (cached, O(N))
                                from app.core.cost_utils import attach_cost_metadata
                                attach_cost_metadata(messages, session_id)

                                # Check if session is currently streaming (late-joining device)
                                is_streaming = sync_engine.is_session_streaming(session_id)
                                logger.debug(f"Session {session_id} streaming status: {is_streaming}")

                                # Also check for queries that are about to stream but haven't
                                # started yet (gap between session creation and first stream_start
                                # from the SDK). This ensures late-joining devices see the session
                                # as streaming even during the SDK initialization delay.
                                if not is_streaming:
                                    async with _active_sessions_lock:
                                        pending_task = _active_chat_sessions.get(session_id)
                                    if pending_task and not pending_task.done():
                                        is_streaming = True
                                        logger.debug(f"Session {session_id} has pending query task, marking as streaming")

                                # Validate streaming state - if marked streaming but no active task, it's stale
                                streaming_usage = None
                                active_block_type = None
                                if is_streaming:
                                    async with _active_sessions_lock:
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
                                        # Use live_session_store for complete, authoritative state.
                                        # This provides ALL messages with ALL fields (subagent, tool, etc.)
                                        live_data = live_session_store.get_session_dict(session_id)
                                        if live_data:
                                            messages = live_data["messages"]
                                            streaming_usage = live_data.get("usage")
                                            active_block_type = live_data.get("active_block_type")
                                            logger.debug(f"Session {session_id} is streaming, using live_session_store with {len(messages)} messages, usage: {streaming_usage}")
                                        else:
                                            # LiveSessionStore should always have data for streaming sessions now.
                                            # If not, log a warning and return empty state.
                                            logger.warning(f"[WS] LiveSessionStore missing for streaming session {session_id}")
                                            messages = []
                                            streaming_usage = None
                                            active_block_type = None

                                # Add worktree info if session is linked to a worktree
                                worktree_id = session.get("worktree_id")
                                if worktree_id:
                                    worktree = await run_sync(database.get_worktree, worktree_id)
                                    if worktree and worktree.get("status") == WorktreeStatus.ACTIVE:
                                        session["worktree_id"] = worktree_id
                                        session["worktree_branch"] = worktree.get("branch_name")
                                        session["worktree_path"] = worktree.get("worktree_path")
                                        session["worktree_deleted"] = False
                                    elif worktree and worktree.get("status") == WorktreeStatus.COMPLETED:
                                        session["worktree_id"] = worktree_id
                                        session["worktree_branch"] = worktree.get("branch_name")
                                        session["worktree_path"] = worktree.get("worktree_path")
                                        session["worktree_deleted"] = True
                                    else:
                                        session["worktree_id"] = None
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

                    elif msg_type == "watch_session":
                        # Register this device to receive sync broadcasts for a session.
                        session_id = data.get("session_id")
                        logger.debug(f"[watch_session] device={device_id} session_id={session_id}")
                        if session_id:
                            # Check if this device_id is already registered from a different
                            # WebSocket (e.g., another browser tab sharing localStorage device_id,
                            # or the same device reconnecting after a page refresh).
                            if sync_engine.is_device_watching_session(device_id, session_id):
                                existing_conn = sync_engine._connections.get(session_id, {}).get(device_id)
                                if existing_conn and existing_conn.websocket is not websocket:
                                    # Check if the old WS is still alive. If it's dead/closing,
                                    # this is a reconnection (e.g., page refresh) — let the new
                                    # WS take over. If it's alive, this is likely a duplicate tab
                                    # sharing the same device_id — skip to avoid a reconnect loop.
                                    old_ws_alive = existing_conn.websocket.client_state == WebSocketState.CONNECTED
                                    if old_ws_alive:
                                        logger.debug(f"Device {device_id} already watching session {session_id} from another live WS, skipping")
                                        current_session_id = session_id
                                        continue
                                    else:
                                        logger.debug(f"Device {device_id} reconnecting (old WS state={existing_conn.websocket.client_state}), replacing")
                                # Same WebSocket re-watching — proceed normally

                            # Unregister from old session if switching
                            if current_session_id and current_session_id != session_id:
                                await sync_engine.unregister_device(device_id, current_session_id)

                            await sync_engine.register_device(device_id, session_id, websocket)
                            current_session_id = session_id
                            logger.debug(f"Device {device_id} watching session {session_id}")

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

                    elif msg_type == "queue_message":
                        # Queue a message to send after the current stream completes
                        content = (data.get("content") or "").strip()
                        sid = data.get("session_id") or current_session_id
                        qp_profile = data.get("profile")
                        qp_project = data.get("project")

                        if not content:
                            await send_json({"type": "error", "message": "Empty queue content"})
                            continue
                        if not sid:
                            await send_json({"type": "error", "message": "No session for queue"})
                            continue

                        await run_sync(
                            database.add_queued_message,
                            session_id=sid,
                            content=content,
                            profile_id=qp_profile,
                            project_id=qp_project,
                        )
                        queue = await run_sync(database.get_queued_messages, sid)
                        # Broadcast to other devices via SyncEngine
                        await sync_engine.broadcast_queue_updated(sid, queue)
                        # Direct send to this device (unregistered from SyncEngine during streaming)
                        await send_json({
                            "type": "sync",
                            "event_type": "queue_updated",
                            "session_id": sid,
                            "data": {"queued_messages": queue},
                        })

                    elif msg_type == "remove_queued_message":
                        qm_id = data.get("message_id")
                        sid = data.get("session_id") or current_session_id
                        if qm_id:
                            await run_sync(database.remove_queued_message, qm_id)
                            if sid:
                                queue = await run_sync(database.get_queued_messages, sid)
                                await sync_engine.broadcast_queue_updated(sid, queue)
                                await send_json({
                                    "type": "sync",
                                    "event_type": "queue_updated",
                                    "session_id": sid,
                                    "data": {"queued_messages": queue},
                                })

                    elif msg_type == "clear_queued_messages":
                        sid = data.get("session_id") or current_session_id
                        if sid:
                            await run_sync(database.clear_queued_messages, sid)
                            await sync_engine.broadcast_queue_updated(sid, [])
                            await send_json({
                                "type": "sync",
                                "event_type": "queue_updated",
                                "session_id": sid,
                                "data": {"queued_messages": []},
                            })

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

                # Check if this was the last device for this session.
                # Hold a single lock across the entire check-and-cleanup to prevent
                # a TOCTOU race where a new device connects or a new query starts
                # between the condition check and the cleanup action.
                async with _active_sessions_lock:
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
                            await cleanup_session_handlers(current_session_id)
                            logger.debug(f"Cleaned up stuck streaming session {current_session_id}")

                        _active_chat_sessions.pop(current_session_id, None)

                        # Clear streaming state in sync engine (no need to broadcast - no devices left)
                        await sync_engine.clear_streaming_state(current_session_id)

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
