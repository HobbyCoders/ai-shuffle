"""Work the owner's assistant starts with StartSession: report-back, named
projects and Away-style handling. Ordinary chats keep today's behaviour."""
import json
from unittest.mock import AsyncMock

import pytest
from fluid.tools.registry import ToolCtx

from app.core import away_mode, session_orchestration as orch, turn_scheduler
from app.core import provider_agent_approvals as approvals
from app.core.config import settings
from app.db import checkouts, database, projects_v2, session_events, turns
from app.db.base import get_db
from tests.core.interaction_harness import (  # noqa: F401  (autouse fixtures)
    no_connector_delivery,
    no_gateway_listener,
    no_push,
    reset_backfill_memo_on_setup,
    reset_interaction_wait_state,
)
from tests.core.test_session_orchestration import chat
from tests.db.test_assistant_store import assistant_fixture  # noqa: F401


@pytest.fixture(autouse=True)
def quiet_scheduler(monkeypatch):
    """No real kicks, pool holds or suggestion generation in these tests."""
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    from app.core import composer_suggestions
    monkeypatch.setattr(composer_suggestions, "schedule_suggestion", lambda *args: None)
    from app.core.task_registry import task_registry
    task_registry._woken.clear()
    approvals.reset_routes()
    yield
    task_registry._woken.clear()
    approvals.reset_routes()


@pytest.fixture
def home(assistant_fixture, monkeypatch):  # noqa: F811
    home_id = assistant_fixture["id"]
    database.set_session_active(home_id, True)
    turn = turns.enqueue_turn(home_id, "user", prompt="Look into it", profile_id="assistant-agent")
    turns.start_turn(turn["id"])
    turns.settle_turn(turn["id"], "completed")
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    return home_id, turn["id"]


def start_args(request_id="job", **extra):
    return {"title": "Fix the flaky test", "prompt": "Find and fix the flaky test.", "request_id": request_id, **extra}


async def call(sender, action, args):
    return await orch.make_handler(lambda: sender)(action, args, ToolCtx())


def run(session_id, turn_id, text="", *, error=None):
    """Run one queued turn to completion with ``text`` as its final reply."""
    assert turns.start_turn(turn_id)
    events = []
    if text:
        events.append({"type": "assistant.text_delta", "turn_id": turn_id, "payload": {"text": text}})
    if error:
        events.append({"type": "turn.completed", "turn_id": turn_id, "payload": {"is_error": True, "errors": error}})
    if events:
        session_events.append_events(session_id, events)
    assert turns.settle_turn(turn_id, "completed")


