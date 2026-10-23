"""Compactor.frozen_prefix / window: what a live store may stop holding.

The newest checkpoint renders in place of root..end, so only the root, the
end and the pinned-reminder chain of that prefix are read. The window must
render byte-identically to the whole path, keep the chain a fold carries, and
be idempotent so a store holding only the window serves the same list.
"""
from dataclasses import replace

from fluid.context.compaction import CompactionConfig, Compactor
from fluid.engine.native import NativeLoop
from fluid.messages import Message, Text
from fluid.store.base import MemoryStore, SessionNode


def path(turns=12, memory=()):
    """Alternating user/assistant turns; ``memory`` maps turn -> reminder."""
    out = []
    for i in range(turns):
        for role in ("user", "assistant"):
            text = f"turn {i}: " + "word " * 2000
            message = Message(role=role, content=[Text(text=text)])
            if role == "user" and i in dict(memory):
                note = dict(memory)[i]
                message = Message(role=role, content=[Text(text=text + "\n\n" + note)])
                message.pin_context(note)
            out.append(SessionNode(node_id=f"node_{i}_{role}",
                                   parent_id=out[-1].node_id if out else None, message=message))
    return out


def reminder(header, body):
    return f"<system-reminder>\n{header}{body}\n</system-reminder>"


def checkpointed(nodes):
    config = replace(CompactionConfig(summary_enabled=True, keep_recent_turns=1,
                                      recency_tail_tokens=0, max_resident_span_digests=1000))
    c = Compactor(config=config)
    c._digest_until(nodes, overhead_tokens=0, target=0)
    region = c.summary_region(nodes)
    assert c.apply_summary(nodes, (*region[:2], "checkpoint text"))
    return c, region[1]


def test_no_checkpoint_means_no_window():
    nodes = path()
    c = Compactor(config=CompactionConfig(summary_enabled=True))
    assert c.frozen_prefix(nodes) is None
    assert c.window(nodes) is nodes


def test_window_renders_identically_and_is_idempotent():
    nodes = path()
    c, end = checkpointed(nodes)
    frozen = c.frozen_prefix(nodes)
    assert frozen is not None and frozen[0] == end
    assert frozen[1] == {nodes[0].node_id, end}
    window = c.window(nodes)
    stop = [n.node_id for n in nodes].index(end)
    assert [n.node_id for n in window] == [nodes[0].node_id, *[n.node_id for n in nodes[stop:]]]
    assert c.view(window) == c.view(nodes)
    assert c.window(window) == window
    assert c.frozen_prefix(window) == frozen


def test_window_keeps_the_pinned_memory_chain_from_the_newest_full_block():
    memory = {
        1: reminder("# Memory\n", "- [m1] old snapshot"),
        2: reminder("# Memory changes\n", "+ [m2] superseded change"),
        4: reminder("# Memory\n", "- [m1] newer snapshot"),
        6: reminder("# Memory changes\n", "+ [m3] change after it"),
    }
    nodes = path(memory=memory)
    c, end = checkpointed(nodes)
    _, keep = c.frozen_prefix(nodes)
    stop = [n.node_id for n in nodes].index(end)
    assert stop > nodes.index(next(n for n in nodes if n.node_id == "node_6_user"))
    # Older chain links before the newest full snapshot are not anchors.
    assert keep == {nodes[0].node_id, end, "node_4_user", "node_6_user"}
    window = c.window(nodes)
    view = c.view(window)
    assert view == c.view(nodes)
    assert "newer snapshot" in view[0].text() and "change after it" in view[0].text()


async def test_engine_context_path_matches_the_full_path_view():
    """A store without load_window: the engine windows load_path itself."""
    nodes = path()
    store = MemoryStore()
    meta = await store.create_session()
    for item in nodes:
        await store.append(meta.session_id, item)
    loop = NativeLoop(provider=None, store=store, model="fixture",
                      compaction=CompactionConfig(summary_enabled=True))
    loop._compactor, _end = checkpointed(nodes)
    window = await loop._context_path(meta.session_id)
    assert loop._compactor.view(window) == loop._compactor.view(nodes)
    assert len(window) < len(nodes)
    # Reclaimed tokens still describe the whole path.
    stats = loop._compaction_stats(window, None, sid=meta.session_id)
    assert stats["reclaimedTokens"] == loop._compactor.stats(nodes)["reclaimedTokens"]
    # The store still has every node.
    assert await store.load_path(meta.session_id) == nodes
    await loop.aclose()
