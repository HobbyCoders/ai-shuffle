"""Stop cancels every queued message and keeps the words the owner wrote.

Stop's contract is that nothing queued runs. These tests pin the other half: a
message the owner typed is not lost with it, whichever path aborts it. Each one
becomes its own "Not sent" row, and rows the owner did not write never do.
"""

from __future__ import annotations

import pytest

from app.core.query_session_lifecycle import enter_user_stop
from app.core.task_registry import task_registry
from app.db import database, session_events, sessions, turns
from tests.shared_fixtures import clean_task_registry  # noqa: F401  (autouse fixture)


def _chat(session_id: str) -> str:
    database.create_session(session_id=session_id, profile_id="default", title="t")
    return session_id


def _rows(session_id: str) -> list[dict]:
    return [e["payload"] for e in session_events.get_events(session_id)
            if e["event_type"] == "message.user" and e["payload"].get("not_sent")]


async def test_stop_gives_each_queued_message_its_own_row(mock_db):
    _chat("s-two")
    first = turns.enqueue_turn("s-two", "user", prompt="first words",
                               payload={"upload_paths": ["/uploads/proof.png"]})
    second = turns.enqueue_turn("s-two", "queue", prompt="second words")

    await enter_user_stop("s-two")

    assert {turns.get_turn(t["id"])["state"] for t in (first, second)} == {"aborted"}
    rows = _rows("s-two")
    assert [r["content"] for r in rows] == ["first words", "second words"]
    assert rows[0]["upload_paths"] == ["/uploads/proof.png"]
    assert [m["content"] for m in sessions.get_session_messages("s-two")
            if (m.get("metadata") or {}).get("not_sent")] == ["first words", "second words"]


async def test_a_repeated_stop_does_not_write_a_second_row(mock_db):
    # The chat handler and the owning runner's durable path both sweep.
    _chat("s-twice")
    turns.enqueue_turn("s-twice", "user", prompt="only once")
    await enter_user_stop("s-twice")
    await enter_user_stop("s-twice")
    assert [r["content"] for r in _rows("s-twice")] == ["only once"]


@pytest.mark.parametrize("source,prompt,payload", [
    pytest.param("user", "/compact", {}, id="bare-compact"),
    pytest.param("user", "already shown", {"user_message_already_recorded": True}, id="requeued-steer"),
    pytest.param("wake", "continue", {}, id="automatic-wake"),
    pytest.param("agent_message", "from a peer", {}, id="peer-message"),
])
async def test_rows_the_owner_has_no_unsent_words_in_stay_out(mock_db, source, prompt, payload):
    _chat("s-none")
    row = turns.enqueue_turn("s-none", source, prompt=prompt, payload=payload)
    await enter_user_stop("s-none")
    assert turns.get_turn(row["id"])["state"] == "aborted"
    assert _rows("s-none") == []


async def test_a_recovered_turn_already_on_screen_gets_no_second_row(mock_db):
    # A crash puts a started turn back in the queue; its words are already shown.
    _chat("s-recovered")
    row = turns.enqueue_turn("s-recovered", "user", prompt="already shown")
    session_events.append_events("s-recovered", [{
        "type": "message.user", "turn_id": row["id"], "payload": {"content": "already shown"},
    }])
    await enter_user_stop("s-recovered")
    assert turns.get_turn(row["id"])["state"] == "aborted"
    assert _rows("s-recovered") == []


def test_dropping_a_chip_writes_no_row(mock_db):
    # The owner removed this one on purpose.
    _chat("s-chip")
    row = turns.enqueue_turn("s-chip", "user", prompt="never mind")
    turns.remove_queued_turn(row["id"])
    assert turns.get_turn(row["id"])["state"] == "aborted"
    assert _rows("s-chip") == []


async def test_a_claim_pass_that_reaches_a_stopped_row_keeps_its_words(mock_db):
    """The race that lost a message: the once-a-second claim pass picks up the
    head of the queue just as Stop latches, and aborts it before the sweep does."""
    from app.core import session_runtime
    from tests.core.test_session_runtime import _runtime_env

    async def pump(**kwargs):
        raise AssertionError("a stopped session must not run")
        yield  # pragma: no cover

    _chat("s-race")
    head = turns.enqueue_turn("s-race", "user", prompt="head of the queue")
    behind = turns.enqueue_turn("s-race", "user", prompt="behind it")
    task_registry.mark_user_stopped("s-race")
    with _runtime_env(pump=pump) as env:
        env["registry"].is_user_stopped.side_effect = task_registry.is_user_stopped
        assert await session_runtime.SessionRuntime(head).execute() is None
    await enter_user_stop("s-race")

    assert {turns.get_turn(t["id"])["state"] for t in (head, behind)} == {"aborted"}
    assert sorted(r["content"] for r in _rows("s-race")) == ["behind it", "head of the queue"]


