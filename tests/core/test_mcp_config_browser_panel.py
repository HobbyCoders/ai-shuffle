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
def _clean_ssrf_env(monkeypatch, request):
    """Neither the process env nor the host's real deployment mode leaks in."""
    monkeypatch.delenv("BROWSER_SSRF_ALLOW", raising=False)
    monkeypatch.delenv("BROWSER_SSRF_BLOCK", raising=False)
    monkeypatch.delenv("BROWSER_SSRF_DENY_PORTS", raising=False)
    monkeypatch.delenv("BROWSER_LOOPBACK_HOST", raising=False)
    if "own_address" not in request.node.name:
        monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: None)
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", None, raising=False)
    _set_local_mode(monkeypatch, False)


def test_upload_roots_are_enabled_and_operator_override_is_preserved(monkeypatch, tmp_path):
    monkeypatch.delenv("BROWSER_UPLOAD_DIR", raising=False)
    monkeypatch.setattr(type(settings), "effective_workspace_dir", property(lambda self: tmp_path))
    env = mcp_config._build_browser_server_env({})
    assert env["BROWSER_UPLOAD_DIR"] == str(tmp_path.resolve())
    assert env["AIS_BROWSER_PANEL_UPLOAD_DIR"] == str((tmp_path / ".browser-uploads").resolve())
    assert mcp_config._build_browser_server_env({"BROWSER_UPLOAD_DIR": "/restricted"})["BROWSER_UPLOAD_DIR"] == "/restricted"
    # An explicit unusable root stays explicit: runtime preflight must diagnose
    # it rather than replace it with a wider workspace allowlist.
    disabled_root = str(tmp_path / "does-not-exist")
    assert mcp_config._build_browser_server_env({"BROWSER_UPLOAD_DIR": disabled_root})["BROWSER_UPLOAD_DIR"] == disabled_root
    monkeypatch.setenv("BROWSER_UPLOAD_DIR", "/operator-root")
    assert mcp_config._build_browser_server_env({})["BROWSER_UPLOAD_DIR"] == "/operator-root"
    assert mcp_config._build_browser_server_env({"BROWSER_UPLOAD_DIR": "/restricted"})["BROWSER_UPLOAD_DIR"] == "/restricted"


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
# loopback in every runtime (Docker, native and desktop). AI Shuffle exposes no
# tenancy setting, so the deployment-mode probe does not gate it (e55cfa74).
# That is the documented policy in docs/internal/DEVELOPMENT.md and .env.example.

_REAL_IS_LOCAL_MODE = type(settings).is_local_mode


def test_docker_default_allows_loopback(monkeypatch):
    """Unset knob in Docker: loopback is allowed."""
    _set_local_mode(monkeypatch, False)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_native_default_allows_loopback(monkeypatch):
    _set_local_mode(monkeypatch, True)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


@pytest.mark.parametrize("local_mode_env", [
    # The desktop shell starts the worker with LOCAL_MODE=true (desktop/src/main/worker.ts).
    "true",
    # An explicit LOCAL_MODE=false is a Docker deployment.
    "false",
])
def test_runtime_default_is_the_same_under_real_deployment_detection(monkeypatch, local_mode_env):
    """End to end through settings.is_local_mode() and app.core.platform, not a stub."""
    monkeypatch.setattr(type(settings), "is_local_mode", _REAL_IS_LOCAL_MODE)
    monkeypatch.setattr(settings, "local_mode", None, raising=False)
    monkeypatch.setenv("LOCAL_MODE", local_mode_env)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


def test_deployment_mode_is_not_consulted(monkeypatch):
    """The old rule keyed off is_local_mode(); nothing does any more."""
    def _boom(self):  # pragma: no cover - fails the test if it is ever called
        raise AssertionError("is_local_mode must not gate the SSRF policy")

    monkeypatch.setattr(type(settings), "is_local_mode", _boom)

    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK


@pytest.mark.parametrize("local_mode", [False, True], ids=["docker", "native"])
def test_setting_false_blocks_loopback(monkeypatch, local_mode):
    """An install fronted for other people turns it off explicitly, in any runtime."""
    _set_local_mode(monkeypatch, local_mode)
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
    monkeypatch.setenv("BROWSER_LOOPBACK_HOST", "10.0.0.30")
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_LOOPBACK_HOST"] == "10.0.0.30"


def test_loopback_host_prefers_the_session_env_over_the_process_env(monkeypatch):
    monkeypatch.setenv("BROWSER_LOOPBACK_HOST", "10.0.0.30")
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
    ('192.168.10.2', '192.168.10.2'),
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


