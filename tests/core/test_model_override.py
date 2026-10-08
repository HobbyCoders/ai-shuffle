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

def _turn(session_id, source, prompt, *, steered=False, payload=None, profile="caller", command_id=None):
    turn = turns.enqueue_turn(session_id, source, prompt=prompt, profile_id=profile, payload=payload,
                              command_id=command_id)
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
    monkeypatch.setattr(mo, "_APPROVED", set())
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


@pytest.mark.parametrize("wording", ["gpt 6 astra", "GPT-6-ASTRA", "gPt6AsTrA"])
async def test_spoken_model_task_dispatch(world, wording):
    _, seen, prompts, _ = world
    _turn("parent", "user", f"use {wording} for the worker")
    result = await _task({"agent": "pinned", "model": wording})
    assert result["ok"], result
    assert result["model"] == "astra" and result["provider"]["id"] == "lane"
    assert result["model_override"]["source"] == mo.NAMED
    assert len(seen) == 1 and seen[0]["model_id"] == "astra" and prompts == []


def test_spoken_model_id_resolves_to_registry_spelling(world):
    world[0].create_model_provider("chatgpt", "ChatGPT", "openai", "https://example.invalid/v1",
                                   models=[{"id": "gpt-6.1-sol", "display_name": "Bright Sol"}])
    for wording in ("GPT 6.1 SOL", "gpt6.1sol", "BRIGHT-SOL"):
        assert mo.resolve(wording, "lane") == mo.Choice("prov:chatgpt:gpt-6.1-sol", "chatgpt", "gpt-6.1-sol")


@pytest.mark.parametrize("wording, expected", [("opus", "opus"), ("OPUS", "opus"),
                                              ("Claude Opus", "opus"), ("claude-opus-4-6", "opus"),
                                              ("prov:lane:astra", "prov:lane:astra")])
@pytest.mark.parametrize("remote", [False, True])
def test_spoken_resolution_preserves_claude_and_exact_composites(world, wording, expected, remote):
    assert mo.resolve(wording, "lane", runs_here=not remote).model == expected


def test_spoken_resolution_never_uses_a_partial_or_fuzzy_name(world):
    for wording in ("gpt 6 astr", "use gpt 6 astra", "some Astra model"):
        with pytest.raises(ValueError, match="No matching model id or display name"):
            mo.resolve(wording)


def test_spoken_unavailable_choices_are_bounded_and_exclude_disabled_and_missing_credentials(world, monkeypatch):
    db = world[0]
    db.update_model_provider("lane", enabled=False)
    monkeypatch.setattr(td, "_credential_gap", lambda pid: "No credential" if pid == "grok" else None)
    db.create_model_provider("many", "Many", "openai", "https://example.invalid/v1",
                             models=[{"id": f"available-{n}"} for n in range(30)])
    with pytest.raises(ValueError) as failure:
        mo.resolve("invented model")
    text = str(failure.value)
    choices = text.split("Choose one of: ")[1].split(" (at most 20 shown)")[0].split(", ")
    assert len(choices) == 20
    assert all(not model.startswith(("prov:lane:", "prov:grok:")) for model in choices)
    assert set(choices) <= set(mo._catalog())  # Real, untruncated IDs, not guessed suggestions.


@pytest.mark.parametrize("explicit", [False, True])
async def test_spoken_model_ambiguity_refuses_before_dispatch(world, explicit):
    db, seen, prompts, _ = world
    if explicit:
        db.update_model_provider("lane", models_json=[{"id": "a", "label": "Shared Model"},
                                                {"id": "b", "name": "Shared-Model"}])
        wording = "prov:lane:SHARED MODEL"
    else:
        db.create_model_provider("other", "Other", "openai", "https://example.invalid/v1",
                                 models=[{"id": "astra", "label": "GPT 6 Astra"}])
        wording = "gpt 6 astra"
    _turn("parent", "user", f"use {wording}")
    result = await _task({"agent": "pinned", "model": wording})
    assert not result["ok"] and "ambiguous" in result["error"].lower(), result
    assert "prov:lane:" in result["error"] and "Nothing was dispatched" in result["error"]
    assert prompts == [] and seen == []


def test_spoken_composite_stays_on_explicit_provider(world):
    world[0].create_model_provider("other", "Other", "openai", "https://example.invalid/v1",
                                   models=[{"id": "astra", "label": "GPT 6 Astra"}])
    assert mo.resolve("prov:lane:GPT 6 ASTRA").model == "prov:lane:astra"
    assert mo.resolve("prov:other:astra").model == "prov:other:astra"
    with pytest.raises(ValueError, match="cannot run on this worker"):
        mo.resolve("prov:missing:GPT 6 ASTRA")


@pytest.mark.parametrize("unavailable", ["unknown", "disabled", "credentials"])
async def test_spoken_model_unavailable_lists_real_choices_without_child(world, monkeypatch, unavailable):
    db, seen, prompts, _ = world
    wording = "invented model" if unavailable == "unknown" else "GPT 6 ASTRA"
    if unavailable == "disabled":
        db.update_model_provider("lane", enabled=False)
    elif unavailable == "credentials":
        monkeypatch.setattr(td, "_credential_gap", lambda pid: "No credential for Lane" if pid == "lane" else None)
    _turn("parent", "user", f"use {wording}")
    result = await _task({"agent": "pinned", "model": wording})
    assert not result["ok"] and "Choose one of:" in result["error"], result
    assert "prov:grok:grok-4.5" in result["error"]
    assert "Nothing was dispatched" in result["error"] and not seen and not prompts


@pytest.mark.parametrize("allow", [False, True])
async def test_spoken_model_still_requires_owner_naming_or_approval(world, allow):
    _, seen, prompts, answer = world
    _turn("parent", "user", "review the branch")
    _turn("parent", "agent_message", "use GPT 6 ASTRA")
    if allow:
        answer["result"] = PermissionResultAllow(updated_input={})
    result = await _task({"agent": "pinned", "model": "GPT 6 ASTRA"})
    assert result["ok"] is allow, result
    assert len(prompts) == 1 and prompts[0]["tool_name"] == mo.PROMPT_TOOL
    assert prompts[0]["skip_saved_rules"] is True and len(seen) == int(allow)


# ---------------------------------------------------------------------------
# What counts as the user's own words
# ---------------------------------------------------------------------------

