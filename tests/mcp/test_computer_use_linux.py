"""Computer use on Linux: session detection, the enable gate, and the Linux backends.

Everything is faked: no test opens an X display, runs xclip/xsel, or sends
input. Live X11 control is not exercised here.
"""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.mcp.computer_use import linux, safety, scaling

# ---------------------------------------------------------------------------
# Session type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env, expected", [
    ({"DISPLAY": ":0"}, "x11"),
    ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}, "x11"),
    ({"DISPLAY": ":99", "XDG_SESSION_TYPE": "tty"}, "x11"),  # Xvfb from a shell
    ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
    # XWayland sets DISPLAY too, but only shows X11 apps: still Wayland.
    ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, "wayland"),
    ({"XDG_SESSION_TYPE": "Wayland", "DISPLAY": ":0"}, "wayland"),
    ({}, "none"),
    ({"XDG_SESSION_TYPE": "x11"}, "none"),  # a service without DISPLAY
])
def test_session_type(env, expected):
    assert linux.session_type(env) == expected


# ---------------------------------------------------------------------------
# Missing packages
# ---------------------------------------------------------------------------

def test_missing_python_packages_names_pip_packages():
    present = {"mss", "PIL"}
    missing = linux.missing_python_packages(lambda m: object() if m in present else None)
    assert missing == ["pynput", "python-xlib"]


def test_missing_python_packages_never_imports(monkeypatch):
    # find_spec only: importing pynput would open an X connection.
    seen = []
    linux.missing_python_packages(lambda m: seen.append(m) or object())
    assert seen == ["mss", "pynput", "PIL", "Xlib"]


def test_missing_system_libraries_for_mss10(monkeypatch):
    monkeypatch.setattr(linux, "_found_libraries", set())
    monkeypatch.setattr(linux.importlib.metadata, "version", lambda name: "10.2.0")
    found = {"xcb", "xcb-render", "xcb-xfixes"}
    assert linux.missing_system_libraries(lambda n: f"lib{n}.so" if n in found else None) == [
        "libxcb-randr0", "libxcb-shm0"]


def test_missing_system_libraries_for_older_mss(monkeypatch):
    monkeypatch.setattr(linux, "_found_libraries", set())
    monkeypatch.setattr(linux.importlib.metadata, "version", lambda name: "9.0.2")
    assert linux.missing_system_libraries(lambda n: None) == ["libx11-6", "libxrandr2"]


def test_found_libraries_are_not_looked_up_again(monkeypatch):
    monkeypatch.setattr(linux, "_found_libraries", set())
    monkeypatch.setattr(linux.importlib.metadata, "version", lambda name: "10.2.0")
    calls = []
    def find(name):
        calls.append(name)
        return f"lib{name}.so"
    assert linux.missing_system_libraries(find) == []
    assert linux.missing_system_libraries(find) == []
    assert len(calls) == len(linux._MSS_XCB_LIBS)


def test_help_texts_name_what_to_install():
    assert "pynput, python-xlib" in linux.missing_python_help(["pynput", "python-xlib"])
    assert "requirements-native.txt" in linux.missing_python_help(["mss"])
    text = linux.missing_libraries_help(["libxcb-randr0", "libxcb-shm0"])
    assert "sudo apt install libxcb-randr0 libxcb-shm0" in text


# ---------------------------------------------------------------------------
# X server probe
# ---------------------------------------------------------------------------

class _FakeDisplay:
    def __init__(self, xtest=True, xwayland=False):
        self.xtest = xtest
        self.xwayland = xwayland
        self.closed = False

    def has_extension(self, name):
        return {"XTEST": self.xtest, "XWAYLAND": self.xwayland}.get(name, False)

    def close(self):
        self.closed = True


