"""#523: the owner's other message is never held behind a pending approval.

Owner decision: only a firm yes or no answers an approval asked in an owner
chat. Any other message is an ordinary message that reaches the agent at once,
and the approval stays pending and answerable. When the chat is itself blocked
on one of its own calls waiting for that approval, the call gets a "not run
yet" result so the engine can take the message; a later yes admits exactly
that call once, a later no or an expiry is reported back.

These drive the real intake (``turn_manager.submit_async``), the real
permission plumbing (``provider_agent_approvals.route_permission`` into
``permission_handler``) and the real sweeper. How each engine folds the
owner's message into the placeholder result is covered in fluid's
``tests/unit/test_deny_carries_steer.py``.
"""
import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

from app.core import assistant_confirm as confirm
from app.core import away_mode, interaction_sweeper, session_runtime
from app.core import permission_handler as ph
from app.core import provider_agent_approvals as approvals
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.db import interactions, turns
from app.db.base import get_db
from tests.core.interaction_harness import (  # noqa: F401  (autouse fixtures)
    no_connector_delivery,
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)
from tests.core.test_assistant_confirm import (  # noqa: F401  (autouse fixture)
    _answers,
    _ask,
    _outcomes,
    _pending,
    _started_chat,
    clean_confirm,
    owner_says,
)
from tests.core.test_assistant_started_work import home, quiet_scheduler  # noqa: F401
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401

OTHER = "Instead, explain what that command would change"


def _own_call(home_id, command="gh pr merge 12"):
    """One of the owner chat's OWN calls asking for approval (production entry)."""
    kwargs = dict(request_id=f"perm-{home_id}-{uuid4().hex[:8]}", session_id=home_id,
                  profile_id="assistant-agent", tool_name="Bash", tool_input={"command": command},
                  broadcast_func=AsyncMock())
    return asyncio.create_task(approvals.route_permission(kwargs, ph.permission_handler.request_permission))


def _steers(monkeypatch, only=None):
    """Record each hand-over of queued messages to a chat's engine, with the
    queued rows at that moment; with ``only``, just that chat's."""
    calls = []

    async def steer(session_id):
        if only is None or session_id == only:
            calls.append([row["prompt"] for row in turns.get_queued_turns(session_id)])
        return 1

    monkeypatch.setattr(session_runtime, "try_steer_pending", steer)
    return calls


def _wakes(home_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source='wakeup' ORDER BY position",
                       (home_id,))
        return [turns._row(row) for row in cursor.fetchall()]


async def _parked(home_id, monkeypatch, command="gh pr merge 12"):
    """An own call answered "not run yet" by the owner's other message."""
    _steers(monkeypatch)
    task = _own_call(home_id, command)
    [request] = await _pending(home_id)
    await owner_says(home_id, OTHER)
    result = await asyncio.wait_for(task, 12.0)
    assert isinstance(result, PermissionResultDeny) and result.message == confirm.NOT_RUN_YET
    return request


async def test_another_message_lets_the_waiting_call_go_and_keeps_the_card(home, monkeypatch):  # noqa: F811
    home_id, _ = home
    steers = _steers(monkeypatch)
    task = _own_call(home_id)
    [request] = await _pending(home_id)
    turn_id = await owner_says(home_id, OTHER)
    result = await asyncio.wait_for(task, 12.0)
    # The call is let go with the placeholder, never run and never declined.
    assert isinstance(result, PermissionResultDeny) and result.message == confirm.NOT_RUN_YET
    # The owner's message was queued and handed to the engine BEFORE the call
    # was let go, so the engine folds it into this very result.
    assert steers == [[OTHER]]
    # It is an ordinary message: no answer, no outcome row.
    assert confirm.ANSWER_KEY not in (turns.get_turn(turn_id)["payload"] or {})
    assert _answers(home_id) == [] and _outcomes(home_id) == []
    # The card stays pending and answerable.
    [still] = confirm.pending_requests(home_id)
    assert still["request_id"] == request["request_id"] and still["parked"] and still["own"]
    rows = ph.permission_handler.get_pending_requests(home_id)
    assert [r["request_id"] for r in rows] == [request["request_id"]]
    assert "Not run yet" in away_mode.turn_note(home_id)


