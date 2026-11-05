"""Finished Settled history does not hold resources; unfinished work still does."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.db import database, turns
from app.db.base import get_db
from app.core import checkout_lock, session_pr_state, turn_finalize
from app.core.session_reservations import holds_resources
from app.core.task_registry import task_registry


@pytest.fixture
def history(mock_db):
    database.create_profile("agent", "Agent", None, {})
    for sid in ("history", "caller", "child"):
        database.create_session(sid, "agent", is_active=False)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET parent_session_id='history' WHERE id='child'")
    return database.get_session("history")


def test_idle_history_does_not_hold_resources(history):
    assert not holds_resources("history")
    database.set_session_active("history", True)
    assert not holds_resources("history")  # Moving a row is not starting work.


@pytest.mark.parametrize("sid", ["history", "child"])
def test_settled_running_and_child_work_keep_reservation(history, sid):
    turn = turns.enqueue_turn(sid, "user", prompt="work", profile_id="agent")
    assert holds_resources("history")
    assert turns.start_turn(turn["id"])
    assert holds_resources("history")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
    assert not holds_resources("history")


def test_unresolved_monitor_launch_keeps_reservation(history):
    task_registry.mark_background_launch("history", "monitor-launch")
    assert holds_resources("history")


def test_rebind_and_pr_claim_ignore_idle_history_but_not_running(history, monkeypatch):
    row = {**history, "project_v2_id":"project", "branch":"main", "pr_number":42}
    monkeypatch.setattr(database, "list_sessions_for_v2_branch", lambda *a: [row])
    monkeypatch.setattr(database, "list_sessions_by_pr_number", lambda *a: [row])
    caller = {"id":"caller", "project_v2_id":"project"}
    assert not turn_finalize._checkout_shared_with_another_session(caller, "project", "main")
    assert not session_pr_state._pull_claimed_elsewhere(SimpleNamespace(number=42), caller, None)
    turns.enqueue_turn("history", "user", prompt="work", profile_id="agent")
    assert turn_finalize._checkout_shared_with_another_session(caller, "project", "main")
    assert session_pr_state._pull_claimed_elsewhere(SimpleNamespace(number=42), caller, None)
