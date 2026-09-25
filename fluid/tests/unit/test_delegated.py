"""M3 — the delegated ``claude`` CLI backend, driven against ``fake_claude.py``.

No real ``claude`` binary exists here, so every test runs the fake (which honors
the stream-json contract). Covers: pure frame→Event mapping (golden fixture),
capability matrix, simple text turn (event-shape + gapless seq parity with
NativeLoop), the permission control-frame round-trip (allow + deny), mid-turn
steering, ``--resume``, partial-streaming dedupe, store persistence, and the
bundled MCP server bridge.
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
import fluid.engine.delegated as delegated_mod
from fluid.engine.delegated import (
    CLAUDE_CLI_MISSING_MESSAGE,
    ClaudeCliMissingError,
    DelegatedLoop,
    _cli_permission_mode,
    _version_at_least,
)
from fluid.engine.delegated_proto import StreamJsonMapper, stop_reason_from_subtype
from fluid.events import (
    BlockStart,
    BlockStop,
    ContextCompaction,
    DelegatedSurface,
    ErrorEvent,
    PermissionRequest,
    RespondPermission,
    SteerDelivered,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    TurnStarted,
    Usage,
    UserTurn,
)
from fluid.events.model import event_from_dict, event_to_dict
from fluid.events.ops import Interrupt, SetPermissionMode, Steer
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.mcp.bundled_server import (
    _answer,
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


@pytest.mark.parametrize("sources,expected", [(None, None), ((), ""), (("user", "local"), "user,local")])
def test_setting_sources_reaches_cli_even_when_empty(sources, expected):
    loop = DelegatedLoop(store=MemoryStore(), model="fake", setting_sources=sources)
    argv = loop._build_argv()
    if expected is None:
        assert "--setting-sources" not in argv
    else:
        assert argv[argv.index("--setting-sources") + 1] == expected


# ─── CLI version gates ───────────────────────────────────────────────────────


@pytest.mark.parametrize("version,minimum,expected", [
    ("2.1.280 (Claude Code)", "2.1.280", True),
    ("2.1.281 (Claude Code)", "2.1.280", True),
    ("2.1.279 (Claude Code)", "2.1.280", False),
    ("2.1.280", "2.1.99", True),  # numeric, not lexical
    ("10.0.0", "9.9.9", True),
    ("claude 2.0.0 (fake-fluid)", "2.0.0", True),
    ("v2.2.0", "2.1.280", True),
    ("2.1.280-beta.1", "2.1.280", False),  # a pre-release is below its release
    ("2.1.280-beta.1", "2.1.279", True),
    ("2.1.280-beta.2", "2.1.280-beta.1", True),
    ("2.1.280-beta.10", "2.1.280-beta.2", True),  # numeric identifiers
    ("2.1.280-beta", "2.1.280-beta.1", False),  # fewer identifiers sort first
    ("2.1.280-rc.1", "2.1.280-beta.9", True),
    ("2.1.280-1", "2.1.280-alpha", False),  # numeric below alphanumeric
    ("2.1.280+build.5", "2.1.280", True),  # build metadata ignored
    ("2.1.280", "2.1.280+build.5", True),
    (None, "2.1.280", False),
    ("", "2.1.280", False),
    ("Claude Code", "2.1.280", False),
    ("2.1", "2.1.0", False),
    ("2.1.280", "not a version", False),
])
def test_version_at_least(version, minimum, expected) -> None:
    assert _version_at_least(version, minimum) is expected


def test_permission_mode_is_default_for_every_cli_until_the_threshold_is_set() -> None:
    # No CLI is known to reject "default" yet: the switch ships off.
    assert delegated_mod.CLI_MANUAL_PERMISSION_MODE_SINCE is None
    for version in ("2.1.280 (Claude Code)", "99.0.0", None, "garbage"):
        assert _cli_permission_mode("default", version) == "default"


@pytest.mark.parametrize("version,expected", [
    ("3.0.0 (Claude Code)", "manual"),  # at the threshold
    ("3.0.1 (Claude Code)", "manual"),  # above
    ("3.1.0-beta.1", "manual"),  # a pre-release of a later version
    ("2.1.280 (Claude Code)", "default"),  # below
    ("3.0.0-rc.1", "default"),  # a pre-release of the threshold is below it
    (None, "default"),  # version unknown
    ("Claude Code (dev build)", "default"),  # unparsable
])
def test_permission_mode_mapping_around_the_threshold(monkeypatch, caplog, version, expected) -> None:
    monkeypatch.setattr(delegated_mod, "CLI_MANUAL_PERMISSION_MODE_SINCE", "3.0.0")
    with caplog.at_level("INFO", logger=delegated_mod.logger.name):
        assert _cli_permission_mode("default", version) == expected
    assert ("sending permission mode 'default' as 'manual'" in caplog.text) is (expected == "manual")
    # Only "default" is renamed; every other mode goes through as is.
    for mode in ("acceptEdits", "bypassPermissions", "dontAsk", "auto"):
        assert _cli_permission_mode(mode, version) == mode


async def test_permission_mode_mapping_reaches_argv_and_control(monkeypatch) -> None:
    monkeypatch.setattr(delegated_mod, "CLI_MANUAL_PERMISSION_MODE_SINCE", "3.0.0")
    loop = DelegatedLoop(store=MemoryStore(), model="fake", permission_mode="plan")
    sent: list[dict] = []

    async def send_control(request: dict) -> None:
        sent.append(request)

    loop._send_control = send_control  # type: ignore[method-assign]
    for version, wire in (("2.1.280 (Claude Code)", "default"), ("3.0.0 (Claude Code)", "manual")):
        loop.cli_version = version
        argv = loop._build_argv()
        assert argv[argv.index("--permission-mode") + 1] == wire
        await loop.submit(SetPermissionMode(mode="default"))
        assert sent[-1] == {"subtype": "set_permission_mode", "mode": wire}
        # The loop keeps fluid's name; only the wire spelling changes.
        assert loop.permission_mode == "default"


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
    # announced surface (init), thinking block, text block, usage, turn-complete
    assert types == [
        "DelegatedSurface",                           # init frame's tool list
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


# ─── Announced tool surface (init frame) ─────────────────────────────────────


def _mapper() -> StreamJsonMapper:
    return StreamJsonMapper(
        session_id="s1", turn_id="t1", model="claude-fake", user_node_id="node_user",
    )


def test_init_frame_surfaces_the_announced_tools() -> None:
    """The CLI picks its own tool surface; the init frame is where it says so."""
    mapper = _mapper()
    mapped = mapper.feed({
        "type": "system", "subtype": "init", "session_id": "cli-1",
        "tools": ["Read", "Write", "mcp__fluid__Task"],
        "mcp_servers": [{"name": "fluid", "status": "connected"}],
    })

    assert mapper.cli_tools == ("Read", "Write", "mcp__fluid__Task")
    assert mapper.cli_mcp_servers == ({"name": "fluid", "status": "connected"},)

    assert len(mapped.events) == 1
    event = mapped.events[0]
    assert isinstance(event, DelegatedSurface)
    assert event.tools == mapper.cli_tools
    assert event.mcp_servers == mapper.cli_mcp_servers
    assert event.session_id == "s1" and event.turn_id == "t1"


def test_init_frame_without_tools_leaves_the_surface_unknown() -> None:
    """No announcement is not the same as "no tools" — stay None, say nothing."""
    mapper = _mapper()
    mapped = mapper.feed({"type": "system", "subtype": "init", "session_id": "cli-1"})

    assert mapper.cli_tools is None
    assert mapper.cli_mcp_servers is None
    assert mapped.events == []
    assert mapper.cli_session_id == "cli-1"  # the handle is still captured


def test_delegated_surface_round_trips_through_the_codec() -> None:
    event = DelegatedSurface(
        session_id="s1", turn_id="t1",
        tools=("Read", "Bash"),
        mcp_servers=({"name": "fluid", "status": "connected"},),
    )
    assert event_from_dict(event_to_dict(event)) == event
    # Durable stores go through JSON, which brings both tuples back as lists.
    revived = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    assert revived == event


# ─── Capability matrix ───────────────────────────────────────────────────────


def test_caps_matrix() -> None:
    caps = DelegatedLoop.caps
    assert caps.code_mode is False
    assert caps.inproc_tools is True
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
        # DelegatedSurface lands right after TurnStarted: the CLI announces its
        # tool list in the init frame, once per process.
        assert types == [
            "TurnStarted", "DelegatedSurface",
            "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
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
        # ... and the tool surface the CLI announced in the same init frame
        assert loop.cli_tools == ("Read", "Bash")
        assert loop.cli_mcp_servers == ({"name": "fluid", "status": "connected"},)
        surface = next(e for e in events if isinstance(e, DelegatedSurface))
        assert surface.tools == ("Read", "Bash")

        # nodes persisted: user + assistant, reload-able
        path = await loop.store.load_path(sid)
        assert [n.message.role for n in path] == ["user", "assistant"]
        assert path[-1].message.text() == "Hello from fake."
    finally:
        await loop.aclose()


async def test_pid_tracks_the_live_cli_process() -> None:
    loop = _make_loop("a")
    try:
        assert loop.pid is None  # spawned lazily by the first turn
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        assert loop.pid is not None and loop.pid == loop._proc.pid
    finally:
        await loop.aclose()
    assert loop.pid is None


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
            "TurnStarted", "DelegatedSurface",
            "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete",
        ]
        assert [e.seq for e in persisted] == [1, 2, 3, 4, 5, 6, 7]
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
        turn = ["TurnStarted", "BlockStart", "TextDelta", "BlockStop", "Usage", "TurnComplete"]
        # The init frame arrives once, inside the first turn — so does its event.
        assert [type(e).__name__ for e in events] == [
            "TurnStarted", "DelegatedSurface", *turn[1:], *turn,
        ]
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


# ─── Mid-turn steering ───────────────────────────────────────────────────────


def _spy_on_frames(loop: DelegatedLoop) -> list[dict]:
    """Record every frame the loop writes to the CLI's stdin (still writing it)."""
    frames: list[dict] = []
    original = loop._write_frame

    async def spy(frame: dict) -> None:
        frames.append(frame)
        await original(frame)

    loop._write_frame = spy  # type: ignore[method-assign]
    return frames


