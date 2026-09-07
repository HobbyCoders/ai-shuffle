"""Task-tool dispatch — the unified agent-orchestration surface (rulings A6-A10).

The ``Task`` tool (fluid builtin on native legs, ``mcp__builtins__Task`` on the
delegated leg) resolves its target against the unified ``agents`` table and
runs it as a worker session via :func:`app.core.provider_agent.dispatch_worker`
— the proven ProviderAgent machinery (real session rows, approval routing,
supervisor teardown, progress feed, bounded report).

What this module adds on top of raw ``dispatch_worker``:

* DB target resolution happens only inside the current provider's configured
  subagent roster; ``agent_invoke_mode`` and ``enabled_agents`` control that
  roster as well as the prompt index;
* the caller provider plus the target's model assignment on that provider are
  authoritative; a Task call cannot switch providers or choose a model;
* nesting depth (A9) — max ``settings.task_tool_max_depth`` (5) levels of
  agent-invokes-agent, counted by walking worker-created ancestor sessions;
* cycle detection (A9) — an agent already active in the ancestor chain cannot
  be dispatched again beneath itself;
* worker budget (A9) — at most ``settings.task_tool_max_workers`` live
  workers per root session tree. NESTED workers count against it: the budget
  is about what is running on this box under one human's session, and a
  worker's worker is still a worker. (What nested dispatch no longer spends on
  the root is its NOTIFICATION lanes — see "root routing" below.);
* optional provider-row admission — an administrator can set
  ``settings.task_worker_max_per_provider`` to queue excess workers. This is
  a Shuffle concurrency policy, not a provider token quota. It is disabled by
  default; actual rate-limit feedback and resource guards still apply;
* worker liveness — a worker that never streams a frame has not started, and
  is failed at ``settings.task_worker_first_frame_seconds`` with the existing
  "worker failed to start" summary rather than after fifteen silent minutes.
  A zero-frame failure is then re-dispatched automatically (transport: once;
  provider capacity: with backoff), each attempt its own visible task row.
  A WORKING worker stays unbounded; a SYNCHRONOUS one additionally gets the
  hard cap ``settings.task_worker_sync_max_seconds``, because it is holding
  the parent's turn while it waits;
* pool admission — the worker budget is a per-tree ceiling, not a statement
  about the box; a dispatch also has to fit on the box. That is checked HERE,
  before anything is returned: a dispatch at a ceiling waits briefly and is
  then refused with ``ok=False`` (see :func:`_reserve_admission`), because a
  background receipt is a promise to wake the parent and a worker that never
  started keeps no promises. Which ceiling applies depends on the worker's
  lane: a DELEGATED worker owns a CLI subprocess and is bounded by the session
  pool's subprocess cap, while a NATIVE (in-process) worker is bounded by the
  real resource guards plus ``settings.subagent_max_concurrent_per_session``,
  the per-parent fan-out limit;
* root routing (A9) — approvals, progress cards, and supervisor registration
  land on the ROOT session (the one with a human watching), while
  project/worktree/cwd inheritance still reads the direct caller. The
  NOTIFICATION lanes are the exception (2026-09-05 audit): the terminal chip,
  the scheduler wake and the task-notification steer are armed on the
  DISPATCHING session, not the root — a root that never called Task cannot act
  on the report, and while it was spending turns trying, the worker that did
  call Task ended prematurely waiting for a wake that went elsewhere. See
  ``provider_agent.dispatch_worker``'s "OWNERSHIP vs ROUTING" note;
* modes (A8) — ``sync`` blocks, writes the worker's full report to a file
  under the parent's ``.fluid/task-reports/`` and returns ``report_path`` +
  ``report_chars`` + a bounded ``report_head`` so the parent reads the rest
  selectively instead of paying for it in context up front;
  ``background`` pre-allocates and returns the exact worker session id as a
  Fluid ``BackgroundTaskReceipt``, then announces completion to the parent
  without a polling loop — and writes its report to the SAME directory
  (:func:`write_task_report`, called from ``provider_agent``'s completion
  path), so ``TaskGet`` can hand the woken parent a head plus a path instead
  of leaving it to guess. A call that names no mode takes the target's own
  default: Explore-class agents (ones whose declared tool surface cannot write
  files) go ``background``, since a research child's parent has nothing to wait
  for and blocking it turns a fan-out into a queue; everything else stays
  ``sync``. For an Explore-class target the default is not a suggestion — an
  explicit ``mode`` in the call is IGNORED and the dispatch still goes
  ``background`` (the round-2 audit found every model defeating the default by
  passing ``mode="sync"``, and a user Stop then destroyed a finished sync
  child's research along with its parent). ``build_task_contract`` drops
  ``mode`` from the tool schema entirely when every reachable target is
  Explore-class, so on those rosters the key cannot be sent at all; this
  runtime rule covers the rest — replayed transcripts, retries, and mixed
  rosters. Everywhere else an explicit ``mode`` still wins.

File-defined agents (Claude-ecosystem compat, ruling CE6)
---------------------------------------------------------
On a native (fluid) leg the engine reads the user's ``.claude/agents/**/*.md``
definitions — project, global, and namespaced ``<plugin>:<agent>`` ones — and
publishes them on ``ctx.extra["agent_registry"]``. fluid never spawns anything;
this module is what makes those names dispatchable, as a FALLBACK behind the
app's own ``agents`` table (host wins a name collision, matching fluid's own
doctrine and its registry-seam precedence).

The DELEGATED (claude CLI) leg runs no fluid engine, so nothing publishes that
registry there. Its bridged ``mcp__builtins__Task`` runs the same discovery
itself from the session's cwd (``app.mcp.builtin_tools._discover_agent_registry``)
and passes the result to ``agent_registry`` below — the two legs therefore
resolve the same names, which is what the adapter's shared agents index
promises the model on both.

A file agent has no ``agents`` row, so it borrows the caller's profile,
effective provider, and selected model. On a provider-bound dispatch the
borrowed model is resolved as parent turn's model → caller profile's model for
that provider → provider ``default_model``, and the dispatch is refused when
none of those is curated (never the registry's first entry). Its markdown body
rides the worker's system prompt as an override block. Consequences,
deliberate:

* ``tools:`` in the frontmatter is ENFORCED (ruling CE12), not advisory: the
  list rides the worker's turn as a ``tool_allowlist`` override and becomes
  ``NativeLoop(allowed_tools=...)`` on the native leg — a filtered tool's schema
  never reaches the model, ToolSearch will not load it, and a call naming it
  comes back as an in-context error. An empty/absent list inherits the caller's
  full toolset. On the DELEGATED (claude CLI) leg the same list is passed as
  ``--allowed-tools``, which the CLI reads as permission rules rather than a
  surface restriction — see :func:`app.core.provider.harness_adapter` for the
  exact per-leg semantics;
* ``model:`` metadata cannot move the worker to another provider; the live
  caller binding remains authoritative;
* the profile-id cycle check does not apply (the worker runs on the caller's own
  profile by construction); depth and worker budget both do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.session_pool import DELEGATED_LANE, NATIVE_LANE

if TYPE_CHECKING:  # pragma: no cover — annotation-only; every DB import here is
    # deliberately lazy (see the function bodies), so the row types ride the
    # string annotations ``from __future__ import annotations`` already gives us.
    from app.db.profiles import ProfileRow
    from app.db.sessions import SessionRow

logger = logging.getLogger(__name__)

#: Hard ceiling on parent-chain walks — above max depth anyway.
_CHAIN_LIMIT = 12

#: Background dispatch tasks, kept referenced so asyncio never GCs them.
_BACKGROUND_TASKS: set = set()

#: How much of a sync worker's report rides the tool result itself. The whole
#: report always lands in the report file; this head is the "did it work and
#: roughly what happened" glance that decides whether to read further.
SYNC_REPORT_HEAD_CHARS = 2_000

#: Report files live under ``<parent cwd>/.fluid/task-reports/`` — the same
#: ``.fluid`` infrastructure dir a checkout already carries for worktrees, and
#: inside the tree the parent's own file tools already reach.
_REPORT_DIR_PARTS = (".fluid", "task-reports")

# ``None`` is a real provider binding (native Claude), so callers that know the
# effective turn provider must be able to pass it distinctly from callers that
# omitted provider context and need the session-row compatibility fallback.
_PROVIDER_UNSET = object()
_EFFORT_UNSET = object()

# ── session-pool admission, checked BEFORE any receipt is returned ───────────
# A worker is a new pooled session, so it is subject to
# ``SessionPool.admission_check`` — but that check used to run deep inside the
# worker's own turn, seconds AFTER this module had already handed the model a
# background receipt saying "end the turn, you will be woken". The 2026-09-04
# audit caught the result: seven receipts, then seven "worker failed to start:
# session pool at hard cap (8/8)" frames, then a re-dispatch that failed the
# same way. Admission is now decided here, synchronously, and a dispatch that
# cannot be admitted is refused outright rather than receipted.
#
# WHICH CEILING applies depends on the worker's LANE (the same 2026-09-04
# audit, second finding). The pool's hard cap counts node CLI subprocesses; a
# DELEGATED worker is one, so it is gated by the cap exactly as before. A
# NATIVE worker (openai_responses / anthropic / openrouter) is coroutines in
# this process — it was being refused by a subprocess cap it could never
# contribute to, which is how two gpt-6-astra fan-outs lost seven dispatches to
# "8/8". Native workers are gated instead by what actually costs something: the
# pool's host-memory / descendant-process / tree-RSS guards, and the per-parent
# fan-out limit below.

#: How long a dispatch waits for a pool slot before refusing. Covers the common
#: case — a fan-out that is only transiently over the cap because a sibling is
#: about to settle — without turning a full box into an unbounded stall.
_ADMISSION_WAIT_SECONDS = 90.0

#: Re-check cadence while waiting for a slot.
_ADMISSION_POLL_SECONDS = 0.5

#: How long an admitted-but-not-yet-connected dispatch keeps counting against
#: the pool. A worker occupies its slot only once its turn connects an adapter,
#: which is seconds to minutes after admission; without this reservation window
#: every waiter would see the same free slot and all pile into it (the exact
#: check-then-act race that overshot the cap). Self-limiting: a reservation is
#: dropped as soon as its session is really pooled, and expires regardless.
_ADMISSION_RESERVATION_SECONDS = 180.0

# ── optional provider-row admission ────────────────────────────────────────
# A positive ``settings.task_worker_max_per_provider`` is an administrator's
# concurrency policy, not a measurement of the provider's token quota. It is
# disabled by default. Actual 429 responses retain their existing retry/backoff
# path; host memory, session and subprocess guards remain active independently.
# The provider-less delegated Claude leg has no provider-row limit here.


@dataclass(frozen=True)
class _Reservation:
    """One admitted-but-not-yet-connected dispatch.

    ``lane`` decides which ceiling this reservation counts against (only a
    delegated one holds a subprocess slot); ``parent`` is the session whose
    fan-out budget it is spending; ``provider`` is the row whose optional
    concurrency policy applies (``None`` on the provider-less Claude leg).
    """

    expires: float
    lane: str = DELEGATED_LANE
    parent: Optional[str] = None
    provider: Optional[str] = None
    root: Optional[str] = None


#: worker session id -> its admission reservation.
_PENDING_ADMISSIONS: Dict[str, _Reservation] = {}

#: (loop, lock) for the admission critical section. Rebuilt when the running
#: loop differs, because an ``asyncio.Lock`` belongs to the loop that awaited
#: it and this module is imported once across many test loops.
_ADMISSION_LOCK: Optional[Tuple[Any, asyncio.Lock]] = None


# ---------------------------------------------------------------------------
# Chain / depth helpers
# ---------------------------------------------------------------------------

def _session_row(session_id: str) -> Optional[SessionRow]:
    try:
        from app.db import database

        return database.get_session(session_id)
    except Exception:  # noqa: BLE001
        logger.debug("task_dispatch: session read failed for %s", session_id, exc_info=True)
        return None


def _first_turn_source(session_id: str) -> Optional[str]:
    """The ``source`` of a session's first turn — worker sessions are born
    with a ``provider_agent`` turn; forks/user sessions are not."""
    try:
        from app.db.base import get_db

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT source FROM turns WHERE session_id = %s "
                "ORDER BY created_at ASC LIMIT 1",
                (session_id,),
            )
            row = cursor.fetchone()
            return row["source"] if row else None
    except Exception:  # noqa: BLE001
        return None


def _is_worker_session(session_id: str) -> bool:
    from app.core import provider_agent

    if provider_agent.is_worker_session(session_id):
        return True
    return _first_turn_source(session_id) == provider_agent.WORKER_TURN_SOURCE


def caller_chain(session_id: str) -> Tuple[List[SessionRow], str, int]:
    """Walk the parent chain upward from ``session_id``.

    Returns ``(worker_ancestor_rows, root_session_id, depth)`` where ``depth``
    is the number of worker sessions in the chain INCLUDING ``session_id``
    itself (0 for a top-level human session), and ``root_session_id`` is the
    top non-worker session (falls back to the highest reachable ancestor).
    """
    rows: List[SessionRow] = []
    depth = 0
    current_id = session_id
    root_id = session_id
    for _ in range(_CHAIN_LIMIT):
        row = _session_row(current_id)
        if row is None:
            break
        if current_id != session_id:
            rows.append(row)
        if _is_worker_session(current_id):
            depth += 1
            parent = row.get("parent_session_id")
            if parent:
                root_id = parent
                current_id = parent
                continue
            break
        # First non-worker session is the root — stop walking (a fork's
        # parent link above a human session is lineage, not nesting).
        root_id = current_id
        break
    return rows, root_id, depth


def _chain_agent_ids(session_id: str) -> List[str]:
    """Profile (agent) ids active in the caller chain, caller first."""
    ids: List[str] = []
    current_id: Optional[str] = session_id
    for _ in range(_CHAIN_LIMIT):
        if not current_id:
            break
        row = _session_row(current_id)
        if row is None:
            break
        pid = row.get("profile_id")
        if pid:
            ids.append(pid)
        if not _is_worker_session(current_id):
            break
        current_id = row.get("parent_session_id")
    return ids


def _live_worker_count_for_root(root_session_id: str) -> int:
    """Live Task/ProviderAgent workers anywhere under ``root_session_id``."""
    from app.core import provider_agent_supervisor as sup

    count = 0
    live = getattr(sup, "_LIVE", {})
    for parent_id, bucket in list(live.items()):
        _, root, _ = caller_chain(parent_id)
        if root == root_session_id or parent_id == root_session_id:
            count += len(bucket)
    return count


# ---------------------------------------------------------------------------
# Session-pool admission
# ---------------------------------------------------------------------------

def _admission_lock() -> asyncio.Lock:
    """The admission critical section's lock, bound to the running loop."""
    global _ADMISSION_LOCK

    loop = asyncio.get_running_loop()
    if _ADMISSION_LOCK is None or _ADMISSION_LOCK[0] is not loop:
        _ADMISSION_LOCK = (loop, asyncio.Lock())
    return _ADMISSION_LOCK[1]


