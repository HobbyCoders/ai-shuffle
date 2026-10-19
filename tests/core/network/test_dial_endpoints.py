"""Which of a peer's addresses are dialed: its account tunnel alone when it has one.

A worker signed in to a Shuffle account advertises its https tunnel and its
peers then dial nothing else, even when its plain-http LAN address is known
and both sit on one LAN. Plain http is dialed only for a worker without a
tunnel, and a tunnel that does not answer surfaces as the peer being
unreachable instead of quietly falling back to the LAN address. Plain-http
invites are accepted only on a path the owner controls.
"""
from types import SimpleNamespace

import httpx
import pytest

from app.db import network as store
from app.network import control, endpoints, engine, enrollment, runners, startup, transport
from tests.core.network.harness import exchange, join, owner_pref, set_owner_pref
from tests.core.network.test_transport import ServedBy

LAN, TUNNEL = "http://a.local:8000", "https://a.tunnel.example"


@pytest.fixture(autouse=True)
def _reset_enrollment_task():
    yield
    enrollment._task = None


class ServedAt(ServedBy):
    """Worker A answering at several base URLs, each of which can be switched off."""

    def __init__(self, server, bases):
        super().__init__(server, bases[0])
        self.bases = list(bases)
        self.reachable = set(bases)
        self.urls = []

    def handler(self, request):
        url = str(request.url)
        self.urls.append(url.split("?")[0])
        base = next((b for b in self.bases if url.startswith(b)), None)
        if base is None or base not in self.reachable:
            return httpx.Response(502, json={"detail": "unreachable"})
        self.base_url = base
        return super().handler(request)


def _advertise(monkeypatch, own):
    """Each worker advertises the list under its device id (``_own_endpoints`` is per context)."""
    monkeypatch.setattr(engine, "_own_endpoints",
                        lambda: list(own.get(getattr(engine.context(), "device_id", None), [])))


def _route_httpx(monkeypatch, served):
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: real_async(**{**kw, "transport": served.transport()}))
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real_sync(**{**kw, "transport": served.transport()}))


def test_dial_endpoints_uses_the_tunnel_alone_when_one_is_advertised():
    assert endpoints.dial_endpoints([LAN, TUNNEL]) == [TUNNEL]
    assert endpoints.dial_endpoints([TUNNEL, LAN]) == [TUNNEL]
    assert endpoints.dial_endpoints([LAN]) == [LAN]
    assert endpoints.dial_endpoints([LAN, "https://a.example.com/", "https://b.example.com"]) == [
        "https://a.example.com", "https://b.example.com"]
    assert endpoints.dial_endpoints(["http://169.254.1.1", "http://user:pw@a.local", LAN]) == [LAN]
    assert endpoints.dial_endpoints(None) == []
    # The stored row and the presence record are combined before the rule applies.
    assert endpoints.peer_endpoints({"endpoints": [LAN]}, {"endpoints": [TUNNEL, LAN]}) == [TUNNEL]
    assert endpoints.peer_endpoints({"endpoints": [LAN]}, None) == [LAN]
    assert endpoints.peer_endpoints(None, {"endpoints": "not-a-list"}) == []
    assert endpoints.peer_endpoints(None, None) == []


@pytest.mark.parametrize("dot", ["。", "．", "｡"], ids=["ideographic", "fullwidth", "halfwidth"])
@pytest.mark.asyncio
async def test_unicode_dot_peer_addresses_are_filtered_before_any_request(dot):
    url = f"http://169{dot}254{dot}169{dot}254:80"
    assert endpoints.peer_endpoints({"endpoints": [url]}, None) == []
    assert endpoints.peer_endpoints(None, {"endpoints": [url]}) == []
    assert endpoints.dial_endpoints([url]) == []
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(403)
    # Also exercise the raw-list constructor: no challenge may be sent before
    # the stricter token transcript canonicalizer gets a chance to reject it.
    link = transport.PeerLink("peer", [url])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(transport.PeerError):
            await link.authenticate(client, SimpleNamespace(device_id="local"))
    assert requests == []


@pytest.mark.parametrize("url", ["https://faß.example", "https://K.example", "http://example.com:８０"])
def test_peer_authorities_require_ascii_before_normalizing(url):
    assert not endpoints.is_valid_endpoint(url)
    assert endpoints.dial_endpoints([url]) == []


@pytest.mark.parametrize("url, valid", [
    ("http://[2002:a9fe:a9fe::1]:8000", False),
    ("http://[2002:c0a8:114::1]:8000", True),
    ("https://[2002:808:808::1]", True),
])
def test_6to4_addresses_apply_the_embedded_ipv4_link_local_rule(url, valid):
    assert endpoints.is_valid_endpoint(url) is valid
    assert endpoints.dial_endpoints([url]) == ([url] if valid else [])


