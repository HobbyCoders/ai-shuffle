"""Read-only tool calls of one model reply run concurrently; everything else
is a barrier that runs alone, in its place.

The native loop dispatches a run of consecutive calls that opted in
(``ToolSpec.concurrent``), whose effective risk is ``safe`` and that the
permission engine allows without a prompt, together. Results reach the model in
call order whatever order they finish in.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fluid.engine.native import NativeLoop
from fluid.events import (
    Interrupt,
    PermissionRequest,
    RespondPermission,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from fluid.events.ops import Steer
from fluid.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from fluid.messages import ToolResultBlock
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.rules import Rule
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}}}


def _calls(*calls: tuple[str, str, dict]) -> list[StreamChunk]:
    """One assistant reply carrying every ``(tool_use_id, name, input)``."""
    chunks = [StreamChunk("message_start", {"usage": {"input_tokens": 10}})]
    for index, (tuid, name, args) in enumerate(calls):
        chunks += [
            StreamChunk("block_start", {
                "index": index, "block_type": "tool_use",
                "tool_name": name, "tool_use_id": tuid,
            }),
            StreamChunk("tool_args_delta", {"index": index, "partial_json": json.dumps(args)}),
            StreamChunk("block_stop", {"index": index}),
        ]
    chunks += [
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]
    return chunks


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


class ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


class Timeline:
    """Records when each call started and ended, and how many overlapped."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.started: dict[str, asyncio.Event] = {}
        self.ids_seen: dict[str, Any] = {}

    def started_event(self, call: str) -> asyncio.Event:
        return self.started.setdefault(call, asyncio.Event())

    def index(self, kind: str, call: str) -> int:
        return self.events.index((kind, call))

    def ran_before(self, first: str, second: str) -> bool:
        return self.index("end", first) < self.index("start", second)

    def overlapped(self, a: str, b: str) -> bool:
        return not self.ran_before(a, b) and not self.ran_before(b, a)


def _registry(timeline: Timeline, delays: dict[str, float]) -> ToolRegistry:
    """``Probe`` (concurrent, safe), ``Touch`` (risk write) and ``Serial``
    (safe but never opted in). Each sleeps ``delays[id]`` seconds."""

    def make(label: str):
        async def handler(input: dict, ctx: ToolCtx) -> ToolOutcome:
            call = input["id"]
            timeline.events.append(("start", call))
            timeline.active += 1
            timeline.max_active = max(timeline.max_active, timeline.active)
            timeline.started_event(call).set()
            try:
                await asyncio.sleep(delays.get(call, 0.0))
                # The call's own id must still be on its ctx after siblings ran.
                timeline.ids_seen[call] = ctx.extra.get("tool_use_id")
                if call.startswith("fail"):
                    raise RuntimeError(f"{call} broke")
                return ToolOutcome(text=f"{label}:{call}")
            finally:
                timeline.active -= 1
                timeline.events.append(("end", call))
        return handler

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(name="Probe", description="read", input_schema=_SCHEMA, risk="safe",
                 concurrent=True),
        make("probe"),
    )
    registry.register_builtin(
        ToolSpec(name="Touch", description="write", input_schema=_SCHEMA, risk="write",
                 concurrent=True),
        make("touch"),
    )
    registry.register_builtin(
        ToolSpec(name="Serial", description="read, not audited", input_schema=_SCHEMA,
                 risk="safe"),
        make("serial"),
    )
    return registry


def _loop(provider, registry, *, permissions=None, hooks=None, store=None, sid=None,
          **kwargs) -> NativeLoop:
    return NativeLoop(
        provider=provider, store=store or MemoryStore(), model="claude-sonnet-5",
        session_id=sid, registry=registry,
        permissions=permissions or PermissionEngine("never_ask"), hooks=hooks, **kwargs,
    )


def _result_order(provider: ScriptedProvider) -> list[str]:
    """tool_use ids of the results sent back to the model, in transcript order."""
    message = provider.requests[1].messages[-1]
    return [b.tool_use_id for b in message.content if isinstance(b, ToolResultBlock)]


def _results(loop: NativeLoop) -> list[ToolResult]:
    return [e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult)]


