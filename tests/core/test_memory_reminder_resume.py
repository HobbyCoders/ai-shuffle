"""Retry-from-here keeps its prefix unless memory needs fresh tail context."""
import pytest

from app.core import memory_reminder as mr
from fluid.engine.native import NativeLoop
from fluid.messages import Message, Text
from fluid.providers.anthropic.serialize import build_request_body
from fluid.store.base import SessionNode, new_id
from fluid.store.jsonl import JsonlStore
from tests.test_harness_adapter import _collect
from tests.core.test_memory_reminder import (
    FACTS, _section, _adapter, _turn, _Recording, _memory_kinds,
)


def _wire_memory_kinds(request):
    return [kind for message in build_request_body(request)["messages"]
            for block in message["content"] if block["type"] == "text"
            for kind, _ in mr.memory_blocks(block["text"])]


@pytest.mark.asyncio
@pytest.mark.parametrize("state,restart", [("unchanged", False), ("changed", False),
                                            ("missing", False), ("missing", True), ("model_switch", False)])
async def test_resume_replays_or_restores_memory_at_the_tail(monkeypatch, tmp_path, state, restart):
    memory = {"section": _section(*FACTS)}
    provider = _Recording()
    loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="fake")
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
    kinds = _wire_memory_kinds(provider.requests[-1])
    # A native model switch renders the same stored path, so the snapshot it
    # already holds is not appended a second time.
    assert kinds == (["full", "changes"] if state == "changed" else ["full"])
    if state in ("unchanged", "model_switch"):
        # No user node or reminder is appended on the ordinary Retry path.
        assert len(after) == len(before) + 1  # only the assistant's answer
    else:
        assert after[len(before)].message.role == "user"
        assert mr.memory_blocks(after[len(before)].message.text())
    if restart:
        loop = NativeLoop(provider=provider, store=JsonlStore(tmp_path), model="fake", session_id=loop.session_id)
        adapter, _ = await _adapter(monkeypatch, memory, loop)
    await _turn(adapter)
    assert _wire_memory_kinds(provider.requests[-1]) == kinds
    assert (await _memory_kinds(loop))[-1] is None
