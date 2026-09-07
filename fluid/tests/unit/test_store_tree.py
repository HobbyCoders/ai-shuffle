"""M1 — branching tree persistence, fork/rewind, and durable event replay.

Drives the native loop with a scripted fake provider over a JsonlStore, then
reloads from disk with a fresh store instance to prove the tree, the head
tracking, and the event log all survive a restart.
"""

from collections.abc import AsyncIterator

from fluid.engine.native import NativeLoop
from fluid.events import Fork, Rewind, UserTurn
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.jsonl import JsonlStore


class ScriptedProvider:
    """Replays a fixed assistant reply (one text block) per model call."""

    name = "anthropic"
    caps = ProviderCaps(explicit_cache_breakpoints=True, max_cache_breakpoints=4)

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        text = self.replies[self.calls] if self.calls < len(self.replies) else "ok"
        self.calls += 1
        yield StreamChunk("message_start", {"usage": {"input_tokens": 5}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": text})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 3}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})


async def _turn(loop: NativeLoop, text: str, parent: str | None = None) -> None:
    await loop.submit(UserTurn(content=text, parent_node_id=parent))
    await loop.wait_turn()


async def _assistant_nodes(store, sid) -> list:
    path = await store.load_path(sid)
    return [n for n in path if n.message.role == "assistant"]


async def test_fork_rewind_persist_and_reload(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    provider = ScriptedProvider()
    provider.replies = ["A1", "A2", "A3", "B2", "C2"]
    loop = NativeLoop(provider=provider, store=store, model="claude-sonnet-5")

    # 3-turn conversation.
    await _turn(loop, "u1")
    await _turn(loop, "u2")
    await _turn(loop, "u3")
    sid = loop.session_id

    main_path = await store.load_path(sid)
    assert [n.message.role for n in main_path] == ["user", "assistant"] * 3
    assert [n.message.text() for n in main_path if n.message.role == "assistant"] == ["A1", "A2", "A3"]

    # Fork at turn-1's assistant node → divergent branch.
    turn1_assistant = main_path[1].node_id
    await loop.submit(Fork(from_node_id=turn1_assistant))
    await _turn(loop, "b2")  # new turn continues from the fork point
    branch_path = await store.load_path(sid)
    assert [n.message.text() for n in branch_path if n.message.role == "assistant"] == ["A1", "B2"]

    # ─── Reload from disk with a fresh store: both branches intact ───────
    store2 = JsonlStore(tmp_path)
    # Head is on the B-branch after the fork.
    reloaded_head = await store2.load_path(sid)
    assert [n.message.text() for n in reloaded_head if n.message.role == "assistant"] == ["A1", "B2"]
    # The original branch is still reachable leaf-first from A3's node.
    a3_node = main_path[5].node_id
    reloaded_main = await store2.load_path(sid, a3_node)
    assert [n.message.role for n in reloaded_main] == ["user", "assistant"] * 3
    assert reloaded_main[0].parent_id is None  # root-first ordering
    assert [n.message.text() for n in reloaded_main if n.message.role == "assistant"] == ["A1", "A2", "A3"]

    # ─── Rewind then new turn = sibling branch off the fork point ────────
    loop2 = NativeLoop(provider=provider, store=store2, model="claude-sonnet-5", session_id=sid)
    await loop2.submit(Rewind(to_node_id=turn1_assistant))
    await _turn(loop2, "c2")
    sibling_path = await store2.load_path(sid)
    assert [n.message.text() for n in sibling_path if n.message.role == "assistant"] == ["A1", "C2"]
    # The C-branch and B-branch share the A1 parent (siblings, not a chain).
    c2_assistant = sibling_path[-1]
    assert c2_assistant.parent_id != branch_path[-1].node_id


async def test_event_replay_across_fresh_store(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    provider = ScriptedProvider()
    provider.replies = ["hello", "again"]
    loop = NativeLoop(provider=provider, store=store, model="claude-sonnet-5")
    await _turn(loop, "u1")
    await _turn(loop, "u2")
    sid = loop.session_id

    live = await store.events_since(sid, 0)
    live_seqs = [e.seq for e in live]
    assert live_seqs == list(range(1, len(live) + 1))
    assert [type(e).__name__ for e in live][0] == "TurnStarted"

    # Fresh store instance replays the identical seq sequence from disk.
    store2 = JsonlStore(tmp_path)
    replayed = await store2.events_since(sid, 0)
    assert [e.seq for e in replayed] == live_seqs
    assert [type(e).__name__ for e in replayed] == [type(e).__name__ for e in live]
    # since_seq is exclusive.
    assert [e.seq for e in await store2.events_since(sid, 3)] == live_seqs[3:]
