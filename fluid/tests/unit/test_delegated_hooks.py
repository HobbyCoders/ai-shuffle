"""The delegated leg's Claude Code hooks, forwarded into fluid's HookBus.

The loop registers hook callbacks with the CLI at ``initialize`` (the Agent
SDK's mechanism) and answers each ``hook_callback`` control_request through the
HookBus event the native loop emits at the same point.
"""

from __future__ import annotations

import asyncio

import json
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


async def test_pre_tool_use_policy_with_cwd_gets_the_cli_shell_directory(tmp_path) -> None:
    """The CLI's Bash keeps a ``cd`` between calls; a policy that takes
    ``cwd`` sees the directory the hook payload reports, others keep the
    two-argument call."""
    seen = []

    def policy(name, tool_input, cwd=None):
        seen.append(cwd)
        return "elsewhere" if cwd == "/other/checkout" else None

    loop, written = await _loop(tmp_path, tool_policy=policy)
    denied = await _fire(loop, written, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "git switch main"},
        "tool_use_id": "t1", "cwd": "/other/checkout",
    })
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == "elsewhere"
    assert await _fire(loop, written, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t2",
    }) == {}
    assert seen == ["/other/checkout", "."]

    two_args, written = await _loop(tmp_path / "two", tool_policy=lambda name, _input: None)
    assert await _fire(two_args, written, "PreToolUse", {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t3", "cwd": "/x",
    }) == {}


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


def _peer_ready(loop: DelegatedLoop, *, main_ids=("t1",)) -> StreamJsonMapper:
    """Put one admitted peer in front of the running turn's next boundary and
    return that turn's mapper (the reader's, which alone may deliver it)."""
    from fluid.events.ops import AgentMessage

    loop._peer_admission_open = True
    loop._pending_peers = [AgentMessage(sender="reviewer", text="hold the merge", id="p1", wake=False)]
    loop._turn_peer_ids = {"p1"}
    mapper = StreamJsonMapper(
        session_id=loop.session_id, turn_id="turn_1", model="claude-fake", user_node_id="n0",
    )
    mapper.main_tool_use_ids.update(main_ids)
    return mapper


async def _fire_at_boundary(loop, written, event, data, mapper) -> dict:
    await loop._answer_hook_request(HookCallbackRequest(
        cli_request_id="cli_hook_9", callback_id=f"fluid_{event}",
        input={"hook_event_name": event, "session_id": "cli-sess",
               "transcript_path": "/t/cli.jsonl", "cwd": ".", **data},
        tool_use_id=data.get("tool_use_id"),
    ), mapper=mapper)
    response = written[-1]["response"]
    assert response["subtype"] == "success", response
    return response["response"]


def _receipts(loop: DelegatedLoop) -> list:
    from fluid.events.model import SteerDelivered

    return [e for e in loop.bus.log(loop.session_id) if isinstance(e, SteerDelivered)]


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
async def test_peer_rides_a_main_thread_tool_boundary_beside_hook_context(tmp_path, event) -> None:
    from fluid.context.peer import format_peer_message

    rec = _Recorder({POST_TOOL_USE: HookDecision(additional_context="lint is red")})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    mapper = _peer_ready(loop)
    output = await _fire_at_boundary(loop, written, event, {
        "tool_name": "Bash", "tool_input": {}, "tool_use_id": "t1", "error": "x",
    }, mapper)
    peer = format_peer_message("reviewer", "hold the merge").strip()
    assert output["hookSpecificOutput"]["additionalContext"] == f"lint is red\n\n{peer}"
    assert loop._pending_peers == []
    [receipt] = _receipts(loop)
    assert receipt.steers == [{"id": "p1", "text": "hold the merge", "from": "reviewer"}]
    assert receipt.turn_id == "turn_1"


@pytest.mark.parametrize("case", [
    "no-mapper", "subagent-id", "subagent-agent-id", "admission-closed",
    "interrupted", "hook-stop", "pre-tool-use", "over-budget",
])
async def test_peer_never_rides_an_unsafe_boundary(tmp_path, monkeypatch, case) -> None:
    """Each answer here either cannot reach the running query's next model
    call or is not a tool-result boundary: it carries nothing, receipts
    nothing, and the peer keeps waiting."""
    answers = {}
    if case == "hook-stop":
        answers[POST_TOOL_USE] = HookDecision(stop=True, note="halt")
    rec = _Recorder(answers)
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    mapper = _peer_ready(loop)
    event, data = "PostToolUse", {"tool_name": "Bash", "tool_input": {}, "tool_use_id": "t1"}
    if case == "no-mapper":
        mapper = None  # the between-turn drain / initialize read
    elif case == "subagent-id":
        data["tool_use_id"] = "t_sub"
    elif case == "subagent-agent-id":
        data["agent_id"] = "agent_1"
    elif case == "admission-closed":
        loop._peer_admission_open = False  # the result was read
    elif case == "interrupted":
        loop._interrupted.set()
    elif case == "pre-tool-use":
        event = "PreToolUse"
    elif case == "over-budget":
        monkeypatch.setattr("fluid.engine.delegated.PEER_BOUNDARY_HOOK_BUDGET", -1.0)
    output = await _fire_at_boundary(loop, written, event, data, mapper)
    assert "hold the merge" not in json.dumps(output)
    assert _receipts(loop) == []
    assert [op.id for op in loop._pending_peers] == ["p1"]


