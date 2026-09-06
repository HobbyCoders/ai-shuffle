"""Prompt-cache instrumentation and window-scaled eviction triggers.

Pins three things the audit asked for:

* ``derive_trigger_tokens`` — the ladder arms at a fraction of a KNOWN context
  window, keeps the historical 120k floor, respects headroom, and is unchanged
  when the window is unknown or the ratio is configured to 0.
* ``fingerprint_prefix`` — stable absolute checkpoints, small payload, and the
  shape that lands in ``Usage.raw``.
* ``diagnose_cache_miss`` — fires only on a big fresh prompt with ~no cache
  read, and names the earliest segment that actually moved.
"""

import asyncio
from collections.abc import AsyncIterator

from fluid.context.cache_probe import (
    CacheProbeConfig,
    checkpoint_positions,
    diagnose_cache_miss,
    fingerprint_prefix,
    message_count,
)
from fluid.context.compaction import (
    CompactionConfig,
    Compactor,
    derive_trigger_tokens,
)
from fluid.context.trim import ToolResultTrimmer, TrimConfig
from fluid.engine.native import NativeLoop
from fluid.events import Usage, UserTurn
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode

# ─── Trigger derivation ──────────────────────────────────────────────────────


def test_trigger_unknown_window_is_the_bare_base() -> None:
    """No window known → the pre-existing fixed trigger, untouched."""
    assert (
        derive_trigger_tokens(
            base=120_000,
            window_ratio=0.6,
            context_window=None,
            headroom_tokens=16_000,
            output_reserve_tokens=32_000,
        )
        == 120_000
    )


def test_trigger_scales_up_with_a_large_window() -> None:
    """The b2f05a46 case: a 1M-window model must not compact at 120k."""
    assert (
        derive_trigger_tokens(
            base=120_000,
            window_ratio=0.6,
            context_window=1_000_000,
            headroom_tokens=16_000,
            output_reserve_tokens=32_000,
        )
        == 600_000
    )


def test_trigger_floor_holds_for_a_small_window() -> None:
    """A 200k model: window*0.6 = 120k is exactly the historical trigger, and
    the headroom cap never lets the threshold reach the window."""
    got = derive_trigger_tokens(
        base=120_000,
        window_ratio=0.6,
        context_window=200_000,
        headroom_tokens=16_000,
        output_reserve_tokens=32_000,
    )
    assert got == 120_000
    assert got <= 200_000 - 16_000 - 32_000


def test_trigger_capped_below_the_window_by_headroom() -> None:
    """Ratio would ask for more than the window can hold; headroom wins."""
    got = derive_trigger_tokens(
        base=120_000,
        window_ratio=0.95,
        context_window=200_000,
        headroom_tokens=16_000,
        output_reserve_tokens=32_000,
    )
    assert got == 200_000 - 16_000 - 32_000


def test_trigger_ratio_zero_restores_fixed_behavior() -> None:
    """The config override: ratio 0 pins the old fixed trigger, still capped."""
    assert (
        derive_trigger_tokens(
            base=120_000,
            window_ratio=0.0,
            context_window=1_000_000,
            headroom_tokens=16_000,
            output_reserve_tokens=0,
        )
        == 120_000
    )


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(
        node_id=nid, parent_id=None, message=Message(role=role, content=blocks)
    )


def _bulky_history(turns: int, result_chars: int = 20_000) -> list[SessionNode]:
    nodes: list[SessionNode] = []
    for t in range(turns):
        nodes.append(_node(f"u{t}", "user", [Text(f"ask {t}")]))
        nodes.append(
            _node(
                f"a{t}",
                "assistant",
                [ToolUseBlock(id=f"c{t}", name="Read", input={"p": f"f{t}"})],
            )
        )
        nodes.append(
            _node(
                f"r{t}",
                "user",
                [
                    ToolResultBlock(
                        tool_use_id=f"c{t}", content=[Text("x" * result_chars)]
                    )
                ],
            )
        )
        nodes.append(_node(f"f{t}", "assistant", [Text(f"done {t}")]))
    return nodes


