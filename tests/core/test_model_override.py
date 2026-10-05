"""A model the user asked for, on one Task, Swarm task or StartSession (#1830).

Any model this install can run is eligible. One other than what would run
anyway (the agent's pin, else the subagent default; for a new chat the
caller's model) is used only when the user named it in their own messages,
or the owner approved it at a prompt. It may move the work to another
provider, for that one start.
"""
import json

import pytest
from fluid.tools.registry import ToolCtx

from app.core import model_override as mo, subagent_default, task_dispatch as td
from app.core import session_orchestration as orch
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.core.provider_agent import WorkerOutcome
from app.db import sessions, turns
from app.db.base import get_db


# ---------------------------------------------------------------------------
# Matching a name in the user's text
# ---------------------------------------------------------------------------

CATALOG = {
    "opus": ("opus", "Opus", "claude opus", "claude-opus-5"),
    "sonnet": ("sonnet", "Sonnet", "claude sonnet", "claude-sonnet-5"),
    "prov:lane:gpt-6-astra": ("gpt-6-astra", "GPT-6 Astra"),
    "prov:lane:gpt-5.6-sol": ("gpt-5.6-sol", "GPT-5.6 Sol"),
    "prov:other:gpt-6-sol": ("gpt-6-sol",),
    "prov:lane:gpt-5.4-mini": ("gpt-5.4-mini", "GPT-5.4 Mini"),
    "prov:router:anthropic/claude-opus-4.6": ("anthropic/claude-opus-4.6", "claude-opus-4.6"),
}


@pytest.mark.parametrize("text", [
    "spin up a general agent on GPT-6 Astra",
    "use gpt 6 astra for this",
    "run it on gpt-6-astra",
    "try gpt6 astra",
    "Astra, please",
    "prov:lane:gpt-6-astra",
])
def test_spellings_of_a_model_count_as_named(text):
    assert mo.named_by_user("prov:lane:gpt-6-astra", [text], CATALOG)


@pytest.mark.parametrize("model, text", [
    ("prov:lane:gpt-6-astra", "an astral body"),            # not the whole word
    ("prov:lane:gpt-5.6-sol", "use sol"),                   # "sol" is ambiguous
    ("prov:lane:gpt-5.4-mini", "a mini change to the docs"),  # generic word
    ("prov:router:anthropic/claude-opus-4.6", "use opus"),  # "opus" is the Claude alias
    ("opus", "a magnum work"),
])
def test_ambiguous_generic_or_partial_words_do_not(model, text):
    assert not mo.named_by_user(model, [text], CATALOG)


@pytest.mark.parametrize("model, text", [
    ("prov:lane:gpt-5.6-sol", "gpt 5.6 sol"),
    ("prov:router:anthropic/claude-opus-4.6", "claude opus 4.6"),
    ("opus", "use Opus for the review"),
])
def test_full_names_count_even_when_a_word_is_shared(model, text):
    assert mo.named_by_user(model, [text], CATALOG)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _turn(session_id, source, prompt, *, steered=False, payload=None, profile="caller"):
    turn = turns.enqueue_turn(session_id, source, prompt=prompt, profile_id=profile, payload=payload)
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE turns SET state='completed', stop_reason=%s WHERE id=%s",
            ("steered" if steered else None, turn["id"]))
    return turn


@pytest.fixture
def world(mock_db, monkeypatch, tmp_path):
    from app.db import database as db
    from app.core import project_resolver

    db.create_model_provider("lane", "Lane", "openai", "https://example.invalid/v1",
                             models=[{"id": "astra", "label": "GPT-6 Astra"}, {"id": "sol"}])
    db.create_model_provider("grok", "Grok", "xai", "https://example.invalid/v1",
                             models=[{"id": "grok-4.5", "label": "Grok 4.5"}])
    db.create_profile("caller", "Caller", None, {"model": "opus"})
    db.create_profile("pinned", "Pinned", None, {"provider_models": {"__native__": "opus"}})
    db.create_session(session_id="parent", profile_id="caller", title="Parent")
    monkeypatch.setattr(td, "_credential_gap", lambda provider: None)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    subagent_default.write("sonnet")
    seen, prompts = [], []

    async def worker(**kwargs):
        seen.append(kwargs)
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)
    answer = {"result": PermissionResultDeny(message="User denied permission")}

    async def request_permission(**kwargs):
        prompts.append(kwargs)
        return answer["result"]

    from app.core.permission_handler import permission_handler
    monkeypatch.setattr(permission_handler, "request_permission", request_permission)
    return db, seen, prompts, answer