async def test_more_messages_neither_answer_nor_release_twice(home, monkeypatch):  # noqa: F811
    home_id, _ = home
    request = await _parked(home_id, monkeypatch)
    steers = _steers(monkeypatch)
    for text in ("and also check CI", "maybe", "you decide"):
        await owner_says(home_id, text)
    assert steers == []  # nothing left waiting to let go
    [still] = confirm.pending_requests(home_id)
    assert still["request_id"] == request["request_id"] and still["parked"]
    assert _answers(home_id) == [] and _outcomes(home_id) == [] and _wakes(home_id) == []


async def test_parallel_own_calls_are_all_let_go(home, monkeypatch):  # noqa: F811
    """Each parallel call blocks the loop: all of them get the placeholder,
    and both cards stay, asked one at a time."""
    home_id, _ = home
    _steers(monkeypatch)
    first = _own_call(home_id, "git push")
    await _pending(home_id)
    second = _own_call(home_id, "gh release create v1")
    await _pending(home_id, 2)
    await owner_says(home_id, OTHER)
    for task in (first, second):
        result = await asyncio.wait_for(task, 12.0)
        assert isinstance(result, PermissionResultDeny) and result.message == confirm.NOT_RUN_YET
    assert [item["parked"] for item in confirm.pending_requests(home_id)] == [True, True]


async def test_a_later_yes_admits_exactly_that_call_once(home, monkeypatch):  # noqa: F811
    home_id, _ = home
    request = await _parked(home_id, monkeypatch)
    turn_id = await owner_says(home_id, "yes")
    assert confirm.pending_requests(home_id) == []
    payload = turns.get_turn(turn_id)["payload"]
    answer = payload[confirm.ANSWER_KEY]
    assert answer["request_id"] == request["request_id"]
    assert answer["decision"] == "approved" and answer["parked"] is True
    # The agent hears it with the owner's own yes, at turn start or steered.
    note = confirm.engine_note(payload)
    assert "run that exact call again now" in note and "gh pr merge 12" in note
    assert "run that exact call again now" in session_runtime._with_voice_note("yes", payload)
    # The typed yes carries it, so no separate wake.
    assert _wakes(home_id) == []

    # A different call is not admitted: it asks as usual.
    other = _own_call(home_id, "gh pr merge 13")
    [asked] = await _pending(home_id)
    assert "gh pr merge 13" in asked["question"]
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(other, 12.0), PermissionResultDeny)

    # The exact call is admitted once, without asking.
    again = _own_call(home_id)
    assert isinstance(await asyncio.wait_for(again, 5.0), PermissionResultAllow)
    assert confirm.pending_requests(home_id) == []
    # ...and only once.
    third = _own_call(home_id)
    await _pending(home_id)
    assert not third.done()
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(third, 12.0), PermissionResultDeny)


async def test_a_later_no_declines_and_says_so(home, monkeypatch):  # noqa: F811
    home_id, _ = home
    request = await _parked(home_id, monkeypatch)
    turn_id = await owner_says(home_id, "no")
    payload = turns.get_turn(turn_id)["payload"]
    assert payload[confirm.ANSWER_KEY]["decision"] == "declined"
    assert payload[confirm.ANSWER_KEY]["request_id"] == request["request_id"]
    assert "Do not run it" in confirm.engine_note(payload)
    assert confirm.pending_requests(home_id) == []
    # Nothing was admitted: the same call asks again.
    again = _own_call(home_id)
    await _pending(home_id)
    assert not again.done()
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(again, 12.0), PermissionResultDeny)


async def test_an_expiry_wakes_the_agent_once(home, monkeypatch):  # noqa: F811
    home_id, _ = home
    monkeypatch.setattr(confirm, "TTL_SECONDS", 1)
    await _parked(home_id, monkeypatch)
    await asyncio.sleep(2.1)  # the test database stores expiry to the whole second
    assert await interaction_sweeper.sweep_once() >= 1
    [wake] = _wakes(home_id)
    assert wake["prompt"] == confirm.expired_note("run `gh pr merge 12`")
    assert wake["profile_id"] == "assistant-agent"
    assert confirm.pending_requests(home_id) == []
    # A yes after the expiry answers nothing and admits nothing.
    assert await confirm.on_owner_message(home_id, "yes") is None
    assert not confirm.take_approved_once(home_id, "Bash", {"command": "gh pr merge 12"})
    await interaction_sweeper.sweep_once()
    assert len(_wakes(home_id)) == 1


