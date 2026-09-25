"""LAN hosts cannot pick their own rate-limit key, nor evict a member's challenge."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.api import pairing as pairing_api
from app.core.config import settings
from app.network import engine, tokens
from tests.core.network.harness import join

DEVICE = {"signing_public": "A" * 43, "encryption_public": "A" * 43, "name": "x"}


def _app():
    app = FastAPI()
    app.include_router(network_api.router)
    app.include_router(pairing_api.router)
    return app


def test_forwarded_for_from_a_lan_host_does_not_reset_join_and_exchange_limits(workers, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "")
    a = workers("a")
    network_api._join_limiter.reset()
    pairing_api._exchange_limiter.reset()
    try:
        with a.active():
            engine.create_network("Home")
            lan = TestClient(_app(), client=("192.168.1.66", 5555))
            spoofed = [lan.post("/api/v1/network/join", json={"code": "AAAA-BBBB-CCCC", "device": DEVICE},
                                headers={"X-Forwarded-For": f"10.9.0.{i}"}).status_code for i in range(30)]
            assert 429 in spoofed
            exchange = [lan.post("/api/v1/pairing/exchange",
                                 json={"code": "AAAA-BBBB-CCCC", "device_id": "dddddddd", "device_name": "n",
                                       "platform": "p"},
                                 headers={"X-Forwarded-For": f"10.8.0.{i}"}).status_code for i in range(30)]
            assert 429 in exchange
    finally:
        network_api._join_limiter.reset()
        pairing_api._exchange_limiter.reset()


def test_a_configured_proxy_still_attributes_each_client(workers, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "172.18.0.2")
    a = workers("a")
    network_api._join_limiter.reset()
    try:
        with a.active():
            engine.create_network("Home")
            proxy = TestClient(_app(), client=("172.18.0.2", 5555))
            codes = [proxy.post("/api/v1/network/join", json={"code": "AAAA-BBBB-CCCC", "device": DEVICE},
                                headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code for i in range(15)]
            assert 429 not in codes
    finally:
        network_api._join_limiter.reset()


def test_challenge_flood_from_another_host_does_not_evict_a_members_challenge(workers, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "")
    network_api._challenge_limiter.reset()
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        member = TestClient(_app(), client=("192.168.1.20", 5555))
        attacker = TestClient(_app(), client=("192.168.1.66", 5555))
        mine = member.post("/api/v1/network/peer/auth/challenge", json={"device_id": b.device_id}).json()["challenge"]
        # The attacker knows only B's public device id.
        for _ in range(tokens.CHALLENGES_PER_DEVICE * 2):
            assert attacker.post("/api/v1/network/peer/auth/challenge",
                                 json={"device_id": b.device_id}).status_code == 200
        sig = tokens.sign_challenge(b.identity.signing_private, mine, a.device_id)
        r = member.post("/api/v1/network/peer/auth/token",
                        json={"device_id": b.device_id, "challenge": mine, "signature": sig})
        assert r.status_code == 200, r.text
        # The attacker's own outstanding challenges stay bounded per device.
        held = [c for c, (_e, d, req) in tokens._challenges.items() if d == b.device_id and req == "192.168.1.66"]
        assert len(held) == tokens.CHALLENGES_PER_DEVICE
    network_api._challenge_limiter.reset()


def test_outstanding_challenges_have_a_global_ceiling(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    monkeypatch.setattr(tokens, "CHALLENGES_MAX", 5)
    with a.active():
        issued = [tokens.new_challenge(b.device_id, f"10.0.0.{i}") for i in range(8)]
    assert len(tokens._challenges) == 5
    assert list(tokens._challenges) == issued[-5:]