@pytest.mark.parametrize("url, local", [
    ("http://127.0.0.1:8000", True), ("http://localhost:8000", True), ("http://[::1]:8000", True),
    ("http://10.0.0.5:8000", True), ("http://192.168.1.20", True), ("http://172.16.4.4:8000", True),
    ("http://[fd00::1]:8000", True), ("http://169.254.1.1:8000", True), ("http://[fe80::1]:8000", True),
    ("http://[::ffff:10.0.0.5]:8000", True),
    ("http://100.64.3.2:8000", True), ("http://100.127.255.254", True), ("http://100.128.0.1", False),
    ("http://desk.tail1234.ts.net:8000", True), ("http://desk.local:8000", True),
    ("http://8.8.8.8:8000", False), ("http://[2001:4860:4860::8888]:8000", False),
    ("http://[::ffff:8.8.8.8]:8000", False), ("http://worker.example.com:8000", False), ("http://desk", False),
    ("https://worker.example.com", True), ("https://8.8.8.8", True), ("ftp://10.0.0.1", False), ("http://", False),
], ids=[
    "loopback-v4", "localhost", "loopback-v6", "rfc1918-10", "rfc1918-192", "rfc1918-172", "ula", "link-local-v4",
    "link-local-v6", "mapped-private", "tailscale-low", "tailscale-high", "past-tailscale", "ts-net", "mdns",
    "public-v4", "public-v6", "mapped-public", "public-name", "bare-name", "https-name", "https-ip", "ftp", "no-host",
])
def test_plain_http_is_accepted_only_on_a_local_path(url, local):
    assert endpoints.plain_http_is_local(url) is local


async def test_join_over_plain_http_is_refused_outside_a_local_path(workers, monkeypatch):
    b = workers("b")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(502, json={})

    real_async = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **kw: real_async(**{**kw, "transport": httpx.MockTransport(handler)}))
    with b.active():
        for public in ("http://8.8.8.8:8000", "http://worker.example.com:8000", "http://[2001:4860:4860::8888]:8000"):
            with pytest.raises(enrollment.EnrollmentError, match="home network or a VPN"):
                await enrollment.start_enrollment(endpoint_url=public, code="ABCD-EFGH-JKLM")
        assert calls == []  # refused before the code left this worker
        local = ("http://10.0.0.5:8000", "http://100.64.3.2:8000", "http://desk.local:8000",
                 "http://desk.tail1234.ts.net:8000", "https://worker.example.com")
        for address in local:
            with pytest.raises(enrollment.EnrollmentError, match="HTTP 502"):
                await enrollment.start_enrollment(endpoint_url=address, code="ABCD-EFGH-JKLM")
        assert len(calls) == len(local)


async def test_tunnel_appearing_later_switches_the_link_and_never_falls_back(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = ServedAt(a, [LAN, TUNNEL])
    _route_httpx(monkeypatch, served)
    own = {a.device_id: [LAN], b.device_id: ["http://b.local:8000"]}
    _advertise(monkeypatch, own)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)

    # Paired by code on the LAN: no tunnel anywhere, plain http is dialed.
    served.reachable = {LAN}
    with b.active():
        store.touch_member(a.device_id, endpoints=[LAN])
        transport.reset_for_tests()
        async with httpx.AsyncClient() as client:
            results = await transport.sync_pass(client)
        assert a.device_id in results
        link = transport._links[a.device_id]
        assert link.endpoints == [LAN] and link.base_url == LAN

        # A signs in: its tunnel is advertised on the very next exchange. B's
        # link switches inside that pass, without re-pairing: the exchange that
        # carried the news went over the LAN, everything after it over the tunnel.
        own[a.device_id] = [TUNNEL, LAN]
        served.reachable = {LAN, TUNNEL}
        served.urls.clear()
        set_owner_pref(b, "after-tunnel", 1)
        async with httpx.AsyncClient() as client:
            results = await transport.sync_pass(client)
        assert results[a.device_id]["sent"] >= 1
        assert link.endpoints == [TUNNEL] and link.base_url == TUNNEL
        first_tunnel = next(i for i, u in enumerate(served.urls) if u.startswith(TUNNEL))
        assert all(u.startswith(TUNNEL) for u in served.urls[first_tunnel:])
        assert any(u.startswith(TUNNEL) and u.endswith("/peer/ops") for u in served.urls)
        assert store.get_member(a.device_id)["endpoints"] == [TUNNEL, LAN]
        directory = runners.directory()["runners"]
        assert [r["base_url"] for r in directory if r["device_id"] == a.device_id] == [TUNNEL]
        assert [r["endpoints"] for r in directory if r["device_id"] == a.device_id] == [[TUNNEL]]
    assert owner_pref(a, "after-tunnel") == 1

    # The tunnel stops answering while the LAN address is still there: A is
    # unreachable (sync and control alike); its LAN address is never dialed.
    served.reachable = {LAN}
    served.urls.clear()
    with b.active():
        link.backoff_until = 0.0
        set_owner_pref(b, "while-tunnel-down", 1)
        async with httpx.AsyncClient() as client:
            results = await transport.sync_pass(client)
        assert a.device_id not in results
        assert "HTTP 502" in (link.last_error or "") and link.base_url != LAN
        assert [p["last_error"] for p in store.list_peers() if p["device_id"] == a.device_id] == [link.last_error]
        with pytest.raises(control.RunnerUnreachable):
            await control.call(a.device_id, "ping", {})
        # A fresh link built the way control and the browser panel build one selects the tunnel too.
        transport._links.pop(a.device_id)
        with pytest.raises(control.RunnerUnreachable):
            await control.call(a.device_id, "ping", {})
        assert transport._links[a.device_id].endpoints == [TUNNEL]
        assert served.urls and all(u.startswith(TUNNEL) for u in served.urls)
    assert owner_pref(a, "while-tunnel-down") is None

    # A signs out: its presence record withdraws the tunnel. However the
    # record arrives (here from A's own log), it also updates A's stored row,
    # so B returns to the LAN address: there is no account link any more.
    own[a.device_id] = [LAN]
    with a.active():
        engine.publish_presence(version="t")
    monkeypatch.setattr(engine, "_listeners", [startup._on_applied])
    exchange(a, b)
    served.urls.clear()
    with b.active():
        assert engine.presence_of(a.device_id)["endpoints"] == [LAN]
        assert store.get_member(a.device_id)["endpoints"] == [LAN]
        transport._links[a.device_id].backoff_until = 0.0
        async with httpx.AsyncClient() as client:
            results = await transport.sync_pass(client)
        assert results[a.device_id]["sent"] >= 1
        assert transport._links[a.device_id].endpoints == [LAN]
        assert all(u.startswith(LAN) for u in served.urls)
    assert owner_pref(a, "while-tunnel-down") == 1


