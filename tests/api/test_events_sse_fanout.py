"""Shared-reader fan-out for the SSE tail (audit W4 / F9).

The live tail used to cost every connected client a ``get_max_seq`` + a
``get_events`` + a full re-serialization on every notify tick. These tests pin
the replacement: ONE read and ONE serialization per append batch per session,
fanned out as pre-rendered frames — plus the guarantees that make pushing rows
safe (a client emits a batch only when it continues exactly where that client
left off; anything else falls back to a read).

Like the other SSE tests these consume the module-level ``event_stream``
generator directly (ASGI test transports buffer the infinite body) and stub the
gateway's PostgreSQL listener thread.
"""

import asyncio

import pytest

import app.api.events as events_mod
from app.api.events import event_stream
from app.db import session_events as se
from tests.api.sse_helpers import collect_frames, parse_frame


@pytest.fixture(autouse=True)
def _clean_tails():
    events_mod._tails.clear()
    yield
    for tail in list(events_mod._tails.values()):
        tail.close()
    events_mod._tails.clear()


class _Counter:
    """Counts calls to a module-level DB helper without changing behaviour."""

    def __init__(self, monkeypatch, name):
        self.calls = 0
        real = getattr(events_mod, name)

        def _wrapped(*args, **kwargs):
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(events_mod, name, _wrapped)


async def _to_live(agen, timeout=5.0):
    """Pump until the ``caught_up`` sentinel, so the stream is in its live tail.

    Returns the backfill frames. Stopping at the first frame instead would park
    the generator mid-backfill, before it has seeded the shared reader.
    """
    frames = []

    async def _pump():
        async for chunk in agen:
            if chunk.startswith(":"):
                continue
            parsed = parse_frame(chunk)
            if parsed.get("event") == "caught_up":
                return
            frames.append(parsed)

    await asyncio.wait_for(_pump(), timeout=timeout)
    return frames


async def _collect_comments(agen, n, timeout=5.0):
    """Read ``n`` keepalive comments."""
    seen = []

    async def _pump():
        async for chunk in agen:
            if chunk.startswith(":"):
                seen.append(chunk)
                if len(seen) >= n:
                    return

    await asyncio.wait_for(_pump(), timeout=timeout)
    return seen


# ---------------------------------------------------------------------------
# W4 — one DB read + one serialization per batch, whatever the client count
# ---------------------------------------------------------------------------


async def test_one_db_read_per_batch_regardless_of_client_count(
    mock_db, gw, monkeypatch
):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-fan", [{"type": "turn.started"}])

    agens = [event_stream("sess-fan", 0) for _ in range(4)]
    try:
        for agen in agens:
            assert [f["id"] for f in await _to_live(agen)] == [1]
        await asyncio.sleep(0.05)  # let the shared reader seed its cursor

        reads = _Counter(monkeypatch, "get_events")
        seq_reads = _Counter(monkeypatch, "get_max_seq")

        _, last_seq = se.append_events(
            "sess-fan",
            [
                {"type": "assistant.text_delta", "payload": {"text": "a"}},
                {"type": "assistant.text_delta", "payload": {"text": "b"}},
            ],
        )
        gw._dispatch("sess-fan", last_seq)

        for agen in agens:
            got = await collect_frames(agen, 2, timeout=3.0)
            assert [f["id"] for f in got] == [2, 3]
    finally:
        for agen in agens:
            await agen.aclose()

    # One read for the batch, shared by all four clients (it used to be 2 per
    # client per tick), and no get_max_seq probe at all on the push path.
    assert reads.calls == 1
    assert seq_reads.calls == 0


async def test_serialization_is_shared_not_repeated_per_client(
    mock_db, gw, monkeypatch
):
    """Every client is handed the SAME frame string object — serialize once."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-share", [{"type": "turn.started"}])

    frames_built = _Counter(monkeypatch, "_frame")
    agens = [event_stream("sess-share", 0) for _ in range(3)]
    try:
        for agen in agens:
            await _to_live(agen)
        await asyncio.sleep(0.05)
        before = frames_built.calls

        _, last_seq = se.append_events("sess-share", [{"type": "turn.completed"}])
        gw._dispatch("sess-share", last_seq)

        payloads = []
        for agen in agens:
            payloads.append((await collect_frames(agen, 1, timeout=3.0))[0])
    finally:
        for agen in agens:
            await agen.aclose()

    assert frames_built.calls - before == 1  # one serialization for three clients
    assert all(p["id"] == 2 for p in payloads)


# ---------------------------------------------------------------------------
# W4 — ordering / gap-free / duplicate-free under the pushed path
# ---------------------------------------------------------------------------


async def test_pushed_batches_preserve_order_and_are_gap_free(
    mock_db, gw, monkeypatch
):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-ord", [{"type": "turn.started"}])

    agens = [event_stream("sess-ord", 0) for _ in range(2)]
    try:
        for agen in agens:
            await _to_live(agen)
        await asyncio.sleep(0.05)

        for i in range(5):
            _, last_seq = se.append_events(
                "sess-ord",
                [{"type": "assistant.text_delta", "payload": {"text": str(i)}}],
            )
            gw._dispatch("sess-ord", last_seq)

        for agen in agens:
            got = await collect_frames(agen, 5, timeout=3.0)
            assert [f["id"] for f in got] == [2, 3, 4, 5, 6]
            assert [f["data"]["text"] for f in got] == ["0", "1", "2", "3", "4"]
    finally:
        for agen in agens:
            await agen.aclose()


async def test_reconnect_cursor_semantics_hold_on_the_push_path(
    mock_db, gw, monkeypatch
):
    """``from_seq`` stays exclusive: a resumed client sees seq > from_seq only."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-res", [{"type": "a"}, {"type": "b"}, {"type": "c"}])

    agen = event_stream("sess-res", 2)
    try:
        assert [f["id"] for f in await _to_live(agen)] == [3]
        await asyncio.sleep(0.05)
        _, last_seq = se.append_events("sess-res", [{"type": "d"}])
        gw._dispatch("sess-res", last_seq)
        got = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert [f["id"] for f in got] == [4]


