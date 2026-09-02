"""Async line REPL — the plain-stdout client (no textual dependency).

Reads a line, submits a ``UserTurn``, subscribes to the engine's EventBus, and
renders the stream: ``TextDelta``/``ThinkingDelta`` inline, tool use + results,
permission prompts (y/n on stdin → ``RespondPermission``), and per-turn usage on
``TurnComplete``. Slash commands ``/model``, ``/fork``, ``/rewind``, ``/exit`` map
to the corresponding ops; ``/context`` (alias ``/usage``), ``/tools`` and
``/skills`` are read-only views over the engine's context breakdown, tool
registry and discovered skills.

Everything routes through :func:`stream_turn` and the small ``on_permission`` /
``input_fn`` seams so the one-shot ``run`` path and the unit tests reuse the exact
same rendering and permission logic without a pty.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

from fluid.cli.config import host_of
from fluid.cli.host import render_worker_event
from fluid.events.model import (
    ContinualUpdate,
    ErrorEvent,
    Event,
    HookEvent,
    Interrupted,
    PermissionRequest,
    QuestionRequest,
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
from fluid.events.ops import (
    Fork,
    Interrupt,
    RespondPermission,
    RespondQuestion,
    Rewind,
    SetModel,
    UserTurn,
)
from fluid.permissions.engine import PROMPT_DENIAL, non_interactive_denial

# A permission responder: given the request event, return a decision string.
# A responder returns either a bare decision ("allow" | "deny" | "allow_always")
# or a full RespondPermission when it wants to attach a reason.
PermissionResponder = Callable[[PermissionRequest], Awaitable["str | RespondPermission"]]

# A question responder: given a QuestionRequest (AskUser, or an MCP server's
# input_required round-trip), return the user's answer text.
QuestionResponder = Callable[[QuestionRequest], Awaitable[str]]

# What a parked question receives when no human is attached (one-shot ``run``
# without a tty). Worded for the model: proceed rather than wedge.
NO_USER_ANSWER = (
    "(no interactive user is attached to this session — proceed with your "
    "best judgment and state the assumption you made)"
)


async def auto_answer(event: QuestionRequest) -> str:
    """Non-interactive question responder: unblock the turn, never guess."""
    return NO_USER_ANSWER

_TERMINAL = (TurnComplete, Interrupted, ErrorEvent)

# A second Ctrl-C inside this many seconds quits instead of interrupting again.
SIGINT_EXIT_WINDOW = 2.0
# The exit code a Ctrl-C ended the process with (128 + SIGINT).
SIGINT_EXIT_CODE = 130


class LineReader:
    """Blocking ``input_fn`` calls on one daemon thread, handed back as futures.

    ``asyncio.to_thread`` ran the read on the default executor, whose threads
    are joined at shutdown, so a Ctrl-C at the prompt hung the process until
    stdin produced a line. This thread is a daemon: exit never waits for it. A
    read the signal handler abandons keeps blocking here; the line it finally
    gets is kept and answers the next request, so nothing typed after an
    interrupt is lost.

    While it blocks, the thread keeps a reference to ``sys.stdin``. On a pipe,
    ``input`` reads through the ``BufferedReader`` under its lock, and nothing
    else holds the wrapper alive: clearing the ``sys`` module at shutdown
    would drop the last reference, and closing the stream then needs the lock
    this thread still holds, which CPython turns into a fatal abort (exit -6
    instead of 130). A reference held by a frame that never unwinds keeps the
    stream alive until the process ends, so shutdown never tries to close it.
    """

    def __init__(self, input_fn: Callable[[str], str]) -> None:
        self._input_fn = input_fn
        self._requests: queue.SimpleQueue[
            tuple[str | None, asyncio.AbstractEventLoop, asyncio.Future[str]]
        ] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._reading = False  # the thread is inside input_fn
        self._stashed: str | None = None
        self._pending: asyncio.Future[str] | None = None
        # Serializes "abandon this read" against "this read's line is on its
        # way": whichever wins decides whether the line answers the pending
        # request or is stashed for the next one. Both happen on the thread
        # side before the next request is taken, so lines stay in order.
        self._lock = threading.Lock()
        self._handed: asyncio.Future[str] | None = None

    def read(self, prompt: str, *, out: TextIO | None = None) -> asyncio.Future[str]:
        """Start a read; the future resolves with the line on the running loop."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending = fut
        request_prompt: str | None = prompt
        if self._reading and out is not None:
            # The thread is still inside an abandoned read, which already
            # showed its own prompt; that call cannot print a new one.
            out.write(prompt)
            out.flush()
            request_prompt = None
        self._requests.put((request_prompt, loop, fut))
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._serve, name="fluid-stdin", daemon=True
            )
            self._thread.start()
        return fut

    def abandon(self) -> None:
        """Resolve the pending read with an empty line without waiting on stdin."""
        with self._lock:
            fut = self._pending
            if fut is None or fut.done() or fut is self._handed:
                return  # nothing pending, or its line is already on its way
            fut.set_result("")

    def _serve(self) -> None:
        while True:
            prompt, loop, fut = self._requests.get()
            if self._stashed is not None:
                line, self._stashed = self._stashed, None
                outcome: tuple[Callable[[Any], None], Any] = (fut.set_result, line)
            else:
                self._reading = True
                keep_alive = sys.stdin  # noqa: F841 — see the class docstring
                try:
                    line = self._input_fn("" if prompt is None else prompt)
                except BaseException as exc:  # noqa: BLE001 — EOFError and whatever input_fn raises
                    outcome = (fut.set_exception, exc)
                else:
                    outcome = (fut.set_result, line)
                finally:
                    self._reading = False
            with self._lock:
                if fut.done() and outcome[0] == fut.set_result:
                    # Abandoned by a signal while we were blocked: keep the
                    # line for the next request, decided here, before that
                    # request is taken, so it cannot be overtaken.
                    self._stashed = outcome[1]
                    continue
                self._handed = fut
            try:
                loop.call_soon_threadsafe(self._settle, fut, *outcome)
            except RuntimeError:
                return  # the loop is closed; nobody is waiting

    def _settle(self, fut: asyncio.Future[str], setter, value) -> None:
        if not fut.done():
            setter(value)


