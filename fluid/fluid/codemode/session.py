"""CodeModeSession — the code-mode orchestrator wired into ``NativeLoop``.

Owns the persistent kernel for a session, projects the registry's work-tools
into the kernel VFS, and implements the four code-mode tools:

- ``run_python``    — execute code in the persistent kernel (engine-driven);
- ``list_tools``    — names + one-line summaries of callable tools;
- ``search_tools``  — keyword search over the same catalog;
- ``read_tool_docs``— the generated typed stub (signature + docstring) for a tool.

When the host restricts the session's tool surface it sets ``tool_filter``
(ruling T9); every one of those four tools, the projected VFS, and the host-call
gate then behave as though a filtered tool does not exist.

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
The session wraps that bridge with the engine's two round-trips back to the
human — ``PermissionRequest`` (``on_permission``) and the input-required/MRTR
``QuestionRequest`` (``on_input_required``, which is what lets an in-kernel
``AskUser`` actually reaches the user) — and emits
``ToolUse``/``ToolResult`` events so in-kernel calls are as observable as JSON
calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fluid.codemode.bridge import HostBridge
from fluid.codemode.journal import FrictionDetector, append_friction, strip_reminders
from fluid.codemode.kernel import HostReply
from fluid.codemode.projection import Projection, func_name
from fluid.codemode.skills import SkillStore, referenced_skills, validate_skill
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from fluid.codemode.verify import verify_sources, verify_store
from fluid.events.model import HookEvent, ToolResult, ToolUse
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import new_id
from fluid.tools.hardening import fence_shell_output
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

CODE_MODE_SOURCE = "codemode"
SKILLS_SOURCE = "skills"

_RUN_PYTHON_DESC = (
    "Execute Python in a persistent per-session kernel: names you bind stay "
    "bound from one cell to the next. Persistence is not a guarantee across "
    "TURNS — the kernel can be recycled while a turn is parked, and when that "
    "happens the next cell's result opens with a note saying so; a NameError "
    "for a tool you imported earlier means exactly that, so re-import and "
    "carry on rather than treating it as a mistake. "
    "Discover tools with the list_tools/search_tools/read_tool_docs "
    "tools — they are separate top-level calls, not functions inside the "
    "kernel; from inside a cell, `dir(tools)` lists what is mounted. Then "
    "`from tools import Read, Edit` and call them as plain synchronous "
    "functions (`import tools; tools.Read(...)` also works) — each call is "
    "permission-gated on the host. Catch only MissingPathError around "
    "discovery (a Read whose path may not exist); other errors now return as "
    "results and do not abort the cell: Grep/Glob on a missing path return "
    "'(path missing …)', an Edit whose old_string does not match returns "
    "'Edit NOT applied …', and an MCP tool error (a stale browser ref) "
    "returns its error text — read each result before depending on it. "
    "Permission denials, cancellation and tool timeouts still abort the "
    "cell; do not wrap calls in try/except to get past those. "
    "Saved skills (reusable helpers that persist "
    "across sessions) import via `from skills import <name>`; persist a new one "
    "with the save_skill tool when you notice a reusable pattern. The kernel "
    "refuses sockets, direct file and directory access, subprocesses and "
    "native code (an interpreter audit hook, not just patched builtins) — use "
    "the projected tools instead; only a stdlib-safe "
    "import allowlist is available — json, re, math, itertools, collections "
    "and textwrap are already bound in the namespace, along with pathlib.Path. "
    "The last expression's repr is returned (REPL-style) and stays bound to "
    "`_` in the next cell; large tool results come back as a ResultRef you can "
    "slice in a later call. Every ResultRef is also filed in `_refs` under the "
    "id its repr shows, so a handle you forgot to assign is recovered with "
    "`_refs[7]` — never re-run a tool to get a result back.\n"
    "\n"
    "One cell per step, not per call: compose the several tool calls a step "
    "needs into a single cell — loop Read over a file list, search then read "
    "then decide, apply an Edit and rerun the check in the same cell. A cell "
    "that makes a single tool call is usually a wasted round-trip.\n"
    "\n"
    "The cell must be valid Python, so:\n"
    "- Quote every argument. `Grep(pattern=r\"def foo|bar\", glob=\"*.py\")`, "
    "never `pattern=def foo|bar`. Raw strings for regexes and Windows paths.\n"
    "- Never nest a shell heredoc or a long multi-line body (a commit message, "
    "a PR description, a markdown report) inside a command string. Put the "
    "text in its own triple-quoted variable, or Write it to a file and point "
    "the command at that file (`git commit -F <file>`, `gh pr create "
    "--body-file <file>`) — writing that file OUTSIDE the repo working tree, "
    "since `git add -A` will otherwise commit it.\n"
    "- Tools return text, not dicts; parse with json.loads when a tool emits "
    "JSON."
)


def _run_python_desc(stdout_cap: int = KernelConfig.stdout_cap) -> str:
    """run_python's description plus this session's real stdout budget.

    The cap used to be undocumented, and it is the single largest silent loss
    in the code surface: transcripts show cells printing 27k chars, losing 11k
    of them, and the model paying to re-run the tools because nothing had told
    it a budget existed or that the full results were still in the kernel.
    """
    return (
        f"{_RUN_PYTHON_DESC}\n"
        "\n"
        f"A cell's stdout is capped at {stdout_cap:,} characters, fair-shared "
        "across its prints, and what is cut is named in a closing notice. Print "
        "what you need to decide, not whole payloads: a tool result too large "
        "to print is already in the kernel as a ResultRef, so slice it (or "
        "recover it from `_refs[N]`) rather than re-running the tool."
    )

class _HumanWait:
    """A stopwatch for the parts of a cell that are waiting on a person.

    The kernel bounds a single host call, and a permission prompt or an
    in-kernel AskUser is a host call that legitimately takes as long as the
    user takes. Entering the context marks the wait; ``seconds`` reports the
    total INCLUDING a wait still in flight, so the kernel can extend its
    deadline while the prompt is still on screen rather than only afterwards.
    """

    def __init__(self) -> None:
        self._total = 0.0
        self._since: float | None = None
        self._depth = 0

    @staticmethod
    def _now() -> float:
        import asyncio

        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover - always called inside a loop
            import time

            return time.monotonic()

    def __enter__(self) -> _HumanWait:
        if self._depth == 0:
            self._since = self._now()
        self._depth += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            if self._since is not None:
                self._total += self._now() - self._since
            self._since = None

    def seconds(self) -> float:
        if self._since is None:
            return self._total
        return self._total + (self._now() - self._since)


# Journal previews are bounded so journal.jsonl (synthesis input) stays small.
_JOURNAL_STDOUT_CAP = 1500
_JOURNAL_VALUE_CAP = 300
_JOURNAL_EVENT_CAP = 2000  # user/assistant conversation events
# Cell source was the one uncapped field: a few hundred cells carrying
# multi-kilobyte scripts filled the 1 MiB journal on their own. The
# synthesizer reads code for its shape, not its bulk.
_JOURNAL_CODE_CAP = 2000


def _cap_code(code: str) -> str:
    """Journal form of a cell's source: whole when short, else the head plus
    a marker naming how much was dropped."""
    if len(code) <= _JOURNAL_CODE_CAP:
        return code
    dropped = len(code) - _JOURNAL_CODE_CAP
    return code[:_JOURNAL_CODE_CAP] + f"\n# [... {dropped} more chars not journaled]"

# Images produced by in-kernel tool calls ride out on the cell's tool result.
# Capped so one loop over a directory of screenshots can't blow up the request.
_CELL_IMAGE_CAP = 20

# ── committed-call manifest (appended when a cell dies mid-way) ──
# The kwarg that names what a call acted on, most identifying first.
_TARGET_KEYS = (
    "file_path", "path", "notebook_path", "target_file", "command",
    "pattern", "url", "query", "name", "description", "prompt",
)
_TARGET_CAP = 60
_MUTATING_RISKS = ("write", "destructive")
# The manifest exists so the model doesn't re-apply edits it already made; past
# a certain length it stops being a manifest and starts being noise.
_MANIFEST_CALL_CAP = 30


def _call_target(kwargs: dict) -> str:
    """A short, recognisable label for what one in-kernel call acted on."""
    for key in _TARGET_KEYS:
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            if len(text) > _TARGET_CAP:
                text = text[:_TARGET_CAP] + "…"
            return text
    return ""


def _committed_manifest(calls: list[dict]) -> str:
    """Ordered record of the calls that ran before the cell blew up.

    A mid-cell ``ToolError`` aborts the whole cell, so the model-facing result
    is a traceback and nothing else — the Edits that already landed leave no
    trace in context, and the model's next move is to redo them. This names
    them instead: what ran, against what, and whether it succeeded.
    """
    if not calls:
        return ""
    shown = calls[:_MANIFEST_CALL_CAP]
    parts = []
    for i, call in enumerate(shown, 1):
        target = f"({call['target']})" if call["target"] else "()"
        # The leaf name is what the cell actually wrote — echoing the registry
        # name (``mcp__fs__Edit``) would read as a different call.
        name = func_name(call["name"])
        parts.append(f"{i}. {name}{target} {'ok' if call['ok'] else 'FAILED'}")
    line = f"calls completed before the error: {', '.join(parts)}"
    if len(calls) > len(shown):
        line += f", … and {len(calls) - len(shown)} more"
    if any(c["ok"] and c["mutates"] for c in calls):
        line += (
            " — completed Edits/Writes are already applied to disk; "
            "do not re-issue them."
        )
    return line


class CodeModeSession:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        config: KernelConfig | None = None,
        exec_timeout: float = 30.0,
        skills: SkillStore | None = None,
        synthesizer: Any | None = None,  # fluid.codemode.continual.Synthesizer
        tool_filter: Callable[[str], bool] | None = None,
        friction: Any | None = None,  # fluid.codemode.journal.FrictionDetector
    ) -> None:
        self._registry = registry
        self._config = config or KernelConfig()
        self._exec_timeout = exec_timeout
        self._kernel: SubprocessKernel | None = None
        self._vfs_signature: tuple | None = None
        self._skills = skills
        self._synthesizer = synthesizer
        # Mechanical harness-friction detection over the turn's in-kernel calls
        # (the JSON surface has the same detector behind ToolJournal). Entries
        # are buffered per cell and journaled right after the cell they came
        # from, so the synthesis prompt reads "cell, then what it cost".
        self._friction = friction if friction is not None else FrictionDetector()
        self._friction_pending: list[dict] = []
        # The session's tool surface (ruling T9): a ``(name) -> bool`` predicate,
        # ``None`` for "everything". Settable after construction — the engine
        # wires its own predicate in when it registers the code-mode tools. The
        # projection is rebuilt per call, so a later change takes effect at once
        # (and the VFS signature shift re-mounts the kernel's ``tools`` package).
        self.tool_filter: Callable[[str], bool] | None = tool_filter
        # Tool names whose stub this session has already handed to the model.
        # Not cleared per turn: the point is exactly to notice a re-fetch many
        # turns later.
        self._docs_served: set[str] = set()
        self._verification_degraded: str | None = None
        self._verification_degraded_reported = False
    @property
    def skills(self) -> SkillStore | None:
        return self._skills

    @property
    def friction_count(self) -> int:
        """Friction entries recorded this turn (the engine's nudge trigger)."""
        return self._friction.count if self._skills is not None else 0

    def begin_turn(self) -> None:
        """Turn boundary: reset friction budget and cross-call state."""
        try:
            self._friction.begin_turn()
        except Exception:
            pass
        self._friction_pending = []


    # ── projection (rebuilt cheaply; reflects late-loaded MCP tools) ──
    def _projection(self) -> Projection:
        specs = [
            s for s in self._registry.list_tools(loaded_only=False)
            if s.source != CODE_MODE_SOURCE
        ]
        # Deferral rides along: every projected tool is callable, but a host
        # that inlines per-tool documentation into the prompt must collapse the
        # deferred ones to name + one-liner and leave read_tool_docs to serve
        # the rest. Best-effort — a registry double without the accessor simply
        # projects everything as loaded.
        try:
            deferred = {s.name for s in self._registry.deferred_tools()}
        except Exception:  # noqa: BLE001
            deferred = set()
        return Projection(specs, tool_filter=self.tool_filter, deferred=deferred)

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
        # Audited sessions re-fetched the same four browser stubs an hour
        # apart with the first copy still in context, eight calls for four
        # signatures. The docs are returned anyway — a compaction may have
        # taken them — but the model is told it is probably already holding
        # them.
        if name in self._docs_served:
            return (
                f"(You already read {name}'s docs earlier in this session; "
                "unless a compaction has since dropped them, this stub is "
                "still in your context.)\n" + src
            )
        self._docs_served.add(name)
        return src

    # ── run_python (driven by the engine, not the registry) ──
    async def _mountable_skills(self) -> dict[str, str]:
        """Verified skill sources for the kernel mount.

        Stale skills (source changed since their last verdict) are re-verified
        in a throwaway kernel first, within the mount's verification budget;
        broken ones are excluded from the mount and flagged in
        ``list_tools``, and ones the budget did not reach wait for the next
        mount. Verification failure of the *pass* itself (e.g. kernel spawn
        issues) degrades to an empty skill mount. Ordinary projected tools
        remain available, and the session reports the degraded state.
        """
        if self._skills is None:
            return {}
        try:
            await verify_store(self._skills, config=self._config)
        except Exception as exc:
            self._verification_degraded = (
                "skill verification unavailable; saved skills were withheld "
                f"from this code-mode kernel ({type(exc).__name__})"
            )
            return {}
        self._verification_degraded = None
        self._verification_degraded_reported = False
        return {
            s.name: s.source for s in self._skills.list()
            if s.broken is None and s.source
            and not self._skills.needs_verification(s.name)
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
        if self._verification_degraded and not self._verification_degraded_reported:
            await loop._emit(HookEvent(
                session_id=sid,
                turn_id=turn_id,
                level="warning",
                message=self._verification_degraded,
                hook_event="skill_verification",
            ))
            self._verification_degraded_reported = True

        # Every second this cell spends waiting on a HUMAN. The kernel credits
        # it back against the per-host-call ceiling, so a permission prompt or
        # an in-kernel AskUser that a user answers in their own time is never
        # cancelled as a hung tool — only a tool that is genuinely not
        # answering is.
        human_wait = _HumanWait()

        # Bridge: the JSON-path gate for in-kernel tool calls. Its ``ask`` verdict
        # is surfaced as an engine PermissionRequest (parking the turn until the
        # client answers) via on_permission.
        async def _on_permission(
            spec: ToolSpec, tool_input: dict
        ) -> tuple[str, str | None, dict | None]:
            with human_wait:
                verdict, message, updated_input = await loop._await_permission(
                    sid, turn_id, spec.name, new_id("call"), tool_input, spec.risk
                )
            return verdict, message, updated_input

        # MRTR round-trip: an in-kernel AskUser (or an elicitating
        # MCP tool) pauses on ``input_required``, and the engine turns that into
        # QuestionRequest events, parks for RespondQuestion, and hands the
        # answers back for the retry leg — the same path the JSON surface uses.
        # An embedder whose loop has no such round-trip leaves this unwired, and
        # the bridge then refuses the call loudly instead of answering "".
        collect = getattr(loop, "_collect_input_responses", None)
        on_input_required = None
        if collect is not None:
            async def on_input_required(
                name: str, tool_use_id: str | None, marker: Any
            ) -> dict | None:
                with human_wait:
                    return await collect(sid, turn_id, name, tool_use_id, marker)

        # No engine ⇒ allow-all, matching the JSON path (which skips gating when
        # ``permissions is None``).
        permissions = loop.permissions or PermissionEngine("never_ask")

        # A hook's ``continue: false`` on an in-kernel call ends the turn the
        # way it does on the JSON path: the loop reads the reason once the
        # cell's result is recorded. Loops without that seam get no stop.
        on_stop = None
        if hasattr(loop, "_hook_stop_reason"):
            def on_stop(reason: str) -> None:
                loop._hook_stop_reason = reason

        bridge = HostBridge(
            self._registry, permissions,
            hooks=loop.hooks, on_permission=_on_permission,
            on_input_required=on_input_required, ctx=loop.ctx,
            tool_filter=self.tool_filter,
            on_stop=on_stop,
            # What this surface actually delivers: a result over the ref
            # threshold reaches the cell as a ResultRef stub, not as text the
            # model reads (see ToolCtx.result_budget).
            result_budget=self._config.ref_threshold,
        )

        # Ordered per-call records: name, what it acted on, whether it landed,
        # and whether it mutated anything. Feeds the committed-call manifest on
        # a mid-cell failure and the journal's ordered call list.
        called_tools: list[dict] = []
        # Image results are host-side only: the kernel gets the "[image: …]"
        # caption as text, and the real blocks ride out on this cell's outcome
        # so the model can actually see them (JSON-path parity).
        collected_images: list[Any] = []

        async def _host_call(name: str, kwargs: dict) -> HostReply:
            spec = self._registry.get(name)
            record = {
                "name": name,
                "target": _call_target(kwargs),
                "ok": False,
                "mutates": bool(
                    spec is not None
                    and spec.effective_risk(kwargs) in _MUTATING_RISKS
                ),
            }
            called_tools.append(record)
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
                # Tools whose output talks about how to call things next need
                # to know they were reached from inside the kernel (ToolSearch
                # otherwise prints wire names the kernel cannot import).
                extra["in_kernel_call"] = True
            try:
                reply = await bridge.on_host_call(
                    name, kwargs, tool_use_id=tool_use_id
                )
            except asyncio.CancelledError:
                # The kernel's per-call ceiling (or the turn's watchdog)
                # cancelled this call. The ToolUse above already went out;
                # without a matching ToolResult the transcript shows a call
                # that never returned and the audit reads it as a dead turn.
                try:
                    await loop._emit(ToolResult(
                        session_id=sid, turn_id=turn_id, tool_use_id=tool_use_id,
                        name=name, is_error=True,
                        output=(
                            f"{name} was cancelled before it returned (per-call "
                            "ceiling or turn watchdog); the cell stops here."
                        ),
                    ))
                except Exception:  # noqa: BLE001 — the cancel must still land
                    pass
                raise
            finally:
                if stamped:
                    extra.pop("in_kernel_call", None)
                    if had_previous:
                        extra["tool_use_id"] = previous
                    else:
                        extra.pop("tool_use_id", None)
            collected_images.extend(reply.images)
            record["ok"] = reply.result is not None
            record["shell_exit_code"] = reply.shell_exit_code
            if reply.result is not None:
                out, is_error = reply.result, False
            elif reply.denied:
                out, is_error = reply.message or "permission denied", True
            else:
                out, is_error = reply.error or "tool error", True
            if self._skills is not None:
                # Best-effort friction detection over this in-kernel call; the
                # entries ride out with the cell's journal record.
                try:
                    self._friction_pending.extend(
                        self._friction.observe(name, kwargs, out, is_error)
                    )
                except Exception:
                    pass
                # Facts a tool result establishes on its own (the browser
                # host) go straight to the memory store, tool-provenance,
                # so the next session does not rediscover them.
                try:
                    from fluid.context.runtime_facts import (
                        harvest_tool_facts,
                        write_harvested_facts,
                    )

                    harvested = harvest_tool_facts(name, out)
                    if harvested:
                        write_harvested_facts(
                            self._skills, (extra or {}).get("memory_project"), harvested,
                        )
                except Exception:
                    pass
            await loop._emit(ToolResult(
                session_id=sid, turn_id=turn_id, tool_use_id=tool_use_id,
                name=name, output=out, is_error=is_error,
                images=[{"media_type": image.media_type, "data": image.data} for image in reply.images],
            ))
            return reply

        result = await kernel.exec(
            code, timeout=timeout, on_host_call=_host_call,
            host_wait_credit=human_wait.seconds,
        )
        self._record_cell(code, result, called_tools)

        parts: list[str] = []
        if result.stdout.strip():
            parts.append(result.stdout.rstrip("\n"))
        if result.value_repr:
            parts.append(f"=> {result.value_repr}")
        text = "\n".join(parts) if parts else "(no output)"
        if result.error:
            text = (text + "\n" if parts else "") + f"Error: {result.error}"
        # Every cell can print sliced or persisted shell data, even without a
        # host call this time. Frame at the model boundary, not in Python strings.
        text = fence_shell_output(text, "code-mode output (may include shell stdout/stderr)")
        # Lifecycle/status are host-authored, separate from the cell's data.
        for call in called_tools:
            if call.get("shell_exit_code") is not None:
                text += f"\nShell call {call['name']}: [exit code {call['shell_exit_code']}]"
        if result.notes:
            text = f"{result.notes}\n{text}"
        if result.error:
            # A cell aborts at the first ToolError, so the mutations that
            # already applied are invisible in the traceback. Name them.
            manifest = _committed_manifest(called_tools)
            if manifest:
                text += "\n" + manifest
        dropped = len(collected_images) - _CELL_IMAGE_CAP
        if dropped > 0:
            text += f"\n({dropped} further images omitted — read fewer per cell)"
        return ToolOutcome(
            text=text,
            is_error=bool(result.error),
            images=tuple(collected_images[:_CELL_IMAGE_CAP]),
        )

    # ── continual harness hooks ──
    def _record_cell(self, code: str, result: Any, called_tools: list[dict]) -> None:
        """Journal the cell, bump skill usage, kick synthesis. Never raises."""
        if self._skills is None:
            self._friction_pending = []
            return
        try:
            error_lines = (result.error or "").strip().splitlines()
            self._skills.journal_append({
                "kind": "cell",
                "code": _cap_code(code),
                "stdout": result.stdout[:_JOURNAL_STDOUT_CAP],
                "value": result.value_repr[:_JOURNAL_VALUE_CAP],
                "error": error_lines[-1] if error_lines else None,
                "host_calls": result.host_calls,
                "tools": sorted({c["name"] for c in called_tools}),
                # Compatible extension: the same calls in the order they ran,
                # with their outcome — a workflow is a sequence, and the
                # unordered ``tools`` set cannot express one.
                "calls": [
                    {"name": c["name"], "ok": c["ok"]} for c in called_tools
                ],
            })
            self._skills.note_uses(referenced_skills(code))
        except Exception:
            pass
        pending, self._friction_pending = self._friction_pending, []
        append_friction(self._skills, self._synthesizer, pending)
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
        if kind == "user":
            # The prompt carries the app's memory snapshot and the engine's
            # reminders; journaling them made learner evidence an echo of
            # facts that are already saved.
            text = strip_reminders(text)
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

def skill_store_tool_specs() -> dict[str, ToolSpec]:
    """Persistence tool specs, available before a code-mode session is registered."""
    return {
        "save_skill": ToolSpec(
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
        "delete_skill": ToolSpec(
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
    }


def register_code_mode_tools(
    registry: ToolRegistry,
    session: CodeModeSession,
    *,
    tool_filter: Callable[[str], bool] | None = None,
) -> None:
    """Register the four code-mode tools as the model's entire schema surface.

    ``tool_filter`` is the host's tool-surface predicate (ruling T9). When given
    it is stamped onto the session, so discovery (``list_tools``,
    ``search_tools``, ``read_tool_docs``), the projected kernel VFS, and the
    host-call gate all describe and accept exactly the tools this session may
    call. Passing ``None`` leaves the session's existing filter in place.
    """
    if tool_filter is not None:
        session.tool_filter = tool_filter

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
            name="run_python",
            description=_run_python_desc(session._config.stdout_cap),
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

    specs = skill_store_tool_specs()
    registry.register_builtin(specs["save_skill"], _save_skill)
    registry.register_builtin(specs["delete_skill"], _delete_skill)
