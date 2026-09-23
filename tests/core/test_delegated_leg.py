"""The delegated Claude leg: Claude Code's preset prompt with the app's text
appended (decision D6)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import app.core.provider.harness_adapter as ha
from app.core.provider.events import ResultMessage
from app.core.provider.types import SessionOptions
from fluid.engine.delegated import DelegatedLoop
from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

FAKE = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"

_BRIDGED = ("Remember", "Recall", "Forget", "SessionSearch", "SessionView")


def _memory_registry() -> ToolRegistry:
    """A registry carrying the app-memory bridge the way a delegated connect
    registers it (``mcp__builtins__*`` names)."""

    async def handler(input, ctx):
        return ToolOutcome(text="ok")

    registry = ToolRegistry()
    for tool in _BRIDGED:
        registry.register_builtin(
            ToolSpec(name=f"mcp__builtins__{tool}", description=tool, input_schema={}),
            handler,
        )
    return registry


def _preset_options(cwd, *, registry=None, **extra) -> SessionOptions:
    return SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND TEXT"},
        cwd=str(cwd),
        extra={"fluid_registry": registry if registry is not None else ToolRegistry(), **extra},
    )


class _ArgvLoop(DelegatedLoop):
    """The real delegated loop, recording the argv of every CLI spawn."""

    argvs: list = []

    def _build_argv(self):
        argv = super()._build_argv()
        type(self).argvs.append(argv)
        return argv


def _flag(argv: list, name: str):
    return argv[argv.index(name) + 1] if name in argv else None


async def _run_turn(adapter, text: str) -> ResultMessage:
    await adapter.send_query(text)
    async for message in adapter.receive_events():
        if isinstance(message, ResultMessage):
            return message
    raise AssertionError("turn ended without a result")


def test_delegated_append_carries_the_app_guidance(tmp_path):
    """Memory, recall and the gh note ride the appended text, named the way
    the CLI sees the bridged tools; fluid's Note/Ledger sentence does not,
    because the delegated leg has neither tool."""
    text = ha._delegated_append_text(
        "APPEND TEXT",
        _preset_options(tmp_path, github_connected=True),
        _memory_registry(),
    )
    assert text.startswith("APPEND TEXT")
    assert "# App tools on this session" in text
    for tool in _BRIDGED:
        assert f"__{tool}" in text
    assert ha.RECALL_BLOCK in text
    assert ha._MEMORY_BLOCK_CORE in text
    assert "Note and Ledger" not in text
    assert ha.GITHUB_BLOCK in text


def test_delegated_append_drops_guidance_for_tools_off_the_surface(tmp_path):
    """Ruling T9 on this leg too: no block describes a tool the session
    cannot call."""
    options = _preset_options(tmp_path, github_connected=True)
    options.tool_allowlist = ["Read"]
    text = ha._delegated_append_text("APPEND TEXT", options, _memory_registry())
    assert text == "APPEND TEXT"


@pytest.mark.asyncio
async def test_delegated_append_is_byte_identical_across_turns(monkeypatch, tmp_path):
    """The appended prompt is part of the CLI's cached prefix, so it must not
    change between turns. Two turns, with a CLI restart between them so the
    second one builds its argv again, must send the same bytes."""
    monkeypatch.setattr(ha, "DelegatedLoop", _ArgvLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    _ArgvLoop.argvs = []

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        _preset_options(tmp_path, registry=_memory_registry(), github_connected=True)
    )
    try:
        first = await _run_turn(adapter, "first turn")
        await adapter.engine._stop_process()
        second = await _run_turn(adapter, "second turn")
    finally:
        await adapter.disconnect()

    assert not first.is_error and not second.is_error
    assert len(_ArgvLoop.argvs) == 2
    appends = [_flag(argv, "--append-system-prompt") for argv in _ArgvLoop.argvs]
    assert appends[0] is not None
    assert appends[0].encode("utf-8") == appends[1].encode("utf-8")
    assert all(_flag(argv, "--system-prompt") is None for argv in _ArgvLoop.argvs)
    # The second spawn resumed the same CLI conversation.
    assert _flag(_ArgvLoop.argvs[1], "--resume") is not None


# ─── Hooks: the delegated leg feeds the same HookBus the native leg does ────


class _KwargsLoop:
    """Records the kwargs an engine is built with."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        self.agents = []

    async def _ensure_session(self):
        class _Meta:
            session_id = "ses_x"

        return _Meta()

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_both_legs_get_the_adapters_hook_bus(monkeypatch, tmp_path):
    """One bus per adapter, handed to whichever loop the connect builds, so a
    callback on ``adapter.hooks`` observes either leg."""

    async def _client(kind, options):
        return object(), None

    monkeypatch.setattr(ha, "DelegatedLoop", _KwargsLoop)
    delegated = ha.HarnessAdapter()
    await delegated.connect(_preset_options(tmp_path))
    assert _KwargsLoop.captured["hooks"] is delegated.hooks
    assert callable(_KwargsLoop.captured["tool_policy"])

    monkeypatch.setattr(ha, "NativeLoop", _KwargsLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _client)
    native = ha.HarnessAdapter()
    await native.connect(SessionOptions(
        model="claude-sonnet-4-5", cwd=str(tmp_path),
        extra={"fluid_engine": "native", "fluid_provider": "anthropic"},
    ))
    assert _KwargsLoop.captured["hooks"] is native.hooks


