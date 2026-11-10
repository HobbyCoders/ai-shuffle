"""A whole-collection scan reads only the rows that moved (real PostgreSQL).

The scanner used to read every row in full on every pass to learn that
nothing had changed. These tests pin both halves of the replacement: an
unchanged pass moves no payload out of the database, and every kind of
change the full read caught is still caught.
"""
from contextlib import contextmanager

import pytest

from app.db import database
from app.db import network as store
from app.network import engine, policy
from tests.core.network.test_transcript_op_retention_postgres import (  # noqa: F401
    cluster, pg_workers,
)

FINGERPRINT = engine._SCAN_FINGERPRINT_COLUMN


@contextmanager
def returned_rows(monkeypatch, module):
    """Observe the rows that cross the database boundary, statement by statement.

    Everything else about the connection (its server version, commit and
    rollback) is the real one's.
    """
    original = module.get_db
    reads = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
            self.query = ""

        def __getattr__(self, name):
            return getattr(self.cursor, name)

        def execute(self, query, *args):
            self.query = query
            return self.cursor.execute(query, *args)

        def fetchall(self):
            rows = self.cursor.fetchall()
            reads.append((self.query, [dict(row) for row in rows]))
            return rows

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def cursor(self):
            return Cursor(self.connection.cursor())

    @contextmanager
    def observed():
        with original() as connection:
            yield Connection(connection)

    with monkeypatch.context() as patch:
        patch.setattr(module, "get_db", observed)
        yield reads


@pytest.fixture
def owner(pg_workers):  # noqa: F811
    worker = pg_workers("scan-fingerprint")
    with worker.active():
        engine.create_network("Scan fingerprints")
        yield worker


def _sessions(count, prefix="s"):
    ids = [f"{prefix}{index:03d}" for index in range(count)]
    for session_id in ids:
        database.create_session(session_id=session_id, profile_id="default", project_id=None, title=session_id)
    return ids


def _payloads(reads, table="sessions", keys=("id",)):
    """Full rows that crossed the database boundary (not key/fingerprint pairs)."""
    return [row for query, rows in reads if f"FROM {table} " in query + " " for row in rows
            if set(row) - {FINGERPRINT, *keys}]


def _scan(monkeypatch, **kwargs):
    with returned_rows(monkeypatch, engine) as reads:
        published = engine.scan_collection("sessions", owner_only=True, **kwargs)
    return published, sorted(row["id"] for row in _payloads(reads))


def _execute(sql, params=()):
    with store.get_db() as conn:
        conn.cursor().execute(sql, params)


def _full_read_agrees(monkeypatch):
    """The full read (kill switch on) finds nothing the fingerprint pass left."""
    with monkeypatch.context() as patch:
        patch.setenv(engine.SCAN_FINGERPRINT_ENV, "0")
        with returned_rows(monkeypatch, engine) as reads:
            assert engine.scan_collection("sessions", owner_only=True) == 0
        assert not any(FINGERPRINT in query for query, _rows in reads)


def test_unchanged_pass_reads_no_payload_and_changes_are_still_published(owner, monkeypatch):
    ids = _sessions(7)
    assert _scan(monkeypatch) == (7, ids)  # cold: every row read once and published
    # Published rows are verified once more against their records, then trusted.
    assert _scan(monkeypatch) == (0, [])
    assert _scan(monkeypatch) == (0, [])

    _execute("UPDATE sessions SET title = 'edited elsewhere' WHERE id = 's001'")
    _execute("DELETE FROM sessions WHERE id = 's002'")
    [added] = _sessions(1, prefix="new")
    assert _scan(monkeypatch) == (3, sorted([added, "s001"]))
    assert _scan(monkeypatch) == (0, [])
    with store.get_db() as conn:
        cursor = conn.cursor()
        assert store.get_record(cursor, "sessions", "s002")["deleted"]
        assert not store.get_record(cursor, "sessions", "s001")["deleted"]
        coll = policy.collection("sessions")
        cursor.execute("SELECT * FROM sessions")
        for row in cursor.fetchall():
            digest = engine.content_hash(coll, engine.serialize_row(coll, dict(row)))
            assert store.get_record(cursor, "sessions", row["id"])["content_hash"] == digest
    _full_read_agrees(monkeypatch)