def _user_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("type") == "user"]


async def test_steer_mid_turn_writes_one_user_frame_and_receipts_it() -> None:
    """A live turn is steered by writing a second ``user`` frame to the CLI's
    stdin; the write is the delivery, so it is receipted immediately."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("b", store=store, session_id=sid)
    frames = _spy_on_frames(loop)

    async def responder() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, PermissionRequest):
                # The turn is parked on a permission here — mid-flight, exactly
                # the window a steer is for.
                await loop.submit(Steer(text="use the staging config", id="q1"))
                await loop.submit(
                    RespondPermission(request_id=e.request_id, decision="allow")
                )
                return

    watcher = asyncio.create_task(responder())
    try:
        await loop.submit(UserTurn(content="weather in Paris?"))
        await loop.wait_turn()
        await asyncio.wait_for(watcher, timeout=10)

        # The turn's own user frame, then exactly one steer frame — no batching,
        # no duplicate, and the steer text is the whole content.
        assert len(_user_frames(frames)) == 2
        assert _user_frames(frames)[1] == {"type": "user", "message": {
            "role": "user",
            "content": [{"type": "text", "text": "use the staging config"}],
        }}

        events = loop.bus.log(sid)
        receipt = next(e for e in events if isinstance(e, SteerDelivered))
        assert receipt.steers == [{"id": "q1", "text": "use the staging config"}]
        started = next(e for e in events if isinstance(e, TurnStarted))
        assert receipt.turn_id == started.turn_id
        assert not any(isinstance(e, ErrorEvent) for e in events)
        # The turn still runs to completion around the injected frame.
        assert any(isinstance(e, TurnComplete) for e in events)
    finally:
        await loop.aclose()


async def test_steer_without_a_running_turn_is_refused() -> None:
    """No live turn means nothing to fold the text into: the caller must send a
    UserTurn instead, and nothing reaches the CLI."""
    loop = _make_loop("a")
    frames = _spy_on_frames(loop)
    with pytest.raises(RuntimeError, match="no turn is running"):
        await loop.submit(Steer(text="too late", id="q1"))
    assert frames == []


async def test_steer_after_an_interrupt_is_refused() -> None:
    """A turn on its way out must not be steered into — the host queues the
    message instead, so nothing is written and nothing is receipted."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("b", store=store, session_id=sid)
    frames = _spy_on_frames(loop)
    refused: list[BaseException] = []

    async def responder() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, PermissionRequest):
                await loop.submit(Interrupt())
                try:
                    await loop.submit(Steer(text="too late", id="q1"))
                except RuntimeError as exc:
                    refused.append(exc)
                # Let the fake finish so the turn does not hang on its parked
                # permission (the fake has no interrupt handling of its own).
                await loop.submit(
                    RespondPermission(request_id=e.request_id, decision="deny")
                )
                return

    watcher = asyncio.create_task(responder())
    try:
        await loop.submit(UserTurn(content="weather in Paris?"))
        await loop.wait_turn()
        await asyncio.wait_for(watcher, timeout=10)

        assert len(refused) == 1 and "interrupt" in str(refused[0])
        assert len(_user_frames(frames)) == 1  # only the turn's own frame
        assert not [e for e in loop.bus.log(sid) if isinstance(e, SteerDelivered)]
    finally:
        await loop.aclose()


async def test_steer_is_no_longer_an_unsupported_op() -> None:
    """Steer left the NotImplementedError set — it now fails (when it fails) as
    a RuntimeError about turn state, while Resume/Fork stay unsupported."""
    from fluid.events.ops import Fork, Resume

    loop = _make_loop("a")
    with pytest.raises(NotImplementedError):
        await loop.submit(Resume())
    with pytest.raises(NotImplementedError):
        await loop.submit(Fork(from_node_id="node_1"))
    with pytest.raises(RuntimeError) as caught:
        await loop.submit(Steer(text="hi"))
    assert not isinstance(caught.value, NotImplementedError)


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


class _BatchingStore(MemoryStore):
    """Records which store hook each event reached; batches nothing itself."""

    def __init__(self) -> None:
        super().__init__()
        self.streamed: list[int] = []
        self.flushes = 0

    async def append_stream_event(self, session_id, event) -> None:
        self.streamed.append(event.seq)
        await self.append_event(session_id, event)

    async def flush_events(self, session_id) -> None:
        self.flushes += 1


async def test_deltas_reach_the_stores_stream_hook_and_turns_flush() -> None:
    store = _BatchingStore()
    loop = _make_loop("s", store=store, partial=True, extra_env={"FAKE_CLAUDE_DELTAS": "6,2"})
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()
    # Only reconstructible deltas take the batchable path; every other event
    # (block boundaries, tool calls, usage, terminals) is a direct write.
    assert store.streamed == [e.seq for e in events if isinstance(e, TextDelta)]
    assert len(store.streamed) == 8
    assert [e.seq for e in await store.events_since(loop.session_id, 0)] == [e.seq for e in events]
    assert store.flushes == 1  # The turn's exit commits whatever is still batched.


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


