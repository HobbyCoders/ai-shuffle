"""Query-shape profile for real producer-to-buffered transcript catch-up."""
from collections import Counter
from contextlib import contextmanager
import json
import os
import re

import pytest

from app.db import network as store
from app.network import engine
from tests.core.network.test_pending_apply_budget import (  # noqa: F401
    backlog, measured_apply, pending_chunk_delay, pending_semantics_clock,
    pg_workers, private_cluster,
)


pytestmark = pytest.mark.usefixtures("pending_semantics_clock")


@contextmanager
def query_profile(worker):
    counts = Counter()
    previous = worker.raw.cursor_factory

    class ProfileCursor(previous):
        # Preserve the driver's backend marker used by engine._savepoint.
        __module__ = previous.__module__

        def execute(self, query, vars=None):
            sql = query.decode() if isinstance(query, bytes) else str(query)
            command = sql.split(None, 1)[0].upper()
            table = re.search(r"\b(?:FROM|INTO|UPDATE)\s+([a-z_]+)", sql, re.I)
            counts[command + "/" + (table.group(1) if table else "transaction")] += 1
            return super().execute(query, vars)

    worker.raw.cursor_factory = ProfileCursor
    try:
        yield counts
    finally:
        worker.raw.cursor_factory = previous


def test_buffer_phase_uses_one_stream_read_and_insert_per_chunk(pg_workers, monkeypatch):
    a, b, ops = backlog(pg_workers, 193, buffer=False)
    chunks = (len(ops) - 2) // engine.APPLY_CHUNK + 1
    with b.active(), query_profile(b) as profile, measured_apply(monkeypatch, b) as (transactions, statements):
        result = engine.apply_batch(ops[1:])
    assert result.pending == len(ops) - 1 and result.retry == result.rejected == 0
    assert result.applied == 0 and not result.touched
    assert len(transactions) == chunks
    assert sum(t["ops"] for t in transactions) == len(ops) - 1
    assert max(t["ops"] for t in transactions) <= engine.APPLY_CHUNK
    # One clock lock/update, highest incarnation, watermark, pending count and
    # multi-row insert per chunk. No per-row SQL or savepoints in this phase.
    assert sum(profile.values()) <= chunks * 5
    assert profile["SELECT/sync_watermarks"] == chunks * 2
    assert profile["SELECT/sync_pending"] == profile["INSERT/sync_pending"] == chunks
    assert profile["SAVEPOINT/transaction"] == profile["RELEASE/transaction"] == 0
    with b.active(), store.get_db() as conn:
        cur = conn.cursor()
        assert store.count_pending(cur, a.device_id, 1) == len(ops) - 1
        assert store.get_watermark(cur, a.device_id, 1) == ops[0]["origin_seq"] - 1


@pytest.mark.parametrize("corruption", ["signature", "network", "key", "counter"])
def test_buffer_phase_invalid_envelope_keeps_valid_rows_and_existing_outcomes(workers, corruption):
    from app.network.envelope import Envelope

    a, b, ops = backlog(workers, 193, buffer=False)
    bad = Envelope.from_dict(ops[70])
    if corruption == "signature":
        bad.signature = "bad"
    else:
        if corruption == "network":
            bad.network_id = "another-network"
        elif corruption == "key":
            bad.key_version += 100
        else:
            bad.origin_seq = 0
        bad.sign(a.identity.signing_private)
    wire = list(ops[1:])
    wire[69] = bad.to_dict()
    with b.active():
        result = engine.apply_batch(wire)
        assert result.pending == len(wire) - 1
        assert result.retry == int(corruption == "key")
        assert result.rejected == int(corruption != "key")
        assert result.applied == 0 and not result.touched
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.count_pending(cur, a.device_id, 1) == len(wire) - 1
            assert store.get_watermark(cur, a.device_id, 1) == ops[0]["origin_seq"] - 1


@pytest.mark.parametrize("budget,limit,accepted", [("cap", 80, 80), ("window", 80, 79)])
def test_buffer_phase_preserves_partial_acceptance_at_stream_limits(workers, monkeypatch, budget, limit, accepted):
    a, b, ops = backlog(workers, 193, buffer=False)
    monkeypatch.setattr(engine, "PENDING_CAP_PER_STREAM" if budget == "cap" else "PENDING_WINDOW", limit)
    with b.active():
        result = engine.apply_batch(ops[1:])
        assert result.pending == accepted and result.rejected == len(ops) - 1 - accepted
        assert result.retry == 0
        with store.get_db() as conn:
            assert store.count_pending(conn.cursor(), a.device_id, 1) == accepted
        if budget == "cap":
            # Legacy semantics refuse even buffered duplicates once at the cap.
            replay = engine.apply_batch(ops[1:])
            assert replay.rejected == len(ops) - 1 and replay.pending == 0


