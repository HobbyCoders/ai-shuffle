"""The global challenge ceiling refuses new challenges; it never evicts a pending one (R9-12).

A flood from many addresses (an IPv6 /64, many hosts) fills the table; the
member's challenge issued before the flood must still be spendable, and the
member's own addresses keep headroom for their CHALLENGES_PER_DEVICE.
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.core.config import settings
from app.db import network as store
from app.network import engine, tokens
from tests.core.network.harness import join

CEILING = 6


def _app():
    app = FastAPI()
    app.include_router(network_api.router)
    return app


@pytest.fixture
def pair(workers, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "")
    monkeypatch.setattr(tokens, "CHALLENGES_MAX", CEILING)
    network_api._challenge_limiter.reset()
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    yield a, b
    network_api._challenge_limiter.reset()


def _flood(device_id):
    """Issue from fresh addresses until the ceiling refuses one; the refusals' status codes."""
    refused = []
    for i in range(CEILING * 3):
        try:
            tokens.new_challenge(device_id, f"2001:db8::{i + 1:x}")
        except HTTPException as exc:
            refused.append(exc.status_code)
    return refused


def _spend(a, b, challenge):
    sig = tokens.sign_challenge(b.identity.signing_private, challenge, a.device_id)
    return tokens.issue_token(device_id=b.device_id, challenge=challenge, signature=sig)


def test_many_address_flood_cannot_evict_a_members_pending_challenge(pair):
    a, b = pair
    with a.active():
        mine = tokens.new_challenge(b.device_id, "192.168.1.20")
        refused = _flood(b.device_id)
        assert refused and set(refused) == {429}
        assert len(tokens._challenges) == CEILING
        assert mine in tokens._challenges
        assert _spend(a, b, mine)["token"].startswith("dvt_")


def test_ceiling_answers_429_with_retry_after_through_the_route(pair):
    a, b = pair
    with a.active():
        _flood(b.device_id)
        stranger = TestClient(_app(), client=("192.168.1.77", 5555))
        r = stranger.post("/api/v1/network/peer/auth/challenge", json={"device_id": b.device_id})
    assert r.status_code == 429
    assert r.headers.get("retry-after") == str(tokens.CHALLENGE_RETRY_AFTER_SECONDS)


def test_an_address_that_spent_a_challenge_keeps_its_headroom_past_the_ceiling(pair):
    a, b = pair
    with a.active():
        _spend(a, b, tokens.new_challenge(b.device_id, "192.168.1.20"))
        _flood(b.device_id)
        assert tokens.at_capacity()
        held = [tokens.new_challenge(b.device_id, "192.168.1.20") for _ in range(tokens.CHALLENGES_PER_DEVICE)]
        assert all(c in tokens._challenges for c in held)
        # The per-requester cap still holds: the next one recycles its own oldest.
        extra = tokens.new_challenge(b.device_id, "192.168.1.20")
        assert held[0] not in tokens._challenges and extra in tokens._challenges
        # The same address naming another device is not recognised for it.
        with pytest.raises(HTTPException) as refused:
            tokens.new_challenge(a.device_id, "192.168.1.20")
        assert refused.value.status_code == 429


def test_a_members_advertised_endpoint_address_gets_its_challenges_past_the_ceiling(pair):
    a, b = pair
    with a.active():
        store.touch_member(b.device_id, endpoints=["http://192.168.1.50:8000"])
        _flood(b.device_id)
        member = TestClient(_app(), client=("192.168.1.50", 5555))
        stranger = TestClient(_app(), client=("192.168.1.51", 5555))
        codes = [member.post("/api/v1/network/peer/auth/challenge", json={"device_id": b.device_id}).status_code
                 for _ in range(tokens.CHALLENGES_PER_DEVICE)]
        assert codes == [200] * tokens.CHALLENGES_PER_DEVICE
        assert stranger.post("/api/v1/network/peer/auth/challenge",
                             json={"device_id": b.device_id}).status_code == 429


def test_is_member_endpoint_matches_literal_ip_hosts_only():
    assert tokens.is_member_endpoint("10.0.0.5", ["http://10.0.0.5:8000"])
    assert tokens.is_member_endpoint("2001:db8::5", ["https://[2001:db8::5]:8443/x"])
    assert not tokens.is_member_endpoint("10.0.0.5", ["http://10.0.0.6:8000", "http://host.lan:8000", 7])
    assert not tokens.is_member_endpoint("unknown", ["http://10.0.0.5:8000"])
