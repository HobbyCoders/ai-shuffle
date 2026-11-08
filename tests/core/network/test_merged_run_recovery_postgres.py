# ruff: noqa: F811
"""A peer holding part of a delta run the owner later merged recovers (#1278).

The owner's delta compaction merges a run of streamed fragments into one row
at the run's last seq, under the first fragment's id, naming the run's first
seq (``merged_from_seq``). That row means the same on every worker, so one rule
applies on every path (``session_events.fit_mirrored_delta``): a merged row
replaces the fragments of its run a worker holds, and an original fragment of
a run held merged is not written. A peer may hold any prefix of the run, get
the merged row from the owner or another mirror, and get the owner's original
fragments before or after it; every worker ends with the owner's text once.
A reader whose cursor row a merged row replaced is reset and replays.

Real PostgreSQL, independent worker schemas, the real event and catalog
producers, signed exchange through the actual peer router and the scheduled
maintenance loop. Never the configured application database.
"""
import asyncio
import json
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

from app.api.events import _resume_check
from app.core.transcript_replay import _coalesce_deltas, _flush_pending
from app.db import base, sessions
from app.db import network as store
from app.db import session_events as events_db
from app.db.base import get_db
from app.network import catalog, engine, handoff, policy, transcripts, transport
from tests.core.network.test_transcript_op_retention_postgres import (  # noqa: F401
    admit,
    cluster,
    old_ops,
    pg_workers,
    prune_once,
    publish,
    sync,
    transcript,
)
from tests.core.network.test_transport import ServedBy

WHOLE = "part 0 part 1 part 2 part 3 part 4 part 5 "


def deltas(worker, count, *, start=0, finish=False):
    with worker.active():
        events_db.append_events("chat", [
            {"type": "assistant.text_delta", "payload": {"text": f"part {n} "}} for n in range(start, start + count)])
        if finish:
            events_db.append_events("chat", [{"type": "message.assistant", "payload": {"content": "done"}}])
        catalog.publish_owned()


def frames(worker, after=0):
    """What a reader is sent from ``after`` on: the replay's merged frames."""
    with worker.active():
        rows = events_db.get_events("chat", from_seq=after, limit=1000)
    pending = [None]
    merged = [*_coalesce_deltas(rows, pending), *_flush_pending(pending)]
    return [(row["event_type"], (row.get("payload") or {}).get("text") or (row.get("payload") or {}).get("content"))
            for row in merged]


def expire(source, peer):
    """The peer is unseen past the contact grace; the source prunes its transcript ops."""
    source.sql("INSERT INTO sync_peers(device_id, last_contact, updated_at) VALUES (%s, '2000-01-01', '2000-01-01') "
               "ON CONFLICT (device_id) DO UPDATE SET last_contact = EXCLUDED.last_contact, "
               "updated_at = EXCLUDED.updated_at", (peer.device_id,))
    transport._peer_watermarks.pop(peer.device_id, None)
    old_ops(source)


async def fill(peer, served, monkeypatch):
    monkeypatch.setattr(transcripts, "_link", lambda device_id: transport.PeerLink(
        device_id, [served.base_url] if device_id == served.server.device_id else []))
    with peer.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            for _ in range(5):
                if not transcripts.pending():
                    break
                await transcripts.run_once(client)
        assert not transcripts.pending()


def copy_items(source, *, after, held):
    """The source's backfill page for the chat (what ``transcripts.serve`` seals)."""
    coll = policy.collection("session_events")
    with source.active(), get_db() as conn:
        items, _last, more, _used = transcripts._session_items(conn.cursor(), coll, "chat", after, 1 << 30, held=held)
    assert not more
    return items


def apply_items(peer, items):
    """Apply a backfill page on the peer the way ``transcripts`` does."""
    coll = policy.collection("session_events")
    with peer.active():
        with get_db() as conn:
            _applied, touched = engine.apply_snapshot_items(conn.cursor(), coll, items, ctx=engine.context(),
                                                            existed=["chat"])
        engine._notify(touched)


async def merged_source(pg_workers, held):
    """A source whose six-fragment run is merged; the peer synced ``held`` fragments of it."""
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Merged")
    publish(source, 1, create=True)
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    deltas(source, held)
    await sync(peer, served)
    assert len(transcript(peer)) == 1 + held
    deltas(source, 6 - held, start=held, finish=True)
    with source.active():
        assert events_db.compact_session_deltas("chat", keep_tail_events=0) == (1, 5)
    assert [row["seq"] for row in transcript(source)] == [1, 7, 8]
    return source, peer, served


async def merged_source_unmerged(pg_workers):
    """A source with fragments 0-3 and a peer synced through fragment 1: the next ops are a fast append."""
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Merged")
    publish(source, 1, create=True)
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    deltas(source, 2)
    await sync(peer, served)
    deltas(source, 2, start=2)
    return source, peer, served


