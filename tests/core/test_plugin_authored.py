"""#1610: agent-authored plugins in the app's plugin list, and the owner's approval.

A draft saved by fluid's save_plugin shows up at once as a pending, disabled
plugin. Enabling it approves the exact content hash the owner reviewed; the
fluid loader then agrees it is approved. A later version is pending again.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import plugins as plugins_api
from app.api.auth import require_admin, require_auth
from app.core.plugin_service import PluginService
from fluid.context.plugin_authoring import is_approved, save_authored_plugin

SPEC = {
    "name": "release-notes",
    "version": "0.1.0",
    "description": "Drafts release notes",
    "skills": [{"name": "draft-notes", "description": "Writes notes", "body": "Read the diff."}],
    "mcp_servers": {"changelog": {"command": "python", "args": ["srv.py"]}},
}
PLUGIN_ID = "release-notes@authored"


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid = tmp_path / "fluid"
    fluid.mkdir()
    for module in ("app.core.plugin_service", "app.core.plugin_models", "app.core.connector_packages"):
        monkeypatch.setattr(f"{module}._get_fluid_dir", lambda: fluid)
    return PluginService()


def _save(svc, **overrides):
    return save_authored_plugin({**SPEC, **overrides}, home=svc.PLUGINS_DIR)


def _listed(svc):
    return next(p for p in svc.get_installed_plugins() if p.id == PLUGIN_ID)


def test_a_new_draft_is_listed_pending_and_off(svc):
    saved = _save(svc)
    plugin = _listed(svc)
    assert (plugin.trust, plugin.approval, plugin.enabled) == ("authored", "pending", False)
    assert plugin.version == "0.1.0" and plugin.content_hash == saved.content_hash
    assert plugin.format == "agent-plugins" and plugin.has_skills and plugin.has_mcp
    details = svc.get_plugin_details(PLUGIN_ID)
    assert details.skills == ["draft-notes"]
    assert {(c["kind"], c["name"]) for c in details.components} == {
        ("skill", "draft-notes"), ("mcp_server", "changelog"),
    }


def test_enabled_plugins_entry_alone_does_not_turn_a_draft_on(svc):
    _save(svc)
    svc.SETTINGS_FILE.write_text(json.dumps({"enabledPlugins": {PLUGIN_ID: True}}), encoding="utf-8")
    assert _listed(svc).enabled is False


def test_enable_needs_the_reviewed_hash(svc):
    saved = _save(svc)
    with pytest.raises(ValueError, match="Review the current version"):
        svc.enable_plugin(PLUGIN_ID)
    with pytest.raises(ValueError):
        svc.enable_plugin(PLUGIN_ID, content_hash="sha256:" + "0" * 64)
    assert not is_approved("release-notes", saved.path, home=svc.PLUGINS_DIR)

    assert svc.enable_plugin(PLUGIN_ID, content_hash=saved.content_hash)
    plugin = _listed(svc)
    assert (plugin.approval, plugin.enabled) == ("approved", True)
    # The fluid loader reads the same approval record.
    assert is_approved("release-notes", saved.path, home=svc.PLUGINS_DIR)
    record = json.loads((svc.PLUGINS_DIR / "authored_approvals.json").read_text())
    assert record["approvals"][PLUGIN_ID]["hash"] == saved.content_hash


def test_a_new_version_after_approval_is_pending_again(svc):
    saved = _save(svc)
    svc.enable_plugin(PLUGIN_ID, content_hash=saved.content_hash)
    newer = _save(svc, version="0.2.0", description="Newer")
    plugin = _listed(svc)
    assert (plugin.version, plugin.approval, plugin.enabled) == ("0.2.0", "pending", False)
    assert not is_approved("release-notes", newer.path, home=svc.PLUGINS_DIR)


def test_disable_drops_the_approval_and_uninstall_removes_everything(svc):
    saved = _save(svc)
    svc.enable_plugin(PLUGIN_ID, content_hash=saved.content_hash)
    assert svc.disable_plugin(PLUGIN_ID)
    assert _listed(svc).approval == "pending"

    svc.enable_plugin(PLUGIN_ID, content_hash=saved.content_hash)
    assert svc.uninstall_plugin(PLUGIN_ID)
    assert not saved.path.parent.exists()
    assert all(p.id != PLUGIN_ID for p in svc.get_installed_plugins())
    settings = json.loads(svc.SETTINGS_FILE.read_text())
    assert PLUGIN_ID not in settings.get("enabledPlugins", {})
    approvals = json.loads((svc.PLUGINS_DIR / "authored_approvals.json").read_text())
    assert PLUGIN_ID not in approvals["approvals"]


def test_uninstall_refuses_names_outside_the_authored_folder(svc):
    for bad in ("..@authored", "../x@authored", ".authored-staging@authored"):
        with pytest.raises(ValueError):
            svc.uninstall_plugin(bad)


@pytest.mark.parametrize("name", ["authored", "Authored"])
def test_a_marketplace_cannot_take_the_authored_source_name(svc, name):
    # Its plugins would land in the drafts folder and be read as drafts.
    with pytest.raises(ValueError, match="reserved"):
        svc.add_marketplace("https://github.com/example/plugins.git", name=name)
    with pytest.raises(ValueError, match="reserved"):
        svc.add_marketplace(f"https://github.com/example/{name}.git")
    assert not (svc.MARKETPLACES_DIR / name).exists()


def test_enable_route_passes_the_reviewed_hash_and_refuses_stale_reviews(svc, monkeypatch):
    saved = _save(svc)
    app = FastAPI()
    app.include_router(plugins_api.router)
    app.dependency_overrides[require_auth] = lambda: "session"
    app.dependency_overrides[require_admin] = lambda: "admin"
    monkeypatch.setattr(plugins_api, "get_plugin_service", lambda: svc)
    client = TestClient(app)

    listed = next(p for p in client.get("/api/v1/plugins/installed").json() if p["id"] == PLUGIN_ID)
    assert (listed["approval"], listed["content_hash"]) == ("pending", saved.content_hash)

    stale = client.post(f"/api/v1/plugins/{PLUGIN_ID}/enable")
    assert stale.status_code == 409
    ok = client.post(f"/api/v1/plugins/{PLUGIN_ID}/enable", json={"content_hash": saved.content_hash})
    assert ok.status_code == 200, ok.text
    details = client.get(f"/api/v1/plugins/{PLUGIN_ID}").json()
    assert (details["approval"], details["enabled"]) == ("approved", True)
