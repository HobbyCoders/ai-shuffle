"""One assistant per owner across workers: sync, one home, one research runner."""
import json
import logging
import uuid
from datetime import timedelta

import pytest

from app.core import assistant_service as service
from app.core.config import settings
from app.db import assistant as store, database
from app.db import session_events as events_db
from app.network import control, engine, startup
from tests.core.network.harness import converge, join


@pytest.fixture(autouse=True)
def _per_worker_install_ids(monkeypatch):
    # Each harness worker then gets its own persisted install id.
    monkeypatch.setattr(settings, "instance_id", None)
    monkeypatch.setattr(service, "_guard_version", 1)


def settle(workers, rounds=3):
    """What the manager tick and the scanner do on every worker, repeated."""
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
    """Run shim SQL (``%s`` placeholders) on ``worker`` and commit it."""
    with worker.active():
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            try:
                return [dict(row) for row in cursor.fetchall()]
            except Exception:  # noqa: BLE001 - statements without rows
                return []


def homes(worker):
    """Sessions on ``worker`` that are or were an assistant home."""
    return sql(worker, "SELECT id, title, owner_device_id FROM sessions WHERE title IN (%s, %s) ORDER BY id",
               (service.HOME_TITLE, service.RETIRED_HOME_TITLE))


def runner_row(worker):
    with worker.active():
        return store.get_state(service.assistant_id())


def test_configuration_syncs_and_a_second_worker_opens_the_one_home(workers, monkeypatch):
    a, b = network(workers)
    monkeypatch.setattr(engine, "_listeners", [startup._on_applied])
    with a.active():
        service.configure({"create_agent": True, "assistant_name": "Ada", "avatar_id": "fox", "enabled": True})
        home = service.open_home()
        engine.scan_all()
    converge([a, b])

    with b.active():
        # Applying A's rows re-ran reconcile here: no manual step needed.
        assert store.get_state(service.assistant_id())["home_session_id"] == home["id"]
        state = service.get_state()
        assert state["configured"] and state["assistant_name"] == "Ada" and state["avatar_id"] == "fox"
        assert state["enabled"] and state["status"] == "ready"
        assert state["home_session_id"] == home["id"]
        assert not state["runs_here"]
        assert state["home_worker"]["device_id"] == a.device_id and not state["home_worker"]["is_self"]
        # B opens A's conversation as a remote session; it never mints its own.
        opened = service.open_home()
        assert opened["id"] == home["id"]
        assert control.owner_of(opened) == a.device_id
        # Research is A's job: B's runner row has it off.
        assert store.get_state(service.assistant_id())["enabled"] is False or \
            not store.get_state(service.assistant_id())["enabled"]
    assert [row["id"] for row in homes(b)] == [home["id"]]

    # An edit made on B reaches A.
    with b.active():
        service.configure({"paused": True, "assistant_name": "Ada Lovelace"})
        engine.scan_all()
    converge([a, b])
    with a.active():
        state = service.get_state()
        assert state["paused"] and state["assistant_name"] == "Ada Lovelace" and state["runs_here"]
        assert store.get_state(service.assistant_id())["paused"]
        assert store.get_state(service.assistant_id())["enabled"]
    assert [row["id"] for row in homes(a)] == [home["id"]]


def test_opening_before_the_home_syncs_waits_instead_of_creating_one(workers):
    a, b = network(workers)
    with a.active():
        service.configure({"create_agent": True})
        service.open_home()
        engine.scan_all()
    # Deliver the account and agent rows, but not the session catalog yet.
    with a.active():
        from app.db import network as net_store
        ops = [op for op in net_store.ops_since({}) if op["collection"] in ("agents", "assistant")]
    with b.active():
        engine.apply_batch(ops)
        with pytest.raises(ValueError, match="hasn't reached this worker"):
            service.open_home()
        assert not service.get_state()["runs_here"]
    assert homes(b) == []


@pytest.mark.parametrize("older", ["a", "b"])
def test_simultaneous_setup_elects_the_older_home_on_both_workers(workers, older):
    a, b = network(workers)
    first, second = (a, b) if older == "a" else (b, a)
    opened = {}
    for worker in (first, second):
        with worker.active():
            service.configure({"create_agent": True, "enabled": True})
            opened[worker.name] = service.open_home()["id"]
            engine.scan_all()
    winner = opened[first.name]
    loser = opened[second.name]
    settle([a, b])

    for worker in (a, b):
        with worker.active():
            assert store.get_account()["home_session_id"] == winner
            assert service.open_home()["id"] == winner
            assert store.get_state(service.assistant_id())["home_session_id"] == winner
        rows = {row["id"]: row for row in homes(worker)}
        # The losing conversation is kept, under a title that says what it was.
        assert rows[winner]["title"] == service.HOME_TITLE
        assert rows[loser]["title"] == service.RETIRED_HOME_TITLE
    # Exactly one worker runs research: the one that owns the home.
    assert runner_row(first)["enabled"] and not runner_row(second)["enabled"]
    with second.active():
        assert not service.get_state()["runs_here"]
        assert store.admit_run(service.assistant_id()) is None


