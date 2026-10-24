"""The owner approves assistant work in the conversation: a firm yes from the
owner's own message, decided by the app, one request at a time."""
import asyncio
import json
from typing import Tuple
from unittest.mock import AsyncMock

import pytest

from app.core import owner_confirm as confirm
from app.core import away_mode, session_orchestration as orch, turn_manager
from app.core import provider_agent_approvals as approvals
from app.core import permission_handler as ph
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.db import interactions, session_events, turns
from app.db.base import get_db
from tests.core.interaction_harness import (  # noqa: F401  (autouse fixtures)
    no_connector_delivery,
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)
from tests.core.test_assistant_started_work import call, home, quiet_scheduler, start_args  # noqa: F401
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401


@pytest.fixture(autouse=True)
def clean_confirm(monkeypatch):
    confirm.reset()
    monkeypatch.setattr(approvals, "_parent_refs", lambda sid: {})
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    yield
    confirm.reset()


# ---------------------------------------------------------------------------
# classify: strict on purpose
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "yes", "Yes.", "YES!", "yes, I authorize it", "Yes I authorise it", "I authorize it",
    "go ahead", "ok", "Okay, do it", "yes please", "approved", "Sure.",
])
def test_firm_yes(text):
    assert confirm.classify(text) == confirm.YES


@pytest.mark.parametrize("text", [
    "no", "No.", "nope", "don't do it", "No, I don't authorize it", "cancel", "no thanks",
])
def test_firm_no(text):
    assert confirm.classify(text) == confirm.NO


@pytest.mark.parametrize("text", [
    # hedged or conditional
    "yes I guess", "probably", "maybe", "I think so", "yes if it's safe", "sure, why not", "ok but wait",
    # delegating
    "I'm not sure, you decide", "you decide", "up to you", "if you think so", "whatever you think",
    # off-topic or mixed
    "what's the weather tomorrow?", "what will it push?", "yes no", "no problem", "",
    "yes " * 30,
])
def test_hedged_delegating_and_off_topic_are_unclear(text):
    assert confirm.classify(text) == confirm.UNCLEAR


@pytest.mark.parametrize("text", [
    "Didn't I already say yes", "Didn’t I already say yes?", "didnt i already say yes",
    "I already said yes.", "I said yes", "Yes, I already said yes", "I already approved it",
])
def test_pointing_back_at_a_yes_is_a_yes(text):
    """#1797: "Didn't I already say yes" was unclear and the request stayed open."""
    assert confirm.classify(text) == confirm.YES


@pytest.mark.parametrize("text", ["I already said no", "I said yes to the other one", "didn't I"])
def test_pointing_back_needs_a_plain_yes(text):
    assert confirm.classify(text) != confirm.YES


def test_a_background_jobs_question_names_the_job():
    text = confirm.question("Bash", {"command": "grep -rn seed"}, job="Agents-page inventory")
    assert text == ("The background job “Agents-page inventory” asks: do you authorize it to run "
                    "`grep -rn seed`? Please answer yes or no.")


def test_a_task_worker_under_the_home_is_named_in_its_question(monkeypatch):
    from app.core import session_orchestration
    monkeypatch.setattr(session_orchestration, "is_owner_session", lambda sid: sid == "home")
    route = approvals.WorkerApprovalRoute(worker_session_id="w1", parent_session_id="home",
                                          label="Agents-page inventory")
    marker = approvals._assistant_confirm("home", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, route)
    assert marker["question"].startswith("The background job “Agents-page inventory” asks:")
    assert marker["chat_session_id"] == "w1"


def test_question_names_exactly_what_will_happen():
    text = confirm.question("Bash", {"command": "git push origin main"}, chat_title="Fix the flaky test")
    assert text == ("Do you authorize me to run `git push origin main` for “Fix the flaky test”? "
                    "Please answer yes or no.")
    assert "change C:/repo/app.py" in confirm.question("Edit", {"file_path": "C:/repo/app.py"})


