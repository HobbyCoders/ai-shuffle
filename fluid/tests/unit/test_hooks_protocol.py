"""Claude Code's hook contract, exercised with real hook scripts: the stdin
payload, the exit codes, the stdout JSON fields, and what the engine does
with each answer on every event."""

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluid.config.loader import build_engine
from fluid.context.compaction import Promotion
from fluid.engine.native import MAX_STOP_HOOK_CONTINUATIONS, NativeLoop
from fluid.events import (
    ErrorEvent,
    HookEvent,
    Interrupt,
    Interrupted,
    PermissionRequest,
    RespondPermission,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from fluid.hooks.bus import (
    POST_TOOL_USE,
    PRE_TOOL_USE,
    SESSION_START,
    TURN_END,
    TURN_START,
    HookBus,
    HookDecision,
)
from fluid.hooks.command import CommandHook
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from tests.unit.test_hooks_config import (
    ScriptedProvider,
    _config_with_hooks,
    _payloads,
    _run_turn,
    _text,
    _tool,
    _weather_registry,
)

# One script, one behaviour per mode. Every run appends its stdin payload to
# the jsonl named in argv[1], so tests can read back what the hook saw.
_HOOK_SCRIPT = """\
import json, sys
payload = json.load(sys.stdin)
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload) + "\\n")
mode = sys.argv[2]
hso = lambda **fields: json.dumps({"hookSpecificOutput": {"hookEventName": payload["hook_event_name"], **fields}})
if mode == "exit2":
    sys.stderr.write("nope from stderr\\n")
    sys.exit(2)
elif mode == "block":
    print(json.dumps({"decision": "block", "reason": "blocked by json"}))
elif mode == "approve":
    print(json.dumps({"decision": "approve"}))
elif mode == "hso_deny":
    print(hso(permissionDecision="deny", permissionDecisionReason="hso says no"))
elif mode == "hso_allow":
    print(hso(permissionDecision="allow", permissionDecisionReason="hso says go"))
elif mode == "hso_ask":
    print(hso(permissionDecision="ask", permissionDecisionReason="please confirm"))
elif mode == "hso_update":
    print(hso(updatedInput={"city": "Oslo"}))
elif mode == "context":
    print(hso(additionalContext="remember the budget"))
elif mode == "plain":
    print("plain stdout context")
elif mode == "stop":
    print(json.dumps({"continue": False, "stopReason": "enough for today"}))
elif mode == "sysmsg":
    print(json.dumps({"systemMessage": "heads up from the hook"}))
elif mode == "loglines":
    print("log line one")
    print("{not json at all")
    print(json.dumps({"decision": "block", "reason": "after the logs"}))
elif mode == "stop_once":
    if not payload["stop_hook_active"]:
        print(json.dumps({"decision": "block", "reason": "finish the tests first"}))
elif mode == "stop_always":
    sys.stderr.write("keep going\\n")
    sys.exit(2)
"""


def _hook(tmp_path: Path, mode: str, tag: str = "") -> tuple[str, Path]:
    """A shell command running the hook script in ``mode`` via this interpreter."""
    script = tmp_path / "hook.py"
    if not script.exists():
        script.write_text(_HOOK_SCRIPT, encoding="utf-8")
    out = tmp_path / f"{mode}{tag}.jsonl"
    py = Path(sys.executable).as_posix()
    return f'"{py}" "{script.as_posix()}" "{out.as_posix()}" {mode}', out


def _direct(tmp_path: Path, event: str, mode: str) -> CommandHook:
    command, _ = _hook(tmp_path, mode)
    return CommandHook(event=event, command=command, cwd=str(tmp_path))


_PRE_PAYLOAD = {"name": "Bash", "input": {"command": "ls"}, "tool_use_id": "t1", "session_id": "s"}


class RecordingProvider(ScriptedProvider):
    """ScriptedProvider that keeps every request it was sent."""

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        super().__init__(scripts)
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        async for chunk in super().stream(request):
            yield chunk


class ErrorProvider(ScriptedProvider):
    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        yield StreamChunk("message_start", {"usage": {"input_tokens": 1}})
        yield StreamChunk("error", {"message": "provider exploded", "retriable": False})


class InterruptingProvider(ScriptedProvider):
    """Presses stop from inside the stream, after the first chunk."""

    engine: NativeLoop | None = None

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        yield StreamChunk("message_start", {"usage": {"input_tokens": 1}})
        assert self.engine is not None
        await self.engine.submit(Interrupt())
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "never seen"})


def _last_user_text(request: ProviderRequest) -> str:
    return request.messages[-1].text()


