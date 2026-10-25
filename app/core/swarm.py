"""AI Shuffle host for Fluid's Swarm coordinator and scoped native messaging."""
from __future__ import annotations
import asyncio
import contextlib
import copy
import json
import logging
from collections.abc import AsyncIterator
from fluid.swarm import (
    RESULT_LIMIT, TERMINAL, SwarmCoordinator, current_launch, now, recover, validate,
)
from fluid.tools.registry import ToolOutcome

logger = logging.getLogger(__name__)
coordinators = {}
workers = {}  # app worker session id -> trusted Launch
worker_jobs = {}  # initial receipt id -> dispatch/retry task
_locks = {}  # session id -> lock serialising orphan settlement and cancel
# Sessions this process found with no orphaned workflow left to settle. A
# workflow is orphaned only when its coordinator died with an earlier process,
# so one clean look per session per process is enough.
_orphan_free = set()
# Session id -> whether this install holds a killed Swarm workflow row for it.
# Only a restart reap (before this process ran anything for the session), the
# stale sweep or a stop write one, and none of those creates an orphan while
# this process lives, so one answer per session per process holds.
_killed_swarms = {}
# Lease a repair holds while it mutates a session that no run owns.
_REPAIR_LEASE_SECONDS = 120
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


class SwarmUnavailableError(RuntimeError):
    """The workflow has no coordinator in this process to accept control."""

async def persist(snapshot, *, run_id=None, fence_token=None):
    from app.core.history_backfill import ensure_backfilled
    from app.db.session_events import append_events
    await ensure_backfilled(snapshot["session_id"])
    await asyncio.to_thread(append_events, snapshot["session_id"], [
        {"type": "swarm.updated", "payload": {"swarm": snapshot}}],
        run_id=run_id, fence_token=fence_token)

async def snapshots(session_id: str) -> list[dict]:
    """Read the latest durable publications; observing is never recovery."""
    from app.db.session_events import get_latest_swarm_snapshots
    # One MVCC statement observes a consistent projection. Readers neither
    # mutate nor recover anything, so they need no coordinator/session lock.
    return await asyncio.to_thread(get_latest_swarm_snapshots, session_id)

async def bind_worker(worker_id, owner_session_id, *, worker=None):
    """Called by the real dispatch producer before each admitted attempt."""
    launch = current_launch.get()
    if launch is None or launch.coordinator.session_id != owner_session_id:
        return
    workers[worker_id] = launch
    row = next(t for t in launch.coordinator.swarms[launch.swarm_id]["tasks"] if t["id"] == launch.task_id)
    row["worker_session_id"] = worker_id
    if worker is not None:
        row["worker"] = dict(worker)
    await launch.coordinator.emit(launch.swarm_id)


def unbind_worker(worker_id):
    """Forget a remote launch only when the target provably started nothing."""
    launch = workers.pop(worker_id, None)
    if launch is None:
        return
    row = next(t for t in launch.coordinator.swarms[launch.swarm_id]["tasks"] if t["id"] == launch.task_id)
    if row["worker_session_id"] == worker_id:
        row["worker_session_id"] = None
        row.pop("worker", None)


def register_job(worker_id, task, owner_session_id):
    launch = current_launch.get()
    if launch is None or launch.coordinator.session_id != owner_session_id:
        return
    worker_jobs[worker_id] = task
    def settled(job):
        worker_jobs.pop(worker_id, None)
        try:
            outcome = job.result()
            launch.finish("completed" if outcome.ok else "failed", outcome.report or outcome.error)
        except asyncio.CancelledError:
            launch.finish("cancelled", "Cancelled by host")
        except Exception as exc:
            launch.finish("failed", str(exc))
    task.add_done_callback(settled)

def owns_terminal(parent_session_id):
    launch = current_launch.get()
    return launch is not None and launch.coordinator.session_id == parent_session_id

