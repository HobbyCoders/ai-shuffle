"""
ChatWebSocketHandler — class-based handler for the primary chat WebSocket.

Extracted from the nested-closure `chat_websocket` function in websocket.py.
All instance state replaces former `nonlocal` variables; nested functions
become methods.  Behaviour is identical to the original implementation.
"""

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import HTTPException
from fastapi.websockets import WebSocketState
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.core import account_failover
from app.core.config import settings
from app.core.connectors.manager import connector_manager
from app.core.message_resolver import message_resolver, resolve_working_dir
from app.core.permission_handler import permission_handler
from app.core.streaming_context import StreamingContext
from app.core.sync_engine import sync_engine
from app.core.user_question_handler import user_question_handler
from app.core.webhook_service import dispatch_session_complete, dispatch_session_error
from app.db import database
from app.db.async_database import run_sync
from app.db.sessions import SessionRow
from app.api.worktree_session_meta import enrich_session_worktree_meta
from app.api.ws_utils import safe_ws_send, WSPayloadEncodeError

logger = logging.getLogger(__name__)


# v2 core (Phase 2 A6b): the command layer + durable turns table the WS handlers
# delegate to. The v2 turns path is now unconditional (the legacy pipeline and
# its gating flag are gone). Lightweight modules (no import cycle: turn_manager
# imports only app.db.turns).
from app.core import turn_manager  # noqa: E402
from app.db import turns as turns_db  # noqa: E402


# Terminal background-task statuses that should wake the parent (mirror the SDK's
# TaskNotificationStatus literal — every TaskNotificationMessage is terminal).
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped"})


# Queue sources that render as user-visible queue chips (B3). Internal
# failover/auto_continue/unpark turns never appear in the queue UI.
_V2_QUEUE_SOURCES = ("user", "queue")


def _v2_queue_view(session_id: str) -> List[Dict[str, Any]]:
    """B3 — the user-visible queue for a session, re-sourced from the durable
    ``turns`` table (v2 path).

    Only ``source in ('user','queue')`` queued turns render as chips; the server
    sorts with the exact ``pop_ready`` ordering (``get_queued_turns`` already
    orders by ``position, created_at, id``) and emits SEQUENTIAL INTEGER
    positions (the legacy numbering convention) so the float ``turns.position``
    never leaks to the wire. ``id``←turn id (the ``query_queued`` ack carries the
    same turn id, so FE-held ids match for remove), ``content``←prompt,
    ``created_at``←ISO. The row shape matches ``queued_messages`` so the frontend
    needs zero changes.
    """
    rows: List[Dict[str, Any]] = []
    pos = 0
    for turn in turns_db.get_queued_turns(session_id):
        if turn.get("source") not in _V2_QUEUE_SOURCES:
            continue
        created = turn.get("created_at")
        if created is not None and hasattr(created, "isoformat"):
            created = created.isoformat()
        rows.append({
            "id": turn["id"],
            "session_id": session_id,
            "content": turn.get("prompt") or "",
            "profile_id": turn.get("profile_id"),
            "project_id": turn.get("project_id"),
            "position": pos,
            "created_at": created,
        })
        pos += 1
    return rows

# How long the two-phase ``interrupt_for_query`` waits for the interrupted
# streaming task to drain naturally before force-cancelling it. The frontend's
# interrupt-confirm window must exceed this plus the SDK interrupt timeout
# (see message-sender.ts).
_INTERRUPT_DRAIN_TIMEOUT_SECONDS = 8.0


def register_terminal_notification_callback() -> None:
    """Register the chat layer's guaranteed terminal-notification callback.

    Called ONCE from ``startup.start_services``. From then on, every
    error/killed transition of the parent's OWN background work in
    ``task_registry.complete_task`` fires ``_on_task_terminal`` exactly once
    per task — covering the kill paths that previously surfaced NOTHING to the
    parent (force-eviction reaps, ghost reaps, pool teardown).
    """
    from app.core.task_registry import task_registry

    task_registry.set_terminal_notification_callback(_on_task_terminal)


