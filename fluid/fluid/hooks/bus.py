"""HookBus — lifecycle interception points (Claude Code hooks model).

Events: ``PreToolUse``, ``PostToolUse``, ``TurnStart``, ``TurnEnd``. A hook
returns a ``HookDecision``:
- ``allow``  — proceed (the default when a hook returns None);
- ``deny``   — short-circuit; for PreToolUse the engine turns this into an error
               ToolResult kept in context (never a silent drop);
- ``modify`` — replace the tool input before dispatch.

Hooks run in registration order. A ``deny`` short-circuits immediately; a
``modify`` threads its ``replacement_input`` into the payload for later hooks
and is what the engine ultimately dispatches.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
TURN_START = "TurnStart"
TURN_END = "TurnEnd"


@dataclass(frozen=True, kw_only=True)
class HookDecision:
    action: str = "allow"  # "allow" | "deny" | "modify"
    replacement_input: dict[str, Any] | None = None
    note: str | None = None
    # Non-fatal diagnostics a hook wants surfaced (a command hook that timed
    # out or exited with an unexpected code). ``emit`` accumulates them across
    # every hook that ran, so the engine can report them without changing the
    # decision.
    warnings: tuple[str, ...] = ()


HookCallback = Callable[[str, dict[str, Any]], Awaitable["HookDecision | None"]]


class HookBus:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = {}

    def on(self, event: str, callback: HookCallback) -> None:
        self._hooks.setdefault(event, []).append(callback)

    async def emit(self, event: str, payload: dict[str, Any]) -> HookDecision:
        """Run every hook for ``event``; deny short-circuits, modify accumulates."""
        current = dict(payload)
        final = HookDecision(action="allow")
        warnings: list[str] = []
        for callback in self._hooks.get(event, []):
            decision = await callback(event, current)
            if decision is None:
                continue
            warnings.extend(decision.warnings)
            if decision.action == "deny":
                return replace(decision, warnings=tuple(warnings))
            if decision.action == "modify":
                if decision.replacement_input is not None:
                    current["input"] = decision.replacement_input
                final = HookDecision(
                    action="modify",
                    replacement_input=current.get("input"),
                    note=decision.note,
                )
        return replace(final, warnings=tuple(warnings))
