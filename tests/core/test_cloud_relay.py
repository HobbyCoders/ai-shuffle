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


# --- relay_queue_message (send-later to owning install) ----------------------


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """Minimal async context-manager httpx stand-in (see test_mobile_notify_bridge)."""

    def __init__(self, *, resp=None, exc=None, recorder=None):
        self._resp = resp
        self._exc = exc
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        if self._recorder is not None:
            self._recorder.update(url=url, json=json, headers=headers)
        if self._exc:
            raise self._exc
        return self._resp


def _online_worker(monkeypatch):
    monkeypatch.setattr(
        cloud_relay.database,
        "get_install",
        lambda i: _install(base_url="https://worker.example.com"),
    )
    monkeypatch.setattr(cloud_relay, "_cluster_headers", lambda: {"X-Cluster": "secret"})


async def test_relay_queue_message_posts_to_owner(this_install, monkeypatch):
    _online_worker(monkeypatch)
    rec: dict = {}
    monkeypatch.setattr(
        cloud_relay.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(resp=_FakeResp(200, {"ok": True}), recorder=rec),
    )

    session = {"id": "s1", "instance_id": "install-other"}
    await cloud_relay.relay_queue_message(session, "do the thing", "claude-code", "proj1")

    assert rec["url"] == "https://worker.example.com/api/v1/cluster/sessions/s1/queue_message"
    assert rec["json"] == {
        "content": "do the thing",
        "profile_id": "claude-code",
        "project_id": "proj1",
    }
    assert rec["headers"] == {"X-Cluster": "secret"}


async def test_relay_queue_message_offline_when_unreachable(this_install, monkeypatch):
    _online_worker(monkeypatch)
    monkeypatch.setattr(
        cloud_relay.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(exc=cloud_relay.httpx.ConnectError("boom")),
    )

    with pytest.raises(cloud_relay.InstallOffline):
        await cloud_relay.relay_queue_message(
            {"id": "s1", "instance_id": "install-other"}, "hi"
        )


async def test_relay_queue_message_raises_on_peer_error(this_install, monkeypatch):
    _online_worker(monkeypatch)
    monkeypatch.setattr(
        cloud_relay.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(resp=_FakeResp(500, {"detail": "nope"})),
    )

    with pytest.raises(cloud_relay.CloudRelayError):
        await cloud_relay.relay_queue_message(
            {"id": "s1", "instance_id": "install-other"}, "hi"
        )


async def test_relay_queue_message_refuses_local_session(this_install):
    # A local session must never relay to itself (would loop). resolve_target
    # raises before any HTTP call.
    with pytest.raises(cloud_relay.CloudRelayError):
        await cloud_relay.relay_queue_message(
            {"id": "s1", "instance_id": "install-self"}, "hi"
        )
