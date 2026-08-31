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
2. **QUEUE DRAIN** — a settled turn with queued rows behind it kicks the front
   of the queue. This fires on EVERY settle outcome (``completed``, ``parked``,
   ``aborted``, error) — a queued user message starts the next turn the moment
   the current one comes to rest, matching Claude Code's own semantics. On a
   ``parked`` settlement the queued turn runs CONCURRENTLY with the session's
   still-running background work (park releases the run lease and the live-turn
   slot). A user stop settles ``aborted`` and now drains too — this supersedes
   the old preserve-on-stop rule (F4, retired).
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
   When the parent is MID-TURN rather than at rest, that same prompt is instead
   STEERED into the running turn (:func:`_try_steer_terminal_notification`), so
   it lands at the next cell boundary rather than waiting out a turn that may
   run for another hour. See "Mid-turn delivery" below.

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
from uuid import uuid4

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

# Content-empty guard (#690): the fields that carry a terminal task frame's
# renderable content. A frame with NONE of them populated has nothing to render
# in a ``<task-notification>`` and must never be enqueued as a parent-visible
# wake (the reference harness never emits empty notifications). ``task_id`` /
# ``tool_use_id`` are identity — a bare TaskUpdated frame carrying only those
# still identifies WHICH background unit came to rest (and is later upgraded by
# its rich frame, #688 A-2), so it counts as renderable; a frame with not even
# an id is dropped at the single ``on_task_terminal`` choke point.
_FRAME_CONTENT_FIELDS = ("task_id", "tool_use_id", "summary", "output_file")


def _frame_has_renderable_content(frame: Dict[str, Any]) -> bool:
    """True iff ``frame`` carries any renderable content (identity or result).

    False only for a wholly-empty frame (no ``task_id``/``tool_use_id``/
    ``summary``/``output_file``) — the caller drops those with a DEBUG log
    instead of enqueueing a contentless notification turn."""
    return any(frame.get(f) for f in _FRAME_CONTENT_FIELDS)


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
    """The turn whose context an unpark wake inherits, in preference order:

    1. the most recent ``parked`` turn — the one whose background work just
       came to rest;
    2. the session's live/settling turn (``running``/``draining``/
       ``interrupting``) — a background task can go terminal while its parent
       turn is still settling, and a wake enqueued inside that window must
       inherit that still-settling turn's context rather than none at all;
    3. the most recent settled turn.

    ``None`` when the session has no usable turns at all.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        row = None
        for state_clause in (
            "state='parked'",
            "state IN ('running','draining','interrupting')",
            "state IN ('completed','aborted')",
        ):
            cursor.execute(
                "SELECT * FROM turns WHERE session_id=%s AND " + state_clause + " "
                "ORDER BY ended_at DESC, position DESC, created_at DESC, id DESC "
                "LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                break
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
    # QUEUE DRAIN runs on EVERY settle outcome (completed, parked, aborted,
    # error). A queued user message must start the next turn the moment the
    # current one comes to rest — matching Claude Code's own semantics, including
    # when the agent PARKS with background work still running: park releases both
    # the run lease and the live-turn slot, so the queued turn runs CONCURRENTLY
    # with that background work (a message queued AFTER park already starts
    # immediately; this makes queued-BEFORE-park consistent). A user STOP settles
    # aborted and now drains too — this deliberately supersedes the old
    # preserve-on-stop rule (F4). Each drain consumes one queued row, so an error
    # settlement carries no retry-loop risk. The park→unpark machinery
    # (on_task_terminal) is untouched: a queued message naturally orders ahead of
    # a later source=unpark wake by queue position, no special ordering needed.
    settled = await asyncio.to_thread(turns_db.get_turn, turn_id)
    if settled is None:
        return
    # ONE terminal-payload read feeds both the failover (api_error_status) and
    # auto-continue (stop_reason) derivations. Both are naturally inert on a
    # non-completed settlement — a parked/aborted turn has no terminal
    # ``turn.completed`` payload to trip them.
    terminal = await asyncio.to_thread(_turn_terminal_payload, session_id, turn_id)

    # (1) FAILOVER — stays FIRST: if it enqueued a retry the drain must NOT also
    # fire (structural precedence guard; with OC-1 retired failover never fires
    # on a non-completed outcome, but the order is preserved).
    if await _maybe_failover(session_id, settled, terminal):
        return

    # (2) QUEUE DRAIN — pop_ready (non-mutating peek) → kick starts the front
    # queued row. Fires on ALL outcomes, ahead of auto-continue.
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

    "Not user-stopped" holds by construction: the usage-limit signal lives in a
    settled turn's terminal ``turn.completed`` payload, which a user stop
    (``aborted``) / park never carries — so ``is_usage_limit_error`` is False on
    every non-completed settlement and this returns early. The attempted-account
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
    into it). PUMP wakes (``source=unpark_pump``) are deliberately excluded — the
    own lane never merges into a pump turn and vice-versa."""
    for turn in turns_db.get_queued_turns(session_id):
        if turn.get("source") == "unpark":
            return turn
    return None


