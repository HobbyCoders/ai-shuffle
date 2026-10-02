"""Offline sidebar edits use the real API producer and signed replication path."""
from unittest.mock import AsyncMock

import pytest

from app.db import database, network as store
from app.db.base import get_db
from app.network import engine
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_session_active_control import settle


@pytest.mark.parametrize("final_active", [False, True])
async def test_offline_repeated_edits_survive_restart_and_reconnect(workers, monkeypatch, final_active):
    a, b = workers("gaming"), workers("unraid")
    with a.active():
        engine.create_network("Home", device_name="DesktopWorker")
    join(a, b)
    _new_session(a, "offline", "Gaming session")
    converge([a, b])
    from app.core.sync_engine import sync_engine
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    with b.active():
        store.touch_member(a.device_id, endpoints=[])
        for active in (False, True, False, final_active):
            row = await settle("offline", active)
            assert bool(row["is_active"]) is active
        broadcast.assert_awaited()
        assert broadcast.call_args.kwargs["updates"]["is_active"] is final_active
        # Reload context from this worker's durable DB, not a fixture-seeded flag.
        engine.load_context()
        assert bool(database.get_session("offline")["is_active"]) is final_active
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            row = database.get_session("offline")
            assert bool(row["is_active"]) is final_active
            assert row["owner_device_id"] == a.device_id


@pytest.mark.parametrize("initial_active", [False, True])
async def test_held_offline_edit_survives_inbound_membership_before_scan(workers, initial_active):
    a, b = workers("gaming"), workers("unraid")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "held", "Gaming session")
    with a.active():
        await settle("held", initial_active)  # a real older shared membership record
    converge([a, b])
    with b.active():
        engine.hold_outbox(True)
        try:
            await settle("held", False)
            assert engine.scan_all() == 0
            assert b.sql("SELECT local_pending FROM network_session_active")[0]["local_pending"]
            engine.load_context()
            assert bool(database.get_session("held")["is_active"]) is False
        finally:
            engine.hold_outbox(False)
    with a.active():
        await settle("held", True)  # a reconnecting owner's previously unseen edit
    # Incoming ops arrive before the periodic scanner can sign the local edit.
    result = exchange(a, b)
    assert not result.rejected and not result.retry
    with b.active():
        assert bool(database.get_session("held")["is_active"]) is False, b.sql("SELECT * FROM network_session_active")
        assert not b.sql("SELECT local_pending FROM network_session_active")[0]["local_pending"]
    back = exchange(b, a)
    assert not back.rejected and not back.retry
    with a.active():
        assert bool(database.get_session("held")["is_active"]) is False
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            assert bool(database.get_session("held")["is_active"]) is False
            assert not worker.sql("SELECT local_pending FROM network_session_active")[0]["local_pending"]


async def test_membership_precedes_catalog_on_third_worker(workers):
    a, b, c = workers("gaming"), workers("unraid"), workers("third")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    _new_session(a, "later-catalog", "Gaming session")
    exchange(a, b)
    with b.active():
        await settle("later-catalog", False)
        # Deliver just b's stream; no catalog from a exists on c yet.
        ops = [op for op in store.ops_since({}) if op["origin_device_id"] == b.device_id]
    with c.active():
        result = engine.apply_batch(ops)
        assert not result.rejected and not result.retry
        assert database.get_session("later-catalog") is None
        assert c.sql("SELECT is_active FROM network_session_active WHERE session_id='later-catalog'")[0]["is_active"] == 0
    converge([a, b, c])
    with c.active():
        assert bool(database.get_session("later-catalog")["is_active"]) is False


async def test_concurrent_sidebar_edits_converge_and_do_not_republish_catalog(workers):
    a, b, c = workers("gaming"), workers("unraid"), workers("third")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    _new_session(a, "concurrent", "Gaming session")
    converge([a, b, c])
    with b.active():
        await settle("concurrent", False)
    with c.active():
        await settle("concurrent", True)
    # Different arrival orders still produce the same LWW winner.
    exchange(b, a)
    exchange(c, a)
    exchange(c, b)
    exchange(b, c)
    converge([a, b, c])
    states = []
    for worker in (a, b, c):
        with worker.active():
            row = database.get_session("concurrent")
            states.append(bool(row["is_active"]))
            assert row["owner_device_id"] == a.device_id
            engine.scan_all()
            if worker is not a:
                assert not [op for op in store.ops_since({})
                            if op["collection"] == "sessions" and op["origin_device_id"] == worker.device_id]
    assert len(set(states)) == 1


async def test_database_setter_joins_shared_conflict_order(workers):
    # The voice path uses this producer instead of the active API.
    a, b = workers("gaming"), workers("unraid")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "voice", "Gaming session")
    converge([a, b])
    with b.active():
        database.set_session_active("voice", False)
    converge([a, b])
    with a.active():
        database.set_session_active("voice", True)
    converge([a, b])
    with b.active():
        assert bool(database.get_session("voice")["is_active"]) is True


@pytest.mark.parametrize("payload", [
    {"session_id": "other", "is_active": False},
    {"session_id": "target", "is_active": "false"},
])
async def test_malformed_signed_membership_cannot_project_another_session(workers, payload):
    a, b = workers("gaming"), workers("unraid")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "target", "Gaming session")
    _new_session(a, "other", "Other session")
    converge([a, b])
    with a.active():
        with get_db() as conn:
            op = engine.record(conn.cursor(), "session_active", "target", payload, force=True)
    with b.active():
        result = engine.apply_batch([op])
        assert result.rejected == 1
        assert bool(database.get_session("target")["is_active"]) is True
        assert bool(database.get_session("other")["is_active"]) is True
