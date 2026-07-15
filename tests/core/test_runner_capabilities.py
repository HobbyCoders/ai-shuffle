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
    assert set(caps.keys()) == {"os", "has_display", "has_chrome", "docker", "account_ids"}
    assert caps["os"] in ("windows", "linux", "darwin")
    assert isinstance(caps["has_display"], bool)
    assert isinstance(caps["has_chrome"], bool)
    assert isinstance(caps["docker"], bool)
    assert isinstance(caps["account_ids"], list)


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


def test_account_ids_default_empty_when_lookup_raises():
    with patch(
        "app.db.claude_accounts.get_all_claude_accounts",
        side_effect=RuntimeError("db down"),
    ):
        assert rc._detect_account_ids() == []


def test_account_ids_are_ids_only():
    fake = [{"id": "acc-1", "name": "A", "email": "a@x"}, {"id": "acc-2"}, {"name": "no-id"}]
    with patch("app.db.claude_accounts.get_all_claude_accounts", return_value=fake):
        assert rc._detect_account_ids() == ["acc-1", "acc-2"]


def test_detect_capabilities_never_raises_on_total_failure():
    def _boom(*a, **k):
        raise RuntimeError("everything is broken")

    with patch.object(rc.shutil, "which", side_effect=_boom), \
            patch.object(rc.os.path, "exists", side_effect=_boom), \
            patch("app.db.claude_accounts.get_all_claude_accounts", side_effect=_boom):
        caps = rc.detect_capabilities()  # must not raise
        assert caps["has_chrome"] is False
        assert caps["docker"] is False
        assert caps["account_ids"] == []


# ---------------------------------------------------------------------------
# cluster wiring — register_self + heartbeat_self write the merged blob.
# ---------------------------------------------------------------------------

_SENTINEL_CAPS = {
    "os": "linux",
    "has_display": False,
    "has_chrome": True,
    "docker": True,
    "account_ids": ["acc-1"],
}


def test_build_capabilities_merges_source_and_detected():
    with patch("app.core.runner_capabilities.detect_capabilities", return_value=dict(_SENTINEL_CAPS)):
        caps = cluster._build_capabilities("config")
    assert caps["advertise_url_source"] == "config"
    assert caps["has_chrome"] is True and caps["docker"] is True
    assert caps["account_ids"] == ["acc-1"]


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
    assert caps["account_ids"] == ["acc-1"]


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
    assert caps["account_ids"] == ["acc-1"]