def merged_row(worker):
    rows = [row for row in transcript(worker) if (row["payload"] or {}).get("text") == WHOLE]
    assert len(rows) == 1
    return rows[0]


def ops_for(source, seqs):
    """The source's signed ordinary-sync envelopes for these transcript seqs, in order."""
    wanted = {f"chat|{seq}" for seq in seqs}
    with source.active():
        ops = store.ops_since({}, limit=10000)
    picked = [op for op in ops if op["collection"] == "session_events" and op["record_id"] in wanted]
    assert len(picked) == len(wanted)
    return picked


@pytest.mark.parametrize("held", [1, 3])
async def test_expired_peer_holding_part_of_a_merged_run_recovers(pg_workers, monkeypatch, held):
    source, peer, served = await merged_source(pg_workers, held)
    with peer.active():
        epoch = events_db.get_log_epoch("chat")
    expire(source, peer)
    await prune_once(source, monkeypatch)
    assert (await sync(peer, served))["snapshot_required"]
    await fill(peer, served, monkeypatch)
    # The whole answer, once, as the owner shows it, stored as the owner stores it.
    assert frames(peer) == frames(source) == [
        ("message.user", "message 1"), ("assistant.text_delta", WHOLE), ("message.assistant", "done")]
    assert transcript(peer) == transcript(source)
    assert merged_row(peer)["payload"][events_db.MERGED_FROM_KEY] == 2
    # A reader that showed the held fragments sits on a replaced row: it is
    # reset (rows rewritten beneath the terminal one) and replays, never sent
    # the run twice. Workers still compare the plain first-row id.
    with peer.active():
        current = events_db.get_reader_epoch("chat")
        assert current != epoch and events_db.get_log_epoch("chat") == epoch
        assert _resume_check("chat", 1 + held, epoch) == (current, "epoch")
        assert events_db.get_log_check("chat", 1 + held) == (current, False)
    # Later writes still flow by ops.
    publish(source, 1)
    assert not (await sync(peer, served)).get("snapshot_required")
    assert frames(peer) == frames(source)


async def test_original_fragments_arriving_after_the_merged_row_do_not_repeat_text(pg_workers):
    # A deferred copy captured the frontier after C; the merged ABCDEF reaches
    # the peer before the ordinary D/E/F operations do.
    source, peer, served = await merged_source(pg_workers, 3)
    apply_items(peer, copy_items(source, after=4, held=4))
    assert transcript(peer) == transcript(source)
    with peer.active():
        epoch = events_db.get_log_epoch("chat")
    assert not (await sync(peer, served)).get("snapshot_required")
    assert frames(peer) == frames(source)
    assert frames(peer)[1] == ("assistant.text_delta", WHOLE)
    # D/E/F were not written next to the merged row that stands for them.
    assert transcript(peer) == transcript(source)
    with peer.active():
        assert events_db.get_log_epoch("chat") == epoch
    assert frames(peer, after=7) == [("message.assistant", "done")]


async def test_ordinary_sync_split_before_the_last_fragment_never_shows_wrong_text(pg_workers, monkeypatch):
    # S1: the bulk append path (not only the per-op path) applies the rule. A
    # batch carrying D/E but not F would have no sync record of its own to
    # push it off the fast path, so the guard must see the merged row.
    source, peer, _served = await merged_source(pg_workers, 3)
    reader = 4  # a connected reader has been sent message 1 and A, B, C
    apply_items(peer, copy_items(source, after=4, held=4))
    with peer.active():
        # The shared SSE reader's probe after the mirror write: reset.
        assert events_db.get_log_check("chat", reader) == (events_db.get_reader_epoch("chat"), False)
    assert frames(peer) == frames(source)
    reader = 8  # it replayed the whole log
    with peer.active():
        replayed_epoch = events_db.get_reader_epoch("chat")
    guarded = []
    real = events_db.delta_rows_need_fitting

    def spy(cur, records):
        answer = real(cur, records)
        guarded.append(([record["seq"] for record in records], answer))
        return answer

    monkeypatch.setattr(events_db, "delta_rows_need_fitting", spy)
    for batch in ([5, 6], [7, 8]):
        with peer.active():
            applied = engine.apply_batch(ops_for(source, batch))
        assert applied.rejected == applied.pending == applied.retry == 0
        # Between batches the reader is sent nothing new and its log reads right.
        assert frames(peer, after=reader) == []
        assert frames(peer) == frames(source)
        with peer.active():
            assert events_db.get_log_check("chat", reader) == (replayed_epoch, True)  # reset once only
    assert ([5, 6], True) in guarded  # the D/E batch reached the bulk guard
    assert transcript(peer) == transcript(source)