async def _stop_remote_item(worker_id, reason):
    """Stop a Swarm item that runs on another worker; False when it runs here.

    It goes through the supervisor's teardown, like Stop on its card: the
    record settles here and the worker that runs it is asked to stop it.
    """
    from app.core import provider_agent_supervisor as supervisor
    from app.core.remote_task import stop_remote_child
    from app.db import remote_tasks
    try:
        dispatch = await asyncio.to_thread(remote_tasks.get_dispatch, worker_id)
    except Exception:
        return False
    if dispatch is None:
        return False
    summary = "Session closed" if reason == "session_closed" else "Swarm cancelled"
    record = supervisor.get_worker(dispatch["root_session_id"], worker_id)
    if record is not None:
        await supervisor.teardown_worker(record, reason=reason, summary=summary, preview=f"{summary}.")
    else:
        await stop_remote_child(worker_id, dispatch["target_device_id"], reason=reason, summary=summary)
    return True

async def _stop_item(worker_id, reason):
    from app.core.provider_agent_supervisor import _kill_worker_session
    if not await _stop_remote_item(worker_id, reason):
        await _kill_worker_session(worker_id, reason=reason)

async def cancel_worker(task_id):
    launch = workers.get(task_id)
    ids = [key for key, value in workers.items() if value is launch] if launch else [task_id]
    reason = "session_closed" if launch and launch.coordinator.closed else "swarm_cancelled"
    await asyncio.gather(*(_stop_item(key, reason) for key in ids))
    job = worker_jobs.get(task_id)
    if job is not None:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
    if launch:
        launch.finish("cancelled", "Session closed" if reason == "session_closed" else "Swarm cancelled")

def latch_all(session_id):
    """Let no running workflow of ``session_id`` dispatch another worker.

    Stop latches first and then stops each running worker as a task of its
    own; a workflow left unlatched would replace them as they die.
    """
    coordinator = coordinators.get(session_id)
    if coordinator:
        coordinator.cancelled.update(key for key, job in coordinator.jobs.items() if not job.done())


async def cancel_all(session_id):
    coordinator = coordinators.get(session_id)
    if coordinator:
        # Latch every workflow before the first await: no successor may slip
        # through while the host is stopping another workflow's worker.
        active = [key for key, job in coordinator.jobs.items() if not job.done()]
        coordinator.cancelled.update(active)
        await asyncio.gather(*(coordinator.cancel(key) for key in active))

async def close(session_id):
    coordinator = coordinators.get(session_id)
    if coordinator:
        await coordinator.aclose()
        coordinators.pop(session_id, None)
        for key, launch in list(workers.items()):
            if launch.coordinator is coordinator:
                workers.pop(key, None)


def _owns_recovery_lease(session_id, run_id, fence_token):
    """Read live, local ownership; an expired active row is not authority."""
    from app.core.config import settings
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM runs WHERE session_id=%s AND id=%s "
            "AND fence_token=%s AND runner_id=%s AND state='active' "
            "AND lease_expires_at >= NOW()",
            (session_id, run_id, fence_token, settings.effective_instance_id),
        )
        return cursor.fetchone() is not None


def _locally_owned(session_id: str, key: str) -> bool:
    """A coordinator in THIS process still runs (or is starting) the workflow."""
    live = coordinators.get(session_id)
    if live is None:
        return False
    job = getattr(live, "jobs", {}).get(key)
    if job is not None:
        return not job.done()
    return key in getattr(live, "swarms", {})


def _append_authority(
    session_id: str, run_id: str | None, fence_token: int | None,
) -> tuple[bool, str | None, int | None]:
    """``(refused, run_id, fence_token)`` for appending to an orphaned workflow.

    With a lease (turn start) the proof is that lease. Without one (boot,
    cancel, a worker's completion) a live run claimed by a different runner,
    as after a remote hand-off, means its coordinator may live there: leave it
    alone. A live run of this runner fences the append, so a claim that moves
    the session elsewhere in the meantime makes the write fail, not land.
    """
    if run_id is not None:
        return not _owns_recovery_lease(session_id, run_id, fence_token), run_id, fence_token
    from app.core.config import settings
    from app.db.runs import get_active_run

    run = get_active_run(session_id)
    if run is None:
        return False, None, None
    if run.get("runner_id") != settings.effective_instance_id:
        return True, None, None
    return False, run["id"], run["fence_token"]