async def test_an_expiry_of_a_call_still_waiting_needs_no_wake(home, monkeypatch):  # noqa: F811
    """Unchanged: a call still waiting gets "timed out" itself."""
    home_id, _ = home
    monkeypatch.setattr(confirm, "TTL_SECONDS", 1)
    task = _own_call(home_id)
    await _pending(home_id)
    result = await asyncio.wait_for(task, 12.0)
    assert isinstance(result, PermissionResultDeny) and "timed out" in result.message
    await asyncio.sleep(2.1)
    await interaction_sweeper.sweep_once()
    assert _wakes(home_id) == []


async def test_an_answer_from_elsewhere_wakes_the_agent(home, monkeypatch):  # noqa: F811
    """A voice call or another device answers the parked request: no owner
    message carries the outcome, so a wake does, and a yes still admits the
    call once."""
    home_id, _ = home
    request = await _parked(home_id, monkeypatch)
    await ph.permission_handler.respond(request["request_id"], home_id, "allow", broadcast_func=AsyncMock())
    [wake] = _wakes(home_id)
    assert wake["prompt"].startswith("The owner approved this request: run `gh pr merge 12`.")
    assert confirm.take_approved_once(home_id, "Bash", {"command": "gh pr merge 12"})


async def test_a_plain_yes_or_no_with_nothing_else_is_unchanged(home, monkeypatch):  # noqa: F811
    home_id, _ = home
    steers = _steers(monkeypatch)
    task = _own_call(home_id)
    await _pending(home_id)
    turn_id = await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
    payload = turns.get_turn(turn_id)["payload"]
    assert payload[confirm.ANSWER_KEY]["decision"] == "approved"
    assert not payload[confirm.ANSWER_KEY]["parked"]
    assert confirm.engine_note(payload) is None and steers == [] and _wakes(home_id) == []
    # A live call's yes is spent on that call: nothing is left to admit.
    assert not confirm.take_approved_once(home_id, "Bash", {"command": "gh pr merge 12"})

    task = _own_call(home_id, "rm -rf build")
    await _pending(home_id)
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)
    assert steers == [] and _wakes(home_id) == []


async def test_a_started_chats_request_is_not_let_go(home, monkeypatch):  # noqa: F811
    """A started chat's (or worker's) call blocks its own loop, not this one:
    the owner's other message reaches the home as usual and the call keeps
    waiting for the yes or no."""
    home_id, _ = home
    steers = _steers(monkeypatch, only=home_id)
    task = _ask(await _started_chat(home_id))
    [request] = await _pending(home_id)
    assert not request["own"]
    await owner_says(home_id, OTHER)
    await asyncio.sleep(0.05)
    assert not task.done() and steers == []
    assert not confirm.pending_requests(home_id)[0]["parked"]
    await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)


def test_the_one_shot_admit_is_exact_and_single():
    confirm.allow_once("s1", "Bash", {"command": "ls", "timeout": 5})
    assert not confirm.take_approved_once("s2", "Bash", {"command": "ls", "timeout": 5})
    assert not confirm.take_approved_once("s1", "Bash", {"command": "ls"})
    assert not confirm.take_approved_once("s1", "Read", {"command": "ls", "timeout": 5})
    # Key order does not matter; the input itself does.
    assert confirm.take_approved_once("s1", "Bash", {"timeout": 5, "command": "ls"})
    assert not confirm.take_approved_once("s1", "Bash", {"command": "ls", "timeout": 5})


async def test_mark_parked_only_touches_a_pending_row(mock_db):
    row = interactions.create_interaction("s1", "permission", {"request_id": "r1"}, 60)
    assert interactions.mark_interaction_parked(row["id"])
    assert interactions.get_interaction(row["id"])["payload"] == {"request_id": "r1", "parked": True}
    assert interactions.mark_interaction_parked(row["id"])  # idempotent
    interactions.answer_interaction(row["id"], {"decision": "deny"})
    other = interactions.create_interaction("s1", "permission", {"request_id": "r2"}, 60)
    interactions.cancel_interaction(other["id"])
    assert not interactions.mark_interaction_parked(other["id"])
    assert not interactions.mark_interaction_parked("missing")
