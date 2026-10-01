"""Hierarchical, recoverable compaction: the mechanical eviction ladder.

Pins the invariants of fluid/context/compaction.py — deterministic extractive
digests, batched+sticky promotions, byte-identical renders between batches,
fork safety, event-replay rehydration, and the stateless intake stub policy.
The stored tree is never mutated; every tier is a view transform.
"""

import asyncio
from collections.abc import AsyncIterator

from fluid.context.compaction import (
    CompactionConfig,
    Compactor,
    is_turn_start,
    render_span_digest,
    split_spans,
    strip_system_reminders,
)
from fluid.context.trim import TrimConfig
from fluid.engine.native import NativeLoop, repair_dangling_tool_calls
from fluid.events import ContextCompaction, UserTurn
from fluid.events.model import event_from_dict, event_to_dict
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import SessionNode
from fluid.store.jsonl import JsonlStore


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _turn(t: int, *, result_chars: int = 4_000, error: bool = False) -> list[SessionNode]:
    """One complete turn: user → assistant(tool_use) → result → conclusion."""
    return [
        _node(f"node_u{t}", "user", [Text(f"user asks about topic-{t} " + "pad " * 100)]),
        _node(f"node_a{t}", "assistant", [
            Text(f"let me check {t}"),
            ToolUseBlock(id=f"call{t}", name="Read", input={"file_path": f"f{t}.py"}),
        ]),
        _node(f"node_r{t}", "user", [
            ToolResultBlock(
                tool_use_id=f"call{t}",
                content=[Text(f"secret-payload-{t} " + "x" * result_chars)],
                is_error=error,
            )
        ]),
        _node(f"node_f{t}", "assistant", [Text(f"conclusion: topic-{t} resolved")]),
    ]


def _history(turns: int, **kw) -> list[SessionNode]:
    nodes: list[SessionNode] = []
    for t in range(turns):
        nodes.extend(_turn(t, **kw))
    return nodes


def _cfg(**kw) -> CompactionConfig:
    base = dict(
        trigger_tokens=1_000,
        target_ratio=0.5,
        keep_recent_turns=2,
        trim=TrimConfig(trigger_tokens=1_000, keep_recent_tool_results=2,
                        min_result_chars=500),
    )
    base.update(kw)
    return CompactionConfig(**base)


def _texts(messages: list[Message]) -> list[str]:
    return [
        "".join(getattr(b, "text", "") or "".join(
            getattr(c, "text", "") for c in (getattr(b, "content", None) or [])
        ) for b in m.content)
        for m in messages
    ]


def _growing_turn(calls: int, *, metadata: bool = True) -> list[SessionNode]:
    nodes = [_node("start", "user", [Text("Never change production credentials.")])]
    for i in range(calls):
        nodes.extend([
            _node(f"a{i}", "assistant", [ToolUseBlock(id=f"c{i}", name="Edit", input={"text": "a" * 18_000})]),
            _node(f"r{i}", "user", [ToolResultBlock(
                tool_use_id=f"c{i}", content=[Text("r" * 2_200)],
                delivery_metadata_version=int(metadata),
            )]),
        ])
    return nodes


def test_growing_protected_turn_has_no_tiny_optional_rewrites() -> None:
    # Replay the observed 50-call argument-heavy turn, including legacy data
    # whose lack of typed delivery metadata rules out within-turn digestion.
    #
    # Updated for plan item 4 (B1). The old version passed partly because the
    # calls below the trigger armed the retry watermark, so the first real
    # crossing (call 24) waited until call 31. Those calls now report
    # below_threshold and arm nothing, and the first batch lands on the first
    # call over the trigger. What this test pins is unchanged: every batch
    # clears the 5% floor, batches stay rare, and nothing is digested.
    c = Compactor()
    batches = []
    reasons = []
    for count in range(1, 51):
        _, promotions = c.apply(_growing_turn(count, metadata=False), turn_boundary=False)
        reasons.append(c.last_decision["reason"])
        if promotions:
            first = promotions[0]
            assert first.before_tokens - first.after_tokens >= int(first.before_tokens * 0.05)
            assert first.first_changed_message is not None
            batches.append(promotions)
    assert batches
    assert len(batches) < 19
    assert not c._digested  # No unsafe fallback to parsing tool text.
    first_over = next(i for i, r in enumerate(reasons) if r != "below_threshold")
    assert reasons[first_over] == "meaningful_reclaim"
    assert "awaiting_reclaimable_growth" not in reasons[:first_over]


def test_below_threshold_is_labelled_and_arms_no_watermark() -> None:
    # Plan item 4 (B1): a call at or below the trigger builds no batch. It
    # used to be labelled insufficient_reclaim and arm the retry watermark,
    # which made later mid-turn calls skip the threshold check.
    nodes = _history(3)
    c = Compactor(config=_cfg(trigger_tokens=50_000))
    rendered, promotions = c.apply(nodes, turn_boundary=False)
    assert not promotions and rendered == c.view(nodes)
    assert c.last_decision["reason"] == "below_threshold"
    assert c.last_decision["threshold_tokens"] == 50_000
    assert c._optional_retry is None


def test_later_crossing_fires_at_once() -> None:
    # Mid-turn calls below the trigger, then one call over it. Here the
    # overhead grows (a larger system prompt or tool list), so the history
    # and its reclaimable chars stay the same. The crossing builds its batch
    # on that call. With the legacy arming the same call waited for
    # reclaimable growth that never came.
    from fluid.context.trim import estimate_tokens_of_messages

    nodes = _history(6)
    size = estimate_tokens_of_messages(Compactor(config=_cfg()).view(nodes))
    trigger = size + 1_000

    def run(skips: bool) -> tuple[Compactor, list]:
        c = Compactor(config=_cfg(trigger_tokens=trigger,
                                  below_threshold_skips_retry=skips))
        for i in range(3):
            c.apply(nodes, turn_boundary=False)
            # Legacy: the first below-threshold call arms the watermark and
            # every later mid-turn call waits on it without a threshold check.
            expected = ("below_threshold" if skips or i == 0
                        else "awaiting_reclaimable_growth")
            assert c.last_decision["reason"] == expected
        _, promotions = c.apply(nodes, overhead_tokens=2_000, turn_boundary=False)
        return c, promotions

    fixed, promotions = run(True)
    assert promotions
    assert fixed.last_decision["reason"] == "meaningful_reclaim"
    assert fixed.last_decision["threshold_tokens"] == trigger
    legacy, promotions = run(False)
    assert not promotions
    assert legacy.last_decision["reason"] == "awaiting_reclaimable_growth"


def test_empty_candidate_over_the_trigger_arms_no_watermark() -> None:
    # Over the trigger with nothing eligible: the candidate equals the
    # current view, so there is no growth worth waiting for.
    nodes = _history(1)
    c = Compactor(config=_cfg())
    rendered, promotions = c.apply(nodes, overhead_tokens=10_000, turn_boundary=False)
    assert not promotions and rendered == c.view(nodes)
    assert c.last_decision["reason"] == "insufficient_reclaim"
    assert c.last_decision["candidate_after_tokens"] == c.last_decision["before_tokens"]
    assert c._optional_retry is None


def test_trim_only_keeps_the_legacy_watermark() -> None:
    # The trim-only contract stays byte-identical: the decision is relabelled
    # but the watermark is armed exactly as before.
    nodes = _history(3)
    c = Compactor(config=CompactionConfig(enabled=False, trim=TrimConfig(
        enabled=True, trigger_tokens=50_000)))
    c.apply(nodes, turn_boundary=False)
    assert c.last_decision["reason"] == "below_threshold"
    assert c.last_decision["threshold_tokens"] == 50_000
    assert c._optional_retry is not None


def test_optional_rejection_rolls_back_and_window_protection_keeps_the_floor() -> None:
    from fluid.context.trim import estimate_tokens_of_messages

    nodes = _growing_turn(50, metadata=False)
    # Plan item 2 replaced keep_recent_tool_results with a token tail when the
    # window is known, and waived the floor at 90% of the window, where this
    # window sits. Both are off here so the 5% floor itself stays pinned;
    # test_window_protection_floor_is_waived_near_the_window covers the
    # waiver.
    c = Compactor(config=CompactionConfig(
        trim=TrimConfig(keep_recent_tool_results=49), **_ITEM2_OFF))
    original = c.view(nodes)
    before = estimate_tokens_of_messages(original)
    rendered, promotions = c.apply(nodes, turn_boundary=False)
    assert not promotions and rendered == original
    assert c.last_decision["reason"] == "insufficient_reclaim"
    assert not c.trimmer._trimmed
    c.apply(nodes, turn_boundary=False)
    assert c.last_decision["reason"] == "awaiting_reclaimable_growth"
    rendered, promotions = c.apply(
        nodes, turn_boundary=False,
        context_window=before + c.config.headroom_tokens + 4_000 - 1,
        output_reserve_tokens=4_000,
    )
    # Window protection skips the retry watermark and builds a candidate, but
    # a sub-5% reclaim is still a cache break for almost nothing: rejected.
    assert not promotions and rendered == original
    decision = c.last_decision
    assert decision["reason"] == "insufficient_reclaim"
    assert decision["window_protection"] is True
    assert 0 < before - decision["candidate_after_tokens"] < before * 0.05
    assert not c.trimmer._trimmed


def test_rejected_optional_image_candidate_restores_consumed_state() -> None:
    c = Compactor(config=_armed())
    nodes = _image_history()
    original = c.view(nodes)
    rendered, promotions = c.apply(nodes, overhead_tokens=100_000)
    assert rendered == original and not promotions
    assert not c.trimmer._image_consumed
    assert not c.trimmer._trimmed


def test_digest_expansion_is_rejected_using_actual_rendered_view() -> None:
    c = Compactor(config=_cfg(keep_recent_turns=0, trim=TrimConfig(enabled=False)))
    nodes = [_node("u", "user", [Text("hi")])]
    assert c._digest_until(nodes, overhead_tokens=0, target=0) == []
    assert not c._digested
    assert c.view(nodes) == [nodes[0].message]


def test_long_turn_segments_preserve_pairing_steers_and_replay() -> None:
    from fluid.context.compaction import render_epoch_stub
    from fluid.messages.content import SteerDelivery

    nodes = _growing_turn(50)
    steer = SteerDelivery(text="Do not deploy.\n" + "retain this constraint " * 100, id="real-user")
    block = nodes[2].message.content[0]
    block.delivered_steers = [steer]
    block.content.append(Text(steer.render()))
    original = [n.message.to_dict() for n in nodes]
    c = Compactor()
    rendered, promotions = c.apply(nodes, turn_boundary=False)
    assert any(p.tier == 2 for p in promotions)
    assert rendered[0] == nodes[0].message
    assert steer.render() in "\n".join(_texts(rendered))
    assert repair_dangling_tool_calls(rendered) is rendered
    assert [n.message.to_dict() for n in nodes] == original
    replayed_nodes = [SessionNode(node_id=n.node_id, parent_id=n.parent_id,
                                 message=Message.from_dict(n.message.to_dict())) for n in nodes]
    resumed = Compactor()
    resumed.rehydrate(promotions)
    assert resumed.view(replayed_nodes) == rendered
    # A fork before the promoted end cannot inherit an off-path segment.
    fork = replayed_nodes[:10]
    fork_trims = Compactor()
    fork_trims.rehydrate([p for p in promotions if p.tier == 1])
    assert resumed.view(fork) == fork_trims.view(fork)
    assert "compacted turn" not in "\n".join(_texts(resumed.view(fork)))
    assert steer.render() in "\n".join(_texts([render_epoch_stub([nodes], c.config)]))
    assert nodes[-2].message in rendered and nodes[-1].message in rendered


def test_segments_do_not_cross_errors_unpaired_calls_or_untyped_results() -> None:
    from fluid.context.compaction import completed_segments

    assert not completed_segments(_growing_turn(16, metadata=False))
    nodes = _growing_turn(16)
    nodes[2].message.content[0].is_error = True
    segments = completed_segments(nodes)
    assert len(segments) == 1
    assert nodes[2] not in segments[0]
    nodes = _growing_turn(16)
    nodes.pop(2)  # First call remains unresolved; no later group may hide it.
    assert not completed_segments(nodes)


# ─── Span derivation ─────────────────────────────────────────────────────────


def test_span_split_turn_boundaries() -> None:
    nodes = _history(3)
    spans = split_spans(nodes)
    assert [len(s) for s in spans] == [4, 4, 4]
    # Tool-result-only user nodes are NOT turn starts.
    assert not is_turn_start(nodes[2])
    # Real user turns are, even with a system-reminder Text appended.
    assert is_turn_start(_node("n", "user", [Text("hi"), Text("<system-reminder>x</system-reminder>")]))