def test_volatile_only_change_is_read_once_and_not_published(owner, monkeypatch):
    _sessions(3)
    assert _scan(monkeypatch)[0] == 3
    # updated_at is outside the content hash: the row moved, the record did not.
    _execute("UPDATE sessions SET updated_at = updated_at + interval '1 hour' WHERE id = 's000'")
    assert _scan(monkeypatch) == (0, ["s000"])
    assert _scan(monkeypatch) == (0, [])
    _full_read_agrees(monkeypatch)


def test_a_record_that_moved_is_republished_although_its_row_did_not(owner, monkeypatch):
    _sessions(3)
    assert _scan(monkeypatch)[0] == 3
    assert _scan(monkeypatch) == (0, [])
    with store.get_db() as conn:
        cursor = conn.cursor()
        record = store.get_record(cursor, "sessions", "s001")
        store.put_record(cursor, "sessions", "s001", clock=record["clock"], origin_device_id=owner.device_id,
                         content_hash="someone else's version", deleted=False)
        store.put_record(cursor, "sessions", "s002", clock=record["clock"], origin_device_id=owner.device_id,
                         content_hash=store.get_record(cursor, "sessions", "s002")["content_hash"], deleted=True)
    assert _scan(monkeypatch) == (2, ["s001", "s002"])
    assert _scan(monkeypatch) == (0, [])
    _full_read_agrees(monkeypatch)


def test_named_records_pass_publishes_retracts_and_keeps_the_cache_current(owner, monkeypatch):
    ids = _sessions(5)
    assert _scan(monkeypatch)[0] == 5
    assert _scan(monkeypatch) == (0, [])
    _execute("UPDATE sessions SET title = 'renamed' WHERE id IN ('s000', 's003')")
    _execute("DELETE FROM sessions WHERE id = 's001'")
    # Only the named records are considered; s003 waits for a pass that names it.
    assert _scan(monkeypatch, record_ids=["s000", "s001", "s004"]) == (2, ["s000"])
    assert _scan(monkeypatch, record_ids=["s000", "s001", "s004"]) == (0, [])
    assert _scan(monkeypatch) == (1, ["s003"])
    assert _scan(monkeypatch) == (0, [])
    assert ids and _scan(monkeypatch, record_ids=["missing"]) == (0, [])
    _full_read_agrees(monkeypatch)


def test_changed_rows_are_read_in_bounded_statements(owner, monkeypatch):
    monkeypatch.setattr(engine, "SCAN_FETCH_BATCH", 4)
    ids = _sessions(10)
    with returned_rows(monkeypatch, engine) as reads:
        assert engine.scan_collection("sessions", owner_only=True) == 10
    batches = [len(rows) for query, rows in reads if "FROM sessions " in query and _payloads([(query, rows)])]
    assert batches == [4, 4, 2]
    assert sorted(row["id"] for row in _payloads(reads)) == ids


def test_periodic_verification_rereads_every_row_once(owner, monkeypatch):
    ids = _sessions(4)
    assert _scan(monkeypatch)[0] == 4
    assert _scan(monkeypatch) == (0, [])
    cache = engine._scan_cache(policy.collection("sessions"), engine.context(), True)
    cache.verified_at -= engine.SCAN_FINGERPRINT_VERIFY_SECONDS + 1
    # A named-records pass never spends the verification.
    assert _scan(monkeypatch, record_ids=["s000"]) == (0, [])
    assert _scan(monkeypatch) == (0, ids)
    assert _scan(monkeypatch) == (0, [])


