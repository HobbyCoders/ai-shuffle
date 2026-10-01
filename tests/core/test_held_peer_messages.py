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
    # Attributed, so a live turn can take it at a tool boundary (#1618).
    assert row["payload"]["task_peer_sender"] == "w1"
    assert row["payload"]["peer_message_content"] == "hello"
    assert _held("s2") == []
    assert _quiet == ["s2"]


async def test_an_urgent_message_carrying_held_ones_is_not_a_single_peer_steer(mock_db, _quiet):
    """The peer channel carries one message; a row that also delivers held
    messages must run whole, never be steered as its newest message only."""
    _parked("s9")
    assert await turn_scheduler.on_agent_message("s9", "w1", "note") == turn_scheduler.HELD
    await turn_scheduler.on_agent_message("s9", "w2", "blocked", urgent=True)

    (row,) = turns_db.get_queued_turns("s9")
    assert "note" in row["payload"]["held_peer_messages"]
    assert "task_peer_sender" not in row["payload"]
    assert "peer_message_content" not in row["payload"]


def test_held_peer_deliveries_attribute_each_message_once():
    from fluid.context.peer import format_peer_message

    held = turn_scheduler._join_held(
        turn_scheduler._held_entry(format_peer_message("w1", "one").strip()),
        turn_scheduler._held_entry(format_peer_message("w2", "two\n\nparas").strip()),
    )
    assert turn_scheduler.held_peer_deliveries(held) == [
        {"sender": "w1", "content": "one"},
        {"sender": "w2", "content": "two\n\nparas"},
    ]
    assert turn_scheduler.held_peer_deliveries("") == []


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


# ---------------------------------------------------------------------------
# Held messages ride the parent's next turn, however it starts
# ---------------------------------------------------------------------------

async def _run_turn(turn, notes=None):
    """Run ``turn`` through a real SessionRuntime with the pump stubbed;
    return the prompt the engine was given. ``notes`` collects the
    ``agent_message`` events the turn recorded."""
    from app.core import session_runtime
    from tests.core.test_send_steering import _pump_env

    seen = {}

    async def _impl(**kwargs):
        seen["prompt"] = kwargs.get("prompt")
        yield {"type": "done", "metadata": {}}

    with _pump_env(_impl):
        recorder = session_runtime.TurnRecorder.create_for_run.return_value
        await session_runtime.SessionRuntime(turn).execute()
    if notes is not None:
        notes.extend(
            call.args[0]["data"] for call in recorder.on_event.await_args_list
            if call.args[0].get("subtype") == "agent_message"
        )
    return seen.get("prompt") or ""


def _seed(session_id):
    from app.db import database

    database.create_session(session_id=session_id, profile_id=None, title="t")


@pytest.fixture
def _clean_delivered():
    getattr(turn_scheduler, "_delivered_held", {}).clear()
    yield
    getattr(turn_scheduler, "_delivered_held", {}).clear()


async def test_a_user_turn_carries_held_messages_once(mock_db, _clean_delivered):
    _seed("g1")
    _parked("g1")
    assert await turn_scheduler.on_agent_message("g1", "w1", "halfway") == turn_scheduler.HELD

    first = turns_db.enqueue_turn("g1", "user", prompt="how is it going?")
    notes = []
    prompt = await _run_turn(first, notes)
    assert prompt.startswith("how is it going?")
    assert prompt.count("halfway") == 1
    assert "[held-peer-message" not in prompt
    assert _held("g1") == []
    # #1618: the held message is shown once, attributed, instead of only
    # riding the engine prompt where the user never sees it.
    assert notes == [{"sender": "w1", "content": "halfway", "delivery": "delivered"}]

    second = turns_db.enqueue_turn("g1", "user", prompt="and now?")
    notes = []
    assert "halfway" not in await _run_turn(second, notes)
    assert notes == []


def _events(session_id):
    from app.db import session_events

    return session_events.get_events(session_id)


def _agent_notes(session_id):
    return [
        {k: e["payload"]["data"][k] for k in ("sender", "content", "delivery")}
        for e in _events(session_id)
        if e["event_type"] == "system.note" and e["payload"].get("subtype") == "agent_message"
    ]


async def test_a_steered_completion_receipt_shows_its_held_messages(mock_db, _clean_delivered):
    """#1618: held messages that rode a steered task notification are shown as
    one attributed agent message each when that steer is receipted."""
    from fluid.context.peer import format_peer_message

    from app.core import session_runtime
    from app.core.turn_recorder import TurnRecorder

    _seed("g8")
    live = turns_db.enqueue_turn("g8", "user", prompt="ongoing", profile_id="profile")
    turns_db.start_turn(live["id"], None, "runner")
    held = turn_scheduler._join_held(
        turn_scheduler._held_entry(format_peer_message("w1", "first").strip()),
        turn_scheduler._held_entry(format_peer_message("w2", "second\nline").strip()),
    )
    runtime = session_runtime.SessionRuntime(live)
    recorder = TurnRecorder("g8", "cmd", None, None, None)
    recorder.turn_id = live["id"]

    assert await runtime._on_task_notification_delivered(
        recorder, {"frames": [], "task_ids": [], "held_peer_messages": held},
    )
    assert _agent_notes("g8") == [
        {"sender": "w1", "content": "first", "delivery": "delivered"},
        {"sender": "w2", "content": "second\nline", "delivery": "delivered"},
    ]


