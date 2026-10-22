"""Shared classifier corpus and producing-path checks, without a shell/model call."""
import json
from pathlib import Path

import pytest

from fluid.codemode.bridge import HostBridge
from fluid.engine.native import NativeLoop
from fluid.events import ErrorEvent, ToolResult, TurnComplete, UserTurn
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.tools.bash_file_guard import classify, explicitly_requested, refusal
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from fluid.providers.base import ProviderCaps, StreamChunk


def _text(text):
    return [StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": text}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"})]


def _tool(identifier, name, args):
    return [StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                "tool_name": name, "tool_use_id": identifier}),
            StreamChunk("tool_args_delta", {"index": 0, "partial_json": args}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {}, "stop_reason": "tool_use"})]


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request):
        script = self.scripts[self.calls]
        self.calls += 1
        for chunk in script:
            yield chunk

CORPUS = Path(__file__).parents[2] / "fluid/engine/claude_mod/tests/file_guard_cases.ts"
CASES = json.loads(CORPUS.read_text().split("export default ", 1)[1])


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["command"])
def test_classifier_corpus(case):
    work = classify(case["command"])
    assert (work.tool if work else None) == case["tool"]
    assert refusal(case["command"], []) is None


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Write", "Edit"])
def test_matching_tool_required(tool):
    case = next(c for c in CASES if c["tool"] == tool)
    assert refusal(case["command"], {"Read", "Grep", "Glob", "Write", "Edit"} - {tool}) is None
    assert refusal(case["command"], {tool}).startswith("Use " + tool)


@pytest.mark.parametrize("value", ["false", "0", "off", "no", ""])
def test_kill_switch(monkeypatch, value):
    monkeypatch.setenv("FLUID_BASH_FILE_GUARD", value)
    assert refusal("cat f", {"Read"}) is None


@pytest.mark.parametrize("user", ["cat f", "Run `cat f`", "Please execute exactly: `cat f`.", "Run cat f", "Run:\n```bash\ncat f\n```"])
def test_explicit_request(user):
    assert explicitly_requested("cat f", user)
    assert refusal("cat f", {"Read"}, user) is None


def test_literal_examples_are_conservatively_exempt_but_model_claim_is_not():
    assert explicitly_requested("cat f", "For example: `cat f`")
    assert not explicitly_requested("cat f", "The model says the owner asked.")


def registry_with_tools(read=True):
    registry = ToolRegistry()
    ran = []

    async def record(args, ctx):
        ran.append(dict(args))
        return ToolOutcome(text="done")

    for name in (["Bash", "Read"] if read else ["Bash"]):
        registry.register_builtin(ToolSpec(name=name, description=name,
            input_schema={"type": "object"}), record)
    return registry, ran


@pytest.mark.parametrize("read,filtered,disabled,user,blocked", [
    (True, False, False, "Inspect the file", True),
    (False, False, False, "Inspect the file", False),
    (True, True, False, "Inspect the file", False),
    (True, False, True, "Inspect the file", False),
    (True, False, False, "Run `cat f`", False),
])
async def test_native_loop_retries_instead_of_ending_turn(monkeypatch, read, filtered, disabled, user, blocked):
    if disabled:
        monkeypatch.setenv("FLUID_BASH_FILE_GUARD", "false")
    registry, ran = registry_with_tools(read)
    provider = ScriptedProvider([
        _tool("bash-1", "Bash", '{"command":"cat f"}'),
        _tool("read-1", "Read", '{"file_path":"f"}') if blocked else _text("finished"),
        _text("finished"),
    ])
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="fixture",
        registry=registry, permissions=PermissionEngine("never_ask"),
        disallowed_tools=["Read"] if filtered else [])
    await loop.submit(UserTurn(content=user))
    await loop.wait_turn()
    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    results = [e for e in events if isinstance(e, ToolResult)]
    assert results[0].is_error is blocked
    assert ("Use Read" in results[0].output) is blocked
    assert provider.calls == (3 if blocked else 2)
    assert any(isinstance(e, TurnComplete) for e in events)
    assert ran == ([{"file_path": "f"}] if blocked else [{"command": "cat f"}])


