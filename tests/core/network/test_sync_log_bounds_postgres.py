# ruff: noqa: F811
"""The sync log keeps acknowledged transcript and session ops for hours, not 30 days (#1167).

Real PostgreSQL, independent worker schemas, the real event and catalog
producers, signed exchange through the actual peer router and the scheduled
maintenance loop. Never the configured application database.
"""
import time

import httpx
import psycopg2.errors
import pytest

from app.db import database
from app.db import network as store
from app.db import session_events as events_db
from app.network import catalog, engine, transcripts, transport
from tests.core.network.test_transcript_op_retention_postgres import (  # noqa: F401
    admit,
    cluster,
    op_count,
    pg_workers,
    protected_rows,
    prune_once,
    publish,
    sync,
    transcript,
)
from tests.core.network.test_transport import ServedBy


def age_ops(worker, hours):
    """Make every logged op look ``hours`` old: older than the new floor, far below 30 days."""
    worker.sql("UPDATE sync_ops SET created_at = NOW() - make_interval(hours => %s)", (hours,))


def ops_of(worker, collection):
    return worker.sql("SELECT record_id, seq, pg_column_size(envelope) AS bytes FROM sync_ops "
                      "WHERE collection = %s ORDER BY ingest_id", (collection,))


def report(label, worker):
    rows = worker.sql("SELECT collection, COUNT(*) AS n, SUM(pg_column_size(envelope)) AS bytes "
                      "FROM sync_ops GROUP BY collection ORDER BY collection")
    print(f"sync_log_bounds {label} " + " ".join(f"{r['collection']}={r['n']}/{r['bytes']}B" for r in rows))
    return {r["collection"]: r["n"] for r in rows}


def stream_answer(worker, chunks):
    """A streamed answer as the turn recorder saves it: one delta row per flush, then the message."""
    with worker.active():
        events_db.append_events("chat", [
            {"type": "assistant.text_delta", "payload": {"text": f"chunk {n} "}} for n in range(chunks)])
        events_db.append_events("chat", [{"type": "message.assistant", "payload": {"content": "done"}}])
        catalog.publish_owned()


def retitle(worker, n):
    with worker.active():
        database.update_session("chat", title=f"Chat {n}")
        catalog.publish_owned()


async def test_acknowledged_transcript_and_session_ops_drop_within_hours(pg_workers, monkeypatch):
    """A busy chat's log shrinks to its anchors once both peers hold it; the offline one still catches up."""
    source, online, offline = (pg_workers(name) for name in ("source", "online", "offline"))
    with source.active():
        engine.create_network("Bounds")
    publish(source, 3, create=True)
    admit(source, online)
    admit(source, offline)
    served = ServedBy(source, "https://source.example.com")
    await sync(online, served)
    await sync(offline, served)
    offline_prefix = len(transcript(offline))

    # A realistic slice of work: several streamed answers and catalog updates.
    for n in range(8):
        stream_answer(source, 20)
        retitle(source, n)
    await sync(online, served)
    await sync(online, served)  # the next request advertises the committed frontier
    age_ops(source, 2)
    before = report("before", source)
    assert before["session_events"] > 160 and before["sessions"] >= 9
    content = protected_rows(source)

    # The offline peer is inside its contact grace: it pins everything it lacks.
    await prune_once(source, monkeypatch)
    pinned = report("offline-pinned", source)
    assert pinned["session_events"] >= before["session_events"] - offline_prefix
    served.requests.clear()
    caught_up = await sync(offline, served)
    assert not caught_up.get("snapshot_required")  # ordinary op catch-up, no state copy
    assert not any(path.endswith("/state") for _, path in served.requests)
    assert transcript(offline) == transcript(source)
    await sync(offline, served)  # advertise the new frontier

    # Every recent peer now holds the log: a 2-hour-old prefix is no longer kept for 30 days.
    await prune_once(source, monkeypatch)
    after = report("after", source)
    assert after.get("session_events", 0) <= 1  # at most the stream head anchor
    assert after["sessions"] == 1  # the newest catalog version of the one chat
    assert protected_rows(source) == content  # rows and sync_records untouched

    # Later writes still flow by ops to both peers.
    served.requests.clear()
    stream_answer(source, 3)
    retitle(source, 99)
    for peer in (online, offline):
        assert not (await sync(peer, served)).get("snapshot_required")
        assert transcript(peer) == transcript(source)
        assert peer.sql("SELECT title FROM sessions WHERE id = 'chat'") == [{"title": "Chat 99"}]
    assert not any(path.endswith("/state") for _, path in served.requests)


