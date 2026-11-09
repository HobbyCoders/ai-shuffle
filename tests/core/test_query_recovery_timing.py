"""Recovery is historical evidence, not a fresh execution or billable interval."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.turn_scheduler import _latest_context_turn
from app.db import database, query_recovery, turns
from app.db.base import get_db


@pytest.fixture
def session():
    return database.create_session(session_id="recovery-timing", profile_id="default")


def begin(session, started, **kwargs):
    return query_recovery.begin(
        session["id"], "api_query", instance_id="timing-install", process_id="old-process",
        started_at=started, **kwargs,
    )


def recover():
    return query_recovery.recover(instance_id="timing-install", process_id="new-process")


def test_old_receipt_cannot_replace_newer_execution_or_permission_context(session):
    old = datetime.now(timezone.utc) - timedelta(days=1)
    saved = begin(session, old, overrides={"model": "old-model", "provider": "old-provider",
                                         "permission_mode": "bypassPermissions", "_caller": "old-caller"})
    newer = turns.record_adhoc_turn(
        session["id"], "api_query", started_at=old + timedelta(hours=1),
        overrides={"model": "new-model", "provider": "new-provider",
                   "permission_mode": "default", "_caller": "new-caller"},
    )
    assert recover() == 1
    assert _latest_context_turn(session["id"])["id"] == newer["id"]
    # Other permission/voice/worker readers use immutable history order, not ended_at.
    assert turns.get_latest_turn(session["id"])["id"] == newer["id"]
    assert turns.get_latest_run_turn(session["id"])["id"] == newer["id"]
    assert turns.get_latest_started_turn(session["id"])["id"] == newer["id"]
    assert turns.latest_caller(session["id"]) == "new-caller"
    with get_db() as conn:
        cursor = conn.cursor()
        from app.db.composer_suggestions import _LATEST_TURN
        cursor.execute("SELECT (" + _LATEST_TURN + ") AS id", (session["id"],))
        assert cursor.fetchone()["id"] == newer["id"]
    assert turns.get_turn(saved["id"])["state"] == "aborted"


def test_recovery_never_banks_unknown_execution_or_offline_time(session):
    started = datetime.now(timezone.utc) - timedelta(days=1)
    saved = begin(session, started)
    before = database.get_session(session["id"])
    assert recover() == 1
    terminal = turns.get_turn(saved["id"])
    assert database.get_session(session["id"])["work_ms"] == before["work_ms"]
    assert turns.interval_ms(terminal["started_at"], terminal["ended_at"]) == 0
    assert terminal["ended_at"] is None  # No invented completion time before existing events.
    assert turns._to_utc(terminal["started_at"]) == started
    assert turns._to_utc(terminal["created_at"]) == started
    assert recover() == 0
    assert database.get_session(session["id"])["work_ms"] == before["work_ms"]


@pytest.mark.asyncio
async def test_newest_recovery_hands_its_context_to_the_next_real_wake(session):
    from app.core import limit_resume, turn_scheduler

    older = turns.record_adhoc_turn(session["id"], "api_query", overrides={"model": "older"})
    started = datetime.now(timezone.utc)
    overrides = {"model": "latest", "provider": "latest-provider", "permission_mode": "default"}
    saved = begin(session, started, overrides=overrides, profile_id="default", payload={"api_user_id": "user"})
    assert recover() == 1
    recovered = turns.get_turn(saved["id"])
    assert _latest_context_turn(session["id"])["id"] == saved["id"]
    assert turns.get_latest_turn(session["id"])["id"] == saved["id"]
    assert turns.get_latest_run_turn(session["id"])["id"] == saved["id"]
    assert turns.get_latest_started_turn(session["id"])["id"] == saved["id"]
    assert limit_resume._last_settled_turn(session["id"])["id"] == saved["id"]
    assert limit_resume.hold_until(session["id"]) is None
    wake = await turn_scheduler._enqueue_context_wake(session["id"], "wakeup", [], [], "continue safely")
    assert wake["id"]
    assert wake["overrides"] == overrides
    assert wake["parent_turn_id"] == saved["id"]
    assert wake["payload"]["api_user_id"] == "user"
    assert recovered["ended_at"] is None
    assert turns.get_turn(older["id"])["state"] == "completed"


def test_recovery_does_not_rewrite_events_or_create_fresh_session_activity(session):
    from app.db import session_events

    started = datetime.now(timezone.utc) - timedelta(days=1)
    saved = begin(session, started)
    session_events.append_events(session["id"], [{
        "type": "assistant.text_delta", "turn_id": saved["id"], "payload": {"text": "already durable"},
    }])
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id=%s ORDER BY seq", (session["id"],))
        events = cursor.fetchall()
    before = database.get_session(session["id"])
    assert recover() == 1
    terminal = turns.get_turn(saved["id"])
    # There is no known completion timestamp that could precede its own events.
    assert terminal["ended_at"] is None
    assert turns._to_utc(terminal["created_at"]) == turns._to_utc(terminal["started_at"])
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM session_events WHERE session_id=%s ORDER BY seq", (session["id"],))
        assert cursor.fetchall() == events
    after = database.get_session(session["id"])
    unchanged = ("updated_at", "work_ms", "work_started_at", "total_cost_usd",
                 "total_tokens_in", "total_tokens_out", "turn_count", "last_user_message_at")
    assert {k: after[k] for k in unchanged} == {k: before[k] for k in unchanged}


@pytest.mark.parametrize("normal_first", [True, False])
def test_recovery_and_normal_terminal_race_has_one_identity_and_one_work_result(session, normal_first):
    saved = begin(session, datetime.now(timezone.utc) - timedelta(days=1))
    started = datetime.now(timezone.utc) - timedelta(seconds=2)
    before = database.get_session(session["id"])["work_ms"]
    if normal_first:
        terminal = turns.record_adhoc_turn(session["id"], "api_query", turn_id=saved["id"], started_at=started)
        # A stale replay reader may already have fetched this receipt.
        repeated = turns.record_adhoc_turn(session["id"], "api_query", turn_id=saved["id"],
                                           started_at=started, recovered=True, outcome="aborted")
        assert repeated == terminal
        expected = before + turns.interval_ms(terminal["started_at"], terminal["ended_at"])
        assert expected > before
    else:
        assert recover() == 1
        terminal = turns.get_turn(saved["id"])
        repeated = turns.record_adhoc_turn(session["id"], "api_query", turn_id=saved["id"], started_at=started)
        assert repeated == terminal
        expected = before
    assert recover() == 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM turns WHERE session_id=%s", (session["id"],))
        assert len(cursor.fetchall()) == 1
    assert database.get_session(session["id"])["work_ms"] == expected


def test_historical_recovery_slots_preserve_existing_ordinals_and_future_admission(session):
    base = datetime.now(timezone.utc) - timedelta(days=1)
    first = turns.record_adhoc_turn(session["id"], "api_query", started_at=base)
    second = turns.record_adhoc_turn(session["id"], "api_query", started_at=base + timedelta(hours=2))
    with get_db() as conn:
        cursor = conn.cursor()
        for row, end in ((first, base + timedelta(seconds=1)), (second, base + timedelta(hours=2, seconds=1))):
            cursor.execute("UPDATE turns SET created_at=%s, ended_at=%s WHERE id=%s", (end, end, row["id"]))
    middle = begin(session, base + timedelta(hours=1))
    earliest = begin(session, base - timedelta(hours=1))
    assert recover() == 2
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM turns WHERE session_id=%s "
                       "ORDER BY insert_order, created_at, position, id", (session["id"],))
        assert [r["id"] for r in cursor.fetchall()] == [earliest["id"], first["id"], middle["id"], second["id"]]
    assert turns.get_turn(first["id"])["insert_order"] == first["insert_order"]
    assert turns.get_turn(second["id"])["insert_order"] == second["insert_order"]
    future = turns.enqueue_turn(session["id"], "user", prompt="next input")
    assert future["insert_order"] > second["insert_order"]
    assert turns.get_latest_turn(session["id"])["id"] == future["id"]


def test_equal_time_prefers_observed_completion_over_unknown_recovery(session):
    from app.core.limit_resume import _last_settled_turn

    normal = turns.record_adhoc_turn(session["id"], "api_query")
    stamp = turns._to_utc(normal["ended_at"])
    # PostgreSQL compares TIMESTAMPTZ instants; SQLite's shim otherwise compares
    # a NOW() string without an offset against the adapted aware datetime text.
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET ended_at=%s, created_at=%s WHERE id=%s",
                              (stamp, stamp, normal["id"]))
    saved = begin(session, stamp, overrides={"model": "unknown"})
    assert recover() == 1
    context = _latest_context_turn(session["id"])
    assert context["id"] == normal["id"], (context, normal)
    assert _last_settled_turn(session["id"])["id"] == normal["id"]
    assert turns.get_latest_turn(session["id"])["id"] == normal["id"]
    assert turns.get_turn(saved["id"])["ended_at"] is None


def test_unknown_recovery_time_is_not_reintroduced_by_work_backfill(session, monkeypatch):
    saved = begin(session, datetime.now(timezone.utc) - timedelta(days=1))
    assert recover() == 1
    with monkeypatch.context() as patch:
        patch.setattr(database, "_get_marker", lambda *args: False)
        with get_db() as conn:
            database._backfill_sessions_work_ms(conn.cursor())
    assert database.get_session(session["id"])["work_ms"] == 0
    assert turns.get_turn(saved["id"])["ended_at"] is None


def test_receipt_without_legacy_start_stays_behind_known_context(session):
    saved = begin(session, None, overrides={"model": "unknown-old-model"})
    newer = turns.record_adhoc_turn(session["id"], "api_query", overrides={"model": "new-model"})
    # Same instant/type normalization as the explicit equal-time test above.
    stamp = turns._to_utc(newer["ended_at"])
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET ended_at=%s, created_at=%s WHERE id=%s",
                              (stamp, stamp, newer["id"]))
    assert recover() == 1
    assert _latest_context_turn(session["id"])["id"] == newer["id"]
    assert turns.get_latest_turn(session["id"])["id"] == newer["id"]
    assert turns.get_turn(saved["id"])["ended_at"] is None
