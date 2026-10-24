"""Normal owner authorization/CSRF and the routes kept under ``/api/v1/assistant``.

The personal assistant's own routes (state, setup, open, pause/resume,
activity and the default worker) were removed in #371 step 5.
"""
from __future__ import annotations
from typing import Any
from pytest import MonkeyPatch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import agents, assistant, auth, connectors, sessions
from app.core import assistant_service as service
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


@pytest.mark.parametrize("method,path", [
    ("get", ""), ("patch", ""), ("put", ""), ("post", "/open"), ("post", "/pause"), ("post", "/resume"),
    ("get", "/activity"), ("get", "/worker"), ("patch", "/worker"), ("put", "/worker"),
])
def test_retired_assistant_routes_are_gone(client, method, path):
    response = getattr(client, method)("/api/v1/assistant" + path, **({} if method == "get" else {"json": {}}))
    assert response.status_code in (404, 405), (method, path, response.status_code)


def test_retired_source_api_does_not_reenable_research(client: Any, assistant_fixture: Any) -> None:
    assert client.get("/api/v1/assistant/sources").status_code == 404
    assert (
        client.patch("/api/v1/assistant/sources/conn-mail", json={"enabled": True}).status_code
        == 404
    )


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


def test_owner_only_reads_writes_and_csrf(client: Any, assistant_fixture: Any) -> None:
    database.create_api_user("foreign", "Limited user", auth.hash_api_key("limited-key"))
    key = {"Authorization": "Bearer limited-key"}
    for path in ("/agents", "/limits"):
        assert client.get("/api/v1/assistant" + path, headers=key).status_code == 403
    path = "/api/v1/assistant/agents/assistant-agent/identity"
    assert client.patch(path, json={"name": "No"}, headers=key).status_code == 403
    assert (
        client.patch(
            path, json={"name": "No"}, headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )
    client.headers.pop("Origin")
    assert client.patch(path, json={"name": "No"}).status_code == 403
    assert database.get_profile("assistant-agent")["name"] == "Helper"


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


def test_normal_connector_api_dedicated_credential_is_discoverable(
    client: Any, monkeypatch: MonkeyPatch
) -> None:
    from app.core import connector_packages
    monkeypatch.setattr(connector_packages, "require_installed", lambda _: "test-runtime")
    created = client.post("/api/v1/connectors", json={"name": "New owner mailbox", "type": "gmail", "config": {}, "events": []})
    assert created.status_code == 201
    connector = created.json()
    assert connector["api_user_id"] is not None
    credential = database.get_api_user(connector["api_user_id"])
    assert not credential["web_login_allowed"]
    assert credential["description"] == "Auto-created API user for connector " + connector["id"]
    # Creation alone never authorizes recurring observation. The retired
    # source opt-in route cannot create a watch.
    assert (
        client.patch(
            "/api/v1/assistant/sources/" + connector["id"], json={"enabled": True}
        ).status_code
        == 404
    )
    from app.db.watches import list_watches

    assert list_watches() == []


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


def test_limits_are_owner_only(client, assistant_fixture):
    database.create_api_user("foreign", "Limited user", auth.hash_api_key("limited-key"))
    key = {"Authorization": "Bearer limited-key"}
    assert client.get("/api/v1/assistant/limits", headers=key).status_code == 403
    assert client.patch("/api/v1/assistant/limits", json={"daily_spend_limit_usd": 999}, headers=key).status_code == 403
    assert client.patch("/api/v1/assistant/limits", json={"daily_spend_limit_usd": 999},
                        headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/v1/assistant/limits").json()["daily_spend_limit_usd"] == 20
