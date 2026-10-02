"""Plugin API responses the web/mobile clients consume, pinned to a shared fixture.

``frontend/src/lib/api/__fixtures__/plugin-responses.json`` holds real payloads
from these routes (marketplace sync, available plugins, plugin details for an
installed and an available plugin, installed plugins and file-based agents).
The frontend's vitest suites feed that file to the plugins store and the
PluginDetails component and assert its keys against the TypeScript interfaces
in ``frontend/src/lib/api/plugins.ts``. This test regenerates the payloads from
the real routes over a real PluginService in tmp_path and requires they match
the fixture, so a backend shape change fails here until the fixture (and so the
frontend types) are updated alongside it.

Regenerate after an intentional change with:
    UPDATE_PLUGIN_CONTRACT_FIXTURE=1 python -m pytest tests/api/test_plugins_frontend_contract.py
"""

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import plugins as plugins_api
from app.api.auth import require_admin, require_auth
from app.core.plugin_service import PluginService

FIXTURE = (Path(__file__).resolve().parents[2]
           / "frontend" / "src" / "lib" / "api" / "__fixtures__" / "plugin-responses.json")
HOME = "/home/user"
FIXED_TIME = "2026-01-02T03:04:05"

# Keys per response; mirrors the TypeScript interfaces in frontend/src/lib/api/plugins.ts.
PLUGIN_INFO_KEYS = {
    "id", "name", "marketplace", "description", "version", "scope", "install_path",
    "installed_at", "enabled", "has_commands", "has_agents", "has_skills", "has_hooks",
    "has_mcp", "connector_type", "format", "trust", "components",
}
EXPECTED_KEYS = {
    "marketplace_sync": {"status", "marketplace_id"},
    "available": {
        "id", "name", "marketplace", "marketplace_path", "description", "has_commands",
        "has_agents", "has_skills", "has_hooks", "has_mcp", "connector_type", "installed",
        "enabled", "format", "components",
    },
    "details": PLUGIN_INFO_KEYS | {"commands", "agents", "skills", "readme"},
    "installed": PLUGIN_INFO_KEYS,
    "file_based_agents": {
        "id", "name", "description", "model", "tools", "prompt", "source_plugin", "file_path",
    },
}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid = tmp_path / "fluid"
    fluid.mkdir()
    for module in ("app.core.plugin_service", "app.core.plugin_models", "app.core.connector_packages"):
        monkeypatch.setattr(f"{module}._get_fluid_dir", lambda: fluid)
    service = PluginService()
    monkeypatch.setattr(service, "_get_marketplace_head_version", lambda _mp: None)
    # Sync is a git pull; the route's response shape is what is under test.
    monkeypatch.setattr(service, "sync_marketplace", lambda _mp: True)

    mp = service.MARKETPLACES_DIR / "mp"
    _write(mp / ".claude-plugin" / "marketplace.json", json.dumps({
        "name": "mp",
        "plugins": [
            {"name": "toolkit", "source": "./plugins/toolkit", "description": "Review helpers"},
            {"name": "extra", "source": "./plugins/extra", "description": "Not installed yet"},
        ],
    }))
    service._write_json_file(service.MARKETPLACES_FILE, {"mp": {
        "source": {"source": "git", "url": "https://example.invalid/mp.git"},
        "installLocation": str(mp),
    }})

    toolkit = mp / "plugins" / "toolkit"
    _write(toolkit / ".claude-plugin" / "plugin.json",
           json.dumps({"name": "toolkit", "version": "1.2.0", "description": "Review helpers"}))
    _write(toolkit / "README.md", "# Toolkit\n\nReview helpers for pull requests.\n")
    _write(toolkit / "commands" / "review.md", "# review\n")
    _write(toolkit / "agents" / "reviewer.md",
           "---\ndescription: Reviews diffs\nmodel: sonnet\ntools: Read, Grep\n---\nYou review diffs.\n")
    _write(toolkit / "skills" / "lint" / "SKILL.md", "---\nname: lint\n---\nLint.\n")
    _write(toolkit / "hooks" / "hooks.json", json.dumps({"hooks": {}}))

    extra = mp / "plugins" / "extra"
    _write(extra / ".claude-plugin" / "plugin.json", json.dumps({"name": "extra", "version": "0.1.0"}))
    _write(extra / "commands" / "hello.md", "# hello\n")

    service.install_plugin("toolkit@mp")
    service.enable_plugin("toolkit@mp")
    return service