async def test_two_slow_reads_take_about_the_time_of_one() -> None:
    timeline = Timeline()
    # The first call is the slower one, so they finish out of call order.
    registry = _registry(timeline, {"a": 0.4, "b": 0.2})
    provider = ScriptedProvider([
        _calls(("a", "Probe", {"id": "a"}), ("b", "Probe", {"id": "b"})),
        _text("done"),
    ])
    loop = _loop(provider, registry)
    began = time.monotonic()
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    elapsed = time.monotonic() - began

    assert timeline.max_active == 2
    assert elapsed < 0.55  # one-at-a-time would be at least 0.6s
    # Live results arrive as each call finishes; the model sees call order.
    assert [r.tool_use_id for r in _results(loop)] == ["b", "a"]
    assert _result_order(provider) == ["a", "b"]
    assert [r.output for r in _results(loop)] == ["probe:b", "probe:a"]
    # Each call saw its own id although they ran on one session ctx.
    assert timeline.ids_seen == {"a": "a", "b": "b"}
    assert "tool_use_id" not in loop.ctx.extra
    done = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete))
    assert done.stop_reason == "end_turn"


async def test_mixed_batch_keeps_order_and_the_write_is_a_barrier() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"r1": 0.15, "r2": 0.1, "w": 0.05, "r3": 0.05})
    provider = ScriptedProvider([
        _calls(
            ("r1", "Probe", {"id": "r1"}), ("r2", "Probe", {"id": "r2"}),
            ("w", "Touch", {"id": "w"}), ("r3", "Probe", {"id": "r3"}),
        ),
        _text("done"),
    ])
    loop = _loop(provider, registry)  # never_ask: the write needs no prompt
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    assert timeline.overlapped("r1", "r2")
    assert timeline.ran_before("r1", "w") and timeline.ran_before("r2", "w")
    assert timeline.ran_before("w", "r3")
    assert _result_order(provider) == ["r1", "r2", "w", "r3"]


async def test_tool_without_the_opt_in_is_a_barrier() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"a": 0.05, "s": 0.05, "b": 0.05})
    provider = ScriptedProvider([
        _calls(("a", "Probe", {"id": "a"}), ("s", "Serial", {"id": "s"}),
               ("b", "Probe", {"id": "b"})),
        _text("done"),
    ])
    loop = _loop(provider, registry)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    assert timeline.max_active == 1
    assert [kind + call for kind, call in timeline.events] == [
        "starta", "enda", "starts", "ends", "startb", "endb",
    ]


async def test_concurrency_limit_bounds_a_run() -> None:
    timeline = Timeline()
    ids = ["c1", "c2", "c3", "c4", "c5"]
    registry = _registry(timeline, dict.fromkeys(ids, 0.05))
    provider = ScriptedProvider([
        _calls(*[(i, "Probe", {"id": i}) for i in ids]), _text("done"),
    ])
    loop = _loop(provider, registry, max_concurrent_tools=2)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    assert timeline.max_active == 2
    assert _result_order(provider) == ids


async def test_limit_of_one_dispatches_in_order() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"a": 0.05, "b": 0.05})
    provider = ScriptedProvider([
        _calls(("a", "Probe", {"id": "a"}), ("b", "Probe", {"id": "b"})), _text("done"),
    ])
    loop = _loop(provider, registry, max_concurrent_tools=1)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    assert timeline.max_active == 1
    assert timeline.ran_before("a", "b")


async def test_interrupt_mid_batch_leaves_every_call_with_one_result() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"r1": 30, "r2": 30, "r3": 30, "w": 30})
    provider = ScriptedProvider([
        _calls(
            ("r1", "Probe", {"id": "r1"}), ("r2", "Probe", {"id": "r2"}),
            ("r3", "Probe", {"id": "r3"}), ("w", "Touch", {"id": "w"}),
        ),
        _text("never reached"),
    ])
    loop = _loop(provider, registry)
    await loop.submit(UserTurn(content="go"))
    for call in ("r1", "r2", "r3"):
        await asyncio.wait_for(timeline.started_event(call).wait(), timeout=5)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    results = _results(loop)
    assert sorted(r.tool_use_id for r in results) == ["r1", "r2", "r3", "w"]
    assert all(r.is_error and "interrupted" in r.output for r in results)
    assert ("start", "w") not in timeline.events  # the barrier never started
    assert timeline.active == 0  # every running call was cancelled
    assert provider.calls == 1


