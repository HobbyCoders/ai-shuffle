# ruff: noqa: F811
"""A first copy without transcripts, the transcripts after it, and a fast page apply."""
import json
import time

import httpx
import pytest

from app.db import database
from app.db import network as store
from app.db import session_events as events_db
from app.network import catalog, engine, hub, peer_snapshot, policy, transcripts, transport
from tests.core.network.harness import exchange, owner_pref, set_owner_pref
from tests.core.network.test_transport import (  # noqa: F401
    ServedBy,
    workers,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(peer_snapshot, "_jobs", {})
    monkeypatch.setattr(hub, "_binding", lambda: None)
    hub.reset_for_tests()
    transcripts.reset_for_tests()
    yield
    transcripts.reset_for_tests()
    hub.reset_for_tests()


def _chat(worker, session_id, count, *, updated_at):
    """A session ``worker`` runs, with ``count`` events, published to its outbox."""
    with worker.active():
        database.create_session(session_id=session_id, profile_id="default", project_id=None, title=session_id)
        events_db.append_events(session_id, [
            {"type": "message.user", "payload": {"type": "message.user", "content": f"{session_id} #{i}"}}
            for i in range(count)])
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET updated_at = %s WHERE id = %s", (updated_at, session_id))
        engine.scan_all()
        catalog.publish_events(recent_only=False, limit=100_000)


def joining(workers, chats=(("old", 3, "2026-01-01T00:00:00+00:00"),
                            ("mid", 5, "2026-05-01T00:00:00+00:00"),
                            ("new", 4, "2026-10-01T00:00:00+00:00"))):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="ServerWorker")
        admission = engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                                        encryption_public=b.identity.encryption_public, name="Pop")
    for session_id, count, updated_at in chats:
        _chat(a, session_id, count, updated_at=updated_at)
    set_owner_pref(a, "copied", "yes")
    with b.active():
        engine.enroll_from_response(admission, device_name="Pop")
    return a, b


def transcript(worker, session_id):
    with worker.active():
        return [(e["seq"], e["payload"]) for e in events_db.get_events(session_id, 0)]


def event_records(worker):
    with worker.active():
        return {r["record_id"]: (r["clock"], r["origin_device_id"], r["deleted"])
                for r in worker.sql("SELECT * FROM sync_records WHERE collection = 'session_events'")}


async def first_copy(a, b, served):
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        engine.finish_bootstrap()


async def backfill(b, served, monkeypatch, client_transport=None):
    monkeypatch.setattr(transcripts, "_link",
                        lambda device_id: transport.PeerLink(device_id, [served.base_url]))
    with b.active():
        async with httpx.AsyncClient(transport=client_transport or served.transport()) as client:
            return await transcripts.run_once(client)


# ---------------------------------------------------------------------------
# The first copy leaves transcripts out and the worker is ready at once
# ---------------------------------------------------------------------------

