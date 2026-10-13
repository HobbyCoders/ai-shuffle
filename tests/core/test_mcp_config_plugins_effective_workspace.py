"""Relative plugin paths in a profile resolve against the EFFECTIVE workspace.

In Docker the workspace chosen during setup is a runtime override; the start-up
``settings.workspace_dir`` must not be the base for a profile's relative plugin
directory.
"""
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