def _mcp_registers_handlers_by_kwarg() -> bool:
    """True on mcp 2.0, whose low-level ``Server`` takes handler kwargs and
    exposes ``get_request_handler``. An embedder pinned to mcp<2 through its own
    CLI/SDK dependency gets 1.x, so ``build_child_server`` supports both
    generations and the 2.0-only probes below skip on 1.x (the subprocess tests
    still cover it)."""
    import inspect

    from mcp.server import Server

    return "on_list_tools" in inspect.signature(Server.__init__).parameters


requires_mcp2 = pytest.mark.skipif(
    not _mcp_registers_handlers_by_kwarg(), reason="needs mcp>=2.0 Server API"
)


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
                assert _declared(called, "isError", "is_error") is False


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
        listing = await _socket_rpc(address, {"op": "list"}, loop._bridge_token)
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


@requires_mcp2
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


@requires_mcp2
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


@requires_mcp2
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


# ─── Bridge gates: token, session tool filter, permissions, hooks ────────────


async def _rpc_lines(address: str, *reqs: dict) -> list:
    """Send ``reqs`` on one connection; collect one parsed line per request
    (``None`` where the server closed instead of answering)."""
    reader, writer = await _dial(address)
    out = []
    try:
        for req in reqs:
            try:
                writer.write(json.dumps(req).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
            except (ConnectionError, OSError):
                # The server hung up on an earlier request. A unix socket
                # reports that from drain(); TCP loopback buffers the write
                # and returns an empty read. Both mean "no answer".
                line = b""
            out.append(json.loads(line) if line else None)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return out


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_bridge_refuses_requests_without_the_session_token(transport, tmp_path) -> None:
    """A local process that finds the port and speaks the wire format gets an
    error and a closed connection, never a tool result."""
    reg = _weather_registry()
    address = str(tmp_path / "b.sock") if transport == "unix" else "tcp:127.0.0.1:0"
    server = await serve_registry_socket(reg, address, token="s3cret")
    try:
        if transport == "tcp":
            address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
        call = {"op": "call", "name": "mcp__wx__get_weather", "arguments": {"city": "Paris"}}
        missing = await _rpc_lines(address, call, call)
        assert missing[0] == {"error": "unauthorized: bridge token missing or wrong"}
        assert missing[1] is None  # the connection is gone after the refusal
        wrong = await _rpc_lines(address, {**call, "token": "s3cret-not"})
        assert "error" in wrong[0] and "text" not in wrong[0]
        listing = await _rpc_lines(address, {"op": "list", "token": "nope"})
        assert "tools" not in listing[0]
        # The child helper attaches the token and gets through.
        ok = await _socket_rpc(address, call, "s3cret")
        assert ok == {"text": "Paris: sunny", "is_error": False}
    finally:
        server.close()
        await server.wait_closed()


def test_bundled_config_carries_token_in_env_not_args(tmp_path) -> None:
    reg = _weather_registry()
    config = write_bundled_config(
        reg, str(tmp_path / "mcp.json"), socket_path="tcp:127.0.0.1:1", token="tok",
    )
    entry = config["mcpServers"]["fluid_bundled"]
    assert entry["env"] == {"FLUID_BUNDLED_TOKEN": "tok"}
    assert "tok" not in " ".join(entry["args"])


@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_bundled_child_subprocess_authenticates_with_env_token(transport, tmp_path) -> None:
    """The real child reads FLUID_BUNDLED_TOKEN and presents it on every
    request; without it the parent refuses and the CLI would see no tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    reg = _weather_registry()
    address = str(tmp_path / "b.sock") if transport == "unix" else "tcp:127.0.0.1:0"
    server = await serve_registry_socket(reg, address, token="child-token")
    try:
        if transport == "tcp":
            address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
        env = _child_env()
        env["FLUID_BUNDLED_TOKEN"] = "child-token"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "fluid.tools.mcp.bundled_server", "--socket", address],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [t.name for t in listed.tools] == ["mcp__wx__get_weather"]
                called = await session.call_tool("mcp__wx__get_weather", {"city": "Paris"})
                assert [c.text for c in called.content] == ["Paris: sunny"]
    finally:
        server.close()
        await server.wait_closed()


def _builtin_registry() -> ToolRegistry:
    from fluid.tools.builtins import register_builtins

    reg = ToolRegistry()
    register_builtins(reg)
    return reg


async def test_bridge_applies_session_tool_filter_to_list_and_call() -> None:
    """A tool outside the session surface is neither listed nor callable, and
    the filter honours the CLI-facing ``mcp__fluid_bundled__`` spelling."""
    reg = _builtin_registry()
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=_cmd(), registry=reg,
        disallowed_tools=["mcp__fluid_bundled__Bash", "Write"],
    )
    try:
        filt = loop._bridge_tool_permitted
        listing = await _answer(reg, ToolCtx(), {"op": "list"}, tool_filter=filt)
        names = {t["name"] for t in listing["tools"]}
        assert "Bash" not in names and "Write" not in names
        assert "Read" in names
        denied = await _answer(
            reg, ToolCtx(),
            {"op": "call", "name": "Bash", "arguments": {"command": "echo PWNED"}},
            tool_filter=filt,
        )
        assert denied["is_error"] is True
        assert denied["text"] == "tool not available in this session: Bash"
        assert "PWNED" not in denied["text"]
    finally:
        await loop.aclose()

    allow_only = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=_cmd(), registry=reg,
        allowed_tools=["mcp__fluid_bundled__Read", "Glob"], disallowed_tools=["Glob"],
    )
    try:
        assert allow_only._bridge_tool_permitted("Read")  # CLI-facing spelling
        assert not allow_only._bridge_tool_permitted("Glob")  # deny beats allow
        assert not allow_only._bridge_tool_permitted("Bash")
    finally:
        await allow_only.aclose()


async def test_bridge_routes_calls_through_the_permission_engine(tmp_path) -> None:
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    reg = _builtin_registry()
    ctx = ToolCtx(cwd=str(tmp_path))
    engine = PermissionEngine("never_ask", rules=[Rule(tool="Bash", decision="deny")])
    denied = await _answer(
        reg, ctx,
        {"op": "call", "name": "Bash", "arguments": {"command": "echo PWNED"}},
        permissions=engine,
    )
    assert denied["is_error"] is True
    assert "permission denied by a configured rule" in denied["text"]
    assert "PWNED" not in denied["text"]

    # No rule and a policy that would prompt: nobody is listening on this
    # socket, so the prompt becomes the non-interactive denial.
    asking = PermissionEngine("always_ask")
    parked = await _answer(
        reg, ctx,
        {"op": "call", "name": "Bash", "arguments": {"command": "echo PWNED"}},
        permissions=asking,
    )
    assert parked["is_error"] is True
    assert "non-interactive run cannot prompt" in parked["text"]
    assert "PWNED" not in parked["text"]

    # An allowed call still runs.
    (tmp_path / "hello.txt").write_text("hi")
    allowed = await _answer(
        reg, ctx,
        {"op": "call", "name": "Read", "arguments": {"file_path": str(tmp_path / "hello.txt")}},
        permissions=PermissionEngine("never_ask"),
    )
    assert allowed["is_error"] is False and "hi" in allowed["text"]


async def test_bridge_runs_pre_and_post_tool_hooks(tmp_path) -> None:
    from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, HookBus, HookDecision

    reg = _builtin_registry()
    ctx = ToolCtx(cwd=str(tmp_path))
    seen: list[tuple[str, str]] = []
    bus = HookBus()

    async def pre(event: str, payload: dict) -> HookDecision:
        seen.append((event, payload["name"]))
        if payload["name"] == "Bash":
            return HookDecision(action="deny", note="no shells over the bridge")
        return HookDecision()

    async def post(event: str, payload: dict) -> HookDecision:
        seen.append((event, payload["name"]))
        return HookDecision()

    bus.on(PRE_TOOL_USE, pre)
    bus.on(POST_TOOL_USE, post)

    denied = await _answer(
        reg, ctx,
        {"op": "call", "name": "Bash", "arguments": {"command": "echo PWNED"}},
        hooks=bus,
    )
    assert denied == {"text": "blocked by hook: no shells over the bridge", "is_error": True}
    (tmp_path / "hello.txt").write_text("hi")
    allowed = await _answer(
        reg, ctx,
        {"op": "call", "name": "Read", "arguments": {"file_path": str(tmp_path / "hello.txt")}},
        hooks=bus,
    )
    assert allowed["is_error"] is False
    assert seen == [(PRE_TOOL_USE, "Bash"), (PRE_TOOL_USE, "Read"), (POST_TOOL_USE, "Read")]


async def test_delegated_loop_serves_bridge_with_token_and_gates(tmp_path) -> None:
    """End to end through DelegatedLoop: the server it starts refuses an
    unauthenticated caller, and an authenticated one still meets the session
    filter and the permission engine."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    reg = _builtin_registry()
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=_cmd(), registry=reg,
        ctx=ToolCtx(cwd=str(tmp_path)),
        disallowed_tools=["Write"],
        permissions=PermissionEngine("never_ask", rules=[Rule(tool="Bash", decision="deny")]),
    )
    try:
        await loop._start_bundled_server()
        address = loop._socket_path
        config = json.loads(Path(loop._mcp_config_path).read_text())
        token = config["mcpServers"]["fluid_bundled"]["env"]["FLUID_BUNDLED_TOKEN"]
        assert token == loop._bridge_token and len(token) >= 32

        call = {"op": "call", "name": "Bash", "arguments": {"command": "echo PWNED"}}
        anonymous = await _rpc_lines(address, call)
        assert "error" in anonymous[0] and "text" not in anonymous[0]

        listing = await _socket_rpc(address, {"op": "list"}, token)
        assert "Write" not in {t["name"] for t in listing["tools"]}
        gated = await _socket_rpc(address, call, token)
        assert gated["is_error"] is True and "PWNED" not in gated["text"]
        assert "permission denied by a configured rule" in gated["text"]
    finally:
        await loop.aclose()



