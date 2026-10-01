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

import pytest

from fluid.context.cache_probe import (
    CacheProbeConfig,
    checkpoint_positions,
    diagnose_cache_miss,
    fingerprint_prefix,
    is_unexplained_miss,
    message_count,
)
from fluid.context.compaction import (
    CompactionConfig,
    Compactor,
    derive_trigger_tokens,
)
from fluid.context.trim import ToolResultTrimmer, TrimConfig
from fluid.engine.native import NativeLoop
from fluid.events import CacheMissAlert, Usage, UserTurn
from fluid.hooks.bus import HookBus
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry

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
        text = f"message {i} " + "payload " * 3_000 + ("!" if i == mutate_at else "")
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
    assert [k for k in keys if k.startswith("m")] == ["m1", "m2", "m4", "m8", "m16", "m20"]
    assert all(len(v) == 16 for k, v in fp.items() if not k.startswith("_"))
    assert fp["_version"] == "normalized-v2"
    assert int(fp["_tokens_m20"]) > 0
    # Hashes plus token estimates remain logarithmic in message count.
    assert len(fp) <= 20


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


def test_partial_miss_uses_shared_prefix_not_the_new_suffix() -> None:
    previous = fingerprint_prefix(system="s", tools=[], messages=[Message("user", [Text("p" * 680_000)])])
    current = fingerprint_prefix(system="s", tools=[], messages=[
        Message("user", [Text("p" * 680_000)]), Message("assistant", [Text("new")]),
    ], extra_checkpoints=(1,))
    diagnosis = diagnose_cache_miss(previous=previous, current=current,
                                    input_tokens=150_000, cache_read_tokens=20_000)
    assert diagnosis is not None
    assert diagnosis["reuse_shortfall_tokens_estimated"] > 100_000
    assert diagnosis["shared_prefix_tokens_estimated"] >= 170_000
    assert is_unexplained_miss(diagnosis)

    prefix = Message("user", [Text("p" * 80_000)])
    previous = fingerprint_prefix(system="s", tools=[], messages=[prefix])
    current = fingerprint_prefix(system="s", tools=[], messages=[prefix, Message("user", [Text("new" * 200_000)])])
    assert diagnose_cache_miss(previous=previous, current=current,
                               input_tokens=150_000, cache_read_tokens=20_000) is None


def test_complete_fingerprints_notice_tool_names_raw_reasoning_and_image_tail() -> None:
    from fluid.messages import Image, Reasoning
    from fluid.providers.openai_responses.serialize import build_request_body

    variants = [
        (ToolUseBlock("c", "Read", {}), ToolUseBlock("c", "Write", {})),
        (Reasoning(origin="openai_responses", raw={"type": "reasoning", "id": "r", "encrypted_content": "one"}),
         Reasoning(origin="openai_responses", raw={"type": "reasoning", "id": "r", "encrypted_content": "two"})),
        (Image("image/png", "a" * 64 + "b"), Image("image/png", "a" * 64 + "c")),
    ]
    loop = NativeLoop(provider=_MissProvider(0), store=MemoryStore(), model="m")
    for before, after in variants:
        role = "user" if isinstance(before, Image) else "assistant"
        first = ProviderRequest(model="m", messages=[Message(role, [before])])
        second = ProviderRequest(model="m", messages=[Message(role, [after])])
        assert build_request_body(first) != build_request_body(second)
        a = fingerprint_prefix(system=None, tools=[], messages=first.messages)
        b = fingerprint_prefix(system=None, tools=[], messages=second.messages)
        assert a["m1"] != b["m1"]
        loop._fingerprint_request(first)
        a = loop._prefix_fingerprint
        loop._fingerprint_request(second)
        b = loop._prefix_fingerprint
        assert a["_version"] == "responses-wire-v3"
        assert a["_wire_hash"] != b["_wire_hash"]
        assert a["m1"] != b["m1"]