async def test_a_mirror_recovered_from_a_merged_row_is_a_safe_backfill_source(pg_workers):
    # S2: three workers hold unequal prefixes. B recovers from the owner; C,
    # holding one fragment more than B did, backfills from B.
    source, b, c = pg_workers("source"), pg_workers("b"), pg_workers("c")
    with source.active():
        engine.create_network("Merged")
    publish(source, 1, create=True)
    admit(source, b)
    admit(source, c)
    served = ServedBy(source, "https://source.example.com")
    deltas(source, 3)
    await sync(b, served)
    deltas(source, 1, start=3)
    await sync(c, served)
    assert len(transcript(b)) == 4 and len(transcript(c)) == 5
    deltas(source, 2, start=4, finish=True)
    with source.active():
        assert events_db.compact_session_deltas("chat", keep_tail_events=0) == (1, 5)
    apply_items(b, copy_items(source, after=4, held=4))
    assert transcript(b) == transcript(source)
    apply_items(c, copy_items(b, after=5, held=5))
    assert frames(c) == frames(b) == frames(source) == [
        ("message.user", "message 1"), ("assistant.text_delta", WHOLE), ("message.assistant", "done")]
    assert transcript(c) == transcript(source)
    # The owner's ordinary ops reaching C afterwards change nothing.
    assert not (await sync(c, served)).get("snapshot_required")
    assert transcript(c) == transcript(source)


@pytest.mark.parametrize("legacy", [False, True])
async def test_original_fragments_from_a_mirror_reach_a_peer_holding_the_merged_row(pg_workers, legacy):
    # S3: P copied the compacted owner; a later copy picks mirror Q, which kept
    # the original fragments. ``legacy``: P's merged row predates
    # ``merged_from_seq`` and is recognised by its id alone.
    source, q, p = pg_workers("source"), pg_workers("q"), pg_workers("p")
    with source.active():
        engine.create_network("Merged")
    publish(source, 1, create=True)
    admit(source, q)
    admit(source, p)
    served = ServedBy(source, "https://source.example.com")
    deltas(source, 6, finish=True)
    await sync(q, served)
    with source.active():
        assert events_db.compact_session_deltas("chat", keep_tail_events=0) == (1, 5)
    apply_items(p, copy_items(source, after=1, held=1))
    assert transcript(p) == transcript(source)
    if legacy:
        p.sql("UPDATE session_events SET payload = payload - 'merged_from_seq' WHERE session_id = 'chat'")
    original = copy_items(q, after=1, held=1)
    assert len(original) == 7
    apply_items(p, original)
    assert frames(p) == frames(q) == frames(source)
    assert [row["seq"] for row in transcript(p)] == [1, 7, 8]
    assert merged_row(p)["payload"][events_db.MERGED_FROM_KEY] == 2
    # Again (a restarted copy), then the owner's ordinary ops: still once.
    apply_items(p, original)
    assert not (await sync(p, served)).get("snapshot_required")
    assert transcript(p) == transcript(source)


async def test_reset_peer_holding_any_prefix_recovers_again(pg_workers):
    source, peer, _served = await merged_source(pg_workers, 3)
    backup = peer.sql("SELECT * FROM session_events WHERE session_id = 'chat' ORDER BY seq")
    items = copy_items(source, after=0, held=8)
    apply_items(peer, items)
    assert transcript(peer) == transcript(source)
    # The same page again (a restarted backfill) changes nothing.
    apply_items(peer, items)
    assert transcript(peer) == transcript(source)
    # A restored backup holding a shorter prefix (message 1 and A), then a fresh copy.
    peer.sql("DELETE FROM session_events WHERE session_id = 'chat'")
    for row in backup[:2]:
        peer.sql("INSERT INTO session_events (id, session_id, seq, run_id, turn_id, event_type, payload, "
                 "command_id, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                 (row["id"], row["session_id"], row["seq"], row["run_id"], row["turn_id"], row["event_type"],
                  json.dumps(row["payload"]), row["command_id"], row["created_at"]))
    peer.sql("DELETE FROM sync_records WHERE collection = 'session_events' AND record_id NOT IN ('chat|1', 'chat|2')")
    apply_items(peer, copy_items(source, after=2, held=2))
    assert frames(peer) == frames(source)
    assert transcript(peer) == transcript(source)


async def test_peer_compaction_matches_the_owner_merge(pg_workers):
    # A peer that held the whole run merges it itself: the same canonical row
    # as the owner's, so the owner's merged row arriving later is a no-op.
    source, peer, served = await merged_source(pg_workers, 3)
    assert not (await sync(peer, served)).get("snapshot_required")
    with peer.active():
        assert events_db.compact_session_deltas("chat", keep_tail_events=0) == (1, 5)
    assert transcript(peer) == transcript(source)
    apply_items(peer, copy_items(source, after=0, held=8))
    assert transcript(peer) == transcript(source)