async def test_fresh_ops_stay_and_floor_is_configurable(pg_workers, monkeypatch):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Bounds")
    publish(source, 6, create=True)
    await prune_once(source, monkeypatch)
    assert op_count(source) == 6  # younger than the 1-hour floor
    age_ops(source, 2)
    monkeypatch.setenv(store.TRANSCRIPT_OP_MIN_AGE_HOURS_ENV, "3")
    await prune_once(source, monkeypatch)
    assert op_count(source) == 6
    monkeypatch.setenv(store.TRANSCRIPT_OP_MIN_AGE_HOURS_ENV, "not-a-number")
    await prune_once(source, monkeypatch)
    assert op_count(source) == 6  # misconfiguration fails closed
    monkeypatch.delenv(store.TRANSCRIPT_OP_MIN_AGE_HOURS_ENV)
    await prune_once(source, monkeypatch)
    assert op_count(source) == 1


def deltas(worker, count, *, finish=False):
    with worker.active():
        events_db.append_events("chat", [
            {"type": "assistant.text_delta", "payload": {"text": f"part {n} "}} for n in range(count)])
        if finish:
            events_db.append_events("chat", [{"type": "message.assistant", "payload": {"content": "done"}}])
        catalog.publish_owned()


@pytest.mark.xfail(strict=True, raises=psycopg2.errors.UniqueViolation,
                   reason="issue 1278: the merged row collides with the held first fragment")
async def test_expired_peer_holding_part_of_a_merged_run_recovers(pg_workers, monkeypatch):
    """The peer holds a run's first fragments; the owner later merged the run into one row.

    The merged row reuses the first fragment's event id, so today the transcript
    fill collides with the fragment the peer holds (issue 1278). Any fix must
    also cover ordinary sync interleaving and connected readers; see the issue.
    """
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Bounds")
    publish(source, 1, create=True)
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    deltas(source, 3)
    await sync(peer, served)
    assert len(transcript(peer)) == 4  # the message and the run's first three fragments
    deltas(source, 3, finish=True)
    with source.active():
        assert events_db.compact_session_deltas("chat", keep_tail_events=0) == (1, 5)
    # The peer is unseen for 40 days and the log is pruned below what it holds (as on main).
    source.sql("INSERT INTO sync_peers(device_id, last_contact, updated_at) VALUES (%s, '2000-01-01', '2000-01-01') "
               "ON CONFLICT (device_id) DO UPDATE SET last_contact = EXCLUDED.last_contact, "
               "updated_at = EXCLUDED.updated_at", (peer.device_id,))
    source.sql("UPDATE sync_records SET updated_at = '2000-01-01' WHERE collection = 'presence'")
    transport._peer_watermarks.pop(peer.device_id, None)
    age_ops(source, 40 * 24)
    await prune_once(source, monkeypatch)
    assert (await sync(peer, served))["snapshot_required"]
    monkeypatch.setattr(transcripts, "_link", lambda device_id: transport.PeerLink(
        device_id, [served.base_url] if device_id == source.device_id else []))
    with peer.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            for _ in range(5):
                if not transcripts.pending():
                    break
                await transcripts.run_once(client)
        assert not transcripts.pending()
    assert transcript(peer) == transcript(source)
    assert [row["seq"] for row in transcript(peer)] == [1, 7, 8]
    # Later writes still flow by ops.
    publish(source, 1)
    assert not (await sync(peer, served)).get("snapshot_required")
    assert transcript(peer) == transcript(source)


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e300", "1e12"])
async def test_unrepresentable_floor_skips_only_transcript_retention(pg_workers, monkeypatch, value):
    source = pg_workers("source")
    with source.active():
        engine.create_network("Bounds")
    publish(source, 3, create=True)
    for n in range(3):
        retitle(source, n)
    publish(source, 1)  # a transcript head above the catalog copies
    age_ops(source, 2)
    monkeypatch.setenv(store.TRANSCRIPT_OP_MIN_AGE_HOURS_ENV, value)
    with source.active():
        assert store.prune_transcript_ops(source.device_id, {}) == 0
    await prune_once(source, monkeypatch)
    assert op_count(source) == 4  # transcript retention skipped, fail closed
    assert len(ops_of(source, "sessions")) == 1  # the rest of the maintenance pass still ran


