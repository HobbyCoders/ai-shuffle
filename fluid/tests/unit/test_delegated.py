"""M3 — the delegated ``claude`` CLI backend, driven against ``fake_claude.py``.

No real ``claude`` binary exists here, so every test runs the fake (which honors
the stream-json contract). Covers: pure frame→Event mapping (golden fixture),
capability matrix, simple text turn (event-shape + gapless seq parity with
NativeLoop), the permission control-frame round-trip (allow + deny), ``--resume``,
partial-streaming dedupe, store persistence, and the bundled MCP server bridge.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from fluid.engine.base import EngineCaps
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import StreamJsonMapper, stop_reason_from_subtype
from fluid.events import (
    BlockStart,
    BlockStop,
    ErrorEvent,
    PermissionRequest,
    RespondPermission,
    TextDelta,
    ThinkingDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    TurnStarted,
    Usage,
    UserTurn,
)
from fluid.messages import Text
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.mcp.bundled_server import (
    _call_result,
    _field_name,
    _parse_address,
    _socket_rpc,
    _tool_from_spec,
    build_child_server,
    bundled_server_config,
    serve_registry_socket,
    write_bundled_config,
)
from fluid.tools.registry import ToolCtx, ToolRegistry

FAKE = str(Path(__file__).parent.parent / "fixtures" / "fake_claude.py")
GOLDEN = Path(__file__).parent.parent / "golden" / "simple_turn.jsonl"


def _cmd() -> list[str]:
    return [sys.executable, FAKE]


def _make_loop(scenario: str, *, store=None, session_id=None, cli_session_id=None,
               partial=False, extra_env=None) -> DelegatedLoop:
    env = {"FAKE_CLAUDE_SCENARIO": scenario}
    if extra_env:
        env.update(extra_env)
    return DelegatedLoop(
        store=store or MemoryStore(),
        model="claude-fake",
        claude_cmd=_cmd(),
        session_id=session_id,
        cli_session_id=cli_session_id,
        partial=partial,
        env=env,
    )


# ─── Pure mapping (delegated_proto) ──────────────────────────────────────────


def test_stop_reason_mapping() -> None:
    assert stop_reason_from_subtype("success") == "end_turn"
    assert stop_reason_from_subtype(None) == "end_turn"
    assert stop_reason_from_subtype("error_max_turns") == "error_max_turns"


def test_golden_frame_mapping() -> None:
    """Feed the golden stream-json fixture through the mapper directly."""
    mapper = StreamJsonMapper(
        session_id="s1", turn_id="t1", model="claude-fake", user_node_id="node_user",
    )
    events = []
    node_msgs = []
    for line in GOLDEN.read_text().splitlines():
        if not line.strip():
            continue
        mapped = mapper.feed(json.loads(line))
        events.extend(mapped.events)
        if mapped.message is not None:
            node_msgs.append(mapped.message)

    assert mapper.cli_session_id == "cli-sess-golden"
    types = [type(e).__name__ for e in events]
    # thinking block, then text block, then usage, then turn-complete
    assert types == [
        "BlockStart", "ThinkingDelta", "BlockStop",   # thinking (index 0)
        "BlockStart", "TextDelta", "BlockStop",       # text (index 1)
        "Usage",
        "TurnComplete",
    ]
    usage = next(e for e in events if isinstance(e, Usage))
    assert usage.cache_read_tokens == 4 and usage.input_tokens == 20
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.stop_reason == "end_turn"
    assert done.cost_usd == 0.001
    assert done.num_model_calls == 1
    # The assistant message assembled for persistence carries both blocks.
    assert len(node_msgs) == 1
    assert node_msgs[0].text() == "Hello from the golden turn."


# ─── Capability matrix ───────────────────────────────────────────────────────


def test_caps_matrix() -> None:
    caps = DelegatedLoop.caps
    assert caps.code_mode is False
    assert caps.inproc_tools is True
    assert caps.schema_plan_mode is False
    assert caps.fork is False
    assert caps.rewind is False
    assert caps.subagents is True
    assert caps.cache_metrics is False
    # Documented divergence from the native loop: no arbitrary-node fork/rewind,
    # no code-mode, no schema plan mode, no cache-breakpoint metrics.
    assert isinstance(caps, EngineCaps)


# ─── Simple text turn ────────────────────────────────────────────────────────


async def test_simple_text_turn() -> None:
    loop = _make_loop("a")
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        sid = loop.session_id
        events = loop.bus.log(sid)

        types = [type(e).__name__ for e in events]
        assert types == [
            "TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
        ]
        # gapless per-session seq
        assert [e.seq for e in events] == list(range(1, len(events) + 1))

        started = events[0]
        assert isinstance(started, TurnStarted)
        assert started.provider == "claude" and started.model == "claude-fake"

        delta = next(e for e in events if isinstance(e, TextDelta))
        assert delta.text == "Hello from fake."
        done = next(e for e in events if isinstance(e, TurnComplete))
        assert done.stop_reason == "end_turn"
        assert done.cost_usd == pytest.approx(0.00042)

        # version guard + resume handle captured
        assert loop.cli_version == "claude 2.0.0 (fake-fluid)"
        assert loop.cli_session_id == "cli-sess-123"

        # nodes persisted: user + assistant, reload-able
        path = await loop.store.load_path(sid)
        assert [n.message.role for n in path] == ["user", "assistant"]
        assert path[-1].message.text() == "Hello from fake."
    finally:
        await loop.aclose()


async def test_store_persistence_reloadable() -> None:
    store = MemoryStore()
    loop = _make_loop("a", store=store)
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        sid = loop.session_id
        # events durably persisted, gapless, same objects the bus emitted
        persisted = await store.events_since(sid, 0)
        assert [type(e).__name__ for e in persisted] == [
            "TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
        ]
        assert [e.seq for e in persisted] == [1, 2, 3, 4, 5, 6]
    finally:
        await loop.aclose()


async def test_stale_between_turn_frames_discarded() -> None:
    """Frames the CLI emits while no turn is active (e.g. a scheduled wake)
    must be drained at the next turn's start — a stale buffered ``result``
    frame must never terminate a fresh turn early."""
    loop = _make_loop("e")
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        await asyncio.sleep(0.2)  # let the unsolicited burst reach the reader
        await loop.submit(UserTurn(content="again"))
        await loop.wait_turn()
        sid = loop.session_id
        events = loop.bus.log(sid)

        assert not any(isinstance(e, ErrorEvent) for e in events)
        # both turns ran to completion with the real reply, not the wake text
        assert [e.text for e in events if isinstance(e, TextDelta)] == [
            "Hello from fake.", "Hello from fake.",
        ]
        assert [type(e).__name__ for e in events] == [
            "TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
        ] * 2
        assert [e.seq for e in events] == list(range(1, len(events) + 1))

        path = await loop.store.load_path(sid)
        assert [n.message.role for n in path] == ["user", "assistant", "user", "assistant"]
        assert all("WAKE" not in n.message.text() for n in path)
    finally:
        await loop.aclose()


# ─── Permission round-trip (allow + deny) ────────────────────────────────────


async def _drive_permission(scenario_decision: str):
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("b", store=store, session_id=sid)

    async def responder() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, PermissionRequest):
                await loop.submit(RespondPermission(request_id=e.request_id, decision=scenario_decision))
                return

    watcher = asyncio.create_task(responder())
    try:
        await loop.submit(UserTurn(content="weather in Paris?"))
        await loop.wait_turn()
        await asyncio.wait_for(watcher, timeout=10)
        return loop, loop.bus.log(sid)
    finally:
        await loop.aclose()


async def test_permission_allow() -> None:
    loop, events = await _drive_permission("allow")
    perm = next(e for e in events if isinstance(e, PermissionRequest))
    assert perm.name == "mcp__fluid_bundled__get_weather"
    tool_use = next(e for e in events if isinstance(e, ToolUse))
    assert tool_use.name == "mcp__fluid_bundled__get_weather"
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.name == "mcp__fluid_bundled__get_weather"  # resolved via tool_use_id map
    assert result.output == "Paris: sunny, 21C" and not result.is_error
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)


async def test_permission_deny() -> None:
    loop, events = await _drive_permission("deny")
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.is_error
    assert "denied" in result.output
    assert any(isinstance(e, TurnComplete) for e in events)


# ─── Resume ──────────────────────────────────────────────────────────────────


def test_resume_argv() -> None:
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=_cmd(),
        cli_session_id="sess-xyz",
    )
    argv = loop._build_argv()
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-xyz"


async def test_resume_second_loop() -> None:
    # First loop establishes a CLI session id.
    store1 = MemoryStore()
    loop1 = _make_loop("a", store=store1)
    try:
        await loop1.submit(UserTurn(content="hi"))
        await loop1.wait_turn()
        sid = loop1.cli_session_id
        assert sid == "cli-sess-123"
    finally:
        await loop1.aclose()

    # Second loop resumes it; the fake asserts --resume matches, so a mismatch
    # would close the stream and surface an ErrorEvent instead of a clean turn.
    loop2 = _make_loop("c", cli_session_id=sid, extra_env={"FAKE_CLAUDE_EXPECT_RESUME": sid})
    try:
        await loop2.submit(UserTurn(content="continue"))
        await loop2.wait_turn()
        events = loop2.bus.log(loop2.session_id)
        assert any(isinstance(e, TurnComplete) for e in events)
        assert not any(isinstance(e, ErrorEvent) for e in events)
    finally:
        await loop2.aclose()


async def test_cli_session_id_persisted_to_store() -> None:
    """The --resume handle must outlive the engine object, or a restart resumes
    the CLI with an fluid session id it has never issued."""
    store = MemoryStore()
    loop = _make_loop("a", store=store)
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        sid = loop.session_id
    finally:
        await loop.aclose()  # drains the fire-and-forget write

    meta = await store.get_session(sid)
    assert meta is not None and meta.cli_session_id == "cli-sess-123"


async def test_cli_session_id_survives_jsonl_reload(tmp_path) -> None:
    """Same handle, read back through a cold JsonlStore (serialization path)."""
    store = JsonlStore(tmp_path)
    loop = _make_loop("a", store=store)
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        sid = loop.session_id
    finally:
        await loop.aclose()

    reloaded = await JsonlStore(tmp_path).get_session(sid)
    assert reloaded is not None and reloaded.cli_session_id == "cli-sess-123"

    # A later handle overwrites the earlier record on replay.
    await store.set_cli_session_id(sid, "cli-sess-999")
    assert (await JsonlStore(tmp_path).get_session(sid)).cli_session_id == "cli-sess-999"


async def test_store_without_setter_does_not_break_a_turn() -> None:
    """Persistence is best-effort: a store predating set_cli_session_id (or one
    whose write fails) must not fail the turn."""

    class LegacyStore(MemoryStore):
        set_cli_session_id = None  # type: ignore[assignment]

    class FailingStore(MemoryStore):
        async def set_cli_session_id(self, session_id, cli_session_id):
            raise RuntimeError("boom")

    for store in (LegacyStore(), FailingStore()):
        loop = _make_loop("a", store=store)
        try:
            await loop.submit(UserTurn(content="hi"))
            await loop.wait_turn()
            events = loop.bus.log(loop.session_id)
            assert any(isinstance(e, TurnComplete) for e in events)
            assert not any(isinstance(e, ErrorEvent) for e in events)
            assert loop.cli_session_id == "cli-sess-123"
        finally:
            await loop.aclose()


# ─── Partial streaming dedupe ────────────────────────────────────────────────


async def test_partial_streaming_dedupe() -> None:
    loop = _make_loop("d", partial=True)
    try:
        assert "--include-partial-messages" in loop._build_argv()
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        sid = loop.session_id
        events = loop.bus.log(sid)

        text_deltas = [e for e in events if isinstance(e, TextDelta)]
        # exactly the two streamed fragments — the whole-block re-emission from
        # the final assistant frame is suppressed.
        assert [d.text for d in text_deltas] == ["Hello ", "world."]
        assert sum(1 for e in events if isinstance(e, BlockStart)) == 1
        assert sum(1 for e in events if isinstance(e, BlockStop)) == 1
        assert sum(1 for e in events if isinstance(e, Usage)) == 1  # once, from assistant frame
        assert [e.seq for e in events] == list(range(1, len(events) + 1))

        # persisted assistant node still holds the complete text
        path = await loop.store.load_path(sid)
        assert path[-1].message.text() == "Hello world."
    finally:
        await loop.aclose()


# ─── Bundled MCP server ──────────────────────────────────────────────────────


def _weather_registry() -> ToolRegistry:
    @tool("get_weather", "Get weather", {"type": "object", "properties": {"city": {"type": "string"}}})
    async def get_weather(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"{args['city']}: sunny"}]}

    reg = ToolRegistry()
    reg.register_inproc(create_inproc_server("wx", [get_weather]))
    return reg


def test_write_bundled_config(tmp_path) -> None:
    reg = _weather_registry()
    path = tmp_path / "mcp.json"
    sock = str(tmp_path / "b.sock")
    config = write_bundled_config(reg, str(path), socket_path=sock,
                                  user_servers={"other": {"type": "stdio", "command": "x"}})
    on_disk = json.loads(path.read_text())
    assert on_disk == config
    servers = config["mcpServers"]
    assert "fluid_bundled" in servers and "other" in servers
    bundled = servers["fluid_bundled"]
    assert bundled == bundled_server_config(sock)
    assert bundled["command"] == sys.executable
    assert "-m" in bundled["args"] and "fluid.tools.mcp.bundled_server" in bundled["args"]
    assert sock in bundled["args"]


def test_bundled_config_omits_server_without_tools(tmp_path) -> None:
    reg = ToolRegistry()  # no inproc/builtin tools
    path = tmp_path / "mcp.json"
    config = write_bundled_config(reg, str(path), socket_path=str(tmp_path / "b.sock"))
    assert config["mcpServers"] == {}


# ─── Bridge transports (unix socket / loopback TCP) ──────────────────────────

HAS_AF_UNIX = hasattr(socket, "AF_UNIX")
# TCP is exercised everywhere; unix only where the platform has AF_UNIX (i.e.
# not Windows, which is exactly why the TCP transport exists).
TRANSPORTS = ["tcp"] + (["unix"] if HAS_AF_UNIX else [])


@asynccontextmanager
async def _bridge(reg: ToolRegistry, transport: str, tmp_path):
    """Start the parent bridge; yield the *resolved* address a child would dial."""
    address = str(tmp_path / "b.sock") if transport == "unix" else "tcp:127.0.0.1:0"
    server = await serve_registry_socket(reg, address)
    try:
        if transport == "tcp":
            address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield address
    finally:
        server.close()
        await server.wait_closed()


def _child_env() -> dict[str, str]:
    """Env for a spawned bridge child that imports *this* fluid, not whatever a
    global PYTHONPATH happens to point at."""
    import fluid

    root = str(Path(fluid.__file__).resolve().parent.parent)
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    return env


async def _dial(address: str):
    tcp = _parse_address(address)
    if tcp is not None:
        return await asyncio.open_connection(*tcp)
    return await asyncio.open_unix_connection(address)


def test_parse_address() -> None:
    assert _parse_address("tcp:127.0.0.1:8123") == ("127.0.0.1", 8123)
    assert _parse_address("tcp:127.0.0.1:0") == ("127.0.0.1", 0)
    assert _parse_address("/tmp/fluid/b.sock") is None
    assert _parse_address(r"C:\Users\x\b.sock") is None  # a drive letter is not "tcp:"
    for bad in ("tcp:", "tcp:127.0.0.1", "tcp:127.0.0.1:http"):
        with pytest.raises(ValueError):
            _parse_address(bad)


async def test_tcp_bridge_binds_loopback_only(tmp_path) -> None:
    """The TCP transport must never be reachable off-box."""
    reg = _weather_registry()
    server = await serve_registry_socket(reg, "tcp:127.0.0.1:0")
    try:
        hosts = {s.getsockname()[0] for s in server.sockets}
        assert hosts == {"127.0.0.1"}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_bundled_socket_dispatch(transport, tmp_path) -> None:
    """Parent socket side: list + call against the live registry."""
    reg = _weather_registry()
    async with _bridge(reg, transport, tmp_path) as address:
        reader, writer = await _dial(address)
        writer.write(b'{"op": "list"}\n')
        await writer.drain()
        listing = json.loads(await reader.readline())
        assert [t["name"] for t in listing["tools"]] == ["mcp__wx__get_weather"]

        writer.write(json.dumps({"op": "call", "name": "mcp__wx__get_weather",
                                 "arguments": {"city": "Paris"}}).encode() + b"\n")
        await writer.drain()
        result = json.loads(await reader.readline())
        assert result == {"text": "Paris: sunny", "is_error": False}
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_socket_rpc_round_trip(transport, tmp_path) -> None:
    """The child's own RPC helper, one connection per call, over each transport."""
    reg = _weather_registry()
    async with _bridge(reg, transport, tmp_path) as address:
        listing = await _socket_rpc(address, {"op": "list"})
        assert [t["name"] for t in listing["tools"]] == ["mcp__wx__get_weather"]
        assert listing["tools"][0]["input_schema"]["properties"] == {"city": {"type": "string"}}

        called = await _socket_rpc(address, {
            "op": "call", "name": "mcp__wx__get_weather", "arguments": {"city": "Paris"},
        })
        assert called == {"text": "Paris: sunny", "is_error": False}

        assert "error" in await _socket_rpc(address, {"op": "nope"})


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_bundled_child_subprocess_speaks_mcp(transport, tmp_path) -> None:
    """The real child process, driven by the mcp SDK's own stdio client.

    Deliberately bypasses fluid's ``tools.mcp.client`` wrapper: this test is
    about the child bridge itself (transport + the installed SDK's tool API),
    so a wrapper-side incompatibility must not be able to mask it.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    reg = _weather_registry()
    async with _bridge(reg, transport, tmp_path) as address:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "fluid.tools.mcp.bundled_server", "--socket", address],
            env=_child_env(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [t.name for t in listed.tools] == ["mcp__wx__get_weather"]
                called = await session.call_tool("mcp__wx__get_weather", {"city": "Paris"})
                assert [c.text for c in called.content] == ["Paris: sunny"]
                assert called.is_error is False


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_bundled_server_end_to_end_via_mcp(transport, tmp_path) -> None:
    """Full bridge: an MCP client connects to the child, which proxies over the
    socket to the parent's live registry."""
    parent_reg = _weather_registry()
    client_reg = ToolRegistry()
    async with _bridge(parent_reg, transport, tmp_path) as address:
        try:
            config = {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "fluid.tools.mcp.bundled_server", "--socket", address],
                "env": _child_env(),
            }
            specs = await client_reg.connect_mcp("bundled", config)
            assert "mcp__bundled__mcp__wx__get_weather" in {s.name for s in specs}
            out = await client_reg.dispatch(
                "mcp__bundled__mcp__wx__get_weather", {"city": "Paris"}, ToolCtx(),
            )
            assert out.text == "Paris: sunny"
            assert not out.is_error
        finally:
            await client_reg.aclose()


