# ruff: noqa: F811
"""Envelopes of transcript fragments merged by idle delta compaction retire (#1328).

Idle delta compaction keeps one row per run, at the run's last seq, naming the
run's first seq in ``merged_from_seq``, and deletes the other fragments without
tombstones. Their sync records stay live with no row. The transcript retention
pass now retires such a fragment's envelope when the next row of its session is
a delta with a valid marker reaching down to it and a live record newer than
the fragment's, under the same peer-frontier, age and stream-head rules as any
other transcript envelope. A missing row alone is never proof.

Real PostgreSQL, independent worker schemas, the real event, catalog and
compaction producers, signed exchange through the actual peer router and the
scheduled maintenance loop. Never the configured application database.
"""

import pytest

from app.db import network as store
from app.db import session_events as events_db
from app.network import catalog, engine, transport
from tests.core.network.test_merged_run_recovery_postgres import (
    WHOLE,
    expire,
    fill,
    frames,
)
from tests.core.network.test_sync_log_bounds_postgres import (
    age_ops,
    last_direct_contact,
)
from tests.core.network.test_transcript_op_retention_postgres import (  # noqa: F401
    admit,
    cluster,
    old_ops,
    pg_workers,
    protected_rows,
    prune_once,
    publish,
    sync,
    transcript,
)
from tests.core.network.test_transport import ServedBy

FRAGMENTS = [f"chat|{seq}" for seq in range(2, 7)]  # merged into the survivor at seq 7
REPLY = [("message.user", "message 1"), ("assistant.text_delta", WHOLE), ("message.assistant", "done")]


def stream(worker, kind, count, *, start=0, finish=False):
    """A streamed answer as the turn recorder saves it, published by the catalog."""
    with worker.active():
        events_db.append_events("chat", [
            {"type": kind, "payload": {"text": f"part {n} "}} for n in range(start, start + count)])
        if finish:
            events_db.append_events("chat", [{"type": "message.assistant", "payload": {"content": "done"}}])
        catalog.publish_owned()


def compact(worker, **kwargs):
    with worker.active():
        return events_db.compact_session_deltas("chat", **kwargs)


def network(name, pg_workers):
    worker = pg_workers(name)
    with worker.active():
        engine.create_network("Merged retention")
    publish(worker, 1, create=True)
    return worker


def merged_chat(pg_workers):
    """Message 1, a six-fragment reply merged into seq 7, ``done`` at 8, a later message at 9."""
    source = network("source", pg_workers)
    stream(source, "assistant.text_delta", 6, finish=True)
    assert compact(source, keep_tail_events=0) == (1, 5)
    publish(source, 1)
    assert [row["seq"] for row in transcript(source)] == [1, 7, 8, 9]
    return source


def event_ops(worker):
    return [row["record_id"] for row in worker.sql(
        "SELECT record_id FROM sync_ops WHERE collection = 'session_events' ORDER BY seq")]


def stream_head(worker):
    return worker.sql("SELECT collection, record_id FROM sync_ops WHERE origin_device_id = %s "
                      "ORDER BY seq DESC LIMIT 1", (worker.device_id,))[0]


def prune(worker):
    with worker.active():
        return store.prune_transcript_ops(worker.device_id, {})


def survivor_record(worker):
    return worker.sql("SELECT deleted, clock FROM sync_records WHERE collection = 'session_events' "
                      "AND record_id = 'chat|7'")


async def test_fragments_of_a_marked_merged_run_retire(pg_workers, monkeypatch):
    source = merged_chat(pg_workers)
    old_ops(source)
    shown = frames(source)
    assert shown == [*REPLY, ("message.user", "message 9")]
    content = protected_rows(source)
    before = event_ops(source)
    assert set(FRAGMENTS) <= set(before)  # live records, no rows: kept forever on main
    head = stream_head(source)

    await prune_once(source, monkeypatch)

    after = event_ops(source)
    assert not set(FRAGMENTS) & set(after)
    assert after == ([head["record_id"]] if head["collection"] == "session_events" else [])
    assert stream_head(source) == head  # the stream head is never deleted
    assert protected_rows(source) == content  # rows and sync_records untouched
    survivor = next(row for row in transcript(source) if row["seq"] == 7)
    assert survivor["payload"]["text"] == WHOLE and survivor["payload"][events_db.MERGED_FROM_KEY] == 2
    assert [row["deleted"] for row in survivor_record(source)] == [False]
    assert frames(source) == shown
    # Bounded: a second pass finds nothing more to delete.
    assert prune(source) == 0
    assert event_ops(source) == after


