"""Compatibility rollout and relay resistance of endpoint-bound device tokens."""
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import network as api
from app.core.config import settings
from app.db import network as store
from app.network import crypto, engine, tokens, transport
from tests.core.network.harness import join


@pytest.fixture
def pair(workers, monkeypatch):
    server, peer = workers("server"), workers("peer")
    with server.active():
        engine.create_network("Test network")
    join(server, peer)
    endpoints = ["https://server.example", "http://192.168.50.20:8000"]
    monkeypatch.setattr(engine, "_own_endpoints", lambda: list(endpoints))
    monkeypatch.setattr(settings, "network_require_token_binding", False)
    app = FastAPI()
    app.include_router(api.router)
    return server, peer, endpoints, TestClient(app)


def request_body(client, server, peer, endpoint=None):
    challenge = client.post("/api/v1/network/peer/auth/challenge", json={"device_id": peer.device_id})
    assert challenge.status_code == 200
    assert challenge.json()["token_binding"] == 1
    nonce = challenge.json()["challenge"]
    body = {"device_id": peer.device_id, "challenge": nonce,
            "signature": tokens.sign_challenge(peer.identity.signing_private, nonce, server.device_id)}
    if endpoint is not None:
        body.update(endpoint=endpoint, bound_signature=tokens.sign_bound_challenge(
            peer.identity.signing_private, nonce, server.device_id, endpoint))
    return body


def post(client, body, **kw):
    return client.post("/api/v1/network/peer/auth/token", json=body, **kw)