# ─── Batch 6: the Claude-CLI leg as a daily driver ───────────────────────────


async def _run_one_turn(loop: DelegatedLoop, text: str = "hi") -> list:
    await loop.submit(UserTurn(content=text))
    await loop.wait_turn()
    return loop.bus.log(loop.session_id)


async def test_oversize_tool_result_frame_does_not_kill_the_turn() -> None:
    """ENG-6: a 300 KB tool_result user frame (the CLI echoes whole results)
    is far over asyncio's 64 KiB default line limit; the turn must still
    complete with the result delivered whole."""
    loop = _make_loop("k")
    try:
        events = await _run_one_turn(loop)
        assert not [e for e in events if isinstance(e, ErrorEvent)]
        result = next(e for e in events if isinstance(e, ToolResult))
        assert len(result.output) == 300_000 and result.name == "Read"
        assert isinstance(events[-1], TurnComplete)
        # The next turn on the same process is healthy too: no desync.
        events = await _run_one_turn(loop, "again")
        assert isinstance(events[-1], TurnComplete)
    finally:
        await loop.aclose()


@pytest.mark.parametrize("scenario", ["l", "m"])
async def test_dead_resume_handle_is_dropped_and_the_turn_replayed(scenario) -> None:
    """ENG-15: a --resume handle the CLI rejects (by exiting without frames,
    or with an error result) is cleared, the CLI restarted without it, and
    the same turn replayed once. The session ends the turn with a real
    TurnComplete and the fresh handle persisted, instead of failing forever
    with the stale one."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    await store.set_cli_session_id(sid, "cli-dead")
    loop = _make_loop(scenario, store=store, session_id=sid, cli_session_id="cli-dead")
    try:
        events = await _run_one_turn(loop)
        types = [type(e).__name__ for e in events]
        assert "TurnComplete" in types and "ErrorEvent" not in types
        assert types.count("TurnStarted") == 1  # one turn, replayed inside
        assert loop.cli_session_id == "cli-sess-123"
        # A second turn rides the fresh process without another restart.
        events = await _run_one_turn(loop, "again")
        assert isinstance(events[-1], TurnComplete)
    finally:
        await loop.aclose()
    assert (await store.get_session(sid)).cli_session_id == "cli-sess-123"


async def test_failed_result_becomes_an_error_event_not_a_completion() -> None:
    """ENG-15: ``is_error`` on the result is a failed turn. It is reported as
    an ErrorEvent naming the CLI's errors, never as TurnComplete, and no
    retry happens because the turn had already produced frames."""
    loop = _make_loop("n")
    try:
        events = await _run_one_turn(loop)
        assert not [e for e in events if isinstance(e, TurnComplete)]
        error = events[-1]
        assert isinstance(error, ErrorEvent)
        assert "error_during_execution" in error.message
        assert "tool crashed" in error.message and "stream reset" in error.message
        assert loop.cli_session_id == "cli-sess-123"  # no handle dropped
        # The mapper reports the same thing directly.
        mapper = _mapper()
        mapped = mapper.feed({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "result": "boom",
        })
        assert mapped.is_result and mapped.error == "claude reported error_during_execution: boom"
        assert not [e for e in mapped.events if isinstance(e, TurnComplete)]
    finally:
        await loop.aclose()


async def test_turn_cap_result_still_completes_the_turn() -> None:
    """``error_max_turns`` carries ``is_error`` too, but a cap is a stop
    reason (the native loop's call cap completes the turn the same way)."""
    loop = _make_loop("o")
    try:
        events = await _run_one_turn(loop)
        done = events[-1]
        assert isinstance(done, TurnComplete) and done.stop_reason == "error_max_turns"
    finally:
        await loop.aclose()


async def test_interrupt_while_parked_on_a_permission_ends_the_turn() -> None:
    """The turn's reader parks on the permission future while holding
    stdout; an Interrupt must release it (as a denial) rather than leave the
    CLI and the reader each waiting on the other."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("b", store=store, session_id=sid)

    async def interrupter() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, PermissionRequest):
                await loop.submit(Interrupt())
                return

    from fluid.events.model import Interrupted

    watcher = asyncio.create_task(interrupter())
    try:
        await loop.submit(UserTurn(content="weather?"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
        await watcher
        events = loop.bus.log(sid)
        denied = next(e for e in events if isinstance(e, ToolResult))
        assert denied.is_error
        assert isinstance(events[-1], Interrupted)
    finally:
        await loop.aclose()


async def test_allow_always_sticks_for_the_session_and_is_echoed_to_the_cli() -> None:
    """``allow_always`` used to be answered as a one-shot allow. Now the
    CLI's own ``permission_suggestions`` go back as ``updatedPermissions``
    and the loop remembers the tool, so the next request for it is answered
    without a prompt."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("b", store=store, session_id=sid)
    frames = _spy_on_frames(loop)

    async def responder() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, PermissionRequest):
                await loop.submit(RespondPermission(
                    request_id=e.request_id, decision="allow_always",
                ))
                return

    watcher = asyncio.create_task(responder())
    try:
        await loop.submit(UserTurn(content="weather?"))
        await loop.wait_turn()
        await watcher
        answers = [
            f for f in frames if f.get("type") == "control_response"
        ]
        assert answers and answers[0]["response"]["response"]["behavior"] == "allow"
        assert answers[0]["response"]["response"]["updatedPermissions"] == [{
            "type": "addRules", "behavior": "allow", "destination": "session",
            "rules": [{"toolName": "mcp__fluid_bundled__get_weather"}],
        }]
        first = loop.bus.last_seq(sid)
        # Second turn, same tool: no prompt, the call runs.
        await loop.submit(UserTurn(content="and now?"))
        await loop.wait_turn()
        later = [e for e in loop.bus.log(sid) if e.seq > first]
        assert not [e for e in later if isinstance(e, PermissionRequest)]
        result = next(e for e in later if isinstance(e, ToolResult))
        assert not result.is_error and "sunny" in result.output
    finally:
        await loop.aclose()


