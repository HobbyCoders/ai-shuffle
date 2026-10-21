"""Large durable tool results must not bypass count-bounded cache retention."""
import pytest

from app.core.provider.fluid_pg_store import PgFluidStore
from fluid.events import ToolResult
from fluid.events.bus import _retained_size

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["events_page", "iter_events"])
async def test_live_replay_pages_obey_bytes_and_cursor_gaps(mock_db, monkeypatch, path):
    from app.core.provider import fluid_pg_store as module
    monkeypatch.setattr(module, "EVENT_PAGE_BYTES", 2048, raising=False)
    store = PgFluidStore()
    await store.create_session("live-pages")
    sequences = [1, 3, 8, 9, 20, 21]
    for seq in sequences:
        await store.append_event("live-pages", ToolResult(session_id="live-pages", seq=seq,
            tool_use_id=str(seq), name="large", output="猫" * 250))
    original = module._events_page_sync
    pages = []
    def counted(*args):
        page = original(*args)
        pages.append([event.seq for event in page])
        return page
    monkeypatch.setattr(module, "_events_page_sync", counted)
    if path == "iter_events":
        received = [event.seq async for event in store.iter_events("live-pages", 0)]
    else:
        received = []
        while page := await store.events_page("live-pages", received[-1] if received else 0):
            received.extend(event.seq for event in page)
    assert received == sequences
    assert max(map(len, pages)) < len(sequences)
    assert all(len(page) <= 2 for page in pages)




@pytest.mark.asyncio
async def test_small_live_pages_still_obey_requested_row_limit(mock_db):
    store = PgFluidStore()
    await store.create_session("row-cap")
    for seq in range(1, 6):
        await store.append_event("row-cap", ToolResult(session_id="row-cap", seq=seq,
            tool_use_id=str(seq), name="small", output="ok"))
    assert [event.seq for event in await store.events_page("row-cap", 0, limit=2)] == [1, 2]
    assert [event.seq for event in await store.events_page("row-cap", 2, limit=2)] == [3, 4]
    assert [event.seq async for event in store.iter_events("row-cap", 0, page_size=2)] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_live_replay_oversized_row_always_progresses(mock_db, monkeypatch):
    from app.core.provider import fluid_pg_store as module
    monkeypatch.setattr(module, "EVENT_PAGE_BYTES", 1024, raising=False)
    store = PgFluidStore()
    await store.create_session("live-oversized")
    for seq, size in [(2, 5000), (4, 10), (6, 5000)]:
        await store.append_event("live-oversized", ToolResult(session_id="live-oversized", seq=seq,
            tool_use_id=str(seq), name="varying", output="x" * size))
    assert [event.seq for event in await store.events_page("live-oversized", 0)] == [2]
    assert [event.seq for event in await store.events_page("live-oversized", 2)] == [4]
    assert [event.seq for event in await store.events_page("live-oversized", 4)] == [6]
    assert [event.seq async for event in store.iter_events("live-oversized", 0)] == [2, 4, 6]




@pytest.mark.asyncio
async def test_event_bus_catchup_drains_multiple_byte_bounded_pages(mock_db, monkeypatch):
    import asyncio
    from app.core.provider import fluid_pg_store as module
    from fluid.events.bus import EventBus
    from fluid.store.base import replay_source
    monkeypatch.setattr(module, "EVENT_PAGE_BYTES", 2048)
    store = PgFluidStore()
    await store.create_session("bus-pages")
    bus = EventBus(replay_source=replay_source(store), log_ring_size=1)
    for seq in range(1, 10):
        event = ToolResult(session_id="bus-pages", seq=seq, tool_use_id=str(seq),
                           name="large", output="x" * 900)
        await store.append_event("bus-pages", event)
        await bus.publish(event)
    received = []
    async with asyncio.timeout(5):
        async for event in bus.subscribe("bus-pages"):
            received.append(event.seq)
            if event.seq == 9:
                break
    assert received == list(range(1, 10))
    await bus.close()


@pytest.mark.asyncio
async def test_compatibility_cache_preserves_negative_cursor_floor(mock_db, monkeypatch):
    from app.core.provider import fluid_pg_store as module
    store = PgFluidStore()
    await store.create_session("floor")
    await store.append_event("floor", ToolResult(session_id="floor", seq=1,
        tool_use_id="one", name="small", output="ok"))
    await store.events_since("floor", -1)
    def unexpected(*args):
        pytest.fail("a complete cache should satisfy the original negative cursor")
    monkeypatch.setattr(module, "_events_since_sync", unexpected)
    assert [event.seq for event in await store.events_since("floor", -1)] == [1]



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
async def test_legacy_replay_uses_large_pages_for_small_rows(mock_db, monkeypatch):
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
    assert calls == [(0, 17, 128)]


@pytest.mark.asyncio
async def test_byte_diagnostics_never_serialize_messages_on_event_loop(mock_db, monkeypatch):
    import threading
    from app.core.provider import fluid_pg_store as module
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode
    store = PgFluidStore()
    await store.create_session("off-loop-count")
    node = SessionNode(node_id="root", parent_id=None,
                       message=Message(role="user", content=[Text("large-ish payload" * 1000)]))
    loop_thread = threading.get_ident()
    original = module.json.dumps
    def checked(value, *args, **kwargs):
        assert threading.get_ident() != loop_thread, "diagnostic serialization blocked the event loop"
        return original(value, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(module.json, "dumps", checked)
        await store.append("off-loop-count", node)
        cold = PgFluidStore()
        await cold.get_session("off-loop-count")
        assert cold.cache_stats() == store.cache_stats()