def discard_queued_task_wakes(session_id: str, task_ids: List[str]) -> int:
    """Remove canceled tasks from queued ``source=unpark`` turns.

    Stop marks each task wake-suppressed before calling this function, closing the
    late-frame race. This handles the other side of that race: a terminal frame
    may already have queued a wake immediately before suppression. An all-canceled
    wake is aborted; a mixed wake is rebuilt with only unrelated results.

    Returns the number of queued turns changed. Guarded DB operations make this
    safe when a wake concurrently leaves the queue.
    """
    canceled = {task_id for task_id in task_ids if task_id}
    if not session_id or not canceled:
        return 0

    changed = 0
    for turn in turns_db.get_queued_turns(session_id):
        if turn.get("source") != "unpark":
            continue
        payload = dict(turn.get("payload") or {})
        pending = list(payload.get("pending_frames") or [])
        recorded_ids = list(payload.get("task_ids") or [])
        if not (
            canceled.intersection(recorded_ids)
            or any(frame.get("task_id") in canceled for frame in pending)
        ):
            continue

        remaining = [
            frame for frame in pending if frame.get("task_id") not in canceled
        ]
        if not remaining:
            if turns_db.remove_queued_turn(turn["id"]) is not None:
                changed += 1
            continue

        remaining_ids = [
            task_id for task_id in recorded_ids if task_id not in canceled
        ]
        payload["pending_frames"] = remaining
        payload["task_ids"] = remaining_ids
        prompt = _build_task_notification_prompt(remaining)
        if turns_db.update_queued_turn_prompt(turn["id"], prompt, payload) is not None:
            changed += 1
    return changed


def _session_has_parked_turn(session_id: str) -> bool:
    """True iff the session currently has a ``parked`` turn.

    A background sub-agent/shell only outlives its turn by PARKING it, so a
    parked turn is the observable "the parent is at rest waiting on background
    work" signal the CLI keys its own at-rest wake on (see
    :func:`_cli_owns_at_rest_wake`)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM turns WHERE session_id=%s AND state='parked' LIMIT 1",
            (session_id,),
        )
        return cursor.fetchone() is not None


#: ``frame["origin"]`` stamped by :mod:`app.core.provider_agent` on the terminal
#: frame it hands to :func:`on_task_terminal`. Additive — no other frame producer
#: sets ``origin``, so every pre-existing frame behaves exactly as before.
#:
#: It exists for ONE decision: a ProviderAgent worker is registered
#: ``kind == "agent"`` (load-bearing — it is what pins the parent in the session
#: pool), so a COMPLETED worker resting under a parked parent matches every
#: condition of the A-1 CLI-owned-wake suppression below. But that suppression is
#: predicated on the CLI delivering its own rich ``<task-notification>`` follow-up
#: turn, and there is NO CLI task here at all: the worker is a separate session
#: dispatched from an MCP tool handler. Suppressing the app's wake would strand
#: the parent parked forever. The marker keeps the suppression's scope exactly as
#: written for genuine CLI-native sub-agents.
PROVIDER_AGENT_ORIGIN = "provider_agent"


# A-1 (bug 1): the CLI-native duplicate. When a TOP-LEVEL backgrounded Task/Agent
# comes to rest while the parent turn is PARKED, the CLI itself starts a follow-up
# turn injecting the rich ``[SYSTEM NOTIFICATION] … <task-notification>`` string
# (query_engine ``_cli_turn_events_from_message``). The app's own consolidated
# ``source=unpark`` wake, built from the content-poor bare TaskUpdated frame, is a
# SECOND (empty) delivery of the same completion. When the CLI owns the at-rest
# wake we suppress the app's enqueue entirely so the model sees ONE (rich) wake.
#
# Scope (conservative — a wrong suppression strands a real completion, R1):
#   * status == "completed" ONLY. failed/killed/stopped keep the app wake — the
#     CLI has no at-rest resume for a kill (chat_ws_handler), so its notification
#     is the ONLY delivery.
#   * registry record ``kind == "agent"`` ONLY. Backgrounded SHELLS (kind
#     "shell", incl. SDK-backgrounded local_bash) keep the app wake — the CLI does
#     not own a shell's at-rest resume the way it owns a sub-agent's.
#   * parent PARKED ONLY. The CLI's rich follow-up turn exists only in the parked
#     state; with no parked turn the app wake is the sole delivery and must fire
#     (keeps the non-parked own-lane regression guards green).
# NOTE: a top-level backgrounded agent is ALSO tagged ``is_sdk_background_task``
# (query_engine marks agents whose launch was a background launch), so that flag
# does NOT distinguish agent-from-shell — the ``kind == "agent"`` record field is
# the reliable discriminator.
# ALSO EXCLUDED: a ProviderAgent worker frame (``origin`` marker) — it satisfies
# every condition above but has no CLI task behind it at all. See
# PROVIDER_AGENT_ORIGIN.
def _cli_owns_at_rest_wake(session_id: str, frame: Dict[str, Any]) -> bool:
    """True iff the CLI owns this completion's at-rest wake (so the app must NOT
    also enqueue one). Caller is already inside the OWN (top-level) branch."""
    if frame.get("origin") == PROVIDER_AGENT_ORIGIN:
        # A ProviderAgent worker has no CLI task behind it — see
        # PROVIDER_AGENT_ORIGIN. The app's wake is the ONLY delivery.
        return False
    if frame.get("status") != "completed":
        return False
    from app.core.task_registry import task_registry

    record = task_registry.get_task(session_id, frame.get("task_id"))
    if not record or record.get("kind") != "agent":
        return False
    return _session_has_parked_turn(session_id)


# A-2: a frame is "richer" than a stored one when it POPULATES any content field
# the stored frame left falsy. Purely additive — it never demotes a populated
# field to empty. Used to upgrade a bare TaskUpdated-built wake (task_id/status/
# tool_use_id only) in place when the rich TaskNotification frame arrives after.
_RICHNESS_FIELDS = ("output_file", "summary", "usage")


def _frame_is_richer(incoming: Dict[str, Any], stored: Dict[str, Any]) -> bool:
    """True iff ``incoming`` fills any of ``{output_file, summary, usage}`` that
    ``stored`` leaves falsy."""
    return any(
        incoming.get(f) and not stored.get(f) for f in _RICHNESS_FIELDS
    )


def _dedupe_frames_by_task_id(
    frames: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Collapse ``frames`` so ONE task_id contributes at most ONE frame.

    A task can produce several terminal frames in a single wake batch — a bare
    ``TaskUpdated`` and the rich ``TaskNotification`` that follows it, or the same
    frame arriving down two seams — and every one of them used to become its own
    ``<task-notification>`` block carrying the same result. The richer frame wins
    (:func:`_frame_is_richer`) and keeps the FIRST occurrence's position, so batch
    order is preserved. Frames with no task_id cannot be identified and are all
    kept, exactly as before.
    """
    out: List[Dict[str, Any]] = []
    index: Dict[str, int] = {}
    for frame in frames:
        task_id = frame.get("task_id") if isinstance(frame, dict) else None
        if not task_id:
            out.append(frame)
            continue
        seen = index.get(task_id)
        if seen is None:
            index[task_id] = len(out)
            out.append(frame)
        elif _frame_is_richer(frame, out[seen]):
            out[seen] = frame
    return out