def test_retired_count_is_exact_and_a_second_pass_deletes_nothing(pg_workers):
    source = merged_chat(pg_workers)
    old_ops(source)
    before = event_ops(source)
    deleted = prune(source)
    assert deleted == len(before) - len(event_ops(source))
    assert deleted >= len(FRAGMENTS) + 3  # the five fragments plus the three rows still held
    assert prune(source) == 0


async def test_lagging_recent_peer_pins_the_fragments_until_it_catches_up(pg_workers, monkeypatch):
    source = network("source", pg_workers)
    peer = pg_workers("peer")
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    await sync(peer, served)
    await sync(peer, served)  # the peer holds and acknowledges message 1 only
    stream(source, "assistant.text_delta", 6, finish=True)
    assert compact(source, keep_tail_events=0) == (1, 5)
    publish(source, 1)
    old_ops(source)
    await prune_once(source, monkeypatch)
    assert set(FRAGMENTS) <= set(event_ops(source))  # old-peer catch-up is unchanged
    # Ordinary op catch-up, no state copy; the peer then shows the same reply.
    served.requests.clear()
    assert not (await sync(peer, served)).get("snapshot_required")
    assert not any(path.endswith("/state") for _, path in served.requests)
    assert frames(peer) == frames(source)
    await sync(peer, served)  # advertise the new frontier
    await prune_once(source, monkeypatch)
    assert not set(FRAGMENTS) & set(event_ops(source))


async def test_above_an_expired_members_frontier_the_30_day_age_applies(pg_workers, monkeypatch):
    source = network("source", pg_workers)
    peer = pg_workers("peer")
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    await sync(peer, served)
    await sync(peer, served)
    acked = {origin: dict(streams) for origin, streams in transport._peer_watermarks[peer.device_id].items()}
    stream(source, "assistant.text_delta", 6, finish=True)
    assert compact(source, keep_tail_events=0) == (1, 5)
    publish(source, 1)
    # Past its contact grace, still a member, acknowledged only message 1.
    last_direct_contact(source, peer, 31, acked)
    age_ops(source, 2)
    await prune_once(source, monkeypatch)
    assert set(FRAGMENTS) <= set(event_ops(source))
    last_direct_contact(source, peer, 31, acked)
    age_ops(source, 40 * 24)
    await prune_once(source, monkeypatch)
    assert not set(FRAGMENTS) & set(event_ops(source))


async def test_state_copy_and_transcript_backfill_still_deliver_the_merged_text(pg_workers, monkeypatch):
    source = network("source", pg_workers)
    peer = pg_workers("peer")
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    await sync(peer, served)
    stream(source, "assistant.text_delta", 6, finish=True)
    assert compact(source, keep_tail_events=0) == (1, 5)
    expire(source, peer)
    await prune_once(source, monkeypatch)
    assert not set(FRAGMENTS) & set(event_ops(source))
    served.requests.clear()
    assert (await sync(peer, served))["snapshot_required"]
    await fill(peer, served, monkeypatch)
    assert any(path.endswith("/state") for _, path in served.requests)
    assert any(path.endswith("/transcripts") for _, path in served.requests)
    assert frames(peer) == frames(source) == REPLY
    assert transcript(peer) == transcript(source)
    # Later writes still flow by ops.
    publish(source, 1)
    assert not (await sync(peer, served)).get("snapshot_required")
    assert frames(peer) == frames(source)


def test_a_run_merged_in_two_steps_retires_every_fragment(pg_workers):
    source = network("source", pg_workers)
    stream(source, "assistant.text_delta", 6, finish=True)
    # The retention ceiling splits the run: 2-4 merge into 4, 5-7 stay fragments.
    assert compact(source, keep_tail_events=4) == (1, 2)
    assert [row["seq"] for row in transcript(source)] == [1, 4, 5, 6, 7, 8]
    publish(source, 1)
    shown = frames(source)
    old_ops(source)
    prune(source)
    held = event_ops(source)
    assert not {"chat|2", "chat|3"} & set(held)  # covered by the survivor at 4
    assert frames(source) == shown  # survivor + remainder replay as one frame
    # A second step merges the survivor again into 7; it keeps the first seq.
    source2 = network("source2", pg_workers)
    stream(source2, "assistant.text_delta", 6, finish=True)
    assert compact(source2, keep_tail_events=4) == (1, 2)
    assert compact(source2, keep_tail_events=0) == (1, 3)
    survivor = next(row for row in transcript(source2) if row["seq"] == 7)
    assert survivor["payload"][events_db.MERGED_FROM_KEY] == 2
    publish(source2, 1)
    old_ops(source2)
    prune(source2)
    assert not set(FRAGMENTS) & set(event_ops(source2))
    assert frames(source2) == [*REPLY, ("message.user", "message 9")]


