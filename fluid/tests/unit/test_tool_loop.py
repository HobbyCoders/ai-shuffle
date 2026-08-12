"""M2 — the tool-using engine loop: round-trip, permissions, deny rules, plan mode.

Uses a fake provider that replays a scripted list of StreamChunks per model
call, so the whole loop runs offline.
"""

import asyncio
from typing import AsyncIterator

from loom.engine.native import NativeLoop
from loom.events import PermissionRequest, RespondPermission, ToolResult, TurnComplete, UserTurn
from loom.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from loom.permissions.engine import PermissionEngine
from loom.permissions.rules import Rule
from loom.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from loom.store.base import MemoryStore
from loom.tools.inproc import create_inproc_server, tool
from loom.tools.registry import ToolCtx, ToolRegistry


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
    from loom.tools.builtins import register_builtins

    marker = tmp_path / "marker.txt"
    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([
        _tool("t1", "Bash", '{"command": "touch %s"}' % marker),
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


def test_plan_mode_filters_write_tools() -> None:
    from loom.tools.builtins import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    provider = ScriptedProvider([_text("hi")])
    store = MemoryStore()
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5",
        registry=registry, plan_mode=True,
    )
    request = loop._build_request([])
    names = {t["name"] for t in request.tools}
    assert "Read" in names and "Grep" in names
    assert "Bash" not in names and "Edit" not in names and "Write" not in names
    # third cache breakpoint reserved once tool defs are present
    assert request.cache_breakpoints == 3
