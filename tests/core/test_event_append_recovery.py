"""Recovery covers standalone writers without replaying uncertain commits."""
import asyncio
from unittest.mock import Mock

import psycopg2
import pytest
from psycopg2.errors import LockNotAvailable

from app.core import interaction_events, turn_recorder
from app.db import session_events as events, turns
from tests.shared_fixtures import seed_session


@pytest.fixture(autouse=True)
def session(mock_db, monkeypatch):
    seed_session("recovery-chat")
    monkeypatch.setattr(events, "LOCK_RETRY_DELAYS_SECONDS", (0, 0))
    monkeypatch.setattr(turn_recorder, "_LOCK_RETRY_DELAYS_SECONDS", (0, 0))


def test_sync_retry_rechecks_fence_and_preserves_result(monkeypatch):
    append = Mock(side_effect=[LockNotAvailable(), (4, 5)])
    monkeypatch.setattr(events, "append_events", append)
    batch = [{"type": "swarm.updated", "payload": {}}]
    assert events.append_events_with_lock_retry("recovery-chat", batch, "run", 7) == (4, 5)
    assert append.call_count == 2
    for call in append.call_args_list:
        assert call.args == ("recovery-chat", batch)
        assert call.kwargs == {"run_id": "run", "fence_token": 7}


@pytest.mark.parametrize("error", [
    psycopg2.OperationalError("lost acknowledgement"),
    psycopg2.errors.QueryCanceled("statement timeout"),
    events.FenceViolation("superseded"),
])
def test_sync_unknown_outcomes_and_stale_fences_never_retry(monkeypatch, error):
    append = Mock(side_effect=error)
    monkeypatch.setattr(events, "append_events", append)
    with pytest.raises(type(error)):
        events.append_events_with_lock_retry("recovery-chat", [{"type": "system.note"}])
    assert append.call_count == 1


def test_sync_lock_retry_is_bounded(monkeypatch):
    append = Mock(side_effect=LockNotAvailable())
    monkeypatch.setattr(events, "append_events", append)
    with pytest.raises(LockNotAvailable):
        events.append_events_with_lock_retry("recovery-chat", [{"type": "system.note"}])
    assert append.call_count == 3


async def test_requested_answered_expired_stay_ordered_after_timeout(monkeypatch):
    real = events.append_events
    calls = 0

    def append(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LockNotAvailable()
        return real(*args, **kwargs)

    monkeypatch.setattr(events, "append_events", append)
    await interaction_events.emit_interaction_requested("recovery-chat", "interaction", "request", "permission")
    await interaction_events.emit_interaction_answered("recovery-chat", "interaction", "request", {"decision": "deny"})
    await interaction_events.emit_interaction_expired("recovery-chat", "interaction", "request", "cancelled")
    tail = turn_recorder._live_unfenced_write("recovery-chat")
    if tail:
        await tail.task
    rows = events.get_events("recovery-chat")
    assert [row["event_type"] for row in rows] == [
        "interaction.requested", "interaction.answered", "interaction.expired",
    ]
    assert len({row["id"] for row in rows}) == 3


def test_recording_failure_commits_one_terminal_with_aborted_row():
    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], None, "runner")
    events.append_events("recovery-chat", [{
        "type": "assistant.text_delta", "turn_id": turn["id"], "payload": {"text": "partial"},
    }])
    assert turns.settle_turn(turn["id"], "aborted", "recording_failed")
    assert turns.settle_turn(turn["id"], "aborted", "recording_failed") is None
    rows = events.get_events("recovery-chat")
    assert [r["event_type"] for r in rows] == ["assistant.text_delta", "turn.aborted"]
    assert rows[-1]["payload"]["reason"] == "error"
    assert rows[-1]["payload"]["stop_reason"] == "recording_failed"
    assert rows[-1]["turn_id"] == turn["id"]


