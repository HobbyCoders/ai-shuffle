"""Turn-lifetime safety for Task-dispatched workers.

Background work can outlive its turn, and marking a task_registry row terminal
does NOT stop the child: ``stop_session_task`` may only ghost-reap the row
while the child keeps running. So teardown here kills the worker's SESSION and
its subprocess tree; the registry row is bookkeeping that follows, never the
act itself.

Scope is deliberately narrow: only workers this module registered (via
``provider_agent.dispatch_worker``). Ordinary CLI-native subagent parking (the
park gate in ``SessionRuntime._finalize``) is a feature and is unchanged.
Nothing here can see or touch a task it did not itself register.

Three responsibilities:

1. :func:`teardown_workers` — kill every live worker of a parent session.
   Called from ``SessionRuntime._finalize`` on EVERY normal, aborted and
   crashed turn end.
2. :func:`stop_all_workers` — the same kill, user-initiated, with user
   attribution on the terminal rows.
3. :func:`attribution` — per-turn worker token/spend grouped by the worker's
   PROVIDER, readable while the turn is still running.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


#: ``reason`` stamped on a worker reaped because its parent turn ended. Distinct
#: from ``task_registry.USER_STOP_REASON`` so a turn-end reap is never read as a
#: user action in the logs or on the retained terminal row.
TURN_END_REASON = "parent_turn_ended"
TURN_END_SUMMARY = "Parent turn ended — worker stopped"

#: ``reason`` stamped by the explicit stop-all control.
STOP_ALL_REASON = "user_stopped_all"
STOP_ALL_SUMMARY = "Stopped by user (stop all workers)"

#: ``reason`` stamped on a worker killed because its provider's circuit breaker
#: opened mid-flight (``provider_agent.dispatch_worker``'s rate-limit fail-fast).
#: Distinct from both of the above so the terminal row says WHY: the worker was
#: neither a turn-end casualty nor a user stop, it was refused upstream.
RATE_LIMITED_REASON = "provider_rate_limited"
RATE_LIMITED_SUMMARY = "Provider rate limited — worker stopped"

#: Usage keys we sum for attribution. Anything else in a ``turn.completed``
#: usage payload is carried through untouched on the per-worker row but is not
#: aggregated (the contract calls ``usage`` an open dict).
_TOKEN_KEYS = (
    "tokens_in",
    "tokens_out",
    "cache_creation_tokens",
    "cache_read_tokens",
)
_COST_KEY = "total_cost_usd"


@dataclass
class WorkerRecord:
    """One live Task worker, from dispatch until its tool call returns.

    ``task_id`` and ``tool_use_id`` are BOTH the worker's session id today (see
    ``dispatch_worker``'s "ONE join key" note) but are kept as separate fields
    so a future dispatcher that threads a real MCP ``tool_use_id`` does not have
    to change teardown.
    """

    parent_session_id: str
    worker_session_id: str
    task_id: str
    tool_use_id: str
    provider_id: str = ""
    model_id: str = ""
    label: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    usage: Dict[str, Any] = field(default_factory=dict)
    #: The worker's :class:`app.core.provider_agent_feed.WorkerFeed`, when one
    #: exists. Teardown uses it to SETTLE the parent's card — ``turn.completed``
    #: is emitted before ``_finalize`` runs, so a card left running here would
    #: spin forever on replay.
    feed: Any = None
    #: Latched by teardown so the dispatcher's own ``finally`` does not
    #: re-settle the card or re-complete the registry row.
    torn_down: bool = False
    #: WHY teardown ran (:data:`TURN_END_REASON` / :data:`STOP_ALL_REASON` /
    #: :data:`RATE_LIMITED_REASON`), latched beside ``torn_down``. Read by
    #: ``provider_agent.dispatch_worker`` to fail the worker's outcome with a
    #: message that says what actually happened instead of the default success
    #: it would otherwise derive from an empty event log.
    torn_down_reason: Optional[str] = None
    #: Task-tool background mode: a background worker deliberately
    #: OUTLIVES the caller's turn — its results are collected in a later turn
    #: via the task tools — so the turn-end teardown skips it. stop-all and the
    #: per-worker timeout still apply.
    background: bool = False
    #: Explicit message continuation; guarded against an earlier dispatch cleanup.
    continuation_turn_id: Optional[str] = None


# parent_session_id -> {worker_session_id: WorkerRecord}
_LIVE: Dict[str, Dict[str, WorkerRecord]] = {}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_worker(record: WorkerRecord) -> WorkerRecord:
    """Track ``record`` as a live worker of its parent (idempotent by id)."""
    if not record.parent_session_id or not record.worker_session_id:
        return record
    _LIVE.setdefault(record.parent_session_id, {})[record.worker_session_id] = record
    return record


def unregister_worker(parent_session_id: str, worker_session_id: str) -> Optional[WorkerRecord]:
    """Drop one worker from the live set. Returns the dropped record, if any."""
    bucket = _LIVE.get(parent_session_id)
    if not bucket:
        return None
    record = bucket.pop(worker_session_id, None)
    if not bucket:
        _LIVE.pop(parent_session_id, None)
    return record


def get_worker(parent_session_id: str, worker_session_id: str) -> Optional[WorkerRecord]:
    """The live record for one worker, or ``None``."""
    return _LIVE.get(parent_session_id, {}).get(worker_session_id)


def live_workers(parent_session_id: str) -> List[WorkerRecord]:
    """Every live worker of ``parent_session_id``, oldest first."""
    bucket = _LIVE.get(parent_session_id)
    if not bucket:
        return []
    return sorted(bucket.values(), key=lambda r: r.started_at)


def has_live_workers(parent_session_id: str) -> bool:
    """True iff ``parent_session_id`` has at least one live worker."""
    return bool(_LIVE.get(parent_session_id))


def reset() -> None:
    """Drop every tracked worker (tests / session teardown)."""
    _LIVE.clear()


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def record_usage(
    parent_session_id: str,
    worker_session_id: str,
    usage: Optional[Dict[str, Any]],
) -> None:
    """Merge a worker's latest usage snapshot onto its record.

    Usage arrives as a WHOLE-turn snapshot in the worker's ``turn.completed``
    payload, so this REPLACES matching keys rather than accumulating them —
    accumulating would double-count a snapshot seen both by the feed poller and
    by ``dispatch_worker``'s final read. Never raises.
    """
    if not isinstance(usage, dict) or not usage:
        return
    record = get_worker(parent_session_id, worker_session_id)
    if record is None:
        return
    try:
        record.usage.update(usage)
    except Exception:  # noqa: BLE001 — attribution must never break a worker
        logger.debug("ProviderAgent supervisor: usage merge failed", exc_info=True)


def attribution(parent_session_id: str) -> Dict[str, Any]:
    """Per-worker and per-PROVIDER token/spend for a parent's live workers.

    Readable while the turn runs — that is the point. Returns
    ``{"workers": [...], "providers": {...}, "totals": {...}}`` with an empty
    ``workers`` list when nothing is live, so a caller can attach it
    unconditionally and a wire consumer can test one field.
    """
    workers: List[Dict[str, Any]] = []
    providers: Dict[str, Dict[str, Any]] = {}
    totals: Dict[str, Any] = {k: 0 for k in _TOKEN_KEYS}
    totals[_COST_KEY] = 0.0
    totals["workers"] = 0

    for record in live_workers(parent_session_id):
        usage = record.usage or {}
        row = {
            "workerSessionId": record.worker_session_id,
            "taskId": record.task_id,
            "toolUseId": record.tool_use_id,
            "provider": record.provider_id,
            "model": record.model_id,
            "label": record.label,
            "startedAt": record.started_at,
            "usage": dict(usage),
        }
        workers.append(row)

        bucket = providers.setdefault(
            record.provider_id or "unknown",
            {k: 0 for k in _TOKEN_KEYS} | {_COST_KEY: 0.0, "workers": 0},
        )
        bucket["workers"] += 1
        totals["workers"] += 1
        for key in _TOKEN_KEYS:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                bucket[key] += value
                totals[key] += value
        cost = usage.get(_COST_KEY)
        if isinstance(cost, (int, float)):
            bucket[_COST_KEY] += cost
            totals[_COST_KEY] += cost

    return {"workers": workers, "providers": providers, "totals": totals}


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

async def _kill_worker_session(worker_session_id: str, *, reason: str = "host_cleanup") -> str:
    """Kill ONE worker's session and its subprocess tree. Never raises.

    Two acts, in order, because either alone is insufficient:

    1. ``turn_manager.interrupt`` flips the durable ``turns`` row to
       ``interrupting`` and fires the interrupt executor, so a runtime executing
       the worker turn on ANY runner reacts. Without it a peer runner would keep
       streaming the turn it owns.
    2. ``session_pool.remove_broken`` evicts the pooled adapter, which is
       what actually ends the CLI subprocess AND its descendants
       (``snapshot_descendants`` + ``record_terminated_subprocess``). Marking
       registry rows terminal without this step leaves child processes alive.

    ``remove_broken`` rather than ``remove``: this runs on the ``_finalize`` hot
    path, and ``remove_broken`` pops the pool entry synchronously while the
    (multi-second, exit-grace + SIGTERM) subprocess teardown continues in the
    background. The kill is not delayed — only its bookkeeping is.

    Returns a short outcome token for the log/result: ``"killed"`` when a pooled
    client was actually evicted, ``"interrupted"`` when only the durable turn
    was flipped, ``"idle"`` when there was nothing live to stop.
    """
    interrupted = False
    try:
        from app.core import turn_manager

        # interrupt_async, not to_thread(interrupt): the interrupt executor is a
        # coroutine fn that needs a running loop, so calling the sync form from a
        # worker thread silently drops the interrupt (the DB row flips but the
        # live runtime is never told to stop).
        row = await turn_manager.interrupt_async(worker_session_id, "stop", True,
                                                 source=reason)
        logger.info("ProviderAgent supervisor: stopping %s source=%s", worker_session_id, reason)
        interrupted = row is not None
    except Exception:  # noqa: BLE001 — the pool eviction below still runs
        logger.debug(
            "ProviderAgent supervisor: interrupt failed for worker %s",
            worker_session_id, exc_info=True,
        )

    evicted = False
    try:
        from app.core.session_pool import get_session_pool

        pool = get_session_pool()
        remover = getattr(pool, "remove_broken", None) or getattr(pool, "remove", None)
        if remover is not None:
            evicted = await remover(worker_session_id) is not None
    except Exception:  # noqa: BLE001
        logger.warning(
            "ProviderAgent supervisor: pool eviction failed for worker %s",
            worker_session_id, exc_info=True,
        )

    if evicted:
        return "killed"
    if interrupted:
        return "interrupted"
    return "idle"


async def settle_killed_worker(worker_session_id: str, reason: str) -> None:
    """Book a killed worker's partial usage and stamp its terminal rows.

    Killing a worker ends the runtime that would otherwise have settled the
    worker's OWN bookkeeping. Without this, a killed child would show no usage
    against real provider spend, its ``turns.state`` would never leave its live
    state and ``sessions.status`` would stay ``'active'`` while the
    background-task row says ``killed``.

    Delegated to :func:`app.core.query_persistence.settle_killed_session`,
    which claims the turn with the guarded ``settle_turn`` UPDATE before
    booking anything — so a worker whose runtime DID reach its own turn end
    is left completely alone. Never raises.
    """
    try:
        from app.core.query_persistence import settle_killed_session

        await settle_killed_session(worker_session_id, stop_reason=reason)
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.debug(
            "ProviderAgent supervisor: killed-worker settle failed for %s",
            worker_session_id, exc_info=True,
        )


def _purge_park_gate_residue(parent_session_id: str, record: WorkerRecord) -> None:
    """Drop this worker's ids from the CUMULATIVE park-gate launch sets.

    THE trap that makes a naive teardown silently do nothing: the park gate in
    ``SessionRuntime._finalize`` reads the background-launch set
    (``task_registry.get_background_launches``), which is NEVER reset per turn
    and is cleared only by ``task_registry.clear_session``. A teardown that
    merely flips running tasks terminal leaves that set populated, so the turn
    can park anyway — and so can every turn after it.

    Strictly surgical: only THIS worker's ids are discarded, so an ordinary
    CLI-native subagent's launch id — whose parking is a deliberate feature —
    is left exactly where it was.
    """
    try:
        from app.core.task_registry import task_registry

        discard = getattr(task_registry, "discard_background_launch", None)
        if discard is None:
            return
        for candidate in {record.tool_use_id, record.task_id, record.worker_session_id}:
            if candidate:
                discard(parent_session_id, candidate)
    except Exception:  # noqa: BLE001
        logger.debug(
            "ProviderAgent supervisor: park-gate purge failed for %s",
            parent_session_id, exc_info=True,
        )


async def _settle_card(record: WorkerRecord, preview: str, summary: Optional[str]) -> None:
    """Settle the parent's subagent card for a torn-down worker. Never raises.

    Load-bearing ordering note: ``turn.completed`` is emitted BEFORE
    ``_finalize`` runs, and the reducer speculatively settles running cards
    only on ``turn.completed``, never on ``turn.aborted`` (``reducer.ts``
    ``case 'turn.completed'``). A card we kill after that point therefore gets
    no second chance — if we skip this it spins forever on every replay.
    """
    feed = record.feed
    if feed is None:
        return
    try:
        await feed.stop_following(final_drain=False)
    except Exception:  # noqa: BLE001
        logger.debug("ProviderAgent supervisor: feed stop failed", exc_info=True)
    try:
        await feed.completed("killed", result_preview=preview, summary=summary)
    except Exception:  # noqa: BLE001
        logger.debug("ProviderAgent supervisor: card settle failed", exc_info=True)


#: ``system.note`` subtype for the coalesced teardown line.
NOTE_SUBTYPE = "provider_agent_teardown"


async def _note_parent(parent_session_id: str, message: str, count: int, reason: str) -> None:
    """Append ONE coalesced system note to the parent's log. Never raises.

    This is the chip-flood answer: ``task_registry.complete_task`` fires a
    durable terminal-notification chip per killed task, and fan-out can be
    wide, so N workers would mean N chips at once. Teardown suppresses those
    (``notify=False``) and leaves this single line instead.

    Emitted as a ``system`` frame with an explicit ``subtype``/``data``: the
    shared translator (``turn_recorder._translate_shared``) maps a subtype-less
    system frame onto a ``SystemNote`` whose fields are empty, so the text
    would be silently dropped.
    """
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(parent_session_id, {
            "type": "system",
            "subtype": NOTE_SUBTYPE,
            "data": {"message": message, "count": count, "reason": reason},
        })
    except Exception:  # noqa: BLE001
        logger.debug(
            "ProviderAgent supervisor: teardown note failed for %s",
            parent_session_id, exc_info=True,
        )


async def _deliver_teardown(
    parent_session_id: str,
    results: List[Dict[str, Any]],
    *,
    message: str,
    reason: str,
    summary: str,
) -> None:
    """Tell the PARENT — its engine, and its live turn if it has one.

    ``_note_parent`` writes the coalesced line to the session's event log, which
    is the transcript the user reads. It is not a channel the MODEL has, so a
    parent that heard nothing else would keep telling the user its killed
    worker is still running.

    Two deliveries close it, both best-effort:

    * the ENGINE is told each killed worker is terminal
      (``turn_scheduler.retire_task_terminals``, ``wake=False``). fluid drops the
      ids from its pending set — a worker killed at turn end would otherwise be
      listed on every later ``SessionParked`` — and records a terminal
      ``SubagentEvent`` on the parent's session, which is the thing the parent
      reads back instead of assuming its worker is still working. It schedules
      no turn, so an interrupt the user just asked for stays stopped.
    * when a turn is somehow still running (the stop-all control fires mid-turn,
      unlike the turn-end reap), the same line is STEERED into it so the model
      hears it at the next tool boundary rather than at the end of the turn.

    Never raises: a teardown that has already killed its workers must settle.
    """
    task_ids = [r.get("taskId") for r in results if r.get("taskId")]
    if not parent_session_id or not task_ids:
        return
    frames = [
        {
            "task_id": task_id,
            "status": "killed",
            "summary": summary,
            "reason": reason,
        }
        for task_id in task_ids
    ]
    try:
        from app.core import turn_scheduler

        await turn_scheduler.retire_task_terminals(parent_session_id, frames)
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.debug(
            "ProviderAgent supervisor: engine retirement failed for %s",
            parent_session_id, exc_info=True,
        )
    try:
        from app.core import session_runtime
        from app.core.turn_scheduler import TASK_STEER_ID_PREFIX
        from uuid import uuid4

        await session_runtime.try_steer_task_notification(
            parent_session_id,
            f"<task-notification>\n{message}\n</task-notification>",
            notification_id=f"{TASK_STEER_ID_PREFIX}{uuid4().hex}",
            # No frames/task_ids: the engine retirement above already booked
            # these terminals, and handing them to the steer registry would make
            # an undelivered steer enqueue a WAKE turn for work the user just
            # stopped. The steer here is a courtesy for a live turn, nothing more.
            frames=[],
            task_ids=[],
        )
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.debug(
            "ProviderAgent supervisor: teardown steer failed for %s",
            parent_session_id, exc_info=True,
        )


async def _cancel_worker_prompts(record: WorkerRecord) -> None:
    """Sweep this worker's still-pending approval prompts off its PARENT.

    A worker's prompts are RAISED ON THE PARENT's session id, so killing the
    worker's own session leaves them pending on a surface the user is still
    watching, asked by a worker that no longer exists — and the worker's waiter
    sits in a detached task that only the row leaving ``pending`` unblocks.
    The parent id comes off the record rather than the approval-route registry:
    routes are never unregistered, so they identify no rows.

    Off-loop (``to_thread``) because this is DB work on the ``_finalize`` hot
    path, and best-effort like every other teardown leg.
    """
    try:
        from app.core import provider_agent_approvals

        await asyncio.to_thread(
            provider_agent_approvals.cancel_worker_prompts,
            record.worker_session_id,
            record.parent_session_id,
        )
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.debug(
            "ProviderAgent supervisor: prompt sweep failed for worker %s",
            record.worker_session_id, exc_info=True,
        )


async def teardown_worker(
    record: WorkerRecord,
    *,
    reason: str,
    summary: str,
    preview: str,
) -> Dict[str, Any]:
    """Kill ONE live worker, end to end. Never raises.

    The single per-worker teardown every kill path goes through — turn-end
    reap, the stop-all control, and ``dispatch_worker``'s rate-limit fail-fast —
    so a worker stopped for ANY reason settles its card, drops its registry row
    and clears its park-gate residue identically.

    Order is deliberate: settle the card, THEN kill the session, THEN sweep the
    parent-keyed prompts, THEN complete the registry row, THEN purge the
    park-gate residue. The registry drop is what clears the ``/live`` spinner
    overlay, so the card must already be settled by the time it happens (same
    rule ``dispatch_worker``'s ``finally`` follows).
    """
    parent_session_id = record.parent_session_id
    record.torn_down = True
    record.torn_down_reason = reason
    if record.continuation_turn_id:
        # A continuation may still be queued or parked, with no live runtime to
        # clear its queue or stop descendants when the interrupt is delivered.
        from app.core.query_session_lifecycle import stop_all_background_tasks
        await stop_all_background_tasks(record.worker_session_id, user_stop=True, source=reason)
    await _settle_card(record, preview, summary)

    outcome = await _kill_worker_session(record.worker_session_id, reason=reason)
    # AFTER the kill, so the runtime has stopped appending to the event log the
    # flush reads, and BEFORE the registry row goes terminal, so the worker's
    # own rows are settled by the time anything reads the pair.
    await settle_killed_worker(record.worker_session_id, reason)
    await _cancel_worker_prompts(record)

    try:
        from app.core.task_registry import task_registry

        task_registry.complete_task(
            parent_session_id,
            record.task_id,
            "killed",
            summary=summary,
            reason=reason,
            # Suppress the per-task terminal chip: a wide fan-out
            # would otherwise surface one chip per worker in a burst. The
            # single coalesced note the caller emits replaces them.
            notify=False,
        )
    except TypeError:
        # A task_registry without the ``notify`` kwarg (older worker in the
        # mixed-version cluster): completing the row still matters far more
        # than the chip suppression.
        try:
            from app.core.task_registry import task_registry as _registry

            _registry.complete_task(
                parent_session_id, record.task_id, "killed",
                summary=summary, reason=reason,
            )
        except Exception:  # noqa: BLE001
            logger.debug("ProviderAgent supervisor: complete_task failed", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("ProviderAgent supervisor: complete_task failed", exc_info=True)

    _purge_park_gate_residue(parent_session_id, record)
    unregister_worker(parent_session_id, record.worker_session_id)

    return {
        "workerSessionId": record.worker_session_id,
        "taskId": record.task_id,
        "provider": record.provider_id,
        "model": record.model_id,
        "outcome": outcome,
    }


async def _teardown(
    parent_session_id: str,
    *,
    reason: str,
    summary: str,
    preview: str,
    note: Optional[str],
    include_background: bool = True,
) -> List[Dict[str, Any]]:
    """Kill every live worker of ``parent_session_id``. Never raises.

    ``include_background=False`` (the turn-end path) spares Task-tool
    background workers: they outlive the caller's turn by design
    and die on completion, timeout, or stop-all.
    """
    records = live_workers(parent_session_id)
    if not include_background:
        records = [r for r in records if not getattr(r, "background", False)]
    if not records:
        return []

    results: List[Dict[str, Any]] = []
    for record in records:
        results.append(await teardown_worker(
            record, reason=reason, summary=summary, preview=preview,
        ))

    logger.info(
        "ProviderAgent: tore down %d worker(s) for session %s (reason=%s)",
        len(results), parent_session_id, reason,
    )
    if note:
        message = note.format(count=len(results))
        await _note_parent(parent_session_id, message, len(results), reason)
        # The note is a transcript row; this is the half the MODEL can read.
        await _deliver_teardown(
            parent_session_id, results,
            message=message, reason=reason, summary=summary,
        )
    return results


async def teardown_workers(parent_session_id: str) -> List[Dict[str, Any]]:
    """Turn-end teardown: no synchronous worker outlives its parent turn.

    Called from ``SessionRuntime._finalize`` between ``persist_turn_end_state``
    and the park-gate read, which is the ONE point every normal, aborted and
    crashed turn end funnels through. A no-op (and cheap — one dict lookup)
    for the overwhelming majority of turns, which dispatch no workers at all.
    """
    if not parent_session_id or not has_live_workers(parent_session_id):
        return []
    return await _teardown(
        parent_session_id,
        include_background=False,
        reason=TURN_END_REASON,
        summary=TURN_END_SUMMARY,
        preview="The parent turn ended before the worker finished.",
        note="Stopped {count} ProviderAgent worker(s) because the turn ended.",
    )


async def stop_all_workers(parent_session_id: str) -> Dict[str, Any]:
    """The stop-all control: stop EVERY live worker for a turn, on demand.

    Same kill as :func:`teardown_workers` with user attribution on the terminal
    rows, so the retained ``/live`` row reads as a deliberate stop rather than a
    mystery failure. Returns ``{"stopped": N, "workers": [...]}``.
    """
    from app.core.swarm import cancel_all
    await cancel_all(parent_session_id)
    results = await _teardown(
        parent_session_id,
        reason=STOP_ALL_REASON,
        summary=STOP_ALL_SUMMARY,
        preview="Stopped by the user.",
        note="Stopped {count} ProviderAgent worker(s) at the user's request.",
    )
    return {"stopped": len(results), "workers": results}