@pytest.mark.parametrize("working", [1, 0])
def test_unsupported_fingerprint_falls_back_without_losing_changes(owner, monkeypatch, working):
    # First choice: a function this server does not have (what an older
    # PostgreSQL, or a json column, answers). With no working choice at all
    # the scan is the full read it always was.
    choices = ("no_such_fingerprint_function(scan_row__)",
               engine._SCAN_FINGERPRINT_SQL[1] if working else "no_such_function_either(scan_row__)")
    monkeypatch.setattr(engine, "_SCAN_FINGERPRINT_SQL", choices)
    engine.reset_scan_cache()
    ids = _sessions(3)
    assert _scan(monkeypatch) == (3, ids)
    assert engine._scan_fingerprint_choice["sessions"] == (1 if working else 2)
    _execute("UPDATE sessions SET title = 'edited' WHERE id = 's001'")
    assert _scan(monkeypatch) == (1, ["s001"] if working else ids)
    assert _scan(monkeypatch) == (0, [] if working else ids)
    engine.reset_scan_cache()


def test_shared_collection_scan_skips_unchanged_and_unshared_rows(owner, monkeypatch):
    """Not only the session catalog: any collection whose record is a pure
    function of its row, including rows the collection does not share."""
    coll = policy.collection("working_record")
    assert engine._fingerprint_scannable(coll) and coll.shared_row is not None
    for excluded in ("system_settings", "model_providers", "session_active", "presence"):
        assert not engine._fingerprint_scannable(policy.collection(excluded))

    def scan():
        with returned_rows(monkeypatch, engine) as reads:
            published = engine.scan_collection(coll.name)
        return published, sorted(row["id"] for row in _payloads(reads, coll.table))

    def item(item_id, owner_key, content="note"):
        _execute("INSERT INTO owner_working_items (id, owner_key, kind, content, report_frame, created_at, updated_at) "
                 "VALUES (%s, %s, 'note', %s, %s, now(), now())", (item_id, owner_key, content, '{"a": [1, 2]}'))

    item("mine", policy.OWNER_PLACEHOLDER)
    item("private", "api-user-7")
    assert scan() == (1, ["mine", "private"])
    assert scan() == (0, [])  # neither the published row nor the unshared one is read again
    _execute("UPDATE owner_working_items SET content = 'edited' WHERE id IN ('mine', 'private')")
    assert scan() == (1, ["mine", "private"])
    # An unshared row that becomes shared is published. One that stops being
    # shared keeps its synced copy (``record`` never retracts a now-local
    # row), exactly as the full read leaves it.
    _execute("UPDATE owner_working_items SET owner_key = %s WHERE id = 'private'", (policy.OWNER_PLACEHOLDER,))
    _execute("UPDATE owner_working_items SET owner_key = 'api-user-7' WHERE id = 'mine'")
    assert scan() == (1, ["mine", "private"])
    assert scan() == (0, [])
    with store.get_db() as conn:
        cursor = conn.cursor()
        assert not store.get_record(cursor, coll.name, "mine")["deleted"]
        assert not store.get_record(cursor, coll.name, "private")["deleted"]
    with monkeypatch.context() as patch:
        patch.setenv(engine.SCAN_FINGERPRINT_ENV, "off")
        assert scan() == (0, ["mine", "private"])


def test_sqlite_shim_keeps_the_full_read(tmp_path, monkeypatch):
    """A connection that is not PostgreSQL is never asked for a fingerprint."""
    from app.network import transport
    from tests.core.network.harness import Worker

    worker = Worker("shim-scan", tmp_path)
    try:
        with worker.active():
            engine.create_network("Shim")
            ids = _sessions(2)
            with returned_rows(monkeypatch, engine) as reads:
                assert engine.scan_collection("sessions", owner_only=True) == 2
                assert engine.scan_collection("sessions", owner_only=True) == 0
            assert not any(FINGERPRINT in query for query, _rows in reads)
            assert sorted(row["id"] for row in _payloads(reads)) == sorted(ids * 2)
    finally:
        worker.close()
        engine.reset_for_tests()
        transport.reset_for_tests()