def test_delegated_tool_policy_denies_what_the_app_denies(tmp_path):
    """The PreToolUse policy refuses the blocklist, anything off the surface
    and a permission-engine deny rule; everything else is left to the CLI."""
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    options = _preset_options(tmp_path)
    options.tool_allowlist = ["Bash", "Read", "mcp__browser__*"]
    permissions = PermissionEngine(
        "on_write", rules=[Rule(tool="Read", decision="deny", input_globs={"file_path": "*.env"})],
    )
    policy = ha._delegated_tool_policy(options, permissions, ["Task", "Agent"])

    assert policy("Task", {}) == "Tool 'Task' is disabled in this profile"
    assert policy("WebFetch", {"url": "x"}) == "Tool 'WebFetch' is not enabled in this profile"
    assert "configured rule" in policy("Read", {"file_path": "prod.env"})
    assert policy("Read", {"file_path": "notes.txt"}) is None
    assert policy("Bash", {"command": "ls"}) is None
    assert policy("mcp__browser__navigate", {}) is None


@pytest.mark.asyncio
async def test_delegated_pre_tool_use_reaches_the_bus_and_the_policy_blocks(
    monkeypatch, tmp_path
):
    """End to end against the fake CLI: the CLI's PreToolUse for Bash lands
    on ``adapter.hooks`` as the native loop's PreToolUse event, and the
    profile blocklist denies the call there, so the model gets the denial as
    the tool's result."""
    from fluid.events.model import ToolResult
    from fluid.hooks.bus import PRE_TOOL_USE

    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    seen: list = []

    async def observe(event, payload):
        seen.append((event, payload["name"], payload["input"]))
        return None

    options = _preset_options(tmp_path)
    options.disallowed_tools = ["Bash"]
    options.env = {"FAKE_CLAUDE_SCENARIO": "u"}
    adapter = ha.HarnessAdapter()
    adapter.hooks.on(PRE_TOOL_USE, observe)
    await adapter.connect(options)
    try:
        result = await _run_turn(adapter, "run it")
        events = adapter.engine.bus.log(adapter.engine.session_id)
    finally:
        await adapter.disconnect()

    assert not result.is_error
    assert seen == [(PRE_TOOL_USE, "Bash", {"command": "echo hooked"})]
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [(r.is_error, r.output) for r in results] == [
        (True, "Tool 'Bash' is disabled in this profile"),
    ]


# ─── Usage: the delegated leg fills the same per-call roll-up ───────────────


