"""A repeated peer heartbeat writes the member row only when something changed."""
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.db import network as store
from app.network import engine, transport
from tests.core.network.harness import Worker
from tests.db.schema_lifecycle_helpers import pg_connect

PEER = "dev_heartbeat_peer"


@pytest.fixture
def worker(tmp_path):
    instance = Worker("heartbeat", tmp_path)
    yield instance
    instance.close()
    engine.reset_for_tests()
    transport.reset_for_tests()


def add_peer():
    store.upsert_member(device_id=PEER, name="Peer", kind="worker", signing_public="sign",
                        encryption_public="encrypt", generation=1)


def member():
    with store.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_members WHERE device_id = %s", (PEER,))
        return dict(cursor.fetchone())


def age_last_seen(seconds):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store.get_db() as conn:
        conn.cursor().execute("UPDATE network_members SET last_seen = %s, updated_at = %s WHERE device_id = %s",
                              (stamp, stamp, PEER))
    return member()


DESCRIPTION = {"capabilities": {"sync": 3, "lanes": ["catalog"]}, "endpoints": ["http://peer.local:8000"],
               "version": "1.2.3", "incarnation": 4, "platform": "linux"}


def test_first_contact_and_a_changed_description_are_written(worker):
    with worker.active():
        add_peer()
        assert member()["last_seen"] is None
        store.touch_member(PEER, **DESCRIPTION)
        first = member()
        assert first["last_seen"] is not None
        assert store._load(first["endpoints"]) == DESCRIPTION["endpoints"]
        assert store._load(first["capabilities"]) == DESCRIPTION["capabilities"]
        assert (first["version"], first["incarnation"], first["platform"]) == ("1.2.3", 4, "linux")

        for change in ({"endpoints": ["http://peer.local:9000"]}, {"capabilities": {"sync": 4}},
                       {"version": "1.2.4"}, {"incarnation": 5}, {"platform": "win32"}, {"name": "Renamed"}):
            before = age_last_seen(5)
            store.touch_member(PEER, **{**DESCRIPTION, **change})
            after = member()
            assert after["updated_at"] != before["updated_at"], change
            store.touch_member(PEER, **DESCRIPTION)


def test_an_identical_heartbeat_inside_the_refresh_window_writes_nothing(worker):
    with worker.active():
        add_peer()
        store.touch_member(PEER, **DESCRIPTION)
        before = age_last_seen(store.MEMBER_SEEN_REFRESH_SECONDS - 30)
        store.touch_member(PEER, **DESCRIPTION)
        assert store.touch_member(PEER, heartbeat=True, **DESCRIPTION) is True
        store.touch_member(PEER)
        assert member() == before


def test_last_seen_is_refreshed_once_the_window_has_passed(worker):
    with worker.active():
        add_peer()
        store.touch_member(PEER, **DESCRIPTION)
        before = age_last_seen(store.MEMBER_SEEN_REFRESH_SECONDS + 30)
        store.touch_member(PEER, heartbeat=True, **DESCRIPTION)
        after = member()
        assert after["last_seen"] != before["last_seen"]
        assert store._load(after["endpoints"]) == DESCRIPTION["endpoints"]


# PostgreSQL: row locks and JSONB comparison, which the in-memory harness cannot show.

@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    from tests.db import schema_lifecycle_helpers as helpers

    bindir = helpers.pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with helpers.private_postgres(bindir, tmp_path_factory.mktemp("member-heartbeat-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn:
            store.create_schema(conn.cursor())
        yield descriptor


@pytest.fixture
def postgres(cluster, monkeypatch):
    @contextmanager
    def get_db():
        with closing(pg_connect(cluster)) as conn:
            conn.autocommit = False
            # A regression that waits on the row fails here instead of hanging the run.
            conn.cursor().execute("SET statement_timeout TO 15000")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    monkeypatch.setattr(store, "get_db", get_db)
    with closing(pg_connect(cluster)) as conn:
        conn.cursor().execute("DELETE FROM network_members WHERE device_id = %s", (PEER,))
    add_peer()
    store.touch_member(PEER, **DESCRIPTION)
    return cluster


@contextmanager
def row_locked(cluster):
    with closing(pg_connect(cluster)) as holder:
        holder.autocommit = False
        holder.cursor().execute("SELECT device_id FROM network_members WHERE device_id = %s FOR UPDATE", (PEER,))
        try:
            yield
        finally:
            holder.rollback()


def test_postgres_compares_the_description_by_value(postgres):
    before = member()
    reordered = {**DESCRIPTION, "capabilities": {"lanes": ["catalog"], "sync": 3}}
    store.touch_member(PEER, **reordered)
    assert member() == before
    store.touch_member(PEER, **{**DESCRIPTION, "capabilities": {"lanes": ["catalog", "events"], "sync": 3}})
    after = member()
    assert after["updated_at"] > before["updated_at"]
    assert after["capabilities"] == {"lanes": ["catalog", "events"], "sync": 3}


def test_postgres_unchanged_heartbeat_does_not_wait_for_a_locked_row(postgres):
    before = member()
    with row_locked(postgres):
        started = time.monotonic()
        store.touch_member(PEER, **DESCRIPTION)
        assert store.touch_member(PEER, heartbeat=True, **DESCRIPTION) is True
        assert time.monotonic() - started < 5
    assert member() == before


def test_postgres_changed_heartbeat_gives_up_on_a_busy_row_and_lands_next_time(postgres, monkeypatch):
    monkeypatch.setattr(store, "MEMBER_HEARTBEAT_LOCK_MS", 200)
    before = member()
    moved = {**DESCRIPTION, "endpoints": ["http://peer.local:9000"]}
    with row_locked(postgres):
        started = time.monotonic()
        assert store.touch_member(PEER, heartbeat=True, **moved) is False
        assert time.monotonic() - started < 5
    assert member() == before
    assert store.touch_member(PEER, heartbeat=True, **moved) is True
    assert member()["endpoints"] == ["http://peer.local:9000"]
