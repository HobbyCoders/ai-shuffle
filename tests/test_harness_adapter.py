"""Unit tests for HarnessAdapter — the fluid-backed ProviderAdapter.

These tests are runnable with the fluid venv (no claude_agent_sdk required, no
live API calls). They exercise:

  1. The fluid Event -> ProviderEvent translation table, one case per event type.
  2. Iterator completion on terminal fluid events (TurnComplete/Interrupted/Error).
  3. The permission round-trip: a fluid PermissionRequest surfaces as a
     PermissionRequestEvent, and respond_to_permission() submits a matching
     fluid RespondPermission op back to the engine (tool_use_id -> request_id).
  4. An end-to-end turn over a REAL NativeLoop driven by a fake provider that
     replays StreamChunks (text turn -> deltas + usage + result).
  5. The provider-kind decision logic.
  6. The SDK @tool -> fluid InprocTool bridge.

Run:  cd /home/owner/Documents/AI-Shuffle && \
      /home/owner/Documents/fluid-harness/.venv/bin/python -m pytest \
          tests/test_harness_adapter.py -q
"""

from __future__ import annotations

import dataclasses

import pytest

from app.core.model_registry import resolve_api_model_id
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

from fluid.events import model as lm
from fluid.events.ops import RespondPermission, UserTurn
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore

SID = "ses_test"


# ─── Fakes ────────────────────────────────────────────────────────────────────


class FakeEngine:
    """Minimal stand-in for a fluid engine: scripted events + recorded ops."""

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
    """A fluid ProviderClient replaying a fixed list of StreamChunks."""

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
    # A fluid Usage reading rides on the next synthesized AssistantMessage in
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
    """fluid may report cost_usd=None (NativeLoop never sets it; the delegated
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
    always carries a float there, so these must not inherit the None default.

    These events carry no cost (cost_usd defaults to None), which exercises the
    None -> 0.0 coercion the session-row `col + value` update depends on."""
    got = await _collect(_adapter_with(FakeEngine([
        lm.Interrupted(session_id=SID, message="stopped")])))
    assert got[0].total_cost_usd == 0.0

    got = await _collect(_adapter_with(FakeEngine([
        lm.ErrorEvent(session_id=SID, message="boom", retriable=True)])))
    assert got[0].total_cost_usd == 0.0


@pytest.mark.asyncio
async def test_interrupted_carries_partial_usage_and_cost():
    """A turn-ending Interrupted carries the turn's accumulated partial
    usage/cost; both must reach the ResultMessage so the interrupted turn stops
    undercounting the session totals."""
    got = await _collect(_adapter_with(FakeEngine([
        lm.Interrupted(session_id=SID, message="stopped",
                       usage={"input_tokens": 10, "output_tokens": 5},
                       cost_usd=0.002)])))
    assert len(got) == 1 and isinstance(got[0], ResultMessage)
    assert got[0].total_cost_usd == pytest.approx(0.002)
    assert got[0].usage["input_tokens"] == 10
    assert got[0].usage["output_tokens"] == 5


@pytest.mark.asyncio
async def test_error_event_carries_partial_usage_and_cost():
    got = await _collect(_adapter_with(FakeEngine([
        lm.ErrorEvent(session_id=SID, message="boom", retriable=False,
                      usage={"input_tokens": 7, "output_tokens": 2},
                      cost_usd=0.0015)])))
    assert len(got) == 1 and isinstance(got[0], ResultMessage)
    assert got[0].is_error is True
    assert got[0].total_cost_usd == pytest.approx(0.0015)
    assert got[0].usage["input_tokens"] == 7
    assert got[0].usage["output_tokens"] == 2


# ─── 1c. Context usage ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_context_usage_none_without_engine():
    assert await HarnessAdapter().get_context_usage() is None


@pytest.mark.asyncio
async def test_get_context_usage_none_when_engine_cannot_answer():
    """An engine double WITHOUT the method still degrades to None instead of
    raising AttributeError (which used to 500 GET .../context-usage).

    The real engines all answer now — see
    ``test_vendored_engines_expose_get_context_usage``. This only pins the
    getattr-probe fallback that keeps doubles and any future leg safe."""
    adapter = _adapter_with(FakeEngine([]))
    assert not hasattr(adapter._engine, "get_context_usage")
    assert await adapter.get_context_usage() is None


def test_vendored_engines_expose_get_context_usage():
    """Premise flip (vendored fluid a6957a1): BOTH legs implement the breakdown
    now — the native loop computes it, the delegated loop proxies the claude
    CLI's. The adapter's probe is a live forwarding path, not dead code.

    This test is the tripwire if a future vendor sync drops the capability.
    """
    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.native import NativeLoop

    assert callable(getattr(NativeLoop, "get_context_usage", None))
    assert callable(getattr(DelegatedLoop, "get_context_usage", None))


@pytest.mark.asyncio
async def test_get_context_usage_forwards_real_native_breakdown():
    """Over a REAL NativeLoop the adapter returns fluid's category breakdown,
    priced against the context_window the adapter resolved at connect."""
    from fluid.engine.native import NativeLoop

    loop = NativeLoop(
        provider=FakeProvider([]),
        store=MemoryStore(),
        model="fake",
        system="You are a helpful assistant.",
        context_window=1_000_000,
    )
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    await loop._ensure_session()
    adapter._connected = True

    usage = await adapter.get_context_usage()
    assert usage is not None

    # Rich per-category rows (what the UI renders instead of a token-type
    # estimate), not a bare total.
    names = {c["name"] for c in usage["categories"]}
    assert "System prompt" in names
    assert "Free space" in names
    assert all({"name", "tokens", "color"} <= set(c) for c in usage["categories"])

    # The window flows through to every window-derived field.
    assert usage["maxTokens"] == 1_000_000
    assert usage["rawMaxTokens"] == 1_000_000
    assert 0 < usage["totalTokens"] < 1_000_000
    assert usage["percentage"] == pytest.approx(
        round(usage["totalTokens"] / 1_000_000 * 100, 1)
    )

    # Keys the app's consumers read. fluid trims rather than autocompacting,
    # so isAutoCompactEnabled is False and memoryFiles is always a list.
    assert usage["model"] == "fake"
    assert usage["isAutoCompactEnabled"] is False
    assert usage["memoryFiles"] == []

    await adapter.disconnect()


@pytest.mark.asyncio
async def test_get_context_usage_unknown_window_reports_none_maxtokens():
    """No resolvable window → fluid reports maxTokens None rather than
    guessing, and omits the "Free space" row. The adapter forwards that
    honestly; the UI's own fallback takes over."""
    from fluid.engine.native import NativeLoop

    loop = NativeLoop(
        provider=FakeProvider([]), store=MemoryStore(), model="fake", system="hi"
    )
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    await loop._ensure_session()
    adapter._connected = True

    usage = await adapter.get_context_usage()
    assert usage["maxTokens"] is None
    assert usage["percentage"] is None
    assert "Free space" not in {c["name"] for c in usage["categories"]}

    await adapter.disconnect()


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
    """With an app can_use_tool callback wired (the WS/chat path), a fluid
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


# ─── 3b. Question dismissal / answers survive the fluid permission bridge ─────
#
# On the fluid path an AskUserQuestion call is gated as a PermissionRequest, so
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

    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.delegated_proto import PermissionBridge
    from app.core.user_question_handler import DISMISS_MESSAGE

    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake", claude_cmd=["true"])
    frames: list = []

    async def _capture(frame):
        frames.append(frame)

    loop._write_frame = _capture  # type: ignore[assignment]

    bridge = PermissionBridge(
        cli_request_id="cli_1", fluid_request_id="p1",
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
    from fluid.engine.delegated import DelegatedLoop

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

    from fluid.engine.delegated import DelegatedLoop
    from fluid.engine.delegated_proto import PermissionBridge

    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake", claude_cmd=["true"])
    frames: list = []

    async def _capture(frame):
        frames.append(frame)

    loop._write_frame = _capture  # type: ignore[assignment]

    bridge = PermissionBridge(
        cli_request_id="cli_4", fluid_request_id="p2",
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
    from fluid.engine.native import NativeLoop

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

    # sdk_session_id resolves to the live fluid session id.
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
        SessionOptions(model="claude-x", extra={"fluid_engine": "native", "fluid_provider": "anthropic"})
    ) == "anthropic"
    assert _decide_provider_kind(
        SessionOptions(model="gpt-4o", extra={"fluid_provider": "openrouter"})
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
        _decide_provider_kind(SessionOptions(model="fable", extra={"fluid_engine": "native"}))
        == "anthropic"
    )


# ─── 4b. Provider-ROW-shape preference (PA13 / W-R4) ─────────────────────────


def _bound(model, provider_type, auth_mode=None, provider_id="pid", api_dialect=None):
    extra = {"provider_id": provider_id, "provider_type": provider_type}
    if auth_mode:
        extra["provider_auth_mode"] = auth_mode
    if api_dialect:
        extra["provider_dialect"] = api_dialect
    return SessionOptions(model=model, extra=extra)


def test_provider_kind_prefers_the_bound_row_type_over_the_model_sniff():
    """The model-string sniff reads an id in isolation and mis-legs every
    cross-provider pick it can't see the provider for. When the session carries a
    bound provider row, the row's ``provider_type`` decides."""
    # A bare claude id on an OpenRouter row sniffs as the DELEGATED claude CLI —
    # the single worst misroute (wrong engine AND wrong credentials).
    assert _decide_provider_kind(_bound("claude-3.5-sonnet", "openrouter")) == "openrouter"
    # A short registry alias, same story.
    assert _decide_provider_kind(_bound("opus", "openrouter")) == "openrouter"
    # A slugged model on an OpenAI row sniffs as OpenRouter.
    assert _decide_provider_kind(_bound("some-vendor/some-model", "openai")) == "openai_responses"
    # A non-prefixed model on an OpenAI row would otherwise fall through to the
    # bare `return "anthropic"` (an API-key-less Anthropic client → instant 401).
    assert _decide_provider_kind(_bound("my-tuned-model", "openai")) == "openai_responses"
    # A ChatGPT/Codex subscription row, in both of its shapes.
    assert _decide_provider_kind(_bound("gpt-5-codex", "chatgpt")) == "openai_responses"
    assert _decide_provider_kind(_bound("gpt-5-codex", "custom", "chatgpt")) == "openai_responses"


