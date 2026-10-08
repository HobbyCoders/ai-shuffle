"""Unit tests for app/core/runner_capabilities.py and its wiring into the
cluster registry (V2 Phase 4).

detect_capabilities() is a best-effort, never-raising snapshot of what this
runner can do; cluster.register_self / cluster.heartbeat_self merge it into the
`installs.capabilities` blob (keeping the existing advertise_url_source key).
"""

from unittest.mock import patch

from app.core import cluster
from app.core import runner_capabilities as rc


# ---------------------------------------------------------------------------
# detect_capabilities — shape + value domains.
# ---------------------------------------------------------------------------

def test_shape_has_all_keys_and_types():
    caps = rc.detect_capabilities()
    assert set(caps.keys()) == {"os", "has_display", "computer_use", "has_chrome", "docker",
                                "claude_cli_signed_in", "provider_kinds", "remote_task", "move_wakes",
                                "terminal"}
    assert caps["os"] in ("windows", "linux", "darwin")
    assert isinstance(caps["has_display"], bool)
    assert isinstance(caps["computer_use"], bool)
    assert isinstance(caps["has_chrome"], bool)
    assert isinstance(caps["docker"], bool)
    assert isinstance(caps["claude_cli_signed_in"], bool)
    assert isinstance(caps["provider_kinds"], list)
    assert caps["remote_task"] == rc.REMOTE_TASK_VERSION == 4
    assert caps["move_wakes"] == rc.MOVE_WAKES_VERSION == 3
    assert caps["terminal"] in (0, 1)


def test_terminal_capability_off_without_pty():
    from app.core import terminals

    with patch.object(terminals, "pty_supported", return_value=False):
        assert rc.detect_capabilities()["terminal"] == 0
    with patch.object(terminals, "pty_supported", return_value=True), \
            patch.object(terminals, "available_shells", return_value=[{"name": "sh", "path": "/bin/sh", "default": True}]):
        assert rc.detect_capabilities()["terminal"] == 1


def test_os_matches_platform():
    with patch.object(rc.sys, "platform", "win32"):
        assert rc._detect_os() == "windows"
    with patch.object(rc.sys, "platform", "darwin"):
        assert rc._detect_os() == "darwin"
    with patch.object(rc.sys, "platform", "linux"):
        assert rc._detect_os() == "linux"


# ---------------------------------------------------------------------------
# Safe defaults on detection failure — every probe swallows and defaults.
# ---------------------------------------------------------------------------

def test_chrome_and_docker_default_false_when_probe_raises():
    def _boom(*a, **k):
        raise RuntimeError("PATH lookup exploded")

    with patch.object(rc.shutil, "which", side_effect=_boom), \
            patch.object(rc.os.path, "exists", side_effect=_boom):
        assert rc._detect_has_chrome() is False
        assert rc._detect_docker() is False


def test_has_display_defaults_false_when_env_read_raises():
    class _BoomEnv:
        def get(self, *a, **k):
            raise RuntimeError("env exploded")

    with patch.object(rc.sys, "platform", "linux"), \
            patch.object(rc.os, "environ", _BoomEnv()):
        assert rc._detect_has_display() is False


def test_claude_cli_signed_in_comes_from_the_cached_probe(monkeypatch):
    from app.core import claude_cli_status as cs

    def _no_probe(*a, **k):
        raise AssertionError("capability detection must not run the CLI")
    monkeypatch.setattr(cs, "probe", _no_probe)
    assert rc._detect_claude_cli_signed_in() is False  # nothing probed yet
    monkeypatch.setattr(cs, "_cached", cs.ClaudeCliStatus(installed=True, signed_in=True))
    assert rc._detect_claude_cli_signed_in() is True
    monkeypatch.setattr(cs, "_cached", cs.ClaudeCliStatus(installed=True, signed_in=False))
    assert rc._detect_claude_cli_signed_in() is False


def test_detect_capabilities_never_raises_on_total_failure():
    def _boom(*a, **k):
        raise RuntimeError("everything is broken")

    with patch.object(rc.shutil, "which", side_effect=_boom), \
            patch.object(rc.os.path, "exists", side_effect=_boom), \
            patch("app.core.claude_cli_status.peek", side_effect=_boom):
        caps = rc.detect_capabilities()  # must not raise
        assert caps["has_chrome"] is False
        assert caps["docker"] is False
        assert caps["claude_cli_signed_in"] is False


def test_provider_kinds_default_empty_when_the_database_fails():
    def _boom(*a, **k):
        raise RuntimeError("no database")

    with patch("app.db.base.get_db", side_effect=_boom):
        assert rc._detect_provider_kinds() == []


# ---------------------------------------------------------------------------
# cluster wiring — register_self + heartbeat_self write the merged blob.
# ---------------------------------------------------------------------------

_SENTINEL_CAPS = {
    "os": "linux",
    "has_display": False,
    "has_chrome": True,
    "docker": True,
    "claude_cli_signed_in": True,
}


def test_build_capabilities_merges_source_and_detected():
    with patch("app.core.runner_capabilities.detect_capabilities", return_value=dict(_SENTINEL_CAPS)):
        caps = cluster._build_capabilities("config")
    assert caps["advertise_url_source"] == "config"
    assert caps["has_chrome"] is True and caps["docker"] is True
    assert caps["claude_cli_signed_in"] is True


def test_build_capabilities_degrades_to_base_on_detection_error():
    with patch("app.core.runner_capabilities.detect_capabilities", side_effect=RuntimeError("boom")):
        caps = cluster._build_capabilities("guess")
    # The existing key survives; detection failure never blocks registration.
    assert caps == {"advertise_url_source": "guess"}


def test_register_self_writes_detected_capabilities():
    captured = {}

    def _fake_register(**kwargs):
        captured.update(kwargs)
        return None

    with patch.object(cluster, "advertise_url", return_value=("http://box:8000", "config")), \
            patch("app.core.runner_capabilities.detect_capabilities", return_value=dict(_SENTINEL_CAPS)), \
            patch.object(cluster.database, "register_install", side_effect=_fake_register):
        cluster.register_self()

    caps = captured["capabilities"]
    assert caps["advertise_url_source"] == "config"
    assert caps["has_chrome"] is True
    assert caps["claude_cli_signed_in"] is True


def test_heartbeat_self_refreshes_capabilities():
    captured = {}

    def _fake_heartbeat(install_id, capabilities):
        captured["install_id"] = install_id
        captured["capabilities"] = capabilities

    with patch.object(cluster, "advertise_url", return_value=("http://box:8000", "config")), \
            patch("app.core.runner_capabilities.detect_capabilities", return_value=dict(_SENTINEL_CAPS)), \
            patch.object(cluster.database, "touch_install_heartbeat", side_effect=_fake_heartbeat):
        cluster.heartbeat_self()

    assert captured["install_id"] == cluster.settings.effective_instance_id
    caps = captured["capabilities"]
    assert caps["advertise_url_source"] == "config"
    assert caps["has_chrome"] is True and caps["docker"] is True
    assert caps["claude_cli_signed_in"] is True
