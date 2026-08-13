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


# ─── 1b. Cost / usage accounting on terminal events ─────────────────────────


@pytest.mark.asyncio
async def test_turn_complete_none_cost_becomes_zero():
    """omni may report cost_usd=None (NativeLoop never sets it; the delegated
    leg can too). None would flow into `total_cost_usd = total_cost_usd + NULL`
    and NULL the session row, so the adapter coerces it to 0.0."""
    events = [
        lm.TurnComplete(
            session_id=SID, stop_reason="end_turn", num_model_calls=1,
            usage={"input_tokens": 11, "output_tokens": 4,
                   "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1},
            cost_usd=None,
        ),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    assert len(got) == 1 and isinstance(got[0], ResultMessage)
    assert got[0].total_cost_usd == 0.0
    # Token counts still ride through in the SDK's key names, which is what the
    # query loop reads off ResultMessage.usage for the session-row increments.
    assert got[0].usage["input_tokens"] == 11
    assert got[0].usage["output_tokens"] == 4
    assert got[0].usage["cache_read_input_tokens"] == 3
    assert got[0].usage["cache_creation_input_tokens"] == 1


@pytest.mark.asyncio
async def test_turn_complete_zero_cost_is_preserved():
    events = [lm.TurnComplete(session_id=SID, stop_reason="end_turn",
                              num_model_calls=1, cost_usd=0.0)]
    got = await _collect(_adapter_with(FakeEngine(events)))
    assert got[0].total_cost_usd == 0.0


@pytest.mark.asyncio
async def test_non_turn_complete_terminals_carry_float_cost():
    """Interrupted / ErrorEvent synthesize their own ResultMessage; the SDK path
    always carries a float there, so these must not inherit the None default."""
    got = await _collect(_adapter_with(FakeEngine([
        lm.Interrupted(session_id=SID, message="stopped")])))
    assert got[0].total_cost_usd == 0.0

    got = await _collect(_adapter_with(FakeEngine([
        lm.ErrorEvent(session_id=SID, message="boom", retriable=True)])))
    assert got[0].total_cost_usd == 0.0


# ─── 1c. Context usage ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_context_usage_none_without_engine():
    assert await HarnessAdapter().get_context_usage() is None


@pytest.mark.asyncio
async def test_get_context_usage_none_when_engine_cannot_answer():
    """No omni engine implements the CLI's get_context_usage control request
    today; callers all tolerate None, so the adapter degrades instead of
    raising AttributeError (which used to 500 GET .../context-usage)."""
    adapter = _adapter_with(FakeEngine([]))
    assert not hasattr(adapter._engine, "get_context_usage")
    assert await adapter.get_context_usage() is None


@pytest.mark.asyncio
async def test_get_context_usage_forwards_when_engine_supports_it():
    payload = {"totalTokens": 1234, "maxTokens": 200000, "percentage": 0.6}

    class CapableEngine(FakeEngine):
        async def get_context_usage(self):
            return payload

    assert await _adapter_with(CapableEngine([])).get_context_usage() is payload


@pytest.mark.asyncio
async def test_get_context_usage_swallows_engine_errors():
    class ExplodingEngine(FakeEngine):
        async def get_context_usage(self):
            raise RuntimeError("control request timed out")

    assert await _adapter_with(ExplodingEngine([])).get_context_usage() is None


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
    # No reason given -> the engine keeps its own generic deny text.
    assert engine.ops[0].message is None


# ─── 3b. Question dismissal / answers survive the omni permission bridge ─────
#
# On the omni path an AskUserQuestion call is gated as a PermissionRequest, so
# the modal's "X" resolves through the *permission* deny path. The dismissal
# only reads as a dismissal (rather than "permission denied") if the callback's
# PermissionResultDeny.message rides along on the RespondPermission op — and
# answers only reach the tool if PermissionResultAllow.updated_input does.


class _FakeDeny:
    """Stand-in for the SDK's PermissionResultDeny (detected by class name)."""

    def __init__(self, message: str) -> None:
        self.message = message


_FakeDeny.__name__ = "PermissionResultDeny"


class _FakeAllow:
    """Stand-in for the SDK's PermissionResultAllow."""

    def __init__(self, updated_input: dict) -> None:
        self.updated_input = updated_input


_FakeAllow.__name__ = "PermissionResultAllow"


async def _drain_bridge(engine) -> None:
    """Let the spawned permission-bridge task submit its decision."""
    import asyncio

    for _ in range(20):
        if engine.ops:
            return
        await asyncio.sleep(0)


def _question_request() -> list:
    return [
        lm.PermissionRequest(
            session_id=SID, request_id="q1", tool_use_id="tuq",
            name="AskUserQuestion",
            input={"questions": [{"question": "Pick one", "options": []}]},
            risk="safe",
        ),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]


@pytest.mark.asyncio
async def test_question_dismissal_forwards_deny_message():
    """A dismissed question denies with the dismissal text, not a bare deny."""
    from app.core.user_question_handler import DISMISS_MESSAGE

    engine = FakeEngine(_question_request())
    adapter = _adapter_with(engine)

    async def can_use_tool(tool_name, tool_input, context):
        return _FakeDeny(DISMISS_MESSAGE)

    adapter._can_use_tool = can_use_tool
    await _collect(adapter)
    await _drain_bridge(engine)

    op = engine.ops[0]
    assert isinstance(op, RespondPermission)
    assert op.decision == "deny"
    assert op.message == DISMISS_MESSAGE


@pytest.mark.asyncio
async def test_answered_question_forwards_updated_input():
    """Collected answers (skips included) ride back as ``updated_input``."""
    from app.core.user_question_handler import SKIP_ANSWER_TEXT

    answers = {"Pick one": "A", "Second question": SKIP_ANSWER_TEXT}
    engine = FakeEngine(_question_request())
    adapter = _adapter_with(engine)

    async def can_use_tool(tool_name, tool_input, context):
        return _FakeAllow({**tool_input, "answers": answers})

    adapter._can_use_tool = can_use_tool
    await _collect(adapter)
    await _drain_bridge(engine)

    op = engine.ops[0]
    assert op.decision == "allow"
    assert op.updated_input is not None
    assert op.updated_input["answers"] == answers


@pytest.mark.asyncio
async def test_delegated_loop_writes_deny_message_to_the_cli():
    """DelegatedLoop turns the op's message into the CLI's deny control_response.

    That deny message is what the CLI writes as the blocked call's tool_result,
    so this is the last hop between the modal's "X" and what the model (and the
    chat card) reads.
    """
    import asyncio

    from omni.engine.delegated import DelegatedLoop
    from omni.engine.delegated_proto import PermissionBridge
    from app.core.user_question_handler import DISMISS_MESSAGE

    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake", claude_cmd=["true"])
    frames: list = []

    async def _capture(frame):
        frames.append(frame)

    loop._write_frame = _capture  # type: ignore[assignment]

    bridge = PermissionBridge(
        cli_request_id="cli_1", omni_request_id="p1",
        tool_name="AskUserQuestion", tool_use_id="tuq",
        input={"questions": []},
    )
    task = asyncio.create_task(loop._handle_permission(bridge))
    for _ in range(20):  # let _handle_permission register its future
        if "p1" in loop._pending_permissions:
            break
        await asyncio.sleep(0)
    await loop.submit(
        RespondPermission(request_id="p1", decision="deny", message=DISMISS_MESSAGE)
    )
    await asyncio.wait_for(task, timeout=5)

    assert frames[0]["response"]["response"] == {
        "behavior": "deny", "message": DISMISS_MESSAGE,
    }


@pytest.mark.asyncio
async def test_delegated_loop_deny_falls_back_to_generic_message():
    from omni.engine.delegated import DelegatedLoop

    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake", claude_cmd=["true"])
    frames: list = []

    async def _capture(frame):
        frames.append(frame)

    loop._write_frame = _capture  # type: ignore[assignment]
    await loop._write_control_response("cli_2", False, {"a": 1})
    assert frames[0]["response"]["response"] == {
        "behavior": "deny", "message": "permission denied",
    }

    # An allow carries the (possibly rewritten) input through as updatedInput.
    await loop._write_control_response("cli_3", True, {"answers": {"q": "a"}})
    assert frames[1]["response"]["response"] == {
        "behavior": "allow", "updatedInput": {"answers": {"q": "a"}},
    }


@pytest.mark.asyncio
async def test_delegated_loop_allow_uses_updated_input():
    import asyncio

    from omni.engine.delegated import DelegatedLoop
    from omni.engine.delegated_proto import PermissionBridge

    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake", claude_cmd=["true"])
    frames: list = []

    async def _capture(frame):
        frames.append(frame)

    loop._write_frame = _capture  # type: ignore[assignment]

    bridge = PermissionBridge(
        cli_request_id="cli_4", omni_request_id="p2",
        tool_name="AskUserQuestion", tool_use_id="tuq",
        input={"questions": []},
    )
    task = asyncio.create_task(loop._handle_permission(bridge))
    for _ in range(20):
        if "p2" in loop._pending_permissions:
            break
        await asyncio.sleep(0)
    await loop.submit(RespondPermission(
        request_id="p2", decision="allow",
        updated_input={"questions": [], "answers": {"Pick one": "A"}},
    ))
    await asyncio.wait_for(task, timeout=5)

    assert frames[0]["response"]["response"]["updatedInput"] == {
        "questions": [], "answers": {"Pick one": "A"},
    }


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


def test_provider_kind_short_model_aliases():
    """The app selects Claude models by short alias — those must route to the
    delegated (claude CLI) leg, not the API-key-less native Anthropic client."""
    for alias in ("fable", "opus", "sonnet", "haiku"):
        assert _decide_provider_kind(SessionOptions(model=alias)) == "delegated"
    for full_id in ("claude-sonnet-4-6", "claude-opus-4-5", "claude-haiku-4-5"):
        assert _decide_provider_kind(SessionOptions(model=full_id)) == "delegated"
    # Non-Claude models still route by prefix / slug shape.
    assert _decide_provider_kind(SessionOptions(model="gpt-5")) == "openai_responses"
    assert _decide_provider_kind(SessionOptions(model="gpt-5-codex")) == "openai_responses"
    assert (
        _decide_provider_kind(SessionOptions(model="anthropic/claude-3.5-sonnet"))
        == "openrouter"
    )
    # engine_hint="native" overrides the subscription default for aliases too.
    assert (
        _decide_provider_kind(SessionOptions(model="fable", extra={"omni_engine": "native"}))
        == "anthropic"
    )


@pytest.mark.asyncio
async def test_delegated_loop_kwargs_filter_sdk_servers_and_stream_partial(monkeypatch):
    """The delegated leg JSON-serializes its mcp_servers map into --mcp-config,
    so in-proc type='sdk' entries (live Server objects) must be filtered out;
    and the SDK's include_partial_messages must reach ``partial``."""
    import json

    import app.core.provider.harness_adapter as ha
    from omni.tools.registry import ToolRegistry

    captured: dict = {}

    class FakeDelegatedLoop:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

        async def _ensure_session(self):
            class _Meta:
                session_id = "ses_delegated"

            return _Meta()

        async def aclose(self):
            pass

    monkeypatch.setattr(ha, "DelegatedLoop", FakeDelegatedLoop)

    class LiveServer:
        """Stand-in for an in-proc SDK Server instance (not JSON-serializable)."""

    stdio_cfg = {"type": "stdio", "command": "node", "args": ["ide-server.js"]}
    mcp_servers = {
        "builtins": {"type": "sdk", "name": "builtins", "instance": LiveServer()},
        "ide": stdio_cfg,
    }
    # Prebuilt registry short-circuits _build_registry (no real MCP dial-out).
    options = SessionOptions(
        model="fable",
        mcp_servers=mcp_servers,
        include_partial_messages=True,
        extra={"omni_registry": ToolRegistry()},
    )

    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    assert adapter._is_delegated is True

    # Only the external stdio server survives, and what survives is serializable.
    assert captured["mcp_servers"] == {"ide": stdio_cfg}
    with pytest.raises(TypeError):
        json.dumps(mcp_servers)
    json.dumps(captured["mcp_servers"])

    # include_partial_messages drives --include-partial-messages on the CLI.
    assert captured["partial"] is True

    # extra["omni_partial"] remains a manual override when the SDK flag is off.
    await adapter.connect(
        dataclasses.replace(
            options,
            include_partial_messages=False,
            extra={"omni_registry": ToolRegistry(), "omni_partial": True},
        )
    )
    assert captured["partial"] is True

    await adapter.connect(dataclasses.replace(options, include_partial_messages=False))
    assert captured["partial"] is False


# ─── 7. Builtin file/shell tools on the native leg ───────────────────────────

_OMNI_BUILTINS = {"Read", "Grep", "Write", "Edit", "Bash", "Skill", "Note"}


class _FakeInprocTool:
    name = "Ping"
    description = "ping"
    input_schema = {"type": "object"}
    risk = "safe"

    def as_handler(self):
        async def handler(inp, ctx):
            return "pong"

        return handler


class _FakeInprocServer:
    name = "fake"
    tools = [_FakeInprocTool()]


def _names(registry) -> set:
    return {spec.name for spec in registry.list_tools(loaded_only=False)}


@pytest.mark.asyncio
async def test_native_leg_registry_has_omni_builtins():
    """Native legs talk to the provider API directly — without omni's builtins
    the model has no file/shell tools at all."""
    adapter = HarnessAdapter()
    registry = await adapter._build_registry(SessionOptions(model="gpt-4o"), native=True)
    assert registry is not None
    assert _OMNI_BUILTINS <= _names(registry)


@pytest.mark.asyncio
async def test_delegated_leg_registry_has_no_omni_builtins():
    """The claude CLI ships its own Read/Bash/... — registering omni's here
    would double up on the delegated leg."""
    adapter = HarnessAdapter()
    options = SessionOptions(
        model="fable", extra={"omni_inproc_servers": [_FakeInprocServer()]}
    )
    registry = await adapter._build_registry(options, native=False)
    assert registry is not None
    names = _names(registry)
    assert "mcp__fake__Ping" in names
    assert not (_OMNI_BUILTINS & names)

    # With nothing to bridge at all the delegated leg still gets no registry.
    assert await adapter._build_registry(SessionOptions(model="fable"), native=False) is None


@pytest.mark.asyncio
async def test_connect_native_leg_passes_builtin_registry_to_native_loop(monkeypatch):
    """connect() must decide the leg BEFORE building the registry, so the
    NativeLoop receives one carrying the builtins."""
    import app.core.provider.harness_adapter as ha

    captured: dict = {}

    class FakeNativeLoop:
        def __init__(self, **kwargs):
            captured.clear()
            captured.update(kwargs)

        async def _ensure_session(self):
            class _Meta:
                session_id = "ses_native"

            return _Meta()

        async def aclose(self):
            pass

    async def fake_client(kind, options):
        return FakeProvider([]), None

    monkeypatch.setattr(ha, "NativeLoop", FakeNativeLoop)
    monkeypatch.setattr(ha, "_build_provider_client", fake_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        SessionOptions(model="gpt-4o", extra={"omni_provider": "openai_responses"})
    )
    assert adapter._is_delegated is False
    assert _OMNI_BUILTINS <= _names(captured["registry"])


@pytest.mark.asyncio
async def test_on_write_policy_gates_builtin_bash_but_not_read():
    """The permission prompt only becomes reachable once the builtins exist:
    Bash is risk='write' (ask), Read is risk='safe' (allow)."""
    from omni.permissions.engine import PermissionEngine

    adapter = HarnessAdapter()
    registry = await adapter._build_registry(SessionOptions(model="gpt-4o"), native=True)
    engine = PermissionEngine(approval_policy="on_write")

    assert engine.evaluate(registry.get("Bash"), {"command": "ls"}) == "ask"
    assert engine.evaluate(registry.get("Read"), {"file_path": "/tmp/x"}) == "allow"


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


# ─── 6. Delegated --resume handle resolution ─────────────────────────────────


class FakeDelegatedLoop:
    """Records the kwargs HarnessAdapter builds DelegatedLoop with."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs) -> None:
        FakeDelegatedLoop.last_kwargs = kwargs
        self.store = kwargs["store"]
        self._session_id = kwargs.get("session_id")

    async def _ensure_session(self):
        meta = await self.store.get_session(self._session_id)
        if meta is None:
            meta = await self.store.create_session(self._session_id)
        return meta

    @property
    def session_id(self) -> str:
        return self._session_id

    async def aclose(self) -> None:
        pass


async def _connect_delegated(monkeypatch, store, options) -> HarnessAdapter:
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", FakeDelegatedLoop)
    monkeypatch.setattr(HarnessAdapter, "_build_store", staticmethod(lambda _o: store))
    adapter = HarnessAdapter()
    await adapter.connect(options)
    return adapter


@pytest.mark.asyncio
async def test_delegated_resume_uses_stored_cli_session_id(monkeypatch):
    """A resumed session must pass the CLI's own uuid, never the omni id."""
    store = MemoryStore()
    await store.create_session("ses_resume")
    await store.set_cli_session_id("ses_resume", "cli-uuid-from-init-frame")

    await _connect_delegated(
        monkeypatch, store,
        SessionOptions(model="claude-sonnet-4-5", resume_session_id="ses_resume"),
    )

    kwargs = FakeDelegatedLoop.last_kwargs
    assert kwargs["session_id"] == "ses_resume"
    assert kwargs["cli_session_id"] == "cli-uuid-from-init-frame"


@pytest.mark.asyncio
async def test_delegated_resume_without_stored_handle_starts_fresh(monkeypatch):
    """No stored handle (pre-v70 row, or a session that never saw an init
    frame) → None, i.e. a fresh CLI conversation. Passing the ``ses_...`` id
    through would make the CLI reject --resume and kill the turn."""
    store = MemoryStore()
    await store.create_session("ses_no_handle")

    await _connect_delegated(
        monkeypatch, store,
        SessionOptions(model="claude-sonnet-4-5", resume_session_id="ses_no_handle"),
    )
    assert FakeDelegatedLoop.last_kwargs["cli_session_id"] is None


@pytest.mark.asyncio
async def test_delegated_unknown_session_starts_fresh(monkeypatch):
    """Resuming a session the store has never seen must not raise."""
    await _connect_delegated(
        monkeypatch, MemoryStore(),
        SessionOptions(model="claude-sonnet-4-5", resume_session_id="ses_ghost"),
    )
    assert FakeDelegatedLoop.last_kwargs["cli_session_id"] is None


@pytest.mark.asyncio
async def test_delegated_fresh_session_has_no_cli_handle(monkeypatch):
    """No resume at all → nothing to look up."""
    await _connect_delegated(
        monkeypatch, MemoryStore(), SessionOptions(model="claude-sonnet-4-5"),
    )
    assert FakeDelegatedLoop.last_kwargs["cli_session_id"] is None


# ─── 7. Split per-block assistant frames (duplicate-render regression) ───────
#
# The delegated CLI splits ONE model message into one ``assistant`` frame per
# content block, each frame carrying a single-element content list plus the same
# message ``id`` and the same message-level ``usage``. A thinking+text message —
# what the model produces whenever extended thinking is on, e.g. the reply after
# an AskUserQuestion dismissal or any denied tool — therefore arrives as two
# frames. The mapper used to reset its streamed-block dedupe set at the end of
# EVERY assistant frame and to read the block index off the in-frame position
# (always 0), so the second frame's block was re-emitted whole: the same text
# rendered twice in the chat, and the message's usage counted twice.

_SPLIT_SID = "ses_split"


def _se_frame(event: dict) -> dict:
    return {"type": "stream_event", "session_id": _SPLIT_SID, "event": event}


def _assistant_frame(blocks: list[dict], msg_id: str = "msg_split") -> dict:
    return {
        "type": "assistant",
        "session_id": _SPLIT_SID,
        "message": {
            "id": msg_id,
            "model": "claude-fake",
            "content": blocks,
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 7, "output_tokens": 11,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            },
        },
    }


def _thinking_then_text_frames(text: str = "Looks like you dismissed the prompt.") -> list[dict]:
    """The real CLI frame sequence for one thinking+text message, partial
    streaming on: per-block stream events, each followed by that block's own
    ``assistant`` frame."""
    return [
        _se_frame({"type": "message_start", "message": {"usage": {}}}),
        _se_frame({"type": "content_block_start", "index": 0,
                   "content_block": {"type": "thinking"}}),
        _se_frame({"type": "content_block_delta", "index": 0,
                   "delta": {"type": "thinking_delta", "thinking": "weighing it"}}),
        _se_frame({"type": "content_block_stop", "index": 0}),
        _assistant_frame([{"type": "thinking", "thinking": "weighing it", "signature": "sig"}]),
        _se_frame({"type": "content_block_start", "index": 1,
                   "content_block": {"type": "text"}}),
        _se_frame({"type": "content_block_delta", "index": 1,
                   "delta": {"type": "text_delta", "text": text}}),
        _se_frame({"type": "content_block_stop", "index": 1}),
        _assistant_frame([{"type": "text", "text": text}]),
    ]


def _map_frames(frames: list[dict]) -> list:
    from omni.engine.delegated_proto import StreamJsonMapper

    mapper = StreamJsonMapper(
        session_id=_SPLIT_SID, turn_id="turn_split", model="claude-fake",
        user_node_id="node_0",
    )
    out: list = []
    for frame in frames:
        out.extend(mapper.feed(frame).events)
    return out


def test_split_assistant_frames_emit_each_block_once():
    """Streamed blocks are not re-emitted by their own per-block frame."""
    text = "Looks like you dismissed the prompt."
    events = _map_frames(_thinking_then_text_frames(text))

    texts = [e for e in events if isinstance(e, lm.TextDelta)]
    assert [e.text for e in texts] == [text]
    thinking = [e for e in events if isinstance(e, lm.ThinkingDelta)]
    assert [e.text for e in thinking] == ["weighing it"]


def test_split_assistant_frames_count_usage_once():
    """Every per-block frame repeats the message-level usage verbatim; only the
    first may become a Usage event or the turn double-counts tokens and cost."""
    events = _map_frames(_thinking_then_text_frames())
    assert len([e for e in events if isinstance(e, lm.Usage)]) == 1


def test_split_assistant_frames_dedupe_survives_batched_ordering():
    """Same guarantee when the CLI flushes both per-block frames after the whole
    message streamed (ordering is not part of the contract)."""
    text = "batched ordering"
    stream, frames = [], _thinking_then_text_frames(text)
    assistant_frames = [f for f in frames if f["type"] == "assistant"]
    stream = [f for f in frames if f["type"] != "assistant"]
    events = _map_frames(stream + assistant_frames)

    assert [e.text for e in events if isinstance(e, lm.TextDelta)] == [text]
    assert len([e for e in events if isinstance(e, lm.Usage)]) == 1


def test_split_assistant_frames_next_message_is_not_suppressed():
    """The dedupe set is cleared per MESSAGE, so a following message whose block
    indices repeat still renders."""
    frames = _thinking_then_text_frames("first message")
    frames += [
        _se_frame({"type": "message_start", "message": {"usage": {}}}),
        _se_frame({"type": "content_block_start", "index": 0,
                   "content_block": {"type": "text"}}),
        _se_frame({"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": "second message"}}),
        _se_frame({"type": "content_block_stop", "index": 0}),
        _assistant_frame([{"type": "text", "text": "second message"}], msg_id="msg_split_2"),
    ]
    events = _map_frames(frames)

    assert [e.text for e in events if isinstance(e, lm.TextDelta)] == [
        "first message", "second message",
    ]
    assert len([e for e in events if isinstance(e, lm.Usage)]) == 2


