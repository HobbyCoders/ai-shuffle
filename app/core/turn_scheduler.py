"""app/core/turn_scheduler.py — the settlement policies (v2 core, Phase 2 A5).

The scheduler is the component that decides *what happens next* after a turn
settles. It owns no state machine of its own — every decision is a **durable
derivation** over the ``turns`` table and the ``session_events`` log ("the log
is the record"), replacing the legacy in-memory latches (the #557
auto-continue latch, the per-turn ``failover_attempted`` set, the
``unpark_pending`` latch). Four policies, per the Phase 2 plan §1.6, evaluated
in this order on every settlement:

1. **FAILOVER** — a settled-``completed`` turn whose terminal ``turn.completed``
   event carries a usage-limit ``api_error_status`` (429) enqueues a
   ``source=failover`` retry of the same prompt with ``parent_turn_id`` set and
   the accumulated attempted-account ids in ``payload.failover_attempted``,
   then kicks. The scheduler appends NO ``message.user`` and persists NO
   session message — user-visible message rows exist only for
   ``source in (user, queue)`` turns (enforced where messages are recorded),
   so the seed dedup stays deterministic.
2. **QUEUE DRAIN** — a settled-``completed`` turn with queued rows behind it
   kicks the front of the queue. A user stop settles ``aborted``, so queued
   rows REMAIN untouched (F4).
3. **AUTO-CONTINUE (#557)** — a settled-``completed`` turn cut at the CLI's
   lazy-compaction boundary (``stop_reason == "tool_use"`` + live context over
   the autocompact threshold) enqueues ONE ``source=auto_continue``
   continuation. The one-shot latch is a durable derivation: skip if the
   settled turn itself — or the most recent settled non-user turn since the
   last ``source in (user, queue)`` turn — was ``auto_continue``. Reset-on-user-
   message is automatic (the next user/queue turn settles closer in history).
4. **PARK/UNPARK** — a parked settlement is left alone; the wake arrives via
   the idle reader's terminal task notification (:func:`on_task_terminal`),
   which synthesizes ONE consolidated ``<task-notification>`` prompt, enqueues
   it as ``source=unpark`` (context copied from the parked turn), and kicks.

The FAILOVER and AUTO-CONTINUE derivations share ONE read of the settled
turn's terminal ``turn.completed`` payload from ``session_events``
(``api_error_status`` for failover, ``stop_reason`` for the boundary cut) —
done once per settlement and fed to both policies.

**Kick** — the single "try to start this session's next queued turn" primitive
(registered as :mod:`app.core.turn_manager`'s kick seam): peek the front
queued row (``pop_ready`` — a non-mutating peek) and hand it to
:func:`app.core.session_runtime.execute`, which claims ownership at the DB
(run lease + guarded ``start_turn``). Duplicate kicks are harmless — start
races resolve at the database, and a losing kick leaves the row queued. The
legacy ``startup.is_shutting_down`` guard moves here: a shutting-down process
refuses to start new work; queued rows stay durable for the next boot's sweep.

Nothing calls :func:`install` yet — the transport commit (A6) wires it at
boot; tests call it directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set

from app.db import turns as turns_db
from app.db.base import get_db

logger = logging.getLogger(__name__)

# Payload key carrying the accumulated set of account ids a prompt has already
# 429'd on across its failover retry chain (plan §1.6(1): "attempts in
# payload" — durable, replacing the legacy per-turn in-memory set).
FAILOVER_ATTEMPTED_KEY = "failover_attempted"

# Transport-context payload keys copied from a settled/parked turn onto a
# scheduler-enqueued successor so the retry/continuation/wake runs with the
# same attribution the original turn had.
_CONTEXT_PAYLOAD_KEYS = ("api_user_id", "user_id")

# Turn sources authored by a human (directly or via the send-queue). Used by
# the #557 durable latch derivation: any of these settling re-arms
# auto-continue; everything else (auto_continue/failover/unpark/automation/…)
# is server-originated.
_USER_SOURCES = ("user", "queue")

# Live references to in-flight kick tasks so they are not garbage-collected
# mid-flight (asyncio keeps only weak refs to tasks).
_kick_tasks: Set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Durable reads (the turns table is the machine; the log is the record)
# ---------------------------------------------------------------------------

def _parse_json(raw: Any) -> Any:
    """Decode a JSONB column the test shim stores as TEXT (dicts pass through)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _turn_terminal_payload(session_id: str, turn_id: str) -> Dict[str, Any]:
    """The settled turn's terminal ``turn.completed`` payload from the event
    log (``{}`` when the turn has no terminal event — e.g. recorder never ran).

    This single read feeds BOTH the failover derivation (``api_error_status``,
    per the §1.6(1) amendment: the recorder copies it from the pump's ``done``
    metadata) and the auto-continue boundary derivation (``stop_reason``).
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload FROM session_events "
            "WHERE session_id=%s AND turn_id=%s AND event_type='turn.completed' "
            "ORDER BY seq DESC LIMIT 1",
            (session_id, turn_id),
        )
        row = cursor.fetchone()
    if row is None:
        return {}
    payload = _parse_json(row["payload"])
    return payload if isinstance(payload, dict) else {}


def _auto_continued_since_last_user(session_id: str) -> bool:
    """#557 one-shot latch, derived durably from the ``turns`` table.

    Walk the session's settled turns newest-first: an ``auto_continue`` turn
    seen before any ``user``/``queue`` turn means this session has already been
    nudged since the last genuine user message — never chain nudges. A
    user/queue turn re-arms (returns ``False``) automatically; no in-memory
    latch, no reset call.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT source FROM turns "
            "WHERE session_id=%s AND state IN ('completed','aborted','parked') "
            "ORDER BY ended_at DESC, position DESC, created_at DESC, id DESC",
            (session_id,),
        )
        rows = cursor.fetchall()
    for row in rows:
        source = row["source"]
        if source in _USER_SOURCES:
            return False
        if source == "auto_continue":
            return True
    return False