async def _enqueue_context_wake(
    session_id: str,
    source: str,
    frames: List[Dict[str, Any]],
    task_ids: List[str],
    prompt: str,
) -> Dict[str, Any]:
    """Enqueue a fresh context-carrying wake turn for the OWN lane's fall-through
    enqueue. Reads the session's latest context turn for
    profile/project/worktree/parent linkage, attaches the ``frames`` and their
    ``task_ids`` to the payload, and returns the enqueued turn."""
    context = await asyncio.to_thread(_latest_context_turn, session_id) or {}
    payload = _context_payload(context)
    # One task_id, one frame — the wake turn's payload is what a later merge
    # rebuilds the prompt from, so a duplicate stored here outlives this call.
    payload["pending_frames"] = _dedupe_frames_by_task_id(list(frames))
    payload["task_ids"] = list(dict.fromkeys(task_ids))
    profile_id = context.get("profile_id")
    if not profile_id:
        # Belt and braces: a profile-less wake is refused downstream ("Profile
        # not found: None") and the task notification is lost for good — the
        # mark_woken latch never lets it be enqueued again. Whenever the
        # context turn cannot supply a profile, fall back to the session's own.
        from app.db import sessions as sessions_db
        session_row = await asyncio.to_thread(
            sessions_db.get_session, session_id,
        ) or {}
        profile_id = session_row.get("profile_id")
        logger.warning(
            "turn_scheduler: %s wake for session %s inherited no profile from "
            "its context turn — falling back to the session profile %s",
            source, session_id, profile_id,
        )
    return await asyncio.to_thread(
        _enqueue,
        session_id,
        source,
        prompt=prompt,
        profile_id=profile_id,
        project_id=context.get("project_id"),
        worktree_id=context.get("worktree_id"),
        payload=payload,
        parent_turn_id=context.get("id"),
    )