async def test_peer_whose_boundary_answer_cannot_be_written_is_not_receipted(tmp_path) -> None:
    rec = _Recorder()
    loop, _written = await _loop(tmp_path, hooks=rec.bus)
    mapper = _peer_ready(loop)

    async def broken(frame: dict) -> None:
        raise OSError("stdin closed")

    loop._write_frame = broken  # type: ignore[method-assign]
    await loop._answer_hook_request(HookCallbackRequest(
        cli_request_id="cli_hook_9", callback_id="fluid_PostToolUse",
        input={"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {},
               "tool_use_id": "t1"},
        tool_use_id="t1",
    ), mapper=mapper)
    assert _receipts(loop) == []
    assert loop._pending_peers == [] and "p1" not in loop._turn_peer_ids


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


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
async def test_cli_scratch_guidance_rides_session_hook_not_user_prompt(tmp_path, source):
    from fluid.cli.host import CliHost
    from fluid.config.model import HarnessConfig
    from fluid.state import scratch_prompt

    loop, written = await _loop(tmp_path)
    host = CliHost(HarnessConfig(cwd=str(tmp_path)), loop.store).attach(loop)
    try:
        output = await _fire(loop, written, "SessionStart", {"source": source})
        assert output == {"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": scratch_prompt(loop.ctx),
        }}
        # Built-in CLI child agents must not inherit the parent's scratch path.
        assert await _fire(loop, written, "SessionStart", {
            "source": source, "agent_id": "child",
        }) == {}
        # App/embedding hosts provide their own guidance instead.
        loop.ctx.extra["harness_state_root"] = tmp_path / "host-state"
        assert await _fire(loop, written, "SessionStart", {"source": source}) == {}
    finally:
        await host.aclose()
        await loop.aclose()


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
        ledger.read_ledger(ToolCtx(cwd=str(tmp_path)), loop.session_id), tool_name=None,
    )
    output = await _fire(loop, written, "SessionStart", {"source": "compact"})
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context == f"{expected}\n\nhook context"
    assert "never push to main" in context
    # This loop has no bundled bridge, so no Ledger tool: the reminder must
    # not tell the model to curate with one.
    assert "curate with" not in context
    assert "read-only here" in context
    # Only a compaction brings it back; a plain start leaves it to the turns.
    startup = await _fire(loop, written, "SessionStart", {"source": "startup"})
    assert "never push to main" not in startup["hookSpecificOutput"]["additionalContext"]


# ─── the host's turn note, put back after a compaction ──────────────────────

_NOTE = "<system-reminder>\nowner is away; today is 2026-10-03\n</system-reminder>"


def _note_provider(text: str | None = _NOTE, *, calls: list | None = None):
    async def provide() -> str | None:
        if calls is not None:
            calls.append(1)
        return text

    return provide


async def _add_ledger(loop: DelegatedLoop, tmp_path) -> str:
    await ledger.handler(
        {"action": "add", "section": "constraints", "text": "never push to main"},
        ToolCtx(cwd=str(tmp_path), session_id=loop.session_id),
    )
    return ledger.render_ledger_reminder(
        ledger.read_ledger(ToolCtx(cwd=str(tmp_path)), loop.session_id), tool_name=None,
    )


async def _fire_main_compact(loop, written, **data) -> dict:
    """SessionStart(compact) the way the CLI fires it for the main thread:
    after its ``status: compacting`` frame, before the null status."""
    loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
    try:
        return await _fire(loop, written, "SessionStart", {"source": "compact", **data})
    finally:
        loop._track_main_compaction({
            "type": "system", "subtype": "status", "status": None, "compact_result": "success",
        })


async def test_compact_puts_the_note_back_after_the_ledger_once(tmp_path) -> None:
    rec = _Recorder({SESSION_START: HookDecision(additional_context="hook context")})
    calls: list = []
    loop, written = await _loop(
        tmp_path, hooks=rec.bus, compact_context=_note_provider(calls=calls),
    )
    expected_ledger = await _add_ledger(loop, tmp_path)
    output = await _fire_main_compact(loop, written)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context == f"{expected_ledger}\n\n{_NOTE}\n\nhook context"
    assert context.count("never push to main") == 1
    assert calls == [1]
    # Rendered fresh for every compaction, never cached.
    await _fire_main_compact(loop, written)
    assert calls == [1, 1]


async def test_the_note_is_only_for_a_compaction(tmp_path) -> None:
    calls: list = []
    loop, written = await _loop(
        tmp_path, hooks=HookBus(), compact_context=_note_provider(calls=calls),
    )
    loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
    for source in ("startup", "resume", "clear"):
        assert await _fire(loop, written, "SessionStart", {"source": source}) == {}
    assert calls == []


async def test_a_subagent_compaction_gets_no_note(tmp_path) -> None:
    """A subagent's compaction fires SessionStart(compact) too (2.1.289), with
    no status frame on the main stream and no agent id; the ledger still
    comes back there, as before, but not the main conversation's note."""
    calls: list = []
    loop, written = await _loop(
        tmp_path, hooks=HookBus(), compact_context=_note_provider(calls=calls),
    )
    expected_ledger = await _add_ledger(loop, tmp_path)
    output = await _fire(loop, written, "SessionStart", {"source": "compact"})
    assert output["hookSpecificOutput"]["additionalContext"] == expected_ledger
    # A payload that names an agent is a subagent's even mid-compaction.
    output = await _fire_main_compact(loop, written, agent_id="a5c59162fe12413ab")
    assert output["hookSpecificOutput"]["additionalContext"] == expected_ledger
    assert calls == []


def test_status_frames_track_the_main_compaction(tmp_path) -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", claude_cmd=["true"])
    assert loop._main_compacting is False
    loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
    assert loop._main_compacting is True
    # A subagent's frame (parent_tool_use_id) and other frames leave it alone.
    loop._track_main_compaction({
        "type": "system", "subtype": "status", "status": None, "parent_tool_use_id": "toolu_1",
    })
    loop._track_main_compaction({"type": "system", "subtype": "compact_boundary"})
    assert loop._main_compacting is True
    loop._track_main_compaction({
        "type": "system", "subtype": "status", "status": None, "compact_result": "failed",
    })
    assert loop._main_compacting is False


@pytest.mark.parametrize("provider", ["raises", "returns_none", "returns_blank", "wrong_type"])
async def test_a_failing_note_leaves_the_old_answer(tmp_path, provider) -> None:
    async def raises() -> str:
        raise RuntimeError("memory store unreadable")

    providers = {
        "raises": raises,
        "returns_none": _note_provider(None),
        "returns_blank": _note_provider("  \n "),
        "wrong_type": _note_provider(42),  # type: ignore[arg-type]
    }
    rec = _Recorder({SESSION_START: HookDecision(additional_context="hook context")})
    loop, written = await _loop(tmp_path, hooks=rec.bus, compact_context=providers[provider])
    expected_ledger = await _add_ledger(loop, tmp_path)
    output = await _fire_main_compact(loop, written)
    assert output["hookSpecificOutput"]["additionalContext"] == (
        f"{expected_ledger}\n\nhook context"
    )


