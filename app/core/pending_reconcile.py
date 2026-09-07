"""Reconcile the ENGINE's background-pending set against the app's task truth.

fluid keeps its own ``_background_pending`` set and rebuilds it, on every
rehydrate, purely from the ``SubagentEvent`` log
(``fluid/fluid/engine/background.py`` ``_rehydrate_background_lifecycle_unlocked``):
a ``started`` event puts an id in, and ONLY a terminal ``SubagentEvent``
(``finished``/``failed``/``cancelled``) takes it out. Every ``SessionParked``
lists whatever is left.

That makes a lost terminal permanent. In the audited session seven tasks came
to rest without one — six delivered as mid-turn steers before the round-6
retirement fix shipped, one killed by an interrupt that emitted nothing — and
those seven ids then appeared in 35 of the next 38 parks, one of them for
twenty-one hours. The round-6 fix (``turn_scheduler.retire_task_terminals``)
stops NEW ids leaking, but it is not retroactive: the terminal events are
absent from the durable log, so each rehydrate faithfully re-creates the
phantoms.

This module closes both halves:

* :func:`reconcile_engine_pending` — run whenever a session's engine comes up
  (adapter connect / resume). It asks the live engine what it still counts
  pending, checks each id against the app's task truth (the in-memory registry
  first, the durable ``background_tasks`` mirror behind it), and submits a
  terminal for every id the app knows is over. The submission goes through
  ``turn_scheduler.retire_task_terminals`` — the round-6 helper — so there is
  exactly one place that talks to ``prepare_background_completions``. Because
  fluid PERSISTS the resulting terminal ``SubagentEvent``, this repair is
  durable: the next rehydrate is clean.

* :func:`repair_fluid_pending_log` — the boot sweep's arm, for sessions nobody
  has re-opened yet. With no engine to ask, it derives the same orphan set
  straight from ``fluid_session_events`` and appends the missing terminal
  events itself, at ``max(seq) + 1``. That is safe precisely because fluid
  seeds a resumed run's numbering from the store's maximum
  (``native._seed_bus_seq``), so a repaired session simply continues after the
  rows written here.

Everything in this module is best-effort: a reconcile failure must never break
a connect, a turn, or boot. Read helpers answer empty on any error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

#: The one non-terminal status in the registry's vocabulary. Everything else
#: ("completed", "error", "killed", "stopped", …) is a settled task.
RUNNING_STATUS = "running"

#: App task status → the fluid terminal kind a repair event should carry.
#: Mirrors ``harness_adapter.prepare_background_completions``'s map; anything
#: unrecognised is recorded as a failure rather than silently "finished".
_FLUID_TERMINAL_KIND = {
    "completed": "finished",
    "finished": "finished",
    "error": "failed",
    "failed": "failed",
    "killed": "cancelled",
    "cancelled": "cancelled",
    "stopped": "cancelled",
}

#: Boot-sweep bounds. The sweep reads one indexed query per session, so it is
#: cheap per session but must not turn a boot into a full-history scan.
MAX_REPAIR_SESSIONS = 200
MAX_REPAIR_IDS_PER_SESSION = 100


# ---------------------------------------------------------------------------
# Durable task truth (read-only; NEVER edits task_registry)
# ---------------------------------------------------------------------------

def durable_task_statuses(
    session_id: str, task_ids: Sequence[str]
) -> Dict[str, str]:
    """``{task_id: status}`` for the ids this install holds a durable row for.

    One query, instance-scoped like every other ``background_tasks`` reader.
    Ids with no row at all are simply absent from the result — that is the
    launched-but-not-yet-persisted case, and callers must treat it as "not
    known to be terminal", never as terminal.
    """
    ids = [str(t) for t in (task_ids or []) if t]
    if not session_id or not ids:
        return {}
    try:
        from app.core.config import settings
        from app.db.base import get_db, rows_to_list
        from app.db.compat import ph

        _ph = ph()
        placeholders = ", ".join([_ph] * len(ids))
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""SELECT task_id, status FROM background_tasks
                     WHERE instance_id = {_ph} AND session_id = {_ph}
                       AND task_id IN ({placeholders})""",
                (settings.effective_instance_id, session_id, *ids),
            )
            rows = rows_to_list(cursor.fetchall())
    except Exception:  # noqa: BLE001 — a read failure is "nothing known".
        logger.debug(
            "pending_reconcile: durable status read failed for %s", session_id,
            exc_info=True,
        )
        return {}
    out: Dict[str, str] = {}
    for row in rows:
        task_id = row.get("task_id") if isinstance(row, dict) else row[0]
        status = row.get("status") if isinstance(row, dict) else row[1]
        if task_id:
            out[str(task_id)] = str(status or "")
    return out


