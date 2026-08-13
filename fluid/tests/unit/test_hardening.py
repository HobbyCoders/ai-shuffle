"""Hardening fixes: Responses reasoning round-trip, interrupt/doom-loop
permission behavior, sticky allow_always, usage summing, trim estimator +
context-overflow recovery, client retry, ToolSearch, [mcp] config.
"""

import asyncio
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from omni.config.loader import load_config
from omni.context.trim import ToolResultTrimmer, TrimConfig, estimate_tokens
from omni.engine.native import NativeLoop
from omni.events import (
    ErrorEvent,
    Interrupt,
    Interrupted,
    PermissionRequest,
    RespondPermission,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from omni.messages import Message, Reasoning, Text, ToolUseBlock
from omni.permissions.engine import PermissionEngine
from omni.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from omni.store.base import MemoryStore, SessionNode
from omni.tools.inproc import create_inproc_server, tool
from omni.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

GOLDEN = Path(__file__).parent.parent / "golden" / "anthropic_text_thinking_tool.sse"


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _tool(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
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


def _echo_server():
    @tool("echo", "Echo", {"type": "object", "properties": {"city": {"type": "string"}}})
    async def echo(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"echo:{args.get('city', '')}"}]}

    return create_inproc_server("m", [echo])


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


# ─── 1. OpenAI Responses reasoning round-trip through the engine ─────────────


class ResponsesScriptedProvider(ScriptedProvider):
    name = "openai_responses"
    caps = ProviderCaps(stateful_reasoning=True)


