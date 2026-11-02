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
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest
from uuid import uuid4

from app.core import owner_confirm as confirm
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
# (home session id, its first turn id), from the ``home`` fixture.
Home = Tuple[str, str]


def _own_call(home_id: str, command: str = "gh pr merge 12",
              **extra: Any) -> "asyncio.Task[Any]":
    """One of the owner chat's OWN calls asking for approval, past saved rules
    as the owner gate asks (production entry, ``query_engine``)."""
    kwargs = dict(request_id=f"perm-{home_id}-{uuid4().hex[:8]}", session_id=home_id,
                  profile_id="assistant-agent", tool_name="Bash", tool_input={"command": command},
                  broadcast_func=AsyncMock(), skip_saved_rules=True, **extra)
    request = ph.permission_handler.request_permission
    return asyncio.create_task(approvals.route_permission(kwargs, request))


def _steers(monkeypatch: pytest.MonkeyPatch, only: Optional[str] = None) -> List[List[str]]:
    """Record each hand-over of queued messages to a chat's engine, with the
    queued rows at that moment; with ``only``, just that chat's."""
    calls = []

    async def steer(session_id: str) -> int:
        if only is None or session_id == only:
            calls.append([row["prompt"] for row in turns.get_queued_turns(session_id)])
        return 1

    monkeypatch.setattr(session_runtime, "try_steer_pending", steer)
    return calls


def _wakes(home_id: str) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source='wakeup' "
                       "ORDER BY position", (home_id,))
        return [turns._row(row) for row in cursor.fetchall()]


async def _parked(home_id: str, monkeypatch: pytest.MonkeyPatch,
                  command: str = "gh pr merge 12") -> Dict[str, Any]:
    """An own call answered "not run yet" by the owner's other message."""
    _steers(monkeypatch)
    task = _own_call(home_id, command)
    [request] = await _pending(home_id)
    await owner_says(home_id, OTHER)
    result = await asyncio.wait_for(task, 12.0)
    assert isinstance(result, PermissionResultDeny) and result.message == confirm.NOT_RUN_YET
    return request


