"""Differential: buffered bulk append vs forced per-op on identical producer envelopes."""
import json

import pytest

from app.db import database, network as store, session_events
from app.network import catalog, engine
from tests.core.network.harness import converge, join
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: F401


def produce(a):
    with a.active():
        database.create_session(session_id="chat", profile_id="default", title="Chat")
        catalog.publish_owned()


def grow(a, scenario, *, rounds=5, events_per_round=40):
    with a.active():
        if scenario == "pure":
            session_events.append_events("chat", [
                {"type": "message.user", "payload": {"content": f"m{i}"}} for i in range(193)])
            catalog.hydrate("chat")
        elif scenario == "mixed":
            for round_ in range(rounds):
                session_events.append_events("chat", [
                    {"type": "message.user", "payload": {"content": f"r{round_} m{i}"}} for i in range(events_per_round)])
                catalog.hydrate("chat")
                database.update_session("chat", title=f"Title {round_}", turn_increment=1)
                catalog.publish_owned()
        elif scenario == "compact":
            session_events.append_events("chat", [
                {"type": "assistant.text_delta", "turn_id": "t1", "payload": {"text": f"d{i}"}}
                for i in range(100)])
            catalog.hydrate("chat")
            session_events.compact_session_deltas("chat", keep_tail_events=0)
            catalog.publish_owned()
            catalog.hydrate("chat")
            session_events.append_events("chat", [
                {"type": "message.user", "payload": {"content": f"after {i}"}} for i in range(80)])
            catalog.hydrate("chat")


def state(w, origin, base):
    import sqlite3
    q = (lambda sql, *args: w.sql(sql.replace('%s', '?'), *args)) if isinstance(w.raw, sqlite3.Connection) else w.sql
    with w.active():
        out = {
            "events": q("SELECT * FROM session_events WHERE session_id='chat' ORDER BY seq"),
            "records": q("SELECT collection, record_id, clock, origin_device_id, content_hash, deleted, order_seq "
                             "FROM sync_records WHERE origin_device_id=%s AND collection IN ('session_events','sessions') ORDER BY collection, record_id", (origin,)),
            "ops": q("SELECT origin_device_id, incarnation, seq, collection, record_id, op, clock, lane "
                         "FROM sync_ops WHERE origin_device_id=%s AND seq>%s ORDER BY seq", (origin, base)),
            "marks": q("SELECT origin_device_id, incarnation, last_seq FROM sync_watermarks "
                           "WHERE origin_device_id=%s", (origin,)),
            "pending": w.sql("SELECT COUNT(*) AS n FROM sync_pending")[0]["n"],
            "counters": w.sql("SELECT * FROM session_event_counters WHERE session_id='chat'"),
            "session": w.sql("SELECT title, turn_count FROM sessions WHERE id='chat'"),
        }
        with store.get_db() as conn:
            out["catalog_cursor"] = store.catalog_cursor(conn.cursor(), "chat")
    return json.loads(json.dumps(out, default=str))


@pytest.mark.parametrize("scenario", ["pure", "mixed", "compact"])
@pytest.mark.parametrize("use_postgres", [False, True], ids=["shim", "pg"])
def test_bulk_matches_per_op(request, monkeypatch, scenario, use_postgres):
    factory = request.getfixturevalue("pg_workers" if use_postgres else "workers")
    a, bulk, perop = factory("publisher"), factory("bulk"), factory("perop")
    with a.active():
        engine.create_network("Diff")
    join(a, bulk)
    join(a, perop)
    produce(a)
    converge([a, bulk, perop])
    with bulk.active():
        marks = store.local_watermarks()
    with perop.active():
        assert store.local_watermarks() == marks
    grow(a, scenario)
    with a.active():
        ops = store.ops_since(marks, limit=100000, exclude_origin=bulk.device_id)
    kinds = sorted({(o["collection"], o["op"]) for o in ops})
    print("SCENARIO", scenario, len(ops), kinds)

    appended = []
    real = engine._apply_event_append

    def spy(cursor, chunk, *args, **kwargs):
        ok = real(cursor, chunk, *args, **kwargs)
        if kwargs.get("_from_pending"):
            appended.append((len(chunk), ok))
        return ok

    results = {}
    for w, force_per_op in ((bulk, False), (perop, True)):
        with w.active(), monkeypatch.context() as patch:
            patch.setattr(engine, "_apply_event_append", spy)
            if force_per_op:
                patch.setattr(engine, "_append_pending", lambda *a_, **k_: None)
            first = engine.apply_batch(ops[1:])
            r = engine.apply_batch(ops[:1])
            results[w.name] = dict(pending=first.pending, applied=r.applied, dup=r.duplicate, lost=r.lost,
                                   rejected=r.rejected, retry=r.retry, touched=sorted(map(list, r.touched)))
            with store.get_db() as conn:
                results[w.name]["clock_after"] = store.bump_clock(conn.cursor()) > max(int(o["clock"]) for o in ops)
    print("APPEND_ATTEMPTS", scenario, appended)
    assert results["bulk"] == results["perop"], (results["bulk"], results["perop"])
    base = min(int(o['origin_seq']) for o in ops) - 1
    sb, sp = state(bulk, a.device_id, base), state(perop, a.device_id, base)
    for key in sb:
        assert sb[key] == sp[key], key
    # Replay is idempotent on both.
    for w in (bulk, perop):
        with w.active():
            assert engine.apply_batch(ops).duplicate == len(ops)