async def test_peer_merging_its_held_prefix_still_reaches_the_owner_text(pg_workers):
    # The peer merged A/B/C among themselves before D/E/F arrived. Its merged
    # row names its run's first seq too, so it stays right wherever it goes.
    source, peer, served = await merged_source(pg_workers, 3)
    with peer.active():
        assert events_db.compact_session_deltas("chat", keep_tail_events=0) == (1, 2)
    assert not (await sync(peer, served)).get("snapshot_required")
    assert frames(peer) == frames(source)
    rows = transcript(peer)
    assert [row["seq"] for row in rows] == [1, 4, 5, 6, 7, 8]
    assert rows[1]["payload"][events_db.MERGED_FROM_KEY] == 2
    apply_items(peer, copy_items(source, after=0, held=8))
    assert frames(peer) == frames(source)
    # Backfilled to a worker holding only message 1 and A, it replaces A.
    third = pg_workers("third")
    admit(source, third)
    third.sql("DELETE FROM session_events WHERE session_id = 'chat' AND seq > 1")
    third.sql("INSERT INTO session_events (id, session_id, seq, event_type, payload) "
              "VALUES (%s, 'chat', 2, 'assistant.text_delta', %s::jsonb)",
              (rows[1]["id"], json.dumps({"text": "part 0 "})))
    third.sql("DELETE FROM sync_records WHERE collection = 'session_events' AND record_id NOT IN ('chat|1', 'chat|2')")
    apply_items(third, copy_items(peer, after=2, held=2))
    assert frames(third) == frames(source)


# ---------------------------------------------------------------------------
# Coverage that cannot be proven: nothing deleted, whole-log reinstall (#1278)
# ---------------------------------------------------------------------------

def delta_row(seq, row_id, text, first=None):
    payload = {"text": text}
    if first is not None:
        payload[events_db.MERGED_FROM_KEY] = first
    return {"id": row_id, "session_id": "chat", "seq": seq, "event_type": "assistant.text_delta",
            "turn_id": None, "run_id": None, "payload": payload}


def lone_worker(pg_workers):
    worker = pg_workers("lone")
    with worker.active():
        engine.create_network("Lone")
    publish(worker, 1, create=True)
    return worker


def plant(worker, *rows):
    """Replace the chat's rows above its first with exactly these."""
    worker.sql("DELETE FROM session_events WHERE session_id = 'chat' AND seq > 1")
    for row in rows:
        worker.sql("INSERT INTO session_events (id, session_id, seq, event_type, payload) "
                   "VALUES (%s, 'chat', %s, 'assistant.text_delta', %s::jsonb)",
                   (row["id"], row["seq"], json.dumps(row["payload"])))


def apply_fitted(worker, record):
    """One mirrored write, fitted and upserted the way ``materialize`` does it."""
    with worker.active(), get_db() as conn:
        cursor = conn.cursor()
        fitted = events_db.fit_mirrored_delta(cursor, "chat", record["seq"], record)
        if fitted is None:
            return None
        cursor.execute("INSERT INTO session_events (id, session_id, seq, event_type, payload) "
                       "VALUES (%s, 'chat', %s, 'assistant.text_delta', %s::jsonb) "
                       "ON CONFLICT (session_id, seq) DO UPDATE SET id = EXCLUDED.id, payload = EXCLUDED.payload",
                       (fitted["id"], fitted["seq"], json.dumps(fitted["payload"])))
        return fitted


def held(worker):
    return [(row["seq"], row["id"], row["payload"].get("text"), row["payload"].get(events_db.MERGED_FROM_KEY))
            for row in transcript(worker) if row["seq"] > 1]


def queued(worker):
    return [row["session_id"] for row in worker.sql("SELECT session_id FROM network_transcript_repairs")]


@pytest.mark.parametrize("stamped", [True, False])
async def test_partly_overlapping_merged_runs_keep_the_held_text(pg_workers, stamped):
    # S4: the held ABC ends inside the incoming CDEF but starts before it. An
    # unstamped held row looks like a fragment; its text does not fit either.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(4, "A", "ABC", 2 if stamped else None))
    assert apply_fitted(worker, delta_row(7, "C", "CDEF", 4)) is None
    assert held(worker) == [(4, "A", "ABC", 2 if stamped else None)]
    assert queued(worker) == ["chat"]


async def test_held_merged_run_starting_earlier_is_kept_even_when_its_text_fits(pg_workers):
    # A repeating reply: the held "ha" does appear in "hahaha", but it stands
    # for seqs before the incoming run, so the incoming row does not cover it.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(4, "A", "ha", 2))
    assert apply_fitted(worker, delta_row(7, "C", "hahaha", 4)) is None
    assert held(worker) == [(4, "A", "ha", 2)]
    assert queued(worker) == ["chat"]


async def test_merged_row_starting_inside_a_held_merged_run_is_queued(pg_workers):
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(7, "D", "DEFG", 5))
    assert apply_fitted(worker, delta_row(6, "B", "BCDE", 3)) is None
    assert held(worker) == [(7, "D", "DEFG", 5)]
    assert queued(worker) == ["chat"]


