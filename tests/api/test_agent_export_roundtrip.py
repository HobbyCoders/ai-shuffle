"""Round trips use the real import functions and isolated SQLite DB fixture."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_admin
from app.api.import_export import _export_profile, _import_payload, router
from app.db import database
from app.db.subagents import _agent_to_subagent


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "fixture-admin"
    with TestClient(app) as client:
        yield client


def agent(agent_id="roundtrip"):
    return {
        "id": agent_id, "name": "Round trip", "description": "Fixture",
        "mcp_tools": ["app_config.GetAgents"],
        "config": {
            "system_prompt": {"type": "preset", "preset": "claude_code", "append": "Keep this guidance"},
            "allowed_tools": ["Read"], "skills": ["research"], "memory": "project",
            "mcp_servers": ["fixture"], "max_turns": 17, "effort": "high",
        },
    }


@pytest.mark.parametrize("transport", ["file", "json"])
def test_legacy_all_prefers_full_config_and_restores_once(client, transport):
    original = agent()
    payload = {
        "version": "1.0", "export_type": "all", "exported_at": "2026-09-04T00:00:00Z",
        "profiles": [_export_profile(original).model_dump()],
        "subagents": [_agent_to_subagent(original)],
    }
    if transport == "file":
        response = client.post("/api/v1/export-import/import", files={"file": ("agents.json", json.dumps(payload), "application/json")})
    else:
        response = client.post("/api/v1/export-import/import/json", json=payload)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["profiles_imported"] == 1
    restored = database.get_profile(original["id"])
    assert restored["config"] == original["config"]
    assert restored["mcp_tools"] == original["mcp_tools"]


def test_canonical_export_roundtrip_and_overwrite(client):
    original = agent()
    database.create_profile(original["id"], original["name"], original["description"], original["config"], mcp_tools=original["mcp_tools"])
    payload = client.get("/api/v1/export-import/agents").json()
    assert payload["version"] == "2.0"
    exported = next(item for item in payload["agents"] if item["id"] == original["id"])
    assert exported["config"] == original["config"]
    database.update_profile(original["id"], config={"changed": True}, mcp_tools=[])
    response = client.post("/api/v1/export-import/import/json?overwrite_existing=true", json=payload)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert database.get_profile(original["id"])["config"] == original["config"]
    assert database.get_profile(original["id"])["mcp_tools"] == original["mcp_tools"]


def test_legacy_subagent_retains_extended_fields():
    original = agent()
    result = _import_payload({"subagents": [_agent_to_subagent(original)]}, False)
    assert result.success
    config = database.get_profile(original["id"])["config"]
    for key in ("skills", "memory", "mcp_servers"):
        assert config[key] == original["config"][key]


def test_legacy_overwrite_keeps_fields_an_older_export_lacks():
    original = agent()
    assert _import_payload({"subagents": [_agent_to_subagent(original)]}, False).success
    older = {key: value for key, value in _agent_to_subagent(original).items() if key not in ("skills", "memory", "mcp_servers")}
    older["description"] = "Rewritten"
    result = _import_payload({"version": "1.0", "subagents": [older]}, True)
    assert result.success and result.subagents_updated == 1
    restored = database.get_profile(original["id"])
    assert restored["description"] == "Rewritten"
    for key in ("skills", "memory", "mcp_servers"):
        assert restored["config"][key] == original["config"][key]


def test_reference_to_an_agent_whose_import_failed_is_dropped(monkeypatch):
    first, second = agent("first"), agent("second")
    first["config"]["enabled_agents"] = ["second"]
    real_create = database.create_profile

    def failing_create(profile_id, *args, **kwargs):
        if profile_id == "second":
            raise RuntimeError("synthetic write failure")
        return real_create(profile_id, *args, **kwargs)

    monkeypatch.setattr(database, "create_profile", failing_create)
    result = _import_payload({"profiles": [first, second]}, False)
    assert not result.success
    assert database.get_profile("second") is None
    assert database.get_profile("first")["config"]["enabled_agents"] == []
    assert any("failed to import" in warning for warning in result.warnings)


def test_forward_references_are_preserved():
    first, second = agent("first"), agent("second")
    first["config"]["enabled_agents"] = ["second"]
    result = _import_payload({"profiles": [first, second]}, False)
    assert result.success
    assert database.get_profile("first")["config"]["enabled_agents"] == ["second"]


def test_bad_collection_and_version_are_rejected(client):
    response = client.post("/api/v1/export-import/import", files={"file": ("bad.json", json.dumps({"profiles": "bad"}), "application/json")})
    assert response.status_code == 400
    response = client.post("/api/v1/export-import/import/json", json={"version": "99.0", "export_type": "agents", "exported_at": "now"})
    assert response.status_code == 400