@pytest.mark.parametrize("refusal", ["revoked", "stale"])
def test_buffer_phase_refuses_revoked_and_stale_streams(workers, refusal):
    from dataclasses import replace
    from app.network import membership

    a, b, ops = backlog(workers, 193, buffer=False)
    with b.active():
        if refusal == "revoked":
            ctx = engine.require_context()
            ctx.members[a.device_id] = replace(ctx.member(a.device_id), status=membership.STATUS_REVOKED,
                last_seq_accepted=ops[0]["origin_seq"] - 1, last_incarnation_accepted=1)
        else:
            with store.get_db() as conn:
                store.set_watermark(conn.cursor(), a.device_id, 2, 0)
        result = engine.apply_batch(ops[1:])
        assert result.rejected == len(ops) - 1 and result.pending == result.retry == 0
        with store.get_db() as conn:
            assert store.count_pending(conn.cursor(), a.device_id, 1) == 0


@pytest.mark.parametrize("use_postgres,failure", [(False, "runtime"), (True, "sql")])
def test_buffer_phase_failed_bulk_insert_rolls_back_before_per_row_fallback(
        request, use_postgres, failure, monkeypatch):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 193, buffer=False)
    real_stash = store.stash_pending_batch
    attempts = []

    def failed_stash(cursor, envelopes):
        real_stash(cursor, envelopes)
        attempts.append(len(envelopes))
        if failure == "sql":
            cursor.execute("SELECT 1/0")
        raise RuntimeError("buffer insert failed after writing rows")

    monkeypatch.setattr(store, "stash_pending_batch", failed_stash)
    with b.active():
        result = engine.apply_batch(ops[1:])
        assert attempts == [engine.APPLY_CHUNK] * 3
        assert result.pending == len(ops) - 1 and result.retry == result.rejected == 0
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.count_pending(cur, a.device_id, 1) == len(ops) - 1
            assert store.get_watermark(cur, a.device_id, 1) == ops[0]["origin_seq"] - 1


@pytest.mark.parametrize("value", ["0", "false", " OFF ", "no"])
def test_buffer_phase_kill_switch_retains_per_row_path(workers, monkeypatch, value):
    from unittest.mock import Mock

    monkeypatch.setenv("AI_SHUFFLE_PERF_BULK_PENDING", value)
    a, b, ops = backlog(workers, 193, buffer=False)
    bulk = Mock(wraps=store.stash_pending_batch)
    monkeypatch.setattr(store, "stash_pending_batch", bulk)
    with b.active():
        assert engine.apply_batch(ops[1:]).pending == len(ops) - 1
        bulk.assert_not_called()
        result = engine.apply_batch(ops[:1])
        assert result.applied == len(ops) and result.retry == result.rejected == 0


def test_buffer_phase_duplicates_and_metadata_use_the_same_bulk_path(workers, monkeypatch):
    from unittest.mock import Mock
    from app.db import tags
    from tests.core.network.harness import join

    a, b = workers("tag-source"), workers("tag-peer")
    with a.active():
        engine.create_network("Shared metadata buffer")
    join(a, b)
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        for index in range(193):
            tags.create_tag(f"tag-{index}", f"Tag {index}")
        engine.scan_collection("tags")
        ops = store.ops_since(marks, limit=1000, exclude_origin=b.device_id)
    assert len(ops) == 193
    bulk = Mock(wraps=store.stash_pending_batch)
    monkeypatch.setattr(store, "stash_pending_batch", bulk)
    with b.active():
        for _ in range(2):
            assert engine.apply_batch(ops[1:]).pending == len(ops) - 1
        assert bulk.call_count == 6
        with store.get_db() as conn:
            assert store.count_pending(conn.cursor(), a.device_id, 1) == len(ops) - 1
        result = engine.apply_batch(ops[:1])
        assert result.applied == len(ops) and result.retry == result.rejected == 0
        assert len(tags.get_all_tags()) == len(ops)


def test_postgres_buffered_query_profile(pg_workers, monkeypatch):
    count = int(os.environ.get("SYNC_LAB_OPS", "193"))
    a, b, ops = backlog(pg_workers, count)
    with b.active(), query_profile(b) as profile, measured_apply(monkeypatch, b) as (transactions, statements):
        result = engine.apply_batch(ops[:1])
    print("PENDING_QUERY_PROFILE " + json.dumps(dict(sorted(profile.items()))))
    assert result.applied == len(ops) and result.retry == 0
    assert max(t["ops"] for t in transactions) <= engine.APPLY_CHUNK
    with b.active(), store.get_db() as conn:
        cur = conn.cursor()
        assert store.count_pending(cur, a.device_id, 1) == 0
        assert store.get_watermark(cur, a.device_id, 1) == ops[-1]["origin_seq"]


