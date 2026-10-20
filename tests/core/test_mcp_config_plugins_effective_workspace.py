"""Relative plugin paths in a profile resolve against the EFFECTIVE workspace.

In Docker the workspace chosen during setup is a runtime override; the start-up
``settings.workspace_dir`` must not be the base for a profile's relative plugin
directory.
"""
import json
import logging
from types import SimpleNamespace

from app.core import config as config_module
from app.core import mcp_config
from app.core import plugin_service


def test_relative_plugin_path_resolves_under_effective_workspace(tmp_path, monkeypatch):
    startup, chosen = tmp_path / "startup-workspace", tmp_path / "chosen-workspace"
    (startup / "my-plugin").mkdir(parents=True)
    (chosen / "my-plugin").mkdir(parents=True)
    monkeypatch.setattr(config_module.settings, "workspace_dir", startup)
    monkeypatch.setitem(config_module._runtime_overrides, "workspace_dir", chosen)
    fake_service = SimpleNamespace(
        INSTALLED_FILE=tmp_path / "missing-installed_plugins.json",
        get_enabled_plugins=lambda: {},
    )
    monkeypatch.setattr(plugin_service, "get_plugin_service", lambda: fake_service)

    plugins = mcp_config._build_plugins_list(["my-plugin"], setting_sources=None)

    assert plugins == [{"type": "local", "path": str(chosen / "my-plugin")}]


def test_authored_plugin_skips_registry_lookup(tmp_path, monkeypatch, caplog):
    fake_service = SimpleNamespace(
        INSTALLED_FILE=tmp_path / "missing-installed_plugins.json",
        get_enabled_plugins=lambda: {"demo@authored": True},
    )
    monkeypatch.setattr(plugin_service, "get_plugin_service", lambda: fake_service)

    with caplog.at_level(logging.DEBUG, logger=mcp_config.__name__):
        plugins = mcp_config._build_plugins_list(None, setting_sources=None)

    assert plugins is None
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert "Skipping authored plugin" in caplog.text


def test_missing_marketplace_plugin_warns(tmp_path, monkeypatch, caplog):
    fake_service = SimpleNamespace(
        INSTALLED_FILE=tmp_path / "missing-installed_plugins.json",
        get_enabled_plugins=lambda: {"x@some-market": True},
    )
    monkeypatch.setattr(plugin_service, "get_plugin_service", lambda: fake_service)

    with caplog.at_level(logging.WARNING, logger=mcp_config.__name__):
        plugins = mcp_config._build_plugins_list(None, setting_sources=None)

    assert plugins is None
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage() == "Plugin not found in installed_plugins.json: x@some-market"
        for record in caplog.records
    )


def test_installed_marketplace_plugin_resolves(tmp_path, monkeypatch, caplog):
    plugin_dir = tmp_path / "marketplace-plugin"
    plugin_dir.mkdir()
    installed_file = tmp_path / "installed_plugins.json"
    installed_file.write_text(json.dumps({
        "plugins": {"x@some-market": [{"installPath": str(plugin_dir)}]},
    }), encoding="utf-8")
    fake_service = SimpleNamespace(
        INSTALLED_FILE=installed_file,
        get_enabled_plugins=lambda: {"x@some-market": True},
    )
    monkeypatch.setattr(plugin_service, "get_plugin_service", lambda: fake_service)

    with caplog.at_level(logging.WARNING, logger=mcp_config.__name__):
        plugins = mcp_config._build_plugins_list(None, setting_sources=None)

    assert plugins == [{"type": "local", "path": str(plugin_dir)}]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