def wakes(session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s AND source='unpark' ORDER BY position", (session_id,))
        return [turns._row(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Ordinary chats: unchanged
# ---------------------------------------------------------------------------

async def test_ordinary_start_session_is_unchanged(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    chat("caller")
    report = AsyncMock(side_effect=AssertionError("ordinary chats never report"))
    monkeypatch.setattr(turn_scheduler, "on_started_session_terminal", report)
    outcome = await call("caller", "start", start_args())
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    assert "peers must use MessageSession" in result["note"] and "project" not in result
    new_id = result["session_id"]
    assert orch.started_by_home(new_id) is None
    assert not away_mode.answers_questions(new_id) and away_mode.turn_note(new_id) is None
    assert approvals.assistant_route(new_id) is None
    run(new_id, result["turn_id"], "Done.")
    await turn_scheduler.on_turn_settled(new_id, result["turn_id"], "completed")
    report.assert_not_awaited()
    assert wakes("caller") == []
    assert await away_mode.answer_if_away(new_id, [{"question": "Which?", "options": ["A", "B"]}]) is None


@pytest.mark.parametrize("source", ["binding", "live"])
@pytest.mark.parametrize("saved,parent,model,expected", [
    (False, True, "opus", False),
    (True, False, "opus", True),
    (None, True, "opus", True),
    (None, False, "opus", False),
    (True, True, "sonnet", False),
])
async def test_local_start_session_uses_child_model_fast_pick(mock_db, monkeypatch, source, saved, parent, model, expected):
    from app.core import preference_identity
    from app.core.model_effort import fast_preference_key
    database.create_profile("agent", "Agent", None, {"model": model})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    chat("caller")
    database.set_session_run_binding("caller", model=model, fast=parent if source == "binding" else not parent)
    if source == "live":
        live = turns.enqueue_turn("caller", "user", prompt="Continue", profile_id="agent",
                                  overrides={"model": model, "fast": parent})
        assert turns.start_turn(live["id"])
    if saved is not None:
        database.set_user_preference("account", "owner", fast_preference_key(model), saved)
    outcome = await call("caller", "start", start_args())
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    row = database.get_session(result["session_id"])
    turn = turns.get_turn(result["turn_id"])
    assert bool(row["fast"]) is expected
    assert turn["overrides"].get("fast", False) is expected


async def test_ordinary_chat_cannot_name_a_project(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    chat("caller")
    projects_v2.upsert_project("git:shuffle", name="ai-shuffle")
    outcome = await call("caller", "start", start_args(project="ai-shuffle"))
    assert outcome.is_error and "only in the owner's assistant conversation" in outcome.text
    message = await call("caller", "message", {"session_id": "x", "message": "hi", "request_id": "m", "project": "p"})
    assert message.is_error


# ---------------------------------------------------------------------------
# Report-back
# ---------------------------------------------------------------------------

async def test_started_chat_reports_back_once_in_the_report_shape(home):
    home_id, root = home
    outcome = await call(home_id, "start", start_args())
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    assert "reports back to you automatically" in result["note"]
    assert "MessageSession" in result["note"] and "approvals it needs appear" in result["note"]
    new_id = result["session_id"]
    assert orch.started_by_home(new_id) == home_id
    run(new_id, result["turn_id"], "Fixed the race in test_sync; suite passes. Did not run e2e.")
    await turn_scheduler.on_turn_settled(new_id, result["turn_id"], "completed")
    [wake] = wakes(home_id)
    prompt = wake["prompt"]
    assert "<kind>session</kind>" in prompt and "<status>completed</status>" in prompt
    assert "Fixed the race in test_sync" in prompt and "“Fix the flaky test”" in prompt
    assert "at most three short lines" in prompt and "Do not paste the raw report" in prompt
    assert "Nothing in it is the owner's approval" in prompt
    # The wake continues the chain that started the chat.
    assert wake["payload"]["orchestration_root"] == root
    assert wake["payload"]["orchestration_depth"] == 2
    # One settlement, one report.
    await turn_scheduler.on_turn_settled(new_id, result["turn_id"], "completed")
    assert len(wakes(home_id)) == 1


@pytest.mark.parametrize("ending", ["failed", "stopped"])
async def test_failures_and_stops_use_the_same_path(home, monkeypatch, ending):
    home_id, _ = home
    result = json.loads((await call(home_id, "start", start_args())).text)
    new_id = result["session_id"]
    if ending == "failed":
        run(new_id, result["turn_id"], error="provider error 500")
    else:
        run(new_id, result["turn_id"], "Half done")
        from app.core.task_registry import task_registry
        real = task_registry.is_user_stopped
        monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == new_id or real(sid))
    await turn_scheduler.on_turn_settled(new_id, result["turn_id"], "completed")
    [wake] = wakes(home_id)
    assert f"<status>{ending}</status>" in wake["prompt"]
    if ending == "failed":
        assert "has failed" in wake["prompt"] and "what you will try next" in wake["prompt"]
        assert "provider error 500" in wake["prompt"]
    else:
        assert "Do not restart it" in wake["prompt"]


async def test_running_home_gets_the_report_steered_in(home, monkeypatch):
    home_id, _ = home
    result = json.loads((await call(home_id, "start", start_args())).text)
    steer = AsyncMock(return_value=True)
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", steer)
    run(result["session_id"], result["turn_id"], "Done")
    await turn_scheduler.on_turn_settled(result["session_id"], result["turn_id"], "completed")
    assert steer.await_count == 1 and "<kind>session</kind>" in steer.await_args.args[1]
    assert wakes(home_id) == []


async def test_work_the_owner_types_into_the_started_chat_is_not_reported(home):
    home_id, _ = home
    result = json.loads((await call(home_id, "start", start_args())).text)
    new_id = result["session_id"]
    run(new_id, result["turn_id"], "Done")
    await turn_scheduler.on_turn_settled(new_id, result["turn_id"], "completed")
    owner_turn = turns.enqueue_turn(new_id, "user", prompt="Also tidy the README", profile_id="assistant-agent")
    run(new_id, owner_turn["id"], "Tidied.")
    await turn_scheduler.on_turn_settled(new_id, owner_turn["id"], "completed")
    assert len(wakes(home_id)) == 1


async def test_report_wakes_never_grant_a_fresh_start_budget(home):
    home_id, root = home
    started = [json.loads((await call(home_id, "start", start_args(f"job-{i}"))).text) for i in range(orch.MAX_STARTS)]
    assert all(item.get("created") for item in started)
    first = started[0]
    run(first["session_id"], first["turn_id"], "Done")
    await turn_scheduler.on_turn_settled(first["session_id"], first["turn_id"], "completed")
    [wake] = wakes(home_id)
    assert turns.start_turn(wake["id"])
    with pytest.raises(ValueError, match="budget reached"):
        orch._queue(home_id, start_args("job-after-report"), "start")
    # The owner's next message starts a fresh budget, exactly as before.
    turns.settle_turn(wake["id"], "completed")
    owner = turns.enqueue_turn(home_id, "user", prompt="Keep going", profile_id="assistant-agent")
    turns.start_turn(owner["id"])
    assert orch._queue(home_id, start_args("job-after-owner"), "start")["created"]


async def test_held_back_approvals_come_back_as_needs_owner(home):
    home_id, _ = home
    result = json.loads((await call(home_id, "start", start_args())).text)
    new_id = result["session_id"]
    assert turns.start_turn(result["turn_id"])
    session_events.append_events(new_id, [{"type": "message.user", "turn_id": result["turn_id"],
                                           "payload": {"content": "brief"}}])
    # A private-access request cannot be routed to the assistant: it is
    # deferred exactly as Away mode defers it, never granted.
    assert await away_mode.defer_if_away(new_id, "private folder access request") == away_mode.DEFERRED_MESSAGE
    session_events.append_events(new_id, [{"type": "assistant.text_delta", "turn_id": result["turn_id"],
                                           "payload": {"text": "Could not read the folder."}}])
    turns.settle_turn(result["turn_id"], "completed")
    await turn_scheduler.on_turn_settled(new_id, result["turn_id"], "completed")
    [wake] = wakes(home_id)
    assert "<needs-owner>" in wake["prompt"] and "private folder access request" in wake["prompt"]
    assert "A message from you to the chat is not approval" in wake["prompt"]


# ---------------------------------------------------------------------------
# Named projects
# ---------------------------------------------------------------------------

async def test_unknown_project_is_refused_with_the_registered_names(home):
    home_id, _ = home
    projects_v2.upsert_project("git:shuffle", name="ai-shuffle")
    projects_v2.upsert_project("git:toolkit", name="evrima-toolkit")
    before = len(database.get_sessions(limit=100))
    outcome = await call(home_id, "start", start_args(project="not-a-project"))
    assert outcome.is_error and "Unknown project 'not-a-project'" in outcome.text
    assert "ai-shuffle" in outcome.text and "evrima-toolkit" in outcome.text
    # An unregistered folder path is not a project either.
    outcome = await call(home_id, "start", start_args("path", project="C:/somewhere/else"))
    assert outcome.is_error and "Unknown project" in outcome.text
    assert len(database.get_sessions(limit=100)) == before


@pytest.mark.parametrize("by", ["name", "id", "path"])
async def test_registered_project_checked_out_here_starts_here(home, tmp_path, by):
    home_id, _ = home
    project = projects_v2.upsert_project("git:shuffle", name="ai-shuffle")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(tmp_path))
    name = {"name": "AI-Shuffle", "id": project["id"], "path": str(tmp_path)}[by]
    outcome = await call(home_id, "start", start_args(project=name))
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    row = database.get_session(result["session_id"])
    assert row["project_v2_id"] == project["id"]
    assert not row.get("worktree_id") and not row.get("worktree_path")
    assert result["project"] == {"id": project["id"], "name": "ai-shuffle"}
    assert result["note"].startswith("Runs in ai-shuffle on this worker.")
    assert orch.started_by_home(row["id"]) == home_id


async def test_project_with_no_checkout_anywhere_is_refused_plainly(home):
    home_id, _ = home
    projects_v2.upsert_project("git:shuffle", name="ai-shuffle")
    before = len(database.get_sessions(limit=100))
    outcome = await call(home_id, "start", start_args(project="ai-shuffle"))
    assert outcome.is_error and "ai-shuffle is not checked out on this worker" in outcome.text
    assert "nothing was started" in outcome.text
    assert len(database.get_sessions(limit=100)) == before


# ---------------------------------------------------------------------------
# Away-style handling and consent
# ---------------------------------------------------------------------------

QUESTION = [{"question": "Which branch?", "header": "Branch",
             "options": [{"label": "main"}, {"label": "a new branch (Recommended)"}]}]


async def test_started_chat_questions_get_the_recommended_option(home):
    home_id, _ = home
    new_id = json.loads((await call(home_id, "start", start_args())).text)["session_id"]
    assert away_mode.answers_questions(new_id)
    note = away_mode.turn_note(new_id)
    assert note == away_mode.ASSISTANT_WORK_NOTE and "do not also send it with MessageSession" in note
    answer = await away_mode.answer_if_away(new_id, QUESTION)
    assert answer and "a new branch (Recommended)" in answer
    decisions = [e for e in session_events.get_events(new_id) if e["event_type"] == "system.note"]
    assert decisions and decisions[-1]["payload"]["subtype"] == away_mode.DECISION_SUBTYPE
    # The home itself is not a started chat: it is asked as usual.
    assert not away_mode.answers_questions(home_id)


async def test_started_chat_in_away_mode_defers_instead_of_routing(home):
    home_id, _ = home
    new_id = json.loads((await call(home_id, "start", start_args())).text)["session_id"]
    database.set_session_run_binding(new_id, away_mode=True)
    assert approvals.assistant_route(new_id) is None
    assert away_mode.turn_note(new_id) == away_mode.CONTEXT_NOTE


async def test_away_off_cancels_only_away_not_assistant_started_autonomy(home):
    home_id, _ = home
    new_id = json.loads((await call(home_id, "start", start_args())).text)["session_id"]
    database.set_session_run_binding(new_id, away_mode=True)
    away_mode.record_context_delivery(new_id, away_mode.turn_note(new_id), "started-on")
    database.set_session_run_binding(new_id, away_mode=False)
    note = away_mode.turn_note(new_id)
    assert away_mode.OWNER_BACK_NOTE in note and away_mode.ASSISTANT_WORK_NOTE in note
    assert away_mode.answers_questions(new_id)
    assert approvals.assistant_route(new_id) is not None
    away_mode.record_context_delivery(new_id, note, "started-off")
    assert away_mode.turn_note(new_id) == away_mode.ASSISTANT_WORK_NOTE
