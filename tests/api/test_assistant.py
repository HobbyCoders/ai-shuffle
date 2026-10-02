"""Normal owner authorization/CSRF and the public assistant HTTP contract."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import assistant, auth, connectors, sessions
from app.core import assistant_service as service, owner_auth
from app.core.config import settings
from app.db import assistant as store, database
from tests.db.test_assistant_store import assistant_fixture


class LocalListener:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope["shuffle.ingress"] = "local"
        await self.app(scope, receive, send)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "instance_id", "assistant-test")
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(service, "_guard_version", 0)
    app = FastAPI()
    app.include_router(assistant.router)
    app.include_router(connectors.router)
    app.include_router(sessions.router)
    with TestClient(LocalListener(app), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 43210), headers={"Origin": "http://127.0.0.1:8000"}) as value:
        yield value


def test_safe_unconfigured_http_and_stable_open(client):
    state = client.get("/api/v1/assistant")
    assert state.status_code == 200 and state.json()["status"] == "unconfigured"
    assert not state.json()["enabled"]
    assert client.get("/api/v1/assistant/sources").json() == []
    assert client.get("/api/v1/assistant/activity").json() == []
    assert client.post("/api/v1/assistant/open").status_code == 409
    database.create_profile("assistant-agent", "Helper", None, {"prompt": "Retain custom instructions"})
    updated = client.patch("/api/v1/assistant", json={"agent_id": "assistant-agent", "assistant_name": "Overseer", "avatar_id": "robot", "introduction": {"preferred_name": "Ada", "github_username": "", "repository_refs": []}})
    assert updated.status_code == 200
    assert updated.json()["assistant_name"] == "Overseer"
    assert not updated.json()["enabled"]
    first = client.post("/api/v1/assistant/open")
    second = client.post("/api/v1/assistant/open")
    assert first.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]
    # Older clients still call pause/resume; research has no switch, so
    # neither changes anything.
    for path in ("/resume", "/pause"):
        response = client.post("/api/v1/assistant" + path)
        assert response.status_code == 200 and response.json()["paused"] is False


def test_http_home_is_persistent_and_not_an_ordinary_card(client, assistant_fixture):
    sid = assistant_fixture["id"]
    result = client.patch(f"/api/v1/sessions/{sid}/active", json={"is_active": False})
    assert result.status_code == 409
    assert "persistent" in result.json()["detail"]
    assert database.get_session(sid)["is_active"]
    assert sid not in {row["id"] for row in database.get_sessions()}
    ordinary = database.create_session("ordinary-api", "assistant-agent", is_active=True)
    response = client.patch(f"/api/v1/sessions/{ordinary['id']}/active", json={"is_active": False})
    assert response.status_code == 200 and not response.json()["is_active"]


def test_http_source_explicit_optin_and_activity(client, assistant_fixture):
    sources = client.get("/api/v1/assistant/sources")
    assert sources.status_code == 200 and sources.json()[0]["connector_id"] == "conn-mail"
    assert "config" not in sources.json()[0]
    source = client.patch("/api/v1/assistant/sources/conn-mail", json={"enabled": False})
    assert source.status_code == 200 and not source.json()["enabled"]
    assert client.patch("/api/v1/assistant/sources/conn-mail", json={"enabled": True}).json()["enabled"]
    assert client.get("/api/v1/assistant/activity?limit=101").status_code == 422


@pytest.mark.parametrize("body", [
    {"max_runs_per_day": 0}, {"max_concurrent_runs": 4}, {"max_turns": 21},
    {"timeout_seconds": 0}, {"max_cost_per_day": -1}, {"home_session_id": "forged"},
    {"avatar_id": "javascript:alert(1)"}, {"avatar_id": "<svg onload=alert(1)>"},
    {"introduction": {"github_username": "https://github.com/user"}},
    {"introduction": {"repository_refs": ["https://token@github.com/owner/repo"]}},
    {"introduction": {"notes": "\x00"}}, {"enabled": None},
])
def test_unsafe_or_unbounded_configuration_rejected(client, body):
    response = client.patch("/api/v1/assistant", json=body)
    assert response.status_code == 422
    assert service.get_state()["status"] == "unconfigured"


def test_identity_specialist_endpoint_preserves_all_other_config(client, assistant_fixture):
    before = database.get_profile("assistant-agent")
    response = client.patch("/api/v1/assistant/agents/assistant-agent/identity", json={"name": "New name", "avatar_id": "spark"})
    assert response.status_code == 200 and response.json()["avatar_id"] == "spark"
    assert next(a for a in client.get("/api/v1/assistant/agents").json() if a["id"] == "assistant-agent")["avatar_id"] == "spark"
    after = database.get_profile("assistant-agent")
    assert after["mcp_tools"] == before["mcp_tools"]
    assert {k: v for k, v in after["config"].items() if k != "avatar_id"} == before["config"]
    assert client.patch("/api/v1/assistant/agents/assistant-agent/identity", json={"avatar_id": "https://host/avatar.svg"}).status_code == 422



def test_identity_endpoint_rejects_unknown_agents_and_blank_names(client, assistant_fixture):
    missing = client.patch("/api/v1/assistant/agents/no-such-agent/identity", json={"name": "Name"})
    assert missing.status_code == 404
    assert client.patch("/api/v1/assistant/agents/assistant-agent/identity", json={"name": "   "}).status_code == 422
    assert database.get_profile("assistant-agent")["name"] == "Helper"
    renamed = client.patch("/api/v1/assistant/agents/assistant-agent/identity", json={"name": "  Trimmed  "})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Trimmed"


@pytest.mark.parametrize("home_model,kept", [
    (None, None), ("old-model", None), ("prov:old-provider:m", None), ("prov:new-provider:m", "prov:new-provider:m"),
])
def test_agent_switch_drops_the_old_agents_model(client, assistant_fixture, home_model, kept):
    """The next home turn must not send the old provider's model to the new one."""
    home_id = assistant_fixture["id"]
    database.update_session(session_id=home_id, launched_model="old-model")
    if home_model:
        database.set_session_run_binding(home_id, model=home_model)
    assert database.get_session(home_id)["model"] == home_model
    database.create_profile("specialist", "Specialist", None, {"provider": "new-provider"})
    assert client.patch("/api/v1/assistant", json={"agent_id": "specialist"}).status_code == 200
    home = database.get_session(home_id)
    assert (home["profile_id"], home["provider_id"]) == ("specialist", "new-provider")
    assert home["model"] == kept and home["launched_model"] is None

