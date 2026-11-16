"""#1605: a local change the scanner has not published yet must survive a peer's op.

Before the fix, applying a peer's op over a row this worker had changed (but
not yet scanned into its outbox) overwrote the row, recorded no conflict and
never republished the change: it was lost on every worker.

Now the apply signs such a change first, through ``record()`` as the scanner
would, and ordinary last-writer-wins decides between the two. A change that
cannot be signed yet (outbox held, restore proof in the batch, credential
storage locked, an app write racing the apply) makes the peer op wait.
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


def _own_ops(worker, collection=None):
    with worker.active():
        ops = [store._load(row["envelope"], {}) for row in worker.sql(
            "SELECT envelope FROM sync_ops WHERE origin_device_id = ? ORDER BY seq", (worker.device_id,))]
    return [op for op in ops if collection is None or op["collection"] == collection]


def _settle(a, b):
    """Converge, letting each scanner publish what the exchange left (a
    retraction of a child its parent's deletion removed, presence)."""
    for _ in range(3):
        converge([a, b])
        for worker in (a, b):
            with worker.active():
                engine.scan_all()
    converge([a, b])
    _scan_is_quiet(a, b)


@pytest.mark.parametrize("local_clock_ahead", [False, True])
def test_unscanned_edit_is_signed_then_ordered(workers, local_clock_ahead):
    """The issue's reproduction. B's edit is signed before A's op applies, at
    B's clock then, so the higher clock wins on both workers and the loser is
    a conflict on both."""
    a, b = _pair(workers)
    _advance_clock(b if local_clock_ahead else a)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    signed = len(_own_ops(b))
    with b.active():
        database.update_tag("t1", name="from-b")  # the scanner has not run yet
    exchange(a, b)
    assert len(_own_ops(b)) == signed + 1
    _settle(a, b)
    winner, loser = (("from-b", ("from-a", a.device_id)) if local_clock_ahead
                     else ("from-a", ("from-b", b.device_id)))
    assert _tag_name(a) == _tag_name(b) == winner
    assert _losers(a) == _losers(b) == [loser]


def test_unscanned_edit_twice_then_two_peer_ops(workers):
    a, b = _pair(workers)
    with b.active():
        database.update_tag("t1", name="b-1")
        database.update_tag("t1", name="b-2")
    with a.active():
        for name in ("a-1", "a-2"):
            database.update_tag("t1", name=name)
            engine.scan_all()
    signed = len(_own_ops(b))
    exchange(a, b)
    assert [op["record_id"] for op in _own_ops(b)[signed:]] == ["t1"]  # once, the latest value
    _settle(a, b)
    assert _tag_name(a) == _tag_name(b) == "a-2"
    assert ("b-2", b.device_id) in _losers(b)


def test_the_same_record_in_two_consecutive_batches(workers):
    a, b = _pair(workers)
    for n in range(2):
        with b.active():
            database.update_tag("t1", name=f"b-{n}")
        with a.active():
            database.update_tag("t1", name=f"a-{n}")
            engine.scan_all()
        exchange(a, b)
    _settle(a, b)
    assert _tag_name(a) == _tag_name(b) is not None
    assert set(_losers(a)) == set(_losers(b))


def test_unscanned_local_delete_that_loses_is_a_conflict_on_both_workers(workers):
    a, b = _pair(workers)
    _advance_clock(a)
    with b.active():
        database.delete_tag("t1")
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    exchange(a, b)
    _settle(a, b)
    assert _tag_name(a) == _tag_name(b) == "from-a"
    assert _losers(a) == _losers(b) == [("<deleted>", b.device_id)]


@pytest.mark.parametrize("peer_clock_ahead", [True, False])
def test_peer_delete_racing_an_unscanned_local_edit(workers, peer_clock_ahead):
    a, b = _pair(workers)
    _advance_clock(a if peer_clock_ahead else b)
    with b.active():
        database.update_tag("t1", name="from-b")
    with a.active():
        database.delete_tag("t1")
        engine.scan_all()
    exchange(a, b)
    _settle(a, b)
    if peer_clock_ahead:
        assert _tag_name(a) is _tag_name(b) is None
        assert _losers(a) == _losers(b) == [("from-b", b.device_id)]
    else:
        assert _tag_name(a) == _tag_name(b) == "from-b"
        assert _losers(a) == _losers(b) == [("<deleted>", a.device_id)]


def test_unscanned_local_delete_racing_a_peer_delete_records_nothing(workers):
    a, b = _pair(workers)
    with b.active():
        database.delete_tag("t1")
    with a.active():
        database.delete_tag("t1")
        engine.scan_all()
    exchange(a, b)
    _settle(a, b)
    assert _tag_name(a) is _tag_name(b) is None
    assert not _conflicts(a) and not _conflicts(b)


def test_same_unscanned_edit_on_both_sides_records_no_conflict(workers):
    a, b = _pair(workers)
    for worker in (a, b):
        with worker.active():
            database.update_tag("t1", name="same")
    with a.active():
        engine.scan_all()
    exchange(a, b)
    _settle(a, b)
    assert _tag_name(a) == _tag_name(b) == "same"
    assert not _conflicts(a) and not _conflicts(b)


def test_unscanned_new_row_with_the_same_id_as_a_peer_row(workers):
    a, b = _pair(workers)
    with b.active():
        database.create_tag("t2", "from-b")
    with a.active():
        database.create_tag("t2", "from-a")
        engine.scan_all()
    exchange(a, b)
    _settle(a, b)
    assert _tag_name(a, "t2") == _tag_name(b, "t2") is not None
    assert len(_conflicts(a, "t2")) == len(_conflicts(b, "t2")) == 1


def test_published_row_follows_normal_ordering(workers):
    """A row the scanner already published: the apply signs nothing, and the
    outcome is base's."""
    a, b = _pair(workers)
    _advance_clock(a)
    with b.active():
        database.update_tag("t1", name="from-b")
        engine.scan_all()
    signed = len(_own_ops(b))
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    exchange(a, b)
    assert len(_own_ops(b)) == signed
    _settle(a, b)
    assert _tag_name(a) == _tag_name(b) == "from-a"
    assert _losers(b) == [("from-b", b.device_id)]


def test_peer_op_waits_while_the_outbox_is_held(workers):
    a, b = _pair(workers)
    with b.active():
        database.update_tag("t1", name="from-b")
        engine.hold_outbox(True)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    try:
        exchange(a, b)
        assert _tag_name(b) == "from-b"
        assert not _conflicts(b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    exchange(a, b)
    with b.active():
        engine.resume_pending()
    _settle(a, b)
    assert _tag_name(a) == _tag_name(b)
    assert len(set(_losers(a))) == 1 and set(_losers(a)) == set(_losers(b))


def test_a_state_copy_signs_an_unscanned_edit_first(workers):
    a, b = _pair(workers)
    with b.active():
        database.update_tag("t1", name="from-b")
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
        chunk = engine.snapshot_chunk("tags", 0)
    signed = len(_own_ops(b))
    with b.active():
        engine.apply_snapshot_chunk(chunk)
    assert len(_own_ops(b)) == signed + 1
    _settle(a, b)
    assert _tag_name(a) == _tag_name(b)
    # The losing state-copy item and the same op later each record it (base).
    assert len(set(_losers(a))) == 1 and set(_losers(a)) == set(_losers(b))


@pytest.fixture
def real_savepoints(monkeypatch):
    """Run the PostgreSQL per-op savepoint path on the SQLite shim."""
    from tests import pg_shim
    monkeypatch.setattr(pg_shim.ShimCursor, "__module__", "psycopg2.shim")


def test_a_refused_op_does_not_undo_the_signed_local_change(workers, real_savepoints):
    """A's first op is refused by the database and rolls back to its
    savepoint. B's deletion was signed before any savepoint, so the next op
    of the same chunk is ordered against it."""
    a, b = _pair(workers)
    _advance_clock(a)
    with a.active():
        ctx = engine.context()
        with database.get_db() as conn:
            row = dict(database.get_tag("t1"))
            assert engine.record(conn.cursor(), "tags", "t1", {**row, "name": None}, ctx=ctx, force=True)
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    with b.active():
        database.delete_tag("t1")
    signed = len(_own_ops(b))
    result = exchange(a, b)
    assert result.rejected == 1
    assert [op["op"] for op in _own_ops(b)[signed:]] == ["delete"]
    assert _tag_name(b) == "from-a"
    assert ("<deleted>", b.device_id) in _losers(b)


# -- Credential storage locked: the local row cannot be signed, so the op waits.

def _provider_pair(workers, monkeypatch, provider, **fields):
    from app.db import database as db
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    a, b = _pair(workers)
    with a.active():
        db.create_model_provider(provider, "API", "xai", "https://api.x.ai/v1", **fields)
        engine.scan_all()
    converge([a, b])
    return a, b


def _while_locked(monkeypatch, a, b, provider, expected_name):
    from app.db import database as db
    signed = len(_own_ops(b))
    with monkeypatch.context() as locked:
        locked.setattr(encryption, "is_encryption_ready", lambda: False)
        exchange(a, b)
        with b.active():
            row = db.get_model_provider(provider)
    assert (row or {}).get("name") == expected_name
    assert not _conflicts(b, provider, "model_providers")
    assert len(_own_ops(b)) == signed
    exchange(a, b)  # unlocked: signed, then ordered
    with b.active():
        engine.resume_pending()
    _settle(a, b)


@pytest.mark.parametrize("peer_op", ["update", "delete"])
def test_a_peer_op_waits_while_a_local_change_cannot_be_read(workers, monkeypatch, peer_op):
    """B renames a provider and gives it a token without a scan; credential
    storage then locks. A's rename (which needs no encryption) or deletion
    waits instead of replacing B's row. After unlock B's change is signed
    and ordered like a scanned edit: A's clock is ahead, so A's op wins and
    B's whole change is B's conflict on both workers (its token redacted)."""
    from app.db import database as db
    provider = "sync-unscanned-provider"
    a, b = _provider_pair(workers, monkeypatch, provider)
    _advance_clock(a)
    with b.active():
        db.update_model_provider(provider, name="from-b", auth_token=encryption.encrypt_value("b-token"))
    with a.active():
        if peer_op == "update":
            db.update_model_provider(provider, name="from-a")
        else:
            db.delete_model_provider(provider)
        engine.scan_all()
    _while_locked(monkeypatch, a, b, provider, "from-b")
    for worker in (a, b):
        with worker.active():
            assert (db.get_model_provider(provider) or {}).get("name") == (
                "from-a" if peer_op == "update" else None)
        losers = _conflicts(worker, provider, "model_providers")
        assert [(c["losing_device_id"], c["losing_record"]["name"]) for c in losers] == [(b.device_id, "from-b")]
        assert "b-token" not in str(losers)


# -- A restored worker (#1570): nothing is signed into the old stream while the
# -- batch proves its next sequence numbers are taken.

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
    _a, b, proof, held = _restored_with_proof(workers)
    with b.active():
        database.update_tag("t1", name="after-restore")
        engine.apply_batch(proof)
    _old_stream_unchanged(b, held)


def test_restored_worker_proof_after_peer_ops_in_a_later_chunk(workers, monkeypatch):
    """The peer's op comes first and waits; the proof moves B to a new
    stream; the retry signs B's edit there, then orders the two."""
    monkeypatch.setattr(engine, "APPLY_CHUNK", 2)
    a, b, proof, held = _restored_with_proof(workers)
    peer = _peer_edit(a, "from-a")
    with b.active():
        database.update_tag("t1", name="after-restore")
        engine.apply_batch(peer + [peer[0]] + proof)
    _old_stream_unchanged(b, held)
    assert _tag_name(b) == "after-restore"
    with b.active():
        engine.resume_pending()
    _settle(a, b)
    _old_stream_unchanged(b, held)
    assert _tag_name(a) == _tag_name(b)


# -- A parent's deletion cascades into shared children: unscanned children are
# -- signed first, like the parent itself.

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
                # add_permission_rule signs itself; drop its op to leave it unscanned.
                rule = subagents.add_permission_rule("p1", "Bash", "ls *", "allow")["id"]
                with database.get_db() as conn:
                    conn.cursor().execute("DELETE FROM sync_records WHERE collection = 'permission_rules' "
                                          "AND record_id = %s", (rule,))
                return "permission_rules"
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
def test_a_parent_deletion_signs_an_unscanned_child_first(workers, kind):
    parent, child, drop = _cascade_case(kind)
    a, b = _pair(workers)
    parent(a)
    with a.active():
        engine.scan_all()
    converge([a, b])
    collection = child(b)  # the scanner has not run on B
    drop(a)
    with a.active():
        engine.scan_all()
    signed = len(_own_ops(b, collection))
    exchange(a, b)
    assert len(_own_ops(b, collection)) == signed + 1
    _settle(a, b)


def test_a_state_copy_deletion_signs_an_unscanned_child_first(workers):
    from app.db import notes
    a, b = _pair(workers)
    with a.active():
        notes.create_note("n1", "Shared note", "body")
        engine.scan_all()
    converge([a, b])
    with b.active():
        notes.create_note_attachment("att-b", "n1", "b.txt", "notes/b.txt", "text/plain", 3, b"abc")
    with a.active():
        notes.delete_note("n1")
        engine.scan_all()
        chunk = engine.snapshot_chunk("notes", 0)
    with b.active():
        engine.apply_snapshot_chunk(chunk)
        assert notes.get_note("n1") is None
    assert [op["record_id"] for op in _own_ops(b, "note_attachments")] == ["att-b"]


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


# Real PostgreSQL: B's apply runs in one transaction while an app writer on a
# second connection writes the same row. The SQLite shim has a single
# connection per worker, so it cannot interleave two transactions.
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: E402,F401


def _second_connection(private_cluster, worker):
    from tests.db.schema_lifecycle_helpers import pg_connect
    schema = worker.sql("SELECT current_schema() AS s")[0]["s"]
    conn = pg_connect(private_cluster)
    with conn.cursor() as cursor:
        cursor.execute("SET search_path TO " + schema)
        cursor.execute("SET lock_timeout = '30s'")
    conn.commit()
    conn.autocommit = False
    return conn


def _interleave(monkeypatch, private_cluster, worker, hook, collection, record_id, work):
    """Run ``work(conn)`` on a second connection to ``worker``'s database
    just before its apply reaches ``engine.<hook>`` for ``record_id``; give
    it a second to finish (it blocks only on a lock the apply holds)."""
    import threading
    from tests.core.network.harness import _ACTIVE
    original = getattr(engine, hook)
    state = {"thread": None, "error": None}

    def run():
        conn = _second_connection(private_cluster, worker)
        try:
            work(conn)
        except Exception as exc:  # noqa: BLE001
            state["error"] = exc
        finally:
            conn.close()

    def wrapped(cursor, *args, **kwargs):
        coll, rid = (args[1], args[2]) if hook == "_check_local" else (args[0], args[1])
        if coll.name == collection and rid == record_id and state["thread"] is None and _ACTIVE.get() is worker:
            state["thread"] = threading.Thread(target=run)
            state["thread"].start()
            state["thread"].join(1.0)
        return original(cursor, *args, **kwargs)

    monkeypatch.setattr(engine, hook, wrapped)
    return state


def _finish(state):
    assert state["thread"] is not None
    state["thread"].join(30)
    assert not state["thread"].is_alive() and state["error"] is None


def _sql(statement, params=()):
    def work(conn):
        with conn.cursor() as cursor:
            cursor.execute(statement, params)
        conn.commit()
    return work


def test_postgres_app_write_between_check_and_replacement(pg_workers, private_cluster, monkeypatch):
    """The app renames the tag after B checked it and before the peer version
    is written. The row lock makes the rename wait for the apply's commit, so
    it lands on top and the scanner publishes it."""
    a, b = _pair(pg_workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    state = _interleave(monkeypatch, private_cluster, b, "materialize", "tags", "t1",
                        _sql("UPDATE tags SET name = %s WHERE id = 't1'", ("concurrent",)))
    exchange(a, b)
    _finish(state)
    assert _tag_name(b) == "concurrent"
    with b.active():
        assert engine.scan_all() == 1
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "concurrent"


def test_postgres_app_insert_while_a_peer_row_arrives(pg_workers, private_cluster, monkeypatch):
    """The app creates a row with the id of a peer row B has not got yet,
    after B found it absent. The apply does not overwrite it: the op waits,
    and its retry signs B's row first."""
    a, b = _pair(pg_workers)
    with a.active():
        database.create_tag("t2", "from-a")
        engine.scan_all()
    state = _interleave(
        monkeypatch, private_cluster, b, "materialize", "tags", "t2",
        _sql("INSERT INTO tags (id, name, color, created_at, updated_at) VALUES ('t2', %s, '#000000', NOW(), NOW())",
             ("local",)))
    exchange(a, b)
    _finish(state)
    assert _tag_name(b, "t2") == "local"
    exchange(a, b)
    with b.active():
        engine.resume_pending()
    converge([a, b])
    assert _tag_name(a, "t2") == _tag_name(b, "t2")
    assert len(_conflicts(a, "t2")) == len(_conflicts(b, "t2")) == 1


def test_postgres_permission_revocation_during_a_peer_apply(pg_workers, private_cluster, monkeypatch):
    """A local revocation of rule R runs while B applies A's change to R, after
    the apply took network_state and before it locks R. The revocation takes
    network_state before R, so it waits for the apply instead of deadlocking
    with it (R locked by the revocation, network_state by the apply)."""
    from types import SimpleNamespace
    from app.db import subagents
    from tests.core.network.harness import _ACTIVE
    a, b = _pair(pg_workers)
    with a.active():
        database.create_profile(profile_id="p1", name="Agent", description=None, config={})
        rule = subagents.add_permission_rule("p1", "Bash", "ls *", "allow")["id"]
        engine.scan_all()
    converge([a, b])
    with a.active():
        subagents.delete_permission_rule(rule)

    def revoke(conn):
        _ACTIVE.set(SimpleNamespace(shim=conn))  # this thread's get_db: the second connection
        subagents.delete_permission_rule(rule)

    state = _interleave(monkeypatch, private_cluster, b, "_check_local", "permission_rules", rule, revoke)
    result = exchange(a, b)
    _finish(state)
    assert result.retry == result.rejected == 0, result.reasons
    _settle(a, b)
    for worker in (a, b):
        assert not worker.sql("SELECT id FROM permission_rules WHERE id = ?", (rule,))
