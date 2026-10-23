"""Native live-session window (issue #264): bounded retention, durable history.

``PgFluidStore.evict_prefix`` drops a checkpointed prefix's interior from a
live native store; ``load_path``/``get_node`` must still return every node
from the database, and the engine's per-call ``load_window`` must stay small.
The end-to-end tests drive the real ``NativeLoop`` and compactor over the
store and check the request the provider receives is the one the whole path
renders.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode

from app.core.provider.fluid_pg_store import PgFluidStore


def node(key, parent=None, text=None, role="user"):
    return SessionNode(node_id=key, parent_id=parent,
                       message=Message(role=role, content=[Text(text=text or key)]))


async def chain(store, sid, count, *, branch_at=None):
    await store.create_session(sid)
    parent = None
    for i in range(count):
        await store.append(sid, node(f"n{i}", parent, role="user" if i % 2 == 0 else "assistant"))
        parent = f"n{i}"
    if branch_at is not None:
        await store.append(sid, node("side", f"n{branch_at}"))
        await store.set_head(sid, parent)


async def test_evicted_prefix_stays_readable_and_window_is_small(mock_db):
    store = PgFluidStore()
    await chain(store, "win", 40, branch_at=5)
    full = await store.load_path("win")
    await store.evict_prefix("win", "n30", {"n0", "n12", "n30"})

    # Only the anchors, the end and the tail below it stay in memory; the
    # off-path branch hanging from the evicted prefix leaves too.
    assert set(store._nodes["win"]) == {"n0", "n12", *(f"n{i}" for i in range(30, 40))}
    assert store.cache_stats()["retained_nodes"] == 12
    assert store.cache_stats()["windowed_sessions"] == 1
    assert store.cache_stats()["message_json_bytes"] == sum(store._node_sizes["win"].values())

    window, end = await store.load_window("win")
    assert end == "n30"
    assert [n.node_id for n in window] == ["n0", "n12", *(f"n{i}" for i in range(30, 40))]

    # No loss: the whole path, an evicted node and the evicted branch read back.
    assert await store.load_path("win") == full
    assert (await store.get_node("win", "n7")) == full[7]
    assert (await store.get_node("win", "side")).parent_id == "n5"
    assert [n.node_id for n in await store.load_path("win", "side")] == [
        *(f"n{i}" for i in range(6)), "side"]
    assert await store.get_node("win", "missing") is None
    # Reads back from the database are not re-cached.
    assert store.cache_stats()["retained_nodes"] == 12
    assert [n.node_id for n in await PgFluidStore().load_path("win")] == [n.node_id for n in full]


async def test_appends_below_window_extend_it_and_rewind_recaches(mock_db):
    store = PgFluidStore()
    await chain(store, "rw", 20)
    await store.evict_prefix("rw", "n15", {"n0"})
    await store.append("rw", node("n20", "n19"))
    window, end = await store.load_window("rw")
    assert end == "n15" and [n.node_id for n in window][-1] == "n20"
    assert len(await store.load_path("rw")) == 21

    # A rewind into the evicted interior drops the window and re-caches the
    # path it reads, so the engine never gets a path with a hole in it.
    await store.set_head("rw", "n8")
    window, end = await store.load_window("rw")
    assert end is None
    assert [n.node_id for n in window] == [f"n{i}" for i in range(9)]
    assert store.cache_stats()["windowed_sessions"] == 0
    assert {f"n{i}" for i in range(9)} <= set(store._nodes["rw"])

    # A later window can form again at a new end.
    await store.append("rw", node("b9", "n8"))
    await store.evict_prefix("rw", "n8", {"n0"})
    window, end = await store.load_window("rw")
    assert end == "n8" and [n.node_id for n in window] == ["n0", "n8", "b9"]


async def test_evict_with_unknown_end_is_a_no_op(mock_db):
    store = PgFluidStore()
    await chain(store, "noop", 6)
    before = set(store._nodes["noop"])
    await store.evict_prefix("noop", "missing", {"n0"})
    assert set(store._nodes["noop"]) == before
    assert (await store.load_window("noop"))[1] is None
    lazy = PgFluidStore(cache_nodes=False)
    await lazy.evict_prefix("noop", "n3", {"n0"})
    assert (await lazy.load_window("noop"))[1] is None
    assert len((await lazy.load_window("noop"))[0]) == 6


# ── Cycle guard: a looping parent chain raises instead of growing forever ────

async def test_cached_walk_raises_on_a_parent_cycle(mock_db):
    """Duplicate ids (the lab's old seeding) overwrite a cached node with one
    whose parent is a later node; the walk must refuse, not append forever."""
    import pytest
    from app.core.provider.fluid_pg_store import TranscriptCycleError

    store = PgFluidStore()
    await store.create_session("cyc")
    await store.append("cyc", node("a"))
    await store.append("cyc", node("b", "a"))
    await store.append("cyc", node("a", "b"))  # DB keeps the first "a"; cache does not
    with pytest.raises(TranscriptCycleError, match="loops"):
        await store.load_path("cyc")
    with pytest.raises(TranscriptCycleError):
        await store.load_window("cyc")
    self_loop = PgFluidStore()
    await self_loop.create_session("self")
    await self_loop.append("self", node("s", "s"))
    with pytest.raises(TranscriptCycleError):
        await self_loop.load_path("self")


async def test_database_walks_raise_on_a_parent_cycle(mock_db, monkeypatch):
    """The uncached reads (evicted read-back, delegated memory path and task
    lookup, delegated load_path) refuse a cycle stored in the database itself,
    and the ancestry readers do it from one MAX(depth) row: no message or id
    rows are fetched over the looping walk."""
    import contextlib

    import pytest
    from app.core.provider import fluid_pg_store as module

    monkeypatch.setattr(module, "MAX_PATH_DEPTH", 64)
    store = PgFluidStore(cache_nodes=False)
    await store.create_session("dbcyc")
    await store.append("dbcyc", node("x", "y"))
    await store.append("dbcyc", node("y", "x"))

    statements = []
    real_get_db = module.get_db

    @contextlib.contextmanager
    def spying_get_db():
        with real_get_db() as conn:
            class Cursor:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=()):
                    statements.append(sql)
                    return self._inner.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self._inner, name)

            class Conn:
                def cursor(self, *a, **k):
                    return Cursor(conn.cursor(*a, **k))

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield Conn()

    monkeypatch.setattr(module, "get_db", spying_get_db)
    readers = [
        lambda: module._load_ancestors_sync("dbcyc", "y"),
        lambda: module._load_memory_path_sync("dbcyc", "y"),
        lambda: module._initial_task_sync("dbcyc", ["x"]),
    ]
    for read in readers:
        statements.clear()
        with pytest.raises(module.TranscriptCycleError, match="loops"):
            read()
        walks = [sql for sql in statements if "WITH RECURSIVE ancestors" in sql]
        assert walks and all("SELECT MAX(depth) AS depth FROM ancestors" in sql for sql in walks)
    monkeypatch.setattr(module, "get_db", real_get_db)

    with pytest.raises(module.TranscriptCycleError):
        await store.load_path("dbcyc")
    with pytest.raises(module.TranscriptCycleError):
        await store.load_memory_path("dbcyc")
    # A real chain under the bound still reads back whole.
    await store.create_session("ok")
    parent = None
    for i in range(60):
        await store.append("ok", node(f"k{i}", parent))
        parent = f"k{i}"
    assert len(module._load_ancestors_sync("ok", "k59")) == 60
    assert await store.load_memory_path("ok") == []
    assert await store.initial_task_query("ok", ["k0"]) == ""


# ── End to end: the real NativeLoop, compactor and checkpoint tier ────────────

def _loop_fixtures():
    from fluid.context.checkpoint_summary import CHECKPOINT_SYSTEM
    from fluid.context.compaction import CompactionConfig
    from fluid.providers.base import ProviderCaps, StreamChunk

    class Provider:
        name = "anthropic"
        caps = ProviderCaps()

        def __init__(self):
            self.requests = []

        def estimate_cost(self, model, usage):
            return 0.0

        async def stream(self, request):
            checkpoint = request.system == CHECKPOINT_SYSTEM
            if not checkpoint:
                self.requests.append(request)
            text = "stable checkpoint text" if checkpoint else "done " * 1000
            yield StreamChunk("message_start", {"usage": {"input_tokens": 100, "output_tokens": 1}})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": text})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 7}, "stop_reason": "end_turn"})
            yield StreamChunk("message_stop", {"stop_reason": "end_turn"})

    config = replace(CompactionConfig(summary_enabled=True, keep_recent_turns=1,
                                      recency_tail_tokens=0, max_resident_span_digests=1000),
                     trigger_tokens=10_000_000)
    return Provider, config


async def _turn(loop, content):
    from fluid.events import ErrorEvent, TurnComplete, UserTurn
    before = len(loop.bus.log(loop.session_id)) if loop._session else 0
    await loop.submit(UserTurn(content=content))
    await asyncio.wait_for(loop.wait_turn(), 10)
    events = loop.bus.log(loop.session_id)[before:]
    assert not [e for e in events if isinstance(e, ErrorEvent)]
    assert any(isinstance(e, TurnComplete) for e in events)


async def test_live_native_session_retention_is_bounded_and_history_complete(mock_db):
    from fluid.engine.native import NativeLoop

    Provider, config = _loop_fixtures()
    provider = Provider()
    store = PgFluidStore()
    loop = NativeLoop(provider=provider, store=store, model="fixture", compaction=config)
    retained, paths = [], []
    for round_ in range(4):
        for i in range(6):
            await _turn(loop, f"request {round_}.{i} " + "word " * 2000)
        await _turn(loop, "/compact")
        await _turn(loop, f"after compact {round_}")
        sid = loop.session_id
        paths.append(len(await store.load_path(sid)))
        retained.append(store.cache_stats()["retained_nodes"])

    sid = loop.session_id
    # Steady turns below the window read only memory: no evicted-prefix
    # read-back per model call (that would trade RAM for a DB read per call).
    from app.core.provider import fluid_pg_store as module
    reads = []
    original = module._load_ancestors_sync
    module._load_ancestors_sync = lambda *a: reads.append(a) or original(*a)
    try:
        await _turn(loop, "a steady turn below the window")
    finally:
        module._load_ancestors_sync = original
    assert reads == []
    paths[-1] += 2
    # The path keeps growing; what the store holds does not grow with it.
    assert paths == sorted(paths) and paths[-1] > 4 * retained[-1] / 2
    assert max(retained[1:]) <= retained[0] + 4
    assert retained[-1] < paths[-1] / 2

    # The request the model got is exactly what the whole path renders (the
    # last request was sent before its reply node was appended).
    from fluid.engine.native import repair_dangling_tool_calls
    full = await store.load_path(sid)
    assert len(full) == paths[-1]
    expected = repair_dangling_tool_calls(loop._compactor.view(full[:-1]))
    assert [m.to_dict() for m in provider.requests[-1].messages] == [m.to_dict() for m in expected]
    assert any("checkpoint summary" in m.text() for m in expected)

    # Every evicted node reads back exactly, and a fresh store agrees.
    evicted = [n for n in full if n.node_id not in store._nodes[sid]]
    assert evicted
    for item in evicted[:5] + evicted[-5:]:
        assert await store.get_node(sid, item.node_id) == item
    assert [n.node_id for n in await PgFluidStore().load_path(sid)] == [n.node_id for n in full]

    # HistoryExpand/HistorySearch read the whole path through load_path.
    from fluid.context.recall import HistoryRecall
    assert [n.node_id for n in await HistoryRecall(store=store, session_id=sid).load_path()] == [
        n.node_id for n in full]
    await loop.aclose()


async def test_resumed_engine_renders_the_same_view_from_a_windowed_store(mock_db):
    from fluid.engine.native import NativeLoop

    Provider, config = _loop_fixtures()
    store = PgFluidStore()
    loop = NativeLoop(provider=Provider(), store=store, model="fixture", compaction=config)
    for i in range(6):
        await _turn(loop, f"request {i} " + "word " * 2000)
    await _turn(loop, "/compact")
    await _turn(loop, "after compact")
    sid = loop.session_id
    assert store.cache_stats()["windowed_sessions"] == 1
    expected = loop._compactor.view(await store.load_path(sid))
    await loop.aclose()

    # A fresh engine over the SAME windowed store (a park/wake in the app):
    # its first read is the whole path, then it adopts the window.
    provider = Provider()
    fresh = NativeLoop(provider=provider, store=store, model="fixture", session_id=sid,
                       compaction=config)
    await fresh._rehydrate_compaction(sid)
    window = await fresh._context_path(sid)
    assert fresh._compactor.view(window) == expected
    assert fresh._compactor.view(await store.load_path(sid)) == expected
    assert await fresh._context_path(sid) == window
    await fresh.aclose()
