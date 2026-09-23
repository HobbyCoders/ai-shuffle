"""The delegated leg's Claude Code hooks, forwarded into fluid's HookBus.

The loop registers hook callbacks with the CLI at ``initialize`` (the Agent
SDK's mechanism) and answers each ``hook_callback`` control_request through the
HookBus event the native loop emits at the same point.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import HookCallbackRequest, StreamJsonMapper
from fluid.events.model import HookEvent, ToolResult, TurnComplete
from fluid.events.ops import UserTurn
from fluid.hooks.bus import (
    POST_TOOL_USE,
    PRE_COMPACT,
    PRE_TOOL_USE,
    SESSION_START,
    SUBAGENT_STOP,
    TURN_END,
    TURN_START,
    HookBus,
    HookDecision,
)
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.rules import Rule
from fluid.store.base import MemoryStore
from fluid.tools.builtins import ledger
from fluid.tools.registry import ToolCtx

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")


class _Recorder:
    """A HookBus with one recording callback per event and a scripted answer."""

    def __init__(self, answers: dict[str, HookDecision] | None = None) -> None:
        self.bus = HookBus()
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.answers = answers or {}
        for event in (
            PRE_TOOL_USE, POST_TOOL_USE, TURN_START, TURN_END,
            SESSION_START, PRE_COMPACT, SUBAGENT_STOP,
        ):
            self.bus.on(event, self._record)

    async def _record(self, event: str, payload: dict[str, Any]) -> HookDecision | None:
        self.seen.append((event, dict(payload)))
        return self.answers.get(event)


async def _loop(tmp_path, *, hooks=None, **kwargs) -> tuple[DelegatedLoop, list[dict]]:
    """A loop with a session and no CLI; frames it would write are captured."""
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", hooks=hooks,
        ctx=ToolCtx(cwd=str(tmp_path)), claude_cmd=[sys.executable, FAKE], **kwargs,
    )
    await loop._ensure_session()
    loop._active_turn_id = "turn_1"
    written: list[dict] = []

    async def capture(frame: dict) -> None:
        written.append(frame)

    loop._write_frame = capture  # type: ignore[method-assign]
    return loop, written


async def _fire(loop: DelegatedLoop, written: list[dict], event: str, data: dict) -> dict:
    """Deliver one hook_callback and return the output the loop answered."""
    await loop._answer_hook_request(HookCallbackRequest(
        cli_request_id="cli_hook_9", callback_id=f"fluid_{event}",
        input={"hook_event_name": event, "session_id": "cli-sess",
               "transcript_path": "/t/cli.jsonl", "cwd": ".", **data},
        tool_use_id=data.get("tool_use_id"),
    ))
    frame = written[-1]
    assert frame["type"] == "control_response"
    response = frame["response"]
    assert response["subtype"] == "success", response
    assert response["request_id"] == "cli_hook_9"
    return response["response"]


def _hook_events(loop: DelegatedLoop) -> list[HookEvent]:
    return [e for e in loop.bus.log(loop.session_id) if isinstance(e, HookEvent)]


# ─── one test per hook: the forwarded HookBus event ─────────────────────────


async def test_pre_tool_use_forwards_to_the_bus(tmp_path) -> None:
    rec = _Recorder()
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_1",
    })
    assert output == {}
    assert rec.seen == [(PRE_TOOL_USE, {
        "session_id": loop.session_id, "transcript_path": "/t/cli.jsonl",
        "name": "Bash", "input": {"command": "ls"}, "tool_use_id": "toolu_1",
        "turn_id": "turn_1",
    })]


async def test_pre_tool_use_hook_deny_becomes_the_cli_deny(tmp_path) -> None:
    rec = _Recorder({PRE_TOOL_USE: HookDecision(action="deny", note="no shells")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_1",
    })
    assert output == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "blocked by hook: no shells",
    }}
    assert [(e.level, e.message, e.tool_name) for e in _hook_events(loop)] == [
        ("deny", "no shells", "Bash"),
    ]


async def test_pre_tool_use_policy_deny_blocks_the_call(tmp_path) -> None:
    """The deny policy refuses the call even when a hook approved it: deny
    wins on this seam as it does on can_use_tool."""
    rec = _Recorder({PRE_TOOL_USE: HookDecision(permission_decision="allow")})
    loop, written = await _loop(
        tmp_path, hooks=rec.bus,
        tool_policy=lambda name, _input: "Bash is disabled here" if name == "Bash" else None,
    )
    denied = await _fire(loop, written, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "tool_use_id": "t1",
    })
    assert denied == {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Bash is disabled here",
    }}
    # The hooks still saw the call: observation does not depend on the verdict.
    assert rec.seen[0][0] == PRE_TOOL_USE
    allowed = await _fire(loop, written, "PreToolUse", {
        "tool_name": "Read", "tool_input": {"file_path": "a.txt"}, "tool_use_id": "t2",
    })
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"


async def test_pre_tool_use_falls_back_to_permission_deny_rules(tmp_path) -> None:
    """With no explicit policy, the permission engine's deny rules are the
    policy; its asks stay with the CLI's own permission prompt."""
    loop, written = await _loop(
        tmp_path,
        permissions=PermissionEngine("on_write", rules=[Rule(tool="Bash", decision="deny")]),
    )
    assert loop._cli_hook_events() == ("PreToolUse", "SessionStart")
    denied = await _fire(loop, written, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t1",
    })
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "configured rule" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert await _fire(loop, written, "PreToolUse", {
        "tool_name": "Write", "tool_input": {"file_path": "x"}, "tool_use_id": "t2",
    }) == {}