async def test_a_slow_note_is_skipped_at_its_deadline(tmp_path, monkeypatch) -> None:
    import asyncio

    import fluid.engine.delegated as delegated_mod

    monkeypatch.setattr(delegated_mod, "COMPACT_CONTEXT_TIMEOUT", 0.05)

    async def slow() -> str:
        await asyncio.sleep(5)
        return _NOTE

    loop, written = await _loop(tmp_path, hooks=HookBus(), compact_context=slow)
    expected_ledger = await _add_ledger(loop, tmp_path)
    started = asyncio.get_running_loop().time()
    output = await _fire_main_compact(loop, written)
    assert asyncio.get_running_loop().time() - started < 2
    assert output["hookSpecificOutput"]["additionalContext"] == expected_ledger


async def test_the_note_goes_back_in_parts_small_enough_to_reach_the_model(tmp_path) -> None:
    """Claude Code keeps hook context over 10,000 characters in a file and shows
    the model a 2 KB preview, so one answer stays under that, and the whole note
    stays under its cap: the end of an overlong one is cut and says so."""
    from fluid.engine.delegated import COMPACT_CONTEXT_MAX_CHARS, HOOK_CONTEXT_INLINE_CHARS

    assert HOOK_CONTEXT_INLINE_CHARS < 10_000
    loop, written = await _loop(
        tmp_path, hooks=HookBus(), compact_context=_note_provider("x" * 100_000),
    )
    output = await _fire_main_compact(loop, written)
    first = output["hookSpecificOutput"]["additionalContext"]
    assert first.startswith("xxx") and len(first) <= HOOK_CONTEXT_INLINE_CHARS
    assert first.endswith("\n[note continues in a later note]")
    rest = await loop._finish_restoration()
    assert len(rest) <= COMPACT_CONTEXT_MAX_CHARS
    assert f"[note cut: it was longer than {COMPACT_CONTEXT_MAX_CHARS} characters]" in rest
    # A note that fits one answer goes out whole, and is over at once.
    loop.compact_context = _note_provider("y" * HOOK_CONTEXT_INLINE_CHARS)
    output = await _fire_main_compact(loop, written)
    assert output["hookSpecificOutput"]["additionalContext"] == "y" * HOOK_CONTEXT_INLINE_CHARS
    assert loop._restoration is None


def _restorations(loop: DelegatedLoop) -> list:
    from fluid.events.model import DelegatedDiagnostic

    return [e for e in loop.bus.log(loop.session_id)
            if isinstance(e, DelegatedDiagnostic) and e.kind == "compaction_restoration"]


def _boundary(loop: DelegatedLoop) -> None:
    loop._take_main_boundary({"type": "system", "subtype": "compact_boundary"})


