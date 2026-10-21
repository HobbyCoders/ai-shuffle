"""Large durable tool results must not bypass count-bounded cache retention."""
import pytest

from app.core.provider.fluid_pg_store import PgFluidStore
from fluid.events import ToolResult
from fluid.events.bus import _retained_size


@pytest.mark.asyncio
async def test_event_cache_is_byte_bounded_and_replays_evicted_prefix(mock_db, monkeypatch):
    store = PgFluidStore()
    monkeypatch.setattr(store, "EVENT_CACHE_BYTES", 2048, raising=False)
    await store.create_session("byte-cap")
    for seq in range(1, 8):
        await store.append_event("byte-cap", ToolResult(session_id="byte-cap", seq=seq,
            tool_use_id=str(seq), name="large", output="x" * 700 + str(seq)))
    assert [e.seq for e in await store.events_since("byte-cap", 0)] == list(range(1, 8))
    cached = store._events["byte-cap"]
    assert sum(_retained_size(e) for e in cached) <= store.EVENT_CACHE_BYTES
    assert 0 < len(cached) < 7
    assert cached[-1].seq == 7
    assert store._event_floor["byte-cap"] == cached[0].seq - 1
    assert [e.seq for e in await store.events_since("byte-cap", 1)] == list(range(2, 8))
    assert await store.events_since("byte-cap", 7) == []


@pytest.mark.asyncio
async def test_single_oversized_event_is_not_cached_but_still_durable(mock_db, monkeypatch):
    store = PgFluidStore()
    monkeypatch.setattr(store, "EVENT_CACHE_BYTES", 1024, raising=False)
    await store.create_session("oversized")
    event = ToolResult(session_id="oversized", seq=1, tool_use_id="big", name="large", output="x" * 5000)
    await store.append_event("oversized", event)
    assert [e.output for e in await store.events_since("oversized", 0)] == [event.output]
    assert store._events["oversized"] == []
    assert store._event_floor["oversized"] == 1
    assert (await store.events_since("oversized", 0))[0].output == event.output
    assert await store.events_since("oversized", 1) == []


@pytest.mark.asyncio
async def test_node_byte_diagnostics_follow_cache_insert_and_cold_restore(mock_db):
    import json
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode
    store = PgFluidStore()
    await store.create_session("node-bytes")
    node = SessionNode(node_id="root", parent_id=None,
                       message=Message(role="user", content=[Text("unicode: é 猫")]))
    await store.append("node-bytes", node)
    await store.append("node-bytes", node)
    stats = store.cache_stats()
    assert stats["retained_nodes"] == 1
    assert stats["message_json_bytes"] == len(json.dumps(node.message.to_dict()).encode())
    cold = PgFluidStore()
    await cold.get_session("node-bytes")
    assert cold.cache_stats() == stats


@pytest.mark.asyncio
async def test_legacy_replay_decodes_in_small_finite_pages(mock_db, monkeypatch):
    from app.core.provider import fluid_pg_store as module
    store = PgFluidStore()
    await store.create_session("paged")
    for seq in range(1, 18):
        await store.append_event("paged", ToolResult(session_id="paged", seq=seq,
            tool_use_id=str(seq), name="large", output="x" * 700))
    original = module._events_page_sync
    calls = []
    def counted(sid, after, through, limit):
        calls.append((after, through, limit))
        return original(sid, after, through, limit)
    monkeypatch.setattr(module, "_events_page_sync", counted)
    assert [e.seq for e in await store.events_since("paged", 0)] == list(range(1, 18))
    assert calls == [(0, 17, 8), (8, 17, 8), (16, 17, 8)]