def test_owner_only_reads_writes_and_csrf(client, assistant_fixture):
    database.create_api_user("foreign", "Limited user", auth.hash_api_key("limited-key"))
    for path in ("", "/sources", "/activity"):
        assert client.get("/api/v1/assistant" + path, headers={"Authorization": "Bearer limited-key"}).status_code == 403
    assert client.patch("/api/v1/assistant", json={"enabled": False}, headers={"Authorization": "Bearer limited-key"}).status_code == 403
    assert client.post("/api/v1/assistant/pause", headers={"Origin": "http://evil.example"}).status_code == 403
    client.headers.pop("Origin")
    assert client.post("/api/v1/assistant/pause").status_code == 403
    assert service.get_state()["agent_id"] == "assistant-agent" and not service.get_state()["paused"]


def test_invalid_config_does_not_partially_change_identity(client, assistant_fixture):
    response = client.patch("/api/v1/assistant", json={"assistant_name": "Must not stick", "agent_id": "no-such-agent"})
    assert response.status_code == 404
    assert database.get_profile("assistant-agent")["name"] == "Helper"
    assert service.get_state()["agent_id"] == "assistant-agent"


def test_minimal_setup_creates_a_dedicated_agent_on_the_text_generation_model(client):
    database.set_system_setting("text_generation_model", "prov:gen-provider:gen-model")
    database.set_system_setting("text_generation_effort", "low")
    created = client.patch("/api/v1/assistant", json={"create_agent": True})
    assert created.status_code == 200
    state = created.json()
    assert state["configured"] and state["assistant_name"] == "Assistant"
    assert state["avatar_id"] in service.AVATAR_PRESETS and state["agent"]["dedicated"]
    agent = database.get_profile(state["agent_id"])
    assert not agent["is_builtin"]
    assert {k: agent["config"][k] for k in ("model", "provider", "effort", "tool_selection_mode")} == {
        "model": "gen-model", "provider": "gen-provider", "effort": "low", "tool_selection_mode": "all"}
    # A built-in guard does not apply: identity edits work on the new agent.
    renamed = client.patch("/api/v1/assistant", json={"assistant_name": "Juniper", "avatar_id": "owl"})
    assert renamed.status_code == 200 and renamed.json()["assistant_name"] == "Juniper"
    assert renamed.json()["avatar_id"] == "owl"
    # Asking again reuses the same agent rather than making a second one.
    again = client.patch("/api/v1/assistant", json={"create_agent": True, "assistant_name": "Juniper"})
    assert again.json()["agent_id"] == state["agent_id"]
    assert sum(1 for a in client.get("/api/v1/assistant/agents").json() if a["dedicated"]) == 1
    assert client.post("/api/v1/assistant/open").status_code == 200
    assert client.patch("/api/v1/assistant", json={"create_agent": True, "agent_id": state["agent_id"]}).status_code == 409


def test_new_agent_takes_the_chosen_name_and_avatar(client):
    created = client.patch("/api/v1/assistant", json={"create_agent": True, "assistant_name": "Pip", "avatar_id": "fox"})
    assert created.status_code == 200
    agent = database.get_profile(created.json()["agent_id"])
    assert agent["name"] == "Pip" and agent["config"]["avatar_id"] == "fox"
    assert agent["id"] == "pip"


