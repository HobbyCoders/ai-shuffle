"""Delegated stores persist full history without retaining a second transcript."""
import pytest

from app.core.provider.fluid_pg_store import PgFluidStore
from fluid.events.model import TextDelta
from fluid.messages.content import Message, Text
from fluid.store.base import SessionNode


def node(key, parent=None):
    return SessionNode(node_id=key, parent_id=parent,
                       message=Message(role="user", content=[Text(text=key)]))


@pytest.mark.asyncio
async def test_lazy_append_does_not_retain_nodes_and_preserves_forks(mock_db):
    store = PgFluidStore(cache_nodes=False)
    await store.create_session("lazy")
    for item in (node("root"), node("left", "root"), node("right", "root")):
        await store.append("lazy", item)
    assert not store._nodes.get("lazy")
    assert [n.node_id for n in await store.load_path("lazy")] == ["root", "right"]
    await store.set_head("lazy", "left")
    assert [n.node_id for n in await store.load_path("lazy")] == ["root", "left"]
    assert [n.node_id for n in await store.load_path("lazy", "right")] == ["root", "right"]
    assert (await store.get_node("lazy", "right")).message.text() == "right"
    assert await store.get_node("lazy", "missing") is None
    assert not store._nodes.get("lazy")
    reloaded = PgFluidStore()
    assert [n.node_id for n in await reloaded.load_path("lazy")] == ["root", "left"]


@pytest.mark.asyncio
async def test_lazy_resume_reads_metadata_without_decoding_transcript(mock_db, monkeypatch):
    from app.core.provider import fluid_pg_store as module
    writer = PgFluidStore()
    await writer.create_session("lazy-resume")
    await writer.append("lazy-resume", node("root"))
    await writer.set_cli_session_id("lazy-resume", "cli-handle")
    reader = PgFluidStore(cache_nodes=False)
    original = module.Message.from_dict
    def forbidden(*args, **kwargs):
        pytest.fail("metadata resume decoded a transcript message")
    monkeypatch.setattr(module.Message, "from_dict", forbidden)
    meta = await reader.get_session("lazy-resume")
    assert meta.head_node_id == "root" and meta.cli_session_id == "cli-handle"
    assert not reader._nodes.get("lazy-resume")
    monkeypatch.setattr(module.Message, "from_dict", original)
    assert (await reader.get_node("lazy-resume", "root")).message.text() == "root"


@pytest.mark.asyncio
async def test_lazy_reads_flush_pending_events_and_preserve_replay(mock_db):
    store = PgFluidStore(cache_nodes=False)
    await store.create_session("lazy-events")
    await store.append("lazy-events", node("root"))
    await store.append_stream_event("lazy-events", TextDelta(
        session_id="lazy-events", index=0, text="hello", seq=1))
    await store.load_path("lazy-events")
    assert not store._pending_events.get("lazy-events")
    events = await PgFluidStore().events_since("lazy-events", 0)
    assert [e.text for e in events] == ["hello"]

@pytest.mark.asyncio
async def test_delegated_query_turns_never_reload_full_tree(mock_db, monkeypatch, tmp_path):
    from app.core.provider import fluid_pg_store as module
    from app.core.provider.harness_adapter import HarnessAdapter
    from tests.core.test_memory_reminder import _delegated, _delegated_turn, _section, FACTS

    store = PgFluidStore()
    monkeypatch.setattr(HarnessAdapter, "_build_store", staticmethod(lambda options: store))
    adapter = await _delegated(monkeypatch, tmp_path, {"section": _section(*FACTS)})
    original = module._load_session_sync
    full_loads = []
    def counted(sid, *, include_nodes=True):
        if include_nodes:
            full_loads.append(sid)
        return original(sid, include_nodes=include_nodes)
    monkeypatch.setattr(module, "_load_session_sync", counted)
    try:
        for index in range(6):
            await _delegated_turn(adapter, f"memory backend task {index}")
        assert not full_loads
        assert not store._nodes.get(adapter.sdk_session_id)
        from tests.core.test_memory_reminder import _memory_kinds
        assert (await _memory_kinds(adapter._engine)).count("full") == 1
    finally:
        await adapter.disconnect()

@pytest.mark.asyncio
async def test_memory_projection_is_small_restores_and_follows_rewinds(mock_db, monkeypatch):
    from app.core import memory_reminder as mr
    from app.core.provider import fluid_pg_store as module
    store = PgFluidStore(cache_nodes=False)
    await store.create_session("memory-path")
    note = mr.wrap("# Memory\n- [m1 r1; session] A fact")
    root = node("root")
    root.message = Message(role="user", content=[Text(text="owner task " + "x" * 100000 + note)]).pin_context(note)
    await store.append("memory-path", root)
    await store.append("memory-path", node("left", "root"))
    other = node("off-path", "root")
    other.message = Message(role="assistant", content=[Text(text="UNRELATED " * 20000)])
    await store.append("memory-path", other)
    await store.set_head("memory-path", "left")
    cold = PgFluidStore(cache_nodes=False)
    original = module.Message.from_dict
    decoded = []
    def counted(raw):
        decoded.append(raw)
        assert "UNRELATED" not in str(raw)
        return original(raw)
    monkeypatch.setattr(module.Message, "from_dict", counted)
    projection = await cold.load_memory_path("memory-path")
    assert [n.node_id for n in projection] == ["root"]
    assert projection[0].message.text() == note
    assert mr.held_from_path(projection, "root") == mr.held_from_path([root], "root")
    # A linear append uses the tiny projection; no ancestry read/decode.
    count = len(decoded)
    await cold.append("memory-path", node("next", "left"))
    assert await cold.load_memory_path("memory-path") == projection
    assert len(decoded) == count
    assert await cold.initial_task_query("memory-path", ["off-path", "root"]) == ("owner task " + "x" * 100000)[:4000]
    # A rewind performed by another store must invalidate the projection.
    await store.set_head("memory-path", None)
    assert await cold.load_memory_path("memory-path") == []


@pytest.mark.asyncio
async def test_memory_projection_overflow_is_conservative_and_recovers(mock_db):
    from app.core import memory_reminder as mr
    from app.core.provider.fluid_pg_store import MEMORY_PATH_BYTES
    store = PgFluidStore(cache_nodes=False)
    await store.create_session("overflow")
    huge = mr.wrap("# Memory\n- [m1 r1; session] " + "x" * MEMORY_PATH_BYTES)
    first = node("big")
    first.message = Message(role="user", content=[Text(text=huge)]).pin_context(huge)
    await store.append("overflow", first)
    assert await store.load_memory_path("overflow") is None
    small = mr.wrap("# Memory\n- [m2 r1; session] Small")
    replacement = node("small", "big")
    replacement.message = Message(role="user", content=[Text(text=small)]).pin_context(small)
    await store.append("overflow", replacement)
    projection = await store.load_memory_path("overflow")
    assert [n.node_id for n in projection] == ["small"]
    assert [n.node_id for n in await PgFluidStore(cache_nodes=False).load_memory_path("overflow")] == ["small"]
