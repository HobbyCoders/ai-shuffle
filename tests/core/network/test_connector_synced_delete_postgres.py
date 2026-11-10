"""A synced connector delete takes the connector lock before the row (#1399 review).

Real PostgreSQL (a disposable cluster, never the app database). One sync
chunk edits connector C and then deletes it. A second connection holds C's
journal lock and then updates C's row, as ``import_watch`` does through
``watch_journal.restore``. If the chunk writes C's row before it asks for the
lock, PostgreSQL finds a deadlock and aborts one of the two. A peer copy can
write C's row without touching C itself: deleting C's API key on an earlier
page sets ``connectors.api_user_id`` to NULL in the same transaction.
"""
import copy
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
from app.network import engine, peer_snapshot
from tests.core.network.harness import converge, join
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres

BOT = "bot-1399-lock-order"
KEY = "key-1399-lock-order"


def _clean(conn, origin):
    """Each case starts from a database that has never seen this connector."""
    c = conn.cursor()
    c.execute("DELETE FROM connectors WHERE id = %s", (BOT,))
    c.execute("DELETE FROM api_users WHERE id = %s", (KEY,))
    c.execute("DELETE FROM sync_records WHERE record_id IN (%s, %s)", (BOT, KEY))
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


@contextmanager
def _pg(cluster):
    with closing(pg_connect(cluster)) as conn:
        conn.autocommit = False
        conn.cursor().execute("SET statement_timeout TO 15000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _race_watch_import(cluster, apply):
    """Run ``apply`` while another connection holds C's lock and then updates C's row."""
    held, released = threading.Event(), threading.Event()
    importer_pid = []

    def importer():
        # import_watch's order: the connector lock, then restore's row update.
        with _pg(cluster) as conn:
            c = conn.cursor()
            importer_pid.append(conn.get_backend_pid())
            watch_store.lock_connector(c, BOT)
            held.set()
            assert released.wait(15), "the sync apply never queued behind the connector lock"
            watch_journal.restore(c, {"source": {"connector_id": BOT, "channel_id": "123"}},
                                  {"entries": [], "heads": []})

    with ThreadPoolExecutor(max_workers=2) as pool:
        imported = pool.submit(importer)
        assert held.wait(10)
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
                    if applied.done():
                        applied.result()  # its own error, if it failed before the lock
                    assert time.monotonic() < deadline, "the sync apply never asked for the connector lock"
                    time.sleep(0.01)
        finally:
            released.set()
        imported.result(timeout=30)
        return applied.result(timeout=30)


def _assert_deleted(cluster):
    with _pg(cluster) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM connectors WHERE id = %s", (BOT,))
        assert c.fetchone() is None
        c.execute("SELECT fence FROM watch_connector_journals WHERE connector_id = %s", (BOT,))
        assert c.fetchone()["fence"] == "deleted"


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

    def pg():
        return _pg(cluster)

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

    caplog.set_level(logging.DEBUG, logger=engine.logger.name)
    result = engine.ApplyResult()

    def apply():
        with b.active():
            if delivery == "chunk":
                return engine._apply_chunk(chunk, ctx, result)
            # The same ops buffered earlier, drained by the pending path.
            assert engine._apply_pending_chunk(origin, incarnation, ctx, result)
            return True

    committed = _race_watch_import(cluster, apply)

    # A deadlock victim inside the chunk is rolled back to its savepoint and
    # left for a retry; one outside it would fail the watch import above.
    failures = [record.exc_info[1] for record in caplog.records if record.exc_info]
    assert committed and result.applied == 2 and result.retry == result.rejected == 0, failures
    _assert_deleted(cluster)


def test_peer_copy_deleting_a_key_then_its_connector_takes_the_connector_lock_first(
        cluster, workers, monkeypatch, tmp_path):
    """One copy transaction deletes API key U on an earlier page and connector C on a later one.

    The foreign key's ON DELETE SET NULL writes C's row during U's delete, so
    the copy must hold C's lock before its first page.
    """
    a, b = workers("worker-a"), workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b)
    converge([a, b])
    # Worker A deletes the key and then its connector, as connector_service does.
    with a.active():
        database.create_api_user(KEY, "connector:" + BOT, "hash")
        database.create_connector(BOT, "telegram", "Bot", {}, [], api_user_id=KEY, is_active=False)
        engine.scan_all()
        assert database.delete_api_user(KEY)
        assert database.delete_connector(BOT)
        engine.scan_all()
        manifest = peer_snapshot.capture(b.device_id)
        for index in range(len(manifest["pages"])):
            (tmp_path / f"{index}.json").write_bytes(
                peer_snapshot.page(b.device_id, manifest["transfer"], index))
        with database.get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM sync_records WHERE record_id NOT IN (%s, %s)", (BOT, KEY))
            held = [dict(row) for row in c.fetchall()]
    collections = [page["collection"] for page in manifest["pages"]]
    assert collections.index("api_users") < collections.index("connectors")
    with b.active():
        receiver = copy.deepcopy(engine.context())

    # Worker B's database is the PostgreSQL cluster. It still holds U and C,
    # and C references U. It already holds A's version of every other record,
    # so the copy skips those (A's SQLite rows carry 0/1 for booleans, which
    # PostgreSQL refuses) and applies only the two tombstones.
    monkeypatch.setattr(store, "get_db", lambda: _pg(cluster))
    monkeypatch.setattr(engine, "get_db", lambda: _pg(cluster))
    monkeypatch.setattr(engine, "load_context", lambda conn=None, **_: receiver)
    monkeypatch.setattr(engine, "_notify", lambda _: None)
    with _pg(cluster) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM network_state")
        store.create_state(network_id=receiver.network_id, name=receiver.name,
                           founder_device_id=receiver.founder_device_id, keyring={}, _conn=conn)
        _clean(conn, a.device_id)
        for row in held:
            store.put_record(c, row["collection"], row["record_id"], clock=int(row["clock"]),
                             origin_device_id=row["origin_device_id"], content_hash=row["content_hash"],
                             deleted=bool(row["deleted"]), order_seq=row.get("order_seq"))
        c.execute("INSERT INTO api_users (id, name, api_key_hash) VALUES (%s, %s, 'hash')",
                  (KEY, "connector:" + BOT))
        c.execute("INSERT INTO connectors (id, type, name, api_user_id) VALUES (%s, 'telegram', 'Bot', %s)",
                  (BOT, KEY))

    def apply():
        with b.active():
            return peer_snapshot.apply(manifest, tmp_path, a.device_id)

    # Before the copy took C's lock up front: psycopg2.errors.DeadlockDetected.
    assert _race_watch_import(cluster, apply) > 0
    _assert_deleted(cluster)
    with _pg(cluster) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM api_users WHERE id = %s", (KEY,))
        assert c.fetchone() is None