async def test_an_unpark_wake_carries_held_messages_once(mock_db, _clean_delivered):
    _seed("g2")
    _parked("g2")
    assert await turn_scheduler.on_agent_message("g2", "w1", "halfway") == turn_scheduler.HELD
    (held_row,) = _held("g2")

    frame = _frame("w1")
    async with turn_scheduler._terminal_delivery_lock("g2"):
        await turn_scheduler._enqueue_context_wake(
            "g2", "unpark", [frame], ["w1"],
            turn_scheduler._build_task_notification_prompt([frame]),
        )
    (wake,) = turns_db.get_queued_turns("g2")
    # A stray copy of the same message reaching the store again (a restore
    # racing the wake) must not be delivered a second time.
    await wakeup_service.hold_peer_messages(
        "g2", held_row["prompt"], delay_seconds=600,
        profile_id="profile", project_id=None, model=None,
    )

    prompt = await _run_turn(wake)
    assert prompt.count("halfway") == 1
    assert _held("g2") == []


async def test_a_steered_completion_carries_held_messages_once(
    mock_db, monkeypatch, _clean_delivered,
):
    from app.core import session_runtime
    from app.core.task_registry import task_registry

    _parked("g3")
    assert await turn_scheduler.on_agent_message("g3", "w1", "halfway") == turn_scheduler.HELD

    steered = []

    async def _steer(session_id, prompt, **kwargs):
        steered.append((prompt, kwargs))
        return True

    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: object())
    monkeypatch.setattr(session_runtime, "try_steer_task_notification", _steer)
    monkeypatch.setattr(task_registry, "is_own_background_task", lambda *a, **k: True)

    await turn_scheduler.on_task_terminal("g3", _frame("w2"))

    assert len(steered) == 1
    prompt, kwargs = steered[0]
    assert "w2 done" in prompt
    assert prompt.count("halfway") == 1
    assert "halfway" in kwargs["held_peer_messages"]
    assert _held("g3") == []
    assert turns_db.get_queued_turns("g3") == []
    # The next turn does not get it again.
    assert await turn_scheduler.fold_held_peer_messages_for_turn(
        "g3", {"id": "next", "payload": {}},
    ) == ""


async def test_a_steer_the_turn_never_delivers_hands_held_messages_to_the_wake(
    mock_db, _clean_delivered,
):
    _parked("g4")
    assert await turn_scheduler.on_agent_message("g4", "w1", "halfway") == turn_scheduler.HELD
    async with turn_scheduler._terminal_delivery_lock("g4"):
        held = await turn_scheduler._take_held_peer_messages("g4")
    turn_scheduler._mark_held_delivered("g4", turn_scheduler._held_ids(held))

    await turn_scheduler.on_task_notification_stranded(
        "g4", [_frame("w2")], ["w2"], held_peer_messages=held,
    )

    (wake,) = turns_db.get_queued_turns("g4")
    assert wake["prompt"].count("halfway") == 1


@pytest.mark.parametrize("live_source", ["unpark", "user"])
async def test_a_message_to_a_running_parked_session_queues_for_its_live_turn(
    mock_db, _clean_delivered, live_source,
):
    """#1618: a live turn beside the park (the wake just claimed, or a user turn
    running while background work continues) is a running receiver. The
    message queues as one attributed agent message the live turn can take at
    its next tool boundary; it is not held until that turn ends."""
    _parked("g6")
    if live_source == "unpark":
        live = _queued_unpark("g6", "w1")
    else:
        live = turns_db.enqueue_turn("g6", "user", prompt="next", profile_id="profile")
    # The live turn is claimed; the park row has not settled yet.
    assert turns_db.start_turn(live["id"], None, "runner") is not None

    turn_id = await turn_scheduler.on_agent_message("g6", "w2", "late news")
    assert turn_id not in (None, turn_scheduler.HELD)
    assert _held("g6") == []
    (row,) = turns_db.get_queued_turns("g6")
    assert row["id"] == turn_id and row["source"] == "agent_message"
    assert row["payload"]["task_peer_sender"] == "w2"
    assert row["payload"]["peer_message_content"] == "late news"
    assert row["prompt"].count("late news") == 1
    # Nothing is left for the live turn's start to fold in a second time.
    assert await turn_scheduler.fold_held_peer_messages_for_turn(
        "g6", turns_db.get_turn(live["id"]),
    ) == ""


async def test_held_message_defaults(mock_db, monkeypatch, _clean_delivered):
    from app.core import query_session_lifecycle

    assert turn_scheduler.HELD_PEER_PROMOTION_SECONDS == 600.0
    _parked("g7")
    now = [1_000_000.0]
    monkeypatch.setattr(wakeup_service, "_now_epoch", lambda: now[0])

    assert await turn_scheduler.on_agent_message("g7", "w1", "one") == turn_scheduler.HELD
    (row,) = _held("g7")
    assert float(row["fire_at"]) == 1_000_600.0
    # A later message does not extend the deadline.
    now[0] += 300
    assert await turn_scheduler.on_agent_message("g7", "w1", "two") == turn_scheduler.HELD
    (row,) = _held("g7")
    assert float(row["fire_at"]) == 1_000_600.0

    # Stop drops held messages, and a stopped session holds no new ones.
    await query_session_lifecycle.enter_user_stop("g7")
    assert _held("g7") == []
    assert await turn_scheduler.on_agent_message("g7", "w1", "three") is None
    assert _held("g7") == []
