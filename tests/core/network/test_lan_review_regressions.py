"""Regressions from the security review of the LAN-direct path (#2169, PR #2185).

Each test started life as a reproduction of an attack on the first version of
the sealed LAN session and now asserts the attack fails.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.db import network as store
from app.network import engine, lan, tokens, transport
from app.network.endpoints import known_endpoints
from tests.core.network.test_lan_direct import (  # noqa: F401  (fixtures)
    LAN_A, LAN_B, TUNNEL_A, TUNNEL_B, Net, _pair, addresses, clock,
)

EVIL = ["https://evil.example.net", "http://10.66.66.66:8000"]
DESCRIPTION = ("endpoints", "capabilities", "incarnation", "version", "platform", "name")


def _rewriting_session(net: Net, seen: dict):
    """A LAN man in the middle: records what ``/peer/lan/session`` shows and plants a description in it."""
    real = net.handler

    def mitm(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(lan.SESSION_PATH):
            body = json.loads(request.content)
            seen.update(body)
            body.update(endpoints=EVIL, capabilities={"remote_task": 99}, incarnation=987654, version="evil",
                        platform="evil")
            request = httpx.Request(request.method, request.url, headers={"content-type": "application/json"},
                                    content=json.dumps(body).encode())
        return real(request)

    net.handler = mitm


async def test_lan_session_carries_no_description_and_a_rewrite_is_not_stored(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    seen: dict = {}
    _rewriting_session(net, seen)
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.open(client, engine.context())
            assert link.route == "lan"  # the session itself is still accepted
    # A passive observer reads nothing about B from the handshake.
    assert seen and not set(seen) & set(DESCRIPTION)
    with a.active():
        row = store.get_member(b.device_id)
        assert not set(row.get("endpoints") or []) & set(EVIL)
        assert row.get("incarnation") != 987654
        assert (row.get("capabilities") or {}).get("remote_task") != 99
        assert row.get("version") != "evil" and row.get("platform") != "evil"
        link_ab = transport.PeerLink(b.device_id, known_endpoints(row, engine.all_presence().get(b.device_id)))
        assert EVIL[0] not in link_ab.endpoints and EVIL[1] not in link_ab.lan_endpoints

    # B's real description reaches A through the sealed exchange instead.
    async with net.client_for() as client:
        with b.active():
            await link.sync_once(client, engine.context())
            assert link.route == "lan"
    with a.active():
        row = store.get_member(b.device_id)
        assert row["endpoints"] == [TUNNEL_B, LAN_B]
        assert row["platform"] and row["platform"] != "evil"
    assert not any(r["path"] == "/api/v1/network/peer/exchange" for r in net.to(LAN_A))


async def test_planted_endpoint_cannot_relay_a_device_token(workers, addresses):
    """The chain: rewrite B's handshake at A, then have A authenticate to B through the attacker."""
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    _rewriting_session(net, {})
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.open(client, engine.context())
            assert link.route == "lan"

    # The attacker cuts B's real addresses off from A and proxies its own host to B.
    net2 = Net().serve(b, TUNNEL_B, LAN_B, *EVIL)
    net2.failure[TUNNEL_B] = "refused"
    net2.failure[LAN_B] = "refused"
    async with net2.client_for() as client:
        with a.active():
            row = store.get_member(b.device_id)
            candidates = known_endpoints(row, engine.all_presence().get(b.device_id))
            assert not set(candidates) & set(EVIL)
            link_ab = transport.PeerLink(b.device_id, candidates)
            with pytest.raises((transport.PeerError, httpx.HTTPError)):
                await link_ab.open(client, engine.context())
            assert link_ab.token is None
    assert not any(r["base"] in EVIL for r in net2.log)


async def test_a_challenge_seen_on_the_lan_cannot_be_burned(workers, addresses):
    a, b = _pair(workers, addresses)
    net = Net().serve(a, TUNNEL_A, LAN_A)
    real = net.handler
    refused = []

    def racer(request: httpx.Request) -> httpx.Response:
        response = real(request)
        if str(request.url).endswith(lan.HELLO_PATH) and response.status_code == 200:
            challenge = json.loads(response.content)["challenge"]
            with a.active():  # whoever saw it spends it first, with a junk signature, on both routes
                for path in (lan.SESSION_PATH, "/api/v1/network/peer/auth/token"):
                    r = net.client.post(path, json={"device_id": b.device_id, "challenge": challenge,
                                                    "signature": "A" * 86})
                    refused.append(r.status_code)
        return response

    net.handler = racer
    async with net.client_for() as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [TUNNEL_A, LAN_A])
            await link.sync_once(client, engine.context())
            assert link.route == "lan" and link.lan_last_error is None
    assert refused == [403, 400]  # bad signature on /session; a LAN challenge buys no device token


def test_pending_handshakes_are_bounded_per_requester(workers, addresses, monkeypatch):
    a, b = _pair(workers, addresses)
    ephemeral = lan.crypto.b64u_encode(bytes(32))
    with a.active():
        ctx = engine.context()
        lan.server_hello(ctx, device_id=b.device_id, client_ephemeral=ephemeral, challenge="member-" + "0" * 16,
                         requester="10.0.0.12")
        # A flood naming B's public device id from one address recycles only its own handshakes.
        for n in range(tokens.CHALLENGES_PER_DEVICE * 3):
            lan.server_hello(ctx, device_id=b.device_id, client_ephemeral=ephemeral,
                             challenge=f"flood-{n:016d}", requester="10.66.66.66")
        flood = [p for p in lan._pending.values() if p.requester == "10.66.66.66"]
        assert len(flood) == tokens.CHALLENGES_PER_DEVICE
        assert "member-" + "0" * 16 in lan._pending
        # A full table refuses new hellos instead of evicting the member's handshake.
        monkeypatch.setattr(lan, "PENDING_MAX", len(lan._pending))
        with pytest.raises(lan.LanBusy):
            lan.server_hello(ctx, device_id=b.device_id, client_ephemeral=ephemeral, challenge="more-" + "0" * 16,
                             requester="10.77.77.77")
        assert "member-" + "0" * 16 in lan._pending


def test_unknown_session_is_refused_before_the_body_is_read(workers, addresses, monkeypatch):
    a, _b = _pair(workers, addresses)
    net = Net()
    monkeypatch.setattr(lan, "CALL_MAX_BYTES", 16)
    with a.active():
        r = net.client.post(lan.CALL_PATH, content=b"x" * 1024,
                            headers={lan.SESSION_HEADER: "no-such-session", lan.SEQ_HEADER: "1"})
    assert r.status_code == 401  # not 413: the oversized body was never read
