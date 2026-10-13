"""Unit tests for app/core/idle_reader.py — the drain reader that settles a
``draining`` turn once its wedged client is safe to evict.

The session pool is faked; the turns/runs DB is exercised for real (pg_shim).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import idle_reader, session_runtime
from app.db import turns as turns_db


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    monkeypatch.setattr(idle_reader, "IDLE_READ_POLL_SECONDS", 0.01)
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime.register_scheduler(None)
    yield
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime.register_scheduler(None)


def _make_pool(*, alive=True):
    """A get_session_pool() stand-in whose peek() yields a drainable state."""
    state = MagicMock()
    state.adapter.is_alive.return_value = alive
    state.is_streaming = False
    state.idle_reader_active = False

    pool = MagicMock()
    pool.peek.return_value = state
    pool.remove_broken = AsyncMock()
    pool.remove_dead = AsyncMock()
    return pool, state


def _draining_turn(session_id):
    turn = turns_db.enqueue_turn(session_id, "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    return turn


async def test_drain_settles_aborted_and_evicts_once_the_wedge_resolves(mock_db):
    """Settling here keeps reap_expired from requeueing and RE-RUNNING the
    interrupted user turn after execute() released its lease."""
    turn = _draining_turn("s1")
    scheduler = MagicMock()
    session_runtime.register_scheduler(scheduler)
    pool, _ = _make_pool()
    wedge = MagicMock(side_effect=[False, False, True])

    with patch("app.core.session_pool.get_session_pool", return_value=pool), \
            patch("app.core.session_runtime._should_evict_after_escalation", wedge):
        await idle_reader.run_idle_reader("s1", drain_turn_id=turn["id"])

    assert wedge.call_count == 3
    pool.remove_broken.assert_awaited_once_with("s1")
    assert turns_db.get_turn(turn["id"])["state"] == "aborted"
    scheduler.assert_called_once_with("s1", turn["id"], "aborted")


async def test_drain_holds_the_eviction_guard_flag_while_it_runs(mock_db):
    turn = _draining_turn("s1")
    pool, state = _make_pool()
    seen = []

    def _wedge(_sid):
        seen.append(state.idle_reader_active)
        return True

    with patch("app.core.session_pool.get_session_pool", return_value=pool), \
            patch("app.core.session_runtime._should_evict_after_escalation", _wedge):
        await idle_reader.run_idle_reader("s1", drain_turn_id=turn["id"])

    assert seen == [True]
    assert state.idle_reader_active is False


async def test_no_drainable_client_leaves_the_turn_alone(mock_db):
    turn = _draining_turn("s1")
    pool = MagicMock()
    pool.peek.return_value = None

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await idle_reader.run_idle_reader("s1", drain_turn_id=turn["id"])

    assert turns_db.get_turn(turn["id"])["state"] != "aborted"
