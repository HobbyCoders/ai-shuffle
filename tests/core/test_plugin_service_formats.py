"""#1610: PluginService reads plugins through fluid's shared reader.

Claude Code and Agent Plugins / OpenAI packages land in one PluginInfo shape
with format, trust and per-component support; existing plugin IDs, installed
records, enabled state and connector runtime IDs are left untouched.
"""

import json
from pathlib import Path

import pytest

from app.core.plugin_service import PluginService


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid_dir = tmp_path / "fluid"
    fluid_dir.mkdir()
    for module in ("app.core.plugin_service", "app.core.plugin_models", "app.core.connector_packages"):
        monkeypatch.setattr(f"{module}._get_fluid_dir", lambda: fluid_dir)
    service = PluginService()
    monkeypatch.setattr(service, "_get_marketplace_head_version", lambda _mp: None)
    return service


def _agent_plugin(root: Path) -> Path:
    _write(root / "plugin.json", {
        "name": "notes-helper",
        "version": "0.3.0",
        "extensions": {"com.openai": {
            "interface": {"displayName": "Notes Helper", "shortDescription": "Keeps notes"},
        }},
    })
    _write(root / "skills" / "jot" / "SKILL.md", "---\nname: jot\ndescription: writes\n---\n")
    _write(root / "mcp.json", {"mcpServers": {"notes": {"command": "python"}}})
    _write(root / ".app.json", {"apps": {"drive": {}}})
    return root


def _register(svc: PluginService, plugin_id: str, install_path: Path, version: str) -> dict:
    data = {"version": 2, "plugins": {plugin_id: [{
        "scope": "user", "installPath": str(install_path), "version": version,
        "installedAt": "2026-01-02T03:04:05Z",
    }]}}
    svc._write_json_file(svc.INSTALLED_FILE, data)
    return data


def test_installed_agent_plugin_lists_its_format_and_components(svc):
    root = _agent_plugin(svc.CACHE_DIR / "mp" / "notes-helper" / "0.3.0")
    _register(svc, "notes-helper@mp", root, "0.3.0")

    [plugin] = [p for p in svc.get_installed_plugins() if p.id == "notes-helper@mp"]
    assert plugin.format == "agent-plugins"
    assert plugin.trust == "marketplace"
    assert plugin.description == "Keeps notes"
    assert (plugin.has_skills, plugin.has_mcp, plugin.has_commands) == (True, True, False)
    support = {(c["kind"], c["name"]): c["support"] for c in plugin.components}
    assert support == {
        ("skill", "jot"): "loaded",
        ("mcp_server", "notes"): "loaded",
        ("app", "drive"): "unsupported",
    }

    details = svc.get_plugin_details("notes-helper@mp")
    assert details.skills == ["jot"] and details.format == "agent-plugins"


def test_agents_marketplace_lists_and_installs_its_plugins(svc):
    mp = svc.MARKETPLACES_DIR / "openai-mp"
    _write(mp / ".agents" / "plugins" / "marketplace.json", {
        "name": "openai-mp",
        "plugins": [
            {"name": "notes-helper", "source": {"source": "local", "path": "plugins/notes-helper"}},
            {"name": "npm-only", "source": {"source": "npm", "package": "@acme/x"}},
        ],
    })
    _agent_plugin(mp / "plugins" / "notes-helper")
    svc._write_json_file(svc.MARKETPLACES_FILE, {"openai-mp": {
        "source": {"source": "git", "url": "https://example.invalid/mp.git"},
        "installLocation": str(mp),
    }})

    available = [p for p in svc.get_available_plugins("openai-mp")]
    assert [p.id for p in available] == ["notes-helper@openai-mp"]
    assert available[0].format == "agent-plugins"
    assert available[0].description == "Keeps notes"

    installed = svc.install_plugin("notes-helper@openai-mp")
    assert installed.id == "notes-helper@openai-mp"
    assert (Path(installed.install_path) / "plugin.json").is_file()
    assert installed.version == "0.3.0"


def test_reading_never_rewrites_ids_state_or_connector_records(svc, monkeypatch):
    """Listing plugins is read-only: the registry, enabledPlugins and connector
    runtime records keep their bytes, so account IDs and automations bound to
    ``type@shuffle-connectors`` keep resolving."""
    claude_root = svc.CACHE_DIR / "claude-plugins-official" / "reviewkit" / "1.0.0"
    _write(claude_root / ".claude-plugin" / "plugin.json", {"name": "reviewkit", "description": "Reviews"})
    _write(claude_root / "commands" / "review.md", "review")
    _register(svc, "reviewkit@claude-plugins-official", claude_root, "1.0.0")
    _write(svc.SETTINGS_FILE, {"enabledPlugins": {
        "reviewkit@claude-plugins-official": True, "gmail@shuffle-connectors": True,
    }})

    from app.core import connector_packages
    monkeypatch.setattr(connector_packages, "record", lambda kind: (
        {"enabled": True, "release": "1.0.0", "installed_at": "2026-01-02T03:04:05"}
        if kind == "gmail" else None
    ))
    before = {p: p.read_bytes() for p in (svc.INSTALLED_FILE, svc.SETTINGS_FILE)}

    plugins = {p.id: p for p in svc.get_installed_plugins()}
    svc.get_plugin_details("reviewkit@claude-plugins-official")

    assert {p: p.read_bytes() for p in before} == before
    reviewkit = plugins["reviewkit@claude-plugins-official"]
    assert (reviewkit.enabled, reviewkit.format, reviewkit.description) == (True, "claude", "Reviews")
    connector = plugins["gmail@shuffle-connectors"]
    assert connector.connector_type == "gmail"
    assert (connector.format, connector.trust) == ("shuffle", "official")
    assert connector.components == [
        {"kind": "connector_runtime", "name": "gmail", "support": "loaded", "reason": ""},
    ]


def test_unreadable_plugin_dir_still_lists_with_empty_components(svc):
    missing = svc.CACHE_DIR / "mp" / "gone" / "1.0.0"
    _register(svc, "gone@mp", missing, "1.0.0")
    [plugin] = [p for p in svc.get_installed_plugins() if p.id == "gone@mp"]
    assert (plugin.format, plugin.components, plugin.has_skills) == (None, [], False)


def test_project_plugins_read_either_format(svc, tmp_path):
    project = tmp_path / "proj"
    service = PluginService(project_dir=project)
    _agent_plugin(project / "plugins" / "notes-helper")
    [plugin] = service.get_project_installed_plugins()
    assert plugin.id == "notes-helper@project-project"
    assert (plugin.version, plugin.format, plugin.has_mcp) == ("0.3.0", "agent-plugins", True)