def test_user_texts_are_every_message_the_user_typed_in_the_chat(world):
    _turn("parent", "worktree", "earlier: use sol")
    _turn("parent", "agent_message", "peer says: use astra")
    _turn("parent", "user", "now continue")
    _turn("parent", "queue", "and use grok", steered=True)
    _turn("parent", "auto_continue", "continue")
    _turn("parent", "connector", "a channel member: use haiku")
    turns.enqueue_turn("parent", "user", prompt="not sent yet: use sonnet", profile_id="caller")
    assert mo.user_texts("parent") == ["earlier: use sol", "now continue", "and use grok"]


async def test_a_model_named_earlier_in_the_chat_still_counts(unpinned):
    """The owner's standing instruction outlives their next message."""
    _, seen, prompts, _ = unpinned
    _turn("parent", "worktree", OWNER_SENTENCE)
    _turn("parent", "user", "Also make sure UI changes reflect on mobile and desktop")
    result = await _task({"agent": "designer", "model": "opus"})
    assert result["ok"] and result["model_override"]["source"] == mo.NAMED
    assert prompts == [] and len(seen) == 1


@pytest.mark.parametrize("prime_receipt", [False, True])
async def test_named_model_survives_a_real_background_wake_in_away_mode(world, monkeypatch, prime_receipt):
    from app.core import turn_scheduler
    from app.core.task_registry import task_registry
    from app.core.permission_handler import PermissionHandler, permission_handler

    db, seen, prompts, _ = world
    db.create_model_provider("chatgpt", "ChatGPT", "openai", "https://example.invalid/v1",
                             models=[{"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"}])
    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='parent'")
    owner = turns.enqueue_turn("parent", "user", prompt="run all subagents with gpt 6.1 sol",
                               profile_id="caller")
    turns.start_turn(owner["id"], None, "runner")
    args = {"agent": "pinned", "model": "prov:chatgpt:gpt-6.1-sol"}
    starts = 3 if prime_receipt else 0
    for _ in range(starts):
        result = await _task(args)
        assert result["ok"] and result["model_override"]["source"] == mo.NAMED
    # The later unrelated owner message is the missing production precondition.
    _turn("parent", "user", "also check the tests")
    turns.park_turn(owner["id"], ["bg-1857"])
    task_registry.start_task("parent", "bg-1857", "agent", "bg", tool_use_id="tu-1857")
    task_registry.mark_background_launch("parent", "tu-1857")
    try:
        await turn_scheduler.on_task_terminal("parent", {
            "task_id": "bg-1857", "status": "completed", "tool_use_id": "tu-1857",
            "summary": "background work finished", "output_file": "", "usage": {},
        })
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM turns WHERE session_id='parent' AND source='unpark'")
            wake = cursor.fetchone()
        assert wake and "<task-notification>" in wake["prompt"]
        turns.start_turn(wake["id"], None, "runner")
        again = await _task(args)
        assert again["ok"] and again["model_override"]["source"] == mo.NAMED
        assert len(seen) == starts + 1 and prompts == []
    finally:
        task_registry.clear_session("parent")


async def test_named_receipt_records_own_message_and_skips_history_after_restart(world, monkeypatch):
    from app.db import interactions

    turn = _turn("parent", "user", "use Astra")
    assert await mo.confirm("parent", "caller", model="prov:lane:astra", baseline="opus", action="start") == mo.NAMED
    receipt = interactions.get_interaction(mo._named_id("parent", "prov:lane:astra"))
    assert receipt["payload"] == {"model": "prov:lane:astra", "source": "own_message",
                                  "turn_id": turn["id"], "session_id": "parent", "api_user_id": None}
    mo._APPROVED.clear()
    monkeypatch.setattr(mo, "_user_evidence", lambda sid: pytest.fail("receipt must skip history"))
    assert await mo.confirm("parent", "caller", model="prov:lane:astra", baseline="opus", action="start") == mo.NAMED
    assert not mo._has_named("parent", "prov:lane:sol")
    assert not mo._has_named("other", "prov:lane:astra")


async def test_api_naming_receipt_keeps_its_sending_principal(world, monkeypatch):
    from app.db import interactions

    db = world[0]
    db.create_api_user("key-a", "Key A", "hash")
    db.create_api_user("key-b", "Key B", "hash")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id='key-a' WHERE id='parent'")
    _turn("parent", "api", "use Astra", payload={"api_user_id": "key-a"})
    assert await mo.confirm("parent", "caller", model="prov:lane:astra", baseline="opus", action="start") == mo.NAMED
    receipt = interactions.get_interaction(mo._named_id("parent", "prov:lane:astra"))
    assert receipt["payload"]["api_user_id"] == "key-a"
    assert mo._has_named("parent", "prov:lane:astra")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id='key-b' WHERE id='parent'")
    assert not mo._has_named("parent", "prov:lane:astra")
    assert not mo.user_texts("parent")


@pytest.mark.parametrize("restart", [False, True])
async def test_saved_approval_does_not_cross_principals(world, restart):
    db, _, _, answer = world
    answer["result"] = PermissionResultAllow(updated_input={})
    assert await mo.confirm("parent", "caller", model="prov:lane:astra", baseline="opus", action="start") == mo.APPROVED
    db.create_api_user("new-key", "New key", "hash")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id='new-key' WHERE id='parent'")
    if restart:
        mo._APPROVED.clear()
    answer["result"] = PermissionResultDeny(message="Denied")
    with pytest.raises(PermissionError):
        await mo.confirm("parent", "caller", model="prov:lane:astra", baseline="opus", action="start")


async def test_saved_and_in_memory_approval_are_checked_before_history(world, monkeypatch):
    assert mo._remember_approval("parent", "prov:lane:astra")
    monkeypatch.setattr(mo, "_user_evidence", lambda sid: pytest.fail("approval must skip history"))
    for _ in range(2):
        assert await mo.confirm("parent", "caller", model="prov:lane:astra", baseline="opus", action="start") == mo.APPROVED


@pytest.mark.parametrize("padding", ["rows", "characters"])
async def test_first_use_after_upgrade_finds_old_naming(world, monkeypatch, padding):
    from app.core.permission_handler import PermissionHandler, permission_handler

    db, seen, prompts, _ = world
    db.create_model_provider("chatgpt", "ChatGPT", "openai", "https://example.invalid/v1",
                             models=[{"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"}])
    _turn("parent", "user", "all subagents are run with gpt 6.1 sol")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE sessions SET away_mode=TRUE WHERE id='parent'")
        for position in range(2, 515 if padding == "rows" else 3):
            cursor.execute("INSERT INTO turns (id, session_id, source, prompt, position, state) "
                           "VALUES (%s, 'parent', 'user', %s, %s, 'completed')",
                           (f"old-{position}", "continue" if padding == "rows" else "x" * 262144, position))
    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))
    result = await _task({"agent": "pinned", "model": "prov:chatgpt:gpt-6.1-sol"})
    assert result["ok"], result
    assert result["model_override"]["source"] == mo.NAMED
    assert len(seen) == 1 and prompts == []
    assert mo._has_named("parent", "prov:chatgpt:gpt-6.1-sol")


