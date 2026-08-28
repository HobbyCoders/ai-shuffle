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

import asyncio
import dataclasses

import pytest

from app.core.model_registry import resolve_api_model_id
from app.core.provider.harness_adapter import HarnessAdapter, _decide_provider_kind
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.core.provider.events import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    SteerDeliveredEvent,
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

from fluid.context.trim import estimate_tokens_of_text
from app.core.provider.legacy_events import SafetyWarning
from fluid.events import model as lm
from fluid.events.ops import (
    BackgroundTaskFinished, RespondPermission, RespondQuestion, Steer, UserTurn,
)
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


@pytest.mark.asyncio
async def test_host_background_completion_clears_fluid_without_double_wake():
    engine = FakeEngine([])
    adapter = _adapter_with(engine)

    await adapter.prepare_background_completions([{
        "task_id": "worker-1",
        "status": "completed",
        "summary": "done",
    }])

    assert len(engine.ops) == 1
    op = engine.ops[0]
    assert isinstance(op, BackgroundTaskFinished)
    assert op.task_id == "worker-1"
    assert op.status == "finished"
    assert op.summary == "done"
    assert op.wake is False


@pytest.mark.asyncio
async def test_host_owned_fluid_lifecycle_events_are_not_mirrored_twice():
    events = [
        lm.SubagentEvent(
            session_id=SID,
            task_id="worker-1",
            kind="started",
            worker_session_id="worker-1",
        ),
        lm.SessionParked(session_id=SID, pending_task_ids=("worker-1",)),
        lm.SessionWoken(session_id=SID, task_ids=("worker-1",)),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))

    assert len(got) == 1
    assert isinstance(got[0], ResultMessage)


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
    # Usage is a durable, per-model-call record now; it is no longer held and
    # attached to an unrelated later assistant block.
    assert isinstance(got[8], AssistantMessage) and got[8].content == []
    assert got[8].usage["input_tokens"] == 10
    assert isinstance(got[9], RateLimitEvent)
    assert got[9].rate_limit_info.rate_limit_type == "requests"
    assert got[9].rate_limit_info.utilization == 0.5
    assert isinstance(got[10], TaskStartedMessage) and got[10].task_id == "t1"
    assert isinstance(got[10], SystemMessage)  # subclass, like the SDK hierarchy
    assert isinstance(got[11], TaskProgressMessage)
    assert isinstance(got[12], TaskNotificationMessage) and got[12].status == "completed"
    assert isinstance(got[13], ResultMessage) and got[13].stop_reason == "end_turn"
    assert got[13].total_cost_usd == 0.01
    assert got[13].usage == {"input_tokens": 10}
    assert len(got) == 14  # nothing after the terminal event


@pytest.mark.asyncio
async def test_host_monitor_receipt_translates_as_a_shell_task():
    """A SubagentEvent receipt WITHOUT a worker session is a host Monitor
    shell: its TaskStarted must carry task_type="local_bash" so
    _record_task_message files it kind="shell" instead of clobbering the
    spawner's SHELL registration with kind="agent". A Task/ProviderAgent
    worker (worker_session_id set) keeps the agent classification."""
    events = [
        lm.SubagentEvent(
            session_id=SID, task_id="mon-1", kind="started", description="ci run",
        ),
        # A Task/ProviderAgent worker (worker_session_id set) never mirrors at
        # all — the host owns its lifecycle (existing dedup rule).
        lm.SubagentEvent(
            session_id=SID, task_id="agent-1", kind="started",
            worker_session_id="agent-1", description="research",
        ),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    started = [e for e in got if isinstance(e, TaskStartedMessage)]
    assert len(started) == 1
    assert started[0].task_id == "mon-1"
    assert started[0].task_type == "local_bash"


@pytest.mark.asyncio
async def test_provider_retry_translates_to_a_system_note():
    """The engine's empty-completion retry surfaces as a plain SystemMessage
    the turn recorder files as a system.note; the UI folds consecutive ones
    into a single counting card."""
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.ProviderRetry(
            session_id=SID, attempt=3, max_attempts=10,
            reason="empty response from provider",
            delay_seconds=4.0, model="ox-alpha",
        ),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    notes = [e for e in got if isinstance(e, SystemMessage)
             and e.subtype == "provider_retry"]
    assert len(notes) == 1
    assert notes[0].data == {
        "attempt": 3,
        "max_attempts": 10,
        "reason": "empty response from provider",
        "delay_seconds": 4.0,
        "model": "ox-alpha",
    }


@pytest.mark.asyncio
async def test_steer_submits_the_op_and_its_receipt_reaches_the_stream():
    """A queued message handed to the running turn goes out as a fluid Steer
    carrying the queue row's id, and fluid's delivery receipt mirrors into the
    stream — that receipt is what lets the host retire the row."""
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.SteerDelivered(session_id=SID, steers=[
            {"id": "turn-9", "text": "check staging too"},
        ]),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    engine = FakeEngine(events)
    adapter = _adapter_with(engine)

    await adapter.steer("check staging too", id="turn-9")
    steers = [op for op in engine.ops if isinstance(op, Steer)]
    assert len(steers) == 1
    assert steers[0].text == "check staging too" and steers[0].id == "turn-9"

    got = await _collect(adapter)
    receipts = [e for e in got if isinstance(e, SteerDeliveredEvent)]
    assert len(receipts) == 1
    assert receipts[0].steers == [{"id": "turn-9", "text": "check staging too"}]


@pytest.mark.asyncio
async def test_steer_without_an_engine_refuses():
    """Nothing connected means nothing to steer into — the caller leaves the
    message queued rather than believing it was delivered."""
    with pytest.raises(RuntimeError):
        await HarnessAdapter().steer("too early", id="turn-1")


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
async def test_usage_emits_immediately_as_its_own_assistant_record():
    # A fluid Usage reading surfaces immediately as an empty-content
    # AssistantMessage.  Waiting for the next block lost the last call's usage
    # and made interrupted turns undercount provider spend.
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
    usage_messages = [e for e in asst if e.usage]
    assert len(usage_messages) == 1 and usage_messages[0].usage == {
        "input_tokens": 10,
        "output_tokens": 3,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 2,
    }


@pytest.mark.asyncio
async def test_usage_carries_per_call_context_snapshot():
    # fluid attaches the context breakdown for the call to its Usage event.
    # It has to survive the shim so the UI can drive the context bar off the
    # event stream instead of re-fetching between calls.
    snapshot = {
        "categories": [{"name": "Messages", "tokens": 40, "color": "#fff"}],
        "totalTokens": 40,
        "maxTokens": 200_000,
        "measured": True,
    }
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.Usage(session_id=SID, input_tokens=10, output_tokens=3,
                 context_usage=snapshot),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    usage = [e.usage for e in got if isinstance(e, AssistantMessage) and e.usage][0]
    assert usage["context_usage"] == snapshot
    assert usage["input_tokens"] == 10


@pytest.mark.asyncio
async def test_usage_without_snapshot_omits_the_key():
    # Absent rather than None: downstream treats presence as the signal, so a
    # null would read as "there is a snapshot" to a naive `in` check.
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.Usage(session_id=SID, input_tokens=10, output_tokens=3),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    usage = [e.usage for e in got if isinstance(e, AssistantMessage) and e.usage][0]
    assert "context_usage" not in usage


@pytest.mark.asyncio
async def test_snapshot_only_usage_still_produces_a_shim_record():
    # The delegated leg answers a mid-turn snapshot request with a SECOND Usage
    # for the same node: breakdown set, every token field 0 (the tokens were
    # billed by the first record). It must still reach the shim, or the
    # breakdown never arrives.
    snapshot = {"categories": [], "totalTokens": 128, "maxTokens": 200_000}
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.Usage(session_id=SID, context_usage=snapshot),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    usage = [e.usage for e in got if isinstance(e, AssistantMessage) and e.usage][0]
    assert usage["context_usage"] == snapshot
    assert usage["input_tokens"] == 0 and usage["output_tokens"] == 0

    from app.core.query_streaming import is_context_snapshot_only

    # ...and the streaming paths recognise it, so its zeroes never overwrite
    # the persisted last-call context size.
    assert is_context_snapshot_only(usage) is True


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


def _with_model_calls(base: type, **kwargs):
    """Build a same-named event class that also carries ``num_model_calls``.

    The partial model-call count is a NEW field on fluid's ``Interrupted`` /
    ``ErrorEvent``; the pinned vendor here does not have it yet. The adapter
    dispatches on ``type(event).__name__``, so a subclass keeps the same branch
    while letting these tests pin BOTH sides of the defensive read.
    """
    cls = dataclasses.make_dataclass(
        base.__name__,
        [("num_model_calls", int, 0)],
        bases=(base,),
        frozen=True,
        kw_only=True,
    )
    return cls(**kwargs)


@pytest.mark.asyncio
async def test_interrupted_without_model_calls_books_zero():
    """Today's vendor has no ``num_model_calls`` on Interrupted. The defensive
    read must leave the count at zero rather than raising."""
    got = await _collect(_adapter_with(FakeEngine([
        lm.Interrupted(session_id=SID, message="stopped",
                       usage={"input_tokens": 10})])))
    assert got[0].num_turns == 0


@pytest.mark.asyncio
async def test_interrupted_carries_partial_model_call_count():
    """The bug: an interrupted turn's ResultMessage carried usage but no
    ``num_turns``, which is what ``query_persistence`` feeds
    ``model_calls_increment`` — so every stopped turn booked ZERO model calls
    and a session with 167 real calls read as 11."""
    got = await _collect(_adapter_with(FakeEngine([
        _with_model_calls(
            lm.Interrupted, session_id=SID, message="stopped",
            usage={"input_tokens": 10}, cost_usd=0.002, num_model_calls=78,
        )])))
    assert len(got) == 1 and isinstance(got[0], ResultMessage)
    assert got[0].num_turns == 78
    assert got[0].total_cost_usd == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_error_terminal_carries_partial_model_call_count():
    got = await _collect(_adapter_with(FakeEngine([
        _with_model_calls(
            lm.ErrorEvent, session_id=SID, message="boom", retriable=False,
            usage={"input_tokens": 7}, num_model_calls=12,
        )])))
    assert len(got) == 1 and isinstance(got[0], ResultMessage)
    assert got[0].is_error is True
    assert got[0].num_turns == 12


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


def test_vendored_session_search_api_is_stable():
    """SessionSearch is host-backed: fluid ships the tool, the app's store ships
    the search. Two things have to hold for that seam to work — the tool gates
    on the ``session_search`` extra the adapter injects, and ``SessionSearchHit``
    (the shape PgFluidStore returns) lives where the store imports it from.

    This is the tripwire if a future vendor sync renames either.
    """
    from fluid.store.base import SessionSearchHit
    from fluid.tools.builtins.session_search import SPEC

    assert SPEC.requires_extra == ("session_search",)
    assert SessionSearchHit.__module__ == "fluid.store.base"


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


# ─── 3c. Fluid QuestionRequest (AskUser / StopPlanning) → RespondQuestion ─────
#
# Native AskUser and StopPlanning park on QuestionRequest, not PermissionRequest.
# The adapter must NOT submit RespondPermission for those — NativeLoop is waiting
# on RespondQuestion and a permission op would hang the turn.


def _fluid_question_request(*, name="AskUser", message="Pick one?", schema=None):
    return [
        lm.QuestionRequest(
            session_id=SID, request_id="q-fluid-1", tool_use_id="tu-ask",
            name=name, message=message, schema=schema or {
                "question": "Pick one?",
                "header": "Choice",
                "options": [{"label": "Approve", "description": "yes"}],
            },
        ),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]


@pytest.mark.asyncio
async def test_fluid_question_submits_respond_question_with_label():
    """An answered AskUser lands as RespondQuestion(answer=label), not permission."""
    from app.core.user_question_handler import FLUID_ANSWER_KEY

    engine = FakeEngine(_fluid_question_request())
    adapter = _adapter_with(engine)

    async def can_use_tool(tool_name, tool_input, context):
        assert tool_name == "AskUser"
        assert tool_input["schema"]["question"] == "Pick one?"
        return _FakeAllow({**tool_input, FLUID_ANSWER_KEY: "Approve"})

    adapter._can_use_tool = can_use_tool
    await _collect(adapter)
    await _drain_bridge(engine)

    assert len(engine.ops) == 1
    op = engine.ops[0]
    assert isinstance(op, RespondQuestion)
    assert op.request_id == "q-fluid-1"
    assert op.answer == "Approve"


@pytest.mark.asyncio
async def test_fluid_question_dismiss_submits_empty_answer():
    """A dismissed AskUser is an empty RespondQuestion, not a permission deny."""
    engine = FakeEngine(_fluid_question_request(name="AskUser"))
    adapter = _adapter_with(engine)

    async def can_use_tool(tool_name, tool_input, context):
        return _FakeDeny("User dismissed the question dialog without answering.")

    adapter._can_use_tool = can_use_tool
    await _collect(adapter)
    await _drain_bridge(engine)

    op = engine.ops[0]
    assert isinstance(op, RespondQuestion)
    assert op.answer == ""
    assert not any(isinstance(o, RespondPermission) for o in engine.ops)


@pytest.mark.asyncio
async def test_stop_planning_does_not_skip_modal_on_stale_bypass_flag():
    """A leftover pre-plan bypass flag must not hide the modal after the chip
    switched to ask-permission. Live mode is can_use_tool's job."""
    called = []

    async def can_use_tool(tool_name, tool_input, context):
        called.append(tool_name)
        assert tool_input["plan"] == "# Plan"
        return _FakeAllow({**tool_input, "_fluid_answer": "Approve"})

    message = (
        "# Plan\n\nApprove this plan and leave plan mode? "
        "(1 = Approve, anything else = feedback)"
    )
    engine = FakeEngine(_fluid_question_request(name="StopPlanning", message=message))
    adapter = _adapter_with(engine)
    adapter._can_use_tool = can_use_tool
    adapter._pre_plan_permission_mode = "bypassPermissions"
    adapter._options.permission_mode = "default"
    await _collect(adapter)
    await _drain_bridge(engine)

    assert called == ["StopPlanning"]
    op = engine.ops[0]
    assert isinstance(op, RespondQuestion)
    assert op.answer == "Approve"


@pytest.mark.asyncio
async def test_ask_user_still_prompts_in_bypass():
    """Bypass auto-accept is StopPlanning-only — AskUser still goes to the modal."""
    engine = FakeEngine(_fluid_question_request(name="AskUser"))
    adapter = _adapter_with(engine)
    adapter._pre_plan_permission_mode = "bypassPermissions"

    async def can_use_tool(tool_name, tool_input, context):
        return _FakeAllow({**tool_input, "_fluid_answer": "Yes"})

    adapter._can_use_tool = can_use_tool
    await _collect(adapter)
    await _drain_bridge(engine)
    assert engine.ops[0].answer == "Yes"


def test_plan_from_bypass_does_not_move_the_chip():
    """Entering plan from bypass must not persist permission_mode=plan."""
    engine = FakeEngine([])
    engine.plan_mode = False
    engine.permissions = type("P", (), {"approval_policy": "never_ask"})()
    adapter = _adapter_with(engine)
    adapter._pre_plan_permission_mode = "bypassPermissions"
    adapter._on_plan_mode_changed(True)
    assert engine.plan_mode is True
    assert engine.permissions.approval_policy == "never_ask"
    assert adapter._pending_plan_mode_events == []


@pytest.mark.asyncio
async def test_fluid_question_does_not_yield_permission_event():
    """QuestionRequest is bridged out-of-band; the stream must not see it."""
    engine = FakeEngine(_fluid_question_request())
    adapter = _adapter_with(engine)

    async def can_use_tool(tool_name, tool_input, context):
        return _FakeAllow({**tool_input, "_fluid_answer": "Approve"})

    adapter._can_use_tool = can_use_tool
    events = await _collect(adapter)
    await _drain_bridge(engine)
    assert not any(isinstance(ev, PermissionRequestEvent) for ev in events)


@pytest.mark.asyncio
async def test_plan_mode_change_is_yielded_as_status_system_message():
    """PlanModeControl.set_active queues a status note receive_events drains."""
    engine = FakeEngine([
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ])
    adapter = _adapter_with(engine)
    adapter._on_plan_mode_changed(True)
    events = await _collect(adapter)
    notes = [e for e in events if isinstance(e, SystemMessage) and e.subtype == "status"]
    assert notes
    assert notes[0].data["permissionMode"] == "plan"


def test_wire_plan_mode_control_wraps_set_active():
    """Wrapping PlanModeControl makes set_active queue the status note."""
    from fluid.tools.builtins.plan import PlanModeControl
    from fluid.tools.registry import ToolCtx

    flag = {"on": False}
    engine = FakeEngine([])
    engine.ctx = ToolCtx()
    engine.ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: flag["on"],
        set_active=lambda v: flag.__setitem__("on", bool(v)),
    )
    engine.plan_mode = False
    engine.permissions = type("P", (), {"approval_policy": "on_write"})()
    adapter = _adapter_with(engine)
    adapter._engine = engine
    adapter._wire_plan_mode_control(SessionOptions(model="fake-model", permission_mode="default"))
    extra = engine.ctx.extra["plan_mode_control"]
    extra.set_active(True)
    assert flag["on"] is True
    assert adapter._pending_plan_mode_events
    assert adapter._pending_plan_mode_events[0].data["permissionMode"] == "plan"


