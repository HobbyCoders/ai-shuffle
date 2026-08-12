"""Unit tests for HarnessAdapter — the loom-backed ProviderAdapter.

These tests are runnable with the loom venv (no claude_agent_sdk required, no
live API calls). They exercise:

  1. The loom Event -> ProviderEvent translation table, one case per event type.
  2. Iterator completion on terminal loom events (TurnComplete/Interrupted/Error).
  3. The permission round-trip: a loom PermissionRequest surfaces as a
     PermissionRequestEvent, and respond_to_permission() submits a matching
     loom RespondPermission op back to the engine (tool_use_id -> request_id).
  4. An end-to-end turn over a REAL NativeLoop driven by a fake provider that
     replays StreamChunks (text turn -> deltas + usage + result).
  5. The provider-kind decision logic.
  6. The SDK @tool -> loom InprocTool bridge.

Run:  cd /home/owner/Documents/AI-Shuffle && \
      /home/owner/Documents/loom-harness/.venv/bin/python -m pytest \
          tests/test_harness_adapter.py -q
"""

from __future__ import annotations

import dataclasses

import pytest

from app.core.provider.harness_adapter import HarnessAdapter, _decide_provider_kind
from app.core.provider.types import (
    ErrorEvent,
    InterruptedEvent,
    PermissionRequestEvent,
    RateLimitEvent,
    ResultEvent,
    SessionOptions,
    StreamBlockStartEvent,
    StreamBlockStopEvent,
    StreamDeltaEvent,
    SystemInitEvent,
    TaskNotificationEvent,
    TaskProgressEvent,
    TaskStartedEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnUsageEvent,
)

from loom.events import model as lm
from loom.events.ops import RespondPermission, UserTurn
from loom.providers.base import ProviderCaps, StreamChunk
from loom.store.base import MemoryStore

SID = "ses_test"


# ─── Fakes ────────────────────────────────────────────────────────────────────


class FakeEngine:
    """Minimal stand-in for a loom engine: scripted events + recorded ops."""

    def __init__(self, events: list) -> None:
        # assign monotonic seq like the EventBus would
        self._events = [dataclasses.replace(e, seq=i + 1) for i, e in enumerate(events)]
        self.ops: list = []
        self.model = "fake-model"

    async def submit(self, op) -> None:
        self.ops.append(op)

    async def events(self, *, since_seq: int = 0):
        for e in self._events:
            if e.seq > since_seq:
                yield e

    @property
    def session_id(self) -> str:
        return SID

    async def aclose(self) -> None:
        pass


class FakeProvider:
    """A loom ProviderClient replaying a fixed list of StreamChunks."""

    name = "fake"
    caps = ProviderCaps()

    def __init__(self, chunks: list) -> None:
        self._chunks = chunks

    async def stream(self, request):
        for c in self._chunks:
            yield c


def _adapter_with(engine) -> HarnessAdapter:
    a = HarnessAdapter()
    a._engine = engine
    a._connected = True
    a._options = SessionOptions(model="fake-model")
    return a


async def _collect(adapter, limit: int = 100) -> list:
    out = []
    async for ev in adapter.receive_events():
        out.append(ev)
        if len(out) >= limit:
            break
    return out


