"""Turn-lifetime safety for ProviderAgent workers (chunk P3-A4).

The 2026-07-22 rate-limit storm is the design case, and its forensics
(reconstructed 2026-07-25 from ``~/.ai-shuffle/logs/debug-8766/app.log`` and the
``background_tasks`` table) are what this module exists to answer:

* the parent turn ended at 19:58:23 and **all 3,172 upstream requests happened
  after that** — background work outliving its turn is the whole failure mode;
* 122 tasks were registered but only 2 were the parent's own — the other 120
  started after the turn had ended;
* both registered roots reached a terminal status at 20:06:43 and **410 more
  upstream requests still flowed over the next two minutes**, with 23
  descendants running and 35 child processes alive.

That last point is the load-bearing one: **marking a registry row terminal does
NOT cascade to its subtree.** ``stop_session_task`` even falls through to
"ghost-reaping" the registry row when the pooled client is gone
(``app/core/query_session_lifecycle.py:293-321``) — the row goes ``killed`` and
the child keeps running. So teardown here kills the worker's SESSION and its
subprocess tree; the registry row is bookkeeping that follows, never the act
itself.

Scope, deliberately narrow (plan §2 C.4 RULING, 2026-07-25): turn-lifetime
binding applies to **ProviderAgent workers ONLY**. Ordinary CLI-native subagent
parking — the ``_finalize`` park gate at ``app/core/session_runtime.py:664-690``
— is a deliberate feature and is NOT changed. Nothing in this module can see or
touch a task it did not itself register.

Teardown is necessary but not sufficient: PR #761 adds the gateway-level
per-provider circuit breaker separately, and that breaker (not this) is the
protection against retries originating INSIDE an already-running child, which
no registry-driven teardown could ever have seen (plan §9 Q6).

Three responsibilities:

1. :func:`teardown_workers` — kill every live worker of a parent session.
   Called from ``SessionRuntime._finalize`` on EVERY normal, aborted and
   crashed turn end.
2. :func:`stop_all_workers` — the same kill, user-initiated, with user
   attribution on the terminal rows (plan §2 C.4 "stop-all control").
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
    """One live ProviderAgent worker, from dispatch until its tool call returns.

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
    #: is emitted before ``_finalize`` runs (``app/core/query_engine.py:1785``),
    #: so a card left running here would spin forever on replay.
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
    #: Task-tool background mode (ruling A8): a background worker deliberately
    #: OUTLIVES the caller's turn — its results are collected in a later turn
    #: via the task tools — so the turn-end teardown skips it. stop-all and the
    #: per-worker timeout still apply.
    background: bool = False


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
# Attribution (plan §2 C.4: "live attribution + stop-all")
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

    Readable while the turn runs — that is the point (plan §2 C.4). Returns
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

async def _kill_worker_session(worker_session_id: str) -> str:
    """Kill ONE worker's session and its subprocess tree. Never raises.

    Two acts, in order, because either alone is insufficient:

    1. ``turn_manager.interrupt`` flips the durable ``turns`` row to
       ``interrupting`` and fires the interrupt executor, so a runtime executing
       the worker turn on ANY runner reacts. Without it a peer runner would keep
       streaming the turn it owns.
    2. ``session_pool.remove_broken`` evicts the pooled SDK client, which is
       what actually ends the CLI subprocess AND its descendants
       (``snapshot_descendants`` + ``record_terminated_subprocess``). This is
       the step the 2026-07-22 forensics show missing: the registry rows went
       terminal at 20:06:43 while 35 child processes stayed alive.

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

        row = await asyncio.to_thread(
            turn_manager.interrupt, worker_session_id, "stop", True
        )
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
    worker's OWN bookkeeping, which is how the live DB grew a user-killed sync
    child showing 0 calls / 0 tokens / $0 against real provider usage, with
    ``turns.state`` never leaving its live state and ``sessions.status`` still
    ``'active'`` while the background-task row said ``killed``. The parent's
    card, registry row and attribution were all settled; only the child's own
    rows were not.

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

    THE trap that makes a naive teardown silently do nothing (plan §2 C.4): the
    park gate at ``app/core/session_runtime.py:674`` ORs
    ``task_registry.get_background_launches(sid)``, a set that is NEVER reset
    per turn and is cleared only by ``clear_session``
    (``app/core/task_registry.py:1049``). A teardown that merely flips running
    tasks terminal leaves that set populated, so the turn parks anyway — and so
    does every turn after it, forever.

    Strictly surgical: only THIS worker's ids are discarded, so an ordinary
    CLI-native subagent's launch id — whose parking is a deliberate feature the
    ruling says must not change — is left exactly where it was.
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
    ``_finalize`` runs (``app/core/query_engine.py:1785`` →
    ``app/core/turn_recorder.py:703-709``), and the reducer speculatively
    settles running cards only on ``turn.completed``, never on ``turn.aborted``
    (``reducer.ts:609``). A card we kill after that point therefore gets no
    second chance — if we skip this it spins forever on every replay.
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

    This is the chip-flood answer (plan §2 C.4): ``complete_task`` fires a
    durable terminal-notification chip per killed task
    (``app/core/task_registry.py:785-796``), and fan-out is uncapped by ruling
    P12, so N workers would mean N chips at once. Teardown suppresses those
    (``notify=False``) and leaves this single line instead.

    Emitted as a ``system`` frame with an explicit ``subtype``/``data``: the
    shared translator maps a subtype-less system frame onto a ``SystemNote``
    whose fields are empty, so the text would be silently dropped
    (``app/core/turn_recorder.py:352-356``).
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


async def _cancel_worker_prompts(record: WorkerRecord) -> None:
    """Sweep this worker's still-pending approval prompts off its PARENT.

    A worker's prompts are RAISED ON THE PARENT's session id (P3-A3), so killing
    the worker's own session leaves them pending on a surface the user is still
    watching, asked by a worker that no longer exists — and the worker's waiter
    sits in a detached SDK task that only the row leaving ``pending`` unblocks.
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
    await _settle_card(record, preview, summary)

    outcome = await _kill_worker_session(record.worker_session_id)
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
            # Suppress the per-task terminal chip: uncapped fan-out (P12)
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
    background workers (ruling A8): they outlive the caller's turn by design
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
        await _note_parent(
            parent_session_id, note.format(count=len(results)), len(results), reason
        )
    return results


async def teardown_workers(parent_session_id: str) -> List[Dict[str, Any]]:
    """Turn-end teardown: no ProviderAgent worker outlives its parent turn.

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
    results = await _teardown(
        parent_session_id,
        reason=STOP_ALL_REASON,
        summary=STOP_ALL_SUMMARY,
        preview="Stopped by the user.",
        note="Stopped {count} ProviderAgent worker(s) at the user's request.",
    )
    return {"stopped": len(results), "workers": results}