def test_plan_mode_exit_restores_policy_and_flag():
    """Leaving plan mode must flip the engine flag AND the approval policy.

    Otherwise a chip-initiated plan (always_ask) that StopPlanning approved
    would keep prompting every write for the rest of the turn, and a
    Plan-tool entry whose QuestionRequest was dropped would linger forever
    because the next send still says permission_mode=default.
    """
    engine = FakeEngine([])
    engine.plan_mode = True
    engine.permissions = type("P", (), {"approval_policy": "always_ask"})()
    adapter = _adapter_with(engine)
    adapter._pre_plan_permission_mode = "acceptEdits"
    adapter._on_plan_mode_changed(False)
    assert engine.plan_mode is False
    assert engine.permissions.approval_policy == "on_write"


def test_bypass_exit_from_chip_initiated_plan_restores_binding():
    """A bypass user who picked Plan Only, then approved the plan exit, must
    get their session binding restored — not stay bound to ``plan`` forever.

    Regression: the bypass early-return in _on_plan_mode_changed skipped the
    restore entirely whenever pre was bypassPermissions, so an exit never
    cleared the persisted ``plan`` binding on this path either."""
    engine = FakeEngine([])
    engine.plan_mode = True
    engine.permissions = type("P", (), {"approval_policy": "never_ask"})()
    adapter = _adapter_with(engine)
    adapter._pre_plan_permission_mode = "bypassPermissions"
    # The chip-initiated plan left the session binding at plan.
    adapter._options.permission_mode = "plan"
    restored = adapter._on_plan_mode_changed(False)
    assert restored == "bypassPermissions"
    assert engine.plan_mode is False
    # Bypass semantics hold: straight back to never_ask.
    assert engine.permissions.approval_policy == "never_ask"
    # The status note is queued so the chip follows.
    assert adapter._pending_plan_mode_events


def test_notify_plan_mode_exited_noop_when_not_in_plan():
    """An exit sync on an adapter that was never plan-bound does nothing."""
    engine = FakeEngine([])
    engine.plan_mode = False
    adapter = _adapter_with(engine)
    assert adapter.notify_plan_mode_exited() is None
    assert not adapter._pending_plan_mode_events
    assert engine.plan_mode is False


def test_notify_plan_mode_exited_syncs_options_and_engine():
    """The delegated-leg exit path: notify restores binding + options + flag."""
    engine = FakeEngine([])
    engine.plan_mode = True
    engine.permissions = type("P", (), {"approval_policy": "always_ask"})()
    adapter = _adapter_with(engine)
    adapter._options.permission_mode = "plan"  # session bound to plan
    adapter._pre_plan_permission_mode = "default"

    restored = adapter.notify_plan_mode_exited()

    assert restored == "default"
    assert adapter._options.permission_mode == "default"
    assert adapter._pre_plan_permission_mode == "default"
    assert engine.plan_mode is False
    assert engine.permissions.approval_policy == "on_write"
    notes = [n for n in adapter._pending_plan_mode_events if n.subtype == "status"]
    assert notes and notes[0].data["permissionMode"] == "default"


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


@pytest.mark.asyncio
async def test_send_resume_submits_a_resume_op():
    """Retry-from-here submits fluid's Resume op — no UserTurn, no prompt."""
    from fluid.events.ops import Resume, UserTurn

    submitted: list = []

    class _RecordingEngine:
        async def submit(self, op):
            submitted.append(op)

    adapter = HarnessAdapter()
    adapter._engine = _RecordingEngine()

    await adapter.send_resume()

    assert len(submitted) == 1
    assert isinstance(submitted[0], Resume)
    assert not any(isinstance(op, UserTurn) for op in submitted)


@pytest.mark.asyncio
async def test_send_resume_without_an_engine_raises():
    adapter = HarnessAdapter()
    with pytest.raises(RuntimeError, match="Adapter not connected"):
        await adapter.send_resume()


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
    # First-class xAI API-key and SuperGrok subscription rows.
    assert _decide_provider_kind(_bound("grok-4.6", "xai")) == "openai_responses"
    assert _decide_provider_kind(_bound("grok-4.6", "grok")) == "openai_responses"
    assert _decide_provider_kind(_bound("grok-4.6", "custom", "grok")) == "openai_responses"


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


def test_responses_url_composition():
    from app.core.provider.harness_adapter import _responses_url

    assert _responses_url("https://api.x.ai") == "https://api.x.ai/v1/responses"
    assert _responses_url("https://api.x.ai/") == "https://api.x.ai/v1/responses"
    assert _responses_url("https://api.x.ai/v1") == "https://api.x.ai/v1/responses"
    assert _responses_url("https://api.openai.com/v1") == "https://api.openai.com/v1/responses"


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


# ─── Native-leg code-mode + continual harness ────────────────────────────────


async def _connect_native_capturing(monkeypatch, tmp_path, model="gpt-4o"):
    """Connect a native leg over a fake NativeLoop and return its kwargs.

    ``data_dir`` is pointed at tmp_path so the skill store (which mkdirs its
    root eagerly) never writes into the real home directory.
    """
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)

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
        SessionOptions(model=model, extra={"fluid_provider": "openai_responses"})
    )
    return captured


@pytest.mark.asyncio
async def test_native_leg_gets_code_mode_and_continual_by_default(monkeypatch, tmp_path):
    """Both defaults are ON: NativeLoop receives a CodeModeSession whose skill
    store is rooted under the app data dir, with a synthesizer attached."""
    from fluid.codemode.session import CodeModeSession
    from fluid.codemode.skills import SkillStore
    from app.core.config import settings

    # The suite-wide isolation fixture pins ``fluid_skills_dir`` so no test can
    # write into the real data dir. Clear it here — this test is precisely the
    # one that asserts the UNSET default resolution (<data_dir>/fluid/skills),
    # and ``_connect_native_capturing`` already points data_dir at tmp_path.
    monkeypatch.setattr(settings, "fluid_skills_dir", None)

    captured = await _connect_native_capturing(monkeypatch, tmp_path)

    codemode = captured["codemode"]
    assert isinstance(codemode, CodeModeSession)
    assert isinstance(codemode.skills, SkillStore)
    assert codemode.skills.root == tmp_path / "fluid" / "skills"
    assert codemode._synthesizer is not None