def _latest_context_turn(session_id: str) -> Optional[Dict[str, Any]]:
    """The turn whose context an unpark wake inherits: the most recent
    ``parked`` turn (the one whose background work just came to rest), falling
    back to the most recent settled turn. ``None`` when the session has no
    terminal turns at all."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM turns WHERE session_id=%s AND state='parked' "
            "ORDER BY ended_at DESC, position DESC, created_at DESC, id DESC "
            "LIMIT 1",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.execute(
                "SELECT * FROM turns "
                "WHERE session_id=%s AND state IN ('completed','aborted') "
                "ORDER BY ended_at DESC, position DESC, created_at DESC, id DESC "
                "LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
    return turns_db._row(row)


def _context_payload(turn: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Copy the transport-context keys from a turn's payload (attribution for a
    scheduler-enqueued successor)."""
    src = (turn or {}).get("payload") or {}
    return {k: src[k] for k in _CONTEXT_PAYLOAD_KEYS if src.get(k) is not None}


# ---------------------------------------------------------------------------
# Kick — start the front queued turn (turn_manager's kick seam)
# ---------------------------------------------------------------------------

def kick(session_id: str) -> None:
    """Nudge a session: try to start its front queued turn.

    Non-blocking / fire-and-forget (turn_manager calls it synchronously right
    after enqueue). The legacy shutdown guard lives here (plan §1.6(4)): while
    the process is tearing down, no new work starts — the queued rows are
    durable and the next boot's sweep/kick picks them up.
    """
    from app.core import startup
    if getattr(startup, "is_shutting_down", False):
        logger.info(
            "turn_scheduler: shutting down — kick suppressed for %s "
            "(queued turns stay durable)", session_id,
        )
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Sync caller with no loop: best-effort no-op, same contract as an
        # unregistered kick — the enqueued turn stays queued for a later kick.
        logger.debug(
            "turn_scheduler: no running loop — kick dropped for %s", session_id,
        )
        return
    task = loop.create_task(_kick_impl(session_id))
    _kick_tasks.add(task)
    task.add_done_callback(_kick_done)


def _kick_done(task: asyncio.Task) -> None:
    _kick_tasks.discard(task)
    if not task.cancelled():
        task.exception()  # retrieve, so a failed kick never logs as un-awaited


