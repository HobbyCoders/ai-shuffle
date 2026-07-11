"""
Headless chat query runner.

Launches a Claude query without an attached WebSocket — used by the
`/sessions/with-worktree` REST endpoint so the user can send a prompt,
lock their phone, and come back to a completed (or still-streaming)
session. The query task is registered in ``_active_chat_sessions`` so
``/stop`` and reconnect-attach work identically to a WS-launched query.

Design
------
Reuses ``ChatWebSocketHandler.run_query`` verbatim by subclassing and:

1. Skipping the WebSocket field entirely (``self.websocket = None``).
2. Overriding ``send_json`` to be a silent no-op — state propagates to
   registered devices via ``sync_engine`` broadcasts inside
   ``dispatch_event`` (the run_query pipeline already does this for
   every streamed chunk). Direct-to-WS sends from run_query (``start``,
   heartbeats, final events) are swallowed silently.
3. Using a synthetic ``device_id`` that is never registered with
   ``sync_engine``, so unregister/register calls inside run_query
   become harmless no-ops.

Limitations (deliberate — MVP):
- Permission and user-question events are stored server-side by
  ``permission_handler`` / ``user_question_handler`` regardless of
  ``broadcast_func`` success, so they survive until the user reconnects
  and polls for pending items. The real-time push to an already-
  connected device is skipped — this is fine for the "lock phone and
  come back" UX and can be upgraded later to bridge events through
  ``sync_engine`` if needed.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

from app.api.chat_ws_handler import ChatWebSocketHandler, WebSocketRateLimiter
from app.core.config import settings

logger = logging.getLogger(__name__)


class HeadlessChatHandler(ChatWebSocketHandler):
    """Stand-in ``ChatWebSocketHandler`` with no WebSocket attached."""

    def __init__(self, user_id: str, api_user_id: Optional[str]):
        # Deliberately skip the parent __init__ — it sets websocket-related
        # fields that we do not have. Mirror the fields run_query reads.
        self.websocket = None  # type: ignore[assignment]
        self.device_id = f"headless-{uuid.uuid4()}"
        self.user_id = user_id
        self.api_user_id = api_user_id

        self.current_session_id: Optional[str] = None
        self.query_task: Optional[asyncio.Task] = None
        # send_json short-circuits on the websocket-None branch, but we also
        # want the fast-path early return (line 142) to fall through so
        # broadcast_func-wrapped permission events can still propagate to
        # sync_engine via send_json -> short-circuit True.
        self.connection_healthy = True

        self.rate_limiter = WebSocketRateLimiter(
            max_messages=settings.ws_rate_limit_messages,
            window_seconds=settings.ws_rate_limit_window_seconds,
        )
        self.send_lock = asyncio.Lock()
        self._ws_seq: int = 0
        self._ws_drops: int = 0
        self._handlers: Dict[str, Any] = {}

        # Terminal-frame capture for socketless callers (automation runs read
        # these to stamp cost/failure onto the run row). run_query reports the
        # turn outcome only as direct frames to the (absent) socket, so sniff
        # them in send_json before the websocket-None short-circuit swallows
        # them. A successor turn on the same handler (account-failover retry,
        # compact-boundary auto-continue) overwrites — latest turn wins.
        self.last_turn_metadata: Dict[str, Any] = {}
        self.last_turn_error: Optional[str] = None
        self.last_turn_interrupted: bool = False

    async def send_json(self, data: dict) -> bool:
        frame_type = data.get("type")
        if frame_type == "done":
            self.last_turn_metadata = data.get("metadata") or {}
            # A clean successor completion supersedes an earlier turn's error.
            self.last_turn_error = None
        elif frame_type == "error":
            self.last_turn_error = str(data.get("message") or "unknown error")
        elif frame_type in ("interrupted", "stopped"):
            self.last_turn_interrupted = True
        return await super().send_json(data)


async def launch_headless_query(
    *,
    prompt: str,
    session_id: str,
    profile_id: str,
    project_id: Optional[str],
    user_id: str,
    api_user_id: Optional[str],
    worktree_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    deferred_delivery: bool = False,
) -> HeadlessChatHandler:
    """Spawn ``run_query`` without an attached WebSocket.

    Registers the task in ``_active_chat_sessions`` so ``/stop`` and
    reconnect-attach paths behave identically to a WS-launched query.

    ``deferred_delivery=True`` marks the turn server-initiated (the #543
    contract): run_query keeps watching devices registered with sync_engine so
    the turn streams live to any open tab via broadcasts, instead of only
    surfacing on reload. Automation runs use this; the interactive
    ``/sessions/with-worktree`` path keeps the default.

    Returns the handler; the scheduled task is ``handler.query_task`` and the
    sniffed turn outcome lands on ``handler.last_turn_metadata`` /
    ``last_turn_error`` / ``last_turn_interrupted`` once the task completes.
    """
    from app.api.websocket import _active_chat_sessions, _active_sessions_lock

    handler = HeadlessChatHandler(user_id=user_id, api_user_id=api_user_id)
    handler.current_session_id = session_id

    # Cancel any existing query for this session so we don't double-spawn.
    async with _active_sessions_lock:
        existing = _active_chat_sessions.get(session_id)
    if existing:
        existing.cancel()
        try:
            await existing
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Existing query for {session_id} raised during cancel: {e}")

    task = asyncio.create_task(
        handler.run_query(
            prompt=prompt,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            overrides=overrides,
            worktree_id=worktree_id,
            deferred_delivery=deferred_delivery,
        ),
        name=f"headless-run_query-{session_id}",
    )
    handler.query_task = task

    async with _active_sessions_lock:
        _active_chat_sessions[session_id] = task

    def _on_done(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.error(
                f"Headless run_query failed for session {session_id}: {exc}",
                exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return handler
