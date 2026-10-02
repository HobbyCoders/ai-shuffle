"""Shared assistant identity, independent persistent homes on each worker."""
import uuid

import pytest

from app.core import assistant_service as service
from app.core.config import settings
from app.db import assistant as store, database
from app.db import session_events as events_db
from app.network import control, engine, startup, session_active
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


def sql(worker, query, params=()):
    with worker.active():
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            try:
                return [dict(row) for row in cursor.fetchall()]
            except Exception:
                return []


def test_identity_syncs_but_each_worker_opens_its_own_hidden_home(workers, monkeypatch):
    a, b = network(workers)
    monkeypatch.setattr(engine, "_listeners", [startup._on_applied])
    with a.active():
        service.configure({"create_agent": True, "assistant_name": "Ada", "avatar_id": "fox"})
        first = service.open_home()
        events_db.append_events(first["id"], [{"type": "message.user", "payload": {
            "type": "message.user", "content": "Desktop history"}}])
        engine.scan_all()
    converge([a, b])
    with b.active():
        state = service.get_state()
        assert state["configured"] and state["assistant_name"] == "Ada" and state["avatar_id"] == "fox"
        assert state["home_session_id"] is None and state["runs_here"]
        second = service.open_home()
        assert second["id"] != first["id"] and service._owned_here(second)
        assert control.owner_of(second) is None
        assert service.open_home()["id"] == second["id"]
        assert service.brief_destination() == (second["id"], None)
        service.configure({"assistant_name": "Ada Lovelace"})
        engine.scan_all()
    settle([a, b])
    for worker, home in ((a, first), (b, second)):
        with worker.active():
            state = service.get_state()
            assert state["assistant_name"] == "Ada Lovelace"
            assert state["home_session_id"] == home["id"] and state["runs_here"]
            assert state["home_worker"]["is_self"]
            assert service.open_home()["id"] == home["id"]
            assert database.get_session(home["id"])["hidden"]
            assert database.get_sessions() == []  # No assistant cards in Active/Settled.
            assert database.get_session(first["id"]) is not None
            assert database.get_session(second["id"]) is not None
    with a.active():
        assert [e["payload"]["content"] for e in events_db.get_events(first["id"], 0)] == ["Desktop history"]


def test_open_needs_only_identity_not_peer_transcript_or_runner(workers, monkeypatch):
    a, b = network(workers)
    with a.active():
        service.configure({"create_agent": True})
        first = service.open_home()
        engine.scan_all()
        from app.db import network as net_store
        ops = [op for op in net_store.ops_since({}) if op["collection"] in ("agents", "assistant")]
    with b.active():
        engine.apply_batch(ops)
        assert database.get_session(first["id"]) is None
        def no_remote(*args, **kwargs):
            pytest.fail("Opening this worker's assistant must not contact the desktop")
        monkeypatch.setattr(control, "call", no_remote)
        second = service.open_home()
        assert second["id"] != first["id"] and service._owned_here(second)
        assert service.open_home()["id"] == second["id"]
        assert service.detection_wanted()


def test_simultaneous_setup_does_not_elect_or_retire_homes(workers):
    a, b = network(workers)
    made = {}
    for worker in (a, b):
        with worker.active():
            service.configure({"create_agent": True})
            made[worker.name] = service.open_home()["id"]
    settle([a, b])
    for worker in (a, b):
        with worker.active():
            assert service.open_home()["id"] == made[worker.name]
            for sid in made.values():
                assert database.get_session(sid)["title"] == service.HOME_TITLE
            assert service.get_state()["runs_here"]


def test_pre_sync_local_homes_and_history_are_preserved(workers):
    a, b = network(workers)
    made = {}
    for worker in (a, b):
        with worker.active():
            home = database.create_session(str(uuid.uuid4()), "default", title=service.HOME_TITLE)
            store.configure(service.assistant_id(), {"agent_id": "default", "home_session_id": home["id"]})
            made[worker.name] = home["id"]
    settle([a, b])
    for worker in (a, b):
        with worker.active():
            assert service.open_home()["id"] == made[worker.name]
            assert database.get_session(made[worker.name])["hidden"]
            assert database.get_session(made[worker.name])["is_active"]


def test_upgrade_recovers_owned_legacy_home_but_ignores_remote_projection(workers):
    a, b = network(workers)
    with a.active():
        service.configure({"create_agent": True})
        old = service.open_home()
        with store.get_db() as conn:
            store.write_account(conn, {"home_session_id": old["id"], "home_device_id": a.device_id,
                                       "home_created_at": str(old["created_at"])})
            # Old release: its home still appears as a normal session card.
            conn.cursor().execute("UPDATE sessions SET hidden=FALSE WHERE id=%s", (old["id"],))
        engine.scan_all()
    converge([a, b])
    with b.active():
        assert not database.get_session(old["id"])["hidden"]
        assert database.get_sessions() == []
        assert database.get_sessions_paginated() == ([], 0)
        assert database.get_sessions_cursor()[0] == []
        assert old["id"] in {s["id"] for s in database.get_sessions(include_hidden=True)}
    # Simulate the previous release projecting A's shared home onto both rows.
    for worker in (a, b):
        with worker.active():
            store.configure(service.assistant_id(), {"home_session_id": old["id"]})
    with a.active():
        assert service.open_home()["id"] == old["id"]
    with b.active():
        new = service.open_home()
        assert new["id"] != old["id"] and service._owned_here(new)
    settle([a, b])
    with b.active():
        assert service.open_home()["id"] == new["id"]
        assert database.get_session(old["id"]) is not None
    # The legacy account binding is not rewritten by B's local open.
    with a.active():
        assert store.get_account()["home_session_id"] == old["id"]
        assert service.open_home()["id"] == old["id"]


def test_sources_sync_but_availability_and_research_stay_local(workers):
    a, b = network(workers)
    with a.active():
        database.create_connector("mail", "gmail", "Desktop mail", {}, [], is_active=True,
                                  runner_id=service.assistant_id())
        database.update_connector_status("mail", "connected")
        service.configure({"create_agent": True})
        service.open_home()
        service.set_source("mail", True)
        engine.scan_all()
    settle([a, b])
    with b.active():
        assert store.get_account()["sources"] == {"mail": True}
        source = next(s for s in service.source_catalog() if s["id"] == "mail")
        assert source["enabled"] and not source["available"]
        assert service.get_state()["status"] == "no_sources"
        assert service.get_state()["research_sources"] == 0
        assert service.record_proactive_event("mail", "email_received", {"message_id": "peer"}) == []
        assert store.admit_run(service.assistant_id()) is None
        service.set_source("mail", False)
        engine.scan_all()
    settle([a, b])
    with a.active():
        assert store.get_account()["sources"] == {}
        assert not store.get_sources(service.assistant_id())[0]["enabled"]


def test_network_local_and_peer_settlement_refuse_persistent_home(workers):
    a, b = network(workers)
    with a.active():
        service.configure({"create_agent": True})
        home = service.open_home()
        for local in (True, False):
            with pytest.raises(PermissionError, match="persistent"):
                session_active._write(home["id"], False, local=local)
            # Archiving a home is refused too, whatever is_active asks for.
            with pytest.raises(PermissionError, match="persistent"):
                session_active._write(home["id"], True, archived=True, local=local)
        # The shared sidebar path (settle while the owner is offline) refuses it as well.
        with pytest.raises(PermissionError, match="persistent"):
            session_active.write_shared(home["id"], False)
        with pytest.raises(PermissionError, match="persistent"):
            database.set_session_active(home["id"], False)
        assert database.get_session(home["id"])["is_active"]
        assert database.get_session(home["id"])["status"] != "archived"
        ordinary = database.create_session("ordinary", "default", is_active=True)
        session_active._write(ordinary["id"], False, local=True)
        assert not database.get_session(ordinary["id"])["is_active"]
        shared = database.create_session("shared", "default", is_active=True)
        session_active.write_shared(shared["id"], False)
        assert not database.get_session(shared["id"])["is_active"]


def test_account_collection_keeps_wire_compatibility():
    from app.network import policy
    coll = policy.collection("assistant")
    assert coll.table == "assistant_account" and coll.mode == policy.MODE_SHARED
    assert set(coll.hash_fields) == {"id", "agent_id", "enabled", "paused", "sources", "introduction",
                                     "home_session_id", "home_device_id", "home_created_at"}
    assert not {"assistant_state", "assistant_sources", "assistant_runs", "assistant_events"} & {
        c.table for c in policy.COLLECTIONS.values()}
