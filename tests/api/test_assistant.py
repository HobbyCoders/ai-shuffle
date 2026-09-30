"""Normal owner authorization/CSRF and the public assistant HTTP contract."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import assistant, auth
from app.core import assistant_service as service, owner_auth
from app.core.config import settings
from app.db import database
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
    assert client.post("/api/v1/assistant/resume").json()["enabled"]
    assert client.post("/api/v1/assistant/pause").json()["paused"]


def test_http_source_explicit_optin_and_activity(client, assistant_fixture):
    sources = client.get("/api/v1/assistant/sources")
    assert sources.status_code == 200 and sources.json()[0]["connector_id"] == "mail"
    assert "config" not in sources.json()[0]
    source = client.patch("/api/v1/assistant/sources/mail", json={"enabled": False})
    assert source.status_code == 200 and not source.json()["enabled"]
    assert client.patch("/api/v1/assistant/sources/mail", json={"enabled": True}).json()["enabled"]
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
    after = database.get_profile("assistant-agent")
    assert after["mcp_tools"] == before["mcp_tools"]
    assert {k: v for k, v in after["config"].items() if k != "avatar_id"} == before["config"]
    assert client.patch("/api/v1/assistant/agents/assistant-agent/identity", json={"avatar_id": "https://host/avatar.svg"}).status_code == 422


def test_owner_only_reads_writes_and_csrf(client, assistant_fixture):
    database.create_api_user("foreign", "Limited user", auth.hash_api_key("limited-key"))
    for path in ("", "/sources", "/activity"):
        assert client.get("/api/v1/assistant" + path, headers={"Authorization": "Bearer limited-key"}).status_code == 403
    assert client.patch("/api/v1/assistant", json={"enabled": False}, headers={"Authorization": "Bearer limited-key"}).status_code == 403
    assert client.post("/api/v1/assistant/pause", headers={"Origin": "http://evil.example"}).status_code == 403
    client.headers.pop("Origin")
    assert client.post("/api/v1/assistant/pause").status_code == 403
    assert service.get_state()["enabled"] and not service.get_state()["paused"]


def test_invalid_config_does_not_partially_change_identity(client, assistant_fixture):
    response = client.patch("/api/v1/assistant", json={"assistant_name": "Must not stick", "introduction": {"project_ids": ["unknown-project"]}})
    assert response.status_code == 409
    assert database.get_profile("assistant-agent")["name"] == "Helper"
