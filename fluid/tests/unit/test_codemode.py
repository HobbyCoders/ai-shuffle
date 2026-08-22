"""M5 — code-mode: subprocess kernel, projection/VFS, gated host calls, truncation.

Exit criteria exercised here:
- a task completes using ≥3 tools via ``run_python`` with only the four-tool schema;
- a destructive host call triggers a ``PermissionRequest`` (allow + deny paths);
- ``code_mode_allowed=False`` forces the JSON path (rejected host-side);
- network / disallowed imports are blocked in the kernel;
- a large tool result stays in the kernel behind a sliceable ``ResultRef``;
- a runaway cell hits the wall-clock timeout, the kernel restarts, and the
  namespace loss is reported.
"""

import asyncio
from typing import AsyncIterator

import pytest

from fluid.codemode.kernel import HostReply
from fluid.codemode.projection import Projection, func_name, stub_source
from fluid.codemode.session import CodeModeSession
from fluid.codemode.subprocess_kernel import KernelConfig, SubprocessKernel
from fluid.engine.native import NativeLoop
from fluid.events import PermissionRequest, RespondPermission, ToolResult, ToolUse, TurnComplete, UserTurn
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec


# ── scripted provider (same shape as test_tool_loop) ──────────────────────

def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _run_python(code: str, tuid: str = "t1") -> list[StreamChunk]:
    import json

    args = json.dumps({"code": code})
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start",
                    {"index": 0, "block_type": "tool_use", "tool_name": "run_python", "tool_use_id": tuid}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args}),
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


def _weather_registry() -> ToolRegistry:
    @tool("get_weather", "Get the weather for a city.",
          {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: 21C sunny"}]}

    @tool("distance", "Distance between two cities in km.",
          {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]})
    async def distance(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "343"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("geo", [get_weather, distance]))
    return registry


# ── projection unit tests (host-side, no subprocess) ──────────────────────

def test_func_name_and_stub_signature() -> None:
    assert func_name("mcp__geo__get_weather") == "get_weather"
    assert func_name("Read") == "Read"

    spec = ToolSpec(
        name="mcp__geo__get_weather", description="Get the weather.\nSecond line.",
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}, "days": {"type": "integer"}},
            "required": ["city"],
        },
        risk="write", source="mcp",
    )
    src = stub_source(spec, "get_weather")
    assert "def get_weather(city: str, days: int | None = None) -> str:" in src
    assert "[risk: write]" in src
    assert "__host_call__('mcp__geo__get_weather', _args)" in src
    # docstring carries the whole description, plus a Parameters block
    assert "Get the weather." in src and "Second line." in src
    assert "city (str, required):" in src and "days (int, optional):" in src


def test_projection_lists_searches_and_docs() -> None:
    proj = Projection(_weather_registry().list_tools(loaded_only=False))
    leaves = proj.leaves()
    assert "get_weather" in leaves and "distance" in leaves
    assert any("weather" in one for _, one in proj.entries())
    assert proj.search("distance")[0][0] == "distance"
    assert "def get_weather(" in proj.docs("get_weather")
    assert proj.docs("nope") is None


# ── kernel unit tests (subprocess) ────────────────────────────────────────

async def _null_host(name: str, kwargs: dict) -> HostReply:  # pragma: no cover
    return HostReply(result="")


async def test_kernel_persistent_namespace_and_value_repr() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        r1 = await kernel.exec("acc = 40\nacc + 2", timeout=10, on_host_call=_null_host)
        assert r1.error is None and r1.value_repr == "42"
        r2 = await kernel.exec("acc * 10", timeout=10, on_host_call=_null_host)
        assert r2.value_repr == "400"  # namespace persisted across calls
    finally:
        await kernel.stop()


async def test_kernel_blocks_network_and_disallowed_imports() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        r_os = await kernel.exec("import os", timeout=10, on_host_call=_null_host)
        assert r_os.error and "blocked" in r_os.error and "os" in r_os.error
        r_net = await kernel.exec("import socket", timeout=10, on_host_call=_null_host)
        assert r_net.error and "blocked" in r_net.error
    finally:
        await kernel.stop()


