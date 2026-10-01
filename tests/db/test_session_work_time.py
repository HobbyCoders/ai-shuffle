"""Session working time (#1619): ``sessions.work_ms`` / ``sessions.work_started_at``.

The sidebar card shows how long a session has actually worked. Every turn
transition that ends a live interval banks it onto the session row once;
idle gaps between turns (and the wait while a turn is parked on background
work) never count. The total survives completion and parking and keeps
growing when a later turn runs.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.models import Session
from app.db import database, runs, turns
from app.network import engine, policy
from tests.db.migration_helpers import column_names


def _mk(session_id: str) -> str:
    database.create_session(session_id=session_id, profile_id="default")
    return session_id


def _work(session_id: str) -> tuple[int, object]:
    row = database.get_session(session_id)
    return int(row["work_ms"] or 0), row["work_started_at"]


def _backdate_start(turn_id: str, seconds: int) -> None:
    """Pretend the running turn started ``seconds`` ago."""
    from app.db.base import get_db

    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE turns SET started_at = NOW() - make_interval(secs => %s) WHERE id = %s",
            (seconds, turn_id),
        )


def _run(session_id: str, source: str = "user", *, seconds: int = 0) -> dict:
    turn = turns.enqueue_turn(session_id, source, prompt="p")
    assert turns.start_turn(turn["id"]) is not None
    if seconds:
        _backdate_start(turn["id"], seconds)
    return turn


# ---------------------------------------------------------------------------
# interval_ms
# ---------------------------------------------------------------------------

def test_interval_ms_reads_every_timestamp_shape():
    aware = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert turns.interval_ms(aware, aware + timedelta(seconds=90)) == 90_000
    # Naive values are UTC (the shim's CURRENT_TIMESTAMP text form).
    assert turns.interval_ms("2026-10-01 12:00:00", "2026-10-01 12:01:30") == 90_000
    assert turns.interval_ms("2026-10-01T12:00:00Z", "2026-10-01T13:00:00+01:00") == 0
    assert turns.interval_ms("2026-10-01T12:00:00Z", "2026-10-01T12:00:01.500+00:00") == 1_500
    # Missing, unparseable or reversed bounds bank nothing.
    assert turns.interval_ms(None, aware) == 0
    assert turns.interval_ms("garbage", aware) == 0
    assert turns.interval_ms(aware, aware - timedelta(seconds=5)) == 0


# ---------------------------------------------------------------------------
# Banking on turn transitions
# ---------------------------------------------------------------------------

def test_new_session_starts_at_zero(mock_db):
    sid = _mk("wt-new")
    assert _work(sid) == (0, None)


def test_running_turn_sets_live_start_and_settle_banks_it(mock_db):
    sid = _mk("wt-settle")
    turn = _run(sid, seconds=90)
    # start_turn records the live start; the backdate above moved the turn's
    # own start, so the session mirrors the turn row on the next transition.
    assert _work(sid)[1] is not None

    settled = turns.settle_turn(turn["id"], "completed")
    banked, live = _work(sid)
    assert live is None
    assert banked == turns.interval_ms(settled["started_at"], settled["ended_at"])
    assert 89_000 <= banked <= 92_000


def test_total_survives_park_and_grows_with_the_next_turn(mock_db):
    """Run -> park -> (idle) -> wake -> complete: the total never resets, the
    park wait is not counted, and the wake's own interval is added."""
    sid = _mk("wt-park")
    first = _run(sid, seconds=120)
    parked = turns.park_turn(first["id"], ["task-1"])
    after_park, live = _work(sid)
    assert live is None
    assert after_park == turns.interval_ms(parked["started_at"], parked["ended_at"])

    # The background work finishes much later; settling the parked row moves
    # its ended_at but must not bank the wait.
    turns.settle_parked_turns(sid)
    assert _work(sid)[0] == after_park

    wake = _run(sid, "unpark", seconds=30)
    assert _work(sid)[0] == after_park  # a running turn banks nothing yet
    done = turns.settle_turn(wake["id"], "completed")
    total, live = _work(sid)
    assert live is None
    assert total == after_park + turns.interval_ms(done["started_at"], done["ended_at"])
    assert total > after_park


def test_settle_is_banked_once(mock_db):
    sid = _mk("wt-once")
    turn = _run(sid, seconds=10)
    turns.settle_turn(turn["id"], "completed")
    banked = _work(sid)[0]
    # A repeated settle (a racer, a retry) is a no-op and banks nothing more.
    assert turns.settle_turn(turn["id"], "aborted") is None
    assert turns.park_turn(turn["id"]) is None
    assert _work(sid)[0] == banked


