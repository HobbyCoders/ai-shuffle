"""Issue #181: native target, actual request sizes, replay and overhead floors."""
import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from fluid.context.checkpoint_summary import CHECKPOINT_SYSTEM
from fluid.context.compaction import CompactionConfig, Compactor, MANUAL_COMPACT_REASON
from fluid.context.trim import estimate_tokens_of_messages, estimate_tokens_of_text
from fluid.engine.native import NativeLoop
from fluid.events import ContextCompacting, ContextCompaction, ErrorEvent, Interrupt, UserTurn
from fluid.events.model import event_from_dict, event_to_dict
from fluid.messages import Message, Text, ToolUseBlock, ToolResultBlock
from fluid.messages.content import Image
from fluid.store.base import SessionNode
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.jsonl import JsonlStore

SPEC = importlib.util.spec_from_file_location(
    "compaction_benchmark", Path(__file__).resolve().parents[3] / "scripts/benchmark_compaction_target.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize("window", [400_000, 256_000])
@pytest.mark.parametrize("mode", ["manual", "automatic"])
def test_long_fixture_size_budget(window, mode):
    result = benchmark.measure(window, mode)
    assert result["after"] <= window * 0.15
    # Fixture/allowance guard: do not let prefix growth or extra summary calls
    # silently consume the win. Baseline provenance is recorded alongside it.
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/compaction_target_budget.json").read_text())
    budget = fixture["after_allowance"][str(window)][mode]
    assert result["after"] <= budget
    assert result["summary_calls"] <= (1 if mode == "manual" else 0)
    assert result["static_cacheable"] == 12000


@pytest.mark.parametrize("window", [400_000, 256_000, 32_000])
@pytest.mark.parametrize("manual", [False, True])
def test_no_immediate_retrigger_even_when_overhead_exceeds_target(window, manual):
    path = benchmark.history(max(4, window // 16000))
    c = Compactor()
    if manual:
        promotions = c.compact_now(path, context_window=window, overhead_tokens=12000)
    else:
        _, promotions = c.apply(path, context_window=window, overhead_tokens=12000)
    assert promotions
    view = c.view(path)
    for boundary in (False, True, False):
        rendered, again = c.apply(path, context_window=window, overhead_tokens=12000,
                                  turn_boundary=boundary)
        assert again == [] and rendered == view


class MeasuredProvider:
    """Offline provider records the complete request, never calls a service."""
    name = "fixture"
    caps = ProviderCaps()

    def __init__(self):
        self.requests = []
        self.summary_calls = 0

    async def stream(self, request):
        self.requests.append(request)
        if request.system == CHECKPOINT_SYSTEM:
            self.summary_calls += 1
            text = "checkpoint work status " * 360
        else:
            text = "done"
        tokens = estimate_tokens_of_messages(request.messages) + estimate_tokens_of_text(request.system or "")
        yield StreamChunk("message_start", {"usage": {"input_tokens": tokens}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": text})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {"stop_reason": "end_turn"})


async def seeded_loop(tmp_path, window, *, residue=False, overhead=12000, provider=None):
    provider = provider or MeasuredProvider()
    cfg = CompactionConfig(summary_enabled=True)
    if residue:
        cfg = replace(cfg, epoch_fold_spans=1, user_turn_text_cap=12000)
    loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="gpt-6-astra",
                      system="s" * (overhead * 4), context_window=window, compaction=cfg)
    await loop._ensure_session()
    path = benchmark.history(max(4, window // 16000))
    if residue:
        # A long user-intent pin in each tiny epoch leaves residue above 15%,
        # so only the real checkpoint producer can finish this batch.
        for node in path[:-1]:
            if node.message.role == "user" and isinstance(node.message.content[0], Text):
                node.message.content = [Text("work intent " * 1000)]
    for node in path:
        await loop.store.append(loop.session_id, node)
    return loop, provider, path


@pytest.mark.parametrize("window", [400_000, 256_000])
@pytest.mark.parametrize("manual", [True, False])
async def test_native_manual_and_automatic_requests_use_selected_limit(tmp_path, window, manual):
    loop, provider, original = await seeded_loop(tmp_path, window)
    try:
        await loop.submit(UserTurn(content="/compact" if manual else "continue"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        events = loop.bus.log(loop.session_id)
        assert not [e for e in events if isinstance(e, ErrorEvent)]
        boundaries = [e for e in events if isinstance(e, ContextCompaction) and e.batch_final]
        assert boundaries
        card = boundaries[-1]
        assert card.context_window == window  # NOT the 1M model maximum
        assert 0 < card.after_tokens <= window * 0.15
        path = await loop.store.load_path(loop.session_id)
        # Measure the produced view, not a stubbed target or a seeded consumer.
        assert loop._compactor.estimate_view_tokens(path, loop._context_overhead_tokens()) <= window * 0.15
        if manual:
            assert card.reason == MANUAL_COMPACT_REASON
            assert card.after_tokens == loop._compactor.estimate_view_tokens(path, loop._context_overhead_tokens())
        else:
            request = next(r for r in provider.requests if r.system != CHECKPOINT_SYSTEM)
            assert estimate_tokens_of_messages(request.messages) + 12000 <= window * 0.15
        assert path[0].node_id == original[0].node_id
    finally:
        await loop.aclose()


@pytest.mark.parametrize("manual", [False, True])
async def test_checkpoint_producer_reaches_target_when_epoch_residue_cannot(tmp_path, manual):
    loop, provider, _ = await seeded_loop(tmp_path, 400000, residue=True)
    try:
        await loop.submit(UserTurn(content="/compact" if manual else "continue"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert provider.summary_calls == 1
        events = loop.bus.log(loop.session_id)
        assert not [e for e in events if isinstance(e, ErrorEvent)]
        assert any(isinstance(e, ContextCompaction) and e.tier == 4 for e in events)
        assert loop._compactor.estimate_view_tokens(await loop.store.load_path(loop.session_id), 12000) <= 60000
    finally:
        await loop.aclose()


async def test_unreachable_overhead_reports_actual_outcome_and_preserves_inflight(tmp_path):
    loop, _, original = await seeded_loop(tmp_path, 32000)
    try:
        await loop.submit(UserTurn(content="/compact"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        [card] = [e for e in loop.bus.log(loop.session_id)
                  if isinstance(e, ContextCompaction) and e.reason == MANUAL_COMPACT_REASON]
        path = await loop.store.load_path(loop.session_id)
        actual = loop._compactor.estimate_view_tokens(path, 12000)
        assert 4800 < 12000 < actual < 14000
        assert card.after_tokens == actual and card.after_tokens != 4800
        assert loop._compactor.view(path)[-1] == original[-1].message
    finally:
        await loop.aclose()


def test_epoch_pin_budget_is_frozen_across_event_roundtrip_and_limit_change():
    path = benchmark.history(40)
    c = Compactor()
    _, promotions = c.apply(path, overhead_tokens=12000, context_window=256000)
    assert any(p.tier == 3 for p in promotions)
    assert all(p.intent_pin_tokens == 1280 for p in promotions)
    view = c.view(path)
    events = [event_from_dict(event_to_dict(ContextCompaction(
        session_id="fixture", tier=p.tier, reason=p.reason, spans=[list(s) for s in p.spans],
        trimmed=[list(t) for t in p.trimmed], intent_pin_tokens=p.intent_pin_tokens,
        node_id=path[-1].node_id, render_version=p.render_version))) for p in promotions]
    from fluid.context.compaction import Promotion
    fresh = Compactor()
    fresh.rehydrate(Promotion(tier=e.tier, spans=tuple(map(tuple, e.spans)),
        trimmed=tuple(map(tuple, e.trimmed)), intent_pin_tokens=e.intent_pin_tokens,
        path_end=e.node_id, render_version=e.render_version) for e in events)
    fresh.apply(path, context_window=1000000, overhead_tokens=12000)
    assert fresh.view(path) == view


def test_rolling_checkpoint_input_does_not_duplicate_summary_paragraphs():
    c = Compactor(config=CompactionConfig(summary_enabled=True))
    path = benchmark.history(40)
    c.compact_now(path, context_window=256000)
    region = c.summary_region(path, context_window=256000, for_target=True)
    paragraph = "Distinct completed-work paragraph; preserve its exact identifiers."
    assert c.apply_summary(path, (*region[:2], paragraph))
    longer = benchmark.history(80)
    c.compact_now(longer, context_window=256000)
    next_region = c.summary_region(longer, context_window=256000, for_target=True)
    assert next_region is not None
    assert next_region[2].count(paragraph) == 1
    assert next_region[2].count("[checkpoint summary of") == 1


async def test_image_cap_below_pressure_never_awaits_checkpoint(tmp_path):
    """A prior mechanical batch plus screenshots is not a new size batch."""
    window = 400_000
    loop, provider, _ = await seeded_loop(tmp_path, window)
    # Build the earlier mechanical batch through the real producer, then grow
    # the session below the 45% floor, as in the review reproduction.
    path = benchmark.history(25)[:-1]
    loop._compactor.apply(path + benchmark.history(1)[-1:],
                          context_window=window, overhead_tokens=12000)
    assert not loop._compactor._summaries

    def add(role, blocks):
        path.append(SessionNode(node_id=f"shot_node_{len(path):06d}",
            parent_id=path[-1].node_id, message=Message(role=role, content=blocks)))

    # seeded_loop already stored the prefix; append only the new suffix.
    start = len(path)
    add("user", [Text(text="drive the browser")])
    for k in range(14):
        tid = f"shot_{k}"
        add("assistant", [Text(text="look " * 10), ToolUseBlock(id=tid, name="Screenshot", input={})])
        add("user", [ToolResultBlock(tool_use_id=tid, content=[Text(text="page text " * 1200),
            Image(media_type="image/png", data="iVBOR" * 20)])])
    add("assistant", [Text(text="done")])
    add("user", [Text(text="continue")])
    for node in path[start:]:
        await loop.store.append(loop.session_id, node)
    before = loop._compactor.estimate_view_tokens(path, loop._context_overhead_tokens())
    assert window * 0.30 < before < window * 0.32
    try:
        await loop.submit(UserTurn(content="go on"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        events = loop.bus.log(loop.session_id)
        assert not [e for e in events if isinstance(e, ErrorEvent)]
        assert any(isinstance(e, ContextCompaction) and e.reason == "image_consumed" for e in events)
        assert not [e for e in events if isinstance(e, ContextCompacting)]
        assert provider.summary_calls == 0
        assert ["summary" if r.system == CHECKPOINT_SYSTEM else "main" for r in provider.requests] == ["main"]
    finally:
        await loop.aclose()


async def test_sweep_only_below_pressure_never_awaits_checkpoint(tmp_path):
    window = 400_000
    loop, provider, _ = await seeded_loop(tmp_path, window, overhead=25000)
    path = benchmark.history(25)[:-1]
    loop._compactor.apply(path + benchmark.history(1)[-1:],
                          context_window=window, overhead_tokens=12000)
    loop._compactor.config = replace(loop._compactor.config, sweep_mode="on")
    start = len(path)

    def add(role, blocks):
        path.append(SessionNode(node_id=f"sweep_node_{len(path):06d}",
            parent_id=path[-1].node_id, message=Message(role=role, content=blocks)))

    add("user", [Text(text="inspect the implementation")])
    for k in range(8):
        tid = f"sweep_{k}"
        add("assistant", [ToolUseBlock(id=tid, name="Read", input={"file_path": f"f{k}"})])
        add("user", [ToolResultBlock(tool_use_id=tid, content=[Text(text="r" * 16000)],
                                   delivery_metadata_version=1)])
    add("assistant", [Text(text="done")])
    add("user", [Text(text="continue")])
    for node in path[start:]:
        await loop.store.append(loop.session_id, node)
    assert window * 0.15 < loop._compactor.estimate_view_tokens(path, 25000) < window * 0.45
    try:
        await loop.submit(UserTurn(content="go on"))
        await asyncio.wait_for(loop.wait_turn(), 5)
        events = loop.bus.log(loop.session_id)
        assert not [e for e in events if isinstance(e, ErrorEvent)]
        assert any(isinstance(e, ContextCompaction) and e.decision_reason == "sweep" for e in events)
        # Even after the free sweep, the view misses 15%; no size batch was due.
        assert loop._compactor.estimate_view_tokens(await loop.store.load_path(loop.session_id), 25000) > window * 0.15
        assert provider.summary_calls == 0
        assert ["summary" if r.system == CHECKPOINT_SYSTEM else "main" for r in provider.requests] == ["main"]
        assert not [e for e in events if isinstance(e, ContextCompacting)]
    finally:
        await loop.aclose()


@pytest.mark.parametrize("window", [128_000, 200_000])
async def test_native_automatic_batch_summary_call_measurement(tmp_path, window):
    """Reproducible real-loop measurement, not the benchmark's emulation."""
    loop, provider, path = await seeded_loop(tmp_path, window, overhead=25000)
    before = loop._compactor.estimate_view_tokens(path, loop._context_overhead_tokens())
    try:
        await loop.submit(UserTurn(content="continue"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        events = loop.bus.log(loop.session_id)
        assert not [e for e in events if isinstance(e, ErrorEvent)]
        batches = [e for e in events if isinstance(e, ContextCompaction) and e.batch_final]
        assert len(batches) == 1
        expected_calls = 1 if window == 128_000 else 0
        assert provider.summary_calls == expected_calls
        activity = [e.active for e in events if isinstance(e, ContextCompacting) and e.trigger == "auto"]
        assert activity == ([True, False] if expected_calls else [])
        request = next(r for r in provider.requests if r.system != CHECKPOINT_SYSTEM)
        after = estimate_tokens_of_messages(request.messages) + loop._context_overhead_tokens()
        assert after <= max(window * 0.15, 28000)  # overhead floor at 128k
        print(json.dumps(dict(window=window, overhead=loop._context_overhead_tokens(), before=before,
            after=after, batches=len(batches), summary_calls=provider.summary_calls,
            request_order=["summary" if r.system == CHECKPOINT_SYSTEM else "main" for r in provider.requests])))
        # No fresh rewrite/call when the unremovable overhead misses the target.
        await loop.submit(UserTurn(content="continue without growth"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert provider.summary_calls == expected_calls
        assert len([e for e in loop.bus.log(loop.session_id)
                    if isinstance(e, ContextCompaction) and e.batch_final]) == 1
    finally:
        await loop.aclose()


@pytest.mark.parametrize("outcome", ["success", "error", "stop", "cancel"])
async def test_automatic_checkpoint_activity_cleared_on_every_exit(tmp_path, outcome):
    class Gated(MeasuredProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def stream(self, request):
            if request.system == CHECKPOINT_SYSTEM:
                self.started.set()
                await self.release.wait()
                if outcome == "error":
                    raise RuntimeError("offline summary failure")
            async for chunk in super().stream(request):
                yield chunk

    provider = Gated()
    loop, _, _ = await seeded_loop(tmp_path, 128000, overhead=25000, provider=provider)
    try:
        await loop.submit(UserTurn(content="continue"))
        await asyncio.wait_for(provider.started.wait(), 5)
        activity = lambda: [e.active for e in loop.bus.log(loop.session_id)
                           if isinstance(e, ContextCompacting) and e.trigger == "auto"]
        assert activity() == [True]  # visible while the side call is blocked
        if outcome == "stop":
            await loop.submit(Interrupt())
        elif outcome == "cancel":
            loop._turn_task.cancel()
        else:
            provider.release.set()
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(loop.wait_turn(), 5)
        else:
            await asyncio.wait_for(loop.wait_turn(), 5)
        assert activity() == [True, False]
        main = [r for r in provider.requests if r.system != CHECKPOINT_SYSTEM]
        assert len(main) == (0 if outcome in {"stop", "cancel"} else 1)
    finally:
        await loop.aclose()