def test_provider_kind_anthropic_row_respects_the_subscription_constraint():
    """An Anthropic-API-KEY row drives the native anthropic client; a
    SUBSCRIPTION-auth row must stay on the delegated claude CLI — Anthropic bans
    third-party use of subscription tokens and ``resolve_provider_credentials``
    raises for kind='anthropic' on such a row."""
    assert _decide_provider_kind(_bound("claude-sonnet-4-5", "anthropic")) == "anthropic"
    for mode in ("oauth", "subscription", "claude", "max", "pro", "claude_subscription"):
        assert (
            _decide_provider_kind(_bound("claude-sonnet-4-5", "anthropic", mode))
            == "delegated"
        ), mode


def test_provider_kind_explicit_hints_still_outrank_the_row():
    """The fluid_engine / fluid_provider escape hatches keep top priority."""
    opts = _bound("claude-3.5-sonnet", "openrouter")
    opts.extra["fluid_engine"] = "delegated"
    assert _decide_provider_kind(opts) == "delegated"

    opts2 = _bound("gpt-5", "openai")
    opts2.extra["fluid_provider"] = "openrouter"
    assert _decide_provider_kind(opts2) == "openrouter"


def test_provider_kind_legacy_omni_keys_still_honored(monkeypatch):
    """Pre-rename ``omni_*`` extra keys and AIH_OMNI_PROVIDER keep working."""
    opts = _bound("claude-3.5-sonnet", "openrouter")
    opts.extra["omni_engine"] = "delegated"
    assert _decide_provider_kind(opts) == "delegated"

    opts2 = _bound("gpt-5", "openai")
    opts2.extra["omni_provider"] = "openrouter"
    assert _decide_provider_kind(opts2) == "openrouter"

    # the new key outranks a stale legacy one
    opts3 = _bound("gpt-5", "openai")
    opts3.extra["fluid_provider"] = "anthropic"
    opts3.extra["omni_provider"] = "openrouter"
    assert _decide_provider_kind(opts3) == "anthropic"

    monkeypatch.setenv("AIH_OMNI_PROVIDER", "openrouter")
    opts4 = _bound("gpt-5", "openai")
    assert _decide_provider_kind(opts4) == "openrouter"


def test_provider_kind_custom_row_is_the_anthropic_compatible_native_leg():
    """A ``custom`` row is an Anthropic-Messages-compatible endpoint by the
    app's own contract (the delegated CLI pointed ANTHROPIC_BASE_URL at it),
    so it always drives the native anthropic client at the row's base_url —
    NEVER the model-string sniff. The sniff sent a custom row's non-prefixed
    model ("grok-4.5") to the REAL Anthropic API with the row's foreign key:
    an instant silent 401 and a turn that completed with zero output."""
    assert _decide_provider_kind(_bound("grok-4.5", "custom")) == "anthropic"
    # Model ids that LOOK like other vendors' stay on the row's endpoint too —
    # the row is authoritative for both endpoint and credentials.
    assert _decide_provider_kind(_bound("gpt-4o", "custom")) == "anthropic"
    assert _decide_provider_kind(_bound("claude-sonnet-4-5", "custom")) == "anthropic"
    # ...except the Codex-subscription shape (custom + chatgpt auth), which is
    # its own leg and is asserted in the row-outranks-sniff test above.


def test_provider_kind_custom_row_openai_dialect_is_chat_completions():
    """api_dialect='openai' flips a custom row onto fluid's generic OpenAI
    chat/completions client (the 'openrouter' kind) at the row's base_url —
    xAI-direct / Groq / vLLM endpoints. An explicit 'anthropic' (or garbage)
    keeps the default Messages leg."""
    assert (
        _decide_provider_kind(_bound("grok-4.5", "custom", api_dialect="openai"))
        == "openrouter"
    )
    assert (
        _decide_provider_kind(_bound("grok-4.5", "custom", api_dialect="anthropic"))
        == "anthropic"
    )
    assert (
        _decide_provider_kind(_bound("grok-4.5", "custom", api_dialect="weird"))
        == "anthropic"
    )


def test_chat_completions_url_composition():
    from app.core.provider.harness_adapter import _chat_completions_url

    assert _chat_completions_url("https://api.x.ai") == "https://api.x.ai/v1/chat/completions"
    assert _chat_completions_url("https://api.x.ai/") == "https://api.x.ai/v1/chat/completions"
    assert _chat_completions_url("https://api.x.ai/v1") == "https://api.x.ai/v1/chat/completions"
    # OpenRouter's stored base composes to the client's own default URL.
    assert (
        _chat_completions_url("https://openrouter.ai/api")
        == "https://openrouter.ai/api/v1/chat/completions"
    )


def test_provider_kind_unknown_row_type_falls_back_to_the_sniff():
    """A row shape the mapping doesn't recognise must not swallow the decision."""
    assert _decide_provider_kind(_bound("gpt-4o", "weird-new-type")) == "openai_responses"
    assert _decide_provider_kind(_bound("claude-sonnet-4-5", "weird-new-type")) == "delegated"


def test_provider_kind_looks_the_row_up_when_the_shape_is_missing(monkeypatch):
    """A SessionOptions built without the stamped shape fields (a hand-built
    options object) still gets the row-type preference via a guarded lookup."""
    import app.db.database as db

    monkeypatch.setattr(
        db, "get_model_provider", lambda pid, include_token=False: {
            "id": pid, "provider_type": "openrouter", "auth_mode": None
        }
    )
    opts = SessionOptions(model="claude-3.5-sonnet", extra={"provider_id": "pid"})
    assert _decide_provider_kind(opts) == "openrouter"


def test_provider_kind_lookup_failure_falls_back_to_the_sniff(monkeypatch):
    import app.db.database as db

    def _boom(pid, include_token=False):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_model_provider", _boom)
    opts = SessionOptions(model="gpt-4o", extra={"provider_id": "pid"})
    assert _decide_provider_kind(opts) == "openai_responses"


def test_provider_kind_native_session_is_unchanged():
    """No bound provider → the sniff is still the whole decision."""
    assert _decide_provider_kind(SessionOptions(model="sonnet")) == "delegated"
    assert _decide_provider_kind(SessionOptions(model="gpt-4o")) == "openai_responses"


@pytest.mark.asyncio
async def test_delegated_loop_kwargs_filter_sdk_servers_and_stream_partial(monkeypatch):
    """The delegated leg JSON-serializes its mcp_servers map into --mcp-config,
    so in-proc type='sdk' entries (live Server objects) must be filtered out;
    and the SDK's include_partial_messages must reach ``partial``."""
    import json

    import app.core.provider.harness_adapter as ha
    from fluid.tools.registry import ToolRegistry

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
        extra={"fluid_registry": ToolRegistry()},
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

    # extra["fluid_partial"] remains a manual override when the SDK flag is off.
    await adapter.connect(
        dataclasses.replace(
            options,
            include_partial_messages=False,
            extra={"fluid_registry": ToolRegistry(), "fluid_partial": True},
        )
    )
    assert captured["partial"] is True

    await adapter.connect(dataclasses.replace(options, include_partial_messages=False))
    assert captured["partial"] is False


# ─── 7. Builtin file/shell tools on the native leg ───────────────────────────

_FLUID_BUILTINS = {"Read", "Grep", "Write", "Edit", "Bash", "Skill", "Note"}


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
async def test_native_leg_registry_has_fluid_builtins():
    """Native legs talk to the provider API directly — without fluid's builtins
    the model has no file/shell tools at all."""
    adapter = HarnessAdapter()
    registry = await adapter._build_registry(SessionOptions(model="gpt-4o"), native=True)
    assert registry is not None
    assert _FLUID_BUILTINS <= _names(registry)


@pytest.mark.asyncio
async def test_delegated_leg_registry_has_no_fluid_builtins():
    """The claude CLI ships its own Read/Bash/... — registering fluid's here
    would double up on the delegated leg."""
    adapter = HarnessAdapter()
    options = SessionOptions(
        model="fable", extra={"fluid_inproc_servers": [_FakeInprocServer()]}
    )
    registry = await adapter._build_registry(options, native=False)
    assert registry is not None
    names = _names(registry)
    assert "mcp__fake__Ping" in names
    assert not (_FLUID_BUILTINS & names)

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
        SessionOptions(model="gpt-4o", extra={"fluid_provider": "openai_responses"})
    )
    assert adapter._is_delegated is False
    assert _FLUID_BUILTINS <= _names(captured["registry"])


# ─── Native-leg context window (feeds get_context_usage) ─────────────────────


def test_native_context_window_uses_the_claude_registry_for_claude_slugs():
    """No bound provider row (env-credentialed native Claude) → the model
    registry's window. Resolution keys on the REGISTRY slug, not the API id."""
    from app.core.provider.harness_adapter import _native_context_window

    for slug in ("sonnet", "opus", "fable"):
        opts = SessionOptions(model=slug, extra={"fluid_engine": "native"})
        assert _native_context_window("anthropic", opts) == 1_000_000
    haiku = SessionOptions(model="haiku", extra={"fluid_engine": "native"})
    assert _native_context_window("anthropic", haiku) == 200_000


def test_native_context_window_none_for_an_unknown_model():
    """An unregistered model on no row has no knowable window. None (not a
    200k guess) — fluid then reports maxTokens None instead of pricing against
    a window that isn't real."""
    from app.core.provider.harness_adapter import _native_context_window

    assert _native_context_window("openrouter", SessionOptions(model="mystery-9")) is None


def test_native_context_window_resolves_through_the_bound_provider_row(monkeypatch):
    """Provider-backed native legs resolve via the SHARED
    ``resolve_provider_context_window`` — the model's own models_json entry
    beats the row's column, which is the whole point of #874 (a row that
    advertises 272k must not pin a 1M model down to 272k)."""
    import app.core.provider.harness_adapter as ha
    from app.db import database as db

    row = {
        "id": "pid",
        "provider_type": "openai",
        "context_window": 272_000,
        "models_json": [{"id": "gpt-5.6-sol", "context_window": 1_000_000}],
    }
    monkeypatch.setattr(db, "get_model_provider", lambda pid, include_token=False: row)

    bound = SessionOptions(model="gpt-5.6-sol", extra={"provider_id": "pid"})
    assert ha._native_context_window("openai_responses", bound) == 1_000_000

    # A model with no entry of its own falls back to the row's column.
    other = SessionOptions(model="gpt-4o-mini", extra={"provider_id": "pid"})
    assert ha._native_context_window("openai_responses", other) == 272_000


