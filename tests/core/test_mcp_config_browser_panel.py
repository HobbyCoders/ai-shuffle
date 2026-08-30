"""Tests for the browser-panel and SSRF env wiring in app.core.mcp_config."""

import pytest

from app.core import mcp_config
from app.core import browser_panel_registry as registry
from app.core.config import settings

LOOPBACK = "127.0.0.0/8,::1/128"


def _set_local_mode(monkeypatch, value: bool) -> None:
    """``settings`` is a pydantic model: patch the method on its class, not the
    instance (pydantic rejects instance attributes that are not fields)."""
    monkeypatch.setattr(type(settings), "is_local_mode", lambda self: value)


@pytest.fixture(autouse=True)
def _clean_ssrf_env(monkeypatch):
    """Neither the process env nor the host's real deployment mode leaks in."""
    monkeypatch.delenv("BROWSER_SSRF_ALLOW", raising=False)
    monkeypatch.delenv("BROWSER_SSRF_BLOCK", raising=False)
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", None, raising=False)
    _set_local_mode(monkeypatch, False)


def test_no_session_omits_panel_wiring():
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert "AIS_SESSION_ID" not in env
    assert "AIS_PANEL_TOKEN" not in env
    assert "AIS_PANEL_REGISTER_URL" not in env


def test_session_injects_panel_wiring(monkeypatch):
    monkeypatch.delenv("BROWSER_PANEL_REGISTER_URL", raising=False)
    registry.clear("sess-mcp")

    env = mcp_config._build_browser_server_env({}, session_id="sess-mcp")

    assert env["AIS_SESSION_ID"] == "sess-mcp"
    # Token matches the registry's issued (idempotent) token.
    assert registry.verify_token("sess-mcp", env["AIS_PANEL_TOKEN"])
    assert env["AIS_PANEL_TOKEN"] == registry.issue_token("sess-mcp")
    assert env["AIS_PANEL_REGISTER_URL"].startswith("http://127.0.0.1:")
    assert env["AIS_PANEL_REGISTER_URL"].endswith("/api/v1/browser/panel/register")


def test_register_url_os_override(monkeypatch):
    monkeypatch.setenv("BROWSER_PANEL_REGISTER_URL", "http://elsewhere:9/reg")
    registry.clear("sess-mcp2")

    env = mcp_config._build_browser_server_env({}, session_id="sess-mcp2")
    assert env["AIS_PANEL_REGISTER_URL"] == "http://elsewhere:9/reg"


# --- BROWSER_SSRF_ALLOW policy -------------------------------------------------
#
# Matrix: loopback is allowed on local/self-hosted installs (where the agent is
# the only tenant and routinely needs to view a dev server it just started) and
# blocked everywhere else. An explicit BROWSER_SSRF_ALLOW always wins.


def test_local_mode_allows_loopback_by_default(monkeypatch):
    _set_local_mode(monkeypatch, True)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_non_local_mode_leaves_loopback_blocked():
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert "BROWSER_SSRF_ALLOW" not in env


def test_setting_forces_loopback_on_in_docker(monkeypatch):
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", True)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_setting_forces_loopback_off_in_local_mode(monkeypatch):
    _set_local_mode(monkeypatch, True)
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", False)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert "BROWSER_SSRF_ALLOW" not in env


def test_explicit_session_allow_wins_over_local_default(monkeypatch):
    _set_local_mode(monkeypatch, True)

    env = mcp_config._build_browser_server_env(
        {"BROWSER_SSRF_ALLOW": "10.9.0.0/16"}, session_id=None
    )
    assert env["BROWSER_SSRF_ALLOW"] == "10.9.0.0/16"


def test_explicit_process_allow_wins_and_reaches_the_server(monkeypatch):
    # An operator sets this in .env / compose, not in the session profile.
    _set_local_mode(monkeypatch, True)
    monkeypatch.setenv("BROWSER_SSRF_ALLOW", "192.168.50.0/24")

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == "192.168.50.0/24"


def test_process_block_list_reaches_the_server(monkeypatch):
    monkeypatch.setenv("BROWSER_SSRF_BLOCK", "10.0.0.0/8")

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_BLOCK"] == "10.0.0.0/8"


def test_local_mode_probe_failure_falls_back_to_blocking(monkeypatch):
    def boom(self):
        raise RuntimeError("deployment probe exploded")

    monkeypatch.setattr(type(settings), "is_local_mode", boom)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert "BROWSER_SSRF_ALLOW" not in env
