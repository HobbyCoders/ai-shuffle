"""Shell-command hooks — the config-driven face of :class:`HookBus`.

A ``[[hooks]]`` entry in config.toml (or an imported Claude Code ``hooks.json``
group) becomes one :class:`CommandHook`: a shell string run through the same
shell the Bash tool uses (:func:`fluid.tools.shell.resolve_shell`), with a JSON
payload on stdin describing the event, in Claude Code's shape (see
docs/hooks.md). The process answers with its exit code:

- ``0``  — allow / continue. stdout may be a JSON object carrying Claude
  Code's fields (``decision``, ``reason``, ``continue``, ``stopReason``,
  ``systemMessage``, ``hookSpecificOutput``) or fluid's older
  ``updated_input`` / ``decision: deny`` spellings. On TurnStart and
  SessionStart plain stdout is added to the model's context.
- ``2``  — block. Stderr (falling back to stdout) is the reason.
- other — the harness logs a warning and continues as if the hook allowed.

A timeout (``timeout`` seconds, default 60) and a failed spawn both count as
allow with a warning. Hooks never raise into the engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Any

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
    HookDecision,
)
from fluid.tools.child_env import scrub_child_env

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DENY_EXIT_CODE = 2
_TOOL_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE)
_TURN_EVENTS = (TURN_START, TURN_END)
# Events whose block has nothing left to block: the request becomes a warning.
_UNBLOCKABLE_EVENTS = (SESSION_START, SESSION_END, PRE_COMPACT, SUBAGENT_STOP, NOTIFICATION)
# Events on which Claude Code injects ``additionalContext`` (or plain stdout).
_CONTEXT_EVENTS = (TURN_START, SESSION_START, POST_TOOL_USE)
_STDOUT_CONTEXT_EVENTS = (TURN_START, SESSION_START)
# Regex metacharacters other than ``*``: a matcher without any of them is a
# plain name or a glob, never a regex.
_REGEX_METACHARS = set(".^$+?{}[]\\|()")

# fluid's internal event names against the names Claude Code uses on the
# wire (``hook_event_name``) and in hooks.json.
CLAUDE_EVENT_NAMES = {TURN_START: "UserPromptSubmit", TURN_END: "Stop"}


def claude_event_name(event: str) -> str:
    """The Claude Code spelling of a fluid hook event."""
    return CLAUDE_EVENT_NAMES.get(event, event)


def matcher_matches(matcher: str | None, tool_name: str | None) -> bool:
    """Does ``matcher`` select ``tool_name``?

    Empty / ``None`` / ``*`` match everything. The matcher is tried as a
    regular expression first, whole-name (``re.fullmatch``, so ``Edit|Write``
    and ``Notebook.*`` behave as in Claude Code) and then searched (Claude
    Code's ``RegExp.test``, so ``^mcp__`` matches too). A matcher with no
    regex metacharacter other than ``*`` is also tried as a glob, so
    ``mcp__*`` and ``Bash`` mean what they look like. A plain name only
    matches itself.
    """
    if not matcher or matcher == "*":
        return True
    if tool_name is None:
        return False
    if matcher == tool_name:
        return True
    has_regex = any(ch in _REGEX_METACHARS for ch in matcher)
    if has_regex:
        try:
            pattern = re.compile(matcher)
        except re.error:
            return False
        return pattern.fullmatch(tool_name) is not None or pattern.search(tool_name) is not None
    if "*" in matcher:
        return fnmatchcase(tool_name, matcher)
    return False


def claude_hook_input(event: str, payload: dict[str, Any], *, cwd: str = ".") -> dict[str, Any]:
    """A HookBus payload in Claude Code's hook-input shape.

    What a command hook reads on stdin, and what the app hands the Agent
    SDK hook callbacks it registers on the bus.

    Claude Code's fields come first (``hook_event_name``, ``session_id``,
    ``transcript_path``, ``cwd``, then the per-event ones); fluid keeps
    its own spellings next to them (``event``, ``tool_output``) so older
    scripts keep working.
    """
    body: dict[str, Any] = {
        "hook_event_name": claude_event_name(event),
        "event": event,
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path") or "",
        "cwd": cwd,
    }
    if event in _TOOL_EVENTS:
        body["tool_name"] = payload.get("name")
        body["tool_input"] = payload.get("input")
        body["tool_use_id"] = payload.get("tool_use_id")
    if event == POST_TOOL_USE:
        # The engine says ``output``; the code-mode bridge says ``result``.
        output = payload.get("output", payload.get("result"))
        body["tool_response"] = output
        body["tool_output"] = output
        body["is_error"] = bool(payload.get("is_error", False))
    if event in _TURN_EVENTS:
        body["turn_id"] = payload.get("turn_id")
    if event == TURN_START:
        body["prompt"] = payload.get("prompt", "")
    if event == TURN_END:
        body["stop_hook_active"] = bool(payload.get("stop_hook_active", False))
        body["stop_reason"] = payload.get("stop_reason", "end_turn")
        body["text"] = payload.get("text", "")
    if event == SESSION_START:
        body["source"] = payload.get("source", "startup")
    if event == SESSION_END:
        body["reason"] = payload.get("reason", "other")
    if event == PRE_COMPACT:
        body["trigger"] = payload.get("trigger", "auto")
        body["custom_instructions"] = payload.get("custom_instructions", "")
        body["tiers"] = list(payload.get("tiers") or ())
    if event == SUBAGENT_STOP:
        body["stop_hook_active"] = False
        for key in ("task_id", "worker_session_id", "status", "summary"):
            body[key] = payload.get(key)
    if event == NOTIFICATION:
        body["message"] = payload.get("message", "")
        body["notification_type"] = payload.get("notification_type", "")
        body["tool_name"] = payload.get("name")
        body["tool_use_id"] = payload.get("tool_use_id")
    return body


@dataclass(frozen=True, kw_only=True)
class CommandHook:
    event: str
    command: str
    matcher: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    cwd: str = "."

    @property
    def label(self) -> str:
        head = self.command if len(self.command) <= 60 else self.command[:57] + "..."
        return f"{self.event} hook `{head}`"

    def matches(self, tool_name: str | None) -> bool:
        return matcher_matches(self.matcher, tool_name)

    def build_payload(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The JSON object written to the hook's stdin (see
        :func:`claude_hook_input`)."""
        return claude_hook_input(event, payload, cwd=self.cwd)

    async def __call__(self, event: str, payload: dict[str, Any]) -> HookDecision | None:
        if event != self.event:
            return None
        if event in _TOOL_EVENTS and not self.matches(payload.get("name")):
            return None
        stdin = json.dumps(self.build_payload(event, payload), default=str).encode("utf-8")
        try:
            code, stdout, stderr = await self._run(stdin)
        except TimeoutError:
            return HookDecision(
                warnings=(f"{self.label} timed out after {self.timeout:g}s; continuing",)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a hook never raises into the engine
            log.warning("%s failed to run", self.label, exc_info=True)
            return HookDecision(warnings=(f"{self.label} could not run: {exc!r}",))
        return self.interpret(event, code, stdout, stderr)

    async def _run(self, stdin: bytes) -> tuple[int, str, str]:
        from fluid.tools.shell import resolve_shell

        spawn_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            # Own process group so a timeout can kill the whole tree.
            spawn_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *resolve_shell().argv_prefix,
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd or None,
            **spawn_kwargs,
            env=scrub_child_env(),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), self.timeout)
        except TimeoutError:
            await _kill_tree(proc)
            raise
        return (
            proc.returncode or 0,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    def interpret(self, event: str, code: int, stdout: str, stderr: str) -> HookDecision:
        """Map an exit code plus its streams onto a :class:`HookDecision`.

        Exit 2 blocks with stderr as the reason. Exit 0 with a JSON object on
        stdout is read the way Claude Code reads it: ``continue: false``
        stops the turn; ``decision`` ``block``/``deny`` blocks and ``approve``
        skips the permission prompt; ``hookSpecificOutput`` carries
        ``permissionDecision`` (allow/deny/ask), ``permissionDecisionReason``,
        ``updatedInput`` and ``additionalContext``; ``systemMessage`` reaches
        the user. fluid's ``updated_input`` still works. Exit 0 with plain
        text is context on TurnStart and SessionStart and nothing elsewhere.
        """
        stdout = stdout.strip()
        stderr = stderr.strip()
        if code == DENY_EXIT_CODE:
            return self._deny(event, stderr or stdout or "blocked by hook")
        if code != 0:
            detail = stderr or stdout
            tail = f": {detail[-300:]}" if detail else ""
            return HookDecision(
                warnings=(f"{self.label} exited with code {code}{tail}; continuing",)
            )
        parsed = _parse_json_object(stdout)
        if parsed is None:
            if stdout and event in _STDOUT_CONTEXT_EVENTS:
                return HookDecision(additional_context=stdout)
            return HookDecision()

        specific = parsed.get("hookSpecificOutput")
        if not isinstance(specific, dict):
            specific = {}
        system_message = _text_or_none(parsed.get("systemMessage"))
        reason = _text_or_none(specific.get("permissionDecisionReason")) or _text_or_none(
            parsed.get("reason")
        )
        context = (
            _text_or_none(specific.get("additionalContext")) if event in _CONTEXT_EVENTS else None
        )

        if parsed.get("continue") is False:
            stop_reason = _text_or_none(parsed.get("stopReason")) or reason
            return HookDecision(
                stop=True, note=stop_reason or "stopped by hook",
                system_message=system_message,
            )

        decision = parsed.get("decision")
        permission = specific.get("permissionDecision")
        if decision in ("deny", "block") or permission == "deny":
            denied = self._deny(event, reason or "blocked by hook")
            return replace(denied, system_message=system_message)

        updated = specific.get("updatedInput")
        if not isinstance(updated, dict):
            updated = parsed.get("updated_input")
        permission_decision: str | None = None
        if event == PRE_TOOL_USE:
            if permission == "ask":
                permission_decision = "ask"
            elif permission == "allow" or decision == "approve":
                permission_decision = "allow"
        if event == PRE_TOOL_USE and isinstance(updated, dict):
            return HookDecision(
                action="modify", replacement_input=dict(updated),
                permission_decision=permission_decision,
                note=reason if permission_decision == "ask" else None,
                system_message=system_message,
            )
        return HookDecision(
            permission_decision=permission_decision,
            note=reason if permission_decision == "ask" else None,
            additional_context=context,
            system_message=system_message,
        )

    def _deny(self, event: str, reason: str) -> HookDecision:
        """A block on an event that has nothing left to block (SessionStart,
        PreCompact, ...) becomes a warning; every other event's block reaches
        the engine as ``deny`` and is interpreted there."""
        if event in _UNBLOCKABLE_EVENTS:
            return HookDecision(
                warnings=(f"{self.label} asked to block ({reason}) but {event} cannot be blocked",)
            )
        return HookDecision(action="deny", note=reason)


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a timed-out hook and everything it spawned.

    The shell wrapper is the direct child; the user's command is usually its
    grandchild, and it holds our stdout/stderr pipes. asyncio only reports the
    process as finished once those pipes close, so killing the shell alone
    would leave ``wait()`` blocked until the grandchild exits on its own.
    """
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                env=scrub_child_env(),
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        log.warning("hook process %s did not exit after kill", proc.pid)


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """The last JSON object in ``text``.

    A script may print log lines before its answer, so every line that starts
    with ``{`` is tried, last first; the first that decodes as an object wins.
    Text after the object is ignored.
    """
    decoder = json.JSONDecoder()
    for start in reversed(_object_line_starts(text)):
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _object_line_starts(text: str) -> list[int]:
    """Offsets of every ``{`` that begins a line (leading spaces allowed)."""
    offsets: list[int] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("{"):
            offsets.append(pos + len(line) - len(stripped))
        pos += len(line)
    return offsets


def register_config_hooks(bus: Any, hooks: Iterable[Any], *, cwd: str = ".") -> list[CommandHook]:
    """Register every :class:`~fluid.config.model.HookConfig` on ``bus``.

    ``bus`` only needs an ``on(event, callback)`` method, so embedding hosts
    with their own bus-shaped object work too. Returns the hooks registered,
    in order.
    """
    registered: list[CommandHook] = []
    for entry in hooks:
        hook = CommandHook(
            event=entry.event,
            command=entry.command,
            matcher=entry.matcher,
            timeout=float(entry.timeout),
            cwd=cwd,
        )
        bus.on(hook.event, hook)
        registered.append(hook)
    return registered