async def test_another_message_lets_the_waiting_call_go_and_keeps_the_card(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
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
    assert still["request_id"] == request["request_id"] and still["own"]
    assert still["parked"] and still["released"]
    rows = ph.permission_handler.get_pending_requests(home_id)
    assert [r["request_id"] for r in rows] == [request["request_id"]]
    assert "Not run yet" in away_mode.turn_note(home_id)


async def test_more_messages_neither_answer_nor_release_twice(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    home_id, _ = home
    request = await _parked(home_id, monkeypatch)
    steers = _steers(monkeypatch)
    for text in ("and also check CI", "maybe", "you decide"):
        await owner_says(home_id, text)
    assert steers == []  # nothing left waiting to let go
    [still] = confirm.pending_requests(home_id)
    assert still["request_id"] == request["request_id"] and still["parked"]
    assert _answers(home_id) == [] and _outcomes(home_id) == [] and _wakes(home_id) == []


async def test_parallel_own_calls_are_all_let_go(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
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


async def test_a_later_yes_admits_exactly_that_call_once(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    home_id, _ = home
    request = await _parked(home_id, monkeypatch)
    turn_id = await owner_says(home_id, "yes")
    assert confirm.pending_requests(home_id) == []
    payload = turns.get_turn(turn_id)["payload"]
    answer = payload[confirm.ANSWER_KEY]
    assert answer["request_id"] == request["request_id"]
    assert answer["decision"] == "approved" and answer["released"] is True
    assert answer["pass"] == {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 12"},
                              "interaction_id": request["interaction_id"]}
    # The agent hears it with the owner's own yes, at turn start or steered.
    note = confirm.engine_note(payload)
    assert "run that exact call again now" in note and "gh pr merge 12" in note
    assert "It should not ask again" in note
    assert "run that exact call again now" in session_runtime._with_voice_note("yes", payload)
    # The typed yes carries it, so no separate wake, and no outcome note twice.
    assert _wakes(home_id) == [] and confirm.context_note(home_id) is None

    # Nothing is admitted until the turn carrying the yes reaches the engine.
    early = _own_call(home_id)
    await _pending(home_id)
    assert not early.done()
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(early, 12.0), PermissionResultDeny)
    # session_runtime arms the pass when it hands the yes to the engine.
    assert confirm.arm_pass(home_id, payload, "turn-yes")

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
    # ...and only once, even when the same message is delivered again.
    assert not confirm.arm_pass(home_id, payload, "turn-later")
    third = _own_call(home_id)
    await _pending(home_id)
    assert not third.done()
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(third, 12.0), PermissionResultDeny)


async def test_the_pass_lives_only_for_its_turn_and_only_replaces_the_ask(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    home_id, _ = home
    await _parked(home_id, monkeypatch)
    payload = turns.get_turn(await owner_says(home_id, "yes"))["payload"]
    assert confirm.arm_pass(home_id, payload, "turn-yes")

    # A subagent's call never spends the main chat's pass.
    sub_call = _own_call(home_id, agent_id="agent-1")
    await _pending(home_id)
    assert not sub_call.done()
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(sub_call, 12.0), PermissionResultDeny)
    # Nor does a call outside the allowed paths.
    blocked = _own_call(home_id, blocked_path="/etc")
    await _pending(home_id)
    assert not blocked.done()
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(blocked, 12.0), PermissionResultDeny)
    # A saved Deny outranks it: the call is asked, the pass stays unspent.
    ph.permission_handler._add_session_rule_idempotent(home_id, "Bash", "gh pr merge 12", "deny")
    denied = _own_call(home_id)
    await _pending(home_id)
    assert not denied.done()
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(denied, 12.0), PermissionResultDeny)
    ph.permission_handler._session_rules.pop(home_id, None)
    # Away mode defers the call before the pass is looked at.
    from app.core import away_mode as away

    async def away_now(session_id: str, what: str) -> str:
        return "Owner away"

    present = away.defer_if_away
    monkeypatch.setattr(away, "defer_if_away", away_now)
    result = await asyncio.wait_for(_own_call(home_id), 5.0)
    assert isinstance(result, PermissionResultDeny) and result.message == "Owner away"
    monkeypatch.setattr(away, "defer_if_away", present)

    # The pass is still there for its turn; the turn ending drops it.
    confirm.drop_passes(home_id, "another-turn")
    assert confirm.take_approved_once(home_id, "Bash", {"command": "gh pr merge 12"})
    assert confirm.arm_pass(home_id, {confirm.PASS_KEY: {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "interaction_id": "i-2"}}, "turn-2")
    confirm.drop_passes(home_id, "turn-2")
    assert not confirm.take_approved_once(home_id, "Bash", {"command": "ls"})
    # Dropped unspent (its turn ended first), it arms again where the message
    # lands next; once spent, never again.
    unspent = {confirm.PASS_KEY: {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "interaction_id": "i-2"}}
    assert confirm.arm_pass(home_id, unspent, "turn-3")
    assert confirm.take_approved_once(home_id, "Bash", {"command": "ls"})
    assert not confirm.arm_pass(home_id, unspent, "turn-4")


async def test_a_steered_yes_arms_its_pass_only_once_the_engine_took_it(
    mock_db: sqlite3.Connection,
) -> None:
    """A steer the engine refused leaves the pass for the turn the row runs in."""
    from tests.core.test_steering_delivery_flows import (
        _Adapter,
        _live_runtime,
        _NoSteeringAdapter,
        _pooled,
        _start_live,
    )

    sid = "pass-steer-1"
    _live_runtime(sid, _start_live(sid))
    turns.enqueue_turn(sid, "user", prompt="yes", payload={confirm.PASS_KEY: {
        "tool_name": "Bash", "tool_input": {"command": "ls"}, "interaction_id": "i-steer"}})
    try:
        with _pooled(_NoSteeringAdapter()):
            assert await session_runtime.try_steer_pending(sid) == 0
        assert not confirm.take_approved_once(sid, "Bash", {"command": "ls"})
        with _pooled(_Adapter()):
            assert await session_runtime.try_steer_pending(sid) == 1
        assert confirm.take_approved_once(sid, "Bash", {"command": "ls"})
    finally:
        session_runtime._live_runtimes.pop(sid, None)