def test_compactor_trim_only_holds_off_on_a_large_window() -> None:
    """The ai-shuffle contract (trim-only) under a big window: history that
    would have tripped the fixed 120k trigger is left alone."""
    nodes = _bulky_history(12)  # ~60k estimated tokens
    config = CompactionConfig(
        enabled=False, trim=TrimConfig(enabled=True, trigger_tokens=40_000)
    )
    small = Compactor(config=config)
    _, promotions = small.apply(nodes, context_window=None)
    assert promotions, "fixed trigger should have armed a batch trim"

    big = Compactor(config=config)
    _, promotions = big.apply(nodes, context_window=1_000_000)
    assert not promotions, "a 1M window must not trim at 60k tokens"


def test_trimmer_threshold_scales_with_window() -> None:
    trimmer = ToolResultTrimmer(
        config=TrimConfig(trigger_tokens=120_000, trigger_window_ratio=0.6)
    )
    assert trimmer.trigger_threshold(None) == 120_000
    assert trimmer.trigger_threshold(200_000) == 120_000
    assert trimmer.trigger_threshold(1_000_000) == 600_000


def test_context_config_carries_the_ratio() -> None:
    from fluid.config.model import ContextConfig

    assert ContextConfig().trigger_window_ratio == 0.6
    assert (
        ContextConfig.from_dict({"trigger_window_ratio": 0.25}).trigger_window_ratio
        == 0.25
    )


# ─── Prefix fingerprint shape ────────────────────────────────────────────────


def test_checkpoint_positions_are_absolute_powers_of_two() -> None:
    assert checkpoint_positions(0) == []
    assert checkpoint_positions(1) == [1]
    assert checkpoint_positions(8) == [1, 2, 4, 8]
    assert checkpoint_positions(11) == [1, 2, 4, 8, 11]
    # Absolute, so a shared checkpoint means the same prefix on both calls.
    assert set(checkpoint_positions(11)) >= set(checkpoint_positions(8)) - {8}


def _messages(n: int, *, mutate_at: int | None = None) -> list[Message]:
    out = []
    for i in range(n):
        text = f"message {i}" + ("!" if i == mutate_at else "")
        out.append(Message(role="user" if i % 2 == 0 else "assistant",
                           content=[Text(text)]))
    return out


def test_fingerprint_shape_is_small_and_ordered() -> None:
    fp = fingerprint_prefix(
        system="you are a harness",
        tools=[{"name": "Read", "input_schema": {}}],
        messages=_messages(20),
    )
    keys = list(fp)
    assert keys[:2] == ["system", "tools"]
    assert keys[2:] == ["m1", "m2", "m4", "m8", "m16", "m20"]
    assert all(len(v) == 16 for v in fp.values())
    # A handful of short hashes: cheap enough to persist on every Usage event.
    assert len(fp) <= 16


def test_fingerprint_prefix_is_stable_as_history_grows() -> None:
    """Append-only growth must leave every earlier checkpoint byte-identical —
    otherwise the diagnostic would flag a mutation on every single call."""
    a = fingerprint_prefix(system="s", tools=[], messages=_messages(8))
    b = fingerprint_prefix(system="s", tools=[], messages=_messages(12))
    for key in ("system", "tools", "m1", "m2", "m4", "m8"):
        assert a[key] == b[key]


def test_fingerprint_detects_a_mid_history_mutation() -> None:
    a = fingerprint_prefix(system="s", tools=[], messages=_messages(12))
    b = fingerprint_prefix(
        system="s", tools=[], messages=_messages(12, mutate_at=5)
    )
    assert a["m4"] == b["m4"]
    assert a["m8"] != b["m8"]


