"""M2 — the tool-using engine loop: round-trip, permissions, deny rules, plan mode.

Uses a fake provider that replays a scripted list of StreamChunks per model
call, so the whole loop runs offline.
"""

import asyncio
import json
from collections.abc import AsyncIterator

import pytest

from fluid.engine.native import NativeLoop
from fluid.events import (
    ErrorEvent,
    Interrupt,
    Interrupted,
    PermissionRequest,
    RespondPermission,
    ToolResult,
    ToolUse,
    TurnComplete,
    Usage,
    UserTurn,
)
from fluid.context.compaction import CompactionConfig
from fluid.context.trim import TrimConfig
from fluid.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from fluid.messages import ToolResultBlock
from fluid.permissions.engine import PermissionEngine
from fluid.permissions.rules import Rule
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolCtx, ToolRegistry


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _tool(
    tuid: str, name: str, args_json: str, *, stop_reason: str = "tool_use"
) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": stop_reason}),
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


def _weather_server():
    @tool("get_weather", "Get weather", {"type": "object", "properties": {"city": {"type": "string"}}})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: sunny"}]}

    return create_inproc_server("wx", [get_weather])


async def _first_event(loop: NativeLoop, sid: str, kind: type):
    holder: dict = {}
    done = asyncio.Event()

    async def watch() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, kind):
                holder["e"] = e
                done.set()
                return

    task = asyncio.create_task(watch())
    return holder, done, task


async def test_tool_round_trip() -> None:
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _text("It is sunny in Paris."),
    ])
    store = MemoryStore()
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="weather in Paris?"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    results = [e for e in events if isinstance(e, ToolResult)]
    assert len(results) == 1
    assert results[0].output == "Paris: sunny"
    assert not results[0].is_error

    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.num_model_calls == 2
    assert done.stop_reason == "end_turn"
    assert provider.calls == 2


async def test_tool_call_reported_with_end_turn_stop_reason_is_dispatched() -> None:
    """llama.cpp, vLLM and Ollama builds send ``finish_reason: stop`` next to
    tool_calls; the call still has to run or the ToolUse is left unanswered."""
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}', stop_reason="end_turn"),
        _text("It is sunny in Paris."),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="weather in Paris?"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    uses = [e for e in events if isinstance(e, ToolUse)]
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [e.tool_use_id for e in uses] == ["t1"]
    assert [e.tool_use_id for e in results] == ["t1"]
    assert results[0].output == "Paris: sunny"
    assert provider.calls == 2
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.num_model_calls == 2
    assert done.stop_reason == "end_turn"