def test_span_split_interrupted_turn() -> None:
    # A turn that died after the tool call still forms a complete span.
    nodes = _history(1)[:2] + [_node("node_u9", "user", [Text("next")])]
    spans = split_spans(nodes)
    assert [len(s) for s in spans] == [2, 1]


# ─── Digest rendering ────────────────────────────────────────────────────────


def test_digest_render_deterministic() -> None:
    span = _turn(0)
    cfg = _cfg()
    a = render_span_digest(span, cfg).content[0].text
    b = render_span_digest(span, cfg).content[0].text
    assert a == b


def test_digest_carries_refs_and_extracts() -> None:
    text = render_span_digest(_turn(0), _cfg()).content[0].text
    assert "node_u0..node_f0" in text          # span ref for HistoryExpand
    assert "HistoryExpand" in text and "HistorySearch" in text
    assert "user asks about topic-0" in text   # user text near-verbatim
    assert "conclusion: topic-0 resolved" in text
    assert "Read" in text                      # tool traffic line


def test_digest_preserves_error_text_verbatim() -> None:
    text = render_span_digest(_turn(0, error=True, result_chars=100), _cfg()).content[0].text
    assert "secret-payload-0" in text          # error text kept
    assert "✗ error" in text


def test_digest_strips_system_reminder_blocks() -> None:
    assert strip_system_reminders("keep <system-reminder>drop\nme</system-reminder> this") == "keep  this"
    span = [_node("node_u0", "user", [
        Text("real question"),
        Text("<system-reminder>ledger copy</system-reminder>"),
    ])]
    text = render_span_digest(span, _cfg()).content[0].text
    assert "real question" in text and "ledger copy" not in text


# ─── Promotion ───────────────────────────────────────────────────────────────


def test_tier2_batch_reclaims_and_keeps_recent_turns() -> None:
    nodes = _history(6)
    c = Compactor(config=_cfg())
    rendered, promos = c.apply(nodes)
    tiers = [p.tier for p in promos]
    assert 2 in tiers
    joined = "\n".join(_texts(rendered))
    # Old turns digested, newest keep_recent_turns=2 verbatim.
    assert "compacted turn node_u0" in joined
    assert "user asks about topic-5" in joined and "compacted turn node_u5" not in joined
    assert "user asks about topic-4" in joined and "compacted turn node_u4" not in joined


def test_no_dangling_calls_after_digest() -> None:
    nodes = _history(6)
    c = Compactor(config=_cfg())
    rendered, _ = c.apply(nodes)
    # Digests replace whole turns (tool_use + result together), so repair
    # finds nothing to fix and returns the same list object.
    assert repair_dangling_tool_calls(rendered) is rendered


def test_render_byte_identical_between_batches() -> None:
    nodes = _history(6)
    c = Compactor(config=_cfg())
    first, promos = c.apply(nodes)
    assert promos
    second, promos2 = c.apply(nodes)
    assert promos2 == []                      # sticky: no new batch under trigger
    assert _texts(first) == _texts(second)    # byte-identical render


def test_view_has_no_side_effects() -> None:
    nodes = _history(6)
    c = Compactor(config=_cfg())
    c.view(nodes)
    assert c._digested == {} and c.trimmer._trimmed == set()
    rendered = c.view(nodes)
    assert rendered == [n.message for n in nodes]


def test_tier3_epoch_fold() -> None:
    nodes = _history(10)
    c = Compactor(config=_cfg(max_resident_span_digests=3, epoch_fold_spans=3))
    _, promos = c.apply(nodes)
    assert any(p.tier == 3 for p in promos)
    joined = "\n".join(_texts(c.view(nodes)))
    assert "[epoch node_u0.." in joined
    assert "older turns folded" in joined


# ─── Consume-once image results ──────────────────────────────────────────────


def _image_history(*, consumed: bool = True) -> list[SessionNode]:
    nodes = [
        _node("u0", "user", [Text("look at this")]),
        _node("a0", "assistant", [
            Text("taking screenshot"),
            ToolUseBlock(id="call0", name="Screenshot", input={}),
        ]),
        _node("r0", "user", [
            ToolResultBlock(
                tool_use_id="call0",
                content=[Image(media_type="image/png", data="a" * 20_000)],
                is_error=False,
            )
        ]),
    ]
    if consumed:
        nodes.append(_node("f0", "assistant", [Text("I see it")]))
    return nodes


def _images(message: Message) -> list[Image]:
    return [
        inner
        for block in message.content
        for inner in getattr(block, "content", None) or []
        if isinstance(inner, Image)
    ]


def test_compactor_trims_consumed_image_results() -> None:
    """The Compactor renders node-by-node, so the consume-once decision has to
    be computed over the WHOLE path before those per-node renders happen."""
    nodes = _image_history()
    c = Compactor(config=_armed())

    # Nothing decided yet, so the view still carries the image: consumption is
    # a decision apply() makes, not something rendering does behind its back.
    assert _images(c.view(nodes)[2]) != []

    rendered, promotions = c.apply(nodes)
    assert _images(rendered[2]) == []
    # An image gets its OWN placeholder: HistoryExpand cannot restore it.
    assert "image consumed from context" in "\n".join(_texts([rendered[2]]))
    # No size tier fired, but the consume-once trim is itself reported so the
    # loss is visible in the transcript instead of silent.
    assert [(p.tier, p.reason, p.trimmed) for p in promotions] == [
        (1, "image_consumed", (("r0", "call0"),))
    ]
    assert ("r0", "call0") in c.trimmer._image_consumed  # the sticky decision
    assert c.trimmer._trimmed == set()  # NOT a full trim of the whole result
    # Decided now, so the read-only view reports exactly the next request.
    assert _images(c.view(nodes)[2]) == []


def _armed() -> CompactionConfig:
    """A ladder whose image rung is armed — consumption needs real pressure."""
    return CompactionConfig(trigger_tokens=1, trim=TrimConfig(trigger_tokens=1))


def test_images_survive_until_the_context_is_under_real_pressure() -> None:
    """The audited cost bug: images were consumed before the threshold was even
    computed, so a session at 18% window occupancy paid a mid-prefix rewrite
    per screenshot. Under the default trigger this history is nowhere near it.
    """
    nodes = _image_history()
    c = Compactor(config=CompactionConfig())

    rendered, promotions = c.apply(nodes)
    assert promotions == []
    assert _images(rendered[2]) != []
    assert c.trimmer._image_consumed == set()


def test_images_are_never_consumed_mid_turn() -> None:
    """A rewrite between two calls of one turn breaks a prefix the provider
    built seconds ago, so pressure alone is not enough: the batch waits for
    the next turn's first call."""
    nodes = _image_history()
    c = Compactor(config=_armed())

    rendered, promotions = c.apply(nodes, turn_boundary=False)
    assert promotions == []
    assert _images(rendered[2]) != []

    rendered, promotions = c.apply(nodes, turn_boundary=True)
    assert [p.reason for p in promotions] == ["image_consumed"]
    assert _images(rendered[2]) == []


def test_consuming_an_image_keeps_its_sibling_text_in_place() -> None:
    """trim.py used to replace the whole ToolResultBlock, so a 57-line test
    summary left context with the PNG it shared a result with."""
    nodes = _image_history()
    nodes[2] = _node("r0", "user", [ToolResultBlock(
        tool_use_id="call0",
        content=[
            Text("[image: /w/shot.png, image/png, 12 bytes]"),
            Image(media_type="image/png", data="a" * 20_000),
            Text("PASS: 57 checks"),
        ],
    )])
    rendered, _promotions = Compactor(config=_armed()).apply(nodes)

    texts = [b.text for b in rendered[2].content[0].content]
    assert texts[0] == "[image: /w/shot.png, image/png, 12 bytes]"
    assert texts[2] == "PASS: 57 checks"               # sibling text untouched
    assert "image consumed from context" in texts[1]   # placeholder, in position
    assert 'Re-Read the file at "/w/shot.png"' in texts[1]
    assert _images(rendered[2]) == []


def test_a_consumed_image_replays_as_an_image_not_a_full_trim() -> None:
    """Resume rehydration: the two tier-1 reasons rewrite different amounts of
    the block, so replaying one as the other would move the prefix."""
    nodes = _image_history()
    nodes[2] = _node("r0", "user", [ToolResultBlock(
        tool_use_id="call0",
        content=[Text("keep me"), Image(media_type="image/png", data="a" * 20_000)],
    )])
    first = Compactor(config=_armed())
    rendered, promotions = first.apply(nodes)

    resumed = Compactor(config=_armed())
    resumed.rehydrate(promotions)
    assert resumed.trimmer._image_consumed == first.trimmer._image_consumed
    assert resumed.trimmer._trimmed == set()
    assert _texts(resumed.view(nodes)) == _texts(rendered)  # byte-identical view


def test_compactor_keeps_images_when_trim_disabled() -> None:
    nodes = _image_history()
    c = Compactor(config=CompactionConfig(
        trigger_tokens=1, trim=TrimConfig(enabled=False, trigger_tokens=1)
    ))

    assert _images(c.view(nodes)[2]) == _images(nodes[2].message)
    rendered, _promos = c.apply(nodes)
    assert _images(rendered[2]) == _images(nodes[2].message)
    assert c.trimmer._trimmed == set()


def test_compactor_keeps_unconsumed_image() -> None:
    """No later assistant node → the image has not reached the model yet."""
    nodes = _image_history(consumed=False)
    c = Compactor(config=_armed())
    rendered, promotions = c.apply(nodes)
    assert promotions == []
    assert _images(rendered[2]) == _images(nodes[2].message)


def _image_then_turns(turns: int) -> list[SessionNode]:
    """A consumed screenshot, ``turns`` finished turns of tool results, and
    the next turn's first user message."""
    nodes = _image_history()
    for t in range(1, turns + 1):
        nodes.extend(_turn(t))
    nodes.append(_node("u_next", "user", [Text("next question")]))
    return nodes


def _due_only_before_images(nodes: list[SessionNode]) -> tuple[int, int, int]:
    """(prompt estimate, image reclaim, trigger) with the trigger placed so the
    prompt is over it before the images leave and under it after."""
    from fluid.context.trim import estimate_tokens_of_messages

    probe = Compactor(config=_cfg())
    before = estimate_tokens_of_messages(probe.view(nodes))
    assert probe.trimmer.consume_images(nodes)
    reclaim = before - estimate_tokens_of_messages(probe.view(nodes))
    return before, reclaim, before - reclaim // 2


def _ladder_for(trigger: int, *, in_batch: bool) -> CompactionConfig:
    return _cfg(trigger_tokens=trigger, keep_recent_turns=50,
                image_rung_in_batch=in_batch)


def test_image_rung_and_size_trim_share_one_batch() -> None:
    # Plan item 6 (B13), the 55a36089 seq 26699/26859 shape: a due batch at a
    # turn start consumed the images, which pulled the prompt under the
    # trigger, so the size trim waited and came one call later as a second
    # rebuild. The due decision is now made before the images leave, so both
    # rungs land in one rewrite.
    nodes = _image_then_turns(4)
    before, reclaim, trigger = _due_only_before_images(nodes)
    assert reclaim >= before * 0.05  # the image batch alone clears the floor
    # One call later the turn has grown by about what the images freed.
    grown = nodes + [
        _node("a_next", "assistant", [
            ToolUseBlock(id="call_next", name="Read", input={"file_path": "g.py"}),
        ]),
        _node("r_next", "user", [ToolResultBlock(
            tool_use_id="call_next", content=[Text("y" * (reclaim * 4))])]),
    ]

    def run(in_batch: bool) -> tuple[Compactor, list[list[str]]]:
        c = Compactor(config=_ladder_for(trigger, in_batch=in_batch))
        batches = []
        for prefix, boundary in ((nodes, True), (grown, False)):
            rendered, promotions = c.apply(prefix, turn_boundary=boundary)
            assert rendered == c.view(prefix)
            batches.append([p.reason for p in promotions])
        return c, batches

    legacy, batches = run(False)
    assert batches == [["image_consumed"], ["trigger"]]  # two rebuilds

    fixed, batches = run(True)
    assert batches == [["image_consumed", "trigger"], []]
    assert fixed.last_decision["reason"] == "below_threshold"
    assert fixed.trimmer._image_consumed == {("r0", "call0")}
    # The two newest results stay; the older ones went in the first batch.
    assert fixed.trimmer._trimmed == {("node_r1", "call1"), ("node_r2", "call2")}

    # The merged batch replays on resume like any other.
    c = Compactor(config=_ladder_for(trigger, in_batch=True))
    _, promotions = c.apply(nodes)
    resumed = Compactor(config=_ladder_for(trigger, in_batch=True))
    resumed.rehydrate(promotions)
    assert resumed.view(nodes) == c.view(nodes)


