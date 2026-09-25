"""What a worker's peer plane does with bad input from an authenticated member.

A member's device token only proves who is talking; every operation it pushes
is still checked on its own. These tests push malformed, forged, foreign,
oversized and replayed envelopes over the real ``/peer/*`` routes (worker A
served in-process) and assert what each worker holds afterwards.
"""
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.api.auth import require_admin, require_auth
from app.db import network as store
from app.db.base import get_db
from app.network import crypto, engine, envelope as envelope_mod, tokens, transport
from tests.core.network.harness import converge, exchange, join, owner_pref, set_owner_pref
from tests.core.network.test_ownership_revocation import Network, _network


def _client():
    app = FastAPI()
    app.include_router(network_api.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = lambda: "admin"
    return TestClient(app)


def _bearer(server, member):
    """A device token ``server`` issues to ``member`` through the real challenge routes."""
    client = _client()
    with server.active():
        challenge = client.post("/api/v1/network/peer/auth/challenge", json={"device_id": member.device_id})
        assert challenge.status_code == 200, challenge.text
        signature = tokens.sign_challenge(member.identity.signing_private, challenge.json()["challenge"],
                                          server.device_id)
        issued = client.post("/api/v1/network/peer/auth/token", json={
            "device_id": member.device_id, "challenge": challenge.json()["challenge"], "signature": signature})
        assert issued.status_code == 200, issued.text
    return client, {"Authorization": "Bearer " + issued.json()["token"]}


def _pref_ops(worker, key):
    with worker.active():
        return [o for o in store.ops_since({}) if o["record_id"].endswith("|" + key)]


def _watermark(worker, origin, incarnation=1):
    with worker.active(), get_db() as conn:
        return store.get_watermark(conn.cursor(), origin, incarnation)


def test_pushed_batch_applies_only_its_valid_operations(workers):
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "good", "value")
    [op] = _pref_ops(b, "good")
    set_owner_pref(b, "second", 2)
    [op2] = _pref_ops(b, "second")
    before = _watermark(a, b.device_id)
    client, headers = _bearer(a, b)
    batch = [
        {"garbage": True},                                                   # malformed
        dict(op, unknown_field=1),                                           # unknown header field
        dict(op, record_id="admin|@owner|other"),                            # signature mismatch
        dict(op, network_id="net_elsewhere"),                                # another network
        dict(op, ciphertext=op2["ciphertext"]),                              # payload moved between headers
        dict(op, ciphertext="A" * ((1 << 22) + 1)),                          # oversized field
        dict(op, clock=True),                                                # wrong type
        op,                                                                  # the real one
        op,                                                                  # replayed in the same batch
    ]
    with a.active():
        result = client.post("/api/v1/network/peer/ops", headers=headers, json={"ops": batch})
    assert result.status_code == 200, result.text
    body = result.json()
    assert (body["applied"], body["duplicate"], body["rejected"]) == (1, 1, 7)
    for reason in ("bad signature", "wrong network"):
        assert reason in body["reasons"]
    assert any(r.startswith("malformed") for r in body["reasons"])
    assert owner_pref(a, "good") == "value"
    assert owner_pref(a, "other") is None and owner_pref(a, "second") is None
    assert _watermark(a, b.device_id) == before + 1 == op["origin_seq"]
    assert len(_pref_ops(a, "good")) == 1
    # The same push again (a retry after a lost response) is all duplicates.
    with a.active():
        again = client.post("/api/v1/network/peer/ops", headers=headers, json={"ops": [op]}).json()
    assert (again["applied"], again["duplicate"]) == (0, 1)
    # The next op in B's stream still applies: nothing above stalled it.
    exchange(b, a)
    assert owner_pref(a, "second") == 2


def test_oversized_and_invalid_peer_requests_are_refused_before_any_work(workers):
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "k", 1)
    [op] = _pref_ops(b, "k")
    client, headers = _bearer(a, b)
    with a.active():
        ops_before = store.count_ops()
        # More ops than one push may carry: refused whole, nothing applied.
        too_many = client.post("/api/v1/network/peer/ops", headers=headers, json={"ops": [op] * 2001})
        assert too_many.status_code == 422
        assert client.post("/api/v1/network/peer/ops", headers=headers, json={"ops": "nope"}).status_code == 422
        assert client.post("/api/v1/network/peer/exchange", headers=headers,
                           json={"limit": 2001}).status_code == 422
        assert client.post("/api/v1/network/peer/exchange", headers=headers,
                           json={"watermarks": {"dev_x": {"1": "many"}}}).status_code == 422
        assert client.post("/api/v1/network/peer/exchange", headers=headers,
                           json={"endpoints": ["http://h%d.local" % i for i in range(9)]}).status_code == 422
        # Stream lookups validate the identity they name.
        assert client.get(f"/api/v1/network/peer/ops/{'d' * 65}/1/1", headers=headers).status_code == 400
        assert client.get(f"/api/v1/network/peer/ops/{b.device_id}/0/1", headers=headers).status_code == 400
        assert client.get(f"/api/v1/network/peer/ops/{b.device_id}/1/0", headers=headers).status_code == 400
        assert client.get(f"/api/v1/network/peer/ops/{b.device_id}/1/999", headers=headers).status_code == 404
        # A control body that is not an object.
        assert client.post("/api/v1/network/peer/control/turns", headers=headers, json=[1]).status_code == 400
        # Without a token nothing is looked at.
        assert client.post("/api/v1/network/peer/ops", json={"ops": [op]}).status_code == 401
        assert store.count_ops() == ops_before
    assert owner_pref(a, "k") is None


