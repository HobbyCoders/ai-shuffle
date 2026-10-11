"""Owner-only, opt-in starter team installation over the ordinary agents API."""
import pytest
from tests.builtin_fixtures import create_unchanged_builtin

from app.db import database
from app.core.builtin_subagents import seed_builtin_subagents

BASE = "/api/v1/agents"
INSTALL = BASE + "/templates/starter-team/install"


def test_templates_listed(authenticated_client):
    response = authenticated_client.get(BASE + "/templates")
    assert response.status_code == 200
    assert response.json() == []


def test_templates_route_not_shadowed_by_profile_lookup(authenticated_client):
    response = authenticated_client.get(BASE + "/templates")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_startup_seeds_no_agents(bare_db):
    assert seed_builtin_subagents() == {}
    assert database.get_all_profiles() == []


def test_install_is_opt_in_idempotent_editable_and_deletable(authenticated_client):
    client = authenticated_client
    before = {a["id"] for a in database.get_all_profiles()}
    template = client.get(BASE + "/team-templates").json()[0]
    assert template["id"] == "starter-team"
    assert all(a["existing_agent_id"] is None for a in template["agents"])
    response = client.post(INSTALL)
    assert response.status_code == 200, response.text
    installed = response.json()
    ids = {a["existing_agent_id"] for a in installed["agents"]}
    assert len(ids) == 2 and None not in ids
    assert not ids & {"Explore", "Plan", "general-purpose", "frontend-designer"}
    assert {a["id"] for a in database.get_all_profiles()} == before | ids
    for agent_id in ids:
        assert not database.get_profile(agent_id)["is_builtin"]
        assert database.get_subagent(agent_id)["prompt"]
    assert client.post(INSTALL).json() == installed
    assert client.get(BASE + "/team-templates").json() == [installed]
    agent_id = installed["agents"][0]["existing_agent_id"]
    response = client.put(BASE + "/" + agent_id, json={"name": "My renamed agent", "config": {
        "system_prompt": {"type": "custom", "content": "My own prompt"},
    }})
    assert response.status_code == 200, response.text
    assert client.post(INSTALL).json() == installed
    assert database.get_subagent(agent_id)["prompt"] == "My own prompt"
    seed_builtin_subagents()
    assert database.get_profile(agent_id)["name"] == "My renamed agent"
    assert client.delete(BASE + "/" + agent_id).status_code == 204
    replacement = client.post(INSTALL).json()
    assert replacement["agents"][0]["existing_agent_id"] not in ids
    assert replacement["agents"][1] == installed["agents"][1]


def test_install_preserves_same_named_owner_agent_and_old_key_scope(authenticated_client):
    database.create_profile("owner-gp", "General Purpose", "Mine", {"model": "opus"})
    create_unchanged_builtin("general-purpose")
    database.create_api_user("retired-key", "Retired scope", "synthetic-template-key", profile_ids=["general-purpose"])
    seed_builtin_subagents()
    before = database.get_profile("owner-gp")
    result = authenticated_client.post(INSTALL).json()
    assert result["agents"][0]["existing_agent_id"] == "owner-gp"
    assert database.get_profile("owner-gp") == before
    assert database.get_api_user("retired-key")["profile_ids"] == ["general-purpose"]


@pytest.mark.parametrize("retired_id", ["general-purpose", "frontend-designer"])
def test_retired_automation_still_loads_and_requires_reassignment(authenticated_client, retired_id):
    create_unchanged_builtin(retired_id)
    database.create_automation("retired-auto", "Saved work", "interval", {"seconds": 300},
                               retired_id, prompt="Do work", enabled=True)
    seed_builtin_subagents()
    response = authenticated_client.get("/api/v1/automations/retired-auto")
    assert response.status_code == 200, response.text
    assert response.json()["profile_id"] is None
    assert not response.json()["is_enabled"]
    response = authenticated_client.post("/api/v1/automations/retired-auto/enable")
    assert response.status_code == 400
    assert "Choose an agent" in response.json()["detail"]
    response = authenticated_client.patch("/api/v1/automations/retired-auto", json={"is_enabled": True})
    assert response.status_code == 400
    assert "Choose an agent" in response.json()["detail"]
    assert not database.get_automation("retired-auto")["is_enabled"]


