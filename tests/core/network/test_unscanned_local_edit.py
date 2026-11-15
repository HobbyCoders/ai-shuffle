"""#1605: a local change the scanner has not published yet must survive a peer's op.

Before the fix, applying a peer's higher-clock op over a row this worker had
changed (but not yet scanned into its outbox) overwrote the row, recorded no
conflict and never republished the change: it was lost on every worker.

Now the apply transaction signs the change first, with the clock it would
have had if the scanner had run just before the batch. Last writer wins
decides as usual and the loser is kept as a conflict on every worker.
"""
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


def _conflicts(worker, record_id="t1"):
    with worker.active():
        return [c for c in store.list_conflicts() if c["collection"] == "tags" and c["record_id"] == record_id]


def _losers(worker, record_id="t1"):
    return sorted((c["losing_record"].get("name", "<deleted>") if c["losing_record"] else None,
                   c["losing_device_id"]) for c in _conflicts(worker, record_id))


def _scan_is_quiet(*workers):
    for worker in workers:
        with worker.active():
            assert engine.scan_all() == 0


def test_unscanned_edit_of_an_existing_row_is_published_before_a_higher_clock_op(workers):
    """The issue's reproduction: B's edit is signed first and loses by clock,
    so it is a conflict on both workers instead of vanishing."""
    a, b = _pair(workers)
    _advance_clock(a)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    with b.active():
        database.update_tag("t1", name="from-b")  # the scanner has not run yet
    exchange(a, b)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("from-b", b.device_id)]
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-a"
    assert _losers(a) == [("from-b", b.device_id)]
    _scan_is_quiet(a, b)