@pytest.fixture
def client(svc, monkeypatch):
    app = FastAPI()
    app.include_router(plugins_api.router)
    app.dependency_overrides[require_auth] = lambda: "session"
    app.dependency_overrides[require_admin] = lambda: "admin"
    monkeypatch.setattr(plugins_api, "get_plugin_service", lambda: svc)
    return TestClient(app)


def _normalise(value, tmp: str):
    if isinstance(value, dict):
        return {k: (FIXED_TIME if k == "installed_at" and v else _normalise(v, tmp))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v, tmp) for v in value]
    if isinstance(value, str):
        return value.replace(tmp, HOME)
    return value


def _payloads(client, tmp_path) -> dict:
    def ok(response):
        assert response.status_code == 200, response.text
        return response.json()

    raw = {
        "marketplace_sync": ok(client.post("/api/v1/plugins/marketplaces/mp/sync")),
        "available": ok(client.get("/api/v1/plugins/available?marketplace=mp")),
        "details_installed": ok(client.get("/api/v1/plugins/toolkit@mp")),
        "details_available": ok(client.get("/api/v1/plugins/extra@mp")),
        "installed": ok(client.get("/api/v1/plugins/installed")),
        "file_based_agents": ok(client.get("/api/v1/plugins/agents/file-based")),
    }
    return _normalise(raw, str(tmp_path))


def test_response_keys_match_the_frontend_types(client, tmp_path):
    p = _payloads(client, tmp_path)
    assert set(p["marketplace_sync"]) == EXPECTED_KEYS["marketplace_sync"]
    assert p["marketplace_sync"] == {"status": "synced", "marketplace_id": "mp"}
    assert {a["id"] for a in p["available"]} == {"toolkit@mp", "extra@mp"}
    for plugin in p["available"]:
        assert set(plugin) == EXPECTED_KEYS["available"]
    for key in ("details_installed", "details_available"):
        assert set(p[key]) == EXPECTED_KEYS["details"]
    # Details carry hook presence only, never a list of hook names.
    assert "hooks" not in p["details_installed"] and p["details_installed"]["has_hooks"] is True
    assert [set(i) for i in p["installed"]] == [EXPECTED_KEYS["installed"]]
    assert [set(a) for a in p["file_based_agents"]] == [EXPECTED_KEYS["file_based_agents"]]
    assert p["file_based_agents"][0]["source_plugin"] == "toolkit@mp"
    # #1610: the shared reader's format and components ride along.
    installed = p["installed"][0]
    assert (installed["format"], installed["trust"]) == ("claude", "marketplace")
    assert {(c["kind"], c["name"], c["support"]) for c in installed["components"]} == {
        ("skill", "lint", "loaded"), ("command", "review", "loaded"), ("agent", "reviewer", "loaded"),
    }
    for component in installed["components"]:
        assert set(component) == {"kind", "name", "support", "reason"}


def test_shared_frontend_fixture_matches_live_responses(client, tmp_path):
    payloads = _payloads(client, tmp_path)
    rendered = json.dumps(payloads, indent="\t", sort_keys=True) + "\n"
    if os.environ.get("UPDATE_PLUGIN_CONTRACT_FIXTURE") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(rendered)
    assert FIXTURE.exists(), f"missing {FIXTURE}; regenerate with UPDATE_PLUGIN_CONTRACT_FIXTURE=1"
    assert json.loads(FIXTURE.read_text()) == payloads, (
        "Plugin API responses changed; update frontend/src/lib/api/plugins.ts, then regenerate "
        "the fixture with UPDATE_PLUGIN_CONTRACT_FIXTURE=1")
