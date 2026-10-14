"""Checkpoint tier: producer, persisted text, guards, failure and projection."""
import asyncio
from dataclasses import replace

import pytest

from fluid.context.cache_signal import CacheSignal
from fluid.context.checkpoint_summary import CHECKPOINT_SYSTEM, build_checkpoint
from fluid.context.compaction import CompactionConfig, Compactor
from fluid.engine.native import NativeLoop
from fluid.messages import Message, Text
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import SessionNode
from fluid.store.jsonl import JsonlStore


def nodes(count=12):
    out = []
    for i in range(count):
        for role in ("user", "assistant"):
            out.append(SessionNode(node_id=f"node_{i}_{role}", parent_id=out[-1].node_id if out else None,
                                   message=Message(role=role, content=[Text(text=f"turn {i}: " + "word " * 2000)])))
    return out


def config(**kwargs):
    return replace(CompactionConfig(summary_enabled=True, keep_recent_turns=1,
                                   recency_tail_tokens=0, max_resident_span_digests=1000), **kwargs)


def compacted():
    path = nodes()
    c = Compactor(config=config())
    c._digest_until(path, overhead_tokens=0, target=0)
    return c, path


class Provider:
    name = "anthropic"
    caps = ProviderCaps()

    def estimate_cost(self, model, usage):
        return 0.125

    def __init__(self, text="checkpoint", stop="end_turn", error=False):
        self.requests = []
        self.text, self.stop, self.error = text, stop, error

    async def stream(self, request):
        self.requests.append(request)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 100, "output_tokens": 1}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": self.text})
        yield StreamChunk("block_stop", {"index": 0})
        if self.error:
            yield StreamChunk("error", {"message": "synthetic failure"})
        yield StreamChunk("usage", {"usage": {"output_tokens": 7}, "stop_reason": self.stop})
        yield StreamChunk("message_stop", {"stop_reason": self.stop})


async def test_side_call_prompt_usage_and_routing():
    provider = Provider()
    billed = []
    async def account(raw):
        billed.append(raw)
    assert await build_checkpoint(provider, model="fixture", text="history", max_tokens=4000,
                                  account=account, extra={"prompt_cache_key": "checkpoint:fixture"}) == "checkpoint"
    request = provider.requests[0]
    assert request.tools == [] and request.max_tokens == 4000
    assert request.system == CHECKPOINT_SYSTEM
    assert request.extra["prompt_cache_key"] == "checkpoint:fixture"
    assert billed == [{"input_tokens": 100, "output_tokens": 7}]


@pytest.mark.parametrize("text,stop,error", [("", "end_turn", False), ("partial", "max_tokens", False),
                                             ("partial", "length", False), ("text", "end_turn", True)])
async def test_failed_completions_still_account(text, stop, error):
    billed = []
    async def account(raw):
        billed.append(raw)
    with pytest.raises(ValueError):
        await build_checkpoint(Provider(text, stop, error), model="fixture", text="history",
                               max_tokens=4000, account=account)
    assert billed[0]["output_tokens"] == 7


def test_summary_retirement_rehydrate_rolling_and_fork():
    c, path = compacted()
    region = c.summary_region(path)
    assert region[:2] == (path[0].node_id, path[-3].node_id)
    p = c.apply_summary(path, (*region[:2], "first checkpoint"))
    assert p.tier == 4 and p.reason == "summary" and p.after_tokens < p.before_tokens
    assert list(c._digested.values()) == ["summary"]
    view = c.view(path)
    assert "HistoryExpand" in view[0].text() and "first checkpoint" in view[0].text()
    fresh = Compactor(config=replace(c.config, summary_enabled=False))
    fresh.rehydrate([p])
    assert fresh.view(path) == view
    assert fresh.view(path[:4]) == Compactor(config=fresh.config).view(path[:4])
    # Rolling checkpoint must include the previous text and survive replay order.
    longer = nodes(16)
    c._digest_until(longer, overhead_tokens=0, target=0)
    rolling = c.summary_region(longer)
    assert "first checkpoint" in rolling[2]
    p2 = c.apply_summary(longer, (*rolling[:2], "second checkpoint"))
    fresh.rehydrate([p2])
    assert fresh.view(longer) == c.view(longer)
    assert "second checkpoint" in fresh.view(longer)[0].text()
    assert fresh.view(path)[0] == view[0]  # successor is off path
    assert fresh.view(path[:4])[0] == path[0].message


