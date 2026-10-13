"""McpService over throwaway config files: scopes, per-project state, bad files.

Every path (user ~/.claude.json, settings.json, project .mcp.json files) lives
in tmp_path; the user's real Claude config is never read or written.
"""

import json

import pytest

from app.core import mcp_service as ms
from app.core.mcp_service import McpScope, McpServerConfig, McpServerType, McpService


@pytest.fixture
def svc(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    s = McpService(project_dir=str(project))
    s.USER_MCP_FILE = tmp_path / "claude.json"
    s.SETTINGS_FILE = tmp_path / "settings.json"
    return s


@pytest.fixture
def user_svc(tmp_path):
    s = McpService()
    s.USER_MCP_FILE = tmp_path / "claude.json"
    s.SETTINGS_FILE = tmp_path / "settings.json"
    return s


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def _read(path):
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Config shapes
# ---------------------------------------------------------------------------


def test_config_round_trip_formats():
    stdio = McpServerConfig(name="s", command="node", args=["a"], env={"K": "V"}, description="d")
    assert stdio.to_mcp_json_format() == {"description": "d", "command": "node", "args": ["a"], "env": {"K": "V"}}
    assert McpServerConfig(name="s").to_mcp_json_format() == {}
    http = McpServerConfig(name="h", type=McpServerType.HTTP, url="https://x", headers={"A": "b"})
    assert http.to_mcp_json_format() == {"type": "http", "url": "https://x", "headers": {"A": "b"}}
    assert http.to_dict()["type"] == "http"
    # String types (not the enum) serialize as-is.
    raw = McpServerConfig(name="r", type="sse", url="u")
    assert raw.to_dict()["type"] == "sse" and raw.to_mcp_json_format()["type"] == "sse"
    # Unknown transport falls back to stdio.
    parsed = McpServerConfig.from_mcp_json("w", {"type": "websocket", "command": "c"}, "local")
    assert parsed.type == McpServerType.STDIO and parsed.scope == "local"


@pytest.mark.parametrize("name,ok", [
    ("good-name_1", True),
    ("", False),
    ("has space", False),
    ("dots.not.allowed", False),
    # Letters from other scripts pass str.isalnum() but cannot appear in a tool name.
    ("café", False),
    ("серверы", False),
    ("٣٤", False),
])
def test_validate_server_name(user_svc, name, ok):
    errors = user_svc.validate_server_config(McpServerConfig(name=name, command="c"))
    assert (errors == []) is ok


def test_validate_transport_requirements(user_svc):
    assert "Command is required for stdio servers" in user_svc.validate_server_config(McpServerConfig(name="a"))
    assert "URL is required for SSE/HTTP servers" in user_svc.validate_server_config(
        McpServerConfig(name="a", type=McpServerType.SSE)
    )
    assert user_svc.validate_server_config(McpServerConfig(name="a", type="http", url="u")) == []


def test_normalize_disabled_servers():
    assert ms._normalize_disabled_servers(["a", "browser", "claude-in-chrome", "a", "b"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# Reading robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", ["null", "[1, 2]", '"text"', "{broken", b"\xff\xfe{}"])
def test_unreadable_or_non_object_files_read_as_empty(user_svc, body):
    """A corrupt ~/.claude.json must not 500 every MCP endpoint."""
    if isinstance(body, bytes):
        user_svc.USER_MCP_FILE.write_bytes(body)
        user_svc.SETTINGS_FILE.write_bytes(body)
    else:
        _write(user_svc.USER_MCP_FILE, body)
        _write(user_svc.SETTINGS_FILE, body)
    assert user_svc.get_disabled_servers() == []
    assert user_svc.get_servers_for_scope(McpScope.USER) == []
    assert user_svc.get_server("x", McpScope.USER) is None


_BAD_BODIES = ["null", "[1, 2]", "{broken", b"\xff\xfe{}"]


def _write_raw(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)


@pytest.mark.parametrize("body", _BAD_BODIES, ids=["null", "list", "broken", "non-utf8"])
@pytest.mark.parametrize("change", [
    "toggle", "add-installed", "remove-installed", "add-user", "update-user", "remove-user",
])
def test_unreadable_claude_json_is_never_overwritten(svc, body, change):
    """R13-11: an unreadable ~/.claude.json read as {} and the next MCP change
    replaced the CLI's whole state file with {"projects": ...}."""
    _write_raw(svc.USER_MCP_FILE, body)
    before = svc.USER_MCP_FILE.read_bytes()
    cfg = McpServerConfig(name="n", command="c")
    act = {
        "toggle": lambda: svc.set_project_server_enabled("n", False),
        "add-installed": lambda: svc.add_project_installed_server("n", cfg),
        "remove-installed": lambda: svc.remove_project_installed_server("n"),
        "add-user": lambda: svc.add_server("n", cfg, McpScope.USER),
        "update-user": lambda: svc.update_server("n", cfg, McpScope.USER),
        "remove-user": lambda: svc.remove_server("n", McpScope.USER),
    }[change]
    with pytest.raises(ms.McpConfigUnreadable) as exc:
        act()
    assert exc.value.status_code == 409
    assert str(svc.USER_MCP_FILE) in exc.value.detail
    assert svc.USER_MCP_FILE.read_bytes() == before
    # Reads still show an empty list rather than failing.
    assert svc.get_project_installed_servers() == []


@pytest.mark.parametrize("body", _BAD_BODIES, ids=["null", "list", "broken", "non-utf8"])
def test_unreadable_settings_and_project_files_are_never_overwritten(svc, body):
    _write_raw(svc.SETTINGS_FILE, body)
    project_file = svc.get_mcp_file_for_scope(McpScope.PROJECT)
    _write_raw(project_file, body)
    settings_before, project_before = svc.SETTINGS_FILE.read_bytes(), project_file.read_bytes()
    with pytest.raises(ms.McpConfigUnreadable):
        svc.set_server_enabled("x", False)
    with pytest.raises(ms.McpConfigUnreadable):
        svc.add_server("n", McpServerConfig(name="n", command="c"), McpScope.PROJECT)
    assert svc.SETTINGS_FILE.read_bytes() == settings_before
    assert project_file.read_bytes() == project_before


def test_missing_claude_json_is_still_created_on_change(svc):
    assert not svc.USER_MCP_FILE.exists()
    assert svc.set_project_server_enabled("n", False) is True
    assert _read(svc.USER_MCP_FILE)["projects"][str(svc.project_dir)]["disabledMcpServers"] == ["n"]


def test_unreadable_claude_json_is_a_409_from_the_api(svc, monkeypatch):
    """The refusal reaches the client as a 409 with the reason, not a bare 500."""
    import asyncio

    from fastapi import HTTPException

    from app.api.common import api_error_handler

    _write_raw(svc.USER_MCP_FILE, "{broken")

    async def run():
        async with api_error_handler("toggle MCP server"):
            svc.set_project_server_enabled("n", False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(run())
    assert exc.value.status_code == 409
    assert "left unchanged" in exc.value.detail


def test_non_object_project_file_reads_as_empty(svc):
    _write(svc.USER_MCP_FILE, "[]")
    assert svc.get_project_disabled_servers() == []
    assert svc.get_project_installed_servers() == []


def test_write_failure_reports_false(user_svc, tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    # Parent "directory" is a regular file -> mkdir fails with an OSError.
    assert user_svc._write_json_file(blocker / "sub" / "x.json", {}) is False


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


def test_scope_files(svc, user_svc):
    assert svc.get_mcp_file_for_scope(McpScope.LOCAL) == svc.project_dir / ".claude" / ".mcp.json"
    assert svc.get_mcp_file_for_scope(McpScope.PROJECT) == svc.project_dir / ".mcp.json"
    assert user_svc.get_mcp_file_for_scope(McpScope.LOCAL) is None
    assert user_svc.get_servers_for_scope(McpScope.PROJECT) == []
    assert "local" not in user_svc.get_all_servers()


def test_user_scope_add_update_remove_preserves_other_keys(user_svc):
    _write(user_svc.USER_MCP_FILE, {"oauthAccount": {"keep": True}})
    user_svc.add_server("one", McpServerConfig(name="ignored", command="c1"))
    with pytest.raises(ValueError, match="already exists"):
        user_svc.add_server("one", McpServerConfig(name="one", command="c1"))
    user_svc.update_server("one", McpServerConfig(name="x", command="c2", args=["--v"]))
    data = _read(user_svc.USER_MCP_FILE)
    assert data["oauthAccount"] == {"keep": True}
    assert data["mcpServers"]["one"] == {"command": "c2", "args": ["--v"]}
    with pytest.raises(ValueError, match="not found"):
        user_svc.update_server("missing", McpServerConfig(name="m", command="c"))
    with pytest.raises(ValueError, match="not found"):
        user_svc.remove_server("missing")
    assert user_svc.remove_server("one") is True
    assert _read(user_svc.USER_MCP_FILE)["mcpServers"] == {}


def test_update_and_remove_without_mcp_servers_key(user_svc):
    with pytest.raises(ValueError, match="not found"):
        user_svc.update_server("x", McpServerConfig(name="x", command="c"))
    _write(user_svc.USER_MCP_FILE, {})
    assert user_svc.remove_server("x") is False


def test_unavailable_scope_raises(user_svc):
    cfg = McpServerConfig(name="x", command="c")
    for call in (user_svc.add_server, user_svc.update_server):
        with pytest.raises(ValueError, match="not available"):
            call("x", cfg, McpScope.LOCAL)
    with pytest.raises(ValueError, match="not available"):
        user_svc.remove_server("x", McpScope.PROJECT)


def test_write_failures_raise_runtime_error(user_svc, monkeypatch):
    user_svc.add_server("a", McpServerConfig(name="a", command="c"))
    monkeypatch.setattr(McpService, "_write_json_file", lambda self, path, data: False)
    with pytest.raises(RuntimeError):
        user_svc.add_server("b", McpServerConfig(name="b", command="c"))
    with pytest.raises(RuntimeError):
        user_svc.update_server("a", McpServerConfig(name="a", command="d"))
    with pytest.raises(RuntimeError):
        user_svc.remove_server("a")


def test_project_scopes_and_all_servers(svc, monkeypatch):
    monkeypatch.setattr(McpService, "get_builtin_servers", lambda self: [])
    svc.add_server("loc", McpServerConfig(name="loc", command="l"), McpScope.LOCAL)
    svc.add_server("prj", McpServerConfig(name="prj", type=McpServerType.HTTP, url="u"), McpScope.PROJECT)
    _write(svc.USER_MCP_FILE, {
        "mcpServers": {"u1": {"command": "x"}, "u2": {"command": "y"}},
        "projects": {str(svc.project_dir): {"disabledMcpServers": ["u2"]}},
    })
    everything = svc.get_all_servers()
    assert [s.name for s in everything["local"]] == ["loc"]
    assert everything["project"][0].type == McpServerType.HTTP
    user = {s.name: s.enabled for s in everything["user"]}
    # With a project context the per-project disable list applies to USER scope.
    assert user == {"u1": True, "u2": False}
    assert everything["installed"] == []


def test_user_scope_uses_global_disable_list_without_project(user_svc, monkeypatch):
    _write(user_svc.USER_MCP_FILE, {"mcpServers": {"u1": {"command": "x"}, "bad": "not-a-dict"}})
    _write(user_svc.SETTINGS_FILE, {"disabledMcpServers": ["u1"]})
    servers = user_svc.get_servers_for_scope(McpScope.USER)
    # The malformed entry is skipped, not fatal.
    assert [(s.name, s.enabled) for s in servers] == [("u1", False)]


# ---------------------------------------------------------------------------
# Per-project enable / installed servers
# ---------------------------------------------------------------------------


def test_project_enable_disable_round_trip(svc, user_svc):
    with pytest.raises(ValueError):
        user_svc.set_project_server_enabled("x", False)
    assert user_svc.get_project_disabled_servers() == []

    assert svc.set_project_server_enabled("x", False)
    assert svc.set_project_server_enabled("x", False)  # idempotent
    assert svc.get_project_disabled_servers() == ["x"]
    assert svc.set_project_server_enabled("x", True)
    assert svc.get_project_disabled_servers() == []
    key = str(svc.project_dir).replace("\\", "/")
    assert _read(svc.USER_MCP_FILE)["projects"][key]["disabledMcpServers"] == []


def test_project_installed_servers(svc, user_svc):
    for call in (
        lambda s: s.add_project_installed_server("a", McpServerConfig(name="a", command="c")),
        lambda s: s.remove_project_installed_server("a"),
    ):
        with pytest.raises(ValueError, match="No project directory"):
            call(user_svc)
    assert user_svc.get_project_installed_servers() == []

    with pytest.raises(ValueError, match="Project not found"):
        svc.remove_project_installed_server("a")

    added = svc.add_project_installed_server("a", McpServerConfig(name="zzz", command="c"))
    assert added.name == "a" and added.scope == "installed"
    with pytest.raises(ValueError, match="already exists"):
        svc.add_project_installed_server("a", McpServerConfig(name="a", command="c"))
    svc.add_project_installed_server("b", McpServerConfig(name="b", command="c"))
    svc.set_project_server_enabled("b", False)

    installed = {s.name: s.enabled for s in svc.get_project_installed_servers()}
    assert installed == {"a": True, "b": False}

    with pytest.raises(ValueError, match="not found"):
        svc.remove_project_installed_server("missing")
    assert svc.remove_project_installed_server("a") is True
    assert [s.name for s in svc.get_project_installed_servers()] == ["b"]


def test_project_installed_write_failures(svc, monkeypatch):
    svc.add_project_installed_server("a", McpServerConfig(name="a", command="c"))
    monkeypatch.setattr(McpService, "_write_json_file", lambda self, path, data: False)
    with pytest.raises(RuntimeError):
        svc.add_project_installed_server("b", McpServerConfig(name="b", command="c"))
    with pytest.raises(RuntimeError):
        svc.remove_project_installed_server("a")


def test_installed_server_parse_errors_are_skipped(svc):
    _write(svc.USER_MCP_FILE, {"projects": {str(svc.project_dir): {"mcpServers": {"ok": {"command": "c"}, "bad": 5}}}})
    assert [s.name for s in svc.get_project_installed_servers()] == ["ok"]


# ---------------------------------------------------------------------------
# Global enable/disable
# ---------------------------------------------------------------------------


def test_set_server_enabled_and_always_on(user_svc):
    assert user_svc.set_server_enabled("browser", False) is True
    assert not user_svc.SETTINGS_FILE.exists()  # always-on: nothing written
    assert user_svc.set_server_enabled("x", False)
    assert user_svc.set_server_enabled("x", False)
    assert user_svc.get_disabled_servers() == ["x"]
    assert user_svc.set_server_enabled("x", True)
    assert user_svc.get_disabled_servers() == []


def test_builtin_servers_reflect_disable_list(user_svc, monkeypatch):
    import app.core.builtin_mcp_servers as bms

    monkeypatch.setattr(bms, "get_builtin_servers", lambda: [
        McpServerConfig(name="browser", command="n", builtin=True),
        McpServerConfig(name="extra", command="n", builtin=True),
    ])
    _write(user_svc.SETTINGS_FILE, {"disabledMcpServers": ["browser", "extra"]})
    states = {s.name: s.enabled for s in user_svc.get_builtin_servers()}
    assert states == {"browser": True, "extra": False}
    names = [s.name for s in user_svc.get_all_servers_with_builtins()]
    assert names[:2] == ["browser", "extra"]


def test_get_mcp_service_is_cached_per_project(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_mcp_services", {})
    a = ms.get_mcp_service(tmp_path)
    assert ms.get_mcp_service(tmp_path) is a
    assert ms.get_mcp_service(None) is not a
