"""#1605: a local change the scanner has not published yet must survive a peer's op.

Before the fix, applying a peer's op over a row this worker had changed (but
not yet scanned into its outbox) overwrote the row, recorded no conflict and
never republished the change: it was lost on every worker.

Now the peer version applies and the local change is kept as this worker's
conflict, written in the same savepoint as the replacement. An apply never
signs the change itself: the batch may still prove that this worker was
restored from a backup and that its next sequence numbers are taken (#1570).
"""
import pytest

from app.core import encryption
from app.db import database
from app.db import network as store
from app.network import engine
from tests.core.network.harness import converge, exchange, join


def _pair(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        database.create_tag("t1", "original")
        engine.scan_all()
    converge([a, b])
    return a, b


def _advance_clock(worker, clock=10000):
    with worker.active():
        with database.get_db() as conn:
            store.observe_clock(conn.cursor(), clock)


def _tag_name(worker, tag_id="t1"):
    with worker.active():
        row = database.get_tag(tag_id)
        return row["name"] if row else None


def _conflicts(worker, record_id="t1", collection="tags"):
    with worker.active():
        return [c for c in store.list_conflicts() if c["collection"] == collection and c["record_id"] == record_id]


def _losers(worker, record_id="t1"):
    return sorted((str(c["losing_record"].get("name", "<deleted>")), c["losing_device_id"])
                  for c in _conflicts(worker, record_id))


def _scan_is_quiet(*workers):
    for worker in workers:
        with worker.active():
            assert engine.scan_all() == 0


def _own_ops(worker):
    with worker.active():
        return [store._load(row["envelope"], {}) for row in worker.sql(
            "SELECT envelope FROM sync_ops WHERE origin_device_id = ? ORDER BY seq", (worker.device_id,))]


@pytest.mark.parametrize("local_clock_ahead", [False, True])
def test_unscanned_edit_of_an_existing_row_is_kept_as_a_conflict(workers, local_clock_ahead):
    """The issue's reproduction. The edit has no clock yet, whatever this
    worker's clock is: the peer version applies and the edit stays visible as
    B's conflict instead of vanishing. Nothing is signed by the apply."""
    a, b = _pair(workers)
    _advance_clock(b if local_clock_ahead else a)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    signed = len(_own_ops(b))
    with b.active():
        database.update_tag("t1", name="from-b")  # the scanner has not run yet
    exchange(a, b)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("from-b", b.device_id)]
    assert len(_own_ops(b)) == signed
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-a"
    assert not _conflicts(a)  # never signed, so never sent
    _scan_is_quiet(a, b)


def test_unscanned_edit_twice_then_two_peer_ops(workers):
    """Only the last local value is kept, once; both peer ops apply in order."""
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="a-1")
        engine.scan_all()
        database.update_tag("t1", name="a-2")
        engine.scan_all()
    with b.active():
        database.update_tag("t1", name="b-1")
        database.update_tag("t1", name="b-2")
    exchange(a, b)
    assert _tag_name(b) == "a-2"
    assert _losers(b) == [("b-2", b.device_id)]
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "a-2"
    _scan_is_quiet(a, b)


def test_the_same_record_in_two_consecutive_batches(workers):
    a, b = _pair(workers)
    with b.active():
        database.update_tag("t1", name="from-b")
    for name in ("a-1", "a-2"):
        with a.active():
            database.update_tag("t1", name=name)
            engine.scan_all()
        exchange(a, b)
    assert _tag_name(b) == "a-2"
    assert _losers(b) == [("from-b", b.device_id)]


def test_unscanned_local_delete_is_kept_as_a_conflict(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    with b.active():
        database.delete_tag("t1")
    exchange(a, b)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("<deleted>", b.device_id)]
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-a"
    _scan_is_quiet(a, b)


def test_published_local_delete_that_loses_is_a_conflict_on_both_workers(workers):
    a, b = _pair(workers)
    with b.active():
        database.delete_tag("t1")
        engine.scan_all()
    _advance_clock(a)
    with a.active():
        database.update_tag("t1", name="from-a")  # before B's deletion arrives
        engine.scan_all()
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-a"
    assert _losers(a) == _losers(b) == [("<deleted>", b.device_id)]
    _scan_is_quiet(a, b)