async def test_a_cut_note_is_not_credited_as_restored(tmp_path) -> None:
    """#755: a note that went out only in part leaves the put-back pending, and
    the next turn's tail sends what is missing, without repeating what went."""
    from fluid.engine.delegated import COMPACT_CONTEXT_MAX_CHARS, HOOK_CONTEXT_INLINE_CHARS

    size = COMPACT_CONTEXT_MAX_CHARS + 1
    note = "".join(f"line {i:06d}\n" for i in range(size // 12 + 1))[:size]
    loop, written = await _loop(tmp_path, hooks=HookBus(), compact_context=_note_provider(note))
    output = await _fire_main_compact(loop, written)
    _boundary(loop)
    assert loop._context_restore_pending is True
    [record] = _restorations(loop)
    assert record.data["complete"] is False
    [section] = record.data["sections"]
    assert section["name"] == "note" and section["status"] == "pending"
    assert 0 < section["chars"] <= HOOK_CONTEXT_INLINE_CHARS
    assert section["original_chars"] == len(note.strip())
    rest = await loop._finish_restoration()
    await loop._acknowledge_note()
    # All of it went out, across the two, with no line sent twice.
    first = output["hookSpecificOutput"]["additionalContext"].split("\n[note continues")[0]
    rest = rest.removeprefix("[note, continued]\n")
    assert (first + "\n" + rest).split() == note.split()
    assert loop.last_restoration["complete"] is True
    assert loop._context_restore_pending is False
    # Past the bound even the rest cannot all go: it ends cut and is not credited.
    loop.compact_context = _note_provider("x" * 100_000)
    await _fire_main_compact(loop, written)
    _boundary(loop)
    await loop._finish_restoration()
    await loop._acknowledge_note()
    assert loop.last_restoration["complete"] is False
    assert loop.last_restoration["sections"][0]["status"] == "cut"
    # A whole note is credited: nothing is left pending.
    loop._context_restore_pending = False
    loop.compact_context = _note_provider()
    await _fire_main_compact(loop, written)
    _boundary(loop)
    assert loop._context_restore_pending is False
    assert _restorations(loop)[-1].data["complete"] is True


async def test_sections_go_back_by_priority_and_only_whole_ones_are_credited(tmp_path) -> None:
    """The host's sections claim the note by priority, keep their order in it,
    and a section calls back only when the last of it went out."""
    from fluid.engine.compact_restore import RestorationSection
    from fluid.engine.delegated import COMPACT_CONTEXT_MAX_CHARS, HOOK_CONTEXT_INLINE_CHARS

    delivered: list[str] = []
    big = "m" * COMPACT_CONTEXT_MAX_CHARS

    def section(name: str, text: str, priority: int, **kw: Any) -> RestorationSection:
        return RestorationSection(name=name, text=text, priority=priority,
                                  on_delivered=lambda: delivered.append(name), **kw)

    async def provide() -> list:
        return [
            section("environment", "today is 2026-10-28", 0),
            section("memory", big, 3, cuttable=True),
            section("working_record", "record " * 50, 4, cuttable=True),
            section("away", "owner is away", 1),
        ]

    loop, written = await _loop(tmp_path, hooks=HookBus(), compact_context=provide)
    output = await _fire_main_compact(loop, written)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert len(context) <= HOOK_CONTEXT_INLINE_CHARS
    assert context.startswith("today is 2026-10-28\n\nmmm")
    assert context.endswith("owner is away")
    assert "[memory continues" in context and "record" not in context
    assert delivered == ["environment", "away"]
    statuses = {s["name"]: s["status"] for s in _restorations(loop)[-1].data["sections"]}
    assert statuses == {"environment": "delivered", "memory": "pending",
                        "working_record": "omitted", "away": "delivered"}
    assert loop.last_restoration["complete"] is False
    _boundary(loop)
    assert loop._context_restore_pending is True
    # What is left goes on the next turn's tail, and only then is it credited.
    rest = await loop._finish_restoration()
    assert "record" in rest and rest.count("m") + context.count("m") >= COMPACT_CONTEXT_MAX_CHARS
    assert delivered == ["environment", "away"]  # nothing counts before the turn is written
    await loop._acknowledge_note()
    assert delivered == ["environment", "away", "memory", "working_record"]
    assert loop.last_restoration["complete"] is True


def _tool(tool_use_id: str = "t1") -> dict:
    return {"tool_name": "Read", "tool_input": {}, "tool_use_id": tool_use_id}


async def test_a_long_turn_gets_the_rest_of_the_note_at_its_next_tool_results(tmp_path) -> None:
    """A subagent can work one turn for hours: the rest of the note goes out at
    its main thread's tool results, a part each, and then nothing more."""
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS

    note = "\n".join(f"line {i:05d}" for i in range(1_800))
    loop, written = await _loop(tmp_path, hooks=HookBus(), compact_context=_note_provider(note))
    mapper = _peer_ready(loop, main_ids=("t1", "t2", "t3", "t4"))
    loop._pending_peers = []
    loop._active_turn_id = mapper.turn_id
    output = await _fire_main_compact(loop, written)
    parts = [output["hookSpecificOutput"]["additionalContext"]]
    assert not loop.last_restoration["complete"]

    # A subagent's result, and a tool the main thread did not call, carry nothing.
    other = await _fire_at_boundary(loop, written, "PostToolUse", {
        **_tool(), "agent_id": "sub-1",
    }, mapper)
    assert "additionalContext" not in other.get("hookSpecificOutput", {})
    stranger = await _fire_at_boundary(loop, written, "PostToolUse", _tool("not-main"), mapper)
    assert "additionalContext" not in stranger.get("hookSpecificOutput", {})

    for tool_use_id in ("t1", "t2", "t3", "t4"):
        out = await _fire_at_boundary(loop, written, "PostToolUse", _tool(tool_use_id), mapper)
        context = out.get("hookSpecificOutput", {}).get("additionalContext")
        if context:
            assert len(context) <= HOOK_CONTEXT_INLINE_CHARS
            parts.append(context)
    assert 2 <= len(parts) <= 3  # it ends, and no result repeats a part
    lines = [ln for part in parts for ln in part.splitlines() if ln.startswith("line ")]
    assert lines == note.splitlines()
    assert loop.last_restoration["complete"] is True and loop._restoration is None
    # The second compaction's own note, later, is a new one under a new id.
    assert await _fire_at_boundary(loop, written, "PostToolUse", _tool("t1"), mapper) == {}


async def test_a_provider_past_its_limit_joins_the_queue_instead_of_vanishing(
    tmp_path, monkeypatch,
) -> None:
    """#755: a note not ready in time was skipped without a word and credited."""
    from fluid.engine import delegated

    monkeypatch.setattr(delegated, "COMPACT_CONTEXT_TIMEOUT", 0.01)
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "the late note"

    loop, written = await _loop(tmp_path, hooks=HookBus(), compact_context=slow)
    mapper = _peer_ready(loop)
    loop._pending_peers = []
    loop._active_turn_id = mapper.turn_id
    output = await _fire_main_compact(loop, written)
    assert "additionalContext" not in output.get("hookSpecificOutput", {})
    assert loop.last_restoration["complete"] is False
    assert loop.last_restoration["provider"] == "late"
    _boundary(loop)
    assert loop._context_restore_pending is True

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    out = await _fire_at_boundary(loop, written, "PostToolUse", _tool(), mapper)
    assert out["hookSpecificOutput"]["additionalContext"] == "the late note"
    assert loop.last_restoration["complete"] is True


async def test_a_provider_still_late_at_the_next_turn_is_given_up_and_not_credited(
    tmp_path, monkeypatch,
) -> None:
    from fluid.engine import delegated

    monkeypatch.setattr(delegated, "COMPACT_CONTEXT_TIMEOUT", 0.01)

    async def never() -> str:
        await asyncio.Event().wait()
        return "never"

    loop, written = await _loop(tmp_path, hooks=HookBus(), compact_context=never)
    await _fire_main_compact(loop, written)
    late = loop._restoration_late
    assert await loop._finish_restoration() == ""
    await asyncio.sleep(0)
    assert late.cancelled()


async def test_a_batch_of_messages_stays_under_the_inline_limit(tmp_path) -> None:
    """Over the limit the model would read a 2 KB preview of the whole batch, so
    what does not fit waits for the next result: nothing is lost or previewed."""
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS
    from fluid.events.ops import AgentMessage

    loop, written = await _loop(tmp_path)
    mapper = _peer_ready(loop, main_ids=("t1", "t2"))
    loop._pending_peers = [
        AgentMessage(sender="reviewer", text=f"{name} " + "x" * 6_000, id=name, wake=False)
        for name in ("p1", "p2")
    ]
    loop._turn_peer_ids = {"p1", "p2"}
    first = await _fire_at_boundary(loop, written, "PostToolUse", _tool("t1"), mapper)
    text = first["hookSpecificOutput"]["additionalContext"]
    assert "p1 x" in text and "p2 x" not in text and len(text) <= HOOK_CONTEXT_INLINE_CHARS
    assert [p.id for p in loop._pending_peers] == ["p2"]
    second = await _fire_at_boundary(loop, written, "PostToolUse", _tool("t2"), mapper)
    assert "p2 x" in second["hookSpecificOutput"]["additionalContext"]
    assert loop._pending_peers == []


async def test_one_message_over_the_limit_is_still_delivered(tmp_path) -> None:
    """Holding it would lose it; the model gets the preview and the file path."""
    from fluid.events.ops import AgentMessage

    loop, written = await _loop(tmp_path)
    mapper = _peer_ready(loop)
    loop._pending_peers = [AgentMessage(sender="reviewer", text="y" * 20_000, id="p1", wake=False)]
    out = await _fire_at_boundary(loop, written, "PostToolUse", _tool(), mapper)
    assert "y" * 20_000 in out["hookSpecificOutput"]["additionalContext"]
    assert loop._pending_peers == []


async def test_a_delivered_message_is_repeated_only_when_the_answer_dropped_it(tmp_path) -> None:
    """#996: on the Fluid path only what the answer no longer shows comes back,
    so a compaction that kept it adds nothing."""
    from fluid.context.compaction import INSTRUCTIONS_LEAD
    from fluid.engine.delegated import _CompactionTxn
    from fluid.messages.content import SteerDelivery

    loop, _ = await _loop(tmp_path, hooks=HookBus())
    steer = SteerDelivery(id="p1", text="hold the merge", origin="peer", sender="reviewer")
    loop._delivered_on("t1", [steer])
    rendered = steer.render().strip()

    def answer(**fields: Any) -> _CompactionTxn:
        base: dict[str, Any] = {"rows": [], "handles": ()}
        return _CompactionTxn(compaction_id="cmp_1", checkpoint=None, **{**base, **fields})

    dropped = await loop._instructions_section(answer())
    assert dropped is not None and rendered in dropped.text and INSTRUCTIONS_LEAD in dropped.text
    # Its row went back by handle, so Claude Code keeps the attachment on it.
    assert await loop._instructions_section(answer(rows=[
        {"handle": "h1", "toolResults": [{"tool_use_id": "t1", "content": "body"}]},
    ], handles=("h1",))) is None
    # The answer's own text shows it.
    assert await loop._instructions_section(answer(rows=[
        {"text": f"earlier: {rendered}"},
    ])) is None
    # With no answer Claude Code's summarizer may have paraphrased it: all repeat.
    assert rendered in (await loop._instructions_section(None)).text


async def test_harness_notices_are_not_repeated_as_instructions(tmp_path) -> None:
    from fluid.messages.content import SteerDelivery

    loop, _ = await _loop(tmp_path, hooks=HookBus())
    loop._delivered_on(None, [SteerDelivery(text="a background job ended", origin="system")])
    assert await loop._instructions_section(None) is None


async def test_a_note_alone_registers_session_start(tmp_path) -> None:
    """With neither a HookBus nor a deny policy, a note provider still has
    the CLI ask the loop at SessionStart, and the answer needs no bus."""
    loop, written = await _loop(tmp_path, compact_context=_note_provider())
    assert loop._cli_hook_events() == ("PreToolUse", "SessionStart", "PreCompact")
    output = await _fire_main_compact(loop, written)
    assert output["hookSpecificOutput"]["additionalContext"] == _NOTE


FAKE_SCRIPT = str(Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py")


def _hook_callback(request_id: str, **data: Any) -> dict:
    return {"type": "control_request", "request_id": request_id, "request": {
        "subtype": "hook_callback", "callback_id": "fluid_SessionStart", "tool_use_id": None,
        "input": {"hook_event_name": "SessionStart", "session_id": "cli-sess",
                  "transcript_path": "/t/cli.jsonl", "cwd": ".", **data},
    }}


@pytest.mark.parametrize("trigger", ["auto", "manual"])
async def test_the_note_comes_back_in_stream_order_end_to_end(tmp_path, trigger) -> None:
    """The frame order 2.1.289 writes, against the scripted fake CLI: a
    subagent's compaction (no status frame) gets the ledger only; the main
    thread's, automatic mid-turn or manual, gets the ledger and then the note."""
    from fluid.context.compaction import INSTRUCTIONS_LEAD

    status_on = {"type": "system", "subtype": "status", "status": "compacting",
                 "session_id": "cli-sess"}
    status_off = {"type": "system", "subtype": "status", "status": None,
                  "compact_result": "success", "session_id": "cli-sess"}
    boundary = {"type": "system", "subtype": "compact_boundary", "session_id": "cli-sess",
                "compact_metadata": {"trigger": trigger, "pre_tokens": 1276}}
    script = {"turns": [[
        {"op": "frame", "frame": _hook_callback("cli_hook_sub", source="compact")},
        {"op": "frame", "frame": status_on},
        {"op": "frame", "frame": _hook_callback("cli_hook_main", source="compact")},
        {"op": "frame", "frame": status_off},
        {"op": "frame", "frame": boundary},
        # After the main compaction is over, a stray compact SessionStart
        # (a later subagent's) gets no note either.
        {"op": "frame", "frame": _hook_callback("cli_hook_late", source="compact")},
        {"op": "text", "text": "done"},
        {"op": "result"},
    ]]}
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script), encoding="utf-8")
    calls: list = []
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE_SCRIPT],
        env={"FAKE_CLAUDE_SCRIPT": str(script_path)}, cwd=str(tmp_path),
        ctx=ToolCtx(cwd=str(tmp_path)), hooks=HookBus(),
        compact_context=_note_provider(calls=calls),
    )
    answers: dict[str, str] = {}
    write = loop._write_frame

    async def capture(frame: dict) -> None:
        response = frame.get("response") or {}
        if frame.get("type") == "control_response" and str(
            response.get("request_id", "")
        ).startswith("cli_hook_"):
            specific = (response.get("response") or {}).get("hookSpecificOutput") or {}
            answers[response["request_id"]] = specific.get("additionalContext", "")
        await write(frame)

    loop._write_frame = capture  # type: ignore[method-assign]
    try:
        await loop._ensure_session()
        expected_ledger = await _add_ledger(loop, tmp_path)
        await loop.submit(UserTurn(content="/compact" if trigger == "manual" else "go",
                                   accepted_at=1_700_000_000.0))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()

    assert isinstance(events[-1], TurnComplete)
    # With no Fluid answer, Claude Code's summarizer wrote this compaction and may
    # have dropped the brief, so it goes back too (#996). "/compact" is no brief.
    brief = "" if trigger == "manual" else (
        f"{INSTRUCTIONS_LEAD}\nYour original brief:\n"
        "[sent/delivered 2023-11-14 22:13:20 UTC +0000 (UTC)]\ngo\n\n"
    )
    assert answers == {
        "cli_hook_sub": expected_ledger,
        "cli_hook_main": f"{expected_ledger}\n\n{brief}{_NOTE}",
        "cli_hook_late": expected_ledger,
    }
    assert list(answers) == ["cli_hook_sub", "cli_hook_main", "cli_hook_late"]
    assert calls == [1]


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