def test_native_context_window_survives_a_broken_row_lookup(monkeypatch):
    """A DB failure must never break connect: it degrades to the Claude
    registry (or None), it does not raise."""
    import app.core.provider.harness_adapter as ha
    from app.db import database as db

    def _boom(pid, include_token=False):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_model_provider", _boom)

    claude = SessionOptions(model="opus", extra={"provider_id": "pid"})
    assert ha._native_context_window("anthropic", claude) == 1_000_000
    other = SessionOptions(model="mystery-9", extra={"provider_id": "pid"})
    assert ha._native_context_window("openrouter", other) is None


@pytest.mark.asyncio
async def test_connect_native_leg_passes_resolved_context_window(monkeypatch):
    """End of the wire: connect() hands NativeLoop the resolved window, which
    is what makes the leg's get_context_usage() report a real maxTokens."""
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
        SessionOptions(model="opus", extra={"fluid_engine": "native"})
    )
    assert captured["context_window"] == 1_000_000
    # The window is the INPUT budget — it must not be confused with max_tokens
    # (the OUTPUT cap), which stays unset so fluid keeps its own default.
    assert "max_tokens" not in captured
    # The loop still gets the API-resolved model id, not the registry slug.
    assert captured["model"] == resolve_api_model_id("opus")


@pytest.mark.asyncio
async def test_on_write_policy_gates_builtin_bash_but_not_read():
    """The permission prompt only becomes reachable once the builtins exist:
    Bash is risk='write' (ask), Read is risk='safe' (allow)."""
    from fluid.permissions.engine import PermissionEngine

    adapter = HarnessAdapter()
    registry = await adapter._build_registry(SessionOptions(model="gpt-4o"), native=True)
    engine = PermissionEngine(approval_policy="on_write")

    assert engine.evaluate(registry.get("Bash"), {"command": "ls"}) == "ask"
    assert engine.evaluate(registry.get("Read"), {"file_path": "/tmp/x"}) == "allow"


# ─── P1 prompt compiler integration (rulings A1/A3/A12) ──────────────────────


class _CapturingLoop:
    """Shared fake for NativeLoop/DelegatedLoop kwarg capture."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)

    async def _ensure_session(self):
        class _Meta:
            session_id = "ses_x"

        return _Meta()

    async def aclose(self):
        pass


async def _fake_provider_client(kind, options):
    return FakeProvider([]), None


@pytest.mark.asyncio
async def test_native_leg_system_prompt_is_compiled(monkeypatch, tmp_path):
    """Native legs always run the fluid-compiled prompt: base first, append
    text as a block, env gathered by fluid."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-4o",
            system_prompt="I am the configured agent prompt.",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    system = _CapturingLoop.captured["system"]
    assert system.startswith("I am the configured agent prompt.")
    assert "# Environment" in system
    assert f"Working directory: {tmp_path.resolve()}" in system


@pytest.mark.asyncio
async def test_native_leg_preset_append_rides_as_block(monkeypatch, tmp_path):
    """A preset-shaped profile has no base; its append text (security +
    instructions) must still appear in the compiled output, after env."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-4o",
            system_prompt={"type": "preset", "preset": "claude_code", "append": "SECURITY BLOCK"},
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    system = _CapturingLoop.captured["system"]
    assert "SECURITY BLOCK" in system
    assert system.index("# Environment") < system.index("SECURITY BLOCK")


@pytest.mark.asyncio
async def test_native_leg_passes_compiled_sections_for_context_usage(
    monkeypatch, tmp_path
):
    """connect() hands NativeLoop the SAME compilation twice: the joined text
    as ``system`` and its labeled sections as ``system_sections`` — the latter
    is what lets get_context_usage() price Memory files / Skills as their own
    rows (Claude-Code-style source split) instead of one opaque blob."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    (tmp_path / "CLAUDE.md").write_text("Project rule: be terse.", encoding="utf-8")
    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-4o",
            system_prompt="I am the configured agent prompt.",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    compiled = _CapturingLoop.captured["system_sections"]
    assert compiled is not None
    # Same compilation, two views — the text the engine sends is exactly the
    # joined sections, so the breakdown can never price a different prompt.
    assert compiled.text == _CapturingLoop.captured["system"]
    kinds = [s.kind for s in compiled.sections]
    assert "base" in kinds and "environment" in kinds
    assert any(
        s.kind == "memory" and s.path and s.path.endswith("CLAUDE.md")
        for s in compiled.sections
    )


@pytest.mark.asyncio
async def test_native_leg_compiler_failure_degrades_sections_to_none(
    monkeypatch, tmp_path
):
    """Compiler failure keeps the string fallback AND passes sections=None —
    the breakdown then prices the whole prompt as "System prompt" instead of
    the session failing to start."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    monkeypatch.setattr(
        ha.HarnessAdapter,
        "_compile_prompt_sections",
        staticmethod(lambda system, append_system, options: None),
    )

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-4o",
            system_prompt="I am the configured agent prompt.",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    assert _CapturingLoop.captured["system_sections"] is None
    assert _CapturingLoop.captured["system"] == "I am the configured agent prompt."


@pytest.mark.asyncio
async def test_native_leg_app_env_block_not_duplicated(monkeypatch, tmp_path):
    """inject_env_details profiles already carry the app's <env> block —
    fluid's env section must be skipped, not doubled."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    base = "PROMPT\n\n<env>\nWorking directory: /w\n</env>"
    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-4o",
            system_prompt=base,
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    system = _CapturingLoop.captured["system"]
    assert "<env>" in system
    assert "# Environment" not in system


# ─── Native Anthropic leg: registry alias → API model id ─────────────────────


class _SwapLoop(_CapturingLoop):
    """_CapturingLoop plus the NativeLoop hot-swap contract: ``submit`` is
    unimplemented, so set_model falls through to the ``model`` attribute."""

    model = None

    async def submit(self, _op):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_anthropic_native_leg_canonicalizes_the_model_alias(monkeypatch, tmp_path):
    """fluid's Anthropic client sends the model verbatim to the Messages API —
    a bare registry alias would 404 there."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="opus",
            cwd=str(tmp_path),
            extra={"fluid_engine": "native", "fluid_provider": "anthropic"},
        )
    )
    assert _CapturingLoop.captured["model"] == "claude-opus-5"


@pytest.mark.asyncio
async def test_anthropic_native_leg_canonicalizes_legacy_1m_values(monkeypatch, tmp_path):
    """The retired [1m] suffix was never a distinct API model id — a persisted
    value still carrying it must still reach the API as the base model."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="opus[1m]",
            cwd=str(tmp_path),
            extra={"fluid_engine": "native", "fluid_provider": "anthropic"},
        )
    )
    assert _CapturingLoop.captured["model"] == "claude-opus-5"


@pytest.mark.asyncio
async def test_anthropic_native_leg_passes_custom_models_through(monkeypatch, tmp_path):
    """Only registry presets are rewritten — a custom id reaches the API as-is."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="my-custom-model",
            cwd=str(tmp_path),
            extra={"fluid_engine": "native", "fluid_provider": "anthropic"},
        )
    )
    assert _CapturingLoop.captured["model"] == "my-custom-model"


@pytest.mark.asyncio
async def test_other_native_kinds_are_not_canonicalized(monkeypatch, tmp_path):
    """The gating is per provider kind: non-Anthropic native legs run
    provider-native ids that must never be rewritten to a Claude API id."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="opus",
            cwd=str(tmp_path),
            extra={"fluid_engine": "native", "fluid_provider": "openrouter"},
        )
    )
    assert _CapturingLoop.captured["model"] == "opus"


@pytest.mark.asyncio
async def test_set_model_canonicalizes_on_the_anthropic_leg(monkeypatch, tmp_path):
    """Mid-session hot swap lands on NativeLoop's raw model attribute — the
    same verbatim-to-API path as connect."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _SwapLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        SessionOptions(
            model="opus",
            cwd=str(tmp_path),
            extra={"fluid_engine": "native", "fluid_provider": "anthropic"},
        )
    )
    await adapter.set_model("sonnet")
    assert adapter.engine.model == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_delegated_append_mode_bypasses_compiler(monkeypatch, tmp_path):
    """Flag off (default): the delegated leg's system/append are NOT compiled —
    the CLI gathers its own env and instruction files.

    The one thing the app does add here is the agents index (the CLI cannot
    know about DB agents), so the assertion is "the app's text, then at most
    that block" rather than byte-identity — see
    test_delegated_append_mode_advertises_agents.
    """
    import app.core.provider.harness_adapter as ha
    from fluid.tools.registry import ToolRegistry

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="fable",
            system_prompt={"type": "preset", "append": "APPEND TEXT"},
            cwd=str(tmp_path),
            extra={"fluid_registry": ToolRegistry()},
        )
    )
    assert _CapturingLoop.captured["system"] is None
    append = _CapturingLoop.captured["append_system_prompt"]
    assert append.startswith("APPEND TEXT")
    # Compiler markers: none of fluid's gathering may appear on this leg.
    assert "# Environment" not in append
    assert "# Skills" not in append


@pytest.mark.asyncio
async def test_delegated_full_replace_compiles(monkeypatch, tmp_path):
    """PROMPT_FULL_REPLACE=true: the delegated leg gets the compiled prompt
    via --system-prompt and no append."""
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings
    from fluid.tools.registry import ToolRegistry

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(settings, "prompt_full_replace", True)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="fable",
            system_prompt={"type": "preset", "append": "APPEND TEXT"},
            cwd=str(tmp_path),
            extra={"fluid_registry": ToolRegistry()},
        )
    )
    system = _CapturingLoop.captured["system"]
    assert _CapturingLoop.captured["append_system_prompt"] is None
    assert "APPEND TEXT" in system
    assert "# Environment" in system


@pytest.mark.asyncio
async def test_delegated_leg_bans_cli_subagent_tools(monkeypatch, tmp_path):
    """Ruling A6: the CLI's native Task/Agent/Workflow are always disallowed;
    profile-level disallows merge in."""
    import app.core.provider.harness_adapter as ha
    from fluid.tools.registry import ToolRegistry

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="fable",
            cwd=str(tmp_path),
            disallowed_tools=["WebSearch"],
            extra={"fluid_registry": ToolRegistry()},
        )
    )
    banned = set(_CapturingLoop.captured["disallowed_tools"])
    assert {"Task", "Agent", "Workflow", "WebSearch"} <= banned


@pytest.mark.asyncio
async def test_delegated_leg_bans_cli_wake_tools(monkeypatch, tmp_path):
    """The CLI's native wake/cron tools fire turns the delegated client never
    reads — only the bridged app builtin may schedule wakes."""
    import app.core.provider.harness_adapter as ha
    from fluid.tools.registry import ToolRegistry

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="fable",
            cwd=str(tmp_path),
            extra={"fluid_registry": ToolRegistry()},
        )
    )
    banned = set(_CapturingLoop.captured["disallowed_tools"])
    assert {"ScheduleWakeup", "CronCreate", "CronDelete", "CronList"} <= banned


@pytest.mark.asyncio
async def test_add_dirs_reach_delegated_loop(monkeypatch, tmp_path):
    """add_dirs (notes dir et al.) must reach the CLI as --add-dir dirs."""
    import app.core.provider.harness_adapter as ha
    from fluid.tools.registry import ToolRegistry

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="fable",
            cwd=str(tmp_path),
            add_dirs=["/notes", "/extra"],
            extra={"fluid_registry": ToolRegistry()},
        )
    )
    assert _CapturingLoop.captured["add_dirs"] == ["/notes", "/extra"]


def test_store_derived_from_database_presence(monkeypatch):
    """No store flags (2026-08-13 ruling): DATABASE_URL set -> PgFluidStore;
    DB-less (unit tests) -> MemoryStore. The retired AIH_OMNI_STORE env var
    must be ignored."""
    from app.core.config import settings
    from app.core.provider.fluid_pg_store import PgFluidStore
    from fluid.store.base import MemoryStore as FluidMemoryStore

    monkeypatch.setattr(settings, "database_url", "postgresql://REDACTED@h:5432/db")
    store = HarnessAdapter._build_store(SessionOptions(model="m"))
    assert isinstance(store, PgFluidStore)

    monkeypatch.setattr(settings, "database_url", None)
    monkeypatch.setenv("AIH_OMNI_STORE", "pg")  # retired flag — ignored
    store = HarnessAdapter._build_store(SessionOptions(model="m"))
    assert isinstance(store, FluidMemoryStore)


def test_requires_reconnect():
    a = HarnessAdapter()
    a._options = SessionOptions(model="m", system_prompt="S", cwd="/w")
    # model-only change: hot-swappable
    assert a.requires_reconnect(SessionOptions(model="m2")) is False
    # cwd change: structural
    assert a.requires_reconnect(SessionOptions(model="m", cwd="/other")) is True
    # system prompt change: structural
    assert a.requires_reconnect(SessionOptions(model="m", system_prompt="Different")) is True


# ─── 5. SDK @tool -> fluid InprocTool bridge ──────────────────────────────────


@pytest.mark.asyncio
async def test_tool_bridge():
    from app.core.provider.fluid_tool_bridge import (
        register_sdk_tools,
        sdk_tool_to_inproc,
    )
    from fluid.tools.registry import ToolRegistry

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
    """A resumed session must pass the CLI's own uuid, never the fluid id."""
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
    from fluid.engine.delegated_proto import StreamJsonMapper

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
# injected into the fluid client.


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
    fluid client (client._http) and handed back for the caller to close."""
    import app.core.provider.harness_adapter as ha
    from app.core.provider.fluid_credentials import ProviderCredentials

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
    client is injected and the fluid client keeps its own default transport."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setenv("OPENAI_API_KEY", "test")

    client, http_client = await ha._build_provider_client(
        "openai_responses", SessionOptions(model="gpt-5")
    )

    assert http_client is None
    assert client._http is None


@pytest.mark.asyncio
async def test_disconnect_closes_capture_http_client():
    """Teardown owns the injected client — the fluid client never closes one it
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


