"""Verified duplicate prefixes must match per-envelope apply, not trust sequence IDs."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest

from app.db import network as store
from app.network import engine, membership
from app.network.envelope import Envelope
from tests.core.network.harness import converge, join
from tests.core.network.test_bulk_differential import grow, produce, state
from tests.core.network.test_offline_delta_postgres import (  # noqa: F401
    _assert_complete, private_cluster, receiver,
)
from tests.core.network.test_pending_apply_budget import (  # noqa: F401
    pg_workers, pending_semantics_clock,
)


@pytest.mark.usefixtures("pending_semantics_clock")
@pytest.mark.parametrize("scenario", [
    "duplicates", "mixed", "forged_duplicate", "malformed_duplicate",
    "forged_fresh", "unknown_key", "wrong_network", "revoked", "stale", "unordered",
])
def test_duplicate_prefix_matches_per_envelope(pg_workers, monkeypatch, scenario):
    a, bulk, perop = [pg_workers(name) for name in ("source", "bulk", "perop")]
    with a.active():
        engine.create_network("Duplicate differential")
    join(a, bulk)
    join(a, perop)
    produce(a)
    converge([a, bulk, perop])
    with bulk.active():
        marks = store.local_watermarks()
    grow(a, "pure")
    with a.active():
        ops = [op for op in store.ops_since(marks, limit=1000) if op["collection"] == "session_events"]
        # A changed session header precedes these events on the real stream.
        headers = [op for op in store.ops_since(marks, limit=1000) if op["collection"] != "session_events"]
    assert len(ops) >= 48
    for worker in (bulk, perop):
        with worker.active():
            engine.apply_batch(headers)
            assert engine.apply_batch(ops[:24]).applied == 24
    wire = list(ops[:24] if scenario == "duplicates" else ops[:48])
    if scenario in ("forged_duplicate", "malformed_duplicate", "forged_fresh", "unknown_key", "wrong_network"):
        index = 36 if scenario == "forged_fresh" else 10
        bad = Envelope.from_dict(wire[index])
        if scenario.startswith("forged"):
            bad.signature = "bad"
        elif scenario == "malformed_duplicate":
            bad.origin_seq = 0
        else:
            if scenario == "unknown_key":
                bad.key_version += 100
            else:
                bad.network_id = "wrong-network"
            bad.sign(a.identity.signing_private)
        wire[index] = bad.to_dict()
    if scenario == "unordered":
        wire[1], wire[2] = wire[2], wire[1]
    results, snapshots = [], []
    for worker, force in ((bulk, False), (perop, True)):
        with worker.active(), monkeypatch.context() as patch:
            ctx = engine.require_context()
            if scenario == "revoked":
                ctx.members[a.device_id] = replace(ctx.member(a.device_id), status=membership.STATUS_REVOKED,
                    last_seq_accepted=ops[4]["origin_seq"], last_incarnation_accepted=1)
            elif scenario == "stale":
                with store.get_db() as conn:
                    store.set_watermark(conn.cursor(), a.device_id, 2, 0)
            if force:
                patch.setattr(engine, "_apply_chunk", lambda *a, **k: False)
                patch.setattr(engine, "_append_pending", lambda *a, **k: None)
            result = engine.apply_batch(wire)
            results.append((result.as_dict(), sorted(result.touched), result.own_ahead))
            snapshots.append(state(worker, a.device_id, ops[0]["origin_seq"] - 1))
    assert results[0] == results[1]
    assert snapshots[0] == snapshots[1]
    if scenario in ("forged_duplicate", "malformed_duplicate", "wrong_network", "forged_fresh"):
        assert results[0][0]["rejected"] == 1
    if scenario == "unknown_key":
        assert results[0][0]["retry"] == 1
    if scenario == "revoked":
        assert results[0][0]["rejected"] == len(wire) - 5
    if scenario == "stale":
        assert results[0][0]["rejected"] == len(wire)


@pytest.mark.parametrize("fresh", [0, 32])
def test_duplicate_prefix_statement_ratchet(receiver, monkeypatch, fresh):
    connect, delta = receiver
    ops, _ = delta(64)
    assert engine.apply_batch(ops[:32]).applied == 32
    ctx = engine.require_context()
    signatures = []
    verify = Envelope.verify_signature

    def verified(env, public):
        signatures.append(env.origin_seq)
        return verify(env, public)

    monkeypatch.setattr(Envelope, "verify_signature", verified)
    import psycopg2.extras
    statements = []
    execute = psycopg2.extras.RealDictCursor.execute

    def counted(cur, query, vars=None):
        statements.append(" ".join(str(query).split()))
        return execute(cur, query, vars)

    monkeypatch.setattr(psycopg2.extras.RealDictCursor, "execute", counted)
    result = engine.apply_batch(ops[:32 + fresh], ctx=ctx)
    assert result.duplicate == 32 and result.applied == fresh
    assert result.retry == result.rejected == result.pending == 0
    assert set(signatures) == {op["origin_seq"] for op in ops[:32 + fresh]}
    assert not any(q.startswith(("SAVEPOINT", "RELEASE SAVEPOINT")) for q in statements)
    # One locked frontier read plus the unchanged post-commit readiness probe.
    frontier_reads = [q for q in statements if q.startswith("SELECT") and "sync_watermarks" in q]
    assert len(frontier_reads) == 2
    assert sum(q.startswith("SELECT COALESCE(MAX(incarnation)") for q in frontier_reads) == 1
    if fresh:
        _assert_complete(connect, ops)


def test_racing_mixed_prefixes_keep_the_highest_watermark(receiver, monkeypatch):
    connect, delta = receiver
    ops, _ = delta(96)
    engine.apply_batch(ops[:32])
    ctx = engine.require_context()
    barrier = threading.Barrier(2)
    entered = threading.local()
    observe = store.observe_clock

    def locked(cur, clock):
        if not getattr(entered, "once", False):
            entered.once = True
            barrier.wait(timeout=10)
        return observe(cur, clock)

    monkeypatch.setattr(store, "observe_clock", locked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(engine.apply_batch, wire, ctx=ctx) for wire in (ops[:64], ops[16:])]
        results = [future.result(timeout=20) for future in futures]
    assert sum(r.applied for r in results) == 64
    assert sum(r.duplicate for r in results) == 80
    assert all(r.retry == r.rejected == r.pending == 0 for r in results)
    _assert_complete(connect, ops)


def test_duplicate_with_ready_pending_still_drains(receiver):
    connect, delta = receiver
    ops, _ = delta(96)
    engine.apply_batch(ops[:16])
    # Deliver the tail first, then commit the missing head without draining.
    assert engine.apply_batch(ops[32:]).pending == 64
    assert engine.apply_batch(ops[16:32], _pending_chunk_limit=0).applied == 16
    result = engine.apply_batch(ops[:32])
    assert result.duplicate == 32 and result.applied == 64
    assert result.retry == result.rejected == result.pending == 0
    _assert_complete(connect, ops)


def test_mixed_prefix_bulk_failure_rolls_back_before_retry(receiver, monkeypatch):
    connect, delta = receiver
    ops, _ = delta(64)
    engine.apply_batch(ops[:32])
    insert = store.insert_event_batch
    failures = []

    def failed(cur, *args):
        insert(cur, *args)
        failures.append(True)
        cur.execute("SELECT 1/0")

    monkeypatch.setattr(store, "insert_event_batch", failed)
    result = engine.apply_batch(ops)
    assert failures == [True]
    assert result.duplicate == result.applied == 32
    assert result.retry == result.rejected == result.pending == 0
    _assert_complete(connect, ops)