def _pending_admission_count(
    *,
    lane: Optional[str] = None,
    parent: Optional[str] = None,
    provider: Optional[str] = None,
    root: Optional[str] = None,
) -> int:
    """Dispatches admitted here that the pool has not counted yet.

    Prunes as it counts: a reservation is dropped once its session is really in
    the pool (the pool's own number now covers it) or once it has aged out (the
    dispatch died before connecting, or is wedged — either way it must stop
    holding a slot hostage).

    ``lane``, ``parent`` and ``provider`` narrow the count to the reservations
    that gate the ceiling being checked: the subprocess cap only sees delegated
    reservations, a parent's fan-out budget only sees its own, and a provider
    row's optional concurrency limit only sees dispatches bound to that row.
    """
    if not _PENDING_ADMISSIONS:
        return 0
    from app.core import session_pool as pool_module

    pool = getattr(pool_module, "_session_pool", None)
    now = time.monotonic()
    for key, reservation in list(_PENDING_ADMISSIONS.items()):
        if reservation.expires <= now:
            _PENDING_ADMISSIONS.pop(key, None)
            continue
        try:
            if pool is not None and pool.peek(key) is not None:
                _PENDING_ADMISSIONS.pop(key, None)
        except Exception:  # noqa: BLE001 — a bookkeeping read, never fatal
            continue
    return sum(
        1
        for reservation in _PENDING_ADMISSIONS.values()
        if (lane is None or reservation.lane == lane)
        and (parent is None or reservation.parent == parent)
        and (provider is None or reservation.provider == provider)
        and (root is None or (reservation.root or reservation.parent) == root)
    )


def _live_worker_count_for_parent(parent_session_id: Optional[str]) -> int:
    """Live workers registered under ``parent_session_id``.

    The supervisor keys its live set by the parent id a dispatch registered
    under (:mod:`app.core.provider_agent_supervisor`), which is what a fan-out
    budget is about: how many workers THIS parent has running right now.
    """
    if not parent_session_id:
        return 0
    from app.core import provider_agent_supervisor as sup

    try:
        return len(getattr(sup, "_LIVE", {}).get(parent_session_id, ()) or ())
    except Exception:  # noqa: BLE001 — a bookkeeping read, never fatal
        return 0


def _live_worker_count_for_provider(provider_id: Optional[str]) -> int:
    """Live workers anywhere on this box bound to ``provider_id``.

    The optional row policy is process-wide rather than per-session, so two
    unrelated sessions sharing this configured provider also share the limit.
    It does not measure provider quotas, which can span rows or installations.
    """
    if not provider_id:
        return 0
    from app.core import provider_agent_supervisor as sup

    count = 0
    try:
        for bucket in list(getattr(sup, "_LIVE", {}).values()):
            for record in list(bucket.values()):
                if (getattr(record, "provider_id", "") or None) == provider_id:
                    count += 1
    except Exception:  # noqa: BLE001 — a bookkeeping read, never fatal
        logger.debug(
            "task_dispatch: provider worker count failed for %s",
            provider_id, exc_info=True,
        )
        return 0
    return count


