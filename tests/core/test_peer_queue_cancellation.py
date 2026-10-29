"""Queued peer cancellations are explicit, durable, and never delivery guesses."""

from unittest.mock import AsyncMock

import pytest

from app.core import archive_guard, history_backfill, query_session_lifecycle, turn_manager, wakeup_service
from app.core import task_registry as registry_module
from app.db import database, profiles, session_events, sessions, turns
from app.db.base import get_db
from tests.shared_fixtures import reset_backfill_memo  # noqa: F401 (autouse)


@pytest.fixture
def legacy_recipient(mock_db, monkeypatch):
    database.create_profile("cancel-agent", "Agent", None, {})
    sessions.create_session("recipient", "cancel-agent", title="Recipient")
    monkeypatch.setattr(registry_module, "task_registry", registry_module.TaskRegistry())
    monkeypatch.setattr(wakeup_service, "cancel_session_wakeups_async", AsyncMock(return_value=0))
    return "recipient"


@pytest.fixture
def recipient(legacy_recipient):
    # A client can display projected rows only once its V2 log is initialized.
    session_events.append_events(legacy_recipient, [{
        "type": "system.note", "payload": {"content": "V2 feed initialized"},
    }])
    return legacy_recipient


def queued(session_id, source="agent_message"):
    return turns.enqueue_turn(session_id, source, prompt="Review complete", profile_id="cancel-agent")


def cancellations(session_id):
    return [turn_id for event in session_events.get_events(session_id)
            if event["event_type"] == "queue.cancelled"
            for turn_id in event["payload"]["cancelled_turn_ids"]]


async def test_stop_reports_only_successfully_cancelled_queued_peers(recipient):
    live = queued(recipient)
    turns.start_turn(live["id"], None, "runner")
    first, second = queued(recipient), queued(recipient)
    queued(recipient, "user")

    result = await query_session_lifecycle.enter_user_stop(recipient)

    assert result["queued_aborted"] == 3
    assert set(cancellations(recipient)) == {first["id"], second["id"]}
    assert live["id"] not in cancellations(recipient)
    await query_session_lifecycle.enter_user_stop(recipient)
    assert len(cancellations(recipient)) == 2


def test_archive_reports_cancelled_peers(recipient):
    peer = queued(recipient)
    assert archive_guard.drop_queued_turns(recipient) == 1
    assert cancellations(recipient) == [peer["id"]]


def test_explicit_queue_removal_reports_once(recipient):
    peer = queued(recipient)
    assert turn_manager.remove_queued(peer["id"]) is not None
    assert turn_manager.remove_queued(peer["id"]) is None
    assert cancellations(recipient) == [peer["id"]]


@pytest.mark.parametrize("delivery", ["steer", "start", "abort_live"])
def test_delivery_does_not_report_cancellation(recipient, delivery):
    peer = queued(recipient)
    if delivery == "steer":
        assert turns.absorb_queued_turn(peer["id"])
    else:
        assert turns.start_turn(peer["id"], None, "runner") is not None
        if delivery == "abort_live":
            assert turns.settle_turn(peer["id"], "aborted", "interrupt") is not None
    # A stale Stop/removal racing a delivered or started row loses the guard.
    assert turns.remove_queued_turn(peer["id"]) is None
    assert cancellations(recipient) == []


def test_other_queued_peer_abort_reports_cancellation(recipient):
    peer = queued(recipient)
    settled = turns.settle_turn(peer["id"], "aborted", "admission_failed", error="Cannot start")
    assert settled["error"] == "Cannot start"
    assert cancellations(recipient) == [peer["id"]]


def test_stale_owner_cannot_cancel_a_requeued_peer(recipient):
    peer = queued(recipient)
    assert turns.settle_turn(peer["id"], "aborted", "interrupt", run_id="old-owner") is None
    assert cancellations(recipient) == []
    assert [row["id"] for row in turns.get_queued_turns(recipient)] == [peer["id"]]