@pytest.mark.parametrize("too_old", [False, True])
def test_history_row_budget_newest_first(world, too_old):
    _turn("parent", "user", "use Astra")
    # Bulk fixture setup keeps the test fast while exercising the real SQL window.
    with get_db() as conn:
        cursor = conn.cursor()
        for position in range(2, 513 + int(too_old)):
            cursor.execute("INSERT INTO turns (id, session_id, source, prompt, position, state) "
                           "VALUES (%s, 'parent', 'user', 'continue', %s, 'completed')",
                           (f"budget-{position}", position))
    texts = mo.user_texts("parent")
    assert len(texts) == 512
    assert mo.named_by_user("prov:lane:astra", texts) == (not too_old)


def test_history_character_budget_includes_payloads(world):
    _turn("parent", "user", "use Astra")
    _turn("parent", "user", "x" * 262144, payload={"padding": "x"})
    assert mo.user_texts("parent") == []


async def test_approval_survives_lost_process_memory_and_away_mode(world, monkeypatch):
    from app.core import interaction_wait
    from app.core.permission_handler import PermissionHandler, permission_handler
    from app.db import interactions

    _, seen, prompts, _ = world
    _turn("parent", "user", "review the branch")
    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))

    async def owner_answers(interaction_id, session_id, timeout):
        row = interactions.get_interaction(interaction_id)
        result = await permission_handler.respond(row["payload"]["request_id"], session_id, "allow")
        assert result["resolved"]
        return interactions.get_interaction(interaction_id)

    monkeypatch.setattr(interaction_wait, "await_interaction", owner_answers)
    monkeypatch.setattr("app.services.push_notification.fire_and_forget_notify", lambda **kw: None)
    first = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert first["ok"] and first["model_override"]["source"] == mo.APPROVED
    receipt = interactions.get_interaction(mo._approval_id("parent", "prov:lane:astra"))
    assert receipt["state"] == "answered" and receipt["expires_at"] is None
    assert interactions.get_pending_interactions("parent") == []
    mo._APPROVED.clear()  # Simulate a server restart, retaining only database state.
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='parent'")
    _turn("parent", "unpark", "background work finished")
    again = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert again["ok"], again
    assert again["model_override"]["source"] == mo.APPROVED
    assert len(seen) == 2 and prompts == []
    # The durable approval is model- and chat-scoped, not a broad tool rule.
    other_model = await _task({"agent": "pinned", "model": "prov:lane:sol"})
    assert not other_model["ok"] and "no approval prompt was shown" in other_model["error"]
    sessions.create_session("other", "caller", title="Other")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='other'")
    with pytest.raises(PermissionError, match="owner is away"):
        await mo.confirm("other", "caller", model="prov:lane:astra", baseline="opus", action="start x")
    sessions.delete_session("parent")
    mo._APPROVED.clear()
    assert not mo._has_approval("parent", "prov:lane:astra")


@pytest.mark.parametrize("remember", ["profile", "session"])
async def test_remembered_answer_approves_only_its_own_model(world, monkeypatch, remember):
    """An older client's "Always" on one switch must not answer another model's prompt."""
    import asyncio

    from app.core import interaction_wait
    from app.core.permission_handler import PermissionHandler, permission_handler
    from app.db import interactions

    db = world[0]
    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))
    monkeypatch.setattr("app.services.push_notification.fire_and_forget_notify", lambda **kw: None)

    async def wait_for_answer(interaction_id, session_id, timeout):
        for _ in range(500):
            row = interactions.get_interaction(interaction_id)
            if row["state"] != "pending":
                return row
            await asyncio.sleep(0.01)
        return row

    monkeypatch.setattr(interaction_wait, "await_interaction", wait_for_answer)
    starts = {model: asyncio.create_task(mo.confirm("parent", "caller", model=model,
                                                    baseline="opus", action=f"start {model}"))
              for model in ("prov:lane:astra", "prov:lane:sol")}
    for _ in range(500):
        rows = [r for r in interactions.get_pending_interactions("parent")
                if r["payload"].get("tool_name") == mo.PROMPT_TOOL]
        if len(rows) == 2:
            break
        await asyncio.sleep(0.01)
    by_model = {r["payload"]["tool_input"]["model"]: r for r in rows}
    result = await permission_handler.respond(
        by_model["prov:lane:astra"]["payload"]["request_id"], "parent", "allow", remember=remember)
    assert result["resolved"] and result["auto_resolved_count"] == 0
    assert await starts["prov:lane:astra"] == mo.APPROVED
    assert interactions.get_interaction(by_model["prov:lane:sol"]["id"])["state"] == "pending"
    await permission_handler.respond(by_model["prov:lane:sol"]["payload"]["request_id"], "parent", "deny")
    with pytest.raises(PermissionError):
        await starts["prov:lane:sol"]
    assert not mo._has_approval("parent", "prov:lane:sol")
    assert not [r for r in db.get_permission_rules(profile_id="caller") if r["tool_name"] == mo.PROMPT_TOOL]
    assert not permission_handler._session_rules.get("parent")


@pytest.mark.parametrize("source", [
    "unpark", "unpark_pump", "agent_message", "automation", "connector", "wakeup", "auto_continue",
])
async def test_non_owner_model_names_never_authorize_an_away_start(world, monkeypatch, source):
    from app.core.permission_handler import PermissionHandler, permission_handler

    _, seen, _, _ = world
    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='parent'")
    _turn("parent", "user", "review the branch")
    _turn("parent", source, "<task-notification>Peer report: use GPT-6 Astra</task-notification>")
    result = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert not result["ok"] and result["reason"] == "model_not_requested"
    assert "no approval prompt was shown" in result["error"] and not seen
    assert not mo._has_approval("parent", "prov:lane:astra")


