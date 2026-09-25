"""The standalone CLI's background-task host.

The ``Task`` and ``Monitor`` builtins only own their shape; running the work is
the embedding application's job, injected through ``ToolCtx.extra`` as
``task_dispatch`` and ``monitor_spawn``. An embedding app has its own
task manager.
This module is the equivalent for ``fluid chat`` / ``fluid run``, so both tools
work out of the box the way Claude Code's Agent and Monitor tools do.

It lives under ``fluid.cli`` rather than ``fluid.engine`` because building a
child engine needs :class:`~fluid.config.model.HarnessConfig` and
:func:`~fluid.config.loader.build_engine`, which sit above the engine layer.
Nothing here is CLI-specific beyond that dependency, so another in-process host
may reuse :class:`CliHost` as is.

It is also the messaging host: ``agent_roster`` lists the workers it holds (plus
``main``, inside a worker) and ``agent_message`` resolves one of those names to
an engine and submits an ``AgentMessage`` to it. Choosing the delivery channel
is the receiving engine's job, not this module's, so a sender never races the
receiver's busy/idle state. A name that resolves to a *finished* worker is
resumed first — rebuilt over its stored session, registered as a new background
task, and started on the message — so being alive is never a precondition for
being reachable.

Two worker kinds, one lifecycle:

* ``monitor_spawn`` runs a shell command as a detached asyncio subprocess,
  buffers its combined stdout/stderr (head + tail, capped), and returns a
  :class:`BackgroundTaskReceipt` immediately. When the process exits, the host
  submits ``BackgroundTaskFinished`` to the parent engine with the exit code
  and output tail as the summary.
* ``task_dispatch`` builds a child :class:`~fluid.engine.native.NativeLoop`
  from the same config and provider client, with its own session in the same
  store. A named agent definition (``.agents/agents/**/*.md`` or
  ``~/.fluid/agents``) supplies the system prompt, the tool restriction, and
  an optional model override; otherwise a generic subagent prompt is used.
  ``mode="sync"`` runs the child inline and returns its final text;
  ``mode="background"`` returns a receipt and submits the final text later.

Workers are asyncio tasks owned by the host, not by the parent turn: the
parent's ``TurnComplete`` or an ``Interrupt`` never cancels them. They end on
their own, through :meth:`CliHost.cancel`, or when the host closes.

Permissions in a child follow the parent's approval policy and rules. When a
child needs to ask, the request goes to the host's ``permission_responder``
(the REPL wires its own y/N prompt in; the one-shot ``run`` path denies). The
child's deny message tells the model where the decision came from.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fluid.events.model import (
    ErrorEvent,
    Event,
    Interrupted,
    PermissionRequest,
    TurnComplete,
)
from fluid.events.ops import (
    AgentMessage,
    BackgroundTaskFinished,
    RespondPermission,
    UserTurn,
)
from fluid.store.base import new_id
from fluid.tools.builtins._util import strip_ansi
from fluid.tools.registry import BackgroundTaskReceipt, ToolCtx, ToolOutcome
from fluid.tools.shell import resolve_shell

log = logging.getLogger(__name__)

PermissionResponder = Callable[[PermissionRequest], Awaitable[str]]

# Output kept for a Monitor summary: this many characters from the start and
# from the end of the stream, the middle dropped with a marker.
MONITOR_HEAD_CHARS = 4_000
MONITOR_TAIL_CHARS = 12_000
# Total buffered per process before the middle is discarded for good.
_MONITOR_BUFFER_CAP = MONITOR_HEAD_CHARS + MONITOR_TAIL_CHARS * 4

GENERIC_SUBAGENT_PROMPT = (
    "You are a subagent working on one delegated task for a parent agent. You "
    "do not see the parent's conversation. Do the work described in the prompt "
    "with the tools available, then reply with a concise report of what you "
    "did and found — that reply is all the parent receives."
)


async def deny_permission(event: PermissionRequest) -> str:
    """Default child responder: deny with a message the model can act on."""
    return "deny"


# ``name [ref]`` — the address form ListAgents prints and SendMessage accepts.
# The ref is only ever needed to break a tie between two agents sharing a name,
# so it stays short enough to type and long enough not to collide.
_REF_CHARS = 6
_ADDRESS_RE = re.compile(r"^(?P<name>.*?)\s*\[(?P<ref>[^\[\]]+)\]$")


def _ref_of(task_id: str) -> str:
    """The disambiguating ref for a worker's task id."""
    return task_id[-_REF_CHARS:]


def _split_ref(to: str) -> tuple[str, str | None]:
    """Split an address into ``(name, ref)``; ref is None when none was given."""
    match = _ADDRESS_RE.match(to.strip())
    if match is None:
        return to.strip(), None
    return match.group("name").strip(), match.group("ref").strip()