def test_retired_builtin_reports_queued_peers_but_not_delivered_parks(recipient):
    database.create_profile("Explore", "Explore", None, {})
    profiles.set_profile_builtin("Explore", True)
    parked = turns.enqueue_turn(recipient, "agent_message", prompt="Delivered", profile_id="Explore")
    turns.start_turn(parked["id"], None, "runner")
    turns.park_turn(parked["id"], [])
    peer = turns.enqueue_turn(recipient, "agent_message", prompt="Queued", profile_id="Explore")

    assert profiles.retire_builtin_subagent("Explore")
    assert cancellations(recipient) == [peer["id"]]
    assert turns.get_queued_turns(recipient) == []


@pytest.mark.parametrize("zero_counter", [False, True])
@pytest.mark.parametrize("removal", ["stop", "archive", "remove", "settle", "retire_builtin"])
async def test_legacy_history_survives_cancellation_then_backfill(legacy_recipient, zero_counter, removal):
    sid = legacy_recipient
    profile_id = "cancel-agent"
    if removal == "retire_builtin":
        database.create_profile("Explore", "Explore", None, {})
        profiles.set_profile_builtin("Explore", True)
        sid, profile_id = "legacy-builtin-recipient", "Explore"
        sessions.create_session(sid, profile_id, title="Legacy built-in chat")
    sessions.add_session_message(sid, "user", "Legacy question", profile_id=profile_id)
    sessions.add_session_message(sid, "assistant", "Legacy reply", profile_id=profile_id)
    if zero_counter:
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO session_event_counters (session_id,next_seq) VALUES (%s,0)", (sid,),
            )
    if removal == "retire_builtin":
        peer = turns.enqueue_turn(sid, "agent_message", prompt="Queued", profile_id="Explore")
        assert profiles.retire_builtin_subagent("Explore")
    else:
        peer = queued(sid)
        if removal == "stop":
            assert (await query_session_lifecycle.enter_user_stop(sid))["queued_aborted"] == 1
        elif removal == "archive":
            assert archive_guard.drop_queued_turns(sid) == 1
        elif removal == "settle":
            assert turns.settle_turn(peer["id"], "aborted", "admission_failed") is not None
        else:
            assert turn_manager.remove_queued(peer["id"]) is not None

    assert turns.get_queued_turns(sid) == []
    assert session_events.get_events(sid) == []
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT next_seq FROM session_event_counters WHERE session_id=%s", (sid,))
        counter = cursor.fetchone()
    # Cancellation may reserve the lock row, but must not allocate a single seq.
    assert counter["next_seq"] == 0
    await history_backfill.ensure_backfilled(sid)
    events = session_events.get_events(sid)
    assert [event["event_type"] for event in events] == ["message.user", "message.assistant"]
    assert [event["payload"]["content"] for event in events] == ["Legacy question", "Legacy reply"]
    assert all(event["turn_id"] == "backfill" for event in events)


def test_existing_log_keeps_history_and_appends_cancellation(recipient):
    before = session_events.get_events(recipient)
    peer = queued(recipient)
    assert turns.remove_queued_turn(peer["id"]) is not None
    after = session_events.get_events(recipient)
    assert after[:-1] == before
    assert after[-1]["event_type"] == "queue.cancelled"
    assert after[-1]["payload"]["cancelled_turn_ids"] == [peer["id"]]


def test_cancellation_and_event_commit_together(recipient, monkeypatch):
    peer = queued(recipient)

    def fail_append(*args, **kwargs):
        raise RuntimeError("event write failed")

    monkeypatch.setattr(session_events, "append_events", fail_append)
    with pytest.raises(RuntimeError, match="event write failed"):
        turns.remove_queued_turn(peer["id"])
    assert [row["id"] for row in turns.get_queued_turns(recipient)] == [peer["id"]]
    assert cancellations(recipient) == []