def test_peer_delete_racing_an_unscanned_local_edit(workers):
    a, b = _pair(workers)
    with a.active():
        database.delete_tag("t1")
        engine.scan_all()
    with b.active():
        database.update_tag("t1", name="from-b")
    exchange(a, b)
    assert _tag_name(b) is None
    assert _losers(b) == [("from-b", b.device_id)]
    converge([a, b])
    assert _tag_name(a) is None and _tag_name(b) is None
    _scan_is_quiet(a, b)


def test_unscanned_local_delete_racing_a_peer_delete_records_nothing(workers):
    a, b = _pair(workers)
    with a.active():
        database.delete_tag("t1")
        engine.scan_all()
    with b.active():
        database.delete_tag("t1")
    converge([a, b])
    assert _tag_name(a) is None and _tag_name(b) is None
    assert not _conflicts(a) and not _conflicts(b)
    _scan_is_quiet(a, b)


def test_same_unscanned_edit_on_both_sides_records_no_conflict(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="same")
        engine.scan_all()
    with b.active():
        database.update_tag("t1", name="same")
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "same"
    assert not _conflicts(a) and not _conflicts(b)
    _scan_is_quiet(a, b)


def test_unscanned_new_row_with_the_same_id_as_a_peer_row(workers):
    """A row created here and never scanned, whose id a peer also wrote."""
    a, b = _pair(workers)
    with a.active():
        database.create_tag("t2", "from-a")
        engine.scan_all()
    with b.active():
        database.create_tag("t2", "from-b")
    exchange(a, b)
    assert _tag_name(b, "t2") == "from-a"
    assert _losers(b, "t2") == [("from-b", b.device_id)]
    converge([a, b])
    assert _tag_name(a, "t2") == _tag_name(b, "t2") == "from-a"
    _scan_is_quiet(a, b)