async def test_row_of_another_kind_inside_a_merged_run_is_not_replaced(pg_workers):
    # The owner only merges unbroken runs: this worker's log is not that run.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(2, "A", "A"), delta_row(3, "B", "B"))
    worker.sql("UPDATE session_events SET event_type = 'message.assistant' WHERE session_id = 'chat' AND seq = 3")
    assert apply_fitted(worker, delta_row(7, "A", "ABCDEF", 2)) is None
    assert [row[0] for row in held(worker)] == [2, 3]
    assert queued(worker) == ["chat"]


async def test_merged_row_covered_by_a_held_merged_run_is_skipped(pg_workers):
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(7, "A", "ABCDEF", 2))
    assert apply_fitted(worker, delta_row(4, "B", "BC", 3)) is None
    assert held(worker) == [(7, "A", "ABCDEF", 2)]
    assert queued(worker) == []


async def test_disjoint_merged_runs_both_stay(pg_workers):
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(4, "A", "ABC", 2))
    assert apply_fitted(worker, delta_row(7, "D", "DEF", 5)) is not None
    assert [row[2] for row in held(worker)] == ["ABC", "DEF"]
    assert queued(worker) == []


async def test_unstamped_merge_keeps_the_known_start_of_its_run(pg_workers):
    # S6: stamped ABC from 2, then an older worker's ABCDEF with the same head.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(4, "A", "ABC", 2))
    apply_fitted(worker, delta_row(7, "A", "ABCDEF"))
    assert held(worker) == [(7, "A", "ABCDEF", 2)]
    for seq, char in enumerate("ABCDEF", 2):
        assert apply_fitted(worker, delta_row(seq, char, char)) is None
    assert held(worker) == [(7, "A", "ABCDEF", 2)]
    assert queued(worker) == []


async def test_first_fragment_widens_a_start_that_was_inferred_too_late(pg_workers):
    # ABC at 4 was merged before runs named their start, so 4 is all we could say.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(4, "A", "ABC"))
    apply_fitted(worker, delta_row(7, "A", "ABCDEF"))
    assert held(worker) == [(7, "A", "ABCDEF", 4)]
    assert apply_fitted(worker, delta_row(2, "A", "A")) is None
    assert held(worker) == [(7, "A", "ABCDEF", 2)]
    for seq, char in zip((3, 4, 5, 6, 7), "BCDEF"):
        assert apply_fitted(worker, delta_row(seq, char, char)) is None
    assert held(worker) == [(7, "A", "ABCDEF", 2)]


async def test_merged_row_of_unknown_start_does_not_replace_held_fragments(pg_workers):
    # Held D/E/F and no A: nothing says where the unstamped merged row starts.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(5, "D", "D"), delta_row(6, "E", "E"), delta_row(7, "F", "F"))
    assert apply_fitted(worker, delta_row(7, "A", "ABCDEF")) is None
    assert [row[2] for row in held(worker)] == ["D", "E", "F"]
    assert queued(worker) == ["chat"]


async def test_fragments_written_beneath_an_unstamped_merged_row_are_queued(pg_workers):
    # A copy captured mid-run got the merged row, then only D/E/F as operations.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(7, "A", "ABCDEF"))
    apply_fitted(worker, delta_row(5, "D", "D"))
    apply_fitted(worker, delta_row(6, "E", "E"))
    assert queued(worker) == []
    assert apply_fitted(worker, delta_row(7, "F", "F")) is None
    assert queued(worker) == ["chat"]


async def test_owner_log_replaces_a_queued_transcript_and_resets_readers(pg_workers, monkeypatch):
    source, peer, served = await merged_source(pg_workers, 3)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (source.device_id,))
    plant(peer, delta_row(4, "held-head", "ABC", 2))
    peer.sql("DELETE FROM sync_records WHERE collection = 'session_events' AND record_id <> 'chat|1'")
    peer.sql("INSERT INTO network_transcript_repairs (session_id, reason) VALUES ('chat', 'test')")
    monkeypatch.setattr(transcripts, "_link", lambda device_id: transport.PeerLink(device_id, [served.base_url]))
    with peer.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transcripts.repair_queued(client)
    assert [(r["seq"], r["id"], r["payload"]) for r in transcript(peer)] == [
        (r["seq"], r["id"], r["payload"]) for r in transcript(source)]
    assert queued(peer) == []
    counter = peer.sql("SELECT next_seq, rewrite_token FROM session_event_counters WHERE session_id = 'chat'")[0]
    assert counter["rewrite_token"] and counter["next_seq"] >= transcript(peer)[-1]["seq"]