async def test_argv_carries_strict_mcp_config_with_the_written_config(tmp_path) -> None:
    """The child connects the servers fluid wrote and none of the user's own:
    --strict-mcp-config rides with --mcp-config, and the config is written
    even when it names no server, so the flag always applies."""
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=_cmd(),
        registry=ToolRegistry(),
        mcp_servers={"docs": {"transport": "streamable_http", "url": "http://x/mcp"}},
    )
    try:
        await loop._start_bundled_server()
        argv = loop._build_argv()
        i = argv.index("--mcp-config")
        assert argv[i + 1] == loop._mcp_config_path
        assert argv[i + 2] == "--strict-mcp-config"
        written = json.loads(Path(loop._mcp_config_path).read_text())["mcpServers"]
        assert written == {"docs": {"type": "http", "url": "http://x/mcp"}}
    finally:
        await loop.aclose()

    empty = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=_cmd(), registry=ToolRegistry(),
    )
    try:
        await empty._start_bundled_server()
        assert empty._socket_server is None  # nothing to serve ...
        assert "--strict-mcp-config" in empty._build_argv()  # ... still strict
        assert json.loads(Path(empty._mcp_config_path).read_text()) == {"mcpServers": {}}
    finally:
        await empty.aclose()


def test_user_servers_are_rewritten_into_the_cli_shape() -> None:
    from fluid.tools.mcp.bundled_server import cli_server_entry

    assert cli_server_entry({"command": "srv", "args": ["-x"]}) == {
        "type": "stdio", "command": "srv", "args": ["-x"],
    }
    assert cli_server_entry({"transport": "http", "url": "u"}) == {"type": "http", "url": "u"}
    assert cli_server_entry({"transport": "streamable_http", "url": "u"}) == {
        "type": "http", "url": "u",
    }
    assert cli_server_entry({"type": "sse", "url": "u"}) == {"type": "sse", "url": "u"}


async def test_failure_before_the_user_frame_still_ends_the_turn() -> None:
    """A failure between the turn's start and its first frame used to sit
    outside the error guard: no terminal event, and events() waited
    forever. Now it ends the turn with an ErrorEvent."""

    class Exploding(MemoryStore):
        async def append(self, session_id, node):  # type: ignore[override]
            raise RuntimeError("disk on fire")

    loop = _make_loop("a", store=Exploding())
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
        events = loop.bus.log(loop.session_id)
        assert isinstance(events[-1], ErrorEvent) and "disk on fire" in events[-1].message
    finally:
        await loop.aclose()


async def test_hook_stop_on_a_bridge_call_ends_the_turn_as_the_native_loop_does() -> None:
    """A hook's ``continue: false`` on a bridge-served tool is reported as
    the turn's terminal ErrorEvent once the CLI's result lands."""
    loop = _make_loop("a")
    try:
        await loop._ensure_session()
        await loop._hook_stop("no more shells")
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)
        assert isinstance(events[-1], ErrorEvent)
        assert events[-1].message == "stopped by hook: no more shells"
        assert events[-1].retriable is False
        assert loop._hook_stop_reason is None
    finally:
        await loop.aclose()


async def test_bridge_honours_hook_permission_decisions_and_stops(tmp_path) -> None:
    from fluid.hooks.bus import POST_TOOL_USE, PRE_TOOL_USE, HookBus, HookDecision
    from fluid.permissions.engine import PermissionEngine

    reg = _builtin_registry()
    ctx = ToolCtx(cwd=str(tmp_path))
    (tmp_path / "hello.txt").write_text("hi")
    read = {"op": "call", "name": "Read", "arguments": {"file_path": str(tmp_path / "hello.txt")}}
    stops: list[str] = []

    async def on_stop(reason: str) -> None:
        stops.append(reason)

    # permissionDecision: allow skips a policy that would otherwise prompt
    # (and, with nobody to prompt, deny).
    bus = HookBus()
    bus.on(PRE_TOOL_USE, lambda _e, _p: _decide(HookDecision(permission_decision="allow")))
    allowed = await _answer(
        reg, ctx, read, permissions=PermissionEngine("always_ask"), hooks=bus,
    )
    assert allowed["is_error"] is False and "hi" in allowed["text"]

    # permissionDecision: ask forces a prompt the bridge cannot show.
    bus = HookBus()
    bus.on(PRE_TOOL_USE, lambda _e, _p: _decide(
        HookDecision(permission_decision="ask", note="double-check reads"),
    ))
    asked = await _answer(
        reg, ctx, read, permissions=PermissionEngine("never_ask"), hooks=bus,
    )
    assert asked["is_error"] is True and "double-check reads" in asked["text"]

    # PreToolUse stop: refused, and the loop is told to end the turn.
    bus = HookBus()
    bus.on(PRE_TOOL_USE, lambda _e, _p: _decide(HookDecision(stop=True, note="enough")))
    stopped = await _answer(reg, ctx, read, hooks=bus, on_stop=on_stop)
    assert stopped == {"text": "blocked by hook: enough", "is_error": True}
    assert stops == ["enough"]

    # PostToolUse deny and additionalContext ride back on the result; a
    # PostToolUse stop ends the turn after it.
    bus = HookBus()
    bus.on(POST_TOOL_USE, lambda _e, _p: _decide(
        HookDecision(action="deny", note="lint failed", additional_context="run ruff"),
    ))
    fed_back = await _answer(reg, ctx, read, hooks=bus)
    assert fed_back["is_error"] is False
    assert "PostToolUse hook feedback:\nlint failed" in fed_back["text"]
    assert "PostToolUse hook context:\nrun ruff" in fed_back["text"]
    bus = HookBus()
    bus.on(POST_TOOL_USE, lambda _e, _p: _decide(HookDecision(stop=True, note="done now")))
    await _answer(reg, ctx, read, hooks=bus, on_stop=on_stop)
    assert stops == ["enough", "done now"]


async def _decide(decision):
    return decision


@pytest.mark.parametrize("transport", ["unix", "tcp"])
async def test_bridge_carries_oversize_arguments_and_results(transport, tmp_path) -> None:
    """A Write of a large file, or a large result, is one JSON line on the
    bridge socket; both sides read past asyncio's default line limit."""
    if transport == "unix" and not hasattr(socket, "AF_UNIX"):
        pytest.skip("no AF_UNIX on this platform")
    reg = ToolRegistry()

    @tool("echo", "echo", {"type": "object", "properties": {"text": {"type": "string"}}})
    async def echo(args: dict) -> dict:
        return {"content": [{"type": "text", "text": args["text"]}]}

    reg.register_inproc(create_inproc_server("big", [echo]))
    address = (
        str(tmp_path / "big.sock") if transport == "unix" else "tcp:127.0.0.1:0"
    )
    server = await serve_registry_socket(reg, address, ToolCtx(), token="t")
    try:
        if transport == "tcp":
            address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
        payload = "y" * 400_000
        reply = await _socket_rpc(
            address, {"op": "call", "name": "mcp__big__echo", "arguments": {"text": payload}}, "t",
        )
        assert reply["is_error"] is False and reply["text"] == payload
    finally:
        server.close()
        await server.wait_closed()