def test_switches_and_limits_are_accepted_but_ignored(client, assistant_fixture, monkeypatch):
    monkeypatch.setattr(service, "_guard_version", 1)
    response = client.patch("/api/v1/assistant", json={
        "enabled": False, "paused": True,
        "max_runs_per_day": 100, "max_concurrent_runs": 3, "max_turns": 20, "timeout_seconds": 900,
        "max_cost_per_run": 50, "max_cost_per_day": 900})
    assert response.status_code == 200
    state = response.json()
    assert state["status"] == "ready" and state["enabled"] and not state["paused"]
    # The limits stay server-side: not in the response, and admission still
    # uses the fixed values whatever an older client sent.
    for body in (state, client.get("/api/v1/assistant").json()):
        assert not set(store.LIMIT_KEYS) & set(body)
        assert "usage" not in body and "cost_accounting" not in body
    assert store.research_limits(store.get_state(service.assistant_id())) == store.RESEARCH_LIMITS


def test_activity_rows_leave_out_research_limits(client, assistant_fixture, monkeypatch):
    from tests.db.test_assistant_store import events
    monkeypatch.setattr(service, "_guard_version", 1)
    events()
    run = store.admit_run(service.assistant_id())
    assert run["limits"]["max_turns"] == store.RESEARCH_LIMITS["max_turns"]  # Kept server-side.
    rows = client.get("/api/v1/assistant/activity").json()
    assert [row["id"] for row in rows] == [run["id"]]
    assert not {"limits", "deadline_at", "brief"} & set(rows[0])


def test_owner_corrects_the_detected_introduction_and_can_restore_it(client, assistant_fixture):
    service.apply_detection({"preferred_name": "ada", "email": "ada@example.com"}, {"gmail": True, "github": False})
    response = client.patch("/api/v1/assistant", json={"introduction": {"preferred_name": "Ada Lovelace", "github_username": "ada-gh"}})
    assert response.status_code == 200
    state = response.json()
    assert state["introduction"] == {"preferred_name": "Ada Lovelace", "email": "ada@example.com", "github_username": "ada-gh"}
    assert state["introduction_detected"] == {"preferred_name": "ada", "email": "ada@example.com"}
    assert state["introduction_corrected"] == ["github_username", "preferred_name"]
    restored = client.patch("/api/v1/assistant", json={"introduction": {"preferred_name": "", "github_username": ""}}).json()
    assert restored["introduction"] == {"preferred_name": "ada", "email": "ada@example.com"}
    assert restored["introduction_corrected"] == []
    assert client.patch("/api/v1/assistant", json={"introduction": {"email": "not an address"}}).status_code == 422
    assert client.patch("/api/v1/assistant", json={"introduction": {"projects": ["typed"]}}).status_code == 422


def test_opening_the_page_runs_a_fresh_detection_pass(client, assistant_fixture, monkeypatch):
    from app.core import assistant_introduction as introductions
    from app.core.assistant_manager import assistant_manager
    monkeypatch.setattr(assistant_manager, "_detect_task", None)
    monkeypatch.setattr(assistant_manager, "_detected_at", None)
    monkeypatch.setattr(introductions, "github_login", lambda unknown=None: "ada-gh")
    assert "github_username" not in client.get("/api/v1/assistant").json()["introduction"]
    state = client.get("/api/v1/assistant?detect=true").json()
    assert state["introduction"]["github_username"] == "ada-gh"
    assert state["suggested_connections"] == []


def test_research_turns_on_by_itself_with_an_opted_in_source_and_the_guard(client, assistant_fixture, monkeypatch):
    # An install that had research switched off before this change.
    store.configure(service.assistant_id(), {"enabled": False, "paused": True})
    state = client.get("/api/v1/assistant").json()
    assert state["status"] == "guard_unavailable" and not state["enabled"]
    monkeypatch.setattr(service, "_guard_version", 1)
    state = client.get("/api/v1/assistant").json()
    assert state["status"] == "ready" and state["enabled"] and not state["paused"]
    assert state["research_sources"] == 1
    client.patch("/api/v1/assistant/sources/conn-mail", json={"enabled": False})
    state = client.get("/api/v1/assistant").json()
    assert state["status"] == "no_sources" and not state["enabled"] and state["research_sources"] == 0



def test_normal_connector_api_dedicated_credential_is_discoverable(client, monkeypatch):
    from app.core import connector_packages
    monkeypatch.setattr(connector_packages, "require_installed", lambda _: "test-runtime")
    created = client.post("/api/v1/connectors", json={"name": "New owner mailbox", "type": "gmail", "config": {}, "events": []})
    assert created.status_code == 201
    connector = created.json()
    assert connector["api_user_id"] is not None
    credential = database.get_api_user(connector["api_user_id"])
    assert not credential["web_login_allowed"]
    assert credential["description"] == "Auto-created API user for connector " + connector["id"]
    sources = client.get("/api/v1/assistant/sources").json()
    source = next(s for s in sources if s["id"] == connector["id"])
    assert not source["enabled"] and not source["available"]
    assert "api_user_id" not in source and "config" not in source
    # Creation alone is NOT authorization or a successful OAuth connection.
    assert client.patch("/api/v1/assistant/sources/" + connector["id"], json={"enabled": True}).status_code == 409
