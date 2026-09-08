"""The /api/v1/network routes: admin surface, peer plane, join flow."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import cluster as cluster_api
from app.api import network as network_api
from app.api import pairing as pairing_api
from app.api.auth import require_admin, require_auth
from app.db import database
from app.db import network as store
from app.network import crypto, engine, enrollment, tokens, transport
from tests.core.network.harness import Worker, exchange


@pytest.fixture
def workers(tmp_path):
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


@pytest.fixture
def make_client():
    def _make():
        app = FastAPI()
        app.include_router(network_api.router)
        app.include_router(pairing_api.router)
        app.dependency_overrides[require_admin] = lambda: "admin"
        app.dependency_overrides[require_auth] = lambda: "admin"
        return TestClient(app)
    return _make


def _peer_token(client, ctx_of_peer, server_device_id):
    challenge = client.post("/api/v1/network/peer/auth/challenge", json={"device_id": ctx_of_peer.device_id}).json()
    signature = tokens.sign_challenge(ctx_of_peer.identity.signing_private, challenge["challenge"], server_device_id)
    response = client.post("/api/v1/network/peer/auth/token", json={
        "device_id": ctx_of_peer.device_id, "challenge": challenge["challenge"], "signature": signature,
        "incarnation": 1, "endpoints": ["http://peer.local:8000"], "version": "t"})
    return response


def test_status_create_members_recovery_and_conflicts(workers, make_client):
    a = workers("a")
    with a.active():
        client = make_client()
        before = client.get("/api/v1/network/status").json()
        assert before["enrolled"] is False and before["device"]["device_id"] == a.device_id
        created = client.post("/api/v1/network/create", json={"name": "Home", "device_name": "Desk"})
        assert created.status_code == 201 and created.json()["network"]["name"] == "Home"
        assert client.post("/api/v1/network/create", json={}).status_code == 409
        status = client.get("/api/v1/network/status").json()
        assert status["enrolled"] and status["network"]["member_count"] == 1
        me = status["members"][0]
        assert me["is_self"] and me["online"] and me["name"] == "Desk" and me["fingerprint"].count("-") == 7
        renamed = client.patch(f"/api/v1/network/members/{a.device_id}", json={"name": "Desk 2"})
        assert renamed.json()["name"] == "Desk 2"
        assert client.delete(f"/api/v1/network/members/{a.device_id}").status_code == 400
        assert client.delete("/api/v1/network/members/dev_nobody").status_code == 404
        policy_doc = client.get("/api/v1/network/policy").json()
        assert any(row["collection"] == "user_preferences" for row in policy_doc["collections"])
        runners = client.get("/api/v1/network/runners").json()
        assert runners["runners"][0]["is_self"] and runners["self_device_id"] == a.device_id

        assert client.get("/api/v1/network/recovery").json()["configured"] is False
        made = client.post("/api/v1/network/recovery")
        assert made.status_code == 201 and made.json()["recovery_key"].startswith("A1-")
        assert client.get("/api/v1/network/recovery").json()["configured"] is True

        with store.get_db() as conn:
            store.add_conflict(conn.cursor(), collection="tags", record_id="t", losing_clock=1, losing_device_id="x",
                               losing_record={"name": "old"}, winning_clock=2, winning_device_id=a.device_id)
        conflicts = client.get("/api/v1/network/conflicts").json()["conflicts"]
        assert len(conflicts) == 1
        assert client.delete(f"/api/v1/network/conflicts/{conflicts[0]['id']}").status_code == 204
        assert client.get("/api/v1/network/conflicts").json()["conflicts"] == []


def test_peer_plane_requires_membership_and_valid_signatures(workers, make_client):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        client = make_client()
        # B is not a member yet.
        b_ctx = type("Ctx", (), {"device_id": b.device_id, "identity": b.identity})()
        assert _peer_token(client, b_ctx, a.device_id).status_code == 403
        assert client.post("/api/v1/network/peer/exchange", json={}).status_code == 401
        engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                            encryption_public=b.identity.encryption_public, name="B")
        # Wrong signature.
        challenge = client.post("/api/v1/network/peer/auth/challenge", json={"device_id": b.device_id}).json()["challenge"]
        bad = client.post("/api/v1/network/peer/auth/token", json={
            "device_id": b.device_id, "challenge": challenge, "signature": crypto.b64u_encode(bytes(64))})
        assert bad.status_code == 403
        # Replayed challenge is gone.
        good_sig = tokens.sign_challenge(b.identity.signing_private, challenge, a.device_id)
        assert client.post("/api/v1/network/peer/auth/token", json={
            "device_id": b.device_id, "challenge": challenge, "signature": good_sig}).status_code == 400
        token = _peer_token(client, b_ctx, a.device_id)
        assert token.status_code == 200, token.text
        headers = {"Authorization": "Bearer " + token.json()["token"]}
        ping = client.get("/api/v1/network/peer/ping", headers=headers).json()
        assert ping["device_id"] == a.device_id
        assert client.get("/api/v1/network/status").json()["members"][1]["online"] is True
        manifest = client.get("/api/v1/network/peer/snapshot", headers=headers).json()
        assert "membership" in manifest["collections"] and manifest["device_id"] == a.device_id
        chunk = client.get("/api/v1/network/peer/snapshot", headers=headers, params={"collection": "membership"}).json()
        assert chunk["count"] >= 2 and chunk["signature"]
        assert client.get("/api/v1/network/peer/snapshot", headers=headers, params={"collection": "nope"}).status_code == 404
        exchange_resp = client.post("/api/v1/network/peer/exchange", headers=headers, json={"watermarks": {}}).json()
        assert exchange_resp["ops"] and exchange_resp["watermarks"][a.device_id]
        # Revoked devices lose access within the token lifetime: immediately here.
        engine.revoke_device(b.device_id)
        assert client.get("/api/v1/network/peer/ping", headers=headers).status_code == 403
        assert tokens.verify_token("dvt_garbage") is None


def test_join_flow_end_to_end(workers, make_client, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="Desk")
        client = make_client()
        monkeypatch.setattr("app.core.cluster.advertise_url", lambda: ("http://10.0.0.5:8000", "config"))
        grant = client.post("/api/v1/pairing/grants", json={"purpose": "network:join", "endpoint_url": "http://10.0.0.5:8000"})
        assert grant.status_code == 201, grant.text
        link = grant.json()["join"]
        assert link["url"].startswith("shuffle://network/join?") and link["fingerprint"] == a.identity.fingerprint
        assert grant.json()["qr_svg"].startswith("<svg")
        parsed = enrollment.parse_join_link(link["url"])
        assert parsed["endpoint"] == "http://10.0.0.5:8000" and parsed["fingerprint"] == link["fingerprint"]

        device = {"signing_public": crypto.b64u_encode(b.identity.signing_public),
                  "encryption_public": crypto.b64u_encode(b.identity.encryption_public),
                  "name": "Laptop", "platform": "linux", "legacy_instance_id": "laptop-1234"}
        # Wrong fingerprint claimed by the joiner is refused before the code is consumed.
        wrong = client.post("/api/v1/network/join", json={"code": parsed["code"], "device": device,
                                                          "inviter_fingerprint": "0000-0000-0000-0000-0000-0000-0000-0000"})
        assert wrong.status_code == 400
        posted = client.post("/api/v1/network/join", json={"code": parsed["code"], "device": device,
                                                           "inviter_fingerprint": parsed["fingerprint"]})
        assert posted.status_code == 202, posted.text
        join = posted.json()
        assert join["inviter"]["device_id"] == a.device_id and join["joiner_fingerprint"] == b.identity.fingerprint
        # The code is single use.
        assert client.post("/api/v1/network/join", json={"code": parsed["code"], "device": device}).status_code == 400
        pending = client.get("/api/v1/network/joins").json()["joins"]
        assert pending[0]["fingerprint"] == b.identity.fingerprint and pending[0]["name"] == "Laptop"
        assert client.get(f"/api/v1/network/join/{join['join_id']}", params={"secret": "wrong"}).status_code == 404
        assert client.get(f"/api/v1/network/join/{join['join_id']}", params={"secret": join["poll_secret"]}).json()["status"] == "pending"
        assert client.post(f"/api/v1/network/joins/{join['join_id']}/approve", json={"fingerprint_confirmed": False}).status_code == 400
        approved = client.post(f"/api/v1/network/joins/{join['join_id']}/approve", json={"fingerprint_confirmed": True})
        assert approved.status_code == 200, approved.text
        picked = client.get(f"/api/v1/network/join/{join['join_id']}", params={"secret": join["poll_secret"]}).json()
        assert picked["status"] == "approved" and picked["response"]["network_id"]
        response = picked["response"]
        # Second pickup finds nothing sensitive.
        assert client.get(f"/api/v1/network/join/{join['join_id']}", params={"secret": join["poll_secret"]}).json().get("response") is None
        assert engine.context().legacy_instance_map["laptop-1234"] == b.device_id
        manifest = engine.snapshot_manifest()
        chunks = [engine.snapshot_chunk(c, 0) for c in manifest["collections"]]
        database.set_system_setting("tts_model", "voice-2")
        engine.scan_all()
    with b.active():
        ctx = engine.enroll_from_response(response, device_name="Laptop", legacy_instance_id="laptop-1234")
        assert ctx.bootstrap_state == "syncing"
        assert engine.record(store.get_db().__enter__().cursor(), "tags", "x", {"id": "x", "name": "n", "color": "#fff"}) is None
        for chunk in chunks:
            engine.apply_snapshot_chunk(chunk)
        engine.adopt_watermarks(manifest["watermarks"])
        engine.finish_bootstrap()
        assert engine.context().member(a.device_id).name == "Desk"
    exchange(a, b)
    with b.active():
        assert database.get_system_setting("tts_model") == "voice-2"
        status = make_client().get("/api/v1/network/status").json()
        assert status["network"]["member_count"] == 2
        assert status["members"][0]["is_self"]


def test_enrollment_status_and_cancel(workers, make_client):
    a = workers("a")
    with a.active():
        client = make_client()
        assert client.get("/api/v1/network/enroll").json() == {"enrollment": None, "enrolled": False}
        bad = client.post("/api/v1/network/enroll", json={"link": "not a link"})
        assert bad.status_code == 400
        missing = client.post("/api/v1/network/enroll", json={"endpoint_url": "http://x", "code": "AAAA"})
        assert missing.status_code == 400
        assert client.delete("/api/v1/network/enroll").status_code == 204


def test_cluster_installs_marks_this_worker_by_device_id(workers, make_client):
    """/cluster/installs lists the runner directory once enrolled, so its self
    marker and ``self_instance_id`` must use the device id the rows carry, not
    the legacy instance id (which a fresh network never maps)."""
    a = workers("a")
    with a.active():
        client = make_client()
        client.app.include_router(cluster_api.router)
        assert client.post("/api/v1/network/create", json={"name": "Home", "device_name": "Desk"}).status_code == 201
        listing = client.get("/api/v1/cluster/installs").json()
        assert listing["self_instance_id"] == a.device_id
        assert [row["id"] for row in listing["installs"]] == [a.device_id]
        me = listing["installs"][0]
        assert me["is_self"] is True and me["online"] is True and me["device_name"] == "Desk"
