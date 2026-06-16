"""Regression tests for the computer-use capability wiring in query_options.

Computer-use is an in-process SDK MCP server (not a CLI-flag built-in like Claude
in Chrome). It must only be wired in when BOTH the profile enables it AND the host
reports it available — so we patch ``computer_use_available`` to drive both
branches deterministically (the test host's real display state must not matter).

These tests pin:
  1. enabled + available  -> tools allow-listed AND server registered.
  2. enabled + unavailable -> neither tools nor server (e.g. running in Docker).
  3. disabled             -> neither tools nor server.
"""

from unittest.mock import patch

import app.core.query_options as qo
from app.mcp.computer_use_sdk import get_computer_use_tool_names

_CU_TOOLS = set(get_computer_use_tool_names())


def _profile(computer_use=None):
    config = {"model": "claude-opus-4-8", "tools_preset": "claude_code"}
    if computer_use is not None:
        config["computer_use"] = computer_use
    return {"id": "t", "config": config}


def _build(profile):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=profile)
    return opts


class TestEnabledAndAvailable:
    def test_tools_allowlisted_and_server_registered(self):
        with patch(
            "app.mcp.computer_use.scaling.computer_use_available",
            return_value=(True, "ok"),
        ):
            opts = _build(_profile({"enabled": True}))

        allowed = set(opts.allowed_tools or [])
        assert _CU_TOOLS.issubset(allowed), "computer-use tools should be allow-listed"

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" in servers, "computer-use server should be registered"

    def test_system_prompt_gets_guidance(self):
        with patch(
            "app.mcp.computer_use.scaling.computer_use_available",
            return_value=(True, "ok"),
        ):
            opts = _build(_profile({"enabled": True}))

        sp = opts.system_prompt
        text = sp if isinstance(sp, str) else (sp or {}).get("append", "")
        assert "mcp__computer-use__" in text
        assert "screenshot" in text


class TestEnabledButUnavailable:
    def test_docker_host_omits_tools_and_server(self):
        with patch(
            "app.mcp.computer_use.scaling.computer_use_available",
            return_value=(False, "docker"),
        ):
            opts = _build(_profile({"enabled": True}))

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed), "no computer-use tools when host unavailable"

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers


class TestProfileConfigPersistence:
    """ProfileConfig must keep computer_use — Pydantic silently drops unknown
    keys, so a missing field means the toggle never saves (regression guard)."""

    def test_enabled_survives_model_dump(self):
        from app.core.models import ProfileConfig

        cfg = ProfileConfig(**{"model": "claude-opus-4-8", "computer_use": {"enabled": True}})
        dumped = cfg.model_dump(exclude_none=True)
        assert dumped.get("computer_use", {}).get("enabled") is True

    def test_disabled_survives_model_dump(self):
        from app.core.models import ProfileConfig

        cfg = ProfileConfig(**{"computer_use": {"enabled": False}})
        dumped = cfg.model_dump(exclude_none=True)
        assert dumped.get("computer_use", {}).get("enabled") is False

    def test_absent_when_unset(self):
        from app.core.models import ProfileConfig

        cfg = ProfileConfig(model="x")
        assert "computer_use" not in cfg.model_dump(exclude_none=True)


class TestDisabled:
    def test_default_profile_has_no_computer_use(self):
        # No computer_use config at all (the common case).
        opts = _build(_profile())

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed)

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers

    def test_explicitly_disabled(self):
        opts = _build(_profile({"enabled": False}))

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed)

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers
