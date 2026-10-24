"""The synced assistant research account; research itself stays on each worker.

The personal assistant's own conversation (its "home") was retired in #371
step 5, so nothing here opens, elects or protects one.
"""
import pytest

from app.core import assistant_service as service
from app.core.config import settings
from app.db import assistant as store, database
from app.network import engine, session_active
from tests.core.network.harness import converge, join


@pytest.fixture(autouse=True)
def _per_worker_install_ids(monkeypatch):
    monkeypatch.setattr(settings, "instance_id", None)
    monkeypatch.setattr(service, "_guard_version", 1)


def settle(workers, rounds=3):
    for _ in range(rounds):
        for worker in workers:
            with worker.active():
                service.reconcile()
                engine.scan_all()
        converge(workers)


def network(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="Desk")
    join(a, b, name="Server")
    return a, b


def set_up_owner():
    with store.get_db() as conn:
        store.write_account(conn, {"agent_id":"default", "introduction":{"detected":{"preferred_name":"Owner"}}})
    service.reconcile()


def test_owner_introduction_still_syncs_without_research(workers):
    a,b=network(workers)
    with a.active():
        set_up_owner()
        engine.scan_all()
    settle([a,b])
    for worker in (a,b):
        with worker.active():
            assert store.get_account()['introduction']['detected']['preferred_name']=='Owner'
            assert database.get_sessions(include_hidden=True)==[]
            from app.db.base import table_exists
            with store.get_db() as conn:
                assert not table_exists(conn.cursor(),'assistant_sources')


def test_older_peers_source_optins_sync_without_recreating_watches(workers):
    a,b=network(workers)
    with a.active():
        set_up_owner()
        with store.get_db() as conn:
            store.write_account(conn,{'sources':{'mail':True}})
        engine.scan_all()
    settle([a,b])
    for worker in (a,b):
        with worker.active():
            assert store.get_account()['sources']=={'mail':True}
            from app.db.watches import list_watches
            assert list_watches()==[]


def test_any_session_can_be_settled_and_archived(workers):
    """The persistent-home refusal is gone from every settle and archive path."""
    a, _b = network(workers)
    with a.active():
        for local in (True, False):
            row = database.create_session(f"local-{local}", "default", is_active=True)
            session_active._write(row["id"], False, local=local)
            assert not database.get_session(row["id"])["is_active"]
        shared = database.create_session("shared", "default", is_active=True)
        session_active.write_shared(shared["id"], False)
        assert not database.get_session(shared["id"])["is_active"]
        archived = database.create_session("archived", "default", is_active=True)
        session_active._write(archived["id"], True, archived=True, local=True)
        assert database.get_session(archived["id"])["status"] == "archived"


def test_account_collection_keeps_wire_compatibility():
    from app.network import policy
    coll = policy.collection("assistant")
    assert coll.table == "assistant_account" and coll.mode == policy.MODE_SHARED
    # Peers on an older release still send the retired name, avatar and home
    # fields, so the collection keeps them.
    assert set(coll.hash_fields) == {"id", "agent_id", "enabled", "paused", "sources", "introduction",
                                     "home_session_id", "home_device_id", "home_created_at",
                                     "assistant_name", "avatar_id"}
    assert not {"assistant_state", "assistant_sources", "assistant_runs", "assistant_events"} & {
        c.table for c in policy.COLLECTIONS.values()}
