"""Shell-command hooks — the config-driven face of :class:`HookBus`.

A ``[[hooks]]`` entry in config.toml (or an imported Claude Code ``hooks.json``
group) becomes one :class:`CommandHook`: a shell string run through the same
shell the Bash tool uses (:func:`fluid.tools.shell.resolve_shell`), with a JSON
payload on stdin describing the event. The process answers with its exit code:

- ``0``  — allow / continue. If stdout is a JSON object, ``{"decision": "deny",
  "reason": "..."}`` denies anyway and ``{"updated_input": {...}}`` (PreToolUse
  only) rewrites the tool input.
- ``2``  — deny. Stderr (falling back to stdout) is the reason the model sees.
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
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Iterable

from fluid.hooks.bus import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    TURN_END,
    TURN_START,
    HookDecision,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
DENY_EXIT_CODE = 2
_REGEX_MARKERS = ("^", "|", "(")
_TOOL_EVENTS = (PRE_TOOL_USE, POST_TOOL_USE)
_TURN_EVENTS = (TURN_START, TURN_END)


def matcher_matches(matcher: str | None, tool_name: str | None) -> bool:
    """Does ``matcher`` select ``tool_name``?

    Empty / ``None`` / ``*`` match everything. A matcher containing ``^``,
    ``|`` or ``(`` is a regular expression (``re.search``, like Claude Code);
    anything else is an fnmatch pattern, so ``mcp__*`` and ``Bash`` both work.
    """
    if not matcher or matcher == "*":
        return True
    if tool_name is None:
        return False
    if any(marker in matcher for marker in _REGEX_MARKERS):
        try:
            return re.search(matcher, tool_name) is not None
        except re.error:
            return False
    return fnmatchcase(tool_name, matcher)


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
        """The JSON object written to the hook's stdin."""
        body: dict[str, Any] = {
            "event": event,
            "session_id": payload.get("session_id"),
            "cwd": self.cwd,
        }
        if event in _TOOL_EVENTS:
            body["tool_name"] = payload.get("name")
            body["tool_input"] = payload.get("input")
            body["tool_use_id"] = payload.get("tool_use_id")
        if event == POST_TOOL_USE:
            # The engine says ``output``; the code-mode bridge says ``result``.
            output = payload.get("output", payload.get("result"))
            body["tool_output"] = output
            body["is_error"] = bool(payload.get("is_error", False))
        if event in _TURN_EVENTS:
            body["turn_id"] = payload.get("turn_id")
        if event == TURN_END:
            body["text"] = payload.get("text", "")
        return body

    async def __call__(self, event: str, payload: dict[str, Any]) -> HookDecision | None:
        if event != self.event:
            return None
        if event in _TOOL_EVENTS and not self.matches(payload.get("name")):
            return None
        stdin = json.dumps(self.build_payload(event, payload), default=str).encode("utf-8")
        try:
            code, stdout, stderr = await self._run(stdin)
        except asyncio.TimeoutError:
            return HookDecision(
                warnings=(f"{self.label} timed out after {self.timeout:g}s; continuing",)
            )
        except OSError as exc:
            return HookDecision(warnings=(f"{self.label} could not start: {exc}",))
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
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), self.timeout)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            raise
        return (
            proc.returncode or 0,
            out.decode("utf-8", errors="replace"),
            err.decode("utf-8", errors="replace"),
        )

    def interpret(self, event: str, code: int, stdout: str, stderr: str) -> HookDecision:
        """Map an exit code plus its streams onto a :class:`HookDecision`."""
        stdout = stdout.strip()
        stderr = stderr.strip()
        if code == DENY_EXIT_CODE:
            return self._deny(event, stderr or stdout or "denied by hook")
        if code != 0:
            detail = stderr or stdout
            tail = f": {detail[-300:]}" if detail else ""
            return HookDecision(
                warnings=(f"{self.label} exited with code {code}{tail}; continuing",)
            )
        parsed = _parse_json_object(stdout)
        if parsed is None:
            return HookDecision()
        if parsed.get("decision") == "deny":
            reason = parsed.get("reason")
            return self._deny(event, str(reason) if reason else "denied by hook")
        updated = parsed.get("updated_input")
        if event == PRE_TOOL_USE and isinstance(updated, dict):
            return HookDecision(action="modify", replacement_input=dict(updated))
        return HookDecision()

    def _deny(self, event: str, reason: str) -> HookDecision:
        """A denial blocks only PreToolUse; elsewhere the action has already
        happened, so the request becomes a warning instead."""
        if event == PRE_TOOL_USE:
            return HookDecision(action="deny", note=reason)
        return HookDecision(
            warnings=(f"{self.label} asked to deny ({reason}) but {event} cannot be blocked",)
        )


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
    except asyncio.TimeoutError:
        log.warning("hook process %s did not exit after kill", proc.pid)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    if not text.startswith("{"):
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
