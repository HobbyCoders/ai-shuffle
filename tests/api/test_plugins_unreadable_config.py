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


def _local_marketplace(svc, name="market", plugins=("demo",)):
    root = svc.MARKETPLACES_DIR / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
        {"name": name, "plugins": [{"name": p, "source": f"./plugins/{p}"} for p in plugins]}
    ))
    for p in plugins:
        src = root / "plugins" / p
        (src / ".claude-plugin").mkdir(parents=True)
        (src / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": p, "version": "1.0.0"}))
        (src / "README.md").write_text(f"# {p}\n\n{p} plugin\n")
    svc.MARKETPLACES_FILE.write_text(json.dumps(
        {name: {"source": {"source": "git", "url": "u"}, "installLocation": str(root)}}
    ))


def test_uninstall_409_leaves_the_plugin_and_a_retry_succeeds(client, svc):
    cache = svc.CACHE_DIR / "market" / "demo" / "1"
    cache.mkdir(parents=True)
    svc.INSTALLED_FILE.write_text(json.dumps(
        {"version": 2, "plugins": {"demo@market": [{"scope": "user", "installPath": str(cache)}]}}
    ))
    installed_before = svc.INSTALLED_FILE.read_bytes()
    svc.SETTINGS_FILE.write_text("{broken")
    assert client.delete("/api/v1/plugins/demo@market").status_code == 409
    assert cache.exists()
    assert svc.INSTALLED_FILE.read_bytes() == installed_before
    svc.SETTINGS_FILE.write_text("{}")
    resp = client.delete("/api/v1/plugins/demo@market")
    assert resp.status_code == 204, resp.text
    assert not cache.exists()


def test_batch_install_surfaces_the_409_and_copies_nothing(client, svc):
    _local_marketplace(svc, plugins=("one", "two"))
    svc.SETTINGS_FILE.write_text("{broken")
    resp = client.post("/api/v1/plugins/install/batch", json={"plugin_ids": ["one@market", "two@market"]})
    assert resp.status_code == 409, resp.text
    assert str(svc.SETTINGS_FILE) in resp.json()["detail"]
    assert not (svc.CACHE_DIR / "market").exists()
    svc.SETTINGS_FILE.unlink()
    resp = client.post("/api/v1/plugins/install/batch", json={"plugin_ids": ["one@market", "two@market"]})
    assert resp.status_code == 201, resp.text
    assert [p["id"] for p in resp.json()] == ["one@market", "two@market"]


def test_batch_install_bad_id_is_400_like_the_single_route(client, svc):
    _local_marketplace(svc, plugins=("one",))
    resp = client.post("/api/v1/plugins/install/batch", json={"plugin_ids": ["one@market", "missing@market"]})
    assert resp.status_code == 400, resp.text
    assert "missing" in resp.json()["detail"]
