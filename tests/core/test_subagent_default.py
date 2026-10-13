"""One global subagent default in place of a required model per agent (#1827).

Resolution when a subagent starts: a model named for this task (used only
when the user asked for it, #1830; see test_model_override.py), else the
agent's pin for the caller's provider, else the default, else a refusal that
asks the owner once. Never the session's model.
"""
import asyncio

import pytest

from app.core import agent_provider_models as apm, subagent_default, task_dispatch as td
from app.core.provider_agent import WorkerOutcome


@pytest.fixture
def lane(mock_db, monkeypatch, tmp_path, owner):
    from app.db import database as db
    from app.core import project_resolver

    db.create_model_provider(
        "lane", "Lane", "openai", "https://example.invalid/v1",
        models=[{"id": "astra"}, {"id": "sol"}], default_model="astra",
    )
    db.create_profile("caller", "Caller", None, {"model": "opus"})
    db.create_profile("pinned", "Pinned", None, {"provider_models": {"__native__": "haiku"}})
    db.create_profile("plain", "Plain", None, {"description": "no model of its own"})
    db.create_session(session_id="parent", profile_id="caller", title="Parent")
    monkeypatch.setattr(td, "_credential_gap", lambda provider: None)
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda row: str(tmp_path))
    subagent_default.reset_asked()
    asked = []
    monkeypatch.setattr(subagent_default, "ask_owner_once",
                        lambda session_id=None: asked.append(session_id) or True)
    seen = []

    async def worker(**kwargs):
        seen.append(kwargs)
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)
    return db, seen, asked


class _FakeOwner:
    """Stands in for the owner's question card (no sockets, no real wait)."""

    def __init__(self, monkeypatch):
        from app.core import away_mode, sync_engine as se, user_question_handler as uqh

        self.reply = {uqh.DISMISSED_KEY: True, "error": uqh.DISMISS_MESSAGE}
        self.questions = []
        self.frames = []
        self.gate = None
        subagent_default._ASKING = None
        subagent_default._DISMISSED_AT.clear()

        async def ask(**kwargs):
            self.questions.append(kwargs)
            if self.gate is not None:
                await self.gate.wait()
            return self.reply

        async def frame(user_id, payload, source_device_id=None):
            self.frames.append((user_id, payload))

        async def attention(session_id):
            return None

        async def present(session_id):
            return False

        monkeypatch.setattr(uqh.user_question_handler, "_request_answers_v2", ask)
        monkeypatch.setattr(se.sync_engine, "broadcast_user_question", frame)
        monkeypatch.setattr(uqh, "publish_session_attention", attention)
        monkeypatch.setattr(away_mode, "answers_questions_async", present)

    def chooses(self, model):
        self.reply = {subagent_default.QUESTION_TEXT: model}


@pytest.fixture
def owner(monkeypatch):
    """The owner's chat: by default they dismiss the model question."""
    return _FakeOwner(monkeypatch)


async def _task(args, provider=None, session="parent", **kwargs):
    return await td.dispatch_task_tool(
        {"prompt": "go", "mode": "sync", **args},
        caller_session_id=session, caller_provider_id=provider, **kwargs,
    )


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("agent, model", [("pinned", "haiku"), ("plain", "sonnet")])
async def test_explicit_model_equal_to_the_pin_or_default_needs_no_check(lane, monkeypatch, agent, model):
    from app.core import model_override

    async def never(*args, **kwargs):
        raise AssertionError("a model the agent runs on anyway needs no check")

    monkeypatch.setattr(model_override, "confirm", never)
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": agent, "model": model})
    assert result["ok"], result
    assert seen[0]["model_id"] == model
    assert "model_override" not in result