def test_fingerprint_separates_system_from_tools() -> None:
    base = fingerprint_prefix(system="s", tools=[{"name": "Read"}],
                              messages=_messages(4))
    sys_moved = fingerprint_prefix(system="s2", tools=[{"name": "Read"}],
                                   messages=_messages(4))
    tools_moved = fingerprint_prefix(system="s", tools=[{"name": "Grep"}],
                                     messages=_messages(4))
    assert base["system"] != sys_moved["system"]
    assert base["tools"] == sys_moved["tools"]
    assert base["tools"] != tools_moved["tools"]
    assert base["system"] == tools_moved["system"]


def test_fingerprint_sees_tool_result_text() -> None:
    def msgs(payload: str) -> list[Message]:
        return [
            Message(
                role="user",
                content=[
                    ToolResultBlock(tool_use_id="c1", content=[Text(payload)])
                ],
            )
        ]

    a = fingerprint_prefix(system=None, tools=[], messages=msgs("original"))
    b = fingerprint_prefix(system=None, tools=[], messages=msgs("trimmed"))
    assert a["m1"] != b["m1"]


# ─── Miss diagnostic ─────────────────────────────────────────────────────────


def _fp(**over: str) -> dict[str, str]:
    base = {"system": "a" * 16, "tools": "b" * 16, "m1": "c" * 16,
            "m2": "d" * 16, "m4": "e" * 16}
    base.update(over)
    return base


def test_no_diagnostic_on_a_healthy_cache_hit() -> None:
    assert (
        diagnose_cache_miss(
            previous=_fp(),
            current=_fp(m4="z" * 16),
            input_tokens=800,
            cache_read_tokens=90_000,
        )
        is None
    )


def test_no_diagnostic_on_a_small_prompt() -> None:
    """A short prompt that missed is noise, not a finding."""
    assert (
        diagnose_cache_miss(
            previous=_fp(),
            current=_fp(m2="z" * 16),
            input_tokens=3_000,
            cache_read_tokens=0,
        )
        is None
    )


def test_diagnostic_fires_on_a_big_uncached_prompt() -> None:
    out = diagnose_cache_miss(
        previous=_fp(),
        current=_fp(m4="z" * 16),
        input_tokens=100_000,
        cache_read_tokens=512,  # the observed "full re-send" signature
    )
    assert out is not None
    assert out["reason"] == "prefix_changed"
    assert out["changed"] == "m4"
    assert out["stable_through"] == "m2"
    assert out["message_range"] == [2, 4]


def test_diagnostic_names_the_earliest_changed_segment() -> None:
    """Later checkpoints are cumulative echoes; only the first one is evidence."""
    out = diagnose_cache_miss(
        previous=_fp(),
        current=_fp(system="z" * 16, m2="y" * 16, m4="x" * 16),
        input_tokens=50_000,
        cache_read_tokens=0,
    )
    assert out["changed"] == "system"
    assert out["stable_through"] is None
    assert "message_range" not in out


def test_diagnostic_flags_tool_schema_churn() -> None:
    out = diagnose_cache_miss(
        previous=_fp(),
        current=_fp(tools="z" * 16),
        input_tokens=50_000,
        cache_read_tokens=0,
    )
    assert out["changed"] == "tools"


def test_diagnostic_reports_an_unmutated_prefix() -> None:
    """Nothing moved but the cache still missed — the F6/9235e80f case. Says so
    explicitly instead of blaming a block."""
    previous = _fp()
    current = dict(previous)
    current["m8"] = "f" * 16  # history only grew
    out = diagnose_cache_miss(
        previous=previous, current=current,
        input_tokens=60_000, cache_read_tokens=0,
    )
    assert out["reason"] == "prefix_grew_only"
    assert out["stable_through"] == "m4"
    assert "expiry" in out["note"]


