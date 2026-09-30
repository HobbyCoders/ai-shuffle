"""Snapshot pagination stays wire-compatible without rescanning its prefix."""
from app.db import network as store
from app.network import engine


def test_keyset_pages_match_legacy_pages_and_keep_cursor_private(workers, monkeypatch):
    worker = workers("source")
    with worker.active():
        ctx = engine.create_network("Home")
        with store.get_db() as conn:
            cursor = conn.cursor()
            for index in range(7):
                name = f"tag-{index}"
                cursor.execute("INSERT INTO tags(id,name,color) VALUES(%s,%s,%s)", (name, name, "blue"))
                store.put_record(cursor, "tags", name, clock=1, origin_device_id=ctx.device_id,
                                 content_hash="hash", deleted=index == 3)
        from tests.pg_shim import ShimCursor
        execute = ShimCursor.execute
        queries = []

        def record_query(self, sql, *args, **kwargs):
            queries.append(sql)
            return execute(self, sql, *args, **kwargs)

        monkeypatch.setattr(ShimCursor, "execute", record_query)
        after = None
        seen = []
        for offset in (0, 3, 6):
            queries.clear()
            chunk, after = engine.snapshot_page("tags", offset, after_record_id=after, limit=3, ctx=ctx)
            page_queries = [sql for sql in queries if "FROM sync_records" in sql]
            assert len(page_queries) == 1
            assert "OFFSET" not in page_queries[0]
            if offset:
                assert "record_id >" in page_queries[0]
            legacy = engine.snapshot_chunk("tags", offset, limit=3, ctx=ctx)
            assert set(chunk) == set(legacy)
            body = engine.open_snapshot_chunk(chunk, ctx=ctx)
            assert body == engine.open_snapshot_chunk(legacy, ctx=ctx)
            seen.extend(item["record_id"] for item in body["items"])
            assert after == body["items"][-1]["record_id"]
        assert seen == [f"tag-{index}" for index in range(7)]
        assert not chunk["more"]
        empty, cursor = engine.snapshot_page("tags", 7, after_record_id=after, limit=3, ctx=ctx)
        assert empty["count"] == 0 and cursor == after


def test_snapshot_context_read_does_not_replace_live_context(workers):
    worker = workers("source")
    with worker.active():
        engine.create_network("Home")
        live = engine.require_context()
        with store.get_db() as conn:
            frozen = engine.load_context(conn, publish=False)
        assert frozen is not live
        assert frozen.network_id == live.network_id
        assert engine.require_context() is live
