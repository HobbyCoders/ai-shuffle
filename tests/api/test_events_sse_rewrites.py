"""Real receiver writes must invalidate a warm SSE reader, not wait for reconnect.

Only the PostgreSQL LISTEN transport is stubbed, as in the other SSE tests.
Rows go through the production mirror materializer and post-mirror notifier;
no test injects a reset frame or calls the reader's reset machinery.
"""
import asyncio
import json
from uuid import uuid4

import pytest

import app.api.events as events_mod
from app.api.events import event_stream
from app.db import database, session_events as se
from app.db.base import get_db
from app.network import catalog, engine, policy
from tests.api.sse_helpers import collect_frames, parse_frame


@pytest.fixture(autouse=True)
def clean_tails():
    yield
    for tail in list(events_mod._tails.values()):
        tail.close()
    events_mod._tails.clear()


class NotifyCursor:
    def __init__(self, cursor, notifications):
        self.cursor, self.notifications = cursor, notifications

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def execute(self, sql, params=()):
        if "pg_notify" in sql:
            self.notifications.append(params[1])
        return self.cursor.execute(sql, params)


def mirror_rows(session_id, count, *, replace):
    """The same materialize/post-mirror path used by applied peer snapshots."""
    notifications = []
    old = se.get_events(session_id)
    if old:
        template = dict(old[0])
    else:
        # Produce a first peer row with the normal writer before mirroring it.
        source = session_id + "-source"
        database.create_session(source, "default", title="Disposable peer source")
        se.append_events(source, [{"type": "message.user", "payload": {"content": "source"}}])
        template = {**se.get_events(source)[0], "session_id": session_id}
    coll = policy.collection("session_events")
    start = 1 if replace or not old else old[-1]["seq"] + 1
    with get_db() as conn:
        cursor = NotifyCursor(conn.cursor(), notifications)
        if replace:
            for row in old:
                engine.materialize(cursor, coll, f"{session_id}|{row['seq']}", None)
        for seq in range(start, start + count):
            row = {**template, "id": str(uuid4()), "seq": seq,
                   "event_type": "message.user", "payload": {"content": f"peer-{seq}"}}
            engine.materialize(cursor, coll, f"{session_id}|{seq}", row)
            catalog.after_event_mirrored(cursor, row)
    return notifications


async def warm_reader(session_id):
    database.create_session(session_id, "default", title="Disposable mirror rewrite")
    se.append_events(session_id, [{"type": "message.user", "payload": {"content": f"old-{n}"}}
                                  for n in range(3)])
    epoch = se.get_log_epoch(session_id)
    # Warm cache: its view already holds seq 3 in this epoch. Freshness is still
    # waiting when the receiver replaces the log after this caught_up marker.
    stream = event_stream(session_id, 3, epoch)
    caught = parse_frame(await anext(stream))
    assert caught == {"event": "caught_up", "data": {"seq": 3, "epoch": epoch}}
    await asyncio.sleep(0.03)
    return stream, epoch


@pytest.mark.parametrize("new_count", [1, 3, 5])
async def test_peer_replacement_after_connect_resets_warm_reader(
    mock_db, gw, monkeypatch, new_count
):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    stream, epoch = await warm_reader("mirror-replaced")
    try:
        notifications = mirror_rows("mirror-replaced", new_count, replace=True)
        for payload in notifications:
            gw._handle_payload(payload)  # Deliver the REAL committed pg_notify payload.
        reset = (await collect_frames(stream, 1, timeout=0.8))[0]
        assert reset == {"event": "reset", "data": {
            "reason": "epoch", "epoch": se.get_log_epoch("mirror-replaced")}}
        assert reset["data"]["epoch"] != epoch
    finally:
        await stream.aclose()
    fresh = event_stream("mirror-replaced", 0)
    try:
        rows = await collect_frames(fresh, new_count)
        assert [row["data"]["content"] for row in rows] == [f"peer-{n}" for n in range(1, new_count + 1)]
    finally:
        await fresh.aclose()