def test_unscanned_change_is_kept_while_the_outbox_is_held(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        database.create_tag("t2", "two")
        engine.scan_all()
    converge([a, b])
    with a.active():
        database.update_tag("t1", name="from-a-2")
        database.update_tag("t2", name="two-a")
        engine.scan_all()
    with b.active():
        database.update_tag("t1", name="from-b")
        database.delete_tag("t2")
        engine.hold_outbox(True)
    try:
        exchange(a, b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    assert _tag_name(b) == "from-a-2" and _tag_name(b, "t2") == "two-a"
    assert _losers(b) == [("from-b", b.device_id)]
    assert _losers(b, "t2") == [("<deleted>", b.device_id)]
    _scan_is_quiet(a, b)


def test_unscanned_edit_is_kept_by_a_state_copy(workers):
    """The snapshot (state copy) path applies the same rule."""
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
        chunk = engine.snapshot_chunk("tags", 0)
    signed = len(_own_ops(b))
    with b.active():
        database.update_tag("t1", name="from-b")
        engine.apply_snapshot_chunk(chunk)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("from-b", b.device_id)]
    assert len(_own_ops(b)) == signed
    _scan_is_quiet(b)


def test_published_row_still_takes_the_peer_op(workers):
    """No local change: the peer op applies exactly as before, no conflict."""
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    result = exchange(a, b)
    assert result.applied == 1
    assert _tag_name(b) == "from-a"
    assert not _conflicts(b)
    with b.active():
        assert engine.scan_all() == 0


def test_published_local_edit_that_loses_is_a_conflict_on_both_workers(workers):
    a, b = _pair(workers)
    with b.active():
        database.update_tag("t1", name="from-b")
        engine.scan_all()
    _advance_clock(a)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-a"
    assert _losers(a) == _losers(b) == [("from-b", b.device_id)]


# -- S3: the conflict and the replacement commit or roll back together.

@pytest.fixture
def real_savepoints(monkeypatch):
    """Run the PostgreSQL per-op savepoint path on the SQLite shim."""
    from tests import pg_shim
    monkeypatch.setattr(pg_shim.ShimCursor, "__module__", "psycopg2.shim")


def test_a_refused_op_does_not_use_up_the_local_change(workers, real_savepoints):
    """A's first op is refused by the database and rolls back to its
    savepoint; the next op of the same chunk still keeps B's deletion."""
    a, b = _pair(workers)
    with a.active():
        ctx = engine.context()
        with database.get_db() as conn:
            row = dict(database.get_tag("t1"))
            assert engine.record(conn.cursor(), "tags", "t1", {**row, "name": None}, ctx=ctx, force=True)
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    with b.active():
        database.delete_tag("t1")
        engine.hold_outbox(True)
    try:
        result = exchange(a, b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    assert result.rejected == 1
    assert _tag_name(b) == "from-a"
    assert ("<deleted>", b.device_id) in _losers(b)


# -- S4: an unreadable local row is not taken for an unchanged one.

def test_a_peer_op_waits_while_a_local_change_cannot_be_read(workers, monkeypatch):
    from app.db import database as db
    provider = "sync-unscanned-provider"
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    a, b = _pair(workers)
    with a.active():
        db.create_model_provider(provider, "API", "xai", "https://api.x.ai/v1",
                                 auth_token=encryption.encrypt_value("first"))
        engine.scan_all()
    converge([a, b])
    with b.active():
        db.update_model_provider(provider, auth_token=encryption.encrypt_value("rotated"))  # not scanned
    with a.active():
        db.delete_model_provider(provider)
        engine.scan_all()
    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        exchange(a, b)
        with b.active():
            assert db.get_model_provider(provider) is not None
        assert not _conflicts(b, provider, "model_providers")
    # Credential storage is open again: the op drains, the rotation is kept.
    exchange(a, b)
    with b.active():
        engine.resume_pending()
        assert db.get_model_provider(provider) is None
    losers = _conflicts(b, provider, "model_providers")
    assert [c["losing_device_id"] for c in losers] == [b.device_id]
    assert "rotated" not in str(losers)  # the secret itself is redacted, as in every conflict
    converge([a, b])
    with a.active():
        assert db.get_model_provider(provider) is None


# -- A restored worker (#1570): no local op may take a sequence number its own
# -- retained ops already hold, in this batch or a later one.

def _restored_with_proof(workers):
    """B is restored from a backup; A holds ops B signed after it."""
    from tests.core.network.test_restored_worker_ordering import _restore
    a, b = _pair(workers)
    backup = b.raw.serialize()
    with b.active():
        for n in range(3):
            database.update_tag("t1", name=f"lost-{n}")
            engine.scan_all()
    converge([a, b])
    signed = _own_ops(b)
    _restore(b, backup)
    with b.active(), database.get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, 1)
    proof = [op for op in signed if op["origin_seq"] > held]
    assert proof
    return a, b, proof, held


def _old_stream_unchanged(b, held):
    """Nothing was signed into the restored incarnation past what it held."""
    with b.active():
        assert engine.context().identity.incarnation == 2
    assert not [op for op in _own_ops(b) if op["origin_incarnation"] == 1 and op["origin_seq"] > held]


def _peer_edit(a, name):
    with a.active():
        database.update_tag("t1", name=name)
        engine.scan_all()
    return [_own_ops(a)[-1]]


def test_restored_worker_unscanned_edit_waits_for_the_new_stream(workers):
    a, b, proof, held = _restored_with_proof(workers)
    with b.active():
        database.update_tag("t1", name="after-restore")
        result = engine.apply_batch(proof)
        assert result.own_ahead == max(op["origin_seq"] for op in proof)
    _old_stream_unchanged(b, held)
    with b.active():
        assert engine.scan_all() == 1  # signed in the new stream
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "after-restore"


def test_restored_worker_proof_through_the_one_by_one_path(workers, monkeypatch):
    monkeypatch.setattr(engine, "_apply_chunk", lambda chunk, ctx, result: False)
    a, b, proof, held = _restored_with_proof(workers)
    with b.active():
        database.update_tag("t1", name="after-restore")
        engine.apply_batch(proof)
    _old_stream_unchanged(b, held)


def test_restored_worker_proof_after_peer_ops_in_a_later_chunk(workers, monkeypatch):
    monkeypatch.setattr(engine, "APPLY_CHUNK", 2)
    a, b, proof, held = _restored_with_proof(workers)
    peer = _peer_edit(a, "from-a")
    with b.active():
        database.update_tag("t1", name="after-restore")
        engine.apply_batch(peer + [peer[0]] + proof)
    _old_stream_unchanged(b, held)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("after-restore", b.device_id)]


def test_restored_worker_proof_in_a_later_batch(workers):
    """The peer's op arrives in one batch with the outbox open, B's own
    retained op in the next: the first apply must not have signed into it."""
    a, b, proof, held = _restored_with_proof(workers)
    peer = _peer_edit(a, "from-a")
    with b.active():
        assert not engine.outbox_held()
        database.update_tag("t1", name="after-restore")
        engine.apply_batch(peer)
        result = engine.apply_batch(proof)
        assert result.own_ahead == max(op["origin_seq"] for op in proof)
    _old_stream_unchanged(b, held)
    assert _losers(b) == [("after-restore", b.device_id)]