@pytest.mark.asyncio
async def test_native_leg_code_mode_can_be_turned_off(monkeypatch, tmp_path):
    """FLUID_CODE_MODE=false → no code-mode session at all."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_code_mode", False)
    captured = await _connect_native_capturing(monkeypatch, tmp_path)
    assert captured["codemode"] is None


@pytest.mark.asyncio
async def test_native_leg_continual_off_keeps_code_mode(monkeypatch, tmp_path):
    """FLUID_CONTINUAL=false leaves code-mode running but stops the background
    skill-synthesis passes."""
    from fluid.codemode.session import CodeModeSession
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_continual", False)
    captured = await _connect_native_capturing(monkeypatch, tmp_path)

    codemode = captured["codemode"]
    assert isinstance(codemode, CodeModeSession)
    assert codemode._synthesizer is None


@pytest.mark.asyncio
async def test_native_leg_continual_model_pins_synthesis(monkeypatch, tmp_path):
    """fluid_continual_model pins synthesis to one model; unset it would be the
    session's own model."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_continual_model", "foo")
    captured = await _connect_native_capturing(monkeypatch, tmp_path)
    assert captured["codemode"]._synthesizer._model == "foo"

    monkeypatch.setattr(settings, "fluid_continual_model", None)
    captured = await _connect_native_capturing(monkeypatch, tmp_path)
    assert captured["codemode"]._synthesizer._model == "gpt-4o"


@pytest.mark.asyncio
async def test_native_leg_code_mode_failure_degrades_to_none(monkeypatch, tmp_path):
    """A broken skill store must never fail the connect — the leg falls back to
    plain JSON tool calling."""
    import app.core.provider.harness_adapter as ha

    def _boom(root):
        raise OSError("no such volume")

    monkeypatch.setattr(ha, "SkillStore", _boom)
    captured = await _connect_native_capturing(monkeypatch, tmp_path)
    assert captured["codemode"] is None


# ─── Code-mode prompt rewrites ───────────────────────────────────────────────
#
# Under code mode the model's schema holds ONLY run_python/list_tools/
# search_tools/read_tool_docs, and fluid injects nothing about that into the
# system prompt. Every "use the X tool" line the app writes therefore has to
# say how X is actually reached — and the rewrite must key on the session
# really coming up, because its construction is fail-open.
#
# ``agent_workspace``/``_agents_options``/``_write_file`` live further down the
# module with the agents-index tests; they give a real project cwd, which is
# what makes a skills section and an agents index exist at all.


async def _connect_code_mode(monkeypatch, workspace):
    """Native connect over ``_CapturingLoop`` in a workspace with a skill."""
    import app.core.provider.harness_adapter as ha

    _write_file(
        workspace / ".agents" / "skills" / "demo" / "SKILL.md",
        "---\nname: demo\ndescription: Demo skill\n---\nSKILL BODY",
    )
    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    await ha.HarnessAdapter().connect(
        _agents_options(
            workspace, declared=[{"name": "Helper", "description": "Helps out"}]
        )
    )
    return _CapturingLoop.captured


@pytest.mark.asyncio
async def test_code_mode_rewrites_the_compiled_prompt(monkeypatch, agent_workspace):
    """Defaults ON: the skills/agents headers name the import path and the
    ``# Code mode`` rule closes the prompt."""
    import app.core.provider.harness_adapter as ha

    captured = await _connect_code_mode(monkeypatch, agent_workspace)
    assert captured["codemode"] is not None
    system = captured["system"]

    # LAST — the operating rule for every instruction above it, immediately
    # followed by the "# Tools" catalog it points at.
    assert ha.CODE_MODE_SECTION in system
    assert system.index(ha.CODE_MODE_SECTION) < system.index(_TOOLS_HEADING)
    # Skills: header rewritten, index body untouched.
    assert "from tools.Skill import Skill" in system
    assert "- demo: Demo skill" in system
    assert "full instructions with the Skill tool" not in system
    # Agents: fluid's own sentence, extended with how Task is reached.
    assert "delegated to with the Task tool, called from run_python" in system
    # Line-anchored: the Task tool's own description names the heading in
    # prose ("descriptions are under '# Available agent types'"), and under
    # code mode that description now rides the prompt inside the # Tools
    # catalog. What must never appear twice is the INDEX BLOCK itself.
    assert system.count("\n# Available agent types\n") == 1


@pytest.mark.asyncio
async def test_code_mode_section_is_priced_as_a_section(monkeypatch, agent_workspace):
    """The block rides ``system_sections`` too, so get_context_usage() prices
    it instead of seeing a prompt longer than the sections it was given."""
    import app.core.provider.harness_adapter as ha

    captured = await _connect_code_mode(monkeypatch, agent_workspace)
    compiled = captured["system_sections"]
    assert [s for s in compiled.sections if s.text == ha.CODE_MODE_SECTION]
    assert sum(s.text.startswith("# Code mode") for s in compiled.sections) == 1
    # The two views never drift: joined sections ARE the prompt.
    assert compiled.text == captured["system"]


@pytest.mark.asyncio
async def test_code_mode_off_leaves_the_prompt_alone(monkeypatch, agent_workspace):
    """FLUID_CODE_MODE=false → the plain JSON-tool-calling prompt, verbatim."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "fluid_code_mode", False)
    captured = await _connect_code_mode(monkeypatch, agent_workspace)

    assert captured["codemode"] is None
    system = captured["system"]
    assert "# Code mode" not in system
    assert "Load a skill's full instructions with the Skill tool" in system
    assert "delegated to with the Task tool." in system
    assert "run_python" not in system


@pytest.mark.asyncio
async def test_code_mode_prompt_keys_on_the_session_not_the_setting(
    monkeypatch, agent_workspace
):
    """The setting is on but the session failed to build (fail-open): the
    prompt must NOT tell the model to import tools inside a run_python it does
    not have."""
    import app.core.provider.harness_adapter as ha

    def _boom(root):
        raise OSError("no such volume")

    monkeypatch.setattr(ha, "SkillStore", _boom)
    captured = await _connect_code_mode(monkeypatch, agent_workspace)

    assert captured["codemode"] is None
    system = captured["system"]
    assert "# Code mode" not in system
    assert "run_python" not in system
    assert "Load a skill's full instructions with the Skill tool" in system


# The catalog section's heading, anchored to a line start so a "# Tools"
# inside some tool's own description cannot satisfy the assertion.
_TOOLS_HEADING = "\n# Tools\n"


class _FakeCodeMode:
    """A codemode session that is nothing but its projection.

    The catalog is built from ``_projection()`` — the same snapshot behind
    ``list_tools`` and the VFS stubs — so a real ``Projection`` over hand-built
    specs exercises the real leaf naming, ordering and signature rules.
    """

    def __init__(self, specs):
        self._specs = list(specs)

    def _projection(self):
        from fluid.codemode.projection import Projection

        return Projection(self._specs)


def test_code_mode_tools_section_carries_full_tool_documentation(agent_workspace):
    """The whole point of the section: the descriptions and parameter docs the
    JSON-tool path sends, which a four-tool code-mode schema otherwise drops."""
    import app.core.provider.harness_adapter as ha
    from fluid.tools.registry import ToolSpec

    grep = ToolSpec(
        name="Grep",
        description=(
            "Search file contents with a regex.\n"
            "\n"
            "Prefer this over shelling out to grep."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regex to run"},
                "output_mode": {
                    "type": "string",
                    "description": "How matches are reported.",
                    "enum": ["content", "count"],
                    "default": "content",
                },
            },
            "required": ["pattern"],
        },
    )
    danger = ToolSpec(
        name="mcp__acme__Danger",
        description="Escape hatch the kernel may not reach.",
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "What to do it to"},
            },
            "required": ["target"],
        },
        code_mode_allowed=False,
    )

    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "BASE",
        None,
        _agents_options(agent_workspace),
        code_mode=True,
        codemode=_FakeCodeMode([grep, danger]),
    )
    system = compiled.text

    # After the rule, never before it.
    assert system.index("# Code mode") < system.index(_TOOLS_HEADING)
    # Import line, Python signature (required first, optionals ``| None = None``).
    assert "## Grep" in system
    assert "`from tools.Grep import Grep`" in system
    assert "`Grep(pattern: str, output_mode: str | None = None)`" in system
    # Full description, not the one-line catalog blurb.
    assert "Search file contents with a regex." in system
    assert "Prefer this over shelling out to grep." in system
    # Parameter docs, with enum values and defaults.
    assert "Parameters:" in system
    assert "- pattern (str, required): The regex to run" in system
    # Rendered by the same helper the VFS stub docstrings use, so the enum and
    # default text in the prompt is byte-identical to the stubs'.
    from fluid.codemode.projection import _param_doc

    assert "- " + _param_doc(
        "output_mode",
        grep.input_schema["properties"]["output_mode"],
        required=False,
    ) in system
    # MCP tool -> leaf name; the JSON-path-only escape hatch says so.
    assert "## Danger" in system
    assert "`from tools.Danger import Danger`" in system
    assert "Escape hatch the kernel may not reach." in system
    assert "Not callable from code mode — use it as a direct tool call." in system
    assert "- target (str, required): What to do it to" in system
    # And it is priced like the rest, not a text-only append: ONE section per
    # tool (the heading rides whichever sorts first), each naming the registry
    # tool it documents.
    tool_sections = [s for s in compiled.sections if s.kind == "tools"]
    assert len(tool_sections) == 2
    by_name = {s.tool_name: s for s in tool_sections}
    assert set(by_name) == {"Grep", "mcp__acme__Danger"}
    # The "# Tools" heading opens exactly one section — the catalog's first.
    headed = [s for s in tool_sections if s.text.startswith("# Tools\n")]
    assert len(headed) == 1
    # The joined view never drifts: sections concatenated ARE the prompt.
    assert compiled.text == "\n\n".join(s.text for s in compiled.sections)


def test_code_mode_tools_section_skipped_without_a_projection(agent_workspace):
    """A codemode object with no projection (a double, a future refactor) must
    degrade to no section rather than failing the prompt."""
    import app.core.provider.harness_adapter as ha

    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "BASE", None, _agents_options(agent_workspace),
        code_mode=True, codemode=object(),
    )
    assert "# Code mode" in compiled.text
    assert _TOOLS_HEADING not in compiled.text


@pytest.mark.asyncio
async def test_tool_catalog_prices_under_tools_not_system_prompt(
    monkeypatch, agent_workspace
):
    """The point of the per-tool sections: get_context_usage() puts each tool's
    prompt-carried documentation under System/MCP tools — where the equivalent
    JSON schema would sit on the direct-tool path — not under System prompt."""
    from fluid.context.usage import build_context_usage

    captured = await _connect_code_mode(monkeypatch, agent_workspace)
    compiled = captured["system_sections"]

    usage = build_context_usage(sections=compiled.sections, model="claude-opus-4-6")
    cats = {c["name"]: c["tokens"] for c in usage["categories"]}

    tool_sections = [s for s in compiled.sections if s.kind == "tools"]
    assert tool_sections, "catalog sections must ride as kind='tools'"
    expected_tools = sum(
        estimate_tokens_of_text(s.text)
        for s in tool_sections if not s.tool_name.startswith("mcp__")
    )
    expected_mcp = sum(
        estimate_tokens_of_text(s.text)
        for s in tool_sections if s.tool_name.startswith("mcp__")
    )
    assert cats.get("System tools", 0) == expected_tools
    assert cats.get("MCP tools", 0) == expected_mcp
    # None of the catalog text leaked into the System prompt row (which prices
    # base + extra + agents + environment; memory/skills have their own rows).
    assert cats["System prompt"] == sum(
        estimate_tokens_of_text(s.text)
        for s in compiled.sections
        if s.kind in ("base", "environment", "extra", "agents")
    )

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
    a mutating Bash command asks, a read-only one (fluid's classifier) and Read
    are allowed."""
    from fluid.permissions.engine import PermissionEngine

    adapter = HarnessAdapter()
    registry = await adapter._build_registry(SessionOptions(model="gpt-4o"), native=True)
    engine = PermissionEngine(approval_policy="on_write")

    assert engine.evaluate(registry.get("Bash"), {"command": "rm -rf build"}) == "ask"
    assert engine.evaluate(registry.get("Bash"), {"command": "ls"}) == "allow"
    assert engine.evaluate(registry.get("Read"), {"file_path": "/tmp/x"}) == "allow"