class _Journal:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.closed = False

    def begin_turn(self) -> None:
        self.events.append(("begin",))

    def record_event(self, kind: str, text: str) -> None:
        self.events.append(("event", kind, text))

    def record_tool(self, name: str, input: dict, output: str, is_error: bool) -> None:
        self.events.append(("tool", name, input, output, is_error))

    async def aclose(self) -> None:
        self.closed = True


async def test_delegated_turns_feed_the_continual_journal() -> None:
    """Continual learning on this leg: the user text, every tool result
    (with the input the CLI reported for it) and the final assistant text
    are journaled, and closing the loop closes the journal."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    journal = _Journal()
    loop = DelegatedLoop(
        store=store, model="claude-fake", claude_cmd=_cmd(), session_id=sid,
        env={"FAKE_CLAUDE_SCENARIO": "b"}, journal=journal,
    )

    async def responder() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, PermissionRequest):
                await loop.submit(RespondPermission(request_id=e.request_id, decision="allow"))
                return

    watcher = asyncio.create_task(responder())
    try:
        await loop.submit(UserTurn(content="weather in Paris?"))
        await loop.wait_turn()
        await watcher
    finally:
        await loop.aclose()
    assert journal.events == [
        ("begin",),
        ("event", "user", "weather in Paris?"),
        ("tool", "mcp__fluid_bundled__get_weather", {"city": "Paris"}, "Paris: sunny, 21C", False),
        ("event", "assistant", "It is sunny in Paris."),
    ]
    assert journal.closed


def test_delegated_engine_is_built_with_the_journal_and_recall_wiring(tmp_path) -> None:
    """The delegated leg gets the journal plus Recall/Remember wiring, and a
    synthesizer only when a native provider is named for it."""
    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge
    from fluid.config.model import HarnessConfig

    base = {
        "cwd": str(tmp_path), "continual": True, "skills_dir": str(tmp_path / "skills"),
    }
    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, base))
    engine = build_engine(config, MemoryStore(), provider_name="claude")
    assert isinstance(engine, DelegatedLoop)
    assert engine._journal is not None and engine._journal._synthesizer is None
    assert "recall_store" in engine.ctx.extra and "memory_store" in engine.ctx.extra

    with_provider = HarnessConfig.from_dict(deep_merge(
        BUILTIN_DEFAULTS, {**base, "continual_provider": "anthropic"},
    ))
    engine = build_engine(with_provider, MemoryStore(), provider_name="claude")
    assert engine._journal._synthesizer is not None


# ─── CLI-side compaction (compact_boundary frames) ───────────────────────────


def _compacting_frames() -> list[dict]:
    """A recorded turn the CLI compacts in the middle of.

    Prompt size collapses across the boundary — 166k to 35k tokens, the shape
    of the audited Opus session — and the CLI says so in its own frame rather
    than in the usage numbers.
    """
    return [
        {"type": "system", "subtype": "init", "session_id": "cli-1",
         "tools": ["Read"], "mcp_servers": []},
        {"type": "assistant", "message": {
            "id": "msg_1", "content": [{"type": "text", "text": "Reading everything."}],
            "usage": {"input_tokens": 166397, "output_tokens": 12},
        }},
        {"type": "system", "subtype": "compact_boundary", "session_id": "cli-1",
         "uuid": "uuid-1", "compact_metadata": {
             "trigger": "auto", "pre_tokens": 166397, "post_tokens": 34856,
         }},
        {"type": "assistant", "message": {
            "id": "msg_2", "content": [{"type": "text", "text": "Carrying on."}],
            "usage": {"input_tokens": 34856, "output_tokens": 9},
        }},
        {"type": "result", "subtype": "success", "num_turns": 2,
         "usage": {"input_tokens": 34856, "output_tokens": 9}},
    ]


def test_compact_boundary_frame_becomes_a_context_compaction() -> None:
    mapper = _mapper()
    events = []
    for frame in _compacting_frames():
        events.extend(mapper.feed(frame).events)

    compactions = [e for e in events if isinstance(e, ContextCompaction)]
    assert len(compactions) == 1
    event = compactions[0]
    assert event.reason == "cli_auto_compact"
    assert event.before_tokens == 166397
    assert event.after_tokens == 34856
    assert event.tier == 3
    # The CLI owns the decisions; fluid invents none of them.
    assert event.trimmed == [] and event.spans == []
    assert event.session_id == "s1" and event.turn_id == "t1"


def test_manual_compaction_is_named_as_such() -> None:
    mapper = _mapper()
    mapped = mapper.feed({
        "type": "system", "subtype": "compact_boundary",
        "compact_metadata": {"trigger": "manual", "pre_tokens": 90_000},
    })
    event = mapped.events[0]
    assert isinstance(event, ContextCompaction)
    assert event.reason == "cli_manual_compact"
    assert event.before_tokens == 90_000
    # post_tokens is optional on the wire; an unreported count stays 0.
    assert event.after_tokens == 0


def test_compact_boundary_without_metadata_is_still_reported() -> None:
    """A boundary is a boundary even when the CLI sends no numbers with it."""
    mapper = _mapper()
    mapped = mapper.feed({"type": "system", "subtype": "compact_boundary"})
    event = mapped.events[0]
    assert isinstance(event, ContextCompaction)
    assert event.reason == "cli_compact"
    assert event.before_tokens == 0 and event.after_tokens == 0


def test_compaction_event_round_trips_through_the_codec() -> None:
    event = ContextCompaction(
        session_id="s1", turn_id="t1", tier=3, reason="cli_auto_compact",
        before_tokens=166397, after_tokens=34856,
    )
    assert event_from_dict(event_to_dict(event)) == event
    revived = event_from_dict(json.loads(json.dumps(event_to_dict(event))))
    assert revived == event


async def test_the_cli_lane_reports_its_compaction_to_the_host() -> None:
    """End to end: the boundary reaches the bus and the store, so a host asking
    for the latest compaction no longer reads a Claude-lane session as one that
    never compacted."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("p", store=store, session_id=sid)
    try:
        await loop.submit(UserTurn(content="read the repo"))
        await loop.wait_turn()

        events = loop.bus.log(sid)
        compactions = [e for e in events if isinstance(e, ContextCompaction)]
        assert len(compactions) == 1
        assert compactions[0].reason == "cli_auto_compact"
        assert compactions[0].before_tokens == 166397
        assert compactions[0].after_tokens == 34856
        assert compactions[0].turn_id == next(
            e.turn_id for e in events if isinstance(e, TurnStarted)
        )
        # It lands between the two model messages, not after the turn.
        order = [type(e).__name__ for e in events]
        assert order.index("ContextCompaction") < order.index("TurnComplete")
        assert any(isinstance(e, ContextCompaction) for e in await store.events_since(sid, 0))
        assert not any(isinstance(e, ErrorEvent) for e in events)
    finally:
        await loop.aclose()


# ─── Harness notices do not borrow the user's marker ─────────────────────────


