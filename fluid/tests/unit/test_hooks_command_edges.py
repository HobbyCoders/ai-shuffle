"""Command-hook edges: events/matchers that do not apply, spawn failures,
non-zero exits, JSON parsing fallbacks and the process-tree kill."""

from __future__ import annotations

import asyncio
import sys

import pytest

from fluid.hooks import command as command_mod
from fluid.hooks.bus import PRE_COMPACT, PRE_TOOL_USE, SESSION_START, TURN_START
from fluid.hooks.command import CommandHook, _kill_tree, _parse_json_object


def _hook(tmp_path, event=PRE_TOOL_USE, **kw) -> CommandHook:
    return CommandHook(event=event, command=kw.pop("command", "true"), cwd=str(tmp_path), **kw)


async def test_other_event_and_unmatched_tool_are_ignored(tmp_path) -> None:
    hook = _hook(tmp_path, matcher="Bash")
    assert await hook(TURN_START, {}) is None
    assert await hook(PRE_TOOL_USE, {"name": "Read"}) is None


async def test_spawn_failure_becomes_a_warning(tmp_path) -> None:
    hook = CommandHook(event=TURN_START, command="true", cwd=str(tmp_path / "missing-dir"))
    decision = await hook(TURN_START, {})
    assert decision is not None and decision.warnings
    assert "could not run" in decision.warnings[0]


async def test_timeout_becomes_a_warning(tmp_path) -> None:
    hook = _hook(tmp_path, event=TURN_START, command="sleep 30", timeout=0.2)
    decision = await hook(TURN_START, {})
    assert "timed out after 0.2s; continuing" in decision.warnings[0]


def test_label_truncates_long_commands(tmp_path) -> None:
    hook = _hook(tmp_path, command="x" * 100)
    assert hook.label == f"{PRE_TOOL_USE} hook `{'x' * 57}...`"


def test_nonzero_exit_warns_with_output_tail(tmp_path) -> None:
    hook = _hook(tmp_path)
    decision = hook.interpret(PRE_TOOL_USE, 1, "", "e" * 500)
    assert decision.warnings[0].endswith(f": {'e' * 300}; continuing")
    bare = hook.interpret(PRE_TOOL_USE, 3, "", "")
    assert bare.warnings[0].endswith("exited with code 3; continuing")


def test_block_on_unblockable_event_is_a_warning(tmp_path) -> None:
    hook = _hook(tmp_path, event=PRE_COMPACT)
    decision = hook.interpret(PRE_COMPACT, 2, "", "no")
    assert decision.action != "deny" and "cannot be blocked" in decision.warnings[0]


def test_plain_stdout_is_context_only_on_context_events(tmp_path) -> None:
    hook = _hook(tmp_path, event=SESSION_START)
    assert hook.interpret(SESSION_START, 0, "hello\n", "").additional_context == "hello"
    assert hook.interpret(PRE_TOOL_USE, 0, "hello\n", "").additional_context is None


def test_continue_false_and_ask_and_updated_input(tmp_path) -> None:
    hook = _hook(tmp_path)
    stop = hook.interpret(PRE_TOOL_USE, 0, '{"continue": false, "reason": "r"}', "")
    assert stop.stop and stop.note == "r"
    ask = hook.interpret(
        PRE_TOOL_USE, 0,
        '{"hookSpecificOutput": {"permissionDecision": "ask", '
        '"permissionDecisionReason": "check", "updatedInput": {"a": 1}}}', "",
    )
    assert ask.action == "modify" and ask.replacement_input == {"a": 1}
    assert ask.permission_decision == "ask" and ask.note == "check"
    legacy = hook.interpret(PRE_TOOL_USE, 0, '{"decision": "approve", "updated_input": {"b": 2}}', "")
    assert legacy.permission_decision == "allow" and legacy.replacement_input == {"b": 2}
    odd = hook.interpret(PRE_TOOL_USE, 0, '{"hookSpecificOutput": "not-a-dict", "systemMessage": " "}', "")
    assert odd.system_message is None and odd.action != "deny"


def test_parse_json_object_takes_last_decodable_object_line() -> None:
    text = 'log line\n{"a": 1}\n{not json\n  {"b": 2} trailing\n[1, 2]\n'
    assert _parse_json_object(text) == {"b": 2}
    assert _parse_json_object('{"x": [1,\n') is None
    assert _parse_json_object("no objects") is None


class _FakeProc:
    def __init__(self, returncode=None, *, kill_error=False, never_exits=False) -> None:
        self.returncode = returncode
        self.pid = 999_999_999
        self.kill_error = kill_error
        self.never_exits = never_exits
        self.killed = False

    def kill(self) -> None:
        if self.kill_error:
            raise ProcessLookupError
        self.killed = True

    async def wait(self) -> int:
        if self.never_exits:
            await asyncio.sleep(3600)
        return -9


async def test_kill_tree_skips_finished_process() -> None:
    proc = _FakeProc(returncode=0)
    await _kill_tree(proc)
    assert not proc.killed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
async def test_kill_tree_tolerates_vanished_process_and_stuck_wait(monkeypatch, caplog) -> None:
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(command_mod.os, "killpg", gone)
    await _kill_tree(_FakeProc(kill_error=True))  # both kills fail quietly

    real_wait_for = asyncio.wait_for

    async def short_wait_for(aw, timeout):
        return await real_wait_for(aw, 0.01)

    monkeypatch.setattr(command_mod.asyncio, "wait_for", short_wait_for)
    await _kill_tree(_FakeProc(never_exits=True))
    assert "did not exit after kill" in caplog.text