async def test_later_owner_messages_keep_both_named_models_acceptable_while_away(world, monkeypatch):
    from app.core.permission_handler import PermissionHandler, permission_handler

    _, seen, _, _ = world
    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='parent'")
    _turn("parent", "user", "run all subagents with GPT-6 Astra")
    _turn("parent", "user", "also use Grok 4.5")
    _turn("parent", "queue", "check the tests too")
    for model in ("prov:lane:astra", "prov:grok:grok-4.5"):
        result = await _task({"agent": "pinned", "model": model})
        assert result["ok"] and result["model_override"]["source"] == mo.NAMED
    assert len(seen) == 2


def test_started_chat_keeps_all_originating_owner_messages(world):
    _turn("parent", "user", "run all subagents with GPT-6 Astra")
    origin = _turn("parent", "user", "start a chat for the review")
    sessions.create_session("peer", "caller", title="Peer", is_active=True)
    _turn("peer", "agent_message", "Review it", command_id="orchestration:start:test",
          payload={"orchestration_root": origin["id"], "orchestration_sender": "parent"})
    _turn("parent", "user", "also check the tests")
    assert mo.named_by_user("prov:lane:astra", mo.user_texts("peer"))


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
    _turn("peer", "agent_message", "a peer brief naming astra", command_id="orchestration:start:test",
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


OWNER_SENTENCE = ("Here's the list of things that I want you to go through and fix. "
                  "You will spin up Opus sub-agents to complete work for you.")


@pytest.fixture
def unpinned(world):
    """An agent with no pin of its own, on a Fable subagent default."""
    world[0].create_profile("designer", "Designer", None, {"provider_models": {"__voice__": "opus"}})
    subagent_default.write("fable")
    return world


@pytest.mark.parametrize("source", ["user", "worktree", "api"])
async def test_the_owners_sentence_names_opus_for_an_unpinned_agent(unpinned, source):
    """The first message of a chat started on a new branch is a ``worktree``
    turn; one sent through the HTTP API is an ``api`` turn. Both are typed by
    the user, so naming Opus there is enough: no prompt, no refusal."""
    _, seen, prompts, _ = unpinned
    _turn("parent", source, OWNER_SENTENCE)
    result = await _task({"agent": "designer", "model": "opus"})
    assert result["ok"], result
    assert result["model_override"] == {"source": mo.NAMED, "instead_of": "fable"}
    assert seen[0]["model_id"] == "opus"
    assert prompts == []


@pytest.mark.parametrize("text", [
    OWNER_SENTENCE,
    "use opus",
    "run them on Opus 4",
    "OPUS sub-agents please",
    "spin up opus-powered agents",
    "Claude Opus for the reviewers",
    "claude-opus-5",
])
def test_ordinary_phrasings_of_opus_count_as_named(unpinned, text):
    assert mo.named_by_user("opus", [text])


@pytest.mark.parametrize("text", [
    "You will spin up sub-agents to complete work for you.",  # no model at all
    "You will spin up Sonnet sub-agents to complete work for you.",  # another model
    "write it as an opusculum",  # not the whole word
])
async def test_a_branch_chat_that_does_not_name_opus_still_asks(unpinned, text):
    _, seen, prompts, _ = unpinned
    _turn("parent", "worktree", text)
    result = await _task({"agent": "designer", "model": "opus"})
    assert not result["ok"] and result["reason"] == "model_not_requested"
    assert len(prompts) == 1 and not seen


async def test_a_connector_message_naming_the_model_does_not_count(unpinned):
    """A connector channel can carry other people's messages."""
    _, seen, prompts, _ = unpinned
    _turn("parent", "connector", "use Opus sub-agents")
    result = await _task({"agent": "designer", "model": "opus"})
    assert not result["ok"] and len(prompts) == 1 and not seen


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


async def test_one_approval_covers_that_model_for_the_rest_of_the_chat(unpinned):
    db, seen, prompts, answer = unpinned
    _turn("parent", "worktree", "You will spin up sub-agents to complete work for you.")
    answer["result"] = PermissionResultAllow(updated_input={})
    first = await _task({"agent": "designer", "model": "opus"})
    assert first["ok"] and first["model_override"]["source"] == mo.APPROVED
    assert len(prompts) == 1
    # The same switch again, for another agent and in a later message: no prompt.
    answer["result"] = PermissionResultDeny(message="User denied permission")
    _turn("parent", "user", "and one more")
    again = await _task({"agent": "designer", "model": "opus"})
    assert again["ok"] and again["model_override"] == {"source": mo.APPROVED, "instead_of": "fable"}
    db.create_profile("writer", "Writer", None, {})
    other_agent = await _task({"agent": "writer", "model": "opus"})
    assert other_agent["ok"], other_agent
    assert len(prompts) == 1 and len(seen) == 3
    # Another model is a new question, and so is another chat.
    assert not (await _task({"agent": "designer", "model": "haiku"}))["ok"]
    assert len(prompts) == 2
    sessions.create_session("other", "caller", title="Other")
    _turn("other", "user", "go")
    elsewhere = await td.dispatch_task_tool(
        {"prompt": "go", "mode": "sync", "agent": "designer", "model": "opus"},
        caller_session_id="other", caller_provider_id=None)
    assert not elsewhere["ok"] and len(prompts) == 3


async def test_a_task_worker_shares_its_chats_approval(unpinned):
    _, _, prompts, answer = unpinned
    _turn("parent", "user", "review the branch")
    answer["result"] = PermissionResultAllow(updated_input={})
    assert (await _task({"agent": "designer", "model": "opus"}))["ok"]
    from app.core.provider_agent import WORKER_TURN_SOURCE

    sessions.create_session("child", "caller", title="Child", parent_session_id="parent",
                            is_worker=True, hidden=True)
    _turn("child", WORKER_TURN_SOURCE, "Review it")
    answer["result"] = PermissionResultDeny(message="User denied permission")
    mo._APPROVED.clear()  # A worker also recovers its top-level chat's durable approval.
    assert await mo.confirm("child", "caller", model="opus", baseline="fable", action="start x") == mo.APPROVED
    assert len(prompts) == 1


async def test_an_approval_that_cannot_be_saved_fails_closed(world, monkeypatch):
    _, seen, prompts, answer = world
    _turn("parent", "user", "review the branch")
    answer["result"] = PermissionResultAllow(updated_input={})
    monkeypatch.setattr(mo, "_remember_approval", lambda *args: False)
    result = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert not result["ok"] and "approval could not be saved" in result["error"]
    assert not seen and len(prompts) == 1
    assert ("parent", "prov:lane:astra") not in mo._APPROVED


async def test_a_denial_is_not_remembered(unpinned):
    _, _, prompts, answer = unpinned
    _turn("parent", "user", "review the branch")
    assert not (await _task({"agent": "designer", "model": "opus"}))["ok"]
    answer["result"] = PermissionResultAllow(updated_input={})
    assert (await _task({"agent": "designer", "model": "opus"}))["ok"]
    assert len(prompts) == 2


@pytest.mark.parametrize("allow", [True, False])
async def test_parallel_calls_needing_the_same_switch_share_one_prompt(unpinned, monkeypatch, allow):
    """Five Task calls in one parallel block raise one prompt, not five."""
    import asyncio
    from app.core.permission_handler import permission_handler

    _, seen, _, _ = unpinned
    _turn("parent", "user", "review the branch")
    asked, release = [], asyncio.Event()

    async def request_permission(**kwargs):
        asked.append(kwargs)
        await release.wait()
        return (PermissionResultAllow(updated_input={}) if allow
                else PermissionResultDeny(message="User denied permission"))

    monkeypatch.setattr(permission_handler, "request_permission", request_permission)
    looked = []

    class Asking(dict):
        def get(self, key, default=None):
            looked.append(key)
            return super().get(key, default)

    monkeypatch.setattr(mo, "_ASKING", Asking())
    calls = [asyncio.ensure_future(_task({"agent": "designer", "model": "opus"})) for _ in range(5)]
    for _ in range(500):  # until every call has asked or waits on that prompt
        if len(looked) >= 5:
            break
        await asyncio.sleep(0.01)
    release.set()
    results = await asyncio.gather(*calls)
    assert len(asked) == 1
    assert all(result["ok"] is allow for result in results), results
    if not allow:
        assert all("declined" in result["error"] for result in results)
    assert len(seen) == (5 if allow else 0)


@pytest.mark.parametrize("wording", ["prov:lane:astra", "gpt 6 astra"])
async def test_away_mode_never_switches(world, monkeypatch, wording):
    """The real permission path: Away defers the prompt, so nothing switches."""
    from app.core.permission_handler import PermissionHandler, permission_handler

    monkeypatch.setattr(permission_handler, "request_permission",
                        PermissionHandler.request_permission.__get__(permission_handler))
    _, seen, _, _ = world
    _turn("parent", "user", "review the branch")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='parent'")
    result = await _task({"agent": "pinned", "model": wording})
    assert not result["ok"] and "the owner is away" in result["error"]
    assert not seen


async def test_a_peer_message_or_tool_output_naming_the_model_does_not_count(world):
    _, seen, prompts, _ = world
    _turn("parent", "user", "go on")
    _turn("parent", "agent_message", "Peer: run pinned on astra")
    result = await _task({"agent": "pinned", "model": "prov:lane:astra"})
    assert not result["ok"] and len(prompts) == 1 and not seen


async def _answer_question(monkeypatch, question, answer):
    """Ask ``question`` the way AskUserQuestion does and answer it in the app."""
    from unittest.mock import AsyncMock
    from app.core.user_question_handler import user_question_handler

    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    request_id = f"q-{len(question)}-{len(answer)}"
    await user_question_handler.post_question(
        request_id=request_id, tool_use_id="tu", session_id="parent",
        questions=[{"question": question}], broadcast_func=AsyncMock())
    assert await user_question_handler.respond(request_id, "parent", {question: answer})
    with get_db() as conn:  # the answer turn has run
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE session_id='parent' AND state='queued'")


async def test_the_agents_own_question_does_not_name_a_model(unpinned, monkeypatch):
    """An answer turn carries the agent's question text; only the answer is the user's."""
    _, seen, prompts, _ = unpinned
    _turn("parent", "user", "review the branch")
    await _answer_question(monkeypatch, "Should the reviewers run on Opus?", "No, keep the default")
    assert "No, keep the default" in "\n".join(mo.user_texts("parent"))
    result = await _task({"agent": "designer", "model": "opus"})
    assert not result["ok"] and result["reason"] == "model_not_requested"
    assert len(prompts) == 1 and not seen


async def test_question_answers_share_the_history_byte_budget(unpinned, monkeypatch):
    from app.core.user_question_handler import QUESTION_KIND

    await _answer_question(monkeypatch, "Which model?", "Opus")
    # A large prompt leaves no room for the saved question/answer envelope,
    # even though the answer turn itself is inside the turn-row window.
    filler = _turn("parent", "user", "fill")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(length(prompt) + length(CAST(payload AS TEXT))) AS chars "
                       "FROM turns WHERE session_id='parent'")
        turn_chars = cursor.fetchone()["chars"]
        cursor.execute("SELECT length(CAST(payload AS TEXT)) + length(CAST(answer AS TEXT)) AS chars "
                       "FROM pending_interactions WHERE session_id='parent' AND kind=%s", (QUESTION_KIND,))
        envelope_chars = cursor.fetchone()["chars"]
        filler_size = 262144 - turn_chars + len("fill") - envelope_chars + 1
        cursor.execute("UPDATE turns SET prompt=%s WHERE id=%s", ("x" * filler_size, filler["id"]))
    assert not mo.named_by_user("opus", mo.user_texts("parent"))