def _running(session_id: str, prompt: str = "words in flight") -> dict:
    """A turn past start_turn, with nothing recorded yet."""
    queued = turns.enqueue_turn(session_id, "user", prompt=prompt)
    return turns.start_turn(queued["id"], "run-1", "runner-1")


def _resumed_parked(session_id: str, prompt: str = "the new words") -> dict:
    """A parked turn that recorded its own prompt, resumed by newer input: the
    same turn id now holds words nothing has recorded."""
    from app.core.session_runtime import SessionRuntime

    original = _running(session_id, "the older prompt")
    session_events.append_events(session_id, [{
        "type": "message.user", "turn_id": original["id"], "payload": {"content": "the older prompt"},
    }])
    assert turns.park_turn(original["id"], ["task-1"])
    queued = turns.enqueue_turn(session_id, "user", prompt=prompt)
    resumed = turns.start_turn(queued["id"], "run-2", "runner-1", can_resume=SessionRuntime._can_resume_parked)
    assert resumed["id"] == original["id"], "the new input must resume the parked turn"
    return resumed


def test_stop_during_the_start_keeps_the_words_of_a_turn_it_can_no_longer_sweep(mock_db):
    _chat("s-started")
    row = _running("s-started")
    settled = turns.settle_turn(row["id"], "aborted", "interrupted", keep_unsent_words=True)
    assert settled["state"] == "aborted"
    assert [r["content"] for r in _rows("s-started")] == ["words in flight"]
    # A second settle changes nothing, so it can never add a second row.
    assert turns.settle_turn(row["id"], "aborted", "interrupted", keep_unsent_words=True) is None
    assert len(_rows("s-started")) == 1


def test_an_ordinary_settle_writes_no_row(mock_db):
    _chat("s-plain")
    row = _running("s-plain")
    turns.settle_turn(row["id"], "aborted", "interrupted")
    assert _rows("s-plain") == []


async def test_the_start_guard_keeps_the_words(mock_db, monkeypatch):
    """Stop lands while start_turn is off-thread: the runtime wakes up owning a
    turn the sweep already passed over."""
    from app.core import session_runtime
    from tests.core.test_session_runtime import _runtime_env

    async def pump(**kwargs):
        raise AssertionError("a stopped session must not run")
        yield  # pragma: no cover

    _chat("s-guard")
    row = turns.enqueue_turn("s-guard", "user", prompt="caught mid start")
    real_start = turns.start_turn
    stopped = []

    def start_then_stop(*args, **kwargs):
        started = real_start(*args, **kwargs)
        stopped.append(True)
        return started

    monkeypatch.setattr(turns, "start_turn", start_then_stop)
    with _runtime_env(pump=pump) as env:
        # Only the guard after the start may see the stop; the ones before it
        # abort a still-queued row, which has its own test above.
        env["registry"].stop_invalidated.side_effect = lambda *_: bool(stopped)
        await session_runtime.SessionRuntime(row).execute()

    assert turns.get_turn(row["id"])["state"] == "aborted"
    assert [r["content"] for r in _rows("s-guard")] == ["caught mid start"]


async def test_an_older_prompt_under_a_resumed_turn_does_not_hide_the_new_words(mock_db):
    # Crash after the resume and before the recording; recovery requeues the
    # same id; Stop then finds the older prompt's event under it.
    _chat("s-resumed")
    resumed = _resumed_parked("s-resumed")
    assert resumed["payload"]["accepted_input_id"]
    _force_queued(resumed["id"])
    await enter_user_stop("s-resumed")
    assert [r["content"] for r in _rows("s-resumed")] == ["the new words"]


