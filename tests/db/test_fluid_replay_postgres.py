"""Real PostgreSQL contracts for bounded and class-filtered resume replay."""
from contextlib import contextmanager

import pytest

from tests.db import schema_lifecycle_helpers as lifecycle
from tests.db.postgres_harness import requires_real_pg

pytestmark = requires_real_pg


@pytest.fixture(scope="module")
def private_pg(tmp_path_factory):
    bindir = lifecycle.pg16_bindir()
    if bindir is None:
        pytest.skip("PostgreSQL 16 server binaries not found")
    with lifecycle.private_postgres(bindir, tmp_path_factory.mktemp("replay-pg")) as descriptor:
        conn = lifecycle.pg_connect(descriptor)
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE fluid_session_events "
                            "(session_id TEXT, seq BIGINT, event JSONB, PRIMARY KEY(session_id, seq))")
        finally:
            conn.close()
        yield descriptor


@pytest.fixture
def replay_db(private_pg, monkeypatch):
    from app.core.provider import fluid_pg_store as module
    conn = lifecycle.pg_connect(private_pg)
    @contextmanager
    def get_db():
        yield conn
    monkeypatch.setattr(module, "get_db", get_db)
    monkeypatch.setattr(module, "EVENT_PAGE_BYTES", 2048)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE fluid_session_events")
    yield module
    conn.close()


def test_byte_bounded_pages_mixed_prefix_oversize_and_gaps(replay_db):
    from fluid.events import ToolResult
    module = replay_db
    sizes = [(1, 10), (3, 1100), (8, 5000), (20, 10), (30, 10)]
    module._append_events_sync("mixed", [ToolResult(session_id="mixed", seq=seq,
        tool_use_id=str(seq), name="synthetic", output="x" * size) for seq, size in sizes])
    cursor = 0
    pages = []
    while page := module._events_page_sync("mixed", cursor, 30, 100):
        pages.append([e.seq for e in page])
        cursor = page[-1].seq
    assert pages == [[1, 3], [8], [20, 30]]
    assert module._events_page_sync("mixed", 3, 20, 100)[0].seq == 8
    assert [e.seq for e in module._events_page_sync("mixed", 8, 20, 100)] == [20]


@pytest.mark.asyncio
async def test_filtered_replay_decodes_only_matching_committed_prefix(replay_db, monkeypatch):
    from fluid.events import ToolResult, Usage, TurnStarted
    from fluid.store.base import iter_events_of_class
    module = replay_db
    module._append_events_sync("filtered", [
        ToolResult(session_id="filtered", seq=1, tool_use_id="large", name="synthetic", output="x" * 5000),
        Usage(session_id="filtered", seq=3),
        TurnStarted(session_id="filtered", seq=8, model="synthetic", provider="synthetic"),
        Usage(session_id="filtered", seq=20),
        ToolResult(session_id="filtered", seq=30, tool_use_id="tail", name="synthetic", output="x" * 5000),
    ])
    decoded = []
    original = module.event_from_dict
    def decode(row):
        decoded.append(row["_cls"])
        return original(row)
    monkeypatch.setattr(module, "event_from_dict", decode)
    store = module.PgFluidStore()
    # One-row pages exercise cursors across nonmatching rows and sequence gaps.
    replay = store.iter_events_of_class("filtered", 0, (Usage, TurnStarted), page_size=1)
    first = await anext(replay)
    module._append_events_sync("filtered", [Usage(session_id="filtered", seq=40)])
    assert [first.seq] + [e.seq async for e in replay] == [3, 8, 20]
    assert decoded == ["Usage", "TurnStarted", "Usage"]
    assert [e.seq async for e in iter_events_of_class(store, "filtered", 8, (Usage,))] == [20, 40]
    assert [e async for e in iter_events_of_class(store, "filtered", 0, ())] == []


@pytest.mark.asyncio
async def test_filtered_replay_flushes_events_from_producing_path(replay_db):
    from fluid.events import TextDelta, Usage
    from fluid.store.base import iter_events_of_class
    store = replay_db.PgFluidStore()
    await store.append_event("pending", Usage(session_id="pending", seq=1))
    await store.append_stream_event("pending", TextDelta(session_id="pending", seq=3, index=0, text="buffered"))
    assert store._pending_events["pending"]
    assert [e.seq async for e in iter_events_of_class(store, "pending", 0, (TextDelta,))] == [3]
    assert not store._pending_events
    assert [e.seq async for e in iter_events_of_class(store, "pending", 0, (Usage,))] == [1]