@contextlib.asynccontextmanager
async def _append_lease(
    session_id: str, run_id: str | None, fence_token: int | None,
) -> AsyncIterator[tuple[str, int] | None]:
    """The fence for a repair's writes, held across them; None when refused.

    A turn's lease (turn start) or a live run of this runner fences the writes
    as is. With no live run this runner claims one for the repair and releases
    it afterwards: until then no other runner can claim the session, and a
    takeover after it lapsed makes the fenced append fail instead of landing.
    """
    from app.core.config import settings
    from app.db import runs

    def acquire() -> tuple[tuple[str, int] | None, dict | None]:
        if run_id is not None:
            owned = _owns_recovery_lease(session_id, run_id, fence_token)
            return ((run_id, fence_token) if owned else None), None
        active = runs.get_active_run(session_id)
        if active is not None:
            if active.get("runner_id") != settings.effective_instance_id:
                return None, None
            return (active["id"], active["fence_token"]), None
        claimed = runs.claim_run(
            session_id, settings.effective_instance_id, _REPAIR_LEASE_SECONDS)
        if claimed is None:
            return None, None
        return (claimed["id"], claimed["fence_token"]), claimed

    fence, held = await asyncio.to_thread(acquire)
    try:
        yield fence
    finally:
        if held is not None:
            await asyncio.to_thread(runs.release_run, held["id"], held["fence_token"])


def _worker_id(task: dict) -> str | None:
    """The item's CURRENT worker: a retry rebinds it, ``task_id`` is the first spawn."""
    worker_id = task.get("worker_session_id") or task.get("task_id")
    return str(worker_id) if worker_id else None


def _worker_outcome(session_id: str, task: dict) -> tuple[str, str]:
    """``(running|completed|interrupted|unknown, result)`` for an item's worker.

    ``unknown`` means the durable record could not be read: never evidence
    that the worker stopped.
    """
    from app.core.task_registry import restored_status, task_registry
    from app.db import background_tasks as background_tasks_db

    worker_id = _worker_id(task)
    if not worker_id:
        return "interrupted", ""
    record = task_registry.get_task(session_id, worker_id)
    if record is None:
        try:
            row = background_tasks_db.get_background_task(worker_id, session_id)
        except Exception:
            logger.debug("Swarm: worker outcome unreadable for %s", worker_id, exc_info=True)
            return "unknown", ""
        record = {} if row is None else dict(
            status=restored_status(row.get("status"), row.get("reason")),
            summary=row.get("summary"), report_path=row.get("report_path"))
    status = record.get("status")
    if status == "running":
        return "running", ""
    if status != "completed":
        return "interrupted", ""
    path = record.get("reportPath") or record.get("report_path")
    if path:
        try:
            with open(path, encoding="utf-8") as report:
                return "completed", report.read(RESULT_LIMIT)
        except OSError:
            pass
    return "completed", str(record.get("summary") or "")[:RESULT_LIMIT]


def _reconcile_items(
    session_id: str, snapshot: dict, *, reopen: frozenset = frozenset(),
) -> tuple[dict, list[dict], bool]:
    """A copy of an orphaned workflow carrying each worker's real outcome.

    Open items, plus terminal items whose status is in ``reopen``, take their
    worker's outcome: completed workers become completed items with their
    report. Returns the copy, the items whose worker is still running, and
    whether any outcome was unreadable; the rest stay for the caller to settle.
    """
    snapshot = copy.deepcopy(snapshot)
    running = []
    unknown = False
    for task in snapshot["tasks"]:
        if (task["status"] in TERMINAL and task["status"] not in reopen) or not _worker_id(task):
            continue
        state, result = _worker_outcome(session_id, task)
        if state == "running":
            running.append(task)
        elif state == "unknown":
            unknown = True
        elif state == "completed":
            task.update(status="completed", result=result, error=None,
                        finished_at=now() if task["status"] in TERMINAL
                        else task.get("finished_at") or now())
    return snapshot, running, unknown


async def _proven_ended(session_id: str, keys: list[str]) -> set[str]:
    """Workflow keys whose own task this install recorded as ended."""
    from app.core.pending_reconcile import terminal_frames_for

    frames = await asyncio.to_thread(terminal_frames_for, session_id, keys)
    return {frame["task_id"] for frame in frames}


