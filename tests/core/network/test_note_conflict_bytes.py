"""An attachment displaced by a remote parent delete has a durable recovery owner."""
import hashlib

import pytest

from app.db import database, network as store, notes
from app.network import blobs, engine
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: F401


def _read_after_restart(descriptor, schema, digest):
    from contextlib import closing
    from tests.db.schema_lifecycle_helpers import pg_connect

    with closing(pg_connect(descriptor)) as conn:
        conn.cursor().execute("SELECT set_config('search_path', %s, false)", (schema,))
        return blobs.read_local(digest, _conn=conn)


@pytest.mark.parametrize("snapshot", [False, True])
def test_parent_delete_keeps_bytes_until_both_conflicts_dismissed(pg_workers, private_cluster, snapshot):
    a, b = pg_workers("a"), pg_workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        notes.create_note("n", "Shared", "body")
        engine.scan_all()
    converge([a, b])
    data = b"one recovery payload for two attachments"
    digest = hashlib.sha256(data).hexdigest()
    with b.active():
        for aid in ("first", "second"):
            notes.create_note_attachment(aid, "n", aid, aid, "text/plain", len(data), data)
        engine.scan_all()
    with a.active():
        notes.delete_note("n")
        engine.scan_all()
        chunk = engine.snapshot_chunk("notes", 0)
    with b.active():
        if snapshot:
            engine.apply_snapshot_chunk(chunk)
        else:
            assert exchange(a, b).retry == 0
        assert notes.get_note("n") is None
        assert blobs.read_local(digest) == data
        with database.get_db() as conn:
            conn.cursor().execute("UPDATE sync_conflicts SET expires_at='2000-01-01'")
        # A new Python process and a new connection to B: no cached payload,
        # disk upload or old engine context can satisfy this read.
        from concurrent.futures import ProcessPoolExecutor
        from multiprocessing import get_context
        schema = b.sql("SELECT current_schema() AS s")[0]["s"]
        with ProcessPoolExecutor(1, mp_context=get_context("spawn")) as process:
            assert process.submit(_read_after_restart, private_cluster, schema, digest).result(timeout=30) == data
        store.expire_conflicts()
        pins = [c for c in store.list_conflicts() if c["collection"] == "note_attachments"]
        assert {c["record_id"] for c in pins} == {"first", "second"}
        assert store.delete_conflict(pins[0]["id"])
        assert blobs.read_local_range(digest, 1, 4) == (data[1:5], len(data))
        assert store.delete_conflict(pins[1]["id"])
        assert blobs.read_local(digest) is None