_stdin_reader: LineReader | None = None


def stdin_reader() -> LineReader:
    """The process-wide reader for real stdin (shared by every prompt on it)."""
    global _stdin_reader
    if _stdin_reader is None:
        _stdin_reader = LineReader(input)
    return _stdin_reader


def abandon_stdin_read() -> None:
    """Release whoever is waiting on stdin, with an empty line."""
    if _stdin_reader is not None:
        _stdin_reader.abandon()


_UNSET = object()


@contextlib.contextmanager
def sigint_scope(callback: Callable[[], None]) -> Iterator[None]:
    """Run ``callback`` on the event loop whenever Ctrl-C arrives in the block.

    Uses ``loop.add_signal_handler`` where the loop supports it. Windows loops
    do not, so there a ``signal.signal`` handler runs on the main thread
    between bytecodes and only forwards to the loop; Ctrl-Break is routed the
    same way, since that is the console event a parent process can deliver to
    a child. Off the main thread no handler can be installed and the block
    runs unguarded.
    """
    loop = asyncio.get_running_loop()
    restore: list[tuple[int, Any]] = []
    signums = [signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        signums.append(signal.SIGBREAK)
    for signum in signums:
        try:
            loop.add_signal_handler(signum, callback)
            restore.append((signum, _UNSET))
            continue
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            previous = signal.signal(
                signum, lambda *_: loop.call_soon_threadsafe(callback)
            )
        except (ValueError, OSError):
            continue
        restore.append((signum, previous))
    try:
        yield
    finally:
        for signum, previous in restore:
            if previous is _UNSET:
                loop.remove_signal_handler(signum)
            else:
                signal.signal(signum, previous)


_interrupt_submits: set[asyncio.Task] = set()


def request_interrupt(engine) -> None:
    """Submit ``Interrupt`` to the engine from a signal callback."""

    async def _submit() -> None:
        try:
            await engine.submit(Interrupt())
        except Exception:  # noqa: BLE001 — an engine without a turn has nothing to stop
            pass

    task = asyncio.ensure_future(_submit())
    _interrupt_submits.add(task)
    task.add_done_callback(_interrupt_submits.discard)


# Per-stream render mode ("text" | "thinking"), keyed by id(out), so thinking
# and answer text get separated without threading state through every caller.
_mode: dict[int, str] = {}
# Worker notes that arrived mid-turn, flushed at TurnComplete (keyed by id(out)).
_deferred_notes: dict[int, list[str]] = {}


def _switch_mode(out: TextIO, mode: str) -> None:
    key = id(out)
    previous = _mode.get(key, "text")
    _mode[key] = mode  # also marks "a turn is streaming" for note deferral
    if previous == mode:
        return
    dim = getattr(out, "isatty", lambda: False)()
    if mode == "thinking":
        out.write("\n  [thinking] " + ("\x1b[2m" if dim else ""))
    else:
        out.write(("\x1b[0m" if dim else "") + "\n\n")
    _mode[key] = mode


def _end_stream(out: TextIO) -> None:
    """Leave streaming mode at a terminal event, whichever kind it is.

    Undoes a thinking dim (the ``\\x1b[0m`` a tty needs) and drops the
    per-stream mode and deferred notes, so a turn that ends interrupted or
    with an error cannot leave the terminal dimmed or hand stale state to the
    next turn.
    """
    _switch_mode(out, "text")
    _mode.pop(id(out), None)
    out.writelines(f"\n  {note}\n" for note in _deferred_notes.pop(id(out), []))


def render_hook_event(event: HookEvent) -> str:
    """``[hook] ...`` line for a hook denial or warning."""
    if event.level == "deny":
        target = f" {event.tool_name}" if event.tool_name else ""
        return f"[hook] denied{target}: {event.message}"
    return f"[hook] {event.message}"


def render_event(event: Event, out: TextIO) -> None:
    """Render one event to ``out`` (matches native + delegated event shapes).

    Thinking is shown under a ``[thinking]`` marker (dimmed on a tty) so it can
    never be mistaken for the answer; the answer resumes on its own lines.
    """
    if isinstance(event, TextDelta):
        _switch_mode(out, "text")
        out.write(event.text)
    elif isinstance(event, ThinkingDelta):
        _switch_mode(out, "thinking")
        out.write(event.text)
    elif isinstance(event, ToolUse):
        _switch_mode(out, "text")
        out.write(f"\n  [tool] {event.name} {event.input}\n")
    elif isinstance(event, ToolResult):
        tag = "tool!" if event.is_error else "tool"
        out.write(f"  [{tag}] {event.output}\n")
    elif isinstance(event, PermissionRequest):
        out.write(f"\n  [permission] {event.name} (risk={event.risk}) {event.input}\n")
    elif isinstance(event, QuestionRequest):
        _switch_mode(out, "text")
        out.write(f"\n  [question] {event.message}\n")
    elif isinstance(event, Usage):
        out.write(
            f"\n  [usage] in={event.input_tokens} out={event.output_tokens} "
            f"cache_read={event.cache_read_tokens} "
            f"cache_write={event.cache_creation_tokens} "
            f"hit_rate={event.cache_hit_rate:.2f}\n"
        )
    elif isinstance(event, TurnComplete):
        _end_stream(out)
        cost =f" cost=${event.cost_usd:.4f}" if event.cost_usd is not None else ""
        out.write(f"\n[done] stop={event.stop_reason} calls={event.num_model_calls}{cost}\n")
    elif isinstance(event, Interrupted):
        _end_stream(out)
        out.write("\n[interrupted]\n")
    elif isinstance(event, ErrorEvent):
        _end_stream(out)
        out.write(f"\n[error] {event.message}\n")
    elif isinstance(event, HookEvent):
        out.write(f"  {render_hook_event(event)}\n")
    elif isinstance(event, ContinualUpdate):
        line = render_continual_update(event)
        if id(out) in _mode:
            # Synthesis is a background pass; it can land mid-stream. Hold
            # the note until the turn ends rather than splicing it in.
            _deferred_notes.setdefault(id(out), []).append(line)
        else:
            out.write(f"\n  {line}\n")
    elif isinstance(event, (SubagentEvent, SessionParked, SessionWoken)):
        line = render_worker_event(event)
        if line is None:
            return
        if isinstance(event, SubagentEvent) and id(out) in _mode:
            # A worker finishing while the model is mid-sentence: hold the
            # note until the turn ends rather than splicing it into the text.
            _deferred_notes.setdefault(id(out), []).append(line)
        else:
            out.write(f"\n  {line}\n")
    out.flush()


def format_context_usage(
    usage: dict[str, Any] | None, *, last_usage: Usage | None = None
) -> str:
    """Render ``get_context_usage()`` output as a compact table.

    Header: total prompt tokens against the context window and the percentage
    used; then one row per category with tokens and percent of the window (or
    of the total when the window is unknown). Deferred rows are marked since
    they are not in the prompt. The last call's cache hit rate follows when a
    ``Usage`` event is available, and the compaction stats when present.
    """
    if not usage:
        return "context usage unavailable for this engine"
    total = int(usage.get("totalTokens") or 0)
    window = usage.get("maxTokens")
    lines: list[str] = []
    if window:
        pct = usage.get("percentage")
        pct = pct if pct is not None else round(total / window * 100, 1)
        lines.append(f"context: {total:,} / {window:,} tokens ({pct}%)")
    else:
        lines.append(f"context: {total:,} tokens (window unknown)")
    if usage.get("model"):
        lines.append(f"model: {usage['model']}" + ("" if usage.get("measured") else " (estimated)"))
    denominator = window or total or 1
    rows = [
        c for c in usage.get("categories", []) if c.get("name") != "Free space"
    ]
    name_width = max((len(c["name"]) for c in rows), default=10) + 2
    for c in rows:
        tokens = int(c.get("tokens") or 0)
        share = tokens / denominator * 100
        mark = "  (deferred, not in prompt)" if c.get("isDeferred") else ""
        lines.append(f"  {c['name'].ljust(name_width)}{tokens:>9,}  {share:5.1f}%{mark}")
    if window:
        lines.append(f"  {'Free space'.ljust(name_width)}{max(0, window - total):>9,}")
    if last_usage is not None:
        lines.append(
            f"last call: cache hit rate {last_usage.cache_hit_rate * 100:.0f}% "
            f"(read={last_usage.cache_read_tokens:,} write={last_usage.cache_creation_tokens:,} "
            f"in={last_usage.input_tokens:,})"
        )
    compaction = usage.get("compaction")
    if compaction:
        lines.append("compaction: " + ", ".join(f"{k}={v}" for k, v in compaction.items()))
    return "\n".join(lines)


def format_tools(registry) -> str:
    """One line per registered tool: name, loaded/deferred, risk."""
    if registry is None:
        return "(no tool registry)"
    specs = sorted(registry.list_tools(loaded_only=False), key=lambda s: s.name)
    if not specs:
        return "(no tools registered)"
    deferred = {s.name for s in registry.deferred_tools()}
    width = max(len(s.name) for s in specs) + 2
    lines = [f"{len(specs)} tools ({len(specs) - len(deferred)} loaded, {len(deferred)} deferred)"]
    for s in specs:
        state = "deferred" if s.name in deferred else "loaded"
        lines.append(f"  {s.name.ljust(width)}{state:<9} risk={s.risk}")
    return "\n".join(lines)


def format_skills(skills) -> str:
    """One line per discovered SKILL.md: name, scope, description."""
    skills = list(skills)
    if not skills:
        return "(no skills discovered)"
    width = max(len(s.name) for s in skills) + 2
    lines = [f"{len(skills)} skills"]
    for s in skills:
        desc = " ".join((s.description or "").split())
        if len(desc) > 80:
            desc = desc[:79] + "\u2026"
        lines.append(f"  {s.name.ljust(width)}[{s.scope}] {desc}".rstrip())
    return "\n".join(lines)


async def ask_permission(
    event: PermissionRequest, read_line: Callable[[str], Awaitable[str]]
) -> str:
    """Interactive responder: y = allow once, a = allow this tool (and, for
    Bash, these programs) for the rest of the session, anything else = deny."""
    answer = (await read_line(f"  allow {event.name}? [y/N/a=always] ")).strip().lower()
    if answer in ("a", "always"):
        return "allow_always"
    return "allow" if answer in ("y", "yes") else "deny"


def tty_permission_responder() -> PermissionResponder:
    """The one-shot ``fluid run`` responder: ask on a terminal, deny otherwise.

    Claude Code's print mode denies silently; fluid asks whenever a human is
    actually attached, since a denied Write usually means a wasted run.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return auto_deny

    async def _ask(event: PermissionRequest) -> str:
        return await ask_permission(event, lambda p: stdin_reader().read(p))

    return _ask


def tty_question_responder() -> QuestionResponder | None:
    """Interactive question responder for one-shot runs, or ``None`` off-tty."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    async def _ask(event: QuestionRequest) -> str:
        answer = await stdin_reader().read("  answer> ")
        return answer.strip()

    return _ask


async def auto_deny(event: PermissionRequest) -> RespondPermission:
    """Non-interactive default responder (the one-shot ``fluid run`` path).

    Denies, and tells the model why: there is nobody to ask, and how the
    caller can unblock the call next time.
    """
    return RespondPermission(
        request_id=event.request_id, decision="deny",
        message=non_interactive_denial(event.name, event.reason),
    )


def _to_response(event: PermissionRequest, answer: str | RespondPermission) -> RespondPermission:
    """Responders may return a bare decision string or a full op."""
    if isinstance(answer, RespondPermission):
        return answer
    message = PROMPT_DENIAL if answer == "deny" else None
    if message and event.reason:
        message = f"{message} ({event.reason})"
    return RespondPermission(request_id=event.request_id, decision=answer, message=message)


async def stream_turn(
    engine,
    session_id: str,
    text: str | list[Any],
    *,
    out: TextIO,
    on_permission: PermissionResponder = auto_deny,
    on_question: QuestionResponder = auto_answer,
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
    start_seq = engine.bus.last_seq(session_id)
    await engine.submit(UserTurn(content=text, parent_node_id=parent_node_id))
    result = "end_turn"
    # aclosing: leaving the loop early must unregister the subscriber now,
    # not whenever the generator is collected.
    async with contextlib.aclosing(
        engine.bus.subscribe(session_id, since_seq=start_seq, from_store=False)
    ) as events:
        async for event in events:
            render_event(event, out)
            if on_event is not None:
                on_event(event)
            if isinstance(event, PermissionRequest):
                await engine.submit(_to_response(event, await on_permission(event)))
                continue
            if isinstance(event, QuestionRequest):
                await engine.submit(
                    RespondQuestion(
                        request_id=event.request_id, answer=await on_question(event)
                    )
                )
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
        self._reader = stdin_reader() if input_fn is None else LineReader(input_fn)
        self.out = out or sys.stdout
        self.prompt = prompt
        # Background rendering state: the foreground turn renders its own
        # events; the watcher renders everything after ``_rendered_seq`` while
        # no foreground turn is active (park/wake continuations).
        self._foreground = False
        self._rendered_seq = 0
        self._watcher: asyncio.Task | None = None
        # Ctrl-C state: the exit code ``run`` returns, whether a quit was
        # asked for, when the last Ctrl-C landed, and the task running ``run``
        # (cancelled as the last resort when a turn ignores Interrupt).
        self.exit_code = 0
        self._exit_requested = False
        self._last_sigint = 0.0
        self._loop_task: asyncio.Task | None = None
        host = host_of(engine)
        if host is not None:
            host.permission_responder = self.handle_permission

    async def _read_line(self, prompt: str) -> str:
        return await self._reader.read(prompt, out=self.out)

    def on_sigint(self) -> None:
        """Ctrl-C: interrupt the running turn; again (or at the prompt) to quit.

        The first press during a turn submits ``Interrupt`` so the turn ends
        with ``Interrupted`` and the session stays intact. A second press
        within :data:`SIGINT_EXIT_WINDOW`, or any press while no turn runs,
        asks ``run`` to return :data:`SIGINT_EXIT_CODE`. A third press while
        a quit is already pending cancels the loop task outright, for a turn
        that does not honor Interrupt.
        """
        now = time.monotonic()
        again = now - self._last_sigint < SIGINT_EXIT_WINDOW
        self._last_sigint = now
        if self._foreground and not again and not self._exit_requested:
            self.out.write("\n[interrupting; press Ctrl-C again to quit]\n")
            self.out.flush()
            request_interrupt(self.engine)
            self._reader.abandon()
            return
        if self._exit_requested:
            if self._loop_task is not None and not self._loop_task.done():
                self._loop_task.cancel()
            return
        self._exit_requested = True
        self.exit_code = SIGINT_EXIT_CODE
        if self._foreground:
            request_interrupt(self.engine)
        self._reader.abandon()

    async def handle_permission(self, event: PermissionRequest) -> str:
        return await ask_permission(event, self._read_line)

    async def handle_question(self, event: QuestionRequest) -> str:
        # The question text (with any numbered options) was already rendered
        # by render_event; just collect the answer line.
        return (await self._read_line("  answer> ")).strip()

    async def run_turn(self, text: str | list[Any]) -> str:
        self._foreground = True
        try:
            return await stream_turn(
                self.engine, self.session_id, text,
                out=self.out, on_permission=self.handle_permission,
                on_question=self.handle_question,
                on_event=self._note_rendered,
            )
        finally:
            self._foreground = False

    def _note_rendered(self, event: Event) -> None:
        self._rendered_seq = max(self._rendered_seq, event.seq)

    async def _watch_background(self) -> None:
        """Render wake-turn events that arrive while the user is at the prompt."""
        since = self.engine.bus.last_seq(self.session_id)
        self._rendered_seq = max(self._rendered_seq, since)
        async for event in self.engine.bus.subscribe(
            self.session_id, since_seq=since, from_store=False
        ):
            if self._foreground or event.seq <= self._rendered_seq:
                continue
            self._rendered_seq = event.seq
            render_event(event, self.out)
            if isinstance(event, PermissionRequest):
                decision = await self.handle_permission(event)
                await self.engine.submit(
                    RespondPermission(request_id=event.request_id, decision=decision)
                )
            elif isinstance(event, QuestionRequest):
                await self.engine.submit(
                    RespondQuestion(
                        request_id=event.request_id,
                        answer=await self.handle_question(event),
                    )
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
                "commands: /model <name>, /fork <node_id>, /rewind <node_id>,\n"
                "  /context (or /usage), /tools, /skills, /tasks, /cancel <task_id>,\n"
                "  /exit\n"
                "resume a session later with: fluid chat --resume <session-id> (or --continue)\n"
            )
        elif cmd in ("/context", "/usage"):
            self.out.write(await self.render_context_usage() + "\n")
        elif cmd == "/tools":
            self.out.write(format_tools(getattr(self.engine, "registry", None)) + "\n")
        elif cmd == "/skills":
            self.out.write(self.render_skills() + "\n")
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

    async def render_context_usage(self) -> str:
        """The ``/context`` text: breakdown plus the last call's cache stats."""
        getter = getattr(self.engine, "get_context_usage", None)
        if getter is None:
            return "context usage unavailable for this engine"
        usage = await getter()
        last = None
        for event in reversed(self.engine.bus.log(self.session_id)):
            if isinstance(event, Usage):
                last = event
                break
        if last is None:
            # A resumed session has no Usage in this process's ring yet; the
            # persisted log carries the last call's.
            store = getattr(self.engine, "store", None)
            events_since = getattr(store, "events_since", None)
            if events_since is not None:
                try:
                    persisted = await events_since(self.session_id, 0)
                except Exception:  # noqa: BLE001 - a display line, never a failure
                    persisted = []
                for event in reversed(list(persisted)):
                    if isinstance(event, Usage):
                        last = event
                        break
        return format_context_usage(usage, last_usage=last)

    def render_skills(self) -> str:
        from fluid.context.instructions import discover_skills

        cwd = getattr(getattr(self.engine, "ctx", None), "cwd", None) or "."
        return format_skills(discover_skills(cwd))

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

    async def run(self) -> int:
        """Main loop: read lines, dispatch slash-commands or run turns.

        Returns the process exit code: 0 after ``/exit`` or end of input,
        :data:`SIGINT_EXIT_CODE` when Ctrl-C ended the session.
        """
        self._loop_task = asyncio.current_task()
        self._start_watcher()
        try:
            with sigint_scope(self.on_sigint):
                while not self._exit_requested:
                    try:
                        line = await self._read_line(self.prompt)
                    except EOFError:
                        break
                    except KeyboardInterrupt:
                        self.exit_code = SIGINT_EXIT_CODE
                        break
                    if self._exit_requested or not line.strip():
                        continue
                    if line.strip().startswith("/"):
                        if not await self.dispatch_slash(line):
                            break
                        continue
                    await self.run_turn(line)
        except asyncio.CancelledError:
            if not self._exit_requested:
                raise
        finally:
            await self._stop_watcher()
        self.out.write("\n")
        self.out.flush()
        return self.exit_code


def render_continual_update(event: ContinualUpdate) -> str:
    """The learner's note: what changed, what it cost, what waits on the
    user. Memories held back were distilled only from tool output; the user
    confirms one by asking to remember it."""
    line = f"[learned] {', '.join(event.changes)}"
    usage = event.usage or {}
    tokens_in = int(usage.get("input_tokens", 0) or 0)
    tokens_out = int(usage.get("output_tokens", 0) or 0)
    if tokens_in or tokens_out:
        line += f" ({tokens_in} in / {tokens_out} out tokens)"
    for held in event.held:
        line += (
            f"\n  [held] {held} — from tool output only; say \"remember ...\" "
            "to keep it"
        )
    return line