def test_region_tail_due_cap_and_no_recall():
    c, path = compacted()
    assert c.summary_region(path, due_only=True) is None  # unknown window
    assert c.summary_region(path, context_window=1_000_000, due_only=True) is None
    assert c.summary_region(path, context_window=1000, due_only=True)
    c.config = replace(c.config, summary_input_cap_tokens=100)
    assert len(c.summary_region(path)[2]) <= 400
    c.config = replace(c.config, recency_tail_tokens=1_000_000, recency_tail_window_ratio=1)
    assert c.summary_region(path, context_window=1_000_000) is None
    c.config = replace(c.config, recency_tail_tokens=0, recall_tools=False)
    region = c.summary_region(path)
    c.apply_summary(path, (*region[:2], "checkpoint"))
    assert "HistoryExpand" not in c.view(path)[0].text()


def test_region_includes_live_tool_gaps_and_delivery_constraints():
    from fluid.messages.content import ToolUseBlock, ToolResultBlock, SteerDelivery
    c, path = compacted()
    # A live hole inside the checkpoint region must not silently lose calls/results.
    c._retire((path[4].node_id, path[5].node_id))
    path[4].message.content = [ToolUseBlock(id="exact-id", name="Read", input={"path": "/exact/path"})]
    path[5].message.content = [ToolResultBlock(tool_use_id="exact-id", content=[Text("exact output")],
        delivered_steers=[SteerDelivery(text="preserve this constraint")])]
    text = c.summary_region(path)[2]
    assert "/exact/path" in text and "exact output" in text and "preserve this constraint" in text
    c.config = replace(c.config, summary_input_cap_tokens=1)
    assert len(c.summary_region(path)[2]) <= 4


def test_projection_is_read_only_and_manual_below_warning_threshold():
    path = nodes(70)
    c = Compactor(config=config(user_turn_text_cap=8000, assistant_text_cap=4000))
    before = c.estimate_view_tokens(path, 20_000)
    assert before > 150_000
    view = c.view(path)
    projected = c.projected_after_compact(path, overhead_tokens=20_000, context_window=400_000)
    assert projected < 150_000 and c.view(path) == view and not c._digested
    c.compact_now(path, overhead_tokens=20_000, context_window=400_000)
    region = c.summary_region(path, context_window=400_000)
    assert region
    promo = c.apply_summary(path, (*region[:2], "s" * 16000), overhead_tokens=20_000)
    assert promo and promo.after_tokens < 150_000


@pytest.mark.parametrize("boundary,gap,size,batch,applied", [
    (False, 400, 1000, False, False), (True, 20, 1000, False, False),
    (True, 400, 1000, False, True), (False, 20, 150000, False, True),
    (False, 20, 1000, True, True),
])
async def test_pending_apply_boundaries(tmp_path, boundary, gap, size, batch, applied):
    loop = NativeLoop(provider=Provider(), store=JsonlStore(tmp_path), model="fixture", compaction=config())
    loop._compactor, path = compacted()
    region = loop._compactor.summary_region(path)
    loop._pending_summary = (*region[:2], "checkpoint")
    promo = loop._apply_pending_checkpoint(path, cache=CacheSignal(gap_s=gap), turn_boundary=boundary,
                                            measured=size, overhead=0, batch_due=batch)
    assert bool(promo) is applied
    await loop.aclose()




class LoopProvider(Provider):
    def __init__(self, *, fail=False, gate=None):
        super().__init__()
        self.fail, self.gate = fail, gate
        self.started = asyncio.Event()

    async def stream(self, request):
        if request.system == CHECKPOINT_SYSTEM:
            self.started.set()
            if self.gate:
                await self.gate.wait()
            response = Provider(text="stable checkpoint text", error=self.fail)
        else:
            response = Provider(text="done " * 1000)
        self.requests.append(request)
        async for chunk in response.stream(request):
            yield chunk


async def turn(loop, content):
    from fluid.events import ErrorEvent, TurnComplete, Usage, UserTurn
    before = len(loop.bus.log(loop.session_id)) if loop._session else 0
    await loop.submit(UserTurn(content=content))
    await asyncio.wait_for(loop.wait_turn(), 5)
    events = loop.bus.log(loop.session_id)[before:]
    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert any(isinstance(e, TurnComplete) and e.stop_reason == "end_turn" for e in events)
    if content != "/compact":
        assert any(isinstance(e, Usage) and not e.raw.get("auxiliary_source") for e in events)
    return events