async def test_superseded_session_ops_pin_for_a_lagging_peer_and_honour_kill_switch(pg_workers, monkeypatch):
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Bounds")
    publish(source, 1, create=True)
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    await sync(peer, served)
    await sync(peer, served)
    for n in range(5):
        retitle(source, n)
    held = ops_of(source, "sessions")
    assert len(held) >= 6
    # The peer has not received the retitles: it pins them; only copies it holds may go.
    await prune_once(source, monkeypatch)
    assert ops_of(source, "sessions")[-5:] == held[-5:]
    await sync(peer, served)
    await sync(peer, served)  # advertise the new frontier
    monkeypatch.setenv(store.SESSION_OP_COMPACTION_ENV, "off")
    await prune_once(source, monkeypatch)
    assert ops_of(source, "sessions")[-5:] == held[-5:]
    monkeypatch.delenv(store.SESSION_OP_COMPACTION_ENV)
    await prune_once(source, monkeypatch)
    assert [row["seq"] for row in ops_of(source, "sessions")] == [held[-1]["seq"]]
    assert peer.sql("SELECT title FROM sessions WHERE id = 'chat'") == [{"title": "Chat 4"}]



def head_seq(worker):
    return worker.sql("SELECT COALESCE(MAX(seq), 0) AS s FROM sync_ops WHERE origin_device_id = %s",
                      (worker.device_id,))[0]["s"]


def own_ops(worker, after):
    return worker.sql("SELECT collection, record_id, seq FROM sync_ops WHERE origin_device_id = %s AND seq > %s "
                      "ORDER BY seq", (worker.device_id, after))


def last_direct_contact(source, peer, days, marks):
    """The peer last exchanged with ``source`` directly ``days`` ago and acknowledged ``marks`` then.

    Every other trace of it is that old too: no fresher presence receipt.
    """
    source.sql("INSERT INTO sync_peers(device_id, last_contact, updated_at) "
               "VALUES (%s, NOW() - make_interval(days => %s), NOW() - make_interval(days => %s)) "
               "ON CONFLICT (device_id) DO UPDATE SET last_contact = EXCLUDED.last_contact, "
               "updated_at = EXCLUDED.updated_at", (peer.device_id, days, days))
    source.sql("UPDATE sync_records SET updated_at = NOW() - make_interval(days => %s) "
               "WHERE collection = 'presence' AND record_id = %s", (days, peer.device_id))
    # One process hosts every test worker: restore what ``source`` itself last observed.
    transport._peer_watermarks[peer.device_id] = marks
    transport._peer_watermark_at[peer.device_id] = time.time() - days * 86400


def age_split(source, held, *, old_days, new_hours):
    """Ops the peer held at its last direct contact are that old; everything after is recent."""
    source.sql("UPDATE sync_ops SET created_at = CASE WHEN origin_device_id = %s AND seq <= %s "
               "THEN NOW() - make_interval(days => %s) ELSE NOW() - make_interval(hours => %s) END",
               (source.device_id, held, old_days, new_hours))