# ─── 1. Translation table ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_translation_table():
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.BlockStart(session_id=SID, index=0, block_type="text"),
        lm.TextDelta(session_id=SID, index=0, text="Hel"),
        lm.ThinkingDelta(session_id=SID, index=1, text="hmm"),
        lm.BlockStop(session_id=SID, index=0),
        lm.ToolUse(session_id=SID, tool_use_id="tu1", name="Read", input={"path": "/x"}),
        lm.ToolResult(session_id=SID, tool_use_id="tu1", name="Read", output="ok"),
        lm.Usage(session_id=SID, input_tokens=10, output_tokens=3),
        lm.RateLimit(session_id=SID, kind="requests", resets_at="soon", utilization=0.5),
        lm.SubagentEvent(session_id=SID, task_id="t1", kind="started", description="d"),
        lm.SubagentEvent(session_id=SID, task_id="t1", kind="progress", description="p"),
        lm.SubagentEvent(session_id=SID, task_id="t1", kind="finished", summary="done"),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1,
                        usage={"input_tokens": 10}, cost_usd=0.01),
    ]
    adapter = _adapter_with(FakeEngine(events))
    got = await _collect(adapter)

    # TurnComplete is terminal -> iterator stops right after ResultEvent.
    assert isinstance(got[0], SystemInitEvent) and got[0].sdk_session_id == SID
    assert isinstance(got[1], StreamBlockStartEvent) and got[1].block_type == "text"
    assert isinstance(got[2], StreamDeltaEvent) and got[2].delta_type == "text" and got[2].content == "Hel"
    assert isinstance(got[3], StreamDeltaEvent) and got[3].delta_type == "thinking"
    assert isinstance(got[4], StreamBlockStopEvent) and got[4].block_index == 0
    assert isinstance(got[5], ToolUseEvent) and got[5].name == "Read" and got[5].tool_use_id == "tu1"
    assert isinstance(got[6], ToolResultEvent) and got[6].output == "ok"
    assert isinstance(got[7], TurnUsageEvent) and got[7].usage["input_tokens"] == 10
    assert isinstance(got[8], RateLimitEvent) and got[8].rate_limit_type == "requests"
    assert isinstance(got[9], TaskStartedEvent) and got[9].task_id == "t1"
    assert isinstance(got[10], TaskProgressEvent)
    assert isinstance(got[11], TaskNotificationEvent) and got[11].status == "finished"
    assert isinstance(got[12], ResultEvent) and got[12].stop_reason == "end_turn"
    assert got[12].total_cost_usd == 0.01
    assert len(got) == 13  # nothing after the terminal event


