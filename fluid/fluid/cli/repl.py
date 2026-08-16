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
from typing import Awaitable, Callable, TextIO

from fluid.events.model import (
    ErrorEvent,
    Event,
    Interrupted,
    PermissionRequest,
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
            f"cache_read={event.cache_read_tokens} hit_rate={event.cache_hit_rate:.2f}\n"
        )
    elif isinstance(event, TurnComplete):
        cost = f" cost=${event.cost_usd:.4f}" if event.cost_usd is not None else ""
        out.write(f"\n[done] stop={event.stop_reason} calls={event.num_model_calls}{cost}\n")
    elif isinstance(event, Interrupted):
        out.write("\n[interrupted]\n")
    elif isinstance(event, ErrorEvent):
        out.write(f"\n[error] {event.message}\n")
    out.flush()


async def auto_deny(event: PermissionRequest) -> str:
    """Non-interactive default responder — deny (used by the one-shot path)."""
    return "deny"


async def stream_turn(
    engine,
    session_id: str,
    text: str,
    *,
    out: TextIO,
    on_permission: PermissionResponder = auto_deny,
    parent_node_id: str | None = None,
) -> str:
    """Submit a turn and render its event stream until a terminal event.

    Returns the terminal ``stop_reason`` (or ``"interrupted"``/``"error"``).
    Permission requests are answered via ``on_permission`` while the turn is
    parked, then the loop resumes.
    """
    start_seq = len(engine.bus.log(session_id))
    await engine.submit(UserTurn(content=text, parent_node_id=parent_node_id))
    result = "end_turn"
    async for event in engine.bus.subscribe(session_id, since_seq=start_seq):
        render_event(event, out)
        if isinstance(event, PermissionRequest):
            decision = await on_permission(event)
            await engine.submit(RespondPermission(request_id=event.request_id, decision=decision))
        elif isinstance(event, TurnComplete):
            result = event.stop_reason
            break
        elif isinstance(event, Interrupted):
            result = "interrupted"
            break
        elif isinstance(event, ErrorEvent):
            result = "error"
            break
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

    async def _read_line(self, prompt: str) -> str:
        return await asyncio.to_thread(self.input_fn, prompt)

    async def handle_permission(self, event: PermissionRequest) -> str:
        answer = await self._read_line(f"  allow {event.name}? [y/N] ")
        return "allow" if answer.strip().lower() in ("y", "yes") else "deny"

    async def run_turn(self, text: str) -> str:
        return await stream_turn(
            self.engine, self.session_id, text,
            out=self.out, on_permission=self.handle_permission,
        )

    async def dispatch_slash(self, line: str) -> bool:
        """Handle a ``/command``. Returns False to signal exit, True otherwise."""
        parts = line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            self.out.write(
                "commands: /model <name>, /fork <node_id>, /rewind <node_id>, /exit,\n"
                "plus any .claude/commands|.agents/commands *.md file as /<name> [args]\n"
            )
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
            # User-defined slash commands: .claude/commands/*.md and
            # .agents/commands/*.md files run as a turn, rendered per the
            # Claude Code conventions ($ARGUMENTS/$1/$2, !`bash`, @file).
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

    def _render_user_command(self, name: str, arg: str) -> str | None:
        """Resolve a discovered command file to its rendered prompt, or None."""
        from fluid.context.instructions import discover_commands, render_command

        cwd = getattr(getattr(self.engine, "ctx", None), "cwd", None) or "."
        match = next((c for c in discover_commands(cwd) if c.name == name), None)
        if match is None:
            return None
        try:
            raw = Path(match.path).read_text(encoding="utf-8")
        except OSError:
            return None
        return render_command(raw, arg, cwd=cwd)

    async def _submit_or_report(self, op, label: str) -> None:
        try:
            await self.engine.submit(op)
            self.out.write(f"{label} ok\n")
        except NotImplementedError:
            self.out.write(f"{label} not supported by this engine\n")

    async def run(self) -> None:
        """Main loop: read lines, dispatch slash-commands or run turns."""
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