def test_small_image_reclaim_no_longer_sinks_a_due_batch() -> None:
    # Plan item 6 (B13), the 5ee437a3 seq 3238 shape: the images freed under
    # 5% and pulled the prompt under the trigger, so the whole due batch was
    # rejected and the watermark armed. Now the size rungs run in the same
    # candidate and the batch clears the floor.
    nodes = _image_then_turns(30)
    before, reclaim, trigger = _due_only_before_images(nodes)
    assert 0 < reclaim < before * 0.05

    legacy = Compactor(config=_ladder_for(trigger, in_batch=False))
    rendered, promotions = legacy.apply(nodes)
    assert not promotions and rendered == legacy.view(nodes)
    assert legacy.last_decision["reason"] == "insufficient_reclaim"
    assert not legacy.trimmer._image_consumed  # rolled back with the batch

    fixed = Compactor(config=_ladder_for(trigger, in_batch=True))
    rendered, promotions = fixed.apply(nodes)
    assert [p.reason for p in promotions] == ["image_consumed", "trigger"]
    assert fixed.last_decision["reason"] == "meaningful_reclaim"
    assert fixed.last_decision["after_tokens"] < before * 0.95
    assert fixed._optional_retry is None
    assert _images(rendered[2]) == []


def test_image_rung_below_the_trigger_still_builds_no_size_batch() -> None:
    # The due decision moved, not the trigger: a prompt under it at a turn
    # start runs no size rung, whatever image_trigger_ratio consumed.
    nodes = _image_then_turns(4)
    before, _reclaim, _trigger = _due_only_before_images(nodes)
    c = Compactor(config=_cfg(trigger_tokens=before + 1_000,
                              image_trigger_ratio=0.5, keep_recent_turns=50))
    _, promotions = c.apply(nodes)
    assert [p.reason for p in promotions] == ["image_consumed"]
    assert c.trimmer._trimmed == set()


# ─── Threshold arithmetic ────────────────────────────────────────────────────


def test_threshold_absolute_when_window_none() -> None:
    nodes = _history(3)
    c = Compactor(config=_cfg(trigger_tokens=10_000_000,
                              trim=TrimConfig(trigger_tokens=10_000_000)))
    _, promos = c.apply(nodes, context_window=None)
    assert promos == []                       # huge absolute trigger, no window


def test_threshold_uses_window_headroom() -> None:
    nodes = _history(6)
    # Same huge absolute trigger, but a small window forces the batch:
    # threshold = min(10M, 3000 - 1000 - 500).
    c = Compactor(config=_cfg(trigger_tokens=10_000_000, headroom_tokens=1_000,
                              trim=TrimConfig(trigger_tokens=10_000_000,
                                              keep_recent_tool_results=2,
                                              min_result_chars=500)))
    _, promos = c.apply(nodes, context_window=3_000, output_reserve_tokens=500)
    assert promos


def test_overhead_counts_toward_trigger() -> None:
    nodes = _history(6, result_chars=8_000)
    cfg = _cfg(trigger_tokens=100_000,
               trim=TrimConfig(trigger_tokens=100_000,
                               keep_recent_tool_results=2, min_result_chars=500))
    quiet = Compactor(config=cfg)
    _, promos = quiet.apply(nodes, overhead_tokens=0)
    assert promos == []                       # messages alone are under trigger
    loud = Compactor(config=cfg)
    _, promos = loud.apply(nodes, overhead_tokens=99_000)
    assert promos                             # system+tools overhead tips it


# ─── Fork safety ─────────────────────────────────────────────────────────────


def test_promotions_off_path_ignored() -> None:
    nodes = _history(6)
    c = Compactor(config=_cfg())
    c.apply(nodes)
    assert c._digested
    # A different branch that shares no promoted node renders fully live.
    branch = _turn(9)
    rendered = c.view(branch)
    assert rendered == [n.message for n in branch]


# ─── Intake stubs (stateless view policy) ────────────────────────────────────


def test_oversize_user_text_stubs_from_first_render() -> None:
    big = "A" * 30_000
    nodes = [_node("node_u0", "user", [Text("please review this"), Text(big)])]
    c = Compactor(config=_cfg(intake_stub_chars=25_000))
    rendered = c.view(nodes)
    texts = [b.text for b in rendered[0].content]
    assert texts[0] == "please review this"   # instruction text untouched
    assert "large user attachment: 30000 chars" in texts[1]
    assert 'HistoryExpand("node_u0"' in texts[1]
    assert c._digested == {} and c.trimmer._trimmed == set()  # stateless


def test_intake_stub_deterministic_and_under_threshold_inert() -> None:
    small = _node("node_u0", "user", [Text("A" * 1_000)])
    c = Compactor(config=_cfg())
    assert c.view([small]) == [small.message]


# ─── Engine integration: rehydration and event durability ────────────────────


def test_context_compaction_event_codec_roundtrip() -> None:
    e = ContextCompaction(
        session_id="s", turn_id="t", tier=2, reason="trigger",
        trimmed=[["n1", "t1"]], spans=[["node_a", "node_b", "digest"]],
        reclaimed_chars=1234, seq=7,
    )
    assert event_from_dict(event_to_dict(e)) == e


class _ScriptedProvider:
    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _text_script(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _request_texts(request: ProviderRequest) -> str:
    out = []
    for m in request.messages:
        for b in m.content:
            text = getattr(b, "text", None)
            if isinstance(text, str):
                out.append(text)
            for c in getattr(b, "content", None) or []:
                if isinstance(getattr(c, "text", None), str):
                    out.append(c.text)
    return "\n".join(out)


async def test_long_turn_engine_persists_delivery_segments_and_replays(tmp_path) -> None:
    import json
    from fluid.events.ops import Steer
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins import register_builtins
    from fluid.tools.inproc import create_inproc_server, tool
    from fluid.tools.registry import ToolRegistry

    registry = ToolRegistry()
    register_builtins(registry)
    calls = 0
    steer = "Do not deploy; preserve production credentials."

    @tool("work", "Work", {"type": "object", "properties": {"text": {"type": "string"}}})
    async def work(args):
        nonlocal calls
        calls += 1
        if calls == 1:
            await loop.submit(Steer(text=steer, id="steer-1"))
        return {"content": [{"type": "text", "text": "r" * 2_200}]}

    registry.register_inproc(create_inproc_server("long", [work]))
    scripts = []
    for i in range(50):
        scripts.append([
            StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
            StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                        "tool_use_id": f"c{i}", "tool_name": "mcp__long__work"}),
            StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps({"text": f"{i:02d}" + "a" * 17_998})}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
            StreamChunk("message_stop", {}),
        ])
    scripts.append(_text_script("done"))
    provider = _ScriptedProvider(scripts)
    store = JsonlStore(tmp_path)
    loop = NativeLoop(provider=provider, store=store, model="m", registry=registry,
                      permissions=PermissionEngine("never_ask"))
    await loop.submit(UserTurn(content="Keep every production credential unchanged."))
    await asyncio.wait_for(loop.wait_turn(), 15)
    assert calls == 50
    sid = loop.session_id
    events = await store.events_since(sid, 0)
    promotions = [e for e in events if isinstance(e, ContextCompaction)]
    assert any(e.tier == 2 for e in promotions)
    assert all(e.before_tokens > e.after_tokens > 0 for e in promotions)
    assert all(e.first_changed_message is not None for e in promotions)
    # Each decision records the renderer it was made under, so resume replays
    # it at that version.
    assert {e.render_version for e in promotions} == {loop._compactor.render_version}
    assert all(steer in _request_texts(r) for r in provider.requests[1:])
    assert all(repair_dangling_tool_calls(r.messages) is r.messages for r in provider.requests)
    await loop.aclose()
    fresh_store = JsonlStore(tmp_path)
    fresh = NativeLoop(provider=_ScriptedProvider([_text_script("done")]), store=fresh_store,
                       model="m", session_id=sid, registry=registry)
    await fresh._rehydrate_compaction(sid)
    nodes = await fresh_store.load_path(sid)
    assert fresh._compactor.view(nodes) == loop._compactor.view(nodes)
    first_result = next(b for n in nodes for b in n.message.content if isinstance(b, ToolResultBlock))
    assert first_result.delivered_steers[0].id == "steer-1"
    assert first_result.delivered_steers[0].text == steer
    assert len(next(b.input["text"] for n in nodes for b in n.message.content if isinstance(b, ToolUseBlock))) == 18_000
    await fresh.aclose()


async def test_engine_compacts_emits_event_and_rehydrates(tmp_path) -> None:
    """End to end over a durable store: the trigger fires mid-session, a
    ContextCompaction event is persisted, and a FRESH engine over the reloaded
    store renders the byte-identical compacted view."""
    store = JsonlStore(tmp_path)
    compaction = CompactionConfig(
        trigger_tokens=800, target_ratio=0.5, keep_recent_turns=1,
        trim=TrimConfig(trigger_tokens=800, keep_recent_tool_results=1,
                        min_result_chars=200),
    )
    provider = _ScriptedProvider([_text_script("ok " + "pad " * 300)])
    loop = NativeLoop(provider=provider, store=store, model="m",
                      compaction=compaction)
    for i in range(6):
        await loop.submit(UserTurn(content=f"turn {i} " + "words " * 150))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
    sid = loop.session_id
    events = loop.bus.log(sid)
    compactions = [e for e in events if isinstance(e, ContextCompaction)]
    assert compactions and all(e.reason == "trigger" for e in compactions)
    last_request = provider.requests[-1]
    assert "compacted turn" in _request_texts(last_request)

    # Resume: fresh store instance, fresh engine, replay events → same view.
    store2 = JsonlStore(tmp_path)
    provider2 = _ScriptedProvider([_text_script("done")])
    loop2 = NativeLoop(provider=provider2, store=store2, model="m",
                       session_id=sid, compaction=compaction)
    await loop2.submit(UserTurn(content="resumed"))
    await asyncio.wait_for(loop2.wait_turn(), timeout=5)
    resumed = _request_texts(provider2.requests[0])
    original = _request_texts(last_request)
    # Every digest line rendered before the restart appears verbatim after it.
    for line in original.splitlines():
        if line.startswith("[compacted turn") or line.startswith("[epoch"):
            assert line in resumed
    # And the store itself was never mutated: full fidelity is still there.
    nodes = await store2.load_path(sid)
    all_text = "\n".join(
        "".join(getattr(b, "text", "") for b in n.message.content)
        for n in nodes
    )
    assert "turn 0" in all_text               # original user text intact


