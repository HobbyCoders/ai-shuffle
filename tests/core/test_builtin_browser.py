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
     NO ``--chrome`` extra_arg.
  4. The built-in is ALWAYS ON: stale disable entries (legacy
     ``claude-in-chrome`` included) are stripped from settings.json reads and a
     disable write refuses, so no gate can unspawn it.

build_options_from_profile makes one unavoidable DB call (get_security_instructions);
we patch that single boundary and let the rest run against on-disk config.
"""

from unittest.mock import MagicMock, patch

import pytest

import app.core.query_options as qo
from app.core.builtin_mcp_servers import (
    BROWSER_SERVER_NAME,
    DEFAULT_BROWSER_MCP_ENTRY,
    get_browser_server_config,
    get_builtin_servers,
)
from app.core.mcp_config import _build_browser_server_env, _build_mcp_servers_dict
from app.core.mcp_service import (
    McpServerConfig,
    McpServerType,
    McpService,
    _normalize_disabled_servers,
)


def _profile():
    return {"id": "t", "config": {"model": "opus", "tools_preset": "claude_code"}}


@pytest.fixture(autouse=True)
def _browser_configuration(monkeypatch):
    """Exercise configured availability independently of the host's Chrome flag.

    These tests inspect config only; they never launch a browser or an MCP server.
    The explicit disabled-configuration test overrides this value itself.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "start_chrome", True)


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

    @pytest.mark.parametrize("label", ["Google Chrome", "Chromium", "Microsoft Edge"])
    def test_description_names_the_browser_in_use(self, label):
        with patch("app.core.native_chrome.browser_label", return_value=label):
            b = get_browser_server_config()
        assert f"drive the supervised {label} over CDP" in b.description
        for other in {"Google Chrome", "Chromium", "Microsoft Edge"} - {label}:
            assert other not in b.description

    def test_start_chrome_off_withdraws_the_browser_suite(self, monkeypatch):
        """START_CHROME=0 means no Chromium exists to drive, so sessions must not
        be offered mcp__browser__* tools that would only fail to connect."""
        from app.core.config import settings
        monkeypatch.setattr(settings, "start_chrome", False)
        assert get_builtin_servers() == []

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
        assert "ANTHROPIC_BASE_URL" not in env
        assert "ANTHROPIC_AUTH_TOKEN" not in env
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

    def test_stale_disable_entry_still_spawns_browser(self):
        """The built-in is always on: even a stale disable list must not gate it.

        The service honours _normalize_disabled_servers (which strips
        ``browser``), so a settings.json leftover cannot unspawn the server.
        """
        stale_service = MagicMock()
        stale_service.get_disabled_servers.return_value = []
        builtin = get_browser_server_config()
        stale_service.get_all_servers.return_value = {
            "builtin": [builtin],
            "user": [],
        }

        with patch.object(qo, "get_security_instructions", return_value=""), \
             patch("app.core.mcp_service.get_mcp_service", return_value=stale_service), \
             patch("app.core.mcp_config.get_mcp_service", return_value=stale_service):
            opts, _ = qo.build_options_from_profile(profile=_profile())

        extra = opts.extra_args or {}
        assert "chrome" not in extra
        assert "mcp__browser" in (opts.allowed_tools or [])
        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "browser" in servers


class TestAlwaysOn:
    """The browser built-in has no setting anymore — it is always enabled.

    A stale disable entry (including the legacy ``claude-in-chrome`` name) must
    be stripped from every normalized read, and a disable write must refuse.
    """
    def test_normalize_strips_browser(self):
        assert _normalize_disabled_servers(["browser", "other"]) == ["other"]
        assert _normalize_disabled_servers(["browser"]) == []

    def test_normalize_strips_legacy_name_too(self):
        # Legacy entry would map to ``browser``, which is then stripped.
        assert _normalize_disabled_servers(["claude-in-chrome"]) == []
        assert _normalize_disabled_servers(["other", "claude-in-chrome"]) == ["other"]

    def test_normalize_is_idempotent_and_dedups(self):
        assert _normalize_disabled_servers(["other", "more", "other"]) == [
            "other",
            "more",
        ]

    def test_builtin_reports_enabled_even_with_stale_disable_entry(self):
        servers = get_builtin_servers()
        for s in servers:
            if s.name in set(_normalize_disabled_servers(["browser"])):
                s.enabled = False
        browser = next(s for s in servers if s.name == BROWSER_SERVER_NAME)
        assert browser.enabled is True

    def test_set_server_enabled_refuses_to_disable(self):
        service = McpService()
        with patch.object(service, "_read_json_file",
                          return_value={"disabledMcpServers": []}), \
             patch.object(service, "_write_json_file", return_value=True) as w:
            assert service.set_server_enabled(BROWSER_SERVER_NAME, False) is True
            # The refusal happens before any settings.json write-back.
            w.assert_not_called()

    def test_set_server_enabled_can_enable_other_servers(self):
        service = McpService()
        with patch.object(service, "_read_json_file",
                          return_value={"disabledMcpServers": ["foo"]}), \
             patch.object(service, "_write_json_file", return_value=True) as w:
            assert service.set_server_enabled("foo", True) is True
            (path, data), _ = w.call_args
            assert path == service.SETTINGS_FILE
            assert data["disabledMcpServers"] == []