async def test_one_failing_call_does_not_cancel_its_siblings() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"fail1": 0.1, "ok": 0.2})
    provider = ScriptedProvider([
        _calls(("fail1", "Probe", {"id": "fail1"}), ("ok", "Probe", {"id": "ok"})),
        _text("done"),
    ])
    loop = _loop(provider, registry)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    by_id = {r.tool_use_id: r for r in _results(loop)}
    assert by_id["fail1"].is_error and "fail1 broke" in by_id["fail1"].output
    assert not by_id["ok"].is_error and by_id["ok"].output == "probe:ok"
    assert timeline.overlapped("fail1", "ok")
    assert _result_order(provider) == ["fail1", "ok"]


async def _answer_first_prompt(loop: NativeLoop, sid: str, check) -> PermissionRequest:
    """Wait for the first PermissionRequest, run ``check`` while it is open,
    then allow it."""
    seen: dict[str, PermissionRequest] = {}

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid):
            if isinstance(event, PermissionRequest):
                seen["req"] = event
                check(event)
                await loop.submit(RespondPermission(
                    request_id=event.request_id, decision="allow",
                ))
                return

    return seen, asyncio.create_task(watch())


async def test_call_needing_a_prompt_runs_alone() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"r1": 0.1, "p": 0.05, "r2": 0.05})
    provider = ScriptedProvider([
        _calls(("r1", "Probe", {"id": "r1"}), ("p", "Probe", {"id": "p"}),
               ("r2", "Probe", {"id": "r2"})),
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    permissions = PermissionEngine(
        "on_write", rules=[Rule(tool="Probe", decision="ask", input_globs={"id": "p"})],
    )
    loop = _loop(provider, registry, permissions=permissions, store=store, sid=sid)
    states: list[tuple[list, int]] = []
    seen, watcher = await _answer_first_prompt(
        loop, sid, lambda _req: states.append((list(timeline.events), timeline.active)),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    await watcher

    assert seen["req"].tool_use_id == "p"
    events_at_prompt, active_at_prompt = states[0]
    # While the prompt was open nothing else ran: r1 had finished, r2 had not
    # started.
    assert active_at_prompt == 0
    assert ("end", "r1") in events_at_prompt
    assert ("start", "r2") not in events_at_prompt
    assert timeline.max_active == 1
    assert _result_order(provider) == ["r1", "p", "r2"]


async def test_hook_asking_inside_a_run_turns_that_call_into_a_barrier() -> None:
    """The dry check passed all three; the hook's ask is only known at the
    gate, so the call waits for its earlier siblings and holds back the later
    ones before the prompt is shown."""
    timeline = Timeline()
    registry = _registry(timeline, {"r1": 0.2, "r2": 0.05, "r3": 0.05})
    provider = ScriptedProvider([
        _calls(("r1", "Probe", {"id": "r1"}), ("r2", "Probe", {"id": "r2"}),
               ("r3", "Probe", {"id": "r3"})),
        _text("done"),
    ])
    hooks = HookBus()
    gate_order: list[str] = []

    async def pre(_event: str, payload: dict) -> HookDecision:
        gate_order.append(payload["tool_use_id"])
        if payload["tool_use_id"] == "r2":
            return HookDecision(permission_decision="ask", note="check r2")
        return HookDecision()

    hooks.on(PRE_TOOL_USE, pre)
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _loop(provider, registry, permissions=PermissionEngine("on_write"),
                 hooks=hooks, store=store, sid=sid)
    states: list[tuple[list, int]] = []
    seen, watcher = await _answer_first_prompt(
        loop, sid, lambda _req: states.append((list(timeline.events), timeline.active)),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    await watcher

    assert seen["req"].tool_use_id == "r2"
    events_at_prompt, active_at_prompt = states[0]
    assert active_at_prompt == 0
    assert ("end", "r1") in events_at_prompt
    assert ("start", "r3") not in events_at_prompt
    assert gate_order == ["r1", "r2", "r3"]  # PreToolUse fires per call, in order
    assert _result_order(provider) == ["r1", "r2", "r3"]


async def test_hook_rewrite_inside_a_run_is_a_barrier() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"r1": 0.15, "r2": 0.05, "r3": 0.05})
    provider = ScriptedProvider([
        _calls(("r1", "Probe", {"id": "r1"}), ("r2", "Probe", {"id": "r2"}),
               ("r3", "Probe", {"id": "r3"})),
        _text("done"),
    ])
    hooks = HookBus()

    async def pre(_event: str, payload: dict) -> HookDecision:
        if payload["tool_use_id"] == "r2":
            return HookDecision(action="modify", replacement_input={"id": "r2x"})
        return HookDecision()

    hooks.on(PRE_TOOL_USE, pre)
    loop = _loop(provider, registry, hooks=hooks)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    assert timeline.ran_before("r1", "r2x")
    assert timeline.ran_before("r2x", "r3")
    assert _result_order(provider) == ["r1", "r2", "r3"]


async def test_doom_loop_guard_counts_every_call_of_a_run() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {})
    same = {"id": "same"}
    provider = ScriptedProvider([
        _calls(*[(f"d{i}", "Probe", same) for i in range(5)]), _text("done"),
    ])
    permissions = PermissionEngine("never_ask")
    noted: list[str] = []
    original = permissions.note_execution

    def note(name: str, input: dict) -> bool:
        noted.append(name)
        return original(name, input)

    permissions.note_execution = note  # type: ignore[method-assign]
    loop = _loop(provider, registry, permissions=permissions)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    assert noted == ["Probe"] * 5
    results = {r.tool_use_id: r for r in _results(loop)}
    assert len(results) == 5
    # The guard trips on a repeat as it does serially (never_ask: in-context
    # denial instead of a prompt); the earlier calls still ran.
    assert any(r.is_error for r in results.values())
    assert _result_order(provider) == [f"d{i}" for i in range(5)]


async def test_steer_rides_exactly_one_result_of_a_run() -> None:
    timeline = Timeline()
    registry = _registry(timeline, {"a": 0.3, "b": 0.3, "c": 0.3})
    provider = ScriptedProvider([
        _calls(("a", "Probe", {"id": "a"}), ("b", "Probe", {"id": "b"}),
               ("c", "Probe", {"id": "c"})),
        _text("done"),
    ])
    loop = _loop(provider, registry)
    await loop.submit(UserTurn(content="go"))
    for call in ("a", "b", "c"):
        await asyncio.wait_for(timeline.started_event(call).wait(), timeout=5)
    await loop.submit(Steer(text="also check the tests", id="s1"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    message = provider.requests[1].messages[-1]
    blocks = [b for b in message.content if isinstance(b, ToolResultBlock)]
    carrying = [b for b in blocks if b.delivered_steers]
    assert len(carrying) == 1
    assert [d.id for d in carrying[0].delivered_steers] == ["s1"]
    assert "also check the tests" in carrying[0].content[0].text


@pytest.mark.parametrize("command, concurrent", [
    ("git status", True),
    ("rm -rf build", False),
])
async def test_bash_joins_a_run_only_when_read_only(command: str, concurrent: bool) -> None:
    from fluid.tools.builtins import bash

    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, bash.handler)
    loop = _loop(ScriptedProvider([_text("x")]), registry)
    tu = {"tool_use_id": "t", "name": "Bash", "input": {"command": command}}
    assert loop._runs_concurrently(tu) is concurrent


async def test_an_inherited_run_slot_is_ignored_by_other_loops_and_calls() -> None:
    """A child engine or task started inside a run member inherits the context
    variable; only the member's own loop and call may act on it."""
    from fluid.engine.native import _RUN_SLOT, _RunSlot, _ToolRun

    timeline = Timeline()
    parent = _loop(ScriptedProvider([_text("x")]), _registry(timeline, {}))
    child = _loop(ScriptedProvider([_text("x")]), _registry(timeline, {}))
    slot = _RunSlot(_ToolRun(2, 2), 0, owner=parent, tool_use_id="t1")
    token = _RUN_SLOT.set(slot)
    try:
        assert parent._run_slot("t1") is slot
        assert parent._run_slot("t2") is None
        assert child._run_slot("t1") is None
    finally:
        _RUN_SLOT.reset(token)