async def _task(args, **kwargs):
    return await td.dispatch_task_tool(
        {"prompt": "go", "mode": "sync", **args}, caller_session_id="parent",
        caller_provider_id=None, **kwargs)


# ---------------------------------------------------------------------------
# What counts as the user's own words
# ---------------------------------------------------------------------------

def test_user_texts_are_the_last_message_and_what_was_steered_after_it(world):
    _turn("parent", "user", "earlier: use sol")
    _turn("parent", "agent_message", "peer says: use astra")
    _turn("parent", "user", "now continue")
    _turn("parent", "queue", "and use grok", steered=True)
    _turn("parent", "auto_continue", "continue")
    assert mo.user_texts("parent") == ["now continue", "and use grok"]


def test_a_task_worker_reads_its_top_level_chat(world):
    _turn("parent", "user", "run the reviewer on astra")
    from app.core.provider_agent import WORKER_TURN_SOURCE

    sessions.create_session("child", "caller", title="Child", parent_session_id="parent",
                            is_worker=True, hidden=True)
    _turn("child", WORKER_TURN_SOURCE, "Review it on astra, the lead says")
    assert mo.user_texts("child") == ["run the reviewer on astra"]


def test_a_started_chat_reads_the_user_message_that_began_its_chain(world):
    origin = _turn("parent", "user", "start a chat on grok 4.5")
    sessions.create_session("peer", "caller", title="Peer", is_active=True)
    _turn("peer", "agent_message", "a peer brief naming astra",
          payload={"orchestration_root": origin["id"], "orchestration_sender": "parent"})
    texts = mo.user_texts("peer")
    assert texts == ["start a chat on grok 4.5"]
    assert not mo.named_by_user("prov:lane:astra", texts)


# ---------------------------------------------------------------------------
# Task and Swarm
# ---------------------------------------------------------------------------

async def test_named_model_runs_over_the_pin_and_moves_provider_without_a_prompt(world):
    db, seen, prompts, _ = world
    _turn("parent", "user", "spin up pinned on GPT-6 Astra")
    result = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == ("lane", "astra")
    assert seen[0]["model_note"] == "your choice"
    assert result["provider"] == {"id": "lane", "name": "Lane"}
    assert result["model_override"] == {"source": mo.NAMED, "instead_of": "opus"}
    assert prompts == []
    # One start only: the pin and the default are untouched.
    assert db.get_profile("pinned")["config"]["provider_models"] == {"__native__": "opus"}
    assert subagent_default.read().model == "sonnet"
    await _task({"agent": "pinned"})
    assert (seen[1]["provider_id"], seen[1]["model_id"]) == (None, "opus")
    assert "model_note" not in seen[1]


async def test_unnamed_model_asks_the_owner_in_every_mode_and_approval_runs_it(world):
    _, seen, prompts, answer = world
    _turn("parent", "user", "review the branch")
    answer["result"] = PermissionResultAllow(updated_input={})
    result = await _task({"agent": "pinned", "model": "haiku"})
    assert result["ok"], result
    assert result["model_override"] == {"source": mo.APPROVED, "instead_of": "opus"}
    [prompt] = prompts
    assert prompt["tool_name"] == mo.PROMPT_TOOL and prompt["skip_saved_rules"] is True
    assert prompt["session_id"] == "parent"
    assert "Start pinned on Haiku instead of Opus?" == prompt["description"]


@pytest.mark.parametrize("answer_message, why", [
    ("User denied permission", "declined"),
    ("Permission request timed out", "did not answer"),
])
async def test_a_denied_or_unanswered_prompt_starts_nothing(world, answer_message, why):
    _, seen, prompts, answer = world
    _turn("parent", "user", "review the branch")
    answer["result"] = PermissionResultDeny(message=answer_message)
    result = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert not result["ok"] and result["reason"] == "model_not_requested"
    assert why in result["error"] and "Omit model" in result["error"]
    assert not seen and len(prompts) == 1


