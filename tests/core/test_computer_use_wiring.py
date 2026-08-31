"""Regression tests for the computer-use capability wiring in query_options.

Computer-use is an in-process SDK MCP server (not a CLI-flag built-in like Claude
in Chrome). It must only be wired in when BOTH the GLOBAL toggle (a system
setting, read via app.api.computer_use.get_computer_use_enabled) is on AND the
host reports it available — so we patch both seams deterministically (the test
host's real display state and stored setting must not matter).

The legacy per-profile ``computer_use.enabled`` config key is no longer
consulted; a profile that still carries it must NOT enable the tools.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import app.core.query_options as qo
from app.mcp.computer_use_sdk import get_computer_use_tool_names

_CU_TOOLS = set(get_computer_use_tool_names())


def _profile(computer_use=None):
    config = {"model": "opus", "tools_preset": "claude_code"}
    if computer_use is not None:
        config["computer_use"] = computer_use
    return {"id": "t", "config": config}


def _build(profile):
    with patch.object(qo, "get_security_instructions", return_value=""):
        opts, _ = qo.build_options_from_profile(profile=profile)
    return opts


@contextmanager
def _enabled_and_available(enabled=True):
    """Drive the toggle+availability wiring path deterministically.

    Three independent seams are controlled so the test host's real display
    state, optional deps (pynput) and stored setting don't matter:

      * the global toggle is read through
        ``app.api.computer_use.get_computer_use_enabled`` at call time —
        patched to return ``enabled``;
      * ``query_options`` re-imports ``computer_use_available`` from
        ``app.mcp.computer_use.scaling`` at call time to decide tool
        allow-listing and system-prompt guidance — patched to report available;
      * ``query_options`` builds the server via
        ``app.mcp.computer_use_sdk.create_computer_use_server``, whose real body
        requires ``pynput`` and a live display. We stub it to a sentinel server
        so the registration branch is exercised without host dependencies (the
        factory's own availability logic is covered by test_computer_use_scaling).
    """
    fake_server = MagicMock(name="computer-use-server")
    with patch(
        "app.api.computer_use.get_computer_use_enabled",
        return_value=enabled,
    ), patch(
        "app.mcp.computer_use.scaling.computer_use_available",
        return_value=(True, "ok"),
    ), patch(
        "app.mcp.computer_use_sdk.create_computer_use_server",
        return_value=fake_server,
    ):
        yield


class TestEnabledAndAvailable:
    def test_tools_allowlisted_and_server_registered(self):
        with _enabled_and_available(enabled=True):
            opts = _build(_profile())

        allowed = set(opts.allowed_tools or [])
        assert _CU_TOOLS.issubset(allowed), "computer-use tools should be allow-listed"

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" in servers, "computer-use server should be registered"

    def test_system_prompt_gets_guidance(self):
        with _enabled_and_available(enabled=True):
            opts = _build(_profile())

        sp = opts.system_prompt
        text = sp if isinstance(sp, str) else (sp or {}).get("append", "")
        assert "mcp__computer-use__" in text
        assert "screenshot" in text


class TestEnabledButUnavailable:
    def test_docker_host_omits_tools_and_server(self):
        with patch(
            "app.api.computer_use.get_computer_use_enabled",
            return_value=True,
        ), patch(
            "app.mcp.computer_use.scaling.computer_use_available",
            return_value=(False, "docker"),
        ):
            opts = _build(_profile())

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed), "no computer-use tools when host unavailable"

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers


class TestToggleReadErrorsClosed:
    def test_toggle_read_failure_disables(self):
        with patch(
            "app.api.computer_use.get_computer_use_enabled",
            side_effect=RuntimeError("db down"),
        ):
            opts = _build(_profile())

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed)
        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers


class TestLegacyProfileKeyIgnored:
    """The old per-profile switch must no longer grant the capability."""

    def test_profile_config_alone_enables_nothing(self):
        with _enabled_and_available(enabled=False):
            opts = _build(_profile({"enabled": True}))

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed)

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers


class TestDisabled:
    def test_default_profile_has_no_computer_use(self):
        # No computer_use config at all and the global toggle off (the common case).
        with _enabled_and_available(enabled=False):
            opts = _build(_profile())

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed)

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers

    def test_explicitly_disabled(self):
        with _enabled_and_available(enabled=False):
            opts = _build(_profile({"enabled": False}))

        allowed = set(opts.allowed_tools or [])
        assert not (_CU_TOOLS & allowed)

        servers = opts.mcp_servers if isinstance(opts.mcp_servers, dict) else {}
        assert "computer-use" not in servers


class TestProfileConfigPersistence:
    """ProfileConfig still round-trips a legacy computer_use key instead of
    dropping it (Pydantic silently discards unknown keys) — old configs must
    survive a save even though the key is no longer consulted."""

    def test_enabled_survives_model_dump(self):
        from app.core.models import ProfileConfig

        cfg = ProfileConfig(**{"model": "opus", "computer_use": {"enabled": True}})
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
