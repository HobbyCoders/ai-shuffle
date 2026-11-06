"""Archive retirement uses ordinary settlement and explicit fresh admission.

These replace read-only-until-Restore tests: a converted chat has no extra
runtime gate, but conversion, opening and legacy aliases must never clear Stop.
"""
from unittest.mock import AsyncMock
import pytest
from app.core import archive_guard, session_orchestration as orch, turn_manager, turn_scheduler
from app.core.task_registry import task_registry
from app.db import database, sessions, turns
from app.db.base import get_db
from app.db.session_archive_migration import migrate, reconcile


@pytest.fixture
def converted(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    for sid in ("caller", "old"):
        sessions.create_session(sid, "agent", title=sid, is_active=True)
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET status='archived' WHERE id='old'")
        migrate(cur)
    return "old"


async def test_converted_chat_has_no_readonly_gate(converted):
    row = sessions.get_session(converted)
    assert row["status"] == "active" and not row["is_active"]
    assert not archive_guard.is_archived_row(row)
    assert not archive_guard.is_archived(converted)
    assert not await archive_guard.is_archived_async(converted)
    # Even an old queued payload cannot turn the retired decoder into a gate.
    assert not archive_guard.is_archived_row({"status": "archived"})


async def test_migration_reconcile_preserves_stop_and_never_submits(converted, monkeypatch):
    task_registry.mark_user_stopped(converted)
    submit = AsyncMock(side_effect=AssertionError("migration must never submit work"))
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    await reconcile()
    await reconcile()
    assert task_registry.is_user_stopped(converted)
    assert turns.get_queued_turns(converted) == []
    assert not sessions.get_session(converted)["is_active"]
    submit.assert_not_called()


@pytest.mark.parametrize("source", ["user", "queue", "api", "resume"])
async def test_only_explicit_send_reactivates_with_fresh_turn(converted, source):
    tid = await turn_manager.submit_async(converted, source, prompt="fresh request", profile_id="agent")
    assert [row["id"] for row in turns.get_queued_turns(converted)] == [tid]
    assert sessions.get_session(converted)["is_active"]
    assert sessions.get_session(converted)["status"] == "active"


async def test_legacy_alias_does_not_stop_running_work_or_clear_stop(converted):
    from app.network.session_active import update_membership
    await update_membership(sessions.get_session(converted), True)
    live = turns.enqueue_turn(converted, "user", prompt="long job", profile_id="agent")
    assert turns.start_turn(live["id"])
    before = turns.get_turn(live["id"])
    task_registry.mark_user_stopped(converted)
    for archived in (True, False):
        row = await update_membership(sessions.get_session(converted), False, archived)
        assert row["status"] == "active" and not row["is_active"]
        assert turns.get_turn(live["id"]) == before
        assert task_registry.is_user_stopped(converted)


async def test_open_and_list_history_are_inert(converted):
    before = sessions.get_session(converted)
    assert [row["id"] for row in orch.list_sessions("caller", {"state":"settled"})["sessions"]] == [converted]
    assert sessions.get_session(converted) == before
    assert turns.get_queued_turns(converted) == []
