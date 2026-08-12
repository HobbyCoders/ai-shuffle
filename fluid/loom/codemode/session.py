"""CodeModeSession — the code-mode orchestrator wired into ``NativeLoop``.

Owns the persistent kernel for a session, projects the registry's work-tools
into the kernel VFS, and implements the four code-mode tools:

- ``run_python``    — execute code in the persistent kernel (engine-driven);
- ``list_tools``    — names + one-line summaries of callable tools;
- ``search_tools``  — keyword search over the same catalog;
- ``read_tool_docs``— the generated typed stub (signature + docstring) for a tool.

When code-mode is active the model's *schema* is only these four tools; the
underlying tools stay in the registry (dispatched via host calls) and in the
projection (discoverable), carrying zero prompt-schema cost.

Each in-kernel tool call is gated by ``HostBridge`` — the same PreToolUse hook →
PermissionEngine (incl. doom-loop) → dispatch → PostToolUse pipeline the JSON
path uses, with the ``code_mode_allowed=False`` escape hatch enforced host-side.
The session wraps that bridge with the engine's ``PermissionRequest`` round-trip
(``on_permission``) and emits ``ToolUse``/``ToolResult`` events so in-kernel calls
are as observable as JSON calls.
"""

from __future__ import annotations

from typing import Any

from loom.codemode.bridge import HostBridge
from loom.codemode.kernel import HostReply
from loom.codemode.projection import Projection
from loom.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from loom.events.model import ToolResult, ToolUse
from loom.permissions.engine import PermissionEngine
from loom.store.base import new_id
from loom.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

CODE_MODE_SOURCE = "codemode"

_RUN_PYTHON_DESC = (
    "Execute Python in a persistent per-session kernel (state survives across "
    "calls). Discover tools with list_tools/search_tools/read_tool_docs, then "
    "`from tools.<name> import <name>` and call them — each call is permission-"
    "gated on the host. Network is disabled; only a stdlib-safe import allowlist "
    "is available. The last expression's repr is returned (REPL-style); large "
    "tool results come back as a ResultRef you can slice in a later call."
)