# ---------------------------------------------------------------------------
# Mid-turn delivery — steer the notification into the RUNNING turn
# ---------------------------------------------------------------------------
#
# The bug this closes: a wake enqueued while the parent is MID-TURN cannot run
# until that turn settles (the session has one live-turn slot), so a sub-agent
# that finished in two minutes was delivered fifty-two minutes later, at the
# next turn boundary — while the parent, seeing ``status=completed`` on the
# task list and no report, redid the child's work by hand.
#
# The delivery channel already exists: a user's own mid-turn message is handed
# to the live engine as a fluid ``Steer`` and folded into the next tool result
# (``session_runtime.try_steer_pending``). A terminal ``<task-notification>``
# is the same shape of interjection, so it takes the same road.
#
# Ownership of the delivery is settled by the RECEIPT, never by the submit:
# fluid drops an undelivered steer at turn end, so treating submission as
# delivery would strand a real completion (R1). Hence:
#
#   * submit succeeds  → return here; ``mark_woken`` (latched by the caller just
#     above) keeps the later park/idle wake from re-delivering the same task;
#   * receipt arrives  → ``on_task_notification_delivered`` latches
#     ``terminal_notified``;
#   * turn ends with the steer unreceipted → ``session_runtime._finalize`` calls
#     ``on_task_notification_stranded``, which enqueues the ordinary at-rest
#     wake after all;
#   * submit fails (no live runtime, no adapter, a dying turn) → the caller
#     falls straight through to the ordinary enqueue, exactly as before.
#
#: Steer-id prefix for a mid-turn ``<task-notification>``. A steer id is
#: normally a QUEUED TURN's row id, which the receipt handler loads to record
#: and retire; a notification has no queued row behind it, so the prefix keeps
#: the two namespaces apart (``session_runtime`` routes a prefixed receipt back
#: here instead of logging it as a receipt for an unknown turn).
TASK_STEER_ID_PREFIX = "task-notify:"


def _mark_terminal_notified(session_id: str, task_ids: List[str]) -> None:
    """Latch ``terminal_notified`` for every task whose notification just landed.

    ``background_tasks.terminal_notified`` used to stay False forever: the only
    caller of ``mark_terminal_notified`` is ``complete_task``, and only on the
    FAILURE statuses that fire the chat-layer chip — a clean completion delivered
    as a wake or a steer never touched the latch. This is the missing write, on
    both delivery paths.

    ``update_task`` with no field to change is the registry's public flush: it
    re-stamps ``updated_at`` and mirrors the record — including the latch state
    ``_persist_task_state`` reads back out — into the durable queue. Best-effort
    throughout; a bookkeeping write never breaks a delivery.
    """
    try:
        from app.core.task_registry import task_registry

        for task_id in task_ids:
            if not task_id:
                continue
            if task_registry.mark_terminal_notified(session_id, task_id):
                task_registry.update_task(session_id, task_id)
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a delivery.
        logger.debug(
            "turn_scheduler: terminal_notified latch failed for %s (%s)",
            session_id, task_ids, exc_info=True,
        )


async def _try_steer_terminal_notification(
    session_id: str,
    prompt: str,
    frames: List[Dict[str, Any]],
    task_ids: List[str],
) -> bool:
    """Try to hand ``prompt`` to the session's RUNNING turn as a steer.

    True only when the engine accepted the submission — the caller then skips
    the queued wake it would otherwise enqueue. False for every "there is no
    running turn to steer into" answer (no runtime in this process, no adapter,
    a turn already being interrupted, a backend with no steering channel), which
    is also the at-rest case: the caller falls through to the ordinary wake.

    Never raises — :func:`session_runtime.try_steer_task_notification` swallows
    its own failures, and this wrapper covers the import.
    """
    try:
        from app.core import session_runtime

        return await session_runtime.try_steer_task_notification(
            session_id,
            prompt,
            notification_id=f"{TASK_STEER_ID_PREFIX}{uuid4().hex}",
            frames=frames,
            task_ids=task_ids,
        )
    except Exception:  # noqa: BLE001 — the queued wake is the correctness path.
        logger.warning(
            "turn_scheduler: mid-turn notification steer failed for %s",
            session_id, exc_info=True,
        )
        return False


def on_task_notification_delivered(session_id: str, task_ids: List[str]) -> None:
    """Receipt callback: fluid folded a steered ``<task-notification>`` into the
    live turn, so the model has the words. Latch ``terminal_notified``.

    Called from ``session_runtime._on_steer_delivered`` — the same receipt that
    retires a steered user message's queue row. SYNC and never raises."""
    _mark_terminal_notified(session_id, list(task_ids or []))
    logger.info(
        "turn_scheduler: mid-turn task notification delivered to %s (tasks=%s)",
        session_id, list(task_ids or []),
    )