async def _has_killed_swarms(session_id: str) -> bool:
    """The cheap gate: this install holds a killed workflow row for the session."""
    from app.db.background_tasks import list_sessions_with_killed_swarms

    if session_id not in _killed_swarms:
        _killed_swarms[session_id] = bool(
            await asyncio.to_thread(list_sessions_with_killed_swarms, session_id))
    return _killed_swarms[session_id]


async def _killed_snapshots(session_id: str) -> list[dict]:
    """Snapshots no local coordinator owns, behind a cheap killed-row gate."""
    if not await _has_killed_swarms(session_id):
        return []
    return [
        snapshot for snapshot in await snapshots(session_id)
        if not _locally_owned(session_id, snapshot["id"])
    ]


async def _publish_owned(
    session_id: str, rows: list[dict], run_id: str | None, fence_token: int | None,
) -> list[dict]:
    """Append ``rows`` under a held fence unless ownership moved meanwhile."""
    from app.db.session_events import FenceViolation

    published = []
    async with _append_lease(session_id, run_id, fence_token) as fence:
        if fence is None:
            return []
        for snapshot in rows:
            if _locally_owned(session_id, snapshot["id"]):
                continue
            try:
                await persist(snapshot, run_id=fence[0], fence_token=fence[1])
            except FenceViolation:
                logger.info("Swarm: %s changed owner before its repair landed", session_id)
                break
            published.append(snapshot)
    return published


async def settle_orphans(
    session_id: str, *, run_id: str | None = None,
    fence_token: int | None = None,
) -> list[dict]:
    """Settle workflows whose coordinator died with an earlier process.

    Orphaned means: the latest snapshot is non-terminal, no coordinator here
    owns it, and this install recorded the workflow's own task as ended (a
    restart reap). Each item takes its worker's real outcome; a workflow with a
    worker still running, or one whose outcome cannot be read, is left for a
    later pass. The aggregate is completed only when every item completed,
    otherwise interrupted. Returns the published snapshots. Reads
    (``snapshots``) never do this themselves.
    """
    if not session_id or session_id in _orphan_free:
        return []
    async with _locks.setdefault(session_id, asyncio.Lock()):
        candidates = [
            s for s in await _killed_snapshots(session_id)
            if s.get("status") not in TERMINAL_STATUSES
        ]
        if not candidates:
            _orphan_free.add(session_id)
            return []
        refused, _, _ = await asyncio.to_thread(
            _append_authority, session_id, run_id, fence_token,
        )
        if refused:
            return []
        ended = await _proven_ended(session_id, [s["id"] for s in candidates])
        deferred = len(ended) < len(candidates)
        settled = []
        for snapshot in candidates:
            if snapshot["id"] not in ended:
                continue
            snapshot, running, unknown = await asyncio.to_thread(
                _reconcile_items, session_id, snapshot,
            )
            if running or unknown:
                deferred = True
                continue
            if all(task["status"] == "completed" for task in snapshot["tasks"]):
                snapshot.update(status="completed", updated_at=now())
            else:
                snapshot = recover(snapshot)
            settled.append(snapshot)
        # Recheck after the evidence reads, before any durable write.
        if settled:
            published = await _publish_owned(session_id, settled, run_id, fence_token)
            if len(published) < len(settled):
                return published
            settled = published
        if not deferred:
            _orphan_free.add(session_id)
        return settled


async def settle_orphans_at_boot(limit: int = 200) -> int:
    """Boot arm of :func:`settle_orphans` for every session a restart hit."""
    from app.db.background_tasks import list_sessions_with_killed_swarms

    count = 0
    for session_id in await asyncio.to_thread(list_sessions_with_killed_swarms, None, limit):
        try:
            count += len(await settle_orphans(session_id))
        except Exception:
            logger.warning("Swarm: orphan settlement failed for %s", session_id, exc_info=True)
    return count