async def test_kernel_timeout_restarts_and_restores_checkpoint() -> None:
    kernel = SubprocessKernel(KernelConfig())
    await kernel.start()
    try:
        await kernel.exec("keep = 7", timeout=10, on_host_call=_null_host)
        slow = await kernel.exec("while True:\n    pass", timeout=0.4, on_host_call=_null_host)
        assert slow.error and "wall clock" in slow.error
        # checkpointing (default-on) restores the pre-kill namespace data
        assert "restored from last checkpoint" in slow.error
        after = await kernel.exec("keep", timeout=10, on_host_call=_null_host)
        assert after.value_repr == "7"
        ok = await kernel.exec("1 + 1", timeout=10, on_host_call=_null_host)
        assert ok.value_repr == "2"
    finally:
        await kernel.stop()


async def test_kernel_large_result_becomes_sliceable_result_ref() -> None:
    kernel = SubprocessKernel(KernelConfig(ref_threshold=1024))
    await kernel.start()

    async def big_host(name: str, kwargs: dict) -> HostReply:
        return HostReply(result="X" * 5000)

    try:
        await kernel.set_vfs({"blob": (
            "def blob() -> str:\n"
            "    return __host_call__('mcp__s__blob', {})\n"
        )})
        # last expression is the raw call → repr is the truncated ResultRef
        r = await kernel.exec(
            "from tools.blob import blob\ndata = blob()\ndata",
            timeout=10, on_host_call=big_host,
        )
        assert r.host_calls == 1
        assert "ResultRef" in r.value_repr and "5000 bytes" in r.value_repr
        assert len(r.value_repr) < 5000  # truncated, not the whole payload
        # the full payload stayed in the kernel and is sliceable
        sliced = await kernel.exec("len(data), data[:3]", timeout=10, on_host_call=big_host)
        assert sliced.value_repr == "(5000, 'XXX')"
    finally:
        await kernel.stop()


# ── full-loop integration (code-mode wired into NativeLoop) ───────────────

async def _watch_first(loop: NativeLoop, sid: str, kind: type):
    holder: dict = {}
    done = asyncio.Event()

    async def watch() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, kind):
                holder["e"] = e
                done.set()
                return

    return holder, done, asyncio.create_task(watch())


async def test_four_tool_schema_and_task_uses_three_tools() -> None:
    registry = _weather_registry()
    session = CodeModeSession(registry)
    code = (
        "from tools.get_weather import get_weather\n"
        "from tools.distance import distance\n"
        "a = get_weather('Paris')\n"
        "b = get_weather('Rome')\n"
        "d = distance('Paris', 'Rome')\n"
        "print(a); print(b); print(d)\n"
    )
    provider = ScriptedProvider([_run_python(code), _text("All gathered.")])
    store = MemoryStore()
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5",
        registry=registry, codemode=session, permissions=PermissionEngine("never_ask"),
    )
    try:
        await loop.submit(UserTurn(content="gather weather + distance"))
        await loop.wait_turn()

        # the model saw ONLY the four-tool code-mode surface
        schema_names = {t["name"] for t in provider.requests[0].tools}
        assert schema_names == {"run_python", "list_tools", "search_tools", "read_tool_docs"}

        events = loop.bus.log(loop.session_id)
        inner = [e for e in events if isinstance(e, ToolResult) and e.name.startswith("mcp__geo__")]
        assert len(inner) == 3  # three gated host calls
        rp = next(e for e in events if isinstance(e, ToolResult) and e.name == "run_python")
        assert "Paris: 21C sunny" in rp.output and "343" in rp.output
        assert not rp.is_error

        inner_uses = [e for e in events if isinstance(e, ToolUse) and e.name.startswith("mcp__geo__")]
        assert len(inner_uses) == 3

        done = next(e for e in events if isinstance(e, TurnComplete))
        assert done.stop_reason == "end_turn"
    finally:
        await loop.aclose()