def test_settling_a_queued_turn_keeps_the_live_turn_running(mock_db):
    sid = _mk("wt-queued")
    live = _run(sid, seconds=20)
    queued = turns.enqueue_turn(sid, "queue", prompt="later")
    turns.settle_turn(queued["id"], "aborted", "removed")
    banked, started = _work(sid)
    assert banked == 0
    assert started is not None  # still the live turn's start
    turns.settle_turn(live["id"], "completed")
    assert _work(sid)[1] is None


def test_requeue_clears_live_start_without_banking(mock_db):
    sid = _mk("wt-requeue")
    turn = _run(sid, seconds=40)
    turns.requeue_turn(turn["id"])
    assert _work(sid) == (0, None)


def test_reap_banks_aborted_work_and_clears_requeued_start(mock_db):
    aborted_sid = _mk("wt-reap-abort")
    auto = turns.enqueue_turn(aborted_sid, "auto_continue")
    turns.start_turn(auto["id"], run_id=None)
    _backdate_start(auto["id"], 15)

    requeued_sid = _mk("wt-reap-requeue")
    msg = turns.enqueue_turn(requeued_sid, "user", prompt="keep me")
    dead = runs.claim_run(requeued_sid, "runner", -1)
    turns.start_turn(msg["id"], run_id=dead["id"])

    turns.reap_expired()
    banked, started = _work(aborted_sid)
    assert started is None
    assert 14_000 <= banked <= 17_000
    assert _work(requeued_sid) == (0, None)


def test_stopped_orphan_turn_is_banked(mock_db):
    sid = _mk("wt-orphan")
    turn = _run(sid, seconds=25)
    stopped = turns.stop_orphaned_turns(sid)
    assert [r["id"] for r in stopped] == [turn["id"]]
    assert _work(sid) == (turns.interval_ms(stopped[0]["started_at"], stopped[0]["ended_at"]), None)


def test_adhoc_turn_is_banked(mock_db):
    sid = _mk("wt-adhoc")
    started = datetime.now(timezone.utc) - timedelta(seconds=45)
    row = turns.record_adhoc_turn(sid, "api_query", started_at=started.replace(microsecond=0))
    banked, live = _work(sid)
    assert live is None
    assert banked == turns.interval_ms(row["started_at"], row["ended_at"])
    assert banked >= 44_000


def test_work_update_failure_never_breaks_the_transition(mock_db, monkeypatch):
    """The session write runs after the turn transition commits and swallows
    its own failure: the turn still settles."""
    sid = _mk("wt-fail")
    turn = _run(sid)
    real_get_db = turns.get_db
    calls = {"n": 0}

    def flaky_get_db():
        calls["n"] += 1
        if calls["n"] == 2:  # the session write after the turn transition
            raise RuntimeError("db down")
        return real_get_db()

    monkeypatch.setattr(turns, "get_db", flaky_get_db)
    assert turns.settle_turn(turn["id"], "completed") is not None
    monkeypatch.undo()
    assert calls["n"] == 2
    assert turns.get_turn(turn["id"])["state"] == "completed"


# ---------------------------------------------------------------------------
# Payload + sync
# ---------------------------------------------------------------------------

def test_session_payload_carries_work_fields(mock_db):
    sid = _mk("wt-payload")
    turn = _run(sid, seconds=5)
    model = Session(**database.get_session(sid))
    assert model.work_ms == 0
    assert model.work_started_at is not None
    turns.settle_turn(turn["id"], "completed")
    model = Session(**database.get_session(sid))
    assert model.work_ms >= 4_000
    assert model.work_started_at is None
    # A NULL column (a row the default never reached) reads as 0.
    row = dict(database.get_session(sid), work_ms=None)
    assert Session(**row).work_ms == 0


def test_work_fields_travel_to_peer_mirrors():
    coll = policy.collection("sessions")
    assert {"work_ms", "work_started_at"} <= set(coll.fields)
    assert not {"work_ms", "work_started_at"} & set(coll.local_fields)
    started = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    record = engine.serialize_row(coll, {"id": "s1", "work_ms": 61_000, "work_started_at": started})
    assert record["work_ms"] == 61_000
    assert record["work_started_at"] == started.isoformat()