async def test_explicit_model_this_install_cannot_run_is_refused(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": "plain", "model": "prov:lane:unlisted"})
    assert not result["ok"]
    assert "cannot run on this worker" in result["error"]
    assert "Nothing was dispatched" in result["error"]
    assert not seen


async def test_pin_beats_the_default(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": "pinned"})
    assert result["ok"], result
    assert seen[0]["model_id"] == "haiku"


async def test_unpinned_agent_runs_on_the_default(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == (None, "sonnet")


async def test_changing_the_default_changes_every_unpinned_agent(lane):
    _, seen, _ = lane
    subagent_default.write("sonnet")
    await _task({"agent": "plain"})
    subagent_default.write("opus")
    await _task({"agent": "plain"})
    await _task({"agent": "pinned"})
    assert [s["model_id"] for s in seen] == ["sonnet", "opus", "haiku"]


async def test_a_default_on_another_provider_pins_that_provider(lane):
    """Like an assignment that names another leg: the worker runs there."""
    _, seen, _ = lane
    subagent_default.write("prov:lane:sol")
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == ("lane", "sol")


async def test_no_default_refuses_and_asks_the_owner_never_the_session_model(lane, owner):
    """With no owner in the chat (an automation run), the refusal is as before."""
    db, seen, asked = lane
    db.create_session(session_id="nightly-run", profile_id="caller", automation_id="nightly")
    result = await _task({"agent": "plain"}, session="nightly-run")
    assert not result["ok"]
    assert not owner.questions
    assert "no owner is present" in result["error"]
    assert result["reason"] == "subagent_default_missing"
    assert "owner has not chosen a subagent default" in result["error"]
    assert "never runs on this session's model" in result["error"]
    assert "Agents page" in result["error"]
    assert asked == ["nightly-run"]
    assert not seen


async def test_no_default_refusal_points_at_a_user_named_model(lane):
    _, _, _ = lane
    refused = await _task({"agent": "plain"})
    assert "If the user named a model for this task, pass it as model" in refused["error"]


# ---------------------------------------------------------------------------
# Asking the owner in the chat (#1827)
# ---------------------------------------------------------------------------

async def test_no_default_asks_in_the_chat_saves_the_choice_and_dispatches(lane, owner):
    _, seen, asked = lane
    owner.chooses("sonnet")
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == (None, "sonnet")
    assert subagent_default.read().model == "sonnet"
    [question] = owner.questions
    assert question["session_id"] == "parent"
    assert question["ttl_seconds"] == subagent_default.ASK_TTL_SECONDS
    [card] = question["questions"]
    assert card["question"] == "Which model should subagents use?"
    assert card["picker"] == "model"
    # No options: Away mode can never pick a model for the owner.
    assert card["options"] == []
    assert not asked
    # The default now exists: the next delegation asks nothing.
    await _task({"agent": "plain"})
    assert len(owner.questions) == 1


async def test_a_choice_on_another_provider_moves_the_worker_there(lane, owner):
    _, seen, _ = lane
    owner.chooses("prov:lane:sol")
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert (seen[0]["provider_id"], seen[0]["model_id"]) == ("lane", "sol")


async def test_dismissing_refuses_and_the_next_delegation_asks_again(lane, owner, monkeypatch):
    _, seen, asked = lane
    refused = await _task({"agent": "plain"})
    assert not refused["ok"]
    assert refused["reason"] == "subagent_default_missing"
    assert "the owner dismissed the question" in refused["error"]
    assert not seen and not asked
    # A Task call right behind it (same batch) sees the same refusal...
    await _task({"agent": "plain"})
    assert len(owner.questions) == 1
    # ...and a later delegation asks again.
    monkeypatch.setattr(subagent_default, "DISMISS_QUIET_SECONDS", 0.0)
    owner.chooses("opus")
    result = await _task({"agent": "plain"})
    assert result["ok"], result
    assert len(owner.questions) == 2
    assert seen[0]["model_id"] == "opus"


async def test_several_task_calls_at_once_ask_one_question(lane, owner):
    _, seen, _ = lane
    owner.gate = asyncio.Event()
    owner.chooses("sonnet")
    calls = [asyncio.create_task(_task({"agent": "plain"})) for _ in range(3)]
    for _ in range(50):
        await asyncio.sleep(0.01)
        if owner.questions:
            break
    await asyncio.sleep(0.05)
    owner.gate.set()
    results = await asyncio.gather(*calls)
    assert all(r["ok"] for r in results), results
    assert len(owner.questions) == 1
    assert [s["model_id"] for s in seen] == ["sonnet"] * 3


@pytest.mark.parametrize("kind", [
    {"automation_id": "nightly"},
    {"api_user_id": "key-user"},
    {"hidden": True},
])
async def test_no_owner_present_keeps_the_refusal_and_never_shows_a_card(lane, owner, kind):
    db, seen, asked = lane
    if "api_user_id" in kind:
        db.create_api_user("key-user", "Key user", "key-user-hash")
    db.create_session(session_id="headless", profile_id="caller", **kind)
    result = await _task({"agent": "plain"}, session="headless")
    assert not result["ok"]
    assert "no owner is present" in result["error"]
    assert not owner.questions and not seen
    assert asked == ["headless"]


async def test_away_mode_never_answers_the_model_question(lane, owner, monkeypatch):
    _, seen, asked = lane

    async def away(session_id):
        return True

    monkeypatch.setattr("app.core.away_mode.answers_questions_async", away)
    result = await _task({"agent": "plain"})
    assert not result["ok"]
    assert not owner.questions and not seen
    assert asked == ["parent"]


async def test_a_worker_asks_in_its_owners_chat(lane, owner):
    db, seen, _ = lane
    db.create_session(session_id="child", profile_id="caller", parent_session_id="parent",
                      is_worker=True)
    owner.chooses("sonnet")
    result = await _task({"agent": "plain"}, session="child")
    assert result["ok"], result
    assert owner.questions[0]["session_id"] == "parent"


@pytest.mark.parametrize("reply, why", [
    ({subagent_default.QUESTION_TEXT: "gpt-nowhere"}, "not a model this app can run"),
    ({"error": "Question timed out - no response from user"}, "did not answer in time"),
    ({"error": "No answers received"}, "withdrawn"),
])
async def test_an_unusable_answer_refuses_and_saves_nothing(lane, owner, reply, why):
    _, seen, _ = lane
    owner.reply = reply
    result = await _task({"agent": "plain"})
    assert not result["ok"]
    assert why in result["error"]
    assert subagent_default.read().model is None
    assert not seen


async def test_a_cancelled_dispatch_withdraws_its_card(lane, owner, monkeypatch):
    from app.core.user_question_handler import user_question_handler as handler

    owner.gate = asyncio.Event()
    retired = []
    monkeypatch.setattr(handler, "_v2_pending_questions", lambda sid: [
        {"id": "row-1", "payload": {"request_id": owner.questions[0]["request_id"]}}])

    async def retire(session_id, interaction_id, request_id):
        retired.append((session_id, interaction_id))

    monkeypatch.setattr(handler, "_retire_unanswered", retire)
    call = asyncio.create_task(_task({"agent": "plain"}))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if owner.questions:
            break
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert retired == [("parent", "row-1")]
    assert owner.frames[-1][1]["type"] == "user_question_timeout"
    assert subagent_default._ASKING is None


async def test_question_ttl_is_shortened_never_lengthened(mock_db, monkeypatch):
    from app.core import user_question_handler as uqh
    from app.db import interactions

    seen = []

    def create(session_id, kind, payload, ttl, **kwargs):
        seen.append(ttl)
        raise RuntimeError("stop here")

    monkeypatch.setattr(interactions, "create_interaction", create)
    monkeypatch.setattr(uqh, "_question_ttl_seconds", lambda: 3600)

    async def broadcast(payload):
        return True

    for ttl_seconds in (900, 7200, None):
        with pytest.raises(RuntimeError):
            await uqh.user_question_handler._request_answers_v2(
                "r", "t", "s", [{"question": "q"}], broadcast, ttl_seconds=ttl_seconds)
    assert seen == [900, 3600, 3600]


def test_ask_owner_once_notifies_a_single_time(mock_db, monkeypatch):
    sent = []

    async def emit(user_id, **kwargs):
        sent.append((user_id, kwargs["category"]))

    monkeypatch.setattr("app.services.notification_service.emit_notification", emit)
    subagent_default.reset_asked()

    async def run():
        first = subagent_default.ask_owner_once("s1")
        second = subagent_default.ask_owner_once("s2")
        await asyncio.sleep(0)
        return first, second

    assert asyncio.run(run()) == (True, False)
    assert sent == [("admin", subagent_default.NOTIFICATION_CATEGORY)]
    # Saving a default re-arms it for a later "cleared again".
    subagent_default.write("sonnet")
    assert subagent_default._asked is False


async def test_file_agent_uses_the_default_on_its_own_leg_else_the_session(lane):
    _, seen, _ = lane
    registry = {"reader": {"name": "reader", "description": "Read", "prompt": "Read"}}
    db = lane[0]
    db.update_session("parent", launched_model="opus")
    # No default: the file agent keeps borrowing the caller's model, as before.
    await _task({"agent": "reader"}, agent_registry=registry)
    subagent_default.write("sonnet")
    await _task({"agent": "reader"}, agent_registry=registry)
    # A default on another leg is not taken: file agents never switch legs.
    subagent_default.write("prov:lane:sol")
    await _task({"agent": "reader"}, agent_registry=registry)
    assert [(s["provider_id"], s["model_id"]) for s in seen] == [
        (None, "opus"), (None, "sonnet"), (None, "opus")]


def test_voice_roster_ignores_the_default(lane):
    db, _, _ = lane
    subagent_default.write("sonnet")
    db.create_profile("voiced", "Voiced", None, {"provider_models": {"__voice__": "haiku"}})
    roster = apm.available_voice_subagents()
    assert [(t.id, t.model) for t in roster] == [("voiced", "haiku")]


# ---------------------------------------------------------------------------
# Task tool contract
# ---------------------------------------------------------------------------

def test_task_model_takes_any_model_and_states_the_user_rule(lane):
    subagent_default.write("sonnet")
    contract = apm.build_task_contract("caller", None)
    prop = contract.input_schema["properties"]["model"]
    assert prop["type"] == "string" and "enum" not in prop
    assert "Omitted, the agent runs on its own pinned model" in prop["description"]
    assert "never runs on this session's model" in prop["description"]
    assert "Only for when the user named a model" in prop["description"]
    assert "The host checks that the user asked" in prop["description"]
    assert "The owner's subagent default: sonnet." in contract.description
    assert "- plain (model: sonnet, the subagent default; runs on Claude)" in contract.description
    assert "- pinned (model: haiku; runs on Claude)" in contract.description


def test_without_a_default_unpinned_agents_are_listed_as_needing_one(lane):
    contract = apm.build_task_contract("caller", None)
    assert "enum" not in contract.input_schema["properties"]["model"]
    assert "plain" in contract.input_schema["properties"]["agent"]["enum"]
    assert "- plain (no model: not pinned and the owner has not chosen" in contract.description
    assert "subagent default:" not in contract.description


def test_an_unrunnable_default_reads_as_none(lane):
    db, _, _ = lane
    subagent_default.write("prov:lane:sol")
    db.update_model_provider("lane", enabled=False)
    by_id = {t.id: t for t in apm.available_subagents("caller", None)}
    assert by_id["plain"].model_source == "unset"
    assert subagent_default.effective().model is None


# ---------------------------------------------------------------------------
# Validation and storage
# ---------------------------------------------------------------------------

def test_write_normalizes_and_validates(lane):
    saved = subagent_default.write("claude-opus-4-6")
    assert saved == subagent_default.SubagentDefault("opus")
    assert subagent_default.read() == saved


def test_a_stored_allowed_list_from_before_1830_is_ignored(lane):
    from app.db import database

    database.set_system_setting(
        subagent_default.SETTING_KEY, '{"model": "sonnet", "allowed": ["haiku"]}')
    assert subagent_default.read() == subagent_default.SubagentDefault("sonnet")
    assert subagent_default.payload() == {"model": "sonnet"}


@pytest.mark.parametrize("model", [
    "gpt-5",                 # a bare non-Claude id
    "prov:lane:unlisted",    # not curated on the provider
    "prov:missing:sol",      # no such provider
    "prov:lane",             # malformed composite
    "claude-3-opus",         # not a Claude model the app offers
])
def test_write_refuses_models_this_install_cannot_run(lane, model):
    subagent_default.write("sonnet")
    with pytest.raises(ValueError):
        subagent_default.write(model)
    assert subagent_default.read().model == "sonnet"


# ---------------------------------------------------------------------------
# Upgrade behaviour
# ---------------------------------------------------------------------------

async def test_upgrade_with_pins_and_no_default_seeds_nothing_and_keeps_working(lane):
    _, seen, asked = lane
    assert subagent_default.read() == subagent_default.SubagentDefault()
    result = await _task({"agent": "pinned"})
    assert result["ok"], result
    assert seen[0]["model_id"] == "haiku"
    assert not asked
    assert subagent_default.read() == subagent_default.SubagentDefault()


# ---------------------------------------------------------------------------
# The model a subagent runs on rides its task.updated frames
# ---------------------------------------------------------------------------

def test_task_updated_frames_carry_the_registry_model(monkeypatch):
    from app.contract.events import TaskUpdated
    from app.core import turn_recorder
    from app.core.task_registry import task_registry

    task_registry.start_task("s-model", "w-model", "agent", "Fix it",
                             model="Claude · sonnet", agent="Plain")
    try:
        assert turn_recorder._registry_worker("s-model", "w-model") == {
            "model": "Claude · sonnet",
            "started_at": task_registry.get_task("s-model", "w-model")["startedAt"],
        }
        task_registry.start_task("s-model", "sh-1", "shell", "npm test")
        assert turn_recorder._registry_worker("s-model", "sh-1") == {
            "started_at": task_registry.get_task("s-model", "sh-1")["startedAt"],
        }
    finally:
        task_registry.clear_session("s-model")
    assert "model" in TaskUpdated.model_fields
