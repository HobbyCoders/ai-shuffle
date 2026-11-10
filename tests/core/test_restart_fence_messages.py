"""Resume and message producers share the idle-restart admission fence (#1121).

``turn_scheduler.on_agent_message`` (local SendMessage resumes, limit resumes,
remote messages) restores a worker's checkout and re-registers it before its
turn is queued; the remote message endpoints and ``send_to_child`` (which
reopens tracking) admit before their first await too.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core import remote_task, restart_fence, turn_scheduler
from app.core.worker_checkout import WorkerCheckoutError

TOKEN = "m" * 32


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr("app.core.desktop_lifecycle.active_session_ids", lambda: set())
    monkeypatch.setattr("app.db.turns.get_cwd_failure", lambda sid: None)
    monkeypatch.setattr("app.core.archive_guard.is_archived_async", AsyncMock(return_value=False))
    monkeypatch.setattr("app.db.turns.get_live_turn", lambda sid: None)
    yield
    restart_fence._reset_for_tests()


def _blocked_restore(monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def restore(session_id, **kwargs):
        entered.set()
        await finish.wait()
        return True, None

    monkeypatch.setattr("app.core.worker_checkout.restore_worker_checkout_async", restore)
    locked = AsyncMock(return_value="turn-1")
    monkeypatch.setattr(turn_scheduler, "_on_agent_message_locked", locked)
    return entered, finish, locked


async def test_a_resume_is_refused_before_anything_while_a_restart_is_reserved(monkeypatch):
    entered, _, locked = _blocked_restore(monkeypatch)
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True
    with pytest.raises(WorkerCheckoutError, match="about to restart"):
        await turn_scheduler.on_agent_message("worker-1", "parent-1", "continue", reply_to="parent-1")
    assert not entered.is_set() and locked.await_count == 0


async def test_a_resume_in_flight_refuses_the_reservation_until_it_is_queued(monkeypatch):
    entered, finish, locked = _blocked_restore(monkeypatch)
    resume = asyncio.create_task(turn_scheduler.on_agent_message("worker-1", "parent-1", "continue"))
    await entered.wait()
    refused = await restart_fence.reserve(TOKEN)
    assert refused["reserved"] is False and restart_fence.in_flight() == ["agent message"]
    finish.set()
    assert await resume == "turn-1"
    assert restart_fence.in_flight() == []
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True


async def test_a_cancelled_resume_stays_busy_until_its_checkout_restore_ends(monkeypatch):
    entered, finish, locked = _blocked_restore(monkeypatch)
    resume = asyncio.create_task(turn_scheduler.on_agent_message("worker-1", "parent-1", "continue"))
    await entered.wait()
    resume.cancel()
    with pytest.raises(asyncio.CancelledError):
        await resume
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False, "the restore is still running"
    finish.set()
    for _ in range(100):
        if not restart_fence.in_flight():
            break
        await asyncio.sleep(0.01)
    assert locked.await_count == 0, "nothing was queued"
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True


def _message(message_id="msg-1"):
    return {"version": remote_task.WIRE_VERSION, "to": remote_task.TO_CHILD,
            "worker_session_id": "child-1", "message_id": message_id,
            "sender_session_id": "parent-1", "text": "continue", "urgent": False,
            "known_completion_id": None}


async def test_a_fenced_target_refuses_a_new_remote_message_but_answers_a_retry(monkeypatch):
    monkeypatch.setattr(remote_task, "_require_worker_member", lambda device: None)
    applied = AsyncMock(return_value={"status": "queued", "turn_id": "t"})
    monkeypatch.setattr(remote_task, "_message_child", applied)
    answers = {"msg-1": {"from_device_id": "dev-a", "worker_session_id": "child-1",
                         "answer": {"status": "queued", "turn_id": "t0"}}}
    monkeypatch.setattr("app.db.remote_tasks.message_answer", lambda mid: answers.get(mid))
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True
    retry = await remote_task.handle_task_message(_message("msg-1"), "dev-a")
    assert retry == {"status": "queued", "turn_id": "t0", "duplicate": True}
    with pytest.raises(ValueError, match="about to restart"):
        await remote_task.handle_task_message(_message("msg-2"), "dev-a")
    applied.assert_not_awaited()


async def test_a_remote_message_in_flight_holds_the_fence_until_answered(monkeypatch):
    monkeypatch.setattr(remote_task, "_require_worker_member", lambda device: None)
    monkeypatch.setattr("app.db.remote_tasks.message_answer", lambda mid: None)
    monkeypatch.setattr("app.db.remote_tasks.record_message", lambda *a, **k: None)
    gate = asyncio.Event()

    async def message_child(msg, from_device):
        await gate.wait()
        return {"status": "queued", "turn_id": "t"}

    monkeypatch.setattr(remote_task, "_message_child", message_child)
    handled = asyncio.create_task(remote_task.handle_task_message(_message(), "dev-a"))
    await asyncio.sleep(0.05)
    assert (await restart_fence.reserve(TOKEN))["reserved"] is False
    gate.set()
    assert (await handled)["duplicate"] is False
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True


async def test_send_to_child_is_refused_before_anything_while_reserved(monkeypatch):
    get_dispatch = AsyncMock()
    monkeypatch.setattr("app.db.remote_tasks.get_dispatch", get_dispatch)
    assert (await restart_fence.reserve(TOKEN))["reserved"] is True
    outcome = await remote_task.send_to_child(
        "parent-1", {"worker_session_id": "child-1", "name": "reviewer"}, "continue")
    assert outcome["is_error"] and "about to restart" in outcome["text"]
    get_dispatch.assert_not_called()
