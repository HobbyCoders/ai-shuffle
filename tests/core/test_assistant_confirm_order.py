"""#484: an approval answered in words is recorded as request, then the owner's
answer, then the outcome, with the answered request id on the outcome.

The owner's message settles the request at once (``turn_manager.submit_async``)
and the outcome rides the owner's turn. ``SessionRuntime`` writes it right after
the owner's message lands in the transcript: at turn start, or where the
message is steered into a running turn. These drive the real runtime with the
pump and recorder stubbed (the harness from ``test_steering_delivery_flows``).
"""

from unittest.mock import AsyncMock, patch

from app.core import assistant_confirm, session_runtime
from app.db import turns as turns_db
from tests.core.test_steering_delivery_flows import (  # noqa: F401  (autouse fixture)
    _clean_module_state,
    _pump,
    _pump_env,
    _seed_session,
)


def _answer(request_id, decision="approved"):
    return {"request_id": request_id, "interaction_id": f"i-{request_id}", "decision": decision,
            "question": f"Do you authorize it to run `{request_id}`? Please answer yes or no."}


def _outcomes_logged_into(log):
    async def _record(session_id, answer):
        log.append(("outcome", answer["request_id"], answer["decision"]))
    return patch.object(assistant_confirm, "record_answer", new=AsyncMock(side_effect=_record))


async def test_the_outcome_follows_the_owner_message_that_starts_a_turn(mock_db):
    sid = _seed_session("confirm-order-1")
    turn = turns_db.enqueue_turn(sid, "user", prompt="yes",
                                 payload={assistant_confirm.ANSWER_KEY: _answer("perm-a")})
    events = [{"type": "chunk", "content": "Thanks, it is going ahead."}, {"type": "done", "metadata": {}}]

    with _pump_env(_pump(events)) as env:
        env["recorder"].start.side_effect = lambda prompt, **kw: env["log"].append(("user", prompt))
        with _outcomes_logged_into(env["log"]):
            assert await session_runtime.SessionRuntime(turn).execute() == "completed"

    assert [entry[:2] for entry in env["log"]] == [
        ("user", "yes"), ("outcome", "perm-a"), ("event", "chunk"), ("event", "done")]


async def test_the_outcome_follows_the_owner_message_steered_into_a_running_turn(mock_db):
    """Two jobs ask; the owner answers both while the chat is busy. Each outcome
    lands right after the message that gave it, with that request's id."""
    sid = _seed_session("confirm-order-2")
    live = turns_db.enqueue_turn(sid, "user", prompt="the live one")
    yes = turns_db.enqueue_turn(sid, "user", prompt="yes",
                                payload={assistant_confirm.ANSWER_KEY: _answer("perm-a")})
    no = turns_db.enqueue_turn(sid, "user", prompt="no",
                               payload={assistant_confirm.ANSWER_KEY: _answer("perm-b", "declined")})
    events = [
        {"type": "tool_use", "id": "t1", "name": "Bash"},
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
        {"type": "steer_delivered", "steers": [{"id": yes["id"], "text": "yes"}]},
        {"type": "tool_use", "id": "t2", "name": "Bash"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "ok"},
        {"type": "steer_delivered", "steers": [{"id": no["id"], "text": "no"}]},
        {"type": "done", "metadata": {}},
    ]

    with _pump_env(_pump(events)) as env:
        with _outcomes_logged_into(env["log"]):
            assert await session_runtime.SessionRuntime(live).execute() == "completed"

    order = [entry[1] if entry[0] != "outcome" else f"outcome:{entry[1]}:{entry[2]}" for entry in env["log"]]
    assert order == [
        "tool_use", "tool_result", "yes", "outcome:perm-a:approved",
        "tool_use", "tool_result", "no", "outcome:perm-b:declined", "done",
    ]


async def test_a_message_already_in_the_transcript_does_not_repeat_its_outcome(mock_db):
    """A row re-queued after its steer was swallowed keeps its payload; its
    message and outcome were recorded the first time."""
    sid = _seed_session("confirm-order-3")
    turn = turns_db.enqueue_turn(sid, "user", prompt="yes", payload={
        assistant_confirm.ANSWER_KEY: _answer("perm-a"), "user_message_already_recorded": True})

    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        with _outcomes_logged_into(env["log"]):
            assert await session_runtime.SessionRuntime(turn).execute() == "completed"

    assert not [entry for entry in env["log"] if entry[0] == "outcome"]


async def test_an_ordinary_message_records_no_outcome(mock_db):
    sid = _seed_session("confirm-order-4")
    turn = turns_db.enqueue_turn(sid, "user", prompt="hello")

    with _pump_env(_pump([{"type": "done", "metadata": {}}])) as env:
        with _outcomes_logged_into(env["log"]):
            assert await session_runtime.SessionRuntime(turn).execute() == "completed"

    assert not [entry for entry in env["log"] if entry[0] == "outcome"]