class CodeModeSession:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        config: KernelConfig | None = None,
        exec_timeout: float = 30.0,
    ) -> None:
        self._registry = registry
        self._config = config or KernelConfig()
        self._exec_timeout = exec_timeout
        self._kernel: SubprocessKernel | None = None
        self._vfs_signature: tuple | None = None

    # ── projection (rebuilt cheaply; reflects late-loaded MCP tools) ──
    def _projection(self) -> Projection:
        specs = [
            s for s in self._registry.list_tools(loaded_only=False)
            if s.source != CODE_MODE_SOURCE
        ]
        return Projection(specs)

    # ── the three discovery tools ──
    def render_list(self) -> str:
        entries = self._projection().entries()
        if not entries:
            return "No tools available."
        lines = ["Tools callable from run_python via `from tools.<name> import <name>`:"]
        lines += [f"- {leaf}: {one}" for leaf, one in entries]
        return "\n".join(lines)

    def render_search(self, query: str) -> str:
        hits = self._projection().search(query)
        if not hits:
            return f"No tools match {query!r}."
        return "\n".join(f"- {leaf}: {one}" for leaf, one in hits)

    def render_docs(self, name: str) -> str:
        src = self._projection().docs(name)
        if src is None:
            return f"No such tool: {name!r}. Try list_tools or search_tools."
        return src

    # ── run_python (driven by the engine, not the registry) ──
    async def _ensure_kernel(self) -> SubprocessKernel:
        sources = self._projection().sources()
        signature = tuple(sorted(sources.items()))
        if self._kernel is None:
            self._kernel = SubprocessKernel(self._config)
            await self._kernel.start()
            await self._kernel.set_vfs(sources)
            self._vfs_signature = signature
        elif signature != self._vfs_signature:
            await self._kernel.set_vfs(sources)
            self._vfs_signature = signature
        return self._kernel

    async def run_python(self, *, sid: str, turn_id: str, input: dict, loop: Any) -> ToolOutcome:
        code = input.get("code") or ""
        try:
            timeout = float(input.get("timeout") or self._exec_timeout)
        except (TypeError, ValueError):
            timeout = self._exec_timeout
        kernel = await self._ensure_kernel()

        # Bridge: the JSON-path gate for in-kernel tool calls. Its ``ask`` verdict
        # is surfaced as an engine PermissionRequest (parking the turn until the
        # client answers) via on_permission.
        async def _on_permission(spec: ToolSpec, tool_input: dict) -> bool:
            verdict = await loop._await_permission(
                sid, turn_id, spec.name, new_id("call"), tool_input, spec.risk
            )
            return verdict == "allow"

        # No engine ⇒ allow-all, matching the JSON path (which skips gating when
        # ``permissions is None``).
        permissions = loop.permissions or PermissionEngine("never_ask")
        bridge = HostBridge(
            self._registry, permissions,
            hooks=loop.hooks, on_permission=_on_permission, ctx=loop.ctx,
        )

        async def _host_call(name: str, kwargs: dict) -> HostReply:
            tool_use_id = new_id("call")
            await loop._emit(ToolUse(
                session_id=sid, turn_id=turn_id,
                tool_use_id=tool_use_id, name=name, input=kwargs,
            ))
            reply = await bridge.on_host_call(name, kwargs)
            if reply.result is not None:
                out, is_error = reply.result, False
            elif reply.denied:
                out, is_error = reply.message or "permission denied", True
            else:
                out, is_error = reply.error or "tool error", True
            await loop._emit(ToolResult(
                session_id=sid, turn_id=turn_id, tool_use_id=tool_use_id,
                name=name, output=out, is_error=is_error,
            ))
            return reply

        result = await kernel.exec(code, timeout=timeout, on_host_call=_host_call)

        parts: list[str] = []
        if result.stdout.strip():
            parts.append(result.stdout.rstrip("\n"))
        if result.value_repr:
            parts.append(f"=> {result.value_repr}")
        text = "\n".join(parts) if parts else "(no output)"
        if result.error:
            text = (text + "\n" if parts else "") + f"Error: {result.error}"
        return ToolOutcome(text=text, is_error=bool(result.error))

    async def aclose(self) -> None:
        if self._kernel is not None:
            await self._kernel.stop()
            self._kernel = None


# ── registration ──────────────────────────────────────────────────────────

def register_code_mode_tools(registry: ToolRegistry, session: CodeModeSession) -> None:
    """Register the four code-mode tools as the model's entire schema surface."""

    async def _run_guard(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        # The engine intercepts run_python and drives the kernel directly; this
        # guard only fires if something dispatches it out of band.
        return ToolOutcome(
            text="run_python is engine-driven and cannot be dispatched directly",
            is_error=True,
        )

    async def _list(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text=session.render_list())

    async def _search(input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text=session.render_search(input.get("query", "")))

    async def _docs(input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text=session.render_docs(input.get("name", "")))

    registry.register_builtin(
        ToolSpec(
            name="run_python", description=_RUN_PYTHON_DESC,
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute."},
                    "timeout": {"type": "number", "description": "Wall-clock seconds (optional)."},
                },
                "required": ["code"],
            },
            source=CODE_MODE_SOURCE,
        ),
        _run_guard,
    )
    registry.register_builtin(
        ToolSpec(
            name="list_tools",
            description="List every tool callable from run_python (name + one-line summary).",
            input_schema={"type": "object", "properties": {}},
            source=CODE_MODE_SOURCE,
        ),
        _list,
    )
    registry.register_builtin(
        ToolSpec(
            name="search_tools",
            description="Search callable tools by keyword.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            source=CODE_MODE_SOURCE,
        ),
        _search,
    )
    registry.register_builtin(
        ToolSpec(
            name="read_tool_docs",
            description="Show the typed Python stub (signature + docstring) for a tool.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            source=CODE_MODE_SOURCE,
        ),
        _docs,
    )