def test_split_assistant_frames_without_partial_streaming_still_emit():
    """Partial streaming off: no stream events exist to suppress against, so
    every per-block frame must still produce its block."""
    events = _map_frames([
        _assistant_frame([{"type": "thinking", "thinking": "weighing it", "signature": "s"}]),
        _assistant_frame([{"type": "text", "text": "no partials here"}]),
    ])

    assert [e.text for e in events if isinstance(e, lm.TextDelta)] == ["no partials here"]
    assert [e.text for e in events if isinstance(e, lm.ThinkingDelta)] == ["weighing it"]
    assert len([e for e in events if isinstance(e, lm.Usage)]) == 1


@pytest.mark.asyncio
async def test_adapter_surfaces_one_assistant_message_for_split_frames():
    """End of the pipe: the app sees a single streamed text + a single complete
    AssistantMessage, which is what the chat renders."""
    mapped = _map_frames(_thinking_then_text_frames("one bubble only"))
    engine = FakeEngine(mapped + [lm.TurnComplete(
        session_id=_SPLIT_SID, node_id="n", turn_id="turn_split",
        stop_reason="end_turn", num_model_calls=1, usage={}, cost_usd=0.0,
    )])
    adapter = _adapter_with(engine)
    adapter._options = SessionOptions(model="fake-model", include_partial_messages=True)

    events = await _collect(adapter)

    text_messages = [
        e for e in events
        if isinstance(e, AssistantMessage)
        and any(isinstance(b, TextBlock) for b in e.content)
    ]
    assert len(text_messages) == 1
    assert text_messages[0].content[0].text == "one bubble only"

    deltas = [
        e for e in events
        if isinstance(e, StreamEvent)
        and e.event.get("delta", {}).get("type") == "text_delta"
    ]
    assert [d.event["delta"]["text"] for d in deltas] == ["one bubble only"]