async def test_relayed_member_past_its_contact_grace_keeps_what_it_lacks(pg_workers, monkeypatch):
    """B last met A directly 31 days ago but syncs through C; nothing young that B lacks is deleted.

    B's presence fields never change, so A has no fresh trace of it at all.
    """
    a, b, c = (pg_workers(name) for name in ("a", "b", "c"))
    with a.active():
        engine.create_network("Bounds")
    publish(a, 1, create=True)
    admit(a, b)
    admit(a, c)
    served_a, served_c = ServedBy(a, "https://a.example.com"), ServedBy(c, "https://c.example.com")
    await sync(b, served_a)
    await sync(b, served_a)  # B's last direct acknowledgement to A
    held = head_seq(a)
    acked = {origin: dict(streams) for origin, streams in transport._peer_watermarks[b.device_id].items()}
    await sync(c, served_a)
    # Two hours ago: B gets the start of a streamed reply through C, then goes offline.
    deltas(a, 3)
    await sync(c, served_a)
    await sync(b, served_c)
    # A finishes the reply and retitles; C holds and acknowledges everything.
    deltas(a, 3, finish=True)
    retitle(a, 1)
    retitle(a, 2)
    await sync(c, served_a)
    await sync(c, served_a)
    last_direct_contact(a, b, 31, acked)
    age_split(a, held, old_days=31, new_hours=2)
    with b.active():
        b_holds = store.local_watermarks()[a.device_id][str(a.identity.incarnation)]
    assert b_holds > held  # the reply's start, received through C
    young = own_ops(a, held)
    assert sum(op["collection"] == "session_events" for op in young) == 7
    assert sum(op["collection"] == "sessions" for op in young) >= 2

    await prune_once(a, monkeypatch)
    # B never acknowledged these to A, so they keep main's 30 days, the part B holds included.
    assert own_ops(a, held) == young
    served_a.requests.clear()
    assert not (await sync(b, served_a)).get("snapshot_required")
    assert not any(path.endswith("/state") for _, path in served_a.requests)
    assert transcript(b) == transcript(a)
    assert b.sql("SELECT title FROM sessions WHERE id = 'chat'") == [{"title": "Chat 2"}]


async def test_any_unrevoked_member_pins_and_revocation_releases(pg_workers, monkeypatch):
    """A short grace or a long absence does not release a member's ops; only revocation does."""
    source, peer = pg_workers("source"), pg_workers("peer")
    with source.active():
        engine.create_network("Bounds")
    publish(source, 1, create=True)
    admit(source, peer)
    served = ServedBy(source, "https://source.example.com")
    await sync(peer, served)
    await sync(peer, served)
    held = head_seq(source)
    acked = {origin: dict(streams) for origin, streams in transport._peer_watermarks[peer.device_id].items()}
    publish(source, 4)
    retitle(source, 1)
    retitle(source, 2)
    lacking = own_ops(source, held)
    assert sum(op["collection"] == "session_events" for op in lacking) == 4

    # A 1-day grace and a 2-day absence, then 31 days with no trace: still a member, still pinned.
    monkeypatch.setenv(store.PEER_GRACE_DAYS_ENV, "1")
    for days in (2, 31):
        last_direct_contact(source, peer, days, acked)
        age_split(source, held, old_days=days, new_hours=2)
        await prune_once(source, monkeypatch)
        assert own_ops(source, held) == lacking

    # Revoked: the member no longer pins and the early rules apply.
    with source.active():
        engine.revoke_device(peer.device_id)
    age_split(source, held, old_days=31, new_hours=2)
    await prune_once(source, monkeypatch)
    remaining = own_ops(source, held)
    assert sum(op["collection"] == "session_events" for op in remaining) <= 1
    assert sum(op["collection"] == "sessions" for op in remaining) == 1