async def on_task_notification_stranded(
    session_id: str,
    frames: List[Dict[str, Any]],
    task_ids: List[str],
) -> None:
    """Fallback: a steered notification the turn ended without ever delivering.

    A user's steer that dies this way is safe by construction — its queue row
    was never retired, so the settle-drain runs it as a turn of its own. A task
    notification has no such row, so the at-rest wake has to be enqueued HERE
    instead, or the completion is lost for good.

    Deliberately bypasses the ``mark_woken`` latch: the caller already spent it
    on the steer attempt, and this is that same single delivery arriving by the
    other road. Never raises — called from ``_finalize``, which must settle."""
    frames = [f for f in (frames or []) if isinstance(f, dict)]
    if not frames:
        return
    try:
        prompt = await asyncio.to_thread(_build_task_notification_prompt, frames)
        turn = await _enqueue_context_wake(
            session_id, "unpark", frames, list(task_ids or []), prompt,
        )
        logger.info(
            "turn_scheduler: steered notification for %s (tasks=%s) was never "
            "delivered — enqueued wake turn %s instead",
            session_id, list(task_ids or []), turn["id"],
        )
        kick(session_id)
    except Exception:  # noqa: BLE001 — a wake failure must never fail a settle.
        logger.warning(
            "turn_scheduler: could not enqueue the fallback wake for %s (tasks=%s)",
            session_id, list(task_ids or []), exc_info=True,
        )