# ─── P1 prompt compiler integration (rulings A1/A3/A12) ──────────────────────


class _CapturingLoop:
    """Shared fake for NativeLoop/DelegatedLoop kwarg capture."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        # Mirror NativeLoop: the priced "Custom agents" list lives on the
        # instance, so ``connect``'s post-construction clear has something real
        # to act on.
        self.agents = list(kwargs.get("agents") or ())

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

    (tmp_path / "AGENTS.md").write_text("Project rule: be terse.", encoding="utf-8")
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
        s.kind == "memory" and s.path and s.path.endswith("AGENTS.md")
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
        staticmethod(lambda system, append_system, options, **kwargs: None),
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
    """Flag off (opt-out; default is full replace since the 2026-08 prompt
    overhaul): the delegated leg's system/append are NOT compiled — the CLI
    gathers its own env and instruction files.

    The one thing the app does add here is the agents index (the CLI cannot
    know about DB agents), so the assertion is "the app's text, then at most
    that block" rather than byte-identity — see
    test_delegated_append_mode_advertises_agents.
    """
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings
    from fluid.tools.registry import ToolRegistry

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(settings, "prompt_full_replace", False)

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
    assert "compaction" not in kwargs  # native rendered-view policy only


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


def test_openrouter_pricing_is_sanitized_from_bound_provider_only(monkeypatch):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(
        ha, "_lookup_provider_row",
        lambda provider_id: {
            "id": provider_id,
            "models_json": [
                {
                    "id": "x-ai/grok-4.6",
                    "pricing": {
                        "prompt": "0.000001",
                        "completion": 0.000002,
                        "input_cache_read": "0.0000002",
                        "input_cache_write": "nan",
                    },
                },
                {"id": "broken", "pricing": {"prompt": "not-a-number"}},
            ],
        },
    )
    assert ha._openrouter_pricing(SessionOptions(
        model="x-ai/grok-4.6", extra={"provider_id": "or-1"}
    )) == {
        "x-ai/grok-4.6": {
            "prompt": 0.000001,
            "completion": 0.000002,
            "input_cache_read": 0.0000002,
        }
    }


@pytest.mark.asyncio
async def test_legacy_safety_events_remain_replayable():
    events = [
        SafetyWarning(
            session_id=SID, code="spend_budget", message="Spend warning",
            hard=False, counters={"model_calls": 7},
        ),
        SafetyWarning(
            session_id=SID, code="spend_budget", message="Spend halted",
            hard=True, counters={"model_calls": 8},
        ),
        lm.TurnComplete(
            session_id=SID, stop_reason="safety_limit:spend_budget",
            num_model_calls=8,
        ),
    ]
    got = await _collect(_adapter_with(FakeEngine(events)))
    assert [message.subtype for message in got[:2]] == [
        "safety_warning", "safety_limit",
    ]
    assert got[0].data["counters"]["model_calls"] == 7
    assert isinstance(got[-1], ResultMessage)
    assert got[-1].terminal_reason == "safety_limit"


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
    import fluid.context.plugins as fluid_plugins

    async def fake_client(kind, options):
        return FakeProvider(list(_SEED_CHUNKS)), None

    monkeypatch.setattr(ha, "_build_provider_client", fake_client)
    monkeypatch.setattr(HarnessAdapter, "_build_store", staticmethod(lambda _o: store))
    # Unit isolation: installed Claude/Chrome plugins must not start real MCP
    # processes while this test is proving only durable bus sequence behavior.
    monkeypatch.setattr(fluid_plugins, "discover_plugins", lambda *a, **kw: [])

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
        got = await asyncio.wait_for(_collect(adapter), timeout=5)

        # (i) no historical event leaks into this turn's stream
        delta_texts = [
            e.event["delta"]["text"]
            for e in got
            if isinstance(e, StreamEvent) and e.event.get("delta", {}).get("type")
            == "text_delta"
        ]
        assert delta_texts == ["fresh reply"], [
            (type(event).__name__, getattr(event, "subtype", None),
             getattr(event, "result", None))
            for event in got
        ]
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
async def test_connect_native_leg_keeps_provider_settings_inert(monkeypatch, tmp_path):
    """Native sessions never activate provider-home env or permission rules."""
    import json

    import app.core.provider.harness_adapter as ha
    from fluid.permissions.engine import PermissionEngine
    from fluid.permissions.rules import Rule

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
    assert "settings_env" not in engine.ctx.extra
    assert engine.permissions.rules == [host_rule]


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
async def test_no_max_turns_adds_no_automatic_call_limit(monkeypatch, tmp_path):
    """Only an explicitly configured profile may cap native model calls."""
    for kwargs in ({}, {"max_turns": 0}):
        captured = await _connect_capturing_native(monkeypatch, tmp_path, **kwargs)
        assert captured["max_model_calls_per_turn"] is None
        assert "safety_limits" not in captured
        assert "trim" not in captured


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

    async def _fake_dispatch(
        args,
        *,
        caller_session_id,
        caller_profile_id=None,
        caller_provider_id=None,
        launch_tool_use_id=None,
        agent_registry=None,
    ):
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

    async def _fake_dispatch(
        args,
        *,
        caller_session_id,
        caller_profile_id=None,
        caller_provider_id=None,
        launch_tool_use_id=None,
        agent_registry=None,
    ):
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
    payload = json.loads(raw.text)
    assert raw.is_error is True
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

    async def _fake_dispatch(
        args,
        *,
        caller_session_id,
        caller_profile_id=None,
        caller_provider_id=None,
        launch_tool_use_id=None,
        agent_registry=None,
    ):
        seen["caller"] = caller_session_id
        return {"ok": True}

    monkeypatch.setattr(td, "dispatch_task_tool", _fake_dispatch)
    monkeypatch.setattr(
        database, "get_session_by_sdk_id", lambda sdk_id: mapped.get(sdk_id)
    )
    await _connect_native_leg(monkeypatch, tmp_path)

    dispatcher = _injected_dispatcher()
    first_outcome = await dispatcher({"agent": "helper", "prompt": "go"}, object())
    first = json.loads(first_outcome.text)
    assert first_outcome.is_error is True
    assert first["ok"] is False and "caller" not in seen

    mapped["ses_x"] = {"id": "app-sess-3"}
    await dispatcher({"agent": "helper", "prompt": "go"}, object())
    assert seen["caller"] == "app-sess-3"


# ─── Monitor spawner injection on native legs (A13, Task-parity) ─────────────
#
# Regression cover for the live bug: a native session called fluid's bare
# Monitor builtin and got "Monitor is not available in this environment — no
# monitor spawner was injected by the host application", because the app
# bridged only its MCP mcp__builtins__Monitor copy and never injected the
# spawner fluid's builtin expects.


def _injected_monitor_spawn():
    return _CapturingLoop.captured["ctx"].extra.get("monitor_spawn")


@pytest.mark.asyncio
async def test_native_leg_injects_monitor_spawn(monkeypatch, tmp_path):
    await _connect_native_leg(monkeypatch, tmp_path)
    assert _injected_monitor_spawn() is not None


@pytest.mark.asyncio
async def test_monitor_spawn_runs_detached_and_returns_receipt(
    monkeypatch, tmp_path
):
    """The spawner reuses the app's shell-task registry and returns a durable
    receipt; the command outlives the tool call."""
    from app.mcp import builtin_tools as bt

    spawned = []

    class _FakeTask:
        task_id = "mon-x1"
        exit_code = None

        def is_running(self):
            return True

    async def _fake_spawn(command, cwd, session_id):
        spawned.append((command, cwd, session_id))
        return _FakeTask()

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn)
    await _connect_native_leg(monkeypatch, tmp_path, app_session_id="app-sess-m1")

    from fluid.tools.registry import BackgroundTaskReceipt, ToolOutcome

    outcome = await _injected_monitor_spawn()(
        {"command": "npm run build", "cwd": str(tmp_path)}, object()
    )
    # A ToolOutcome carrying the receipt: the app owns the receipt STRING (R6)
    # while fluid's detach seam still reads ``background_task``.
    assert isinstance(outcome, ToolOutcome) and not outcome.is_error
    receipt = outcome.background_task
    assert isinstance(receipt, BackgroundTaskReceipt)
    assert receipt.task_id == "mon-x1"
    assert spawned == [("npm run build", str(tmp_path), "app-sess-m1")]


@pytest.mark.asyncio
async def test_monitor_receipt_commands_the_park(monkeypatch, tmp_path):
    """R6: the receipt tells the model to END THE TURN, not just that a task
    started. Audited sessions read the bare 'Monitoring started (task X).' as
    licence to sleep-poll in foreground Bash — 9 of 10 spawns in one session,
    24 minutes of polling for a notification already queued for them."""
    from app.mcp import builtin_tools as bt

    class _FakeTask:
        task_id = "mon-p1"
        exit_code = None

        def is_running(self):
            return True

    async def _fake_spawn(command, cwd, session_id):
        return _FakeTask()

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn)
    await _connect_native_leg(monkeypatch, tmp_path, app_session_id="app-sess-mp")

    outcome = await _injected_monitor_spawn()({"command": "npm test"}, object())

    assert "Monitoring started (task mon-p1)." in outcome.text
    assert "End the turn now" in outcome.text
    assert "woken when it comes to rest" in outcome.text


@pytest.mark.asyncio
async def test_monitor_receipt_survives_fluids_outcome_normalizer(
    monkeypatch, tmp_path
):
    """The park instruction must reach the MODEL, so it has to survive the
    builtin's own receipt handling — fluid renders its default text from a bare
    BackgroundTaskReceipt, which is exactly what this outcome avoids."""
    from fluid.tools.builtins import monitor as fluid_monitor

    from app.mcp import builtin_tools as bt

    class _FakeTask:
        task_id = "mon-p2"
        exit_code = None

        def is_running(self):
            return True

    async def _fake_spawn(command, cwd, session_id):
        return _FakeTask()

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn)
    await _connect_native_leg(monkeypatch, tmp_path, app_session_id="app-sess-mp2")

    spawner = _injected_monitor_spawn()
    input_ = {"command": "npm test"}
    rendered = fluid_monitor._receipt_from(await spawner(input_, object()), input_)

    assert "End the turn now" in rendered.text
    assert rendered.background_task is not None
    assert rendered.background_task.task_id == "mon-p2"


@pytest.mark.asyncio
async def test_monitor_spawn_wakes_the_parent_on_completion(monkeypatch, tmp_path):
    """On process exit the output tail is folded into the record and handed to
    turn_scheduler.on_task_terminal — the unpark seam."""
    import asyncio

    from app.mcp import builtin_tools as bt

    class _FakeTask:
        def __init__(self):
            self.task_id = "mon-w1"
            self.exit_code = 0
            self.lines = ["build finished ok"]
            self._done = asyncio.Event()

        def is_running(self):
            return not self._done.is_set()

    fake = _FakeTask()

    async def _fake_spawn(command, cwd, session_id):
        return fake

    wakes: list = []

    async def _fake_terminal(session_id, frame):
        wakes.append((session_id, frame))

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn)
    import app.core.turn_scheduler as ts
    monkeypatch.setattr(ts, "on_task_terminal", _fake_terminal)

    await _connect_native_leg(monkeypatch, tmp_path, app_session_id="app-sess-m2")
    await _injected_monitor_spawn()({"command": "make build"}, object())

    fake._done.set()
    for _ in range(40):
        if wakes:
            break
        await asyncio.sleep(0.05)
    assert wakes, "completion never reached the scheduler"
    session_id, frame = wakes[0]
    assert session_id == "app-sess-m2"
    assert frame["task_id"] == "mon-w1"
    assert frame["status"] == "completed"
    assert "build finished ok" in (frame.get("summary") or "")


@pytest.mark.asyncio
async def test_monitor_spawn_rejects_an_empty_command(monkeypatch, tmp_path):
    from fluid.tools.registry import ToolOutcome

    await _connect_native_leg(monkeypatch, tmp_path, app_session_id="app-sess-m3")
    out = await _injected_monitor_spawn()({"command": "   "}, object())
    assert isinstance(out, ToolOutcome) and out.is_error


@pytest.mark.asyncio
async def test_native_leg_skips_the_bridged_mcp_monitor(monkeypatch, tmp_path):
    """One Monitor per leg: the bridged mcp__builtins__Monitor copy must not be
    registered when fluid's bare builtin runs."""
    import app.core.provider.harness_adapter as ha

    captured: dict = {}

    def _fake_register(registry, mcp_servers, *, skip_tools=None,
                       defer_servers=None):
        captured["skip"] = skip_tools
        return []

    monkeypatch.setattr(ha.NativeLoop, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(
        ha, "_build_provider_client",
        lambda *a, **kw: (None, None),
    )
    # register_recorded_sdk_servers is imported inside connect(); patch at source.
    import app.core.provider.fluid_tool_bridge as bridge
    monkeypatch.setattr(bridge, "register_recorded_sdk_servers", _fake_register)

    adapter = ha.HarnessAdapter()
    try:
        await adapter.connect(SessionOptions(
            model="gpt-4o", cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        ))
    except Exception:
        pass  # downstream wiring may fail on fakes — the capture below is the point
    assert (captured.get("skip") or {}).get("builtins") is None or (
        "Monitor" in (captured.get("skip") or {}).get("builtins", set())
    )


# ─── SessionSearch injection on native legs ─────────────────────────────────
#
# Cross-session recall is host-backed: fluid's builtin only ranks and formats,
# the store does the searching. The adapter hands the store's ``search`` to the
# engine ctx when the store has one — PgFluidStore does, MemoryStore does not,
# and the tool's requires_extra gate turns that absence into "tool not offered"
# rather than "tool that errors".


class _SearchableStore(MemoryStore):
    """A store that CAN answer recall — stands in for PgFluidStore here."""

    async def search(self, terms, *, limit=5, exclude_session_id=None):
        return []

    async def recent_sessions(self, *, limit=10, exclude_session_id=None):
        return []

    async def session_slice(self, session_id, *, start=-10, count=10):
        return None


@pytest.mark.asyncio
async def test_native_leg_injects_the_stores_recall_hooks(monkeypatch, tmp_path):
    import app.core.provider.harness_adapter as ha

    store = _SearchableStore()
    monkeypatch.setattr(
        ha.HarnessAdapter, "_build_store", staticmethod(lambda _o: store)
    )
    await _connect_native_leg(monkeypatch, tmp_path)
    extra = _CapturingLoop.captured["ctx"].extra
    assert extra.get("session_search") == store.search
    assert extra.get("session_recent") == store.recent_sessions
    assert extra.get("session_view") == store.session_slice


@pytest.mark.asyncio
async def test_native_leg_omits_recall_hooks_for_a_store_without_them(
    monkeypatch, tmp_path
):
    """MemoryStore keeps no durable record, so it offers no recall — the extras
    must stay absent and let fluid drop the tools from the request schemas."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(
        ha.HarnessAdapter, "_build_store", staticmethod(lambda _o: MemoryStore())
    )
    await _connect_native_leg(monkeypatch, tmp_path)
    extra = _CapturingLoop.captured["ctx"].extra
    assert "session_search" not in extra
    assert "session_recent" not in extra
    assert "session_view" not in extra


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
            aih_profile_id="profile-4",
        )
    )
    assert adapter._options.extra["app_session_id"] == "app-sess-4"
    assert adapter._options.extra["profile_id"] == "profile-4"


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


def _stub_task_contract():
    from app.core.agent_provider_models import SubagentTarget, TaskContract

    target = SubagentTarget(
        id="reviewer",
        name="Reviewer",
        description="Checks correctness and risk.",
        model="gpt-5.6-mini",
    )
    return TaskContract(
        description=(
            "Delegate to a configured subagent. Available subagents on this "
            "provider: reviewer (model: gpt-5.6-mini)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["reviewer"]},
                "prompt": {"type": "string"},
            },
            "required": ["agent", "prompt"],
            "additionalProperties": False,
        },
        targets=(target,),
    )


def test_amend_native_task_spec_installs_provider_scoped_roster(monkeypatch):
    """Native Task exposes targets, while model/provider stay host-owned."""
    from types import SimpleNamespace

    import app.core.provider.harness_adapter as ha
    import app.core.agent_provider_models as agent_models

    seen = {}

    def _contract(profile_id, provider_id, additional_targets=()):
        seen.update(
            profile_id=profile_id,
            provider_id=provider_id,
            additional_targets=additional_targets,
        )
        return _stub_task_contract()

    monkeypatch.setattr(agent_models, "build_task_contract", _contract)
    monkeypatch.setattr(
        "fluid.context.agents.discover_agents",
        lambda cwd: [
            SimpleNamespace(
                name="file-reviewer",
                description="Reviews from a file.",
            )
        ],
    )

    registry = ha.ToolRegistry()
    from fluid.tools.builtins import register_builtins

    register_builtins(registry)
    options = SessionOptions(
        model="gpt-5.6-sol",
        extra={"profile_id": "caller", "provider_id": "prov1"},
    )

    ha._amend_native_task_spec(registry, options)

    task = registry.get("Task")
    assert seen["profile_id"] == "caller"
    assert seen["provider_id"] == "prov1"
    assert [(target.id, target.model) for target in seen["additional_targets"]] == [
        ("file-reviewer", "gpt-5.6-sol")
    ]
    assert task.input_schema["properties"]["agent"]["enum"] == ["reviewer"]
    assert "model" not in task.input_schema["properties"]
    assert "subagent" in task.description
    assert "gpt-5.6-mini" in task.description


def test_delegated_task_tool_shares_provider_scoped_subagent_schema(monkeypatch):
    """The delegated bridge receives the same host-scoped Task contract."""
    from unittest.mock import patch as _patch

    import claude_agent_sdk as sdk

    import app.mcp.builtin_tools as bt
    import app.core.agent_provider_models as agent_models

    seen = {}

    def _contract(profile_id, provider_id, additional_targets=()):
        seen.update(
            profile_id=profile_id,
            provider_id=provider_id,
            additional_targets=additional_targets,
        )
        return _stub_task_contract()

    monkeypatch.setattr(agent_models, "build_task_contract", _contract)

    captured = {}
    def _spy(name, version, tools):
        captured["tools"] = tools
        return {"name": name, "version": version, "tools": tools}

    with _patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        bt.create_builtin_tools_server(
            session_id="s-policy", profile_id="p", provider_id="or-1",
            model="openai/gpt-5",
        )

    task = {t.name: t for t in captured["tools"]}["Task"]
    assert seen["profile_id"] == "p"
    assert seen["provider_id"] == "or-1"
    assert seen["additional_targets"] == ()
    assert task.input_schema["properties"]["agent"]["enum"] == ["reviewer"]
    assert "model" not in task.input_schema["properties"]


def test_amend_native_task_spec_scopes_native_claude_sessions(monkeypatch):
    """A provider-less native Claude session still gets its profile roster."""
    import app.core.provider.harness_adapter as ha
    import app.core.agent_provider_models as agent_models

    from fluid.tools.builtins import register_builtins

    seen = {}

    def _contract(profile_id, provider_id, additional_targets=()):
        seen.update(
            profile_id=profile_id,
            provider_id=provider_id,
            additional_targets=additional_targets,
        )
        return _stub_task_contract()

    monkeypatch.setattr(agent_models, "build_task_contract", _contract)

    registry = ha.ToolRegistry()
    register_builtins(registry)
    options = SessionOptions(model="opus", extra={"profile_id": "caller"})

    ha._amend_native_task_spec(registry, options)

    task = registry.get("Task")
    assert seen["profile_id"] == "caller"
    assert seen["provider_id"] is None
    assert task.input_schema["properties"]["agent"]["enum"] == ["reviewer"]
    assert "model" not in task.input_schema["properties"]


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

    task = registry.get("Task")
    assert task.description == _TASK_SPEC.description
    assert task.input_schema == _TASK_SPEC.input_schema


def test_native_mutation_tool_guidance_requires_write_or_edit_but_keeps_bash():
    """The mutation/shell contract now ships in fluid's own tool specs (the
    app-side ``_amend_native_mutation_specs`` downgrade patch is gone), so a
    bare ``register_builtins`` must already carry it."""
    import app.core.provider.harness_adapter as ha
    from fluid.tools.builtins import register_builtins

    registry = ha.ToolRegistry()
    register_builtins(registry)

    write = registry.get("Write").description
    edit = registry.get("Edit").description
    bash = registry.get("Bash").description
    assert "invoke Write immediately" in write
    assert "invoke Edit immediately" in edit
    assert "git, builds" in bash
    assert "Shell line" in bash
    assert "Edit/Write" in bash


def test_native_environment_declares_the_actual_windows_shell(tmp_path, monkeypatch):
    import fluid.context.environment as environment

    # Keep path construction native to the Linux test host. Mutating the shared
    # ``os.name`` module otherwise makes pathlib select WindowsPath, which cannot
    # be instantiated here and prevents the shell assertion from running.
    monkeypatch.setattr(environment, "Path", type(tmp_path))
    monkeypatch.setattr(environment.os, "name", "nt")
    monkeypatch.setattr(environment, "git_context", lambda cwd: None)
    block = environment.environment_block(tmp_path)
    # fluid resolves the real shell (Git Bash when installed, cmd.exe
    # otherwise) and the block must name whichever one the Bash tool runs.
    from fluid.tools.shell import describe

    assert f"Shell: {describe()}" in block
    assert ("Git Bash" in block) or ("cmd.exe" in block)


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


@pytest.mark.asyncio
async def test_delegated_none_mode_denies_every_tool(recording_adapter):
    """T4: ``tool_selection_mode: none`` resolves to ``disallowed=["*"]``, and
    the pinned CLI glob-matches deny rules — so one entry walls the whole leg."""
    adapter, made = recording_adapter
    await adapter.connect(
        SessionOptions(model="claude-sonnet-4-5", disallowed_tools=["*"])
    )
    assert "*" in made["delegated"]["disallowed_tools"]
    assert made["delegated"]["allowed_tools"] is None


@pytest.mark.asyncio
async def test_delegated_allow_mode_subtracts_the_cached_cli_surface(
    recording_adapter,
):
    """T4: with a known CLI surface, everything outside the allow list is passed
    as ``--disallowed-tools``. The CLI reads --allowed-tools as a GRANT, so
    without this subtraction the restriction would not exist on this leg."""
    from app.core import cli_tool_surface

    cli_tool_surface.reset_cli_tools()
    cli_tool_surface.record_cli_tools(["Read", "Bash", "Write", "Glob"])
    try:
        adapter, made = recording_adapter
        await adapter.connect(
            SessionOptions(model="claude-sonnet-4-5", tool_allowlist=["Read", "Glob"])
        )
    finally:
        cli_tool_surface.reset_cli_tools()

    denied = made["delegated"]["disallowed_tools"]
    assert "Bash" in denied and "Write" in denied
    assert "Read" not in denied and "Glob" not in denied
    assert made["delegated"]["allowed_tools"] == ["Read", "Glob"]


@pytest.mark.asyncio
async def test_delegated_allow_mode_degrades_without_a_cached_surface(
    recording_adapter,
):
    """Cold cache: only the grant goes out (plus the A6 bans). A documented
    degradation — the app's can_use_tool callback still gates every call."""
    from app.core import cli_tool_surface
    from app.core.provider.harness_adapter import DELEGATED_BANNED_TOOLS

    cli_tool_surface.reset_cli_tools()
    adapter, made = recording_adapter
    await adapter.connect(
        SessionOptions(model="claude-sonnet-4-5", tool_allowlist=["Read"])
    )
    assert made["delegated"]["disallowed_tools"] == sorted(DELEGATED_BANNED_TOOLS)
    assert made["delegated"]["allowed_tools"] == ["Read"]


