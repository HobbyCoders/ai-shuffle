"""Render surface of the compaction ladder: render versions and steer receipts.

Version 1 is the original renderer and must stay byte-for-byte, because
decisions persisted before render_version existed replay through it. Version 2
turns large harness notices (origin "system") into receipts once their result
is trimmed, digested or folded; user and peer steers stay verbatim.
"""

import re

from fluid.context.compaction import (
    SPAN_DIGEST,
    CompactionConfig,
    Compactor,
    Promotion,
    render_epoch_stub,
    render_span_digest,
    split_spans,
)
from fluid.context.recall import expand_ref
from fluid.context.steer_render import render_steers
from fluid.context.trim import TrimConfig
from fluid.events import ContextCompaction
from fluid.events.model import event_from_dict, event_to_dict
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.messages.content import SteerDelivery
from fluid.store.base import SessionNode

NOTICE = (
    "<task-notification>\n<task-id>bg-7</task-id>\n<status>completed</status>\n"
    "<result>" + "report line with detail 42\n" * 1_400 + "</result>\n"
    "</task-notification>"
)
_EXPAND_RE = re.compile(r'HistoryExpand\("(node_[0-9a-f]+)", offset=(\d+)\)')


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _turn(t: int, deliveries: list[SteerDelivery]) -> list[SessionNode]:
    """A user turn whose one tool result carries ``deliveries``, stored the
    way the engine stores them: rendered into the result text as well."""
    text = f"result-{t} " + "x" * 3_000 + "".join(d.render() for d in deliveries)
    return [
        _node(f"node_{t}a", "user", [Text(f"please look at topic {t}")]),
        _node(f"node_{t}b", "assistant", [
            ToolUseBlock(id=f"call{t}", name="Read", input={"file_path": f"f{t}.py"}),
        ]),
        _node(f"node_{t}c", "user", [ToolResultBlock(
            tool_use_id=f"call{t}", content=[Text(text)],
            delivered_steers=deliveries, delivery_metadata_version=1,
        )]),
        _node(f"node_{t}d", "assistant", [Text(f"done with {t}")]),
    ]


def _system(text: str = NOTICE, id: str = "n1") -> SteerDelivery:
    return SteerDelivery(text=text, origin="system", id=id)


def _flat(messages: list[Message]) -> str:
    parts: list[str] = []
    for m in messages:
        for b in m.content:
            if isinstance(b, Text):
                parts.append(b.text)
            elif isinstance(b, ToolResultBlock):
                parts.extend(c.text for c in b.content if isinstance(c, Text))
    return "\n".join(parts)


def _trim_only(render_version: int = 2) -> Compactor:
    return Compactor(config=CompactionConfig(
        enabled=False, render_version=render_version,
        trim=TrimConfig(keep_recent_tool_results=0, min_result_chars=500),
    ))


def test_user_and_peer_steers_stay_verbatim_in_every_render_site() -> None:
    long = "Keep the release gate closed until QA signs off. " * 60
    user = SteerDelivery(text=long, id="u1")
    peer = SteerDelivery(text=long + " (peer)", sender="planner", id="p1")
    legacy_peer = SteerDelivery(text=long + " (legacy)", origin="peer",
                                sender="planner", id="p2")
    steers = [user, peer, legacy_peer]
    nodes = _turn(0, steers)
    config = CompactionConfig()

    trimmer = _trim_only()
    trimmer.trimmer.trim_batch(nodes)
    digest = render_span_digest(nodes, config)
    epoch = render_epoch_stub(split_spans(nodes), config)
    for view in (trimmer.view(nodes), [digest], [epoch]):
        flat = _flat(view)
        for steer in steers:
            assert steer.render().strip() in flat
        assert "receipt" not in flat


def test_large_system_notice_renders_as_receipt_with_working_ref() -> None:
    assert len(NOTICE) > 36_000
    nodes = _turn(0, [_system()])
    config = CompactionConfig()
    trimmed = _trim_only()
    trimmed.trimmer.trim_batch(nodes)
    views = {
        "trim": trimmed.view(nodes),
        "digest": [render_span_digest(nodes, config)],
        "epoch": [render_epoch_stub(split_spans(nodes), config)],
    }
    for site, view in views.items():
        flat = _flat(view)
        assert NOTICE not in flat, site
        assert "harness notice receipt" in flat, site
        assert f"{len(NOTICE)} chars delivered at node_0c" in flat, site
        assert "<task-notification> <task-id>bg-7</task-id>" in flat, site
        assert len(flat) < 8_000, site
        ref, offset = _EXPAND_RE.search(flat).groups()
        text, is_error = expand_ref(nodes, ref, offset=int(offset))
        assert not is_error
        assert text.split("\n", 1)[1].startswith(NOTICE[:5_000]), site


def test_short_or_unrecoverable_notices_stay_verbatim() -> None:
    short = _system("<task-notification>done</task-notification>")
    nodes = _turn(0, [short])
    digest = render_span_digest(nodes, CompactionConfig())
    assert short.render() in digest.content[0].text

    # Recall off: a receipt would point at a tool the model does not have.
    nodes = _turn(1, [_system()])
    no_recall = render_span_digest(nodes, CompactionConfig(recall_tools=False))
    assert NOTICE in no_recall.content[0].text

    # A notice the stored node does not carry cannot be re-read, so it stays.
    block = nodes[2].message.content[0]
    detached = ToolResultBlock(
        tool_use_id=block.tool_use_id, content=[Text("x" * 3_000)],
        delivered_steers=block.delivered_steers, delivery_metadata_version=1,
    )
    nodes[2] = _node("node_1c", "user", [detached])
    digest = render_span_digest(nodes, CompactionConfig())
    assert NOTICE in digest.content[0].text


