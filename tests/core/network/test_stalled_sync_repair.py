"""Tombstoned parents drain; genuinely missing parents retain ordered diagnostics."""
from unittest.mock import AsyncMock

import httpx

from app.db import database, network as store
from app.network import engine, transport, transcripts
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_transport import ServedBy


async def test_deleted_parent_drains_without_snapshot_or_backfill(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Repair")
    join(a, b)
    _new_session(a, "blocked", "Before")
    converge([a, b])
    with b.active():
        database.create_tag("removed", "Removed")
        engine.scan_all()
    exchange(b, a)
    with a.active():
        database.add_session_tag("blocked", "removed")
        engine.scan_all()
        child_clock = a.sql("SELECT clock FROM sync_records WHERE collection='session_tags'")[0]["clock"]
    # A final deletion must drain even when its concurrent clock is lower.
    with b.active():
        database.delete_tag("removed")
        engine.scan_all()
        tombstone_clock = b.sql("SELECT clock FROM sync_records WHERE collection='tags'")[0]["clock"]
        assert child_clock >= tombstone_clock
    exchange(b, a)
    with a.active():
        database.set_session_active("blocked", False)
        engine.scan_all()
    served = ServedBy(a, "http://owner.local:8000")
    with b.active():
        link = transport.PeerLink(a.device_id, [served.base_url])
        async with httpx.AsyncClient(transport=served.transport()) as client:
            result = await link.sync_once(client, engine.require_context())
            assert result["apply"]["retry"] == 0
            assert result["apply"]["rejected"] == 1
            assert "parent deleted: tags/removed" in result["apply"]["reasons"]
            assert not database.get_session("blocked")["is_active"]
            assert not store.pending_holes()
            assert not transcripts.pending()
            assert store.list_conflicts(), "permanent refusal must not silently lose the assignment"
            with a.active():
                database.update_session("blocked", title="After repair")
                engine.scan_all()
            await link.sync_once(client, engine.require_context())
            assert database.get_session("blocked")["title"] == "After repair"
    assert not any(path == "/api/v1/network/peer/state" for _, path in served.requests)


async def test_missing_log_op_uses_existing_snapshot_path(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Repair")
    join(a, b)
    _new_session(a, "missing", "Before")
    converge([a, b])
    with a.active():
        database.set_session_active("missing", False)
        engine.scan_all()
        ops = store.ops_since({})
        victim = next(op for op in reversed(ops) if op["collection"] == "session_active")
        a.sql("DELETE FROM sync_ops WHERE origin_device_id=? AND incarnation=? AND seq=?",
              (victim["origin_device_id"], victim["origin_incarnation"], victim["origin_seq"]))
    served = ServedBy(a, "http://owner.local:8000")
    with b.active():
        link = transport.PeerLink(a.device_id, [served.base_url])
        async with httpx.AsyncClient(transport=served.transport()) as client:
            result = await link.sync_once(client, engine.require_context())
        assert result["snapshot_required"]
        assert not database.get_session("missing")["is_active"]
        assert not store.pending_holes()


def test_missing_parent_diagnostics_persist_without_automatic_copy(workers, monkeypatch, caplog):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Diagnostics")
    join(a, b)
    join(a, c)
    _new_session(a, "tagged", "Tagged")
    converge([a, b, c])
    with b.active():
        database.create_tag("late", "Late")
        engine.scan_all()
    exchange(b, a)
    with a.active():
        database.add_session_tag("tagged", "late")
        engine.scan_all()
    with c.active():
        marks = store.local_watermarks()
    with a.active():
        ops = [op for op in store.ops_since(marks) if op["origin_device_id"] == a.device_id]
        theirs = store.local_watermarks()
    with c.active():
        link = transport.PeerLink(a.device_id, [])
        transport._links[a.device_id] = link
        copy = AsyncMock()
        monkeypatch.setattr(link, "receive_state", copy)
        for attempt in range(10):
            result = engine.apply_batch(ops)
            assert result.retry == 1 and result.rejected == 0
            assert store.local_watermarks() == marks
            link._note_stalled_pull(engine.require_context(), marks, marks, theirs, set(), {"apply": result.as_dict()})
            stalled = next(row for row in transport.link_status()[0]["stalled_streams"]
                           if row["origin_device_id"] == a.device_id)
            assert stalled["passes"] == attempt + 1
            assert stalled["head_seq"] == ops[0]["origin_seq"]
            assert stalled["first_seen_at"] > 0
            assert stalled["reason"] == "Referenced record has not arrived yet"
        copy.assert_not_called()
        assert sum(record.message.startswith("Sync blocked at") for record in caplog.records) == 1
    exchange(b, c)
    with c.active():
        result = engine.apply_batch(ops)
        assert result.retry == 0 and result.applied == 1
        assert database.get_session_tags("tagged")[0]["id"] == "late"
        current = store.local_watermarks()
        link._note_stalled_pull(engine.require_context(), marks, current, {}, set(), {})
        assert not link._stalled_streams and link.sync_warning is None


def test_stale_incarnation_is_not_reported_as_a_stall(workers):
    a = workers("a")
    with a.active():
        engine.create_network("Diagnostics")
        link = transport.PeerLink("peer", [])
        marks = {"peer": {"1": 3, "2": 4}}
        link._note_stalled_pull(engine.require_context(), marks, marks,
                                {"peer": {"1": 100, "2": 4}}, set(), {})
        assert not link._stalled_streams
