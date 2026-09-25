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
from app.network import crypto, engine, enrollment, membership, tokens
from tests.core.network.harness import exchange


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
    issued = client.post("/api/v1/network/peer/auth/challenge", json={"device_id": ctx_of_peer.device_id})
    if issued.status_code != 200:
        return issued  # a non-member is refused before it gets a challenge
    challenge = issued.json()
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
        assert engine.context().member(a.device_id).name == "Desk 2"
        assert client.patch(f"/api/v1/network/members/{a.device_id}", json={"name": "   "}).status_code == 400
        assert store.get_member(a.device_id)["name"] == "Desk 2"
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
        # The revocation moves the membership generation, which every token is
        # bound to, so the old token is refused before membership is even checked.
        engine.revoke_device(b.device_id)
        assert client.get("/api/v1/network/peer/ping", headers=headers).status_code in (401, 403)
        assert _peer_token(client, b_ctx, a.device_id).status_code == 403
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

        device = membership.sign_device_description({
            "device_id": b.device_id, "signing_public": crypto.b64u_encode(b.identity.signing_public),
            "encryption_public": crypto.b64u_encode(b.identity.encryption_public),
            "name": "Laptop", "platform": "linux", "kind": "worker", "legacy_instance_id": "laptop-1234"},
            b.identity.signing_private)
        # Wrong fingerprint claimed by the joiner is refused before the code is consumed.
        wrong = client.post("/api/v1/network/join", json={"code": parsed["code"], "device": device,
                                                          "inviter_fingerprint": "0000-0000-0000-0000-0000-0000-0000-0000"})
        assert wrong.status_code == 400
        posted = client.post("/api/v1/network/join", json={"code": parsed["code"], "device": device,
                                                           "inviter_fingerprint": parsed["fingerprint"]})
        assert posted.status_code == 202, posted.text
        join = posted.json()
        assert join["inviter"]["device_id"] == a.device_id and join["joiner_fingerprint"] == b.identity.fingerprint
        # The consumed code admitted the laptop at once: no approval queue, and the code is single use.
        assert join["status"] == "approved"
        assert client.post("/api/v1/network/join", json={"code": parsed["code"], "device": device}).status_code == 400
        assert client.get("/api/v1/network/joins").json()["joins"] == []
        assert engine.context().member(b.device_id).status == "active"
        assert client.get(f"/api/v1/network/join/{join['join_id']}", params={"secret": "wrong"}).status_code == 404
        # Approving again is harmless for older clients that still call it.
        approved = client.post(f"/api/v1/network/joins/{join['join_id']}/approve", json={})
        assert approved.status_code == 200 and approved.json()["status"] == "approved", approved.text
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
        missing = client.post("/api/v1/network/enroll", json={"endpoint_url": "http://x"})
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