# ─── interpretation of a hook's answer ───────────────────────────────────────


async def test_block_via_exit_2(tmp_path) -> None:
    decision = await _direct(tmp_path, PRE_TOOL_USE, "exit2")(PRE_TOOL_USE, _PRE_PAYLOAD)
    assert decision is not None
    assert decision.action == "deny" and decision.note == "nope from stderr"


async def test_block_via_decision_block(tmp_path) -> None:
    decision = await _direct(tmp_path, PRE_TOOL_USE, "block")(PRE_TOOL_USE, _PRE_PAYLOAD)
    assert decision is not None
    assert decision.action == "deny" and decision.note == "blocked by json"


@pytest.mark.parametrize(
    ("mode", "action", "permission", "note"),
    [
        ("hso_deny", "deny", None, "hso says no"),
        ("hso_allow", "allow", "allow", None),
        ("hso_ask", "allow", "ask", "please confirm"),
        ("approve", "allow", "allow", None),
    ],
)
async def test_hook_specific_output_permission_decisions(
    tmp_path, mode, action, permission, note
) -> None:
    decision = await _direct(tmp_path, PRE_TOOL_USE, mode)(PRE_TOOL_USE, _PRE_PAYLOAD)
    assert decision is not None
    assert decision.action == action
    assert decision.permission_decision == permission
    assert decision.note == note


async def test_hook_specific_output_updated_input(tmp_path) -> None:
    decision = await _direct(tmp_path, PRE_TOOL_USE, "hso_update")(PRE_TOOL_USE, _PRE_PAYLOAD)
    assert decision is not None
    assert decision.action == "modify" and decision.replacement_input == {"city": "Oslo"}


async def test_continue_false_stops(tmp_path) -> None:
    decision = await _direct(tmp_path, PRE_TOOL_USE, "stop")(PRE_TOOL_USE, _PRE_PAYLOAD)
    assert decision is not None
    assert decision.stop is True and decision.note == "enough for today"


async def test_system_message_is_carried(tmp_path) -> None:
    decision = await _direct(tmp_path, PRE_TOOL_USE, "sysmsg")(PRE_TOOL_USE, _PRE_PAYLOAD)
    assert decision is not None
    assert decision.action == "allow" and decision.system_message == "heads up from the hook"


async def test_log_lines_before_json_still_count(tmp_path) -> None:
    decision = await _direct(tmp_path, PRE_TOOL_USE, "loglines")(PRE_TOOL_USE, _PRE_PAYLOAD)
    assert decision is not None
    assert decision.action == "deny" and decision.note == "after the logs"


async def test_payload_carries_both_key_spellings(tmp_path) -> None:
    command, out = _hook(tmp_path, "plain", "post")
    post = CommandHook(event=POST_TOOL_USE, command=command, cwd=str(tmp_path))
    await post(POST_TOOL_USE, {
        "name": "Bash", "input": {"command": "ls"}, "tool_use_id": "t1",
        "output": "listing", "is_error": False, "session_id": "s1",
        "transcript_path": "/tmp/s1.jsonl",
    })
    (payload,) = _payloads(out)
    assert payload["hook_event_name"] == "PostToolUse" and payload["event"] == "PostToolUse"
    assert payload["tool_response"] == "listing" and payload["tool_output"] == "listing"
    assert payload["session_id"] == "s1"
    assert payload["transcript_path"] == "/tmp/s1.jsonl"
    assert payload["cwd"] == str(tmp_path)

    command, out = _hook(tmp_path, "plain", "start")
    start = CommandHook(event=TURN_START, command=command, cwd=str(tmp_path))
    await start(TURN_START, {"session_id": "s1", "turn_id": "t", "prompt": "hello"})
    (payload,) = _payloads(out)
    assert payload["hook_event_name"] == "UserPromptSubmit" and payload["event"] == "TurnStart"
    assert payload["prompt"] == "hello"
    assert payload["transcript_path"] == ""

    command, out = _hook(tmp_path, "plain", "end")
    end = CommandHook(event=TURN_END, command=command, cwd=str(tmp_path))
    await end(TURN_END, {"session_id": "s1", "turn_id": "t", "text": "bye"})
    (payload,) = _payloads(out)
    assert payload["hook_event_name"] == "Stop" and payload["event"] == "TurnEnd"
    assert payload["stop_hook_active"] is False


# ─── PreToolUse through the engine ───────────────────────────────────────────


