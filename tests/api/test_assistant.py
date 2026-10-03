"""Normal owner authorization/CSRF and the public assistant HTTP contract."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import agents, assistant, auth, connectors, sessions
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
    app.include_router(agents.router)
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
    # An older client still names an agent; it is accepted and ignored.
    updated = client.patch("/api/v1/assistant", json={"agent_id": "assistant-agent", "assistant_name": "Overseer", "avatar_id": "robot", "introduction": {"preferred_name": "Ada", "github_username": "", "repository_refs": []}})
    assert updated.status_code == 200
    assert updated.json()["assistant_name"] == "Overseer" and updated.json()["agent_id"] == service.AGENT_ID
    assert not updated.json()["enabled"]
    assert database.get_profile("assistant-agent")["name"] == "Helper"
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


def test_owner_only_reads_writes_and_csrf(client, assistant_fixture):
    database.create_api_user("foreign", "Limited user", auth.hash_api_key("limited-key"))
    for path in ("", "/sources", "/activity"):
        assert client.get("/api/v1/assistant" + path, headers={"Authorization": "Bearer limited-key"}).status_code == 403
    assert client.patch("/api/v1/assistant", json={"enabled": False}, headers={"Authorization": "Bearer limited-key"}).status_code == 403
    assert client.post("/api/v1/assistant/pause", headers={"Origin": "http://evil.example"}).status_code == 403
    client.headers.pop("Origin")
    assert client.post("/api/v1/assistant/pause").status_code == 403
    assert service.get_state()["agent_id"] == service.AGENT_ID and not service.get_state()["paused"]


def test_invalid_config_does_not_partially_change_identity(client, assistant_fixture):
    before = service.get_state()["assistant_name"]
    response = client.patch("/api/v1/assistant", json={"assistant_name": "Must not stick", "introduction": {"project_ids": ["no-such-project"]}})
    assert response.status_code == 409
    assert service.get_state()["assistant_name"] == before
    assert service.get_state()["agent_id"] == service.AGENT_ID


def test_minimal_setup_runs_on_the_builtin_agent_not_the_text_generation_model(client):
    database.set_system_setting("text_generation_model", "prov:gen-provider:gen-model")
    database.set_system_setting("text_generation_effort", "low")
    created = client.patch("/api/v1/assistant", json={"create_agent": True})
    assert created.status_code == 200
    state = created.json()
    assert state["configured"] and state["assistant_name"] == "Assistant"
    assert state["agent_id"] == service.AGENT_ID and state["agent"]["builtin"]
    assert state["avatar_id"] in service.AVATAR_PRESETS
    agent = database.get_profile(service.AGENT_ID)
    assert agent["is_builtin"]
    # Every tool and every subagent; no pinned model, provider or effort.
    assert agent["config"] == {"tool_selection_mode": "all", "agent_invoke_mode": "all"}
    renamed = client.patch("/api/v1/assistant", json={"assistant_name": "Juniper", "avatar_id": "owl"})
    assert renamed.status_code == 200
    assert (renamed.json()["assistant_name"], renamed.json()["avatar_id"]) == ("Juniper", "owl")
    assert database.get_profile(service.AGENT_ID)["name"] == service.AGENT_NAME
    # Asking again keeps the name the owner chose.
    again = client.patch("/api/v1/assistant", json={"create_agent": True})
    assert again.json()["assistant_name"] == "Juniper" and again.json()["agent_id"] == service.AGENT_ID
    # The home exists already and runs on the main chat default, not the text model.
    home = database.get_session(state["home_session_id"])
    assert home["profile_id"] == service.AGENT_ID
    assert (home["model"], home["provider_id"], home["effort"]) == (None, None, None)
    assert client.post("/api/v1/assistant/open").json()["session_id"] == home["id"]


def test_new_assistant_takes_the_chosen_name_and_avatar(client):
    created = client.patch("/api/v1/assistant", json={"create_agent": True, "assistant_name": "Pip", "avatar_id": "fox"})
    assert created.status_code == 200
    assert (created.json()["assistant_name"], created.json()["avatar_id"]) == ("Pip", "fox")
    assert created.json()["agent"]["name"] == "Pip"
    account = store.get_account()
    assert (account["assistant_name"], account["avatar_id"]) == ("Pip", "fox")


def test_builtin_agent_is_hidden_from_agent_lists_and_locked(client, assistant_fixture):
    agent_id = service.AGENT_ID
    assert agent_id not in {row["id"] for row in client.get("/api/v1/agents").json()}
    assert agent_id not in {row["id"] for row in client.get("/api/v1/assistant/agents").json()}
    assert agent_id not in {row["id"] for row in database.get_all_profiles()}
    assert client.get(f"/api/v1/agents/{agent_id}").status_code == 200  # Readable by id.
    assert client.put(f"/api/v1/agents/{agent_id}", json={"name": "Hijacked"}).status_code == 403
    assert client.delete(f"/api/v1/agents/{agent_id}").status_code == 403
    assert client.put(f"/api/v1/agents/{agent_id}/enabled-agents", json={"enabled_agents": []}).status_code == 403
    assert client.patch(f"/api/v1/assistant/agents/{agent_id}/identity", json={"name": "Hijacked"}).status_code == 409
    with pytest.raises(PermissionError):
        database.update_profile(agent_id, name="Hijacked", allow_builtin=True)
    with pytest.raises(PermissionError):
        database.delete_profile(agent_id)
    assert database.get_profile(agent_id)["name"] == service.AGENT_NAME
    # Ordinary agents keep working as before.
    assert client.put("/api/v1/agents/assistant-agent", json={"name": "Renamed"}).status_code == 200


def test_state_is_openable_without_research_sources_and_creates_the_home(client, monkeypatch):
    monkeypatch.setattr(service, "_guard_version", 1)
    client.patch("/api/v1/assistant", json={"assistant_name": "Pip"})
    # Simulate a home lost since setup: the next state read makes a new one.
    store.configure(service.assistant_id(), {"home_session_id": None})
    state = client.get("/api/v1/assistant").json()
    assert state["status"] == "no_sources" and not state["enabled"]
    assert state["openable"] and state["home_session_id"]
    assert database.get_session(state["home_session_id"])["hidden"]
    assert client.post("/api/v1/assistant/open").json()["session_id"] == state["home_session_id"]
    assert client.get("/api/v1/assistant").json()["home_session_id"] == state["home_session_id"]


def test_open_seeds_the_main_chat_model_once_and_keeps_the_owners_pick(client):
    client.patch("/api/v1/assistant", json={"create_agent": True})
    home_id = client.get("/api/v1/assistant").json()["home_session_id"]
    # An unusable model is ignored rather than failing the open.
    assert client.post("/api/v1/assistant/open", json={"model": "prov:no-such-provider:m"}).status_code == 200
    assert client.post("/api/v1/assistant/open", json={"model": "made-up-model"}).status_code == 200
    assert database.get_session(home_id)["model"] is None
    opened = client.post("/api/v1/assistant/open", json={"model": "opus"})
    assert opened.status_code == 200 and opened.json()["session"]["model"] == "opus"
    # Later opens never override it; the composer's pick is what changes it.
    client.post("/api/v1/assistant/open", json={"model": "haiku"})
    assert database.get_session(home_id)["model"] == "opus"
    database.set_session_run_binding(home_id, model="fable")
    client.get("/api/v1/assistant")
    client.post("/api/v1/assistant/open", json={"model": "haiku"})
    assert database.get_session(home_id)["model"] == "fable"
    assert service.home_model() == ("fable", None)
    assert client.post("/api/v1/assistant/open", json={"unexpected": True}).status_code == 422


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


def test_limits_read_update_and_confirmed_off_switch(client, assistant_fixture):
    state = client.get("/api/v1/assistant/limits")
    assert state.status_code == 200
    body = state.json()
    assert body["daily_spend_limit_usd"] == 20 and body["max_job_minutes"] == 120
    assert body["spend_limit_enabled"] and body["job_limit_enabled"] and body["spent_today_usd"] == 0
    updated = client.patch("/api/v1/assistant/limits", json={"daily_spend_limit_usd": 7.5, "max_job_minutes": 45})
    assert updated.status_code == 200
    assert updated.json()["daily_spend_limit_usd"] == 7.5 and updated.json()["max_job_minutes"] == 45
    # Switching the spending limit off needs an explicit confirmation.
    refused = client.patch("/api/v1/assistant/limits", json={"spend_limit_enabled": False})
    assert refused.status_code == 409
    assert client.get("/api/v1/assistant/limits").json()["spend_limit_enabled"]
    off = client.put("/api/v1/assistant/limits", json={"spend_limit_enabled": False, "confirm_spend_limit_off": True})
    assert off.status_code == 200 and not off.json()["spend_limit_enabled"]
    assert off.json()["daily_spend_limit_usd"] == 7.5  # Kept for when it is switched back on.
    assert client.patch("/api/v1/assistant/limits", json={"job_limit_enabled": False}).json()["job_limit_enabled"] is False
    for bad in ({"daily_spend_limit_usd": 0}, {"daily_spend_limit_usd": 1e9}, {"max_job_minutes": 1},
                {"max_job_minutes": 100000}, {"daily_spend_limit_usd": None}, {"reset": True}):
        assert client.patch("/api/v1/assistant/limits", json=bad).status_code == 422, bad
    # The general assistant settings cannot touch the limits.
    assert client.patch("/api/v1/assistant", json={"daily_spend_limit_usd": 1000}).status_code == 422


def test_limits_are_owner_only(client, assistant_fixture):
    database.create_api_user("foreign", "Limited user", auth.hash_api_key("limited-key"))
    key = {"Authorization": "Bearer limited-key"}
    assert client.get("/api/v1/assistant/limits", headers=key).status_code == 403
    assert client.patch("/api/v1/assistant/limits", json={"daily_spend_limit_usd": 999}, headers=key).status_code == 403
    assert client.patch("/api/v1/assistant/limits", json={"daily_spend_limit_usd": 999},
                        headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/v1/assistant/limits").json()["daily_spend_limit_usd"] == 20