# ─── 9. Bus seq seeding on resume (durable-event-loss regression) ────────────
#
# fluid's EventBus numbers events off its IN-MEMORY log (seq = len(log) + 1). A
# freshly built engine over a resumed session starts with an empty log, so it
# re-uses seq 1..N — and the durable append is idempotent per (session_id, seq),
# so those events are silently dropped. Headless one-shot turns (ScheduleWakeup
# wakes, automations) build a new adapter per turn and hit this EVERY turn, so
# whole wake turns never reached fluid_session_events (and the live feed).
# connect() now seeds the bus log from the store, restoring seq continuity.

_SEED_CHUNKS = [
    StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
    StreamChunk("block_start", {"index": 0, "block_type": "text"}),
    StreamChunk("text_delta", {"index": 0, "text": "fresh reply"}),
    StreamChunk("block_stop", {"index": 0}),
    StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
]


def _old_event(sid: str, seq: int) -> "lm.TextDelta":
    return lm.TextDelta(session_id=sid, index=0, text=f"old-{seq}", seq=seq)


async def _seeded_store(sid: str, seqs: list[int]) -> MemoryStore:
    store = MemoryStore()
    await store.create_session(sid)
    for seq in seqs:
        await store.append_event(sid, _old_event(sid, seq))
    return store


async def _connect_native(monkeypatch, store, tmp_path, **option_kwargs) -> HarnessAdapter:
    """connect() a REAL NativeLoop (real EventBus) over a fake provider."""
    import app.core.provider.harness_adapter as ha

    async def fake_client(kind, options):
        return FakeProvider(list(_SEED_CHUNKS)), None

    monkeypatch.setattr(ha, "_build_provider_client", fake_client)
    monkeypatch.setattr(HarnessAdapter, "_build_store", staticmethod(lambda _o: store))

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(tmp_path),
            include_partial_messages=True,
            extra={"fluid_provider": "openai_responses"},
            **option_kwargs,
        )
    )
    return adapter


@pytest.mark.asyncio
async def test_resume_seeds_bus_log_and_continues_seq(monkeypatch, tmp_path):
    """A resumed session's new events must persist — i.e. continue at N+1."""
    sid = "ses_seed"
    store = await _seeded_store(sid, [1, 2, 3])

    adapter = await _connect_native(monkeypatch, store, tmp_path, resume_session_id=sid)
    try:
        # The bus log is primed with the durable history, and the replay cursor
        # sits past it so receive_events() will not re-yield old turns.
        assert adapter._event_cursor == 3
        assert [e.seq for e in adapter._engine.bus.log(sid)] == [1, 2, 3]
        assert [e.text for e in adapter._engine.bus.log(sid)] == [
            "old-1", "old-2", "old-3",
        ]

        await adapter.send_query("hi")
        got = await _collect(adapter)

        # (i) no historical event leaks into this turn's stream
        delta_texts = [
            e.event["delta"]["text"]
            for e in got
            if isinstance(e, StreamEvent) and e.event.get("delta", {}).get("type")
            == "text_delta"
        ]
        assert delta_texts == ["fresh reply"]
        assert not any("old-" in t for t in delta_texts)

        # (ii) the turn's events landed durably, numbered strictly after the
        # stored tail — the seq collision that dropped them is gone.
        seqs = [e.seq for e in store._events[sid]]
        assert seqs[:3] == [1, 2, 3]
        new_seqs = seqs[3:]
        assert new_seqs and new_seqs[0] == 4
        assert new_seqs == sorted(new_seqs) and len(set(new_seqs)) == len(new_seqs)
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_fresh_session_starts_at_seq_one(monkeypatch, tmp_path):
    """Nothing stored → nothing to seed; numbering starts at 1 as before."""
    store = MemoryStore()
    adapter = await _connect_native(monkeypatch, store, tmp_path)
    try:
        assert adapter._event_cursor == 0
        sid = adapter._engine.session_id
        assert adapter._engine.bus.log(sid) == []

        await adapter.send_query("hi")
        await _collect(adapter)

        seqs = [e.seq for e in store._events[sid]]
        assert seqs and seqs[0] == 1
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_non_contiguous_store_seeds_prefix_only(monkeypatch, tmp_path, caplog):
    """A holed durable log (the fingerprint of prior drops) seeds only its
    contiguous prefix — the bus indexes its log positionally, so seeding past a
    hole would mis-number everything after it."""
    import logging

    sid = "ses_holed"
    store = await _seeded_store(sid, [1, 2, 5])

    with caplog.at_level(logging.WARNING, logger="app.core.provider.harness_adapter"):
        adapter = await _connect_native(
            monkeypatch, store, tmp_path, resume_session_id=sid
        )
    try:
        assert [e.seq for e in adapter._engine.bus.log(sid)] == [1, 2]
        assert adapter._event_cursor == 2
        assert any(
            "non-contiguous durable event log" in r.getMessage() and sid in r.getMessage()
            for r in caplog.records
        )
    finally:
        await adapter.disconnect()


# ─── W0a: native-leg MCP normalization + turn/output caps ────────────────────
#
# The app emits SDK-shaped MCP configs while fluid's client dispatches on
# ``transport``; without normalization every remote server silently failed to
# connect on native legs. NativeLoop also ran at its own defaults regardless of
# the profile's max_turns / output-token settings. See
# docs/PROVIDER_AGNOSTIC_PLAN.md wave W0a (ruling PA5).

import logging as _logging
import types as _types

from app.core.provider.harness_adapter import (
    _native_max_output_tokens,
    _normalize_mcp_config_for_fluid,
    _positive_int,
    _prune_duplicate_plugin_mcp_servers as ha_prune,
)

_HA_LOGGER = "app.core.provider.harness_adapter"


def test_normalize_mcp_stdio_config_passes_through():
    """stdio is already fluid's default transport — nothing to translate."""
    cfg = {"command": "node", "args": ["server.js"], "env": {"K": "v"}}
    assert _normalize_mcp_config_for_fluid("local", cfg) == cfg


def test_normalize_mcp_http_config_becomes_streamable_http():
    cfg = {"type": "http", "url": "https://example.test/mcp"}
    out = _normalize_mcp_config_for_fluid("remote", cfg)

    assert out == {"url": "https://example.test/mcp", "transport": "streamable_http"}
    # The caller's dict must survive untouched: the delegated leg serializes the
    # same objects into its --mcp-config file, where the SDK shape is required.
    assert cfg == {"type": "http", "url": "https://example.test/mcp"}


def test_normalize_mcp_http_headers_warn_but_still_connect(caplog):
    cfg = {
        "type": "http",
        "url": "https://example.test/mcp",
        "headers": {"Authorization": "Bearer x"},
    }
    with caplog.at_level(_logging.WARNING, logger=_HA_LOGGER):
        out = _normalize_mcp_config_for_fluid("authed", cfg)

    assert out is not None
    assert out["transport"] == "streamable_http"
    assert any(
        "authed" in r.getMessage() and "headers" in r.getMessage().lower()
        for r in caplog.records
    )


def test_normalize_mcp_sse_config_is_skipped_with_warning(caplog):
    cfg = {"type": "sse", "url": "https://example.test/sse"}
    with caplog.at_level(_logging.WARNING, logger=_HA_LOGGER):
        out = _normalize_mcp_config_for_fluid("streamer", cfg)

    assert out is None
    assert any(
        "streamer" in r.getMessage() and "SSE" in r.getMessage() for r in caplog.records
    )