def _engine_busy(engine: Any | None) -> bool:
    """True while ``engine`` is running a turn (so a message would queue)."""
    turn = getattr(engine, "_turn_task", None)
    return turn is not None and not turn.done()


@dataclass
class Worker:
    """One host-owned background unit — a monitored process or a child agent."""

    task_id: str
    kind: str  # "monitor" | "task"
    description: str
    # The address a peer messages this worker by (Task's ``label``, else the
    # agent name). Not unique on its own — ``ref`` disambiguates.
    name: str = ""
    # The child engine, while it is alive. Cleared once the worker is closed,
    # which is how a peer message tells "still running" from "needs resuming".
    engine: Any | None = None
    # The agent definition this worker was built from (None for the generic
    # subagent). Kept so a resume rebuilds the same worker — same prompt, same
    # tool restriction, same model — over its stored session.
    definition: Any = None
    swarm_launch: Any = None
    task: asyncio.Task | None = None
    process: asyncio.subprocess.Process | None = None
    worker_session_id: str | None = None
    status: str = "running"  # running | finished | failed | cancelled
    summary: str | None = None
    # A serving process (Monitor with ``serves``): meant to stay up, so it is
    # not work the run or a synchronous Task has to wait for.
    serves: bool = False
    _head: list[str] = field(default_factory=list, repr=False)
    _tail: list[str] = field(default_factory=list, repr=False)
    _head_len: int = field(default=0, repr=False)
    _tail_len: int = field(default=0, repr=False)
    _dropped: bool = field(default=False, repr=False)

    @property
    def done(self) -> bool:
        return self.status != "running"

    # ── output buffer (monitor only) ──

    def feed(self, chunk: str) -> None:
        if self._head_len < MONITOR_HEAD_CHARS:
            take = min(len(chunk), MONITOR_HEAD_CHARS - self._head_len)
            self._head.append(chunk[:take])
            self._head_len += take
            chunk = chunk[take:]
            if not chunk:
                return
        self._tail.append(chunk)
        self._tail_len += len(chunk)
        if self._tail_len > _MONITOR_BUFFER_CAP:
            joined = "".join(self._tail)[-MONITOR_TAIL_CHARS:]
            self._tail = [joined]
            self._tail_len = len(joined)
            self._dropped = True

    def output(self) -> str:
        head = "".join(self._head)
        tail = "".join(self._tail)
        dropped = self._dropped
        if len(tail) > MONITOR_TAIL_CHARS:
            tail = tail[-MONITOR_TAIL_CHARS:]
            dropped = True
        if dropped:
            return f"{head}\n... [output truncated] ...\n{tail}"
        return head + tail


@dataclass
class _Target:
    """What an address resolved to.

    ``engine`` set means the receiver can take a message right now. ``engine``
    None with a ``worker`` means the agent has finished and its session is
    waiting to be resumed — which is a route, not a dead end, so the two cases
    share a type instead of one of them being an error.
    """

    name: str
    engine: Any | None = None
    worker: Worker | None = None


