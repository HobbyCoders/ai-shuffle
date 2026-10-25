"""AI Shuffle host for Fluid's Swarm coordinator and scoped native messaging."""
from __future__ import annotations
import asyncio
import copy
import json
import logging
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


def _ownership_refused(
    session_id: str, run_id: str | None, fence_token: int | None,
) -> bool:
    """True when another runner may be executing this session's workflows.

    With a lease (turn start) the proof is that lease. Without one (boot,
    cancel) a live run claimed by a different runner, as after a remote
    hand-off, means its coordinator may live there: leave it alone.
    """
    if run_id is not None:
        return not _owns_recovery_lease(session_id, run_id, fence_token)
    from app.core.config import settings
    from app.db.runs import get_active_run

    run = get_active_run(session_id)
    return run is not None and run.get("runner_id") != settings.effective_instance_id


def _worker_outcome(session_id: str, task: dict) -> tuple[str, str]:
    """``(running|completed|interrupted, result)`` from an item's worker task."""
    from app.core.task_registry import task_registry

    task_id = task.get("task_id") or task.get("worker_session_id")
    if not task_id:
        return "interrupted", ""
    record = task_registry.get_task(session_id, str(task_id), durable=True) or {}
    if record.get("status") == "running":
        return "running", ""
    if record.get("status") != "completed":
        return "interrupted", ""
    path = record.get("reportPath") or record.get("report_path")
    if path:
        try:
            with open(path, encoding="utf-8") as report:
                return "completed", report.read(RESULT_LIMIT)
        except OSError:
            pass
    return "completed", str(record.get("summary") or "")[:RESULT_LIMIT]


def _reconcile_items(session_id: str, snapshot: dict) -> tuple[dict, list[dict]]:
    """A copy of an orphaned workflow carrying each worker's real outcome.

    Completed workers become completed items with their report. Returns the
    copy and the items whose worker is still running; the rest stay open for
    the caller to settle.
    """
    snapshot = copy.deepcopy(snapshot)
    running = []
    for task in snapshot["tasks"]:
        if task["status"] in TERMINAL or not (
            task.get("task_id") or task.get("worker_session_id")
        ):
            continue
        state, result = _worker_outcome(session_id, task)
        if state == "running":
            running.append(task)
        elif state == "completed":
            task.update(status="completed", result=result, error=None,
                        finished_at=task.get("finished_at") or now())
    return snapshot, running


async def _proven_ended(session_id: str, keys: list[str]) -> set[str]:
    """Workflow keys whose own task this install recorded as ended."""
    from app.core.pending_reconcile import terminal_frames_for

    frames = await asyncio.to_thread(terminal_frames_for, session_id, keys)
    return {frame["task_id"] for frame in frames}


async def _open_orphan_candidates(session_id: str) -> list[dict]:
    """Non-terminal snapshots no local coordinator owns, behind a cheap gate."""
    from app.db.background_tasks import list_sessions_with_killed_swarms

    if not await asyncio.to_thread(list_sessions_with_killed_swarms, session_id):
        return []
    return [
        snapshot for snapshot in await snapshots(session_id)
        if snapshot.get("status") not in TERMINAL_STATUSES
        and not _locally_owned(session_id, snapshot["id"])
    ]


async def settle_orphans(
    session_id: str, *, run_id: str | None = None,
    fence_token: int | None = None,
) -> list[dict]:
    """Settle workflows whose coordinator died with an earlier process.

    Orphaned means: the latest snapshot is non-terminal, no coordinator here
    owns it, and this install recorded the workflow's own task as ended (a
    restart reap). Each item takes its worker's real outcome; a workflow with a
    worker still running is left for a later pass. The aggregate is completed
    only when every item completed, otherwise interrupted. Returns the
    published snapshots. Reads (``snapshots``) never do this themselves.
    """
    if not session_id or session_id in _orphan_free:
        return []
    async with _locks.setdefault(session_id, asyncio.Lock()):
        candidates = await _open_orphan_candidates(session_id)
        if not candidates:
            _orphan_free.add(session_id)
            return []
        if await asyncio.to_thread(_ownership_refused, session_id, run_id, fence_token):
            return []
        ended = await _proven_ended(session_id, [s["id"] for s in candidates])
        deferred = len(ended) < len(candidates)
        settled = []
        for snapshot in candidates:
            if snapshot["id"] not in ended:
                continue
            snapshot, running = await asyncio.to_thread(_reconcile_items, session_id, snapshot)
            if running:
                deferred = True
                continue
            if all(task["status"] == "completed" for task in snapshot["tasks"]):
                snapshot.update(status="completed", updated_at=now())
            else:
                snapshot = recover(snapshot)
            settled.append(snapshot)
        # Recheck after the evidence reads, before any durable write.
        if settled and await asyncio.to_thread(
            _ownership_refused, session_id, run_id, fence_token,
        ):
            return []
        settled = [s for s in settled if not _locally_owned(session_id, s["id"])]
        for snapshot in settled:
            await persist(snapshot, run_id=run_id, fence_token=fence_token)
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


async def _cancel_orphan(session_id: str, key: str) -> dict | None:
    """Cancel an orphaned workflow here; None when it may run elsewhere."""
    async with _locks.setdefault(session_id, asyncio.Lock()):
        snapshot = next((s for s in await snapshots(session_id) if s["id"] == key), None)
        if snapshot is None:
            raise ValueError("Unknown swarm")
        if snapshot["status"] in TERMINAL_STATUSES:
            return snapshot
        if _locally_owned(session_id, key) or await asyncio.to_thread(
            _ownership_refused, session_id, None, None,
        ) or key not in await _proven_ended(session_id, [key]):
            return None
        snapshot, running = await asyncio.to_thread(_reconcile_items, session_id, snapshot)
        await asyncio.gather(*(
            cancel_worker(str(task.get("task_id") or task["worker_session_id"]))
            for task in running))
        stamp = now()
        for task in snapshot["tasks"]:
            if task["status"] not in TERMINAL:
                task.update(status="cancelled", error="Swarm cancelled", finished_at=stamp)
        snapshot.update(status="cancelled", updated_at=stamp)
        await persist(snapshot)
        return snapshot


async def reconcile_owned_pending(session_id, run_id, fence_token):
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


async def _reconcile_pending_receipts(session_id, run_id, fence_token):
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