@pytest.mark.parametrize("fail", [False, True])
async def test_manual_build_persist_rehydrate_and_failure_fallback(tmp_path, fail):
    from fluid.events import ContextCompaction, Usage
    provider = LoopProvider(fail=fail)
    cfg = config(trigger_tokens=10_000_000)
    loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="fixture", compaction=cfg)
    for i in range(8):
        await turn(loop, f"request {i} " + "word " * 2000)
    sid = loop.session_id
    original = await loop.store.load_path(sid)
    from fluid.events import TurnComplete
    manual = await turn(loop, "/compact")
    terminal = next(e for e in manual if isinstance(e, TurnComplete))
    assert terminal.cost_usd == 0.125
    assert terminal.usage["input_tokens"] == 100
    assert terminal.usage["output_tokens"] == 7
    assert await loop.store.load_path(sid) == original
    events = loop.bus.log(sid)
    checkpoints = [e for e in events if isinstance(e, ContextCompaction) and e.tier == 4 and e.reason == "summary"]
    assert len(checkpoints) == (0 if fail else 1)
    assert any(isinstance(e, ContextCompaction) and e.tier < 4 for e in events)
    [usage] = [e for e in events if isinstance(e, Usage) and e.raw.get("auxiliary_source")]
    assert usage.raw["auxiliary_source"] == "checkpoint_summary"
    assert usage.raw["cost_usd"] == 0.125
    main_usage = [e for e in events if isinstance(e, Usage) and not e.raw.get("auxiliary_source")]
    assert main_usage
    assert all(isinstance(e.context_usage["compaction"]["projectedAfterCompactTokens"], int)
               for e in main_usage)
    assert usage.output_tokens == 7 and not usage.node_id and usage.context_usage is None
    view = loop._compactor.view(original)
    await loop.aclose()
    fresh = NativeLoop(provider=Provider(), store=JsonlStore(tmp_path), model="fixture", session_id=sid,
                       compaction=replace(cfg, summary_enabled=False))
    await fresh._rehydrate_compaction(sid)
    assert fresh._compactor.view(original) == view
    await fresh.aclose()


async def test_background_single_flight_does_not_block_turn_and_cancel_accounts(tmp_path):
    from fluid.events import Usage
    gate = asyncio.Event()
    provider = LoopProvider(gate=gate)
    loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="fixture",
                      context_window=40_000, compaction=config(trigger_tokens=10_000_000))
    await turn(loop, "start")
    # Seed through the store and mechanical producer, not summary consumer state.
    path = nodes(15)
    for node in path:
        await loop.store.append(loop.session_id, node)
    loop._compactor._digest_until(path, overhead_tokens=0, target=0)
    # Let the no-op task from the first turn settle before requesting a build.
    if loop._summary_task:
        await loop._summary_task
    loop._schedule_checkpoint(loop.session_id, "fixture-turn")
    await asyncio.wait_for(provider.started.wait(), 3)
    task = loop._summary_task
    loop._schedule_checkpoint(loop.session_id, "duplicate")
    assert loop._summary_task is task and not task.done()
    # Main-model turn finishes while the side completion remains gated.
    await turn(loop, "continue")
    assert not task.done()
    gate.set()
    await asyncio.wait_for(task, 3)
    assert loop._pending_summary is not None
    assert sum(e.raw.get("auxiliary_source") == "checkpoint_summary"
               for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)) == 1
    await loop.aclose()


async def test_cancel_side_call_accounts_partial_usage():
    started = asyncio.Event()
    class Gated(Provider):
        async def stream(self, request):
            yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
            started.set()
            await asyncio.Event().wait()
    billed = []
    async def account(raw):
        billed.append(raw)
    task = asyncio.create_task(build_checkpoint(Gated(), model="fixture", text="history",
                                                max_tokens=4000, account=account))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert billed == [{"input_tokens": 100}]


def test_prompt_budget_and_duplicate_paragraphs():
    # Fixed fixture + small allowance: edits must consciously re-budget the side prompt.
    assert len(CHECKPOINT_SYSTEM) <= 700
    paragraphs = [p.strip() for p in CHECKPOINT_SYSTEM.split("\n\n") if p.strip()]
    assert len(paragraphs) == len(set(paragraphs))
    for section in ("User requests and constraints", "Decisions", "Exact identifiers", "Completed work",
                    "Pending work and next step", "Errors and how they were resolved", "Open questions"):
        assert CHECKPOINT_SYSTEM.count("## " + section + "\n") == 1

