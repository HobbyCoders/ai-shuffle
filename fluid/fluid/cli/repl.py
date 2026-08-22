"""Async line REPL — the plain-stdout client (no textual dependency).

Reads a line, submits a ``UserTurn``, subscribes to the engine's EventBus, and
renders the stream: ``TextDelta``/``ThinkingDelta`` inline, tool use + results,
permission prompts (y/n on stdin → ``RespondPermission``), and per-turn usage on
``TurnComplete``. Slash commands ``/model``, ``/fork``, ``/rewind``, ``/exit`` map
to the corresponding ops.

Everything routes through :func:`stream_turn` and the small ``on_permission`` /
``input_fn`` seams so the one-shot ``run`` path and the unit tests reuse the exact
same rendering and permission logic without a pty.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, TextIO

from fluid.cli.config import host_of
from fluid.cli.host import render_worker_event
from fluid.events.model import (
    ErrorEvent,
    Event,
    Interrupted,
    PermissionRequest,
    SessionParked,
    SessionWoken,
    SubagentEvent,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    Usage,
)
from fluid.events.ops import Fork, RespondPermission, Rewind, SetModel, UserTurn

# A permission responder: given the request event, return a decision string.
PermissionResponder = Callable[[PermissionRequest], Awaitable[str]]

_TERMINAL = (TurnComplete, Interrupted, ErrorEvent)


def render_event(event: Event, out: TextIO) -> None:
    """Render one event to ``out`` (matches native + delegated event shapes)."""
    if isinstance(event, TextDelta):
        out.write(event.text)
    elif isinstance(event, ThinkingDelta):
        out.write(event.text)
    elif isinstance(event, ToolUse):
        out.write(f"\n  [tool] {event.name} {event.input}\n")
    elif isinstance(event, ToolResult):
        tag = "tool!" if event.is_error else "tool"
        out.write(f"  [{tag}] {event.output}\n")
    elif isinstance(event, PermissionRequest):
        out.write(f"\n  [permission] {event.name} (risk={event.risk}) {event.input}\n")
    elif isinstance(event, Usage):
        out.write(
            f"\n  [usage] in={event.input_tokens} out={event.output_tokens} "
            f"cache_read={event.cache_read_tokens} "
            f"cache_write={event.cache_creation_tokens} "
            f"hit_rate={event.cache_hit_rate:.2f}\n"
        )
    elif isinstance(event, TurnComplete):
        cost = f" cost=${event.cost_usd:.4f}" if event.cost_usd is not None else ""
        out.write(f"\n[done] stop={event.stop_reason} calls={event.num_model_calls}{cost}\n")
    elif isinstance(event, Interrupted):
        out.write("\n[interrupted]\n")
    elif isinstance(event, ErrorEvent):
        out.write(f"\n[error] {event.message}\n")
    elif isinstance(event, (SubagentEvent, SessionParked, SessionWoken)):
        line = render_worker_event(event)
        if line is not None:
            out.write(f"\n  {line}\n")
    out.flush()


async def auto_deny(event: PermissionRequest) -> str:
    """Non-interactive default responder — deny (used by the one-shot path)."""
    return "deny"


async def stream_turn(
    engine,
    session_id: str,
    text: "str | list[Any]",
    *,
    out: TextIO,
    on_permission: PermissionResponder = auto_deny,
    parent_node_id: str | None = None,
    wait_background: bool = False,
    on_event: Callable[[Event], None] | None = None,
) -> str:
    """Submit a turn and render its event stream until a terminal event.

    Returns the terminal ``stop_reason`` (or ``"interrupted"``/``"error"``).
    Permission requests are answered via ``on_permission`` while the turn is
    parked, then the loop resumes.

    ``wait_background=True`` keeps consuming past a terminal event while the
    attached :class:`~fluid.cli.host.CliHost` still has workers running or a
    wake pending, so the park → wake continuation renders too and the caller
    only gets control back once nothing is in flight (the one-shot ``run``
    path). The REPL leaves it off: the prompt returns while work runs and a
    watcher renders the wake turn later.
    """
    host = host_of(engine)
    start_seq = len(engine.bus.log(session_id))
    await engine.submit(UserTurn(content=text, parent_node_id=parent_node_id))
    result = "end_turn"
    async for event in engine.bus.subscribe(session_id, since_seq=start_seq):
        render_event(event, out)
        if on_event is not None:
            on_event(event)
        if isinstance(event, PermissionRequest):
            decision = await on_permission(event)
            await engine.submit(RespondPermission(request_id=event.request_id, decision=decision))
            continue
        if isinstance(event, TurnComplete):
            result = event.stop_reason
        elif isinstance(event, Interrupted):
            result = "interrupted"
        elif isinstance(event, ErrorEvent):
            result = "error"
        else:
            continue
        if wait_background and host is not None and host.busy():
            continue
        break
    if host is None or not host.busy():
        await engine.wait_turn()
    return result


async def set_model(engine, model: str) -> None:
    """Change the model, working across native (attribute) and delegated (op)."""
    try:
        await engine.submit(SetModel(model=model))
    except NotImplementedError:
        engine.model = model


class Repl:
    """Interactive REPL bound to one engine + session."""

    def __init__(
        self,
        engine,
        session_id: str,
        *,
        input_fn: Callable[[str], str] | None = None,
        out: TextIO | None = None,
        prompt: str = "fluid> ",
    ) -> None:
        self.engine = engine
        self.session_id = session_id
        self.input_fn = input_fn or input
        self.out = out or sys.stdout
        self.prompt = prompt
        # Background rendering state: the foreground turn renders its own
        # events; the watcher renders everything after ``_rendered_seq`` while
        # no foreground turn is active (park/wake continuations).
        self._foreground = False
        self._rendered_seq = 0
        self._watcher: asyncio.Task | None = None
        host = host_of(engine)
        if host is not None:
            host.permission_responder = self.handle_permission

    async def _read_line(self, prompt: str) -> str:
        return await asyncio.to_thread(self.input_fn, prompt)

    async def handle_permission(self, event: PermissionRequest) -> str:
        answer = await self._read_line(f"  allow {event.name}? [y/N] ")
        return "allow" if answer.strip().lower() in ("y", "yes") else "deny"

    async def run_turn(self, text: "str | list[Any]") -> str:
        self._foreground = True
        try:
            return await stream_turn(
                self.engine, self.session_id, text,
                out=self.out, on_permission=self.handle_permission,
                on_event=self._note_rendered,
            )
        finally:
            self._foreground = False

    def _note_rendered(self, event: Event) -> None:
        self._rendered_seq = max(self._rendered_seq, event.seq)

    async def _watch_background(self) -> None:
        """Render wake-turn events that arrive while the user is at the prompt."""
        since = len(self.engine.bus.log(self.session_id))
        self._rendered_seq = max(self._rendered_seq, since)
        async for event in self.engine.bus.subscribe(self.session_id, since_seq=since):
            if self._foreground or event.seq <= self._rendered_seq:
                continue
            self._rendered_seq = event.seq
            render_event(event, self.out)
            if isinstance(event, PermissionRequest):
                decision = await self.handle_permission(event)
                await self.engine.submit(
                    RespondPermission(request_id=event.request_id, decision=decision)
                )

    def _start_watcher(self) -> None:
        if self._watcher is None and host_of(self.engine) is not None:
            self._watcher = asyncio.create_task(self._watch_background())

    async def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._watcher = None

    async def dispatch_slash(self, line: str) -> bool:
        """Handle a ``/command``. Returns False to signal exit, True otherwise."""
        parts = line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            self.out.write(
                "commands: /model <name>, /fork <node_id>, /rewind <node_id>, "
                "/tasks, /cancel <task_id>, /exit,\n"
                "plus any ~/.fluid/commands or .agents/commands *.md file as /<name> [args]\n"
            )
        elif cmd == "/tasks":
            host = host_of(self.engine)
            workers = list(host.workers.values()) if host is not None else []
            if not workers:
                self.out.write("(no background tasks)\n")
            for w in workers:
                self.out.write(f"{w.task_id}  {w.kind:8} {w.status:9} {w.description}\n")
        elif cmd == "/cancel":
            host = host_of(self.engine)
            if host is None or not arg:
                self.out.write("usage: /cancel <task_id>\n")
            elif await host.cancel(arg):
                self.out.write(f"cancelled {arg}\n")
            else:
                self.out.write(f"no running task {arg}\n")
        elif cmd == "/model":
            if arg:
                await set_model(self.engine, arg)
                self.out.write(f"model → {arg}\n")
            else:
                self.out.write(f"model = {self.engine.model}\n")
        elif cmd == "/fork":
            await self._submit_or_report(Fork(from_node_id=arg), "fork")
        elif cmd == "/rewind":
            await self._submit_or_report(Rewind(to_node_id=arg), "rewind")
        else:
            # User-defined slash commands: ~/.fluid/commands/*.md and
            # .agents/commands/*.md files run as a turn. Rendering uses the
            # documented Fluid command syntax ($ARGUMENTS/$1/$2, !`bash`, @file).
            # Off-thread because rendering may run inline bash.
            rendered = await asyncio.to_thread(
                self._render_user_command, cmd.lstrip("/"), arg
            )
            if rendered is not None:
                await self.run_turn(rendered)
            else:
                self.out.write(f"unknown command: {cmd} (try /help)\n")
        self.out.flush()
        return True

    def _render_user_command(self, name: str, arg: str):
        """Resolve a discovered command file to its rendered prompt, or None.

        Returns a plain string for a bodiless render, or a list of Text
        blocks when the command pulled in ``@file`` contents — each dump rides
        as its own content block so the compaction view can stub an oversize
        one without hiding the instruction text.
        """
        from fluid.context.instructions import discover_commands, render_command_blocks
        from fluid.messages import Text

        cwd = getattr(getattr(self.engine, "ctx", None), "cwd", None) or "."
        match = next((c for c in discover_commands(cwd) if c.name == name), None)
        if match is None:
            return None
        try:
            raw = Path(match.path).read_text(encoding="utf-8")
        except OSError:
            return None
        blocks = render_command_blocks(raw, arg, cwd=cwd)
        if len(blocks) <= 1:
            return blocks[0] if blocks else ""
        return [Text(b) for b in blocks]

    async def _submit_or_report(self, op, label: str) -> None:
        try:
            await self.engine.submit(op)
            self.out.write(f"{label} ok\n")
        except NotImplementedError:
            self.out.write(f"{label} not supported by this engine\n")

    async def run(self) -> None:
        """Main loop: read lines, dispatch slash-commands or run turns."""
        self._start_watcher()
        try:
            while True:
                try:
                    line = await self._read_line(self.prompt)
                except (EOFError, KeyboardInterrupt):
                    self.out.write("\n")
                    break
                if not line.strip():
                    continue
                if line.strip().startswith("/"):
                    if not await self.dispatch_slash(line):
                        break
                    continue
                await self.run_turn(line)
        finally:
            await self._stop_watcher()