# -- S6: an upsert over a row this worker cannot read keeps its readable fields.

def test_a_peer_upsert_over_an_unreadable_local_change_keeps_its_readable_fields(workers, monkeypatch):
    """B gives a shared provider a token and a new name without a scan; A's
    rename carries no token, so it needs no credential storage to apply.
    While B cannot read its own row, the rename still applies (a partial
    writer's clear must not wait on secrets), and B's replaced name is kept
    as B's conflict, with the secret columns as the placeholder."""
    from app.db import database as db
    provider = "sync-unscanned-upsert"
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    a, b = _pair(workers)
    with a.active():
        db.create_model_provider(provider, "API", "xai", "https://api.x.ai/v1")
        engine.scan_all()
    converge([a, b])
    with b.active():
        db.update_model_provider(provider, name="from-b", auth_token=encryption.encrypt_value("b-token"))
    with a.active():
        db.update_model_provider(provider, name="from-a")
        engine.scan_all()
    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        exchange(a, b)
        with b.active():
            assert db.get_model_provider(provider)["name"] == "from-a"
    losers = _conflicts(b, provider, "model_providers")
    assert [(c["losing_record"]["name"], c["losing_device_id"]) for c in losers] == [("from-b", b.device_id)]
    assert losers[0]["losing_record"]["auth_token"] == engine.SECRET_PLACEHOLDER
    assert "b-token" not in str(losers)


# -- S8: a parent's deletion cascades into shared children the scanner has
# -- not published; each is kept as this worker's conflict.

def _note_with_attachment(worker, child_id):
    from app.db import notes
    with worker.active():
        notes.create_note_attachment(child_id, "n1", "b.txt", "notes/b.txt", "text/plain", 3, b"abc")


def _cascade_case(kind):
    from app.db import api_users, notes, profiles, subagents

    def parent(worker):
        with worker.active():
            if kind == "notes":
                notes.create_note("n1", "Shared note", "body")
            elif kind == "agents":
                database.create_profile(profile_id="p1", name="Agent", description=None, config={})
            else:
                api_users.create_api_user("u1", "Key", "hash")

    def child(worker):
        with worker.active():
            if kind == "notes":
                notes.create_note_attachment("att-b", "n1", "b.txt", "notes/b.txt", "text/plain", 3, b"abc")
                return "note_attachments"
            if kind == "agents":
                return "permission_rules", subagents.add_permission_rule("p1", "Bash", "ls *", "allow")["id"]
            api_users.set_user_credential_policy("u1", "openai", "user_provided")
            return "user_credential_policies"

    def drop(worker):
        with worker.active():
            if kind == "notes":
                notes.delete_note("n1")
            elif kind == "agents":
                profiles.delete_profile("p1")
            else:
                api_users.delete_api_user("u1")
    return parent, child, drop


@pytest.mark.parametrize("kind", ["notes", "agents", "api_users"])
def test_a_parent_deletion_keeps_an_unscanned_child(workers, kind):
    parent, child, drop = _cascade_case(kind)
    a, b = _pair(workers)
    parent(a)
    with a.active():
        engine.scan_all()
    converge([a, b])
    added = child(b)  # the scanner has not run on B
    collection = added[0] if isinstance(added, tuple) else added
    drop(a)
    with a.active():
        engine.scan_all()
    signed = len(_own_ops(b))
    exchange(a, b)
    with b.active():
        kept = [c for c in store.list_conflicts() if c["collection"] == collection]
    assert [c["losing_device_id"] for c in kept] == [b.device_id]
    assert kept[0]["winning_device_id"] == a.device_id
    assert len(_own_ops(b)) == signed


def test_a_state_copy_deletion_keeps_an_unscanned_child(workers):
    from app.db import notes
    a, b = _pair(workers)
    with a.active():
        notes.create_note("n1", "Shared note", "body")
        engine.scan_all()
    converge([a, b])
    _note_with_attachment(b, "att-b")
    with a.active():
        notes.delete_note("n1")
        engine.scan_all()
        chunk = engine.snapshot_chunk("notes", 0)
    with b.active():
        engine.apply_snapshot_chunk(chunk)
        assert notes.get_note("n1") is None
    kept = _conflicts(b, "att-b", "note_attachments")
    assert [c["losing_device_id"] for c in kept] == [b.device_id]