def test_normalize_mcp_explicit_transport_is_respected():
    """A caller already speaking fluid's dialect is left alone."""
    cfg = {"transport": "streamable_http", "url": "https://example.test/mcp"}
    assert _normalize_mcp_config_for_fluid("native-shaped", cfg) is cfg


def test_normalize_mcp_unknown_shapes_pass_through(caplog):
    with caplog.at_level(_logging.WARNING, logger=_HA_LOGGER):
        assert _normalize_mcp_config_for_fluid("weird", {"type": "carrier-pigeon"}) == {
            "type": "carrier-pigeon"
        }
    assert any("weird" in r.getMessage() for r in caplog.records)
    # Non-dict entries are somebody else's problem, not ours to reshape.
    assert _normalize_mcp_config_for_fluid("odd", "nonsense") == "nonsense"


@pytest.mark.asyncio
async def test_build_registry_normalizes_before_connect_mcp(monkeypatch):
    """The seam fix, end to end: http reaches connect_mcp as streamable_http,
    sse never reaches it at all, stdio is untouched."""
    import app.core.provider.harness_adapter as ha

    calls: list = []

    async def fake_connect_mcp(self, name, config):
        calls.append((name, dict(config)))
        return []

    monkeypatch.setattr(ha.ToolRegistry, "connect_mcp", fake_connect_mcp)

    servers = {
        "local": {"command": "node", "args": ["s.js"]},
        "remote": {"type": "http", "url": "https://example.test/mcp"},
        "streamer": {"type": "sse", "url": "https://example.test/sse"},
    }
    adapter = ha.HarnessAdapter()
    await adapter._build_registry(
        SessionOptions(model="gpt-4o", mcp_servers=servers), native=True
    )

    connected = dict(calls)
    assert set(connected) == {"local", "remote"}
    assert connected["local"] == {"command": "node", "args": ["s.js"]}
    assert connected["remote"] == {
        "url": "https://example.test/mcp",
        "transport": "streamable_http",
    }
    # Original configs unmutated (the delegated leg reads the same dicts).
    assert servers["remote"] == {"type": "http", "url": "https://example.test/mcp"}


@pytest.mark.asyncio
async def test_delegated_leg_never_connects_external_mcp(monkeypatch):
    """Ruling PA5: the delegated leg passes external MCP servers to the claude
    CLI via --mcp-config, and the CLI spawns them inside its child process.
    connect_mcp'ing them in-process too would run every external server TWICE
    per session — the in-app connection is never read on that leg (the bundled
    socket only serves inproc/builtin specs)."""
    import app.core.provider.harness_adapter as ha

    calls: list = []

    async def fake_connect_mcp(self, name, config):
        calls.append(name)
        return []

    monkeypatch.setattr(ha.ToolRegistry, "connect_mcp", fake_connect_mcp)

    servers = {
        "local": {"command": "node", "args": ["s.js"]},
        "remote": {"type": "http", "url": "https://example.test/mcp"},
    }
    adapter = ha.HarnessAdapter()
    registry = await adapter._build_registry(
        SessionOptions(model="fable", mcp_servers=servers), native=False
    )
    assert calls == []
    # Still a registry, not None: DelegatedLoop._start_bundled_server bails on
    # a None registry BEFORE writing --mcp-config, which would silently drop
    # the external servers from the CLI child too.
    assert registry is not None

    # Same servers on a native leg DO connect in-process (the only path there).
    await adapter._build_registry(
        SessionOptions(model="gpt-4o", mcp_servers=servers), native=True
    )
    assert set(calls) == {"local", "remote"}


class _PluginMcpLoop(_CapturingLoop):
    """A NativeLoop that did what fluid does: merged an enabled plugin's MCP
    servers into its own lazy-connect set at construction time."""

    plugin_servers: dict = {
        "chrome-devtools": {"command": "npx", "args": ["chrome-devtools-mcp"]},
        "plugin-only": {"command": "npx", "args": ["other"]},
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._mcp_servers = dict(kwargs.get("mcp_servers") or {})
        for name, cfg in type(self).plugin_servers.items():
            if name in self._mcp_servers:
                continue
            self._mcp_servers[name] = cfg


def test_prune_duplicate_plugin_mcp_servers_is_name_keyed_and_app_wins(caplog):
    engine = _types.SimpleNamespace(
        _mcp_servers={"chrome-devtools": {"command": "npx"}, "solo": {"command": "x"}}
    )
    with caplog.at_level(_logging.WARNING, logger=_HA_LOGGER):
        dropped = ha_prune(engine, {"chrome-devtools": {"command": "app-side"}})

    assert dropped == ["chrome-devtools"]
    assert set(engine._mcp_servers) == {"solo"}
    assert any("chrome-devtools" in r.getMessage() for r in caplog.records)


def test_prune_duplicate_plugin_mcp_servers_no_op_cases():
    """No app servers, no plugin servers, or an engine without the attribute at
    all (delegated leg / a future fluid) — all inert, never raising."""
    engine = _types.SimpleNamespace(_mcp_servers={"solo": {"command": "x"}})
    assert ha_prune(engine, None) == []
    assert ha_prune(engine, {}) == []
    assert ha_prune(_types.SimpleNamespace(), {"solo": {}}) == []
    assert set(engine._mcp_servers) == {"solo"}


@pytest.mark.asyncio
async def test_connect_drops_plugin_mcp_servers_the_app_already_connected(
    monkeypatch, tmp_path
):
    """A plugin server whose name matches an app-configured one must not be
    connected a second time: the app connects its servers eagerly onto the
    shared registry, so fluid's own host-wins check (which only sees servers
    passed as mcp_servers=) cannot see the collision."""
    import app.core.provider.harness_adapter as ha

    connected: list = []

    async def fake_connect_mcp(self, name, config):
        connected.append(name)
        return []

    monkeypatch.setattr(ha.ToolRegistry, "connect_mcp", fake_connect_mcp)
    monkeypatch.setattr(ha, "NativeLoop", _PluginMcpLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
            mcp_servers={"chrome-devtools": {"command": "app-configured"}},
        )
    )

    # The app connected its own copy...
    assert connected == ["chrome-devtools"]
    # ...and the engine's lazy set keeps only the plugin server nobody owns.
    assert set(adapter._engine._mcp_servers) == {"plugin-only"}


@pytest.mark.asyncio
async def test_connect_native_leg_folds_the_hosts_claude_settings(monkeypatch, tmp_path):
    """Ruling CE4: a native leg adopts the user's settings.json stack.

    Asserted over a REAL NativeLoop (the pin lifted in this change is a
    constructor kwarg, so a capturing fake could only prove the kwarg's
    absence): ``env`` lands on ``ctx.extra["settings_env"]`` as the
    child-process overlay, and the settings permission rules are PREPENDED to
    the app's own — evaluation is last-match-wins, so the app keeps the final
    say over its session.
    """
    import json

    import app.core.provider.harness_adapter as ha
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

    # HOME redirected so the developer's real ~/.claude never leaks in.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".git").mkdir()
    (proj / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "env": {"AIH_SETTINGS_PROBE": "from-settings"},
                "permissions": {"allow": ["Bash(npm run *)"]},
            }
        ),
        encoding="utf-8",
    )

    # One host rule that contradicts the settings rule, so the ordering is
    # observable rather than incidental.
    host_rule = Rule(tool="Bash", decision="deny", input_globs={"command": "npm run *"})
    monkeypatch.setattr(
        ha.HarnessAdapter,
        "_build_permissions",
        lambda self, options: (PermissionEngine("on_write", rules=[host_rule]), False),
    )
    monkeypatch.setattr(
        ha.HarnessAdapter, "_build_store", staticmethod(lambda options: MemoryStore())
    )
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(proj),
            extra={"fluid_provider": "openai_responses"},
        )
    )

    engine = adapter._engine
    assert engine.ctx.extra["settings_env"] == {"AIH_SETTINGS_PROBE": "from-settings"}
    # Settings rule first, host rule last — the host's deny is what evaluates.
    assert [r.decision for r in engine.permissions.rules] == ["allow", "deny"]
    assert engine.permissions.rules[-1] is host_rule


async def _connect_capturing_native(monkeypatch, tmp_path, **option_kwargs):
    """connect() a native leg whose NativeLoop kwargs are captured."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
            **option_kwargs,
        )
    )
    return _CapturingLoop.captured


@pytest.mark.asyncio
async def test_max_turns_becomes_model_call_budget(monkeypatch, tmp_path):
    captured = await _connect_capturing_native(monkeypatch, tmp_path, max_turns=7)
    assert captured["max_model_calls_per_turn"] == 7


@pytest.mark.asyncio
async def test_no_max_turns_keeps_fluid_default(monkeypatch, tmp_path):
    """Unset / non-positive values must not be passed — fluid's default (50) wins."""
    captured = await _connect_capturing_native(monkeypatch, tmp_path)
    assert "max_model_calls_per_turn" not in captured

    captured = await _connect_capturing_native(monkeypatch, tmp_path, max_turns=0)
    assert "max_model_calls_per_turn" not in captured


@pytest.mark.asyncio
async def test_max_turns_projected_from_sdk_options(monkeypatch, tmp_path):
    """query_engine hands ClaudeAgentOptions, where max_turns actually lives."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    sdk_options = _types.SimpleNamespace(
        model="gpt-4o", cwd=str(tmp_path), max_turns=3, mcp_servers=None
    )
    await ha.HarnessAdapter().connect_with_sdk_options(sdk_options)

    assert _CapturingLoop.captured["max_model_calls_per_turn"] == 3


@pytest.mark.asyncio
async def test_env_output_token_cap_reaches_native_loop(monkeypatch, tmp_path):
    """Interim escape hatch from fluid's silent 8192 output cap."""
    captured = await _connect_capturing_native(
        monkeypatch, tmp_path, env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000"}
    )
    assert captured["max_tokens"] == 32000


@pytest.mark.asyncio
async def test_no_env_cap_keeps_fluid_default(monkeypatch, tmp_path):
    captured = await _connect_capturing_native(monkeypatch, tmp_path)
    assert "max_tokens" not in captured

    captured = await _connect_capturing_native(
        monkeypatch, tmp_path, env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "0"}
    )
    assert "max_tokens" not in captured


