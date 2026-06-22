"""
ChatWebSocketHandler — class-based handler for the primary chat WebSocket.

Extracted from the nested-closure `chat_websocket` function in websocket.py.
All instance state replaces former `nonlocal` variables; nested functions
become methods.  Behaviour is identical to the original implementation.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

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
from app.db.async_database import run_sync
from app.db.base import WorktreeStatus

logger = logging.getLogger(__name__)

# Shared module-level sets tracking sessions with a pending two-phase
# ``interrupt_for_query`` or a user-initiated stop.  Checked by run_query's
# finally block to decide whether to auto-process the next queued message.
# Defined in ``ws_cleanup`` so both this module and ``app/api/websocket.py``
# see the same flag regardless of which endpoint set it.
from app.api.ws_cleanup import (  # noqa: E402  (after logger by design)
    _interrupt_pending_sessions,
    _stopped_sessions,
)


# Terminal background-task statuses that should wake the parent (mirror the SDK's
# TaskNotificationStatus literal — every TaskNotificationMessage is terminal).
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped"})

# The note Claude Code attaches to every task-notification: it fires each time the
# agent comes to rest with no live background children, and the user can resume it
# so the same task-id may notify more than once.
_TASK_NOTIFICATION_NOTE = (
    "This notification fires each time the agent comes to rest with no live "
    "background children; the user can resume it, so the same task-id may notify "
    "more than once."
)


def _build_task_notification_prompt(pending: list) -> str:
    """Build ONE synthesized user prompt consolidating all pending notifications.

    Mirrors Claude Code's reference ``<task-notification>`` contract so the parent
    wakes with each sub-agent's FULL result. The full result text is read off the
    local disk via ``query_engine._read_task_output_file`` (the only way to obtain
    it — the SDK message carries only a summary + path). The returned string always
    contains the literal ``<task-notification>`` substring, so the existing user-
    message filters keep it from rendering as a chat bubble.
    """
    from app.core.query_engine import _read_task_output_file

    def _esc(value: Any) -> str:
        return "" if value is None else str(value)

    blocks: List[str] = []
    for note in pending:
        output_file = note.get("output_file") or ""
        result_text = _read_task_output_file(output_file)
        usage = note.get("usage") or {}
        usage_xml = (
            "  <usage>\n"
            f"    <subagent_tokens>{_esc(usage.get('total_tokens'))}</subagent_tokens>\n"
            f"    <tool_uses>{_esc(usage.get('tool_uses'))}</tool_uses>\n"
            f"    <duration_ms>{_esc(usage.get('duration_ms'))}</duration_ms>\n"
            "  </usage>"
        )
        blocks.append(
            "<task-notification>\n"
            f"  <task-id>{_esc(note.get('task_id'))}</task-id>\n"
            f"  <tool-use-id>{_esc(note.get('tool_use_id'))}</tool-use-id>\n"
            f"  <output-file>{_esc(output_file)}</output-file>\n"
            f"  <status>{_esc(note.get('status'))}</status>\n"
            f"  <summary>{_esc(note.get('summary'))}</summary>\n"
            f"  <note>{_TASK_NOTIFICATION_NOTE}</note>\n"
            f"  <result>{result_text}</result>\n"
            f"{usage_xml}\n"
            "</task-notification>"
        )
    return "\n".join(blocks)


class WebSocketRateLimiter:
    """Sliding-window rate limiter for WebSocket messages per connection.

    Uses a simple timestamp list to enforce a maximum number of messages
    within a rolling time window.  One instance should be created per
    WebSocket connection (not shared globally).
    """

    def __init__(self, max_messages: int = 30, window_seconds: float = 10.0):
        self._timestamps: list[float] = []
        self._max_messages = max_messages
        self._window_seconds = window_seconds

    def check(self) -> bool:
        """Return ``True`` if the message is allowed, ``False`` if rate-limited."""
        now = time.monotonic()
        # Prune timestamps outside the current window
        cutoff = now - self._window_seconds
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self._max_messages:
            return False
        self._timestamps.append(now)
        return True


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

        # (interrupt_pending_sessions and stopped_sessions are now module-level
        # sets so the flag is visible across handler instances / devices.)

        # Per-connection rate limiter for inbound messages
        self.rate_limiter = WebSocketRateLimiter(
            max_messages=settings.ws_rate_limit_messages,
            window_seconds=settings.ws_rate_limit_window_seconds,
        )

        # Lock to serialize WebSocket sends — prevents concurrent sends from
        # ping_loop, heartbeat, and the main message handler racing each other
        self.send_lock = asyncio.Lock()

        # Monotonically increasing sequence number for every outbound message.
        # Frontend can track gaps to measure how often WebSocket delivery fails
        # (polling recovers the data, but this gives us observability).
        self._ws_seq: int = 0
        self._ws_drops: int = 0

        # Message-type dispatch table
        self._handlers: Dict[str, Callable[[dict], Any]] = {
            "query": self.handle_query,
            "stop": self.handle_stop,
            "interrupt_for_query": self.handle_interrupt_for_query,
            "load_session": self.handle_load_session,
            "close_session": self.handle_close_session,
            "permission_response": self.handle_permission_response,
            "get_pending_permissions": self.handle_get_pending_permissions,
            "requeue_deferred_permissions": self.handle_requeue_deferred_permissions,
            "user_question_response": self.handle_user_question_response,
            "get_pending_questions": self.handle_get_pending_questions,
            "tabs_update": self.handle_tabs_update,
            "pong": self._handle_pong,
            "queue_message": self.handle_queue_message,
            "remove_queued_message": self.handle_remove_queued_message,
            "clear_queued_messages": self.handle_clear_queued_messages,
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

        Every outbound message gets a monotonic ``_seq`` number so the frontend
        can detect delivery gaps (for diagnostics — polling handles recovery).
        """
        # Fast path: skip lock acquisition if already known dead
        if not self.connection_healthy:
            return False

        async with self.send_lock:
            if not self.connection_healthy:
                return False

            # Headless path: no websocket attached (e.g. server-side query
            # kicked off by the /sessions/with-worktree REST endpoint).
            # Silently "succeed" so run_query doesn't early-exit; state
            # propagates via sync_engine broadcasts to any registered
            # devices instead of via direct-to-WS sends.
            if self.websocket is None:
                return True

            if self.websocket.client_state != WebSocketState.CONNECTED:
                self.connection_healthy = False
                logger.debug(f"WebSocket disconnected, cannot send message type: {data.get('type')}")
                return False

            try:
                self._ws_seq += 1
                data["_seq"] = self._ws_seq
                await self.websocket.send_json(data)
                return True
            except Exception as e:
                self._ws_drops += 1
                self.connection_healthy = False
                msg_type = data.get("type") or data.get("event_type", "unknown")
                logger.warning(
                    f"WebSocket send failed [seq={self._ws_seq}, type={msg_type}, "
                    f"device={self.device_id}, total_drops={self._ws_drops}]: {e}"
                )
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

                        # Rate-limit user-initiated messages (skip keepalive pongs)
                        if msg_type != "pong" and not self.rate_limiter.check():
                            logger.warning(
                                f"WebSocket rate limited: device={self.device_id}, "
                                f"msg_type={msg_type}"
                            )
                            await self.send_json({
                                "type": "error",
                                "error": "Rate limited: too many messages. Please slow down.",
                            })
                            continue

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
                    from app.api.websocket import _active_chat_sessions, _active_sessions_lock

                    remaining_devices = sync_engine.get_device_count(self.current_session_id)

                    async with _active_sessions_lock:
                        active_task = _active_chat_sessions.get(self.current_session_id)
                        is_task_done = active_task is None or active_task.done()

                        if remaining_devices == 0 and is_task_done:
                            # Remove from active chat sessions tracking
                            _active_chat_sessions.pop(self.current_session_id, None)

                    if remaining_devices == 0 and is_task_done:
                        # No more devices watching and no active query
                        # Only cleanup sessions still in streaming state (abnormal);
                        # ended sessions are handled by _delayed_session_cleanup
                        logger.debug(f"Last device disconnected from session {self.current_session_id}")
                        if live_session_store.has_session(self.current_session_id) and live_session_store.is_streaming(self.current_session_id):
                            await live_session_store.cleanup_session(self.current_session_id)
                            logger.debug(f"Cleaned up stuck streaming session {self.current_session_id}")

                        # Clear streaming state in sync engine (no need to broadcast - no devices left)
                        await sync_engine.clear_streaming_state(self.current_session_id)

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
        from app.api.websocket import _active_chat_sessions, _active_sessions_lock

        prompt = data.get("prompt", "").strip()
        session_id = data.get("session_id")
        profile_id = data.get("profile", "claude-code")
        project_id = data.get("project")
        overrides = data.get("overrides")  # Optional: {model, permission_mode}
        worktree_id = data.get("worktree_id")  # Optional: link session to existing worktree

        if not prompt:
            await self.send_json({"type": "error", "message": "Empty prompt"})
            return

        # Validate project-level authorization for API users
        # API users are scoped to a specific project — prevent cross-project access
        if self.api_user_id and project_id:
            api_user = await run_sync(database.get_api_user, self.api_user_id)
            if api_user and api_user.get("project_id") and api_user["project_id"] != project_id:
                logger.warning(
                    f"Authorization denied: API user {self.api_user_id} attempted to access "
                    f"project {project_id} but is scoped to project {api_user['project_id']}"
                )
                await self.send_json({"type": "error", "message": "Not authorized for this project"})
                return

        # Create or get session
        if not session_id:
            # Validate worktree access if provided
            worktree = None
            if worktree_id:
                worktree = await run_sync(database.get_worktree, worktree_id)
                if not worktree:
                    await self.send_json({"type": "error", "message": f"Worktree not found: {worktree_id}"})
                    return
                # Get repository to verify project ownership
                repo = await run_sync(database.get_git_repository, worktree["repository_id"])
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
            is_new_session = True
            # Generate title from the user's first message
            title = prompt[:100].strip()
            if len(prompt) > 100:
                title = title.rsplit(' ', 1)[0] + "..."
            # Get worktree info for persistent storage on session
            wt_branch = worktree.get("branch_name") if worktree_id and worktree else None
            wt_path = worktree.get("worktree_path") if worktree_id and worktree else None

            # Create session + first user message atomically so neither is orphaned.
            # Wrapped in run_sync to avoid blocking the event loop during DB I/O.
            from app.db.base import transaction

            def _create_session_atomic():
                with transaction() as txn:
                    database.create_session(
                        session_id=session_id,
                        profile_id=profile_id,
                        project_id=project_id,
                        api_user_id=self.api_user_id,
                        title=title,
                        worktree_id=worktree_id,
                        worktree_branch=wt_branch,
                        worktree_path=wt_path,
                        _conn=txn
                    )
                    _msg_role = "user"
                    _msg_meta = None
                    if prompt.strip().startswith("/"):
                        _parts = prompt.strip().split(None, 1)
                        _msg_role = "command"
                        _msg_meta = {
                            "command_name": _parts[0],
                            "command_args": _parts[1] if len(_parts) > 1 else "",
                        }
                    database.add_session_message(
                        session_id=session_id,
                        role=_msg_role,
                        content=prompt,
                        metadata=_msg_meta,
                        profile_id=profile_id,
                        _conn=txn
                    )

            await run_sync(_create_session_atomic)

            # Immediately notify frontend of new session_id so it persists on refresh
            await self.send_json({
                "type": "session_created",
                "session_id": session_id,
                "title": title,
                "worktree_id": worktree_id  # Include worktree_id in response
            })

            # Notify other devices so their side panels show the new session
            await sync_engine.broadcast_user_session_created(
                user_id=self.user_id,
                session_id=session_id,
                source_device_id=self.device_id
            )
        else:
            is_new_session = False

        # Register device with session if switching sessions
        if session_id != self.current_session_id:
            # Unregister from old session if any
            if self.current_session_id:
                await sync_engine.unregister_device(self.device_id, self.current_session_id)

            # Register to new session
            await sync_engine.register_device(self.device_id, session_id, self.websocket)
            self.current_session_id = session_id

        # Store user message for existing sessions (new sessions already stored atomically above)
        if not is_new_session:
            # Backfill title from first user message for sessions created without
            # one (e.g. via the auto-worktree REST endpoint, which creates the
            # session before any user prompt exists).
            existing_session = await run_sync(database.get_session, session_id)
            if existing_session and not (existing_session.get("title") or "").strip():
                derived_title = prompt[:100].strip()
                if len(prompt) > 100:
                    derived_title = derived_title.rsplit(' ', 1)[0] + "..."
                if derived_title:
                    await run_sync(
                        database.update_session,
                        session_id=session_id,
                        title=derived_title,
                    )
                    # Notify all devices (including this one) so sidebars refresh.
                    await sync_engine.broadcast_session_updated(
                        session_id=session_id,
                        updates={"title": derived_title},
                        source_device_id=None,
                    )

            _msg_role = "user"
            _msg_meta = None
            if prompt.strip().startswith("/"):
                _parts = prompt.strip().split(None, 1)
                _msg_role = "command"
                _msg_meta = {
                    "command_name": _parts[0],
                    "command_args": _parts[1] if len(_parts) > 1 else "",
                }
            await run_sync(
                database.add_session_message,
                session_id=session_id,
                role=_msg_role,
                content=prompt,
                metadata=_msg_meta,
                profile_id=profile_id,
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
        async with _active_sessions_lock:
            existing_task = _active_chat_sessions.get(session_id)
        if existing_task:
            existing_task.cancel()
            try:
                await existing_task
            except asyncio.CancelledError:
                pass

        # Start new query task
        # Note: broadcast_stream_start is called inside run_query when actual
        # streaming begins. This prevents stuck streaming state if query fails early.
        self.query_task = asyncio.create_task(
            self.run_query(prompt, session_id, profile_id, project_id, overrides, worktree_id)
        )
        async with _active_sessions_lock:
            _active_chat_sessions[session_id] = self.query_task

    # ------------------------------------------------------------------
    # handle_stop
    # ------------------------------------------------------------------

    async def handle_stop(self, data: dict) -> None:
        """Handle 'stop' message type."""
        from app.api.websocket import _active_chat_sessions, _active_sessions_lock

        session_id = data.get("session_id") or self.current_session_id
        if session_id:
            from app.core.query_engine import interrupt_session

            # Mark session as user-stopped so the finally block in run_query
            # does NOT auto-fire queued messages.  The queue stays intact —
            # the user can send them manually or clear them.
            _stopped_sessions.add(session_id)

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
                    _t = asyncio.create_task(_safety_cancel(task, session_id))
                    _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # ------------------------------------------------------------------
    # handle_interrupt_for_query (two-phase: interrupt only, then confirm)
    # ------------------------------------------------------------------

    async def handle_interrupt_for_query(self, data: dict) -> None:
        """Handle 'interrupt_for_query' — Phase 1 of the two-phase interrupt+query.

        Interrupts the active streaming session, waits for the drain to complete,
        then sends 'interrupt_confirmed' back to the frontend.  The frontend
        will only send the actual query AFTER receiving confirmation, preventing
        the race condition where a message is sent before the interrupt finishes.
        """
        from app.api.websocket import _active_chat_sessions, _active_sessions_lock

        session_id = data.get("session_id") or self.current_session_id
        if not session_id:
            await self.send_json({"type": "interrupt_failed", "session_id": session_id, "message": "No active session"})
            return

        from app.core.query_engine import interrupt_session

        # Mark as interrupt-pending so the old task's finally block skips queue processing
        _interrupt_pending_sessions.add(session_id)

        try:
            # Step 1: Interrupt the SDK-level streaming
            interrupted = await interrupt_session(session_id)
            logger.debug(f"interrupt_for_query: interrupt session {session_id}: {interrupted}")

            # Step 2: Wait for the streaming task to drain fully.
            # Drain wait — we MUST let
            # receive_response() drain before the frontend sends a new query.
            async with _active_sessions_lock:
                task = _active_chat_sessions.get(session_id)
            if task and not task.done():
                done, pending = await asyncio.wait({task}, timeout=8.0)
                if pending:
                    logger.warning(f"interrupt_for_query: drain timed out for {session_id}, force cancelling")
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

            # Step 3: Confirm to frontend — safe to send the query now
            await self.send_json({"type": "interrupt_confirmed", "session_id": session_id})
            logger.debug(f"interrupt_for_query: confirmed for session {session_id}")

        except Exception as e:
            logger.error(f"interrupt_for_query failed for {session_id}: {e}")
            await self.send_json({"type": "interrupt_failed", "session_id": session_id, "message": str(e)})
        finally:
            _interrupt_pending_sessions.discard(session_id)

    # ------------------------------------------------------------------
    # handle_load_session
    # ------------------------------------------------------------------

    async def handle_load_session(self, data: dict) -> None:
        """Handle 'load_session' message type."""
        from app.api.websocket import _active_chat_sessions

        session_id = data.get("session_id")
        if not session_id:
            return

        # Try direct lookup by AI Shuffle session ID first (non-blocking)
        session = await run_sync(database.get_session, session_id)

        # If not found, try fallback lookup by SDK session ID (Claude Code UUID)
        # This handles cases where old references might use the SDK session ID
        if not session:
            session = await run_sync(database.get_session_by_sdk_id, session_id)
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

        # Unified message resolution: LiveSessionStore -> JSONL -> PostgreSQL
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
        # Include queued messages so frontend displays server-side queue
        queued = await run_sync(database.get_queued_messages, session_id)
        if queued:
            history_response["queuedMessages"] = queued
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
        response_session_id = data.get("session_id") or self.current_session_id
        decision = data.get("decision")  # "allow", "deny", or "defer"
        # remember and pattern are ignored when decision == "defer"
        remember = data.get("remember")  # "none", "session", or "profile"
        pattern = data.get("pattern")  # Optional pattern for the rule

        if request_id and decision and response_session_id:
            result = await permission_handler.respond(
                request_id=request_id,
                session_id=response_session_id,
                decision=decision,
                remember=remember if decision != "defer" else None,
                pattern=pattern if decision != "defer" else None,
                broadcast_func=self.send_json
            )
            logger.debug(f"Permission response processed: {result}")

            # Send confirmation back
            await self.send_json({
                "type": "permission_response_ack",
                "session_id": response_session_id,
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
            deferred = permission_handler.get_deferred_requests(self.current_session_id)
            await self.send_json({
                "type": "pending_permissions",
                "session_id": self.current_session_id,
                "pending": pending,
                "deferred": deferred,
            })

    # ------------------------------------------------------------------
    # handle_requeue_deferred_permissions
    # ------------------------------------------------------------------

    async def handle_requeue_deferred_permissions(self, data: dict) -> None:
        """Handle 'requeue_deferred_permissions' — move deferred requests back to pending."""
        session_id = data.get("session_id") or self.current_session_id
        if not session_id:
            await self.send_json({"type": "error", "message": "No session_id for requeue"})
            return

        requeued_ids = await permission_handler.requeue_deferred(
            session_id=session_id,
            broadcast_func=self.send_json,
        )

        logger.debug(f"Requeued {len(requeued_ids)} deferred permission(s) for {session_id}")

        await self.send_json({
            "type": "permission_queue_update",
            "session_id": session_id,
            "requeued_ids": requeued_ids,
            "remaining_count": permission_handler.get_queue_count(session_id),
        })

    # ------------------------------------------------------------------
    # handle_user_question_response
    # ------------------------------------------------------------------

    async def handle_user_question_response(self, data: dict) -> None:
        """Handle 'user_question_response' message type."""
        request_id = data.get("request_id")
        response_session_id = data.get("session_id") or self.current_session_id
        answers = data.get("answers", {})

        if response_session_id and request_id and answers:
            result = await user_question_handler.respond(
                request_id=request_id,
                session_id=response_session_id,
                answers=answers
            )
            logger.debug(f"User question response processed: {request_id} -> {result}")

            await self.send_json({
                "type": "user_question_response_ack",
                "session_id": response_session_id,
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
    # handle_queue_message
    # ------------------------------------------------------------------

    async def handle_queue_message(self, data: dict) -> None:
        """Handle 'queue_message' — add a message to the server-side queue."""
        from app.api.websocket import _active_chat_sessions

        content = (data.get("content") or "").strip()
        session_id = data.get("session_id") or self.current_session_id
        profile_id = data.get("profile")
        project_id = data.get("project")

        if not content:
            await self.send_json({"type": "error", "message": "Empty queue content"})
            return
        if not session_id:
            await self.send_json({"type": "error", "message": "No session for queue"})
            return

        # Clear stopped flag — user explicitly queued a new message, so they
        # want the queue to auto-process when the session becomes idle.
        _stopped_sessions.discard(session_id)

        # Store in DB
        await run_sync(
            database.add_queued_message,
            session_id=session_id,
            content=content,
            profile_id=profile_id,
            project_id=project_id,
        )

        # Broadcast updated queue to all devices via sync engine
        queue = await run_sync(database.get_queued_messages, session_id)
        await sync_engine.broadcast_queue_updated(session_id, queue)

        # During streaming, this device is unregistered from SyncEngine (to
        # prevent duplicate events), so the broadcast above won't reach it.
        # Send the queue update directly so the queued-messages UI updates.
        await self.send_json({
            "type": "sync",
            "event_type": "queue_updated",
            "session_id": session_id,
            "data": {"queued_messages": queue},
        })

        # If session is NOT currently streaming, immediately process the queue.
        # Wrap in try/except — _process_next_queued re-raises after recovery,
        # and an unhandled exception here would kill the WebSocket connection
        # (the dispatch loop only catches asyncio.TimeoutError).
        active_task = _active_chat_sessions.get(session_id)
        if not active_task or active_task.done():
            try:
                await self._process_next_queued(session_id)
            except Exception as exc:
                logger.warning(f"[Queue] Failed to auto-process queued message from handle_queue_message: {exc}")

    # ------------------------------------------------------------------
    # handle_remove_queued_message
    # ------------------------------------------------------------------

    async def handle_remove_queued_message(self, data: dict) -> None:
        """Handle 'remove_queued_message' — remove a single message from the queue."""
        message_id = data.get("message_id")
        session_id = data.get("session_id") or self.current_session_id

        if not message_id:
            return

        await run_sync(database.remove_queued_message, message_id)

        # Broadcast updated queue
        if session_id:
            queue = await run_sync(database.get_queued_messages, session_id)
            await sync_engine.broadcast_queue_updated(session_id, queue)
            # Direct send for streaming device (unregistered from SyncEngine)
            await self.send_json({
                "type": "sync",
                "event_type": "queue_updated",
                "session_id": session_id,
                "data": {"queued_messages": queue},
            })

    # ------------------------------------------------------------------
    # handle_clear_queued_messages
    # ------------------------------------------------------------------

    async def handle_clear_queued_messages(self, data: dict) -> None:
        """Handle 'clear_queued_messages' — remove all queued messages for a session."""
        session_id = data.get("session_id") or self.current_session_id

        if not session_id:
            return

        await run_sync(database.clear_queued_messages, session_id)
        await sync_engine.broadcast_queue_updated(session_id, [])
        # Direct send for streaming device (unregistered from SyncEngine)
        await self.send_json({
            "type": "sync",
            "event_type": "queue_updated",
            "session_id": session_id,
            "data": {"queued_messages": []},
        })

    # ------------------------------------------------------------------
    # _process_next_queued — auto-send next queued message
    # ------------------------------------------------------------------

    async def _process_next_queued(self, session_id: str) -> None:
        """Pop the next queued message and start a query for it.

        Called after a query completes (done/stopped/error) or when a new
        message is queued and the session is idle. Runs entirely server-side
        — does not require the originating browser to be connected.

        If anything fails after the message is popped from the queue, it is
        re-inserted at the front so the user never silently loses a message.
        """
        from app.api.websocket import _active_chat_sessions, _active_sessions_lock

        msg = await run_sync(database.pop_next_queued_message, session_id)
        if not msg:
            return

        content = msg["content"]
        profile_id = msg.get("profile_id") or "claude-code"
        project_id = msg.get("project_id")

        logger.info(f"[Queue] Auto-processing queued message for session {session_id}: {content[:80]}...")

        try:
            # Broadcast updated queue to all devices
            queue = await run_sync(database.get_queued_messages, session_id)
            await sync_engine.broadcast_queue_updated(session_id, queue)

            # Store user message in DB (detect slash commands)
            _msg_role = "user"
            _msg_meta = None
            if content.strip().startswith("/"):
                _parts = content.strip().split(None, 1)
                _msg_role = "command"
                _msg_meta = {
                    "command_name": _parts[0],
                    "command_args": _parts[1] if len(_parts) > 1 else "",
                }
            await run_sync(
                database.add_session_message,
                session_id=session_id,
                role=_msg_role,
                content=content,
                metadata=_msg_meta,
                profile_id=profile_id,
            )

            # Broadcast user message to all devices (no source_device_id — server-originated)
            await sync_engine.broadcast_message_added(
                session_id=session_id,
                message={
                    "role": "user",
                    "content": content,
                    "profile_id": profile_id
                },
                source_device_id=None  # Send to ALL devices
            )

            # Cancel any existing query (shouldn't be one, but safety)
            async with _active_sessions_lock:
                existing_task = _active_chat_sessions.get(session_id)
            if existing_task and not existing_task.done():
                existing_task.cancel()
                try:
                    await existing_task
                except asyncio.CancelledError:
                    pass

            # Look up worktree_id from session
            session_data = await run_sync(database.get_session, session_id)
            worktree_id = session_data.get("worktree_id") if session_data else None

            # Start the query
            task = asyncio.create_task(
                self.run_query(content, session_id, profile_id, project_id, None, worktree_id)
            )
            async with _active_sessions_lock:
                _active_chat_sessions[session_id] = task
            self.query_task = task

        except Exception as exc:
            # Re-insert the message at the front of the queue so it isn't lost.
            logger.error(f"[Queue] Failed to start query for queued message, re-queuing: {exc}")
            try:
                await run_sync(
                    database.requeue_message_at_front,
                    session_id=session_id,
                    content=content,
                    profile_id=profile_id if profile_id != "claude-code" else None,
                    project_id=project_id,
                )
                # Broadcast the restored queue
                queue = await run_sync(database.get_queued_messages, session_id)
                await sync_engine.broadcast_queue_updated(session_id, queue)
            except Exception as requeue_err:
                logger.error(f"[Queue] Failed to re-queue message: {requeue_err}")
            raise

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
        from app.api.websocket import _active_chat_sessions, _active_sessions_lock, schedule_delayed_cleanup, cancel_pending_cleanup
        from app.api.ws_event_handlers import EventContext, MutableStreamState, dispatch_event
        from app.core.query_engine import stream_to_websocket
        from app.core.jsonl_parser import parse_session_history
        from app.core.task_registry import task_registry
        import time

        # Cancel any pending delayed cleanup for this session — a new query is starting,
        # so the old cleanup must not fire and remove the session from live_session_store.
        cancel_pending_cleanup(session_id)

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
        # If the device disconnects and reconnects, it will re-register and get sync events.
        # Pass self.websocket so we only unregister OUR connection — if user already
        # reconnected (new ws registered for same device_id), we must not nuke it.
        await sync_engine.unregister_device(self.device_id, session_id, self.websocket)
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

        # Set to True when pre-close queue processing spawned a successor
        # run_query task for the next queued message. When True, the finally
        # block below MUST NOT touch live_session_store, sync_engine streaming
        # state, or the SyncEngine device registration for this session — the
        # successor task now owns all of that. Touching them here would race
        # with the successor's start_streaming/unregister_device calls and
        # leave the session in a frozen "locked" state with no stream events
        # reaching the client.
        handed_off_to_queued = False

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
                # Tag every event with session_id so the frontend can reject
                # late-arriving events from a previous session (cross-session guard).
                event["session_id"] = session_id
                ws_connected = await self.send_json(event)
                if not ws_connected and not stream_state.connection_lost_logged:
                    stream_state.connection_lost_logged = True
                    ctx.debug(logger, "WebSocket disconnected during streaming, updates continue via sync_engine + polling")

                # Dispatch to handler — updates live_session_store and broadcasts via sync_engine
                await dispatch_event(event, event_ctx, stream_state)

                # --- Background-agent "unpark" stash ---
                # A terminal task_notification arrives IN-STREAM (the receive loop
                # stays open the whole time the sub-agent runs), immediately before
                # the ResultMessage that ends this loop. Stash it now (deduped by
                # task_registry.mark_notified) and fire ONE consolidated follow-up
                # turn in the post-loop block — never start a new query mid-loop.
                if event_type == 'task_notification' and event.get('status') in _TERMINAL_TASK_STATUSES:
                    _task_id = event.get('task_id')
                    _output_file = event.get('output_file')
                    _signature = event.get('uuid') or f"{_output_file}:{event.get('status')}"
                    if task_registry.mark_notified(session_id, _task_id, _signature):
                        stream_state.pending_task_notifications.append({
                            "task_id": _task_id,
                            "status": event.get('status'),
                            "summary": event.get('summary'),
                            "output_file": _output_file,
                            "uuid": event.get('uuid'),
                            "usage": event.get('usage'),
                            "tool_use_id": event.get('tool_use_id'),
                        })

                # NOTE: Queued ("Send Later") messages are intentionally NOT sent
                # at intermediate turn boundaries (e.g. stop_reason="tool_use").
                # Interrupting the SDK mid-query at a tool_use boundary leaves an
                # orphaned tool_use block with no matching tool_result in the
                # session history — an invalid conversation state that corrupts
                # the session so the queued message (and every query after it)
                # immediately stops. We let the query run to its natural end (the
                # final assistant message / end_turn); the pre-close queue
                # processing below then sends the queued message as a clean new
                # query.

            ctx.debug(logger, "Query completed")

            # --- Background-agent "unpark" follow-up turn ---
            # If any terminal task_notifications were stashed in-loop, synthesize
            # ONE consolidated user prompt carrying each sub-agent's FULL result
            # (read off local disk) and start a fresh turn so the parent "unparks"
            # and responds. Modeled EXACTLY on the queue-handoff pattern below
            # (persist user msg → broadcast → end_streaming → create_task → return)
            # so streaming tears down + re-inits without a race.
            #
            # The synthesized prompt contains the literal "<task-notification>"
            # substring, so the poller + live_session_store.end_streaming filters
            # suppress it from rendering as a chat bubble. It is a plain user
            # message (not an SDK TaskNotificationMessage), so it cannot itself
            # emit a task_notification event; combined with mark_notified keyed on
            # uuid, the same completion can never re-trigger this branch.
            if (
                stream_state.pending_task_notifications
                and session_id not in _stopped_sessions
                and session_id not in _interrupt_pending_sessions
            ):
                pending = stream_state.pending_task_notifications
                stream_state.pending_task_notifications = []  # clear before handing off

                synth_prompt = _build_task_notification_prompt(pending)

                ctx.debug(
                    logger,
                    f"Unpark: delivering {len(pending)} background task notification(s) as a follow-up turn",
                )

                # Persist as a user message (same call the queue path uses).
                await run_sync(
                    database.add_session_message,
                    session_id=session_id,
                    role="user",
                    content=synth_prompt,
                    metadata=None,
                    profile_id=profile_id,
                )

                # Broadcast it (filters strip the <task-notification> bubble).
                await sync_engine.broadcast_message_added(
                    session_id=session_id,
                    message={
                        "role": "user",
                        "content": synth_prompt,
                        "profile_id": profile_id,
                    },
                    source_device_id=None,
                )

                # End current streaming state so the follow-up query can re-init.
                await live_session_store.end_streaming(
                    session_id=session_id,
                    metadata=stream_state.final_metadata,
                    interrupted=False,
                )

                try:
                    task = asyncio.create_task(
                        self.run_query(synth_prompt, session_id, profile_id, project_id, None, worktree_id)
                    )
                    async with _active_sessions_lock:
                        _active_chat_sessions[session_id] = task
                    self.query_task = task
                    handed_off_to_queued = True
                    ctx.debug(logger, "Unpark: follow-up query started (handed off)")
                    return  # Exit this run_query — the follow-up task continues streaming
                except Exception as exc:
                    # Best-effort: if the follow-up couldn't start, fall through to
                    # normal completion (the notification was already deduped, so it
                    # won't retrigger — the parent simply won't unpark this time).
                    ctx.error(logger, f"Unpark: failed to start follow-up query: {exc}")

            # --- Pre-close queue processing ---
            # Before ending the session, check for queued messages.  If any
            # exist, pop the next one and start a new query *before* the
            # session state is torn down.  This prevents the race where
            # the frontend receives "done", navigates away, and the 0.5s
            # delay in the finally-block never gets a chance to fire.
            if session_id not in _stopped_sessions and session_id not in _interrupt_pending_sessions:
                queued_msg = await run_sync(database.pop_next_queued_message, session_id)
                if queued_msg:
                    q_content = queued_msg["content"]
                    q_profile = queued_msg.get("profile_id") or profile_id
                    q_project = queued_msg.get("project_id") or project_id

                    ctx.debug(logger, f"Queue: processing queued message before session close: {q_content[:80]}...")

                    # Broadcast updated (now smaller) queue to all devices
                    remaining_queue = await run_sync(database.get_queued_messages, session_id)
                    await sync_engine.broadcast_queue_updated(session_id, remaining_queue)

                    # Store user message in DB (detect slash commands)
                    _msg_role = "user"
                    _msg_meta = None
                    if q_content.strip().startswith("/"):
                        _parts = q_content.strip().split(None, 1)
                        _msg_role = "command"
                        _msg_meta = {
                            "command_name": _parts[0],
                            "command_args": _parts[1] if len(_parts) > 1 else "",
                        }
                    await run_sync(
                        database.add_session_message,
                        session_id=session_id,
                        role=_msg_role,
                        content=q_content,
                        metadata=_msg_meta,
                        profile_id=q_profile,
                    )

                    # Broadcast user message to all devices
                    await sync_engine.broadcast_message_added(
                        session_id=session_id,
                        message={
                            "role": "user",
                            "content": q_content,
                            "profile_id": q_profile,
                        },
                        source_device_id=None,
                    )

                    # End current streaming state so the new query can re-init
                    await live_session_store.end_streaming(
                        session_id=session_id,
                        metadata=stream_state.final_metadata,
                        interrupted=False,
                    )

                    # Start new query inline — session stays active, no gap
                    try:
                        task = asyncio.create_task(
                            self.run_query(q_content, session_id, q_profile, q_project, None, worktree_id)
                        )
                        async with _active_sessions_lock:
                            _active_chat_sessions[session_id] = task
                        self.query_task = task
                        # Mark that ownership passed to the successor task. The
                        # finally block will skip live_session_store cleanup,
                        # sync_engine streaming teardown, and device re-register
                        # to avoid racing with the successor's setup.
                        handed_off_to_queued = True
                        ctx.debug(logger, "Queue: new query started for queued message (handed off)")
                        return  # Exit this run_query — the new task continues streaming
                    except Exception as exc:
                        # Re-queue on failure so the message isn't lost
                        ctx.error(logger, f"Queue: failed to start query for queued message, re-queuing: {exc}")
                        await run_sync(
                            database.requeue_message_at_front,
                            session_id=session_id,
                            content=q_content,
                            profile_id=q_profile if q_profile != "claude-code" else None,
                            project_id=q_project,
                        )
                        queue = await run_sync(database.get_queued_messages, session_id)
                        await sync_engine.broadcast_queue_updated(session_id, queue)
                        # Fall through to normal completion

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
            ctx.error(logger, f"Query error: {e}", exc_info=True)
            # Surface the actual error to the user instead of a generic message
            error_detail = str(e) if str(e) else type(e).__name__
            await self.send_json({"type": "error", "message": f"Error: {error_detail}"})

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

            if handed_off_to_queued:
                # Ownership of session_id streaming state belongs to the
                # successor run_query task spawned in the pre-close queue
                # branch. Any cleanup / broadcast / device re-register here
                # would race with its setup and leave the session locked.
                # Only safely drop our task-reference if it is still us.
                current_task = asyncio.current_task()
                if _active_chat_sessions.get(session_id) is current_task:
                    # Successor replaced this entry at hand-off time, so this
                    # branch normally won't fire — guard is defensive only.
                    del _active_chat_sessions[session_id]
                ctx.debug(logger, "run_query finally: handed off to queued successor — skipping cleanup")
            else:
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
                if self.websocket is not None and self.websocket.client_state == WebSocketState.CONNECTED:
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

                # Fallback queue processing for error/cancel paths.
                # The normal completion path handles queued messages pre-close
                # (before end_streaming), so this only fires when:
                #  - Query was cancelled (CancelledError) or errored
                #  - A queued message arrived during the cancel/error teardown
                if session_id in _interrupt_pending_sessions:
                    ctx.debug(logger, "Skipping queue processing — interrupt_for_query pending")
                elif session_id in _stopped_sessions:
                    _stopped_sessions.discard(session_id)
                    ctx.debug(logger, "Skipping queue processing — user stopped session")
                else:
                    # Check if a new query was already spawned by pre-close processing
                    async with _active_sessions_lock:
                        active_task = _active_chat_sessions.get(session_id)
                    if active_task and not active_task.done():
                        ctx.debug(logger, "Skipping fallback queue processing — new query already active")
                    else:
                        try:
                            await self._process_next_queued(session_id)
                        except Exception as queue_err:
                            logger.warning(f"[Queue] Fallback queue processing failed: {queue_err}")
