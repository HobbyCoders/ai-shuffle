"""CodeModeSession — the code-mode orchestrator wired into ``NativeLoop``.

Owns the persistent kernel for a session, projects the registry's work-tools
into the kernel VFS, and implements the four code-mode tools:

- ``run_python``    — execute code in the persistent kernel (engine-driven);
- ``list_tools``    — names + one-line summaries of callable tools;
- ``search_tools``  — keyword search over the same catalog;
- ``read_tool_docs``— the generated typed stub (signature + docstring) for a tool.

With a :class:`~fluid.codemode.skills.SkillStore` attached, the session is also
the driver of the *continual harness*: persistent skills mount into the kernel
as ``skills.<name>``, ``save_skill``/``delete_skill`` are projected as gated
in-kernel tools, every executed cell is journaled, and (when a synthesizer is
wired) new journal entries trigger a background distillation pass that turns
recurring workflows into new skills.

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

from fluid.codemode.bridge import HostBridge
from fluid.codemode.kernel import HostReply
from fluid.codemode.projection import Projection
from fluid.codemode.skills import SkillStore, referenced_skills, validate_skill
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from fluid.codemode.verify import verify_sources, verify_store
from fluid.events.model import ToolResult, ToolUse
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import new_id
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

CODE_MODE_SOURCE = "codemode"
SKILLS_SOURCE = "skills"

_RUN_PYTHON_DESC = (
    "Execute Python in a persistent per-session kernel (state survives across "
    "calls). Discover tools with list_tools/search_tools/read_tool_docs, then "
    "`from tools import Read, Edit` and call them as plain synchronous "
    "functions (`import tools; tools.Read(...)` also works) — each call is "
    "permission-gated on the host. Saved skills (reusable helpers that persist "
    "across sessions) import via `from skills import <name>`; persist a new one "
    "with the save_skill tool when you notice a reusable pattern. Network and "
    "direct file I/O are disabled (use the file tools); only a stdlib-safe "
    "import allowlist is available. The last expression's repr is returned "
    "(REPL-style); large tool results come back as a ResultRef you can slice "
    "in a later call."
)

# Journal previews are bounded so journal.jsonl (synthesis input) stays small.
_JOURNAL_STDOUT_CAP = 1500
_JOURNAL_VALUE_CAP = 300
_JOURNAL_EVENT_CAP = 2000  # user/assistant conversation events

# Images produced by in-kernel tool calls ride out on the cell's tool result.
# Capped so one loop over a directory of screenshots can't blow up the request.
_CELL_IMAGE_CAP = 20


class CodeModeSession:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        config: KernelConfig | None = None,
        exec_timeout: float = 30.0,
        skills: SkillStore | None = None,
        synthesizer: "Any | None" = None,  # fluid.codemode.continual.Synthesizer
    ) -> None:
        self._registry = registry
        self._config = config or KernelConfig()
        self._exec_timeout = exec_timeout
        self._kernel: SubprocessKernel | None = None
        self._vfs_signature: tuple | None = None
        self._skills = skills
        self._synthesizer = synthesizer

    @property
    def skills(self) -> SkillStore | None:
        return self._skills

    # ── projection (rebuilt cheaply; reflects late-loaded MCP tools) ──
    def _projection(self) -> Projection:
        specs = [
            s for s in self._registry.list_tools(loaded_only=False)
            if s.source != CODE_MODE_SOURCE
        ]
        return Projection(specs)

    # ── the three discovery tools (tools + persistent skills) ──
    def render_list(self) -> str:
        entries = self._projection().entries()
        lines: list[str] = []
        if entries:
            lines.append("Tools callable from run_python via `from tools import <name>`:")
            lines += [f"- {leaf}: {one}" for leaf, one in entries]
        if self._skills is not None:
            saved = self._skills.list()
            if saved:
                lines.append("Saved skills, import via `from skills import <name>`:")
                for s in saved:
                    flag = f" [BROKEN, not mounted: {s.broken}]" if s.broken else ""
                    lines.append(f"- {s.name}: {s.description} [used {s.uses}x]{flag}")
        return "\n".join(lines) if lines else "No tools available."

    def render_search(self, query: str) -> str:
        hits = [(leaf, one) for leaf, one in self._projection().search(query)]
        if self._skills is not None:
            tokens = [t for t in query.lower().split() if t]
            for s in self._skills.list():
                hay = f"skills.{s.name} {s.description}".lower()
                if any(t in hay for t in tokens) or (not tokens and query.lower() in hay):
                    hits.append((f"skills.{s.name}", s.description))
        if not hits:
            return f"No tools match {query!r}."
        return "\n".join(f"- {leaf}: {one}" for leaf, one in hits)

    def render_docs(self, name: str) -> str:
        if self._skills is not None:
            skill = self._skills.get(name.removeprefix("skills."))
            if skill is not None:
                return skill.source
        src = self._projection().docs(name)
        if src is None:
            return f"No such tool: {name!r}. Try list_tools or search_tools."
        return src

    # ── run_python (driven by the engine, not the registry) ──
    async def _mountable_skills(self) -> dict[str, str]:
        """Verified skill sources for the kernel mount.

        Stale skills (source changed since their last verdict) are re-verified
        in a throwaway kernel first; broken ones are excluded from the mount
        and flagged in ``list_tools``. Verification failure of the *pass*
        itself (e.g. kernel spawn issues) degrades to mounting as-is — the
        continual layer must never take down run_python.
        """
        if self._skills is None:
            return {}
        try:
            await verify_store(self._skills, config=self._config)
        except Exception:
            return self._skills.sources()
        return {
            s.name: s.source for s in self._skills.list() if s.broken is None
        }

    async def _ensure_kernel(self) -> SubprocessKernel:
        sources = self._projection().sources()
        skills = await self._mountable_skills()
        signature = (
            tuple(sorted(sources.items())),
            self._skills.signature() if self._skills is not None else (),
        )
        if self._kernel is None:
            self._kernel = SubprocessKernel(self._config)
            await self._kernel.start()
            await self._kernel.set_vfs(sources, skills)
            self._vfs_signature = signature
        elif signature != self._vfs_signature:
            await self._kernel.set_vfs(sources, skills)
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
            verdict, _message, _updated_input = await loop._await_permission(
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

        called_tools: list[str] = []
        # Image results are host-side only: the kernel gets the "[image: …]"
        # caption as text, and the real blocks ride out on this cell's outcome
        # so the model can actually see them (JSON-path parity).
        collected_images: list[Any] = []

        async def _host_call(name: str, kwargs: dict) -> HostReply:
            called_tools.append(name)
            tool_use_id = new_id("call")
            await loop._emit(ToolUse(
                session_id=sid, turn_id=turn_id,
                tool_use_id=tool_use_id, name=name, input=kwargs,
            ))
            # Correlate host-side work with THIS inner call, not the outer
            # run_python block: _dispatch_tool stamped run_python's id on the
            # ctx, and tools that spawn UI-joined work (Task) read it from
            # there. Restore the outer value afterwards.
            extra = getattr(getattr(loop, "ctx", None), "extra", None)
            stamped = isinstance(extra, dict)
            had_previous = stamped and "tool_use_id" in extra
            previous = extra.get("tool_use_id") if had_previous else None
            if stamped:
                extra["tool_use_id"] = tool_use_id
            try:
                reply = await bridge.on_host_call(name, kwargs)
            finally:
                if stamped:
                    if had_previous:
                        extra["tool_use_id"] = previous
                    else:
                        extra.pop("tool_use_id", None)
            collected_images.extend(reply.images)
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
        self._record_cell(code, result, called_tools)

        parts: list[str] = []
        if result.stdout.strip():
            parts.append(result.stdout.rstrip("\n"))
        if result.value_repr:
            parts.append(f"=> {result.value_repr}")
        text = "\n".join(parts) if parts else "(no output)"
        if result.error:
            text = (text + "\n" if parts else "") + f"Error: {result.error}"
        dropped = len(collected_images) - _CELL_IMAGE_CAP
        if dropped > 0:
            text += f"\n({dropped} further images omitted — read fewer per cell)"
        return ToolOutcome(
            text=text,
            is_error=bool(result.error),
            images=tuple(collected_images[:_CELL_IMAGE_CAP]),
        )

    # ── continual harness hooks ──
    def _record_cell(self, code: str, result: Any, called_tools: list[str]) -> None:
        """Journal the cell, bump skill usage, kick synthesis. Never raises."""
        if self._skills is None:
            return
        try:
            error_lines = (result.error or "").strip().splitlines()
            self._skills.journal_append({
                "kind": "cell",
                "code": code,
                "stdout": result.stdout[:_JOURNAL_STDOUT_CAP],
                "value": result.value_repr[:_JOURNAL_VALUE_CAP],
                "error": error_lines[-1] if error_lines else None,
                "host_calls": result.host_calls,
                "tools": sorted(set(called_tools)),
            })
            self._skills.note_uses(referenced_skills(code))
        except Exception:
            pass
        if self._synthesizer is not None:
            try:
                self._synthesizer.kick()
            except Exception:
                pass

    def record_event(self, kind: str, text: str) -> None:
        """Journal a conversation event — the episodic half of the journal.

        ``kind`` is ``"user"`` (the prompt that started a turn) or
        ``"assistant"`` (the turn's final text). Synthesis reads these for the
        intent and feedback surrounding the executed cells: *why* a workflow
        ran and whether the user accepted it. No-op without a store; never
        raises (called from the engine's turn path).
        """
        if self._skills is None or not (text or "").strip():
            return
        try:
            self._skills.journal_append({"kind": kind, "text": text[:_JOURNAL_EVENT_CAP]})
        except Exception:
            pass
        # A finished turn is a natural synthesis boundary; user events just
        # accumulate until then.
        if kind == "assistant" and self._synthesizer is not None:
            try:
                self._synthesizer.kick()
            except Exception:
                pass

    async def aclose(self) -> None:
        if self._synthesizer is not None:
            try:
                await self._synthesizer.drain()
            except Exception:
                pass
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

    # Continual-harness persistence tools — registered only with a SkillStore.
    # Source is SKILLS_SOURCE (not codemode) so they are *projected into the
    # kernel* rather than into the prompt schema: the model reaches them via
    # `from tools import save_skill`, and every save/delete rides
    # the same host permission gate as any write-risk tool.
    store = session.skills
    if store is None:
        return

    async def _save_skill(input: dict, _ctx: ToolCtx) -> ToolOutcome:
        name = input.get("name") or ""
        source = input.get("source") or ""
        error = validate_skill(name, source)
        if error is not None:
            return ToolOutcome(text=error, is_error=True)
        # Test-on-save: import + optional _selftest() in a throwaway kernel.
        # A skill that can't prove itself never lands in the store.
        try:
            verdicts = await verify_sources(
                {**store.sources(), name: source}, [name], config=session._config
            )
            verdict = verdicts.get(name)
        except Exception:
            verdict = None  # verification infrastructure failure ≠ bad skill
        if verdict is not None:
            return ToolOutcome(
                text=f"skill {name!r} rejected — verification failed: {verdict}",
                is_error=True,
            )
        skill = store.save(name, source)
        store.mark_verified(name)
        return ToolOutcome(
            text=(
                f"skill {skill.name!r} saved and verified — import with "
                f"`from skills import {skill.name}` (next run_python "
                f"call onward, and in every future session): {skill.description}"
            )
        )

    async def _delete_skill(input: dict, _ctx: ToolCtx) -> ToolOutcome:
        name = input.get("name") or ""
        if store.delete(name):
            return ToolOutcome(text=f"skill {name!r} deleted")
        return ToolOutcome(text=f"no such skill: {name!r}", is_error=True)

    registry.register_builtin(
        ToolSpec(
            name="save_skill",
            description=(
                "Persist a reusable Python skill module across sessions. It "
                "is imported with `from skills import <name>` in the run_python kernel. Module "
                "docstring required (first line = one-sentence description); "
                "define a function named like the skill. Include a pure "
                "_selftest() (inline fixture data, no tool calls) — it runs "
                "before the skill is accepted, and again whenever the source "
                "changes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "snake_case identifier"},
                    "source": {"type": "string", "description": "full module source"},
                },
                "required": ["name", "source"],
            },
            risk="write",
            source=SKILLS_SOURCE,
        ),
        _save_skill,
    )
    registry.register_builtin(
        ToolSpec(
            name="delete_skill",
            description="Delete a persistent skill by name.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            risk="write",
            source=SKILLS_SOURCE,
        ),
        _delete_skill,
    )