def settled_task_ids(session_id: str, task_ids: Sequence[str]) -> Set[str]:
    """The subset of ``task_ids`` whose DURABLE row says they are over.

    The park gate reads the in-memory registry, which can be a beat behind the
    row a completion already wrote (the audited park listed a shell whose row
    had settled four seconds earlier and whose terminal frame landed 0.13 s
    after the park, buying a no-op wake turn). An id with no durable row is
    never reported settled.
    """
    statuses = durable_task_statuses(session_id, task_ids)
    return {
        task_id for task_id, status in statuses.items()
        if status and status != RUNNING_STATUS
    }


def nonterminal_agent_task_ids(session_id: str) -> List[str]:
    """Durable rows for sub-agent work this session still has outstanding.

    The park gate's third arm. Deliberately ``kind == "agent"`` ONLY: a
    sub-agent is never a fluid ``serves`` receipt, whereas a Monitor shell can
    be a dev server that is meant to stay up — parking on one of those would
    pin the session forever, which is exactly why fluid excludes ``serves``
    from park accounting and why the registry-backed gate ignores Monitor
    shells today.
    """
    if not session_id:
        return []
    try:
        from app.db.background_tasks import (
            list_session_nonterminal_background_tasks,
        )

        rows = list_session_nonterminal_background_tasks(session_id)
    except Exception:  # noqa: BLE001 — a read failure parks nothing extra.
        logger.debug(
            "pending_reconcile: durable agent-work read failed for %s",
            session_id, exc_info=True,
        )
        return []
    out: List[str] = []
    for row in rows or []:
        kind = row.get("kind") if isinstance(row, dict) else row[5]
        task_id = row.get("task_id") if isinstance(row, dict) else row[0]
        if task_id and (kind or "") == "agent":
            out.append(str(task_id))
    return out


def _task_status(session_id: str, task_id: str) -> Optional[str]:
    """This task's status per the in-memory registry, else None."""
    try:
        from app.core.task_registry import task_registry

        record = task_registry.get_task(session_id, task_id)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(record, dict):
        return None
    status = record.get("status")
    return str(status) if status else None


def terminal_frames_for(
    session_id: str, task_ids: Sequence[str]
) -> List[Dict[str, Any]]:
    """Retirement frames for every id the app can PROVE is terminal.

    Registry first (it carries the summary), durable mirror behind it. An id
    neither source knows, or that either still calls ``running``, is left
    alone: a pending set is only ever narrowed by evidence, never by silence —
    retiring a live worker would drop the very park that is holding its
    session open.
    """
    ids = [str(t) for t in (task_ids or []) if t]
    if not session_id or not ids:
        return []
    unresolved = [t for t in ids if (_task_status(session_id, t) or "") == ""]
    durable = durable_task_statuses(session_id, unresolved) if unresolved else {}
    frames: List[Dict[str, Any]] = []
    for task_id in ids:
        status = _task_status(session_id, task_id) or durable.get(task_id) or ""
        if not status or status == RUNNING_STATUS:
            continue
        frames.append({
            "task_id": task_id,
            "status": status,
            "summary": (
                "The app recorded this task as finished; its completion never "
                "reached the engine, so the session was still listed as parked "
                "on it."
            ),
        })
    return frames


# ---------------------------------------------------------------------------
# Live-engine reconcile (adapter connect / resume)
# ---------------------------------------------------------------------------

def _pooled_adapter(session_id: str) -> Any:
    """The session's pooled adapter, or None. Peeks — never mints a session."""
    try:
        from app.core.session_pool import get_session_pool

        state = get_session_pool().peek(session_id)
    except Exception:  # noqa: BLE001 — no pool, no reconcile.
        logger.debug(
            "pending_reconcile: pool peek failed for %s", session_id,
            exc_info=True,
        )
        return None
    return getattr(state, "adapter", None) if state is not None else None


async def engine_pending_task_ids(adapter: Any) -> List[str]:
    """What the session's fluid engine still counts as in-flight.

    Reaches into the engine's background-lifecycle state on purpose: fluid
    exposes no accessor, and the set is only meaningful AFTER a rehydrate,
    which is itself lazy (it runs on the next task start, terminal, or park).
    Taking the engine's own lock and forcing the rehydrate here is what makes a
    reconcile at connect time see the same set the first park would have.

    Answers ``[]`` for a delegated leg (its CLI keeps its own pending set), a
    disconnected adapter, or any engine that does not carry this state.
    """
    if adapter is None or getattr(adapter, "_is_delegated", False):
        return []
    engine = getattr(adapter, "_engine", None)
    pending = getattr(engine, "_background_pending", None)
    if not isinstance(pending, dict):
        return []
    lock = getattr(engine, "_background_lock", None)
    rehydrate = getattr(engine, "_rehydrate_background_lifecycle_unlocked", None)
    try:
        if lock is not None and callable(rehydrate):
            async with lock:
                await rehydrate()
                return [str(task_id) for task_id in pending]
        return [str(task_id) for task_id in pending]
    except Exception:  # noqa: BLE001 — never break a connect over bookkeeping.
        logger.debug(
            "pending_reconcile: could not read the engine pending set",
            exc_info=True,
        )
        return []