async def test_tool_call_without_a_registry_ends_the_turn() -> None:
    """No registry means nothing can run the call: the turn ends on it."""
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _text("must not be called"),
    ])
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m")
    await loop.submit(UserTurn(content="weather?"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    assert provider.calls == 1
    assert not any(isinstance(e, ToolResult) for e in events)
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.stop_reason == "tool_use"


async def test_explicit_model_call_cap_halts_with_ordered_tool_pairs() -> None:
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city":"Paris"}'),
        _tool("t2", "mcp__wx__get_weather", '{"city":"London"}'),
        _text("must not be called"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"),
        max_model_calls_per_turn=2,
    )
    await loop.submit(UserTurn(content="weather"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    assert provider.calls == 2
    uses = [event for event in events if isinstance(event, ToolUse)]
    results = [event for event in events if isinstance(event, ToolResult)]
    assert [event.tool_use_id for event in uses] == ["t1", "t2"]
    assert [event.tool_use_id for event in results] == ["t1", "t2"]
    assert all(
        next(i for i, event in enumerate(events) if event is use)
        < next(i for i, event in enumerate(events) if event is result)
        for use, result in zip(uses, results)
    )
    done = next(event for event in events if isinstance(event, TurnComplete))
    assert done.stop_reason == "max_calls"


async def test_large_existing_context_does_not_trigger_an_automatic_limit() -> None:
    # This history alone would trip any cumulative-prompt ceiling an embedder
    # might once have imposed. Without an explicitly supplied policy the
    # provider remains the source of truth and the corrective turn is sent.
    provider = ScriptedProvider([_text("x" * 700_000), _text("continued")])
    store = MemoryStore()
    loop = NativeLoop(provider=provider, store=store, model="m")
    await loop.submit(UserTurn(content="same prompt"))
    await loop.wait_turn()
    assert provider.calls == 1

    await loop.submit(UserTurn(content="corrective message"))
    await loop.wait_turn()
    assert provider.calls == 2
    completed = [
        event for event in loop.bus.log(loop.session_id)
        if isinstance(event, TurnComplete)
    ]
    assert completed[-1].num_model_calls == 1
    assert completed[-1].stop_reason == "end_turn"


async def test_turn_has_no_ceiling_below_the_configured_call_cap() -> None:
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([
        *[
            _tool(f"t{i}", "mcp__wx__get_weather", '{"city":"Paris"}')
            for i in range(51)
        ],
        _text("finished after the provider chose to stop"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="keep going until you are done"))
    await loop.wait_turn()

    events = loop.bus.log(loop.session_id)
    assert provider.calls == 52
    assert len([event for event in events if isinstance(event, ToolResult)]) == 51
    done = next(event for event in events if isinstance(event, TurnComplete))
    assert done.num_model_calls == 52
    assert done.stop_reason == "end_turn"


async def test_disabled_compaction_resends_large_tool_results_verbatim() -> None:
    """An embedder that owns its own context policy turns rewriting off and
    gets the stored history back byte-for-byte."""
    large_result = "x" * 500_000

    @tool("blob", "Return a large result", {"type": "object", "properties": {}})
    async def blob(args: dict) -> dict:
        return {"content": [{"type": "text", "text": large_result}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("large", [blob]))
    provider = ScriptedProvider([
        _tool("t-large", "mcp__large__blob", "{}"),
        _text("finished"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"),
        compaction=CompactionConfig(enabled=False, trim=TrimConfig(enabled=False)),
    )
    await loop.submit(UserTurn(content="get the full result"))
    await loop.wait_turn()

    resent_results = [
        block
        for message in provider.requests[1].messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(resent_results) == 1
    assert resent_results[0].content[0].text == large_result


def _text_and_tool(text: str, tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("block_start", {
            "index": 1, "block_type": "tool_use", "tool_name": name,
            "tool_use_id": tuid,
        }),
        StreamChunk("tool_args_delta", {"index": 1, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 1}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


async def test_interrupted_stream_closes_request_and_keeps_request_id_usage() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class HangingProvider:
        name = "openrouter"
        caps = ProviderCaps()
        calls = 0

        async def stream(self, request):
            self.calls += 1
            try:
                yield StreamChunk("provider_request", {
                    "provider_requests": {
                        "logical_call_id": "logical-1",
                        "attempts": [{
                            "attempt": 1, "status": 200,
                            "generation_id": "gen-physical-1",
                        }],
                    }
                })
                yield StreamChunk("message_start", {"usage": {}})
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    provider = HangingProvider()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m")
    await loop.submit(UserTurn(content="perform the requested interrupted provider operation"))
    await asyncio.wait_for(started.wait(), timeout=5)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    assert closed.is_set()
    assert provider.calls == 1
    events = loop.bus.log(loop.session_id)
    usage = next(event for event in events if isinstance(event, Usage))
    assert usage.raw["model_call_index"] == 1
    assert usage.raw["projected_prompt_tokens"] > 0
    assert usage.raw["provider_requests"]["attempts"][0]["generation_id"] == (
        "gen-physical-1"
    )
    assert any(isinstance(event, Interrupted) for event in events)


async def test_direct_close_settles_observed_stream_usage_once() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class HangingProvider:
        name = "openrouter"
        caps = ProviderCaps()

        async def stream(self, request):
            try:
                yield StreamChunk("message_start", {"usage": {"input_tokens": 123}})
                yield StreamChunk("usage", {"usage": {"output_tokens": 7}})
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    store = MemoryStore()
    loop = NativeLoop(provider=HangingProvider(), store=store, model="m")
    await loop.submit(UserTurn(content="hello"))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(loop.aclose(), 5)
    await loop.aclose()
    events = await store.events_since(loop.session_id, 0)
    terminals = [e for e in events if isinstance(e, (Interrupted, TurnComplete, ErrorEvent))]
    assert len(terminals) == 1
    assert terminals[0].message == "engine closed"
    assert terminals[0].usage["input_tokens"] == 123
    assert terminals[0].usage["output_tokens"] == 7
    assert terminals[0].num_model_calls == 1
    assert len([e for e in events if isinstance(e, Usage)]) == 1
    assert closed.is_set()
    assert loop._turn_task.cancelled()


async def test_direct_close_during_tool_cancels_dispatch_and_preserves_usage() -> None:
    started, closed = asyncio.Event(), asyncio.Event()

    @tool("hang", "Wait", {"type": "object"})
    async def hang(args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("test", [hang]))
    store = MemoryStore()
    loop = NativeLoop(
        provider=ScriptedProvider([_tool("c1", "mcp__test__hang", "{}")]),
        store=store, model="m", registry=registry,
        permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(asyncio.gather(loop.aclose(), loop.aclose()), 5)
    events = await store.events_since(loop.session_id, 0)
    terminals = [e for e in events if isinstance(e, (Interrupted, TurnComplete, ErrorEvent))]
    assert len(terminals) == 1
    assert terminals[0].usage["input_tokens"] == 10
    assert terminals[0].usage["output_tokens"] == 5
    assert terminals[0].num_model_calls == 1
    assert len([e for e in events if isinstance(e, Usage)]) == 1
    assert len([e for e in events if isinstance(e, ToolResult)]) == 1
    assert closed.is_set()


async def test_close_during_terminal_persistence_does_not_duplicate_terminal() -> None:
    writing, release = asyncio.Event(), asyncio.Event()

    class SlowStore(MemoryStore):
        async def append_event(self, sid, event):
            if isinstance(event, TurnComplete):
                writing.set()
                await release.wait()
            await super().append_event(sid, event)

    store = SlowStore()
    loop = NativeLoop(provider=ScriptedProvider([_text("done")]), store=store, model="m")
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(writing.wait(), 5)
    close_started = asyncio.Event()
    original_close = loop._close_background_lifecycle

    async def observe_close():
        close_started.set()
        await original_close()

    loop._close_background_lifecycle = observe_close
    # Race a task cancellation with close while durable publication holds the
    # lifecycle lock. Closing alone waits on that lock before cancelling.
    loop._turn_task.cancel()
    closing = asyncio.create_task(loop.aclose())
    await asyncio.wait_for(close_started.wait(), 5)
    assert loop._turn_task.cancelling()
    release.set()
    await asyncio.wait_for(closing, 5)
    events = await store.events_since(loop.session_id, 0)
    assert len([e for e in events if isinstance(e, (Interrupted, TurnComplete, ErrorEvent))]) == 1
    assert len([e for e in events if isinstance(e, Usage)]) == 1


async def test_shell_file_write_is_not_blocked_by_model_prose_heuristics(tmp_path) -> None:
    from fluid.tools.builtins import register_builtins

    marker = tmp_path / "must-not-exist.txt"
    registry = ToolRegistry()
    register_builtins(registry)
    command = f'echo bad > "{marker}"'
    provider = ScriptedProvider([
        _text_and_tool(
            "I will now write the file.", "t-shell", "Bash",
            json.dumps({"command": command}),
        ),
        _text("I am continuing with another inspection."),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="create the file"))
    await loop.wait_turn()

    assert marker.read_text().strip() == "bad"
    events = loop.bus.log(loop.session_id)
    result = next(
        event for event in events
        if isinstance(event, ToolResult) and event.tool_use_id == "t-shell"
    )
    assert not result.is_error
    done = next(event for event in events if isinstance(event, TurnComplete))
    assert done.stop_reason == "end_turn"


async def test_interrupt_cancels_in_flight_tool_dispatch() -> None:
    """An Interrupt must not wait for a long-running tool: the in-flight
    dispatch is cancelled, the call surfaces as an is_error ToolResult (the
    transcript stays well-formed), and the turn ends with Interrupted without
    another model call."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @tool("hang", "Hang until cancelled", {"type": "object", "properties": {}})
    async def hang(args: dict) -> dict:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"content": [{"type": "text", "text": "never"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("m", [hang]))
    provider = ScriptedProvider([
        _tool("t1", "mcp__m__hang", "{}"),
        _text("should never be requested"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(started.wait(), timeout=5)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    events = loop.bus.log(loop.session_id)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error
    assert "interrupted" in result.output
    assert cancelled.is_set()
    assert any(isinstance(e, Interrupted) for e in events)
    assert provider.calls == 1  # no model call after the interrupt


async def test_dispatch_stamps_tool_use_id_on_ctx() -> None:
    """During each dispatch the engine exposes the call's tool_use_id via
    ctx.extra — host dispatchers (the Task tool) correlate their UI work with
    the launching tool_use block through it — and clears it afterwards."""
    from fluid.tools.registry import ToolOutcome, ToolSpec

    seen: dict = {}
    ctx = ToolCtx()

    async def peek(input: dict, c: ToolCtx) -> ToolOutcome:
        seen["tool_use_id"] = (c.extra or {}).get("tool_use_id")
        return ToolOutcome(text="ok")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(name="Peek", description="peek", input_schema={"type": "object"}),
        peek,
    )
    provider = ScriptedProvider([
        _tool("tu-42", "Peek", "{}"),
        _text("done"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, permissions=PermissionEngine("never_ask"), ctx=ctx,
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    assert seen["tool_use_id"] == "tu-42"
    assert "tool_use_id" not in ctx.extra  # cleared after the dispatch


async def test_permission_ask_allow_and_deny() -> None:
    @tool("touch", "Touch a marker", {"type": "object", "properties": {}}, risk="write")
    async def touch(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "touched"}]}

    for decision, expect_error, expect_text in [("allow", False, "touched"), ("deny", True, "permission denied")]:
        registry = ToolRegistry()
        registry.register_inproc(create_inproc_server("m", [touch]))
        provider = ScriptedProvider([
            _tool("t1", "mcp__m__touch", "{}"),
            _text("done"),
        ])
        store = MemoryStore()
        sid = (await store.create_session()).session_id
        loop = NativeLoop(
            provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
            registry=registry, permissions=PermissionEngine("on_write"),
        )
        holder, got, watcher = await _first_event(loop, sid, PermissionRequest)
        await loop.submit(UserTurn(content="go"))
        await asyncio.wait_for(got.wait(), timeout=5)
        req: PermissionRequest = holder["e"]
        assert req.name == "mcp__m__touch"
        assert req.risk == "write"
        await loop.submit(RespondPermission(request_id=req.request_id, decision=decision))
        await loop.wait_turn()
        await watcher

        result = next(e for e in loop.bus.log(sid) if isinstance(e, ToolResult))
        assert result.is_error is expect_error
        assert expect_text in result.output


async def test_deny_rule_blocks_bash(tmp_path) -> None:
    from fluid.tools.builtins import register_builtins

    marker = tmp_path / "marker.txt"
    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        _tool("t1", "Bash", json.dumps({"command": f"touch {marker}"})),
        _text("blocked"),
    ])
    store = MemoryStore()
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5",
        registry=registry, ctx=ToolCtx(cwd=str(tmp_path)),
        permissions=PermissionEngine("never_ask", rules=[Rule(tool="Bash", decision="deny")]),
    )
    await loop.submit(UserTurn(content="run it"))
    await loop.wait_turn()

    result = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult))
    assert result.is_error
    assert "permission denied" in result.output
    assert not marker.exists()  # the command never ran


async def test_pre_tool_use_hook_deny_short_circuits() -> None:
    @tool("danger", "Dangerous", {"type": "object", "properties": {}}, risk="write")
    async def danger(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "ran"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("d", [danger]))
    hooks = HookBus()

    async def block(event: str, payload: dict):
        return HookDecision(action="deny", note="not allowed")

    hooks.on(PRE_TOOL_USE, block)
    provider = ScriptedProvider([_tool("t1", "mcp__d__danger", "{}"), _text("ok")])
    store = MemoryStore()
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5",
        registry=registry, hooks=hooks, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()
    result = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult))
    assert result.is_error
    assert "blocked by hook: not allowed" in result.output


# ─── per-turn cost reporting ─────────────────────────────────────────────────


def _with_cost(script: list[StreamChunk], cost: float) -> list[StreamChunk]:
    """Copy a script, adding a provider-reported cost to its usage chunk."""
    out: list[StreamChunk] = []
    for chunk in script:
        if chunk.kind == "usage":
            payload = dict(chunk.payload)
            payload["usage"] = {**payload["usage"], "cost_usd": cost}
            out.append(StreamChunk("usage", payload))
        else:
            out.append(chunk)
    return out


class EstimatingProvider(ScriptedProvider):
    """Provider with no reported cost but the optional estimate hook."""

    def estimate_cost(self, model: str, usage: dict) -> float | None:
        return 0.5


async def _run_two_call_turn(
    provider: ScriptedProvider, model: str = "claude-sonnet-5"
) -> TurnComplete:
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=model,
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="weather in Paris?"))
    await loop.wait_turn()
    done = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete))
    assert done.num_model_calls == 2
    return done


async def test_turn_cost_sums_provider_reported_costs() -> None:
    provider = ScriptedProvider([
        _with_cost(_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'), 0.002),
        _with_cost(_text("It is sunny in Paris."), 0.003),
    ])
    done = await _run_two_call_turn(provider)
    assert done.cost_usd == pytest.approx(0.005)
    assert "cost_usd" not in done.usage


async def test_turn_cost_falls_back_to_provider_estimate() -> None:
    provider = EstimatingProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _text("It is sunny in Paris."),
    ])
    done = await _run_two_call_turn(provider)
    assert done.cost_usd == pytest.approx(1.0)
    assert "cost_usd" not in done.usage


async def test_turn_cost_is_none_without_cost_or_estimator() -> None:
    """Nothing can price this call: the provider reports no cost, exposes no
    estimator, and the model id is in no pricing table. The cost stays absent
    rather than becoming a guess."""
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _text("It is sunny in Paris."),
    ])
    done = await _run_two_call_turn(provider, model="mystery-model-1")
    assert done.cost_usd is None


async def test_turn_cost_falls_back_to_the_cross_provider_table() -> None:
    """A provider with no estimator still prices a model fluid knows — the
    Grok/GLM gap that recorded whole sessions as free."""
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _text("It is sunny in Paris."),
    ])
    done = await _run_two_call_turn(provider, model="claude-sonnet-5")
    # 10 input + 5 output tokens per call at Sonnet 5's $2/$10 per MTok.
    assert done.cost_usd == pytest.approx(2 * (10 * 2.0 + 5 * 10.0) / 1_000_000)


# ─── partial cost on turn-ending Interrupted / ErrorEvent ────────────────────
#
# These turns end without a TurnComplete (they stay open-ended), so the spend
# they already incurred has to ride the terminating event or it is lost.


def _error(message: str, usage: dict) -> list[StreamChunk]:
    """A call that streams some usage and then fails."""
    return [
        StreamChunk("message_start", {"usage": usage}),
        StreamChunk("error", {"message": message, "retriable": False}),
    ]


async def _interrupt_during_dispatch(provider: ScriptedProvider) -> Interrupted:
    """Run a turn whose single model call requests a tool that hangs, then
    interrupt it mid-dispatch, and return the Interrupted event."""
    started = asyncio.Event()

    @tool("hang", "Hang until cancelled", {"type": "object", "properties": {}})
    async def hang(args: dict) -> dict:
        started.set()
        await asyncio.sleep(3600)
        return {"content": [{"type": "text", "text": "never"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("m", [hang]))
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(started.wait(), timeout=5)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, TurnComplete) for e in events)  # turn stays open-ended
    assert provider.calls == 1
    return next(e for e in events if isinstance(e, Interrupted))


async def test_interrupted_turn_reports_accumulated_cost() -> None:
    provider = ScriptedProvider([
        _with_cost(_tool("t1", "mcp__m__hang", "{}"), 0.002),
        _text("should never be requested"),
    ])
    ev = await _interrupt_during_dispatch(provider)
    assert ev.cost_usd == pytest.approx(0.002)
    assert "cost_usd" not in ev.usage
    assert ev.usage["input_tokens"] == 10
    assert ev.usage["output_tokens"] == 5


async def test_interrupted_turn_falls_back_to_provider_estimate() -> None:
    provider = EstimatingProvider([
        _tool("t1", "mcp__m__hang", "{}"),
        _text("should never be requested"),
    ])
    ev = await _interrupt_during_dispatch(provider)
    assert ev.cost_usd == pytest.approx(0.5)
    assert "cost_usd" not in ev.usage


async def test_provider_error_turn_reports_accumulated_cost() -> None:
    """The costed first call plus the failed call's partial tokens both land on
    the ErrorEvent that ends the turn."""
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([
        _with_cost(_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'), 0.002),
        _error("upstream exploded", {"input_tokens": 7}),
    ])
    loop = NativeLoop(
        # An unpriced model id, so the only cost in play is the one the
        # provider reported — this test is about accumulation, not pricing.
        provider=provider, store=MemoryStore(), model="mystery-model-1",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="weather in Paris?"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, TurnComplete) for e in events)  # turn stays open-ended
    err = next(e for e in events if isinstance(e, ErrorEvent))
    assert "upstream exploded" in err.message
    assert err.cost_usd == pytest.approx(0.002)
    assert "cost_usd" not in err.usage
    assert err.usage["input_tokens"] == 17  # 10 from the good call + 7 from the failed one
    assert err.usage["output_tokens"] == 5


def test_interrupted_usage_survives_the_durable_codec() -> None:
    from fluid.events.model import event_from_dict, event_to_dict

    ev = Interrupted(
        session_id="s1", turn_id="t1", node_id="n1",
        usage={"input_tokens": 10, "output_tokens": 5}, cost_usd=0.002,
        num_model_calls=3,
    )
    assert event_from_dict(event_to_dict(ev)) == ev


# ─── partial model-call count on turn-ending Interrupted / ErrorEvent ────────
#
# Only TurnComplete used to carry num_model_calls, so a turn the user stopped
# booked zero calls downstream however many it had actually paid for.


async def test_interrupted_turn_reports_partial_model_calls() -> None:
    """Two model calls in, the tool hangs and the user stops the turn: the
    Interrupted event books both calls."""
    started = asyncio.Event()

    @tool("hang", "Hang until cancelled", {"type": "object", "properties": {}})
    async def hang(args: dict) -> dict:
        started.set()
        await asyncio.sleep(3600)
        return {"content": [{"type": "text", "text": "never"}]}

    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    registry.register_inproc(create_inproc_server("m", [hang]))
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _tool("t2", "mcp__m__hang", "{}"),
        _text("should never be requested"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(started.wait(), timeout=5)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, TurnComplete) for e in events)
    assert provider.calls == 2
    ev = next(e for e in events if isinstance(e, Interrupted))
    assert ev.num_model_calls == 2


async def test_interrupt_during_model_call_reports_partial_model_calls() -> None:
    """The other interrupt path — the stop lands while the model call is still
    streaming, so the call itself comes back marked interrupted."""
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())

    class _StopAfterFirstCall(ScriptedProvider):
        def __init__(self, scripts: list[list[StreamChunk]], loop_ref: dict) -> None:
            super().__init__(scripts)
            self.loop_ref = loop_ref

        async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
            async for chunk in super().stream(request):
                yield chunk
            # Press stop as the call finishes streaming. Setting the engine's
            # flag directly (rather than submitting Interrupt) keeps the timing
            # deterministic: the op queue runs on its own task.
            self.loop_ref["loop"]._interrupted.set()

    ref: dict = {}
    provider = _StopAfterFirstCall(
        [_tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}')], ref
    )
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    ref["loop"] = loop
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, TurnComplete) for e in events)
    # The call itself was marked interrupted, so the loop stopped before
    # dispatching the tool it asked for — a different emission point from the
    # interrupt-during-dispatch test above.
    assert not any(isinstance(e, ToolResult) for e in events)
    ev = next(e for e in events if isinstance(e, Interrupted))
    assert ev.num_model_calls == 1


async def test_provider_error_turn_reports_partial_model_calls() -> None:
    """The failed call counts too: one good call plus the one that exploded."""
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([
        _tool("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _error("upstream exploded", {"input_tokens": 7}),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="weather in Paris?"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    err = next(
        e for e in loop.bus.log(loop.session_id) if isinstance(e, ErrorEvent)
    )
    assert err.num_model_calls == 2


def test_legacy_terminal_events_without_call_counts_still_parse() -> None:
    """Events persisted before the field existed decode with it unset."""
    from fluid.events.model import event_from_dict

    legacy_interrupt = {
        "_cls": "Interrupted", "session_id": "s1", "node_id": "n1",
        "turn_id": "t1", "seq": 4, "message": "interrupted",
        "usage": {"input_tokens": 10}, "cost_usd": 0.002,
    }
    legacy_error = {
        "_cls": "ErrorEvent", "session_id": "s1", "node_id": "n1",
        "turn_id": "t1", "seq": 5, "message": "boom", "retriable": False,
        "usage": {"input_tokens": 7}, "cost_usd": None,
    }
    assert event_from_dict(legacy_interrupt).num_model_calls is None
    assert event_from_dict(legacy_error).num_model_calls is None


def _cut_off(text: str) -> list[StreamChunk]:
    chunks = _text(text)
    chunks[4] = StreamChunk(
        "usage", {"usage": {"output_tokens": 5}, "stop_reason": "max_tokens"}
    )
    return chunks


async def test_max_tokens_stop_continues_the_turn() -> None:
    provider = ScriptedProvider([_cut_off("half a plan"), _text("...and the rest.")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=ToolRegistry(), permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    done = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete))
    assert done.stop_reason == "end_turn"
    assert done.num_model_calls == 2
    # The second call sees the cut-off reply and a continuation nudge after it.
    history = provider.requests[1].messages
    assert history[-1].role == "user"
    assert "cut off" in history[-1].content[0].text
    assert history[-2].role == "assistant"


async def test_max_tokens_continuations_are_bounded() -> None:
    provider = ScriptedProvider([_cut_off("never ends")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="claude-sonnet-5",
        registry=ToolRegistry(), permissions=PermissionEngine("never_ask"),
        max_continuations_per_turn=2,
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    done = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete))
    assert done.stop_reason == "max_tokens"
    assert done.num_model_calls == 3
