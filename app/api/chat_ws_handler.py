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
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi.websockets import WebSocketState
from starlette.websockets import WebSocket, WebSocketDisconnect

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
from app.api.worktree_session_meta import enrich_session_worktree_meta
from app.api.ws_utils import safe_ws_send, WSPayloadEncodeError

logger = logging.getLogger(__name__)

# Gate for verbose unpark-decision tracing. Off unless either env var is truthy.
# Read once at import so the hot path only does a bool check. (Mirrors the
# STREAM_DEBUG/AIHUB_DEBUG_STREAM gate in app/core/query_engine.py.) When on,
# EVERY unpark decision (in-stream stash vs. drain pump) logs the task_id, status,
# signature, whether the task was started-this-turn, and the registry status, so a
# live incident can confirm the mechanism. Silent + cheap by default.
_STREAM_DEBUG = bool(
    os.environ.get("STREAM_DEBUG") or os.environ.get("AIHUB_DEBUG_STREAM")
)


def _log_unpark_decision(
    *,
    path: str,
    session_id: str,
    task_id: Any,
    status: Any,
    signature: Any,
    started_this_turn: Any,
    decision: str,
) -> None:
    """INFO-level trace of a single unpark decision.

    Raised from STREAM_DEBUG to plain INFO (34-min stale-UI incident): on a
    recurrence the plain logs must show whether the pump reached a terminal task
    and whether delivery fired ("deliver"/"deliver-killed") vs was suppressed/
    deduped. These fire once per terminal-task decision (NOT per message/delta), so
    they are low-frequency and safe to keep at INFO. ``path`` is "in-stream" or
    "pump"; ``decision`` is a short verb ("stash"/"suppress-orphan"/"deliver"/…).
    """
    try:
        from app.core.task_registry import task_registry

        rec = task_registry.get_task(session_id, task_id) if task_id else None
        reg_status = rec.get("status") if rec else None
    except Exception:
        reg_status = None
    logger.info(
        "unpark-decision path=%s session=%s task_id=%s status=%s "
        "signature=%s started_this_turn=%s registry_status=%s decision=%s",
        path,
        session_id,
        task_id,
        status,
        signature,
        started_this_turn,
        reg_status,
        decision,
    )


# Shared module-level sets tracking sessions with a pending two-phase
# ``interrupt_for_query`` or a user-initiated stop.  Checked by run_query's
# finally block to decide whether to auto-process the next queued message.
# Defined in ``ws_cleanup`` so both this module and ``app/api/websocket.py``
# see the same flag regardless of which endpoint set it.
from app.api.ws_cleanup import (  # noqa: E402  (after logger by design)
    _interrupt_pending_sessions,
    _stopped_sessions,
    superseded_by_successor,
)


# Terminal background-task statuses that should wake the parent (mirror the SDK's
# TaskNotificationStatus literal — every TaskNotificationMessage is terminal).
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped"})

# How long the two-phase ``interrupt_for_query`` waits for the interrupted
# streaming task to drain naturally before force-cancelling it. The frontend's
# interrupt-confirm window must exceed this plus the SDK interrupt timeout
# (see message-sender.ts).
_INTERRUPT_DRAIN_TIMEOUT_SECONDS = 8.0

# How long ``handle_stop`` waits after a soft interrupt() before force-cancelling
# the streaming task (the "SDK didn't stop" safety net). Module-level so the
# escalation-eviction tests can shrink it.
_STOP_SAFETY_TIMEOUT_SECONDS = 10.0


def _should_evict_after_escalation(session_id: str) -> bool:
    """Decide whether an interrupt-escalation force-cancel should ALSO evict the
    pooled client (``remove_broken`` → SIGTERM of the subprocess).

    Interrupt escalation (the stop-safety timer and the drain-timeout branch)
    force-cancels the wedged parent turn. The bare ``task.cancel()`` is harmless:
    the streaming generator swallows CancelledError and keeps a connected client
    pooled — it does NOT touch the subprocess. Only ``remove_broken`` reaches
    SDK close() → SIGTERM, which kills EVERY background sub-agent/shell parented
    to that one CLI subprocess (invariant documented at session_pool.py:965-985).

    So escalation must NEVER trade live background work for eviction of a healthy
    client. We evict only when it is actually safe/necessary:

    * If there is live background work AND the subprocess is still alive, KEEP the
      client. The turn was cancelled but the CLI is fine and its children are
      running; the send-now "Fatal error in message reader" crash it guards
      against is a *next-query* hazard — if the client is genuinely wedged, that
      next query's error path will evict it, at which point eviction is justified.
    * If the subprocess is DEAD, evict regardless — the background children are
      already gone, and a broken client must not stay pooled (preserves the
      send-now crash fix: a genuinely broken client is never reused).
    * With no live background work, keep the original behavior: evict.
    """
    from app.core.task_registry import task_registry
    from app.core.session_pool import get_session_pool, is_subprocess_alive

    if not task_registry.has_active_background_work(session_id):
        return True  # no children to protect — original eviction behavior

    state = get_session_pool().peek(session_id)
    if state is None:
        return True  # already gone from the pool — nothing to protect

    # Live background work: only evict (killing the children) if the subprocess
    # is already dead. A dead subprocess means the children are gone anyway and
    # the broken entry must not be reused.
    return not is_subprocess_alive(state)

# Statuses for which a TaskUpdatedMessage-ALONE should UNPARK the parent in the
# drain pump. Deliberately ONLY the notification-less terminals: a TaskStop emits
# "killed" (and "stopped" defensively) via a TaskUpdatedMessage with NO following
# TaskNotificationMessage, so the pump must unpark on those. A normally completed/
# failed task ALSO emits a TaskNotificationMessage (carrying the full result via
# output_file); we let THAT drive the rich unpark and do NOT double-fire here.
# (The registry indicator still clears for ALL terminal updates — that happens in
# query_engine._record_task_message, independent of this set.) Dedicated set kept
# distinct from ``_TERMINAL_TASK_STATUSES`` so the notification path is untouched.
_UNPARK_ON_TASK_UPDATE_STATUSES = frozenset({"killed", "stopped"})

# Consolidation window for the drain pump. After the first deliverable completion
# is detected, the pump keeps draining any FURTHER completions that are already
# buffered (or land within this window) and folds them into ONE follow-up turn,
# instead of firing a separate "unpark" turn per completion. Small by design: the
# dominant win is collapsing the backlog of completions that piled up on the pooled
# client WHILE the previous turn was streaming (the pump is stopped during a turn),
# all of which drain near-instantly because they are already queued. Tests patch
# this to a tiny value; read fresh at the call site so monkeypatching takes effect.
_UNPARK_BATCH_WINDOW_SECONDS = 1.5