def test_enrollment_preserves_the_actionable_refusal(pair, monkeypatch):
    from app.network import enrollment
    server, peer, _, server_client = pair
    real_client = httpx.Client
    def handler(request):
        if request.url.host == "offline.example":
            raise httpx.ConnectError("offline", request=request)
        with server.active():
            response = server_client.post(request.url.path, json=json.loads(request.content))
        return httpx.Response(response.status_code, json=response.json())
    monkeypatch.setattr(enrollment.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    with peer.active():
        with pytest.raises(enrollment.EndpointEnrollmentError) as exc:
            enrollment._copy_snapshot_sync(engine.context(), server.device_id,
                                           ["https://alias.example", "https://offline.example"])
        assert "https://alias.example" in enrollment._join_error(exc.value)
        assert "CLUSTER_ADVERTISE_URL" in enrollment._join_error(exc.value)


@pytest.mark.parametrize("body", [b'[]', b'"forbidden"', b'<html>Forbidden</html>'], ids=["list", "string", "html"])
@pytest.mark.parametrize("retry", [False, True], ids=["only-refusal", "next-endpoint"])
def test_enrollment_handles_unexpected_forbidden_bodies(pair, monkeypatch, body, retry):
    from app.network import enrollment
    server, peer, _, server_client = pair
    real_client = httpx.Client
    attempted = []
    def handler(request):
        if request.url.path.endswith("/peer/state"):
            return httpx.Response(404)
        if request.url.path.endswith("/peer/snapshot"):
            return httpx.Response(200, json={"collections": [], "watermarks": {}})
        if request.url.path.endswith("/auth/token"):
            attempted.append(request.url.host)
            if request.url.host == "malformed.example":
                return httpx.Response(403, content=body)
        with server.active():
            response = server_client.post(request.url.path, json=json.loads(request.content))
        return httpx.Response(response.status_code, json=response.json())
    monkeypatch.setattr(enrollment.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    urls = ["https://malformed.example"] + (["https://server.example"] if retry else [])
    with peer.active():
        if retry:
            enrollment._copy_snapshot_sync(engine.context(), server.device_id, urls)
        else:
            with pytest.raises(enrollment.EnrollmentError, match="^Snapshot copy failed: EnrollmentError: token HTTP 403$"):
                enrollment._copy_snapshot_sync(engine.context(), server.device_id, urls)
    assert attempted == (["malformed.example", "server.example"] if retry else ["malformed.example"])


@pytest.mark.asyncio
async def test_warning_limit_spans_fresh_peer_links_and_expires(monkeypatch, caplog):
    monkeypatch.setattr(transport, "_binding_warning_at", {})
    def handler(request):
        return httpx.Response(403, json={"detail": {"code": tokens.ENDPOINT_NOT_ADVERTISED}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def refused(device_id):
            # Control/browser calls each construct a fresh link instance.
            link = transport.PeerLink(device_id, ["https://alias.example"])
            with pytest.raises(transport.PeerEndpointNotAdvertised):
                await link._request(client, "POST", "/api/v1/network/peer/auth/token",
                                    auth=False, base="https://alias.example")
        await refused("peer-a")
        await refused("peer-a")
        await refused("peer-b")
        assert len([r for r in caplog.records if "does not advertise" in r.message]) == 2
        transport._binding_warning_at["peer-a"] -= 301
        transport._binding_warning_at["expired-peer"] = transport._binding_warning_at["peer-a"]
        await refused("peer-a")
        assert len([r for r in caplog.records if "does not advertise" in r.message]) == 3
        assert "expired-peer" not in transport._binding_warning_at


def test_mismatch_code_discloses_no_addresses_and_requires_valid_proof(pair):
    server, peer, endpoints, client = pair
    with server.active():
        body = request_body(client, server, peer, "https://alias.example")
        response = post(client, body)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == tokens.ENDPOINT_NOT_ADVERTISED
        assert all(url not in response.text for url in endpoints)
        assert body["challenge"] not in response.text
        assert body["signature"] not in response.text
        body = request_body(client, server, peer, "https://alias.example")
        body["bound_signature"] = body["signature"]
        assert post(client, body).json()["detail"] == "Endpoint binding is invalid"


@pytest.mark.asyncio
async def test_mismatch_reason_reaches_peer_status_and_rate_limited_logs(pair, caplog):
    server, peer, _, server_client = pair
    alias = "https://alias.example"
    replies = []
    def handler(request):
        if request.url.host == "offline.example":
            raise httpx.ConnectError("offline", request=request)
        with server.active():
            reply = server_client.post(request.url.path, json=json.loads(request.content))
        replies.append(reply.json())
        return httpx.Response(reply.status_code, json=reply.json())
    with peer.active():
        link = transport.PeerLink(server.device_id, [alias, "https://offline.example"])
        transport._links[server.device_id] = link
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for _ in range(2):
                with pytest.raises(transport.PeerEndpointNotAdvertised) as exc:
                    await link.authenticate(client, engine.context())
                assert exc.value.code == tokens.ENDPOINT_NOT_ADVERTISED
        status = transport.link_status()[0]
        assert alias in status["last_error"]
        assert "Advertised addresses" in status["last_error"]
        assert "CLUSTER_ADVERTISE_URL" in status["last_error"]
    warnings = [r.message for r in caplog.records if "does not advertise" in r.message]
    assert len(warnings) == 1
    assert alias in warnings[0] and server.device_id in warnings[0]
    for reply in replies:
        if "challenge" in reply:
            assert reply["challenge"] not in warnings[0]


def test_owner_configured_alias_repairs_unadvertised_direct_endpoint(workers, monkeypatch):
    from app.core import managed_connection
    from app.network import lan

    monkeypatch.setattr(managed_connection, "status", lambda: {})
    monkeypatch.setattr(lan, "owner_addresses", lambda: [])
    monkeypatch.setattr(settings, "cluster_advertise_url", "http://192.168.50.20:8000")
    monkeypatch.setattr(settings, "network_require_token_binding", False)
    server, peer = workers("server"), workers("peer")
    with server.active():
        engine.create_network("Test network")
    join(server, peer)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app)
    alias = "https://proxy.example/worker"
    with server.active():
        # Real advertisement producer, not an injected allowlist: an external
        # reverse-proxy URL was reachable before but is not yet advertised.
        assert engine._own_endpoints() == ["http://192.168.50.20:8000"]
        assert post(client, request_body(client, server, peer)).status_code == 200
        assert post(client, request_body(client, server, peer, alias)).status_code == 403
        assert not store.get_member(peer.device_id)["token_binding_required"]
        # Existing trusted owner configuration repairs the topology without
        # deriving an alias from Host/forwarded headers or weakening binding.
        monkeypatch.setattr(settings, "cluster_advertise_url", alias)
        assert engine._own_endpoints() == [alias]
        assert post(client, request_body(client, server, peer, alias)).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]


def test_legacy_then_bound_then_stripped_and_relay_refused(pair):
    server, peer, endpoints, client = pair
    with server.active():
        # Transitional first contact remains vulnerable to stripping by design.
        body = request_body(client, server, peer, endpoints[0])
        body.pop("endpoint")
        body.pop("bound_signature")
        assert post(client, body).status_code == 200
        assert not store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer, endpoints[0])).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer)).status_code == 403
        relay = request_body(client, server, peer, "https://relay.example")
        assert post(client, relay, headers={"host": "relay.example", "x-forwarded-host": "relay.example"}).status_code == 403
        assert post(client, request_body(client, server, peer, endpoints[1])).status_code == 200


