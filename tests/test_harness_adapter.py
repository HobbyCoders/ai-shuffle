"""Unit tests for HarnessAdapter — the omni-backed ProviderAdapter.

These tests are runnable with the omni venv (no claude_agent_sdk required, no
live API calls). They exercise:

  1. The omni Event -> ProviderEvent translation table, one case per event type.
  2. Iterator completion on terminal omni events (TurnComplete/Interrupted/Error).
  3. The permission round-trip: a omni PermissionRequest surfaces as a
     PermissionRequestEvent, and respond_to_permission() submits a matching
     omni RespondPermission op back to the engine (tool_use_id -> request_id).
  4. An end-to-end turn over a REAL NativeLoop driven by a fake provider that
     replays StreamChunks (text turn -> deltas + usage + result).
  5. The provider-kind decision logic.
  6. The SDK @tool -> omni InprocTool bridge.

Run:  cd /home/owner/Documents/AI-Shuffle && \
      /path/to/omni-harness/.venv/bin/python -m pytest \
          tests/test_harness_adapter.py -q
"""

from __future__ import annotations

import dataclasses

import pytest

from app.core.provider.harness_adapter import HarnessAdapter, _decide_provider_kind
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.core.provider.events import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from omni.events import model as lm
from omni.events.ops import RespondPermission, UserTurn
from omni.providers.base import ProviderCaps, StreamChunk
from omni.store.base import MemoryStore

SID = "ses_test"


# ─── Fakes ────────────────────────────────────────────────────────────────────


class FakeEngine:
    """Minimal stand-in for a omni engine: scripted events + recorded ops."""

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
    """A omni ProviderClient replaying a fixed list of StreamChunks."""

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
    # Partial mode on: BlockStart/deltas surface as raw StreamEvents, exactly
    # like the SDK's include_partial_messages stream.
    adapter._options = SessionOptions(model="fake-model", include_partial_messages=True)
    got = await _collect(adapter)

    # TurnComplete is terminal -> iterator stops right after ResultMessage.
    assert isinstance(got[0], SystemMessage) and got[0].subtype == "init"
    assert got[0].data["session_id"] == SID and got[0].data["model"] == "m"
    assert isinstance(got[1], StreamEvent) and got[1].event["type"] == "content_block_start"
    assert got[1].event["content_block"]["type"] == "text"
    assert isinstance(got[2], StreamEvent) and got[2].event["delta"] == {
        "type": "text_delta", "text": "Hel"}
    assert isinstance(got[3], StreamEvent) and got[3].event["delta"] == {
        "type": "thinking_delta", "thinking": "hmm"}
    assert isinstance(got[4], StreamEvent) and got[4].event["type"] == "content_block_stop"
    # Complete text block follows the deltas as an AssistantMessage (SDK parity).
    assert isinstance(got[5], AssistantMessage)
    assert isinstance(got[5].content[0], TextBlock) and got[5].content[0].text == "Hel"
    assert got[5].model == "m"  # model from TurnStarted, not options
    assert isinstance(got[6], AssistantMessage)
    tu = got[6].content[0]
    assert isinstance(tu, ToolUseBlock) and tu.name == "Read" and tu.id == "tu1"
    assert isinstance(got[7], UserMessage)
    tr = got[7].content[0]
    assert isinstance(tr, ToolResultBlock) and tr.tool_use_id == "tu1" and tr.content == "ok"
    assert isinstance(got[8], RateLimitEvent)
    assert got[8].rate_limit_info.rate_limit_type == "requests"
    assert got[8].rate_limit_info.utilization == 0.5
    assert isinstance(got[9], TaskStartedMessage) and got[9].task_id == "t1"
    assert isinstance(got[9], SystemMessage)  # subclass, like the SDK hierarchy
    assert isinstance(got[10], TaskProgressMessage)
    assert isinstance(got[11], TaskNotificationMessage) and got[11].status == "completed"
    assert isinstance(got[12], ResultMessage) and got[12].stop_reason == "end_turn"
    assert got[12].total_cost_usd == 0.01
    assert got[12].usage == {"input_tokens": 10}
    assert len(got) == 13  # nothing after the terminal event


@pytest.mark.asyncio
async def test_translation_non_partial_buffers_deltas():
    # Default options (include_partial_messages=False): deltas are buffered
    # silently and surface once, as the complete AssistantMessage at BlockStop.
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.BlockStart(session_id=SID, index=0, block_type="text"),
        lm.TextDelta(session_id=SID, index=0, text="Hello "),
        lm.TextDelta(session_id=SID, index=0, text="world"),
        lm.BlockStop(session_id=SID, index=0),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    kinds = [type(e).__name__ for e in got]
    assert kinds == ["SystemMessage", "AssistantMessage", "ResultMessage"]
    assert got[1].content[0].text == "Hello world"