def test_unscanned_edit_with_the_higher_clock_wins_everywhere(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    _advance_clock(b)
    with b.active():
        database.update_tag("t1", name="from-b")
    exchange(a, b)
    assert _tag_name(b) == "from-b"
    assert _losers(b) == [("from-a", a.device_id)]
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-b"
    assert _losers(a) == [("from-a", a.device_id)]
    _scan_is_quiet(a, b)


def test_unscanned_edit_twice_then_two_peer_ops(workers):
    """Only the last local value is signed, once; both peer ops then apply in order."""
    a, b = _pair(workers)
    _advance_clock(a)
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
    assert ("b-2", b.device_id) in _losers(b)
    assert ("b-1", b.device_id) not in _losers(b)
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "a-2"
    assert ("b-2", b.device_id) in _losers(a)
    _scan_is_quiet(a, b)


def test_unscanned_local_delete_is_published_and_kept_by_the_peer(workers):
    a, b = _pair(workers)
    _advance_clock(a)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    with b.active():
        database.delete_tag("t1")
    exchange(a, b)
    assert _tag_name(b) == "from-a"
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-a"
    # The signed delete lost by clock: both workers keep it as the loser.
    assert _losers(a) == _losers(b) == [("<deleted>", b.device_id)]
    _scan_is_quiet(a, b)


def test_unscanned_local_delete_with_the_higher_clock_is_not_resurrected(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    _advance_clock(b)
    with b.active():
        database.delete_tag("t1")
    exchange(a, b)
    assert _tag_name(b) is None
    converge([a, b])
    assert _tag_name(a) is None and _tag_name(b) is None
    assert _losers(a) == _losers(b) == [("from-a", a.device_id)]
    _scan_is_quiet(a, b)


def test_peer_delete_racing_an_unscanned_local_edit(workers):
    a, b = _pair(workers)
    _advance_clock(a)
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
    assert _losers(a) == [("from-b", b.device_id)]
    _scan_is_quiet(a, b)


def test_same_unscanned_edit_on_both_sides_records_no_conflict(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="same")
        engine.scan_all()
    with b.active():
        database.update_tag("t1", name="same")
    exchange(a, b)
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "same"
    assert not _conflicts(a) and not _conflicts(b)
    _scan_is_quiet(a, b)


def test_unscanned_new_row_with_the_same_id_as_a_peer_row(workers):
    """A row created here and never scanned, whose id a peer also wrote."""
    a, b = _pair(workers)
    _advance_clock(a)
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
    assert _losers(a, "t2") == [("from-b", b.device_id)]
    _scan_is_quiet(a, b)


def test_unscanned_edit_is_kept_as_a_conflict_while_the_outbox_is_held(workers):
    """It cannot be signed yet (a restarted worker before its first compare):
    the peer's version applies and the edit stays visible as a conflict."""
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    with b.active():
        database.update_tag("t1", name="from-b")
        engine.hold_outbox(True)
    try:
        exchange(a, b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("from-b", b.device_id)]
    _scan_is_quiet(a, b)


def test_unscanned_delete_is_kept_as_a_conflict_while_the_outbox_is_held(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
    with b.active():
        database.delete_tag("t1")
        engine.hold_outbox(True)
    try:
        exchange(a, b)
    finally:
        with b.active():
            engine.hold_outbox(False)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("<deleted>", b.device_id)]


def test_unscanned_edit_is_published_before_a_state_copy(workers):
    """The snapshot (state copy) path applies the same rule."""
    a, b = _pair(workers)
    _advance_clock(a)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
        chunk = engine.snapshot_chunk("tags", 0)
    with b.active():
        database.update_tag("t1", name="from-b")
        engine.apply_snapshot_chunk(chunk)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("from-b", b.device_id)]
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-a"
    assert _losers(a) == [("from-b", b.device_id)]
    _scan_is_quiet(a, b)


def test_unscanned_edit_is_kept_as_a_conflict_by_a_held_state_copy(workers):
    a, b = _pair(workers)
    with a.active():
        database.update_tag("t1", name="from-a")
        engine.scan_all()
        chunk = engine.snapshot_chunk("tags", 0)
    with b.active():
        database.update_tag("t1", name="from-b")
        engine.hold_outbox(True)
        try:
            engine.apply_snapshot_chunk(chunk)
        finally:
            engine.hold_outbox(False)
    assert _tag_name(b) == "from-a"
    assert _losers(b) == [("from-b", b.device_id)]


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


def test_peer_delete_loses_to_a_higher_clock_unscanned_edit(workers):
    a, b = _pair(workers)
    with a.active():
        database.delete_tag("t1")
        engine.scan_all()
    _advance_clock(b)
    with b.active():
        database.update_tag("t1", name="from-b")
    exchange(a, b)
    assert _tag_name(b) == "from-b"
    converge([a, b])
    assert _tag_name(a) == _tag_name(b) == "from-b"
    assert _losers(a) == _losers(b) == [("<deleted>", a.device_id)]
    _scan_is_quiet(a, b)


# -- A restored worker (#1570): no local op may take a sequence number its own
# -- retained ops already hold.

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
    with a.active(), database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT envelope FROM sync_ops WHERE origin_device_id = %s ORDER BY seq", (b.device_id,))
        signed = [store._load(row["envelope"], {}) for row in cursor.fetchall()]
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
        ops = [store._load(row["envelope"], {}) for row in b.sql(
            "SELECT envelope FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))]
    assert not [op for op in ops if op["origin_incarnation"] == 1 and op["origin_seq"] > held]


def _peer_edit(a, name):
    with a.active():
        database.update_tag("t1", name=name)
        engine.scan_all()
        ops = [store._load(row["envelope"], {}) for row in a.sql(
            "SELECT envelope FROM sync_ops WHERE origin_device_id = ? ORDER BY seq", (a.device_id,))]
    return [ops[-1]]


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
    """The peer's op for the edited record comes first, in its own chunk; the
    proof comes later. The edit is not signed into the old stream: the peer
    version replaces it and it is kept as B's conflict."""
    monkeypatch.setattr(engine, "APPLY_CHUNK", 2)
    a, b, proof, held = _restored_with_proof(workers)
    peer = _peer_edit(a, "from-a")
    with b.active():
        database.update_tag("t1", name="after-restore")
        engine.apply_batch(peer + [peer[0]] + proof)
    _old_stream_unchanged(b, held)
    assert _tag_name(b) == "from-a"
    assert ("after-restore", b.device_id) in _losers(b)
