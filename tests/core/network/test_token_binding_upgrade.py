"""Rolling upgrades keep paired identities and unadvertised routes usable by default."""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as api
from app.core import managed_connection
from app.core.config import Settings, settings
from app.db import network as store
from app.network import advertisements, engine, lan, tokens, transport
from tests.core.network.harness import join
from tests.core.network.test_token_binding import post, request_body


@pytest.fixture
def upgrade_pair(workers, monkeypatch):
    monkeypatch.setattr(settings, "network_token_binding_enabled", False)
    monkeypatch.setattr(settings, "network_require_token_binding", False)
    monkeypatch.setattr(settings, "cluster_advertise_url", "http://192.168.50.20:8000")
    monkeypatch.setattr(managed_connection, "status", lambda: {"status": "connected", "url": "https://tunnel.example"})
    monkeypatch.setattr(lan, "owner_addresses", lambda: [])
    server, peer = workers("docker"), workers("desktop")
    with server.active():
        engine.create_network("Existing network")
    join(server, peer)
    app = FastAPI()
    app.include_router(api.router)
    return server, peer, TestClient(app)


def test_rollout_settings_default_off():
    assert Settings.model_fields["network_token_binding_enabled"].default is False
    assert Settings.model_fields["network_require_token_binding"].default is False


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["http://192.168.50.20:18000", "https://proxy.example", "https://tunnel.example"])
@pytest.mark.parametrize("client_enabled", [False, True], ids=["legacy-wire", "bound-wire"])
async def test_upgrade_keeps_existing_identity_and_routes(upgrade_pair, monkeypatch, endpoint, client_enabled):
    server, peer, server_client = upgrade_pair
    before = (server.device_id, peer.device_id, server.context.network_id)
    token_requests = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/token"):
            token_requests.append(body)
        # Upgrading the receiver alone does not enforce binding, even if a
        # newer caller already sends a signed support statement and bound proof.
        with monkeypatch.context() as receiver, server.active():
            receiver.setattr(settings, "network_token_binding_enabled", False)
            response = server_client.post(request.url.path, json=body)
        return httpx.Response(response.status_code, json=response.json())

    monkeypatch.setattr(settings, "network_token_binding_enabled", client_enabled)
    for restart in (False, True):
        if restart:
            with server.active():
                engine.reset_for_tests()
                assert engine.context() is not None
        with peer.active():
            link = transport.PeerLink(server.device_id, [endpoint])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                token, _ = await link._issue_token(client, engine.context(), endpoint)
        with server.active():
            assert tokens.verify_token(token) == peer.device_id
            assert not store.get_member(peer.device_id)["token_binding_required"]
    assert (server.device_id, peer.device_id, server.context.network_id) == before
    assert len(token_requests) == 2
    for body in token_requests:
        assert ("bound_signature" in body) is client_enabled
        assert ("token_binding_proof" in body) is client_enabled


def test_existing_pin_is_ignored_when_disabled_and_retained_for_reenable(upgrade_pair, monkeypatch):
    server, peer, client = upgrade_pair
    alias = "https://proxy.example"
    with server.active():
        monkeypatch.setattr(settings, "network_token_binding_enabled", True)
        advertisements.set_owner_urls([alias])
        assert post(client, request_body(client, server, peer, alias)).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer)).status_code == 403
        monkeypatch.setattr(settings, "network_token_binding_enabled", False)
        monkeypatch.setattr(settings, "network_require_token_binding", True)
        advertisements.set_owner_urls([])
        engine.reset_for_tests()
        assert engine.context() is not None
        # Both a rolled-back client and a still-upgraded client keep working,
        # even if the strict sub-setting was left on or the alias disappeared.
        assert post(client, request_body(client, server, peer)).status_code == 200
        assert post(client, request_body(client, server, peer, alias)).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        monkeypatch.setattr(settings, "network_token_binding_enabled", True)
        assert post(client, request_body(client, server, peer)).status_code == 403
        assert post(client, request_body(client, server, peer, alias)).status_code == 403
        advertisements.set_owner_urls([alias])
        assert post(client, request_body(client, server, peer, alias)).status_code == 200


def test_disabled_exchange_does_not_publish_or_learn_binding_support(upgrade_pair):
    server, peer, client = upgrade_pair
    with peer.active():
        assert "token_binding_proof" not in transport._description()
        support = tokens.binding_support_proof(engine.context())
    with server.active():
        token = post(client, request_body(client, server, peer)).json()["token"]
        response = client.post("/api/v1/network/peer/exchange", json={"token_binding_proof": support},
                               headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert "token_binding_proof" not in response.json()
        assert not store.get_member(peer.device_id)["token_binding_required"]
        bad = request_body(client, server, peer)
        bad["signature"] = "x" * 86
        assert post(client, bad).status_code == 403  # the gate never bypasses device authentication


def test_additive_upgrade_preserves_pre_binding_membership(upgrade_pair):
    server, peer, client = upgrade_pair
    with server.active():
        before = store.get_member(peer.device_id)
        # Reproduce the pre-upgrade table, then run the real additive migration.
        server.raw.execute("ALTER TABLE network_members DROP COLUMN token_binding_required")
        with store.get_db() as conn:
            store._ensure_columns(conn.cursor())
        after = store.get_member(peer.device_id)
        assert after["signing_public"] == before["signing_public"]
        assert after["status"] == before["status"] == "active"
        assert not after["token_binding_required"]
        assert post(client, request_body(client, server, peer)).status_code == 200

@pytest.mark.parametrize("client_enabled", [False, True])
def test_enrollment_snapshot_over_unadvertised_alias_during_upgrade(upgrade_pair, monkeypatch, client_enabled):
    from app.network import enrollment
    server, peer, server_client = upgrade_pair
    real_client = httpx.Client
    seen = []

    def handler(request):
        with monkeypatch.context() as receiver, server.active():
            receiver.setattr(settings, "network_token_binding_enabled", False)
            if request.url.path.endswith("/auth/token"):
                seen.append(json.loads(request.content))
            response = server_client.request(request.method, request.url.path,
                                             content=request.content, headers=dict(request.headers),
                                             params=request.url.params)
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(enrollment.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(settings, "network_token_binding_enabled", client_enabled)
    with peer.active():
        network_id = engine.context().network_id
        enrollment._copy_snapshot_sync(engine.context(), server.device_id, ["https://proxy.example"])
        assert engine.context().network_id == network_id
    assert len(seen) == 1
    assert ("bound_signature" in seen[0]) is client_enabled