async def test_a_release_from_elsewhere_before_the_typed_yes_is_reported(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """The typed yes read the request before another device's message let
    its call go: the answer is judged on the row as answered, so the yes
    still tells the agent to run it again and carries the pass."""
    home_id, _ = home
    _steers(monkeypatch)
    task = _own_call(home_id)
    [request] = await _pending(home_id)
    real = confirm.pending_requests

    def read_then_release(session_id: str) -> List[Dict[str, Any]]:
        items = real(session_id)
        # Another device parks and the waiter releases, right after this read.
        assert interactions.mark_interaction_parked(request["interaction_id"])
        assert interactions.mark_interaction_released(request["interaction_id"])
        return items

    monkeypatch.setattr(confirm, "pending_requests", read_then_release)
    turn_id = await owner_says(home_id, "yes")
    monkeypatch.setattr(confirm, "pending_requests", real)
    answer = turns.get_turn(turn_id)["payload"][confirm.ANSWER_KEY]
    assert answer["released"] is True and answer["pass"]["tool_name"] == "Bash"
    task.cancel()


async def test_a_yes_that_reaches_the_waiter_first_runs_the_call_once(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """The yes lands after the request was parked but before its waiter read
    the row: the waiter gets the yes and runs the call, so the yes neither
    admits the call again nor tells the agent to re-run it."""
    home_id, _ = home
    steers = _steers(monkeypatch)
    task = _own_call(home_id)
    [request] = await _pending(home_id)
    # park() stamped the row, but its waiter has not read it yet.
    assert interactions.mark_interaction_parked(request["interaction_id"])
    turn_id = await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
    answer = turns.get_turn(turn_id)["payload"][confirm.ANSWER_KEY]
    assert answer["decision"] == "approved" and answer["released"] is False
    assert "pass" not in answer and confirm.engine_note({confirm.ANSWER_KEY: answer}) is None
    assert not interactions.mark_interaction_released(request["interaction_id"])
    assert steers == [] and _wakes(home_id) == []


async def test_a_remembered_answer_reports_every_released_sibling(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """An "always allow" answer settles the sibling requests its rule matches
    too: each released one wakes the agent with its own pass."""
    home_id, _ = home
    _steers(monkeypatch)
    first = _own_call(home_id, "git push origin a")
    await _pending(home_id)
    second = _own_call(home_id, "git push origin b")
    pending = await _pending(home_id, 2)
    await owner_says(home_id, OTHER)
    for task in (first, second):
        assert (await asyncio.wait_for(task, 12.0)).message == confirm.NOT_RUN_YET
    await ph.permission_handler.respond(pending[0]["request_id"], home_id, "allow",
                                        remember="session", pattern="git push*",
                                        broadcast_func=AsyncMock())
    assert confirm.pending_requests(home_id) == []
    wakes = _wakes(home_id)
    assert [w["payload"][confirm.PASS_KEY]["tool_input"]["command"] for w in wakes] == [
        "git push origin a", "git push origin b"]
    assert all(w["prompt"].startswith("The owner approved this request:") for w in wakes)


async def test_a_later_no_declines_and_says_so(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
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


async def test_an_expiry_wakes_the_agent_once(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
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


async def test_an_expiry_of_a_call_still_waiting_needs_no_wake(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
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


async def test_an_answer_from_elsewhere_wakes_the_agent(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """A voice call or another device answers the parked request: no owner
    message carries the outcome, so a wake does, and a yes still admits the
    call once."""
    home_id, _ = home
    request = await _parked(home_id, monkeypatch)
    await ph.permission_handler.respond(request["request_id"], home_id, "allow",
                                        broadcast_func=AsyncMock())
    [wake] = _wakes(home_id)
    assert wake["prompt"].startswith("The owner approved this request: run `gh pr merge 12`.")
    # The wake's turn carries the pass; it admits the call once it runs.
    assert not confirm.take_approved_once(home_id, "Bash", {"command": "gh pr merge 12"})
    assert confirm.arm_pass(home_id, wake["payload"], wake["id"])
    assert confirm.take_approved_once(home_id, "Bash", {"command": "gh pr merge 12"})


async def test_an_expiry_reports_only_a_call_its_waiter_released(mock_db: sqlite3.Connection,
                                                                 monkeypatch: pytest.MonkeyPatch,
                                                                 ) -> None:
    woken: List[str] = []

    async def wake(session_id: str, prompt: str, **kwargs: Any) -> None:
        woken.append(prompt)

    monkeypatch.setattr(confirm, "_wake", wake)
    base = {"request_id": "r", "tool_name": "Bash", "tool_input": {"command": "ls"},
            confirm.SUBTYPE: {"question": "Run ls?", "action": "run `ls`"}}
    rows = [
        {"id": "a", "session_id": "s1", "kind": "permission", "payload": {**base, "parked": True}},
        {"id": "b", "session_id": "s1", "kind": "permission",
         "payload": {**base, "parked": True, "released": True}},
    ]
    assert await confirm.on_expired(rows) == 1
    assert woken == [confirm.expired_note("run `ls`")]


def test_only_the_placeholder_and_a_persons_no_carry_the_message() -> None:
    assert confirm.deny_carries_steers(confirm.NOT_RUN_YET)
    assert confirm.deny_carries_steers(ph.OWNER_DENIAL)
    from app.core.session_settlement import SETTLED_DENIAL

    for message in ("Request cancelled", "Permission request timed out", "Denied by saved rule",
                    "Owner away", "", SETTLED_DENIAL, f"{ph.OWNER_DENIAL}: settled",
                    f"{confirm.NOT_RUN_YET} (late)"):
        assert not confirm.deny_carries_steers(message)


async def test_a_plain_yes_or_no_with_nothing_else_is_unchanged(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    home_id, _ = home
    steers = _steers(monkeypatch)
    task = _own_call(home_id)
    await _pending(home_id)
    turn_id = await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
    payload = turns.get_turn(turn_id)["payload"]
    assert payload[confirm.ANSWER_KEY]["decision"] == "approved"
    assert not payload[confirm.ANSWER_KEY]["released"]
    assert not confirm.arm_pass(home_id, payload, turn_id)
    assert confirm.engine_note(payload) is None and steers == [] and _wakes(home_id) == []
    # A live call's yes is spent on that call: nothing is left to admit.
    assert not confirm.take_approved_once(home_id, "Bash", {"command": "gh pr merge 12"})

    task = _own_call(home_id, "rm -rf build")
    await _pending(home_id)
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)
    assert steers == [] and _wakes(home_id) == []


async def test_a_started_chats_request_is_not_let_go(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
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


def test_the_one_shot_admit_is_exact_and_single() -> None:
    assert confirm.arm_pass("s1", {confirm.PASS_KEY: {
        "tool_name": "Bash", "tool_input": {"command": "ls", "timeout": 5},
        "interaction_id": "i-1"}}, "turn-1")
    assert not confirm.take_approved_once("s2", "Bash", {"command": "ls", "timeout": 5})
    assert not confirm.take_approved_once("s1", "Bash", {"command": "ls"})
    assert not confirm.take_approved_once("s1", "Read", {"command": "ls", "timeout": 5})
    # Key order does not matter; the input itself does.
    assert confirm.take_approved_once("s1", "Bash", {"timeout": 5, "command": "ls"})
    assert not confirm.take_approved_once("s1", "Bash", {"command": "ls", "timeout": 5})


async def test_mark_parked_only_touches_a_pending_row(mock_db: sqlite3.Connection) -> None:
    row = interactions.create_interaction("s1", "permission", {"request_id": "r1"}, 60)
    assert interactions.mark_interaction_parked(row["id"])
    assert interactions.mark_interaction_released(row["id"])
    payload = interactions.get_interaction(row["id"])["payload"]
    assert payload == {"request_id": "r1", "parked": True, "released": True}
    assert interactions.mark_interaction_parked(row["id"])  # idempotent
    interactions.answer_interaction(row["id"], {"decision": "deny"})
    other = interactions.create_interaction("s1", "permission", {"request_id": "r2"}, 60)
    interactions.cancel_interaction(other["id"])
    assert not interactions.mark_interaction_parked(other["id"])
    assert not interactions.mark_interaction_released(other["id"])
    assert not interactions.mark_interaction_parked("missing")


async def test_a_safety_check_is_never_let_go(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    """A provider safety check (``ModelGuardrail``, docs/internal/
    guardrail-contract.md) is not let go "not run yet" by the owner's other
    message: its caller would not come back, leaving a card asked with nobody
    waiting. It keeps waiting, and a later yes still continues the turn."""
    home_id, _ = home
    _steers(monkeypatch)
    kwargs = {"request_id": f"guardrail-{uuid4().hex[:8]}", "session_id": home_id,
              "profile_id": "assistant-agent", "tool_name": "ModelGuardrail",
              "tool_input": {"guardrail": True, "provider_label": "Anthropic",
                             "requested_model": "claude-opus-4-8",
                             "suggested_model": "claude-opus-4-7"},
              "broadcast_func": AsyncMock(), "skip_saved_rules": True}
    task = asyncio.create_task(
        approvals.route_permission(kwargs, ph.permission_handler.request_permission))
    [request] = await _pending(home_id)
    await owner_says(home_id, OTHER)
    await asyncio.sleep(0.3)
    assert not task.done()
    [still] = confirm.pending_requests(home_id)
    assert still["request_id"] == request["request_id"] and not still["parked"]
    await owner_says(home_id, "yes")
    result = await asyncio.wait_for(task, 12.0)
    assert isinstance(result, PermissionResultAllow)


# ---------------------------------------------------------------------------
# A bare yes or no to a safety check answers it and starts no turn
# ---------------------------------------------------------------------------

def _ask_guardrail(home_id: str) -> "asyncio.Task[Any]":
    kwargs = {"request_id": f"guardrail-{uuid4().hex[:8]}", "session_id": home_id,
              "profile_id": "assistant-agent", "tool_name": "ModelGuardrail",
              "tool_input": {"guardrail": True, "provider_label": "Anthropic",
                             "requested_model": "claude-opus-4-8",
                             "suggested_model": "claude-opus-4-7"},
              "broadcast_func": AsyncMock(), "skip_saved_rules": True}
    return asyncio.create_task(
        approvals.route_permission(kwargs, ph.permission_handler.request_permission))


class _LiveTurn:
    """The paused turn's runtime as ``answer_guardrail_in_place`` sees it."""

    def __init__(self, home_id: str) -> None:
        self.home_id = home_id
        self.turn_id = f"live-{uuid4().hex[:8]}"
        self.replies: List[Dict[str, Any]] = []

    async def record_owner_reply(self, content: str, **kwargs: Any) -> bool:
        # Whether the prompt was still open when the message went in: the
        # owner's answer must come before whatever the settled turn does next.
        self.replies.append({"content": content, "open": bool(confirm.pending_requests(self.home_id)),
                             **kwargs})
        return True


def _live(home_id: str, monkeypatch: pytest.MonkeyPatch) -> _LiveTurn:
    live = _LiveTurn(home_id)
    monkeypatch.setitem(session_runtime._live_runtimes, home_id, live)
    return live


def _owner_turns(home_id: str) -> List[Dict[str, Any]]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source IN ('user','queue')",
                       (home_id,))
        return [turns._row(row) for row in cursor.fetchall()]


@pytest.mark.parametrize("text,allowed", [("yes", True), ("No.", False)])
async def test_a_firm_answer_to_a_live_safety_check_starts_no_turn(
    home: Home, monkeypatch: pytest.MonkeyPatch, text: str, allowed: bool,  # noqa: F811
) -> None:
    """Typing yes continues the paused turn and typing no stops it, exactly as
    the card's buttons do. The message is the answer and nothing more: it is
    recorded on the live turn before the prompt settles, its outcome follows
    it, and no turn runs for it (after Stop a "no" turn would re-send the
    blocked request on the original model; after Continue a stray "yes" turn
    would follow the answer)."""
    home_id, _ = home
    steers = _steers(monkeypatch)
    live = _live(home_id, monkeypatch)
    before = len(_owner_turns(home_id))
    task = _ask_guardrail(home_id)
    [request] = await _pending(home_id)
    taken = await turn_manager_submit(home_id, text, command_id="client-1")
    result = await asyncio.wait_for(task, 12.0)
    assert isinstance(result, PermissionResultAllow if allowed else PermissionResultDeny)
    assert taken == live.turn_id
    assert len(_owner_turns(home_id)) == before and steers == []
    [reply] = live.replies
    assert reply["content"] == text and reply["open"] is True
    assert reply["command_id"] == "client-1" and reply["source"] == "user"
    [outcome] = _outcomes(home_id)
    assert outcome["data"]["request_id"] == request["request_id"]
    assert outcome["data"]["decision"] == ("approved" if allowed else "declined")
    assert confirm.pending_requests(home_id) == []


async def turn_manager_submit(home_id: str, text: str, **kwargs: Any) -> str:
    from app.core import turn_manager

    return await turn_manager.submit_async(home_id, "user", prompt=text,
                                           profile_id="assistant-agent", **kwargs)


@pytest.mark.parametrize("text", ["no, try a smaller change instead", "hmm, why was it blocked?"])
async def test_a_reply_with_more_in_it_still_reaches_the_agent(
    home: Home, monkeypatch: pytest.MonkeyPatch, text: str,  # noqa: F811
) -> None:
    home_id, _ = home
    _steers(monkeypatch)
    live = _live(home_id, monkeypatch)
    before = len(_owner_turns(home_id))
    task = _ask_guardrail(home_id)
    await _pending(home_id)
    await owner_says(home_id, text)
    await asyncio.sleep(0.2)
    assert not task.done() and live.replies == []
    assert len(_owner_turns(home_id)) == before + 1
    await turn_manager_submit(home_id, "no")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)


async def test_an_ordinary_approval_answered_in_words_is_unchanged(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    home_id, _ = home
    _steers(monkeypatch)
    live = _live(home_id, monkeypatch)
    task = _own_call(home_id)
    [request] = await _pending(home_id)
    await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
    assert live.replies == []
    [answer] = _answers(home_id)
    assert answer["request_id"] == request["request_id"] and answer["decision"] == "approved"


def _reply_turns(home_id: str) -> List[Dict[str, Any]]:
    from app.db import session_events

    return [e for e in session_events.get_events(home_id)
            if str(e.get("turn_id") or "").startswith("reply-")]


@pytest.mark.parametrize("answer", ["deny", "allow"])
async def test_a_late_answer_after_the_card_settled_starts_no_turn(
    home: Home, monkeypatch: pytest.MonkeyPatch, answer: str,  # noqa: F811
) -> None:
    """The prompt was answered on the card (or another device) and its turn
    moved on. A bare no typed afterwards, or a yes to a prompt that was
    allowed, has nothing left to answer: it is kept in the transcript as a
    reply and runs no turn. A yes to a declined prompt is a request to try
    again and goes on as a message."""
    home_id, _ = home
    _steers(monkeypatch)
    before = len(_owner_turns(home_id))
    task = _ask_guardrail(home_id)
    [request] = await _pending(home_id)
    await ph.permission_handler.respond(request["request_id"], home_id, answer,
                                        broadcast_func=AsyncMock())
    await asyncio.wait_for(task, 12.0)
    taken = await turn_manager_submit(home_id, "no", command_id="client-late")
    assert taken.startswith("reply-")
    assert len(_owner_turns(home_id)) == before
    events = [e for e in _reply_turns(home_id) if e["turn_id"] == taken]
    assert [e["event_type"] for e in events] == ["turn.started", "message.user", "turn.completed"]
    assert events[1]["payload"]["content"] == "no" and events[1]["command_id"] == "client-late"
    yes = await turn_manager_submit(home_id, "yes")
    if answer == "allow":
        assert yes.startswith("reply-") and len(_owner_turns(home_id)) == before
    else:
        assert not yes.startswith("reply-") and len(_owner_turns(home_id)) == before + 1


async def test_a_late_no_after_an_expired_safety_check_starts_no_turn(
    home: Home, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    home_id, _ = home
    _steers(monkeypatch)
    monkeypatch.setattr(confirm, "TTL_SECONDS", 1)
    before = len(_owner_turns(home_id))
    task = _ask_guardrail(home_id)
    await _pending(home_id)
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)
    await asyncio.sleep(1.1)  # the test database stores expiry to the whole second
    assert confirm.pending_requests(home_id) == []
    assert (await turn_manager_submit(home_id, "no")).startswith("reply-")
    assert len(_owner_turns(home_id)) == before
