"""
Regression tests for the built-in ``browser`` MCP server.

The browser built-in replaced the CLI-flag "Claude in Chrome" integration. It is
now an ordinary spawned STDIO MCP server: the SDK launches
``node /opt/browser-mcp/dist/index.js`` per session and exposes
``mcp__browser__*`` tools. These tests pin the wiring that makes that happen:

  1. The built-in is modelled as a spawnable STDIO server (command "node",
     args=[entry], NO cli_flag).
  2. The mcp_servers builder INCLUDES it (unlike the old CLI-flag server), and
     injects the curated per-session env subset for it (and only it).
  3. build_options_from_profile adds the ``mcp__browser`` tool wildcard and sets
     NO ``--chrome`` extra_arg; a disabled built-in omits the wildcard.
  4. A legacy ``claude-in-chrome`` disable entry keeps the ``browser`` server
     disabled (settings.json key migration).

build_options_from_profile makes one unavoidable DB call (get_security_instructions);
we patch that single boundary and let the rest run against on-disk config.
"""

from unittest.mock import patch, MagicMock

import app.core.query_options as qo
from app.core.builtin_mcp_servers import (
    get_builtin_servers,
    get_browser_server_config,
    BROWSER_SERVER_NAME,
    DEFAULT_BROWSER_MCP_ENTRY,
)
from app.core.mcp_config import _build_mcp_servers_dict, _build_browser_server_env
from app.core.mcp_service import (
    McpServerConfig,
    McpServerType,
    _normalize_disabled_servers,
)


def _profile():
    return {"id": "t", "config": {"model": "opus", "tools_preset": "claude_code"}}


class TestBuiltinShape:
    def test_browser_builtin_is_a_spawnable_stdio_server(self):
        # Force the docker-path branch of _browser_mcp_entry (the packaged path
        # exists) so the args assertion is stable regardless of host layout.
        with patch("app.core.builtin_mcp_servers.os.path.exists", return_value=True):
            b = get_browser_server_config()
        assert b.name == BROWSER_SERVER_NAME == "browser"
        assert b.type == McpServerType.STDIO
        assert b.command == "node"
        assert b.args == [DEFAULT_BROWSER_MCP_ENTRY]
        # No CLI-flag path anymore; it is spawned like any other server.
        assert b.cli_flag is None
        assert b.url is None
        assert b.builtin is True

    def test_entry_point_is_env_overridable(self):
        with patch.dict("os.environ", {"BROWSER_MCP_ENTRY": "/custom/browser.js"}):
            b = get_browser_server_config()
        assert b.args == ["/custom/browser.js"]

    def test_get_builtin_servers_contains_only_browser(self):
        names = [s.name for s in get_builtin_servers()]
        assert names == ["browser"]


class TestMcpServersInclusion:
    def test_browser_builtin_is_spawned_with_curated_env(self):
        """The spawnable browser built-in reaches mcp_servers WITH its env subset."""
        with patch("app.core.builtin_mcp_servers.os.path.exists", return_value=True):
            builtin = get_browser_server_config()
        normal = McpServerConfig(
            name="normal", type=McpServerType.STDIO, command="echo", args=["hi"]
        )
        fake_service = MagicMock()
        fake_service.get_all_servers.return_value = {
            "builtin": [builtin],
            "user": [normal],
        }
        tool_env = {
            "ANTHROPIC_BASE_URL": "http://gw.local",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_SMALL_FAST_MODEL": "haiku",
            "CHROME_CDP_PORT": "9221",
            "UNRELATED_SECRET": "nope",
        }
        with patch("app.core.mcp_config.get_mcp_service", return_value=fake_service):
            result = _build_mcp_servers_dict(project_dir=None, tool_env=tool_env) or {}

        assert "browser" in result
        assert result["browser"]["command"] == "node"
        assert result["browser"]["args"] == [DEFAULT_BROWSER_MCP_ENTRY]

        # Curated env: whitelisted keys injected, others excluded.
        env = result["browser"]["env"]
        assert env["ANTHROPIC_BASE_URL"] == "http://gw.local"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "tok"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "haiku"
        assert env["CHROME_CDP_PORT"] == "9221"
        assert "UNRELATED_SECRET" not in env

        # The curated env is applied to the browser server ONLY.
        assert "normal" in result
        assert "env" not in result["normal"]

    def test_curated_env_omits_unset_keys(self):
        env = _build_browser_server_env({"ANTHROPIC_API_KEY": "k"})
        assert env.get("ANTHROPIC_API_KEY") == "k"
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env


class TestBuildOptionsWiring:
    def test_enabled_adds_browser_wildcard_and_no_chrome_flag(self):
        with patch.object(qo, "get_security_instructions", return_value=""):
            opts, _ = qo.build_options_from_profile(profile=_profile())

        extra = opts.extra_args or {}
        # The removed CLI flag must never be set.
        assert "chrome" not in extra
        assert "mcp__browser" in (opts.allowed_tools or [])
        # Spawned as a real server this time.
        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "browser" in servers
        assert servers["browser"]["command"] == "node"

    def test_disabled_omits_browser_wildcard_and_server(self):
        """When the built-in is disabled, neither wildcard nor spawned server appears."""
        disabled_service = MagicMock()
        disabled_service.get_disabled_servers.return_value = ["browser"]
        disabled_builtin = get_browser_server_config()
        disabled_builtin.enabled = False
        disabled_service.get_all_servers.return_value = {
            "builtin": [disabled_builtin],
            "user": [],
        }

        with patch.object(qo, "get_security_instructions", return_value=""), \
             patch("app.core.mcp_service.get_mcp_service", return_value=disabled_service), \
             patch("app.core.mcp_config.get_mcp_service", return_value=disabled_service):
            opts, _ = qo.build_options_from_profile(profile=_profile())

        extra = opts.extra_args or {}
        assert "chrome" not in extra
        assert "mcp__browser" not in (opts.allowed_tools or [])
        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "browser" not in servers


class TestLegacyDisableMigration:
    def test_normalize_maps_legacy_name(self):
        assert _normalize_disabled_servers(["claude-in-chrome"]) == ["browser"]

    def test_normalize_is_idempotent_and_dedups(self):
        # Legacy + new both present must collapse to a single ``browser`` entry.
        assert _normalize_disabled_servers(
            ["other", "claude-in-chrome", "browser"]
        ) == ["other", "browser"]
        # Already-normalized list is unchanged.
        assert _normalize_disabled_servers(["browser", "other"]) == ["browser", "other"]

    def test_legacy_disabled_name_keeps_browser_disabled(self):
        """A settings.json entry for the old name disables the new built-in."""
        # Simulate the normalized disable list the read path produces.
        disabled = set(_normalize_disabled_servers(["claude-in-chrome"]))
        servers = get_builtin_servers()
        for s in servers:
            if s.name in disabled:
                s.enabled = False
        browser = next(s for s in servers if s.name == BROWSER_SERVER_NAME)
        assert browser.enabled is False
        assert "browser" in disabled