def test_diagnostic_reports_the_first_call_of_a_session() -> None:
    out = diagnose_cache_miss(
        previous=None, current=_fp(), input_tokens=60_000, cache_read_tokens=0
    )
    assert out["reason"] == "no_previous_request"


def test_diagnostic_respects_config_thresholds() -> None:
    config = CacheProbeConfig(min_input_tokens=1_000, max_cache_read_tokens=0)
    assert (
        diagnose_cache_miss(
            previous=_fp(), current=_fp(m2="z" * 16),
            input_tokens=2_000, cache_read_tokens=1, config=config,
        )
        is None
    )
    assert (
        diagnose_cache_miss(
            previous=_fp(), current=_fp(m2="z" * 16),
            input_tokens=2_000, cache_read_tokens=0, config=config,
        )
        is not None
    )


def test_probe_can_be_disabled() -> None:
    assert (
        diagnose_cache_miss(
            previous=_fp(), current=_fp(m2="z" * 16),
            input_tokens=100_000, cache_read_tokens=0,
            config=CacheProbeConfig(enabled=False),
        )
        is None
    )


# ─── Engine wiring ───────────────────────────────────────────────────────────


class _ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, usages: list[dict]) -> None:
        self.usages = usages
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        usage = self.usages[min(self.calls, len(self.usages) - 1)]
        self.calls += 1
        yield StreamChunk("message_start", {"usage": usage})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "ok"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk(
            "usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}
        )
        yield StreamChunk("message_stop", {})


async def test_usage_events_carry_prefix_hashes_and_a_miss_diagnostic() -> None:
    """Two turns: the first re-sends everything fresh (no previous request),
    the second misses again after the prompt grew — both land in ``Usage.raw``."""
    provider = _ScriptedProvider(
        [
            {"input_tokens": 50_000, "cache_read_input_tokens": 0},
            {"input_tokens": 60_000, "cache_read_input_tokens": 512},
        ]
    )
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", system="sys"
    )
    for i in range(2):
        await loop.submit(UserTurn(content=f"turn {i}"))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)

    usages = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]
    assert len(usages) == 2
    for event in usages:
        hashes = event.raw["prefix_hashes"]
        assert list(hashes)[:2] == ["system", "tools"]
        assert all(len(v) == 16 for v in hashes.values())

    assert usages[0].raw["cache_miss"]["reason"] == "no_previous_request"
    second = usages[1].raw["cache_miss"]
    # The user turn appended messages; nothing upstream was rewritten.
    assert second["reason"] == "prefix_grew_only"
    assert second["cache_read_tokens"] == 512