async def test_approve_skips_permission_prompt(tmp_path) -> None:
    command, _ = _hook(tmp_path, "approve")
    config = _config_with_hooks(tmp_path, [{"event": "PreToolUse", "command": command}])
    provider = ScriptedProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                 _text("done")])
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(),
        permissions=PermissionEngine("always_ask"),
    )
    events = await _run_turn(engine)
    assert not any(isinstance(e, PermissionRequest) for e in events)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert not result.is_error and "Paris: sunny" in result.output
    approved = next(e for e in events if isinstance(e, HookEvent))
    assert approved.level == "info" and "approved mcp__wx__get_weather" in approved.message


async def test_ask_forces_permission_prompt_and_notification_fires(tmp_path) -> None:
    ask_command, _ = _hook(tmp_path, "hso_ask")
    notify_command, notify_out = _hook(tmp_path, "plain", "notify")
    config = _config_with_hooks(tmp_path, [
        {"event": "PreToolUse", "command": ask_command},
        {"event": "Notification", "command": notify_command},
    ])
    provider = ScriptedProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                 _text("done")])
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(),
        permissions=PermissionEngine("never_ask"),
    )
    await engine.submit(UserTurn(content="weather?"))
    request = None
    for _ in range(500):
        await asyncio.sleep(0.02)
        request = next(
            (e for e in engine.bus.log(engine.session_id) if isinstance(e, PermissionRequest)),
            None,
        )
        if request is not None:
            break
    assert request is not None and request.reason == "please confirm"
    await engine.submit(RespondPermission(request_id=request.request_id, decision="allow"))
    await engine.wait_turn()
    events = list(engine.bus.log(engine.session_id))
    result = next(e for e in events if isinstance(e, ToolResult))
    assert not result.is_error and "Paris: sunny" in result.output
    (notice,) = _payloads(notify_out)
    assert notice["hook_event_name"] == "Notification"
    assert notice["notification_type"] == "permission_prompt"
    assert notice["tool_name"] == "mcp__wx__get_weather"


async def test_pre_tool_use_stop_denies_call_and_ends_turn(tmp_path) -> None:
    command, _ = _hook(tmp_path, "stop")
    end_command, end_out = _hook(tmp_path, "plain", "end")
    config = _config_with_hooks(tmp_path, [
        {"event": "PreToolUse", "command": command},
        {"event": "TurnEnd", "command": end_command},
    ])
    provider = ScriptedProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                 _text("never reached")])
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(),
    )
    events = await _run_turn(engine)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error and result.output == "blocked by hook: enough for today"
    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert error.message == "stopped by hook: enough for today"
    assert provider.calls == 1
    (end,) = _payloads(end_out)
    assert end["stop_reason"] == "stopped"


# ─── UserPromptSubmit ────────────────────────────────────────────────────────


async def test_user_prompt_submit_block_aborts_turn_and_persists_nothing(tmp_path) -> None:
    command, out = _hook(tmp_path, "exit2")
    end_command, end_out = _hook(tmp_path, "plain", "end")
    config = _config_with_hooks(tmp_path, [
        {"event": "UserPromptSubmit", "command": command},
        {"event": "Stop", "command": end_command},
    ])
    provider = ScriptedProvider([_text("never")])
    engine = build_engine(config, MemoryStore(), provider=provider)
    events = await _run_turn(engine, "do something forbidden")
    assert provider.calls == 0
    denial = next(e for e in events if isinstance(e, HookEvent))
    assert denial.level == "deny" and denial.message == "nope from stderr"
    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert error.message == "prompt blocked by hook: nope from stderr"
    assert not any(isinstance(e, TurnComplete) for e in events)
    assert await engine.store.load_path(engine.session_id) == []
    (payload,) = _payloads(out)
    assert payload["prompt"] == "do something forbidden"
    (end,) = _payloads(end_out)
    assert end["stop_reason"] == "blocked"
    # The engine is usable again: the next prompt runs.
    events = await _run_turn(engine, "hello")
    assert provider.calls == 0  # still blocked by the same hook, still no model call
    assert sum(isinstance(e, ErrorEvent) for e in events) == 2


async def test_user_prompt_submit_context_reaches_model(tmp_path) -> None:
    json_command, _ = _hook(tmp_path, "context")
    plain_command, _ = _hook(tmp_path, "plain")
    config = _config_with_hooks(tmp_path, [
        {"event": "UserPromptSubmit", "command": json_command},
        {"event": "UserPromptSubmit", "command": plain_command},
    ])
    provider = RecordingProvider([_text("ok")])
    engine = build_engine(config, MemoryStore(), provider=provider)
    events = await _run_turn(engine, "weather?")
    assert any(isinstance(e, TurnComplete) for e in events)
    user_text = _last_user_text(provider.requests[0])
    assert user_text.startswith("weather?")
    assert "<system-reminder>\nUserPromptSubmit hook context:\nremember the budget" in user_text
    assert "plain stdout context" in user_text
    # The stored node carries the same text, so a resume replays it.
    (node,) = [n for n in await engine.store.load_path(engine.session_id)
               if n.message.role == "user"]
    assert "remember the budget" in node.message.text()


