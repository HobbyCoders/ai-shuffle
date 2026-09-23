"""Peer messages to a parked session are held, never lost.

The end-to-end paths (hold, unpark delivery, urgent, durable promotion) run
against a real Task worker in ``test_agent_messaging_producer.py``. These
cover the scheduler's edges: a message that arrives while the unpark wake is
already queued, a wake whose tasks the user cancels, and a user message
arriving while messages are held.
"""

import pytest
import pytest_asyncio

from app.core import turn_scheduler, wakeup_service
from app.db import turns as turns_db
from app.db import wakeups as wakeups_db


@pytest_asyncio.fixture(autouse=True)
async def _quiet(monkeypatch):
    kicks = []
    monkeypatch.setattr(turn_scheduler, "kick", kicks.append)
    yield kicks
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()


def _parked(session_id):
    turn = turns_db.enqueue_turn(
        session_id, "user", prompt="Delegate", profile_id="profile",
        overrides={"model": "m"},
    )
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.park_turn(turn["id"], ["w1", "w2"])
    return turn


def _frame(task_id):
    return {"task_id": task_id, "status": "completed", "summary": f"{task_id} done"}


def _queued_unpark(session_id, *task_ids):
    frames = [_frame(t) for t in task_ids]
    return turns_db.enqueue_turn(
        session_id, "unpark",
        prompt=turn_scheduler._build_task_notification_prompt(frames),
        profile_id="profile",
        payload={"pending_frames": frames, "task_ids": list(task_ids)},
    )


def _held(session_id):
    return wakeups_db.get_session_wakeups(session_id, wakeup_service.HELD_PEER_SOURCE)


async def test_a_message_while_the_unpark_is_queued_rides_it(mock_db, _quiet):
    _parked("s1")
    wake = _queued_unpark("s1", "w1")

    assert await turn_scheduler.on_agent_message("s1", "w2", "halfway") == turn_scheduler.HELD

    (row,) = turns_db.get_queued_turns("s1")
    assert row["id"] == wake["id"]
    assert "w1 done" in row["prompt"]
    assert "halfway" in row["prompt"][row["prompt"].index("<system-reminder>"):]
    assert "halfway" in row["payload"]["held_peer_messages"]
    assert _held("s1") == []
    assert _quiet == []


async def test_a_message_to_a_session_that_is_not_parked_still_queues_a_turn(mock_db, _quiet):
    turn_id = await turn_scheduler.on_agent_message("s2", "w1", "hello")

    assert turn_id and turn_id != turn_scheduler.HELD
    (row,) = turns_db.get_queued_turns("s2")
    assert row["source"] == "agent_message"
    assert _held("s2") == []
    assert _quiet == ["s2"]


async def test_cancelling_some_tasks_keeps_the_held_message_in_the_wake(mock_db):
    _parked("s3")
    _queued_unpark("s3", "w1", "w2")
    assert await turn_scheduler.on_agent_message("s3", "w9", "note") == turn_scheduler.HELD

    assert turn_scheduler.discard_queued_task_wakes("s3", ["w1"]) == 1

    (row,) = turns_db.get_queued_turns("s3")
    assert "w1 done" not in row["prompt"] and "w2 done" in row["prompt"]
    assert "note" in row["prompt"]


async def test_cancelling_every_task_puts_the_held_message_back(mock_db):
    _parked("s4")
    _queued_unpark("s4", "w1")
    assert await turn_scheduler.on_agent_message("s4", "w9", "note") == turn_scheduler.HELD

    assert turn_scheduler.discard_queued_task_wakes("s4", ["w1"]) == 1

    assert turns_db.get_queued_turns("s4") == []
    (held,) = _held("s4")
    assert "note" in held["prompt"]


async def test_a_user_message_supersedes_armed_wakes_but_keeps_held_messages(mock_db):
    from app.core import turn_manager

    _parked("s5")
    assert await turn_scheduler.on_agent_message("s5", "w1", "note") == turn_scheduler.HELD
    await wakeup_service.schedule(
        session_id="s5", profile_id="profile", project_id=None,
        prompt="check back", delay_seconds=600,
    )

    turn_manager._submit_blocking("s5", "user", prompt="hi", profile_id="profile")

    assert not wakeups_db.session_has_pending("s5", "schedule_wakeup")
    (held,) = _held("s5")
    assert "note" in held["prompt"]


async def test_a_promotion_that_fires_while_a_message_is_held_is_not_delivered_twice(
    mock_db, monkeypatch,
):
    """The promotion timer claims the held row outside the scheduler's lock.
    A message held at that moment must not copy the claimed text into the
    new row, or the promotion delivers it and the next wake does again."""
    _parked("s6")
    assert await turn_scheduler.on_agent_message("s6", "w1", "first") == turn_scheduler.HELD
    (row,) = _held("s6")

    real = turn_scheduler._latest_context_turn

    def claim_then_read(session_id):
        # The timer fires between the hold's read and its write.
        assert wakeups_db.claim_wakeup(row["id"])
        return real(session_id)

    monkeypatch.setattr(turn_scheduler, "_latest_context_turn", claim_then_read)
    assert await turn_scheduler.on_agent_message("s6", "w1", "second") == turn_scheduler.HELD

    (held,) = _held("s6")
    assert "second" in held["prompt"]
    assert "first" not in held["prompt"]


def _fail_enqueue(*args, **kwargs):
    raise RuntimeError("database went away")


async def test_held_messages_survive_an_unpark_that_fails_to_enqueue(mock_db, monkeypatch):
    _parked("s7")
    assert await turn_scheduler.on_agent_message("s7", "w1", "note") == turn_scheduler.HELD

    monkeypatch.setattr(turn_scheduler, "_enqueue", _fail_enqueue)
    frame = _frame("w1")
    with pytest.raises(RuntimeError):
        await turn_scheduler._enqueue_context_wake(
            "s7", "unpark", [frame], ["w1"],
            turn_scheduler._build_task_notification_prompt([frame]),
        )

    (held,) = _held("s7")
    assert "note" in held["prompt"]


async def test_held_messages_survive_an_urgent_message_that_fails_to_enqueue(
    mock_db, monkeypatch,
):
    _parked("s8")
    assert await turn_scheduler.on_agent_message("s8", "w1", "note") == turn_scheduler.HELD

    monkeypatch.setattr(turn_scheduler, "_enqueue", _fail_enqueue)
    with pytest.raises(RuntimeError):
        await turn_scheduler.on_agent_message("s8", "w1", "blocked", urgent=True)

    (held,) = _held("s8")
    assert "note" in held["prompt"]
    assert "blocked" not in held["prompt"]