def test_probe_x11_ok_closes_the_connection(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    conn = _FakeDisplay()
    assert linux.probe_x11(lambda: conn) == (None, "")
    assert conn.closed


def test_probe_x11_unreachable_explains_display_and_xauthority(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    def refuse():
        raise RuntimeError("Can't connect to display \":0\": Authorization required")
    reason, text = linux.probe_x11(refuse)
    assert reason == "x11-unreachable"
    assert "':0'" in text and "Authorization required" in text and "XAUTHORITY" in text


def test_probe_x11_without_xtest(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":1")
    conn = _FakeDisplay(xtest=False)
    reason, text = linux.probe_x11(lambda: conn)
    assert reason == "x11-no-xtest" and "XTEST" in text
    assert conn.closed


def test_probe_x11_refuses_xwayland(monkeypatch):
    # Regression: a Wayland desktop whose WAYLAND_DISPLAY/XDG_SESSION_TYPE did
    # not reach the worker (only DISPLAY) passed as X11 and reported available.
    monkeypatch.setenv("DISPLAY", ":0")
    conn = _FakeDisplay(xwayland=True)
    reason, text = linux.probe_x11(lambda: conn)
    assert reason == "wayland" and "XWayland" in text
    assert conn.closed


# ---------------------------------------------------------------------------
# The enable gate (computer_use_availability) on each Linux case
# ---------------------------------------------------------------------------

class _FakeMss:
    def __init__(self, monitors):
        self.monitors = monitors

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def linux_host(monkeypatch):
    """A native Linux X11 host where every check passes until a test breaks one."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(scaling, "_in_docker", lambda: False)
    monkeypatch.delenv("COMPUTER_USE_DISABLED", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(linux, "missing_python_packages", list)
    monkeypatch.setattr(linux, "missing_system_libraries", list)
    monkeypatch.setattr(linux, "probe_x11", lambda: (None, ""))
    monkeypatch.setitem(sys.modules, "pynput", SimpleNamespace())
    monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}] * 2
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=lambda: _FakeMss(monitors)))
    return monitors


def test_x11_desktop_is_available(linux_host):
    result = scaling.computer_use_availability()
    assert result == scaling.Availability(True, "ok")
    assert scaling.computer_use_available() == (True, "ok")


def test_docker_is_unavailable_with_an_explanation(linux_host, monkeypatch):
    monkeypatch.setattr(scaling, "_in_docker", lambda: True)
    result = scaling.computer_use_availability()
    assert (result.available, result.reason) == (False, "docker")
    assert "container" in result.detail


def test_headless_linux_is_no_display(linux_host, monkeypatch):
    monkeypatch.delenv("DISPLAY")
    result = scaling.computer_use_availability()
    assert (result.available, result.reason) == (False, "no-display")
    assert "DISPLAY" in result.detail and "headless" in result.detail


@pytest.mark.parametrize("env", [{"WAYLAND_DISPLAY": "wayland-0"}, {"XDG_SESSION_TYPE": "wayland"}])
def test_wayland_is_reported_not_silently_failed(linux_host, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    probed = []
    monkeypatch.setattr(linux, "probe_x11", lambda: probed.append(1) or (None, ""))
    result = scaling.computer_use_availability()
    assert (result.available, result.reason) == (False, "wayland")
    assert "X11 session" in result.detail
    assert probed == []  # never touches XWayland


def test_missing_python_packages_are_named(linux_host, monkeypatch):
    monkeypatch.setattr(linux, "missing_python_packages", lambda: ["pynput", "python-xlib"])
    result = scaling.computer_use_availability()
    assert (result.available, result.reason) == (False, "missing-deps")
    assert result.missing == ("pynput", "python-xlib")
    assert "pynput, python-xlib" in result.detail


def test_missing_system_libraries_are_named(linux_host, monkeypatch):
    monkeypatch.setattr(linux, "missing_system_libraries", lambda: ["libxcb-shm0"])
    result = scaling.computer_use_availability()
    assert (result.available, result.reason) == (False, "missing-system-packages")
    assert result.missing == ("libxcb-shm0",)
    assert "sudo apt install libxcb-shm0" in result.detail


@pytest.mark.parametrize("reason", ["x11-unreachable", "x11-no-xtest"])
def test_x11_probe_failures_surface(linux_host, monkeypatch, reason):
    monkeypatch.setattr(linux, "probe_x11", lambda: (reason, "why"))
    assert scaling.computer_use_availability() == scaling.Availability(False, reason, "why")


def test_pynput_import_failure_is_not_called_missing_deps(linux_host, monkeypatch):
    monkeypatch.setitem(sys.modules, "pynput", None)  # import raises ImportError
    result = scaling.computer_use_availability()
    assert (result.available, result.reason) == (False, "x11-unreachable")


def test_no_monitors_and_capture_failure(linux_host, monkeypatch):
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=lambda: _FakeMss([{}])))
    assert scaling.computer_use_available() == (False, "no-monitors")
    def broken():
        raise RuntimeError("Library libxcb-randr.so not found")
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=broken))
    result = scaling.computer_use_availability()
    assert result.reason == "screenshot-probe-failed" and "libxcb-randr" in result.detail


def test_env_kill_switch_wins(linux_host, monkeypatch):
    monkeypatch.setenv("COMPUTER_USE_DISABLED", "1")
    assert scaling.computer_use_available() == (False, "env-disabled")


def test_last_availability_matches_reason(linux_host, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    scaling.computer_use_availability()
    assert scaling.last_availability("wayland").reason == "wayland"
    assert scaling.last_availability("docker") is None


def test_quick_check_never_probes_the_display(linux_host, monkeypatch):
    def boom(*args):
        raise AssertionError("quick check must not open the display or run ldconfig")
    monkeypatch.setattr(linux, "probe_x11", boom)
    monkeypatch.setattr(linux, "missing_system_libraries", boom)
    assert scaling.computer_use_quick_check().available is True
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert scaling.computer_use_quick_check().reason == "wayland"
    monkeypatch.setattr(scaling, "_in_docker", lambda: True)
    assert scaling.computer_use_quick_check().reason == "docker"


# ---------------------------------------------------------------------------
# Windows and macOS keep their checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_platforms_skip_the_linux_checks(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(scaling, "_in_docker", lambda: False)
    monkeypatch.setattr(scaling, "_ensure_dpi_awareness", lambda: None)
    monkeypatch.delenv("COMPUTER_USE_DISABLED", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)  # no DISPLAY needed off Linux
    def boom(*args):
        raise AssertionError("Linux check ran on " + platform)
    monkeypatch.setattr(linux, "session_type", boom)
    monkeypatch.setattr(linux, "probe_x11", boom)
    monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}] * 2
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=lambda: _FakeMss(monitors)))
    monkeypatch.setitem(sys.modules, "pynput", SimpleNamespace())
    assert scaling.computer_use_available() == (True, "ok")
    monkeypatch.setitem(sys.modules, "pynput", None)
    result = scaling.computer_use_availability()
    assert (result.available, result.reason) == (False, "missing-deps")


# ---------------------------------------------------------------------------
# Status endpoint carries the explanation
# ---------------------------------------------------------------------------

@pytest.fixture
def api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.auth import require_admin, require_auth
    from app.api.computer_use import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-session-token"
    with TestClient(app) as test_client:
        yield test_client


def test_status_reports_detail_and_missing(api, monkeypatch):
    from app.api import computer_use as cu_api

    found = scaling.Availability(False, "missing-system-packages", "install these", ("libxcb-shm0",))
    monkeypatch.setattr(cu_api, "computer_use_available", lambda: (False, found.reason))
    monkeypatch.setattr(cu_api, "last_availability", lambda reason: found if reason == found.reason else None)
    body = api.get("/api/v1/computer-use/status").json()
    assert body["available"] is False
    assert body["reason"] == "missing-system-packages"
    assert body["detail"] == "install these"
    assert body["missing"] == ["libxcb-shm0"]


def test_status_has_no_detail_when_available(api, monkeypatch):
    from app.api import computer_use as cu_api

    monkeypatch.setattr(cu_api, "computer_use_available", lambda: (True, "ok"))
    body = api.get("/api/v1/computer-use/status").json()
    assert body["detail"] is None and body["missing"] == []


# ---------------------------------------------------------------------------
# Escape: the agent's own Escape must not stop its turn on X11
# ---------------------------------------------------------------------------

@pytest.fixture
def escapes():
    linux.reset_agent_escapes()
    yield
    linux.reset_agent_escapes()


def test_announced_escape_is_consumed_once(escapes):
    linux.expect_agent_escape(now=10.0)
    assert linux.consume_agent_escape(now=10.01) is True
    assert linux.consume_agent_escape(now=10.02) is False  # a person's Escape


def test_announcement_expires(escapes):
    linux.expect_agent_escape(now=10.0)
    assert linux.consume_agent_escape(now=10.0 + linux._ECHO_WINDOW + 0.5) is False


def test_holding_escape_never_silences_a_person(escapes):
    # Regression: the agent holding Escape (hold_key, up to 100 s) used to
    # swallow every Escape until it let go, including the person's stop.
    linux.expect_agent_escape(now=10.0)
    assert linux.consume_agent_escape(now=10.0) is True  # the agent's press
    assert linux.consume_agent_escape(now=10.6) is False  # repeat or a person: stops
    assert linux.consume_agent_escape(now=12.0) is False


def test_each_announcement_covers_one_press(escapes):
    for i in range(3):
        linux.expect_agent_escape(now=10.0 + i * 0.01)
    # A person's Escape arriving among the agent's presses consumes one
    # announcement, so the agent's last press is unannounced and stops the turn.
    assert [linux.consume_agent_escape(now=10.05) for _ in range(4)] == [True, True, True, False]


def test_failed_press_withdraws_its_announcement(escapes, monkeypatch):
    from app.mcp.computer_use import abort

    monkeypatch.setattr(safety, "_x11", lambda: True)
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})

    def broken(key):
        raise OSError("X connection lost")

    keyboard = SimpleNamespace(press=broken, release=lambda key: None)
    inputs = safety.SafeInput("linux-esc-fail", None, keyboard, escape_key="esc")
    with pytest.raises(OSError):
        inputs.press(keyboard, "esc")
    # No Escape was sent, so the person's next Escape is not mistaken for it.
    assert linux.consume_agent_escape() is False


def test_safe_input_announces_escape_on_x11(escapes, monkeypatch):
    from app.mcp.computer_use import abort

    monkeypatch.setattr(safety, "_x11", lambda: True)
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    monkeypatch.setattr(safety, "_agent_escape_at", 0.0)
    keyboard = SimpleNamespace(press=lambda key: None, release=lambda key: None)
    inputs = safety.SafeInput("linux-esc", None, keyboard, escape_key="esc")
    inputs.press(keyboard, "esc")
    inputs.release(keyboard, "esc")
    assert linux.consume_agent_escape() is True
    assert linux.consume_agent_escape() is False
    # Linux counts presses; the time window used elsewhere is never armed.
    assert not safety._recent_agent_escape()
    inputs.press(keyboard, "enter")
    assert linux.consume_agent_escape() is False


def test_safe_input_does_not_announce_off_x11(escapes, monkeypatch):
    from app.mcp.computer_use import abort

    monkeypatch.setattr(safety, "_x11", lambda: False)
    monkeypatch.setattr(abort, "_HALTED", {})
    monkeypatch.setattr(abort, "_GENERATIONS", {})
    keyboard = SimpleNamespace(press=lambda key: None, release=lambda key: None)
    inputs = safety.SafeInput("win-esc", None, keyboard, escape_key="esc")
    inputs.press(keyboard, "esc")
    assert linux.consume_agent_escape() is False


def test_listener_stops_on_a_person_escape_but_not_the_agents(escapes, monkeypatch):
    stops = []
    created = {}

    class Listener:
        def __init__(self, on_press=None, **kwargs):
            created["on_press"] = on_press
            self._alive = True

        def start(self):
            pass

        def wait(self):
            pass

        def is_alive(self):
            return self._alive

        def stop(self):
            self._alive = False

    key = SimpleNamespace(esc="esc")
    monkeypatch.setitem(sys.modules, "pynput.keyboard", SimpleNamespace(Listener=Listener, Key=key))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(safety, "_listener", None)
    monkeypatch.setattr(safety.abort, "stop_active",
                        lambda reason, detail=None: stops.append(reason))
    assert safety.ensure_started() is True
    on_press = created["on_press"]
    assert on_press is safety._on_press

    linux.expect_agent_escape()
    on_press("esc", False)  # the agent's XTEST Escape
    assert stops == []
    on_press("esc", False)  # the person's, right after the agent's
    assert stops == ["escape"]
    on_press("esc", True)  # injected events never stop
    assert stops == ["escape"]
    # The time window other platforms use excuses nothing on Linux: only an
    # announced press does, so a person's Escape always stops the turn.
    safety.note_agent_escape()
    on_press("esc", False)
    assert stops == ["escape", "escape"]


# ---------------------------------------------------------------------------
# Clipboard (command construction only; nothing is run)
# ---------------------------------------------------------------------------

def test_clipboard_prefers_xclip():
    which = lambda name: f"/usr/bin/{name}"
    assert linux.clipboard_command(False, which) == ["xclip", "-selection", "clipboard", "-out"]
    assert linux.clipboard_command(True, which) == ["xclip", "-selection", "clipboard", "-in"]


def test_clipboard_falls_back_to_xsel():
    which = lambda name: "/usr/bin/xsel" if name == "xsel" else None
    assert linux.clipboard_command(False, which) == ["xsel", "--clipboard", "--output"]
    assert linux.clipboard_command(True, which) == ["xsel", "--clipboard", "--input"]


def test_clipboard_without_tools_names_them():
    with pytest.raises(RuntimeError, match="xclip or xsel"):
        linux.clipboard_command(False, lambda name: None)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_sdk_clipboard_children_scrub_cloud_credentials(monkeypatch, platform):
    from app.mcp import computer_use_sdk as sdk

    cloud_env = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_SECRET_ACCESS_KEY": "fake-aws-secret",
                 "ANTHROPIC_AUTH_TOKEN": "fake-gateway-token", "ANTHROPIC_BASE_URL": "https://fake.invalid"}
    for name, value in cloud_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(linux, "clipboard_command", lambda write: ["fake-clipboard"])
    calls = []
    def run(args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stdout="fake-clipboard-text")
    monkeypatch.setattr(sdk.subprocess, "run", run)
    assert sdk._clipboard_read() == "fake-clipboard-text"
    sdk._clipboard_write("fake-clipboard-text")
    assert len(calls) == 2
    for kwargs in calls:
        assert not set(kwargs["env"]) & cloud_env.keys()


def test_sdk_clipboard_uses_linux_command_without_waiting_on_pipes(monkeypatch):
    from app.mcp import computer_use_sdk as sdk

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux, "clipboard_command", lambda write: ["xclip", "w" if write else "r"])
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="pasted")
    monkeypatch.setattr(sdk.subprocess, "run", run)
    sdk._clipboard_write("héllo")
    args, kwargs = calls[0]
    assert args == ["xclip", "w"]
    assert kwargs["input"] == "héllo".encode()
    assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
    assert sdk._clipboard_read() == "pasted"
    assert calls[1][0] == ["xclip", "r"]


# ---------------------------------------------------------------------------
# Prompt guidance and the worker capability
# ---------------------------------------------------------------------------

def test_guidance_matches_the_platform(monkeypatch):
    from app.mcp import computer_use_sdk as sdk

    monkeypatch.setattr(sys, "platform", "linux")
    text = sdk.system_guidance()
    assert "Linux (X11)" in text and "launcher" in text and "polkit/sudo" in text
    assert "list_windows" not in text and "ui_tree" not in text
    for other in ("win32", "darwin"):
        monkeypatch.setattr(sys, "platform", other)
        text = sdk.system_guidance()
        assert "Linux" not in text and "polkit" not in text


def test_capability_reflects_gate_and_toggle(monkeypatch):
    from app.core import runner_capabilities as rc

    monkeypatch.setattr(scaling, "computer_use_quick_check", lambda: scaling.Availability(True, "ok"))
    monkeypatch.setattr("app.api.computer_use.get_computer_use_enabled", lambda: True)
    assert rc._detect_computer_use() is True
    monkeypatch.setattr("app.api.computer_use.get_computer_use_enabled", lambda: False)
    assert rc._detect_computer_use() is False
    monkeypatch.setattr("app.api.computer_use.get_computer_use_enabled", lambda: True)
    monkeypatch.setattr(scaling, "computer_use_quick_check",
                        lambda: scaling.Availability(False, "wayland", linux.WAYLAND_HELP))
    assert rc._detect_computer_use() is False


def test_worker_traits_name_computer_use():
    from app.core.agent_provider_models import _worker_traits

    assert "computer use" in _worker_traits({"capabilities": {"os": "linux", "has_display": True,
                                                              "computer_use": True, "remote_task": 1}})
    assert "computer use" not in _worker_traits({"capabilities": {"os": "linux", "remote_task": 1}})
