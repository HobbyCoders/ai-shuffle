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
