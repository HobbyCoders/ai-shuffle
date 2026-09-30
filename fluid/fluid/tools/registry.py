"""ToolRegistry — one dispatch surface over builtin, in-process, and MCP tools.

Progressive disclosure (the Manus/Anthropic "code execution with MCP" lesson):
MCP tool *schemas* stay out of the cached prompt prefix until needed. A server's
tools are *known* (name + description, searchable) as soon as it connects, but a
schema is only ``load_schema``-d — and thus eligible for ``list_tools`` and the
provider request — on demand. Builtin and in-process tools are always loaded.

Tool naming follows the Claude Code convention kept for ecosystem compat:
MCP and in-process-server tools are ``mcp__<server>__<tool>``; builtins keep
their bare names (``Read``, ``Edit``, ...).
"""

from __future__ import annotations

import asyncio

import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from fluid.tools.mcp.manager import McpManager

log = logging.getLogger(__name__)

# The id of the tool call being dispatched, for handlers that take no ctx.
# In-process server tools (``register_inproc``) are called with their
# arguments only, so a host tool that needs its own tool_use_id (to join its
# UI card, to key a background task) reads it here. ``dispatch`` sets it from
# ``ctx.extra["tool_use_id"]``, which the native loop stamps per call and the
# delegated bundled bridge fills from the CLI's request. None outside a call
# or when the caller did not supply an id.
current_tool_use_id: ContextVar[str | None] = ContextVar(
    "fluid_current_tool_use_id", default=None,
)

# A local (builtin/inproc) handler: takes the parsed input and the tool context,
# returns anything ``_normalize`` understands (ToolOutcome | str | SDK dict).
Handler = Callable[[dict[str, Any], "ToolCtx"], Awaitable[Any]]

_RISK_ORDER = {"safe": 0, "write": 1, "destructive": 2}

# The provider's tool-name grammar (Anthropic: ``^[a-zA-Z0-9_-]{1,64}$``). A
# name outside it fails every request for the rest of the session.
API_TOOL_NAME_MAX = 64
_API_NAME_BAD_CHAR = re.compile(r"[^a-zA-Z0-9_-]")


def api_tool_name(server: str, tool: str, taken: Callable[[str], bool]) -> str:
    """``mcp__<server>__<tool>`` made valid for the provider.

    Disallowed characters become ``_``; a name past 64 characters loses the
    tail of its tool part and gains a 6-hex digest of the original so two long
    names stay distinct. ``taken`` reports names already registered to a
    different tool, and a collision gets the same digest suffix.
    """
    prefix = f"mcp__{_API_NAME_BAD_CHAR.sub('_', server)}__"
    safe = _API_NAME_BAD_CHAR.sub("_", tool) or "_"
    digest = hashlib.sha1(tool.encode("utf-8")).hexdigest()[:6]
    room = max(API_TOOL_NAME_MAX - len(prefix), len(digest) + 2)

    def with_digest() -> str:
        return prefix + safe[: room - len(digest) - 1] + "_" + digest

    if len(safe) > room:
        return with_digest()
    name = prefix + safe
    return with_digest() if taken(name) else name