# ─── Stop ────────────────────────────────────────────────────────────────────


async def test_stop_block_continues_once_with_stop_hook_active(tmp_path) -> None:
    command, out = _hook(tmp_path, "stop_once")
    config = _config_with_hooks(tmp_path, [{"event": "Stop", "command": command}])
    provider = RecordingProvider([_text("first answer"), _text("second answer")])
    engine = build_engine(config, MemoryStore(), provider=provider)
    events = await _run_turn(engine)
    assert provider.calls == 2
    first, second = _payloads(out)
    assert first["stop_hook_active"] is False and first["text"] == "first answer"
    assert second["stop_hook_active"] is True and second["text"] == "second answer"
    nudge = _last_user_text(provider.requests[1])
    assert "A Stop hook asked you to continue:\nfinish the tests first" in nudge
    notice = next(e for e in events if isinstance(e, HookEvent))
    assert notice.level == "info" and "finish the tests first" in notice.message
    complete = next(e for e in events if isinstance(e, TurnComplete))
    assert complete.num_model_calls == 2


async def test_stop_block_stops_after_the_cap(tmp_path) -> None:
    command, out = _hook(tmp_path, "stop_always")
    config = _config_with_hooks(tmp_path, [{"event": "Stop", "command": command}])
    provider = ScriptedProvider([_text("again")])
    engine = build_engine(config, MemoryStore(), provider=provider)
    events = await _run_turn(engine)
    assert provider.calls == MAX_STOP_HOOK_CONTINUATIONS + 1
    payloads = _payloads(out)
    assert [p["stop_hook_active"] for p in payloads] == [False] + [True] * MAX_STOP_HOOK_CONTINUATIONS
    capped = [e for e in events if isinstance(e, HookEvent) and e.level == "warning"]
    assert capped and "ending it" in capped[-1].message
    assert any(isinstance(e, TurnComplete) for e in events)


# ─── PostToolUse ─────────────────────────────────────────────────────────────


async def test_post_tool_use_block_and_context_append_to_result(tmp_path) -> None:
    block_command, _ = _hook(tmp_path, "block")
    context_command, _ = _hook(tmp_path, "context")
    config = _config_with_hooks(tmp_path, [
        {"event": "PostToolUse", "command": block_command},
        {"event": "PostToolUse", "command": context_command},
    ])
    provider = RecordingProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                  _text("done")])
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(),
    )
    events = await _run_turn(engine)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert not result.is_error
    assert result.output.startswith("Paris: sunny")
    assert "PostToolUse hook feedback:\nblocked by json" in result.output
    # A block short-circuits the later hooks, so the second hook's context is
    # not part of this result; the model still sees the reason.
    model_saw = provider.requests[1].messages[-1].content[0].content[0].text
    assert "blocked by json" in model_saw


# ─── Session and compaction events ───────────────────────────────────────────


async def test_session_start_fires_with_source_and_seeds_context(tmp_path) -> None:
    command, out = _hook(tmp_path, "context")
    config = _config_with_hooks(tmp_path, [{"event": "SessionStart", "command": command}])
    store = MemoryStore()
    provider = RecordingProvider([_text("ok")])
    engine = build_engine(config, store, provider=provider)
    await _run_turn(engine, "first")
    await _run_turn(engine, "second")
    resumed = build_engine(config, store, provider=provider, session_id=engine.session_id)
    await _run_turn(resumed, "third")
    payloads = _payloads(out)
    assert [p["source"] for p in payloads] == ["startup", "resume"]
    assert all(p["hook_event_name"] == "SessionStart" for p in payloads)
    # The context rides the first user message of each engine only.
    texts = [_last_user_text(r) for r in provider.requests]
    assert "SessionStart hook context:\nremember the budget" in texts[0]
    assert "remember the budget" not in texts[1]
    assert "SessionStart hook context:\nremember the budget" in texts[2]