async def test_unreachable_owner_keeps_the_held_rows_and_does_not_raise(pg_workers, monkeypatch):
    source, peer, served = await merged_source(pg_workers, 3)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (source.device_id,))
    plant(peer, delta_row(4, "held-head", "ABC", 2))
    peer.sql("INSERT INTO network_transcript_repairs (session_id, reason) VALUES ('chat', 'test')")
    monkeypatch.setattr(transcripts, "_link", lambda device_id: transport.PeerLink(device_id, []))
    with peer.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transcripts.repair_queued(client)
    assert held(peer) == [(4, "held-head", "ABC", 2)]
    assert queued(peer) == ["chat"]
    entry = peer.sql("SELECT attempts, not_before FROM network_transcript_repairs WHERE session_id = 'chat'")[0]
    assert entry["attempts"] == 1 and entry["not_before"] > time.time()


async def test_a_session_that_changed_owner_is_not_installed_over(pg_workers):
    source, peer, _served = await merged_source(pg_workers, 3)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", ("someone-else",))
    plant(peer, delta_row(4, "held-head", "ABC", 2))
    peer.sql("INSERT INTO network_transcript_repairs (session_id, reason) VALUES ('chat', 'test')")
    items = copy_items(source, after=0, held=8)
    with peer.active():
        assert transcripts.install_owner_log("chat", source.device_id, items, 8, engine.context()) is None
    assert held(peer) == [(4, "held-head", "ABC", 2)]


def test_a_run_the_owner_merged_between_two_pages_is_not_installed_twice():
    def item(seq, text, first=None):
        return {"record_id": f"chat|{seq}", "deleted": False, "record": {**delta_row(seq, text, text, first)}}
    kept = transcripts._without_merged_duplicates(
        [item(2, "A"), item(3, "B"), item(7, "ABCDEF", 2), item(9, "G")])
    assert [row["record"]["seq"] for row in kept] == [7, 9]


def test_repeated_text_does_not_prove_an_unstamped_held_run(pg_workers):
    # Held "ha"x3 really covers 2-4; the incoming "ha"x4 covers 4-7. One sits
    # inside the other, but that says nothing: the first is not part of the second.
    worker = lone_worker(pg_workers)
    with worker.active():
        events_db.append_events("chat", [{"type": "assistant.text_delta", "payload": {"text": "ha"}} for _ in range(3)])
        originals = events_db.get_events("chat")[1:]
        events_db.compact_session_deltas("chat", keep_tail_events=0)
    worker.sql("UPDATE session_events SET payload = payload - 'merged_from_seq' WHERE seq = 4")
    before = held(worker)
    apply_fitted(worker, delta_row(7, originals[2]["id"], "ha" * 4, 4))
    assert held(worker) == before
    assert queued(worker) == ["chat"]


async def test_a_fetched_log_never_replaces_a_clear_that_synced_meanwhile(pg_workers):
    source, peer, served = await merged_source(pg_workers, 3)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (source.device_id,))
    peer.sql("INSERT INTO network_transcript_repairs (session_id, reason) VALUES ('chat', 'test')")
    fetched = copy_items(source, after=0, held=8)
    with source.active():
        sessions.clear_session_conversation("chat")
        catalog.publish_owned()
    await sync(peer, served)
    cleared = transcript(peer)
    assert len(cleared) == len(transcript(source))
    with peer.active():
        assert transcripts.install_owner_log("chat", source.device_id, fetched, 8, engine.context()) is None
    assert transcript(peer) == cleared
    assert queued(peer) == ["chat"]  # Fetched again, from the log as it is now.


async def test_a_hand_off_that_won_the_counter_is_not_replaced_by_the_repair(pg_workers):
    # Hand-off holds the counter, publishes and takes ownership without network_state.
    source, peer, _served = await merged_source(pg_workers, 3)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (source.device_id,))
    peer.sql("INSERT INTO network_transcript_repairs (session_id, reason) VALUES ('chat', 'test')")
    items = copy_items(source, after=0, held=8)
    entered = threading.Event()

    @contextmanager
    def hooked_db():
        with base.get_db() as conn:
            class Cursor:
                def __init__(self):
                    self.real = conn.cursor()

                def execute(self, sql, params=()):
                    if sql.startswith("INSERT INTO session_event_counters"):
                        entered.set()
                    return self.real.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self.real, name)

            class Conn:
                def cursor(self):
                    return Cursor()
            yield Conn()

    with peer.active():
        hand = peer.connect()
        cur = hand.cursor()
        cur.execute("SELECT * FROM session_event_counters WHERE session_id = 'chat' FOR UPDATE")
        cur.execute("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (peer.device_id,))
        cur.execute("UPDATE session_events SET payload = %s::jsonb WHERE session_id = 'chat' AND seq = 4",
                    (json.dumps({"text": "LOCAL-AUTHORITATIVE"}),))
        with patch.object(transcripts, "get_db", hooked_db):
            install = asyncio.create_task(asyncio.to_thread(
                transcripts.install_owner_log, "chat", source.device_id, items, 8, engine.context()))
            assert await asyncio.to_thread(entered.wait, 5)
            hand.commit()
            hand.close()
            assert await install is None
    row = peer.sql("SELECT payload FROM session_events WHERE session_id = 'chat' AND seq = 4")[0]
    assert row["payload"] == {"text": "LOCAL-AUTHORITATIVE"}


async def mirror_queued_with_a_fragment_missing(pg_workers):
    """An idle mirror holding A,C,D,E,F of a run whose owner holds ABCDEF, queued for repair."""
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Merged")
    publish(source, 1, create=True)
    admit(source, peer)
    deltas(source, 6, finish=True)
    await sync(peer, ServedBy(source, "https://source.example.com"))
    peer.sql("DELETE FROM session_events WHERE session_id = 'chat' AND seq = 3")
    with source.active():
        events_db.compact_session_deltas("chat", keep_tail_events=0)
    items = copy_items(source, after=0, held=8)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (source.device_id,))
    assert apply_fitted(peer, next(item["record"] for item in items if item["record"]["seq"] == 7)) is None
    assert queued(peer) == ["chat"]
    return source, peer, items