async def _kick_impl(session_id: str) -> None:
    """Peek the front queued turn and execute it. Ownership is DB-raced inside
    ``execute`` (run lease + guarded ``start_turn``) — a lost race returns
    ``None`` and the row stays queued for whoever owns the session."""
    try:
        row = await asyncio.to_thread(turns_db.pop_ready, session_id)
        if row is None:
            return
        from app.core import session_runtime
        await session_runtime.execute(row)
    except Exception:  # noqa: BLE001 — a failed kick must never lose the turn.
        logger.warning(
            "turn_scheduler: kick failed for %s", session_id, exc_info=True,
        )


# ---------------------------------------------------------------------------
# on_turn_settled — policies (1) failover, (2) queue drain, (3) auto-continue
# ---------------------------------------------------------------------------

async def on_turn_settled(session_id: str, turn_id: str, outcome: str) -> None:
    """Settlement hook (session_runtime's scheduler seam): fired exactly once
    per real DB settlement with ``outcome in (completed|aborted|parked)``."""
    try:
        await _on_turn_settled_impl(session_id, turn_id, outcome)
    except Exception:  # noqa: BLE001 — settlement must never break teardown.
        logger.warning(
            "turn_scheduler: on_turn_settled failed for %s turn %s",
            session_id, turn_id, exc_info=True,
        )


async def _on_turn_settled_impl(session_id: str, turn_id: str, outcome: str) -> None:
    if outcome != "completed":
        # aborted — a user stop (or crash/drain) settles aborted: queued rows
        #   REMAIN untouched (F4) and nothing auto-starts; the user's next
        #   action (or the reap sweep) resumes the queue.
        # parked — background work still running; the successor turn arrives
        #   via on_task_terminal when that work comes to rest.
        return

    settled = await asyncio.to_thread(turns_db.get_turn, turn_id)
    if settled is None:
        return
    # ONE terminal-payload read feeds both the failover (api_error_status) and
    # auto-continue (stop_reason) derivations.
    terminal = await asyncio.to_thread(_turn_terminal_payload, session_id, turn_id)

    # (1) FAILOVER
    if await _maybe_failover(session_id, settled, terminal):
        return

    # (2) QUEUE DRAIN — completed + not stopped → pop_ready → start.
    front = await asyncio.to_thread(turns_db.pop_ready, session_id)
    if front is not None:
        kick(session_id)
        return

    # (3) AUTO-CONTINUE (#557) — only when nothing is queued (a queued turn
    # resumes work on its own, same gate as the legacy path).
    await _maybe_auto_continue(session_id, settled, terminal)


async def _maybe_failover(
    session_id: str,
    settled: Dict[str, Any],
    terminal: Dict[str, Any],
) -> bool:
    """Policy (1): usage-limit fail + not user-stopped → enqueue a
    ``source=failover`` retry. Returns True when a retry was enqueued.

    "Not user-stopped" holds by construction: a user stop settles ``aborted``
    and this runs only for ``completed`` settlements. The attempted-account
    set rides ``payload.failover_attempted`` across the retry chain, so
    rotation terminates once every account has 429'd (``retry=False`` → fall
    through to the remaining policies). No message append here — retries are
    invisible to the message record (source-typed persistence).
    """
    from app.core import account_failover

    if not account_failover.is_usage_limit_error(terminal):
        return False

    payload = dict(settled.get("payload") or {})
    # attempt_failover MUTATES this set (adds the exhausted current account and
    # the chosen target) — persist the union on the retry turn.
    attempted: Set[str] = set(payload.get(FAILOVER_ATTEMPTED_KEY) or [])
    decision = await account_failover.attempt_failover(
        session_id, payload.get("api_user_id"), attempted,
    )
    if not decision.retry:
        logger.info(
            "turn_scheduler: failover exhausted for %s turn %s "
            "(attempted=%s) — no retry",
            session_id, settled.get("id"), sorted(attempted),
        )
        return False

    payload[FAILOVER_ATTEMPTED_KEY] = sorted(attempted)
    retry = await asyncio.to_thread(
        _enqueue,
        session_id,
        "failover",
        prompt=settled.get("prompt"),
        profile_id=settled.get("profile_id"),
        project_id=settled.get("project_id"),
        worktree_id=settled.get("worktree_id"),
        overrides=settled.get("overrides") or None,
        payload=payload,
        parent_turn_id=settled.get("id"),
        # The retry continues the settled turn — it must run BEFORE any turn a
        # user queued during the failed turn, not behind it (front of queue).
        at_front=True,
    )
    logger.info(
        "turn_scheduler: usage-limit failover for %s — retry turn %s on "
        "account %s (switched=%s)",
        session_id, retry["id"], decision.account_id, decision.switched,
    )
    kick(session_id)
    return True