@pytest.mark.parametrize("_sample", range(int(os.environ.get("SYNC_LAB_REPEATS", "1"))))
def test_buffered_transcript_uses_bounded_bulk_sql(pg_workers, monkeypatch, _sample):
    a, b, ops = backlog(pg_workers, 193)
    chunks = []
    real_insert = store.insert_event_batch

    def insert(cursor, coll, rows, *args):
        chunks.append(len(rows))
        return real_insert(cursor, coll, rows, *args)

    monkeypatch.setattr(store, "insert_event_batch", insert)
    with b.active(), measured_apply(monkeypatch, b) as (transactions, statements):
        result = engine.apply_batch(ops[:1])
    assert result.applied == len(ops) and result.retry == 0
    assert chunks == [engine.APPLY_CHUNK] * 3
    assert len(statements) < len(ops) * 2
    with b.active(), store.get_db() as conn:
        clock = store.bump_clock(conn.cursor())
        assert clock > max(int(op["clock"]) for op in ops)


@pytest.mark.parametrize("use_postgres,failure", [(False, "runtime"), (True, "runtime"), (True, "sql")])
def test_failed_bulk_insert_rolls_back_before_per_op_fallback(request, use_postgres, failure, monkeypatch):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 193)
    real_insert = store.insert_event_batch
    attempts = []

    def failing_insert(cursor, coll, rows, *args):
        real_insert(cursor, coll, rows, *args)
        attempts.append(len(rows))
        if failure == "sql":
            cursor.execute("SELECT 1/0")
        raise RuntimeError("bulk insert failed after writing all three tables")

    with b.active():
        with monkeypatch.context() as patch:
            patch.setattr(store, "insert_event_batch", failing_insert)
            result = part = engine.apply_batch(ops[:1])
            # Per-op fallback can exhaust the request's elapsed-time budget.
            # Keep the injected failure active during background continuation.
            for _ in range(len(ops)):
                if not part._pending_more:
                    break
                part = engine.resume_pending()
                engine._merge_result(result, part)
            assert not part._pending_more
        assert attempts == [engine.APPLY_CHUNK] * 3
        assert result.applied == len(ops) and result.retry == result.rejected == 0
        assert len(result.touched) == len(ops)
        assert engine.apply_batch(ops).duplicate == len(ops)
        assert b.sql("SELECT COUNT(*) AS n FROM session_events WHERE session_id='chat'")[0]["n"] == len(ops)
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.count_pending(cur, a.device_id, 1) == 0
            assert store.get_watermark(cur, a.device_id, 1) == ops[-1]["origin_seq"]


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_buffered_prefix_stops_at_real_hole_and_resumes(request, use_postgres):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 193, withhold=(90,))
    with b.active():
        first = engine.apply_batch(ops[:1])
        assert first.applied == 90 and first.retry == 0
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.get_watermark(cur, a.device_id, 1) == ops[89]["origin_seq"]
            assert store.count_pending(cur, a.device_id, 1) == len(ops) - 91
        tail = engine.apply_batch(ops[90:91])
        assert tail.applied == len(ops) - 90 and tail.retry == 0
        assert engine.apply_batch(ops).duplicate == len(ops)
        assert not store.pending_holes()
        assert b.sql("SELECT COUNT(*) AS n FROM session_events WHERE session_id='chat'")[0]["n"] == len(ops)


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
def test_buffered_append_preserves_existing_newer_tombstone(request, use_postgres):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 193)
    target = ops[80]
    with b.active():
        with store.get_db() as conn:
            # Model a legacy/recovered sync record that predates this pending
            # stream. Real producer envelopes must not bypass its LWW tombstone.
            store.put_record(conn.cursor(), "session_events", target["record_id"],
                             clock=target["clock"] + 1000, origin_device_id=b.device_id,
                             content_hash="", deleted=True)
        result = engine.apply_batch(ops[:1])
        assert result.applied == len(ops) - 1 and result.lost == 1
        assert result.retry == result.rejected == 0
        with store.get_db() as conn:
            cur = conn.cursor()
            record = store.get_record(cur, "session_events", target["record_id"])
            assert record["deleted"] and record["clock"] == target["clock"] + 1000
            assert store.get_watermark(cur, a.device_id, 1) == ops[-1]["origin_seq"]
            assert store.count_pending(cur, a.device_id, 1) == 0
        assert b.sql("SELECT COUNT(*) AS n FROM session_events WHERE session_id='chat'")[0]["n"] == len(ops) - 1


