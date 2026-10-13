"""Assistant identity, provenance and scope helpers."""
import pytest

from app.core import assistant_service as service, task_registry
from app.core.config import settings
from app.db import assistant as store, database, turns
from app.db import session_events as events_db
from app.db.base import get_db
from tests.db.test_assistant_store import assistant_fixture, events, lose_agent


# ---------------------------------------------------------------------------
# Moving an older assistant onto the built-in agent
# ---------------------------------------------------------------------------

TEXT_MODEL_AGENT = {"model": "gen-model", "provider": "gen-provider", "effort": "low", "avatar_id": "owl",
                    "tool_selection_mode": "all", service.DEDICATED_KEY: True}


def legacy_assistant(monkeypatch, agent_config, *, launched=None, home_model=None, account_agent="juniper"):
    """An assistant saved by an older release: it ran on its own agent row."""
    monkeypatch.setattr(settings, "instance_id", "assistant-test")
    database.create_profile("juniper", "Juniper", None, agent_config)
    home = database.create_session("legacy-home", "juniper", title=service.HOME_TITLE, is_active=True, hidden=True)
    if launched:
        database.update_session(session_id=home["id"], launched_model=launched)
    if home_model:
        database.set_session_run_binding(home["id"], model=home_model)
    events_db.append_events(home["id"], [{"type": "message.user", "payload": {
        "type": "message.user", "content": "Earlier conversation"}}])
    with get_db() as conn:
        store.write_account(conn, {"agent_id": account_agent, "introduction": {"preferred_name": "Ada"},
                                   "sources": {"conn-elsewhere": True}})
    store.configure(service.assistant_id(), {"agent_id": "juniper", "home_session_id": home["id"]})
    return database.get_session(home["id"])


@pytest.mark.parametrize("launched,home_model", [
    ("gen-model", None),                         # Ran on the text model setup gave it.
    (None, "prov:gen-provider:gen-model"),       # The composer echoed that same default.
])
def test_text_model_assistant_moves_to_the_builtin_agent_and_the_main_default(monkeypatch, launched, home_model):
    home = legacy_assistant(monkeypatch, TEXT_MODEL_AGENT, launched=launched, home_model=home_model)
    state = service.get_state()
    assert state["agent_id"] == service.AGENT_ID and state["openable"]
    assert (state["assistant_name"], state["avatar_id"]) == ("Juniper", "owl")
    assert state["home_session_id"] == home["id"]
    assert state["introduction"]["preferred_name"] == "Ada"
    assert store.get_account()["sources"] == {"conn-elsewhere": True}
    row = database.get_session(home["id"])
    assert row["profile_id"] == service.AGENT_ID
    assert (row["model"], row["provider_id"], row["launched_model"]) == (None, None, None)
    assert [e["payload"]["content"] for e in events_db.get_events(home["id"], 0)] == ["Earlier conversation"]
    # The old agent stays an ordinary agent; sessions may still use it.
    assert database.get_profile("juniper") is not None
    assert service.open_home()["id"] == home["id"]


def test_a_model_the_owner_picked_survives_the_move(monkeypatch):
    home = legacy_assistant(monkeypatch, TEXT_MODEL_AGENT, launched="gen-model", home_model="opus")
    service.get_state()
    row = database.get_session(home["id"])
    assert (row["profile_id"], row["model"], row["provider_id"]) == (service.AGENT_ID, "opus", home["provider_id"])


def test_a_custom_agents_model_survives_the_move(monkeypatch):
    home = legacy_assistant(monkeypatch, {"model": "custom-model", "provider": "custom-provider", "avatar_id": "fox"},
                            launched="custom-model")
    state = service.get_state()
    assert (state["assistant_name"], state["avatar_id"]) == ("Juniper", "fox")
    row = database.get_session(home["id"])
    assert (row["profile_id"], row["model"], row["provider_id"]) == (service.AGENT_ID, "custom-model", "custom-provider")
    assert database.get_profile("juniper")["config"]["model"] == "custom-model"  # Untouched.


