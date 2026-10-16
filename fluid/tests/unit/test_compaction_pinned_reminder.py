"""A fold never hides the newest pinned reminder chain.

The host sends its ``# Memory`` snapshot once and ``# Memory changes`` lists
after it, not on every message. When a digest, epoch, checkpoint summary or
intake stub folds the node carrying one of them, the fold's message carries
the block verbatim, so every model call still shows the snapshot.
"""
from __future__ import annotations

from dataclasses import replace

from fluid.context.compaction import (
    CompactionConfig,
    Compactor,
    pinned_reminder_chain,
)
from fluid.messages import Message
from fluid.messages.content import Text
from fluid.store.base import SessionNode

FULL = "<system-reminder>\n# Memory\nSaved facts.\n\n- [m1 r1; user] The port is 8001.\n</system-reminder>"
FULL2 = "<system-reminder>\n# Memory\nSaved facts.\n\n- [m1 r2; user] The port is 8002.\n</system-reminder>"
CHANGES = "<system-reminder>\n# Memory changes\nNew or updated:\n- [m2 r1; user] Tests use pytest.\n</system-reminder>"
OTHER = "<system-reminder>\nSession ledger (x)\n</system-reminder>"


def _path(*user_texts: str) -> list[SessionNode]:
    nodes, parent = [], None
    for i, text in enumerate(user_texts):
        for role, content in (("user", text), ("assistant", f"reply {i} " + "r " * 400)):
            node_id = f"n{len(nodes)}"
            nodes.append(SessionNode(node_id=node_id, parent_id=parent,
                                     message=Message(role=role, content=[Text(content)])))
            parent = node_id
    return nodes


def _view_text(compactor: Compactor, nodes) -> str:
    return "\n".join(m.text() or "" for m in compactor.view(nodes))


def test_chain_is_the_newest_full_block_and_the_change_lists_after_it():
    headers = CompactionConfig().pinned_reminder
    nodes = _path(f"a\n\n{FULL}\n\n{OTHER}", f"b\n\n{CHANGES}", f"c\n\n{FULL2}", "d", f"e\n\n{CHANGES}")
    assert pinned_reminder_chain(nodes, headers) == {4: [FULL2], 8: [CHANGES]}
    assert pinned_reminder_chain(_path("a", "b"), headers) == {}
    assert pinned_reminder_chain(nodes, None) == {}


def _digested(*user_texts: str, **config):
    compactor = Compactor(config=CompactionConfig(trigger_tokens=500, keep_recent_turns=1, **config))
    nodes = _path(*user_texts)
    compactor.apply(nodes, context_window=None)
    return compactor, nodes


def test_a_digest_carries_the_folded_snapshot_verbatim():
    compactor, nodes = _digested(f"first ask\n\n{FULL}", f"second\n\n{CHANGES}", "third", "fourth")
    assert compactor._digest_by_start or compactor._epoch_by_start, "nothing was folded"
    text = _view_text(compactor, nodes)
    assert text.count(FULL) == 1 and text.count(CHANGES) == 1
    # Without the pin the digest strips it: the reason the pin exists.
    compactor.config = replace(compactor.config, pinned_reminder=None)
    assert FULL not in _view_text(compactor, nodes)


def test_a_snapshot_superseded_before_the_fold_is_not_carried():
    compactor, nodes = _digested(f"one\n\n{FULL}", f"two\n\n{FULL2}", "three", "four")
    text = _view_text(compactor, nodes)
    assert FULL2 in text and FULL not in text


def test_a_session_with_a_snapshot_on_every_turn_carries_none_onto_its_folds():
    # Sessions from before send-once carry a full snapshot on every message.
    # Each was superseded by the time its turn folded: no fold grows by one.
    snapshots = [FULL.replace("8001", str(9000 + k)) for k in range(6)]
    compactor, nodes = _digested(*(f"turn {k}\n\n{snap}" for k, snap in enumerate(snapshots)))
    assert compactor._digest_by_start, "nothing was folded"
    view = compactor.view(nodes)
    live = [n for n in nodes if n.node_id not in compactor._digest_by_start]
    text = "\n".join(m.text() or "" for m in view)
    assert sum(text.count(snap) for snap in snapshots) < len(snapshots)
    assert snapshots[-1] in text
    folded_text = "\n".join(
        m.text() or "" for m, n in zip(view, nodes) if n.node_id in compactor._digest_by_start
    )
    assert "# Memory" not in folded_text and live