async def test_a_model_the_user_gave_as_an_answer_counts(unpinned, monkeypatch):
    _, seen, prompts, _ = unpinned
    _turn("parent", "user", "review the branch")
    await _answer_question(monkeypatch, "Which model should the reviewers use?", "Opus")
    result = await _task({"agent": "designer", "model": "opus"})
    assert result["ok"] and result["model_override"]["source"] == mo.NAMED
    assert prompts == [] and len(seen) == 1


async def test_a_message_a_voice_model_handed_to_the_chat_does_not_count(unpinned):
    """A voice call's hand-off is written by the voice model (or wraps the
    whole call transcript), so it is not the user's own words."""
    _, seen, prompts, _ = unpinned
    _turn("parent", "user", "Use Opus sub-agents for the review", payload={"voice_delegation": True})
    result = await _task({"agent": "designer", "model": "opus"})
    assert not result["ok"] and len(prompts) == 1 and not seen


@pytest.mark.parametrize("effort,expected", [(None, "max"), ("low", "low"), ("ultra", "ultra")])
async def test_task_reads_child_model_preference_after_spoken_model_resolution(world, monkeypatch, effort, expected):
    from app.core import preference_identity
    db, seen, _, _ = world
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    db.set_user_preference("account", "owner", "composer.modelEffort.astra", "max")
    _turn("parent", "user", "use gpt 6 astra")
    args = {"agent": "pinned", "model": "gpt 6 astra"}
    if effort is not None:
        args["effort"] = effort
    result = await _task(args, caller_effort="low")
    assert result["ok"], result
    assert seen[0]["inherited_effort"] == expected