def _responses_tool_call() -> list[StreamChunk]:
    """Reasoning item + function_call whose args arrive ONLY on the done item."""
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("block_start", {"index": 0, "block_type": "thinking", "block": {}}),
        StreamChunk("thinking_delta", {"index": 0, "text": "pondering"}),
        StreamChunk("block_stop", {"index": 0, "block": {
            "id": "rs_1", "type": "reasoning", "encrypted_content": "BLOB", "content": [],
        }}),
        StreamChunk("block_start", {"index": 1, "block_type": "tool_use",
                                    "tool_name": "mcp__m__echo", "tool_use_id": "call_1"}),
        StreamChunk("block_stop", {"index": 1, "block": {
            "id": "fc_1", "type": "function_call", "call_id": "call_1",
            "name": "mcp__m__echo", "arguments": '{"city": "Nice"}',
        }}),
        StreamChunk("usage", {"usage": {}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


async def test_responses_reasoning_and_args_round_trip() -> None:
    registry = ToolRegistry()
    registry.register_inproc(_echo_server())
    provider = ResponsesScriptedProvider([_responses_tool_call(), _text("done")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="gpt",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()

    # The done item's arguments were used even though no deltas streamed.
    result = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult))
    assert result.output == "echo:Nice"

    # The second call's history carries a provider-tagged Reasoning envelope
    # with the encrypted blob, and the serializer resends it.
    assistant = next(
        m for m in provider.requests[1].messages
        if m.role == "assistant" and any(isinstance(b, Reasoning) for b in m.content)
    )
    reasoning = next(b for b in assistant.content if isinstance(b, Reasoning))
    assert reasoning.origin == "openai_responses"
    assert reasoning.encrypted_content == "BLOB"
    assert reasoning.raw and reasoning.raw["type"] == "reasoning"

    tool_use = next(b for b in assistant.content if isinstance(b, ToolUseBlock))
    assert tool_use.input == {"city": "Nice"}

    from omni.providers.openai_responses.serialize import build_request_body

    items = build_request_body(provider.requests[1])["input"]
    types = [it["type"] for it in items]
    assert "reasoning" in types  # the pairing the Codex backend requires
    assert types.index("reasoning") < types.index("function_call")


async def test_anthropic_reasoning_origin_unchanged() -> None:
    thinking = [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("block_start", {"index": 0, "block_type": "thinking"}),
        StreamChunk("thinking_delta", {"index": 0, "text": "hmm"}),
        StreamChunk("signature_delta", {"index": 0, "signature": "SIG"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("block_start", {"index": 1, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 1, "text": "answer"}),
        StreamChunk("block_stop", {"index": 1}),
        StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]
    provider = ScriptedProvider([thinking])
    store = MemoryStore()
    loop = NativeLoop(provider=provider, store=store, model="m")
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()
    nodes = await store.load_path(loop.session_id)
    reasoning = next(
        b for n in nodes for b in n.message.content if isinstance(b, Reasoning)
    )
    assert reasoning.origin == "anthropic"
    assert reasoning.signature == "SIG"
    assert reasoning.raw == {"type": "thinking", "thinking": "hmm", "signature": "SIG"}


# ─── 2. Interrupt unparks a pending permission; doom-loop under never_ask ────


async def test_interrupt_unparks_pending_permission() -> None:
    @tool("touch", "Touch", {"type": "object", "properties": {}}, risk="write")
    async def touch(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "touched"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("m", [touch]))
    provider = ScriptedProvider([_tool("t1", "mcp__m__touch", "{}"), _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=registry, permissions=PermissionEngine("on_write"),
    )
    holder, got, watcher = await _first_event(loop, sid, PermissionRequest)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(got.wait(), timeout=5)
    await loop.submit(Interrupt())
    await asyncio.wait_for(loop.wait_turn(), timeout=5)  # must not hang
    await watcher

    events = loop.bus.log(sid)
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error and "interrupted" in result.output
    assert any(isinstance(e, Interrupted) for e in events)
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_doom_loop_denies_in_context_under_never_ask() -> None:
    same = _tool("t", "mcp__m__echo", '{"city": "X"}')
    provider = ScriptedProvider([same, same, same, _text("moving on")])
    registry = ToolRegistry()
    registry.register_inproc(_echo_server())
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)  # must not park

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, PermissionRequest) for e in events)
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.is_error for r in results] == [False, False, True]
    assert "doom-loop guard" in results[2].output
    assert any(isinstance(e, TurnComplete) for e in events)


# ─── 8. allow_always is sticky ───────────────────────────────────────────────


async def test_allow_always_skips_future_prompts() -> None:
    @tool("touch", "Touch", {"type": "object", "properties": {}}, risk="write")
    async def touch(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "touched"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("m", [touch]))
    provider = ScriptedProvider([
        _tool("t1", "mcp__m__touch", '{"a": 1}'),
        _tool("t2", "mcp__m__touch", '{"a": 2}'),
        _text("done"),
    ])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=registry, permissions=PermissionEngine("on_write"),
    )
    holder, got, watcher = await _first_event(loop, sid, PermissionRequest)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(got.wait(), timeout=5)
    await loop.submit(RespondPermission(
        request_id=holder["e"].request_id, decision="allow_always"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await watcher

    events = loop.bus.log(sid)
    prompts = [e for e in events if isinstance(e, PermissionRequest)]
    assert len(prompts) == 1  # the second call ran without asking
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.is_error for r in results] == [False, False]


# ─── 7. usage sums across the turn's model calls ─────────────────────────────


async def test_turn_usage_sums_across_calls() -> None:
    registry = ToolRegistry()
    registry.register_inproc(_echo_server())
    provider = ScriptedProvider([
        _tool("t1", "mcp__m__echo", '{"city": "Paris"}'),
        _text("done"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()
    done = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete))
    assert done.num_model_calls == 2
    assert done.usage["input_tokens"] == 20   # 10 + 10, summed (was max = 10)
    assert done.usage["output_tokens"] == 10  # 5 + 5


# ─── 5. trim estimator counts tool_use inputs; overflow recovery ─────────────


def test_estimate_counts_tool_use_inputs() -> None:
    big_input = {"file_path": "/x", "content": "y" * 40_000}
    node = SessionNode(
        node_id="n1", parent_id=None,
        message=Message(role="assistant", content=[
            ToolUseBlock(id="t1", name="Write", input=big_input),
        ]),
    )
    assert estimate_tokens([node]) > 40_000 // 4  # input JSON is counted


def test_force_trim_ignores_trigger() -> None:
    from omni.messages import ToolResultBlock

    trimmer = ToolResultTrimmer(config=TrimConfig(
        trigger_tokens=10_000_000, keep_recent_tool_results=0, min_result_chars=100))
    nodes = [
        SessionNode(node_id=f"n{i}", parent_id=None, message=Message(
            role="user",
            content=[ToolResultBlock(tool_use_id=f"t{i}", content=[Text("x" * 5000)])],
        ))
        for i in range(2)
    ]
    assert trimmer.apply(nodes) == [n.message for n in nodes]  # under trigger
    assert trimmer.force_trim(nodes) is True
    texts = [
        "".join(getattr(c, "text", "") for c in b.content)
        for m in trimmer.apply(nodes) for b in m.content
    ]
    assert all("trimmed to save context" in t for t in texts)
    assert trimmer.force_trim(nodes) is False  # nothing left to trim


def _error(message: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("error", {"message": message, "retriable": False}),
    ]


async def test_context_overflow_force_trims_and_retries_once() -> None:
    registry = ToolRegistry()
    registry.register_inproc(_echo_server())
    big = {"content": [{"type": "text", "text": "B" * 5000}]}

    @tool("blob", "Blob", {"type": "object", "properties": {}})
    async def blob(args: dict) -> dict:
        return big

    registry.register_inproc(create_inproc_server("b", [blob]))
    provider = ScriptedProvider([
        _tool("t1", "mcp__b__blob", "{}"),
        _error("prompt is too long: 210000 tokens > 200000 maximum"),
        _text("recovered"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m",
        registry=registry, permissions=PermissionEngine("never_ask"),
        trim=TrimConfig(trigger_tokens=10_000_000,  # normal trigger never fires
                        keep_recent_tool_results=0, min_result_chars=100),
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, ErrorEvent) for e in events)  # recovered silently
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.stop_reason == "end_turn"
    assert provider.calls == 3
    # The retried request saw the trimmed view.
    from omni.messages import ToolResultBlock
    retry_texts = [
        "".join(getattr(c, "text", "") for c in b.content)
        for m in provider.requests[2].messages
        for b in m.content if isinstance(b, ToolResultBlock)
    ]
    assert any("trimmed to save context" in t for t in retry_texts)


async def test_non_overflow_error_still_surfaces() -> None:
    provider = ScriptedProvider([_error("upstream exploded")])
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="m")
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()
    err = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, ErrorEvent))
    assert "upstream exploded" in err.message