def test_tokens_die_with_membership_changes_and_revocation(workers):
    a, b, c = _network(workers, "a", "b", "c")
    client, headers = _bearer(a, b)
    with a.active():
        assert client.get("/api/v1/network/peer/ping", headers=headers).status_code == 200
    # Any membership change on A (a new generation) voids every token at once.
    join(a, workers("d"))
    with a.active():
        assert client.get("/api/v1/network/peer/ping", headers=headers).status_code == 401
    client, headers = _bearer(a, b)
    c_client, c_headers = _bearer(a, c)
    with a.active():
        engine.revoke_device(b.device_id)
        assert client.post("/api/v1/network/peer/exchange", headers=headers, json={}).status_code == 401
        refused = client.post("/api/v1/network/peer/auth/challenge", json={"device_id": b.device_id})
        assert refused.status_code == 403
        # C's token from before the revocation is void too; a fresh one works.
        assert c_client.post("/api/v1/network/peer/exchange", headers=c_headers, json={}).status_code == 401
    c_client, c_headers = _bearer(a, c)
    with a.active():
        assert c_client.post("/api/v1/network/peer/exchange", headers=c_headers, json={}).status_code == 200


async def test_endpoint_answering_for_another_device_is_not_trusted(workers, monkeypatch):
    a, b, c = _network(workers, "a", "b", "c")
    net = Network(monkeypatch, a=a)
    async with httpx.AsyncClient() as client:
        with b.active():
            # C's advertised address actually reaches A.
            link = transport.PeerLink(c.device_id, [net.url("a")])
            with pytest.raises(transport.PeerError):
                await link.authenticate(client, engine.context())
            assert link.token is None and link.base_url is None
    # A refused to mint a token for a challenge signed to another audience.
    assert ("POST", "/api/v1/network/peer/auth/token") in net.requests("a")
    assert b.device_id not in transport.online_devices()


async def test_sync_pass_backs_off_unreachable_peers_and_holds_the_outbox(workers, monkeypatch):
    a, b = _network(workers, "a", "b")
    net = Network(monkeypatch, a=a)
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://nowhere.local:9"])
        engine.hold_outbox(True)
        try:
            set_owner_pref(b, "while-held", 1)  # a local write while held is not published
            assert _pref_ops(b, "while-held") == []
            async with httpx.AsyncClient() as client:
                assert await transport.sync_pass(client) == {}
            link = transport._links[a.device_id]
            assert link.failures == 1 and link.backoff_until > time.monotonic()
            assert "unreachable" in (link.last_error or "")
            assert engine.outbox_held() is True
            assert [p for p in store.list_peers() if p["device_id"] == a.device_id][0]["last_error"]
            # A pass inside the backoff window does not dial again.
            async with httpx.AsyncClient() as client:
                assert await transport.sync_pass(client) == {}
            assert link.failures == 1
            # The peer becomes reachable: the stream is compared, the outbox opens.
            link.endpoints, link.backoff_until = [net.url("a")], 0.0
            async with httpx.AsyncClient() as client:
                results = await transport.sync_pass(client)
            assert results[a.device_id]["received"] == 0
            assert engine.outbox_held() is False and link.failures == 0 and link.last_error is None
            engine.scan_all()
            assert len(_pref_ops(b, "while-held")) == 1
        finally:
            engine.hold_outbox(False)
    exchange(b, a)
    assert owner_pref(a, "while-held") == 1


async def test_fully_refused_push_is_reported_unsettled_and_not_retried_forever(workers, monkeypatch):
    """C forwards a removed device's late writes; A refuses every one of them."""
    a, b, c = _network(workers, "a", "b", "c")
    net = Network(monkeypatch, a=a)
    for i in range(3):
        set_owner_pref(b, f"late{i}", i)
    exchange(b, c)  # C still trusts B and holds its writes
    with a.active():
        engine.revoke_device(b.device_id)
    monkeypatch.setattr(transport, "BATCH_LIMIT", 3)
    async with httpx.AsyncClient() as client:
        with c.active():
            store.touch_member(a.device_id, endpoints=[net.url("a")])
            link = transport.PeerLink(a.device_id, [net.url("a")])
            summary = await link.sync_once(client, engine.context())
            assert summary["sent"] == 3 and summary.get("unsettled") is True
            # C learned the revocation in the same pass.
            assert engine.context().member(b.device_id).status == "revoked"
            # A's watermark moved past the refused ops, so nothing is resent.
            again = await link.sync_once(client, engine.context())
            assert again["sent"] == 0
    for i in range(3):
        assert owner_pref(a, f"late{i}") is None
    assert _watermark(a, b.device_id) == _watermark(c, b.device_id)
    converge([a, c])
    for i in range(3):
        assert owner_pref(a, f"late{i}") is None


def test_forwarded_envelope_is_byte_identical_and_cannot_be_rekeyed(workers):
    """A forwarder can only pass an op on unchanged: re-sealing it under another
    key or re-signing it as someone else is refused by the receiver."""
    a, b, c = _network(workers, "a", "b", "c")
    set_owner_pref(a, "forwarded", "x")
    exchange(a, b)
    [original] = _pref_ops(a, "forwarded")
    [forwarded] = _pref_ops(b, "forwarded")
    assert forwarded == original
    with b.active():
        ctx = engine.context()
        forged = envelope_mod.Envelope.from_dict(original)
        forged.ciphertext = crypto.encrypt_payload(crypto.generate_network_key(),
                                                   envelope_mod.record_bytes({"key": "forwarded"}), forged.aad())
        forged.sign(ctx.identity.signing_private)  # signed by B, but claims to be A's op
    with c.active():
        result = engine.apply_batch([forged.to_dict()])
        assert result.rejected == 1 and "bad signature" in result.reasons
        assert engine.apply_batch([forwarded]).applied == 1
    assert owner_pref(c, "forwarded") == "x"