def test_the_move_waits_for_a_live_home_turn(monkeypatch):
    home = legacy_assistant(monkeypatch, TEXT_MODEL_AGENT, launched="gen-model")
    turn = turns.enqueue_turn(home["id"], "user", prompt="In flight")
    assert service.get_state()["agent_id"] == service.AGENT_ID
    assert database.get_session(home["id"])["profile_id"] == "juniper"
    turns.settle_turn(turn["id"], "completed")
    service.reconcile()
    row = database.get_session(home["id"])
    assert (row["profile_id"], row["model"]) == (service.AGENT_ID, None)


def test_a_peer_moved_account_takes_its_identity_from_this_workers_home(monkeypatch):
    # Another worker moved the account first, without the old agent row.
    home = legacy_assistant(monkeypatch, TEXT_MODEL_AGENT, launched="gen-model", account_agent=service.AGENT_ID)
    state = service.get_state()
    assert (state["assistant_name"], state["avatar_id"]) == ("Juniper", "owl")
    assert database.get_session(home["id"])["profile_id"] == service.AGENT_ID


def test_a_changed_builtin_agent_row_is_restored(assistant_fixture):
    with get_db() as conn:
        conn.cursor().execute("UPDATE agents SET name='Edited', config=%s WHERE id=%s",
                              ('{"model": "pinned", "system_prompt": "x"}', service.AGENT_ID))
    service.reconcile()
    agent = database.get_profile(service.AGENT_ID)
    assert agent["name"] == service.AGENT_NAME and agent["config"] == service.AGENT_CONFIG and agent["is_builtin"]


# ---------------------------------------------------------------------------
# Prompt assembly: the home's instructions and delegation default
# ---------------------------------------------------------------------------

async def _assemble(session_id, profile_id):
    from unittest.mock import AsyncMock
    from app.core.provider import harness_adapter as ha
    from app.core.query_options import build_options_from_profile
    options, _ = build_options_from_profile(database.get_profile(profile_id), session_id=session_id)
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(options)
    projected = adapter.connect.await_args.args[0]
    return options, projected, "\n\n".join(ha._workflow_prompt_blocks(projected, None))


@pytest.mark.asyncio
async def test_home_prompt_has_the_assistant_block_and_delegates_by_default(assistant_fixture):
    from app.core.agent_provider_models import DELEGATE_BY_DEFAULT, DELEGATE_WHEN_AUTHORIZED
    from app.core.model_registry import DEFAULT_MODEL
    database.set_system_setting("text_generation_model", "haiku")
    service.configure({"assistant_name": "Juniper"})
    options, projected, workflow = await _assemble(assistant_fixture["id"], service.AGENT_ID)
    prompt = str(options.system_prompt)
    assert service.HOME_INSTRUCTIONS in prompt and "Your name: Juniper." in prompt
    for section in ("## Talk", "## Act", "## Report", "## Interrupts", "## Approvals", "## Corrections", "## Trust"):
        assert section in prompt
    assert projected.extra.get("assistant_home") is True
    assert DELEGATE_BY_DEFAULT in workflow
    assert "authorizes subagents" not in prompt + workflow
    # The home runs on the main chat default, never the text-generation model.
    assert options.model == DEFAULT_MODEL


@pytest.mark.asyncio
async def test_ordinary_session_prompt_is_unchanged(assistant_fixture):
    from app.core.agent_provider_models import DELEGATE_BY_DEFAULT, DELEGATE_WHEN_AUTHORIZED
    ordinary = database.create_session("ordinary-chat", "assistant-agent", is_active=True)
    options, projected, workflow = await _assemble(ordinary["id"], "assistant-agent")
    assert "# Personal assistant" not in str(options.system_prompt)
    assert "assistant_home" not in projected.extra
    assert DELEGATE_WHEN_AUTHORIZED in workflow and DELEGATE_BY_DEFAULT not in workflow
    # A session started from the home is linked, not given the home's block.
    child = database.create_session("linked-job", service.AGENT_ID, is_active=True)
    service.link_job(assistant_fixture["id"], child["id"])
    options, projected, workflow = await _assemble(child["id"], service.AGENT_ID)
    assert "# Personal assistant" not in str(options.system_prompt)
    assert "assistant_home" not in projected.extra and DELEGATE_WHEN_AUTHORIZED in workflow
    # Its reply reaches the home on its own; it is not told to message it.
    assert "session messaging" not in service.build_role_context(child["id"])


