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

import pytest

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
        # A resume is cold only when the previous call's age is unknown.
        signal = CacheSignal(
            gap_s={"gap": 600.0, "static_changed": 5.0, "resumed": None}[reason],
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
    # (the report's example, P = 160k, fires at once); never longer. A batch
    # that waits shifts every later crossing with it, so the bound is checked
    # per crossing: at most one warm deferral before each commit.
    overhead = 30_000
    legacy = _batches(overhead, None)
    assert len(legacy) == 3
    flaky = _batches(overhead, CacheSignal(p_cold=0.9, reuse_floor=overhead, **_WARM))
    assert flaky == legacy
    nearly = _batches(overhead, CacheSignal(p_cold=0.85, reuse_floor=overhead, **_WARM))
    assert len(nearly) == len(legacy)
    assert all(0 <= b[0] - a[0] for a, b in zip(legacy, nearly))
    c = Compactor(config=CompactionConfig())
    signal = CacheSignal(p_cold=0.85, reuse_floor=overhead, **_WARM)
    deferrals = 0
    for turns in range(1, 90):
        _, promotions = _apply(c, _history(turns), overhead=overhead, cache=signal,
                               boundary=True)
        if promotions:
            assert deferrals <= 1, turns
            deferrals = 0
        elif c.last_decision.get("reason") == "warm_defer":
            deferrals += 1


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


async def test_resumed_engine_seeds_p_cold_from_persisted_usage(tmp_path, monkeypatch) -> None:
    loop, events = await _run_engine(tmp_path, hit=0.0)
    last_usage = next(e for e in reversed(events) if isinstance(e, Usage))
    monkeypatch.setattr("fluid.engine.native.time.time", lambda: last_usage.ts + 30)
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
    assert signal.gap_s == 30.0 and not signal.cold_now
    assert not signal.static_changed  # A new engine has no previous static hash.
    again = fresh._cache_signal(sid, overhead_tokens=1_000, turn_start=None)
    assert not again.resumed and again.p_cold == 1.0
    fresh.system = "a different system prompt"
    assert fresh._cache_signal(sid, overhead_tokens=1_000, turn_start=None).static_changed
    await fresh.aclose()


@pytest.mark.parametrize("gap,reason", [(20, ""), (300, ""), (400, "gap")])
def test_resumed_cache_signal_uses_known_gap(gap, reason) -> None:
    signal = CacheSignal(resumed=True, gap_s=gap)
    assert signal.cold_reason == reason
    assert signal.to_dict()["resumed"] is True
    assert CacheSignal(resumed=True, gap_s=gap, static_changed=True).cold_reason == "static_changed"


@pytest.mark.parametrize("gap,reason", [(20, ""), (400, "gap")])
async def test_unpark_cache_signal_uses_known_gap(tmp_path, monkeypatch, gap, reason) -> None:
    from fluid.events.model import TurnStarted
    from fluid.store.base import persist_event

    loop = NativeLoop(provider=_UsageProvider(0, 0.0), store=JsonlStore(tmp_path), model="m")
    sid = (await loop.store.create_session()).session_id
    now = 1_000.0
    monkeypatch.setattr("fluid.engine.native.time.time", lambda: now)
    try:
        assert loop._cache_signal(sid, overhead_tokens=0, turn_start=None).cold_reason == "resumed"
        await persist_event(loop.store, TurnStarted(
            session_id=sid, node_id="wake", seq=1, source="unpark",
            model="m", provider="fake",
        ))
        await loop._note_wake_turn_starts(sid)
        now += gap
        signal = loop._cache_signal(sid, overhead_tokens=0, turn_start="wake")
        assert signal.resumed and signal.gap_s == gap
        assert signal.cold_reason == reason
    finally:
        await loop.aclose()


@pytest.mark.parametrize("events,expected_gap,reason", [
    ([], None, "resumed"),
    ([(0, 100)], None, "resumed"),  # Legacy Usage without a timestamp is unknown.
    ([(970, 100), (995, 0)], 30, ""),  # Unmeasured Usage is not a call seed.
    ([(500, 100), (970, 100)], 30, ""),
    ([(600, 100)], 400, "gap"),
])
async def test_fresh_cache_signal_seeds_newest_measured_usage(
    tmp_path, monkeypatch, events, expected_gap, reason,
) -> None:
    from fluid.store.base import persist_event

    store = JsonlStore(tmp_path)
    sid = (await store.create_session()).session_id
    for seq, (ts, tokens) in enumerate(events, start=1):
        await persist_event(store, Usage(session_id=sid, node_id="off-path",
                                        seq=seq, ts=ts, input_tokens=tokens))
    fresh = NativeLoop(provider=_UsageProvider(0, 0.0), store=JsonlStore(tmp_path),
                       model="m", session_id=sid)
    monkeypatch.setattr("fluid.engine.native.time.time", lambda: 1_000.0)
    try:
        # Provider-cache age is session-wide even when the measured node is off-path.
        assert await fresh._rehydrate_last_call_usage(sid, []) is None
        signal = fresh._cache_signal(sid, overhead_tokens=0, turn_start=None)
        assert signal.resumed and not signal.static_changed
        assert signal.gap_s == expected_gap and signal.cold_reason == reason
    finally:
        await fresh.aclose()


# ── The boundary sweep (plan item 8) ────────────────────────────────────
#
# Results here are 16k chars (4k tokens), so the 20k-token keep holds the
# newest five results of the earlier turns and a turn of eight calls carries
# 32k tokens of them.


def _sweep_turn(t: int, *, calls: int = 8, result_chars: int = 16_000,
                tools: list[tuple[str, dict]] | None = None) -> list[SessionNode]:
    """One complete turn. Node ids are hex so HistoryExpand refs parse."""
    nodes = [_start(t)]
    for k in range(calls):
        name, input_ = tools[k] if tools and k < len(tools) else (
            "Read", {"file_path": f"f{t}_{k}"})
        nodes.extend([
            _node(f"node_{t:02x}{k:02x}a", "assistant", [ToolUseBlock(
                id=f"c{t}_{k}", name=name, input=input_)]),
            _node(f"node_{t:02x}{k:02x}b", "user", [ToolResultBlock(
                tool_use_id=f"c{t}_{k}",
                content=[Text(f"{t}:{k} ".ljust(result_chars, "r"))],
                delivery_metadata_version=1)]),
        ])
    nodes.append(_node(f"node_{t:02x}ffc", "assistant", [Text(f"done {t}")]))
    return nodes


def _start(t: int) -> SessionNode:
    return _node(f"node_{t:02x}00", "user", [Text(f"turn {t} " + "q" * 400)])


def _results(nodes: list[SessionNode]) -> list[tuple[str, str]]:
    return [(n.node_id, n.message.content[0].tool_use_id) for n in nodes
            if n.message.role == "user"
            and isinstance(n.message.content[0], ToolResultBlock)]


def _views(rendered: list) -> str:
    return json.dumps([m.to_dict() for m in rendered], sort_keys=True, default=str)


def _drive(mode: str, *, turns: int = 7, overhead: int = 20_000,
           cold_boundaries: bool = True) -> list[tuple[str, list, dict]]:
    """Every model-call point of a growing session: warm mid-turn, and cold
    (or warm) at each turn's first call."""
    c = Compactor(config=CompactionConfig(sweep_mode=mode))
    nodes: list[SessionNode] = []
    out: list[tuple[str, list, dict]] = []
    for t in range(turns):
        turn = _sweep_turn(t)
        nodes.append(turn[0])
        for k in range(8):
            boundary = k == 0
            signal = CacheSignal(**(_COLD if boundary and cold_boundaries else _WARM))
            rendered, promotions = _apply(c, nodes, overhead=overhead,
                                          cache=signal, boundary=boundary)
            out.append((_views(rendered), _fields(promotions), dict(c.last_decision)))
            nodes.extend(turn[1 + 2 * k: 3 + 2 * k])
        nodes.append(turn[-1])
    return out


def test_shadow_sweep_leaves_the_view_byte_identical() -> None:
    for cold in (True, False):
        off = _drive("off", cold_boundaries=cold)
        shadow = _drive("shadow", cold_boundaries=cold)
        # The run crosses the trigger, so the ladder commits batches too.
        assert any(promos for _, promos, _ in off)
        assert [(v, p) for v, p, _ in shadow] == [(v, p) for v, p, _ in off]
        # Shadow mode logs the would-be sweep on every turn boundary, and
        # only there; off logs nothing.
        logged = [d for _, _, d in shadow if "sweep_mode" in d]
        assert len(logged) == 7
        assert all("sweep_mode" not in d for _, _, d in off)
        assert any(d["swept_results"] > 0 and d["sweep_reclaim_tokens"] > 0
                   for d in logged)
        gates = {d["sweep_gate"] for d in logged}
        assert ("cold" in gates) is cold
        assert ("warm_defer" in gates) is not cold


def test_warm_sweep_below_the_trigger_defers() -> None:
    overhead = 20_000
    # Three finished turns (96k tokens of results) put the prompt between F
    # and S; one puts it below F.
    for turns, zone in ((3, "opportunistic"), (1, "below_floor")):
        nodes = [n for t in range(turns) for n in _sweep_turn(t)] + [_start(turns)]
        prompt = _prompt(nodes, overhead)
        assert prompt < 120_000
        assert (prompt >= 90_000) is (zone == "opportunistic")
        c = Compactor(config=CompactionConfig(sweep_mode="on"))
        view = c.view(nodes)
        rendered, promotions = _apply(c, nodes, overhead=overhead,
                                      cache=CacheSignal(**_WARM), boundary=True)
        assert promotions == [] and rendered == view
        d = c.last_decision
        assert (d["reason"], d["schedule"], d["sweep_gate"]) == (
            "below_threshold", zone, "warm_defer")
        assert d["swept_results"] > 0
        # Without a cache signal the sweep waits for a batch the same way.
        unpriced = Compactor(config=CompactionConfig(sweep_mode="on"))
        assert _apply(unpriced, nodes, overhead=overhead, cache=None,
                      boundary=True)[1] == []
        assert unpriced.last_decision["sweep_gate"] == "below_threshold"
        # Cold, the same sweep is free. Below F it commits on its own; from
        # F up it is the first rung of the cold opportunistic batch, which
        # here has nothing else to take.
        cold = Compactor(config=CompactionConfig(sweep_mode="on"))
        rendered, promotions = _apply(cold, nodes, overhead=overhead,
                                      cache=CacheSignal(**_COLD), boundary=True)
        reason = "cold_opportunistic" if zone == "opportunistic" else "sweep"
        assert [p.reason for p in promotions] == ["sweep"]
        assert {p.decision_reason for p in promotions} == {reason}
        assert cold.last_decision["reason"] == reason
        assert len(promotions[0].trimmed) == d["swept_results"]
        assert prompt - (
            overhead + estimate_tokens_of_messages(rendered)
        ) == d["sweep_reclaim_tokens"]
        # Mid-turn there is no sweep at all.
        mid = Compactor(config=CompactionConfig(sweep_mode="on"))
        _apply(mid, nodes, overhead=overhead, cache=CacheSignal(**_COLD))
        assert "sweep_gate" not in mid.last_decision


def test_sweep_first_changed_stays_inside_the_previous_turn() -> None:
    """A sweep rewrites only what fell out of the keep since the last one.

    At the first sweep that is the older part of the previous turn. Later
    the keep also hands back the results it held for one turn, which sit at
    the very end of the turn before, so the rewrite starts there and never
    reaches anything an earlier sweep already decided.
    """
    overhead = 20_000
    c = Compactor(config=CompactionConfig(sweep_mode="on"))
    nodes: list[SessionNode] = []
    kept_before: list[int] = []
    for t in range(5):
        nodes.append(_start(t))
        if t:
            starts = [i for i, n in enumerate(nodes) if n.node_id.endswith("00")]
            view = c.view(nodes)
            rendered, promotions = _apply(c, nodes, overhead=overhead,
                                          cache=CacheSignal(**_COLD), boundary=True)
            assert [p.reason for p in promotions] == ["sweep"]
            first = promotions[0].first_changed_message
            assert first == next(i for i, (a, b) in enumerate(zip(view, rendered))
                                 if a != b)
            if t == 1:
                assert starts[-2] < first < starts[-1]
            else:
                # The oldest result the last keep held, at the end of the
                # turn before the previous one.
                assert first == min(kept_before)
                assert starts[-3] < first < starts[-2]
            # Everything older than the change is untouched.
            assert rendered[:first] == view[:first]
            trimmed = c.trimmer._trimmed
            kept_before = [
                i for i, n in enumerate(nodes[:starts[-1]])
                if n.message.role == "user"
                and isinstance(n.message.content[0], ToolResultBlock)
                and (n.node_id, n.message.content[0].tool_use_id) not in trimmed
            ]
            assert len(kept_before) == 5  # 20k tokens of 4k-token results
        nodes.extend(_sweep_turn(t)[1:])


def test_history_expand_pins_results_for_the_next_turn() -> None:
    overhead = 20_000
    old = _sweep_turn(0)
    target = old[4]  # the second result of turn 0
    expand = ("HistoryExpand", {"ref": target.node_id})
    cell = ("run_python", {"code": "print(HistoryExpand(ref=%r))" % old[6].node_id})
    turn1 = _sweep_turn(1, tools=[expand, cell])
    nodes = old + turn1 + [_start(2)]
    c = Compactor(config=CompactionConfig(sweep_mode="on"))
    _, promotions = _apply(c, nodes, overhead=overhead,
                           cache=CacheSignal(**_COLD), boundary=True)
    swept = set(promotions[0].trimmed)
    pinned = {(target.node_id, "c0_1"), (old[6].node_id, "c0_2"),
              (turn1[2].node_id, "c1_0"), (turn1[4].node_id, "c1_1")}
    assert not pinned & swept
    d = c.last_decision
    assert d["pinned_results"] == 4
    assert d["pinned_tokens"] == 16_000
    # Pins do not count toward the keep: the newest five other results stay.
    unpinned = [p for p in _results(nodes) if p not in pinned]
    assert [p for p in unpinned if p not in swept] == unpinned[-5:]
    # The pin lasts one turn: with no expand in the turn that just ended,
    # the next sweep takes the pinned results too.
    nodes = nodes + _sweep_turn(2)[1:] + [_start(3)]
    _, later = _apply(c, nodes, overhead=overhead,
                      cache=CacheSignal(**_COLD), boundary=True)
    assert pinned <= set(later[0].trimmed)
    # The pin cap keeps the newest pins only.
    capped = Compactor(config=CompactionConfig(sweep_mode="on", sweep_pin_tokens=9_000))
    _apply(capped, old + turn1 + [_start(2)], overhead=overhead,
           cache=CacheSignal(**_COLD), boundary=True)
    assert capped.last_decision["pinned_results"] == 2


def test_sweep_rides_a_due_batch_and_replays_byte_identically() -> None:
    overhead = 28_000
    nodes = [n for t in range(3) for n in _sweep_turn(t)] + [_start(3)]
    assert 120_000 <= _prompt(nodes, overhead) < 148_000
    # Warm over the trigger: the sweep is one rung of the priced batch, so
    # it waits while the batch defers and commits with it at break-even.
    c = Compactor(config=CompactionConfig(sweep_mode="on"))
    signal = CacheSignal(reuse_floor=overhead, **_WARM)
    for _ in range(20):
        rendered, promotions = _apply(c, nodes, overhead=overhead,
                                      cache=signal, boundary=True)
        if promotions:
            break
        assert c.last_decision["reason"] == "warm_defer"
        assert c.last_decision["sweep_gate"] == "batch"
    assert promotions[0].reason == "sweep"
    assert {p.decision_reason for p in promotions} == {"break_even"}
    assert len({p.first_changed_message for p in promotions}) == 1
    resumed = Compactor(config=CompactionConfig(sweep_mode="on"))
    resumed.rehydrate(promotions)
    assert resumed.view(nodes) == rendered
    # The tree is never mutated, and errors are never swept.
    assert nodes[2].message.content[0].content[0].text == "0:0 ".ljust(16_000, "r")
    errs = _sweep_turn(9)
    errs[2] = _node(errs[2].node_id, "user", [ToolResultBlock(
        tool_use_id="c9_0", content=[Text("E" * 30_000)], is_error=True,
        delivery_metadata_version=1)])
    with_error = errs + [n for t in range(2) for n in _sweep_turn(t)] + [_start(5)]
    e = Compactor(config=CompactionConfig(sweep_mode="on"))
    _, promotions = _apply(e, with_error, overhead=overhead,
                           cache=CacheSignal(**_COLD), boundary=True)
    assert promotions and all(p[1] != "c9_0" for p in promotions[0].trimmed)


def test_trim_only_never_sweeps() -> None:
    nodes = [n for t in range(3) for n in _sweep_turn(t)] + [_start(3)]
    config = CompactionConfig(enabled=False, trim=TrimConfig(enabled=True),
                              sweep_mode="on")
    c = Compactor(config=config)
    _apply(c, nodes, overhead=20_000, cache=CacheSignal(**_COLD), boundary=True)
    assert "sweep_gate" not in c.last_decision