async def test_inherited_effort_is_clamped_when_the_new_provider_lacks_it(world):
    _, seen, _, _ = world
    _turn("parent", "user", "use grok 4.5")
    result = await _task({"agent": "pinned", "model": "prov:grok:grok-4.5"}, caller_effort="xhigh")
    assert result["ok"], result
    assert seen[0]["provider_id"] == "grok" and seen[0]["inherited_effort"] == "high"


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


@pytest.mark.parametrize("wording, expected", [("gpt 6 astra", "completed"),
                                               ("not in library", "failed")])
@pytest.mark.parametrize("effort,chosen", [(None, "max"), ("low", "low"), ("ultra", "ultra")])
async def test_spoken_model_through_real_swarm(world, monkeypatch, wording, expected, effort, chosen):
    from unittest.mock import AsyncMock
    from app.core import swarm, provider_agent, turn_scheduler
    from app.core.task_registry import task_registry

    from app.core import preference_identity
    db, seen, prompts, _ = world
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    db.set_user_preference("account", "owner", "composer.modelEffort.astra", "max")
    for name in ("coordinators", "workers", "worker_jobs", "_locks"):
        monkeypatch.setattr(swarm, name, {})
    monkeypatch.setattr(swarm, "persist", AsyncMock())
    monkeypatch.setattr(turn_scheduler, "on_task_terminal", AsyncMock())
    monkeypatch.setattr(td, "_await_worker_registration", AsyncMock(return_value=True))

    async def worker(**kwargs):
        seen.append(kwargs)
        wid = kwargs["worker_session_id"]
        await provider_agent._notify_caller_terminal("parent", wid, wid, "completed", "done")
        return WorkerOutcome(ok=True, session_id=wid, report="done")

    monkeypatch.setattr(provider_agent, "dispatch_worker", worker)
    _turn("parent", "user", f"use {wording} for the swarm")
    ctx = ToolCtx(extra={"task_dispatch": td.make_fluid_dispatcher("parent", "caller", None)})
    try:
        outcome = await swarm.make_handler(lambda: "parent")({"action": "start", "tasks": [
            {"id": "review", "agent": "pinned", "prompt": "go", "model": wording,
             **({"effort": effort} if effort is not None else {})}]}, ctx)
        assert not outcome.is_error, outcome.text
        coordinator = swarm.coordinators["parent"]
        key = outcome.background_task.task_id
        await coordinator.jobs[key]
        snapshot = coordinator.snapshot(key)
        assert snapshot["status"] == expected, snapshot
        assert len(seen) == int(expected == "completed") and not prompts
        if seen:
            assert (seen[0]["provider_id"], seen[0]["model_id"]) == ("lane", "astra")
            assert seen[0]["inherited_effort"] == chosen
        else:
            assert "Nothing was dispatched" in snapshot["tasks"][0]["error"]
    finally:
        task_registry.clear_session("parent")


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


@pytest.mark.parametrize("wording", ["gpt 6 astra", "prov:lane:GPT-6-ASTRA"])
async def test_spoken_model_startsession_queues_canonical_binding(chats, wording):
    _turn("caller", "user", f"start it on {wording}", profile="agent")
    outcome = await _start({**ARGS, "model": wording})
    assert not outcome.is_error, outcome.text
    result = json.loads(outcome.text)
    child = sessions.get_session(result["session_id"])
    assert (child["provider_id"], child["model"]) == ("lane", "prov:lane:astra")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM turns WHERE session_id=%s", (child["id"],))
        queued = turns._row(cursor.fetchone())
    assert queued["overrides"]["model"] == "prov:lane:astra"
    assert queued["payload"]["model_override"]["source"] == mo.NAMED
    assert chats[2] == []


@pytest.mark.parametrize("model,words,key", [(None, "start a chat", "sonnet"),
                                             ("gpt 6 astra", "start it on gpt 6 astra", "astra")])
@pytest.mark.parametrize("effort,chosen", [(None, "low"), ("medium", "medium"), ("ultra", "max")])
async def test_startsession_reads_model_preference_for_inherited_and_spoken_models(chats, monkeypatch, model, words, key, effort, chosen):
    from app.core import preference_identity
    db = chats[0]
    monkeypatch.setattr(preference_identity, "principal_preference_identity", lambda p: ("account", "owner"))
    db.set_user_preference("account", "owner", "composer.modelEffort." + key, "low")
    _turn("caller", "user", words, profile="agent")
    outcome = await _start({**ARGS, **({"model": model} if model else {}),
                           **({"effort": effort} if effort is not None else {})})
    assert not outcome.is_error, outcome.text
    # Astra advertises Ultra; native Sonnet fits it to Max.
    if effort == "ultra" and key == "astra":
        chosen = "ultra"
    row = sessions.get_session(json.loads(outcome.text)["session_id"])
    assert row["effort"] == chosen
    assert orch._start_turn(row["id"])["overrides"]["effort"] == chosen


async def test_spoken_model_startsession_ambiguity_creates_no_chat(chats):
    chats[0].create_model_provider("other", "Other", "openai", "https://example.invalid/v1",
                                   models=[{"id": "different", "label": "GPT 6 Astra"}])
    _turn("caller", "user", "start it on gpt 6 astra", profile="agent")
    before = len(sessions.get_sessions(limit=100))
    outcome = await _start({**ARGS, "model": "gpt 6 astra"})
    assert outcome.is_error and "ambiguous" in outcome.text and "Nothing was started" in outcome.text
    assert len(sessions.get_sessions(limit=100)) == before and not chats[2]


async def test_real_started_chat_inherits_saved_naming_after_history_retention(chats):
    _turn("caller", "user", "use Astra for all subagents", profile="agent")
    assert await mo.confirm("caller", "agent", model="prov:lane:astra", baseline="sonnet", action="start") == mo.NAMED
    outcome = await _start(ARGS)
    assert not outcome.is_error, outcome.text
    child = json.loads(outcome.text)["session_id"]
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM turns WHERE session_id='caller' AND source='user'")
    assert await mo.confirm(child, "agent", model="prov:lane:astra", baseline="sonnet", action="start") == mo.NAMED
    assert mo._has_named(child, "prov:lane:astra")
    assert not mo._has_named(child, "prov:other:astra")


