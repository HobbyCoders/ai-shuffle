"""Assistant identity, provenance and scope helpers."""
import pytest

from app.core import assistant_service as service, task_registry
from app.db import assistant as store, database, turns
from tests.db.test_assistant_store import assistant_fixture, events


def test_unconfigured_safe_empty():
    state = service.get_state()
    assert state["status"] == "unconfigured"
    assert state["agent_id"] is None and not state["enabled"]
    assert service.source_catalog() == []
    with pytest.raises(ValueError, match="Choose"):
        service.open_home()


def test_stable_home_identity_and_onboarding_preserve_agent(assistant_fixture):
    assert service.open_home()["id"] == assistant_fixture["id"]
    assert service.is_primary_session(assistant_fixture["id"])
    before = database.get_profile("assistant-agent")
    service.configure({"assistant_name": "Overseer", "avatar_id": "robot", "introduction": {"preferred_name": "Ada", "github_username": "ada", "repository_refs": ["ada/project"]}})
    service.configure({"introduction": {"notes": "Likes concise updates"}})
    after = database.get_profile("assistant-agent")
    assert after["name"] == "Overseer"
    assert after["mcp_tools"] == before["mcp_tools"]
    assert {k: v for k, v in after["config"].items() if k != "avatar_id"} == before["config"]
    context = service.build_role_context(assistant_fixture["id"])
    assert "Ada" in context and "ada/project" in context and "not access grants" in context
    assert service.get_state()["introduction"]["preferred_name"] == "Ada"
    assert service.open_home()["id"] == assistant_fixture["id"]
    service.configure({"introduction": {"github_username": "", "repository_refs": []}})
    assert service.get_state()["introduction"]["notes"] == "Likes concise updates"


def test_changing_agent_preserves_home_not_prompt(assistant_fixture):
    database.create_profile("specialist", "Specialist", None, {"prompt": "Other prompt"})
    service.configure({"agent_id": "specialist"})
    home = service.open_home()
    assert home["id"] == assistant_fixture["id"]
    assert home["profile_id"] == "specialist"
    assert database.get_profile("assistant-agent")["config"]["prompt"] == "Keep custom prompt"


def test_live_home_agent_switch_refused(assistant_fixture):
    database.create_profile("other", "Other", None, {})
    turns.enqueue_turn(assistant_fixture["id"], "user", prompt="In flight")
    with pytest.raises(ValueError, match="settle"):
        service.configure({"agent_id": "other"})
    assert service.get_state()["agent_id"] == "assistant-agent"


def test_proactive_provenance_pause_revoke_and_forgery(assistant_fixture):
    events()
    run = store.admit_run(service.assistant_id())
    marker = service.proactive_metadata(run)
    assert service.assert_proactive_allowed(marker, run["session_id"])["id"] == run["id"]
    with pytest.raises(ValueError, match="match"):
        service.assert_proactive_allowed(dict(marker, source_ids=["forged"]))
    with pytest.raises(ValueError, match="Invalid"):
        service.assert_proactive_allowed(dict(marker, read_only=False))
    service.configure({"paused": True})
    with pytest.raises(ValueError, match="paused"):
        service.assert_proactive_allowed(marker)
    service.configure({"paused": False})
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
    database.update_api_user("mail-user", profile_ids=["assistant-agent"])
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