def test_no_hooks_and_no_policy_still_register_the_output_guard(tmp_path) -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", claude_cmd=["true"])
    assert loop._cli_hook_events() == ("PreToolUse",)
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
    """A deny policy fails closed. Otherwise refusal costs the hooks, as
    before, not the session; it does not diagnose the separate output guard."""
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
        # Do not misdiagnose a policy-registration failure as a guard failure.
        assert not any(isinstance(e, HookEvent) and "tool-output" in e.message for e in events)
    else:
        assert isinstance(events[-1], TurnComplete)
        assert len(results) == 1
        assert not any(isinstance(e, HookEvent) and e.level == "warning" for e in events)
        assert not loop._tool_output_guard_reported


@pytest.mark.parametrize("with_policy", [True, False])
@pytest.mark.parametrize("stage_failed", [True, False])
async def test_current_cli_initialize_timeout_keeps_plain_chats_running_but_not_denied_policy(
    tmp_path, monkeypatch, caplog, with_policy, stage_failed,
) -> None:
    """A registration timeout costs the hooks, as before, not a plain chat.
    Only the pre-existing deny policy requires a stopped turn."""
    import logging
    import fluid.engine.delegated as delegated_mod
    from fluid.engine.delegated_guard_policy import UNGUARDED_WARNING
    from fluid.events.model import ErrorEvent

    monkeypatch.setattr(delegated_mod, "INITIALIZE_TIMEOUT", 0.25)
    if stage_failed:
        monkeypatch.setattr(delegated_mod, "TOOL_OUTPUT_GUARD_SOURCE",
                            str(tmp_path / "missing-guard-source"))
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, FAKE],
        env={"FAKE_CLAUDE_SCENARIO": "u", "FAKE_CLAUDE_IGNORE_INITIALIZE": "1",
             "FAKE_CLAUDE_VERSION": "claude 2.1.314 (fixture)"},
        cwd=str(tmp_path), hooks=HookBus(),
        tool_policy=(lambda name, _i: "no shells here") if with_policy else None,
    )
    try:
        with caplog.at_level(logging.ERROR, logger="fluid.engine.delegated"):
            await loop.submit(UserTurn(content="run it"))
            await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()

    assert loop.cli_version == "claude 2.1.314 (fixture)"
    assert loop.cli_hooks_registered is False
    warnings = [e for e in events if isinstance(e, HookEvent) and e.level == "warning"]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    if with_policy:
        assert isinstance(events[-1], ErrorEvent)
        assert "tool deny policy" in events[-1].message and "timeout" in events[-1].message
        assert not warnings
        assert not any("tool-output" in message for message in errors)
    else:
        assert isinstance(events[-1], TurnComplete)
        assert any(isinstance(e, ToolResult) for e in events)
        if stage_failed:
            assert [e.message for e in warnings] == [UNGUARDED_WARNING]
            assert errors == [UNGUARDED_WARNING]
            assert loop._tool_output_guard_reported
        else:
            assert not warnings and not errors
            assert not loop._tool_output_guard_reported