async def test_first_copy_leaves_transcripts_out_and_is_ready_at_once(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    with a.active():
        manifest = peer_snapshot.capture(b.device_id, fmt=peer_snapshot.FORMAT_DEFERRED)
        assert manifest["format"] == 2 and manifest["deferred"] == ["session_events"]
        assert manifest["encoding"] == "zlib"
        assert "session_events" not in {page["collection"] for page in manifest["pages"]}
        peer_snapshot._jobs.clear()
    await first_copy(a, b, served)
    with b.active():
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_READY
        assert {s["id"] for s in database.get_sessions()} >= {"old", "mid", "new"}
        assert b.sql("SELECT COUNT(*) AS n FROM session_events")[0]["n"] == 0
        assert owner_pref(b, "copied") == "yes"
        assert transcripts.pending()
        status = hub.status()["transcripts"]
        assert status["phase"] == "transcripts" and status["peer_name"] == "ServerWorker"
        assert (status["sessions_done"], status["sessions_total"]) == (0, 3)
        # Its watermarks claim A's events: those are owed by the backfill.
        assert store.local_watermarks()[a.device_id] == manifest["watermarks"][a.device_id]
        # A worker with gaps hands on neither a copy nor transcripts.
        with pytest.raises(ValueError, match="still receiving its transcripts"):
            peer_snapshot.state(a.device_id, fmt=2)
        with pytest.raises(ValueError, match="still receiving its transcripts"):
            transcripts.serve(a.device_id, [{"id": "new", "after": 0}])


async def test_backfill_copies_every_transcript_newest_first(workers, monkeypatch):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    monkeypatch.setattr(transcripts, "REQUEST_SESSIONS", 1)
    order = []
    apply_reply = transcripts.apply_reply

    def watched(reply, source, asked, **kwargs):
        order.extend(asked)
        return apply_reply(reply, source, asked, **kwargs)

    monkeypatch.setattr(transcripts, "apply_reply", watched)
    assert await backfill(b, served, monkeypatch)
    assert order == ["new", "mid", "old"]
    for session_id in ("old", "mid", "new"):
        assert transcript(b, session_id) == transcript(a, session_id)
    assert event_records(b) == event_records(a)
    with b.active():
        assert not transcripts.pending() and hub.status()["transcripts"] is None
        assert store.backfill_state() is None
        assert events_db.get_max_seq("mid") == 5
        # Now complete, it may serve others again.
        assert transcripts.serve(a.device_id, [{"id": "new", "after": 0}])["parts"]


async def test_an_opened_session_is_copied_next(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    with b.active():
        assert [r["id"] for r in transcripts._next_requests()[1]] == ["new", "mid", "old"]
        assert transcripts.prioritise("old")
        assert [r["id"] for r in transcripts._next_requests()[1]] == ["old", "new", "mid"]
        assert not transcripts.prioritise("not-a-session")


async def test_interrupted_backfill_resumes_where_it_stopped(workers, monkeypatch):
    a, b = joining(workers, chats=(("long", 40, "2026-10-01T00:00:00+00:00"),
                                   ("short", 2, "2026-01-01T00:00:00+00:00")))
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    # Small replies: a few events per request.
    monkeypatch.setattr(transcripts, "REPLY_BYTES", 300)
    calls = []

    def dropping(request):
        if request.url.path == transcripts.PATH:
            calls.append(json.loads(request.content)["sessions"])
            if len(calls) > 3:
                return httpx.Response(503, json={"detail": "gone"})
        return served.handler(request)

    assert not await backfill(b, served, monkeypatch, httpx.MockTransport(dropping))
    with b.active():
        [row] = b.sql("SELECT after_seq, done FROM network_transcript_backfill WHERE session_id = 'long'")
        copied = row["after_seq"]
        assert 0 < copied < 40 and not row["done"]
        assert [seq for seq, _ in transcript(b, "long")] == list(range(1, copied + 1))
        assert "gone" in hub.status()["transcripts"]["error"]
    # A restart forgets everything in memory; the next pass resumes after ``copied``.
    transcripts.reset_for_tests()
    calls.clear()

    def counting(request):
        if request.url.path == transcripts.PATH:
            calls.append(json.loads(request.content)["sessions"])
        return served.handler(request)

    assert await backfill(b, served, monkeypatch, httpx.MockTransport(counting))
    assert calls[0][0] == {"id": "long", "after": copied, "held": copied}
    assert transcript(b, "long") == transcript(a, "long")
    assert transcript(b, "short") == transcript(a, "short")
    assert event_records(b) == event_records(a)


async def test_live_events_during_the_backfill_leave_no_gap_or_duplicate(workers, monkeypatch):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    # While B still lacks the old transcripts, A keeps writing: new events
    # in an old session and a new session. They reach B by the live tail first.
    with a.active():
        events_db.append_events("mid", [{"type": "message.assistant",
                                         "payload": {"type": "message.assistant", "content": "later"}}])
        catalog.publish_events(recent_only=False, limit=100)
    _chat(a, "brand-new", 2, updated_at="2026-10-09T00:00:00+00:00")
    exchange(a, b)
    with b.active():
        assert [seq for seq, _ in transcript(b, "mid")] == [6], "only the live event so far"
        assert transcripts.session_pending("mid") and not transcripts.session_pending("brand-new")
    assert transcript(b, "brand-new") == transcript(a, "brand-new")
    assert await backfill(b, served, monkeypatch)
    for session_id in ("old", "mid", "new", "brand-new"):
        assert transcript(b, session_id) == transcript(a, session_id), session_id
    assert event_records(b) == event_records(a)
    with b.active():
        assert b.sql("SELECT COUNT(*) AS n FROM session_events")[0]["n"] == sum(
            len(transcript(a, s)) for s in ("old", "mid", "new", "brand-new"))
        assert events_db.get_max_seq("mid") == 6
    # Later writes still flow by the tail; nothing is applied twice.
    with a.active():
        events_db.append_events("old", [{"type": "message.user", "payload": {"type": "message.user", "content": "x"}}])
        catalog.publish_events(recent_only=False, limit=100)
    result = exchange(a, b)
    assert result.applied == 1 and not result.rejected
    assert transcript(b, "old") == transcript(a, "old")


async def test_a_source_behind_the_copy_is_refused(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    with a.active():
        reply = transcripts.serve(b.device_id, [{"id": "new", "after": 0}])
    with b.active():
        state = store.backfill_state()
        # The same reply, from a source that has not seen everything the copy covers.
        floor = {origin: {inc: seq + 1 for inc, seq in streams.items()} for origin, streams in state["floor"].items()}
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE network_transcript_backfill_state SET floor = %s", (json.dumps(floor),))
        with pytest.raises(transcripts.SourceBehind):
            transcripts.apply_reply(reply, a.device_id, {"new": 0}, generation=state["generation"])
        forged = dict(reply, watermarks={a.device_id: {"1": 10 ** 6}})
        with pytest.raises(ValueError, match="did not verify"):
            transcripts.apply_reply(forged, a.device_id, {"new": 0}, generation=state["generation"])
        assert b.sql("SELECT COUNT(*) AS n FROM session_events")[0]["n"] == 0


# ---------------------------------------------------------------------------
# Compatibility and compression
# ---------------------------------------------------------------------------

async def test_an_older_publisher_still_copies_everything_at_once(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    original = served.handler

    def older_server(request):
        if request.url.path == "/api/v1/network/peer/state":
            assert request.url.params.get("format") == "2"
            with a.active():
                # An older publisher ignores the query: a format 1 copy, transcripts included.
                return httpx.Response(200, json=peer_snapshot.capture(b.device_id))
        return original(request)

    with b.active():
        async with httpx.AsyncClient(transport=httpx.MockTransport(older_server)) as client:
            await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        engine.finish_bootstrap()
        assert not transcripts.pending()
    for session_id in ("old", "mid", "new"):
        assert transcript(b, session_id) == transcript(a, session_id)
    assert event_records(b) == event_records(a)


def test_an_older_recipient_gets_format_1_and_pages_compress(workers):
    a, b = joining(workers)
    with a.active():
        assert peer_snapshot.requested_format(None) == 1
        assert peer_snapshot.requested_format("7") == 2
        old = peer_snapshot.state(b.device_id)  # no format asked: an older recipient
        assert old["format"] == 1 and "deferred" not in old and "encoding" not in old
        assert "session_events" in {page["collection"] for page in old["pages"]}
        ctx = engine.require_context()
        plain, _ = engine.snapshot_page("session_events", ctx=ctx, limit=50)
        packed, _ = engine.snapshot_page("session_events", ctx=ctx, limit=50, encoding="zlib")
        assert "encoding" not in plain and packed["encoding"] == "zlib"
        assert engine.open_snapshot_chunk(plain, ctx=ctx) == engine.open_snapshot_chunk(packed, ctx=ctx)
        # The flag is signed: dropping it does not turn the page into a plain one.
        stripped = {k: v for k, v in packed.items() if k != "encoding"}
        with pytest.raises(engine.ApplyError):
            engine.open_snapshot_chunk(stripped, ctx=ctx)


def test_transcript_pages_are_much_smaller_compressed(workers):
    a = workers("a")
    with a.active():
        ctx = engine.create_network("Home")
        items = [{"record_id": f"s|{i}", "clock": i, "origin_device_id": ctx.device_id, "deleted": False,
                  "order_seq": i, "record": _event("s", i)} for i in range(1, 201)]
        plain = engine.seal_snapshot_chunk("session_events", 0, items, False, ctx=ctx)
        packed = engine.seal_snapshot_chunk("session_events", 0, items, False, ctx=ctx, encoding="zlib")
        assert len(json.dumps(packed)) * 3 < len(json.dumps(plain))


# ---------------------------------------------------------------------------
# Applying pages
# ---------------------------------------------------------------------------

def _event(session_id, seq):
    return {"id": f"{session_id}-{seq}", "session_id": session_id, "seq": seq, "run_id": "run-1",
            "turn_id": f"turn-{seq // 4}", "event_type": "message.assistant",
            "payload": {"type": "message.assistant", "content": f"answer {seq} " + "lorem ipsum " * 20},
            "command_id": None, "created_at": "2026-10-09T00:00:00+00:00"}


def _counting_statements(monkeypatch):
    from tests.pg_shim import ShimCursor
    execute = ShimCursor.execute
    seen = []

    def record(self, sql, *args, **kwargs):
        seen.append(" ".join(sql.split()))
        return execute(self, sql, *args, **kwargs)

    monkeypatch.setattr(ShimCursor, "execute", record)
    return seen


def test_the_clock_moves_once_per_page_and_new_events_insert_in_bulk(workers, monkeypatch):
    a = workers("a")
    with a.active():
        ctx = engine.create_network("Home")
        database.create_session(session_id="s", profile_id="default", project_id=None, title="s")
        items = [{"record_id": f"s|{i}", "clock": 1000 + i, "origin_device_id": "dev_other", "deleted": False,
                  "order_seq": i, "record": _event("s", i)} for i in range(1, 301)]
        chunk = engine.seal_snapshot_chunk("session_events", 0, items, False, ctx=ctx, encoding="zlib")
        seen = _counting_statements(monkeypatch)
        assert engine.apply_snapshot_chunk(chunk, ctx=ctx) == 300
        clock_updates = [sql for sql in seen if sql.startswith("UPDATE network_state SET clock")]
        assert len(clock_updates) == 1
        assert len([sql for sql in seen if sql.startswith("INSERT INTO session_events")]) == 1
        assert len(seen) < 20, "a page is a handful of statements, not several per event"
        assert store.get_state()["clock"] == 1300
        assert events_db.get_max_seq("s") == 300
        assert [e["seq"] for e in events_db.get_events("s", 0)] == list(range(1, 301))
        # The same page again is a no-op (resume after a crash).
        seen.clear()
        assert engine.apply_snapshot_chunk(chunk, ctx=ctx) == 0
        assert a.sql("SELECT COUNT(*) AS n FROM session_events")[0]["n"] == 300


def test_bulk_apply_keeps_the_per_record_rules_for_rows_already_here(workers):
    a = workers("a")
    with a.active():
        ctx = engine.create_network("Home")
        database.create_session(session_id="s", profile_id="default", project_id=None, title="s")
        # A local journal row at seq 2 and a newer record of seq 3 already held.
        events_db.append_events("s", [{"type": "message.user", "payload": {"content": "one"}},
                                      {"type": "message.user", "payload": {"content": "two"}}])
        newer = _event("s", 3)
        first = engine.seal_snapshot_chunk("session_events", 0, [
            {"record_id": "s|3", "clock": 50, "origin_device_id": "dev_other", "deleted": False,
             "order_seq": 3, "record": newer}], False, ctx=ctx)
        engine.apply_snapshot_chunk(first, ctx=ctx)
        stale = dict(_event("s", 3), payload={"content": "stale"})
        page = engine.seal_snapshot_chunk("session_events", 0, [
            {"record_id": f"s|{i}", "clock": 10, "origin_device_id": "dev_other", "deleted": False,
             "order_seq": i, "record": stale if i == 3 else _event("s", i)} for i in (2, 3, 4)], False, ctx=ctx)
        assert engine.apply_snapshot_chunk(page, ctx=ctx) == 2  # seq 2 (via the record path) and 4
        events = {e["seq"]: e for e in events_db.get_events("s", 0)}
        assert events[3]["payload"] == newer["payload"], "an older copy never replaces a newer record"
        assert events[2]["payload"] == _event("s", 2)["payload"]
        assert set(events) == {1, 2, 3, 4}


async def test_an_apply_cut_off_midway_resumes_and_adopts_the_frontier_last(workers, monkeypatch):
    a, b = joining(workers)
    with a.active():
        manifest = peer_snapshot.capture(b.device_id)  # format 1: every collection, several pages
    directory = b.data_dir / "copy"
    directory.mkdir()
    with a.active():
        for index in range(len(manifest["pages"])):
            raw = peer_snapshot.page(b.device_id, manifest["transfer"], index)
            (directory / f"{index}.json").write_bytes(raw)
    monkeypatch.setattr(peer_snapshot, "APPLY_BATCH_ITEMS", 1)  # commit after every page
    apply_chunk = engine.apply_snapshot_chunk
    calls = []

    def crashing(chunk, **kwargs):
        calls.append(chunk["collection"])
        if len(calls) == 6:
            raise RuntimeError("power cut")
        return apply_chunk(chunk, **kwargs)

    monkeypatch.setattr(engine, "apply_snapshot_chunk", crashing)
    with b.active():
        with pytest.raises(RuntimeError, match="power cut"):
            peer_snapshot.apply(manifest, directory, a.device_id)
        progress = json.loads((directory / "applied.json").read_text())
        assert progress == {"transfer": manifest["transfer"], "pages": 5}
        assert a.device_id not in store.local_watermarks(), "the frontier waits for the last page"
        calls.clear()

        def recording(chunk, **kwargs):
            calls.append(chunk["collection"])
            return apply_chunk(chunk, **kwargs)

        monkeypatch.setattr(engine, "apply_snapshot_chunk", recording)
        seen = []
        peer_snapshot.apply(manifest, directory, a.device_id,
                            on_progress=lambda done, total: seen.append((done, total)))
        assert seen[0][0] > 5 and seen[-1] == (len(manifest["pages"]), len(manifest["pages"]))
        assert calls[0] == manifest["pages"][5]["collection"], "it went on from the page that failed"
        assert store.local_watermarks()[a.device_id] == manifest["watermarks"][a.device_id]
        engine.finish_bootstrap()
        assert owner_pref(b, "copied") == "yes"
    for session_id in ("old", "mid", "new"):
        assert transcript(b, session_id) == transcript(a, session_id)
    assert event_records(b) == event_records(a)


def test_applying_50k_events_is_fast(workers):
    """Rough speed check: 50,000 events in pages of 500, one transaction per page.

    The old apply ran about ten statements per event, including an update of
    the one network_state row each time; this is a handful per page.
    """
    a = workers("a")
    with a.active():
        ctx = engine.create_network("Home")
        coll = policy.collection("session_events")
        sessions = [f"chat-{n}" for n in range(50)]
        for sid in sessions:
            database.create_session(session_id=sid, profile_id="default", project_id=None, title=sid)
        pages = []
        for sid in sessions:
            for start in (1, 501):
                pages.append([{"record_id": f"{sid}|{i}", "clock": i, "origin_device_id": "dev_other",
                               "deleted": False, "order_seq": i, "record": _event(sid, i)}
                              for i in range(start, start + 500)])
        started = time.perf_counter()
        for items in pages:
            with store.get_db() as conn:
                engine.apply_snapshot_items(conn.cursor(), coll, items, ctx=ctx)
        elapsed = time.perf_counter() - started
        assert a.sql("SELECT COUNT(*) AS n FROM session_events")[0]["n"] == 50_000
        assert a.sql("SELECT COUNT(*) AS n FROM sync_records WHERE collection = 'session_events'")[0]["n"] == 50_000
        print(f"applied 50,000 events in {elapsed:.1f}s ({50_000 / elapsed:,.0f} events/s)")
        assert elapsed < 60


async def test_leaving_the_network_drops_the_backfill(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    with b.active():
        assert transcripts.pending()
        engine.leave_network()
        assert not transcripts.pending()
        assert b.sql("SELECT COUNT(*) AS n FROM network_transcript_backfill")[0]["n"] == 0


# ---------------------------------------------------------------------------
# Review regressions
# ---------------------------------------------------------------------------

async def test_a_reply_fetched_before_a_re_copy_is_not_applied(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    with a.active():
        reply = transcripts.serve(b.device_id, [{"id": "new", "after": 0}])
    with b.active():
        old = store.backfill_state()
        # A re-copy lands meanwhile: higher floor, every session from the start.
        higher = {origin: {inc: seq + 5 for inc, seq in streams.items()} for origin, streams in old["floor"].items()}
        with store.get_db() as conn:
            cursor = conn.cursor()
            store.lock_state(cursor)
            generation = store.start_backfill(cursor, publisher=a.device_id, floor=higher,
                                              session_ids=["new", "mid", "old"])
        assert generation == old["generation"] + 1
        counts = transcripts.apply_reply(reply, a.device_id, {"new": 0}, generation=old["generation"])
        assert counts["restarted"] and counts["moved"] == 0
        [row] = b.sql("SELECT after_seq, done FROM network_transcript_backfill WHERE session_id = 'new'")
        assert row["after_seq"] == 0 and not row["done"]
        assert b.sql("SELECT COUNT(*) AS n FROM session_events")[0]["n"] == 0
        # Finishing the old generation deletes nothing of the new one.
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE network_transcript_backfill SET done = TRUE")
        assert not store.finish_backfill(old["generation"])
        assert store.backfill_state()["generation"] == generation
        assert store.finish_backfill(generation)
        assert store.backfill_state() is None


def test_applying_a_reply_takes_locks_in_the_registration_order(workers, monkeypatch):
    a, b = joining(workers)
    with a.active():
        reply = transcripts.serve(b.device_id, [{"id": "new", "after": 0}])
    with b.active():
        with store.get_db() as conn:
            generation = store.start_backfill(conn.cursor(), publisher=a.device_id, floor={}, session_ids=["new"])
        seen = _counting_statements(monkeypatch)
        transcripts.apply_reply(reply, a.device_id, {"new": 0}, generation=generation)
        state_lock = next(i for i, sql in enumerate(seen) if sql.startswith("SELECT * FROM network_state"))
        backfill_lock = next(i for i, sql in enumerate(seen)
                             if "FROM network_transcript_backfill_state" in sql and "FOR SHARE" in sql)
        row_lock = next(i for i, sql in enumerate(seen)
                        if "FROM network_transcript_backfill WHERE session_id" in sql)
        assert state_lock < backfill_lock < row_lock


async def test_a_re_copy_drops_events_deleted_elsewhere(workers, monkeypatch):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    assert await backfill(b, served, monkeypatch)
    assert [seq for seq, _ in transcript(b, "mid")] == [1, 2, 3, 4, 5]
    # A retracts the transcript's tail and a row in the middle, and B misses
    # those operations (it was cut off, then copied again).
    with a.active():
        ctx = engine.require_context()
        with store.get_db() as conn:
            cursor = conn.cursor()
            for seq in (3, 5):
                cursor.execute("DELETE FROM session_events WHERE session_id = 'mid' AND seq = %s", (seq,))
                engine.record(cursor, "session_events", f"mid|{seq}", None, ctx=ctx)
        floor = store.local_watermarks()
    with b.active():
        with store.get_db() as conn:
            cursor = conn.cursor()
            store.lock_state(cursor)
            store.start_backfill(cursor, publisher=a.device_id, floor=floor, session_ids=["mid"])
        transcripts.after_commit()
    assert await backfill(b, served, monkeypatch)
    assert [seq for seq, _ in transcript(b, "mid")] == [1, 2, 4]
    records = event_records(b)
    assert records["mid|3"][2] and records["mid|5"][2], "the deletions are recorded"


def test_serving_checks_the_table_not_the_cache(workers):
    a, b = joining(workers)
    with b.active():
        with store.get_db() as conn:
            store.start_backfill(conn.cursor(), publisher=a.device_id, floor={}, session_ids=["new"])
        transcripts._set_active(False)  # a stale cache, as before a registration commits
        assert not transcripts.pending()
        with pytest.raises(ValueError, match="still receiving its transcripts"):
            transcripts.serve(a.device_id, [{"id": "new", "after": 0}])
        engine.finish_bootstrap()
        transcripts._set_active(False)
        with pytest.raises(ValueError, match="still receiving its transcripts"):
            peer_snapshot.capture(a.device_id, fmt=2)


async def test_a_peer_still_backfilling_does_not_stop_the_exchange(workers):
    from tests.core.network.harness import join
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home", device_name="ServerWorker")
    join(a, c, name="C")  # C copied A long ago; its frontier is older than B's will be
    with a.active():
        admission = engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                                        encryption_public=b.identity.encryption_public, name="Pop")
    _chat(a, "new", 3, updated_at="2026-10-01T00:00:00+00:00")
    with b.active():
        engine.enroll_from_response(admission, device_name="Pop")
    await first_copy(a, b, ServedBy(a, "https://a.example.com"))
    with b.active():
        assert transcripts.pending()
    set_owner_pref(c, "from_c", "hello")
    served_b = ServedBy(b, "https://b.example.com")
    with c.active():
        async with httpx.AsyncClient(transport=served_b.transport()) as client:
            summary = await transport.PeerLink(b.device_id, [served_b.base_url]).sync_once(client, engine.context())
    assert summary["snapshot_required"] and summary["snapshot_deferred"]
    assert summary["sent"] > 0
    assert owner_pref(b, "from_c") == "hello", "C's operations still reached B"


async def test_the_floor_is_relaxed_when_no_source_has_covered_it_for_long(workers, monkeypatch):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    await first_copy(a, b, served)
    with b.active():
        # The copy's publisher is gone; the only source left is behind its floor.
        state = store.backfill_state()
        floor = {origin: {inc: seq + 100 for inc, seq in streams.items()} for origin, streams in state["floor"].items()}
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE network_transcript_backfill_state SET floor = %s", (json.dumps(floor),))
    assert await backfill(b, served, monkeypatch) is False
    with b.active():
        since = store.backfill_state()["stalled_since"]
        assert since is not None and "holds less" in hub.status()["transcripts"]["error"]
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE network_transcript_backfill_state SET stalled_since = %s",
                                  (since - transcripts.GIVE_UP_SECONDS - 1,))
    assert await backfill(b, served, monkeypatch) is True
    for session_id in ("old", "mid", "new"):
        assert transcript(b, session_id) == transcript(a, session_id)


def test_pages_that_compress_well_still_open_on_the_recipient(workers, monkeypatch):
    a, b = joining(workers)
    with a.active():
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET title = %s", ("x" * 30_000,))
    monkeypatch.setattr(engine, "MAX_SEALED_BODY", 50_000)
    with a.active():
        manifest = peer_snapshot.capture(b.device_id, fmt=2)
        ctx = engine.require_context()
        pages = [i for i, page in enumerate(manifest["pages"]) if page["collection"] == "sessions"]
        assert len(pages) >= 2, "the sender split the page by its size before compression"
        for index in pages:
            chunk = json.loads(peer_snapshot.page(b.device_id, manifest["transfer"], index))
            body = engine.open_snapshot_chunk(chunk, ctx=ctx)
            assert len(json.dumps(body)) <= 60_000


def test_a_row_written_meanwhile_is_left_to_the_per_record_path(workers):
    a = workers("a")
    with a.active():
        ctx = engine.create_network("Home")
        database.create_session(session_id="s", profile_id="default", project_id=None, title="s")
        coll = policy.collection("session_events")
        rows = [[engine._column_value(coll, name, _event("s", seq).get(name), ()) for name in coll.fields]
                for seq in (1, 2)]
        versions = [(f"s|{seq}", 10, ctx.device_id, "hash", seq) for seq in (1, 2)]
        # Another writer put seq 2 in after the caller's check.
        events_db.append_events("s", [{"type": "message.user", "payload": {"content": "one"}},
                                      {"type": "message.user", "payload": {"content": "local"}}])
        with store.get_db() as conn:
            conn.cursor().execute("DELETE FROM session_events WHERE session_id = 's' AND seq = 1")
        with store.get_db() as conn:
            inserted = store.insert_snapshot_events(conn.cursor(), coll.fields, rows, versions)
        assert inserted == {"s-1"}
        assert [r["record_id"] for r in a.sql(
            "SELECT record_id FROM sync_records WHERE collection = 'session_events'")] == ["s|1"]
        assert events_db.get_events("s", 0)[1]["payload"] == {"content": "local"}