async def test_receiving_a_real_peer_message_never_inherits_model_consent(chats):
    from app.core.permission_handler import PermissionHandler, permission_handler
    from unittest.mock import patch

    _turn("caller", "user", "use Astra for subagents", profile="agent")
    sessions.create_session("independent", "agent", title="Independent", is_active=True)
    _turn("independent", "user", "check the tests", profile="agent")
    outcome = await orch.make_handler(lambda: "caller")("message", {
        "session_id": "independent", "message": "please review", "request_id": "peer-review"
    }, ToolCtx())
    assert not outcome.is_error, outcome.text
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE session_id='independent'")
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id='independent'")
    with patch.object(permission_handler, "request_permission",
                      PermissionHandler.request_permission.__get__(permission_handler)):
        result = await td.dispatch_task_tool(
            {"prompt": "review", "mode": "sync", "agent": "pinned", "model": "prov:lane:astra"},
            caller_session_id="independent", caller_provider_id=None)
        assert not result["ok"] and "away" in result["error"], result
    assert not mo.named_by_user("prov:lane:astra", mo.user_texts("independent"))


@pytest.mark.parametrize("source", ["api", "user", "queue"])
async def test_api_key_message_on_owner_chat_is_not_owner_consent(world, source):
    _turn("parent", source, "use Astra", payload={"api_user_id": "key-a"})
    assert not mo.named_by_user("prov:lane:astra", mo.user_texts("parent"))


@pytest.mark.parametrize("source,payload,counts", [
    ("api", {"api_user_id": "key-a"}, True),
    ("user", {"api_user_id": "key-a"}, True),
    ("queue", {"api_user_id": "key-a"}, True),
    ("user", {}, True),  # Legacy turns inherit their session's principal.
    ("user", {"user_id": "admin"}, False),
    ("queue", {"user_id": "paired:phone"}, False),
    ("api", {"api_user_id": "key-b"}, False),
])
def test_only_a_sessions_own_principal_can_name_a_model(world, source, payload, counts):
    db = world[0]
    db.create_api_user("key-a", "Key A", "hash")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id='key-a' WHERE id='parent'")
    _turn("parent", source, "use Astra", payload=payload)
    assert mo.named_by_user("prov:lane:astra", mo.user_texts("parent")) == counts


@pytest.mark.parametrize("worker", [False, True])
def test_launch_ancestry_stops_at_a_different_principal(world, worker):
    db = world[0]
    db.create_api_user("key-a", "Key A", "hash")
    _turn("parent", "user", "use Astra")
    sessions.create_session("foreign", "caller", title="Foreign", is_active=True,
                            is_worker=worker, parent_session_id="parent" if worker else None)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id='key-a' WHERE id='foreign'")
    if not worker:
        _turn("foreign", "agent_message", "review", command_id="orchestration:start:test",
              payload={"orchestration_sender": "parent"})
    assert not mo.named_by_user("prov:lane:astra", mo.user_texts("foreign"))
    assert mo.chat_of("foreign") == "foreign"


def test_legacy_task_principal_inherits_from_parent_without_crossing_to_owner(world):
    db = world[0]
    db.create_api_user("key-a", "Key A", "hash")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id='key-a' WHERE id='parent'")
    _turn("parent", "api", "use Astra", payload={"api_user_id": "key-a"})
    sessions.create_session("legacy", "caller", title="Legacy", is_worker=True,
                            parent_session_id="parent")
    assert mo.chat_of("legacy") == "parent"
    assert mo.named_by_user("prov:lane:astra", mo.user_texts("legacy"))


async def test_launch_after_receiving_peer_context_does_not_inherit_the_peers_consent(chats):
    _turn("caller", "user", "use Astra", profile="agent")
    sessions.create_session("independent", "agent", title="Independent", is_active=True)
    _turn("independent", "user", "review it", profile="agent")
    outcome = await orch.make_handler(lambda: "caller")("message", {
        "session_id": "independent", "message": "please review", "request_id": "peer-launch"
    }, ToolCtx())
    assert not outcome.is_error, outcome.text
    outcome = await _start({**ARGS, "request_id": "independent-child"}, sender="independent")
    assert not outcome.is_error, outcome.text
    child = json.loads(outcome.text)["session_id"]
    assert not mo.named_by_user("prov:lane:astra", mo.user_texts(child))


async def test_real_started_chat_keeps_origin_after_its_own_later_owner_message(chats):
    _turn("caller", "user", "run all subagents with GPT-6 Astra", profile="agent")
    _turn("caller", "user", "start the review chat", profile="agent")
    outcome = await _start(ARGS)
    assert not outcome.is_error, outcome.text
    child_id = json.loads(outcome.text)["session_id"]
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE session_id=%s", (child_id,))
        conn.cursor().execute("UPDATE sessions SET away_mode=TRUE WHERE id=%s", (child_id,))
    _turn(child_id, "user", "check the tests too", profile="agent")
    result = await td.dispatch_task_tool(
        {"prompt": "go", "mode": "sync", "agent": "pinned", "model": "prov:lane:astra"},
        caller_session_id=child_id, caller_provider_id=None)
    assert result["ok"], result
    assert result["model_override"]["source"] == mo.NAMED
    assert chats[2] == []


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
    # Fit the parent's effort to Grok's supported scale; drop the invalid window.
    assert row["effort"] == "high" and row["context_window"] is None
    turn = orch._start_turn(row["id"])
    assert turn["overrides"]["model"] == "prov:grok:grok-4.5"
    assert turn["overrides"]["effort"] == "high" and "provider" not in turn["overrides"]
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