async def test_delegated_start_bundled_server_uses_available_transport(tmp_path) -> None:
    """The loop picks a transport for the platform and writes the *resolved*
    address into the config it hands the CLI."""
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=_cmd(),
        registry=_weather_registry(),
    )
    try:
        await loop._start_bundled_server()
        assert loop._socket_server is not None
        address = json.loads(Path(loop._mcp_config_path).read_text())[
            "mcpServers"]["fluid_bundled"]["args"][-1]
        assert address == loop._socket_path
        if HAS_AF_UNIX:
            assert _parse_address(address) is None
        else:
            host, port = _parse_address(address)
            assert (host, port > 0) == ("127.0.0.1", True)
        # the address written to disk is the one that actually answers
        listing = await _socket_rpc(address, {"op": "list"})
        assert [t["name"] for t in listing["tools"]] == ["mcp__wx__get_weather"]
    finally:
        await loop.aclose()
    assert not os.path.exists(loop._mcp_config_path)


# ─── Child-side mcp SDK compatibility ────────────────────────────────────────


def _declared(model_obj, *candidates: str):
    """Read whichever of ``candidates`` the model actually declares, so these
    assertions do not care which spelling the installed SDK uses."""
    dumped = model_obj.model_dump(by_alias=True)
    declared = [c for c in candidates if c in dumped]
    assert declared, f"none of {candidates} present in {sorted(dumped)}"
    return dumped[declared[0]]