def test_env_output_cap_parsing(caplog):
    assert _native_max_output_tokens(SessionOptions(model="m")) is None
    assert (
        _native_max_output_tokens(
            SessionOptions(model="m", env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": " 16000 "})
        )
        == 16000
    )
    with caplog.at_level(_logging.WARNING, logger=_HA_LOGGER):
        assert (
            _native_max_output_tokens(
                SessionOptions(model="m", env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "lots"})
            )
            is None
        )
    assert any("CLAUDE_CODE_MAX_OUTPUT_TOKENS" in r.getMessage() for r in caplog.records)


def test_positive_int_rejects_bools_and_junk():
    assert _positive_int(5) == 5
    assert _positive_int("5") == 5
    assert _positive_int(True) is None
    assert _positive_int(-1) is None
    assert _positive_int(None) is None
    assert _positive_int("abc") is None


# ─── Task dispatcher injection on native legs (ruling A13) ───────────────────
#
# Regression cover for the live bug: a codex-native session called the bare
# ``Task`` builtin and got "Task dispatch is not available in this environment
# — no dispatcher was injected by the host application", because connect() only
# injected the dispatcher when a fluid session id already existed.


async def _connect_native_leg(monkeypatch, tmp_path, **extra) -> HarnessAdapter:
    """A native (openai_responses) leg over the kwarg-capturing fake loop."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    adapter = ha.HarnessAdapter()
    await adapter.connect(
        SessionOptions(
            model="gpt-4o",
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses", **extra},
        )
    )
    return adapter


def _injected_dispatcher():
    return _CapturingLoop.captured["ctx"].extra.get("task_dispatch")


@pytest.mark.asyncio
async def test_native_leg_injects_task_dispatch_without_a_session_id(
    monkeypatch, tmp_path
):
    """A brand-new native session has neither session_id nor resume_session_id
    at connect time — the dispatcher must be injected anyway."""
    await _connect_native_leg(monkeypatch, tmp_path)
    assert _injected_dispatcher() is not None


@pytest.mark.asyncio
async def test_task_dispatch_uses_the_stamped_app_session_id(monkeypatch, tmp_path):
    """query_options stamps the app session id; the dispatcher must dispatch
    against THAT, not the fluid ``ses_...`` handle riding on the ctx."""
    import app.core.task_dispatch as td

    seen: dict = {}

    async def _fake_dispatch(args, *, caller_session_id, caller_profile_id=None, launch_tool_use_id=None, agent_registry=None):
        seen["caller"] = caller_session_id
        return {"ok": True}

    monkeypatch.setattr(td, "dispatch_task_tool", _fake_dispatch)
    await _connect_native_leg(monkeypatch, tmp_path, app_session_id="app-sess-1")

    ctx = _CapturingLoop.captured["ctx"]
    assert ctx.session_id != "app-sess-1"  # the fluid id is NOT the app id
    await _injected_dispatcher()({"agent": "helper", "prompt": "go"}, ctx)
    assert seen["caller"] == "app-sess-1"


@pytest.mark.asyncio
async def test_task_dispatch_falls_back_to_the_sdk_id_lookup(monkeypatch, tmp_path):
    """Without the stamp, the app session is recovered by reverse-mapping the
    fluid session id through ``sessions.sdk_session_id``."""
    import app.core.task_dispatch as td
    from app.db import database

    seen: dict = {}

    async def _fake_dispatch(args, *, caller_session_id, caller_profile_id=None, launch_tool_use_id=None, agent_registry=None):
        seen["caller"] = caller_session_id
        return {"ok": True}

    monkeypatch.setattr(td, "dispatch_task_tool", _fake_dispatch)
    monkeypatch.setattr(
        database, "get_session_by_sdk_id",
        lambda sdk_id: {"id": "app-sess-2"} if sdk_id == "ses_x" else None,
    )
    adapter = await _connect_native_leg(monkeypatch, tmp_path)
    assert adapter._sdk_session_id == "ses_x"

    await _injected_dispatcher()({"agent": "helper", "prompt": "go"}, object())
    assert seen["caller"] == "app-sess-2"


@pytest.mark.asyncio
async def test_task_dispatch_reports_a_clear_error_when_unresolvable(
    monkeypatch, tmp_path
):
    """No stamp and no mapping → a named error, never a parentless worker."""
    import json

    import app.core.task_dispatch as td
    from app.db import database

    async def _explode(*a, **kw):  # pragma: no cover — must never run
        raise AssertionError("dispatch must not be attempted without a caller")

    monkeypatch.setattr(td, "dispatch_task_tool", _explode)
    monkeypatch.setattr(database, "get_session_by_sdk_id", lambda sdk_id: None)
    await _connect_native_leg(monkeypatch, tmp_path)

    raw = await _injected_dispatcher()({"agent": "helper", "prompt": "go"}, object())
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "could not resolve the calling session" in payload["error"]


@pytest.mark.asyncio
async def test_pooled_engine_dispatcher_picks_up_a_late_session_id(
    monkeypatch, tmp_path
):
    """The ctx built in connect() outlives the connect. The SAME dispatcher
    must start working once the session id becomes resolvable — this is the
    pooled-client reuse path ("Reusing pooled client for session ...")."""
    import json

    import app.core.task_dispatch as td
    from app.db import database

    seen: dict = {}
    mapped: dict = {}

    async def _fake_dispatch(args, *, caller_session_id, caller_profile_id=None, launch_tool_use_id=None, agent_registry=None):
        seen["caller"] = caller_session_id
        return {"ok": True}

    monkeypatch.setattr(td, "dispatch_task_tool", _fake_dispatch)
    monkeypatch.setattr(
        database, "get_session_by_sdk_id", lambda sdk_id: mapped.get(sdk_id)
    )
    await _connect_native_leg(monkeypatch, tmp_path)

    dispatcher = _injected_dispatcher()
    first = json.loads(await dispatcher({"agent": "helper", "prompt": "go"}, object()))
    assert first["ok"] is False and "caller" not in seen

    mapped["ses_x"] = {"id": "app-sess-3"}
    await dispatcher({"agent": "helper", "prompt": "go"}, object())
    assert seen["caller"] == "app-sess-3"


@pytest.mark.asyncio
async def test_connect_with_sdk_options_projects_the_app_session_id(monkeypatch):
    """``aih_session_id`` (stamped by query_options) lands on
    ``SessionOptions.extra['app_session_id']``."""
    import types

    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(
        HarnessAdapter, "_build_store", staticmethod(lambda _o: MemoryStore())
    )

    adapter = ha.HarnessAdapter()
    await adapter.connect_with_sdk_options(
        types.SimpleNamespace(
            model="claude-sonnet-4-5",
            aih_provider_id=None,
            aih_session_id="app-sess-4",
        )
    )
    assert adapter._options.extra["app_session_id"] == "app-sess-4"


@pytest.mark.asyncio
async def test_effort_reaches_the_delegated_loop(monkeypatch):
    """Profile/override effort lands on DelegatedLoop(effort=...) → --effort."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(
        HarnessAdapter, "_build_store", staticmethod(lambda _o: MemoryStore())
    )

    await ha.HarnessAdapter().connect(
        SessionOptions(model="claude-sonnet-4-5", effort="xhigh")
    )
    assert _CapturingLoop.captured["effort"] == "xhigh"


@pytest.mark.asyncio
async def test_effort_reaches_the_native_loop(monkeypatch, tmp_path):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    monkeypatch.setattr(
        HarnessAdapter, "_build_store", staticmethod(lambda _o: MemoryStore())
    )

    await ha.HarnessAdapter().connect(
        SessionOptions(model="gpt-4o", cwd=str(tmp_path), effort="max")
    )
    assert _CapturingLoop.captured["effort"] == "max"


@pytest.mark.asyncio
async def test_junk_effort_is_dropped(monkeypatch):
    """An unrecognized effort value must not reach the engine (it would 400
    the provider request or fail the CLI spawn)."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(
        HarnessAdapter, "_build_store", staticmethod(lambda _o: MemoryStore())
    )

    await ha.HarnessAdapter().connect(
        SessionOptions(model="claude-sonnet-4-5", effort="turbo")
    )
    assert _CapturingLoop.captured["effort"] is None


def test_effort_change_requires_reconnect():
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="claude-sonnet-4-5", effort="high")
    assert adapter.requires_reconnect(
        SessionOptions(model="claude-sonnet-4-5", effort="low")
    )
    assert not adapter.requires_reconnect(
        SessionOptions(model="claude-sonnet-4-5", effort="high")
    )
    # None means "no override" — keeps the old effort, no reconnect
    assert not adapter.requires_reconnect(
        SessionOptions(model="claude-sonnet-4-5")
    )


# ─── _amend_native_task_spec ───────────────────────────────────────────────────


def test_amend_native_task_spec_states_the_policy_without_a_model_menu(monkeypatch):
    """A native leg bound to a provider row gets a Task ``model`` description
    that states the OVERRIDE POLICY — and deliberately does NOT enumerate the
    row's curated ids.

    2026-08-17 ruling: inlining the curated roster read as a menu and taught the
    model to shop for models (the live incident moved a subscription-bound agent
    onto a metered API row). The wording now says: omit it and the agent runs as
    configured; an override is honored only if YOUR provider has the model, and
    is otherwise ignored with a note."""
    import app.core.provider.harness_adapter as ha
    import app.db.database as db

    monkeypatch.setattr(
        db,
        "get_model_provider",
        lambda pid, include_token=False: {
            "id": pid,
            "models_json": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.6-mini"}],
        },
    )

    registry = ha.ToolRegistry()
    from fluid.tools.builtins import register_builtins

    register_builtins(registry)
    options = SessionOptions(model="gpt-5.6-sol", extra={"provider_id": "prov1"})

    ha._amend_native_task_spec(registry, options)

    description = registry.get("Task").input_schema["properties"]["model"]["description"]
    # The curated roster is GONE — no menu, and no provider row id either.
    assert "gpt-5.6-mini" not in description
    assert "prov1" not in description
    assert "normally OMIT it" in description
    assert "runs the agent exactly as configured" in description
    assert "available on YOUR OWN provider" in description


def test_delegated_task_tool_shares_the_same_model_policy_wording():
    """Leg parity: the delegated leg's ``mcp__builtins__Task`` carries the exact
    same ``model`` description, so neither leg advertises a model menu."""
    from unittest.mock import patch as _patch

    import claude_agent_sdk as sdk

    import app.mcp.builtin_tools as bt
    from app.core.task_dispatch import TASK_MODEL_OVERRIDE_DESCRIPTION

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with _patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        bt.create_builtin_tools_server(session_id="s-policy", profile_id="p")

    task = {t.name: t for t in captured["tools"]}["Task"]
    assert (
        task.input_schema["properties"]["model"]["description"]
        == TASK_MODEL_OVERRIDE_DESCRIPTION
    )
    assert "curates:" not in TASK_MODEL_OVERRIDE_DESCRIPTION


def test_amend_native_task_spec_leaves_unbound_sessions_untouched(monkeypatch):
    """No ``provider_id`` in ``extra`` means the session is delegated/Claude-
    default — the vendor Task spec's own description must survive unmodified."""
    import app.core.provider.harness_adapter as ha

    from fluid.tools.builtins import register_builtins
    from fluid.tools.builtins.task import SPEC as _TASK_SPEC

    registry = ha.ToolRegistry()
    register_builtins(registry)
    options = SessionOptions(model="gpt-5.6-sol", extra={})

    ha._amend_native_task_spec(registry, options)

    description = registry.get("Task").input_schema["properties"]["model"]["description"]
    assert description == _TASK_SPEC.input_schema["properties"]["model"]["description"]