async def test_a_system_origin_steer_reaches_the_cli_in_the_notice_marker() -> None:
    from fluid.context.steering import NOTICE_OPEN, STEER_OPEN

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _make_loop("b", store=store, session_id=sid)
    frames = _spy_on_frames(loop)

    async def responder() -> None:
        async for e in loop.bus.subscribe(sid):
            if isinstance(e, PermissionRequest):
                await loop.submit(Steer(
                    text="<task-notification>build finished</task-notification>",
                    id="n1", origin="system",
                ))
                await loop.submit(
                    RespondPermission(request_id=e.request_id, decision="allow")
                )
                return

    watcher = asyncio.create_task(responder())
    try:
        await loop.submit(UserTurn(content="weather in Paris?"))
        await loop.wait_turn()
        await asyncio.wait_for(watcher, timeout=10)

        injected = _user_frames(frames)[1]["message"]["content"][0]["text"]
        assert NOTICE_OPEN in injected
        assert STEER_OPEN not in injected  # emphatically not the user's marker
        assert "build finished" in injected

        receipt = next(
            e for e in loop.bus.log(sid) if isinstance(e, SteerDelivered)
        )
        assert receipt.steers[0]["origin"] == "system"
    finally:
        await loop.aclose()


# ─── Missing CLI ─────────────────────────────────────────────────────────────


async def test_missing_cli_raises_typed_setup_error() -> None:
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake",
        claude_cmd=["/definitely/missing/claude"],
    )
    with pytest.raises(ClaudeCliMissingError) as caught:
        await loop._version_guard()
    assert str(caught.value) == CLAUDE_CLI_MISSING_MESSAGE
    await loop.aclose()


async def test_missing_cli_turn_reports_setup_message() -> None:
    """A turn against a missing binary ends with the setup message as its
    error event, not a ``spawn failed: FileNotFoundError(...)`` repr."""
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake",
        claude_cmd=["/definitely/missing/claude"],
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
        errors = [e for e in loop.bus.log(loop.session_id) if isinstance(e, ErrorEvent)]
        assert [e.message for e in errors] == [CLAUDE_CLI_MISSING_MESSAGE]
    finally:
        await loop.aclose()


# ─── Requests the CLI blocks on are always answered ──────────────────────────


def test_permission_grants_join_allowed_tools_without_narrowing_the_bridge() -> None:
    loop = DelegatedLoop(
        store=MemoryStore(), model="fake",
        permission_grants=["mcp__fluid_bundled__mcp__builtins__Task", "mcp__browser"],
    )
    argv = loop._build_argv()
    assert argv[argv.index("--allowed-tools") + 1] == (
        "mcp__fluid_bundled__mcp__builtins__Task,mcp__browser"
    )
    # A grant is not a surface: every bridge tool is still served.
    assert loop._bridge_tool_permitted("mcp__builtins__Remember")

    surfaced = DelegatedLoop(
        store=MemoryStore(), model="fake", allowed_tools=["Read"], permission_grants=["mcp__browser"],
    )
    argv = surfaced._build_argv()
    assert argv[argv.index("--allowed-tools") + 1] == "Read,mcp__browser"
    assert not surfaced._bridge_tool_permitted("mcp__builtins__Remember")


def test_an_unknown_control_request_is_mapped_for_an_answer() -> None:
    mapped = _mapper().feed({
        "type": "control_request", "request_id": "cli_9",
        "request": {"subtype": "elicitation", "message": "?"},
    })
    assert mapped.unsupported_request == ("cli_9", "elicitation")
    assert mapped.permission is None and not mapped.events


async def test_an_unanswered_permission_prompt_is_denied_after_the_timeout(monkeypatch) -> None:
    from fluid.engine import delegated

    monkeypatch.setattr(delegated, "PERMISSION_TIMEOUT", 0.2)
    loop = _make_loop("b")
    frames = _spy_on_frames(loop)
    try:
        events = await asyncio.wait_for(_run_one_turn(loop, "weather in Paris?"), 10)
        denial = next(
            f["response"]["response"] for f in frames
            if f.get("type") == "control_response" and f["response"]["request_id"] == "cli_req_1"
        )
        assert denial["behavior"] == "deny" and "timed out" in denial["message"]
        assert next(e for e in events if isinstance(e, ToolResult)).is_error
        assert isinstance(events[-1], TurnComplete)
    finally:
        await loop.aclose()


_UNKNOWN_CONTROL_CLI = r'''
import json, sys

if "--version" in sys.argv:
    print("claude 9.9.9 (fake unknown control)")
    sys.exit(0)

SID = "cli-unknown-1"
USAGE = {"input_tokens": 1, "output_tokens": 1}

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line) if line.strip() else {}

while True:
    if read().get("type") != "user":
        continue
    emit({"type": "system", "subtype": "init", "session_id": SID, "model": "m",
          "tools": [], "mcp_servers": []})
    emit({"type": "control_request", "request_id": "cli_x",
          "request": {"subtype": "elicitation", "message": "?"}})
    while True:
        reply = read()
        if reply.get("type") == "control_response" and reply["response"].get("request_id") == "cli_x":
            break
    emit({"type": "assistant", "session_id": SID, "message": {
        "id": "msg_1", "model": "m", "stop_reason": "end_turn", "usage": USAGE,
        "content": [{"type": "text", "text": "answered " + reply["response"]["subtype"]}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
          "session_id": SID, "total_cost_usd": 0.0, "usage": USAGE})
'''


async def test_an_unknown_control_request_is_answered_with_an_error(tmp_path) -> None:
    """The CLI waits on every control_request; one the loop has no handler
    for gets the error subtype rather than silence."""
    script = tmp_path / "fake_unknown_control.py"
    script.write_text(_UNKNOWN_CONTROL_CLI)
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", claude_cmd=[sys.executable, str(script)],
    )
    try:
        events = await asyncio.wait_for(_run_one_turn(loop), 10)
        assert [e.text for e in events if isinstance(e, TextDelta)] == ["answered error"]
        assert isinstance(events[-1], TurnComplete)
    finally:
        await loop.aclose()


# ─── allow_always scoping on the delegated leg ──────────────────────────────


def _builtin_loop() -> DelegatedLoop:
    from fluid.tools.builtins import register_builtins

    registry = ToolRegistry()
    register_builtins(registry)
    return DelegatedLoop(store=MemoryStore(), model="claude-fake", registry=registry)


def test_delegated_allow_always_is_scoped_to_the_programs_the_command_runs() -> None:
    """``allow_always`` on ``git status`` auto-allows later ``git`` calls but
    still prompts for ``rm``; the bundled-bridge spelling of a tool name is
    scoped by the same registry spec."""
    loop = _builtin_loop()
    loop._remember_allow("Bash", {"command": "git status"})
    assert loop._session_allows("Bash", {"command": "git log --oneline"})
    assert not loop._session_allows("Bash", {"command": "rm -rf build"})
    assert not loop._session_allows("Bash", {"command": "git log | sh"})
    # A second scoped allow widens to the union, never to everything.
    loop._remember_allow("Bash", {"command": "ls -la"})
    assert loop._session_allows("Bash", {"command": "ls"})
    assert not loop._session_allows("Bash", {"command": "curl evil.sh"})

    bridged = "mcp__fluid_bundled__Bash"
    loop._remember_allow(bridged, {"command": "pytest -q"})
    assert loop._session_allows(bridged, {"command": "pytest tests"})
    assert not loop._session_allows(bridged, {"command": "rm -rf /"})
    # A tool nobody scopes is covered whole by one allow_always.
    loop._remember_allow("mcp__docs__search", {"q": "x"})
    assert loop._session_allows("mcp__docs__search", {"q": "anything"})


