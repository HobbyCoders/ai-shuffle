"""Owner scans return only publishable payloads, without losing tombstones."""
from contextlib import contextmanager

import pytest

from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import catalog, engine, transport
from tests.core.network.harness import Worker


@pytest.fixture
def worker(tmp_path):
    instance = Worker("owner-reads", tmp_path)
    yield instance
    instance.close()
    engine.reset_for_tests()
    transport.reset_for_tests()


def seed(worker):
    engine.create_network("Owner reads")
    layouts = {
        "own": (worker.device_id, "another-instance"),
        "legacy": (None, settings.effective_instance_id),
        "unassigned": (None, None),
        "empty": (None, ""),
        "foreign": ("peer-device", settings.effective_instance_id),
        "other-install": (None, "another-instance"),
    }
    for session_id, (owner, instance) in layouts.items():
        database.create_session(session_id=session_id, profile_id="default", project_id=None, title=session_id)
        with store.get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sessions SET owner_device_id = %s, instance_id = %s WHERE id = %s",
                        (owner, instance, session_id))
    return layouts


@contextmanager
def returned_rows(monkeypatch, module):
    """Observe rows actually crossing the DB boundary, not just serialized rows."""
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

        def cursor(self):
            return Cursor(self.connection.cursor())

    @contextmanager
    def observed():
        with original() as connection:
            yield Connection(connection)

    with monkeypatch.context() as patch:
        patch.setattr(module, "get_db", observed)
        yield reads


def test_session_payload_reads_preserve_legacy_ownership_and_deletion(worker, monkeypatch):
    with worker.active():
        seed(worker)
        with returned_rows(monkeypatch, engine) as reads:
            assert engine.scan_collection("sessions", owner_only=True) == 4
        payloads = [row for query, rows in reads if "FROM sessions" in query for row in rows]
        assert {row["id"] for row in payloads} == {"own", "legacy", "unassigned", "empty"}
        # External changes still reconcile; no dirty marker is necessary.
        with store.get_db() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sessions SET title = 'external edit' WHERE id = 'legacy'")
            cur.execute("DELETE FROM sessions WHERE id = 'own'")
            store.put_record(cur, "sessions", "foreign-deleted", clock=1,
                             origin_device_id="peer-device", content_hash="foreign", deleted=False)
        assert engine.scan_collection("sessions", owner_only=True) == 2
        assert engine.scan_collection("sessions", owner_only=True) == 0
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.get_record(cur, "sessions", "own")["deleted"]
            assert not store.get_record(cur, "sessions", "foreign-deleted")["deleted"]


@pytest.mark.parametrize("collection", ["session_tags", "pending_interactions"])
def test_child_payload_reads_and_tombstones_keep_strict_session_ownership(worker, monkeypatch, collection):
    with worker.active():
        layouts = seed(worker)
        with store.get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO tags(id, name) VALUES ('tag', 'Tag')")
            for session_id in layouts:
                if collection == "session_tags":
                    cur.execute("INSERT INTO session_tags(session_id, tag_id) VALUES (%s, 'tag')", (session_id,))
                else:
                    cur.execute("INSERT INTO pending_interactions(id, session_id, kind) VALUES (%s, %s, 'question')",
                                ("prompt-" + session_id, session_id))
        with returned_rows(monkeypatch, catalog) as reads:
            assert catalog._publish_child_rows(collection, engine.context()) == 2
        payloads = [row for query, rows in reads if f"FROM {collection} " in query for row in rows]
        assert {row["session_id"] for row in payloads} == {"own", "legacy"}
        with store.get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {collection} WHERE session_id = 'own'")
            store.put_record(cur, collection, "foreign-deleted", clock=1,
                             origin_device_id="peer-device", content_hash="foreign", deleted=False)
        assert catalog._publish_child_rows(collection, engine.context()) == 1
        assert catalog._publish_child_rows(collection, engine.context()) == 0
        with store.get_db() as conn:
            cur = conn.cursor()
            record_id = "own|tag" if collection == "session_tags" else "prompt-own"
            assert store.get_record(cur, collection, record_id)["deleted"]
            assert not store.get_record(cur, collection, "foreign-deleted")["deleted"]


def test_runtime_owner_read_needs_no_instance_column(worker, monkeypatch):
    with worker.active():
        seed(worker)
        with store.get_db() as conn:
            cur = conn.cursor()
            for session_id, owner in [("own", worker.device_id), ("foreign", "peer-device")]:
                cur.execute("INSERT INTO network_session_runtime(session_id, owner_device_id, state) VALUES (%s, %s, 'idle')",
                            (session_id, owner))
        with returned_rows(monkeypatch, engine) as reads:
            assert engine.scan_collection("session_runtime", owner_only=True) == 1
        payloads = [row for query, rows in reads if "FROM network_session_runtime" in query for row in rows]
        assert [row["session_id"] for row in payloads] == ["own"]