# Guidance attached to every consolidated <task-notification> follow-up turn. The
# app now fires AT MOST ONE unpark per background task (see
# ``task_registry.mark_unparked``) and batches near-simultaneous completions into a
# single turn, so this is an informational, one-time consolidation — NOT a prompt
# for new work. The wording steers the model away from manufacturing a verbose
# "nothing to act on" rationalization (the symptom this whole change targets).
_TASK_NOTIFICATION_NOTE = (
    "Background work has come to rest. This is an automatic, one-time summary of "
    "its result(s) for your awareness — you are NOT being asked to do anything. If "
    "the outcome is already reflected in your earlier work, or needs no follow-up, "
    "stop after a brief acknowledgement; do not invent next steps or re-explain "
    "what already finished."
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


# ======================================================================
# Shared background-agent park→wake ("unpark") machinery
# ======================================================================
# These module-level functions hold the park/wake logic ONCE so BOTH chat-
# WebSocket handlers drive identical behaviour: the class-based
# ``ChatWebSocketHandler`` (used by the headless runner) AND the inline
# ``/ws/chat`` endpoint in ``app/api/websocket.py`` (the live browser path).
# Historically only the class had this logic, so the browser path had NO
# background-agent unpark at all — these shared helpers fix that.
#
# Each function takes the handler's per-connection collaborators as callables
# (``run_query`` / ``send_json`` / ``set_query_task`` / ``deliver_unpark`` /
# ``drain_coro_factory``) so it is independent of which handler invoked it. The
# class methods below are thin wrappers that pass ``self.*`` (late-bound, so unit
# tests that monkeypatch e.g. ``handler._deliver_unpark`` still intercept).


def _latch_terminal_notification(event: dict, session_id: str) -> None:
    """Latch an in-stream terminal ``task_notification`` — record, never deliver.

    A task that comes to rest BEFORE the parent's ``ResultMessage`` is the CLI's
    to report, not ours: the CLI enqueues the notification on its own message
    queue (visible as a ``queue-operation`` transcript entry) and injects it into
    the model's context before the turn ends — same steering semantics as a user
    message sent mid-turn. The model therefore already acknowledged the result
    in THIS turn. The app used to ALSO stash such notifications and fire a
    ``<task-notification>`` follow-up turn after the ResultMessage, which woke
    the session to re-tell the model about work it had already reported — the
    recurring "task messages surface after the session ends" bug (2026-07-02
    incident). Only a task that comes to rest AFTER the turn ends (session
    parked) needs an app-driven wake, and that is the between-turns drain pump's
    job (``_drain_notifications_impl``).

    What remains here is bookkeeping: latch ``task_registry.mark_unparked`` for
    a genuinely backgrounded task so the drain pump can never re-deliver a
    notification the CLI already surfaced (defense against the pooled-client
    buffer replaying it between turns), and keep the ``unpark-decision``
    diagnostic trail. No-op for non-terminal / non-notification events, so it is
    safe to call for every streamed event. Registry completion is recorded
    upstream in ``_record_task_message`` regardless, so indicators never strand.
    """
    from app.core.task_registry import task_registry

    if event.get("type") != "task_notification":
        return
    if event.get("status") not in _TERMINAL_TASK_STATUSES:
        return

    task_id = event.get("task_id")
    output_file = event.get("output_file")
    status = event.get("status")
    signature = event.get("uuid") or f"{output_file}:{status}"

    # Prior-turn orphan leaking from the pooled-client buffer: nothing to latch —
    # its unpark fate was decided in its own turn; just leave the audit line.
    started_this_turn = task_registry.was_started_this_turn(session_id, task_id)
    if not started_this_turn:
        _log_unpark_decision(
            path="in-stream",
            session_id=session_id,
            task_id=task_id,
            status=status,
            signature=signature,
            started_this_turn=False,
            decision="suppress-orphan",
        )
        return

    # Foreground work (a synchronous sub-agent, or a plain Bash/PowerShell call —
    # the CLI emits local_bash task messages for those too): its result already
    # returned INLINE as a tool_result this turn. No latch needed — the unpark
    # gates (was_background_launch / is_sdk_background_task) keep it out of the
    # drain pump as well.
    if not (
        task_registry.was_background_launch(session_id, event.get("tool_use_id"))
        or task_registry.is_sdk_background_task(session_id, task_id)
    ):
        _log_unpark_decision(
            path="in-stream",
            session_id=session_id,
            task_id=task_id,
            status=status,
            signature=signature,
            started_this_turn=True,
            decision="suppress-foreground",
        )
        return

    task_registry.mark_unparked(session_id, task_id)
    _log_unpark_decision(
        path="in-stream",
        session_id=session_id,
        task_id=task_id,
        status=status,
        signature=signature,
        started_this_turn=True,
        decision="cli-delivered-in-turn",
    )


async def _deliver_unpark_impl(
    *,
    pending: list,
    session_id: str,
    profile_id: str,
    project_id: Optional[str],
    worktree_id: Optional[str],
    ctx,
    final_metadata: Optional[Dict[str, Any]] = None,
    run_query: "Callable[..., Awaitable[Any]]",
    set_query_task: "Callable[[asyncio.Task], None]",
) -> bool:
    """Persist + broadcast a consolidated ``<task-notification>`` prompt and start
    ONE follow-up turn so the parent "unparks" and responds.

    Called by the between-turns drain-pump path (``_drain_notifications``) — the
    only path that delivers task notifications app-side; a task that comes to
    rest mid-turn is delivered to the model by the CLI itself (see
    ``_latch_terminal_notification``). Modeled EXACTLY on the queue-handoff
    pattern (persist user msg → broadcast → end_streaming →
    create_task(run_query)) so streaming tears down + re-inits without a race.

    The caller owns any stopped/interrupting guards and dedup via
    ``task_registry.mark_unparked`` BEFORE building ``pending`` — by the time we
    are called the completion is latched, so a failure here cannot re-trigger.
    Returns ``True`` if a follow-up turn was handed off (caller must return).
    """
    from app.api.websocket import _active_chat_sessions, _active_sessions_lock
    from app.core.task_registry import task_registry

    if not pending:
        return False

    # Busy-write guard: if a user query won the race and already owns a live
    # turn, the unpark must yield — overwriting _active_chat_sessions and
    # querying would write into the mid-turn CLI and kill it. The completions
    # are already latched (mark_unparked), so nothing re-fires; the parent
    # simply doesn't unpark this time. Checked again under the lock at task
    # registration below (this early check just avoids persisting a synthetic
    # prompt that would never be delivered).
    async with _active_sessions_lock:
        _existing = _active_chat_sessions.get(session_id)
    if _existing is not None and not _existing.done():
        ctx.info(logger, "Unpark: a live turn already owns the session — yielding (no clobber)")
        return False

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
    # (Between-turns pump path: streaming already ended for this turn, so this is
    # an idempotent re-end.)
    await live_session_store.end_streaming(
        session_id=session_id,
        metadata=final_metadata or {},
        interrupted=False,
    )

    # Latch "unpark pending" BEFORE handing off, so a polling client never sees a
    # transient (is_streaming=false, activeTasks=[]) and STOPS in the gap between
    # the background task completing (already dropped from ``get_active`` above via
    # ``complete_task``) and the follow-up turn flipping ``is_streaming`` true. The
    # follow-up's ``live_session_store.start_streaming`` clears it (it flips with
    # ``is_streaming``); ``end_streaming`` / ``clear_session`` clear it as
    # backstops; the ``except`` below clears it if the hand-off never happens. Set
    # AFTER the ``end_streaming`` above (which also clears the latch) so it isn't
    # immediately wiped. See ``/live`` (``unpark_pending``) and the session poller.
    task_registry.mark_unpark_pending(session_id)

    try:
        # ``deferred_delivery=True``: this follow-up turn is started server-side
        # by the drain pump (or in-stream stash), often AFTER the user navigated
        # away or reconnected — so this handler's origin socket may already be
        # dead. Marking it deferred makes ``run_query`` fall through a failed
        # ``start`` frame and stream via the live store + ``sync_engine``
        # broadcasts + polling instead of aborting. Without this, a parked
        # session's unpark turn silently tore itself down on the dead socket and
        # the parent's response never surfaced until a full reload (same
        # dead-socket failure mode as the "Send later" queue-drain bug).
        # Busy-write guard (late re-check, TOCTOU): the persist/broadcast awaits
        # above are exactly where a fresh user query can land. Re-check and
        # register under ONE lock acquisition so a live turn is never
        # overwritten — if a query won the race, yield instead of clobbering.
        async with _active_sessions_lock:
            existing = _active_chat_sessions.get(session_id)
            if existing is not None and not existing.done():
                task_registry.clear_unpark_pending(session_id)
                ctx.info(
                    logger,
                    "Unpark: a user query won the race at delivery — yielding (no clobber)",
                )
                return False
            task = asyncio.create_task(
                run_query(synth_prompt, session_id, profile_id, project_id, None, worktree_id, deferred_delivery=True)
            )
            _active_chat_sessions[session_id] = task
        set_query_task(task)
        ctx.debug(logger, "Unpark: follow-up query started (handed off)")
        return True
    except Exception as exc:
        # Best-effort: if the follow-up couldn't start, fall through to normal
        # completion (the notification was already deduped, so it won't
        # retrigger — the parent simply won't unpark this time). Drop the latch we
        # set above so a polling client doesn't park-poll forever waiting on a
        # follow-up that will never stream.
        task_registry.clear_unpark_pending(session_id)
        ctx.error(logger, f"Unpark: failed to start follow-up query: {exc}")
        return False


def _drop_drain_pump_self(session_id: str, drain_pumps: "Dict[str, asyncio.Task]") -> None:
    """Remove the CURRENT pump task from the registry iff the entry is still us.

    Guards the TOCTOU where ``stop_drain_pump`` already popped/replaced the entry.
    """
    current = asyncio.current_task()
    if drain_pumps.get(session_id) is current:
        drain_pumps.pop(session_id, None)


async def _drain_notifications_impl(
    session_id: str,
    profile_id: str,
    project_id: Optional[str],
    worktree_id: Optional[str],
    *,
    device_id: str,
    user_id: str,
    send_json: "Callable[[dict], Awaitable[bool]]",
    deliver_unpark: "Callable[..., Awaitable[bool]]",
) -> None:
    """Between-turns reader that catches a SLOW sub-agent's terminal
    ``TaskNotificationMessage`` and unparks the parent.

    The parent turn's ``receive_response()`` returns at the parent's
    ``ResultMessage`` and does NOT stay open while a background sub-agent runs, so
    a SLOW sub-agent's terminal notification arrives LATER on the pooled,
    still-alive SDK client with nothing reading it. This pump uses
    ``client.receive_messages()`` (which does NOT stop at a ``ResultMessage``) to
    read the pooled client BETWEEN turns and: feed EVERY message into
    ``task_registry`` (this alone unfreezes the indicator) and, on the FIRST
    terminal notification, dedupe + deliver ONE unpark follow-up turn.

    SINGLE-READER INVARIANT: this is reader R2; it runs only when no turn owns the
    client (``is_streaming`` False) and is cancelled by ``stop_drain_pump`` at the
    start of every turn, so it never reads the client concurrently with a turn's
    ``receive_response`` (R1). Fires per-completion so nothing is lost if a user
    message cancels the pump mid-wait.
    """
    from app.api.websocket import _drain_pumps
    from app.core.session_pool import get_session_pool
    from app.core.task_registry import task_registry
    from app.core.query_engine import (
        _record_task_message,
        _subagent_events_from_message,
        _task_notification_frame,
        _task_update_terminal_status,
        TaskNotificationMessage,
        TaskUpdatedMessage,
    )
    from app.api.ws_event_handlers import (
        dispatch_event,
        EventContext,
        MutableStreamState,
    )

    ctx = StreamingContext(
        session_id=session_id,
        device_id=device_id,
        message_id=str(uuid.uuid4()),
        user_id=user_id,
    )

    pending: List[Dict[str, Any]] = []
    try:
        state = get_session_pool().peek(session_id)  # peek = no LRU promote
        if state is None or not state.adapter.is_alive() or state.is_streaming:
            # A turn owns the client, or the client is gone — nothing to drain.
            logger.info(
                "Drain pump: no drainable client (state=%s, alive=%s, streaming=%s) "
                "— exiting without reading [session=%s]",
                state is not None,
                bool(state and state.adapter.is_alive()),
                bool(state and state.is_streaming),
                session_id,
            )
            return

        client = state.client
        if client is None:
            return

        logger.info(
            "Drain pump: started reading pooled client between turns [session=%s]",
            session_id,
        )

        # Re-dispatch context for SLOW background sub-agent inner activity. The
        # subagent node already lives in the live_session_store (created in-stream
        # at launch); these handlers locate it by agent_id/tool_use_id, so the
        # message_id here is immaterial (the FE renders agent_children via the
        # /live park-poll, not the WS broadcast).
        sub_ectx = EventContext(
            ctx=ctx,
            session_id=session_id,
            message_id=ctx.message_id,
            user_id=user_id,
            project_id=project_id,
            worktree_id=worktree_id,
        )
        sub_state = MutableStreamState()

        agen = client.receive_messages()
        batch_deadline: Optional[float] = None
        while True:
            if batch_deadline is None:
                # No deliverable yet — block for the next pooled-client message.
                try:
                    message = await agen.__anext__()
                except StopAsyncIteration:
                    break
            else:
                # At least one completion is already queued for delivery — keep
                # draining any FURTHER completions that are buffered (or land within
                # the consolidation window) so near-simultaneous completions fold
                # into ONE follow-up turn instead of one turn per completion
                # (Bug 1, policy #3). The backlog that piled up while the previous
                # turn was streaming is already queued, so it drains near-instantly.
                _wait = batch_deadline - time.monotonic()
                if _wait <= 0:
                    break
                try:
                    message = await asyncio.wait_for(agen.__anext__(), timeout=_wait)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    # Window elapsed (or stream ended) — deliver what we have. A
                    # message still buffered on the anyio receive stream is left
                    # intact (a cancelled receive does not consume an item), so the
                    # next pump run re-reads it and re-dedupes via mark_unparked.
                    break

            # Feed EVERY message into the registry — this is what unfreezes the
            # indicator (start/update/complete_task) for the terminal
            # notification even before we deliver the unpark.
            _record_task_message(session_id, message)

            # Re-dispatch a SLOW background sub-agent's inner activity (assistant
            # text + tool calls/results, identified by parent_tool_use_id) into the
            # SAME subagent inner group it would have filled in-stream. Without this
            # the node renders "completed / no activity recorded" because the parent
            # turn's receive_response() already returned before these arrived.
            try:
                for _sub_ev in _subagent_events_from_message(message):
                    await dispatch_event(_sub_ev, sub_ectx, sub_state)
            except Exception as _exc:
                ctx.debug(logger, f"Drain pump: subagent re-dispatch failed: {_exc}")

            # THE GAP: a killed/stopped (TaskStop) background task can terminate via
            # a TaskUpdatedMessage ALONE — no following TaskNotificationMessage. The
            # registry indicator already cleared above (_record_task_message handles
            # ALL terminal updates), but without this branch the parent would hang
            # silently. Deliver a minimal unpark so the parent acknowledges the stop.
            # We trigger ONLY on killed/stopped (the notification-less terminals): a
            # normally completed/failed task ALSO emits a TaskNotificationMessage
            # carrying the full result, which drives the rich unpark below — so we
            # must NOT also fire here for those, or the parent would unpark twice.
            # A killed task has NO output_file/result, so we build a result-free
            # pending payload; the unpark prompt builder tolerates an empty
            # output_file (reads "" off disk).
            if (
                not isinstance(message, TaskNotificationMessage)
                and isinstance(message, TaskUpdatedMessage)
            ):
                _u_status = _task_update_terminal_status(message)
                if _u_status not in _UNPARK_ON_TASK_UPDATE_STATUSES:
                    # Non-terminal, or a completed/failed update whose unpark is
                    # owned by the forthcoming notification — registry already
                    # cleared the indicator, nothing more to do here.
                    continue
                _u_task_id = getattr(message, "task_id", None)
                _u_uuid = getattr(message, "uuid", None)
                _u_signature = _u_uuid or f":{_u_status}"
                # SUPPRESS PHANTOM TOP-LEVEL REST (Bug 1, policy #1): the newer CLI
                # emits a killed/stopped TaskUpdatedMessage when the SESSION'S OWN
                # top-level agent comes to rest — a phantom that was never a tracked
                # background task. Unlike the notification path below, this branch has
                # no background-launch gate, so without this check that phantom would
                # storm the session with "nothing to act on" unparks. A genuine
                # user-killed background task WAS started via the registry, so it has
                # a live record (complete_task keeps it, only flipping status); a
                # phantom self-rest has none. Gate on a record so only real tracked
                # tasks unpark.
                if task_registry.get_task(session_id, _u_task_id) is None:
                    _log_unpark_decision(
                        path="pump",
                        session_id=session_id,
                        task_id=_u_task_id,
                        status=_u_status,
                        signature=_u_signature,
                        started_this_turn=task_registry.was_started_this_turn(session_id, _u_task_id),
                        decision="suppress-untracked",
                    )
                    continue
                # Dedup keyed on task_id ALONE (Bug 1, policy #2): one unpark per task
                # for its whole life, regardless of the per-rest uuid the newer CLI
                # re-stamps on each at-rest re-emission.
                if not task_registry.mark_unparked(session_id, _u_task_id):
                    # Already drove an unpark for this task — keep draining.
                    _log_unpark_decision(
                        path="pump",
                        session_id=session_id,
                        task_id=_u_task_id,
                        status=_u_status,
                        signature=_u_signature,
                        started_this_turn=task_registry.was_started_this_turn(session_id, _u_task_id),
                        decision="dedup",
                    )
                    continue
                _log_unpark_decision(
                    path="pump",
                    session_id=session_id,
                    task_id=_u_task_id,
                    status=_u_status,
                    signature=_u_signature,
                    started_this_turn=task_registry.was_started_this_turn(session_id, _u_task_id),
                    decision="deliver-killed",
                )
                pending.append({
                    "task_id": _u_task_id,
                    "status": _u_status,
                    "summary": None,
                    "output_file": None,
                    "uuid": _u_uuid,
                    "usage": None,
                    "tool_use_id": None,
                })
                # Latch unpark-pending the MOMENT the completion is consumed —
                # see the notification path below for why this cannot wait until
                # ``_deliver_unpark_impl``.
                task_registry.mark_unpark_pending(session_id)
                # Consolidate (Bug 1, policy #3): keep draining within the batch
                # window instead of breaking, so completions landing close together
                # fold into ONE follow-up turn.
                if batch_deadline is None:
                    batch_deadline = time.monotonic() + _UNPARK_BATCH_WINDOW_SECONDS
                continue

            if not isinstance(message, TaskNotificationMessage):
                continue

            frame = _task_notification_frame(message)
            status = frame.get("status")
            if status not in _TERMINAL_TASK_STATUSES:
                continue

            # Complete the matching subagent node (keyed by tool_use_id). The node
            # was kept RUNNING at launch (the launch-ACK no longer completes it), so
            # the terminal notification is what marks it done/errored — mirrors the
            # in-stream task_notification path for FAST sub-agents. Idempotent:
            # no-ops if the node was already completed in-stream or isn't a subagent.
            _notif_tool_use_id = frame.get("tool_use_id")
            if _notif_tool_use_id:
                try:
                    await dispatch_event(
                        {
                            "type": "task_notification",
                            "tool_use_id": _notif_tool_use_id,
                            "status": status,
                            "usage": frame.get("usage"),
                        },
                        sub_ectx,
                        sub_state,
                    )
                except Exception as _exc:
                    ctx.debug(logger, f"Drain pump: subagent completion dispatch failed: {_exc}")

            # Optional best-effort FE nudge; registry + poll already clear the
            # indicator, so failure here is harmless.
            try:
                await send_json(dict(frame, session_id=session_id))
            except Exception:
                pass

            # SPURIOUS-UNPARK GATE (synchronous sub-agent fix): the SDK emits an
            # at-rest TaskNotificationMessage for EVERY sub-agent when the parent
            # comes to rest — including SYNCHRONOUS ones whose full result was
            # already delivered INLINE as a tool_result during the turn, and
            # grandchild background tasks a SUB-AGENT launched (which the parent
            # never tracked). Only a notification whose tool_use_id the PARENT
            # recorded as a run_in_background=true launch represents genuinely-
            # backgrounded work the parent has NOT yet seen and therefore warrants
            # the wake-up follow-up turn. For anything else, suppress the unpark and
            # keep draining (do NOT break/deliver). The indicator already cleared
            # above via _record_task_message, and the subagent node was already
            # completed via the dispatch above, so suppressing here only drops the
            # spurious follow-up turn — nothing strands.
            #
            # Fallback for backgrounded local_bash shells (the actual slow-path bug):
            # the SDK does not reliably set tool_use_id on a shell's terminal
            # notification, so also accept a task recorded in _sdk_background_tasks
            # (keyed by the always-present task_id). This is the path that wakes a
            # session parked on a backgrounded Bash/PowerShell command.
            if not (
                task_registry.was_background_launch(session_id, frame.get("tool_use_id"))
                or task_registry.is_sdk_background_task(session_id, frame.get("task_id"))
            ):
                _log_unpark_decision(
                    path="pump",
                    session_id=session_id,
                    task_id=frame.get("task_id"),
                    status=status,
                    signature=frame.get("uuid") or f"{frame.get('output_file')}:{status}",
                    started_this_turn=task_registry.was_started_this_turn(session_id, frame.get("task_id")),
                    decision="suppress-foreground",
                )
                continue

            task_id = frame.get("task_id")
            output_file = frame.get("output_file")
            signature = frame.get("uuid") or f"{output_file}:{status}"
            # Dedup keyed on task_id ALONE (Bug 1, policy #2): one unpark per task
            # for its whole life. The newer CLI re-emits a terminal notification
            # with a FRESH uuid each time the agent comes to rest, which used to
            # re-arm the old (task_id, signature) latch and storm the session.
            if not task_registry.mark_unparked(session_id, task_id):
                # Already drove an unpark for this task — keep draining.
                _log_unpark_decision(
                    path="pump",
                    session_id=session_id,
                    task_id=task_id,
                    status=status,
                    signature=signature,
                    started_this_turn=task_registry.was_started_this_turn(session_id, task_id),
                    decision="dedup",
                )
                continue

            _log_unpark_decision(
                path="pump",
                session_id=session_id,
                task_id=task_id,
                status=status,
                signature=signature,
                started_this_turn=task_registry.was_started_this_turn(session_id, task_id),
                decision="deliver",
            )
            pending.append({
                "task_id": task_id,
                "status": status,
                "summary": frame.get("summary"),
                "output_file": output_file,
                "uuid": frame.get("uuid"),
                "usage": frame.get("usage"),
                "tool_use_id": frame.get("tool_use_id"),
            })
            # Latch unpark-pending the MOMENT the completion is consumed.
            # ``complete_task`` (via ``_record_task_message`` above) already
            # dropped the task from ``get_active``, so from here until the
            # follow-up's ``start_streaming`` a /live poll would read
            # (is_streaming=false, activeTasks=[]) — indistinguishable from
            # idle. Latching only inside ``_deliver_unpark_impl`` (after the
            # consolidation window + its DB writes) left that gap >= the whole
            # 1.5s window — LONGER than the client's 1.5s park-poll interval —
            # so every successful unpark landed a poll in the gap, STOPPED the
            # poller, and the follow-up streamed into a stranded UI (stuck
            # showing parked + a stale running task). Idempotent with the
            # deliver-time latch, which stays as the last-line re-assert after
            # its own ``end_streaming`` (that call clears the latch).
            task_registry.mark_unpark_pending(session_id)
            # Consolidate (Bug 1, policy #3): keep draining within the batch window
            # instead of breaking, so completions landing close together fold into
            # ONE follow-up turn.
            if batch_deadline is None:
                batch_deadline = time.monotonic() + _UNPARK_BATCH_WINDOW_SECONDS
            continue
    except asyncio.CancelledError:
        # Stopped by stop_drain_pump (a new turn is starting) — exit without
        # firing. Drop our early latch (if any): no follow-up will stream for the
        # pending we collected, and the new turn's own start_streaming would only
        # clear it if that turn actually starts. Re-raise so the awaiter sees the
        # cancellation.
        if pending:
            task_registry.clear_unpark_pending(session_id)
        _drop_drain_pump_self(session_id, _drain_pumps)
        raise
    except Exception as exc:
        ctx.debug(logger, f"Drain pump error (swallowed): {exc}")
        if pending:
            task_registry.clear_unpark_pending(session_id)
        _drop_drain_pump_self(session_id, _drain_pumps)
        return

    # Remove OURSELVES from the registry BEFORE spawning the follow-up turn so the
    # follow-up's start-of-turn stop_drain_pump() does not cancel/await the very
    # pump that is spawning it (self-deadlock).
    _drop_drain_pump_self(session_id, _drain_pumps)

    if pending:
        # Re-check guards before delivering — a stop/interrupt may have raced.
        if session_id in _stopped_sessions or session_id in _interrupt_pending_sessions:
            logger.info(
                "Drain pump: session stopped/interrupting — NOT delivering unpark "
                "(pending=%d dropped) [session=%s]",
                len(pending),
                session_id,
            )
            # Drop the early latch: no follow-up will stream, so a polling client
            # must not be held in park-poll waiting for one.
            task_registry.clear_unpark_pending(session_id)
            return
        logger.info(
            "Drain pump: delivering unpark follow-up [pending=%d, session=%s]",
            len(pending),
            session_id,
        )
        await deliver_unpark(
            pending=pending,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            worktree_id=worktree_id,
            ctx=ctx,
            final_metadata=None,
        )


def _start_drain_pump_impl(
    session_id: str,
    *,
    drain_coro_factory: "Callable[[], Awaitable[None]]",
) -> None:
    """Start a between-turns drain pump for a session (replacing any existing).

    Caller MUST ensure no turn owns the client (``is_streaming`` False) and that
    there is in-flight background work to wait for — a sub-agent (``kind=="agent"``)
    OR an SDK-backgrounded ``local_bash`` shell (see ``get_active_background_work``).
    ``drain_coro_factory`` returns a fresh ``_drain_notifications`` coroutine bound
    to the right handler.
    """
    from app.api.websocket import _drain_pumps

    existing = _drain_pumps.get(session_id)
    if existing is not None and not existing.done():
        # A pump is already running for this session — leave it.
        logger.info(
            "Drain pump: already running — not starting a second [session=%s]",
            session_id,
        )
        return
    task = asyncio.create_task(drain_coro_factory())
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
    _drain_pumps[session_id] = task
    logger.info("Drain pump: task created [session=%s]", session_id)


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
                        # ended sessions age out via pool eviction (LRU/idle)
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
        # Client-generated adoption token for a NEW-session query. Echoed back in
        # both the ``session_created`` and the first ``start`` frame so the
        # frontend can authenticate that the minted session is genuinely the one it
        # asked for — not a foreign session's server-initiated turn (unpark / queue
        # drain) unparking on the shared device socket during its awaiting window.
        # Only meaningful for a new session (session_id is None); ignored otherwise.
        request_id = data.get("request_id")

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

        # Cross-install (cluster) session: owned by another install on the
        # shared DB. Forward the follow-up prompt there; its /live stream is
        # mirrored back via the poller. Headless handlers never relay
        # (websocket is None) — a headless query always runs locally.
        if session_id and self.websocket is not None:
            cloud_sess = await run_sync(database.get_session, session_id)
            from app.core import cloud_relay
            if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                if session_id != self.current_session_id:
                    if self.current_session_id:
                        await sync_engine.unregister_device(self.device_id, self.current_session_id)
                    await sync_engine.register_device(self.device_id, session_id, self.websocket)
                    self.current_session_id = session_id
                try:
                    await cloud_relay.relay_message(
                        cloud_sess, prompt, profile_id, overrides
                    )
                    await self.send_json({"type": "cloud_forwarded", "session_id": session_id})
                except cloud_relay.InstallOffline as e:
                    await self.send_json({
                        "type": "error",
                        "message": f"Device offline — cannot send: {e}",
                    })
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Cluster relay_message failed for {session_id}: {e}")
                    await self.send_json({
                        "type": "error",
                        "message": f"Could not reach owning device: {e}",
                    })
                return

        # Busy-write guard: a query for a session that already has a live turn
        # must NEVER cancel-and-requery — the bare cancel skipped the SDK
        # interrupt, so the follow-up prompt was written into the mid-turn
        # CLI's stdin, killing it ("Fatal error in message reader") and losing
        # the whole in-flight turn. Enqueue the message instead: the existing
        # queue machinery drains it as a clean new query when the turn ends,
        # and the user sees it via the queue_updated ack frames. ("Send now"
        # goes through the two-phase interrupt_for_query, not this path.)
        if session_id:
            async with _active_sessions_lock:
                active_task = _active_chat_sessions.get(session_id)
            if active_task is not None and not active_task.done():
                logger.info(
                    f"[BusyGuard] query for session {session_id} arrived mid-turn — "
                    f"enqueueing instead of cancelling the live turn"
                )
                await self.handle_queue_message({
                    "session_id": session_id,
                    "content": prompt,
                    "profile": data.get("profile"),
                    "project": project_id,
                })
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

            # Immediately notify frontend of new session_id so it persists on refresh.
            # Echo the client's adoption token so the awaiting frontend accepts this
            # session_created ONLY if it matches — authenticating new-session adoption.
            session_created_frame = {
                "type": "session_created",
                "session_id": session_id,
                "title": title,
                "worktree_id": worktree_id  # Include worktree_id in response
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

        # Start new query task — busy-write guard (late re-check): the liveness
        # check and the registration happen under the SAME lock acquisition, so
        # a turn that started while we were persisting the user message above
        # (queue drain, unpark follow-up, another device) is never clobbered.
        # We never bare-cancel a live turn here: cancelling without an SDK
        # interrupt leaves the CLI mid-turn, and the next client.query() into
        # its stdin kills it and loses the whole in-flight turn.
        # Note: broadcast_stream_start is called inside run_query when actual
        # streaming begins. This prevents stuck streaming state if query fails early.
        # Pass the adoption token ONLY for a brand-new session so the first `start`
        # frame echoes it (authenticating adoption). An existing-session query needs
        # no token — the frontend already routes its start by the known session_id.
        preempted = False
        async with _active_sessions_lock:
            existing_task = _active_chat_sessions.get(session_id)
            if existing_task is not None and not existing_task.done():
                preempted = True
            else:
                self.query_task = asyncio.create_task(
                    self.run_query(
                        prompt, session_id, profile_id, project_id, overrides, worktree_id,
                        request_id=(request_id if is_new_session else None),
                    )
                )
                _active_chat_sessions[session_id] = self.query_task
        if preempted:
            # Rare double-race (a server-initiated turn won between the early
            # busy check and here). Enqueue so the message is never lost — the
            # live turn's post-turn queue drain picks it up. The user message
            # was already persisted above; the drain's re-persist is the same
            # rare duplicate the /ws/chat drain preempt path accepts.
            logger.info(
                f"[BusyGuard] query for session {session_id} preempted at "
                f"registration — enqueueing instead of cancelling the live turn"
            )
            await run_sync(
                database.add_queued_message,
                session_id=session_id,
                content=prompt,
                profile_id=data.get("profile"),
                project_id=project_id,
            )
            queue = await run_sync(database.get_queued_messages, session_id)
            await sync_engine.broadcast_queue_updated(session_id, queue)
            await self.send_json({
                "type": "sync",
                "event_type": "queue_updated",
                "session_id": session_id,
                "data": {"queued_messages": queue},
            })
            return

    # ------------------------------------------------------------------
    # handle_stop
    # ------------------------------------------------------------------

    async def handle_stop(self, data: dict) -> None:
        """Handle 'stop' message type."""
        from app.api.websocket import _active_chat_sessions, _active_sessions_lock

        session_id = data.get("session_id") or self.current_session_id
        if session_id:
            # Cross-install (cluster) session: relay the interrupt to the
            # owning install. A silent skip when offline is fine — there is
            # nothing running here to stop. PARITY: like the closure, this
            # returns BEFORE the _stopped_sessions latch add, so a remote stop
            # never latches locally. Headless handlers never relay
            # (websocket is None).
            if self.websocket is not None:
                cloud_sess = await run_sync(database.get_session, session_id)
                from app.core import cloud_relay
                if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                    try:
                        await cloud_relay.relay_interrupt(cloud_sess)
                    except cloud_relay.InstallOffline as e:
                        logger.debug(f"Cluster interrupt skipped (offline) for {session_id}: {e}")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"Cluster relay_interrupt failed for {session_id}: {e}")
                    return

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

            # Safety timeout: force-cancel if SDK doesn't stop within the budget.
            async with _active_sessions_lock:
                task = _active_chat_sessions.get(session_id)
            if task and not task.done():
                    async def _safety_cancel(t, sid, timeout=_STOP_SAFETY_TIMEOUT_SECONDS):
                        try:
                            await asyncio.sleep(timeout)
                            if not t.done():
                                logger.warning(f"SDK interrupt safety timeout for {sid}, force cancelling")
                                t.cancel()
                                try:
                                    await t
                                except (asyncio.CancelledError, Exception):
                                    pass
                                # Same broken-client hazard as interrupt_for_query:
                                # the CLI never honored the interrupt, so the next
                                # message must not reuse this pooled client — UNLESS
                                # live background work is parented to a still-alive
                                # subprocess. remove_broken → SIGTERM would kill
                                # every sub-agent/shell child; never trade live
                                # background work for eviction of a healthy client.
                                if _should_evict_after_escalation(sid):
                                    from app.core.session_pool import get_session_pool
                                    await get_session_pool().remove_broken(sid)
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

        # Cross-install (cluster) session: relay the interrupt to the owner,
        # then confirm so the frontend sends the replacement query (also
        # relayed). Headless handlers never relay (websocket is None).
        if self.websocket is not None:
            cloud_sess = await run_sync(database.get_session, session_id)
            from app.core import cloud_relay
            if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                try:
                    await cloud_relay.relay_interrupt(cloud_sess)
                    await self.send_json({"type": "interrupt_confirmed", "session_id": session_id})
                except cloud_relay.InstallOffline as e:
                    await self.send_json({
                        "type": "interrupt_failed",
                        "session_id": session_id,
                        "message": f"Device offline: {e}",
                    })
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Cluster interrupt_for_query failed for {session_id}: {e}")
                    await self.send_json({
                        "type": "interrupt_failed",
                        "session_id": session_id,
                        "message": f"Could not reach owning device: {e}",
                    })
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
                done, pending = await asyncio.wait({task}, timeout=_INTERRUPT_DRAIN_TIMEOUT_SECONDS)
                if pending:
                    logger.warning(f"interrupt_for_query: drain timed out for {session_id}, force cancelling")
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    # Drain timeout means the CLI never acknowledged the
                    # interrupt and is likely still mid-turn (e.g. extended
                    # thinking). The stream's finally keeps connected clients
                    # pooled, so without eviction the follow-up query would
                    # write into a busy subprocess and kill it ("Fatal error
                    # in message reader"), stopping the session dead. BUT if
                    # live background work is parented to a still-alive
                    # subprocess, remove_broken → SIGTERM would kill every
                    # sub-agent/shell child; never trade live background work
                    # for eviction of a healthy client. A truly wedged client
                    # is evicted by the next query's error path instead.
                    if _should_evict_after_escalation(session_id):
                        from app.core.session_pool import get_session_pool
                        await get_session_pool().remove_broken(session_id)

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

        # Recover any messages that were left queued while the session was
        # idle — e.g. a "Send later" message whose drain was dropped because
        # the originating socket went away (the user reloaded). Ported from
        # the /ws/chat closure's drain-on-load (drain_next_queued_if_idle,
        # log_prefix="load-session"). The closure's helper bails on the
        # stop/interrupt latches BEFORE popping (and never clears them — a
        # user Stop survives a reload), so mirror those checks here:
        # _process_next_queued leaves latch checks to its callers, exactly
        # like run_query's finally-block fallback. The busy guard lives
        # inside _process_next_queued itself (check-and-register under
        # _active_sessions_lock, re-queue at the front if preempted) — the
        # class's equivalent of the closure's _draining_sessions
        # reservation — so a load racing a turn start can never double-run
        # the message. Like the closure's drain, this is deliberately
        # socket-independent (the drained turn streams via the live store +
        # sync_engine + polling), so it needs no headless websocket-None
        # guard; HeadlessChatHandler never issues load_session anyway
        # (headless_query_runner / cluster relay only reach run_query and
        # handle_queue_message).
        if (
            not is_streaming
            and session_id not in _interrupt_pending_sessions
            and session_id not in _stopped_sessions
        ):
            try:
                await self._process_next_queued(session_id)
            except Exception as exc:
                logger.warning(f"[Queue] Failed to drain on load_session for {session_id}: {exc}")

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
                database.get_queued_messages, session_id
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

            # Cross-install session: the pending permission request and the
            # coroutine awaiting it live on the OWNING install, so responding
            # locally would just log "Request not found" and the owner's prompt
            # would sit until its 300s auto-deny. Relay the decision to the owner
            # to unblock its waiter. On failure the ack carries WHY, so the
            # frontend can tell a retryable delivery failure ('relay_offline' —
            # keep the modal so the decision isn't lost) from an authoritative
            # "no such pending request" ('not_pending' — dismiss the stale
            # modal). Headless handlers never relay (websocket is None).
            fail_reason = None
            cloud_sess = None
            if self.websocket is not None:
                cloud_sess = await run_sync(database.get_session, response_session_id)
            from app.core import cloud_relay
            if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                success = False
                try:
                    success = await cloud_relay.relay_permission_response(
                        cloud_sess,
                        request_id=request_id,
                        decision=decision,
                        remember=eff_remember,
                        pattern=eff_pattern,
                    )
                    if not success:
                        fail_reason = "not_pending"
                except cloud_relay.InstallOffline as e:
                    fail_reason = "relay_offline"
                    logger.debug(f"Cluster relay_permission_response offline for {response_session_id}: {e}")
                except Exception as e:  # noqa: BLE001
                    fail_reason = "relay_offline"
                    logger.warning(f"Cluster relay_permission_response failed for {response_session_id}: {e}")
                result = success
            else:
                result = await permission_handler.respond(
                    request_id=request_id,
                    session_id=response_session_id,
                    decision=decision,
                    remember=eff_remember,
                    pattern=eff_pattern,
                    broadcast_func=self.send_json
                )
            logger.debug(f"Permission response processed: {result}")

            # Send confirmation back. Mirrors user_question_response_ack: a
            # remote relay reports success + optional reason; a local respond
            # returns its richer result dict for backward compatibility.
            ack = {
                "type": "permission_response_ack",
                "session_id": response_session_id,
                "request_id": request_id,
            }
            if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                ack["success"] = bool(result)
                if fail_reason:
                    ack["reason"] = fail_reason
            else:
                ack["result"] = result
            await self.send_json(ack)
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
            # Cross-install session: the pending permission request lives in the
            # OWNING install's in-memory handler, so the local handler is always
            # empty here. Relay the read to the owner. A SUCCESSFUL relay (even
            # with zero requests) is authoritative and replaces the local set.
            # But a FAILED relay (owner offline / error) leaves us with the local
            # empty set, which is NOT authoritative — replying with bare empty
            # lists would let the frontend wipe a locally-open permission modal on
            # every reconnect/switch resync. So on relay failure we flag the reply
            # `stale` and keep what we have; a stale-aware frontend leaves its
            # modal untouched. Mirrors handle_get_pending_questions. Headless
            # handlers never relay (websocket is None).
            pending = permission_handler.get_pending_requests(self.current_session_id)
            deferred = permission_handler.get_deferred_requests(self.current_session_id)
            stale = False
            if self.websocket is not None:
                cloud_sess = await run_sync(database.get_session, self.current_session_id)
                from app.core import cloud_relay
                if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                    try:
                        payload = await cloud_relay.relay_get_pending_permissions(cloud_sess)
                        pending = payload.get("pending", [])
                        deferred = payload.get("deferred", [])
                    except cloud_relay.InstallOffline as e:
                        stale = True
                        logger.debug(f"Cluster relay_get_pending_permissions offline for {self.current_session_id}: {e}")
                    except Exception as e:  # noqa: BLE001
                        stale = True
                        logger.warning(f"Cluster relay_get_pending_permissions failed for {self.current_session_id}: {e}")
            reply = {
                "type": "pending_permissions",
                "session_id": self.current_session_id,
                "pending": pending,
                "deferred": deferred,
            }
            # Only mark a FAILED relay. A success (even empty) omits the key so
            # old clients keep clearing stale modals.
            if stale:
                reply["stale"] = True
            await self.send_json(reply)

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
            # Cross-install session: the pending question and its awaiting
            # coroutine live on the OWNING install, so responding locally would
            # just log "no pending question". Relay the answer to the owner to
            # unblock its waiter. On failure the ack carries WHY, so the
            # frontend can tell a retryable delivery failure ('relay_offline' —
            # keep the modal so the answer isn't lost) from an authoritative
            # "no such pending question" ('not_pending' — dismiss the stale
            # modal). Headless handlers never relay (websocket is None).
            fail_reason = None
            cloud_sess = None
            if self.websocket is not None:
                cloud_sess = await run_sync(database.get_session, response_session_id)
            from app.core import cloud_relay
            if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                result = False
                try:
                    result = await cloud_relay.relay_question_response(
                        cloud_sess, request_id, answers
                    )
                    if not result:
                        fail_reason = "not_pending"
                except cloud_relay.InstallOffline as e:
                    fail_reason = "relay_offline"
                    logger.debug(f"Cluster relay_question_response offline for {response_session_id}: {e}")
                except Exception as e:  # noqa: BLE001
                    fail_reason = "relay_offline"
                    logger.warning(f"Cluster relay_question_response failed for {response_session_id}: {e}")
            else:
                result = await user_question_handler.respond(
                    request_id=request_id,
                    session_id=response_session_id,
                    answers=answers
                )
                if not result:
                    fail_reason = "not_pending"
            logger.debug(f"User question response processed: {request_id} -> {result}")

            ack = {
                "type": "user_question_response_ack",
                "session_id": response_session_id,
                "request_id": request_id,
                "success": result
            }
            if fail_reason:
                ack["reason"] = fail_reason
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
            # Cross-install session: the pending question lives in the OWNING
            # install's in-memory handler, so the local handler is always
            # empty here. Relay the read to the owner. A SUCCESSFUL relay
            # (even with zero questions) is authoritative and must clear any
            # stale modal. But a FAILED relay (owner offline / error) leaves
            # us with the local empty set, which is NOT authoritative —
            # replying with a bare empty list would let the frontend wipe a
            # locally-open question modal (and its in-progress answer) on
            # every reconnect/switch resync. So on relay failure we flag the
            # reply `stale` and keep the questions we have; the frontend
            # leaves its modal untouched when it sees the marker. Headless
            # handlers never relay (websocket is None).
            pending = user_question_handler.get_pending_questions(self.current_session_id)
            stale = False
            if self.websocket is not None:
                cloud_sess = await run_sync(database.get_session, self.current_session_id)
                from app.core import cloud_relay
                if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                    try:
                        pending = await cloud_relay.relay_get_pending_questions(cloud_sess)
                    except cloud_relay.InstallOffline as e:
                        stale = True
                        logger.debug(f"Cluster relay_get_pending_questions offline for {self.current_session_id}: {e}")
                    except Exception as e:  # noqa: BLE001
                        stale = True
                        logger.warning(f"Cluster relay_get_pending_questions failed for {self.current_session_id}: {e}")
            reply = {
                "type": "pending_questions",
                "session_id": self.current_session_id,
                "questions": pending
            }
            # Only mark a FAILED relay. A success (even empty) omits the key
            # so old clients keep clearing stale modals.
            if stale:
                reply["stale"] = True
            await self.send_json(reply)

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

        # Cross-install (cloud) session: it runs on the owning install, where
        # the worktree/working dir live. Relay the queue op there — it stores
        # the message in the shared queue and drains it on the owner when
        # idle. Draining here would try to run the query against a worktree
        # this install does not have (the original "cloud mode queue" error).
        # The queue lives in the shared DB, so we read it back to update this
        # device's queue UI immediately (and to reconcile away the frontend's
        # optimistic entry on a relay failure). PARITY: returns BEFORE the
        # _stopped_sessions.discard below — a remote queue op never touches
        # the local latch. Headless handlers never relay (websocket is None).
        if self.websocket is not None:
            cloud_sess = await run_sync(database.get_session, session_id)
            from app.core import cloud_relay
            if cloud_sess and cloud_relay.is_remote_session(cloud_sess):
                relay_error: Optional[str] = None
                try:
                    await cloud_relay.relay_queue_message(
                        cloud_sess, content, profile_id, project_id
                    )
                except cloud_relay.InstallOffline as e:
                    relay_error = f"Device offline — cannot queue: {e}"
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Cluster relay_queue_message failed for {session_id}: {e}")
                    relay_error = f"Could not reach owning device: {e}"
                queue = await run_sync(database.get_queued_messages, session_id)
                await self.send_json({
                    "type": "sync",
                    "event_type": "queue_updated",
                    "session_id": session_id,
                    "data": {"queued_messages": queue},
                })
                if relay_error:
                    await self.send_json({"type": "error", "message": relay_error})
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

        # Busy-write guard: never cancel a live turn to make room for a queued
        # message (the bare cancel skipped the SDK interrupt, so the queued
        # prompt was written into the mid-turn CLI's stdin, killing it and
        # losing the in-flight turn). If a turn is running, put the message
        # back at the front — the post-turn queue drain picks it up.
        async with _active_sessions_lock:
            active_task = _active_chat_sessions.get(session_id)
        if active_task is not None and not active_task.done():
            logger.info(
                f"[Queue] Session {session_id} has a live turn — re-queuing "
                f"instead of cancelling it"
            )
            await run_sync(
                database.requeue_message_at_front,
                session_id=session_id,
                content=content,
                profile_id=profile_id if profile_id != "claude-code" else None,
                project_id=project_id,
            )
            queue = await run_sync(database.get_queued_messages, session_id)
            await sync_engine.broadcast_queue_updated(session_id, queue)
            return

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

            # Look up worktree_id from session
            session_data = await run_sync(database.get_session, session_id)
            worktree_id = session_data.get("worktree_id") if session_data else None

            # Start the query — busy-write guard (late re-check): check-and-
            # register under ONE lock acquisition so a turn that started during
            # the awaits above (an explicit query, an unpark follow-up) is never
            # clobbered. If preempted, re-queue at the front so the message is
            # not lost — mirrors the /ws/chat _drain_reserved preempt path.
            preempted = False
            async with _active_sessions_lock:
                existing_task = _active_chat_sessions.get(session_id)
                if existing_task is not None and not existing_task.done():
                    preempted = True
                else:
                    task = asyncio.create_task(
                        self.run_query(content, session_id, profile_id, project_id, None, worktree_id)
                    )
                    _active_chat_sessions[session_id] = task
            if preempted:
                logger.info(
                    f"[Queue] Drain preempted before launch for session "
                    f"{session_id} — re-queuing instead of cancelling the live turn"
                )
                await run_sync(
                    database.requeue_message_at_front,
                    session_id=session_id,
                    content=content,
                    profile_id=profile_id if profile_id != "claude-code" else None,
                    project_id=project_id,
                )
                queue = await run_sync(database.get_queued_messages, session_id)
                await sync_engine.broadcast_queue_updated(session_id, queue)
                return
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

    async def _deliver_unpark(
        self,
        *,
        pending: list,
        session_id: str,
        profile_id: str,
        project_id: Optional[str],
        worktree_id: Optional[str],
        ctx,
        final_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Thin wrapper → ``_deliver_unpark_impl`` (shared with /ws/chat).

        Binds ``self.run_query`` and ``self.query_task`` assignment so the
        follow-up turn runs on this handler. Returns ``True`` on handoff.
        """
        def _set_query_task(task: "asyncio.Task") -> None:
            self.query_task = task

        return await _deliver_unpark_impl(
            pending=pending,
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            worktree_id=worktree_id,
            ctx=ctx,
            final_metadata=final_metadata,
            run_query=self.run_query,
            set_query_task=_set_query_task,
        )

    # ------------------------------------------------------------------
    # _drain_notifications — between-turns drain pump
    # ------------------------------------------------------------------

    async def _drain_notifications(
        self,
        session_id: str,
        profile_id: str,
        project_id: Optional[str],
        worktree_id: Optional[str],
    ) -> None:
        """Thin wrapper → ``_drain_notifications_impl`` (shared with /ws/chat).

        Passes ``self.send_json`` and ``self._deliver_unpark`` late-bound so unit
        tests that monkeypatch ``_deliver_unpark`` still intercept.
        """
        await _drain_notifications_impl(
            session_id,
            profile_id,
            project_id,
            worktree_id,
            device_id=self.device_id,
            user_id=self.user_id,
            send_json=self.send_json,
            deliver_unpark=self._deliver_unpark,
        )

    @staticmethod
    def _drop_drain_pump_self(session_id: str, drain_pumps: Dict[str, asyncio.Task]) -> None:
        """Thin wrapper → module ``_drop_drain_pump_self`` (back-compat shim)."""
        _drop_drain_pump_self(session_id, drain_pumps)

    def _start_drain_pump(
        self,
        session_id: str,
        profile_id: str,
        project_id: Optional[str],
        worktree_id: Optional[str],
    ) -> None:
        """Thin wrapper → ``_start_drain_pump_impl`` (shared with /ws/chat).

        Binds a factory producing a fresh ``self._drain_notifications`` coroutine
        so the pump reads the pooled client via THIS handler.
        """
        _start_drain_pump_impl(
            session_id,
            drain_coro_factory=lambda: self._drain_notifications(
                session_id, profile_id, project_id, worktree_id
            ),
        )

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
        deferred_delivery: bool = False,
        request_id: Optional[str] = None,
    ) -> None:
        """Execute query and stream results directly to WebSocket.

        ``deferred_delivery`` marks a server-initiated turn (a queued "Send later"
        drain or a background sub-agent's unpark follow-up) whose delivery does not
        depend on this handler's own socket. When set, a failed ``start`` frame is
        non-fatal: the turn streams via the live store + ``sync_engine`` broadcasts
        + polling instead of tearing itself down (see the live ``/ws/chat`` route's
        ``run_query`` for the matching contract).

        ``request_id`` is the client's new-session adoption token (see
        ``handle_query``). It is echoed in the first ``start`` frame so the awaiting
        frontend adopts the minted session ONLY on a match. It is passed ONLY for
        the interactive new-session path — server-initiated turns (queue drain,
        unpark follow-up) leave it ``None``, so their ``start`` frame carries no
        token and can never be adopted as a foreign session's brand-new chat.
        """
        from app.api.websocket import (
            _active_chat_sessions, _active_sessions_lock, stop_drain_pump,
        )
        from app.api.ws_event_handlers import EventContext, MutableStreamState, dispatch_event
        from app.core.query_engine import stream_to_websocket
        from app.core.jsonl_parser import parse_session_history
        from app.core.task_registry import task_registry
        import time

        # SINGLE-READER INVARIANT: stop any between-turns drain pump BEFORE this
        # turn connects/queries the pooled SDK client. The pump reads the same
        # client via receive_messages(); this turn will read it via
        # receive_response() — at most one reader at a time.
        await stop_drain_pump(session_id)

        # Reset the per-session "tasks started this turn" set so the in-stream
        # unpark stash only fires for a background agent that BOTH started and
        # completed within THIS turn. Without this reset, a prior-turn orphan
        # task_notification leaking from the SDK's pooled-client buffer would be
        # treated as "started this turn" and fire a spurious unpark in this
        # later, unrelated turn (the reported bug). The registry's own
        # completion bookkeeping is unaffected.
        task_registry.reset_started_this_turn(session_id)

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
        # Offload the (potentially uncached) seed transcript parse to a thread:
        # resolve_for_streaming_init can do a full synchronous Postgres store read in
        # postgres_only mode, which would otherwise block the event loop on every turn.
        initial_messages = await run_sync(
            message_resolver.resolve_for_streaming_init, session_id, sdk_sid, w_dir
        )
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
            # Echo the client's adoption token (interactive new-session path only) so
            # the awaiting frontend accepts this start ONLY on a match. Server-
            # initiated turns leave request_id None → no token → non-adoptable.
            start_frame: Dict[str, Any] = {"type": "start", "session_id": session_id, "message_id": message_id}
            if request_id:
                start_frame["request_id"] = request_id
            if not await self.send_json(start_frame):
                ctx.warning(logger, "Connection lost before query could start")
                if deferred_delivery:
                    # Server-initiated turn (queue drain / unpark follow-up) on a
                    # dead origin socket: fall through and stream via the live
                    # store + sync_engine + polling rather than aborting (which
                    # stranded parked-session unpark turns until a full reload).
                    ctx.info(logger, "start frame undeliverable on origin socket — streaming via sync_engine + polling")
                else:
                    await live_session_store.cleanup_session(session_id)
                    return  # Exit early - connection is dead

            # NOTE: Don't call start_assistant_message here!
            # Creating an empty text message before content arrives causes the "Claude [time]"
            # header to appear even when the first content is a thinking block.
            # Let append_text/append_thinking create messages lazily when content arrives.

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(send_heartbeats())

            ctx.debug(logger, f"Calling stream_to_websocket, api_user_id={self.api_user_id}")
            # A Stop can arrive during session setup — BEFORE the SDK session is
            # registered in the pool. handle_stop records the id in
            # _stopped_sessions and calls interrupt_session(), but with nothing
            # pooled yet that interrupt no-ops and is never retried, so the model
            # would stream a full response and the turn would NOT look empty.
            # Re-issue the interrupt ONCE here: by the time events flow the SDK is
            # pooled, so the interrupt now lands and the SDK winds down naturally
            # (same proven path as a mid-stream Stop — keeps the pooled client
            # healthy). We only do this while nothing user-visible has streamed
            # (init does not flip stream_started), so a real partial turn is never
            # disturbed; the finally block then discards the empty orphan turn.
            stop_interrupt_reissued = False
            async for event in stream_to_websocket(
                prompt=prompt,
                session_id=session_id,
                profile_id=profile_id,
                project_id=project_id,
                overrides=overrides,
                broadcast_func=self.send_json,  # Pass send_json for permission requests
                api_user_id=self.api_user_id,  # Pass for user-scoped credential resolution
                user_id=self.user_id  # Pass for user-level broadcasts (awaiting-input indicator)
            ):
                if (
                    not stop_interrupt_reissued
                    and not stream_state.stream_started
                    and session_id in _stopped_sessions
                ):
                    stop_interrupt_reissued = True
                    from app.core.query_engine import interrupt_session
                    await interrupt_session(session_id)
                    ctx.info(logger, "Re-issued interrupt for a Stop that beat SDK pool registration")

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

                # --- Background-task terminal latch (in-stream completions) ---
                # If a background task finishes BEFORE the parent's ResultMessage,
                # its terminal task_notification arrives IN-STREAM and the CLI
                # itself delivers the completion to the model mid-turn (queued
                # message with steering semantics). We only LATCH it here
                # (task_registry.mark_unparked) so the between-turns drain pump
                # never re-delivers it as a spurious post-turn follow-up.
                #
                # A SLOWER background task's notification arrives AFTER this loop
                # has ended (stream_to_websocket's receive_response() returns at
                # the PARENT's ResultMessage); that case — the only one needing an
                # app-driven unpark — is handled by the between-turns drain pump
                # (_drain_notifications), started on normal completion below.
                _latch_terminal_notification(event, session_id)

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

            # --- Background "unpark" drain pump (SLOW path) ---
            # This turn is genuinely ending: no in-stream notification handed off
            # above and there was no queued successor. If there is still in-flight
            # background work — a sub-agent OR an SDK-backgrounded local_bash shell —
            # its terminal TaskNotificationMessage will arrive LATER on the pooled,
            # still-alive SDK client (after this turn's receive_response() already
            # returned at the parent ResultMessage). Start a between-turns drain pump
            # to catch it and unpark the parent. By here stream_to_websocket's finally
            # has set state.is_streaming False and handle_done has called
            # live_session_store.end_streaming, so the pump sees the client idle and
            # not owned by a turn.
            if (
                session_id not in _stopped_sessions
                and session_id not in _interrupt_pending_sessions
                and not handed_off_to_queued
            ):
                _active_bg_work = task_registry.get_active_background_work(session_id)
                _bg_launches = task_registry.get_background_launches(session_id)
                # Broadened gate (34-min stale-UI incident fix): start the pump when
                # there is STILL-RUNNING background work OR when this session recorded
                # ANY run_in_background launch. A background task whose terminal
                # TaskUpdatedMessage was consumed in-stream just before the parent
                # ResultMessage flips its registry entry to completed, so
                # get_active_background_work sees [] — but its terminal
                # TaskNotificationMessage may still be buffered on the pooled client
                # and arrive LATER with nothing reading it. Gating on the durable
                # launch set too means the pump is there to catch it. Starting a pump
                # that never receives a message is safe: it blocks on the pooled
                # client and is cancelled by stop_drain_pump at the next turn.
                if _active_bg_work or _bg_launches:
                    logger.info(
                        "Unpark: %d active background task(s), %d recorded launch(es) "
                        "— starting between-turns drain pump [session=%s]",
                        len(_active_bg_work),
                        len(_bg_launches),
                        session_id,
                    )
                    self._start_drain_pump(session_id, profile_id, project_id, worktree_id)

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

            # Cleanup of the in-memory buffer is now driven by pool eviction
            # (LRU/idle → SessionPool._do_evict → live-store/registry teardown),
            # not a standalone timer. The buffer stays warm for reconnecting
            # clients until the session ages out of the pool.

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
            # Carry session_id (the in-loop stamp above doesn't reach this except
            # block) so the frontend routes the error to the right session.
            await self.send_json({"type": "error", "message": f"Error: {error_detail}", "session_id": session_id})

            # End live session store on error
            # NOTE: end_streaming() now keeps messages in memory for reconnecting clients
            await live_session_store.end_streaming(
                session_id=session_id,
                metadata={"error": str(e)},
                interrupted=True
            )

            # Cleanup of the in-memory buffer is now driven by pool eviction
            # (LRU/idle → SessionPool._do_evict → live-store/registry teardown),
            # not a standalone timer.

            # Broadcast stream end on error
            if stream_state.stream_started:
                await sync_engine.broadcast_stream_end(
                    session_id=session_id,
                    message_id=message_id,
                    metadata={"error": error_detail},
                    interrupted=True,
                    source_device_id=None
                )

            # --- Background "unpark" drain pump (ERROR path; Bug C) ---
            # Mirror of the success-branch pump start above. This turn ERRORED rather
            # than completing cleanly, but background work it had already launched (a
            # sub-agent OR an SDK-backgrounded local_bash shell) may still be running
            # on the pooled, still-alive SDK client. Its terminal
            # TaskNotificationMessage will land LATER with nothing reading it — the
            # exact SLOW-path race the success branch starts a pump for. Without this
            # mirror, an error that did NOT kill the client (a transient handler/
            # processing error) permanently strands the parent: the registry task
            # stays "running" and the parent never unparks. end_streaming(interrupted)
            # above already cleared is_streaming, so the pump sees an idle client; its
            # own liveness guard (peek → not alive / is_streaming → return) makes this
            # a safe no-op when the error DID tear the client down (that case is reaped
            # by force-eviction cleanup instead — see live_session_store force path).
            # A user Stop takes the CancelledError branch above, which intentionally
            # does NOT pump (the user explicitly stopped — don't resurrect the turn).
            if (
                session_id not in _stopped_sessions
                and session_id not in _interrupt_pending_sessions
            ):
                _active_bg_work = task_registry.get_active_background_work(session_id)
                _bg_launches = task_registry.get_background_launches(session_id)
                # Broadened gate — mirror of the success path (see that comment).
                if _active_bg_work or _bg_launches:
                    logger.info(
                        "Unpark (error path): %d active background task(s), %d recorded "
                        "launch(es) — starting between-turns drain pump [session=%s]",
                        len(_active_bg_work),
                        len(_bg_launches),
                        session_id,
                    )
                    self._start_drain_pump(session_id, profile_id, project_id, worktree_id)

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
                # Ownership guard: a still-streaming store can also mean the drain
                # pump handed this session to an unpark follow-up turn while this
                # finally was unwinding — see ws_cleanup.superseded_by_successor.
                # Cleaning up then would wipe the live session + task registry out
                # from under the running follow-up.
                if live_session_store.has_session(session_id) and live_session_store.is_streaming(session_id):
                    if await superseded_by_successor(session_id):
                        ctx.debug(logger, "run_query finally: successor turn owns the live session — skipping cleanup")
                    else:
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
                        # Only force-cleanup if still marked as streaming (abnormal state).
                        # Ownership guard (same as the cleanup above): "no devices +
                        # still streaming" is exactly the signature of an unpark
                        # follow-up that started while this finally was unwinding —
                        # that state belongs to the successor, not us.
                        if await superseded_by_successor(session_id):
                            ctx.debug(logger, "run_query finally: successor turn owns the live session — skipping no-device cleanup")
                        else:
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

                    # Orphan-turn discard: the user pressed Stop before the model
                    # produced ANY user-visible output. stream_state.stream_started
                    # flips True on the first content event (text delta, chunk,
                    # tool_use, or a non-init system message); the SDK 'init' does
                    # NOT flip it. So `not stream_started` means nothing streamed —
                    # the turn is empty and the eagerly-persisted user prompt is
                    # misleading. A mid-stream Stop AFTER output has stream_started
                    # True and is a real, partial turn — keep it untouched.
                    #
                    # When empty: tell the frontend to pull the prompt back into the
                    # input box, and clean every store the chat reads from. The
                    # restore value is the `prompt` argument — exactly what the user
                    # typed this turn — so it is authoritative and never races the
                    # store teardown. The SESSION ROW IS KEPT: deleting it would
                    # orphan a worktree session's worktree link.
                    if not stream_state.stream_started:
                        # Best-effort live-store cleanup: a fast force-cancel can
                        # tear the live session down before end_streaming runs, so
                        # this may already be a no-op by the time we get here.
                        try:
                            await live_session_store.discard_empty_user_turn(session_id)
                        except Exception as discard_err:
                            ctx.warning(logger, f"discard_empty_user_turn (live) failed: {discard_err}")
                        # Durable cleanup. `sdk_sid` is the session's sdk_session_id
                        # captured at the START of this turn (before streaming),
                        # so it tells first turn from follow-up.
                        if sdk_sid is None:
                            # FIRST turn of a brand-new session. Under postgres_only
                            # the chat is read back from session_store_entries (the
                            # SDK transcript), so the SDK may already have mirrored
                            # the user line there; deleting only the session_messages
                            # row would NOT stop the next poll/reload from
                            # re-surfacing it. Wipe the whole empty transcript and
                            # unlink sdk_session_id so the session reverts cleanly
                            # (worktree link preserved). Only here is it safe to
                            # emit user_message_discarded: the follow-up poll reads
                            # an empty store, so the message stays gone — and the
                            # frontend pulls the prompt back into the input box.
                            try:
                                await run_sync(database.discard_orphan_first_turn, session_id)
                            except Exception as durable_err:
                                ctx.warning(logger, f"durable first-turn discard failed: {durable_err}")
                            await self.send_json({
                                "type": "user_message_discarded",
                                "session_id": session_id,
                                "prompt": prompt,
                            })
                            ctx.info(
                                logger,
                                "Discarded empty interrupted first turn "
                                "(stopped before any output); restoring prompt to input",
                            )
                        else:
                            # FOLLOW-UP turn that shares its transcript with the
                            # prior conversation. We must NOT edit that transcript
                            # (it holds real history), and the store still carries
                            # this user line — so emitting user_message_discarded
                            # would only flash the message out and let the next poll
                            # re-add it. Leave the chat untouched; just drop the
                            # eager session_messages row as legacy-table hygiene.
                            # (Cleanly reverting a single follow-up turn would need
                            # per-turn store surgery — a known limitation, not done
                            # here; the reported bug is the first-turn case above.)
                            try:
                                await run_sync(database.delete_orphan_user_turn, session_id)
                            except Exception as durable_err:
                                ctx.warning(logger, f"orphan follow-up user-row cleanup failed: {durable_err}")
                            ctx.info(
                                logger,
                                "Stopped before output on a follow-up turn — kept in place "
                                "(postgres_only reads a shared transcript; no clean per-turn revert)",
                            )
                    else:
                        ctx.debug(
                            logger,
                            "Stopped after output began — keeping the partial turn "
                            "(not an orphan)",
                        )
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
