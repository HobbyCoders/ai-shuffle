"""AI Shuffle host for Fluid's Swarm coordinator and scoped native messaging."""
from __future__ import annotations
import asyncio
import json
from fluid.swarm import SwarmCoordinator, current_launch, recover, validate
from fluid.tools.registry import ToolOutcome

coordinators = {}
workers = {}  # app worker session id -> trusted Launch
worker_jobs = {}  # initial receipt id -> dispatch/retry task
_locks = {}
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
    from app.db.session_events import get_events
    async with _locks.setdefault(session_id, asyncio.Lock()):
        latest, cursor = {}, 0
        while True:
            page = await asyncio.to_thread(get_events, session_id, cursor, 500)
            if not page:
                break
            for event in page:
                if event["event_type"] == "swarm.updated":
                    snapshot = event["payload"].get("swarm")
                    if isinstance(snapshot, dict):
                        latest[snapshot["id"]] = snapshot
            cursor = page[-1]["seq"]
        return list(latest.values())

async def bind_worker(worker_id, owner_session_id):
    """Called by the real dispatch producer before each admitted attempt."""
    launch = current_launch.get()
    if launch is None or launch.coordinator.session_id != owner_session_id:
        return
    workers[worker_id] = launch
    row = next(t for t in launch.coordinator.swarms[launch.swarm_id]["tasks"] if t["id"] == launch.task_id)
    row["worker_session_id"] = worker_id
    await launch.coordinator.emit(launch.swarm_id)

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

async def cancel_worker(task_id):
    from app.core.provider_agent_supervisor import _kill_worker_session
    launch = workers.get(task_id)
    ids = [key for key, value in workers.items() if value is launch] if launch else [task_id]
    await asyncio.gather(*(_kill_worker_session(key) for key in ids))
    job = worker_jobs.get(task_id)
    if job is not None:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
    if launch:
        launch.finish("cancelled", "Cancelled by user")

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


async def reconcile_owned_pending(session_id, run_id, fence_token):
    """Repair pooled Swarm receipts only at the runtime's leased reader boundary.

    Connect and turn-end reconciliation still own general Task hygiene. Here we
    also publish the interrupted workflow that receipt-only repair cannot fix.
    No pool/Swarm receipt means no lease query or session-history scan.
    """
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
                            status=wire_status, tool_use_id=key, summary=summary,
                            origin=turn_scheduler.PROVIDER_AGENT_ORIGIN))
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
        # The durable interrupt command targets an entire session/turn, not a
        # single workflow. Do not silently cancel unrelated work as a fallback.
        raise SwarmUnavailableError("Swarm cancellation is unavailable on this runner; contact the execution owner")
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
                rows.append(dict(name=task["id"], ref=target, worker_session_id=target,
                    task_id=task["task_id"], status=task["status"], kind="subagent", description=task["title"]))
    return rows