def test_unconfigured_safe_empty():
    state = service.get_state()
    assert state["status"] == "unconfigured"
    assert state["agent_id"] is None and not state["enabled"]
    assert service.source_catalog() == []
    with pytest.raises(ValueError, match="Set up"):
        service.open_home()
    assert not state["openable"] and state["home_session_id"] is None


def test_stable_home_identity_and_onboarding_preserve_agent(assistant_fixture):
    assert service.open_home()["id"] == assistant_fixture["id"]
    assert service.is_primary_session(assistant_fixture["id"])
    before = database.get_profile(service.AGENT_ID)
    service.configure({"assistant_name": "Overseer", "avatar_id": "robot", "introduction": {"preferred_name": "Ada", "github_username": "ada", "repository_refs": ["ada/project"]}})
    service.configure({"introduction": {"notes": "Likes concise updates"}})
    # The name and avatar are the assistant's own; the built-in agent is untouched.
    assert database.get_profile(service.AGENT_ID) == before
    state = service.get_state()
    assert (state["assistant_name"], state["avatar_id"]) == ("Overseer", "robot")
    context = service.build_role_context(assistant_fixture["id"])
    assert "Ada" in context and "ada/project" in context and "not access grants" in context
    assert "Your name: Overseer." in context
    assert service.get_state()["introduction"]["preferred_name"] == "Ada"
    assert service.open_home()["id"] == assistant_fixture["id"]
    service.configure({"introduction": {"github_username": "", "repository_refs": []}})
    assert service.get_state()["introduction"]["notes"] == "Likes concise updates"


def test_persistent_home_cannot_settle_but_regular_sessions_can(assistant_fixture):
    with pytest.raises(PermissionError, match="persistent"):
        database.set_session_active(assistant_fixture["id"], False)
    assert database.get_session(assistant_fixture["id"])["is_active"]
    ordinary = database.create_session("ordinary", "assistant-agent", is_active=True)
    assert not database.set_session_active(ordinary["id"], False)["is_active"]


def test_an_older_clients_agent_choice_is_accepted_and_ignored(assistant_fixture):
    database.create_profile("specialist", "Specialist", None, {"prompt": "Other prompt"})
    state = service.configure({"agent_id": "specialist"})
    assert state["agent_id"] == service.AGENT_ID
    home = service.open_home()
    assert home["id"] == assistant_fixture["id"] and home["profile_id"] == service.AGENT_ID
    assert database.get_profile("assistant-agent")["config"]["prompt"] == "Keep custom prompt"
    assert service.configure({"create_agent": True})["agent_id"] == service.AGENT_ID


def test_proactive_provenance_revoke_and_forgery(assistant_fixture):
    events()
    run = store.admit_run(service.assistant_id())
    marker = service.proactive_metadata(run)
    assert service.assert_proactive_allowed(marker, run["session_id"])["id"] == run["id"]
    with pytest.raises(ValueError, match="match"):
        service.assert_proactive_allowed(dict(marker, source_ids=["forged"]))
    with pytest.raises(ValueError, match="Invalid"):
        service.assert_proactive_allowed(dict(marker, read_only=False))
    # A legacy pause no longer revokes; removing the agent does.
    service.configure({"paused": True, "enabled": False})
    store.configure(service.assistant_id(), {"paused": True, "enabled": False})
    service.reconcile()  # Sets enabled where the home is and ignores paused.
    assert service.assert_proactive_allowed(marker)["id"] == run["id"]
    lose_agent()
    with pytest.raises(ValueError, match="missing its agent"):
        service.assert_proactive_allowed(marker)
    service.reconcile()  # The built-in agent is still there: projection restores it.
    assert service.assert_proactive_allowed(marker)["id"] == run["id"]
    database.update_connector("conn-mail", is_active=False)
    with pytest.raises(ValueError, match="grant"):
        service.assert_proactive_allowed(marker)


