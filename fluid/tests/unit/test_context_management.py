"""Context management: cache-safe trimming, Note recitation, Amp-style handoff."""

from typing import AsyncIterator

from fluid.context.trim import ToolResultTrimmer, TrimConfig
from fluid.engine.native import NativeLoop
from fluid.events import Handoff, HandoffCreated, UserTurn
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.registry import ToolCtx
from fluid.tools.builtins.note import handler as note_handler


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(node_id=nid, parent_id=None,
                       message=Message(role=role, content=blocks))


def _result_node(nid: str, tuid: str, chars: int, *, is_error: bool = False) -> SessionNode:
    return _node(nid, "user", [
        ToolResultBlock(tool_use_id=tuid, content=[Text("x" * chars)], is_error=is_error)
    ])


# ─── Trimming ────────────────────────────────────────────────────────────────


def _tool_result_texts(messages: list[Message]) -> list[str]:
    out = []
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                out.append("".join(getattr(c, "text", "") for c in b.content or []))
    return out


def test_trim_below_trigger_is_identity() -> None:
    trimmer = ToolResultTrimmer(config=TrimConfig(trigger_tokens=10_000))
    nodes = [_result_node(f"n{i}", f"t{i}", 3_000) for i in range(3)]
    rendered = trimmer.apply(nodes)
    assert rendered == [n.message for n in nodes]  # untouched, same objects


def test_trim_batch_keeps_recent_and_errors() -> None:
    cfg = TrimConfig(trigger_tokens=10, keep_recent_tool_results=2,
                     min_result_chars=100)
    trimmer = ToolResultTrimmer(config=cfg)
    nodes = [
        _result_node("n0", "t0", 5_000),                  # old + big  → trimmed
        _result_node("n1", "t1", 50),                     # too small  → kept
        _result_node("n2", "t2", 5_000, is_error=True),   # error      → kept
        _result_node("n3", "t3", 5_000),                  # recent (K=2) → kept
        _result_node("n4", "t4", 5_000),                  # recent      → kept
    ]
    texts = _tool_result_texts(trimmer.apply(nodes))
    assert "trimmed to save context" in texts[0]
    assert texts[1] == "x" * 50
    assert texts[2] == "x" * 5_000  # error kept verbatim
    assert texts[3] == "x" * 5_000 and texts[4] == "x" * 5_000


def test_trim_is_sticky_and_batchy() -> None:
    """Once trimmed, always trimmed — and nothing NEW is trimmed while the
    rendered size sits under the trigger (prefix stays byte-identical)."""
    cfg = TrimConfig(trigger_tokens=3_000, keep_recent_tool_results=1,
                     min_result_chars=100)
    trimmer = ToolResultTrimmer(config=cfg)
    nodes = [_result_node(f"n{i}", f"t{i}", 8_000) for i in range(3)]
    first = _tool_result_texts(trimmer.apply(nodes))
    assert "trimmed" in first[0] and "trimmed" in first[1]
    assert first[2] == "x" * 8_000  # the most recent survived

    # Append one small result: n2 leaves the K-window but the rendered size is
    # now under trigger — n2 must NOT be newly trimmed (no per-turn cache burn).
    nodes.append(_result_node("n3", "t3", 200))
    second = _tool_result_texts(trimmer.apply(nodes))
    assert second[2] == "x" * 8_000
    assert second[:2] == first[:2]  # earlier trims unchanged (sticky)


# ─── Note builtin ────────────────────────────────────────────────────────────


async def test_note_add_recites_and_persists(tmp_path) -> None:
    ctx = ToolCtx(cwd=str(tmp_path), session_id="s1")
    first = await note_handler({"action": "add", "text": "step 1: build"}, ctx)
    second = await note_handler({"action": "add", "text": "step 2: ship"}, ctx)
    assert "step 1: build" in second.text and "step 2: ship" in second.text

    listing = await note_handler({"action": "list"}, ctx)
    assert listing.text == second.text  # recitation returns the full notes

    # Session-scoped: another session sees its own empty notes.
    other = await note_handler({"action": "list"}, ToolCtx(cwd=str(tmp_path), session_id="s2"))
    assert "(empty)" in other.text

    cleared = await note_handler({"action": "clear"}, ctx)
    assert "(empty)" in cleared.text


# ─── Handoff ─────────────────────────────────────────────────────────────────


class HandoffProvider:
    """Scripted: first call answers the turn, the handoff call returns a doc."""

    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        text = ("HANDOFF DOC: goal, state, next steps"
                if "handoff document" in request.messages[0].content[0].text
                else "worked on it")
        for chunk in [
            StreamChunk("message_start", {"usage": {"input_tokens": 1}}),
            StreamChunk("block_start", {"index": 0, "block_type": "text"}),
            StreamChunk("text_delta", {"index": 0, "text": text}),
            StreamChunk("block_stop", {"index": 0}),
            StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"}),
            StreamChunk("message_stop", {}),
        ]:
            yield chunk


async def test_handoff_creates_seeded_session_and_event() -> None:
    provider = HandoffProvider()
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)

    await loop.submit(UserTurn(content="do the thing"))
    await loop.wait_turn()
    await loop.submit(Handoff(goal="finish the migration"))

    events = [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    assert len(events) == 1
    ev = events[0]
    assert ev.goal == "finish the migration"
    assert "HANDOFF DOC" in ev.content

    # The new session exists and starts from the handoff document.
    new_path = await store.load_path(ev.new_session_id)
    assert len(new_path) == 1
    assert new_path[0].message.role == "user"
    assert "HANDOFF DOC" in new_path[0].message.text()

    # The summary request carried the transcript and the goal.
    handoff_req = provider.requests[-1]
    assert "finish the migration" in handoff_req.messages[0].content[0].text
    assert "do the thing" in handoff_req.messages[0].content[0].text