def test_two_existing_homes_migrate_to_one_and_keep_history(workers, caplog):
    a, b = network(workers)
    made = {}
    # Before this change each worker kept its own assistant: a runner row and
    # a home, and no account row. B's home is the older one.
    for worker in (b, a):
        with worker.active():
            session = database.create_session(str(uuid.uuid4()), "default", title=service.HOME_TITLE,
                                              is_active=True, target_instance_id=service.assistant_id())
            events_db.append_events(session["id"], [{"type": "message.user", "payload": {
                "type": "message.user", "content": "remember this, " + worker.name}}])
            store.configure(service.assistant_id(), {"agent_id": "default", "enabled": True})
            sql(worker, "UPDATE assistant_state SET home_session_id=%s WHERE id=%s", (session["id"], service.assistant_id()))
            made[worker.name] = session["id"]
            assert store.get_account() is None
    with caplog.at_level(logging.WARNING, logger="app.core.assistant_service"):
        settle([a, b])

    for worker in (a, b):
        with worker.active():
            assert store.get_account()["home_session_id"] == made["b"]
            assert service.get_state()["home_session_id"] == made["b"]
    # A's old home is still there, with its transcript, on A.
    with a.active():
        kept = database.get_session(made["a"])
        assert kept["title"] == service.RETIRED_HOME_TITLE
        assert [e["payload"]["content"] for e in events_db.get_events(made["a"], 0)] == ["remember this, a"]
        assert not store.get_state(service.assistant_id())["enabled"]
    assert runner_row(b)["enabled"]
    assert any(made["a"] in record.getMessage() and "yields" in record.getMessage() for record in caplog.records)


def test_research_stops_on_a_worker_that_loses_the_home(workers):
    a, b = network(workers)
    with b.active():
        service.configure({"create_agent": True, "enabled": True})
        home = service.open_home()
        # A research run B admitted while it still believed it ran the home.
        run_id = str(uuid.uuid4())
        deadline = (store.now() + timedelta(minutes=3)).isoformat()
        sql(b, """INSERT INTO assistant_runs (id,assistant_id,home_session_id,status,event_ids,source_ids,deadline_at,agent_id,limits)
                 VALUES (%s,%s,%s,'running','[]','[]',%s,%s,'{}')""",
              (run_id, service.assistant_id(), home["id"], deadline, store.get_account()["agent_id"]))
        run = store.get_run(run_id)
        service.assert_proactive_allowed(service.proactive_metadata(run))
    # A set up first, so its home is older and wins once the workers meet.
    with a.active():
        older = database.create_session(str(uuid.uuid4()), "default", title=service.HOME_TITLE,
                                         is_active=True, target_instance_id=service.assistant_id())
        with store.get_db() as conn:
            store.write_account(conn, dict(service._home_fields(older), agent_id="default", enabled=True))
        older_created = service._ts(older["created_at"]) - timedelta(days=1)
        sql(a, "UPDATE sessions SET created_at=%s WHERE id=%s", (older_created.isoformat(), older["id"]))
        sql(a, "UPDATE assistant_account SET home_created_at=%s", (older_created.isoformat(),))
    settle([a, b])
    with b.active():
        assert store.get_account()["home_session_id"] == older["id"]
        with pytest.raises(ValueError, match="disabled"):
            service.assert_proactive_allowed(service.proactive_metadata(store.get_run(run_id)))
        assert store.admit_run(service.assistant_id()) is None
    assert runner_row(a)["enabled"]


def test_a_home_on_a_removed_worker_is_replaced_and_kept_as_history(workers):
    a, b = network(workers)
    with b.active():
        service.configure({"create_agent": True, "enabled": True})
        lost = service.open_home()["id"]
        engine.scan_all()
    converge([a, b])
    with a.active():
        assert service.open_home()["id"] == lost
        engine.revoke_device(b.device_id, reason="lost")
        # B can never run or publish that conversation again: start one here.
        replacement = service.open_home()
        assert replacement["id"] != lost and service._owned_here(replacement)
        assert store.get_account()["home_session_id"] == replacement["id"]
        assert database.get_session(lost) is not None
        state = service.get_state()
        assert state["runs_here"] and state["home_session_id"] == replacement["id"]
        assert store.get_state(service.assistant_id())["enabled"]


def test_sources_sync_and_only_the_runner_keeps_them_on(workers):
    a, b = network(workers)
    with a.active():
        connector = database.create_connector("mail", "gmail", "Owner mail", {}, [], is_active=True,
                                              runner_id=service.assistant_id())
        database.update_connector_status(connector["id"], "connected")
        service.configure({"create_agent": True, "enabled": True})
        service.open_home()
        service.set_source("mail", True)
        engine.scan_all()
    settle([a, b])
    with a.active():
        assert [s["enabled"] for s in store.get_sources(service.assistant_id())] == [True]
    with b.active():
        assert store.get_account()["sources"] == {"mail": True}
        assert next(s for s in service.source_catalog() if s["id"] == "mail")["enabled"]
        # B does not observe: its runner row keeps no opted-in source.
        assert not any(s["enabled"] for s in store.get_sources(service.assistant_id()))
        service.set_source("mail", False)
        engine.scan_all()
    settle([a, b])
    with a.active():
        assert store.get_account()["sources"] == {}
        assert [s["enabled"] for s in store.get_sources(service.assistant_id())] == [False]


def test_account_collection_leaves_other_collections_alone():
    from app.network import policy
    coll = policy.collection("assistant")
    assert coll.table == "assistant_account" and coll.mode == policy.MODE_SHARED
    assert "assistant" in policy.SCANNED_COLLECTIONS
    assert set(coll.hash_fields) == {"id", "agent_id", "enabled", "paused", "sources", "introduction",
                                     "home_session_id", "home_device_id", "home_created_at"}
    # Runner-local tables never leave the worker.
    assert not {"assistant_state", "assistant_sources", "assistant_runs", "assistant_events"} & {
        c.table for c in policy.COLLECTIONS.values()}
    json.dumps(policy.inventory())
