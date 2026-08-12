"""Bridges — every in-kernel tool call, run through the real host gate.

The kernel is untrusted (prompt injection == code execution), so the host-call
handler is the one place the host reasserts control. Two bridges implement the
same idea at different integration depths:

- ``HostCallBridge`` wires each ``__host_call__`` into a live ``NativeLoop``, so
  in-kernel calls reuse the loop's exact gated path (``_gated_call``) and surface
  as real ``ToolUse`` / ``ToolResult`` / ``PermissionRequest`` events. This is
  what ``CodeModeSession`` uses in production.
- ``HostBridge`` is a self-contained gate (registry + PermissionEngine + optional
  hooks) with an injected ``on_permission`` callback — no engine required. Handy
  for embedding code-mode outside the native loop and for focused tests.

Both enforce the same two hard refusals before dispatch: a tool with
``code_mode_allowed=False`` (the auditable high-risk escape hatch — the model is
told to use the JSON tool path) and a denied permission/hook. Refusals come back
as ``HostReply(denied=…)`` / ``HostReply(error=…)`` so the kernel *raises* in the
model's code — the error stays in context rather than being silently dropped.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loom.codemode.kernel import HostReply
from loom.events.model import ToolUse
from loom.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, HookBus
from loom.permissions.engine import PermissionEngine
from loom.store.base import new_id
from loom.tools.registry import ToolCtx, ToolOutcome, ToolRegistry


class HostCallBridge:
    """Routes one code-mode kernel's host calls through a live ``NativeLoop``.

    Constructed per ``run_python`` invocation with the loop plus the current
    ``session_id`` / ``turn_id``. ``handle`` is the ``on_host_call`` coroutine
    passed to ``Kernel.exec``: for each in-kernel tool call it emits a ``ToolUse``,
    reuses the loop's ``_gated_call`` (PreToolUse hook → PermissionEngine, incl.
    the ``ask`` → ``PermissionRequest`` round-trip and doom-loop guard → dispatch
    → PostToolUse → ``ToolResult``), and maps the outcome back to a ``HostReply``.
    The ``code_mode_allowed=False`` escape hatch is enforced here, before the
    gate, and reported so the model is steered to the JSON tool path.
    """

    def __init__(self, *, loop: Any, sid: str, turn_id: str) -> None:
        self._loop = loop
        self._sid = sid
        self._turn_id = turn_id

    async def handle(self, name: str, kwargs: dict[str, Any]) -> HostReply:
        loop = self._loop
        tool_use_id = new_id("codetool")
        await loop._emit(
            ToolUse(
                session_id=self._sid, turn_id=self._turn_id,
                tool_use_id=tool_use_id, name=name, input=kwargs,
            )
        )

        registry = loop.registry
        spec = registry.get(name) if registry is not None else None
        if spec is None:
            outcome = ToolOutcome(text=f"unknown tool: {name}", is_error=True)
            await loop._finish_tool(self._sid, self._turn_id, name, tool_use_id, outcome)
            return HostReply(error=outcome.text)

        # Auditable escape hatch: high-risk tools opt out of code-mode entirely.
        if not spec.code_mode_allowed:
            message = (
                f"{name} is JSON-path only — not callable from code-mode; "
                "use the JSON tool interface (auditable high-risk path)."
            )
            await loop._finish_tool(
                self._sid, self._turn_id, name, tool_use_id,
                ToolOutcome(text=message, is_error=True),
            )
            return HostReply(denied=True, message=message)

        # Reuse the loop's full gated path — same hook/permission/dispatch/events
        # the JSON tool surface uses, including the PermissionRequest round-trip.
        outcome = await loop._gated_call(
            self._sid, self._turn_id, name, tool_use_id, dict(kwargs)
        )
        if outcome.is_error:
            if outcome.text == "permission denied":
                return HostReply(denied=True, message=outcome.text)
            return HostReply(error=outcome.text)
        return HostReply(result=outcome.text)

# Injected permission round-trip: the engine says "ask", the bridge calls this
# to surface a PermissionRequest and await the user's answer. In production the
# engine loop backs it with a PermissionRequest/RespondPermission pair; tests
# inject a fake that returns a bool.
OnPermission = Callable[[Any, dict[str, Any]], Awaitable[bool]]


class HostBridge:
    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        *,
        hooks: HookBus | None = None,
        on_permission: OnPermission | None = None,
        ctx: ToolCtx | None = None,
        doom_loop: bool = True,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._hooks = hooks
        self._on_permission = on_permission
        self._ctx = ctx or ToolCtx()
        self._doom_loop = doom_loop

    async def on_host_call(self, tool_name: str, kwargs: dict[str, Any]) -> HostReply:
        spec = self._registry.get(tool_name)
        if spec is None:
            return HostReply(error=f"unknown tool: {tool_name}")

        # Auditable escape hatch: high-risk tools opt out of code-mode entirely.
        if not spec.code_mode_allowed:
            return HostReply(
                denied=True,
                message=(
                    f"{tool_name} is not callable from code-mode; invoke it via "
                    "the JSON tool interface (auditable high-risk path)."
                ),
            )

        tool_input: dict[str, Any] = dict(kwargs)

        # PreToolUse hook (deny short-circuits; modify rewrites the input).
        if self._hooks is not None:
            decision = await self._hooks.emit(
                PRE_TOOL_USE, {"name": tool_name, "input": tool_input}
            )
            if decision.action == "deny":
                return HostReply(denied=True, message=f"blocked by hook: {decision.note}")
            if decision.action == "modify" and decision.replacement_input is not None:
                tool_input = decision.replacement_input

        # PermissionEngine: allow / deny / ask, plus the doom-loop guard.
        verdict = self._permissions.evaluate(spec, tool_input)
        if verdict != "deny" and self._doom_loop:
            if self._permissions.note_execution(tool_name, tool_input):
                verdict = "ask"
        if verdict == "deny":
            return HostReply(denied=True, message="permission denied")
        if verdict == "ask":
            if self._on_permission is None:
                return HostReply(
                    denied=True, message="permission required but no handler is wired"
                )
            if not await self._on_permission(spec, tool_input):
                return HostReply(denied=True, message="permission denied")

        outcome = await self._registry.dispatch(tool_name, tool_input, self._ctx)

        if self._hooks is not None:
            await self._hooks.emit(
                POST_TOOL_USE,
                {
                    "name": tool_name,
                    "input": tool_input,
                    "result": outcome.text,
                    "is_error": outcome.is_error,
                },
            )

        if outcome.is_error:
            return HostReply(error=outcome.text)
        return HostReply(result=outcome.text)