# ─── 8. Usage-header capture on the native legs ──────────────────────────────
#
# Flag-on native traffic never touches the translator gateway, so the gateway's
# passive header capture (upstream.py) never runs and provider_usage_snapshots
# goes stale. _usage_capture_http_client restores it via an httpx response hook
# injected into the omni client.


CODEX_HEADERS = {
    "x-codex-primary-used-percent": "12",
    "x-codex-primary-window-minutes": "300",
}


class _RecordingCapture:
    """Stand-in for provider_usage.capture_*_headers that records its calls."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, provider_id, headers) -> None:
        self.calls.append((provider_id, dict(headers)))


@pytest.mark.asyncio
async def test_usage_capture_hook_routes_codex_headers(monkeypatch):
    """The injected client carries exactly one response hook, and it forwards a
    Codex response's headers to capture_codex_headers."""
    import httpx

    from app.core import provider_usage
    import app.core.provider.harness_adapter as ha

    codex = _RecordingCapture()
    monkeypatch.setattr(provider_usage, "capture_codex_headers", codex)

    client = ha._usage_capture_http_client("prov-123", True)
    try:
        hooks = client.event_hooks["response"]
        assert len(hooks) == 1
        await hooks[0](httpx.Response(200, headers=dict(CODEX_HEADERS)))
    finally:
        await client.aclose()

    assert len(codex.calls) == 1
    provider_id, headers = codex.calls[0]
    assert provider_id == "prov-123"
    assert headers["x-codex-primary-used-percent"] == "12"
    assert headers["x-codex-primary-window-minutes"] == "300"