async def test_the_ledger_reminder_names_the_bridged_ledger_tool(tmp_path) -> None:
    """When the bundled bridge serves the Ledger tool, the reminder names it
    the way the CLI's model calls it."""
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_builtins(registry)
    loop, written = await _loop(tmp_path, hooks=HookBus(), registry=registry)
    await ledger.handler(
        {"action": "add", "section": "constraints", "text": "never push to main"},
        ToolCtx(cwd=str(tmp_path), session_id=loop.session_id),
    )
    loop._socket_server = object()  # the bridge is up
    try:
        output = await _fire(loop, written, "SessionStart", {"source": "compact"})
    finally:
        loop._socket_server = None
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "curate with the mcp__fluid_bundled__Ledger tool" in context


# ─── nothing counts until it is written; identity is the delivery (review of #1050) ──


def _compact_request(request_id: str = "cli_hook_c") -> HookCallbackRequest:
    return HookCallbackRequest(
        cli_request_id=request_id, callback_id="fluid_SessionStart",
        input={"hook_event_name": "SessionStart", "session_id": "cli-sess",
               "transcript_path": "/t/cli.jsonl", "cwd": ".", "source": "compact"},
    )


def _rules_provider(delivered: list[str], *extra: Any):
    from fluid.engine.compact_restore import RestorationSection

    async def provide() -> list:
        return [
            RestorationSection(name="rules", text="never push to main", priority=0,
                               on_delivered=lambda: delivered.append("rules")),
            *extra,
        ]

    return provide


async def test_a_put_back_is_not_credited_when_its_answer_cannot_be_written(tmp_path) -> None:
    """The handler forgets peers on a failed write, and restoration does the same:
    no callback, no completion, and the same words go out at the next chance."""
    delivered: list[str] = []
    loop, written = await _loop(
        tmp_path, hooks=HookBus(), compact_context=_rules_provider(delivered),
    )
    capture = loop._write_frame

    async def closed(_frame: dict) -> None:
        raise OSError("stdin closed")

    loop._write_frame = closed  # type: ignore[method-assign]
    loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
    await loop._answer_hook_request(_compact_request())
    assert delivered == []
    assert not loop._compaction_context_restored
    assert loop._context_restore_pending is True
    assert not any(r.data.get("complete") for r in _restorations(loop))

    loop._write_frame = capture  # type: ignore[method-assign]
    mapper = _peer_ready(loop)
    loop._pending_peers = []
    loop._active_turn_id = mapper.turn_id
    output = await _fire_at_boundary(loop, written, "PostToolUse", _tool(), mapper)
    assert "never push to main" in output["hookSpecificOutput"]["additionalContext"]
    assert delivered == ["rules"] and loop.last_restoration["complete"] is True


async def test_a_cancelled_answer_keeps_the_part_and_the_message(tmp_path) -> None:
    delivered: list[str] = []
    loop, _ = await _loop(tmp_path, hooks=HookBus(), compact_context=_rules_provider(delivered))

    async def cancelled(_frame: dict) -> None:
        raise asyncio.CancelledError

    loop._write_frame = cancelled  # type: ignore[method-assign]
    loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
    with pytest.raises(asyncio.CancelledError):
        await loop._answer_hook_request(_compact_request())
    assert delivered == [] and loop._context_restore_pending is True