def test_a_neighbouring_run_that_does_not_cover_the_gap_is_no_proof(pg_workers):
    source = network("source", pg_workers)
    stream(source, "assistant.thinking_delta", 3)  # seqs 2-4
    stream(source, "assistant.text_delta", 3, finish=True)  # seqs 5-7, done at 8
    assert compact(source, keep_tail_events=0) == (2, 4)
    assert [row["seq"] for row in transcript(source)] == [1, 4, 7, 8]
    # The thinking survivor goes for another reason: its run now has no row,
    # and the next row is the text run's merged row, which starts above it.
    source.sql("DELETE FROM session_events WHERE session_id = 'chat' AND seq = 4")
    publish(source, 1)
    old_ops(source)
    prune(source)
    held = set(event_ops(source))
    assert {"chat|2", "chat|3", "chat|4"} <= held
    assert not {"chat|5", "chat|6"} & held  # covered by the text survivor at 7


NO_PROOF = {
    "legacy unmarked survivor": "UPDATE session_events SET payload = payload - 'merged_from_seq' "
                                "WHERE session_id = 'chat' AND seq = 7",
    "next row not a delta": "DELETE FROM session_events WHERE session_id = 'chat' AND seq = 7",
    "no later row": "DELETE FROM session_events WHERE session_id = 'chat' AND seq >= 7",
    "session deleted": "DELETE FROM sessions WHERE id = 'chat'",
    "survivor record missing": "DELETE FROM sync_records WHERE collection = 'session_events' "
                               "AND record_id = 'chat|7'",
    "survivor record deleted": "UPDATE sync_records SET deleted = TRUE WHERE collection = 'session_events' "
                               "AND record_id = 'chat|7'",
    "survivor record not newer": "UPDATE sync_records SET clock = (SELECT clock FROM sync_records "
                                 "WHERE collection = 'session_events' AND record_id = 'chat|2') "
                                 "WHERE collection = 'session_events' AND record_id = 'chat|7'",
    "survivor payload not an object": "UPDATE session_events SET payload = '[2]'::jsonb "
                                      "WHERE session_id = 'chat' AND seq = 7",
}


@pytest.mark.parametrize("damage", list(NO_PROOF), ids=list(NO_PROOF))
def test_a_missing_row_without_a_covering_merged_row_stays(pg_workers, damage):
    source = merged_chat(pg_workers)
    source.sql(NO_PROOF[damage])
    old_ops(source)
    prune(source)
    assert set(FRAGMENTS) <= set(event_ops(source))
    assert prune(source) == 0


@pytest.mark.parametrize("marker, proves", [
    ("2", True),  # the control: the real value
    ('"2"', False), ("true", False), ("2.0", False), ("2.5", False), ("null", False),
    ("0", False), ("-1", False), ("7", False), ("8", False), ("1e400", False), ('{"seq": 2}', False),
])
def test_only_a_valid_integer_marker_is_proof(pg_workers, marker, proves):
    source = merged_chat(pg_workers)
    source.sql("UPDATE session_events SET payload = jsonb_set(payload, '{merged_from_seq}', %s::jsonb) "
               "WHERE session_id = 'chat' AND seq = 7", (marker,))
    row = next(row for row in transcript(source) if row["seq"] == 7)
    assert (events_db._merged_from(row) is not None) == proves  # the same rule as the mirrors
    old_ops(source)
    prune(source)
    retired = not set(FRAGMENTS) & set(event_ops(source))
    kept = set(FRAGMENTS) <= set(event_ops(source))
    assert (retired, kept) == (proves, not proves)


def test_a_marker_reaching_only_part_of_the_run_retires_only_that_part(pg_workers):
    source = merged_chat(pg_workers)
    source.sql("UPDATE session_events SET payload = jsonb_set(payload, '{merged_from_seq}', '5') "
               "WHERE session_id = 'chat' AND seq = 7")
    old_ops(source)
    prune(source)
    held = set(event_ops(source))
    assert {"chat|2", "chat|3", "chat|4"} <= held
    assert not {"chat|5", "chat|6"} & held
    assert transcript(source)[1]["payload"]["text"] == WHOLE