async def _maybe_auto_continue(
    session_id: str,
    settled: Dict[str, Any],
    terminal: Dict[str, Any],
) -> bool:
    """Policy (3): compact-boundary cut → enqueue ONE ``source=auto_continue``
    continuation. Returns True when one was enqueued."""
    from app.core import compact_continue

    if settled.get("source") == "auto_continue":
        # The continuation itself cut at the boundary again: compaction already
        # ran at its start, so a second cut is something else — never chain
        # nudges (#557), just log it.
        if compact_continue.is_abnormal_boundary_stop(terminal):
            logger.warning(
                "turn_scheduler: auto-continue turn %s for %s cut at the "
                "boundary AGAIN — not re-nudging",
                settled.get("id"), session_id,
            )
        return False
    if not compact_continue.auto_continue_enabled():
        return False
    if not compact_continue.is_abnormal_boundary_stop(terminal):
        return False
    if await asyncio.to_thread(_auto_continued_since_last_user, session_id):
        return False
    over, pre_tokens = await compact_continue.context_over_threshold(session_id)
    if not over:
        return False

    turn = await asyncio.to_thread(
        _enqueue,
        session_id,
        "auto_continue",
        prompt=compact_continue.AUTO_CONTINUE_PROMPT,
        profile_id=settled.get("profile_id"),
        project_id=settled.get("project_id"),
        worktree_id=settled.get("worktree_id"),
        payload=_context_payload(settled) or None,
        parent_turn_id=settled.get("id"),
    )
    logger.info(
        "turn_scheduler: compact-boundary auto-continue for %s — turn %s "
        "(pre_tokens=%s)",
        session_id, turn["id"], pre_tokens,
    )
    kick(session_id)
    return True


def _enqueue(session_id: str, source: str, **kwargs: Any) -> Dict[str, Any]:
    """Thin sync wrapper so policy enqueues run off the event loop uniformly."""
    return turns_db.enqueue_turn(session_id, source, **kwargs)


# ---------------------------------------------------------------------------
# on_task_terminal — policy (4) unpark
# ---------------------------------------------------------------------------

def _queued_unpark_turn(session_id: str) -> Optional[Dict[str, Any]]:
    """The session's front-most still-``queued`` ``source=unpark`` wake, if any
    (there is at most one by construction — near-simultaneous completions batch
    into it)."""
    for turn in turns_db.get_queued_turns(session_id):
        if turn.get("source") == "unpark":
            return turn
    return None


