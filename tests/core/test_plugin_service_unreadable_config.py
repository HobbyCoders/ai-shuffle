"""An unreadable plugin settings/registry file is never overwritten.

``PluginService`` used to read a corrupt, non-UTF-8, non-object or
permission-denied ``settings.json`` / ``installed_plugins.json`` /
``known_marketplaces.json`` as ``{}``, and the next enable/disable/install/
uninstall/marketplace change wrote that back over the whole file. Reads
still tolerate such a file; writes refuse with a 409
(:class:`PluginConfigUnreadable`, the plugin sibling of
``McpConfigUnreadable``) and keep a ``.bak`` of bytes that failed to parse.
"""

import json
import os

import pytest

from app.core.mcp_service import McpConfigUnreadable
from app.core.plugin_models import AutoUpdateSettings, PluginScope
from app.core.plugin_service import PluginConfigUnreadable, PluginService

_BAD_BODIES = ["{broken", b"\xff\xfe{}", "[1, 2]", "null"]
_BAD_IDS = ["corrupt-json", "non-utf8", "list", "null"]


def _write_raw(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


def _bak(path):
    return path.with_name(path.name + ".bak")


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid_dir = tmp_path / "fluid"
    fluid_dir.mkdir()
    monkeypatch.setattr("app.core.plugin_service._get_fluid_dir", lambda: fluid_dir)
    project = tmp_path / "project"
    project.mkdir()
    return PluginService(project_dir=project)


@pytest.fixture
def unreadable(tmp_path):
    """Make a file permission-denied; skip where permissions are not enforced."""
    changed = []

    def make(path):
        _write_raw(path, json.dumps({"enabledPlugins": {"keep@m": True}}))
        path.chmod(0)
        changed.append(path)
        try:
            path.read_bytes()
        except PermissionError:
            return
        pytest.skip("file permissions are not enforced for this user")

    yield make
    for path in changed:
        path.chmod(0o600)


def test_error_is_a_409_sibling_of_the_mcp_error(tmp_path):
    err = PluginConfigUnreadable(tmp_path / "settings.json")
    assert isinstance(err, McpConfigUnreadable)
    assert err.status_code == 409
    assert err.detail == McpConfigUnreadable(tmp_path / "settings.json").detail


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
@pytest.mark.parametrize("change", ["enable", "disable"])
def test_user_settings_are_never_overwritten(svc, body, change):
    _write_raw(svc.SETTINGS_FILE, body)
    before = svc.SETTINGS_FILE.read_bytes()
    act = svc.enable_plugin if change == "enable" else svc.disable_plugin
    with pytest.raises(PluginConfigUnreadable) as exc:
        act("demo@market")
    assert exc.value.status_code == 409
    assert str(svc.SETTINGS_FILE) in exc.value.detail
    assert svc.SETTINGS_FILE.read_bytes() == before
    # Bytes that failed to parse are kept for the owner, and named.
    assert _bak(svc.SETTINGS_FILE).read_bytes() == before
    assert str(_bak(svc.SETTINGS_FILE)) in exc.value.detail
    # Reads still show empty rather than failing.
    assert svc.get_enabled_plugins() == {}
    assert svc.get_installed_plugins() == []


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
@pytest.mark.parametrize("scope_marker", ["project-local", "project-project"])
def test_project_settings_are_never_overwritten(svc, body, scope_marker):
    scope = PluginScope.LOCAL if scope_marker == "project-local" else PluginScope.PROJECT
    settings_file = svc._get_project_settings_file(scope)
    _write_raw(settings_file, body)
    before = settings_file.read_bytes()
    with pytest.raises(PluginConfigUnreadable):
        svc.enable_project_plugin(f"demo@{scope_marker}")
    with pytest.raises(PluginConfigUnreadable):
        svc.disable_project_plugin(f"demo@{scope_marker}")
    assert settings_file.read_bytes() == before
    assert svc.get_project_enabled_plugins() == {}


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_uninstall_does_not_overwrite_unreadable_settings(svc, body):
    """Delegate read-modify-writes (plugin_install) are stopped at the write."""
    svc.INSTALLED_FILE.write_text(json.dumps(
        {"version": 2, "plugins": {"demo@market": [{"scope": "user"}]}}
    ))
    _write_raw(svc.SETTINGS_FILE, body)
    before = svc.SETTINGS_FILE.read_bytes()
    with pytest.raises(PluginConfigUnreadable):
        svc.uninstall_plugin("demo@market")
    assert svc.SETTINGS_FILE.read_bytes() == before


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_marketplace_registry_writes_are_refused(svc, body):
    """remove_marketplace (plugin_marketplace) must not clobber the installed registry,
    and a marketplace write must not clobber a corrupt known_marketplaces.json."""
    svc.MARKETPLACES_FILE.write_text(json.dumps(
        {"mkt": {"source": {"source": "git", "url": "u"}, "installLocation": ""}}
    ))
    _write_raw(svc.INSTALLED_FILE, body)
    installed_before = svc.INSTALLED_FILE.read_bytes()
    with pytest.raises(PluginConfigUnreadable):
        svc.remove_marketplace("mkt")
    assert svc.INSTALLED_FILE.read_bytes() == installed_before

    _write_raw(svc.MARKETPLACES_FILE, body)
    marketplaces_before = svc.MARKETPLACES_FILE.read_bytes()
    assert svc.get_marketplaces() == []
    # The write add_marketplace performs after cloning.
    with pytest.raises(PluginConfigUnreadable):
        svc._write_json_file(svc.MARKETPLACES_FILE, {"new": {}})
    assert svc.MARKETPLACES_FILE.read_bytes() == marketplaces_before


@pytest.mark.parametrize("target", ["settings", "installed"])
def test_permission_denied_is_refused_without_backup(svc, unreadable, target):
    path = svc.SETTINGS_FILE if target == "settings" else svc.INSTALLED_FILE
    unreadable(path)
    # Reads tolerate it.
    assert svc.get_enabled_plugins() == {}
    assert svc.get_installed_plugins() == []
    with pytest.raises(PluginConfigUnreadable) as exc:
        if target == "settings":
            svc.enable_plugin("demo@market")
        else:
            svc._write_json_file(path, {"version": 2, "plugins": {}})
    assert exc.value.status_code == 409
    assert exc.value.backup is None
    assert not _bak(path).exists()
    path.chmod(0o600)
    assert json.loads(path.read_text()) == {"enabledPlugins": {"keep@m": True}}


def test_missing_settings_file_is_still_created(svc):
    assert not svc.SETTINGS_FILE.exists()
    assert svc.enable_plugin("demo@market") is True
    assert json.loads(svc.SETTINGS_FILE.read_text()) == {"enabledPlugins": {"demo@market": True}}
    assert svc.disable_plugin("demo@market") is True
    assert svc.get_enabled_plugins() == {"demo@market": False}
    assert not _bak(svc.SETTINGS_FILE).exists()


def test_missing_project_settings_file_is_still_created(svc):
    settings_file = svc._get_project_settings_file(PluginScope.LOCAL)
    assert not settings_file.exists()
    assert svc.enable_project_plugin("demo@project-local") is True
    assert json.loads(settings_file.read_text()) == {"enabledPlugins": {"demo@project-local": True}}


def test_valid_settings_are_merged_not_replaced(svc):
    svc.SETTINGS_FILE.write_text(json.dumps({"other": 1, "enabledPlugins": {"a@m": True}}))
    assert svc.disable_plugin("b@m") is True
    assert json.loads(svc.SETTINGS_FILE.read_text()) == {
        "other": 1, "enabledPlugins": {"a@m": True, "b@m": False},
    }


@pytest.mark.parametrize("body", _BAD_BODIES, ids=_BAD_IDS)
def test_auto_update_settings_replace_a_corrupt_file_keeping_a_backup(svc, body):
    """The auto-update file is always written whole, so a corrupt one is backed
    up and replaced (else it could never be fixed from the app)."""
    _write_raw(svc.AUTO_UPDATE_FILE, body)
    before = svc.AUTO_UPDATE_FILE.read_bytes()
    assert svc.save_auto_update_settings(AutoUpdateSettings(enabled=True, interval_hours=12)) is True
    assert _bak(svc.AUTO_UPDATE_FILE).read_bytes() == before
    assert json.loads(svc.AUTO_UPDATE_FILE.read_text())["intervalHours"] == 12


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX permissions only")
def test_auto_update_settings_refuse_a_permission_denied_file(svc, unreadable):
    unreadable(svc.AUTO_UPDATE_FILE)
    with pytest.raises(PluginConfigUnreadable):
        svc.save_auto_update_settings(AutoUpdateSettings())
