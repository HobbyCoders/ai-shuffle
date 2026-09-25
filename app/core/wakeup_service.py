"""Durable wakeups + the idle-at-edge watchdog.

Two problems this module solves, both facets of the edge-of-context stall where
the Claude Code CLI cuts a turn silently at the compaction boundary and the session
parks forever because no next turn arrives:

1. **Durable wakeups.** :func:`schedule` persists the wake to
   ``pending_wakeups`` first, so an app restart does not drop it, and
   :func:`rehydrate_pending` re-arms this install's rows on boot. Firing is a
   *claim* (delete the row, then run) so a rehydrated timer and a peer install
   cannot double-fire — and because the claim is destructive, a submit that
   raises or refuses puts the row BACK with a backed-off ``fire_at`` instead of
   dropping the wake on the floor.

2. **Idle-at-edge watchdog.** :func:`scan_once` (run on a timer by
   :func:`watchdog_loop`) finds sessions sitting IDLE above the context-window
   edge and arms a synthetic nudge through the SAME wakeup machinery. Firing a
   real turn is what makes the CLI run its deferred compaction — the
   belt-and-braces backstop that covers the cases the opt-in ScheduleWakeup and
   the turn scheduler's boundary auto-continue miss.

A wake does NOT run a query of its own. :func:`_fire` enrolls the prompt in the
v2 turn queue (``await turn_manager.submit_async(..., source="wakeup")``), so the wake goes
through the same run lease and one-live-turn-per-session index every other turn
obeys. Firing straight at the query engine would create a second agent loop
against a session that already has one running: a forked conversation DAG, no
``turns`` row, no lease, and untracked spend.

Two cancellation rules keep armed wakes from stacking up:

* :func:`schedule` replaces any outstanding wake for the same
  ``(session_id, source)`` — one pending wake per producer, never a pile;
* a user message supersedes every pending wake for the session
  (:func:`cancel_session_wakeups`, called from ``turn_manager.submit`` on
  ``user``/``queue`` turns). The agent can re-arm on its next turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.db import wakeups as wakeups_db

logger = logging.getLogger(__name__)

# The nudge a watchdog wake fires. Honest + explicitly labeled so it never reads
# as a human instruction when it renders in chat. Its purpose is simply to BE a
# new turn (which triggers the CLI's deferred compaction); the wording steers a
# session with nothing left to do toward a quiet stop.
WATCHDOG_NUDGE_PROMPT = (
    "[auto-continue] This session was idle near the context-window limit; a "
    "compaction has now been triggered. If you had unfinished work, continue it "
    "exactly where you left off. If everything is already complete, stop after a "
    "brief acknowledgement — do not invent new work."
)

# Overdue-wake rehydrate stagger: on boot, wakes whose fire_at already passed are
# re-armed a few seconds apart (capped) instead of all firing at once, so a long
# outage does not stampede the query engine at startup.
_REHYDRATE_STAGGER_SECONDS = 3.0
_REHYDRATE_STAGGER_CAP_SECONDS = 60.0

# Submit-failure retry policy. The claim is destructive (the row is deleted
# before the turn is submitted, which is what stops a double fire), so a submit
# that raises or refuses would otherwise lose the wake outright. A failed submit
# re-inserts the SAME row id with a backed-off ``fire_at`` and re-arms it; after
# _RETRY_MAX_ATTEMPTS the wake is dropped loudly (log + a system note on the
# session) instead of silently.
_RETRY_BASE_SECONDS = 5.0
_RETRY_MAX_SECONDS = 120.0
_RETRY_MAX_ATTEMPTS = 5

# Per-wakeup submit-failure counter, keyed by wakeup id. In-process only: a
# restart resets it, which at worst grants a rehydrated row one more round of
# retries — the safe direction for a wake that has not run yet.
_retry_attempts: "dict[str, int]" = {}

#: ``system.note`` subtype for the "this wake never ran" line.
DROPPED_NOTE_SUBTYPE = "wakeup_dropped"

#: ``pending_wakeups.source`` for peer messages held while their receiver was
#: parked (``turn_scheduler``). The row's prompt is the held messages; firing
#: it promotes them to a ``source="promotion"`` turn. A user's message does not
#: supersede it, because the row is the only copy of those messages.
HELD_PEER_SOURCE = "held_peer_promotion"

# Live references to in-flight wake timer tasks, keyed by wakeup id (asyncio
# keeps only weak refs, and the key is what lets a specific wake be disarmed
# when its row is deleted out from under it).
_timers: "dict[str, asyncio.Task]" = {}

# Strong refs for the (unexpected) idless row — same GC guard, no key.
_anon_timers: "set[asyncio.Task]" = set()

@dataclass
class _WatchdogLatch:
    """Per-session watchdog state, latched at the moment a nudge is armed.

    ``fraction`` is the persisted usage fraction that triggered the nudge — a
    later sweep only re-arms once the CURRENT fraction has visibly dropped
    below it (compaction landed), not merely once the cooldown has elapsed.
    ``count`` tracks lifetime nudges for this session (this process only);
    once it hits :data:`_WATCHDOG_LIFETIME_CAP` the session is ``muted`` and
    never re-nudged again until process restart.
    """

    monotonic_ts: float
    fraction: float
    count: int = 0
    muted: bool = False


# In-memory per-session latch for the watchdog: session_id -> _WatchdogLatch.
# Bounds re-nudging a session whose compaction did not drop it back under the
# fraction (rate AND count). Reset on process restart (at most one extra
# nudge cycle per session after a restart — acceptable for a backstop).
_watchdog_cooldown: "dict[str, _WatchdogLatch]" = {}

# Required drop (as a fraction, e.g. 0.02 == 2 percentage points) between the
# fraction latched at the last nudge and the current persisted fraction
# before a session is eligible to be re-nudged. Guards against re-nudging a
# session whose compaction was deferred and never actually landed.
_WATCHDOG_DECREASE_EPSILON = 0.02

# Max nudges armed for a single session over this process's lifetime. A
# session that hits this cap is muted rather than nudged forever.
_WATCHDOG_LIFETIME_CAP = 3

_watchdog_task: "Optional[asyncio.Task]" = None


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# Firing + arming
# ---------------------------------------------------------------------------

async def _resolve_profile_id(row: Dict[str, Any]) -> Optional[str]:
    """The profile a wake should run under: the row's, else the session's.

    A wakeup persisted without a profile (an older row, or a producer that had
    none to hand) would enqueue a turn the runtime cannot resolve an agent for,
    so the session's own profile is the fallback and "neither" is a drop.
    """
    profile_id = row.get("profile_id")
    if profile_id:
        return profile_id
    session_id = row.get("session_id")
    if not session_id:
        return None
    try:
        from app.db import database

        session = await asyncio.to_thread(database.get_session, session_id)
    except Exception:  # noqa: BLE001 — a lookup failure is just "no fallback".
        logger.exception(
            "wakeup profile fallback lookup failed for session %s", session_id
        )
        return None
    return (session or {}).get("profile_id") or None


async def _announce_dropped_wake(row: Dict[str, Any], reason: str) -> None:
    """Tell the SESSION that a wake it was promised will never run.

    The agent parked expecting to be woken, so without a note the session sits
    silent until a human types something. The note is
    an unfenced ``system`` frame on the session's own log — the same seam the
    supervisor's teardown note uses — so the stall is visible where the user is
    already looking. Strictly best-effort: never raises.
    """
    session_id = row.get("session_id")
    if not session_id:
        return
    message = (
        "A scheduled wake-up for this session could not be started "
        f"({reason}). Nothing resumed automatically — send a message to "
        "continue."
    )
    try:
        from app.core.turn_recorder import record_drained_event

        await record_drained_event(str(session_id), {
            "type": "system",
            "subtype": DROPPED_NOTE_SUBTYPE,
            "data": {
                "message": message,
                "reason": reason,
                "source": row.get("source"),
                "wakeup_id": row.get("id"),
            },
        })
    except Exception:  # noqa: BLE001 — a note must never break the wake path.
        logger.debug(
            "wakeup drop note failed for session %s", session_id, exc_info=True
        )


async def _rearm_after_failed_submit(row: Dict[str, Any], reason: str) -> None:
    """Re-insert and re-arm a claimed wake whose turn submission did not take.

    The claim already deleted the row, so this is what keeps the wake alive:
    the SAME id is written back with a backed-off ``fire_at`` and a fresh timer
    is armed on it. Re-using the id keeps the one-row-per-wake invariant (and
    the claim's double-fire guarantee) intact for the retry.

    After :data:`_RETRY_MAX_ATTEMPTS` the wake is given up on — logged at ERROR
    with the last refusal reason and announced on the session — rather than
    retried forever against a condition that is clearly not clearing.
    """
    wakeup_id = row.get("id")
    session_id = row.get("session_id")
    attempt = _retry_attempts.get(str(wakeup_id), 0) + 1 if wakeup_id else 1
    if attempt > _RETRY_MAX_ATTEMPTS:
        if wakeup_id:
            _retry_attempts.pop(str(wakeup_id), None)
        logger.error(
            "wakeup %s (session %s, source %s) dropped after %d failed submit "
            "attempts: %s",
            wakeup_id, session_id, row.get("source"), _RETRY_MAX_ATTEMPTS,
            reason,
        )
        await _announce_dropped_wake(row, reason)
        return

    delay = min(
        _RETRY_BASE_SECONDS * (2 ** (attempt - 1)), _RETRY_MAX_SECONDS
    )
    fire_at = _now_epoch() + delay
    try:
        replacement = await asyncio.to_thread(
            wakeups_db.create_wakeup,
            id=str(wakeup_id),
            session_id=str(session_id),
            prompt=row.get("prompt"),
            fire_at=fire_at,
            profile_id=row.get("profile_id"),
            project_id=row.get("project_id"),
            reason=row.get("reason"),
            model=row.get("model"),
            source=row.get("source") or "schedule_wakeup",
            instance_id=row.get("instance_id") or settings.effective_instance_id,
        )
    except Exception:  # noqa: BLE001 — a re-arm failure is a real drop.
        logger.exception(
            "wakeup %s (session %s) could not be re-armed after a failed "
            "submit (%s) — the wake is lost",
            wakeup_id, session_id, reason,
        )
        await _announce_dropped_wake(row, reason)
        return

    if wakeup_id:
        _retry_attempts[str(wakeup_id)] = attempt
    logger.warning(
        "wakeup %s (session %s, source %s) submit failed (%s) — re-armed in "
        "%.0fs (attempt %d/%d)",
        wakeup_id, session_id, row.get("source"), reason, delay, attempt,
        _RETRY_MAX_ATTEMPTS,
    )
    _arm(replacement, delay)


def _session_user_stopped(session_id: Optional[str]) -> bool:
    """True while ``session_id`` is under a user Stop with no newer message.

    Read through a helper so an import/registry failure can never turn a wake
    into an exception: a wakeup service that cannot answer the question fires
    the wake, which is the behaviour that predates the latch.
    """
    if not session_id:
        return False
    try:
        from app.core.task_registry import task_registry

        return task_registry.is_user_stopped(session_id)
    except Exception:  # noqa: BLE001 — never let the check break a wake.
        logger.debug("wakeup: stop-latch check failed", exc_info=True)
        return False


async def _fire(row: Dict[str, Any]) -> None:
    """Claim one wakeup (delete its row) and enrol its prompt in the turn queue.

    The claim stays first and stays atomic: it is what stops a rehydrated timer
    and a peer install from both firing. The wake is a ``source="wakeup"`` turn
    submitted through :func:`app.core.turn_manager.submit` rather than a direct
    query-engine call. If the session already has a live turn the new turn
    simply queues behind it and drains at settlement, instead of racing it with
    a second agent loop.

    Failure-safe: the claim is destructive, so every
    way the submit can fail to produce a turn — it raises, or it refuses and
    returns no turn id — re-inserts the row with a backed-off ``fire_at`` and
    re-arms it (:func:`_rearm_after_failed_submit`). A wake that is ultimately
    given up on, or that cannot resolve a profile at all, says so on the
    session (:func:`_announce_dropped_wake`) instead of vanishing into a log
    line while the agent stays parked waiting for it.
    """
    wakeup_id = row.get("id")
    session_id = row.get("session_id")

    # STOP SEMANTICS: a session the user stopped stays stopped until
    # they speak again. Stop already DELETES the session's pending wakeup rows,
    # which is the cross-runner half of this; the latch is the local race-closer
    # for a timer that had already fired (or was mid-flight) when Stop landed.
    # Dropped rather than re-armed: the wake's premise — that the session should
    # pick work back up on its own — is exactly what the user just refused.
    if _session_user_stopped(session_id):
        logger.info(
            "wakeup %s (session %s, source %s) dropped — the user stopped this "
            "session and has not messaged it since",
            wakeup_id, session_id, row.get("source"),
        )
        if wakeup_id:
            await asyncio.to_thread(wakeups_db.claim_wakeup, wakeup_id)
            _retry_attempts.pop(str(wakeup_id), None)
        return

    claimed = await asyncio.to_thread(wakeups_db.claim_wakeup, wakeup_id)
    if not claimed:
        logger.info(
            "wakeup %s (session %s) already claimed/fired — skipping",
            wakeup_id, session_id,
        )
        return

    profile_id = await _resolve_profile_id(row)
    if not profile_id:
        logger.warning(
            "wakeup %s (session %s, source %s) has no profile and its session "
            "resolves to none — dropping the wake rather than queueing a turn "
            "that cannot run",
            wakeup_id, session_id, row.get("source"),
        )
        if wakeup_id:
            _retry_attempts.pop(str(wakeup_id), None)
        await _announce_dropped_wake(
            row, "no agent profile could be resolved for the wake"
        )
        return

    # The wake must run on the scheduling turn's effective model, not the
    # profile default — otherwise it re-primes the prompt cache on the wrong
    # model. Rows without a model (e.g. idle_watchdog) keep the default path.
    model = row.get("model")
    overrides = {"model": model} if model else None

    from app.core import turn_manager

    try:
        if row.get("source") == HELD_PEER_SOURCE:
            # Held peer messages run as a context wake (the parked turn's
            # overrides and attribution), not a bare prompt on the profile.
            from app.core import turn_scheduler

            turn_id = await turn_scheduler.promote_held_peer_messages(
                session_id, row.get("prompt") or "",
            )
        else:
            turn_id = await turn_manager.submit_async(
                session_id,
                "wakeup",
                prompt=row.get("prompt"),
                profile_id=profile_id,
                project_id=row.get("project_id"),
                overrides=overrides,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a raise must not lose the wake.
        logger.warning(
            "wakeup %s (session %s) submit raised", wakeup_id, session_id,
            exc_info=True,
        )
        await _rearm_after_failed_submit(row, f"{type(exc).__name__}: {exc}")
        return
    if not turn_id:
        await _rearm_after_failed_submit(
            row, "the turn manager refused the wake (no turn id returned)"
        )
        return

    if wakeup_id:
        _retry_attempts.pop(str(wakeup_id), None)
    logger.info(
        "wakeup fired: id=%s session=%s source=%s -> turn %s",
        wakeup_id, session_id, row.get("source"), turn_id,
    )


def _cancel_timer_task(task: "asyncio.Task") -> None:
    """Cancel a wake timer from whichever thread we happen to be on.

    ``Task.cancel`` is only safe on the task's own loop; ``cancel_session_wakeups``
    is a sync function callable from a worker thread (``turn_manager.submit``
    runs wherever its caller does), so hop back via ``call_soon_threadsafe``
    when the loops differ.
    """
    if task.done():
        return
    try:
        loop = task.get_loop()
    except Exception:  # noqa: BLE001
        task.cancel()
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        task.cancel()
        return
    try:
        loop.call_soon_threadsafe(task.cancel)
    except RuntimeError:  # loop already closed — nothing left to disarm
        pass


def _disarm(wakeup_ids: "list[str]") -> None:
    """Cancel the in-process timers for ``wakeup_ids`` (missing ids are fine)."""
    for wid in wakeup_ids:
        task = _timers.pop(wid, None)
        if task is not None:
            _cancel_timer_task(task)


def cancel_session_wakeups(
    session_id: str,
    source: Optional[str] = None,
    *,
    exclude_source: Optional[str] = None,
) -> int:
    """Delete a session's pending wakeups and disarm their local timers.

    Returns the number of rows removed. ``exclude_source`` keeps that
    source's rows when ``source`` is None. Synchronous on purpose: the callers are
    sync (``turn_manager.submit``, the session-delete path). Use
    :func:`cancel_session_wakeups_async` from async code so the DB hit lands on
    a worker thread.

    Wakes armed by a PEER install are not reachable from here and do not need to
    be: their timer's :func:`app.db.wakeups.claim_wakeup` finds no row and
    no-ops.
    """
    try:
        ids = wakeups_db.pop_session_wakeups(
            session_id, source,
            **({"exclude_source": exclude_source} if exclude_source else {}),
        )
    except Exception:  # noqa: BLE001 — cancelling must never break the caller.
        logger.exception(
            "wakeup cancel failed for session %s (source=%s)", session_id, source
        )
        return 0
    _disarm(ids)
    if ids:
        logger.info(
            "wakeup cancel: dropped %d pending wake(s) for session %s (source=%s)",
            len(ids), session_id, source or "*",
        )
    return len(ids)


async def cancel_session_wakeups_async(
    session_id: str, source: Optional[str] = None
) -> int:
    """Async form of :func:`cancel_session_wakeups` (DB hit off the loop)."""
    try:
        ids = await asyncio.to_thread(
            wakeups_db.pop_session_wakeups, session_id, source
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "wakeup cancel failed for session %s (source=%s)", session_id, source
        )
        return 0
    _disarm(ids)
    if ids:
        logger.info(
            "wakeup cancel: dropped %d pending wake(s) for session %s (source=%s)",
            len(ids), session_id, source or "*",
        )
    return len(ids)


async def take_session_wakeups(session_id: str, source: str) -> List[Dict[str, Any]]:
    """Delete a session's pending wakeups of ``source``, disarm their timers,
    and return the rows. A row a timer already claimed is not returned."""
    rows = await asyncio.to_thread(wakeups_db.take_session_wakeups, session_id, source)
    _disarm([str(row["id"]) for row in rows if row.get("id")])
    return rows


def _arm(row: Dict[str, Any], delay_seconds: float) -> None:
    """Arm an in-process timer that fires ``row`` after ``delay_seconds``."""
    wakeup_id = row.get("id")

    async def _run() -> None:
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await _fire(row)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a wake failure must never leak.
            logger.exception(
                "wakeup task failed id=%s session=%s",
                row.get("id"), row.get("session_id"),
            )

    task = asyncio.create_task(_run(), name=f"wakeup-{row.get('session_id')}")
    if not wakeup_id:
        # No id to key on (shouldn't happen for a persisted row) — keep a plain
        # strong ref so asyncio's weak bookkeeping cannot collect the timer.
        _anon_timers.add(task)
        task.add_done_callback(_anon_timers.discard)
        return

    previous = _timers.get(wakeup_id)
    if previous is not None and previous is not task:
        _cancel_timer_task(previous)
    _timers[wakeup_id] = task

    def _forget(finished: "asyncio.Task", _id: str = wakeup_id) -> None:
        if _timers.get(_id) is finished:
            _timers.pop(_id, None)

    task.add_done_callback(_forget)


def arm_persisted(row: Dict[str, Any]) -> None:
    """Arm the timer for a row a sync caller already persisted, at its
    ``fire_at``. Without a running loop the row waits for boot rehydrate."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _arm(row, max(0.0, float(row.get("fire_at") or 0.0) - _now_epoch()))


def _held_merge_kwargs(
    session_id: str, prompt: str, delay_seconds: float, *, prepend: bool,
    profile_id: Optional[str], project_id: Optional[str], model: Optional[str],
) -> Dict[str, Any]:
    return dict(
        id=f"wake-{uuid.uuid4().hex}",
        session_id=session_id,
        source=HELD_PEER_SOURCE,
        prompt=prompt,
        fire_at=_now_epoch() + max(0.0, float(delay_seconds)),
        prepend=prepend,
        profile_id=profile_id,
        project_id=project_id,
        reason="peer messages held while parked",
        model=model,
        instance_id=settings.effective_instance_id,
    )


async def hold_peer_messages(
    session_id: str, prompt: str, *, delay_seconds: float,
    profile_id: Optional[str], project_id: Optional[str], model: Optional[str],
) -> Dict[str, Any]:
    """Add ``prompt`` to the session's held peer messages and return the row.

    The session keeps one :data:`HELD_PEER_SOURCE` row: the messages so far
    plus this one, promoted at the earliest deadline already set, or
    ``delay_seconds`` from now for the first message. The fold is one
    transaction (:func:`wakeups_db.merge_session_wakeup`), so a promotion
    that fires meanwhile is never delivered twice and earlier text is never
    lost. Refused (``{}``) for a session the user stopped.
    """
    if _session_user_stopped(session_id):
        logger.info(
            "held peer message refused for stopped session %s", session_id,
        )
        return {}
    row, replaced = await asyncio.to_thread(
        wakeups_db.merge_session_wakeup,
        **_held_merge_kwargs(
            session_id, prompt, delay_seconds, prepend=False,
            profile_id=profile_id, project_id=project_id, model=model,
        ),
    )
    _disarm(replaced)
    _arm(row, max(0.0, float(row["fire_at"]) - _now_epoch()))
    return row


def restore_held_peer_messages(
    session_id: str, prompt: str, *, delay_seconds: float,
    profile_id: Optional[str], project_id: Optional[str], model: Optional[str],
) -> Dict[str, Any]:
    """Sync form of :func:`hold_peer_messages` for messages that were held
    before any still in the store, so they go first. No stop check: the
    caller is putting back text it already took."""
    row, replaced = wakeups_db.merge_session_wakeup(
        **_held_merge_kwargs(
            session_id, prompt, delay_seconds, prepend=True,
            profile_id=profile_id, project_id=project_id, model=model,
        ),
    )
    _disarm(replaced)
    arm_persisted(row)
    return row


async def schedule(
    *,
    session_id: str,
    profile_id: Optional[str],
    project_id: Optional[str],
    prompt: str,
    delay_seconds: float,
    reason: Optional[str] = None,
    model: Optional[str] = None,
    source: str = "schedule_wakeup",
) -> Dict[str, Any]:
    """Persist a wakeup and arm its in-process timer. Returns the persisted row.

    Durable-first: the row is written BEFORE the timer is armed, so a crash in the
    window loses nothing (boot rehydrate re-arms it). ``fire_at`` is an absolute
    unix epoch so rehydrate can compute the remaining delay.

    ``model`` is the scheduling turn's effective model, persisted so the wake
    replays it as a query-level override instead of running on the profile
    default.

    Scheduling REPLACES: any outstanding wake for the same
    ``(session_id, source)`` is deleted and disarmed first, so a producer that
    re-arms on every turn leaves one pending wake, not a growing pile that all
    come due together.
    """
    if source != "schedule_wakeup" and _session_user_stopped(session_id):
        # No SERVER-originated producer arms a wake on a session the user
        # stopped — the idle watchdog above all, which exists precisely to nudge
        # quiet sessions back to work. Any outstanding row for the source is
        # still dropped, so this never leaves a stale wake behind.
        # ``schedule_wakeup`` (the model's own tool, which returns the row to a
        # caller that reads its id) is deliberately not refused here: it can
        # only run inside a turn, and :func:`_fire` drops the wake anyway if the
        # stop still stands when it comes due.
        dropped = await cancel_session_wakeups_async(session_id, source)
        logger.info(
            "wakeup schedule refused for stopped session %s (source=%s, "
            "dropped %d outstanding)",
            session_id, source, dropped,
        )
        return {}
    replaced = await cancel_session_wakeups_async(session_id, source)
    if replaced:
        logger.info(
            "wakeup schedule: replaced %d outstanding %s wake(s) for session %s",
            replaced, source, session_id,
        )
    fire_at = _now_epoch() + max(0.0, float(delay_seconds))
    row = await asyncio.to_thread(
        wakeups_db.create_wakeup,
        id=f"wake-{uuid.uuid4().hex}",
        session_id=session_id,
        prompt=prompt,
        fire_at=fire_at,
        profile_id=profile_id,
        project_id=project_id,
        reason=reason,
        model=model,
        source=source,
        instance_id=settings.effective_instance_id,
    )
    _arm(row, max(0.0, float(delay_seconds)))
    logger.info(
        "wakeup scheduled: id=%s session=%s source=%s fire_in=%.0fs",
        row["id"], session_id, source, max(0.0, float(delay_seconds)),
    )
    return row


async def rehydrate_pending() -> int:
    """Re-arm this install's persisted wakeups on boot. Returns the count.

    Overdue wakes (``fire_at`` in the past — the common restart case) are armed
    with a small staggered delay rather than firing simultaneously.
    """
    try:
        rows = await asyncio.to_thread(
            wakeups_db.get_pending_wakeups, settings.effective_instance_id
        )
    except Exception:  # noqa: BLE001 — rehydrate must never block boot.
        logger.exception("wakeup rehydrate failed to read pending rows")
        return 0

    now = _now_epoch()
    overdue = 0
    for i, row in enumerate(rows):
        try:
            fire_at = float(row.get("fire_at") or 0.0)
        except (TypeError, ValueError):
            fire_at = 0.0
        remaining = fire_at - now
        if remaining <= 0:
            remaining = min(
                _REHYDRATE_STAGGER_SECONDS * overdue, _REHYDRATE_STAGGER_CAP_SECONDS
            )
            overdue += 1
        _arm(row, remaining)
    if rows:
        logger.info(
            "wakeup rehydrate: re-armed %d pending wakeup(s) (%d overdue) for "
            "install %s", len(rows), overdue, settings.effective_instance_id,
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Idle-at-edge watchdog
# ---------------------------------------------------------------------------

def _watchdog_eligible(session_id: str, frac: float, now_monotonic: float) -> bool:
    """True if ``session_id`` may be nudged again given the latched state.

    A previously-nudged session is only eligible once BOTH: the cooldown has
    elapsed, AND the current fraction has dropped at least
    :data:`_WATCHDOG_DECREASE_EPSILON` below the fraction latched at the last
    nudge (compaction visibly landed). A muted session (lifetime cap reached)
    is never eligible again.
    """
    latch = _watchdog_cooldown.get(session_id)
    if latch is None:
        return True
    if latch.muted:
        return False
    if (now_monotonic - latch.monotonic_ts) < settings.idle_watchdog_cooldown_seconds:
        return False
    if frac > latch.fraction - _WATCHDOG_DECREASE_EPSILON:
        logger.info(
            "idle-watchdog: session %s still at %.1f%% after nudge (no decrease "
            "observed) — holding",
            session_id, frac * 100.0,
        )
        return False
    return True


async def scan_once() -> List[str]:
    """One idle-at-edge sweep. Returns the session ids nudged this pass.

    For each structurally-eligible candidate (idle, active, snapshot present, no
    live turn, no pending wakeup — :func:`db.wakeups.idle_edge_candidates`) that
    has autocompaction enabled, is over the configured context fraction, off
    cooldown, has visibly dropped fraction since its last nudge (or was never
    nudged), and has not hit its lifetime nudge cap, arm ONE synthetic nudge
    through the durable wakeup machinery.
    """
    from app.core import compact_continue

    nudged: List[str] = []
    try:
        candidates = await asyncio.to_thread(
            wakeups_db.idle_edge_candidates,
            settings.effective_instance_id,
            "active",
            settings.idle_watchdog_max_idle_seconds,
        )
    except Exception:  # noqa: BLE001 — the loop must survive a bad scan.
        logger.exception("idle-watchdog: candidate scan failed")
        return nudged

    now_monotonic = time.monotonic()
    fraction_gate = settings.idle_watchdog_context_fraction
    for cand in candidates:
        session_id = cand.get("id")
        if not session_id:
            continue
        usage = compact_continue._persisted_usage(session_id)
        # No autocompaction → no compaction edge to nudge past: fluid's native
        # legs trim instead of autocompacting and always report False. Mirrors
        # the same guard in compact_continue.usage_over_threshold.
        if usage is not None and usage.get("isAutoCompactEnabled") is False:
            continue
        frac = compact_continue.usage_fraction(usage)
        if frac is None or frac < fraction_gate:
            continue
        if not _watchdog_eligible(session_id, frac, now_monotonic):
            continue
        logger.info(
            "idle-watchdog: session %s idle at %.1f%% context (>= %.0f%%) — "
            "arming synthetic nudge",
            session_id, frac * 100.0, fraction_gate * 100.0,
        )
        try:
            await schedule(
                session_id=session_id,
                profile_id=cand.get("profile_id"),
                project_id=cand.get("project_id"),
                prompt=WATCHDOG_NUDGE_PROMPT,
                delay_seconds=0.0,
                reason="idle-at-edge watchdog (#598)",
                source="idle_watchdog",
            )
        except Exception:  # noqa: BLE001 — one bad arm must not abort the sweep.
            logger.exception(
                "idle-watchdog: failed to arm nudge for session %s", session_id
            )
            continue
        prev_count = _watchdog_cooldown.get(session_id)
        count = (prev_count.count if prev_count else 0) + 1
        muted = count >= _WATCHDOG_LIFETIME_CAP
        _watchdog_cooldown[session_id] = _WatchdogLatch(
            monotonic_ts=now_monotonic, fraction=frac, count=count, muted=muted,
        )
        if muted:
            logger.info(
                "idle-watchdog: session %s reached lifetime nudge cap (%d) — "
                "muting",
                session_id, _WATCHDOG_LIFETIME_CAP,
            )
        nudged.append(session_id)
    return nudged


async def watchdog_loop() -> None:
    """Periodic idle-at-edge sweep (started by ``startup.start_background_tasks``)."""
    interval = max(30, int(settings.idle_watchdog_interval_seconds))
    # Let boot settle (and rehydrate finish) before the first sweep.
    await asyncio.sleep(min(30, interval))
    while True:
        try:
            nudged = await scan_once()
            if nudged:
                logger.info(
                    "idle-watchdog: armed %d nudge(s) this sweep", len(nudged)
                )
        except asyncio.CancelledError:
            break
        except Exception:  # noqa: BLE001
            logger.exception("idle-watchdog sweep failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
