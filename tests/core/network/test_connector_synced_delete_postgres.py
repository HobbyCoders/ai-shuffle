"""A synced connector delete takes the connector lock before the row (#1399 review).

Real PostgreSQL (a disposable cluster, never the app database). One sync
chunk edits connector C and then deletes it. A second connection holds C's
journal lock and then updates C's row, as ``import_watch`` does through
``watch_journal.restore``. If the chunk writes C's row before it asks for the
lock, PostgreSQL finds a deadlock and aborts one of the two.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

import pytest

from app.db import database, watch_journal
from app.db import network as store
from app.db import watches as watch_store
from app.db.schema_postgres import create_postgres_schema
from app.network import engine
from tests.core.network.harness import converge, join
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres

BOT = "bot-1399-lock-order"


def _clean(conn, origin):
    """Each case starts from a database that has never seen this connector."""
    c = conn.cursor()
    c.execute("DELETE FROM connectors WHERE id = %s", (BOT,))
    c.execute("DELETE FROM sync_records WHERE collection = 'connectors' AND record_id = %s", (BOT,))
    for table in ("watch_connector_journals", "watch_connector_inbox", "watch_connector_heads"):
        c.execute("DELETE FROM " + table + " WHERE connector_id = %s", (BOT,))
    for table in ("sync_watermarks", "sync_pending"):
        c.execute("DELETE FROM " + table + " WHERE origin_device_id = %s", (origin,))


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("connector-delete-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn, conn:
            create_postgres_schema(conn.cursor())
        yield descriptor


def _ops(sender, receiver):
    """What ``receiver`` lacks from ``sender``, as signed wire envelopes."""
    with receiver.active():
        marks = store.local_watermarks()
    with sender.active():
        ops, _, needs_snapshot = store.exchange_batch(marks, exclude_origin=receiver.device_id)
    assert not needs_snapshot
    return ops


@pytest.mark.parametrize("delivery", ["chunk", "pending"])
def test_chunk_editing_then_deleting_a_connector_waits_for_its_lock_first(
        cluster, workers, monkeypatch, caplog, delivery):
    a, b = workers("worker-a"), workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b)
    converge([a, b])
    with b.active():
        ctx = engine.context()

    @contextmanager
    def pg():
        with closing(pg_connect(cluster)) as conn:
            conn.autocommit = False
            conn.cursor().execute("SET statement_timeout TO 15000")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def publish(deleted=False):
        # Worker A runs on the SQLite shim, which stores booleans as 0/1. A
        # PostgreSQL worker publishes a real boolean: sign what it would send.
        with a.active(), database.get_db() as conn:
            row = None if deleted else engine._read_row(
                conn.cursor(), engine.policy.collection("connectors"), BOT)
            if row is not None:
                row = {**row, "is_active": bool(row["is_active"])}
            assert engine.record(conn.cursor(), "connectors", BOT, row) is not None

    with a.active():
        database.create_connector(BOT, "telegram", "Bot", {}, [], is_active=False)
    publish()
    created = _ops(a, b)
    with a.active():
        database.update_connector(BOT, name="Renamed bot")
    publish()
    with a.active():
        assert database.delete_connector(BOT)
    publish(deleted=True)
    every = _ops(a, b)
    chunk = every[len(created):]
    assert [(op["collection"], op["record_id"], op["op"]) for op in chunk] == [
        ("connectors", BOT, engine.OP_UPSERT), ("connectors", BOT, engine.OP_DELETE)]

    # Worker B's database is the PostgreSQL cluster from here on: it already
    # holds C (the earlier sync), so the chunk's edit locks an existing row.
    monkeypatch.setattr(engine, "get_db", pg)
    with pg() as conn:
        store.create_state(network_id=ctx.network_id, name=ctx.name,
                           founder_device_id=ctx.founder_device_id, keyring={}, _conn=conn)
        origin, incarnation = created[0]["origin_device_id"], created[0]["origin_incarnation"]
        _clean(conn, origin)
        store.set_watermark(conn.cursor(), origin, incarnation, created[0]["origin_seq"] - 1)
    seeded = engine.ApplyResult()
    with b.active():
        assert engine._apply_chunk(created, ctx, seeded)
    assert seeded.retry == seeded.rejected == 0
    with pg() as conn:
        c = conn.cursor()
        c.execute("SELECT name FROM connectors WHERE id = %s", (BOT,))
        assert c.fetchone()["name"] == "Bot"
    if delivery == "pending":
        with pg() as conn:
            store.stash_pending_batch(conn.cursor(), chunk)

    held, released = threading.Event(), threading.Event()
    importer_pid = []

    def importer():
        # import_watch's order: the connector lock, then restore's row update.
        with pg() as conn:
            c = conn.cursor()
            importer_pid.append(conn.get_backend_pid())
            watch_store.lock_connector(c, BOT)
            held.set()
            assert released.wait(15), "the sync apply never queued behind the connector lock"
            watch_journal.restore(c, {"source": {"connector_id": BOT, "channel_id": "123"}},
                                  {"entries": [], "heads": []})

    caplog.set_level(logging.DEBUG, logger=engine.logger.name)
    result = engine.ApplyResult()
    with ThreadPoolExecutor(max_workers=2) as pool:
        imported = pool.submit(importer)
        assert held.wait(10)

        def apply():
            with b.active():
                if delivery == "chunk":
                    return engine._apply_chunk(chunk, ctx, result)
                # The same ops buffered earlier, drained by the pending path.
                assert engine._apply_pending_chunk(origin, incarnation, ctx, result)
                return True

        applied = pool.submit(apply)
        try:
            deadline = time.monotonic() + 10
            with closing(pg_connect(cluster)) as observer:
                observer.autocommit = True
                while True:
                    c = observer.cursor()
                    c.execute("SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                              "AND pid <> %s", (importer_pid[0],))
                    if c.fetchone():
                        break
                    assert time.monotonic() < deadline, "the sync apply never asked for the connector lock"
                    time.sleep(0.01)
        finally:
            released.set()
        imported.result(timeout=30)
        committed = applied.result(timeout=30)

    # A deadlock victim inside the chunk is rolled back to its savepoint and
    # left for a retry; one outside it would fail the watch import above.
    failures = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert committed and result.applied == 2 and result.retry == result.rejected == 0, failures
    with pg() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM connectors WHERE id = %s", (BOT,))
        assert c.fetchone() is None
        c.execute("SELECT fence FROM watch_connector_journals WHERE connector_id = %s", (BOT,))
        assert c.fetchone()["fence"] == "deleted"