def test_amend_native_task_spec_swallows_a_registration_failure(monkeypatch):
    """A failure anywhere in the amend path must never propagate out of
    connect() — the vendor Task spec just stays registered as-is."""
    import app.core.provider.harness_adapter as ha

    from fluid.tools.builtins import register_builtins
    from fluid.tools.builtins.task import SPEC as _TASK_SPEC

    registry = ha.ToolRegistry()
    register_builtins(registry)

    def _boom(*a, **kw):
        raise RuntimeError("registry down")

    monkeypatch.setattr(registry, "register_builtin", _boom)
    options = SessionOptions(model="gpt-5.6-sol", extra={"provider_id": "prov1"})

    ha._amend_native_task_spec(registry, options)  # must not raise

    description = registry.get("Task").input_schema["properties"]["model"]["description"]
    assert description == _TASK_SPEC.input_schema["properties"]["model"]["description"]


# ─── 7. CE12 — enforced tool restriction projected onto the engines ──────────


class _RecordingEngine:
    """Stands in for NativeLoop/DelegatedLoop and captures its kwargs.

    Deliberately has no ``_ensure_session``: connect() primes the session only
    when the engine offers one, so omitting it keeps these tests off the store.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture()
def recording_adapter(monkeypatch):
    """A HarnessAdapter whose engine construction is captured, with the store,
    registry, provider client and prompt compiler stubbed out."""
    import app.core.provider.harness_adapter as ha

    made: dict = {}

    class _Native(_RecordingEngine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            made["native"] = kwargs

    class _Delegated(_RecordingEngine):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            made["delegated"] = kwargs

    monkeypatch.setattr(ha, "NativeLoop", _Native)
    monkeypatch.setattr(ha, "DelegatedLoop", _Delegated)
    monkeypatch.setattr(HarnessAdapter, "_build_store", lambda self, o: MemoryStore())

    async def _registry(self, options, native):
        return ha.ToolRegistry()

    async def _client(kind, options):
        return object(), None

    async def _cli_sid(self, fluid_session_id, options):
        return None

    monkeypatch.setattr(HarnessAdapter, "_build_registry", _registry)
    monkeypatch.setattr(HarnessAdapter, "_compile_prompt_sections", lambda *a, **k: None)
    monkeypatch.setattr(HarnessAdapter, "_resolve_cli_session_id", _cli_sid)
    monkeypatch.setattr(ha, "_build_provider_client", _client)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["true"])
    return HarnessAdapter(), made


def _native_opts(**kw):
    """Options that route to the native anthropic leg."""
    kw.setdefault("model", "claude-sonnet-4-5")
    extra = kw.pop("extra", None) or {}
    extra.setdefault("fluid_engine", "native")
    extra.setdefault("fluid_provider", "anthropic")
    return SessionOptions(extra=extra, **kw)


@pytest.mark.asyncio
async def test_native_loop_gets_the_tool_allowlist(recording_adapter):
    """A worker spawned from a file agent with ``tools: [Read, Grep]`` reaches
    NativeLoop as ``allowed_tools=["Read", "Grep"]`` (ruling CE12)."""
    adapter, made = recording_adapter
    await adapter.connect(_native_opts(tool_allowlist=["Read", "Grep"]))
    assert made["native"]["allowed_tools"] == ["Read", "Grep"]


@pytest.mark.asyncio
async def test_native_loop_unrestricted_without_an_allowlist(recording_adapter):
    """No allowlist (and a blank one) = None: fluid reads that as "every tool".
    Passing [] instead would blank the whole session's tool surface."""
    adapter, made = recording_adapter
    await adapter.connect(_native_opts())
    assert made["native"]["allowed_tools"] is None

    await HarnessAdapter().connect(_native_opts(tool_allowlist=["", "  "]))
    assert made["native"]["allowed_tools"] is None


@pytest.mark.asyncio
async def test_native_loop_gets_the_profile_blocklist(recording_adapter):
    """``disallowed_tools`` is no longer degraded on native legs — it reaches
    NativeLoop, which filters schema, catalog and dispatch."""
    adapter, made = recording_adapter
    await adapter.connect(_native_opts(disallowed_tools=["Bash", "Write"]))
    assert made["native"]["disallowed_tools"] == ["Bash", "Write"]

    await HarnessAdapter().connect(_native_opts(disallowed_tools=[]))
    assert made["native"]["disallowed_tools"] is None


@pytest.mark.asyncio
async def test_delegated_leg_forwards_the_allowlist_and_keeps_its_bans(
    recording_adapter,
):
    """The CLI leg passes the same list as ``--allowed-tools`` while the A6 ban
    set (Task/Agent/wake family) stays in ``disallowed_tools``."""
    adapter, made = recording_adapter
    await adapter.connect(
        SessionOptions(
            model="claude-sonnet-4-5",
            tool_allowlist=["Read"],
            disallowed_tools=["Write"],
        )
    )
    assert made["delegated"]["allowed_tools"] == ["Read"]
    assert "Task" in made["delegated"]["disallowed_tools"]
    assert "Write" in made["delegated"]["disallowed_tools"]


@pytest.mark.asyncio
async def test_connect_with_sdk_options_threads_aih_tool_allowlist(recording_adapter):
    """``aih_tool_allowlist`` is the surface channel through the projection
    shim. ``allowed_tools`` — the SDK's PERMISSION allowlist, full of MCP names
    and empty of builtins under a tools preset — must NOT become one: projecting
    it would delete Read/Bash/Edit from every native session."""
    from types import SimpleNamespace

    adapter, made = recording_adapter
    sdk_options = SimpleNamespace(
        model="claude-sonnet-4-5",
        system_prompt=None,
        cwd=None,
        resume=None,
        permission_mode=None,
        mcp_servers=None,
        env=None,
        include_partial_messages=False,
        add_dirs=None,
        disallowed_tools=None,
        max_turns=None,
        effort=None,
        allowed_tools=["mcp__builtins__Task", "mcp__ai-tools"],
        aih_tool_allowlist=["Read"],
        aih_provider_id=None,
        aih_session_id="app-sess",
    )
    await adapter.connect_with_sdk_options(sdk_options)
    assert made["delegated"]["allowed_tools"] == ["Read"]
    assert adapter._options.tool_allowlist == ["Read"]
    assert adapter._options.allowed_tools is None


def test_requires_reconnect_on_a_changed_tool_surface():
    """The restriction is frozen at construction (CE12), so a change can only be
    honored by a rebuild — never a model hot-swap."""
    adapter = HarnessAdapter()
    adapter._options = _native_opts(tool_allowlist=["Read"])
    assert adapter.requires_reconnect(_native_opts(tool_allowlist=["Read"])) is False
    assert adapter.requires_reconnect(_native_opts(tool_allowlist=["Read", "Bash"])) is True
    assert adapter.requires_reconnect(_native_opts()) is True

    adapter._options = _native_opts(disallowed_tools=["Bash"])
    assert adapter.requires_reconnect(_native_opts(disallowed_tools=["Bash"])) is False
    assert adapter.requires_reconnect(_native_opts(disallowed_tools=[])) is True


def test_vendored_native_loop_accepts_the_restriction_kwargs():
    """Pins the vendored seam itself (fluid 020a1d6, ruling CE12)."""
    import inspect

    from fluid.engine.delegated import DelegatedLoop as _RealDelegated
    from fluid.engine.native import NativeLoop as _RealNative

    native_params = inspect.signature(_RealNative.__init__).parameters
    assert "allowed_tools" in native_params and "disallowed_tools" in native_params
    assert "allowed_tools" in inspect.signature(_RealDelegated.__init__).parameters

    loop = _RealNative(
        provider=FakeProvider([]), store=MemoryStore(), model="fake",
        allowed_tools=["Read", "mcp__x__*"], disallowed_tools=["mcp__x__danger"],
        claude_settings=False, claude_plugins=False, claude_agents=False,
    )
    assert loop._tool_permitted("Read") is True
    assert loop._tool_permitted("Bash") is False            # outside the allowlist
    assert loop._tool_permitted("mcp__x__safe") is True     # fnmatch pattern
    assert loop._tool_permitted("mcp__x__danger") is False  # deny beats allow


# ─── Generation-path binding projection ──────────────────────────────────────


class _GenOptions:
    """The shape ``generation_options.build_generation_options`` returns.

    Only the attributes ``connect_with_sdk_options`` reads are modelled; the
    real object is a ``ClaudeAgentOptions`` carrying the same ``aih_*`` stamps.
    """

    def __init__(self, model, provider_id=None, provider_type=None,
                 auth_mode=None, dialect=None):
        self.model = model
        self.env = {}
        self.max_turns = 1
        self.aih_provider_id = provider_id
        self.aih_provider_type = provider_type
        self.aih_provider_auth_mode = auth_mode
        self.aih_provider_dialect = dialect


async def _kind_for_generation_options(gen_options, monkeypatch):
    """Decide the leg the way a real generation call does.

    Runs the REAL projection (``HarnessAdapter.connect_with_sdk_options``) with
    only the engine-building ``connect`` stubbed out, then asks
    ``_decide_provider_kind`` — so this covers the stamp -> extra -> decision
    chain end to end rather than re-deriving the projection in the test.
    """
    captured = {}

    async def _fake_connect(self, opts):
        captured["opts"] = opts

    monkeypatch.setattr(HarnessAdapter, "connect", _fake_connect, raising=True)
    await HarnessAdapter().connect_with_sdk_options(gen_options)
    return _decide_provider_kind(captured["opts"])


