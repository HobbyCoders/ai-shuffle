"""Retry-from-here keeps its prefix unless memory needs fresh tail context."""
import pytest

from app.core import memory_reminder as mr
from fluid.engine.native import NativeLoop
from fluid.messages import Message, Text
from fluid.store.base import MemoryStore, SessionNode, new_id
from tests.test_harness_adapter import _collect
from tests.core.test_memory_reminder import (
    FACTS, _section, _adapter, _turn, _Recording, _memory_kinds,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unchanged", "changed", "missing", "model_switch"])
async def test_resume_replays_or_restores_memory_at_the_tail(monkeypatch, state):
    memory = {"section": _section(*FACTS)}
    provider = _Recording()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="fake")
    adapter, _ = await _adapter(monkeypatch, memory, loop)
    await _turn(adapter)
    nodes = await loop.store.load_path(loop.session_id)
    user = nodes[0]
    # Resume an unanswered user node, as a failed request leaves it. In the
    # missing case the restored path no longer contains the snapshot.
    head = SessionNode(
        node_id=new_id("node"), parent_id=None if state == "missing" else user.node_id,
        message=Message(role="user", content=[Text("Continue the unfinished task.")]),
    )
    await loop.store.append(loop.session_id, head)
    before = await loop.store.load_path(loop.session_id)
    old_text = [n.message.text() for n in before]
    if state == "changed":
        memory["section"] = _section(*FACTS, "- [m99 r1; user] A newly saved preference.")
    elif state == "model_switch":
        await adapter.set_model("another-model")
    await adapter.send_resume()
    await _collect(adapter)
    after = await loop.store.load_path(loop.session_id)
    assert [n.message.text() for n in after[:len(before)]] == old_text
    kinds = [kind for message in provider.requests[-1].messages
             for kind, _ in mr.memory_blocks(message.text() or "")]
    assert kinds == (["full", "changes"] if state == "changed" else
                     ["full", "full"] if state == "model_switch" else ["full"])
    if state == "unchanged":
        # No user node or reminder is appended on the ordinary Retry path.
        assert len(after) == len(before) + 1  # only the assistant's answer
    else:
        assert after[len(before)].message.role == "user"
        assert mr.memory_blocks(after[len(before)].message.text())
    await _turn(adapter)
    assert (await _memory_kinds(loop))[-1] is None