@pytest.mark.parametrize("damage", ["foreign", "endpoint", "signature", "audience", "nonce", "null", "empty", "missing_endpoint"])
def test_present_binding_fails_closed_even_before_pin(pair, damage):
    server, peer, endpoints, client = pair
    with server.active():
        body = request_body(client, server, peer, endpoints[0])
        if damage == "foreign":
            body = request_body(client, server, peer, "https://relay.example")
        elif damage == "endpoint":
            body["endpoint"] = endpoints[1]  # even another legitimate endpoint needs its own proof
        elif damage == "signature":
            body["bound_signature"] = body["signature"]
        elif damage in ("audience", "nonce"):
            body["bound_signature"] = tokens.sign_bound_challenge(
                peer.identity.signing_private, "other" if damage == "nonce" else body["challenge"],
                "other" if damage == "audience" else server.device_id, endpoints[0])
        elif damage == "null":
            body["endpoint"] = body["bound_signature"] = None
        elif damage == "empty":
            body["bound_signature"] = ""
        else:
            body.pop("endpoint")
        assert post(client, body).status_code == 403
        assert not store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer)).status_code == 200


def test_replay_rotation_restart_and_membership_updates_preserve_pin(pair):
    server, peer, endpoints, client = pair
    with server.active():
        body = request_body(client, server, peer, endpoints[0])
        assert post(client, body).status_code == 200
        assert post(client, body).status_code == 400
        old = request_body(client, server, peer, endpoints[0])
        endpoints[0] = "https://rotated.example/base"
        assert post(client, old).status_code == 403
        assert post(client, request_body(client, server, peer, endpoints[0])).status_code == 200
        tokens.reset_for_tests()
        engine.reset_for_tests()
        assert engine.context() is not None  # reload context from the same durable store
        row = store.get_member(peer.device_id)
        for status in ("revoked", "active"):
            store.upsert_member(device_id=peer.device_id, name=row["name"], kind=row["kind"],
                                signing_public=row["signing_public"], encryption_public=row["encryption_public"],
                                generation=row["generation"] + 1, status=status)
        store.touch_member(peer.device_id, version="old", capabilities={"token_binding_required": False})
        assert store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer)).status_code == 403


def test_global_requirement_and_unsigned_description_cannot_pin(pair, monkeypatch):
    server, peer, endpoints, client = pair
    with server.active():
        body = request_body(client, server, peer)
        body.update(version="999", capabilities={"token_binding_required": True}, token_binding_required=True)
        assert post(client, body).status_code == 200
        assert not store.get_member(peer.device_id)["token_binding_required"]
        monkeypatch.setattr(settings, "network_require_token_binding", True)
        assert post(client, request_body(client, server, peer)).status_code == 403
        assert post(client, request_body(client, server, peer, endpoints[0])).status_code == 200