def test_a_live_snapshot_is_not_duplicated():
    compactor, nodes = _digested("one", "two", "three", f"four\n\n{FULL}")
    assert _view_text(compactor, nodes).count(FULL) == 1


def test_the_view_is_byte_identical_on_every_render():
    compactor, nodes = _digested(f"first\n\n{FULL}", "second", "third", "fourth")
    assert [m.to_dict() for m in compactor.view(nodes)] == [m.to_dict() for m in compactor.view(nodes)]


def test_an_intake_stub_carries_the_snapshot():
    compactor = Compactor(config=CompactionConfig(intake_stub_chars=200))
    nodes = _path("x" * 500 + f"\n\n{FULL}")
    text = _view_text(compactor, nodes)
    assert "large user attachment" in text and text.count(FULL) == 1


def test_a_later_full_snapshot_never_rewrites_an_older_fold():
    # The host sends a new full snapshot once change lists outgrow half of
    # one (or after a rewind). The fold made earlier keeps carrying the copy
    # it folded, so the rendered prefix (and the provider's cache) is intact.
    compactor, nodes = _digested(f"a\n\n{FULL}", f"b\n\n{CHANGES}", "c", "d", "e")
    assert compactor._digest_by_start or compactor._epoch_by_start, "nothing was folded"
    before = [m.to_dict() for m in compactor.view(nodes)]
    later = nodes + _path(f"f\n\n{FULL2}")[:1]
    later[-1] = replace(later[-1], node_id="n_later", parent_id=nodes[-1].node_id)
    after = [m.to_dict() for m in compactor.view(later)]
    assert after[:len(before)] == before
    text = _view_text(compactor, later)
    # The newest snapshot is in view; the superseded one stays in its fold.
    assert text.count(FULL2) == 1 and text.count(FULL) == 1 and text.count(CHANGES) == 1


def test_a_fold_spanning_two_snapshots_carries_only_the_newer_one():
    compactor = Compactor(config=CompactionConfig(summary_enabled=True))
    nodes = _path(f"one\n\n{FULL}", f"two\n\n{FULL2}", f"three\n\n{CHANGES}", "four")
    promotion = compactor.apply_summary(nodes, (nodes[0].node_id, nodes[5].node_id, "Summary."))
    assert promotion is not None, "the summary was not committed"
    text = _view_text(compactor, nodes)
    assert "Summary." in text
    assert FULL2 in text and FULL not in text and text.count(CHANGES) == 1


def test_a_fold_after_the_snapshot_carries_its_change_lists():
    from fluid.context.compaction import _PinScan

    headers = CompactionConfig().pinned_reminder
    nodes = _path(f"a\n\n{FULL}", f"b\n\n{CHANGES}", "c", f"d\n\n{FULL2}")
    scan = _PinScan(nodes, headers)
    assert scan.chain(2, 5) == {2: [CHANGES]}
    assert scan.chain(0, 1) == {0: [FULL]}
    # As of the path end, FULL2 supersedes both: nothing before it is carried.
    assert scan.chain(0, 5, as_of=7) == {}
    assert scan.chain(6, 7, as_of=7) == {6: [FULL2]}
    # Change lists with no snapshot before them belong to no chain.
    assert _PinScan(_path(f"b\n\n{CHANGES}"), headers).chain(0, 1) == {}


def test_a_resumed_compactor_renders_the_same_pins():
    # The engine persists each decision with the path's last node (the
    # ContextCompaction event's node_id) and passes it back as path_end.
    config = CompactionConfig(trigger_tokens=500, keep_recent_turns=1)
    compactor = Compactor(config=config)
    nodes = _path(f"one\n\n{FULL}", f"two\n\n{FULL2}", f"three\n\n{CHANGES}", "four")
    _, promotions = compactor.apply(nodes, context_window=None)
    assert promotions, "nothing was folded"
    expected = [m.to_dict() for m in compactor.view(nodes)]

    resumed = Compactor(config=config)
    resumed.rehydrate(replace(p, path_end=nodes[-1].node_id) for p in promotions)
    assert [m.to_dict() for m in resumed.view(nodes)] == expected
    # An unknown end (no node id) still keeps the snapshot in view.
    legacy = Compactor(config=config)
    legacy.rehydrate(promotions)
    assert FULL2 in _view_text(legacy, nodes)