async def test_compaction_never_appends_nodes(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    provider = _ScriptedProvider([_text_script("ok")])
    loop = NativeLoop(
        provider=provider, store=store, model="m",
        compaction=CompactionConfig(
            trigger_tokens=50, keep_recent_turns=0,
            trim=TrimConfig(trigger_tokens=50, keep_recent_tool_results=0,
                            min_result_chars=100),
        ),
    )
    await loop.submit(UserTurn(content="x " * 500))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    nodes = await store.load_path(loop.session_id)
    # Exactly the user node + assistant node — no digest was written back.
    assert [n.message.role for n in nodes] == ["user", "assistant"]



# ─── Batch 8: PRM-5, placeholders without a recall route ─────────────────────


def test_placeholders_drop_the_recall_route_when_the_tools_are_absent() -> None:
    from fluid.context.trim import (
        TrimConfig,
        _image_placeholder,
        _placeholder,
        _user_image_placeholder,
    )

    assert "HistoryExpand" in _placeholder("node_1", 5000, "head").text
    no_recall = _placeholder("node_1", 5000, "head", recall=False).text
    assert "HistoryExpand" not in no_recall and "re-run the tool" in no_recall
    assert "NOT in context" in no_recall and "head" in no_recall
    assert "HistoryExpand" not in _image_placeholder("n", 1, "[image: a.png, image/png]", recall=False).text
    assert "HistoryExpand" not in _user_image_placeholder("n", 1, recall=False).text
    assert TrimConfig().recall_tools is True


def test_digests_and_stubs_drop_the_recall_route_when_the_tools_are_absent() -> None:
    from fluid.context.compaction import (
        CompactionConfig,
        _intake_stub,
        _tool_result_stub,
        render_epoch_stub,
        render_span_digest,
    )
    from fluid.messages import Message
    from fluid.messages.content import Text
    from fluid.store.base import SessionNode

    span = [
        SessionNode(node_id="n1", parent_id=None,
                    message=Message(role="user", content=[Text("do the thing")])),
        SessionNode(node_id="n2", parent_id="n1",
                    message=Message(role="assistant", content=[Text("done")])),
    ]
    with_recall = CompactionConfig()
    without = CompactionConfig(recall_tools=False)
    assert "HistoryExpand" in render_span_digest(span, with_recall).text()
    digest = render_span_digest(span, without).text()
    assert "HistoryExpand" not in digest and "HistorySearch" not in digest
    assert "not recoverable" in digest and "do the thing" in digest
    epoch = render_epoch_stub([span], without).text()
    assert "HistoryExpand" not in epoch and "not recoverable" in epoch
    assert "HistoryExpand" not in _intake_stub("n1", "x" * 100, without).text
    assert "HistoryExpand" not in _tool_result_stub("n1", "x" * 100, without).text
    assert "HistoryExpand" in _tool_result_stub("n1", "x" * 100, with_recall).text


def test_engine_turns_the_recall_route_off_with_the_tools(tmp_path) -> None:
    from types import SimpleNamespace

    from fluid.config.loader import BUILTIN_DEFAULTS, build_engine, deep_merge
    from fluid.config.model import HarnessConfig
    from fluid.store.base import MemoryStore

    config = HarnessConfig.from_dict(deep_merge(BUILTIN_DEFAULTS, {"cwd": str(tmp_path)}))
    on = build_engine(config, MemoryStore(), provider=SimpleNamespace())
    assert on._compactor.config.recall_tools and on._trimmer.config.recall_tools
    off = build_engine(
        config, MemoryStore(), provider=SimpleNamespace(), disallowed_tools=["HistoryExpand"],
    )
    assert not off._compactor.config.recall_tools
    assert not off._trimmer.config.recall_tools


# ─── Window protection: measured budget, guarded rungs, 5% floor ─────────────


def _live_turn(calls: int, *, result_chars: int, steer: str | None = None) -> list[SessionNode]:
    """An in-flight turn: the user's ask, then call/result pairs with typed
    delivery metadata. ``steer`` rides on the newest result, the way the
    engine delivers a mid-turn user message."""
    from fluid.messages.content import SteerDelivery

    nodes = [_node("live_u", "user", [Text("Fix the connector. " + "pad " * 50)])]
    for i in range(calls):
        steers = [SteerDelivery(text=steer, id="steer-live")] if steer and i == calls - 1 else []
        nodes += [
            _node(f"live_a{i}", "assistant", [
                ToolUseBlock(id=f"live{i}", name="Bash", input={"cmd": f"probe {i}"}),
            ]),
            _node(f"live_r{i}", "user", [ToolResultBlock(
                tool_use_id=f"live{i}",
                content=[Text(f"live-result-{i} " + "y" * result_chars)],
                delivered_steers=steers,
                delivery_metadata_version=1,
            )]),
        ]
    return nodes


def test_window_protection_uses_measured_prompt() -> None:
    # Character-based estimates can overstate the provider-billed prompt and trigger premature window
    # protection. Use the measured prompt when it is available.
    from fluid.context.trim import estimate_tokens_of_messages

    nodes = _history(6)
    reserve = 4_000
    estimate = estimate_tokens_of_messages(Compactor(config=_cfg()).view(nodes))
    window = estimate + _cfg().headroom_tokens + reserve  # estimate sits at the limit
    measured = int(estimate * 0.66)  # the like-for-like pair in the verify report

    # Mid-turn, with an earlier optional pass rejected and waiting for more
    # reclaimable growth. Only window protection may skip that wait.
    calibrated = Compactor(config=_cfg())
    calibrated._optional_retry = (1 << 62, 0)
    original = calibrated.view(nodes)
    rendered, promotions = calibrated.apply(
        nodes, context_window=window, output_reserve_tokens=reserve,
        turn_boundary=False, measured_prompt_tokens=measured,
    )
    assert not promotions and rendered == original
    assert calibrated.last_decision["reason"] != "window_protection"
    assert calibrated.last_decision["reason"] == "awaiting_reclaimable_growth"
    assert calibrated.last_decision["window_protection"] is False
    assert calibrated.last_decision["token_basis"] == "measured"
    assert calibrated.last_decision["measured_prompt_tokens"] == measured

    # The same state with no measurement falls back to the estimate, which
    # says the window is full: window protection runs and records its basis.
    estimated = Compactor(config=_cfg())
    estimated._optional_retry = (1 << 62, 0)
    _, promotions = estimated.apply(
        nodes, context_window=window, output_reserve_tokens=reserve,
        turn_boundary=False,
    )
    assert promotions
    assert {p.decision_reason for p in promotions} == {"window_protection"}
    assert {p.token_basis for p in promotions} == {"estimate"}
    assert {p.measured_prompt_tokens for p in promotions} == {0}

    # A measurement that really is at the limit still protects the window,
    # and the event says the decision was sized on it.
    at_limit = Compactor(config=_cfg())
    at_limit._optional_retry = (1 << 62, 0)
    _, promotions = at_limit.apply(
        nodes, context_window=window, output_reserve_tokens=reserve,
        turn_boundary=False, measured_prompt_tokens=estimate,
    )
    assert promotions
    assert {p.decision_reason for p in promotions} == {"window_protection"}
    assert {p.token_basis for p in promotions} == {"measured"}
    assert {p.measured_prompt_tokens for p in promotions} == {estimate}

    # The factor never exceeds 1: a measurement above the estimate (stale,
    # taken before a compaction) cannot make a roomy prompt look full.
    roomy = Compactor(config=_cfg())
    roomy.apply(
        nodes, context_window=window * 2, output_reserve_tokens=reserve,
        measured_prompt_tokens=window * 2,
    )
    assert roomy.last_decision["window_protection"] is False


def test_window_protection_never_digests_inflight_tail() -> None:
    # Window protection acts on a size check before anything is sent. When
    # the guarded rungs cannot reach the target it must stop there: trimming
    # or digesting the newest result is for a prompt the provider rejected.
    # In the verified incident that result carried the user's steer and the
    # model spent a call recovering it with HistoryExpand.
    from fluid.context.trim import estimate_tokens_of_messages

    steer = "rensaio wont connect either"
    cfg = _cfg(headroom_tokens=1_000, keep_recent_turns=1)
    nodes = _history(6) + _live_turn(3, result_chars=24_000, steer=steer)
    c = Compactor(config=cfg)
    estimate = estimate_tokens_of_messages(c.view(nodes))
    reserve = 1_000
    # The protected tail alone is over the limit, so the guarded rungs run
    # out before the target and the old code went on to trim(0)/digest(0).
    window = estimate // 2 + cfg.headroom_tokens + reserve

    rendered, promotions = c.apply(
        nodes, context_window=window, output_reserve_tokens=reserve,
        turn_boundary=False,
    )
    assert promotions  # the guarded rungs still reclaimed the old turns
    assert {p.decision_reason for p in promotions} == {"window_protection"}

    live_ids = {n.node_id for n in nodes if n.node_id.startswith("live_")}
    index = {n.node_id: i for i, n in enumerate(nodes)}
    for promo in promotions:
        for start, end, _kind in promo.spans:
            covered = {n.node_id for n in nodes[index[start]:index[end] + 1]}
            assert not covered & live_ids, (start, end)
    newest = [("live_r1", "live1"), ("live_r2", "live2")]  # keep_recent_tool_results=2
    trimmed = {pair for promo in promotions for pair in promo.trimmed}
    assert not trimmed & set(newest)
    texts = "\n".join(_texts(rendered))
    assert "live-result-2 " + "y" * 24_000 in texts
    assert "live-result-1 " + "y" * 24_000 in texts
    assert steer in "\n".join(str(b) for m in rendered for b in m.content)
    # Still over the escalation target: only the unguarded rungs could go on.
    assert c._estimate(nodes, 0) > c._threshold(cfg.trigger_tokens, window, reserve)

    # Provider-overflow recovery keeps the unguarded rungs.
    overflow = c.escalate(
        nodes, context_window=window, output_reserve_tokens=reserve,
    )
    assert any(set(p.trimmed) & set(newest) for p in overflow)


_VERIFIED_FALSE_PASSES = [
    # (billed prompt of the previous call, chars/4 estimate of the next one),
    # the eight gpt-6-astra window_protection batches in verify/compaction-estimator.
    (145_140, 216_979),
    (140_502, 211_906),
    (162_989, 210_162),
    (156_768, 212_243),
    (155_783, 211_465),
    (150_032, 211_018),
    (147_728, 211_654),
    (148_006, 210_064),
]


def test_verified_false_window_protection_passes_no_longer_compact() -> None:
    # Each batch fired window protection on the estimate while the billed
    # prompt sat at 55-64% of a 256k window, and 4 of the 5 that stopped at the
    # candidate reclaimed under 5% of the prompt, a cache-breaking rewrite for
    # 3-5% of it. Replayed with the measurement: none of the eight compacts.
    from fluid.context.trim import estimate_tokens_of_messages

    window, reserve = 256_000, 32_000  # limit 208k: every estimate above is over it
    nodes = _history(4) + _live_turn(2, result_chars=4_000)
    history_tokens = estimate_tokens_of_messages(Compactor().view(nodes))
    for billed, estimate in _VERIFIED_FALSE_PASSES:
        overhead = estimate - history_tokens
        # Without the measurement the request is still judged over the limit;
        # the floor now keeps even that pass from rewriting the prefix.
        blind = Compactor()
        _, promotions = blind.apply(
            nodes, overhead_tokens=overhead, context_window=window,
            output_reserve_tokens=reserve, turn_boundary=False,
        )
        assert promotions == [], (billed, estimate)
        assert blind.last_decision["window_protection"] is True
        assert blind.last_decision["token_basis"] == "estimate"

        # Sized on the billed prompt it is not window protection at all.
        c = Compactor()
        rendered, promotions = c.apply(
            nodes, overhead_tokens=overhead, context_window=window,
            output_reserve_tokens=reserve, turn_boundary=False,
            measured_prompt_tokens=billed,
        )
        assert promotions == [], (billed, estimate)
        assert rendered == c.view(nodes)
        assert c.last_decision["window_protection"] is False
        assert c.last_decision["token_basis"] == "measured"
        assert not c._digested and not c.trimmer._trimmed


def test_measured_calibration_sizes_the_optional_trigger_and_the_stop_target() -> None:
    # Calibration used to reach only the window-protection check. The
    # optional meaningful_reclaim trigger and the ladder's stop target still
    # read raw chars/4, so a real batch such as 63844 (211.9k estimated,
    # 140.5k billed) could still compact mid-turn, as meaningful_reclaim.
    from fluid.context.trim import estimate_tokens_of_messages

    # Trimming off, so only the digest rung reclaims, one turn at a time: its
    # stop target decides how much history goes.
    cfg = _cfg(trigger_tokens=1_000, trigger_window_ratio=0.6, target_ratio=0.7,
               headroom_tokens=1_000,
               trim=TrimConfig(trigger_tokens=1_000, keep_recent_tool_results=2,
                               min_result_chars=10**6))
    nodes = _history(20, result_chars=2_000)
    estimate = estimate_tokens_of_messages(Compactor(config=cfg).view(nodes))
    reserve = 1_000
    # The estimate is over the 0.6 * window trigger with plenty to reclaim;
    # the measured prompt (the 63844 ratio) is under it.
    window = int(estimate / 0.7)
    measured = int(estimate * 140_502 / 211_906)
    trigger = int(window * 0.6)
    assert measured < trigger < estimate

    blind = Compactor(config=cfg)
    _, promotions = blind.apply(
        nodes, context_window=window, output_reserve_tokens=reserve, turn_boundary=False,
    )
    assert promotions, "the history offers more than the 5% floor to reclaim"
    assert blind.last_decision["reason"] == "meaningful_reclaim"

    calibrated = Compactor(config=cfg)
    rendered, promotions = calibrated.apply(
        nodes, context_window=window, output_reserve_tokens=reserve,
        turn_boundary=False, measured_prompt_tokens=measured,
    )
    assert promotions == [] and rendered == calibrated.view(nodes)
    assert calibrated.last_decision["reason"] != "meaningful_reclaim"
    assert not calibrated._digested and not calibrated.trimmer._trimmed

    # Over the trigger on the measurement too: it compacts, and stops at the
    # target in measured tokens, which leaves more history than the blind
    # pass that aimed at the same number in chars/4.
    over = int(trigger * 1.05)
    scale = over / estimate
    sized = Compactor(config=cfg)
    _, sized_promotions = sized.apply(
        nodes, context_window=window, output_reserve_tokens=reserve,
        turn_boundary=False, measured_prompt_tokens=over,
    )
    assert sized_promotions
    sized_after = sized.last_decision["after_tokens"]
    blind_after = blind.last_decision["after_tokens"]
    assert sized_after > blind_after
    # The stop target, 0.7 of the trigger, is met in measured tokens.
    assert sized_after * scale <= int(trigger * 0.7)


async def test_engine_sizes_window_protection_on_the_billed_prompt(tmp_path) -> None:
    # The engine hands the compactor the last call's billed prompt carried
    # forward; with none on the path the decision says it used the estimate.
    from fluid.context.trim import estimate_tokens_of_messages

    def script(billed: int) -> list[StreamChunk]:
        return [
            StreamChunk("message_start", {"usage": {"input_tokens": billed}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": "noted"}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
            StreamChunk("message_stop", {}),
        ]

    decisions = {}
    for billed in (0, 10):
        store = JsonlStore(tmp_path / str(billed))
        loop = NativeLoop(provider=_ScriptedProvider([script(billed)]), store=store,
                          model="m", max_tokens=1_000)
        await loop.submit(UserTurn(content="context " + "words " * 4_000))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
        nodes = await store.load_path(loop.session_id)
        estimate = loop._context_overhead_tokens() + estimate_tokens_of_messages(
            loop._compactor.view(nodes)
        )
        # The next request's estimate is over the limit; the billed prompt is
        # far under it.
        loop.context_window = estimate + loop._compactor.config.headroom_tokens + 1_000
        await loop.submit(UserTurn(content="next"))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
        decisions[billed] = dict(loop._compactor.last_decision)
        await loop.aclose()

    assert decisions[0]["token_basis"] == "estimate"
    assert decisions[0]["window_protection"] is True
    assert decisions[10]["token_basis"] == "measured"
    assert decisions[10]["measured_prompt_tokens"] > 10
    assert decisions[10]["window_protection"] is False



async def test_window_protection_ignores_a_measurement_from_another_model(tmp_path) -> None:
    # A billed count is in the tokenizer of the model that billed it. After a
    # switch, live or across an engine rebuild, the window decision falls back
    # to the estimate until the new model's first call is measured.
    from fluid.context.trim import estimate_tokens_of_messages
    from fluid.events import SetModel

    def script(billed: int) -> list[StreamChunk]:
        return [
            StreamChunk("message_start", {"usage": {"input_tokens": billed}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": "noted"}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
            StreamChunk("message_stop", {}),
        ]

    async def first_call(store: JsonlStore) -> str:
        loop = NativeLoop(provider=_ScriptedProvider([script(10)]), store=store,
                          model="m", max_tokens=1_000)
        await loop.submit(UserTurn(content="context " + "words " * 4_000))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
        sid = loop.session_id
        await loop.aclose()
        return sid

    async def next_decision(loop: NativeLoop, store: JsonlStore, sid: str) -> dict:
        nodes = await store.load_path(sid)
        estimate = loop._context_overhead_tokens() + estimate_tokens_of_messages(
            loop._compactor.view(nodes)
        )
        # The estimate is over the limit; the billed prompt is far under it.
        loop.context_window = estimate + loop._compactor.config.headroom_tokens + 1_000
        await loop.submit(UserTurn(content="next"))
        await asyncio.wait_for(loop.wait_turn(), timeout=5)
        return dict(loop._compactor.last_decision)

    # Live switch on one engine.
    store = JsonlStore(tmp_path / "live")
    loop = NativeLoop(provider=_ScriptedProvider([script(10)]), store=store,
                      model="m", max_tokens=1_000)
    await loop.submit(UserTurn(content="context " + "words " * 4_000))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await loop.submit(SetModel(model="m2"))
    switched = await next_decision(loop, store, loop.session_id)
    await loop.aclose()
    assert switched["token_basis"] == "estimate"
    assert switched["window_protection"] is True

    # A rebuilt engine reads the model off the persisted Usage event.
    decisions = {}
    for model in ("m", "m2"):
        store = JsonlStore(tmp_path / f"rebuilt-{model}")
        sid = await first_call(store)
        rebuilt = NativeLoop(provider=_ScriptedProvider([script(10)]), store=store,
                             model=model, max_tokens=1_000, session_id=sid)
        decisions[model] = await next_decision(rebuilt, store, sid)
        await rebuilt.aclose()
    assert decisions["m"]["token_basis"] == "measured"
    assert decisions["m"]["window_protection"] is False
    assert decisions["m2"]["token_basis"] == "estimate"
    assert decisions["m2"]["window_protection"] is True


def test_context_compaction_event_carries_token_basis() -> None:
    e = ContextCompaction(
        session_id="s", turn_id="t", tier=1, reason="trigger",
        trimmed=[["n1", "t1"]], decision_reason="window_protection",
        token_basis="measured", measured_prompt_tokens=150_032, seq=3,
    )
    assert event_from_dict(event_to_dict(e)) == e
    legacy = event_to_dict(e)
    legacy.pop("token_basis")
    legacy.pop("measured_prompt_tokens")
    revived = event_from_dict(legacy)
    assert revived.token_basis == "" and revived.measured_prompt_tokens == 0


def test_segments_refuse_reused_call_ids_and_orphan_results() -> None:
    """A tool_use id seen twice, or a result for a call the batch never made,
    means the pairing cannot be trusted: the batch is not digested."""
    from fluid.context.compaction import completed_segments

    def turn(steps: list[tuple[str, str]]) -> list[SessionNode]:
        nodes = [_node("start", "user", [Text("Keep the API stable.")])]
        for n, (kind, ident) in enumerate(steps):
            if kind == "call":
                nodes.append(_node(f"a{n}", "assistant", [
                    ToolUseBlock(id=ident, name="Edit", input={"text": "a"})]))
            else:
                nodes.append(_node(f"r{n}", "user", [ToolResultBlock(
                    tool_use_id=ident, content=[Text("ok")], delivery_metadata_version=1,
                )]))
        return nodes

    paired = [(k, f"c{i}") for i in range(8) for k in ("call", "result")]
    assert len(completed_segments(turn(paired))) == 1

    # Calls 0 and 1 share an id; the one result settles "both" of them.
    reused = [("call", "c0"), ("call", "c0"), ("result", "c0"), *paired[4:]]
    assert not completed_segments(turn(reused))

    # A result whose call is absent, with every real call still paired.
    orphan = [*paired[:2], ("result", "ghost"), *paired[2:]]
    assert not completed_segments(turn(orphan))


# ─── Guard tests (compaction plan item 0) ────────────────────────────────────
#
# The cache-priced redesign (plan items 1-13) adds a cache signal, schedule
# zones and new ContextCompaction fields. These three tests pin what must not
# move while it lands: the legacy render, the provider-overflow path, and the
# replay of events persisted before any of the new fields existed.


def _parity_session(turns: int = 30) -> list[SessionNode]:
    """A long deterministic history that reaches every legacy render path:
    trims, digests, epoch folds, consumed images, oversize-intake stubs,
    verbatim errors, and user and system steers riding on results."""
    from fluid.messages.content import SteerDelivery

    tools = [("Read", "file_path"), ("Bash", "command"), ("Edit", "text")]
    nodes: list[SessionNode] = []
    for t in range(turns):
        ask = f"turn {t}: look at module-{t}. " + "detail " * (40 + 7 * (t % 5))
        if t % 11 == 4:
            ask += "A" * 26_000  # over intake_stub_chars: always a stub
        nodes.append(_node(f"u{t}", "user", [Text(ask)]))
        calls = 1 + t % 3
        for k in range(calls):
            name, arg = tools[(t + k) % 3]
            nodes.append(_node(f"a{t}_{k}", "assistant", [
                Text(f"step {k} of turn {t}"),
                ToolUseBlock(id=f"c{t}_{k}", name=name, input={arg: f"target-{t}-{k}"}),
            ]))
            error = t % 7 == 5 and k == calls - 1
            if error:
                content: list = [Text(f"Traceback: failure in turn {t}\n" + "e" * 900)]
            elif t % 8 == 3 and k == 0:
                content = [Text(f"screenshot {t}"),
                           Image(media_type="image/png", data="i" * 8_000)]
            else:
                content = [Text(f"result {t}.{k} " + "r" * (1_500 + 900 * ((t + k) % 6)))]
            steers = []
            if t % 6 == 2 and k == calls - 1:
                steers.append(SteerDelivery(text=f"user steer {t}: keep the API stable",
                                            id=f"steer-{t}"))
            if t % 9 == 6 and k == 0:
                steers.append(SteerDelivery(text=f"task-notification {t} " + "n" * 1_200,
                                            origin="system", id=f"note-{t}"))
            content.extend(Text(s.render()) for s in steers)
            nodes.append(_node(f"r{t}_{k}", "user", [ToolResultBlock(
                tool_use_id=f"c{t}_{k}", content=content, is_error=error,
                delivered_steers=steers, delivery_metadata_version=1,
            )]))
        nodes.append(_node(f"f{t}", "assistant", [Text(f"conclusion: module-{t} done")]))
    return nodes


# Today's Promotion fields. Listed explicitly so fields added later (render
# version, schedule, ...) cannot change the snapshot by existing.
_LEGACY_PROMOTION_FIELDS = (
    "tier", "reason", "trimmed", "spans", "reclaimed_chars", "before_tokens",
    "after_tokens", "decision_reason", "first_changed_message", "token_basis",
    "measured_prompt_tokens",
)


def _drive_legacy(compactor: Compactor, nodes: list[SessionNode], *,
                  window: int | None = None, measured: bool = False,
                  **apply_kw) -> tuple[str, list]:
    """Replay the model calls of ``nodes`` through ``apply`` the way the engine
    does: one call per turn start (a boundary) and one after each tool result
    (mid-turn). Returns a digest of every rendered view plus every promotion.
    ``apply_kw`` goes to every ``apply`` call (the item-3 cache signal)."""
    import hashlib
    import json

    from fluid.context.trim import estimate_tokens_of_messages

    digest = hashlib.sha256()
    promotions = []
    for end, node in enumerate(nodes, start=1):
        if node.message.role != "user":
            continue
        is_start = not any(isinstance(b, ToolResultBlock) for b in node.message.content)
        prefix = nodes[:end]
        kw: dict = {"overhead_tokens": 3_000, "turn_boundary": is_start, **apply_kw}
        if window is not None:
            kw.update(context_window=window, output_reserve_tokens=4_000)
        if measured:
            kw["measured_prompt_tokens"] = 3_000 + int(
                estimate_tokens_of_messages(compactor.view(prefix)) * 0.85)
        rendered, promos = compactor.apply(prefix, **kw)
        assert rendered == compactor.view(prefix)
        records = [[getattr(p, f) for f in _LEGACY_PROMOTION_FIELDS] for p in promos]
        promotions.extend(promos)
        digest.update(json.dumps([[m.to_dict() for m in rendered], records],
                                 sort_keys=True, default=list).encode())
    return digest.hexdigest(), promotions


# New behaviour flags switched off, so each ladder scenario runs the legacy
# path. Plan item 4 turned below_threshold_skips_retry on by default; with it
# on, the ladder, windowed and optional mega-turn scenarios commit earlier.
# Plan item 6 turned image_rung_in_batch on, which runs the size rungs in the
# same batch as the images. Plan item 5 turned on the deep target, the hard
# zone and the shortfall watermark, which change every windowed scenario.
# Plan item 2 turned on the token-budgeted recency tail, the in-flight rung,
# the floor waiver near the window and wake-aware turn counting (_ITEM2_OFF).
# The trim-only scenario needs no flag: that
# contract keeps the legacy path.
_ITEM2_OFF = {"recency_tail_tokens": 0,
              "turn_budget_window_ratio": 0.0,
              "window_floor_waiver_ratio": 0.0,
              "wake_aware_turns": False}
# render_version 1 is the origin/main renderer; version 2 (steer receipts,
# intent pins, cell summaries) changes digest and epoch bytes by design.
_LEGACY_FLAGS = {"render_version": 1,
                 "below_threshold_skips_retry": False,
                 "image_rung_in_batch": False,
                 "deep_target_window_ratio": 0.0,
                 "hard_window_ratio": 0.0,
                 "shortfall_watermark": False,
                 **_ITEM2_OFF}


def _parity_ladder() -> CompactionConfig:
    return CompactionConfig(
        trigger_tokens=12_000, keep_recent_turns=2,
        max_resident_span_digests=4, epoch_fold_spans=2,
        trim=TrimConfig(trigger_tokens=12_000, keep_recent_tool_results=3,
                        min_result_chars=500),
        **_LEGACY_FLAGS,
    )


_PARITY_SCENARIOS = {
    # The full ladder on an absolute trigger: trims, digests, epoch folds,
    # consumed images, retry watermarks.
    "ladder": (_parity_session, _parity_ladder, {}),
    # A known window with the measured basis: window scaling and calibration.
    "windowed_measured": (
        _parity_session,
        lambda: CompactionConfig(trigger_tokens=10_000, trigger_window_ratio=0.3,
                                 keep_recent_turns=2, **_LEGACY_FLAGS),
        {"window": 40_000, "measured": True},
    ),
    # One 50-call turn, the shape the redesign targets. On a small window it
    # runs window protection and the guarded escalation; on a large one the
    # optional mid-turn batches and their retry watermark.
    "mega_turn_window_protection": (
        lambda: _growing_turn(50), lambda: CompactionConfig(**_LEGACY_FLAGS),
        {"window": 100_000, "measured": True},
    ),
    "mega_turn_optional": (
        lambda: _growing_turn(50), lambda: CompactionConfig(**_LEGACY_FLAGS),
        {"window": 200_000, "measured": True},
    ),
    # The trim-only contract host apps rely on.
    "trim_only": (
        _parity_session,
        lambda: CompactionConfig(enabled=False, render_version=1, trim=TrimConfig(
            enabled=True, trigger_tokens=12_000, keep_recent_tool_results=3,
            min_result_chars=500)),
        {},
    ),
}

# Snapshot of origin/main 0163171fe. Any change here is a legacy render change:
# the cache-priced schedule must be opt-in (no cache signal, new flags off).
_PARITY_DIGESTS = {
    "ladder":
        "831ff63ee4b575bd3940115b830f33502e90e57f7e03b0861759dbc749fdc922",
    "windowed_measured":
        "0078398a095a29c2f97e60d6894582086344fc2ba8f7fdd28c60cb3b303ac781",
    "mega_turn_window_protection":
        "60d078381534cf10f66fddd54483b8e1837a20b2c9d57c95848cbb4cefd7a3f0",
    "mega_turn_optional":
        "cf373e1280f7b83f18ed1e992ae4fd72dc2b868fb9b9a34f846d2f4d7963e214",
    "trim_only":
        "26d6e88a8b740e5f112696ddb0bea481b76c8f5fdcb73071106006466ab166e4",
}


def test_legacy_render_parity_snapshot() -> None:
    """With no cache signal and every new flag off, the rendered view and the
    persisted decisions of each scenario are byte-identical to origin/main."""
    for name, (make_nodes, make_config, kw) in _PARITY_SCENARIOS.items():
        nodes = make_nodes()
        compactor = Compactor(config=make_config())
        digest, promotions = _drive_legacy(compactor, nodes, **kw)
        assert promotions, name
        assert digest == _PARITY_DIGESTS[name], name
        # Resume from the persisted decisions renders the same bytes.
        resumed = Compactor(config=make_config())
        resumed.rehydrate(promotions)
        assert resumed.view(nodes) == compactor.view(nodes), name


def test_cache_none_and_unpriced_signals_keep_the_legacy_digests() -> None:
    """Plan item 3: ``cache=None`` is today's path byte-for-byte. A cache
    signal changes nothing with ``cache_schedule`` off, with no known window
    (the ladder scenario), or under the trim-only contract."""
    from dataclasses import replace

    from fluid.context.cache_signal import CacheSignal

    warm = CacheSignal(gap_s=5.0, reuse_floor=3_000)
    for name, (make_nodes, make_config, kw) in _PARITY_SCENARIOS.items():
        digest, _ = _drive_legacy(Compactor(config=make_config()), make_nodes(),
                                  cache=None, **kw)
        assert digest == _PARITY_DIGESTS[name], name
        off = replace(make_config(), cache_schedule=False)
        digest, _ = _drive_legacy(Compactor(config=off), make_nodes(), cache=warm, **kw)
        assert digest == _PARITY_DIGESTS[name], name
        if "window" not in kw:
            digest, _ = _drive_legacy(Compactor(config=make_config()), make_nodes(),
                                      cache=warm, **kw)
            assert digest == _PARITY_DIGESTS[name], name


async def test_provider_overflow_runs_the_unguarded_rungs_and_recovers(tmp_path) -> None:
    """The escalate path has never run in production. The provider rejects a
    prompt whose only bulk is the in-flight turn, which every guarded rung
    protects, so recovery has to come from the guard-off rungs: trim(0) on the
    first retry, digest(0) on the second. The third request goes through."""
    import json

    from fluid.events import ErrorEvent, TurnComplete
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.inproc import create_inproc_server, tool
    from fluid.tools.registry import ToolRegistry

    @tool("blob", "Blob", {"type": "object", "properties": {}})
    async def blob(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "fresh-payload " + "B" * 6_000}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("b", [blob]))
    overflow = [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("error", {"message": "prompt is too long: 210000 tokens > 200000 maximum",
                              "retriable": False}),
    ]
    tool_call = [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "tool_use",
                                    "tool_use_id": "t1", "tool_name": "mcp__b__blob"}),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": json.dumps({})}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]
    provider = _ScriptedProvider([tool_call, overflow, overflow, _text_script("recovered")])
    store = JsonlStore(tmp_path)
    loop = NativeLoop(
        provider=provider, store=store, model="m", registry=registry,
        permissions=PermissionEngine("never_ask"),
        # Defaults for every guard (3 turns, 5 results); the normal trigger
        # never fires, so only the overflow path can compact.
        compaction=CompactionConfig(
            trigger_tokens=10_000_000,
            trim=TrimConfig(trigger_tokens=10_000_000, min_result_chars=500),
        ),
    )
    assert loop.context_window is None  # one rung per retry
    ask = "Keep the API stable. " + "context " * 400
    await loop.submit(UserTurn(content=ask))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert next(e for e in events if isinstance(e, TurnComplete)).stop_reason == "end_turn"
    assert provider.calls == 4
    rungs = [e for e in events if isinstance(e, ContextCompaction)]
    assert [(e.tier, e.reason, e.decision_reason) for e in rungs] == [
        (1, "overflow", "provider_overflow"),
        (2, "overflow", "provider_overflow"),
    ]
    nodes = await store.load_path(loop.session_id)
    result_node = next(n for n in nodes if any(
        isinstance(b, ToolResultBlock) for b in n.message.content))
    # trim(0): the newest result, inside every recency guard, was trimmed.
    assert rungs[0].trimmed == [[result_node.node_id, "t1"]]
    assert "trimmed" in _request_texts(provider.requests[2])
    assert "B" * 6_000 not in _request_texts(provider.requests[2])
    # digest(0): the in-flight turn itself, turn-start node included.
    assert rungs[1].spans[0][0] == nodes[0].node_id
    final = provider.requests[3]
    assert "compacted turn" in _request_texts(final)
    assert repair_dangling_tool_calls(final.messages) is final.messages
    assert all(e.before_tokens > e.after_tokens > 0 for e in rungs)
    # The store keeps full fidelity; eviction stays recoverable.
    assert "fresh-payload" in result_node.message.content[0].content[0].text
    assert nodes[0].message.content[0].text == ask
    # And a fresh engine replays both rungs to the same view.
    view = loop._compactor.view(nodes)
    await loop.aclose()
    fresh = NativeLoop(provider=_ScriptedProvider([_text_script("done")]),
                       store=JsonlStore(tmp_path), model="m",
                       session_id=loop.session_id, compaction=loop._compactor.config)
    await fresh._rehydrate_compaction(loop.session_id)
    assert fresh._compactor.view(nodes) == view
    await fresh.aclose()


async def test_rehydrate_over_events_persisted_without_the_new_fields(tmp_path) -> None:
    """Rows written by older builds lack every field added since: the oldest
    carry only the decision, newer ones the token basis. Each field a row
    lacks takes its default and the resumed view is byte-identical."""
    import dataclasses
    import json

    from fluid.events.model import SCHEMA_VERSION
    from fluid.tools.builtins import register_builtins
    from fluid.tools.registry import ToolRegistry

    nodes = _parity_session(16)
    live = Compactor(config=_parity_ladder())
    _, promotions = _drive_legacy(live, nodes)
    assert {p.tier for p in promotions} == {1, 2, 3}
    assert any(p.reason == "image_consumed" for p in promotions)

    oldest = ("tier", "reason", "trimmed", "spans", "reclaimed_chars")
    sid = "ses_legacy"
    rows = []
    for seq, p in enumerate(promotions, start=1):
        row = {"_cls": "ContextCompaction", "_v": SCHEMA_VERSION,
               "session_id": sid, "turn_id": "turn_legacy", "seq": seq,
               "tier": p.tier, "reason": p.reason,
               "trimmed": [list(t) for t in p.trimmed],
               "spans": [list(s) for s in p.spans],
               "reclaimed_chars": p.reclaimed_chars}
        if seq % 2 == 0:  # a build that already had the token basis
            row.update(before_tokens=p.before_tokens, after_tokens=p.after_tokens,
                       decision_reason=p.decision_reason,
                       first_changed_message=p.first_changed_message,
                       token_basis=p.token_basis,
                       measured_prompt_tokens=p.measured_prompt_tokens)
        rows.append(row)
    (tmp_path / f"{sid}.events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    store = JsonlStore(tmp_path)
    events = await store.events_since(sid, 0)
    assert len(events) == len(rows)
    assert all(isinstance(e, ContextCompaction) for e in events)
    base = {"session_id", "node_id", "turn_id", "seq", "ts"}
    for event, row in zip(events, rows):
        for f in dataclasses.fields(ContextCompaction):
            if f.name in row or f.name in base:
                continue
            default = (f.default if f.default is not dataclasses.MISSING
                       else f.default_factory())  # type: ignore[misc]
            assert getattr(event, f.name) == default, f.name
    for event, row in zip(events, rows):
        assert all(getattr(event, k) == row[k] for k in oldest)

    registry = ToolRegistry()
    register_builtins(registry)  # HistoryExpand on the surface: recall stays on
    fresh = NativeLoop(provider=_ScriptedProvider([_text_script("done")]), store=store,
                       model="m", session_id=sid, compaction=_parity_ladder(),
                       registry=registry)
    assert fresh._compactor.config == live.config
    await fresh._rehydrate_compaction(sid)
    assert fresh._compactor.view(nodes) == live.view(nodes)
    await fresh.aclose()

    # A build on the current renderer resumes the same rows at the version
    # they were written under (1, since they carry no render_version), so an
    # old session keeps its bytes after an upgrade.
    from dataclasses import replace
    current = replace(_parity_ladder(), render_version=CompactionConfig().render_version)
    assert current.render_version > 1
    upgraded = NativeLoop(provider=_ScriptedProvider([_text_script("done")]),
                          store=JsonlStore(tmp_path), model="m", session_id=sid,
                          compaction=current, registry=registry)
    await upgraded._rehydrate_compaction(sid)
    assert upgraded._compactor.view(nodes) == live.view(nodes)
    await upgraded.aclose()


# ── Plan item 5 (B2): deep target, hard zone, shortfall watermark ──────────
#
# The 272k window used below gives, with a 4k output reserve: trigger S =
# 163,200, deep target T = 0.35W = 95,200, hard ceiling H = min(0.75W,
# L - 32k) = 204,000 and window-protection limit L = 252,000.

# Item 5's comparisons run against the legacy recency guards as well, so the
# item 2 flags are off in the baseline too.
_ITEM5_OFF = {"deep_target_window_ratio": 0.0, "hard_window_ratio": 0.0,
              "shortfall_watermark": False, **_ITEM2_OFF}
_W, _RESERVE = 272_000, 4_000


def _floor_heavy_turn(calls: int) -> list[SessionNode]:
    """One long turn of small calls with typed delivery metadata, so its
    completed segments become digestible as the turn grows."""
    nodes = [_node("start", "user", [Text("Ship the release. Never touch prod credentials.")])]
    for i in range(calls):
        nodes.append(_node(f"a{i}", "assistant", [
            ToolUseBlock(id=f"c{i}", name="Bash", input={"command": "x" * 2_000})]))
        nodes.append(_node(f"r{i}", "user", [ToolResultBlock(
            tool_use_id=f"c{i}", content=[Text("r" * 4_000)],
            delivery_metadata_version=1)]))
    return nodes


def _drive_mega_turn(config: CompactionConfig, calls: int, *, overhead: int) -> tuple[
        Compactor, list[SessionNode], list[list], list[dict], int]:
    """Every call after a tool result, mid-turn, the way the engine drives a
    long turn. Returns the batches, every decision, and the peak prompt."""
    from fluid.context.trim import estimate_tokens_of_messages

    c = Compactor(config=config)
    nodes = _floor_heavy_turn(calls)
    batches, decisions, peak = [], [], 0
    for end in range(3, len(nodes) + 1, 2):
        prefix = nodes[:end]
        peak = max(peak, overhead + estimate_tokens_of_messages(c.view(prefix)))
        rendered, promotions = c.apply(
            prefix, overhead_tokens=overhead, context_window=_W,
            output_reserve_tokens=_RESERVE, turn_boundary=False)
        assert rendered == c.view(prefix)
        decisions.append(dict(c.last_decision))
        if promotions:
            batches.append(promotions)
    return c, nodes, batches, decisions, peak


def test_mega_turn_fires_at_most_two_batches_where_legacy_fires_five() -> None:
    # The 33dc66c6 turn 1612b7 shape: a long turn over a large floor that no
    # rung can reclaim (system prompt, tools, steers; 130k here). Each batch
    # can only digest the newest aged segment, so the legacy commit reset the
    # watermark and the ladder refired at each segment completion, about
    # every 8-10 calls, each time for a shallow reclaim.
    _, _, old, _, old_peak = _drive_mega_turn(
        CompactionConfig(**_ITEM5_OFF), 60, overhead=130_000)
    assert len(old) == 5
    # Item 5 alone: the item 2 recency tail keeps 40k tokens verbatim, which
    # changes what the first batch can take, so it stays off on both sides.
    c, nodes, new, decisions, peak = _drive_mega_turn(
        CompactionConfig(**_ITEM2_OFF), 60, overhead=130_000)
    assert len(new) <= 2
    # The first batch is the same. The shortfall watermark then holds the
    # next one until a batch can reclaim the shortfall, so it is deeper than
    # any legacy refire.
    assert [p.trimmed for p in new[0]] == [p.trimmed for p in old[0]]
    legacy_drops = [b[0].before_tokens - b[0].after_tokens for b in old[1:]]
    assert new[1][0].before_tokens - new[1][0].after_tokens > 2 * max(legacy_drops)
    assert "awaiting_reclaimable_growth" in [d["reason"] for d in decisions]
    # The cost is a higher peak, still under the hard ceiling and well under
    # window protection.
    assert old_peak < peak < 204_000
    assert not any(d.get("window_protection") for d in decisions)
    # Persisted decisions replay to the same view.
    resumed = Compactor(config=CompactionConfig(**_ITEM2_OFF))
    resumed.rehydrate(p for batch in new for p in batch)
    assert resumed.view(nodes) == c.view(nodes)


def _old_turns(turns: int) -> list[SessionNode]:
    """Complete old turns whose bulk is tool arguments, which only a digest
    reclaims (results stay under the trim size)."""
    nodes: list[SessionNode] = []
    for t in range(turns):
        nodes.extend([
            _node(f"u{t}", "user", [Text(f"turn {t}")]),
            _node(f"a{t}", "assistant", [ToolUseBlock(
                id=f"c{t}", name="Edit", input={"text": "e" * 4_000})]),
            _node(f"r{t}", "user", [ToolResultBlock(
                tool_use_id=f"c{t}", content=[Text("ok " * 100)])]),
            _node(f"f{t}", "assistant", [Text(f"done {t}")]),
        ])
    return nodes


def test_batch_reaches_the_deep_target_on_a_known_window() -> None:
    # Window 100k: trigger 60k, legacy target 0.7 * 60k = 42k, T = 35k.
    nodes = _old_turns(60)
    config = dict(trigger_tokens=1_000, keep_recent_turns=2,
                  trim=TrimConfig(min_result_chars=500, keep_recent_tool_results=2))

    def run(**flags) -> tuple[Compactor, list]:
        c = Compactor(config=CompactionConfig(**config, **flags))
        _, promotions = c.apply(nodes, context_window=100_000)
        return c, promotions

    legacy, old = run(**_ITEM5_OFF)
    deep, new = run()
    assert legacy.last_decision["target_tokens"] == 42_000
    assert deep.last_decision["target_tokens"] == 35_000
    assert 35_000 < old[0].after_tokens <= 42_000
    assert new[0].after_tokens <= 35_000
    # Unknown window: the legacy target, whatever the flag says.
    unknown = Compactor(config=CompactionConfig(**config))
    unknown.apply(nodes)
    assert unknown.last_decision["target_tokens"] == int(1_000 * 0.7)


def _spy_escalate(c: Compactor) -> list[dict]:
    calls: list[dict] = []
    real = c.escalate

    def spy(nodes, **kw):
        calls.append(kw)
        return real(nodes, **kw)

    c.escalate = spy  # type: ignore[method-assign]
    return calls


def test_hard_zone_batch_that_misses_the_trigger_escalates() -> None:
    # Over H mid-turn, with a floor no ordinary rung can take below S: the
    # batch runs the guarded escalation rather than commit a shallow rewrite
    # that the next few calls would repeat.
    nodes = _floor_heavy_turn(40)
    hard = Compactor(config=CompactionConfig())
    calls = _spy_escalate(hard)
    _, promotions = hard.apply(nodes, overhead_tokens=170_000, context_window=_W,
                               output_reserve_tokens=_RESERVE, turn_boundary=False)
    assert promotions
    assert hard.last_decision["reason"] == "hard_ceiling"
    assert hard.last_decision["hard_ceiling"] is True
    assert hard.last_decision["hard_ceiling_tokens"] == 204_000
    assert calls and all(kw["guarded_only"] for kw in calls)
    assert {p.decision_reason for p in promotions} == {"hard_ceiling"}
    # The in-flight turn-start node is never digested by the guarded rungs.
    assert all(start != "start" for start, _end in hard._digested)
    # With the hard zone off the same call is an ordinary batch.
    legacy = Compactor(config=CompactionConfig(hard_window_ratio=0.0))
    legacy_calls = _spy_escalate(legacy)
    legacy.apply(nodes, overhead_tokens=170_000, context_window=_W,
                 output_reserve_tokens=_RESERVE, turn_boundary=False)
    assert legacy.last_decision["reason"] == "meaningful_reclaim"
    assert not legacy_calls


def test_hard_zone_batch_that_reaches_the_trigger_does_not_escalate() -> None:
    # 140 calls of 1.5k tokens: about 213k, between H and L.
    nodes = _floor_heavy_turn(140)
    c = Compactor(config=CompactionConfig())
    calls = _spy_escalate(c)
    _, promotions = c.apply(nodes, overhead_tokens=3_000, context_window=_W,
                            output_reserve_tokens=_RESERVE, turn_boundary=False)
    assert c.last_decision["reason"] == "hard_ceiling"
    assert promotions[0].after_tokens <= c.last_decision["threshold_tokens"]
    assert not calls


def test_shortfall_watermark_holds_mid_turn_and_yields_to_boundaries() -> None:
    from fluid.context.trim import estimate_tokens_of_messages

    nodes = _floor_heavy_turn(60)
    c = Compactor(config=CompactionConfig())
    kw = dict(overhead_tokens=130_000, context_window=_W,
              output_reserve_tokens=_RESERVE)
    committed = None
    for end in range(3, len(nodes) + 1, 2):
        _, promotions = c.apply(nodes[:end], turn_boundary=False, **kw)
        if promotions:
            committed = end
            break
    assert committed is not None
    shortfall = c.last_decision["shortfall_tokens"]
    assert shortfall == (c.last_decision["after_tokens"]
                         - c.last_decision["target_tokens"])
    assert c._shortfall_retry is not None and c._optional_retry is None
    # A later mid-turn call over the trigger that the watermark holds.
    held = None
    for end in range(committed + 2, len(nodes) + 1, 2):
        _, promotions = c.apply(nodes[:end], turn_boundary=False, **kw)
        assert not promotions
        if c.last_decision["reason"] == "awaiting_reclaimable_growth":
            held = end
            assert c.last_decision["shortfall_tokens"] == shortfall
            break
    assert held is not None
    assert 130_000 + estimate_tokens_of_messages(c.view(nodes[:held])) > 163_200
    # The legacy commit clears the watermark, so it never holds that call.
    legacy = Compactor(config=CompactionConfig(shortfall_watermark=False))
    for end in range(3, held + 1, 2):
        legacy.apply(nodes[:end], turn_boundary=False, **kw)
        assert legacy._shortfall_retry is None
    # A turn boundary never waits on it.
    c.apply(nodes[:held], turn_boundary=True, **kw)
    assert c.last_decision["reason"] != "awaiting_reclaimable_growth"


def test_shortfall_watermark_needs_a_known_window() -> None:
    # No window means no hard zone and no window protection to cap the wait,
    # so a commit above its target clears the watermark as before.
    nodes = _floor_heavy_turn(40)
    c = Compactor(config=CompactionConfig(trigger_tokens=60_000))
    _, promotions = c.apply(nodes, overhead_tokens=50_000, turn_boundary=False)
    assert promotions
    assert c.last_decision["after_tokens"] > c.last_decision["target_tokens"]
    assert c._shortfall_retry is None
    assert "shortfall_tokens" not in c.last_decision


# ── Plan item 2 (B3, B2): token-budgeted recency and the in-flight rung ────
#
# The steer-extended turn below follows session e104534c: one user turn of
# 46 calls, results of 2k to 40k chars, a task notification delivered as a
# steer every third call, and a 272k window sized on the measured basis. The
# engine's default output reserve (32k) puts the window-protection limit L
# at 272k - 16k - 32k = 224k.

_ITEM2_SIZES = [2_000, 9_000, 40_000, 4_000, 22_000, 31_000, 6_000, 15_000]
_ITEM2_RESERVE = 32_000
_ITEM2_LIMIT = _W - 16_000 - _ITEM2_RESERVE


def _steer_turn(calls: int, *, arg: int) -> list[SessionNode]:
    from fluid.messages.content import SteerDelivery

    nodes = [_node("start", "user", [
        Text("Fix the git panel. Never force-push main. " + "pad " * 50)])]
    for i in range(calls):
        steers = []
        if i % 3 == 2:
            steers = [SteerDelivery(
                text=f"<task-notification>task {i} finished</task-notification>",
                origin="system", id=f"s{i}")]
        nodes += [
            _node(f"a{i}", "assistant", [ToolUseBlock(
                id=f"c{i}", name="run_python",
                input={"code": f"step {i} " + "x" * arg})]),
            _node(f"r{i}", "user", [ToolResultBlock(
                tool_use_id=f"c{i}",
                content=[Text(f"result-{i} " + "y" * _ITEM2_SIZES[i % 8])],
                delivered_steers=steers, delivery_metadata_version=1)]),
        ]
    return nodes


def _measured_after(rendered: list[Message], overhead: int, ratio: float) -> int:
    from fluid.context.trim import estimate_tokens_of_messages

    return int((overhead + estimate_tokens_of_messages(rendered)) * ratio)


def _drive_steer_turn(config: CompactionConfig, *, overhead: int,
                      arg: int, ratio: float = 0.92) -> tuple[
        Compactor, list[SessionNode], list[dict], list[int], list[list]]:
    """Every model call of the turn, with the billed prompt running at
    ``ratio`` of the chars/4 estimate."""
    from fluid.context.trim import estimate_tokens_of_messages

    c = Compactor(config=config)
    nodes = _steer_turn(46, arg=arg)
    decisions, prompts, batches = [], [], []
    for end in range(1, len(nodes) + 1, 2):
        prefix = nodes[:end]
        measured = int((overhead + estimate_tokens_of_messages(c.view(prefix))) * ratio)
        rendered, promotions = c.apply(
            prefix, overhead_tokens=overhead, context_window=_W,
            output_reserve_tokens=_ITEM2_RESERVE, turn_boundary=end == 1,
            measured_prompt_tokens=measured)
        assert rendered == c.view(prefix)
        decisions.append(dict(c.last_decision))
        prompts.append(_measured_after(rendered, overhead, ratio))
        if promotions:
            batches.append(promotions)
    return c, nodes, decisions, prompts, batches


def _assert_turn_start_and_newest_calls_kept(
    nodes: list[SessionNode], promotions: list, rendered: list[Message],
) -> None:
    index = {n.node_id: i for i, n in enumerate(nodes)}
    newest = {n.node_id for n in nodes[-4:]}  # the newest two calls
    for promo in promotions:
        for start, end, _kind in promo.spans:
            covered = {n.node_id for n in nodes[index[start]:index[end] + 1]}
            assert "start" not in covered, (start, end)
            assert not covered & newest, (start, end)
        assert not {nid for nid, _ in promo.trimmed} & newest
    texts = "\n".join(_texts(rendered))
    assert "Fix the git panel. Never force-push main." in texts
    for node in nodes[-4:]:
        if node.message.role == "user":
            assert _texts([node.message])[0] in texts


def test_steer_extended_turn_stays_under_the_window_limit() -> None:
    # Legacy guards: three protected segments plus the newest five results
    # leave the long turn's run_python arguments out of reach, so the prompt
    # crosses L and window protection rejects its sub-5% candidates.
    _, _, legacy_decisions, legacy_prompts, _ = _drive_steer_turn(
        CompactionConfig(**_LEGACY_FLAGS), overhead=40_000, arg=22_000)
    assert max(legacy_prompts) >= _ITEM2_LIMIT
    assert any(d["reason"] == "insufficient_reclaim" and d["window_protection"]
               for d in legacy_decisions)

    c, nodes, decisions, prompts, batches = _drive_steer_turn(
        CompactionConfig(), overhead=40_000, arg=22_000)
    crossings = [i for i, d in enumerate(decisions) if d["window_protection"]]
    first = crossings[0] if crossings else 0
    # After the first crossing (there is none here) no call carries a prompt
    # at or over L, and the tail kept the turn well under it.
    assert all(p < _ITEM2_LIMIT for p in prompts[first:])
    assert max(prompts) < 0.75 * _W
    assert len(batches) <= len([b for b in legacy_decisions
                                if b["reason"] in ("meaningful_reclaim",
                                                   "hard_ceiling",
                                                   "window_protection")])
    rendered = c.view(nodes)
    _assert_turn_start_and_newest_calls_kept(
        nodes, [p for b in batches for p in b], rendered)
    # Every task notification survives, verbatim or inside a digest.
    texts = "\n".join(str(b) for m in rendered for b in m.content)
    for i in range(2, 46, 3):
        assert f"task {i} finished" in texts, i
    # The decisions replay to the same view.
    resumed = Compactor(config=CompactionConfig())
    resumed.rehydrate(p for b in batches for p in b)
    assert resumed.view(nodes) == rendered


def test_turn_already_over_the_limit_is_reclaimed_below_it() -> None:
    # A 46-call turn that is over L when it is first measured, after a resume
    # or a model switch, over a floor no rung can touch. The candidate and
    # the guarded tiers take everything before the recency tail and are still
    # over L. Only the in-flight rung reaches the tail itself.
    overhead, ratio = 210_000, 0.92
    nodes = _steer_turn(46, arg=1_000)

    def run(**flags) -> tuple[Compactor, list[Message], list]:
        from fluid.context.trim import estimate_tokens_of_messages

        c = Compactor(config=CompactionConfig(**flags))
        estimate = overhead + estimate_tokens_of_messages(c.view(nodes))
        rendered, promotions = c.apply(
            nodes, overhead_tokens=overhead, context_window=_W,
            output_reserve_tokens=_ITEM2_RESERVE, turn_boundary=False,
            measured_prompt_tokens=int(estimate * ratio))
        return c, rendered, promotions

    off, off_rendered, _ = run(turn_budget_window_ratio=0.0)
    assert off.last_decision["window_protection"] is True
    assert _measured_after(off_rendered, overhead, ratio) >= _ITEM2_LIMIT

    c, rendered, promotions = run()
    assert c.last_decision["reason"] == "window_protection"
    assert c.last_decision["turn_live_tokens"] > 0.2 * _W
    budget = [p for p in promotions if p.reason == "turn_budget"]
    assert budget and budget[0].tier == 1
    assert {p.decision_reason for p in promotions} == {"window_protection"}
    assert _measured_after(rendered, overhead, ratio) < _ITEM2_LIMIT
    _assert_turn_start_and_newest_calls_kept(nodes, promotions, rendered)
    # The store is untouched and the decisions replay byte-identically.
    assert all("y" * 2_000 in _texts([n.message])[0]
               for n in nodes if n.node_id.startswith("r"))
    resumed = Compactor(config=CompactionConfig())
    resumed.rehydrate(promotions)
    assert resumed.view(nodes) == rendered


def test_in_flight_rung_digests_segments_only_at_the_limit() -> None:
    # Over L with nothing left to trim, the rung digests completed in-flight
    # segments inside the recency tail, up to the newest calls. Under L it
    # leaves the tail alone.
    from fluid.context.trim import estimate_tokens_of_messages

    nodes = _floor_heavy_turn(60)
    window, reserve = 100_000, 4_000
    limit = window - 16_000 - reserve
    tail_from = index_of(nodes, "a30")

    def prepared() -> Compactor:
        c = Compactor(config=CompactionConfig())
        # Everything trimmable is already trimmed, so only digests reclaim.
        c.trimmer.mark_trimmed(c.trimmer._eligible(nodes, keep_recent=0))
        c._tail_from, c._turn_live = tail_from, 100_000
        return c

    # What the guarded digest rung can reach on its own: segments ending
    # before the tail.
    probe = prepared()
    probe._digest_until(nodes, overhead_tokens=0, target=0)
    floor = estimate_tokens_of_messages(probe.view(nodes))

    def escalate(over_limit_by: int) -> tuple[Compactor, list]:
        c = prepared()
        overhead = limit - floor + over_limit_by
        promotions = c.escalate(nodes, overhead_tokens=overhead,
                                context_window=window,
                                output_reserve_tokens=reserve, guarded_only=True)
        return c, promotions

    # 5k over: more than the fold of the guarded digests frees (about 1k).
    c, promotions = escalate(5_000)
    digests = [p for p in promotions if p.reason == "turn_budget"]
    assert digests and all(p.tier == 2 for p in digests)
    index = {n.node_id: i for i, n in enumerate(nodes)}
    newest_call = len(nodes) - 4
    for promo in digests:
        for start, end, _kind in promo.spans:
            assert start != "start"
            assert tail_from <= index[end] < newest_call
    assert c._estimate(nodes, limit - floor + 5_000) < limit

    _, promotions = escalate(-1_000)
    assert not [p for p in promotions if p.reason == "turn_budget"]


def test_window_protection_floor_is_waived_near_the_window() -> None:
    # The test above pins the 5% floor under window protection. At 90% of
    # the window and above, any reclaim of 2k tokens or more commits instead
    # (the 33dc66c6 turn rejected 13 candidates of 7k to 16k units that way
    # while the prompt climbed to 98.5% of the window).
    from fluid.context.trim import estimate_tokens_of_messages

    nodes = _growing_turn(50, metadata=False)
    no_tail = {**_ITEM2_OFF}
    no_tail.pop("window_floor_waiver_ratio")

    def run(**flags) -> tuple[Compactor, list, int]:
        c = Compactor(config=CompactionConfig(
            trim=TrimConfig(keep_recent_tool_results=40), **flags))
        before = estimate_tokens_of_messages(c.view(nodes))
        window = before + c.config.headroom_tokens + 4_000 - 1
        _, promotions = c.apply(nodes, turn_boundary=False, context_window=window,
                                output_reserve_tokens=4_000)
        return c, promotions, before

    floor, promotions, before = run(**_ITEM2_OFF)
    assert not promotions
    assert floor.last_decision["reason"] == "insufficient_reclaim"
    assert floor.last_decision["window_protection_misses"] == 1
    reclaim = before - floor.last_decision["candidate_after_tokens"]
    assert 2_000 <= reclaim < before * 0.05

    waived, promotions, _ = run(**no_tail)
    assert promotions
    assert waived.last_decision["reason"] == "window_protection"
    assert waived.last_decision["window_protection_misses"] == 0
    assert waived.last_decision["after_tokens"] == before - reclaim


def test_trim_eligibility_uses_the_tail_only_with_a_known_window() -> None:
    from fluid.context.trim import ToolResultTrimmer

    nodes = _steer_turn(12, arg=100)
    trimmer = ToolResultTrimmer(config=TrimConfig(keep_recent_tool_results=5))
    legacy = trimmer._eligible(nodes)
    assert ("r6", "c6") in legacy and ("r7", "c7") not in legacy
    # A protected-from index replaces the count: results before it are in
    # reach whatever the count says, results from it on are kept.
    tailed = trimmer._eligible(nodes, protected_from=index_of(nodes, "a10"))
    assert ("r9", "c9") in tailed and ("r10", "c10") not in tailed
    # An explicit keep_recent (overflow recovery) still wins.
    assert trimmer._eligible(nodes, keep_recent=0, protected_from=1) == (
        trimmer._eligible(nodes, keep_recent=0))

    c = Compactor()
    assert c._recency_tail_start(nodes, None, 1.0) is None
    start = c._recency_tail_start(nodes, _W, 1.0)
    # The tail never holds fewer than the newest two calls.
    assert start is not None and start <= index_of(nodes, "a10")
    trim_only = Compactor(config=CompactionConfig(enabled=False))
    assert trim_only._recency_tail_start(nodes, _W, 1.0) is None


def index_of(nodes: list[SessionNode], node_id: str) -> int:
    return next(i for i, n in enumerate(nodes) if n.node_id == node_id)


def _wake_history() -> list[SessionNode]:
    """Two real user turns, five wake turns, then an in-flight wake turn.
    Every turn carries one large result and one argument-heavy call."""
    nodes: list[SessionNode] = []
    for t, name in enumerate(["u0", "u1", "w2", "w3", "w4", "w5", "w6", "w7"]):
        nodes += [
            _node(name, "user", [Text(f"turn {name} " + "pad " * 40)]),
            _node(f"{name}_a", "assistant", [ToolUseBlock(
                id=f"{name}_c", name="Edit", input={"text": "e" * 3_000})]),
            _node(f"{name}_r", "user", [ToolResultBlock(
                tool_use_id=f"{name}_c", content=[Text("ok " * 400)])]),
            _node(f"{name}_f", "assistant", [Text(f"done {name}")]),
        ]
    return nodes


def test_wake_turns_do_not_age_out_real_user_turns() -> None:
    nodes = _wake_history()
    wakes = {"w2", "w3", "w4", "w5", "w6", "w7"}

    def digested(c: Compactor) -> set[str]:
        _, promotions = c.apply(nodes)
        return {s for p in promotions for s, _e, _k in p.spans}

    legacy = Compactor(config=_cfg(keep_recent_turns=3))
    assert {"u0", "u1"} <= digested(legacy)

    aware = Compactor(config=_cfg(keep_recent_turns=3))
    aware.wake_turn_starts = set(wakes)
    starts = digested(aware)
    assert starts and not starts & {"u0", "u1", "w5", "w6", "w7"}
    assert starts <= {"w2", "w3", "w4"}

    # The flag off restores the legacy count even with sources known.
    off = Compactor(config=_cfg(keep_recent_turns=3, wake_aware_turns=False))
    off.wake_turn_starts = set(wakes)
    assert {"u0", "u1"} <= digested(off)


async def test_engine_reads_wake_turn_sources_from_turn_started(tmp_path) -> None:
    import json

    from fluid.events.model import TurnStarted
    from fluid.store.base import persist_event

    sid = "ses_wake"
    rows = [
        TurnStarted(session_id=sid, node_id="u0", turn_id="t0", seq=1,
                    model="m", provider="p", source="user"),
        TurnStarted(session_id=sid, node_id="w1", turn_id="t1", seq=2,
                    model="m", provider="p", source="unpark"),
        TurnStarted(session_id=sid, node_id="w2", turn_id="t2", seq=3,
                    model="m", provider="p", source="agent_message"),
        TurnStarted(session_id=sid, node_id="old", turn_id="t3", seq=4,
                    model="m", provider="p"),  # a row from before sources
        # A resumed turn re-reports w1 as a user turn; the first row decides.
        TurnStarted(session_id=sid, node_id="w1", turn_id="t4", seq=5,
                    model="m", provider="p", source="user"),
    ]
    (tmp_path / f"{sid}.events.jsonl").write_text(
        "".join(json.dumps(event_to_dict(r)) + "\n" for r in rows),
        encoding="utf-8")
    store = JsonlStore(tmp_path)
    loop = NativeLoop(provider=_ScriptedProvider([_text_script("done")]),
                      store=store, model="m", session_id=sid)
    await loop._note_wake_turn_starts(sid)
    assert loop._compactor.wake_turn_starts == {"w1", "w2"}
    await persist_event(store, TurnStarted(
        session_id=sid, node_id="w3", turn_id="t5", seq=6,
        model="m", provider="p", source="wakeup"))
    await loop._note_wake_turn_starts(sid)
    assert loop._compactor.wake_turn_starts == {"w1", "w2", "w3"}
    await loop.aclose()