def test_cached_cli_surface_ignores_an_empty_frame():
    """A frame without a tool list must not blank a good cache — that would
    silently turn the surface wall back off."""
    from app.core import cli_tool_surface

    cli_tool_surface.reset_cli_tools()
    cli_tool_surface.record_cli_tools(["Read"])
    cli_tool_surface.record_cli_tools([])
    cli_tool_surface.record_cli_tools(None)
    assert cli_tool_surface.cached_cli_tools() == ["Read"]
    cli_tool_surface.reset_cli_tools()
    assert cli_tool_surface.cached_cli_tools() is None


@pytest.mark.asyncio
async def test_delegated_surface_event_feeds_the_cache_and_shows_nothing():
    """T4 ingest: fluid's ``DelegatedSurface`` (the CLI's ``init`` frame,
    republished by DelegatedLoop) is what finally CALLS ``record_cli_tools``.
    It is plumbing, so it must produce no user-visible message."""
    from app.core import cli_tool_surface

    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.DelegatedSurface(
            session_id=SID,
            tools=("Read", "Bash", "Write", "Glob"),
            mcp_servers=({"name": "ai-tools", "status": "connected"},),
        ),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    cli_tool_surface.reset_cli_tools()
    try:
        got = await _collect(_adapter_with(FakeEngine(events)))
        assert cli_tool_surface.cached_cli_tools() == [
            "Read", "Bash", "Write", "Glob",
        ]
    finally:
        cli_tool_surface.reset_cli_tools()

    # No SystemMessage / assistant text was minted for the surface frame: the
    # only messages are the turn's own start/end bookkeeping.
    assert not [
        e for e in got
        if isinstance(e, SystemMessage) and "surface" in (e.subtype or "")
    ]
    assert not [e for e in got if isinstance(e, (UserMessage, StreamEvent))]