async def record_worker_completion(session_id: str, worker_id: str) -> list[dict]:
    """Write a late worker completion back into its orphaned workflow.

    Called from the parent's terminal-delivery producer when a worker reports
    completion. A live coordinator records its own workers; here only a
    workflow whose coordinator died with an earlier process is touched: an
    open one is settled, and a restart-interrupted item of a settled one takes
    the worker's result (the workflow becomes completed once every item is).
    Returns the published snapshots; never raises.
    """
    if not session_id or not worker_id or worker_id.startswith("swarm_") or worker_id in workers:
        return []
    try:
        if not await _has_killed_swarms(session_id):
            return []  # never cut off by a restart: nothing to write back
        # A completion can be what a deferred settlement was waiting for.
        _orphan_free.discard(session_id)
        published = await settle_orphans(session_id)
        async with _locks.setdefault(session_id, asyncio.Lock()):
            targets = [
                s for s in await _killed_snapshots(session_id)
                if s.get("status") == "interrupted" and any(
                    t["status"] == "interrupted" and _worker_id(t) == worker_id
                    for t in s["tasks"])
            ]
            if not targets:
                return published
            ended = await _proven_ended(session_id, [s["id"] for s in targets])
            updated = []
            for snapshot in targets:
                if snapshot["id"] not in ended:
                    continue
                item = [t for t in snapshot["tasks"] if _worker_id(t) == worker_id]
                result, _, _ = await asyncio.to_thread(
                    _reconcile_items, session_id, dict(snapshot, tasks=item),
                    reopen=frozenset({"interrupted"}),
                )
                if result["tasks"][0]["status"] != "completed":
                    continue
                rows = {t["id"]: t for t in result["tasks"]}
                snapshot = copy.deepcopy(snapshot)
                snapshot["tasks"] = [rows.get(t["id"], t) for t in snapshot["tasks"]]
                if all(t["status"] == "completed" for t in snapshot["tasks"]):
                    snapshot["status"] = "completed"
                snapshot["updated_at"] = now()
                updated.append(snapshot)
            if updated:
                published += await _publish_owned(session_id, updated, None, None)
        return published
    except Exception:
        logger.warning("Swarm: worker write-back failed for %s/%s", session_id, worker_id,
                       exc_info=True)
        return []


_UNREADABLE = "unreadable"


async def _cancel_orphan(session_id: str, key: str) -> dict | str | None:
    """Cancel an orphaned workflow here.

    None when it may run elsewhere; ``_UNREADABLE`` when a worker's outcome
    cannot be read yet, so nothing was stopped or written.
    """
    async with _locks.setdefault(session_id, asyncio.Lock()):
        snapshot = next((s for s in await snapshots(session_id) if s["id"] == key), None)
        if snapshot is None:
            raise ValueError("Unknown swarm")
        if snapshot["status"] in TERMINAL_STATUSES:
            return snapshot
        if _locally_owned(session_id, key) or (await asyncio.to_thread(
            _append_authority, session_id, None, None,
        ))[0] or key not in await _proven_ended(session_id, [key]):
            return None
        snapshot, running, unknown = await asyncio.to_thread(
            _reconcile_items, session_id, snapshot,
        )
        if unknown:
            return _UNREADABLE
        # Recheck after the evidence and outcome reads, before stopping anyone,
        # and hold that fence until the cancelled snapshot is appended under it.
        from app.db.session_events import FenceViolation

        async with _append_lease(session_id, None, None) as fence:
            if fence is None or _locally_owned(session_id, key):
                return None
            await asyncio.gather(*(cancel_worker(_worker_id(task)) for task in running))
            stamp = now()
            for task in snapshot["tasks"]:
                if task["status"] not in TERMINAL:
                    task.update(status="cancelled", error="Swarm cancelled", finished_at=stamp)
            snapshot.update(status="cancelled", updated_at=stamp)
            try:
                await persist(snapshot, run_id=fence[0], fence_token=fence[1])
            except FenceViolation:
                return None  # another runner took the session over meanwhile
            return snapshot


