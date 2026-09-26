"""Run the app's Agent SDK hooks on fluid's HookBus.

``hook_service.attach_hooks_to_options`` builds ``options.hooks``: Claude Code
event name -> ``HookMatcher`` list, each holding SDK callbacks
``(hook_input, tool_use_id, context) -> SyncHookJSONOutput``. They carry the
project's DB hooks (including blocking PreToolUse security guards), plugin
``hooks.json`` scripts and the Stop handler. fluid legs have no Agent SDK to
call them, so :func:`sdk_hooks_bus` registers each one on a HookBus: the bus
payload is turned into Claude Code's hook input, and the callback's output is
read the way fluid reads a command hook's JSON stdout
(:meth:`fluid.hooks.command.CommandHook.interpret`). A PreToolUse callback
that raises or times out answers with a deny (fail closed); on other events
the failure is a warning and the turn carries on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Mapping, Optional

from fluid.hooks.bus import (
    NOTIFICATION,
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_END,
    SESSION_START,
    SUBAGENT_STOP,
    TURN_END,
    TURN_START,
    HookBus,
    HookDecision,
)
from fluid.hooks.command import (
    DEFAULT_TIMEOUT,
    CommandHook,
    claude_hook_input,
    matcher_matches,
)

logger = logging.getLogger(__name__)

#: Claude Code hook event names against the HookBus events both fluid legs emit.
SDK_EVENT_TO_BUS: Dict[str, str] = {
    "PreToolUse": PRE_TOOL_USE,
    "PostToolUse": POST_TOOL_USE,
    "UserPromptSubmit": TURN_START,
    "Stop": TURN_END,
    "SessionStart": SESSION_START,
    "SessionEnd": SESSION_END,
    "PreCompact": PRE_COMPACT,
    "SubagentStop": SUBAGENT_STOP,
    "Notification": NOTIFICATION,
}
BUS_EVENTS = tuple(SDK_EVENT_TO_BUS.values())
_TOOL_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE)
# The SDK's Python spellings of JSON keys that are Python keywords.
_KEYWORD_KEYS = {"continue_": "continue", "async_": "async"}


def _output_json(output: Any) -> str:
    """The callback's SyncHookJSONOutput as the JSON the CLI would read."""
    if not isinstance(output, Mapping):
        return ""
    return json.dumps(
        {_KEYWORD_KEYS.get(k, k): v for k, v in output.items() if v is not None},
        default=str,
    )


def _callback_hook(
    bus_event: str, callback: Any, matcher: Optional[str], timeout: float, cwd: str,
):
    name = getattr(callback, "__qualname__", None) or repr(callback)
    reader = CommandHook(event=bus_event, command=f"sdk:{name}", matcher=matcher, cwd=cwd)

    async def hook(event: str, payload: Dict[str, Any]) -> Optional[HookDecision]:
        if event != bus_event:
            return None
        if event in _TOOL_EVENTS and not matcher_matches(matcher, payload.get("name")):
            return None
        hook_input = claude_hook_input(event, payload, cwd=cwd)
        try:
            output = await asyncio.wait_for(
                callback(hook_input, payload.get("tool_use_id"), {"signal": None}),
                timeout,
            )
            return reader.interpret(event, 0, _output_json(output), "")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            problem = f"timed out after {timeout:g}s"
        except Exception as exc:
            if event != PRE_TOOL_USE:
                raise  # the bus reports it as a warning and carries on
            problem = f"failed ({type(exc).__name__}: {exc})"
            logger.warning("%s %s", reader.label, problem, exc_info=True)
        if event == PRE_TOOL_USE:
            # A guard that cannot answer must not let the call through: the
            # engines treat a missing answer as "no opinion" and proceed.
            return HookDecision(
                action="deny",
                note=f"{reader.label} {problem}; the call was denied",
            )
        return HookDecision(warnings=(f"{reader.label} {problem}; continuing",))

    hook.label = reader.label  # type: ignore[attr-defined]
    return hook


def sdk_hooks_bus(hooks: Optional[Mapping[str, Any]], cwd: Optional[str]) -> Optional[HookBus]:
    """A HookBus running every SDK callback in ``hooks``, or None when there
    are none. Events fluid never emits are skipped with a debug line."""
    if not hooks:
        return None
    bus = HookBus()
    registered = 0
    for sdk_event, matchers in hooks.items():
        bus_event = SDK_EVENT_TO_BUS.get(sdk_event)
        if bus_event is None:
            logger.debug("SDK hook event %s has no HookBus equivalent; skipped", sdk_event)
            continue
        for hook_matcher in matchers or ():
            matcher = getattr(hook_matcher, "matcher", None)
            timeout = float(getattr(hook_matcher, "timeout", None) or DEFAULT_TIMEOUT)
            for callback in getattr(hook_matcher, "hooks", None) or ():
                bus.on(bus_event, _callback_hook(bus_event, callback, matcher, timeout, cwd or "."))
                registered += 1
    return bus if registered else None
