"""
ChatWebSocketHandler — class-based handler for the primary chat WebSocket.

Extracted from the nested-closure `chat_websocket` function in websocket.py.
All instance state replaces former `nonlocal` variables; nested functions
become methods.  Behaviour is identical to the original implementation.
"""

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi.websockets import WebSocketState
from starlette.websockets import WebSocket

from app.core.config import settings
from app.core.connectors.manager import connector_manager
from app.core.live_session_store import live_session_store
from app.core.message_resolver import message_resolver, resolve_working_dir
from app.core.permission_handler import permission_handler
from app.core.streaming_context import StreamingContext
from app.core.sync_engine import sync_engine
from app.core.user_question_handler import user_question_handler
from app.core.webhook_service import dispatch_session_complete, dispatch_session_error
from app.db import database

logger = logging.getLogger(__name__)


class ChatWebSocketHandler:
    """Handles a single WebSocket chat connection lifecycle."""

    def __init__(
        self,
        websocket: WebSocket,
        device_id: str,
        user_id: str,
        api_user_id: Optional[str],
    ):
        self.websocket = websocket
        self.device_id = device_id
        self.user_id = user_id
        self.api_user_id = api_user_id

        # Mutable connection state (formerly nonlocal variables)
        self.current_session_id: Optional[str] = None
        self.query_task: Optional[asyncio.Task] = None
        self.connection_healthy: bool = True

        # Lock to serialize WebSocket sends — prevents concurrent sends from
        # ping_loop, heartbeat, and the main message handler racing each other
        self.send_lock = asyncio.Lock()

        # Message-type dispatch table
        self._handlers: Dict[str, Callable[[dict], Any]] = {
            "query": self.handle_query,
            "stop": self.handle_stop,
            "interrupt_and_query": self.handle_interrupt_and_query,
            "load_session": self.handle_load_session,
            "close_session": self.handle_close_session,
            "permission_response": self.handle_permission_response,
            "get_pending_permissions": self.handle_get_pending_permissions,
            "user_question_response": self.handle_user_question_response,
            "get_pending_questions": self.handle_get_pending_questions,
            "tabs_update": self.handle_tabs_update,
            "pong": self._handle_pong,
        }

    # ------------------------------------------------------------------
    # send_json — safe JSON send with connection check
    # ------------------------------------------------------------------

    async def send_json(self, data: dict) -> bool:
        """
        Safe JSON send with connection check.

        Returns True if send succeeded, False if connection is dead.
        Callers should check return value and abort operations if False.

        Uses a lock to make the state-check + send atomic, preventing
        TOCTOU races where a close frame arrives between the check and send.
        """
        # Fast path: skip lock acquisition if already known dead
        if not self.connection_healthy:
            return False

        async with self.send_lock:
            if not self.connection_healthy:
                return False

            if self.websocket.client_state != WebSocketState.CONNECTED:
                self.connection_healthy = False
                logger.debug(f"WebSocket disconnected, cannot send message type: {data.get('type')}")
                return False

            try:
                await self.websocket.send_json(data)
                return True
            except Exception as e:
                self.connection_healthy = False
                logger.warning(f"Failed to send WebSocket message (type={data.get('type')}): {e}")
                return False

    # ------------------------------------------------------------------
    # run — main connection loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main connection loop — replaces the body of chat_websocket."""
        from app.api.websocket import ping_loop

        # Register user device for tab sync
        await sync_engine.register_user_device(self.user_id, self.device_id, self.websocket)

        # Always send streaming sessions snapshot on connect so the client can
        # reconcile stale entries from before a disconnect.  Previously this was
        # only sent when there *were* streaming sessions, which meant stale
        # entries from a missed `done` event would persist indefinitely.
        user_streaming = sync_engine.get_user_streaming_sessions(self.user_id)
        await self.send_json({
            "type": "user_streaming_sessions",
            "sessions": user_streaming or [],
            "session_ids": [s["session_id"] for s in user_streaming] if user_streaming else [],
        })

        try:
            # Start ping task
            ping_task = asyncio.create_task(ping_loop(self.websocket, "chat", send_fn=self.send_json))

            try:
                while True:
                    try:
                        data = await asyncio.wait_for(
                            self.websocket.receive_json(),
                            timeout=60.0
                        )

                        msg_type = data.get("type")
                        handler = self._handlers.get(msg_type)
                        if handler is not None:
                            await handler(data)

                    except asyncio.TimeoutError:
                        if self.websocket.client_state != WebSocketState.CONNECTED:
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
                if self.query_task and not self.query_task.done():
                    logger.debug("WebSocket closed but query task still running for session - letting it continue for other devices")

                # Unregister user device from tab sync
                await sync_engine.unregister_user_device(self.user_id, self.device_id, self.websocket)

                # Unregister device from sync engine, but only if still using this websocket
                # This prevents race conditions where a new connection was established
                # before this cleanup runs (e.g., page reload during streaming)
                if self.current_session_id:
                    await sync_engine.unregister_device(self.device_id, self.current_session_id, self.websocket)
                    logger.debug(f"Cleanup: unregister device {self.device_id} from session {self.current_session_id}")

                    # Check if this was the last device for this session
                    # If so, and no active query is running, cleanup the session resources
                    from app.api.websocket import _active_chat_sessions

                    remaining_devices = sync_engine.get_device_count(self.current_session_id)
                    active_task = _active_chat_sessions.get(self.current_session_id)
                    is_task_done = active_task is None or active_task.done()

                    if remaining_devices == 0 and is_task_done:
                        # No more devices watching and no active query
                        # Only cleanup sessions still in streaming state (abnormal);
                        # ended sessions are handled by _delayed_session_cleanup
                        logger.debug(f"Last device disconnected from session {self.current_session_id}")
                        if live_session_store.has_session(self.current_session_id) and live_session_store.is_streaming(self.current_session_id):
                            await live_session_store.cleanup_session(self.current_session_id)
                            logger.debug(f"Cleaned up stuck streaming session {self.current_session_id}")

                        # Remove from active chat sessions tracking
                        _active_chat_sessions.pop(self.current_session_id, None)

                        # Clear streaming state in sync engine (no need to broadcast - no devices left)
                        sync_engine.clear_streaming_state(self.current_session_id)

                        # Cleanup browser session if one exists
                        try:
                            from app.mcp.browser_session import browser_session_manager
                            if browser_session_manager.has_session(self.current_session_id):
                                await browser_session_manager.close_session(self.current_session_id)
                                logger.info(f"Cleaned up browser session for {self.current_session_id}")
                        except Exception:
                            pass
                    elif remaining_devices > 0:
                        logger.debug(f"Session {self.current_session_id} still has {remaining_devices} device(s) connected")
                    elif not is_task_done:
                        logger.debug(f"Session {self.current_session_id} has active query, deferring cleanup")

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

    # ------------------------------------------------------------------
    # handle_query
    # ------------------------------------------------------------------

    async def handle_query(self, data: dict) -> None:
        """Handle 'query' message type."""
        from app.api.websocket import _active_chat_sessions

        prompt = data.get("prompt", "").strip()
        session_id = data.get("session_id")
        profile_id = data.get("profile", "claude-code")
        project_id = data.get("project")
        overrides = data.get("overrides")  # Optional: {model, permission_mode}
        worktree_id = data.get("worktree_id")  # Optional: link session to existing worktree

        if not prompt:
            await self.send_json({"type": "error", "message": "Empty prompt"})
            return

        # Create or get session
        if not session_id:
            # Validate worktree access if provided
            worktree = None
            if worktree_id:
                worktree = database.get_worktree(worktree_id)
                if not worktree:
                    await self.send_json({"type": "error", "message": f"Worktree not found: {worktree_id}"})
                    return
                # Get repository to verify project ownership
                repo = database.get_git_repository(worktree["repository_id"])
                if not repo:
                    await self.send_json({"type": "error", "message": "Worktree repository not found"})
                    return
                # Verify worktree belongs to the specified project
                if project_id and repo["project_id"] != project_id:
                    logger.warning(f"Authorization denied: worktree {worktree_id} belongs to project {repo['project_id']}, not {project_id}")
                    await self.send_json({"type": "error", "message": "Worktree does not belong to this project"})
                    return
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
                api_user_id=self.api_user_id,
                title=title,
                worktree_id=worktree_id,  # Link to worktree if provided
                worktree_branch=wt_branch,
                worktree_path=wt_path
            )
            # Immediately notify frontend of new session_id so it persists on refresh
            await self.send_json({
                "type": "session_created",
                "session_id": session_id,
                "worktree_id": worktree_id  # Include worktree_id in response
            })

            # Notify other devices so their side panels show the new session
            await sync_engine.broadcast_user_session_created(
                user_id=self.user_id,
                session_id=session_id,
                source_device_id=self.device_id
            )

        # Register device with session if switching sessions
        if session_id != self.current_session_id:
            # Unregister from old session if any
            if self.current_session_id:
                await sync_engine.unregister_device(self.device_id, self.current_session_id)

            # Register to new session
            await sync_engine.register_device(self.device_id, session_id, self.websocket)
            self.current_session_id = session_id

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
            source_device_id=self.device_id  # Exclude the sender
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
        self.query_task = asyncio.create_task(
            self.run_query(prompt, session_id, profile_id, project_id, overrides, worktree_id)
        )
        _active_chat_sessions[session_id] = self.query_task

    # ------------------------------------------------------------------
    # handle_stop
    # ------------------------------------------------------------------

    async def handle_stop(self, data: dict) -> None:
        """Handle 'stop' message type."""
        from app.api.websocket import _active_chat_sessions

        session_id = data.get("session_id") or self.current_session_id
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
                    _t = asyncio.create_task(_safety_cancel(task, session_id))
                    _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # ------------------------------------------------------------------
    # handle_interrupt_and_query
    # ------------------------------------------------------------------

    async def handle_interrupt_and_query(self, data: dict) -> None:
        """Handle 'interrupt_and_query' message type."""
        from app.api.websocket import _active_chat_sessions

        session_id = data.get("session_id") or self.current_session_id
        prompt = data.get("prompt", "").strip()
        profile_id = data.get("profile", "claude-code")
        project_id = data.get("project")
        overrides = data.get("overrides")
        worktree_id = data.get("worktree_id")

        if not prompt:
            await self.send_json({"type": "error", "message": "Empty prompt"})
            return

        if not session_id:
            await self.send_json({"type": "error", "message": "No active session"})
            return

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
            source_device_id=self.device_id
        )

        # Step 5: Start new query task
        self.query_task = asyncio.create_task(
            self.run_query(prompt, session_id, profile_id, project_id, overrides, worktree_id)
        )
        _active_chat_sessions[session_id] = self.query_task
        logger.debug(f"Started new query after interrupt for session {session_id}")

    # ------------------------------------------------------------------
    # handle_load_session
    # ------------------------------------------------------------------

    async def handle_load_session(self, data: dict) -> None:
        """Handle 'load_session' message type."""
        from app.api.websocket import _active_chat_sessions

        session_id = data.get("session_id")
        if not session_id:
            return

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

        if not session:
            await self.send_json({"type": "session_not_found", "session_id": session_id})
            return

        # Check if THIS DEVICE already has this session open in another tab
        # This prevents duplicate tabs with the same session on the same device
        # But allows DIFFERENT devices to view the same session simultaneously
        # Also allows the SAME WebSocket to re-load its own session (e.g.,
        # when user clicks a session that was already opened via tabs_sync)
        if sync_engine.is_device_watching_session(self.device_id, session_id):
            existing_conn = sync_engine._connections.get(session_id, {}).get(self.device_id)
            if existing_conn and existing_conn.websocket is not self.websocket:
                # Different WebSocket = different tab trying to open same session
                await self.send_json({
                    "type": "session_already_open",
                    "session_id": session_id,
                    "message": "This session is already open in another tab on this device."
                })
                return
            # Same WebSocket re-loading — proceed normally

        # Unregister from old session if switching
        if self.current_session_id and self.current_session_id != session_id:
            await sync_engine.unregister_device(self.device_id, self.current_session_id)

        # Register to new session
        await sync_engine.register_device(self.device_id, session_id, self.websocket)
        self.current_session_id = session_id

        # Unified message resolution: LiveSessionStore -> JSONL -> SQLite
        # The force_jsonl flag bypasses the live store cache (used after
        # rewind to ensure we read the truncated JSONL, not stale data).
        sdk_session_id = session.get("sdk_session_id")
        force_jsonl = data.get("force_jsonl", False)
        working_dir = resolve_working_dir(dict(session))

        resolved = message_resolver.resolve(
            session_id, sdk_session_id, working_dir,
            force_jsonl=force_jsonl,
        )
        messages = resolved.messages
        logger.debug(f"Loaded {len(messages)} messages from {resolved.source} for session {session_id}")

        # Post-processing: merge profile_id for JSONL-sourced messages
        # (DB and live_store messages already include profile_id)
        if resolved.source == "jsonl":
            from app.core.cost_utils import get_cached_profile_map
            db_profile_map = get_cached_profile_map(session_id)
            for msg in messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if content in db_profile_map:
                        msg["profile_id"] = db_profile_map[content]

        # Enrich session with usage/token data from resolver
        if resolved.usage:
            cache_creation = resolved.usage.get("cache_creation_tokens", 0)
            cache_read = resolved.usage.get("cache_read_tokens", 0)
            last_input = resolved.usage.get("last_input_tokens", 0)

            session["cache_creation_tokens"] = cache_creation
            session["cache_read_tokens"] = cache_read
            session["context_tokens"] = last_input + cache_creation + cache_read

            # Also update total tokens if DB doesn't have them
            if session.get("total_tokens_in", 0) == 0 and session.get("total_tokens_out", 0) == 0:
                session["total_tokens_in"] = resolved.usage.get("total_tokens_in", 0)
                session["total_tokens_out"] = resolved.usage.get("total_tokens_out", 0)

            logger.debug(f"Loaded context tokens for session {session_id}: context={session['context_tokens']}")

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
                    source_device_id=self.device_id  # Exclude self from broadcast
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
        await self.send_json(history_response)

        # Broadcast to other devices that this session was opened
        await sync_engine.broadcast_session_opened(
            session_id=session_id,
            device_id=self.device_id,
            is_new=False  # Resuming existing session
        )

    # ------------------------------------------------------------------
    # handle_close_session
    # ------------------------------------------------------------------

    async def handle_close_session(self, data: dict) -> None:
        """Handle 'close_session' message type."""
        old_session_id = self.current_session_id
        if old_session_id:
            await sync_engine.unregister_device(self.device_id, old_session_id)
            # Broadcast to other devices that this session was closed
            await sync_engine.broadcast_session_closed(
                session_id=old_session_id,
                device_id=self.device_id
            )

            self.current_session_id = None
            logger.debug(f"Device {self.device_id} closed session {old_session_id}")
            # Try to send confirmation, but don't worry if WebSocket is already closed
            # (common when frontend sends close_session right before disconnecting)
            await self.send_json({"type": "session_closed", "session_id": old_session_id})

    # ------------------------------------------------------------------
    # handle_permission_response
    # ------------------------------------------------------------------

    async def handle_permission_response(self, data: dict) -> None:
        """Handle 'permission_response' message type."""
        request_id = data.get("request_id")
        decision = data.get("decision")  # "allow" or "deny"
        remember = data.get("remember")  # "none", "session", or "profile"
        pattern = data.get("pattern")  # Optional pattern for the rule

        if request_id and decision and self.current_session_id:
            result = await permission_handler.respond(
                request_id=request_id,
                session_id=self.current_session_id,
                decision=decision,
                remember=remember,
                pattern=pattern,
                broadcast_func=self.send_json
            )
            logger.debug(f"Permission response processed: {result}")

            # Send confirmation back
            await self.send_json({
                "type": "permission_response_ack",
                "request_id": request_id,
                "result": result
            })
        else:
            await self.send_json({
                "type": "error",
                "message": "Invalid permission response"
            })

    # ------------------------------------------------------------------
    # handle_get_pending_permissions
    # ------------------------------------------------------------------

    async def handle_get_pending_permissions(self, data: dict) -> None:
        """Handle 'get_pending_permissions' message type."""
        if self.current_session_id:
            pending = permission_handler.get_pending_requests(self.current_session_id)
            await self.send_json({
                "type": "pending_permissions",
                "session_id": self.current_session_id,
                "requests": pending
            })

    # ------------------------------------------------------------------
    # handle_user_question_response
    # ------------------------------------------------------------------

    async def handle_user_question_response(self, data: dict) -> None:
        """Handle 'user_question_response' message type."""
        request_id = data.get("request_id")
        answers = data.get("answers", {})

        if self.current_session_id and request_id and answers:
            result = await user_question_handler.respond(
                request_id=request_id,
                session_id=self.current_session_id,
                answers=answers
            )
            logger.debug(f"User question response processed: {request_id} -> {result}")

            await self.send_json({
                "type": "user_question_response_ack",
                "request_id": request_id,
                "success": result
            })
        else:
            await self.send_json({
                "type": "error",
                "message": "Invalid user question response"
            })

    # ------------------------------------------------------------------
    # handle_get_pending_questions
    # ------------------------------------------------------------------

    async def handle_get_pending_questions(self, data: dict) -> None:
        """Handle 'get_pending_questions' message type."""
        if self.current_session_id:
            pending = user_question_handler.get_pending_questions(self.current_session_id)
            await self.send_json({
                "type": "pending_questions",
                "session_id": self.current_session_id,
                "questions": pending
            })

    # ------------------------------------------------------------------
    # handle_tabs_update
    # ------------------------------------------------------------------

    async def handle_tabs_update(self, data: dict) -> None:
        """Handle 'tabs_update' message type."""
        tabs_data = data.get("tabs_data", {})
        await sync_engine.broadcast_tabs_updated(
            user_id=self.user_id,
            tabs_data=tabs_data,
            source_device_id=self.device_id
        )

    # ------------------------------------------------------------------
    # _handle_pong (no-op)
    # ------------------------------------------------------------------

    async def _handle_pong(self, data: dict) -> None:
        """Handle 'pong' message type — keep-alive response, no-op."""
        pass

    # ------------------------------------------------------------------
    # run_query — the streaming pipeline
    # ------------------------------------------------------------------

    async def run_query(
        self,
        prompt: str,
        session_id: str,
        profile_id: str,
        project_id: Optional[str],
        overrides: Optional[Dict[str, Any]] = None,
        worktree_id: Optional[str] = None,
    ) -> None:
        """Execute query and stream results directly to WebSocket."""
        from app.api.websocket import _active_chat_sessions, _delayed_session_cleanup
        from app.api.ws_event_handlers import EventContext, MutableStreamState, dispatch_event
        from app.core.query_engine import stream_to_websocket
        from app.core.jsonl_parser import parse_session_history
        import time

        # Generate a message ID for this streaming session
        message_id = str(uuid.uuid4())

        # Structured logging context for this streaming request
        ctx = StreamingContext(
            session_id=session_id,
            device_id=self.device_id,
            message_id=message_id,
            user_id=self.user_id,
        )

        # Event dispatch context and mutable state
        event_ctx = EventContext(
            ctx=ctx,
            session_id=session_id,
            message_id=message_id,
            user_id=self.user_id,
            project_id=project_id,
            worktree_id=worktree_id,
        )
        stream_state = MutableStreamState()

        # Unregister this device from SyncEngine while streaming
        # This prevents duplicate events (we get direct events via send_json)
        # If the device disconnects and reconnects, it will re-register and get sync events
        await sync_engine.unregister_device(self.device_id, session_id)
        ctx.debug(logger, "Unregistered device during streaming to prevent duplicates")

        # Initialize LiveSessionStore for server-authoritative state
        # Uses MessageResolver: checks ended live store first (for interrupt transitions),
        # then JSONL. No DB fallback (DB message IDs conflict with JSONL-based sessions).
        session_data = database.get_session(session_id)
        sdk_sid = session_data.get("sdk_session_id") if session_data else None
        w_dir = resolve_working_dir(session_data) if session_data else str(settings.workspace_dir)
        initial_messages = message_resolver.resolve_for_streaming_init(session_id, sdk_sid, w_dir)
        ctx.debug(logger, f"Loaded {len(initial_messages)} initial messages via MessageResolver")

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
                    await self.send_json(heartbeat_data)

        try:
            ctx.debug(logger, f"Starting query profile={profile_id}, project={project_id}, overrides={overrides}")
            if not await self.send_json({"type": "start", "session_id": session_id, "message_id": message_id}):
                ctx.warning(logger, "Connection lost before query could start")
                await live_session_store.cleanup_session(session_id)
                return  # Exit early - connection is dead

            # NOTE: Don't call start_assistant_message here!
            # Creating an empty text message before content arrives causes the "Claude [time]"
            # header to appear even when the first content is a thinking block.
            # Let append_text/append_thinking create messages lazily when content arrives.

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(send_heartbeats())

            ctx.debug(logger, f"Calling stream_to_websocket, api_user_id={self.api_user_id}")
            async for event in stream_to_websocket(
                prompt=prompt,
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                overrides=overrides,
                broadcast_func=self.send_json,  # Pass send_json for permission requests
                api_user_id=self.api_user_id  # Pass for user-scoped credential resolution
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
                ws_connected = await self.send_json(event)
                if not ws_connected and not stream_state.connection_lost_logged:
                    stream_state.connection_lost_logged = True
                    ctx.debug(logger, "WebSocket disconnected during streaming, updates continue via sync_engine + polling")

                # Dispatch to handler — updates live_session_store and broadcasts via sync_engine
                await dispatch_event(event, event_ctx, stream_state)

            ctx.debug(logger, "Query completed")

            # Dispatch notifications for session completion (non-blocking)
            import time as time_module
            duration = time_module.time() - stream_state.query_start_time
            final_metadata = stream_state.final_metadata
            session_data = database.get_session(session_id)
            try:
                _cb = lambda t: t.exception() if not t.cancelled() else None
                # Legacy webhook dispatch
                _t = asyncio.create_task(dispatch_session_complete(
                    session_id=session_id,
                    title=session_data.get("title") if session_data else None,
                    profile_id=profile_id,
                    total_cost=final_metadata.get("total_cost_usd", 0.0),
                    input_tokens=final_metadata.get("tokens_in", 0),
                    output_tokens=final_metadata.get("tokens_out", 0),
                    duration_seconds=duration
                ))
                _t.add_done_callback(_cb)
                # Connector dispatch
                _t = asyncio.create_task(connector_manager.dispatch_event(
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
                _t.add_done_callback(_cb)
            except Exception as webhook_error:
                ctx.warning(logger, f"Failed to dispatch session.complete notifications: {webhook_error}")

        except asyncio.CancelledError:
            await self.send_json({"type": "stopped", "session_id": session_id})
            ctx.debug(logger, "Query cancelled")

            # End live session store with interrupted flag
            # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
            await live_session_store.end_streaming(
                session_id=session_id,
                metadata={},
                interrupted=True
            )

            # Schedule delayed cleanup of ended session (gives clients time to reconnect)
            # This prevents memory leak while ensuring messages are available for ~30 seconds
            _t = asyncio.create_task(_delayed_session_cleanup(session_id, delay_seconds=30))
            _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

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
            ctx.error(logger, f"Query error: {e}", exc_info=True)
            await self.send_json({"type": "error", "message": str(e)})

            # End live session store on error
            # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
            await live_session_store.end_streaming(
                session_id=session_id,
                metadata={"error": str(e)},
                interrupted=True
            )

            # Schedule delayed cleanup of ended session (gives clients time to reconnect)
            _t = asyncio.create_task(_delayed_session_cleanup(session_id, delay_seconds=30))
            _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

            # Broadcast stream end on error
            if stream_state.stream_started:
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id=message_id,
                    metadata={"error": str(e)},
                    interrupted=True,
                    source_device_id=None
                )

            # Dispatch notifications for session error (non-blocking)
            session_data = database.get_session(session_id)
            try:
                _cb = lambda t: t.exception() if not t.cancelled() else None
                # Legacy webhook dispatch
                _t = asyncio.create_task(dispatch_session_error(
                    session_id=session_id,
                    title=session_data.get("title") if session_data else None,
                    profile_id=profile_id,
                    error_message=str(e)
                ))
                _t.add_done_callback(_cb)
                # Connector dispatch
                _t = asyncio.create_task(connector_manager.dispatch_event(
                    "session.error",
                    {
                        "session_id": session_id,
                        "title": session_data.get("title") if session_data else None,
                        "profile_id": profile_id,
                        "error_message": str(e),
                    }
                ))
                _t.add_done_callback(_cb)
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
            if self.websocket.client_state == WebSocketState.CONNECTED:
                # Check if a different websocket is already registered for this device
                # (This happens when user refreshes the page during streaming)
                current_connection = sync_engine._connections.get(session_id, {}).get(self.device_id)
                if current_connection is not None and current_connection.websocket is not self.websocket:
                    # A newer connection exists - don't replace it with this old websocket
                    ctx.debug(logger, "Skipping re-register - newer connection already exists")
                else:
                    await sync_engine.register_device(self.device_id, session_id, self.websocket)
                    ctx.debug(logger, "Re-registered device after streaming complete")
            else:
                ctx.debug(logger, "Skipping re-register - websocket disconnected during streaming")

            # Clean up task reference only if it's still our task (avoid TOCTOU race
            # where a different device started a new query for the same session)
            current_task = asyncio.current_task()
            if _active_chat_sessions.get(session_id) is current_task:
                del _active_chat_sessions[session_id]

            # If no devices are connected after query finished, schedule cleanup
            # (don't destroy immediately — user may reopen within 30s)
            remaining_devices = sync_engine.get_device_count(session_id)
            if remaining_devices == 0:
                ctx.debug(logger, "Query finished but no devices connected, scheduling cleanup")
                if live_session_store.has_session(session_id) and live_session_store.is_streaming(session_id):
                    # Only force-cleanup if still marked as streaming (abnormal state)
                    await live_session_store.cleanup_session(session_id)
                    ctx.debug(logger, "Cleaned up stuck streaming session")