def test_signed_description_producer_pins_before_first_bound_request(pair):
    server, peer, endpoints, client = pair
    with peer.active():
        description = transport._description()  # actual producer, not a seeded capability field
    with server.active():
        token = post(client, request_body(client, server, peer)).json()["token"]
        response = client.post("/api/v1/network/peer/exchange", json=description,
                               headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer)).status_code == 403
        reply_proof = response.json()["token_binding_proof"]
    with peer.active():
        tokens.remember_binding_support(server.device_id, reply_proof)
        assert store.get_member(server.device_id)["token_binding_required"]


def test_token_description_is_authenticated_before_capability_pin(pair):
    server, peer, endpoints, client = pair
    with peer.active():
        description = transport._description()
    with server.active():
        body = {**request_body(client, server, peer), **description}
        body["signature"] = "x" * 86
        assert post(client, body).status_code == 403
        assert not store.get_member(peer.device_id)["token_binding_required"]
        # Stripping just the bound proof cannot bypass the signed capability.
        body = {**request_body(client, server, peer), **description}
        assert post(client, body).status_code == 403
        assert store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer, endpoints[0])).status_code == 200


def test_unsigned_or_wrong_key_capability_cannot_pin(pair):
    server, peer, _, _ = pair
    with server.active():
        for proof in ("", tokens.binding_support_proof(engine.context())):
            with pytest.raises(HTTPException):
                tokens.remember_binding_support(peer.device_id, proof)
        assert not store.get_member(peer.device_id)["token_binding_required"]


@pytest.mark.parametrize("raw,canonical", [
    ("HTTPS://EXAMPLE.COM:443/", "https://example.com"),
    ("http://EXAMPLE.COM:80///", "http://example.com"),
    ("https://example.com:8443/base/", "https://example.com:8443/base"),
    ("http://[2001:0DB8:0:0::1]:80/", "http://[2001:db8::1]"),
    ("https://[2001:db8::1]:8443/A//B/", "https://[2001:db8::1]:8443/A//B"),
    ("https://example.com/%7euser/%2f/", "https://example.com/~user/%2F"),
    ("https://example.com./Case/", "https://example.com./Case"),
    ("https://XN--BCHER-KVA.example/", "https://xn--bcher-kva.example"),
])
def test_normalization(raw, canonical):
    assert tokens.canonical_endpoint(raw) == canonical
    assert tokens.canonical_endpoint(canonical) == canonical


@pytest.mark.parametrize("raw", ["ftp://example.com", "https://u:p@example.com", "https://example.com?x=1",
    "https://example.com/#fragment", "https://example.com:99999", "https://[fe80::1%eth0]", "https://example.com/a/../b",
    "https://example.com/%2e/", "https://example.com/a\\b", " https://example.com", "https://example.com/%zz", "https://example.com/é"])
def test_ambiguous_endpoints_rejected(raw):
    with pytest.raises(ValueError):
        tokens.canonical_endpoint(raw)


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("relay_endpoint", ["https://faß.example", "https://xn--fa-hia.example"])
def test_idna_destinations_cannot_share_a_binding(pair, pinned, relay_endpoint):
    server, peer, endpoints, client = pair
    legitimate = "https://fass.example"
    endpoints[:] = [legitimate]
    assert httpx.URL("https://faß.example").raw_host == b"xn--fa-hia.example"
    assert httpx.URL(legitimate).raw_host == b"fass.example"
    with server.active():
        if pinned:
            assert post(client, request_body(client, server, peer, legitimate)).status_code == 200
        # Reproduce the old IDNA-2003 proof: the client signed fass.example
        # while HTTPX connected to xn--fa-hia.example. It must not be accepted.
        body = request_body(client, server, peer, legitimate)
        body["endpoint"] = relay_endpoint
        response = post(client, body)
        assert response.status_code == 403
        assert "token" not in response.json()
        assert bool(store.get_member(peer.device_id)["token_binding_required"]) == pinned