@pytest.mark.parametrize("_sample", range(int(os.environ.get("SYNC_LAB_REPEATS", "1"))))
def test_mixed_buffered_transcript_uses_bulk_for_event_runs(pg_workers, monkeypatch, _sample):
    from tests.core.network.harness import converge, join
    from tests.core.network.test_bulk_differential import grow, produce

    a, b = pg_workers("publisher"), pg_workers("receiver")
    with a.active():
        engine.create_network("Mixed pending budget")
    join(a, b)
    produce(a)
    converge([a, b])
    with b.active():
        marks = store.local_watermarks()
    rounds = int(os.environ.get("SYNC_LAB_ROUNDS", "5"))
    grow(a, "mixed", rounds=rounds)
    with a.active():
        ops = store.ops_since(marks, limit=100000, exclude_origin=b.device_id)
    with b.active():
        assert engine.apply_batch(ops[1:]).pending == len(ops) - 1
    chunks = []
    real_insert = store.insert_event_batch

    def insert(cursor, coll, rows, *args):
        chunks.append(len(rows))
        return real_insert(cursor, coll, rows, *args)

    monkeypatch.setattr(store, "insert_event_batch", insert)
    with b.active(), measured_apply(monkeypatch, b) as (transactions, statements):
        result = engine.apply_batch(ops[:1])
        assert result.applied == min(len(ops), 1 + engine.INBOUND_PENDING_CHUNKS * engine.APPLY_CHUNK)
        part = result
        for _ in range(len(ops)):
            if not part._pending_more:
                break
            part = engine.resume_pending()
            engine._merge_result(result, part)
        assert not part._pending_more
    print("MIXED_BULK_METRICS " + json.dumps({"ops": len(ops), "rounds": rounds,
          "events": rounds * 40, "bulk_events": sum(chunks), "bulk_chunks": len(chunks)}))
    assert result.applied == len(ops) and result.retry == result.rejected == 0
    events = sum(op["collection"] == "session_events" and op["op"] == "upsert" for op in ops)
    head_is_event = ops[0]["collection"] == "session_events" and ops[0]["op"] == "upsert"
    assert sum(chunks) == events - int(head_is_event)
    assert max(t["ops"] for t in transactions) <= engine.APPLY_CHUNK
    assert len(statements) < len(ops) * 4
    with b.active(), store.get_db() as conn:
        cur = conn.cursor()
        assert store.count_pending(cur, a.device_id, 1) == 0
        assert store.get_watermark(cur, a.device_id, 1) == ops[-1]["origin_seq"]
        assert store.bump_clock(cur) > max(int(op["clock"]) for op in ops)
    assert b.sql("SELECT COUNT(*) AS n FROM session_events WHERE session_id='chat'")[0]["n"] == rounds * 40


@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "postgres"])
@pytest.mark.parametrize("pending_chunk_delay", [0.0, 0.3], ids=["fast", "slow"], indirect=True)
def test_bulk_failure_then_transient_per_op_failure_commits_good_prefix(
        request, use_postgres, monkeypatch, caplog, pending_chunk_delay):
    import logging
    from app.network.engine import TransientApplyError

    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, b, ops = backlog(factory, 193)
    real_insert, real_apply = store.insert_event_batch, engine._apply_next
    bad = ops[79]["origin_seq"]

    def failed_bulk(cursor, coll, rows, *args):
        real_insert(cursor, coll, rows, *args)
        raise RuntimeError("bulk failure after writes")

    def transient(cursor, ctx, env, member, result):
        if env.origin_seq == bad:
            raise TransientApplyError("waiting for dependency")
        return real_apply(cursor, ctx, env, member, result)

    caplog.set_level(logging.DEBUG, logger=engine.__name__)
    with b.active():
        with monkeypatch.context() as patch:
            patch.setattr(store, "insert_event_batch", failed_bulk)
            patch.setattr(engine, "_apply_next", transient)
            result = engine.apply_batch(ops[:1])
        assert result.applied == 79 and result.retry == 1 and result.rejected == 0
        assert any(row.exc_info and "bulk" in row.message.lower() for row in caplog.records)
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.get_watermark(cur, a.device_id, 1) == ops[78]["origin_seq"]
            assert store.count_pending(cur, a.device_id, 1) == len(ops) - 79
        tail = engine.resume_pending()
        assert tail.applied == 0  # the failed head is still cooling down
        # A fresh peer replay can retry now and clear the cooldown on progress.
        assert engine.apply_batch(ops[79:80]).applied == len(ops) - 79
        assert engine.apply_batch(ops).duplicate == len(ops)
        assert not engine.require_context()._pending_retries