@pytest.mark.parametrize("profile_id", [None, ""])
def test_cannot_unlink_enabled_automation(authenticated_client, profile_id):
    database.create_profile("chosen", "Chosen", None, {})
    database.create_automation("linked-auto", "Saved work", "interval", {"seconds": 300},
                               "chosen", prompt="Do work", enabled=True)
    response = authenticated_client.patch("/api/v1/automations/linked-auto", json={"profile_id": profile_id})
    assert response.status_code == 400
    assert database.get_automation("linked-auto")["profile_id"] == "chosen"
    response = authenticated_client.patch("/api/v1/automations/linked-auto",
                                          json={"profile_id": profile_id, "is_enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["profile_id"] is None
    response = authenticated_client.patch("/api/v1/automations/linked-auto",
                                          json={"profile_id": "chosen", "is_enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["is_enabled"]


@pytest.mark.parametrize("method,path", [("get", "/team-templates"), ("post", "/templates/starter-team/install")])
@pytest.mark.parametrize("api_key", [False, True])
def test_templates_require_owner(client, method, path, api_key):
    from unittest.mock import patch
    from app.api.auth import hash_api_key
    headers = {}
    if api_key:
        database.create_api_user("template-reader", "Ordinary API user", hash_api_key("aih_template_test"))
        headers["Authorization"] = "Bearer aih_template_test"
    before = database.get_all_profiles()
    # Not a trusted local-owner connection and not an owner session.
    with patch("app.api.auth.owner_auth.local_access", return_value=False):
        response = getattr(client, method)(BASE + path, headers=headers)
    assert response.status_code == 403
    assert database.get_all_profiles() == before


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("retire", [False, True])
async def test_stale_action_only_approval_is_cancelled(mock_db, monkeypatch, queued, retire):
    import asyncio
    from unittest.mock import AsyncMock
    from app.api.automations import approve_run_endpoint
    from app.core import automation_manager as module

    manager = module.AutomationManager()
    monkeypatch.setattr(module, "automation_manager", manager)
    tasks = []
    create_task = manager._create_tracked_task

    def track(coro, *, name):
        task = create_task(coro, name=name)
        tasks.append(task)
        return task

    monkeypatch.setattr(manager, "_create_tracked_task", track)
    actions = AsyncMock()
    output = AsyncMock()
    prompt = AsyncMock()
    monkeypatch.setattr(manager, "_execute_action_steps", actions)
    monkeypatch.setattr(manager, "_deliver_output", output)
    monkeypatch.setattr(manager, "_run_prompt_as_session", prompt)
    create_unchanged_builtin("general-purpose")
    database.create_automation(
        "retirement-job", "Saved job", "interval", {"seconds": 300},
        "general-purpose", prompt="", enabled=True, requires_approval=True,
        action_steps=[{"type": "shell", "command": "must not run"}],
    )
    # Produce a real pending approval rather than seeding only the consumer's row.
    await manager._execute("retirement-job", {"trigger": "manual"})
    approval = next(a for a in database.get_pending_automation_approvals()
                    if a["automation_id"] == "retirement-job")

    def disable():
        if retire:
            seed_builtin_subagents()
            assert database.get_automation("retirement-job")["profile_id"] is None
        else:
            database.update_automation("retirement-job", is_enabled=False)

    if queued:
        holder = asyncio.get_running_loop().create_future()
        manager._running["retirement-job"] = holder
    else:
        disable()
    await approve_run_endpoint(approval["id"], token="admin")
    if queued:
        # execute_approved_run already loaded the enabled/linked snapshot.
        disable()
        holder.set_result(None)
    await asyncio.gather(*tasks)
    run = database.get_automation_run(approval["run_id"])
    assert run["status"] == "cancelled"
    assert ("Choose an agent" if retire else "Enable this automation") in run["error"]
    assert run["completed_at"]
    actions.assert_not_called()
    output.assert_not_called()
    prompt.assert_not_called()
    assert not database.get_automation("retirement-job")["is_enabled"]


def test_install_publishes_shared_agents_only_after_commit(bare_db, monkeypatch):
    from unittest.mock import Mock, call
    from app.core.agent_templates import install_starter_team
    from app.core.global_events import global_events

    before = database.get_all_profiles()
    # Query through another connection at notification time: both members must
    # already be visible, not just inserted inside the install transaction.
    def committed(*args, **kwargs):
        assert len(database.get_all_profiles()) == len(before) + 2

    changed = Mock(side_effect=committed)
    monkeypatch.setattr(global_events, "resources_changed", changed)
    installed = install_starter_team()
    assert changed.call_args_list == [
        call("agents", user_type="account"), call("agents", user_type="paired"),
        call("agents"), call("agents", user_type="api_user"),
    ]
    changed.reset_mock()
    assert install_starter_team() == installed
    changed.assert_not_called()


@pytest.mark.parametrize("failure", ["insert", "commit"])
def test_failed_install_does_not_publish_agents(bare_db, monkeypatch, failure):
    from contextlib import contextmanager
    from unittest.mock import Mock
    from app.core import agent_templates
    from app.core.global_events import global_events

    before = database.get_all_profiles()
    changed = Mock()
    monkeypatch.setattr(global_events, "resources_changed", changed)
    if failure == "insert":
        original = agent_templates._subagent_config
        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("install failed")
            return original(*args)

        monkeypatch.setattr(agent_templates, "_subagent_config", fail_second)
    else:
        original_db = database.get_db

        @contextmanager
        def fail_commit():
            with original_db() as conn:
                yield conn
                raise RuntimeError("install failed")

        monkeypatch.setattr(database, "get_db", fail_commit)
    with pytest.raises(RuntimeError, match="install failed"):
        agent_templates.install_starter_team()
    changed.assert_not_called()
    assert database.get_all_profiles() == before