async def on_task_terminal(session_id: str, frame: Dict[str, Any]) -> None:
    """Idle-reader hook: a background task reached a terminal state between
    turns. Fold the terminal task into ONE consolidated ``source=unpark`` wake.

    Near-simultaneous completions must batch into a single turn (the builder's
    consolidation only fires when it sees all pending frames at once), and a
    duplicate frame for a task already folded in must not spawn a second turn:

    * (a) a queued unpark whose ``task_ids`` already include this frame's task →
      complete no-op (idempotent);
    * (b) a queued unpark WITHOUT this task → merge: append the frame to
      ``payload.pending_frames``, rebuild the FULL prompt over every pending
      frame, and rewrite the row via a guarded UPDATE. If that UPDATE loses
      (the wake already started), fall through to (c);
    * (c) otherwise enqueue a new unpark wake carrying ``pending_frames`` +
      ``task_ids`` (context inherited from the parked turn).

    Kicks after a merge/enqueue (never after the (a) no-op)."""
    try:
        task_id = frame.get("task_id")
        # OWNERSHIP GATE (subagent task-notification leak): the reader hands off
        # EVERY terminal background frame — including tasks launched INSIDE a
        # sub-agent (grandchildren), which the main session never launched.
        # Policy: the parent's OWN background work always wakes it; and ANY
        # still-running background agent admits the wake, because the
        # main-session pump is the ONLY resume path for a parked sub-agent
        # (2026-07-10 strand repair) and a grandchild task is by construction
        # launched WHILE its sub-agent is running — so no ownership-lane arming
        # is required for the strand-repair case. Suppression happens only when
        # the session is fully at rest with NO owner: an orphan grandchild or a
        # synchronous sub-agent coming to rest (the "nothing to act on" leak).
        from app.core.task_registry import task_registry
        should_wake = (
            task_registry.is_own_background_task(session_id, task_id)
            or task_registry.has_active_background_agent(session_id)
        )
        if not should_wake:
            logger.info(
                "turn_scheduler: unpark-decision suppressed for %s — task %s "
                "not the parent's background work and no active background agent",
                session_id, task_id,
            )
            return

        existing = await asyncio.to_thread(_queued_unpark_turn, session_id)
        if existing is not None:
            existing_payload = dict(existing.get("payload") or {})
            existing_task_ids = list(existing_payload.get("task_ids") or [])
            if task_id and task_id in existing_task_ids:
                # (a) idempotent: this task is already folded into the wake.
                logger.info(
                    "turn_scheduler: unpark for %s — task %s already in wake "
                    "turn %s, no-op", session_id, task_id, existing.get("id"),
                )
                return
            # (b) merge this frame into the pending wake and rebuild its prompt.
            pending = list(existing_payload.get("pending_frames") or [])
            pending.append(frame)
            merged_payload = dict(existing_payload)
            merged_payload["pending_frames"] = pending
            merged_payload["task_ids"] = existing_task_ids + (
                [task_id] if task_id else []
            )
            prompt = await asyncio.to_thread(
                _build_task_notification_prompt, pending
            )
            updated = await asyncio.to_thread(
                turns_db.update_queued_turn_prompt,
                existing["id"], prompt, merged_payload,
            )
            if updated is not None:
                logger.info(
                    "turn_scheduler: unpark for %s — task %s terminal (%s) "
                    "merged into wake turn %s (%d pending)",
                    session_id, task_id, frame.get("status"),
                    updated["id"], len(pending),
                )
                kick(session_id)
                return
            # The guarded UPDATE lost (the wake left the queue) — enqueue fresh.

        # (c) enqueue a new consolidated unpark wake.
        prompt = await asyncio.to_thread(_build_task_notification_prompt, [frame])
        context = await asyncio.to_thread(_latest_context_turn, session_id) or {}
        payload = _context_payload(context)
        payload["pending_frames"] = [frame]
        payload["task_ids"] = [task_id] if task_id else []
        turn = await asyncio.to_thread(
            _enqueue,
            session_id,
            "unpark",
            prompt=prompt,
            profile_id=context.get("profile_id"),
            project_id=context.get("project_id"),
            worktree_id=context.get("worktree_id"),
            payload=payload,
            parent_turn_id=context.get("id"),
        )
        logger.info(
            "turn_scheduler: unpark for %s — task %s terminal (%s), wake turn %s",
            session_id, task_id, frame.get("status"), turn["id"],
        )
        kick(session_id)
    except Exception:  # noqa: BLE001 — a wake failure must never kill the reader.
        logger.warning(
            "turn_scheduler: on_task_terminal failed for %s", session_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def install() -> None:
    """Wire the scheduler into the three seams. Called once at boot (A6)."""
    from app.core import idle_reader, session_runtime, turn_manager
    session_runtime.register_scheduler(on_turn_settled)
    idle_reader.register_on_task_terminal(on_task_terminal)
    turn_manager.register_kick(kick)
    logger.info("turn_scheduler installed (settlement policies active)")


# ---------------------------------------------------------------------------
# <task-notification> prompt synthesis — RELOCATED from app/api/chat_ws_handler
# (plan §4: the builder survives V2; the legacy copy goes with the PR-B
# deletion of the old unpark path).
# ---------------------------------------------------------------------------

# Guidance attached to every consolidated <task-notification> follow-up turn.
# The app fires AT MOST ONE unpark per background task and batches
# near-simultaneous completions into a single turn, so this is an
# informational, one-time consolidation — NOT a prompt for new work. The
# wording steers the model away from manufacturing a verbose "nothing to act
# on" rationalization.
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
