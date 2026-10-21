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
