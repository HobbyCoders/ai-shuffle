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

from app.core import account_failover
from app.core import compact_continue
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
    "pump"; ``decision`` is a short verb ("cli-delivered-in-turn"/
    "cli-delivered-cross-turn"/"suppress-foreground"/"deliver"/
    "deliver-subagent" (the deliver path, admitted via the subagent-lane
    background marker)/"dedup"/…).
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


def _normalize_relay_remember(remember: Any) -> Optional[str]:
    """Coerce a `remember` value to the strict cluster enum before relaying.

    The local ``permission_handler.respond`` accepts a loose ``remember`` (a
    boolean from legacy callers), but the owning install's cluster endpoint
    validates it against Literal 'none'|'session'|'profile' and 422s otherwise.
    Match local respond() semantics EXACTLY so a relayed decision behaves the
    same as a local one:

    * ``None`` / ``'none'`` (or any falsy) → ``'none'`` (no rule created).
      respond() short-circuits on ``if remember and remember != "none"``.
    * ``'session'`` / ``'profile'`` → passed through (valid enum strings).
    * boolean ``True`` (or any truthy non-enum value) → ``'profile'``.
      In respond(), a truthy non-"session" value enters the rule branch and,
      not matching ``remember == "session"``, falls to the profile-persist
      else — i.e. legacy ``remember=True`` means "remember for this profile".
    """
    if remember is None:
        return None
    if remember in ("none", "session", "profile"):
        return remember  # already a valid enum string
    if not remember:
        # falsy (False, "", 0) → local respond() creates no rule
        return "none"
    # truthy but not an enum string (legacy remember=True) → profile scope
    return "profile"


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

# Wedge-poll interval for the drain pump while it is winding down a force-cancelled
# turn (stale_turn_undrained). The killed turn's terminal ResultMessage usually
# arrives promptly, but if the CLI itself is wedged it may never come; on each
# expiry the pump re-checks whether the background work that made us keep the
# client has resolved, and evicts the now-idle/broken client if so. Read fresh at
# the call site so tests can shrink it.
_STALE_DRAIN_WEDGE_POLL_SECONDS = 30.0

# One startup retry for a pump started by interrupt escalation: the force-cancelled
# turn's finally (which flips is_streaming back to False) runs on deferred generator
# finalization and may not have landed yet when the escalation handler starts the
# pump. Exiting immediately would strand stale_turn_undrained with nothing left to
# clear it. Read fresh at the call site so tests can shrink it.
_STALE_STARTUP_RETRY_SECONDS = 0.25


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


_MAX_TASK_RESULT_CHARS = 60_000


def _extract_final_assistant_text(output_file: str) -> Optional[str]:
    """Extract the sub-agent's FINAL assistant message from its JSONL transcript.

    A terminal ``TaskNotificationMessage``'s ``output_file`` points at the
    sub-agent's raw JSONL transcript (agent-*.jsonl shape). Dumping that whole
    file verbatim as ``<result>`` (the old ``_read_task_output_file`` behaviour)
    floods the parent's context with the entire transcript; the model only needs
    the agent's final answer. Reuse the canonical agent-transcript parser
    (``parse_agent_history``) and return the LAST assistant entry's text — JOINED
    across all of that entry's text blocks, not just the trailing block. Returns
    ``None`` on any failure / empty or unreadable transcript so the caller can
    fall back to the raw output file / short summary.

    ``transform_agent_entries`` emits ONE ``{"type": "text", ...}`` child per text
    block, each id'd ``agent-text-<entry-uuid>`` — so every text block from the
    same assistant entry shares one ``id``. Grouping the trailing text children by
    that shared id reconstructs the last assistant message's full text.
    """
    if not output_file:
        return None
    from pathlib import Path
    from app.core.jsonl_parser import parse_agent_history

    try:
        children = parse_agent_history(Path(output_file))
    except Exception:
        logger.debug(
            "failed to parse task output_file %r for final assistant text",
            output_file,
            exc_info=True,
        )
        return None

    text_children = [
        c for c in children
        if c.get("type") == "text" and (c.get("content") or "").strip()
    ]
    if not text_children:
        return None
    # The last assistant entry = the source entry of the final text child. All its
    # text blocks share the same ``id`` (embeds the entry uuid); join them in order.
    last_id = text_children[-1].get("id")
    parts = [
        c.get("content") or "" for c in text_children if c.get("id") == last_id
    ]
    joined = "\n".join(p for p in parts if p.strip())
    return joined or None


def _build_task_notification_prompt(pending: list) -> str:
    """Build ONE synthesized user prompt consolidating all pending notifications.

    Mirrors Claude Code's reference ``<task-notification>`` contract so the parent
    wakes with each sub-agent's result. ``<result>`` carries the sub-agent's FINAL
    assistant message, extracted from its transcript via
    ``_extract_final_assistant_text`` (the raw transcript file is huge — the model
    only needs the final answer). When the transcript yields no assistant text it
    falls back to the RAW output-file read (``query_engine._read_task_output_file``
    — this restores backgrounded SHELL stdout, whose plain-text output_file parses
    to no assistant entries), then the short ``summary``. The returned string
    always contains the literal ``<task-notification>`` substring, so the existing
    user-message filters keep it from rendering as a chat bubble.
    """
    from app.core.query_engine import _read_task_output_file
    from app.core.query_streaming import truncate_large_payload

    def _esc(value: Any) -> str:
        return "" if value is None else str(value)

    blocks: List[str] = []
    for note in pending:
        output_file = note.get("output_file") or ""
        # Fallback chain: parsed final assistant text → raw output-file read
        # (backgrounded-shell stdout) → short summary → "".
        result_text = _extract_final_assistant_text(output_file)
        if not result_text:
            result_text = _read_task_output_file(output_file) or None
        if not result_text:
            result_text = note.get("summary") or ""
        if result_text and len(result_text) > _MAX_TASK_RESULT_CHARS:
            # truncate via the shared helper (matches query_engine's own use)
            # instead of a naive head-slice.
            result_text = truncate_large_payload(result_text, _MAX_TASK_RESULT_CHARS)
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


