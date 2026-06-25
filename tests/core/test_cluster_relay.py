"""Unit tests for the registry-aware cross-install relay resolution.

On a shared database a session's ``instance_id`` names the install that owns the
running subprocess. These tests cover how the relay turns that id into a
peer-reachable ``base_url`` (or refuses to, when the owner is local / offline /
view-only). Everything is mocked at the registry boundary — no DB, no network.
"""

from unittest.mock import patch

import pytest

from app.core import cloud_relay


@pytest.fixture
def registry(monkeypatch):
    """Pin this install's id and yield a programmable ``get_install`` store."""
    installs: dict = {}
    monkeypatch.setattr(cloud_relay.database, "get_install", lambda iid: installs.get(iid))
    # The relay reads settings.effective_instance_id via an in-function import,
    # so patching the module attribute is enough.
    with patch("app.core.config.settings") as mock_settings:
        mock_settings.effective_instance_id = "me-install"
        yield installs


# ---------------------------------------------------------------------------
# is_remote_session
# ---------------------------------------------------------------------------

def test_is_remote_session_treats_null_and_self_as_local(registry):
    assert cloud_relay.is_remote_session({"instance_id": None}) is False
    assert cloud_relay.is_remote_session({"instance_id": "me-install"}) is False


def test_is_remote_session_true_for_foreign_owner(registry):
    assert cloud_relay.is_remote_session({"instance_id": "other-install"}) is True


# ---------------------------------------------------------------------------
# resolve_target (live control plane)
# ---------------------------------------------------------------------------

def test_resolve_target_refuses_local_session(registry):
    # Owned here (or legacy NULL) → caller should run it locally, not relay.
    with pytest.raises(cloud_relay.CloudRelayError):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "me-install"})
    with pytest.raises(cloud_relay.CloudRelayError):
        cloud_relay.resolve_target({"id": "s1", "instance_id": None})


def test_resolve_target_unregistered_owner_is_offline(registry):
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "ghost"})


def test_resolve_target_offline_owner(registry):
    registry["box"] = {"device_name": "Box", "online": False, "base_url": "http://box:1455"}
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "box"})


def test_resolve_target_view_only_owner(registry):
    registry["box"] = {"device_name": "Box", "online": True, "base_url": None}
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "box"})


def test_resolve_target_online_returns_base_url_and_shared_id(registry):
    registry["box"] = {"device_name": "Box", "online": True, "base_url": "http://box:1455/"}
    base_url, sid = cloud_relay.resolve_target({"id": "s1", "instance_id": "box"})
    assert base_url == "http://box:1455"  # trailing slash stripped
    assert sid == "s1"  # the shared session id is returned unchanged


# ---------------------------------------------------------------------------
# resolve_install_base_url (offload target)
# ---------------------------------------------------------------------------

def test_resolve_install_base_url_online(registry):
    registry["box"] = {"device_name": "Box", "online": True, "base_url": "http://box:1455/"}
    assert cloud_relay.resolve_install_base_url("box") == "http://box:1455"


def test_resolve_install_base_url_unregistered(registry):
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_install_base_url("ghost")


def test_resolve_install_base_url_offline(registry):
    registry["box"] = {"device_name": "Box", "online": False, "base_url": "http://box:1455"}
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_install_base_url("box")


def test_resolve_install_base_url_view_only(registry):
    registry["box"] = {"device_name": "Box", "online": True, "base_url": ""}
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_install_base_url("box")