# --- private ranges and the worker's own ports (SEC-12) ------------------------


def test_loopback_stand_in_is_allowed_with_automatic_loopback(monkeypatch):
    """browser-mcp now refuses private addresses by default, so the loopback
    stand-in (this host's own address) rides on the automatic loopback allow."""
    _set_local_mode(monkeypatch, True)
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: "172.17.0.5")
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK + ",172.17.0.5/32"


def test_explicit_allow_is_not_widened_by_the_loopback_stand_in(monkeypatch):
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: "172.17.0.5")
    env = mcp_config._build_browser_server_env(
        {"BROWSER_SSRF_ALLOW": "127.0.0.1/32"}, session_id=None
    )
    assert env["BROWSER_SSRF_ALLOW"] == "127.0.0.1/32"


def test_stand_in_is_not_allowed_when_loopback_is_off(monkeypatch):
    _set_local_mode(monkeypatch, True)
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: "172.17.0.5")
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", False, raising=False)
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert "BROWSER_SSRF_ALLOW" not in env


def test_docker_default_allows_loopback_and_the_stand_in(monkeypatch):
    _set_local_mode(monkeypatch, False)
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: "172.17.0.5")
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_ALLOW"] == LOOPBACK + ",172.17.0.5/32"
    assert env["BROWSER_LOOPBACK_HOST"] == "172.17.0.5"


def test_docker_opt_out_refuses_loopback_and_the_stand_in(monkeypatch):
    """The container's own address is private; with loopback off it stays
    refused too, but the stand-in is still named for error messages."""
    _set_local_mode(monkeypatch, False)
    monkeypatch.setattr(settings, "browser_ssrf_allow_loopback", False, raising=False)
    monkeypatch.setattr(mcp_config, "_own_routable_address", lambda: "172.17.0.5")
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert "BROWSER_SSRF_ALLOW" not in env
    assert env["BROWSER_LOOPBACK_HOST"] == "172.17.0.5"


def test_worker_ports_are_always_denied(monkeypatch):
    monkeypatch.setattr(settings, "port", 8000, raising=False)
    monkeypatch.delenv("SHUFFLE_DESKTOP_PROTECTED_PORT", raising=False)
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_DENY_PORTS"] == "8000,8001"


def test_operator_deny_ports_add_to_the_worker_ports(monkeypatch):
    monkeypatch.setattr(settings, "port", 8000, raising=False)
    monkeypatch.delenv("SHUFFLE_DESKTOP_PROTECTED_PORT", raising=False)
    monkeypatch.setenv("BROWSER_SSRF_DENY_PORTS", "9000")
    env = mcp_config._build_browser_server_env({}, session_id=None)
    assert env["BROWSER_SSRF_DENY_PORTS"] == "8000,8001,9000"


class _Task:
    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


@pytest.mark.parametrize(
    ("in_container", "start_chrome", "task", "expected"),
    [
        (True, None, None, True),  # Docker: start-chrome.sh launches it behind the proxy
        (True, "0", None, False),  # Docker with START_CHROME=0: someone else's browser
        (False, None, _Task(done=False), True),  # native supervisor running
        (False, None, _Task(done=True), False),  # native supervisor gone
        (False, None, None, False),  # external browser on the CDP port / not started
    ],
)
def test_egress_proxy_flag_follows_the_launcher(monkeypatch, in_container, start_chrome, task, expected):
    """browser-mcp reads BROWSER_EGRESS_PROXY=1 as "an https tunnel refusal is the
    egress policy"; it must be set exactly when the browser runs behind egress-launch."""
    from app.core import native_chrome

    monkeypatch.setattr(native_chrome, "_in_container", lambda: in_container)
    monkeypatch.setattr(native_chrome, "_supervisor_task", task)
    if start_chrome is None:
        monkeypatch.delenv("START_CHROME", raising=False)
    else:
        monkeypatch.setenv("START_CHROME", start_chrome)

    env = mcp_config._build_browser_server_env({})

    if expected:
        assert env["BROWSER_EGRESS_PROXY"] == "1"
    else:
        assert "BROWSER_EGRESS_PROXY" not in env


def test_egress_proxy_flag_is_not_taken_from_the_profile_env(monkeypatch):
    from app.core import native_chrome

    monkeypatch.setattr(native_chrome, "_in_container", lambda: False)
    monkeypatch.setattr(native_chrome, "_supervisor_task", None)
    monkeypatch.setenv("BROWSER_EGRESS_PROXY", "1")

    env = mcp_config._build_browser_server_env({"BROWSER_EGRESS_PROXY": "1"})

    assert "BROWSER_EGRESS_PROXY" not in env
