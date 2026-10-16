"""Owner-only explicit URL advertisements and the real signed presence path."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, network as api
from app.core import managed_connection
from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import advertisements, engine, lan, policy, tokens, transport
from tests.core.network.conftest import workers  # noqa: F401
from tests.core.network.harness import exchange, join
from tests.core.network.test_token_binding import post, request_body


@pytest.fixture
def setup(workers, monkeypatch):
    monkeypatch.setattr(managed_connection, "status", lambda: {"status": "connected", "url": "https://tunnel.example"})
    monkeypatch.setattr(settings, "cluster_advertise_url", "http://192.168.50.20:8000")
    monkeypatch.setattr(settings, "network_require_token_binding", False)
    monkeypatch.setattr(lan, "owner_addresses", lambda: [])
    server, peer = workers("server"), workers("peer")
    with server.active():
        engine.create_network("Test network")
    join(server, peer)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[auth.require_admin] = lambda: "owner"
    return server, peer, TestClient(app)


def test_public_alias_with_tunnel_repairs_binding_and_is_signed_into_presence(setup, monkeypatch):
    server, peer, client = setup
    capabilities = {"platform": "linux", "docker": True, "browser": True}
    monkeypatch.setattr(transport, "_capabilities", lambda: capabilities)
    alias = "https://proxy.example:8443/base"
    with server.active():
        assert post(client, request_body(client, server, peer, alias)).json()["detail"]["code"] == tokens.ENDPOINT_NOT_ADVERTISED
        response = client.put("/api/v1/network/advertised-addresses", json={"addresses": [alias + "/"]})
        assert response.status_code == 200
        assert response.json()["addresses"] == [alias]
        assert "https://tunnel.example" in response.json()["advertised"]
        assert alias in response.json()["advertised"]
        assert post(client, request_body(client, server, peer, alias)).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        # Previous-release callers still work before pinning. This peer is now
        # pinned, so the owner setting never weakens stripped-proof refusal.
        assert post(client, request_body(client, server, peer)).status_code == 403
        assert not policy.is_shared_setting_key(advertisements.SETTING)
    applied = exchange(server, peer)  # verifies/decrypts real signed ops
    assert not applied.rejected
    with peer.active():
        assert alias in engine.presence_of(server.device_id)["endpoints"]
        assert engine.presence_of(server.device_id)["capabilities"] == capabilities
        assert advertisements.owner_urls() == []  # another worker's local config is not imported
    with server.active():
        assert client.get("/api/v1/network/advertised-addresses").json()["addresses"] == [alias]
        assert client.put("/api/v1/network/advertised-addresses", json={"addresses": []}).status_code == 200
        assert post(client, request_body(client, server, peer, alias)).status_code == 403


@pytest.mark.parametrize("url", ["ftp://example.com", "https://u:p@example.com", "https://example.com?token=x",
    "https://example.com/#fragment", "https://faß.example", "https://K.example", "https://a..example",
    "https://example.com/%2e/", "https://[fe80::1%eth0]", "https://example.com:99999",
    "http://169.254.1.1:8000", "http://[fe80::1]:8000",
    "http://2852039166:8000", "http://0xa9.0xfe.0xa9.0xfe", "http://0251.0376.0251.0376",
    "http://169.254.43518",
    "https://example.com/" + "x" * 512, "https://example.com/a\\b", "https://example.com/ with space"])
def test_invalid_alias_is_rejected_atomically_without_echoing_input(setup, url):
    server, _, client = setup
    with server.active():
        advertisements.set_owner_urls(["https://saved.example"])
        response = client.put("/api/v1/network/advertised-addresses", json={"addresses": [url]})
        assert response.status_code == 400
        assert url not in response.text
        assert advertisements.owner_urls() == ["https://saved.example"]


def test_bound_count_and_canonicalization_keep_previous_wire_limit(setup, monkeypatch):
    server, _, client = setup
    with server.active():
        too_many = [f"https://alias{i}.example" for i in range(3)]
        assert client.put("/api/v1/network/advertised-addresses", json={"addresses": too_many}).status_code == 422
        response = client.put("/api/v1/network/advertised-addresses", json={
            "addresses": ["HTTPS://XN--FA-HIA.example:443/", "http://[2001:0db8::1]:80/"]})
        assert response.status_code == 200
        assert response.json()["addresses"] == ["https://xn--fa-hia.example", "http://[2001:db8::1]"]
        monkeypatch.setattr(lan, "owner_addresses", lambda: [f"http://192.168.50.{i}:8000" for i in range(30, 34)])
        assert len(engine._own_endpoints()) == 8
        assert len(api.TokenRequest(device_id=server.device_id, challenge="x" * 32,
                                    signature="x" * 86, endpoints=engine._own_endpoints()).endpoints) == 8
        database.set_system_setting(advertisements.SETTING, json.dumps(["https://faß.example"]))
        assert advertisements.owner_urls() == []


def test_preview_cookie_can_read_but_cannot_put_advertised_addresses(setup, monkeypatch):
    from app.core import preview_access
    server, _, client = setup
    client.app.dependency_overrides.clear()
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda *a: False)
    monkeypatch.setattr(auth.owner_auth, "owner_session", lambda *a: False)
    monkeypatch.setattr(preview_access, "_private_peer", lambda request: True)
    monkeypatch.setattr(preview_access, "verify", lambda token:
        preview_access.Grant(session_id="preview-session", expires_at=2**40) if token == "preview-token" else None)
    client.cookies.set(preview_access.COOKIE_NAME, "preview-token")
    with server.active():
        advertisements.set_owner_urls(["https://saved.example"])
        assert client.get("/api/v1/network/advertised-addresses").json()["addresses"] == ["https://saved.example"]
        response = client.put("/api/v1/network/advertised-addresses", json={"addresses": ["https://changed.example"]})
        assert response.status_code == 403
        assert response.json()["detail"].startswith("Agent previews cannot change")
        assert advertisements.owner_urls() == ["https://saved.example"]


@pytest.mark.parametrize("method", ["get", "put"])
def test_real_admin_guard_refuses_peer_device_tokens(setup, monkeypatch, method):
    server, peer, client = setup
    with server.active():
        token = post(client, request_body(client, server, peer)).json()["token"]
        client.app.dependency_overrides.clear()
        monkeypatch.setattr(auth, "get_paired_client_from_token", lambda *a, **kw: None)
        monkeypatch.setattr(auth.owner_auth, "owner_session", lambda *a: False)
        monkeypatch.setattr(auth.owner_auth, "local_access", lambda *a: False)
        response = getattr(client, method)("/api/v1/network/advertised-addresses",
            headers={"Authorization": "Bearer " + token}, **({"json": {"addresses": ["https://attacker.example"]}} if method == "put" else {}))
        assert response.status_code == 403
        assert advertisements.owner_urls() == []