async def test_bridge_served_calls_are_left_to_the_bridge(tmp_path) -> None:
    """The bundled bridge runs hooks for the calls it serves; the CLI's hook
    must not run them a second time."""
    rec = _Recorder()
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    loop._socket_server = object()  # a running bridge
    output = await _fire(loop, written, "PreToolUse", {
        "tool_name": "mcp__fluid_bundled__Recall", "tool_input": {}, "tool_use_id": "t1",
    })
    assert output == {} and rec.seen == []


async def test_post_tool_use_forwards_output_and_context(tmp_path) -> None:
    rec = _Recorder({POST_TOOL_USE: HookDecision(additional_context="lint is red")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "PostToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t1",
        "tool_response": [{"type": "text", "text": "a.txt"}],
    })
    assert output == {"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": "lint is red",
    }}
    event, payload = rec.seen[0]
    assert event == POST_TOOL_USE
    assert payload["name"] == "Bash" and payload["output"] == "a.txt"
    assert payload["is_error"] is False and payload["tool_use_id"] == "t1"


async def test_post_tool_use_failure_forwards_as_an_error_result(tmp_path) -> None:
    rec = _Recorder({POST_TOOL_USE: HookDecision(action="deny", note="stop editing tests")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "PostToolUseFailure", {
        "tool_name": "Edit", "tool_input": {}, "tool_use_id": "t1", "error": "no match",
    })
    assert output == {"decision": "block", "reason": "stop editing tests"}
    event, payload = rec.seen[0]
    assert event == POST_TOOL_USE
    assert payload["is_error"] is True and payload["output"] == "no match"


async def test_user_prompt_submit_forwards_as_turn_start(tmp_path) -> None:
    rec = _Recorder({TURN_START: HookDecision(additional_context="today is Tuesday")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "UserPromptSubmit", {"prompt": "fix the bug"})
    assert output == {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "today is Tuesday",
    }}
    assert rec.seen == [(TURN_START, {
        "session_id": loop.session_id, "transcript_path": "/t/cli.jsonl",
        "turn_id": "turn_1", "prompt": "fix the bug",
    })]


async def test_user_prompt_submit_block(tmp_path) -> None:
    rec = _Recorder({TURN_START: HookDecision(action="deny", note="outside hours")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "UserPromptSubmit", {"prompt": "deploy"})
    assert output == {"decision": "block", "reason": "outside hours"}


async def test_stop_forwards_as_turn_end_and_caps_continuations(tmp_path) -> None:
    from fluid.engine.native import MAX_STOP_HOOK_CONTINUATIONS

    rec = _Recorder({TURN_END: HookDecision(action="deny", note="tests still fail")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    outputs = [
        await _fire(loop, written, "Stop", {
            "stop_hook_active": i > 0, "last_assistant_message": "Done.",
        })
        for i in range(MAX_STOP_HOOK_CONTINUATIONS + 1)
    ]
    assert outputs[:-1] == [{"decision": "block", "reason": "tests still fail"}] * (
        MAX_STOP_HOOK_CONTINUATIONS
    )
    assert outputs[-1] == {}  # past the cap the turn ends
    event, payload = rec.seen[0]
    assert event == TURN_END
    assert payload["text"] == "Done." and payload["stop_hook_active"] is False
    assert payload["stop_reason"] == "end_turn" and payload["turn_id"] == "turn_1"


async def test_subagent_stop_forwards_and_stays_observational(tmp_path) -> None:
    rec = _Recorder({SUBAGENT_STOP: HookDecision(action="deny", note="redo it")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "SubagentStop", {
        "stop_hook_active": False, "agent_id": "agent_7", "agent_type": "Explore",
        "agent_transcript_path": "/t/agent.jsonl", "last_assistant_message": "found it",
    })
    assert output == {}
    event, payload = rec.seen[0]
    assert event == SUBAGENT_STOP
    assert payload["task_id"] == "agent_7" and payload["summary"] == "found it"
    assert payload["session_id"] == loop.session_id and payload["status"] == "completed"


async def test_pre_compact_forwards(tmp_path) -> None:
    rec = _Recorder()
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "PreCompact", {
        "trigger": "auto", "custom_instructions": None,
    })
    assert output == {}
    assert rec.seen == [(PRE_COMPACT, {
        "session_id": loop.session_id, "transcript_path": "/t/cli.jsonl",
        "turn_id": "turn_1", "trigger": "auto", "custom_instructions": "", "tiers": [],
    })]


async def test_session_start_forwards_with_its_source(tmp_path) -> None:
    rec = _Recorder({SESSION_START: HookDecision(additional_context="branch is main")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    output = await _fire(loop, written, "SessionStart", {"source": "startup"})
    assert output == {"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": "branch is main",
    }}
    assert rec.seen == [(SESSION_START, {
        "session_id": loop.session_id, "transcript_path": "/t/cli.jsonl", "source": "startup",
    })]


async def test_session_start_after_compact_reinjects_the_ledger(tmp_path) -> None:
    """A compaction can drop the ledger from the CLI's summary; the
    compact-sourced SessionStart puts it back, rendered the way the native
    loop recites it."""
    rec = _Recorder({SESSION_START: HookDecision(additional_context="hook context")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    await ledger.handler(
        {"action": "add", "section": "constraints", "text": "never push to main"},
        ToolCtx(cwd=str(tmp_path), session_id=loop.session_id),
    )
    expected = ledger.render_ledger_reminder(
        ledger.read_ledger(ToolCtx(cwd=str(tmp_path)), loop.session_id)
    )
    output = await _fire(loop, written, "SessionStart", {"source": "compact"})
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context == f"{expected}\n\nhook context"
    assert "never push to main" in context
    # Only a compaction brings it back; a plain start leaves it to the turns.
    startup = await _fire(loop, written, "SessionStart", {"source": "startup"})
    assert "never push to main" not in startup["hookSpecificOutput"]["additionalContext"]


async def test_unknown_callback_answers_with_an_error(tmp_path) -> None:
    loop, written = await _loop(tmp_path, hooks=HookBus())
    await loop._answer_hook_request(HookCallbackRequest(
        cli_request_id="r1", callback_id="hook_0", input={},
    ))
    assert written[-1]["response"]["subtype"] == "error"


def test_mapper_surfaces_hook_callbacks() -> None:
    mapper = StreamJsonMapper(
        session_id="s", turn_id="t", model="m", user_node_id="n",
    )
    mapped = mapper.feed({"type": "control_request", "request_id": "cli_1", "request": {
        "subtype": "hook_callback", "callback_id": "fluid_Stop",
        "input": {"hook_event_name": "Stop"}, "tool_use_id": None,
    }})
    assert mapped.events == [] and mapped.permission is None
    assert mapped.hook_request == HookCallbackRequest(
        cli_request_id="cli_1", callback_id="fluid_Stop",
        input={"hook_event_name": "Stop"}, tool_use_id=None,
    )


def test_no_hooks_and_no_policy_register_nothing(tmp_path) -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", claude_cmd=["true"])
    assert loop._cli_hook_events() == ()
    loop = DelegatedLoop(store=MemoryStore(), model="m", claude_cmd=["true"], hooks=HookBus())
    assert set(loop._cli_hook_events()) == {
        "PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
        "Stop", "SubagentStop", "PreCompact", "SessionStart",
    }


# ─── end to end against the fake CLI ────────────────────────────────────────


@pytest.mark.parametrize("policy_denies", [True, False])
async def test_policy_deny_blocks_a_cli_tool_call_end_to_end(tmp_path, policy_denies) -> None:
    """The loop registers its hooks at initialize, the fake CLI asks the
    PreToolUse callback before running Bash, and the policy's deny comes back
    to the model as the tool's error result. The CLI's init frame, read while
    waiting for the initialize reply, still reaches the turn."""
    rec = _Recorder()
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE],
        env={"FAKE_CLAUDE_SCENARIO": "u"}, cwd=str(tmp_path), hooks=rec.bus,
        tool_policy=(lambda name, _i: "no shells here" if name == "Bash" else None)
        if policy_denies else None,
    )
    try:
        await loop.submit(UserTurn(content="run it"))
        await loop.wait_turn()
        assert loop.cli_hooks_registered is True
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()

    assert isinstance(events[-1], TurnComplete)
    assert loop.cli_session_id == "cli-sess-123"
    results = [e for e in events if isinstance(e, ToolResult)]
    assert len(results) == 1
    if policy_denies:
        assert results[0].is_error and results[0].output == "no shells here"
    else:
        assert not results[0].is_error and results[0].output == "hooked"
    assert [(event, payload["name"]) for event, payload in rec.seen] == [(PRE_TOOL_USE, "Bash")]


@pytest.mark.parametrize("with_policy", [True, False])
async def test_a_refused_initialize_fails_closed_when_a_deny_policy_rides_it(
    tmp_path, with_policy,
) -> None:
    """Without its PreToolUse hook the deny policy is off for the whole CLI
    process. With a policy the turn is not started; without one the CLI's
    refusal only costs the hooks, as before."""
    from fluid.events.model import ErrorEvent

    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE],
        env={"FAKE_CLAUDE_SCENARIO": "u", "FAKE_CLAUDE_REFUSE_INITIALIZE": "1"},
        cwd=str(tmp_path), hooks=HookBus(),
        tool_policy=(lambda name, _i: "no shells here") if with_policy else None,
    )
    try:
        await loop.submit(UserTurn(content="run it"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()

    assert loop.cli_hooks_registered is False
    results = [e for e in events if isinstance(e, ToolResult)]
    if with_policy:
        assert isinstance(events[-1], ErrorEvent)
        assert "did not register its hooks (refused)" in events[-1].message
        assert results == []
    else:
        assert isinstance(events[-1], TurnComplete)
        assert len(results) == 1