def test_delegated_usage_stream_rolls_up_with_its_cache_hit_ratio():
    """A synthetic CLI message stream with known usage, mapped by the
    delegated leg and translated by the adapter, fills the per-call roll-up
    the native leg fills: summed tokens, a cost for every call, one call per
    model message, and a cache-hit ratio that matches the totals."""
    from app.core.provider.events import AssistantMessage
    from fluid.engine.delegated_proto import StreamJsonMapper
    from fluid.events.model import Usage
    from fluid.providers.pricing import estimate_cost

    model = "claude-opus-4-6"
    calls = [
        {"input_tokens": 100, "output_tokens": 20,
         "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 900},
        {"input_tokens": 50, "output_tokens": 40,
         "cache_read_input_tokens": 9900, "cache_creation_input_tokens": 50},
    ]

    def frame(msg_id, usage, block):
        return {"type": "assistant", "session_id": "cli", "message": {
            "id": msg_id, "model": model, "stop_reason": "end_turn",
            "content": [block], "usage": usage,
        }}

    frames = [
        # One model message split into two per-block frames, each repeating
        # the message's usage: it is still one call.
        frame("msg_1", calls[0], {"type": "thinking", "thinking": "plan", "signature": "s"}),
        frame("msg_1", calls[0], {"type": "text", "text": "Looking."}),
        frame("msg_2", calls[1], {"type": "text", "text": "Done."}),
    ]
    mapper = StreamJsonMapper(session_id="ses", turn_id="turn", model="fable", user_node_id="n0")
    events = [e for f in frames for e in mapper.feed(f).events]
    usages = [e for e in events if isinstance(e, Usage)]
    # The context breakdown the loop asks for after each call arrives as its
    # own record, tokens zero, carrying the index of the call it describes.
    events.append(Usage(
        session_id="ses", node_id=usages[-1].node_id, turn_id="turn",
        context_usage={"totalTokens": 20000, "maxTokens": 200000},
        raw={"model_call_index": 2},
    ))

    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="fable")
    adapter._mark_turn_floor()
    per_call = [
        m.usage for e in events for m in adapter._translate(e)
        if isinstance(m, AssistantMessage) and m.usage
    ]

    totals = adapter._turn_usage_snapshot()
    assert adapter._turn_model_calls == 2
    assert totals["input_tokens"] == 150
    assert totals["output_tokens"] == 60
    assert totals["cache_read_input_tokens"] == 18900
    assert totals["cache_creation_input_tokens"] == 950
    assert totals["cost_usd"] == pytest.approx(
        estimate_cost(model, calls[0]) + estimate_cost(model, calls[1])
    )
    prompt = (
        totals["input_tokens"] + totals["cache_read_input_tokens"]
        + totals["cache_creation_input_tokens"]
    )
    assert totals["cache_read_input_tokens"] / prompt == pytest.approx(0.945)

    # Each call's record carries the provider-reported cache verdict and its
    # index, like a native record; the snapshot rides with the second call's.
    assert [u["model_call_index"] for u in per_call] == [1, 2, 2]
    assert per_call[0]["cache_read_report"]["cached_fraction"] == pytest.approx(0.9, abs=1e-3)
    assert per_call[1]["cache_read_report"]["cached_fraction"] == pytest.approx(0.99, abs=1e-3)
    assert per_call[2]["context_usage"]["totalTokens"] == 20000


def test_a_zero_usage_native_call_still_counts_as_a_call():
    """Only the delegated leg's snapshot record (a call index already counted
    this turn, every token field zero) is skipped. A native call whose
    provider reported no usage also has zero tokens and a context breakdown,
    but it is a real call with its own index, and the ErrorEvent backstop
    must still count it."""
    from fluid.events.model import Usage

    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="local-model")
    adapter._mark_turn_floor()
    for index in (1, 2):
        adapter._translate(Usage(
            session_id="ses", node_id=f"n{index}", turn_id="turn",
            raw={"model_call_index": index},
            context_usage={"totalTokens": 1000 * index, "maxTokens": 32000},
        ))
    assert adapter._turn_model_calls == 2
