"""Unknown LAN addresses cannot crowd authenticated member addresses out of hello."""
import pytest
from cryptography.hazmat.primitives.asymmetric import x25519
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.core.config import settings
from app.db import network as store
from app.network import crypto, engine, lan, tokens
from tests.core.network.harness import join

MEMBER_IP = "192.0.2.20"


@pytest.fixture
def pair(workers, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "")
    network_api._challenge_limiter.reset()
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    yield a, b
    network_api._challenge_limiter.reset()


def _client(address):
    app = FastAPI()
    app.include_router(network_api.router)
    return TestClient(app, client=(address, 5555))


def _ephemeral():
    return crypto.b64u_encode(lan._x25519_public(x25519.X25519PrivateKey.generate()))


def _hello(client, device_id, ephemeral):
    return client.post(lan.HELLO_PATH, json={"device_id": device_id, "ephemeral": ephemeral})


def _complete(client, a, b, hello, ephemeral):
    body = lan.transcript(network_id=a.context.network_id, client=b.device_id, server=a.device_id,
                          challenge=hello["challenge"], client_ephemeral=ephemeral,
                          server_ephemeral=hello["server_ephemeral"])
    signature = crypto.b64u_encode(crypto.sign(b.identity.signing_private, lan._SESSION_DOMAIN + body))
    response = client.post(lan.SESSION_PATH, json={"device_id": b.device_id,
                           "challenge": hello["challenge"], "signature": signature})
    assert response.status_code == 200, response.text
    assert lan.session_known(response.json()["session"])


def _flood(device_id, ephemeral):
    ctx = engine.context()
    for i in range(lan.PENDING_MAX):
        address = f"2001:db8::{i // tokens.CHALLENGES_PER_DEVICE + 1:x}"
        challenge = tokens.new_challenge(device_id, address)
        lan.server_hello(ctx, device_id=device_id, client_ephemeral=ephemeral, challenge=challenge,
                         requester=address)
    assert lan.at_capacity()


@pytest.mark.parametrize("recognition", ["advertised", "token", "lan"])
def test_member_can_finish_handshake_after_many_address_flood(pair, recognition):
    a, b = pair
    ephemeral = _ephemeral()
    with a.active(), _client(MEMBER_IP) as member, _client("2001:db8::ffff") as stranger:
        if recognition == "advertised":
            store.touch_member(b.device_id, endpoints=[f"http://{MEMBER_IP}:8000"])
        elif recognition == "token":
            challenge = member.post("/api/v1/network/peer/auth/challenge",
                                    json={"device_id": b.device_id}).json()["challenge"]
            response = member.post("/api/v1/network/peer/auth/token", json={
                "device_id": b.device_id, "challenge": challenge,
                "signature": tokens.sign_challenge(b.identity.signing_private, challenge, a.device_id),
            })
            assert response.status_code == 200, response.text
        else:
            hello = _hello(member, b.device_id, ephemeral)
            assert hello.status_code == 200, hello.text
            _complete(member, a, b, hello.json(), ephemeral)

        _flood(b.device_id, ephemeral)
        assert len({p.requester for p in lan._pending.values()}) >= 256
        # LAN fills before the token store: endpoint recognition must check both ceilings.
        assert not tokens.at_capacity()
        original = set(lan._pending)
        refused = _hello(stranger, b.device_id, ephemeral)
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == str(tokens.CHALLENGE_RETRY_AFTER_SECONDS)

        held = []
        for _ in range(tokens.CHALLENGES_PER_DEVICE + 1):
            response = _hello(member, b.device_id, ephemeral)
            assert response.status_code == 200, response.text
            held.append(response.json())
        assert held[0]["challenge"] not in lan._pending
        assert all(hello["challenge"] in lan._pending for hello in held[1:])
        assert len(lan._pending) == lan.PENDING_MAX + tokens.CHALLENGES_PER_DEVICE
        assert original <= set(lan._pending)  # no other requester's handshake is evicted
        assert all(lan._pending[hello["challenge"]].reserved for hello in held[1:])
        # Recognition is per (device, address), not permission to name another device.
        assert _hello(member, a.device_id, ephemeral).status_code == 429
        _complete(member, a, b, held[-1], ephemeral)
        assert _hello(stranger, b.device_id, ephemeral).status_code == 429


def test_pending_ceiling_recycles_only_own_slots_and_expires(pair, monkeypatch):
    a, b = pair
    monkeypatch.setattr(lan, "PENDING_MAX", tokens.CHALLENGES_PER_DEVICE)
    ephemeral = _ephemeral()
    with a.active(), _client("2001:db8::1") as first, _client("2001:db8::2") as second:
        _flood(b.device_id, ephemeral)
        original = list(lan._pending)
        assert _hello(first, b.device_id, ephemeral).status_code == 200
        assert original[0] not in lan._pending
        assert set(original[1:]) <= set(lan._pending)
        assert len(lan._pending) == lan.PENDING_MAX
        assert _hello(second, b.device_id, ephemeral).status_code == 429
        # Use the same expiry clock for challenges and pending handshakes.
        now = lan.time.monotonic()
        monkeypatch.setattr(lan.time, "monotonic", lambda: now + tokens.CHALLENGE_TTL_SECONDS + 1)
        assert not lan.at_capacity()
        assert _hello(second, b.device_id, ephemeral).status_code == 200
        assert len(lan._pending) == 1