def test_version_and_request_settings_changes_are_not_unexplained_misses() -> None:
    messages = _messages(4)
    first = fingerprint_prefix(system="s", tools=[], messages=messages,
                               request_identity={"provider": "p", "model": "m", "effort": "low"})
    second = fingerprint_prefix(system="s", tools=[], messages=messages,
                                request_identity={"provider": "p", "model": "m", "effort": "high"})
    diagnosis = diagnose_cache_miss(previous=first, current=second,
                                    input_tokens=150_000, cache_read_tokens=20_000)
    assert diagnosis["reason"] == "request_settings_changed"
    assert not is_unexplained_miss(diagnosis)
    diagnosis = diagnose_cache_miss(previous=_fp(), current=second,
                                    input_tokens=150_000, cache_read_tokens=20_000)
    assert diagnosis["reason"] == "fingerprint_version_changed"
    assert not is_unexplained_miss(diagnosis)


async def test_partial_miss_alert_is_once_per_turn_even_after_healthy_call() -> None:
    class IntermittentProvider(_MissProvider):
        async def stream(self, request):
            self.cache_read = 90_000 if self.calls == 3 else 20_000
            async for chunk in super().stream(request):
                yield chunk

    loop = await _run_misses(7, provider=IntermittentProvider(7))
    events = loop.bus.log(loop.session_id)
    assert len([e for e in events if isinstance(e, CacheMissAlert)]) == 1
    usages = [e for e in events if isinstance(e, Usage)]
    assert usages[-1].raw["cache_miss"]["reuse_shortfall_tokens_estimated"] > 0
    assert usages[-1].raw["request_interval_seconds"] >= 0


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
        provider=provider, store=MemoryStore(), model="m", system="sys " * 50_000
    )
    for i in range(2):
        await loop.submit(UserTurn(content=f"turn {i}"))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)

    usages = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]
    assert len(usages) == 2
    for event in usages:
        hashes = event.raw["prefix_hashes"]
        assert list(hashes)[:2] == ["system", "tools"]
        assert all(len(v) == 16 for k, v in hashes.items() if not k.startswith("_"))
        assert hashes["_version"] == "normalized-v2"

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
        provider=provider, store=MemoryStore(), model="m", system="sys " * 50_000
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
        store=store, model="m", system="sys " * 50_000,
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
        store=MemoryStore(), model="m", system="sys " * 50_000,
    )
    await loop.submit(UserTurn(content="hello"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    usage = next(e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage))
    assert usage.raw["cache_miss"]["reason"] == "no_previous_request"


# ─── Acting on the diagnostic ────────────────────────────────────────────────


def _grew(cache_read: int = 0, **over) -> dict:
    """A ``prefix_grew_only`` verdict on a fully verified prefix."""
    base = {
        "reason": "prefix_grew_only",
        "full_coverage": True,
        "shared_messages": 40,
        "verified_messages": 40,
        "input_tokens": 60_000,
        "cache_read_tokens": cache_read,
    }
    base.update(over)
    return base


def test_only_a_verified_zero_read_miss_counts_as_unexplained() -> None:
    assert is_unexplained_miss(_grew())
    assert not is_unexplained_miss(None)
    # A partial read is the provider serving something — not a full miss.
    assert not is_unexplained_miss(_grew(cache_read=512))
    # Checkpoints that stop short cannot rule out a rewrite past them.
    assert not is_unexplained_miss(_grew(full_coverage=False))
    # Fluid rewrote history itself: explained, and named.
    assert not is_unexplained_miss(
        _grew(history_rewritten=[{"tier": 1, "reason": "image_consumed"}])
    )
    assert not is_unexplained_miss({"reason": "prefix_changed", "changed": "system"})
    assert not is_unexplained_miss({"reason": "no_previous_request"})


def test_the_note_drops_breakpoints_on_a_provider_that_has_none() -> None:
    """Every audit chased "a missing cache breakpoint" on openai_responses,
    where there is no breakpoint field to place — the API caches by itself."""
    previous = fingerprint_prefix(system="s", tools=[], messages=_messages(4))
    current = fingerprint_prefix(system="s", tools=[], messages=_messages(6))
    kwargs = dict(
        previous=previous, current=current,
        input_tokens=60_000, cache_read_tokens=0,
    )
    explicit = diagnose_cache_miss(**kwargs, explicit_breakpoints=True)
    automatic = diagnose_cache_miss(**kwargs, explicit_breakpoints=False)

    assert "a missing cache breakpoint" in explicit["note"]
    assert "a missing cache breakpoint" not in automatic["note"]
    assert "there is no breakpoint to place" in automatic["note"]
    assert "caches automatically" in automatic["note"]
    assert automatic["reason"] == explicit["reason"] == "prefix_grew_only"


class _MissProvider:
    """Full misses on every call, and a tool call to keep the turn going."""

    name = "openai_responses"
    caps = ProviderCaps(explicit_cache_breakpoints=False)

    def __init__(self, tool_calls: int, *, cache_read: int = 0) -> None:
        self.tool_calls = tool_calls
        self.cache_read = cache_read
        self.calls = 0
        self.request_extra: list[dict] = []

    async def stream(self, request: ProviderRequest):
        self.request_extra.append(dict(request.extra))
        self.calls += 1
        yield StreamChunk("message_start", {"usage": {
            "input_tokens": 60_000 + self.calls,
            "cache_read_input_tokens": self.cache_read,
        }})
        if self.calls <= self.tool_calls:
            yield StreamChunk("block_start", {
                "index": 0, "block_type": "tool_use",
                "tool_name": "mcp__t__ping", "tool_use_id": f"t{self.calls}",
            })
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


def _ping_registry() -> ToolRegistry:
    @tool("ping", "Ping", {"type": "object", "properties": {}})
    async def ping(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "pong"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("t", [ping]))
    return registry


async def _run_misses(tool_calls: int, **kwargs) -> NativeLoop:
    loop = NativeLoop(
        provider=kwargs.pop("provider", _MissProvider(tool_calls)),
        store=MemoryStore(), model="m", system="sys " * 50_000,
        registry=_ping_registry(), permissions=PermissionEngine("never_ask"),
        **kwargs,
    )
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    return loop


async def test_two_unexplained_misses_in_one_turn_raise_an_alert() -> None:
    """Ten consecutive full-price calls went by with nothing in the transcript
    saying the cache had stopped working. The second one now says so."""
    loop = await _run_misses(3)
    events = loop.bus.log(loop.session_id)
    alerts = [e for e in events if isinstance(e, CacheMissAlert)]

    assert len(alerts) == 1, "one alert per run, not one per call"
    alert = alerts[0]
    assert alert.consecutive == 2
    assert alert.reason == "prefix_grew_only"
    assert alert.provider == "openai_responses"
    assert alert.cache_read_tokens == 0
    # The key the request actually carried, so a reader can tell a routing
    # problem from a partitioning one.
    assert alert.cache_key == loop.session_id
    # The streak rides the diagnostic too, for whoever reads Usage rows later.
    streaks = [
        e.raw["cache_miss"].get("consecutive_unexplained")
        for e in events if isinstance(e, Usage) and "cache_miss" in e.raw
    ]
    # Call 1 has no previous request to compare against, so it is explained;
    # the streak runs over the three calls after it.
    assert streaks == [None, 1, 2, 3]


async def test_the_alert_stays_quiet_when_the_cache_is_working() -> None:
    loop = await _run_misses(3, provider=_MissProvider(3, cache_read=90_000))
    assert not [e for e in loop.bus.log(loop.session_id)
                if isinstance(e, CacheMissAlert)]


async def test_the_alert_threshold_is_configurable_and_can_be_off() -> None:
    off = await _run_misses(3, cache_probe=CacheProbeConfig(alert_after_misses=0))
    assert not [e for e in off.bus.log(off.session_id)
                if isinstance(e, CacheMissAlert)]

    late = await _run_misses(5, cache_probe=CacheProbeConfig(alert_after_misses=4))
    alerts = [e for e in late.bus.log(late.session_id)
              if isinstance(e, CacheMissAlert)]
    assert [a.consecutive for a in alerts] == [4]


async def test_the_alert_reaches_an_optional_host_hook() -> None:
    seen: list[dict] = []

    async def spy(event: str, payload: dict):
        seen.append(payload)
        return None

    hooks = HookBus()
    hooks.on("CacheMiss", spy)
    await _run_misses(3, hooks=hooks)

    assert len(seen) == 1
    assert seen[0]["consecutive"] == 2
    assert seen[0]["provider"] == "openai_responses"
    assert seen[0]["reason"] == "prefix_grew_only"
    assert seen[0]["note"]


async def test_no_hook_wired_is_not_an_error() -> None:
    """Safe default: the alert is emitted, nothing listens, the turn ends."""
    loop = await _run_misses(3)
    assert loop.bus.log(loop.session_id)[-1].__class__.__name__ == "TurnComplete"


async def test_an_unexplained_miss_logs_the_usage_block_and_the_cache_key(
    caplog,
) -> None:
    """Provider routing and a missing breakpoint look identical in the
    diagnostic alone; what tells them apart is what the provider reported and
    which affinity key the request carried."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="fluid.engine.native"):
        loop = await _run_misses(1)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "cache miss" in r.getMessage()]

    lines = [r.getMessage() for r in caplog.records if "cache miss" in r.getMessage()]
    assert lines, "an unexplained miss must be logged"
    detailed = lines[-1]
    assert "provider=openai_responses" in detailed
    assert f"cache_key={loop.session_id}" in detailed
    assert "provider_usage=" in detailed
    assert "'cache_read_input_tokens': 0" in detailed
    assert "'input_tokens': 60" in detailed


# ─── Cache-key durability ────────────────────────────────────────────────────


async def test_the_affinity_key_defaults_to_the_fluid_session_id() -> None:
    provider = _MissProvider(0)
    loop = await _run_misses(0, provider=provider)
    assert provider.request_extra[0]["prompt_cache_key"] == loop.session_id


async def test_a_host_supplied_cache_key_survives_a_new_fluid_session() -> None:
    """A resumed conversation lands on a new fluid session id, which used to
    repartition the provider cache at the moment the prefix is longest. A host
    with its own durable conversation id keeps the partition."""
    first = _MissProvider(0)
    second = _MissProvider(0)
    leg_one = await _run_misses(0, provider=first, cache_key="conv-42")
    leg_two = await _run_misses(0, provider=second, cache_key="conv-42")

    assert leg_one.session_id != leg_two.session_id
    assert first.request_extra[0]["prompt_cache_key"] == "conv-42"
    assert second.request_extra[0]["prompt_cache_key"] == "conv-42"


async def test_the_openrouter_key_takes_the_same_override() -> None:
    class _Router(_MissProvider):
        name = "openrouter"
        caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    provider = _Router(0)
    await _run_misses(0, provider=provider, cache_key="conv-7")
    assert provider.request_extra[0] == {"session_id": "conv-7"}


async def test_an_empty_cache_key_falls_back_to_the_session_id() -> None:
    provider = _MissProvider(0)
    loop = await _run_misses(0, provider=provider, cache_key="")
    assert provider.request_extra[0]["prompt_cache_key"] == loop.session_id


async def test_the_streak_does_not_carry_across_turns() -> None:
    """A cache that expired over user think-time is ordinary, so the run is
    only counted inside one turn."""
    provider = _MissProvider(0)
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", system="sys " * 50_000,
        registry=_ping_registry(), permissions=PermissionEngine("never_ask"),
    )
    for i in range(4):
        await loop.submit(UserTurn(content=f"turn {i}"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)

    events = loop.bus.log(loop.session_id)
    assert not [e for e in events if isinstance(e, CacheMissAlert)]
    streaks = [
        e.raw["cache_miss"].get("consecutive_unexplained")
        for e in events if isinstance(e, Usage) and "cache_miss" in e.raw
    ]
    assert streaks == [None, 1, 1, 1]


def test_the_alert_round_trips_through_the_durable_codec() -> None:
    from fluid.events.model import event_from_dict, event_to_dict

    event = CacheMissAlert(
        session_id="s", turn_id="t", consecutive=2, reason="prefix_grew_only",
        input_tokens=94_000, cache_read_tokens=0,
        provider="openai_responses", model="gpt-6-astra",
        cache_key="conv-42", note="every shared checkpoint matched",
    )
    assert event_from_dict(event_to_dict(event)) == event
    assert event.type == "cache_miss_alert"


def test_the_cache_key_and_image_ratio_are_configurable() -> None:
    from fluid.config.model import ContextConfig, HarnessConfig

    assert ContextConfig().image_trigger_ratio == 1.0
    assert (
        ContextConfig.from_dict({"image_trigger_ratio": 0.5}).image_trigger_ratio
        == 0.5
    )
    assert HarnessConfig().cache_key is None
    loaded = HarnessConfig.from_dict({"cache_key": "conv-42"})
    assert loaded.cache_key == "conv-42"
    assert loaded.to_dict()["cache_key"] == "conv-42"
    # Blank is "unset", not an empty affinity key on every request.
    assert HarnessConfig.from_dict({"cache_key": ""}).cache_key is None


# ─── Routing headers in the request identity ─────────────────────────────────


class _HeaderMissProvider(_MissProvider):
    """Full misses, and routing headers the test can change between calls."""

    def __init__(self, tool_calls: int, *, switch_after: int) -> None:
        super().__init__(tool_calls)
        self.switch_after = switch_after

    def routing_headers(self, request=None) -> dict[str, str]:
        account = "acct-a" if self.calls < self.switch_after else "acct-b"
        return {"chatgpt-account-id": account, "originator": "codex_cli_rs"}


async def test_a_header_only_change_is_a_request_identity_change() -> None:
    """The probe hashed the body only, so a call whose routing header moved
    read as an unchanged prefix and was blamed on the provider. The header is
    now part of the request identity and the diagnosis says it moved."""
    loop = await _run_misses(3, provider=_HeaderMissProvider(3, switch_after=2))
    usages = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]
    diagnoses = [u.raw["cache_miss"] for u in usages]

    assert [d["reason"] for d in diagnoses] == [
        "no_previous_request",
        "prefix_grew_only",
        # Call 3 is the first with the new account header; its body is the
        # previous body plus one tool round, the same growth as on call 2.
        "request_settings_changed",
        "prefix_grew_only",
    ]
    assert diagnoses[2]["routing_headers_changed"] is True
    assert diagnoses[2]["other_settings_changed"] is False
    assert diagnoses[2]["note"] == "the routing headers changed"
    assert not is_unexplained_miss(diagnoses[2])


def test_the_normalized_fingerprint_hashes_routing_headers() -> None:
    messages = _messages(4)
    identity = {"provider": "p", "model": "m"}
    before = fingerprint_prefix(system="s", tools=[], messages=messages,
                                request_identity=identity,
                                routing_headers={"session-id": "one"})
    after = fingerprint_prefix(system="s", tools=[], messages=messages,
                               request_identity=identity,
                               routing_headers={"Session-Id": "two"})
    same = fingerprint_prefix(system="s", tools=[], messages=messages,
                              request_identity=identity,
                              routing_headers={"SESSION-ID": "one"})
    assert before["request"] != after["request"]
    assert before["request"] == same["request"], "header names are case-insensitive"
    diagnosis = diagnose_cache_miss(previous=before, current=after,
                                    input_tokens=150_000, cache_read_tokens=0)
    assert diagnosis["reason"] == "request_settings_changed"
    assert diagnosis["routing_headers_changed"] is True

    # A settings change with the same headers is not a header change.
    effort = fingerprint_prefix(system="s", tools=[], messages=messages,
                                request_identity={**identity, "effort": "high"},
                                routing_headers={"session-id": "one"})
    diagnosis = diagnose_cache_miss(previous=before, current=effort,
                                    input_tokens=150_000, cache_read_tokens=0)
    assert diagnosis["routing_headers_changed"] is False

    # No headers leaves the identity as it was before headers were hashed, so
    # fingerprints persisted by an older release still compare equal.
    bare = fingerprint_prefix(system="s", tools=[], messages=messages,
                              request_identity=identity)
    empty = fingerprint_prefix(system="s", tools=[], messages=messages,
                               request_identity=identity, routing_headers={})
    assert bare == empty
    assert "_routing_headers" not in bare


def test_headers_that_move_with_a_settings_switch_are_reported_with_it() -> None:
    """Switching a session from the Codex backend to the public API changes
    the endpoint and drops the Codex headers in the same request. That is a
    settings switch, and the diagnosis says the headers were not all that
    moved, so the host does not file it as an unannounced routing change."""
    from fluid.context.cache_probe import fingerprint_responses_request

    request = ProviderRequest(model="m", messages=_messages(4))
    codex = {"chatgpt-account-id": "acct-1", "originator": "codex_cli_rs"}

    def responses(endpoint: str, headers: dict) -> dict:
        fp = fingerprint_prefix(system=None, tools=[], messages=request.messages,
                                routing_headers=headers)
        fingerprint_responses_request(request, fp, previous_count=0,
                                      endpoint=endpoint, routing_headers=headers)
        return fp

    before = responses("https://chatgpt.com/backend-api/codex/responses", codex)
    switched = responses("https://api.openai.com/v1/responses", {})
    moved = responses("https://chatgpt.com/backend-api/codex/responses",
                      {**codex, "chatgpt-account-id": "acct-2"})

    diagnosis = diagnose_cache_miss(previous=before, current=switched,
                                    input_tokens=150_000, cache_read_tokens=0)
    assert diagnosis["reason"] == "request_settings_changed"
    assert diagnosis["routing_headers_changed"] is True
    assert diagnosis["other_settings_changed"] is True
    assert "and the routing headers changed" in diagnosis["note"]

    diagnosis = diagnose_cache_miss(previous=before, current=moved,
                                    input_tokens=150_000, cache_read_tokens=0)
    assert diagnosis["routing_headers_changed"] is True
    assert diagnosis["other_settings_changed"] is False

    # The normalized fingerprint makes the same distinction, including when
    # one side sent headers and the other sent none.
    identity = {"provider": "p", "model": "m"}
    with_headers = fingerprint_prefix(system="s", tools=[], messages=request.messages,
                                      request_identity=identity, routing_headers=codex)
    bare = fingerprint_prefix(system="s", tools=[], messages=request.messages,
                              request_identity=identity)
    other_model = fingerprint_prefix(system="s", tools=[], messages=request.messages,
                                     request_identity={**identity, "model": "n"})
    diagnosis = diagnose_cache_miss(previous=with_headers, current=bare,
                                    input_tokens=150_000, cache_read_tokens=0)
    assert diagnosis["other_settings_changed"] is False
    diagnosis = diagnose_cache_miss(previous=with_headers, current=other_model,
                                    input_tokens=150_000, cache_read_tokens=0)
    assert diagnosis["routing_headers_changed"] is True
    assert diagnosis["other_settings_changed"] is True


async def test_the_responses_client_routing_headers_are_the_ones_it_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid.providers.openai_responses.client import (
        CODEX_RESPONSES_URL,
        ROUTING_ARM_ENV,
        ROUTING_HINT_ENV,
        OpenAIResponsesClient,
    )

    monkeypatch.delenv(ROUTING_ARM_ENV, raising=False)
    monkeypatch.delenv(ROUTING_HINT_ENV, raising=False)

    class _Auth:
        account_id = "acct-1"

        async def get_access_token(self) -> str:
            return "secret-token"

        async def on_401(self) -> None:
            return None

    client = OpenAIResponsesClient(auth=_Auth(), base_url=CODEX_RESPONSES_URL)
    routing = client.routing_headers()
    assert routing == {"chatgpt-account-id": "acct-1",
                       "OpenAI-Beta": "responses=experimental",
                       "originator": "codex_cli_rs"}
    sent = await client._headers()
    assert {k: sent[k] for k in routing} == routing
    assert "Authorization" not in routing, "credentials never enter the probe"
    # The public API sends no routing headers today.
    assert OpenAIResponsesClient(api_key="k").routing_headers() == {}


# ─── Miss classes on the event log ───────────────────────────────────────────


async def test_the_alert_and_the_usage_rows_carry_the_miss_class() -> None:
    # Design 5.1 reads CacheMissAlert.class from the event log. A streak of
    # matching-prefix misses inside the cache lifetime is a provider fallback.
    loop = await _run_misses(3)
    events = loop.bus.log(loop.session_id)
    (alert,) = [e for e in events if isinstance(e, CacheMissAlert)]
    assert alert.miss_class == "provider_fallback"
    classes = [e.raw.get("cache_miss_class") for e in events if isinstance(e, Usage)]
    # Call 1 is the session's first request: a planned rebuild.
    assert classes == ["planned_rebuild"] + ["provider_fallback"] * 3


class _AnthropicWriteProvider(_MissProvider):
    """Anthropic accounting: a rewritten prefix is billed as a cache write and
    input_tokens holds only the few tokens after the last breakpoint."""

    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True)

    def __init__(self, tool_calls: int) -> None:
        super().__init__(tool_calls)
        self.loop: NativeLoop | None = None

    async def stream(self, request: ProviderRequest):
        if self.calls == 0 and self.loop is not None:
            # The harness rewrites its own system prompt between two calls.
            self.loop.system = "changed " * 50_000
        async for chunk in super().stream(request):
            if chunk.kind == "message_start":
                chunk = StreamChunk("message_start", {"usage": {
                    "input_tokens": 2_000,
                    "cache_creation_input_tokens": 58_000,
                    "cache_read_input_tokens": 0,
                }})
            yield chunk


async def test_an_anthropic_prefix_mutation_is_classified_as_a_mutation() -> None:
    # Before, the probe's 10k fresh-input gate read only input_tokens (2k
    # here), so the rewrite never got a diagnosis and was filed as
    # append_growth.
    provider = _AnthropicWriteProvider(1)
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", system="sys " * 50_000,
        registry=_ping_registry(), permissions=PermissionEngine("never_ask"),
    )
    provider.loop = loop
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    usages = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]
    second = usages[1].raw
    assert second["cache_miss"]["reason"] == "prefix_changed"
    assert second["cache_miss"]["changed"] == "system"
    assert second["cache_miss_class"] == "harness_mutation"


# ─── OpenAI routing arm and previous-prompt coverage ─────────────────────────


class _CodexAuth:
    account_id = "acct-1"

    async def get_access_token(self) -> str:
        return "secret-token"

    async def on_401(self) -> None:
        return None


def _codex_client(arm: str | None = None):
    from fluid.providers.openai_responses.client import (
        CODEX_RESPONSES_URL,
        OpenAIResponsesClient,
    )

    return OpenAIResponsesClient(
        auth=_CodexAuth(), base_url=CODEX_RESPONSES_URL, routing_arm=arm,
    )


class _CodexRoutedProvider(_MissProvider):
    """Full misses, with the routing arm and headers of a real Codex-backend
    Responses client. ``switch_after`` calls in, a second client takes over
    (a worker restarted onto a new arm)."""

    def __init__(
        self, tool_calls: int, *, arm: str | None = None,
        then: str | None = None, switch_after: int = 0,
    ) -> None:
        super().__init__(tool_calls)
        self.first = _codex_client(arm)
        self.second = _codex_client(then) if then is not None else self.first
        self.switch_after = switch_after

    def _current(self):
        if self.switch_after and self.calls >= self.switch_after:
            return self.second
        return self.first

    @property
    def routing_arm(self) -> str:
        return self._current().routing_arm

    def routing_headers(self, request=None) -> dict[str, str]:
        return self._current().routing_headers(request)


@pytest.fixture
def _no_routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid.providers.openai_responses.client import ROUTING_ARM_ENV, ROUTING_HINT_ENV

    monkeypatch.delenv(ROUTING_ARM_ENV, raising=False)
    monkeypatch.delenv(ROUTING_HINT_ENV, raising=False)


@pytest.mark.usefixtures("_no_routing_env")
async def test_the_default_arm_gives_each_session_its_own_route_hash() -> None:
    first = await _run_misses(2, provider=_CodexRoutedProvider(2))
    second = await _run_misses(2, provider=_CodexRoutedProvider(2))

    def hashes(loop: NativeLoop) -> list[dict]:
        return [e.raw["prefix_hashes"] for e in loop.bus.log(loop.session_id)
                if isinstance(e, Usage)]

    one, two = hashes(first), hashes(second)
    assert {h["_routing_arm"] for h in one + two} == {"B"}
    assert len({h["_routing_headers"] for h in one}) == 1, "constant within a session"
    assert len({h["_routing_headers"] for h in two}) == 1
    assert one[0]["_routing_headers"] != two[0]["_routing_headers"]

    diagnoses = [e.raw["cache_miss"] for e in first.bus.log(first.session_id)
                 if isinstance(e, Usage)]
    assert [d["reason"] for d in diagnoses] == [
        "no_previous_request", "prefix_grew_only", "prefix_grew_only",
    ]
    assert not any(d.get("routing_headers_changed") for d in diagnoses)


@pytest.mark.usefixtures("_no_routing_env")
async def test_an_arm_change_is_one_routing_headers_change() -> None:
    provider = _CodexRoutedProvider(3, arm="A", then="B", switch_after=2)
    loop = await _run_misses(3, provider=provider)
    usages = [e for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]
    diagnoses = [u.raw["cache_miss"] for u in usages]

    assert [u.raw["prefix_hashes"]["_routing_arm"] for u in usages] == ["A", "A", "B", "B"]
    assert [d["reason"] for d in diagnoses] == [
        "no_previous_request",
        "prefix_grew_only",
        "request_settings_changed",
        "prefix_grew_only",
    ]
    assert [bool(d.get("routing_headers_changed")) for d in diagnoses] == [
        False, False, True, False,
    ]


def test_the_routing_arm_key_never_marks_a_miss() -> None:
    previous = _fp(_routing_arm="A")
    current = _fp(_routing_arm="B")
    diagnosis = diagnose_cache_miss(previous=previous, current=current,
                                    input_tokens=150_000, cache_read_tokens=0)
    assert diagnosis["reason"] == "prefix_unchanged"


class _CoverageProvider(_MissProvider):
    """Scripted prompt usage per call, on top of the miss provider's turn."""

    def __init__(self, usages: list[dict]) -> None:
        super().__init__(len(usages) - 1)
        self.usages = usages

    async def stream(self, request: ProviderRequest):
        index = self.calls
        async for chunk in super().stream(request):
            if chunk.kind == "message_start":
                chunk = StreamChunk("message_start", {"usage": self.usages[index]})
            yield chunk


async def test_prev_prompt_coverage_is_the_share_of_the_last_prompt_read() -> None:
    provider = _CoverageProvider([
        {"input_tokens": 60_000, "cache_read_input_tokens": 0},
        {"input_tokens": 1_000, "cache_read_input_tokens": 30_000},
        # More than the previous prompt read back: capped at 1.0.
        {"input_tokens": 500, "cache_read_input_tokens": 40_000},
    ])
    loop = await _run_misses(0, provider=provider)
    raws = [e.raw for e in loop.bus.log(loop.session_id) if isinstance(e, Usage)]

    assert "prev_prompt_coverage" not in raws[0], "nothing to compare on the first call"
    assert raws[1]["prev_prompt_coverage"] == 0.5
    assert raws[2]["prev_prompt_coverage"] == 1.0


async def test_prev_prompt_coverage_starts_over_with_a_new_session() -> None:
    first = await _run_misses(0, provider=_CoverageProvider([
        {"input_tokens": 60_000, "cache_read_input_tokens": 0},
        {"input_tokens": 1_000, "cache_read_input_tokens": 60_000},
    ]))
    assert [e.raw.get("prev_prompt_coverage") for e in first.bus.log(first.session_id)
            if isinstance(e, Usage)] == [None, 1.0]

    fresh = await _run_misses(0, provider=_CoverageProvider([
        {"input_tokens": 70_000, "cache_read_input_tokens": 0},
    ]))
    (usage,) = [e for e in fresh.bus.log(fresh.session_id) if isinstance(e, Usage)]
    assert "prev_prompt_coverage" not in usage.raw


async def test_prev_prompt_coverage_survives_a_rebuilt_engine() -> None:
    store = MemoryStore()
    first = NativeLoop(
        provider=_CoverageProvider([
            {"input_tokens": 40_000, "cache_read_input_tokens": 0},
        ]),
        store=store, model="m", system="sys " * 50_000,
        registry=_ping_registry(), permissions=PermissionEngine("never_ask"),
    )
    await first.submit(UserTurn(content="one"))
    await asyncio.wait_for(first.wait_turn(), timeout=10)
    sid = first.session_id

    second = NativeLoop(
        provider=_CoverageProvider([
            {"input_tokens": 2_000, "cache_read_input_tokens": 30_000},
        ]),
        store=store, model="m", system="sys " * 50_000, session_id=sid,
        registry=_ping_registry(), permissions=PermissionEngine("never_ask"),
    )
    await second.submit(UserTurn(content="two"))
    await asyncio.wait_for(second.wait_turn(), timeout=10)
    usage = [e for e in second.bus.log(sid) if isinstance(e, Usage)][-1]
    assert usage.raw["prev_prompt_coverage"] == 0.75