async def test_a_resumed_turn_recorded_before_the_crash_gets_no_second_row(mock_db):
    _chat("s-receipt")
    resumed = _resumed_parked("s-receipt")
    session_events.append_events("s-receipt", [{
        "type": "message.user", "turn_id": resumed["id"],
        "payload": {"content": "the new words", "accepted_input_id": resumed["payload"]["accepted_input_id"]},
    }])
    _force_queued(resumed["id"])
    await enter_user_stop("s-receipt")
    assert _rows("s-receipt") == []


async def test_the_recording_carries_the_resumed_inputs_receipt(mock_db):
    from app.core.turn_recorder import TurnRecorder

    _chat("s-carry")
    recorder = TurnRecorder("s-carry", None, None, None, "runner-1")
    await recorder.start("the new words", accepted_input_id="input-1", source="user")
    event = next(e for e in session_events.get_events("s-carry") if e["event_type"] == "message.user")
    assert event["payload"]["accepted_input_id"] == "input-1"
    assert event["payload"]["source"] == "user"


def _force_queued(turn_id: str) -> None:
    """What reap_expired does to a dead runner's turn: same id, back in the queue."""
    from app.db.base import get_db

    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='queued', run_id=NULL, instance_id=NULL WHERE id=%s", (turn_id,))


def _note(message_id: str) -> dict:
    return {"id": "unsent-restored-" + message_id, "type": "system.note",
            "payload": {"subtype": "unsent_restored", "data": {"message_id": message_id}}}


def test_rest_history_marks_a_stopped_row_and_drops_it_once_restored(mock_db):
    from app.api.sessions_helpers import project_event_messages

    _chat("s-rest")
    row = turns.enqueue_turn("s-rest", "user", prompt="kept words",
                             payload={"upload_paths": ["/uploads/a.png"]})
    turns.remove_queued_turn(row["id"], "interrupted", keep_unsent_words=True)
    event = next(e for e in session_events.get_events("s-rest") if e["event_type"] == "message.user")

    shown = project_event_messages("s-rest", session_events.get_events("s-rest"))
    assert [m["content"] for m in shown] == ["kept words"]
    assert shown[0]["metadata"]["not_sent"] is True
    assert shown[0]["metadata"]["not_sent_id"] == event["id"]
    assert shown[0]["metadata"]["upload_paths"] == ["/uploads/a.png"]

    session_events.append_events("s-rest", [_note(event["id"])])
    assert project_event_messages("s-rest", session_events.get_events("s-rest")) == []


def test_a_restore_note_only_removes_the_row_it_names(mock_db):
    from app.api.sessions_helpers import project_event_messages

    _chat("s-rest-two")
    session_events.append_events("s-rest-two", [
        {"id": "real", "type": "message.user", "turn_id": "t1", "payload": {"content": "a real message"}},
        _note("real"),
    ])
    assert [m["content"] for m in project_event_messages("s-rest-two", session_events.get_events("s-rest-two"))] \
        == ["a real message"]


async def test_stop_writes_the_whole_queue_in_one_commit(mock_db, monkeypatch):
    """The chat shows one commit at a time; a row per commit would push the
    bubbles still waiting below it down once per queued message."""
    _chat("s-batch")
    queued = [turns.enqueue_turn("s-batch", "user", prompt=words)
              for words in ("first words", "second words", "third words")]
    notified: list[tuple[str, list[str]]] = []
    real_notify = session_events.notify_session_write

    def notify(session_id, collection):
        notified.append((session_id, [r["content"] for r in _rows(session_id)]))
        real_notify(session_id, collection)

    monkeypatch.setattr(session_events, "notify_session_write", notify)

    await enter_user_stop("s-batch")

    batch = [words for sid, words in notified if sid == "s-batch" and words]
    assert batch and batch[0] == ["first words", "second words", "third words"]
    assert {turns.get_turn(t["id"])["state"] for t in queued} == {"aborted"}


async def test_stopping_a_queue_spares_a_row_that_already_left_it(mock_db):
    _chat("s-raced")
    started = turns.enqueue_turn("s-raced", "user", prompt="already gone")
    waiting = turns.enqueue_turn("s-raced", "user", prompt="still waiting")
    turns.remove_queued_turn(started["id"])

    removed = turns.stop_queued_turns("s-raced", [started["id"], waiting["id"]])

    assert [row["id"] for row in removed] == [waiting["id"]]
    assert [r["content"] for r in _rows("s-raced")] == ["still waiting"]