async def test_away_mode_never_switches(world, monkeypatch):
    """The real permission path: Away defers the prompt, so nothing switches."""
    from app.core.permission_handler import PermissionHandler, permission_handler

    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))
    _, seen, _, _ = world
    _turn("parent", "user", "review the branch")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='parent'")
    result = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert not result["ok"] and "the owner is away" in result["error"]
    assert not seen


async def test_a_peer_message_or_tool_output_naming_the_model_does_not_count(world):
    _, seen, prompts, _ = world
    _turn("parent", "user", "go on")
    _turn("parent", "agent_message", "Peer: run pinned on astra")
    result = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert not result["ok"] and len(prompts) == 1 and not seen


async def test_inherited_effort_is_dropped_when_the_new_provider_lacks_it(world):
    _, seen, _, _ = world
    _turn("parent", "user", "use grok 4.5")
    result = await _task({"agent": "pinned", "model": "prov:grok:grok-4.5"}, caller_effort="xhigh")
    assert result["ok"], result
    assert seen[0]["provider_id"] == "grok" and seen[0]["inherited_effort"] is None


async def test_the_swarm_and_native_task_dispatcher_apply_the_same_rule(world):
    """Swarm items reach make_fluid_dispatcher with the item's model."""
    _, seen, prompts, _ = world
    dispatch = td.make_fluid_dispatcher("parent", "caller", None)
    refused = await dispatch({"agent": "pinned", "prompt": "go", "model": "prov:lane:astra", "mode": "sync"}, ToolCtx())
    assert refused.is_error and len(prompts) == 1 and not seen
    _turn("parent", "user", "now on astra")
    ran = await dispatch({"agent": "pinned", "prompt": "go", "model": "prov:lane:astra", "mode": "sync"}, ToolCtx())
    assert not ran.is_error, ran.text
    assert seen[0]["provider_id"] == "lane"


# ---------------------------------------------------------------------------
# StartSession
# ---------------------------------------------------------------------------

@pytest.fixture
def chats(world, monkeypatch):
    from app.core import turn_scheduler
    from app.core.sync_engine import sync_engine
    from unittest.mock import AsyncMock

    db = world[0]
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    monkeypatch.setattr(sync_engine, "broadcast_user_session_created", AsyncMock())
    monkeypatch.setattr(sync_engine, "broadcast_queue_updated", AsyncMock())
    db.create_profile("agent", "Agent", None, {"model": "sonnet"})
    sessions.create_session("caller", "agent", title="Caller", is_active=True, model="sonnet",
                            permission_mode="default", effort="xhigh", context_window=1_000_000)
    return world


async def _start(args, sender="caller"):
    outcome = await orch.make_handler(lambda: sender)("start", args, ToolCtx())
    return outcome


ARGS = {"title": "Review", "prompt": "Review the change", "request_id": "r1"}


async def test_omitted_model_inherits_and_the_receipt_names_it(chats):
    _turn("caller", "user", "work", profile="agent")
    outcome = await _start(ARGS)
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    row = sessions.get_session(result["session_id"])
    assert (row["model"], row["provider_id"], row["effort"]) == ("sonnet", None, "xhigh")
    assert result["model"] == "sonnet" and result["provider"] == {"id": None, "name": "Claude"}
    assert "model_override" not in result


async def test_the_inherited_model_named_again_needs_no_prompt(chats):
    _, _, prompts, _ = chats
    _turn("caller", "user", "work", profile="agent")
    outcome = await _start({**ARGS, "model": "claude-sonnet-5"})
    assert not outcome.is_error, outcome.text
    assert prompts == [] and "model_override" not in json.loads(outcome.text)