def test_unicode_advertisement_cannot_authorize_another_ascii_host(pair):
    server, peer, endpoints, client = pair
    endpoints[:] = ["https://faß.example"]
    with server.active():
        assert post(client, request_body(client, server, peer, "https://fass.example")).status_code == 403
        assert post(client, request_body(client, server, peer, "https://xn--fa-hia.example")).status_code == 403


def test_punycoded_advertisement_works_and_rotates_without_aliasing(pair):
    server, peer, endpoints, client = pair
    endpoints[:] = ["https://XN--FA-HIA.example/"]
    with server.active():
        assert post(client, request_body(client, server, peer, "https://xn--fa-hia.example")).status_code == 200
        assert store.get_member(peer.device_id)["token_binding_required"]
        assert post(client, request_body(client, server, peer, "https://fass.example")).status_code == 403
        endpoints[:] = ["https://fass.example"]
        assert post(client, request_body(client, server, peer, "https://xn--fa-hia.example")).status_code == 403
        assert post(client, request_body(client, server, peer, "https://fass.example")).status_code == 200


@pytest.mark.parametrize("host", ["faß.example", "bücher.example", "K.example", "ｆａｓｓ.example",
                                  "-bad.example", "bad-.example", "a..example", "under_score.example"])
def test_non_ascii_and_invalid_dns_hosts_are_rejected(host):
    with pytest.raises(ValueError):
        tokens.canonical_endpoint("https://" + host)


@pytest.mark.asyncio
async def test_client_refuses_unicode_endpoint_before_sending_any_proof(pair):
    server, peer, _, _ = pair
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"challenge": "nonce-from-relay-123456"})
    with peer.active():
        link = transport.PeerLink(server.device_id, [])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="ASCII A-labels"):
                await link._issue_token(client, engine.context(), "https://faß.example")
    assert len(requests) == 1
    assert requests[0].url.raw_host == b"xn--fa-hia.example"
    assert requests[0].url.path.endswith("/challenge")
    assert "signature" not in json.loads(requests[0].content)


def test_transcript_is_versioned_and_unambiguous():
    message = tokens.bound_challenge_message("nonce", "device|id", "HTTPS://EXAMPLE.COM:443/")
    assert json.loads(message) == ["shuffle-device-token-bound-v1", "nonce", "device|id", "https://example.com"]
    assert message != tokens.bound_challenge_message("nonce|device", "id", "https://example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_client_always_sends_both_proofs_to_old_server_without_downgrade(pair, reject):
    server, peer, endpoints, _ = pair
    seen = []
    nonce = "legacy-challenge-123456789"
    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/challenge"):
            return httpx.Response(200, json={"challenge": nonce})  # no support flag from an old server
        seen.append(body)
        assert body["endpoint"] == endpoints[0]
        assert crypto.verify(peer.identity.signing_public,
                             tokens.challenge_message(nonce, server.device_id), crypto.b64u_decode(body["signature"]))
        assert crypto.verify(peer.identity.signing_public,
                             tokens.bound_challenge_message(nonce, server.device_id, endpoints[0]),
                             crypto.b64u_decode(body["bound_signature"]))
        return httpx.Response(403 if reject else 200, json={
            "token": "legacy-token", "device_id": server.device_id, "network_id": server.context.network_id})
    with peer.active():
        link = transport.PeerLink(server.device_id, endpoints[:1])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if reject:
                with pytest.raises(transport.PeerError):
                    await link._issue_token(client, engine.context(), endpoints[0])
            else:
                token, _ = await link._issue_token(client, engine.context(), endpoints[0])
                assert token == "legacy-token"
    assert len(seen) == 1