async def test_compaction_that_read_the_damaged_rows_cannot_undo_the_repair(pg_workers):
    # S11: compaction writes back what it read, so a repair committing between
    # its read and its write would be overwritten with ACDEF, nothing queued.
    source, peer, items = await mirror_queued_with_a_fragment_missing(pg_workers)
    has_read, resume = threading.Event(), threading.Event()

    def compact():
        with base.get_db() as conn:
            class Cursor:
                def __init__(self):
                    self.real, self.sql = conn.cursor(), ""

                def execute(self, sql, params=()):
                    self.sql = sql
                    return self.real.execute(sql, params)

                def fetchall(self):
                    rows = self.real.fetchall()
                    if self.sql.startswith("SELECT id, seq, payload, command_id, created_at FROM session_events"):
                        has_read.set()
                        assert resume.wait(20)
                    return rows

                def __getattr__(self, name):
                    return getattr(self.real, name)

            return events_db.compact_session_deltas("chat", keep_tail_events=0, cursor=Cursor())

    with peer.active():
        compacting = asyncio.create_task(asyncio.to_thread(compact))
        try:
            assert await asyncio.to_thread(has_read.wait, 5)
            install = asyncio.create_task(asyncio.to_thread(
                transcripts.install_owner_log, "chat", source.device_id, items, 8, engine.context()))
            done, _ = await asyncio.wait({install}, timeout=1.5)
            assert not done, "the repair must wait behind a compaction that has read the rows"
        finally:
            resume.set()
        assert (await compacting)[0] == 1
        assert await install is not None
    assert frames(peer) == frames(source)
    assert queued(peer) == []


async def test_compaction_skips_a_session_whose_counter_is_held(pg_workers):
    # Maintenance never queues behind a live turn or a long hand-off import.
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(2, "a", "a"), delta_row(3, "b", "b"))
    worker.sql("UPDATE session_event_counters SET next_seq = 3 WHERE session_id = 'chat'")
    holder = worker.connect()
    holder.cursor().execute("SELECT 1 FROM session_event_counters WHERE session_id = 'chat' FOR UPDATE")
    with worker.active():
        try:
            started = time.monotonic()
            assert await asyncio.to_thread(events_db.compact_session_deltas, "chat", keep_tail_events=0) == (0, 0)
            assert time.monotonic() - started < 2
            assert [row[0] for row in held(worker)] == [2, 3]
        finally:
            holder.rollback()
            holder.close()
        assert await asyncio.to_thread(events_db.compact_session_deltas, "chat", keep_tail_events=0) == (1, 1)
    assert [row[:3] for row in held(worker)] == [(3, "a", "ab")]


async def test_fast_append_reserves_the_counter_before_judging_the_held_rows(pg_workers, monkeypatch):
    # The same shape as compaction on the sync side: a fragment judged not to
    # need fitting must not land beneath a run merged after that judgement.
    source, peer, _served = await merged_source_unmerged(pg_workers)
    judged = threading.Event()
    real = events_db.delta_rows_need_fitting

    def spy(cur, records):
        judged.set()
        return real(cur, records)

    monkeypatch.setattr(events_db, "delta_rows_need_fitting", spy)
    holder = peer.connect()
    holder.cursor().execute("SELECT 1 FROM session_event_counters WHERE session_id = 'chat' FOR UPDATE")
    with peer.active():
        applying = asyncio.create_task(asyncio.to_thread(engine.apply_batch, ops_for(source, [4, 5])))
        try:
            assert not await asyncio.to_thread(judged.wait, 1.5), "judged the held rows outside the fence"
        finally:
            holder.rollback()
            holder.close()
        applied = await applying
    assert judged.is_set()
    assert applied.rejected == applied.pending == applied.retry == 0
    assert frames(peer) == frames(source)