async def test_an_error_answer_receipts_no_peer_and_credits_no_part(tmp_path) -> None:
    """The CLI drops a failed hook's context, so neither reached the model."""
    loop, written = await _loop(tmp_path, hooks=HookBus())
    mapper = _peer_ready(loop)
    receipts: list = []
    keep = loop._receipt_boundary

    async def receipt(peers: list, carrier: Any) -> None:
        receipts.append(peers)
        await keep(peers, carrier)

    async def broken(*_: Any) -> None:
        raise RuntimeError("the put-back blew up")

    loop._receipt_boundary = receipt  # type: ignore[method-assign]
    loop._restoration_boundary = broken  # type: ignore[method-assign]
    await loop._answer_hook_request(HookCallbackRequest(
        cli_request_id="cli_hook_e", callback_id="fluid_PostToolUse",
        input={"hook_event_name": "PostToolUse", "session_id": "cli-sess",
               "transcript_path": "/t/cli.jsonl", "cwd": ".", **_tool()},
        tool_use_id="t1",
    ), mapper=mapper)
    assert written[-1]["response"]["subtype"] == "error"
    assert receipts == [] and loop._delivered == []


async def test_a_turn_tail_that_was_not_written_goes_out_with_the_next_turn(tmp_path) -> None:
    """A real turn through the CLI's stdin: the tail counts when its frame is
    written, and a turn that could not write it leaves it owed."""
    from fluid.engine.compact_restore import RestorationSection
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS

    delivered: list[str] = []
    long = "\n".join(f"line {i:05d}" for i in range(1_400))
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", hooks=HookBus(),
        ctx=ToolCtx(cwd=str(tmp_path)), claude_cmd=[sys.executable, FAKE],
        compact_context=_rules_provider(delivered, RestorationSection(
            name="record", text=long, priority=1, cuttable=True,
            on_delivered=lambda: delivered.append("record"),
        )),
    )
    assert len(long) > HOOK_CONTEXT_INLINE_CHARS
    try:
        await loop._ensure_session()
        await loop.submit(UserTurn(content="one"))
        await loop.wait_turn()
        loop._track_main_compaction(
            {"type": "system", "subtype": "status", "status": "compacting"})
        await loop._answer_hook_request(_compact_request())
        loop._track_main_compaction({
            "type": "system", "subtype": "status", "status": None, "compact_result": "success",
        })
        assert delivered == ["rules"] and not loop.last_restoration["complete"]

        frames: list[dict] = []
        real = loop._write_frame

        async def refused(frame: dict) -> None:
            if frame.get("type") == "user":
                raise OSError("stdin closed")
            await real(frame)

        async def seen(frame: dict) -> None:
            frames.append(frame)
            await real(frame)

        loop._write_frame = refused  # type: ignore[method-assign]
        await loop.submit(UserTurn(content="two"))
        await loop.wait_turn()
        assert delivered == ["rules"] and not loop.last_restoration["complete"]
        assert loop._context_restore_pending is True

        loop._write_frame = seen  # type: ignore[method-assign]
        await loop.submit(UserTurn(content="three"))
        await loop.wait_turn()
        assert delivered == ["rules", "record"] and loop.last_restoration["complete"] is True
        sent = json.dumps([f for f in frames if f.get("type") == "user"][-1])
        assert "line 01399" in sent and sent.count("line 01399") == 1
    finally:
        await loop.aclose()


async def test_the_same_words_delivered_again_are_an_instruction_of_their_own(tmp_path) -> None:
    """"Do not deploy" / "You may deploy now" / "Do not deploy": what stands is the
    last, so all three come back in order, however they reached the agent."""
    import re

    from fluid.engine.delegated import _CompactionTxn
    from fluid.messages import Message, ToolResultBlock
    from fluid.messages.content import SteerDelivery
    from fluid.store.base import SessionNode

    said = ["Do not deploy", "You may deploy now", "Do not deploy"]
    words = re.compile("|".join(said[:2]))
    steers = [SteerDelivery(id=f"p{i}", text=text, origin="peer", sender="lead")
              for i, text in enumerate(said)]

    def order(section: Any) -> list[str]:
        return words.findall(section.text)

    # All live: the CLI compacted before any of their results was stored.
    loop, _ = await _loop(tmp_path, hooks=HookBus())
    for i, steer in enumerate(steers):
        loop._delivered_on(f"t{i}", [steer])
    assert order(await loop._instructions_section(None)) == said

    # The first is on the stored path as well: still three, not four.
    await loop.store.append(loop.session_id, SessionNode(
        node_id="n1", parent_id=None, message=Message(role="user", content=[ToolResultBlock(
            tool_use_id="t0", content=[], delivered_steers=[steers[0]],
        )]),
    ))
    assert order(await loop._instructions_section(None)) == said

    # An answer that shows only the first copy: what follows comes back, in order.
    def answer(*shown: SteerDelivery) -> _CompactionTxn:
        rows = [{"text": steer.render()} for steer in shown]
        return _CompactionTxn(compaction_id="cmp_1", checkpoint=None, rows=rows, handles=())

    assert order(await loop._instructions_section(answer(steers[0]))) == said[1:]
    # One copy of the words accounts for one delivery only.
    assert order(await loop._instructions_section(answer(steers[0], steers[1]))) == said[2:]
    assert await loop._instructions_section(answer(*steers)) is None


