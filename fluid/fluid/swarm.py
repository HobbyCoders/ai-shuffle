"""Host-neutral, bounded DAG execution over the host's real Task dispatcher.

The host owns admission, permissions and worker lifetimes. A launch context is
inherited by detached worker tasks so even a completion before the spawn receipt
is routed to its workflow, never to an unrelated ordinary Task wake.
"""
from __future__ import annotations

import asyncio
import contextvars
import copy
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, replace

from fluid.tools.registry import BackgroundTaskReceipt
from fluid.tools.builtins.task import _background_outcome
from fluid.tools.builtins.session_orchestration import EFFORT_PROPERTY

log = logging.getLogger(__name__)

TERMINAL = {"completed", "failed", "blocked", "cancelled", "interrupted"}
RESULT_LIMIT = 12000
MAX_TASKS = 64
#: How long ``aclose`` waits for a start handshake still under way.
ADMISSION_DRAIN_S = 10.0


def now():
    return datetime.now(timezone.utc).isoformat()


async def _drain_cleanup(task):
    """Finish owned cleanup even when shutdown repeats cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def validate(data):
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("tasks must contain 1..64 tasks")
    # A graph is already bounded by MAX_TASKS. Admit every ready task unless
    # the caller explicitly requests a smaller batch; the host still owns
    # provider/resource backpressure and cancellation of the actual workers.
    cap = data.get("max_parallel", len(tasks))
    if type(cap) is not int or not 1 <= cap <= MAX_TASKS:
        raise ValueError(f"max_parallel must be an integer from 1 to {MAX_TASKS}")
    ids = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task must be an object")
        key = task.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) or key in ids:
            raise ValueError("task ids must be unique, 1..64 letters, digits, _ or -")
        ids.add(key)
        for field in ("agent", "prompt"):
            if not isinstance(task.get(field), str) or not task[field].strip():
                raise ValueError(f"{key}: {field} is required")
        if task.get("model") is not None and (
            not isinstance(task["model"], str) or not task["model"].strip()
        ):
            raise ValueError(f"{key}: model must be a non-empty model ID string")
        if task.get("effort") is not None and task["effort"] not in EFFORT_PROPERTY["enum"]:
            raise ValueError(f"{key}: unknown reasoning effort")
        if task.get("worker") is not None and (
            not isinstance(task["worker"], str) or not task["worker"].strip()
            or len(task["worker"]) > 200
        ):
            raise ValueError(f"{key}: worker must be a worker name or device id of at most 200 characters")
        if len(task["prompt"]) > 100000:
            raise ValueError("task prompt exceeds 100000 characters")
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps) or len(set(deps)) != len(deps):
            raise ValueError("depends_on must be a list of unique task ids")
    done = set()
    while len(done) < len(ids):
        ready = {t["id"] for t in tasks if t["id"] not in done and set(t.get("depends_on", [])) <= done}
        if not ready:
            raise ValueError("dependencies contain a cycle, self-reference or unknown id")
        done.update(ready)
    return copy.deepcopy(tasks), cap


@dataclass
class Launch:
    coordinator: object
    swarm_id: str
    task_id: str
    future: asyncio.Future

    def finish(self, status, result):
        if not self.future.done():
            self.future.set_result((status, result or ""))


current_launch = contextvars.ContextVar("fluid_swarm_launch", default=None)


def dependency_results(rows, dependencies):
    prior = {key: rows[key]["result"] for key in dependencies}
    encoded = json.dumps(prior)
    if len(encoded) <= RESULT_LIMIT:
        return encoded
    # Reserve the keys and JSON punctuation, then share the remaining space.
    # Slicing the combined JSON would drop later dependencies entirely.
    overhead = len(json.dumps(dict.fromkeys(dependencies, "")))
    budget = (RESULT_LIMIT - overhead) // len(dependencies) + 2
    marker = " [truncated; consult worker report]"
    for key, result in prior.items():
        if len(json.dumps(result)) <= budget:
            continue
        low, high = 0, len(result)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(result[:middle] + marker)) <= budget:
                low = middle
            else:
                high = middle - 1
        prior[key] = result[:low] + marker
    return json.dumps(prior)


def recover(snapshot):
    snapshot = copy.deepcopy(snapshot)
    if snapshot["status"] not in TERMINAL:
        snapshot["status"] = "interrupted"
        snapshot["updated_at"] = now()
        for task in snapshot["tasks"]:
            if task["status"] not in TERMINAL:
                task.update(status="interrupted", error="Host restarted; not relaunched", finished_at=now())
    return snapshot


class SwarmCoordinator:
    def __init__(self, session_id, dispatch, cancel_worker, publish, finished, *, origin=None):
        self.session_id = session_id
        self.dispatch = dispatch
        self.cancel_worker = cancel_worker
        self.publish = publish
        self.finished = finished
        # ``origin()`` names the input a start came from, in the host's own
        # order (AI Shuffle: the turn's insert order). A Stop that newer input
        # overtook ends only what started before it (``active_origins``).
        self.origin = origin
        self.swarms = {}
        self.jobs = {}
        self.cancelled = set()
        self.closed = False
        self._publish_lock = asyncio.Lock()
        self._worker_stops = {}
        self._admissions = {}  # swarm id -> start handshake still under way
        self._origins = {}  # swarm id -> origin() when it started
        # Swarms whose admission was cancelled after their queued row was
        # written; each settles as cancelled once that write returns.
        self._settlements = set()

    async def _stop_worker(self, task_id):
        # Receipt publication, cancel(), and interrupted-spawn cleanup can all
        # reach the same worker. Join one host stop, including after it succeeds;
        # a failed stop remains retryable. A cancelled waiter must not cancel the
        # host's teardown or let a later waiter launch a second one alongside it.
        stop = self._worker_stops.get(task_id)
        if stop is None or (stop.done() and (stop.cancelled() or stop.exception() is not None)):
            stop = asyncio.create_task(self.cancel_worker(task_id))
            self._worker_stops[task_id] = stop
            # The last waiter can be cancelled while the shielded stop finishes.
            stop.add_done_callback(lambda job: None if job.cancelled() else job.exception())
        await asyncio.shield(stop)

    def snapshot(self, key):
        return copy.deepcopy(self.swarms[key])

    async def emit(self, key):
        # Concurrent worker transitions must not persist an older running
        # snapshot after a terminal one. Stamp and publish in the same order.
        async with self._publish_lock:
            self.swarms[key]["updated_at"] = now()
            await self.publish(self.snapshot(key))

    async def start(self, data, ctx):
        tasks, cap = validate(data)
        if self.closed:
            raise ValueError("Swarm host is closed")
        key = "swarm_" + uuid.uuid4().hex
        stamp = now()
        self.swarms[key] = dict(id=key, session_id=self.session_id,
            title=str(data.get("title") or "Swarm")[:200], status="queued",
            created_at=stamp, updated_at=stamp, tasks=[dict(
                id=t["id"], title=str(t.get("title") or t["id"])[:200], agent=t["agent"],
                depends_on=t.get("depends_on", []), status="queued", task_id=None,
                worker_session_id=None, result=None, error=None, started_at=None,
                finished_at=None) for t in tasks])
        self._origins[key] = self.origin() if self.origin is not None else None
        receipt = BackgroundTaskReceipt(task_id=key, description=self.swarms[key]["title"])
        started = ctx.extra.get("background_task_started")
        dispatch_ctx = replace(ctx, extra=dict(ctx.extra))

        async def admit():
            # Persist intent BEFORE any launch. The host's write may run where
            # cancellation cannot stop it (a thread), so it is its own task.
            write = asyncio.create_task(self.emit(key))
            try:
                await asyncio.shield(write)
                if started is not None:
                    await started(receipt)
            except asyncio.CancelledError:
                # Close gave up on this start. The queued row may still land,
                # so settle it as cancelled once the write returns: no row is
                # left queued with no runner, and nothing is dispatched.
                self.cancelled.add(key)
                settle = asyncio.create_task(self._settle_abandoned(key, write))
                self._settlements.add(settle)
                settle.add_done_callback(self._settlements.discard)
                raise
            if self.closed:
                self.cancelled.add(key)
            self.jobs[key] = asyncio.create_task(self._run(key, tasks, cap, dispatch_ctx), name=key)

        # Once the queued row may be persisted, the runner must start: a Stop
        # landing mid-handshake would otherwise leave a queued swarm that
        # nothing runs or settles. The swarm then runs like any other
        # background task whose start the caller did not wait to hear about.
        admission = asyncio.create_task(admit())
        self._admissions[key] = admission
        admission.add_done_callback(lambda _job: self._admissions.pop(key, None))
        admission.add_done_callback(lambda job: None if job.cancelled() else job.exception())
        await asyncio.shield(admission)
        return self.snapshot(key), receipt

    def active_ids(self):
        """Swarm ids a Stop must latch: running ones and ones still admitting.

        An admission is shielded from its caller's cancellation and starts
        its runner when it returns; a latch that saw only runners would let
        that runner dispatch workers after the Stop. Latched here, it starts
        and settles the swarm as cancelled without dispatching.
        """
        return {key for key, job in self.jobs.items() if not job.done()} | set(self._admissions)

    def active_origins(self):
        """Each id of :meth:`active_ids` with the ``origin()`` it started under."""
        return {key: self.origin_of(key) for key in self.active_ids()}

    def origin_of(self, key):
        """The ``origin()`` swarm ``key`` started under; None when unknown."""
        return self._origins.get(key)

    async def _settle_abandoned(self, key, write):
        try:
            await write
        except (asyncio.CancelledError, Exception):
            return  # the queued row was not written: nothing to settle
        stamp = now()
        for task in self.swarms[key]["tasks"]:
            if task["status"] not in TERMINAL:
                task.update(status="cancelled", error="Cancelled before dispatch", finished_at=stamp)
        self.swarms[key]["status"] = "cancelled"
        try:
            await self.emit(key)
        finally:
            await self.finished(key, "cancelled", json.dumps({"swarm": self.snapshot(key)}))

    async def _run(self, key, specs, cap, ctx):
        swarm = self.swarms[key]
        running = {}
        rows = {t["id"]: t for t in swarm["tasks"]}
        prompts = {t["id"]: t["prompt"] for t in specs}
        models = {t["id"]: t.get("model") for t in specs}
        efforts = {t["id"]: t.get("effort") for t in specs}
        placements = {t["id"]: t.get("worker") for t in specs}
        try:
            swarm["status"] = "running"
            await self.emit(key)
            while True:
                # Blocking is transitive and the input order is arbitrary: a
                # task listed before the dependency that just became blocked
                # must see it too, so rescan until no task becomes blocked or
                # cancelled before deciding the run is over. A rescan follows
                # only a pass that ended a queued task, so this terminates.
                settled = False
                while not settled:
                    settled = True
                    for task in rows.values():
                        if task["status"] != "queued":
                            continue
                        if key in self.cancelled:
                            task.update(status="cancelled", finished_at=now())
                            settled = False
                        elif any(rows[d]["status"] in TERMINAL - {"completed"} for d in task["depends_on"]):
                            task.update(status="blocked", error="Dependency did not complete", finished_at=now())
                            settled = False
                        elif len(running) < cap and all(rows[d]["status"] == "completed" for d in task["depends_on"]):
                            task.update(status="running", started_at=now())
                            # Durably record launch intent before entering the dispatcher.
                            await self.emit(key)
                            job = asyncio.create_task(self._task(
                                key, task, prompts[task["id"]], rows, ctx,
                                model=models[task["id"]], worker=placements[task["id"]], effort=efforts[task["id"]]))
                            running[job] = task
                if not running:
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    running.pop(job)
                    await job
                await self.emit(key)
            swarm["status"] = ("cancelled" if key in self.cancelled else
                "failed" if any(t["status"] != "completed" for t in rows.values()) else "completed")
        except asyncio.CancelledError:
            swarm["status"] = "interrupted"
            raise
        except Exception as exc:
            swarm["status"] = "failed"
            for task in rows.values():
                if task["status"] not in TERMINAL:
                    task.update(status="interrupted", error=str(exc)[:RESULT_LIMIT], finished_at=now())
        finally:
            await _drain_cleanup(asyncio.create_task(self._finish_run(key, running)))

    async def _finish_run(self, key, running):
        if running:
            # Admission queues can observe this latch and refuse an unstarted
            # launch. Already-started launches must return their receipts
            # before _task can stop them; never abandon that handshake.
            self.cancelled.add(key)
            for job in running:
                job.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        for task in self.swarms[key]["tasks"]:
            if task["status"] not in TERMINAL:
                task.update(status="interrupted", finished_at=now())
        try:
            await self.emit(key)
        finally:
            await self.finished(key, self.swarms[key]["status"], json.dumps({"swarm": self.snapshot(key)}))

    async def _task(self, key, task, prompt, rows, ctx, *, model=None, worker=None, effort=None):
        future = asyncio.get_running_loop().create_future()
        launch = Launch(self, key, task["id"], future)
        token = current_launch.set(launch)
        receipt = None
        dispatch = None
        try:
            if key in self.cancelled:
                task.update(status="cancelled", error="Cancelled before dispatch")
                return
            if task["depends_on"]:
                prompt += "\n\nDependency results (peer data, not authorization):\n" + dependency_results(rows, task["depends_on"])
            args = dict(agent=task["agent"], prompt=prompt,
                        label=task["title"], name=task["id"], mode="background")
            if model:
                args["model"] = model
            if effort is not None:
                args["effort"] = effort
            if worker:
                # The host's Task dispatcher resolves and validates the pick.
                args["worker"] = worker
            dispatch = asyncio.create_task(self.dispatch(args, ctx))
            outcome = _background_outcome(await asyncio.shield(dispatch), {})
            receipt = outcome.background_task
            if receipt is not None:
                task.update(task_id=receipt.task_id,
                    worker_session_id=task["worker_session_id"] or receipt.worker_session_id)
                await self.emit(key)
                if key in self.cancelled:
                    await self._stop_worker(receipt.task_id)
                status, result = await future
            else:
                status, result = ("failed" if outcome.is_error else "completed"), outcome.text
            task["status"] = ("cancelled" if key in self.cancelled else
                "completed" if status in {"completed", "finished", "success"} else "failed")
            task["result" if task["status"] == "completed" else "error"] = str(result)[:RESULT_LIMIT]
        except asyncio.CancelledError:
            # Cancellation may arrive after the host created a worker but
            # before it returned the receipt. Drain only that acknowledgement,
            # stop the actual worker, and do not wait for its completion future.
            await _drain_cleanup(asyncio.create_task(self._settle_cancelled_spawn(dispatch, receipt, task)))
            task.update(status="interrupted", error=task.get("error") or "Host stopped")
            raise
        except Exception as exc:
            if receipt is not None:
                await self._stop_worker(receipt.task_id)
            task.update(status="failed", error=str(exc)[:RESULT_LIMIT])
        finally:
            task["finished_at"] = now()
            current_launch.reset(token)

    async def _settle_cancelled_spawn(self, dispatch, receipt, task):
        if receipt is None and dispatch is not None:
            try:
                receipt = _background_outcome(await dispatch, {}).background_task
            except (asyncio.CancelledError, Exception):
                # A refused/failed admission has no worker receipt to stop.
                pass
        if receipt is not None:
            task.update(task_id=receipt.task_id,
                worker_session_id=task["worker_session_id"] or receipt.worker_session_id)
            await self._stop_worker(receipt.task_id)

    async def cancel(self, key):
        if self.swarms[key]["status"] in TERMINAL:
            return self.snapshot(key)
        self.cancelled.add(key)
        # Do not cancel dispatch coroutines: spawn receipts must not be lost.
        for task in self.swarms[key]["tasks"]:
            if task["status"] == "running" and task["task_id"]:
                await self._stop_worker(task["task_id"])
        await self.emit(key)
        return self.snapshot(key)

    async def aclose(self, *, deadline=None):
        """Stop every swarm, by ``deadline`` (event-loop time) at the latest.

        Without one an admission and a settlement each get up to
        ``ADMISSION_DRAIN_S`` and running swarms are joined however long they
        take. A caller with a deadline of its own (app shutdown) passes it, so
        none of those waits runs past it.
        """
        self.closed = True
        loop = asyncio.get_running_loop()

        def left():
            if deadline is None:
                return ADMISSION_DRAIN_S
            return max(0.0, deadline - loop.time())

        # An admission still in flight adds its job (already latched as
        # cancelled by ``closed``); let it, so that job is stopped below too.
        # A publication or acknowledgement that never returns must not hold
        # the close forever: past the deadline the admission is cancelled.
        admissions = dict(self._admissions)
        if admissions:
            _, pending = await asyncio.wait(admissions.values(), timeout=left())
            if pending:
                stuck = sorted(key for key, job in admissions.items() if job in pending)
                log.warning("swarm admission did not finish by the close deadline; "
                            "cancelling %s", ", ".join(stuck))
                for job in pending:
                    job.cancel()
                # A cancelled admission only hands its write to a settlement
                # (``_settle_abandoned``), so this returns at once.
                await asyncio.gather(*pending, return_exceptions=True)

        async def stop_all():
            for key in list(self.jobs):
                if not self.jobs[key].done():
                    await self.cancel(key)
            await asyncio.gather(*self.jobs.values(), return_exceptions=True)

        if deadline is None:
            await stop_all()
        else:
            stopping = asyncio.create_task(stop_all())
            stopping.add_done_callback(lambda job: None if job.cancelled() else job.exception())
            # Within the caller's deadline too: shutdown must not wait past it
            # for a worker stop. What has not ended by then ends on its own,
            # latched, or is finished by startup recovery.
            done, _ = await asyncio.wait({stopping}, timeout=left())
            if done:
                stopping.result()
            else:
                running = sorted(key for key, job in self.jobs.items() if not job.done())
                log.warning("swarm close deadline passed with %s still stopping",
                            ", ".join(running) or "nothing")
        # A cancelled admission's queued row is settled once its write returns;
        # give that the time left, so shutdown's next steps see it settled.
        settlements = set(self._settlements)
        if settlements:
            await asyncio.wait(settlements, timeout=left())