async def reconcile_engine_pending(
    session_id: str, adapter: Any = None
) -> List[str]:
    """Retire every engine-pending id the app knows is terminal. Never raises.

    Returns the ids actually submitted for retirement (empty when there was
    nothing to do, or when the submission could not be made). Submission goes
    through ``turn_scheduler.retire_task_terminals``; when the pool cannot yet
    supply the adapter — which is the case DURING a connect, before the pooled
    entry exists — the adapter we were handed is used directly, because it is
    the same call the helper would have made.
    """
    if not session_id:
        return []
    try:
        if adapter is None:
            adapter = _pooled_adapter(session_id)
        pending = await engine_pending_task_ids(adapter)
        if not pending:
            return []
        frames = terminal_frames_for(session_id, pending)
        if not frames:
            return []
        from app.core import turn_scheduler

        retired = await turn_scheduler.retire_task_terminals(session_id, frames)
        if not retired and adapter is not None:
            prepare = getattr(adapter, "prepare_background_completions", None)
            if prepare is None:
                return []
            await prepare(frames)
        task_ids = [f["task_id"] for f in frames]
        logger.warning(
            "pending_reconcile: retired %d stale engine-pending task(s) for "
            "%s (%s) — their completions never reached the engine",
            len(task_ids), session_id, task_ids,
        )
        return task_ids
    except Exception:  # noqa: BLE001 — bookkeeping never breaks a connect.
        logger.warning(
            "pending_reconcile: engine reconcile failed for %s", session_id,
            exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# Boot repair (no engine to ask — go straight at the durable event log)
# ---------------------------------------------------------------------------

def _subagent_event_rows(fluid_session_id: str) -> List[Dict[str, Any]]:
    """Every ``SubagentEvent`` row of one fluid session, oldest first.

    Narrowed in SQL by ``event ->> '_cls'`` so a session with a hundred
    thousand delta rows still costs one small result set. (The test shim
    rewrites ``->>`` to ``json_extract``; this predicate is the shape it
    documents as supported.)
    """
    if not fluid_session_id:
        return []
    try:
        from app.db.base import get_db, rows_to_list
        from app.db.compat import ph

        _ph = ph()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""SELECT seq, event FROM fluid_session_events
                     WHERE session_id = {_ph}
                       AND event ->> '_cls' = 'SubagentEvent'
                     ORDER BY seq""",
                (fluid_session_id,),
            )
            return rows_to_list(cursor.fetchall())
    except Exception:  # noqa: BLE001 — an unreadable log repairs nothing.
        logger.debug(
            "pending_reconcile: subagent-event read failed for %s",
            fluid_session_id, exc_info=True,
        )
        return []


def _as_event_dict(raw: Any) -> Dict[str, Any]:
    """The stored event payload as a dict (Postgres hands back a dict, the
    test shim a JSON string)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        import json

        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def orphan_pending_ids(fluid_session_id: str) -> List[str]:
    """Ids a rehydrate would still call pending, straight from the log.

    Exactly fluid's own rule: a ``started`` puts an id in, one of
    ``finished``/``failed``/``cancelled`` takes it out. Order preserved so the
    repair writes terminals in the order the tasks were launched.
    """
    started: List[str] = []
    terminal: Set[str] = set()
    for row in _subagent_event_rows(fluid_session_id):
        event = _as_event_dict(
            row.get("event") if isinstance(row, dict) else row[1]
        )
        task_id = event.get("task_id")
        kind = event.get("kind")
        if not task_id:
            continue
        task_id = str(task_id)
        if kind == "started":
            if task_id not in started:
                started.append(task_id)
        elif kind in ("finished", "failed", "cancelled"):
            terminal.add(task_id)
    return [t for t in started if t not in terminal]


def _max_event_seq(fluid_session_id: str) -> int:
    try:
        from app.db.base import get_db
        from app.db.compat import ph

        _ph = ph()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT MAX(seq) FROM fluid_session_events WHERE session_id = {_ph}",
                (fluid_session_id,),
            )
            row = cursor.fetchone()
    except Exception:  # noqa: BLE001
        logger.debug(
            "pending_reconcile: max-seq read failed for %s", fluid_session_id,
            exc_info=True,
        )
        return -1
    if row is None:
        return 0
    value = row[0] if not isinstance(row, dict) else list(row.values())[0]
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def repair_fluid_pending_log(
    app_session_id: str, fluid_session_id: str
) -> List[str]:
    """Append the terminal ``SubagentEvent`` every orphaned id is missing.

    Boot-only. Writes are appended after the session's current maximum ``seq``,
    which is where fluid itself resumes numbering (``_seed_bus_seq`` takes the
    store's maximum when the store offers no ``last_event_seq``), so a repaired
    session's next run continues past these rows rather than colliding with
    them.

    ``wake=False`` / ``wake_disposition="suppressed"`` throughout: this is
    bookkeeping about work that finished long ago, and the app's durable turn
    queue owns wake admission. Only ids the app can PROVE are terminal are
    written. Returns the repaired ids; never raises.
    """
    if not app_session_id or not fluid_session_id:
        return []
    try:
        orphans = orphan_pending_ids(fluid_session_id)[:MAX_REPAIR_IDS_PER_SESSION]
        if not orphans:
            return []
        frames = terminal_frames_for(app_session_id, orphans)
        if not frames:
            return []

        from fluid.events.model import WAKE_SUPPRESSED, SubagentEvent, event_to_dict

        from app.core.provider.fluid_pg_store import _append_event_sync

        seq = _max_event_seq(fluid_session_id)
        if seq < 0:
            return []
        repaired: List[str] = []
        for frame in frames:
            seq += 1
            kind = _FLUID_TERMINAL_KIND.get(str(frame.get("status") or ""), "failed")
            event = SubagentEvent(
                session_id=fluid_session_id,
                seq=seq,
                task_id=str(frame["task_id"]),
                kind=kind,
                summary=frame.get("summary"),
                # Same shape ``prepare_background_completions`` submits, so a
                # replay maps this row exactly as it maps a live retirement:
                # a receipt carrying a worker session belongs to the host's own
                # task cards and is not mirrored a second time.
                worker_session_id=str(frame["task_id"]),
                wake=False,
                status=str(frame.get("status") or kind),
                wake_disposition=WAKE_SUPPRESSED,
            )
            _append_event_sync(fluid_session_id, event_to_dict(event), seq)
            repaired.append(str(frame["task_id"]))
        logger.warning(
            "pending_reconcile: repaired %d orphaned engine-pending task(s) in "
            "the event log of %s (fluid %s): %s",
            len(repaired), app_session_id, fluid_session_id, repaired,
        )
        return repaired
    except Exception:  # noqa: BLE001 — a repair failure must never block boot.
        logger.warning(
            "pending_reconcile: event-log repair failed for %s", app_session_id,
            exc_info=True,
        )
        return []


