"""Selected replay uses a store query when available and keeps legacy ordering."""
import pytest

from fluid.events import Usage, ToolResult
from fluid.store.base import iter_events_of_class


@pytest.mark.parametrize("bounded", [False, True])
async def test_legacy_store_fallback_preserves_order_and_cursor(bounded):
    events = [Usage(session_id="s", seq=1),
              ToolResult(session_id="s", seq=3, tool_use_id="t", name="test", output="large"),
              Usage(session_id="s", seq=8)]
    class Store:
        async def events_since(self, sid, seq):
            return [e for e in events if e.seq > seq]
    store = Store()
    if bounded:
        async def replay(sid, seq):
            for event in await store.events_since(sid, seq):
                yield event
        store.iter_events = replay
    assert [e.seq async for e in iter_events_of_class(store, "s", 1, (Usage,))] == [8]
    assert [e async for e in iter_events_of_class(store, "s", 0, ())] == []


async def test_store_side_filter_used_without_full_replay():
    calls = []
    class Store:
        async def iter_events_of_class(self, sid, seq, classes):
            calls.append((sid, seq, classes))
            yield Usage(session_id=sid, seq=8)
        async def events_since(self, *args):
            raise AssertionError("must not read the whole history")
    assert [e.seq async for e in iter_events_of_class(Store(), "s", 3, (Usage,))] == [8]
    assert calls == [("s", 3, (Usage,))]


async def test_wake_bookkeeping_tracks_only_active_window_and_restores_first_source():
    from types import SimpleNamespace
    from fluid.engine.native import NativeLoop
    from fluid.events import TurnStarted
    from fluid.store.base import MemoryStore

    store = MemoryStore()
    await store.create_session("s")
    loop = object.__new__(NativeLoop)
    loop.store = store
    loop._compactor = SimpleNamespace(wake_turn_starts=set())
    for index in range(100):
        node_id = str(index)
        first, later = ("unpark", "user") if index % 2 == 0 else ("user", "unpark")
        await store.append_event("s", TurnStarted(session_id="s", seq=index * 2 + 1,
            node_id=node_id, source=first, model="synthetic", provider="synthetic"))
        # A later resume must never change the node's original source.
        await store.append_event("s", TurnStarted(session_id="s", seq=index * 2 + 2,
            node_id=node_id, source=later, model="synthetic", provider="synthetic"))
        nodes = [SimpleNamespace(node_id=str(i)) for i in range(max(0, index - 3), index + 1)]
        await loop._note_wake_turn_starts("s", nodes)
        active = {n.node_id for n in nodes}
        assert loop._wake_scan_nodes == active
        assert loop._wake_scan_examined == active
        wakes = {node_id for node_id in active if int(node_id) % 2 == 0}
        assert loop._compactor.wake_turn_starts == wakes
        assert loop._unpark_turn_starts == wakes
        assert len(loop._wake_scan_nodes) <= 4
    # Re-enter an evicted branch: the first event still wins, not its resume.
    await loop._note_wake_turn_starts("s", [SimpleNamespace(node_id="0")])
    assert loop._wake_scan_nodes == {"0"}
    assert loop._compactor.wake_turn_starts == {"0"}
    assert loop._unpark_turn_starts == {"0"}
    await loop._note_wake_turn_starts("s", [SimpleNamespace(node_id="1")])
    assert loop._wake_scan_nodes == {"1"}
    assert loop._compactor.wake_turn_starts == set()
    assert loop._unpark_turn_starts == set()


async def test_wake_replay_mixed_roles_is_incremental_until_pruned_nodes_reenter():
    from types import SimpleNamespace
    from fluid.engine.native import NativeLoop
    from fluid.events import TurnStarted
    from fluid.messages import Message, Text, ToolResultBlock
    from fluid.store.base import MemoryStore, SessionNode

    class CountingStore(MemoryStore):
        def __init__(self):
            super().__init__()
            self.cursors = []
            self.decoded = 0

        async def iter_events_of_class(self, sid, seq, classes):
            self.cursors.append(seq)
            for event in await self.events_since(sid, seq):
                if isinstance(event, classes):
                    self.decoded += 1
                    yield event

    store = CountingStore()
    await store.create_session("s")
    loop = object.__new__(NativeLoop)
    loop.store = store
    loop._compactor = SimpleNamespace(wake_turn_starts=set())
    nodes = [
        SessionNode(node_id="u0", parent_id=None, message=Message(role="user", content=[Text("start")])),
        SessionNode(node_id="a0", parent_id="u0", message=Message(role="assistant", content=[Text("call")])),
        SessionNode(node_id="r0", parent_id="a0", message=Message(role="user", content=[
            ToolResultBlock(tool_use_id="t", content=[Text("output")])])),
    ]
    for node in nodes:
        await store.append("s", node)
    await store.append_event("s", TurnStarted(session_id="s", node_id="u0", seq=1,
        source="unpark", model="synthetic", provider="synthetic"))
    await loop._note_wake_turn_starts("s", nodes)
    await loop._note_wake_turn_starts("s", nodes)
    assert store.cursors == [0, 1]
    assert store.decoded == 1

    # The real append path records fresh IDs separately from TurnStarted provenance.
    fresh = SessionNode(node_id="u1", parent_id="r0", message=Message(role="user", content=[Text("next")]))
    await loop._append_node("s", fresh)
    nodes.append(fresh)
    await store.append_event("s", TurnStarted(session_id="s", node_id="u1", seq=3,
        source="user", model="synthetic", provider="synthetic"))
    await loop._note_wake_turn_starts("s", nodes)
    assert store.cursors == [0, 1, 1]
    assert store.decoded == 2
    assert loop._compactor.wake_turn_starts == {"u0"}

    # Pruning alone stays incremental; only returning old nodes needs the prefix.
    await loop._note_wake_turn_starts("s", [fresh])
    assert store.cursors[-1] == 3
    assert loop._wake_scan_examined == {"u1"}
    await store.append_event("s", TurnStarted(session_id="s", node_id="u0", seq=5,
        source="user", model="synthetic", provider="synthetic"))
    await loop._note_wake_turn_starts("s", nodes[:3])
    assert store.cursors[-1] == 0
    assert store.decoded == 5
    assert loop._compactor.wake_turn_starts == {"u0"}


async def test_native_turn_producer_keeps_wake_replay_incremental():
    from fluid.engine.native import NativeLoop
    from fluid.events import TurnStarted, UserTurn
    from fluid.store.base import MemoryStore
    from tests.unit.test_store_tree import ScriptedProvider

    class CountingStore(MemoryStore):
        cursors = None
        decoded = 0

        async def iter_events_of_class(self, sid, seq, classes):
            if classes == (TurnStarted,):
                self.cursors.append(seq)
            for event in await self.events_since(sid, seq):
                if isinstance(event, classes):
                    if classes == (TurnStarted,):
                        self.decoded += 1
                    yield event

    store = CountingStore()
    store.cursors = []
    loop = NativeLoop(provider=ScriptedProvider(), store=store, model="synthetic")
    try:
        await loop.submit(UserTurn(content="first"))
        await loop.wait_turn()
        first_cursor = loop._wake_scan_seq
        assert first_cursor > 0
        await loop.submit(UserTurn(content="next"))
        await loop.wait_turn()
        assert store.cursors == [0, first_cursor]
        assert store.decoded == 2
        path = await store.load_path(loop.session_id)
        assert [n.message.role for n in path] == ["user", "assistant", "user", "assistant"]
        assert loop._wake_scan_examined == {n.node_id for n in path}
    finally:
        await loop.aclose()