@pytest.mark.asyncio
async def test_generation_options_route_like_chat(monkeypatch):
    """AI generation must pick the SAME leg chat would for the same binding.

    Regression for the 190s hang: generation used to call the SDK's ``query()``
    directly, so EVERY generation call spawned ``claude.exe`` — including when
    the Default AI profile pointed at a GPT model.
    """
    async def kind(opts):
        return await _kind_for_generation_options(opts, monkeypatch)

    # Non-Claude provider row → fluid native leg, no claude.exe anywhere.
    assert await kind(
        _GenOptions("gpt-5.6-terra", provider_id="op", provider_type="openai")
    ) == "openai_responses"
    assert await kind(
        _GenOptions("meta-llama/llama-3.1-70b", provider_id="op",
                    provider_type="openrouter")
    ) == "openrouter"
    # An Anthropic API-KEY row also runs natively.
    assert await kind(
        _GenOptions("claude-sonnet-4-5", provider_id="op",
                    provider_type="anthropic")
    ) == "anthropic"
    # ...but a Claude SUBSCRIPTION row stays on the delegated claude CLI —
    # Anthropic bans third-party use of subscription tokens.
    for mode in ("oauth", "subscription", "claude_subscription", "max", "pro"):
        assert await kind(
            _GenOptions("claude-sonnet-4-5", provider_id="op",
                        provider_type="anthropic", auth_mode=mode)
        ) == "delegated", mode
    # A provider-less (native Claude) profile keeps the delegated leg via the
    # model sniff, exactly as before the fix.
    assert await kind(_GenOptions("opus")) == "delegated"


# ─── Agents in context (2026-08-17 ruling, superseding A10) ──────────────────
#
# ``enabled_agents``/``agent_invoke_mode`` DECLARE agents into the caller's
# context; they never gate dispatch. The declaration must be index-shaped —
# name + description and nothing else — and must sit in the SAME
# "Available agent types" block as the file and plugin agents fluid discovers.


def _agent_md(name: str, description: str, body: str = "BODY") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def _write_file(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def agent_workspace(tmp_path, monkeypatch):
    """A project cwd with a file agent, plus an installed plugin agent.

    HOME/USERPROFILE are redirected so the developer's real ~/.claude never
    leaks into an assertion (fluid's own test convention).
    """
    import json

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _write_file(
        project / ".claude" / "agents" / "researcher.md",
        _agent_md("researcher", "Reads the codebase"),
    )

    plugin_root = home / ".claude" / "plugins" / "cache" / "testmp" / "myplug" / "1.0.0"
    _write_file(plugin_root / ".claude-plugin" / "plugin.json", json.dumps({"name": "myplug"}))
    _write_file(
        plugin_root / "agents" / "reviewer.md",
        _agent_md("reviewer", "Reviews diffs", body="PLUGIN AGENT PROMPT"),
    )
    _write_file(
        home / ".claude" / "settings.json",
        json.dumps({"enabledPlugins": {"myplug@testmp": True}}),
    )
    return project


def _agents_options(cwd, declared=None):
    extra = {"fluid_provider": "openai_responses"}
    if declared is not None:
        extra["declared_agents"] = declared
    return SessionOptions(
        model="gpt-4o",
        system_prompt="BASE PROMPT",
        cwd=str(cwd),
        extra=extra,
    )


@pytest.mark.asyncio
async def test_agents_index_carries_file_plugin_and_db_agents(
    monkeypatch, agent_workspace
):
    """One index, three sources, name+description only."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        _agents_options(
            agent_workspace,
            declared=[{"name": "Helper", "description": "Helps out"}],
        )
    )
    system = _CapturingLoop.captured["system"]
    assert system.count("# Available agent types") == 1
    assert "- researcher: Reads the codebase" in system      # (a) file agent
    assert "- myplug:reviewer: Reviews diffs" in system      # (b) plugin agent
    assert "- Helper: Helps out" in system                   # (c) DB agent
    # Index-only: no definition body may reach the caller's context.
    assert "PLUGIN AGENT PROMPT" not in system
    assert "BODY" not in system


@pytest.mark.asyncio
async def test_declared_db_agents_reach_native_loop_as_descriptors(
    monkeypatch, agent_workspace
):
    """A profile with a custom enabled_agents selection hands NativeLoop the
    matching ``agents=`` descriptors (name + description, source 'app')."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        _agents_options(
            agent_workspace,
            declared=[{"name": "Helper", "description": "Helps out"}],
        )
    )
    agents = _CapturingLoop.captured["agents"]
    app_agents = [a for a in agents if a["source"] == "app"]
    assert app_agents == [
        {"agentType": "Helper", "source": "app", "description": "Helps out"}
    ]
    # The discovered definitions ride along too — passing ``agents=`` at all
    # suppresses fluid's own auto-fill, so the usage row must not lose them.
    assert {a["agentType"] for a in agents} == {"Helper", "researcher", "myplug:reviewer"}


@pytest.mark.asyncio
async def test_no_declared_agents_leaves_the_discovered_index_alone(
    monkeypatch, agent_workspace
):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(_agents_options(agent_workspace))
    system = _CapturingLoop.captured["system"]
    assert "- researcher: Reads the codebase" in system
    assert "- myplug:reviewer: Reviews diffs" in system
    assert sorted(a["agentType"] for a in _CapturingLoop.captured["agents"]) == [
        "myplug:reviewer", "researcher",
    ]


@pytest.mark.asyncio
async def test_declared_db_agent_wins_a_name_collision_with_a_file_agent(
    monkeypatch, agent_workspace
):
    """Host-first resolution (CE6): the index must describe the definition the
    dispatcher would actually run."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        _agents_options(
            agent_workspace,
            declared=[{"name": "researcher", "description": "the app's own"}],
        )
    )
    system = _CapturingLoop.captured["system"]
    assert "- researcher: the app's own" in system
    assert "Reads the codebase" not in system


@pytest.mark.asyncio
async def test_plugin_agents_reach_the_dispatch_registry(monkeypatch, agent_workspace):
    """A plugin agent is not just advertised, it is DISPATCHABLE.

    Over a REAL NativeLoop (no fake), the namespaced definition lands in
    ctx.extra["agent_registry"], which is what task_dispatch resolves against.
    """
    import app.core.provider.harness_adapter as ha
    import app.core.task_dispatch as td

    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(_agents_options(agent_workspace))
    registry = adapter.engine.ctx.extra["agent_registry"]
    assert set(registry) == {"researcher", "myplug:reviewer"}
    resolved = td.resolve_registry_agent("myplug:reviewer", registry)
    assert resolved is not None and resolved.body.strip() == "PLUGIN AGENT PROMPT"


# ─── the same index on the DELEGATED (Claude-subscription) leg ───────────────
#
# The Claude-sub caller direction: in append mode (the default) the CLI builds
# its own prompt, so the app injects nothing and the one leg that never learned
# which agents exist was the subscription one — it could not name a target for
# the Task tool at all. The block now rides whichever prompt part the CLI reads.


def _delegated_agents_options(cwd, declared=None):
    from fluid.tools.registry import ToolRegistry

    extra = {"fluid_registry": ToolRegistry()}
    if declared is not None:
        extra["declared_agents"] = declared
    return SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "append": "APPEND TEXT"},
        cwd=str(cwd),
        extra=extra,
    )


@pytest.mark.asyncio
async def test_delegated_append_mode_advertises_agents(monkeypatch, agent_workspace):
    """Default (append) mode: the merged index rides --append-system-prompt,
    so a Claude-sub caller sees file, plugin AND DB agents."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)

    await ha.HarnessAdapter().connect(
        _delegated_agents_options(
            agent_workspace, declared=[{"name": "Helper", "description": "Helps out"}]
        )
    )
    append = _CapturingLoop.captured["append_system_prompt"]
    # The CLI reads --append-system-prompt OR --system-prompt, never both.
    assert _CapturingLoop.captured["system"] is None
    assert append.startswith("APPEND TEXT")
    assert append.count("# Available agent types") == 1
    assert "- researcher: Reads the codebase" in append      # file agent
    assert "- myplug:reviewer: Reviews diffs" in append       # plugin agent
    assert "- Helper: Helps out" in append                    # DB agent
    # Index-only, exactly like the native leg: no definition bodies.
    assert "PLUGIN AGENT PROMPT" not in append
    assert "BODY" not in append


@pytest.mark.asyncio
async def test_delegated_plain_prompt_carries_the_index(monkeypatch, agent_workspace):
    """A full-replacement (plain string) profile prompt is the part the CLI
    reads, so the index has to ride THAT one instead."""
    import app.core.provider.harness_adapter as ha
    from fluid.tools.registry import ToolRegistry

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="fable",
            system_prompt="BASE PROMPT",
            cwd=str(agent_workspace),
            extra={
                "fluid_registry": ToolRegistry(),
                "declared_agents": [{"name": "Helper", "description": "Helps out"}],
            },
        )
    )
    system = _CapturingLoop.captured["system"]
    assert _CapturingLoop.captured["append_system_prompt"] is None
    assert system.startswith("BASE PROMPT")
    assert system.count("# Available agent types") == 1
    assert "- Helper: Helps out" in system


@pytest.mark.asyncio
async def test_delegated_full_replace_does_not_double_the_index(
    monkeypatch, agent_workspace
):
    """PROMPT_FULL_REPLACE already slots the index into the compiled prompt —
    the append-mode injection must not add a second copy."""
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(settings, "prompt_full_replace", True)

    await ha.HarnessAdapter().connect(
        _delegated_agents_options(
            agent_workspace, declared=[{"name": "Helper", "description": "Helps out"}]
        )
    )
    system = _CapturingLoop.captured["system"]
    assert _CapturingLoop.captured["append_system_prompt"] is None
    assert system.count("# Available agent types") == 1
    assert "- Helper: Helps out" in system


def test_with_agents_index_is_idempotent():
    """A part that already advertises agents is returned untouched, so no
    reconnect (or profile prompt that carries its own index) can double it."""
    import app.core.provider.harness_adapter as ha

    index = f"{ha._agents_header()}\n- Helper: Helps out"
    # Append text wins (it is the flag the CLI actually spends).
    assert ha._with_agents_index("BASE", "APP", index) == ("BASE", f"APP\n\n{index}")
    # Already present -> unchanged, both ways round.
    assert ha._with_agents_index(None, f"APP\n\n{index}", index) == (
        None, f"APP\n\n{index}",
    )
    assert ha._with_agents_index(f"BASE\n\n{index}", None, index) == (
        f"BASE\n\n{index}", None,
    )
    # No prompt at all -> a bare append, which keeps the CLI's own preset.
    assert ha._with_agents_index(None, None, index) == (None, index)
    # Nothing to advertise -> untouched.
    assert ha._with_agents_index("BASE", None, None) == ("BASE", None)