async def test_discontinuous_batch_is_never_emitted_but_read_instead(
    mock_db, gw, monkeypatch
):
    """A batch that does not continue this client's cursor must be repaired.

    This is the invariant that makes pushing rows safe at all: a dropped batch
    (slow client, queue overflow) or a late attach leaves a discontinuity, and
    the client must fall back to a read rather than emit what it was handed.
    """
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-gap", [{"type": "turn.started"}])

    agen = event_stream("sess-gap", 0)
    try:
        await _to_live(agen)
        await asyncio.sleep(0.05)

        tail = events_mod._tails["sess-gap"]
        sub = next(iter(tail._subs))
        # A real event lands, but this client is handed a batch that starts far
        # past its cursor (as if everything in between had been dropped).
        se.append_events("sess-gap", [{"type": "turn.completed"}])
        poisoned = events_mod._TailBatch(
            98, 99, ((99, "id: 99\nevent: bogus\ndata: {}\n\n"),)
        )
        sub.queue.put_nowait(poisoned)

        got = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    # The bogus frame is discarded; the real row is read from the log.
    assert got[0]["id"] == 2
    assert got[0]["event"] == "turn.completed"


async def test_already_seen_batch_is_dropped_not_duplicated(
    mock_db, gw, monkeypatch
):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-dup", [{"type": "turn.started"}])

    agen = event_stream("sess-dup", 0)
    try:
        await _to_live(agen)
        await asyncio.sleep(0.05)

        tail = events_mod._tails["sess-dup"]
        sub = next(iter(tail._subs))
        # Re-deliver the backfilled row: wholly at/below the cursor -> dropped.
        stale = events_mod._TailBatch(
            0, 1, ((1, "id: 1\nevent: turn.started\ndata: {}\n\n"),)
        )
        sub.queue.put_nowait(stale)

        _, last_seq = se.append_events("sess-dup", [{"type": "turn.completed"}])
        gw._dispatch("sess-dup", last_seq)
        got = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert got[0]["id"] == 2  # the stale re-delivery never reached the wire


async def test_event_appended_between_backfill_and_seed_still_arrives(
    mock_db, gw, monkeypatch
):
    """The reader starts at min(client cursor, tail) — never above a client."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-race", [{"type": "turn.started"}])

    agen = event_stream("sess-race", 0)
    try:
        await _to_live(agen)
        # Land the event BEFORE the reader has had a chance to seed itself, then
        # tick: a reader seeded at the current tail would swallow it forever.
        _, last_seq = se.append_events("sess-race", [{"type": "turn.completed"}])
        gw._dispatch("sess-race", last_seq)
        got = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert got[0]["id"] == 2


async def test_bogus_from_seq_cannot_starve_other_clients(mock_db, gw, monkeypatch):
    """``from_seq`` is caller-supplied; the reader must clamp it to the tail."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-bogus", [{"type": "turn.started"}])

    liar = event_stream("sess-bogus", 10**9)
    honest = event_stream("sess-bogus", 0)
    try:
        await _to_live(liar)
        await _to_live(honest)
        await asyncio.sleep(0.05)
        _, last_seq = se.append_events("sess-bogus", [{"type": "turn.completed"}])
        gw._dispatch("sess-bogus", last_seq)
        got = await collect_frames(honest, 1, timeout=3.0)
    finally:
        await liar.aclose()
        await honest.aclose()

    assert got[0]["id"] == 2
    assert events_mod._tails == {}  # both detached -> reader torn down