async def test_pre_compact_fires_with_trigger(tmp_path, monkeypatch) -> None:
    command, out = _hook(tmp_path, "plain")
    config = _config_with_hooks(tmp_path, [{"event": "PreCompact", "command": command}])
    engine = build_engine(config, MemoryStore(), provider=ScriptedProvider([_text("ok")]))
    original = engine._compactor.apply

    def apply_with_batch(nodes, **kwargs):
        rendered, _ = original(nodes, **kwargs)
        return rendered, [Promotion(tier=1, reason="trigger")]

    monkeypatch.setattr(engine._compactor, "apply", apply_with_batch)
    events = await _run_turn(engine)
    assert any(isinstance(e, TurnComplete) for e in events)
    (payload,) = _payloads(out)
    assert payload["hook_event_name"] == "PreCompact"
    assert payload["trigger"] == "auto" and payload["tiers"] == [1]


async def test_session_end_fires_on_close(tmp_path) -> None:
    command, out = _hook(tmp_path, "plain")
    config = _config_with_hooks(tmp_path, [{"event": "SessionEnd", "command": command}])
    engine = build_engine(config, MemoryStore(), provider=ScriptedProvider([_text("ok")]))
    await _run_turn(engine)
    assert _payloads(out) == []
    await engine.aclose()
    (payload,) = _payloads(out)
    assert payload["hook_event_name"] == "SessionEnd" and payload["reason"] == "other"
    assert payload["session_id"] == engine.session_id


# ─── Robustness ──────────────────────────────────────────────────────────────


async def test_turn_end_fires_on_provider_error(tmp_path) -> None:
    command, out = _hook(tmp_path, "plain")
    config = _config_with_hooks(tmp_path, [{"event": "TurnEnd", "command": command}])
    engine = build_engine(config, MemoryStore(), provider=ErrorProvider([]))
    events = await _run_turn(engine)
    assert any(isinstance(e, ErrorEvent) for e in events)
    (payload,) = _payloads(out)
    assert payload["stop_reason"] == "error" and payload["text"] == ""


async def test_turn_end_fires_on_interrupt(tmp_path) -> None:
    command, out = _hook(tmp_path, "plain")
    config = _config_with_hooks(tmp_path, [{"event": "TurnEnd", "command": command}])
    provider = InterruptingProvider([])
    engine = build_engine(config, MemoryStore(), provider=provider)
    provider.engine = engine
    events = await _run_turn(engine)
    assert any(isinstance(e, Interrupted) for e in events)
    (payload,) = _payloads(out)
    assert payload["stop_reason"] == "interrupted"


async def test_raising_callback_does_not_end_turn(tmp_path) -> None:
    bus = HookBus()

    async def broken(event, payload):
        raise RuntimeError("hook bug")

    bus.on(PRE_TOOL_USE, broken)
    config = _config_with_hooks(tmp_path, [])
    provider = ScriptedProvider([_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
                                 _text("done")])
    engine = build_engine(
        config, MemoryStore(), provider=provider, registry=_weather_registry(), hooks=bus,
    )
    events = await _run_turn(engine)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert not result.is_error and "Paris: sunny" in result.output
    warning = next(e for e in events if isinstance(e, HookEvent))
    assert warning.level == "warning" and "raised RuntimeError('hook bug')" in warning.message
    assert any(isinstance(e, TurnComplete) for e in events)


async def test_bus_merges_context_and_permission_across_hooks() -> None:
    bus = HookBus()

    async def allow(event, payload):
        return HookDecision(permission_decision="allow", additional_context="one")

    async def ask(event, payload):
        return HookDecision(permission_decision="ask", note="check", additional_context="two")

    bus.on(SESSION_START, allow)
    bus.on(SESSION_START, ask)
    decision = await bus.emit(SESSION_START, {"session_id": "s"})
    assert decision.permission_decision == "ask" and decision.note == "check"
    assert decision.additional_context == "one\n\ntwo"


# ─── SubagentStop from the CLI host ──────────────────────────────────────────


async def test_subagent_stop_fires_when_task_worker_finishes(tmp_path) -> None:
    import io

    from fluid.cli.__main__ import run_once
    from tests.unit.test_cli_host import RoutingProvider, _allow, _config, _task_call
    from tests.unit.test_cli_host import _text as _host_text

    command, out = _hook(tmp_path, "plain")
    provider = RoutingProvider(
        parent=[_task_call("sync"), _host_text("done")],
        child=[_host_text("child report")],
    )
    config = _config(tmp_path, hooks=[{"event": "SubagentStop", "command": command}])
    await run_once(config, MemoryStore(), "go", provider=provider,
                   out=io.StringIO(), on_permission=_allow)
    (payload,) = _payloads(out)
    assert payload["hook_event_name"] == "SubagentStop"
    assert payload["status"] == "finished" and payload["summary"] == "child report"
    assert payload["stop_hook_active"] is False
    assert payload["worker_session_id"] and payload["session_id"] != payload["worker_session_id"]