@pytest.mark.parametrize("window, expected", [(272_000, 200_000), (1_000_000, 1_000_000)])
async def test_a_named_claude_model_keeps_the_senders_window_choice(chats, window, expected):
    """A GPT chat on its standard window that starts a chat on Claude gets
    Claude's standard window, not the CLI's 1M default: the user's "standard"
    choice carries across model families as it does for a Task worker."""
    db = chats[0]
    db.create_model_provider("codex", "Codex", "openai", "https://example.invalid/v1",
                             models=[{"id": "gpt-6-luna", "label": "GPT-6 Luna"}])
    sessions.create_session("gpt-caller", "agent", title="GPT caller", is_active=True,
                            provider_id="codex", model="prov:codex:gpt-6-luna", context_window=window)
    _turn("gpt-caller", "user", "start a new chat on opus for the review", profile="agent")
    outcome = await _start({**ARGS, "model": "opus"}, sender="gpt-caller")
    assert not outcome.is_error, outcome.text
    row = sessions.get_session(json.loads(outcome.text)["session_id"])
    assert (row["provider_id"], row["model"], row["context_window"]) == (None, "opus", expected)
    assert orch._start_turn(row["id"])["overrides"]["context_window"] == expected


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
    ("gpt-99-imaginary", "No matching model id or display name"),
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
    from app.core.agent_provider_models import build_task_contract
    from fluid.tools.builtins.swarm import input_schema
    descriptions = [prop["description"],
                    build_task_contract("caller", None).input_schema["properties"]["model"]["description"],
                    input_schema()["properties"]["tasks"]["items"]["properties"]["model"]["description"]]
    for description in descriptions:
        assert "Pass the model exactly as the user named it" in description
        assert "model IDs and display names" in description
        assert "case, spaces and dashes" in description
        assert "start nothing" in description and "ambiguous" in description
        assert "receiving worker's own eligibility" in description
    assert "Examples: sonnet." in prop["description"]
    assert "pass model only when the user named a model" in spec.description


def test_the_owner_prompt_reads_as_one_question_in_the_assistant():
    from app.core import assistant_confirm

    question = assistant_confirm.question(mo.PROMPT_TOOL, {
        "model": "prov:lane:astra", "model_switch": "start pinned on GPT-6 Astra (Lane) instead of Opus"})
    assert question == ("Do you authorize me to start pinned on GPT-6 Astra (Lane) instead of Opus? "
                        "Please answer yes or no.")


# ---------------------------------------------------------------------------
# Which models fast mode applies to (inherited fast mode)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model, route, expected", [
    ("opus", "delegated", True),
    ("claude-opus-4-7", "delegated", True),
    ("opus[1m]", "delegated", True),
    ("sonnet", "delegated", False),
    ("haiku", "delegated", False),
    ("prov:lane:gpt-5.5", "openai_responses", True),
    ("prov:lane:gpt-6-astra", "openai_responses", True),
    ("prov:lane:gpt-6.1", "openai_responses", True),
    ("prov:lane:gpt-6-mini", "openai_responses", False),
    ("prov:grok:grok-4.5", "openai_chat", False),
    ("opus", "native", False),
    ("", "delegated", False),
])
def test_fast_mode_applies_only_where_the_composer_offers_it(monkeypatch, model, route, expected):
    from app.core.provider import harness_adapter

    seen = []

    def decide(options):
        seen.append((options.model, (options.extra or {}).get("provider_id")))
        return route
    monkeypatch.setattr(harness_adapter, "_decide_provider_kind", decide)
    assert mo.fast_mode_supported(model, None) is expected
    if model.startswith("prov:"):
        # The composite is unpacked before the route is decided.
        _, provider, bare = model.split(":", 2)
        assert seen == [(bare, provider)]


def test_an_unresolvable_route_is_not_fast(monkeypatch):
    from app.core.provider import harness_adapter

    def decide(options):
        raise RuntimeError("no such provider")
    monkeypatch.setattr(harness_adapter, "_decide_provider_kind", decide)
    assert mo.fast_mode_supported("opus", "gone") is False


# ---------------------------------------------------------------------------
# A translated (ChatGPT/Codex-served) main keeps the requested model
# ---------------------------------------------------------------------------

async def _through_translated_main_hooks(tool_name, args, tmp_path):
    """``args`` after the PreToolUse hooks a translated main's session runs.

    The native leg runs the app's SDK hooks on fluid's HookBus, and both its
    JSON path and code mode apply a ``modify`` before the Task dispatcher.
    """
    from fluid.hooks.bus import PRE_TOOL_USE

    from app.core.hook_service import build_hooks_config
    from app.core.provider.sdk_hook_bridge import sdk_hooks_bus

    bus = sdk_hooks_bus(build_hooks_config(str(tmp_path), None, translated_main=True), str(tmp_path))
    decision = await bus.emit(PRE_TOOL_USE, {"name": tool_name, "input": dict(args)})
    assert decision.action != "deny"
    if decision.action == "modify" and decision.replacement_input is not None:
        return decision.replacement_input
    return args


@pytest.mark.parametrize("default, requested, provider, model_id", [
    # Cross-provider: the agent would run on Claude, the user asked for a lane model.
    ("sonnet", "prov:lane:sol", "lane", "sol"),
    # Same provider: the agent would run on lane's astra, the user asked for sol.
    ("prov:lane:astra", "prov:lane:sol", "lane", "sol"),
])
async def test_a_translated_main_starts_the_native_task_on_the_named_model(
    world, tmp_path, default, requested, provider, model_id,
):
    """Live ov6: a ChatGPT-served chat's Task(model=...) ran on the default.

    The translated-main PreToolUse hook stripped ``model`` from every
    ``Task`` call, so the worker silently started on the agent's default.
    """
    _, seen, prompts, _ = world
    subagent_default.write(default)
    _turn("parent", "user", f'Use the Task tool with model "{requested}"')
    args = await _through_translated_main_hooks(
        "Task", {"agent": "pinned", "prompt": "go", "mode": "sync", "model": requested}, tmp_path)
    assert args.get("model") == requested

    dispatch = td.make_fluid_dispatcher("parent", "caller", "lane")
    out = await dispatch(args, ToolCtx())
    payload = json.loads(out.text)
    assert payload["ok"], payload
    assert payload["model"] == model_id
    assert payload["provider"]["id"] == provider
    assert payload["model_override"]["source"] == mo.NAMED
    assert (seen[-1]["provider_id"], seen[-1]["model_id"]) == (provider, model_id)
    assert prompts == []


async def test_the_delegated_task_keeps_its_model_through_the_same_hooks(world, tmp_path):
    """The Claude Code leg's Task is ``mcp__builtins__Task``: never stripped."""
    _, seen, _, _ = world
    _turn("parent", "user", "run it on prov:lane:sol")
    args = await _through_translated_main_hooks(
        "mcp__builtins__Task", {"agent": "pinned", "model": "prov:lane:sol"}, tmp_path)
    payload = await _task(args)
    assert payload["ok"], payload
    assert (payload["model"], payload["provider"]["id"]) == ("sol", "lane")
    assert (seen[-1]["provider_id"], seen[-1]["model_id"]) == ("lane", "sol")
