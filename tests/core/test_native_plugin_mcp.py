"""Native plugin MCP discovery, app enablement and delegated isolation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.plugin_service import PluginService
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions

_SERVER = """
import sys
from mcp.server.fastmcp import FastMCP
server = FastMCP("plugin-test")
@server.tool(name=sys.argv[1])
def ping() -> str:
    return "pong"
server.run()
"""


def _config(tool: str = "plugin_ping") -> dict[str, Any]:
    return {"command": sys.executable, "args": ["-c", _SERVER, tool]}


def _install(home: Path, name: str, servers: dict[str, Any], source: str = "testmp") -> Path:
    root = home / ".fluid" / "plugins" / "cache" / source / name / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return root


@pytest.fixture
def plugin_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, PluginService]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr("app.core.plugin_service._get_fluid_dir", lambda: home / ".fluid")
    service = PluginService()
    _install(home, "demo", {"demo": _config()})
    # Produce enablement through the app's owner-facing service, not a mock.
    assert service.enable_plugin("demo@testmp")
    return home, service


@pytest.mark.asyncio
async def test_enabled_plugin_reaches_native_catalog(
    plugin_home: tuple[Path, PluginService], tmp_path: Path,
) -> None:
    options = SessionOptions(model="gpt-4o", cwd=str(tmp_path))
    adapter = ha.HarnessAdapter()
    registry = await adapter._build_registry(options, native=True)
    assert registry is not None
    try:
        names = [tool.name for tool in registry.list_tools(loaded_only=False)]
        assert names.count("mcp__demo__plugin_ping") == 1
    finally:
        await registry.aclose()
    assert options.mcp_servers is None  # Discovery never mutates the shared CLI map.


def test_disabled_and_unreviewed_plugins_stay_out(
    plugin_home: tuple[Path, PluginService], tmp_path: Path,
) -> None:
    home, service = plugin_home
    assert service.disable_plugin("demo@testmp")
    _install(home, "draft", {"unreviewed": _config()}, source="authored")
    # Even a stale enabled bit must not bypass the authored content review gate.
    service.SETTINGS_FILE.write_text(json.dumps({
        "enabledPlugins": {"demo@testmp": False, "draft@authored": True},
    }), encoding="utf-8")
    assert ha._native_external_mcp_servers(SessionOptions(model="gpt-4o", cwd=str(tmp_path))) == {}


@pytest.mark.asyncio
async def test_host_wins_collision_in_native_catalog(
    plugin_home: tuple[Path, PluginService], tmp_path: Path,
) -> None:
    host = {"demo": _config("host_ping")}
    options = SessionOptions(model="gpt-4o", cwd=str(tmp_path), mcp_servers=host)
    assert ha._native_external_mcp_servers(options) == host
    registry = await ha.HarnessAdapter()._build_registry(options, native=True)
    assert registry is not None
    try:
        names = {tool.name for tool in registry.list_tools(loaded_only=False)}
        assert "mcp__demo__host_ping" in names
        assert "mcp__demo__plugin_ping" not in names
    finally:
        await registry.aclose()
    assert options.mcp_servers == host


def test_scope_filter_and_sorted_server_map(
    plugin_home: tuple[Path, PluginService], tmp_path: Path,
) -> None:
    home, service = plugin_home
    _install(home, "another", {"zulu": _config(), "alpha": _config()})
    assert service.enable_plugin("another@testmp")
    host = {"omega": _config(), "beta": _config()}
    options = SessionOptions(model="gpt-4o", cwd=str(tmp_path), mcp_servers=host)
    first = ha._native_external_mcp_servers(options)
    options.mcp_servers = dict(reversed(list(host.items())))
    second = ha._native_external_mcp_servers(options)
    assert list(first) == list(second) == ["alpha", "beta", "demo", "omega", "zulu"]
    assert json.dumps(first) == json.dumps(second)
    options.setting_sources = []
    assert list(ha._native_external_mcp_servers(options)) == ["beta", "omega"]
    options.setting_sources = None
    options.mcp_servers = {"demo": {"type": "sdk", "instance": object()}}
    assert "demo" not in ha._native_external_mcp_servers(options)


@pytest.mark.asyncio
async def test_delegated_path_does_not_copy_plugin_servers(
    plugin_home: tuple[Path, PluginService], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class DelegatedStub:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def _ensure_session(self) -> Any:
            from types import SimpleNamespace
            return SimpleNamespace(session_id="ses_plugin_test")

        async def aclose(self) -> None:
            pass

    def no_native_discovery(options: SessionOptions) -> dict[str, Any]:
        pytest.fail("delegated connect must leave plugin MCP loading to the CLI")

    monkeypatch.setattr(ha, "DelegatedLoop", DelegatedStub)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude"])
    monkeypatch.setattr(ha, "_native_external_mcp_servers", no_native_discovery)
    options = SessionOptions(
        model="sonnet", cwd=str(tmp_path), mcp_servers={"host": _config()},
    )
    adapter = ha.HarnessAdapter()
    try:
        await adapter.connect(options)
        assert captured["mcp_servers"] == options.mcp_servers
        registry = adapter._registry
        assert registry is None or not any(
            tool.name.startswith("mcp__demo__")
            for tool in registry.list_tools(loaded_only=False)
        )
    finally:
        await adapter.disconnect()