@dataclass
class ToolCtx:
    """Per-session execution context threaded into every dispatch.

    ``read_files`` is the read-before-write registry: builtins record the files
    they Read here so Edit/Write can enforce read-before-write within a session.
    ``read_windows`` is its line-level half (see :mod:`fluid.tools.read_window`):
    the inclusive 1-based spans each Read delivered, so Edit can also ask
    whether the lines it is replacing were ever in front of the model.

    ``read_seen`` and ``grep_seen`` are the repeat-work registries. Read records
    ``(path, offset, limit) -> (mtime_ns, size)`` for every successful text read;
    a read that lands on an entry already there returns its full result with a
    short note saying so, and an Edit or a Write bumps the file's mtime, which
    drops the match on its own. Grep records the search-relevant half of its
    input mapped to a hash of the result it returned; a repeat whose result
    hashes the same returns a pointer at the earlier copy instead of the rows,
    and a repeat whose result changed (an edit, a new file) is delivered in
    full. SessionSearch keeps the same kind of map in
    ``extra["session_search_seen"]``.

    ``read_delivered`` is the honesty half of that registry: only the keys whose
    payload the calling surface actually put in front of the model. A repeat-read
    note claims "you already have this", which is false when the earlier result
    was handed to a code-mode cell as a ResultRef stub the cell never printed.
    ``result_budget`` is what the surface declares about itself — the largest
    result it delivers verbatim (None = no ceiling, the JSON tool path).
    """

    cwd: str = "."
    session_id: str | None = None
    read_files: set[str] = field(default_factory=set)
    read_windows: dict[str, list[tuple[int, int | None]]] = field(default_factory=dict)
    read_seen: dict[tuple[str, int | None, int | None], tuple[int, int]] = field(
        default_factory=dict
    )
    read_delivered: set[tuple[str, int | None, int | None]] = field(
        default_factory=set
    )
    grep_seen: dict[tuple[Any, ...], str] = field(default_factory=dict)
    result_budget: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: str = "safe"  # "safe" | "write" | "destructive"
    source: str = "builtin"  # "builtin" | "inproc" | "mcp"
    code_mode_allowed: bool = True
    # Optional per-input risk override, consulted by the permission engine at
    # evaluation time: ``classify_risk(input) -> "safe" | "write" | "destructive"``.
    # ``risk`` stays the static ceiling used for schema-level decisions (plan
    # mode); the hook lets a tool like Bash report a read-only command as safe.
    classify_risk: Callable[[dict[str, Any]], str] | None = field(
        default=None, compare=False, repr=False
    )
    # Optional ``allow_always`` scoping: ``session_scope(input)`` returns the
    # set of labels a session allow covers (Bash: the programs the command
    # runs). None → the allow covers every call to the tool.
    session_scope: Callable[[dict[str, Any]], frozenset[str] | None] | None = field(
        default=None, compare=False, repr=False
    )
    # Optional availability probe for tools whose backing can be absent (a
    # container runtime, a device, a binary). Consulted through the registry's
    # TTL cache with a last-good grace window — one flaky probe must never
    # strip a tool out from under a session mid-turn. None → always available.
    check_available: Callable[[], bool] | None = field(
        default=None, compare=False, repr=False
    )
    # ``ctx.extra`` keys this tool needs before it can do anything (a host
    # injection: a dispatcher, a spawner, a store handle). The engine drops a
    # tool whose keys are missing from the request schemas, so a capability the
    # host never wired is invisible rather than a schema that errors on call
    # (the handler's own guard stays as the dispatch backstop). Hosts wire
    # capabilities at attach time, before the first turn — the surface is
    # stable for the whole session.
    requires_extra: tuple[str, ...] = ()

    def scope_of(self, input: dict[str, Any]) -> frozenset[str] | None:
        """Labels an ``allow_always`` for this call would cover (None = all)."""
        if self.session_scope is None:
            return None
        try:
            return self.session_scope(input)
        except Exception:
            return None

    def effective_risk(self, input: dict[str, Any]) -> str:
        """Risk of one concrete call: the hook's answer, capped at ``risk``."""
        if self.classify_risk is None:
            return self.risk
        try:
            risk = self.classify_risk(input)
        except Exception:  # a broken classifier must never lower the guard
            return self.risk
        if risk not in _RISK_ORDER:
            return self.risk
        return risk if _RISK_ORDER[risk] <= _RISK_ORDER[self.risk] else self.risk

    def to_api(self) -> dict[str, Any]:
        """Anthropic tool-def shape (the serializer places cache_control)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True, kw_only=True)
class InputRequired:
    """MRTR (Multi Round-Trip Request) marker — spec 2026-07-28.

    An MCP ``tools/call`` may return ``resultType == "input_required"`` instead
    of a result: the server needs elicitation/sampling/roots input before it can
    finish. This maps 1:1 onto the harness ``PermissionRequest``/``RespondQuestion``
    round-trip. ``request_state`` is echoed back on the retry alongside the
    collected ``inputResponses`` (see ``McpManager.dispatch``).
    """

    server: str
    tool: str
    request_state: str | None
    requests: dict[str, Any]  # request-id → descriptor {method, message, schema}


@dataclass(frozen=True, kw_only=True)
class ToolImage:
    """Binary image output (file reads, screenshots) carried alongside text.

    Kept as (media_type, base64) rather than a ``fluid.messages`` block so the
    tools layer stays independent of the conversation model; the engine turns
    these into ``Image`` content blocks on the tool result.
    """

    media_type: str
    data: str  # base64


@dataclass(frozen=True, kw_only=True)
class BackgroundTaskReceipt:
    """Durable identity returned after a host detached a background worker."""

    task_id: str
    worker_session_id: str | None = None
    description: str | None = None
    # A SERVICE, not a job: a process meant to stay up (a dev server, a
    # tunnel, a file watcher) for as long as the work needs it. It is
    # registered, surfaced and stoppable like any other background task, but
    # it is EXCLUDED from park accounting — a turn that started one may end
    # without parking, and ``SessionParked.pending_task_ids`` never lists it.
    # Without this the Monitor contract ("the command must exit") contradicts
    # its own dev-server use case and every later turn parks on a process
    # whose only "completion" is its death.
    serves: bool = False


@dataclass(frozen=True, kw_only=True)
class ContentWindow:
    """This result is a *slice* of a bigger payload the tool can also return.

    ``content_id`` is the identity that whole payload has (same shape as
    ``ToolOutcome.content_id``), and ``start``/``stop`` are the 0-based, Python
    slice bounds of the lines this result covers within it — ``stop=None``
    meaning "through the last line". Read stamps every windowed read with the
    identity the *whole* file would have had at this same mtime/size, which is
    what lets code mode answer a re-read at a new offset/limit out of a
    ResultRef it is already holding for the whole file.
    """

    content_id: str
    start: int
    stop: int | None = None


@dataclass(frozen=True, kw_only=True)
class ToolOutcome:
    """Dual-output tool result: ``text`` for the model, ``ui`` for clients."""

    text: str
    ui: Any = None
    is_error: bool = False
    # The tool ran to completion and the "error" is ordinary data (a command's
    # non-zero exit code). The JSON surface still flags is_error so the model
    # notices; code mode returns the text instead of raising, so a cell that
    # probes exit codes (git merge-base, grep -q, test) keeps running.
    soft_error: bool = False
    # Only missing read-only discovery targets are explicitly recoverable.
    error_code: str | None = None
    # Raw shell stdout/stderr and the actual process exit status.
    shell_result: tuple[str, int] | None = None
    input_required: InputRequired | None = None
    images: tuple[ToolImage, ...] = ()
    background_task: BackgroundTaskReceipt | None = None
    # Opaque identity of the payload in ``text``: two outcomes carrying the same
    # ``content_id`` carry byte-identical content. Read stamps it with the file
    # window plus the file's mtime/size, which is what lets code mode answer a
    # repeat read with a pointer at the ResultRef it already handed the kernel
    # instead of a second copy. None = "no identity claim" — the default, and
    # what anything that appends to ``text`` after dispatch must reset it to.
    content_id: str | None = None
    # Set when ``text`` is a window into a larger payload this same tool can
    # return whole: it names that whole payload's identity and the slice this
    # one covers. Code mode uses it to answer a windowed re-read from a ref it
    # already holds for the whole file. Reset it alongside ``content_id``.
    window_of: ContentWindow | None = None


@dataclass
class _Entry:
    spec: ToolSpec
    handler: Handler | None = None  # None for MCP tools (routed to the manager)
    server: str | None = None  # MCP/inproc server name
    remote_name: str | None = None  # the MCP server's own name for the tool
    # Per-tool replacement for the registry's ``external_call_timeout``
    # (only read when ``has_call_timeout``); None means unbounded.
    has_call_timeout: bool = False
    call_timeout: float | None = None


def _normalize(result: Any) -> ToolOutcome:
    """Coerce a handler return into a ToolOutcome.

    Accepts a ToolOutcome, a plain string, or the claude_agent_sdk MCP envelope
    (``{"content": [{"type": "text", "text": ...}], "isError"?: bool}``) so the
    a host app's own tool modules port over with import-line changes only.
    """
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, str):
        return ToolOutcome(text=result)
    if isinstance(result, dict) and "content" in result:
        parts: list[str] = []
        images: list[ToolImage] = []
        for c in result.get("content", []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "image" and c.get("data"):
                images.append(ToolImage(
                    media_type=c.get("mimeType") or c.get("media_type") or "image/png",
                    data=c["data"],
                ))
        is_error = bool(result.get("isError") or result.get("is_error"))
        return ToolOutcome(
            text="".join(parts), ui=result, is_error=is_error, images=tuple(images),
        )
    return ToolOutcome(text=json.dumps(result, default=str))


# Availability-probe caching: verdicts hold for the TTL; a failure within the
# grace window of a prior success serves the last-good True instead — a
# transient probe timeout must not strip a toolset out from under a session.
PROBE_TTL_S = 30.0
PROBE_GRACE_S = 60.0

# Hard ceiling on one external (MCP or in-process server) tool call as seen
# from the registry, on top of the MCP client's own per-request deadline. The
# client's deadline covers the wire; this one covers everything around it
# (the actor queue, a wedged in-process handler) so a call can never hang the
# turn. Slightly above the client default so the client's clearer message
# wins when the wire is what stalled.
DEFAULT_EXTERNAL_CALL_TIMEOUT_S = 75.0
# error_code values a code-mode cell must NOT continue past (the tool did
# not run); every other external error becomes the call's result text.
FATAL_EXTERNAL_ERROR_CODES = frozenset({"tool_timeout", "unavailable"})
# ``_bounded_external``'s "use the registry's own bound" default.
_REGISTRY_BOUND: Any = object()


class ToolRegistry:
    def __init__(
        self,
        *,
        manager: McpManager | None = None,
        external_call_timeout: float | None = DEFAULT_EXTERNAL_CALL_TIMEOUT_S,
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        self._loaded: set[str] = set()
        self._manager = manager or McpManager()
        # Seconds one MCP/inproc dispatch may take; None or <=0 disables.
        self.external_call_timeout = external_call_timeout
        # name -> (verdict, verdict_ts, last_true_ts)
        self._probe_cache: dict[str, tuple[bool, float, float]] = {}
        self._clock: Callable[[], float] = time.monotonic

    def available(self, name: str) -> bool:
        """The tool's probe verdict (cached; grace-windowed). True without one."""
        entry = self._entries.get(name)
        if entry is None:
            return False
        probe = entry.spec.check_available
        if probe is None:
            return True
        now = self._clock()
        cached = self._probe_cache.get(name)
        if cached is not None and now - cached[1] < PROBE_TTL_S:
            return cached[0]
        last_true = cached[2] if cached is not None else 0.0
        try:
            verdict = bool(probe())
        except Exception:
            verdict = False
        if verdict:
            last_true = now
        elif last_true and now - last_true < PROBE_GRACE_S:
            verdict = True  # transient failure — serve the last-good verdict
        self._probe_cache[name] = (verdict, now, last_true)
        return verdict

    # ─── Registration ────────────────────────────────────────────────────

    def register_builtin(self, spec: ToolSpec, handler: Handler) -> None:
        """Register a bare-named builtin (Read/Edit/Bash/...). Always loaded."""
        self._entries[spec.name] = _Entry(spec=spec, handler=handler)
        self._loaded.add(spec.name)

    def register_inproc(
        self,
        server: Any,
        *,
        defer: bool = False,
        call_timeouts: Mapping[str, float | None] | None = None,
    ) -> None:
        """Register an in-process MCP server (from ``create_inproc_server``).

        Each tool surfaces as ``mcp__<server>__<tool>``. By default schemas
        load eagerly into the prompt prefix like builtins; pass ``defer=True``
        to register them in the deferred tier instead (known + searchable via
        ToolSearch, schema out of context until loaded) — the right choice for
        large in-proc servers whose tools are rarely used in a typical turn.

        ``call_timeouts`` maps a tool's own name (``tool.name``, not the
        ``mcp__`` name) to the seconds one call may take, replacing
        ``external_call_timeout`` for that tool; ``None`` leaves it unbounded.
        It is for tools with a legitimate long block that already carry their
        own deadline (a synchronous subagent dispatch), which the registry's
        hang guard would otherwise cancel mid-run.
        """
        timeouts = dict(call_timeouts or {})
        for tool in server.tools:
            name = f"mcp__{server.name}__{tool.name}"
            spec = ToolSpec(
                name=name, description=tool.description, input_schema=tool.input_schema,
                risk=tool.risk, source="inproc",
            )
            self._entries[name] = _Entry(
                spec=spec, handler=tool.as_handler(), server=server.name,
                has_call_timeout=tool.name in timeouts,
                call_timeout=timeouts.get(tool.name),
            )
            if not defer:
                self._loaded.add(name)

    async def connect_mcp(self, name: str, config: dict[str, Any]) -> list[ToolSpec]:
        """Connect an MCP server and register its catalog (schemas deferred).

        Tools become *known* and searchable immediately but stay unloaded until
        ``load_schema`` — the progressive-disclosure seam.
        """
        descriptors = await self._manager.connect(name, config)
        specs: list[ToolSpec] = []
        # A server's readOnlyHint is the server's word about itself. Taken
        # at face value it skips the permission prompt, so it counts only
        # when the config says the
        # server is trusted. A destructiveHint always counts: it raises the
        # guard, never lowers it.
        trusted = bool(config.get("trust_annotations", False))

        def taken_by_other(candidate: str, remote: str) -> bool:
            # Only another MCP tool is a collision to rename around; a builtin
            # or in-proc holder of the name makes the server's tool skip below.
            entry = self._entries.get(candidate)
            return (
                entry is not None
                and entry.spec.source == "mcp"
                and (entry.server != name or entry.remote_name != remote)
            )

        for d in descriptors:
            tool_name = api_tool_name(name, d.name, lambda c, r=d.name: taken_by_other(c, r))
            if tool_name != f"mcp__{name}__{d.name}":
                log.warning(
                    "mcp: server %r tool %r registered as %s (name made valid for the API)",
                    name, d.name, tool_name,
                )
            existing = self._entries.get(tool_name)
            if existing is not None and existing.spec.source != "mcp":
                # A server may not replace a builtin or in-proc tool: it would
                # inherit the always-loaded status and route the call remotely.
                log.warning(
                    "mcp: server %r tool %r skipped; %s is a %s tool",
                    name, d.name, tool_name, existing.spec.source,
                )
                continue
            risk = d.risk if trusted or d.risk != "safe" else "write"
            spec = ToolSpec(
                name=tool_name, description=d.description, input_schema=d.input_schema,
                risk=risk, source="mcp",
            )
            self._entries[tool_name] = _Entry(spec=spec, server=name, remote_name=d.name)
            specs.append(spec)
        return specs

    # ─── Discovery ───────────────────────────────────────────────────────

    def list_tools(self, *, loaded_only: bool = True) -> list[ToolSpec]:
        return [
            e.spec for name, e in self._entries.items()
            if (not loaded_only or name in self._loaded)
            and (e.spec.check_available is None or self.available(name))
        ]

    def deferred_tools(self) -> list[ToolSpec]:
        """Known-but-unloaded tools — the name+description tier of progressive
        disclosure. Their schemas enter the prompt only after ``load_schema``."""
        return [
            e.spec for name, e in self._entries.items() if name not in self._loaded
        ]

    def search_tools(self, query: str) -> list[ToolSpec]:
        """Substring/word match over name + description, best matches first."""
        tokens = [t for t in query.lower().split() if t]
        scored: list[tuple[int, str, ToolSpec]] = []
        for name, entry in self._entries.items():
            hay = f"{name} {entry.spec.description}".lower()
            score = sum(1 for t in tokens if t in hay)
            if not tokens and query.lower() in hay:
                score = 1
            if score:
                scored.append((score, name, entry.spec))
        scored.sort(key=lambda s: (-s[0], s[1]))
        return [s[2] for s in scored]

    def load_schema(self, name: str) -> ToolSpec:
        """Mark an MCP tool loaded (its schema now enters the prefix)."""
        entry = self._entries[name]
        self._loaded.add(name)
        return entry.spec

    def get(self, name: str) -> ToolSpec | None:
        entry = self._entries.get(name)
        return entry.spec if entry else None

    def amend_spec(self, name: str, **changes: Any) -> None:
        """Replace fields on a registered spec (description refinement seam).

        Used once at session build to specialize prompt-facing text to the
        session — the permitted tool surface, config-dependent limits — before
        the first request is serialized. Amending an unknown name is a no-op.
        """
        entry = self._entries.get(name)
        if entry is not None:
            entry.spec = replace(entry.spec, **changes)

    # ─── Dispatch ────────────────────────────────────────────────────────

    async def dispatch(
        self,
        name: str,
        input: dict[str, Any],
        ctx: ToolCtx | None = None,
        *,
        input_responses: Any = None,
        request_state: str | None = None,
        raw_shell_output: bool = False,
    ) -> ToolOutcome:
        """``input_responses``/``request_state`` are the MRTR retry leg: echo a
        prior ``input_required`` outcome's ``request_state`` together with the
        collected answers and the MCP server resumes the paused call. Only
        meaningful for MCP-sourced tools; ignored for in-proc handlers.
        ``raw_shell_output`` is only for code mode, which frames its complete
        model-facing output after Python has parsed/sliced the raw strings.

        A string ``ctx.extra["tool_use_id"]`` is published as
        ``current_tool_use_id`` for the duration of the call.
        """
        ctx = ctx or ToolCtx()
        tool_use_id = (getattr(ctx, "extra", None) or {}).get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return await self._dispatch(
                name, input, ctx, input_responses=input_responses,
                request_state=request_state, raw_shell_output=raw_shell_output,
            )
        token = current_tool_use_id.set(tool_use_id)
        try:
            return await self._dispatch(
                name, input, ctx, input_responses=input_responses,
                request_state=request_state, raw_shell_output=raw_shell_output,
            )
        finally:
            current_tool_use_id.reset(token)

    async def _dispatch(
        self,
        name: str,
        input: dict[str, Any],
        ctx: ToolCtx,
        *,
        input_responses: Any,
        request_state: str | None,
        raw_shell_output: bool,
    ) -> ToolOutcome:
        from fluid.tools.hardening import (
            coerce_args,
            elision_notice,
            fence_untrusted,
            fence_shell_output,
            scrub_error_text,
        )

        entry = self._entries.get(name)
        if entry is None:
            return ToolOutcome(text=f"unknown tool: {name}", is_error=True)
        if entry.spec.check_available is not None and not self.available(name):
            return ToolOutcome(
                text=(
                    f"tool {name} is temporarily unavailable (its backing "
                    "service did not respond). This is an outage, not a "
                    "denial: do not retry it immediately; use another tool "
                    "that is itself available, or tell the user."
                ),
                is_error=True,
            )
        try:
            input = coerce_args(input, entry.spec.input_schema)
            if entry.spec.source == "mcp":
                _, server, tool = name.split("__", 2)
                if entry.server is not None:
                    server = entry.server
                if entry.remote_name is not None:
                    tool = entry.remote_name  # the server knows only its own name
                outcome = await self._bounded_external(name, self._manager.dispatch(
                    server, tool, input,
                    input_responses=input_responses, request_state=request_state,
                ))
            else:
                assert entry.handler is not None
                call_input = input
                if input_responses is not None:
                    # The builtin/in-proc leg of the input-required round-trip:
                    # collected answers ride a reserved key so the handler's
                    # second dispatch can read them.
                    call_input = {**input, "__input_responses__": dict(input_responses)}
                call = entry.handler(call_input, ctx)
                if entry.spec.source == "inproc":
                    limit = (
                        entry.call_timeout if entry.has_call_timeout
                        else self.external_call_timeout
                    )
                    outcome = _normalize(
                        await self._bounded_external(name, call, limit=limit)
                    )
                else:
                    outcome = _normalize(await call)
        except Exception as exc:  # keep tool errors in-context, never crash the loop
            text = scrub_error_text(f"{type(exc).__name__}: {exc}")
            if name in {"Bash", "Monitor"} and not raw_shell_output:
                text = fence_shell_output(text, f"{name} execution error")
            return ToolOutcome(text=text, is_error=True)
        # Code mode keeps raw shell text for parsing; its enclosing cell is
        # fenced after truncation, including output from later persistent cells.
        if (
            outcome.shell_result is not None and outcome.shell_result[0].strip()
            and not raw_shell_output
        ):
            output, code = outcome.shell_result
            outcome = replace(
                outcome,
                text=fence_shell_output(output, f"{name} stdout/stderr", exit_code=code),
                content_id=None, window_of=None,
            )
        # External-source hardening: MCP results are authored by whatever the
        # server fetched, so their text is fenced as data (with any embedded
        # elision markers flagged first, inside the fence, next to the data
        # they describe). Errors are bounded and de-framed instead — a fence
        # around an error would bury the reason the model must read.
        if entry.spec.source == "mcp":
            if outcome.is_error:
                outcome = replace(outcome, text=scrub_error_text(outcome.text))
            elif outcome.text:
                text = outcome.text
                notice = elision_notice(text)
                if notice:
                    text = f"{text}\n\n{notice}"
                outcome = replace(outcome, text=fence_untrusted(text, name))
        return outcome

    async def _bounded_external(
        self, name: str, call: Any, *, limit: Any = _REGISTRY_BOUND,
    ) -> Any:
        """Await an MCP/inproc call under ``external_call_timeout``.

        On expiry the call is cancelled and an ``is_error`` outcome with
        ``error_code="tool_timeout"`` comes back — the transcript gets a
        ToolResult either way, which is what a hung browser_snapshot never
        produced. ``limit`` replaces the registry bound for one call (a
        per-tool override from ``register_inproc``); None or <=0 disables it.
        """
        if limit is _REGISTRY_BOUND:
            limit = self.external_call_timeout
        if not limit or limit <= 0:
            return await call
        try:
            return await asyncio.wait_for(call, timeout=limit)
        except TimeoutError:
            return ToolOutcome(
                text=(
                    f"{name} did not return within {limit:g} s and was "
                    "cancelled. The tool is hung or its target is not "
                    "responding: do not simply re-issue the same call; try a "
                    "different route or report it."
                ),
                is_error=True,
                error_code="tool_timeout",
            )

    async def aclose(self) -> None:
        await self._manager.aclose()
