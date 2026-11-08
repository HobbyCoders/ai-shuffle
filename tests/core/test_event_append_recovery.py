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


def test_failed_terminal_write_rolls_back_the_settlement(monkeypatch):
    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], None, "runner")
    monkeypatch.setattr(events, "append_events", Mock(side_effect=LockNotAvailable()))
    with pytest.raises(LockNotAvailable):
        turns.settle_turn(turn["id"], "aborted", "recording_failed")
    assert turns.get_turn(turn["id"])["state"] == "running"


def test_stale_runner_cannot_publish_a_failure():
    turn = turns.enqueue_turn("recovery-chat", "user", prompt="hello")
    turns.start_turn(turn["id"], None, "runner")
    assert turns.settle_turn(turn["id"], "aborted", "recording_failed", run_id="old-run") is None
    assert events.get_events("recovery-chat") == []