async def test_own_address_change_is_announced_at_once(workers, monkeypatch):
    """The presence record carries a new or withdrawn tunnel on the next pass,
    not at the next presence interval, so peers that reach us switch too."""
    a, b = workers("a"), workers("b")
    own = {a.device_id: [LAN], b.device_id: ["http://b.local:8000"]}
    _advertise(monkeypatch, own)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        transport.reset_for_tests()
        engine.publish_presence(version="t")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502))) as client:
            await transport.sync_pass(client)  # seeds the last advertised list
            assert engine.presence_of(a.device_id)["endpoints"] == [LAN]
            own[a.device_id] = [TUNNEL, LAN]
            await transport.sync_pass(client)
            assert engine.presence_of(a.device_id)["endpoints"] == [TUNNEL, LAN]
            own[a.device_id] = [LAN]
            await transport.sync_pass(client)
            assert engine.presence_of(a.device_id)["endpoints"] == [LAN]
    exchange(a, b)
    with b.active():
        assert engine.presence_of(a.device_id)["endpoints"] == [LAN]


def test_enrollment_copies_the_snapshot_over_the_inviters_tunnel(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = ServedAt(a, [LAN, TUNNEL])
    _route_httpx(monkeypatch, served)
    _advertise(monkeypatch, {a.device_id: [TUNNEL, LAN], b.device_id: ["http://b.local:8000"]})
    with a.active():
        engine.create_network("Home", device_name="A")
        engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                            encryption_public=b.identity.encryption_public, name="B")
        certificate = [r["record"] for r in store.list_membership_records()
                       if r["record"].get("device_id") == b.device_id][0]
        response = engine.join_response(device_id=b.device_id, encryption_public=b.identity.encryption_public,
                                        certificate=certificate)
    assert response["issuer"]["endpoints"] == [TUNNEL, LAN]

    # The invite was accepted at the LAN address, but the inviter is signed in:
    # its settings are copied over the tunnel, and a tunnel that does not
    # answer fails the copy with a message that says so, instead of using the LAN.
    served.reachable = {LAN}
    with b.active():
        with pytest.raises(enrollment.EnrollmentError, match="encrypted tunnel"):
            enrollment.complete_enrollment(response, client=None, endpoint=LAN, device_name="B",
                                           legacy_instance_id=None)
        assert served.urls and all(u.startswith(TUNNEL) for u in served.urls)
        assert engine.context().bootstrap_state != engine.BOOTSTRAP_READY

    served.reachable = {LAN, TUNNEL}
    served.urls.clear()
    with b.active():
        enrollment.complete_enrollment(response, client=None, endpoint=LAN, device_name="B", legacy_instance_id=None)
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_READY
        assert store.get_member(a.device_id)["endpoints"] == [LAN, TUNNEL]
        assert any(u.endswith("/peer/state") for u in served.urls)
        assert all(u.startswith(TUNNEL) for u in served.urls)
