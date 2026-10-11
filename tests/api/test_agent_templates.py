"""Owner-only, opt-in starter team installation over the ordinary agents API."""
import pytest

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
    database.create_profile("general-purpose", "Old built-in", None, {}, is_builtin=True)
    database.create_api_user("retired-key", "Retired scope", "synthetic-template-key", profile_ids=["general-purpose"])
    seed_builtin_subagents()
    before = database.get_profile("owner-gp")
    result = authenticated_client.post(INSTALL).json()
    assert result["agents"][0]["existing_agent_id"] == "owner-gp"
    assert database.get_profile("owner-gp") == before
    assert database.get_api_user("retired-key")["profile_ids"] == ["general-purpose"]


@pytest.mark.parametrize("retired_id", ["general-purpose", "frontend-designer"])
def test_retired_automation_still_loads_and_requires_reassignment(authenticated_client, retired_id):
    database.create_profile(retired_id, "Old built-in", None, {}, is_builtin=True)
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
