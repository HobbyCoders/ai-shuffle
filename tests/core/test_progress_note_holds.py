"""Agent-message kinds (#960): progress notes stop waking idle or parked chats.

result, needs_decision and alert wake an idle or parked receiver at once. A
progress note is held, with no timer, and rides the receiver's next wake for
any reason, or is delivered when its sender's run ends. A busy receiver takes
every kind at its next tool boundary. A missing kind (an older caller or a
replicated row) and ``urgent`` are alert.
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
    turn_scheduler._delivered_held.clear()
    yield kicks
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    turn_scheduler._delivered_held.clear()


def _seed(session_id):
    from app.db import database

    database.create_session(session_id=session_id, profile_id=None, title="t")


def _parked(session_id):
    _seed(session_id)
    turn = turns_db.enqueue_turn(
        session_id, "user", prompt="Delegate", profile_id="profile", overrides={"model": "m"},
    )
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.park_turn(turn["id"], ["w1"])
    return turn


def _held(session_id):
    return wakeups_db.get_session_wakeups(session_id, wakeup_service.HELD_PEER_SOURCE)


async def _run_turn(turn):
    """Run ``turn`` through a real SessionRuntime, pump stubbed; return the
    prompt the engine was given."""
    from app.core import session_runtime
    from tests.core.test_send_steering import _pump_env

    seen = {}

    async def _impl(**kwargs):
        seen["prompt"] = kwargs.get("prompt")
        yield {"type": "done", "metadata": {}}

    with _pump_env(_impl):
        await session_runtime.SessionRuntime(turn).execute()
    return seen.get("prompt") or ""


@pytest.mark.parametrize("kind", ["result", "needs_decision", "alert", None])
async def test_actionable_kinds_and_missing_kind_wake_a_parked_receiver(mock_db, _quiet, kind):
    _parked("p1")
    turn_id = await turn_scheduler.on_agent_message("p1", "w1", "done: report attached", kind=kind)

    assert turn_id not in (None, turn_scheduler.HELD)
    (row,) = turns_db.get_queued_turns("p1")
    assert row["id"] == turn_id
    # Persisted, so restart and remote paths keep it; missing means alert.
    assert row["payload"][turn_scheduler.AGENT_MESSAGE_KIND_KEY] == (kind or "alert")
    assert _held("p1") == []
    assert _quiet == ["p1"]


async def test_urgent_progress_is_an_alert(mock_db, _quiet):
    _parked("p2")
    turn_id = await turn_scheduler.on_agent_message("p2", "w1", "stop editing x.py", urgent=True,
                                                    kind="progress")
    (row,) = turns_db.get_queued_turns("p2")
    assert row["id"] == turn_id and row["payload"]["agent_message_kind"] == "alert"


@pytest.mark.parametrize("parked", [True, False], ids=["parked", "idle"])
async def test_progress_is_held_without_a_timer(mock_db, _quiet, parked):
    if parked:
        _parked("p3")
    else:
        _seed("p3")
    assert await turn_scheduler.on_agent_message("p3", "w1", "tests 40/80", kind="progress") \
        == turn_scheduler.HELD

    # No turn runs for it, nothing is kicked, and no timer is armed.
    assert turns_db.get_queued_turns("p3") == []
    assert _quiet == []
    (row,) = _held("p3")
    assert "tests 40/80" in row["prompt"] and "sender=w1]" in row["prompt"]
    assert wakeup_service.never_fires(row)
    assert not wakeup_service.is_armed(row["id"])
    # The queued row it came from is retired as held, not left pending.
    (moved,) = [t for t in _turns("p3") if t["source"] == "agent_message"]
    assert moved["state"] == "aborted" and moved["stop_reason"] == turn_scheduler.HELD_PROGRESS_STOP_REASON


def _turns(session_id):
    from app.db.base import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s", (session_id,))
        return [turns_db._row(r) for r in cursor.fetchall()]


async def test_boot_rehydration_arms_no_timer_for_held_progress(mock_db):
    _parked("p4")
    await turn_scheduler.on_agent_message("p4", "w1", "halfway", kind="progress")
    wakeup_service._timers.clear()

    await wakeup_service.rehydrate_pending()
    (row,) = _held("p4")
    assert not wakeup_service.is_armed(row["id"])


async def test_held_notes_ride_the_next_wake_as_one_compact_block(mock_db, _quiet):
    _parked("p5")
    for text in ("brief read, cloning", "tests 40/80\npassing"):
        assert await turn_scheduler.on_agent_message("p5", "w1", text, kind="progress") \
            == turn_scheduler.HELD
    # An actionable message wakes the receiver; the held notes ride along.
    turn_id = await turn_scheduler.on_agent_message("p5", "w2", "blocked: may I push?",
                                                    kind="needs_decision")
    (wake,) = turns_db.get_queued_turns("p5")
    assert wake["id"] == turn_id

    prompt = await _run_turn(wake)
    assert prompt.count("<system-reminder>") == 1
    block = prompt[prompt.index("<system-reminder>"):]
    lines = [line for line in block.splitlines() if line.startswith("- from ")]
    assert lines == ["- from 'w1': brief read, cloning", "- from 'w1': tests 40/80 passing"]
    assert "blocked: may I push?" in prompt
    assert _held("p5") == []
    # Delivered once.
    assert await turn_scheduler.fold_held_peer_messages_for_turn("p5", {"id": "n", "payload": {}}) == ""


async def test_a_child_completion_wake_carries_held_notes(mock_db, _quiet):
    _parked("p6")
    await turn_scheduler.on_agent_message("p6", "w1", "halfway", kind="progress")

    frame = {"task_id": "w1", "status": "completed", "summary": "w1 done"}
    async with turn_scheduler._terminal_delivery_lock("p6"):
        await turn_scheduler._enqueue_context_wake(
            "p6", "unpark", [frame], ["w1"], turn_scheduler._build_task_notification_prompt([frame]),
        )
    (wake,) = turns_db.get_queued_turns("p6")
    assert "- from 'w1': halfway" in wake["prompt"]
    assert _held("p6") == []


async def test_a_busy_receiver_keeps_progress_for_its_next_tool_boundary(mock_db, _quiet):
    _seed("p7")
    live = turns_db.enqueue_turn("p7", "user", prompt="work", profile_id="profile")
    turns_db.start_turn(live["id"], None, "runner")

    turn_id = await turn_scheduler.on_agent_message("p7", "w1", "tests green", kind="progress")
    assert turn_id not in (None, turn_scheduler.HELD)
    (row,) = turns_db.get_queued_turns("p7")
    # Attributed for SessionRuntime.steer_pending, exactly as before.
    assert row["payload"]["task_peer_sender"] == "w1" and row["payload"]["agent_message_kind"] == "progress"
    assert _held("p7") == []


async def test_a_note_no_boundary_took_is_held_when_the_turn_ends(mock_db, _quiet):
    _seed("p8")
    live = turns_db.enqueue_turn("p8", "user", prompt="work", profile_id="profile")
    turns_db.start_turn(live["id"], None, "runner")
    await turn_scheduler.on_agent_message("p8", "w1", "tests green", kind="progress")
    turns_db.settle_turn(live["id"], "completed", stop_reason="end_turn")
    _quiet.clear()

    await turn_scheduler._on_turn_settled_impl("p8", live["id"], "completed")

    assert turns_db.get_queued_turns("p8") == []
    assert _quiet == []  # no turn of its own
    (row,) = _held("p8")
    assert "tests green" in row["prompt"]


async def test_a_queued_progress_row_never_starts_a_turn_on_a_kick(mock_db, _quiet, monkeypatch):
    """Restart recovery: a progress row still queued from before a crash is
    held by the next kick instead of running."""
    from app.core import session_runtime

    _seed("p9")
    turns_db.enqueue_turn("p9", "agent_message", prompt="note", profile_id="profile",
                          payload={"agent_message_kind": "progress", "task_peer_sender": "w1"})
    ran = []

    async def _execute(row):
        ran.append(row["id"])

    monkeypatch.setattr(session_runtime, "execute", _execute)
    await turn_scheduler._kick_impl("p9")
    assert ran == []
    assert turns_db.get_queued_turns("p9") == [] and len(_held("p9")) == 1


async def test_a_workers_notes_are_delivered_when_its_run_ends(mock_db, _quiet):
    from app.db import database

    _parked("r1")
    _seed("owner")
    database.create_session(session_id="worker-1", profile_id=None, title="w",
                            parent_session_id="owner")
    await turn_scheduler.on_agent_message("r1", "worker-1", "halfway", kind="progress")

    assert await turn_scheduler.release_held_progress_from("worker-1") == 1
    (wake,) = turns_db.get_queued_turns("r1")
    assert wake["source"] == "promotion"
    assert "delivered now because their sender stopped" in wake["prompt"]
    assert "- from 'worker-1': halfway" in wake["prompt"]
    assert _held("r1") == []
    assert _quiet == ["r1"]


@pytest.mark.parametrize("fork", [False, True], ids=["chat", "fork"])
async def test_a_chats_notes_wait_for_the_receivers_next_wake(mock_db, _quiet, fork):
    """A chat ending a turn is not a worker ending its run: its notes stay held."""
    from app.db import database

    _parked("r3")
    _seed("origin")
    database.create_session(session_id="peer-chat", profile_id=None, title="c",
                            parent_session_id="origin" if fork else None,
                            fork_point_message_index=3 if fork else None)
    await turn_scheduler.on_agent_message("r3", "peer-chat", "halfway", kind="progress")

    assert await turn_scheduler.release_held_progress_from("peer-chat") == 0
    assert turns_db.get_queued_turns("r3") == [] and len(_held("r3")) == 1
    assert _quiet == []


async def test_a_task_childs_notes_wait_for_its_completion_wake(mock_db, _quiet, monkeypatch):
    from app.core.task_registry import task_registry
    from app.db import database

    _parked("r2")
    database.create_session(session_id="w1", profile_id=None, title="w", parent_session_id="r2")
    await turn_scheduler.on_agent_message("r2", "w1", "halfway", kind="progress")
    monkeypatch.setattr(task_registry, "get_task",
                        lambda sid, tid, durable=False: {"id": tid} if (sid, tid) == ("r2", "w1") else None)

    # Its completion, failure or stop wakes r2 and carries them instead.
    assert await turn_scheduler.release_held_progress_from("w1") == 0
    assert turns_db.get_queued_turns("r2") == [] and len(_held("r2")) == 1


async def test_held_progress_rows_are_not_pending_peer_rows(mock_db):
    from app.api.chat_ws_handler import _v2_peer_queue_view

    _seed("v1")
    live = turns_db.enqueue_turn("v1", "user", prompt="work", profile_id="profile")
    turns_db.start_turn(live["id"], None, "runner")
    await turn_scheduler.on_agent_message("v1", "w1", "fyi", kind="progress")
    await turn_scheduler.on_agent_message("v1", "w1", "conflict on x.py", kind="alert")

    assert [row["content"] for row in _v2_peer_queue_view("v1")] == ["conflict on x.py"]


def test_kind_validation():
    from fluid.context.peer import message_kind

    assert message_kind(None) == "alert" and message_kind("") == "alert"
    assert message_kind("progress", urgent=True) == "alert"
    assert message_kind("result") == "result"
    with pytest.raises(ValueError):
        message_kind("fyi")
