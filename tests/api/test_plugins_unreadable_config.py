"""The plugins API answers 409 (not 500, not a silent overwrite) when a plugin
settings/registry file exists but cannot be read as a JSON object.

Runs the real ``PluginService`` against a temp Fluid dir behind the plugins
router, like ``McpConfigUnreadable`` does for the MCP routes.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import plugins as plugins_api
from app.api.auth import require_admin, require_auth
from app.core.plugin_service import PluginService

_BAD_BODIES = ["{broken", b"\xff\xfe{}", "[1, 2]"]
_BAD_IDS = ["corrupt-json", "non-utf8", "list"]


def _write_raw(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid_dir = tmp_path / "fluid"
    fluid_dir.mkdir()
    monkeypatch.setattr("app.core.plugin_service._get_fluid_dir", lambda: fluid_dir)
    service = PluginService()
    monkeypatch.setattr(plugins_api, "get_plugin_service", lambda *a, **k: service)
    return service


@pytest.fixture
def client(svc):
    app = FastAPI()
    app.include_router(plugins_api.router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-admin-token"
    return TestClient(app)


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
@pytest.mark.parametrize("action", ["enable", "disable"])
def test_enable_disable_unreadable_settings_is_409(client, svc, body, action):
    _write_raw(svc.SETTINGS_FILE, body)
    before = svc.SETTINGS_FILE.read_bytes()
    resp = client.post(f"/api/v1/plugins/demo@market/{action}")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert str(svc.SETTINGS_FILE) in detail
    assert "left unchanged" in detail
    assert svc.SETTINGS_FILE.read_bytes() == before
    # Reads through the API still work and show nothing enabled.
    state = client.get("/api/v1/plugins/state/enabled")
    assert state.status_code == 200
    assert state.json()["enabled_plugins"] == {}


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_uninstall_with_unreadable_settings_is_409(client, svc, body):
    svc.INSTALLED_FILE.write_text(json.dumps(
        {"version": 2, "plugins": {"demo@market": [{"scope": "user"}]}}
    ))
    _write_raw(svc.SETTINGS_FILE, body)
    before = svc.SETTINGS_FILE.read_bytes()
    resp = client.delete("/api/v1/plugins/demo@market")
    assert resp.status_code == 409, resp.text
    assert svc.SETTINGS_FILE.read_bytes() == before


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_remove_marketplace_with_unreadable_registry_is_409(client, svc, body):
    svc.MARKETPLACES_FILE.write_text(json.dumps(
        {"mkt": {"source": {"source": "git", "url": "u"}, "installLocation": ""}}
    ))
    _write_raw(svc.INSTALLED_FILE, body)
    before = svc.INSTALLED_FILE.read_bytes()
    resp = client.delete("/api/v1/plugins/marketplaces/mkt")
    assert resp.status_code == 409, resp.text
    assert str(svc.INSTALLED_FILE) in resp.json()["detail"]
    assert svc.INSTALLED_FILE.read_bytes() == before


def test_permission_denied_settings_is_409(client, svc):
    _write_raw(svc.SETTINGS_FILE, json.dumps({"enabledPlugins": {"keep@m": True}}))
    svc.SETTINGS_FILE.chmod(0)
    try:
        try:
            svc.SETTINGS_FILE.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("file permissions are not enforced for this user")
        resp = client.post("/api/v1/plugins/demo@market/enable")
        assert resp.status_code == 409, resp.text
        assert str(svc.SETTINGS_FILE) in resp.json()["detail"]
    finally:
        svc.SETTINGS_FILE.chmod(0o600)
    assert json.loads(svc.SETTINGS_FILE.read_text()) == {"enabledPlugins": {"keep@m": True}}


def test_missing_settings_file_is_created_through_the_api(client, svc):
    assert not svc.SETTINGS_FILE.exists()
    resp = client.post("/api/v1/plugins/demo@market/enable")
    assert resp.status_code == 200, resp.text
    assert json.loads(svc.SETTINGS_FILE.read_text()) == {"enabledPlugins": {"demo@market": True}}