def _provider_worker_limit() -> int:
    """Administrator's worker limit per provider row (0 = disabled)."""
    try:
        from app.core.config import settings

        return int(getattr(settings, "task_worker_max_per_provider", 0) or 0)
    except Exception:  # noqa: BLE001 — a missing setting must not gate dispatch
        return 0


def _sync_cap_seconds() -> float:
    """Hard wall-clock cap for a synchronous Task, across attempts (0 = off)."""
    try:
        from app.core.config import settings

        return max(
            0.0, float(getattr(settings, "task_worker_sync_max_seconds", 0.0) or 0.0)
        )
    except Exception:  # noqa: BLE001
        return 0.0


def _provider_queue_seconds() -> float:
    """How long a dispatch queues on a configured provider-row limit."""
    try:
        from app.core.config import settings

        return max(
            0.0,
            float(getattr(settings, "task_worker_provider_queue_seconds", 0.0) or 0.0),
        )
    except Exception:  # noqa: BLE001
        return 0.0


def _worker_lane(provider_id: Optional[str], model_id: Optional[str]) -> str:
    """Which lane a worker with this provider/model binding will run on.

    Defers to the harness adapter's single leg-decision table rather than
    re-deriving it (a second copy would drift the moment the provider table
    changes), by projecting the binding onto the same ``aih_*`` fields
    ``provider_kind_for_sdk_options`` reads. Anything unknowable degrades to
    ``delegated``: the conservative answer, since that is the lane with the
    subprocess cap.
    """
    try:
        from app.core.provider.harness_adapter import provider_kind_for_sdk_options

        kind = provider_kind_for_sdk_options(
            SimpleNamespace(model=model_id, aih_provider_id=provider_id)
        )
    except Exception:  # noqa: BLE001 — never fail a dispatch over a lane guess
        logger.debug("task_dispatch: could not decide worker lane", exc_info=True)
        return DELEGATED_LANE
    return DELEGATED_LANE if kind in (None, DELEGATED_LANE) else NATIVE_LANE


def _release_admission(worker_session_id: str) -> None:
    """Drop a reservation (the dispatch settled, or never started)."""
    _PENDING_ADMISSIONS.pop(worker_session_id, None)


def _fanout_limit() -> int:
    """Per-parent concurrent-worker limit (0 = disabled)."""
    try:
        from app.core.config import settings

        return int(getattr(settings, "subagent_max_concurrent_per_session", 0) or 0)
    except Exception:  # noqa: BLE001 — a missing setting must not gate dispatch
        return 0


def _root_worker_limit() -> int:
    """The shared budget for a root session and all descendant workers."""
    from app.core.config import settings

    return int(getattr(settings, "task_tool_max_workers", 64) or 64)