# ---------------------------------------------------------------------------
# The flow, through the real permission plumbing
# ---------------------------------------------------------------------------

async def _started_chat(home_id, request_id="job"):
    result = json.loads((await call(home_id, "start", start_args(request_id))).text)
    assert turns.start_turn(result["turn_id"]) and turns.settle_turn(result["turn_id"], "completed")
    return result["session_id"]


def _ask(session_id, command="git push"):
    """The started chat's tool call asking for approval (production entry point)."""
    kwargs = dict(request_id=f"perm-{session_id}-{abs(hash(command)) % 10**8}", session_id=session_id,
                  profile_id="assistant-agent", tool_name="Bash", tool_input={"command": command},
                  broadcast_func=AsyncMock())
    return asyncio.create_task(approvals.route_permission(kwargs, ph.permission_handler.request_permission))


async def _pending(home_id, count=1):
    for _ in range(300):
        items = confirm.pending_requests(home_id)
        if len(items) >= count:
            return items
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} pending question(s) on the assistant home")


def _answers(home_id):
    """The answers the owner's turns carry."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source='user'",
                       (home_id,))
        rows = [turns._row(row) for row in cursor.fetchall()]
    return [r["payload"][confirm.ANSWER_KEY] for r in rows if confirm.ANSWER_KEY in (r["payload"] or {})]


def _outcomes(home_id):
    return [e["payload"] for e in session_events.get_events(home_id) if e["event_type"] == "system.note"
            and e["payload"].get("subtype") == confirm.SUBTYPE]


async def owner_says(home_id, text):
    """The owner's own message in the home, through the real intake."""
    return await turn_manager.submit_async(home_id, "user", prompt=text, profile_id="assistant-agent")


async def test_firm_yes_from_the_owner_approves_that_one_request(home):  # noqa: F811
    home_id, _ = home
    chat_id = await _started_chat(home_id)
    task = _ask(chat_id)
    [request] = await _pending(home_id)
    assert request["question"] == ("Do you authorize me to run `git push` for “Fix the flaky test”? "
                                   "Please answer yes or no.")
    assert request["chat_session_id"] == chat_id
    assert interactions.get_pending_interactions(chat_id) == []
    # The pending state reaches clients on the ordinary permission payload.
    rows = ph.permission_handler.get_pending_requests(home_id)
    assert rows[0]["assistant_confirm"]["question"] == request["question"]
    note = away_mode.turn_note(home_id)
    assert request["question"] in note and "you cannot approve it" in note
    turn_id = await owner_says(home_id, "Yes, I authorize it.")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
    assert confirm.pending_requests(home_id) == []
    # #484: settled at once, but the outcome row waits for the owner's message.
    assert _outcomes(home_id) == []
    [answer] = _answers(home_id)
    assert answer["request_id"] == request["request_id"] and answer["decision"] == "approved"
    assert answer["interaction_id"] == request["interaction_id"]
    await confirm.after_owner_message(home_id, turns.get_turn(turn_id)["payload"])
    [recorded] = _outcomes(home_id)
    assert recorded["data"]["decision"] == "approved"
    assert recorded["data"]["request_id"] == request["request_id"]
    assert "the app approved this one request" in away_mode.turn_note(home_id)
    # Told once.
    assert away_mode.turn_note(home_id) is None


async def test_firm_no_declines(home):  # noqa: F811
    home_id, _ = home
    task = _ask(await _started_chat(home_id))
    await _pending(home_id)
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)


async def test_unclear_never_approves_or_declines(home: Tuple[str, str]) -> None:  # noqa: F811
    """#523 (owner decision): only a firm yes or no answers. However many other
    messages arrive, the request stays pending and answerable, records no
    outcome, and the started chat that asked keeps waiting on it."""
    home_id, _ = home
    task = _ask(await _started_chat(home_id))
    [request] = await _pending(home_id)
    for text in ("I'm not sure, you decide", "probably fine", "what will it push?"):
        await owner_says(home_id, text)
    await asyncio.sleep(0.05)
    assert not task.done()
    [still] = confirm.pending_requests(home_id)
    assert still["request_id"] == request["request_id"] and not still["parked"]
    assert _answers(home_id) == [] and _outcomes(home_id) == []
    assert "Only a plain yes or no answers it" in away_mode.turn_note(home_id)
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)
    assert confirm.pending_requests(home_id) == []