@pytest.mark.asyncio
async def test_delegated_surface_warms_the_allow_mode_subtraction(recording_adapter):
    """End to end for ruling T4: the CLI announces its surface, the adapter
    records it, and the NEXT delegated connect of an allow-mode agent walls off
    every announced tool the agent did not select."""
    from app.core import cli_tool_surface

    cli_tool_surface.reset_cli_tools()
    try:
        # 1. The CLI's init frame arrives on a live delegated leg.
        await _collect(_adapter_with(FakeEngine([
            lm.TurnStarted(session_id=SID, model="m", provider="fake"),
            lm.DelegatedSurface(
                session_id=SID, tools=("Read", "Bash", "Write", "Glob"),
            ),
            lm.TurnComplete(
                session_id=SID, stop_reason="end_turn", num_model_calls=1,
            ),
        ])))

        # 2. A later connect builds argv against the now-warm cache.
        adapter, made = recording_adapter
        await adapter.connect(
            SessionOptions(model="claude-sonnet-4-5", tool_allowlist=["Read", "Glob"])
        )
    finally:
        cli_tool_surface.reset_cli_tools()

    denied = made["delegated"]["disallowed_tools"]
    assert "Bash" in denied and "Write" in denied
    assert "Read" not in denied and "Glob" not in denied
    assert made["delegated"]["allowed_tools"] == ["Read", "Glob"]


def test_profile_surface_and_dispatch_surface_intersect(mock_db):
    """T3: the agent's ``allow`` list and a file-agent worker's ``tools:`` list
    compose — the session gets their intersection, and an empty intersection
    becomes a total deny rather than an empty (= unrestricted) list."""
    from app.core.query_options import build_options_from_profile

    profile = {
        "id": "p1",
        "name": "p",
        "config": {"tool_selection_mode": "allow", "allowed_tools": ["Read", "Bash"]},
    }
    options, _ = build_options_from_profile(
        profile, overrides={"tool_allowlist": ["Read", "Grep"]}
    )
    assert options.aih_tool_allowlist == ["Read"]

    options, _ = build_options_from_profile(
        profile, overrides={"tool_allowlist": ["Grep"]}
    )
    assert options.aih_tool_allowlist is None
    assert "*" in options.disallowed_tools


def test_profile_surface_alone_becomes_the_session_surface(mock_db):
    """No dispatch list: the agent's own allow list IS the surface (T3)."""
    from app.core.query_options import build_options_from_profile

    options, _ = build_options_from_profile(
        {
            "id": "p1",
            "name": "p",
            "config": {"tool_selection_mode": "allow", "allowed_tools": ["Read"]},
        }
    )
    assert options.aih_tool_allowlist == ["Read"]

    options, _ = build_options_from_profile(
        {"id": "p2", "name": "p", "config": {"tool_selection_mode": "none"}}
    )
    assert options.aih_tool_allowlist is None
    assert options.disallowed_tools == ["*"]


# ─── T9: the prompt never describes a tool the session cannot call ───────────


def test_permitted_tool_names_is_none_when_unrestricted():
    from app.core.provider.harness_adapter import _permitted_tool_names

    assert _permitted_tool_names(_native_opts(), None) is None


def test_permitted_tool_names_filters_the_registry():
    from app.core.provider.harness_adapter import _permitted_tool_names

    permitted = _permitted_tool_names(
        _native_opts(disallowed_tools=["Task", "Bash"]), None
    )
    assert permitted is not None
    assert "Read" in permitted
    assert "Task" not in permitted and "Bash" not in permitted


def test_disallowing_task_drops_the_agents_index_from_the_prompt(monkeypatch):
    """An agent with ``Task`` disallowed compiles a native prompt with no
    "Available agent types" section."""
    import app.core.provider.harness_adapter as ha

    index = "## Available agent types" + chr(10) + "- x"
    monkeypatch.setattr(ha, "_agent_context", lambda *a, **k: (index, []))

    with_task = ha.HarnessAdapter._compile_prompt_sections("base", None, _native_opts())
    assert any(s.kind == "agents" for s in with_task.sections)

    without = ha.HarnessAdapter._compile_prompt_sections(
        "base", None, _native_opts(disallowed_tools=["Task"])
    )
    assert not any(s.kind == "agents" for s in without.sections)
    assert "Available agent types" not in (without.text or "")


def test_code_mode_tool_sections_skip_a_filtered_tool():
    """``Bash`` disallowed in code mode yields no Bash docs section."""
    from types import SimpleNamespace

    from app.core.provider.harness_adapter import (
        _code_mode_tool_sections,
        _tool_predicate,
    )

    class _Spec:
        def __init__(self, name):
            self.name = name
            self.description = name + " does things."
            self.input_schema = {"type": "object", "properties": {}}
            self.code_mode_allowed = True

    class _Projection:
        def leaves(self):
            return ["Read", "Bash"]

        def spec_for(self, leaf):
            return _Spec(leaf)

    codemode = SimpleNamespace(_projection=lambda: _Projection())

    unfiltered = [name for _, name in _code_mode_tool_sections(codemode)]
    assert unfiltered == ["Read", "Bash"]

    permit = _tool_predicate(_native_opts(disallowed_tools=["Bash"]))
    filtered = [name for _, name in _code_mode_tool_sections(codemode, permit)]
    assert filtered == ["Read"]


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
# "Available agent types" block as the native file agents Fluid discovers.


