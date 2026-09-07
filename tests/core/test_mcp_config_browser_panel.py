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
# Matrix (most specific first): an explicit BROWSER_SSRF_ALLOW always wins; then
# BROWSER_SSRF_ALLOW_LOOPBACK true/false; then the default, which ALLOWS
# loopback. AI Shuffle exposes no tenancy setting, so "self-hosted single user"
# is what an install is — Docker included. The deployment-mode probe is no
# longer consulted at all.


def test_default_allows_loopback(monkeypatch):
    """Unset knob, non-local (Docker) deployment: loopback is allowed."""
    _set_local_mode(monkeypatch, False)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_local_mode_allows_loopback_by_default(monkeypatch):
    _set_local_mode(monkeypatch, True)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_deployment_mode_is_not_consulted(monkeypatch):
    """The old rule keyed off is_local_mode(); nothing does any more."""
    def _boom(self):  # pragma: no cover - fails the test if it is ever called
        raise AssertionError("is_local_mode must not gate the SSRF policy")

    monkeypatch.setattr(type(settings), "is_local_mode", _boom)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_setting_false_blocks_loopback(monkeypatch):
    """An install fronted for other people turns it back off explicitly."""
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", False)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert "BROWSER_SSRF_ALLOW" not in env


def test_setting_forces_loopback_on_in_docker(monkeypatch):
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", True)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_explicit_allow_from_session_env_passes_through(monkeypatch):
    """An operator-stated policy is handed over verbatim — never appended to."""
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", False)

    env = mcp_config._build_browser_server_env(
        {"BROWSER_SSRF_ALLOW": "10.0.0.0/8"}, session_id=None
    )
    assert env["BROWSER_SSRF_ALLOW"] == "10.0.0.0/8"


def test_explicit_allow_from_process_env_passes_through(monkeypatch):
    monkeypatch.setenv("BROWSER_SSRF_ALLOW", "192.168.0.0/16")

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == "192.168.0.0/16"


# --- BROWSER_LOOPBACK_HOST ----------------------------------------------------
#
# The shell and the browser are not always in the same network namespace, so a
# dev server on localhost can be real and unreachable at once. The app passes
# the address this host answers on; browser-mcp uses it only after a direct
# attempt fails.


def test_loopback_host_passes_an_explicit_value_through(monkeypatch):
    monkeypatch.setenv("BROWSER_LOOPBACK_HOST", "10.0.0.135")
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_LOOPBACK_HOST"] == "10.0.0.135"


def test_loopback_host_prefers_the_session_env_over_the_process_env(monkeypatch):
    monkeypatch.setenv("BROWSER_LOOPBACK_HOST", "10.0.0.135")
    env = mcp_config._build_browser_server_env(
        {"BROWSER_LOOPBACK_HOST": "172.17.0.5"}, session_id=None
    )
    assert env["BROWSER_LOOPBACK_HOST"] == "172.17.0.5"


def test_loopback_host_falls_back_to_this_host_s_own_address(monkeypatch):
    monkeypatch.delenv("BROWSER_LOOPBACK_HOST", raising=False)
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: "172.17.0.5")
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_LOOPBACK_HOST"] == "172.17.0.5"


def test_loopback_host_is_omitted_when_no_address_resolves(monkeypatch):
    monkeypatch.delenv("BROWSER_LOOPBACK_HOST", raising=False)
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: None)
    assert "BROWSER_LOOPBACK_HOST" not in mcp_config._build_browser_server_env({}, session_id=None)


def test_loopback_host_survives_a_socket_that_refuses_to_probe(monkeypatch):
    monkeypatch.delenv("BROWSER_LOOPBACK_HOST", raising=False)

    def boom():
        raise OSError("no network")

    monkeypatch.setattr(mcp_config, "_own_routable_address", boom)
    assert "BROWSER_LOOPBACK_HOST" not in mcp_config._build_browser_server_env({}, session_id=None)


@pytest.mark.parametrize('host,expected', [
    ('192.168.20.2', '192.168.20.2'),
    ('127.0.0.1', None),
    ('::1', None),
    ('fe80::1', None),
])
def test_own_address_is_routable_or_absent(monkeypatch, host, expected):
    from unittest.mock import MagicMock

    sock = MagicMock()
    sock.getsockname.return_value = (host, 1234)
    monkeypatch.setattr('socket.socket', lambda *args, **kwargs: sock)
    assert mcp_config._own_routable_address() == expected
    sock.close.assert_called()
