"""Direct LAN sync between workers of one account (#2169).

Workers are served in-process: an httpx MockTransport routes each base URL
(a worker's tunnel or its LAN address) into a TestClient of the network
router entered under that worker. Everything else is real: the sealed LAN
handshake and calls, device tokens on the tunnel, exchange, push, snapshot
copy and the hub-relayed admission.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import network as store
from app.network import engine, hub, lan, peer_snapshot, transport
from app.network.endpoints import dial_endpoints
from tests.core.network.harness import _ACTIVE, join, owner_pref, set_owner_pref
from tests.core.network.test_transport import _app

TUNNEL_A = "https://a.example.com"
LAN_A = "http://10.0.0.11:8000"
TUNNEL_B = "https://b.example.com"
LAN_B = "http://10.0.0.12:8000"
ORIGIN = {"Origin": "https://app.example.com"}  # the owner UI, as CSRF requires
FORWARDED = ("authorization", "content-type", "accept", lan.SESSION_HEADER.lower(), lan.SEQ_HEADER.lower())


class Net:
    """Base URL -> in-process worker, with per-address failures and a request log."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.failure: dict[str, str] = {}
        self.log: list[dict] = []
        self.client = TestClient(_app())
        self.tamper = False

    def serve(self, worker, *bases):
        for base in bases:
            self.routes[base] = worker
        return self

    def to(self, base: str) -> list[dict]:
        return [r for r in self.log if r["base"] == base]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        base = next((b for b in self.routes if url == b or url.startswith(b + "/")), None)
        if base is None:
            raise httpx.ConnectError("no route to host", request=request)
        path = url[len(base):]
        self.log.append({"base": base, "method": request.method, "path": path.split("?")[0],
                         "content": request.content, "headers": dict(request.headers)})
        failure = self.failure.get(base)
        if failure == "timeout":
            raise httpx.ConnectTimeout("timed out", request=request)
        if failure == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        if failure == "old" and path.startswith("/api/v1/network/peer/lan/"):
            return httpx.Response(404, json={"detail": "Not Found"})
        server = self.routes[base]
        with server.active():
            response = self.client.request(request.method, path, content=request.content,
                                           headers={k: v for k, v in request.headers.items()
                                                    if k.lower() in FORWARDED})
            if path.startswith(("/api/v1/network/peer/state", lan.CALL_PATH)):
                # This synchronous mock swaps process-wide globals between
                # workers: finish the server's snapshot capture before leaving.
                for key, job in list(peer_snapshot._jobs.items()):
                    if key[0] == str(server.data_dir):
                        try:
                            job.result()
                        except ValueError:
                            pass
        content = response.content
        if self.tamper and path == lan.CALL_PATH and response.status_code == 200:
            content = content[:-1] + bytes([content[-1] ^ 1])
        return httpx.Response(response.status_code, content=content,
                              headers={"content-type": response.headers.get("content-type", "application/json")})

    def client_for(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class Clock:
    """``time.monotonic`` that a test can move forward."""

    def __init__(self):
        self.real = time.monotonic
        self.offset = 0.0

    def __call__(self):
        return self.real() + self.offset


@pytest.fixture
def addresses(monkeypatch):
    """What each worker advertises, by worker name (``engine._own_endpoints``)."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "cors_origins", "https://app.example.com")
    table: dict[str, list[str]] = {}
    monkeypatch.setattr(engine, "_own_endpoints", lambda: list(table.get(_ACTIVE.get().name, [])))
    yield table
    hub.reset_for_tests()


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(time, "monotonic", fake)
    return fake


def _pair(workers, addresses):
    a, b = workers("a"), workers("b")
    addresses["a"] = [TUNNEL_A, LAN_A]
    addresses["b"] = [TUNNEL_B, LAN_B]
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    return a, b


def _lan_paths(net: Net, base: str) -> list[str]:
    return [r["path"] for r in net.to(base)]


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

def test_lan_candidates_keep_private_literal_addresses_only():
    urls = ["https://a.example.com", "http://10.0.0.5:8000", "http://192.168.1.4:8000/", "http://172.20.1.1:8000",
            "http://8.8.8.8:8000", "http://100.64.0.1:8000", "http://169.254.10.10:8000", "http://worker.local:8000",
            "http://[fd12:3456::1]:8000", "http://[2001:db8::1]:8000", "https://10.0.0.6:8443",
            "http://10.0.0.7:8000/path", "http://127.0.0.1:8000"]
    assert lan.lan_candidates(urls) == ["http://10.0.0.5:8000", "http://192.168.1.4:8000", "http://172.20.1.1:8000",
                                        "http://[fd12:3456::1]:8000"]
    assert lan.is_docker_bridge("http://172.17.0.2:8000") and not lan.is_docker_bridge("http://172.18.0.2:8000")


def test_owner_addresses_are_normalized_and_must_be_private():
    assert lan.normalize_owner_address("10.0.0.200:8001", default_port=8000) == "http://10.0.0.200:8001"
    assert lan.normalize_owner_address(" http://192.168.1.9 ", default_port=8000) == "http://192.168.1.9:8000"
    assert lan.normalize_owner_address("[fd00::7]:9000", default_port=8000) == "http://[fd00::7]:9000"
    for bad in ("8.8.8.8:8000", "https://10.0.0.1:8000", "nas.local:8000", "", "10.0.0.1:99999"):
        with pytest.raises(ValueError):
            lan.normalize_owner_address(bad, default_port=8000)


def test_advertising_hides_public_and_bridge_addresses_behind_a_tunnel():
    common = {"docker": True, "enabled": True, "extra": []}
    # Behind a tunnel: a public address the tunnel hides is never published.
    assert lan.advertised_addresses(tunnel=True, advertise="http://203.0.113.5:8000", source="config", **common) == []
    # Docker's guessed or observed bridge address is unreachable from other hosts.
    assert lan.advertised_addresses(tunnel=True, advertise="http://172.17.0.2:8000", source="observed", **common) == []
    assert lan.advertised_addresses(tunnel=True, advertise="http://172.17.0.2:8000", source="config",
                                    **common) == ["http://172.17.0.2:8000"]
    assert lan.advertised_addresses(tunnel=True, advertise="http://10.0.0.200:8001", source="observed",
                                    **common) == ["http://10.0.0.200:8001"]
    # Without a tunnel the address is the only way in: kept as before.
    assert lan.advertised_addresses(tunnel=False, advertise="http://203.0.113.5:8000", source="guess",
                                    **common) == ["http://203.0.113.5:8000"]
    # The owner's host address is added only while LAN-direct is on.
    extra = ["http://10.0.0.200:8001"]
    assert lan.advertised_addresses(tunnel=True, advertise=None, source="none", docker=True, enabled=True,
                                    extra=extra) == extra
    assert lan.advertised_addresses(tunnel=True, advertise=None, source="none", docker=True, enabled=False,
                                    extra=extra) == []


def test_own_endpoints_use_the_owner_setting(workers, monkeypatch):
    from app.core import cluster, managed_connection
    w = workers("solo")
    monkeypatch.setattr(managed_connection, "status", lambda: {"status": "connected", "url": TUNNEL_A})
    monkeypatch.setattr(cluster, "advertise_url", lambda: ("http://172.17.0.2:8000", "observed"))
    with w.active():
        monkeypatch.setattr("app.core.config.settings.local_mode", False)
        assert engine._own_endpoints() == [TUNNEL_A]
        lan.set_owner_addresses(["10.0.0.200:8001"])
        assert engine._own_endpoints() == [TUNNEL_A, "http://10.0.0.200:8001"]
        lan.set_lan_direct_enabled(False)
        assert engine._own_endpoints() == [TUNNEL_A]
        with pytest.raises(ValueError):
            lan.set_owner_addresses(["198.51.100.4:8000"])


# ---------------------------------------------------------------------------
# Direct path first, authenticated and sealed
# ---------------------------------------------------------------------------

async def test_lan_is_dialed_first_and_carries_no_bearer_or_clear_data(workers, addresses):
    a, b = _pair(workers, addresses)
    set_owner_pref(a, "secret_from_a", "alpha")
    set_owner_pref(b, "secret_from_b", "bravo")
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            assert link.endpoints == [TUNNEL_A] and link.lan_endpoints == [LAN_A]
            summary = await link.sync_once(client, engine.context())
            assert summary["received"] >= 1 and summary["sent"] >= 1
            assert link.route == "lan" and link.active_url == LAN_A and link.token is None
            transport._links[a.device_id] = link
            assert transport.route_of(a.device_id) == "lan"
            assert transport.link_status()[0]["route"] == "lan"
    assert owner_pref(b, "secret_from_a") == "alpha"
    assert owner_pref(a, "secret_from_b") == "bravo"
    # Nothing went through the tunnel, and nothing readable crossed the LAN.
    assert net.to(TUNNEL_A) == []
    lan_traffic = net.to(LAN_A)
    assert _lan_paths(net, LAN_A)[:2] == [lan.HELLO_PATH, lan.SESSION_PATH]
    assert set(_lan_paths(net, LAN_A)[2:]) == {lan.CALL_PATH}
    for request in lan_traffic:
        assert "authorization" not in request["headers"]
        assert b"secret_from" not in request["content"] and b"bravo" not in request["content"]
        assert b"watermarks" not in request["content"]


async def test_wrong_identity_on_a_lan_address_is_refused_before_signing(workers, addresses):
    a, b = _pair(workers, addresses)
    c = workers("c")
    addresses["c"] = [LAN_A]
    join(a, c)
    set_owner_pref(a, "via_tunnel", 1)
    # Another member answers at A's LAN address: it cannot prove it is A.
    net = Net().serve(a, TUNNEL_A).serve(c, LAN_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel" and link.lan is None
            assert "LanIdentityMismatch" in (link.lan_last_error or "")
    assert _lan_paths(net, LAN_A) == [lan.HELLO_PATH]  # B never sent its session signature there
    assert owner_pref(b, "via_tunnel") == 1


async def test_impostor_claiming_the_peer_id_fails_signature_check(workers, addresses):
    a, b = _pair(workers, addresses)
    c = workers("c")
    join(a, c)
    net = Net().serve(a, TUNNEL_A).serve(c, LAN_A)
    real = net.handler

    def forged(request):
        response = real(request)
        if str(request.url).endswith(lan.HELLO_PATH) and response.status_code == 200:
            body = json.loads(response.content)
            body["device_id"] = a.device_id  # claims A, signed by C's key
            return httpx.Response(200, json=body)
        return response

    net.handler = forged
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel"
            assert "could not prove" in (link.lan_last_error or "")
    assert _lan_paths(net, LAN_A) == [lan.HELLO_PATH]


async def test_tampered_or_replayed_lan_traffic_is_refused(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "lan"
            # Replaying a captured call is refused before it reaches a handler.
            call = next(r for r in net.log if r["path"] == lan.CALL_PATH)
            with a.active():
                replay = net.client.post(lan.CALL_PATH, content=call["content"],
                                         headers={lan.SESSION_HEADER: call["headers"][lan.SESSION_HEADER.lower()],
                                                  lan.SEQ_HEADER: call["headers"][lan.SEQ_HEADER.lower()]})
                assert replay.status_code == 401
                bad = net.client.post(lan.CALL_PATH, content=b"\x00" * 64,
                                      headers={lan.SESSION_HEADER: link.lan.id, lan.SEQ_HEADER: "999"})
                assert bad.status_code == 401
            # A reply altered on the wire does not verify: the tunnel takes over.
            net.tamper = True
            set_owner_pref(b, "after_tamper", True)
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel" and link.lan is None
    assert owner_pref(a, "after_tamper") is True


# ---------------------------------------------------------------------------
# Fallback, caching and re-probe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("failure", ["timeout", "refused"])
async def test_lan_failure_falls_back_at_once_and_returns_after_reprobe(workers, addresses, clock, failure):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    net.failure[LAN_A] = failure
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            set_owner_pref(b, "first", 1)
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel" and link.failures == 0
            assert link.lan_retry_at == pytest.approx(time.monotonic() + lan.LAN_REPROBE_SECONDS, abs=5)
            # The failed path is cached: the next passes do not try the LAN again.
            tried = len(net.to(LAN_A))
            await link.sync_once(client, engine.context())
            assert len(net.to(LAN_A)) == tried
            # The LAN answers again; after the re-probe interval sync moves back.
            net.failure.pop(LAN_A)
            clock.offset += lan.LAN_REPROBE_SECONDS + 1
            tunnel_before = len(net.to(TUNNEL_A))
            set_owner_pref(b, "second", 2)
            await link.sync_once(client, engine.context())
            assert link.route == "lan" and len(net.to(TUNNEL_A)) == tunnel_before
    assert owner_pref(a, "first") == 1 and owner_pref(a, "second") == 2


async def test_lan_dropping_mid_session_moves_the_same_request_to_the_tunnel(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "lan"
            net.failure[LAN_A] = "timeout"
            set_owner_pref(b, "while_lan_down", "x")
            summary = await link.sync_once(client, engine.context())  # no error surfaces
            assert summary["sent"] >= 1 and link.route == "tunnel"
    assert owner_pref(a, "while_lan_down") == "x"


async def test_both_paths_down_keeps_offline_backoff(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    net.failure[LAN_A] = "refused"
    net.failure[TUNNEL_A] = "refused"
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            with pytest.raises(transport.PeerConnectFailed):
                await link.sync_once(client, engine.context())
            assert link.route is None


async def test_restarted_peer_session_is_reopened_on_the_lan(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            first = link.lan.id
            lan.drop_sessions()  # A restarted and forgot its sessions
            await link.sync_once(client, engine.context())
            assert link.route == "lan" and link.lan.id != first
    assert net.to(TUNNEL_A) == []


async def test_changed_or_withdrawn_lan_address(workers, addresses, clock):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A, "http://10.0.0.21:8000")
    net.failure[LAN_A] = "refused"
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel" and link.lan_retry_at > time.monotonic()
            # A new address is probed at once, without waiting for the re-probe.
            link.update_endpoints([TUNNEL_A, "http://10.0.0.21:8000"])
            assert link.lan_retry_at == 0.0
            addresses["a"] = [TUNNEL_A, "http://10.0.0.21:8000"]
            await link.sync_once(client, engine.context())
            assert link.route == "lan" and link.active_url == "http://10.0.0.21:8000"
            # A withdrawn address stops being dialed, even with a session open.
            link.update_endpoints([TUNNEL_A])
            addresses["a"] = [TUNNEL_A]
            assert link.lan is None and link.lan_endpoints == []
            before = len(net.to("http://10.0.0.21:8000"))
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel" and len(net.to("http://10.0.0.21:8000")) == before


# ---------------------------------------------------------------------------
# Mixed versions and the kill switch
# ---------------------------------------------------------------------------

async def test_older_peer_without_lan_routes_stays_on_the_tunnel(workers, addresses, clock):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    net.failure[LAN_A] = "old"  # A predates LAN sessions: 404
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            set_owner_pref(b, "to_old", 1)
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel"
            await link.sync_once(client, engine.context())
            assert _lan_paths(net, LAN_A) == [lan.HELLO_PATH]  # asked once per re-probe interval
    assert owner_pref(a, "to_old") == 1


async def test_older_peer_that_advertises_no_lan_address_is_never_probed(workers, addresses):
    a, b = _pair(workers, addresses)
    addresses["a"] = [TUNNEL_A]
    net = Net().serve(a, TUNNEL_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A])
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel" and link.lan_endpoints == []
    assert all(not r["path"].startswith("/api/v1/network/peer/lan/") for r in net.log)


async def test_older_client_reaches_a_new_worker_over_the_tunnel(workers, addresses):
    """An older worker dials the https tunnel alone and never sees a LAN route."""
    a, b = _pair(workers, addresses)
    set_owner_pref(a, "for_old_client", 3)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        with b.active():
            ctx = engine.context()
            # What an older build does: device token at its dial address, then /peer/exchange.
            link = transport.PeerLink(a.device_id, dial_endpoints([TUNNEL_A, LAN_A]))
            await link.authenticate(client, ctx)
            answer = await link._request(client, "POST", "/api/v1/network/peer/exchange",
                                         json_body={"watermarks": store.local_watermarks()})
    assert dial_endpoints(answer["endpoints"]) == [TUNNEL_A]
    assert answer["ops"]
    assert net.to(LAN_A) == []


async def test_kill_switch_on_either_side(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        # Off on the dialing worker: it never says hello.
        with b.active():
            lan.set_lan_direct_enabled(False)
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel" and net.to(LAN_A) == []
            lan.set_lan_direct_enabled(True)
        # Off on the answering worker: its LAN routes read as absent.
        with a.active():
            lan.set_lan_direct_enabled(False)
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel"
        assert _lan_paths(net, LAN_A) == [lan.HELLO_PATH]


async def test_owner_turning_lan_off_ends_open_sessions(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "lan"
        with a.active():
            answer = net.client.put("/api/v1/network/lan", json={"enabled": False}, headers=ORIGIN)
            assert answer.status_code == 200 and answer.json()["enabled"] is False
        set_owner_pref(b, "after_off", True)
        with b.active():
            await link.sync_once(client, engine.context())
            assert link.route == "tunnel"
    assert owner_pref(a, "after_off") is True


def test_owner_lan_settings_api(workers, addresses):
    a = workers("a")
    addresses["a"] = [TUNNEL_A, LAN_A]
    client = TestClient(_app())
    with a.active():
        engine.create_network("Home")
        assert client.get("/api/v1/network/lan").json() == {"enabled": True, "addresses": [],
                                                            "advertised": [TUNNEL_A, LAN_A]}
        saved = client.put("/api/v1/network/lan", json={"addresses": ["10.0.0.200:8001"]}, headers=ORIGIN)
        assert saved.status_code == 200 and saved.json()["addresses"] == ["http://10.0.0.200:8001"]
        refused = client.put("/api/v1/network/lan", json={"addresses": ["203.0.113.9:8000"]}, headers=ORIGIN)
        assert refused.status_code == 400 and "private" in refused.json()["detail"]
        assert lan.owner_addresses() == ["http://10.0.0.200:8001"]


# ---------------------------------------------------------------------------
# Bootstrap through the account hub
# ---------------------------------------------------------------------------

async def test_hub_admission_seals_lan_addresses_and_bootstrap_moves_no_tunnel_bytes(workers, addresses,
                                                                                      monkeypatch):
    a, b, c = workers("a"), workers("b"), workers("c")
    addresses["a"] = [TUNNEL_A, LAN_A]
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, c)
    set_owner_pref(a, "copied_over_lan", "yes")
    with a.active():
        admission = engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                                        encryption_public=b.identity.encryption_public, name="B")
        relayed = lan.seal_admission_addresses(admission, device_id=b.device_id,
                                               encryption_public=b.identity.encryption_public)
    # What the hub stores and forwards holds no LAN address in the clear.
    clear = json.dumps({k: v for k, v in relayed.items() if k != "lan_endpoints"})
    assert "10.0.0.11" not in clear and "10.0.0.11" not in json.dumps(relayed)
    assert relayed["issuer"]["endpoints"] == [TUNNEL_A]
    with c.active():
        assert lan.open_admission_addresses(relayed, engine.context()) == {}  # sealed to B alone
    with b.active():
        engine.enroll_from_response(relayed, device_name="B")
        assert lan.open_admission_addresses(relayed, engine.context()) == {a.device_id: [LAN_A]}

    net = Net().serve(a, TUNNEL_A, LAN_A)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(net.handler), **kw))
    with b.active():
        await hub._bootstrap_from_peers(relayed)
    assert owner_pref(b, "copied_over_lan") == "yes"
    assert net.to(TUNNEL_A) == []  # nothing through cloudflared
    assert lan.CALL_PATH in _lan_paths(net, LAN_A)


def test_member_without_a_tunnel_keeps_its_address_in_the_admission(workers):
    joiner = workers("j")
    response = {"network_id": "n", "issuer": {"device_id": "i", "endpoints": ["http://10.0.0.5:8000"]},
                "members": [{"device_id": "m", "endpoints": [TUNNEL_B, LAN_B]}]}
    out = lan.seal_admission_addresses(response, device_id="joiner",
                                       encryption_public=joiner.identity.encryption_public)
    assert out["issuer"]["endpoints"] == ["http://10.0.0.5:8000"]
    assert out["members"][0]["endpoints"] == [TUNNEL_B]
    assert "lan_endpoints" in out and "10.0.0.12" not in json.dumps(out)