async def test_runtime_pushes_work_fields_to_every_device(mock_db, monkeypatch):
    """SessionRuntime sends the row's work fields as ``user_session_updated``
    after a turn starts and after it settles, so sidebars on every device tick
    from the real start and bank the finished interval without a reload."""
    from app.core import session_runtime
    from app.core.sync_engine import sync_engine

    sid = _mk("wt-push")
    sent = []

    async def capture(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", capture)
    turn = _run(sid, seconds=3)
    await session_runtime._broadcast_work_time(sid)
    turns.settle_turn(turn["id"], "completed")
    await session_runtime._broadcast_work_time(sid)
    await session_runtime._broadcast_work_time("no-such-session")

    assert [m["session_id"] for m in sent] == [sid, sid]
    assert sent[0]["user_id"] == "admin"
    assert sent[0]["updates"]["work_ms"] == 0
    assert sent[0]["updates"]["work_started_at"]
    assert sent[1]["updates"] == {"work_ms": _work(sid)[0], "work_started_at": None}
    assert sent[1]["updates"]["work_ms"] >= 2_000


# ---------------------------------------------------------------------------
# Migration + one-time backfill
# ---------------------------------------------------------------------------

def _seed_turn(conn, turn_id, sid, started, ended, stop_reason=None, state="completed"):
    conn.execute(
        "INSERT INTO turns (id, session_id, state, source, started_at, ended_at, stop_reason) "
        "VALUES (?, ?, ?, 'user', ?, ?, ?)",
        (turn_id, sid, state, started, ended, stop_reason),
    )


def test_backfill_sums_merges_and_skips_parked_wait(mock_db):
    conn = mock_db
    _mk("bf-a")
    _mk("bf-b")
    _mk("bf-c")
    # bf-a: two separate 60 s turns with an hour idle between them, plus an
    # ad-hoc run overlapping the first one (counted once).
    _seed_turn(conn, "a1", "bf-a", "2026-09-01 10:00:00", "2026-09-01 10:01:00")
    _seed_turn(conn, "a2", "bf-a", "2026-09-01 11:00:00", "2026-09-01 11:01:00")
    _seed_turn(conn, "a3", "bf-a", "2026-09-01 10:00:30", "2026-09-01 10:01:30")
    # bf-b: a parked turn settled two hours later; its last event (10:05) is
    # where its foreground work ended.
    _seed_turn(conn, "b1", "bf-b", "2026-09-01 10:00:00", "2026-09-01 12:00:00", "parked_settled")
    conn.execute(
        "INSERT INTO session_events (id, session_id, seq, turn_id, event_type, payload, created_at) "
        "VALUES ('e1', 'bf-b', 1, 'b1', 'message.assistant', '{}', '2026-09-01 10:05:00')"
    )
    # A queued turn that never started banks nothing.
    _seed_turn(conn, "b2", "bf-b", None, "2026-09-01 12:00:00", state="aborted")
    # bf-c already banked under the new code: left alone.
    conn.execute("UPDATE sessions SET work_ms = 5000 WHERE id = 'bf-c'")
    _seed_turn(conn, "c1", "bf-c", "2026-09-01 10:00:00", "2026-09-01 10:10:00")
    conn.commit()

    from app.db.base import get_db

    with get_db() as db:
        updated = database._backfill_sessions_work_ms(db.cursor())
    assert updated == 2
    assert _work("bf-a")[0] == 150_000  # 10:00:00-10:01:30 merged + 60 s
    assert _work("bf-b")[0] == 300_000
    assert _work("bf-c")[0] == 5_000

    # Marker-gated: a second pass changes nothing.
    conn.execute("UPDATE sessions SET work_ms = 0 WHERE id = 'bf-a'")
    conn.commit()
    with get_db() as db:
        assert database._backfill_sessions_work_ms(db.cursor()) is None
    assert _work("bf-a")[0] == 0


def test_init_adds_work_columns_to_an_existing_db(mock_db):
    conn = mock_db
    conn.execute("ALTER TABLE sessions DROP COLUMN work_ms")
    conn.execute("ALTER TABLE sessions DROP COLUMN work_started_at")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (90)")
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id) VALUES ('s-legacy-work', 'default', '')"
    )
    _seed_turn(conn, "lw1", "s-legacy-work", "2026-09-01 10:00:00", "2026-09-01 10:02:00")
    conn.commit()
    assert "work_ms" not in column_names(conn, "sessions")

    database.init_database()

    assert {"work_ms", "work_started_at"} <= column_names(conn, "sessions")
    assert _work("s-legacy-work") == (120_000, None)
