"""Unit tests for the cluster mobile-notification bridge.

A session event (question / permission / "done") is raised on the install that
*owns* the session, but a phone is connected to a single install (the hub). The
bridge fans each notification out to every other online, reachable install so
the hub's phone gets notified about activity anywhere in the cluster. The
receiving endpoint delivers locally only and must never re-fan-out (no loop).

Everything is mocked at the registry / HTTP boundary — no DB, no network.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import cloud_relay


# ---------------------------------------------------------------------------
# online_peers_with_url — who gets fanned to
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(monkeypatch):
    """Pin this install's id and program the installs registry."""
    rows: list = []
    monkeypatch.setattr(cloud_relay.database, "get_installs", lambda: list(rows))
    with patch("app.core.config.settings") as mock_settings:
        mock_settings.effective_instance_id = "me-install"
        yield rows


def _install(iid, online=True, base_url="http://peer:1455"):
    return {"id": iid, "device_name": iid, "online": online, "base_url": base_url}


def test_online_peers_excludes_self(registry):
    registry.append(_install("me-install"))
    registry.append(_install("peer-a"))
    peers = cloud_relay.online_peers_with_url()
    assert [p["id"] for p in peers] == ["peer-a"]


def test_online_peers_excludes_offline_and_view_only(registry):
    registry.append(_install("peer-a"))                       # qualifies
    registry.append(_install("peer-off", online=False))       # offline
    registry.append(_install("peer-vo", base_url=None))       # view-only
    registry.append(_install("peer-blank", base_url="  "))    # blank url
    peers = cloud_relay.online_peers_with_url()
    assert [p["id"] for p in peers] == ["peer-a"]


def test_online_peers_empty_for_single_install(registry):
    registry.append(_install("me-install"))
    assert cloud_relay.online_peers_with_url() == []


# ---------------------------------------------------------------------------
# fanout_mobile_notify — best-effort relay to every peer
# ---------------------------------------------------------------------------

async def test_fanout_noop_when_no_peers(monkeypatch):
    monkeypatch.setattr(cloud_relay, "online_peers_with_url", lambda: [])
    posted = []
    monkeypatch.setattr(cloud_relay, "_post_mobile_notify",
                        AsyncMock(side_effect=lambda url, payload: posted.append(url)))
    n = await cloud_relay.fanout_mobile_notify("t", "b", {"type": "x"})
    assert n == 0
    assert posted == []


async def test_fanout_posts_to_every_peer(monkeypatch):
    monkeypatch.setattr(
        cloud_relay, "online_peers_with_url",
        lambda: [_install("peer-a", base_url="http://a:1455"),
                 _install("peer-b", base_url="http://b:1455")],
    )
    posted = []

    async def fake_post(base_url, payload):
        posted.append((base_url, payload))

    monkeypatch.setattr(cloud_relay, "_post_mobile_notify", fake_post)
    n = await cloud_relay.fanout_mobile_notify("Title", "Body", {"type": "user_question"})
    assert n == 2
    assert sorted(u for u, _ in posted) == ["http://a:1455", "http://b:1455"]
    # Payload shape forwarded verbatim to each peer.
    for _, payload in posted:
        assert payload == {"title": "Title", "body": "Body", "data": {"type": "user_question"}}


async def test_fanout_swallows_registry_errors(monkeypatch):
    def boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(cloud_relay, "online_peers_with_url", boom)
    # Must not raise — fan-out is best-effort on a hot path.
    assert await cloud_relay.fanout_mobile_notify("t", "b", None) == 0


async def test_post_mobile_notify_targets_endpoint_with_cluster_auth(monkeypatch):
    """One peer POST hits /cluster/mobile-notify with the shared-secret headers."""
    calls = {}

    class FakeResponse:
        def raise_for_status(self):  # 200 OK
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            calls["timeout"] = k.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            calls["url"] = url
            calls["json"] = json
            calls["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(cloud_relay.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(cloud_relay, "_cluster_headers", lambda: {"Authorization": "Bearer secret"})

    await cloud_relay._post_mobile_notify("http://peer:1455/", {"title": "t", "body": "b", "data": {}})

    assert calls["url"] == "http://peer:1455/api/v1/cluster/mobile-notify"
    assert calls["headers"] == {"Authorization": "Bearer secret"}
    assert calls["timeout"] == cloud_relay.MOBILE_NOTIFY_TIMEOUT


async def test_post_mobile_notify_swallows_network_error(monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise cloud_relay.httpx.ConnectError("unreachable")

    monkeypatch.setattr(cloud_relay.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(cloud_relay, "_cluster_headers", lambda: {})
    # An unreachable peer must not raise — other peers and the caller proceed.
    await cloud_relay._post_mobile_notify("http://dead:1455", {"title": "t"})


# ---------------------------------------------------------------------------
# deliver_local_notification — local-only delivery primitive
# ---------------------------------------------------------------------------

async def test_deliver_local_publishes_to_hub_and_devices(monkeypatch):
    from app.services import push_notification as pn
    import app.services.mobile_push_hub as hub_mod

    publish = AsyncMock()
    monkeypatch.setattr(hub_mod.mobile_push_hub, "publish", publish)
    send_all = AsyncMock()
    monkeypatch.setattr(pn.push_notification_service, "send_to_all_devices", send_all)

    await pn.deliver_local_notification("Title", "Body", {"type": "session_done"})

    publish.assert_awaited_once()
    event = publish.await_args.args[0]
    assert event["type"] == "session_done"
    assert event["title"] == "Title" and event["body"] == "Body"
    send_all.assert_awaited_once()


# ---------------------------------------------------------------------------
# /cluster/mobile-notify endpoint — loop-safe (delivers local, never fans out)
# ---------------------------------------------------------------------------

async def test_mobile_notify_endpoint_delivers_local_and_never_fans_out(monkeypatch):
    from app.api import cluster as cluster_api
    from app.services import push_notification as pn

    delivered = {}
    fanned = {"called": False}

    async def fake_deliver(title, body, data):
        delivered.update(title=title, body=body, data=data)

    async def fake_fanout(*a, **k):  # would indicate a relay loop
        fanned["called"] = True
        return 1

    monkeypatch.setattr(pn, "deliver_local_notification", fake_deliver)
    monkeypatch.setattr(cloud_relay, "fanout_mobile_notify", fake_fanout)

    body = cluster_api.ClusterMobileNotifyRequest(
        title="Question", body="Need approval", data={"type": "permission_request"}
    )
    result = await cluster_api.cluster_mobile_notify(body=body, install_id="peer-b")

    assert result == {"ok": True}
    assert delivered == {
        "title": "Question",
        "body": "Need approval",
        "data": {"type": "permission_request"},
    }
    # The receiving install must deliver locally only — re-fanning would loop.
    assert fanned["called"] is False


async def test_mobile_notify_endpoint_defaults_blank_title(monkeypatch):
    from app.api import cluster as cluster_api
    from app.services import push_notification as pn

    seen = {}

    async def fake_deliver(title, body, data):
        seen.update(title=title, body=body)

    monkeypatch.setattr(pn, "deliver_local_notification", fake_deliver)
    body = cluster_api.ClusterMobileNotifyRequest()  # all blank
    await cluster_api.cluster_mobile_notify(body=body, install_id="")
    assert seen["title"] == "AI Shuffle"
    assert seen["body"] == ""