def _client_as_api_user(api_user):
    from fastapi import Request

    def fake_auth(request: Request):
        request.state.api_user = api_user
        return "user"

    app = FastAPI()
    app.include_router(network_api.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = fake_auth
    return TestClient(app)


def test_fork_here_and_hydrate_enforce_api_user_session_scope(workers):
    """NET-08: a project-restricted API user cannot fork or hydrate a session outside its project."""
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_project(project_id="proj-a", name="A", description=None, path="/srv/a")
        database.create_session(session_id="s-scoped", profile_id="default", project_id="proj-a", title="Scoped",
                                is_active=True)
        outsider = _client_as_api_user({"id": "u-out", "project_id": "proj-other"})
        assert outsider.post("/api/v1/network/sessions/s-scoped/fork-here").status_code == 403
        assert outsider.post(f"/api/v1/network/runners/{a.device_id}/sessions/s-scoped/hydrate").status_code == 403
        assert outsider.post("/api/v1/network/sessions/s-missing/fork-here").status_code == 404
        assert outsider.post(f"/api/v1/network/runners/{a.device_id}/sessions/s-missing/hydrate").status_code == 404
        assert a.sql("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 1  # nothing was forked
        database.create_api_user(user_id="u-in", name="Insider", api_key_hash="h" * 64, project_id="proj-a")
        insider = _client_as_api_user({"id": "u-in", "project_id": "proj-a"})
        # In scope is not enough: a key reaches only the sessions it owns (e90f7808).
        assert insider.post("/api/v1/network/sessions/s-scoped/fork-here").status_code == 403
        database.create_session(session_id="s-insider", profile_id="default", project_id="proj-a",
                                title="Insider's", api_user_id="u-in", is_active=True)
        forked = insider.post("/api/v1/network/sessions/s-insider/fork-here")
        assert forked.status_code == 201, forked.text
        assert database.get_session(forked.json()["session_id"])["api_user_id"] == "u-in"


def test_legacy_plaintext_blob_route_is_gone(workers, make_client):
    """NET-18: only the encrypted parts route serves bytes to members."""
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        client = make_client()
        engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                            encryption_public=b.identity.encryption_public, name="B")
        b_ctx = type("Ctx", (), {"device_id": b.device_id, "identity": b.identity})()
        headers = {"Authorization": "Bearer " + _peer_token(client, b_ctx, a.device_id).json()["token"]}
        assert client.get("/api/v1/network/peer/blob/" + "a" * 64, headers=headers).status_code == 404
        assert client.get("/api/v1/network/peer/blob/" + "a" * 64 + "/parts/0", headers=headers).status_code == 404
        assert not any(getattr(r, "path", "") == "/api/v1/network/peer/blob/{hash_value}" for r in network_api.router.routes)


@pytest.mark.parametrize("url,ok", [
    ("http://[::ffff:169.254.169.254]:80", False),       # IPv4-mapped
    ("http://[::ffff:a9fe:a9fe]/", False),               # the same, hex form
    ("http://[::ffff:0:169.254.169.254]/", False),       # IPv4-translated (SIIT)
    ("http://[64:ff9b::169.254.169.254]/", False),       # NAT64 well-known
    ("http://[64:ff9b:1::169.254.1.1]/", False),         # NAT64 local-use
    ("http://[::169.254.169.254]/", False),              # IPv4-compatible
    ("http://[fe80::1%25eth0]:8000", False),             # link-local with a zone
    ("http://[::ffff:192.168.1.20]:8000", True),         # a mapped LAN address stays allowed
    ("http://[64:ff9b::10.0.0.5]:8000", True),
    ("http://[::1]:8000", True),                         # IPv6 loopback, not ::0.0.0.1
    ("http://[2001:db8::1]:8000", True),
], ids=["mapped", "mapped-hex", "translated", "nat64", "nat64-local", "compatible",
        "zone", "mapped-lan", "nat64-lan", "v6-loopback", "global"])
def test_ipv4_carrying_ipv6_endpoints_are_classified_by_the_ipv4(url, ok):
    """R13-12: ``ipaddress`` calls ``::ffff:169.254.169.254`` neither link-local
    nor private, so the "no link-local" rule let the metadata address through."""
    from app.network import endpoints
    assert endpoints.is_valid_endpoint(url) is ok


def test_member_endpoints_are_validated_before_they_are_stored(workers, make_client):
    """NET-22: only plain http(s) addresses without credentials or link-local hosts are kept."""
    from app.network import endpoints
    good = ["http://10.0.0.5:8000", "https://desk.local", "http://192.168.1.20:8000/", "http://[fd00::1]:8000"]
    bad = ["http://169.254.169.254/latest/meta-data", "http://[fe80::1]:8000", "ftp://10.0.0.5", "http://user:pw@10.0.0.5",
           "http://", "javascript:alert(1)", "http://10.0.0.5:99999", "http://a b", "http://10.0.0.5/" + "x" * 600, "", 7]
    assert endpoints.validate_endpoints(good + bad + good) == [u.rstrip("/") for u in good]
    assert len(endpoints.validate_endpoints([f"http://10.0.0.{i}" for i in range(20)])) == endpoints.MAX_ENDPOINTS
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        client = make_client()
        engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                            encryption_public=b.identity.encryption_public, name="B")
        challenge = client.post("/api/v1/network/peer/auth/challenge", json={"device_id": b.device_id}).json()["challenge"]
        signature = tokens.sign_challenge(b.identity.signing_private, challenge, a.device_id)
        token = client.post("/api/v1/network/peer/auth/token", json={
            "device_id": b.device_id, "challenge": challenge, "signature": signature, "incarnation": 1,
            "endpoints": ["http://169.254.169.254/latest", "http://user:pw@10.0.0.9", "http://10.0.0.9:8000"]})
        assert token.status_code == 200, token.text
        assert store.get_member(b.device_id)["endpoints"] == ["http://10.0.0.9:8000"]
        headers = {"Authorization": "Bearer " + token.json()["token"]}
        exchanged = client.post("/api/v1/network/peer/exchange", headers=headers, json={
            "watermarks": {}, "endpoints": ["http://[fe80::1]:8000", "ftp://x", "https://b.example.com/"]})
        assert exchanged.status_code == 200, exchanged.text
        assert store.get_member(b.device_id)["endpoints"] == ["https://b.example.com"]