def test_owner_runner_and_explicit_source_optin(assistant_fixture):
    database.create_connector("peer", "gmail", "Another worker", {}, [], is_active=True, runner_id="other-worker")
    database.update_connector_status("peer", "connected")
    database.create_api_user("another-user", "Other", "unique-user-key")
    database.create_connector("user-mail", "gmail", "API user's mail", {}, [], api_user_id="another-user", is_active=True, runner_id=service.assistant_id())
    database.update_connector_status("user-mail", "connected")
    assert events(connector_id="peer") == []
    assert events(connector_id="user-mail") == []
    catalog = service.source_catalog()
    assert {s["id"] for s in catalog} == {"conn-mail", "peer"}
    assert not next(s for s in catalog if s["id"] == "peer")["available"]
    with pytest.raises(ValueError, match="unavailable"):
        service.set_source("peer", True)
    service.set_source("conn-mail", False)
    assert events() == []


def test_stop_latch_and_durable_stop_are_honored(assistant_fixture):
    sid = assistant_fixture["id"]
    task_registry.task_registry.mark_user_stopped(sid)
    assert service.session_stopped(sid)
    task_registry.task_registry.clear_user_stop(sid)
    turn = turns.enqueue_turn(sid, "user", prompt="run")
    turns.settle_turn(turn["id"], "aborted", stop_reason="user_stopped")
    assert service.session_stopped(sid)
    turns.enqueue_turn(sid, "user", prompt="user continued")
    assert not service.session_stopped(sid)


def test_linked_sessions_are_normal_and_scope_checked(assistant_fixture):
    child = database.create_session("specialist-session", "assistant-agent", title="Normal linked work")
    job = service.link_job(assistant_fixture["id"], child["id"])
    assert service.link_job(assistant_fixture["id"], child["id"])["id"] == job["id"]
    assert child.get("parent_session_id") is None
    assert store.get_activity(service.assistant_id())[0]["session_id"] == child["id"]
    assert assistant_fixture["id"] in service.build_role_context(child["id"])
    database.create_api_user("another-user", "Other", "unique-user-key")
    database.create_session("other-owner", "assistant-agent", api_user_id="another-user")
    with pytest.raises(ValueError, match="owner"):
        service.link_job(assistant_fixture["id"], "other-owner")



def test_dedicated_connector_credential_revocation_and_agent_scope(assistant_fixture):
    events()
    run = store.admit_run(service.assistant_id())
    marker = service.proactive_metadata(run)
    database.update_api_user("mail-user", is_active=False)
    assert not next(s for s in service.source_catalog() if s["id"] == "conn-mail")["available"]
    assert events(message_id="revoked-credential") == []
    with pytest.raises(ValueError, match="grant"):
        service.assert_proactive_allowed(marker)
    database.update_api_user("mail-user", is_active=True)
    database.create_profile("other-allowed-agent", "Scoped specialist", None, {})
    database.update_api_user("mail-user", profile_ids=["other-allowed-agent"])
    assert events(message_id="wrong-agent") == []
    with pytest.raises(ValueError, match="grant"):
        service.assert_proactive_allowed(marker)
    database.update_api_user("mail-user", profile_ids=[service.AGENT_ID])
    assert service.assert_proactive_allowed(marker)["id"] == run["id"]
    assert events(message_id="allowed-agent")


@pytest.mark.asyncio
async def test_manager_loop_sweeps_retention_at_most_once_per_interval(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.core import assistant_manager as module

    manager = module.AssistantManager()
    monkeypatch.setattr(manager, "run_once", AsyncMock())
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    sweeps = []

    def sweep(assistant_id):
        sweeps.append(assistant_id)
        raise RuntimeError("database unavailable")  # Logged; never stops the loop.
    monkeypatch.setattr(store, "retention_sweep", sweep)
    loop = asyncio.get_running_loop()
    passes = []
    done = asyncio.Event()

    def activity(*args):
        passes.append(True)
        loop.call_soon_threadsafe(done.set if len(passes) >= 4 else manager._wake.set)
        return []
    monkeypatch.setattr(store, "get_activity", activity)
    task = asyncio.create_task(manager._loop())
    try:
        await asyncio.wait_for(done.wait(), timeout=3)
        assert len(sweeps) == 1 and not task.done()
        clock.now += module.RETENTION_INTERVAL_SECONDS
        await manager._maybe_retention_sweep()
        await manager._maybe_retention_sweep()
        assert len(sweeps) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
