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
import logging
import os
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
from fluid.events.ops import BackgroundTaskFinished, RespondPermission, UserTurn
from fluid.store.base import new_id
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


@dataclass
class Worker:
    """One host-owned background unit — a monitored process or a child agent."""

    task_id: str
    kind: str  # "monitor" | "task"
    description: str
    task: asyncio.Task | None = None
    process: asyncio.subprocess.Process | None = None
    worker_session_id: str | None = None
    status: str = "running"  # running | finished | failed | cancelled
    summary: str | None = None
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
    ) -> None:
        self.config = config
        self.store = store
        self.provider = provider
        self.provider_name = provider_name
        self.permission_responder: PermissionResponder = (
            permission_responder or deny_permission
        )
        self.nested = nested
        self.engine: Any | None = None
        self.workers: dict[str, Worker] = {}
        self._closed = False

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
        ctx.extra["cli_host"] = self
        if self.provider is None:
            self.provider = getattr(engine, "provider", None)
        return self

    def running(self) -> list[Worker]:
        return [w for w in self.workers.values() if not w.done]

    def busy(self) -> bool:
        """True while a worker runs or a completion still awaits its wake turn."""
        if self.running():
            return True
        pending_wake = getattr(self.engine, "_background_completions", None)
        return bool(pending_wake)

    async def wait_idle(self) -> None:
        """Block until every worker has delivered its completion."""
        while True:
            tasks = [w.task for w in self.running() if w.task is not None]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel(self, task_id: str) -> bool:
        """Stop one worker and report ``status="cancelled"`` to the parent."""
        worker = self.workers.get(task_id)
        if worker is None or worker.done:
            return False
        worker.status = "cancelled"
        await _stop_worker(worker)
        await self._report(worker, "cancelled", worker.summary or "cancelled by the user")
        return True

    async def aclose(self) -> None:
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
            **spawn_kwargs,
        )
        worker = Worker(
            task_id=new_id("mon"),
            kind="monitor",
            description=command,
            process=process,
        )
        self.workers[worker.task_id] = worker
        worker.task = asyncio.create_task(
            self._supervise(worker), name=f"fluid-monitor-{worker.task_id}"
        )
        return BackgroundTaskReceipt(task_id=worker.task_id, description=command)

    async def _supervise(self, worker: Worker) -> None:
        process = worker.process
        assert process is not None and process.stdout is not None
        try:
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                worker.feed(chunk.decode("utf-8", errors="replace"))
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
        worker = Worker(task_id=new_id("task"), kind="task", description=str(label))
        try:
            child = await self._build_child(definition, worker)
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(text=f"could not start subagent: {exc}", is_error=True)
        self.workers[worker.task_id] = worker

        if input.get("mode", "sync") == "background":
            worker.task = asyncio.create_task(
                self._run_child_detached(worker, child, worker.worker_session_id, prompt),
                name=f"fluid-task-{worker.task_id}",
            )
            return BackgroundTaskReceipt(
                task_id=worker.task_id,
                worker_session_id=worker.worker_session_id,
                description=worker.description,
            )

        worker.task = asyncio.current_task()
        try:
            status, text = await self._run_child(child, worker.worker_session_id, prompt)
        finally:
            await child.aclose()
        worker.status = status
        worker.summary = text
        return ToolOutcome(text=text, is_error=status != "finished")

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

    async def _build_child(self, definition: Any | None, worker: Worker) -> Any:
        from fluid.config.loader import build_engine
        from fluid.context.compiler import PromptInputs, compile_prompt
        from fluid.tools.builtins import register_builtins
        from fluid.tools.registry import ToolRegistry

        meta = await self.store.create_session()
        worker.worker_session_id = meta.session_id

        body = (definition.body.strip() if definition is not None else "") or GENERIC_SUBAGENT_PROMPT
        compiled = compile_prompt(PromptInputs(
            base=body,
            cwd=self.config.cwd,
            include_env=False,
            include_global_instructions=self.config.instruction_files.enabled,
            include_project_instructions=self.config.instruction_files.enabled,
            include_skills=False,
            include_agents=False,
            include_plugins=False,
            setting_sources=self.config.setting_sources,
        ))

        registry = ToolRegistry()
        register_builtins(registry)
        allowed = None
        if definition is not None and definition.tools:
            allowed = list(definition.tools)
        disallowed = ["Task", *self.config.tools.disallowed]

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
            session_id=meta.session_id,
            system=compiled.text,
            system_sections=compiled,
            registry=registry,
            permissions=permissions,
            ctx=ToolCtx(cwd=self.config.cwd, session_id=meta.session_id),
            allowed_tools=allowed,
            disallowed_tools=disallowed,
        )
        if definition is not None and definition.model:
            child.model = definition.model
        CliHost(
            self.config, self.store,
            provider=self.provider, provider_name=self.provider_name,
            permission_responder=self.permission_responder, nested=True,
        ).attach(child)
        return child

    async def _run_child(self, child: Any, session_id: str, prompt: str) -> tuple[str, str]:
        """Run one child turn to completion. Returns ``(status, final_text)``."""
        await child.submit(UserTurn(content=prompt))
        status = "finished"
        error: str | None = None
        async for event in child.bus.subscribe(session_id):
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
        self, worker: Worker, child: Any, session_id: str, prompt: str
    ) -> None:
        try:
            status, text = await self._run_child(child, session_id, prompt)
        except asyncio.CancelledError:
            worker.summary = "subagent cancelled"
            raise
        except Exception as exc:  # noqa: BLE001
            status, text = "failed", f"subagent crashed: {exc}"
        finally:
            try:
                await child.aclose()
            except Exception:  # noqa: BLE001
                pass
        if worker.status == "cancelled":
            return
        await self._report(worker, status, text)


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