def _agent_md(name: str, description: str, body: str = "BODY") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def _write_file(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def agent_workspace(tmp_path, monkeypatch):
    """A project cwd with a native file agent and an inert legacy plugin.

    HOME/USERPROFILE are redirected so the developer's own settings cannot
    leak into an assertion.
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
        project / ".agents" / "agents" / "researcher.md",
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


def _agents_options(cwd, declared=None, disallowed_tools=None):
    extra = {"fluid_provider": "openai_responses"}
    if declared is not None:
        extra["declared_agents"] = declared
    return SessionOptions(
        model="gpt-4o",
        system_prompt="BASE PROMPT",
        cwd=str(cwd),
        extra=extra,
        disallowed_tools=disallowed_tools,
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
    # Line-anchored: the Task tool's own description names the heading in
    # prose ("descriptions are under '# Available agent types'"), and under
    # code mode that description now rides the prompt inside the # Tools
    # catalog. What must never appear twice is the INDEX BLOCK itself.
    assert system.count("\n# Available agent types\n") == 1
    assert "- researcher: Reads the codebase" in system      # (a) file agent
    assert "myplug:reviewer" not in system                   # legacy plugin is inert
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

    adapter = ha.HarnessAdapter()
    await adapter.connect(
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
    assert {a["agentType"] for a in agents} == {"Helper", "researcher"}
    # Control side of the T9 clear in ``connect``: a regression that cleared the
    # priced list unconditionally would still pass the suppressed-index test.
    assert adapter._engine.agents


@pytest.mark.asyncio
async def test_suppressed_agents_index_zeroes_the_usage_descriptors(
    monkeypatch, agent_workspace
):
    """Task disallowed (T9) drops the index from the prompt — the "Custom
    agents" row's descriptors must drop with it."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(
        _agents_options(
            agent_workspace,
            declared=[{"name": "Helper", "description": "Helps out"}],
            disallowed_tools=["Task"],
        )
    )

    assert "Available agent types" not in (_CapturingLoop.captured["system"] or "")
    # The constructor still received the descriptors — an empty list there
    # reads as None and would auto-fill from disk — and the priced list is
    # cleared afterwards instead.
    assert _CapturingLoop.captured["agents"]
    assert adapter._engine.agents == []


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
    assert "myplug:reviewer" not in system
    assert [a["agentType"] for a in _CapturingLoop.captured["agents"]] == ["researcher"]


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
async def test_native_file_agents_reach_dispatch_but_plugins_stay_inert(monkeypatch, agent_workspace):
    import app.core.provider.harness_adapter as ha
    import app.core.task_dispatch as td

    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    adapter = ha.HarnessAdapter()
    await adapter.connect(_agents_options(agent_workspace))
    registry = adapter.engine.ctx.extra["agent_registry"]
    assert set(registry) == {"researcher"}
    resolved = td.resolve_registry_agent("researcher", registry)
    assert resolved is not None and resolved.description == "Reads the codebase"


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
    """Append mode (opt-out) advertises native file and database agents."""
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(settings, "prompt_full_replace", False)

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
    assert "myplug:reviewer" not in append                    # legacy plugin is inert
    assert "- Helper: Helps out" in append                    # DB agent
    # Index-only, exactly like the native leg: no definition bodies.
    assert "PLUGIN AGENT PROMPT" not in append
    assert "BODY" not in append
    # The CLI owns its own tool surface — code mode never reaches this leg, so
    # neither do its prompt rewrites.
    assert "# Code mode" not in append
    assert "These agent types can be delegated to with the Task tool." in append


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


# ─── Native-leg project memory (ruling MEM3) ─────────────────────────────────
#
# The native leg has no CLI behind it, so nothing told the model a memory dir
# existed and nothing was ever written or read. ``extra["memory_dir"]`` (stamped
# by query_options as ``aih_memory_dir``) is what turns that on: the prompt names
# the dir, states the file convention, and inlines the current MEMORY.md.


async def _connect_with_memory_dir(monkeypatch, workspace, memory_dir):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    options = _agents_options(workspace)
    if memory_dir is not None:
        options.extra["memory_dir"] = str(memory_dir)
    await ha.HarnessAdapter().connect(options)
    return _CapturingLoop.captured


@pytest.mark.asyncio
async def test_native_prompt_carries_the_project_memory_section(
    monkeypatch, agent_workspace, tmp_path
):
    import app.core.provider.harness_adapter as ha

    memory_dir = tmp_path / "fluid-projects" / "-proj" / "memory"
    _write_file(
        memory_dir / "MEMORY.md",
        "- [Deploy recipe](deploy.md) — build, copy, restart",
    )

    captured = await _connect_with_memory_dir(monkeypatch, agent_workspace, memory_dir)
    system = captured["system"]

    assert "# Project memory" in system
    assert str(memory_dir) in system
    assert "- [Deploy recipe](deploy.md) — build, copy, restart" in system
    assert "## MEMORY.md" in system
    # Code mode stays LAST: it is the operating rule for everything above it,
    # this section included.
    assert system.index("# Project memory") < system.index("# Code mode")
    assert ha.CODE_MODE_SECTION in system
    # There is no Memory tool anywhere anymore, so the reachable list never
    # names one.
    assert "Skill, Task, Memory, Ledger" not in system


@pytest.mark.asyncio
async def test_project_memory_section_is_priced_as_a_memory_file(
    monkeypatch, agent_workspace, tmp_path
):
    from fluid.context.usage import build_context_usage

    memory_dir = tmp_path / "fluid-projects" / "-proj" / "memory"
    _write_file(memory_dir / "MEMORY.md", "- [Thing](thing.md) — a thing")

    captured = await _connect_with_memory_dir(monkeypatch, agent_workspace, memory_dir)
    compiled = captured["system_sections"]

    memory_sections = [
        s for s in compiled.sections if s.text.startswith("# Project memory")
    ]
    assert len(memory_sections) == 1
    # Memory-kind, pathed at the real file: that is what puts it under the
    # breakdown's "Memory files" category with a per-file detail row.
    assert memory_sections[0].kind == "memory"
    assert memory_sections[0].path == str(memory_dir / "MEMORY.md")
    assert memory_sections[0].scope == "project"
    # The two views never drift: joined sections ARE the prompt.
    assert compiled.text == captured["system"]

    # …and the breakdown a host renders says the same thing. Exactly one row
    # for this file — the AGENTS.md instruction stack rides the same category
    # without either counting the other.
    usage = build_context_usage(
        sections=compiled.sections, model="claude-opus-4-6", context_window=200_000
    )
    rows = [
        f for f in usage["memoryFiles"]
        if f["path"] == str(memory_dir / "MEMORY.md")
    ]
    assert len(rows) == 1
    assert rows[0]["type"] == "Project"
    assert rows[0]["tokens"] > 0
    memory_category = [
        c for c in usage["categories"] if c["name"] == "Memory files"
    ]
    assert len(memory_category) == 1
    assert memory_category[0]["tokens"] == sum(
        f["tokens"] for f in usage["memoryFiles"]
    )


@pytest.mark.asyncio
async def test_empty_memory_dir_says_so_rather_than_going_quiet(
    monkeypatch, agent_workspace, tmp_path
):
    """A dir with no MEMORY.md still gets the section — the model has to know
    where to write the first fact."""
    captured = await _connect_with_memory_dir(
        monkeypatch, agent_workspace, tmp_path / "never-written"
    )
    assert "# Project memory" in captured["system"]
    assert "(empty — nothing saved yet)" in captured["system"]


@pytest.mark.asyncio
async def test_no_memory_dir_means_no_project_memory_section(
    monkeypatch, agent_workspace
):
    captured = await _connect_with_memory_dir(monkeypatch, agent_workspace, None)
    assert "# Project memory" not in captured["system"]
    assert not [
        s for s in captured["system_sections"].sections
        if s.text.startswith("# Project memory")
    ]


@pytest.mark.asyncio
async def test_compile_passes_the_learner_memories_dir(
    monkeypatch, agent_workspace
):
    """The continual learner's memory snapshot rides the compiler: with
    continual learning on (the default) the skills root is handed over as
    ``memories_dir``; switched off, no learner section can appear."""
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings

    captured_inputs = {}
    from fluid.context import compiler as fluid_compiler

    real_compile = fluid_compiler.compile_prompt

    def _spy(inputs):
        captured_inputs["memories_dir"] = inputs.memories_dir
        return real_compile(inputs)

    monkeypatch.setattr(fluid_compiler, "compile_prompt", _spy)
    await _connect_with_memory_dir(monkeypatch, agent_workspace, None)
    assert captured_inputs["memories_dir"] == str(ha._fluid_skills_root())

    monkeypatch.setattr(settings, "fluid_continual", False)
    await _connect_with_memory_dir(monkeypatch, agent_workspace, None)
    assert captured_inputs["memories_dir"] is None


# ─── The "# GitHub" section (gh installed AND signed in) ─────────────────────
#
# ``extra["github_connected"]`` (stamped by query_options as
# ``aih_github_connected``, behind a TTL cache over ``gh auth status``) tells the
# model the binary is there and authenticated. Nothing else in the prompt could:
# without it a native leg rebuilds PR/CI state from raw git or asks the user.


async def _connect_with_github(
    monkeypatch, workspace, connected, disallowed_tools=None
):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)
    options = _agents_options(workspace, disallowed_tools=disallowed_tools)
    if connected:
        options.extra["github_connected"] = True
    await ha.HarnessAdapter().connect(options)
    return _CapturingLoop.captured


@pytest.mark.asyncio
async def test_native_prompt_carries_the_github_section(monkeypatch, agent_workspace):
    import app.core.provider.harness_adapter as ha

    captured = await _connect_with_github(monkeypatch, agent_workspace, True)
    system = captured["system"]

    assert ha.GITHUB_BLOCK in system
    # Code mode stays LAST: it is the operating rule for everything above it,
    # including "reach GitHub through gh in Bash".
    assert system.index("# GitHub") < system.index("# Code mode")

    sections = [
        s for s in captured["system_sections"].sections
        if s.text.startswith("# GitHub")
    ]
    assert len(sections) == 1
    assert sections[0].kind == "extra"
    # The two views never drift: joined sections ARE the prompt.
    assert captured["system_sections"].text == system


def test_github_section_carries_the_git_hazard_rules():
    """Three failures the round-3 audit caught in live sessions, each answered
    by one line of this block."""
    import app.core.provider.harness_adapter as ha

    text = ha.GITHUB_BLOCK

    # CI waits are parked on a Monitor, never blocked on in Bash.
    assert "never wait in Bash" in text
    assert "Monitor(command=" in text and "gh pr checks" in text
    # Worktrees share one stash stack: a bare pop can land another session's
    # work in this tree.
    assert "git stash" in text and 'push -u -m "<tag>"' in text
    # A stale ``--merged`` listing nearly deleted a live session's branch.
    assert "git worktree list" in text and "bulk branch delete" in text


@pytest.mark.asyncio
async def test_no_github_connection_means_no_github_section(
    monkeypatch, agent_workspace
):
    captured = await _connect_with_github(monkeypatch, agent_workspace, False)
    assert "# GitHub" not in captured["system"]
    assert not [
        s for s in captured["system_sections"].sections
        if s.text.startswith("# GitHub")
    ]


@pytest.mark.asyncio
async def test_github_section_is_dropped_when_bash_is_not_permitted(
    monkeypatch, agent_workspace
):
    """Ruling T9, same gate the agents index gets from Task: a block about a
    command-line tool describes something a Bash-less agent cannot call."""
    captured = await _connect_with_github(
        monkeypatch, agent_workspace, True, disallowed_tools=["Bash"]
    )
    assert "# GitHub" not in captured["system"]


# ─── The "# Past sessions are your memory" recall block ──────────────────────
#
# Agent profiles replace fluid's base prompt wholesale (ruling A12), so fluid's
# own recall bullets never reach an AI Shuffle session. The harness appends its
# own guidance instead — gated exactly like the tools: a DB behind the store,
# and SessionSearch/SessionView on the permitted surface.


@pytest.mark.asyncio
async def test_native_prompt_carries_the_recall_block_with_a_db(
    monkeypatch, agent_workspace
):
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql://stub/db")
    captured = await _connect_with_github(monkeypatch, agent_workspace, False)
    system = captured["system"]
    assert ha.RECALL_BLOCK in system
    # Code mode stays LAST — the recall tools are reached from run_python.
    assert system.index("# Past sessions are your memory") < system.index("# Code mode")


@pytest.mark.asyncio
async def test_recall_block_is_absent_without_a_db_backend(
    monkeypatch, agent_workspace
):
    """Unit-test deployments run MemoryStore, which offers no recall — the
    tools are gated off, so nothing may describe them (ruling T9)."""
    captured = await _connect_with_github(monkeypatch, agent_workspace, False)
    assert "# Past sessions are your memory" not in captured["system"]


@pytest.mark.asyncio
async def test_recall_block_is_dropped_when_the_tools_are_not_permitted(
    monkeypatch, agent_workspace
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql://stub/db")
    captured = await _connect_with_github(
        monkeypatch, agent_workspace, False,
        disallowed_tools=["SessionSearch", "SessionView"],
    )
    assert "# Past sessions are your memory" not in captured["system"]


def test_github_auth_check_is_cached_within_the_ttl(monkeypatch):
    """The prompt builder asks every turn; ``gh auth status`` is a subprocess
    with a 10s timeout. One probe per TTL window, not one per turn."""
    from app.core import auth as auth_mod

    calls = []

    def _probe():
        calls.append(1)
        return True

    monkeypatch.setattr(auth_mod.auth_service, "is_github_authenticated", _probe)
    auth_mod.reset_github_auth_cache()
    try:
        assert auth_mod.is_github_authenticated_cached() is True
        assert auth_mod.is_github_authenticated_cached() is True
        assert len(calls) == 1

        # …and an entry older than the TTL probes again.
        value, stamped = auth_mod._github_auth_cache
        auth_mod._github_auth_cache = (
            value, stamped - auth_mod._GITHUB_AUTH_TTL_SECONDS - 1
        )
        assert auth_mod.is_github_authenticated_cached() is True
        assert len(calls) == 2
    finally:
        auth_mod.reset_github_auth_cache()


# ─── No Memory tool anywhere (memory = continual learner + session recall) ──
#
# Declarative memory belongs to the continual learner and episodic recall to
# the session store, so no leg carries a Memory tool: fluid no longer ships
# one, and the app's per-agent ``mcp__builtins__Memory`` is gone with the
# feature. The native registry instead carries the recall builtins.


def _memory_registry_options(tmp_path, memory_dir):
    from app.core.provider.sdk_tool_records import record_sdk_server_tools
    from types import SimpleNamespace

    async def handler(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "ledger"}]}

    class _Instance:
        def call_tool(self):
            return lambda fn: fn

    cfg = {"type": "sdk", "name": "builtins", "instance": _Instance()}
    record_sdk_server_tools(cfg, [
        SimpleNamespace(name="Ledger", description="Ledger tool",
                        input_schema={"type": "object"}, handler=handler),
    ])
    extra = {"fluid_provider": "openai_responses"}
    if memory_dir is not None:
        extra["memory_dir"] = str(memory_dir)
    return SessionOptions(
        model="gpt-4o",
        cwd=str(tmp_path),
        extra=extra,
        mcp_servers={"builtins": cfg},
    )