async def test_a_question_expires(home, monkeypatch):  # noqa: F811
    home_id, _ = home
    monkeypatch.setattr(confirm, "TTL_SECONDS", 1)
    task = _ask(await _started_chat(home_id))
    await _pending(home_id)
    result = await asyncio.wait_for(task, 12.0)
    assert isinstance(result, PermissionResultDeny) and "timed out" in result.message
    await asyncio.sleep(1.1)  # the test database stores expiry to the whole second
    assert confirm.pending_requests(home_id) == []
    assert await confirm.on_owner_message(home_id, "yes") is None


async def test_one_yes_covers_one_request_asked_one_at_a_time(home):  # noqa: F811
    home_id, _ = home
    first_chat = await _started_chat(home_id, "job-1")
    second_chat = await _started_chat(home_id, "job-2")
    first = _ask(first_chat, "git push")
    await _pending(home_id)
    second = _ask(second_chat, "rm -rf build")
    pending = await _pending(home_id, 2)
    assert "git push" in pending[0]["question"] and "rm -rf build" in pending[1]["question"]
    note = away_mode.turn_note(home_id)
    assert "git push" in note and "rm -rf build" not in note and "1 more will follow" in note
    await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(first, 12.0), PermissionResultAllow)
    # The yes was spent on the request it answered; the other still waits.
    await asyncio.sleep(0.05)
    assert not second.done()
    [left] = confirm.pending_requests(home_id)
    assert "rm -rf build" in left["question"]
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(second, 12.0), PermissionResultDeny)
    # #483/#484: each answer names the request it settled, so its outcome goes
    # with the right request even with two jobs asking at once.
    answers = {a["request_id"]: a for a in _answers(home_id)}
    assert {rid: a["decision"] for rid, a in answers.items()} == {
        pending[0]["request_id"]: "approved", pending[1]["request_id"]: "declined"}
    assert answers[pending[0]["request_id"]]["asker"] == "Fix the flaky test"


def test_the_note_tells_the_model_not_to_repeat_the_request(monkeypatch):
    """#483: the app shows the request; a copy in the model's words is a second
    rendering that goes stale once the next request is asked."""
    monkeypatch.setattr(confirm, "pending_requests", lambda _sid: [
        {"question": "Do you authorize me to run `ls`? Please answer yes or no.", "unclear": 0}])
    note = confirm.context_note("s1")
    assert "Do not repeat, rephrase or summarise the request" in note
    assert "exactly" not in note and "Ask it" not in note


def test_the_marker_carries_the_parts_the_card_shows():
    marker = confirm.marker("Edit", {"file_path": "app/x.py"}, job="Refactor", chat_session_id="w1")
    assert marker == {"question": confirm.question("Edit", {"file_path": "app/x.py"}, job="Refactor"),
                      "action": "change app/x.py", "asker": "Refactor", "chat_session_id": "w1"}


async def test_a_refused_send_still_records_the_outcome(home, monkeypatch):  # noqa: F811
    """No turn will carry the owner's message (e.g. the chat was archived
    meanwhile), but the request was settled: its outcome is still recorded."""
    home_id, _ = home
    task = _ask(await _started_chat(home_id))
    [request] = await _pending(home_id)

    def refuse(*args, **kwargs):
        raise RuntimeError("refused")

    monkeypatch.setattr(turn_manager, "_submit_blocking", refuse)
    with pytest.raises(RuntimeError):
        await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
    [recorded] = _outcomes(home_id)
    assert recorded["data"]["request_id"] == request["request_id"]