# ─── 4. client retry/backoff ─────────────────────────────────────────────────


async def test_anthropic_client_retries_retriable_status() -> None:
    from omni.providers._retry import RetryPolicy
    from omni.providers.anthropic.client import AnthropicClient

    statuses = [529, 429, 200]
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(seen), len(statuses) - 1)]
        seen.append(status)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "overloaded"}})
        return httpx.Response(
            200, content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AnthropicClient(
        api_key="k", http_client=http,
        retry=RetryPolicy(max_retries=3, base_delay=0.001),
    )
    request = ProviderRequest(model="m", messages=[Message(role="user", content=[Text("hi")])])
    chunks = [c async for c in client.stream(request)]
    await http.aclose()
    assert seen == [529, 429, 200]
    assert not any(c.kind == "error" for c in chunks)
    assert chunks[-1].kind == "message_stop"


async def test_anthropic_client_gives_up_after_max_retries() -> None:
    from omni.providers._retry import RetryPolicy
    from omni.providers.anthropic.client import AnthropicClient

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(529, json={"error": {"message": "overloaded"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AnthropicClient(
        api_key="k", http_client=http,
        retry=RetryPolicy(max_retries=2, base_delay=0.001),
    )
    request = ProviderRequest(model="m", messages=[Message(role="user", content=[Text("hi")])])
    chunks = [c async for c in client.stream(request)]
    await http.aclose()
    assert calls["n"] == 3  # initial + 2 retries
    assert chunks[-1].kind == "error"
    assert chunks[-1].payload["retriable"] is True


# ─── 6. ToolSearch builtin + [mcp] config ────────────────────────────────────


def _registry_with_deferred() -> ToolRegistry:
    from omni.tools.registry import _Entry

    reg = ToolRegistry()
    spec = ToolSpec(
        name="mcp__srv__hello", description="say hello to someone",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        source="mcp",
    )
    reg._entries[spec.name] = _Entry(spec=spec, server="srv")  # known, unloaded
    return reg


async def test_tool_search_keyword_loads_schema() -> None:
    from omni.tools.builtins.tool_search import handler

    reg = _registry_with_deferred()
    assert reg.list_tools(loaded_only=True) == []
    ctx = ToolCtx(extra={"tool_registry": reg})
    out = await handler({"query": "hello"}, ctx)
    assert not out.is_error
    assert "mcp__srv__hello" in out.text and "input_schema" in out.text
    assert [s.name for s in reg.list_tools(loaded_only=True)] == ["mcp__srv__hello"]


async def test_tool_search_select_and_unknown() -> None:
    from omni.tools.builtins.tool_search import handler

    reg = _registry_with_deferred()
    ctx = ToolCtx(extra={"tool_registry": reg})
    out = await handler({"query": "select:mcp__srv__hello"}, ctx)
    assert not out.is_error
    assert [s.name for s in reg.list_tools(loaded_only=True)] == ["mcp__srv__hello"]

    missing = await handler({"query": "select:nope"}, ctx)
    assert missing.is_error and "nope" in missing.text


async def test_engine_injects_registry_and_connects_config_mcp() -> None:
    registry = ToolRegistry()
    provider = ScriptedProvider([_text("hi")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        mcp_servers={"bad": {"transport": "stdio", "command": "/nonexistent-cmd"}},
    )
    assert loop.ctx.extra["tool_registry"] is registry
    await loop.submit(UserTurn(content="go"))
    await loop.wait_turn()
    # A server that fails to connect is non-fatal: turn completes, error surfaced.
    events = loop.bus.log(loop.session_id)
    assert any(isinstance(e, TurnComplete) for e in events)
    assert any(
        isinstance(e, ErrorEvent) and "failed to connect" in e.message for e in events
    )
    await loop.aclose()


def test_config_mcp_section_round_trips(tmp_path) -> None:
    servers = {"wx": {"transport": "stdio", "command": "wx-server", "args": ["--x"]}}
    config = load_config(
        cwd=tmp_path, overrides={"mcp": servers},
        include_system=False, include_user=False,
    )
    assert config.mcp == servers
    assert config.to_dict()["mcp"] == servers


def test_tool_search_registered_as_builtin() -> None:
    from omni.tools.builtins import register_builtins

    reg = ToolRegistry()
    register_builtins(reg)
    spec = reg.get("ToolSearch")
    assert spec is not None and spec.risk == "safe"