def test_tool_from_spec_uses_installed_schema_field() -> None:
    """mcp's models are extra="allow", so the wrong spelling silently drops the
    schema instead of raising — assert the installed field really got set."""
    import mcp.types as t

    spec = {"name": "wx", "description": "Get weather",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}
    tool_obj = _tool_from_spec(t, spec)
    assert isinstance(tool_obj, t.Tool)
    assert tool_obj.name == "wx"
    assert tool_obj.description == "Get weather"
    assert _declared(tool_obj, "inputSchema", "input_schema") == spec["input_schema"]


def test_tool_from_spec_defaults_missing_schema() -> None:
    import mcp.types as t

    tool_obj = _tool_from_spec(t, {"name": "x"})
    assert _declared(tool_obj, "inputSchema", "input_schema") == {"type": "object"}


def test_call_result_carries_is_error() -> None:
    import mcp.types as t

    ok = _call_result(t, "Paris: sunny", False)
    assert [c.text for c in ok.content] == ["Paris: sunny"]
    assert _declared(ok, "isError", "is_error") is False

    bad = _call_result(t, "boom", True)
    assert _declared(bad, "isError", "is_error") is True


def test_field_name_prefers_declared_spelling() -> None:
    class Camel:
        model_fields = {"inputSchema": object()}

    class Snake:
        model_fields = {"input_schema": object()}

    assert _field_name(Camel, "inputSchema", "input_schema") == "inputSchema"
    assert _field_name(Snake, "inputSchema", "input_schema") == "input_schema"
    assert _field_name(object(), "inputSchema", "input_schema") == "inputSchema"