async def on_task_terminal(session_id: str, frame: Dict[str, Any]) -> Optional[str]:
    """Idle-reader hook: a background task reached a terminal state between
    turns. Fold the terminal task into ONE consolidated ``source=unpark`` wake.

    Returns ``idle_reader.CLI_OWNED_WAKE_SIGNAL`` ("cli-owned-wake") when it
    takes the A-1 suppression branch (the CLI owns this completion's at-rest
    wake and will deliver it itself); the idle reader uses that to arm a grace
    window so it keeps draining until the CLI's wake turn lands instead of
    exiting on its next poll. Every other path returns ``None``.

    Near-simultaneous completions must batch into a single turn (the builder's
    consolidation only fires when it sees all pending frames at once), and a
    duplicate frame for a task already folded in must not spawn a second turn:

    * (a) a queued unpark whose ``task_ids`` already include this frame's task →
      complete no-op (idempotent);
    * (b) a queued unpark WITHOUT this task → merge: append the frame to
      ``payload.pending_frames``, rebuild the FULL prompt over every pending
      frame, and rewrite the row via a guarded UPDATE. If that UPDATE loses
      (the wake already started), fall through to (c);
    * (c) otherwise STEER the notification into the running turn if there is one
      (mid-turn delivery — it lands at the next cell boundary), else enqueue a
      new unpark wake carrying ``pending_frames`` + ``task_ids`` (context
      inherited from the parked turn).

    Kicks after a merge/enqueue (never after the (a) no-op or a steer).

    Two lanes, by ownership (#690 retired the PUMP lane — see below):

    * OWN — ``is_own_background_task`` true: the parent's own background work came
      to rest. Rich ``source=unpark`` wake, with the #688 merge + idempotency
      branches below.
    * else — NOT the parent's own background work (a child/grandchild step of a
      still-running sub-agent, an orphan, or a synchronous sub-agent at rest):
      SUPPRESS. #690 deleted the per-child immediate/deferred PUMP lane that used
      to enqueue a ``source=unpark_pump`` turn here: a child completion while its
      owner is alive produced one parent-visible wake per child (the "3 shells →
      3 wakes" storm) and, for shells with no tool_use_id, fired blindly. The
      parent now hears about delegated work ONLY via its owner task's OWN-lane
      terminal (the reference-harness contract: a background child notifies only
      its owning agent; the CLI resumes a parked owner natively). A silent turn
      is valid.

    A wholly-contentless frame (no ``task_id``/``tool_use_id``/``summary``/
    ``output_file``) is dropped at the choke point below with a DEBUG log — an
    empty notification is never enqueued (#690)."""
    try:
        task_id = frame.get("task_id")
        from app.core.task_registry import task_registry

        # An explicit Stop suppresses the canceled task before issuing provider
        # cancellation. Check at the entry point — ahead of both CLI-owned and
        # app-owned wake paths — so every terminal-frame race is a no-op.
        if task_id and task_registry.is_wake_suppressed(session_id, task_id):
            logger.info(
                "turn_scheduler: wake suppressed for user-canceled task %s/%s",
                session_id, task_id,
            )
            return
        # Content-empty guard (#690): the single enqueue choke point. A frame that
        # carries nothing renderable can only produce an empty <task-notification>
        # turn — drop it (DEBUG) before any lane decides to enqueue.
        if not _frame_has_renderable_content(frame):
            logger.debug(
                "turn_scheduler: dropping contentless task frame for %s (frame=%r)",
                session_id, frame,
            )
            return
        if task_registry.is_own_background_task(session_id, task_id):
            # ── OWN lane — rich consolidated source=unpark wake ──
            # A-1 (bug 1): if the CLI owns this completion's at-rest wake (a
            # top-level backgrounded agent resting while the parent is parked),
            # the CLI already injects the rich <task-notification> follow-up turn;
            # the app's own wake would be a SECOND, content-poor delivery. Suppress
            # it. Shells and non-completed terminals (failed/killed/stopped) keep
            # the app wake — the CLI does not own their at-rest resume.
            if await asyncio.to_thread(_cli_owns_at_rest_wake, session_id, frame):
                # A-1 latch: the CLI emits a rest-notification each time the agent
                # rests; latching the woken mark here lets the A-3 late-frame guard
                # drop post-unpark duplicate frames instead of enqueueing an empty
                # wake. (Return value ignored — this is a suppression path.)
                if task_id:
                    task_registry.mark_woken(session_id, task_id)
                logger.info(
                    "turn_scheduler: unpark for %s — task %s completed; CLI owns "
                    "the at-rest wake (parent parked), suppressing app enqueue",
                    session_id, task_id,
                )
                # Signal the idle reader to arm its CLI-wake grace window: the
                # CLI delivers this wake itself 7-60s later, so the reader must
                # keep draining instead of exiting on its next poll (see
                # idle_reader.CLI_OWNED_WAKE_SIGNAL).
                return "cli-owned-wake"
            existing = await asyncio.to_thread(_queued_unpark_turn, session_id)
            if existing is not None:
                existing_payload = dict(existing.get("payload") or {})
                existing_task_ids = list(existing_payload.get("task_ids") or [])
                existing_pending = list(existing_payload.get("pending_frames") or [])
                if task_id and task_id in existing_task_ids:
                    # (a) this task is already folded into the wake. A-2: if the
                    # incoming frame is RICHER than the one already stored for it
                    # (e.g. the rich TaskNotification arriving after the bare
                    # TaskUpdated built the wake), upgrade the stored frame in place
                    # and rebuild the prompt; otherwise it is a plain idempotent
                    # no-op.
                    stored_idx = next(
                        (i for i, f in enumerate(existing_pending)
                         if f.get("task_id") == task_id),
                        None,
                    )
                    if stored_idx is not None and _frame_is_richer(
                        frame, existing_pending[stored_idx]
                    ):
                        existing_pending[stored_idx] = frame
                        upgraded_payload = dict(existing_payload)
                        upgraded_payload["pending_frames"] = existing_pending
                        prompt = await asyncio.to_thread(
                            _build_task_notification_prompt, existing_pending
                        )
                        updated = await asyncio.to_thread(
                            turns_db.update_queued_turn_prompt,
                            existing["id"], prompt, upgraded_payload,
                        )
                        if updated is not None:
                            logger.info(
                                "turn_scheduler: unpark for %s — task %s rich frame "
                                "upgraded wake turn %s in place",
                                session_id, task_id, updated["id"],
                            )
                            kick(session_id)
                            return
                        # The guarded UPDATE lost (the wake left the queue) — the
                        # task already woke, so fall through to the A-3 latch below.
                    else:
                        logger.info(
                            "turn_scheduler: unpark for %s — task %s already in "
                            "wake turn %s, no-op",
                            session_id, task_id, existing.get("id"),
                        )
                        return
                else:
                    # (b) merge this frame into the pending wake and rebuild its
                    # prompt.
                    # Dedupe on the way in: branch (a) already covers the common
                    # repeat, but pending_frames and task_ids are two lists that
                    # can drift apart (a frame stored under a task_id that never
                    # made it into task_ids), and a drifted pair is how one task
                    # ends up with two blocks in the same prompt.
                    pending = _dedupe_frames_by_task_id(existing_pending + [frame])
                    merged_payload = dict(existing_payload)
                    merged_payload["pending_frames"] = pending
                    merged_task_ids = list(existing_task_ids)
                    if task_id and task_id not in merged_task_ids:
                        merged_task_ids.append(task_id)
                    merged_payload["task_ids"] = merged_task_ids
                    prompt = await asyncio.to_thread(
                        _build_task_notification_prompt, pending
                    )
                    updated = await asyncio.to_thread(
                        turns_db.update_queued_turn_prompt,
                        existing["id"], prompt, merged_payload,
                    )
                    if updated is not None:
                        # A-3: record the merged task woke so a later late frame
                        # for it can't re-enqueue.
                        task_registry.mark_woken(session_id, task_id)
                        logger.info(
                            "turn_scheduler: unpark for %s — task %s terminal (%s) "
                            "merged into wake turn %s (%d pending)",
                            session_id, task_id, frame.get("status"),
                            updated["id"], len(pending),
                        )
                        kick(session_id)
                        return
                    # The guarded UPDATE lost (the wake left the queue) — enqueue
                    # fresh (guarded by the A-3 latch below).

            # (c) enqueue a new consolidated unpark wake. A-3: the late-frame latch
            # (mark_woken returns False) means this task ALREADY produced a wake
            # that has since left the queue — a late/duplicate frame that must not
            # spawn a second wake. Guarded on a truthy task_id: a task_id-less frame
            # never latches (it also can't be deduped), so it enqueues as before.
            if task_id and not task_registry.mark_woken(session_id, task_id):
                logger.info(
                    "turn_scheduler: unpark for %s — task %s late frame; wake "
                    "already produced, no re-enqueue",
                    session_id, task_id,
                )
                return
            wake_task_ids = [task_id] if task_id else []
            prompt = await asyncio.to_thread(_build_task_notification_prompt, [frame])
            # MID-TURN DELIVERY: a wake enqueued while the parent is inside a
            # turn cannot run until that turn settles, so an hour-long turn buys
            # the completion an hour of silence. Steer it into the live turn
            # instead — it lands at the next cell boundary. False whenever the
            # session is genuinely at rest (or unsteerable), which falls through
            # to the queued wake below exactly as before. The ``mark_woken``
            # latch above is already spent, so the later park/idle wake cannot
            # deliver this task a second time.
            if await _try_steer_terminal_notification(
                session_id, prompt, [frame], wake_task_ids
            ):
                logger.info(
                    "turn_scheduler: unpark for %s — task %s terminal (%s) "
                    "steered into the live turn",
                    session_id, task_id, frame.get("status"),
                )
                return
            turn = await _enqueue_context_wake(
                session_id, "unpark", [frame], wake_task_ids, prompt
            )
            logger.info(
                "turn_scheduler: unpark for %s — task %s terminal (%s), wake turn %s",
                session_id, task_id, frame.get("status"), turn["id"],
            )
            kick(session_id)
            return

        # ── else — NOT the parent's own background work → suppress (#690) ──
        # A child/grandchild step of a still-running sub-agent, an orphan, or a
        # synchronous sub-agent at rest. The retired PUMP lane used to enqueue a
        # source=unpark_pump turn here; that produced the parent-visible
        # notification storm and, for tool_use_id-less shells, fired blindly. The
        # parent hears about this work only via its owner task's OWN-lane terminal.
        logger.info(
            "turn_scheduler: unpark-decision suppressed for %s — task %s "
            "not the parent's background work (pump lane retired, #690)",
            session_id, task_id,
        )
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


