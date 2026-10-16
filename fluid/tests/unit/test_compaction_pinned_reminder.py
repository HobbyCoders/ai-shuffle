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


def test_only_the_newest_chain_is_carried():
    compactor, nodes = _digested(f"one\n\n{FULL}", f"two\n\n{FULL2}", "three", "four")
    text = _view_text(compactor, nodes)
    assert FULL2 in text and FULL not in text


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
