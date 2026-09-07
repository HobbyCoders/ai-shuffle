"""HookBus — lifecycle interception points (Claude Code hooks model).

Events: ``PreToolUse``, ``PostToolUse``, ``TurnStart`` (Claude Code's
``UserPromptSubmit``), ``TurnEnd`` (``Stop``), ``SessionStart``,
``SessionEnd``, ``PreCompact``, ``SubagentStop`` and ``Notification``. A hook
returns a ``HookDecision``:
- ``allow``  — proceed (the default when a hook returns None);
- ``deny``   — short-circuit. PreToolUse: an error ToolResult kept in context
               (never a silent drop). TurnStart: the turn is aborted before any
               model call. TurnEnd: the model is asked to keep going with the
               reason. PostToolUse: the reason is appended to the tool result.
- ``modify`` — replace the tool input before dispatch.

Hooks run in registration order. A ``deny`` short-circuits immediately; a
``modify`` threads its ``replacement_input`` into the payload for later hooks
and is what the engine ultimately dispatches. ``stop`` (Claude Code's
``continue: false``) also short-circuits and ends the turn. A callback that
raises is logged and reported as a warning; it never ends the turn.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

log = logging.getLogger(__name__)

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
TURN_START = "TurnStart"
TURN_END = "TurnEnd"
SESSION_START = "SessionStart"
SESSION_END = "SessionEnd"
PRE_COMPACT = "PreCompact"
SUBAGENT_STOP = "SubagentStop"
NOTIFICATION = "Notification"


@dataclass(frozen=True, kw_only=True)
class HookDecision:
    action: str = "allow"  # "allow" | "deny" | "modify"
    replacement_input: dict[str, Any] | None = None
    # The reason behind a deny, a permission ``ask``, or a stop.
    note: str | None = None
    # PreToolUse only: ``"allow"`` skips the permission prompt (Claude Code's
    # ``permissionDecision: allow`` / legacy ``decision: approve``); ``"ask"``
    # forces one, with ``note`` as the reason. None leaves the permission
    # engine to decide.
    permission_decision: str | None = None
    # Text the engine injects into the model's context: appended to the user
    # message on TurnStart/SessionStart, to the tool result on PostToolUse.
    additional_context: str | None = None
    # Text shown to the user (Claude Code's ``systemMessage``).
    system_message: str | None = None
    # Claude Code's ``continue: false``: end the turn once the hooks have run.
    stop: bool = False
    # Non-fatal diagnostics a hook wants surfaced (a command hook that timed
    # out or exited with an unexpected code). ``emit`` accumulates them across
    # every hook that ran, so the engine can report them without changing the
    # decision.
    warnings: tuple[str, ...] = ()


HookCallback = Callable[[str, dict[str, Any]], Awaitable["HookDecision | None"]]


def hook_reminder(label: str, text: str) -> str:
    """Hook-supplied text as a system-reminder block for the model.

    The text is defanged first, so a hook's output cannot close the block
    early or forge a steering marker. Shared by every dispatch path that
    appends PostToolUse feedback to a result: the native loop, the code-mode
    bridge and the delegated leg's bundled bridge.
    """
    from fluid.tools.hardening import defang_harness_markers

    return (
        "<system-reminder>\n"
        f"{label}:\n{defang_harness_markers(text).strip()}\n"
        "</system-reminder>"
    )


class HookBus:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = {}

    def on(self, event: str, callback: HookCallback) -> None:
        self._hooks.setdefault(event, []).append(callback)

    async def emit(self, event: str, payload: dict[str, Any]) -> HookDecision:
        """Run every hook for ``event``; deny and stop short-circuit, the rest
        accumulates.

        Across hooks: ``modify`` chains through the payload; context and
        system messages concatenate; a permission ``ask`` outranks an
        ``allow``; the first ``stop`` or ``deny`` wins and later hooks do not
        run. A callback that raises becomes a warning and is skipped.
        """
        current = dict(payload)
        final = HookDecision(action="allow")
        warnings: list[str] = []
        contexts: list[str] = []
        messages: list[str] = []
        permission: str | None = None
        ask_note: str | None = None
        for callback in self._hooks.get(event, []):
            try:
                decision = await callback(event, current)
            except Exception as exc:  # noqa: BLE001 — a hook never ends the turn
                label = getattr(callback, "label", None) or repr(callback)
                log.warning("%s hook %s raised", event, label, exc_info=True)
                warnings.append(f"{label} raised {exc!r}; continuing")
                continue
            if decision is None:
                continue
            warnings.extend(decision.warnings)
            if decision.system_message:
                messages.append(decision.system_message)
            if decision.action == "deny" or decision.stop:
                return replace(
                    decision,
                    warnings=tuple(warnings),
                    additional_context="\n\n".join(
                        c for c in [*contexts, decision.additional_context] if c
                    ) or None,
                    system_message="\n".join(messages) or None,
                )
            if decision.additional_context:
                contexts.append(decision.additional_context)
            if decision.permission_decision == "ask":
                permission = "ask"
                ask_note = decision.note or ask_note
            elif decision.permission_decision == "allow" and permission is None:
                permission = "allow"
            if decision.action == "modify":
                if decision.replacement_input is not None:
                    current["input"] = decision.replacement_input
                final = HookDecision(
                    action="modify",
                    replacement_input=current.get("input"),
                    note=decision.note,
                )
        return replace(
            final,
            warnings=tuple(warnings),
            permission_decision=permission,
            note=ask_note if permission == "ask" else final.note,
            additional_context="\n\n".join(contexts) or None,
            system_message="\n".join(messages) or None,
        )