def test_cascade_children_match_the_schema(workers):
    """Every shared table a shared parent's deletion cascades into is listed."""
    from app.network import policy
    worker = workers("schema")
    tables = {coll.table: name for name, coll in policy.COLLECTIONS.items()}
    found = {}
    for table in sorted(r["name"] for r in worker.sql("SELECT name FROM sqlite_master WHERE type='table'")):
        for fk in worker.sql(f"PRAGMA foreign_key_list('{table}')"):
            if (fk["on_delete"] == "CASCADE" and table in tables and fk["table"] in tables
                    and tables[table] in policy.SCANNED_COLLECTIONS):
                found.setdefault(tables[fk["table"]], set()).add((tables[table], fk["from"]))
    assert found == {parent: set(children) for parent, children in engine._CASCADE_CHILDREN.items()}


# -- S7: an app writer cannot change the row between capture and replacement.

def test_add_conflict_replay_is_idempotent(workers):
    worker = workers("solo")
    entry = dict(collection="tags", record_id="t1", losing_clock=3, losing_device_id="dev_b",
                 losing_record={"name": "x"}, winning_clock=4, winning_device_id="dev_a")
    with worker.active(), database.get_db() as conn:
        store.add_conflict(conn.cursor(), **entry)
        store.add_conflict(conn.cursor(), **entry)
    with worker.active():
        assert len(store.list_conflicts()) == 1


# Real PostgreSQL: B's apply runs in one transaction while an app writer on a
# second connection commits to the same row. The SQLite shim has a single
# connection per worker, so it cannot interleave two transactions.
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: E402,F401


def _concurrent_writer(monkeypatch, private_cluster, worker, record_id, statement, params):
    """Run ``statement`` on a second connection to ``worker``'s database just
    before its apply writes ``record_id``; give it a second to commit."""
    import threading
    from tests.core.network.harness import _ACTIVE
    from tests.db.schema_lifecycle_helpers import pg_connect
    schema = worker.sql("SELECT current_schema() AS s")[0]["s"]
    original = engine.materialize
    state = {"thread": None, "error": None}

    def write():
        conn = pg_connect(private_cluster)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SET search_path TO " + schema)
                cursor.execute("SET lock_timeout = '30s'")
                cursor.execute(statement, params)
        except Exception as exc:  # noqa: BLE001
            state["error"] = exc
        finally:
            conn.close()

    def materialize(cursor, coll, rid, *args, **kwargs):
        if coll.name == "tags" and rid == record_id and state["thread"] is None and _ACTIVE.get() is worker:
            state["thread"] = threading.Thread(target=write)
            state["thread"].start()
            state["thread"].join(1.0)  # blocks here only if the row is locked
        return original(cursor, coll, rid, *args, **kwargs)

    monkeypatch.setattr(engine, "materialize", materialize)
    return state


def _finish(state):
    assert state["thread"] is not None
    state["thread"].join(30)
    assert not state["thread"].is_alive() and state["error"] is None


def test_postgres_app_write_between_capture_and_replacement(pg_workers, private_cluster, monkeypatch):
    """The app renames the tag after B read it and before the peer version is
    written. The row lock makes the rename wait for the apply's commit, so
    it lands on top and the scanner publishes it."""
    a, b = _pair(pg_workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    state = _concurrent_writer(monkeypatch, private_cluster, b, "t1",
                               "UPDATE tags SET name = %s WHERE id = 't1'", ("concurrent",))
    exchange(a, b)
    _finish(state)
    assert _tag_name(b) == "concurrent" or ("concurrent", b.device_id) in _losers(b)
    with b.active():
        assert engine.scan_all() == 1
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "concurrent"


def test_postgres_app_insert_while_a_peer_row_arrives(pg_workers, private_cluster, monkeypatch):
    """The app creates a row with the id of a peer row B has not got yet,
    after B found it absent. The apply does not overwrite it: the op waits,
    and on retry B's row is kept as its conflict (or wins)."""
    a, b = _pair(pg_workers)
    with a.active():
        database.create_tag("t2", "from-a")
        engine.scan_all()
    state = _concurrent_writer(
        monkeypatch, private_cluster, b, "t2",
        "INSERT INTO tags (id, name, color, created_at, updated_at) VALUES ('t2', %s, '#000000', NOW(), NOW())",
        ("local",))
    exchange(a, b)
    _finish(state)
    assert _tag_name(b, "t2") == "local"
    exchange(a, b)
    with b.active():
        engine.resume_pending()
    assert _tag_name(b, "t2") == "local" or ("local", b.device_id) in _losers(b, "t2")
    converge([a, b])
    assert _tag_name(a, "t2") == _tag_name(b, "t2")
