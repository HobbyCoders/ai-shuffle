"""A blocked origin must recover through verified state, never a fabricated watermark."""
import httpx
import pytest

from app.db import database, network as store
from app.network import engine, transport
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_transport import ServedBy


async def test_transient_head_repaired_after_bounded_retries(workers, caplog):
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
    # B removes the parent before receiving A's assignment. Its tombstone
    # reaches A, which retracts the assignment. Replaying the old child first
    # now fails forever: the parent is legitimately gone, not still in flight.
    with b.active():
        database.delete_tag("removed")
        engine.scan_all()
    exchange(b, a)
    with a.active():
        database.set_session_active("blocked", False)
        engine.scan_all()
    served = ServedBy(a, "http://owner.local:8000")
    with b.active():
        link = transport.PeerLink(a.device_id, [served.base_url])
        transport._links[a.device_id] = link
        async with httpx.AsyncClient(transport=served.transport()) as client:
            for attempt in range(transport.STALLED_PULL_PASSES):
                result = await link.sync_once(client, engine.require_context())
                if attempt < transport.STALLED_PULL_PASSES - 1:
                    assert result["apply"]["retry"]
                    assert "Referenced record has not arrived yet" in result["apply"]["reasons"]
                    assert link.sync_warning
                    status = transport.link_status()[0]
                    assert status["sync_warning"] == link.sync_warning
                    blocked = status["stalled_streams"][0]
                    assert blocked["passes"] == attempt + 1
                    assert blocked["first_seen_at"] > 0
                    assert blocked["head_seq"] == blocked["watermark"] + 1
                    assert blocked["reason"] == "Referenced record has not arrived yet"
                    assert status["last_error"] == link.sync_warning
                    assert database.get_session("blocked")["is_active"]
                    assert not result.get("repair_snapshot")
            assert result["repair_snapshot"]
            assert not database.get_session("blocked")["is_active"]
            assert not store.pending_holes()
            assert link.sync_warning is None
            assert link.last_sync_repair["state"] == "repaired"
            assert sum(record.message.startswith("Sync blocked at") for record in caplog.records) == 1
            # Tail normally after the state frontier, without more repair.
            with a.active():
                database.update_session("blocked", title="After repair")
                engine.scan_all()
            result = await link.sync_once(client, engine.require_context())
            assert database.get_session("blocked")["title"] == "After repair"
            assert not result.get("repair_snapshot")
    assert any(path == "/api/v1/network/peer/state" for _, path in served.requests)


async def test_missing_log_op_uses_verified_snapshot_and_drains(workers):
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
        # Drop a retained operation, as a forwarding peer after pruning can.
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


@pytest.mark.parametrize("failure", ["unavailable", "invalid", "old_copy"])
async def test_unsuccessful_repair_never_skips_hole_and_is_rate_limited(workers, monkeypatch, failure):
    from unittest.mock import AsyncMock

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Repair")
    join(a, b)
    with b.active():
        marks = store.local_watermarks()
        seq = int(marks[a.device_id]["1"])
        theirs = {a.device_id: {"1": seq + 2}}
        link = transport.PeerLink(a.device_id, [])
        copy = AsyncMock(return_value=0)
        if failure == "unavailable":
            copy.side_effect = transport.PeerStateUnavailable("not ready")
        elif failure == "invalid":
            copy.side_effect = engine.ApplyError("snapshot signature is invalid")
        monkeypatch.setattr(link, "receive_state", copy)
        for attempt in range(transport.STALLED_PULL_PASSES):
            summary = {"received": 0}
            if failure == "invalid" and attempt == transport.STALLED_PULL_PASSES - 1:
                with pytest.raises(engine.ApplyError, match="signature"):
                    await link._repair_stalled_pull(None, engine.require_context(), marks, marks, theirs, set(), summary)
            else:
                assert not await link._repair_stalled_pull(
                    None, engine.require_context(), marks, marks, theirs, set(), summary)
        assert store.local_watermarks() == marks
        assert link.sync_warning
        assert link.last_sync_repair["state"] == {
            "unavailable": "deferred", "invalid": "failed", "old_copy": "no_progress"}[failure]
        await link._repair_stalled_pull(None, engine.require_context(), marks, marks, theirs, set(), {"received": 0})
        copy.assert_awaited_once()


async def test_stale_incarnation_does_not_request_repair(workers, monkeypatch):
    from unittest.mock import AsyncMock

    a = workers("a")
    with a.active():
        engine.create_network("Repair")
        link = transport.PeerLink("peer", [])
        copy = AsyncMock()
        monkeypatch.setattr(link, "receive_state", copy)
        marks = {"peer": {"1": 3, "2": 4}}
        theirs = {"peer": {"1": 100, "2": 4}}
        for _ in range(transport.STALLED_PULL_PASSES):
            assert not await link._repair_stalled_pull(
                None, engine.require_context(), marks, marks, theirs, set(), {"received": 0})
        copy.assert_not_called()
        assert not link._stalled_streams
