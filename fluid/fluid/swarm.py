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
import re
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, replace

from fluid.tools.registry import BackgroundTaskReceipt
from fluid.tools.builtins.task import _background_outcome

TERMINAL = {"completed", "failed", "blocked", "cancelled", "interrupted"}
RESULT_LIMIT = 12000
MAX_TASKS = 64


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
    def __init__(self, session_id, dispatch, cancel_worker, publish, finished):
        self.session_id = session_id
        self.dispatch = dispatch
        self.cancel_worker = cancel_worker
        self.publish = publish
        self.finished = finished
        self.swarms = {}
        self.jobs = {}
        self.cancelled = set()
        self.closed = False
        self._publish_lock = asyncio.Lock()

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
        await self.emit(key)  # persist intent BEFORE any launch
        receipt = BackgroundTaskReceipt(task_id=key, description=self.swarms[key]["title"])
        started = ctx.extra.get("background_task_started")
        if started is not None:
            await started(receipt)
        if self.closed:
            self.cancelled.add(key)
        dispatch_ctx = replace(ctx, extra=dict(ctx.extra))
        self.jobs[key] = asyncio.create_task(self._run(key, tasks, cap, dispatch_ctx), name=key)
        return self.snapshot(key), receipt

    async def _run(self, key, specs, cap, ctx):
        swarm = self.swarms[key]
        running = {}
        rows = {t["id"]: t for t in swarm["tasks"]}
        prompts = {t["id"]: t["prompt"] for t in specs}
        try:
            swarm["status"] = "running"
            await self.emit(key)
            while True:
                for task in rows.values():
                    if task["status"] != "queued":
                        continue
                    if key in self.cancelled:
                        task.update(status="cancelled", finished_at=now())
                    elif any(rows[d]["status"] in TERMINAL - {"completed"} for d in task["depends_on"]):
                        task.update(status="blocked", error="Dependency did not complete", finished_at=now())
                    elif len(running) < cap and all(rows[d]["status"] == "completed" for d in task["depends_on"]):
                        task.update(status="running", started_at=now())
                        # Durably record launch intent before entering the dispatcher.
                        await self.emit(key)
                        job = asyncio.create_task(self._task(key, task, prompts[task["id"]], rows, ctx))
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

    async def _task(self, key, task, prompt, rows, ctx):
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
            dispatch = asyncio.create_task(self.dispatch(dict(agent=task["agent"], prompt=prompt,
                label=task["title"], mode="background"), ctx))
            outcome = _background_outcome(await asyncio.shield(dispatch), {})
            receipt = outcome.background_task
            if receipt is not None:
                task.update(task_id=receipt.task_id,
                    worker_session_id=task["worker_session_id"] or receipt.worker_session_id)
                await self.emit(key)
                if key in self.cancelled:
                    await self.cancel_worker(receipt.task_id)
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
                await self.cancel_worker(receipt.task_id)
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
            await self.cancel_worker(receipt.task_id)

    async def cancel(self, key):
        if self.swarms[key]["status"] in TERMINAL:
            return self.snapshot(key)
        self.cancelled.add(key)
        # Do not cancel dispatch coroutines: spawn receipts must not be lost.
        for task in self.swarms[key]["tasks"]:
            if task["status"] == "running" and task["task_id"]:
                await self.cancel_worker(task["task_id"])
        await self.emit(key)
        return self.snapshot(key)

    async def aclose(self):
        self.closed = True
        for key in list(self.jobs):
            if not self.jobs[key].done():
                await self.cancel(key)
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
