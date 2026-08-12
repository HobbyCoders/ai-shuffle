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
import sys
from pathlib import Path

import pytest

from omni.engine.base import EngineCaps
from omni.engine.delegated import DelegatedLoop
from omni.engine.delegated_proto import StreamJsonMapper, stop_reason_from_subtype
from omni.events import (
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
from omni.messages import Text
from omni.store.base import MemoryStore
from omni.tools.inproc import create_inproc_server, tool
from omni.tools.mcp.bundled_server import (
    bundled_server_config,
    serve_registry_socket,
    write_bundled_config,
)
from omni.tools.registry import ToolCtx, ToolRegistry

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
        assert loop.cli_version == "claude 2.0.0 (fake-omni)"
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
    assert perm.name == "mcp__omni_bundled__get_weather"
    tool_use = next(e for e in events if isinstance(e, ToolUse))
    assert tool_use.name == "mcp__omni_bundled__get_weather"
    result = next(e for e in events if isinstance(e, ToolResult))
    assert result.name == "mcp__omni_bundled__get_weather"  # resolved via tool_use_id map
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
    assert "omni_bundled" in servers and "other" in servers
    bundled = servers["omni_bundled"]
    assert bundled == bundled_server_config(sock)
    assert bundled["command"] == sys.executable
    assert "-m" in bundled["args"] and "omni.tools.mcp.bundled_server" in bundled["args"]
    assert sock in bundled["args"]


def test_bundled_config_omits_server_without_tools(tmp_path) -> None:
    reg = ToolRegistry()  # no inproc/builtin tools
    path = tmp_path / "mcp.json"
    config = write_bundled_config(reg, str(path), socket_path=str(tmp_path / "b.sock"))
    assert config["mcpServers"] == {}


async def test_bundled_socket_dispatch(tmp_path) -> None:
    """Parent socket side: list + call against the live registry."""
    reg = _weather_registry()
    sock = str(tmp_path / "b.sock")
    server = await serve_registry_socket(reg, sock)
    try:
        reader, writer = await asyncio.open_unix_connection(sock)
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
    finally:
        server.close()
        await server.wait_closed()


async def test_bundled_server_end_to_end_via_mcp(tmp_path) -> None:
    """Full bridge: an MCP client connects to the child, which proxies over the
    socket to the parent's live registry."""
    parent_reg = _weather_registry()
    sock = str(tmp_path / "b.sock")
    server = await serve_registry_socket(parent_reg, sock)

    client_reg = ToolRegistry()
    try:
        config = {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "omni.tools.mcp.bundled_server", "--socket", sock],
            "env": dict(os.environ),
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
        server.close()
        await server.wait_closed()
