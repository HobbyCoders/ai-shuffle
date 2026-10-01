"""The cache-priced compaction schedule (plan item 3, B4, B5).

``decide_schedule`` is pure, so most of these tests inject a CacheSignal
straight into ``Compactor.apply``. On a 200k window with a 4k output reserve
the zones are F = 90k, S = 120k, H = 148k, L = 180k and the deep target T is
70k, all in measured tokens. With no measured call on the path the estimate
is the measured basis, so the numbers below are chars/4 totals.
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fluid.context.cache_signal import (
    CacheSignal,
    Zones,
    decide_schedule,
    p_cold_from_hit_rates,
    update_p_cold,
)
from fluid.context.compaction import CompactionConfig, Compactor
from fluid.context.trim import TrimConfig, estimate_tokens_of_messages
from fluid.engine.native import NativeLoop
from fluid.events import ContextCompaction, Usage, UserTurn
from fluid.events.model import event_from_dict, event_to_dict
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import SessionNode
from fluid.store.jsonl import JsonlStore

_W, _RESERVE = 200_000, 4_000
_WARM = dict(gap_s=5.0)
_COLD = dict(gap_s=600.0)


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _history(turns: int, *, result_chars: int = 8_000) -> list[SessionNode]:
    """Complete old turns whose results the trim rung can take."""
    nodes: list[SessionNode] = []
    for t in range(turns):
        nodes.extend([
            _node(f"u{t}", "user", [Text(f"turn {t} " + "q" * 400)]),
            _node(f"a{t}", "assistant", [ToolUseBlock(
                id=f"c{t}", name="Read", input={"file_path": "p" * 2_000})]),
            _node(f"r{t}", "user", [ToolResultBlock(
                tool_use_id=f"c{t}", content=[Text("r" * result_chars)])]),
            _node(f"f{t}", "assistant", [Text(f"done {t}")]),
        ])
    return nodes


def _apply(c: Compactor, nodes: list[SessionNode], *, overhead: int,
           cache: CacheSignal | None, boundary: bool = False) -> tuple[list, list]:
    return c.apply(nodes, overhead_tokens=overhead, context_window=_W,
                   output_reserve_tokens=_RESERVE, turn_boundary=boundary,
                   cache=cache)


def _prompt(nodes: list[SessionNode], overhead: int) -> int:
    return overhead + estimate_tokens_of_messages([n.message for n in nodes])


def _fields(promotions: list) -> list[tuple]:
    return [(p.tier, p.reason, p.trimmed, p.spans, p.before_tokens, p.after_tokens)
            for p in promotions]


# ── The pure schedule ───────────────────────────────────────────────────


_ZONES = Zones(floor=115_000, trigger=153_600, hard=176_000, target=90_000,
               limit=208_000)


def test_cold_now_needs_a_gap_a_static_change_or_a_resume() -> None:
    assert not CacheSignal(gap_s=300.0).cold_now
    assert CacheSignal(gap_s=301.0).cold_reason == "gap"
    assert CacheSignal(static_changed=True, gap_s=1.0).cold_reason == "static_changed"
    assert CacheSignal(resumed=True).cold_reason == "resumed"
    # A flaky history is priced through p_cold, never treated as cold now:
    # a miss writes the prefix it missed.
    assert not CacheSignal(gap_s=1.0, p_cold=1.0).cold_now
    assert CacheSignal(gap_s=601.0).to_dict()["cold_reason"] == "gap"


def test_miss_ewma_folds_hit_rates_with_alpha_one_half() -> None:
    assert update_p_cold(None, 0.2) == 1.0
    assert update_p_cold(1.0, 0.9) == 0.5
    assert update_p_cold(0.5, 0.49) == 0.75
    assert p_cold_from_hit_rates([]) is None
    assert p_cold_from_hit_rates([0.0, 0.95, 0.95]) == 0.25


def test_zones_map_to_actions() -> None:
    warm, cold = CacheSignal(**_WARM), CacheSignal(**_COLD)
    for signal in (warm, cold):
        below = decide_schedule(signal, _ZONES, prompt=100_000)
        assert (below.zone, below.action, below.reason) == (
            "below_floor", "skip", "below_threshold")
        assert decide_schedule(signal, _ZONES, prompt=180_000).action == "legacy"
        assert decide_schedule(signal, _ZONES, prompt=210_000).zone == "window"
        assert decide_schedule(signal, _ZONES, prompt=160_000).action == "build"
    assert decide_schedule(warm, _ZONES, prompt=130_000).action == "skip"
    assert decide_schedule(cold, _ZONES, prompt=130_000).action == "build"


def test_priced_band_is_ski_rental_on_the_report_example() -> None:
    # P = 160k, after = 70k, floor 22k: R = 48k * 0.9 = 43.2k, s = 9k.
    warm = CacheSignal(reuse_floor=22_000, **_WARM)
    rent, defers = 0.0, 0
    while True:
        decision = decide_schedule(warm, _ZONES, prompt=160_000,
                                   candidate_after=70_000, rent_paid=rent)
        assert (decision.buy_price, decision.rent_per_call) == (43_200, 9_000)
        if decision.action == "commit":
            break
        assert decision.reason == "warm_defer"
        rent += decision.rent_per_call
        defers += 1
    assert (defers, decision.reason) == (4, "break_even")
    # Cold commits at once; a flaky cache (p_cold 0.85) prices R at 6.5k,
    # under one call's rent, so it fires at the trigger like the old ladder.
    cold = decide_schedule(CacheSignal(reuse_floor=22_000, **_COLD), _ZONES,
                           prompt=160_000, candidate_after=70_000)
    assert cold.reason == "cold_commit"
    flaky = decide_schedule(CacheSignal(reuse_floor=22_000, p_cold=0.85, **_WARM),
                            _ZONES, prompt=160_000, candidate_after=70_000)
    assert (flaky.reason, flaky.buy_price) == ("break_even", 6_480)


def test_opportunistic_band_commits_only_deep_cold_candidates() -> None:
    cold = CacheSignal(**_COLD)
    near_target = decide_schedule(cold, _ZONES, prompt=140_000, candidate_after=99_000)
    assert near_target.reason == "cold_opportunistic"
    large = decide_schedule(cold, _ZONES, prompt=140_000, candidate_after=125_000)
    assert large.reason == "cold_opportunistic"
    small = decide_schedule(cold, _ZONES, prompt=140_000, candidate_after=126_000)
    assert (small.action, small.reason) == ("reject", "insufficient_reclaim")


# ── The compactor with an injected signal ───────────────────────────────


def test_warm_defer_accumulates_rent_and_commits_at_break_even() -> None:
    overhead, nodes = 20_000, _history(40)
    assert 120_000 <= _prompt(nodes, overhead) < 148_000  # the priced band
    legacy = Compactor(config=CompactionConfig())
    _, expected = _apply(legacy, nodes, overhead=overhead, cache=None)
    assert legacy.last_decision["reason"] == "meaningful_reclaim"

    c = Compactor(config=CompactionConfig())
    warm = CacheSignal(reuse_floor=overhead, **_WARM)
    view = c.view(nodes)
    rents = []
    for _ in range(20):
        rendered, promotions = _apply(c, nodes, overhead=overhead, cache=warm)
        decision = c.last_decision
        if promotions:
            break
        assert decision["reason"] == "warm_defer"
        assert decision["schedule"] == "priced"
        assert decision["cache_signal"] == warm.to_dict()
        # A deferral keeps the cached prefix byte-identical and arms nothing.
        assert rendered == view == c.view(nodes)
        assert c._optional_retry is None
        rents.append(decision["rent_paid"])
    buy, rent = decision["buy_price"], decision["rent_per_call"]
    assert decision["reason"] == "break_even"
    # Rent grows by one call's worth per deferral, and the commit comes on the
    # first call whose rent reaches the buy price.
    assert rents == [rent * i for i in range(len(rents))]
    assert rents[-1] + rent < buy <= decision["rent_paid"] + rent
    assert len(rents) >= 5
    # The batch is the one the unpriced ladder takes at the trigger.
    assert _fields(promotions) == _fields(expected)
    assert {p.decision_reason for p in promotions} == {"break_even"}
    assert c._rent_paid == 0.0
    assert c.schedule_event_fields()["rent_paid"] == decision["rent_paid"]


def test_cold_now_commits_at_the_trigger() -> None:
    overhead, nodes = 20_000, _history(40)
    legacy = Compactor(config=CompactionConfig())
    _, expected = _apply(legacy, nodes, overhead=overhead, cache=None)
    for reason in ("gap", "static_changed", "resumed"):
        signal = CacheSignal(
            gap_s=600.0 if reason == "gap" else 5.0,
            static_changed=reason == "static_changed", resumed=reason == "resumed",
            reuse_floor=overhead,
        )
        c = Compactor(config=CompactionConfig())
        _, promotions = _apply(c, nodes, overhead=overhead, cache=signal)
        assert c.last_decision["reason"] == "cold_commit"
        assert c.last_decision["cold_reason"] == reason
        assert _fields(promotions) == _fields(expected)
        assert c.schedule_event_fields()["cold_reason"] == reason
        assert c.schedule_event_fields()["schedule"] == "priced"


def test_cold_now_commits_between_the_floor_and_the_trigger() -> None:
    overhead, nodes = 20_000, _history(30)
    before = _prompt(nodes, overhead)
    assert 90_000 <= before < 120_000
    # The unpriced ladder and a warm cache leave this band alone.
    legacy = Compactor(config=CompactionConfig())
    assert _apply(legacy, nodes, overhead=overhead, cache=None)[1] == []
    warm = Compactor(config=CompactionConfig())
    assert _apply(warm, nodes, overhead=overhead,
                  cache=CacheSignal(**_WARM))[1] == []
    assert warm.last_decision["reason"] == "below_threshold"
    assert warm.last_decision["schedule"] == "opportunistic"
    # Cold, it is a free rewrite: the batch goes to the deep target.
    c = Compactor(config=CompactionConfig())
    rendered, promotions = _apply(c, nodes, overhead=overhead,
                                  cache=CacheSignal(**_COLD))
    assert c.last_decision["reason"] == "cold_opportunistic"
    assert promotions and {p.decision_reason for p in promotions} == {"cold_opportunistic"}
    after = overhead + estimate_tokens_of_messages(rendered)
    assert after <= 70_000 * 1.1
    # The decision replays byte-identically, and the tree is untouched.
    resumed = Compactor(config=CompactionConfig())
    resumed.rehydrate(promotions)
    assert resumed.view(nodes) == rendered
    assert nodes[2].message.content[0].content[0].text == "r" * 8_000


def test_shallow_cold_candidate_in_the_band_is_dropped_and_arms_nothing() -> None:
    # Most of the prompt is overhead no rung can touch.
    overhead, nodes = 85_000, _history(6)
    assert 90_000 <= _prompt(nodes, overhead) < 120_000
    c = Compactor(config=CompactionConfig())
    view = c.view(nodes)
    rendered, promotions = _apply(c, nodes, overhead=overhead,
                                  cache=CacheSignal(**_COLD))
    assert promotions == []
    assert rendered == view == c.view(nodes)
    assert c.last_decision["reason"] == "insufficient_reclaim"
    assert c._optional_retry is None and c._shortfall_retry is None


def test_below_the_floor_builds_no_candidate() -> None:
    overhead, nodes = 20_000, _history(20)
    assert _prompt(nodes, overhead) < 90_000
    c = Compactor(config=CompactionConfig())

    def no_candidate(*args, **kwargs):
        raise AssertionError("a candidate was built below the floor")

    c._apply_candidate = no_candidate  # type: ignore[method-assign]
    for signal in (CacheSignal(**_WARM), CacheSignal(**_COLD)):
        _, promotions = _apply(c, nodes, overhead=overhead, cache=signal,
                               boundary=True)
        assert promotions == []
        assert c.last_decision["reason"] == "below_threshold"
        assert c.last_decision["schedule"] == "below_floor"
        assert c.last_decision["threshold_tokens"] == 120_000
        assert c._optional_retry is None


def _batches(overhead: int, cache: CacheSignal | None) -> list[tuple[int, list]]:
    """Grow a session one turn per call and record which calls committed."""
    c = Compactor(config=CompactionConfig())
    batches = []
    for turns in range(1, 90):
        _, promotions = _apply(c, _history(turns), overhead=overhead, cache=cache,
                               boundary=True)
        if promotions:
            batches.append((turns, _fields(promotions)))
            assert {p.decision_reason for p in promotions} <= {
                "meaningful_reclaim", "break_even"}
    return batches


def test_flaky_cache_behaves_like_the_unpriced_ladder() -> None:
    # A cache that misses most calls makes the buy price small: R is
    # (after - floor) * 0.9 * (1 - p_cold), against one call's rent of
    # 0.1 * (before - after). With a 30k system-and-tools floor, p_cold 0.9
    # puts R under one call's rent at every crossing, so the priced ladder
    # commits the same batches on the same calls as the ladder without a
    # signal. At p_cold 0.85, R sits within a few hundred tokens of the rent
    # on a crossing just over the trigger, so a batch can wait one more call
    # (the report's example, P = 160k, fires at once); never longer.
    overhead = 30_000
    legacy = _batches(overhead, None)
    assert len(legacy) == 3
    flaky = _batches(overhead, CacheSignal(p_cold=0.9, reuse_floor=overhead, **_WARM))
    assert flaky == legacy
    nearly = _batches(overhead, CacheSignal(p_cold=0.85, reuse_floor=overhead, **_WARM))
    assert len(nearly) == len(legacy)
    assert all(0 <= b[0] - a[0] <= 1 for a, b in zip(legacy, nearly))


def test_no_signal_unknown_window_and_switch_off_run_unpriced() -> None:
    overhead, nodes = 20_000, _history(40)
    legacy = Compactor(config=CompactionConfig())
    _, expected = _apply(legacy, nodes, overhead=overhead, cache=None)
    assert "schedule" not in legacy.last_decision
    warm = CacheSignal(reuse_floor=overhead, **_WARM)
    off = Compactor(config=CompactionConfig(cache_schedule=False))
    assert _fields(_apply(off, nodes, overhead=overhead, cache=warm)[1]) == _fields(expected)
    assert off.last_decision["reason"] == "meaningful_reclaim"
    # No known window: no zones, so no price.
    small = dict(trigger_tokens=100_000, trim=TrimConfig(min_result_chars=500))
    plain = Compactor(config=CompactionConfig(**small))
    priced = Compactor(config=CompactionConfig(**small))
    _, a = plain.apply(nodes, overhead_tokens=overhead)
    _, b = priced.apply(nodes, overhead_tokens=overhead, cache=warm)
    assert a and _fields(a) == _fields(b)
    # The trim-only contract ignores the signal.
    trim_only = dict(enabled=False, trim=TrimConfig(
        enabled=True, trigger_tokens=50_000, min_result_chars=500))
    t1 = Compactor(config=CompactionConfig(**trim_only))
    t2 = Compactor(config=CompactionConfig(**trim_only))
    _, a = _apply(t1, nodes, overhead=overhead, cache=None)
    _, b = _apply(t2, nodes, overhead=overhead, cache=warm)
    assert a and _fields(a) == _fields(b)
    assert "schedule" not in t2.last_decision


def test_hard_zone_commits_whatever_the_cache() -> None:
    overhead, nodes = 20_000, _history(50)
    assert 148_000 <= _prompt(nodes, overhead) < 180_000
    c = Compactor(config=CompactionConfig())
    _, promotions = _apply(c, nodes, overhead=overhead,
                           cache=CacheSignal(reuse_floor=overhead, **_WARM))
    assert promotions
    assert c.last_decision["reason"] == "hard_ceiling"
    assert c.last_decision["schedule"] == "hard"


def test_schedule_fields_round_trip_and_default_on_old_events() -> None:
    event = ContextCompaction(
        session_id="s", turn_id="t", tier=1, reason="trigger",
        decision_reason="break_even", schedule="priced", cold_reason="",
        threshold_tokens=120_000, target_tokens=70_000, rent_paid=38_724,
        turn_live_tokens=12_000,
    )
    assert event_from_dict(json.loads(json.dumps(event_to_dict(event)))) == event
    old = event_to_dict(event)
    for key in ("schedule", "cold_reason", "threshold_tokens", "target_tokens",
                "rent_paid", "turn_live_tokens"):
        old.pop(key)
    restored = event_from_dict(old)
    assert (restored.schedule, restored.cold_reason, restored.threshold_tokens,
            restored.target_tokens, restored.rent_paid, restored.turn_live_tokens) == (
        "", "", 0, 0, 0, 0)


# ── The engine builds the signal and persists the schedule ──────────────


class _UsageProvider:
    """Calls a tool ``calls`` times, then answers. Every call reports a prompt
    the size of the request it got, with ``hit`` of it read from cache."""

    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, calls: int, hit: float) -> None:
        self.calls_left, self.hit, self.n = calls, hit, 0
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        self.n += 1
        total = 2_000 + estimate_tokens_of_messages(request.messages)
        read = int(total * self.hit)
        yield StreamChunk("message_start", {"usage": {
            "input_tokens": total - read, "cache_read_input_tokens": read}})
        if self.calls_left:
            self.calls_left -= 1
            yield StreamChunk("block_start", {
                "index": 0, "block_type": "tool_use",
                "tool_use_id": f"t{self.n}", "tool_name": "mcp__b__blob"})
            yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": "{}"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 5},
                                        "stop_reason": "tool_use"})
        else:
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "done"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 5},
                                        "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})


async def _run_engine(tmp_path, *, hit: float) -> tuple[NativeLoop, list]:
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.inproc import create_inproc_server, tool
    from fluid.tools.registry import ToolRegistry

    @tool("blob", "Blob", {"type": "object", "properties": {}})
    async def blob(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "payload " + "B" * 30_000}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("b", [blob]))
    loop = NativeLoop(
        provider=_UsageProvider(12, hit), store=JsonlStore(tmp_path), model="m",
        registry=registry, permissions=PermissionEngine("never_ask"),
        # Window 100k with a 2k reserve: F 45k, S 60k, no hard zone, L 82k.
        context_window=100_000, max_tokens=2_000,
        compaction=CompactionConfig(trigger_tokens=1_000),
    )
    await loop.submit(UserTurn(content="Collect the blobs."))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    return loop, loop.bus.log(loop.session_id)


async def test_engine_prices_rewrites_from_live_hit_rates(tmp_path) -> None:
    """A provider that never hits makes p_cold 1, so the first priced call
    breaks even at once. One that hits 90% of the prompt defers first."""
    for hit, expect_defer in ((0.0, False), (0.9, True)):
        loop, events = await _run_engine(tmp_path / f"hit{hit}", hit=hit)
        decisions = [e.raw["compaction_decision"] for e in events
                     if isinstance(e, Usage) and "compaction_decision" in e.raw]
        signals = [d["cache_signal"] for d in decisions if "cache_signal" in d]
        # Every decision logs the signal it was priced with. The first call
        # of a new engine is resumed; the later ones carry a gap.
        assert len(signals) == len(decisions) > 10
        assert signals[0]["resumed"] and signals[0]["gap_s"] is None
        assert not any(s["resumed"] for s in signals[1:])
        assert all(s["gap_s"] is not None and not s["static_changed"]
                   for s in signals[1:])
        assert signals[-1]["p_cold"] == (1.0 if hit == 0.0 else 0.0)
        reasons = [d["reason"] for d in decisions]
        assert ("warm_defer" in reasons) is expect_defer
        rungs = [e for e in events if isinstance(e, ContextCompaction)]
        assert rungs
        first = rungs[0]
        assert (first.decision_reason, first.schedule) == ("break_even", "priced")
        assert first.threshold_tokens > 0 and first.target_tokens > 0
        assert first.turn_live_tokens > 0
        assert (first.rent_paid > 0) is expect_defer
        await loop.aclose()


async def test_resumed_engine_seeds_p_cold_from_persisted_usage(tmp_path) -> None:
    loop, _ = await _run_engine(tmp_path, hit=0.0)
    sid = loop.session_id
    await loop.aclose()
    fresh = NativeLoop(provider=_UsageProvider(0, 0.0), store=JsonlStore(tmp_path),
                       model="m", session_id=sid, context_window=100_000,
                       compaction=CompactionConfig(trigger_tokens=1_000))
    nodes = await fresh.store.load_path(sid)
    await fresh._measured_window_prompt(sid, nodes)
    signal = fresh._cache_signal(sid, overhead_tokens=1_000, turn_start=None)
    # Every persisted call missed, so the seed is 1 and the resumed engine
    # prices the next warm rewrite as nearly free.
    assert signal.resumed and signal.p_cold == 1.0
    again = fresh._cache_signal(sid, overhead_tokens=1_000, turn_start=None)
    assert not again.resumed and again.p_cold == 1.0
    fresh.system = "a different system prompt"
    assert fresh._cache_signal(sid, overhead_tokens=1_000, turn_start=None).static_changed
    await fresh.aclose()