def _on_task_terminal(session_id: str, task: Dict[str, Any]) -> None:
    """SYNC registry callback: schedule the async terminal-surface work.

    ``complete_task`` is synchronous, so this only schedules; it must never
    raise (the registry also guards, belt-and-braces). No running loop (a
    sync/threaded caller) → nothing to schedule; the durable retention row
    (``_recent_terminal``) still covers /live for the window.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_surface_terminal_notification(session_id, task))
    t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


async def _surface_terminal_notification(session_id: str, task: Dict[str, Any]) -> None:
    """Guaranteed terminal surface for an error/killed background task (4b).

    1. LIVE chip: synthesize the same ``task_updated`` system chip the drain
       pump renders, once per task lifetime (shared ``mark_chipped`` latch, so
       a pump-rendered chip is never duplicated).
    2. DURABLE row: persist the chip via ``database.add_session_message`` so
       the kill survives a reload (the live buffer may already be reaped — the
       force-eviction case — or be reset by the restart that caused the kill).

    The legacy step 3 (the ``HeadlessChatHandler`` unpark handoff) is GONE (V2
    A7, B5): the v2 scheduler (idle_reader -> turn_scheduler.on_task_terminal)
    is the sole unpark producer, so this surface only records; it never spawns.
    """
    from app.core.task_registry import task_registry

    task_id = task.get("id")
    status = task.get("status") or "killed"
    summary = task.get("summary")
    data: Dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        # SystemMessage.svelte's task_updated branch prefers patch.status.
        "patch": {"status": status},
    }
    if task.get("reason"):
        data["reason"] = task.get("reason")
    # The card join key (mirror of the #540 enrichment): the frontend settles a
    # subagent card only when the terminal chip carries the launch tool_use_id
    # (the card's toolId). This SAME data dict feeds both the live chip
    # (record_drained_event) and the durable row, so both carry it.
    tool_use_id = task.get("toolUseId")
    if tool_use_id:
        data["tool_use_id"] = tool_use_id

    # 1) LIVE chip (once per task lifetime — shared latch with pump chips).
    # V2: append the chip to the session event log so the SSE feed renders it.
    if task_registry.mark_chipped(session_id, task_id, "terminal"):
        task_registry.mark_chipped(session_id, task_id, "start")
        try:
            from app.core.turn_recorder import record_task_settle_frame

            await record_task_settle_frame(
                session_id, task_id, status, summary,
                reason=task.get("reason"), tool_use_id=tool_use_id,
            )
        except Exception:
            logger.debug(
                "terminal-surface live chip failed for %s/%s",
                session_id, task_id, exc_info=True,
            )

    # 2) DURABLE row — survives reload / restart (mirrors _row_for_system).
    try:
        await run_sync(
            database.add_session_message,
            session_id=session_id,
            role="system",
            content=summary or "",
            metadata={"subtype": "task_updated", "system_data": data},
        )
    except Exception:
        logger.debug(
            "terminal-surface durable row failed for %s/%s",
            session_id, task_id, exc_info=True,
        )

    # NOTE (V2 A7, B5): the legacy step 3 — the ``HeadlessChatHandler`` unpark
    # handoff via ``_deliver_unpark_impl`` — has been REMOVED. The v2 scheduler
    # (idle_reader -> turn_scheduler.on_task_terminal) is now the sole unpark
    # producer for background-task completions/errors/kills, so firing a second
    # unpark from here would double-wake the parent. Steps 1 (live chip) and 2
    # (durable row) stay: they are the guaranteed terminal SURFACE for a
    # force-eviction/ghost-reap/pool-teardown kill that never flows a frame
    # through the reader. The remaining accepted regression is that an explicit
    # user-stop of a LIVE background sub-agent (which emits no CLI frame) no
    # longer auto-wakes the parent with a <task-notification>; the durable chip
    # still records it. Rollback is a git revert, not a flag flip.


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
            "watch_session": self.handle_watch_session,
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
                await safe_ws_send(self.websocket, data)
                return True
            except WSPayloadEncodeError as e:
                # Non-serializable payload (e.g. a raw datetime from a DB row) —
                # a bug in this one message, not a dead socket. Drop it but keep
                # the connection healthy so a later queue drain can still send.
                self._ws_drops += 1
                msg_type = data.get("type") or data.get("event_type", "unknown")
                logger.warning(
                    f"Dropping non-serializable WebSocket message "
                    f"[type={msg_type}, device={self.device_id}]: {e}"
                )
                return False
            except Exception as e:
                self._ws_drops += 1
                self.connection_healthy = False
                msg_type = data.get("type") or data.get("event_type", "unknown")
                logger.warning(
                    f"WebSocket send failed [seq={self._ws_seq}, type={msg_type}, "
                    f"device={self.device_id}, total_drops={self._ws_drops}]: {e}"
                )
                # A send timeout means the socket is half-open (dead peer, full
                # buffer) — starlette still reports it CONNECTED, so the receive
                # loop would idle on this zombie forever and its finally-cleanup
                # (device deregistration) would never run. Force-close in the
                # background to release the transport and unblock receive_json.
                self._force_close_dead_socket()
                return False

    def _force_close_dead_socket(self) -> None:
        """Best-effort background close of a connection marked dead.

        Fire-and-forget: close() itself can block on the same dead transport,
        so it gets its own timeout and every failure is swallowed — by this
        point the connection is already excluded from sends via
        connection_healthy, this just accelerates resource cleanup.
        """
        ws = self.websocket
        if ws is None:
            return

        async def _close() -> None:
            try:
                await asyncio.wait_for(ws.close(code=1011), timeout=5.0)
            except Exception:  # noqa: BLE001
                pass

        try:
            task = asyncio.get_running_loop().create_task(_close())
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        except RuntimeError:
            pass  # no running loop (teardown) — nothing to accelerate

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
        # Sourced from the durable runs table (v2 core): one lease-valid-runs ⋈
        # sessions query, same outgoing frame/shape as the legacy in-memory map.
        from app.core import streaming_status
        user_streaming = await asyncio.to_thread(
            streaming_status.get_user_streaming_sessions, self.user_id
        )
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
                        # A half-open socket stays CONNECTED forever in
                        # starlette's view; a send timeout is what actually
                        # detects the death. Exit so the finally-cleanup
                        # deregisters this device instead of idling as a zombie.
                        if not self.connection_healthy:
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
                        # No more devices watching and no active query — nothing
                        # to tear down here (V2: busy truth lives in the runs
                        # table; sessions age out via pool eviction).
                        logger.debug(f"Last device disconnected from session {self.current_session_id}")

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

    async def _try_auto_adopt(
        self, session_id: str, session_row: "SessionRow"
    ) -> bool:
        """Take ownership of a remotely-owned session so THIS send runs locally.

        Sending from a second device is the user saying "continue here", so a
        session that merely INHERITED its owner (raw ``placement`` NULL — the
        A11 implicit pin) follows them. Guards, all required:

          * raw ``placement`` is NULL/blank — an explicit ``{"where": …}`` is a
            deliberate placement (composer device pick, /move, /adopt, a claim
            pin) and is NEVER overridden here;
          * no live run — never wrestle an in-flight turn away from its owner
            (same rule the /adopt endpoint enforces);
          * the project's base checkout exists HERE, or the session has no
            project — otherwise the turn would spawn against a path that does
            not exist on this disk.

        The write is ``adopt_session_follow`` (instance_id only, no pin) so the
        session stays freely adoptable by the next device. Returns True when the
        turn should now run locally; False — for a failed guard OR any error —
        leaves the caller on today's remote-dispatch path (fail-open).
        """
        me = settings.effective_instance_id
        try:
            from app.core import placement as placement_mod

            # Reuse the resolver's own normalizer: Postgres JSONB hands back a
            # dict, the SQLite test shim a JSON string.
            if placement_mod._normalize_placement(session_row.get("placement")):
                return False

            from app.core import streaming_status

            if await run_sync(streaming_status.has_active_run, session_id):
                return False

            if session_row.get("project_v2_id") or session_row.get("project_id"):
                from app.core import project_resolver

                has_base = await run_sync(
                    project_resolver.has_local_base_checkout, session_row, me
                )
                if not has_base:
                    return False

            updated = await run_sync(
                database.adopt_session_follow, session_id, me
            )
            if not updated:
                return False
            from app.api.sessions_helpers import apply_sdk_session_keep

            await run_sync(apply_sdk_session_keep, updated, me)
            # Keep-predicate may have NULLed sdk_session_id on the stored row.
            session_row["sdk_session_id"] = updated.get("sdk_session_id")
        except Exception:  # noqa: BLE001 — auto-adopt is an optimization only
            logger.debug(
                "Auto-adopt skipped for session %s — dispatching remotely",
                session_id, exc_info=True,
            )
            return False

        # Keep the caller's copy of the row consistent with what was just
        # written, so anything reading it downstream sees the new owner.
        session_row["instance_id"] = me
        logger.info(
            "Auto-adopted session %s on send (owner -> %s, placement left NULL)",
            session_id, me,
        )
        return True

    async def _validate_target_install(
        self, target_instance_id: str, project_v2_id: Optional[str]
    ) -> bool:
        """Preflight the composer's device pick for a NEW session.

        Mirrors the ``POST /sessions/{id}/move`` preflight — the target must be
        a registered install that is online, and (when the session carries a
        project) must have an ACTIVE BASE checkout of that project
        (``has_active_base_checkout``, not ``capable_runners``: the latter
        also matches branch-only worktrees, and Phase 3 never clones). The
        caller passes the already-resolved ``project_v2_id`` (legacy
        ``project_id`` is resolved upstream). The checkout half fails OPEN on
        lookup trouble, exactly like /move: a preflight bug must not make a
        usable device unpickable. Sends the refusal frame itself and returns
        False when the pick cannot be honored.
        """
        async def _refuse(message: str) -> bool:
            await self.send_json({"type": "error", "message": message})
            return False

        install = await run_sync(database.get_install, target_instance_id)
        if not install:
            return await _refuse("Unknown device — pick another device.")
        device = install.get("device_name") or target_instance_id
        if not install.get("approved", True):
            return await _refuse(
                f"{device} is waiting for admin approval. Approve and restart it first."
            )
        if not install.get("online"):
            return await _refuse(
                f"{device} is offline. Bring it online or pick another device."
            )

        if project_v2_id:
            from app.db import checkouts as checkouts_db

            try:
                if not await run_sync(
                    checkouts_db.has_active_base_checkout,
                    project_v2_id,
                    target_instance_id,
                ):
                    return await _refuse(
                        f"This project isn't checked out on {device} yet. Open "
                        "the project there once (or clone it), then try again."
                    )
            except Exception:  # noqa: BLE001 — fail open on lookup trouble
                logger.warning(
                    "Device-pick preflight errored for project_v2 %s on %s; "
                    "proceeding fail-open", project_v2_id, target_instance_id,
                    exc_info=True,
                )
        return True

    async def _lift_uploads(
        self,
        upload_paths: List[str],
        project_id: Optional[str],
        session_id: str,
    ) -> List[str]:
        """Lift this send's staged uploads into durable blobs (D8).

        Raises ``UploadLiftError`` (or anything the DB layer raises) so the
        caller can refuse the send — never returns partial results.
        """
        from app.core import upload_transfer

        if not project_id:
            raise upload_transfer.UploadLiftError(
                "Attachments can only be sent to another device from a project "
                "session."
            )
        project = await run_sync(database.get_project, project_id)
        if not project:
            raise upload_transfer.UploadLiftError(
                f"Project not found: {project_id}"
            )
        project_v2_id = None
        try:
            from app.core import project_resolver

            resolved = await run_sync(
                project_resolver.resolve_project_v2,
                {"project_id": project_id, "id": session_id},
            )
            project_v2_id = (resolved or {}).get("id")
        except Exception:  # noqa: BLE001 — the v2 id is bookkeeping, not a gate
            logger.debug(
                "Upload lift: project_v2 resolve failed for %s", project_id,
                exc_info=True,
            )
        return await run_sync(
            upload_transfer.lift_paths_to_blobs,
            upload_paths,
            project,
            session_id,
            project_v2_id,
        )

    async def handle_query(self, data: dict) -> None:
        """Handle 'query' message type."""

        prompt = data.get("prompt", "").strip()
        session_id = data.get("session_id")
        profile_id = data.get("profile", "claude-code")
        project_id = data.get("project") or None
        if isinstance(project_id, str):
            project_id = project_id.strip() or None
        project_v2_id = data.get("project_v2_id") or None
        if isinstance(project_v2_id, str):
            project_v2_id = project_v2_id.strip() or None
        overrides = data.get("overrides")  # Optional: {model, permission_mode}
        # Client-generated adoption token for a NEW-session query. Echoed back in
        # both the ``session_created`` and the first ``start`` frame so the
        # frontend can authenticate that the minted session is genuinely the one it
        # asked for — not a foreign session's server-initiated turn (unpark / queue
        # drain) unparking on the shared device socket during its awaiting window.
        # Only meaningful for a new session (session_id is None); ignored otherwise.
        request_id = data.get("request_id")
        # Composer device pick: run this NEW session on another install. Only
        # honored at creation — an existing session's placement is settled, so
        # the key is dropped rather than allowed to silently re-route history.
        target_instance_id = (data.get("target_instance_id") or "").strip() or None
        if session_id or target_instance_id == settings.effective_instance_id:
            target_instance_id = None
        # Staged upload paths referenced by this prompt (D8). They are absolute
        # paths on THIS install's disk, so they only need carrying when the turn
        # ends up running somewhere else; a local turn ignores them entirely.
        upload_paths = [
            p for p in (data.get("upload_paths") or []) if isinstance(p, str) and p
        ]

        if not prompt:
            await self.send_json({"type": "error", "message": "Empty prompt"})
            return

        # Origin access when composing against a project. Neither id →
        # projectless create (today's behavior, not a 400). Both → 400.
        # WS has no Request — pass the api_user dict.
        resolved_pv2_id = None
        if project_id or project_v2_id:
            from app.api.common import require_origin_access

            try:
                user = None
                if self.api_user_id:
                    user = await run_sync(database.get_api_user, self.api_user_id)
                access = await run_sync(
                    require_origin_access,
                    api_user=user,
                    project_id=project_id,
                    project_v2_id=project_v2_id,
                )
            except HTTPException as exc:
                await self.send_json({"type": "error", "message": exc.detail})
                return
            resolved_pv2_id = (access.get("project_v2") or {}).get("id")

        # Where does this session EXECUTE?
        #
        # V2 placement: resolve via sessions.placement. A session placed on
        # ANOTHER runner still takes the LOCAL enqueue path below — the durable
        # queued turn (submitted with ``no_kick``) IS the whole hand-off; the
        # owning runner's turn_claim loop picks it up via the NOTIFY wake (or
        # its 5s poll) and rendering rides the shared SSE feed exactly like a
        # local turn. No HTTP dispatch (OD-1). Headless handlers do neither
        # (websocket is None) — a headless query always runs locally.
        placed_remote = False
        if session_id and self.websocket is not None:
            cloud_sess = await run_sync(database.get_session, session_id)
            # V2 placement: resolve via sessions.placement. A session placed on
            # ANOTHER runner still takes the LOCAL enqueue path below — the
            # durable queued turn IS the whole hand-off (PR-B collapsed the
            # legacy cloud-relay dispatch unconditionally).
            if cloud_sess:
                from app.core import placement as placement_mod
                decision = None
                try:
                    decision = await run_sync(
                        placement_mod.resolve_placement, cloud_sess
                    )
                except Exception:  # noqa: BLE001 — resolver trouble ⇒ run local
                    logger.warning(
                        f"Placement resolution failed for {session_id} — "
                        f"treating as local", exc_info=True,
                    )
                if decision is not None and decision.kind == "none":
                    # Honest degraded state (hazard 3): the pinned runner is
                    # offline / no capable runner exists. The session stays
                    # readable; sending resumes when the runner returns.
                    await self.send_json({
                        "type": "error",
                        "message": "No capable device online for this session "
                                   "— its runner is offline or lacks the "
                                   "required capabilities.",
                    })
                    return
                if (
                    decision is not None
                    and decision.kind == "runner"
                    and decision.runner_id != settings.effective_instance_id
                ):
                    # Sending from a device that does not own this session
                    # ADOPTS it (ownership follows the user) when the session
                    # was never explicitly placed. Anything in the way — an
                    # explicit pin, a live run, a missing local checkout, an
                    # error — leaves today's remote-dispatch behaviour intact.
                    adopted = await self._try_auto_adopt(session_id, cloud_sess)
                    placed_remote = not adopted

        # Create or get session
        if not session_id:
            if upload_paths and not project_id:
                from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT

                await self.send_json({
                    "type": "error",
                    "message": ATTACHMENTS_NEED_LOCAL_CHECKOUT,
                })
                return
            if target_instance_id:
                # Refuse BEFORE creating anything — a session pinned to a device
                # that cannot run it is stranded, not merely slow.
                if not await self._validate_target_install(
                    target_instance_id, resolved_pv2_id
                ):
                    return
                # The session is born owned by (and pinned to) the target, so
                # this turn hands off exactly like any remotely-placed one.
                placed_remote = True

            # Create new session
            session_id = str(uuid.uuid4())
            is_new_session = True
            # Generate title from the user's first message
            title = prompt[:100].strip()
            if len(prompt) > 100:
                title = title.rsplit(' ', 1)[0] + "..."

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
                        is_active=True,
                        target_instance_id=target_instance_id,
                        project_v2_id=resolved_pv2_id,
                        # The composer's pick IS the session's binding (v79) —
                        # agents no longer pin a model, so stamp it at birth.
                        # A ``prov:`` composite also settles provider_id here,
                        # before anything reads the row.
                        model=(overrides or {}).get("model"),
                        effort=(overrides or {}).get("effort"),
                        permission_mode=(overrides or {}).get("permission_mode"),
                        context_window=(overrides or {}).get("context_window"),
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

            # V2 projects: bind the new session to its origin-keyed project_v2 so
            # the resolver is a DIRECT hit on the first send. create_session
            # already stamps project_v2_id when we resolved it above; this
            # self-heal covers the project_id-only path if that stamp missed.
            # Best-effort: a failure defers to the send-path self-heal.
            if project_id and not resolved_pv2_id:
                try:
                    from app.core import project_resolver
                    await run_sync(
                        project_resolver.resolve_project_v2,
                        {"project_id": project_id, "id": session_id},
                    )
                except Exception:  # noqa: BLE001 — self-heal covers it later
                    logger.debug(
                        "V2 project bind failed for new session %s", session_id,
                        exc_info=True,
                    )

            # Immediately notify frontend of new session_id so it persists on refresh.
            # Echo the client's adoption token so the awaiting frontend accepts this
            # session_created ONLY if it matches — authenticating new-session adoption.
            session_created_frame = {
                "type": "session_created",
                "session_id": session_id,
                "title": title,
                # V2: the session was just created on the V2 streaming path —
                # echo it so the client attaches the typed event feed.
                "v2_stream": True
            }
            if request_id:
                session_created_frame["request_id"] = request_id
            await self.send_json(session_created_frame)

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

        if not is_new_session:
            # Backfill title from first user message for sessions created without
            # one (e.g. via the auto-worktree REST endpoint, which creates the
            # session before any user prompt exists). Harmless if this query is
            # later preempted — the title derives from a prompt that will still
            # run via the queue. (The user-message persist itself happens further
            # down, only after this query WINS the check-and-register.)
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

        # v2 turns cutover: submit a durable ``source=user`` turn instead of
        # spawning run_query. ``submit`` enqueues then kicks the scheduler, which
        # starts the turn immediately when the session is idle or leaves it queued
        # behind the live turn (the unique live index is the busy-write guard —
        # a busy-session query queues durably, no cancel-and-requery). Rendering
        # rides SOLELY the SSE typed-event feed; new-session adoption is
        # authenticated by the ``session_created`` request_id echo already sent
        # above (B4) — no origin-socket passthrough, no start-frame token.
        # Persist + broadcast the user message (P3 session_messages dual-write
        # survives — the recorder also logs ``message.user`` to the event
        # feed). New sessions persisted their first message atomically at
        # creation; existing sessions persist here. Ordered before submit is
        # safe: v2 ``submit`` always enqueues durably (never a
        # preempted-and-discarded query), so there is no double-persist race.
        try:
            if not is_new_session:
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
            await sync_engine.broadcast_message_added(
                session_id=session_id,
                message={
                    "role": "user",
                    "content": prompt,
                    "profile_id": profile_id,
                },
                source_device_id=self.device_id,
            )
        except Exception as persist_exc:  # noqa: BLE001
            logger.warning(
                f"[v2] user-message persist/broadcast failed for session "
                f"{session_id} (turn will still run): {persist_exc}"
            )

        # A turn already live for the session means this query queues behind
        # it (busy-write guard implicit). Checked BEFORE submit — submit's
        # kick does not make a turn live synchronously, so a post-submit read
        # could still see the previous live turn.
        had_live = (
            await run_sync(turns_db.get_live_turn, session_id) is not None
        )

        payload: Dict[str, Any] = {}
        if request_id and is_new_session:
            payload["request_id"] = request_id
        if self.api_user_id:
            payload["api_user_id"] = self.api_user_id
        if getattr(self, "user_id", None):
            payload["user_id"] = self.user_id

        # The 'claude-code' no-profile sentinel is install-local — it must
        # never ride a turn row another install will execute (the cluster
        # profile-sentinel rule). Local turns keep it (today's behavior).
        submit_profile_id = profile_id
        if placed_remote and submit_profile_id == "claude-code":
            submit_profile_id = None

        # Uploads travel as durable blobs (D8) — but ONLY when the turn is
        # actually leaving this install. A local turn's files are already on the
        # disk the agent will read, so lifting them would just duplicate bytes.
        # A lift failure FAILS THE SEND: submitting a turn whose prompt points at
        # files the executing install cannot see is exactly the silent loss this
        # mechanism exists to prevent.
        if placed_remote and upload_paths:
            if not project_id:
                from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT

                await self.send_json({
                    "type": "error",
                    "message": ATTACHMENTS_NEED_LOCAL_CHECKOUT,
                })
                return
            try:
                blob_ids = await self._lift_uploads(
                    upload_paths, project_id, session_id
                )
            except Exception as lift_exc:  # noqa: BLE001 — refuse, never half-send
                logger.warning(
                    "Upload lift failed for session %s: %s", session_id, lift_exc
                )
                await self.send_json({
                    "type": "error",
                    "message": (
                        f"Couldn't send your attachments to the other device: "
                        f"{lift_exc}"
                    ),
                })
                return
            if blob_ids:
                payload["upload_blob_ids"] = blob_ids

        turn_manager.submit(
            session_id,
            "user",
            prompt=prompt,
            profile_id=submit_profile_id,
            project_id=project_id,
            overrides=overrides or None,
            payload=payload or None,
            no_kick=placed_remote,
        )

        if placed_remote:
            # Wake the owning runner's claim loop NOW (latency only — its 5s
            # poll is the correctness path), then ack exactly like a queued
            # turn: the client leaves its optimistic streaming state and the
            # real stream arrives over the shared SSE feed once the owner
            # starts the turn.
            from app.core import turn_claim
            await run_sync(turn_claim.notify_turns_changed, session_id)
            await self._v2_broadcast_queue(session_id)
            await self.send_json(
                {"type": "query_queued", "session_id": session_id}
            )
            return

        if had_live:
            # Queued behind the live turn: surface the chip (re-sourced from
            # turns) and send the F5 query_queued ack so the client leaves its
            # optimistic streaming state instead of spinning forever.
            await self._v2_broadcast_queue(session_id)
            await self.send_json(
                {"type": "query_queued", "session_id": session_id}
            )
        return
    # ------------------------------------------------------------------
    # handle_stop
    # ------------------------------------------------------------------

    async def handle_stop(self, data: dict) -> None:
        """Handle 'stop' message type."""

        session_id = data.get("session_id") or self.current_session_id
        if session_id:
            # V2 placement drops any relay entirely: the durable
            # running->interrupting flip below is visible to whichever runner
            # executes the turn (its turn_claim observe pass drives the live
            # runtime), so stop needs no HTTP hop.
            # The CLI's interrupt is query-scoped: a plain interrupt already
            # tears down in-flight background sub-agents, so no per-task stop
            # loop is needed. (The old PRESERVE_BACKGROUND_ON_INTERRUPT
            # binary-patch opt-in that made one meaningful was removed with
            # the Agent SDK.)

            # v2 turns cutover: the ONE interrupt implementation. Flip the live
            # turn running->interrupting (durable intent) and fire the executor,
            # which drives the live runtime's SDK interrupt + evict/drain
            # escalation (the safety-timeout/eviction choreography now lives in
            # SessionRuntime, not here). A user stop settles the turn ``aborted``;
            # the settle path then drains queued turns like any other outcome (F4
            # preserve-on-stop retired, PR #694) — no _stopped_sessions latch.
            # The ``stop_background`` opt-in still tears down surviving background
            # tasks transport-side (the runtime interrupt does not carry it).
            # #585: bind the stop to the turn the FE believed was live (optional
            # — older clients omit it). turn_manager threads it to the DB flip
            # (id=turn_id guard) and the runtime signal so a stop that arrives
            # after its target settled cannot interrupt the successor turn.
            # Absent turn_id keeps the legacy session-scoped stop.
            target_turn_id = data.get("turn_id") or None
            flipped = turn_manager.interrupt(
                session_id,
                "stop",
                stop_background=False,
                turn_id=target_turn_id,
            )
            if flipped is not None:
                # Cross-runner wake: if the executing runtime lives on another
                # install, its turn_claim observe pass reacts on this NOTIFY
                # tick instead of its next 5s poll. Locally the executor
                # already fired; the observe pass is a guarded no-op (O4).
                from app.core import turn_claim
                await run_sync(turn_claim.notify_turns_changed, session_id)
            return

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

        session_id = data.get("session_id") or self.current_session_id
        if not session_id:
            await self.send_json({"type": "interrupt_failed", "session_id": session_id, "message": "No active session"})
            return

        # V2 placement drops any relay: the durable interrupt flip + the
        # drain-wait below both act on the SHARED turns row, so they are
        # cross-runner-correct as-is (the owner's observe pass drives its
        # runtime; our poll sees the row settle/drain).
        #
        # v2 turns cutover (two-phase send-now). Flip the live turn
        # running->interrupting with mode=send_now and fire the executor (SDK
        # interrupt + escalation in the runtime), then wait — with today's drain
        # timeout — until the live turn either SETTLES (clean stop) or ESCALATES
        # to ``draining`` (wedged CLI kept alive). Either outcome is safe to send
        # the replacement query after: the follow-up queued turn is held out of
        # the live slot by the unique live index until the draining turn settles
        # (F6), so it can never cross a mid-turn CLI. Confirm unconditionally so
        # the frontend's replacement query is unblocked.
        try:
            flipped = turn_manager.interrupt(session_id, "send_now")
            if flipped is not None:
                # Cross-runner wake (see handle_stop) — latency only.
                from app.core import turn_claim
                await run_sync(turn_claim.notify_turns_changed, session_id)

            async def _settled_or_draining() -> bool:
                live = await run_sync(turns_db.get_live_turn, session_id)
                return live is None or live.get("state") == "draining"

            loop = asyncio.get_event_loop()
            deadline = loop.time() + _INTERRUPT_DRAIN_TIMEOUT_SECONDS
            while not await _settled_or_draining():
                if loop.time() >= deadline:
                    logger.warning(
                        "interrupt_for_query(v2): drain wait timed out for "
                        "%s — confirming anyway (follow-up held by the "
                        "unique live index until the draining turn settles)",
                        session_id,
                    )
                    break
                await asyncio.sleep(0.05)
            await self.send_json({"type": "interrupt_confirmed", "session_id": session_id})
        except Exception as e:  # noqa: BLE001
            logger.error(f"interrupt_for_query(v2) failed for {session_id}: {e}")
            await self.send_json({
                "type": "interrupt_failed",
                "session_id": session_id,
                "message": str(e),
            })
        return

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

        # Unified message resolution: DB -> JSONL -> SDK (V2: no live tier).
        sdk_session_id = session.get("sdk_session_id")
        working_dir = resolve_working_dir(dict(session))

        resolved = message_resolver.resolve(session_id, sdk_session_id, working_dir)
        messages = resolved.messages
        logger.debug(f"Loaded {len(messages)} messages from {resolved.source} for session {session_id}")

        # Post-processing: merge profile_id for JSONL-sourced messages
        # (DB messages already include profile_id). Consume the
        # ordered per-content queue so each user message gets its own profile even
        # when the same prompt repeats under a changed profile, and carry that
        # turn's profile onto its assistant/thinking rows (JSONL stores none) so
        # each bubble keeps the profile that produced it instead of re-labelling to
        # the session's CURRENT profile on a switch. Emit both the snake_case and
        # camelCase keys the frontend may read.
        if resolved.source == "jsonl":
            from app.core.cost_utils import attach_turn_profile_ids
            attach_turn_profile_ids(session_id, messages)

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

        # Check if session is currently streaming (late-joining device) — busy
        # truth from the durable runs table (v2 core).
        from app.core import streaming_status
        is_streaming = streaming_status.has_active_run(session_id)
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

        # V2: no live-buffer override for streaming sessions — the persistent
        # chain above is the history, and a late joiner picks up the live tail
        # from the session event feed (SSE). Stale busy state self-heals via
        # the run-lease TTL in the runs table.

        # Add worktree info if session is linked to a worktree. Worktree sessions
        # are always reopenable — we never null worktree_id; worktree_deleted only
        # means "not materialized right now" (recreated on next send). See helper.
        worktree_id = session.get("worktree_id")
        worktree = database.get_worktree(worktree_id) if worktree_id else None
        wt_meta = enrich_session_worktree_meta(session, worktree)
        if wt_meta:
            session["worktree_id"] = wt_meta["worktree_id"]
            session["worktree_branch"] = wt_meta["worktree_branch"]
            session["worktree_path"] = wt_meta["worktree_path"]
            session["worktree_deleted"] = wt_meta["worktree_deleted"]

        # Build response for late-joiners
        history_response = {
            "type": "history",
            "session_id": session_id,
            "session": session,
            "messages": messages,
            "isStreaming": is_streaming,
        }
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
        # Include queued messages so frontend displays server-side queue. v2:
        # re-source the chips from the durable turns table (same wire shape).
        queued = await run_sync(_v2_queue_view, session_id)
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
    # handle_watch_session
    # ------------------------------------------------------------------

    async def handle_watch_session(self, data: dict) -> None:
        """Handle 'watch_session' — register this device for sync broadcasts.

        Ported verbatim from the live closure (websocket.py, watch_session
        branch). The frontend sends this on EVERY session switch
        (session-switcher.ts), so the contract here — including the
        dead-vs-live duplicate-socket asymmetry — is load-bearing:

        * old socket DEAD  -> replace it (page-refresh recovery)
        * old socket ALIVE -> skip re-registration (duplicate tab sharing the
          same device_id; re-registering would evict the live tab and cause a
          reconnect loop) but STILL bind current_session_id.

        Note the asymmetry vs load_session: load_session REJECTS a duplicate
        live socket with `session_already_open`; watch_session silently skips.
        """
        session_id = data.get("session_id")
        logger.debug(f"[watch_session] device={self.device_id} session_id={session_id}")
        if not session_id:
            return

        # Headless guard (plan step 6): a handler with no websocket must never
        # register itself as a watcher — sync_engine registration keys on
        # (device_id, websocket) and a None socket would pollute the watcher
        # table with an unsendable connection.
        if self.websocket is None:
            return

        # Check if this device_id is already registered from a different
        # WebSocket (e.g., another browser tab sharing localStorage device_id,
        # or the same device reconnecting after a page refresh).
        if sync_engine.is_device_watching_session(self.device_id, session_id):
            existing_conn = sync_engine._connections.get(session_id, {}).get(self.device_id)
            if existing_conn and existing_conn.websocket is not self.websocket:
                # Check if the old WS is still alive. If it's dead/closing,
                # this is a reconnection (e.g., page refresh) — let the new
                # WS take over. If it's alive, this is likely a duplicate tab
                # sharing the same device_id — skip to avoid a reconnect loop.
                old_ws_alive = existing_conn.websocket.client_state == WebSocketState.CONNECTED
                if old_ws_alive:
                    logger.debug(
                        f"Device {self.device_id} already watching session {session_id} "
                        f"from another live WS, skipping"
                    )
                    self.current_session_id = session_id
                    return
                else:
                    logger.debug(
                        f"Device {self.device_id} reconnecting "
                        f"(old WS state={existing_conn.websocket.client_state}), replacing"
                    )
            # Same WebSocket re-watching — proceed normally

        # Unregister from old session if switching
        if self.current_session_id and self.current_session_id != session_id:
            await sync_engine.unregister_device(self.device_id, self.current_session_id)

        await sync_engine.register_device(self.device_id, session_id, self.websocket)
        self.current_session_id = session_id
        logger.debug(f"Device {self.device_id} watching session {session_id}")

        # Restore the session's queued messages to this
        # device. The frontend resets its queue to [] on every
        # session switch and hydrates session state via HTTP
        # /live, which does NOT carry queue state (it's
        # server-side only). Without this, queued messages
        # vanish from the UI on switch / reconnect even though
        # they remain in the DB and still fire when the turn
        # ends. Mirror the queue_updated event emitted by the
        # queue_message / remove / clear handlers.
        try:
            watch_queue = await run_sync(
                _v2_queue_view, session_id
            )
        except Exception as exc:
            logger.warning(
                f"[Queue] Failed to load queued messages for "
                f"{session_id} on watch: {exc}"
            )
            watch_queue = []
        if watch_queue:
            await self.send_json({
                "type": "sync",
                "event_type": "queue_updated",
                "session_id": session_id,
                "data": {"queued_messages": watch_queue},
            })

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
            eff_remember = remember if decision != "defer" else None
            eff_pattern = pattern if decision != "defer" else None

            # The prompt is a shared ``pending_interactions`` row — ANY install
            # answers it directly against the table, no cloud_relay. The owning
            # runner's awaiting SDK callback wakes via NOTIFY (or in-process fast
            # path when we ARE the owner). Stamp the answering device (Q4-6).
            result = await permission_handler.respond(
                request_id=request_id,
                session_id=response_session_id,
                decision=decision,
                remember=eff_remember,
                pattern=eff_pattern,
                broadcast_func=self.send_json,
                answered_by_device=self.device_id,
            )
            logger.debug(f"Permission response processed (v2): {result}")
            await self.send_json({
                "type": "permission_response_ack",
                "session_id": response_session_id,
                "request_id": request_id,
                "result": result,
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
            # Pending/deferred are read straight from the shared
            # ``pending_interactions`` table, which every install sees — no
            # relay, so the reply is always authoritative (never `stale`).
            await self.send_json({
                "type": "pending_permissions",
                "session_id": self.current_session_id,
                "pending": permission_handler.get_pending_requests(self.current_session_id),
                "deferred": permission_handler.get_deferred_requests(self.current_session_id),
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
        # The modal's "X" dismisses without answers, and per-question "Skip"
        # sends the skipped question keys out-of-band (they carry no answer of
        # their own). Both still resolve the pending interaction, so neither may
        # be gated on `answers` being non-empty.
        dismissed = bool(data.get("dismissed"))
        raw_skipped = data.get("skipped")
        skipped = (
            [str(k) for k in raw_skipped if str(k)]
            if isinstance(raw_skipped, list)
            else []
        )

        if response_session_id and request_id and (answers or dismissed or skipped):
            # The question is a shared ``pending_interactions`` row — answer it
            # directly against the table (no cloud_relay); the owning runner's
            # awaiting SDK callback wakes via NOTIFY. Stamp the answering device
            # (Q4-6).
            result = await user_question_handler.respond(
                request_id=request_id,
                session_id=response_session_id,
                answers=answers,
                answered_by_device=self.device_id,
                dismissed=dismissed,
                skipped=skipped,
            )
            logger.debug(f"User question response processed (v2): {request_id} -> {result}")
            ack = {
                "type": "user_question_response_ack",
                "session_id": response_session_id,
                "request_id": request_id,
                "success": result,
            }
            if not result:
                ack["reason"] = "not_pending"
            await self.send_json(ack)
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
            # Pending questions are read straight from the shared
            # ``pending_interactions`` table, which every install sees — no
            # relay, so the reply is always authoritative (never `stale`).
            await self.send_json({
                "type": "pending_questions",
                "session_id": self.current_session_id,
                "questions": user_question_handler.get_pending_questions(self.current_session_id),
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
    # _v2_broadcast_queue — re-source + fan out the queue_updated frame
    # ------------------------------------------------------------------

    async def _v2_broadcast_queue(self, session_id: str) -> None:
        """Build the B3 queue view from the durable turns table and emit the
        ``queue_updated`` frame both to the other devices (via the sync engine)
        and directly to THIS socket (which is unregistered from the sync engine
        while streaming, so the broadcast alone would not reach it). Same wire
        shape as the legacy ``queued_messages`` path — the frontend is unchanged.
        """
        queue = await run_sync(_v2_queue_view, session_id)
        await sync_engine.broadcast_queue_updated(session_id, queue)
        await self.send_json({
            "type": "sync",
            "event_type": "queue_updated",
            "session_id": session_id,
            "data": {"queued_messages": queue},
        })

    # ------------------------------------------------------------------
    # handle_queue_message
    # ------------------------------------------------------------------

    async def handle_queue_message(self, data: dict) -> None:
        """Handle 'queue_message' — add a message to the server-side queue."""

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

        # V2 placement: the durable queued turn IS the hand-off (see
        # handle_message) — enqueue locally with no kick and wake the owner's
        # claim loop. A session placed on ANOTHER runner still takes the LOCAL
        # enqueue path below (PR-B collapsed the legacy cloud-relay dispatch
        # unconditionally). Headless handlers never place remotely.
        placed_remote = False
        if self.websocket is not None:
            cloud_sess = await run_sync(database.get_session, session_id)
            if cloud_sess:
                from app.core import placement as placement_mod
                decision = None
                try:
                    decision = await run_sync(
                        placement_mod.resolve_placement, cloud_sess
                    )
                except Exception:  # noqa: BLE001 — resolver trouble ⇒ run local
                    logger.warning(
                        f"Placement resolution failed for {session_id} — "
                        f"treating as local", exc_info=True,
                    )
                if decision is not None and decision.kind == "none":
                    await self.send_json({
                        "type": "error",
                        "message": "No capable device online for this session "
                                   "— its runner is offline or lacks the "
                                   "required capabilities.",
                    })
                    return
                if (
                    decision is not None
                    and decision.kind == "runner"
                    and decision.runner_id != settings.effective_instance_id
                ):
                    placed_remote = True

        # v2 turns cutover: enqueue a durable ``source=queue`` turn instead of a
        # ``queued_messages`` row. ``submit`` kicks the scheduler, which starts it
        # immediately when the session is idle, or leaves it queued behind the
        # live turn (busy-write guard implicit — the unique live index prevents a
        # double-start). Called on the loop so the kick can schedule. The
        # queue_updated frame is re-sourced from the turns table (B3).
        # Remote-placed: no kick (the owner's claim loop starts it) and the
        # install-local 'claude-code' sentinel never rides a foreign turn row.
        queue_profile_id = profile_id
        if placed_remote and queue_profile_id == "claude-code":
            queue_profile_id = None
        # Transport context, at parity with handle_message: without it a drained
        # queue turn runs with user_id/api_user_id None, so session_runtime skips
        # the attention bump and the AskUserQuestion fanout.
        payload: Dict[str, Any] = {}
        if self.api_user_id:
            payload["api_user_id"] = self.api_user_id
        if getattr(self, "user_id", None):
            payload["user_id"] = self.user_id
        turn_manager.submit(
            session_id,
            "queue",
            prompt=content,
            profile_id=queue_profile_id,
            project_id=project_id,
            payload=payload or None,
            no_kick=placed_remote,
        )
        if placed_remote:
            from app.core import turn_claim
            await run_sync(turn_claim.notify_turns_changed, session_id)
        await self._v2_broadcast_queue(session_id)
        return

    # ------------------------------------------------------------------
    # handle_remove_queued_message
    # ------------------------------------------------------------------

    async def handle_remove_queued_message(self, data: dict) -> None:
        """Handle 'remove_queued_message' — remove a single message from the queue."""
        message_id = data.get("message_id")
        session_id = data.get("session_id") or self.current_session_id

        if not message_id:
            return

        # v2 turns cutover: ``message_id`` is a turn id (the queue_updated frame
        # emits turn ids as ``id``). Settle it to aborted (kept as history) via
        # the guarded queued->aborted transition; NO scheduler kick fires
        # (removing a chip never starts anything). Re-broadcast from turns.
        await run_sync(turn_manager.remove_queued, message_id)
        if session_id:
            await self._v2_broadcast_queue(session_id)
        return

    # ------------------------------------------------------------------
    # handle_clear_queued_messages
    # ------------------------------------------------------------------

    async def handle_clear_queued_messages(self, data: dict) -> None:
        """Handle 'clear_queued_messages' — remove all queued messages for a session."""
        session_id = data.get("session_id") or self.current_session_id

        if not session_id:
            return

        # v2 turns cutover: abort every user-visible queued turn (kept as
        # history); the queue view then re-sources to empty. Internal
        # failover/auto_continue/unpark turns are never user queue chips, so they
        # are left untouched.
        def _clear_v2_queue():
            for turn in turns_db.get_queued_turns(session_id):
                if turn.get("source") in _V2_QUEUE_SOURCES:
                    turn_manager.remove_queued(turn["id"])
        await run_sync(_clear_v2_queue)
        await self._v2_broadcast_queue(session_id)
        return