async def start_checkpoint(loop, provider):
    await turn(loop, "start")
    if loop._summary_task:
        await loop._summary_task
    path = nodes(15)
    for node in path:
        await loop.store.append(loop.session_id, node)
    loop._compactor._digest_until(path, overhead_tokens=0, target=0)
    loop._schedule_checkpoint(loop.session_id, "checkpoint-origin")
    await asyncio.wait_for(provider.started.wait(), 3)
    return loop._summary_task


@pytest.mark.parametrize("during_turn", [False, True])
async def test_background_usage_has_one_owner(tmp_path, during_turn):
    from fluid.events import TurnComplete, UserTurn
    gate, main_gate, main_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Concurrent(LoopProvider):
        async def stream(self, request):
            if request.system != CHECKPOINT_SYSTEM and main_started.is_set():
                await main_gate.wait()
            async for chunk in super().stream(request):
                yield chunk

    provider = Concurrent(gate=gate)
    billed = []
    async def account(event):
        billed.append(event)
    loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="fixture",
                      context_window=40_000, compaction=config(trigger_tokens=10_000_000))
    loop._background_usage_sink = account
    try:
        task = await start_checkpoint(loop, provider)
        if during_turn:
            main_started.set()
            await loop.submit(UserTurn(content="concurrent turn"))
        gate.set()
        await asyncio.wait_for(task, 3)
        assert len(billed) == 1
        assert billed[0].raw["cost_usd"] == 0.125
        assert billed[0].input_tokens == 100
        assert billed[0].output_tokens == 7
        if during_turn:
            assert not loop._turn_task.done()
            main_gate.set()
            await asyncio.wait_for(loop.wait_turn(), 5)
        else:
            await turn(loop, "next turn")
        terminal = [e for e in loop.bus.log(loop.session_id) if isinstance(e, TurnComplete)][-1]
        assert terminal.cost_usd == 0.125  # main call only, never the checkpoint again
        assert terminal.usage["input_tokens"] == 100
        assert len(billed) == 1
    finally:
        await loop.aclose()


async def test_idle_interrupt_cancels_checkpoint_and_drops_pending(tmp_path):
    from fluid.events import Interrupt
    provider = LoopProvider(gate=asyncio.Event())
    loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="fixture",
                      context_window=40_000, compaction=config(trigger_tokens=10_000_000))
    try:
        task = await start_checkpoint(loop, provider)
        loop._pending_summary = ("start", "end", "pending")
        await loop.submit(Interrupt())
        assert task.cancelled()
        assert loop._pending_summary is None
        loop._schedule_checkpoint(loop.session_id, "interrupted")
        assert loop._summary_task is None or loop._summary_task.done()
    finally:
        await loop.aclose()


async def test_cancel_during_accounting_waits_for_booking():
    started, release = asyncio.Event(), asyncio.Event()
    billed = []
    async def account(raw):
        started.set()
        await release.wait()
        billed.append(raw)
    task = asyncio.create_task(build_checkpoint(
        Provider(), model="fixture", text="history", max_tokens=4000, account=account,
    ))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert billed == [{"input_tokens": 100, "output_tokens": 7}]


@pytest.mark.parametrize("manual", [False, True])
async def test_interrupted_turn_does_not_start_another_checkpoint(tmp_path, manual):
    from fluid.events import Interrupt, Interrupted, UserTurn
    started = asyncio.Event()
    class Waiting(Provider):
        async def stream(self, request):
            yield StreamChunk("message_start", {"usage": {"input_tokens": 100}})
            started.set()
            await asyncio.Event().wait()

    loop = NativeLoop(provider=Waiting(), store=JsonlStore(tmp_path), model="fixture",
                      context_window=40_000, compaction=config(trigger_tokens=10_000_000))
    await loop._ensure_session()
    path = nodes(15)
    for node in path:
        await loop.store.append(loop.session_id, node)
    loop._compactor._digest_until(path, overhead_tokens=0, target=0)
    try:
        await loop.submit(UserTurn(content="/compact" if manual else "continue"))
        await asyncio.wait_for(started.wait(), 3)
        await loop.submit(Interrupt())
        await asyncio.wait_for(loop.wait_turn(), 3)
        assert loop._summary_task is None
        assert loop._pending_summary is None
        terminal = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Interrupted)][-1]
        assert terminal.usage["input_tokens"] == 100
        assert terminal.cost_usd == 0.125
    finally:
        await loop.aclose()