async def reconcile_owned_pending(
    session_id: str, run_id: str, fence_token: int,
) -> list[str]:
    """Repair pooled Swarm receipts only at the runtime's leased reader boundary.

    Connect and turn-end reconciliation still own general Task hygiene. Here we
    also publish the interrupted workflow that receipt-only repair cannot fix,
    and settle workflows orphaned by an earlier process (``settle_orphans``).
    No pool/Swarm receipt and no killed workflow row means no lease query or
    session-history scan.
    """
    try:
        await settle_orphans(session_id, run_id=run_id, fence_token=fence_token)
    except Exception:
        logger.warning("Swarm: orphan settlement failed for %s", session_id, exc_info=True)
    return await _reconcile_pending_receipts(session_id, run_id, fence_token)


async def _reconcile_pending_receipts(
    session_id: str, run_id: str, fence_token: int,
) -> list[str]:
    from app.core.pending_reconcile import (
        _pooled_adapter, engine_pending_task_ids, terminal_frames_for,
    )
    from app.core.turn_scheduler import retire_task_terminals

    pending = await engine_pending_task_ids(_pooled_adapter(session_id))
    keys = [key for key in pending if key.startswith("swarm_")]
    if not keys or not await asyncio.to_thread(
        _owns_recovery_lease, session_id, run_id, fence_token,
    ):
        return []
    live = coordinators.get(session_id)
    keys = [key for key in keys if not (
        live and key in live.jobs and not live.jobs[key].done()
    )]
    frames = await asyncio.to_thread(terminal_frames_for, session_id, keys)
    if not frames:
        return []
    rows = {s["id"]: s for s in await snapshots(session_id)}
    # Recheck after the evidence reads, before either durable mutation. Snapshot
    # appends are fenced too; never turn an unknown aggregate into a success.
    if not await asyncio.to_thread(_owns_recovery_lease, session_id, run_id, fence_token):
        return []
    live = coordinators.get(session_id)
    frames = [frame for frame in frames if not (
        live and frame["task_id"] in live.jobs and not live.jobs[frame["task_id"]].done()
    )]
    for frame in frames:
        snapshot = rows.get(frame["task_id"])
        if snapshot and snapshot["status"] not in TERMINAL_STATUSES:
            await persist(recover(snapshot), run_id=run_id, fence_token=fence_token)
    if not await asyncio.to_thread(_owns_recovery_lease, session_id, run_id, fence_token):
        return []
    if await retire_task_terminals(session_id, frames):
        return [frame["task_id"] for frame in frames]
    return []


def make_recovery(supplier):
    async def recovery(pending):
        from fluid.events.ops import BackgroundTaskFinished
        session_id = supplier()
        if not session_id or not any(key.startswith("swarm_") for key in pending):
            return []
        from app.core.pending_reconcile import terminal_frames_for
        from app.db.runs import get_active_run

        # Engine rehydration is not proof of execution ownership. A different
        # process may still be running the workflow. Even an expired-but-not-
        # reaped run is left to the existing lease lifecycle to reconcile.
        if await asyncio.to_thread(get_active_run, session_id) is not None:
            return []
        rows = {s["id"]: s for s in await snapshots(session_id)}
        keys = [key for key in pending if key.startswith("swarm_")]
        frames = await asyncio.to_thread(terminal_frames_for, session_id, keys)
        settled = {frame["task_id"] for frame in frames}
        live = coordinators.get(session_id)
        ops = []
        for key in keys:
            if live and key in live.jobs and not live.jobs[key].done():
                continue
            snapshot = rows.get(key)
            terminal = snapshot and snapshot["status"] in TERMINAL_STATUSES
            # Missing local state (including a missing task mirror) is not
            # evidence of death. Reuse the host's proven-terminal reconciliation.
            if not terminal and key not in settled:
                continue
            if snapshot and not terminal:
                snapshot = recover(snapshot)
                await persist(snapshot)
            status = snapshot["status"] if snapshot else "interrupted"
            ops.append(BackgroundTaskFinished(
                task_id=key,
                status={"completed": "finished", "cancelled": "cancelled"}.get(status, "failed"),
                wake=False,
                summary=json.dumps({"swarm": snapshot}) if snapshot else "Swarm execution has ended",
            ))
        return ops
    return recovery