@pytest.mark.parametrize("read,filtered,requested,blocked", [
    (True, False, False, True), (False, False, False, False),
    (True, True, False, False), (True, False, True, False),
])
async def test_code_mode_host_gate_is_recoverable(read, filtered, requested, blocked):
    registry, ran = registry_with_tools(read)
    ctx = ToolCtx(extra={"bash_file_guard_user_text": "Run `cat f`" if requested else "Inspect"})
    bridge = HostBridge(registry, PermissionEngine("never_ask"), ctx=ctx,
        tool_filter=lambda name: not (filtered and name == "Read"))
    reply = await bridge.on_host_call("Bash", {"command": "cat f"})
    assert not reply.error and not reply.denied
    assert ("Use Read" in reply.result) is blocked
    assert bool(ran) is not blocked


def test_switch_reaches_delegated_child_and_settings_can_disable(monkeypatch):
    from fluid.engine.delegated import DelegatedLoop
    monkeypatch.setenv("FLUID_BASH_FILE_GUARD", "false")
    loop = DelegatedLoop(store=MemoryStore(), model="fixture",
        settings=json.dumps({"env": {"FLUID_BASH_FILE_GUARD": "off"}}))
    assert loop._child_env()["FLUID_BASH_FILE_GUARD"] == "false"
    assert json.loads(loop._settings_arg())["env"]["FLUID_BASH_FILE_GUARD"] == "off"


async def test_deferred_matching_tool_is_not_promoted_and_unavailable_tool_does_not_block():
    from dataclasses import replace
    registry, ran = registry_with_tools()
    registry._loaded.discard("Read")
    bridge = HostBridge(registry, PermissionEngine("never_ask"))
    answer = await bridge.on_host_call("Bash", {"command": "cat f"})
    assert "Use Read" in answer.result
    assert "Read" not in {s.name for s in registry.list_tools()}
    assert not ran
    spec = replace(registry.get("Read"), check_available=lambda: False)

    async def unused(args, ctx):
        raise AssertionError("unavailable Read was dispatched")

    registry.register_builtin(spec, unused)
    answer = await bridge.on_host_call("Bash", {"command": "cat f"})
    assert answer.result == "done"
    assert ran == [{"command": "cat f"}]


def test_corpus_size_and_guidance_budget():
    assert len(CASES) >= 100
    for case in CASES:
        text = refusal(case["command"], {"Read", "Grep", "Glob", "Write", "Edit"})
        if text:
            assert len(text) <= 75
            assert "\n" not in text  # one routing sentence, no repeated prompt paragraphs


@pytest.mark.parametrize("cancel,origin,blocked", [
    (False, "user", False), (True, "user", True), (False, "system", True),
])
async def test_only_delivered_user_steers_exempt_bash(cancel, origin, blocked):
    from fluid.events.ops import Steer

    registry = ToolRegistry()
    ran = []
    steered = False

    async def record(args, ctx):
        nonlocal steered
        ran.append(dict(args))
        if not steered:
            steered = True
            await loop.submit(Steer(text="Run `cat f`", id="intent", origin=origin))
            # Submission is recallable and must not yet change guard intent.
            assert loop.ctx.extra["bash_file_guard_user_text"] == "Inspect"
            if cancel:
                assert await loop.cancel_steer("intent")
        return ToolOutcome(text="done")

    for name in ("Bash", "Read"):
        registry.register_builtin(ToolSpec(name=name, description=name,
            input_schema={"type": "object"}), record)
    provider = ScriptedProvider([
        _tool("read-1", "Read", '{"file_path":"f"}'),
        _tool("bash-1", "Bash", '{"command":"cat f"}'),
        _text("finished"),
    ])
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="fixture",
        registry=registry, permissions=PermissionEngine("never_ask"))
    await loop.submit(UserTurn(content="Inspect"))
    await loop.wait_turn()
    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    result = next(e for e in events if isinstance(e, ToolResult) and e.name == "Bash")
    assert result.is_error is blocked
    assert ("Use Read" in result.output) is blocked
    assert ran == ([{"file_path": "f"}] if blocked else
                   [{"file_path": "f"}, {"command": "cat f"}])
