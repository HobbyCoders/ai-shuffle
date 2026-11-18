"""HostBridge — every in-kernel tool call, run through the real host gate.

The kernel is untrusted (prompt injection == code execution), so the host-call
handler is the one place the host reasserts control. ``HostBridge`` is that
gate: registry + PermissionEngine + optional hooks, with the interactive
round-trips injected as callbacks so the same object serves both the production
engine and a bare embedding.

Two callbacks, both optional, both about asking the human something:

- ``on_permission`` — the engine says "ask"; in production ``CodeModeSession``
  backs it with a ``PermissionRequest``/``RespondPermission`` pair.
- ``on_input_required`` — the *tool* asks (MRTR ``input_required``);
  production backs it with ``QuestionRequest``/``RespondQuestion``.

Unwired, each one fails loudly rather than quietly: a missing permission
handler denies, and a missing input-required handler returns an error telling
the model the user was never asked and to use the JSON tool path. Neither ever
returns an empty result, which would read to the model as "the tool ran and
said nothing".

Hard refusals, all before dispatch: a tool outside the session's tool surface
(``tool_filter``, ruling T9), a tool with ``code_mode_allowed=False`` (the
auditable high-risk escape hatch — the model is told to use the JSON tool path),
a re-entrant ``run_python``, and a denied permission/hook. Refusals come back as
``HostReply(denied=…)`` / ``HostReply(error=…)`` so the kernel *raises* in the
model's code — the error stays in context rather than being silently dropped.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from dataclasses import dataclass, replace

from fluid.codemode.kernel import HostReply, current_host_call_salvage
from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, HookBus, hook_reminder
from fluid.permissions.engine import (
    PROMPT_DENIAL,
    PermissionEngine,
    non_interactive_denial,
)
from fluid.tools.mrtr import resolve_input_required, unresolved_notice
from fluid.tools.registry import (
    FATAL_EXTERNAL_ERROR_CODES,
    InputRequired,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
)


def _approved_input_error(spec: Any, input: dict[str, Any]) -> str | None:
    """Return a concise schema error for a responder-supplied payload.

    The registry performs the normal coercion and handler-level validation for
    ordinary calls. An approval rewrite needs one host-side check before that
    dispatch so a malformed responder payload cannot reach a side-effecting
    handler with required fields missing.
    """
    required = spec.input_schema.get("required", ())
    if not isinstance(required, (list, tuple)):
        return None
    missing = [name for name in required if name not in input]
    if missing:
        return "missing required argument(s): " + ", ".join(map(str, missing))
    return None

# Injected permission round-trip: the engine says "ask", the bridge calls this
# to surface a PermissionRequest and await the user's answer. In production the
# engine loop backs it with a PermissionRequest/RespondPermission pair; tests
# inject a fake that returns a bool. Structured responses mirror the native
# path and carry an approved rewritten input through the gate.
@dataclass(frozen=True, kw_only=True)
class PermissionResult:
    decision: str
    message: str | None = None
    updated_input: dict[str, Any] | None = None


OnPermission = Callable[
    [Any, dict[str, Any]],
    Awaitable[bool | PermissionResult | tuple[str, str | None, dict[str, Any] | None]],
]

# Injected MRTR round-trip: the tool pauses on ``input_required``, the bridge
# calls this with (tool name, this call's tool_use_id, marker) to surface one
# QuestionRequest per request and await the answers. Returns
# {server-request-id: answer}, or None if the turn was interrupted.
OnInputRequired = Callable[
    [str, str | None, InputRequired], Awaitable[dict[str, Any] | None]
]

# A hook answered ``continue: false`` on an in-kernel call: the engine is told
# the reason and ends the turn once the running cell returns.
OnStop = Callable[[str], None]

# Appended to a result the cell gets because the per-call ceiling expired
# while PostToolUse hooks were still running on it.
_POST_HOOKS_UNFINISHED = (
    "[host: the PostToolUse hooks did not finish before this cell's per-call "
    "ceiling and were cancelled; this result carries no hook feedback or "
    "context, and no hook stop was applied]"
)


_RESULT_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "output": (str,), "exit_code": (int,), "task_id": (str,), "failed": (bool,),
}


def _result_fields(outcome: Any) -> dict[str, Any] | None:
    """The structured pieces a cell gets as attributes on a shell-like result.

    A tool's explicit ``result_fields`` win; otherwise a raw ``shell_result``
    supplies ``output`` and ``exit_code``. Only the known keys with JSON-safe
    values cross the kernel pipe.
    """
    fields = outcome.result_fields
    if fields is None and outcome.shell_result is not None:
        output, code = outcome.shell_result
        fields = {"output": output, "exit_code": code}
    if not isinstance(fields, dict):
        return None
    clean = {
        key: value for key, value in fields.items()
        if key in _RESULT_FIELD_TYPES and (
            value is None or (
                isinstance(value, _RESULT_FIELD_TYPES[key])
                and (key == "failed" or not isinstance(value, bool))
            )
        )
    }
    return clean or None


def _soft_external_error(spec: Any, outcome: Any) -> bool:
    """Whether an MCP/inproc error result should come back as the call's
    result text instead of aborting the cell.

    Ordinary tool errors (a stale ref, a stale screenshot) come back as
    results the cell can recover from; a timeout or an unreachable server
    still aborts, because every later call would pay the same wait.
    """
    if getattr(spec, "source", None) not in ("mcp", "inproc"):
        return False
    return getattr(outcome, "error_code", None) not in FATAL_EXTERNAL_ERROR_CODES


class HostBridge:
    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        *,
        hooks: HookBus | None = None,
        on_permission: OnPermission | None = None,
        on_input_required: OnInputRequired | None = None,
        ctx: ToolCtx | None = None,
        doom_loop: bool = True,
        tool_filter: Callable[[str], bool] | None = None,
        result_budget: int | None = None,
        on_stop: OnStop | None = None,
        hook_payload: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self._registry = registry
        # The engine's own tool-hook payload builder (session id, transcript
        # path, turn id), so an in-kernel call reaches PreToolUse/PostToolUse
        # hooks with the same fields as a JSON-path call. Absent: name/input only.
        self._hook_payload = hook_payload
        self._permissions = permissions
        self._hooks = hooks
        self._on_stop = on_stop
        self._on_permission = on_permission
        self._on_input_required = on_input_required
        self._ctx = ctx or ToolCtx()
        self._doom_loop = doom_loop
        self._tool_filter = tool_filter
        # What the kernel delivers verbatim (its ResultRef threshold). Stamped
        # on the shared ctx so repeat-work notes can tell "the model has this"
        # from "the kernel has this" — see ToolCtx.read_delivered.
        if result_budget is not None:
            self._ctx.result_budget = result_budget

    def _tool_hook_payload(self, **fields: Any) -> dict[str, Any]:
        if self._hook_payload is None:
            # No engine builder: the bare payload this bridge always sent
            # (no tool_use_id key, not even as None).
            fields.pop("tool_use_id", None)
            return fields
        return self._hook_payload(**fields)

    async def on_host_call(
        self, tool_name: str, kwargs: dict[str, Any], *,
        tool_use_id: str | None = None,
    ) -> HostReply:
        # Session tool surface (ruling T9), ahead of the registry lookup so the
        # answer does not depend on whether the filtered name happens to exist.
        # The stub was never projected, so the model should not be able to reach
        # this — but a hand-written ``__host_call__`` must not slip past either,
        # and it must fail loudly rather than quietly doing nothing.
        if self._tool_filter is not None and not self._tool_filter(tool_name):
            return HostReply(error=f"tool not available in this session: {tool_name}")

        spec = self._registry.get(tool_name)
        if spec is None:
            return HostReply(error=f"unknown tool: {tool_name}")

        # Re-entrancy: run_python is engine-driven and owns the very kernel this
        # call came out of. It is not projected, so only a hand-written
        # ``__host_call__`` can name it — refuse rather than recurse.
        if tool_name == "run_python":
            return HostReply(
                error=(
                    "run_python cannot be called from inside the kernel — you "
                    "are already in it; just write the code in this cell."
                )
            )

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
        from fluid.tools.bash_file_guard import guard_input

        guidance = guard_input(tool_name, tool_input, self._registry, self._ctx, self._tool_filter, self._permissions)
        if guidance:
            # A routing correction is recoverable text, not a permission denial
            # that aborts this cell or ends the model's turn.
            return HostReply(result=guidance)

        # PreToolUse hook: deny and stop short-circuit (stop also ends the
        # turn), modify rewrites the input, a permission decision of "allow"
        # skips the permission gate and "ask" forces a prompt.
        hook_permission: str | None = None
        if self._hooks is not None:
            decision = await self._hooks.emit(
                PRE_TOOL_USE,
                self._tool_hook_payload(
                    name=tool_name, input=tool_input, tool_use_id=tool_use_id
                ),
            )
            if decision.action == "deny" or decision.stop:
                reason = decision.note or ("stopped by hook" if decision.stop else "denied")
                if decision.stop and self._on_stop is not None:
                    self._on_stop(reason)
                return HostReply(denied=True, message=f"blocked by hook: {reason}")
            if decision.action == "modify" and decision.replacement_input is not None:
                tool_input = decision.replacement_input
            hook_permission = decision.permission_decision

        # PermissionEngine: allow / deny / ask, plus the doom-loop guard.
        if hook_permission == "allow":
            verdict = "allow"
            decided = None
        else:
            decided = self._permissions.decide(spec, tool_input, cwd=self._ctx.cwd)
            verdict = decided.decision
        if verdict != "deny" and self._doom_loop:
            if self._permissions.note_execution(tool_name, tool_input):
                # Match the native gate: Bypass must not park on an approval
                # when the repeat guard trips. Refuse this call with feedback.
                if self._permissions.approval_policy == "never_ask":
                    return HostReply(
                        denied=True,
                        message=(
                            "doom-loop guard: this exact tool call has now been "
                            "repeated several times in a row — not running it "
                            "again. Change the input or the approach."
                        ),
                    )
                verdict = "ask"
        if verdict == "allow" and hook_permission == "ask":
            verdict = "ask"
        if verdict == "deny":
            return HostReply(
                denied=True,
                message=decided.deny_message() if decided is not None else "permission denied",
            )
        if verdict == "ask":
            if self._on_permission is None:
                return HostReply(
                    denied=True,
                    message=non_interactive_denial(tool_name, "no permission handler is wired"),
                )
            response = await self._on_permission(spec, tool_input)
            if isinstance(response, bool):
                permission = PermissionResult(
                    decision="allow" if response else "deny"
                )
            elif isinstance(response, PermissionResult):
                permission = response
            elif isinstance(response, tuple) and len(response) == 3:
                permission = PermissionResult(
                    decision=response[0],
                    message=response[1],
                    updated_input=response[2],
                )
            else:
                return HostReply(
                    denied=True,
                    message="permission callback returned an invalid decision",
                )
            if permission.decision != "allow":
                return HostReply(
                    denied=True,
                    message=permission.message or PROMPT_DENIAL,
                )
            if permission.updated_input is not None:
                if not isinstance(permission.updated_input, dict):
                    return HostReply(
                        denied=True,
                        message="permission callback returned invalid tool arguments",
                    )
                # Hooks have already run. Apply the approved payload only now,
                # so they cannot cause the original input to execute instead.
                tool_input = dict(permission.updated_input)
                input_error = _approved_input_error(spec, tool_input)
                if input_error is not None:
                    return HostReply(
                        denied=True,
                        message=(
                            "permission callback returned invalid tool arguments: "
                            + input_error
                        ),
                    )

        # The call is allowed, whatever decided it; observers hear of it
        # before it runs, as on the JSON path.
        if self._hooks is not None:
            await self._hooks.tool_allowed({
                "session_id": self._ctx.session_id, "name": tool_name,
                "input": tool_input, "tool_use_id": tool_use_id,
            })

        outcome = await self._registry.dispatch(
            tool_name, tool_input, self._ctx, raw_shell_output=True,
        )

        # MRTR: the tool paused on input_required (an elicitating MCP server
        # or a builtin/in-proc tool). Its first leg is an EMPTY result
        # carrying a marker, which must never be returned as ``result``.
        if outcome.input_required is not None:
            if self._on_input_required is None:
                return HostReply(error=unresolved_notice(tool_name))
            outcome = await resolve_input_required(
                outcome,
                name=tool_name,
                collect=lambda marker: self._on_input_required(  # type: ignore[misc]
                    tool_name, tool_use_id, marker
                ),
                redispatch=lambda answers, request_state: self._registry.dispatch(
                    tool_name, tool_input, self._ctx,
                    input_responses=answers, request_state=request_state,
                ),
            )

        salvage = current_host_call_salvage.get()
        if self._hooks is not None:
            if salvage is not None:
                # The tool is done and its output capped (and spilled); only the
                # hooks are left. If the kernel's per-call ceiling expires while
                # they run, the cell gets this instead of a bare cancellation
                # (#1549). A hook that never answered has no opinion, as on the
                # JSON path, and the note says its verdict is missing.
                salvage.reply = self._reply(spec, tool_name, tool_input, replace(
                    outcome,
                    text=f"{outcome.text.rstrip()}\n\n{_POST_HOOKS_UNFINISHED}",
                    content_id=None, window_of=None,
                ))
            decision = await self._hooks.emit(
                POST_TOOL_USE,
                self._tool_hook_payload(
                    name=tool_name,
                    tool_use_id=tool_use_id,
                    input=tool_input,
                    output=outcome.text,
                    result=outcome.text,
                    is_error=outcome.is_error,
                ),
            )
            # Same contract as the JSON path: a block's reason and any added
            # context ride back to the model on the result; a stop ends the
            # turn once this cell returns.
            additions: list[str] = []
            if decision.stop:
                if self._on_stop is not None:
                    self._on_stop(decision.note or "stopped by hook")
            elif decision.action == "deny":
                additions.append(hook_reminder(
                    "PostToolUse hook feedback", decision.note or "blocked by hook",
                ))
            if decision.additional_context:
                additions.append(hook_reminder(
                    "PostToolUse hook context", decision.additional_context,
                ))
            if additions:
                outcome = replace(
                    outcome, text="\n\n".join([outcome.text, *additions]).strip(),
                    content_id=None, window_of=None,
                )

        reply = self._reply(spec, tool_name, tool_input, outcome)
        if salvage is not None:
            # What is left after this (the host's own result event) must not
            # cost the cell its result either.
            salvage.reply = reply
        return reply

    def _reply(
        self, spec: Any, tool_name: str, tool_input: dict[str, Any], outcome: ToolOutcome,
    ) -> HostReply:
        if outcome.is_error and not outcome.soft_error and not _soft_external_error(spec, outcome):
            return HostReply(
                error=outcome.text,
                error_code=(
                    outcome.error_code
                    if tool_name in {"Read", "Grep", "Glob"} and spec.source == "builtin"
                    and spec.effective_risk(tool_input) == "safe" else None
                ),
            )
        return HostReply(
            result=outcome.text,
            images=outcome.images,
            content_id=outcome.content_id,
            window_of=outcome.window_of,
            shell_exit_code=outcome.shell_result[1] if outcome.shell_result is not None else None,
            fields=_result_fields(outcome),
        )