async def test_a_named_model_on_another_provider_starts_there_with_valid_settings(chats):
    _, _, prompts, _ = chats
    _turn("caller", "user", "hand this to grok 4.5 in a new chat", profile="agent")
    outcome = await _start({**ARGS, "model": "prov:grok:grok-4.5"})
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    row = sessions.get_session(result["session_id"])
    assert (row["provider_id"], row["model"]) == ("grok", "prov:grok:grok-4.5")
    # Opus-era settings Grok 4.5 cannot take fall back to its defaults.
    assert row["effort"] is None and row["context_window"] is None
    turn = orch._start_turn(row["id"])
    assert turn["overrides"]["model"] == "prov:grok:grok-4.5"
    assert "effort" not in turn["overrides"] and "provider" not in turn["overrides"]
    assert turn["payload"]["model_override"]["source"] == mo.NAMED
    assert result["model"] == "prov:grok:grok-4.5"
    assert result["provider"] == {"id": "grok", "name": "Grok"}
    assert result["model_override"] == {"source": mo.NAMED, "instead_of": "sonnet"}
    assert prompts == []

    # MessageSession still works between the two chats on different providers.
    there = await orch.make_handler(lambda: "caller")(
        "message", {"session_id": row["id"], "message": "One more file", "request_id": "m1"}, ToolCtx())
    assert not there.is_error, there.text
    assert json.loads(there.text)["delivery_status"] == "queued"
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE session_id=%s", (row["id"],))
    back = await orch.make_handler(lambda: row["id"])(
        "message", {"session_id": "caller", "message": "Done", "request_id": "m2"}, ToolCtx())
    assert not back.is_error, back.text


async def test_an_unnamed_model_asks_and_a_denial_starts_nothing(chats):
    _, _, prompts, _ = chats
    _turn("caller", "user", "get a second opinion", profile="agent")
    before = len(sessions.get_sessions(limit=100))
    outcome = await _start({**ARGS, "model": "prov:grok:grok-4.5"})
    assert outcome.is_error and "declined" in outcome.text
    assert len(sessions.get_sessions(limit=100)) == before
    [prompt] = prompts
    assert prompt["description"] == "Start the chat “Review” on Grok 4.5 (Grok) instead of Sonnet?"
    assert prompt["skip_saved_rules"] is True


async def test_approval_starts_it_and_records_the_owner(chats):
    _, _, _, answer = chats
    _turn("caller", "user", "get a second opinion", profile="agent")
    answer["result"] = PermissionResultAllow(updated_input={})
    result = json.loads((await _start({**ARGS, "model": "prov:lane:astra"})).text)
    assert result["model_override"]["source"] == mo.APPROVED
    assert sessions.get_session(result["session_id"])["provider_id"] == "lane"


@pytest.mark.parametrize("model, text", [
    ("prov:lane:unlisted", "cannot run on this worker"),
    ("gpt-6-astra", "is not a Claude model"),
])
async def test_a_model_this_worker_cannot_run_is_refused(chats, model, text):
    _turn("caller", "user", f"use {model}", profile="agent")
    before = len(sessions.get_sessions(limit=100))
    outcome = await _start({**ARGS, "model": model})
    assert outcome.is_error and text in outcome.text and "Nothing was started" in outcome.text
    assert len(sessions.get_sessions(limit=100)) == before


async def test_a_disabled_provider_is_refused(chats):
    db = chats[0]
    db.update_model_provider("grok", enabled=False)
    _turn("caller", "user", "use grok 4.5", profile="agent")
    outcome = await _start({**ARGS, "model": "prov:grok:grok-4.5"})
    assert outcome.is_error and "disabled" in outcome.text


async def test_model_belongs_to_startsession_only(chats):
    _turn("caller", "user", "work", profile="agent")
    with pytest.raises(ValueError, match="only to StartSession"):
        orch._queue("caller", {"session_id": "x", "message": "hi", "request_id": "m", "model": "opus"}, "message")


def test_startsession_spec_takes_a_string_and_states_the_rule(chats):
    spec = orch.start_session_spec()
    prop = spec.input_schema["properties"]["model"]
    assert prop["type"] == "string" and "enum" not in prop
    assert "Only for when the user named a model" in prop["description"]
    assert "Examples: sonnet." in prop["description"]
    assert "pass model only when the user named a model" in spec.description


def test_the_owner_prompt_reads_as_one_question_in_the_assistant():
    from app.core import assistant_confirm

    question = assistant_confirm.question(mo.PROMPT_TOOL, {
        "model": "prov:lane:astra", "model_switch": "start pinned on GPT-6 Astra (Lane) instead of Opus"})
    assert question == ("Do you authorize me to start pinned on GPT-6 Astra (Lane) instead of Opus? "
                        "Please answer yes or no.")