@pytest.mark.parametrize("decision,expect_error", [("allow", False), ("deny", True)])
async def test_destructive_host_call_triggers_permission_request(decision, expect_error) -> None:
    @tool("wipe", "Delete a record.", {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, risk="destructive")
    async def wipe(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"wiped {args['id']}"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("db", [wipe]))
    session = CodeModeSession(registry)
    code = "from tools.wipe import wipe\ntry:\n    print(wipe('u1'))\nexcept Exception as e:\n    print('DENIED', e)\n"
    provider = ScriptedProvider([_run_python(code), _text("ok")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5", session_id=sid,
        registry=registry, codemode=session, permissions=PermissionEngine("on_write"),
    )
    holder, got, watcher = await _watch_first(loop, sid, PermissionRequest)
    try:
        await loop.submit(UserTurn(content="wipe u1"))
        await asyncio.wait_for(got.wait(), timeout=15)
        req: PermissionRequest = holder["e"]
        assert req.name == "mcp__db__wipe" and req.risk == "destructive"
        await loop.submit(RespondPermission(request_id=req.request_id, decision=decision))
        await loop.wait_turn()
        await watcher

        rp = next(e for e in loop.bus.log(sid) if isinstance(e, ToolResult) and e.name == "run_python")
        if expect_error:
            assert "DENIED" in rp.output  # the kernel caught the raised PermissionError
        else:
            assert "wiped u1" in rp.output
    finally:
        await loop.aclose()


class _CtxProbeLoop:
    """Minimal ``loop`` surface CodeModeSession.run_python reads."""

    def __init__(self, registry: ToolRegistry, ctx: ToolCtx) -> None:
        self.registry = registry
        self.ctx = ctx
        self.hooks = None
        self.permissions = PermissionEngine("never_ask")
        self.events: list = []

    async def _emit(self, event) -> None:
        self.events.append(event)

    async def _await_permission(self, *args, **kwargs):  # pragma: no cover - never asked
        return "allow", None, None


async def test_inner_host_call_uses_its_own_tool_use_id_and_restores() -> None:
    seen: dict = {}

    async def _probe(_input: dict, ctx: ToolCtx) -> ToolOutcome:
        seen["during"] = ctx.extra.get("tool_use_id")
        return ToolOutcome(text="probed")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(name="probe", description="Record the ambient tool_use_id.",
                 input_schema={"type": "object", "properties": {}}),
        _probe,
    )
    session = CodeModeSession(registry)
    code = "from tools.probe import probe\nprint(probe())\n"

    # _dispatch_tool stamps run_python's OWN id before the dispatch; the inner
    # call must override it and hand it back.
    ctx = ToolCtx(extra={"tool_use_id": "outer-run-python"})
    loop = _CtxProbeLoop(registry, ctx)
    try:
        outcome = await session.run_python(
            sid="s1", turn_id="t1", input={"code": code, "timeout": 20}, loop=loop
        )
        assert "probed" in outcome.text

        inner_use = next(e for e in loop.events if isinstance(e, ToolUse) and e.name == "probe")
        assert seen["during"] == inner_use.tool_use_id
        assert seen["during"] != "outer-run-python"
        # the outer id is back in place once the host call returns
        assert ctx.extra["tool_use_id"] == "outer-run-python"

        # with no prior value the key is popped, not left behind
        ctx.extra.pop("tool_use_id")
        await session.run_python(
            sid="s1", turn_id="t2", input={"code": code, "timeout": 20}, loop=loop
        )
        assert "tool_use_id" not in ctx.extra
    finally:
        await session.aclose()


async def test_code_mode_disallowed_tool_is_rejected_host_side() -> None:
    @tool("transfer", "Move funds.", {"type": "object", "properties": {"amt": {"type": "integer"}}, "required": ["amt"]})
    async def transfer(args: dict) -> dict:  # pragma: no cover - must never run
        return {"content": [{"type": "text", "text": "moved"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("bank", [transfer]))
    # force the auditable JSON path
    entry = registry._entries["mcp__bank__transfer"]
    registry._entries["mcp__bank__transfer"] = type(entry)(
        spec=ToolSpec(**{**entry.spec.__dict__, "code_mode_allowed": False}),
        handler=entry.handler, server=entry.server,
    )
    session = CodeModeSession(registry)
    code = "from tools.transfer import transfer\ntry:\n    print(transfer(100))\nexcept Exception as e:\n    print('BLOCKED', e)\n"
    provider = ScriptedProvider([_run_python(code), _text("ok")])
    store = MemoryStore()
    loop = NativeLoop(
        provider=provider, store=store, model="claude-sonnet-5",
        registry=registry, codemode=session, permissions=PermissionEngine("never_ask"),
    )
    try:
        await loop.submit(UserTurn(content="move funds"))
        await loop.wait_turn()
        rp = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, ToolResult) and e.name == "run_python")
        assert "BLOCKED" in rp.output and "not callable from code-mode" in rp.output
        # and the stub docs advertise the JSON-path-only restriction
        assert "JSON-path only" in session.render_docs("transfer")
    finally:
        await loop.aclose()
