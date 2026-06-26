"""Unit tests for the cluster relay client (registry-based cross-install control).

The legacy per-session offload client (URL + API key) was removed; relaying now
resolves the owning install from the ``installs`` registry by ``instance_id``.
These tests cover the pure resolution helpers without hitting the network.
"""

import pytest

from app.core import cloud_relay
from app.core.config import settings


@pytest.fixture
def this_install(monkeypatch):
    """Pin this install's id so remote/local resolution is deterministic."""
    # effective_instance_id is a property; replace it with a plain class attr.
    monkeypatch.setattr(type(settings), "effective_instance_id", "install-self")
    return "install-self"


def _install(**over):
    base = {"device_name": "Worker", "base_url": "https://worker.example.com", "online": True}
    base.update(over)
    return base


# --- is_remote_session -------------------------------------------------------


def test_local_session_is_not_remote(this_install):
    assert cloud_relay.is_remote_session({"instance_id": "install-self"}) is False


def test_null_instance_is_not_remote(this_install):
    # Legacy rows with no owner count as local.
    assert cloud_relay.is_remote_session({"instance_id": None}) is False


def test_foreign_instance_is_remote(this_install):
    assert cloud_relay.is_remote_session({"instance_id": "install-other"}) is True


# --- resolve_target ----------------------------------------------------------


def test_resolve_target_local_raises(this_install):
    with pytest.raises(cloud_relay.CloudRelayError):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "install-self"})


def test_resolve_target_unregistered_is_offline(this_install, monkeypatch):
    monkeypatch.setattr(cloud_relay.database, "get_install", lambda i: None)
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "install-other"})


def test_resolve_target_offline_device(this_install, monkeypatch):
    monkeypatch.setattr(cloud_relay.database, "get_install", lambda i: _install(online=False))
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "install-other"})


def test_resolve_target_view_only_no_url(this_install, monkeypatch):
    monkeypatch.setattr(cloud_relay.database, "get_install", lambda i: _install(base_url=""))
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_target({"id": "s1", "instance_id": "install-other"})


def test_resolve_target_returns_base_url_and_shared_id(this_install, monkeypatch):
    monkeypatch.setattr(
        cloud_relay.database,
        "get_install",
        lambda i: _install(base_url="https://worker.example.com/"),
    )
    base_url, sid = cloud_relay.resolve_target({"id": "s1", "instance_id": "install-other"})
    assert base_url == "https://worker.example.com"  # trailing slash stripped
    assert sid == "s1"  # session id is shared cluster-wide


# --- resolve_install_base_url (offload target picker) ------------------------


def test_resolve_install_base_url_ok(monkeypatch):
    monkeypatch.setattr(cloud_relay.database, "get_install", lambda i: _install())
    assert cloud_relay.resolve_install_base_url("install-x") == "https://worker.example.com"


def test_resolve_install_base_url_unregistered(monkeypatch):
    monkeypatch.setattr(cloud_relay.database, "get_install", lambda i: None)
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_install_base_url("install-x")


def test_resolve_install_base_url_view_only(monkeypatch):
    monkeypatch.setattr(cloud_relay.database, "get_install", lambda i: _install(base_url=""))
    with pytest.raises(cloud_relay.InstallOffline):
        cloud_relay.resolve_install_base_url("install-x")