def list_sessions_for_pending_repair(
    limit: int = MAX_REPAIR_SESSIONS,
) -> List[Tuple[str, str]]:
    """``(app_session_id, fluid_session_id)`` for this install's open sessions.

    "Open" is deliberately generous — anything not archived that has actually
    run on a fluid engine — because a poisoned pending set is invisible until
    the session is next used, and the whole point of the boot arm is to clear
    it before the user gets there. Newest first, bounded.
    """
    try:
        from app.core.config import settings
        from app.db.base import get_db, rows_to_list
        from app.db.compat import ph

        _ph = ph()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""SELECT id, sdk_session_id FROM sessions
                     WHERE instance_id = {_ph}
                       AND sdk_session_id IS NOT NULL
                       AND status <> 'archived'
                     ORDER BY updated_at DESC
                     LIMIT {_ph}""",
                (settings.effective_instance_id, int(limit)),
            )
            rows = rows_to_list(cursor.fetchall())
    except Exception:  # noqa: BLE001
        logger.debug("pending_reconcile: session listing failed", exc_info=True)
        return []
    out: List[Tuple[str, str]] = []
    for row in rows:
        app_id = row.get("id") if isinstance(row, dict) else row[0]
        fluid_id = row.get("sdk_session_id") if isinstance(row, dict) else row[1]
        if app_id and fluid_id:
            out.append((str(app_id), str(fluid_id)))
    return out


async def sweep_orphaned_engine_pending(
    limit: int = MAX_REPAIR_SESSIONS,
) -> int:
    """Boot arm: repair every open session's poisoned pending set.

    Runs once, from ``startup_reconcile.reconcile_from_durable_truth``, AFTER
    the stale-row sweep has settled this install's non-terminal
    ``background_tasks`` rows — that ordering is what lets the repair prove an
    orphan is terminal. Returns the number of task ids repaired; never raises.
    """
    repaired = 0
    for app_id, fluid_id in list_sessions_for_pending_repair(limit):
        try:
            ids = await asyncio.to_thread(
                repair_fluid_pending_log, app_id, fluid_id
            )
        except Exception:  # noqa: BLE001 — one bad session must not stop boot.
            logger.warning(
                "pending_reconcile: boot repair failed for %s", app_id,
                exc_info=True,
            )
            continue
        repaired += len(ids)
    return repaired