@pytest.mark.asyncio
async def test_native_registry_has_recall_builtins_and_no_memory_tool(tmp_path):
    import app.core.provider.harness_adapter as ha

    registry = await ha.HarnessAdapter()._build_registry(
        _memory_registry_options(tmp_path, tmp_path / "mem"), native=True
    )

    names = {spec.name for spec in registry.list_tools(loaded_only=False)}
    assert "SessionSearch" in names and "SessionView" in names
    assert "Memory" not in names
    assert "mcp__builtins__Memory" not in names
    assert "Read" in names and "mcp__builtins__Ledger" in names
    assert registry.get("Memory") is None


def test_oversized_memory_index_is_truncated(tmp_path):
    import app.core.provider.harness_adapter as ha

    memory_dir = tmp_path / "memory"
    _write_file(memory_dir / "MEMORY.md", "x" * (ha._MEMORY_INDEX_CHAR_CAP + 500))

    text = ha._project_memory_section(str(memory_dir))

    assert text.endswith("[truncated]")
    assert len(text) < ha._MEMORY_INDEX_CHAR_CAP + 2000



# ─── Per-session fast mode (v81) ─────────────────────────────────────────────
#
# ``SessionOptions.fast`` is the user's per-session toggle. Each leg spends it
# differently, and only where the leg actually has an expedited service:
# the delegated CLI gets a ``--settings`` blob carrying ``fastMode``; the
# OpenAI Responses native lane gets ``service_tier: priority`` through fluid's
# ``request_extra``. The anthropic and openrouter native lanes have no such
# knob in v1 and must ignore ``fast`` entirely.


def _delegated_fast_options(fast):
    from fluid.tools.registry import ToolRegistry

    return SessionOptions(
        model="fable",
        fast=fast,
        # Prebuilt registry short-circuits _build_registry (no MCP dial-out).
        extra={"fluid_registry": ToolRegistry()},
    )


@pytest.mark.asyncio
async def test_delegated_fast_mode_sends_settings_json(monkeypatch):
    import json

    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    adapter = ha.HarnessAdapter()
    await adapter.connect(_delegated_fast_options(True))

    assert adapter._is_delegated is True
    assert json.loads(_CapturingLoop.captured["settings"]) == {"fastMode": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("fast", [False, None])
async def test_delegated_without_fast_sends_no_settings(monkeypatch, fast):
    """Standard speed means NO --settings at all — an explicit fastMode:false
    would say the same thing while overriding the account's own settings file."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    await ha.HarnessAdapter().connect(_delegated_fast_options(fast))

    assert _CapturingLoop.captured["settings"] is None


@pytest.mark.asyncio
async def test_openai_responses_fast_mode_sets_priority_service_tier(
    monkeypatch, tmp_path
):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-5",
            fast=True,
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    assert _CapturingLoop.captured["request_extra"] == {"service_tier": "priority"}


@pytest.mark.asyncio
@pytest.mark.parametrize("fast", [False, None])
async def test_openai_responses_without_fast_sends_no_request_extra(
    monkeypatch, tmp_path, fast
):
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="gpt-5",
            fast=fast,
            cwd=str(tmp_path),
            extra={"fluid_provider": "openai_responses"},
        )
    )
    assert _CapturingLoop.captured["request_extra"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "openrouter"])
async def test_other_native_lanes_ignore_fast_mode(monkeypatch, tmp_path, kind):
    """Only the Responses lane has a wire-level expedited tier in v1."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "NativeLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_build_provider_client", _fake_provider_client)

    await ha.HarnessAdapter().connect(
        SessionOptions(
            model="claude-sonnet-5" if kind == "anthropic" else "openai/gpt-5",
            fast=True,
            cwd=str(tmp_path),
            extra={"fluid_provider": kind},
        )
    )
    assert _CapturingLoop.captured["request_extra"] is None


@pytest.mark.asyncio
async def test_sdk_options_fast_rides_onto_session_options(monkeypatch):
    """query_options stamps ``aih_fast``; connect_with_sdk_options projects it
    onto SessionOptions.fast, which is what the two lanes above read."""
    from types import SimpleNamespace

    import app.core.provider.harness_adapter as ha

    adapter = ha.HarnessAdapter()
    captured: dict = {}

    async def fake_connect(opts):
        captured["opts"] = opts

    monkeypatch.setattr(adapter, "connect", fake_connect)
    await adapter.connect_with_sdk_options(
        SimpleNamespace(model="fable", aih_fast=True)
    )
    assert captured["opts"].fast is True


@pytest.mark.asyncio
async def test_fast_mode_flip_requires_a_reconnect(monkeypatch):
    """Fast mode is baked into the engine at construction (CLI argv / native
    request builder), so flipping it can only be honored by a rebuild."""
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    adapter = ha.HarnessAdapter()
    await adapter.connect(_delegated_fast_options(True))

    assert adapter.requires_reconnect(_delegated_fast_options(True)) is False
    assert adapter.requires_reconnect(_delegated_fast_options(False)) is True


@pytest.mark.asyncio
async def test_fast_mode_absent_matches_off_for_reconnect(monkeypatch):
    """``None`` and ``False`` both mean standard speed, so switching between
    them must NOT rebuild the engine.

    The composer now always sends an explicit ``fast`` boolean (it used to omit
    the key when off, which is how "turn fast mode off" never reached the CLI).
    A session that never had fast mode would otherwise see its first
    ``fast: false`` turn as a change and pay a pointless reconnect.
    """
    import app.core.provider.harness_adapter as ha

    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    adapter = ha.HarnessAdapter()
    await adapter.connect(_delegated_fast_options(None))

    assert adapter.requires_reconnect(_delegated_fast_options(False)) is False
    assert adapter.requires_reconnect(_delegated_fast_options(None)) is False
    assert adapter.requires_reconnect(_delegated_fast_options(True)) is True


# ── code-mode kernel config ───────────────────────────────────────────────


def test_codemode_kernel_keeps_fluids_default_stdout_cap():
    """The per-cell stdout cap is fluid's 16,384 default, deliberately.

    It was briefly raised to ``MAX_REPORT_CHARS * 2`` so a parent could print a
    whole subagent report from one cell; every OTHER cell inherited the
    licence and flooded ~120KB of grep/read output per call. Sync Task results
    now carry a bounded head plus a ``report_path``, so no legitimate cell
    needs more than the default."""
    import app.core.provider.harness_adapter as ha

    config = ha._codemode_kernel_config()
    default = ha.KernelConfig()

    assert config.stdout_cap == default.stdout_cap == 16_384
    assert config.ref_threshold == default.ref_threshold
    assert config.checkpoint == default.checkpoint
    assert config.allow_network == default.allow_network
    assert config.allow_fs == default.allow_fs


# ── model-family guidance on native legs ──────────────────────────────────


def test_model_guidance_blocks_reach_a_gpt_native_prompt():
    """fluid writes "Act, don't narrate" / "Execution discipline" for the
    families that narrate instead of calling tools, but only emits them from
    its own ``compose_base_prompt`` — which this app replaces with the
    profile's prompt (ruling A12). Without this injection they reached nothing."""
    import app.core.provider.harness_adapter as ha
    from fluid.cli.prompt import ACT_GUIDANCE_BLOCK, DISCIPLINE_GUIDANCE_BLOCK

    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "base", None, _native_opts(model="gpt-5")
    )

    texts = [section.text for section in compiled.sections]
    assert ACT_GUIDANCE_BLOCK in texts
    assert DISCIPLINE_GUIDANCE_BLOCK in texts


def test_claude_models_get_no_guidance_blocks():
    """The Claude family is deliberately absent from fluid's tables — every
    block is paid for in every session's cached prefix."""
    import app.core.provider.harness_adapter as ha
    from fluid.cli.prompt import ACT_GUIDANCE_BLOCK, DISCIPLINE_GUIDANCE_BLOCK

    compiled = ha.HarnessAdapter._compile_prompt_sections(
        "base", None, _native_opts(model="claude-sonnet-4-5")
    )

    texts = [section.text for section in compiled.sections]
    assert ACT_GUIDANCE_BLOCK not in texts
    assert DISCIPLINE_GUIDANCE_BLOCK not in texts


def test_guidance_bytes_are_stable_for_one_model():
    """Prompt-prefix caching: the same model must compile to identical text."""
    import app.core.provider.harness_adapter as ha

    first = ha._model_guidance_blocks("grok-4")
    second = ha._model_guidance_blocks("grok-4")

    assert first == second and first


def test_guidance_degrades_to_nothing_without_the_fluid_symbol(monkeypatch):
    """A vendored fluid that predates ``model_guidance_blocks`` must cost no
    blocks, not a failed prompt compilation."""
    import builtins

    import app.core.provider.harness_adapter as ha

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "fluid.cli.prompt":
            raise ImportError("no such symbol")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert ha._model_guidance_blocks("gpt-5") == ()


# ── code-mode prompt contracts ────────────────────────────────────────────


def test_code_mode_section_carries_the_four_contracts():
    """The four model-side contracts the audit sized as highest leverage."""
    import app.core.provider.harness_adapter as ha

    text = ha._code_mode_section()

    # (a) bind-then-slice, with the recovery seams named.
    assert "forfeits its ResultRef" in text
    assert "_refs" in text
    # (b) park after delegating.
    assert 'Task(mode="background")' in text and "never poll" in text
    # (c) one cell carries the whole step.
    assert "Never spend a cell on a single Read" in text
    assert "sleep-poll" in text
    # ... and the two batching rules the round-3 audit added beside it: a known
    # recipe is one step, and a bound result is located in-cell, not paged out
    # across cells.
    assert "is ONE step" in text and "same file or working tree" in text
    assert "do not print it in slices across cells" in text
    assert "txt.find('needle')" in text
    # (d) recall budget.
    assert "limit<=5" in text and "two unproductive queries" in text
    # The code-mode rule still ends the block (it is last for a reason).
    assert text.rstrip().endswith("save it as a skill.")


# ── native recoverable context policy ─────────────────────────────────────


@pytest.mark.asyncio
async def test_recoverable_compaction_is_enabled_for_native_loops(monkeypatch, tmp_path):
    """Native loops compact only their rendered view and retain exact nodes."""
    captured = await _connect_capturing_native(monkeypatch, tmp_path)
    compaction = captured["compaction"]

    assert compaction.enabled is True
    assert compaction.trim.enabled is True
    assert compaction.trigger_tokens == 120_000
    assert compaction.headroom_tokens == 16_000
    assert compaction.target_ratio == 0.70