class CliHost:
    """Owns the CLI's background workers and wires them into an engine.

    Build one per parent engine, call :meth:`attach`, and close it alongside
    the engine. ``provider`` is the parent's ProviderClient so child agents
    share the same credentials and HTTP session; ``nested=True`` marks a host
    that serves a child engine, where Task is withheld (no recursive fan-out)
    but Monitor still works.
    """

    def __init__(
        self,
        config: Any,
        store: Any,
        *,
        provider: Any | None = None,
        provider_name: str | None = None,
        permission_responder: PermissionResponder | None = None,
        nested: bool = False,
        parent: CliHost | None = None,
        identity: str = "main",
    ) -> None:
        self.config = config
        self.store = store
        self.provider = provider
        self.provider_name = provider_name
        self.permission_responder: PermissionResponder = (
            permission_responder or deny_permission
        )
        self.nested = nested
        # The host serving the session that started this one, and the name
        # this session answers to there. Together they are the ``main`` route:
        # a worker addresses its parent, and the parent sees the worker's name
        # on the message.
        self.parent = parent
        self.identity = identity
        self.engine: Any | None = None
        self.workers: dict[str, Worker] = {}
        self._closed = False
        self._swarm = None
        self._swarm_lock = asyncio.Lock()
        self.swarm_id = None

    # ── wiring ──

    def attach(self, engine: Any) -> CliHost:
        """Inject the spawners into ``engine.ctx.extra`` and remember the engine."""
        self.engine = engine
        ctx = getattr(engine, "ctx", None)
        if ctx is None:
            return self
        ctx.extra["monitor_spawn"] = self.monitor_spawn
        if not self.nested:
            ctx.extra["task_dispatch"] = self.task_dispatch
            ctx.extra["swarm"] = self.swarm
            ctx.extra["recover_background"] = self._recover_swarms
        # Messaging is wired in a worker too — that is the leg that lets one
        # talk back to the conversation that started it.
        ctx.extra["agent_roster"] = self.agent_roster
        ctx.extra["agent_message"] = self.agent_message
        ctx.extra["cli_host"] = self
        if self.provider is None:
            self.provider = getattr(engine, "provider", None)
        return self

    async def _recover_swarms(self, pending):
        import json
        from fluid.events.model import SwarmUpdated
        from fluid.swarm import recover
        session_id = self.engine.ctx.session_id
        latest = {}
        for event in await self.store.events_since(session_id, -1):
            if isinstance(event, SwarmUpdated):
                latest[event.swarm["id"]] = event.swarm
        completions = []
        for key in pending:
            if not key.startswith("swarm_"):
                continue
            if self._swarm and key in self._swarm.jobs and not self._swarm.jobs[key].done():
                continue
            snapshot = latest.get(key)
            if snapshot:
                restored = recover(snapshot)
                if restored != snapshot:
                    await self.engine._emit(SwarmUpdated(session_id=session_id, swarm=restored))
                summary = json.dumps({"swarm": restored})
            else:
                summary = "Swarm interrupted by host restart; no tasks were relaunched."
            completions.append(BackgroundTaskFinished(task_id=key, status="failed", summary=summary, wake=False))
        return completions

    async def swarm(self, input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        import json
        from fluid.swarm import SwarmCoordinator, recover, validate
        from fluid.events.model import SwarmUpdated

        async with self._swarm_lock:
            if self._swarm is None:
                async def publish(snapshot):
                    await self.engine._emit(SwarmUpdated(session_id=ctx.session_id, swarm=snapshot))

                async def finished(key, status, summary):
                    await self.engine.submit(BackgroundTaskFinished(
                        task_id=key, status="finished" if status == "completed" else "cancelled" if status == "cancelled" else "failed",
                        summary=summary))

                self._swarm = SwarmCoordinator(ctx.session_id, self.task_dispatch,
                    self.cancel, publish, finished)
                for event in await self.store.events_since(ctx.session_id, -1):
                    if isinstance(event, SwarmUpdated):
                        self._swarm.swarms[event.swarm["id"]] = event.swarm
                for key, snapshot in list(self._swarm.swarms.items()):
                    restored = recover(snapshot)
                    self._swarm.swarms[key] = restored
                    if restored != snapshot:
                        await self._swarm.emit(key)
        try:
            receipt = None
            action = input.get("action")
            if action == "start":
                tasks, _ = validate(input)
                for task in tasks:
                    if self._find_agent(task["agent"], ctx) is None:
                        raise ValueError(f"Unknown agent: {task['agent']}")
                snapshot, receipt = await self._swarm.start(input, ctx)
            elif action == "status":
                snapshot = self._swarm.snapshot(input["swarm_id"])
            elif action == "cancel":
                snapshot = await self._swarm.cancel(input["swarm_id"])
            else:
                raise ValueError("action must be start, status or cancel")
            payload = {"swarm": snapshot}
            return ToolOutcome(text=json.dumps(payload), ui=payload, background_task=receipt)
        except (KeyError, ValueError) as exc:
            return ToolOutcome(text=str(exc), is_error=True)

    def _message_workers(self):
        workers = list(self.workers.values())
        if self.parent is not None and self.swarm_id is not None:
            workers.extend(w for w in self.parent.workers.values()
                if w.swarm_launch is not None and w.swarm_launch.swarm_id == self.swarm_id
                and w.engine is not self.engine)
        return workers

    def running(self) -> list[Worker]:
        """Workers still running, serving processes included."""
        return [w for w in self.workers.values() if not w.done]

    def pending(self) -> list[Worker]:
        """Workers whose completion something is waiting for.

        A serving process (a dev server started with ``serves``) is running
        but is not pending: its only completion is its death, and a run or a
        synchronous Task that waited for it would wait forever.
        """
        return [w for w in self.running() if not w.serves]

    def busy(self) -> bool:
        """True while a job runs or a completion still awaits its wake turn."""
        if self.pending() or (self._swarm and any(not job.done() for job in self._swarm.jobs.values())):
            return True
        pending_wake = getattr(self.engine, "_background_completions", None)
        return bool(pending_wake)

    async def wait_idle(self) -> None:
        """Block until every job has delivered its completion."""
        while True:
            tasks = [w.task for w in self.pending() if w.task is not None]
            if self._swarm:
                tasks.extend(job for job in self._swarm.jobs.values() if not job.done())
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel(self, task_id: str) -> bool:
        """Stop one worker and report ``status="cancelled"`` to the parent."""
        if self._swarm and task_id in self._swarm.swarms:
            await self._swarm.cancel(task_id)
            return True
        worker = self.workers.get(task_id)
        if worker is None or worker.done:
            return False
        worker.status = "cancelled"
        await _stop_worker(worker)
        await self._report(worker, "cancelled", worker.summary or "cancelled by the user")
        return True

    async def aclose(self) -> None:
        if self._swarm:
            await self._swarm.aclose()
        self._closed = True
        live = [w for w in self.workers.values() if not w.done]
        for worker in live:
            worker.status = "cancelled"
        await asyncio.gather(*(_stop_worker(w) for w in live), return_exceptions=True)

    async def _report(self, worker: Worker, status: str, summary: str) -> None:
        """Hand a terminal result to the parent engine (idempotent by task_id).

        The worker counts as running until the parent has accepted the result,
        so ``busy()`` never reports idle in the gap between process exit and
        the completion being queued for a wake.
        """
        worker.summary = summary
        if worker.swarm_launch is not None:
            worker.status = status
            worker.swarm_launch.finish(status, summary)
            return
        try:
            if self.engine is not None and not self._closed:
                await self.engine.submit(BackgroundTaskFinished(
                    task_id=worker.task_id,
                    worker_session_id=worker.worker_session_id,
                    status=status,
                    summary=summary,
                ))
        except Exception as exc:  # noqa: BLE001 — the parent may already be closed
            log.warning("could not deliver result of %s: %s", worker.task_id, exc)
        finally:
            worker.status = status

    # ── Monitor ──

    async def monitor_spawn(self, input: dict[str, Any], ctx: ToolCtx) -> BackgroundTaskReceipt:
        command = str(input.get("command") or "").strip()
        if not command:
            raise ValueError("Monitor needs a command")
        cwd = input.get("cwd") or ctx.cwd or None
        spawn_kwargs: dict[str, Any] = {}
        if sys.platform != "win32":
            spawn_kwargs["start_new_session"] = True  # own process group for cancel
        process = await asyncio.create_subprocess_exec(
            *resolve_shell().argv_prefix,
            command,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Same hint Bash gives its children: a monitored test runner or
            # dev server that colourises its output makes the summary
            # unsearchable. Anything that ignores the hint gets its escapes
            # stripped as the stream is read (see _supervise).
            env=dict(os.environ, NO_COLOR="1", FORCE_COLOR="0"),
            **spawn_kwargs,
        )
        serves = bool(input.get("serves"))
        worker = Worker(
            task_id=new_id("mon"),
            kind="monitor",
            description=command,
            process=process,
            serves=serves,
        )
        self.workers[worker.task_id] = worker
        worker.task = asyncio.create_task(
            self._supervise(worker), name=f"fluid-monitor-{worker.task_id}"
        )
        return BackgroundTaskReceipt(
            task_id=worker.task_id, description=command, serves=serves
        )

    async def _supervise(self, worker: Worker) -> None:
        process = worker.process
        assert process is not None and process.stdout is not None
        held = ""  # an escape sequence a chunk boundary cut in half
        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    if held:
                        worker.feed(strip_ansi(held))
                    break
                text, held = _split_trailing_escape(
                    held + chunk.decode("utf-8", errors="replace")
                )
                worker.feed(strip_ansi(text))
            code = await process.wait()
        except asyncio.CancelledError:
            worker.summary = _monitor_summary(worker, None, cancelled=True)
            raise
        except Exception as exc:  # noqa: BLE001
            await self._report(worker, "failed", f"monitor failed: {exc}\n{worker.output()}")
            return
        if worker.status == "cancelled":
            worker.summary = _monitor_summary(worker, code, cancelled=True)
            return
        status = "finished" if code == 0 else "failed"
        await self._report(worker, status, _monitor_summary(worker, code))

    # ── Task ──

    async def task_dispatch(self, input: dict[str, Any], ctx: ToolCtx) -> Any:
        prompt = str(input.get("prompt") or "").strip()
        if not prompt:
            return ToolOutcome(text="Task needs a prompt.", is_error=True)
        agent_name = str(input.get("agent") or "").strip()
        definition = self._find_agent(agent_name, ctx)
        label = input.get("label") or agent_name or "subagent"
        # The Task tool derives an addressable name from the description.
        name = str(input.get("name") or label)
        from fluid.swarm import current_launch
        launch = current_launch.get()
        worker = Worker(
            task_id=new_id("task"), kind="task", description=str(label),
            name=name, definition=definition,
            swarm_launch=launch if launch and launch.coordinator is self._swarm else None,
        )
        try:
            child = await self._build_child(definition, worker)
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(text=f"could not start subagent: {exc}", is_error=True)
        worker.engine = child
        self.workers[worker.task_id] = worker

        if input.get("mode", "sync") == "background":
            worker.task = asyncio.create_task(
                self._run_child_detached(
                    worker, child, worker.worker_session_id, UserTurn(content=prompt),
                ),
                name=f"fluid-task-{worker.task_id}",
            )
            return BackgroundTaskReceipt(
                task_id=worker.task_id,
                worker_session_id=worker.worker_session_id,
                description=worker.description,
            )

        worker.task = asyncio.current_task()
        try:
            status, text = await self._run_child(
                child, worker.worker_session_id, UserTurn(content=prompt),
            )
        finally:
            worker.engine = None
            await child.aclose()
        worker.status = status
        worker.summary = text
        await self._subagent_stop_hooks(worker, status, text)
        return ToolOutcome(text=text, is_error=status != "finished")

    async def _subagent_stop_hooks(self, worker: Worker, status: str, summary: str) -> None:
        """SubagentStop hooks on the parent engine's bus, once a Task worker
        has finished. Observational: the worker is already done, so a block
        is reported and nothing else. Never raises."""
        hooks = getattr(self.engine, "hooks", None)
        session = getattr(self.engine, "_session", None)
        if hooks is None or session is None:
            return
        from fluid.hooks.bus import SUBAGENT_STOP

        try:
            decision = await hooks.emit(SUBAGENT_STOP, {
                "session_id": session.session_id,
                "turn_id": getattr(self.engine, "_active_turn_id", None),
                "task_id": worker.task_id,
                "worker_session_id": worker.worker_session_id,
                "status": status,
                "summary": summary,
            })
        except Exception:  # noqa: BLE001
            log.warning("SubagentStop hooks failed for %s", worker.task_id, exc_info=True)
            return
        for message in decision.warnings:
            log.warning("SubagentStop hook: %s", message)
        if decision.action == "deny":
            log.warning(
                "SubagentStop hook asked to block %s (%s) but the worker had "
                "already finished", worker.task_id, decision.note,
            )

    # ── ListAgents / SendMessage ──

    def agent_roster(self, input: dict[str, Any], ctx: ToolCtx) -> list[dict[str, Any]]:
        """The agents this session can address, newest first.

        A worker is listed while it can still be reached and for as long as
        the host remembers it: a finished one is worth seeing (it says what
        already ran) even though messaging it fails. ``main`` appears only
        inside a worker, where there is a parent to talk to.
        """
        rows: list[dict[str, Any]] = []
        if self.parent is not None:
            rows.append({
                "name": "main",
                "ref": None,
                "status": "running" if _engine_busy(self.parent.engine) else "idle",
                "description": "the conversation that started you",
                "kind": "parent",
            })
        running_only = bool(input.get("running_only"))
        for worker in reversed(self._message_workers()):
            if worker.kind != "task":
                continue
            if running_only and worker.done:
                continue
            rows.append({
                "name": worker.name or worker.description,
                "ref": _ref_of(worker.task_id),
                "status": worker.status,
                "description": worker.description,
                "kind": "subagent",
                "task_id": worker.task_id,
                "worker_session_id": worker.worker_session_id,
            })
        return rows

    async def agent_message(self, input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
        """Deliver one peer message, or explain precisely why it went nowhere.

        Resolution happens here rather than in the tool so the engine handoff
        is one hop: whatever ``to`` names becomes an engine, and the
        ``AgentMessage`` op decides for itself whether that engine is busy
        (queue it) or idle (wake it). Nothing polls, and nothing here has to
        guess at the receiver's state.

        An agent that has already finished is rebuilt over its stored session
        first, so "running" is never a precondition for reaching one — the
        message is what starts it working again.
        """
        to = str(input.get("to") or "").strip()
        target, error = self._resolve_target(to)
        if target is None:
            return ToolOutcome(
                text=error or f"could not resolve {to!r}", is_error=True,
            )
        message = AgentMessage(
            text=str(input.get("message") or ""), sender=self.identity,
            urgent=bool(input.get("urgent")),
        )
        if target.engine is None:
            assert target.worker is not None  # _resolve_target's invariant
            worker, error = await self._resume_worker(target.worker, ctx, message)
            if worker is None:
                return ToolOutcome(
                    text=error or f"could not resume {target.name}", is_error=True,
                )
            return ToolOutcome(
                text=(
                    f"{target.name} had finished, so it was resumed over its "
                    "own transcript and is now working on your message. It "
                    "runs in the background: its report reaches you in a later "
                    "turn, the way a background Task's does."
                ),
                ui={"resumed": worker.task_id, "session": worker.worker_session_id},
            )
        try:
            await target.engine.submit(message)
        except Exception as exc:  # noqa: BLE001 — a closing engine refuses ops
            return ToolOutcome(
                text=f"could not deliver to {target.name}: {exc}", is_error=True,
            )
        if getattr(target.engine, "last_held_message", None) is message:
            return ToolOutcome(
                text=(
                    f"Held for {target.name} until it wakes: it is parked "
                    "waiting on background work and reads your message with "
                    "its next wake. Do not wait for a reply. Send again with "
                    "urgent=true only if you are blocked on its answer."
                )
            )
        return ToolOutcome(
            text=(
                f"Delivered to {target.name}. It picks the message up at its "
                "next tool result if it is working, or starts a turn on it if "
                "it was idle — nothing to wait for here."
            )
        )

    async def _resume_worker(
        self, worker: Worker, ctx: ToolCtx, message: AgentMessage,
    ) -> tuple[Worker | None, str | None]:
        """Rebuild a finished worker over its stored session, ready for a turn.

        The resumed run is registered as a NEW background task rather than a
        second life for the old one: its completion has to travel the ordinary
        park/wake route, and that route settles a task id exactly once. Same
        name, same session, same agent definition — a fresh id.
        """
        if self._closed:
            return None, "this host is shutting down; nothing can be resumed."
        session_id = worker.worker_session_id
        if not session_id:
            return None, (
                f"{worker.name or worker.description} has no stored session to "
                "resume from."
            )
        resumed = Worker(
            task_id=new_id("task"), kind="task", description=worker.description,
            name=worker.name, definition=worker.definition,
        )
        try:
            child = await self._build_child(
                worker.definition, resumed, session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"could not resume {worker.name or worker.description}: {exc}"
        resumed.engine = child
        self.workers[resumed.task_id] = resumed
        resumed.task = asyncio.create_task(
            self._run_child_detached(resumed, child, session_id, message),
            name=f"fluid-task-{resumed.task_id}",
        )
        # Register the run with the parent engine the way Task's background
        # mode does, or its completion would arrive for a task nothing is
        # waiting on — no park, no wake, and a report that goes nowhere.
        started = (ctx.extra or {}).get("background_task_started")
        if started is not None:
            notified = started(BackgroundTaskReceipt(
                task_id=resumed.task_id,
                worker_session_id=session_id,
                description=resumed.description,
            ))
            if inspect.isawaitable(notified):
                await notified
        return resumed, None

    def _resolve_target(self, to: str) -> tuple[_Target | None, str | None]:
        """``to`` as a :class:`_Target`, or ``(None, <why not>)``.

        A target with no ``engine`` is a finished worker the caller may resume;
        one with an engine is ready to take a message now.
        """
        if not to:
            return None, "SendMessage needs a recipient."
        name, ref = _split_ref(to)
        if name.lower() == "main" and ref is None:
            if self.parent is None:
                return None, (
                    "You are the main conversation — there is no `main` to "
                    "message from here."
                )
            if self.parent.engine is None:
                return None, "The parent conversation is no longer running."
            return _Target(name="main", engine=self.parent.engine), None

        matches = [
            w for w in self._message_workers()
            if w.kind == "task" and (
                w.task_id == to
                or (w.name or w.description) == name
                or _ref_of(w.task_id) == (ref or name)
            )
        ]
        if ref is not None:
            matches = [w for w in matches if _ref_of(w.task_id) == ref]
        if not matches:
            known = self.agent_roster({}, ToolCtx())
            listing = ", ".join(
                f"{r['name']} [{r['ref']}]" if r["ref"] else str(r["name"])
                for r in known
            )
            return None, (
                f"No agent named {to!r} in this session."
                + (f" Reachable: {listing}." if listing else "")
                + " Use ListAgents for the current roster."
            )
        # Latest wins on a shared name, but a live agent always beats a
        # finished one: resuming a finished namesake while its twin is still
        # working would split the conversation across two sessions.
        live = [w for w in matches if not w.done]
        candidates = live or matches
        if len(candidates) > 1 and ref is None:
            refs = ", ".join(
                f"{w.name or w.description} [{_ref_of(w.task_id)}] ({w.status})"
                for w in candidates
            )
            return None, (
                f"{name!r} names {len(candidates)} agents. Address one by its "
                f"ref: {refs}."
            )
        worker = candidates[-1]
        display = worker.name or worker.description
        if worker.done and worker.swarm_launch is not None:
            return None, "Swarm worker has finished; it cannot be resumed by a peer message."
        if worker.done:
            # No engine: agent_message resumes it over its stored session.
            return _Target(name=display, worker=worker), None
        if worker.engine is None:
            return None, (
                f"{display} is running but cannot take messages (it was "
                "started synchronously and this session is blocked on it)."
            )
        return _Target(name=display, engine=worker.engine, worker=worker), None

    def _find_agent(self, name: str, ctx: ToolCtx) -> Any | None:
        registry = (ctx.extra or {}).get("agent_registry")
        if isinstance(registry, dict) and name in registry:
            return registry[name]
        from fluid.context.agents import discover_agents

        for definition in discover_agents(
            ctx.cwd or self.config.cwd or ".", include_plugins=True
        ):
            if definition.name == name:
                return definition
        return None

    async def _build_child(
        self, definition: Any | None, worker: Worker, *, session_id: str | None = None,
    ) -> Any:
        from fluid.config.loader import build_engine
        from fluid.context.compiler import PromptInputs, compile_prompt
        from fluid.tools.builtins import (
            refine_builtin_descriptions,
            register_builtins,
        )
        from fluid.tools.registry import ToolRegistry

        # A resume binds the worker's stored session instead of opening a new
        # one: the child comes back with its own transcript behind it, which is
        # the whole difference between resuming an agent and starting a fresh
        # one that happens to share its name.
        if session_id is None:
            meta = await self.store.create_session(cwd=self.config.cwd)
            session_id = meta.session_id
        worker.worker_session_id = session_id

        body = (definition.body.strip() if definition is not None else "") or GENERIC_SUBAGENT_PROMPT
        from fluid.codemode.skills import DEFAULT_SKILLS_ROOT

        compiled = compile_prompt(PromptInputs(
            base=body,
            cwd=self.config.cwd,
            include_env=False,
            include_global_instructions=self.config.instruction_files.enabled,
            include_project_instructions=self.config.instruction_files.enabled,
            include_skills=False,
            include_agents=False,
            include_plugins=False,
            # Subagents share the learner's memory snapshot: a worker that
            # doesn't know the user's conventions re-derives (or violates)
            # them, and its journal evidence feeds the same store.
            memories_dir=(
                (self.config.skills_dir or str(DEFAULT_SKILLS_ROOT))
                if self.config.continual
                else None
            ),
            setting_sources=self.config.setting_sources,
        ))

        registry = ToolRegistry()
        register_builtins(registry)
        # The worker's surface sits inside the parent's: a read-only session
        # must not spawn a worker that can write. The parent engine's own
        # lists are the live restriction (they may be narrower than the
        # config's); an agent definition's ``tools`` narrows further.
        parent_allowed = getattr(self.engine, "allowed_tools", None)
        if parent_allowed is None:
            parent_allowed = self.config.tools.allowed
        allowed: list[str] | None = (
            list(parent_allowed) if parent_allowed is not None else None
        )
        if definition is not None and definition.tools:
            from fluid.engine.base import matches_tool_pattern

            wanted = list(definition.tools)
            if allowed is not None:
                kept = [t for t in wanted if matches_tool_pattern(t, allowed)]
                if len(kept) < len(wanted):
                    log.info(
                        "subagent %s: %s outside the parent's tool allowlist, dropped",
                        definition.name, sorted(set(wanted) - set(kept)),
                    )
                wanted = kept
            allowed = wanted
        # No recursive fan-out inside a worker.
        parent_disallowed = getattr(self.engine, "disallowed_tools", None) or ()
        disallowed = list(dict.fromkeys([
            "Task",
            *self.config.tools.disallowed, *parent_disallowed,
        ]))
        refine_builtin_descriptions(
            registry,
            allowed=allowed,
            disallowed=disallowed,
        )

        permissions = None
        parent_perms = getattr(self.engine, "permissions", None)
        if parent_perms is not None:
            from fluid.permissions.engine import PermissionEngine

            permissions = PermissionEngine(
                parent_perms.approval_policy, rules=list(parent_perms.rules)
            )

        child = build_engine(
            self.config,
            self.store,
            provider=self.provider,
            provider_name=self.provider_name,
            session_id=session_id,
            system=compiled.text,
            system_sections=compiled,
            registry=registry,
            permissions=permissions,
            ctx=ToolCtx(cwd=self.config.cwd, session_id=session_id),
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            # One learner per store: the worker journals into the parent's
            # journal (stamped with its agent name) and the parent's
            # synthesizer reads it. A synthesizer per worker paid for the
            # same slice N times and wrote near-duplicate memories.
            synthesizer=self._learner(),
            agent=definition.name if definition is not None else "subagent",
        )
        if definition is not None and definition.model:
            child.model = definition.model
        # Effort is a live session option, not part of HarnessConfig. A saved
        # agent profile must not replace the caller's effective setting. None
        # leaves the selected model's default intact; an explicit child-session
        # change can still override this after construction.
        child.effort = getattr(self.engine, "effort", None)
        child_host = CliHost(
            self.config, self.store,
            provider=self.provider, provider_name=self.provider_name,
            permission_responder=self.permission_responder, nested=True,
            parent=self, identity=worker.name or worker.description,
        ).attach(child)
        if worker.swarm_launch is not None:
            child_host.swarm_id = worker.swarm_launch.swarm_id
        return child

    def _learner(self) -> Any | None:
        """The parent engine's synthesizer, whichever recorder holds it."""
        for holder in ("_codemode", "_journal"):
            synthesizer = getattr(getattr(self.engine, holder, None), "_synthesizer", None)
            if synthesizer is not None:
                return synthesizer
        return None

    async def _run_child(self, child: Any, session_id: str, op: Any) -> tuple[str, str]:
        """Run one child turn to completion. Returns ``(status, final_text)``.

        ``op`` is whatever starts the turn: a ``UserTurn`` for a fresh brief,
        an ``AgentMessage`` when a peer resumes a worker over its stored
        session. Both start a turn on an idle engine, so nothing below cares
        which arrived.
        """
        await child.submit(op)
        status = "finished"
        error: str | None = None
        async with contextlib.aclosing(
            child.bus.subscribe(session_id, from_store=False)
        ) as events:
            async for event in events:
                if isinstance(event, PermissionRequest):
                    decision = await self.permission_responder(event)
                    await child.submit(RespondPermission(
                        request_id=event.request_id,
                        decision=decision,
                        message=None if decision in ("allow", "allow_always") else (
                            "denied by the parent session's permission policy"
                        ),
                    ))
                elif isinstance(event, TurnComplete):
                    break
                elif isinstance(event, Interrupted):
                    status = "cancelled"
                    break
                elif isinstance(event, ErrorEvent):
                    status = "failed"
                    error = event.message
                    break
        await child.wait_turn()
        host = child.ctx.extra.get("cli_host")
        if isinstance(host, CliHost):
            await host.wait_idle()
            await child.wait_turn()
        text = await _final_assistant_text(child, session_id)
        if error and not text:
            text = f"subagent error: {error}"
        return status, text or "(the subagent returned no text)"

    async def _run_child_detached(
        self, worker: Worker, child: Any, session_id: str, op: Any
    ) -> None:
        try:
            status, text = await self._run_child(child, session_id, op)
        except asyncio.CancelledError:
            worker.summary = "subagent cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            status, text = "failed", f"subagent crashed: {exc}"
        finally:
            worker.engine = None
            try:
                await child.aclose()
            except Exception:  # noqa: BLE001
                pass
        if worker.status == "cancelled":
            return
        await self._subagent_stop_hooks(worker, status, text)
        await self._report(worker, status, text)


# A read(4096) can land in the middle of an escape sequence, and half a
# sequence survives strip_ansi as literal garbage. The unterminated tail is
# held back for the next chunk — up to a bound, so a lone ESC in binary-ish
# output cannot stall the buffer forever.
_TRAILING_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*)?\Z")
_MAX_HELD_CHARS = 256


def _split_trailing_escape(text: str) -> tuple[str, str]:
    """Split ``text`` into (ready to strip, incomplete escape held back)."""
    match = _TRAILING_ESCAPE.search(text)
    if match is None or len(text) - match.start() > _MAX_HELD_CHARS:
        return text, ""
    return text[: match.start()], text[match.start():]


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Kill a shell-spawned command together with every descendant.

    ``Process.kill`` only reaches the shell; the real command would keep the
    stdout pipe open and ``wait()`` would never return. Windows uses
    ``taskkill /T``; POSIX kills the process group the command was started in.
    """
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def _stop_worker(worker: Worker) -> None:
    """Kill a worker's process tree (if any), settle its task, and reap both."""
    process = worker.process
    task = worker.task
    current = asyncio.current_task()
    if process is not None:
        await _kill_tree(process)
        # With the tree gone the supervisor reads EOF and finishes on its own;
        # cancelling a pending pipe read on the Windows proactor loop can leave
        # the transport half-closed, so cancel only as a fallback.
        if task is not None and task is not current and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    if task is not None and task is not current and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if process is not None and process.returncode is None:
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, Exception):  # noqa: BLE001
            pass


def _monitor_summary(worker: Worker, code: int | None, *, cancelled: bool = False) -> str:
    head = (
        "cancelled" if cancelled
        else f"exit code {code}" if code is not None
        else "exited"
    )
    out = worker.output().rstrip()
    return f"`{worker.description}` — {head}\n{out}" if out else f"`{worker.description}` — {head}"


async def _final_assistant_text(child: Any, session_id: str) -> str:
    try:
        nodes = await child.store.load_path(session_id)
    except Exception:  # noqa: BLE001
        return ""
    for node in reversed(nodes):
        if node.message.role == "assistant":
            text = node.message.text().strip()
            if text:
                return text
    return ""


def render_worker_event(event: Event) -> str | None:
    """One-line rendering of park/wake/subagent events for the CLI output."""
    from fluid.events.model import SessionParked, SessionWoken, SubagentEvent

    if isinstance(event, SubagentEvent):
        label = f" {event.description}" if event.description else ""
        if event.kind == "started":
            return f"[background] started {event.task_id}{label}"
        if event.kind in ("finished", "failed", "cancelled"):
            return f"[background] {event.kind} {event.task_id}{label}"
        return None
    if isinstance(event, SessionParked):
        ids = ", ".join(event.pending_task_ids)
        return f"[parked] waiting on background work: {ids}"
    if isinstance(event, SessionWoken):
        ids = ", ".join(event.task_ids)
        return f"[woken] continuing with results from: {ids}"
    return None


__all__ = [
    "GENERIC_SUBAGENT_PROMPT",
    "CliHost",
    "Worker",
    "deny_permission",
    "render_worker_event",
]