@pytest.mark.asyncio
async def test_usage_attaches_to_next_assistant_message():
    # A omni Usage reading rides on the next synthesized AssistantMessage in
    # the SDK's field names, where the loops read per-call token counts.
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.Usage(session_id=SID, input_tokens=10, output_tokens=3,
                 cache_read_tokens=7, cache_creation_tokens=2),
        lm.BlockStart(session_id=SID, index=0, block_type="text"),
        lm.TextDelta(session_id=SID, index=0, text="hi"),
        lm.BlockStop(session_id=SID, index=0),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    asst = [e for e in got if isinstance(e, AssistantMessage)]
    assert asst and asst[0].usage == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 2,
    }


@pytest.mark.asyncio
async def test_interrupted_is_terminal():
    events = [
        lm.TextDelta(session_id=SID, index=0, text="hi"),
        lm.Interrupted(session_id=SID, message="stopped"),
        lm.TextDelta(session_id=SID, index=0, text="should-not-appear"),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    # Non-partial mode buffers the delta; the interrupt lands as the terminal
    # ResultMessage with the SDK's cancellation terminal_reason.
    assert len(got) == 1
    assert isinstance(got[0], ResultMessage)
    assert got[0].terminal_reason == "aborted_streaming"
    assert got[0].is_error is False and got[0].result == "stopped"


@pytest.mark.asyncio
async def test_error_is_terminal():
    events = [lm.ErrorEvent(session_id=SID, message="boom", retriable=True)]
    got = await _collect(_adapter_with(FakeEngine(events)))
    assert len(got) == 1 and isinstance(got[0], ResultMessage)
    assert got[0].is_error is True and got[0].errors == ["boom"]
    assert got[0].subtype == "error_during_execution"


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
async def test_permission_bridged_to_can_use_tool():
    """With an app can_use_tool callback wired (the WS/chat path), a omni
    PermissionRequest never surfaces in-stream; the callback's decision is
    submitted back as a RespondPermission op (SDK out-of-band parity)."""
    events = [
        lm.PermissionRequest(
            session_id=SID, request_id="perm_1", tool_use_id="tu7",
            name="Bash", input={"cmd": "ls"}, risk="safe",
        ),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    engine = FakeEngine(events)
    adapter = _adapter_with(engine)

    seen = {}

    class _Allow:  # detected by class name, like the SDK's PermissionResultAllow
        pass
    _Allow.__name__ = "PermissionResultAllow"

    async def can_use_tool(tool_name, tool_input, context):
        seen["args"] = (tool_name, tool_input, context.tool_use_id)
        return _Allow()

    adapter._can_use_tool = can_use_tool
    got = await _collect(adapter)

    # The permission event was consumed by the bridge, not yielded.
    assert not any(isinstance(e, PermissionRequestEvent) for e in got)
    # Let the spawned bridge task run to completion.
    import asyncio
    for _ in range(10):
        if engine.ops:
            break
        await asyncio.sleep(0)
    assert seen["args"] == ("Bash", {"cmd": "ls"}, "tu7")
    assert len(engine.ops) == 1
    assert isinstance(engine.ops[0], RespondPermission)
    assert engine.ops[0].request_id == "perm_1" and engine.ops[0].decision == "allow"


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
    from omni.engine.native import NativeLoop

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

    inits = [e for e in got if isinstance(e, SystemMessage) and e.subtype == "init"]
    assert inits and inits[0].data["session_id"] == loop.session_id
    text = "".join(
        b.text
        for e in got if isinstance(e, AssistantMessage)
        for b in e.content if isinstance(b, TextBlock)
    )
    assert text == "Hello world"
    assert isinstance(got[-1], ResultMessage) and got[-1].stop_reason == "end_turn"

    # sdk_session_id resolves to the live omni session id.
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
        SessionOptions(model="claude-x", extra={"omni_engine": "native", "omni_provider": "anthropic"})
    ) == "anthropic"
    assert _decide_provider_kind(
        SessionOptions(model="gpt-4o", extra={"omni_provider": "openrouter"})
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


# ─── 5. SDK @tool -> omni InprocTool bridge ──────────────────────────────────


@pytest.mark.asyncio
async def test_tool_bridge():
    from app.core.provider.omni_tool_bridge import (
        register_sdk_tools,
        sdk_tool_to_inproc,
    )
    from omni.tools.registry import ToolRegistry

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