def _kill_summary_from_record(
    record: Optional[Dict[str, Any]], status: str
) -> Optional[str]:
    """Human-readable fallback summary for a killed/errored background task.

    A killed task's synthesized notification often carries no summary of its own,
    but the registry wire record still knows the ``kind`` and ``title`` (for
    Monitor shells the ``title`` is the command). Derive a
    ``"<kind> <status>: <title>"`` line so the parent's ``<task-notification>``
    says WHAT was stopped instead of arriving contentless. Returns ``None`` when
    the record has nothing to describe.
    """
    if not record:
        return None
    title = record.get("subject")
    if not title:
        return None
    kind = record.get("kind") or "task"
    return f"{kind} {status}: {title}"


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

    Admission mirrors the drain pump's background gate
    (``was_background_launch`` / ``is_sdk_background_task``), so the latch arms
    for every genuinely-backgrounded task regardless of whether it started this
    turn — a background sub-agent launched in an EARLIER turn that comes to rest
    IN-STREAM during a LATER turn is a genuine first delivery by the CLI.
    Round 4 note: the pump now APP-DELIVERS only the classes the CLI does not
    reliably self-wake (backgrounded shells; killed/stopped TaskUpdated-alone
    terminals) — a top-level backgrounded Task/Agent completion is CLI-owned
    (``decision=cli-owned-wake``) and never delivered by the app at all. For
    that class this latch is belt-and-suspenders bookkeeping; for shells it is
    still the live dedup that stops a mid-turn CLI delivery from re-firing via
    the pump. ``started_this_turn`` affects only the diagnostic label, never
    whether the latch is armed. Arming is safe for a truly replayed orphan too:
    ``mark_unparked`` is idempotent (already-latched → no-op). A FOREGROUND task
    (no recorded background launch) fails the gate and needs no latch — the pump
    rejects it as well.
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
    started_this_turn = task_registry.was_started_this_turn(session_id, task_id)

    # Foreground work (a synchronous sub-agent, or a plain Bash/PowerShell call —
    # the CLI emits local_bash task messages for those too): its result already
    # returned INLINE as a tool_result. No latch needed — the same gate keeps it
    # out of the drain pump, so it can never be re-delivered.
    if not (
        task_registry.was_background_launch(session_id, event.get("tool_use_id"))
        or task_registry.is_sdk_background_task(session_id, task_id)
        # Subagent-lane background task (grandchild): same admission as the
        # pump's gate, so an in-stream terminal frame the CLI already delivered
        # spends the one-unpark token and the pump can't re-deliver it. Armed
        # ONLY for run_in_background subagent-lane launches — foreground
        # synchronous work never enters this set.
        or task_registry.is_subagent_background_task(session_id, task_id)
        # ...and its launch-tool_use_id twin (mirror of the parent-lane pair
        # above): a flag-less builtin's (Monitor's) TaskStarted may not map the
        # task-id marker, but the terminal frame carries the launch tool_use_id.
        or task_registry.was_subagent_background_launch(
            session_id, event.get("tool_use_id")
        )
    ):
        _log_unpark_decision(
            path="in-stream",
            session_id=session_id,
            task_id=task_id,
            status=status,
            signature=signature,
            started_this_turn=started_this_turn,
            decision="suppress-foreground",
        )
        return

    # Genuine background task the CLI delivered in-stream: ALWAYS latch so the
    # drain pump finds the one-unpark-per-task token spent and never re-delivers.
    # started_this_turn only distinguishes the diagnostic label (in-turn launch vs
    # a prior-turn launch completing during a later turn).
    task_registry.mark_unparked(session_id, task_id)
    _log_unpark_decision(
        path="in-stream",
        session_id=session_id,
        task_id=task_id,
        status=status,
        signature=signature,
        started_this_turn=started_this_turn,
        decision="cli-delivered-in-turn" if started_this_turn else "cli-delivered-cross-turn",
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

    # SHUTDOWN GUARD (lifecycle batch, item 1): while the server is tearing
    # down, no NEW follow-up turn may start — the pooled SDK clients are being
    # disconnected underneath it, so the run_query would die mid-spawn (or pin
    # a subprocess past the pool shutdown). The completions are already latched
    # (mark_unparked), so nothing re-fires on the next boot; the durable
    # terminal chip / reap attribution is the surviving surface. Drop the
    # unpark_pending latch so a still-connected poller isn't held park-polling
    # for a follow-up that will never stream.
    from app.core import startup as _startup
    if getattr(_startup, "is_shutting_down", False):
        task_registry.clear_unpark_pending(session_id)
        ctx.info(
            logger,
            "Unpark: server shutting down — not starting a follow-up turn "
            f"({len(pending)} pending dropped)",
        )
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

    # Runs the transcript parse + file reads off the event loop (matches the
    # run_sync pattern used for the DB write just below).
    synth_prompt = await run_sync(_build_task_notification_prompt, pending)

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
    3. UNPARK: hand off ONE consolidated unpark via ``_deliver_unpark_impl``
       ONLY when a live pooled client exists and the server is NOT shutting
       down (DECISION: durable-chip-only during shutdown). ``mark_unparked``
       dedups against the drain pump's own delivery for the same task.
    """
    from app.core import startup as _startup
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

    # 1) LIVE chip (once per task lifetime — shared latch with pump chips).
    if task_registry.mark_chipped(session_id, task_id, "terminal"):
        task_registry.mark_chipped(session_id, task_id, "start")
        try:
            await live_session_store.add_system_message(
                session_id,
                content=summary or "",
                subtype="task_updated",
                data=data,
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

    # 3) Consolidated unpark — only with a live client, never during shutdown.
    if getattr(_startup, "is_shutting_down", False):
        return
    try:
        from app.core.session_pool import get_session_pool

        state = get_session_pool().peek(session_id)
        if state is None or not state.adapter.is_alive():
            return  # no live client — the durable chip is the surface
    except Exception:
        return
    if not task_registry.mark_unparked(session_id, task_id):
        return  # this task's one-unpark token is already spent (pump/CLI path)

    try:
        session_row = await run_sync(database.get_session, session_id)
    except Exception:
        session_row = None
    if not session_row:
        return

    from app.core.headless_query_runner import HeadlessChatHandler

    handler = HeadlessChatHandler(user_id="admin", api_user_id=None)
    handler.current_session_id = session_id
    ctx = StreamingContext(
        session_id=session_id,
        device_id=handler.device_id,
        message_id=str(uuid.uuid4()),
        user_id=handler.user_id,
    )
    pending = [{
        "task_id": task_id,
        "status": status,
        # ENRICH (kill-notification husk): fall back to a derived
        # "<kind> <status>: <title>" line so a summary-less killed task is not
        # a contentless husk the parent cannot act on. output_file/usage stay
        # None — a killed shell genuinely has no such data.
        "summary": summary or _kill_summary_from_record(task, status),
        "output_file": None,
        "uuid": None,
        "usage": None,
        "tool_use_id": task.get("toolUseId"),
    }]

    def _set_query_task(t: "asyncio.Task") -> None:
        handler.query_task = t

    try:
        await _deliver_unpark_impl(
            pending=pending,
            session_id=session_id,
            profile_id=session_row.get("profile_id"),
            project_id=session_row.get("project_id"),
            worktree_id=session_row.get("worktree_id"),
            ctx=ctx,
            final_metadata=None,
            run_query=handler.run_query,
            set_query_task=_set_query_task,
        )
    except Exception:
        logger.debug(
            "terminal-surface unpark handoff failed for %s/%s",
            session_id, task_id, exc_info=True,
        )


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
    process_next_queued: "Optional[Callable[[str], Awaitable[None]]]" = None,
    suppress_queue_kick: bool = False,
) -> None:
    """Between-turns reader that catches a SLOW sub-agent's terminal
    ``TaskNotificationMessage`` and unparks the parent.

    The parent turn's ``receive_response()`` returns at the parent's
    ``ResultMessage`` and does NOT stay open while a background sub-agent runs, so
    a SLOW sub-agent's terminal notification arrives LATER on the pooled,
    still-alive SDK client with nothing reading it. This pump uses
    ``client.receive_messages()`` (which does NOT stop at a ``ResultMessage``) to
    read the pooled client BETWEEN turns and: feed EVERY message into
    ``task_registry`` (this alone unfreezes the indicator) and route the wake by
    LAUNCH CLASS (round 4 of the duplicate-notification fix):

    - Top-level backgrounded Task/Agent (``was_background_launch`` on the
      notification's tool_use_id, registry kind != "shell"): CLI-OWNED — the CLI
      autonomously injects its own ``<task-notification>`` user-lane turn that
      the model answers, so the app delivers NOTHING (``decision=cli-owned-wake``;
      the app's synthesized unpark here was the duplicate). The pump instead
      attaches-and-streams: it forwards that CLI-driven turn's top-level
      assistant/tool events through ``dispatch_event`` into the live store
      (``_cli_turn_events_from_message``) and finalizes at its ResultMessage, so
      park-pollers render the CLI's wake live.
    - Backgrounded SHELLS (``is_sdk_background_task`` / local_bash) and
      killed/stopped TaskUpdatedMessage-alone terminals: classes the CLI does
      NOT reliably self-wake — dedupe + deliver ONE app unpark follow-up turn,
      exactly as before.

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
        _cli_turn_events_from_message,
        _record_subagent_lane_launches,
        _record_task_message,
        _subagent_events_from_message,
        _task_status_events_from_message,
        _task_status_event_from_notification,
        _task_notification_frame,
        _task_update_terminal_status,
        ResultMessage,
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
    # CLI-OWNED WAKE (round 4): tool_id→name map threaded across a CLI-driven
    # turn's messages (resolves tool_result names), whether any of that turn's
    # content was forwarded to the live pipeline since the last ResultMessage,
    # and whether this pump armed unpark_pending for an in-flight CLI wake (so
    # the exit paths can drop the latch if the wake turn can no longer arrive).
    _cli_tool_names: Dict[str, str] = {}
    _cli_turn_forwarded = False
    _cli_wake_latched = False
    try:
        state = get_session_pool().peek(session_id)  # peek = no LRU promote
        if (
            state is not None
            and state.is_streaming
            and getattr(state, "stale_turn_undrained", False)
        ):
            # Escalation-started pump raced the killed turn's finally (which
            # flips is_streaming False on deferred generator finalization).
            # Retry once before giving up — exiting now would strand the stale
            # flag with nothing left to clear it.
            await asyncio.sleep(_STALE_STARTUP_RETRY_SECONDS)
            state = get_session_pool().peek(session_id)
        if state is None or not state.adapter.is_alive() or state.is_streaming:
            # A turn owns the client, or the client is gone — nothing to drain.
            if state is not None and getattr(state, "stale_turn_undrained", False):
                logger.warning(
                    "Drain pump: exiting at startup with stale_turn_undrained still "
                    "set (alive=%s, streaming=%s) — the flag may strand until the "
                    "next query's error path evicts the client [session=%s]",
                    bool(state.adapter.is_alive()),
                    bool(state.is_streaming),
                    session_id,
                )
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
        # RC2 (parked "Task Updated" chip): task_ids for which a task_updated chip
        # was ALREADY dispatched this pump run — either by the generic converter
        # below (a real TaskUpdatedMessage) or by the notification synth. A normal
        # completion can emit BOTH a TaskUpdatedMessage(completed) AND a terminal
        # TaskNotificationMessage; deduping here keeps the card single.
        _chipped_task_ids: set = set()
        # PERSISTENT READ FUTURE (F1): every timed read below uses ONE future per
        # __anext__ call, kept alive across timeouts via ``asyncio.wait`` (which,
        # unlike ``wait_for``, never cancels it). Cancelling a pending __anext__
        # throws CancelledError INTO the generator frame, closing it permanently —
        # the next read then raises StopAsyncIteration and the pump exits with the
        # stale flag still set (bricked session); on anyio a delivery-then-cancel
        # window can also silently DROP a just-delivered message. Two concurrent
        # __anext__ calls on one generator raise RuntimeError, so the wedge-poll
        # read and the batch-window read MUST share this single pending future.
        next_fut: "Optional[asyncio.Future]" = None

        def _stale_flag_set() -> bool:
            _s = get_session_pool().peek(session_id)
            return bool(_s is not None and getattr(_s, "stale_turn_undrained", False))

        try:
            while True:
                if next_fut is None:
                    next_fut = asyncio.ensure_future(agen.__anext__())
                if batch_deadline is None:
                    # No deliverable yet — block for the next pooled-client message.
                    # WEDGE POLICY: while this pump is winding down a force-cancelled
                    # turn (stale_turn_undrained), do NOT block indefinitely — the
                    # killed turn's terminal frame may never arrive if the CLI itself
                    # wedged. Wait with a periodic timeout and, on each expiry,
                    # re-check whether the background work that made us KEEP the
                    # client has finished (or the subprocess died); once so, evict
                    # the now-idle/broken client and exit. The pending read future is
                    # left UNTOUCHED across expiries (see F1 note above).
                    if _stale_flag_set():
                        done, _ = await asyncio.wait(
                            {next_fut}, timeout=_STALE_DRAIN_WEDGE_POLL_SECONDS
                        )
                        if not done:
                            from app.core.session_pool import (
                                get_session_pool as _gsp,
                                is_subprocess_alive as _alive,
                            )
                            _st = _gsp().peek(session_id)
                            if _st is None:
                                break  # already gone from the pool — nothing to drain
                            if not _should_evict_after_escalation(session_id):
                                # Background work still running on a live subprocess —
                                # keep waiting for the killed turn's terminal frame.
                                continue
                            # Background work finished or subprocess died → the kept
                            # client is now safe/necessary to evict.
                            logger.info(
                                "Drain pump: stale-turn wedge — background work resolved, "
                                "evicting winding-down client [session=%s]", session_id
                            )
                            if not _alive(_st):
                                await _gsp().remove_dead(session_id)
                            else:
                                await _gsp().remove_broken(session_id)
                            break
                    else:
                        await asyncio.wait({next_fut})
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
                    done, _ = await asyncio.wait({next_fut}, timeout=_wait)
                    if not done:
                        # Window elapsed — deliver what we have. The pending read is
                        # cancelled by the exit cleanup below; a message still
                        # buffered on the anyio receive stream is re-read by the next
                        # pump run and re-deduped via mark_unparked.
                        break

                _fut, next_fut = next_fut, None
                try:
                    message = _fut.result()
                except StopAsyncIteration:
                    # R1: the SDK generator ended CLEANLY. If we were winding
                    # down a force-cancelled turn, the killed turn's terminal
                    # ResultMessage will now NEVER arrive (the stream is
                    # finished) — and this pump is exiting, so nothing else can
                    # clear the flag. Exiting flagged would strand the pooled
                    # entry behind the refuse-on-stale gates forever (rebricked
                    # session). Take the same recovery as the read-error path:
                    # clear the flag and evict (a finished message stream
                    # cannot serve another turn anyway).
                    try:
                        from app.core.session_pool import (
                            get_session_pool as _gsp,
                            is_subprocess_alive as _alive,
                        )
                        _cs = _gsp().peek(session_id)
                        if _cs is not None and _cs.stale_turn_undrained:
                            _cs.stale_turn_undrained = False
                            logger.warning(
                                "Drain pump: stream ended before the killed "
                                "turn's ResultMessage arrived — clearing "
                                "stale_turn_undrained and evicting the "
                                "finished client [session=%s]", session_id
                            )
                            if _alive(_cs):
                                await _gsp().remove_broken(session_id)
                            else:
                                await _gsp().remove_dead(session_id)
                    except Exception:
                        pass
                    break

                # Feed EVERY message into the registry — this is what unfreezes the
                # indicator (start/update/complete_task) for the terminal
                # notification even before we deliver the unpark. The subagent-lane
                # launch recorder runs FIRST so a grandchild run_in_background
                # tool_use arriving between turns (a still-running backgrounded
                # sub-agent's inner activity — this pump is its only reader) is
                # marked before its TaskStarted is processed.
                _record_subagent_lane_launches(session_id, message)
                _record_task_message(session_id, message)

                # Re-dispatch a SLOW background sub-agent's inner activity (assistant
                # text + tool calls/results, identified by parent_tool_use_id) into the
                # SAME subagent inner group it would have filled in-stream. Without this
                # the node renders "completed / no activity recorded" because the parent
                # turn's receive_response() already returned before these arrived.
                # SCOPING (chip-flood fix): only when parent_tool_use_id resolves
                # to a launch the PARENT issued (was_background_launch) — a
                # GRANDCHILD's inner activity carries the SUB-agent's launch id,
                # which the parent never recorded, and must not bleed into the
                # parent's subagent nodes.
                try:
                    _inner_parent_tuid = getattr(message, "parent_tool_use_id", None)
                    if _inner_parent_tuid and task_registry.was_background_launch(
                        session_id, _inner_parent_tuid
                    ):
                        for _sub_ev in _subagent_events_from_message(message):
                            await dispatch_event(_sub_ev, sub_ectx, sub_state)
                except Exception as _exc:
                    ctx.debug(logger, f"Drain pump: subagent re-dispatch failed: {_exc}")

                # RC2: deliver a PARKED-state "Task Updated" chip. In-stream a
                # TaskUpdatedMessage (a SystemMessage subclass) yields a
                # {"type": "system", ...} event → handle_system → add_system_message
                # → live-store update_id bump → the chip renders. The pump had no
                # equivalent, so a status patch that arrives while parked was
                # structurally undeliverable (the park-poller keys on update_id and
                # had nothing to fetch). Convert + dispatch through the SAME handler
                # chain here. If the live store has no session (cleanup raced),
                # add_system_message logs + drops it — must not crash the pump.
                try:
                    _status_evs = _task_status_events_from_message(message)
                    if _status_evs:
                        _tu_tid = getattr(message, "task_id", None)
                        _tu_terminal = _task_update_terminal_status(message) is not None
                        # SCOPING (chip-flood fix): only the parent's OWN
                        # background work may chip — a grandchild's status
                        # patches (launched BY a sub-agent, never tracked by
                        # the parent) used to flood the parent chat.
                        _chip_ok = bool(
                            _tu_tid
                            and task_registry.is_own_background_task(session_id, _tu_tid)
                        )
                        # COALESCING: max TWO chips per task lifetime — first
                        # sighting + first terminal (registry-level one-shot
                        # latches, so the cap holds across pump restarts).
                        # Intermediate running/paused patches render nothing
                        # after the first sighting.
                        if _chip_ok:
                            if _tu_terminal:
                                _chip_ok = task_registry.mark_chipped(
                                    session_id, _tu_tid, "terminal"
                                )
                                # Spend the start latch too: a late/out-of-order
                                # non-terminal patch must not chip AFTER the
                                # terminal card.
                                task_registry.mark_chipped(session_id, _tu_tid, "start")
                            else:
                                _chip_ok = task_registry.mark_chipped(
                                    session_id, _tu_tid, "start"
                                )
                        if _chip_ok:
                            for _status_ev in _status_evs:
                                await dispatch_event(_status_ev, sub_ectx, sub_state)
                            if _tu_terminal:
                                # A TERMINAL TaskUpdated chip rendered — record it
                                # so a following terminal notification for the
                                # SAME task does not synthesize a duplicate chip
                                # below (this per-run set's original role).
                                _chipped_task_ids.add(_tu_tid)
                except Exception as _exc:
                    ctx.debug(logger, f"Drain pump: task-status chip re-dispatch failed: {_exc}")

                # ATTACH-AND-STREAM (CLI-owned wake, round 4): a top-level
                # backgrounded Task/Agent coming to rest while the parent is
                # parked makes the CLI start its OWN follow-up turn on the pooled
                # client — an injected user-lane string plus the model's answer.
                # This pump is the only reader, and it used to drop all top-level
                # user/assistant content, so that answer reached only the durable
                # SDK transcript mirror (visible after refresh) but never the live
                # store — the actively-watched UI showed nothing. Forward the
                # CLI-driven turn's top-level events through the SAME dispatch
                # pipeline a normal turn uses (live-store append + update_id bump
                # + broadcasts) so park-pollers render it live. NOT while winding
                # down a force-cancelled turn: the killed turn's residual output
                # must stay dropped (the user stopped it).
                if not _stale_flag_set():
                    try:
                        _cli_evs = _cli_turn_events_from_message(
                            session_id, message, _cli_tool_names
                        )
                        if _cli_evs and not _cli_turn_forwarded:
                            # FIRST forwarded event of a CLI-owned wake turn —
                            # emit the client-facing engagement signal a normal
                            # turn's run_query emits at start. A connected but
                            # IDLE viewing client's poller went idle when the
                            # parent turn ended (the #502 bounded park poll only
                            # covers fresh loads / time-bounded windows); its
                            # ONLY rescue is the WS parked-watcher, which restarts
                            # polling on a stream_start/chunk/end broadcast.
                            # dispatch_event below DOES broadcast stream_start via
                            # the handler chain — but ONLY once, gated behind the
                            # pump's shared ``sub_state.stream_started`` latch that
                            # never resets across the pump's lifetime, so the
                            # SECOND+ CLI wake turn lands silently (content in the
                            # live store, update_id bumped, but no WS signal) and
                            # an idle client renders nothing until a manual
                            # refresh — the observed bug. Broadcast stream_start
                            # explicitly for EVERY wake turn's first forwarded
                            # event so any connected client re-engages its poller.
                            try:
                                await sync_engine.broadcast_stream_start(
                                    session_id=session_id,
                                    message_id=ctx.message_id,
                                    source_device_id=None,
                                    usage=None,
                                    user_id=user_id,
                                    project_id=project_id,
                                    worktree_id=worktree_id,
                                )
                            except Exception as _bexc:
                                ctx.debug(
                                    logger,
                                    "Drain pump: CLI-wake stream_start broadcast "
                                    f"failed: {_bexc}",
                                )
                            # WAKE-TURN STREAMING STATE (Half 1): the CLI's own
                            # wake turn is now streaming content into the live
                            # store — reflect it on the live session so /live
                            # reports is_streaming=true and clients treat it
                            # like a real turn (composer locks, pollers
                            # upshift). set_streaming mutates the EXISTING
                            # LiveSession in place — NEVER start_streaming here
                            # (it rebuilds the session and wipes the parked
                            # message buffer the wake turn appends to). The
                            # pump's own start gate reads the POOL
                            # SessionState.is_streaming, a distinct flag this
                            # never touches. Cleared by the finalize's
                            # end_streaming below and by every pump exit path
                            # (leak guard: a stranded True freezes the
                            # composer with no turn left to ever end it).
                            try:
                                await live_session_store.set_streaming(
                                    session_id, True
                                )
                            except Exception as _sexc:
                                ctx.debug(
                                    logger,
                                    "Drain pump: wake set_streaming(True) "
                                    f"failed: {_sexc}",
                                )
                            # Belt-and-braces for a park-poller mid-cycle: keep the
                            # unpark_pending latch UP while this wake turn is being
                            # forwarded so a poll that samples between complete_task
                            # and the turn finalize doesn't read (is_streaming=False,
                            # activeTasks=[]) and stop. Released at the turn's
                            # ResultMessage finalize (and the exit paths via
                            # ``_cli_wake_latched``).
                            task_registry.mark_unpark_pending(session_id)
                            _cli_wake_latched = True
                        for _cli_ev in _cli_evs:
                            if _cli_ev.get("type") == "thinking_block":
                                # No dispatchable thinking event type exists —
                                # append + finalize directly (complete blocks).
                                await live_session_store.append_thinking(
                                    session_id, _cli_ev.get("content") or ""
                                )
                                await live_session_store.finalize_thinking(session_id)
                            else:
                                await dispatch_event(_cli_ev, sub_ectx, sub_state)
                        if _cli_evs:
                            _cli_turn_forwarded = True
                    except Exception as _exc:
                        ctx.debug(
                            logger, f"Drain pump: CLI-turn forward failed: {_exc}"
                        )

                # STALE-TURN TERMINAL FRAME: the killed turn's own ResultMessage is
                # buffered on the pooled client's read loop. Consuming it here is what
                # lets us clear stale_turn_undrained — the CLI is now genuinely idle, so
                # a follow-up query can safely reuse the client. Placed BEFORE the
                # TaskUpdatedMessage branch since a ResultMessage is neither a task
                # update nor a notification.
                if isinstance(message, ResultMessage):
                    _rs_state = get_session_pool().peek(session_id)
                    if _rs_state is not None and _rs_state.stale_turn_undrained:
                        _rs_state.stale_turn_undrained = False
                        logger.info(
                            "Drain pump: consumed killed turn's terminal ResultMessage — "
                            "cleared stale_turn_undrained [session=%s]", session_id
                        )
                        if pending:
                            # Completions HELD during the wind-down (F2): the batch
                            # window was deliberately NOT armed while the flag was
                            # set (delivering mid-wind-down gets refused by the
                            # stale reuse gate, and the mark_unparked dedup would
                            # lose the result forever). Arm it NOW so the post-loop
                            # delivery fires with the flag cleared.
                            if batch_deadline is None:
                                batch_deadline = (
                                    time.monotonic() + _UNPARK_BATCH_WINDOW_SECONDS
                                )
                            continue
                        if (
                            not suppress_queue_kick
                            and session_id not in _stopped_sessions
                            and session_id not in _interrupt_pending_sessions
                            and process_next_queued is not None
                        ):
                            try:
                                _queue = await run_sync(database.get_queued_messages, session_id)
                            except Exception:
                                _queue = []
                            if _queue:
                                # A queued (e.g. send-now replacement) message is
                                # waiting — the client is idle now, so kick the queue.
                                # Remove ourselves first (same self-deadlock guard as the
                                # unpark path) since _process_next_queued may start a turn
                                # whose stop_drain_pump would otherwise cancel/await us.
                                logger.info(
                                    "Drain pump: stale turn drained + queue non-empty — "
                                    "kicking _process_next_queued [session=%s]", session_id
                                )
                                _drop_drain_pump_self(session_id, _drain_pumps)
                                await process_next_queued(session_id)
                                return
                        if suppress_queue_kick:
                            # Stop-origin pump (F4): the user pressed Stop — a stop
                            # "does NOT auto-fire queued messages", and the
                            # _stopped_sessions latch cannot carry that intent here
                            # (run_query's finally consumed it before this pump
                            # started). The queue stays intact for manual sending.
                            logger.info(
                                "Drain pump: stale turn drained (stop-origin) — queue "
                                "kick suppressed [session=%s]", session_id
                            )
                        # No pending unpark, no deliverable queue (or Stop/interrupt
                        # latched / stop-origin) — keep looping on normal unpark duty.
                        continue
                    # CLI-DRIVEN TURN CAME TO REST: this ResultMessage closes a
                    # turn the CLI started on its own (content was forwarded
                    # above). Finalize the live view exactly like a normal turn
                    # end: end_streaming marks the forwarded messages
                    # non-streaming, filters the hidden <task-notification>
                    # user rows, flags a full refresh + bumps update_id, and
                    # clears the unpark_pending latch we armed at the wake (its
                    # job — keeping the park-poller alive through the CLI turn —
                    # is done; activeTasks keeps the poller up if other
                    # background work remains). Skipped when an app unpark
                    # delivery is pending: _deliver_unpark_impl runs its own
                    # end_streaming + re-latch, and an early clear here would
                    # reopen the #473 poller-strand gap mid-window.
                    if _cli_turn_forwarded:
                        _cli_turn_forwarded = False
                        _cli_wake_latched = False
                        # WAKE-TURN INTERRUPT FINALIZE (Half 2): a user Stop
                        # during a CLI-owned wake reaches this ResultMessage
                        # with the _stopped_sessions latch armed (handle_stop
                        # latches BEFORE interrupt_session) — or the CLI itself
                        # reports the turn errored. Finalizing such a wake as a
                        # CLEAN end (the old hardcoded interrupted=False) lied
                        # to every client, and — since a wake turn has no
                        # run_query finally to consume the latches — leaked
                        # _stopped_sessions (suppressing every later queue
                        # kick) and unpark_pending (stranding the park-poller).
                        _wake_interrupted = (
                            session_id in _stopped_sessions
                            or bool(getattr(message, "is_error", False))
                            or str(getattr(message, "subtype", "") or "").startswith("error")
                            or getattr(message, "stop_reason", None)
                            in ("interrupt", "interrupted")
                        )
                        if _wake_interrupted and pending:
                            # A user Stop ends the whole park/wake episode: the
                            # held unparks must not auto-start a follow-up turn
                            # (same intent as the post-loop stopped-guard,
                            # which would have dropped them anyway).
                            logger.info(
                                "Drain pump: wake turn interrupted — dropping "
                                "%d held unpark(s) [session=%s]",
                                len(pending), session_id,
                            )
                            pending.clear()
                            batch_deadline = None
                        if not pending:
                            try:
                                await live_session_store.end_streaming(
                                    session_id=session_id,
                                    metadata={},
                                    interrupted=_wake_interrupted,
                                )
                                # Matching engagement-END signal for the
                                # stream_start we broadcast when this wake turn's
                                # first event was forwarded. ``end_streaming``
                                # above only MUTATES the live store (marks the
                                # forwarded messages non-streaming + bumps
                                # update_id) — it does NOT broadcast over the WS,
                                # so without this a client that engaged on the
                                # stream_start never sees the turn close (and the
                                # parked-watcher's stream_end restart is lost).
                                try:
                                    await sync_engine.broadcast_stream_end(
                                        session_id=session_id,
                                        message_id=ctx.message_id,
                                        metadata={},
                                        interrupted=_wake_interrupted,
                                        source_device_id=None,
                                    )
                                except Exception as _bexc:
                                    ctx.debug(
                                        logger,
                                        "Drain pump: CLI-wake stream_end "
                                        f"broadcast failed: {_bexc}",
                                    )
                                logger.info(
                                    "Drain pump: CLI-driven turn finalized in "
                                    "live view (interrupted=%s) [session=%s]",
                                    _wake_interrupted,
                                    session_id,
                                )
                            except Exception as _exc:
                                ctx.debug(
                                    logger,
                                    f"Drain pump: CLI-turn finalize failed: {_exc}",
                                )
                        if _wake_interrupted:
                            # Leak cleanup: no run_query finally exists for a
                            # CLI-owned wake, so consume the stop latch here —
                            # a stranded entry suppresses every later queue
                            # kick — and drop the unpark-pending latch
                            # (end_streaming above also clears it; this covers
                            # the finalize paths that skipped it).
                            _stopped_sessions.discard(session_id)
                            task_registry.clear_unpark_pending(session_id)
                    continue

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
                    _u_record = task_registry.get_task(session_id, _u_task_id)
                    if _u_record is None:
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
                    # Shutdown-origin attribution (item 4a): a kill landing
                    # while the server tears down is a restart consequence —
                    # the unpark payload's summary must say so (the payload is
                    # what reaches the parent's <task-notification> prompt).
                    from app.core import startup as _startup_mod
                    from app.core.task_registry import (
                        SERVER_SHUTDOWN_SUMMARY as _SD_SUMMARY,
                    )
                    _u_summary = (
                        _SD_SUMMARY
                        if getattr(_startup_mod, "is_shutting_down", False)
                        else None
                    )
                    # ENRICH (kill-notification husk): a killed task still has a
                    # registry record carrying kind/title and the launch
                    # tool_use_id. Fall back to the record's own summary, then a
                    # derived "<kind> <status>: <title>" line, so the parent's
                    # notification is actionable rather than a contentless husk.
                    # output_file/usage stay None — a killed shell has no such data.
                    _u_summary = (
                        _u_summary
                        or _u_record.get("summary")
                        or _kill_summary_from_record(_u_record, _u_status)
                    )
                    pending.append({
                        "task_id": _u_task_id,
                        "status": _u_status,
                        "summary": _u_summary,
                        "output_file": None,
                        "uuid": _u_uuid,
                        "usage": None,
                        "tool_use_id": _u_record.get("toolUseId"),
                    })
                    # Latch unpark-pending the MOMENT the completion is consumed —
                    # see the notification path below for why this cannot wait until
                    # ``_deliver_unpark_impl``.
                    task_registry.mark_unpark_pending(session_id)
                    # Consolidate (Bug 1, policy #3): keep draining within the batch
                    # window instead of breaking, so completions landing close together
                    # fold into ONE follow-up turn.
                    # F2: while winding down a force-cancelled turn, HOLD the
                    # pending completion — do NOT arm the batch window. Delivering
                    # before the stale ResultMessage clears the flag would be
                    # refused by the stale reuse gate, and the mark_unparked dedup
                    # means it never re-fires. The ResultMessage branch arms the
                    # window once the flag clears.
                    if batch_deadline is None and not _stale_flag_set():
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
                                "summary": frame.get("summary"),
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
                # Third alternative: a background task launched FROM a sub-agent
                # sidechain (run_in_background semantics, parent_tool_id set).
                # The #470 gate keeps these out of BOTH sets above — correctly,
                # for pinning/chips — but that made this gate drop their wake
                # (decision=suppress-foreground) and strand a sub-agent parked
                # awaiting the task (2026-07-10 incident, tasks bqbeiwvv6 /
                # b20nqkc3y). The dedicated marker admits them here ONLY; they
                # then ride the existing deliver machinery below unchanged.
                _bg_launch = task_registry.was_background_launch(
                    session_id, frame.get("tool_use_id")
                )
                # Subagent-lane pair mirrors the parent-lane pair (launch
                # tool_use_id OR task_id): the task-id marker is mapped at
                # TaskStarted, but a flag-less builtin's (Monitor's)
                # TaskStarted may not carry a usable tool_use_id to map — its
                # terminal notification demonstrably DOES carry the launch
                # tool_use_id (live 2026-07-10 b5qiqqf1h transcript:
                # <tool-use-id>toolu_01JuRn…</tool-use-id>), so accept that
                # directly too. Both sets are armed ONLY for subagent-lane
                # background launches; neither admits foreground work.
                _subagent_bg = task_registry.is_subagent_background_task(
                    session_id, frame.get("task_id")
                ) or task_registry.was_subagent_background_launch(
                    session_id, frame.get("tool_use_id")
                )
                if not (
                    _bg_launch
                    or task_registry.is_sdk_background_task(session_id, frame.get("task_id"))
                    or _subagent_bg
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

                # CLI-OWNED WAKE (round 4 of the duplicate-notification fix): a
                # top-level backgrounded Task/Agent that comes to rest while the
                # parent is AT REST is a class the CLI ALWAYS wakes itself — it
                # injects its own "[SYSTEM NOTIFICATION …] <task-notification>"
                # user-lane turn that the model answers (verified live 2026-07-09,
                # ses-f0b1e4e8a896). The app's synthesized unpark for the same
                # completion was the SECOND, duplicate wake turn. The typed
                # message reaches this pump BEFORE the CLI's injection enters the
                # stream (+6ms…+5.8s observed), which is why every latch design
                # keyed on observing the injection text failed (rounds 1–3: the
                # token was spent here first). So the gate keys on the LAUNCH
                # CLASS instead, never on message text: was_background_launch on
                # the completion's tool_use_id identifies the top-level
                # backgrounded launch, and the registry record's kind carves out
                # backgrounded SHELLS (their launch tool_use_ids share the same
                # set, but the CLI does NOT reliably self-deliver shell
                # completions — they keep the app unpark below, as do
                # killed/stopped TaskUpdated-alone terminals). Spend
                # mark_unparked as dedup bookkeeping, keep the completion chip,
                # arm unpark_pending so the park-poller stays alive until the
                # CLI's own turn streams through the forward path above, and
                # deliver NOTHING.
                _rec = task_registry.get_task(session_id, task_id)
                if _bg_launch and (_rec or {}).get("kind") != "shell":
                    _first = task_registry.mark_unparked(session_id, task_id)
                    _log_unpark_decision(
                        path="pump",
                        session_id=session_id,
                        task_id=task_id,
                        status=status,
                        signature=signature,
                        started_this_turn=task_registry.was_started_this_turn(session_id, task_id),
                        decision="cli-owned-wake" if _first else "dedup",
                    )
                    if _first:
                        # Same parked-state completion chip the deliver path
                        # renders (RC2) — the wake itself is CLI-owned but the
                        # completion card is still the app's to surface.
                        # mark_chipped("terminal") is the lifetime cap (max one
                        # terminal chip per task, across pump runs); the per-run
                        # _chipped_task_ids keeps its original in-run
                        # TaskUpdated-vs-notification dedup role.
                        if (
                            task_id
                            and task_id not in _chipped_task_ids
                            and task_registry.mark_chipped(session_id, task_id, "terminal")
                        ):
                            task_registry.mark_chipped(session_id, task_id, "start")
                            try:
                                for _chip_ev in _task_status_event_from_notification(message):
                                    await dispatch_event(_chip_ev, sub_ectx, sub_state)
                                _chipped_task_ids.add(task_id)
                            except Exception as _exc:
                                ctx.debug(logger, f"Drain pump: notification chip synth failed: {_exc}")
                        # Keep the park-poller alive across the gap until the
                        # CLI's injection turn arrives (complete_task already
                        # emptied activeTasks, so /live would otherwise read as
                        # idle and the poller would stop before the CLI turn's
                        # content lands). Released by the CLI turn's
                        # ResultMessage (end_streaming above), a new turn's
                        # start_streaming, or the pump exit paths.
                        task_registry.mark_unpark_pending(session_id)
                        _cli_wake_latched = True
                        # STUCK-ACTIVE SUBAGENT CARD FIX: re-engage an IDLE
                        # watcher's poller so it fetches the just-completed
                        # subagent card. The terminal task_notification dispatch
                        # above marked the subagent node done in the live store,
                        # but a background (run_in_background) subagent's card +
                        # "N/M tools" counter is rendered SOLELY from the /live
                        # park-poll — never from the direct WS frame — so it only
                        # flips active→done once the poller polls AFTER the node
                        # completes. mark_unpark_pending (above) keeps /live
                        # reporting busy, but that only helps a poller that is
                        # STILL polling: a watcher whose bounded park-poll (#502)
                        # already expired when the parent turn ended has a STOPPED
                        # poller, and the node-completion + chip broadcasts
                        # (subagent_*/system event_types) are dropped by the FE's
                        # idle-watcher shouldNotify gate (ws-message-handlers.ts —
                        # only stream_start/chunk/end pass while isStreaming=false).
                        # So the card froze one short (M-1) + spinning until a full
                        # reload read the persisted terminal state. The CLI-owned
                        # wake path's own re-engagement broadcast (broadcast_stream_
                        # start) only fires WHEN the CLI's injection turn is
                        # forwarded (which lands +6ms…+5.8s later, or never for a
                        # completion the model does not answer). Emit a neutral
                        # stream_chunk nudge NOW so the stopped poller restarts
                        # (notifier → startStreaming, self-limiting on the next
                        # idle /live tick) and renders the completed card
                        # immediately. Distinct seam from #543 (that fix keeps the
                        # origin device registered for deferred run_query WAKE
                        # turns — the app-unpark "deliver" branch below; this pump
                        # branch never starts a run_query). stream_chunk (not
                        # stream_start) avoids a spurious streaming-indicator flip
                        # (no _streaming_sessions mutation, no optimistic
                        # isStreaming=true on the FE) — it is a pure "re-fetch".
                        try:
                            await sync_engine.broadcast_stream_chunk(
                                session_id=session_id,
                                message_id=ctx.message_id,
                                chunk_type="subagent_tool_result",
                                chunk_data={"task_id": task_id, "status": status},
                                source_device_id=None,
                            )
                        except Exception as _bexc:
                            ctx.debug(
                                logger,
                                "Drain pump: stuck-card re-engagement broadcast "
                                f"failed: {_bexc}",
                            )
                    continue

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
                    # "deliver-subagent": same deliver path, but admission came
                    # via the subagent-lane marker (distinct label for live
                    # diagnosis of the grandchild-wake class).
                    decision="deliver-subagent" if _subagent_bg else "deliver",
                )
                # RC2 (parked "Task Updated" chip): a normal background completion
                # arrives between turns ONLY as this TaskNotificationMessage (no
                # task_updated patch), so the generic converter above yielded
                # nothing and — while PARKED — no completion card ever rendered.
                # Synthesize the SAME {"type":"system","subtype":"task_updated"}
                # chip the in-stream loop renders and dispatch it through the
                # identical handle_system -> add_system_message chain (live-store
                # update_id bump -> park-poller renders it). Only on the DELIVER
                # path: suppress-foreground (synchronous sub-agent) and dedup
                # notifications continue'd out above, so — like in-stream — a
                # foreground/duplicate completion drops no card. Guard against a
                # task_updated chip already emitted for this task this run, AND
                # against a terminal chip already latched for its lifetime
                # (mark_chipped — the cross-run cap).
                if (
                    task_id
                    and task_id not in _chipped_task_ids
                    and task_registry.mark_chipped(session_id, task_id, "terminal")
                ):
                    task_registry.mark_chipped(session_id, task_id, "start")
                    try:
                        for _chip_ev in _task_status_event_from_notification(message):
                            await dispatch_event(_chip_ev, sub_ectx, sub_state)
                        _chipped_task_ids.add(task_id)
                    except Exception as _exc:
                        ctx.debug(logger, f"Drain pump: notification chip synth failed: {_exc}")
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
                # F2: while the stale flag is set, HOLD the pending unpark — do
                # NOT arm the batch window. Delivering mid-wind-down gets refused
                # by the stale reuse gate and ``mark_unparked``'s dedup loses the
                # result forever. The ResultMessage branch arms the window the
                # moment it clears the flag.
                if batch_deadline is None and not _stale_flag_set():
                    batch_deadline = time.monotonic() + _UNPARK_BATCH_WINDOW_SECONDS
                continue
        finally:
            # Pump exit (return/break/cancel/error): cancel the read future we
            # own so no detached __anext__ keeps running against the client
            # (F1: the loop itself never cancels it); if it already completed,
            # consume its result/exception so nothing logs "exception was
            # never retrieved".
            if next_fut is not None:
                if not next_fut.done():
                    next_fut.cancel()
                else:
                    try:
                        next_fut.result()
                    except BaseException:
                        pass
                next_fut = None
    except asyncio.CancelledError:
        # Stopped by stop_drain_pump (a new turn is starting) — exit without
        # firing. Drop our early latch (if any): no follow-up will stream for the
        # pending we collected, and the new turn's own start_streaming would only
        # clear it if that turn actually starts. A CLI-owned-wake latch is dropped
        # for the same reason — this pump can no longer forward the CLI's turn,
        # and the new turn's start_streaming re-covers poller liveness. Re-raise
        # so the awaiter sees the cancellation.
        if pending or _cli_wake_latched:
            task_registry.clear_unpark_pending(session_id)
        # Leak guard (Half 1): a wake turn was mid-forward — drop the live
        # store's streaming flag or it strands True forever (frozen composer).
        if _cli_wake_latched:
            try:
                await live_session_store.set_streaming(session_id, False)
            except Exception:
                pass
        _drop_drain_pump_self(session_id, _drain_pumps)
        raise
    except Exception as exc:
        ctx.debug(logger, f"Drain pump error (swallowed): {exc}")
        if pending or _cli_wake_latched:
            task_registry.clear_unpark_pending(session_id)
        # Leak guard (Half 1): same stranded-True hazard as the cancel path.
        if _cli_wake_latched:
            try:
                await live_session_store.set_streaming(session_id, False)
            except Exception:
                pass
        # If we were winding down a force-cancelled turn when the read stream died,
        # evict: a dead read loop makes the client broken by definition — its stale
        # frame will never be consumed, so it must not stay pooled behind the
        # refuse-on-stale gate forever. remove_dead if the subprocess is gone,
        # else remove_broken (subprocess alive but read loop unusable).
        try:
            from app.core.session_pool import (
                get_session_pool as _gsp,
                is_subprocess_alive as _alive,
            )
            _es = _gsp().peek(session_id)
            if _es is not None and _es.stale_turn_undrained:
                _es.stale_turn_undrained = False
                if _alive(_es):
                    await _gsp().remove_broken(session_id)
                else:
                    await _gsp().remove_dead(session_id)
        except Exception:
            pass
        _drop_drain_pump_self(session_id, _drain_pumps)
        return

    # Remove OURSELVES from the registry BEFORE spawning the follow-up turn so the
    # follow-up's start-of-turn stop_drain_pump() does not cancel/await the very
    # pump that is spawning it (self-deadlock).
    _drop_drain_pump_self(session_id, _drain_pumps)

    # A normal (non-cancel) loop exit with a CLI wake still latched and nothing
    # to deliver means the pooled read stream ended (StopAsyncIteration /
    # wedge-evict) before the CLI's injection turn arrived — it can no longer be
    # forwarded by this pump, so drop the latch or a park-poller would spin
    # forever on a wake that will never stream. The durable SDK transcript
    # mirror still has the turn (it surfaces on reload).
    if _cli_wake_latched and not pending:
        task_registry.clear_unpark_pending(session_id)

    # Leak guard (Half 1): a wake still latched at a normal exit means its
    # ResultMessage never arrived through this pump — drop the live store's
    # streaming flag or it strands True forever (frozen composer). When
    # ``pending`` will be delivered below, the delivery's own end_streaming /
    # start_streaming supersedes this; clearing first is an idempotent no-op.
    if _cli_wake_latched:
        try:
            await live_session_store.set_streaming(session_id, False)
        except Exception:
            pass

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
            # And the wake-turn streaming flag: a finalize-with-pending left it
            # to this delivery, which is now suppressed (idempotent if clear).
            try:
                await live_session_store.set_streaming(session_id, False)
            except Exception:
                pass
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

        # A genuine user message clears the compact-boundary auto-continue
        # one-shot latch: the next boundary cut may auto-continue again.
        if session_id:
            compact_continue.reset_auto_continue(session_id)

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
                    # 'claude-code' is this handler's "no explicit profile"
                    # default, not a real profile row — never send it across
                    # the relay (the owner would fail get_profile on it).
                    await cloud_relay.relay_message(
                        cloud_sess, prompt,
                        profile_id if profile_id != "claude-code" else None,
                        overrides,
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
            # Route to the queue when a turn is live OR when the pooled client is
            # still winding down a force-cancelled turn (stale_turn_undrained): a
            # follow-up query would cross the buffered stale ResultMessage / write
            # into a possibly mid-turn CLI. Queuing keeps the send-now UX — the
            # message auto-fires once the drain pump clears the flag.
            _stale = False
            if active_task is None or active_task.done():
                from app.core.session_pool import get_session_pool, is_subprocess_alive
                _peek = get_session_pool().peek(session_id)
                if _peek is not None and _peek.stale_turn_undrained:
                    if is_subprocess_alive(_peek):
                        _stale = True
                    else:
                        # R2: a DEAD subprocess can't be "winding down" — no
                        # pump will ever clear the flag, so queue-routing here
                        # would sit in queue-limbo until pool idle eviction.
                        # Clear the flag, drop the dead entry, and fall through
                        # to the normal path (which builds a fresh client).
                        _peek.stale_turn_undrained = False
                        await get_session_pool().remove_dead(session_id)
                        logger.warning(
                            f"[BusyGuard] session {session_id} carried "
                            f"stale_turn_undrained on a DEAD subprocess — "
                            f"cleared flag, removed dead pool entry, "
                            f"proceeding with a fresh client"
                        )
            if (active_task is not None and not active_task.done()) or _stale:
                logger.info(
                    f"[BusyGuard] query for session {session_id} arrived while "
                    f"{'mid-turn' if not _stale else 'winding down a cancelled turn'} "
                    f"— enqueueing instead of cancelling/crossing the turn"
                )
                await self.handle_queue_message({
                    "session_id": session_id,
                    "content": prompt,
                    "profile": data.get("profile"),
                    "project": project_id,
                })
                # F5: ack the auto-route. The client sent this query expecting a
                # turn to start (e.g. the send-now replacement right after
                # interrupt_confirmed); without an explicit ack it sits on its
                # optimistic streaming state forever (unbounded spinner — the
                # poller discards idle polls while interruptPending is latched).
                # queue_updated (broadcast above) carries the queue contents; this
                # frame tells THIS client its query was queued, not started. The
                # exact session_id matters: the FE ignores cross-session frames.
                await self.send_json({"type": "query_queued", "session_id": session_id})
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

        # Start new query task — busy-write guard (late re-check): the liveness
        # check and the registration happen under the SAME lock acquisition, so
        # a turn that started while we were awaiting above (queue drain, unpark
        # follow-up, another device) is never clobbered.
        # We never bare-cancel a live turn here: cancelling without an SDK
        # interrupt leaves the CLI mid-turn, and the next client.query() into
        # its stdin kills it and loses the whole in-flight turn.
        # The user-message persist + broadcast are deliberately ordered AFTER
        # this check-and-register (below): persisting first meant a preempted
        # query enqueued an already-persisted prompt, and the queue drain later
        # persisted it a second time (duplicate user-message row).
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
            # live turn's post-turn queue drain picks it up. NOTHING has been
            # persisted or broadcast for this prompt yet (the persist below
            # runs only after a WON registration), so the drain's persist is
            # the one and only persist — no duplicate user-message row.
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

        # Persist + broadcast the user message now that THIS query owns the
        # turn (existing sessions only — a new session persisted its first
        # message atomically at creation and cannot be preempted: nothing else
        # knows its freshly minted id). The running turn never misses the
        # prompt: run_query hands it to the SDK directly and the live store
        # carries it via add_user_message; session_messages is not on the
        # streaming-init read path. A failure here must not tear down the
        # already-running turn — log and continue.
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
        except Exception as persist_exc:  # noqa: BLE001
            logger.warning(
                f"[BusyGuard] post-registration user-message persist/broadcast "
                f"failed for session {session_id} (turn already running): "
                f"{persist_exc}"
            )

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
                    # NOTE: the `stop_background` opt-in is NOT propagated to
                    # remote-owned sessions — the relay protocol carries only the
                    # bare interrupt. The safe default (keep background agents) is
                    # preserved on the owning install; the "also stop them" opt-in
                    # is a known cross-install gap tracked for a follow-up.
                    return

            from app.core.query_engine import interrupt_session, stop_all_background_tasks
            from app.core.cli_interrupt_patch import is_patch_active

            # Opt-in: also tear down in-flight background sub-agents. Only meaningful
            # when the preserve-background patch is BOTH enabled and actually active
            # — otherwise a plain interrupt already kills them, so the per-task loop
            # would be redundant. Gating here also makes flag-OFF a true no-op even
            # though the frontend always sends stop_background=true.
            stop_background = (
                bool(data.get("stop_background"))
                and settings.preserve_background_on_interrupt
                and is_patch_active()
            )

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
                                # Same broken-client hazard as interrupt_for_query:
                                # the CLI never honored the interrupt, so the next
                                # message must not reuse this pooled client — UNLESS
                                # live background work is parented to a still-alive
                                # subprocess. remove_broken → SIGTERM would kill
                                # every sub-agent/shell child; never trade live
                                # background work for eviction of a healthy client.
                                _keep = not _should_evict_after_escalation(sid)
                                # Flag stale BEFORE cancelling: the cancelled task's
                                # finally flips is_streaming back to False (disarming
                                # the busy gate), so this flag is the only signal that
                                # the pooled client is still winding down the killed
                                # turn's buffered frames. Guard on the pre-cancel
                                # is_streaming so a fresh replacement client (a
                                # concurrent escalation already evicted+recreated) is
                                # never mis-flagged.
                                _flagged = self._flag_stale_if_keeping(sid, keep=_keep)
                                t.cancel()
                                try:
                                    await t
                                except (asyncio.CancelledError, Exception):
                                    pass
                                if not _keep:
                                    from app.core.session_pool import get_session_pool
                                    await get_session_pool().remove_broken(sid)
                                elif _flagged:
                                    # Keep branch: start the between-turns drain pump
                                    # to consume the killed turn's terminal frame and
                                    # clear the stale flag. STOP-origin (F4): the
                                    # _stopped_sessions latch was already consumed by
                                    # the cancelled task's finally during `await t`,
                                    # so the no-auto-fire intent must ride an explicit
                                    # flag — a user Stop never surprise-drains the
                                    # message queue.
                                    await self._start_drain_pump_for_session(
                                        sid, suppress_queue_kick=True
                                    )
                        except asyncio.CancelledError:
                            pass
                    _t = asyncio.create_task(_safety_cancel(task, session_id))
                    _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

            # "Stop, and also stop background agents": drive each still-running
            # background task through the per-task stop path. Done LAST — the
            # foreground safety-cancel above is already armed (non-blocking
            # create_task), so a hung stop_task here can't delay the force-cancel.
            # Skipped by default so background sub-agents survive a plain Stop.
            if stop_background:
                await stop_all_background_tasks(session_id)

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

        # F6: when the drain-timeout keep-branch flags the client, the pump start
        # is deferred until AFTER the interrupt_confirmed send — the confirm is
        # what unblocks the user's replacement query and must never wait on the
        # pump start's run_sync(database.get_session) read.
        _start_pump_after_confirm = False

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
                    _keep = not _should_evict_after_escalation(session_id)
                    # Flag stale BEFORE cancelling (see handle_stop): the
                    # cancelled task's finally clears is_streaming, so this flag
                    # is the only remaining signal that the kept client is still
                    # winding down the killed turn's buffered frames.
                    _flagged = self._flag_stale_if_keeping(session_id, keep=_keep)
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                    if not _keep:
                        from app.core.session_pool import get_session_pool
                        await get_session_pool().remove_broken(session_id)
                    elif _flagged:
                        # Keep branch: start the between-turns drain pump to
                        # consume the killed turn's terminal frame and clear the
                        # stale flag (then re-kick any queued send-now message) —
                        # but only AFTER the confirm below (F6).
                        _start_pump_after_confirm = True

            # Step 3: Confirm to frontend — safe to send the query now
            await self.send_json({"type": "interrupt_confirmed", "session_id": session_id})
            logger.debug(f"interrupt_for_query: confirmed for session {session_id}")

            if _start_pump_after_confirm:
                # Send-now origin: the queue kick stays enabled so the queued
                # replacement message auto-fires once the pump clears the flag.
                await self._start_drain_pump_for_session(session_id)

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
        # (DB and live_store messages already include profile_id). Consume the
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

            # Protocol asymmetry guard: the local respond() tolerates loose
            # `remember` values (a boolean, per legacy callers) but the OWNER's
            # cluster endpoint validates `remember` against the strict Literal
            # 'none'|'session'|'profile' (hardened in 061dc947), so a boolean
            # forwarded verbatim 422s and — without this — collapses to the
            # RETRYABLE 'relay_offline' ack, making the frontend re-send the
            # same rejected payload forever. Normalize to the enum string here,
            # matching local respond() semantics exactly: falsy → no rule
            # ('none'); truthy-but-not-an-enum-string → the profile scope (a
            # legacy remember=True persists a profile-level rule, since
            # respond() enters the rule branch, isn't "session", and so falls
            # to the profile-persist else). Real enum strings pass through.
            eff_remember = _normalize_relay_remember(eff_remember)

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
                except cloud_relay.CloudRelayError as e:
                    # The peer was REACHED but rejected the request. A 4xx is an
                    # authoritative, non-retryable verdict (schema rejection /
                    # gone request) — surfacing it as 'relay_offline' would make
                    # the frontend re-send the same doomed payload forever. Only
                    # a 5xx (or an unknown status) is retryable → 'relay_offline'.
                    status = getattr(e, "status_code", None)
                    if status is not None and 400 <= status < 500:
                        fail_reason = "invalid_response"
                    else:
                        fail_reason = "relay_offline"
                    logger.warning(
                        f"Cluster relay_permission_response rejected for "
                        f"{response_session_id} (status={status}): {e}"
                    )
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
                    # Strip the 'claude-code' no-profile sentinel here too —
                    # the queued row's profile drives the owner-side drain.
                    await cloud_relay.relay_queue_message(
                        cloud_sess, content,
                        profile_id if profile_id != "claude-code" else None,
                        project_id,
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

        Race guards (closing the three latent gaps documented in a361648f):
        - Drain-once reservation: ``_draining_sessions`` is claimed under
          ``_active_sessions_lock`` BEFORE the pop, so at most one drain per
          session is inside the pop→register window at a time. Racing drains
          can therefore never pop two messages and start them out of queue
          order (the loser bails without popping anything).
        - The late check-and-register re-checks the stop/interrupt latches in
          the SAME lock acquisition as the liveness check: a Stop or
          interrupt_for_query landing between the pop and the registration
          aborts the drain (message re-queued at the front, latch left set —
          the latch owner consumes it) instead of being silently lost.
        - The user message is persisted + broadcast only AFTER the
          registration is won, so a preempted/latched drain re-queues a
          message that was never persisted — no duplicate user-message row.

        If anything fails after the message is popped from the queue, it is
        re-inserted at the front so the user never silently loses a message.
        """
        from app.api.websocket import (
            _active_chat_sessions,
            _active_sessions_lock,
            _draining_sessions,
        )

        # Drain-once reservation (see docstring). Claimed under the same lock
        # that serializes the check-and-register; released in the finally.
        async with _active_sessions_lock:
            if session_id in _draining_sessions:
                logger.info(
                    f"[Queue] Drain already in flight for session {session_id} "
                    f"— skipping (drain-once guard)"
                )
                return
            _draining_sessions.add(session_id)

        try:
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
            # Re-queue when a turn is live OR the pooled client is still winding
            # down a force-cancelled turn (stale_turn_undrained) — launching now
            # would cross the buffered stale ResultMessage / write into a mid-turn
            # CLI. The drain pump re-kicks this drain once it clears the flag.
            _stale = False
            if active_task is None or active_task.done():
                from app.core.session_pool import get_session_pool, is_subprocess_alive
                _peek = get_session_pool().peek(session_id)
                if _peek is not None and _peek.stale_turn_undrained:
                    if is_subprocess_alive(_peek):
                        _stale = True
                    else:
                        # R2: DEAD subprocess ⇒ nothing is winding down and no
                        # pump will ever clear the flag — re-queuing would loop
                        # forever (queue-limbo until idle eviction). Clear the
                        # flag, drop the dead entry, and launch on a fresh
                        # client.
                        _peek.stale_turn_undrained = False
                        await get_session_pool().remove_dead(session_id)
                        logger.warning(
                            f"[Queue] session {session_id} carried "
                            f"stale_turn_undrained on a DEAD subprocess — "
                            f"cleared flag, removed dead pool entry, "
                            f"launching queued message on a fresh client"
                        )
            if (active_task is not None and not active_task.done()) or _stale:
                logger.info(
                    f"[Queue] Session {session_id} is "
                    f"{'mid-turn' if not _stale else 'winding down a cancelled turn'} "
                    f"— re-queuing instead of cancelling/crossing it"
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

                # Look up worktree_id from session
                session_data = await run_sync(database.get_session, session_id)
                worktree_id = session_data.get("worktree_id") if session_data else None

                # Start the query — busy-write guard (late re-check): the
                # liveness check, the stop/interrupt-latch check, and the
                # registration all happen under ONE lock acquisition so a turn
                # that started — or a Stop/interrupt that latched — during the
                # awaits above is never clobbered/lost. If preempted or
                # latched, re-queue at the front so the message is not lost
                # (the latch itself is left set for its owner to consume).
                preempted = False
                latched = False
                stale = False
                async with _active_sessions_lock:
                    existing_task = _active_chat_sessions.get(session_id)
                    if existing_task is not None and not existing_task.done():
                        preempted = True
                    elif (
                        session_id in _stopped_sessions
                        or session_id in _interrupt_pending_sessions
                    ):
                        latched = True
                    else:
                        # Late stale-turn re-check: the pooled client may have been
                        # flagged winding-down (interrupt escalation) during the
                        # awaits above. Launching now would cross the buffered stale
                        # ResultMessage — re-queue and let the drain pump re-kick.
                        # R2: only a LIVE subprocess can be winding down. On a dead
                        # one no pump will ever clear the flag (re-queue loops
                        # forever) — clear it and launch normally; the turn path's
                        # reuse gate requires a live subprocess, so it builds a
                        # fresh client (no pool await under this lock).
                        from app.core.session_pool import get_session_pool, is_subprocess_alive
                        _peek = get_session_pool().peek(session_id)
                        if (
                            _peek is not None
                            and _peek.stale_turn_undrained
                            and is_subprocess_alive(_peek)
                        ):
                            stale = True
                        else:
                            if _peek is not None and _peek.stale_turn_undrained:
                                _peek.stale_turn_undrained = False
                                logger.warning(
                                    f"[Queue] session {session_id} carried "
                                    f"stale_turn_undrained on a DEAD subprocess at "
                                    f"the late re-check — cleared flag, launching "
                                    f"on a fresh client"
                                )
                            task = asyncio.create_task(
                                self.run_query(
                                    content, session_id, profile_id, project_id, None, worktree_id,
                                    queue_drain_source={
                                        "content": content,
                                        "profile_id": profile_id,
                                        "project_id": project_id,
                                    },
                                )
                            )
                            _active_chat_sessions[session_id] = task
                if preempted or latched or stale:
                    reason = (
                        "preempted before launch" if preempted
                        else "stop/interrupt latched mid-drain" if latched
                        else "client winding down a cancelled turn"
                    )
                    logger.info(
                        f"[Queue] Drain {reason} for session {session_id} — "
                        f"re-queuing instead of starting the turn"
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

            # Persist + broadcast the user message now that the turn is
            # registered and owns the session. Deliberately AFTER the
            # check-and-register: persisting before it meant a preempted drain
            # re-queued an already-persisted message, and the next drain
            # persisted it a second time (duplicate user-message row). The
            # running turn never misses the prompt — run_query hands it to the
            # SDK directly and the live store carries it via add_user_message;
            # session_messages is not on the streaming-init read path. A
            # failure here must NOT re-queue (the turn is already running —
            # re-queuing would run the message twice) — log and continue.
            try:
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
            except Exception as persist_exc:  # noqa: BLE001
                logger.warning(
                    f"[Queue] post-registration user-message persist/broadcast "
                    f"failed for session {session_id} (turn already running): "
                    f"{persist_exc}"
                )
        finally:
            _draining_sessions.discard(session_id)

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
        suppress_queue_kick: bool = False,
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
            process_next_queued=self._process_next_queued,
            suppress_queue_kick=suppress_queue_kick,
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
        suppress_queue_kick: bool = False,
    ) -> None:
        """Thin wrapper → ``_start_drain_pump_impl`` (shared with /ws/chat).

        Binds a factory producing a fresh ``self._drain_notifications`` coroutine
        so the pump reads the pooled client via THIS handler.
        ``suppress_queue_kick=True`` marks a STOP-origin pump (F4): it still
        drains the stale frames and clears the flag, but never auto-fires the
        message queue — a user Stop must not surprise-drain queued messages.
        """
        _start_drain_pump_impl(
            session_id,
            drain_coro_factory=lambda: self._drain_notifications(
                session_id, profile_id, project_id, worktree_id,
                suppress_queue_kick=suppress_queue_kick,
            ),
        )

    @staticmethod
    def _flag_stale_if_keeping(session_id: str, *, keep: bool) -> bool:
        """Flag the pooled state ``stale_turn_undrained`` for the keep branch.

        Set only when we are KEEPING the client (``keep``) AND the peeked pool
        state is actually mid-turn at fire time (``is_streaming`` True) — the
        guard prevents flagging a FRESH replacement client that a concurrent
        escalation already evicted-and-recreated. Returns True iff the flag was
        set. MUST be called BEFORE ``task.cancel()``: the cancelled task's finally
        flips ``is_streaming`` back to False, after which the pre-cancel guard can
        no longer be evaluated.
        """
        if not keep:
            return False
        from app.core.session_pool import get_session_pool
        state = get_session_pool().peek(session_id)
        if state is None or not state.is_streaming:
            return False
        state.stale_turn_undrained = True
        logger.info(
            "Interrupt escalation kept the client — flagged stale_turn_undrained "
            "[session=%s]", session_id
        )
        return True

    async def _start_drain_pump_for_session(
        self, session_id: str, *, suppress_queue_kick: bool = False
    ) -> None:
        """Start the between-turns drain pump from an escalation site.

        The escalation handlers lack the profile/project/worktree ids, so derive
        them from the durable session row (which carries all three). NEVER inject a
        ``or "claude-code"`` sentinel default here — that sentinel must never cross
        into remote/relay paths; the pump only uses profile_id when delivering an
        unpark follow-up, and a genuine session always has its real profile_id.

        ``suppress_queue_kick=True`` = STOP-origin (see ``_start_drain_pump``).
        """
        session_row = await run_sync(database.get_session, session_id)
        if session_row is None:
            logger.warning(
                "Cannot start drain pump after escalation — session row gone "
                "[session=%s]", session_id
            )
            return
        self._start_drain_pump(
            session_id,
            session_row.get("profile_id"),
            session_row.get("project_id"),
            session_row.get("worktree_id"),
            suppress_queue_kick=suppress_queue_kick,
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
        is_failover_retry: bool = False,
        failover_attempted: Optional[set] = None,
        queue_drain_source: Optional[Dict[str, Any]] = None,
        is_auto_continue: bool = False,
    ) -> None:
        """Execute query and stream results directly to WebSocket.

        ``is_failover_retry`` marks a successor turn spawned by the account
        auto-switch-on-limit seam: the ORIGINAL turn already persisted the user
        message (DB + live store) and the resumed stream seed already replays it,
        so this retry must NOT re-add the user message. ``failover_attempted``
        carries the per-turn set of account ids already tried (and 429'd on) so a
        chain of retries terminates once every account has been attempted.

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

        ``queue_drain_source`` marks this turn as a server-initiated queue-drain
        successor: it carries ``{"content", "profile_id", "project_id"}`` of the
        already-committed queued message that was popped to start this turn. Its
        prompt is therefore NOT a user-typed draft. If a Stop lands before this
        turn emits any output, the finally block RE-QUEUES that message at the
        front (``requeue_message_at_front``) instead of running the #420
        orphan-discard path — a committed queued message must never be silently
        dropped or bounced back into the input box as an abandoned draft.

        ``is_auto_continue`` marks a server-initiated continuation turn spawned by
        the context-boundary auto-continue seam (see
        ``app.core.compact_continue``). It runs like a normal deferred-delivery
        turn but must NEVER itself trigger another auto-continue: if this turn
        also cuts at the boundary, compaction already ran at its start, so a
        second cut is a different problem — we log it instead of chaining nudges.
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

        # Unregister this device from SyncEngine while streaming.
        # This prevents duplicate events (we get direct events via send_json)
        # If the device disconnects and reconnects, it will re-register and get sync events.
        # Pass self.websocket so we only unregister OUR connection — if user already
        # reconnected (new ws registered for same device_id), we must not nuke it.
        #
        # EXCEPTION — server-initiated wake turns (``deferred_delivery=True``): a
        # follow-up turn started by the drain pump / unpark path is NOT owned by
        # the client that is watching the session. Its only delivery channel is the
        # sync_engine broadcast (dispatch_event → broadcast_event, source_device_id
        # =None) to REGISTERED devices — direct ``send_json`` goes to this handler's
        # (often dead or headless-None) origin socket. Unregistering here can empty
        # ``sync_engine._connections[session_id]`` (broadcast_event silently no-ops
        # on an empty map), so every stream_start/chunk/end of the wake turn goes
        # nowhere live and the turn only surfaces on a full reload. KEEP the device
        # registered for deferred turns so the turn streams to it via broadcast.
        # A duplicate direct+broadcast edge on a still-alive origin socket is
        # harmless: the FE applies the direct ``chunk`` (type=…) as content but
        # treats the broadcast ``stream_chunk`` (event_type=…) only as a poller/
        # notifier trigger (disjoint code paths — see ws-message-handlers.ts), so
        # text is never double-appended. Mirrors how the CLI-owned wake pump path
        # already forwards to registered devices via sync_engine.
        if not deferred_delivery:
            await sync_engine.unregister_device(self.device_id, session_id, self.websocket)
            ctx.debug(logger, "Unregistered device during streaming to prevent duplicates")
        else:
            ctx.debug(logger, "Deferred-delivery wake turn — keeping device registered so it streams via sync_engine broadcast")

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
        await live_session_store.start_streaming(
            session_id, initial_messages, profile_id=profile_id
        )

        # Add user message to live store. On a failover retry this is normally
        # skipped: the resumed stream seed already contains the original user
        # message and the DB row was persisted by the original turn, so re-adding
        # would duplicate it. BUT if the ended live-store snapshot was cleaned up
        # in the hand-off window (no-devices cleanup / eviction), the seed can
        # LACK the user message — dropping the add then makes the prompt vanish
        # from the live view. So on a retry, skip only when the resolved seed
        # already carries this exact user prompt; default to ADDING otherwise.
        user_message_id = f"user-{uuid.uuid4()}"
        seed_has_user_prompt = any(
            isinstance(m, dict)
            and m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m.get("content") == prompt
            for m in (initial_messages or [])
        )
        if not (is_failover_retry and seed_has_user_prompt):
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

            # --- Account auto-switch-on-limit (failover) ---
            # If this turn ended on a usage-limit 429 and auto-switch is enabled,
            # rotate to another saved account and re-run the turn on the resumed
            # session so the user never has to intervene. Mirrors the queued-
            # message hand-off below: spawn a successor run_query, register it as
            # the session's active turn under the lock, mark handed_off, return.
            if (
                session_id not in _stopped_sessions
                and session_id not in _interrupt_pending_sessions
                and account_failover.is_usage_limit_error(stream_state.final_metadata)
            ):
                attempted = failover_attempted if failover_attempted is not None else set()
                decision = await account_failover.attempt_failover(
                    session_id, self.api_user_id, attempted
                )
                # attempt_failover can take seconds (credential rotation + pool
                # eviction). Re-check the stop/interrupt flags: if the user
                # stopped or interrupted during that await, do NOT spawn the
                # successor — fall through to normal completion.
                if (
                    decision.retry
                    and (session_id in _stopped_sessions
                         or session_id in _interrupt_pending_sessions)
                ):
                    ctx.debug(
                        logger,
                        "Failover: session stopped/interrupted during failover "
                        "await — not spawning successor",
                    )
                elif decision.retry:
                    ctx.debug(
                        logger,
                        f"Failover: retrying turn on account "
                        f"{decision.account_name} (switched={decision.switched})",
                    )
                    # End current streaming state so the successor can re-init.
                    await live_session_store.end_streaming(
                        session_id=session_id,
                        metadata=stream_state.final_metadata,
                        interrupted=False,
                    )
                    try:
                        task = asyncio.create_task(
                            self.run_query(
                                prompt, session_id, profile_id, project_id,
                                overrides, worktree_id,
                                deferred_delivery=deferred_delivery,
                                is_failover_retry=True,
                                failover_attempted=attempted,
                            )
                        )
                        async with _active_sessions_lock:
                            _active_chat_sessions[session_id] = task
                        self.query_task = task
                        handed_off_to_queued = True
                        ctx.debug(logger, "Failover: successor turn started (handed off)")
                        return  # Exit — the successor task continues streaming
                    except Exception as exc:
                        ctx.error(logger, f"Failover: failed to start successor turn: {exc}")
                        # Fall through to normal completion.

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

                    # Start new query inline — session stays active, no gap.
                    # Mark it a queue-drain successor: msg1 is already committed,
                    # so a Stop before output must re-queue it, not orphan-discard.
                    try:
                        task = asyncio.create_task(
                            self.run_query(
                                q_content, session_id, q_profile, q_project, None, worktree_id,
                                queue_drain_source={
                                    "content": q_content,
                                    "profile_id": q_profile,
                                    "project_id": q_project,
                                },
                            )
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

            # --- Context-boundary auto-continue ---
            # The bundled CLI ends the agentic loop when the NEXT turn would
            # cross the autocompact threshold and defers compaction to the start
            # of the next query, so the turn just ends mid-task
            # (stop_reason="tool_use") and the session goes silent until a human
            # sends a message. On an autonomous run that strands the work. If
            # this turn cut at the boundary AND context is over the autocompact
            # threshold AND nothing else already resumes it (queued message /
            # stop / interrupt), spawn ONE continuation turn so the CLI compacts
            # and the model resumes. Mirrors the failover hand-off: spawn a
            # successor run_query, register it under the lock, mark handed off,
            # return. deferred_delivery=True keeps the device registered so the
            # server-initiated turn streams live to open tabs (the #543 seam).
            # A continuation turn (is_auto_continue) never re-nudges: compaction
            # already ran at its start, so a second cut is a different problem.
            if (
                session_id not in _stopped_sessions
                and session_id not in _interrupt_pending_sessions
                and not handed_off_to_queued
                and not is_auto_continue
            ):
                decision = None
                try:
                    _has_queued = bool(
                        await run_sync(database.count_queued_messages, session_id)
                    )
                    decision = await compact_continue.should_auto_continue(
                        session_id,
                        stream_state.final_metadata,
                        is_stopped=session_id in _stopped_sessions,
                        is_interrupted=session_id in _interrupt_pending_sessions,
                        has_queued_user_message=_has_queued,
                    )
                except Exception as exc:  # noqa: BLE001
                    ctx.warning(logger, f"Auto-continue: decision failed: {exc}")

                if decision is not None and decision.should_continue:
                    logger.info(
                        "compact-boundary auto-continue session=%s preTokens=%s",
                        session_id, decision.pre_tokens,
                    )
                    # Latch the one-shot guard BEFORE spawning so a boundary cut
                    # on the continuation itself is not re-nudged.
                    compact_continue.mark_auto_continued(session_id)
                    try:
                        # Persist + broadcast the automatic message so it renders
                        # (labeled "[auto-continue]") and survives reload — mirror
                        # the queue-drain hand-off.
                        await run_sync(
                            database.add_session_message,
                            session_id=session_id,
                            role="user",
                            content=compact_continue.AUTO_CONTINUE_PROMPT,
                            profile_id=profile_id,
                        )
                        await sync_engine.broadcast_message_added(
                            session_id=session_id,
                            message={
                                "role": "user",
                                "content": compact_continue.AUTO_CONTINUE_PROMPT,
                                "profile_id": profile_id,
                            },
                            source_device_id=None,
                        )
                        # End current streaming state so the successor re-inits.
                        await live_session_store.end_streaming(
                            session_id=session_id,
                            metadata=stream_state.final_metadata,
                            interrupted=False,
                        )
                        task = asyncio.create_task(
                            self.run_query(
                                compact_continue.AUTO_CONTINUE_PROMPT,
                                session_id, profile_id, project_id, None, worktree_id,
                                deferred_delivery=True,
                                is_auto_continue=True,
                            )
                        )
                        async with _active_sessions_lock:
                            _active_chat_sessions[session_id] = task
                        self.query_task = task
                        handed_off_to_queued = True
                        ctx.debug(logger, "Auto-continue: continuation turn started (handed off)")
                        return  # Exit — the continuation task continues streaming
                    except Exception as exc:  # noqa: BLE001
                        ctx.error(logger, f"Auto-continue: failed to start continuation: {exc}")
                        # Fall through to normal completion.
            elif (
                is_auto_continue
                and compact_continue.is_abnormal_boundary_stop(stream_state.final_metadata)
            ):
                # The continuation turn itself cut at the boundary. Compaction
                # already ran at its start, so this is NOT the lazy-compaction
                # deferral — do not chain a nudge, just record it.
                logger.info(
                    "compact-boundary second cut on auto-continue turn — not re-nudging session=%s",
                    session_id,
                )

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
                #
                # Skipped for deferred-delivery wake turns: they never unregistered
                # above (the device stayed registered so the turn could stream via
                # broadcast), so re-registering here would be wrong — register_device
                # CLOSES the existing connection for the device before re-adding,
                # which would tear down the very live socket we kept for delivery.
                if deferred_delivery:
                    ctx.debug(logger, "Deferred-delivery wake turn — skipping re-register (device was never unregistered)")
                elif self.websocket is not None and self.websocket.client_state == WebSocketState.CONNECTED:
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

                # ── Generalized pre-output drain-source re-queue ──────────────
                # A server-initiated queue-drain successor (``queue_drain_source``
                # set) whose turn tears down BEFORE emitting any output must
                # re-queue its already-committed message at the FRONT — never drop
                # it — regardless of WHICH pre-output teardown latch fired. #554
                # (5074cee6) added this only for the user-Stop branch and left the
                # ``interrupt_for_query``-pending teardown as a deferred TOCTOU
                # follow-up: that branch just logged "Skipping queue processing"
                # and returned, silently losing the popped message. The pre-close
                # drain had persisted it ONLY to the legacy ``session_messages``
                # table, which the UI does NOT read under
                # ``session_store_mode=postgres_only``, so it vanished from both the
                # transcript and the queue (incident: session 61e0f967, 2026-07-11).
                # Running the same re-queue for ANY pre-output teardown latch closes
                # that gap.
                drain_source_requeued = False
                if (
                    queue_drain_source is not None
                    and not stream_state.stream_started
                    and (
                        session_id in _interrupt_pending_sessions
                        or session_id in _stopped_sessions
                    )
                ):
                    # Skip the #420 orphan-discard for this committed message even
                    # if the re-queue write below fails — never discard it.
                    drain_source_requeued = True
                    q_src_content = queue_drain_source.get("content", prompt)
                    q_src_profile = queue_drain_source.get("profile_id")
                    q_src_project = queue_drain_source.get("project_id")
                    # Drop the eager session_messages row the pre-close drain
                    # persisted for this message BEFORE re-queuing it, so the
                    # re-drain does not leave two legacy user rows. This is the
                    # SAME silent, legacy-table-only cleanup the follow-up orphan
                    # path uses (delete_orphan_user_turn) — it never touches the
                    # shared SDK transcript and never emits user_message_discarded
                    # (we are re-queuing, not bouncing the text back to the input
                    # box). NOTE: under postgres_only the chat reads the SDK
                    # transcript (session_store_entries), where this turn's eagerly
                    # mirrored user line is left in place — the SAME pre-existing
                    # #420 follow-up limitation as any pre-output teardown on a
                    # follow-up turn (a lone orphan user line that cannot be cleanly
                    # reverted per-turn without wiping shared history).
                    try:
                        await run_sync(database.delete_orphan_user_turn, session_id)
                    except Exception as hygiene_err:  # noqa: BLE001
                        ctx.warning(
                            logger,
                            f"queue-drain re-queue legacy-row cleanup failed: {hygiene_err}",
                        )
                    try:
                        await run_sync(
                            database.requeue_message_at_front,
                            session_id=session_id,
                            content=q_src_content,
                            profile_id=q_src_profile if q_src_profile != "claude-code" else None,
                            project_id=q_src_project,
                        )
                        queue = await run_sync(database.get_queued_messages, session_id)
                        await sync_engine.broadcast_queue_updated(session_id, queue)
                    except Exception as requeue_err:
                        # Never let a re-queue failure crash the finally; log
                        # durably so the drop is at least diagnosable.
                        logger.error(
                            "[Queue] queue-strand-on-stop: FAILED to re-queue a "
                            "drained message after pre-output teardown "
                            "(session=%s): %s",
                            session_id, requeue_err,
                        )
                    logger.warning(
                        "[Queue] queue-strand-on-stop: re-queued a drained message "
                        "at front after pre-output teardown (session=%s) — NOT "
                        "discarded as an orphan draft",
                        session_id,
                    )
                    ctx.info(
                        logger,
                        "Torn down before output on a queue-drain successor — "
                        "re-queued the committed message instead of discarding it",
                    )

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

                        if drain_source_requeued:
                            # This "empty" turn is a server-initiated queue-drain
                            # successor whose already-committed message was ALREADY
                            # re-queued at the FRONT by the generalized pre-output
                            # re-queue above (which fires for ANY pre-output teardown
                            # latch — see there). Do NOT run the #420 orphan-discard
                            # path below: a committed queued message must never be
                            # discarded / bounced back into the input box.
                            pass
                        # Durable cleanup. `sdk_sid` is the session's sdk_session_id
                        # captured at the START of this turn (before streaming),
                        # so it tells first turn from follow-up.
                        elif sdk_sid is None:
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

                    # Non-lossy Stop: a Stop that leaves a non-empty queue keeps the
                    # rows intact (by design — handle_stop never auto-fires them) but
                    # must NOT be silent. Emit a durable, greppable record of the
                    # strand (including any message just re-queued by the fix above)
                    # so a "my queued messages vanished" incident is diagnosable.
                    try:
                        _q_stranded = await run_sync(
                            database.count_queued_messages, session_id
                        )
                    except Exception as count_err:  # noqa: BLE001
                        _q_stranded = None
                        ctx.debug(logger, f"queue-strand count failed: {count_err}")
                    if _q_stranded:
                        logger.warning(
                            "[Queue] queue-strand-on-stop: %d message(s) left queued "
                            "after Stop for session=%s — kept in the queue, not "
                            "auto-fired (send manually or reload to resume)",
                            _q_stranded, session_id,
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
