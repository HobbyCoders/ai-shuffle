"""A peer's note deletion keeps the cascaded attachment bytes on PostgreSQL (#1693).

Real PostgreSQL (a disposable cluster, never the app database): the bytes
go into a BYTEA column, expiry compares a TIMESTAMPTZ, the note is locked
with FOR UPDATE before the cascade, and an existing database gains the new
columns on upgrade. Worker B's database is the cluster; worker A's ops reach
it through the real chunk apply.
"""
from contextlib import closing, contextmanager

import pytest

from app.db import database, network as store
from app.db.schema_postgres import create_postgres_schema
from app.network import blobs, engine
from tests.core.network.harness import converge, join
from tests.db.schema_lifecycle_helpers import pg16_bindir, pg_connect, private_postgres

PAYLOAD = b"bytes only B ever held, on PostgreSQL"
DIGEST = blobs.sha256(PAYLOAD)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    bindir = pg16_bindir()
    if bindir is None:
        pytest.skip("requires PostgreSQL 16 server binaries")
    with private_postgres(bindir, tmp_path_factory.mktemp("attachment-cascade-pg")) as descriptor:
        with closing(pg_connect(descriptor)) as conn, conn:
            cursor = conn.cursor()
            create_postgres_schema(cursor)
            # A database from before the upgrade: no kept-bytes columns yet.
            cursor.execute("DROP INDEX idx_sync_conflicts_kept_blob")
            cursor.execute("ALTER TABLE sync_conflicts DROP COLUMN kept_blob_hash, DROP COLUMN kept_blob")
            store.create_schema(cursor)
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


def _ops(sender, receiver):
    """What ``receiver`` lacks from ``sender``, as signed wire envelopes."""
    with receiver.active():
        marks = store.local_watermarks()
    with sender.active():
        ops, _, needs_snapshot = store.exchange_batch(marks, exclude_origin=receiver.device_id)
    assert not needs_snapshot
    return ops


def test_peer_note_delete_keeps_attachment_bytes_on_postgres(cluster, workers, monkeypatch):
    a, b = workers("worker-a"), workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b)
    converge([a, b])
    with b.active():
        ctx = engine.context()
    with a.active():
        database.create_note(note_id="n1", title="Note", content="body")
        engine.scan_all()
    created = _ops(a, b)
    with a.active():
        database.delete_note("n1")
        engine.scan_all()
    delete = _ops(a, b)[len(created):]
    assert [(op["collection"], op["op"]) for op in delete] == [("notes", engine.OP_DELETE)]

    def pg():
        return _pg(cluster)

    monkeypatch.setattr(engine, "get_db", pg)
    monkeypatch.setattr(blobs, "get_db", pg)
    with pg() as conn:
        c = conn.cursor()
        store.create_state(network_id=ctx.network_id, name=ctx.name,
                           founder_device_id=ctx.founder_device_id, keyring={}, _conn=conn)
        store.set_watermark(c, delete[0]["origin_device_id"], delete[0]["origin_incarnation"],
                            delete[0]["origin_seq"] - 1)
        # B already holds the shared note (seeded directly: worker A signs the
        # shim's 0/1 booleans, which PostgreSQL would refuse). Then B attaches
        # a file A never sees: the bytes live only in this row.
        c.execute("INSERT INTO notes (id, title, content) VALUES ('n1', 'Note', 'body')")
        c.execute(
            "INSERT INTO note_attachments (id, note_id, filename, file_path, content_type, size_bytes, data, blob_hash) "
            "VALUES ('att1', 'n1', 'x.txt', 'x.txt', 'text/plain', %s, %s, %s)", (len(PAYLOAD), PAYLOAD, DIGEST))

    result = engine.ApplyResult()
    with b.active():
        assert engine._apply_chunk(delete, ctx, result)
        assert result.applied == 1 and result.retry == result.rejected == 0
        with pg() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM notes WHERE id = 'n1'")
            assert c.fetchone() is None
            c.execute("SELECT 1 FROM note_attachments WHERE id = 'att1'")
            assert c.fetchone() is None
            c.execute("SELECT record_id, losing_device_id, winning_device_id, losing_record "
                      "FROM sync_conflicts WHERE kept_blob_hash = %s", (DIGEST,))
            [conflict] = c.fetchall()
            assert conflict["record_id"] == "att1" and conflict["losing_device_id"] == b.device_id
            assert conflict["winning_device_id"] == a.device_id
            assert conflict["losing_record"]["blob_hash"] == DIGEST
        assert blobs.read_local(DIGEST) == PAYLOAD
        assert blobs.read_local_range(DIGEST, 6, 4) == (PAYLOAD[6:10], len(PAYLOAD))

        with pg() as conn:
            conn.cursor().execute("UPDATE sync_conflicts SET expires_at = NOW() - INTERVAL '1 second' "
                                  "WHERE kept_blob_hash = %s", (DIGEST,))
        assert blobs.read_local(DIGEST) is None

        # A's own tombstone for the attachment would release the copy; B's would not.
        with pg() as conn:
            assert blobs.release_covered_attachment(conn.cursor(), "att1", deleted_by=b.device_id) == 0
            assert blobs.release_covered_attachment(conn.cursor(), "att1", deleted_by=a.device_id) == 1
            c = conn.cursor()
            c.execute("SELECT 1 FROM sync_conflicts WHERE record_id = 'att1'")
            assert c.fetchone() is None