# Guidance for a PUMP wake (source="unpark_pump"): a single internal step of a
# background agent that is STILL RUNNING, surfaced so the model can react if the
# step's result demands it. The user is not waiting; when nothing needs doing the
# model should end the turn SILENTLY (no text) rather than emit a bare "ok" — the
# FE "ok"-ack suppression is kept only as a fallback for models that reply anyway.
_TASK_NOTIFICATION_PUMP_NOTE = (
    "This is an internal step of a background agent that is STILL RUNNING; it is "
    "surfaced to you only so you can react if the step's result requires it. No "
    "user-facing narration is expected and the user is not waiting on you. If no "
    "action is needed, do NOT respond at all — end the turn with no text; do not "
    "acknowledge, do not summarize. Respond ONLY if this result reveals a problem, "
    "a decision you must make, or an opportunity that requires you to act — in that "
    "case take the action directly."
)


_MAX_TASK_RESULT_CHARS = 60_000

# A summary beside a distinct result is a label, not another full payload. Keep the full _MAX_TASK_RESULT_CHARS
# allowance only when the summary is the sole result; otherwise bound the label to avoid repeating an already
# consumed report.
_MAX_TASK_SUMMARY_CHARS = 400


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


def _build_task_notification_prompt(pending: list, kind: str = "own") -> str:
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

    The payload is emitted ONCE. When the chain falls all the way through to the
    summary — a ProviderAgent worker's frame has a summary and no output_file, so
    it always does — the two elements would be byte-identical, so ``<summary>``
    carries the payload and ``<result>`` is omitted (both the transcript parser
    and the frontend read ``<summary>`` when ``<result>`` is absent). Only a
    genuinely different result gets its own element, and then the summary is
    capped back down to the label it is meant to be.

    ``pending`` is deduplicated by ``task_id`` first: one task contributes one
    block however many terminal frames it produced in the batch.

    ``kind`` selects the contract variant:

    * ``"own"`` (default) — the parent's own background work came to rest. Full
      ``<note>`` + trailing ``<usage>`` block, no ``<kind>`` element.
    * ``"pump"`` — one internal step of a still-running background agent. Nests a
      ``<kind>pump</kind>`` element right after the opening tag, uses the terse
      pump note, and OMITS ``<usage>`` (the step is not a completed unit of work).
      The full result fallback chain is KEPT — reactivity requires the model see
      the step result.

    Empty elements are OMITTED rather than emitted blank: a frame with no
    ``tool_use_id``, no ``output_file``, or no usage numbers renders without
    those tags. A frame whose ``kind`` is ``shell``/``monitor`` (a Monitor'd
    shell command, not a sub-agent) is labelled ``<kind>monitor</kind>`` and
    never carries ``<usage>`` — a shell has no sub-agent token accounting, and
    ``<subagent_tokens></subagent_tokens>`` misnamed the work it described.
    """
    from app.core.query_engine import _read_task_output_file
    from app.core.query_streaming import truncate_large_payload

    def _esc(value: Any) -> str:
        return "" if value is None else str(value)

    def _payload(text: str) -> str:
        # Truncate via the shared helper (matches query_engine's own use) instead
        # of a naive head-slice.
        if text and len(text) > _MAX_TASK_RESULT_CHARS:
            return truncate_large_payload(text, _MAX_TASK_RESULT_CHARS)
        return text

    def _label(text: str) -> str:
        # A label, so a plain head-slice with an honest marker — the shared
        # payload helper has its own display floor and would not honour a cap
        # this small.
        if text and len(text) > _MAX_TASK_SUMMARY_CHARS:
            dropped = len(text) - _MAX_TASK_SUMMARY_CHARS
            head = text[:_MAX_TASK_SUMMARY_CHARS].rstrip()
            return f"{head}… [+{dropped} chars, see <result>]"
        return text

    is_pump = kind == "pump"
    note_text = _TASK_NOTIFICATION_PUMP_NOTE if is_pump else _TASK_NOTIFICATION_NOTE

    blocks: List[str] = []
    for note in _dedupe_frames_by_task_id(pending):
        output_file = note.get("output_file") or ""
        summary_text = note.get("summary") or ""
        # Fallback chain: parsed final assistant text → raw output-file read
        # (backgrounded-shell stdout) → short summary → "".
        result_text = _extract_final_assistant_text(output_file)
        if not result_text:
            result_text = _read_task_output_file(output_file) or None
        if not result_text:
            result_text = summary_text
        # A ProviderAgent worker's frame has a summary and no output_file, so the
        # chain above lands back on the summary and the two elements come out
        # byte-identical. Emit the payload ONCE: <summary> carries it (both the
        # transcript parser and the frontend read <summary> when <result> is
        # absent) and <result> is dropped. Only a genuinely DIFFERENT result gets
        # its own element, and then the summary is capped back down to a label.
        result_is_summary = result_text == summary_text
        # A monitored SHELL command is not a sub-agent: it has no transcript, no
        # tool-use id of its own, and no token accounting. Saying "subagent" at
        # it — which an empty <usage> block's <subagent_tokens> did — misnames
        # the work the parent is being woken about.
        is_monitor = str(note.get("kind") or "").lower() in ("shell", "monitor")
        # PUMP: nested <kind>pump</kind> right after the opening tag; no <usage>.
        # MONITOR: <kind>monitor</kind>, so the model reads the block as a shell
        # command's exit rather than a sub-agent's report.
        # OWN AGENT: no <kind> element (byte-identical to before).
        if is_pump:
            kind_xml = "  <kind>pump</kind>\n"
        elif is_monitor:
            kind_xml = "  <kind>monitor</kind>\n"
        else:
            kind_xml = ""
        # <usage> is sub-agent accounting. Omit it for a pump step, for a
        # monitored shell, and whenever every field is empty — an all-blank
        # block is noise the model has to read past.
        usage = {} if (is_pump or is_monitor) else (note.get("usage") or {})
        usage_fields = "".join(
            f"    <{tag}>{_esc(value)}</{tag}>\n"
            for tag, value in (
                ("subagent_tokens", usage.get("total_tokens")),
                ("tool_uses", usage.get("tool_uses")),
                ("duration_ms", usage.get("duration_ms")),
            )
            if value is not None and _esc(value) != ""
        )
        usage_xml = f"  <usage>\n{usage_fields}  </usage>\n" if usage_fields else ""
        if result_is_summary:
            summary_xml = f"  <summary>{_payload(summary_text)}</summary>\n"
            result_xml = ""
        else:
            summary_xml = f"  <summary>{_label(summary_text)}</summary>\n"
            result_xml = f"  <result>{_payload(result_text)}</result>\n"
        # Empty identity elements are omitted, not emitted blank. A shell
        # monitor has neither a tool-use id nor an output file, and
        # `<tool-use-id></tool-use-id>` told the model nothing while reading
        # like a field it had failed to populate. Every consumer already treats
        # an absent tag as "unknown" (jsonl_parser's field extractor returns
        # None), so omission is the honest form.
        tool_use_xml = (
            f"  <tool-use-id>{_esc(note.get('tool_use_id'))}</tool-use-id>\n"
            if _esc(note.get("tool_use_id")) else ""
        )
        output_file_xml = (
            f"  <output-file>{_esc(output_file)}</output-file>\n"
            if _esc(output_file) else ""
        )
        blocks.append(
            "<task-notification>\n"
            f"{kind_xml}"
            f"  <task-id>{_esc(note.get('task_id'))}</task-id>\n"
            f"{tool_use_xml}"
            f"{output_file_xml}"
            f"  <status>{_esc(note.get('status'))}</status>\n"
            f"{summary_xml}"
            f"  <note>{note_text}</note>\n"
            f"{result_xml}"
            f"{usage_xml}"
            "</task-notification>"
        )
    return "\n".join(blocks)