async def test_peers_are_budgeted_against_the_whole_hook_answer(tmp_path) -> None:
    """What the hook said counts, and the separators too: over the CLI's limit the
    model reads a preview, so the peer waits for a boundary where it fits."""
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS
    from fluid.events.ops import AgentMessage

    hook_said = "h" * 5_000
    rec = _Recorder({POST_TOOL_USE: HookDecision(additional_context=hook_said)})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    mapper = _peer_ready(loop)
    big = AgentMessage(sender="reviewer", text="y" * 6_000, id="p1", wake=False)
    loop._pending_peers = [big]
    output = await _fire_at_boundary(loop, written, "PostToolUse", _tool(), mapper)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context == hook_said and loop._pending_peers == [big]

    # Exactly the limit, separators included, still goes; one more does not.
    overhead = len(loop._boundary_text([AgentMessage(
        sender="reviewer", text="", id="p1", wake=False,
    )]))
    room = HOOK_CONTEXT_INLINE_CHARS - len(hook_said) - 2 - overhead
    over = AgentMessage(sender="reviewer", text="z" * (room + 1), id="p1", wake=False)
    loop._pending_peers = [over]
    output = await _fire_at_boundary(loop, written, "PostToolUse", _tool(), mapper)
    assert output["hookSpecificOutput"]["additionalContext"] == hook_said
    assert loop._pending_peers == [over]
    fits = AgentMessage(sender="reviewer", text="z" * room, id="p1", wake=False)
    loop._pending_peers = [fits]
    output = await _fire_at_boundary(loop, written, "PostToolUse", _tool(), mapper)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert len(context) == HOOK_CONTEXT_INLINE_CHARS and loop._pending_peers == []
    assert context.startswith(f"{hook_said}\n\n")


# ─ the CLI's limit is in UTF-16 units: an emoji is two of them ─

_EMOJI = "\U0001F600"


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


async def test_a_session_start_note_of_emoji_is_not_credited_past_the_cli_limit(tmp_path) -> None:
    """6,008 Python characters are 12,008 to Claude Code's ``e.length <= 10000``: it
    would save them to a file and show a preview, so they go out in parts, and the
    note is complete only when the last part did."""
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS

    note = _EMOJI * 6_000 + "TAIL-END"
    loop, written = await _loop(tmp_path, hooks=HookBus(), compact_context=_note_provider(note))
    output = await _fire_main_compact(loop, written)
    _boundary(loop)
    first = output["hookSpecificOutput"]["additionalContext"]
    assert _units(first) <= HOOK_CONTEXT_INLINE_CHARS
    assert "TAIL-END" not in first and first.encode("utf-8")
    assert loop.last_restoration["complete"] is False
    assert loop._restoration is not None and loop._context_restore_pending is True
    mapper = _peer_ready(loop, main_ids=("t1",))
    loop._pending_peers = []
    loop._active_turn_id = mapper.turn_id
    second = await _fire_at_boundary(loop, written, "PostToolUse", _tool("t1"), mapper)
    rest = second["hookSpecificOutput"]["additionalContext"]
    assert _units(rest) <= HOOK_CONTEXT_INLINE_CHARS and rest.endswith("TAIL-END")
    assert (first + rest).count(_EMOJI) == 6_000
    assert loop.last_restoration["complete"] is True


async def test_session_start_counts_what_else_it_carries_in_those_units(tmp_path) -> None:
    """Existing hook context and the separators count too, in the same units."""
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS

    rec = _Recorder({SESSION_START: HookDecision(additional_context=_EMOJI * 2_500)})
    note = "\n".join(f"line {i:05d}" for i in range(900))
    loop, written = await _loop(tmp_path, hooks=rec.bus, compact_context=_note_provider(note))
    output = await _fire_main_compact(loop, written)
    text = output["hookSpecificOutput"]["additionalContext"]
    assert text.count(_EMOJI) == 2_500 and "line 00000" in text
    assert _units(text) <= HOOK_CONTEXT_INLINE_CHARS
    assert len(text) < _units(text) - 2_000  # the measure the old check used


async def test_post_tool_use_budgets_peers_and_existing_context_in_those_units(tmp_path) -> None:
    """Two messages of 3,000 emoji look like 6,000 characters and are 12,000 to the
    CLI. The second waits for the next result, and existing context counts."""
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS
    from fluid.events.ops import AgentMessage

    loop, written = await _loop(tmp_path)
    mapper = _peer_ready(loop, main_ids=("t1", "t2"))
    loop._pending_peers = [
        AgentMessage(sender="reviewer", text=name + _EMOJI * 3_000, id=name, wake=False)
        for name in ("p1", "p2")
    ]
    loop._turn_peer_ids = {"p1", "p2"}
    first = await _fire_at_boundary(loop, written, "PostToolUse", _tool("t1"), mapper)
    text = first["hookSpecificOutput"]["additionalContext"]
    assert "p1" in text and "p2" not in text and _units(text) <= HOOK_CONTEXT_INLINE_CHARS
    assert [p.id for p in loop._pending_peers] == ["p2"]
    second = await _fire_at_boundary(loop, written, "PostToolUse", _tool("t2"), mapper)
    assert "p2" in second["hookSpecificOutput"]["additionalContext"]

    rec = _Recorder({POST_TOOL_USE: HookDecision(additional_context=_EMOJI * 2_500)})
    loop, written = await _loop(tmp_path, hooks=rec.bus)
    mapper = _peer_ready(loop)
    loop._pending_peers = [AgentMessage(sender="r", text=_EMOJI * 2_500, id="p1", wake=False)]
    output = await _fire_at_boundary(loop, written, "PostToolUse", _tool("t1"), mapper)
    assert output["hookSpecificOutput"]["additionalContext"] == _EMOJI * 2_500
    assert [p.id for p in loop._pending_peers] == ["p1"]


async def test_post_tool_use_fits_a_put_back_beside_emoji_context(tmp_path) -> None:
    """A part of the note goes beside what the hook said, within the limit as a whole."""
    from fluid.engine.delegated import HOOK_CONTEXT_INLINE_CHARS

    note = "\n".join(f"line {i:05d}" for i in range(1_800))
    rec = _Recorder({POST_TOOL_USE: HookDecision(additional_context=_EMOJI * 2_000)})
    loop, written = await _loop(tmp_path, hooks=rec.bus, compact_context=_note_provider(note))
    await _fire_main_compact(loop, written)
    _boundary(loop)
    mapper = _peer_ready(loop, main_ids=("t1",))
    loop._pending_peers = []
    loop._active_turn_id = mapper.turn_id
    output = await _fire_at_boundary(loop, written, "PostToolUse", _tool("t1"), mapper)
    text = output["hookSpecificOutput"]["additionalContext"]
    assert text.startswith(_EMOJI) and "line " in text
    assert _units(text) <= HOOK_CONTEXT_INLINE_CHARS