async def test_a_row_refused_during_the_fetch_makes_the_repair_fetch_again(pg_workers):
    source, peer, _served = await merged_source(pg_workers, 3)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (source.device_id,))
    plant(peer, delta_row(4, "held-head", "ABC", 2))
    peer.sql("DELETE FROM sync_records WHERE collection = 'session_events' AND record_id <> 'chat|1'")
    peer.sql("INSERT INTO network_transcript_repairs (session_id, reason, token) VALUES ('chat', 'test', 'before')")
    items = copy_items(source, after=0, held=8)
    peer.sql("UPDATE network_transcript_repairs SET token = 'after' WHERE session_id = 'chat'")
    with peer.active():
        assert transcripts.install_owner_log(
            "chat", source.device_id, items, 8, engine.context(), token="before") is None
        assert held(peer) == [(4, "held-head", "ABC", 2)]
        assert transcripts.install_owner_log(
            "chat", source.device_id, items, 8, engine.context(), token="after") is not None
    assert queued(peer) == []


async def test_resume_at_a_cursor_that_survived_the_repair_is_reset(pg_workers):
    source, peer, _served = await merged_source(pg_workers, 3)
    peer.sql("UPDATE sessions SET owner_device_id = %s WHERE id = 'chat'", (source.device_id,))
    with peer.active():
        events_db.append_events("chat", [{"type": "message.assistant", "payload": {"content": "done"}}])
    peer.sql("UPDATE session_events SET seq = 8, id = %s WHERE session_id = 'chat' AND seq = 5",
             (transcript(source)[-1]["id"],))
    peer.sql("DELETE FROM sync_records WHERE collection = 'session_events' AND record_id <> 'chat|1'")
    peer.sql("UPDATE session_event_counters SET next_seq = 8 WHERE session_id = 'chat'")
    peer.sql("INSERT INTO network_transcript_repairs (session_id, reason) VALUES ('chat', 'test')")
    items = copy_items(source, after=0, held=8)
    with peer.active():
        old_epoch = events_db.get_reader_epoch("chat")
        assert events_db.has_event_at("chat", 8)
        assert transcripts.install_owner_log("chat", source.device_id, items, 8, engine.context()) == old_epoch
        assert events_db.has_event_at("chat", 8)
        current, reason = _resume_check("chat", 8, old_epoch)
        assert reason == "epoch" and current != old_epoch
        assert _resume_check("chat", 8, current) == (current, None)
        # Workers still compare the plain first-row id with each other.
        assert events_db.get_watermark("chat")["epoch"] == old_epoch


async def test_failing_repairs_rotate_and_do_not_hide_a_reachable_owner(pg_workers, monkeypatch):
    peer = lone_worker(pg_workers)
    sessions_queued = [f"fail-{index:03d}" for index in range(100)] + ["zz-reachable"]
    for session_id in sessions_queued:
        peer.sql("INSERT INTO network_transcript_repairs (session_id, reason) VALUES (%s, 'test')", (session_id,))
    tried: list = []

    async def fake_repair(client, ctx, session_id):
        tried.append(session_id)
        if session_id != "zz-reachable":
            raise ValueError("owner offline")
        peer.sql("DELETE FROM network_transcript_repairs WHERE session_id = %s", (session_id,))

    monkeypatch.setattr(transcripts, "_repair_session", fake_repair)
    with peer.active():
        passes = len(sessions_queued) // transcripts.REPAIR_SESSIONS + 1
        for _ in range(passes):
            await transcripts.repair_queued(None)
    assert set(tried) == set(sessions_queued)
    assert len(tried) == len(sessions_queued)  # A deferred entry is not retried ahead of its turn.
    assert "zz-reachable" not in queued(peer)


def test_a_hand_off_import_resets_readers_only_when_it_rewrites_beneath_them(pg_workers):
    worker = lone_worker(pg_workers)
    plant(worker, delta_row(8, "terminal", "end"))
    first = transcript(worker)[0]["id"]
    unchanged = [(first, 1), ("terminal", 8)]
    above = [*unchanged, ("newer", 9)]
    beneath = [(first, 1), ("filled", 4), ("terminal", 8)]
    with worker.active():
        epoch = events_db.get_reader_epoch("chat")
        for snapshot in (unchanged, above):
            with get_db() as conn:
                handoff._reconcile_app_rows(conn.cursor(), "chat", "session_events", snapshot)
            assert events_db.get_reader_epoch("chat") == epoch
        with get_db() as conn:
            handoff._reconcile_app_rows(conn.cursor(), "chat", "session_events", beneath)
        rewritten = events_db.get_reader_epoch("chat")
        assert rewritten != epoch
        assert events_db.get_log_epoch("chat") == first
        # A rewind leaves nothing above the survivors: a cursor on the removed
        # row is already reset for its row being gone.
        with get_db() as conn:
            handoff._reconcile_app_rows(conn.cursor(), "chat", "session_events", [(first, 1)])
        assert events_db.get_reader_epoch("chat") == rewritten
