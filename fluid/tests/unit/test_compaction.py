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
    c = Compactor()
    batches = []
    for count in range(1, 51):
        _, promotions = c.apply(_growing_turn(count, metadata=False), turn_boundary=False)
        if promotions:
            first = promotions[0]
            assert first.before_tokens - first.after_tokens >= int(first.before_tokens * 0.05)
            assert first.first_changed_message is not None
            batches.append(promotions)
    assert batches
    assert len(batches) < 19
    assert not c._digested  # No unsafe fallback to parsing tool text.


def test_optional_rejection_rolls_back_and_overflow_bypasses_watermark() -> None:
    from fluid.context.trim import estimate_tokens_of_messages

    nodes = _growing_turn(50, metadata=False)
    c = Compactor(config=CompactionConfig(trim=TrimConfig(keep_recent_tool_results=49)))
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
    assert promotions
    assert 0 < before - estimate_tokens_of_messages(rendered) < before * 0.05
    assert promotions[0].decision_reason == "window_protection"


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