async def _reserve_admission(
    worker_session_id: str,
    *,
    lane: str = DELEGATED_LANE,
    parent_session_id: Optional[str] = None,
    context_session_id: Optional[str] = None,
    provider_id: Optional[str] = None,
    wait_seconds: Optional[float] = None,
) -> Tuple[Optional[str], float]:
    """Hold a slot for ``worker_session_id``, waiting if needed.

    Returns ``(None, waited_seconds)`` once the slot is reserved, or
    ``(refusal, waited_seconds)`` if the box is still at a ceiling when the
    wait runs out. The wait is what makes a fan-out behave: dispatches queue on
    the lock in arrival order and take slots as siblings settle, so a transient
    cap costs seconds instead of a false receipt. ``waited_seconds`` is how long
    this dispatch spent queued — the caller reports a non-zero value to the
    model so a slow dispatch reads as "queued", not as a mystery.

    Which ceilings apply depends on ``lane``:

    * DELEGATED — the pool's subprocess hard cap (plus this module's own
      reservations for delegated workers, which the pool cannot see yet), and
      every resource guard in ``admission_check``.
    * NATIVE — no subprocess cap; the resource guards (host memory, descendant
      processes, tree RSS, native soft cap) and the per-parent fan-out limit.

    The per-parent limit applies to BOTH lanes, as does an explicitly configured
    provider-row limit. These are Shuffle policies, not provider token quotas.
    A dispatch held only by the optional provider-row ceiling gets the longer
    ``task_worker_provider_queue_seconds`` budget to let siblings finish.

    A refusal is deliberately a REAL error rather than a queued receipt. A
    receipt is a promise the parent will be woken, and nothing outside this
    process would keep that promise for a dispatch that never started — the
    registry row, the park gate and the completion wake are all armed inside
    ``provider_agent.dispatch_worker``, i.e. only once the worker really runs.
    """
    from app.core import session_pool as pool_module

    started = time.monotonic()
    budget = _ADMISSION_WAIT_SECONDS if wait_seconds is None else wait_seconds
    budget = max(0.0, float(budget))
    fanout_limit = _fanout_limit()
    root_session_id = (
        (await asyncio.to_thread(caller_chain, parent_session_id))[1]
        if parent_session_id else None
    )
    root_limit = _root_worker_limit() if root_session_id else 0
    provider_limit = _provider_worker_limit() if provider_id else 0
    # An explicitly configured provider-row limit gets its own queue budget.
    provider_budget = max(budget, _provider_queue_seconds()) if provider_limit else budget
    lock = _admission_lock()
    waited = False
    from fluid.swarm import current_launch

    launch = current_launch.get()

    def cancelled() -> bool:
        return bool(
            launch is not None
            # The actual caller can be a nested worker. Supervisor/root budget
            # identity is shared by its siblings and grants no cancellation
            # authority over their separate Swarms.
            and launch.coordinator.session_id == (context_session_id or parent_session_id)
            and (
                launch.coordinator.closed
                or launch.swarm_id in launch.coordinator.cancelled
            )
        )

    while True:
        async with lock:
            # Swarm Stop latches cancellation without discarding an atomic
            # spawn acknowledgement. A worker still waiting for admission has
            # not spawned: settle it promptly without reserving a slot. Read
            # only the trusted host launch context, never model arguments.
            if cancelled():
                return (
                    "Swarm cancelled before admission; the worker was NOT started.",
                    time.monotonic() - started,
                )
            # Off the loop, like every other admission_check caller: the
            # ceiling checks can walk the process tree with psutil.
            snapshot = await asyncio.to_thread(pool_module.admission_snapshot, lane)
            # Stop may arrive during the asynchronous resource measurement.
            # There is no await between this guard and the reservation below.
            if cancelled():
                return (
                    "Swarm cancelled before admission; the worker was NOT started.",
                    time.monotonic() - started,
                )
            # Only DELEGATED reservations hold a subprocess slot.
            pending = _pending_admission_count(lane=DELEGATED_LANE)
            at_cap = (
                lane == DELEGATED_LANE
                and snapshot.hard_cap > 0
                and (snapshot.delegated_in_pool + pending) >= snapshot.hard_cap
            )
            siblings = 0
            over_fanout = False
            if fanout_limit > 0 and parent_session_id:
                siblings = _live_worker_count_for_parent(
                    parent_session_id
                ) + _pending_admission_count(parent=parent_session_id)
                over_fanout = siblings >= fanout_limit
            root_workers = 0
            over_root = False
            if root_limit > 0:
                root_workers = _live_worker_count_for_root(
                    root_session_id
                ) + _pending_admission_count(root=root_session_id)
                over_root = root_workers >= root_limit
            on_provider = 0
            over_provider = False
            if provider_limit > 0:
                on_provider = _live_worker_count_for_provider(
                    provider_id
                ) + _pending_admission_count(provider=provider_id)
                over_provider = on_provider >= provider_limit
            if (
                snapshot.refusal is None
                and not at_cap
                and not over_fanout
                and not over_root
                and not over_provider
            ):
                _PENDING_ADMISSIONS[worker_session_id] = _Reservation(
                    expires=time.monotonic() + _ADMISSION_RESERVATION_SECONDS,
                    lane=lane,
                    parent=parent_session_id,
                    provider=provider_id,
                    root=root_session_id,
                )
                queued_for = time.monotonic() - started
                if waited:
                    logger.info(
                        "task_dispatch: %s worker %s admitted after waiting "
                        "%.1fs (%d/%d subprocess sessions, %d reserved, %d "
                        "sibling worker(s), %d on provider %s)",
                        lane, worker_session_id, queued_for,
                        snapshot.delegated_in_pool, snapshot.hard_cap, pending,
                        siblings, on_provider, provider_id,
                    )
                return None, queued_for
            # The applicable deadline depends on WHICH ceiling is holding this
            # dispatch: a box ceiling gets the short budget, a provider-row one
            # the long queue budget.
            only_provider = (
                over_provider
                and snapshot.refusal is None
                and not at_cap
                and not over_fanout
                and not over_root
            )
            deadline_budget = provider_budget if only_provider else budget
            if snapshot.refusal is not None:
                reason = snapshot.refusal
            elif at_cap:
                reason = (
                    f"session pool at hard cap ({snapshot.delegated_in_pool}/"
                    f"{snapshot.hard_cap} subprocess sessions) with {pending} "
                    "worker(s) already admitted and starting"
                )
            elif over_fanout:
                reason = (
                    f"{siblings} worker(s) already running for this session "
                    f"(limit {fanout_limit})"
                )
            elif over_root:
                reason = (
                    f"{root_workers} worker(s) already running in this session tree "
                    f"(Shuffle's configured TASK_TOOL_MAX_WORKERS limit {root_limit})"
                )
            else:
                reason = (
                    f"{on_provider} worker(s) already running on this model "
                    f"provider (Shuffle's configured TASK_WORKER_MAX_PER_PROVIDER "
                    f"limit {provider_limit})"
                )
            if time.monotonic() - started >= deadline_budget:
                logger.warning(
                    "task_dispatch: refusing %s dispatch %s at capacity — %s",
                    lane, worker_session_id, reason,
                )
                return (
                    f"Server is at capacity — {reason}. The worker was NOT "
                    f"started (it queued {deadline_budget:.0f}s and nothing "
                    "freed). Wait for a running worker to finish before "
                    "dispatching another, or do this work yourself."
                ), time.monotonic() - started
        waited = True
        await asyncio.sleep(_ADMISSION_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Automatic re-dispatch (2026-09-05 audit, A + C)
# ---------------------------------------------------------------------------
# A worker that forwarded ZERO frames never started: it died at or before
# provider connect, so nothing it was asked to do has been done and re-running
# the same brief costs exactly one worker. The audit found ten such deaths in
# seven sessions — transport drops ("peer closed connection without sending
# complete message body") and org TPM refusals ("Please try again in 1.049s") —
# each reported ~900s late, each re-dispatched BY THE MODEL, by hand, from a
# woken turn. Two of them were dispatched three times and executed once.
#
# So the harness re-dispatches instead. Deliberately narrow:
#
# * only a ZERO-FRAME failure. A worker that streamed anything did real work,
#   and repeating real work is not free (nor idempotent — it may have edited
#   files);
# * only an ``error`` terminal. A ``killed`` one is a teardown or a Stop: the
#   answer is the kill, not a retry;
# * a transport start failure gets ONE retry (``task_worker_start_retries``);
# * a CAPACITY refusal gets up to ``task_worker_rate_limit_retries``
#   reschedules with backoff seeded from the provider's own retry-after, which
#   is what "reschedule, do not terminate" means for a 429;
# * a failure that will read identically on the tenth attempt — no credentials,
#   no such provider, an unresolvable model — is never retried.
#
# Each attempt is its own worker session and its own visible task row, labelled
# "· retry N", and only the attempt that SETTLES the dispatch spends a wake on
# the parent (see ``dispatch_worker(suppress_caller_wake=...)``).

#: Pause before re-dispatching a transport start failure. Short — the point is
#: to let a dropped connection's socket state clear, not to wait anything out.
_START_RETRY_BACKOFF_SECONDS = 2.0

#: Fallback backoff when a capacity refusal carries no parseable retry-after.
_RATE_RETRY_FALLBACK_SECONDS = 5.0

#: Start failures a retry cannot change. Matched case-insensitively against the
#: worker's error text; keep this list narrow — over-matching here silently
#: turns the watchdog back off.
_PERMANENT_START_FAILURES = (
    "could not create",
    "requires an explicit model",
    "no credentials configured",
    "needs to be reconnected",
    "no longer exists",
    "is disabled",
    "did not finish within the",
    "server is at capacity",
)


def _retry_kind(
    status: str,
    error_text: Optional[str],
    frames_forwarded: int,
    used: Dict[str, int],
    *,
    start_max: int,
    rate_max: int,
) -> Optional[str]:
    """``"rate"``, ``"start"`` or ``None`` — how (and whether) to re-dispatch.

    Pure, so ``dispatch_worker``'s in-``finally`` wake-suppression predicate and
    the caller's post-return decision cannot disagree: both read the same
    ``used`` counters, which only ever change between attempts.
    """
    from app.core import provider_agent

    if status != "error" or frames_forwarded:
        return None
    text = error_text or ""
    if provider_agent.looks_rate_limited(text):
        return "rate" if used["rate"] < rate_max else None
    lowered = text.lower()
    if any(marker in lowered for marker in _PERMANENT_START_FAILURES):
        return None
    return "start" if used["start"] < start_max else None


def _retry_backoff(kind: str, error_text: Optional[str], attempt: int, cap: float) -> float:
    """How long to wait before the next attempt of ``kind``.

    A capacity refusal starts from the provider's OWN retry-after — it knows
    when its window rolls over and we do not — doubled per attempt so a
    still-saturated org is not hammered, and clamped at ``cap``.
    """
    from app.core import provider_agent

    if kind != "rate":
        return _START_RETRY_BACKOFF_SECONDS
    base = provider_agent.rate_limit_retry_after(error_text)
    if base is None:
        base = _RATE_RETRY_FALLBACK_SECONDS
    return min(max(0.0, float(cap)), base * (2 ** max(0, attempt)) + 0.25)


def _capacity_worded(outcome, reschedules: int):
    """Re-word a final CAPACITY failure so it reads as capacity, not as a bug.

    The provider's own string — "Rate limit reached for gpt-5.6-luna in
    organization org-JTwK2… on tokens per min (TPM): Limit 500000, Used
    494784…" — tells a parent nothing it can act on, and the audited models
    responded to it by shrinking their own briefs ("MAX 3 tool calls to avoid
    provider TPM failure"). Say what actually happened instead, and keep the
    provider's text behind it.
    """
    from app.core import provider_agent
    from dataclasses import replace

    if outcome.ok or outcome.frames_forwarded:
        return outcome
    if not provider_agent.looks_rate_limited(outcome.error):
        return outcome
    tried = (
        f" It was rescheduled {reschedules} time(s) and the provider is still "
        "refusing."
        if reschedules
        else ""
    )
    return replace(
        outcome,
        error=(
            "Provider capacity: this model provider's shared token-per-minute "
            f"budget is saturated, so the worker was not started.{tried} Dispatch "
            "fewer workers on this provider at once, or wait a minute and "
            f"re-dispatch — the brief is fine. Provider said: {outcome.error}"
        ),
    )


async def _dispatch_worker_with_retries(
    *,
    kwargs: Dict[str, Any],
    background: bool,
    lane: str,
    provider_id: Optional[str],
    parent_session_id: Optional[str],
    dispatcher: Callable[..., Any],
    overall_deadline: Optional[float] = None,
):
    """Run one Task dispatch, re-dispatching a worker that never started.

    Returns the FINAL :class:`app.core.provider_agent.WorkerOutcome`. The first
    attempt's reservation is already held by the caller; every later attempt
    takes (and releases) its own.

    ``dispatcher`` is ``provider_agent.dispatch_worker``, resolved by the CALLER
    rather than looked up here: a background dispatch runs detached, and binding
    the callable at dispatch time keeps every attempt on the same
    implementation the call was made against.

    ``overall_deadline`` is a ``time.monotonic()`` ceiling for the whole
    sequence — the synchronous lane's hard cap, which must bound the retries
    too or a "capacity, retrying" loop becomes exactly the unbounded block it
    was added to prevent.
    """
    from app.core import provider_agent
    from app.core.config import settings

    start_max = (
        int(getattr(settings, "task_worker_start_retries", 0) or 0)
        if background
        # A synchronous caller is LIVE: it can read the failure, decide, and
        # re-dispatch in the background (which is what the audited model did,
        # correctly, within one turn). Silently re-blocking its turn for
        # another attempt is the opposite of what B asks for.
        else 0
    )
    rate_max = int(getattr(settings, "task_worker_rate_limit_retries", 0) or 0)
    backoff_cap = float(
        getattr(settings, "task_worker_rate_limit_backoff_cap_seconds", 60.0) or 60.0
    )
    used: Dict[str, int] = {"start": 0, "rate": 0}
    base_label = kwargs.get("label")
    attempt_kwargs = dict(kwargs)
    outcome = None

    while True:
        worker_id = attempt_kwargs["worker_session_id"]
        if overall_deadline is not None:
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                # Only reachable when the cap is already spent (a pathological
                # setting, or a retry loop that used all of it). Never return
                # ``None`` to a caller that is about to read ``.ok``.
                return outcome or provider_agent.WorkerOutcome(
                    ok=False,
                    session_id=worker_id,
                    error=(
                        "The synchronous Task budget was exhausted before the "
                        "worker could be started."
                    ),
                )
            attempt_kwargs["max_wall_seconds"] = remaining

        def _will_retry(
            status: str, preview: Optional[str], frames: int
        ) -> bool:
            return _retry_kind(
                status, preview, frames, used,
                start_max=start_max, rate_max=rate_max,
            ) is not None

        try:
            from app.core.swarm import bind_worker
            await bind_worker(worker_id, attempt_kwargs.get("context_session_id"))
            outcome = await dispatcher(
                background=background,
                suppress_caller_wake=_will_retry if background else None,
                **attempt_kwargs,
            )
        finally:
            _release_admission(worker_id)

        status = (
            "completed" if outcome.ok
            else ("killed" if outcome.killed else "error")
        )
        kind = _retry_kind(
            status, outcome.error, outcome.frames_forwarded, used,
            start_max=start_max, rate_max=rate_max,
        )
        if kind is None:
            return _capacity_worded(outcome, used["rate"])

        delay = _retry_backoff(kind, outcome.error, used[kind], backoff_cap)
        used[kind] += 1
        if overall_deadline is not None and (
            time.monotonic() + delay >= overall_deadline
        ):
            return outcome
        logger.warning(
            "task_dispatch: worker %s never started (%s) — re-dispatching the "
            "same brief in %.1fs (attempt %d)",
            worker_id, kind, delay, used["start"] + used["rate"] + 1,
        )
        await asyncio.sleep(delay)

        retry_worker_id = str(uuid.uuid4())
        attempt_kwargs = dict(attempt_kwargs)
        attempt_kwargs["worker_session_id"] = retry_worker_id
        if kind == "start":
            # The first-frame watchdog cannot tell a dead provider connection
            # from a reasoning model whose first token is simply late — the
            # worker's event log is empty in both cases. So the RETRY gets twice
            # the leash: a genuinely slow model survives it (having cost one
            # wasted start), a connection that never opens fails both and is
            # reported, which is the whole point of the deadline.
            leash = attempt_kwargs.get("first_frame_seconds")
            if leash is None:
                leash = provider_agent._worker_deadlines(background, None, None)[0]
            if leash:
                attempt_kwargs["first_frame_seconds"] = leash * 2
        attempt_kwargs["label"] = (
            f"{base_label} · retry {used['start'] + used['rate']}"
            if base_label
            else f"Task worker · retry {used['start'] + used['rate']}"
        )
        # A fresh card for a fresh worker: reusing the model's launch tool_use
        # id would have the reducer absorb two workers into one placeholder.
        attempt_kwargs["launch_tool_use_id"] = None
        capacity_error, _queued = await _reserve_admission(
            retry_worker_id,
            lane=lane,
            parent_session_id=parent_session_id,
            context_session_id=kwargs.get("context_session_id"),
            provider_id=provider_id,
        )
        if capacity_error:
            error = (
                f"{outcome.error or 'The worker failed to start.'} The "
                f"automatic re-dispatch could not be admitted: {capacity_error}"
            )
            if background:
                # The failed attempt's wake was suppressed on the promise of
                # this retry. The retry is not happening, so somebody has to
                # tell the parent — otherwise it stays parked on a receipt
                # nothing will ever settle.
                await provider_agent.announce_abandoned_retry(
                    kwargs.get("context_session_id") or parent_session_id or "",
                    worker_id,
                    error[:240],
                )
            return provider_agent.WorkerOutcome(
                ok=False,
                session_id=outcome.session_id,
                error=error,
                frames_forwarded=outcome.frames_forwarded,
            )


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def resolve_target_agent(ref: str) -> Tuple[Optional[ProfileRow], Optional[str]]:
    """Resolve ``ref`` (agent id, else unambiguous name) to an agent row.

    Returns ``(agent_row, error_message)`` — exactly one is non-None.
    """
    from app.db import database

    ref = (ref or "").strip()
    if not ref:
        return None, "Task requires a target agent (id or name)."
    row = database.get_profile(ref)
    if row:
        return row, None
    matches = [
        a for a in database.get_all_profiles()
        if (a.get("name") or "").strip().lower() == ref.lower()
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        ids = ", ".join(sorted(a["id"] for a in matches))
        return None, f"Agent name '{ref}' is ambiguous — use an id: {ids}"
    return None, f"Unknown agent: '{ref}'. List agents via the agents API/UI."


def _def_field(definition: Any, field: str, default: Any = None) -> Any:
    """One field off an agent definition — dataclass attr or mapping key.

    The registry holds fluid ``AgentDefinition`` records, but this module never
    imports fluid (it also serves the delegated leg, which has no engine
    objects at all): duck-typing keeps the seam honest and the tests trivial.
    """
    if isinstance(definition, dict):
        value = definition.get(field, default)
    else:
        value = getattr(definition, field, default)
    return default if value is None else value


def resolve_registry_agent(ref: str, registry: Any) -> Optional[Any]:
    """A file/plugin agent definition matching ``ref``, or None.

    Exact name first, then a unique case-insensitive match — the same
    forgiveness ``resolve_target_agent`` grants app agents, because the model
    reads these names out of a prompt index and re-types them.
    """
    if not isinstance(registry, dict) or not registry:
        return None
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref in registry:
        return registry[ref]
    matches = [d for name, d in registry.items() if name.strip().lower() == ref.lower()]
    return matches[0] if len(matches) == 1 else None


def _file_agent_prompt(definition: Any) -> str:
    """The worker's system-prompt override block for a file-defined agent."""
    name = str(_def_field(definition, "name", "") or "agent")
    path = str(_def_field(definition, "path", "") or "")
    body = str(_def_field(definition, "body", "") or "").strip()
    tools: Tuple[Any, ...] = tuple(_def_field(definition, "tools", ()) or ())

    lines = [
        f"## Agent definition — you are running as the '{name}' agent",
        (
            f"These instructions come from the agent definition at {path}. "
            "They describe the role you are dispatched for and take precedence "
            "over your profile's general instructions for this task."
            if path
            else
            "The instructions below define the role you are dispatched for and "
            "take precedence over your profile's general instructions for this "
            "task."
        ),
    ]
    if tools:
        lines.append(
            "This definition restricts the agent to these tools: "
            + ", ".join(str(t) for t in tools)
            + ". The restriction is enforced by the runtime — tools outside the "
            "list are not part of your tool surface and calling one fails."
        )
    if body:
        lines.append(body)
    return "\n\n".join(lines)


def _definition_tool_allowlist(definition: Any) -> Optional[List[str]]:
    """The definition's ``tools:`` frontmatter as a worker tool allowlist.

    Ruling CE12: a non-empty list is the worker's ENTIRE tool surface; an empty
    or absent list means "inherit everything the caller has" and is signalled by
    None (fluid's ``allowed_tools=None`` = unrestricted). Blank entries are
    dropped; nothing is ever added back — a definition that omits an "essential"
    tool simply does not get it.
    """
    names = [
        str(t).strip()
        for t in (_def_field(definition, "tools", ()) or ())
        if str(t).strip()
    ]
    return names or None


def _stored_agent_tools(agent_id: str) -> Optional[List[str]]:
    """A stored agent's configured tool allowlist, read only to pick a mode.

    Dispatch deliberately sends no ``tool_allowlist`` for a DB agent — the
    worker's own profile applies its access downstream — so this row lookup
    exists purely to classify the target. A lookup failure is not worth failing
    a dispatch over; the caller falls back to the non-Explore default.
    """
    try:
        from app.db.subagents import get_subagent

        row = get_subagent(agent_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "task_dispatch: tool lookup failed for agent %r; assuming it is "
            "not read-only",
            agent_id,
            exc_info=True,
        )
        return None
    if not row:
        return None
    tools = row.get("tools")
    return list(tools) if isinstance(tools, (list, tuple)) else None


def _default_task_mode(declared_tools: Optional[List[str]], agent_label: str) -> str:
    """The mode a Task call takes when it names none.

    Explore-class targets — read-only research agents — default to
    ``background``. A research child produces a report its parent reads when it
    gets to it; blocking the parent on one turns a fan-out into a queue, which
    is exactly what made research children serialize their parents before. Any
    agent that can write defaults to ``sync``, where the parent normally does
    need the result in hand before it continues.

    For an Explore-class target this is not merely a default: see
    :func:`_effective_task_mode`, which refuses to let an explicit ``mode``
    override it.
    """
    from app.core.builtin_subagents import is_read_only_tool_surface

    if is_read_only_tool_surface(declared_tools):
        logger.debug(
            "task_dispatch: %r declares a read-only tool surface %r; "
            "defaulting to background",
            agent_label,
            declared_tools,
        )
        return "background"
    return "sync"


def _effective_task_mode(
    requested_mode: str, default_mode: str, agent_label: str
) -> Tuple[str, Optional[str]]:
    """``(mode, ignored_request)`` for one dispatch.

    A background-default target is background, full stop. The tool schema
    normally hides ``mode`` for such targets so a model cannot ask for anything
    else, but a request can still arrive — an old transcript replayed, a retry
    built from one, a mixed roster where the key stays in the schema for the
    write-capable targets. Honouring it would reopen exactly the failure the
    default was added to close: a blocked parent, and a user Stop that takes a
    finished research child's report down with it. So it is ignored, loudly,
    and the caller is told in the payload rather than left guessing.

    Every other target keeps the old contract: an explicit ``mode`` wins.
    """
    overridden = requested_mode and requested_mode != default_mode
    if default_mode == "background" and overridden:
        logger.info(
            "task_dispatch: ignoring mode=%r for read-only target %r; "
            "research children always dispatch in the background",
            requested_mode,
            agent_label,
        )
        return "background", requested_mode
    return (requested_mode or default_mode), None


def _caller_provider_id(
    caller_row: Optional[SessionRow], caller_profile_id: Optional[str]
) -> Optional[str]:
    """The provider the caller is bound to — session row first, profile after."""
    if (caller_row or {}).get("mode") == "voice":
        from app.core.query_options import decode_provider_model

        model = (caller_row or {}).get("model")
        if model:
            decoded = decode_provider_model(model)
            return decoded[0] if decoded else None
    provider_id = (caller_row or {}).get("provider_id")
    if provider_id:
        return str(provider_id)
    if not caller_profile_id:
        return None
    try:
        from app.db import database

        profile = database.get_profile(caller_profile_id) or {}
    except Exception:  # noqa: BLE001 — binding lookup is best-effort
        return None
    return ((profile.get("config") or {}).get("provider")) or None


def _credential_gap(provider_id: Optional[str]) -> Optional[str]:
    """A ready-to-return error when the resolved leg has no credentials.

    Fix-3 pre-flight: a background dispatch returns ``ok: true`` the moment the
    worker registers, so a worker that dies at provider connect (the
    ``ProviderCredentialError`` raised by
    :func:`app.core.provider.fluid_credentials.resolve_provider_credentials`)
    used to leave the caller with a success and silence. Checking the same
    *presence* condition here — connection-free, no decryption, mirroring
    ``db.count_configured_model_providers``'s definition of "usable" — turns it
    into a synchronous refusal.

    Scope, deliberate: PROVIDER-ROW legs only. A provider-less (delegated
    Claude-subscription) worker is the app's default path and its credentials
    live in the claude CLI's own config dir plus the account-failover pool; a
    filesystem probe here could refuse a perfectly working install, so that leg
    is left to the runtime. Every lookup failure also fails OPEN — this gate may
    only ever turn a certain failure into a clear message, never invent one.
    """
    if not provider_id:
        return None
    from app.db import database

    try:
        row = database.get_model_provider(provider_id, include_token=True)
    except Exception:  # noqa: BLE001 — fail open
        logger.debug(
            "task_dispatch: credential pre-flight lookup failed for %s",
            provider_id, exc_info=True,
        )
        return None
    if not row:
        return (
            f"Model provider '{provider_id}' no longer exists, so the worker "
            "would have nothing to connect to."
        )
    if not row.get("enabled"):
        return (
            f"Model provider '{row.get('name') or provider_id}' is disabled, so "
            "the worker would fail at connect. Re-enable it or pick another model."
        )
    label = row.get("name") or provider_id
    has_api_key = bool(str(row.get("auth_token") or "").strip())
    has_codex = bool(str(row.get("codex_access_token") or "").strip())
    if not has_api_key and not has_codex:
        return (
            f"Model provider '{label}' has no credentials configured, so the "
            "worker would fail at connect. Add an API key (or connect its "
            "ChatGPT account) under Settings → Model Providers."
        )
    if not has_api_key and has_codex and row.get("needs_reauth"):
        return (
            f"The ChatGPT account for model provider '{label}' needs to be "
            "reconnected before it can run a worker."
        )
    return None


def _file_agent_model_id(
    caller_row: Mapping[str, Any],
    caller_profile_id: str,
    provider_id: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """The model a file/plugin agent inherits — ``(model_id, error)``.

    A file agent has no ``agents`` row to carry a per-provider model, so it
    borrows one. On the native Claude leg ``None`` is a legal answer: the worker
    turn simply inherits the caller's model. On a PROVIDER-bound leg it is not —
    a model-less provider turn resolves downstream to an arbitrary curated entry
    (the registry's FIRST id, which is how every subagent of a cheap parent
    silently ran on the roster's most expensive model, 2026-08-20 incident). The
    provider branch therefore returns only a CURATED id and refuses otherwise;
    list position is never a model choice.
    """
    launched = str(caller_row.get("launched_model") or "").strip() or None
    if caller_row.get("mode") == "voice" and caller_row.get("model"):
        from app.core.query_options import decode_provider_model

        selected = str(caller_row["model"]).strip()
        decoded = decode_provider_model(selected)
        launched = decoded[1] if decoded else selected
    if not provider_id:
        return launched, None

    from app.db import database

    try:
        row = database.get_model_provider(provider_id, include_token=False)
    except Exception:  # noqa: BLE001 — a lookup failure reads as "missing"
        logger.debug(
            "task_dispatch: provider lookup failed for %s", provider_id, exc_info=True
        )
        row = None
    if not row:
        return None, (
            f"Model provider '{provider_id}' no longer exists, so the worker's "
            "model cannot be resolved."
        )

    from app.core.provider_agent import curated_model_ids

    curated = curated_model_ids(row)

    # The parent turn's model first: ``launched_model`` tracks composer
    # hot-swaps, so it is the binding the human actually chose to pay for.
    if launched and launched in curated:
        return launched, None

    try:
        from app.core.agent_provider_models import model_for_provider

        profile = database.get_profile(caller_profile_id) or {}
        mapped = model_for_provider(profile.get("config") or {}, provider_id)
    except Exception:  # noqa: BLE001 — no mapping is a legal outcome
        logger.debug(
            "task_dispatch: profile model lookup failed for %s",
            caller_profile_id, exc_info=True,
        )
        mapped = None
    if mapped:
        from app.core.agent_provider_models import (
            decode_subagent_binding,
            provider_key,
        )

        binding = decode_subagent_binding(provider_key(provider_id), mapped)
        # File agents never switch legs — only a same-provider model id is
        # usable as a fallback. Composites for another provider, and a Claude
        # alias stored on this key, are ignored here.
        if binding is not None and binding[0] == provider_id:
            mapped = binding[1]
        else:
            mapped = None
    if mapped and mapped in curated:
        return mapped, None

    default = row.get("default_model")
    if isinstance(default, str) and default in curated:
        return default, None

    label = row.get("name") or provider_id
    return None, (
        f"Task cannot resolve a model for this file agent on provider "
        f"'{label}': the calling session's model is not usable here and the "
        "provider has no curated default model. Set the provider's default "
        "model or pick a provider model for this session."
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def dispatch_task_tool(
    args: Dict[str, Any],
    *,
    caller_session_id: str,
    caller_profile_id: Optional[str] = None,
    caller_provider_id: Any = _PROVIDER_UNSET,
    caller_effort: Any = _EFFORT_UNSET,
    launch_tool_use_id: Optional[str] = None,
    agent_registry: Optional[Dict[str, Any]] = None,
    roster: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Dispatch through the exact provider-scoped subagent roster.

    DB agents must have an assignment for the caller's effective provider.
    That assignment may name another leg; a pinned target then runs on its
    own provider. Fluid file/plugin definitions remain available as a
    fallback and borrow the caller's profile, provider, and selected model;
    they never switch legs.

    ``roster`` replaces the caller-provider roster with an already-admitted
    one — the voice bridge passes ``available_voice_subagents()``, whose
    targets each PIN their own leg (``provider_pinned``): the worker then runs
    on the target's provider/model, not on whatever funds the caller. Chat
    Task uses the same pin when an Agents-tab assignment names another leg.
    """
    from app.core import provider_agent
    from app.core.config import settings
    from app.core.agent_provider_models import (
        available_subagents,
        resolve_subagent,
    )

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "Task requires a prompt."}
    # The default depends on which agent this call names, so it cannot be
    # applied until the target resolves below. Validate the explicit value here
    # so a typo still fails before any of the resolution work.
    requested_mode = (args.get("mode") or "").strip().lower()
    if requested_mode and requested_mode not in ("sync", "background"):
        return {
            "ok": False,
            "error": f"Unknown mode '{requested_mode}' (sync|background).",
        }
    if args.get("model") not in (None, ""):
        return {
            "ok": False,
            "error": (
                "Task does not accept a model override. The selected subagent's "
                "model is configured in the Agents UI."
            ),
        }

    caller_row = _session_row(caller_session_id) or {}
    # Resolve before the first admission await. Queuing/retries must not inherit
    # a later parent turn's settings. Native callers supply their engine binding.
    selected_effort = (
        provider_agent._caller_effort(caller_row)
        if caller_effort is _EFFORT_UNSET else caller_effort
    )
    caller_pid = caller_profile_id or caller_row.get("profile_id")
    if not caller_pid:
        return {
            "ok": False,
            "error": "Task could not resolve the calling agent profile.",
        }
    effective_provider_id = (
        _caller_provider_id(caller_row, caller_pid)
        if caller_provider_id is _PROVIDER_UNSET
        else caller_provider_id
    )

    ref = (args.get("agent") or "").strip()
    if roster is None:
        roster = available_subagents(caller_pid, effective_provider_id)
    target, roster_error = resolve_subagent(ref, roster)
    if target is not None and getattr(target, "provider_pinned", False):
        # A pinned target (voice roster, or a chat assignment that names
        # another leg) chooses the worker's provider itself.
        effective_provider_id = target.provider_id or None

    # Fluid file/plugin definitions have no Agents-UI row. Keep them usable as
    # a fallback, but never let one shadow a stored DB agent that is unavailable
    # on this provider.
    stored_target, _ = resolve_target_agent(ref)
    definition = (
        resolve_registry_agent(ref, agent_registry)
        if target is None and stored_target is None
        else None
    )
    if target is None and definition is None:
        return {"ok": False, "error": roster_error}

    if definition is not None:
        target_name = str(_def_field(definition, "name", ref) or ref)
        profile_id = str(caller_pid)
        model_id, model_error = _file_agent_model_id(
            caller_row, str(caller_pid), effective_provider_id
        )
        if model_error:
            return {"ok": False, "error": model_error}
        system_prompt_append = _file_agent_prompt(definition)
        tool_allowlist = _definition_tool_allowlist(definition)
        declared_tools = tool_allowlist
        result_agent = target_name
    else:
        assert target is not None
        target_name = target.name
        profile_id = target.id
        model_id = target.model
        system_prompt_append = None
        tool_allowlist = None
        declared_tools = _stored_agent_tools(target.id)
        if target.id in _chain_agent_ids(caller_session_id):
            return {
                "ok": False,
                "error": (
                    f"Cycle refused: subagent '{target.id}' is already active "
                    "in this dispatch chain."
                ),
            }
        result_agent = target.id

    mode, ignored_mode = _effective_task_mode(
        requested_mode,
        _default_task_mode(declared_tools, str(result_agent)),
        str(result_agent),
    )

    max_depth = int(getattr(settings, "task_tool_max_depth", 5) or 5)
    _, root_session_id, caller_depth = caller_chain(caller_session_id)
    if caller_depth + 1 > max_depth:
        return {
            "ok": False,
            "error": f"Nesting limit reached ({max_depth} levels of agent-invokes-agent).",
        }
    max_workers = int(getattr(settings, "task_tool_max_workers", 64) or 64)
    if _live_worker_count_for_root(root_session_id) >= max_workers:
        return {
            "ok": False,
            "error": (
                f"Worker budget reached ({max_workers} live workers for this "
                "session). Wait for running workers or stop them."
            ),
        }

    credential_error = _credential_gap(effective_provider_id)
    if credential_error:
        return {"ok": False, "error": credential_error}

    label = (args.get("label") or "").strip() or f"Task · {target_name}"
    kwargs: Dict[str, Any] = dict(
        parent_session_id=root_session_id,
        context_session_id=caller_session_id,
        profile_id=profile_id,
        provider_id=effective_provider_id,
        model_id=model_id,
        inherited_effort=selected_effort,
        prompt=prompt,
        label=label,
        agent_name=target_name,
        launch_tool_use_id=launch_tool_use_id,
    )
    if system_prompt_append:
        kwargs["system_prompt_append"] = system_prompt_append
    if tool_allowlist:
        kwargs["tool_allowlist"] = tool_allowlist

    # Allocate the durable identity before scheduling the worker. Fluid uses
    # this exact id as its background detach receipt (polling by label could
    # bind two same-label launches to the wrong worker or return None under
    # load), and BOTH modes need it up front now: it is the key the admission
    # reservation below is held under, and the one the pool will show when the
    # worker's turn actually connects.
    worker_id = str(uuid.uuid4())
    kwargs["worker_session_id"] = worker_id

    # Admission BEFORE any receipt. A worker that the session pool cannot admit
    # must fail here, as a tool error the model can act on, rather than three
    # layers down inside a turn the parent has already been told to park on.
    # The lane decides WHICH ceiling applies (a native worker spawns no
    # subprocess, so the subprocess cap is not its ceiling) — resolving it can
    # read the provider row, so it runs off the loop.
    lane = await asyncio.to_thread(_worker_lane, effective_provider_id, model_id)
    capacity_error, queued_seconds = await _reserve_admission(
        worker_id,
        lane=lane,
        parent_session_id=root_session_id,
        context_session_id=caller_session_id,
        provider_id=effective_provider_id,
    )
    if capacity_error:
        return {
            "ok": False,
            "error": capacity_error,
            "reason": "capacity",
            "retryable": True,
        }

    if mode == "background":
        task = asyncio.create_task(
            _dispatch_worker_with_retries(
                kwargs=kwargs,
                background=True,
                lane=lane,
                provider_id=effective_provider_id,
                parent_session_id=root_session_id,
                dispatcher=provider_agent.dispatch_worker,
            )
        )
        from app.core.swarm import register_job
        register_job(worker_id, task, caller_session_id)
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        # The reservation stops counting once the worker is pooled (or ages
        # out); this also frees it the moment a dispatch dies before it ever
        # got that far. (The retry wrapper releases each attempt's own
        # reservation; this is the belt for the first one.)
        task.add_done_callback(lambda _t, _w=worker_id: _release_admission(_w))
        registered = await _await_worker_registration(
            root_session_id, worker_id, task=task
        )
        if not registered and task.done():
            try:
                early = task.result()
            except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "error": f"Background worker failed during registration: {exc}",
                }
            if not early.ok:
                return {
                    "ok": False,
                    "error": early.error or "Background worker failed to start.",
                    "worker_session_id": worker_id,
                }
        elif not registered:
            logger.warning(
                "task_dispatch: worker %s did not register within the spawn "
                "grace period; returning its durable id while the host-owned "
                "dispatch continues",
                worker_id,
            )
        # PARK-FIRST wording, mirroring the Monitor receipt
        # (``monitor_spawn.monitor_receipt_text``) that measurably works: the
        # audited session parked instantly and correctly on Monitor and then
        # re-derived a background worker's entire brief off the old "continue
        # useful work" phrasing here.
        note = (
            "Worker dispatched in the background. End the turn now unless you "
            "have genuinely disjoint work — you will be woken with the report "
            "when it completes. Never re-derive the worker's brief yourself; "
            "mcp__builtins__TaskList and mcp__builtins__TaskGet are for "
            "explicit inspection, not polling. Before the next fan-out, "
            "resolve any ambiguity in the user's report that would change a "
            "worker's brief — ask them; a worker sent on the wrong premise "
            "returns a confident wrong answer you then pay to undo."
        )
        background_payload: Dict[str, Any] = {
            "ok": True,
            "mode": "background",
            "agent": result_agent,
            "model": model_id,
            "task_id": worker_id,
            "worker_session_id": worker_id,
            "description": label,
        }
        if queued_seconds >= 1.0:
            # This wait can come from any Shuffle admission guard; it does
            # not establish a provider-side throughput or concurrency limit.
            background_payload["queued_seconds"] = round(queued_seconds, 1)
            note = (
                f"Queued {queued_seconds:.0f}s behind Shuffle capacity before "
                "starting — this is normal backpressure, not a failure. " + note
            )
        if ignored_mode:
            background_payload["requested_mode_ignored"] = ignored_mode
            note = (
                f"mode={ignored_mode!r} was ignored: '{result_agent}' is a "
                "read-only research agent and always runs in the background. "
                + note
            )
        background_payload["note"] = note
        return background_payload

    # A SYNC dispatch blocks the parent turn: it cannot park, cannot be steered
    # and cannot be told the worker went quiet. Two audited sync Tasks held
    # their parents for 15m03s and 19m28s on a provider connection that never
    # opened, and both ended only because a human intervened. So this lane gets
    # a hard wall-clock cap across every attempt (``dispatch_worker`` also
    # applies the first-frame watchdog, which normally fires long before it),
    # and it returns a worker-failed result the parent can act on.
    sync_cap = _sync_cap_seconds()
    try:
        outcome = await _dispatch_worker_with_retries(
            kwargs=kwargs,
            background=False,
            lane=lane,
            provider_id=effective_provider_id,
            parent_session_id=root_session_id,
            dispatcher=provider_agent.dispatch_worker,
            overall_deadline=(
                time.monotonic() + sync_cap if sync_cap > 0 else None
            ),
        )
    finally:
        # A sync worker holds its slot for as long as it runs, and the pool is
        # what counts that; the reservation only ever covered the gap before it
        # connected (and is dropped by the pool check anyway once it has).
        _release_admission(worker_id)
    payload: Dict[str, Any] = {
        "ok": outcome.ok,
        "mode": "sync",
        "agent": result_agent,
        "model": model_id,
        "worker_session_id": outcome.session_id,
    }
    if queued_seconds >= 1.0:
        payload["queued_seconds"] = round(queued_seconds, 1)
    if outcome.ok:
        # Out-of-band delivery. A full report inline is up to MAX_REPORT_CHARS
        # of the parent's context spent before it knows whether it needs any of
        # it — and under code mode it lands as one kernel value the model then
        # prints whole. The file holds everything; the head decides.
        report = outcome.report or "(no report)"
        report_path = write_task_report(caller_row, outcome.session_id, report)
        payload["report_chars"] = len(report)
        payload["report_head"] = report[:SYNC_REPORT_HEAD_CHARS]
        if report_path:
            payload["report_path"] = report_path
            payload["note"] = (
                "report_head is the complete report; report_path is the same "
                "text on disk. Nothing further to read."
                if len(report) <= SYNC_REPORT_HEAD_CHARS
                else (
                    f"report_head is the first {SYNC_REPORT_HEAD_CHARS} of "
                    f"{len(report)} characters. The rest is at report_path — "
                    "read it with Read(offset=..., limit=...) only if the head "
                    "does not already answer your question, and never print "
                    "the whole file."
                )
            )
        else:
            # The report file could not be written; nothing is lost, but the
            # parent pays the full context cost it normally would not.
            payload["report"] = report
            payload["note"] = (
                "The report file could not be written, so the full report is "
                "inline in 'report'."
            )
        if outcome.truncated:
            # provider_agent bounded the worker's own final text at
            # MAX_REPORT_CHARS. The file carries exactly what the parent would
            # have received inline — nothing is dropped by the file hand-off.
            payload["truncated"] = True
    else:
        payload["error"] = outcome.error or "Worker failed."
    return payload


def task_report_dir(caller_row: Optional[Mapping[str, Any]]) -> Path:
    """Where this parent's Task reports land — sync and background alike.

    The parent's already-materialized checkout when it has one — a report is
    only useful if the parent's Read reaches it, and its cwd is the one tree
    that is always reachable. Sessions with no checkout on this runner (bare
    sessions, tests) fall back to the app data dir.
    """
    from app.core.config import settings

    if caller_row:
        try:
            from app.core import project_resolver

            cwd = project_resolver.resolve_existing_cwd(dict(caller_row))
            if cwd:
                return Path(cwd).joinpath(*_REPORT_DIR_PARTS)
        except Exception:  # noqa: BLE001 — best-effort; the data dir still works
            logger.debug("task_dispatch: report cwd resolution failed", exc_info=True)
    return settings.effective_data_dir.joinpath("fluid", *_REPORT_DIR_PARTS[1:])


def write_task_report(
    caller_row: Optional[Mapping[str, Any]],
    worker_session_id: str,
    report: str,
) -> Optional[str]:
    """Persist a worker's full report; return its absolute path or None.

    Out-of-band delivery is the point: the parent gets a bounded head in its
    context and reads the rest with ``Read(offset=…, limit=…)`` only if it
    needs to. A write failure returns None and the caller degrades to the old
    inline delivery rather than losing the worker's output.

    ONE writer for both modes. A sync dispatch calls it inline (below); a
    BACKGROUND dispatch calls it from ``provider_agent.dispatch_worker``'s
    completion path, because there is no longer a caller frame to return to by
    then. An audited parent whose background child finished with a 27KB report
    had nowhere to read it from, guessed this exact path, found nothing, and
    redid the child's work — so background writes it too.
    """
    try:
        directory = task_report_dir(caller_row)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{worker_session_id or uuid.uuid4()}.md"
        path.write_text(report, encoding="utf-8")
        return str(path.resolve())
    except Exception:  # noqa: BLE001 — never fail a completed dispatch on I/O
        logger.warning(
            "task_dispatch: could not write the report file for worker %s",
            worker_session_id,
            exc_info=True,
        )
        return None


async def _await_worker_registration(
    root_session_id: str,
    worker_session_id: str,
    timeout: float = 5.0,
    *,
    task: Optional[asyncio.Task] = None,
) -> bool:
    """Wait briefly for the pre-allocated worker id to enter the supervisor.

    This is only a spawn-settlement grace period. The background dispatch task
    remains host-owned and strongly referenced if registration takes longer.
    """
    from app.core import provider_agent_supervisor as sup

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if sup.get_worker(root_session_id, worker_session_id) is not None:
            return True
        if task is not None and task.done():
            return False
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Tool-surface adapters
# ---------------------------------------------------------------------------

def make_fluid_dispatcher(
    session_id: "str | Callable[[], Optional[str]]",
    profile_id: Optional[str] = None,
    provider_id: Optional[str] = None,
):
    """The ``ctx.extra['task_dispatch']`` callable for fluid's bare Task builtin
    (native legs).

    ``session_id`` is the caller's **app** session id (``sessions.id``) — the
    row Task dispatch walks for lineage, budget, and worker parenting. It may
    be a plain string or a zero-argument callable resolved at CALL time.

    Late binding is the point: the native leg builds its ``ToolCtx`` inside
    ``HarnessAdapter.connect()``, and a brand-new session has no id to bind
    there yet. The ctx (and therefore this closure) then lives for the whole
    pooled life of the engine, so a connect-time snapshot would stay empty
    forever and every Task call would report the tool unavailable.

    ``ctx.session_id`` is deliberately NOT used as a fallback: on a native leg
    it holds the *fluid* session id (``ses_...``), which is not a
    ``sessions.id`` — dispatching against it would parent the worker onto a
    session row that does not exist.
    """

    async def _dispatch(input: Dict[str, Any], ctx: Any) -> Any:
        from fluid.tools.registry import BackgroundTaskReceipt, ToolOutcome

        caller_session_id: Any = session_id
        if callable(caller_session_id):
            try:
                caller_session_id = caller_session_id()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "task_dispatch: caller session-id supplier raised; Task "
                    "cannot be dispatched for this call",
                    exc_info=True,
                )
                caller_session_id = None
        if not caller_session_id:
            logger.warning(
                "task_dispatch: no app session id resolved at dispatch time — "
                "refusing to dispatch a parentless worker"
            )
            payload = {
                "ok": False,
                "error": (
                    "Task dispatch could not resolve the calling session — the "
                    "worker would have no parent session to report back to. "
                    "This is a host wiring problem, not a problem with the "
                    "request; retry once the session is fully registered."
                ),
            }
            return ToolOutcome(
                text=json.dumps(payload), ui=payload, is_error=True
            )
        # Stamped by the engine for the duration of this dispatch (NativeLoop
        # sets ctx.extra["tool_use_id"] per tool call; dispatches are
        # sequential). Absent on the delegated leg's bridged mcp tool.
        launch_tool_use_id = None
        agent_registry = None
        caller_effort = _EFFORT_UNSET
        try:
            extra = getattr(ctx, "extra", None) or {}
            caller_effort = extra.get("app_caller_effort", _EFFORT_UNSET)
            candidate = extra.get("tool_use_id")
            if isinstance(candidate, str) and candidate:
                launch_tool_use_id = candidate
            # File/plugin agent definitions fluid discovered for this session
            # (ruling CE6). Read at CALL time, not closure time: NativeLoop
            # populates it while constructing itself, which happens AFTER this
            # dispatcher is built and injected into the very same ctx.
            registry = extra.get("agent_registry")
            if isinstance(registry, dict) and registry:
                agent_registry = registry
        except Exception:  # noqa: BLE001 — display-only nicety + optional seam
            launch_tool_use_id = None
        payload = await dispatch_task_tool(
            input,
            caller_session_id=str(caller_session_id),
            caller_profile_id=profile_id,
            caller_provider_id=provider_id,
            caller_effort=caller_effort,
            launch_tool_use_id=launch_tool_use_id,
            agent_registry=agent_registry,
        )
        receipt = None
        if payload.get("ok") and payload.get("mode") == "background":
            task_id = payload.get("task_id") or payload.get("worker_session_id")
            if task_id:
                receipt = BackgroundTaskReceipt(
                    task_id=str(task_id),
                    worker_session_id=str(payload.get("worker_session_id") or task_id),
                    description=payload.get("description") or input.get("label"),
                )
        return ToolOutcome(
            text=json.dumps(payload, default=str),
            ui=payload,
            is_error=not bool(payload.get("ok")),
            background_task=receipt,
        )

    return _dispatch


def subagent_system_prompt(session_id: Optional[str]) -> Optional[str]:
    """Return the model-facing role block for a Task-created worker."""
    if not session_id or not _is_worker_session(session_id):
        return None
    from app.core.config import settings

    _, _, depth = caller_chain(session_id)
    max_depth = int(getattr(settings, "task_tool_max_depth", 5) or 5)
    if depth < max_depth:
        nesting = (
            f"You are at depth {depth} of {max_depth}. You may delegate only "
            "through your own provider-scoped Task roster."
        )
    else:
        nesting = (
            f"You are at the maximum subagent depth ({max_depth}) and cannot "
            "delegate further."
        )
    return (
        "# Subagent context\n"
        "You are a subagent, not the main agent. The first user message is your "
        "complete assigned task. You do not have the parent's conversation.\n"
        "Focus on the assigned scope, use the available tools, and verify the "
        "result. Do not start a conversation with the user or take unrelated "
        "side work. Your final response is returned to the parent, so report "
        "concise accomplishments, findings, changed files, verification, and "
        "blockers.\n"
        "YOU HAVE NO WAKE SEAM YOU CAN RELY ON. Assume ending your turn ends "
        "you: there may be no later turn in which to report, and nothing "
        "outside your session can read a Monitor or task you leave running "
        "(those records are scoped to the session that made them). A worker "
        "that ends with work still in flight is reported to its parent as a "
        "failure, not as a result. So finish the work inside this turn — if "
        "you start a test run or a build, wait for it and report what it "
        "actually said. Never end with 'X is running; I'll report when it "
        "completes': that sentence becomes your entire report, and the parent "
        "redoes your work.\n"
        "Lead your final response with ONE sentence stating the result — what "
        "is now true, or what you found. That line is what the parent sees in "
        "its task list, so a heading, a title, or 'I'll start by…' there tells "
        f"it nothing. {nesting}"
    )