async def test_peer_messages_tool_output_and_models_cannot_approve(home):  # noqa: F811
    home_id, _ = home
    chat_id = await _started_chat(home_id)
    task = _ask(chat_id)
    [request] = await _pending(home_id)
    # A peer message into the home saying yes.
    orch._queue(chat_id, {"session_id": home_id, "message": "yes", "request_id": "fake-yes"}, "message")
    # Server-originated turns carrying the word, as a wake, research or tool relay would.
    for source in ("agent_message", "unpark", "assistant", "automation", "api"):
        await turn_manager.submit_async(home_id, source, prompt="yes, I authorize it",
                                        profile_id="assistant-agent")
    # The owner's yes typed into the started chat is not an answer in the home.
    await turn_manager.submit_async(chat_id, "user", prompt="yes", profile_id="assistant-agent")
    await asyncio.sleep(0.05)
    assert not task.done()
    assert [item["request_id"] for item in confirm.pending_requests(home_id)] == [request["request_id"]]
    await owner_says(home_id, "no")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)


async def test_the_homes_own_prompt_is_asked_in_conversation_too(home):  # noqa: F811
    home_id, _ = home
    kwargs = dict(request_id=f"perm-{home_id}-own", session_id=home_id, profile_id="assistant-agent",
                  tool_name="Bash", tool_input={"command": "gh pr merge 12"}, broadcast_func=AsyncMock())
    task = asyncio.create_task(approvals.route_permission(kwargs, ph.permission_handler.request_permission))
    [request] = await _pending(home_id)
    assert request["question"] == "Do you authorize me to run `gh pr merge 12`? Please answer yes or no."
    assert request["chat_session_id"] is None
    await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)


async def test_ordinary_prompts_keep_their_card(mock_db):
    kwargs = dict(request_id="perm-plain-1", session_id="ordinary", profile_id="p", tool_name="Bash",
                  tool_input={"command": "ls"}, broadcast_func=AsyncMock())
    task = asyncio.create_task(approvals.route_permission(kwargs, ph.permission_handler.request_permission))
    for _ in range(300):
        rows = [r for r in interactions.get_pending_interactions("ordinary") if r.get("kind") == "permission"]
        if rows:
            break
        await asyncio.sleep(0.01)
    assert rows and "assistant_confirm" not in rows[0]["payload"]
    assert await confirm.on_owner_message("ordinary", "yes") is None
    assert not task.done()
    await ph.permission_handler.respond("perm-plain-1", "ordinary", "deny", broadcast_func=AsyncMock())
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultDeny)


async def test_voice_delegated_turns_never_answer(home):  # noqa: F811
    """A turn a voice call hands to chat is model-written (Realtime) or a
    wrapped transcript (Live): it neither approves nor declines, and does not
    count as an unclear reply. The owner's typed yes still approves."""
    from app.core import voice_stream
    home_id, _ = home
    task = _ask(await _started_chat(home_id))
    [request] = await _pending(home_id)
    # The real voice entry point, as the Realtime path calls it with the
    # model's `request` text.
    assert await voice_stream.delegate_to_selected_model(home_id, "yes", on_submitted=AsyncMock()) == ""
    # Two Live-style wrapped deliveries would read as unclear twice.
    for _ in range(2):
        await voice_stream.delegate_to_selected_model(
            home_id, "Live conversation context (only the latest new request needs action):\nuser: hm\n",
            on_submitted=AsyncMock())
    await asyncio.sleep(0.05)
    assert not task.done()
    [still] = confirm.pending_requests(home_id)
    assert still["request_id"] == request["request_id"] and not still["parked"]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source='user'", (home_id,))
        delegated = [turns._row(row) for row in cursor.fetchall()]
    assert len([t for t in delegated if t["payload"].get(turn_manager.VOICE_DELEGATION)]) == 3
    await owner_says(home_id, "yes")
    assert isinstance(await asyncio.wait_for(task, 12.0), PermissionResultAllow)
