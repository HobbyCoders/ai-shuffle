"""Mixed pending runs share the bounded transaction without losing correctness."""
import json
import math
import os
from time import perf_counter

import pytest

from app.db import network as store
from app.network import engine
from tests.core.network.harness import converge, join
from tests.core.network.test_bulk_differential import grow, produce, state
from tests.core.network.test_pending_apply_budget import measured_apply, pg_workers, private_cluster  # noqa: F401


def alternating(factory):
    a, bulk, perop = factory("publisher"), factory("bulk"), factory("perop")
    with a.active():
        engine.create_network("Alternating runs")
    join(a, bulk)
    join(a, perop)
    produce(a)
    converge([a, bulk, perop])
    with bulk.active():
        marks = store.local_watermarks()
    grow(a, "mixed", rounds=60, events_per_round=1)
    with a.active():
        ops = store.ops_since(marks, limit=1000, exclude_origin=bulk.device_id)
    assert len(ops) == 120
    assert [op["collection"] for op in ops] == ["session_events", "sessions"] * 60
    return a, bulk, perop, ops


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
@pytest.mark.parametrize("sample", range(int(os.environ.get("SYNC_RUN_REPEATS", "1"))))
def test_alternating_runs_share_one_bounded_window(request, monkeypatch, use_postgres, sample):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, bulk, perop, ops = alternating(factory)
    real_chunk = engine._apply_pending_chunk
    chunks = []

    def chunk(*args, **kwargs):
        locked = real_chunk(*args, **kwargs)
        if locked:
            chunks.append(True)
        return locked

    totals = {}
    for receiver, force_perop in [(bulk, False), (perop, True)]:
        with receiver.active(), monkeypatch.context() as patch:
            if force_perop:
                patch.setattr(engine, "_append_pending", lambda *args: None)
            else:
                patch.setattr(engine, "_apply_pending_chunk", chunk)
            assert engine.apply_batch(ops[1:]).pending == len(ops) - 1
            with measured_apply(patch, receiver) as (transactions, statements):
                started = perf_counter()
                result = part = engine.apply_batch(ops[:1])
                request_ms = (perf_counter() - started) * 1000
                for _ in range(len(ops)):
                    if not part._pending_more:
                        break
                    part = engine.resume_pending()
                    engine._merge_result(result, part)
                elapsed_ms = (perf_counter() - started) * 1000
            assert not part._pending_more
            assert max(tx["ops"] for tx in transactions) <= engine.APPLY_CHUNK
            assert result.applied == len(ops)
            assert result.retry == result.rejected == result.lost == 0
            totals[receiver.name] = sorted(result.touched)
            if not force_perop:
                print("ALTERNATING_METRICS " + json.dumps({
                    "postgres": use_postgres, "sample": sample, "ops": len(ops),
                    "chunks": len(chunks), "request_ms": request_ms,
                    "elapsed_ms": elapsed_ms, "db_operations": len(statements)}))
    assert totals[bulk.name] == totals[perop.name]
    base = ops[0]["origin_seq"] - 1
    actual, expected = state(bulk, a.device_id, base), state(perop, a.device_id, base)
    assert actual == expected
    assert actual["pending"] == 0
    assert actual["session"] == [{"title": "Title 59", "turn_count": 60}]
    assert len(actual["events"]) == 60
    for receiver in (bulk, perop):
        with receiver.active():
            assert engine.apply_batch(ops).duplicate == len(ops)
            with store.get_db() as conn:
                assert store.bump_clock(conn.cursor()) > max(int(op["clock"]) for op in ops)
    assert len(chunks) == math.ceil((len(ops) - 1) / engine.APPLY_CHUNK)

@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
@pytest.mark.parametrize("failure", ["transient_metadata", "bulk_after_metadata"])
def test_mixed_window_failure_preserves_predecessors(request, monkeypatch, use_postgres, failure):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, receiver, _perop, ops = alternating(factory)
    real_apply, real_insert = engine._apply_next, store.insert_event_batch
    target = ops[31]["origin_seq"]  # metadata after several successfully bulked event runs
    failures = []

    def apply(cursor, ctx, env, member, result):
        if env.origin_seq == target:
            failures.append(True)
            raise engine.TransientApplyError("metadata dependency")
        return real_apply(cursor, ctx, env, member, result)

    def insert(cursor, coll, rows, envelopes, hashes):
        real_insert(cursor, coll, rows, envelopes, hashes)
        # Fail after a metadata run and an event write in the same transaction.
        if envelopes[0]["origin_seq"] == ops[2]["origin_seq"]:
            failures.append(True)
            raise RuntimeError("bulk failed after metadata")

    with receiver.active():
        assert engine.apply_batch(ops[1:]).pending == len(ops) - 1
        with monkeypatch.context() as patch:
            if failure == "transient_metadata":
                patch.setattr(engine, "_apply_next", apply)
            else:
                patch.setattr(store, "insert_event_batch", insert)
            result = engine.apply_batch(ops[:1])
        assert failures
        if failure == "transient_metadata":
            assert result.applied == 31 and result.retry == 1 and result.rejected == 0
            with store.get_db() as conn:
                assert store.get_watermark(conn.cursor(), a.device_id, 1) == ops[30]["origin_seq"]
                assert store.count_pending(conn.cursor(), a.device_id, 1) == len(ops) - 31
            assert engine.apply_batch(ops[31:32]).applied == len(ops) - 31
        else:
            assert result.applied == len(ops) and result.retry == result.rejected == 0
        assert engine.apply_batch(ops).duplicate == len(ops)
    # Compare mirrors, not raw producer storage: the shim serializes the
    # producer's JSON with whitespace while both apply paths canonicalize it.
    with _perop.active():
        assert engine.apply_batch(ops).applied == len(ops)
    expected, actual = state(_perop, a.device_id, ops[0]["origin_seq"] - 1), state(
        receiver, a.device_id, ops[0]["origin_seq"] - 1)
    assert actual == expected
    assert actual["pending"] == 0

