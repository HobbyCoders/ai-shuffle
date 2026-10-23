"""Default-on binding, mixed-worker upgrades, advertised-address repair and rollback."""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as api
from app.core import managed_connection
from app.core.config import Settings, settings
from app.core import internal_defaults, owner_settings
from app.db import network as store
from app.network import advertisements, engine, lan, tokens, transport
from tests.core.network.harness import join
from tests.core.network.test_token_binding import post, request_body


@pytest.fixture
def upgrade_pair(workers, monkeypatch):
    monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=False: value))
    monkeypatch.setattr(type(settings), 'network_require_token_binding', property(lambda self, value=False: value))
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


def test_rollout_settings_default_on_with_legacy_compatibility():
    assert internal_defaults.NETWORK_TOKEN_BINDING_ENABLED is True
    assert owner_settings.OwnerPolicy().network_require_token_binding is False


@pytest.mark.asyncio
@pytest.mark.parametrize("client_enabled,server_enabled", [(False, True), (True, False), (True, True)])
async def test_default_on_upgrade_authenticates_both_wire_versions(
        upgrade_pair, monkeypatch, client_enabled, server_enabled):
    server, peer, server_client = upgrade_pair
    endpoint = "https://tunnel.example"
    default = internal_defaults.NETWORK_TOKEN_BINDING_ENABLED

    def handler(request):
        with monkeypatch.context() as receiver, server.active():
            receiver.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=default if server_enabled else False: value))
            response = server_client.post(request.url.path, json=json.loads(request.content))
        return httpx.Response(response.status_code, json=response.json())

    monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=default if client_enabled else False: value))
    with peer.active():
        link = transport.PeerLink(server.device_id, [endpoint])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token, _ = await link._issue_token(client, engine.context(), endpoint)
    with server.active():
        assert tokens.verify_token(token) == peer.device_id
        assert bool(store.get_member(peer.device_id)["token_binding_required"]) is (client_enabled and server_enabled)


@pytest.mark.asyncio
async def test_default_on_dialer_refuses_changed_address_until_owner_repairs(upgrade_pair, monkeypatch):
    from app.api.auth import require_admin
    server, peer, server_client = upgrade_pair
    alias = "https://proxy.example:8443"
    monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=internal_defaults.NETWORK_TOKEN_BINDING_ENABLED: value))
    server_client.app.dependency_overrides[require_admin] = lambda: "owner"

    def handler(request):
        with server.active():
            response = server_client.post(request.url.path, json=json.loads(request.content))
        return httpx.Response(response.status_code, json=response.json())

    async def issue():
        with peer.active():
            link = transport.PeerLink(server.device_id, [alias])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await link._issue_token(client, engine.context(), alias)

    with pytest.raises(transport.PeerEndpointNotAdvertised):
        await issue()
    with server.active():
        response = server_client.put("/api/v1/network/advertised-addresses", json={"addresses": [alias]})
        assert response.status_code == 200
        assert alias in response.json()["advertised"]
    token, _ = await issue()
    with server.active():
        assert tokens.verify_token(token) == peer.device_id
        assert store.get_member(peer.device_id)["token_binding_required"]


@pytest.mark.asyncio
async def test_rolling_back_one_peer_requires_opt_out_on_its_pinned_receiver(upgrade_pair, monkeypatch):
    server, peer, server_client = upgrade_pair
    endpoint = "https://tunnel.example"
    receiver_enabled = True

    def handler(request):
        with monkeypatch.context() as receiver, server.active():
            receiver.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=receiver_enabled: value))
            response = server_client.post(request.url.path, json=json.loads(request.content))
        return httpx.Response(response.status_code, json=response.json())

    async def issue():
        with peer.active():
            link = transport.PeerLink(server.device_id, [endpoint])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await link._issue_token(client, engine.context(), endpoint)

    monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=True: value))
    await issue()  # the real producer sends binding support and the receiver pins it
    with server.active():
        assert store.get_member(peer.device_id)["token_binding_required"]
    monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=False: value))
    with pytest.raises(transport.PeerError, match="Endpoint binding is required") as refused:
        await issue()
    detail = str(refused.value)
    assert "Update the calling worker" in detail
    assert "Update the calling worker and repair its advertised addresses" in detail
    assert "No re-pairing needed" in detail
    assert len(detail) <= 300  # v1.0.2 callers truncate the response detail at 300 characters
    receiver_enabled = False
    token, _ = await issue()
    with server.active():
        assert tokens.verify_token(token) == peer.device_id
        assert store.get_member(peer.device_id)["token_binding_required"]  # never erase downgrade evidence


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
            receiver.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=False: value))
            # A reverse proxy strips its configured base path before forwarding.
            route = request.url.path.removeprefix(httpx.URL(endpoint).path.rstrip("/"))
            response = server_client.post(route, json=body)
        return httpx.Response(response.status_code, json=response.json())

    monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=client_enabled: value))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["https://faß.example", "https://bücher.example", "https://example.com/café"])
async def test_disabled_binding_preserves_international_routes(upgrade_pair, monkeypatch, endpoint):
    from app.network.endpoints import dial_endpoints
    assert dial_endpoints([endpoint]) == [endpoint]
    await test_upgrade_keeps_existing_identity_and_routes(upgrade_pair, monkeypatch, endpoint, False)


def test_existing_pin_is_ignored_when_disabled_and_retained_for_reenable(upgrade_pair, monkeypatch):
    server, peer, client = upgrade_pair
    alias = "https://proxy.example"
    with server.active():
        monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=True: value))
        advertisements.set_owner_urls([alias])
        assert post(client, request_body(client, server, peer, alias)).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer)).status_code == 403
        monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=False: value))
        monkeypatch.setattr(type(settings), 'network_require_token_binding', property(lambda self, value=True: value))
        advertisements.set_owner_urls([])
        engine.reset_for_tests()
        assert engine.context() is not None
        # Both a rolled-back client and a still-upgraded client keep working,
        # even if the strict sub-setting was left on or the alias disappeared.
        assert post(client, request_body(client, server, peer)).status_code == 200
        assert post(client, request_body(client, server, peer, alias)).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=True: value))
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
            receiver.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=False: value))
            if request.url.path.endswith("/auth/token"):
                seen.append(json.loads(request.content))
            response = server_client.request(request.method, request.url.path,
                                             content=request.content, headers=dict(request.headers),
                                             params=request.url.params)
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": "application/json"})

    monkeypatch.setattr(enrollment.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(type(settings), 'network_token_binding_enabled', property(lambda self, value=client_enabled: value))
    with peer.active():
        network_id = engine.context().network_id
        enrollment._copy_snapshot_sync(engine.context(), server.device_id, ["https://proxy.example"])
        assert engine.context().network_id == network_id
    assert len(seen) == 1
    assert ("bound_signature" in seen[0]) is client_enabled