@pytest.mark.asyncio
async def test_interrupted_is_terminal():
    events = [
        lm.TextDelta(session_id=SID, index=0, text="hi"),
        lm.Interrupted(session_id=SID, message="stopped"),
        lm.TextDelta(session_id=SID, index=0, text="should-not-appear"),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    assert isinstance(got[-1], InterruptedEvent) and got[-1].message == "stopped"
    assert len(got) == 2


@pytest.mark.asyncio
async def test_error_is_terminal():
    events = [lm.ErrorEvent(session_id=SID, message="boom", retriable=True)]
    got = await _collect(_adapter_with(FakeEngine(events)))
    assert len(got) == 1 and isinstance(got[0], ErrorEvent) and got[0].message == "boom"


# ─── 2. Permission round-trip ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permission_round_trip():
    events = [
        lm.PermissionRequest(
            session_id=SID, request_id="perm_9", tool_use_id="tu42",
            name="Bash", input={"cmd": "rm -rf x"}, risk="destructive",
        ),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    engine = FakeEngine(events)
    adapter = _adapter_with(engine)

    agen = adapter.receive_events()
    first = await agen.__anext__()
    assert isinstance(first, PermissionRequestEvent)
    assert first.tool_use_id == "tu42" and first.request_id == "perm_9"
    assert first.risk == "destructive"

    # Respond using the app-facing tool_use_id; adapter maps it to request_id.
    await adapter.respond_to_permission("tu42", allow=True, remember=True)
    assert len(engine.ops) == 1
    op = engine.ops[0]
    assert isinstance(op, RespondPermission)
    assert op.request_id == "perm_9"
    assert op.decision == "allow_always"  # remember=True -> allow_always
    assert op.remember is True

    await agen.aclose()


@pytest.mark.asyncio
async def test_permission_deny_mapping():
    events = [lm.PermissionRequest(session_id=SID, request_id="p1", tool_use_id="t1",
                                   name="Bash", input={}, risk="safe")]
    engine = FakeEngine(events)
    adapter = _adapter_with(engine)
    async for ev in adapter.receive_events():
        break  # grab the permission event
    await adapter.respond_to_permission("t1", allow=False)
    assert engine.ops[0].decision == "deny"


# ─── 3. End-to-end over a real NativeLoop + fake provider ────────────────────


@pytest.mark.asyncio
async def test_native_loop_text_turn():
    from loom.engine.native import NativeLoop

    chunks = [
        StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": "Hello "}),
        StreamChunk("text_delta", {"index": 0, "text": "world"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
    ]
    loop = NativeLoop(provider=FakeProvider(chunks), store=MemoryStore(), model="fake")

    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    await loop._ensure_session()
    adapter._connected = True

    await adapter.send_query("hi")
    got = await _collect(adapter)

    kinds = [type(e).__name__ for e in got]
    assert "SystemInitEvent" in kinds          # from TurnStarted
    text = "".join(e.content for e in got if isinstance(e, StreamDeltaEvent)
                   and e.delta_type == "text")
    assert text == "Hello world"
    assert any(isinstance(e, TurnUsageEvent) for e in got)
    assert isinstance(got[-1], ResultEvent) and got[-1].stop_reason == "end_turn"

    # sdk_session_id resolves to the live loom session id.
    assert adapter.sdk_session_id == loop.session_id
    await adapter.disconnect()


# ─── 4. Provider-kind decision ───────────────────────────────────────────────


def test_provider_kind_decision():
    assert _decide_provider_kind(SessionOptions(model="claude-sonnet-4-5")) == "delegated"
    assert _decide_provider_kind(SessionOptions(model="gpt-4o")) == "openai_responses"
    assert _decide_provider_kind(SessionOptions(model="o3-mini")) == "openai_responses"
    assert _decide_provider_kind(SessionOptions(model="meta-llama/llama-3.1-70b")) == "openrouter"
    # explicit hints win
    assert _decide_provider_kind(
        SessionOptions(model="claude-x", extra={"loom_engine": "native", "loom_provider": "anthropic"})
    ) == "anthropic"
    assert _decide_provider_kind(
        SessionOptions(model="gpt-4o", extra={"loom_provider": "openrouter"})
    ) == "openrouter"


def test_requires_reconnect():
    a = HarnessAdapter()
    a._options = SessionOptions(model="m", system_prompt="S", cwd="/w")
    # model-only change: hot-swappable
    assert a.requires_reconnect(SessionOptions(model="m2")) is False
    # cwd change: structural
    assert a.requires_reconnect(SessionOptions(model="m", cwd="/other")) is True
    # system prompt change: structural
    assert a.requires_reconnect(SessionOptions(model="m", system_prompt="Different")) is True


# ─── 5. SDK @tool -> loom InprocTool bridge ──────────────────────────────────


@pytest.mark.asyncio
async def test_tool_bridge():
    from app.core.provider.loom_tool_bridge import (
        register_sdk_tools,
        sdk_tool_to_inproc,
    )
    from loom.tools.registry import ToolRegistry

    class SdkToolLike:
        """Duck-typed clone of a claude_agent_sdk @tool object."""
        def __init__(self, name, description, schema, handler):
            self.name = name
            self.description = description
            self.input_schema = schema
            self.handler = handler

    async def echo_handler(args):
        return {"content": [{"type": "text", "text": f"echo:{args.get('msg')}"}]}

    sdk_tool = SdkToolLike("Echo", "echoes", {"type": "object"}, echo_handler)

    inproc = sdk_tool_to_inproc(sdk_tool, risk="write")
    assert inproc.name == "Echo" and inproc.risk == "write"

    registry = ToolRegistry()
    register_sdk_tools(registry, "builtins", [sdk_tool], risk_map={"Echo": "write"})

    spec = registry.get("mcp__builtins__Echo")
    assert spec is not None and spec.source == "inproc" and spec.risk == "write"

    outcome = await registry.dispatch("mcp__builtins__Echo", {"msg": "hi"})
    assert outcome.text == "echo:hi" and outcome.is_error is False