@pytest.mark.asyncio
async def test_usage_capture_hook_routes_openai_headers(monkeypatch):
    """use_codex_backend=False routes to the OpenAI-platform capture only."""
    import httpx

    from app.core import provider_usage
    import app.core.provider.harness_adapter as ha

    codex, openai = _RecordingCapture(), _RecordingCapture()
    monkeypatch.setattr(provider_usage, "capture_codex_headers", codex)
    monkeypatch.setattr(provider_usage, "capture_openai_headers", openai)

    client = ha._usage_capture_http_client("prov-openai", False)
    try:
        hook = client.event_hooks["response"][0]
        await hook(httpx.Response(200, headers={"x-ratelimit-remaining-requests": "9"}))
    finally:
        await client.aclose()

    assert codex.calls == []
    assert len(openai.calls) == 1
    assert openai.calls[0][0] == "prov-openai"
    assert openai.calls[0][1]["x-ratelimit-remaining-requests"] == "9"


@pytest.mark.asyncio
async def test_usage_capture_hook_swallows_capture_errors(monkeypatch):
    """Capture is best-effort: a raising capture must never break the turn."""
    import httpx

    from app.core import provider_usage
    import app.core.provider.harness_adapter as ha

    def boom(provider_id, headers):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(provider_usage, "capture_codex_headers", boom)

    client = ha._usage_capture_http_client("prov-123", True)
    try:
        hook = client.event_hooks["response"][0]
        await hook(httpx.Response(429, headers=dict(CODEX_HEADERS)))
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_build_provider_client_injects_capture_client(monkeypatch):
    """A bound openai_responses provider gets the capture client wired into the
    omni client (client._http) and handed back for the caller to close."""
    import app.core.provider.harness_adapter as ha
    from app.core.provider.omni_credentials import ProviderCredentials

    class _DummyAuth:
        account_id = "acct_1"

        async def get_access_token(self) -> str:
            return "tok"

        async def on_401(self) -> None:
            pass

    async def fake_resolve(kind, provider_id):
        assert kind == "openai_responses"
        assert provider_id == "prov-123"
        return ProviderCredentials(api_key=None, auth=_DummyAuth(), use_codex_backend=True)

    monkeypatch.setattr(ha, "resolve_provider_credentials", fake_resolve)

    options = SessionOptions(model="gpt-5-codex", extra={"provider_id": "prov-123"})
    client, http_client = await ha._build_provider_client("openai_responses", options)
    try:
        assert http_client is not None
        assert client._http is http_client
        assert client._base_url == ha.CODEX_RESPONSES_URL
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_build_provider_client_env_fallback_has_no_capture_client(monkeypatch):
    """No bound provider row → nothing to attribute usage to, so no capture
    client is injected and the omni client keeps its own default transport."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setenv("OPENAI_API_KEY", "test")

    client, http_client = await ha._build_provider_client(
        "openai_responses", SessionOptions(model="gpt-5")
    )

    assert http_client is None
    assert client._http is None


@pytest.mark.asyncio
async def test_disconnect_closes_capture_http_client():
    """Teardown owns the injected client — the omni client never closes one it
    did not create."""
    closed = {"count": 0}

    class _StubEngine:
        async def aclose(self) -> None:
            pass

    class _StubHttp:
        async def aclose(self) -> None:
            closed["count"] += 1

    adapter = HarnessAdapter()
    adapter._engine = _StubEngine()
    adapter._connected = True
    adapter._provider_http = _StubHttp()

    await adapter.disconnect()

    assert closed["count"] == 1
    assert adapter._provider_http is None
    assert adapter._connected is False