@pytest.mark.parametrize("boundary", ["lock", "terminal"])
def test_terminal_rollback_retries_whole_transaction_without_reexecution(monkeypatch, boundary):
    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], None, "runner")
    target, attribute = ((turns, "_lock_for_event_write") if boundary == "lock"
                         else (events, "append_events"))
    real = getattr(target, attribute)
    attempts = 0

    def timeout_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LockNotAvailable()
        # The lock precedes UPDATE; the terminal follows the retried UPDATE.
        assert turns.get_turn(turn["id"])["state"] == ("running" if boundary == "lock" else "aborted")
        assert events.get_events("recovery-chat") == []
        return real(*args, **kwargs)

    monkeypatch.setattr(target, attribute, timeout_once)
    assert turns.settle_turn(turn["id"], "aborted", "recording_failed")
    assert attempts == 2
    assert turns.get_turn(turn["id"])["state"] == "aborted"
    assert turns.reap_expired() == []
    rows = events.get_events("recovery-chat")
    assert len(rows) == 1
    assert rows[0]["id"] == "recording-failed:" + turn["id"]


def test_exhausted_terminal_recovery_still_rolls_back_and_can_be_requeued(monkeypatch):
    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], None, "runner")
    append = Mock(side_effect=LockNotAvailable())
    monkeypatch.setattr(events, "append_events", append)
    with pytest.raises(LockNotAvailable):
        turns.settle_turn(turn["id"], "aborted", "recording_failed")
    assert append.call_count == 3
    assert turns.get_turn(turn["id"])["state"] == "running"
    assert events.get_events("recovery-chat") == []
    # Bounded recovery is not a durable failure outbox: if EVERY attempt fails,
    # dead-lease recovery can still requeue this user prompt. Do not hide it.
    assert [row["id"] for row in turns.reap_expired()] == [turn["id"]]
    assert turns.get_turn(turn["id"])["state"] == "queued"


@pytest.mark.parametrize("error", [psycopg2.OperationalError(), psycopg2.errors.QueryCanceled()])
def test_terminal_uncertain_outcome_is_not_retried(monkeypatch, error):
    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], None, "runner")
    append = Mock(side_effect=error)
    monkeypatch.setattr(events, "append_events", append)
    with pytest.raises(type(error)):
        turns.settle_turn(turn["id"], "aborted", "recording_failed")
    assert append.call_count == 1


async def test_deferred_reask_has_a_new_request_generation():
    from app.db import interactions

    payload = {"request_id": "same-request", "tool_name": "Bash", "tool_input": {"command": "pwd"},
               "assistant_confirm": {"question": "May I run pwd? Yes or no."}}
    first = interactions.create_interaction("recovery-chat", "permission", payload, None)
    await interaction_events.emit_interaction_requested(
        "recovery-chat", first["id"], "same-request", "permission", payload)
    assert interactions.answer_interaction(first["id"], {"decision": "defer"})
    await interaction_events.emit_interaction_answered(
        "recovery-chat", first["id"], "same-request", {"decision": "defer"})
    second = interactions.create_interaction("recovery-chat", "permission", payload, None)
    await interaction_events.emit_interaction_requested(
        "recovery-chat", second["id"], "same-request", "permission", payload)
    # An old delayed request cannot reopen the already settled generation.
    await interaction_events.emit_interaction_requested(
        "recovery-chat", first["id"], "same-request", "permission", payload)
    assert interactions.answer_interaction(second["id"], {"decision": "deny"})
    await interaction_events.emit_interaction_answered(
        "recovery-chat", second["id"], "same-request", {"decision": "deny"})
    rows = events.get_events("recovery-chat")
    assert [r["event_type"] for r in rows] == [
        "interaction.requested", "interaction.answered", "interaction.requested", "interaction.answered",
    ]
    assert [r["payload"]["interaction_id"] for r in rows] == [first["id"], first["id"], second["id"], second["id"]]


def test_runner_replaced_during_retry_cannot_publish_a_failure(monkeypatch):
    from app.db.base import get_db

    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], "old-run", "runner")
    lock = Mock(side_effect=[LockNotAvailable(), None])
    monkeypatch.setattr(turns, "_lock_for_event_write", lock)

    def takeover(delay):
        with get_db() as conn:
            conn.cursor().execute("UPDATE turns SET run_id='new-run' WHERE id=%s", (turn["id"],))

    monkeypatch.setattr(turns.time, "sleep", takeover)
    assert turns.settle_turn(turn["id"], "aborted", "recording_failed", run_id="old-run") is None
    assert lock.call_count == 2
    assert turns.get_turn(turn["id"])["run_id"] == "new-run"
    assert events.get_events("recovery-chat") == []


def test_stale_runner_cannot_publish_a_failure():
    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], None, "runner")
    assert turns.settle_turn(turn["id"], "aborted", "recording_failed", run_id="old-run") is None
    assert events.get_events("recovery-chat") == []
