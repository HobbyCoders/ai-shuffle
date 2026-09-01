"""Tier-1-only trim mode — ``CompactionConfig(enabled=False, trim=…enabled)``.

Host apps embedding fluid want tool-result trimming (recoverable placeholders,
stored tree untouched) without the model ever reading a mechanical digest of
its own history. This pins that contract: tier 1 runs, tiers 2 and 3 never do —
not on the trigger, not on an overflow escalation, and not from decisions
rehydrated out of a session that *did* run the ladder.
"""

from fluid.context.compaction import (
    EPOCH_STUB,
    SPAN_DIGEST,
    Compactor,
    CompactionConfig,
    Promotion,
)
from fluid.context.trim import TrimConfig
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.store.base import SessionNode

TRIM_ONLY = CompactionConfig(enabled=False, trim=TrimConfig(enabled=True))


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(
        node_id=nid, parent_id=None, message=Message(role=role, content=blocks)
    )


def _history(turns: int = 30, result_chars: int = 20_000) -> list[SessionNode]:
    """A path of complete turns: user → assistant(tool_use) → tool result."""
    nodes: list[SessionNode] = []
    for t in range(turns):
        nodes += [
            _node(f"node_u{t}", "user", [Text(f"user asks about topic-{t}")]),
            _node(f"node_a{t}", "assistant", [
                ToolUseBlock(id=f"call{t}", name="Read", input={"file_path": "f.py"}),
            ]),
            _node(f"node_r{t}", "user", [
                ToolResultBlock(
                    tool_use_id=f"call{t}", content=[Text("R" * result_chars)]
                )
            ]),
        ]
    return nodes


def _texts(messages: list[Message]) -> str:
    out: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, Text):
                out.append(block.text)
            for inner in getattr(block, "content", None) or ():
                if isinstance(inner, Text):
                    out.append(inner.text)
    return "\n".join(out)


def test_trim_only_trims_old_results_without_digesting_anything() -> None:
    compactor = Compactor(config=TRIM_ONLY)
    assert compactor.trim_only and compactor.active
    nodes = _history()

    rendered, promotions = compactor.apply(nodes)

    assert [p.tier for p in promotions] == [1]
    assert promotions[0].spans == ()
    assert promotions[0].trimmed  # old results really were trimmed
    text = _texts(rendered)
    assert "old tool result trimmed to save context" in text
    # Zero span digests, zero epoch stubs — at any tier, in any spelling.
    assert "compacted turn" not in text
    assert "epoch " not in text
    assert compactor._digested == {}
    stats = compactor.stats(nodes)
    assert stats["digestedSpans"] == 0 and stats["epochStubs"] == 0
    assert stats["trimmedToolResults"] > 0
    # One message out per node: nothing was collapsed away.
    assert len(rendered) == len(nodes)


def test_trim_only_keeps_recent_results_and_the_stored_tree_intact() -> None:
    compactor = Compactor(config=TRIM_ONLY)
    nodes = _history()
    rendered, _ = compactor.apply(nodes)
    # The newest results are untouched (tier-1's keep-recent rule).
    assert "R" * 1000 in _texts(rendered[-1:])
    # The tree itself is never mutated.
    assert all(
        "R" * 1000 in b.content[0].text
        for n in nodes
        for b in n.message.content
        if isinstance(b, ToolResultBlock)
    )


def test_trim_only_escalation_stops_at_tier_one() -> None:
    compactor = Compactor(config=TRIM_ONLY)
    nodes = _history()
    first = compactor.escalate(nodes)
    assert [p.tier for p in first] == [1]
    # Old results are gone; the second overflow drops the recency guard and
    # trims the newest ones. Still tier 1: trim-only never digests a turn.
    second = compactor.escalate(nodes)
    assert [p.tier for p in second] == [1]
    assert all(p.reclaimed_chars > 0 for p in second)
    # Nothing left to trim → no rung 2/3 consolation prize.
    assert compactor.escalate(nodes) == []
    assert compactor._digested == {}


def test_trim_only_ignores_rehydrated_span_promotions() -> None:
    """A session that ran the full ladder, resumed in trim-only mode."""
    compactor = Compactor(config=TRIM_ONLY)
    nodes = _history()
    compactor.rehydrate([
        Promotion(tier=2, spans=(("node_u0", "node_r0", SPAN_DIGEST),)),
        Promotion(tier=3, spans=(("node_u1", "node_r1", EPOCH_STUB),)),
        Promotion(tier=1, trimmed=(("node_r2", "call2"),)),
    ])
    view = compactor.view(nodes)
    text = _texts(view)
    assert "compacted turn" not in text and "folded" not in text
    assert len(view) == len(nodes)
    # The tier-1 decision *is* re-applied — only the ladder above it is inert.
    assert "old tool result trimmed" in text


def test_fully_disabled_still_means_nothing_happens() -> None:
    compactor = Compactor(
        config=CompactionConfig(enabled=False, trim=TrimConfig(enabled=False))
    )
    nodes = _history()
    assert not compactor.active and not compactor.trim_only
    rendered, promotions = compactor.apply(nodes)
    assert promotions == []
    assert rendered == [n.message for n in nodes]
    assert compactor.escalate(nodes) == []


def test_full_ladder_still_digests() -> None:
    """The default contract is unchanged by the trim-only seam."""
    compactor = Compactor(config=CompactionConfig(trigger_tokens=5_000))
    nodes = _history()
    _rendered, promotions = compactor.apply(nodes)
    assert {p.tier for p in promotions} >= {1, 2}
    assert compactor.stats(nodes)["digestedSpans"] > 0


def test_context_config_maps_trim_only_onto_the_contract() -> None:
    from fluid.config.model import ContextConfig

    cfg = ContextConfig.from_dict({"trim_only": True})
    assert cfg.enabled and cfg.trim_only
    # The loader's mapping (config/loader.py) is the same two lines.
    trim_only = cfg.enabled and cfg.trim_only
    compaction = CompactionConfig(
        enabled=cfg.enabled and not trim_only,
        trim=TrimConfig(enabled=cfg.enabled),
    )
    assert Compactor(config=compaction).trim_only

    off = ContextConfig.from_dict({"enabled": False, "trim_only": True})
    assert not (off.enabled and off.trim_only)  # inert when the section is off