async def test_reader_failure_falls_back_to_reads(mock_db, gw, monkeypatch):
    """A dead shared reader must not strand clients — they poll again."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.05)
    se.append_events("sess-dead", [{"type": "turn.started"}])

    agen = event_stream("sess-dead", 0)
    try:
        await _to_live(agen)
        await asyncio.sleep(0.05)
        tail = events_mod._tails["sess-dead"]
        tail._task.cancel()
        await asyncio.sleep(0.05)
        assert tail.alive is False

        se.append_events("sess-dead", [{"type": "turn.completed"}])
        got = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert got[0]["id"] == 2


async def test_last_client_tears_down_the_shared_reader(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    se.append_events("sess-tear", [{"type": "turn.started"}])

    a = event_stream("sess-tear", 0)
    b = event_stream("sess-tear", 0)
    await _to_live(a)
    await _to_live(b)
    assert len(gw._subscribers["sess-tear"]) == 1  # ONE gateway queue per session

    await a.aclose()
    assert "sess-tear" in events_mod._tails  # b still attached
    await b.aclose()
    assert events_mod._tails == {}
    assert gw._subscribers == {}


# ---------------------------------------------------------------------------
# F9 — the keepalive tick stops costing a DB read on a quiet, healthy feed
# ---------------------------------------------------------------------------


async def test_keepalive_skips_the_db_read_while_delivery_is_healthy(
    mock_db, gw, monkeypatch
):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "LIVE_RESYNC_SECONDS", 600.0)
    monkeypatch.setattr(gw, "is_connected", lambda: True)
    se.append_events("sess-quiet", [{"type": "turn.started"}])

    agen = event_stream("sess-quiet", 0)
    try:
        await _to_live(agen)
        await asyncio.sleep(0.05)
        reads = _Counter(monkeypatch, "get_events")
        seq_reads = _Counter(monkeypatch, "get_max_seq")
        comments = await _collect_comments(agen, 3, timeout=3.0)
    finally:
        await agen.aclose()

    assert len(comments) == 3       # the feed is still alive on the wire
    assert reads.calls == 0         # ...but silent ticks cost nothing
    assert seq_reads.calls == 0


async def test_keepalive_still_polls_when_the_gateway_is_degraded(
    mock_db, gw, monkeypatch
):
    """The documented fallback stays intact: no notifies -> poll every tick."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "LIVE_RESYNC_SECONDS", 600.0)
    monkeypatch.setattr(gw, "is_connected", lambda: False)
    se.append_events("sess-degraded", [{"type": "turn.started"}])

    agen = event_stream("sess-degraded", 0)
    try:
        await _to_live(agen)
        await asyncio.sleep(0.05)
        reads = _Counter(monkeypatch, "get_events")
        await _collect_comments(agen, 2, timeout=3.0)
        # Delivery must still work with no dispatch at all.
        se.append_events("sess-degraded", [{"type": "turn.completed"}])
        got = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert reads.calls >= 2
    assert got[0]["id"] == 2


async def test_periodic_resync_still_reconciles_a_healthy_feed(
    mock_db, gw, monkeypatch
):
    """The skipped read is deferred, not abandoned: a low-frequency re-sync
    still catches anything the push path could ever miss."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 0.02)
    monkeypatch.setattr(events_mod, "LIVE_RESYNC_SECONDS", 0.05)
    monkeypatch.setattr(gw, "is_connected", lambda: True)
    se.append_events("sess-resync", [{"type": "turn.started"}])

    agen = event_stream("sess-resync", 0)
    try:
        await _to_live(agen)
        await asyncio.sleep(0.05)
        # No dispatch, no reader push — only the periodic re-sync can find it.
        se.append_events("sess-resync", [{"type": "turn.completed"}])
        got = await collect_frames(agen, 1, timeout=3.0)
    finally:
        await agen.aclose()

    assert got[0]["id"] == 2


# ---------------------------------------------------------------------------
# B6 — the legacy-history seeding is lazy
# ---------------------------------------------------------------------------


async def test_populated_log_never_calls_ensure_backfilled(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    calls = []

    async def _spy(session_id):
        calls.append(session_id)

    monkeypatch.setattr(events_mod, "ensure_backfilled", _spy)
    se.append_events("sess-lazy", [{"type": "turn.started"}])

    agen = event_stream("sess-lazy", 0)
    try:
        assert [f["id"] for f in await _to_live(agen)] == [1]
    finally:
        await agen.aclose()

    assert calls == []  # not even the probing get_max_seq read


async def test_reconnect_never_reseeds_legacy_history(mock_db, gw, monkeypatch):
    """A resumed client already holds its history; seeding one there would
    replay a duplicate legacy prefix past a cursor it cannot reconcile."""
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    calls = []

    async def _spy(session_id):
        calls.append(session_id)

    monkeypatch.setattr(events_mod, "ensure_backfilled", _spy)

    agen = event_stream("sess-lazy-2", 7)  # empty log, resumed cursor
    try:
        await _to_live(agen)
    finally:
        await agen.aclose()

    assert calls == []


async def test_empty_log_on_a_fresh_load_still_seeds(mock_db, gw, monkeypatch):
    monkeypatch.setattr(events_mod, "KEEPALIVE_SECONDS", 30.0)
    calls = []

    async def _seed(session_id):
        calls.append(session_id)
        se.append_events(session_id, [{"type": "message.user"}])

    monkeypatch.setattr(events_mod, "ensure_backfilled", _seed)

    agen = event_stream("sess-lazy-3", 0)
    try:
        frames = await _to_live(agen)
    finally:
        await agen.aclose()

    assert calls == ["sess-lazy-3"]
    assert [f["event"] for f in frames] == ["message.user"]
