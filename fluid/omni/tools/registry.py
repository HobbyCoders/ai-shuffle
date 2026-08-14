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

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from omni.tools.mcp.manager import McpManager

# A local (builtin/inproc) handler: takes the parsed input and the tool context,
# returns anything ``_normalize`` understands (ToolOutcome | str | SDK dict).
Handler = Callable[[dict[str, Any], "ToolCtx"], Awaitable[Any]]

_RISK_ORDER = {"safe": 0, "write": 1, "destructive": 2}


@dataclass
class ToolCtx:
    """Per-session execution context threaded into every dispatch.

    ``read_files`` is the read-before-write registry: builtins record the files
    they Read here so Edit/Write can enforce read-before-write within a session.
    """

    cwd: str = "."
    session_id: str | None = None
    read_files: set[str] = field(default_factory=set)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: str = "safe"  # "safe" | "write" | "destructive"
    source: str = "builtin"  # "builtin" | "inproc" | "mcp"
    code_mode_allowed: bool = True

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
class ToolOutcome:
    """Dual-output tool result: ``text`` for the model, ``ui`` for clients."""

    text: str
    ui: Any = None
    is_error: bool = False
    input_required: InputRequired | None = None


@dataclass
class _Entry:
    spec: ToolSpec
    handler: Handler | None = None  # None for MCP tools (routed to the manager)
    server: str | None = None  # MCP/inproc server name


def _normalize(result: Any) -> ToolOutcome:
    """Coerce a handler return into a ToolOutcome.

    Accepts a ToolOutcome, a plain string, or the claude_agent_sdk MCP envelope
    (``{"content": [{"type": "text", "text": ...}], "isError"?: bool}``) so the
    ~10 AI-Shuffle tool modules port over with import-line changes only.
    """
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, str):
        return ToolOutcome(text=result)
    if isinstance(result, dict) and "content" in result:
        parts = [
            c.get("text", "")
            for c in result.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        is_error = bool(result.get("isError") or result.get("is_error"))
        return ToolOutcome(text="".join(parts), ui=result, is_error=is_error)
    return ToolOutcome(text=json.dumps(result, default=str))


class ToolRegistry:
    def __init__(self, *, manager: McpManager | None = None) -> None:
        self._entries: dict[str, _Entry] = {}
        self._loaded: set[str] = set()
        self._manager = manager or McpManager()

    # ─── Registration ────────────────────────────────────────────────────

    def register_builtin(self, spec: ToolSpec, handler: Handler) -> None:
        """Register a bare-named builtin (Read/Edit/Bash/...). Always loaded."""
        self._entries[spec.name] = _Entry(spec=spec, handler=handler)
        self._loaded.add(spec.name)

    def register_inproc(self, server: "Any") -> None:
        """Register an in-process MCP server (from ``create_inproc_server``).

        Each tool surfaces as ``mcp__<server>__<tool>`` and is loaded eagerly —
        in-proc schemas are cheap and belong in the prefix like builtins.
        """
        for tool in server.tools:
            name = f"mcp__{server.name}__{tool.name}"
            spec = ToolSpec(
                name=name, description=tool.description, input_schema=tool.input_schema,
                risk=tool.risk, source="inproc",
            )
            self._entries[name] = _Entry(spec=spec, handler=tool.as_handler(), server=server.name)
            self._loaded.add(name)

    async def connect_mcp(self, name: str, config: dict[str, Any]) -> list[ToolSpec]:
        """Connect an MCP server and register its catalog (schemas deferred).

        Tools become *known* and searchable immediately but stay unloaded until
        ``load_schema`` — the progressive-disclosure seam.
        """
        descriptors = await self._manager.connect(name, config)
        specs: list[ToolSpec] = []
        for d in descriptors:
            tool_name = f"mcp__{name}__{d.name}"
            spec = ToolSpec(
                name=tool_name, description=d.description, input_schema=d.input_schema,
                risk=d.risk, source="mcp",
            )
            self._entries[tool_name] = _Entry(spec=spec, server=name)
            specs.append(spec)
        return specs

    # ─── Discovery ───────────────────────────────────────────────────────

    def list_tools(self, *, loaded_only: bool = True) -> list[ToolSpec]:
        return [
            e.spec for name, e in self._entries.items()
            if not loaded_only or name in self._loaded
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

    # ─── Dispatch ────────────────────────────────────────────────────────

    async def dispatch(
        self,
        name: str,
        input: dict[str, Any],
        ctx: ToolCtx | None = None,
        *,
        input_responses: Any = None,
        request_state: str | None = None,
    ) -> ToolOutcome:
        """``input_responses``/``request_state`` are the MRTR retry leg: echo a
        prior ``input_required`` outcome's ``request_state`` together with the
        collected answers and the MCP server resumes the paused call. Only
        meaningful for MCP-sourced tools; ignored for in-proc handlers."""
        ctx = ctx or ToolCtx()
        entry = self._entries.get(name)
        if entry is None:
            return ToolOutcome(text=f"unknown tool: {name}", is_error=True)
        try:
            if entry.spec.source == "mcp":
                _, server, tool = name.split("__", 2)
                return await self._manager.dispatch(
                    server, tool, input,
                    input_responses=input_responses, request_state=request_state,
                )
            assert entry.handler is not None
            return _normalize(await entry.handler(input, ctx))
        except Exception as exc:  # keep tool errors in-context, never crash the loop
            return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)

    async def aclose(self) -> None:
        await self._manager.aclose()