def make_handler(session_id_supplier):
    async def handler(data, ctx):
        session_id = session_id_supplier()
        if not session_id:
            return ToolOutcome(text="Calling session is not registered", is_error=True)
        try:
            if data.get("action") == "start":
                from app.core.agent_provider_models import caller_subagents_refusal
                from app.db import database

                caller = await asyncio.to_thread(database.get_session, session_id)
                refusal = caller_subagents_refusal((caller or {}).get("profile_id"))
                if refusal is not None:
                    return ToolOutcome(text=refusal[1], is_error=True)
                validate(data)
                coordinator = coordinators.get(session_id)
                if coordinator is None:
                    from app.core.task_registry import task_registry
                    async def publish(snapshot):
                        await persist(snapshot)
                        if snapshot["status"] == "queued":
                            key = snapshot["id"]
                            task_registry.start_task(session_id, key, "agent", snapshot["title"], tool_use_id=key)
                            task_registry.mark_background_launch(session_id, key)
                    async def finished(key, status, summary):
                        from app.core import turn_scheduler
                        wire_status = "completed" if status == "completed" else "killed" if status == "cancelled" else "error"
                        task_registry.complete_task(session_id, key, wire_status, summary=summary)
                        await turn_scheduler.on_task_terminal(session_id, dict(task_id=key,
                            status=wire_status, tool_use_id=key, summary=summary))
                    coordinator = SwarmCoordinator(session_id, ctx.extra["task_dispatch"], cancel_worker, publish, finished)
                    coordinators[session_id] = coordinator
                snapshot, receipt = await coordinator.start(data, ctx)
                return ToolOutcome(text=json.dumps({"swarm": snapshot}), ui={"swarm": snapshot}, background_task=receipt)
            key = data.get("swarm_id")
            if data.get("action") == "cancel":
                snapshot = await cancel(session_id, key)
            elif data.get("action") == "status":
                snapshot = next((s for s in await snapshots(session_id) if s["id"] == key), None)
                if snapshot is None:
                    raise ValueError("Unknown swarm")
            else:
                raise ValueError("action must be start, status or cancel")
            return ToolOutcome(text=json.dumps({"swarm": snapshot}), ui={"swarm": snapshot})
        except (KeyError, ValueError, SwarmUnavailableError) as exc:
            return ToolOutcome(text=str(exc), is_error=True)
    return handler

async def cancel(session_id, key):
    coordinator = coordinators.get(session_id)
    if coordinator and key in coordinator.swarms:
        return await coordinator.cancel(key)
    snapshot = next((s for s in await snapshots(session_id) if s["id"] == key), None)
    if snapshot is None:
        raise ValueError("Unknown swarm")
    if snapshot["status"] not in TERMINAL_STATUSES:
        # A workflow whose coordinator died here is cancelled here, worker by
        # worker. Otherwise it may run elsewhere: the durable interrupt command
        # targets an entire session/turn, so never cancel unrelated work.
        orphan = await _cancel_orphan(session_id, key)
        if orphan is None:
            raise SwarmUnavailableError(
                "Swarm cancellation is unavailable on this runner; contact the execution owner")
        if orphan == _UNREADABLE:
            raise SwarmUnavailableError(
                "Couldn't read this swarm's workers right now; nothing was stopped. Try again.")
        return orphan
    return snapshot

def peer_rows(session_id):
    launch = workers.get(session_id)
    rows = []
    if launch:
        rows.append(dict(name="main", ref=None, worker_session_id=launch.coordinator.session_id,
            status="running", kind="parent", description="The conversation that started this swarm"))
        owners = [launch.coordinator]
    else:
        owners = [coordinators[session_id]] if session_id in coordinators else []
    for owner in owners:
        for key, swarm in owner.swarms.items():
            if launch and key != launch.swarm_id:
                continue
            for task in swarm["tasks"]:
                target = task["worker_session_id"]
                if not target or target == session_id:
                    continue
                row = dict(name=task["id"], ref=target, worker_session_id=target,
                    task_id=task["task_id"], status=task["status"], kind="subagent", description=task["title"])
                if task.get("worker"):
                    row["worker"] = task["worker"]["name"]
                rows.append(row)
    return rows