async def test_peer_append_keeps_same_reader_without_reset(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    stream, epoch = await warm_reader("mirror-appended")
    try:
        for payload in mirror_rows("mirror-appended", 1, replace=False):
            gw._handle_payload(payload)
        row = (await collect_frames(stream, 1))[0]
        assert row["event"] == "message.user"
        assert row["id"] == 4
        assert row["data"]["content"] == "peer-4"
        assert se.get_log_epoch("mirror-appended") == epoch
    finally:
        await stream.aclose()


async def test_missed_peer_notification_still_resets_on_reconciliation(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "LIVE_RESYNC_SECONDS", 0)
    stream, epoch = await warm_reader("mirror-no-notify")
    try:
        mirror_rows("mirror-no-notify", 1, replace=True)  # Deliberately lose LISTEN delivery.
        reset = (await collect_frames(stream, 1, timeout=0.8))[0]
        assert reset["event"] == "reset"
        assert reset["data"]["reason"] == "epoch"
        assert reset["data"]["epoch"] != epoch
    finally:
        await stream.aclose()


async def test_first_mirrored_append_is_not_a_rewrite(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    sid = "mirror-first"
    database.create_session(sid, "default", title="New empty mirror")
    stream = event_stream(sid)
    try:
        assert parse_frame(await anext(stream))["event"] == "caught_up"
        for payload in mirror_rows(sid, 1, replace=False):
            gw._handle_payload(payload)
        row = (await collect_frames(stream, 1))[0]
        assert row["event"] == "message.user"  # No reset of a newly composed view.
        assert row["data"]["content"] == "peer-1"
    finally:
        await stream.aclose()


async def test_one_epoch_check_resets_all_attached_clients(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    first, epoch = await warm_reader("mirror-fanout-rewrite")
    streams = [first]
    try:
        for _ in range(2):
            stream = event_stream("mirror-fanout-rewrite", 3, epoch)
            assert parse_frame(await anext(stream))["event"] == "caught_up"
            streams.append(stream)
        epoch_reads = []
        real_epoch = events_mod.get_log_epoch

        def count_epoch(session_id):
            epoch_reads.append(session_id)
            return real_epoch(session_id)

        monkeypatch.setattr(events_mod, "get_log_epoch", count_epoch)
        for payload in mirror_rows("mirror-fanout-rewrite", 1, replace=True):
            gw._handle_payload(payload)
        results = await asyncio.gather(*(collect_frames(stream, 1) for stream in streams))
        assert [result[0]["event"] for result in results] == ["reset"] * len(streams)
        assert epoch_reads == ["mirror-fanout-rewrite"]  # Shared, not per client.
    finally:
        for stream in streams:
            await stream.aclose()


async def test_peer_rewrite_during_initial_backfill_is_not_lost(mock_db, gw, monkeypatch):
    sid = "mirror-before-seed"
    database.create_session(sid, "default", title="Receiver still backfilling")
    se.append_events(sid, [{"type": "message.user", "payload": {"content": f"old-{n}"}}
                           for n in range(3)])
    epoch = se.get_log_epoch(sid)
    entered, release = asyncio.Event(), asyncio.Event()
    real_replay = events_mod._replay_from

    async def held_replay(session_id, cursor):
        entered.set()
        await release.wait()
        async for row in real_replay(session_id, cursor):
            yield row

    monkeypatch.setattr(events_mod, "_replay_from", held_replay)
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    stream = event_stream(sid, 3, epoch)
    opening = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(entered.wait(), 0.8)
        for payload in mirror_rows(sid, 1, replace=True):
            gw._handle_payload(payload)
        await asyncio.sleep(0.03)  # The shared reader sees it before note_cursor.
        release.set()
        assert parse_frame(await opening)["event"] == "caught_up"
        reset = (await collect_frames(stream, 1, timeout=0.8))[0]
        assert reset["event"] == "reset"
        assert reset["data"]["epoch"] != epoch
    finally:
        release.set()
        if not opening.done():
            await opening
        await stream.aclose()


@pytest.mark.parametrize("delivery", [
    "local-push", "mirror-missed-before-seed", "mirror-missed-fallback", "mirror-fallback-race",
    "mirror-notified-race",
])
async def test_empty_log_adopts_first_delivered_identity(mock_db, gw, monkeypatch, delivery):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.01)
    # The notified race must use the shared reader, not fallback reconciliation.
    monkeypatch.setattr(events_mod, "LIVE_RESYNC_SECONDS",
                        120 if delivery == "mirror-notified-race" else 0)
    monkeypatch.setattr(events_mod, "_push_delivery_healthy", lambda tail: True)
    seeded, release = asyncio.Event(), asyncio.Event()
    real_seed = events_mod._SessionTail._seed

    async def controlled_seed(tail):
        if delivery == "mirror-missed-before-seed":
            await release.wait()
        result = await real_seed(tail)
        seeded.set()
        return result

    monkeypatch.setattr(events_mod._SessionTail, "_seed", controlled_seed)
    sid = "first-" + delivery
    database.create_session(sid, "default", title="New empty conversation")
    stream = event_stream(sid)
    real_check = events_mod._resume_check
    raced = []

    def check_then_replace(session_id, cursor, epoch):
        result = real_check(session_id, cursor, epoch)
        if (delivery == "mirror-fallback-race" and not raced and cursor == 0
                and result[0] != events_mod.EMPTY_LOG_EPOCH):
            raced.append(True)
            mirror_rows(session_id, 1, replace=True)
        return result

    monkeypatch.setattr(events_mod, "_resume_check", check_then_replace)
    real_epoch = events_mod.get_log_epoch

    def probe_then_replace(session_id):
        epoch = real_epoch(session_id)
        if (delivery == "mirror-notified-race" and not raced
                and epoch != events_mod.EMPTY_LOG_EPOCH):
            # A commits and notifies. The probe sees A, then B commits before
            # the batch read. Both writes use the real receiver materializer.
            raced.append(epoch)
            for payload in mirror_rows(session_id, 1, replace=True):
                gw._handle_payload(payload)
        return epoch

    monkeypatch.setattr(events_mod, "get_log_epoch", probe_then_replace)
    try:
        assert parse_frame(await anext(stream)) == {
            "event": "caught_up", "data": {"seq": 0, "epoch": events_mod.EMPTY_LOG_EPOCH}}
        if delivery != "mirror-missed-before-seed":
            await asyncio.wait_for(seeded.wait(), 1)
        if delivery == "local-push":
            se.append_events(sid, [{"type": "message.user", "payload": {"content": "Local first"}}])
            gw._handle_payload(json.dumps({"session_id": sid, "max_seq": 1}))
        else:
            notifications = mirror_rows(sid, 1, replace=False)
            if delivery == "mirror-notified-race":
                for payload in notifications:
                    gw._handle_payload(payload)
            # Other variants deliberately lose the real mirror NOTIFY.
            release.set()
        first = (await collect_frames(stream, 1))[0]
        assert first["event"] == "message.user"
        if delivery == "mirror-notified-race":
            assert len(raced) == 1
            assert first["data"]["id"] != raced[0]
            assert first["data"]["id"] == se.get_log_epoch(sid)
        # Reconciliation runs now, not after the production 120s deadline. It
        # must keep a valid first-message feed open instead of resetting it.
        assert (await asyncio.wait_for(anext(stream), 1)).startswith(": keepalive")
        assert not events_mod._tails[sid].closed
        se.append_events(sid, [{"type": "message.user", "payload": {"content": "Local second"}}])
        gw._handle_payload(json.dumps({"session_id": sid, "max_seq": 2}))
        second = (await collect_frames(stream, 1))[0]
        assert second["event"] == "message.user"
        assert second["data"]["content"] == "Local second"
        for payload in mirror_rows(sid, 1, replace=True):
            gw._handle_payload(payload)
        reset = (await collect_frames(stream, 1))[0]
        assert reset["event"] == "reset"  # Only a real subsequent rewrite resets.
        assert reset["data"]["epoch"] != first["data"]["id"]
        if delivery == "mirror-fallback-race":
            assert raced == [True]
    finally:
        release.set()
        await stream.aclose()


async def test_empty_client_does_not_adopt_an_older_readers_probe(mock_db, gw, monkeypatch):
    sid = "empty-client-existing-reader"
    warm, _ = await warm_reader(sid)
    mirror_rows(sid, 0, replace=True)
    empty = event_stream(sid)
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.01)
    monkeypatch.setattr(events_mod, "LIVE_RESYNC_SECONDS", 0)
    real_check = events_mod._resume_check
    probed = []

    def replace_before_first_delivery(session_id, cursor, epoch):
        result = real_check(session_id, cursor, epoch)
        if cursor == 0 and result[0] != events_mod.EMPTY_LOG_EPOCH and not probed:
            probed.append(result[0])
            mirror_rows(session_id, 1, replace=True)
        return result

    monkeypatch.setattr(events_mod, "_resume_check", replace_before_first_delivery)
    try:
        assert parse_frame(await anext(empty))["data"] == {
            "seq": 0, "epoch": events_mod.EMPTY_LOG_EPOCH}
        for payload in mirror_rows(sid, 1, replace=False):
            gw._handle_payload(payload)
        first = (await collect_frames(empty, 1))[0]
        assert first["event"] == "message.user"
        assert len(probed) == 1 and first["data"]["id"] != probed[0]
        assert (await asyncio.wait_for(anext(empty), 1)).startswith(": keepalive")
        # The other client really saw an older prefix, so it still resets.
        assert (await collect_frames(warm, 1))[0]["event"] == "reset"
    finally:
        await empty.aclose()
        await warm.aclose()


async def test_first_batch_invalidates_another_clients_delivered_prefix(mock_db, gw, monkeypatch):
    sid = "first-batch-shared-identity"
    database.create_session(sid, "default", title="Concurrent empty and nonempty views")
    entered, release = asyncio.Event(), asyncio.Event()
    real_read = events_mod._SessionTail._read_and_publish

    async def held_first_read(tail):
        entered.set()
        await release.wait()
        await real_read(tail)

    monkeypatch.setattr(events_mod._SessionTail, "_read_and_publish", held_first_read)
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.01)
    monkeypatch.setattr(events_mod, "LIVE_RESYNC_SECONDS", 120)
    empty = event_stream(sid)
    other = None
    try:
        assert parse_frame(await anext(empty))["data"]["epoch"] == events_mod.EMPTY_LOG_EPOCH
        await asyncio.wait_for(entered.wait(), 1)
        mirror_rows(sid, 1, replace=False)
        other = event_stream(sid)
        previous = [parse_frame(await asyncio.wait_for(anext(other), 1)) for _ in range(2)]
        assert [frame["event"] for frame in previous] == ["message.user", "caught_up"]
        mirror_rows(sid, 1, replace=True)  # Deliberately lose the mirror NOTIFY.
        release.set()
        first = (await collect_frames(empty, 1))[0]
        assert first["event"] == "message.user"
        assert first["data"]["id"] != previous[0]["data"]["id"]
        reset = (await collect_frames(other, 1))[0]
        assert reset["event"] == "reset"
        assert reset["data"]["epoch"] == first["data"]["id"]
        assert (await asyncio.wait_for(anext(empty), 1)).startswith(": keepalive")
        assert not events_mod._tails[sid].closed
    finally:
        release.set()
        await empty.aclose()
        if other is not None:
            await other.aclose()