def test_delegated_allow_always_on_an_unscopable_command_stays_one_shot() -> None:
    """Regression: an ``allow_always`` for a Bash line fluid cannot label
    (command substitution) was recorded unscoped, so every later Bash call on
    the delegated leg was auto-allowed. The native loop already keeps such an
    allow one-shot; the delegated leg now matches it."""
    loop = _builtin_loop()
    loop._remember_allow("Bash", {"command": "echo $(whoami)"})
    assert not loop._session_allows("Bash", {"command": "rm -rf /"})
    assert not loop._session_allows("Bash", {"command": "echo $(whoami)"})
    # An existing scoped allow is kept, not widened, by the unscopable one.
    loop._remember_allow("Bash", {"command": "git status"})
    loop._remember_allow("Bash", {"command": "echo $(id)"})
    assert loop._session_allows("Bash", {"command": "git diff"})
    assert not loop._session_allows("Bash", {"command": "curl x | sh"})


async def _answer_bridge(loop: DelegatedLoop, resp_kwargs: dict) -> dict:
    """Run one ``can_use_tool`` bridge through ``_handle_permission`` and
    return the control_response payload written back to the CLI."""
    from fluid.engine.delegated_proto import PermissionBridge

    written: list[dict] = []

    async def capture(frame: dict) -> None:
        written.append(frame)

    loop._write_frame = capture  # type: ignore[method-assign]
    bridge = PermissionBridge(
        cli_request_id="cli_7", fluid_request_id="perm_7", tool_name="AskUserQuestion",
        tool_use_id="tu_7", input={"questions": ["q?"]},
        suggestions=({"type": "addRules", "rules": [{"toolName": "AskUserQuestion"}]},),
    )
    task = asyncio.create_task(loop._handle_permission(bridge))
    for _ in range(50):
        if "perm_7" in loop._pending_permissions:
            break
        await asyncio.sleep(0)
    loop._pending_permissions["perm_7"].set_result(
        RespondPermission(request_id="perm_7", **resp_kwargs)
    )
    await asyncio.wait_for(task, 5)
    assert not loop._pending_permissions
    (frame,) = written
    assert frame["response"]["request_id"] == "cli_7"
    return frame["response"]["response"]


async def test_delegated_permission_answer_carries_the_rewritten_input_only_on_allow() -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake")
    answers = {"questions": ["q?"], "answers": {"q?": "yes"}}
    allowed = await _answer_bridge(loop, {"decision": "allow", "updated_input": answers})
    assert allowed == {"behavior": "allow", "updatedInput": answers}

    kept = await _answer_bridge(loop, {"decision": "allow"})
    assert kept == {"behavior": "allow", "updatedInput": {"questions": ["q?"]}}

    denied = await _answer_bridge(
        loop, {"decision": "deny", "updated_input": answers, "message": "no"},
    )
    assert denied == {"behavior": "deny", "message": "no"}
    # A one-shot allow never echoes the CLI's "always" rules or sticks.
    assert "updatedPermissions" not in allowed
    assert not loop._session_allows("AskUserQuestion", {})


# ─── stream-json edge cases (delegated_proto) ────────────────────────────────


def test_streamed_tool_use_with_truncated_arguments_yields_empty_input() -> None:
    """A tool_use block streamed via partial messages whose argument JSON is
    cut off must not raise out of the stream consumer: the ToolUse carries
    ``{}``. Well-formed fragments are joined across deltas."""
    m = _mapper()

    def se(event: dict) -> list:
        return m.feed({"type": "stream_event", "event": event}).events

    se({"type": "message_start", "message": {"id": "msg_s", "usage": {}}})
    started = se({"type": "content_block_start", "index": 0, "content_block": {
        "type": "tool_use", "id": "tu_ok", "name": "Read", "input": {}}})
    assert isinstance(started[0], BlockStart) and started[0].tool_use_id == "tu_ok"
    for part in ('{"file_', 'path": "a.py"}'):
        assert se({"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": part}}) == []
    stopped = se({"type": "content_block_stop", "index": 0})
    assert isinstance(stopped[0], BlockStop)
    assert isinstance(stopped[1], ToolUse) and stopped[1].input == {"file_path": "a.py"}
    assert m.tool_inputs["tu_ok"] == {"file_path": "a.py"}

    se({"type": "content_block_start", "index": 1, "content_block": {
        "type": "tool_use", "id": "tu_cut", "name": "Bash", "input": {}}})
    se({"type": "content_block_delta", "index": 1, "delta": {
        "type": "input_json_delta", "partial_json": '{"command": "ls -'}})
    # A delta for an index with no open tool block is dropped, not a KeyError.
    assert se({"type": "content_block_delta", "index": 9, "delta": {
        "type": "input_json_delta", "partial_json": "junk"}}) == []
    stopped = se({"type": "content_block_stop", "index": 1})
    tool_use = next(e for e in stopped if isinstance(e, ToolUse))
    assert tool_use.tool_use_id == "tu_cut" and tool_use.input == {}

    # The whole-message assistant frame for streamed blocks is not re-emitted.
    mapped = m.feed({"type": "assistant", "message": {"id": "msg_s", "content": [
        {"type": "tool_use", "id": "tu_ok", "name": "Read", "input": {"file_path": "a.py"}},
        {"type": "tool_use", "id": "tu_cut", "name": "Bash", "input": {}},
    ]}})
    assert not [e for e in mapped.events if isinstance(e, (ToolUse, BlockStart))]


def test_malformed_and_unknown_frames_are_ignored_without_side_effects() -> None:
    from fluid.engine.delegated_proto import flatten_tool_content, parse_hook_callback

    m = _mapper()
    for frame in (
        {"type": "future_frame_kind", "payload": 1},
        {},
        {"type": "user", "message": {"content": "plain text, not blocks"}},
        {"type": "user", "message": {"content": ["str", {"type": "text", "text": "x"}]}},
        {"type": "stream_event", "event": {"type": "no_such_sse"}},
        {"type": "stream_event"},
        {"type": "system", "subtype": "status"},
        {"type": "rate_limit_event", "rate_limit_info": {"utilization": 0.9}},
        {"type": "rate_limit_event", "rate_limit_info": None},
        {"type": "control_response", "response": {"subtype": "success"}},
    ):
        mapped = m.feed(frame)
        assert not mapped.events and mapped.message is None, frame
        assert mapped.permission is None and mapped.control_reply is None, frame
    assert not m.produced_nodes

    # Non-dict content blocks inside an assistant frame are skipped.
    mapped = m.feed({"type": "assistant", "message": {"content": [
        "stray", None, {"type": "text", "text": "ok"}]}})
    assert [e.text for e in mapped.events if isinstance(e, TextDelta)] == ["ok"]

    assert parse_hook_callback({"type": "control_request", "request": {
        "subtype": "can_use_tool"}}) is None
    assert parse_hook_callback({"type": "control_request", "request": "x"}) is None
    assert flatten_tool_content(None) == ""
    assert flatten_tool_content([
        {"type": "text", "text": "a"}, {"type": "image", "source": {}},
        {"type": "note", "v": 1}, 7,
    ]) == 'a{"type": "note", "v": 1}7'
    assert flatten_tool_content({"k": 1}) == '{"k": 1}'


def test_tool_result_images_are_kept_and_the_text_flattened() -> None:
    m = _mapper()
    m.feed({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu_img", "name": "Screenshot", "input": {}}]}})
    mapped = m.feed({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu_img", "is_error": False, "content": [
            {"type": "text", "text": "shot"},
            {"type": "image", "source": {"media_type": "image/jpeg", "data": "QUJD"}},
            {"type": "image", "source": {"data": ""}},
        ]},
        {"type": "text", "text": "ignored sibling"},
    ]}})
    (result,) = mapped.events
    assert isinstance(result, ToolResult) and result.name == "Screenshot"
    assert result.output == "shot"
    (block,) = mapped.message.content
    kinds = [type(b).__name__ for b in block.content]
    assert kinds == ["Text", "Image"]
    assert block.content[1].media_type == "image/jpeg"