def test_identical_notices_delivered_together_collapse_to_one_receipt() -> None:
    other = _system(NOTICE.replace("bg-7", "bg-8"), id="n3")
    user = SteerDelivery(text="stop after this step", id="u1")
    steers = [_system(id="n1"), user, _system(id="n2"), other, user]
    nodes = _turn(0, steers)
    rendered = render_steers(steers, CompactionConfig().steer_render(), node=nodes[2])
    assert len(rendered) == 4
    assert "(2 identical copies delivered together)" in rendered[0]
    assert rendered[1] == user.render()
    assert "bg-8" in rendered[2] and "identical copies" not in rendered[2]
    assert rendered[3] == user.render()

    trimmed = _trim_only()
    trimmed.trimmer.trim_batch(nodes)
    flat = _flat(trimmed.view(nodes))
    assert flat.count("harness notice receipt") == 2
    assert flat.count(user.render().strip()) == 2


def test_v1_renderers_keep_every_steer_verbatim() -> None:
    steers = [_system(id="n1"), _system(id="n2")]
    nodes = _turn(0, steers)
    config = CompactionConfig(render_version=1)
    digest = render_span_digest(nodes, config)
    assert digest.content[0].text.count(NOTICE) == 2
    epoch = render_epoch_stub(split_spans(nodes), config)
    assert [b.text for b in epoch.content[1:]] == [s.render() for s in steers]
    v1 = _trim_only(render_version=1)
    v1.trimmer.trim_batch(nodes)
    assert _flat(v1.view(nodes)).count(NOTICE) == 2


def _persisted(promotions: list[Promotion], render_version: int | None) -> list[Promotion]:
    """Round-trip decisions through ContextCompaction rows the way the engine
    does. ``render_version=None`` writes a row from before the field existed."""
    out: list[Promotion] = []
    for promo in promotions:
        row = event_to_dict(ContextCompaction(
            session_id="s", turn_id="t", tier=promo.tier, reason=promo.reason,
            trimmed=[list(p) for p in promo.trimmed],
            spans=[list(s) for s in promo.spans],
            render_version=render_version or 1,
        ))
        if render_version is None:
            row.pop("render_version")
        event = event_from_dict(row)
        assert isinstance(event, ContextCompaction)
        out.append(Promotion(
            tier=event.tier, reason=event.reason,
            trimmed=tuple((n, t) for n, t in event.trimmed),
            spans=tuple((s, e, k) for s, e, k in event.spans),
            render_version=event.render_version,
        ))
    return out


def test_v1_decisions_resume_byte_identical_under_a_v2_compactor() -> None:
    nodes = [*_turn(0, [_system()]), *_turn(1, []), *_turn(2, [])]
    # The pre-upgrade process: everything it decided rendered at version 1.
    old = Compactor(config=CompactionConfig(
        render_version=1,
        trim=TrimConfig(keep_recent_tool_results=0, min_result_chars=500),
    ))
    trimmed = old.trimmer.trim_batch(nodes)
    old._mark_span("node_1a", "node_1d", SPAN_DIGEST)
    before = old.view(nodes)
    assert _flat(before).count(NOTICE) == 1
    promotions = [Promotion(tier=1, trimmed=tuple(trimmed)),
                  Promotion(tier=2, spans=(("node_1a", "node_1d", SPAN_DIGEST),))]

    legacy_rows = _persisted(promotions, render_version=None)
    assert {p.render_version for p in legacy_rows} == {1}
    resumed = Compactor(config=CompactionConfig(
        trim=TrimConfig(keep_recent_tool_results=0, min_result_chars=500),
    ))
    assert resumed.render_version == 2
    resumed.rehydrate(legacy_rows)
    assert resumed.view(nodes) == before

    # A decision the upgraded process makes afterwards renders at version 2,
    # while the old ones keep their text.
    resumed._mark_span("node_0a", "node_0d", SPAN_DIGEST)
    after = _flat(resumed.view(nodes))
    assert NOTICE not in after and "harness notice receipt" in after

    # Trim-only (the host contract) replays old trims at version 1 too.
    trim_only = _trim_only()
    trim_only.rehydrate(legacy_rows)
    v1_trim_only = _trim_only(render_version=1)
    v1_trim_only.rehydrate(legacy_rows)
    assert trim_only.view(nodes) == v1_trim_only.view(nodes)


def test_v2_decisions_resume_byte_identical() -> None:
    nodes = [*_turn(0, [_system()]), *_turn(1, [_system(id="n9")])]
    live = _trim_only()
    pairs = live.trimmer.trim_batch(nodes)
    before = live.view(nodes)
    assert "harness notice receipt" in _flat(before)
    rows = _persisted([Promotion(tier=1, trimmed=tuple(pairs))], render_version=2)
    resumed = _trim_only()
    resumed.rehydrate(rows)
    assert resumed.view(nodes) == before


def test_rolled_back_rung_restores_span_versions() -> None:
    nodes = _turn(0, [_system()])
    c = Compactor()
    c.rehydrate([Promotion(tier=2, spans=(("node_0a", "node_0d", SPAN_DIGEST),))])
    before = c.view(nodes)
    restore = c._tier_snapshot()
    c._mark_span("node_0a", "node_0d", SPAN_DIGEST)  # re-marked at version 2
    assert c.view(nodes) != before
    restore()
    assert c.view(nodes) == before
