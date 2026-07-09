"""
Regression tests for the built-in "Claude in Chrome" browser integration.

Claude in Chrome replaced the Playwright MCP server. Unlike a normal MCP server,
it is NOT spawned as a subprocess — it is enabled by passing ``--chrome`` to the
bundled Claude Code CLI, which then exposes ``mcp__claude-in-chrome__*`` tools.

These tests pin the wiring that makes that happen:
  1. The built-in is modelled as a CLI-flag server (cli_flag="chrome", no command).
  2. The mcp_servers builder excludes CLI-flag built-ins (they have no command).
  3. build_options_from_profile injects ``--chrome`` + the tool wildcard when the
     built-in is enabled, and omits both when it is disabled.

build_options_from_profile makes one unavoidable DB call (get_security_instructions);
we patch that single boundary and let the rest run against on-disk config.
"""

from unittest.mock import patch, MagicMock

import app.core.query_options as qo
from app.core.builtin_mcp_servers import (
    get_builtin_servers,
    get_browser_server_config,
    BROWSER_SERVER_NAME,
    BROWSER_CLI_FLAG,
)
from app.core.mcp_config import _build_mcp_servers_dict
from app.core.mcp_service import McpServerConfig, McpServerType


def _profile():
    return {"id": "t", "config": {"model": "opus", "tools_preset": "claude_code"}}


class TestBuiltinShape:
    def test_browser_builtin_is_a_cli_flag_server(self):
        b = get_browser_server_config()
        assert b.name == BROWSER_SERVER_NAME == "claude-in-chrome"
        assert b.cli_flag == BROWSER_CLI_FLAG == "chrome"
        # CLI-flag built-ins are not spawnable: no command/url.
        assert b.command is None
        assert b.url is None
        assert b.builtin is True

    def test_get_builtin_servers_contains_only_claude_in_chrome(self):
        names = [s.name for s in get_builtin_servers()]
        assert names == ["claude-in-chrome"]


class TestMcpServersExclusion:
    def test_cli_flag_builtin_is_not_added_to_mcp_servers(self):
        """A CLI-flag server has no command and must never reach mcp_servers."""
        builtin = get_browser_server_config()
        normal = McpServerConfig(
            name="normal", type=McpServerType.STDIO, command="echo", args=["hi"]
        )
        fake_service = MagicMock()
        fake_service.get_all_servers.return_value = {
            "builtin": [builtin],
            "user": [normal],
        }
        with patch("app.core.mcp_config.get_mcp_service", return_value=fake_service):
            result = _build_mcp_servers_dict(project_dir=None) or {}

        assert "claude-in-chrome" not in result
        assert "normal" in result  # ordinary stdio server still passes through


class TestBuildOptionsWiring:
    def test_enabled_injects_chrome_flag_and_tool_wildcard(self):
        with patch.object(qo, "get_security_instructions", return_value=""):
            opts, _ = qo.build_options_from_profile(profile=_profile())

        extra = opts.extra_args or {}
        assert "chrome" in extra
        # None value => the SDK emits a bare ``--chrome`` flag.
        assert extra["chrome"] is None
        assert "mcp__claude-in-chrome" in (opts.allowed_tools or [])
        # Never spawned as a server.
        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "claude-in-chrome" not in servers

    def test_disabled_omits_chrome_flag_and_tool_wildcard(self):
        """When the built-in is in disabledMcpServers, neither flag nor tool appears."""
        disabled_service = MagicMock()
        disabled_service.get_disabled_servers.return_value = ["claude-in-chrome"]
        # get_all_servers marks the builtin disabled so it's filtered everywhere.
        disabled_builtin = get_browser_server_config()
        disabled_builtin.enabled = False
        disabled_service.get_all_servers.return_value = {"builtin": [disabled_builtin], "user": []}

        with patch.object(qo, "get_security_instructions", return_value=""), \
             patch("app.core.mcp_service.get_mcp_service", return_value=disabled_service), \
             patch("app.core.mcp_config.get_mcp_service", return_value=disabled_service):
            opts, _ = qo.build_options_from_profile(profile=_profile())

        extra = opts.extra_args or {}
        assert "chrome" not in extra
        assert "mcp__claude-in-chrome" not in (opts.allowed_tools or [])