async def test_no_miss_diagnostic_when_the_cache_is_working() -> None:
    provider = _ScriptedProvider(
        [{"input_tokens": 200, "cache_read_input_tokens": 90_000}]
    )
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", system="sys"
    )
    await loop.submit(UserTurn(content="hello"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    usage = next(
        e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)
    )
    assert "prefix_hashes" in usage.raw
    assert "cache_miss" not in usage.raw


async def test_probe_can_be_turned_off_on_the_engine() -> None:
    provider = _ScriptedProvider(
        [{"input_tokens": 50_000, "cache_read_input_tokens": 0}]
    )
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m",
        cache_probe=CacheProbeConfig(enabled=False),
    )
    await loop.submit(UserTurn(content="hello"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    usage = next(
        e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)
    )
    assert "prefix_hashes" not in usage.raw
    assert "cache_miss" not in usage.raw


# ─── Blind spot (b): the shared-count checkpoint ─────────────────────────────


def test_extra_checkpoint_covers_the_whole_shared_prefix() -> None:
    """Powers of two alone stop at m32 on a 63-message history, leaving 33..63
    uncompared. Snapshotting the previous request's count closes the gap."""
    assert checkpoint_positions(63) == [1, 2, 4, 8, 16, 32, 63]
    assert checkpoint_positions(70, [63]) == [1, 2, 4, 8, 16, 32, 63, 64, 70]
    # Ascending whatever order the extras arrive in — dict order IS prefix
    # order for the bisection.
    assert checkpoint_positions(70, [63, 3]) == [1, 2, 3, 4, 8, 16, 32, 63, 64, 70]
    # Out-of-range extras are ignored rather than inventing a checkpoint.
    assert checkpoint_positions(8, [0, -1, 99]) == [1, 2, 4, 8]


def test_message_count_reads_the_largest_checkpoint() -> None:
    assert message_count(None) == 0
    assert message_count({"system": "a", "tools": "b"}) == 0
    assert message_count({"m1": "a", "m2": "b", "m63": "c"}) == 63


def test_extra_checkpoint_localizes_a_mutation_past_the_last_power_of_two() -> None:
    """The audited case: message 40 of 63 was rewritten and the diagnosis said
    the prefix was unchanged, because nothing between m32 and m63 was hashed."""
    previous = fingerprint_prefix(system="s", tools=[], messages=_messages(63))
    grown = _messages(70)
    grown[40] = Message(role="assistant", content=[Text("rewritten")])

    blind = diagnose_cache_miss(
        previous=previous,
        current=fingerprint_prefix(system="s", tools=[], messages=grown),
        input_tokens=90_000,
        cache_read_tokens=0,
    )
    assert blind["reason"] == "prefix_grew_only"
    assert blind["stable_through"] == "m32"
    # …and it says so rather than pretending m32 proved anything about 33..63.
    assert blind["verified_messages"] == 32
    assert blind["shared_messages"] == 63
    assert "never compared" in blind["note"]

    seeing = diagnose_cache_miss(
        previous=previous,
        current=fingerprint_prefix(
            system="s", tools=[], messages=grown, extra_checkpoints=(63,)
        ),
        input_tokens=90_000,
        cache_read_tokens=0,
    )
    assert seeing["reason"] == "prefix_changed"
    assert seeing["changed"] == "m63"
    assert seeing["message_range"] == [32, 63]


def test_full_coverage_note_says_the_prefix_really_was_whole() -> None:
    previous = fingerprint_prefix(system="s", tools=[], messages=_messages(63))
    current = fingerprint_prefix(
        system="s", tools=[], messages=_messages(70), extra_checkpoints=(63,)
    )
    out = diagnose_cache_miss(
        previous=previous, current=current,
        input_tokens=90_000, cache_read_tokens=0,
    )
    assert out["reason"] == "prefix_grew_only"
    assert out["verified_messages"] == out["shared_messages"] == 63
    assert "all 63 messages" in out["note"]
    assert "expiry" in out["note"]
    assert "never compared" not in out["note"]


# ─── Blind spot (c): a rewrite fluid itself performed ────────────────────────


def test_a_compaction_since_the_last_request_is_named_not_the_provider() -> None:
    previous = _fp()
    current = dict(previous)
    current["m8"] = "f" * 16  # history grew; the shared prefix matched
    out = diagnose_cache_miss(
        previous=previous, current=current,
        input_tokens=60_000, cache_read_tokens=0,
        history_rewrites=[{"tier": 1, "reason": "image_consumed", "trimmed": 1}],
    )
    assert out["reason"] == "history_rewritten"
    assert out["history_rewritten"][0]["reason"] == "image_consumed"
    assert "tier-1 image_consumed" in out["note"]
    assert "expiry" not in out["note"]


def test_a_rewrite_is_recorded_even_when_a_segment_did_change() -> None:
    out = diagnose_cache_miss(
        previous=_fp(), current=_fp(m4="z" * 16),
        input_tokens=60_000, cache_read_tokens=0,
        history_rewrites=[{"tier": 2, "reason": "trigger"}],
    )
    assert out["reason"] == "prefix_changed"
    assert out["changed"] == "m4"
    assert out["history_rewritten"] == [{"tier": 2, "reason": "trigger"}]
    assert "tier-2 trigger" in out["note"]


def test_describe_promotions_is_the_signal_shape() -> None:
    from fluid.context.compaction import Promotion, describe_promotions

    records = describe_promotions([
        Promotion(
            tier=1, reason="image_consumed",
            trimmed=(("n1", "c1"),), reclaimed_chars=531_000,
        )
    ])
    assert records == [{
        "tier": 1, "reason": "image_consumed", "trimmed": 1,
        "spans": 0, "reclaimed_chars": 531_000,
    }]


async def test_engine_blames_its_own_image_trim_not_the_provider() -> None:
    """End to end: a consume-once image trim rewrites an already-sent message,
    the next call misses, and the Usage event names the trim."""
    from fluid.context.compaction import CompactionConfig
    from fluid.messages import Image

    provider = _ScriptedProvider([
        {"input_tokens": 50_000, "cache_read_input_tokens": 0},
        {"input_tokens": 60_000, "cache_read_input_tokens": 0},
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", system="sys",
        compaction=CompactionConfig(
            # Armed: image consumption needs real pressure now, and the trim
            # rung it shares a threshold with cannot reach the one result.
            enabled=False, trim=TrimConfig(enabled=True, trigger_tokens=1)
        ),
    )
    await loop.submit(UserTurn(content=[Image(data="A" * 4_000, media_type="image/png")]))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await loop.submit(UserTurn(content="and now?"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    usages = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]
    second = usages[1].raw["cache_miss"]
    # The image sits in message 1, so a checkpoint does catch the rewrite here
    # — and the record says who rewrote it, which is the part the transcript
    # was missing.
    assert second["reason"] == "prefix_changed"
    assert second["history_rewritten"][0]["reason"] == "image_consumed"
    assert "tier-1 image_consumed" in second["note"]
    # The rung belongs to the request it preceded, not to the one after.
    assert "history_rewritten" not in usages[0].raw.get("cache_miss", {})


# ─── Blind spot (a): the fingerprint survives a rebuilt engine ───────────────


async def test_fingerprint_is_recovered_from_the_persisted_usage_events() -> None:
    """The app rebuilds the engine per run, so every turn's first call used to
    report ``no_previous_request``. The fingerprint rides the Usage event, so a
    fresh engine on the same session reads the real previous request back."""
    store = MemoryStore()
    first = NativeLoop(
        provider=_ScriptedProvider(
            [{"input_tokens": 50_000, "cache_read_input_tokens": 0}]
        ),
        store=store, model="m", system="sys",
    )
    await first.submit(UserTurn(content="turn one"))
    await asyncio.wait_for(first.wait_turn(), timeout=5)
    sid = first.session_id

    # A brand-new engine on the same session — and a changed system prompt,
    # exactly the observed miss (system hash moved, every mK identical).
    second = NativeLoop(
        provider=_ScriptedProvider(
            [{"input_tokens": 60_000, "cache_read_input_tokens": 0}]
        ),
        store=store, model="m", system="a different system prompt",
        session_id=sid,
    )
    await second.submit(UserTurn(content="turn two"))
    await asyncio.wait_for(second.wait_turn(), timeout=5)

    miss = [
        e for e in second.bus.log(sid) if isinstance(e, Usage)
    ][-1].raw["cache_miss"]
    assert miss["reason"] == "prefix_changed"
    assert miss["changed"] == "system"


async def test_the_very_first_call_of_a_session_still_says_so() -> None:
    """Nothing to recover means nothing to diagnose — the honest answer."""
    loop = NativeLoop(
        provider=_ScriptedProvider(
            [{"input_tokens": 50_000, "cache_read_input_tokens": 0}]
        ),
        store=MemoryStore(), model="m", system="sys",
    )
    await loop.submit(UserTurn(content="hello"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    usage = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage))
    assert usage.raw["cache_miss"]["reason"] == "no_previous_request"