def test_server_registers_handlers_via_constructor_kwargs() -> None:
    """Pins the API this file targets: mcp 2.0's low-level ``Server`` takes
    ``on_list_tools`` / ``on_call_tool`` constructor kwargs and exposes them by
    method string through ``get_request_handler``. If a future SDK drops
    either, this test is the reminder to revisit ``build_child_server``."""
    from mcp.server import Server

    async def noop(ctx, params):  # pragma: no cover - never invoked
        raise AssertionError

    server = Server("probe", version="0.1.0", on_list_tools=noop, on_call_tool=noop)
    assert server.get_request_handler("tools/list").handler is noop
    assert server.get_request_handler("tools/call").handler is noop
    assert server.get_request_handler("resources/list") is None

    bundled = build_child_server("tcp:127.0.0.1:1")
    assert bundled.get_request_handler("tools/list") is not None
    assert bundled.get_request_handler("tools/call") is not None


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_child_server_handlers_answer_over_bridge(transport, tmp_path) -> None:
    """The child's registered handlers, invoked as the SDK would invoke them,
    against a live parent bridge."""
    import mcp.types as t

    reg = _weather_registry()
    async with _bridge(reg, transport, tmp_path) as address:
        server = build_child_server(address)
        list_entry = server.get_request_handler("tools/list")
        call_entry = server.get_request_handler("tools/call")
        assert list_entry is not None and call_entry is not None

        # The handlers ignore the request context, so pass None where the
        # runner would hand them a ServerRequestContext.
        listed = await list_entry.handler(None, None)
        tools = listed.tools
        assert [tool_obj.name for tool_obj in tools] == ["mcp__wx__get_weather"]
        schema = _declared(tools[0], "inputSchema", "input_schema")
        assert schema["properties"] == {"city": {"type": "string"}}

        result = await call_entry.handler(
            None, t.CallToolRequestParams(name="mcp__wx__get_weather", arguments={"city": "Paris"}),
        )
        assert isinstance(result, t.CallToolResult)
        assert [c.text for c in result.content] == ["Paris: sunny"]
        assert result.is_error is False


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_child_server_propagates_tool_errors(transport, tmp_path) -> None:
    """A failing in-process tool must reach the CLI as isError, not as success."""
    import mcp.types as t

    @tool("boom", "Always fails", {"type": "object"})
    async def boom(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "kaboom"}], "is_error": True}

    reg = ToolRegistry()
    reg.register_inproc(create_inproc_server("bad", [boom]))
    async with _bridge(reg, transport, tmp_path) as address:
        server = build_child_server(address)
        called = await server.get_request_handler("tools/call").handler(
            None, t.CallToolRequestParams(name="mcp__bad__boom", arguments={}),
        )
        assert called.is_error is True
        assert [c.text for c in called.content] == ["kaboom"]
