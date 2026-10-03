"""Plugin tools for chats (app/mcp/plugin_tools.py): find, check, install.

An agent's install never turns anything on. A folder it wrote waits for the
owner's hash-pinned approval; a marketplace plugin lands switched off, marked
``approval: "pending"`` with the session that asked, until the owner enables
it. The install tool is write-risk and refuses sessions that are not the
owner's, sources the owner did not add, and files that would not come over
HTTPS. Nothing here touches the network: every source is a local folder.
"""

import json
from pathlib import Path

import pytest

from app.core.plugin_service import PluginService
from fluid.context.plugin_authoring import is_approved


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fluid = tmp_path / "fluid"
    fluid.mkdir()
    for module in ("app.core.plugin_service", "app.core.plugin_models", "app.core.connector_packages"):
        monkeypatch.setattr(f"{module}._get_fluid_dir", lambda: fluid)
    service = PluginService()
    monkeypatch.setattr(service, "_get_marketplace_head_version", lambda _mp: None)
    monkeypatch.setattr("app.core.plugin_service.get_plugin_service", lambda *a, **k: service)

    mp = service.MARKETPLACES_DIR / "mp"
    _write(mp / ".claude-plugin" / "marketplace.json", json.dumps({
        "name": "mp",
        "plugins": [
            {"name": "toolkit", "source": "./plugins/toolkit", "description": "Review helpers"},
            {"name": "over-ssh", "description": "Fetched over SSH",
             "source": {"source": "url", "url": "git@example.invalid:o/r.git"}},
            {"name": "over-http", "description": "Fetched over plain HTTP",
             "source": {"source": "url", "url": "http://example.invalid/o/r.git"}},
        ],
    }))
    service._write_json_file(service.MARKETPLACES_FILE, {"mp": {
        "source": {"source": "git", "url": "https://example.invalid/mp.git"},
        "installLocation": str(mp),
    }})
    toolkit = mp / "plugins" / "toolkit"
    _write(toolkit / ".claude-plugin" / "plugin.json", json.dumps({
        "name": "toolkit", "version": "1.2.0", "description": "Review helpers",
        "extensions": {"ai.shuffle": {"services": [
            {"name": "cloudflare", "provider": "cloudflare", "access": "read", "purpose": "Read DNS"},
        ]}},
    }))
    _write(toolkit / "agents" / "reviewer.md",
           "---\ndescription: Reviews diffs\nmodel: sonnet\ntools: Read, Grep, TodoWrite\n---\nReview.\n")
    _write(toolkit / "hooks" / "hooks.json", json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
    _write(toolkit / ".mcp.json", json.dumps({"mcpServers": {"notes": {
        "command": "node", "args": ["srv.js"], "env": {"TOKEN": "literal-value-never-shown"}}}}))
    return service


def _tools(cwd: Path, *, is_admin: bool = True) -> dict:
    from app.mcp.inproc import tool
    from app.mcp.plugin_tools import build_plugin_tools

    return {t.name: t for t in build_plugin_tools(
        tool, is_admin=is_admin, cwd=str(cwd), session_id="sess-1", profile_id="prof-1",
    )}


def _out(result: dict) -> str:
    return result["content"][0]["text"]


def _folder(root: Path) -> Path:
    _write(root / "plugin.json", json.dumps({
        "name": "notes-helper", "version": "0.1.0", "description": "Drafts release notes",
        "skills": "./skills/",
        "extensions": {"ai.shuffle": {"services": [
            {"name": "github", "provider": "custom", "url": "https://api.github.com",
             "access": "read", "purpose": "Read merged pull requests"},
        ]}},
    }))
    _write(root / "skills" / "draft" / "SKILL.md",
           "---\nname: draft\ndescription: Draft release notes\n---\n1. Read the diff.\n")
    return root


def _record(svc, plugin_id):
    data = json.loads(svc.INSTALLED_FILE.read_text(encoding="utf-8"))
    return data["plugins"][plugin_id][0]


# ─── a folder the agent wrote ────────────────────────────────────────────────


async def test_check_then_install_a_folder_waits_for_the_owners_approval(svc, tmp_path):
    _folder(tmp_path / "work" / "notes-helper")
    tools = _tools(tmp_path / "work")

    checked = await tools["CheckPlugin"].handler({"path": "notes-helper"})
    text = _out(checked)
    assert not checked.get("isError"), text
    assert text.startswith("OK: notes-helper 0.1.0 is a valid plugin")
    assert "Loads: skill draft" in text
    assert "github (read): Read merged pull requests" in text

    result = await tools["InstallPlugin"].handler({"path": "notes-helper"})
    assert not result.get("isError"), _out(result)
    assert "Installed notes-helper@authored version 0.1.0" in _out(result)
    assert "Approve and enable" in _out(result)

    plugin = next(p for p in svc.get_installed_plugins() if p.id == "notes-helper@authored")
    assert (plugin.approval, plugin.enabled) == ("pending", False)
    root = svc.CACHE_DIR / "authored" / "notes-helper" / "0.1.0"
    assert not is_approved("notes-helper", root, home=svc.PLUGINS_DIR)
    with pytest.raises(ValueError):
        svc.enable_plugin("notes-helper@authored")
    assert svc.enable_plugin("notes-helper@authored", content_hash=plugin.content_hash)
    assert is_approved("notes-helper", root, home=svc.PLUGINS_DIR)


async def test_check_shows_where_a_service_signs_in_and_its_scopes(svc, tmp_path):
    root = _folder(tmp_path / "work" / "notes-helper")
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    manifest["extensions"]["ai.shuffle"]["services"][0]["auth"] = {
        "type": "oauth2", "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token", "scopes": ["read:org", "repo"],
        "pkce": True,
    }
    _write(root / "plugin.json", json.dumps(manifest))
    text = _out(await _tools(tmp_path / "work")["CheckPlugin"].handler({"path": "notes-helper"}))
    assert ("github (read): Read merged pull requests; signs in with OAuth at github.com; "
            "scopes: read:org, repo") in text

    manifest["extensions"]["ai.shuffle"]["services"][0]["auth"]["client_secret"] = "s3cr3t-value"
    _write(root / "plugin.json", json.dumps(manifest))
    checked = await _tools(tmp_path / "work")["CheckPlugin"].handler({"path": "notes-helper"})
    assert checked.get("isError")
    assert "never carries an OAuth client secret" in _out(checked)
    assert "s3cr3t-value" not in _out(checked)


async def test_a_folder_that_fails_the_check_is_not_installed(svc, tmp_path):
    root = _folder(tmp_path / "work" / "notes-helper")
    _write(root / "hooks" / "hooks.json", "{}")
    _write(root / "skills" / "draft" / "SKILL.md",
           "---\nname: draft\ndescription: d\n---\nuse sk-abcdefghijklmnopqrstuvwxyz123456\n")
    tools = _tools(tmp_path / "work")
    checked = await tools["CheckPlugin"].handler({"path": str(root)})
    assert checked.get("isError")
    assert "cannot carry hooks" in _out(checked)
    assert "looks like it contains an API key" in _out(checked)
    assert "sk-abcdef" not in _out(checked)
    result = await tools["InstallPlugin"].handler({"path": str(root)})
    assert result.get("isError")
    assert not (svc.CACHE_DIR / "authored" / "notes-helper").exists()


# ─── a plugin from the owner's sources ───────────────────────────────────────


async def test_find_check_and_install_a_listed_plugin_switched_off_and_pending(svc, tmp_path):
    tools = _tools(tmp_path)
    found = _out(await tools["FindPlugins"].handler({"query": "review"}))
    assert "toolkit@mp (available)" in found

    report = _out(await tools["CheckPlugin"].handler({"plugin_id": "toolkit@mp"}))
    assert "toolkit@mp (available): Review helpers" in report
    assert "model pin removed" in report and "TodoWrite was dropped" in report
    assert "hook Stop" in report and "mcp server notes: node srv.js (env: TOKEN=…)" in report
    assert "literal-value-never-shown" not in report
    assert "- cloudflare" in report
    assert "would refuse" not in report

    result = await tools["InstallPlugin"].handler({"plugin_id": "toolkit@mp"})
    text = _out(result)
    assert not result.get("isError"), text
    assert "switched off and waiting for the owner's review" in text
    assert "literal-value-never-shown" not in text

    plugin = next(p for p in svc.get_installed_plugins() if p.id == "toolkit@mp")
    assert (plugin.enabled, plugin.approval) == (False, "pending")
    assert svc.get_plugin_details("toolkit@mp").approval == "pending"
    record = _record(svc, "toolkit@mp")
    assert record["requestedBy"]["session_id"] == "sess-1"
    assert record["requestedBy"]["profile_id"] == "prof-1"
    assert svc.get_enabled_plugins().get("toolkit@mp") is False

    # Nothing in it loads, hooks included, until the owner enables it.
    from fluid.context.plugins import discover_plugins

    assert discover_plugins(tmp_path, settings={"enabledPlugins": svc.get_enabled_plugins()},
                            home=svc.PLUGINS_DIR) == []
    found = _out(await tools["FindPlugins"].handler({"query": "toolkit"}))
    assert "toolkit@mp (installed, waiting for the owner's review)" in found
    again = await tools["InstallPlugin"].handler({"plugin_id": "toolkit@mp"})
    assert "already installed" in _out(again)


def test_the_owner_enabling_it_clears_the_pending_marker(svc):
    svc.install_plugin("toolkit@mp", "user", enable=False, requested_by={"session_id": "s"})
    # An update while it waits keeps it waiting.
    svc.update_plugin("toolkit@mp")
    assert next(p for p in svc.get_installed_plugins() if p.id == "toolkit@mp").approval == "pending"

    assert svc.enable_plugin("toolkit@mp")
    plugin = next(p for p in svc.get_installed_plugins() if p.id == "toolkit@mp")
    assert (plugin.enabled, plugin.approval) == (True, None)
    assert _record(svc, "toolkit@mp")["requestedBy"] == {"session_id": "s"}


def test_an_owner_install_is_never_marked_pending(svc):
    info = svc.install_plugin("toolkit@mp")
    assert (info.enabled, info.approval) == (True, None)
    assert "approval" not in _record(svc, "toolkit@mp")


def test_a_pending_install_is_not_shared_with_other_devices(svc, monkeypatch):
    from app.network import plugins_sync

    monkeypatch.setattr(plugins_sync, "_service", lambda: svc)
    svc.install_plugin("toolkit@mp", "user", enable=False, requested_by={"session_id": "s"})
    assert "toolkit@mp" not in plugins_sync._installed()
    svc.enable_plugin("toolkit@mp")
    assert plugins_sync._installed()["toolkit@mp"]["enabled"] is True


@pytest.mark.parametrize(("plugin_id", "why"), [
    ("over-ssh@mp", "not over HTTPS"),
    ("over-http@mp", "not over HTTPS"),
    ("toolkit@elsewhere", "not one of this worker's plugin sources"),
    ("missing@mp", "does not list a plugin named"),
    ("notes-helper@authored", "path=<folder>"),
    ("webhook@shuffle-connectors", "official connector runtimes"),
])
async def test_install_refuses_what_the_owner_did_not_offer(svc, tmp_path, plugin_id, why):
    result = await _tools(tmp_path)["InstallPlugin"].handler({"plugin_id": plugin_id})
    assert result.get("isError")
    assert why in _out(result), _out(result)
    assert not any(p.id == plugin_id for p in svc.get_installed_plugins())


async def test_only_the_owners_sessions_can_install(svc, tmp_path):
    root = _folder(tmp_path / "work" / "notes-helper")
    tools = _tools(tmp_path / "work", is_admin=False)
    for args in ({"path": str(root)}, {"plugin_id": "toolkit@mp"}):
        result = await tools["InstallPlugin"].handler(args)
        assert result.get("isError") and "owner" in _out(result)
    assert not (svc.CACHE_DIR / "authored").exists()
    assert not any(p.id == "toolkit@mp" for p in svc.get_installed_plugins())


async def test_each_tool_takes_exactly_one_target(svc, tmp_path):
    tools = _tools(tmp_path)
    for name in ("CheckPlugin", "InstallPlugin"):
        for args in ({}, {"path": "x", "plugin_id": "a@b"}):
            assert (await tools[name].handler(args)).get("isError")


def test_install_is_write_risk_and_the_others_are_read_only():
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_MAP
    from app.mcp.builtin_tools import get_builtin_tool_names

    assert BUILTINS_RISK_MAP["InstallPlugin"] == "write"
    assert BUILTINS_RISK_MAP["CheckPlugin"] == "safe"
    assert BUILTINS_RISK_MAP["FindPlugins"] == "safe"
    assert {"mcp__builtins__FindPlugins", "mcp__builtins__CheckPlugin",
            "mcp__builtins__InstallPlugin"} <= set(get_builtin_tool_names())


def test_the_builtins_server_carries_the_plugin_tools():
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools
    from app.mcp.builtin_tools import create_builtin_tools_server

    server = create_builtin_tools_server(session_id="sess-1", profile_id="prof-1", is_admin=True)
    names = {t.name for t in recorded_sdk_server_tools(server)}
    assert {"FindPlugins", "CheckPlugin", "InstallPlugin"} <= names


def test_settings_reads_the_pending_state_from_the_plugins_api(svc, monkeypatch):
    """What Settings > Plugins asks for: the installed list and the details
    both say ``approval: "pending"`` until the owner enables the plugin."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import plugins as plugins_api
    from app.api.auth import require_admin, require_auth

    app = FastAPI()
    app.include_router(plugins_api.router)
    app.dependency_overrides[require_auth] = lambda: "session"
    app.dependency_overrides[require_admin] = lambda: "admin"
    monkeypatch.setattr(plugins_api, "get_plugin_service", lambda: svc)
    client = TestClient(app)

    svc.install_plugin("toolkit@mp", "user", enable=False, requested_by={"session_id": "s"})
    row = next(p for p in client.get("/api/v1/plugins/installed").json() if p["id"] == "toolkit@mp")
    assert (row["enabled"], row["approval"]) == (False, "pending")
    assert client.get("/api/v1/plugins/toolkit@mp").json()["approval"] == "pending"

    assert client.post("/api/v1/plugins/toolkit@mp/enable").status_code < 300
    row = next(p for p in client.get("/api/v1/plugins/installed").json() if p["id"] == "toolkit@mp")
    assert (row["enabled"], row["approval"]) == (True, None)


def test_the_assistant_asks_before_installing_and_reads_freely(tmp_path):
    """In the owner's assistant sessions: search goes ahead, a check of a
    folder is judged like a read, and an install asks the owner first."""
    from app.core import assistant_permissions as ap

    scope = {"roots": [str(tmp_path / "proj")], "cwd": str(tmp_path / "proj")}
    levels = ap.TOOL_LEVELS
    assert levels["FindPlugins"] == ap.LOW
    assert levels["InstallPlugin"] == ap.HIGH
    rule = levels["CheckPlugin"]
    assert rule({"plugin_id": "toolkit@mp"}, scope) == ap.LOW
    assert rule({"path": str(tmp_path / "proj" / "my-plugin")}, scope) == ap.LOW
    assert rule({"path": str(tmp_path / "elsewhere")}, scope) == ap.HIGH
